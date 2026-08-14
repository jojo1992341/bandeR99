"""Localisation des binaires ffmpeg / ffprobe (portable Windows 11 & Linux).

Ordre de recherche :
1. variable d'environnement ``RYTHMO_FFMPEG`` (dossier contenant les 2 binaires) ;
2. le ``PATH`` système (cas d'une installation winget/chocolatey sous Windows) ;
3. repli : binaires portables via le paquet ``static-ffmpeg`` (téléchargés au besoin).
"""
from __future__ import annotations

import os
import shutil
from functools import lru_cache

_EXE_SUFFIX = ".exe" if os.name == "nt" else ""


def _trouver_binaire(nom: str) -> str:
    """Retourne le chemin du binaire ffmpeg ou ffprobe, ou lève RuntimeError."""
    executable = nom + _EXE_SUFFIX

    dossier_env = os.environ.get("RYTHMO_FFMPEG")
    if dossier_env:
        candidat = os.path.join(dossier_env, executable)
        if os.path.isfile(candidat):
            return candidat

    dans_path = shutil.which(executable)
    if dans_path:
        return dans_path

    try:  # repli : paquet static-ffmpeg (télécharge des binaires portables)
        from static_ffmpeg import run as _srun

        ffmpeg_path, ffprobe_path = _srun.get_or_fetch_platform_executables_else_raise()
        return ffmpeg_path if nom == "ffmpeg" else ffprobe_path
    except Exception as exc:  # pragma: no cover - dépend du réseau
        raise RuntimeError(
            f"Binaire {nom} introuvable : installez ffmpeg (winget install Gyan.FFmpeg) "
            "ou définissez la variable d'environnement RYTHMO_FFMPEG."
        ) from exc


@lru_cache(maxsize=1)
def get_ffmpeg_path() -> str:
    """Chemin absolu de l'exécutable ffmpeg."""
    return _trouver_binaire("ffmpeg")


@lru_cache(maxsize=1)
def get_ffprobe_path() -> str:
    """Chemin absolu de l'exécutable ffprobe."""
    return _trouver_binaire("ffprobe")
