"""
CHANTIER B — raccorder le classement manuel de Preda (449 lignes, sheet separe)
aux `series_uuid` du catalogue VeVe. Ecrit l'onglet 🔗A-RACCORD du Sheet principal.

LE PROBLEME
-----------
Preda nomme ses comics a la main et court ("Spider-Man #14", "Into Mystery
Annual #1", "The X-Men #14"), VeVe les nomme "Amazing Spider-Man #14 (1963)" —
ou l'annee entre parentheses est celle du LANCEMENT DU VOLUME, pas de l'issue.
Un rapprochement par nom seul se trompe : "Spider-Man #14" peut etre
Amazing Spider-Man #14 (1963), (2022), Ultimate Spider-Man #14 (2024)...

LE VALIDATEUR : LE SUPPLY
-------------------------
Preda note le supply VeVe du drop dans sa colonne "Supply". On a maintenant le
supply VeVe reel (chantier A). Quand le numero d'issue ET le supply coincident,
le rapprochement n'est plus une ressemblance de texte : c'est une preuve.
C'est ce qui fait passer un match de "probable" a "certain".

STATUTS
-------
  CERTAIN   numero + supply identiques + nom coherent  -> rien a faire
  A_VERIFIER  nom tres proche mais supply different    -> Preda tranche
  AMBIGU    plusieurs candidats se valent              -> Preda choisit
  INTROUVABLE  aucun candidat plausible                -> nom a corriger

Rien n'est ECRIT dans le classement de Preda : ce script LIT son sheet et pose
le resultat dans un onglet du Sheet principal. Aucune de ses lignes ne bouge.

Env : GOOGLE_SERVICE_ACCOUNT_JSON, SHEET_ID, CLASSEMENT_SHEET_ID
      CLASSEMENT_TAB (defaut : le 1er onglet)
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import time
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

import gspread
import requests
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive.readonly"]

COMICS_TAB = "🟢C-COMICS"
RACCORD_TAB = "🔗A-RACCORD"

RACCORD_HEADER = [
    "ligne", "nom_preda", "note", "valeur_irl", "supply_preda", "fa_preda",
    "era_preda", "statut", "score", "series_uuid", "veve_series_name",
    "supply_veve", "prix_gems", "start_year", "cover_exclusive_veve",
    "veve_url", "alternatives", "valide",
]

# Titres que Preda ecrit en abrege / en francais. Le rapprochement se fait sur le
# titre NORMALISE : on remet les formes VeVe avant de comparer.
ALIASES = {
    "into mystery": "journey into mystery",
    "fear": "adventure into fear",
    "spider-man": "amazing spider-man",
    "spiderman": "amazing spider-man",
    "amazing spiderman": "amazing spider-man",
    "the x-men": "x-men",
    "uncanny x-men": "x-men",
    "the uncanny x-men": "x-men",
    "hero for hire": "luke cage, hero for hire",
    "tales to astonish": "tales to astonish",
    "tales of suspense": "tales of suspense",
}

_NUM_RE = re.compile(r"#\s*(\d+)")
_YEAR_RE = re.compile(r"\((\d{4})\)\s*$")
_ANNUAL_RE = re.compile(r"\bannual\b", re.IGNORECASE)


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def _norm(s: Any) -> str:
    """Titre comparable : sans accents, sans annee, sans #numero, sans ponctuation."""
    s = _strip_accents(str(s or "")).lower()
    s = _YEAR_RE.sub("", s)
    s = _NUM_RE.sub("", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if s.startswith("the "):
        s = s[4:]
    return ALIASES.get(s, s)


def _issue(s: Any) -> Optional[int]:
    m = _NUM_RE.search(str(s or ""))
    return int(m.group(1)) if m else None


def _year(s: Any) -> Optional[int]:
    m = _YEAR_RE.search(str(s or "").strip())
    return int(m.group(1)) if m else None


def _num(x: Any) -> Optional[float]:
    if x in (None, ""):
        return None
    try:
        return float(str(x).replace(" ", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


# ---------------------------------------------------------------------------
# Rapprochement
# ---------------------------------------------------------------------------

def match_one(nom: str, supply: Optional[float], era: str,
              by_issue: Dict[int, List[Dict[str, Any]]]) -> Tuple[str, float, List[Dict]]:
    """Renvoie (statut, score, candidats classes)."""
    issue = _issue(nom)
    base = _norm(nom)
    annual = bool(_ANNUAL_RE.search(nom))

    pool = by_issue.get(issue, []) if issue is not None else []
    if not pool:
        return "INTROUVABLE", 0.0, []

    scored: List[Dict[str, Any]] = []
    for cand in pool:
        if bool(_ANNUAL_RE.search(cand["name"])) != annual:
            continue                       # un Annual n'est jamais l'issue reguliere
        r = _ratio(base, cand["base"])
        # le titre court de Preda est souvent INCLUS dans celui de VeVe
        if base and (base in cand["base"] or cand["base"] in base):
            r = max(r, 0.90)
        score = r
        same_supply = (supply is not None and cand["supply"] is not None
                       and abs(supply - cand["supply"]) < 0.5)
        if same_supply:
            score += 0.35                  # LA preuve : le supply VeVe ne s'invente pas
        scored.append({**cand, "score": round(min(score, 1.35), 3),
                       "titre_score": round(r, 3), "same_supply": same_supply})

    if not scored:
        return "INTROUVABLE", 0.0, []
    scored.sort(key=lambda c: -c["score"])
    best = scored[0]

    # ORDRE DES TESTS : on ecarte D'ABORD les titres qui n'ont rien a voir. Sinon
    # "Zorglub #14" ressortait AMBIGU entre deux Spider-Man — deux mauvais
    # candidats qui se valent, ce n'est pas une ambiguite, c'est une absence.
    if best["same_supply"]:
        statut = "CERTAIN" if best["titre_score"] >= 0.75 else "A_VERIFIER"
    elif best["titre_score"] < 0.60:
        statut = "INTROUVABLE"
    elif len(scored) > 1 and scored[1]["score"] >= best["score"] - 0.05:
        statut = "AMBIGU"
    elif best["titre_score"] >= 0.75:
        statut = "A_VERIFIER"
    else:
        statut = "INTROUVABLE"
    return statut, best["score"], scored[:3]


# ---------------------------------------------------------------------------

def _sheet_id(raw: str) -> str:
    """Accepte l'ID nu OU l'URL complete du sheet."""
    raw = str(raw or "").strip()
    m = re.search(r"/d/([a-zA-Z0-9_-]{20,})", raw)
    return m.group(1) if m else raw


def read_classement(classement_id: str, tab: str = "") -> List[Dict[str, Any]]:
    """Lit le classement de Preda en CSV PUBLIC (gviz), sans authentification.

    POURQUOI PAS gspread : le compte de service du pipeline n'est pas partage sur
    ce sheet-la, et l'API repond alors une page d'erreur HTML que gspread essaie
    de parser en JSON (le `JSONDecodeError` du 1er run). Le sheet etant deja
    accessible par lien, gviz suffit — et il n'y a aucun partage a gerer.
    """
    url = (f"https://docs.google.com/spreadsheets/d/{classement_id}"
           f"/gviz/tq?tqx=out:csv")
    if tab:
        url += f"&sheet={requests.utils.quote(tab)}"
    r = requests.get(url, timeout=30)
    if r.status_code != 200 or r.text.lstrip().startswith("<"):
        raise RuntimeError(
            f"Classement illisible (HTTP {r.status_code}). Verifier l'ID et que le "
            f"sheet est bien partage « tous ceux qui ont le lien ».")
    rows = list(csv.DictReader(io.StringIO(r.text)))
    return [r for r in rows if str(r.get("Nom", "") or "").strip()]


def _client() -> gspread.Client:
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON manquant.")
    return gspread.authorize(
        Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES))


def load_veve_series(sh) -> Dict[int, List[Dict[str, Any]]]:
    """Le catalogue comics, regroupe par SERIE puis indexe par numero d'issue."""
    rows = sh.worksheet(COMICS_TAB).get_all_records()
    series: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        uid = str(r.get("series_uuid", "") or "").strip()
        if not uid or uid in series:
            continue
        name = str(r.get("veve_series_name", "") or "")
        series[uid] = {
            "series_uuid": uid,
            "name": name,
            "base": _norm(name),
            "issue": _issue(name),
            "supply": _num(r.get("supply")),
            "gems": _num(r.get("store_price_gems")),
            "start_year": r.get("start_year"),
            "exclusive": r.get("veve_exclusive"),
            "url": r.get("veve_url"),
        }
    by_issue: Dict[int, List[Dict[str, Any]]] = {}
    for s in series.values():
        if s["issue"] is not None:
            by_issue.setdefault(s["issue"], []).append(s)
    print(f"Catalogue : {len(series)} series comics, "
          f"{sum(1 for s in series.values() if s['supply'])} avec un supply.", flush=True)
    return by_issue


def main() -> int:
    t0 = time.time()
    sheet_id = os.environ.get("SHEET_ID")
    classement_id = _sheet_id(os.environ.get("CLASSEMENT_SHEET_ID", ""))
    if not sheet_id or not classement_id:
        print("ERROR: SHEET_ID et CLASSEMENT_SHEET_ID sont requis.", file=sys.stderr)
        return 2
    gc = _client()
    sh = gc.open_by_key(sheet_id)

    # --- le classement manuel de Preda (lecture seule, CSV public) ---
    tab = os.environ.get("CLASSEMENT_TAB", "").strip()
    prows = read_classement(classement_id, tab)
    print(f"Classement de Preda : {len(prows)} lignes"
          f"{f' (onglet « {tab} »)' if tab else ''}.", flush=True)

    by_issue = load_veve_series(sh)

    out: List[List[Any]] = []
    stats = {"CERTAIN": 0, "A_VERIFIER": 0, "AMBIGU": 0, "INTROUVABLE": 0}
    for i, r in enumerate(prows):
        nom = str(r.get("Nom", "")).strip()
        supply = _num(r.get("Supply"))
        statut, score, cands = match_one(nom, supply, str(r.get("Date (Era)", "")),
                                         by_issue)
        stats[statut] += 1
        # INTROUVABLE : on n'affiche AUCUN candidat. Proposer le "moins pire" sur
        # une ligne qu'on a jugee introuvable, c'est exactement comme ca qu'un
        # mauvais uuid finit par etre valide d'un clic.
        best = cands[0] if (cands and statut != "INTROUVABLE") else {}
        alts = "" if statut == "INTROUVABLE" else " | ".join(
            f"{c['name']} (supply {c['supply']:.0f}, score {c['score']})"
            for c in cands[1:] if c.get("supply") is not None)
        out.append([
            i + 2, nom, r.get("Categorie", ""), r.get("Value Irl (Est.)", ""),
            supply if supply is not None else "",
            r.get("FA (First Appearance) / Key", ""), r.get("Date (Era)", ""),
            statut, round(score, 3),
            best.get("series_uuid", ""), best.get("name", ""),
            best.get("supply", "") if best.get("supply") is not None else "",
            best.get("gems", "") if best.get("gems") is not None else "",
            best.get("start_year", ""), best.get("exclusive", ""),
            best.get("url", ""), alts, "",
        ])

    try:
        ws = sh.worksheet(RACCORD_TAB)
        ws.clear()
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=RACCORD_TAB, rows=len(out) + 10,
                              cols=len(RACCORD_HEADER))
    ws.update(range_name="A1", values=[RACCORD_HEADER] + out,
              value_input_option="RAW")
    try:
        ws.format("1:1", {"textFormat": {"bold": True}})
        ws.freeze(rows=1)
    except Exception as e:
        print(f"format warning: {e}", flush=True)

    n = len(out)
    print(f"\n🔗A-RACCORD ecrit : {n} lignes en {time.time() - t0:.0f}s", flush=True)
    for k, v in stats.items():
        print(f"  {k:12s} {v:4d}  ({v * 100 // max(n, 1)} %)", flush=True)
    print("\nColonne `valide` : ecris l'uuid retenu (ou 'ok') sur les lignes "
          "A_VERIFIER / AMBIGU. Le chantier C ne lira que ce qui est CERTAIN ou "
          "valide a la main.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
