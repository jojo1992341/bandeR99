"""Erreurs métier de Rythmo Dub, avec code stable exposé au front (messages FR).

Référentiel des codes (voir T35 pour le mapping complet côté interface) :
- E001 : fichier vidéo illisible ou corrompu
- E002 : aucune piste audio détectée
"""
from __future__ import annotations


class AnnulationDemandee(Exception):
    """Levée (côté worker) quand l'utilisateur demande l'annulation du job."""


# Référentiel des messages utilisateur : code → (titre, suggestion d'action)
CODES_ERREUR: dict[str, tuple[str, str]] = {
    "E001": ("Vidéo illisible ou corrompue",
             "Vérifiez que le fichier s'ouvre dans un lecteur (VLC), puis essayez "
             "de le ré-encoder en MP4 H.264."),
    "E002": ("Aucune piste audio dans la vidéo",
             "Cette vidéo est muette : le doublage nécessite une bande-son. "
             "Choisissez un fichier contenant de la parole."),
    "E003": ("Aucun visage détecté sur l'image",
             "La synchronisation labiale est impossible ici : la bande rythmo "
             "utilisera le calage audio."),
    "E004": ("Chemin de fichier refusé",
             "Le fichier doit se trouver dans le dossier de travail de "
             "l'application (sécurité anti-évasion)."),
    "E005": ("Répliques invalides après édition",
             "Corrigez le texte et les horaires signalés : chaque réplique doit "
             "avoir un texte non vide, un début avant sa fin, rester dans la "
             "durée de la vidéo et ne pas chevaucher sa voisine."),
    "E006": ("Fenêtre audio invalide",
             "La tranche demandée pour l'écoute est vide ou inversée : le début "
             "doit être strictement avant la fin, dans la durée du fichier."),
    "E007": ("Transcription cloud indisponible",
             "Vérifiez la clé API (variable d'environnement RYTHMO_OPENAI_KEY ou "
             "option asr_cle), la connexion et votre quota ; le mode « auto » "
             "bascule tout seul sur la transcription locale."),
    "E008": ("Format de projet non pris en charge",
             "Le fichier de projet a une version inconnue ou plus récente que "
             "cette application : mettez à jour l'application, ou ouvrez-le "
             "avec la version qui l'a créé puis ré-exportez."),
    "E010": ("Import Wikipédia impossible",
             "Vérifiez le titre ou l'URL, la connexion, puis réessayez."),
    "E011": ("Page Wikipédia introuvable",
             "Vérifiez l'orthographe du titre, ou choisissez l'autre langue."),
    "E999": ("Erreur interne inattendue",
             "Consultez le journal du serveur ; si le problème persiste, "
             "signalez-le avec la vidéo concernée."),
}


def format_user_error(code: str, detail: str = "") -> str:
    """Message complet et actionnable : titre + suggestion (+ détail technique)."""
    titre, suggestion = CODES_ERREUR.get(code, ("Erreur inattendue", "Réessayez ; "
                                                "si le problème persiste, redémarrez l'application."))
    if code not in CODES_ERREUR:
        titre = f"Erreur inattendue ({code})"
    base = f"{titre} Suggestion : {suggestion}"
    return f"{base} [Détail : {detail}]" if detail else base



class RythmoError(Exception):
    """Erreur applicative portant un ``code`` stable et un message utilisateur en français."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __repr__(self) -> str:  # pragma: no cover - confort de débogage
        return f"RythmoError({self.code!r}, {self.message!r})"
