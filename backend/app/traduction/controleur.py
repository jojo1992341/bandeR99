"""TranslationController — passe de traduction par lots (spec §20, §23).

Orchestre la traduction de toutes les répliques d'un job : progression X/Y
persistée à chaque entrée (sauvegarde auto partielle), pause coopérative qui
gèle l'état, reprise qui ne retraduit jamais une entrée déjà ``traduit``, et
annulation coopérative (même philosophie que ``Job.annulation``).

Une interruption (crash, disque plein, annulation) ne perd jamais les entrées
déjà persistées : ``traduction.json`` est réécrit de façon atomique après
chaque réplique. Une reprise relit cette couche et repart de là.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from .types import STATUT_TRADUIT

STATUT_EN_COURS = "en_cours"
STATUT_EN_PAUSE = "en_pause"
STATUT_TERMINE = "termine"
STATUT_ANNULE = "annule"
STATUT_ERREUR = "erreur"
STATUTS_CONSERVES = (STATUT_TRADUIT,)
_INTERVALLE_PAUSE_S = 0.02


class AnnulationTraduction(Exception):
    """Levée quand l'utilisateur annule la passe (coopératif)."""


class TranslationController:
    """Traduction par lots, avec pause/reprise/annulation et sauvegarde auto."""

    def __init__(self, traducteur, store, repliques, langue_source, langue_cible, modele):
        self.traducteur = traducteur
        self.store = store
        self.repliques = list(repliques)
        self.langue_source = langue_source
        self.langue_cible = langue_cible
        self.modele = modele
        self._verrou = threading.Lock()
        self._pause = threading.Event()
        self._annulation = threading.Event()
        self._statut = STATUT_EN_COURS
        self._fait = 0

    def total(self) -> int:
        return len(self.repliques)

    def etat(self) -> dict[str, Any]:
        """État vivant de la passe (thread-safe), exposé par l'API."""
        with self._verrou:
            return {"statut": self._statut, "fait": self._fait, "total": self.total()}

    def mettre_en_pause(self) -> None:
        self._pause.set()
        self._poser_statut(STATUT_EN_PAUSE)

    def reprendre(self) -> None:
        self._pause.clear()
        self._poser_statut(STATUT_EN_COURS)

    def annuler(self) -> None:
        self._annulation.set()
        self._poser_statut(STATUT_ANNULE)

    def _poser_statut(self, statut: str) -> None:
        with self._verrou:
            self._statut = statut

    def _verifier(self) -> None:
        """Point de contrôle entre deux répliques : annulation puis pause."""
        while self._pause.is_set():
            time.sleep(_INTERVALLE_PAUSE_S)
            if self._annulation.is_set():
                raise AnnulationTraduction()
        if self._annulation.is_set():
            raise AnnulationTraduction()

    def _verifier_annulation(self) -> None:
        """Après une traduction en vol : seule l'annulation interrompt (pas la pause)."""
        if self._annulation.is_set():
            raise AnnulationTraduction()

    def _maj_compteur(self, statut_entree: str) -> None:
        if statut_entree in STATUTS_CONSERVES:
            self._fait += 1

    def _persister(self, entrees: dict) -> None:
        """Écrit la couche avec la progression courante (atomique via le store)."""
        self.store.ecrire({
            "version": 1,
            "langue_source": self.langue_source,
            "langue_cible": self.langue_cible,
            "modele": self.modele,
            "progression": {"fait": self._fait, "total": self.total()},
            "entrees": entrees,
        })

    def _persister_silencieux(self, entrees: dict) -> None:
        """Persistance best-effort (état final après annulation/erreur)."""
        try:
            self._persister(entrees)
        except OSError:
            pass

    def _terminee(self, entree: dict) -> bool:
        """Entrée terminée (``traduit``) : comptée « fait » et non retraduite."""
        return entree.get("statut") in STATUTS_CONSERVES

    def _a_retraduire(self, entree: dict) -> bool:
        """True s'il faut (re)traduire : ni terminée, ni verrouillée, ni exclue."""
        if entree.get("exclue") or entree.get("verrouillee"):
            return False
        return not self._terminee(entree)

    def executer(self) -> dict[str, Any]:
        """Boucle de traduction. Ne lève jamais : les erreurs finissent en état.

        Reprend depuis ``traduction.json`` : les compteurs sont dérivés des
        entrées existantes et les entrées ``traduit``/verrouillées/exclues sont
        sautées (une réplique verrouillée ou exclue n'est jamais retouchée).
        """
        couche = self.store.lire()
        entrees = dict(couche.get("entrees", {}))
        self._fait = sum(1 for e in entrees.values() if self._terminee(e))
        try:
            for replique in self.repliques:
                rid = str(replique.get("id", ""))
                if not self._a_retraduire(entrees.get(rid, {})):
                    continue  # déjà traduite : jamais retouchée
                self._verifier()
                entree = self.traducteur.traduire(replique)
                entrees[rid] = entree.to_dict()
                self._maj_compteur(entree.statut)
                self._persister(entrees)
                self._verifier_annulation()
            self._poser_statut(STATUT_TERMINE)
        except AnnulationTraduction:
            self._poser_statut(STATUT_ANNULE)
            self._persister_silencieux(entrees)
        except Exception:  # noqa: BLE001 — la passe finit en état, jamais en exception
            self._poser_statut(STATUT_ERREUR)
            self._persister_silencieux(entrees)
        finally:
            # arrêt propre du moteur (slice 8) — best-effort, jamais bloquant
            try:
                self.traducteur.moteur.arreter()
            except Exception:  # noqa: BLE001
                pass
        return self.etat()
