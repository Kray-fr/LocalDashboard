"""Dashboard PC local : serveur léger qui expose les infos système en JSON
et sert la page static/index.html. Écoute uniquement sur 127.0.0.1."""
import hashlib
import ipaddress
import json
import os
import sys
import re
import socket
import urllib.error
import urllib.parse
from collections import deque
import platform
import shutil
import subprocess
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psutil

import connectors

VERSION = 3
BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"

DEFAULT_CONFIG = {
    "port": 8080,
    "name": "",
    "location": {"name": "Solre-le-Château", "latitude": 50.17, "longitude": 4.08},
    "feeds": [
        {"name": "Le Monde", "url": "https://www.lemonde.fr/rss/une.xml"},
        {"name": "France Info", "url": "https://www.francetvinfo.fr/titres.rss"},
    ],
    "shortcuts": [
        {"name": "YouTube", "url": "https://www.youtube.com"},
        {"name": "Gmail", "url": "https://mail.google.com"},
        {"name": "GitHub", "url": "https://github.com"},
        {"name": "Wikipédia", "url": "https://fr.wikipedia.org"},
        {"name": "Claude", "url": "https://claude.ai"},
    ],
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    path = BASE / "config.json"
    if path.exists():
        try:
            cfg.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            print(f"config.json ignoré ({exc})")
    return cfg


CONFIG = load_config()

# ---------------------------------------------------------------- système

HISTORY = deque(maxlen=720)  # 1 point / 5 s, 1 heure
STATE = {
    "cpu": 0.0, "cores": [], "ram": {}, "swap": {}, "disks": [],
    "net": {"down": 0, "up": 0}, "battery": None, "gpu": None, "procs": [],
}
INFO = {
    "host": platform.node(),
    "os": f"{platform.system()} {platform.release()}",
    "cpu": platform.processor(),
    "threads": psutil.cpu_count(logical=True),
    "cores": psutil.cpu_count(logical=False),
}
SKIP_PROCS = {"system idle process", "system", "idle", "registry", "memory compression"}


def read_gpu():
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
            creationflags=0x08000000 if platform.system() == "Windows" else 0,
        ).stdout.strip().splitlines()[0]
        name, util, used, total, temp = [x.strip() for x in out.split(",")]
        return {"name": name, "util": float(util), "mem_used": float(used) * 2**20,
                "mem_total": float(total) * 2**20, "temp": float(temp)}
    except Exception:
        return None


def read_disks():
    disks = []
    for part in psutil.disk_partitions(all=False):
        if "cdrom" in part.opts or not part.fstype:
            continue
        try:
            u = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        disks.append({"name": part.mountpoint, "used": u.used, "total": u.total, "percent": u.percent})
    return disks


def read_procs():
    groups = {}
    ncpu = psutil.cpu_count(logical=True) or 1
    for p in psutil.process_iter(["name", "cpu_percent", "memory_info"]):
        try:
            name = p.info["name"] or "?"
            if name.lower() in SKIP_PROCS:
                continue
            g = groups.setdefault(name, {"name": name, "cpu": 0.0, "mem": 0, "count": 0})
            g["cpu"] += (p.info["cpu_percent"] or 0.0) / ncpu
            g["mem"] += p.info["memory_info"].rss if p.info["memory_info"] else 0
            g["count"] += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return sorted(groups.values(), key=lambda g: (g["cpu"], g["mem"]), reverse=True)[:25]


IO = {"t": time.time(), "c": None}
try:
    IO["c"] = psutil.disk_io_counters()
except Exception:
    pass


def record(now):
    ps = STATE.get("procs") or []
    keep = {p["name"]: p for p in sorted(ps, key=lambda p: -p["cpu"])[:6] + sorted(ps, key=lambda p: -p["mem"])[:6]}
    dr = dw = 0
    try:
        io = psutil.disk_io_counters()
        if io and IO["c"]:
            dt = max(now - IO["t"], 0.001)
            dr, dw = (io.read_bytes - IO["c"].read_bytes) / dt, (io.write_bytes - IO["c"].write_bytes) / dt
        IO.update(t=now, c=io)
    except Exception:
        pass
    g = STATE.get("gpu")
    HISTORY.append({"t": int(now), "cpu": round(STATE["cpu"], 1), "ram": STATE["ram"]["percent"], "down": int(STATE["net"]["down"]), "up": int(STATE["net"]["up"]),
                    "c": [round(x) for x in STATE["cores"]], "p": [[p["name"], round(p["cpu"], 1), p["mem"]] for p in keep.values()],
                    "dr": int(dr), "dw": int(dw), "g": g["util"] if g else 0})


def sampler():
    psutil.cpu_percent(percpu=True)
    last_net, last_t, tick = psutil.net_io_counters(), time.time(), 0
    while True:
        time.sleep(1)
        now = time.time()
        cores = psutil.cpu_percent(percpu=True)
        net = psutil.net_io_counters()
        dt = max(now - last_t, 0.001)
        STATE["net"] = {"down": (net.bytes_recv - last_net.bytes_recv) / dt,
                        "up": (net.bytes_sent - last_net.bytes_sent) / dt}
        last_net, last_t = net, now
        vm, sw = psutil.virtual_memory(), psutil.swap_memory()
        STATE["cpu"] = sum(cores) / len(cores)
        STATE["cores"] = cores
        STATE["ram"] = {"used": vm.used, "total": vm.total, "percent": vm.percent}
        STATE["swap"] = {"used": sw.used, "total": sw.total, "percent": sw.percent}
        b = psutil.sensors_battery()
        STATE["battery"] = ({"percent": b.percent, "plugged": b.power_plugged,
                             "secs": b.secsleft if isinstance(b.secsleft, int) and b.secsleft > 0 else None}
                            if b else None)
        if tick % 3 == 0:
            STATE["procs"] = read_procs()
            STATE["nproc"] = len(psutil.pids())
            STATE["gpu"] = read_gpu()
        if tick % 10 == 0:
            STATE["disks"] = read_disks()
        if tick % 5 == 0:
            record(now)
        tick += 1


# ------------------------------------------------------------------- actus

SETTINGS = BASE / "settings.json"


def load_settings():
    try:
        return json.loads(SETTINGS.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


NEWS_CACHE = {"time": 0, "data": []}


def fetch_feed(feed):
    req = urllib.request.Request(feed["url"], headers={"User-Agent": "Mozilla/5.0 dashboard-pc"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        root = ET.fromstring(resp.read())
    items = []
    for it in root.iter("item"):
        title, link = it.findtext("title"), it.findtext("link")
        if title and link:
            items.append({"title": title.strip(), "link": link.strip()})
        if len(items) >= 6:
            break
    return {"name": feed["name"], "items": items}


def get_news(force=False):
    if not force and time.time() - NEWS_CACHE["time"] < 600 and NEWS_CACHE["data"]:
        return NEWS_CACHE["data"]
    data = []
    st = load_settings()
    feeds = st["feeds"] if isinstance(st.get("feeds"), list) else CONFIG["feeds"]
    for feed in feeds:
        if not str(feed.get("url", "")).startswith(("http://", "https://")):
            continue
        try:
            data.append(fetch_feed(feed))
        except Exception as exc:
            data.append({"name": feed["name"], "items": [], "error": str(exc)})
    NEWS_CACHE.update(time=time.time(), data=data)
    return data


# ------------------------------------------------------------------ logos de sites
FAV = BASE / "favicons"


def _public(host):
    """Refuse les adresses locales ou privées (évite que le serveur interroge ton réseau)."""
    try:
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return True
    except Exception:
        return False


class _Safe(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _public(urllib.parse.urlparse(newurl).hostname or ""):
            raise urllib.error.URLError("redirection refusée")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


OPENER = urllib.request.build_opener(_Safe)


def _fetch(url, limit):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (dashboard-pc favicon)", "Accept": "*/*"})
    with OPENER.open(req, timeout=6) as r:
        return r.read(limit + 1)[:limit], r.headers.get_content_type(), r.geturl()


def _is_image(data, ctype):
    return ctype.startswith("image/") or data[:4] in (b"\x00\x00\x01\x00", b"\x89PNG", b"GIF8", b"RIFF") or data[:2] == b"\xff\xd8" or data[:5] in (b"<?xml", b"<svg ")


def _icon_urls(page):
    data, _, final = _fetch(page, 150_000)
    text, found = data.decode("utf-8", "ignore"), []
    for tag in re.findall(r"<link\b[^>]*>", text, re.I):
        rel = re.search(r'rel\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        href = re.search(r'href\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        if not rel or not href or "icon" not in rel.group(1).lower():
            continue
        size = re.search(r'sizes\s*=\s*["\'](\d+)x', tag, re.I)
        score = (2000 if "apple-touch" in rel.group(1).lower() else 1000) + (int(size.group(1)) if size else 0)
        url = urllib.parse.urljoin(final, href.group(1).strip())
        if url.startswith(("http://", "https://")):
            found.append((score, url))
    return [u for _, u in sorted(found, reverse=True)]


def favicon(site):
    u = urllib.parse.urlparse(site)
    if u.scheme not in ("http", "https") or not u.hostname or not _public(u.hostname):
        return None
    FAV.mkdir(exist_ok=True)
    key = hashlib.sha1(u.hostname.encode()).hexdigest()[:16]
    img, ctf, none, now = FAV / (key + ".bin"), FAV / (key + ".ct"), FAV / (key + ".none"), time.time()
    if img.exists() and now - img.stat().st_mtime < 7 * 86400:
        return img.read_bytes(), ctf.read_text()
    if none.exists() and now - none.stat().st_mtime < 86400:
        return None
    base = "%s://%s/" % (u.scheme, u.netloc)
    try:
        cands = _icon_urls(base)
    except Exception:
        cands = []
    for icon in cands + [base + "favicon.ico"]:
        try:
            data, ctype, _ = _fetch(icon, 300_000)
        except Exception:
            continue
        if data and _is_image(data, ctype):
            ctype = ctype if ctype.startswith("image/") else "image/x-icon"
            img.write_bytes(data)
            ctf.write_text(ctype)
            return data, ctype
    none.write_text("1")
    return None


def favicon_debug(site):
    u, out = urllib.parse.urlparse(site), {"site": site}
    if u.scheme not in ("http", "https") or not u.hostname:
        out["erreur"] = "adresse invalide"
        return out
    out["adresse_publique"] = _public(u.hostname)
    if not out["adresse_publique"]:
        out["erreur"] = "le nom du site ne mène pas à une adresse publique (DNS, VPN ou filtrage réseau ?)"
        return out
    base = "%s://%s/" % (u.scheme, u.netloc)
    try:
        cands = _icon_urls(base)
        out["page"] = "lue"
    except Exception as exc:
        cands, out["page"] = [], "échec : %s" % exc
    out["essais"] = []
    for icon in (cands + [base + "favicon.ico"])[:4]:
        try:
            data, ctype, _ = _fetch(icon, 300_000)
            out["essais"].append("%s -> %s, %d octets, image : %s" % (icon, ctype, len(data), _is_image(data, ctype)))
        except Exception as exc:
            out["essais"].append("%s -> échec : %s" % (icon, exc))
    return out


def restart():
    try:
        subprocess.Popen([sys.executable, str(BASE / "server.py")], cwd=str(BASE), close_fds=True,
                         creationflags=0x08000008 if platform.system() == "Windows" else 0)
    finally:
        os._exit(0)


class Srv(ThreadingHTTPServer):
    allow_reuse_address = platform.system() != "Windows"  # évite que deux serveurs se partagent le port


# ------------------------------------------------------------------ serveur

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC), **kwargs)

    def log_message(self, *args):
        pass

    def send_json(self, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        port = CONFIG["port"]
        ok = {f"http://localhost:{port}", f"http://127.0.0.1:{port}"}
        if self.path not in ("/api/accounts", "/api/settings", "/api/restart") or self.headers.get("Origin") not in ok:
            self.send_error(403)
            return
        if self.path == "/api/restart":
            self.send_json({"ok": True})
            threading.Timer(0.6, restart).start()
            return
        try:
            size = int(self.headers.get("Content-Length", 0))
            if size > 200_000:
                raise ValueError
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            self.send_error(400)
            return
        if self.path == "/api/settings":
            SETTINGS.write_text(json.dumps(body, ensure_ascii=False), "utf-8")
            NEWS_CACHE["time"] = 0
            self.send_json({"ok": True})
        else:
            connectors.save(body)
            self.send_json(connectors.status())

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/stats":
            self.send_json({**STATE, "uptime": time.time() - psutil.boot_time()})
        elif path == "/api/info":
            self.send_json({**INFO, "ram_total": psutil.virtual_memory().total})
        elif path == "/api/news":
            self.send_json(get_news("force=1" in self.path))
        elif path.startswith("/api/") and path[5:] in connectors.NAMES:
            self.send_json(connectors.get(path[5:], "force=1" in self.path))
        elif path == "/api/accounts":
            self.send_json(connectors.status())
        elif path == "/api/favicon":
            site = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("url", [""])[0]
            try:
                res = None if self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site") else favicon(site)
            except Exception:
                res = None
            if not res:
                self.send_error(404)
                return
            body, ctype = res
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "sandbox; default-src 'none'; style-src 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/version":
            self.send_json({"v": VERSION})
        elif path == "/api/favicon-test":
            self.send_json(favicon_debug(urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("url", [""])[0]))
        elif path == "/api/history":
            self.send_json(list(HISTORY))
        elif path == "/api/settings":
            self.send_json(load_settings())
        elif path == "/api/config":
            self.send_json({k: CONFIG[k] for k in ("name", "location", "shortcuts", "feeds")})
        else:
            super().do_GET()


if __name__ == "__main__":
    threading.Thread(target=sampler, daemon=True).start()
    port = int(CONFIG["port"])
    server = None
    for _ in range(20):  # après un redémarrage, l'ancien serveur met un instant à libérer le port
        try:
            server = Srv(("127.0.0.1", port), Handler)
            break
        except OSError:
            time.sleep(0.5)
    if server is None:
        sys.exit("Le port %d est déjà utilisé : un autre serveur du dashboard tourne encore." % port)
    print(f"Dashboard prêt sur http://localhost:{port}  (Ctrl+C pour arrêter)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
