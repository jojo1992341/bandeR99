"""Orchestration d'une traduction de réplique (spec §8).

``DubbingTranslator`` réunit le moteur, les analyseurs et le scoreur :
texte source → **contexte complet** (glossaire, profil de personnage, mémoire,
idiomes, voisins — slice 6) → génération de **candidats multiples** (slice 5)
→ analyse syllabique + phonétique + labiale → score sur 8 critères → choix du
meilleur candidat. Si le meilleur est sous le **seuil**, la boucle de correction
ciblée repasse au moteur avec une contrainte (durée « trop longue de X ms »,
sinon critère le plus faible), bornée par ``max_iterations``.

Priorités (slice 6) : une entrée **exacte du glossaire** ou une phrase déjà en
**mémoire** est réutilisée telle quelle (jamais retraduite par le moteur) ; une
**interjection/réplique courte** n'est jamais supprimée (repli identité). Les
erreurs moteur sont capturées : le job reste toujours sain.
"""
from __future__ import annotations

from typing import Any, Mapping

from .cache import TranslationCache
from .candidats import CandidateGenerator
from .engine import TranslationEngine
from .glossaire import GlossaryManager
from .humour import HumourManager
from .lipsync import LipSyncAnalyzer
from .memoire import TranslationMemory
from .personnages import CharacterManager
from .phonemes import PhonemeAnalyzer
from .score import DubbingScorer
from .syllabes import SyllableAnalyzer
from .types import (STATUT_ERREUR, STATUT_TRADUIT, RepliqueContexte,
                    TraductionCandidat, TraductionEntree)


class DubbingTranslator:
    """Une passe de traduction d'une réplique : contexte → candidats → score → correction."""

    def __init__(self, moteur: TranslationEngine, langue_source: str,
                 langue_cible: str, scorer: DubbingScorer | None = None,
                 analyseur_source: PhonemeAnalyzer | None = None,
                 analyseur_cible: PhonemeAnalyzer | None = None,
                 analyseur_lipsync: LipSyncAnalyzer | None = None,
                 max_iterations: int = 3, tolerance_duree_s: float = 0.0,
                 nombre_candidats: int = 3, seuil_score: float = 85.0,
                 generateur: CandidateGenerator | None = None,
                 glossaire: GlossaryManager | None = None,
                 personnages: CharacterManager | None = None,
                 memoire: TranslationMemory | None = None,
                 humour: HumourManager | None = None,
                 cache: TranslationCache | None = None,
                 temperature: float | None = None):
        self.moteur = moteur
        self.langue_source = langue_source
        self.langue_cible = langue_cible
        self.scorer = scorer or DubbingScorer()
        self.analyseur_source = analyseur_source or PhonemeAnalyzer(langue_source)
        self.analyseur_cible = analyseur_cible or PhonemeAnalyzer(langue_cible)
        self.lipsync = analyseur_lipsync or LipSyncAnalyzer(analyseur=self.analyseur_source)
        self.max_iterations = max(1, int(max_iterations))
        self.tolerance_duree_s = tolerance_duree_s
        self.seuil_score = float(seuil_score)
        self.generateur = generateur or CandidateGenerator(moteur, nombre=nombre_candidats)
        self.glossaire = glossaire
        self.personnages = personnages
        self.memoire = memoire
        self.humour = humour
        self.cache = cache
        self.temperature = temperature

    def _syllabes(self, texte: str, langue: str) -> int:
        """Syllabes d'un texte — via le cache (slice 8) si présent."""
        if self.cache is not None:
            return self.cache.syllabes(texte, langue)
        return SyllableAnalyzer(langue).compter(texte)

    def _phonemes(self, texte: str, langue: str) -> list[str]:
        """Phonèmes aplatis d'un texte — via le cache (slice 8) si présent."""
        if self.cache is not None:
            return list(self.cache.phonemes(texte, langue))
        return PhonemeAnalyzer(langue).analyser(texte).aplatir()

    def _contexte(self, replique: Mapping[str, Any],
                  precedente: str | None = None,
                  suivante: str | None = None) -> dict[str, Any]:
        """Contexte complet transmis au moteur — uniquement les données disponibles."""
        source_text = str(replique.get("texte", ""))
        ctx = RepliqueContexte(
            source_text=source_text,
            personnage=str(replique.get("personnage") or ""),
        )
        if precedente is not None:
            ctx.precedent = str(precedente)
        if suivante is not None:
            ctx.suivant = str(suivante)
        if replique.get("scene"):
            ctx.scene = str(replique["scene"])
        if self.glossaire:
            correspondances = self.glossaire.correspondances(source_text)
            if correspondances:
                ctx.glossaire = correspondances
        if self.personnages and ctx.personnage:
            profil = self.personnages.contexte(ctx.personnage)
            if profil:
                ctx.profil = profil
        if self.memoire:
            correspondances = self.memoire.correspondances(source_text)
            if correspondances:
                ctx.memoire = correspondances
        if self.humour:
            ctx.idiomes = self.humour.adaptations(source_text)
            ctx.interjection = self.humour.est_interjection(source_text)
        base: dict[str, Any] = {
            "source_language": self.langue_source,
            "target_language": self.langue_cible,
            "start_time": float(replique.get("debut", 0.0)),
            "end_time": float(replique.get("fin", 0.0)),
            **ctx.to_dict(),
        }
        if self.temperature is not None:
            base["temperature"] = self.temperature
        return base

    def _analyser_candidat(self, cible: str, source_text: str,
                           source_phonemes: list[str], duree_s: float,
                           source_syllabes: int, replique: Mapping[str, Any],
                           contexte: Mapping[str, Any]) -> tuple[TraductionCandidat,
                                                                 int, list[str]]:
        """Analyse + score un candidat. Retourne (candidat, syllabes, phonèmes)."""
        cible = str(cible)
        target_syllabes = self._syllabes(cible, self.langue_cible)
        target_phonemes = self._phonemes(cible, self.langue_cible)
        lip = self.lipsync.analyser(source_text, cible,
                                    mots_source=replique.get("mots"))
        score_global, scores = self.scorer.scorer(
            source_text, cible, duree_s, source_syllabes, target_syllabes,
            source_phonemes=source_phonemes, target_phonemes=target_phonemes,
            analyse_lipsync=lip, contexte=dict(contexte))
        return TraductionCandidat(texte=cible, score_global=score_global,
                                  scores=scores), target_syllabes, target_phonemes

    def _analyser(self, candidats: list[str], source_text: str,
                  source_phonemes: list[str], duree_s: float,
                  source_syllabes: int, replique: Mapping[str, Any],
                  contexte: Mapping[str, Any]):
        """Analyse + score chaque candidat ; retourne (liste, meilleur, syllabes, phonèmes)."""
        resultats = [self._analyser_candidat(c, source_text, source_phonemes,
                                             duree_s, source_syllabes, replique,
                                             contexte) for c in candidats]
        if not resultats:
            return [], TraductionCandidat(), 0, []
        meilleur = max(resultats, key=lambda r: r[0].score_global)
        return ([r[0] for r in resultats], meilleur[0], meilleur[1], meilleur[2])

    def _contrainte(self, meilleur: TraductionCandidat, duree_s: float) -> str:
        """Contrainte de correction ciblée : durée d'abord, sinon critère le plus faible."""
        syllabes = self._syllabes(meilleur.texte, self.langue_cible)
        excedent = SyllableAnalyzer(self.langue_cible).estimer_duree(syllabes) - duree_s
        if excedent > self.tolerance_duree_s:
            return f"trop longue de {int(round(excedent * 1000))} ms"
        if not meilleur.scores:
            return "texte vide"
        nom, valeur = min(meilleur.scores.items(), key=lambda kv: kv[1])
        return f"{nom} faible ({valeur:.0f})"

    def traduire(self, replique: Mapping[str, Any],
                 precedente: str | None = None,
                 suivante: str | None = None,
                 probleme: str | None = None) -> TraductionEntree:
        """Traduit une réplique : contexte complet, candidats multiples, correction bornée.

        ``probleme`` (retraduction ciblée, slice 7) est transmis au moteur comme
        contrainte dès la première génération, et la mémoire est ignorée (on
        veut une nouvelle tentative, pas la réutilisation d'une ancienne).

        Ne lève jamais : une erreur moteur devient ``statut: erreur`` avec le
        message, la source restant intacte.
        """
        source_text = str(replique.get("texte", ""))
        duree_s = float(replique.get("fin", 0.0)) - float(replique.get("debut", 0.0))
        source_syllabes = self._syllabes(source_text, self.langue_source)
        try:
            contexte = self._contexte(replique, precedente, suivante)
            if probleme:
                contexte = {**contexte, "contrainte": str(probleme)}
            source_phonemes = self._phonemes(source_text, self.langue_source)
            # 1) glossaire prioritaire (entrée exacte) — jamais retraduit par le moteur
            cible_directe = self.glossaire.traduire(source_text) if self.glossaire else None
            # 2) mémoire de traduction (cohérence) — phrase déjà validée réutilisée
            #    (sauf retraduction ciblée : on repart du moteur avec le problème)
            if cible_directe is None and self.memoire and not probleme:
                cible_directe = self.memoire.consulter(source_text)
            autoritaire = cible_directe is not None
            if autoritaire:
                candidats = [cible_directe]
            else:
                candidats = self.generateur.generer(source_text, contexte)
            # 3) interjection / réplique courte : jamais supprimée par le moteur
            if not candidats and self.humour and self.humour.est_replique_courte(source_text):
                candidats = [source_text]
            candidats, meilleur, tgt_syl, tgt_pho = self._analyser(
                candidats, source_text, source_phonemes, duree_s, source_syllabes,
                replique, contexte)
            iterations = 1
            explications: list[str] = []
            # Boucle de correction (slice 5) : sous le seuil, on repasse au moteur
            # avec une contrainte ciblée — sauf traduction autoritaire
            # (glossaire/mémoire, jamais corrigée), bornée et interrompue si le
            # moteur ne progresse plus.
            if not autoritaire:
                for _ in range(self.max_iterations):
                    if meilleur.score_global >= self.seuil_score:
                        break
                    contrainte = self._contrainte(meilleur, duree_s)
                    nv_candidats, nv_meilleur, nv_syl, nv_pho = self._analyser(
                        self.generateur.generer(source_text,
                                                {**contexte, "contrainte": contrainte}),
                        source_text, source_phonemes, duree_s, source_syllabes,
                        replique, contexte)
                    iterations += 1
                    if not nv_meilleur.texte.strip() or nv_meilleur.score_global <= meilleur.score_global:
                        break
                    explications.append(contrainte)
                    candidats, meilleur, tgt_syl, tgt_pho = (nv_candidats, nv_meilleur,
                                                              nv_syl, nv_pho)
            # 4) enregistrer la mémoire (cohérence terminologique sur tout le film)
            if self.memoire and meilleur.texte.strip():
                self.memoire.enregistrer(source_text, meilleur.texte)
            return TraductionEntree(
                source_text=source_text, target_text=meilleur.texte,
                statut=STATUT_TRADUIT, source_syllabes=source_syllabes,
                target_syllabes=tgt_syl, source_phonemes=source_phonemes,
                target_phonemes=tgt_pho, score_global=meilleur.score_global,
                scores=meilleur.scores, candidats=candidats,
                iteration_count=iterations, explications=explications)
        except Exception as exc:  # noqa: BLE001 — le job reste toujours sain
            return TraductionEntree(
                source_text=source_text, statut=STATUT_ERREUR,
                source_syllabes=source_syllabes, erreur=str(exc)[:300])
