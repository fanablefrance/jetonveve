"""🚨 ALERTES SOUS-FLOOR — surveillance rapprochée du floor (12/07/2026).

IDEE CLE (objection de Preda, juste) : un floor rafraichi une fois par jour ne
sert a rien pour attraper une offre qui part en dix minutes. Mais la chaine ne
donne PAS les prix (le dépôt escrow dit qui liste quoi, jamais a combien).

La sortie du probleme : **une offre placee sous le floor DEVIENT le floor**.
Il suffit donc de surveiller le floor de pres — pas besoin du prix de chaque
offre.

SOURCE (sondee le 12/07) : `publicVeve.getElements` — PUBLIC, sans cookie :
    GET https://www.stackr.world/api/trpc/publicVeve.getElements
        ?input={"json":{"limit":100,"page":<n>}}   <-- `page`, PAS `cursor` !
  -> id (= veve_uuid), name, rarity, edition, quantity, volume,
     **floor_market_price**, market_cap, totalCount (6 011 elements).
  Verifie : le floor de StackR est EXACTEMENT le notre (Sea Queen = 1 100 000
  des deux cotes, meme unite que market_lowestOffer dans 🟠H-PRIX).
  limit > 100 est refuse -> un balayage complet = ~61 requetes (~1 minute).
  NB : les parametres de tri/filtre n'ont pas ete devines (search/sort ignores)
  -> on balaye tout. Si une capture DevTools de stackr.world revele les vrais
  noms de parametres, on pourra ne surveiller que le haut du panier.

ALERTE : floor_nouveau <= floor_precedent x (1 - DROP_PCT/100) -> message
Discord (webhook). Anti-bruit : cooldown par item, floor plancher, et on ignore
la premiere observation d'un item (pas de reference = pas d'alerte).

RESPECTE LA REGLE DES COLLECTEURS LONGS : etat persistant (data/floor_state.json),
recolte jamais perdue (une page morte est sautee), backoff exponentiel, et un
balayage trop troue n'ecrase JAMAIS l'etat (sinon les items manquants
paraitraient s'effondrer au tour suivant).

Env : DISCORD_WEBHOOK (sinon : simulation dans les logs), FLOOR_DROP_PCT (20),
      FLOOR_MIN (100 = ignore les items a floor derisoire),
      FLOOR_COOLDOWN_H (6), FLOOR_SWEEPS (5 = nombre de balayages par run),
      FLOOR_INTERVAL_S (600 = 10 min entre deux balayages),
      FLOOR_STATE (data/floor_state.json), FLOOR_LIMIT (100),
      FLOOR_MAX_MISSING_PCT (10 = au-dela, le balayage est juge trop troue).
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import time
import urllib.parse
from typing import Dict, List, Optional

import requests

TRPC = "https://www.stackr.world/api/trpc/publicVeve.getElements?input="
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

STATE_PATH = os.environ.get("FLOOR_STATE", "data/floor_state.json")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()
DROP_PCT = float(os.environ.get("FLOOR_DROP_PCT", "20"))
FLOOR_MIN = float(os.environ.get("FLOOR_MIN", "100"))
COOLDOWN_H = float(os.environ.get("FLOOR_COOLDOWN_H", "6"))
SWEEPS = int(os.environ.get("FLOOR_SWEEPS", "5"))
INTERVAL_S = int(os.environ.get("FLOOR_INTERVAL_S", "600"))
LIMIT = int(os.environ.get("FLOOR_LIMIT", "100"))
MAX_MISSING_PCT = float(os.environ.get("FLOOR_MAX_MISSING_PCT", "10"))
RETRIES = int(os.environ.get("FLOOR_RETRIES", "6"))
TIMEOUT = int(os.environ.get("FLOOR_TIMEOUT", "45"))
PAUSE = float(os.environ.get("FLOOR_PAUSE", "0.2"))


def _now() -> float:
    return time.time()


def fetch_page(page: int, session=None, limit: int = LIMIT):
    """Une page d'elements. None = echec definitif (la page sera SAUTEE).

    PAGINATION (verifiee le 12/07) : le parametre est **`page`** (1-based).
    `cursor`, `offset` et `skip` sont IGNORES en silence par cet endpoint —
    avec `cursor` on relisait 61 fois la meme page sans s'en apercevoir.
    D'ou l'auto-controle de `sweep()` : si la page 2 renvoie les memes items
    que la page 1, on ARRETE au lieu de croire qu'on surveille 6 011 items."""
    payload = {"limit": limit}
    if page > 1:
        payload["page"] = page
    url = TRPC + urllib.parse.quote(
        json.dumps({"json": payload}, separators=(",", ":")))
    s = session or requests
    for attempt in range(RETRIES):
        try:
            r = s.get(url, headers={"User-Agent": UA,
                                    "Accept": "application/json"},
                      timeout=TIMEOUT)
            if r.status_code >= 500:
                raise RuntimeError(f"HTTP {r.status_code}")
            r.raise_for_status()
            d = r.json()
            return (d.get("result", {}).get("data", {})
                    .get("json", {}) or {})
        except Exception as e:
            if attempt == RETRIES - 1:
                print(f"    page {page} abandonnee : {e}", flush=True)
                return None
            wait = min(60, 3 * (2 ** attempt))
            print(f"    page {page} : {e} — nouvel essai dans {wait} s",
                  flush=True)
            time.sleep(wait)
    return None


def sweep(session=None) -> Dict[str, Dict]:
    """Un balayage complet : {uuid -> {name, floor, rarity, qty, volume}}."""
    s = session or requests.Session()
    out: Dict[str, Dict] = {}
    total = None
    page_no = 1
    failed = 0
    premiere: Optional[str] = None
    while True:
        page = fetch_page(page_no, s)
        if page is None:
            failed += 1
            page_no += 1
            if total and (page_no - 1) * LIMIT >= total:
                break
            continue
        rows = page.get("data") or []
        if total is None:
            total = int(page.get("totalCount") or 0)
        if not rows:
            break
        # AUTO-CONTROLE : la page 2 doit ramener d'AUTRES items que la page 1.
        # (Si un jour StackR renomme le parametre de pagination, on s'en rend
        # compte immediatement au lieu de surveiller 100 items en croyant en
        # surveiller 6 011.)
        tete = str(rows[0].get("id") or "")
        if page_no == 1:
            premiere = tete
        elif tete == premiere:
            print("  !! PAGINATION CASSEE : la page 2 renvoie la page 1 "
                  "(le parametre `page` n'est plus pris en compte ?) — "
                  "balayage abandonne, aucune alerte ne sera emise.",
                  flush=True)
            return {}
        for e in rows:
            uid = str(e.get("id") or "")
            if not uid:
                continue
            try:
                floor = float(e.get("floor_market_price") or 0)
            except (TypeError, ValueError):
                floor = 0.0
            out[uid] = {"name": e.get("name") or uid[:8],
                        "floor": floor,
                        "rarity": e.get("rarity") or "",
                        "qty": e.get("quantity") or 0,
                        "volume": e.get("volume") or 0}
        page_no += 1
        if total and len(out) >= total:
            break
        if page_no > 200:                   # garde-fou
            break
        time.sleep(PAUSE)
    if failed:
        print(f"  {failed} page(s) sautee(s) — balayage partiel.", flush=True)
    return out


def load_state() -> Dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"floors": {}, "alerts": {}}


def save_state(st: Dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f)


def detect(state: Dict, now_map: Dict[str, Dict],
           ts: float = None) -> List[Dict]:
    """Compare le balayage a l'etat -> alertes. Met l'etat a jour.

    Une baisse >= DROP_PCT % du floor = quelqu'un vient de lister SOUS le floor.
    Regles anti-bruit : on ignore un item vu pour la premiere fois (aucune
    reference), les floors derisoires (< FLOOR_MIN) et les items deja alertes
    depuis moins de COOLDOWN_H heures."""
    ts = ts if ts is not None else _now()
    floors: Dict[str, float] = state.setdefault("floors", {})
    alerts: Dict[str, float] = state.setdefault("alerts", {})
    out: List[Dict] = []
    for uid, e in now_map.items():
        new = e["floor"]
        old = floors.get(uid)
        if new > 0:
            floors[uid] = new
        if old is None or old <= 0 or new <= 0:
            continue                        # pas de reference / plus d'offre
        if old < FLOOR_MIN:
            continue
        if new > old * (1.0 - DROP_PCT / 100.0):
            continue                        # pas assez decote
        last = alerts.get(uid, 0)
        if ts - last < COOLDOWN_H * 3600:
            continue                        # deja signale recemment
        alerts[uid] = ts
        out.append({"uuid": uid, "name": e["name"], "rarity": e["rarity"],
                    "old": old, "new": new,
                    "drop": round(100.0 * (old - new) / old, 1),
                    "qty": e["qty"], "volume": e["volume"]})
    out.sort(key=lambda a: -a["drop"])
    return out


def notify(alerts: List[Dict]) -> int:
    """Pousse les alertes sur Discord (ou les simule dans les logs)."""
    if not alerts:
        return 0
    lignes = []
    for a in alerts[:10]:
        url = f"https://www.stackr.world/element/{a['uuid']}"
        lignes.append(
            f"**{a['name']}** ({a['rarity']}) — floor **−{a['drop']} %** : "
            f"{a['old']:,.0f} → **{a['new']:,.0f}** gems\n<{url}>"
            .replace(",", " "))
    corps = ("🚨 **SOUS LE FLOOR** — " + _dt.datetime.now(
        _dt.timezone.utc).strftime("%H:%M UTC") + "\n\n" + "\n\n".join(lignes))
    if len(alerts) > 10:
        corps += f"\n\n… et {len(alerts) - 10} autre(s)."
    if not WEBHOOK:
        print("  [SIMULATION — pas de DISCORD_WEBHOOK]\n" + corps, flush=True)
        return len(alerts)
    try:
        r = requests.post(WEBHOOK, json={"content": corps[:1900]}, timeout=20)
        r.raise_for_status()
        print(f"  Discord : {len(alerts)} alerte(s) poussee(s).", flush=True)
    except Exception as e:
        print(f"  Discord KO ({e}) — alertes affichees ici :\n{corps}",
              flush=True)
    return len(alerts)


def main() -> int:
    t0 = _now()
    state = load_state()
    n_alertes = 0
    s = requests.Session()
    for i in range(1, SWEEPS + 1):
        print(f"Balayage {i}/{SWEEPS}...", flush=True)
        now_map = sweep(s)
        connus = len(state.get("floors") or {})
        if not now_map:
            print("  balayage vide — on garde l'etat et on reessaie.",
                  flush=True)
        else:
            # Un balayage trop troue n'ecrase PAS l'etat : sinon les items
            # manquants ressortiraient comme des effondrements au tour suivant.
            manquants = (100.0 * max(0, connus - len(now_map)) / connus
                         if connus else 0)
            if manquants > MAX_MISSING_PCT:
                print(f"  balayage trop incomplet ({manquants:.0f} % des items "
                      f"connus absents) — IGNORE (etat preserve).", flush=True)
            else:
                alertes = detect(state, now_map)
                print(f"  {len(now_map)} elements, {len(alertes)} alerte(s).",
                      flush=True)
                n_alertes += notify(alertes)
                save_state(state)
        if i < SWEEPS:
            time.sleep(INTERVAL_S)
    print(f"Termine : {SWEEPS} balayage(s), {n_alertes} alerte(s), "
          f"{len(state.get('floors') or {})} floors suivis, "
          f"{_now() - t0:.0f}s.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

# FIN floor_watch.py v2 (pagination `page` + auto-controle)
