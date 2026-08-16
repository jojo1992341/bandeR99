"""Analyse de synchronisation labiale : phonèmes visibles + ouverture de bouche (spec §14).

``LipSyncAnalyzer`` est la matière première de la sync labiale : il consomme la
donnée phonétique PAR MOT (``phonemes.py``) et, quand elle existe, la courbe
d'ouverture de bouche (``lips.MouthTrack`` / ``find_speech_onsets``). Repli
gracieux : sans donnée phonétique ni piste vidéo, le score est ``None``
(neutre) — jamais d'erreur. Avec les phonèmes seuls, les bilabiales /p/ /b/ /m/
sont alignées source/cible ; avec la piste, les événements sont calés sur les
fermetures/onsets réels de la bouche.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..lips import find_speech_onsets
from .phonemes import BILABIALES, PhonemeAnalyzer, DonneesPhonetiques

if TYPE_CHECKING:  # import léger : mediapipe reste lazy dans lips.py
    from ..lips import MouthTrack

TOLERANCE_POSITION = 0.35  # écart relatif max (0..1) pour apparier deux bilabiales


@dataclass(frozen=True)
class EvenementBouche:
    """Un événement labial daté : fermeture bilabiale, ouverture, etc."""

    instant: float
    type: str  # 'bilabial' | 'ouverture' | 'fermeture'


@dataclass(frozen=True)
class ResultatLipSync:
    """Résultat d'analyse labiale. ``score`` = ``None`` → donnée absente (neutre)."""

    score: float | None
    source_bilabiales: int = 0
    cible_bilabiales: int = 0
    evenements: tuple[EvenementBouche, ...] = ()
    piste_disponible: bool = False
    explication: str = ""


class LipSyncAnalyzer:
    """Événements bouche + alignement des phonèmes visibles source/cible.

    Consomme ``MouthTrack``/onsets (``lips.py``) quand disponibles, sinon
    raisonne sur les seuls phonèmes par mot (repli gracieux).
    """

    def __init__(self, analyseur: PhonemeAnalyzer | None = None,
                 piste: "MouthTrack | None" = None,
                 onsets: list[float] | None = None,
                 tolerance: float = TOLERANCE_POSITION):
        self.analyseur = analyseur or PhonemeAnalyzer()
        self.piste = piste
        if onsets is None and piste is not None:
            onsets = find_speech_onsets(piste)
        self.onsets = list(onsets or [])
        self.tolerance = tolerance

    def evenements_mots(self, mots, donnees: DonneesPhonetiques) -> list[EvenementBouche]:
        """Événements ``bilabial`` posés au début de chaque mot qui en porte un.

        ``mots`` (``Word`` horodatés) et ``donnees.mots`` doivent être alignés
        (même ordre) ; les marqueurs sont déjà exclus de ``donnees``.
        """
        evenements: list[EvenementBouche] = []
        for mot, pm in zip(mots, donnees.mots):
            if not pm.phonemes:
                continue
            if any(p in BILABIALES for p in pm.phonemes):
                evenements.append(EvenementBouche(float(mot.start), "bilabial"))
        return evenements

    def evenements_piste(self) -> list[EvenementBouche]:
        """Événements d'ouverture déduits des onsets de la ``MouthTrack``."""
        return [EvenementBouche(float(t), "ouverture") for t in self.onsets]

    def aligner_visibles(self, source: DonneesPhonetiques,
                         cible: DonneesPhonetiques) -> float:
        """Score d'alignement des bilabiales source/cible (0–100).

        Chaque bilabiale source cherche une bilabiale cible à la même position
        relative (tolérance ``self.tolerance``) ; 100 si aucune des deux n'en
        porte, 0 si l'une en porte et pas l'autre.
        """
        src = source.bilabiales()
        tgt = cible.bilabiales()
        if not src and not tgt:
            return 100.0
        if not src or not tgt:
            return 0.0
        total_src = max(len(source.aplatir()), 1)
        total_cible = max(len(cible.aplatir()), 1)
        positions_src = [i / total_src for i in src]
        positions_cible = [i / total_cible for i in tgt]
        apparies = sum(
            1 for p in positions_src
            if any(abs(p - q) <= self.tolerance for q in positions_cible)
        )
        return 100.0 * apparies / max(len(src), len(tgt))

    def analyser(self, source_texte: str, cible_texte: str,
                 mots_source=None) -> ResultatLipSync:
        """Analyse complète d'une paire source/cible, avec repli gracieux.

        ``mots_source`` (``Word`` horodatés, optionnels) permet de dater les
        événements bilabiaux dans la fenêtre de la réplique. Sans donnée
        phonétique ET sans piste, le score est ``None`` (neutre).
        """
        source = self.analyseur.analyser(source_texte)
        cible = self.analyseur.analyser(cible_texte)
        piste_dispo = self.piste is not None and bool(self.onsets)
        if not source.disponibles() and not cible.disponibles():
            return ResultatLipSync(None, piste_disponible=piste_dispo,
                                   explication="aucune donnée phonétique")
        score = self.aligner_visibles(source, cible)
        evenements: list[EvenementBouche] = []
        if mots_source:
            parles = [m for m in mots_source if not getattr(m, "marqueur", False)]
            if len(parles) == len(source.mots):
                evenements = self.evenements_mots(parles, source)
        if piste_dispo:
            evenements = self.evenements_piste() + evenements
        src_b, cible_b = len(source.bilabiales()), len(cible.bilabiales())
        explication = (f"{src_b} bilabiale(s) source / {cible_b} cible, "
                       f"alignement {score:.0f} %")
        if piste_dispo:
            explication += " (piste labiale disponible)"
        return ResultatLipSync(score, source_bilabiales=src_b,
                               cible_bilabiales=cible_b,
                               evenements=tuple(evenements),
                               piste_disponible=piste_dispo,
                               explication=explication)
