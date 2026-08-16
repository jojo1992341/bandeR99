"""Glossaire de traduction (slice 6) — prioritaire sur la traduction automatique.

``GlossaryManager`` porte les correspondances terme→terme imposées par
l'utilisateur (noms propres, expressions, terminologie) et les termes
**interdits**. Une entrée exacte du glossaire est **prioritaire** : elle court-circuite
le moteur. Les correspondances partielles sont, elles, transmises au moteur
(``contexte``) pour qu'il les respecte — jamais de donnée hors du glossaire.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable


def normaliser_cle(texte: str) -> str:
    """Minuscules et accents retirés : clés de glossaire insensibles à la casse/accents."""
    if texte is None:
        return ""
    decompose = unicodedata.normalize("NFD", str(texte).lower())
    return "".join(c for c in decompose if unicodedata.category(c) != "Mn")


@dataclass
class EntreeGlossaire:
    """Une correspondance imposée : ``source`` → ``cible`` (note optionnelle)."""

    source: str
    cible: str
    note: str = ""


class GlossaryManager:
    """Correspondances terme→terme (prioritaires) + termes interdits."""

    def __init__(self, entrees: Iterable[EntreeGlossaire | tuple] | None = None,
                 interdits: Iterable[str] | None = None):
        self.entrees: dict[str, str] = {}
        for entree in (entrees or []):
            if isinstance(entree, EntreeGlossaire):
                self.entrees[normaliser_cle(entree.source)] = entree.cible
            else:
                self.entrees[normaliser_cle(entree[0])] = entree[1]
        self.interdits: set[str] = {normaliser_cle(i) for i in (interdits or [])}

    def traduire(self, texte: str) -> str | None:
        """Cible prioritaire si ``texte`` est une entrée exacte ; sinon ``None``."""
        return self.entrees.get(normaliser_cle(texte))

    def correspondances(self, texte: str) -> dict[str, str]:
        """Termes du glossaire présents dans ``texte`` (mot entier) → {source: cible}."""
        norm = normaliser_cle(texte)
        resultat: dict[str, str] = {}
        for source, cible in self.entrees.items():
            if source and re.search(rf"\b{re.escape(source)}\b", norm):
                resultat[source] = cible
        return resultat

    def interdit(self, texte: str) -> bool:
        """Vrai si ``texte`` (normalisé) est un terme interdit."""
        return normaliser_cle(texte) in self.interdits

    def contexte(self, texte: str) -> dict[str, str] | None:
        """Correspondances à transmettre au moteur, ou ``None`` si aucune."""
        correspondances = self.correspondances(texte)
        return correspondances or None
