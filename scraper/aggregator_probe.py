"""
Probe de l'agrégateur d'annonces (#aglomerateur).

But : lister les webhooks « Channel Follower » (type 2) que Discord a créés dans
le salon agrégateur quand tu as SUIVI chaque salon d'annonces externe. Chaque
webhook porte DÉJÀ, sans qu'aucune annonce n'ait été publiée :
  - son `id`  = la clé de routage (les vraies annonces relayées porteront ce
                même webhook_id -> on route par lui, c'est stable) ;
  - `source_guild` / `source_channel` = le serveur + le salon d'origine.

=> on obtient la table source -> post de forum TOUT DE SUITE, sans attendre.

Prérequis :
  - secret DISCORD_BOT_TOKEN (le bot d'archive fait l'affaire) ;
  - le bot doit VOIR le salon #aglomerateur (View Channel) ET avoir le droit
    « Gérer les webhooks » (MANAGE_WEBHOOKS) sur ce salon.
  - AUCUN intent privilégié requis (c'est du REST, pas du gateway).

Env :
  DISCORD_BOT_TOKEN   le token du bot (Authorization: Bot <token>)
  AGG_CHANNEL_ID      id du salon agrégateur (défaut = #aglomerateur)
"""

import json
import os
import sys
import urllib.request
import urllib.error

API = "https://discord.com/api/v10"
CHANNEL_ID = os.environ.get("AGG_CHANNEL_ID", "1527591046505173072")

WH_TYPE = {1: "Incoming", 2: "Channel Follower", 3: "Application"}


def _get(path: str) -> object:
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        print("ERREUR : secret DISCORD_BOT_TOKEN absent.", file=sys.stderr)
        sys.exit(2)
    req = urllib.request.Request(
        API + path,
        headers={"Authorization": f"Bot {token}",
                 "User-Agent": "ScrapeurVeVe-aggregator-probe/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        print(f"ERREUR HTTP {e.code} sur {path} : {body}", file=sys.stderr)
        if e.code == 403:
            print("  -> 403 = le bot n'a pas le droit. Vérifie qu'il VOIT "
                  "#aglomerateur ET qu'il a « Gérer les webhooks » sur ce salon.",
                  file=sys.stderr)
        if e.code == 401:
            print("  -> 401 = token invalide (DISCORD_BOT_TOKEN).",
                  file=sys.stderr)
        sys.exit(1)


def main() -> int:
    hooks = _get(f"/channels/{CHANNEL_ID}/webhooks")
    if not isinstance(hooks, list):
        print(f"Réponse inattendue : {hooks!r}", file=sys.stderr)
        return 1
    followers = [h for h in hooks if h.get("type") == 2]
    print(f"Salon agrégateur {CHANNEL_ID} : {len(hooks)} webhook(s) au total, "
          f"{len(followers)} « Channel Follower ».\n")
    print("=" * 78)
    for h in hooks:
        t = WH_TYPE.get(h.get("type"), h.get("type"))
        sg = h.get("source_guild") or {}
        sc = h.get("source_channel") or {}
        print(f"webhook_id : {h.get('id')}")
        print(f"  type          : {t}")
        print(f"  nom affiche   : {h.get('name')}")
        print(f"  serveur src   : {sg.get('name')}  (id {sg.get('id')})")
        print(f"  salon src     : {sc.get('name')}  (id {sc.get('id')})")
        print("-" * 78)
    # bloc JSON brut compact (au cas où on veuille un champ non affiché)
    print("\n--- JSON brut (webhook_id -> source) ---")
    for h in followers:
        sg = (h.get("source_guild") or {}).get("name")
        sc = (h.get("source_channel") or {}).get("name")
        print(json.dumps({"webhook_id": h.get("id"), "name": h.get("name"),
                          "source_guild": sg, "source_channel": sc},
                         ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
