"""Découpage des mots transcrits en répliques (cues) et cinématique du défilement."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np

from .asr import Word
from .symboles import est_symbole

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
    """Une réplique à afficher sur la bande rythmo.

    ``start_override``/``end_override`` sont la fenêtre d'affichage de la
    réplique, distincte des bornes audio de ses mots. Cette distinction est
    importante en rythmo : une bande professionnelle laisse souvent une petite
    marge avant l'attaque et après la dernière syllabe sans falsifier le
    calage mot-à-mot.
    """

    words: list[Word] = field(default_factory=list)
    personnage: int | None = None  # voix (diarisation) ; None = non analysé
    start_override: float | None = None
    end_override: float | None = None

    @property
    def start(self) -> float:
        return (self.start_override if self.start_override is not None
                else self.words[0].start)

    @property
    def end(self) -> float:
        return (self.end_override if self.end_override is not None
                else self.words[-1].end)

    @property
    def text(self) -> str:
        return joindre_mots_fr([w.text for w in self.words])


def build_cues(mots: list[Word], pause_seuil: float = 0.6,
               max_caracteres: int = 60, max_duree: float = 6.0,
               speaker_labels: list[int | None] | None = None,
               split_on_punctuation: bool = False,
               marge_avant: float = 0.0,
               marge_apres: float = 0.0) -> list[Cue]:
    """Découpe les mots en répliques de qualité studio.

    Les règles historiques (pause, longueur et durée) restent les garde-fous.
    En mode qualité, ``speaker_labels`` permet surtout de ne pas fusionner deux
    tours de parole séparés par une pause courte ; ``split_on_punctuation``
    ajoute une coupure sur ``.``, ``?``, ``!`` ou ``…``. Les marges optionnelles
    ne modifient que la fenêtre de la réplique, jamais les timings des mots.
    """
    if speaker_labels is not None and len(speaker_labels) != len(mots):
        raise ValueError("speaker_labels doit contenir une étiquette par mot")

    cues: list[Cue] = []
    courant: list[Word] = []
    etiquette_courante: int | None = None

    def longueur_si_ajoute(mot: Word) -> int:
        return len(joindre_mots_fr([w.text for w in courant] + [mot.text.strip()]))

    for index, mot in enumerate(mots):
        etiquette = speaker_labels[index] if speaker_labels is not None else None
        if courant:
            pause = mot.start - courant[-1].end
            dernier_texte = courant[-1].text.rstrip()
            changement_voix = (
                speaker_labels is not None
                and etiquette is not None
                and etiquette_courante is not None
                and etiquette != etiquette_courante
            )
            ponctuation_forte = (
                split_on_punctuation
                and dernier_texte.endswith((".", "?", "!", "…"))
            )
            couper = (
                pause > pause_seuil
                or longueur_si_ajoute(mot) > max_caracteres
                or (mot.end - courant[0].start) > max_duree
                or changement_voix
                or ponctuation_forte
            )
            if couper:
                cues.append(Cue(words=courant, personnage=etiquette_courante))
                courant = []
                etiquette_courante = None
        if not courant:
            etiquette_courante = etiquette
        texte_mot = mot.text.strip()
        courant.append(Word(texte_mot, mot.start, mot.end, mot.probability,
                             marqueur=mot.marqueur or est_symbole(texte_mot),
                             incertain=mot.incertain))
    if courant:
        cues.append(Cue(words=courant, personnage=etiquette_courante))

    if cues and (marge_avant > 0.0 or marge_apres > 0.0):
        # Applique les marges après construction : les voisins sont connus.
        # Une marge visuelle ne doit jamais créer un chevauchement artificiel
        # entre deux cues : la phase 2 (valider_repliques) rejette ces
        # chevauchements. On borne donc chaque fin au DÉBUT EFFECTIF de la
        # réplique suivante (qui inclut déjà sa propre marge avant), pas à son
        # premier mot brut — sinon le décalage « fin = début_du_mot_suivant »
        # vs « début suivant = mot − marge_avant » recréerait un recouvrement
        # de la taille de la marge.
        for i, cue in enumerate(cues):
            debut = max(0.0, cue.words[0].start - float(marge_avant))
            if i:
                debut = max(debut, cues[i - 1].words[-1].end)
            fin = cue.words[-1].end
            if i + 1 < len(cues):
                prochain_debut = max(0.0, cues[i + 1].words[0].start - float(marge_avant))
                prochain_debut = max(prochain_debut, cue.words[-1].end)
                fin = min(fin + float(marge_apres), prochain_debut)
            cue.start_override = debut
            cue.end_override = max(debut, fin)
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

# T-REF (référence clideo) : un mot/syllabe soutenu est rendu À LA POLICE DE
# PISTE (hauteur d'encre CONSTANTE) et ses glyphes sont ALLONGÉS horizontalement
# par ``scale_x = empreinte / largeur naturelle``. Plafond de lisibilité du
# facteur (même rôle que le plafond 0,85·H de T54, exprimé en largeur).
# CONSTANTE PARTAGÉE avec le rendu (render.py la réexporte) : le mode flux
# (facteur_etirement) et le mode dynamique (largeurs_dynamiques) appliquent la
# MÊME borne — même mot, même étirement, quel que soit le mode.
#
# Décision 16/08/2026 (retour « Francis! » pas assez étiré) : plafond relevé de
# 3× à 6× — l'empreinte acoustique complète v·durée est atteinte pour les mots
# très tenus (facteur ≤ 6), aligné sur le plafond de garde 6·v du mode dynamique.
FACTEUR_ETIREMENT_MAX = 6.0


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


# ---------------------------------------------------------------------------
# Vitesse dynamique (option T149) : vitesse CONSTANTE par réplique, ancres
# 1ᵉʳ/dernier mot — aucun chevauchement, aucun freeze, loi continue.
# (plan ``plans/vitesse-dynamique-par-replique.md``, §3.1)
# ---------------------------------------------------------------------------

VITESSE_DYNAMIQUE_MAX_RATIO = 6.0  # plafond de garde (précédent T148) : jamais de bond


@dataclass
class SegmentDynamique:
    """Une réplique d'une voix, prête pour le plan dynamique.

    ``positions[j]`` = position de piste absolue du début du mot j ;
    ``largeurs[j]`` = largeur dessinée (étirement T-REF compris, borné par
    l'espacement disponible — jamais de chevauchement) ; ``echelles[j]`` =
    facteur d'étirement horizontal (``None`` = mot non étiré).
    """

    debut: float          # t_1 : attaque du 1er mot
    fin: float            # e_n : fin du dernier mot
    vitesse: float        # v_i (px/s) : vitesse constante de la réplique
    positions: list[float]    # débuts de piste absolus des mots
    largeurs: list[float]     # largeurs dessinées (étirement compris)
    echelles: list[float | None]


def _largeurs_dynamiques(mots: list[Word], largeurs_nat: list[float],
                         v_px_s: float, espace_px: float,
                         etirer: bool) -> list[float]:
    """Largeurs dessinées au débit ``v_px_s`` : étirement T-REF d'un mot tenu
    (empreinte v·durée) borné par la lisibilité (3× la largeur naturelle) et,
    sauf pour le dernier mot, par l'espacement disponible avant le mot suivant
    — jamais de chevauchement, quelle que soit la vitesse locale."""
    if not etirer or not mots:
        return list(largeurs_nat)
    n = len(mots)
    eff: list[float] = []
    for j, mot in enumerate(mots):
        nat = float(largeurs_nat[j])
        duree = max(0.0, float(mot.end - mot.start))
        empreinte = v_px_s * duree
        plafond = FACTEUR_ETIREMENT_MAX * nat  # lisibilité (constante partagée T-REF)
        if j + 1 < n:
            # le mot étiré ne peut pas déborder sur l'espace du mot suivant
            # (dont le début est à v·Δt sur la piste) : jamais de chevauchement
            plafond = min(plafond, v_px_s * float(mots[j + 1].start - mot.start)
                          - espace_px)
        eff.append(max(nat, min(empreinte, plafond)))
    return eff


def vitesse_replique_dynamique(mots: list[Word], largeurs_nat: list[float],
                               espace_px: float, v_base_px_s: float,
                               v_min_px_s: float, v_max_px_s: float,
                               etirer: bool = True) -> float:
    """``v_i`` : plus petit débit constant de la réplique qui (a) ne fait
    jamais chevaucher un mot sur le suivant (bornes de densité naturelles) et
    (b) laisse le dernier mot remplir sa fenêtre acoustique (ancre de fin).

    Avec étirement actif, l'ancre de fin ne descend jamais sous la vitesse de
    base : le dernier mot tenu est étiré à au moins son empreinte référence
    ``v_base·durée`` (geste T-REF) — sinon ``v_i = largeur/durée`` s'effondrerait
    et l'étirement disparaîtrait sur les mots tenus isolés (ex. « Francis! »
    tenu 1,3 s, retour utilisateur 16/08/2026).
    """
    n = len(mots)
    if n == 0:
        return v_base_px_s
    v = v_min_px_s
    for j in range(1, n):
        dt = float(mots[j].start - mots[j - 1].start)
        if dt > 1e-6:
            v = max(v, (float(largeurs_nat[j - 1]) + float(espace_px)) / dt)
    duree_dernier = max(1e-3, float(mots[-1].end - mots[-1].start))
    v_ancre = float(largeurs_nat[-1]) / duree_dernier
    if etirer:
        # le dernier mot est étiré à v_i·durée ; ne pas descendre sous v_base
        # garde l'étirement ≥ empreinte référence (lisibilité du geste tenu)
        v_ancre = max(v_ancre, v_base_px_s)
    v = max(v, v_ancre)
    return max(v_min_px_s, min(v, v_max_px_s))


def construire_plan_dynamique(curseur_x_px: float, v_base_px_s: float,
                              sequences, espace_px: float, duree_s: float,
                              etirer: bool = True,
                              v_min_px_s: float | None = None,
                              v_max_px_s: float | None = None
                              ) -> tuple[ScrollPlan, list[SegmentDynamique]]:
    """Loi « vitesse constante par réplique » pour UNE voix (plan T149 §3.1).

    ``sequences`` : liste de ``(mots, largeurs_naturelles)``, une par réplique,
    dans l'ordre temporel de la voix. Retourne le plan horaire et, dans le même
    ordre, les segments (positions de piste, largeurs dessinées, vitesses).

    La loi est continue et linéaire par morceaux : pente ``−v_i`` sur le corps
    de chaque réplique ``[t_1, e_n]``, pente ``−v_base`` ailleurs (silences,
    approches, évacuations) — aucune pente nulle sur ``[0, durée]`` (jamais de
    gel), gel hors vidéo (convention T47).
    """
    if v_min_px_s is None:
        v_min_px_s = v_base_px_s * (VITESSE_RATIO_MIN / VITESSE_RATIO_DEFAUT)
    if v_max_px_s is None:
        v_max_px_s = VITESSE_DYNAMIQUE_MAX_RATIO * v_base_px_s
    duree_s = max(0.0, float(duree_s))
    if not sequences:
        return build_plan_flux(curseur_x_px, v_base_px_s, duree_s), []

    segments: list[SegmentDynamique] = []
    S = 0.0  # position de piste du 1er mot de la réplique en cours
    for i, (mots, largeurs_nat) in enumerate(sequences):
        if not mots:
            continue
        v_i = vitesse_replique_dynamique(mots, largeurs_nat, espace_px,
                                         v_base_px_s, v_min_px_s, v_max_px_s,
                                         etirer=etirer)
        largeurs = _largeurs_dynamiques(mots, largeurs_nat, v_i, espace_px,
                                        etirer)
        t1 = float(mots[0].start)
        en = max(float(mots[-1].end), t1)
        positions = [S + v_i * (float(m.start) - t1) for m in mots]
        echelles = [(l / n) if l > n + 1e-9 else None
                    for l, n in zip(largeurs, largeurs_nat)]
        segments.append(SegmentDynamique(debut=t1, fin=en, vitesse=v_i,
                                         positions=positions,
                                         largeurs=largeurs, echelles=echelles))
        # position du 1er mot de la réplique suivante : au moins « ancre de fin
        # + glissement v_base pendant la pause » (continuité des ancres) et au
        # moins « bord droit réel + espace » (jamais de chevauchement, même
        # dernier mot très large)
        prochain = sequences[i + 1][0] if i + 1 < len(sequences) else None
        prochain_t1 = (float(prochain[0].start) if prochain else en)
        S = max(S + v_i * (en - t1) + v_base_px_s * (prochain_t1 - en),
                positions[-1] + largeurs[-1] + espace_px)

    temps: list[float] = [0.0]
    offsets: list[float] = [float(curseur_x_px)
                            + v_base_px_s * segments[0].debut]
    for seg in segments:
        s1 = seg.positions[0]
        duree = seg.fin - seg.debut
        temps.append(seg.debut)
        offsets.append(float(curseur_x_px) - s1)
        temps.append(seg.fin)
        offsets.append(float(curseur_x_px) - s1 - seg.vitesse * duree)
    derniere_fin = segments[-1].fin
    duree_fin = max(duree_s, derniere_fin)
    temps.append(duree_fin)
    offsets.append(offsets[-1] - v_base_px_s * (duree_fin - derniere_fin))

    # nœuds strictement croissants (pause nulle → doublet au même instant)
    temps_u: list[float] = []
    offsets_u: list[float] = []
    for t, o in zip(temps, offsets):
        if temps_u and abs(t - temps_u[-1]) < 1e-9:
            offsets_u[-1] = o  # même instant : la dernière ancre fait foi
            continue
        temps_u.append(t)
        offsets_u.append(o)
    return ScrollPlan(temps=np.asarray(temps_u, dtype=np.float64),
                      offsets=np.asarray(offsets_u, dtype=np.float64)), segments


def temps_pour_offset(plan: ScrollPlan, valeur: float) -> float:
    """Inverse ``offset_en`` : plus petit ``t ≥ 0`` où ``offset(t) == valeur``
    (la loi est strictement décroissante ; borné aux extrémités)."""
    offs = plan.offsets
    if offs[0] <= valeur:
        return 0.0
    if offs[-1] >= valeur:
        return float(plan.temps[-1])
    return float(np.interp(valeur, offs[::-1], plan.temps[::-1]))
