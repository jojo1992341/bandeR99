"""Transcription locale mot-à-mot : faster-whisper (base) + affinage WhisperX (alignement forcé).

Aucune donnée ne quitte la machine : modèles téléchargés une fois depuis Hugging Face
puis servis en local, sur GPU CUDA si disponible, sinon CPU (int8).
"""
from __future__ import annotations

import re
import unicodedata
import wave
from dataclasses import dataclass
from pathlib import Path

from .devices import choose_device, compute_type
from .errors import RythmoError
from .symboles import est_symbole


@dataclass(frozen=True)
class Word:
    """Un mot reconnu avec son intervalle temporel (secondes).

    ``marqueur`` : mot entre parenthèses (symbole de respiration, T80–T84) —
    non prononcé, rendu distinctement sur la bande.
    ``incertain`` : probabilité captée strictement sous le seuil (Slice 16) —
    jamais un marqueur, un symbole ou un mot sans probabilité captée.
    """

    text: str
    start: float
    end: float
    probability: float = 0.0
    marqueur: bool = False
    incertain: bool = False


_ASR_CACHE: dict[tuple[str, str, str], object] = {}
_ALIGN_CACHE: dict[tuple[str, str], object] = {}


def get_asr_model(model_name: str = "base", device: str | None = None,
                  compute: str | None = None):
    """Charge (une seule fois) un modèle faster-whisper. Instance mise en cache.

    CUDA prioritaire ; si le chargement CUDA échoue (OOM, pilote, modèle trop
    gros pour la VRAM), repli CPU (int8) et mise en cache sous la clé
    device/compute réellement utilisée.
    """
    device = device or choose_device()
    compute = compute or compute_type(device)
    cle = (model_name, device, compute)
    if cle not in _ASR_CACHE:
        from faster_whisper import WhisperModel

        from .devices import charger_sur_device

        modele, device_reel = charger_sur_device(
            lambda d: WhisperModel(model_name, device=d, compute_type=compute_type(d)),
            device=device)
        cle_reel = (model_name, device_reel, compute_type(device_reel))
        _ASR_CACHE[cle_reel] = modele
        _ASR_CACHE[cle] = modele  # alias : on ne retente pas CUDA au prochain appel
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

    from .devices import charger_sur_device

    cle = (langue, device)
    if cle not in _ALIGN_CACHE:
        modele, device_reel = charger_sur_device(
            lambda d: whisperx.load_align_model(language_code=langue, device=d),
            device=device)
        _ALIGN_CACHE[cle] = (modele, device_reel)
    modele, device_reel = _ALIGN_CACHE[cle]
    modele_align, metadonnees = modele
    audio, rate = charger_wav_float32(chemin_wav)
    assert rate == 16000, "WhisperX attend de l'audio 16 kHz"
    sortie = whisperx.align(
        segments, modele_align, metadonnees, audio, device_reel,
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

# Élisions françaises produites SANS apostrophe par l'ASR : un clitique d'une
# lettre (« d », « j », « l »…) ou une tige élidée (« aujourd », « jusqu »…)
# s'accole au mot suivant commençant par une voyelle (ou un h, traité muet).
_ELISIONS_CLITIQUES = frozenset(("c", "d", "j", "l", "m", "n", "qu", "s", "t"))
_ELISIONS_TIGES = frozenset(("aujourd", "jusqu", "lorsqu", "puisqu", "quelqu",
                             "quoiqu", "presqu"))
# « j » s'élide aussi devant une consonne dans l'écrit familier : « j'suis »,
# « j'veux », « j'crois » — même élision que « j'ai », quelle que soit la lettre
# suivante. Les autres clitiques restent réservés à la voyelle.
_ELISIONS_DEVANT_CONSONNE = frozenset(("j",))
# « t' » devant consonne (registre oral) : « tu » → « t' » ne s'écrit que devant
# une poignée de verbes très fréquents — « t'fais », « t'sais », « t'vois »…
# Jamais devant un nom (« t'voiture ») ni devant n'importe quel verbe : liste
# fermée volontairement, contrairement à « j' » (orthographe standard).
_ELISIONS_T_CONSONNE = frozenset(("connais", "dis", "fais", "prends", "sais",
                                   "veux", "viens", "vois"))
# « y' » (argot) : contraction de « il y » devant les formes de « avoir » et
# « en » — « y'a », « y'avait », « y'aura », « y'en ». Jamais devant « aller »,
# « va », etc. (« y aller » reste deux mots), ni juste après « il » (« il y a »
# reste trois mots).
_ELISIONS_Y = frozenset(("a", "avait", "aura", "aurait", "en"))
# Composés à trait d'union dont le tiret a été perdu : « celui-ci », « celle-là ».
_DEMONSTRATIFS = frozenset(("celui", "celle", "ceux", "celles"))
_PARTICULES_CI_LA = frozenset(("ci", "là"))
_VOYELLE_INITIALE = frozenset("aeiouyhàâäéèêëîïôöùûüœæ")


def _commence_par_voyelle(texte: str) -> bool:
    """Vrai si ``texte`` commence par une voyelle (ou un h, traité comme muet)."""
    t = texte.strip().lstrip("«\"'’-( ")
    return bool(t) and t[0].lower() in _VOYELLE_INITIALE


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
        jointure = ""
        fragment = (texte.startswith(("'", "’", "-"))
                    or precedent.text.endswith(("'", "’", "-")))
        if not fragment:
            prec_nu = _mot_nu(precedent.text)
            cour_nu = _mot_nu(texte)
            if prec_nu in _ELISIONS_CLITIQUES and _commence_par_voyelle(cour_nu):
                fragment, jointure = True, "'"
            elif prec_nu in _ELISIONS_DEVANT_CONSONNE:
                fragment, jointure = True, "'"
            elif prec_nu == "t" and cour_nu in _ELISIONS_T_CONSONNE:
                fragment, jointure = True, "'"
            elif (prec_nu == "y" and cour_nu in _ELISIONS_Y
                  and (len(resultat) < 2 or _mot_nu(resultat[-2].text) != "il")):
                fragment, jointure = True, "'"
            elif prec_nu in _ELISIONS_TIGES and _commence_par_voyelle(cour_nu):
                fragment, jointure = True, "'"
            elif prec_nu in _DEMONSTRATIFS and cour_nu in _PARTICULES_CI_LA:
                fragment, jointure = True, "-"
        if gap <= max_gap_s and (fragment or ponctuation):
            fin = precedent.end
            if not ponctuation or courant.end - precedent.end <= max_duree_ponctuation_s:
                fin = max(fin, courant.end)
            resultat[-1] = Word(
                precedent.text + jointure + texte,
                precedent.start,
                fin,
                (precedent.probability + courant.probability) / 2.0,
                precedent.marqueur or courant.marqueur,
            )
            continue
        resultat.append(courant)
    return resultat


# ---------------------------------------------------------------------------
# Filtre anti-hallucination / anti-répétition (Slice 2)
# ---------------------------------------------------------------------------
# faster-whisper boucle parfois sur un phonème ou un mot en fin de phrase : on
# observe un token géant « aaaa…aaaa » (une seule lettre répétée) ou une suite
# du même mot répété en boucle. Ces artefacts polluent la bande rythmo. On les
# retire SANS toucher aux vrais mots : une répétition légitime (« non, non,
# non ») reste en place, et les marqueurs de respiration/bruitage sont ignorés
# par le filtre.

_REPETITION_CARACTERES_MIN = 8    # token = même lettre répétée ≥ 8× → hallucination
_REPETITION_CONSECUTIVES_MIN = 4  # ≥ 4 mots identiques consécutifs → boucle réduite


def _est_boucle_caracteres(texte: str, min_caracteres: int) -> bool:
    """Vrai si le token n'est qu'une seule lettre répétée (ex. « aaaa…aaaa »)."""
    lettres = normaliser_mot(texte)
    return len(lettres) >= min_caracteres and len(set(lettres)) == 1


def _fusionner_boucles_consecutives(mots: list[Word], min_consecutifs: int) -> list[Word]:
    """Réduit chaque run de ≥ ``min_consecutifs`` mots identiques au premier.

    Les bornes audio sont fusionnées (start du premier, end du dernier) ; les
    marqueurs interrompent un run (ils ne sont jamais absorbés).
    """
    resultat: list[Word] = []
    i = 0
    while i < len(mots):
        mot = mots[i]
        if mot.marqueur:
            resultat.append(mot)
            i += 1
            continue
        j = i
        norme = normaliser_mot(mot.text)
        while (j + 1 < len(mots)
               and not mots[j + 1].marqueur
               and normaliser_mot(mots[j + 1].text) == norme):
            j += 1
        if j - i + 1 >= min_consecutifs:
            resultat.append(Word(mot.text, mot.start, mots[j].end,
                                 mot.probability, marqueur=mot.marqueur))
        else:
            resultat.extend(mots[i:j + 1])
        i = j + 1
    return resultat


def filtrer_repetitions(mots: list[Word],
                        min_caracteres: int = _REPETITION_CARACTERES_MIN,
                        min_consecutifs: int = _REPETITION_CONSECUTIVES_MIN
                        ) -> list[Word]:
    """Supprime les hallucinations : tokens « aaaa… » et boucles de mots répétés.

    - un token formé d'une seule lettre répétée ≥ ``min_caracteres`` fois est
      retiré (le modèle boucle sur un phonème) ;
    - une suite de ≥ ``min_consecutifs`` occurrences consécutives du même mot
      est réduite à la première (start du premier, end du dernier) ;
      « non, non, non » (3) est conservé ;
    - les marqueurs (``marqueur=True`` : respiration, bruitage) ne sont jamais
      filtrés et interrompent une boucle.

    N'altère jamais les timings des mots conservés.
    """
    if not mots:
        return []
    propres = [m for m in mots
               if m.marqueur or not _est_boucle_caracteres(m.text, min_caracteres)]
    return _fusionner_boucles_consecutives(propres, min_consecutifs)


# ---------------------------------------------------------------------------
# Correction des homophones français à haute précision (Slice 3)
# ---------------------------------------------------------------------------
# faster-whisper confond fréquemment « à/a », « où/ou », « ça/çà ». On ne
# corrige que les cas SANS AMBIGUÏTÉ, à partir du seul mot précédent : toute
# autre paire (et/est, son/sont, on/ont, la/là…) reste intacte — le contexte
# ne permet pas de garantir « zéro fausse correction ».

_FORMES_ETRE = {
    "est", "es", "sont", "était", "étaient", "sera", "serait", "seront",
    "c'est", "t'es", "s'est",
}

_VERBES_PREPOSITION_A = {
    "va", "vais", "vas", "allait", "allaient",
    "vient", "viens", "venait", "venaient",
    "arrive", "arrivait", "arrivent",
    "retourne", "retournait",
    "parle", "parlait", "parlent",
    "pense", "pensait", "pensent",
    "répond", "répondait",
    "tient", "tenait",
    "sert", "servait",
    "ressemble", "ressemblait",
}

_PONCTUATION_FIN = ".,;:!?…"


def _mot_nu(texte: str) -> str:
    """Texte minuscule, apostrophes unifiées, ponctuation de fin retirée."""
    return texte.strip().lower().replace("’", "'").rstrip(_PONCTUATION_FIN)


def _avec_majuscule(cible: str, source: str) -> str:
    """Reporte la casse initiale de ``source`` sur ``cible``."""
    if source and source.strip()[0].isupper():
        return cible[0].upper() + cible[1:]
    return cible


def _separe_ponctuation_fin(texte: str) -> tuple[str, str]:
    """Sépare la ponctuation de fin : (mot nu, ponctuation) — ex. (« ou? », « ? »)."""
    t = texte.strip()
    i = len(t)
    while i > 0 and t[i - 1] in _PONCTUATION_FIN:
        i -= 1
    return t[:i], t[i:]


def corriger_homophones_fr(mots: list[Word]) -> list[Word]:
    """Corrige les confusions françaises SANS AMBIGUÏTÉ (zéro fausse correction).

    Règles volontairement minimales, décidées sur le mot précédent :
    - « ca » / « çà » → « ça » ;
    - « ou » → « où » après une forme du verbe être (« c'est où », « t'es où ») ;
    - « à » → « a » après « y » (« il y a ») ou « l' » (« il l'a ») ;
    - « a » → « à » après un verbe qui régit la préposition « à »
      (« il va à Paris », « il pense à toi »).

    Les marqueurs (respiration/bruitage) ne sont jamais modifiés ; la
    ponctuation de fin est conservée (« ou? » → « où? »).
    """
    resultat: list[Word] = []
    for i, mot in enumerate(mots):
        if mot.marqueur:
            resultat.append(mot)
            continue
        nu, ponct = _separe_ponctuation_fin(mot.text)
        cle = _mot_nu(nu)
        prec = _mot_nu(mots[i - 1].text) if i > 0 else ""
        corrige: str | None = None
        if cle in ("çà", "ca"):
            corrige = "ça"
        elif cle == "ou" and prec in _FORMES_ETRE:
            corrige = "où"
        elif cle == "à" and prec in ("y", "l'"):
            corrige = "a"
        elif cle == "a" and prec in _VERBES_PREPOSITION_A:
            corrige = "à"
        texte = (_avec_majuscule(corrige, nu) + ponct) if corrige else mot.text
        resultat.append(Word(texte, mot.start, mot.end, mot.probability,
                             marqueur=mot.marqueur))
    return resultat


# ---------------------------------------------------------------------------
# Homophones niveau 2 : accord grammatical (Slice 14)
# ---------------------------------------------------------------------------
# Slice 3 ne corrigeait que les paires décidables sur le mot précédent. Ici on
# traite ces/ses, c'est/s'est, tout/tous et leur/leurs quand l'ACCORD
# GRAMMATICAL lève l'ambiguïté. Règle générale : ne corriger que ce qui est
# invalide en français (accord rompu), jamais une forme valide — sauf UNE
# limitation assumée : « ces » après un pronom sujet de la phrase est traité
# comme possessif « ses » (« il aime ces amis » → « il aime ses amis »), au
# prix du cas valide « il voit ces gens » (indécidable en surface).

_PRONOMS_SUJET = frozenset(("je", "tu", "il", "elle", "on", "nous", "vous",
                            "ils", "elles"))
# Formes d'être devant lesquelles « ces »/« ses » devient « c' » (élision
# « c'était ») ou « ce » (« ce serait ») selon la voyelle initiale.
_FORMES_ETRE_NIV2 = frozenset(("est", "était", "sera", "serait", "soit"))
_DETERMINANTS_PLURIELS = frozenset(("les", "des", "mes", "tes", "ses", "nos",
                                    "vos", "leurs", "ces"))
_DETERMINANTS_SINGULIERS = frozenset(("le", "la", "l'", "mon", "ma", "ton",
                                      "ta", "son", "sa", "notre", "votre",
                                      "leur"))
_VERBES_PLURIELS_TOUT = frozenset(("sont", "ont", "étaient", "avaient",
                                   "seront"))
# Noms invariants en -s/-x/-z (« leur bois », « leur mois », « leur os ») :
# singulier ou pluriel, jamais transformés en « leurs ».
_NOMS_INVARIANTS_SXZ = frozenset((
    "bois", "bras", "corps", "cours", "fils", "fois", "gens", "héros",
    "mois", "nez", "os", "pays", "poids", "prix", "puits", "temps",
    "vis", "choix", "croix", "paix", "voix", "noix", "pois", "repas",
    "souris", "tapis", "avis", "abus", "accès", "atlas", "campus",
    "concours", "discours",
))


def _est_pronom_sujet(nu: str) -> bool:
    """Vrai si ``nu`` est un pronom sujet, y compris les formes élidées
    (« j'aime », « qu'il », « qu'elle », « qu'on »)."""
    return (nu in _PRONOMS_SUJET or nu.startswith("j'")
            or "'il" in nu or "'elle" in nu or "'on" in nu)


def _est_fin_de_phrase(texte: str) -> bool:
    """Vrai si ``texte`` (ou son dernier caractère) clôt une phrase."""
    t = texte.strip()
    return not t or t[-1] in _PONCTUATION_FIN


def _fenetre_a_pronom_sujet(mots: list[Word], i: int) -> bool:
    """Un pronom sujet apparaît avant ``mots[i]`` dans la même phrase.

    La fenêtre remonte jusqu'à une frontière de phrase (ponctuation ou
    marqueur) ; les marqueurs ne sont jamais lus ni franchis.
    """
    j = i - 1
    while j >= 0:
        prec = mots[j]
        if prec.marqueur or _est_fin_de_phrase(prec.text):
            return False
        if _est_pronom_sujet(_mot_nu(prec.text)):
            return True
        j -= 1
    return False


def _fini_par_s_x_z(nu: str) -> bool:
    """Vrai si ``nu`` se termine par une marque de pluriel écrite (-s/-x/-z)."""
    return bool(nu) and nu[-1] in "sxz"


def corriger_homophones_fr_niveau2(mots: list[Word]) -> list[Word]:
    """Corrige les homophones français par ACCORD GRAMMATICAL (Slice 14).

    Paires traitées, uniquement quand le contexte lève l'ambiguïté :
    - « s'est » en début de phrase → « c'est » ; « c'est » après il/elle/on
      → « s'est » (l'inversion « s'est-il » reste intacte) ;
    - « ces »/« ses » devant un/une → « c'est » ; devant une forme d'être
      élidable (« est », « était ») → « c'était »… (fusion des deux tokens),
      sinon « ce » (« ce serait ») ; devant « sont » → « ce » ; « ces » après
      un pronom sujet de la phrase → « ses » (limitation assumée) ;
    - « tout » devant un verbe ou un déterminant pluriel → « tous » ;
      « tous » devant un déterminant singulier → « tout » ;
    - « leurs » après un pronom sujet ou devant un nom sans marque pluriel
      → « leur » ; « leur » devant un nom pluriel clair (hors invariants)
      → « leurs ».

    Les marqueurs ne sont jamais modifiés ; ponctuation de fin et casse
    initiale conservées.
    """
    resultat: list[Word] = []
    i = 0
    n = len(mots)
    while i < n:
        mot = mots[i]
        if mot.marqueur:
            resultat.append(mot)
            i += 1
            continue
        nu, ponct = _separe_ponctuation_fin(mot.text)
        cle = _mot_nu(nu)
        prec = _mot_nu(mots[i - 1].text) if i > 0 else ""
        suiv = _mot_nu(mots[i + 1].text) if i + 1 < n else ""
        corrige: str | None = None
        fusion: Word | None = None
        if cle == "s'est":
            debut = i == 0 or _est_fin_de_phrase(mots[i - 1].text)
            if debut and suiv not in ("il", "elle", "on"):
                corrige = "c'est"
        elif cle == "c'est":
            if prec in ("il", "elle", "on"):
                corrige = "s'est"
        elif cle in ("ces", "ses"):
            if suiv in ("un", "une"):
                corrige = "c'est"
            elif suiv in _FORMES_ETRE_NIV2:
                if _commence_par_voyelle(suiv):
                    # « ses était » → « c'était » : un seul token, comme la fusion
                    fusion = Word(_avec_majuscule("c'" + suiv, nu),
                                  mot.start, mots[i + 1].end,
                                  (mot.probability + mots[i + 1].probability) / 2.0,
                                  marqueur=mot.marqueur)
                else:
                    corrige = "ce"  # « ces serait » → « ce serait »
            elif suiv == "sont":
                corrige = "ce"
            elif cle == "ces" and _fenetre_a_pronom_sujet(mots, i):
                corrige = "ses"
        elif cle == "tout":
            if suiv in _VERBES_PLURIELS_TOUT or suiv in _DETERMINANTS_PLURIELS:
                corrige = "tous"
        elif cle == "tous":
            if suiv in _DETERMINANTS_SINGULIERS:
                corrige = "tout"
        elif cle == "leurs":
            if prec in _PRONOMS_SUJET:
                corrige = "leur"
            elif suiv and not _fini_par_s_x_z(suiv):
                corrige = "leur"
        elif cle == "leur":
            if (prec not in _PRONOMS_SUJET and suiv
                    and _fini_par_s_x_z(suiv)
                    and suiv not in _NOMS_INVARIANTS_SXZ):
                corrige = "leurs"
        if fusion is not None:
            resultat.append(fusion)
            i += 2  # « ces/ses » + la forme d'être sont absorbés par « c'était »
            continue
        texte = (_avec_majuscule(corrige, nu) + ponct) if corrige else mot.text
        resultat.append(Word(texte, mot.start, mot.end, mot.probability,
                             marqueur=mot.marqueur))
        i += 1
    return resultat


# ---------------------------------------------------------------------------
# Nombres, dates et heures en toutes lettres (Slice 12)
# ---------------------------------------------------------------------------
# faster-whisper transcrit les nombres en chiffres (« 1999 », « 14h30 ») ou les
# déforme phonétiquement. Sur une bande rythmo, le texte doit dire ce que le
# comédien prononce : on épelle donc les tokens numériques entiers (entier pur
# sans zéro initial, ordinal, heure, date). Les codes à zéro initial (« 007 »),
# les alphanumériques (« 3D », « MP4 »), les dates/heures invalides et les mots
# déjà en lettres restent intacts (zéro fausse conversion).

_UNITES_FR = ("zéro", "un", "deux", "trois", "quatre", "cinq", "six", "sept",
              "huit", "neuf", "dix", "onze", "douze", "treize", "quatorze",
              "quinze", "seize", "dix-sept", "dix-huit", "dix-neuf")
_DIZAINES_FR = {2: "vingt", 3: "trente", 4: "quarante", 5: "cinquante",
                6: "soixante"}
_MOIS_FR = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet",
            "août", "septembre", "octobre", "novembre", "décembre")
_ORDINAL_SEGMENT = {
    "un": "unième", "deux": "deuxième", "trois": "troisième",
    "quatre": "quatrième", "cinq": "cinquième", "six": "sixième",
    "sept": "septième", "huit": "huitième", "neuf": "neuvième",
    "dix": "dixième", "onze": "onzième", "douze": "douzième",
    "treize": "treizième", "quatorze": "quatorzième", "quinze": "quinzième",
    "seize": "seizième", "vingt": "vingtième", "vingts": "vingtième",
    "trente": "trentième", "quarante": "quarantième",
    "cinquante": "cinquantième", "soixante": "soixantième",
}

_ENTIER_PUR = re.compile(r"^\d{1,9}$")
_ORDINAL = re.compile(r"^(\d{1,2})(?:er|re|e|ème|eme)$")
_HEURE = re.compile(r"^([01]?\d|2[0-3])h([0-5]\d)?$", re.IGNORECASE)
_DATE = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{1,4})$")


def _nombre_0_99(n: int) -> str:
    if n < 20:
        return _UNITES_FR[n]
    d, u = divmod(n, 10)
    if d < 7:
        if u == 0:
            return _DIZAINES_FR[d]
        if u == 1:
            return _DIZAINES_FR[d] + " et un"
        return _DIZAINES_FR[d] + "-" + _UNITES_FR[u]
    if d == 7:
        if u == 0:
            return "soixante-dix"
        if u == 1:
            return "soixante et onze"
        return "soixante-" + _UNITES_FR[10 + u]
    if d == 8:
        if u == 0:
            return "quatre-vingts"
        return "quatre-vingt-" + _UNITES_FR[u]
    if u == 0:
        return "quatre-vingt-dix"
    return "quatre-vingt-" + _UNITES_FR[10 + u]


def _nombre_0_999(n: int) -> str:
    if n < 100:
        return _nombre_0_99(n)
    c, r = divmod(n, 100)
    tete = "cent" if c == 1 else _UNITES_FR[c] + " cent"
    if r == 0:
        return tete + ("s" if c > 1 else "")
    return tete + " " + _nombre_0_99(r)


def nombre_en_lettres_fr(n: int) -> str:
    """Entier naturel (< 10⁹) écrit en toutes lettres (règles françaises)."""
    n = int(n)
    if n < 0 or n >= 10 ** 9:
        return str(n)
    if n == 0:
        return "zéro"
    millions, r = divmod(n, 10 ** 6)
    milliers, r = divmod(r, 1000)
    parties: list[str] = []
    if millions:
        parties.append("un million" if millions == 1
                       else _nombre_0_999(millions) + " millions")
    if milliers:
        parties.append("mille" if milliers == 1
                       else _nombre_0_999(milliers) + " mille")
    if r:
        parties.append(_nombre_0_999(r))
    return " ".join(parties)


def _ordinal_fr(n: int, feminin: bool = False) -> str:
    if n == 1:
        return "première" if feminin else "premier"
    base = nombre_en_lettres_fr(n)
    jetons = base.split(" ")
    segments = jetons[-1].split("-")
    dernier = segments[-1]
    segments[-1] = _ORDINAL_SEGMENT.get(dernier, dernier + "ième")
    jetons[-1] = "-".join(segments)
    return " ".join(jetons)


def _convertir_nombre(nu: str) -> str | None:
    """Forme écrite d'un token numérique entier, ou ``None`` si non convertible."""
    m = _ORDINAL.match(nu)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 99:
            return _ordinal_fr(n, feminin=nu.endswith("re"))
    m = _HEURE.match(nu)
    if m:
        h = int(m.group(1))
        mn = m.group(2)
        heure = "une heure" if h == 1 else nombre_en_lettres_fr(h) + " heures"
        if mn is None or int(mn) == 0:
            return heure
        return heure + " " + nombre_en_lettres_fr(int(mn))
    m = _DATE.match(nu)
    if m:
        j, mo, a = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= j <= 31 and 1 <= mo <= 12 and 1 <= a <= 9999:
            jour = "premier" if j == 1 else nombre_en_lettres_fr(j)
            return f"{jour} {_MOIS_FR[mo - 1]} {nombre_en_lettres_fr(a)}"
        return None
    if _ENTIER_PUR.match(nu):
        if len(nu) > 1 and nu[0] == "0":
            return None  # code (zéro initial), pas une quantité
        return nombre_en_lettres_fr(int(nu))
    return None


def normaliser_nombres_fr(texte: str) -> str:
    """Épelle un token numérique entier ; sinon le texte est rendu intact."""
    nu, ponct = _separe_ponctuation_fin(texte)
    converti = _convertir_nombre(nu)
    if converti is None:
        return texte
    return converti + ponct


def normaliser_nombres_mots(mots: list[Word]) -> list[Word]:
    """Applique :func:`normaliser_nombres_fr` aux mots parlés (jamais aux marqueurs)."""
    return [Word(normaliser_nombres_fr(m.text) if not m.marqueur else m.text,
                 m.start, m.end, m.probability, marqueur=m.marqueur)
            for m in mots]


# ---------------------------------------------------------------------------
# Correcteur phonétique des noms propres (Slice 13)
# ---------------------------------------------------------------------------
# faster-whisper massacre certains noms propres malgré le prompt FR : « chéri »
# reconnu « charim », « Francis » reconnu « françis » (casse perdue),
# « boucherie » tronquée « boucheri », « super-héros » reconnu sans tiret. On ne
# corrige un mot que s'il est À LA FOIS de confiance BASSE (le modèle hésite) ET
# phonétiquement proche d'un terme du vocabulaire du projet — jamais un mot sûr
# même proche (« chérie » légitime), jamais au-delà du seuil de distance. Sans
# vocabulaire, la fonction est l'identité (zéro fausse correction).

_SEUIL_CONFIANCE_CORRECTION = 0.6    # probability < seuil → candidat à corriger
_SEUIL_SIMILARITE_PHONETIQUE = 0.6   # similarité phonétique ≥ seuil → remplacement

# Repli grapheme → phonème-code grossier (ordre préservé), sans dépendance
# lourde. Les digraphes français stables sont repliés vers une voyelle, les
# voyelles sont conservées, les consonnes voisées/sourdes rapprochées (b/p,
# d/t, g/k, v/f, z/s, j/ch) et le « h » muet retiré — de quoi rapprocher des
# prononciations proches sans dictionnaire.
_DIGRAPHES_PHONETIQUES_FR = {
    "eau": "o", "au": "o", "ou": "u", "oi": "wa",
    "ai": "e", "ei": "e", "eu": "e",
    "ch": "ʃ", "ph": "f", "qu": "k", "gn": "ɲ",
}
_VOYELLES_PHONETIQUES_FR = {"a": "a", "e": "e", "i": "i", "o": "o", "u": "u", "y": "i"}
_VOYELLES_DOUCES_FR = frozenset("eiy")  # « c » doux (/s/) devant e/i/y
_CONSONNES_PHONETIQUES_FR = {
    "b": "p", "p": "p",
    "d": "t", "t": "t",
    "g": "k", "k": "k", "q": "k",
    "v": "f", "f": "f",
    "z": "s", "s": "s", "x": "s",
    "j": "ʃ",
    "m": "m", "n": "n",
    "l": "l", "r": "r",
    "w": "v",
}


def _cle_phonetique_fr(mot: str) -> str:
    """Clé phonétique française grossière : lettres → phonèmes-codes, ordre conservé.

    Les phonèmes identiques adjacents (géminées) sont repliés en un seul.
    """
    nu = normaliser_mot(mot)  # minuscules, sans accents ni ponctuation
    codes: list[str] = []
    i = 0
    while i < len(nu):
        trois = nu[i:i + 3]
        deux = nu[i:i + 2]
        if trois == "eau":
            codes.append("o")
            i += 3
            continue
        if deux in _DIGRAPHES_PHONETIQUES_FR:
            codes.append(_DIGRAPHES_PHONETIQUES_FR[deux])
            i += 2
            continue
        c = nu[i]
        if c in _VOYELLES_PHONETIQUES_FR:
            codes.append(_VOYELLES_PHONETIQUES_FR[c])
        elif c == "c":
            suivant = nu[i + 1] if i + 1 < len(nu) else ""
            codes.append("s" if suivant in _VOYELLES_DOUCES_FR else "k")
        elif c != "h":
            codes.append(_CONSONNES_PHONETIQUES_FR.get(c, c))
        i += 1
    replie: list[str] = []
    for code in codes:
        if not replie or replie[-1] != code:
            replie.append(code)
    return "".join(replie)


def _distance_edit(a: str, b: str) -> int:
    """Distance de Levenshtein caractère à caractère (mémoire O(len(b)))."""
    if not a:
        return len(b)
    if not b:
        return len(a)
    prec = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        courant = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cout = 0 if ca == cb else 1
            courant[j] = min(prec[j] + 1, courant[j - 1] + 1, prec[j - 1] + cout)
        prec = courant
    return prec[-1]


def similarite_phonetique_fr(mot_a: str, mot_b: str) -> float:
    """Similarité phonétique (0–1) entre deux mots ; 1 = même prononciation.

    Chaque mot est réduit à sa clé phonétique (:func:`_cle_phonetique_fr`) puis
    comparé par distance de Levenshtein normalisée par la clé la plus longue.
    Deux clés vides → 1.0 ; une seule clé vide → 0.0.
    """
    ka, kb = _cle_phonetique_fr(mot_a), _cle_phonetique_fr(mot_b)
    if not ka and not kb:
        return 1.0
    if not ka or not kb:
        return 0.0
    return 1.0 - _distance_edit(ka, kb) / max(len(ka), len(kb))


def corriger_noms_propres(mots: list[Word], vocabulaire: list[str] | None = None,
                          seuil_confiance: float = _SEUIL_CONFIANCE_CORRECTION,
                          seuil_similarite: float = _SEUIL_SIMILARITE_PHONETIQUE
                          ) -> list[Word]:
    """Remplace un mot à basse confiance par le terme du lexique le plus proche.

    Pour chaque mot parlé (jamais les marqueurs) de ``probability <
    seuil_confiance``, on retient le terme du ``vocabulaire`` le plus proche
    phonétiquement (:func:`similarite_phonetique_fr`). Si la meilleure
    similarité atteint ``seuil_similarite``, le texte est remplacé (casse
    initiale et ponctuation de fin conservées) ; sinon le mot reste intact.
    Sans vocabulaire, la fonction est l'identité.
    """
    termes = [str(t).strip() for t in (vocabulaire or []) if str(t).strip()]
    if not termes:
        return list(mots)
    resultat: list[Word] = []
    for mot in mots:
        if mot.marqueur or mot.probability >= seuil_confiance:
            resultat.append(mot)
            continue
        nu, ponct = _separe_ponctuation_fin(mot.text)
        if not nu.strip():
            resultat.append(mot)
            continue
        meilleur, meilleure_sim = None, -1.0
        for terme in termes:
            sim = similarite_phonetique_fr(nu, terme)
            if sim > meilleure_sim:
                meilleur, meilleure_sim = terme, sim
        if meilleur is not None and meilleure_sim >= seuil_similarite:
            texte = _avec_majuscule(meilleur, nu) + ponct
            resultat.append(Word(texte, mot.start, mot.end, mot.probability,
                                 marqueur=mot.marqueur))
        else:
            resultat.append(mot)
    return resultat


# ---------------------------------------------------------------------------
# Confiance des mots : drapeau « incertain » exporté (Slice 16)
# ---------------------------------------------------------------------------
# Le monteur doit voir quels mots sont incertains (probabilité basse) sans
# attendre la lecture ; le drapeau est exporté dans ``repliques.json`` et
# prépare le re-décodage ciblé (Slice 19). Jamais un marqueur ni un symbole
# entre parenthèses (non prononcés), jamais un mot sans probabilité captée —
# la valeur 0.0 est la sentinelle « sans probabilité » (défaut du ``Word`` et
# repli cloud ``or 0.0``).

def marquer_mots_incertains(mots: list[Word],
                            seuil: float = _SEUIL_CONFIANCE_CORRECTION
                            ) -> list[Word]:
    """Marque ``incertain`` les mots parlés dont la probabilité est sous le seuil.

    Un mot est marqué s'il est prononcé (ni ``marqueur``, ni symbole entre
    parenthèses), qu'une probabilité a été captée (``probability > 0``) et
    qu'elle est strictement inférieure à ``seuil`` (défaut 0,6). Le texte et
    les timestamps ne changent jamais ; la fonction est pure (la liste
    d'entrée n'est pas mutée).
    """
    resultat: list[Word] = []
    for mot in mots:
        if (mot.marqueur or est_symbole(mot.text)
                or not 0.0 < mot.probability < seuil):
            resultat.append(mot)
            continue
        resultat.append(Word(mot.text, mot.start, mot.end, mot.probability,
                             marqueur=mot.marqueur, incertain=True))
    return resultat


# ---------------------------------------------------------------------------
# Prompt initial français (Slice 5)
# ---------------------------------------------------------------------------
# faster-whisper accepte un ``initial_prompt`` : des amorces de texte qui
# orientent le décodage. Pour le français, on y injecte des mots fréquents et
# le vocabulaire du projet (noms propres, personnages) — c'est le levier le
# plus efficace contre les noms propres massacrés (« Francis » → « processie »).

_PROMPT_FR_BASE = (
    "Bonjour, merci, s'il vous plaît, oui, non, bonsoir, alors, vraiment, "
    "pourquoi, comment, quand, où, voilà, d'accord, maintenant, aujourd'hui, "
    "c'est, il y a, je, tu, il, elle, on, nous, vous, ils, elles, le, la, les, "
    "un, une, des, et, ou, mais, donc, parce que, ce, cette, ces, mon, ma, mes, "
    "ton, ta, tes, son, sa, ses, notre, votre, leur, avec, sans, pour, dans, "
    "sur, sous, à, de, ne, pas, plus, très, bien, mal, comme, ça, quoi, qui, "
    "que, est, sont, fait, faire, dit, dire, va, aller, viens, venir, "
    "y'a, ouais, genre, mec, putain."
)
_PROMPT_FR_MAX_CHARS = 600


def construire_prompt_fr(vocabulaire: list[str] | None = None) -> str:
    """Prompt d'initialisation français (``initial_prompt`` de faster-whisper).

    Amorces françaises fréquentes + vocabulaire du projet (noms propres,
    personnages, termes rares) : biaise le décodage vers les bons mots.
    Vocabulaire dédupliqué (insensible à la casse/accents) et borné en taille.
    """
    termes: list[str] = []
    vus: set[str] = set()
    for terme in (vocabulaire or []):
        mot = str(terme).strip()
        cle = normaliser_mot(mot)
        if cle and cle not in vus:
            vus.add(cle)
            termes.append(mot)
    suffixe = (" " + " ".join(termes)) if termes else ""
    # Le vocabulaire (noms propres) est prioritaire : on tronque l'amorce de
    # base, jamais les mots du projet.
    place = max(0, _PROMPT_FR_MAX_CHARS - len(suffixe))
    base = (_PROMPT_FR_BASE if len(_PROMPT_FR_BASE) <= place
            else _PROMPT_FR_BASE[:place])
    return (base + suffixe)[:_PROMPT_FR_MAX_CHARS]


_CONTEXTE_MOTS_MAX = 20  # mots de contexte transmis au chunk suivant


def contexte_glissant(mots: list[Word], max_mots: int = _CONTEXTE_MOTS_MAX) -> str:
    """Derniers ``max_mots`` mots d'un chunk — contexte initial du chunk suivant.

    Le texte brut des mots récents amorce la continuité du décodage et réduit
    les coupures aux frontières des fenêtres glissantes.
    """
    if not mots:
        return ""
    return " ".join(m.text.strip() for m in mots[-max_mots:])


def composer_prompt_contexte(prompt_fr: str | None, contexte: str,
                             max_chars: int = _PROMPT_FR_MAX_CHARS) -> str | None:
    """Combine le prompt français et le contexte glissant, borné en taille.

    Le contexte (mots les plus récents) est prioritaire : on tronque le prompt
    français pour lui laisser place, jamais le contexte. ``None`` si vide.
    """
    contexte = (contexte or "").strip()
    if not contexte:
        return prompt_fr or None
    if len(contexte) > max_chars:
        contexte = contexte[-max_chars:]  # garde les mots les plus récents
    if not prompt_fr:
        return contexte
    place = max(0, max_chars - len(contexte) - 1)
    base = prompt_fr if len(prompt_fr) <= place else prompt_fr[:place]
    return (base + " " + contexte)[:max_chars]


def _transcrire_fichier(chemin_wav: str | Path, language: str | None = None,
                        model_name: str = "base", prompt: str | None = None):
    """Transcription brute d'un fichier WAV 16 kHz → (mots bruts, langue).

    Point d'injection unique (tests) pour toute transcription. Le chemin fichier
    (et non un tableau numpy) est passé à faster-whisper : décodage interne fiable.
    ``prompt`` (``initial_prompt``) biaise le décodage — transmis seulement s'il
    est fourni (typiquement un prompt français enrichi du vocabulaire du projet).
    """
    modele = get_asr_model(model_name)
    options: dict = dict(
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
        # Les chunks sont ≤ 30 s (fenêtre interne unique) : la continuité entre
        # fenêtres est portée par ``initial_prompt`` (contexte glissant), pas
        # par cette option (réservée aux fenêtres internes d'un audio > 30 s).
        condition_on_previous_text=False,
    )
    if prompt:
        options["initial_prompt"] = prompt
    segments_iter, info = modele.transcribe(str(chemin_wav), **options)
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
                     model_name: str = "base", affiner: bool = True,
                     vocabulaire: list[str] | None = None) -> tuple[list[Word], str]:
    """Transcrit un WAV 16 kHz mono : retourne (mots horodatés, code langue).

    Timestamps affinés par alignement forcé WhisperX quand c'est possible ;
    sinon timestamps natifs faster-whisper (word_timestamps=True).
    Dans tous les cas, les fins de mots sont prolongées sur l'énergie réelle
    (T50) : une syllabe tenue par l'acteur ne doit pas être tronquée par le
    modèle — le mot s'étend jusqu'au dernier son de parole.
    """
    chemin_wav = Path(chemin_wav)
    duree = duree_wav(chemin_wav)
    prompt = construire_prompt_fr(vocabulaire) if language == "fr" else None
    mots, langue = _transcrire_fichier(chemin_wav, language=language,
                                       model_name=model_name, prompt=prompt)
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
    mots_finaux = filtrer_repetitions(mots_finaux)
    mots_finaux = normaliser_nombres_mots(mots_finaux)
    mots_finaux = corriger_homophones_fr(mots_finaux)
    mots_finaux = corriger_homophones_fr_niveau2(mots_finaux)
    mots_finaux = corriger_noms_propres(mots_finaux, vocabulaire)
    rms, zc = enveloppe_parole(chemin_wav)
    mots_finaux = prolonger_fins_sur_audio(mots_finaux, rms, zc_hz=zc)
    mots_finaux = recaler_onsets_sur_audio(mots_finaux, rms, zc)
    mots_finaux = detecter_souffles(mots_finaux, rms, zc)
    return validate_words(mots_finaux, duree), langue


def _centres_silence(rms: np.ndarray, seuil: float, fenetre_s: float,
                     min_silence_s: float) -> list[float]:
    """Centres (s) des runs de trames silencieuses (rms < ``seuil``) ≥ ``min_silence_s``.

    Un run = trames consécutives sous le seuil ; son centre est le milieu du
    run. Les trous trop courts (clic, micro-pause) sont ignorés.
    """
    centres: list[float] = []
    debut_run = None
    for k, v in enumerate(rms):
        if v < seuil:
            if debut_run is None:
                debut_run = k
        elif debut_run is not None:
            if (k - debut_run) * fenetre_s >= min_silence_s:
                centres.append((debut_run + k) / 2 * fenetre_s)
            debut_run = None
    if debut_run is not None and (len(rms) - debut_run) * fenetre_s >= min_silence_s:
        centres.append((debut_run + len(rms)) / 2 * fenetre_s)
    return centres


def frontieres_silences(chemin_wav: str | Path, duree_chunk_s: float = 25.0,
                        recouvrement_s: float = 1.0, tolerance_s: float = 2.5,
                        min_silence_s: float = 0.25,
                        seuil_relatif: float = _SEUIL_RELATIF,
                        plancher: float = _PLANCHER_ABS,
                        fenetre_s: float = _FENETRE_RMS_S,
                        rms: np.ndarray | None = None) -> list[float]:
    """Grille fixe des fenêtres, avec nudge conservateur vers les silences.

    Retourne la liste des débuts de fenêtres (le premier = 0, même grille que
    ``transcribe_chunked``). Chaque frontière INTERMÉDIAIRE qui tombe dans de la
    parole (RMS ≥ ``max(seuil_relatif·pic, plancher)``) est déplacée vers le
    centre du silence le plus proche, à condition qu'il soit à ≤ ``tolerance_s``.

    Une frontière déjà dans un silence est CONSERVÉE : on ne déplace jamais une
    frontière d'un silence à un autre — c'est ce qui distingue ce nudge du
    snapping naïf (déplacer toutes les frontières), qui place des coupures au
    milieu de mots (fausses silences sur interjections) et dégrade le WER.

    ``rms`` : enveloppe RMS (``enveloppe_parole``) si déjà calculée, sinon
    calculée ici. Enveloppe vide ou grille à moins de 3 fenêtres → grille fixe
    (repli sûr, jamais d'exception).
    """
    if rms is None:
        rms = enveloppe_parole(chemin_wav)[0]
    duree = duree_wav(chemin_wav)
    pas = duree_chunk_s - recouvrement_s
    debuts: list[float] = []
    t = 0.0
    while t < duree:
        debuts.append(t)
        t += pas
    if len(rms) == 0 or len(debuts) < 3:
        return debuts
    pic = float(rms.max())
    seuil = max(seuil_relatif * pic, plancher)
    centres = _centres_silence(rms, seuil, fenetre_s, min_silence_s)
    if not centres:
        return debuts
    for i in range(1, len(debuts) - 1):
        k = int(debuts[i] / fenetre_s)
        if k >= len(rms) or rms[k] < seuil:
            continue  # frontière déjà dans le silence : on la garde
        meilleur, meilleure_dist = None, tolerance_s
        for c in centres:
            d = abs(c - debuts[i])
            if d < meilleure_dist:
                meilleur, meilleure_dist = c, d
        if meilleur is not None:
            # garde : la frontière reste entre ses voisines (points médians),
            # monotonie garantie quelle que soit la tolérance
            lo = (debuts[i - 1] + debuts[i]) / 2
            hi = (debuts[i] + debuts[i + 1]) / 2
            debuts[i] = min(max(meilleur, lo), hi)
    return debuts


def transcribe_chunked(chemin_wav: str | Path, duree_chunk_s: float = 25.0,
                       recouvrement_s: float = 1.0, language: str | None = None,
                       model_name: str = "base",
                       vocabulaire: list[str] | None = None) -> tuple[list[Word], str]:
    """Transcrit un long WAV par fenêtres glissantes puis fusionne sans doublon ni trou.

    Chaque fenêtre « possède » les mots débutant dans ``[début_fenêtre, début_fenêtre+pas)``
    (dernière fenêtre : jusqu'à la fin) — les mots du recouvrement sont dédupliqués.
    """
    import tempfile

    prompt_fr = construire_prompt_fr(vocabulaire) if language == "fr" else None
    if duree_wav(chemin_wav) <= duree_chunk_s:
        mots, langue = _transcrire_fichier(chemin_wav, language=language,
                                           model_name=model_name, prompt=prompt_fr)
        mots = fusionner_fragments_fr(mots)
        mots = filtrer_repetitions(mots)
        mots = normaliser_nombres_mots(mots)
        mots = corriger_homophones_fr(mots)
        mots = corriger_homophones_fr_niveau2(mots)
        mots = corriger_noms_propres(mots, vocabulaire)
        rms, zc = enveloppe_parole(chemin_wav)
        mots = prolonger_fins_sur_audio(mots, rms, zc_hz=zc)
        mots = recaler_onsets_sur_audio(mots, rms, zc)
        mots = detecter_souffles(mots, rms, zc)
        return validate_words(mots, duree_wav(chemin_wav)), langue

    mots_fusionnes: list[Word] = []
    langue = language or ""
    # Enveloppe calculée une seule fois : sert au nudge des frontières ET au
    # post-traitement (prolonger/onsets/souffles) en fin de fonction.
    rms, zc = enveloppe_parole(chemin_wav)
    with tempfile.TemporaryDirectory(prefix="rythmo_chunks_") as tampon_dir, \
            wave.open(str(chemin_wav)) as src:
        rate = src.getframerate()
        duree = src.getnframes() / rate
        debuts = frontieres_silences(chemin_wav, duree_chunk_s=duree_chunk_s,
                                     recouvrement_s=recouvrement_s, rms=rms)

        tampon = Path(tampon_dir)
        contexte = ""
        for i, debut in enumerate(debuts):
            fin = min(debut + duree_chunk_s, duree)
            # lecture fenêtrée directement depuis le WAV : mémoire bornée (1 fenêtre)
            src.setpos(int(debut * rate))
            brutes = src.readframes(int((fin - debut) * rate))
            chemin_chunk = tampon / f"chunk_{i:05d}.wav"
            with wave.open(str(chemin_chunk), "wb") as w:
                w.setparams(src.getparams())
                w.writeframes(brutes)
            # Contexte glissant : les derniers mots du chunk précédent amorcent
            # la transcription du suivant (continuité aux frontières).
            prompt = composer_prompt_contexte(prompt_fr, contexte)
            mots, langue_chunk = _transcrire_fichier(chemin_chunk, language=language or None,
                                                     model_name=model_name, prompt=prompt)
            langue = langue or langue_chunk
            contexte = contexte_glissant(mots)
            limite_haut = debuts[i + 1] if i + 1 < len(debuts) else duree + 1.0
            for m in mots:
                debut_global, fin_global = m.start + debut, m.end + debut
                if debut <= debut_global < limite_haut:  # zone « possédée » par ce chunk
                    mots_fusionnes.append(Word(m.text, debut_global,
                                               min(fin_global, duree), m.probability,
                                               marqueur=m.marqueur))
    mots_fusionnes = fusionner_fragments_fr(mots_fusionnes)
    mots_fusionnes = filtrer_repetitions(mots_fusionnes)
    mots_fusionnes = normaliser_nombres_mots(mots_fusionnes)
    mots_fusionnes = corriger_homophones_fr(mots_fusionnes)
    mots_fusionnes = corriger_homophones_fr_niveau2(mots_fusionnes)
    mots_fusionnes = corriger_noms_propres(mots_fusionnes, vocabulaire)
    mots_fusionnes = prolonger_fins_sur_audio(mots_fusionnes, rms, zc_hz=zc)
    mots_fusionnes = recaler_onsets_sur_audio(mots_fusionnes, rms, zc)
    mots_fusionnes = detecter_souffles(mots_fusionnes, rms, zc)
    return validate_words(mots_fusionnes, duree), langue


def normaliser_mot(texte: str) -> str:
    """Minuscules, sans accents ni ponctuation — pour comparaisons tolérantes."""
    texte = unicodedata.normalize("NFD", texte.lower().strip())
    return "".join(c for c in texte if unicodedata.category(c) == "Ll")


def wer_fr(reference: list[str], hypothese: list[str]) -> float:
    """Taux d'erreur de mots (WER) français : distance d'édition / nb de mots.

    Casse, accents et ponctuation ignorés via :func:`normaliser_mot` ; les
    nombres sont d'abord épelés via :func:`normaliser_nombres_fr` (ils comptent
    donc dans la métrique), les tokens sans lettre restants (ponctuation seule)
    sont exclus. ``0.0`` = transcriptions identiques ; ``1.0`` = référence vide
    avec hypothèse non vide. Métrique de référence des améliorations de la
    reconnaissance FR.
    """
    ref = [m for m in (normaliser_mot(normaliser_nombres_fr(t)) for t in reference) if m]
    hyp = [m for m in (normaliser_mot(normaliser_nombres_fr(t)) for t in hypothese) if m]
    if not ref:
        return 0.0 if not hyp else 1.0
    n, m = len(ref), len(hyp)
    # Levenshtein sur deux lignes (mémoire O(m)) — jamais O(n·m).
    prec = list(range(m + 1))
    for i in range(1, n + 1):
        courant = [i] + [0] * m
        for j in range(1, m + 1):
            cout = 0 if ref[i - 1] == hyp[j - 1] else 1
            courant[j] = min(prec[j] + 1,        # suppression
                             courant[j - 1] + 1,  # insertion
                             prec[j - 1] + cout)  # substitution/égalité
        prec = courant
    return prec[m] / n


# ---------------------------------------------------------------------------
# Transcription d'une fenêtre audio unique (resynchronisation par réplique)
# ---------------------------------------------------------------------------
# La resynchronisation globale re-transcrit tout ``audio_16k.wav`` puis réaligne
# chaque réplique. Pour corriger UNE réplique, ce coût est disproportionné : on
# ne transcrit ici que la fenêtre ``[debut - marge, fin + marge]`` de la
# réplique ciblée (marges = contexte nécessaire à l'ASR pour des frontières de
# mots propres), puis on décale les timestamps relatifs en temps absolu.

MARGE_FENETRE_AVANT_S = 0.5   # contexte transcrit avant le début de la réplique
MARGE_FENETRE_APRES_S = 0.5   # contexte transcrit après la fin de la réplique


def fenetre_transcription(duree: float, debut: float, fin: float,
                          marge_avant: float = MARGE_FENETRE_AVANT_S,
                          marge_apres: float = MARGE_FENETRE_APRES_S) -> tuple[float, float]:
    """Fenêtre audio à transcrire : ``[debut - marge_avant, fin + marge_apres]``.

    Bornes clampées à ``[0, duree]``. Lève ``RythmoError("E006")`` si la
    réplique est inversée (``fin <= debut``) ou entièrement hors du fichier.
    """
    debut, fin = float(debut), float(fin)
    if fin <= debut:
        raise RythmoError("E006",
                          f"Fenêtre audio invalide : début {debut:.3f} s ≥ fin "
                          f"{fin:.3f} s.")
    d0 = max(0.0, debut - float(marge_avant))
    d1 = min(float(duree), fin + float(marge_apres))
    if d1 <= d0:
        raise RythmoError("E006",
                          f"Fenêtre audio hors fichier : [{d0:.3f}, {d1:.3f}] s "
                          f"(durée {duree:.3f} s).")
    return d0, d1


def decaler_mots(mots: list[Word], decalage: float) -> list[Word]:
    """Décale les timestamps (temps relatif → absolu) sans toucher au texte."""
    decalage = float(decalage)
    return [Word(m.text, m.start + decalage, m.end + decalage, m.probability,
                 marqueur=m.marqueur) for m in mots]


def transcrire_fenetre(chemin_wav: str | Path, debut: float, fin: float,
                       language: str | None = None, model_name: str = "base",
                       affiner: bool = True,
                       marge_avant: float = MARGE_FENETRE_AVANT_S,
                       marge_apres: float = MARGE_FENETRE_APRES_S,
                       vocabulaire: list[str] | None = None) -> list[Word]:
    """Transcrit uniquement la fenêtre ``[debut, fin]`` (+ marges) d'un WAV 16 kHz.

    La tranche est extraite vers un fichier temporaire puis transcrite via
    ``transcribe_words`` (même post-traitement que le chemin complet) ; les
    timestamps, relatifs à la tranche, sont ensuite décalés en temps absolu.
    Ne passe jamais par le cache disque du fichier entier : transcription
    ponctuelle destinée à la resynchronisation d'une seule réplique.
    """
    import tempfile

    from .audio_segments import decouper_segment

    d0, d1 = fenetre_transcription(duree_wav(chemin_wav), debut, fin,
                                   marge_avant, marge_apres)
    morceau, _ = decouper_segment(chemin_wav, d0, d1)
    with tempfile.TemporaryDirectory(prefix="rythmo_fenetre_") as tampon_dir:
        chemin_fenetre = Path(tampon_dir) / "fenetre.wav"
        chemin_fenetre.write_bytes(morceau)
        mots, _ = transcribe_words(chemin_fenetre, language=language,
                                   model_name=model_name, affiner=affiner,
                                   vocabulaire=vocabulaire)
    return decaler_mots(mots, d0)
