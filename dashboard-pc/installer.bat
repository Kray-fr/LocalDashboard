@echo off
cd /d "%~dp0"
echo Installation de la dependance (psutil)...
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo Erreur : Python n'est pas installe ou pas dans le PATH.
  echo Installe-le depuis python.org en cochant "Add python.exe to PATH".
  pause
  exit /b 1
)
echo Ajout du lancement automatique a l'ouverture de session...
set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Dashboard-PC.lnk"
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%LNK%'); $s.TargetPath='%~dp0lancer_silencieux.vbs'; $s.WorkingDirectory='%~dp0'; $s.Save()"
echo.
echo Termine. Le serveur se lancera a chaque ouverture de session Windows.
echo Pour le demarrer tout de suite, double-clique sur lancer_silencieux.vbs
pause
