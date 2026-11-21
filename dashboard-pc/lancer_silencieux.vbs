' Lance le serveur sans fenêtre noire
Set fso = CreateObject("Scripting.FileSystemObject")
dossier = fso.GetParentFolderName(WScript.ScriptFullName)
CreateObject("WScript.Shell").CurrentDirectory = dossier
CreateObject("WScript.Shell").Run "pythonw server.py", 0, False
