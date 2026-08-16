"""Humour, idiomes et répliques courtes (slice 6).

``HumourManager`` porte les adaptations d'idiomes/jeux de mots (source → effet →
adaptation) et détecte les **interjections** et répliques courtes. Une
interjection (« ah », « oh »…) ou une réplique courte ne doit jamais être
supprimée par le moteur : le traducteur la préserve (repli identité).
"""
from __future__ import annotations

import re

from .glossaire import normaliser_cle

# Interjections / répliques courtes (français et voisines) à préserver telles
# quelles — leur suppression casserait le rythme et le sens de la bande.
INTERJECTIONS = frozenset((
    "ah", "oh", "he", "hé", "eh", "euh", "ouf", "pff", "pfff", "aïe", "aie",
    "hola", "holà", "hey", "oui", "non", "bon", "hum", "ouais",
))

_LONGUEUR_COURTE = 12  # réplique courte : un seul mot court


class HumourManager:
    """Idiomes/jeux de mots + détection des interjections et répliques courtes."""

    def __init__(self, idiomes: dict[str, str] | None = None):
        self.idiomes: dict[str, str] = {
            normaliser_cle(k): v for k, v in (idiomes or {}).items()
        }

    def est_interjection(self, texte: str) -> bool:
        """Vrai si ``texte`` est une interjection (« ah », « oh »…)."""
        return normaliser_cle(texte) in INTERJECTIONS

    def est_replique_courte(self, texte: str) -> bool:
        """Vrai pour une interjection ou une réplique très courte (1 mot court)."""
        norm = normaliser_cle(texte).strip()
        if not norm:
            return False
        if norm in INTERJECTIONS:
            return True
        mots = norm.split()
        return len(mots) == 1 and len(norm) <= _LONGUEUR_COURTE

    def adaptations(self, texte: str) -> dict[str, str]:
        """Idiomes connus présents dans ``texte`` → {idiome: adaptation}."""
        norm = normaliser_cle(texte)
        return {
            idiome: adaptation for idiome, adaptation in self.idiomes.items()
            if idiome and idiome in norm
        }

    def contexte(self, texte: str) -> dict[str, str] | None:
        adaptations = self.adaptations(texte)
        return adaptations or None
