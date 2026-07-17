"""
Routeur d'annonces : #aglomerateur -> le bon post de forum.

Lit les nouveaux messages du salon agrégateur (où les salons d'annonces externes
sont RELAYÉS par Discord via des webhooks « Channel Follower »), et re-poste
chaque annonce dans le post de forum correspondant, identifié par le
`webhook_id` du message relayé (clé stable, cf. probe).

Choix Preda (17/07) :
  - attribution EN TÊTE : « 📢 **Source** » puis le contenu + embeds ;
  - images JUSTE LIÉES (on passe les URL, pas de re-upload) — ⚠️ les URL Discord
    expirent ~24 h, mais on reposte dans les minutes qui suivent donc l'aperçu
    se génère au moment du post.

Tourne sur jetonveve (bot d'archive : token + MESSAGE CONTENT INTENT déjà là ;
lire le CONTENU des messages EXIGE cet intent). Poste via le bot token dans le
thread (POST /channels/{thread_id}/messages ; le bot doit avoir « Envoyer des
messages dans les fils » sur le salon forum, et Voir + Lire l'historique sur
#aglomerateur).

Env :
  DISCORD_BOT_TOKEN   token du bot (Authorization: Bot ...)
  AGG_CHANNEL_ID      salon agrégateur (défaut #aglomerateur)
  ROUTER_STATE        fichier d'état (défaut data/announce_router_state.json)
  ROUTER_MAX          garde-fou : messages max traités par run (défaut 50)
  ROUTER_DRY          "1" = ne poste rien, log seulement (réglage/essai)
  ROUTER_RESET        "1" = ré-ancrer le watermark au dernier message, rien poster
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error

API = "https://discord.com/api/v10"
AGG = os.environ.get("AGG_CHANNEL_ID", "1527591046505173072")
STATE = os.environ.get("ROUTER_STATE", "data/announce_router_state.json")
MAXN = int(os.environ.get("ROUTER_MAX", "50"))
DRY = os.environ.get("ROUTER_DRY", "").strip() in ("1", "true", "oui")
RESET = os.environ.get("ROUTER_RESET", "").strip() in ("1", "true", "oui")

# webhook_id -> (post de forum destination, nom de source pour l'attribution).
# Verrouillé par le probe du 17/07. Ajouter une ligne après tout nouveau suivi.
ROUTES = {
    "1527591359660425267": ("1526846817931755652", "Candy"),            # dc-announcements   -> 🔴 CANDY
    "1527591460231188530": ("1526846817931755652", "Candy"),            # general-announce.  -> 🔴 CANDY
    "1527592798289920161": ("1526822928606564493", "ECOMI"),            # omi-announcements  -> 🔵 VEVE
    "1527593633182650430": ("1526822928606564493", "Feeshes"),          # announcements      -> 🔵 VEVE
    "1527592876442128424": ("1526848348064055397", "Fanable"),          # announcements      -> 🃏 FANABLE
    "1527593669211979818": ("1526845492842070066", "McFarlane Toys"),   # announcements      -> 🟤 MCFARLANE
    "1527593840540913724": ("1526847942101827685", "Digitoys"),         # announcements      -> 🟠 DIGITOYS
    "1527593888359911494": ("1526845908795392001", "Disney Pinnacle"),  # announcements      -> 🧷 DISNEY PINNACLE
    "1527593970350293114": ("1526847750749028352", "ElmonX"),           # announcements      -> ⚪ ELMON X
}

TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
_HDRS = {"Authorization": f"Bot {TOKEN}",
         "User-Agent": "ScrapeurVeVe-announce-router/1.0",
         "Content-Type": "application/json"}


def _req(method: str, path: str, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(API + path, data=data, headers=_HDRS,
                                 method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode("utf-8")
        return r.status, (json.loads(body) if body else None)


def _call(method: str, path: str, payload=None, tries: int = 5):
    """Appel avec gestion du 429 (retry_after) et des erreurs réseau."""
    for i in range(tries):
        try:
            return _req(method, path, payload)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code == 429:
                try:
                    wait = float(json.loads(body).get("retry_after", 1.0))
                except Exception:
                    wait = 1.0
                print(f"    429 rate-limit, pause {wait:.1f}s", flush=True)
                time.sleep(wait + 0.3)
                continue
            if e.code in (401, 403):
                print(f"ERREUR {e.code} sur {path} : {body}", file=sys.stderr)
                print("  -> le bot voit-il #aglomerateur (View + Read History) "
                      "et peut-il écrire dans les fils du forum "
                      "(Send Messages in Threads) ? Token valide ?",
                      file=sys.stderr)
                sys.exit(1)
            print(f"    HTTP {e.code} sur {path} : {body}", file=sys.stderr)
            return e.code, None
        except Exception as e:  # réseau
            print(f"    erreur réseau ({e}), essai {i + 1}/{tries}", flush=True)
            time.sleep(2 * (i + 1))
    return 0, None


def _load_state() -> dict:
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _save_state(st: dict) -> None:
    os.makedirs(os.path.dirname(STATE) or ".", exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE)


def _dernier_id() -> str:
    """id du message le plus récent du salon (pour l'ancrage 1er run)."""
    _, msgs = _call("GET", f"/channels/{AGG}/messages?limit=1")
    return msgs[0]["id"] if msgs else "0"


def _nouveaux(after: str) -> list:
    """Messages postés APRÈS `after` (ordre chronologique, cap MAXN)."""
    _, msgs = _call("GET", f"/channels/{AGG}/messages?after={after}&limit=100")
    msgs = msgs or []
    msgs.sort(key=lambda m: int(m["id"]))     # plus ancien -> plus récent
    return msgs[:MAXN]


def _rendu(msg: dict, source: str) -> dict:
    """Construit le payload de repost : attribution + contenu + URL des PJ +
    embeds 'rich' (les embeds auto image/lien se régénèrent depuis les URL)."""
    lignes = [f"📢 **{source}**"]
    corps = (msg.get("content") or "").strip()
    if corps:
        lignes.append(corps)
    for a in msg.get("attachments") or []:
        url = a.get("url")
        if url:
            lignes.append(url)               # image liée -> aperçu auto
    content = "\n".join(lignes)[:2000]        # limite Discord
    embeds = [e for e in (msg.get("embeds") or []) if e.get("type") == "rich"][:10]
    payload = {"content": content,
               "allowed_mentions": {"parse": []}}   # jamais de ping @everyone
    if embeds:
        payload["embeds"] = embeds
    return payload


def main() -> int:
    if not TOKEN:
        print("ERREUR : DISCORD_BOT_TOKEN absent.", file=sys.stderr)
        return 2
    st = _load_state()

    if RESET or not st.get("last_id"):
        anc = _dernier_id()
        st["last_id"] = anc
        _save_state(st)
        raison = "reset demandé" if RESET else "premier run"
        print(f"⚓ {raison} : watermark ancré au dernier message ({anc}). "
              f"Rien reposté (on ne rejoue pas l'historique).", flush=True)
        return 0

    after = st["last_id"]
    msgs = _nouveaux(after)
    if not msgs:
        print(f"Rien de neuf depuis {after}.", flush=True)
        return 0

    print(f"{len(msgs)} message(s) depuis {after}.", flush=True)
    postes, ignores = 0, 0
    nouveau_wm = after
    for m in msgs:
        mid = m["id"]
        wid = m.get("webhook_id")
        route = ROUTES.get(str(wid)) if wid else None
        if not route:
            ignores += 1                      # pas une source suivie (ou système)
            nouveau_wm = mid
            continue
        dest, source = route
        if DRY:
            print(f"  [DRY] {source} -> post {dest} (msg {mid})", flush=True)
            postes += 1
            nouveau_wm = mid
            continue
        status, _ = _call("POST", f"/channels/{dest}/messages", _rendu(m, source))
        if status and 200 <= status < 300:
            postes += 1
            print(f"  ✓ {source} -> post {dest} (msg {mid})", flush=True)
        else:
            # échec non-429 : on NE fait pas avancer le watermark au-delà, pour
            # réessayer ce message au prochain run (récolte sacrée).
            print(f"  ✗ échec repost {source} (msg {mid}, status {status}) — "
                  f"on s'arrête là, on réessaiera.", flush=True)
            break
        nouveau_wm = mid
        time.sleep(1.0)                       # doux avec l'API

    st["last_id"] = nouveau_wm
    _save_state(st)
    print(f"Fini : {postes} reposté(s), {ignores} ignoré(s), "
          f"watermark -> {nouveau_wm}.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
