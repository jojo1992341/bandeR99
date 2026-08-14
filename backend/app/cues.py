"""Découpage des mots transcrits en répliques (cues) et cinématique du défilement."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np

from .asr import Word

_PONCTUATION_COLLANTE = {".", ",", ":", ";", "!", "?", "…", ")", "»", "%", "'", "'"}


def joindre_mots_fr(textes: list[str]) -> str:
    """Joint des fragments style Whisper : espaces propres, ponctuation collée."""
    morceaux = [t.strip() for t in textes if t.strip()]
    if not morceaux:
        return ""
    sortie = morceaux[0]
    for m in morceaux[1:]:
        if m in _PONCTUATION_COLLANTE or m[0] in ".,:;!?…)]»%'\u2019":
            sortie += m
        else:
            sortie += " " + m
    return re.sub(r"\s+", " ", sortie).strip()


@dataclass
class Cue:
    """Une réplique à afficher sur la bande rythmo."""

    words: list[Word] = field(default_factory=list)
    personnage: int | None = None  # voix (diarisation) ; None = non analysé

    @property
    def start(self) -> float:
        return self.words[0].start

    @property
    def end(self) -> float:
        return self.words[-1].end

    @property
    def text(self) -> str:
        return joindre_mots_fr([w.text for w in self.words])


def build_cues(mots: list[Word], pause_seuil: float = 0.6,
               max_caracteres: int = 60, max_duree: float = 6.0) -> list[Cue]:
    """Découpe les mots en cues : pause > seuil, longueur max caractères, durée max."""
    cues: list[Cue] = []
    courant: list[Word] = []

    def longueur_si_ajoute(mot: Word) -> int:
        return len(joindre_mots_fr([w.text for w in courant] + [mot.text.strip()]))

    for mot in mots:
        if courant:
            pause = mot.start - courant[-1].end
            couper = (
                pause > pause_seuil
                or longueur_si_ajoute(mot) > max_caracteres
                or (mot.end - courant[0].start) > max_duree
            )
            if couper:
                cues.append(Cue(words=courant))
                courant = []
        courant.append(Word(mot.text.strip(), mot.start, mot.end, mot.probability,
                             marqueur=mot.marqueur))
    if courant:
        cues.append(Cue(words=courant))
    return cues


def reflow_lignes(texte: str, police, largeur_max_px: float, max_lignes: int = 2) -> list[str]:
    """Repli glouton mesuré en pixels (PIL) : ≤ ``max_lignes``, aucun mot coupé.

    Si le texte déborde encore après ``max_lignes`` lignes, la dernière ligne absorbe
    le reste (le recalage en amont — durée/caractères — empêche ce cas).
    """
    from PIL import Image, ImageDraw

    dessin = ImageDraw.Draw(Image.new("RGB", (4, 4)))

    def largeur(t: str) -> float:
        return dessin.textlength(t, font=police)

    mots = texte.split()
    lignes: list[str] = []
    courante = ""
    for mot in mots:
        essai = mot if not courante else courante + " " + mot
        if largeur(essai) <= largeur_max_px or not courante:
            courante = essai
        else:
            lignes.append(courante)
            courante = mot
    if courante:
        lignes.append(courante)
    if len(lignes) > max_lignes:  # repli en dernier recours : tout fondre sur la dernière
        tete, queue = lignes[: max_lignes - 1], lignes[max_lignes - 1:]
        lignes = tete + [" ".join(queue)]
    return lignes or [""]


# ---------------------------------------------------------------------------
# Cinématique du défilement (bande rythmo style karaoké)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScrollPlan:
    """Loi horaire ``offset_en(t)`` : décalage X (px) du début de piste à l'instant t.

    **Flux continu à vitesse constante (rythmo pro, T47)** : un plan unique pour
    toute la vidéo, ``offset_en(t) = curseur − v·t`` ; la position écran d'un mot
    est ``offset_en(t) + s_mot`` où ``s_mot`` est sa position de piste (voir
    ``layout_flux``). La bande est une piste temporelle rigide : chaque mot passe
    sous le curseur exactement à son instant, les silences défilent « à vide » —
    régularité parfaite mesurée sur la référence pro (153 px/s à 480 px de large).
    """

    temps: np.ndarray  # points de contrôle croissants
    offsets: np.ndarray  # décalages X correspondants

    def offset_en(self, t: float) -> float:
        return float(np.interp(t, self.temps, self.offsets))


VITESSE_RATIO_DEFAUT = 0.32  # fraction de la largeur par seconde (mesuré : réf. pro)
VITESSE_RATIO_MIN = 0.05
VITESSE_RATIO_MAX = 1.5


def layout_flux(mots: list[Word], largeurs_px: list[float], espace_px: float,
                vitesse_px_s: float, s_min: float = 0.0) -> list[float]:
    """Positions de piste (px) des mots : ancrage temporel + anti-chevauchement.

    ``s_i = max(v·start_i, s_{i-1} + largeur_{i-1} + espace, s_min)`` :
    chaque mot est idéalement placé pour passer sous le curseur à son ``start`` ;
    seules les rafales de parole plus rapides que la piste dilatent localement
    l'espacement (jamais de superposition). ``s_min`` chaîne la continuité avec
    la fin de piste de la réplique précédente (monotonie globale sur la vidéo).
    """
    if len(mots) != len(largeurs_px):
        raise ValueError("mots et largeurs_px doivent avoir la même longueur")
    positions: list[float] = []
    suivant_min = float(s_min)
    for mot, largeur in zip(mots, largeurs_px):
        s = max(vitesse_px_s * float(mot.start), suivant_min)
        positions.append(s)
        suivant_min = s + float(largeur) + float(espace_px)
    return positions


def largeurs_piste_etirees(mots: list[Word], largeurs_naturelles: list[float],
                           vitesse_px_s: float) -> list[float]:
    """Largeurs de piste (px) : jamais moins que la largeur naturelle, sinon
    proportionnelles à la durée acoustique (v·durée) du mot.

    Geste du rythmographe pour les syllabes allongées : un mot traîné (ex.
    « Franciiis » — la syllabe « cis » tenue sur plus d'une seconde) doit
    occuper sur la piste l'espace ``v·(fin−début)`` pour que son dernier caractère passe sous le curseur
    exactement à sa fin de parole — la bande montre l'étirement au lieu de
    défiler à vide pendant la syllabe tenue. Un mot prononcé plus vite que la
    piste garde sa largeur naturelle (jamais de compression : on condense la
    police, T48, on n'accélère pas la bande).
    """
    if len(mots) != len(largeurs_naturelles):
        raise ValueError("mots et largeurs_naturelles doivent avoir la même longueur")
    return [max(float(largeur), vitesse_px_s * max(0.0, float(mot.end - mot.start)))
            for mot, largeur in zip(mots, largeurs_naturelles)]


def build_plan_flux(curseur_x_px: float, vitesse_px_s: float,
                    duree_s: float) -> ScrollPlan:
    """Plan global : ``offset_en(t) = curseur_x − vitesse·t``, gel hors [0, durée]."""
    duree_s = max(0.0, float(duree_s))
    return ScrollPlan(
        temps=np.asarray([0.0, duree_s], dtype=np.float64),
        offsets=np.asarray([curseur_x_px, curseur_x_px - vitesse_px_s * duree_s],
                           dtype=np.float64),
    )
