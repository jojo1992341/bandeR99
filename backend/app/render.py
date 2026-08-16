"""Rendu de la bande rythmo (Pillow) : 2 styles × thèmes.

- RYTHMO   : texte défilant droite→gauche sous un curseur fixe.
             Thème STUDIO (défaut) : bande claire, texte noir, passé gris, ligne de
             base continue, mot actif surligné rose (boîte + flèche) — référence pro.
             Thème SOMBRE : bande noire, futur blanc, actif jaune (look karaoké initial).
             Typo T51/T52 : lettres serrées (espacement_lettres) et CONDENSÉES
             (condensation 0,78 ≈ Arial Narrow) — à densité de piste égale, la loi
             de densité choisit un corps plus grand (lisible, référence pro).
- RÉPLIQUE : réplique courante centrée, changée au calage labial (style sous-titre
             studio) — rendu sombre quel que soit le thème, jamais condensé.
"""
from __future__ import annotations

from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

from .cues import (FACTEUR_ETIREMENT_MAX, VITESSE_DYNAMIQUE_MAX_RATIO,
                   VITESSE_RATIO_DEFAUT, VITESSE_RATIO_MAX, VITESSE_RATIO_MIN,
                   Cue, build_plan_flux, construire_plan_dynamique,
                   joindre_mots_fr, largeurs_piste_etirees, layout_flux,
                   reflow_lignes, temps_pour_offset)

_POLICES_CANDIDATES = [
    "arialbd.ttf",
    "arial.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]

# Palettes par (thème, style) : la référence studio pro (bande claire) pour le
# défilant ; le rendu sous-titre reste sombre pour RÉPLIQUE.
_PALETTES = {
    "STUDIO": {
        "RYTHMO": {"fond": (240, 241, 250), "futur": (22, 22, 26),
                   "passe": (150, 150, 158), "actif": (255, 255, 255),
                   "surlignage": (240, 118, 152), "ligne_base": (70, 70, 80),
                   "marqueur": (0, 108, 108), "marqueur_fond": (204, 235, 235)},
        "REPLIQUE": {"fond": (10, 10, 14), "futur": (240, 240, 240),
                     "passe": (150, 150, 152), "actif": (255, 212, 64),
                     "surlignage": None, "ligne_base": None,
                     "marqueur": None, "marqueur_fond": None},
    },
    "SOMBRE": {
        "RYTHMO": {"fond": (10, 10, 14), "futur": (240, 240, 240),
                   "passe": (150, 150, 152), "actif": (255, 212, 64),
                   "surlignage": None, "ligne_base": None,
                   "marqueur": (120, 222, 222), "marqueur_fond": (24, 62, 66)},
        "REPLIQUE": {"fond": (10, 10, 14), "futur": (240, 240, 240),
                     "passe": (150, 150, 152), "actif": (255, 212, 64),
                     "surlignage": None, "ligne_base": None,
                     "marqueur": None, "marqueur_fond": None},
    },
}

_AUTO = "auto"  # sentinelle : « prendre la valeur de la palette du thème »

# T52 : plancher de lisibilité de la police de piste (avant : 11 px). Une parole
# très dense ne descend plus sous 14 px — l'anti-chevauchement reste le filet.
POLICE_PISTE_MIN = 14

# T-REF (référence clideo) : un mot/syllabe soutenu est rendu À LA POLICE DE
# PISTE (hauteur d'encre CONSTANTE — la référence n'agrandit jamais les lettres)
# et ses glyphes sont ALLONGÉS horizontalement par un facteur
# ``scale_x = empreinte / largeur naturelle`` (chaque lettre s'étire en largeur,
# les lettres restent adjacentes — pas de tracking). Le facteur est borné pour la
# lisibilité (même rôle que le plafond 0,85·H de T54, exprimé en largeur : une
# empreinte énorme ne peut pas être comblée intégralement, le mot finit alors
# avant v·fin). La constante est partagée avec le mode dynamique (cues.py) —
# ``FACTEUR_ETIREMENT_MAX`` y est définie et réexportée ici.
#
# Décision 16/08/2026 (retour « Francis! » pas assez étiré) : plafond relevé de
# 3× à 6× — l'empreinte acoustique complète v·durée est atteinte pour les mots
# très tenus (facteur ≤ 6), aligné sur le plafond de garde 6·v du mode dynamique.
def facteur_etirement(largeur_naturelle: float, empreinte: float,
                      borne_max: float = FACTEUR_ETIREMENT_MAX) -> float | None:
    """scale_x horizontal d'un mot tenu : empreinte / largeur naturelle, borné.

    Renvoie ``None`` quand l'empreinte ne dépasse pas la largeur naturelle (mot
    prononcé plus vite que la piste : aucun étirement, police de piste) et le
    facteur borné par ``borne_max`` sinon (lisibilité — une empreinte énorme ne
    peut pas être comblée intégralement).
    """
    if empreinte <= largeur_naturelle + 1e-6:
        return None
    return min(empreinte / largeur_naturelle, borne_max)


def largeur_trackee(dessin, texte: str, police, espacement_lettres: float,
                    condensation: float = 1.0) -> float:
    """Largeur du texte avec espacement des lettres réglable et condensation.

    ``espacement_lettres`` = fraction de la taille de police ajoutée entre chaque
    paire de lettres (négatif = lettres serrées) ; ``condensation`` = facteur
    horizontal appliqué au rendu (0,78 ≈ Arial Narrow, typo des bandes pro).
    Les mesures de layout (densité T48, empreintes T49) et le rendu
    (:func:`dessiner_mot_condense`) utilisent la MÊME largeur : ce qui est mesuré
    est exactement ce qui est dessiné.
    """
    if not texte:
        return 0.0
    tot = sum(dessin.textlength(c, font=police) for c in texte)
    return (tot + espacement_lettres * police.size * (len(texte) - 1)) * condensation


def dessiner_mot_tracke(dessin, x, y, texte: str, police, remplissage,
                        espacement_lettres: float, ancre: str = "lm") -> float:
    """Dessine ``texte`` caractère par caractère avec l'espacement réglable.

    Le caractère suivant démarre à la fin du précédent + ``espacement_lettres``
    × taille de police (négatif = lettres serrées). Renvoie l'avance totale :
    elle est EXACTEMENT la largeur mesurée par :func:`largeur_trackee` (le rendu
    colle aux mesures, pas de dérive entre calcul et gravure)."""
    x_courant = float(x)
    for i, car in enumerate(texte):
        dessin.text((x_courant, y), car, font=police, fill=remplissage, anchor=ancre)
        x_courant += dessin.textlength(car, font=police)
        if i < len(texte) - 1:
            x_courant += espacement_lettres * police.size
    return x_courant - x


def dessiner_mot_condense(canvas, x, y, texte: str, police, remplissage,
                          espacement_lettres: float,
                          condensation: float = 1.0) -> float:
    """Dessine ``texte`` (espacement réglable) puis l'écrase horizontalement.

    C'est la typo condensée des bandes rythmo pro : des lettres HAUTES et
    ÉTROITES tiennent dans la même empreinte de piste — la loi de densité T48
    peut donc choisir un corps plus grand (lisible). ``condensation=1.0`` =
    dessin direct (via :func:`dessiner_mot_tracke`) ; ``condensation > 1`` =
    étirement HORIZONTAL (mots tenus T-REF : chaque glyphe allongé en largeur à
    hauteur constante — même mécanisme de redimensionnement que la condensation,
    facteur inverse). Renvoie l'avance dessinée (= la mesure
    :func:`largeur_trackee` avec la même condensation, ±1 px d'arrondi). Peut
    recevoir un seul caractère (syllabes tenues T49/T51).
    """
    if not texte:
        return 0.0
    if abs(condensation - 1.0) < 1e-9:
        return dessiner_mot_tracke(ImageDraw.Draw(canvas), x, y, texte, police,
                                   remplissage, espacement_lettres)
    mesure = ImageDraw.Draw(Image.new("RGB", (4, 4)))
    largeur_nat = sum(mesure.textlength(c, font=police) for c in texte)
    if len(texte) > 1:
        largeur_nat += espacement_lettres * police.size * (len(texte) - 1)
    hauteur = _hauteur_texte(police)
    marge = 2
    img = Image.new("RGBA", (max(2, int(largeur_nat)) + 2 * marge, hauteur + 2 * marge),
                    (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x_courant = float(marge)
    y_milieu = (hauteur + 2 * marge) / 2
    for i, car in enumerate(texte):
        d.text((x_courant, y_milieu), car, font=police, fill=remplissage, anchor="lm")
        x_courant += mesure.textlength(car, font=police)
        if i < len(texte) - 1:
            x_courant += espacement_lettres * police.size
    largeur_cond = max(1, int(round(largeur_nat * condensation)))
    img = img.resize((largeur_cond, img.height), Image.BILINEAR)
    # alignement vertical identique au dessin direct anchor "lm" : le milieu du
    # font de l'image (y_milieu) doit retomber sur y du canvas
    canvas.paste(img, (round(x) - marge, round(y) - round(y_milieu)), img)
    return largeur_nat * condensation


def _largeur_trackee_lettres(dessin, texte: str, polices: list,
                             espacement_lettres: float,
                             condensation: float = 1.0) -> float:
    """Comme :func:`largeur_trackee` mais avec UNE police par lettre (syllabe
    tenue à taille croissante) — même formule, taille locale à chaque paire."""
    if not texte:
        return 0.0
    tot = sum(dessin.textlength(c, font=p) for c, p in zip(texte, polices))
    if len(texte) > 1:
        tot += espacement_lettres * (sum(p.size for p in polices) / len(polices)) \
               * (len(texte) - 1)
    return tot * condensation


def _polices_lettres_croissantes(dessin, texte: str, police_base, empreinte: float,
                                 espacement_lettres: float, condensation: float,
                                 taille_max: int):
    """Une police PAR LETTRE, grandissant de ``police_base`` (1res lettres) à une
    taille finale (dernières lettres), pour remplir l'empreinte acoustique.

    Geste traditionnel du rythmographe pour une syllabe tenue (ex. « Franciiis » :
    les dernières lettres grandissent visiblement) plutôt qu'un mot uniformément
    agrandi — plus lisible et plus proche de la référence pro. Recherche par
    dichotomie sur la taille finale (largeur croissante avec la taille).
    ``taille_max`` borne la dernière lettre (plafond de bande / deux voix, T107).
    """
    n = len(texte)
    if n == 0:
        return [], police_base

    def largeur_pour_final(taille_finale):
        pas = (taille_finale - police_base.size) / max(1, n - 1)
        pols = [get_police(max(1, round(police_base.size + i * pas)))
                for i in range(n)]
        return _largeur_trackee_lettres(dessin, texte, pols,
                                        espacement_lettres, condensation), pols

    borne_haute = max(police_base.size, taille_max)
    largeur_max, pols = largeur_pour_final(borne_haute)
    if largeur_max <= empreinte:
        return pols, pols[-1]  # même à la taille plafond, ça ne remplit pas
    lo, hi = float(police_base.size), float(borne_haute)
    for _ in range(14):
        mid = (lo + hi) / 2
        largeur_mid, pols = largeur_pour_final(mid)
        if largeur_mid < empreinte:
            lo = mid
        else:
            hi = mid
    _, pols = largeur_pour_final(hi)
    return pols, pols[-1]


def dessiner_mot_croissant(canvas, x, y_base, texte: str, polices: list,
                           remplissage, espacement_lettres: float,
                           condensation: float = 1.0) -> float:
    """Dessine ``texte`` lettre par lettre à taille croissante (``polices``),
    aligné sur la ligne de BASE commune ``y_base`` (le bas des lettres reste
    fixe, seul le haut grandit — comme la référence pro), condensé comme
    :func:`dessiner_mot_condense`."""
    if not texte:
        return 0.0
    if condensation >= 1.0 - 1e-9:
        dessin = ImageDraw.Draw(canvas)
        x_courant = float(x)
        for i, (car, police) in enumerate(zip(texte, polices)):
            dessin.text((x_courant, y_base), car, font=police,
                       fill=remplissage, anchor="ls")
            x_courant += dessin.textlength(car, font=police)
            if i < len(texte) - 1:
                x_courant += espacement_lettres * police.size
        return x_courant - x
    mesure = ImageDraw.Draw(Image.new("RGB", (4, 4)))
    largeur_nat = sum(mesure.textlength(c, font=p) for c, p in zip(texte, polices))
    if len(texte) > 1:
        largeur_nat += espacement_lettres * (sum(p.size for p in polices)
                                             / len(polices)) * (len(texte) - 1)
    hauteur = max(_hauteur_texte(p) for p in polices)
    marge = 2
    img = Image.new("RGBA", (max(2, int(largeur_nat)) + 2 * marge, hauteur + 2 * marge),
                    (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x_courant = float(marge)
    y_bas_img = hauteur + marge
    for i, (car, police) in enumerate(zip(texte, polices)):
        d.text((x_courant, y_bas_img), car, font=police, fill=remplissage, anchor="ls")
        x_courant += mesure.textlength(car, font=police)
        if i < len(texte) - 1:
            x_courant += espacement_lettres * police.size
    largeur_cond = max(1, int(round(largeur_nat * condensation)))
    img = img.resize((largeur_cond, img.height), Image.BILINEAR)
    canvas.paste(img, (round(x) - marge, round(y_base) - round(y_bas_img)), img)
    return largeur_nat * condensation


@dataclass(frozen=True)
class StyleBande:
    """Paramètres visuels de la bande (couleurs None/auto = palette du thème)."""

    style: str = "RYTHMO"  # ou "REPLIQUE"
    theme: str = "STUDIO"  # ou "SOMBRE"
    hauteur_bande: int = 96
    taille_police: int = 40
    taille_police_min: int | None = None  # plancher de lisibilité (None = max(14, 0,22·H))
    curseur_ratio: float = 0.15  # position X du curseur (fraction de la largeur)
    fond: tuple | None = None
    curseur: tuple = (255, 64, 64)
    actif: tuple | None = None
    futur: tuple | None = None
    passe: tuple | None = None
    surlignage: tuple | None | str = _AUTO   # boîte du mot actif (None = désactivée)
    ligne_base: tuple | None | str = _AUTO   # ligne horizontale guide (None = off)
    marqueur: tuple | None | str = _AUTO     # texte des symboles « (souffle) » (T80)
    marqueur_fond: tuple | None | str = _AUTO  # fond de l'encart du symbole
    vitesse_ratio: float = VITESSE_RATIO_DEFAUT  # défilement RYTHMO (largeur/s)
    vitesse_dynamique: bool = False  # T149 : vitesse constante par réplique (ancres 1ᵉʳ/dernier mot)
    etirer_mots: bool = True      # RYTHMO : étirer l'empreinte des mots allongés
    espacement_lettres: float = -0.05  # T51 : espacement inter-lettres réglable
    condensation: float = 0.78    # T52 : typo condensée (lettres hautes et étroites, pro)
    anticipation_s: float = 1.2   # REPLIQUE : arrive avant la première syllabe
    linger_s: float = 0.35        # REPLIQUE : reste après la dernière
    marge_px: int = 12

    def __post_init__(self):
        palette = _PALETTES.get(self.theme, _PALETTES["STUDIO"])
        couleurs = palette.get(self.style) or palette["REPLIQUE"]
        for champ in ("fond", "actif", "futur", "passe", "surlignage",
                      "ligne_base", "marqueur", "marqueur_fond"):
            if getattr(self, champ) is None or getattr(self, champ) == _AUTO:
                object.__setattr__(self, champ, couleurs[champ])
        object.__setattr__(self, "vitesse_ratio",
                           max(VITESSE_RATIO_MIN,
                               min(VITESSE_RATIO_MAX, float(self.vitesse_ratio))))


def construire_style(params: dict, taille: int) -> StyleBande:
    """Style du rendu depuis les params normalisés (T149).

    ``params["vitesse"]`` : nombre (vitesse constante explicite), le sentinelle
    ``"dynamique"`` (vitesse constante PAR RÉPLIQUE, ancres 1ᵉʳ/dernier mot) ou
    ``None`` (défaut Auto = 0,32 largeur/s, comportement historique strict).
    """
    base = dict(
        style=params["style"], theme=params.get("theme", "STUDIO"),
        hauteur_bande=params["hauteur_bande"], taille_police=taille,
        taille_police_min=params.get("taille_police_min"),
        curseur_ratio=float(params.get("curseur_ratio", 0.15)),
        etirer_mots=bool(params.get("etirer_mots", True)),
    )
    vitesse = params.get("vitesse")
    if vitesse == "dynamique":
        return StyleBande(**base, vitesse_dynamique=True)
    if vitesse:
        return StyleBande(**base, vitesse_ratio=float(vitesse))
    return StyleBande(**base)


def x_curseur(style: "StyleBande", largeur: int) -> int:
    """Colonne (px) du curseur, bornée dans la moitié gauche de la bande."""
    return int(max(style.marge_px + 4,
                   min(round(style.curseur_ratio * largeur), largeur // 2)))


def get_police(taille: int):
    """Police truetype à la taille voulue (repli : police intégrée de Pillow)."""
    for candidat in _POLICES_CANDIDATES:
        try:
            return ImageFont.truetype(candidat, taille)
        except OSError:
            continue
    return ImageFont.load_default(size=taille)


def taille_police_min_effective(hauteur_bande: int | None, taille_min: int | None) -> int:
    """Plancher de lisibilité de la bande : valeur explicite, sinon la loi par
    défaut ``max(14, 0,22 × hauteur de bande)`` (s'adapte à la bande comme la
    police auto 0,28·H, conserve le plancher 14 sur les petites bandes)."""
    if taille_min is not None:
        return max(POLICE_PISTE_MIN, int(taille_min))
    return max(POLICE_PISTE_MIN, round((hauteur_bande or 0) * 0.22))


def taille_police_auto(largeur: int, hauteur_bande: int | None = None,
                       taille_min: int | None = None) -> int:
    """Taille par défaut : max(≈1/25e de la largeur, ≈0,28 × hauteur de bande).

    T52 : la hauteur de bande impose un corps minimal pour que le texte REMPLISSE
    la bande comme la référence pro (densité ~1/25e mesurée sur la réf. à vitesse
    constante — la loi de densité T48 condense ensuite si la parole l'exige).
    L'appel historique sans hauteur garde l'ancienne loi (T47 inchangé).

    ``taille_min`` (plancher de lisibilité) borne le résultat : jamais de texte
    plus petit que le minimum choisi (ou que ``max(14, 0,22 × hauteur)`` par
    défaut) dans la vidéo finale."""
    par_largeur = round(largeur * 0.04)
    par_hauteur = round(hauteur_bande * 0.28) if hauteur_bande else 0
    mini = taille_police_min_effective(hauteur_bande, taille_min)
    return int(max(mini, min(90, max(par_largeur, par_hauteur))))


def _hauteur_texte(police) -> int:
    ascent, descent = police.getmetrics()
    return ascent + descent


def _debit_spatial_max(mots: list, largeurs: list[float], espace_px: float,
                       pause_raccrochage: float = 0.8,
                       fenetre_s: float = 3.0) -> float:
    """Débit spatial maximal (px/s) exigé par la parole, mesuré sur fenêtres
    glissantes de 3 s. Une pause > ``pause_raccrochage`` borne la fenêtre :
    elle recale la piste sur le temps (l'anti-chevauchement ne cumule pas
    au-delà d'une respiration)."""
    meilleur = 0.0
    n = len(mots)
    for i in range(n):
        # distance de piste exigée entre le début du mot i et celui du mot j :
        # Σ (largeur_k + espace) pour k ∈ [i, j) — la largeur du mot j ne joue
        # pas sur sa POSITION de départ (hors-par-un corrigé en T48)
        cum = 0.0
        for j in range(i + 1, n):
            if mots[j].start - mots[i].start > fenetre_s:
                break
            if mots[j].start - mots[j - 1].end > pause_raccrochage:
                break
            cum += largeurs[j - 1] + espace_px
            span = mots[j].start - mots[i].start
            if span > 1e-9:
                meilleur = max(meilleur, cum / span)
    return meilleur


def _police_pour_piste(mots: list, police, taille_style: int, dessin,
                       vitesse_px_s: float, espacement_lettres: float = 0.0,
                       condensation: float = 1.0,
                       taille_min_px: int | None = None):
    """Police globale du flux : réduite (jamais agrandie) si le débit spatial
    maximal de la parole dépasse la capacité de la piste à vitesse constante.
    C'est le geste du rythmographe : on condense le texte plutôt que d'accélérer
    la bande — sinon l'anti-chevauchement cumule un retard sans retour (dérive
    constatée par l'utilisateur : +330 px à 52 s).

    T51/T52 : le débit est mesuré avec la largeur TRACKÉE et CONDENSÉE (lettres
    serrées + typo condensée) — à parole égale, la police réduite est donc moins
    petite : le texte reste aussi grand que possible tout en tenant dans la piste.
    Le plancher de lisibilité (``taille_min_px``, défaut ``POLICE_PISTE_MIN = 14``
    — ou le minimum choisi par l'utilisateur) borne la réduction : le texte ne
    devient jamais illisible ; l'anti-chevauchement reste le filet pour les pics
    résiduels (léger retard, jamais de superposition)."""
    if not mots:
        return None
    mini = max(POLICE_PISTE_MIN, int(taille_min_px or POLICE_PISTE_MIN))

    def mesure_debit(pol):
        larg = [largeur_trackee(dessin, w.text, pol, espacement_lettres,
                                condensation) for w in mots]
        esp = dessin.textlength(" ", font=pol) * condensation
        return _debit_spatial_max(mots, larg, esp)

    cible = vitesse_px_s * 0.97  # petite marge : le pire débit doit tenir
    debit_style = mesure_debit(police)
    if debit_style <= cible:
        return None  # la parole tient : police du style conservée

    taille = max(mini, int(taille_style * (cible / debit_style)))
    for _ in range(8):  # la mesure n'est pas linéaire en taille : on itère
        if taille <= mini:
            taille = mini
            break
        pol = get_police(taille)
        if mesure_debit(pol) <= cible:
            break
        taille = max(mini, int(round(taille * (cible / mesure_debit(pol)))))
    if taille >= taille_style:
        return None
    return get_police(taille)


@dataclass
class RepliqueVisuelle:
    """Cue pré-calculée pour le rendu (mesures police + plan de défilement).

    RYTHMO (flux continu T47) : ``prefixes_px`` = positions de **piste absolues**
    ``s_i`` (vidéo entière) et ``plan`` = plan global partagé — la position écran
    d'un mot est ``plan.offset_en(t) + s_i``.

    T-REF : ``polices_mots[i]`` pour une syllabe tenue vaut la police de piste
    (signal « mot tenu », hauteur constante — la référence n'agrandit jamais les
    lettres) et ``scale_x_mots[i]`` porte le facteur d'étirement horizontal
    (empreinte / largeur naturelle, borné). ``None`` pour un mot rapide et pour
    le style RÉPLIQUE (jamais étiré).
    """

    cue: Cue
    lignes: list[str]
    prefixes_px: list[float]
    plan: object
    debut_affichage: float
    fin_affichage: float
    curseur_x_px: int = 100
    police: object | None = None  # police ajustée (REPLIQUE étroit) ; None = police du style
    polices_mots: list[object | None] | None = None  # mot tenu → police de piste (signal T-REF)
    scale_x_mots: list[float | None] | None = None  # T-REF : étirement horizontal par mot tenu


def preparer_repliques(cues: list[Cue], police, largeur_bande: int, curseur_x_px: int,
                       style: StyleBande) -> list[RepliqueVisuelle]:
    """Mesure chaque cue ; construit le plan de scroll ; calcule les fenêtres.

    RYTHMO : **flux continu** (T47) — piste unique sur toute la vidéo, positions
    ancrées au temps (chaque mot sous le curseur à son start), fenêtres déduites
    de la géométrie (entrée bord droit / sortie bord gauche) ; deux répliques
    proches se chevauchent à l'écran en toute continuité (intervalles disjoints).
    REPLIQUE : fenêtres jointives au milieu du silence (une seule réplique centrée
    à la fois), anticipation/linger historiques.
    """
    dessin = ImageDraw.Draw(Image.new("RGB", (4, 4)))
    presentes = [c for c in cues if c.words]
    plans_par_cue: dict[int, object] = {}  # T149 : plan de la voix par réplique

    if style.style == "REPLIQUE":
        debuts = [max(0.0, c.start - style.anticipation_s) for c in presentes]
        fins = [c.end + style.linger_s for c in presentes]
        for i in range(1, len(presentes)):
            milieu = (presentes[i - 1].end + presentes[i].start) / 2
            fins[i - 1] = min(fins[i - 1], milieu)
            debuts[i] = max(debuts[i], fins[i - 1])
            debuts[i] = min(debuts[i], presentes[i].start)  # jamais après le vrai début
    else:  # RYTHMO : piste continue — positions puis fenêtres géométriques
        vitesse_px_s = style.vitesse_ratio * largeur_bande
        # T48 : si la parole dépasse la capacité de la piste, police réduite
        # (globale à toute la vidéo) — sinon l'anti-chevauchement cumule un
        # retard sans retour = dérive du texte loin du curseur
        mots_tous = [w for c in presentes for w in c.words]
        min_effectif = taille_police_min_effective(style.hauteur_bande,
                                                   style.taille_police_min)
        police_piste = _police_pour_piste(mots_tous, police, style.taille_police,
                                          dessin, vitesse_px_s,
                                          style.espacement_lettres,
                                          style.condensation,
                                          taille_min_px=min_effectif)
        # T107 : deux voix distinctes se partagent la hauteur de bande — la
        # police de piste (comme les mots tenus, plus bas) est plafonnée à
        # ~0,42× de la bande pour que chaque rang tienne dans sa moitié.
        deux_voix = len({c.personnage for c in presentes
                         if c.personnage is not None}) == 2
        if deux_voix and (police_piste or police).size > int(style.hauteur_bande * 0.42):
            police_piste = get_police(int(style.hauteur_bande * 0.42))
        police_mesure = police_piste if police_piste is not None else police
        espace_px = (dessin.textlength(" ", font=police_mesure)
                     * style.condensation)
        pistes: list[list[float]] = []
        fins_piste: list[float] = []
        polices_par_replique: list[list[object | None]] = []
        etirements_par_replique: list[list[float | None]] = []
        # Parole simultanée (T127) : chaque voix a sa PROPRE piste, indépendante.
        # Un unique ``s_min`` global forcerait les mots d'une voix à se caler
        # après la fin de la piste de l'autre — un tour qui chevauche un tour
        # voisin glisserait loin du curseur. Ici ``s_min`` est mémorisé par voix
        # (et None = voix inconnue, piste propre) : deux tours qui se recouvrent
        # défilent chacun sur son rang, au bon instant.
        s_min_par_voix: dict[int | None, float] = {}
        for c in presentes:
            # T-REF : la police de CHAQUE mot est la police de piste (hauteur
            # constante, geste référence clideo) ; un mot tenu est étiré
            # HORIZONTALEMENT — largeur de piste = empreinte acoustique v·durée
            # — et les lettres restent adjacentes (geste T53 : pas de tracking).
            polices_cue: list[object | None] = []
            etirements_cue: list[float | None] = []
            largeurs: list[float] = []
            for w in c.words:
                naturelle = largeur_trackee(dessin, w.text, police_mesure,
                                            style.espacement_lettres,
                                            style.condensation)
                police_mot = None
                scale_x = None
                if style.etirer_mots:
                    # T-REF : un mot/syllabe soutenu est rendu à la police de
                    # piste (hauteur d'encre CONSTANTE — la référence n'agrandit
                    # jamais les lettres) et ses glyphes sont ALLONGÉS en
                    # largeur par ``scale_x`` = empreinte / largeur naturelle :
                    # la largeur de piste du mot = v·durée → le mot démarre sous
                    # le curseur à v·début et son bord droit passe au curseur à
                    # v·fin (sync T54 conservée, sans lettres géantes). Le
                    # facteur est borné (FACTEUR_ETIREMENT_MAX) : une empreinte
                    # énorme ne peut pas être comblée intégralement, le mot
                    # finit alors avant v·fin.
                    empreinte = largeurs_piste_etirees(
                        [w], [naturelle], vitesse_px_s)[0]
                    scale_x = facteur_etirement(naturelle, empreinte)
                    if scale_x is not None:
                        # le mot tenu « porte » sa police de piste : contrat
                        # ``polices_mots`` conservé (signal + mesures de rang)
                        police_mot = police_mesure
                polices_cue.append(police_mot)
                etirements_cue.append(scale_x)
                police_eff = (police_mot[-1] if isinstance(police_mot, list)
                             else police_mot) or police_mesure
                if isinstance(police_mot, list):
                    largeurs.append(_largeur_trackee_lettres(
                        dessin, w.text, police_mot,
                        style.espacement_lettres, style.condensation))
                    continue
                largeurs.append(largeur_trackee(dessin, w.text, police_eff,
                                                style.espacement_lettres,
                                                style.condensation)
                                * (scale_x if scale_x is not None else 1.0))
            s = layout_flux(c.words, largeurs, espace_px, vitesse_px_s,
                            s_min=s_min_par_voix.get(c.personnage, 0.0))
            pistes.append(s)
            fin_piste = s[-1] + largeurs[-1]
            fins_piste.append(fin_piste)
            s_min_par_voix[c.personnage] = fin_piste + espace_px
            polices_par_replique.append(polices_cue)
            etirements_par_replique.append(etirements_cue)
        debuts = [max(0.0, (s[0] + curseur_x_px - largeur_bande) / vitesse_px_s)
                  for s in pistes]
        fins = [(fin_piste + curseur_x_px) / vitesse_px_s
                for fin_piste in fins_piste]
        if style.vitesse_dynamique:
            # T149 — loi « vitesse constante par réplique » (ancres 1ᵉʳ/dernier
            # mot) : positions TEMPORELLES, un plan PAR VOIX, fenêtres déduites
            # par inversion de la loi. La piste rigide flux ci-dessus est
            # calculée puis remplacée : le réducteur de densité T48 (police de
            # piste) reste le même garde-fou (plan `plans/vitesse-dynamique-par-replique.md`).
            voix: dict[int | None, list[Cue]] = {}
            for c in presentes:
                voix.setdefault(c.personnage, []).append(c)
            info_par_cue: dict[int, tuple[object, object]] = {}
            for personnage, cues_voix in voix.items():
                sequences = []
                for c in cues_voix:
                    nat = [largeur_trackee(dessin, w.text, police_mesure,
                                           style.espacement_lettres,
                                           style.condensation)
                           for w in c.words]
                    sequences.append((c.words, nat))
                plan, segs = construire_plan_dynamique(
                    float(curseur_x_px), vitesse_px_s, sequences, espace_px,
                    duree_s=(max((c.words[-1].end for c in presentes),
                                 default=0.0)
                             + (curseur_x_px + largeur_bande) / vitesse_px_s
                             + 2.0),
                    etirer=style.etirer_mots,
                    v_min_px_s=VITESSE_RATIO_MIN * largeur_bande,
                    v_max_px_s=VITESSE_DYNAMIQUE_MAX_RATIO * vitesse_px_s)
                for c, seg in zip(cues_voix, segs):
                    info_par_cue[id(c)] = (plan, seg)
            debuts, fins = [], []
            pistes, polices_par_replique, etirements_par_replique = [], [], []
            for c in presentes:
                plan, seg = info_par_cue[id(c)]
                plans_par_cue[id(c)] = plan
                s1 = seg.positions[0]
                bord_droit = seg.positions[-1] + seg.largeurs[-1]
                debuts.append(temps_pour_offset(plan, largeur_bande - s1))
                fins.append(temps_pour_offset(plan, -bord_droit))
                pistes.append(seg.positions)
                polices_par_replique.append(
                    [police_mesure if e is not None else None
                     for e in seg.echelles])
                etirements_par_replique.append(seg.echelles)

    plan_flux = None
    if style.style != "REPLIQUE" and presentes:
        plan_flux = build_plan_flux(float(curseur_x_px),
                                    style.vitesse_ratio * largeur_bande,
                                    duree_s=max(fins) + 1.0)

    visuelles: list[RepliqueVisuelle] = []
    for i, (cue, debut, fin) in enumerate(zip(presentes, debuts, fins)):
        mots = [w.text for w in cue.words]
        if style.style == "REPLIQUE":
            prefixes = [0.0]
            for m in mots[:-1]:
                prefixes.append(prefixes[-1] + dessin.textlength(m + " ", font=police))
            plan = None
            polices_cue = None
            etirements_cue = None
        else:
            prefixes = pistes[i]  # positions de piste absolues
            plan = plans_par_cue.get(id(cue), plan_flux)  # T149 : plan de la voix
            polices_cue = polices_par_replique[i]
            etirements_cue = etirements_par_replique[i]
        police_replique = None
        if style.style == "RYTHMO":
            lignes = [joindre_mots_fr(mots)]
            police_replique = police_piste  # police ajustée densité (ou None)
        else:
            # autofit : d'abord 2 lignes, puis 3 si la hauteur le permet ;
            # la taille descend jusqu'à ce que largeur ET hauteur tiennent.
            largeur_utile = largeur_bande - 2 * style.marge_px
            trouve = None
            for max_lignes in (2, 3):
                for facteur in (1.0, 0.9, 0.8, 0.72, 0.65, 0.58, 0.5, 0.42, 0.35):
                    taille = max(10, int(style.taille_police * facteur))
                    candidate = police if facteur == 1.0 else get_police(taille)
                    essai = reflow_lignes(cue.text, candidate, largeur_utile,
                                          max_lignes=max_lignes)
                    pire = max((dessin.textlength(l, font=candidate) for l in essai),
                               default=0)
                    hauteur_totale = _hauteur_texte(candidate) * len(essai)
                    if pire <= largeur_utile and hauteur_totale <= style.hauteur_bande * 0.92:
                        trouve = (essai, None if facteur == 1.0 else candidate)
                        break
                if trouve:
                    break
            lignes, police_replique = trouve if trouve else (
                reflow_lignes(cue.text, get_police(10), largeur_utile, max_lignes=3),
                get_police(10))
        visuelles.append(RepliqueVisuelle(cue=cue, lignes=lignes, prefixes_px=prefixes,
                                          plan=plan, debut_affichage=debut,
                                          fin_affichage=fin, curseur_x_px=curseur_x_px,
                                          police=police_replique,
                                          polices_mots=polices_cue,
                                          scale_x_mots=etirements_cue))
    return visuelles


def _couleur_mot(t: float, debut: float, fin: float, style: StyleBande):
    if t < debut:
        return style.futur
    if t <= fin:
        return style.actif
    return style.passe


def render_band_frame(t: float, repliques: list[RepliqueVisuelle], largeur: int,
                      style: StyleBande) -> Image.Image:
    """Image RGB de la bande à l'instant ``t`` (secondes)."""
    h = style.hauteur_bande
    img = Image.new("RGB", (largeur, h), style.fond)
    dessin = ImageDraw.Draw(img)

    curseur_x = (repliques[0].curseur_x_px if repliques
                 else x_curseur(style, largeur))

    if style.style == "RYTHMO":
        # flux continu : TOUTES les répliques présentes à l'écran cohabitent
        # (piste rigide, intervalles disjoints — continuité entre répliques)
        presentes = [r for r in repliques
                     if r.debut_affichage <= t <= r.fin_affichage]
        police = next((r.police for r in presentes if r.police is not None),
                      _police_active(style))
        ascent_piste, descente_piste = police.getmetrics()
        y = h // 2
        y_base = min(h - 4, y + descente_piste + max(2, h // 24))
        # T107 : deux voix distinctes → la ligne de base devient la SÉPARATION
        # des deux rangs : le trait est tracé au centre, la voix 1 (plus petite
        # étiquette) pose sa ligne de base DESSUS, la voix 2 s'accroche DESSOUS.
        voix = sorted({r.cue.personnage for r in repliques
                       if r.cue.personnage is not None})

        # T147 : ligne de base PAR RANG. L'ancrage « milieu » faisait dériver
        # la ligne de base avec la taille de police (baseline = y_milieu +
        # (ascent − descente)/2) : « double » 28 px → 58 mais « crème ! » 81 px
        # → 76 — les mots d'une même phrase n'étaient pas alignés sur le trait.
        # Désormais TOUS les mots d'un rang partagent la MÊME baseline ; en rang
        # unique, elle est POSÉE SUR LE TRAIT (un mot tenu ne pousse que vers le
        # haut, geste du rythmographe) — avec repli automatique vers le
        # recentrage si le plus grand mot ne tient pas au-dessus du trait
        # (glyphes jamais rognés, comportement T54 conservé).
        police_max = police
        for repl in presentes:
            for i, _ in enumerate(repl.cue.words):
                pm = repl.polices_mots[i] if repl.polices_mots else None
                eff = (pm[-1] if isinstance(pm, list) else pm) or police
                if eff.size > police_max.size:
                    police_max = eff
        a_max, d_max = police_max.getmetrics()
        if y_base - a_max >= 2 and y_base + d_max <= h - 2:
            baseline_unique = y_base
        else:
            baseline_unique = y + (a_max - d_max) / 2
        baseline_par_voix: dict[int, float] = {}
        if len(voix) == 2:
            # La baseline de chaque rang est calculée avec la police la PLUS
            # GRANDE possible d'un mot (le plafond 0,42× de la bande) : les
            # polices dynamiques T54 grossissent chaque mot jusqu'à ce plafond
            # — ancrer sur la police de piste ferait déborder les grands mots
            # sur le trait. T107 : la voix 1 est nettement AU-DESSUS du trait
            # (bas de texte à distance), la voix 2 s'accroche dessous — le
            # trait est la séparation des deux rangs.
            y_ligne = h // 2
            plafond_mot = max(police.size,
                              int(style.hauteur_bande * 0.42))
            ascent_max, descente_max = get_police(plafond_mot).getmetrics()
            marge_voix1 = max(2, h // 24)
            decalage = (ascent_piste - descente_piste) / 2
            baseline_par_voix[voix[0]] = (max(y_ligne - descente_max
                                              - marge_voix1, ascent_max)
                                          + decalage)
            baseline_par_voix[voix[1]] = (min(y_ligne + ascent_max + 1,
                                              h - descente_max - 2)
                                          + decalage)
            y_base = y_ligne
        if style.ligne_base is not None:  # repère horizontal continu (studio pro)
            dessin.line([0, y_base, largeur, y_base], fill=style.ligne_base,
                        width=max(2, h // 40))
        for repl in presentes:
            offset = repl.plan.offset_en(t)
            # T147 : ligne de base commune du rang (voix connue, ou rang de
            # repli pour une voix inconnue dans une bande à deux voix)
            y_base_mot = (baseline_par_voix.get(repl.cue.personnage,
                                                baseline_unique)
                          if baseline_par_voix else baseline_unique)
            for i, (w, prefix) in enumerate(zip(repl.cue.words, repl.prefixes_px)):
                x = offset + prefix
                if x > largeur + 50:  # pas encore entré par la droite : inutile
                    continue
                # T-REF : syllabe tenue → étirement HORIZONTAL (glyphes allongés
                # en largeur, hauteur constante) ; police_mot = police de piste.
                police_mot = (repl.polices_mots[i] if repl.polices_mots else None)
                croissant = isinstance(police_mot, list)
                scale_x = (repl.scale_x_mots[i] if repl.scale_x_mots else None)
                police_eff = (police_mot[-1] if croissant else police_mot) or police
                # T147 : chaque mot est ancré à la baseline du rang — conversion
                # « ligne de base » → ancre « milieu » (seule ancre de Pillow
                # utilisée par le rendu condensé) : baseline = y_texte +
                # (ascent − descente)/2, donc y_texte = baseline −
                # (ascent_eff − descente_eff)/2, par police effective.
                a_eff, d_eff = police_eff.getmetrics()
                y_texte = y_base_mot - (a_eff - d_eff) / 2
                if croissant:
                    largeur_ink = _largeur_trackee_lettres(
                        dessin, w.text, police_mot, style.espacement_lettres,
                        style.condensation)
                else:
                    largeur_ink = largeur_trackee(dessin, w.text, police_eff,
                                                  style.espacement_lettres,
                                                  style.condensation)
                    if scale_x is not None:
                        largeur_ink = largeur_ink * scale_x
                if w.marqueur and style.marqueur is not None:
                    # symbole de respiration (T80–T84) : encart dédié, jamais
                    # la couleur des mots parlés — le comédien respire pendant
                    # que le symbole passe sous le curseur (bande à vitesse
                    # constante, comme le rouleau des studios)
                    bbox_vert = dessin.textbbox((0, y_texte), w.text,
                                                font=police_eff, anchor="lm")
                    dessin.rounded_rectangle(
                        [x - 6, bbox_vert[1] - 3, x + largeur_ink + 6,
                         bbox_vert[3] + 3], radius=6, fill=style.marqueur_fond)
                    dessiner_mot_condense(img, x, y_texte, w.text, police_eff,
                                          style.marqueur,
                                          style.espacement_lettres,
                                          style.condensation
                                          * (scale_x if scale_x is not None else 1.0))
                    continue
                if style.surlignage is not None and w.start <= t <= w.end:
                    # boîte qui suit l'encre réelle : largeur condensée mesurée à
                    # la police (agrandie) du mot
                    bbox_vert = dessin.textbbox((0, y_texte), w.text,
                                                font=police_eff, anchor="lm")
                    bbox = (x, bbox_vert[1], x + largeur_ink, bbox_vert[3])
                    # boîte pleine arrondie + petite flèche pointant vers le trait
                    dessin.rounded_rectangle(
                        [bbox[0] - 7, bbox[1] - 4, bbox[2] + 7, bbox[3] + 5],
                        radius=8, fill=style.surlignage)
                    cx = (bbox[0] + bbox[2]) / 2
                    if y_texte < y_base:  # texte au-dessus : flèche sous le trait
                        y_tri = min(y_base + 2, h - 14)
                        dessin.polygon([(cx - 8, y_tri), (cx + 8, y_tri),
                                        (cx, min(y_base + 13, h - 1))],
                                       fill=style.surlignage)
                    else:  # texte en dessous : flèche au-dessus du trait (T107)
                        y_tri = max(2, y_base - 2)
                        dessin.polygon([(cx - 8, y_tri), (cx + 8, y_tri),
                                        (cx, max(2, y_base - 13))],
                                       fill=style.surlignage)
                # T51 + T52 : espacement de lettres + condensation ; T-REF : le
                # mot tenu est en plus étiré horizontalement (facteur scale_x
                # multiplié à la condensation : le rendu colle aux mesures)
                couleur_mot = _couleur_mot(t, w.start, w.end, style)
                if croissant:
                    dessiner_mot_croissant(img, x, y_base_mot, w.text, police_mot,
                                           couleur_mot, style.espacement_lettres,
                                           style.condensation)
                else:
                    dessiner_mot_condense(img, x, y_texte, w.text, police_eff,
                                          couleur_mot, style.espacement_lettres,
                                          style.condensation
                                          * (scale_x if scale_x is not None else 1.0))
    else:  # REPLIQUE : la réplique courante centrée (une seule à la fois)
        repl = next((r for r in repliques
                     if r.debut_affichage <= t <= r.fin_affichage), None)
        if repl is not None:
            police = repl.police if repl.police is not None else _police_active(style)
            hauteur_ligne = _hauteur_texte(police)
            y = (h - hauteur_ligne * len(repl.lignes)) / 2
            for ligne in repl.lignes:
                dessin.text((largeur / 2, y), ligne, font=police, anchor="ma",
                            fill=style.actif)
                y += hauteur_ligne

    if style.style == "RYTHMO":  # curseur par-dessus le texte (toujours visible)
        dessin.rectangle([curseur_x, 0, curseur_x + 1, h], fill=style.curseur)
    return img


_POLICES: dict[int, object] = {}


def _police_active(style: StyleBande):
    taille = style.taille_police
    if taille not in _POLICES:
        _POLICES[taille] = get_police(taille)
    return _POLICES[taille]
