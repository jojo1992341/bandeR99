"""Format de projet versionné : sauvegarde et restauration du travail complet.

Un « projet » regroupe tout ce qui fait le travail du comédien — répliques
corrigées, timings mot-à-mot, options de rendu — dans un fichier JSON portable
(export/import). Le champ ``format_version`` permet de migrer les anciens
fichiers et de refuser proprement ceux d'une version plus récente (E008).

La vidéo source n'est **pas** incluse (trop lourde) : à l'import, le lien
local du job (``params.json["source"]``) est préservé ; l'utilisateur
ré-importe sa vidéo sur la nouvelle machine et relance le rendu.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .cues_edit import valider_repliques
from .edition import chemin_params, ecrire_repliques, lire_repliques

VERSION_FORMAT_PROJET = 1

# Bornes des paramètres numériques importés (cohérentes avec les options du front)
PARAMS_BORNES: dict[str, tuple[float, float]] = {
    "hauteur_bande": (60, 400),
    "curseur_ratio": (0.05, 0.50),
    "vitesse": (0.05, 5.0),
    "taille_police": (10, 200),
}
PARAMS_ENTIERS = {"hauteur_bande", "taille_police"}
PARAMS_BOOLEENS = {"aligner_whisperx", "lipsync", "etirer_mots", "diariser", "edition"}
PARAMS_CHAINES = {"langue", "modele", "style", "theme", "asr", "modele_cloud", "asr_cle"}


def _assainir_params(params) -> dict:
    """Garde les clés connues, coërce les types, borne les numériques.

    Un fichier de projet manipulé à la main ne doit jamais pouvoir faire
    planter le rendu ni sortir des plages affichables (E004/E005 côté
    serveur) : on écrase toute valeur douteuse par la valeur assainie.
    """
    if not isinstance(params, dict):
        return {}
    propres: dict = {}
    for cle, valeur in params.items():
        if cle in PARAMS_ENTIERS:
            try:
                propres[cle] = int(round(float(valeur)))
            except (TypeError, ValueError):
                continue
            mini, maxi = PARAMS_BORNES[cle]
            propres[cle] = max(mini, min(maxi, propres[cle]))
        elif cle in PARAMS_BORNES:  # flottants bornés
            try:
                propres[cle] = float(valeur)
            except (TypeError, ValueError):
                continue
            mini, maxi = PARAMS_BORNES[cle]
            propres[cle] = max(mini, min(maxi, propres[cle]))
        elif cle in PARAMS_BOOLEENS:
            if isinstance(valeur, str):
                propres[cle] = valeur.strip().lower() in ("true", "1", "oui")
            else:
                propres[cle] = bool(valeur)
        elif cle in PARAMS_CHAINES:
            if isinstance(valeur, str):
                propres[cle] = valeur
    return propres


def exporter_projet(job_dir: str | Path) -> dict:
    """Bundles le travail du job (params + répliques + méta source) en un dict portable."""
    job_dir = Path(job_dir)
    payload = lire_repliques(job_dir)
    cfg_path = chemin_params(job_dir)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.is_file() else {}
    return {
        "format_version": VERSION_FORMAT_PROJET,
        "app_version": __version__,
        "export_date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "params": cfg.get("params", {}),
        "repliques": payload,
        "source": {
            "nom": cfg.get("source"),
            "duree_video": payload.get("duree_video"),
        },
    }


def migrer_projet(projet: dict) -> dict:
    """Vérifie la version du format et migre si besoin ; refuse sinon (E008)."""
    from .errors import RythmoError

    version = projet.get("format_version")
    if not isinstance(version, int) or version < 1:
        raise RythmoError("E008", f"Format de projet inconnu : version {version!r} "
                                  f"(attendu ≥ 1).")
    if version > VERSION_FORMAT_PROJET:
        raise RythmoError(
            "E008", f"Format de projet trop récent : version {version} alors que "
                    f"cette application gère jusqu'à la {VERSION_FORMAT_PROJET}. "
                    "Mettez à jour l'application pour ouvrir ce projet.")
    if version < VERSION_FORMAT_PROJET:
        # aucune migration nécessaire à ce jour (v1 est le format courant)
        pass
    return projet


def importer_projet(job_dir: str | Path, projet: dict) -> dict:
    """Restaure répliques + params d'un projet, après validation complète.

    Lève ``RythmoError`` **avant** toute écriture (E005 répliques invalides,
    E008 format inconnu) : le job existant n'est jamais endommagé par un
    import raté. Les répliques sont revalidées (valider_repliques) et les
    params assainis (bornes + types).
    """
    from .errors import RythmoError

    job_dir = Path(job_dir)
    projet = migrer_projet(projet)

    repliques = projet.get("repliques")
    if not isinstance(repliques, dict) or not isinstance(repliques.get("repliques"), list):
        raise RythmoError("E005", "Le projet ne contient pas de répliques valides "
                                  "(champ « repliques » attendu).")
    try:
        duree = float(repliques.get("duree_video", 0.0))
    except (TypeError, ValueError):
        raise RythmoError("E005", "Le projet ne contient pas de durée de vidéo "
                                  "valide.") from None
    validees = valider_repliques(repliques["repliques"], duree)
    repliques["repliques"] = validees

    # écriture atomique (jamais de JSON tronqué en cas de crash)
    ecrire_repliques(job_dir, repliques)

    cfg_path = chemin_params(job_dir)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.is_file() else {}
    cfg["params"] = _assainir_params(projet.get("params")) or cfg.get("params", {})
    # le lien local vers la vidéo source est préservé : il appartient à la machine
    cfg.setdefault("source", "source.mp4")
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    return repliques
