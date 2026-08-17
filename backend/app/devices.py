"""Sélection du périphérique de calcul pour l'IA : GPU CUDA si disponible, sinon CPU."""
from __future__ import annotations


def diagnostic_device() -> tuple[str, str | None]:
    """Périphérique choisi **et** raison du repli CPU.

    - ``("cuda", None)`` : GPU CUDA exploitable ;
    - ``("cpu", "torch_absent")`` : torch n'est pas installé ;
    - ``("cpu", "torch_sans_cuda")`` : torch installé sans CUDA exploitable
      (build CPU, pilote manquant, CUDA cassé).

    Ne lève jamais.
    """
    try:
        import torch
    except Exception:  # noqa: BLE001 - torch absent
        return "cpu", "torch_absent"
    try:
        if torch.cuda.is_available():
            return "cuda", None
    except Exception:  # noqa: BLE001 - CUDA inaccessible/cassé
        pass
    return "cpu", "torch_sans_cuda"


def choose_device() -> str:
    """Retourne ``"cuda"`` si un GPU CUDA est exploitable, sinon ``"cpu"``.

    Ne lève jamais d'exception : toute indisponibilité (torch absent, pilote
    manquant, CUDA masqué) aboutit au repli CPU.
    """
    return diagnostic_device()[0]


def compute_type(device: str | None = None) -> str:
    """Type de calcul optimal pour faster-whisper/WhisperX selon le périphérique."""
    device = device or choose_device()
    return "float16" if device == "cuda" else "int8"


def verifier_installation(gpu_present: bool, device: str) -> tuple[bool, str]:
    """Cohérence matériel GPU ↔ build torch : ``(ok, message)``.

    ``ok`` est False quand un GPU NVIDIA est présent mais le périphérique choisi
    n'est pas ``"cuda"`` — signe d'un torch build CPU installé par erreur (le
    bug « GPU non détecté »). Le message indique la commande de correction.
    """
    if gpu_present and device != "cuda":
        return (False,
                "GPU NVIDIA détecté mais torch n'utilise pas CUDA. "
                "Réinstallez torch en build CUDA :\n"
                "  .venv/Scripts/python -m pip install torch==2.8.0 "
                "torchaudio==2.8.0 --index-url "
                "https://download.pytorch.org/whl/cu126 --upgrade")
    return (True, "OK")


def charger_sur_device(fabrique, device: str | None = None) -> tuple[object, str]:
    """Charge un modèle sur le périphérique choisi, avec repli CPU si CUDA échoue.

    ``fabrique(device)`` reçoit ``"cuda"`` ou ``"cpu"`` et doit retourner le
    modèle chargé sur ce périphérique. Si la tentative CUDA lève (OOM, pilote
    manquant, modèle trop gros pour la VRAM…), on recharge sur CPU. Retourne
    ``(modèle, device_réel)``. Un échec du repli CPU, lui, est propagé.
    """
    device = device or choose_device()
    if device == "cuda":
        try:
            return fabrique("cuda"), "cuda"
        except Exception:  # noqa: BLE001 - repli CPU volontairement très large
            pass
    return fabrique("cpu"), "cpu"
