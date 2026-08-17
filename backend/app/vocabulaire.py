"""Vocabulaire automatique du projet (Slice 18).

Collecte les noms propres et termes du projet pour biaiser la transcription
française (prompt initial) et alimenter le correcteur phonétique des noms
propres (Slice 13). Le vocabulaire explicite fourni par l'utilisateur est
TOUJOURS prioritaire ; sinon, on rassemble — sans jamais quitter le dossier du
job — :

- les noms des personnages de la scène (``repliques.json["personnages"]``), en
  écartant les libellés automatiques (« Voix 1 », « Personnage 2 ») ;
- les termes du glossaire du job (``glossaire.json`` : liste de chaînes, ou
  objet ``{"termes": [...]}``).

Le résultat est dédupliqué insensiblement à la casse/accents (premier terme
gagnant). Aucune donnée hors du dossier du job n'entre dans le résultat.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .asr import normaliser_mot

# Libellés générés automatiquement (« Voix 1 », « Personnage 2 ») : ce ne sont
# pas des noms propres, ils n'apportent rien au décodage.
_LIBELLE_AUTOMATIQUE = re.compile(r"^(voix|personnage)\s*\d+$", re.IGNORECASE)


def _noms_personnages(job_dir: Path) -> list[str]:
    """Noms des personnages de la scène (``repliques.json``), libellés auto exclus."""
    chemin = job_dir / "repliques.json"
    if not chemin.is_file():
        return []
    try:
        payload = json.loads(chemin.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    noms = payload.get("personnages") if isinstance(payload, dict) else None
    if not isinstance(noms, list):
        return []
    return [str(n).strip() for n in noms
            if str(n).strip() and not _LIBELLE_AUTOMATIQUE.match(str(n).strip())]


def _termes_glossaire(job_dir: Path) -> list[str]:
    """Termes du glossaire du job (``glossaire.json``) : liste ou ``{"termes": [...]}``."""
    chemin = job_dir / "glossaire.json"
    if not chemin.is_file():
        return []
    try:
        donnees = json.loads(chemin.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(donnees, list):
        return [str(t).strip() for t in donnees if str(t).strip()]
    if isinstance(donnees, dict) and isinstance(donnees.get("termes"), list):
        return [str(t).strip() for t in donnees["termes"] if str(t).strip()]
    return []


def vocabulaire_du_projet(job_dir: str | Path,
                          explicite: list[str] | None = None) -> list[str]:
    """Vocabulaire du projet : l'explicite prime, sinon personnages + glossaire.

    ``explicite`` non vide est rendu TEL QUEL (priorité absolue, jamais
    écrasé). Sinon, les noms des personnages puis les termes du glossaire du
    job sont concaténés et dédupliqués (casse/accents ignorés, premier terme
    gagnant). Aucune donnée hors du dossier du job n'entre dans le résultat.
    """
    if explicite:
        return list(explicite)
    job_dir = Path(job_dir)
    termes: list[str] = []
    vus: set[str] = set()
    for terme in _noms_personnages(job_dir) + _termes_glossaire(job_dir):
        cle = normaliser_mot(terme)
        if cle and cle not in vus:
            vus.add(cle)
            termes.append(terme)
    return termes
