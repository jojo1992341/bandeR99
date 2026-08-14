@echo off
chcp 65001 >nul
title Rythmo Dub
cd /d %~dp0..
if not exist .venv (
  echo [ERREUR] Environnement absent : lancez d'abord scripts\install_win.ps1
  pause
  exit /b 1
)
rem Front separe (html/js/css) : verifier que les trois fichiers sont presents
for %%f in (frontend\index.html frontend\app.js frontend\style.css) do (
  if not exist %%f (
    echo [ERREUR] Fichier front manquant : %%f
    pause
    exit /b 1
  )
)
echo Demarrage de Rythmo Dub sur http://localhost:8000 ...
start "" http://localhost:8000
.\.venv\Scripts\python.exe -m uvicorn app.__main__:appli --app-dir backend --host 0.0.0.0 --port 8000
pause
