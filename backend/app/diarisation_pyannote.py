"""Diarisation de niveau studio via pyannote.audio (T112–T113, session 29).

``pyannote/speaker-diarization-3.1`` est un vrai pipeline de diarisation
(segmentation VAD + embeddings locuteurs + clustering) : il peut séparer des
locuteurs de MÊME tessiture que la hauteur (T56) et Resemblyzer (T109) ne
distinguent pas — à condition que l'audio contienne des tours séparés.

Les modèles étant « gated » (licence à accepter sur
https://huggingface.co/pyannote/speaker-diarization-3.1 puis token), le token
est lu depuis ``RYTHMO_HF_TOKEN``, ``HF_TOKEN`` ou ``HUGGING_FACE_HUB_TOKEN``.
Sans token — ou à la moindre défaillance (téléchargement, exécution) — cette
fonction retourne ``None`` et le pipeline retombe sur Resemblyzer puis sur la
hauteur : jamais d'exception, l'application fonctionne sans.

Les étiquettes brutes (``SPEAKER_00``…) sont renumérotées par ordre de
première apparition (voix 0, 1, …) pour rester stables et lisibles, et chaque
réplique prend le locuteur qui couvre le plus son intervalle (dominance).
"""
from __future__ import annotations

import wave
from pathlib import Path

_TOKEN_VARS = ("RYTHMO_HF_TOKEN", "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN")
# Repli « fichier .env » : racine du projet (où scripts/lancer.bat démarre
# uvicorn) puis répertoire courant — l'app serveur trouve le token quel que
# soit le lanceur, sans config Windows.
_FICHIERS_ENV = (
    Path(__file__).resolve().parents[2] / ".env",
    Path(".env"),
)
_PIPELINE = None  # pipeline pyannote chargé une seule fois (singleton)


def _token() -> str | None:
    """Premier token Hugging Face trouvé, sinon None.

    Ordre : variables d'environnement (``RYTHMO_HF_TOKEN``, ``HF_TOKEN``,
    ``HUGGING_FACE_HUB_TOKEN``) puis fichier ``.env`` (racine du projet ou
    répertoire courant). Lignes vides/commentaires (``#``) ignorées ; valeur
    entre guillemets acceptée.
    """
    import os

    for var in _TOKEN_VARS:
        val = os.environ.get(var)
        if val:
            return val
    for chemin in _FICHIERS_ENV:
        try:
            lignes = chemin.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for ligne in lignes:
            ligne = ligne.strip()
            if not ligne or ligne.startswith("#"):
                continue
            cle, sep, valeur = ligne.partition("=")
            if sep and cle.strip() in _TOKEN_VARS and valeur.strip():
                return valeur.strip().strip('\"').strip("'")
    return None


def charger_pipeline():
    """Pipeline pyannote diarisation 3.1 (chargé une fois). ``None`` sans token
    ou si le chargement échoue (jamais d'exception)."""
    global _PIPELINE
    if _PIPELINE is not None:
        return _PIPELINE
    token = _token()
    if not token:
        return None
    try:
        from pyannote.audio import Pipeline

        _PIPELINE = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1", token=token)
        _PIPELINE.to(__import__("torch").device("cpu"))
    except Exception:
        _PIPELINE = None
    return _PIPELINE


def _assigner_par_dominance(cues, tours) -> list[int] | None:
    """Étiquette de chaque réplique = locuteur dominant sur son intervalle.

    ``tours`` : liste de ``(debut, fin, locuteur)`` (ce que pyannote renvoie).
    Les locuteurs sont renumérotés par ordre de première apparition (voix 0,
    1, …). Une réplique sans aucun chevauchement prend le locuteur du tour le
    plus proche dans le temps ; sans tour du tout → ``None`` (repli).
    """
    if not tours:
        return None
    ordre: dict[str, int] = {}
    for debut, fin, loc in tours:
        if loc not in ordre:
            ordre[loc] = len(ordre)
    labels = []
    for cue in cues:
        debut, fin = float(cue.start), float(cue.end)
        meilleur, meilleure_duree = None, -1.0
        for t_debut, t_fin, loc in tours:
            chevauchement = min(fin, t_fin) - max(debut, t_debut)
            if chevauchement > meilleure_duree:
                meilleure_duree, meilleur = chevauchement, loc
        if meilleur is not None and meilleure_duree > 0:
            labels.append(ordre[meilleur])
        else:  # zone sans locuteur : le tour dont le centre est le plus proche
            centre = (debut + fin) / 2.0
            plus_proche = min(tours,
                              key=lambda t: abs((t[0] + t[1]) / 2.0 - centre))
            labels.append(ordre[plus_proche[2]])
    return labels


def _lire_waveform(chemin_wav):
    """(waveform torch [1, n], sample_rate) lus depuis le WAV 16 kHz mono."""
    import numpy as np
    import torch

    with wave.open(str(chemin_wav)) as w:
        rate = w.getframerate()
        brut = w.readframes(w.getnframes())
    buf = np.frombuffer(brut, dtype=np.int16).astype(np.float32) / 32768.0
    return torch.from_numpy(buf).unsqueeze(0), rate


def obtenir_tours_pyannote(chemin_wav) -> list[tuple[float, float, str]] | None:
    """Exécute pyannote une fois et renvoie ses tours de parole normalisés.

    Cette primitive évite de réduire trop tôt une diarisation studio à une
    étiquette par réplique : le pipeline peut ensuite couper une réplique au
    changement de locuteur, même quand la pause est inférieure à 600 ms.
    """
    try:
        pipeline = charger_pipeline()
        if pipeline is None:
            return None
        try:
            diarization = pipeline(str(chemin_wav))
        except Exception:
            waveform, rate = _lire_waveform(chemin_wav)
            diarization = pipeline({"waveform": waveform, "sample_rate": rate})
        # pyannote.audio >= 4.0 renvoie un ``DiarizeOutput`` (l'Annotation est
        # dans le champ ``speaker_diarization``) au lieu de l'Annotation directe
        # de pyannote 3.1.
        if not hasattr(diarization, "itertracks"):
            diarization = getattr(diarization, "speaker_diarization", None)
        if diarization is None:
            return None
        return [(float(turn.start), float(turn.end), str(loc))
                for turn, _, loc in diarization.itertracks(yield_label=True)]
    except Exception:
        return None  # toute défaillance → repli, jamais d'exception


def _renumeroter_tours(tours: list[tuple[float, float, str]]) -> dict[str, int]:
    """Renumérote les locuteurs par première apparition temporelle."""
    ordre: dict[str, int] = {}
    for _, _, loc in sorted(tours, key=lambda t: (t[0], t[1], t[2])):
        if loc not in ordre:
            ordre[loc] = len(ordre)
    return ordre


def etiqueter_mots_pyannote(mots, tours: list[tuple[float, float, str]] | None
                            ) -> list[int] | None:
    """Attribue chaque mot au tour qui le recouvre le plus.

    Contrairement à l'ancienne dominance par réplique, cette granularité permet
    à ``build_cues`` de couper un cue quand le locuteur change. Les tours
    simultanés restent déterministes : le plus grand recouvrement gagne, sans
    inventer une troisième voix.
    """
    if not mots:
        return []
    if not tours:
        return None
    ordre = _renumeroter_tours(tours)
    labels: list[int] = []
    for mot in mots:
        meilleur_loc, meilleur = None, 0.0
        for debut, fin, loc in tours:
            recouvrement = min(float(mot.end), fin) - max(float(mot.start), debut)
            if recouvrement > meilleur:
                meilleur, meilleur_loc = recouvrement, loc
        if meilleur_loc is None:
            centre = (float(mot.start) + float(mot.end)) / 2.0
            plus_proche = min(tours,
                              key=lambda t: abs((t[0] + t[1]) / 2.0 - centre))
            meilleur_loc = plus_proche[2]
        labels.append(ordre[meilleur_loc])
    return labels


def diariser_repliques_pyannote(cues, chemin_wav) -> list[int] | None:
    """Étiquettes de personnage par réplique via pyannote (vrai modèle).

    Retourne ``None`` si le pipeline est indisponible (token absent, modèle
    non téléchargé) ou si l'exécution échoue — le pipeline retombe alors sur
    Resemblyzer (T109) puis la hauteur (T56). Jamais d'exception.
    """
    if not cues:
        return []
    tours = obtenir_tours_pyannote(chemin_wav)
    return _assigner_par_dominance(cues, tours) if tours else None
