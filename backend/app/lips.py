"""Analyse labiale : ouverture de bouche image par image via MediaPipe FaceLandmarker.

Sert à la synchronisation labiale de la bande rythmo (calage des répliques sur
l'articulation visible). Tout est local : le modèle ``face_landmarker.task``
(~3,6 Mo) est téléchargé une fois depuis le CDN officiel MediaPipe.
"""
from __future__ import annotations

import urllib.request
from dataclasses import dataclass

from pathlib import Path

import numpy as np

from .errors import RythmoError

_MODELE_DEFAUT = Path(__file__).resolve().parent / "models" / "face_landmarker.task"
_URL_MODELE = ("https://storage.googleapis.com/mediapipe-models/"
               "face_landmarker/face_landmarker/float16/1/face_landmarker.task")

# Indices canoniques Face Mesh (478 points)
_HAUT_LEVRE_INT, _BAS_LEVRE_INT = 13, 14
_COIN_G, _COIN_D = 61, 291
_HAUT_LEVRE_EXT, _BAS_LEVRE_EXT = 0, 17


def chemin_modele() -> Path:
    """Chemin du modèle FaceLandmarker, téléchargé au besoin (une seule fois)."""
    if not _MODELE_DEFAUT.exists():
        _MODELE_DEFAUT.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(_URL_MODELE, _MODELE_DEFAUT)
    return _MODELE_DEFAUT


def _creer_landmarker(mode: str = "VIDEO"):
    """Nouvelle instance FaceLandmarker (mode ``VIDEO`` ou ``IMAGE``).

    Une instance VIDEO impose des timestamps strictement croissants : une par
    flux analysé. Le modèle (~3,6 Mo) se recharge en quelques dizaines de ms.
    """
    import mediapipe as mp

    running = mp.tasks.vision.RunningMode[mode]
    options = mp.tasks.vision.FaceLandmarkerOptions(
        base_options=mp.tasks.BaseOptions(model_asset_path=str(chemin_modele())),
        running_mode=running,
        num_faces=1,
    )
    return mp.tasks.vision.FaceLandmarker.create_from_options(options)


def _landmarks_image(bgr: np.ndarray, landmarker=None, timestamp_ms: int = 0):
    """Landmarks normalisés (x,y ∈ [0,1]) d'une frame BGR, ou None si pas de visage."""
    import mediapipe as mp

    rgb = np.ascontiguousarray(bgr[:, :, ::-1])
    image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
    if landmarker is None:
        landmarker = _creer_landmarker("IMAGE")
        try:
            resultat = landmarker.detect(image)
        finally:
            landmarker.close()
    else:
        resultat = landmarker.detect_for_video(image, timestamp_ms)
    if not resultat.face_landmarks:
        return None
    return resultat.face_landmarks[0]


def _px(lm, index: int, largeur: int, hauteur: int) -> tuple[float, float]:
    return lm[index].x * largeur, lm[index].y * hauteur


@dataclass(frozen=True)
class MouthTrack:
    """Série temporelle d'ouverture de bouche (ratio adimensionné ≈ 0.05 fermé, ≥ 0.2 ouvert)."""

    times: np.ndarray
    apertures: np.ndarray


def detect_mouth_track(chemin_video: str | Path, fps_echantillon: int = 12) -> MouthTrack | None:
    """Mesure l'aperture bouche à ``fps_echantillon`` im/s ; None si pas de visage exploitable."""
    import cv2

    cap = cv2.VideoCapture(str(chemin_video))
    if not cap.isOpened():
        raise RythmoError("E001", f"Vidéo illisible : {chemin_video}")
    fps_src = cap.get(cv2.CAP_PROP_FPS) or 25.0
    intervalle = 1.0 / max(float(fps_echantillon), 1.0)  # échantillonnage temporel exact
    landmarker = _creer_landmarker("VIDEO")

    times: list[float] = []
    apertures: list[float] = []
    idx, vus_sans_visage, ts_precedent = 0, 0, -1
    grille_n = 0  # grille fixe n/fps_echantillon (évite la dérive d'arrondi aux frames)
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        t = idx / fps_src
        if t + 1e-9 >= grille_n * intervalle:
            grille_n += 1
            ts_ms = max(int(t * 1000), ts_precedent + 1)
            ts_precedent = ts_ms
            lm = _landmarks_image(frame, landmarker=landmarker, timestamp_ms=ts_ms)
            if lm is None:
                apertures.append(np.nan)
                vus_sans_visage += 1
            else:
                h, w = frame.shape[:2]
                x1, y1 = _px(lm, _HAUT_LEVRE_INT, w, h)
                x2, y2 = _px(lm, _BAS_LEVRE_INT, w, h)
                g = _px(lm, _COIN_G, w, h)
                d = _px(lm, _COIN_D, w, h)
                largeur_bouche = max(np.hypot(d[0] - g[0], d[1] - g[1]), 1e-6)
                apertures.append(float(np.hypot(x2 - x1, y2 - y1) / largeur_bouche))
            times.append(idx / fps_src)
        idx += 1
    cap.release()
    landmarker.close()

    mesures = np.asarray(apertures, dtype=np.float32)
    if len(mesures) == 0 or vus_sans_visage > len(mesures) / 2:
        return None  # majorité sans visage : repli audio pur (T14)
    valides = ~np.isnan(mesures)
    mesures = np.interp(np.arange(len(mesures)), np.flatnonzero(valides), mesures[valides])
    noyau = np.ones(3) / 3.0  # lissage doux
    mesures = np.convolve(mesures, noyau, mode="same")
    return MouthTrack(times=np.asarray(times, dtype=np.float32), apertures=mesures)


def find_speech_onsets(piste: MouthTrack, seuil_relatif: float = 0.35,
                       distance_min_s: float = 0.25) -> list[float]:
    """Instants (s) où la bouche s'ouvre franchement (fronts montants d'aperture).

    Seuil adaptatif (percentiles) + franchissement interpolé entre échantillons ;
    les fronts à moins de ``distance_min_s`` sont fusionnés.
    """
    a = piste.apertures
    if len(a) < 2:
        return []
    p10, p95 = np.percentile(a, [10, 95])
    seuil = p10 + seuil_relatif * (p95 - p10)
    onsets: list[float] = []
    for i in range(1, len(a)):
        if a[i - 1] <= seuil < a[i]:
            frac = (seuil - a[i - 1]) / max(a[i] - a[i - 1], 1e-9)
            t_cross = float(piste.times[i - 1] + frac * (piste.times[i] - piste.times[i - 1]))
            if not onsets or t_cross - onsets[-1] > distance_min_s:
                onsets.append(t_cross)
    return onsets


def align_cues_to_mouth(cues, onsets, decalage_max: float = 0.25):
    """Recale chaque cue sur l'onset bouche le plus proche (|décalage| ≤ borne).

    Le décalage est borné, la durée des répliques est préservée, aucune réplique
    ne démarre avant t=0. Sans onset dans la fenêtre : cue inchangée.
    """
    from .cues import Cue  # import local : évite un cycle lips<->cues

    from .asr import Word

    ajustees = []
    for cue in cues:
        if onsets and cue.words:
            plus_proche = min(onsets, key=lambda o: abs(o - cue.start))
            delta = plus_proche - cue.start
            if abs(delta) <= decalage_max:
                delta = max(delta, -cue.start)  # jamais avant t=0
                mots = [Word(w.text, w.start + delta, w.end + delta, w.probability)
                        for w in cue.words]
                ajustees.append(Cue(words=mots))
                continue
        ajustees.append(cue)
    return ajustees

def mesurer_visage_image(bgr: np.ndarray) -> dict:
    """Géométrie bouche (pixels) d'un visage sur image fixe ; lève si aucun visage."""
    lm = _landmarks_image(bgr)
    if lm is None:
        raise RythmoError("E003", "Aucun visage détecté sur l'image de référence.")
    h, w = bgr.shape[:2]
    g = _px(lm, _COIN_G, w, h)
    d = _px(lm, _COIN_D, w, h)
    _, y_haut = _px(lm, _HAUT_LEVRE_EXT, w, h)
    _, y_bas = _px(lm, _BAS_LEVRE_EXT, w, h)
    _, y_lip = _px(lm, _HAUT_LEVRE_INT, w, h)
    _, y_lb = _px(lm, _BAS_LEVRE_INT, w, h)
    return {
        "cx": (g[0] + d[0]) / 2,
        "y_levres": (y_lip + y_lb) / 2,
        "demi_largeur": float(np.hypot(d[0] - g[0], d[1] - g[1]) / 2) * 0.95,
        "hauteur": max(y_bas - y_haut, 6.0),
    }


def ouvrir_bouche(bgr: np.ndarray, geo: dict, a: float) -> np.ndarray:
    """Rend l'image avec la bouche ouverte à la fraction ``a`` ∈ [0, 1] (jaw-drop)."""
    import cv2

    a = float(np.clip(a, 0.0, 1.0))
    sortie = bgr
    px = a * geo["hauteur"] * 1.6
    if px > 0.5:
        h, w = bgr.shape[:2]
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        sous = np.clip(yy - geo["y_levres"], 0, None)
        fx = np.exp(-((xx - geo["cx"]) / (geo["demi_largeur"] * 1.25)) ** 2)
        fy = np.exp(-(sous / (geo["hauteur"] * 2.2)) ** 2)
        decal = px * fx * fy * (yy >= geo["y_levres"])
        decal_haut = -px * 0.22 * fx * np.exp(-((geo["y_levres"] - yy).clip(0) /
                                                (geo["hauteur"] * 1.1)) ** 2) * (yy < geo["y_levres"])
        carte_y = np.clip(yy + decal + decal_haut, 0, h - 1)
        sortie = cv2.remap(bgr, xx, carte_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
        if px > 2.0:  # cavité buccale sombre quand la bouche est réellement entrouverte
            masque = np.zeros((h, w), dtype=np.uint8)
            centre = (int(geo["cx"]), int(geo["y_levres"] + px * 0.30))
            axes = (int(geo["demi_largeur"] * 0.78), max(int(px * 0.55), 1))
            cv2.ellipse(masque, centre, axes, 0, 0, 360, 255, -1)
            masque = cv2.GaussianBlur(masque, (9, 9), 0)
            teinte = np.full_like(sortie, (58, 18, 34))  # rouge sombre (BGR)
            alpha = (masque.astype(np.float32) / 255.0 * 0.88)[:, :, None]
            sortie = (sortie * (1 - alpha) + teinte * alpha).astype(np.uint8)
    return sortie
