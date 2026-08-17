"""Glossaire de traduction (slice 6) — prioritaire sur la traduction automatique.

``GlossaryManager`` porte les correspondances terme→terme imposées par
l'utilisateur (noms propres, expressions, terminologie) et les termes
**interdits**. Une entrée exacte du glossaire est **prioritaire** : elle court-circuite
le moteur. Les correspondances partielles sont, elles, transmises au moteur
(``contexte``) pour qu'il les respecte — jamais de donnée hors du glossaire.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..paths import safe_path


def normaliser_cle(texte: str) -> str:
    """Minuscules et accents retirés : clés de glossaire insensibles à la casse/accents."""
    if texte is None:
        return ""
    decompose = unicodedata.normalize("NFD", str(texte).lower())
    return "".join(c for c in decompose if unicodedata.category(c) != "Mn")


@dataclass
class EntreeGlossaire:
    """Une correspondance imposée : ``source`` → ``cible`` (note optionnelle)."""

    source: str
    cible: str
    note: str = ""


class GlossaryManager:
    """Correspondances terme→terme (prioritaires) + termes interdits."""

    def __init__(self, entrees: Iterable[EntreeGlossaire | tuple] | None = None,
                 interdits: Iterable[str] | None = None):
        self.entrees: dict[str, str] = {}
        for entree in (entrees or []):
            if isinstance(entree, EntreeGlossaire):
                self.entrees[normaliser_cle(entree.source)] = entree.cible
            else:
                self.entrees[normaliser_cle(entree[0])] = entree[1]
        self.interdits: set[str] = {normaliser_cle(i) for i in (interdits or [])}

    def traduire(self, texte: str) -> str | None:
        """Cible prioritaire si ``texte`` est une entrée exacte ; sinon ``None``."""
        return self.entrees.get(normaliser_cle(texte))

    def correspondances(self, texte: str) -> dict[str, str]:
        """Termes du glossaire présents dans ``texte`` (mot entier) → {source: cible}."""
        norm = normaliser_cle(texte)
        resultat: dict[str, str] = {}
        for source, cible in self.entrees.items():
            if source and re.search(rf"\b{re.escape(source)}\b", norm):
                resultat[source] = cible
        return resultat

    def interdit(self, texte: str) -> bool:
        """Vrai si ``texte`` (normalisé) est un terme interdit."""
        return normaliser_cle(texte) in self.interdits

    def contexte(self, texte: str) -> dict[str, str] | None:
        """Correspondances à transmettre au moteur, ou ``None`` si aucune."""
        correspondances = self.correspondances(texte)
        return correspondances or None


NOM_FICHIER_GLOSSAIRE = "glossaire.json"


class GlossaireStore:
    """Persistance du glossaire du job (``glossaire.json``) — termes pour l'ASR.

    Format stocké : ``{"termes": ["Francis", "Kaamelott", …]}``. Lecture
    tolérante (fichier absent/corrompu → liste vide) ; écriture atomique
    (tampon puis ``replace``) et dédupliquée insensiblement à la casse/accents.
    """

    def __init__(self, job_dir: str | Path):
        self.job_dir = Path(job_dir)

    def chemin(self) -> Path:
        return safe_path(self.job_dir, NOM_FICHIER_GLOSSAIRE)

    def lire(self) -> list[str]:
        """Termes persistés (non vides), liste vide si absent ou illisible."""
        cible = self.chemin()
        if not cible.is_file():
            return []
        try:
            donnees = json.loads(cible.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        termes = donnees.get("termes") if isinstance(donnees, dict) else None
        if not isinstance(termes, list):
            return []
        return [str(t).strip() for t in termes if str(t).strip()]

    def ecrire(self, termes: Iterable[str] | None) -> Path:
        """Écrit le glossaire (dédupliqué, casse/accents ignorés) de façon atomique."""
        propres: list[str] = []
        vus: set[str] = set()
        for terme in (termes or []):
            mot = str(terme).strip()
            cle = normaliser_cle(mot)
            if cle and cle not in vus:
                vus.add(cle)
                propres.append(mot)
        cible = self.chemin()
        tampon = cible.with_suffix(".tmp")
        tampon.write_text(json.dumps({"termes": propres}, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        tampon.replace(cible)
        return cible
