"""Resynchronisation mot-à-mot des répliques sur l'audio (bouton « Resynchroniser »).

Après un import de ``.srt`` (ou tout état sans alignement audio), les mots sont
répartis uniformément sur chaque fenêtre — un simple repli. Ce module re-transcrit
l'audio du job (servi depuis le cache disque quand le même modèle/langue a déjà
été utilisé) puis recolle les timings mot à mot des mots transcrits au texte des
répliques par appariement token (difflib, insensible à la casse/accents/ponctuation)
dans la fenêtre ``[debut, fin]`` de chaque réplique.
"""
from __future__ import annotations

import json
from pathlib import Path

from .asr import (Word, marquer_mots_incertains, transcribe_words,
                  transcrire_fenetre)
from .cache import obtenir_transcription
from .cues_edit import (_aligner_tokens, _distribuer_uniforme, _forcer_monotonie)
from .edition import chemin_params, lire_repliques
from .errors import RythmoError
from .symboles import etiqueter_mots
from .vocabulaire import vocabulaire_du_projet


def _valider_entrees(repliques) -> None:
    """Contrôles structurels légers : pas de blocage sur chevauchement/hors vidéo.

    La resynchronisation est une aide à l'édition : elle doit fonctionner même
    sur un brouillon dont les fenêtres se chevauchent ou dépassent la vidéo
    (le rendu final, lui, refusera ces états — cf. T120).
    """
    if not isinstance(repliques, list) or not repliques:
        raise RythmoError("E005", "La liste de répliques est vide : "
                                  "il faut au moins une réplique à resynchroniser.")
    for i, r in enumerate(repliques, start=1):
        if not isinstance(r, dict):
            raise RythmoError("E005", f"Réplique {i} : objet invalide")
        texte = r.get("texte")
        if not isinstance(texte, str) or not texte.strip():
            raise RythmoError("E005", f"Réplique {i} : texte vide ou invalide")
        debut, fin = r.get("debut"), r.get("fin")
        if (not isinstance(debut, (int, float)) or isinstance(debut, bool)
                or not isinstance(fin, (int, float)) or isinstance(fin, bool)):
            raise RythmoError("E005", f"Réplique {i} : horaires invalides")
        if float(fin) <= float(debut):
            raise RythmoError("E005", f"Réplique {i} : le début doit être "
                                      f"avant la fin")


def _aligner_repliques(repliques: list[dict], mots_transcrits: list[Word]) -> list[dict]:
    """Attache des timings mot à mot à chaque réplique depuis les mots transcrits.

    Pour chaque réplique, seuls les mots transcrits débutant dans sa fenêtre
    ``[debut, fin]`` sont candidats : leur texte sert de référence temporelle et
    l'appariement token (difflib) porte les timings sur le texte de la réplique
    (mot remplacé → part égale de l'union, mot inséré → trou entre voisins).
    Sans mot candidat, repli : distribution uniforme (comportement actuel).
    """
    sortie: list[dict] = []
    for r in repliques:
        debut, fin = float(r["debut"]), float(r["fin"])
        tokens = str(r.get("texte", "")).split()
        fenetre = [w for w in mots_transcrits if debut <= w.start < fin]
        resultat = dict(r)
        if not tokens:
            resultat["mots"] = []
        elif not fenetre:
            resultat["mots"] = _distribuer_uniforme(tokens, debut, fin)
        else:
            recales = [{"texte": w.text, "debut": w.start, "fin": w.end,
                        **({"incertain": True} if w.incertain else {})}
                       for w in fenetre]
            alignes = _aligner_tokens(tokens, recales, debut, fin)
            resultat["mots"] = _forcer_monotonie(alignes, debut, fin)
        etiqueter_mots(resultat["mots"])  # les « (souffle) » redeviennent des marqueurs
        sortie.append(resultat)
    return sortie


def _config_transcription(job_dir: Path) -> tuple[str | None, str, bool, list[str]]:
    """Langue, modèle, affinage WhisperX et vocabulaire lus depuis le job."""
    try:
        cfg = json.loads(chemin_params(job_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise RythmoError("E005", "Paramètres du job illisibles : "
                                  "relancez l'analyse de la vidéo.") from None
    params = cfg.get("params", {}) if isinstance(cfg, dict) else {}
    payload = lire_repliques(job_dir)
    langue = payload.get("langue") or params.get("langue")
    modele = params.get("modele", "medium")
    affiner = bool(params.get("aligner_whisperx", True))
    vocabulaire = vocabulaire_du_projet(job_dir, params.get("vocabulaire"))
    return langue, modele, affiner, vocabulaire


def synchroniser_repliques(job_dir: str | Path, repliques: list[dict]) -> list[dict]:
    """Re-transcrit l'audio du job puis réaligne le texte des répliques dessus.

    Retourne les répliques complétées d'un champ ``mots`` horodaté. Ne persiste
    rien et ne relance pas le rendu : le front affiche la nouvelle timeline et
    l'utilisateur valide ensuite (PUT) s'il est satisfait.
    """
    job_dir = Path(job_dir)
    _valider_entrees(repliques)

    wav = job_dir / "audio_16k.wav"
    if not wav.is_file():
        raise RythmoError("E002", "Audio indisponible pour la synchronisation "
                                  "(le fichier audio_16k.wav est absent).")

    langue, modele, affiner, vocabulaire = _config_transcription(job_dir)

    def _transcrire():
        return transcribe_words(wav, language=langue or None, model_name=modele,
                                affiner=affiner, vocabulaire=vocabulaire)

    try:
        mots, _ = obtenir_transcription(wav, modele, langue, _transcrire,
                                        vocabulaire=vocabulaire)
    except RythmoError:
        raise
    except Exception as exc:  # noqa: BLE001 — modèle absent, échec ASR…
        raise RythmoError("E999", "Synchronisation impossible : "
                                  f"{str(exc)[:200]}") from None

    # Slice 16-bis : la resynchronisation re-transcrit → on re-marque les mots
    # à basse confiance (texte et timestamps inchangés).
    mots = marquer_mots_incertains(mots)
    return _aligner_repliques(repliques, mots)


def resynchroniser_replique(job_dir: str | Path, replique: dict) -> dict:
    """Re-transcrit la seule fenêtre ``[debut, fin]`` de la réplique puis réaligne.

    Même contrat que ``synchroniser_repliques`` mais pour UNE réplique : seul
    l'audio de sa fenêtre (avec marges de contexte) est transcrit, jamais le
    fichier entier. Ne persiste rien et ne relance pas le rendu — le front
    ré-affiche la piste et l'utilisateur valide ensuite (PUT) s'il est satisfait.
    """
    job_dir = Path(job_dir)
    _valider_entrees([replique])

    wav = job_dir / "audio_16k.wav"
    if not wav.is_file():
        raise RythmoError("E002", "Audio indisponible pour la synchronisation "
                                  "(le fichier audio_16k.wav est absent).")

    langue, modele, affiner, vocabulaire = _config_transcription(job_dir)

    debut, fin = float(replique["debut"]), float(replique["fin"])
    try:
        mots = transcrire_fenetre(wav, debut, fin, language=langue or None,
                                  model_name=modele, affiner=affiner,
                                  vocabulaire=vocabulaire)
    except RythmoError:
        raise
    except Exception as exc:  # noqa: BLE001 — modèle absent, échec ASR…
        raise RythmoError("E999", "Synchronisation impossible : "
                                  f"{str(exc)[:200]}") from None

    mots = marquer_mots_incertains(mots)
    return _aligner_repliques([replique], mots)[0]
