"""Transcription cloud via API (T76–T79, session 18).

En complément du local (faster-whisper/WhisperX), le pipeline peut transcrire
via l'API OpenAI Whisper — mots horodatés inclus. Trois modes (paramètre
``asr`` du job) :

- ``local`` (défaut) : 100 % local, rien ne quitte la machine (identité du
  produit) ;
- ``cloud``  : strict — toute erreur cloud (clé absente, réseau, quota) est
  visible (``RythmoError E007``) ;
- ``auto``   : cloud d'abord, **repli automatique** sur le local en cas
  d'échec (source enregistrée ``repli_local``).

La clé vient de l'environnement (``RYTHMO_OPENAI_KEY``) ou des options du job
(``asr_cle``). Le post-traitement local (prolongation des syllabes tenues T50,
validation) s'applique aussi aux mots cloud : la qualité du défilement reste
identique quel que soit le moteur.
"""
from __future__ import annotations

import os
from pathlib import Path

import httpx

from .asr import (Word, duree_wav, enveloppe_rms, prolonger_fins_sur_audio,
                  validate_words)
from .errors import RythmoError

URL_DEFAUT = "https://api.openai.com/v1/audio/transcriptions"
MODELE_DEFAUT = "whisper-1"
ENV_CLE = "RYTHMO_OPENAI_KEY"
_DELAI_S = 120.0  # une vidéo longue peut prendre du temps côté serveur


def cle_cloud() -> str | None:
    """Clé API depuis l'environnement (jamais de clé en dur dans le code)."""
    return os.environ.get(ENV_CLE) or None


def _parser_mots(corps: dict) -> tuple[list[Word], str]:
    mots: list[Word] = []
    for segment in corps.get("segments", []):
        for w in segment.get("words", []):
            if w.get("start") is None or w.get("end") is None:
                continue
            mots.append(Word(text=str(w.get("word", "")).strip(),
                             start=float(w["start"]), end=float(w["end"]),
                             probability=float(w.get("probability") or 0.0)))
    return mots, str(corps.get("language") or "")


def transcrire_cloud(chemin_wav: str | Path, language: str | None = None,
                     cle: str | None = None, endpoint: str | None = None,
                     modele: str = MODELE_DEFAUT,
                     transport: httpx.BaseTransport | None = None
                     ) -> tuple[list[Word], str]:
    """Transcrit un WAV via l'API cloud : retourne (mots horodatés, langue).

    ``transport`` (httpx.MockTransport en test) injecte le transport réseau ;
    la clé est obligatoire (``RythmoError E007`` sinon), tout statut ≠ 200
    lève ``RythmoError E007`` avec le détail serveur. Les mots cloud passent
    par le même post-traitement que le local (T50 + validation).
    """
    cle = cle or cle_cloud()
    if not cle:
        raise RythmoError("E007", "Aucune clé API cloud : renseignez la variable "
                                  f"d'environnement {ENV_CLE} ou l'option asr_cle.")
    chemin_wav = Path(chemin_wav)
    donnes_wav = chemin_wav.read_bytes()
    donnees = {"model": modele, "response_format": "json",
               "timestamp_granularities[]": "word"}
    if language:
        donnees["language"] = language
    try:
        with httpx.Client(transport=transport, timeout=_DELAI_S) as client:
            rep = client.post(endpoint or URL_DEFAUT,
                              headers={"Authorization": f"Bearer {cle}"},
                              files={"file": (chemin_wav.name, donnes_wav,
                                              "audio/wav")},
                              data=donnees)
    except httpx.HTTPError as exc:
        raise RythmoError("E007", f"Échec de la transcription cloud : {exc}") from exc
    if rep.status_code != 200:
        detail = ""
        try:
            detail = rep.json().get("error", {}).get("message", "")
        except ValueError:
            pass
        raise RythmoError("E007", f"Transcription cloud refusée (HTTP "
                                  f"{rep.status_code}) : {detail or rep.text[:200]}")
    try:
        corps = rep.json()
    except ValueError as exc:
        raise RythmoError("E007", "Réponse cloud illisible (JSON attendu).") from exc
    mots, langue = _parser_mots(corps)
    if not mots:
        raise RythmoError("E007", "Le cloud n'a renvoyé aucun mot horodaté "
                                  "(timestamp_granularities[].word requis).")
    # même post-traitement que le local : syllabes tenues prolongées, bornes sûres
    duree = duree_wav(chemin_wav)
    mots = prolonger_fins_sur_audio(mots, enveloppe_rms(chemin_wav))
    return validate_words(mots, duree), langue or language or ""


def transcrire_avec_repli(chemin_wav: str | Path, language: str | None,
                          mode: str, fonction_cloud, fonction_locale,
                          cle: str | None = None) -> tuple[list[Word], str, str]:
    """Dispatche selon ``mode`` : retourne ``(mots, langue, source)``.

    ``source`` ∈ {``local``, ``cloud``, ``repli_local``} — enregistrée dans le
    job pour l'audit. ``auto`` sans clé → local direct (le cloud ne démarre
    même pas) ; ``cloud`` sans clé ou en échec → ``RythmoError E007``.
    """
    if mode in ("cloud", "auto") and (cle or cle_cloud()):
        try:
            mots, langue = fonction_cloud()
            return mots, langue, "cloud"
        except RythmoError:
            if mode == "cloud":
                raise
            mots, langue = fonction_locale()
            return mots, langue, "repli_local"
    if mode == "cloud":
        raise RythmoError("E007", "Aucune clé API cloud : renseignez la variable "
                                  f"d'environnement {ENV_CLE} ou l'option asr_cle.")
    mots, langue = fonction_locale()
    return mots, langue, "local"
