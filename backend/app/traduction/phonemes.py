"""Phonétisation locale mot à mot (spec §5, §14) + phonèmes visibles pour la sync labiale.

Produit la donnée phonétique PAR MOT (``PhonemeMot`` / ``DonneesPhonetiques``)
consommée par ``LipSyncAnalyzer``. La chaîne G2P est locale et par langue, avec
repli gracieux : moteurs externes optionnels (espeak-ng → epitran, slice 8),
puis heuristique grapheme pure — chaque lettre normalisée devient un phonème.
Aucune dépendance lourde n'est requise pour tourner, et l'absence de donnée
phonétique n'est jamais une erreur.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

# Phonèmes « visibles » (lisibles sur les lèvres) — approximation grapheme.
# Les bilabiales /p/ /b/ /m/ ferment la bouche : c'est le signal le plus fort
# pour la synchronisation labiale (spec §14).
BILABIALES = frozenset(("p", "b", "m"))
LABIODENTALES = frozenset(("f", "v"))
VOYELLES_ARRONDIES = frozenset(("o", "u", "y"))
VOYELLES_OUVERTES = frozenset(("a", "e", "i"))

# Règles « visuellement importantes » (REFACTOR slice 4) : digraphes et géminées
# labiales lus sur les lèvres. Chaque entrée replie un multi-graphe vers UN seul
# phonème : « ph » = /f/ (labiodentale — jamais un /p/ bilabial), et une géminée
# (« pp », « bb », « mm »…) n'est qu'une seule articulation. C'est un jeu de
# données par langue (surchargeable), pas des ``if`` dispersés.
_MULTIGRAPHES_COMMUNS = {
    "ph": "f", "pp": "p", "bb": "b", "mm": "m", "ff": "f", "vv": "v",
}
_MULTIGRAPHES_PAR_LANGUE = {
    langue: dict(_MULTIGRAPHES_COMMUNS)
    for langue in ("fr", "en", "es", "de", "it")
}


def _grapheme_visuel(mot_normalise: str, langue: str | None) -> tuple[str, ...]:
    """Repli grapheme : multi-graphes labiaux repliés puis lettre par lettre."""
    regles = _MULTIGRAPHES_PAR_LANGUE.get((langue or "").lower(),
                                          _MULTIGRAPHES_COMMUNS)
    texte = mot_normalise
    for grapheme, phoneme in sorted(regles.items(), key=lambda kv: -len(kv[0])):
        texte = texte.replace(grapheme, phoneme)
    return tuple(c for c in texte if c.isalpha())

_MOT = re.compile(r"[^\W\d_]+", re.UNICODE)
_MARQUEUR = re.compile(r"\([^()]*\)", re.UNICODE)


@dataclass(frozen=True)
class PhonemeMot:
    """Phonèmes d'un seul mot : la donnée phonétique PAR MOT."""

    mot: str
    phonemes: tuple[str, ...]


@dataclass(frozen=True)
class DonneesPhonetiques:
    """Données phonétiques d'un texte, mot par mot."""

    mots: tuple[PhonemeMot, ...] = ()
    langue: str = ""

    def aplatir(self) -> list[str]:
        """Séquence aplatie des phonèmes (pour le score et la persistance)."""
        return [p for m in self.mots for p in m.phonemes]

    def disponibles(self) -> bool:
        """Vrai si au moins un mot porte des phonèmes (donnée exploitable)."""
        return any(m.phonemes for m in self.mots)

    def bilabiales(self) -> list[int]:
        """Indices (dans la séquence aplatie) des phonèmes bilabiaux /p/ /b/ /m/."""
        return [i for i, p in enumerate(self.aplatir()) if p in BILABIALES]


def _normaliser(mot: str) -> str:
    """Minuscules et accents retirés : le grapheme de repli est insensible à la casse."""
    decompose = unicodedata.normalize("NFD", mot.lower())
    return "".join(c for c in decompose if unicodedata.category(c) != "Mn")


def _mots(texte: str) -> list[str]:
    """Mots alphabétiques d'un texte ; les marqueurs ``(…)`` sont ignorés."""
    sans_marqueurs = _MARQUEUR.sub(" ", texte or "")
    return _MOT.findall(sans_marqueurs)


def similarite_phonemes(source: list[str], cible: list[str]) -> float:
    """Recouvrement multiset (Jaccard) des phonèmes source/cible, 0–100.

    100 si les deux séquences sont vides (rien à comparer, pas d'erreur).
    """
    src, tgt = Counter(source), Counter(cible)
    if not src and not tgt:
        return 100.0
    inter = sum((src & tgt).values())
    union = sum((src | tgt).values())
    if not union:
        return 100.0
    return 100.0 * inter / union


class G2PEngine:
    """Grapheme→phonèmes local, par langue, avec repli grapheme (spec §5).

    ``phonemiser_mot`` essaie d'abord les moteurs externes optionnels (chargés
    à la volée, jamais obligatoires), puis retombe sur l'heuristique grapheme :
    chaque lettre normalisée devient un phonème. Déterministe et hors ligne.
    """

    def phonemiser_mot(self, mot: str, langue: str | None = None) -> tuple[str, ...]:
        normal = _normaliser(mot)
        if not normal:
            return ()
        externe = self._phonemiser_externe(normal, langue)
        if externe:
            return tuple(externe)
        return _grapheme_visuel(normal, langue)

    def _phonemiser_externe(self, mot: str, langue: str | None) -> list[str] | None:
        """Essaie espeak-ng puis epitran (optionnels, lazy) ; ``None`` si indisponibles."""
        for chargeur in (self._essayer_espeak, self._essayer_epitran):
            try:
                resultat = chargeur(mot, langue)
                if resultat:
                    return resultat
            except Exception:  # noqa: BLE001 — moteur absent → repli grapheme
                continue
        return None

    @staticmethod
    def _essayer_espeak(mot: str, langue: str | None) -> list[str] | None:
        try:
            from phonemizer import phonemize  # lazy : dépendance optionnelle

            phonemes = phonemize(mot, language=langue or "fr", backend="espeak",
                                 strip=True, with_stress=False)
            return list(phonemes.replace(" ", ""))
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _essayer_epitran(mot: str, langue: str | None) -> list[str] | None:
        try:
            import epitran  # lazy : dépendance optionnelle

            epi = epitran.Epitran(langue or "fr-Latn")
            return list(epi.transliterate(mot))
        except Exception:  # noqa: BLE001
            return None

    def phonemes_par_mot(self, texte: str, langue: str | None = None) -> list[PhonemeMot]:
        """Donnée phonétique PAR MOT d'un texte (marqueurs ``(…)`` exclus)."""
        return [PhonemeMot(m, self.phonemiser_mot(m, langue)) for m in _mots(texte)]


class PhonemeAnalyzer:
    """Analyse phonétique d'un texte : données par mot + phonèmes visibles."""

    def __init__(self, langue: str | None = None, g2p: G2PEngine | None = None):
        self.langue = langue
        self.g2p = g2p or G2PEngine()

    def analyser(self, texte: str) -> DonneesPhonetiques:
        return DonneesPhonetiques(
            mots=tuple(self.g2p.phonemes_par_mot(texte, self.langue)),
            langue=self.langue or "",
        )

    def bilabiales(self, texte: str) -> list[int]:
        """Indices des phonèmes bilabiaux de ``texte`` (séquence aplatie)."""
        return self.analyser(texte).bilabiales()

    def comparer(self, source: DonneesPhonetiques,
                 cible: DonneesPhonetiques) -> float:
        """Similarité des phonèmes source/cible (0–100)."""
        return similarite_phonemes(source.aplatir(), cible.aplatir())


def phonemiser_mot(mot: str, langue: str | None = None) -> tuple[str, ...]:
    """Phonèmes d'un mot (repli grapheme) via un moteur G2P par défaut."""
    return G2PEngine().phonemiser_mot(mot, langue)
