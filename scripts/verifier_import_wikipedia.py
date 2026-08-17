"""Vérification manuelle de l'import Wikipédia du glossaire (heuristique).

Interroge la VRAIE API Wikipédia (réseau requis) pour une page donnée et
affiche les candidats de vocabulaire extraits par l'heuristique
(``extraire_termes_avec_repli``) : liens internes présents dans l'extrait,
filtrés et dédoublonnés.

Usage :
    .venv/Scripts/python scripts/verifier_import_wikipedia.py [URL|titre]

Exemples :
    .venv/Scripts/python scripts/verifier_import_wikipedia.py
    .venv/Scripts/python scripts/verifier_import_wikipedia.py https://fr.wikipedia.org/wiki/Kaamelott
    .venv/Scripts/python scripts/verifier_import_wikipedia.py "Kaamelott" --langue en

Retour : 0 si l'import a abouti (candidats obtenus, y compris liste vide) ;
         1 en cas d'erreur (URL refusée, réseau, page introuvable dans les
         deux langues).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.wikipedia_import import (ErreurImportWikipedia,  # noqa: E402
                                  extraire_termes_avec_repli, resoudre_saisie)

PAGE_PAR_DEFAUT = "https://fr.wikipedia.org/wiki/Kaamelott"


def main(argv: list[str] | None = None) -> int:
    analyseur = argparse.ArgumentParser(
        description="Vérifie l'import Wikipédia du glossaire sur une vraie page.")
    analyseur.add_argument(
        "saisie", nargs="?", default=PAGE_PAR_DEFAUT,
        help="Titre ou URL Wikipédia (défaut : %(default)s)")
    analyseur.add_argument(
        "--langue", choices=("fr", "en"), default=None,
        help="Langue pour un titre nu (une URL fixe déjà la langue) ; "
             "repli automatique vers l'autre langue si la page est absente")
    args = analyseur.parse_args(argv)

    explicite = args.saisie.startswith(("http://", "https://"))
    try:
        langue, titre = resoudre_saisie(args.saisie, args.langue)
        langue_effective, candidats = extraire_termes_avec_repli(
            titre, langue, repli=not explicite)
    except ErreurImportWikipedia as exc:
        print(f"[ERREUR {exc.code}] {exc.message}", file=sys.stderr)
        return 1

    print(f"Page  : {titre}")
    print(f"Langue: {langue_effective} ({langue_effective}.wikipedia.org)")
    if not candidats:
        print("Candidats : aucun terme trouvé pour cette page.")
        return 0
    print(f"Candidats ({len(candidats)}) :")
    for terme in candidats:
        print(f"  - {terme}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
