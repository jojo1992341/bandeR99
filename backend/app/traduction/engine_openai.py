"""Moteur « API compatible OpenAI » (slice 2 du plan éditeur-reprise-réglages).

Fonctionne avec n'importe quel serveur OpenAI-compatible : LM Studio,
llama.cpp server, Ollama derrière un proxy, ou une API cloud. La config
(URL de base + clé API + nom de modèle) est fournie par le client à chaque
requête — rien n'est en dur. Lazy : importer ce module ne charge rien ;
``requests`` n'est importé qu'à la première traduction.

La clé API est envoyée en en-tête ``Authorization: Bearer …`` et **jamais**
persistée (ni dans ``traduction.json``, ni ailleurs). Un serveur local sans
authentification peut laisser le champ vide.

Un serveur occupé (429/5xx, coupure de connexion, timeout) déclenche des
réessais bornés avec backoff exponentiel : les échecs définitifs (4xx) sont,
eux, remontés immédiatement.
"""
from __future__ import annotations

import time
from typing import Any, Mapping

from .engine import TranslationEngine, enregistrer_moteur

# ——— réessais : le serveur est parfois occupé ———
NB_REESSAIS_MAX = 10        # réessais après un échec transitoire (11 tentatives au plus)
DELAI_REESSAI_BASE = 0.5    # s — backoff exponentiel : 0.5, 1, 2, 4, 8, 8…
DELAI_REESSAI_MAX = 8.0     # s — plafond du délai entre deux tentatives
STATUTS_REESSAYABLES = {408, 429, 500, 502, 503, 504}


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
        url = self._url()
        derniere: Exception | None = None
        for essai in range(NB_REESSAIS_MAX + 1):
            try:
                reponse = requests.post(url, json=corps, headers=en_tetes,
                                        timeout=120)
            except requests.exceptions.Timeout as exc:
                derniere = exc
            except requests.exceptions.ConnectionError as exc:
                derniere = exc
            else:
                if reponse.status_code in STATUTS_REESSAYABLES:
                    # serveur occupé : nouvelle tentative après un court délai
                    derniere = requests.exceptions.HTTPError(
                        f"{reponse.status_code} Server Error for url: {url}",
                        response=reponse)
                else:
                    reponse.raise_for_status()  # 4xx définitif : échec immédiat
                    contenu = reponse.json()["choices"][0]["message"]["content"]
                    return str(contenu or texte).strip()
            if essai < NB_REESSAIS_MAX:
                time.sleep(min(DELAI_REESSAI_BASE * (2 ** essai), DELAI_REESSAI_MAX))
        if derniere is not None:
            raise derniere
        raise RuntimeError("Échec du serveur OpenAI-compatible après réessais")

    def arreter(self) -> None:
        self._requests = None


enregistrer_moteur("openai_compatible", MoteurOpenAI)
