"""Ingestion vidéo : métadonnées (ffprobe) et extraction audio 16 kHz mono (ffmpeg)."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import RythmoError
from .ffmpeg_tools import get_ffmpeg_path, get_ffprobe_path


@dataclass(frozen=True)
class VideoInfo:
    """Métadonnées utiles d'une vidéo source."""

    duration: float  # secondes
    fps: float  # images/seconde (moyenne)
    width: int
    height: int
    has_audio: bool
    rotation: float = 0.0  # degrés (métadonnée d'affichage, ex. smartphone 90/270)
    fps_texte: str = "0/1"   # débit exact type ffprobe (« 24000/1001 ») pour -r/fps=

    @property
    def largeur_affichee(self) -> int:
        """Largeur telle qu'affichée (rotation appliquée)."""
        return self.height if int(abs(self.rotation)) % 180 == 90 else self.width

    @property
    def hauteur_affichee(self) -> int:
        return self.width if int(abs(self.rotation)) % 180 == 90 else self.height


def _fraction_vers_float(texte: str) -> float:
    """Convertit une fraction ffprobe (``"25/1"``) en float ; 0.0 si indéterminé."""
    try:
        num, den = texte.split("/")
        num_f, den_f = float(num), float(den)
        return num_f / den_f if den_f else 0.0
    except (ValueError, AttributeError):
        return 0.0


def probe_video(chemin: str | Path) -> VideoInfo:
    """Retourne les métadonnées de la vidéo ou lève ``RythmoError('E001')``."""
    chemin = Path(chemin)
    if not chemin.is_file():
        raise RythmoError("E001", f"Fichier introuvable : {chemin}")

    cmd = [
        get_ffprobe_path(),
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(chemin),
    ]
    res = subprocess.run(cmd, capture_output=True, timeout=120)
    if res.returncode != 0:
        raise RythmoError(
            "E001",
            "Fichier vidéo illisible ou corrompu : " + res.stderr.decode("utf-8", "replace")[:300],
        )
    try:
        donnees = json.loads(res.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise RythmoError("E001", "Analyse ffprobe impossible (JSON invalide).") from exc

    flux = donnees.get("streams", [])
    flux_video = next((s for s in flux if s.get("codec_type") == "video"), None)
    if flux_video is None:
        raise RythmoError("E001", "Aucun flux vidéo détecté dans le fichier.")

    try:
        duree = float(donnees.get("format", {}).get("duration", 0.0))
    except (TypeError, ValueError):
        duree = 0.0
    if duree <= 0:
        try:
            duree = float(flux_video.get("duration", 0.0))
        except (TypeError, ValueError):
            duree = 0.0

    fps = _fraction_vers_float(flux_video.get("avg_frame_rate", "0/1"))

    rotation = 0.0  # métadonnée d'affichage (ex. vidéo smartphone tournée)
    for donnee in flux_video.get("side_data_list", []):
        if "rotation" in donnee:
            try:
                rotation = float(donnee["rotation"])
            except (TypeError, ValueError):
                rotation = 0.0
    if not rotation:
        try:  # variante : tag rotate
            rotation = float(flux_video.get("tags", {}).get("rotate", 0) or 0)
        except (TypeError, ValueError):
            rotation = 0.0

    info = VideoInfo(
        duration=duree,
        fps=fps,
        width=int(flux_video.get("width", 0)),
        height=int(flux_video.get("height", 0)),
        has_audio=any(s.get("codec_type") == "audio" for s in flux),
        rotation=rotation,
        fps_texte=flux_video.get("avg_frame_rate", "0/1"),
    )
    if info.duration <= 0 or info.fps <= 0 or info.width <= 0 or info.height <= 0:
        raise RythmoError("E001", "Métadonnées vidéo incomplètes (durée/fps/dimensions).")
    return info


def extract_audio(chemin_video: str | Path, cible_wav: str | Path) -> Path:
    """Extrait l'audio en WAV 16 kHz mono PCM s16 (format attendu par l'ASR).

    Lève ``RythmoError('E002')`` si la vidéo n'a pas de piste audio.
    """
    info = probe_video(chemin_video)
    cible_wav = Path(cible_wav)
    cible_wav.parent.mkdir(parents=True, exist_ok=True)
    if not info.has_audio:
        raise RythmoError("E002", "Cette vidéo ne contient aucune piste audio à doubler.")

    cmd = [
        get_ffmpeg_path(),
        "-y", "-v", "error",
        "-i", str(chemin_video),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        str(cible_wav),
    ]
    res = subprocess.run(cmd, capture_output=True, timeout=600)
    if res.returncode != 0:
        raise RythmoError(
            "E001",
            "Extraction audio impossible : " + res.stderr.decode("utf-8", "replace")[:300],
        )
    return cible_wav
