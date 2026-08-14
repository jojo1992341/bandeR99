"""Suggestions de correction en français (T71–T75, session 17).

L'ASR locale (whisper) produit des artefacts typiques en français. On propose
des corrections **granulaires** — chaque suggestion porte ses décalages
caractères dans le texte d'origine — pour que le front les applique d'un clic,
une à une, puis re-calcule les suivantes sur le texte corrigé.

Règles (toutes sans ambiguïté, aucune n'invente du sens) :
- **apostrophe** : en français, une apostrophe n'est jamais suivie d'un espace
  (« c' est » → « c'est », « l' heure » → « l'heure ») ;
- **majuscule** : une réplique commence par une majuscule ;
- **ponctuation** : un point final manquant (phrase ni terminée, ni ponctuée) ;
- **orthographe** : coquilles fréquentes de l'ASR (dictionnaire restreint de
  mots sans ambiguïté : « bonjours », « voila », « tres »…).

Les suggestions sont triées par position et conçues pour ne pas se chevaucher ;
``appliquer_suggestions`` applique la liste en partant de la fin (les décalages
du début du texte restent valides) — la correction complète tient en un clic.
"""
from __future__ import annotations

import re

# Coquilles ASR françaises sans ambiguïté (correction sûre hors contexte)
DICTIONNAIRE: dict[str, str] = {
    "bonjours": "bonjour",
    "voila": "voilà",
    "tres": "très",
    "apres": "après",
    "desole": "désolé",
    "desolee": "désolée",
    "meme": "même",
    "deja": "déjà",
    "ete": "été",
    "peutetre": "peut-être",
    "parceque": "parce que",
    "beaucoups": "beaucoup",
}

_APOSTROPHE = re.compile(r"(\w)' (\w+)")     # « c' est »
_APOSTROPHE_INV = re.compile(r"(\w+) '(\w+)")  # « c 'est »
_MOT = re.compile(r"\w+", re.UNICODE)
_PONCTUATION_FINALE = ".!?…"
_FERMETURES = "»)\"'"


def _sugg_majuscule(texte: str, suggestions: list[dict]) -> None:
    premiere = re.search(r"[a-zà-öø-ÿ]", texte)  # minuscule accentuée comprise
    if premiere and premiere.start() == 0:
        debut = 0
        suggestions.append({"type": "majuscule", "debut": debut, "fin": debut + 1,
                            "avant": texte[debut], "apres": texte[debut].upper(),
                            "message": "Majuscule en début de réplique"})


def _sugg_ponctuation(texte: str, suggestions: list[dict]) -> None:
    if not texte:
        return
    dernier = texte[-1]
    if dernier.isalnum() or dernier in _FERMETURES:
        n = len(texte)
        suggestions.append({"type": "ponctuation", "debut": n, "fin": n,
                            "avant": "", "apres": ".",
                            "message": "Point final manquant"})


def _sugg_apostrophes(texte: str, suggestions: list[dict]) -> None:
    vus: set[tuple[int, int]] = set()
    for motif in (_APOSTROPHE, _APOSTROPHE_INV):
        for m in motif.finditer(texte):
            zone = (m.start(), m.end())
            if any(not (zone[1] <= z[0] or z[1] <= zone[0]) for z in vus):
                continue  # déjà couvert par une autre règle
            vus.add(zone)
            avant = m.group(0)
            apres = avant.replace("' ", "'").replace(" '", "'")
            suggestions.append({"type": "apostrophe", "debut": m.start(),
                                "fin": m.end(), "avant": avant, "apres": apres,
                                "message": f"Coller l'apostrophe : « {apres} »"})


def _sugg_dictionnaire(texte: str, suggestions: list[dict]) -> None:
    for m in _MOT.finditer(texte):
        mot = m.group(0)
        cle = mot.lower()
        if cle in DICTIONNAIRE and not mot.isupper():
            apres = DICTIONNAIRE[cle]
            if mot[0].isupper():
                apres = apres[0].upper() + apres[1:]
            suggestions.append({"type": "orthographe", "debut": m.start(),
                                "fin": m.end(), "avant": mot, "apres": apres,
                                "message": f"Corriger : « {mot} » → « {apres} »"})


def suggerer_replique(texte: str) -> list[dict]:
    """Suggestions de correction pour une réplique (décalages du texte d'origine).

    Chaque suggestion : ``type`` (apostrophe | majuscule | ponctuation |
    orthographe), ``debut``/``fin`` (décalages caractères), ``avant``/``apres``
    (tranche à remplacer et remplacement), ``message`` (FR, affichable tel quel).
    """
    texte = texte or ""
    if not texte.strip():
        return []
    suggestions: list[dict] = []
    _sugg_majuscule(texte, suggestions)
    _sugg_ponctuation(texte, suggestions)
    _sugg_apostrophes(texte, suggestions)
    _sugg_dictionnaire(texte, suggestions)
    return sorted(suggestions, key=lambda s: (s["debut"], s["fin"]))


def appliquer_suggestions(texte: str, suggestions: list[dict]) -> str:
    """Applique toutes les suggestions : la correction complète en un clic.

    Application en partant de la fin : les décalages du début du texte restent
    valides pendant l'application (les suggestions se réfèrent au texte
    d'origine et ne se chevauchent pas).
    """
    resultat = texte or ""
    for s in sorted(suggestions, key=lambda s: (s["debut"], s["fin"]), reverse=True):
        debut, fin, avant, apres = s["debut"], s["fin"], s["avant"], s["apres"]
        if resultat[debut:fin] == avant:
            resultat = resultat[:debut] + apres + resultat[fin:]
    return resultat
