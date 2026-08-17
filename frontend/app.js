(() => {
  const $ = s => document.querySelector(s);

  /* ——————— bande rythmo (en-tête) : héros OU mini-lecteur du job ———————
     Par défaut, mini bande défilante : le produit lui-même en héros (mots
     dupliqués pour une boucle sans couture, figée si prefers-reduced-motion).
     Dès qu'un job est éditable (répliques horodatées + audio disponibles),
     la bande devient un VRAI mini-lecteur : les mots du job défilent AU
     RYTHME DE L'AUDIO — piste temporelle rigide (même loi que le rendu
     serveur : position s = v·début, anti-chevauchement), lecture/pause,
     timecode et clic pour se déplacer. */
  const bandeFenetre = document.querySelector(".bande-fenetre");
  const bandePiste = document.querySelector(".bande-piste");
  const bandeControles = document.querySelector("#bande-controles");
  const bandeJouer = document.querySelector("#bande-jouer");
  const bandeTemps = document.querySelector("#bande-temps");
  const VITESSE_BANDE = 0.32;  // fraction de la largeur par seconde (réf. pro)
  const ESPACE_BANDE = 26;     // px entre mots sur la piste
  const DUREE_TRANCHE = 20;    // s par tranche audio chargée (jamais le WAV entier)
  let rafHero = null;          // boucle héros (null = lecteur actif)
  let lecteurBande = null;     // état du mini-lecteur (null = bande héros)

  function demarrerBandeHero() {
    const fenetre = bandeFenetre, piste = bandePiste;
    if (!fenetre || !piste) return;
    piste.innerHTML = "";
    ["Le", "doublage", "mot", "par", "mot", "100 %", "local"].forEach(t => {
      const m = document.createElement("span");
      m.className = "bande-mot";
      m.textContent = t;
      piste.appendChild(m);
    });
    const originaux = [...piste.children];
    if (originaux.length < 2) return;
    originaux.forEach(m => piste.appendChild(m.cloneNode(true)));
    const mots = [...piste.querySelectorAll(".bande-mot")];
    const n = originaux.length;
    const largeurDemi = mots[n].offsetLeft - mots[0].offsetLeft;
    if (largeurDemi <= 0) return;
    const reduit = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const VITESSE = 0.07;   // fraction de la largeur par seconde
    let tx = 0;

    function placer() {
      const curseur = fenetre.clientWidth * 0.15;
      mots.forEach(m => {
        const g = m.offsetLeft + tx;
        const d = g + m.offsetWidth;
        m.classList.toggle("actif", g <= curseur + 1 && d >= curseur - 1);
        m.classList.toggle("passe", d < curseur - 6);
      });
      piste.style.transform = "translateX(" + tx + "px)";
    }

    // position initiale : le deuxième mot est sous le curseur, en surbrillance
    tx = fenetre.clientWidth * 0.15 - (mots[1].offsetLeft + mots[1].offsetWidth / 2);
    placer();
    if (reduit) return;  // bande figée

    let precedent = null;
    function cadre(ts) {
      if (!precedent) precedent = ts;
      const dt = Math.min((ts - precedent) / 1000, 0.1);
      precedent = ts;
      tx -= fenetre.clientWidth * VITESSE * dt;
      if (tx <= -largeurDemi) tx += largeurDemi;
      placer();
      rafHero = requestAnimationFrame(cadre);
    }
    rafHero = requestAnimationFrame(cadre);
  }  demarrerBandeHero();

  let fichierChoisi = null;
  let jobCourant = null;        // job en cours (événements + édition)
  let dureeVideoJob = 0;        // durée vidéo annoncée par l'analyse
  let sourceCourante = null;    // EventSource de suivi (jamais deux à la fois)
  let editionModifiee = false;   // corrections non encore validées côté serveur
  let nomsPersonnages = [];     // noms des personnages de la scène (index 0 = voix 1)
  let termesGlossaire = [];     // termes du glossaire du projet (panneau éditeur)
  let candidatsWikipedia = [];  // candidats proposés par l'import Wikipédia
  let candidatsWikipediaCategories = null;  // classement LLM {essentiels, difficiles, termes}
  let importWikipediaEnCours = false;  // garde anti-double-clic de l'import
  let menuPersonnage = null;    // menu contextuel « attribuer ce mot à un personnage »

  const RAISONS_DEVICE = {
    torch_absent: "torch absent",
    torch_sans_cuda: "torch sans CUDA",
  };
  fetch("/api/health").then(r => r.json()).then(h => {
    let libelle = h.device.toUpperCase();
    if (h.device_raison) {
      libelle += " · " + (RAISONS_DEVICE[h.device_raison] || h.device_raison);
    }
    $("#device").textContent = libelle;
    $("#version").textContent = "v" + h.version;
  }).catch(() => { $("#device").textContent = "?"; });

  // ——— slice 3 : projets déjà analysés sur cette machine (étape 01) ———
  // La liste est chargée au démarrage ; rouvrir ré-hydrate le job sans
  // ré-upload ni ré-analyse (le serveur scanne data/jobs).
  async function chargerProjets() {
    const bloc = $("#section-projets"), liste = $("#liste-projets");
    if (!bloc || !liste) return;
    try {
      const rep = await fetch("/api/projets");
      if (!rep.ok) return;
      const corps = await rep.json();
      const projets = corps.projets || [];
      liste.innerHTML = "";
      projets.forEach(p => {
        const infos = '<span class="infos-projet">' +
          (p.nom_source || p.id) +
          (p.duree ? " · " + p.duree.toFixed(1) + " s" : "") +
          " · " + (p.statut === "termine" ? "terminé" : "édition en pause") +
          '</span>';
        liste.insertAdjacentHTML('beforeend',
          '<li class="ligne-projet">' + infos +
          '<span class="actions-projet">' +
          '<button type="button" class="bouton-rouvrir" ' +
          'data-id="' + p.id + '">📂 Rouvrir</button>' +
          '<button type="button" class="bouton-supprimer-projet" ' +
          'data-id="' + p.id + '" title="Supprimer ce projet et tous ses fichiers">🗑 Supprimer</button>' +
          '</span></li>');
      });
      liste.querySelectorAll(".bouton-rouvrir").forEach(b =>
        b.addEventListener("click", () => rouvrirProjet(b.dataset.id)));
      liste.querySelectorAll(".bouton-supprimer-projet").forEach(b =>
        b.addEventListener("click", () => supprimerProjet(b.dataset.id)));
      bloc.hidden = !projets.length;
    } catch (err) { /* serveur injoignable : la section reste cachée */ }
  }

  async function rouvrirProjet(id) {
    try {
      const rep = await fetch("/api/projets/" + encodeURIComponent(id) + "/rouvrir",
                              { method: "POST" });
      if (!rep.ok) {
        afficherErreur("impossible de rouvrir ce projet (HTTP " + rep.status + ")");
        return;
      }
      jobCourant = id;
      const r2 = await fetch("/api/jobs/" + encodeURIComponent(id) + "/repliques");
      if (!r2.ok) {
        afficherErreur("impossible de charger les répliques de ce projet");
        return;
      }
      cacherErreur();
      ouvrirEditeur(await r2.json());
      $("#resultat").style.display = "none";
    } catch (err) {
      afficherErreur("serveur injoignable : " + err.message);
    }
  }

  async function supprimerProjet(id) {
    // Confirmation native : on n'efface jamais une vidéo, son analyse et son
    // rendu sans un accord explicite du comédien.
    if (!confirm("Supprimer définitivement ce projet (vidéo, analyse et rendu) ?"))
      return;
    try {
      const rep = await fetch("/api/projets/" + encodeURIComponent(id),
                              { method: "DELETE" });
      if (!rep.ok) {
        const d = await rep.json().catch(() => ({}));
        afficherErreur("suppression refusée : " + (d.detail || ("HTTP " + rep.status)));
        return;
      }
      if (jobCourant === id) {
        jobCourant = null;
        arreterBandeLecteur();
        $("#editeur").style.display = "none";
        $("#resultat").style.display = "none";
      }
      chargerProjets();  // rafraîchit la liste
    } catch (err) {
      afficherErreur("serveur injoignable : " + err.message);
    }
  }
  chargerProjets();

  const zone = $("#zone-depot"), champ = $("#champ-fichier");
  zone.addEventListener("click", () => champ.click());
  zone.addEventListener("dragover", e => { e.preventDefault(); zone.classList.add("survole"); });
  zone.addEventListener("dragleave", () => zone.classList.remove("survole"));
  zone.addEventListener("drop", e => {
    e.preventDefault(); zone.classList.remove("survole");
    if (e.dataTransfer.files.length) retenir(e.dataTransfer.files[0]);
  });
  champ.addEventListener("change", () => champ.files.length && retenir(champ.files[0]));

  function retenir(f) {
    fichierChoisi = f;
    $("#nom-fichier").textContent = "✔ " + f.name + " (" + (f.size / 1048576).toFixed(1) + " Mo)";
    $("#nom-fichier").style.display = "block";
    $("#bouton-lancer").disabled = false;
    cacherErreur();
  }

  function afficherErreur(msg) {
    const e = $("#erreur"); e.textContent = "⚠ " + msg; e.style.display = "block";
  }
  function cacherErreur(){ $("#erreur").style.display = "none"; }

  function marquerModifiee() {
    editionModifiee = true;
    majEtatEdition();
  }

  function majEtatEdition() {
    const el = $("#etat-edition");
    if (!el) return;
    el.textContent = editionModifiee ? "● non enregistré" : "";
    el.classList.toggle("modifie", editionModifiee);
  }

  $("#bouton-lancer").addEventListener("click", async () => {
    if (!fichierChoisi) return;
    cacherErreur();
    $("#bouton-lancer").disabled = true;
    $("#resultat").style.display = "none";
    $("#progress").style.display = "block";
    progression(0, "envoi de la vidéo…");

    const options = {
      modele: $("#opt-modele").value,
      asr: $("#opt-asr").value,
      langue: $("#opt-langue").value || null,
      vocabulaire: ($("#opt-vocabulaire").value || "")
        .split(",").map(s => s.trim()).filter(Boolean),
      style: $("#opt-style").value,
      theme: $("#opt-theme").value,
      hauteur_bande: parseInt($("#opt-taille").value) || 110,
      taille_police_min: $("#opt-taille-police-min").value
        ? Math.max(10, Math.min(200,
            parseInt($("#opt-taille-police-min").value) || 200))
        : null,
      curseur_ratio: Math.min(50, Math.max(5, parseInt($("#opt-curseur").value) || 15)) / 100,
      // T149 : valeur brute ("0.24", "dynamique"…) — parseFloat détruirait
      // « dynamique » en NaN ; le backend traduit chaque sentinelle.
      vitesse: $("#opt-vitesse").value || null,
      etirer: $("#opt-etirer").checked,
      lipsync: $("#opt-lipsync").checked,
      edition: $("#opt-edition").checked,
      diariser: $("#opt-diariser").checked,
    };
    const corps = new FormData();
    corps.append("fichier", fichierChoisi);
    corps.append("options", JSON.stringify(options));

    let rep;
    try {
      rep = await fetch("/api/jobs", { method: "POST", body: corps });
    } catch (err) {
      progression(0, ""); $("#progress").style.display = "none";
      $("#bouton-lancer").disabled = false;
      return afficherErreur("serveur injoignable : " + err.message);
    }
    if (rep.status !== 202) {
      const d = await rep.json().catch(() => ({}));
      $("#progress").style.display = "none";
      $("#bouton-lancer").disabled = false;
      return afficherErreur(d.detail || ("refus du fichier (HTTP " + rep.status + ")"));
    }
    const { job_id } = await rep.json();
    jobCourant = job_id;
    arreterBandeLecteur();  // nouveau job : bande héros jusqu'aux répliques
    $("#bouton-annuler").onclick = async () => {
      if (!confirm("Annuler le traitement en cours ?")) return;
      try { await fetch("/api/jobs/" + job_id + "/cancel", { method: "POST" }); } catch (_) {}
    };
    suivre(job_id);
  });

  /* ——————— éditeur de répliques ——————— */

  function reediterRepliques() {
    if (!jobCourant) return;
    cacherErreur();
    fetch("/api/jobs/" + jobCourant + "/repliques")
      .then(r => r.ok ? r.json()
                      : Promise.reject(new Error("HTTP " + r.status)))
      .then(donnees => { ouvrirEditeur(donnees); $("#resultat").style.display = "none"; })
      .catch(err => afficherErreur("impossible de récupérer les répliques : " + err.message));
  }

  function sauvegarderRepliques() {
    const repliques = collecterRepliques();
    // ni chevauchement ni fin au-delà de la vidéo n'empêchent de sauvegarder :
    // ce fichier sert à archiver/échanger, pas à valider (le PUT, lui, reste strict)
    const erreurs = validerLocal(repliques, { pourFichier: true });
    afficherErreursEdition(erreurs);
    if (erreurs.length) return;
    const donnees = { "version": 1, "duree_video": dureeVideoJob,
                      "job": jobCourant, "repliques": repliques,
                      "personnages": collecterPersonnages() };
    const blob = new Blob([JSON.stringify(donnees, null, 2)],
                          { type: "application/json" });
    const lien = document.createElement("a");
    lien.href = URL.createObjectURL(blob);
    lien.download = "rythmo_repliques_" + (jobCourant || "brouillon") + ".json";
    lien.click();
    URL.revokeObjectURL(lien.href);
  }

  function chargerRepliques(fichier) {
    const lecteur = new FileReader();
    lecteur.onload = () => {
      let donnees;
      try {
        donnees = JSON.parse(lecteur.result);
      } catch (err) {
        return afficherErreursEdition(["Fichier illisible : JSON invalide."]);
      }
      const repliques = Array.isArray(donnees) ? donnees : donnees.repliques;
      if (!Array.isArray(repliques) || !repliques.length)
        return afficherErreursEdition(["Aucune réplique trouvée dans ce fichier."]);
      const vues = repliques.map(r => ({
        id: r.id != null ? String(r.id) : undefined,
        texte: String(r.texte ?? ""),
        debut: Number(r.debut), fin: Number(r.fin),
        personnage: r.personnage,
        mots: Array.isArray(r.mots) ? r.mots : undefined,
      }));
      const erreurs = validerLocal(vues, { pourFichier: true });
      if (erreurs.length)
        return afficherErreursEdition(["Fichier refusé :", ...erreurs]);
      if (Array.isArray(donnees.personnages))
        initialiserPersonnages({ repliques: vues, personnages: donnees.personnages });
      remplacerListeRepliques(vues);
    };
    lecteur.readAsText(fichier);
  }

  /* ——————— import de sous-titres .srt (T118) ——————— */

  function remplacerListeRepliques(vues) {
    $("#liste-repliques").innerHTML = "";
    vues.forEach(r => ajouterLigneReplique(r, true));
    afficherErreursEdition([]);
    marquerModifiee();
    // si la bande d'en-tête est en mode lecteur (job en cours), on la resynchronise
    if (lecteurBande) activerBandeLecteur({ repliques: vues, duree_video: dureeVideoJob });
  }

  function secondesSrt(chaine) {
    // « HH:MM:SS,mmm » (virgule = norme SRT) ou « HH:MM:SS.mmm » ; on cherche
    // le premier horodatage de la chaîne pour ignorer d'éventuels réglages de
    // position placés après le temps de fin.
    const m = /(\d{1,2}):(\d{1,2}):(\d{1,2})[,.](\d{1,3})/.exec(String(chaine));
    if (!m) return null;
    const h = parseInt(m[1], 10), min = parseInt(m[2], 10), s = parseInt(m[3], 10);
    const ms = parseInt(m[4].padEnd(3, "0"), 10);
    return h * 3600 + min * 60 + s + ms / 1000;
  }

  function nettoyerTexteSrt(chaine) {
    return String(chaine || "")
      .replace(/<[^>]*>/g, "")                // balises <i>, <font>… courantes
      .replace(/&amp;/g, "&").replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">").replace(/&nbsp;/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  function parserSrt(contenu) {
    // Le .srt sépare chaque cue par une ligne vide : on découpe sur ces lignes
    // vides, puis on localise la ligne « debut --> fin » de chaque bloc.
    const brut = String(contenu || "")
      .replace(/^\uFEFF/, "")                 // BOM UTF-8 éventuel
      .replace(/\r\n?/g, "\n");
    const repliques = [];
    for (const bloc of brut.split(/\n[ \t]*\n/)) {
      const lignes = bloc.split("\n").map(l => l.trim()).filter(Boolean);
      if (!lignes.length) continue;
      const iFleche = lignes.findIndex(l => l.includes("-->"));
      if (iFleche === -1) continue;
      const morceaux = lignes[iFleche].split("-->");
      if (morceaux.length !== 2) continue;
      const debut = secondesSrt(morceaux[0]);
      const fin = secondesSrt(morceaux[1]);
      if (debut == null || fin == null || fin <= debut) continue;
      const texte = nettoyerTexteSrt(lignes.slice(iFleche + 1).join(" "));
      if (!texte) continue;
      repliques.push({ texte, debut, fin });
    }
    return repliques;
  }

  function importerSrt(fichier) {
    const lecteur = new FileReader();
    lecteur.onload = () => {
      const repliques = parserSrt(lecteur.result);
      if (!repliques.length)
        return afficherErreursEdition(
          ["Aucune réplique trouvée : ce fichier .srt est vide ou mal formé."]);
      // Chaque cue devient une réplique ; faute d'alignement audio, ses mots
      // sont répartis sur sa fenêtre (même distribution uniforme que le repli
      // serveur) : la timeline est manipulable immédiatement.
      const vues = repliques.map(r => ({
        texte: r.texte,
        debut: r.debut,
        fin: r.fin,
        mots: RythmoTimeline.distribuerUniforme(
          r.texte.split(/\s+/).filter(Boolean), r.debut, r.fin),
      }));
      const erreurs = validerLocal(vues, { pourFichier: true });
      if (erreurs.length)
        return afficherErreursEdition(["Fichier refusé :", ...erreurs]);
      remplacerListeRepliques(vues);
    };
    lecteur.readAsText(fichier);
  }

  /* ——————— projet versionné : enregistrer / restaurer (T89–T93) ——————— */

  function enregistrerProjet() {
    if (!jobCourant) return;
    fetch("/api/jobs/" + jobCourant + "/projet")
      .then(r => r.ok ? r.blob() : Promise.reject(new Error("HTTP " + r.status)))
      .then(blob => {
        const lien = document.createElement("a");
        lien.href = URL.createObjectURL(blob);
        lien.download = "rythmo_projet_" + jobCourant + ".json";
        lien.click();
        URL.revokeObjectURL(lien.href);
      })
      .catch(err => afficherErreursEdition(
        ["impossible d'enregistrer le projet : " + err.message]));
  }

  function chargerProjet(fichier) {
    const lecteur = new FileReader();
    lecteur.onload = () => {
      let projet;
      try { projet = JSON.parse(lecteur.result); }
      catch (err) {
        return afficherErreursEdition(["Fichier illisible : JSON invalide."]);
      }
      if (!projet || typeof projet !== "object" || !projet.repliques)
        return afficherErreursEdition(
          ["Ce fichier n'est pas un projet Rythmo Dub (champ « repliques » absent)."]);
      if (!jobCourant)
        return afficherErreursEdition(["Aucun job actif : lancez d'abord une vidéo."]);
      fetch("/api/jobs/" + jobCourant + "/projet", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(projet),
      })
        .then(r => r.ok ? r.json()
                        : r.json().then(e => Promise.reject(new Error(
                            (e.detail && (e.detail.message_utilisateur || e.detail.message)) ||
                            ("refus du serveur (HTTP " + r.status + ")")))))
        .then(() => {
          afficherErreursEdition([]);
          $("#editeur").style.display = "none";
          $("#progress").style.display = "block";
          progression(80, "projet restauré : rendu en cours…");
          if (sourceCourante) { sourceCourante.close(); sourceCourante = null; }
          suivre(jobCourant);
        })
        .catch(err => afficherErreursEdition(["projet refusé : " + err.message]));
    };
    lecteur.readAsText(fichier);
  }

  /* ——————— personnages de la scène (noms + parole simultanée, T125) ——————— */

  function echapperHtml(chaine) {
    return String(chaine == null ? "" : chaine).replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function optionsVoix() {
    let html = '<option value="">—</option>';
    nomsPersonnages.forEach((nom, i) => {
      html += '<option value="' + (i + 1) + '">' +
              echapperHtml(nom || ("Personnage " + (i + 1))) + "</option>";
    });
    return html;
  }

  function majSelectsVoix() {
    document.querySelectorAll(".champ-voix").forEach(sel => {
      const garde = sel.value;
      sel.innerHTML = optionsVoix();
      if ([...sel.options].some(o => o.value === garde)) sel.value = garde;
    });
  }

  function majChampsNoms() {
    const conteneur = $("#noms-personnages");
    if (!conteneur) return;
    conteneur.innerHTML = "";
    nomsPersonnages.forEach((nom, i) => {
      const champ = document.createElement("input");
      champ.type = "text";
      champ.className = "nom-personnage";
      champ.value = nom || "";
      champ.placeholder = "Personnage " + (i + 1);
      champ.title = "Nom du personnage " + (i + 1);
      champ.addEventListener("input", () => {
        nomsPersonnages[i] = champ.value.trim() || ("Personnage " + (i + 1));
        majSelectsVoix();
        marquerModifiee();
      });
      conteneur.appendChild(champ);
    });
    majSelectsVoix();
  }

  function initialiserPersonnages(donnees) {
    let voixMax = 0;
    (donnees.repliques || []).forEach(r => {
      if (r.personnage != null) voixMax = Math.max(voixMax, Number(r.personnage) + 1);
    });
    const annonces = Array.isArray(donnees.personnages) ? donnees.personnages : null;
    let nb = Math.max(annonces ? annonces.length : 0,
                      Number(donnees.nb_personnages) || 0, voixMax);
    if (!nb) nb = 2;  // aucune information : un dialogue à deux par défaut
    nomsPersonnages = [];
    for (let i = 0; i < nb; i++) {
      nomsPersonnages.push(annonces && annonces[i]
        ? String(annonces[i]) : "Personnage " + (i + 1));
    }
    const champ = $("#nb-personnages");
    if (champ) champ.value = String(nb);
    majChampsNoms();
  }

  function collecterPersonnages() {
    return nomsPersonnages.slice();
  }

  /* ——————— glossaire du projet (panneau d'édition, Slice 18) ——————— */

  function cleGlossaire(terme) {
    return String(terme || "").toLowerCase()
      .normalize("NFD").replace(/[\u0300-\u036f]/g, "");
  }

  function rendererGlossaire() {
    const conteneur = $("#liste-glossaire");
    if (!conteneur) return;
    conteneur.innerHTML = "";
    termesGlossaire.forEach((terme, i) => {
      const badge = document.createElement("span");
      badge.className = "terme-glossaire";
      const texte = document.createElement("span");
      texte.textContent = terme;
      const retirer = document.createElement("button");
      retirer.type = "button";
      retirer.className = "retirer-glossaire";
      retirer.textContent = "✕";
      retirer.title = "Retirer « " + terme + " »";
      retirer.addEventListener("click", () => {
        termesGlossaire.splice(i, 1);
        rendererGlossaire();
        marquerGlossaireModifie();
      });
      badge.appendChild(texte);
      badge.appendChild(retirer);
      conteneur.appendChild(badge);
    });
  }

  function marquerGlossaireModifie() {
    const etat = $("#etat-glossaire");
    etat.textContent = "modifié — non enregistré";
    etat.className = "etat-glossaire modifie";
  }

  function ajouterTermeGlossaire() {
    const champ = $("#glossaire-terme");
    const valeur = (champ.value || "").trim();
    if (!valeur) return;
    const cle = cleGlossaire(valeur);
    if (!termesGlossaire.some(t => cleGlossaire(t) === cle)) {
      termesGlossaire.push(valeur);
    }
    champ.value = "";
    rendererGlossaire();
    marquerGlossaireModifie();
  }

  async function chargerGlossaire() {
    try {
      const rep = await fetch("/api/jobs/" + jobCourant + "/glossaire");
      if (!rep.ok) return;
      const donnees = await rep.json();
      termesGlossaire = Array.isArray(donnees.termes) ? donnees.termes : [];
      rendererGlossaire();
      const etat = $("#etat-glossaire");
      etat.textContent = "";
      etat.className = "etat-glossaire";
    } catch (_) { /* glossaire indisponible : la liste reste vide */ }
  }

  async function enregistrerGlossaire() {
    const etat = $("#etat-glossaire");
    try {
      const rep = await fetch("/api/jobs/" + jobCourant + "/glossaire", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ termes: termesGlossaire }),
      });
      if (!rep.ok) {
        const d = await rep.json().catch(() => ({}));
        etat.textContent = "échec : " + (d.detail || ("HTTP " + rep.status));
        etat.className = "etat-glossaire erreur";
        return;
      }
      const donnees = await rep.json();
      termesGlossaire = Array.isArray(donnees.termes) ? donnees.termes : termesGlossaire;
      rendererGlossaire();
      etat.textContent = "enregistré ✓";
      etat.className = "etat-glossaire enregistre";
    } catch (err) {
      etat.textContent = "serveur injoignable : " + err.message;
      etat.className = "etat-glossaire erreur";
    }
  }

  /* ——————— import Wikipédia (candidats → glossaire, Slice 1) ——————— */

  const CATEGORIES_WIKIPEDIA = ["essentiels", "difficiles", "termes"];
  const LIBELLES_CATEGORIES = { essentiels: "★ Essentiels",
                                 difficiles: "⚠ Difficiles", termes: "Termes" };

  function badgeCandidat(terme) {
    const badge = document.createElement("label");
    badge.className = "candidat-wikipedia";
    const caseCoche = document.createElement("input");
    caseCoche.type = "checkbox";
    caseCoche.value = terme;
    caseCoche.checked = true;
    const texte = document.createElement("span");
    texte.textContent = terme;
    badge.appendChild(caseCoche);
    badge.appendChild(texte);
    return badge;
  }

  function rendererCandidatsWikipedia() {
    const conteneur = $("#candidats-wikipedia");
    const actions = $("#actions-candidats-wikipedia");
    if (!conteneur) return;
    conteneur.innerHTML = "";
    const categories = candidatsWikipediaCategories;
    const categoriesFournies = categories &&
      CATEGORIES_WIKIPEDIA.some(c => Array.isArray(categories[c]) && categories[c].length);
    if (categoriesFournies) {
      CATEGORIES_WIKIPEDIA.forEach(categorie => {
        const termes = Array.isArray(categories[categorie]) ? categories[categorie] : [];
        if (!termes.length) return;
        const titre = document.createElement("div");
        titre.className = "categorie-wikipedia";
        titre.textContent = LIBELLES_CATEGORIES[categorie] + " (" + termes.length + ")";
        conteneur.appendChild(titre);
        termes.forEach(terme => conteneur.appendChild(badgeCandidat(terme)));
      });
    } else {
      candidatsWikipedia.forEach(terme => conteneur.appendChild(badgeCandidat(terme)));
    }
    if (actions) actions.hidden = candidatsWikipedia.length === 0;
  }

  async function importerWikipedia() {
    if (!jobCourant || importWikipediaEnCours) return;
    const champ = $("#wikipedia-titre");
    const etat = $("#etat-import-wikipedia");
    const bouton = $("#bouton-importer-wikipedia");
    const boutonReessayer = $("#bouton-reessayer-wikipedia");
    const saisie = (champ.value || "").trim();
    if (!saisie) return;
    const langue = ($("#wikipedia-langue") ? $("#wikipedia-langue").value : "") || "fr";
    const methode = ($("#wikipedia-methode") ? $("#wikipedia-methode").value : "") || "wikipedia";
    importWikipediaEnCours = true;
    if (bouton) { bouton.disabled = true; bouton.textContent = "récupération…"; }
    if (etat) { etat.textContent = "récupération…"; etat.className = "etat-glossaire"; }
    if (boutonReessayer) boutonReessayer.hidden = true;
    const corps = { titre: saisie, langue: langue, methode: methode };
    if (methode === "llm") {
      corps.modele = ($("#traduction-modele") ? $("#traduction-modele").value : "") || undefined;
      Object.assign(corps, configMoteurTraduction());
    }
    try {
      const rep = await fetch("/api/jobs/" + jobCourant + "/glossaire/candidats-wikipedia", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(corps),
      });
      if (!rep.ok) {
        const d = await rep.json().catch(() => ({}));
        const detail = d.detail || {};
        if (etat) {
          etat.textContent = detail.message_utilisateur || detail.message ||
            ("HTTP " + rep.status);
          etat.className = "etat-glossaire erreur";
        }
        if (boutonReessayer) boutonReessayer.hidden = false;
        return;
      }
      const donnees = await rep.json();
      candidatsWikipedia = Array.isArray(donnees.candidats) ? donnees.candidats : [];
      candidatsWikipediaCategories =
        donnees.categories && typeof donnees.categories === "object"
          ? donnees.categories : null;
      rendererCandidatsWikipedia();
      if (etat) {
        etat.textContent = candidatsWikipedia.length
          ? candidatsWikipedia.length + " candidats proposés — cochez puis ajoutez (" +
            (donnees.langue || "fr") + ")"
          : "aucun terme trouvé pour cette page";
        etat.className = "etat-glossaire";
      }
    } catch (err) {
      if (etat) {
        etat.textContent = "serveur injoignable : " + err.message;
        etat.className = "etat-glossaire erreur";
      }
      if (boutonReessayer) boutonReessayer.hidden = false;
    } finally {
      importWikipediaEnCours = false;
      if (bouton) { bouton.disabled = false; bouton.textContent = "🔎 Importer"; }
    }
  }

  function reessayerAvecWikipedia() {
    const sel = $("#wikipedia-methode");
    if (sel && sel.value === "llm") sel.value = "wikipedia";
    importerWikipedia();
  }

  function ajouterSelectionWikipedia() {
    const coches = [...document.querySelectorAll("#candidats-wikipedia input:checked")]
      .map(c => c.value);
    let ajoutes = 0;
    coches.forEach(terme => {
      const cle = cleGlossaire(terme);
      if (!termesGlossaire.some(t => cleGlossaire(t) === cle)) {
        termesGlossaire.push(terme);
        ajoutes++;
      }
    });
    if (ajoutes) {
      rendererGlossaire();
      marquerGlossaireModifie();
    }
  }

  function fermerMenuPersonnage() {
    if (menuPersonnage) { menuPersonnage.remove(); menuPersonnage = null; }
  }

  function assignerMotPersonnage(piste, indexMot, indicePersonnage) {
    const bloc = piste.closest(".bloc-replique");
    if (!bloc) return;
    const ligne = bloc.querySelector(".ligne-replique");
    const mots = piste.mots;
    if (!mots || mots.length < 2) {
      afficherErreursEdition(
        ["Impossible d'attribuer ce mot : la réplique doit conserver au moins un mot."]);
      return;
    }
    const [deplace] = mots.splice(indexMot, 1);
    const restants = mots;
    piste.mots = restants;
    ligne.querySelector(".champ-texte").value =
      restants.map(m => m.texte).join(" ");
    // la fenêtre de la source suit son premier/dernier mot restant
    ligne.querySelector(".champ-debut").value = restants[0].debut.toFixed(3);
    ligne.querySelector(".champ-fin").value =
      restants[restants.length - 1].fin.toFixed(3);
    rendererPiste(piste);

    // un clone par (réplique source, personnage) : la première attribution
    // crée le clone, les suivantes vers le MÊME personnage y ajoutent le mot
    const clones = bloc.__clones || (bloc.__clones = {});
    let clone = clones[indicePersonnage];
    if (!clone || !clone.isConnected) {
      const liste = $("#liste-repliques");
      const suivant = bloc.nextElementSibling;
      const index = [...liste.children].indexOf(bloc) + 1;
      clone = ligneReplique({ texte: deplace.texte, debut: deplace.debut,
                              fin: deplace.fin, personnage: indicePersonnage,
                              mots: [deplace] }, index, true);
      clone.classList.add("clone");
      if (suivant) liste.insertBefore(clone, suivant);
      else liste.appendChild(clone);
      clones[indicePersonnage] = clone;
    } else {
      const pisteClone = clone.querySelector(".piste-mots");
      const motsClone = pisteClone.mots;
      motsClone.push(deplace);
      motsClone.sort((a, b) => a.debut - b.debut);
      const ligneClone = clone.querySelector(".ligne-replique");
      ligneClone.querySelector(".champ-texte").value =
        motsClone.map(m => m.texte).join(" ");
      ligneClone.querySelector(".champ-debut").value =
        Math.min(...motsClone.map(m => m.debut)).toFixed(3);
      ligneClone.querySelector(".champ-fin").value =
        Math.max(...motsClone.map(m => m.fin)).toFixed(3);
      rendererPiste(pisteClone);
    }
    renumeroter();
    rafraichirApercu();
    marquerModifiee();
  }

  function ouvrirMenuPersonnage(e, piste, indexMot) {
    e.preventDefault();
    fermerMenuPersonnage();
    const menu = document.createElement("div");
    menu.className = "menu-personnage";
    nomsPersonnages.forEach((nom, i) => {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "menu-personnage-item";
      item.textContent = nom || ("Personnage " + (i + 1));
      item.onclick = () => {
        fermerMenuPersonnage();
        assignerMotPersonnage(piste, indexMot, i);
      };
      menu.appendChild(item);
    });
    document.body.appendChild(menu);
    menuPersonnage = menu;
    const rect = menu.getBoundingClientRect();
    menu.style.left = Math.max(8, Math.min(e.clientX, window.innerWidth - rect.width - 8)) + "px";
    menu.style.top = Math.max(8, Math.min(e.clientY, window.innerHeight - rect.height - 8)) + "px";
  }

  function ligneReplique(replique, index, nouvelle) {
    const bloc = document.createElement("div");
    bloc.className = "bloc-replique";
    const ligne = document.createElement("div");
    ligne.className = "ligne-replique" + (nouvelle ? " nouvelle" : "");
    if (replique.id != null) ligne.dataset.id = replique.id;
    ligne.innerHTML =
      '<span class="num">' + (index + 1) + "</span>" +
      '<input type="number" class="champ-debut" min="0" step="0.05" value="' +
        Number(replique.debut).toFixed(2) + '" title="début (s)">' +
      '<input type="number" class="champ-fin" min="0" step="0.05" value="' +
        Number(replique.fin).toFixed(2) + '" title="fin (s)">' +
      '<textarea class="champ-texte" rows="1" spellcheck="false"></textarea>' +
      '<select class="champ-voix" title="Voix de cette réplique : détectée automatiquement, ou choix manuel (T94)">' +
        optionsVoix() +
      '</select>' +
      '<div class="actions-replique">' +
        '<button class="bouton-ecoute" title="Écouter cette réplique">▶</button>' +
        '<button class="bouton-suggestions" title="Proposer des corrections">✨</button>' +
        '<button class="bouton-inserer" title="Insérer une réplique après celle-ci">＋</button>' +
        '<button class="bouton-suppr" title="Supprimer cette réplique">🗑</button>' +
        '<button class="bouton-resync" title="Resynchroniser cette réplique sur son audio (fenêtre [début, fin] uniquement)">🔄</button>' +
      '</div>';
    if (replique.personnage != null)
      ligne.querySelector(".champ-voix").value =
        String(Number(replique.personnage) + 1);
    ligne.querySelector(".champ-voix").addEventListener("change", marquerModifiee);
    ligne.querySelector(".bouton-resync").onclick = () => resynchroniserReplique(bloc);
    ligne.querySelector(".champ-texte").value = replique.texte;
    ligne.querySelector(".bouton-inserer").onclick = () => insererLigneReplique(bloc);
    ligne.querySelector(".bouton-suppr").onclick = () => {
      bloc.remove();
      renumeroter();
      rafraichirApercu();
      marquerModifiee();
    };
    ligne.querySelector(".bouton-ecoute").onclick = () => {
      const d = parseFloat(ligne.querySelector(".champ-debut").value);
      const f = parseFloat(ligne.querySelector(".champ-fin").value);
      if (!isNaN(d) && !isNaN(f) && f > d) jouerSegment(d, f, ligne);
    };
    ligne.querySelector(".bouton-suggestions").onclick = () => basculerSuggestions(bloc);
    // la timeline suit le texte tapé : ajout, suppression ou refonte d'un mot
    // resynchronise les blocs-mots EN DIRECT (mots inchangés → timings
    // conservés ; mot inséré → silence entre voisins ; mot supprimé → disparaît),
    // comme la resynchronisation serveur après validation.
    ligne.querySelector(".champ-texte").addEventListener("input", () => {
      const piste = bloc.querySelector(".piste-mots");
      if (!piste) return;
      const d0 = parseFloat(ligne.querySelector(".champ-debut").value);
      const d1 = parseFloat(ligne.querySelector(".champ-fin").value);
      if (isFinite(d0) && isFinite(d1) && d1 > d0) {
        piste.mots = RythmoTimeline.resynchroniserMots(
          piste.mots, ligne.querySelector(".champ-texte").value, d0, d1);
      } else {
        // horaires en cours de saisie : on ne recalcule pas la timeline, on
        // renomme juste les mots existants tant que possible
        const tokens = ligne.querySelector(".champ-texte").value
          .split(/\s+/).filter(Boolean);
        piste.mots.forEach((m, i) => { if (tokens[i]) m.texte = tokens[i]; });
      }
      rendererPiste(piste);
      marquerModifiee();
    });
    [".champ-debut", ".champ-fin"].forEach(sel =>
      ligne.querySelector(sel).addEventListener("input", () => {
        rendererPiste(bloc.querySelector(".piste-mots"));
        rafraichirApercu();
        marquerModifiee();
      }));
    const panneauSuggs = document.createElement("div");
    panneauSuggs.className = "suggestions-replique";
    const piste = construirePisteMots(replique);
    bloc.appendChild(ligne);
    bloc.appendChild(panneauSuggs);
    bloc.appendChild(piste);
    creerControlesZoom(piste, ligne);
    rendererPiste(piste);
    return bloc;
  }

  function ajouterLigneReplique(replique, nouvelle) {
    const liste = $("#liste-repliques");
    liste.appendChild(ligneReplique(replique, liste.children.length, !!nouvelle));
  }

  function insererLigneReplique(bloc) {
    // Nouvelle réplique entre « bloc » et sa voisine : début = fin de bloc,
    // fin = début de la voisine (bornée à 1,2 s pour rester courte).
    const ligne = bloc.querySelector(".ligne-replique");
    const suivant = bloc.nextElementSibling;
    const debut = parseFloat(ligne.querySelector(".champ-fin").value) || 0;
    let fin = debut + 1.2;
    if (suivant) {
      const debutSuivant = parseFloat(suivant.querySelector(".champ-debut").value);
      if (!isNaN(debutSuivant) && debutSuivant > debut + 0.05)
        fin = Math.min(fin, debutSuivant);
    }
    const liste = $("#liste-repliques");
    const index = [...liste.children].indexOf(bloc) + 1;
    const nouveau = ligneReplique({ texte: "", debut, fin: Math.max(fin, debut + 0.05) },
                                  index, true);
    if (suivant) liste.insertBefore(nouveau, suivant);
    else liste.appendChild(nouveau);
    renumeroter();
    rafraichirApercu();
    marquerModifiee();
    nouveau.querySelector(".champ-texte").focus();
  }

  function scinderReplique(piste, indexMot) {
    // Coupe la réplique au mot indexMot : ce mot et les suivants (le nouveau
    // personnage qui prend la parole) partent dans une nouvelle réplique
    // insérée juste après, avec leurs timings d'origine — la synchronisation
    // mot-à-mot est conservée à l'identique (ni rescale, ni redistribution).
    // La fenêtre de la réplique source suit son dernier mot restant ; le
    // comédien choisit ensuite la voix du nouveau personnage au sélecteur.
    const bloc = piste.closest(".bloc-replique");
    if (!bloc) return;
    const ligne = bloc.querySelector(".ligne-replique");
    const mots = piste.mots;
    if (!mots || mots.length < 2 || indexMot <= 0) return;
    const restants = mots.slice(0, indexMot);
    const deplaces = mots.slice(indexMot);
    piste.mots = restants;
    ligne.querySelector(".champ-texte").value =
      restants.map(m => m.texte).join(" ");
    ligne.querySelector(".champ-fin").value =
      restants[restants.length - 1].fin.toFixed(3);
    const liste = $("#liste-repliques");
    const suivant = bloc.nextElementSibling;
    const index = [...liste.children].indexOf(bloc) + 1;
    const nouveau = ligneReplique({
      texte: deplaces.map(m => m.texte).join(" "),
      debut: deplaces[0].debut,
      fin: deplaces[deplaces.length - 1].fin,
      mots: deplaces,
    }, index, true);
    if (suivant) liste.insertBefore(nouveau, suivant);
    else liste.appendChild(nouveau);
    renumeroter();
    rendererPiste(piste);
    rendererPiste(nouveau.querySelector(".piste-mots"));
    rafraichirApercu();
    marquerModifiee();
  }

  function deplacerMotsVersReplique(piste, indexMot, sens) {
    // Déplace une TRANche de mots vers la réplique voisine EXISTANTE, timings
    // d'origine conservés à l'identique (sync mot-à-mot) :
    //   sens -1 (⏮) : ce mot et les précédents → réplique précédente ;
    //   sens +1 (⏭) : ce mot et les suivants → réplique suivante.
    // La fenêtre de chaque réplique suit son premier/dernier mot, donc aucun
    // chevauchement possible (la tranche déplacée est toujours un préfixe ou
    // un suffixe de la source).
    const bloc = piste.closest(".bloc-replique");
    if (!bloc) return;
    const ligne = bloc.querySelector(".ligne-replique");
    const voisin = sens < 0 ? bloc.previousElementSibling : bloc.nextElementSibling;
    if (!voisin) return;
    const cible = voisin.querySelector(".piste-mots");
    const mots = piste.mots;
    if (!mots || !mots.length || !cible) return;
    let deplaces, restants;
    if (sens < 0) {
      if (indexMot >= mots.length - 1) return;  // la source resterait vide
      deplaces = mots.slice(0, indexMot + 1);
      restants = mots.slice(indexMot + 1);
    } else {
      if (indexMot <= 0) return;  // la source resterait vide
      deplaces = mots.slice(indexMot);
      restants = mots.slice(0, indexMot);
    }
    // la source ne garde que ses mots restants : fenêtre suivie, texte joint
    piste.mots = restants;
    ligne.querySelector(".champ-texte").value =
      restants.map(m => m.texte).join(" ");
    if (sens < 0)
      ligne.querySelector(".champ-debut").value = restants[0].debut.toFixed(3);
    else
      ligne.querySelector(".champ-fin").value =
        restants[restants.length - 1].fin.toFixed(3);
    // la cible accueille la tranche (ordre chronologique conservé)
    cible.mots = sens < 0 ? cible.mots.concat(deplaces) : deplaces.concat(cible.mots);
    const ligneCible = voisin.querySelector(".ligne-replique");
    ligneCible.querySelector(".champ-texte").value =
      cible.mots.map(m => m.texte).join(" ");
    if (sens < 0)
      ligneCible.querySelector(".champ-fin").value =
        deplaces[deplaces.length - 1].fin.toFixed(3);
    else
      ligneCible.querySelector(".champ-debut").value = deplaces[0].debut.toFixed(3);
    rendererPiste(piste);
    rendererPiste(cible);
    rafraichirApercu();
    marquerModifiee();
  }

  function renumeroter() {
    $("#liste-repliques").querySelectorAll(".bloc-replique")
      .forEach((b, i) => { b.querySelector(".num").textContent = i + 1; });
  }

  function ouvrirEditeur(donnees) {
    dureeVideoJob = donnees.duree_video || 0;
    initialiserPersonnages(donnees);
    chargerGlossaire();
    $("#liste-repliques").innerHTML = "";
    (donnees.repliques || []).forEach(r => ajouterLigneReplique(r, false));
    $("#erreurs-edition").style.display = "none";
    $("#editeur").style.display = "block";
    editionModifiee = false;
    majEtatEdition();
    progression(78, "en attente de vos corrections…");
    rendererToutesLesPistes();
    rafraichirApercu();
    activerBandeLecteur(donnees);  // la bande d'en-tête suit les répliques du job
    $("#editeur").scrollIntoView({ behavior: "smooth" });
  }

  function collecterReplique(bloc) {
    const lg = bloc.querySelector(".ligne-replique");
    const r = {
      texte: lg.querySelector(".champ-texte").value,
      debut: parseFloat(lg.querySelector(".champ-debut").value),
      fin: parseFloat(lg.querySelector(".champ-fin").value),
    };
    if (lg.dataset.id != null) r.id = lg.dataset.id;
    // voix choisie au sélecteur : nombre, ou null explicite pour la retirer
    const choixVoix = lg.querySelector(".champ-voix");
    if (choixVoix) r.personnage = choixVoix.value === "" ? null
                                                         : Number(choixVoix.value) - 1;
    const piste = bloc.querySelector(".piste-mots");
    if (piste && piste.mots && piste.mots.length)
      r.mots = piste.mots.map(m => ({
        texte: m.texte,
        debut: +m.debut.toFixed(3),
        fin: +m.fin.toFixed(3),
        ...(m.incertain ? { incertain: true } : {}),
      }));
    return r;
  }

  function collecterRepliques() {
    const sortie = [];
    $("#liste-repliques").querySelectorAll(".bloc-replique").forEach(bloc => {
      sortie.push(collecterReplique(bloc));
    });
    return sortie;
  }

  function validerLocal(repliques, options) {
    // Pour un FICHIER (sauvegarde/chargement .json, import/export .srt), on ne
    // bloque que les répliques structurellement malformées : texte vide,
    // horaires illisibles, début < 0, fin ≤ début. Les contraintes de RENDU —
    // chevauchement entre répliques, fin au-delà de la vidéo — ne sont
    // vérifiées que pour le PUT (le serveur les refuserait de toute façon) :
    // on doit pouvoir archiver ou échanger un travail en cours.
    const pourFichier = !!(options && options.pourFichier);
    const erreurs = [];
    if (!repliques.length) erreurs.push("Il faut au moins une réplique.");
    repliques.forEach((r, i) => {
      const n = i + 1;
      if (!r.texte || !r.texte.trim()) erreurs.push("Réplique " + n + " : texte vide.");
      if (isNaN(r.debut) || isNaN(r.fin))
        erreurs.push("Réplique " + n + " : horaires illisibles.");
      else {
        if (r.debut < 0) erreurs.push("Réplique " + n + " : début négatif.");
        if (r.fin <= r.debut)
          erreurs.push("Réplique " + n + " : le début doit être avant la fin.");
        if (!pourFichier && dureeVideoJob && r.fin > dureeVideoJob + 0.6)
          erreurs.push("Réplique " + n + " : fin au-delà de la vidéo (" +
                       dureeVideoJob.toFixed(1) + " s).");
      }
      if (!pourFichier && i > 0) {
        const p = repliques[i - 1];
        // parole simultanée (T127) : deux voix DIFFÉRENTES peuvent se chevaucher
        const voixDifferentes = p.personnage != null && r.personnage != null &&
                                p.personnage !== r.personnage;
        if (!voixDifferentes && !isNaN(p.fin) && !isNaN(r.debut) &&
            r.debut < p.fin - 0.05)
          erreurs.push("Répliques " + i + " et " + n + " : elles se chevauchent.");
      }
    });
    return erreurs;
  }

  function afficherErreursEdition(erreurs) {
    const bloc = $("#erreurs-edition");
    bloc.textContent = erreurs.join("\n");
    bloc.style.display = erreurs.length ? "block" : "none";
  }

  async function envoyerRepliques() {
    const repliques = collecterRepliques();
    const erreurs = validerLocal(repliques);
    afficherErreursEdition(erreurs);
    if (erreurs.length) return;
    $("#bouton-valider-edition").disabled = true;
    let rep;
    try {
      rep = await fetch("/api/jobs/" + jobCourant + "/repliques", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repliques, personnages: collecterPersonnages() }),
      });
    } catch (err) {
      afficherErreursEdition(["serveur injoignable : " + err.message]);
      $("#bouton-valider-edition").disabled = false;
      return;
    }
    $("#bouton-valider-edition").disabled = false;
    if (rep.status === 202) {
      editionModifiee = false;
      majEtatEdition();
      $("#editeur").style.display = "none";
      $("#progress").style.display = "block";
      progression(80, "répliques validées : rendu en cours…");
      // le flux précédent est clos (après `termine`) ou obsolète : on en ouvre un neuf
      if (sourceCourante) { sourceCourante.close(); sourceCourante = null; }
      suivre(jobCourant);
      return;
    }
    const d = await rep.json().catch(() => ({}));
    const detail = d.detail || {};
    if (detail.code === "E005") {
      afficherErreursEdition([detail.message_utilisateur || detail.message]);
    } else {
      afficherErreursEdition([detail.message || ("refus du serveur (HTTP " + rep.status + ")")]);
    }
  }

  $("#bouton-valider-edition").addEventListener("click", envoyerRepliques);
  $("#bouton-ajouter-glossaire").addEventListener("click", ajouterTermeGlossaire);
  $("#bouton-enregistrer-glossaire").addEventListener("click", enregistrerGlossaire);
  $("#glossaire-terme").addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); ajouterTermeGlossaire(); }
  });
  $("#bouton-importer-wikipedia").addEventListener("click", importerWikipedia);
  $("#bouton-ajouter-selection").addEventListener("click", ajouterSelectionWikipedia);
  $("#bouton-reessayer-wikipedia").addEventListener("click", reessayerAvecWikipedia);
  $("#wikipedia-titre").addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); importerWikipedia(); }
  });

  async function traduireRepliques() {
    // Slice 1 : une passe de traduction locale, couche séparée (traduction.json).
    // L'original n'est jamais touché — on envoie uniquement langue source/cible.
    // Slice 2 : url/cle_api/modele_api configurent le moteur « API compatible OpenAI ».
    if (!jobCourant) return;
    const source = $("#traduction-langue-source").value || "en";
    const cible = $("#traduction-langue-cible").value || "fr";
    const modele = $("#traduction-modele").value || "deterministe";
    const temperature = parseFloat($("#traduction-temperature").value);
    const bouton = $("#bouton-traduire");
    const etat = $("#etat-traduction");
    bouton.disabled = true;
    etat.textContent = "traduction en cours…";
    try {
      const rep = await fetch("/api/jobs/" + jobCourant + "/traductions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.assign(
          { langue_source: source, langue_cible: cible, modele: modele,
            temperature: isNaN(temperature) ? undefined : temperature },
          configMoteurTraduction())),
      });
      if (!rep.ok) {
        const d = await rep.json().catch(() => ({}));
        const detail = d.detail || {};
        etat.textContent = detail.message ||
          ("refus du serveur (HTTP " + rep.status + ")");
        return;
      }
      // la passe est asynchrone côté serveur : on suit la progression X/Y
      for (let i = 0; i < 2000; i++) {
        await new Promise(res => setTimeout(res, 100));
        const lecture = await fetch("/api/jobs/" + jobCourant + "/traductions");
        if (!lecture.ok) break;
        const couche = await lecture.json();
        const prog = couche.progression || {};
        etat.textContent = "traduction… " + (prog.fait || 0) + "/" +
          (prog.total || 0) + (prog.statut === "en_pause" ? " (en pause)" : "");
        if (prog.statut === "termine") {
          afficherTraductions(couche);
          etat.textContent = "traduction terminée · " + (prog.fait || 0) +
            "/" + (prog.total || 0);
          break;
        }
        if (prog.statut === "annule") {
          etat.textContent = "traduction annulée";
          break;
        }
        if (prog.statut === "erreur") {
          etat.textContent = "erreur de traduction";
          break;
        }
      }
    } catch (err) {
      etat.textContent = "serveur injoignable : " + err.message;
    } finally {
      bouton.disabled = false;
    }
  }

  function afficherTraductions(couche) {
    // Slice 7 : comparaison source/cible, édition manuelle, verrouillage,
    // exclusion, retraduction et explication des scores — l'original (texte
    // source éditable) n'est jamais modifié.
    const entrees = (couche && couche.entrees) || {};
    $("#liste-repliques").querySelectorAll(".bloc-replique").forEach(bloc => {
      const ligne = bloc.querySelector(".ligne-replique");
      const rid = ligne && ligne.dataset.id;
      let zone = bloc.querySelector(".traduction-replique");
      if (!zone) {
        zone = document.createElement("div");
        zone.className = "traduction-replique";
        bloc.appendChild(zone);
      }
      const e = rid != null ? entrees[rid] : null;
      if (!e) { zone.innerHTML = ""; return; }
      if (e.statut === "erreur") {
        zone.innerHTML = "⚠ traduction impossible : " + echapperHtml(e.erreur || "erreur");
        return;
      }
      if (e.exclue) {
        zone.innerHTML = '<span class="traduction-exclue">✖ exclue de la traduction</span>';
        return;
      }
      const explications = (e.explications || []).map(echapperHtml).join(" · ");
      zone.innerHTML =
        '<div class="traduction-ligne">' +
          '<span class="traduction-source">' + echapperHtml(e.source_text) + '</span>' +
          '<span class="traduction-fleche">→</span>' +
          '<input class="traduction-champ-cible" spellcheck="false" ' +
            'title="Traduction modifiable — l\'original reste intact" value="' +
            echapperHtml(e.target_text) + '">' +
        '</div>' +
        '<div class="traduction-actions">' +
          '<button class="traduction-verrou" title="Verrouiller : jamais retouchée">' +
            (e.verrouillee ? "🔒" : "🔓") + '</button>' +
          '<button class="traduction-exclure" title="Exclure de la traduction">✖ exclure</button>' +
          '<button class="traduction-retraduire" title="Retraduire cette réplique">🔄</button>' +
        '</div>' +
        '<div class="traduction-scores">score ' + Math.round(e.score_global) +
          ' · ' + e.source_syllabes + '→' + e.target_syllabes + ' syllabes' +
          (explications ? '<span class="traduction-explications"> · ' +
                          explications + '</span>' : '') +
        '</div>';
      zone.querySelector(".traduction-champ-cible").addEventListener("change", ev => {
        editerTraduction(rid, { target_text: ev.target.value });
      });
      zone.querySelector(".traduction-verrou").onclick = () =>
        verrouillerTraduction(rid, !e.verrouillee);
      zone.querySelector(".traduction-exclure").onclick = () =>
        exclureTraduction(rid, true);
      zone.querySelector(".traduction-retraduire").onclick = () =>
        retraduireReplique(rid);
    });
  }

  async function editerTraduction(rid, corps) {
    // Édition manuelle d'une entrée de traduction (cible, candidat…).
    if (!jobCourant) return;
    try {
      await fetch("/api/jobs/" + jobCourant + "/traductions/" + rid, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(corps),
      });
    } catch (err) { /* l'état est rechargé à la prochaine lecture */ }
  }

  async function verrouillerTraduction(rid, verrou) {
    if (!jobCourant) return;
    try {
      await fetch("/api/jobs/" + jobCourant + "/traductions/" + rid, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ verrouillee: verrou }),
      });
    } catch (err) { return; }
    rechargerTraductions();
  }

  async function exclureTraduction(rid, exclu) {
    if (!jobCourant) return;
    try {
      await fetch("/api/jobs/" + jobCourant + "/traductions/" + rid, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ exclue: exclu }),
      });
    } catch (err) { return; }
    rechargerTraductions();
  }

  async function retraduireReplique(rid) {
    // Retraduction ciblée (slice 7) : la réplique repart au moteur.
    if (!jobCourant) return;
    const etat = $("#etat-traduction");
    try {
      const rep = await fetch("/api/jobs/" + jobCourant + "/traductions/retraduire", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.assign({ repliques: [rid] },
                                           configMoteurTraduction())),
      });
      if (rep.ok) afficherTraductions(await rep.json());
      else etat.textContent = "retraduction refusée (HTTP " + rep.status + ")";
    } catch (err) {
      etat.textContent = "serveur injoignable : " + err.message;
    }
  }

  async function rechargerTraductions() {
    if (!jobCourant) return;
    const rep = await fetch("/api/jobs/" + jobCourant + "/traductions");
    if (rep.ok) afficherTraductions(await rep.json());
  }

  $("#bouton-traduire").addEventListener("click", traduireRepliques);

  // ——— config du moteur distant (slice 2) : champs URL/clé visibles
  // uniquement pour « API compatible OpenAI ». La config n'est jamais envoyée
  // au serveur pour être STOCKÉE : elle est seulement mémorisée côté navigateur
  // pour pré-remplir les champs entre deux imports/traductions. ———
  function configMoteurTraduction() {
    const v = id => { const el = $(id); return el ? el.value.trim() : ""; };
    const url = v("#traduction-url"), cle = v("#traduction-cle-api"),
          modele = v("#traduction-modele-api");
    return { url: url || undefined, cle_api: cle || undefined,
             modele_api: modele || undefined };
  }

  // ——— mémorisation du dernier moteur distant (URL + modèle + clé) ———
  const CLE_CONFIG_MOTEUR = "rythmo.config-moteur";

  function restaurerConfigMoteur() {
    let stocke = null;
    try { stocke = JSON.parse(localStorage.getItem(CLE_CONFIG_MOTEUR) || "null"); }
    catch (_) { stocke = null; }
    if (!stocke || typeof stocke !== "object") return;
    const remplir = (id, valeur) => {
      const el = $(id);
      if (el && typeof valeur === "string" && valeur.trim()) el.value = valeur;
    };
    remplir("#traduction-url", stocke.url);
    remplir("#traduction-cle-api", stocke.cle_api);
    remplir("#traduction-modele-api", stocke.modele_api);
  }

  function sauvegarderConfigMoteur() {
    const config = configMoteurTraduction();
    try {
      localStorage.setItem(CLE_CONFIG_MOTEUR, JSON.stringify({
        url: config.url || "",
        cle_api: config.cle_api || "",
        modele_api: config.modele_api || "",
      }));
    } catch (_) { /* stockage indisponible (navigateur restreint) : silencieux */ }
  }

  ["#traduction-url", "#traduction-cle-api", "#traduction-modele-api"].forEach(id => {
    const el = $(id);
    if (el) el.addEventListener("input", sauvegarderConfigMoteur);
  });
  restaurerConfigMoteur();

  function majChampsMoteur() {
    const el = $("#config-moteur-ouvert");
    if (!el) return;
    el.hidden = $("#traduction-modele").value !== "openai_compatible";
  }
  $("#traduction-modele").addEventListener("change", majChampsMoteur);
  majChampsMoteur();

  async function commandeTraduction(action) {
    // Pause / reprise / annulation de la passe en cours (slice 2).
    if (!jobCourant) return;
    const etat = $("#etat-traduction");
    try {
      const rep = await fetch("/api/jobs/" + jobCourant + "/traductions/" + action,
                              { method: "POST" });
      if (!rep.ok) {
        const d = await rep.json().catch(() => ({}));
        const detail = d.detail || {};
        etat.textContent = detail.message || ("refus (HTTP " + rep.status + ")");
      }
    } catch (err) {
      etat.textContent = "serveur injoignable : " + err.message;
    }
  }

  $("#bouton-traduction-pause").addEventListener("click", () => commandeTraduction("pause"));
  $("#bouton-traduction-reprendre").addEventListener("click", () => commandeTraduction("reprendre"));
  $("#bouton-traduction-annuler").addEventListener("click", () => commandeTraduction("annuler"));

  async function rendreBandeTraduite() {
    // Slice 9 : rend la bande doublée (timecodes identiques) ; l'original intact.
    if (!jobCourant) return;
    const etat = $("#etat-traduction");
    const bouton = $("#bouton-rendre-traduction");
    bouton.disabled = true;
    etat.textContent = "rendu de la bande traduite…";
    let rep;
    try {
      rep = await fetch("/api/jobs/" + jobCourant + "/traductions/rendre",
                        { method: "POST" });
    } catch (err) {
      bouton.disabled = false;
      etat.textContent = "serveur injoignable : " + err.message;
      return;
    }
    bouton.disabled = false;
    if (rep.status === 202) {
      editionModifiee = false;
      majEtatEdition();
      $("#editeur").style.display = "none";
      $("#progress").style.display = "block";
      progression(80, "rendu de la bande traduite en cours…");
      if (sourceCourante) { sourceCourante.close(); sourceCourante = null; }
      suivre(jobCourant);
      return;
    }
    const d = await rep.json().catch(() => ({}));
    const detail = d.detail || {};
    etat.textContent = detail.message || ("refus du serveur (HTTP " + rep.status + ")");
  }

  function exporterSrtTraduit() {
    // Slice 9 : sous-titres traduits (texte cible + horodatages d'origine).
    if (!jobCourant) return;
    const lien = document.createElement("a");
    lien.href = "/api/jobs/" + jobCourant + "/traductions/srt?m=" + Date.now();
    lien.download = "rythmo_" + jobCourant + "_traduit.srt";
    lien.click();
  }

  $("#bouton-rendre-traduction").addEventListener("click", rendreBandeTraduite);
  $("#bouton-srt-traduction").addEventListener("click", exporterSrtTraduit);

  function formaterTempsSRT(s) {
    const ms = Math.max(0, Math.round(Number(s) * 1000));
    const h = Math.floor(ms / 3600000),
          m = Math.floor((ms % 3600000) / 60000),
          sec = Math.floor((ms % 60000) / 1000),
          mill = ms % 1000;
    return [h, m, sec].map(x => String(x).padStart(2, "0")).join(":") +
           "," + String(mill).padStart(3, "0");
  }

  function exporterSrt() {
    const repliques = collecterRepliques();
    // on exporte même avec des chevauchements ou une fin au-delà de la vidéo :
    // un .srt reste lisible par n'importe quel lecteur
    const erreurs = validerLocal(repliques, { pourFichier: true });
    afficherErreursEdition(erreurs);
    if (erreurs.length) return;
    const blocs = repliques.map((r, i) =>
      (i + 1) + "\n" + formaterTempsSRT(r.debut) + " --> " + formaterTempsSRT(r.fin) +
      "\n" + String(r.texte).trim());
    const blob = new Blob([blocs.join("\n\n") + "\n"],
                          { type: "application/x-subrip" });
    const lien = document.createElement("a");
    lien.href = URL.createObjectURL(blob);
    lien.download = "rythmo_" + (jobCourant || "brouillon") + ".srt";
    lien.click();
    URL.revokeObjectURL(lien.href);
  }

  async function relancerSynchronisation() {
    if (!jobCourant) return;
    const repliques = collecterRepliques();
    // on peut resynchroniser un brouillon même avec des chevauchements ou une
    // fin hors vidéo : seuls les champs structurels (texte, début < fin) comptent
    const erreurs = validerLocal(repliques, { pourFichier: true });
    afficherErreursEdition(erreurs);
    if (erreurs.length) return;
    const bouton = $("#bouton-synchroniser");
    bouton.disabled = true;
    const libelle = bouton.textContent;
    bouton.textContent = "🔄 Synchronisation…";
    let rep;
    try {
      rep = await fetch("/api/jobs/" + jobCourant + "/synchroniser", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repliques, personnages: collecterPersonnages() }),
      });
    } catch (err) {
      bouton.disabled = false; bouton.textContent = libelle;
      return afficherErreursEdition(["serveur injoignable : " + err.message]);
    }
    bouton.disabled = false; bouton.textContent = libelle;
    if (!rep.ok) {
      const d = await rep.json().catch(() => ({}));
      const detail = d.detail || {};
      return afficherErreursEdition([
        detail.message_utilisateur || detail.message ||
        ("synchronisation refusée (HTTP " + rep.status + ")")]);
    }
    const donnees = await rep.json();
    // les mots renvoyés sont calés sur l'audio : on reconstruit l'éditeur
    const vues = (donnees.repliques || []).map(r => ({
      id: r.id != null ? String(r.id) : undefined,
      texte: String(r.texte ?? ""),
      debut: Number(r.debut), fin: Number(r.fin),
      personnage: r.personnage,
      mots: Array.isArray(r.mots) ? r.mots : undefined,
    }));
    remplacerListeRepliques(vues);
    rendererToutesLesPistes();
  }

  async function resynchroniserReplique(bloc) {
    if (!jobCourant) return;
    const replique = collecterReplique(bloc);
    // même règle que la resynchronisation globale : seuls les champs
    // structurels (texte, début < fin) comptent pour un brouillon
    const erreurs = validerLocal([replique], { pourFichier: true });
    if (erreurs.length) return afficherErreursEdition(erreurs);
    const bouton = bloc.querySelector(".bouton-resync");
    bouton.disabled = true;
    const libelle = bouton.textContent;
    bouton.textContent = "⏳";
    let rep;
    try {
      rep = await fetch("/api/jobs/" + jobCourant + "/synchroniser-replique", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ replique }),
      });
    } catch (err) {
      bouton.disabled = false; bouton.textContent = libelle;
      return afficherErreursEdition(["serveur injoignable : " + err.message]);
    }
    bouton.disabled = false; bouton.textContent = libelle;
    if (!rep.ok) {
      const d = await rep.json().catch(() => ({}));
      const detail = d.detail || {};
      return afficherErreursEdition([
        detail.message_utilisateur || detail.message ||
        ("synchronisation refusée (HTTP " + rep.status + ")")]);
    }
    const donnees = await rep.json();
    const resync = donnees.replique || {};
    const piste = bloc.querySelector(".piste-mots");
    if (piste) {
      piste.mots = RythmoTimeline.vuesMots(resync.mots || []);
      rendererPiste(piste);
    }
    rafraichirApercu();
    marquerModifiee();
  }

  $("#bouton-synchroniser").addEventListener("click", relancerSynchronisation);
  $("#bouton-export-srt").addEventListener("click", exporterSrt);
  $("#bouton-sauvegarde-repliques").addEventListener("click", sauvegarderRepliques);
  $("#bouton-sauvegarde-projet").addEventListener("click", enregistrerProjet);
  $("#bouton-charge-projet").addEventListener("click",
    () => $("#charge-projet-fichier").click());
  $("#charge-projet-fichier").addEventListener("change", e => {
    if (e.target.files.length) chargerProjet(e.target.files[0]);
    e.target.value = "";  // permet de recharger le même fichier ensuite
  });
  $("#bouton-charge-repliques").addEventListener("click",
    () => $("#charge-repliques-fichier").click());
  $("#charge-repliques-fichier").addEventListener("change", e => {
    if (e.target.files.length) chargerRepliques(e.target.files[0]);
    e.target.value = "";  // permet de recharger le même fichier ensuite
  });
  $("#bouton-importer-srt").addEventListener("click",
    () => $("#charge-srt-fichier").click());
  $("#charge-srt-fichier").addEventListener("change", e => {
    if (e.target.files.length) importerSrt(e.target.files[0]);
    e.target.value = "";  // permet de réimporter le même fichier ensuite
  });
  $("#bouton-reediter").addEventListener("click", reediterRepliques);
  $("#bouton-ajouter-replique").addEventListener("click", () => {
    const lignes = $("#liste-repliques").querySelectorAll(".ligne-replique");
    let debut = 0;
    if (lignes.length) {
      debut = parseFloat(lignes[lignes.length - 1].querySelector(".champ-fin").value) || 0;
    }
    ajouterLigneReplique({ texte: "", debut, fin: debut + 1.2 }, true);
    marquerModifiee();
  });
  $("#bouton-annuler-edition").addEventListener("click", async () => {
    if (!jobCourant || !confirm("Abandonner ce job ?")) return;
    try { await fetch("/api/jobs/" + jobCourant + "/cancel", { method: "POST" }); } catch (_) {}
    editionModifiee = false;
    majEtatEdition();
  });

  // nombre de personnages de la scène : on redimensionne la liste des noms
  $("#nb-personnages").addEventListener("input", () => {
    const nb = Math.min(12, Math.max(1, parseInt($("#nb-personnages").value) || 1));
    nomsPersonnages = Array.from({ length: nb },
      (_, i) => nomsPersonnages[i] || ("Personnage " + (i + 1)));
    majChampsNoms();
    marquerModifiee();
  });

  // clic droit sur un mot : attribuer ce mot à un personnage (parole simultanée)
  $("#liste-repliques").addEventListener("contextmenu", e => {
    const blocMot = e.target.closest(".bloc-mot");
    if (!blocMot) return;
    const piste = blocMot.closest(".piste-mots");
    const blocRepl = blocMot.closest(".bloc-replique");
    if (!piste || !piste.mots || !blocRepl || blocRepl.classList.contains("clone"))
      return;  // on n'attribue pas un mot déjà cloné
    const index = [...piste.querySelectorAll(".bloc-mot")].indexOf(blocMot);
    if (index >= 0) ouvrirMenuPersonnage(e, piste, index);
  });
  document.addEventListener("click", fermerMenuPersonnage);
  document.addEventListener("keydown", e => {
    if (e.key === "Escape") fermerMenuPersonnage();
  });

  /* ——————— timeline mot-à-mot : onde + blocs draggables (T60–T66) ——————— */
  // T85–T88 (vidéos longues) : l'onde vient de pics min/max calculés côté
  // serveur (/onde) — jamais le WAV entier (≈ 345 Mo sur 90 min) au navigateur.
  let picsFenetres = {};   // cache des pics par (job | fenêtre | colonnes)

  function chargerPics(jobId, t0, t1, colonnes) {
    const cle = jobId + "|" + Math.round(t0 * 100) + "|" + Math.round(t1 * 100) +
                "|" + colonnes;
    if (picsFenetres[cle]) return picsFenetres[cle];
    const promesse = fetch("/api/jobs/" + jobId + "/onde?colonnes=" + colonnes +
                           "&debut=" + t0.toFixed(3) + "&fin=" + t1.toFixed(3))
      .then(r => r.ok ? r.json() : null)
      .catch(() => null);
    picsFenetres[cle] = promesse;
    return promesse;
  }

  function preparerCanvas(canvas) {
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth || 1, h = canvas.clientHeight || 1;
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx, w, h };
  }

  async function dessinerOnde(canvas, t0, t1, teinte, jobId, colonnes) {
    const { ctx, w, h } = preparerCanvas(canvas);
    ctx.clearRect(0, 0, w, h);
    const milieu = h / 2;
    ctx.strokeStyle = "rgba(255,255,255,.07)";
    ctx.beginPath(); ctx.moveTo(0, milieu); ctx.lineTo(w, milieu); ctx.stroke();
    if (t1 > t0) {
      const donnees = await chargerPics(jobId, t0, t1, colonnes);
      if (donnees && donnees.pics && donnees.pics.length) {
        const pics = donnees.pics;
        ctx.fillStyle = teinte || "rgba(120,180,255,.7)";
        for (let x = 0; x < w; x++) {
          const i = Math.min(pics.length - 1,
                             Math.max(0, Math.round(x / w * (pics.length - 1))));
          const mn = pics[i][0], mx = pics[i][1];
          const y0 = milieu - mx * (milieu - 1);
          const y1 = milieu - mn * (milieu - 1);
          ctx.fillRect(x, y0, 1, Math.max(1, y1 - y0));
        }
      }
    }
    if (t1 - t0 <= 30 && t1 > t0) {  // règle de temps sur les fenêtres courtes
      ctx.font = "9px sans-serif";
      ctx.fillStyle = "rgba(255,255,255,.35)";
      const pasT = ((t1 - t0) / w * 60) >= 8 ? 5 : 0.5;
      for (let t = Math.ceil(t0 / pasT) * pasT; t <= t1; t += pasT) {
        const x = (t - t0) / (t1 - t0) * w;
        ctx.fillRect(x, 0, 1, h);
        ctx.fillText(t.toFixed(1), x + 2, 9);
      }
    }
  }

  async function rafraichirApercu() {
    const canvas = $("#onde-globale");
    if (!canvas) return;
    const duree = dureeVideoJob > 0 ? dureeVideoJob : 0;
    await dessinerOnde(canvas, 0, duree, "rgba(255,255,255,.8)", jobCourant,
                       Math.max(800, Math.round(canvas.clientWidth || 1600)));
    const { ctx, w, h } = preparerCanvas(canvas);
    const teintes = ["rgba(70,140,255,.32)", "rgba(255,120,60,.32)",
                     "rgba(90,200,140,.32)"];
    const blocs = [...$("#liste-repliques").querySelectorAll(".bloc-replique")];
    blocs.forEach((bloc, idx) => {
      const d = parseFloat(bloc.querySelector(".champ-debut").value);
      const f = parseFloat(bloc.querySelector(".champ-fin").value);
      if (isNaN(d) || isNaN(f) || duree <= 0) return;
      const x0 = d / duree * w, x1 = f / duree * w;
      ctx.fillStyle = teintes[idx % teintes.length];
      ctx.fillRect(x0, 0, Math.max(1, x1 - x0), h);
    });
  }

  $("#apercu-timeline").addEventListener("click", e => {
    const canvas = $("#onde-globale");
    if (!canvas || dureeVideoJob <= 0) return;
    const rect = canvas.getBoundingClientRect();
    if (rect.width <= 0) return;
    const t = (e.clientX - rect.left) / rect.width * dureeVideoJob;
    const blocs = [...$("#liste-repliques").querySelectorAll(".bloc-replique")];
    const cible = blocs.find(b => {
      const d = parseFloat(b.querySelector(".champ-debut").value) || 0;
      const f = parseFloat(b.querySelector(".champ-fin").value) || Infinity;
      return t >= d && t <= f;
    }) || blocs[blocs.length - 1];
    if (cible) cible.scrollIntoView({ behavior: "smooth", block: "center" });
  });

  /* Resynchronisation mot-à-mot à la frappe : logique pure déplacée dans
     timeline.js (`RythmoTimeline.resynchroniserMots`, `distribuerUniforme`),
     testée par node:test — le client ne garde ici que le DOM. */

  /* ——————— zoom des timelines par réplique (1×–60×) ———————
     La géométrie (bornes, fenêtre visible, position des mots) est dans
     timeline.js (logique pure, testée par node:test). Ici : état `zoom`/
     `ancrage` PORTÉ PAR CHAQUE piste (zoom indépendant par réplique) et
     contrôles −/N×/+. Zoomer ne modifie jamais début/fin ni les mots :
     c'est un cadrage d'affichage. */
  const PALIERS_ZOOM = [1, 2, 4, 8, 15, 30, 60];

  function ligneDePiste(piste) {
    const bloc = piste.closest(".bloc-replique");
    return bloc ? bloc.querySelector(".ligne-replique") : piste.previousElementSibling;
  }

  function zoomSuivant(z) {
    const c = RythmoTimeline.bornesZoom(z);
    for (const p of PALIERS_ZOOM) if (p > c) return p;
    return RythmoTimeline.ZOOM_MAX;
  }

  function zoomPrecedent(z) {
    const c = RythmoTimeline.bornesZoom(z);
    for (let i = PALIERS_ZOOM.length - 1; i >= 0; i--)
      if (PALIERS_ZOOM[i] < c) return PALIERS_ZOOM[i];
    return RythmoTimeline.ZOOM_MIN;
  }

  function fmtZoom(z) {
    const r = Math.round(z * 10) / 10;
    return (Number.isInteger(r) ? String(r) : r.toFixed(1)) + "×";
  }

  function majControlesZoom(piste) {
    const valeur = piste.__zoomValeur;
    if (valeur) valeur.textContent = fmtZoom(piste.zoom || 1);
    piste.classList.toggle("zoomable", (piste.zoom || 1) > 1);
  }

  function creerControlesZoom(piste, ligne) {
    const actions = ligne.querySelector(".actions-replique");
    if (!actions) return;
    const groupe = document.createElement("div");
    groupe.className = "zoom-replique";
    groupe.title = "Zoom de la timeline de cette réplique (1×–60×) — indépendant des autres répliques";
    const moins = document.createElement("button");
    moins.type = "button";
    moins.className = "zoom-moins";
    moins.textContent = "−";
    moins.title = "Dézoomer";
    moins.onclick = () => {
      piste.zoom = zoomPrecedent(piste.zoom);
      rendererPiste(piste);
    };
    const valeur = document.createElement("button");
    valeur.type = "button";
    valeur.className = "zoom-valeur";
    valeur.title = "Réinitialiser le zoom à 1×";
    valeur.onclick = () => {
      piste.zoom = 1;
      piste.ancrage = 0.5;
      rendererPiste(piste);
    };
    const plus = document.createElement("button");
    plus.type = "button";
    plus.className = "zoom-plus";
    plus.textContent = "+";
    plus.title = "Zoomer";
    plus.onclick = () => {
      piste.zoom = zoomSuivant(piste.zoom);
      rendererPiste(piste);
    };
    groupe.appendChild(moins);
    groupe.appendChild(valeur);
    groupe.appendChild(plus);
    actions.appendChild(groupe);
    piste.__zoomValeur = valeur;
  }

  function construirePisteMots(replique) {
    const piste = document.createElement("div");
    piste.className = "piste-mots";
    const canvas = document.createElement("canvas");
    canvas.className = "onde-piste";
    piste.appendChild(canvas);
    piste.mots = RythmoTimeline.vuesMots(replique.mots || []);
    piste.zoom = 1;       // zoom par réplique, borné 1×–60× (timeline.js)
    piste.ancrage = 0.5;  // 0.5 = fenêtre visible centrée quand zoom > 1
    attacherNavigationPiste(piste);
    return piste;  // rendu différé : la piste n'est pas encore dans le DOM
  }

  function rendererPiste(piste) {
    // la piste n'est pas le voisin immédiat de la ligne (panneau de suggestions
    // entre les deux) : on remonte au bloc pour retrouver la ligne (T74).
    const ligne = ligneDePiste(piste);
    const d0 = parseFloat(ligne.querySelector(".champ-debut").value);
    const d1 = parseFloat(ligne.querySelector(".champ-fin").value);
    piste.zoom = RythmoTimeline.bornesZoom(piste.zoom != null ? piste.zoom : 1);
    if (piste.ancrage == null) piste.ancrage = 0.5;
    // fenêtre visible = fenêtre de la réplique / zoom, ancrée dans [d0, d1]
    const vue = RythmoTimeline.fenetreZoom(d0, d1, piste.zoom, piste.ancrage);
    dessinerOnde(piste.querySelector("canvas"), vue.t0, vue.t1, "rgba(120,180,255,.6)",
                 jobCourant, Math.max(120, Math.round(piste.clientWidth || 600)));
    piste.querySelectorAll(".bloc-mot").forEach(b => b.remove());
    piste.mots.forEach((m, i) => {
      const bloc = document.createElement("div");
      bloc.className = "bloc-mot";
      const marqueur = /^\([^()]{1,60}\)$/.test(String(m.texte).trim());
      if (marqueur) bloc.classList.add("bloc-marqueur");
      if (m.incertain) bloc.classList.add("bloc-incertain");
      const texteMot = document.createElement("span");
      texteMot.className = "texte-mot";
      texteMot.textContent = m.texte;
      bloc.appendChild(texteMot);
      bloc.title = marqueur ? "Symbole de respiration — non prononcé"
                 : m.incertain ? "Mot incertain — probabilité de reconnaissance faible"
                 : "";
      const pos = RythmoTimeline.positionMot(m.debut, m.fin, vue.t0, vue.t1);
      bloc.style.left = pos.left + "%";
      bloc.style.width = Math.max(pos.width, 4) + "%";
      const poigneeGauche = document.createElement("i");
      poigneeGauche.className = "poignee-gauche";
      poigneeGauche.title = "Étirer le début du mot";
      bloc.appendChild(poigneeGauche);
      const poignee = document.createElement("i");
      poignee.className = "poignee-mot";
      poignee.title = "Étirer la fin du mot";
      bloc.appendChild(poignee);
      const scinder = document.createElement("button");
      scinder.className = "bouton-scinder";
      scinder.textContent = "✂";
      scinder.title = "Couper ici : ce mot et les suivants partent dans une nouvelle réplique (sync conservé)";
      scinder.disabled = (i === 0);  // couper au 1er mot laisserait la source vide
      scinder.onclick = () => scinderReplique(piste, i);
      bloc.appendChild(scinder);
      // déplacer une tranche de mots vers la réplique voisine EXISTANTE
      // (⏭ : ce mot et les suivants → suivante ; ⏮ : ce mot et les précédents
      // → précédente), timings conservés — désactivés aux bornes (la source
      // resterait vide) ou sans voisine
      const blocRepl = piste.closest(".bloc-replique");
      const precedente = blocRepl ? blocRepl.previousElementSibling : null;
      const suivante = blocRepl ? blocRepl.nextElementSibling : null;
      const bAvant = document.createElement("button");
      bAvant.className = "bouton-deplacer-avant";
      bAvant.textContent = "⏮";
      bAvant.title = "Ce mot et les précédents → réplique précédente (sync conservée)";
      bAvant.disabled = (i === piste.mots.length - 1 || !precedente);
      bAvant.onclick = () => deplacerMotsVersReplique(piste, i, -1);
      bloc.appendChild(bAvant);
      const bApres = document.createElement("button");
      bApres.className = "bouton-deplacer-apres";
      bApres.textContent = "⏭";
      bApres.title = "Ce mot et les suivants → réplique suivante (sync conservée)";
      bApres.disabled = (i === 0 || !suivante);
      bApres.onclick = () => deplacerMotsVersReplique(piste, i, +1);
      bloc.appendChild(bApres);
      attacherGlisser(piste, bloc, i, poignee, poigneeGauche);
      piste.appendChild(bloc);
    });
    majControlesZoom(piste);
  }

  function rendererToutesLesPistes() {
    $("#liste-repliques").querySelectorAll(".piste-mots")
      .forEach(rendererPiste);
  }

  function attacherGlisser(piste, bloc, i, poignee, poigneeGauche) {
    const ligne = ligneDePiste(piste);
    let mode = null, x0 = 0, deplace = false;
    bloc.addEventListener("pointerdown", e => {
      if (e.target.closest("button")) {
        // clic sur un bouton du mot (✂, ⏮, ⏭) : on laisse le click se
        // déclencher, le drag ne doit ni capturer le pointeur ni re-rendre la
        // piste (le bouton serait détruit avant le click)
        mode = "bouton";
        deplace = false;
        return;
      }
      e.preventDefault();
      deplace = false;
      if (e.target === poignee) mode = "etirer-droite";
      else if (e.target === poigneeGauche) mode = "etirer-gauche";
      else mode = "deplacer";
      x0 = e.clientX;
      bloc.setPointerCapture(e.pointerId);
    });
    bloc.addEventListener("pointermove", e => {
      if (!mode || mode === "bouton") return;
      deplace = true;
      const rect = piste.getBoundingClientRect();
      const d0 = parseFloat(ligne.querySelector(".champ-debut").value);
      const d1 = parseFloat(ligne.querySelector(".champ-fin").value);
      // échelle de la fenêtre VISIBLE (zoom compris) : déplacer de N px déplace
      // le mot du bon nombre de secondes à n'importe quel niveau de zoom
      const vue = RythmoTimeline.fenetreZoom(d0, d1, piste.zoom, piste.ancrage);
      const secParPx = RythmoTimeline.echelle(vue.t0, vue.t1, rect.width);
      const delta = (e.clientX - x0) * secParPx;
      const m = piste.mots[i];
      const duree = m.fin - m.debut;
      if (mode === "deplacer") {
        const min = i > 0 ? piste.mots[i - 1].fin : d0;
        const max = i + 1 < piste.mots.length ? piste.mots[i + 1].debut - duree
                                              : d1 - duree;
        m.debut = Math.min(Math.max(m.debut + delta, min), max);
        m.fin = m.debut + duree;
        // ni le premier ni le dernier mot ne déplacent la fenêtre pendant le
        // geste (sinon le mot « recolle » au bord et cette bordure devient
        // inutilisable) : la fenêtre ne suit qu'à la relâche, vers l'extérieur.
      } else if (mode === "etirer-droite") {
        const min = m.debut + 0.04;
        let max;
        if (i + 1 < piste.mots.length) {
          max = piste.mots[i + 1].debut;  // jamais au-delà du mot suivant
        } else {
          // dernier mot : on peut l'étirer jusqu'au début de la réplique
          // suivante (jamais de chevauchement) ou à la fin de la vidéo ; la
          // fenêtre reste stable pendant le geste (sinon le mot « glisse »
          // vers la fin et la fin de la réplique devient inutilisable)
          const blocRepl = ligne.closest(".bloc-replique");
          const suivante = blocRepl && blocRepl.nextElementSibling;
          if (suivante) {
            const dSuiv = parseFloat(suivante.querySelector(".champ-debut").value);
            max = (!isNaN(dSuiv) && dSuiv > m.debut + 0.05)
                  ? dSuiv - 0.05 : (dureeVideoJob > 0 ? dureeVideoJob : d1 + 600);
          } else {
            max = dureeVideoJob > 0 ? dureeVideoJob : d1 + 600;
          }
        }
        m.fin = Math.min(Math.max(m.fin + delta, min), max);
      } else {  // etirer-gauche
        const max = m.fin - 0.04;  // durée minimale conservée
        let min;
        if (i > 0) {
          min = piste.mots[i - 1].fin;  // jamais avant le mot précédent
        } else {
          // premier mot : on peut l'étirer vers la gauche jusqu'à la fin de la
          // réplique précédente (jamais de chevauchement) ou à 0 ; la fenêtre
          // reste stable pendant le geste (sinon le mot « recolle » au bord
          // gauche et le début de la réplique devient inutilisable)
          const blocRepl = ligne.closest(".bloc-replique");
          const precedente = blocRepl && blocRepl.previousElementSibling;
          if (precedente) {
            const fPrev = parseFloat(precedente.querySelector(".champ-fin").value);
            min = (!isNaN(fPrev) && fPrev < m.fin - 0.05) ? fPrev + 0.05 : 0;
          } else {
            min = 0;
          }
        }
        m.debut = Math.min(Math.max(m.debut + delta, min), max);
      }
      const pos = RythmoTimeline.positionMot(m.debut, m.fin, vue.t0, vue.t1);
      bloc.style.left = pos.left + "%";
      bloc.style.width = Math.max(pos.width, 4) + "%";
      x0 = e.clientX;
    });
    const relacher = () => {
      if (mode === "bouton") { mode = null; return; }  // le clic du bouton se gère lui-même
      const simpleClic = mode !== null && !deplace;  // clic sans glisser : écouter le mot
      mode = null;
      if (piste.mots.length) {
        // la fenêtre ne suit le dernier mot QUE vers l'extérieur : étendre le
        // mot allonge la réplique, le raccourcir laisse un espace travaillable
        // à la fin (bug signalé : la fenêtre « recollait » au mot)
        if (i === piste.mots.length - 1) {
          const d1 = parseFloat(ligne.querySelector(".champ-fin").value);
          const finMot = piste.mots[i].fin;
          if (finMot > d1) ligne.querySelector(".champ-fin").value = finMot.toFixed(3);
        }
        // symétrique pour le PREMIER mot : la fenêtre ne suit que vers
        // l'extérieur gauche (étirer le mot allonge la réplique vers l'avant,
        // le raccourcir laisse un espace travaillable au début)
        if (i === 0) {
          const d0 = parseFloat(ligne.querySelector(".champ-debut").value);
          const debutMot = piste.mots[i].debut;
          if (debutMot < d0) ligne.querySelector(".champ-debut").value = debutMot.toFixed(3);
        }
      }
      rendererPiste(piste);
      rafraichirApercu();
      if (simpleClic) {
        const m = piste.mots[i];
        jouerSegment(m.debut, m.fin, piste.querySelectorAll(".bloc-mot")[i]);
      }
    };
    bloc.addEventListener("pointerup", relacher);
    bloc.addEventListener("pointercancel", relacher);
  }

  function attacherNavigationPiste(piste) {
    // Pan : glisser le FOND VIDE de la piste (ni un mot, ni un bouton) quand
    // zoom > 1. La fenêtre visible reste bornée à la réplique (decalerAncrage).
    let pan = null;
    piste.addEventListener("pointerdown", e => {
      if (e.target.closest(".bloc-mot")) return;
      if ((piste.zoom || 1) <= 1) return;
      e.preventDefault();
      const ligne = ligneDePiste(piste);
      const d0 = parseFloat(ligne.querySelector(".champ-debut").value);
      const d1 = parseFloat(ligne.querySelector(".champ-fin").value);
      const vue = RythmoTimeline.fenetreZoom(d0, d1, piste.zoom, piste.ancrage);
      const rect = piste.getBoundingClientRect();
      pan = {
        x0: e.clientX,
        ancrage0: piste.ancrage,
        duree: Math.max(d1 - d0, 0.001),
        secParPx: RythmoTimeline.echelle(vue.t0, vue.t1, rect.width),
        zoom: piste.zoom,
      };
      piste.setPointerCapture(e.pointerId);
    });
    piste.addEventListener("pointermove", e => {
      if (!pan) return;
      const deltaSec = (e.clientX - pan.x0) * pan.secParPx;
      piste.ancrage = RythmoTimeline.decalerAncrage(
        pan.ancrage0, deltaSec, pan.duree, pan.zoom);
      rendererPiste(piste);
    });
    const finPan = () => { pan = null; };
    piste.addEventListener("pointerup", finPan);
    piste.addEventListener("pointercancel", finPan);

    // Molette : Ctrl = zoom autour du curseur ; sinon pan horizontal si zoomé
    // (à 1×, la molette laisse la page défiler normalement).
    piste.addEventListener("wheel", e => {
      if (e.ctrlKey) {
        e.preventDefault();
        const rect = piste.getBoundingClientRect();
        const f = rect.width > 0
          ? Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width))
          : 0.5;
        const ligne = ligneDePiste(piste);
        const d0 = parseFloat(ligne.querySelector(".champ-debut").value);
        const d1 = parseFloat(ligne.querySelector(".champ-fin").value);
        const duree = Math.max(d1 - d0, 0.001);
        const pivot = RythmoTimeline.pivoter(
          f, duree, piste.zoom, piste.ancrage, e.deltaY < 0 ? 2 : 0.5);
        piste.zoom = pivot.zoom;
        piste.ancrage = pivot.ancrage;
        rendererPiste(piste);
        return;
      }
      if ((piste.zoom || 1) <= 1) return;
      e.preventDefault();
      const ligne = ligneDePiste(piste);
      const d0 = parseFloat(ligne.querySelector(".champ-debut").value);
      const d1 = parseFloat(ligne.querySelector(".champ-fin").value);
      const duree = Math.max(d1 - d0, 0.001);
      const vue = RythmoTimeline.fenetreZoom(d0, d1, piste.zoom, piste.ancrage);
      const rect = piste.getBoundingClientRect();
      const secParPx = RythmoTimeline.echelle(vue.t0, vue.t1, rect.width);
      const deltaSec = (e.deltaX || e.deltaY || 0) * secParPx;
      piste.ancrage = RythmoTimeline.decalerAncrage(
        piste.ancrage, deltaSec, duree, piste.zoom);
      rendererPiste(piste);
    }, { passive: false });
  }

  /* ——————— suggestions de correction FR (T71–T75) ——————— */
  async function rafraichirSuggestions(bloc) {
    const panneau = bloc.querySelector(".suggestions-replique");
    const corps = { repliques: collecterRepliques().map(r => ({ id: r.id,
                                                                texte: r.texte })) };
    try {
      const rep = await fetch("/api/jobs/" + jobCourant + "/suggestions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(corps),
      });
      if (!rep.ok) return;
      const donnees = await rep.json();
      const blocs = [...$("#liste-repliques").querySelectorAll(".bloc-replique")];
      const idx = blocs.indexOf(bloc);
      const entree = (donnees.repliques || [])[idx];
      const suggestions = entree ? (entree.suggestions || []) : [];
      panneau.innerHTML = "";
      if (!suggestions.length) {
        const vide = document.createElement("span");
        vide.className = "suggestion-vide";
        vide.textContent = "Aucune correction proposée";
        panneau.appendChild(vide);
        return;
      }
      suggestions.forEach(s => {
        const chip = document.createElement("button");
        chip.className = "suggestion";
        chip.textContent = "« " + s.avant + " » → « " + s.apres + " »";
        chip.title = s.message;
        chip.onclick = () => {
          const zone = bloc.querySelector(".champ-texte");
          if (zone.value.slice(s.debut, s.fin) !== s.avant) return;  // texte dépassé
          zone.value = zone.value.slice(0, s.debut) + s.apres + zone.value.slice(s.fin);
          zone.dispatchEvent(new Event("input"));
          marquerModifiee();
          rafraichirSuggestions(bloc);  // re-calcule sur le texte corrigé
        };
        panneau.appendChild(chip);
      });
    } catch (_) { /* hors ligne : pas de suggestions */ }
  }

  function basculerSuggestions(bloc) {
    const panneau = bloc.querySelector(".suggestions-replique");
    if (panneau.dataset.ouvert === "1") {
      panneau.dataset.ouvert = "0";
      panneau.innerHTML = "";
      return;
    }
    panneau.dataset.ouvert = "1";
    rafraichirSuggestions(bloc);
  }

  /* ——————— écoute des segments (T67–T70) ——————— */
  let ctxAudio = null;      // AudioContext partagé (créé dans le geste utilisateur)
  let sourceLecture = null; // AudioBufferSourceNode en cours
  let blocEnLecture = null;
  window.__rythmoLecture = { enCours: false, debut: 0, fin: 0 };

  function arreterLecture() {
    if (sourceLecture) {
      try { sourceLecture.onended = null; sourceLecture.stop(); } catch (_) {}
      sourceLecture = null;
    }
    if (blocEnLecture) {
      blocEnLecture.classList.remove("en-lecture");
      blocEnLecture = null;
    }
    window.__rythmoLecture.enCours = false;
  }

  async function jouerSegment(debut, fin, cible) {
    if (lecteurBande) pauseBandeLecteur();  // écoute d'un mot : on coupe la bande
    arreterLecture();
    try {
      if (!ctxAudio)
        ctxAudio = new (window.AudioContext || window.webkitAudioContext)();
      if (ctxAudio.state === "suspended") ctxAudio.resume().catch(() => {});
      const rep = await fetch("/api/jobs/" + jobCourant + "/audio?debut=" +
                              debut.toFixed(3) + "&fin=" + fin.toFixed(3));
      if (!rep.ok) return;
      const brut = await rep.arrayBuffer();
      const audio = await ctxAudio.decodeAudioData(brut);
      const source = ctxAudio.createBufferSource();
      source.buffer = audio;
      source.connect(ctxAudio.destination);
      sourceLecture = source;
      if (cible) { cible.classList.add("en-lecture"); blocEnLecture = cible; }
      window.__rythmoLecture = { enCours: true, debut: +debut.toFixed(3),
                                 fin: +fin.toFixed(3) };
      source.onended = () => arreterLecture();
      source.start(0);
    } catch (_) {
      arreterLecture();
    }
  }

  /* ——————— mini-lecteur : piste temporelle rigide + audio réel ———————
     La bande d'en-tête devient un vrai lecteur : les mots du job sont posés
     sur une piste temporelle rigide (même loi que le rendu serveur :
     s = v·début, anti-chevauchement) et défilent au rythme de l'AUDIO —
     lecture/pause, timecode, clic pour se déplacer. Jamais le WAV entier :
     des tranches de 20 s via /audio (comme l'écoute d'un mot, T67). */
  const cacheLargeurs = new Map();   // largeur d'un mot déjà mesuré
  function largeurMot(texte) {
    if (cacheLargeurs.has(texte)) return cacheLargeurs.get(texte);
    let jauge = document.querySelector("#jauge-bande");
    if (!jauge) {
      jauge = document.createElement("span");
      jauge.id = "jauge-bande";
      jauge.className = "bande-mot";
      jauge.style.cssText = "position:fixed;left:-9999px;top:0;visibility:hidden;" +
        "white-space:nowrap;font-size:1.35rem;padding:6px 12px";
      document.body.appendChild(jauge);
    }
    jauge.textContent = texte;
    const w = jauge.offsetWidth;
    cacheLargeurs.set(texte, w);
    return w;
  }

  function construireMotsBande(donnees) {
    // mots horodatés de toutes les répliques ; une réplique sans mots reçoit
    // la même distribution uniforme que le repli serveur
    const bruts = [];
    (donnees.repliques || []).forEach(r => {
      const d0 = Number(r.debut) || 0, d1 = Number(r.fin) || d0;
      let ms = r.mots;
      if (!Array.isArray(ms) || !ms.length)
        ms = RythmoTimeline.distribuerUniforme(
          String(r.texte || "").split(/\s+/).filter(Boolean), d0, d1);
      (ms || []).forEach(m => {
        const debut = Number(m.debut), fin = Number(m.fin);
        if (isFinite(debut) && isFinite(fin) && String(m.texte || "").trim())
          bruts.push({ texte: String(m.texte), debut, fin });
      });
    });
    bruts.sort((a, b) => a.debut - b.debut || a.fin - b.fin);
    if (!bruts.length) return { mots: [], v: 0, duree: 0 };
    const largeur = bandeFenetre.clientWidth || 900;
    const v = Math.max(largeur * VITESSE_BANDE, 20);
    let s = 0;
    const mots = bruts.map(m => {
      const w = largeurMot(m.texte);
      s = Math.max(v * m.debut, s);   // ancrage temporel + anti-chevauchement
      const o = { ...m, s, w };
      s = s + w + ESPACE_BANDE;
      return o;
    });
    return { mots, v, duree: Math.max(Number(donnees.duree_video) || 0,
                                      ...bruts.map(b => b.fin)) };
  }

  function fmtBande(s) {
    s = Math.max(0, Math.floor(s));
    const m = Math.floor(s / 60), r = s % 60;
    return m + ":" + String(r).padStart(2, "0");
  }

  function majBandeLecteur() {
    const L = lecteurBande;
    if (!L) return;
    const largeur = bandeFenetre.clientWidth || 900;
    const curseur = largeur * 0.15;
    bandePiste.style.transform = "translateX(" + (curseur - L.v * L.position) + "px)";
    // mots visibles (fenêtre ± marge) : recherche binaire sur s (croissant)
    const sMin = L.v * L.position - curseur - 60;
    const sMax = sMin + largeur + 120;
    const mots = L.mots;
    let lo = 0, hi = mots.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (mots[mid].s < sMin) lo = mid + 1; else hi = mid; }
    let ro = 0, rh = mots.length;
    while (ro < rh) { const mid = (ro + rh) >> 1; if (mots[mid].s <= sMax) ro = mid + 1; else rh = mid; }
    while (L.rendus.length && L.rendus[0].i < lo) L.rendus.shift().el.remove();
    while (L.rendus.length && L.rendus[L.rendus.length - 1].i >= ro) L.rendus.pop().el.remove();
    const present = new Set(L.rendus.map(r => r.i));
    for (let i = lo; i < ro; i++) {
      if (present.has(i)) continue;
      const m = mots[i];
      const el = document.createElement("span");
      el.className = "bande-mot";
      el.textContent = m.texte;
      el.style.left = m.s + "px";
      bandePiste.appendChild(el);
      L.rendus.push({ i, el });
    }
    L.rendus.sort((a, b) => a.i - b.i);
    const t = L.position;
    L.rendus.forEach(r => {
      const m = mots[r.i];
      r.el.classList.toggle("actif", t >= m.debut && t <= m.fin);
      r.el.classList.toggle("passe", m.fin < t - 0.05);
    });
    if (bandeTemps) bandeTemps.textContent = fmtBande(t) + " / " + fmtBande(L.duree);
  }

  function cadreBandeLecteur() {
    const L = lecteurBande;
    if (!L) return;
    if (L.lecture && ctxAudio) {
      L.position = L.basePos + (ctxAudio.currentTime - L.baseCtx);
      if (L.position >= L.duree) { L.position = L.duree; pauseBandeLecteur(); }
    }
    majBandeLecteur();
    L.raf = requestAnimationFrame(cadreBandeLecteur);
  }

  function activerBandeLecteur(donnees) {
    if (!bandeFenetre || !bandePiste || !donnees || !(donnees.repliques || []).length) return;
    const construit = construireMotsBande(donnees);
    if (!construit.mots.length) return;
    if (rafHero) { cancelAnimationFrame(rafHero); rafHero = null; }
    if (lecteurBande) { pauseBandeLecteur(); if (lecteurBande.raf) cancelAnimationFrame(lecteurBande.raf); }
    lecteurBande = { ...construit, position: 0, lecture: false, basePos: 0, baseCtx: 0,
                     sources: [], minuterie: null, rendus: [], raf: null };
    bandeFenetre.classList.add("mode-lecteur");
    bandePiste.innerHTML = "";
    const bande = document.querySelector(".bande");
    if (bande) bande.setAttribute("aria-hidden", "false");
    if (bandeControles) bandeControles.hidden = false;
    if (bandeJouer) bandeJouer.textContent = "▶";
    majBandeLecteur();
    lecteurBande.raf = requestAnimationFrame(cadreBandeLecteur);
  }

  function jouerBandeLecteur() {
    const L = lecteurBande;
    if (!L || !jobCourant) return;
    arreterLecture();   // coupe l'écoute d'un mot de l'éditeur
    if (!ctxAudio)
      ctxAudio = new (window.AudioContext || window.webkitAudioContext)();
    if (ctxAudio.state === "suspended") ctxAudio.resume().catch(() => {});
    if (L.position >= L.duree - 0.1) L.position = 0;
    L.lecture = true;
    L.basePos = L.position;
    L.baseCtx = ctxAudio.currentTime + 0.1;
    L.sources = [];
    if (bandeJouer) bandeJouer.textContent = "❚❚";
    jouerTrancheBande(L.position);
  }

  function jouerTrancheBande(debut) {
    const L = lecteurBande;
    if (!L || !L.lecture || !jobCourant) return;
    const fin = Math.min(debut + DUREE_TRANCHE, L.duree);
    fetch("/api/jobs/" + jobCourant + "/audio?debut=" + debut.toFixed(3) +
          "&fin=" + fin.toFixed(3))
      .then(r => r.ok ? r.arrayBuffer() : null)
      .then(brut => brut ? ctxAudio.decodeAudioData(brut) : null)
      .then(buffer => {
        if (!buffer || lecteurBande !== L || !L.lecture) return;
        const source = ctxAudio.createBufferSource();
        source.buffer = buffer;
        source.connect(ctxAudio.destination);
        const quand = Math.max(L.baseCtx + (debut - L.basePos),
                               ctxAudio.currentTime + 0.02);
        source.start(quand);
        L.sources.push(source);
        source.onended = () => { L.sources = L.sources.filter(s => s !== source); };
        if (fin < L.duree - 0.1) {
          // pré-charge la tranche suivante quand la fin approche
          const delai = Math.max(0, (quand + (fin - debut)) - ctxAudio.currentTime - 1.5) * 1000;
          L.minuterie = setTimeout(() => jouerTrancheBande(fin), Math.min(delai, 2000));
        }
      })
      .catch(() => {});
  }

  function pauseBandeLecteur() {
    const L = lecteurBande;
    if (!L) return;
    if (L.lecture && ctxAudio)
      L.position = L.basePos + (ctxAudio.currentTime - L.baseCtx);
    L.lecture = false;
    L.sources.forEach(s => { try { s.stop(); } catch (_) {} });
    L.sources = [];
    if (L.minuterie) { clearTimeout(L.minuterie); L.minuterie = null; }
    if (bandeJouer) bandeJouer.textContent = "▶";
    majBandeLecteur();
  }

  function seBandeLecteur(ts) {
    const L = lecteurBande;
    if (!L) return;
    const enLecture = L.lecture;
    if (enLecture) {
      L.lecture = false;
      L.sources.forEach(s => { try { s.stop(); } catch (_) {} });
      L.sources = [];
      if (L.minuterie) { clearTimeout(L.minuterie); L.minuterie = null; }
    }
    L.position = Math.min(Math.max(ts, 0), L.duree);
    if (enLecture) jouerBandeLecteur();
    majBandeLecteur();
  }

  function arreterBandeLecteur() {
    const L = lecteurBande;
    if (!L) return;
    pauseBandeLecteur();
    if (L.raf) cancelAnimationFrame(L.raf);
    lecteurBande = null;
    bandeFenetre.classList.remove("mode-lecteur");
    bandePiste.innerHTML = "";
    if (bandeControles) bandeControles.hidden = true;
    const bande = document.querySelector(".bande");
    if (bande) bande.setAttribute("aria-hidden", "true");
    demarrerBandeHero();
  }

  if (bandeFenetre)
    bandeFenetre.addEventListener("click", e => {
      if (!lecteurBande || !bandeFenetre.classList.contains("mode-lecteur")) return;
      if (e.target.closest("#bande-controles")) return;
      const rect = bandeFenetre.getBoundingClientRect();
      if (!rect.width) return;
      const x = e.clientX - rect.left;
      seBandeLecteur(lecteurBande.position + (x - rect.width * 0.15) / lecteurBande.v);
    });
  if (bandeJouer)
    bandeJouer.addEventListener("click", e => {
      e.stopPropagation();
      if (!lecteurBande) return;
      if (lecteurBande.lecture) pauseBandeLecteur(); else jouerBandeLecteur();
    });

  let minuterieRedessin = null;
  window.addEventListener("resize", () => {
    clearTimeout(minuterieRedessin);
    minuterieRedessin = setTimeout(() => {
      rendererToutesLesPistes();
      rafraichirApercu();
    }, 120);
  });

  /* ——————— raccourcis clavier + navigation entre répliques ——————— */
  function naviguerReplique(sens) {
    const blocs = [...$("#liste-repliques").querySelectorAll(".bloc-replique")];
    if (!blocs.length) return;
    let index = sens < 0 ? blocs.length - 1 : 0;
    const actif = document.activeElement && document.activeElement.closest
                  ? document.activeElement.closest(".bloc-replique") : null;
    if (actif) index = blocs.indexOf(actif) + sens;
    index = Math.min(blocs.length - 1, Math.max(0, index));
    const cible = blocs[index];
    cible.scrollIntoView({ behavior: "smooth", block: "center" });
    const champ = cible.querySelector(".champ-texte");
    if (champ) { champ.focus(); champ.select(); }
  }

  document.addEventListener("keydown", e => {
    if (e.key === "Escape") {
      if (lecteurBande && lecteurBande.lecture) pauseBandeLecteur();
      if (sourceLecture || blocEnLecture) arreterLecture();
      return;
    }
    if ($("#editeur").style.display !== "block") return;
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.key === "Enter") {
      e.preventDefault();
      envoyerRepliques();
    } else if (mod && e.key.toLowerCase() === "s") {
      e.preventDefault();
      sauvegarderRepliques();
    } else if (e.altKey && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
      e.preventDefault();
      naviguerReplique(e.key === "ArrowDown" ? 1 : -1);
    }
  });

  window.addEventListener("beforeunload", e => {
    if (editionModifiee) {
      e.preventDefault();
      e.returnValue = "";
    }
  });

  function progression(pct, etape) {
    $("#barre-remplie").style.width = pct + "%";
    $("#pct").textContent = pct + " %";
    $("#etape-texte").textContent = etape;
  }

  function suivre(job_id) {
    if (sourceCourante) { sourceCourante.close(); sourceCourante = null; }
    const source = new EventSource("/api/jobs/" + job_id + "/events");
    sourceCourante = source;
    source.onmessage = ev => {
      const d = JSON.parse(ev.data);
      if (d.statut === "annule") {
        source.close();
        if (sourceCourante === source) sourceCourante = null;
        arreterBandeLecteur();  // plus de job : bande héros
        $("#editeur").style.display = "none";
        afficherErreur("Traitement annulé.");
        $("#bouton-lancer").disabled = false;
        return;
      }
      if (d.statut === "erreur") {
        source.close();
        if (sourceCourante === source) sourceCourante = null;
        arreterBandeLecteur();  // plus de job : bande héros
        $("#editeur").style.display = "none";
        afficherErreur((d.erreur && (d.erreur.message_utilisateur || d.erreur.message))
                       || "échec du traitement");
        $("#bouton-lancer").disabled = false;
        return;
      }
      // pause d'édition : on récupère les répliques et on ouvre l'éditeur,
      // SANS fermer le flux (le rendu repartira sur le même suivi).
      if (d.statut === "pret_edition" && $("#editeur").style.display !== "block") {
        fetch("/api/jobs/" + job_id + "/repliques")
          .then(r => r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status)))
          .then(ouvrirEditeur)
          .catch(err => {
            source.close();
            afficherErreur("impossible de récupérer les répliques : " + err.message);
            $("#bouton-lancer").disabled = false;
          });
        return;
      }
      progression(d.progression, d.etape || "");
      if (d.statut === "termine") {
        source.close();
        if (sourceCourante === source) sourceCourante = null;
        const url = "/api/jobs/" + job_id + "/result?m=" + Date.now();
        $("#preview").src = url;
        $("#download").href = url;
        $("#download").setAttribute("download", "rythmo_" + job_id + ".mp4");
        $("#download-srt").href = "/api/jobs/" + job_id + "/srt?m=" + Date.now();
        $("#download-srt").setAttribute("download", "rythmo_" + job_id + ".srt");
        $("#resultat").style.display = "block";
        $("#meta").textContent = "Traitement 100 % local • job " + job_id;
        $("#bouton-lancer").disabled = false;
        $("#resultat").scrollIntoView({ behavior: "smooth" });
        // bande finale : les répliques validées, défilantes au rythme de l'audio
        fetch("/api/jobs/" + job_id + "/repliques")
          .then(r => r.ok ? r.json() : null)
          .then(d => { if (d) activerBandeLecteur(d); })
          .catch(() => {});
      }
    };
    source.onerror = () => {
      source.close();
      afficherErreur("suivi interrompu (connexion au serveur perdue)");
      $("#bouton-lancer").disabled = false;
    };
  }
})();
