"""Persistance de l'édition manuelle des répliques (phase 1 ↔ phase 2).

``repliques.json`` (dossier du job) est la source de vérité éditable échangée
avec le front ; ``params.json`` fige les options choisies à l'envoi et le nom de
la copie locale de la vidéo (``source.<ext>``) pour que le rendu puisse se
rejouer indépendamment.
"""
from __future__ import annotations

import json
from pathlib import Path

from .asr import Word
from .cues import Cue
from .symboles import est_symbole
from .cues_edit import _distribuer_uniforme, resynchroniser_mots, valider_repliques
from .paths import safe_path

VERSION_REPLIQUES = 1


def chemin_repliques(job_dir: str | Path) -> Path:
    return safe_path(job_dir, "repliques.json")


def chemin_params(job_dir: str | Path) -> Path:
    return safe_path(job_dir, "params.json")


def ecrire_repliques(job_dir: str | Path, payload: dict) -> Path:
    """Écrit le payload de façon atomique (jamais de JSON tronqué en cas de crash)."""
    cible = chemin_repliques(job_dir)
    tampon = cible.with_suffix(".tmp")
    tampon.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tampon.replace(cible)
    return cible


def lire_repliques(job_dir: str | Path) -> dict:
    cible = chemin_repliques(job_dir)
    if not cible.is_file():
        from .errors import RythmoError
        raise RythmoError("E005", "Aucun fichier de répliques pour ce job : "
                                  "lancez d'abord la phase d'analyse.")
    return json.loads(cible.read_text(encoding="utf-8"))


def appliquer_edition(job_dir: str | Path, repliques_editees,
                      personnages: list[str] | None = None) -> dict:
    """Valide les corrections, resynchronise les mots, persiste ; retourne le payload.

    ``personnages`` (noms de la scène, facultatifs) est mémorisé tel quel : il
    décore l'éditeur (badges, clic droit parole simultanée) sans affecter le
    rendu. Lève ``RythmoError("E005")`` sans toucher au fichier si les
    répliques sont invalides (le front reçoit alors le message agrégé).
    """
    payload = lire_repliques(job_dir)
    validees = valider_repliques(repliques_editees, float(payload["duree_video"]))
    resynchronisees = resynchroniser_mots(validees, payload.get("repliques", []))
    payload["repliques"] = resynchronisees
    if isinstance(personnages, list) and all(isinstance(n, str) for n in personnages):
        payload["personnages"] = [n.strip() or f"Personnage {i + 1}"
                                  for i, n in enumerate(personnages)]
    payload["edite_manuellement"] = True
    ecrire_repliques(job_dir, payload)
    return payload


def appliquer_textes_cibles(payload: dict, texte_cible: dict | None = None) -> dict:
    """Substitution pure du texte des répliques par leur traduction (spec §22).

    ``texte_cible`` associe l'``id`` d'une réplique à son ``target_text``. Les
    timecodes (``debut``/``fin``) ne changent jamais ; les ``mots`` d'origine
    sont remplacés par les tokens de la cible, répartis uniformément dans la
    même fenêtre (le générateur karaoké défile ainsi la traduction sur la bande
    d'origine, sans recalage). Une réplique sans cible (ou cible vide) garde son
    texte source. L'entrée n'est jamais mutée : cette fonction est pure.
    """
    if not texte_cible:
        return payload
    repliques = []
    for r in payload.get("repliques", []):
        cible = texte_cible.get(str(r.get("id", "")))
        texte = str(cible).strip() if cible is not None else ""
        if not texte:
            repliques.append(r)
            continue
        debut, fin = float(r["debut"]), float(r["fin"])
        nouvelle = dict(r)
        nouvelle["texte"] = texte
        nouvelle["mots"] = _distribuer_uniforme(texte.split(), debut, fin)
        repliques.append(nouvelle)
    return {**payload, "repliques": repliques}


def payload_vers_cues(payload: dict, texte_cible: dict | None = None) -> list[Cue]:
    """Convertit les répliques persistées en ``Cue`` prêts pour le rendu karaoké.

    Les timings des mots sont bornés à la fenêtre [debut, fin] de leur réplique
    (fichier éventuellement retapé à la main) ; un mot incohérent retombe sur la
    fenêtre entière de sa réplique. Avec ``texte_cible`` (argument optionnel,
    rétro-compatible), le texte de chaque réplique est remplacé par sa traduction
    et les mots re-timés dans la même fenêtre — les timecodes restent identiques.
    """
    payload = appliquer_textes_cibles(payload, texte_cible)
    cues: list[Cue] = []
    for r in payload["repliques"]:
        debut, fin = float(r["debut"]), float(r["fin"])
        mots: list[Word] = []
        for m in r.get("mots", []):
            d = min(max(float(m["debut"]), debut), fin)
            f = min(max(float(m["fin"]), debut), fin)
            if f <= d:
                d, f = debut, fin  # mot incohérent : couverture de la réplique
            texte = str(m["texte"])
            mots.append(Word(texte, d, f,
                             marqueur=bool(m.get("marqueur")) or est_symbole(texte)))
        if not mots:  # réplique sans détail mot : un seul mot-couverture
            mots = [Word(r["texte"], debut, fin)]
        cues.append(Cue(words=mots, personnage=r.get("personnage")))
    return cues
