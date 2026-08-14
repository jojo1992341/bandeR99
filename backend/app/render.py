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

from .cues import (VITESSE_RATIO_DEFAUT, VITESSE_RATIO_MAX, VITESSE_RATIO_MIN,
                   Cue, build_plan_flux, joindre_mots_fr, largeurs_piste_etirees,
                   layout_flux, reflow_lignes)

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
# (T54 : la police des mots tenus est pilotée par leur empreinte acoustique,
# plus de plafond 1,35× — seule la hauteur de bande borne l'agrandissement.)
POLICE_PISTE_MIN = 14


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
    dessin direct (via :func:`dessiner_mot_tracke`). Renvoie l'avance dessinée
    (= la mesure :func:`largeur_trackee` avec la même condensation, ±1 px
    d'arrondi). Peut recevoir un seul caractère (syllabes tenues T49/T51).
    """
    if not texte:
        return 0.0
    if condensation >= 1.0 - 1e-9:
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


@dataclass(frozen=True)
class StyleBande:
    """Paramètres visuels de la bande (couleurs None/auto = palette du thème)."""

    style: str = "RYTHMO"  # ou "REPLIQUE"
    theme: str = "STUDIO"  # ou "SOMBRE"
    hauteur_bande: int = 96
    taille_police: int = 40
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


def taille_police_auto(largeur: int, hauteur_bande: int | None = None) -> int:
    """Taille par défaut : max(≈1/25e de la largeur, ≈0,28 × hauteur de bande).

    T52 : la hauteur de bande impose un corps minimal pour que le texte REMPLISSE
    la bande comme la référence pro (densité ~1/25e mesurée sur la réf. à vitesse
    constante — la loi de densité T48 condense ensuite si la parole l'exige).
    L'appel historique sans hauteur garde l'ancienne loi (T47 inchangé)."""
    par_largeur = round(largeur * 0.04)
    par_hauteur = round(hauteur_bande * 0.28) if hauteur_bande else 0
    return int(max(14, min(90, max(par_largeur, par_hauteur))))


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
                       condensation: float = 1.0):
    """Police globale du flux : réduite (jamais agrandie) si le débit spatial
    maximal de la parole dépasse la capacité de la piste à vitesse constante.
    C'est le geste du rythmographe : on condense le texte plutôt que d'accélérer
    la bande — sinon l'anti-chevauchement cumule un retard sans retour (dérive
    constatée par l'utilisateur : +330 px à 52 s).

    T51/T52 : le débit est mesuré avec la largeur TRACKÉE et CONDENSÉE (lettres
    serrées + typo condensée) — à parole égale, la police réduite est donc moins
    petite : le texte reste aussi grand que possible tout en tenant dans la piste
    (plancher de lisibilité POLICE_PISTE_MIN = 14 px, T52)."""
    if not mots:
        return None

    def mesure_debit(pol):
        larg = [largeur_trackee(dessin, w.text, pol, espacement_lettres,
                                condensation) for w in mots]
        esp = dessin.textlength(" ", font=pol) * condensation
        return _debit_spatial_max(mots, larg, esp)

    cible = vitesse_px_s * 0.97  # petite marge : le pire débit doit tenir
    debit_style = mesure_debit(police)
    if debit_style <= cible:
        return None  # la parole tient : police du style conservée

    taille = max(POLICE_PISTE_MIN, int(taille_style * (cible / debit_style)))
    for _ in range(8):  # la mesure n'est pas linéaire en taille : on itère
        if taille <= POLICE_PISTE_MIN:
            taille = POLICE_PISTE_MIN
            break
        pol = get_police(taille)
        if mesure_debit(pol) <= cible:
            break
        taille = max(POLICE_PISTE_MIN,
                     int(round(taille * (cible / mesure_debit(pol)))))
    if taille >= taille_style:
        return None
    return get_police(taille)


@dataclass
class RepliqueVisuelle:
    """Cue pré-calculée pour le rendu (mesures police + plan de défilement).

    RYTHMO (flux continu T47) : ``prefixes_px`` = positions de **piste absolues**
    ``s_i`` (vidéo entière) et ``plan`` = plan global partagé — la position écran
    d'un mot est ``plan.offset_en(t) + s_i``.

    T51/T53 : ``polices_mots[i]`` = police AGrandie embarquée pour une syllabe
    tenue (``None`` = mot à la police de piste) — la syllabe tenue se lit en PLUS
    GRAND avec un espacement de lettres NORMAL (typo pro, T53 ; l'étirement
    proportionnel T49 a été retiré : il écartait les lettres et rendait les mots
    illisibles). ``None`` pour le style RÉPLIQUE (jamais agrandi).
    """

    cue: Cue
    lignes: list[str]
    prefixes_px: list[float]
    plan: object
    debut_affichage: float
    fin_affichage: float
    curseur_x_px: int = 100
    police: object | None = None  # police ajustée (REPLIQUE étroit) ; None = police du style
    polices_mots: list[object | None] | None = None      # T51/T53 : police agrandie par mot tenu


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
        police_piste = _police_pour_piste(mots_tous, police, style.taille_police,
                                          dessin, vitesse_px_s,
                                          style.espacement_lettres,
                                          style.condensation)
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
        s_min = 0.0
        for c in presentes:
            # T53 : la police de CHAQUE mot est choisie d'abord (syllabe tenue →
            # police agrandie, geste T51), puis la piste utilise la largeur
            # naturelle À CETTE police — l'espacement entre mots reste normal,
            # comme la bande pro (plus d'étirement proportionnel des lettres).
            polices_cue: list[object | None] = []
            largeurs: list[float] = []
            for w in c.words:
                naturelle = largeur_trackee(dessin, w.text, police_mesure,
                                            style.espacement_lettres,
                                            style.condensation)
                police_mot = None
                if style.etirer_mots:
                    # T54 : police DYNAMIQUE par mot — la largeur naturelle
                    # (lettres NORMALES, T53) remplit EXACTEMENT l'empreinte
                    # acoustique v·durée : le mot démarre sous le curseur à
                    # v·début et son bord droit passe au curseur à v·fin (sync
                    # T49 restaurée sans écartement). Plus de plafond 1,35× :
                    # c'est l'empreinte qui pilote la taille. Bornes physiques :
                    # jamais sous la police de piste (mot rapide → None) ni
                    # au-dessus de 0,85 × hauteur de bande (les glyphes
                    # restent dans la bande — une empreinte énorme ne peut pas
                    # être comblée, le mot finit alors avant v·fin).
                    empreinte = largeurs_piste_etirees(
                        [w], [naturelle], vitesse_px_s)[0]
                    if empreinte > naturelle + 1e-6:
                        # largeur ∥ police (textlength + tracking linéaires en
                        # taille) : base × ratio remplit l'empreinte à ~1 px
                        # près ; quelques itérations absorbent l'arrondi entier
                        taille_mot = round(police_mesure.size
                                           * empreinte / naturelle)
                        for _ in range(3):
                            candidat = get_police(max(1, taille_mot))
                            largeur_cand = largeur_trackee(
                                dessin, w.text, candidat,
                                style.espacement_lettres, style.condensation)
                            if largeur_cand <= 0:
                                break
                            ajust = empreinte / largeur_cand
                            if 0.995 <= ajust <= 1.005:
                                break
                            taille_mot = round(taille_mot * ajust)
                        # T107 : à deux voix, chaque rang ne dispose que de la
                        # moitié de la bande — plafond 0,42× (au lieu de 0,85×)
                        taille_mot = min(taille_mot,
                                         int(style.hauteur_bande
                                             * (0.42 if deux_voix else 0.85)))
                        police_mot = get_police(max(1, taille_mot))
                polices_cue.append(police_mot)
                police_eff = police_mot if police_mot is not None else police_mesure
                largeurs.append(largeur_trackee(dessin, w.text, police_eff,
                                                style.espacement_lettres,
                                                style.condensation))
            s = layout_flux(c.words, largeurs, espace_px, vitesse_px_s, s_min=s_min)
            pistes.append(s)
            fin_piste = s[-1] + largeurs[-1]
            fins_piste.append(fin_piste)
            s_min = fin_piste + espace_px
            polices_par_replique.append(polices_cue)
        debuts = [max(0.0, (s[0] + curseur_x_px - largeur_bande) / vitesse_px_s)
                  for s in pistes]
        fins = [(fin_piste + curseur_x_px) / vitesse_px_s
                for fin_piste in fins_piste]

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
        else:
            prefixes = pistes[i]  # positions de piste absolues
            plan = plan_flux
            polices_cue = polices_par_replique[i]
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
                                          polices_mots=polices_cue))
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
        y = h // 2
        _, descente = police.getmetrics()
        y_base = min(h - 4, y + descente + max(2, h // 24))
        # T107 : deux voix distinctes → la ligne de base devient la SÉPARATION
        # des deux rangs : le trait est tracé au centre, la voix 1 (plus petite
        # étiquette) pose sa ligne de base DESSUS, la voix 2 s'accroche DESSOUS.
        voix = sorted({r.cue.personnage for r in repliques
                       if r.cue.personnage is not None})
        y_par_voix: dict[int, int] = {}
        if len(voix) == 2:
            # L'ancre est calculée avec la police la PLUS GRANDE possible d'un
            # mot (le plafond 0,42× de la bande) : les polices dynamiques T54
            # grossissent chaque mot jusqu'à ce plafond — ancrer sur la police
            # de piste ferait déborder les grands mots sur le trait.
            y_ligne = h // 2
            plafond_mot = max(police.size,
                              int(style.hauteur_bande * 0.42))
            ascent_max, descente_max = get_police(plafond_mot).getmetrics()
            # T107 : la voix 1 est nettement AU-DESSUS du trait (bas de texte à
            # distance, jamais posé sur la ligne) ; la voix 2 s'accroche juste
            # en dessous — le trait est la séparation des deux rangs.
            marge_voix1 = max(2, h // 24)
            y_par_voix[voix[0]] = max(y_ligne - descente_max - marge_voix1,
                                      ascent_max)
            y_par_voix[voix[1]] = min(y_ligne + ascent_max + 1,
                                      h - descente_max - 2)
            y_base = y_ligne
        if style.ligne_base is not None:  # repère horizontal continu (studio pro)
            dessin.line([0, y_base, largeur, y_base], fill=style.ligne_base,
                        width=max(2, h // 40))
        for repl in presentes:
            offset = repl.plan.offset_en(t)
            y_texte = y_par_voix.get(repl.cue.personnage, y)
            for i, (w, prefix) in enumerate(zip(repl.cue.words, repl.prefixes_px)):
                x = offset + prefix
                if x > largeur + 50:  # pas encore entré par la droite : inutile
                    continue
                # T51/T53 : syllabe tenue → police AGrandie embarquée pour ce mot
                # (espacement de lettres normal, plus d'étirement proportionnel)
                police_mot = (repl.polices_mots[i] if repl.polices_mots else None)
                police_eff = police_mot if police_mot is not None else police
                if w.marqueur and style.marqueur is not None:
                    # symbole de respiration (T80–T84) : encart dédié, jamais
                    # la couleur des mots parlés — le comédien respire pendant
                    # que le symbole passe sous le curseur (bande à vitesse
                    # constante, comme le rouleau des studios)
                    largeur_ink = largeur_trackee(dessin, w.text, police_eff,
                                                  style.espacement_lettres,
                                                  style.condensation)
                    bbox_vert = dessin.textbbox((0, y_texte), w.text,
                                                font=police_eff, anchor="lm")
                    dessin.rounded_rectangle(
                        [x - 6, bbox_vert[1] - 3, x + largeur_ink + 6,
                         bbox_vert[3] + 3], radius=6, fill=style.marqueur_fond)
                    dessiner_mot_condense(img, x, y_texte, w.text, police_eff,
                                          style.marqueur,
                                          style.espacement_lettres,
                                          style.condensation)
                    continue
                if style.surlignage is not None and w.start <= t <= w.end:
                    # boîte qui suit l'encre réelle : largeur condensée mesurée à
                    # la police (agrandie) du mot
                    bbox_vert = dessin.textbbox((0, y_texte), w.text,
                                                font=police_eff, anchor="lm")
                    largeur_ink = largeur_trackee(dessin, w.text, police_eff,
                                                  style.espacement_lettres,
                                                  style.condensation)
                    bbox = (x, bbox_vert[1], x + largeur_ink, bbox_vert[3])
                    # boîte rosée arrondie + petite flèche pointant vers le trait
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
                # T51 + T52 : espacement de lettres + condensation — le rendu
                # colle aux mesures (lettres serrées, typo condensée pro)
                dessiner_mot_condense(img, x, y_texte, w.text, police_eff,
                                      _couleur_mot(t, w.start, w.end, style),
                                      style.espacement_lettres,
                                      style.condensation)
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
