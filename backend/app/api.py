"""API FastAPI : upload vidéo, suivi de job, récupération du MP4, front statique."""
from __future__ import annotations

import asyncio
import json
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .devices import diagnostic_device
from .errors import AnnulationDemandee, RythmoError, format_user_error
from .paths import safe_path

EXTENSIONS_VIDEO = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm"}


def _bloc_erreur(job: "Job") -> dict:
    return {"code": job.erreur_code, "message": job.erreur_message,
            "message_utilisateur": format_user_error(job.erreur_code or "E999",
                                                     job.erreur_message or "")}
FRONT_DIR = Path(__file__).resolve().parents[2] / "frontend"
DOSSIER_JOBS_DEFAUT = Path(__file__).resolve().parents[1] / "data" / "jobs"


def chemin_resultat(job_dir: Path) -> Path:
    return job_dir / "final.mp4"


@dataclass
class Job:
    id: str
    statut: str = "en_attente"      # en_attente | traitement | termine | annule | erreur
    progression: int = 0
    etape: str = ""
    erreur_code: str | None = None
    erreur_message: str | None = None
    job_dir: Path | None = None
    thread: threading.Thread | None = None
    verrou: threading.Lock = field(default_factory=threading.Lock)
    annulation: threading.Event = field(default_factory=threading.Event)
    sous_processus: object | None = None  # Popen courant (ffmpeg), tué en cas d'annulation

    def publier(self, progression: int, etape: str) -> None:
        with self.verrou:
            self.statut = "traitement" if progression < 100 else "termine"
            self.progression = progression
            self.etape = etape

    def progresser_ou_annuler(self, progression: int, etape: str) -> None:
        """Callback de progression passé au pipeline : lève si annulation demandée."""
        if self.annulation.is_set():
            raise AnnulationDemandee()
        self.publier(progression, etape)

    def definir_sous_processus(self, proc) -> None:
        with self.verrou:
            self.sous_processus = proc


class GestionJobs:
    """Registre en mémoire + exécution du pipeline dans un pool de threads."""

    def __init__(self, jobs_dir: Path, nb_workers: int = 2):
        self.jobs_dir = jobs_dir
        self.jobs: dict[str, Job] = {}
        self.pool = ThreadPoolExecutor(max_workers=nb_workers)

    def soumettre(self, chemin_video: Path, params: dict) -> Job:
        job = Job(id=uuid.uuid4().hex[:12])
        job.job_dir = self.jobs_dir / job.id
        job.job_dir.mkdir(parents=True, exist_ok=True)
        self.jobs[job.id] = job
        self.pool.submit(self._executer, job, chemin_video, params)
        return job

    def _gerer_fin(self, job: Job, fonction) -> None:
        """Enrobe un appel worker : mapping exceptions → états du job."""
        try:
            fonction()
        except AnnulationDemandee:
            self._finaliser_annulation(job)
        except RythmoError as exc:
            if job.annulation.is_set():  # ffmpeg tué par l'annulation → état « annule »
                self._finaliser_annulation(job)
            else:
                with job.verrou:
                    job.statut, job.progression = "erreur", 100
                    job.erreur_code, job.erreur_message = exc.code, exc.message
                    job.etape = exc.message
        except Exception as exc:  # noqa: BLE001
            if job.annulation.is_set():
                self._finaliser_annulation(job)
                return
            with job.verrou:
                job.statut, job.progression = "erreur", 100
                job.erreur_code, job.erreur_message = "E999", str(exc)[:300]
                job.etape = str(exc)[:200]

    def _executer(self, job: Job, chemin_video: Path, params: dict) -> None:
        from . import pipeline  # import tardif : monkeypatchable par les tests

        def phase_unique_ou_analyse():
            job.publier(1, "démarrage")
            final = pipeline.traiter_job(job.job_dir, chemin_video, params,
                                         job.progresser_ou_annuler,
                                         registrer_processus=job.definir_sous_processus)
            if final is None:  # edition=True : l'analyse est finie, pause avant rendu
                with job.verrou:
                    job.statut = "pret_edition"
                    job.etape = "répliques prêtes : correction possible avant rendu"
            else:
                job.publier(100, "terminé")

        self._gerer_fin(job, phase_unique_ou_analyse)

    def executer_rendu(self, job_id: str, texte_cible: dict | None = None) -> Job | None:
        """Lance la phase 2 (après PUT des répliques éditées). Retourne le job.

        ``texte_cible`` (optionnel) rend la bande traduite : le texte de chaque
        réplique est substitué par sa traduction, timecodes inchangés.
        """
        job = self.jobs.get(job_id)
        if job is None:
            return None
        with job.verrou:
            job.statut, job.progression = "traitement", max(job.progression, 80)
            job.etape = "répliques validées : rendu en cours"
        self.pool.submit(self._executer_rendu, job, texte_cible)
        return job

    def _executer_rendu(self, job: Job, texte_cible: dict | None = None) -> None:
        from . import pipeline  # import tardif : monkeypatchable par les tests

        def phase_rendu():
            # rétro-compatible : le rendu original n'envoie pas ``texte_cible``
            # (les faux de test existants n'acceptent pas ce mot-clé).
            if texte_cible:
                pipeline.reprendre_rendu(
                    job.job_dir, job.progresser_ou_annuler,
                    registrer_processus=job.definir_sous_processus,
                    texte_cible=texte_cible)
            else:
                pipeline.reprendre_rendu(
                    job.job_dir, job.progresser_ou_annuler,
                    registrer_processus=job.definir_sous_processus)
            job.publier(100, "terminé")

        self._gerer_fin(job, phase_rendu)

    @staticmethod
    def _finaliser_annulation(job: Job) -> None:
        """État « annule » + nettoyage des fichiers partiels du rendu."""
        for nom in ("final.mp4", "audio_16k.wav"):
            try:
                safe_path(job.job_dir, nom).unlink(missing_ok=True)
            except OSError:
                pass
        with job.verrou:
            job.statut, job.etape = "annule", "annulé par l'utilisateur"

    def annuler(self, job_id: str) -> Job | None:
        """Demande l'annulation : flag coopératif + kill du sous-processus éventuel."""
        job = self.jobs.get(job_id)
        if job is None:
            return None
        job.annulation.set()
        with job.verrou:
            proc = job.sous_processus
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        return job

    # ——— reprise de projet (slice 3) : projets persistés réouvrables ———

    @staticmethod
    def _statut_persiste(dossier: Path) -> str:
        """Statut dérivé des fichiers : ``termine`` si le rendu existe, sinon édition."""
        return "termine" if (dossier / "final.mp4").is_file() else "pret_edition"

    def lister_projets(self) -> list[dict]:
        """Projets persistés disposant de répliques — réouvrables sans ré-analyse.

        Un dossier sans ``repliques.json`` (analyse interrompue, upload
        orphelin) n'est jamais proposé en « rouvrir ».
        """
        projets: list[dict] = []
        if not self.jobs_dir.is_dir():
            return projets
        for dossier in sorted(self.jobs_dir.iterdir()):
            if not dossier.is_dir():
                continue
            repliques = dossier / "repliques.json"
            if not repliques.is_file():
                continue
            params = {}
            try:
                params = json.loads((dossier / "params.json")
                                    .read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
            nom_source = str(params.get("source") or params.get("nom_source")
                             or "source.mp4")
            duree = 0.0
            try:
                duree = float(json.loads(repliques.read_text(encoding="utf-8"))
                              .get("duree_video", 0.0))
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                pass
            try:
                date = repliques.stat().st_mtime
            except OSError:
                date = 0.0
            projets.append({"id": dossier.name, "nom_source": nom_source,
                            "duree": duree, "date": date,
                            "statut": self._statut_persiste(dossier)})
        return projets

    def rouvrir(self, projet_id: str) -> Job | None:
        """Ré-hydrate un projet persisté en ``Job`` mémoire (sans relancer le pipeline).

        Le dossier existant devient le ``job_dir`` du Job : les routes
        répliques/audio/onde/srt répondent aussitôt. Idempotent ; un job déjà
        en mémoire (y compris en cours de traitement) n'est pas perturbé.
        """
        dossier = self.jobs_dir / projet_id
        if not (dossier / "repliques.json").is_file():
            return None
        job = self.jobs.get(projet_id)
        if job is None:
            job = Job(id=projet_id)
            job.job_dir = dossier
            self.jobs[projet_id] = job
        with job.verrou:
            if job.statut != "traitement":
                job.statut = self._statut_persiste(dossier)
                job.progression = 100 if job.statut == "termine" else 78
                job.etape = "projet rouvert depuis le disque — prêt à reprendre"
        return job

    def supprimer_projet(self, projet_id: str) -> str:
        """Supprime un projet : dossier + entrée mémoire. Retourne un état.

        ``"supprime"`` / ``"absent"`` / ``"en_cours"``. Jamais un job en cours
        (``en_attente``/``traitement``) : on ne retire pas les fichiers sous
        les pieds d'un worker actif. Le chemin est confiné par ``safe_path``.
        """
        job = self.jobs.get(projet_id)
        if job is not None:
            with job.verrou:
                if job.statut in ("en_attente", "traitement"):
                    return "en_cours"
            self.jobs.pop(projet_id, None)
        try:
            dossier = safe_path(self.jobs_dir, projet_id)
        except RythmoError:
            return "absent"
        if not dossier.is_dir():
            return "absent"
        shutil.rmtree(dossier)
        return "supprime"


def creer_app(jobs_dir: Path | None = None, max_upload_octets: int = 512 * 1024 * 1024
              ) -> FastAPI:
    """Fabrique l'application FastAPI (dossiers et plafonds injectables pour les tests)."""
    jobs_dir = Path(jobs_dir) if jobs_dir else DOSSIER_JOBS_DEFAUT
    jobs_dir.mkdir(parents=True, exist_ok=True)
    gestion = GestionJobs(jobs_dir)

    appli = FastAPI(title="Rythmo Dub", version=__version__)
    appli.state.jobs_dir = str(jobs_dir)
    appli.state.flux_sse_actifs = 0

    @appli.get("/api/health")
    def health():
        device, raison = diagnostic_device()
        return {"statut": "ok", "device": device, "device_raison": raison,
                "version": __version__,
                "flux_sse_actifs": appli.state.flux_sse_actifs}

    @appli.get("/api/projets")
    def lister_projets_route():
        """Projets persistés (répliques disponibles) — liste pour l'étape 01.

        Survit au redémarrage : scan de ``data/jobs``, sans ré-upload ni
        ré-analyse (slice 3). Un job sans répliques n'est pas proposé.
        """
        return {"projets": gestion.lister_projets()}

    @appli.post("/api/projets/{projet_id}/rouvrir")
    def rouvrir_projet_route(projet_id: str):
        """Ré-hydrate un projet analysé : répliques/timings/audio, sans pipeline."""
        job = gestion.rouvrir(projet_id)
        if job is None:
            raise HTTPException(404, "Projet inconnu (ou sans répliques)")
        with job.verrou:
            return {"job_id": job.id, "statut": job.statut,
                    "progression": job.progression}

    @appli.delete("/api/projets/{projet_id}")
    def supprimer_projet_route(projet_id: str):
        """Efface proprement un projet : dossier du job + entrée mémoire.

        Refuse (409) un job en cours de traitement ; 404 si inconnu. Le chemin
        est confiné au dossier des jobs (``safe_path`` → jamais d'effacement
        hors du répertoire autorisé).
        """
        resultat = gestion.supprimer_projet(projet_id)
        if resultat == "en_cours":
            raise HTTPException(409, "Projet en cours de traitement : "
                                     "impossible de le supprimer")
        if resultat == "absent":
            raise HTTPException(404, "Projet inconnu")
        return {"projet_id": projet_id, "statut": "supprime"}

    @appli.post("/api/jobs", status_code=202)
    async def creer_job(fichier: UploadFile = File(...), options: str = Form(default="{}")):
        nom = fichier.filename or "video.mp4"
        if Path(nom).suffix.lower() not in EXTENSIONS_VIDEO:
            raise HTTPException(415, f"Type non pris en charge : {nom}")
        try:
            params = json.loads(options) if options.strip() else {}
        except json.JSONDecodeError:
            raise HTTPException(400, "Paramètre 'options' : JSON invalide") from None
        if not isinstance(params, dict):
            params = {}
        # nom d'origine conservé : la liste des projets reste parlante
        # (sinon tout devient « source.mp4 » après copie dans le job)
        params["nom_fichier"] = nom

        tampon = safe_path(jobs_dir, f".upload_{uuid.uuid4().hex[:8]}{Path(nom).suffix.lower()}")
        taille = 0
        trop_gros = False
        try:
            with open(tampon, "wb") as sortie:
                while morceau := await fichier.read(1024 * 1024):
                    taille += len(morceau)
                    if taille > max_upload_octets:
                        # Sous Windows, le fichier est encore ouvert par le
                        # ``with`` : on reporte la suppression après sa fermeture
                        # (sinon WinError 32 masque le HTTP 413 attendu).
                        trop_gros = True
                        break
                    sortie.write(morceau)
        finally:
            await fichier.close()
        if trop_gros:
            tampon.unlink(missing_ok=True)
            raise HTTPException(413, f"Fichier trop lourd (> {max_upload_octets // (1024 * 1024)} Mo)")
        job = gestion.soumettre(tampon, params)
        return {"job_id": job.id, "statut": job.statut}

    @appli.get("/api/jobs/{job_id}")
    def statut_job(job_id: str):
        job = gestion.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Job inconnu")
        with job.verrou:
            corps = {"job_id": job.id, "statut": job.statut,
                     "progression": job.progression, "etape": job.etape}
            if job.statut == "erreur":
                corps["erreur"] = _bloc_erreur(job)
            return corps

    @appli.get("/api/jobs/{job_id}/events")
    def evenements(job_id: str, request: Request):
        if job_id not in gestion.jobs:
            raise HTTPException(404, "Job inconnu")

        async def flux():
            appli.state.flux_sse_actifs += 1
            try:
                while True:
                    job = gestion.jobs[job_id]
                    with job.verrou:
                        donnees = {"statut": job.statut, "progression": job.progression,
                                   "etape": job.etape}
                        if job.statut == "erreur":
                            donnees["erreur"] = _bloc_erreur(job)
                    yield f"data: {json.dumps(donnees)}\n\n"
                    if job.statut in ("termine", "erreur", "annule"):
                        break
                    await asyncio.sleep(0.4)
                    if await request.is_disconnected():
                        break  # onglet fermé : ne pas laisser de tâche fantôme
            finally:
                appli.state.flux_sse_actifs -= 1

        return StreamingResponse(flux(), media_type="text/event-stream")

    @appli.post("/api/jobs/{job_id}/cancel")
    def annuler_job(job_id: str):
        job = gestion.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Job inconnu")
        with job.verrou:
            statut = job.statut
        if statut in ("termine", "erreur", "annule"):
            raise HTTPException(409, f"Job déjà à l'état « {statut} »")
        gestion.annuler(job_id)
        if statut == "pret_edition":
            # pas de worker actif en pause édition : finalisation immédiate
            gestion._finaliser_annulation(job)
            return {"job_id": job_id, "statut": "annule"}
        return {"job_id": job_id, "statut": "annulation_demandee"}

    # statuts depuis lesquels on peut lire/corriger les répliques : pause édition
    # ou génération déjà terminée (le dossier du job conserve source + params)
    STATUTS_EDITABLES = ("pret_edition", "termine")

    @appli.get("/api/jobs/{job_id}/repliques")
    def lire_repliques_route(job_id: str):
        from .edition import lire_repliques

        job = gestion.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Job inconnu")
        with job.verrou:
            statut = job.statut
        if statut not in STATUTS_EDITABLES:
            raise HTTPException(409, f"Ce job n'a pas de répliques disponibles "
                                     f"(statut « {statut} »)")
        try:
            payload = lire_repliques(job.job_dir)
        except (RythmoError, json.JSONDecodeError):
            raise HTTPException(409, "Répliques indisponibles pour ce job "
                                     "(fichier absent ou corrompu)") from None
        return {"job_id": job_id, **payload}

    @appli.put("/api/jobs/{job_id}/repliques", status_code=202)
    async def appliquer_repliques_route(job_id: str, request: Request):
        from .edition import appliquer_edition

        job = gestion.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Job inconnu")
        with job.verrou:
            statut = job.statut
        if statut not in STATUTS_EDITABLES:
            raise HTTPException(409, f"Ce job n'accepte pas de corrections "
                                     f"(statut « {statut} »)")
        try:
            corps = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(400, {"code": "E005",
                                      "message": "Corps JSON illisible",
                                      "message_utilisateur": format_user_error(
                                          "E005", "Corps JSON illisible")}) from None
        repliques = corps.get("repliques") if isinstance(corps, dict) else None
        personnages = corps.get("personnages") if isinstance(corps, dict) else None
        try:
            appliquer_edition(job.job_dir, repliques, personnages)
        except RythmoError as exc:
            raise HTTPException(400, {"code": exc.code, "message": exc.message,
                                      "message_utilisateur": format_user_error(
                                          exc.code, exc.message)}) from None
        gestion.executer_rendu(job_id)
        return {"job_id": job_id, "statut": "rendu_en_cours"}

    @appli.get("/api/jobs/{job_id}/glossaire")
    def lire_glossaire_route(job_id: str):
        """Glossaire du job (termes pour la transcription) : ``{"termes": [...]}``."""
        from .traduction.glossaire import GlossaireStore

        job = gestion.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Job inconnu")
        with job.verrou:
            statut = job.statut
        if statut not in STATUTS_EDITABLES:
            raise HTTPException(409, f"Ce job n'a pas de glossaire disponible "
                                     f"(statut « {statut} »)")
        return {"job_id": job_id, "termes": GlossaireStore(job.job_dir).lire()}

    @appli.put("/api/jobs/{job_id}/glossaire")
    async def ecrire_glossaire_route(job_id: str, request: Request):
        """Remplace le glossaire du job ; alimente le vocabulaire de re-transcription."""
        from .traduction.glossaire import GlossaireStore

        job = gestion.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Job inconnu")
        with job.verrou:
            statut = job.statut
        if statut not in STATUTS_EDITABLES:
            raise HTTPException(409, f"Ce job n'accepte pas de glossaire "
                                     f"(statut « {statut} »)")
        try:
            corps = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(400, {"code": "E005",
                                      "message": "Corps JSON illisible",
                                      "message_utilisateur": format_user_error(
                                          "E005", "Corps JSON illisible")}) from None
        termes = corps.get("termes") if isinstance(corps, dict) else None
        if not isinstance(termes, list) or not all(isinstance(t, str) for t in termes):
            raise HTTPException(400, {
                "code": "E005",
                "message": "Glossaire invalide : champ « termes » (liste de chaînes) attendu",
                "message_utilisateur": format_user_error(
                    "E005", "Glossaire invalide : champ « termes » (liste de chaînes) attendu")})
        GlossaireStore(job.job_dir).ecrire(termes)
        return {"job_id": job_id, "termes": GlossaireStore(job.job_dir).lire()}

    @appli.post("/api/jobs/{job_id}/glossaire/candidats-wikipedia")
    async def candidats_wikipedia(job_id: str, request: Request):
        """Candidats de vocabulaire extraits d'une page Wikipédia (Slices 1–3).

        Ne modifie JAMAIS le glossaire : la route retourne des candidats que
        l'utilisateur sélectionne, puis la fusion passe par le PUT /glossaire.
        ``methode`` : ``wikipedia`` (heuristique, défaut) ou ``llm`` (moteur du
        panneau Traduire). ``langue`` : ``fr`` (défaut) / ``en``, avec repli
        automatique tant que la langue n'est pas fixée par une URL explicite.
        Avec ``methode=llm``, ``categories`` porte le classement
        ``{essentiels, difficiles, termes}`` et ``candidats`` la liste plate
        dédupliquée (les deux sont renvoyés) ; sinon ``categories`` est ``null``.
        """
        from .wikipedia_import import (ErreurImportWikipedia, aplanir_categories,
                                       construire_moteur_llm,
                                       extraire_termes_avec_repli, extraire_termes_llm,
                                       resoudre_saisie)

        job = gestion.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Job inconnu")
        with job.verrou:
            statut = job.statut
        if statut not in STATUTS_EDITABLES:
            raise HTTPException(409, f"Ce job n'accepte pas d'import de glossaire "
                                     f"(statut « {statut} »)")
        try:
            corps = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(400, {"code": "E005",
                                      "message": "Corps JSON illisible",
                                      "message_utilisateur": format_user_error(
                                          "E005", "Corps JSON illisible")}) from None
        corps = corps if isinstance(corps, dict) else {}
        saisie = str(corps.get("titre") or corps.get("url") or "").strip()
        if not saisie:
            raise HTTPException(400, {
                "code": "E005",
                "message": "Titre ou URL Wikipédia requis",
                "message_utilisateur": format_user_error(
                    "E005", "Titre ou URL Wikipédia requis")})
        langue_param = corps.get("langue")
        methode = str(corps.get("methode") or "wikipedia").strip()
        if langue_param is not None and langue_param not in ("fr", "en"):
            raise HTTPException(400, {
                "code": "E005", "message": f"Langue inconnue : {langue_param}",
                "message_utilisateur": format_user_error(
                    "E005", f"Langue inconnue : {langue_param}")})
        if methode not in ("wikipedia", "llm"):
            raise HTTPException(400, {
                "code": "E005", "message": f"Méthode inconnue : {methode}",
                "message_utilisateur": format_user_error(
                    "E005", f"Méthode inconnue : {methode}")})
        explicite = saisie.startswith(("http://", "https://"))
        categories = None
        try:
            langue, titre = resoudre_saisie(saisie, langue_param)
            if methode == "llm":
                moteur = construire_moteur_llm(corps)
                categories = extraire_termes_llm(titre, langue, moteur)
                candidats = aplanir_categories(categories)
            else:
                langue, candidats = extraire_termes_avec_repli(titre, langue,
                                                               repli=not explicite)
        except ErreurImportWikipedia as exc:
            statut_http = {"E005": 400, "E011": 404}.get(exc.code, 502)
            raise HTTPException(statut_http, {
                "code": exc.code, "message": exc.message,
                "message_utilisateur": format_user_error(exc.code, exc.message)}) from None
        return {"job_id": job_id, "titre": titre, "langue": langue,
                "source": f"{langue}.wikipedia.org", "candidats": candidats,
                "categories": categories}

    @appli.get("/api/jobs/{job_id}/srt")
    def sous_titres_srt(job_id: str):
        """Sous-titres `.srt` des répliques (générées ou corrigées).

        Disponibles dès que les répliques existent (``pret_edition`` ou
        ``termine``) : le fichier est régénéré à la volée depuis
        ``repliques.json`` — la source de vérité, corrections comprises.
        """
        from .edition import lire_repliques
        from .srt_export import generer_srt

        job = gestion.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Job inconnu")
        with job.verrou:
            statut = job.statut
        if statut not in STATUTS_EDITABLES:
            raise HTTPException(409, f"Ce job n'a pas de sous-titres disponibles "
                                     f"(statut « {statut} »)")
        try:
            payload = lire_repliques(job.job_dir)
        except (RythmoError, json.JSONDecodeError):
            raise HTTPException(409, "Sous-titres indisponibles pour ce job "
                                     "(fichier absent ou corrompu)") from None
        srt = generer_srt(payload)
        nom = f"rythmo_{job.id}.srt"
        return Response(srt, media_type="application/x-subrip",
                        headers={"Content-Disposition":
                                 f'attachment; filename="{nom}"'})

    @appli.get("/api/jobs/{job_id}/audio")
    def audio_job(job_id: str, debut: float | None = None, fin: float | None = None):
        """Audio 16 kHz du job : forme d'onde (fichier entier) et écoute (tranche).

        Servi dès que le job est éditable (``pret_edition`` ou ``termine``).
        Avec ``debut``/``fin`` (secondes), seule la tranche ``[debut, fin]``
        est renvoyée — le navigateur ne reçoit jamais le WAV entier, même sur
        une vidéo de 90 minutes (l'écoute mot à mot, T67–T68).
        """
        job = gestion.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Job inconnu")
        with job.verrou:
            statut = job.statut
        if statut not in STATUTS_EDITABLES:
            raise HTTPException(409, f"Ce job n'a pas d'audio disponible "
                                     f"(statut « {statut} »)")
        audio = safe_path(job.job_dir, "audio_16k.wav")
        if not audio.is_file():
            raise HTTPException(404, "Audio indisponible pour ce job "
                                     "(fichier absent)")
        if debut is not None or fin is not None:
            from .audio_segments import decouper_segment

            try:
                morceau, _ = decouper_segment(audio, debut, fin)
            except RythmoError as exc:
                raise HTTPException(400, {"code": exc.code, "message": exc.message,
                                          "message_utilisateur": format_user_error(
                                              exc.code, exc.message)}) from None
            return Response(morceau, media_type="audio/wav")
        return FileResponse(audio, media_type="audio/wav",
                            headers={"Content-Disposition": "inline"})

    @appli.get("/api/jobs/{job_id}/onde")
    def onde_job(job_id: str, colonnes: int = 1600,
                 debut: float | None = None, fin: float | None = None):
        """Pics min/max de l'onde (aperçu) — calculés côté serveur, en streaming.

        Le navigateur ne reçoit jamais le WAV entier (jusqu'à ≈ 345 Mo sur une
        vidéo de 90 min) : seulement quelques milliers de paires de flottants
        (T85–T88). ``colonnes`` borné, fenêtre inversée → 400 E006.
        """
        from .onde import extraire_pics

        job = gestion.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Job inconnu")
        with job.verrou:
            statut = job.statut
        if statut not in STATUTS_EDITABLES:
            raise HTTPException(409, f"Ce job n'a pas d'onde disponible "
                                     f"(statut « {statut} »)")
        if not 1 <= int(colonnes) <= 20000:
            raise HTTPException(400, {"code": "E006",
                                      "message": f"colonnes hors bornes : "
                                                  f"{colonnes} (1–20000)",
                                      "message_utilisateur": format_user_error(
                                          "E006", "colonnes hors bornes")})
        audio = safe_path(job.job_dir, "audio_16k.wav")
        if not audio.is_file():
            raise HTTPException(404, "Audio indisponible pour ce job "
                                     "(fichier absent)")
        try:
            pics, rate, duree = extraire_pics(audio, debut, fin, colonnes)
        except RythmoError as exc:
            raise HTTPException(400, {"code": exc.code, "message": exc.message,
                                      "message_utilisateur": format_user_error(
                                          exc.code, exc.message)}) from None
        return {"duree": duree, "rate": rate, "colonnes": len(pics),
                "pics": pics}

    @appli.post("/api/jobs/{job_id}/suggestions")
    async def suggestions_route(job_id: str, request: Request):
        """Suggestions de correction FR pour les textes envoyés (état affiché).

        Stateless : le front envoie les textes **actuels** de ses répliques
        (corrections à la main comprises) et reçoit les suggestions calculées
        sur ces textes — jamais de décalage avec ce que voit l'utilisateur.
        """
        from .suggestions import suggerer_replique

        job = gestion.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Job inconnu")
        with job.verrou:
            statut = job.statut
        if statut not in STATUTS_EDITABLES:
            raise HTTPException(409, f"Ce job n'accepte pas de suggestions "
                                     f"(statut « {statut} »)")
        try:
            corps = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(400, {"code": "E005",
                                      "message": "Corps JSON illisible",
                                      "message_utilisateur": format_user_error(
                                          "E005", "Corps JSON illisible")}) from None
        entrees = corps.get("repliques") if isinstance(corps, dict) else None
        if not isinstance(entrees, list):
            raise HTTPException(400, {"code": "E005",
                                      "message": "Le corps attendu est une "
                                                  "liste de répliques",
                                      "message_utilisateur": format_user_error(
                                          "E005", "Le corps attendu est une "
                                                  "liste de répliques")})
        return {"repliques": [
            {"id": r.get("id"), "texte": str(r.get("texte", "")),
             "suggestions": suggerer_replique(str(r.get("texte", "")))}
            for r in entrees if isinstance(r, dict)
        ]}

    @appli.post("/api/jobs/{job_id}/synchroniser")
    async def synchroniser_route(job_id: str, request: Request):
        """Réaligne le texte des répliques envoyées sur l'audio du job (mot à mot).

        Stateless : reçoit l'état affiché par l'éditeur et renvoie ce même état
        complété de timings ``mots`` — sans persister ni relancer le rendu (le
        PUT habituel reste le seul déclencheur de rendu). Utile après un import
        de ``.srt``, dont les mots sont sinon répartis uniformément.
        """
        from .synchroniser import synchroniser_repliques

        job = gestion.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Job inconnu")
        with job.verrou:
            statut = job.statut
        if statut not in STATUTS_EDITABLES:
            raise HTTPException(409, f"Ce job n'accepte pas de resynchronisation "
                                     f"(statut « {statut} »)")
        try:
            corps = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(400, {"code": "E005",
                                      "message": "Corps JSON illisible",
                                      "message_utilisateur": format_user_error(
                                          "E005", "Corps JSON illisible")}) from None
        repliques = corps.get("repliques") if isinstance(corps, dict) else None
        try:
            resynchronisees = synchroniser_repliques(job.job_dir, repliques)
        except RythmoError as exc:
            raise HTTPException(400, {"code": exc.code, "message": exc.message,
                                      "message_utilisateur": format_user_error(
                                          exc.code, exc.message)}) from None
        return {"job_id": job_id, "repliques": resynchronisees}

    @appli.post("/api/jobs/{job_id}/synchroniser-replique")
    async def synchroniser_replique_route(job_id: str, request: Request):
        """Réaligne UNE réplique sur sa seule fenêtre audio (mot à mot).

        Stateless : reçoit la réplique affichée et renvoie cette même réplique
        complétée de timings ``mots`` — sans persister ni relancer le rendu.
        Contrairement à ``/synchroniser``, seule la fenêtre ``[debut, fin]`` de
        la réplique est transcrite (jamais le fichier entier).
        """
        from .synchroniser import resynchroniser_replique

        job = gestion.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Job inconnu")
        with job.verrou:
            statut = job.statut
        if statut not in STATUTS_EDITABLES:
            raise HTTPException(409, f"Ce job n'accepte pas de resynchronisation "
                                     f"(statut « {statut} »)")
        try:
            corps = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(400, {"code": "E005",
                                      "message": "Corps JSON illisible",
                                      "message_utilisateur": format_user_error(
                                          "E005", "Corps JSON illisible")}) from None
        replique = corps.get("replique") if isinstance(corps, dict) else None
        try:
            resynchronisee = resynchroniser_replique(job.job_dir, replique)
        except RythmoError as exc:
            raise HTTPException(400, {"code": exc.code, "message": exc.message,
                                      "message_utilisateur": format_user_error(
                                          exc.code, exc.message)}) from None
        return {"job_id": job_id, "replique": resynchronisee}

    @appli.get("/api/jobs/{job_id}/projet")
    def exporter_projet_route(job_id: str):
        """Export du travail complet (répliques + timings + options) en JSON portable.

        Le comédien sauvegarde son projet (versionné) et le restaure plus tard
        — même sur une autre machine — sans rien retaper (T89–T92).
        """
        from .projet import exporter_projet

        job = gestion.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Job inconnu")
        with job.verrou:
            statut = job.statut
        if statut not in STATUTS_EDITABLES:
            raise HTTPException(409, f"Ce job n'a pas de projet à exporter "
                                     f"(statut « {statut} »)")
        try:
            projet = exporter_projet(job.job_dir)
        except (RythmoError, json.JSONDecodeError, OSError):
            raise HTTPException(409, "Projet indisponible pour ce job "
                                     "(fichiers absents ou corrompus)") from None
        corps = json.dumps(projet, ensure_ascii=False, indent=2)
        return Response(corps, media_type="application/json",
                        headers={"Content-Disposition":
                                 f'attachment; filename="rythmo_{job_id}.projet.json"'})

    @appli.post("/api/jobs/{job_id}/projet", status_code=202)
    async def importer_projet_route(job_id: str, request: Request):
        """Restaure un projet exporté : remplace répliques + params puis re-rend.

        La validation est complète avant toute écriture : répliques invalides →
        400 E005, format de projet inconnu → 400 E008, jamais de job corrompu.
        Comme le PUT /repliques, l'import relance la phase 2 (rendu).
        """
        from .projet import importer_projet

        job = gestion.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Job inconnu")
        with job.verrou:
            statut = job.statut
        if statut not in STATUTS_EDITABLES:
            raise HTTPException(409, f"Ce job n'accepte pas d'import de projet "
                                     f"(statut « {statut} »)")
        try:
            corps = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(400, {"code": "E005",
                                      "message": "Corps JSON illisible",
                                      "message_utilisateur": format_user_error(
                                          "E005", "Corps JSON illisible")}) from None
        if not isinstance(corps, dict):
            raise HTTPException(400, {"code": "E005",
                                      "message": "Le corps attendu est un objet "
                                                  "de projet",
                                      "message_utilisateur": format_user_error(
                                          "E005", "Le corps attendu est un objet "
                                                  "de projet")})
        try:
            importer_projet(job.job_dir, corps)
        except RythmoError as exc:
            raise HTTPException(400, {"code": exc.code, "message": exc.message,
                                      "message_utilisateur": format_user_error(
                                          exc.code, exc.message)}) from None
        gestion.executer_rendu(job_id)
        return {"job_id": job_id, "statut": "rendu_en_cours"}

    @appli.get("/api/jobs/{job_id}/result")
    def resultat(job_id: str):
        job = gestion.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Job inconnu")
        if job.statut == "annule":
            raise HTTPException(410, "Job annulé : aucun résultat")
        if job.statut != "termine":
            raise HTTPException(409, f"Traitement en cours ({job.progression} %)")
        resultat_path = safe_path(job.job_dir, "final.mp4")
        if not resultat_path.is_file():
            raise HTTPException(410, "Résultat absent (fichier supprimé)")
        return FileResponse(resultat_path, media_type="video/mp4",
                            filename=f"rythmo_{job.id}.mp4")

    # Couche de traduction IA (module dédié, sans toucher aux routes existantes)
    from .traduction.traduction_api import enregistrer_routes as enregistrer_routes_traduction
    enregistrer_routes_traduction(appli, gestion)

    if FRONT_DIR.is_dir():
        appli.mount("/", StaticFiles(directory=FRONT_DIR, html=True), name="front")
    return appli
