"""Moteur « API compatible OpenAI » (slice 2 du plan éditeur-reprise-réglages).

Fonctionne avec n'importe quel serveur OpenAI-compatible : LM Studio,
llama.cpp server, Ollama derrière un proxy, ou une API cloud. La config
(URL de base + clé API + nom de modèle) est fournie par le client à chaque
requête — rien n'est en dur. Lazy : importer ce module ne charge rien ;
``requests`` n'est importé qu'à la première traduction.

La clé API est envoyée en en-tête ``Authorization: Bearer …`` et **jamais**
persistée (ni dans ``traduction.json``, ni ailleurs). Un serveur local sans
authentification peut laisser le champ vide.
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


class MoteurOpenAI(TranslationEngine):
    """Moteur via n'importe quel serveur OpenAI-compatible (chat/completions)."""

    def __init__(self, hote: str = "", cle_api: str = "",
                 modele: str = "gpt-4o-mini", temperature: float = 0.7):
        self.hote = hote
        self.cle_api = cle_api
        self.modele = modele
        self.temperature = temperature
        self._requests = None

    def charger(self) -> None:
        """Vérifie la config/dépendance (lazy) — erreur claire sinon."""
        if not self.hote or not self.hote.startswith(("http://", "https://")):
            raise RuntimeError(
                "Aucune URL de serveur OpenAI-compatible configurée "
                "(fournissez une URL http(s)://… dans le champ « URL »)")
        try:
            import requests  # lazy : dépendance optionnelle
        except ImportError as exc:
            raise RuntimeError("requests n'est pas installé (pip install requests)") from exc
        self._requests = requests

    def _url(self) -> str:
        """URL complète de l'endpoint chat/completions (base fournie par le client)."""
        base = self.hote.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def traduire(self, texte: str, contexte: Mapping[str, Any]) -> str:
        if self._requests is None:
            self.charger()
        requests = self._requests
        en_tetes = {"Content-Type": "application/json"}
        if self.cle_api:
            en_tetes["Authorization"] = f"Bearer {self.cle_api}"
        temperature = float(contexte.get("temperature", self.temperature))
        corps = {
            "model": self.modele,
            "messages": [
                {"role": "system", "content":
                 f"Tu traduis de {contexte.get('source_language', 'en')} vers "
                 f"{contexte.get('target_language', 'fr')}. Réponds uniquement "
                 "avec le texte traduit, sans commentaire."},
                {"role": "user", "content": _prompt(texte, contexte)},
            ],
            "temperature": temperature,
        }
        reponse = requests.post(self._url(), json=corps, headers=en_tetes,
                                timeout=120)
        reponse.raise_for_status()  # erreur claire (statut HTTP) si serveur défaillant
        contenu = reponse.json()["choices"][0]["message"]["content"]
        return str(contenu or texte).strip()

    def arreter(self) -> None:
        self._requests = None


enregistrer_moteur("openai_compatible", MoteurOpenAI)
