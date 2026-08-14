"""Symboles de respiration entre parenthèses (T80–T84, session 19).

Convention de la bande rythmo pro (Q6) : les indications au comédien
(respirations, pauses, silences…) s'écrivent **entre parenthèses** — « (souffle) »,
« (silence) », « (pause) »… Un mot entièrement entre parenthèses est un
**marqueur** : il n'est pas prononcé, il occupe son propre intervalle sur la
piste (le comédien respire pendant qu'il passe sous le curseur — la bande reste
à vitesse constante, comme le rouleau des studios) et il est rendu
distinctement à l'écran.
"""
from __future__ import annotations

import re

_REGLE = re.compile(r"^\(\s*([^()]{1,60}?)\s*\)$", re.UNICODE)


def est_symbole(texte: str) -> bool:
    """Vrai si ``texte`` est exactement un marqueur entre parenthèses.

    Contenu libre (``(souffle)``, ``(hésitation longue)``…) sans parenthèses
    imbriquées ni texte autour ; casse et espaces internes tolérés.
    """
    m = _REGLE.match((texte or "").strip())
    return bool(m) and bool(m.group(1).strip())


def etiqueter_mots(mots: list[dict]) -> None:
    """Marque en place les mots-marqueurs : ``m["marqueur"] = True``."""
    for m in mots:
        if isinstance(m, dict) and est_symbole(str(m.get("texte", ""))):
            m["marqueur"] = True
