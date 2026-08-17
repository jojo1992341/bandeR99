"""Vérifie que torch CUDA est bien installé quand un GPU NVIDIA est présent.

Usage : .venv/Scripts/python scripts/verifier_gpu.py
Retour : 0 si cohérent (pas de GPU, ou GPU + torch CUDA) ;
         1 si un GPU NVIDIA est détecté mais torch n'exploite pas CUDA.

Appelé par scripts/install_win.ps1 à la fin de l'installation : une build CPU
de torch installée par erreur fait échouer l'installation avec un message
clair au lieu de laisser l'application afficher silencieusement « CPU ».
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.devices import diagnostic_device, verifier_installation  # noqa: E402


def main() -> int:
    gpu_present = shutil.which("nvidia-smi") is not None
    device, _ = diagnostic_device()
    ok, message = verifier_installation(gpu_present, device)
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
