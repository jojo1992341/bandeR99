"""Mémoire de traduction (slice 6) — cohérence terminologique sur tout le film.

``TranslationMemory`` retient les correspondances source→cible déjà validées :
une phrase (ou un terme) déjà traduit est **réutilisé** tel quel, et les termes
connus sont transmis au moteur (``contexte``) pour qu'il reste cohérent. C'est
ce qui garantit qu'un terme ne change pas de traduction d'une réplique à l'autre.
"""
from __future__ import annotations

import re

from .glossaire import normaliser_cle


class TranslationMemory:
    """Correspondances source→cible retenues (phrase ou terme)."""

    def __init__(self, entrees: dict[str, str] | None = None):
        self.entrees: dict[str, str] = {
            normaliser_cle(k): v for k, v in (entrees or {}).items()
        }

    def enregistrer(self, source: str, cible: str) -> None:
        """Retient une correspondance (termes/phrases déjà traduits)."""
        if source:
            self.entrees[normaliser_cle(source)] = cible

    def consulter(self, texte: str) -> str | None:
        """Cible déjà retenue pour ``texte`` exact ; sinon ``None``."""
        return self.entrees.get(normaliser_cle(texte))

    def correspondances(self, texte: str) -> dict[str, str]:
        """Termes connus présents dans ``texte`` (mot entier) → {source: cible}."""
        norm = normaliser_cle(texte)
        return {
            source: cible for source, cible in self.entrees.items()
            if source and re.search(rf"\b{re.escape(source)}\b", norm)
        }

    def contexte(self, texte: str) -> dict[str, str] | None:
        correspondances = self.correspondances(texte)
        return correspondances or None

    def to_dict(self) -> dict:
        return dict(self.entrees)
