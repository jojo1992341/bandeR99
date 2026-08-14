(() => {
  const $ = s => document.querySelector(s);
  let fichierChoisi = null;
  let jobCourant = null;        // job en cours (événements + édition)
  let dureeVideoJob = 0;        // durée vidéo annoncée par l'analyse
  let sourceCourante = null;    // EventSource de suivi (jamais deux à la fois)
  let editionModifiee = false;   // corrections non encore validées côté serveur

  fetch("/api/health").then(r => r.json()).then(h => {
    $("#device").textContent = h.device.toUpperCase();
    $("#version").textContent = "v" + h.version;
  }).catch(() => { $("#device").textContent = "?"; });

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
      style: $("#opt-style").value,
      theme: $("#opt-theme").value,
      hauteur_bande: parseInt($("#opt-taille").value) || 110,
      curseur_ratio: Math.min(50, Math.max(5, parseInt($("#opt-curseur").value) || 15)) / 100,
      vitesse: $("#opt-vitesse").value ? parseFloat($("#opt-vitesse").value) : null,
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
    const erreurs = validerLocal(repliques);
    afficherErreursEdition(erreurs);
    if (erreurs.length) return;  // on ne sauvegarde pas un brouillon invalide
    const donnees = { "version": 1, "duree_video": dureeVideoJob,
                      "job": jobCourant, "repliques": repliques };
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
      const erreurs = validerLocal(vues);
      if (erreurs.length)
        return afficherErreursEdition(["Fichier refusé :", ...erreurs]);
      $("#liste-repliques").innerHTML = "";
      vues.forEach((r, i) => ajouterLigneReplique(r, true));
      afficherErreursEdition([]);
      marquerModifiee();
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
      '<button class="bouton-ecoute" title="Écouter cette réplique">▶</button>' +
      '<button class="bouton-suggestions" title="Proposer des corrections">✨</button>' +
      '<select class="champ-voix" title="Voix de cette réplique : détectée automatiquement, ou choix manuel (T94)">' +
        '<option value="">—</option>' +
        '<option value="1">Voix 1</option>' +
        '<option value="2">Voix 2</option>' +
        '<option value="3">Voix 3</option>' +
        '<option value="4">Voix 4</option>' +
      '</select>' +
      '<textarea class="champ-texte" rows="1" spellcheck="false"></textarea>' +
      '<button class="bouton-inserer" title="Insérer une réplique après celle-ci">＋</button>' +
      '<button class="bouton-suppr" title="Supprimer cette réplique">🗑</button>';
    if (replique.personnage != null)
      ligne.querySelector(".champ-voix").value =
        String(Number(replique.personnage) + 1);
    ligne.querySelector(".champ-voix").addEventListener("change", marquerModifiee);
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
        piste.mots = resynchroniserMots(
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
    $("#liste-repliques").innerHTML = "";
    (donnees.repliques || []).forEach(r => ajouterLigneReplique(r, false));
    $("#erreurs-edition").style.display = "none";
    $("#editeur").style.display = "block";
    editionModifiee = false;
    majEtatEdition();
    progression(78, "en attente de vos corrections…");
    rendererToutesLesPistes();
    rafraichirApercu();
    $("#editeur").scrollIntoView({ behavior: "smooth" });
  }

  function collecterRepliques() {
    const sortie = [];
    $("#liste-repliques").querySelectorAll(".bloc-replique").forEach(bloc => {
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
        }));
      sortie.push(r);
    });
    return sortie;
  }

  function validerLocal(repliques) {
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
        if (dureeVideoJob && r.fin > dureeVideoJob + 0.6)
          erreurs.push("Réplique " + n + " : fin au-delà de la vidéo (" +
                       dureeVideoJob.toFixed(1) + " s).");
      }
      if (i > 0) {
        const p = repliques[i - 1];
        if (!isNaN(p.fin) && !isNaN(r.debut) && r.debut < p.fin - 0.05)
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
        body: JSON.stringify({ repliques }),
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
    const erreurs = validerLocal(repliques);
    afficherErreursEdition(erreurs);
    if (erreurs.length) return;  // on n'exporte pas un brouillon invalide
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

  /* ——————— resynchronisation mot-à-mot à la frappe (miroir client du serveur) ———————
     Le comédien corrige le texte d'une réplique : la timeline se met à jour
     immédiatement avec la même logique que `resynchroniser_mots` côté serveur
     (difflib) — les mots inchangés gardent leurs timings, un mot inséré
     occupe le silence entre ses voisins, un mot supprimé disparaît. */
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
    const tokens = String(nouveauTexte || "").split(/\s+/).filter(t => t.length);
    if (!tokens.length) return [];
    debut = Number(debut); fin = Number(fin);
    if (!isFinite(debut) || !isFinite(fin) || fin <= debut) return [];
    if (!motsActuels || !motsActuels.length)
      return distribuerUniforme(tokens, debut, fin);
    const seqO = motsActuels.map(m => normaliserToken(m.texte));
    const seqN = tokens.map(normaliserToken);
    if (seqO.length === seqN.length && seqO.every((t, i) => t === seqN[i])) {
      // même liste de mots (hors casse/ponctuation) : timings conservés
      return tokens.map((t, i) => ({
        texte: t, debut: motsActuels[i].debut, fin: motsActuels[i].fin,
      }));
    }
    const intervalles = new Array(tokens.length).fill(null);
    diffOpcodes(seqO, seqN).forEach(op => {
      if (op.type === "equal") {
        for (let k = 0; k < op.i2 - op.i1; k++) {
          const m = motsActuels[op.i1 + k];
          intervalles[op.j1 + k] = [m.debut, m.fin];
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
    })), debut, fin);
  }

  function construirePisteMots(replique) {
    const piste = document.createElement("div");
    piste.className = "piste-mots";
    const canvas = document.createElement("canvas");
    canvas.className = "onde-piste";
    piste.appendChild(canvas);
    piste.mots = (replique.mots || []).map(m => ({
      texte: String(m.texte ?? ""),
      debut: Number(m.debut), fin: Number(m.fin),
    }));
    return piste;  // rendu différé : la piste n'est pas encore dans le DOM
  }

  function rendererPiste(piste) {
    // la piste n'est pas le voisin immédiat de la ligne (panneau de suggestions
    // entre les deux) : on remonte au bloc pour retrouver la ligne (T74).
    const ligne = piste.closest(".bloc-replique")
      ? piste.closest(".bloc-replique").querySelector(".ligne-replique")
      : piste.previousElementSibling;
    const d0 = parseFloat(ligne.querySelector(".champ-debut").value);
    const d1 = parseFloat(ligne.querySelector(".champ-fin").value);
    const duree = Math.max(d1 - d0, 0.001);
    dessinerOnde(piste.querySelector("canvas"), d0, d1, "rgba(120,180,255,.6)",
                 jobCourant, Math.max(120, Math.round(piste.clientWidth || 600)));
    piste.querySelectorAll(".bloc-mot").forEach(b => b.remove());
    piste.mots.forEach((m, i) => {
      const bloc = document.createElement("div");
      bloc.className = "bloc-mot";
      const marqueur = /^\([^()]{1,60}\)$/.test(String(m.texte).trim());
      if (marqueur) bloc.classList.add("bloc-marqueur");
      const texteMot = document.createElement("span");
      texteMot.className = "texte-mot";
      texteMot.textContent = m.texte;
      bloc.appendChild(texteMot);
      bloc.title = marqueur ? "Symbole de respiration — non prononcé" : "";
      bloc.style.left = (m.debut - d0) / duree * 100 + "%";
      bloc.style.width = Math.max((m.fin - m.debut) / duree * 100, 4) + "%";
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
      attacherGlisser(piste, bloc, i, poignee);
      piste.appendChild(bloc);
    });
  }

  function rendererToutesLesPistes() {
    $("#liste-repliques").querySelectorAll(".piste-mots")
      .forEach(rendererPiste);
  }

  function attacherGlisser(piste, bloc, i, poignee) {
    const ligne = piste.closest(".bloc-replique")
      ? piste.closest(".bloc-replique").querySelector(".ligne-replique")
      : piste.previousElementSibling;
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
      mode = e.target === poignee ? "etirer" : "deplacer";
      x0 = e.clientX;
      bloc.setPointerCapture(e.pointerId);
    });
    bloc.addEventListener("pointermove", e => {
      if (!mode || mode === "bouton") return;
      deplace = true;
      const rect = piste.getBoundingClientRect();
      const d0 = parseFloat(ligne.querySelector(".champ-debut").value);
      const d1 = parseFloat(ligne.querySelector(".champ-fin").value);
      const secParPx = (d1 - d0) / Math.max(rect.width, 1);
      const delta = (e.clientX - x0) * secParPx;
      const m = piste.mots[i];
      if (mode === "deplacer") {
        const duree = m.fin - m.debut;
        const min = i > 0 ? piste.mots[i - 1].fin : d0;
        const max = i + 1 < piste.mots.length ? piste.mots[i + 1].debut - duree
                                              : d1 - duree;
        m.debut = Math.min(Math.max(m.debut + delta, min), max);
        m.fin = m.debut + duree;
        if (i === 0) ligne.querySelector(".champ-debut").value = m.debut.toFixed(3);
        // dernier mot : la fenêtre ne suit pas pendant le geste (voir relacher)
      } else {
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
      }
      bloc.style.left = (m.debut - d0) / (d1 - d0) * 100 + "%";
      bloc.style.width = Math.max((m.fin - m.debut) / (d1 - d0) * 100, 4) + "%";
      x0 = e.clientX;
    });
    const relacher = () => {
      if (mode === "bouton") { mode = null; return; }  // le clic du bouton se gère lui-même
      const simpleClic = mode !== null && !deplace;  // clic sans glisser : écouter le mot
      mode = null;
      if (i === piste.mots.length - 1 && piste.mots.length) {
        // la fenêtre ne suit le dernier mot QUE vers l'extérieur : étendre le
        // mot allonge la réplique, le raccourcir laisse un espace travaillable
        // à la fin (bug signalé : la fenêtre « recollait » au mot)
        const d1 = parseFloat(ligne.querySelector(".champ-fin").value);
        const finMot = piste.mots[i].fin;
        if (finMot > d1) ligne.querySelector(".champ-fin").value = finMot.toFixed(3);
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
        $("#editeur").style.display = "none";
        afficherErreur("Traitement annulé.");
        $("#bouton-lancer").disabled = false;
        return;
      }
      if (d.statut === "erreur") {
        source.close();
        if (sourceCourante === source) sourceCourante = null;
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
      }
    };
    source.onerror = () => {
      source.close();
      afficherErreur("suivi interrompu (connexion au serveur perdue)");
      $("#bouton-lancer").disabled = false;
    };
  }
})();
