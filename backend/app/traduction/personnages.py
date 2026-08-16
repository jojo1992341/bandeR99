"""Profils de personnages (slice 6) — registre, âge, style, vocabulaire.

``CharacterManager`` porte le profil de chaque personnage (spéc. §18) : le
registre (« formel », « familier »…), l'âge, le style et un vocabulaire
préférentiel. Ces données sont transmises au moteur **seulement** quand le
personnage de la réplique a un profil connu — repli neutre sinon.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass
class ProfilPersonnage:
    """Profil d'un personnage : registre, âge, style, vocabulaire."""

    nom: str
    registre: str = ""
    age: str = ""
    style: str = ""
    vocabulaire: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Sérialisation compacte : uniquement les champs renseignés."""
        d: dict = {}
        if self.registre:
            d["registre"] = self.registre
        if self.age:
            d["age"] = self.age
        if self.style:
            d["style"] = self.style
        if self.vocabulaire:
            d["vocabulaire"] = list(self.vocabulaire)
        return d


class CharacterManager:
    """Profils de personnages, consultés par nom (``None`` si inconnu)."""

    def __init__(self, profils: Iterable[ProfilPersonnage] | None = None):
        self.profils: dict[str, ProfilPersonnage] = {
            p.nom: p for p in (profils or [])
        }

    def profil(self, nom: str) -> ProfilPersonnage | None:
        """Profil du personnage ``nom`` ; ``None`` si inconnu (neutre)."""
        return self.profils.get(nom)

    def contexte(self, nom: str) -> dict | None:
        """Profil sérialisé (compact) à transmettre au moteur, ou ``None``."""
        profil = self.profil(nom)
        return profil.to_dict() if profil else None
