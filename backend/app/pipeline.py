"""Chaîne de traitement complète : vidéo → transcription → sync labiale → bande rythmo.

Deux phases, combinables :
- ``traiter_job``    : analyse IA puis rendu enchaîné (one-shot, historique) ;
                       avec ``params["edition"]`` à True, pause après l'analyse
                       (``repliques.json`` écrit, retour ``None``) pour édition.
- ``reprendre_rendu`` : relecture du dossier du job (répliques corrigées ou non)
                       et production de ``final.mp4`` + ``cues.json``.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from .compose import compose_final
from .vocabulaire import vocabulaire_du_projet

EXTENSIONS_VIDEO = (".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm")


def _nom_source_sur(chemin_video: Path, nom_fichier: str | None) -> str:
    """Nom de fichier sûr pour la vidéo source copiée dans le dossier du job.

    Conserve le **nom d'origine** du fichier uploadé (la liste des projets
    reste parlante) en le durcissant :
    - seul le nom de base est gardé (aucun chemin, ``..`` neutralisé) ;
    - caractères illégaux sous Windows remplacés par ``_`` ;
    - extension vidéo d'origine conservée, sinon celle de ``chemin_video`` ;
    - repli ``source.ext`` si le nom est vide ou réduit à des points.
    """
    suffixe = chemin_video.suffix.lower() or ".mp4"
    if nom_fichier:
        base = Path(str(nom_fichier).replace("\\", "/")).name
        base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).strip().strip(".")
        if base:
            base = base[:120]  # borne de longueur raisonnable
            if Path(base).suffix.lower() in EXTENSIONS_VIDEO:
                return base
            return base + suffixe
    return f"source{suffixe}"


def transcribe_words(*args, **kwargs):
    """Point d'injection ASR conservant les monkeypatchs et le repli local.

    L'import tardif permet au pipeline d'utiliser la version effectivement
    configurée dans ``app.asr`` (et évite qu'un cache de module ne fige une
    ancienne fonction pendant une session serveur).
    """
    from . import asr

    return asr.transcribe_words(*args, **kwargs)


def transcribe_chunked(*args, **kwargs):
    """Même contrat que :func:`transcribe_words` pour les vidéos longues."""
    from . import asr

    return asr.transcribe_chunked(*args, **kwargs)
from .cues import build_cues
from .devices import choose_device
from .ingest import extract_audio, probe_video
from .lips import detect_mouth_track
from .render import construire_style, taille_police_auto

PARAMS_DEFAUT = {
    "langue": None,            # None = détection automatique
    "modele": "medium",        # tiny / base / small / medium / large-v3
    "vocabulaire": [],         # noms propres/mots du projet pour le prompt FR
    "aligner_whisperx": True,  # alignement forcé si dispo (repli natif sinon)
    "lipsync": True,
    "style": "RYTHMO",         # ou REPLIQUE
    "theme": "STUDIO",         # bande claire façon pro (ou SOMBRE, look karaoké)
    "hauteur_bande": 110,
    "taille_police": None,     # None = auto (≈ largeur/25) pour RYTHMO, 44 sinon
    "taille_police_min": None,  # None = max(14, 0,22·hauteur_bande) ; jamais illisible
    "curseur_ratio": 0.15,     # position du curseur RYTHMO (fraction de la largeur)
    "vitesse": None,           # None = auto 0,32 (mesuré réf. pro) largeur/s RYTHMO
    "etirer_mots": True,       # RYTHMO : syllabes allongées étirées sur la piste
    "diariser": True,          # séparation automatique des voix (pistes distinctes)
    "asr": "local",            # local | cloud (strict) | auto (repli local)
    "modele_cloud": "whisper-1",  # modèle de l'API cloud
    "edition": False,          # True = pause après analyse pour édition manuelle
    # Réglages de segmentation « qualité studio » : les marges ne touchent
    # jamais aux timings des mots, elles élargissent seulement la fenêtre visuelle.
    "max_caracteres_cue": 80,
    "max_duree_cue": 6.0,
    "marge_cue_avant": 0.08,
    "marge_cue_apres": 0.12,
    "split_cue_ponctuation": True,
}


def _analyser(job_dir: Path, chemin_video: Path, params: dict, progresser) -> dict:
    """Phase 1 : tout l'IA. Renvoie le contexte et persiste repliques/params/source."""
    from .edition import VERSION_REPLIQUES, ecrire_repliques

    progresser(3, "lecture de la vidéo")
    info = probe_video(chemin_video)

    progresser(8, "extraction audio")
    wav = extract_audio(chemin_video, job_dir / "audio_16k.wav")

    progresser(20, "transcription IA locale")
    from .cache import obtenir_transcription

    vocabulaire = vocabulaire_du_projet(job_dir, params.get("vocabulaire"))

    def _transcrire():
        if info.duration > 60:  # vidéos longues : fenêtres glissantes fusionnées
            return transcribe_chunked(wav, language=params["langue"] or None,
                                      model_name=params["modele"],
                                      vocabulaire=vocabulaire)
        return transcribe_words(wav, language=params["langue"] or None,
                                model_name=params["modele"],
                                affiner=params["aligner_whisperx"],
                                vocabulaire=vocabulaire)

    if params["asr"] == "local":
        mots, langue = obtenir_transcription(wav, params["modele"], params["langue"],
                                             _transcrire, vocabulaire=vocabulaire)
        source_asr = "local"
    else:  # cloud (strict) ou auto (repli local) — T76–T78
        from .asr_cloud import cle_cloud, transcrire_avec_repli, transcrire_cloud

        modele_cloud = params.get("modele_cloud", "whisper-1")

        def _cloud():
            return obtenir_transcription(
                wav, f"cloud:{modele_cloud}", params["langue"],
                lambda: transcrire_cloud(wav, language=params["langue"] or None,
                                         cle=params.get("asr_cle") or cle_cloud(),
                                         modele=modele_cloud))

        def _locale():
            return obtenir_transcription(wav, params["modele"], params["langue"],
                                         _transcrire, vocabulaire=vocabulaire)

        mots, langue, source_asr = transcrire_avec_repli(
            wav, params["langue"], params["asr"], _cloud, _locale,
            cle=params.get("asr_cle"))
    # Slice 16 : drapeau « incertain » des mots à basse confiance, exporté
    # dans le payload des répliques (texte et timestamps inchangés).
    from .asr import marquer_mots_incertains

    mots = marquer_mots_incertains(mots)
    progresser(55, f"transcription {langue or '?'} : {len(mots)} mots"
                  + (f" (source {source_asr})" if source_asr != "local" else ""))

    labial = None
    if params["lipsync"]:
        try:
            progresser(60, "analyse des lèvres")
            labial = detect_mouth_track(chemin_video, fps_echantillon=12)
        except Exception:
            labial = None  # repli : timing audio seul

    # La diarisation doit précéder le découpage : si on étiquette des cues déjà
    # fusionnés, un changement de voix rapide devient une seule réplique et la
    # bande perd la piste du bon comédien. On conserve une étiquette par mot,
    # puis build_cues coupe uniquement aux changements réellement observés.
    speaker_labels = None
    diarisation_source = None
    if params["diariser"] and mots:
        from .diarisation import diariser_mots, diariser_mots_embeddings
        from .diarisation_pyannote import (etiqueter_mots_pyannote,
                                           obtenir_tours_pyannote)

        def _repli_diarisation():
            """Resemblyzer (timbre), puis hauteur si aucun split validé."""
            labels_embeddings = diariser_mots_embeddings(mots, wav)
            labels_hauteur = None
            if labels_embeddings is None or len(set(labels_embeddings)) < 2:
                # Un encodeur peut réussir techniquement mais ne rien séparer
                # (mots trop courts, timbres proches). La hauteur est alors un
                # repli utile pour les dialogues dont les tessitures diffèrent.
                labels_hauteur = diariser_mots(mots, wav)
            if labels_embeddings is not None and len(set(labels_embeddings)) >= 2:
                return labels_embeddings, "resemblyzer"
            return (labels_hauteur if labels_hauteur is not None else labels_embeddings), "hauteur"

        tours = obtenir_tours_pyannote(wav)
        if tours:
            candidat = etiqueter_mots_pyannote(mots, tours)
            # Un modèle qui n'entend qu'une voix ne doit pas empêcher les
            # niveaux inférieurs de récupérer un dialogue distinct ; on ne
            # considère pyannote « validé » que s'il sépare effectivement.
            if candidat is not None and len(set(candidat)) >= 2:
                speaker_labels = candidat
                diarisation_source = "pyannote"
        if speaker_labels is None:
            speaker_labels, diarisation_source = _repli_diarisation()
            progresser(70, "séparation automatique des voix"
                       + f" ({diarisation_source})")
        else:
            progresser(70, "séparation automatique des voix (pyannote)")

    progresser(72, "découpage des répliques")
    cues = build_cues(
        mots,
        pause_seuil=0.6,
        max_caracteres=int(params.get("max_caracteres_cue", 80)),
        max_duree=float(params.get("max_duree_cue", 6.0)),
        speaker_labels=speaker_labels,
        split_on_punctuation=bool(params.get("split_cue_ponctuation", True)),
        marge_avant=max(0.0, float(params.get("marge_cue_avant", 0.08))),
        marge_apres=max(0.0, float(params.get("marge_cue_apres", 0.12))),
    )
    if labial is not None:
        from .lips import align_cues_to_mouth, find_speech_onsets

        onsets = find_speech_onsets(labial)
        if onsets:
            decales = sum(
                1 for c in cues
                if min((abs(o - c.start) for o in onsets), default=1.0) <= 0.25
            )
            progresser(75, f"sync labiale : {decales} réplique(s) calée(s)")
            cues = align_cues_to_mouth(cues, onsets, decalage_max=0.25)

    # persistance de reprise : source copiée + params figés + payload éditable
    def _payload_replique(i: int, c) -> dict:
        r = {"id": f"r{i}", "texte": c.text,
             "debut": round(c.start, 3), "fin": round(c.end, 3),
             "mots": [{"texte": w.text.strip(), "debut": round(w.start, 3),
                       "fin": round(w.end, 3),
                       **({"marqueur": True} if w.marqueur else {}),
                       **({"incertain": True} if w.incertain else {})}
                      for w in c.words]}
        if c.personnage is not None:
            r["personnage"] = c.personnage
        return r

    nom_source = _nom_source_sur(chemin_video, params.get("nom_fichier"))
    shutil.copyfile(chemin_video, job_dir / nom_source)
    nb_personnages = len({c.personnage for c in cues
                          if c.personnage is not None})
    payload = {
        "version": VERSION_REPLIQUES,
        "duree_video": info.duration,
        "langue": langue,
        "style": params["style"],
        "nb_personnages": nb_personnages,
        # Noms neutres : l'éditeur peut les remplacer sans toucher aux
        # identifiants numériques persistés dans chaque réplique.
        "personnages": [f"Voix {i + 1}" for i in range(nb_personnages)],
        "repliques": [_payload_replique(i, c) for i, c in enumerate(cues)],
    }
    ecrire_repliques(job_dir, payload)
    (job_dir / "params.json").write_text(
        json.dumps({"params": params, "source": nom_source,
                    "lipsync_actif": labial is not None,
                    "asr_source": source_asr,
                    "diarisation_source": diarisation_source},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    return {"info": info, "cues": cues, "langue": langue,
            "labial": labial is not None, "nb_mots": len(mots), "payload": payload,
            "asr_source": source_asr, "diarisation_source": diarisation_source}


def _rendre(job_dir: Path, chemin_video: Path, params: dict, cues, langue: str | None,
            lipsync_actif: bool, nb_mots: int, info, progresser,
            registrer_processus=None) -> Path:
    """Phase 2 : rendu de la bande + assemblage ffmpeg + méta ``cues.json``."""
    progresser(80, "rendu de la bande rythmo")
    taille = params.get("taille_police")
    if not taille:  # auto (largeur + hauteur de bande, T52) pour le défilant ;
        # base 44 pour RÉPLIQUE (autofit)
        if params["style"] == "RYTHMO":
            taille = taille_police_auto(
                info.width, hauteur_bande=params["hauteur_bande"],
                taille_min=params.get("taille_police_min"))
        else:
            taille = 44
    # T149 : `vitesse` numérique → vitesse constante explicite ; le sentinelle
    # « dynamique » → vitesse constante PAR RÉPLIQUE (ancres 1ᵉʳ/dernier mot) ;
    # None → défaut Auto 0,32 (comportement historique strict).
    style = construire_style(params, taille)
    final = compose_final(chemin_video, cues, job_dir / "final.mp4", style,
                          registrer_processus=registrer_processus)

    meta = {
        "device": choose_device(),
        "langue": langue,
        "lipsync": lipsync_actif,
        "nb_mots": nb_mots,
        "style": params["style"],
        "video": {"largeur": info.width, "hauteur": info.height,
                  "fps": info.fps, "duree": info.duration},
        "repliques": [
            {"debut": c.start, "fin": c.end, "texte": c.text,
             "personnage": c.personnage,
             "mots": [{"texte": w.text.strip(), "debut": w.start, "fin": w.end,
                       **({"marqueur": True} if w.marqueur else {})}
                      for w in c.words]}
            for c in cues
        ],
    }
    (job_dir / "cues.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    progresser(100, "terminé")
    return final


def traiter_job(job_dir: Path, chemin_video: Path, params: dict, progresser,
                registrer_processus=None) -> Path | None:
    """Produit ``final.mp4`` + ``cues.json`` dans ``job_dir``. Retourne le MP4.

    Avec ``params["edition"]`` : pause après l'analyse (retour ``None``) ; le rendu
    est alors déclenché par :func:`reprendre_rendu` une fois les répliques validées.

    ``registrer_processus`` : hook optionnel recevant le ``Popen`` ffmpeg (pour kill
    en cas d'annulation) puis ``None`` une fois le rendu fini.
    """
    donnees = params or {}
    params = {**PARAMS_DEFAUT, **donnees}
    # Normalisation de la case front « étirer les syllabes » : le formulaire
    # envoie ``etirer``, le pipeline lit ``etirer_mots`` (défaut actif). La
    # valeur explicite du formulaire prime sur le défaut (jamais sur un
    # ``etirer_mots`` déjà fourni par un import de projet).
    if "etirer" in donnees and "etirer_mots" not in donnees:
        params["etirer_mots"] = bool(donnees["etirer"])
    job_dir = Path(job_dir)
    chemin_video = Path(chemin_video)

    ctx = _analyser(job_dir, chemin_video, params, progresser)
    if params["edition"]:
        progresser(78, "répliques prêtes : correction possible avant rendu")
        return None
    return _rendre(job_dir, chemin_video, params, ctx["cues"], ctx["langue"],
                   ctx["labial"], ctx["nb_mots"], ctx["info"], progresser,
                   registrer_processus=registrer_processus)


def reprendre_rendu(job_dir: Path, progresser, registrer_processus=None,
                    texte_cible: dict | None = None) -> Path:
    """Phase 2 rejouée depuis le dossier du job : répliques (éditées) → MP4 final.

    Les répliques sont revalidées : un fichier corrompu/invalide lève E005.
    ``texte_cible`` (optionnel) substitue le texte de chaque réplique par sa
    traduction — timecodes inchangés — pour rendre la bande traduite.
    """
    from .cues_edit import valider_repliques
    from .edition import chemin_params, lire_repliques, payload_vers_cues
    from .errors import RythmoError

    job_dir = Path(job_dir)
    cfg = json.loads(chemin_params(job_dir).read_text(encoding="utf-8"))
    params = {**PARAMS_DEFAUT, **cfg["params"], "edition": False}
    payload = lire_repliques(job_dir)
    # sécurité : même un fichier édité hors API repasse par la validation métier
    payload["repliques"] = valider_repliques(payload["repliques"],
                                             float(payload["duree_video"]))
    source = job_dir / cfg["source"]
    if not source.is_file():
        raise RythmoError("E001", f"Vidéo source introuvable pour la reprise : {source}")

    progresser(79, "relecture des répliques corrigées")
    cues = payload_vers_cues(payload, texte_cible=texte_cible)
    info = probe_video(source)
    return _rendre(job_dir, source, params, cues, payload.get("langue"),
                   lipsync_actif=bool(cfg.get("lipsync_actif")),
                   nb_mots=sum(len(c.words) for c in cues),
                   info=info, progresser=progresser,
                   registrer_processus=registrer_processus)
