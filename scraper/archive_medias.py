"""🖼️ LA PASSE MÉDIAS — ~7 300 images, ~5 Go, sauvées avant qu'elles n'expirent.

Elle tourne sur **jetonveve (PUBLIC)** et pas sur preda (prive) pour une raison
comptable : un repo prive en plan gratuit dispose d'environ 500 Mo d'artifacts.
5 Go n'y rentrent pas. Un repo PUBLIC a des artifacts et des minutes illimites.

Le JSONL, lui, reste sur preda (PRIVE) : on va le CHERCHER avec PREDA_TOKEN, on
ne le recopie jamais ici.

═══ LES DEUX MORTS QU'ON COURSE ═══
  1. **Les pieces jointes Discord** vivent derriere des URLs **signees** (ex/is/hm)
     qui **EXPIRENT ~24 h apres avoir ete emises** — c'est-a-dire apres la
     collecte du message. Une passe medias lancee trois jours plus tard ne
     ramenerait que des 403. D'ou `rafraichir()` : l'endpoint Discord
     `/attachments/refresh-urls` (50 URLs par appel, jeton bot requis) rend une
     URL neuve. **C'est ce qui rend la passe rejouable dans le temps.**
  2. **Les images des embeds** (pbs.twimg.com) n'expirent pas, mais X peut les
     effacer a tout moment, avec le tweet et le compte. Elles ne se signent pas :
     on les prend telles quelles.

═══ LA REPRISE, POUR DE VRAI CETTE FOIS ═══
Le dossier `archive_medias/` n'existe QUE pendant le run (il n'est pas commite —
des Go dans git, c'est un repo qu'on ne clone plus). Donc un plafond `max_medias`
NE DECOUPE RIEN : au run suivant, le dossier est vide et on retelechargerait les
memes fichiers. C'est le bug de la v2, et il est corrige ici par un **REGISTRE
COMMITE** (`data/archive_medias_faits.txt`, un nom de fichier par ligne) : la
reprise vient de la DONNEE ecrite, pas d'un compteur en memoire.

⚠️ COROLLAIRE : chaque run depose son PROPRE artifact (nomme par numero de run).
Il faut les telecharger TOUS — le registre dit « c'est fait », il ne garde pas
les octets.

Env : ARCHIVE_JSONL · ARCHIVE_MEDIAS · ARCHIVE_REGISTRE
      ARCHIVE_MAX_MO (taille max d'un run, defaut 1500) · ARCHIVE_MAX_MEDIAS
      DISCORD_BOT_TOKEN (optionnel : sans lui, pas de rafraichissement d'URL)
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Dict, List, Optional

import requests

API = "https://discord.com/api/v10"
BOT = os.environ.get("DISCORD_BOT_TOKEN", "").strip()

JSONL = os.environ.get("ARCHIVE_JSONL", "data/archive_veille.jsonl")
MEDIAS = os.environ.get("ARCHIVE_MEDIAS", "archive_medias")
REGISTRE = os.environ.get("ARCHIVE_REGISTRE", "data/archive_medias_faits.txt")

MAX_MO = float(os.environ.get("ARCHIVE_MAX_MO", "1500"))     # par run
MAX_MEDIAS = int(os.environ.get("ARCHIVE_MAX_MEDIAS", "0"))  # 0 = illimite
PAUSE = float(os.environ.get("ARCHIVE_PAUSE_S", "0.15"))
TIMEOUT = 60

RE_CDN = re.compile(r"https://(cdn|media)\.discordapp\.(net|com)/")


# ---------------------------------------------------------------------------
# Le registre : la reprise vient de ce qui est ECRIT, pas d'un compteur
# ---------------------------------------------------------------------------

def charger_registre() -> set:
    try:
        with open(REGISTRE, encoding="utf-8") as f:
            return {l.strip() for l in f if l.strip()}
    except FileNotFoundError:
        return set()


def inscrire(noms: List[str]) -> None:
    """On ECRIT AU FUR ET A MESURE (et on fsync) : un job annule ne doit pas
    faire oublier ce qui est deja dans l'artifact."""
    if not noms:
        return
    os.makedirs(os.path.dirname(REGISTRE) or ".", exist_ok=True)
    with open(REGISTRE, "a", encoding="utf-8") as f:
        for n in noms:
            f.write(n + "\n")
        f.flush()
        os.fsync(f.fileno())


# ---------------------------------------------------------------------------
# Ce qu'il y a a telecharger
# ---------------------------------------------------------------------------

def _nom_fichier(mid: str, i: int, nom: str) -> str:
    propre = re.sub(r"[^A-Za-z0-9._-]", "_", nom or "fichier")[-60:]
    return f"{mid}_{i}_{propre}"


def medias_du_message(m: Dict) -> List[Dict]:
    """Les pieces jointes ET les images des embeds : les deux meurent, mais pas
    de la meme mort. On garde `signe` pour savoir laquelle on peut ressusciter."""
    out = []
    for i, p in enumerate(m.get("pieces_jointes") or []):
        if p.get("url"):
            out.append({"url": p["url"], "signe": True,
                        "fichier": _nom_fichier(m["id"], i, p.get("nom"))})
    for j, e in enumerate(m.get("embeds") or []):
        for k, cle in enumerate(("image", "thumbnail")):
            u = (e.get(cle) or {}).get("url") or ""
            if u:
                out.append({"url": u, "signe": bool(RE_CDN.match(u)),
                            "fichier": _nom_fichier(m["id"], 100 + j * 2 + k,
                                                    f"embed_{cle}.jpg")})
    return out


def tout_le_travail() -> List[Dict]:
    travail, vus = [], set()
    try:
        f = open(JSONL, encoding="utf-8")
    except FileNotFoundError:
        print(f"⛔ Pas de JSONL ({JSONL}) : rien à télécharger.", file=sys.stderr)
        return []
    with f:
        for ligne in f:
            try:
                m = json.loads(ligne)
            except Exception:                               # noqa: BLE001
                continue                  # une ligne tronquee ne casse pas le run
            for md in medias_du_message(m):
                if md["fichier"] in vus:
                    continue              # deux embeds, la meme image
                vus.add(md["fichier"])
                travail.append(md)
    return travail


# ---------------------------------------------------------------------------
# ⚠️ LA RESURRECTION D'URL — ce qui rend la passe rejouable
# ---------------------------------------------------------------------------

def rafraichir(urls: List[str]) -> Dict[str, str]:
    """Discord rend une URL signee NEUVE pour une URL expiree (50 par appel).
    Sans jeton bot, on ne peut pas : on rend {} et les pieces jointes expirees
    seront perdues — c'est pour CA que la passe medias doit suivre la collecte
    de pres."""
    if not BOT or not urls:
        return {}
    neuf: Dict[str, str] = {}
    for i in range(0, len(urls), 50):
        lot = urls[i:i + 50]
        try:
            r = requests.post(f"{API}/attachments/refresh-urls",
                              headers={"Authorization": f"Bot {BOT}",
                                       "Content-Type": "application/json"},
                              json={"attachment_urls": lot}, timeout=TIMEOUT)
            if r.status_code == 429:
                time.sleep(float(r.json().get("retry_after", 5)) + 1)
                r = requests.post(f"{API}/attachments/refresh-urls",
                                  headers={"Authorization": f"Bot {BOT}",
                                           "Content-Type": "application/json"},
                                  json={"attachment_urls": lot}, timeout=TIMEOUT)
            if r.status_code != 200:
                print(f"   refresh KO ({r.status_code})", file=sys.stderr)
                continue
            for e in (r.json() or {}).get("refreshed_urls", []):
                if e.get("original") and e.get("refreshed"):
                    neuf[e["original"]] = e["refreshed"]
        except Exception as e:                              # noqa: BLE001
            print(f"   refresh KO ({e})", file=sys.stderr)
    return neuf


def _aspirer(url: str) -> Optional[bytes]:
    """None = mort (403/404 : l'URL a expire, ou l'image n'existe plus)."""
    try:
        r = requests.get(url, timeout=TIMEOUT)
    except Exception:                                       # noqa: BLE001
        return None
    if r.status_code == 429:
        try:
            time.sleep(min(float(r.json().get("retry_after", 5)) + 1, 60))
        except Exception:                                   # noqa: BLE001
            time.sleep(5)
        try:
            r = requests.get(url, timeout=TIMEOUT)
        except Exception:                                   # noqa: BLE001
            return None
    return r.content if (r.status_code == 200 and r.content) else None


# ---------------------------------------------------------------------------
# La passe
# ---------------------------------------------------------------------------

def collecter() -> Dict:
    os.makedirs(MEDIAS, exist_ok=True)
    faits_avant = charger_registre()
    travail = [t for t in tout_le_travail() if t["fichier"] not in faits_avant]
    print(f"Registre : {len(faits_avant)} média(s) déjà pris. "
          f"Reste {len(travail)} à faire.", flush=True)
    if not travail:
        return {"faits": 0, "morts": 0, "reste": 0, "mo": 0}

    faits, morts, octets, nouveaux = 0, 0, 0, []
    expirees: List[Dict] = []                # a ressusciter en lot

    def _borne():
        return ((MAX_MEDIAS and faits >= MAX_MEDIAS)
                or (MAX_MO and octets / 1e6 >= MAX_MO))

    def _ecrire(md, contenu):
        nonlocal faits, octets
        with open(os.path.join(MEDIAS, md["fichier"]), "wb") as g:
            g.write(contenu)
        faits += 1
        octets += len(contenu)
        nouveaux.append(md["fichier"])
        if len(nouveaux) >= 25:              # ON INSCRIT EN COURS DE ROUTE
            inscrire(nouveaux)
            nouveaux.clear()

    for md in travail:
        if _borne():
            break
        contenu = _aspirer(md["url"])
        if contenu is None and md["signe"]:
            expirees.append(md)              # peut-etre ressuscitable
        elif contenu is None:
            morts += 1
            inscrire([md["fichier"]])        # une image X effacee ne reviendra pas
        else:
            _ecrire(md, contenu)
        if faits and faits % 100 == 0:
            print(f"   … {faits} médias · {octets / 1e6:.0f} Mo", flush=True)
        time.sleep(PAUSE)

    # ⚠️ LA SECONDE CHANCE : les URLs signees expirees, ressuscitees EN LOT.
    if expirees:
        print(f"🔄 {len(expirees)} URL(s) expirée(s) — on demande à Discord de "
              f"les rouvrir…", flush=True)
        neuf = rafraichir([e["url"] for e in expirees])
        for md in expirees:
            u = neuf.get(md["url"])
            contenu = _aspirer(u) if u else None
            if contenu is None:
                morts += 1
                inscrire([md["fichier"]])    # definitivement perdue : on tourne la page
            else:
                _ecrire(md, contenu)
            time.sleep(PAUSE)
        if not BOT:
            print("⚠️ Sans DISCORD_BOT_TOKEN, aucune URL expirée n'a pu être "
                  "rouverte : ces pièces jointes sont perdues.", file=sys.stderr)

    inscrire(nouveaux)
    reste = len(travail) - faits - morts
    r = {"faits": faits, "morts": morts, "reste": max(reste, 0),
         "mo": round(octets / 1e6)}
    if reste > 0:
        print(f"⏸️  Plafond du run atteint. RELANCE pour continuer : le registre "
              f"reprendra exactement ici ({reste} restants).", flush=True)
    return r


def main() -> int:
    t0 = time.time()
    r = collecter()
    print(f"\nMédias : {r} (en {time.time() - t0:.0f}s)", flush=True)
    print("⚠️ TÉLÉCHARGE L'ARTIFACT DE CE RUN : le registre dit « c'est fait », "
          "il ne garde pas les octets.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

# FIN archive_medias.py — la reprise vient du REGISTRE (ce qui est ecrit), pas
# d'un compteur ; et une URL signee expiree se ressuscite avant d'etre enterree.
