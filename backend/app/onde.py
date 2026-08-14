"""Pics d'onde serveur (T85–T88, session 20) : l'aperçu des vidéos longues.

Sur une vidéo de 90 minutes, le WAV 16 kHz pèse ≈ 345 Mo : le navigateur ne
doit jamais le recevoir en entier pour dessiner l'aperçu. ``extraire_pics``
calcule côté serveur les **min/max par colonne** d'une fenêtre, en lisant le
fichier par blocs (mémoire bornée : un bloc + deux accumulateurs par colonne,
quelle que soit la durée). Le front ne reçoit que quelques milliers de paires
de flottants.
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

from .errors import RythmoError

_BLOC = 65536  # échantillons par lecture (512 Ko en float64) — borne mémoire


def extraire_pics(chemin_wav: str | Path, debut: float | None = None,
                  fin: float | None = None,
                  colonnes: int = 1600) -> tuple[list[tuple[float, float]], int, float]:
    """Min/max normalisés [-1, 1] par colonne sur ``[debut, fin]`` (s).

    Retourne ``(pics, rate, duree)``. Bornes clampées à la durée du fichier
    (``None`` = borne) ; fenêtre vide/inversée → ``RythmoError E006``. Le
    fichier est lu par blocs séquentiels : la mémoire ne dépend que de
    ``colonnes``, jamais de la durée.
    """
    colonnes = max(1, int(colonnes))
    with wave.open(str(chemin_wav)) as w:
        rate = w.getframerate()
        duree = w.getnframes() / float(rate)
        d0 = 0.0 if debut is None else min(max(float(debut), 0.0), duree)
        d1 = duree if fin is None else min(max(float(fin), 0.0), duree)
        if d1 <= d0:
            raise RythmoError("E006", f"Fenêtre d'onde invalide : début "
                                      f"{d0:.3f} s ≥ fin {d1:.3f} s.")
        i0, i1 = int(d0 * rate), int(d1 * rate)
        largeur = max(i1 - i0, 1)
        mn = np.full(colonnes, 32767.0, dtype=np.float64)
        mx = np.full(colonnes, -32768.0, dtype=np.float64)
        w.setpos(i0)
        restant = i1 - i0
        while restant > 0:
            n = min(restant, _BLOC)
            brut = w.readframes(n)
            if not brut:
                break
            bloc = np.frombuffer(brut, dtype=np.int16).astype(np.float64)
            base = i1 - restant  # index absolu du premier échantillon du bloc
            cols = ((base + np.arange(len(bloc)) - i0) * colonnes) // largeur
            cols = np.minimum(cols, colonnes - 1)
            np.minimum.at(mn, cols, bloc)
            np.maximum.at(mx, cols, bloc)
            restant -= len(bloc)
    pics = [(float(mn[c]) / 32768.0, float(mx[c]) / 32768.0)
            for c in range(colonnes)]
    return pics, rate, duree
