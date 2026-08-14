"""Chemins sûrs : toute entrée utilisateur reste confinée dans le dossier prévu."""
from __future__ import annotations

from pathlib import Path

from .errors import RythmoError


def safe_path(base: str | Path, *segments: str) -> Path:
    """Résout ``base/segments…`` et garantit le confinement dans ``base``.

    - chemins avec espaces/accents : acceptés ;
    - ``..``, chemins absolus étrangers, liens vers l'extérieur : ``E004``.
    """
    base_r = Path(base).resolve()
    cible = base_r.joinpath(*segments).resolve()
    if cible != base_r and base_r not in cible.parents:
        raise RythmoError("E004", f"Chemin hors du dossier autorisé : {cible}")
    return cible
