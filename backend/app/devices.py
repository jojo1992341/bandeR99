"""Sélection du périphérique de calcul pour l'IA : GPU CUDA si disponible, sinon CPU."""
from __future__ import annotations


def choose_device() -> str:
    """Retourne ``"cuda"`` si un GPU CUDA est exploitable, sinon ``"cpu"``.

    Ne lève jamais d'exception : toute indisponibilité (torch absent, pilote
    manquant, CUDA masqué) aboutit au repli CPU.
    """
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:  # noqa: BLE001 - repli CPU volontairement très large
        return "cpu"


def compute_type(device: str | None = None) -> str:
    """Type de calcul optimal pour faster-whisper/WhisperX selon le périphérique."""
    device = device or choose_device()
    return "float16" if device == "cuda" else "int8"
