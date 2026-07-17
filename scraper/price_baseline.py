"""price_baseline — le CONSOMMATEUR des baselines de prix (jetonveve).

Lit le fichier COMPACT `prices_baselines.csv.gz` (publie par price_history en mode
baselines dans la release prices-full) et fournit aux alertes de VRAIES references
MULTI-ANNEES : ou se situe un floor dans l'histoire de son item (percentile), et si
le nombre d'offres s'ecarte de sa norme historique.

Pourquoi ce module (et pas des appels dans floor_watch)
------------------------------------------------------
L'entrepot veut que les consommateurs LISENT une reference pre-calculee par URL, au
lieu de recalculer a chaque run. `floor_watch` reconstruit aujourd'hui ses references
en paginant ~12 k tx/h ; ici il telecharge UN petit CSV (~18 900 lignes) une fois par
run et juge en memoire. Les fonctions sont PURES et testables hors reseau.

Contenu d'une baseline (1 ligne / uuid)
---------------------------------------
n_points, first_ts, last_ts,
floor_min, floor_p5, floor_p25, floor_p50, floor_p75, floor_p95, floor_max,
listings_p50, listings_p90, listings_max, last_floor, last_listings

⚠️ Les percentiles sont calcules sur les points ON-CHANGE (non ponderes par la duree)
-> une approximation « position dans l'histoire », a lire avec min/max, pas au chiffre
pres. Tout est en $ (meme unite que le floor VeVe de floor_watch — leçon v18 : jamais
deux unites).
"""

from __future__ import annotations

import csv
import gzip
import io
import os
import time
from typing import Dict, List, Optional

try:
    import requests
except Exception:                       # test unitaire pur
    requests = None                     # type: ignore

# URL publique par defaut (release prices-full). Repli -prev en secours.
DEFAULT_URL = ("https://github.com/fanablefrance/jetonveve/releases/download/"
               "prices-full/prices_baselines.csv.gz")
PREV_URL = DEFAULT_URL.replace("prices-full/", "prices-full-prev/")

_FLOAT_COLS = ("floor_min", "floor_p5", "floor_p25", "floor_p50", "floor_p75",
               "floor_p95", "floor_max", "listings_p50", "listings_p90",
               "listings_max", "last_floor", "last_listings")


def _f(x) -> Optional[float]:
    if x in (None, ""):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Chargement (URL ou fichier local), tolerant : jamais de crash de floor_watch
# ---------------------------------------------------------------------------

def _parse(text: str) -> Dict[str, Dict]:
    out: Dict[str, Dict] = {}
    for r in csv.DictReader(io.StringIO(text)):
        uid = (r.get("veve_uuid") or "").strip()
        if not uid:
            continue
        rec: Dict[str, object] = {"n_points": int(_f(r.get("n_points")) or 0)}
        for c in _FLOAT_COLS:
            rec[c] = _f(r.get(c))
        out[uid] = rec
    return out


def load_baselines(source: Optional[str] = None, timeout: int = 40) -> Dict[str, Dict]:
    """Charge {uuid -> baseline}. `source` = URL http(s) OU chemin local .csv[.gz].
    Sur URL : tente l'URL, puis -prev, sinon renvoie {} (alertes historiques
    simplement muettes ce run — jamais d'exception). Ne leve JAMAIS.
    """
    source = source or os.environ.get("BASELINES_SRC") or DEFAULT_URL
    # fichier local
    if not source.startswith("http"):
        try:
            op = gzip.open if source.endswith(".gz") else open
            with op(source, "rt", encoding="utf-8") as f:
                return _parse(f.read())
        except FileNotFoundError:
            return {}
        except Exception:
            return {}
    if requests is None:
        return {}
    for url in (source, PREV_URL if source == DEFAULT_URL else None):
        if not url:
            continue
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code == 200 and r.content:
                raw = gzip.decompress(r.content) if url.endswith(".gz") else r.content
                return _parse(raw.decode("utf-8"))
        except Exception:
            continue
    return {}


# ---------------------------------------------------------------------------
# Jugements PURS
# ---------------------------------------------------------------------------

def pct_rank(bl: Dict, floor: float) -> Optional[float]:
    """Percentile approx (0..100) de `floor` dans l'histoire de l'item, par
    interpolation lineaire sur l'echelle min/p5/p25/p50/p75/p95/max."""
    if not bl or floor is None:
        return None
    ladder = [(bl.get("floor_min"), 0.0), (bl.get("floor_p5"), 5.0),
              (bl.get("floor_p25"), 25.0), (bl.get("floor_p50"), 50.0),
              (bl.get("floor_p75"), 75.0), (bl.get("floor_p95"), 95.0),
              (bl.get("floor_max"), 100.0)]
    ladder = [(v, p) for v, p in ladder if v is not None]
    if not ladder:
        return None
    if floor <= ladder[0][0]:
        return 0.0
    if floor >= ladder[-1][0]:
        return 100.0
    for (v0, p0), (v1, p1) in zip(ladder, ladder[1:]):
        if v0 <= floor <= v1:
            if v1 == v0:
                return p0
            return p0 + (p1 - p0) * (floor - v0) / (v1 - v0)
    return None


def is_hist_low(bl: Dict, floor: float, pct: float = 10.0) -> bool:
    """True si le floor est dans les `pct` % les moins chers de son histoire."""
    r = pct_rank(bl, floor)
    return r is not None and r <= pct


def is_hist_high(bl: Dict, floor: float, pct: float = 90.0) -> bool:
    r = pct_rank(bl, floor)
    return r is not None and r >= pct


def vol_ratio(bl: Dict, listings: Optional[float]) -> Optional[float]:
    """Nb d'offres courant / mediane historique. >1 = plus d'offres que d'habitude."""
    if listings is None or not bl:
        return None
    med = bl.get("listings_p50")
    if not med or med <= 0:
        return None
    return listings / med


# ---------------------------------------------------------------------------
# Detecteurs prets a cabler dans floor_watch (memes conventions : state, cooldown,
# garde-fou MAX, plancher, preuve de vente). Tous OFF tant que `on=False`.
# ---------------------------------------------------------------------------

def detect_hist_low(state: Dict, veve: Dict[str, float], baselines: Dict[str, Dict],
                    cat: Optional[Dict] = None, sales: Optional[Dict] = None,
                    ts: Optional[float] = None, *, on: bool = False,
                    pct: float = 10.0, plancher: float = 1.0,
                    cooldown_h: float = 6.0, maxn: int = 10,
                    require_sale: bool = True, min_points: int = 30) -> List[Dict]:
    """📉📊 Un floor dans le BAS de sa distribution MULTI-ANNEES (percentile <= pct).
    Plus riche que l'ATL brut : capte la « zone basse » sans attendre le minimum
    absolu (trollable), et exige assez d'historique (min_points).
    `veve` = {uuid: floor_usd}. Preuve de vente exigee par defaut.
    """
    ts = ts if ts is not None else time.time()
    alerts = state.setdefault("alerts_histlow", {})
    cat = cat or {}
    sales = sales or {}
    out: List[Dict] = []
    for uid, vf_ in (veve or {}).items():
        vf = _f(vf_) or 0.0
        bl = baselines.get(uid)
        if not on or vf <= plancher or not bl:
            continue
        if (bl.get("n_points") or 0) < min_points:      # pas assez d'histoire
            continue
        rank = pct_rank(bl, vf)
        if rank is None or rank > pct:
            continue
        if require_sale:
            last = sales.get(uid)
            if not last or (_f(last[0]) or 0) <= 0:
                continue
        if ts - alerts.get(uid, 0) < cooldown_h * 3600:
            continue
        alerts[uid] = ts
        c = cat.get(uid) or {}
        out.append({"uuid": uid, "name": c.get("name") or uid[:8],
                    "categorie": c.get("categorie", ""), "floor": round(vf, 2),
                    "rank": round(rank, 1), "p5": bl.get("floor_p5"),
                    "min": bl.get("floor_min"), "p50": bl.get("floor_p50"),
                    "n": bl.get("n_points")})
    if len(out) > maxn:                                 # avalanche = seuil mal regle
        for a in out:
            alerts.pop(a["uuid"], None)
        return []
    out.sort(key=lambda a: a["rank"])
    return out


def detect_vol_anomaly(state: Dict, listings_map: Dict[str, float],
                       baselines: Dict[str, Dict], cat: Optional[Dict] = None,
                       ts: Optional[float] = None, *, on: bool = False,
                       ratio: float = 3.0, cooldown_h: float = 6.0,
                       maxn: int = 10, min_points: int = 30,
                       min_listings: int = 5) -> List[Dict]:
    """🔊 Le nombre d'OFFRES d'un item s'ecarte fortement de sa norme historique
    (>= ratio × mediane ET au-dela du p90). Cote OFFRE (pas ventes) : complete le
    🔥 pic (ventes) du Lot 3. `listings_map` = {uuid: nb_offres_courant}.
    """
    ts = ts if ts is not None else time.time()
    alerts = state.setdefault("alerts_vol", {})
    cat = cat or {}
    out: List[Dict] = []
    for uid, cur_ in (listings_map or {}).items():
        cur = _f(cur_)
        bl = baselines.get(uid)
        if not on or cur is None or cur < min_listings or not bl:
            continue
        if (bl.get("n_points") or 0) < min_points:
            continue
        med = bl.get("listings_p50") or 0
        p90 = bl.get("listings_p90") or 0
        if med <= 0 or cur < med * ratio or cur < p90:
            continue
        if ts - alerts.get(uid, 0) < cooldown_h * 3600:
            continue
        alerts[uid] = ts
        c = cat.get(uid) or {}
        out.append({"uuid": uid, "name": c.get("name") or uid[:8],
                    "categorie": c.get("categorie", ""), "listings": int(cur),
                    "med": med, "p90": p90, "ratio": round(cur / med, 1)})
    if len(out) > maxn:
        for a in out:
            alerts.pop(a["uuid"], None)
        return []
    out.sort(key=lambda a: -a["ratio"])
    return out
