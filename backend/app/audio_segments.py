"""Écoute des segments (T67–T70, session 16) : découpe d'une fenêtre audio.

Le comédien cale « à l'oreille » : cliquer une réplique ou un mot doit jouer
le son correspondant. Pour rester léger même sur une vidéo de 90 minutes
(≈ 345 Mo en WAV 16 kHz), le serveur découpe le WAV **à la demande** : le
navigateur ne reçoit jamais le fichier entier, seulement la tranche écoutée.
La lecture est streamée (``wave.setpos``), la mémoire reste bornée.
"""
from __future__ import annotations

import io
import wave
from pathlib import Path

from .errors import RythmoError


def decouper_segment(chemin_wav: str | Path, debut: float | None,
                     fin: float | None = None) -> tuple[bytes, int]:
    """Extrait la fenêtre ``[debut, fin]`` (s) d'un WAV PCM 16 bits mono.

    Retourne ``(WAV complet de la tranche, taux d'échantillonnage)``. Bornes
    clampées à la durée du fichier (``debut`` négatif → 0, ``fin`` au-delà →
    durée) ; ``None`` = borne du fichier. Une fenêtre vide ou inversée
    (``fin <= debut``) lève ``RythmoError(\"E006\")``.
    """
    with wave.open(str(chemin_wav)) as w:
        rate = w.getframerate()
        duree = w.getnframes() / float(rate)
        d0 = 0.0 if debut is None else min(max(float(debut), 0.0), duree)
        d1 = duree if fin is None else min(max(float(fin), 0.0), duree)
        if d1 <= d0:
            raise RythmoError("E006",
                              f"Fenêtre audio invalide : début {d0:.3f} s ≥ fin "
                              f"{d1:.3f} s (durée du fichier {duree:.3f} s).")
        i0, i1 = int(d0 * rate), int(d1 * rate)
        w.setpos(i0)
        brutes = w.readframes(i1 - i0)

    tampon = io.BytesIO()
    with wave.open(tampon, "wb") as sortie:
        sortie.setnchannels(1)
        sortie.setsampwidth(2)
        sortie.setframerate(rate)
        sortie.writeframes(brutes)
    return tampon.getvalue(), rate
