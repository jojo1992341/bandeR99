"""Cache de traduction (slice 8) — analyses et résultats mémoisés.

``TranslationCache`` mémoise les analyses pures (syllabes, phonèmes) par
``(texte, langue)`` et les résultats de traduction par clé stable : il ne
recalcule que ce qui change (clé différente) ou après invalidation.
``MoteurAvecCache`` enveloppe un moteur (lourd) pour mémoiser ses sorties —
le moteur sous-jacent n'est appelé qu'une fois par clé.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from .engine import TranslationEngine
from .phonemes import PhonemeAnalyzer
from .syllabes import SyllableAnalyzer


def _compter_syllabes(texte: str, langue: str) -> int:
    return SyllableAnalyzer(langue).compter(texte)


def _phonemes_par_defaut(texte: str, langue: str) -> tuple[str, ...]:
    return tuple(PhonemeAnalyzer(langue).analyser(texte).aplatir())


def _cle_contexte(contexte: Mapping[str, Any]) -> str:
    """Clé stable d'un contexte (indépendante de l'ordre des clés)."""
    return json.dumps(dict(contexte), sort_keys=True, default=str, ensure_ascii=False)


class TranslationCache:
    """Mémoise les analyses pures et les résultats, avec invalidation."""

    def __init__(self, analyseur_syllabes=None, analyseur_phonemes=None):
        self._analyseur_syllabes = analyseur_syllabes or _compter_syllabes
        self._analyseur_phonemes = analyseur_phonemes or _phonemes_par_defaut
        self._analyses: dict[tuple, Any] = {}
        self._resultats: dict[tuple, str] = {}

    def syllabes(self, texte: str, langue: str) -> int:
        """Syllabes de ``texte``, calculées une seule fois par ``(langue, texte)``."""
        cle = ("syllabes", langue, texte)
        if cle not in self._analyses:
            self._analyses[cle] = self._analyseur_syllabes(texte, langue)
        return self._analyses[cle]

    def phonemes(self, texte: str, langue: str) -> tuple[str, ...]:
        """Phonèmes de ``texte``, calculés une seule fois par ``(langue, texte)``."""
        cle = ("phonemes", langue, texte)
        if cle not in self._analyses:
            self._analyses[cle] = tuple(self._analyseur_phonemes(texte, langue))
        return self._analyses[cle]

    def cle_resultat(self, modele: str, temperature: float | None,
                     texte: str, contexte: Mapping[str, Any]) -> tuple:
        """Clé stable d'un résultat de traduction (moteur + texte + contexte + température)."""
        return (modele, temperature, texte, _cle_contexte(contexte))

    def consulter_resultat(self, cle: tuple) -> str | None:
        return self._resultats.get(cle)

    def stocker_resultat(self, cle: tuple, cible: str) -> None:
        self._resultats[cle] = cible

    def invalider(self) -> None:
        """Vide le cache (analyses et résultats)."""
        self._analyses.clear()
        self._resultats.clear()


class MoteurAvecCache(TranslationEngine):
    """Enveloppe un moteur et mémoise ses résultats (jamais deux appels pour la même clé)."""

    def __init__(self, moteur: TranslationEngine, cache: TranslationCache,
                 nom: str | None = None, temperature: float | None = None):
        self.moteur = moteur
        self.cache = cache
        self.nom = nom or type(moteur).__name__
        self.temperature = temperature

    def traduire(self, texte: str, contexte: Mapping[str, Any]) -> str:
        cle = self.cache.cle_resultat(self.nom, self.temperature, texte, contexte)
        hit = self.cache.consulter_resultat(cle)
        if hit is not None:
            return hit
        cible = self.moteur.traduire(texte, contexte)
        self.cache.stocker_resultat(cle, cible)
        return cible

    def charger(self) -> None:
        self.moteur.charger()

    def arreter(self) -> None:
        self.moteur.arreter()
