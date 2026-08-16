"""Moteurs LLM locaux (slice 8) : GGUF (llama-cpp-python) et Ollama.

Dépendances lourdes **optionnelles et lazy** : importer ce module ne charge rien.
``charger()`` importe la dépendance et charge le modèle uniquement si un chemin/
nom est fourni ; sinon, ou si la dépendance manque, une **erreur claire** est
levée (jamais de crash à l'import). ``arreter()`` libère les ressources.
Les deux moteurs s'enregistrent dans le registre ``engine`` : ils deviennent
sélectionnables via ``obtenir_moteur(\"llama_cpp\" | \"ollama\")``, sans casser
le reste du pipeline (abstraction ``TranslationEngine``).
"""
from __future__ import annotations

from typing import Any, Mapping

from .engine import TranslationEngine, enregistrer_moteur


def _prompt(texte: str, contexte: Mapping[str, Any]) -> str:
    """Prompt compact : texte + seules les données de contexte réellement disponibles."""
    lignes = [f"Traduis dans {contexte.get('target_language', 'fr')} :", texte]
    for cle in ("glossaire", "memoire", "precedent", "suivant", "contrainte"):
        valeur = contexte.get(cle)
        if valeur:
            lignes.append(f"{cle}: {valeur}")
    return "\n".join(lignes)


class LlamaCppEngine(TranslationEngine):
    """Moteur GGUF local via llama-cpp-python (CPU/GPU selon ``choose_device``)."""

    def __init__(self, chemin_modele: str | None = None, temperature: float = 0.7,
                 n_ctx: int = 2048):
        self.chemin_modele = chemin_modele
        self.temperature = temperature
        self.n_ctx = n_ctx
        self._modele = None

    def charger(self) -> None:
        """Charge le modèle (lazy) — erreur claire si non configuré ou dépendance absente."""
        if not self.chemin_modele:
            raise RuntimeError("Aucun modèle GGUF configuré (fournissez un chemin .gguf)")
        try:
            from llama_cpp import Llama  # lazy : dépendance optionnelle
        except ImportError as exc:
            raise RuntimeError("llama-cpp-python n'est pas installé "
                               "(pip install llama-cpp-python)") from exc
        from ..devices import choose_device

        self._modele = Llama(model_path=self.chemin_modele, n_ctx=self.n_ctx,
                             n_gpu_layers=-1 if choose_device() == "cuda" else 0)

    def traduire(self, texte: str, contexte: Mapping[str, Any]) -> str:
        if self._modele is None:
            self.charger()
        temperature = float(contexte.get("temperature", self.temperature))
        sortie = self._modele(_prompt(texte, contexte), max_tokens=256,
                              temperature=temperature)
        return str(sortie["choices"][0]["text"] or texte).strip()

    def arreter(self) -> None:
        self._modele = None


class OllamaEngine(TranslationEngine):
    """Moteur via un serveur Ollama local (HTTP), lazy."""

    def __init__(self, modele: str | None = None, hote: str = "http://localhost:11434",
                 temperature: float = 0.7):
        self.modele = modele
        self.hote = hote
        self.temperature = temperature
        self._requests = None

    def charger(self) -> None:
        """Vérifie la config/dépendance (lazy) — erreur claire sinon."""
        if not self.modele:
            raise RuntimeError("Aucun modèle Ollama configuré (fournissez un nom de modèle)")
        try:
            import requests  # lazy : dépendance optionnelle
        except ImportError as exc:
            raise RuntimeError("requests n'est pas installé (pip install requests)") from exc
        self._requests = requests

    def traduire(self, texte: str, contexte: Mapping[str, Any]) -> str:
        if not self.modele:
            self.charger()
        requests = self._requests
        if requests is None:
            try:
                import requests  # noqa: F401 — lazy
            except ImportError as exc:
                raise RuntimeError("requests n'est pas installé "
                                   "(pip install requests)") from exc
        temperature = float(contexte.get("temperature", self.temperature))
        reponse = requests.post(f"{self.hote}/api/generate", json={
            "model": self.modele,
            "prompt": _prompt(texte, contexte),
            "stream": False,
            "options": {"temperature": temperature},
        }, timeout=120)
        reponse.raise_for_status()
        return str(reponse.json().get("response") or texte).strip()

    def arreter(self) -> None:
        self._requests = None


enregistrer_moteur("llama_cpp", LlamaCppEngine)
enregistrer_moteur("ollama", OllamaEngine)
