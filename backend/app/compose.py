"""Assemblage final : vidéo source + bande rythmo empilée dessous (ffmpeg, flux brut).

Les frames de bande sont rendues à la volée (Pillow) et injectées dans ffmpeg via
stdin (rawvideo RGB24) : pas de PNG temporaires, mémoire constante.
"""
from __future__ import annotations

import math
import subprocess
from pathlib import Path

import numpy as np

from .cues import Cue
from .errors import RythmoError
from .ffmpeg_tools import get_ffmpeg_path
from .ingest import probe_video
from .render import (StyleBande, get_police, preparer_repliques, render_band_frame,
                     x_curseur)


def compose_final(chemin_video: str | Path, cues: list[Cue], cible: str | Path,
                  style: StyleBande | None = None, registrer_processus=None) -> Path:
    """Produit le MP4 final : vidéo + bande rythmo dessous, audio conservé.

    Dimensions : ``W × (H + hauteur_bande)`` ; fps et durée conservés.
    """
    style = style or StyleBande()
    info = probe_video(chemin_video)
    # dimensions D'AFFICHAGE : la rotation est « cuite » explicitement avant l'empilement
    largeur, h_bande, fps = info.largeur_affichee, style.hauteur_bande, info.fps
    fps_exact = info.fps_texte if info.fps_texte not in ("0/1", "") else f"{fps:.6f}"
    curseur_x = x_curseur(style, largeur)
    police = get_police(style.taille_police)
    repliques = preparer_repliques(cues, police, largeur, curseur_x, style)

    n_frames = max(1, math.ceil(info.duration * fps))
    cible = Path(cible)
    cible.parent.mkdir(parents=True, exist_ok=True)

    # la rotation d'affichage est appliquée automatiquement par ffmpeg (autorotate)
    # avant le graphe de filtres : la bande dimensionnée en dims d'affichage suffit.
    cmd = [
        get_ffmpeg_path(), "-y", "-v", "error",
        "-i", str(chemin_video),
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{largeur}x{h_bande}", "-r", fps_exact,
        "-i", "-",  # frames de bande sur stdin
        "-filter_complex",
        f"[0:v]fps={fps_exact}[v0];[v0][1:v]vstack=inputs=2[v]",
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", "-preset", "veryfast",
        "-c:a", "aac",
        str(cible),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    if registrer_processus is not None:
        registrer_processus(proc)  # permet l'annulation (kill) par l'API
    try:
        for i in range(n_frames):
            image = render_band_frame(i / fps, repliques, largeur, style)
            proc.stdin.write(np.asarray(image, dtype=np.uint8).tobytes())
        proc.stdin.close()
        _, err = proc.communicate(timeout=900)
    except BrokenPipeError:
        _, err = proc.communicate(timeout=60)
        raise RythmoError("E001", "ffmpeg a interrompu le rendu bande : "
                                  + err.decode("utf-8", "replace")[:300])
    finally:
        if registrer_processus is not None:
            registrer_processus(None)
    if proc.returncode != 0:
        raise RythmoError("E001", "Échec de l'assemblage final : "
                                  + err.decode("utf-8", "replace")[:300])
    return cible
