"""Scoring de compatibilité doublage (spec §8) — complet et configurable (slice 5).

Huit critères pondérés, **jamais figés en dur** : les poids sont injectables et
les critères sont des **fonctions pures injectables** (REFACTOR slice 5). Chaque
critère renvoie ``float | None`` ; ``None`` signifie « donnée absente » → le
critère est omis de l'agrégat (repli neutre, jamais de pénalité artificielle).

Les critères ``semantic/naturalness/character/context`` n'ont pas encore de
signal dédié à ce stade : leur implémentation par défaut est **neutre** (100) et
sera enrichie par les slices 6 (contexte/personnages) et 8 (fidélité du moteur
local). Toute valeur est normalisée dans [0, 100] avant agrégation.
"""
from __future__ import annotations

from .phonemes import similarite_phonemes
from .syllabes import SYLLABES_PAR_SECONDE

# Ordre canonique des 8 critères (spec §16).
CRITERES = (
    "semantic_score",
    "duration_score",
    "syllable_score",
    "phonetic_score",
    "lip_sync_score",
    "naturalness_score",
    "character_score",
    "context_score",
)

POIDS_DEFAUT = {
    "semantic_score": 1.0,
    "duration_score": 1.2,
    "syllable_score": 0.8,
    "phonetic_score": 0.6,
    "lip_sync_score": 0.7,
    "naturalness_score": 1.0,
    "character_score": 0.9,
    "context_score": 0.8,
}


def _score_syllabes(source: int, cible: int) -> float:
    """100 pour un nombre de syllabes identique ; −20 pts par syllabe d'écart."""
    return max(0.0, 100.0 - 20.0 * abs(int(source) - int(cible)))


def _score_duree(duree_disponible_s: float, target_syllabes: int,
                 syllabes_par_seconde: float) -> float:
    """100 si la cible est prononçable dans la fenêtre ; pénalité linéaire sinon."""
    if target_syllabes <= 0:
        return 100.0
    estimee = target_syllabes / float(syllabes_par_seconde)
    if estimee <= duree_disponible_s:
        return 100.0
    if duree_disponible_s <= 0:
        return 0.0
    return max(0.0, 100.0 * (2.0 - estimee / duree_disponible_s))


def _score_phonetique(source: list[str], cible: list[str]) -> float:
    """Similarité des phonèmes source/cible (recouvrement multiset, 0–100)."""
    return similarite_phonemes(list(source), list(cible))


# ------------------- critères comme fonctions pures (injectables) -------------

def _critere_duree(ctx: dict) -> float:
    return _score_duree(ctx["duree_s"], ctx["target_syllabes"], SYLLABES_PAR_SECONDE)


def _critere_syllabes(ctx: dict) -> float:
    return _score_syllabes(ctx["source_syllabes"], ctx["target_syllabes"])


def _critere_phonetique(ctx: dict) -> float | None:
    source = ctx.get("source_phonemes") or []
    cible = ctx.get("target_phonemes") or []
    if not source or not cible:
        return None
    return _score_phonetique(source, cible)


def _critere_lipsync(ctx: dict) -> float | None:
    analyse = ctx.get("analyse_lipsync")
    if analyse is None or analyse.score is None:
        return None
    return float(analyse.score)


def _critere_semantique(ctx: dict) -> float:
    """Fidélité sémantique : neutre (100) tant que le moteur local n'en fournit pas (slice 8)."""
    return 100.0


def _critere_naturel(ctx: dict) -> float:
    """Naturalité : neutre (100) à ce stade (enrichie slice 6)."""
    return 100.0


def _critere_personnage(ctx: dict) -> float:
    """Adéquation au registre du personnage : neutre (100) à ce stade (enrichie slice 6)."""
    return 100.0


def _critere_contexte(ctx: dict) -> float:
    """Cohérence de contexte : neutre (100) à ce stade (enrichie slice 6)."""
    return 100.0


CRITERES_DEFAUT: dict[str, callable] = {
    "semantic_score": _critere_semantique,
    "duration_score": _critere_duree,
    "syllable_score": _critere_syllabes,
    "phonetic_score": _critere_phonetique,
    "lip_sync_score": _critere_lipsync,
    "naturalness_score": _critere_naturel,
    "character_score": _critere_personnage,
    "context_score": _critere_contexte,
}


def _borner(valeur: float) -> float:
    """Normalise un score de critère dans [0, 100] (jamais hors bornes)."""
    return max(0.0, min(100.0, float(valeur)))


class DubbingScorer:
    """Agrège les 8 critères disponibles, pondérés par ``poids`` (configurable)."""

    def __init__(self, poids: dict[str, float] | None = None,
                 criteres: dict[str, callable] | None = None):
        self.poids = dict(poids) if poids else dict(POIDS_DEFAUT)
        self.criteres = dict(CRITERES_DEFAUT)
        if criteres:
            self.criteres.update(criteres)

    def scorer(self, source_texte: str, cible_texte: str, duree_s: float,
               source_syllabes: int, target_syllabes: int,
               source_phonemes: list[str] | None = None,
               target_phonemes: list[str] | None = None,
               analyse_lipsync=None, contexte=None) -> tuple[float, dict[str, float]]:
        """Retourne ``(score_global, {critere: valeur})`` — scores explicables.

        Un critère sans donnée (``None``) est omis de l'agrégat — repli neutre.
        Chaque valeur est normalisée dans [0, 100] ; le score global est la
        moyenne pondérée des critères présents.
        """
        ctx = {
            "source_texte": source_texte,
            "cible_texte": cible_texte,
            "duree_s": float(duree_s),
            "source_syllabes": int(source_syllabes),
            "target_syllabes": int(target_syllabes),
            "source_phonemes": list(source_phonemes or []),
            "target_phonemes": list(target_phonemes or []),
            "analyse_lipsync": analyse_lipsync,
            "contexte": contexte,
        }
        scores: dict[str, float] = {}
        for nom, fonction in self.criteres.items():
            valeur = fonction(ctx)
            if valeur is None:
                continue
            scores[nom] = _borner(valeur)
        ponderation = {c: self.poids.get(c, 0.0) for c in scores}
        denominateur = sum(ponderation.values()) or 1.0
        score_global = sum(ponderation[c] * v for c, v in scores.items()) / denominateur
        return score_global, scores
