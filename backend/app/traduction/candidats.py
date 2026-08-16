"""Génération de candidats de traduction (slice 5).

``CandidateGenerator`` interroge le moteur ``nombre`` fois pour produire des
candidats **distincts** (A/B/C…). Un moteur déterministe (repli hors ligne)
renvoie toujours le même texte : le générateur le déduplique et ne rend alors
qu'un seul candidat — comportement gracieux, jamais d'erreur.
"""
from __future__ import annotations

from typing import Any, Mapping

from .engine import TranslationEngine


class CandidateGenerator:
    """Produit ``nombre`` candidats distincts à partir d'un moteur."""

    def __init__(self, moteur: TranslationEngine, nombre: int = 3):
        self.moteur = moteur
        self.nombre = max(1, int(nombre))

    def generer(self, texte: str, contexte: Mapping[str, Any]) -> list[str]:
        """Candidats distincts et non vides, dans l'ordre de production."""
        candidats: list[str] = []
        vus: set[str] = set()
        for _ in range(self.nombre):
            produit = self.moteur.traduire(texte, dict(contexte))
            if produit is None:
                continue
            produit = str(produit).strip()
            if not produit or produit in vus:
                continue
            vus.add(produit)
            candidats.append(produit)
        return candidats
