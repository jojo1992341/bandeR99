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
# Prolongation acoustique des syllabes tenues (T50)
# ---------------------------------------------------------------------------
# Constat réel (Redoublage.mp4, ~9 s) : la syllabe « cis » de « Francis » est
# tenue par l'acteur pendant plus d'une seconde (voyelle /i/ maintenue, bouche
# ouverte en continu). L'ASR (faster-whisper) puis l'alignement forcé WhisperX
# bornent le mot au phonème consonantique et coupent la tenue — le mot se
# retrouve tronqué de ~0,5 s de parole réelle. La prolongation ci-dessous
# récupère la vraie fin de parole depuis l'enveloppe d'énergie du signal,
# indépendamment du modèle : le mot s'étend jusqu'au dernier son tenu, en
# s'arrêtant au premier vrai silence (jamais d'absorption d'un mot suivant
# que l'ASR aurait raté) et jamais au-delà du début du mot suivant.

_FENETRE_RMS_S = 0.020          # fenêtre d'énergie (s)
_SEUIL_RELATIF = 0.15           # fraction du pic d'énergie de la fenêtre de recherche
_PLANCHER_ABS = 0.006           # RMS normalisé : au-dessus du bruit de fond typique
_PAUSE_SILENCE_S = 0.12         # un trou ≥ cette durée stoppe la prolongation
_MAX_EXTENSION_S = 1.5          # plafond de prolongation (s)
_MARGE_SUIVANT_S = 0.03         # jamais au-delà du début du mot suivant − marge
_MIN_EXTENSION_S = 0.06         # en-deçà : on garde la borne de l'aligneur (stabilité)
_PAUSE_AVANT_S = 0.25           # pas de prolongation si le mot suivant enchaîne


def enveloppe_rms(chemin_wav: str | Path,
                  fenetre_s: float = _FENETRE_RMS_S) -> np.ndarray:
    """Enveloppe d'énergie RMS (par fenêtre glissante) d'un WAV mono PCM.

    Taux d'échantillonnage quelconque : les fenêtres sont définies en secondes.
    Lecture streamée par blocs : la mémoire reste bornée (seule l'enveloppe,
    ~50 valeurs/s, est conservée) même pour les très longs fichiers.
    """
    import numpy as np

    valeurs: list[np.ndarray] = []
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
                valeurs.append(morceau.std(axis=1))
    if not valeurs:
        return np.zeros(0, dtype=np.float64)
    return np.concatenate(valeurs).astype(np.float64)


def prolonger_fins_sur_audio(mots: list[Word], enveloppe: np.ndarray,
                             duree_fenetre_s: float = _FENETRE_RMS_S,
                             seuil_relatif: float = _SEUIL_RELATIF,
                             plancher: float = _PLANCHER_ABS,
                             max_extension_s: float = _MAX_EXTENSION_S,
                             marge_s: float = _MARGE_SUIVANT_S,
                             pause_silence_s: float = _PAUSE_SILENCE_S,
                             min_extension_s: float = _MIN_EXTENSION_S,
                             pause_avant_s: float = _PAUSE_AVANT_S) -> list[Word]:
    """Étend la fin de chaque mot jusqu'au dernier son de parole tenu.

    Pour un mot, on cherche dans ``[fin, min(fin + max_extension, début du mot
    suivant − marge)]`` le dernier instant où l'énergie reste au-dessus du
    seuil, en s'arrêtant au premier trou ≥ ``pause_silence`` : une tenue
    (voyelle maintenue, sifflement, cri…) prolonge le mot ; un vrai silence ou
    une nouvelle attaque de parole après un trou ne sont jamais absorbés.

    ``enveloppe`` : valeurs RMS par fenêtre (``enveloppe_rms``). Les bornes
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
        pic = float(fen.max())
        seuil = max(seuil_relatif * pic, plancher)
        dernier = -1
        trou = 0
        for k, v in enumerate(fen):
            if v >= seuil:
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
                              m.probability))
    return prolonges


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
            raccourci = Word(precedent.text, precedent.start, min(precedent.end, debut), precedent.probability)
            if raccourci.end - raccourci.start >= recouvrement_max:
                propres[-1] = raccourci
            debut = max(debut, propres[-1].end)
            fin = max(fin, debut)
            if fin - debut <= 0:
                continue
        propres.append(Word(m.text.strip(), debut, fin, m.probability))
    return propres


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
        vad_filter=True,
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
                if courant is None or m.start - courant["end"] > 0.8:
                    courant = {"start": m.start, "end": m.end, "text": ""}
                    segs.append(courant)
                courant["text"] = (courant["text"] + " " + m.text).strip()
                courant["end"] = m.end
            mots_alignes = _affiner_avec_whisperx(segs, chemin_wav, langue, choose_device())
            if mots_alignes:
                mots_finaux = mots_alignes
        except Exception:
            pass  # repli timestamps natifs
    mots_finaux = prolonger_fins_sur_audio(mots_finaux, enveloppe_rms(chemin_wav))
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
        mots = prolonger_fins_sur_audio(mots, enveloppe_rms(chemin_wav))
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
                                               min(fin_global, duree), m.probability))
    mots_fusionnes = prolonger_fins_sur_audio(mots_fusionnes, enveloppe_rms(chemin_wav))
    return validate_words(mots_fusionnes, duree), langue


def normaliser_mot(texte: str) -> str:
    """Minuscules, sans accents ni ponctuation — pour comparaisons tolérantes."""
    texte = unicodedata.normalize("NFD", texte.lower().strip())
    return "".join(c for c in texte if unicodedata.category(c) == "Ll")
