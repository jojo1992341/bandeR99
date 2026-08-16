"""Routes de la couche de traduction (slices 1, 2, 7 et 9).

- ``GET  /api/jobs/{id}/traductions`` : lit la couche persistée + progression ;
- ``POST /api/jobs/{id}/traductions`` : lance une passe asynchrone (lots) via le
  ``TranslationController`` — progression X/Y, pause/reprise/annulation ;
- ``POST …/traductions/pause`` / ``…/reprendre`` / ``…/annuler`` : pilotent la
  passe en cours ;
- ``PUT  /api/jobs/{id}/traductions/{rid}`` : édition manuelle (``target_text``),
  verrouillage, exclusion, choix d'un candidat ;
- ``POST …/traductions/retraduire`` : retraduction ciblée (``probleme`` transmis
  au moteur), en sautant les répliques verrouillées/exclues ;
- ``POST …/traductions/rendre`` : rend la bande traduite (mêmes timecodes) ;
- ``GET  …/traductions/srt`` : sous-titres ``.srt`` traduits.

Le job doit être éditable (``pret_edition`` ou ``termine``), comme les routes
``/repliques`` existantes. La traduction n'altère jamais ``statut`` du job ni
``repliques.json`` (couche séparée ``traduction.json``).
"""
from __future__ import annotations

import json
import threading

from fastapi import HTTPException, Request, Response

from ..errors import format_user_error
from .controleur import TranslationController
from .engine import obtenir_moteur
from .score import DubbingScorer
from .stockage import TraductionStore
from .syllabes import SyllableAnalyzer
from .traducteur import DubbingTranslator

STATUTS_EDITABLES = ("pret_edition", "termine")
PROGRESSION_INITIALE = {"statut": "en_attente", "fait": 0, "total": 0}
# États terminaux de la passe (controleur) : plus pilotable par pause/reprendre/annuler.
STATUTS_TERMINAUX = ("termine", "annule", "erreur")

_CONTROLEURS: dict[str, TranslationController] = {}


def _erreur_005(message: str) -> HTTPException:
    return HTTPException(400, {"code": "E005", "message": message,
                               "message_utilisateur": format_user_error("E005", message)})


def _verifier_job_editable(gestion, job_id: str):
    job = gestion.jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Job inconnu")
    with job.verrou:
        statut = job.statut
    if statut not in STATUTS_EDITABLES:
        raise HTTPException(409, f"Ce job n'accepte pas la traduction "
                                 f"(statut « {statut} »)")
    return job


def _scope_repliques(repliques: list, selection: list | None) -> list:
    """Répliques à traduire, dans l'ordre du payload (``selection`` → sous-ensemble)."""
    if not selection:
        return list(repliques)
    ids = set(selection)
    return [r for r in repliques if r.get("id") in ids]


def _construire_traducteur(corps: dict) -> DubbingTranslator:
    """Construit un traducteur à partir du corps de la requête (config, jamais en dur).

    ``url`` / ``cle_api`` / ``modele_api`` (slice 2) configurent le moteur
    distant (« API compatible OpenAI ») : une URL manquante ou invalide pour
    un moteur distant → 400 E005, sans jamais toucher ``traduction.json``.
    """
    langue_source = str(corps.get("langue_source") or "en")
    langue_cible = str(corps.get("langue_cible") or "fr")
    modele = str(corps.get("modele") or "deterministe")
    poids = corps.get("poids") if isinstance(corps.get("poids"), dict) else None
    try:
        nombre_candidats = int(corps.get("nombre_candidats", 3))
        seuil_score = float(corps.get("seuil_score", 85.0))
        temperature = (float(corps["temperature"])
                       if corps.get("temperature") is not None else None)
    except (TypeError, ValueError):
        raise _erreur_005("nombre_candidats / seuil_score / temperature invalides") from None
    url = str(corps.get("url") or "").strip()
    cle_api = str(corps.get("cle_api") or "").strip()
    modele_api = str(corps.get("modele_api") or "").strip()
    if modele == "openai_compatible":
        if not url:
            raise _erreur_005("Le moteur « API compatible OpenAI » exige une URL "
                              "de serveur (champ « URL ») — ex. http://localhost:1234/v1")
        if not url.startswith(("http://", "https://")):
            raise _erreur_005(f"URL de serveur invalide : « {url} » "
                              f"(http:// ou https:// attendu)")
    config = {"url": url, "cle_api": cle_api, "modele": modele_api}
    try:
        moteur = obtenir_moteur(modele, config)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return DubbingTranslator(moteur, langue_source, langue_cible,
                             DubbingScorer(poids),
                             nombre_candidats=nombre_candidats,
                             seuil_score=seuil_score,
                             temperature=temperature)


def _entree_par_defaut(source_text: str, langue: str) -> dict:
    """Entrée minimale de la couche, avant toute traduction."""
    return {
        "source_text": source_text, "target_text": "", "statut": "en_attente",
        "source_syllabes": SyllableAnalyzer(langue).compter(source_text),
        "target_syllabes": 0, "score_global": 0.0, "scores": {},
        "candidats": [], "iteration_count": 0, "erreur": "",
        "explications": [], "source_phonemes": [], "target_phonemes": [],
        "verrouillee": False, "exclue": False,
    }


def enregistrer_routes(appli, gestion) -> None:
    """Enregistre les routes de traduction dans l'application FastAPI."""

    @appli.get("/api/jobs/{job_id}/traductions")
    def lire_traductions(job_id: str):
        job = _verifier_job_editable(gestion, job_id)
        couche = TraductionStore(job.job_dir).lire()
        ctl = _CONTROLEURS.get(job_id)
        # ``ctl.etat()`` porte le statut vivant de la passe (en_cours/termine…) ;
        # la progression persistée (fait/total) n'a pas ce statut — elle ne doit
        # donc jamais écraser celle du contrôleur.
        progression = ctl.etat() if ctl else couche.get("progression",
                                                        dict(PROGRESSION_INITIALE))
        return {**couche, "job_id": job_id, "progression": progression}

    @appli.post("/api/jobs/{job_id}/traductions", status_code=202)
    async def traduire_repliques(job_id: str, request: Request):
        from ..edition import lire_repliques

        job = _verifier_job_editable(gestion, job_id)
        try:
            corps = await request.json()
        except json.JSONDecodeError:
            raise _erreur_005("Corps JSON illisible") from None
        corps = corps if isinstance(corps, dict) else {}
        selection = corps.get("repliques") if isinstance(corps.get("repliques"), list) else None
        payload = lire_repliques(job.job_dir)
        repliques = _scope_repliques(payload.get("repliques", []), selection)
        traducteur = _construire_traducteur(corps)
        langue_source = traducteur.langue_source
        langue_cible = traducteur.langue_cible
        modele = str(corps.get("modele") or "deterministe")
        ctl = TranslationController(traducteur, TraductionStore(job.job_dir),
                                    repliques, langue_source, langue_cible, modele)
        _CONTROLEURS[job_id] = ctl
        threading.Thread(target=ctl.executer, daemon=True).start()
        return {"job_id": job_id, "statut": "traduction_lancee"}

    @appli.put("/api/jobs/{job_id}/traductions/{rid}")
    async def editer_traduction(job_id: str, rid: str, request: Request):
        """Édition manuelle : ``target_text``, ``verrouillee``, ``exclue``, ``candidat``."""
        from ..edition import lire_repliques

        job = _verifier_job_editable(gestion, job_id)
        try:
            corps = await request.json()
        except json.JSONDecodeError:
            raise _erreur_005("Corps JSON illisible") from None
        corps = corps if isinstance(corps, dict) else {}
        payload = lire_repliques(job.job_dir)
        replique = next((r for r in payload.get("repliques", [])
                         if str(r.get("id", "")) == rid), None)
        if replique is None:
            raise HTTPException(404, "Réplique inconnue")
        source_text = str(replique.get("texte", ""))
        store = TraductionStore(job.job_dir)
        couche = store.lire()
        langue_cible = str(couche.get("langue_cible") or corps.get("langue_cible") or "fr")
        entrees = dict(couche.get("entrees", {}))
        entree = dict(entrees.get(rid, {}) or _entree_par_defaut(source_text, langue_cible))
        if "candidat" in corps and corps["candidat"] is not None:
            try:
                index = int(corps["candidat"])
            except (TypeError, ValueError):
                raise _erreur_005("candidat invalide") from None
            candidats = entree.get("candidats", [])
            if not 0 <= index < len(candidats):
                raise _erreur_005(f"Candidat hors bornes : {index}")
            choisi = candidats[index]
            entree["target_text"] = choisi["texte"]
            entree["score_global"] = choisi.get("score_global", 0.0)
            entree["scores"] = dict(choisi.get("scores", {}))
            entree["target_syllabes"] = SyllableAnalyzer(langue_cible).compter(choisi["texte"])
            entree["statut"] = "traduit"
        if "target_text" in corps and corps["target_text"] is not None:
            entree["target_text"] = str(corps["target_text"])
            entree["target_syllabes"] = SyllableAnalyzer(langue_cible).compter(
                entree["target_text"])
            entree["statut"] = "traduit"
        if "verrouillee" in corps:
            entree["verrouillee"] = bool(corps["verrouillee"])
        if "exclue" in corps:
            entree["exclue"] = bool(corps["exclue"])
        entrees[rid] = entree
        couche["entrees"] = entrees
        store.ecrire(couche)
        return {"job_id": job_id, "rid": rid, **entree}

    @appli.post("/api/jobs/{job_id}/traductions/retraduire")
    async def retraduire_repliques(job_id: str, request: Request):
        """Retraduction ciblée (synchrone) : les verrouillées/exclues sont sautées."""
        from ..edition import lire_repliques

        job = _verifier_job_editable(gestion, job_id)
        try:
            corps = await request.json()
        except json.JSONDecodeError:
            raise _erreur_005("Corps JSON illisible") from None
        corps = corps if isinstance(corps, dict) else {}
        probleme = str(corps.get("probleme") or "").strip() or None
        selection = corps.get("repliques") if isinstance(corps.get("repliques"), list) else None
        traducteur = _construire_traducteur(corps)
        payload = lire_repliques(job.job_dir)
        repliques = _scope_repliques(payload.get("repliques", []), selection)
        store = TraductionStore(job.job_dir)
        couche = store.lire()
        entrees = dict(couche.get("entrees", {}))
        for replique in repliques:
            rid = str(replique.get("id", ""))
            existante = entrees.get(rid, {})
            if existante.get("verrouillee") or existante.get("exclue"):
                continue  # verrouillée/exclue : jamais retouchée
            entrees[rid] = traducteur.traduire(replique, probleme=probleme).to_dict()
        couche["entrees"] = entrees
        store.ecrire(couche)
        return {**couche, "job_id": job_id}

    def _texte_cible(job) -> dict:
        """Mapping ``rid → target_text`` depuis la couche persistée (non vides)."""
        couche = TraductionStore(job.job_dir).lire()
        return {rid: e.get("target_text") for rid, e in couche.get("entrees", {}).items()
                if e.get("target_text")}

    @appli.post("/api/jobs/{job_id}/traductions/rendre", status_code=202)
    def rendre_traduction(job_id: str):
        """Rend la bande traduite (mêmes timecodes) — l'original reste intact."""
        job = _verifier_job_editable(gestion, job_id)
        texte_cible = _texte_cible(job)
        if not texte_cible:
            raise HTTPException(409, "Aucune traduction disponible pour ce job : "
                                     "lancez d'abord une passe de traduction.")
        gestion.executer_rendu(job_id, texte_cible=texte_cible)
        return {"job_id": job_id, "statut": "rendu_en_cours"}

    @appli.get("/api/jobs/{job_id}/traductions/srt")
    def sous_titres_traduits(job_id: str):
        """Sous-titres ``.srt`` traduits : texte cible + horodatages d'origine."""
        from ..edition import appliquer_textes_cibles, lire_repliques
        from ..srt_export import generer_srt

        job = _verifier_job_editable(gestion, job_id)
        texte_cible = _texte_cible(job)
        if not texte_cible:
            raise HTTPException(409, "Aucune traduction disponible pour ce job : "
                                     "lancez d'abord une passe de traduction.")
        payload = appliquer_textes_cibles(lire_repliques(job.job_dir), texte_cible)
        srt = generer_srt(payload)
        nom = f"rythmo_{job.id}_traduit.srt"
        return Response(srt, media_type="application/x-subrip",
                        headers={"Content-Disposition":
                                 f'attachment; filename="{nom}"'})

    def _ctl_requis(job_id: str) -> TranslationController:
        _verifier_job_editable(gestion, job_id)
        ctl = _CONTROLEURS.get(job_id)
        if ctl is None:
            raise HTTPException(409, "Aucune passe de traduction en cours pour ce job")
        statut = ctl.etat()["statut"]
        if statut in STATUTS_TERMINAUX:
            raise HTTPException(409, f"La passe de traduction est déjà terminée "
                                     f"(statut « {statut} »)")
        return ctl

    @appli.post("/api/jobs/{job_id}/traductions/pause")
    def pause_traduction(job_id: str):
        ctl = _ctl_requis(job_id)
        ctl.mettre_en_pause()
        return {"job_id": job_id, "statut": ctl.etat()["statut"]}

    @appli.post("/api/jobs/{job_id}/traductions/reprendre")
    def reprendre_traduction(job_id: str):
        ctl = _ctl_requis(job_id)
        ctl.reprendre()
        return {"job_id": job_id, "statut": ctl.etat()["statut"]}

    @appli.post("/api/jobs/{job_id}/traductions/annuler")
    def annuler_traduction(job_id: str):
        ctl = _ctl_requis(job_id)
        ctl.annuler()
        return {"job_id": job_id, "statut": ctl.etat()["statut"]}
