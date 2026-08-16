"""Analyse syllabique locale (spec §5) — multilingue (slice 3).

Chaque langue a un ``LanguageAdapter`` : règles d'élision/contraction, digraphes
(« qu » français, « qu/gu » espagnol), « e » final muet (fr/en), et une table de
**nombres 0-99** convertis en mots avant comptage. Une langue sans adapter
retombe sur le comptage générique par groupes de voyelles — jamais d'erreur.

L'estimation est une approximation de diction (l'objectif est la compatibilité
temporelle d'une traduction, pas une phonologie exacte) : les lettres muettes
médianes (« ninety », « recipe ») ne sont pas toutes modélisées. Au-delà de 99,
un nombre est lu chiffre à chiffre (approximation documentée).
"""
from __future__ import annotations

import re

_VOYELLES = "aeiouyàâäéèêëîïôöùûüœæ"
_TOKEN = re.compile(r"[^\W\d_]+|\d+", re.UNICODE)

# Débit de diction approximatif (syllabes/seconde), commun aux 5 langues.
SYLLABES_PAR_SECONDE = 5.0

# Clitiques français : toujours élidés devant l'apostrophe → 0 syllabe.
_CLITIQUES_FR = frozenset(("j", "n", "l", "d", "m", "t", "s", "c", "qu",
                            "jusqu", "lorsqu", "puisqu", "presqu", "quoiqu"))


def _groupes_voyelles(mot: str) -> int:
    """Nombre de noyaux syllabiques (groupes de voyelles consécutives) d'un mot."""
    mot = mot.lower()
    compte = 0
    dans_groupe = False
    for caractere in mot:
        if caractere in _VOYELLES:
            if not dans_groupe:
                compte += 1
                dans_groupe = True
        else:
            dans_groupe = False
    return compte


def _e_final_muet(mot: str) -> bool:
    """Vrai si le « e » final (éventuellement suivi de « s ») est muet.

    Le « e » est muet quand il est précédé d'une consonne : il forme alors son
    propre groupe de voyelles en fin de mot (« homme », « parle », « one »).
    Précédé d'une voyelle (« rue », « armée »), il fait partie du noyau : pas
    de syllabe supplémentaire à retirer.
    """
    m = mot[:-1] if mot.endswith("s") else mot
    if not m.endswith("e"):
        return False
    return len(m) >= 2 and m[-2] not in _VOYELLES


def _sans_u_apres_q(mot: str) -> str:
    """Neutralise le « u » du digraphe « qu » (fr) : remplacé par une consonne."""
    return re.sub(r"qu", "q-", mot)


def _sans_u_apres_q_ou_g(mot: str) -> str:
    """Neutralise le « u » muet après « q » ou « g » (es)."""
    return re.sub(r"([qg])u", r"\1-", mot)


class LanguageAdapter:
    """Règles syllabiques d'une langue (contractions, digraphes, nombres)."""

    langue = ""
    unites: tuple[str, ...] = ()
    dizaines: dict[int, str] = {}

    # ------------------------------------------------------------------
    def syllabes_texte(self, texte: str) -> int:
        total = 0
        for token in _TOKEN.findall(texte or ""):
            if token.isdigit():
                total += self.nombre_en_syllabes(token)
            else:
                total += self.syllabes_mot(token)
        return total

    def syllabes_mot(self, mot: str) -> int:
        """Comptage par défaut : groupes de voyelles (repli générique)."""
        return _groupes_voyelles(mot)

    def nombre_en_syllabes(self, chiffres: str) -> int:
        return self.syllabes_texte(self.nombre_vers_mots(int(chiffres)))

    def nombre_vers_mots(self, n: int) -> str:
        """Nombre orthographié (0-99) ; au-delà, lecture chiffre à chiffre."""
        if n < 0:
            return ""
        if n < 100:
            return self._nombre_0_99(n)
        return " ".join(self._nombre_0_99(int(c)) for c in str(n))

    def _nombre_0_99(self, n: int) -> str:
        """Composition générique : dizaine-unité (en/de)."""
        if n < 20:
            return self.unites[n]
        d, u = divmod(n, 10)
        if u == 0:
            return self.dizaines[d]
        return self.dizaines[d] + "-" + self.unites[u]


# ------------------------------- français -------------------------------------

_UNITES_FR = ("zéro", "un", "deux", "trois", "quatre", "cinq", "six", "sept",
              "huit", "neuf", "dix", "onze", "douze", "treize", "quatorze",
              "quinze", "seize", "dix-sept", "dix-huit", "dix-neuf")
_DIZAINES_FR = {2: "vingt", 3: "trente", 4: "quarante", 5: "cinquante",
                6: "soixante"}


class FrenchAdapter(LanguageAdapter):
    langue = "fr"
    unites = _UNITES_FR
    dizaines = _DIZAINES_FR

    def syllabes_mot(self, mot: str) -> int:
        mot = mot.lower()
        if mot in _CLITIQUES_FR:
            return 0
        compte = _groupes_voyelles(_sans_u_apres_q(mot))
        if compte >= 2 and _e_final_muet(mot):
            compte -= 1
        return compte

    def _nombre_0_99(self, n: int) -> str:
        if n < 17:
            return self.unites[n]
        if n < 20:
            return "dix-" + self.unites[n - 10]
        d, u = divmod(n, 10)
        if d <= 6:
            if u == 0:
                return self.dizaines[d]
            if u == 1:
                return self.dizaines[d] + "-et-un"
            return self.dizaines[d] + "-" + self.unites[u]
        if d == 7:
            if u == 0:
                return "soixante-dix"
            if u == 1:
                return "soixante-et-onze"
            return "soixante-" + self.unites[10 + u]
        if d == 8:
            if u == 0:
                return "quatre-vingts"
            return "quatre-vingt-" + self.unites[u]
        # d == 9
        if u == 0:
            return "quatre-vingt-dix"
        return "quatre-vingt-" + self.unites[10 + u]


# ------------------------------- anglais --------------------------------------

_UNITES_EN = ("zero", "one", "two", "three", "four", "five", "six", "seven",
              "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen",
              "fifteen", "sixteen", "seventeen", "eighteen", "nineteen")
_DIZAINES_EN = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty", 6: "sixty",
                7: "seventy", 8: "eighty", 9: "ninety"}


class EnglishAdapter(LanguageAdapter):
    langue = "en"
    unites = _UNITES_EN
    dizaines = _DIZAINES_EN

    def syllabes_mot(self, mot: str) -> int:
        mot = mot.lower()
        compte = _groupes_voyelles(mot)
        if compte >= 2 and _e_final_muet(mot):
            compte -= 1
        return compte


# ------------------------------- espagnol ------------------------------------

_UNITES_ES = ("cero", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete",
              "ocho", "nueve", "diez", "once", "doce", "trece", "catorce",
              "quince", "dieciséis", "diecisiete", "dieciocho", "diecinueve")
_DIZAINES_ES = {2: "veinte", 3: "treinta", 4: "cuarenta", 5: "cincuenta",
                6: "sesenta", 7: "setenta", 8: "ochenta", 9: "noventa"}


class SpanishAdapter(LanguageAdapter):
    langue = "es"
    unites = _UNITES_ES
    dizaines = _DIZAINES_ES

    def syllabes_mot(self, mot: str) -> int:
        return _groupes_voyelles(_sans_u_apres_q_ou_g(mot.lower()))

    def _nombre_0_99(self, n: int) -> str:
        if n < 20:
            return self.unites[n]
        d, u = divmod(n, 10)
        if d == 2 and u:
            return "veinti" + self.unites[u]
        if u == 0:
            return self.dizaines[d]
        return self.dizaines[d] + " y " + self.unites[u]


# ------------------------------- allemand ------------------------------------

_UNITES_DE = ("null", "eins", "zwei", "drei", "vier", "fünf", "sechs", "sieben",
              "acht", "neun", "zehn", "elf", "zwölf", "dreizehn", "vierzehn",
              "fünfzehn", "sechzehn", "siebzehn", "achtzehn", "neunzehn")
_DIZAINES_DE = {2: "zwanzig", 3: "dreißig", 4: "vierzig", 5: "fünfzig",
                6: "sechzig", 7: "siebzig", 8: "achtzig", 9: "neunzig"}


class GermanAdapter(LanguageAdapter):
    langue = "de"
    unites = _UNITES_DE
    dizaines = _DIZAINES_DE

    def _nombre_0_99(self, n: int) -> str:
        if n < 20:
            return self.unites[n]
        d, u = divmod(n, 10)
        if u == 0:
            return self.dizaines[d]
        # séparé par des espaces : la jonction voyelle-voyelle entre le chiffre
        # des unités et « und » (« zwei und … ») ne forme pas un seul noyau.
        return self.unites[u] + " und " + self.dizaines[d]


# ------------------------------- italien -------------------------------------

_UNITES_IT = ("zero", "uno", "due", "tre", "quattro", "cinque", "sei", "sette",
              "otto", "nove", "dieci", "undici", "dodici", "tredici",
              "quattordici", "quindici", "sedici", "diciassette", "diciotto",
              "diciannove")
_DIZAINES_IT = {2: "venti", 3: "trenta", 4: "quaranta", 5: "cinquanta",
                6: "sessanta", 7: "settanta", 8: "ottanta", 9: "novanta"}


class ItalianAdapter(LanguageAdapter):
    langue = "it"
    unites = _UNITES_IT
    dizaines = _DIZAINES_IT

    def _nombre_0_99(self, n: int) -> str:
        if n < 20:
            return self.unites[n]
        d, u = divmod(n, 10)
        if u == 0:
            return self.dizaines[d]
        if u in (1, 8):
            # « ventuno », « ventotto » : la voyelle finale de la dizaine tombe
            return self.dizaines[d][:-1] + self.unites[u]
        return self.dizaines[d] + self.unites[u]


# ------------------------------ registre --------------------------------------

_ADAPTERS: dict[str, type[LanguageAdapter]] = {
    "fr": FrenchAdapter,
    "en": EnglishAdapter,
    "es": SpanishAdapter,
    "de": GermanAdapter,
    "it": ItalianAdapter,
}


def obtenir_adapter(langue: str | None) -> LanguageAdapter:
    """Adapter de ``langue`` ; langue inconnue/absente → repli générique."""
    classe = _ADAPTERS.get((langue or "").lower())
    return classe() if classe else LanguageAdapter()


class SyllableAnalyzer:
    """Comptage et estimation de durée syllabiques d'un texte, par langue."""

    def __init__(self, langue: str | None = None):
        self.langue = langue
        self.adapter = obtenir_adapter(langue)

    def compter(self, texte: str) -> int:
        """Nombre de syllabes estimé de ``texte`` (mots, contractions, nombres)."""
        return self.adapter.syllabes_texte(texte)

    def estimer_duree(self, nb_syllabes: int) -> float:
        """Durée de diction estimée (s) pour ``nb_syllabes`` syllabes."""
        return float(nb_syllabes) / SYLLABES_PAR_SECONDE
