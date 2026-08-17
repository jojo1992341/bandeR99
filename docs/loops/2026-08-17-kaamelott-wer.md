# Boucle WER Kaamelott — Plan de conception de boucle

**Objectif :** faire descendre le WER de la transcription française de l'échantillon
Kaamelott (S01E01, slice de 420 s) sous `WER_DE_BASE` (départ 12,5 %), accepter et
implanter chaque amélioration qui passe sous la base courante, et itérer jusqu'à ce
que `WER_DE_BASE` ≤ 5 % — en rejetant (et révertant) toute proposition dont le WER
mesuré est ≥ `WER_DE_BASE`.

## Contexte vérifié dans le code

- **Référence humaine** : `videos/Kaamelott_S01E01_Episode_1.json` (1 342 mots normalisés).
- **Audio** : `backend/data/jobs/46d2608acf96/audio_16k.wav` (420 s, 16 kHz mono).
- **Métrique** : `app.asr.wer_fr` (Levenshtein sur mots normalisés, nombres épelés).
- **Pipeline mesuré** : `app.asr.transcribe_chunked(wav, language="fr", model_name="medium")`
  — chunking 25 s + recouvrement 1 s + nudge conservateur des frontières.
- **WER actuel (référence Slice 11)** : **11,25 %** — déjà sous 12,5 % : le Run 0
  confirmera la mesure et posera `WER_DE_BASE = min(0,125, mesure)` ≈ 0,1125.
- **Garde-fou existant** : `tests/integration/test_wer_fr.py` verrouille la
  non-régression sur Redoublage.mp4 (small + medium, avec/sans prompt) et sur
  Kaamelott (plafond = référence + marge 0,05).
- **Levier immédiat évident** : le job de production a été lancé avec
  `vocabulaire: []` alors que le glossaire du projet existe
  (`backend/data/jobs/46d2608acf96/glossaire.json`, 43 termes : Perceval, Karadoc…).
  Le test actuel n'en passe aucun à `transcribe_chunked`.
- **Pas de dépôt git** dans ce workspace : le plan repose sur un protocole de
  **sauvegardes manuelles** pour pouvoir réverter un candidat rejeté.

## Forme de la boucle

```
État (backend/data/wer_loop_state.json)
   │  lit : WER_DE_BASE, config implantée, historique, leviers essayés
   ▼
[Maker]  analyse les erreurs du dernier run → propose UN levier
   │      (ou micro-lot cohérent) → implémente dans le code (avec sauvegarde)
   ▼
[Script] scripts/mesurer_wer_kaamelott.py --json   (mesure objective, externe)
   │      + tests de non-régression Redoublage (pytest -m integration)
   ▼
[Vérificateur]  re-mesure 2× (max conservateur), lit le diff, juge :
   │      WER < WER_DE_BASE  → ACCEPTÉ : nouvelle base, code gardé
   │      WER ≥ WER_DE_BASE  → REJETÉ  : revert depuis la sauvegarde
   ▼
État mis à jour (historique, base, compteurs, statut)
   │
   ├── WER_DE_BASE ≤ 0,05 → STOP : verrouillage (tests) + rapport final
   ├── 3 échecs consécutifs OU 10 runs → ESCALADE (rapport humain)
   └── sinon → run suivant (reprend où l'état s'est arrêté)
```

## Primitives & configuration

| Primitive | Choix | Configuration |
|---|---|---|
| **Automations** | Run-until-done (goal-driven), relancé à la demande | Prompt d'automation complet ci-dessous. Rejeté : cron/scheduled — la boucle doit tourner sur la machine cible (modèles faster-whisper installés), pas sur un serveur sans GPU. |
| **Worktrees** | **Rejeté** | Mono-agent séquentiel : un seul écrivain (le maker) ; le vérificateur ne fait que lire/exécuter. Aucun risque de collision de fichiers. |
| **Skills** | `.agents/skills/kaamelott-wer/SKILL.md` | Codifie la procédure de mesure, le catalogue de leviers, la règle d'acceptation et le protocole de sauvegarde — le prompt d'automation reste court et chaque run démarre avec le savoir des runs précédents. |
| **Connectors** | **Rejeté** | Aucun outil externe : la mesure est locale (faster-whisper + `wer_fr`). Hugging Face (téléchargement des modèles) est déjà géré par faster-whisper. |
| **Subagents** | Maker + Vérificateur | `.claude/agents/wer-maker.md` et `.claude/agents/wer-verifier.md` (contenus complets ci-dessous). Dans cet environnement ils servent de prompts de rôle distincts ; le maker ne juge jamais son propre travail. |
| **État** | `backend/data/wer_loop_state.json` | Schéma complet ci-dessous. C'est la colonne vertébrale : sans lui, chaque run repartirait de zéro. Sauvegardes des fichiers modifiés dans `backend/data/wer_loop_backups/<run_id>/`. |

## Schéma du fichier d'état

Emplacement : `backend/data/wer_loop_state.json` (à côté de `_proof_segmentation_long.json`).
Écrit par le maker ET par le vérificateur (verdict), jamais par le script de mesure.

```json
{
  "cible_wer": 0.05,
  "wer_de_base": 0.125,
  "statut": "IDLE",
  "run_courant": null,
  "config_implantee": {
    "modele": "medium",
    "duree_chunk_s": 25.0,
    "recouvrement_s": 1.0,
    "vocabulaire": [],
    "leviers_code": {}
  },
  "historique": [
    {
      "run_id": "2026-08-17-01",
      "date": "2026-08-17T10:00:00",
      "hypothese": "Passer le glossaire du job comme vocabulaire",
      "levier": "vocabulaire",
      "fichiers_modifies": ["backend/app/asr.py", "tests/integration/test_wer_fr.py"],
      "sauvegarde": "backend/data/wer_loop_backups/2026-08-17-01/",
      "wer_maker": 0.1042,
      "wer_verif_1": 0.1031,
      "wer_verif_2": 0.1050,
      "wer_verif_max": 0.1050,
      "redoublage_ok": true,
      "accepte": true,
      "nouveau_wer_de_base": 0.1050,
      "analyse_erreurs": "Top substitutions : perceval→perceval(?)…",
      "verdict_verificateur": "OK : WER 0,1050 < base 0,1125 ; règles générales ; tests Redoublage passent."
    }
  ],
  "leviers_essayes": [
    {"levier": "vocabulaire", "resultat": "accepte", "gain": 0.0075}
  ],
  "echecs_consecutifs": 0,
  "runs_totaux": 0,
  "escalade": null,
  "derniere_erreur": null
}
```

Champs :

- `cible_wer` : 0,05 — condition d'arrêt finale (`wer_de_base ≤ cible_wer`).
- `wer_de_base` : base courante. Initialisée à 0,125 au Run 0, puis
  `min(wer_de_base, wer_verif_max)` à chaque acceptation. **Règle stricte :
  acceptation si et seulement si `wer_verif_max < wer_de_base` (strictement).**
- `statut` : `IDLE` (prêt pour un run) · `RUNNING` (maker en cours) ·
  `VERIFYING` (vérificateur en cours) · `ESCALATED` · `DONE` (cible atteinte).
  Permet la reprise après crash : un run qui reprend avec `statut=RUNNING` sans
  verdict repart du même point (restaure la sauvegarde du run si nécessaire).
- `run_courant` : id du run en cours (ex. `2026-08-17-01`).
- `config_implantee` : configuration ASR réellement dans le code (le maker la met
  à jour à chaque acceptation ; le script de mesure lit les défauts du code).
- `historique` : un objet par run, avec les deux mesures du vérificateur —
  `wer_verif_max = max(wer_verif_1, wer_verif_2)` est la valeur de décision
  (conservateur contre le nondéterminisme).
- `leviers_essayes` : index rapide (levier → résultat) pour ne jamais re-tenter
  un levier déjà rejeté ; l'historique reste la source de détail.
- `echecs_consecutifs` / `runs_totaux` : compteurs d'escalade.
- `escalade` : rempli avec `{date, raison, rapport}` lors de l'escalade.
- `derniere_erreur` : trace de la dernière exception (reprise après crash).

## Prompt d'automation

Texte exact reçu par chaque run (relance manuelle de l'agent, ou boucle autonome) :

> Boucle WER Kaamelott — run d'amélioration.
>
> Suis `.agents/skills/kaamelott-wer/SKILL.md` à la lettre (procédure de mesure,
> catalogue de leviers, protocole de sauvegarde). Lis d'abord
> `backend/data/wer_loop_state.json`.
>
> Règles :
> 1. Si `statut` est `RUNNING`/`VERIFYING` sans verdict, reprends ce run (crash) :
>    restaure la sauvegarde si le code est à moitié modifié, puis re-mesure.
> 2. Si `wer_de_base ≤ cible_wer` (0,05) : arrête-toi, écris le rapport final dans
>    `docs/loops/rapport-final-kaamelott-wer.md` (historique complet, diff global),
>    mets `statut=DONE`.
> 3. Sinon, si `echecs_consecutifs ≥ 3` ou `runs_totaux ≥ 10` : ESCALADE — écris
>    `docs/loops/escalade-kaamelott-wer.md` (ce qui a été tenté, erreurs restantes,
>    recommandations : large-v3, fine-tuning, ASR cloud) et mets `statut=ESCALATED`.
>    Ne modifie plus rien.
> 4. Sinon, exécute UN run : joue le rôle **maker** (`.claude/agents/wer-maker.md`),
>    puis le rôle **vérificateur** (`.claude/agents/wer-verifier.md`), dans cet
>    ordre, en passant par le script de mesure et les tests — jamais de verdict
>    auto-attribué.
> 5. Mets à jour l'état après chaque verdict et répète jusqu'à l'une des
>    conditions d'arrêt (cible, escalade). Ne triche jamais : pas de règle codée
>    pour le slice seul, pas de modification de la référence, pas de seuil
>    bricolé pour passer la mesure.

## Définitions des sous-agents

### `.claude/agents/wer-maker.md`

```markdown
---
name: wer-maker
description: Propose et implémente UN levier d'amélioration du WER Kaamelott,
puis mesure son effet. Ne juge jamais lui-même l'acceptation.
---

# wer-maker

Tu es le MAKER de la boucle WER Kaamelott. Tu PROPOSES et IMPLÉMENTES, tu ne
décides pas. Le verdict appartient au vérificateur.

## Avant d'agir
1. Lis `backend/data/wer_loop_state.json` : WER_DE_BASE, config implantée,
   historique, leviers_essayes, statut (reprise après crash).
2. Si un run est déjà en cours sans verdict (statut RUNNING/VERIFYING), reprends-le
   (restaure la sauvegarde si le code est à moitié modifié).
3. Obtiens la liste des erreurs actuelles :
   `.venv/Scripts/python.exe scripts/mesurer_wer_kaamelott.py --analyse-erreurs`
   (substitutions/insertions/suppressions avec contexte).

## Proposer UN levier
Choisis UNE hypothèse ciblant l'erreur dominante (voir le catalogue dans
`.agents/skills/kaamelott-wer/SKILL.md`). Ne combine pas plusieurs leviers dans un
même run : s'ils gagnent, on ne saura pas lequel. Un « micro-lot » cohérent
(ex. : une règle de post-traitement + sa jumelle symétrique) est accepté.
N'interdis pas un levier déjà rejeté (il est dans `leviers_essayes`).

## Implémenter (avec sauvegarde obligatoire)
1. Crée `backend/data/wer_loop_backups/<run_id>/` et COPIE chaque fichier que tu
   vas modifier AVANT de le toucher (il n'y a pas de git : la sauvegarde EST le revert).
2. Modifie le code. Règle absolue : la modification doit être GÉNÉRALE (elle
   s'applique à n'importe quel fichier audio français). Tout ce qui est un cas
   particulier du slice (nom de fichier, mots de la référence en dur, seuil
   calibré sur une phrase) sera rejeté par le vérificateur.
3. Mets à jour `statut=RUNNING` et `run_courant` dans l'état (avant la mesure).
4. Mesure :
   `.venv/Scripts/python.exe scripts/mesurer_wer_kaamelott.py --json`
   (réglages de la config implantée via les options du script si le levier est
   un paramètre exposé ; sinon le code modifié EST la mesure).
5. Écris le résultat dans `historique[run].wer_maker` et `analyse_erreurs`
   (top erreurs restantes). Passe la main au vérificateur. Ne conclus RIEN.
```

### `.claude/agents/wer-verifier.md`

```markdown
---
name: wer-verifier
description: Vérifie indépendamment la mesure du maker, lit le diff et décide
l'acceptation (WER < WER_DE_BASE) ou le rejet (revert). Seul juge du verdict.
---

# wer-verifier

Tu es le VÉRIFICATEUR de la boucle WER Kaamelott. Tu es le SEUL à décider
d'accepter ou de rejeter. Tu ne fais confiance à aucune mesure du maker.

## Procédure (dans l'ordre)
1. Lis le diff du run : `historique[run].fichiers_modifies` + les sauvegardes
   `backend/data/wer_loop_backups/<run_id>/` (compare fichier par fichier).
   REJET immédiat (sans mesure) si :
   - la modification est un cas particulier du slice (nom de fichier, mots de la
     référence en dur, règle calibrée sur une phrase précise) ;
   - la référence `videos/Kaamelott_S01E01_Episode_1.json`, `wer_fr` ou la
     fonction de mesure a été modifiée ;
   - le maker a touché des fichiers hors de la liste déclarée.
2. Re-mesure TOI-MÊME, deux fois, dans des processus frais :
   `.venv/Scripts/python.exe scripts/mesurer_wer_kaamelott.py --json`
   puis une seconde exécution identique. Décision sur
   `wer_verif_max = max(wer_verif_1, wer_verif_2)` (conservateur).
3. Vérifie la non-régression Redoublage :
   `.venv/Scripts/python.exe -m pytest -m integration -q`
   (les tests Redoublage small/medium et le test Kaamelott doivent passer).
   Tout échec = rejet.
4. Verdict :
   - `wer_verif_max < wer_de_base` ET Redoublage OK → **ACCEPTÉ** :
     `accepte=true`, `nouveau_wer_de_base=wer_verif_max`,
     `config_implantee` mise à jour, `echecs_consecutifs=0`, `statut=IDLE`.
   - sinon → **REJETÉ** : `accepte=false`, `echecs_consecutifs += 1`,
     `statut=IDLE`. **Revert obligatoire** : restaure chaque fichier depuis la
     sauvegarde du run (supprime ensuite le dossier de sauvegarde ? NON : garde
     la sauvegarde et note `revert=true` dans l'historique — traçabilité).
5. Remplis `historique[run]` (verdict, mesures, redoublage_ok, commentaire) et
   mets à jour `wer_de_base` si accepté, `leviers_essayes`, `runs_totaux`.
```

## Compétence à écrire

### `.agents/skills/kaamelott-wer/SKILL.md`

```markdown
---
name: kaamelott-wer
description: Boucle d'amélioration du WER de la transcription française Kaamelott.
Mesure objective, règle d'acceptation stricte (WER < WER_DE_BASE), maker/checker
séparés, sauvegardes manuelles (pas de git). À activer pour tout run de la boucle.
---

# Kaamelott WER — procédure de la boucle

## Métrique et mesure
- Référence : `videos/Kaamelott_S01E01_Episode_1.json` (1 342 mots normalisés).
- Audio : `backend/data/jobs/46d2608acf96/audio_16k.wav` (420 s).
- Commande de mesure (externe, objective) :
  `.venv/Scripts/python.exe scripts/mesurer_wer_kaamelott.py --json`
  Options : `--modele`, `--duree-chunk`, `--recouvrement`, `--vocabulaire <glossaire.json>`,
  `--analyse-erreurs`, `--double` (2 mesures, garde le pire).
- Non-régression : `.venv/Scripts/python.exe -m pytest -m integration -q`.

## Règle d'acceptation (stricte, non négociable)
- ACCEPTÉ ssi `wer_verif_max < wer_de_base` (strictement) ET tests Redoublage OK.
- REJETÉ sinon, avec revert depuis `backend/data/wer_loop_backups/<run_id>/`.
- À l'acceptation : `wer_de_base = wer_verif_max` ; le code reste implanté.
- Condition d'arrêt : `wer_de_base ≤ 0,05`. Escalade : 3 échecs consécutifs ou 10 runs.
- Départ : `wer_de_base = 0,125` (le Run 0 le remplace par min(0,125, mesure actuelle)).

## État
`backend/data/wer_loop_state.json` (schéma dans docs/loops/2026-08-17-kaamelott-wer.md).
Toujours lire au début, écrire après chaque verdict. Ne jamais re-tenter un levier
présent dans `leviers_essayes` avec résultat « rejete ».

## Catalogue de leviers (ancré dans le code)
1. **Vocabulaire du projet** : passer `vocabulaire=` à `transcribe_chunked` avec
   `backend/data/jobs/46d2608acf96/glossaire.json` (43 termes). Levier immédiat,
   zéro risque — la prod tournait avec `vocabulaire: []`.
2. **Modèle** : `medium` → `large-v3` (ou `large-v3-turbo`, plus rapide).
   `get_asr_model(model_name)`. Coût VRAM/temps ↑ ; gain typique de plusieurs points
   sur l'oral français.
3. **Taille de chunk** : `duree_chunk_s` 25 → 10–15 s (moins de coupures en
   bordure de fenêtre) ou → 30 s (fenêtre interne unique). À mesurer, jamais à parier.
4. **Recouvrement** : `recouvrement_s` 1 → 2 s (mots de bordure mieux décodés,
   dédupliqués ensuite).
5. **Nudge des frontières** (`frontieres_silences`) : `tolerance_s` 2,5 → 4–5 s,
   `min_silence_s` 0,25 → 0,15–0,3. Historique : le nudge conservateur a fait
   19,4 % → 11,25 %. Trop agressif → mots coupés (le vérificateur le détecte).
6. **Contexte glissant** : `_CONTEXTE_MOTS_MAX` 20 → 40–60 ; ne passer que les mots
   « possédés » du chunk précédent (pas ceux du recouvrement).
7. **Décodage** : `beam_size` 5 → 8–10 dans `_transcrire_fichier` (plus lent) ;
   tester `condition_on_previous_text` (actuellement False).
8. **VAD** : `vad_parameters` (`min_silence_duration_ms` 300 → 200/400,
   `speech_pad_ms` 80 → 150) — mots coupés en tête/queue de segment.
9. **Ré-décodage ciblé** (préparé par le code, Slice 19) : re-transcrire avec un
   modèle plus grand les fenêtres contenant des mots `incertain`
   (`marquer_mots_incertains`, `transcrire_fenetre`) et garder le meilleur texte.
10. **Post-traitement** : étendre homophones/élisions/nombres/noms propres
    (`corriger_homophones_fr*`, `fusionner_fragments_fr`, `normaliser_nombres_mots`,
    `corriger_noms_propres`) sur les erreurs RÉELLES du slice — chaque règle doit
    être générale (français, pas Kaamelott).
11. **Prompt FR** : enrichir `_PROMPT_FR_BASE` avec le registre oral (le
    vocabulaire du projet y est déjà injecté dynamiquement).

## Anti-triche (vérifié par le vérificateur)
Interdit : règle codée pour le slice seul, mots de la référence en dur, seuil
calibré sur une phrase, modification de la référence ou de `wer_fr`, fichiers
modifiés hors de la liste déclarée.

## Protocole de sauvegarde (pas de git ici)
Avant toute modification : copier chaque fichier touché dans
`backend/data/wer_loop_backups/<run_id>/`. Rejet → restauration depuis ce dossier
(puis `revert=true` dans l'historique). Le dossier de sauvegarde est conservé.
```

## Condition d'arrêt et vérification

- **Condition de succès (objective)** : `wer_de_base ≤ 0,05`, où `wer_de_base` est
  le WER mesuré par le **vérificateur** (`max` de 2 exécutions fraîches de
  `scripts/mesurer_wer_kaamelott.py`) — jamais une appréciation, jamais la mesure
  du maker.
- **Commande de vérification** : `scripts/mesurer_wer_kaamelott.py` (contenu
  complet ci-dessous), qui réutilise `app.asr.wer_fr` et `app.asr.transcribe_chunked`
  — la même fonction que les tests verrouillés.
- **Qui vérifie** : le sous-agent vérificateur (deuxième paire d'yeux, mesure
  refaite de zéro) + les tests pytest existants (Redoublage) comme garde-fou externe.
- **Verrouillage final** : quand la cible est atteinte, mettre à jour
  `WER_REFERENCE_KAAMELOTT` dans `tests/integration/test_wer_fr.py` avec la valeur
  finale, lancer toute la suite `-m integration`, puis rédiger le rapport final.

### Contenu complet de `scripts/mesurer_wer_kaamelott.py` (à créer au rollout)

```python
"""Mesure officielle du WER Kaamelott (métrique de la boucle d'amélioration).

Usage :
    .venv/Scripts/python.exe scripts/mesurer_wer_kaamelott.py [--json]
    .venv/Scripts/python.exe scripts/mesurer_wer_kaamelott.py --analyse-erreurs
    .venv/Scripts/python.exe scripts/mesurer_wer_kaamelott.py --double --json
    .venv/Scripts/python.exe scripts/mesurer_wer_kaamelott.py --modele large-v3 \
        --duree-chunk 15 --recouvrement 2 \
        --vocabulaire backend/data/jobs/46d2608acf96/glossaire.json

Mesure ce qui est IMPLANTÉ dans le code (défauts de transcribe_chunked), avec
quelques réglages exposés en options. Ne décide jamais : le verdict appartient
au vérificateur. Retour 0 si la mesure aboutit, 1 sinon.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.asr import normaliser_mot, normaliser_nombres_fr, transcribe_chunked, wer_fr

RACINE = Path(__file__).resolve().parents[1]
JSON_KAAMELOTT = RACINE / "videos" / "Kaamelott_S01E01_Episode_1.json"
JOB_KAAMELOTT = RACINE / "backend" / "data" / "jobs" / "46d2608acf96"
AUDIO = JOB_KAAMELOTT / "audio_16k.wav"
ETAT = RACINE / "backend" / "data" / "wer_loop_state.json"


def mots_reference() -> list[str]:
    payload = json.loads(JSON_KAAMELOTT.read_text(encoding="utf-8"))
    return [str(m["texte"]) for r in payload["repliques"]
            for m in r.get("mots", [])
            if normaliser_mot(str(m["texte"]))]


def aligner(ref: list[str], hyp: list[str]) -> list[tuple[str, int, int]]:
    """Retrace Levenshtein → liste d'opérations (sub/ins/del) avec indices."""
    n, m = len(ref), len(hyp)
    d = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        d[i][0] = i
    for j in range(m + 1):
        d[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1,
                          d[i - 1][j - 1] + (0 if ref[i - 1] == hyp[j - 1] else 1))
    ops: list[tuple[str, int, int]] = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0 and ref[i - 1] == hyp[j - 1]:
            i, j = i - 1, j - 1
        elif i > 0 and j > 0 and d[i][j] == d[i - 1][j - 1] + 1:
            ops.append(("sub", i - 1, j - 1)); i, j = i - 1, j - 1
        elif j > 0 and d[i][j] == d[i][j - 1] + 1:
            ops.append(("ins", i, j - 1)); j -= 1
        else:
            ops.append(("del", i - 1, j)); i -= 1
    return list(reversed(ops))


def analyser_erreurs(ref: list[str], hyp: list[str], top: int = 15) -> list[str]:
    lignes: list[str] = []
    for op, ri, hi in aligner(ref, hyp):
        if op == "sub":
            c = ref[max(0, ri - 2):ri] + [f"**{ref[ri]}**"] + ref[ri + 1:ri + 3]
            lignes.append(f"sub  réf «{ref[ri]}» → hyp «{hyp[hi]}»  [{' '.join(c)}]")
        elif op == "ins":
            lignes.append(f"ins  «{hyp[hi]}» en trop")
        else:
            lignes.append(f"del  «{ref[ri]}» manquant")
    return lignes[:top]


def charger_vocabulaire(chemin: str | None) -> list[str] | None:
    if not chemin:
        return None
    payload = json.loads(Path(chemin).read_text(encoding="utf-8"))
    return [str(t) for t in payload.get("termes", []) if str(t).strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="WER Kaamelott (métrique de boucle)")
    ap.add_argument("--modele", default="medium")
    ap.add_argument("--duree-chunk", type=float, default=25.0)
    ap.add_argument("--recouvrement", type=float, default=1.0)
    ap.add_argument("--vocabulaire", default=None,
                    help="chemin vers un glossaire.json ; défaut : aucun (comme le test)")
    ap.add_argument("--double", action="store_true",
                    help="2 mesures ; garde le pire WER (conservateur)")
    ap.add_argument("--analyse-erreurs", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if not AUDIO.is_file() or not JSON_KAAMELOTT.is_file():
        print("audio ou référence Kaamelott introuvable", file=sys.stderr)
        return 1
    try:
        import faster_whisper  # noqa: F401
    except Exception as exc:
        print(f"faster-whisper indisponible : {exc}", file=sys.stderr)
        return 1

    ref = mots_reference()
    vocab = charger_vocabulaire(args.vocabulaire)
    mesures: list[float] = []
    nb_hyp = 0
    for _ in range(2 if args.double else 1):
        mots, langue = transcribe_chunked(
            AUDIO, duree_chunk_s=args.duree_chunk,
            recouvrement_s=args.recouvrement, language="fr",
            model_name=args.modele, vocabulaire=vocab)
        hyp = [m.text for m in mots if not m.marqueur]
        mesures.append(wer_fr(ref, hyp))
        nb_hyp = len(hyp)
    wer = max(mesures) if args.double else mesures[0]

    base = 0.125
    if ETAT.is_file():
        base = float(json.loads(ETAT.read_text(encoding="utf-8")).get("wer_de_base", 0.125))

    if args.json:
        print(json.dumps({
            "wer": round(wer, 6), "wer_de_base": base,
            "accepte_si_strictement_inferieur": round(wer, 6) < round(base, 6),
            "nb_mots_reference": len(ref), "nb_mots_hypothese": nb_hyp,
            "modele": args.modele, "duree_chunk_s": args.duree_chunk,
            "recouvrement_s": args.recouvrement,
            "vocabulaire": args.vocabulaire or [],
            "mesures": [round(v, 6) for v in mesures],
        }, ensure_ascii=False, indent=2))
    else:
        print(f"WER Kaamelott = {wer:.4f} ({wer * 100:.2f} %)  [base {base * 100:.2f} %]")
        print(f"référence {len(ref)} mots · hypothèse {nb_hyp} mots · "
              f"modèle {args.modele} · chunk {args.duree_chunk}s · "
              f"recouvrement {args.recouvrement}s")
        if args.analyse_erreurs:
            mots, _ = transcribe_chunked(
                AUDIO, duree_chunk_s=args.duree_chunk,
                recouvrement_s=args.recouvrement, language="fr",
                model_name=args.modele, vocabulaire=vocab)
            hyp = [m.text for m in mots if not m.marqueur]
            print("--- erreurs (top) ---")
            for ligne in analyser_erreurs(ref, hyp):
                print(" ", ligne)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

## Escalade et revue humaine

- **Déclencheurs** : 3 rejets consécutifs, 10 runs sans atteindre la cible, ou
  erreur bloquante (audio/référence manquants, modèle introuvable).
- **Où atterrit l'escalade** : `docs/loops/escalade-kaamelott-wer.md` — contenu :
  historique complet (leviers tentés, résultats), erreurs restantes (top),
  hypothèse sur le plafond atteignable, et recommandations lourdes :
  modèle `large-v3`, ré-décodage ciblé des mots incertains, fine-tuning
  (faster-whisper), ou ASR cloud (`asr_cloud.py` existe déjà, `modele_cloud: whisper-1`).
- **Ce que l'humain révise** : le rapport d'escalade (et, à la fin, le rapport
  final de succès). Pendant la boucle, chaque diff accepté est traçable dans
  l'historique + les sauvegardes — l'humain peut revenir dessus à tout moment.
- **Succès** : `docs/loops/rapport-final-kaamelott-wer.md` — chemin de
  0,125 → valeur finale, liste des leviers implantés (avec gains), verrouillage
  des tests.

## Budget tokens

- **Run maker** : ~40–80k tokens (lecture du code + état, analyse d'erreurs,
  implémentation). Modèle fort (raisonnement + édition).
- **Run vérificateur** : ~15–25k tokens (diff, 2 mesures, verdict). Modèle
  rapide/cheap : le travail coûteux (transcription) est du calcul local, pas des
  tokens.
- **Total par run** : ~60–100k tokens + 1 à 3 transcriptions `medium` de 420 s
  (≈ 3–5 min sur GPU CUDA, 15–30 min sur CPU int8 — la machine cible a CUDA
  d'après les références existantes).
- **Plafond** : 10 runs max → ≤ ~1M tokens + ~1 h de transcription GPU. La cible
  5 % peut être inatteignable avec `medium` : l'escalade (3 échecs consécutifs)
  coupe la dépense avant le plafond.

## Étapes de déploiement

1. Créer `docs/loops/` (ce plan est le livrable de conception).
2. Créer `scripts/mesurer_wer_kaamelott.py` (contenu ci-dessus) et vérifier :
   `.venv/Scripts/python.exe scripts/mesurer_wer_kaamelott.py --json`
   → attendu ≈ `wer: 0.1125`.
3. Créer `backend/data/wer_loop_state.json` (template du schéma, `statut: IDLE`,
   `wer_de_base: 0.125`) et `backend/data/wer_loop_backups/` (vide).
4. **Run 0** : mesurer → poser `wer_de_base = min(0,125, mesure)` (≈ 0,1125).
   Si la mesure dépasse 0,125 (machine différente), la base reste 0,125 et les
   runs suivants cherchent à passer dessous.
5. Écrire `.agents/skills/kaamelott-wer/SKILL.md` (contenu ci-dessus).
6. Écrire `.claude/agents/wer-maker.md` et `.claude/agents/wer-verifier.md`
   (contenus ci-dessus) — ou les utiliser comme prompts de rôle dans cet
   environnement.
7. Lancer la boucle avec le prompt d'automation (relance manuelle ou agent
   autonome) jusqu'à `wer_de_base ≤ 0,05`, escalade, ou arrêt humain.
8. **Verrouillage final** : mettre `WER_REFERENCE_KAAMELOTT` à la valeur finale
   dans `tests/integration/test_wer_fr.py`, lancer `.venv/Scripts/python.exe -m
   pytest -m integration -q`, rédiger `docs/loops/rapport-final-kaamelott-wer.md`.

## Risques et parades

1. **Surapprentissage sur le slice** (7 min de Kaamelott ≠ tout le français) :
   chaque changement doit être une règle générale ; le vérificateur rejette tout
   cas particulier (anti-triche) ; les tests Redoublage restent verrouillés.
2. **Nondéterminisme de la mesure** : le vérificateur mesure 2× et décide sur le
   pire (`wer_verif_max`) ; l'acceptation exige un gain strict (<), pas une
   égalité — un gain dû au bruit ne passe pas.
3. **Pas de git → revert fragile** : sauvegarde systématique des fichiers avant
   modification, restauration depuis `wer_loop_backups/<run_id>/` sur rejet ;
   si une restauration échoue, `derniere_erreur` + escalade (jamais un échec
   silencieux).
4. **Cible 5 % hors d'atteinte avec `medium`** : les compteurs d'escalade
   s'activent (3 échecs consécutifs) et le rapport recommande les options
   lourdes (large-v3, ré-décodage ciblé, ASR cloud) — la décision revient à
   l'humain.
