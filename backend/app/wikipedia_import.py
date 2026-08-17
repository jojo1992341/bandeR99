"""Import Wikipédia du glossaire — heuristique déterministe, sans LLM.

``resoudre_saisie`` transforme un titre nu ou une URL ``*.wikipedia.org`` en
``(langue, titre)`` — seuls les hôtes ``fr.wikipedia.org`` et
``en.wikipedia.org`` sont acceptés (jamais d'URL arbitraire : pas de SSRF).
``extraire_termes`` interroge l'API Action Wikipédia et récupère le **wikitext
complet** de l'article (source non tronquée, infobox comprise — c'est ce qui
est rendu dans ``<div class="mw-page-container">``). Il en extrait :

- le titre de l'article ;
- les cibles des wikiliens ``[[…]]`` (personnages, lieux, acteurs, termes) ;
- les noms propres capitalisés détectés dans le texte intégral.

Le tout est filtré (liste d'arrêt, dates/digits), dédoublonné (casse/accents
insensible) et plafonné. Le client HTTP est injectable (les tests n'utilisent
jamais le réseau).
"""
from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

from .traduction.glossaire import normaliser_cle

HOTES_AUTORISES = ("fr.wikipedia.org", "en.wikipedia.org")
MAX_CANDIDATS = 800000
MAX_MOTS_TERME = 4  # au-delà, un terme ressemble à une phrase, pas à un nom

# Catégories de la sortie LLM structurée (ordre d'affichage et d'aplatissement).
CATEGORIES = ("essentiels", "difficiles", "termes")

# La politique de l'API Wikimedia exige un User-Agent descriptif avec un
# contact ; sans cela, l'API répond 403 Forbidden.
USER_AGENT = "RythmoDub/1.0 (contact: rythmo-dub@example.com)"

# Termes génériques fréquemment liés dans un article de film/série : ils
# n'apportent rien à la reconnaissance FR. Comparaison normalisée (casse et
# accents ignorés).
_ARRETS = {
    "france", "comedie", "drame", "film", "serie", "serie televisee",
    "cinema", "acteur", "actrice", "realisation", "realisateur",
    "personnage", "personnages", "saison", "episode", "liste de",
    "wikipedia", "paris", "anglais", "francais", "royaume-uni", "etats-unis",
    "amerique", "europe", "annee", "siecle", "naissance", "mort",
    "television", "humour", "fantasy", "science-fiction", "aventure",
    "histoire", "guerre", "amour", "famille",
    # pronoms/déterminants/conjonctions courants : capitalisés en début de
    # phrase, jamais utiles à la reconnaissance vocale
    "elle", "il", "ils", "elles", "on", "je", "tu", "nous", "vous",
    "ce", "cette", "ces", "cet", "son", "sa", "ses", "leur", "leurs",
    "un", "une", "mon", "ma", "mes", "ton", "ta", "tes", "notre", "nos",
    "votre", "vos", "qui", "que", "quoi", "ou", "quand", "comment", "pourquoi",
    # rôles/fonctions d'infobox et mots communs : jamais utiles à l'ASR
    "musique", "production", "producteur", "productrice", "producteurs",
    "realisatrice", "scenariste", "scenario", "directeur", "directrice",
    "montage", "diffusion", "chaine", "format", "genre", "costumes", "decors",
    "casting", "doublage", "trilogie", "francaise", "quebecois", "belge", "suisse",
    "premier", "premiere", "deuxieme", "troisieme", "quatrieme", "cinquieme",
    "sixieme", "septieme", "huitieme", "dernier", "derniere",
}

_ARRET_REGEXES = (
    re.compile(r"^saison \d"),        # « Saison 4 de Kaamelott »…
    re.compile(r"^episode \d"),
    re.compile(r"^\d{4}$"),
    re.compile(r"^liste "),           # « Liste des épisodes de … » → page-liste
    re.compile(r"^personnages? de "),  # « Personnages de … » → page-liste
    re.compile(r"^[ivxlcdm]+e$"),     # ordinaux romains : Ve, IVe, Xe, XXe…
    re.compile(r"^[ivxlcdm]+er$"),    # Ier, IIer…
    # rôles de production / ordinaux en tête de terme (« sixième et dernière saison »)
    re.compile(r"^producteur "),
    re.compile(r"^production "),
    re.compile(r"^directeur "),
    re.compile(r"^direction "),
    re.compile(r"^delegue"),
    re.compile(r"^(premier|premiere|deuxieme|troisieme|quatrieme|cinquieme|"
               r"sixieme|septieme|huitieme|dernier|derniere)\b"),
)


class ErreurImportWikipedia(Exception):
    """Erreur d'import portant un code stable et un message FR."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _langue_de_hote(hote: str) -> str | None:
    """Code langue (``fr``/``en``) d'un hôte ``*.wikipedia.org``, ou ``None``."""
    hote = (hote or "").lower()
    suffixe = ".wikipedia.org"
    if not hote.endswith(suffixe):
        return None
    prefixe = hote[: -len(suffixe)]
    if not prefixe:
        return None
    libelles = prefixe.split(".")
    if libelles and libelles[-1] == "m":  # fr.m.wikipedia.org → mobile
        libelles = libelles[:-1]
    if len(libelles) != 1 or libelles[0] not in {"fr", "en"}:
        return None
    return libelles[0]


def resoudre_saisie(saisie: str, langue: str | None = None) -> tuple[str, str]:
    """``(langue, titre)`` depuis un titre nu (défaut ``fr``) ou une URL Wikipédia.

    Pour un titre nu, ``langue`` (défaut ``fr``) est utilisée si fournie. Pour
    une URL, l'hôte fixe la langue (prioritaire sur ``langue``).
    """
    saisie = (saisie or "").strip()
    if not saisie:
        raise ErreurImportWikipedia("E005", "Titre ou URL Wikipédia requis")
    if langue is not None and langue not in {"fr", "en"}:
        raise ErreurImportWikipedia("E005", f"Langue inconnue : {langue}")
    if saisie.startswith(("http://", "https://")):
        partie = urlparse(saisie)
        langue_hote = _langue_de_hote(partie.hostname or "")
        if langue_hote is None:
            raise ErreurImportWikipedia(
                "E005", "URL Wikipédia non reconnue (fr./en.wikipedia.org attendu)")
        chemin = partie.path or ""
        if not chemin.startswith("/wiki/"):
            raise ErreurImportWikipedia(
                "E005", "URL Wikipédia invalide (chemin /wiki/ attendu)")
        titre = unquote(chemin[len("/wiki/"):]).replace("_", " ").strip()
        if not titre:
            raise ErreurImportWikipedia("E005", "Titre vide dans l'URL Wikipédia")
        return langue_hote, titre
    return (langue or "fr"), saisie


# Découpage du texte complet : les mots capitalisés hors connecteurs sont des
# noms propres potentiels. Les connecteurs (de, du, l', d', …) relient les
# mots d'un même nom.
_TOKEN = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]+")
_SEGMENTS = re.compile(r"[.!?…;:()\[\]{}\n«»\"“”,]")
_CONNECTEURS = {
    "de", "du", "des", "la", "le", "les", "d", "l", "et", "a", "au", "aux",
    "en", "sur", "sous", "pour", "avec", "sans", "ou", "of", "the", "and", "in",
}


def _connecteur(mot: str) -> bool:
    """Vrai si le mot est un connecteur (préposition/article/élision)."""
    return mot.lower() in _CONNECTEURS


def _nom_propre(mot: str) -> bool:
    """Mot capitalisé qui n'est pas un connecteur (nom propre potentiel)."""
    if not mot or not mot[0].isupper():
        return False
    return not _connecteur(mot)


def _sequences_noms_propres(extrait: str) -> list[tuple[str, bool]]:
    """Séquences capitalisées du texte, avec « début de segment » ou non.

    Une séquence commence et finit par un nom propre (≤ ``MAX_MOTS_TERME``), les
    connecteurs reliant les mots entre eux (ex. « Lancelot du Lac »).
    """
    resultats: list[tuple[str, bool]] = []
    for segment in _SEGMENTS.split(extrait):
        mots = _TOKEN.findall(segment)
        i = 0
        while i < len(mots):
            if not _nom_propre(mots[i]):
                i += 1
                continue
            debut = i
            i += 1
            nb_caps = 1
            while i < len(mots):
                if _nom_propre(mots[i]):
                    if nb_caps >= MAX_MOTS_TERME:
                        break
                    nb_caps += 1
                    i += 1
                elif _connecteur(mots[i]):
                    k = i
                    while k < len(mots) and _connecteur(mots[k]):
                        k += 1
                    if k < len(mots) and _nom_propre(mots[k]) and nb_caps < MAX_MOTS_TERME:
                        nb_caps += 1
                        i = k + 1
                    else:
                        break
                else:
                    break
            resultats.append((" ".join(mots[debut:i]), debut == 0))
    return resultats


def _extraire_noms_propres_texte(extrait: str) -> list[str]:
    """Noms propres du texte complet, hors débuts de phrase ambigus.

    Un candidat n'est retenu que s'il apparaît au moins une fois ailleurs qu'en
    début de segment (il n'est donc pas simplement capitalisé par la ponctuation).
    """
    stats: dict[str, dict] = {}
    ordre: list[str] = []
    for texte, en_debut in _sequences_noms_propres(extrait):
        cle = normaliser_cle(texte)
        if not cle or len(cle) < 2:
            continue
        if cle not in stats:
            stats[cle] = {"texte": texte, "hors_debut": 0}
            ordre.append(cle)
        if not en_debut:
            stats[cle]["hors_debut"] += 1
    return [stats[c]["texte"] for c in ordre if stats[c]["hors_debut"] > 0]


def _valide(norm: str) -> bool:
    """Filtre final commun : stoplist, dates/digits, longueur raisonnable."""
    if not norm or len(norm) < 2 or len(norm) > 60:
        return False
    if norm in _ARRETS:
        return False
    if re.match(r"\d", norm):
        return False  # les dates commencent par un chiffre ; les sigles (M6, TF1) restent
    if re.search(r"\b(?:1[89]\d{2}|20\d{2})\b", norm):
        return False  # « … des années 2020 » : une année quelque part dans le terme
    return not any(regex.match(norm) for regex in _ARRET_REGEXES)


def _dedoublonner(termes) -> list[str]:
    """Dédoublonne (casse/accents ignorés), filtre et plafonne les candidats."""
    vus: set[str] = set()
    candidats: list[str] = []
    for terme in termes:
        terme = str(terme).strip()
        norm = normaliser_cle(terme)
        if not _valide(norm) or norm in vus:
            continue
        vus.add(norm)
        candidats.append(terme)
        if len(candidats) >= MAX_CANDIDATS:
            break
    return candidats


# ——— wikitext → texte clair / wikiliens ———

_LIEN = re.compile(r"\[\[([^\[\]|]+)(?:\|([^\[\]]*))?\]\]")
_NAMESPACES_IGNORES = (
    "fichier:", "file:", "image:", "catégorie:", "category:", "aide:", "help:",
    "modèle:", "template:", "wikipédia:", "wikipedia:", "portail:", "spécial:",
    "special:", "media:",
)


def _nettoyer_cible(cible: str) -> str:
    """Retire l'ancre ``#Section`` et la désambiguïsation ``(…)`` d'une cible."""
    cible = cible.split("#")[0].strip()
    return re.sub(r"\s*\([^)]*\)\s*$", "", cible).strip()


def _retirer_modeles(wikitext: str) -> str:
    """Retire les modèles ``{{…}}`` (imbrications bornées) du wikitext."""
    resultat = wikitext
    for _ in range(20):  # profondeur bornée : les infobox imbriquent rarement plus
        avant = resultat
        resultat = re.sub(r"\{\{[^{}]*\}\}", " ", resultat)
        if resultat == avant:
            break
    return resultat


def _texte_wikitext(wikitext: str) -> str:
    """Wikitext → texte clair (pour l'analyse des noms propres et le prompt LLM)."""
    texte = _retirer_modeles(wikitext)
    texte = re.sub(r"<!--.*?-->", " ", texte, flags=re.DOTALL)
    texte = re.sub(r"<ref[^>]*/>", " ", texte)
    texte = re.sub(r"<ref[^>]*>.*?</ref>", " ", texte, flags=re.DOTALL)
    texte = re.sub(r"<[^>]+>", " ", texte)
    texte = re.sub(r"\[\[([^\[\]|]+)\|([^\[\]]*)\]\]", r"\2", texte)
    texte = re.sub(r"\[\[([^\[\]]+)\]\]", r"\1", texte)
    texte = re.sub(r"'''|''", "", texte)
    texte = re.sub(r"={2,}[^=\n]+={2,}", " ", texte)
    texte = texte.replace("&nbsp;", " ")
    texte = re.sub(r"\s+", " ", texte)
    return texte.strip()


def _liens_wikitext(wikitext: str) -> list[str]:
    """Cibles ET libellés capitalisés des wikiliens ``[[…]]``.

    Le libellé est la forme écrite dans l'article (ex. « Arthur » pour
    ``[[Personnages de Kaamelott#Arthur|Arthur]]``) : c'est lui que l'audio
    prononce. La cible est nettoyée (ancre ``#…`` et parenthèses retirées).
    """
    titres: list[str] = []
    for cible, libelle in _LIEN.findall(wikitext):
        cible = (cible or "").strip()
        libelle = (libelle or "").strip()
        if cible and not cible.lower().startswith(_NAMESPACES_IGNORES):
            cible = _nettoyer_cible(cible)
            if cible:
                titres.append(cible)
        if libelle and re.search(r"[A-ZÀ-ÖØ-öø-ÿ]", libelle) \
                and not libelle.lower().startswith(_NAMESPACES_IGNORES):
            titres.append(libelle)
    return titres


# ——— extraction depuis la réponse API ———

def _page_de_payload(payload: dict) -> dict:
    """Page unique d'une réponse ``action=query`` (lève E011 si absente)."""
    pages = (payload.get("query") or {}).get("pages") or []
    if not pages:
        raise ErreurImportWikipedia("E011", "Page Wikipédia introuvable")
    page = pages[0] or {}
    if page.get("missing"):
        raise ErreurImportWikipedia("E011", "Page Wikipédia introuvable")
    return page


def _wikitext_de_page(payload: dict) -> str:
    """Wikitext complet d'une réponse ``action=query`` (``rvprop=content``)."""
    page = _page_de_payload(payload)
    revisions = page.get("revisions") or []
    if not revisions:
        raise ErreurImportWikipedia("E011", "Page Wikipédia introuvable")
    slots = (revisions[0] or {}).get("slots") or {}
    contenu = (slots.get("main") or {}).get("content")
    return str(contenu or "")


def _texte_de_page(payload: dict) -> str:
    """Texte clair de la page (wikitext nettoyé) — utilisé aussi par le LLM."""
    return _texte_wikitext(_wikitext_de_page(payload))


def _extraire_depuis_page(payload: dict) -> list[str]:
    """Candidats extraits d'une réponse ``action=query`` (formatversion=2).

    Analyse le contenu COMPLET de l'article (wikitext) : titre + wikiliens +
    noms propres capitalisés du texte intégral.
    """
    page = _page_de_payload(payload)
    titre_page = str(page.get("title") or "").strip()
    wikitext = _wikitext_de_page(payload)
    texte = _texte_wikitext(wikitext)
    return _dedoublonner([titre_page] + _liens_wikitext(wikitext)
                         + _extraire_noms_propres_texte(texte))


def _recuperer_page(titre: str, langue: str, client) -> dict:
    """Appelle l'API Action Wikipédia (client injectable) et renvoie le JSON."""
    if client is None:
        import httpx  # lazy : dépendance déjà déclarée, chargée à l'usage

        client = httpx
    url = f"https://{langue}.wikipedia.org/w/api.php"
    params = {
        "action": "query",
        "titles": titre,
        "prop": "revisions",
        "rvprop": "content",
        "rvslots": "main",
        "format": "json",
        "formatversion": "2",
        "redirects": "1",
    }
    try:
        reponse = client.get(url, params=params, timeout=10,
                             headers={"User-Agent": USER_AGENT})
        reponse.raise_for_status()
        return reponse.json()
    except ErreurImportWikipedia:
        raise
    except Exception as exc:  # noqa: BLE001 — réseau/HTTP : erreur claire, jamais de crash
        raise ErreurImportWikipedia("E010", str(exc)[:200]) from exc


def extraire_termes(titre: str, langue: str = "fr", client=None) -> list[str]:
    """Candidats de vocabulaire pour un titre Wikipédia (sans écrire le glossaire)."""
    if langue not in {"fr", "en"}:
        raise ErreurImportWikipedia("E005", f"Langue inconnue : {langue}")
    payload = _recuperer_page(titre, langue, client)
    return _extraire_depuis_page(payload)


def extraire_termes_avec_repli(titre: str, langue: str = "fr",
                               client=None, repli: bool = True) -> tuple[str, list[str]]:
    """``(langue_effective, candidats)`` : repli sur l'autre langue si la page est absente.

    Le repli ne se déclenche que sur « page introuvable » (E011), jamais sur
    une erreur réseau. ``repli=False`` fige la langue demandée (URL explicite).
    """
    if langue not in {"fr", "en"}:
        raise ErreurImportWikipedia("E005", f"Langue inconnue : {langue}")
    try:
        return langue, extraire_termes(titre, langue, client)
    except ErreurImportWikipedia as exc:
        if not (repli and exc.code == "E011"):
            raise
        autre = "en" if langue == "fr" else "fr"
        try:
            return autre, extraire_termes(titre, autre, client)
        except ErreurImportWikipedia:
            raise exc  # page absente dans les deux langues : on garde l'erreur d'origine


_PROMPT_EXTRACTION = (
    "Voici le contenu Wikipédia d'un film ou d'une série :\n\n{extrait}\n\n"
    "Liste uniquement les noms propres, personnages, lieux et termes rares "
    "utiles pour la reconnaissance vocale française. Réponds par une liste "
    "séparée par des virgules, sans commentaire ni numérotation."
)

_PROMPT_EXTRACTION_CATEGORISE = (
    "Voici le contenu Wikipédia d'un film ou d'une série :\n\n{extrait}\n\n"
    "Classe les noms propres, personnages, lieux et termes utiles à la "
    "reconnaissance vocale française en trois listes :\n"
    "1. « essentiels » : noms très fréquents et indispensables (personnages "
    "principaux, lieux récurrents) ;\n"
    "2. « difficiles » : termes rares, inhabituels ou à l'orthographe piégeuse, "
    "que la reconnaissance vocale risque d'écorcher ;\n"
    "3. « termes » : autres termes utiles (personnages secondaires, lieux, "
    "objets, expressions).\n\n"
    "Réponds STRICTEMENT au format suivant, une liste par ligne, sans "
    "commentaire ni numérotation :\n"
    "essentiels: A, B, C\n"
    "difficiles: D, E, F\n"
    "termes: G, H, I"
)


def _parser_llm(texte: str) -> list[str]:
    """Parse la réponse d'un LLM en liste dédupliquée — jamais d'exception."""
    morceaux = re.split(r"[\n,;]+", str(texte or ""))
    vus: set[str] = set()
    termes: list[str] = []
    for morceau in morceaux:
        terme = _nettoyer_morceau_llm(morceau)
        if not terme:
            continue
        cle = normaliser_cle(terme)
        if cle in vus:
            continue
        vus.add(cle)
        termes.append(terme)
        if len(termes) >= MAX_CANDIDATS:
            break
    return termes


def _nettoyer_morceau_llm(morceau: str) -> str:
    """Nettoie un morceau de réponse LLM (puces, numéros, guillemets) — ou ``''``."""
    terme = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", str(morceau or "")).strip()
    terme = terme.strip("\"'`()[]").rstrip(".,;:")
    if not 2 <= len(terme) <= 60 or len(terme.split()) > MAX_MOTS_TERME:
        return ""
    return terme


def _parser_categorise(texte: str) -> dict[str, list[str]]:
    """Parse une réponse LLM structurée en ``{essentiels, difficiles, termes}``.

    Tolère les variantes de format : en-têtes « essentiels: … », « - essentiels:
    … », « # essentiels » seul sur sa ligne, etc. Chaque liste est dédupliquée
    (casse/accents insensibles). Jamais d'exception.
    """
    resultat: dict[str, list[str]] = {c: [] for c in CATEGORIES}
    vus: dict[str, set[str]] = {c: set() for c in CATEGORIES}
    courante: str | None = None

    for ligne in str(texte or "").splitlines():
        entete = re.match(
            r"^\s*(?:[-*•]|\d+[.)])?\s*(essentiels?|difficiles?|termes?)\s*"
            r"[:：\-–]\s*(.*)$", ligne, re.IGNORECASE)
        if entete:
            courante = _canonique_categorie(entete.group(1))
            contenu = entete.group(2)
        else:
            seule = re.match(
                r"^\s*#*\s*(essentiels?|difficiles?|termes?)\s*$",
                ligne, re.IGNORECASE)
            if seule:
                courante = _canonique_categorie(seule.group(1))
                continue
            if courante is None:
                continue
            contenu = ligne
        if courante not in resultat:
            continue
        for morceau in re.split(r"[,\n;]+", contenu):
            terme = _nettoyer_morceau_llm(morceau)
            if not terme:
                continue
            cle = normaliser_cle(terme)
            if cle and cle not in vus[courante]:
                vus[courante].add(cle)
                resultat[courante].append(terme)
    return resultat


def _canonique_categorie(nom: str) -> str:
    """« essentiel »/« essentiels » → « essentiels » (clé de catégorie)."""
    prefixe = re.sub(r"s$", "", (nom or "").strip().lower())
    for categorie in CATEGORIES:
        if categorie.startswith(prefixe):
            return categorie
    return prefixe


def aplanir_categories(categories: dict[str, list[str]]) -> list[str]:
    """Concatène les catégories LLM en une liste plate dédupliquée.

    Ordre stable : essentiels, difficiles, termes (un terme présent dans
    plusieurs catégories n'apparaît qu'une fois, dans sa première catégorie).
    """
    vus: set[str] = set()
    termes: list[str] = []
    for categorie in CATEGORIES:
        for terme in categories.get(categorie, []):
            cle = normaliser_cle(terme)
            if cle and cle not in vus:
                vus.add(cle)
                termes.append(terme)
                if len(termes) >= MAX_CANDIDATS:
                    return termes
    return termes


def extraire_termes_llm(titre: str, langue: str, moteur,
                        client=None) -> dict[str, list[str]]:
    """Candidats extraits par un LLM, classés ``{essentiels, difficiles, termes}``.

    Repli : si le modèle répond par une liste plate (aucune catégorie reconnue),
    les termes sont placés dans « termes » plutôt que perdus.
    """
    if langue not in {"fr", "en"}:
        raise ErreurImportWikipedia("E005", f"Langue inconnue : {langue}")
    payload = _recuperer_page(titre, langue, client)
    texte = _texte_de_page(payload)
    try:
        reponse = str(moteur.traduire(
            _PROMPT_EXTRACTION_CATEGORISE.format(extrait=texte), {}) or "")
    except ErreurImportWikipedia:
        raise
    except Exception as exc:  # noqa: BLE001 — moteur absent/indisponible : erreur claire
        raise ErreurImportWikipedia("E010", f"Moteur LLM indisponible : {exc}") from exc
    classes = _parser_categorise(reponse)
    if not any(classes.values()):
        classes = {"essentiels": [], "difficiles": [],
                   "termes": _parser_llm(reponse)}
    return classes


def construire_moteur_llm(config: dict):
    """Instancie le moteur LLM choisi (config identique au panneau Traduire)."""
    from .traduction.engine import obtenir_moteur

    config = config or {}
    nom = str(config.get("modele") or "").strip()
    if not nom or nom == "deterministe":
        raise ErreurImportWikipedia(
            "E005",
            "Méthode LLM : choisissez un moteur (openai_compatible, ollama ou llama_cpp)")
    if nom == "openai_compatible":
        url = str(config.get("url") or "").strip()
        if not url or not url.startswith(("http://", "https://")):
            raise ErreurImportWikipedia(
                "E005",
                "Méthode LLM : URL de serveur OpenAI-compatible requise (http:// ou https://)")
    try:
        return obtenir_moteur(nom, {"url": config.get("url") or "",
                                    "cle_api": config.get("cle_api") or "",
                                    "modele": config.get("modele_api") or ""})
    except ValueError as exc:
        raise ErreurImportWikipedia("E005", str(exc)) from exc
