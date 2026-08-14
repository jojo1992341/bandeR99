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
import shutil
from pathlib import Path

from .asr import transcribe_words
from .compose import compose_final
from .cues import build_cues
from .devices import choose_device
from .ingest import extract_audio, probe_video
from .lips import detect_mouth_track
from .render import StyleBande, taille_police_auto

PARAMS_DEFAUT = {
    "langue": None,            # None = détection automatique
    "modele": "base",          # tiny / base / small / medium / large-v3
    "aligner_whisperx": True,  # alignement forcé si dispo (repli natif sinon)
    "lipsync": True,
    "style": "RYTHMO",         # ou REPLIQUE
    "theme": "STUDIO",         # bande claire façon pro (ou SOMBRE, look karaoké)
    "hauteur_bande": 110,
    "taille_police": None,     # None = auto (≈ largeur/25) pour RYTHMO, 44 sinon
    "curseur_ratio": 0.15,     # position du curseur RYTHMO (fraction de la largeur)
    "vitesse": None,           # None = auto 0,32 (mesuré réf. pro) largeur/s RYTHMO
    "etirer_mots": True,       # RYTHMO : syllabes allongées étirées sur la piste
    "diariser": True,          # séparation automatique des voix (pistes distinctes)
    "asr": "local",            # local | cloud (strict) | auto (repli local)
    "modele_cloud": "whisper-1",  # modèle de l'API cloud
    "edition": False,          # True = pause après analyse pour édition manuelle
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

    def _transcrire():
        if info.duration > 60:  # vidéos longues : fenêtres glissantes fusionnées
            from .asr import transcribe_chunked
            return transcribe_chunked(wav, language=params["langue"] or None,
                                      model_name=params["modele"])
        return transcribe_words(wav, language=params["langue"] or None,
                                model_name=params["modele"],
                                affiner=params["aligner_whisperx"])

    if params["asr"] == "local":
        mots, langue = obtenir_transcription(wav, params["modele"], params["langue"],
                                             _transcrire)
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
                                         _transcrire)

        mots, langue, source_asr = transcrire_avec_repli(
            wav, params["langue"], params["asr"], _cloud, _locale,
            cle=params.get("asr_cle"))
    progresser(55, f"transcription {langue or '?'} : {len(mots)} mots"
                  + (f" (source {source_asr})" if source_asr != "local" else ""))

    labial = None
    if params["lipsync"]:
        try:
            progresser(60, "analyse des lèvres")
            labial = detect_mouth_track(chemin_video, fps_echantillon=12)
        except Exception:
            labial = None  # repli : timing audio seul

    progresser(72, "découpage des répliques")
    cues = build_cues(mots, pause_seuil=0.6, max_caracteres=60, max_duree=6.0)
    if params["diariser"]:
        from .diarisation import diariser_repliques_avec_repli
        from .diarisation_pyannote import diariser_repliques_pyannote

        # pyannote (studio) d'abord, puis Resemblyzer (même tessiture), puis
        # hauteur (T56) — chaque niveau retombe proprement sur le suivant
        labels = diariser_repliques_pyannote(cues, wav)
        if labels is not None:
            progresser(73, "séparation automatique des voix (pyannote)")
        else:
            progresser(73, "séparation automatique des voix")
            labels = diariser_repliques_avec_repli(cues, wav)
        for cue, lab in zip(cues, labels):
            cue.personnage = lab
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
                       "fin": round(w.end, 3)} for w in c.words]}
        if c.personnage is not None:
            r["personnage"] = c.personnage
        return r

    nom_source = f"source{chemin_video.suffix.lower() or '.mp4'}"
    shutil.copyfile(chemin_video, job_dir / nom_source)
    payload = {
        "version": VERSION_REPLIQUES,
        "duree_video": info.duration,
        "langue": langue,
        "style": params["style"],
        "nb_personnages": len({c.personnage for c in cues
                               if c.personnage is not None}),
        "repliques": [_payload_replique(i, c) for i, c in enumerate(cues)],
    }
    ecrire_repliques(job_dir, payload)
    (job_dir / "params.json").write_text(
        json.dumps({"params": params, "source": nom_source,
                    "lipsync_actif": labial is not None,
                    "asr_source": source_asr},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    return {"info": info, "cues": cues, "langue": langue,
            "labial": labial is not None, "nb_mots": len(mots), "payload": payload,
            "asr_source": source_asr}


def _rendre(job_dir: Path, chemin_video: Path, params: dict, cues, langue: str | None,
            lipsync_actif: bool, nb_mots: int, info, progresser,
            registrer_processus=None) -> Path:
    """Phase 2 : rendu de la bande + assemblage ffmpeg + méta ``cues.json``."""
    progresser(80, "rendu de la bande rythmo")
    taille = params.get("taille_police")
    if not taille:  # auto (largeur + hauteur de bande, T52) pour le défilant ;
        # base 44 pour RÉPLIQUE (autofit)
        if params["style"] == "RYTHMO":
            taille = taille_police_auto(info.width,
                                        hauteur_bande=params["hauteur_bande"])
        else:
            taille = 44
    vitesse = params.get("vitesse")
    style = StyleBande(style=params["style"], theme=params.get("theme", "STUDIO"),
                       hauteur_bande=params["hauteur_bande"], taille_police=taille,
                       curseur_ratio=float(params.get("curseur_ratio", 0.15)),
                       etirer_mots=bool(params.get("etirer_mots", True)),
                       **({"vitesse_ratio": float(vitesse)} if vitesse else {}))
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
             "mots": [{"texte": w.text.strip(), "debut": w.start, "fin": w.end}
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
    params = {**PARAMS_DEFAUT, **(params or {})}
    job_dir = Path(job_dir)
    chemin_video = Path(chemin_video)

    ctx = _analyser(job_dir, chemin_video, params, progresser)
    if params["edition"]:
        progresser(78, "répliques prêtes : correction possible avant rendu")
        return None
    return _rendre(job_dir, chemin_video, params, ctx["cues"], ctx["langue"],
                   ctx["labial"], ctx["nb_mots"], ctx["info"], progresser,
                   registrer_processus=registrer_processus)


def reprendre_rendu(job_dir: Path, progresser, registrer_processus=None) -> Path:
    """Phase 2 rejouée depuis le dossier du job : répliques (éditées) → MP4 final.

    Les répliques sont revalidées : un fichier corrompu/invalide lève E005.
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
    cues = payload_vers_cues(payload)
    info = probe_video(source)
    return _rendre(job_dir, source, params, cues, payload.get("langue"),
                   lipsync_actif=bool(cfg.get("lipsync_actif")),
                   nb_mots=sum(len(c.words) for c in cues),
                   info=info, progresser=progresser,
                   registrer_processus=registrer_processus)
