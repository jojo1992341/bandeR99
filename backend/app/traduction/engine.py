"""Abstraction du moteur de traduction (spec §2).

``TranslationEngine`` est le contrat unique du reste du système : charger,
traduire, s'arrêter. Aucun composant ne dépend d'un modèle concret. Les
moteurs lourds (LLM local GGUF/Ollama) arrivent en slice 8 ; ici, le
``MoteurDeterministe`` sert de référence hors ligne et de repli sans modèle.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping


class TranslationEngine(ABC):
    """Contrat unique d'un moteur de traduction/adaptation."""

    @abstractmethod
    def traduire(self, texte: str, contexte: Mapping[str, Any]) -> str:
        """Adapte ``texte`` dans la langue cible décrite par ``contexte``."""

    def charger(self) -> None:
        """Prépare le modèle (no-op par défaut — surchargé par les moteurs lourds)."""

    def arreter(self) -> None:
        """Libère les ressources du modèle (no-op par défaut)."""


_PHRASES_DETERMINISTES = {
    "hello world": "bonjour tout le monde",
    "thank you": "merci beaucoup",
}


class MoteurDeterministe(TranslationEngine):
    """Repli hors ligne : table de phrases figée, identité sinon (jamais d'erreur)."""

    def traduire(self, texte: str, contexte: Mapping[str, Any]) -> str:
        return _PHRASES_DETERMINISTES.get(texte, texte)


_MOTEURS: dict[str, type[TranslationEngine]] = {"deterministe": MoteurDeterministe}


def enregistrer_moteur(nom: str, classe: type[TranslationEngine]) -> None:
    """Ajoute un moteur au registre (un plugin local en fait autant, slice 8)."""
    _MOTEURS[nom] = classe


def obtenir_moteur(nom: str | None = None,
                   config: Mapping[str, Any] | None = None) -> TranslationEngine:
    """Instancie le moteur ``nom`` (``None`` → repli déterministe).

    ``config`` (dict, optionnel) porte la configuration fournie par le client
    à la requête — ``url`` (URL de base du serveur), ``cle_api`` (Bearer),
    ``modele`` (nom du modèle côté serveur) — jamais persistée par le reste
    du système. Lève ``ValueError`` si le nom est inconnu (l'API le convertit
    en 400) ; une URL manquante/invalide pour un moteur distant lève un
    ``RuntimeError`` clair au premier usage.
    """
    config = dict(config or {})
    if nom is None:
        nom = "deterministe"
    classe = _MOTEURS.get(nom)
    if classe is None and nom in ("llama_cpp", "ollama", "openai_compatible"):
        # moteurs lourds/distants : chargés à la volée (lazy), ils s'enregistrent ici
        from . import llm_local as _llm_local  # noqa: F401
        from . import engine_openai as _engine_openai  # noqa: F401
        classe = _MOTEURS.get(nom)
    if classe is None:
        raise ValueError(f"Moteur de traduction inconnu : '{nom}'")
    if nom == "openai_compatible":
        return classe(hote=str(config.get("url") or ""),
                      cle_api=str(config.get("cle_api") or ""),
                      modele=str(config.get("modele") or "gpt-4o-mini"),
                      temperature=float(config.get("temperature") or 0.7))
    if nom == "ollama":
        # l'URL fournie remplace l'hôte par défaut (ex. proxy ou serveur distant)
        return classe(hote=str(config.get("url") or "http://localhost:11434"))
    return classe()
