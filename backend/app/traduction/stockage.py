"""Persistance de la couche de traduction (``traduction.json``).

Même patron que ``edition.ecrire_repliques`` : écriture atomique (tampon puis
``replace``) pour ne jamais laisser un JSON tronqué. Le fichier est distinct de
``repliques.json`` — la bande originale n'est jamais touchée.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from ..paths import safe_path

NOM_FICHIER = "traduction.json"
_NB_ESSAIS = 10
_DELAI_S = 0.02


class TraductionStore:
    """Lecture/écriture de ``traduction.json`` (atomique, non destructif)."""

    def __init__(self, job_dir: str | Path):
        self.job_dir = Path(job_dir)

    def chemin(self) -> Path:
        return safe_path(self.job_dir, NOM_FICHIER)

    def lire(self) -> dict:
        """Couche persistée (``{}`` si aucune traduction n'a encore été faite)."""
        cible = self.chemin()
        if not cible.is_file():
            return {}
        return json.loads(cible.read_text(encoding="utf-8"))

    def ecrire(self, couche: dict) -> Path:
        """Écrit la couche de façon atomique. Retourne le chemin du fichier."""
        cible = self.chemin()
        tampon = cible.with_suffix(".tmp")
        contenu = json.dumps(couche, ensure_ascii=False, indent=2)
        for _ in range(_NB_ESSAIS):
            try:
                tampon.write_text(contenu, encoding="utf-8")
                tampon.replace(cible)
                return cible
            except OSError:
                time.sleep(_DELAI_S)
        raise OSError(f"Impossible d'écrire la couche de traduction : {cible}")
