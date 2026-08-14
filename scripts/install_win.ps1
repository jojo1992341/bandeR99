# Rythmo Dub — installation Windows 11
# Usage : clic droit > "Executer avec PowerShell"   (ou : powershell -ExecutionPolicy Bypass -File scripts\install_win.ps1)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "== Rythmo Dub : installation ==" -ForegroundColor Cyan

# 1) Python
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { Write-Error "Python introuvable. Installez Python 3.12 depuis https://www.python.org (cochez 'Add to PATH')." }
python --version

# 2) ffmpeg via winget (sinon repli automatique : static-ffmpeg a l'execution)
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host "ffmpeg absent -> tentative winget install Gyan.FFmpeg" -ForegroundColor Yellow
    try { winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements }
    catch { Write-Host "winget indisponible : les binaires portables seront telecharges au 1er lancement." -ForegroundColor Yellow }
}

# 3) Environnement virtuel
if (-not (Test-Path ".venv")) { python -m venv .venv }
.\.venv\Scripts\python.exe -m pip install --upgrade pip

# 4) torch : CUDA si GPU NVIDIA detecte, sinon CPU
$cuda = $false
try { & nvidia-smi | Out-Null; $cuda = $true } catch { $cuda = $false }
if ($cuda) {
    Write-Host "GPU NVIDIA detecte -> torch CUDA" -ForegroundColor Green
    .\.venv\Scripts\python.exe -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu124
} else {
    Write-Host "Pas de GPU NVIDIA -> torch CPU" -ForegroundColor Yellow
    .\.venv\Scripts\python.exe -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
}

# 5) Dependances applicatives
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 6) Modele MediaPipe (landmarks visage, ~3,6 Mo)
.\.venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'backend'); from app.lips import chemin_modele; print('modele visage :', chemin_modele())"

# 7) Resemblyzer — diarisation par embeddings vocaux (voix de même tessiture)
#    Optionnel : sans lui, la séparation des voix retombe sur la méthode par
#    hauteur (T56) et l'application fonctionne intégralement.
Write-Host ""
Write-Host "== Resemblyzer (diarisation par embeddings vocaux) ==" -ForegroundColor Cyan
Write-Host "  Optionnel : separe automatiquement deux voix de MEME tessiture"
Write-Host "  (T109-T111). Necessite torch (deja installe a l'etape 4)."
try {
    .\.venv\Scripts\python.exe -m pip install webrtcvad-wheels
    .\.venv\Scripts\python.exe -m pip install resemblyzer --no-deps
    Write-Host "  Resemblyzer installe." -ForegroundColor Green
} catch {
    Write-Host "  Resemblyzer NON installe (reseau bloque ?) : la diarisation" -ForegroundColor Yellow
    Write-Host "  retombe sur la methode par hauteur. Rejouez plus tard :" -ForegroundColor Yellow
    Write-Host "  .\.venv\Scripts\python.exe -m pip install webrtcvad-wheels" -ForegroundColor Yellow
    Write-Host "  .\.venv\Scripts\python.exe -m pip install resemblyzer --no-deps" -ForegroundColor Yellow
}

# 8) pyannote.audio — diarisation de niveau studio (T112/T113)
#    Optionnel : modèle « gated » — licence à accepter sur
#    https://huggingface.co/pyannote/speaker-diarization-3.1 puis token via
#    la variable RYTHMO_HF_TOKEN (ou HF_TOKEN). Sans lui, la diarisation
#    retombe sur Resemblyzer puis sur la méthode par hauteur.
Write-Host ""
Write-Host "== pyannote.audio (diarisation studio) ==" -ForegroundColor Cyan
Write-Host "  Optionnel : separe des voix de meme tessiture que Resemblyzer"
Write-Host "  ne distingue pas (T112/T113). Exige un token Hugging Face :"
Write-Host "  1) acceptez la licence sur huggingface.co/pyannote/speaker-diarization-3.1"
Write-Host "  2) definissez RYTHMO_HF_TOKEN (ou HF_TOKEN) dans vos variables d'environnement"
try {
    .\.venv\Scripts\python.exe -m pip install pyannote.audio
    Write-Host "  pyannote.audio installe." -ForegroundColor Green
} catch {
    Write-Host "  pyannote.audio NON installe (reseau bloque ?) : la diarisation" -ForegroundColor Yellow
    Write-Host "  retombe sur Resemblyzer puis la hauteur. Rejouez plus tard :" -ForegroundColor Yellow
    Write-Host "  .\.venv\Scripts\python.exe -m pip install pyannote.audio" -ForegroundColor Yellow
}

# 9) Playwright + Chromium (tests navigateur uniquement : T30/T66/T70/T75/T93)
#    Optionnel : l'application tourne sans. ~170 Mo de navigateur a telecharger.
Write-Host ""
Write-Host "== Playwright (tests navigateur) ==" -ForegroundColor Cyan
Write-Host "  Optionnel : necessaire uniquement pour les tests navigateur"
Write-Host "  (T30/T66/T70/T75/T93). L'application fonctionne sans."
try {
    .\.venv\Scripts\python.exe -m pip install playwright
    .\.venv\Scripts\python.exe -m playwright install chromium
    Write-Host "  Playwright installe." -ForegroundColor Green
} catch {
    Write-Host "  Playwright NON installe (reseau bloque ?) : les tests" -ForegroundColor Yellow
    Write-Host "  navigateur seront skippes. Rejouez plus tard :" -ForegroundColor Yellow
    Write-Host "  .\.venv\Scripts\python.exe -m pip install playwright" -ForegroundColor Yellow
    Write-Host "  .\.venv\Scripts\python.exe -m playwright install chromium" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Installation terminee !" -ForegroundColor Green
Write-Host "  - Lancer l'application : scripts\lancer.bat" -ForegroundColor Green
Write-Host "  - Tests rapides (sans IA) : .\.venv\Scripts\python.exe -m pytest -m \"not integration\" -q"
Write-Host "  - Tests complets (IA + navigateur) : .\.venv\Scripts\python.exe -m pytest -q"
Write-Host "  - Mode cloud (optionnel) : definir RYTHMO_OPENAI_KEY puis choisir"
Write-Host "    « Cloud » dans l'option Moteur de transcription du front."
