"""Vérification manuelle de l'import Wikipédia du glossaire (méthode LLM).

Interroge la VRAIE API Wikipédia (réseau requis) pour récupérer le texte de la
page, puis demande à un LLM OpenAI-compatible de le classer en trois listes
pour la reconnaissance vocale française :

- ``essentiels`` : noms propres très fréquents et indispensables
  (personnages principaux, lieux récurrents) ;
- ``difficiles``   : termes rares/inhabituels ou à l'orthographe piégeuse,
  que l'ASR risque d'écorcher ;
- ``termes``       : autres termes utiles (personnages secondaires, lieux,
  objets, expressions).

Contrairement à ``extraire_termes_llm`` (liste plate), ce script teste une
sortie structurée. Il n'écrit rien : il affiche le résultat trié.

Usage :
    .venv/Scripts/python scripts/verifier_import_llm.py
    .venv/Scripts/python scripts/verifier_import_llm.py "Kaamelott" \\
        --serveur http://127.0.0.1:31415/v1 --cle-api "…" --modele auto
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

# Console Windows : afficher les accents (UTF-8) au lieu de « � ».
for flux in (sys.stdout, sys.stderr):
    if flux and hasattr(flux, "reconfigure"):
        flux.reconfigure(encoding="utf-8")

from app.traduction.engine import obtenir_moteur  # noqa: E402
from app.traduction.glossaire import normaliser_cle  # noqa: E402
from app.wikipedia_import import (_recuperer_page,  # noqa: E402
                                  _texte_de_page)

TITRE_PAR_DEFAUT = "Kaamelott"
LANGUE_PAR_DEFAUT = "fr"
SERVEUR_PAR_DEFAUT = "http://127.0.0.1:31415/v1"
MODELE_PAR_DEFAUT = "auto"
LIMIT_TEXTE = 24000  # plafond de caractères du texte envoyé au LLM

SECTIONS = ("essentiels", "difficiles", "termes")

_PROMPT_CATEGORISE = (
    "Voici le contenu Wikipédia d'une série télévisée :\n\n{extrait}\n\n"
    "Classe les noms propres, personnages, lieux et termes utiles à la "
    "reconnaissance vocale française en trois listes :\n"
    "1. « essentiels » : noms très fréquents et indispensables (personnages "
    "principaux, lieux récurrents) ;\n"
    "2. « difficiles » : termes rares, inhabituels ou à l'orthographe piégeuse, "
    "que la reconnaissance vocale risque d'écorcher ;\n"
    "3. « termes » : autres termes utiles (personnages secondaires, lieux, "
    "objets, expressions).\n\n"
    "Réponds STRICTEMENT au format suivant, une liste par ligne, sans "
    "commentaire ni numérotation :\n"
    "essentiels: A, B, C\n"
    "difficiles: D, E, F\n"
    "termes: G, H, I"
)


def _canonique(nom: str) -> str:
    """« essentiel »/« essentiels » → clé de section canonique."""
    nom = re.sub(r"s?$", "", nom.strip().lower())
    for section in SECTIONS:
        if section.startswith(nom):
            return section
    return nom


def _nettoyer_terme(morceau: str) -> str:
    terme = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", morceau).strip()
    terme = terme.strip("\"'`()[]")
    return terme.rstrip(".,;:")


def parser_categorise(texte: str) -> dict[str, list[str]]:
    """Parse une réponse LLM en {essentiels: [], difficiles: [], termes: []}.

    Tolère les variantes de format : en-têtes de section « essentiels: … »,
    « - essentiels: … », « # essentiels » seul sur sa ligne, etc. Jamais
    d'exception.
    """
    resultat: dict[str, list[str]] = {s: [] for s in SECTIONS}
    courante: str | None = None

    for ligne in str(texte or "").splitlines():
        entete = re.match(
            r"^\s*(?:[-*•]|\d+[.)])?\s*(essentiels?|difficiles?|termes?)\s*"
            r"[:：\-–]\s*(.*)$", ligne, re.IGNORECASE)
        if entete:
            courante = _canonique(entete.group(1))
            contenu = entete.group(2)
        else:
            seule = re.match(
                r"^\s*#*\s*(essentiels?|difficiles?|termes?)\s*$",
                ligne, re.IGNORECASE)
            if seule:
                courante = _canonique(seule.group(1))
                continue
            if courante is None:
                continue
            contenu = ligne
        if courante in resultat:
            for morceau in re.split(r"[,\n;]+", contenu):
                terme = _nettoyer_terme(morceau)
                if 2 <= len(terme) <= 60 and len(terme.split()) <= 4:
                    resultat[courante].append(terme)

    # dédoublonnage (casse/accents insensibles), ordre conservé
    for section in SECTIONS:
        vus: set[str] = set()
        propres: list[str] = []
        for terme in resultat[section]:
            cle = normaliser_cle(terme)
            if cle and cle not in vus:
                vus.add(cle)
                propres.append(terme)
        resultat[section] = propres
    return resultat


def main(argv: list[str] | None = None) -> int:
    analyseur = argparse.ArgumentParser(
        description="Vérifie l'import Wikipédia via LLM avec sortie catégorisée.")
    analyseur.add_argument("titre", nargs="?", default=TITRE_PAR_DEFAUT)
    analyseur.add_argument("--langue", choices=("fr", "en"),
                           default=LANGUE_PAR_DEFAUT)
    analyseur.add_argument("--serveur", default=SERVEUR_PAR_DEFAUT)
    analyseur.add_argument("--cle-api", default="", help="Clé Bearer (optionnelle)")
    analyseur.add_argument("--modele", default=MODELE_PAR_DEFAUT,
                           help="Nom de modèle côté serveur (défaut : auto)")
    args = analyseur.parse_args(argv)

    texte = _texte_de_page(_recuperer_page(args.titre, args.langue, client=None))
    tronque = texte if len(texte) <= LIMIT_TEXTE else texte[:LIMIT_TEXTE]

    moteur = obtenir_moteur("openai_compatible", {
        "url": args.serveur, "cle_api": args.cle_api, "modele": args.modele})
    print(f"Page   : {args.titre} ({args.langue}.wikipedia.org)")
    print(f"Texte  : {len(texte)} caractères (envoyé : {len(tronque)})")
    print(f"Modèle : {args.modele} @ {args.serveur}")
    print("Appel du LLM…")
    try:
        reponse = str(moteur.traduire(
            _PROMPT_CATEGORISE.format(extrait=tronque), {}) or "")
    except Exception as exc:  # noqa: BLE001 — erreur claire, jamais de crash
        print(f"[ERREUR] Moteur LLM indisponible : {exc}", file=sys.stderr)
        return 1

    print("\n——— Réponse brute du LLM ———")
    print(reponse.strip()[:4000])

    classes = parser_categorise(reponse)
    print("\n——— Liste triée ———")
    for section in SECTIONS:
        termes = sorted(classes[section],
                        key=lambda t: normaliser_cle(t))
        print(f"\n{section.capitalize()} ({len(termes)}) :")
        for terme in termes:
            print(f"  - {terme}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
