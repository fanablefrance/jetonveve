"""🎯 LA CHASSE AUX NUMÉROS — ce qui rend une édition remarquable.

Un NFT VeVe porte un numéro d'édition (#1, #1234, #2001…). Certains numéros
valent BIEN plus que les autres — et le vendeur ne le voit pas toujours. C'est
exactement là qu'est l'affaire.

═══ CE QUI COMPTE (et pourquoi) ═══
  * **1ʳᵉ édition publique** — VeVe retient des éditions ; la 1ʳᵉ RÉELLEMENT
    vendue au public n'est presque jamais le #1. Elle est dans le catalogue
    (`first_available_edition`) : on ne la devine pas.
  * **bas numéros** (#1…#10) et **hauts numéros** (les 10 derniers du tirage) —
    les extrémités d'un tirage sont recherchées.
  * **séquences** (1234, 6789, 4321) · **binaires** (1010, 1101) ·
    **angéliques** (111, 777, 1111) · **répétitions** (2222, 1212) ·
    **palindromes** (121, 1221, 12321).
  * **dates clés** — 1939 (Batman), 1977 (Star Wars), 1962 (Spider-Man)…
    ⚠️ Celles-là ne se DEVINENT pas : elles viennent d'un tableau que Preda
    tient à jour (`data/dates_cles.csv`). Le module n'invente aucune date — il
    ne saurait pas quand est né tel acteur, et une date fausse serait pire
    qu'une date absente.

═══ CE MODULE NE FAIT AUCUN RÉSEAU ═══
De la logique pure, donc testable jusqu'au dernier motif. Le module qui alerte
(`floor_watch`) s'occupe des prix ; celui-ci ne répond qu'à une question :
« ce numéro-là a-t-il quelque chose de remarquable ? »
"""

from __future__ import annotations

import csv
import os
import re
from typing import Dict, List, Optional, Tuple

# Les extremites du tirage
BAS = int(os.environ.get("MINT_BAS", "10"))          # #1 .. #10
HAUT = int(os.environ.get("MINT_HAUT", "10"))        # les 10 derniers

# Poids : tous les motifs ne se valent pas. Le #1 d'un tirage, ce n'est pas un
# 1010 parmi 10 000. Le score sert a TRIER, pas a decider seul.
POIDS = {
    "premiere_publique": 6,
    "numero_1": 6,
    "bas": 3,
    "haut": 2,
    "sequence": 3,
    "palindrome": 3,
    "repetition": 3,
    "angelique": 2,
    "binaire": 2,
    "date": 4,
}

# Les « nombres angeliques » les plus courus (au-dela des repdigits, deja
# couverts par `repetition`).
ANGELIQUES = {111, 222, 333, 444, 555, 666, 777, 888, 999,
              1010, 1111, 1212, 1221, 1234, 1313, 1414, 1515, 1616,
              1717, 1818, 1919, 2020, 2121, 2222, 3333, 4444, 5555,
              6666, 7777, 8888, 9999}


def _sequence(s: str) -> bool:
    """1234, 6789, 4321 — au moins 3 chiffres qui se suivent."""
    if len(s) < 3:
        return False
    ecarts = {int(s[i + 1]) - int(s[i]) for i in range(len(s) - 1)}
    return ecarts in ({1}, {-1})


def _palindrome(s: str) -> bool:
    return len(s) >= 3 and s == s[::-1]


def _repetition(s: str) -> bool:
    """2222 (repdigit) ou 1212 / 3434 (motif repete)."""
    if len(s) >= 3 and len(set(s)) == 1:
        return True
    if len(s) >= 4 and len(s) % 2 == 0:
        moitie = len(s) // 2
        if s[:moitie] == s[moitie:] and len(set(s)) > 1:
            return True
    return len(s) == 4 and s[0] == s[2] and s[1] == s[3] and s[0] != s[1]


def _binaire(s: str) -> bool:
    """1010, 1101 — que des 0 et des 1, et au moins un 0 (sinon c'est 111,
    qui est deja une repetition)."""
    return len(s) >= 3 and set(s) <= {"0", "1"} and "0" in s


def motifs(edition: int, supply: int = 0, premiere_publique: int = 0,
           annees: set = None) -> List[str]:
    """Tout ce qui rend CE numero remarquable. Liste vide = numero banal."""
    out: List[str] = []
    if edition <= 0:
        return out
    s = str(edition)
    annees = annees or set()

    if premiere_publique and edition == premiere_publique:
        out.append("premiere_publique")
    if edition == 1:
        out.append("numero_1")
    elif edition <= BAS:
        out.append("bas")
    if supply and edition > supply - HAUT and edition <= supply:
        out.append("haut")

    if _sequence(s):
        out.append("sequence")
    if _palindrome(s):
        out.append("palindrome")
    if _repetition(s):
        out.append("repetition")
    if edition in ANGELIQUES and "repetition" not in out:
        out.append("angelique")
    if _binaire(s):
        out.append("binaire")
    if edition in annees:
        out.append("date")
    return out


def score(mot: List[str]) -> int:
    return sum(POIDS.get(m, 1) for m in mot)


LIBELLES = {
    "premiere_publique": "1ʳᵉ édition publique",
    "numero_1": "le #1",
    "bas": "bas numéro",
    "haut": "haut numéro",
    "sequence": "séquence",
    "palindrome": "palindrome",
    "repetition": "répétition",
    "angelique": "nombre angélique",
    "binaire": "binaire",
    "date": "date clé",
}


def raconter(mot: List[str]) -> str:
    return " · ".join(LIBELLES.get(m, m) for m in mot)


# ---------------------------------------------------------------------------
# Les dates cles — un TABLEAU, jamais une devinette
# ---------------------------------------------------------------------------

DATES_CSV = os.environ.get("DATES_CSV", "data/dates_cles.csv")


def charger_dates(chemin: str = None) -> List[Tuple[str, set]]:
    """[(motif_de_nom_en_minuscules, {annees})] — lu depuis le CSV que Preda
    tient a jour. Absent = aucune date, et surtout AUCUNE invention."""
    out: List[Tuple[str, set]] = []
    try:
        with open(chemin or DATES_CSV, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                cle = (r.get("cle") or "").strip().lower()
                if not cle:
                    continue
                ans = {int(a) for a in re.findall(r"\d{4}",
                                                  r.get("annees") or "")}
                if ans:
                    out.append((cle, ans))
    except FileNotFoundError:
        pass
    return out


def annees_pour(textes: List[str], table: List[Tuple[str, set]]) -> set:
    """Les annees cles qui s'appliquent a cet element (on cherche la cle dans le
    nom, la serie, la marque, la licence)."""
    gros = " | ".join(t.lower() for t in textes if t)
    out: set = set()
    for cle, ans in table or []:
        if cle and cle in gros:
            out |= ans
    return out
