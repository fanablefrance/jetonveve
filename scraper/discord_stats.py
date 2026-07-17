"""📊 LES STATS DANS LE POST FORUM DISCORD — 3 messages permanents, REECRITS.

Le post « 📊 STATS » du forum « 📁⎮hub-actu-test » porte QUATRE messages, et
seulement quatre : Stats Années · Stats Mois · Stats Jours (condense) · Stats
Jours (detail). Ils sont postes une fois, puis EDITES a chaque run.

POURQUOI TROIS MESSAGES EDITES, ET PAS UN MESSAGE PAR JOUR
----------------------------------------------------------
Preda voulait « un systeme qui supprime petit a petit les stats journalieres et
mensuelles pour ne laisser que celles des années ». Un webhook Discord peut
EDITER ses propres messages : trois messages reecrits chaque matin font ce
menage TOUT SEULS, sans jamais rien supprimer —
  * un jour qui sort des 7 derniers est deja compte dans SON mois ;
  * un mois clos est deja compte dans SON année ;
  * l'année, elle, ne bouge plus.
Le post reste propre en permanence, et l'historique n'est jamais perdu : il
remonte d'un cran de granularite.

ORDRE DE CREATION : ANNEES, puis MOIS, puis JOURS — dans un fil Discord le
dernier message est en BAS, donc le tableau qu'on lit tous les matins (les
jours) tombe sous les yeux en ouvrant le post.

v6 — LA FORME, APRES QUATRE ALLERS-RETOURS AVEC PREDA
-----------------------------------------------------
* **MARKDOWN** (choix de Preda apres comparaison). Le tableau monospace alignait
  les colonnes, mais imposait sa loi : un bloc de code Discord n'est pas coupe,
  il est ENROULE — donc peu de colonnes, des chiffres arrondis, des barres
  serrees. En markdown il n'y a PLUS DE LARGEUR : on peut tout dire, en toutes
  lettres, avec les chiffres COMPLETS. Le prix a payer : rien n'est aligne, donc
  **chaque nombre porte son etiquette** (« Global **3 421** ») — sinon on ne
  saurait plus lire une colonne.
* **CINQ PARTIES** (TRANSACTION · ACTIFS · REVENUE · LISTING · ACHAT, les memes
  groupes que la page 📊 STATS) — mais SEULEMENT sur le detail des JOURS (v7) :
  a la maille du mois ou de l'année, ce niveau de detail n'apprend rien. Les
  années, les mois et le message CONDENSE des jours tiennent sur une ligne par
  periode.
* **☑ / ☐** en tete de chaque jour : case cochee = jour de drop. Uniquement sur
  les JOURS (sur un mois ou une année, il y a forcement eu des drops).
* `[MAJ 14/07 a 11h]` en code inline dans le message.
* L'avertissement dans le PIED DE LA CARTE : Discord le rend tout petit et
  gris — precede d'un ⓘ, sans matricule ni ⚠️.

LES DEUX PIEGES DISCORD (payes une fois, plus jamais)
-----------------------------------------------------
1. **Poster dans un post de forum** = poster dans un THREAD : le webhook
   appartient au SALON, on lui ajoute `?thread_id=<id du post>`.
2. **Pour EDITER, il faut l'id du message** : on ne peut pas relire un salon
   avec un webhook (il faudrait un vrai bot). L'id est donc memorise dans
   l'etat (`data/discord_stats_state.json`, commite par le workflow). Si un
   message est supprime a la main, le PATCH renvoie 404 -> on le RECREE.

Env :
  SHEET_ID, GOOGLE_SERVICE_ACCOUNT_JSON  (lecture du Sheet)
  DISCORD_STATS_WEBHOOK   (SECRET ; sans lui : simulation dans les logs)
  DISCORD_STATS_THREAD    (id du post « 📊 STATS »)
  DISCORD_STATS_STATE     (data/discord_stats_state.json)
  DISCORD_STATS_JOURS (7) · DISCORD_STATS_MOIS (5) · DISCORD_STATS_ANNEES (4)
  DISCORD_STATS_BLOCS     (annees,mois,jours — pour cibler a la main)
"""

from __future__ import annotations

import datetime as _dt
import os
import sys
import time
from typing import Any, Dict, List, Optional

from scraper import discord_api as api
from scraper import stats_read
from scraper.sheets import _client, append_log

MODULE = "stats"
WEBHOOK = api.webhook(MODULE)
THREAD = os.environ.get("DISCORD_STATS_THREAD", "1526491450538196992").strip()
STATE_PATH = os.environ.get("DISCORD_STATS_STATE",
                            "data/discord_stats_state.json")
# QUATRE messages (v7). Le tableau des jours existe en DEUX versions : un
# condense (l'essentiel d'un coup d'oeil) JUSTE AU-DESSUS du detail en cinq
# parties. Les mois et les années restent condenses : le detail n'a de sens
# qu'a la maille du jour.
ORDRE = ("annees", "mois", "jours_court", "jours")

N = {"jours": int(os.environ.get("DISCORD_STATS_JOURS", "7")),
     "mois": int(os.environ.get("DISCORD_STATS_MOIS", "5")),
     "annees": int(os.environ.get("DISCORD_STATS_ANNEES", "4"))}
N["jours_court"] = N["jours"]

# La source de chaque message dans la page 📊 STATS.
SOURCE = {"annees": "annees", "mois": "mois",
          "jours_court": "jours", "jours": "jours"}

BLOCS = [b.strip() for b in
         os.environ.get("DISCORD_STATS_BLOCS", ",".join(ORDRE)).split(",")
         if b.strip()]

# Sert uniquement a nommer les messages dans les LOGS (cote Discord, plus aucun
# matricule n'est affiche : Preda n'en voulait pas).
CODES = {"annees": "VEVE-STATS-ANNEES",
         "mois": "VEVE-STATS-MOIS",
         "jours_court": "VEVE-STATS-JOURS-COURT",
         "jours": "VEVE-STATS-JOURS"}

TITRES = {"annees": f"🏛️ **Les {N['annees']} dernières années**",
          "mois": f"📅 **Les {N['mois']} derniers mois**",
          "jours_court": f"📊 **Les {N['jours']} derniers jours — l'essentiel**",
          "jours": f"📊 **Les {N['jours']} derniers jours — le détail**"}

CARTES = {"annees": "🏛️ Stats Années",
          "mois": "📅 Stats Mois",
          "jours_court": "📊 Stats Jours — condensé",
          "jours": "📊 Stats Jours — détail"}

COULEURS = {"annees": 0xF1C40F, "mois": 0x7B2CBF,
            "jours_court": 0x2ECC71, "jours": 0x3498DB}

# ☑ / ☐ : cases a cocher (demande de Preda). Ce sont des CARACTERES, pas des
# emojis — discrets, et ils ne deforment pas la ligne.
DROP_OUI = os.environ.get("DISCORD_STATS_DROP_OUI", "☑")
DROP_NON = os.environ.get("DISCORD_STATS_DROP_NON", "☐")

# LA VERSION CONDENSEE : une ligne par periode, l'essentiel et rien d'autre.
# v8 : Nouveaux et Anciens sortent d'ici — ils ne vivent plus que dans le DETAIL
# des jours.
# v10 : sur les MOIS et les ANNEES, on montre le revenue **DROP** et pas le
# TOTAL — et on l'ECRIT (« Rev Drop »), pour que personne ne croie lire un total.
# C'est le chiffre solide a cette maille : le Total y ajoute le revenue MARKET,
# qui est approximatif ou incomplet (et carrement absent avant 2026-01, ou
# l'archive IMX ne porte pas l'uuid des items, donc aucun prix rattachable).
# Mieux vaut un chiffre vrai et nomme qu'un total qui melange du solide et du
# creux.
CONDENSE = {
    "jours_court": [("tx", "Tx"), ("actifs", "Actifs"), ("revenue", "Rev")],
    "mois": [("tx", "Tx"), ("actifs", "Actifs"), ("rev_drop", "Rev Drop")],
    "annees": [("tx", "Tx"), ("actifs", "Actifs"), ("rev_drop", "Rev Drop")],
}

# LES CINQ PARTIES — SEULEMENT pour le detail des JOURS : a la maille du mois ou
# de l'année, ce niveau de detail n'apprend rien. Ce sont exactement les groupes
# de colonnes de la page 📊 STATS.
#
# Chaque partie porte une NOTE (v8, dictee par Preda) : ce sont les precisions
# qui evitent qu'on lise le chiffre de travers. Elles disent ce que le chiffre
# COUVRE et ce qu'il NE couvre PAS — c'est la moitie de l'information.
SECTIONS = [
    ("🔁", "TRANSACTION", "achats, ventes et mints",
     [("tx", "Global"), ("mint", "Mint"), ("market", "Market")]),
    ("👤", "ACTIFS", "wallets uniques, hors listing",
     [("actifs", "Unique"), ("nouveaux", "Nouveaux"),
      ("anciens", "Anciens")]),
    ("💵", "REVENUE", "la valeur Market est souvent approximative ou incomplète",
     [("revenue", "Total"), ("rev_drop", "Drop"),
      ("rev_market", "Market")]),
    ("🏷️", "LISTING", "données sur ceux n'ayant ni acheté, ni vendu, ni mint",
     [("qte", "Quantité"), ("comptes", "Comptes uniques")]),
    ("🛒", "ACHAT", "ne prend pas en compte les achats en fiat",
     [("cours_omi", "Cours OMI"), ("gems_usd", "Gems")]),
]

# La legende des cases, sous le condense des jours (demande de Preda) : un
# symbole qu'on n'explique pas est un symbole qui ne sert a rien.
LEGENDE_DROP = (f"\n\n*{DROP_OUI} = jour de drop · {DROP_NON} = jour sans drop*")

# Les colonnes en dollars (le $ est colle au chiffre, pas mis en legende : en
# markdown rien n'est aligne, donc chaque nombre doit se suffire a lui-meme).
DOLLARS = {"revenue", "rev_drop", "rev_market", "gems_usd", "cours_omi"}

# L'avertissement va dans le PIED DE LA CARTE : c'est la seule zone que Discord
# rend nativement en tout petit et en gris, avec la garantie qu'elle ne prendra
# jamais la parole. (Le sous-texte « -# » du markdown n'est pas garanti dans une
# description d'embed ; s'il ne passait pas, on lirait « -# » en clair — on ne
# parie pas la mise en forme sur une incertitude.) Le ⓘ ouvre la ligne, et il
# n'y a plus ni matricule ni ⚠️, comme demande.
AVERTISSEMENT = ("ⓘ Chiffres indicatifs, issus de sources publiques — ce n'est "
                 "PAS un conseil financier et des erreurs sont possibles.")

MAX_DESC = 4000                      # Discord refuse au-dela de 4096

# 🔔 LE PING EPHEMERE (idee de Preda) — le canal STATS n'a que 4 messages
# EDITES : une edition ne genere AUCUNE notification, donc une mise a jour passe
# inapercue. Parade : a chaque run, poster un message NEUF (qui, lui, notifie)
# puis le SUPPRIMER dans la foulee. Le push (surtout mobile) est deja parti ; le
# fil reste propre, ses 4 messages intacts. Declencheur = chaque run (choix
# Preda) : on ping des qu'au moins un message a ete publie/reecrit.
PING_ON = os.environ.get("DISCORD_STATS_PING", "true").lower() != "false"
PING_TEXTE = os.environ.get("DISCORD_STATS_PING_TEXTE", "🔄 Stats mises à jour")


# ---------------------------------------------------------------------------
# Mise en forme
# ---------------------------------------------------------------------------

def _fr(x) -> str:
    """En markdown il n'y a pas de contrainte de largeur : on donne le chiffre
    COMPLET (c'est le tableau monospace qui obligeait a arrondir)."""
    n = stats_read.nombre(x)
    return f"{n:,}".replace(",", " ") if n else "—"


def _cours(x) -> str:
    """Le cours OMI vaut ~0,000168 $ : `nombre()` le raboterait a 0."""
    try:
        v = float(str(x).replace(",", ".").replace(" ", "").replace("$", ""))
    except (TypeError, ValueError):
        return "—"
    if not v:
        return "—"
    return f"{v:.6f}".rstrip("0").replace(".", ",") + " $"


def _valeur(cle_col: str, r: Dict) -> str:
    if cle_col == "cours_omi":
        return _cours(r.get(cle_col))
    v = _fr(r.get(cle_col))
    return v + " $" if v != "—" and cle_col in DOLLARS else v


def _jour_like(cle: str) -> bool:
    """Les deux messages des jours : le condense et le detail."""
    return cle in ("jours", "jours_court")


def _date(brut) -> Optional[_dt.date]:
    try:
        return _dt.date.fromisoformat(str(brut or "").strip()[:10])
    except ValueError:
        return None


def _periode(cle: str, brut) -> str:
    """Jours (condense OU detail) -> « lun. 13/07 » ; mois -> « 2026-07 » ;
    années -> « 2026 »."""
    if not _jour_like(cle):
        return str(brut or "").strip()
    d = _date(brut)
    if not d:
        return str(brut or "").strip()
    jours = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]
    return f"{jours[d.weekday()]}. {d.strftime('%d/%m')}"


def _case(cle: str, r: Dict) -> str:
    """☑ jour de drop · ☐ jour sans drop — uniquement sur les JOURS (sur un mois
    ou une année, il y a forcement eu des drops : la case ne dirait rien)."""
    if not _jour_like(cle):
        return ""
    return (DROP_OUI if str(r.get("drop") or "").strip() else DROP_NON) + " "


def _ligne(cle: str, r: Dict, colonnes) -> str:
    chiffres = " · ".join(f"{nom} **{_valeur(c, r)}**" for c, nom in colonnes)
    return (f"{_case(cle, r)}`{_periode(cle, r.get('periode'))}` — {chiffres}")


def corps(cle: str, lignes: List[Dict[str, Any]]) -> str:
    """Le DETAIL (cinq parties separees par une ligne vide) n'est rendu que pour
    le message « jours ». Tout le reste — années, mois, et le message condense
    des jours — tient sur une ligne par periode.

    Chaque nombre porte son etiquette : en markdown rien n'est aligne, donc rien
    ne doit dependre de la position."""
    if cle != "jours":
        txt = "\n".join(_ligne(cle, r, CONDENSE[cle]) for r in lignes)
        return txt + LEGENDE_DROP if _jour_like(cle) else txt
    blocs = []
    for emoji, titre, note, colonnes in SECTIONS:
        # Le titre, puis la precision SOUS lui, en italique (demande de Preda) :
        # elle se lit comme une legende, pas comme une parenthese jetee.
        lines = [f"{emoji} **{titre}**", f"*{note}*"]
        lines += [_ligne(cle, r, colonnes) for r in lignes]
        blocs.append("\n".join(lines))
    return "\n\n".join(blocs)


def _maj() -> str:
    """Le format voulu par Preda, en code inline : `[MAJ 14/07 à 11h]`."""
    h = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=2)  # Paris
    return h.strftime("`[MAJ %d/%m à %Hh]`")


def carte(cle: str, lignes: List[Dict]) -> Dict:
    texte = corps(cle, lignes)
    if len(texte) > MAX_DESC:
        print(f"⚠️ {CODES[cle]} : {len(texte)} caracteres — Discord plafonne a "
              f"4096, il faut reduire le nombre de periodes.", file=sys.stderr)
        texte = texte[:MAX_DESC]
    return {"title": CARTES[cle], "color": COULEURS[cle],
            "description": texte,
            "footer": {"text": AVERTISSEMENT}}


def message(cle: str, lignes: List[Dict]) -> Dict:
    return {"content": f"{TITRES[cle]}  ·  {_maj()}",
            "embeds": [carte(cle, lignes)],
            # Un tableau de stats ne doit JAMAIS pouvoir ping qui que ce soit.
            "allowed_mentions": api.mentions()}


# ---------------------------------------------------------------------------
# Discord : poster une fois, editer pour toujours (la couche reseau et TOUS les
# garde-fous vivent dans scraper/discord_api.py — une seule copie, pour tous)
# ---------------------------------------------------------------------------

def reordonner(state: Dict) -> None:
    """Discord n'a AUCUN moyen de deplacer un message : l'ordre d'un fil est
    l'ordre de CREATION. Or le condense doit apparaitre AU-DESSUS du detail, et
    le detail existe deja. Seule sortie : SUPPRIMER le detail et le laisser se
    recreer APRES le condense. On ne le fait qu'une fois — la fois ou le
    condense n'a pas encore d'id."""
    ids = state.get("messages", {})
    if not WEBHOOK or ids.get("jours_court") or not ids.get("jours"):
        return
    print("Le message condense doit passer AU-DESSUS du detail : on supprime "
          "le detail (un fil Discord se range par ordre de creation, rien ne "
          "se deplace) — il sera recree juste apres.", flush=True)
    if api.supprimer(WEBHOOK, THREAD, ids["jours"]):
        ids.pop("jours", None)
    api.souffler()


def publier(cle: str, payload: Dict, state: Dict) -> bool:
    if not WEBHOOK:
        print(f"\n[SIMULATION — pas de webhook] {CODES[cle]}", flush=True)
        print(payload["content"], flush=True)
        print(payload["embeds"][0]["description"], flush=True)
        return True
    ids = state.setdefault("messages", {})
    mid = str(ids.get(cle) or "")
    neuf = (api.editer(WEBHOOK, THREAD, mid, payload) if mid
            else api.poster(WEBHOOK, THREAD, payload))
    if not neuf:
        return False
    ids[cle] = neuf
    print(f"{CODES[cle]} : {'edite' if mid == neuf else 'poste'} ({neuf})",
          flush=True)
    api.souffler()
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def pinger() -> None:
    """Poster un message neuf — qui notifie — puis le SUPPRIMER aussitot. La
    notification est deja partie ; le fil garde ses 4 messages et rien d'autre.
    Plafond + 429 + espacement geres par la couche discord_api, comme partout."""
    if not PING_ON:
        return
    if not WEBHOOK:
        print("[SIMULATION — pas de webhook] ping ephemere STATS "
              f"(« {PING_TEXTE} », poste puis supprime).", flush=True)
        return
    payload = {"content": PING_TEXTE, "allowed_mentions": api.mentions()}
    mid = api.poster(WEBHOOK, THREAD, payload)
    if not mid:
        return                    # plafond atteint ou POST refuse : rien a nettoyer
    api.souffler()
    if api.supprimer(WEBHOOK, THREAD, mid):
        print(f"🔔 ping ephemere STATS ({mid}) : poste puis supprime — la notif "
              "est partie, le fil reste propre.", flush=True)
    else:
        print(f"⚠️ ping ephemere STATS ({mid}) poste mais NON supprime — un "
              "message a nettoyer a la main.", file=sys.stderr)


def run() -> int:
    t0 = time.time()
    sheet_id = os.environ.get("SHEET_ID", "").strip()
    if not sheet_id:
        print("SHEET_ID env var is not set.", file=sys.stderr)
        return 2
    if not THREAD:
        print("DISCORD_STATS_THREAD manquant (id du post forum).",
              file=sys.stderr)
        return 2

    sh = _client().open_by_key(sheet_id)
    page = stats_read.lire(sh)
    if not any(page.values()):
        print("📊 STATS illisible — AUCUN message poste (mieux vaut rien "
              "qu'un tableau faux).", file=sys.stderr)
        return 1

    state = api.load_state(STATE_PATH, WEBHOOK, THREAD)
    contenus = {cle: page[SOURCE[cle]][:N[cle]] for cle in ORDRE}
    reordonner(state)

    # ORDRE : annees -> mois -> jours condense -> jours detaille. Le dernier
    # poste est en BAS du fil, donc le detail ferme la marche et le condense
    # arrive juste au-dessus, comme demande.
    ok, faits = True, []
    for cle in ORDRE:
        if cle not in BLOCS:
            continue
        lignes = contenus[cle]
        if not lignes:
            print(f"{CODES[cle]} : aucune donnee dans 📊 STATS — on ne touche "
                  f"pas au message existant.", flush=True)
            continue
        if publier(cle, message(cle, lignes), state):
            faits.append(cle)
        else:
            ok = False

    # 🔔 Ping ephemere : uniquement si au moins un message a ete publie (sinon
    # notifier « mise a jour » alors que rien n'a bouge serait un mensonge).
    if faits:
        pinger()

    api.save_state(STATE_PATH, state, WEBHOOK, THREAD)
    resume = {"jours": len(contenus["jours"]), "mois": len(contenus["mois"]),
              "annees": len(contenus["annees"]),
              "publies": ",".join(faits) or "aucun",
              "duree": f"{time.time() - t0:.0f}s"}
    try:
        append_log(sheet_id, "discord_stats", "OK" if ok else "ECHEC",
                   "; ".join(f"{k}={v}" for k, v in resume.items()))
    except Exception:                                       # noqa: BLE001
        pass
    print(f"Stats Discord : {resume}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())

# FIN discord_stats.py v10 — 4 messages : condense partout, detail en cinq
# parties sur les seuls jours, et le condense JUSTE AU-DESSUS du detail.
