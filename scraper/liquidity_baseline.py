"""liquidity_baseline — le CONSOMMATEUR de la baseline de LIQUIDITE (jetonveve).

Meme philosophie que price_baseline : au lieu de RECALCULER la liquidite a
chaque run en paginant getVeveTransactions (StackR), on LIT un petit CSV
pre-calcule depuis l'entrepot (transferts on-chain kind='market'). Le fichier
est produit hors reseau par outils/construire_liquidite.py, une fois par jour.

Ce que la baseline repond : « cet item S'EST-IL VENDU pour de vrai, et
recemment ? » — la preuve exigee par REQUIRE_SALE pour qu'un arbitrage
« revendre au floor VeVe » ne soit pas une fiction. Elle ne porte PAS de prix
(la chaine n'en enregistre pas) : le prix de revente continue de venir du floor
et du flux de ventes live. La baseline ne fait qu'AJOUTER de la preuve — si le
fichier manque, load_liquidity() renvoie {} et floor_watch retombe EXACTEMENT
sur son comportement actuel (fetch_history). Ne leve JAMAIS.

Contenu d'une ligne (1 / uuid vendu au moins une fois) :
  veve_uuid, n_sales_30d, n_sales_90d, n_sales_total, last_sale_date,
  sales_per_day_90d
"""

from __future__ import annotations

import csv
import gzip
import io
import os
from typing import Dict, Optional

try:
    import requests
except Exception:                       # test unitaire pur, hors reseau
    requests = None                     # type: ignore

# Release publique par defaut (a creer : tag `liquidity-full`). Repli -prev.
DEFAULT_URL = ("https://github.com/fanablefrance/jetonveve/releases/download/"
               "liquidity-full/liquidity_baselines.csv.gz")
PREV_URL = DEFAULT_URL.replace("liquidity-full/", "liquidity-full-prev/")

_INT_COLS = ("n_sales_30d", "n_sales_90d", "n_sales_total")


def _i(x) -> int:
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return 0


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _parse(text: str) -> Dict[str, Dict]:
    out: Dict[str, Dict] = {}
    for r in csv.DictReader(io.StringIO(text)):
        uid = (r.get("veve_uuid") or "").strip()
        if not uid:
            continue
        rec: Dict[str, object] = {c: _i(r.get(c)) for c in _INT_COLS}
        rec["last_sale_date"] = (r.get("last_sale_date") or "").strip()
        rec["sales_per_day_90d"] = _f(r.get("sales_per_day_90d"))
        out[uid] = rec
    return out


def load_liquidity(source: Optional[str] = None,
                   timeout: int = 40) -> Dict[str, Dict]:
    """Charge {uuid -> ligne}. `source` = URL http(s) OU chemin local .csv[.gz].
    Sur URL : tente l'URL, puis -prev, sinon {}. Sur fichier absent : {}.
    Ne leve JAMAIS — une baseline manquante rend juste la preuve indisponible,
    et floor_watch retombe sur fetch_history.
    """
    source = source or os.environ.get("LIQUIDITY_SRC") or DEFAULT_URL
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
                data = r.content
                if url.endswith(".gz"):
                    data = gzip.decompress(data)
                return _parse(data.decode("utf-8"))
        except Exception:
            continue
    return {}


def est_liquide(rec: Optional[Dict], min_sales: int = 1,
                fenetre: str = "n_sales_90d") -> bool:
    """True si l'item a AU MOINS `min_sales` ventes reelles dans la fenetre.
    `fenetre` : 'n_sales_30d', 'n_sales_90d' (defaut) ou 'n_sales_total'."""
    if not rec:
        return False
    return _i(rec.get(fenetre)) >= max(1, min_sales)
