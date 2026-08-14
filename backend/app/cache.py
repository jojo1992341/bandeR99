"""Cache disque des transcriptions : clé = SHA-1(contenu audio) + modèle + langue.

Évite de retranscrire une vidéo déjà vue (réglages style/typo modifiés, re-rendu
direct) : la 2ᵉ exécution est servie instantanément.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .asr import Word

# Version de l'affinage des timestamps : incrémentée quand le post-traitement
# change les horodatages (ex. T50 — prolongation acoustique des syllabes
# tenues) — les entrées de cache antérieures sont alors re-transcrites une fois.
VERSION_AFFINAGE = "t50-2026-08"


def _cache_dir() -> Path:
    env = os.environ.get("RYTHMO_CACHE_DIR")
    base = Path(env) if env else Path(__file__).resolve().parents[1] / "data" / "cache"
    (base / "transcriptions").mkdir(parents=True, exist_ok=True)
    return base


def cle_transcription(chemin_audio: str | Path, model_name: str,
                      language: str | None) -> str:
    hashage = hashlib.sha1()
    with open(chemin_audio, "rb") as f:  # lecture streamée : OK pour gros fichiers
        while bloc := f.read(1024 * 1024):
            hashage.update(bloc)
    hashage.update(f"|{model_name}|{language or 'auto'}|{VERSION_AFFINAGE}".encode())
    return hashage.hexdigest()


def _chemin(cle: str) -> Path:
    return _cache_dir() / "transcriptions" / f"{cle}.json"


def lire_transcription(cle: str) -> tuple[list[Word], str] | None:
    p = _chemin(cle)
    if not p.is_file():
        return None
    try:
        donnees = json.loads(p.read_text(encoding="utf-8"))
        mots = [Word(m["texte"], float(m["debut"]), float(m["fin"]),
                     float(m.get("proba", 0.0))) for m in donnees["mots"]]
        return mots, donnees["langue"]
    except (KeyError, ValueError, json.JSONDecodeError):
        return None


def ecrire_transcription(cle: str, mots: list[Word], langue: str) -> None:
    donnees = {"langue": langue, "mots": [
        {"texte": m.text, "debut": m.start, "fin": m.end, "proba": m.probability}
        for m in mots]}
    cible = _chemin(cle)
    tampon = cible.with_suffix(".json.tmp")  # écriture atomique (anti-coupure)
    tampon.write_text(json.dumps(donnees, ensure_ascii=False), encoding="utf-8")
    tampon.replace(cible)


def obtenir_transcription(chemin_audio: str | Path, model_name: str, language,
                          producteur) -> tuple[list[Word], str]:
    """Sert la transcription depuis le cache, ou la produit puis la met en cache."""
    cle = cle_transcription(chemin_audio, model_name, language)
    en_cache = lire_transcription(cle)
    if en_cache is not None:
        return en_cache
    mots, langue = producteur()
    ecrire_transcription(cle, mots, langue)
    return mots, langue
