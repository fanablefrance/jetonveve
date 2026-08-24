# ⚠️ DEPOT : fanablefrance/jetonveve   ·   CHEMIN : tests/test_journal_offres.py
# Le projet vit sur 6 depots et DEUX comptes GitHub. Un fichier depose au
# mauvais endroit ne provoque aucune erreur : il dort.
"""📒 LOT 186 — LE JOURNAL DES OFFRES STACKR : ce qu'il ecrit, ce qu'il refuse.

Preda, 24/08/2026 : « tracker les offres stackR ».

🔴🔴 CE BANC EXISTE D'ABORD A CAUSE D'UNE FAUSSE PISTE, ET IL LA FIGE.
Le flux `getAllLatestListings_v2` porte un champ `total_count`. Son nom promet
« le nombre d'offres de cet item » — mesure du 24/08 sur 60 lignes du flux :
les 60 portent la MEME valeur (1050), egale au `totalCount` de la racine, y
compris 20 lignes d'un meme `element_id`. C'est le total du FLUX.
⭐⭐⭐ Un champ dont on devine le sens par son nom rend un chiffre PLAUSIBLE et
FAUX ; celui-la aurait fini affiche sous chaque fiche comme « 1050 offres ».
⇒ §4 refuse explicitement que ce champ redevienne une source de comptage.

CE QUE CE JOURNAL EST : un journal d'EVENEMENTS de mise en vente.
CE QU'IL N'EST PAS : un compteur d'offres EN COURS — une offre retiree ou
vendue n'emet rien sur ce flux, donc compter les lignes surestime le marche.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("FLOOR_STATE", "/tmp/_test_offres_state.json")

from scraper import floor_watch as fw          # noqa: E402

KO = 0


def dit(ok, titre, detail=""):
    global KO
    print(f"  {'✅' if ok else '❌'} {titre}{'   — ' + detail if detail else ''}")
    if not ok:
        KO += 1


def listing(uid, nft, stamp, price, edition=1, sf=999999.0):
    """Un listing a la FORME EXACTE du flux, champs mesures le 24/08."""
    return {"price": str(price), "nft_id": nft, "element_id": uid,
            "edition": edition, "element_type": "COLLECTIBLE_TYPE",
            "image_url": "https://exemple.invalid/i.jpg", "name": "Test",
            "rarity": "COMMON", "timestamp": stamp,
            "listed_by": "0x00", "listed_by_username": "qqun",
            "stackr_floor_price": str(sf), "total_count": "1050"}


TS = time.time()
# ⚠️ Une horodate FRAICHE : le collecteur ecarte les evenements trop vieux
#   quand `FLOOR_EVENT_MAX_AGE_MIN` est arme, et un banc ne doit pas dependre
#   de l'heure a laquelle on le joue.
STAMP = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(TS - 300))

print("\n1. une mise en vente vue est une mise en vente ECRITE")
st = {}
fw.detect(st, [listing("aaaaaaaa-1111-2222-3333-444444444444", "n1", STAMP, 30000)],
          omi=0.000209, veve={}, ts=TS)
j = st.get("offres") or {}
dit(len(j) == 1, "une offre journalisee", f"{len(j)} entree(s)")
if j:
    v = next(iter(j.values()))
    dit(v[0] == "aaaaaaaa-1111-2222-3333-444444444444", "elle porte le veve_uuid")
    dit(v[2] == 30000.0, "elle porte le PRIX DEMANDE, en OMI", f"{v[2]}")
    # ⭐⭐⭐ LE CONTROLE QUI COMPTE LE PLUS DU BANC. `ts` est l'instant ou notre
    #   run a regarde ; `stamp` est l'instant ou l'offre a ete posee. Le flux
    #   garde ~1,7 h de listings et GitHub saute des runs : dater une offre de
    #   « maintenant » la placerait au mauvais endroit sur la courbe. Mesure du
    #   22/07 : jusqu'a 2 h 30 d'ecart entre l'evenement et sa publication.
    dit(abs(v[1] - (TS - 300)) < 2,
        "elle est datee de l'EVENEMENT, pas du tour de collecte",
        f"ecart au tour : {TS - v[1]:.0f} s (attendu ~300)")

print("\n2. ce qui n'est PAS ecrit — et aucun de ces cas n'invente une valeur")
# ⛔ Une horodate illisible fait SAUTER la ligne. La remplacer par « maintenant »
#   fabriquerait un evenement, et sur un site de cotes c'est la seule faute
#   qu'on ne rattrape jamais.
st = {}
fw.detect(st, [listing("aaaaaaaa-1111-2222-3333-444444444444", "n2", "hier soir", 30000)],
          omi=0.000209, veve={}, ts=TS)
dit(not (st.get("offres") or {}), "une horodate illisible : ligne SAUTEE, jamais datee de maintenant")

st = {}
fw.detect(st, [listing("aaaaaaaa-1111-2222-3333-444444444444", "n3", STAMP, 0)],
          omi=0.000209, veve={}, ts=TS)
dit(not (st.get("offres") or {}), "un prix a zero : rien n'est ecrit")

st = {}
fw.detect(st, [listing("", "n4", STAMP, 30000)], omi=0.000209, veve={}, ts=TS)
dit(not (st.get("offres") or {}), "un element_id vide : rien n'est ecrit")

# ⭐ LA CONTRE-EPREUVE DU §2 : sans elle, un journal CASSE (qui n'ecrit jamais
#   rien) rendrait ces trois lignes vertes. Un banc muet ressemble a un succes.
st = {}
fw.detect(st, [listing("aaaaaaaa-1111-2222-3333-444444444444", "n5", STAMP, 30000)],
          omi=0.000209, veve={}, ts=TS)
dit(len(st.get("offres") or {}) == 1,
    "...et le cas NORMAL passe toujours (le journal n'est pas simplement mort)")

print("\n3. la fenetre : bornee, et bornee sur l'age de l'OFFRE")
# ⛔ Sans borne, l'etat grossit de ~1 050 entrees par jour pour toujours — et
#   `floor_state.json` est relu/reecrit a chaque tour, 25 tours par run.
st = {"offres": {
    "vieux|x":  ["u1", TS - (fw.OFFRES_JOURS + 10) * 86400, 1, 1],
    "limite|x": ["u2", TS - (fw.OFFRES_JOURS - 1) * 86400, 1, 1],
    "cassee|x": ["u3", "pas-un-nombre", 1, 1],
}}
fw._journal_offres(st, TS)
restent = sorted(st["offres"])
dit(restent == ["limite|x"],
    f"hors fenetre elague, dans la fenetre conserve, valeur cassee retiree",
    f"restent : {restent}")

# ═══════════════════════════════════════════════════════════════════════════
# 🔴🔴 3 bis — ET `detect()` L'APPELLE VRAIMENT. CE VOLET EST NE D'UN TROU.
# ═══════════════════════════════════════════════════════════════════════════
# Le §3 ci-dessus appelle `_journal_offres()` A LA MAIN : il prouve que la
# fonction elague, JAMAIS qu'elle tourne. Injection du 24/08 — j'ai retire
# l'appel dans `detect()` : le §3 est reste VERT, et l'etat aurait grossi de
# ~1 050 entrees par jour, pour toujours, sans qu'un seul banc le voie.
# ⭐⭐⭐ *Prouver une fonction et prouver qu'on l'appelle sont deux bancs.* Le
# §1 de `test_affichage.mjs` porte la meme paire, pour la meme raison.
st = {"offres": {"tresvieux|x": ["u9", TS - (fw.OFFRES_JOURS + 5) * 86400, 1, 1]}}
fw.detect(st, [listing("aaaaaaaa-1111-2222-3333-444444444444", "n6", STAMP, 30000)],
          omi=0.000209, veve={}, ts=TS)
dit("tresvieux|x" not in (st.get("offres") or {}),
    "un tour de collecte elague le journal tout seul (l'appel est en place)",
    "" if "tresvieux|x" not in st.get("offres", {})
    else "`detect()` n'appelle pas `_journal_offres` : l'etat grossira sans borne")

print("\n4. `total_count` n'est PAS une source de comptage — et il ne le redevient pas")
# 🔴 MESURE DU 24/08 : sur 60 lignes du flux, `total_count` vaut 1050 partout,
#    y compris pour 20 lignes du meme element_id. Ce banc fige le refus.
st = {}
lots = [listing("aaaaaaaa-1111-2222-3333-444444444444", f"n{i}", STAMP, 30000)
        for i in range(3)]
fw.detect(st, lots, omi=0.000209, veve={}, ts=TS)
ecrits = list((st.get("offres") or {}).values())
dit(all(1050 not in [x for x in v if isinstance(x, (int, float))] for v in ecrits),
    "aucune valeur du journal ne vient de `total_count`")
dit(len(ecrits) == 3,
    "trois mises en vente du MEME item font trois lignes, pas un compteur",
    f"{len(ecrits)} ligne(s)")

print("\n5. le fichier servi : ce que l'etape du workflow ecrirait")
# ⭐ ON RELIT LE WORKFLOW LUI-MEME, on ne recopie pas sa logique ici. Une copie
#   de la regle dans le banc mesurerait le banc. (Meme raison qu'ailleurs dans
#   ce depot : deux listes ne divergent pas bruyamment, elles se contredisent
#   en silence.)
YML = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   ".github", "workflows", "floor-watch.yml")
try:
    y = open(YML, encoding="utf-8").read()
except OSError:
    y = ""
if not y:
    print("  ⏸️  sans objet — `floor-watch.yml` illisible depuis ce chemin.")
else:
    dit("offres_stackr.csv" in y, "l'etape ecrit bien `data/offres_stackr.csv`")
    dit('"veve_uuid", "ts_offre", "prix", "unite", "edition"' in y,
        "l'en-tete nomme `ts_offre` (un evenement) et porte l'unite")
    # ⛔⛔ LE FICHIER EST CUMULATIF SUR 30 JOURS : l'ecraser avec un journal
    #   reduit a son en-tete effacerait un mois qu'aucun rejeu ne reconstruit
    #   (le flux StackR ne garde que ~1,7 h de passe).
    dit('[ -s data/offres_stackr.csv ]' in y and 'wc -l < data/offres_stackr.csv' in y,
        "un journal vide n'ECRASE PAS celui de la release")

print(f"\n{'✅ journal des offres : conforme' if KO == 0 else f'❌ journal des offres : {KO} ecart(s)'}")
sys.exit(0 if KO == 0 else 1)
