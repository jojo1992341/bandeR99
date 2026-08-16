"""Transcription locale mot-à-mot : faster-whisper (base) + affinage WhisperX (alignement forcé).

Aucune donnée ne quitte la machine : modèles téléchargés une fois depuis Hugging Face
puis servis en local, sur GPU CUDA si disponible, sinon CPU (int8).
"""
from __future__ import annotations

import unicodedata
import wave
from dataclasses import dataclass
from pathlib import Path

from .devices import choose_device, compute_type


@dataclass(frozen=True)
class Word:
    """Un mot reconnu avec son intervalle temporel (secondes).

    ``marqueur`` : mot entre parenthèses (symbole de respiration, T80–T84) —
    non prononcé, rendu distinctement sur la bande.
    """

    text: str
    start: float
    end: float
    probability: float = 0.0
    marqueur: bool = False


_ASR_CACHE: dict[tuple[str, str, str], object] = {}
_ALIGN_CACHE: dict[tuple[str, str], object] = {}


def get_asr_model(model_name: str = "base", device: str | None = None,
                  compute: str | None = None):
    """Charge (une seule fois) un modèle faster-whisper. Instance mise en cache."""
    device = device or choose_device()
    compute = compute or compute_type(device)
    cle = (model_name, device, compute)
    if cle not in _ASR_CACHE:
        from faster_whisper import WhisperModel

        _ASR_CACHE[cle] = WhisperModel(model_name, device=device, compute_type=compute)
    return _ASR_CACHE[cle]


def duree_wav(chemin: str | Path) -> float:
    """Durée (s) d'un WAV PCM lisible par le module stdlib ``wave``."""
    with wave.open(str(chemin)) as w:
        return w.getnframes() / float(w.getframerate())


def charger_wav_float32(chemin: str | Path):
    """Charge un WAV PCM 16 bits mono en np.float32 normalisé [-1, 1] (pour WhisperX)."""
    import numpy as np

    with wave.open(str(chemin)) as w:
        rate = w.getframerate()
        brut = w.readframes(w.getnframes())
    return np.frombuffer(brut, dtype=np.int16).astype(np.float32) / 32768.0, rate


def _affiner_avec_whisperx(segments: list[dict], chemin_wav: str | Path,
                            langue: str, device: str) -> list[Word]:
    """Alignement forcé WhisperX (wav2vec2) : affine start/end de chaque mot.

    Best-effort : toute indisponibilité (mémoire, modèle d'alignement absent pour
    la langue, etc.) remonte l'exception pour bascule sur les timestamps natifs.
    """
    import whisperx

    cle = (langue, device)
    if cle not in _ALIGN_CACHE:
        _ALIGN_CACHE[cle] = whisperx.load_align_model(language_code=langue, device=device)
    modele_align, metadonnees = _ALIGN_CACHE[cle]
    audio, rate = charger_wav_float32(chemin_wav)
    assert rate == 16000, "WhisperX attend de l'audio 16 kHz"
    sortie = whisperx.align(
        segments, modele_align, metadonnees, audio, device,
        return_char_alignments=False,
    )
    mots: list[Word] = []
    for seg in sortie.get("word_segments", []):
        if seg.get("start") is None or seg.get("end") is None:
            continue
        mots.append(Word(
            text=seg.get("word", "").strip(),
            start=float(seg["start"]),
            end=float(seg["end"]),
            probability=float(seg.get("score") or 0.0),
        ))
    return mots


# ---------------------------------------------------------------------------
# Prolongation acoustique des syllabes tenues (T50) — affinée par les phonèmes
# ---------------------------------------------------------------------------
# Constat réel (Redoublage.mp4, ~9 s) : la syllabe « cis » de « Francis » est
# tenue par l'acteur pendant plus d'une seconde (voyelle /i/ maintenue, bouche
# ouverte en continu). L'ASR (faster-whisper) puis l'alignement forcé WhisperX
# bornent le mot au phonème consonantique et coupent la tenue — le mot se
# retrouve tronqué de ~0,5 s de parole réelle.
#
# T50 prolongeait la fin de mot sur la seule enveloppe d'énergie (une
# « syllabe » = un bloc d'énergie) : n'importe quel son au-dessus du seuil
# étendait le mot, y compris une fricative sourde ou un bruit. T116 remplace
# cette lecture « syllabique » par une lecture PHONÉMIQUE : la tenue d'un
# comédien est une VOYELLE (phonème voisé, quasi périodique) — on ne prolonge
# le mot qu'à travers les trames VOISÉES (énergie suffisante ET taux de
# passages par zéro bas), en s'arrêtant au premier run sourd/silencieux.
# Résultat : la fin de mot épouse le dernier phonème voisé tenu, sans jamais
# absorber la consonne sourde ou l'attaque bruitée qui suit.

_FENETRE_RMS_S = 0.020          # fenêtre d'énergie (s)
_SEUIL_RELATIF = 0.15           # fraction du pic d'énergie de la fenêtre de recherche
_PLANCHER_ABS = 0.006           # RMS normalisé : au-dessus du bruit de fond typique
_PAUSE_SILENCE_S = 0.12         # un trou ≥ cette durée stoppe la prolongation
_MAX_EXTENSION_S = 1.5          # plafond de prolongation (s)
_MARGE_SUIVANT_S = 0.03         # jamais au-delà du début du mot suivant − marge
_MIN_EXTENSION_S = 0.06         # en-deçà : on garde la borne de l'aligneur (stabilité)
_PAUSE_AVANT_S = 0.25           # pas de prolongation si le mot suivant enchaîne
_ZCR_VOISEE_MAX_HZ = 3000.0     # voyelles/nasales ≪ 3 kHz ; fricatives sourdes ≫ 3 kHz
_MARGE_ONSET_ARRIERE_S = 0.15   # on remonte au plus 150 ms pour rattraper une attaque voisée
_MARGE_ONSET_AVANT_S = 0.10     # on avance au plus 100 ms pour couper un silence d'attaque
_MARGE_INTER_MOTS_S = 0.02      # le début ne mord jamais sur la fin du mot précédent
_RUN_VOISE_MIN = 2              # trames voisées consécutives (2 × 20 ms) pour un onset fiable
_SEUIL_RELATIF_ONSET = 0.10     # fraction du pic local d'énergie (plancher absolu en repli)
_MIN_DUREE_MOT_S = 0.04         # un mot garde toujours une durée minimale


def enveloppe_parole(chemin_wav: str | Path,
                     fenetre_s: float = _FENETRE_RMS_S) -> tuple[np.ndarray, np.ndarray]:
    """(rms, zc_hz) par fenêtre : énergie + taux de passages par zéro.

    ``zc_hz`` = passages par zéro par seconde, indépendant du taux
    d'échantillonnage : une voyelle ou une nasale est quasi périodique
    (zc_hz ≈ 2·F0, bas) ; une fricative sourde (/s/, /f/…), une aspiration ou
    un bruit large bande traverse zéro très souvent (zc_hz haut). C'est le
    discriminateur voisé/sourd qui permet de raisonner en PHONÈMES.
    Lecture streamée par blocs : mémoire bornée, même sur 90 min.
    """
    import numpy as np

    energies: list[np.ndarray] = []
    passages: list[np.ndarray] = []
    with wave.open(str(chemin_wav)) as w:
        rate = w.getframerate()
        pas = max(1, int(rate * fenetre_s))
        while True:
            brut = w.readframes(pas * 64)  # 64 fenêtres par bloc
            if not brut:
                break
            bloc = np.frombuffer(brut, dtype=np.int16).astype(np.float32) / 32768.0
            n = len(bloc) // pas
            if n:
                morceau = bloc[: n * pas].reshape(n, pas)
                energies.append(morceau.std(axis=1))
                # signes des échantillons : un passage par zéro = signe qui
                # change entre deux échantillons adjacents d'une même fenêtre
                signe = np.signbit(morceau)
                comptes = np.count_nonzero(signe[:, 1:] != signe[:, :-1], axis=1)
                passages.append(comptes.astype(np.float64) / fenetre_s)
    if not energies:
        return np.zeros(0, dtype=np.float64), np.zeros(0, dtype=np.float64)
    return (np.concatenate(energies).astype(np.float64),
            np.concatenate(passages).astype(np.float64))


def enveloppe_rms(chemin_wav: str | Path,
                  fenetre_s: float = _FENETRE_RMS_S) -> np.ndarray:
    """Enveloppe d'énergie RMS (par fenêtre glissante) d'un WAV mono PCM.

    Taux d'échantillonnage quelconque : les fenêtres sont définies en secondes.
    Lecture streamée par blocs : la mémoire reste bornée (seule l'enveloppe,
    ~50 valeurs/s, est conservée) même pour les très longs fichiers.
    """
    return enveloppe_parole(chemin_wav, fenetre_s)[0]


def prolonger_fins_sur_audio(mots: list[Word], enveloppe: np.ndarray,
                             duree_fenetre_s: float = _FENETRE_RMS_S,
                             seuil_relatif: float = _SEUIL_RELATIF,
                             plancher: float = _PLANCHER_ABS,
                             max_extension_s: float = _MAX_EXTENSION_S,
                             marge_s: float = _MARGE_SUIVANT_S,
                             pause_silence_s: float = _PAUSE_SILENCE_S,
                             min_extension_s: float = _MIN_EXTENSION_S,
                             pause_avant_s: float = _PAUSE_AVANT_S,
                             zc_hz: np.ndarray | None = None,
                             zcr_max: float = _ZCR_VOISEE_MAX_HZ) -> list[Word]:
    """Étend la fin de chaque mot jusqu'au dernier PHONÈME VOISÉ tenu.

    Pour un mot, on cherche dans ``[fin, min(fin + max_extension, début du mot
    suivant − marge)]`` le dernier instant où la trame est de la PAROLE VOISÉE :
    énergie au-dessus du seuil ET (si ``zc_hz`` est fourni) taux de passages
    par zéro ≤ ``zcr_max`` — la tenue d'un comédien est une voyelle/nasale
    tenue (voisée, quasi périodique), pas un bruit ni une fricative sourde.
    On s'arrête au premier trou ≥ ``pause_silence`` : une tenue voisée
    prolonge le mot ; un silence, une consonne sourde ou une nouvelle attaque
    de parole après un trou ne sont jamais absorbés.

    ``enveloppe`` : valeurs RMS par fenêtre (``enveloppe_rms``) ; ``zc_hz`` :
    taux de passages par zéro (``enveloppe_parole``), optionnel — sans lui, le
    comportement historique T50 (énergie seule) est conservé. Les bornes
    gardées sont ``start`` et ``probability`` du mot d'origine.
    """
    duree_fenetre_s = float(duree_fenetre_s)
    pas = max(1, int(pause_silence_s / duree_fenetre_s))
    prolonges: list[Word] = []
    for i, m in enumerate(mots):
        suiv_debut = mots[i + 1].start if i + 1 < len(mots) else m.end + max_extension_s
        if suiv_debut - m.end < pause_avant_s:  # le mot suivant enchaîne : rien à étendre
            prolonges.append(m)
            continue
        borne = min(m.end + max_extension_s, suiv_debut - marge_s)
        if borne <= m.end:
            prolonges.append(m)
            continue
        i0 = int(m.end / duree_fenetre_s)
        i1 = min(int(borne / duree_fenetre_s), len(enveloppe) - 1)
        if i1 <= i0:
            prolonges.append(m)
            continue
        fen = enveloppe[i0:i1 + 1]
        voise = None
        if zc_hz is not None:
            zc_fen = zc_hz[i0:i1 + 1]
            if len(zc_fen) == len(fen):
                voise = zc_fen <= zcr_max
        pic = float(fen.max())
        seuil = max(seuil_relatif * pic, plancher)
        dernier = -1
        trou = 0
        for k, v in enumerate(fen):
            parle = v >= seuil and (voise is None or bool(voise[k]))
            if parle:
                dernier = k
                trou = 0
            else:
                trou += 1
                if trou >= pas:
                    break
        nouvelle = None
        if dernier >= 0:
            fin_etendue = (i0 + dernier + 1) * duree_fenetre_s
            if fin_etendue - m.end >= min_extension_s:
                nouvelle = min(fin_etendue, suiv_debut - marge_s)
        prolonges.append(Word(m.text, m.start,
                              nouvelle if nouvelle is not None else m.end,
                              m.probability, marqueur=m.marqueur))

    return prolonges


def _premier_instant_voise(rms: np.ndarray, zc: np.ndarray, i0: int, i1: int,
                           seuil: float, zcr_max: float, duree_fenetre_s: float,
                           run_min: int) -> float | None:
    """Instant (s) du premier RUN voisé de trames dans [i0, i1], ou None.

    Une trame est voisée si son énergie est ≥ ``seuil`` ET son taux de passages
    par zéro ≤ ``zcr_max``. On exige ``run_min`` trames voisées consécutives
    pour ne pas déclencher sur un artefact isolé (clic, parasite périodique).
    """
    voise = (rms[i0:i1 + 1] >= seuil) & (zc[i0:i1 + 1] <= zcr_max)
    compte = 0
    for k in range(len(voise)):
        if voise[k]:
            compte += 1
            if compte >= run_min:
                return (i0 + k - run_min + 1) * duree_fenetre_s
        else:
            compte = 0
    return None


def recaler_onsets_sur_audio(mots: list[Word], enveloppe: np.ndarray,
                             zc_hz: np.ndarray | None = None,
                             duree_fenetre_s: float = _FENETRE_RMS_S,
                             seuil_relatif: float = _SEUIL_RELATIF_ONSET,
                             plancher: float = _PLANCHER_ABS,
                             zcr_max: float = _ZCR_VOISEE_MAX_HZ,
                             marge_arriere_s: float = _MARGE_ONSET_ARRIERE_S,
                             marge_avant_s: float = _MARGE_ONSET_AVANT_S,
                             marge_inter_s: float = _MARGE_INTER_MOTS_S,
                             run_voise_min: int = _RUN_VOISE_MIN,
                             min_duree_s: float = _MIN_DUREE_MOT_S) -> list[Word]:
    """Recale le DÉBUT de chaque mot sur la vraie attaque de parole (onset voisé).

    Le timestamp ASR seul peut démarrer le mot dans le silence (borne trop tôt)
    ou après le début réel du voisement (attaque voisée douce ratée). Ici, pour
    chaque mot, on cherche dans ``[début − marge_arrière, début + marge_avant]``
    (borné par la fin du mot précédent et par une durée minimale) le PREMIER run
    de trames VOISÉES — énergie ≥ seuil ET passages par zéro ≤ ``zcr_max`` :
    c'est l'onset du phonème voisé, la vraie attaque de la parole. Sans onset
    voisé dans la fenêtre, le début ASR est conservé (repli timing audio).

    ``enveloppe``/``zc_hz`` : ``enveloppe_parole`` ; sans ``zc_hz``, aucun
    recalage (fonction identité). Les fins de mots sont conservées telles
    quelles — à appliquer APRÈS ``prolonger_fins_sur_audio``.
    """
    if zc_hz is None or len(zc_hz) != len(enveloppe):
        return list(mots)
    resultat: list[Word] = []
    for i, m in enumerate(mots):
        debut, fin = float(m.start), float(m.end)
        lo = max(0.0, debut - marge_arriere_s)
        if i > 0:
            lo = max(lo, float(mots[i - 1].end) + marge_inter_s)
        hi = min(debut + marge_avant_s, fin - min_duree_s)
        nouveau = debut
        if hi > lo:
            i0 = max(0, int(lo / duree_fenetre_s))
            i1 = min(len(enveloppe) - 1, int(hi / duree_fenetre_s))
            if i1 > i0:
                pic = float(enveloppe[i0:i1 + 1].max())
                seuil = max(seuil_relatif * pic, plancher)
                onset = _premier_instant_voise(enveloppe, zc_hz, i0, i1, seuil,
                                               zcr_max, duree_fenetre_s,
                                               run_voise_min)
                if onset is not None:
                    nouveau = min(max(onset, lo), hi)
        nouveau = max(0.0, min(nouveau, fin - min_duree_s))
        resultat.append(Word(m.text, nouveau, m.end, m.probability,
                              marqueur=m.marqueur))
    return resultat


# ---------------------------------------------------------------------------
# Détection des respirations / bruitages (Kr, souffles) — T127
# ---------------------------------------------------------------------------
# La référence manuelle (mfd2.json) matérialise chaque respiration du comédien
# par un marqueur « (Kr) » entre parenthèses, posé dans le silence qui sépare
# deux mots. Acoustiquement une respiration est un souffle APÉRIODIQUE : large
# bande, donc taux de passages par zéro élevé (≫ celui d'une voyelle voisée),
# énergie audible mais inférieure à celle de la parole. On ne cherche ces
# bouffées que DANS LES TROUS entre les mots (là où l'ASR n'a rien transcrit) :
# une consonne sourde en pleine syllabe ne peut pas être confondue avec un
# souffle, et une marge de chaque côté du trou écarte la consonne de liaison.

_ZCR_SOUFFLE_MIN_HZ = 3500.0     # plancher apériodique (voyelle ≪ 3 kHz)
_ZCR_MUSIQUE_MAX_HZ = 3000.0     # musique/ton tenu : périodique (voyelle-like ≪ 3 kHz)
_RMS_SOUFFLE_MIN = 0.008         # énergie audible (au-dessus du bruit de fond)
_RMS_SOUFFLE_RATIO_FOND = 2.0    # le souffle doit ressortir du bruit de fond
_DUREE_SOUFFLE_MIN_S = 0.06      # en-deçà : clic/artefact, pas une respiration
_DUREE_SOUFFLE_MAX_S = 1.2       # au-delà : bruitage long, pas une respiration
_DUREE_BRUITAGE_MIN_S = 1.5      # un trou long rempli de bruit = bruitage/effet
_MARGE_GAP_SOUFFLE_S = 0.05      # 50 ms ignorées de chaque côté (collées au mot)


def _runs_aperiodiques(rms: np.ndarray, zc_hz: np.ndarray, i0: int, i1: int,
                       zcr_min: float, rms_min: float,
                       duree_fenetre_s: float) -> list[tuple[float, float]]:
    """Intervalles (s) des runs de trames apériodiques (zc ≥ zcr_min, rms ≥ rms_min)
    dans la fenêtre de trames [i0, i1] inclusive."""
    runs: list[tuple[float, float]] = []
    debut = None
    for k in range(i0, i1 + 1):
        if zc_hz[k] >= zcr_min and rms[k] >= rms_min:
            if debut is None:
                debut = k
        elif debut is not None:
            runs.append((debut * duree_fenetre_s, k * duree_fenetre_s))
            debut = None
    if debut is not None:
        runs.append((debut * duree_fenetre_s, (i1 + 1) * duree_fenetre_s))
    return runs


def _runs_periodiques(rms: np.ndarray, zc_hz: np.ndarray, i0: int, i1: int,
                      zcr_max: float, rms_min: float,
                      duree_fenetre_s: float) -> list[tuple[float, float]]:
    """Intervalles (s) des runs de trames PÉRIODIQUES (zc ≤ zcr_max, rms ≥ rms_min)
    dans la fenêtre de trames [i0, i1] inclusive — musique, ton tenu, note.

    La musique est voisée (quasi périodique, passages par zéro bas), à l'opposé
    du bruit large bande : c'est ce qui distingue « (musique) » de « (Bruitages) »
    dans un silence sans parole.
    """
    runs: list[tuple[float, float]] = []
    debut = None
    for k in range(i0, i1 + 1):
        if zc_hz[k] <= zcr_max and rms[k] >= rms_min:
            if debut is None:
                debut = k
        elif debut is not None:
            runs.append((debut * duree_fenetre_s, k * duree_fenetre_s))
            debut = None
    if debut is not None:
        runs.append((debut * duree_fenetre_s, (i1 + 1) * duree_fenetre_s))
    return runs


def detecter_souffles(mots: list[Word], rms: np.ndarray,
                      zc_hz: np.ndarray | None,
                      duree_fenetre_s: float = _FENETRE_RMS_S,
                      zcr_min: float = _ZCR_SOUFFLE_MIN_HZ,
                      zcr_max: float = _ZCR_MUSIQUE_MAX_HZ,
                      rms_min: float = _RMS_SOUFFLE_MIN,
                      ratio_fond: float = _RMS_SOUFFLE_RATIO_FOND,
                      duree_min_s: float = _DUREE_SOUFFLE_MIN_S,
                      duree_max_s: float = _DUREE_SOUFFLE_MAX_S,
                      duree_bruitage_min_s: float = _DUREE_BRUITAGE_MIN_S,
                      marge_gap_s: float = _MARGE_GAP_SOUFFLE_S) -> list[Word]:
    """Insère des marqueurs « (Kr) » (respiration) et « (Bruitages) » (effet long)
    dans les silences entre les mots, détectés acoustiquement.

    On n'analyse que les trous entre deux mots consécutifs (et celui qui précède
    le premier mot), amputés d'une petite marge de chaque côté. Dans chaque trou,
    un run de trames APÉRIODIQUES (zc ≥ ``zcr_min``) ET audibles (rms ≥
    ``rms_min``, et qui ressortent du bruit de fond de l'enregistrement d'un
    facteur ``ratio_fond``) devient :

    - « (Kr) » si sa durée tient dans [``duree_min_s``, ``duree_max_s``] ;
    - « (Bruitages) » s'il dépasse ``duree_bruitage_min_s`` (long bruit/effet).

    La musique (ton tenu, note) est PÉRIODIQUE, donc invisible à la règle
    apériodique : un run de trames voisées (zc ≤ ``zcr_max``) soutenu au-delà de
    ``duree_bruitage_min_s`` devient « (musique) » ; si le même trou porte aussi
    un long bruit, les deux fusionnent en « (musique_et_bruitage) » (réf.
    Redoublage.json). Les respirations courtes ne sont jamais posées à
    l'intérieur d'un long marqueur déjà émis.

    Sans ``zc_hz`` (ou tailles incohérentes), la fonction est l'identité : les
    mots parlés ne sont jamais modifiés, seuls des marqueurs sont ajoutés.
    """
    import numpy as np

    if zc_hz is None or len(zc_hz) != len(rms) or not mots:
        return list(mots)
    fenetres: list[tuple[float, float]] = []
    if mots[0].start - marge_gap_s > duree_min_s:
        fenetres.append((0.0, mots[0].start - marge_gap_s))
    for a, b in zip(mots, mots[1:]):
        lo = float(a.end) + marge_gap_s
        hi = float(b.start) - marge_gap_s
        if hi - lo >= duree_min_s:
            fenetres.append((lo, hi))
    # Plancher « silence » estimé sur TOUTE l'enveloppe (10e centile) : un
    # souffle doit ressortir du bruit de fond de l'enregistrement, pas du trou
    # lui-même (un trou rempli de bruit a un fond… de bruit).
    fond = float(np.percentile(rms, 10)) if len(rms) else rms_min
    seuil = max(rms_min, fond * ratio_fond)
    marqueurs: list[Word] = []
    for lo, hi in fenetres:
        i0 = int(lo / duree_fenetre_s)
        i1 = min(int(hi / duree_fenetre_s), len(rms) - 1)
        if i1 < i0:
            continue
        runs_bruit = _runs_aperiodiques(rms, zc_hz, i0, i1, zcr_min, seuil,
                                        duree_fenetre_s)
        runs_musique = _runs_periodiques(rms, zc_hz, i0, i1, zcr_max, seuil,
                                         duree_fenetre_s)
        longs_bruit = [r for r in runs_bruit
                       if r[1] - r[0] >= duree_bruitage_min_s]
        longs_musique = [r for r in runs_musique
                         if r[1] - r[0] >= duree_bruitage_min_s]
        # Musique ET bruit cohabitants → un seul marqueur (réf. Redoublage).
        if longs_musique and longs_bruit:
            debut = min(min(r[0] for r in longs_musique),
                        min(r[0] for r in longs_bruit))
            fin = max(max(r[1] for r in longs_musique),
                      max(r[1] for r in longs_bruit))
            marqueurs.append(Word("(musique_et_bruitage)", max(debut, lo),
                                  min(fin, hi), marqueur=True))
            continue
        for debut, fin in longs_musique:
            marqueurs.append(Word("(musique)", max(debut, lo), min(fin, hi),
                                  marqueur=True))
        for debut, fin in longs_bruit:
            marqueurs.append(Word("(Bruitages)", max(debut, lo), min(fin, hi),
                                  marqueur=True))
        # Respirations courtes (Kr) : runs apériodiques brefs, jamais posés à
        # l'intérieur d'un long marqueur musique/bruitage déjà émis.
        for debut, fin in runs_bruit:
            debut, fin = max(debut, lo), min(fin, hi)
            duree = fin - debut
            if duree < duree_min_s or duree > duree_max_s:
                continue
            if any(debut < b[1] and b[0] < fin
                   for b in longs_bruit + longs_musique):
                continue
            marqueurs.append(Word("(Kr)", debut, fin, marqueur=True))
    if not marqueurs:
        return list(mots)
    return sorted(mots + marqueurs, key=lambda m: (m.start, m.end))


def validate_words(mots: list[Word], duree: float, recouvrement_max: float = 0.020) -> list[Word]:
    """Garantit : 0 ≤ start < end ≤ durée, tri croissant, pas de chevauchement.

    Les chevauchements résiduels (≤ ``recouvrement_max`` s) sont rabotés ; au-delà,
    le mot fautif est tronqué à la borne saine.
    """
    propres: list[Word] = []
    for m in sorted(mots, key=lambda m: (m.start, m.end)):
        debut = min(max(m.start, 0.0), duree)
        fin = min(max(m.end, debut), duree)
        if fin - debut <= 0 or not m.text.strip():
            continue
        if propres and debut < propres[-1].end:  # léger chevauchement ASR : on rabote
            precedent = propres[-1]
            raccourci = Word(precedent.text, precedent.start,
                             min(precedent.end, debut), precedent.probability,
                             marqueur=precedent.marqueur)
            if raccourci.end - raccourci.start >= recouvrement_max:
                propres[-1] = raccourci
            debut = max(debut, propres[-1].end)
            fin = max(fin, debut)
            if fin - debut <= 0:
                continue
        propres.append(Word(m.text.strip(), debut, fin, m.probability,
                             marqueur=m.marqueur))
    return propres


_PONCTUATION_SEULE = frozenset((".", ",", ";", ":", "!", "?", "…", "..."))


def fusionner_fragments_fr(mots: list[Word], max_gap_s: float = 0.12,
                           max_duree_ponctuation_s: float = 0.35) -> list[Word]:
    """Rassemble les fragments français produits par l'ASR.

    faster-whisper/WhisperX séparent fréquemment ``d`` + ``'avoir``,
    ``est`` + ``-ce`` ou un mot + ``?`` en plusieurs « mots ». Pour une bande
    rythmo, cette fragmentation est doublement pénalisante : le texte devient
    faux et la punctuation reçoit une case/une durée propre. On fusionne donc
    les apostrophes, les traits d'union et la ponctuation courte, sans toucher
    aux vrais mots séparés par une pause.

    Une ponctuation qui traîne longtemps (hallucination dans un silence) est
    fusionnée au texte mais ne prolonge pas sa borne audio : cela évite le cas
    classique « vieux? » tenu artificiellement pendant deux secondes.
    """
    resultat: list[Word] = []
    for mot in sorted(mots, key=lambda w: (w.start, w.end)):
        texte = mot.text.strip()
        if not texte:
            continue
        courant = Word(texte, mot.start, mot.end, mot.probability, mot.marqueur)
        if not resultat:
            resultat.append(courant)
            continue
        precedent = resultat[-1]
        gap = float(courant.start - precedent.end)
        ponctuation = texte in _PONCTUATION_SEULE
        fragment = (
            texte.startswith(("'", "’", "-"))
            or precedent.text.endswith(("'", "’", "-"))
        )
        if gap <= max_gap_s and (fragment or ponctuation):
            fin = precedent.end
            if not ponctuation or courant.end - precedent.end <= max_duree_ponctuation_s:
                fin = max(fin, courant.end)
            resultat[-1] = Word(
                precedent.text + texte,
                precedent.start,
                fin,
                (precedent.probability + courant.probability) / 2.0,
                precedent.marqueur or courant.marqueur,
            )
            continue
        resultat.append(courant)
    return resultat


def _transcrire_fichier(chemin_wav: str | Path, language: str | None = None,
                        model_name: str = "base"):
    """Transcription brute d'un fichier WAV 16 kHz → (mots bruts, langue).

    Point d'injection unique (tests) pour toute transcription. Le chemin fichier
    (et non un tableau numpy) est passé à faster-whisper : décodage interne fiable.
    """
    modele = get_asr_model(model_name)
    segments_iter, info = modele.transcribe(
        str(chemin_wav),
        language=language,
        word_timestamps=True,
        # Décodage stable plutôt que le greedy par défaut : les noms propres,
        # contractions et dialogues familiers gagnent nettement en précision.
        beam_size=5,
        best_of=5,
        temperature=0.0,
        # Des silences de plateau courts sont des frontières utiles pour la
        # bande ; le VAD par défaut (souvent ~2 s) les gomme trop facilement.
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 300, "speech_pad_ms": 80},
        condition_on_previous_text=False,
    )
    segments = list(segments_iter)
    langue = language or getattr(info, "language", "") or ""
    mots = [
        Word(text=w.word, start=float(w.start), end=float(w.end),
             probability=float(w.probability or 0.0))
        for s in segments
        for w in (s.words or [])
    ]
    return mots, langue


def transcribe_words(chemin_wav: str | Path, language: str | None = None,
                     model_name: str = "base", affiner: bool = True) -> tuple[list[Word], str]:
    """Transcrit un WAV 16 kHz mono : retourne (mots horodatés, code langue).

    Timestamps affinés par alignement forcé WhisperX quand c'est possible ;
    sinon timestamps natifs faster-whisper (word_timestamps=True).
    Dans tous les cas, les fins de mots sont prolongées sur l'énergie réelle
    (T50) : une syllabe tenue par l'acteur ne doit pas être tronquée par le
    modèle — le mot s'étend jusqu'au dernier son de parole.
    """
    chemin_wav = Path(chemin_wav)
    duree = duree_wav(chemin_wav)
    mots, langue = _transcrire_fichier(chemin_wav, language=language, model_name=model_name)
    mots_finaux = mots
    if affiner and mots and langue:
        try:
            segs: list[dict] = []
            courant: dict | None = None
            for m in mots:  # regroupe en segments soufflés pour l'aligneur
                if courant is None or m.start - courant["end"] > 0.45:
                    courant = {"start": m.start, "end": m.end, "text": ""}
                    segs.append(courant)
                courant["text"] = (courant["text"] + " " + m.text).strip()
                courant["end"] = m.end
            mots_alignes = _affiner_avec_whisperx(segs, chemin_wav, langue, choose_device())
            if mots_alignes:
                mots_finaux = mots_alignes
        except Exception:
            pass  # repli timestamps natifs
    mots_finaux = fusionner_fragments_fr(mots_finaux)
    rms, zc = enveloppe_parole(chemin_wav)
    mots_finaux = prolonger_fins_sur_audio(mots_finaux, rms, zc_hz=zc)
    mots_finaux = recaler_onsets_sur_audio(mots_finaux, rms, zc)
    mots_finaux = detecter_souffles(mots_finaux, rms, zc)
    return validate_words(mots_finaux, duree), langue


def transcribe_chunked(chemin_wav: str | Path, duree_chunk_s: float = 25.0,
                       recouvrement_s: float = 1.0, language: str | None = None,
                       model_name: str = "base") -> tuple[list[Word], str]:
    """Transcrit un long WAV par fenêtres glissantes puis fusionne sans doublon ni trou.

    Chaque fenêtre « possède » les mots débutant dans ``[début_fenêtre, début_fenêtre+pas)``
    (dernière fenêtre : jusqu'à la fin) — les mots du recouvrement sont dédupliqués.
    """
    import tempfile

    if duree_wav(chemin_wav) <= duree_chunk_s:
        mots, langue = _transcrire_fichier(chemin_wav, language=language, model_name=model_name)
        mots = fusionner_fragments_fr(mots)
        rms, zc = enveloppe_parole(chemin_wav)
        mots = prolonger_fins_sur_audio(mots, rms, zc_hz=zc)
        mots = recaler_onsets_sur_audio(mots, rms, zc)
        mots = detecter_souffles(mots, rms, zc)
        return validate_words(mots, duree_wav(chemin_wav)), langue

    mots_fusionnes: list[Word] = []
    langue = language or ""
    with tempfile.TemporaryDirectory(prefix="rythmo_chunks_") as tampon_dir, \
            wave.open(str(chemin_wav)) as src:
        rate = src.getframerate()
        duree = src.getnframes() / rate
        pas = duree_chunk_s - recouvrement_s
        debuts: list[float] = []
        t = 0.0
        while t < duree:
            debuts.append(t)
            t += pas

        tampon = Path(tampon_dir)
        for i, debut in enumerate(debuts):
            fin = min(debut + duree_chunk_s, duree)
            # lecture fenêtrée directement depuis le WAV : mémoire bornée (1 fenêtre)
            src.setpos(int(debut * rate))
            brutes = src.readframes(int((fin - debut) * rate))
            chemin_chunk = tampon / f"chunk_{i:05d}.wav"
            with wave.open(str(chemin_chunk), "wb") as w:
                w.setparams(src.getparams())
                w.writeframes(brutes)
            mots, langue_chunk = _transcrire_fichier(chemin_chunk, language=language or None,
                                                     model_name=model_name)
            langue = langue or langue_chunk
            limite_haut = debuts[i + 1] if i + 1 < len(debuts) else duree + 1.0
            for m in mots:
                debut_global, fin_global = m.start + debut, m.end + debut
                if debut <= debut_global < limite_haut:  # zone « possédée » par ce chunk
                    mots_fusionnes.append(Word(m.text, debut_global,
                                               min(fin_global, duree), m.probability,
                                               marqueur=m.marqueur))
    mots_fusionnes = fusionner_fragments_fr(mots_fusionnes)
    rms, zc = enveloppe_parole(chemin_wav)
    mots_fusionnes = prolonger_fins_sur_audio(mots_fusionnes, rms, zc_hz=zc)
    mots_fusionnes = recaler_onsets_sur_audio(mots_fusionnes, rms, zc)
    mots_fusionnes = detecter_souffles(mots_fusionnes, rms, zc)
    return validate_words(mots_fusionnes, duree), langue


def normaliser_mot(texte: str) -> str:
    """Minuscules, sans accents ni ponctuation — pour comparaisons tolérantes."""
    texte = unicodedata.normalize("NFD", texte.lower().strip())
    return "".join(c for c in texte if unicodedata.category(c) == "Ll")
