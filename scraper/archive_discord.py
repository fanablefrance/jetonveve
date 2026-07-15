"""🗄️ L'ARCHIVE DE LA VEILLE — 9 000 messages de veille VeVe, sauves du naufrage.

Preda poste depuis des annees l'actualite VeVe et celle de sa communaute dans un
salon Discord, avec ses sources (des liens Twitter). Ce module descend TOUT le
fil, message par message, et le met a l'abri.

═══ POURQUOI C'EST URGENT (et pas juste « une bonne idee ») ═══
Ce corpus est en train de POURRIR, et personne ne le voit :
  * un lien Twitter de 2022 est mort trois fois — compte supprime, tweet efface,
    et X bloque la lecture sans compte. **Mais quand Preda a colle ce lien,
    Discord a fabrique un EMBED** : le texte du tweet, son auteur, son image.
    Cette copie est peut-etre la SEULE qui existe encore. C'est le tresor.
  * les pieces jointes (fan arts) vivent derriere des URLs **signees qui
    EXPIRENT** (ex/is/hm). L'URL ne vaut rien : seuls les OCTETS valent quelque
    chose. Si on ne telecharge pas maintenant, on ne telechargera jamais.

═══ LA REGLE DES COLLECTEURS LONGS, APPLIQUEE A LA LETTRE ═══
(Elle a coute 52 minutes de backfill perdues le 12/07. Ici, un echec ne coute
pas du temps : il coute un corpus qui n'existe qu'une fois.)
  0. **ON ECRIT EN COURS DE ROUTE** — flush du JSONL ET de l'etat toutes les
     `ARCHIVE_FLUSH` pages. Un job annule ne perd que les 5 dernieres pages.
  1. **LA RECOLTE EST SACREE** — aucun `raise` ne remonte. Sur echec definitif :
     on `break`, on ECRIT ce qu'on a, et le resume dit INCOMPLET.
  2. **ETAT DE REPRISE** (`data/archive_state.json`) : le plus vieux id atteint,
     le plus recent connu, et si la descente est finie. Le run suivant REPREND.
  3. **BACKOFF EXPONENTIEL** (6 essais, jusqu'a ~60 s) — un 500 en profondeur est
     presque toujours transitoire.
  4. **DEGRADATION AVANT ABANDON** — la page passe de 100 a 50 a 25 messages au
     meme endroit avant qu'on renonce.
  5. **LA DEDUPLICATION VIENT DE LA DONNEE, PAS DE L'ETAT** : l'id du message est
     intrinseque. On relit les ids deja ecrits et on ne les reecrit pas — meme
     avec un etat perdu, l'archive ne peut pas se dupliquer.
  6. **LA SONDE DU SALON MUET** (v2, apres le bug du 14/07) — voir plus bas : le
     seul echec qui ressemblait a une reussite.

═══ ON COLLECTE, ON NE TRANSFORME RIEN ═══
Pas de classification, pas de nettoyage, pas de resume pendant la collecte. Le
BRUT d'abord, sur disque, verifiable. Classer (news / projet / fan art /
ressenti) viendra dans une seconde passe, ou se tromper ne coutera plus rien.

Deux modes (`ARCHIVE_MODES`) :
  * `messages` : descend le fil (et, une fois la descente finie, ne prend plus
    que les nouveaux) -> `data/archive_veille.jsonl`, une ligne par message ;
  * `images`   : relit le JSONL et telecharge ce qui manque -> `archive_medias/`.
    Separe volontairement : la moisson des messages doit etre a l'abri AVANT
    qu'on s'attaque au gros du telechargement.

Env : DISCORD_BOT_TOKEN (SECRET) · ARCHIVE_CHANNEL · ARCHIVE_MODES
      ARCHIVE_JSONL · ARCHIVE_STATE · ARCHIVE_MEDIAS · ARCHIVE_FLUSH (5)
      ARCHIVE_MAX_PAGES (0 = illimite) · ARCHIVE_MAX_MEDIAS (0 = illimite)
      ARCHIVE_PURGER (1 = jeter l'archive avant de recollecter)
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

CHANNEL = os.environ.get("ARCHIVE_CHANNEL", "1126838487073689662").strip()
JSONL = os.environ.get("ARCHIVE_JSONL", "data/archive_veille.jsonl")
STATE_PATH = os.environ.get("ARCHIVE_STATE", "data/archive_state.json")
MEDIAS = os.environ.get("ARCHIVE_MEDIAS", "archive_medias")

MODES = [m.strip() for m in
         os.environ.get("ARCHIVE_MODES", "messages,images").split(",")
         if m.strip()]
FLUSH = int(os.environ.get("ARCHIVE_FLUSH", "5"))          # pages
MAX_PAGES = int(os.environ.get("ARCHIVE_MAX_PAGES", "0"))  # 0 = illimite
MAX_MEDIAS = int(os.environ.get("ARCHIVE_MAX_MEDIAS", "0"))
PURGER = os.environ.get("ARCHIVE_PURGER", "").lower() in ("1", "oui", "true")
PAUSE = float(os.environ.get("ARCHIVE_PAUSE_S", "0.6"))
ESSAIS = int(os.environ.get("ARCHIVE_ESSAIS", "6"))
TIMEOUT = 30
TAILLES = [100, 50, 25]          # degradation de page


# ---------------------------------------------------------------------------
# HTTP — un seul endroit ou l'on parle a Discord
# ---------------------------------------------------------------------------

def _entetes() -> Dict[str, str]:
    return {"Authorization": f"Bot {BOT}",
            "User-Agent": "ScrapeurVeVe-Archive (+github/VeVePreda)"}


def _get(url: str, params: Dict = None) -> Optional[List[Dict]]:
    """Renvoie la liste des messages, ou None si la page est DEFINITIVEMENT
    morte. On ne leve jamais : c'est l'appelant qui decide de s'arreter, apres
    avoir mis sa recolte a l'abri."""
    attente = 2.0
    for essai in range(ESSAIS):
        try:
            r = requests.get(url, headers=_entetes(), params=params,
                             timeout=TIMEOUT)
        except Exception as e:                              # noqa: BLE001
            print(f"   réseau KO ({e}) — essai {essai + 1}/{ESSAIS}",
                  file=sys.stderr)
            time.sleep(min(attente, 60))
            attente *= 2
            continue
        if r.status_code == 429:
            try:
                pause = float(r.json().get("retry_after", 5)) + 1
            except Exception:                               # noqa: BLE001
                pause = 5.0
            print(f"   rate limit — on patiente {pause:.0f} s", flush=True)
            time.sleep(min(pause, 60))
            continue
        if r.status_code in (401, 403):
            # Inutile d'insister : c'est une permission, pas un hoquet.
            print(f"⛔ {r.status_code} : le bot n'a pas accès à ce salon "
                  f"(il lui faut « Voir le salon » ET « Lire l'historique des "
                  f"messages »). {r.text[:200]}", file=sys.stderr)
            return None
        if r.status_code >= 500:
            print(f"   HTTP {r.status_code} — essai {essai + 1}/{ESSAIS}, "
                  f"on repasse dans {attente:.0f} s (un 500 en profondeur est "
                  f"presque toujours transitoire)", flush=True)
            time.sleep(min(attente, 60))
            attente *= 2
            continue
        if r.status_code >= 400:
            print(f"⛔ HTTP {r.status_code} : {r.text[:200]}", file=sys.stderr)
            return None
        return r.json()
    return None


def page(avant: str = "", apres: str = "",
         taille: int = 100) -> Optional[List[Dict]]:
    p = {"limit": taille}
    if avant:
        p["before"] = avant
    if apres:
        p["after"] = apres
    return _get(f"{API}/channels/{CHANNEL}/messages", p)


# ---------------------------------------------------------------------------
# Ce qu'on garde d'un message — TOUT ce qui a de la valeur, RIEN de transforme
# ---------------------------------------------------------------------------

RE_LIEN = re.compile(r"https?://\S+")


def extraire(m: Dict) -> Dict:
    """On ne nettoie pas, on ne classe pas : on RANGE. Chaque champ est le champ
    brut de Discord ; les seuls ajouts sont des raccourcis (`liens`) qu'on
    pourrait recalculer a tout moment depuis `contenu`."""
    a = m.get("author") or {}
    return {
        "id": m.get("id"),
        "date": m.get("timestamp"),              # LA DATE FAIT FOI
        "edite": m.get("edited_timestamp"),
        "auteur": {"id": a.get("id"), "nom": a.get("username"),
                   "bot": bool(a.get("bot"))},
        "contenu": m.get("content") or "",
        # LE TRESOR : le tweet fossilise. Le lien mourra, pas cette copie.
        "embeds": m.get("embeds") or [],
        "pieces_jointes": [{"id": p.get("id"), "nom": p.get("filename"),
                            "url": p.get("url"), "taille": p.get("size"),
                            "type": p.get("content_type")}
                           for p in (m.get("attachments") or [])],
        # Le seul signal de ce que la commu a juge important — gratuit
        # maintenant, impossible a reconstituer plus tard.
        "reactions": [{"emoji": (r.get("emoji") or {}).get("name"),
                       "nombre": r.get("count")}
                      for r in (m.get("reactions") or [])],
        "reponse_a": ((m.get("referenced_message") or {}).get("id")
                      if m.get("referenced_message") else
                      (m.get("message_reference") or {}).get("message_id")),
        "liens": RE_LIEN.findall(m.get("content") or ""),
        "type": m.get("type"),
    }


# ---------------------------------------------------------------------------
# ⚠️⚠️ LE GARDE-FOU QUI COMPTE LE PLUS : LA SONDE DU SALON MUET
# ---------------------------------------------------------------------------
# Un bot qui n'a PAS le « Message Content Intent » (privilegie, a activer dans le
# Developer Portal) recoit `content`, `embeds` et `attachments` **VIDES** — avec
# un **HTTP 200**. Pas d'erreur, pas d'avertissement : des COQUILLES VIDES qui
# ressemblent a une reussite. Seuls les messages qui MENTIONNENT le bot (ou qu'il
# a ecrits) arrivent complets — c'est la signature qui a trahi le bug le 14/07 :
# sur les 300 premiers messages collectes, le SEUL lisible etait celui qui
# contenait une mention.
#
# Sans cette sonde, on descendait 9 000 messages de RIEN, on les commitait, et la
# deduplication-par-id les aurait declares « deja collectes » A JAMAIS : le
# garde-fou de l'archive aurait scelle sa propre ruine. Alors on SONDE la
# premiere page, et si elle est muette on n'ecrit RIEN et on CRIE.
#
# (C'est la meme famille que le `cursor` de StackR ignore EN SILENCE : le pire
# echec n'est pas celui qui plante, c'est celui qui a l'air de marcher.)

MUET_SEUIL = float(os.environ.get("ARCHIVE_MUET_SEUIL", "0.9"))


def _est_vide(m: Dict) -> bool:
    return not (m.get("content") or m.get("embeds") or m.get("attachments")
                or m.get("sticker_items"))


def salon_muet(msgs: List[Dict]) -> bool:
    """True si la page est faite de coquilles vides — donc si l'intent manque.
    (Sur un vrai salon de veille, une page de 100 messages sans UN SEUL contenu,
    sans UN SEUL embed et sans UNE SEULE piece jointe n'existe pas.)"""
    if len(msgs) < 20:                 # trop peu pour conclure : on ne bloque pas
        return False
    vides = sum(1 for m in msgs if _est_vide(m))
    return vides / len(msgs) >= MUET_SEUIL


CRI_MUET = """
⛔⛔ SALON MUET — L'ARCHIVE SERAIT VIDE. ON N'ÉCRIT RIEN.

Discord a répondu 200, mais les messages arrivent SANS contenu, SANS embeds et
SANS pièces jointes. Ce n'est pas un salon vide : c'est le bot qui n'a pas le
droit de LIRE le contenu.

  → Developer Portal → ton application → Bot → « Privileged Gateway Intents »
    → MESSAGE CONTENT INTENT → ON → Save.
    (Sous 100 serveurs, c'est un simple interrupteur, aucune vérification.)

Ensuite, RELANCE avec « purger = oui » : les lignes déjà écrites sont des
coquilles vides, et comme on déduplique par id, elles ne seraient JAMAIS
réparées — il faut les jeter avant de recollecter.
"""


# ---------------------------------------------------------------------------
# Etat + JSONL (append-only : on n'ecrase JAMAIS ce qui est deja sur disque)
# ---------------------------------------------------------------------------

def purger() -> None:
    """La seule chose qui a le droit d'EFFACER l'archive, et seulement sur ordre
    explicite : une recolte muette doit pouvoir etre jetee (sinon la dedup la
    fige pour toujours). Un blocage collant doit avoir une porte de sortie."""
    for p in (JSONL, STATE_PATH):
        try:
            os.remove(p)
            print(f"🧹 purgé : {p}", flush=True)
        except FileNotFoundError:
            pass


def charger_etat() -> Dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                       # noqa: BLE001
        return {}


def ecrire_etat(st: Dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1, ensure_ascii=False)


def ids_deja_ecrits() -> set:
    """La deduplication vient de la DONNEE (l'id du message), pas de l'etat.
    Meme avec un etat perdu, l'archive ne peut pas se dupliquer."""
    vus = set()
    try:
        with open(JSONL, encoding="utf-8") as f:
            for ligne in f:
                try:
                    vus.add(json.loads(ligne)["id"])
                except Exception:                           # noqa: BLE001
                    continue          # une ligne tronquee ne casse pas le run
    except FileNotFoundError:
        pass
    return vus


def ajouter(lignes: List[Dict]) -> None:
    if not lignes:
        return
    os.makedirs(os.path.dirname(JSONL) or ".", exist_ok=True)
    with open(JSONL, "a", encoding="utf-8") as f:
        for l in lignes:
            f.write(json.dumps(l, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())          # sur disque POUR DE VRAI, pas dans un cache


# ---------------------------------------------------------------------------
# MODE 1 : les messages
# ---------------------------------------------------------------------------

def collecter_messages() -> Dict:
    st = charger_etat()
    vus = ids_deja_ecrits()
    print(f"Archive : {len(vus)} message(s) déjà sur disque.", flush=True)

    fini = bool(st.get("descente_finie"))
    total, pages, incomplet, muet = 0, 0, False, False
    tampon: List[Dict] = []

    def _vider():
        nonlocal tampon
        ajouter(tampon)
        tampon = []
        ecrire_etat(st)

    while True:
        if MAX_PAGES and pages >= MAX_PAGES:
            print(f"Plafond de {MAX_PAGES} pages atteint — on s'arrête là, la "
                  f"reprise fera le reste.", flush=True)
            break

        # DESCENTE vers le passé, puis INCREMENTAL vers le present.
        if not fini:
            curseur = {"avant": st.get("plus_vieux", "")}
        else:
            curseur = {"apres": st.get("plus_recent", "")}
            if not curseur["apres"]:
                print("Rien à rattraper (aucun id de tête connu).", flush=True)
                break

        msgs = None
        for taille in TAILLES:               # DEGRADATION avant abandon
            msgs = page(taille=taille, **curseur)
            if msgs is not None:
                break
            print(f"   page morte à {taille} — on réessaie plus petit.",
                  flush=True)
        if msgs is None:
            # ECHEC DEFINITIF : on garde tout ce qu'on a. C'est la regle.
            print("⛔ Page définitivement morte. On ARRÊTE, mais la récolte est "
                  "conservée et l'état permettra de reprendre exactement ici.",
                  file=sys.stderr)
            incomplet = True
            break

        if not msgs:
            if not fini:
                st["descente_finie"] = True
                fini = True
                print("✅ Descente terminée : on a touché le premier message du "
                      "salon.", flush=True)
                _vider()
                continue                     # on enchaine sur l'incremental
            print("Rien de neuf.", flush=True)
            break

        # ⚠️ LA SONDE. Sur la PREMIERE page seulement : si le contenu est muet,
        # l'intent manque, et tout ce qu'on collecterait serait des coquilles
        # vides. On n'ecrit RIEN — ni le JSONL, ni l'etat.
        if pages == 0 and salon_muet(msgs):
            print(CRI_MUET, file=sys.stderr)
            muet = True
            tampon = []                  # ON NE GARDE PAS LES COQUILLES
            break

        # Discord rend du plus recent au plus ancien (sauf avec `after`).
        msgs.sort(key=lambda m: int(m["id"]), reverse=True)
        neufs = [extraire(m) for m in msgs if m["id"] not in vus]
        for n in neufs:
            vus.add(n["id"])
        tampon += neufs
        total += len(neufs)
        pages += 1

        ids = [int(m["id"]) for m in msgs]
        plus_vieux = str(min(ids))
        plus_recent = str(max(ids))

        # ⚠️ GARDE-FOU ANTI-BOUCLE. Si le curseur n'AVANCE pas (l'API rend la
        # meme page), on tournerait indefiniment : un job de 6 h qui ne collecte
        # rien, et un quota brule pour rien. Le curseur DOIT progresser, sinon on
        # s'arrete et on le dit. (Meme famille de bug que la pagination de
        # StackR, ou `cursor` etait ignore EN SILENCE et servait 61 fois la
        # meme page.)
        avant_curseur = (st.get("plus_vieux") if not fini
                         else st.get("plus_recent"))
        if not fini:
            st["plus_vieux"] = plus_vieux
        if int(plus_recent) > int(st.get("plus_recent") or 0):
            st["plus_recent"] = plus_recent
        st["messages"] = len(vus)

        apres_curseur = (st.get("plus_vieux") if not fini
                         else st.get("plus_recent"))
        if avant_curseur and avant_curseur == apres_curseur:
            print("⛔ Le curseur n'avance plus (l'API rend la même page) — on "
                  "ARRÊTE plutôt que de tourner à vide. La récolte est "
                  "conservée.", file=sys.stderr)
            incomplet = True
            break

        if pages % FLUSH == 0:               # ON ECRIT EN COURS DE ROUTE
            _vider()
            print(f"   … {pages} pages, {total} nouveaux messages "
                  f"(le plus ancien atteint : {msgs[-1].get('timestamp', '')[:10]})",
                  flush=True)

        time.sleep(PAUSE)

    if muet:
        # On ne touche NI au JSONL NI a l'etat : rien de faux ne doit survivre a
        # ce run. Le prochain, une fois l'intent active, repartira propre.
        return {"pages": 0, "nouveaux": 0, "total": len(vus),
                "descente_finie": bool(st.get("descente_finie")),
                "statut": "MUET"}

    _vider()
    st["incomplet"] = incomplet
    ecrire_etat(st)
    return {"pages": pages, "nouveaux": total, "total": len(vus),
            "descente_finie": bool(st.get("descente_finie")),
            "statut": "INCOMPLET" if incomplet else "OK"}


# ---------------------------------------------------------------------------
# MODE 2 : les medias (URLs signees = elles EXPIRENT ; seuls les octets valent)
# ---------------------------------------------------------------------------

def _nom_fichier(mid: str, i: int, nom: str) -> str:
    propre = re.sub(r"[^A-Za-z0-9._-]", "_", nom or "fichier")[-60:]
    return f"{mid}_{i}_{propre}"


def medias_du_message(m: Dict) -> List[Dict]:
    """Les pieces jointes ET les images des embeds : les deux meurent."""
    out = []
    for i, p in enumerate(m.get("pieces_jointes") or []):
        if p.get("url"):
            out.append({"url": p["url"],
                        "fichier": _nom_fichier(m["id"], i, p.get("nom"))})
    for j, e in enumerate(m.get("embeds") or []):
        for cle in ("image", "thumbnail"):
            u = (e.get(cle) or {}).get("url") or ""
            if u:
                out.append({"url": u,
                            "fichier": _nom_fichier(m["id"], 100 + j * 2,
                                                    f"embed_{cle}.jpg")})
    return out


def telecharger_medias() -> Dict:
    os.makedirs(MEDIAS, exist_ok=True)
    deja = set(os.listdir(MEDIAS))
    faits, rates = 0, 0
    try:
        f = open(JSONL, encoding="utf-8")
    except FileNotFoundError:
        print("Aucun JSONL : rien à télécharger.", file=sys.stderr)
        return {"faits": 0, "rates": 0}

    with f:
        for ligne in f:
            try:
                m = json.loads(ligne)
            except Exception:                               # noqa: BLE001
                continue
            for md in medias_du_message(m):
                if md["fichier"] in deja:
                    continue
                if MAX_MEDIAS and faits >= MAX_MEDIAS:
                    print(f"Plafond de {MAX_MEDIAS} médias atteint — la reprise "
                          f"fera le reste (les fichiers déjà là sont sautés).",
                          flush=True)
                    return {"faits": faits, "rates": rates, "reste": "oui"}
                try:
                    r = requests.get(md["url"], timeout=TIMEOUT)
                    if r.status_code == 200 and r.content:
                        with open(os.path.join(MEDIAS, md["fichier"]),
                                  "wb") as g:
                            g.write(r.content)
                        deja.add(md["fichier"])
                        faits += 1
                    else:
                        # 403/404 = l'URL signee a EXPIRE. C'est exactement ce
                        # qu'on redoutait : ce media-la est perdu, pas le run.
                        rates += 1
                except Exception:                           # noqa: BLE001
                    rates += 1
                if faits % 50 == 0 and faits:
                    print(f"   … {faits} médias enregistrés", flush=True)
                time.sleep(0.15)
    return {"faits": faits, "rates": rates}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    t0 = time.time()
    if not BOT:
        print("DISCORD_BOT_TOKEN manquant : impossible de lire le salon.",
              file=sys.stderr)
        return 2
    if not CHANNEL:
        print("ARCHIVE_CHANNEL manquant.", file=sys.stderr)
        return 2

    if PURGER:
        print("──────── PURGE (demandée explicitement) ────────", flush=True)
        purger()

    resume = {}
    if "messages" in MODES:
        print("──────── MESSAGES ────────", flush=True)
        resume["messages"] = collecter_messages()
        print(f"Messages : {resume['messages']}", flush=True)
        if resume["messages"]["statut"] == "MUET":
            # On SORT EN ROUGE : un run muet ne doit pas passer inapercu, sinon
            # on archiverait 9 000 fois rien en croyant que tout va bien.
            return 3
    if "images" in MODES:
        print("──────── MÉDIAS ────────", flush=True)
        resume["medias"] = telecharger_medias()
        print(f"Médias : {resume['medias']}", flush=True)

    print(f"\nArchive terminée en {time.time() - t0:.0f}s : {resume}",
          flush=True)
    # Un run INCOMPLET n'est pas un echec : la recolte est sur disque, l'etat
    # permet de reprendre. On sort en 0 pour que le commit se fasse.
    return 0


if __name__ == "__main__":
    sys.exit(main())

# FIN archive_discord.py v2 — on collecte, on ne transforme rien ; la recolte est
# sacree ; la dedup vient de la donnee ; et un salon MUET ne s'archive pas.
