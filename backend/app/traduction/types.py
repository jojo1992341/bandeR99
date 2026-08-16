"""Structures de données de la couche de traduction (spec §16).

Une ``TraductionEntree`` ne remplace jamais la réplique source : elle la
complète (source conservée, cible ajoutée, scores et statut persistés).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

STATUT_EN_ATTENTE = "en_attente"
STATUT_TRADUIT = "traduit"
STATUT_ERREUR = "erreur"
STATUT_VERROUILLE = "verrouille"
STATUT_EXCLU = "exclu"


@dataclass
class TraductionCandidat:
    """Un candidat de traduction produit par le moteur, avec son score."""

    texte: str = ""
    score_global: float = 0.0
    scores: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"texte": self.texte, "score_global": self.score_global,
                "scores": dict(self.scores)}


@dataclass
class TraductionEntree:
    """Traduction d'une réplique : source intacte + cible + scores + statut.

    ``source_phonemes``/``target_phonemes`` (slice 4) portent la séquence
    aplatie des phonèmes mot à mot ; sans données phonétiques, ces listes
    restent vides — le score correspondant est alors neutre (omis).
    """

    source_text: str = ""
    target_text: str = ""
    statut: str = STATUT_EN_ATTENTE
    source_syllabes: int = 0
    target_syllabes: int = 0
    score_global: float = 0.0
    scores: dict[str, float] = field(default_factory=dict)
    candidats: list[TraductionCandidat] = field(default_factory=list)
    iteration_count: int = 0
    erreur: str = ""
    explications: list[str] = field(default_factory=list)
    source_phonemes: list[str] = field(default_factory=list)
    target_phonemes: list[str] = field(default_factory=list)
    verrouillee: bool = False
    exclue: bool = False

    def to_dict(self) -> dict:
        return {
            "source_text": self.source_text,
            "target_text": self.target_text,
            "statut": self.statut,
            "source_syllabes": self.source_syllabes,
            "target_syllabes": self.target_syllabes,
            "score_global": self.score_global,
            "scores": dict(self.scores),
            "candidats": [c.to_dict() for c in self.candidats],
            "iteration_count": self.iteration_count,
            "erreur": self.erreur,
            "explications": list(self.explications),
            "source_phonemes": list(self.source_phonemes),
            "target_phonemes": list(self.target_phonemes),
            "verrouillee": self.verrouillee,
            "exclue": self.exclue,
        }


@dataclass
class CoucheTraduction:
    """Couche ``traduction.json`` : versionnée, non destructive."""

    version: int = 1
    langue_source: str = ""
    langue_cible: str = ""
    modele: str = ""
    progression: dict = field(default_factory=dict)
    entrees: dict[str, TraductionEntree] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "langue_source": self.langue_source,
            "langue_cible": self.langue_cible,
            "modele": self.modele,
            "progression": dict(self.progression),
            "entrees": {rid: e.to_dict() for rid, e in self.entrees.items()},
        }


@dataclass
class RepliqueContexte:
    """Contexte complet d'une réplique (spec §16, slice 6).

    N'expose au moteur que les données **réellement disponibles** : ``to_dict``
    omet tout champ vide (sérialisation compacte, REFACTOR slice 6).
    """

    source_text: str = ""
    personnage: str = ""
    precedent: str = ""
    suivant: str = ""
    scene: str = ""
    glossaire: dict[str, str] = field(default_factory=dict)
    profil: dict[str, Any] = field(default_factory=dict)
    memoire: dict[str, str] = field(default_factory=dict)
    idiomes: dict[str, str] = field(default_factory=dict)
    interjection: bool = False

    def to_dict(self) -> dict:
        """Sérialisation compacte : uniquement les champs disponibles."""
        d: dict[str, Any] = {"source_text": self.source_text}
        if self.personnage:
            d["speaker"] = self.personnage
        if self.precedent:
            d["precedent"] = self.precedent
        if self.suivant:
            d["suivant"] = self.suivant
        if self.scene:
            d["scene"] = self.scene
        if self.glossaire:
            d["glossaire"] = dict(self.glossaire)
        if self.profil:
            d["profil"] = dict(self.profil)
        if self.memoire:
            d["memoire"] = dict(self.memoire)
        if self.idiomes:
            d["idiomes"] = dict(self.idiomes)
        if self.interjection:
            d["interjection"] = True
        return d

    def to_prompt(self) -> str:
        """Forme compacte pour un prompt de moteur (une ligne par donnée)."""
        lignes: list[str] = []
        if self.personnage:
            lignes.append(f"personnage: {self.personnage}")
        if self.profil.get("registre"):
            lignes.append(f"registre: {self.profil['registre']}")
        if self.glossaire:
            lignes.append("glossaire: " + "; ".join(
                f"{k}={v}" for k, v in self.glossaire.items()))
        if self.memoire:
            lignes.append("memoire: " + "; ".join(
                f"{k}={v}" for k, v in self.memoire.items()))
        if self.idiomes:
            lignes.append("idiomes: " + "; ".join(
                f"{k}={v}" for k, v in self.idiomes.items()))
        if self.precedent:
            lignes.append(f"precedent: {self.precedent}")
        if self.suivant:
            lignes.append(f"suivant: {self.suivant}")
        return "\n".join(lignes)
