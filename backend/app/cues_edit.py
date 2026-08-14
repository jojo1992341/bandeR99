"""Édition manuelle des répliques : validation et resynchronisation des mots.

Flux : l'utilisateur corrige textes et timings dans le front ; ``valider_repliques``
garantit des données saines (E005 sinon) ; ``resynchroniser_mots`` reconstruit les
timings mot à mot (nécessaires au défilement karaoké) en s'appuyant sur les
répliques d'origine produites par l'IA.
"""
from __future__ import annotations

import difflib
import re
import unicodedata

from .errors import RythmoError

TOLERANCE_FIN_S = 0.6          # une fin peut légèrement dépasser la durée vidéo
TOLERANCE_RECALAGE_S = 0.05    # léger recouvrement manuel toléré entre répliques
DUREE_MOT_MIN_S = 0.04         # jamais de mot à durée nulle (rendu karaoké)


def _est_nombre(valeur) -> bool:
    return isinstance(valeur, (int, float)) and not isinstance(valeur, bool)


def valider_repliques(repliques, duree_video: float,
                      tolerance_fin: float = TOLERANCE_FIN_S,
                      tolerance_recalage: float = TOLERANCE_RECALAGE_S) -> list[dict]:
    """Valide puis normalise les répliques éditées. Lève ``RythmoError("E005")``.

    Normalisation : texte stripé, temps float arrondis au millième, tri par debut,
    fin bornée à ``duree_video`` (au-delà de la tolérance → violation).
    """
    violations: list[str] = []
    if not isinstance(repliques, (list, tuple)):
        raise RythmoError("E005", "Le corps attendu est une liste de répliques.")
    if not repliques:
        raise RythmoError("E005", "La liste de répliques est vide : "
                                  "il faut au moins une réplique à afficher.")
    if not _est_nombre(duree_video) or duree_video <= 0:
        raise RythmoError("E005", f"Durée vidéo incohérente : {duree_video!r}")

    normalisees: list[dict] = []
    for i, replique in enumerate(repliques, start=1):
        etiquette = f"réplique {i}"
        if not isinstance(replique, dict):
            violations.append(f"{etiquette} : objet invalide")
            continue
        texte = replique.get("texte")
        debut = replique.get("debut")
        fin = replique.get("fin")
        if not isinstance(texte, str) or not texte.strip():
            violations.append(f"{etiquette} : texte vide ou invalide")
        if not _est_nombre(debut):
            violations.append(f"{etiquette} : debut invalide ({debut!r})")
        if not _est_nombre(fin):
            violations.append(f"{etiquette} : fin invalide ({fin!r})")
        if violations and any(v.startswith(etiquette) for v in violations):
            continue  # replique mal formée : inutile d'empiler d'autres contrôles
        if debut < 0:
            violations.append(f"{etiquette} : debut négatif ({debut})")
            continue
        if fin <= debut:
            violations.append(f"{etiquette} : debut ({debut}) doit être < fin ({fin})")
            continue
        if fin > duree_video + tolerance_fin:
            violations.append(
                f"{etiquette} : fin ({fin}) au-delà de la vidéo "
                f"({duree_video:.2f} s + tolérance {tolerance_fin} s)")
            continue
        # on conserve les champs auxiliaires (mots resynchronisés…) : la
        # normalisation ne retouche que les champs métier contrôlés ici
        entree = dict(replique)
        entree.update({"texte": texte.strip(),
                       "debut": round(float(debut), 3),
                       "fin": round(min(float(fin), float(duree_video)), 3)})
        identifiant = replique.get("id")
        if identifiant is not None:
            entree["id"] = str(identifiant)
        normalisees.append(entree)

    if not violations:
        normalisees.sort(key=lambda r: (r["debut"], r["fin"]))
        for i in range(1, len(normalisees)):
            precedente, courante = normalisees[i - 1], normalisees[i]
            recouvrement = precedente["fin"] - courante["debut"]
            if recouvrement > tolerance_recalage:
                violations.append(
                    f"répliques « {precedente['texte'][:20]} » et "
                    f"« {courante['texte'][:20]} » : chevauchement de {recouvrement:.2f} s")
                break
    if violations:
        raise RythmoError("E005", "Répliques invalides — " + " ; ".join(violations))
    return normalisees


def normaliser_token(texte: str) -> str:
    """Forme de comparaison : minuscules, sans accents, sans ponctuation."""
    brut = unicodedata.normalize("NFKD", texte.lower())
    sans_accents = "".join(c for c in brut if not unicodedata.combining(c))
    return re.sub(r"[^\w]", "", sans_accents, flags=re.UNICODE)


def _distribuer_uniforme(tokens: list[str], debut: float, fin: float) -> list[dict]:
    """Découpe [debut, fin] en parts égales (repli sans information d'alignement)."""
    n = len(tokens)
    largeur = max((fin - debut) / n, DUREE_MOT_MIN_S)
    return [{"texte": t,
             "debut": round(min(debut + i * largeur, fin - DUREE_MOT_MIN_S), 3),
             "fin": round(min(debut + (i + 1) * largeur, fin), 3)}
            for i, t in enumerate(tokens)]


def _rescaler_mots(mots: list[dict], fenetre_orig: tuple[float, float],
                   fenetre_nouvelle: tuple[float, float]) -> list[dict]:
    """Recale linéairement les timings d'origine dans la fenêtre éditée."""
    d_o, f_o = fenetre_orig
    d_n, f_n = fenetre_nouvelle
    if f_o - d_o <= 1e-9 or f_n - d_n <= 1e-9:
        return []
    facteur = (f_n - d_n) / (f_o - d_o)
    return [{"texte": m["texte"],
             "debut": d_n + (float(m["debut"]) - d_o) * facteur,
             "fin": d_n + (float(m["fin"]) - d_o) * facteur} for m in mots]


def _aligner_tokens(tokens: list[str], mots_recales: list[dict],
                    debut: float, fin: float) -> list[dict]:
    """Affecte un intervalle à chaque token édité via alignement difflib.

    - token apparié (equal)              → intervalle du mot d'origine ;
    - token remplacé (replace, ex. typo) → part égale de l'union des mots remplacés ;
    - token inséré (insert)              → part égale du trou entre voisins.
    """
    if not mots_recales:
        return _distribuer_uniforme(tokens, debut, fin)
    seq_o = [normaliser_token(m["texte"]) for m in mots_recales]
    seq_e = [normaliser_token(t) for t in tokens]
    intervalles: list[tuple[float, float] | None] = [None] * len(tokens)

    for etiquette, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, seq_o, seq_e, autojunk=False).get_opcodes():
        if etiquette == "equal":
            for k in range(i2 - i1):
                m = mots_recales[i1 + k]
                intervalles[j1 + k] = (m["debut"], m["fin"])
        elif etiquette == "replace":
            g0 = mots_recales[i1]["debut"] if i1 < len(mots_recales) else debut
            g1 = mots_recales[i2 - 1]["fin"] if i2 > i1 else g0
            tranche = max(g1 - g0, DUREE_MOT_MIN_S) / max(j2 - j1, 1)
            for j in range(j1, j2):
                intervalles[j] = (g0 + (j - j1) * tranche, g0 + (j - j1 + 1) * tranche)
        elif etiquette == "insert":
            g0 = mots_recales[i1 - 1]["fin"] if i1 > 0 else debut
            g1 = mots_recales[i1]["debut"] if i1 < len(mots_recales) else fin
            if g1 - g0 < 2 * DUREE_MOT_MIN_S:  # pas de trou : on empiète à part égale
                g0 = mots_recales[i1 - 1]["debut"] if i1 > 0 else debut
                g1 = mots_recales[i1 - 1]["fin"] if i1 > 0 else g0 + DUREE_MOT_MIN_S
            tranche = (g1 - g0) / (j2 - j1 + 2)  # marge des deux côtés du trou
            for j in range(j1, j2):
                intervalles[j] = (g0 + (j - j1 + 1) * tranche,
                                  g0 + (j - j1 + 2) * tranche)
        # delete : mot d'origine supprimé par l'utilisateur, rien à porter
    comble = _distribuer_uniforme(tokens, debut, fin)
    return [{"texte": t,
             "debut": (intervalles[i][0] if intervalles[i] else comble[i]["debut"]),
             "fin": (intervalles[i][1] if intervalles[i] else comble[i]["fin"])}
            for i, t in enumerate(tokens)]


def _forcer_monotonie(mots: list[dict], debut: float, fin: float) -> list[dict]:
    """Invariants de rendu : debut ≤ d < f ≤ fin et débuts non décroissants."""
    for m in mots:
        m["debut"] = min(max(float(m["debut"]), debut), fin - DUREE_MOT_MIN_S)
        m["fin"] = min(max(float(m["fin"]), m["debut"] + DUREE_MOT_MIN_S), fin)
    precedent = debut
    for m in mots:
        if m["debut"] < precedent - 1e-9:
            glisser = precedent - m["debut"]
            m["debut"] = precedent
            m["fin"] = min(m["fin"] + glisser, fin)
            if m["fin"] <= m["debut"]:
                m["debut"] = min(m["debut"], fin - DUREE_MOT_MIN_S)
                m["fin"] = m["debut"] + DUREE_MOT_MIN_S
        precedent = m["fin"] if m["fin"] > precedent else precedent
        m["debut"], m["fin"] = round(m["debut"], 3), round(m["fin"], 3)
    return mots


def resynchroniser_mots(repliques_editees: list[dict],
                        originales: list[dict]) -> list[dict]:
    """Attache à chaque réplique éditée des ``mots`` timés dans [debut, fin].

    La correspondance se fait par ``id`` (inconnu/absent → distribution uniforme) ;
    les timings d'origine sont d'abord rescalés dans la nouvelle fenêtre, puis
    alignés token à token (la casse et la ponctuation n'invalident pas l'appariement).
    """
    index = {str(r.get("id")): r for r in originales if r.get("id") is not None}
    sortie: list[dict] = []
    for replique in repliques_editees:
        r = dict(replique)
        debut, fin = float(r["debut"]), float(r["fin"])
        tokens = r["texte"].split()
        originale = index.get(str(r.get("id")))
        if "personnage" in r:
            # Choix explicite du comédien (T94) : un nombre remplace la voix
            # détectée, None (menu « — ») retire la voix — le champ disparaît.
            if r["personnage"] is None:
                r.pop("personnage")
        elif originale and "personnage" in originale:
            r["personnage"] = originale["personnage"]  # la voix survit à l'édition
        mots_o = (originale or {}).get("mots") or []
        # Timeline mot-à-mot (T62) : si le front a envoyé des mots dont les
        # textes correspondent exactement à ceux de la réplique, les positions
        # glissées sont la vérité — on les valide (verrouillage du sync) et on
        # les conserve telles quelles. Dès que le texte change, on repart sur
        # l'alignement difflib (les mots anciens sont périmés).
        mots_explicites = [m for m in (r.get("mots") or []) if isinstance(m, dict)]
        textes_explicites = [normaliser_token(m.get("texte", ""))
                             for m in mots_explicites]
        if textes_explicites and textes_explicites == [normaliser_token(t)
                                                       for t in tokens]:
            # Le texte tapé fait foi, les positions glissées restent : on
            # garde les timings mot-à-mot (verrouillés par valider_mots_edites)
            # et on y reporte les textes exacts de la réplique (casse,
            # ponctuation). Un changement de mots (ajout/suppression/refonte)
            # change la liste normalisée → bascule sur l'alignement difflib.
            from .edition_mots import valider_mots_edites

            gardes = valider_mots_edites(mots_explicites, debut, fin)
            for m, token in zip(gardes, tokens):
                m["texte"] = token
            r["mots"] = gardes
        elif not tokens:
            r["mots"] = []
        elif not mots_o:
            r["mots"] = _distribuer_uniforme(tokens, debut, fin)
        else:
            recales = _rescaler_mots(mots_o, (float(originale["debut"]),
                                              float(originale["fin"])), (debut, fin))
            alignes = _aligner_tokens(tokens, recales, debut, fin)
            r["mots"] = _forcer_monotonie(alignes, debut, fin)
        from .symboles import etiqueter_mots

        etiqueter_mots(r["mots"])  # les « (souffle) » entre parenthèses deviennent des marqueurs
        sortie.append(r)
    return sortie
