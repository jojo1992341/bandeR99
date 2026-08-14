"""Édition mot-à-mot sur la timeline (T60–T63, session 15).

Le comédien cale chaque mot sur la timeline (glisser = translation, poignée de
fin = étirement). Le « verrouillage du sync » (Q15 : empêcher de casser le
sync) est garanti par des invariants structuraux que ces fonctions imposent
toujours, côté serveur comme côté client :

- ordre monotone : ``debut`` des mots jamais décroissant ;
- aucun chevauchement : la fin d'un mot ne dépasse jamais le début du suivant ;
- chaque mot reste dans la fenêtre ``[debut, fin]`` de sa réplique ;
- la fenêtre de la réplique suit son premier et son dernier mot.

Le client peut donc glisser librement : les bornes sont recalculées au vol
(le mot s'arrête au bord du voisin). Le serveur re-vérifie à la réception
(``valider_mots_edites``) — le verrouillage est infalsifiable côté API.
"""
from __future__ import annotations

from .cues_edit import DUREE_MOT_MIN_S
from .errors import RythmoError

_DECIMALES = 3


def _arrondir(valeur: float) -> float:
    return round(float(valeur), _DECIMALES)


def _replique_par_id(repliques, id_replique: str) -> dict:
    for r in repliques:
        if str(r.get("id")) == str(id_replique):
            return r
    raise RythmoError("E005", f"Réplique inconnue pour le déplacement mot-à-mot : "
                              f"{id_replique!r}")


def deplacer_mot(repliques, id_replique: str, index_mot: int, nouveau_debut: float) -> None:
    """Déplace un mot (translation : durée conservée), verrouillage du sync.

    Le mot est borné entre le début de la fenêtre de sa réplique et le début du
    mot suivant (jamais de chevauchement). S'il est premier ou dernier, la
    fenêtre ``[debut, fin]`` de la réplique suit le mot.
    """
    r = _replique_par_id(repliques, id_replique)
    mots = r.get("mots") or []
    if not mots:
        raise RythmoError("E005", f"Réplique « {r.get('texte', '')[:20]} » sans détail "
                                  f"mot : impossible de déplacer un mot.")
    if not 0 <= index_mot < len(mots):
        raise RythmoError("E005", f"Index de mot hors bornes : {index_mot} "
                                  f"(0–{len(mots) - 1}).")
    fenetre_debut, fenetre_fin = float(r["debut"]), float(r["fin"])
    duree = float(mots[index_mot]["fin"]) - float(mots[index_mot]["debut"])
    suivant_debut = (float(mots[index_mot + 1]["debut"]) if index_mot + 1 < len(mots)
                     else fenetre_fin)
    min_debut = float(mots[index_mot - 1]["fin"]) if index_mot > 0 else fenetre_debut
    max_debut = min(suivant_debut - duree, fenetre_fin - duree)
    nouveau = _arrondir(min(max(float(nouveau_debut), min_debut), max_debut))
    mots[index_mot].update({"debut": nouveau, "fin": _arrondir(nouveau + duree)})
    if index_mot == 0:
        r["debut"] = nouveau
    if index_mot == len(mots) - 1:
        r["fin"] = _arrondir(nouveau + duree)


def redimensionner_mot(repliques, id_replique: str, index_mot: int, nouveau_fin: float) -> None:
    """Étire/raccourcit un mot (poignée de fin), borné par le mot suivant.

    La durée reste ≥ ``DUREE_MOT_MIN_S`` ; jamais au-delà du début du mot
    suivant ni de la fin de la fenêtre. Dernier mot → la fenêtre suit.
    """
    r = _replique_par_id(repliques, id_replique)
    mots = r.get("mots") or []
    if not mots:
        raise RythmoError("E005", f"Réplique « {r.get('texte', '')[:20]} » sans détail "
                                  f"mot : impossible d'étirer un mot.")
    if not 0 <= index_mot < len(mots):
        raise RythmoError("E005", f"Index de mot hors bornes : {index_mot} "
                                  f"(0–{len(mots) - 1}).")
    fenetre_fin = float(r["fin"])
    debut = float(mots[index_mot]["debut"])
    suivant_debut = (float(mots[index_mot + 1]["debut"]) if index_mot + 1 < len(mots)
                     else fenetre_fin)
    min_fin = debut + DUREE_MOT_MIN_S
    max_fin = min(suivant_debut, fenetre_fin)
    nouveau = _arrondir(min(max(float(nouveau_fin), min_fin), max_fin))
    mots[index_mot]["fin"] = nouveau
    if index_mot == len(mots) - 1:
        r["fin"] = nouveau


def valider_mots_edites(mots, debut: float, fin: float) -> list[dict]:
    """Normalise les mots de la timeline : bornés, ordonnés, sans chevauchement.

    Invariants du verrouillage du sync appliqués à la réception (jamais de
    rejet : le serveur recalé, il ne casse pas le travail du comédien) :

    - ``debut ≤ d < f ≤ fin`` (fenêtre de la réplique) ;
    - débuts non décroissants, fin d'un mot ≤ début du suivant ;
    - durée ≥ ``DUREE_MOT_MIN_S`` ;
    - textes vides et durées nulles écartés.
    """
    propres: list[dict] = []
    for m in sorted((m for m in (mots or []) if isinstance(m, dict)),
                    key=lambda x: (x.get("debut"), x.get("fin"))):
        texte = str(m.get("texte", "")).strip()
        try:
            d, f = float(m["debut"]), float(m["fin"])
        except (KeyError, TypeError, ValueError):
            continue
        if not texte or not (d < f):
            continue
        d = min(max(d, float(debut)), float(fin) - DUREE_MOT_MIN_S)
        f = min(max(f, d + DUREE_MOT_MIN_S), float(fin))
        if propres:
            precedent = propres[-1]
            if d < precedent["fin"]:  # chevauchement : on rabote
                d = precedent["fin"]
                f = min(max(f, d + DUREE_MOT_MIN_S), float(fin))
        propres.append({"texte": texte,
                        "debut": _arrondir(d), "fin": _arrondir(f)})
    return propres
