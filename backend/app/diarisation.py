"""Diarisation automatique : rattache chaque mot à une « voix » (personnage).

Deux méthodes, combinables via ``diariser_avec_repli`` (ce que le pipeline
utilise) :

1. **Embeddings vocaux (Resemblyzer, T109–T111, session 28)** — chaque mot est
   plongé dans l'espace des d-vectors (encodeur pré-entraîné) ; on projette sur
   la première composante principale et on coupe au plus grand écart, comme la
   méthode par hauteur. Deux locuteurs de MÊME tessiture (hauteurs identiques)
   mais de timbres différents se séparent. Si Resemblyzer/torch est absent, la
   méthode retombe sur la hauteur sans jamais lever d'exception.
2. **Hauteur fondamentale + énergie (T56–T59, session 12)** — pour chaque mot,
   lecture streamée de son segment dans le WAV (la mémoire reste bornée même
   pour les vidéos longues) et extraction de deux caractéristiques :

   - RMS normalisé : un mot quasi muet (bruit de fond, souffle) n'est jamais une
     voix à part entière — il est rattaché à la voix la plus proche ;
   - hauteur fondamentale (autocorrélation) : deux locuteurs aux voix distinctes
     se séparent nettement ; une seule voix (ou des hauteurs quasi identiques)
     → un seul personnage pour tout le monde.

La partition est déterministe (aucune initialisation aléatoire) : même audio →
mêmes étiquettes, ce qui garantit la reproductibilité d'une analyse à l'autre
et la stabilité des identifiants de personnage sur reprise.

Le rattachement des répliques à des noms (voix 1, voix 2…) est conservé par
``attribuer_personnages`` sur toute la durée de vie du job, y compris après
réédition (voir ``resynchroniser_mots`` dans cues_edit.py).
"""
from __future__ import annotations

import wave
from pathlib import Path

_SEUIL_VOISE = 0.01          # RMS normalisé : en-deçà, le segment est « quasi muet »
_SEUIL_GAP = 0.30            # fraction de l'écart min-max (hauteur ou PC1) à
                             # partir de laquelle on sépare en deux voix
_TOL_HAUTEUR_IDENTIQUE = 1.0  # Hz : en-deçà, tout le monde parle de la même voix
_BORNE_BASSE_HZ = 50.0       # hauteur minimale plausible (voix graves)
_BORNE_HAUTE_HZ = 400.0      # hauteur maximale plausible
_SEUIL_MARGE = 0.15          # similarité cosinus : marge minimale (pire classe
                             # intra − inter) pour valider deux voix par embeddings
_FRACTION_MIN = 0.15         # le plus petit groupe doit couvrir cette fraction des
                             # répliques valides, sinon split jugé artefact (outliers)


def _estimer_hauteur(segment, rate: int) -> float:
    """Hauteur fondamentale (Hz) par autocorrélation, premier pic fort.

    Retourne 0.0 si le segment est trop court ou si la corrélation est trop
    faible (bruit, chuchotement…) — le mot est alors considéré non voisé.
    """
    import numpy as np

    x = segment - segment.mean()
    n = len(x)
    lag_min = max(2, int(rate / _BORNE_HAUTE_HZ))
    lag_max = min(int(rate / _BORNE_BASSE_HZ), n - 1)
    if lag_min >= lag_max or n < lag_max:
        return 0.0
    r = np.correlate(x, x, mode="full")[n - 1:]
    energie = float(x @ x) + 1e-12
    fen = r[lag_min:lag_max + 1] / energie
    pic = float(fen.max())
    if pic < 0.3:  # autocorrélation trop faible : pas de périodicité nette
        return 0.0
    seuil = 0.5 * pic
    for i in range(len(fen)):
        if fen[i] >= seuil and (i == 0 or fen[i] >= fen[i - 1]) \
                and (i == len(fen) - 1 or fen[i] >= fen[i + 1]):
            return rate / (lag_min + i)
    return 0.0


def _caracteristiques_par_mot(mots, chemin_wav) -> list[tuple[float, float]]:
    """(RMS, hauteur) de chaque mot, lus directement dans le WAV (streamé)."""
    import numpy as np

    resultats: list[tuple[float, float]] = []
    with wave.open(str(chemin_wav)) as w:
        rate = w.getframerate()
        nframes = w.getnframes()
        for m in mots:
            i0 = int(max(0.0, float(m.start)) * rate)
            i1 = min(int(float(m.end) * rate), nframes)
            if i1 <= i0:
                resultats.append((0.0, 0.0))
                continue
            w.setpos(i0)
            brut = w.readframes(i1 - i0)
            seg = np.frombuffer(brut, dtype=np.int16).astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(seg * seg))) if len(seg) else 0.0
            f0 = _estimer_hauteur(seg, rate) if rms > _SEUIL_VOISE else 0.0
            resultats.append((rms, f0))
    return resultats


_ENCODER = None  # VoiceEncoder Resemblyzer, chargé paresseusement (une fois)


def _obtenir_encoder():
    """Charge (une seule fois) l'encodeur vocal Resemblyzer. Lève si absent.

    GPU CUDA prioritaire ; repli CPU si le chargement CUDA échoue (OOM, pilote…).
    """
    global _ENCODER
    if _ENCODER is None:
        from resemblyzer import VoiceEncoder

        from .devices import charger_sur_device

        _ENCODER, _ = charger_sur_device(lambda device: VoiceEncoder(device=device))
    return _ENCODER


def _embeddings_par_mot(mots, chemin_wav) -> list[tuple[float, object]]:
    """(RMS, embedding normalisé) de chaque mot, lus streamés dans le WAV.

    Un mot quasi muet (ou dont l'embedding échoue) porte ``None`` — il sera
    rattaché à la voix la plus proche, jamais une voix supplémentaire.
    """
    import numpy as np

    enc = _obtenir_encoder()
    resultats: list[tuple[float, object]] = []
    with wave.open(str(chemin_wav)) as w:
        rate = w.getframerate()
        nframes = w.getnframes()
        for m in mots:
            i0 = int(max(0.0, float(m.start)) * rate)
            i1 = min(int(float(m.end) * rate), nframes)
            if i1 <= i0:
                resultats.append((0.0, None))
                continue
            w.setpos(i0)
            seg = np.frombuffer(w.readframes(i1 - i0),
                                dtype=np.int16).astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(seg * seg))) if len(seg) else 0.0
            emb = None
            if rms > _SEUIL_VOISE:
                try:
                    e = np.asarray(enc.embed_utterance(seg), dtype=np.float64)
                    norme = float(np.linalg.norm(e))
                    emb = e / norme if norme > 0 else None
                except Exception:
                    emb = None  # segment trop court : mot non rattaché
            resultats.append((rms, emb))
    return resultats


def diariser_mots_embeddings(mots, chemin_wav) -> list[int] | None:
    """Étiquettes de personnage par embeddings vocaux (Resemblyzer).

    Retourne ``None`` si Resemblyzer/torch est indisponible (le pipeline
    retombe alors sur la hauteur). Chaque mot est plongé dans l'espace des
    d-vectors ; on projette sur la première composante principale (centrage +
    SVD) et on coupe au plus grand écart entre scores triés, comme la méthode
    par hauteur — le seuil relatif ``_SEUIL_GAP`` garantit qu'une seule voix
    (ou des timbres trop proches) reste un seul personnage. Partition
    déterministe : même audio → mêmes étiquettes.
    """
    if not mots:
        return []
    try:
        rms_emb = _embeddings_par_mot(mots, chemin_wav)
    except Exception:
        return None  # resemblyzer/torch absent → repli par la hauteur
    import numpy as np

    n = len(rms_emb)
    if n == 1:
        return [0]
    valides = [i for i, (rms, e) in enumerate(rms_emb) if e is not None]
    if len(valides) < 2:
        return [0] * n
    emb = np.array([rms_emb[i][1] for i in valides])
    centrees = emb - emb.mean(axis=0)
    _, valeurs, vt = np.linalg.svd(centrees, full_matrices=False)
    if valeurs[0] <= 1e-9:  # tous les embeddings identiques : une seule voix
        return [0] * n
    pc1 = centrees @ vt[0]
    scores = sorted((float(pc1[k]), k) for k in range(len(pc1)))
    ecart = scores[-1][0] - scores[0][0]
    if ecart < 1e-9:
        return [0] * n
    meilleur = max(range(len(scores) - 1),
                   key=lambda k: scores[k + 1][0] - scores[k][0])
    gap = scores[meilleur + 1][0] - scores[meilleur][0]
    if gap / ecart < _SEUIL_GAP:  # écart trop faible : pas deux voix distinctes
        return [0] * n
    seuil = (scores[meilleur][0] + scores[meilleur + 1][0]) / 2.0
    brut = {k: (0 if pc1[k] <= seuil else 1) for k in range(len(pc1))}
    if brut[0] == 1:  # recentrage : le premier mot valide devient la voix 0
        brut = {k: 1 - v for k, v in brut.items()}
    labels = [0] * n
    for i, (rms, e) in enumerate(rms_emb):
        if e is not None:
            labels[i] = brut[list(valides).index(i)]
        else:  # mot quasi muet → voix du mot voisé le plus proche dans le temps
            plus_proche = min(valides, key=lambda j: abs(j - i))
            labels[i] = brut[list(valides).index(plus_proche)]
    return labels


def diariser_avec_repli(mots, chemin_wav) -> list[int]:
    """Embeddings vocaux d'abord (voix de même tessiture) ; repli hauteur sinon.

    Le repli est transparent : si Resemblyzer/torch n'est pas installé (ou si
    l'extraction échoue), la méthode par hauteur (T56) s'applique — le pipeline
    ne lève jamais d'exception pour un moteur manquant.
    """
    labels = diariser_mots_embeddings(mots, chemin_wav)
    if labels is None:
        return diariser_mots(mots, chemin_wav)
    return labels


def _clustrer_embeddings(emb: np.ndarray) -> list[int] | None:
    """k-means sphérique k=2 (init déterministe : les 2 répliques les moins
    similaires), validé : le plus petit groupe doit couvrir au moins
    ``_FRACTION_MIN`` des segments et la pire marge intra−inter doit dépasser
    ``_SEUIL_MARGE``. Retourne ``None`` si la séparation n'est pas nette (une
    seule voix, ou deux timbres trop proches / déséquilibrés → artefact).
    """
    import numpy as np

    n = len(emb)
    if n < 2:
        return None
    sim = emb @ emb.T
    i, j = np.unravel_index(np.argmin(sim), sim.shape)
    c0, c1 = emb[i].copy(), emb[j].copy()
    for _ in range(40):
        d0, d1 = emb @ c0, emb @ c1
        a0, a1 = d0 >= d1, d0 < d1
        if a0.sum() == 0 or a1.sum() == 0:
            return None
        n0 = c0 + emb[a0].sum(0)
        n1 = c1 + emb[a1].sum(0)
        n0 /= np.linalg.norm(n0)
        n1 /= np.linalg.norm(n1)
        if np.allclose(n0, c0) and np.allclose(n1, c1):
            break
        c0, c1 = n0, n1
    d0, d1 = emb @ c0, emb @ c1
    lab = (d0 < d1).astype(int)
    g0, g1 = emb[lab == 0], emb[lab == 1]
    petit, grand = sorted((len(g0), len(g1)))
    if petit / max(n, 1) < _FRACTION_MIN:
        return None  # groupe trop petit : outliers, pas une voix
    if petit < 2 or grand < 2:
        return None
    intra = min((g0 @ g0.T).mean(), (g1 @ g1.T).mean())
    inter = (g0 @ g1.T).mean()
    if intra - inter < _SEUIL_MARGE:
        return None  # timbres trop proches : pas deux voix distinctes
    if lab[0] == 1:  # recentrage : la première réplique devient la voix 0
        lab = 1 - lab
    return [int(x) for x in lab]  # int Python (sérialisable JSON)


def _embeddings_spans(cues, chemin_wav) -> list[tuple[float, object]]:
    """(RMS, embedding normalisé) de chaque réplique (son intervalle entier)."""
    import numpy as np

    enc = _obtenir_encoder()
    resultats: list[tuple[float, object]] = []
    with wave.open(str(chemin_wav)) as w:
        rate = w.getframerate()
        nframes = w.getnframes()
        for c in cues:
            i0 = int(max(0.0, float(c.start)) * rate)
            i1 = min(int(float(c.end) * rate), nframes)
            if i1 - i0 < max(1, rate // 4):  # réplique trop courte : inutilisable
                resultats.append((0.0, None))
                continue
            w.setpos(i0)
            seg = np.frombuffer(w.readframes(i1 - i0),
                                dtype=np.int16).astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(seg * seg))) if len(seg) else 0.0
            emb = None
            if rms > _SEUIL_VOISE:
                try:
                    e = np.asarray(enc.embed_utterance(seg), dtype=np.float64)
                    norme = float(np.linalg.norm(e))
                    emb = e / norme if norme > 0 else None
                except Exception:
                    emb = None
            resultats.append((rms, emb))
    return resultats


def diariser_repliques_embeddings(cues, chemin_wav) -> list[int] | None:
    """Étiquettes par RÉPLIQUE (fenêtre 1–6 s) via les embeddings Resemblyzer.

    Bien plus robuste que mot à mot (les mots réels sont souvent < 0,4 s — trop
    courts pour l'encodeur) : chaque réplique est plongée entière, puis k-means
    k=2 validé (voir ``_clustrer_embeddings``). ``None`` si Resemblyzer est
    indisponible ou si la séparation n'est pas nette (repli hauteur ensuite).
    """
    if not cues:
        return []
    try:
        rms_emb = _embeddings_spans(cues, chemin_wav)
    except Exception:
        return None
    import numpy as np

    try:
        valides = [i for i, (rms, e) in enumerate(rms_emb) if e is not None]
        if len(valides) < 2:
            return None
        emb = np.array([rms_emb[i][1] for i in valides])
        labels_valides = _clustrer_embeddings(emb)
        if labels_valides is None:
            return None
        labels = [0] * len(rms_emb)
        for k, i in enumerate(valides):
            labels[i] = labels_valides[k]
        return labels
    except Exception:
        return None  # toute défaillance → repli hauteur, jamais d'exception


def diariser_repliques_avec_repli(cues, chemin_wav) -> list[int]:
    """Diarisation par réplique : embeddings Resemblyzer d'abord, repli hauteur
    (T56, mot à mot puis majorité par réplique) sinon — jamais d'exception.

    C'est la fonction utilisée par le pipeline : les fenêtres de 1 à 6 s d'une
    réplique donnent à l'encodeur vocal le contexte dont il a besoin, là où des
    mots de quelques dixièmes de seconde ne suffisent pas (T109–T111, session
    28).
    """
    labels = diariser_repliques_embeddings(cues, chemin_wav)
    if labels is not None:
        return labels
    per_mot = diariser_mots([w for c in cues for w in c.words], chemin_wav)
    labels, i = [], 0
    for cue in cues:
        tranche = per_mot[i:i + len(cue.words)]
        i += len(cue.words)
        labels.append(_majoritaire(tranche) if tranche else 0)
    return labels


def diariser_mots(mots, chemin_wav) -> list[int]:
    """Étiquettes de personnage (entiers 0..k−1) pour chaque mot, dans l'ordre.

    Une seule voix tant que les hauteurs ne présentent pas un écart net ; sinon
    coupure au plus grand écart entre hauteurs triées. Les mots quasi muets sont
    rattachés à la voix la plus proche (jamais une voix supplémentaire).
    """
    if not mots:
        return []
    rms_f0 = _caracteristiques_par_mot(mots, chemin_wav)
    n = len(rms_f0)
    if n == 1:
        return [0]
    voises = [(i, f0) for i, (rms, f0) in enumerate(rms_f0)
              if rms > _SEUIL_VOISE and f0 > 0.0]
    if not voises:
        return [0] * n
    hauteurs = sorted(f0 for _, f0 in voises)
    ecart = hauteurs[-1] - hauteurs[0]
    if ecart < _TOL_HAUTEUR_IDENTIQUE:
        return [0] * n
    meilleur_gap = max(range(len(hauteurs) - 1),
                      key=lambda k: hauteurs[k + 1] - hauteurs[k])
    gap = hauteurs[meilleur_gap + 1] - hauteurs[meilleur_gap]
    if gap / ecart < _SEUIL_GAP:  # écart trop faible : pas deux voix distinctes
        return [0] * n
    seuil = (hauteurs[meilleur_gap] + hauteurs[meilleur_gap + 1]) / 2.0
    groupe: dict[int, int] = {i: (0 if f0 <= seuil else 1) for i, f0 in voises}
    moyennes = {
        0: sum(f0 for i, f0 in voises if groupe[i] == 0) /
           max(sum(1 for i, _ in voises if groupe[i] == 0), 1),
        1: sum(f0 for i, f0 in voises if groupe[i] == 1) /
           max(sum(1 for i, _ in voises if groupe[i] == 1), 1),
    }
    for i, (rms, f0) in enumerate(rms_f0):
        if i not in groupe:  # mot quasi muet → voix la plus proche
            groupe[i] = 0 if abs(f0 - moyennes[0]) <= abs(f0 - moyennes[1]) else 1
    return [groupe[i] for i in range(n)]


def _majoritaire(segments: list[int]) -> int:
    """Étiquette la plus fréquente ; égalité → première rencontrée (stable)."""
    comptes: dict[int, int] = {}
    premier: dict[int, int] = {}
    for k, v in enumerate(segments):
        comptes[v] = comptes.get(v, 0) + 1
        premier.setdefault(v, k)
    return max(comptes, key=lambda v: (comptes[v], -premier[v]))


def attribuer_personnages(cues, labels: list[int] | None) -> None:
    """Affecte ``cue.personnage`` = voix majoritaire des mots de la réplique.

    ``labels`` est aligné séquentiellement sur les mots des ``cues`` (dans
    l'ordre) ; ``None`` (diarisation désactivée) ne touche à rien.
    """
    if labels is None:
        return
    i = 0
    for cue in cues:
        if not cue.words:
            continue
        tranche = labels[i:i + len(cue.words)]
        i += len(cue.words)
        if tranche:
            cue.personnage = _majoritaire(tranche)
