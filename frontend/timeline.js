/* Logique pure de cadrage des timelines de répliques (zoom 1×–60×).

   UMD sans dépendance :
   - sous Node (node:test) : `module.exports` ;
   - dans le navigateur : objet global `RythmoTimeline` (chargé avant app.js).

   Toutes ces fonctions sont pures (aucun DOM, aucun état) : elles convertissent
   une fenêtre de réplique `[début, fin]`, un niveau de zoom et un ancrage en
   fenêtre visible `[t0, t1]`, puis projettent les mots dans cette fenêtre.
   Zoomer ne modifie jamais les horaires : c'est un changement de cadrage.
 */
(function (global, factory) {
  if (typeof module !== "undefined" && module.exports) {
    module.exports = factory();
  } else {
    global.RythmoTimeline = factory();
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const ZOOM_MIN = 1;
  const ZOOM_MAX = 60;

  function bornesZoom(v) {
    const n = Number(v);
    if (Number.isNaN(n)) return ZOOM_MIN;
    if (n === Infinity) return ZOOM_MAX;
    if (n === -Infinity) return ZOOM_MIN;
    return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, n));
  }

  function clamp01(v) {
    const n = Number(v);
    if (Number.isNaN(n)) return 0;
    return Math.min(1, Math.max(0, n));
  }

  function fenetreZoom(d0, d1, zoom, ancrage) {
    let a = Number(d0);
    let b = Number(d1);
    if (!Number.isFinite(a)) a = 0;
    if (!Number.isFinite(b) || b <= a) b = a + 0.001;  // fenêtre minimale non nulle
    const duree = b - a;
    const largeur = duree / bornesZoom(zoom);
    const decalage = Math.max(0, duree - largeur);
    const t0 = a + clamp01(ancrage) * decalage;
    return { t0: t0, t1: t0 + largeur };
  }

  function positionMot(debut, fin, t0, t1) {
    const largeur = (t1 - t0) || 0.001;
    return {
      left: (Number(debut) - t0) / largeur * 100,
      width: (Number(fin) - Number(debut)) / largeur * 100,
    };
  }

  function decalerAncrage(ancrage, deltaSec, duree, zoom) {
    const d = Number(duree) || 0;
    const decalage = Math.max(0, d - d / bornesZoom(zoom));
    if (decalage <= 0) return clamp01(ancrage);  // 1× : rien à faire défiler
    return clamp01(clamp01(ancrage) + (Number(deltaSec) || 0) / decalage);
  }

  function pivoter(f, duree, zoom, ancrage, facteur) {
    // Zoom autour d'un point : la fraction `f` (0..1) de la fenêtre visible
    // garde le même instant sous la même fraction après le changement de zoom.
    const d = Math.max(Number(duree) || 0, 0);
    const frac = clamp01(f);
    const z = bornesZoom(zoom);
    const zPrime = bornesZoom(z * (Number(facteur) || 1));
    const anc = clamp01(ancrage);
    if (zPrime === z || d <= 0) return { zoom: zPrime, ancrage: anc };
    const largeur = d / z;
    const largeurP = d / zPrime;
    const tF = anc * Math.max(0, d - largeur) + frac * largeur;  // instant pivot (relatif d0=0)
    const decalageP = Math.max(0, d - largeurP);
    const ancrageP = decalageP > 0 ? clamp01((tF - frac * largeurP) / decalageP) : 0;
    return { zoom: zPrime, ancrage: ancrageP };
  }

  function echelle(t0, t1, largeurPx) {
    const px = Number(largeurPx);
    if (!Number.isFinite(px) || px <= 0) return 0;
    return (t1 - t0) / px;
  }

  const DUREE_MOT_MIN = 0.04;

  function arrondir3(v) {
    return Math.round(Number(v) * 1000) / 1000;
  }

  function normaliserToken(t) {
    return String(t || "")
      .toLowerCase()
      .normalize("NFKD")
      .replace(/[\u0300-\u036f]/g, "")
      .replace(/[^\p{L}\p{N}]/gu, "");
  }

  function distribuerUniforme(tokens, debut, fin) {
    const n = tokens.length;
    const largeur = Math.max((fin - debut) / n, DUREE_MOT_MIN);
    return tokens.map((t, i) => ({
      texte: t,
      debut: arrondir3(Math.min(debut + i * largeur, fin - DUREE_MOT_MIN)),
      fin: arrondir3(Math.min(debut + (i + 1) * largeur, fin)),
    }));
  }

  function diffOpcodes(a, b) {
    // Diff LCS entre deux listes de tokens normalisés → opcodes
    // {type:'equal'|'replace'|'insert'|'delete', i1,i2,j1,j2} (runs fusionnés ;
    // un « replace » = une suppression immédiatement suivie d'une insertion).
    const n = a.length, m = b.length;
    const dp = [];
    for (let i = 0; i <= n; i++) dp.push(new Array(m + 1).fill(0));
    for (let i = n - 1; i >= 0; i--)
      for (let j = m - 1; j >= 0; j--)
        dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1
                                 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    const bruts = [];
    let i = 0, j = 0;
    while (i < n || j < m) {
      if (i < n && j < m && a[i] === b[j]) { bruts.push(["equal", i, j]); i++; j++; }
      else if (i < n && (j >= m || dp[i + 1][j] >= dp[i][j + 1])) { bruts.push(["delete", i, j]); i++; }
      else { bruts.push(["insert", i, j]); j++; }
    }
    const opcodes = [];
    for (const [type, oi, oj] of bruts) {
      const prec = opcodes[opcodes.length - 1];
      if (prec && prec.type === type) { prec.i2 = oi + 1; prec.j2 = oj + 1; continue; }
      opcodes.push({ type, i1: oi, i2: oi + 1, j1: oj, j2: oj + 1 });
    }
    for (let k = 0; k + 1 < opcodes.length; k++) {
      if (opcodes[k].type === "delete" && opcodes[k + 1].type === "insert" &&
          opcodes[k].i2 === opcodes[k + 1].i1) {
        opcodes[k] = { type: "replace", i1: opcodes[k].i1, i2: opcodes[k].i2,
                       j1: opcodes[k + 1].j1, j2: opcodes[k + 1].j2 };
        opcodes.splice(k + 1, 1);
      }
    }
    return opcodes;
  }

  function forcerMonotonie(mots, debut, fin) {
    // Invariants de rendu : debut ≤ d < f ≤ fin et débuts non décroissants.
    mots.forEach(m => {
      m.debut = Math.min(Math.max(Number(m.debut), debut), fin - DUREE_MOT_MIN);
      m.fin = Math.min(Math.max(Number(m.fin), m.debut + DUREE_MOT_MIN), fin);
    });
    let precedent = debut;
    mots.forEach(m => {
      if (m.debut < precedent - 1e-9) {
        const glisser = precedent - m.debut;
        m.debut = precedent;
        m.fin = Math.min(m.fin + glisser, fin);
        if (m.fin <= m.debut) {
          m.debut = Math.min(m.debut, fin - DUREE_MOT_MIN);
          m.fin = m.debut + DUREE_MOT_MIN;
        }
      }
      precedent = m.fin > precedent ? m.fin : precedent;
      m.debut = arrondir3(m.debut);
      m.fin = arrondir3(m.fin);
    });
    return mots;
  }

  function resynchroniserMots(motsActuels, nouveauTexte, debut, fin) {
    // Recale la timeline quand l'utilisateur tape, même logique que
    // `resynchroniser_mots` côté serveur (difflib) : mots inchangés → timings
    // ET drapeau « incertain » conservés (Slice 16-bis A) ; mot inséré → le
    // silence entre voisins ; mot supprimé → disparaît. Un mot sans origine
    // (inséré, remplacé, distribué uniformément) n'a plus de confiance ASR.
    const tokens = String(nouveauTexte || "").split(/\s+/).filter(t => t.length);
    if (!tokens.length) return [];
    debut = Number(debut); fin = Number(fin);
    if (!isFinite(debut) || !isFinite(fin) || fin <= debut) return [];
    if (!motsActuels || !motsActuels.length)
      return distribuerUniforme(tokens, debut, fin);
    const seqO = motsActuels.map(m => normaliserToken(m.texte));
    const seqN = tokens.map(normaliserToken);
    if (seqO.length === seqN.length && seqO.every((t, i) => t === seqN[i])) {
      // même liste de mots (hors casse/ponctuation) : timings ET drapeau conservés
      return tokens.map((t, i) => ({
        texte: t, debut: motsActuels[i].debut, fin: motsActuels[i].fin,
        incertain: Boolean(motsActuels[i].incertain),
      }));
    }
    const intervalles = new Array(tokens.length).fill(null);
    const incertains = new Array(tokens.length).fill(false);
    diffOpcodes(seqO, seqN).forEach(op => {
      if (op.type === "equal") {
        for (let k = 0; k < op.i2 - op.i1; k++) {
          const m = motsActuels[op.i1 + k];
          intervalles[op.j1 + k] = [m.debut, m.fin];
          incertains[op.j1 + k] = Boolean(m.incertain);
        }
      } else if (op.type === "replace") {
        const g0 = op.i1 < motsActuels.length ? motsActuels[op.i1].debut : debut;
        const g1 = op.i2 > op.i1 ? motsActuels[op.i2 - 1].fin : g0;
        const nRep = Math.max(op.j2 - op.j1, 1);
        const tranche = Math.max(g1 - g0, DUREE_MOT_MIN) / nRep;
        for (let j = op.j1; j < op.j2; j++)
          intervalles[j] = [g0 + (j - op.j1) * tranche,
                            g0 + (j - op.j1 + 1) * tranche];
      } else if (op.type === "insert") {
        let lo = op.i1 > 0 ? motsActuels[op.i1 - 1].fin : debut;
        let hi = op.i1 < motsActuels.length ? motsActuels[op.i1].debut : fin;
        if (hi - lo < 2 * DUREE_MOT_MIN) {
          lo = op.i1 > 0 ? motsActuels[op.i1 - 1].debut : debut;
          hi = op.i1 > 0 ? motsActuels[op.i1 - 1].fin : lo + DUREE_MOT_MIN;
        }
        const nIns = op.j2 - op.j1;
        const tranche = (hi - lo) / (nIns + 2);
        for (let j = op.j1; j < op.j2; j++)
          intervalles[j] = [lo + (j - op.j1 + 1) * tranche,
                            lo + (j - op.j1 + 2) * tranche];
      }
      // delete : mot supprimé, rien à reporter
    });
    const comble = distribuerUniforme(tokens, debut, fin);
    return forcerMonotonie(tokens.map((t, i) => ({
      texte: t,
      debut: intervalles[i] ? intervalles[i][0] : comble[i].debut,
      fin: intervalles[i] ? intervalles[i][1] : comble[i].fin,
      incertain: incertains[i],
    })), debut, fin);
  }

  function vuesMots(mots) {
    // Payload `repliques.json` → vues de piste (texte/debut/fin normalisés),
    // drapeau « incertain » PRÉSERVÉ (Slice 16-bis B) : jamais de perte
    // silencieuse du drapeau entre le serveur et l'affichage de l'éditeur.
    return (Array.isArray(mots) ? mots : []).map(m => ({
      texte: String(m && m.texte != null ? m.texte : ""),
      debut: Number(m && m.debut),
      fin: Number(m && m.fin),
      incertain: Boolean(m && m.incertain),
    }));
  }

  return {
    ZOOM_MIN: ZOOM_MIN,
    ZOOM_MAX: ZOOM_MAX,
    bornesZoom: bornesZoom,
    fenetreZoom: fenetreZoom,
    positionMot: positionMot,
    decalerAncrage: decalerAncrage,
    pivoter: pivoter,
    echelle: echelle,
    vuesMots: vuesMots,
    distribuerUniforme: distribuerUniforme,
    resynchroniserMots: resynchroniserMots,
  };
});
