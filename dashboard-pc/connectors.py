"""Comptes connectés. Les identifiants sont stockés en clair dans accounts.json (local, jamais renvoyés au navigateur)."""
import datetime as dt
import email
import email.utils
import imaplib
import json
import time
import urllib.request
import xml.etree.ElementTree as ET
from email.header import decode_header, make_header
from pathlib import Path

FILE = Path(__file__).with_name("accounts.json")
CACHE = {}
NAMES = ("github", "mail", "agenda", "steam", "youtube", "gitlab")
KEYS = {"github": "github", "mail": "mail_pass", "agenda": "agenda", "steam": "steam_key", "youtube": "youtube", "gitlab": "gitlab"}
FIELDS = ("github", "mail_user", "mail_pass", "imap", "agenda", "steam_key", "steam_id", "youtube", "gitlab")


GROUPS = {"github": ["github"], "mail": ["mail_user", "mail_pass", "imap"], "agenda": ["agenda"], "steam": ["steam_key", "steam_id"], "youtube": ["youtube"], "gitlab": ["gitlab"]}
PLAIN = ("mail_user", "imap", "steam_id", "youtube")


def load():
    try:
        return json.loads(FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def save(new):
    d = {} if new.get("reset") else load()
    for k in GROUPS.get(new.get("remove"), []):
        d.pop(k, None)
    d.update({k: str(v).strip() for k, v in new.items() if k in FIELDS and str(v).strip()})
    FILE.write_text(json.dumps(d), "utf-8")
    CACHE.clear()


def status():
    c = load()
    r = {n: KEYS[n] in c for n in NAMES}
    r["values"] = {k: c[k] for k in PLAIN if k in c}
    return r


TYPES = {"PullRequest": "Pull request", "Issue": "Issue", "Release": "Release", "Commit": "Commit", "Discussion": "Discussion"}


def _github(c):
    r = urllib.request.Request("https://api.github.com/notifications?per_page=10", headers={
        "Authorization": "Bearer " + c["github"], "User-Agent": "dashboard-pc", "Accept": "application/vnd.github+json"})
    d = json.load(urllib.request.urlopen(r, timeout=8))
    items = []
    for n in d:
        u = n["subject"].get("url") or ""
        link = u.replace("https://api.github.com/repos/", "https://github.com/").replace("/pulls/", "/pull/") if u else n["repository"]["html_url"]
        items.append({"title": n["subject"]["title"], "link": link, "when": n.get("updated_at"),
                      "sub": n["repository"]["full_name"] + " · " + TYPES.get(n["subject"]["type"], n["subject"]["type"])})
    return {"count": len(d), "items": items}


def _mail(c):
    m = imaplib.IMAP4_SSL(c.get("imap", "imap.gmail.com"), timeout=10)
    m.login(c["mail_user"], c["mail_pass"])
    m.select("INBOX", readonly=True)
    ids = m.search(None, "UNSEEN")[1][0].split()
    dec = lambda v: str(make_header(decode_header(v or "")))
    items = []
    for i in ids[-10:][::-1]:
        h = email.message_from_bytes(m.fetch(i, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")[1][0][1])
        name, addr = email.utils.parseaddr(dec(h["From"]))
        try:
            when = email.utils.parsedate_to_datetime(h["Date"]).isoformat()
        except Exception:
            when = None
        items.append({"title": dec(h["Subject"]) or "(sans objet)", "sub": name or addr, "when": when})
    m.logout()
    return {"count": len(ids), "items": items}


def _agenda(c):
    req = urllib.request.Request(c["agenda"], headers={"User-Agent": "dashboard-pc"})
    txt = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", "ignore").replace("\r\n ", "").replace("\n ", "")
    now, out = dt.datetime.now(), []
    for blk in txt.split("BEGIN:VEVENT")[1:]:
        s = t = loc = None
        for ln in blk.splitlines():
            if ln.startswith("DTSTART"):
                s = ln.split(":")[-1].strip()
            elif ln.startswith("SUMMARY"):
                t = ln.split(":", 1)[1].strip()
            elif ln.startswith("LOCATION"):
                loc = ln.split(":", 1)[1].strip().replace("\\,", ",")
        if not (s and t):
            continue
        try:
            d = dt.datetime.strptime(s[:15], "%Y%m%dT%H%M%S") if "T" in s else dt.datetime.strptime(s[:8], "%Y%m%d")
        except ValueError:
            continue
        if s.endswith("Z"):
            d = d.replace(tzinfo=dt.timezone.utc).astimezone().replace(tzinfo=None)
        if d >= now or ("T" not in s and d.date() == now.date()):
            out.append((d, t, "T" not in s, loc))
    out.sort(key=lambda x: x[0])
    return {"count": len(out), "items": [{"title": t, "when": d.isoformat(), "allday": a, "sub": loc or ""} for d, t, a, loc in out[:10]]}


def _steam(c):
    if "steam_id" not in c:
        raise ValueError("SteamID manquant")
    u = ("https://api.steampowered.com/IPlayerService/GetRecentlyPlayedGames/v0001/?key=%s&steamid=%s&count=10&format=json"
         % (c["steam_key"], c["steam_id"]))
    r = json.load(urllib.request.urlopen(u, timeout=8))["response"]
    g = r.get("games", [])
    return {"count": r.get("total_count", len(g)),
            "items": [{"title": x["name"], "link": "https://store.steampowered.com/app/%s" % x["appid"],
                       "sub": "%.1f h en 2 semaines · %d h au total" % (x.get("playtime_2weeks", 0) / 60, x.get("playtime_forever", 0) / 60)} for x in g]}


def _youtube(c):
    ns, vids = "{http://www.w3.org/2005/Atom}", []
    for cid in [x.strip() for x in c["youtube"].split(",") if x.strip()][:8]:
        try:
            req = urllib.request.Request("https://www.youtube.com/feeds/videos.xml?channel_id=" + cid, headers={"User-Agent": "dashboard-pc"})
            root = ET.fromstring(urllib.request.urlopen(req, timeout=8).read())
        except Exception:
            continue
        for e in root.findall(ns + "entry")[:4]:
            vids.append((e.findtext(ns + "published") or "", e.findtext(ns + "title"), e.find(ns + "author").findtext(ns + "name"), e.find(ns + "link").get("href")))
    if not vids:
        raise ValueError("Aucune vidéo trouvée (ID de chaîne correct ?)")
    vids.sort(reverse=True)
    week = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).isoformat()
    return {"count": sum(v[0] > week for v in vids), "items": [{"title": v[1], "sub": v[2], "link": v[3], "when": v[0]} for v in vids[:10]]}


def _gitlab(c):
    r = urllib.request.Request("https://gitlab.com/api/v4/todos?state=pending&per_page=10", headers={"PRIVATE-TOKEN": c["gitlab"], "User-Agent": "dashboard-pc"})
    d = json.load(urllib.request.urlopen(r, timeout=8))
    return {"count": len(d), "items": [{"title": t["target"].get("title") or t["body"], "sub": (t.get("project") or {}).get("path_with_namespace", ""),
                                        "link": t.get("target_url"), "when": t.get("created_at")} for t in d]}


def _open(name, c):
    return {"github": "https://github.com/notifications",
            "mail": "https://mail.google.com/mail/u/0/#inbox" if "gmail" in c.get("imap", "imap.gmail.com") else None,
            "agenda": "https://calendar.google.com" if "google" in c.get("agenda", "") else None,
            "steam": "https://steamcommunity.com/profiles/%s" % c.get("steam_id", ""),
            "youtube": "https://www.youtube.com", "gitlab": "https://gitlab.com/dashboard/todos"}[name]


def get(name, force=False):
    c = load()
    key = KEYS[name]
    if key not in c:
        return {"off": True}
    if not force and name in CACHE and time.time() - CACHE[name][0] < 300:
        return CACHE[name][1]
    try:
        data = {"github": _github, "mail": _mail, "agenda": _agenda, "steam": _steam, "youtube": _youtube, "gitlab": _gitlab}[name](c)
        data["open"] = _open(name, c)
    except Exception as exc:
        data = {"error": str(exc)[:140]}
    CACHE[name] = (time.time(), data)
    return data
