"""Export des répliques en sous-titres SRT (format standard `.srt`).

Convertit le payload ``repliques.json`` (répliques générées ou corrigées)
en fichier SRT : une entrée par réplique, avec son horodatage et son texte.
Le SRT sert de sous-titres lisibles par tout lecteur vidéo (VLC, lecteurs
navigateur…) — c'est la version « texte » du travail de doublage.
"""
from __future__ import annotations


def formater_temps(secondes: float) -> str:
    """``3661.249`` → ``01:01:01,249`` — arrondi au millième, jamais négatif."""
    ms_total = max(0, round(float(secondes) * 1000))
    heures, reste = divmod(ms_total, 3_600_000)
    minutes, reste = divmod(reste, 60_000)
    secondes_entieres, millisecondes = divmod(reste, 1000)
    return f"{heures:02d}:{minutes:02d}:{secondes_entieres:02d},{millisecondes:03d}"


def generer_srt(payload: dict) -> str:
    """Convertit le payload de répliques en texte SRT (UTF-8, LF).

    Chaque réplique devient un bloc ``numéro + horodatage + texte`` séparé
    par une ligne vide ; les éventuels sauts de ligne du texte (corrections
    manuelles multi-lignes) sont conservés tels quels. Sans réplique, le
    fichier est vide (chaîne ``""``).
    """
    blocs: list[str] = []
    for i, r in enumerate(payload.get("repliques", []), start=1):
        debut = formater_temps(float(r["debut"]))
        fin = formater_temps(float(r["fin"]))
        texte = str(r.get("texte", "")).strip()
        blocs.append(f"{i}\n{debut} --> {fin}\n{texte}")
    if not blocs:
        return ""
    return "\n\n".join(blocs) + "\n"
