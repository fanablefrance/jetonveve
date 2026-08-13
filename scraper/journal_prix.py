# ⚠️ DEPOT : fanablefrance/jetonveve   ·   CHEMIN : scraper/journal_prix.py
"""📓 journal_prix — UN POINT DE PRIX PAR PIECE ET PAR JOUR, QU'ON JETAIT DEJA.

POURQUOI CE MODULE EXISTE
-------------------------
Preda, le 13/08/2026 : « je veux un suivi quotidien une fois par jour des prix ».
Mesure du meme jour : ce suivi n'existait nulle part, ET LA DONNEE ETAIT DEJA LA.

  `releves.csv`          -> INSTANTANE, RECRIT a chaque run horaire. La photo
                            d'hier est ecrasee par celle d'aujourd'hui.
  `prices_baselines.csv` -> append-on-change : AUCUN point tant que le prix ne
                            bouge pas (8,6 % seulement des pieces ont date de
                            changement = date de relevé ; ecart median 14 j).
  `hprix_feed.csv`       -> changements aussi, et 0 comic.

⭐⭐⭐ RIEN NE MANQUAIT A LA COLLECTE : `floor-watch` observe deja 6 229 pieces
cote VeVe et 921 cote StackR toutes les heures (mesure du 13/08 sur le
`floor_state.json` de la release). **24 observations par jour partaient a la
poubelle a chaque reecriture.** Ce module ne collecte RIEN de neuf : il ARRETE
DE JETER. -> [[regle-donnee-collectee-puis-jetee]]

CE QU'IL FAIT, ET CE QU'IL REFUSE DE FAIRE
------------------------------------------
  IL FAIT     une ligne par (piece, marche, JOUR UTC DE L'OBSERVATION)
  IL REFUSE   d'inventer un point pour un jour ou rien n'a ete observe

⛔⛔ LA SECONDE MOITIE EST LA PLUS IMPORTANTE. Un journal qui comble les trous
est un journal qui ment, et il ment exactement la ou l'on regarde : sur les
pieces peu suivies. Le site pourra prolonger la derniere valeur s'il le veut —
c'est une decision d'AFFICHAGE. Le journal, lui, ne dit que ce qui a ete VU.

⛔⛔ DEUX MARCHES, PAS DEUX UNITES. `vfloors` = floor VeVe en **USD**,
`sfloors` = floor StackR en **OMI**. La colonne `unite` le dit, la colonne
`source` aussi. ⛔ AUCUNE conversion : le rapport n'est pas constant (mediane
4 423, p10 2 273, p90 8 520 sur 1 306 items communs).

🔴🔴 LE PIEGE QUI A PRODUIT LE LOT 146, ET QU'ON NE REFERA PAS ICI
------------------------------------------------------------------
Le lot 146 a corrige 904 fiches sur 1 200 qui **dataient l'observation d'un
marche avec l'horloge de l'AUTRE**. La meme faute, ici, serait de dater
« aujourd'hui » une observation vieille de dix jours parce que le run, lui, est
d'aujourd'hui. D'ou la regle, non negociable :

    LE JOUR D'UNE LIGNE EST LE JOUR DE SON `ts` D'OBSERVATION,
    JAMAIS LE JOUR DU RUN.

C'est mesurable et ce n'est pas theorique : cote StackR, l'horodate des pieces
presentes dans l'etat s'etale sur **10 jours** (p50 a 3,1 j). Les dater du jour
du run fabriquerait 5 958 fausses observations quotidiennes.

⏱️ ET UNE FENETRE DE FRAICHEUR, PARCE QUE L'ETAT EST CUMULATIF
`floor_state.json` garde des entrees qui n'ont pas ete revues depuis des
semaines (mesure : 586 des 6 764 `vfloors` datent d'avant aujourd'hui, la plus
vieille de 29 jours). Les reemettre a chaque run les reecrirait a l'identique
sur leur vrai jour — inutile mais inoffensif. On les filtre quand meme : une
fenetre etroite rend le cout du run previsible et le rapport lisible.

🕐 IDEMPOTENT PAR CONSTRUCTION. `floor-watch` tourne 24 fois par jour ; ce
module tourne donc 24 fois sur la meme journee. La cle (uuid, source, jour)
dedoublonne, et **c'est l'observation la plus RECENTE du jour qui gagne** —
le prix de cloture de la journee, pas celui de 00 h 03.

⛔ AUCUN RESEAU ICI. Tout est pur et se teste hors ligne, sur l'etat reel
telecharge de la release. -> [[regle-echantillon-hors-ligne-angle-mort]]
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

# L'en-tete du journal. ⛔ `ts_obs` N'EST PAS decoratif : c'est lui qui
# departage deux observations du meme jour, et c'est lui qui prouve que le
# `jour` n'a pas ete pris a l'horloge du run.
COLONNES = ["veve_uuid", "jour", "ts_obs", "source", "floor", "unite"]

# ⭐ Une observation posterieure a l'instant du run est une horloge cassee, pas
# une donnee. On ne la corrige pas en silence : on la COMPTE et on l'ecarte.
# Sans ce compteur, une derive d'horloge creerait des jours futurs dans le
# journal et personne ne saurait d'ou ils viennent.
TOLERANCE_FUTUR_S = 15 * 60


def _jour(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def observations(etat: dict, maintenant: float, fenetre_h: float = 24.0) -> tuple[list, dict]:
    """Extrait de `floor_state.json` les observations FRAICHES.

    Rend (lignes, compteurs). Une ligne = (uuid, jour, ts, source, floor, unite).
    ⭐ Les compteurs contiennent des termes QUI DOIVENT VALOIR ZERO — c'est la
    seule facon qu'un total ait une valeur de preuve. Une somme vraie par
    identite algebrique ne mesure rien (lecon du lot 146, mutant M4).
    """
    plancher = maintenant - fenetre_h * 3600.0
    plafond = maintenant + TOLERANCE_FUTUR_S
    lignes: list = []
    c = {
        "veve_total": 0, "veve_retenus": 0, "veve_sans_date": 0, "veve_rassis": 0,
        "stackr_total": 0, "stackr_retenus": 0, "stackr_sans_date": 0, "stackr_rassis": 0,
        # 🔴 CES DEUX-LA DOIVENT VALOIR ZERO. S'ils bougent, c'est l'instrument
        #    ou l'horloge qui est en cause, pas la donnee.
        "futurs_ecartes": 0,
        "malformes": 0,
    }

    # vfloors : [floor_usd, ts]           -> le ts est en position 1
    # sfloors : [floor, nom, rar, img, ts] -> le ts est en position 4, ajoute le
    #           03/08 EN FIN de tuple pour ne pas casser les 7 lecteurs qui
    #           indexent 0-3. Les entrees ecrites avant cette date n'en ont pas.
    for cle, i_ts, source, unite in (("vfloors", 1, "veve", "USD"),
                                     ("sfloors", 4, "stackr", "OMI")):
        for u, v in (etat.get(cle) or {}).items():
            c[f"{source}_total"] += 1
            if not isinstance(v, list) or len(v) <= i_ts:
                c[f"{source}_sans_date"] += 1
                continue
            ts, floor = v[i_ts], v[0]
            if not isinstance(ts, (int, float)) or isinstance(ts, bool):
                c[f"{source}_sans_date"] += 1
                continue
            if not isinstance(floor, (int, float)) or isinstance(floor, bool):
                c["malformes"] += 1
                continue
            if ts > plafond:
                c["futurs_ecartes"] += 1
                continue
            if ts < plancher:
                c[f"{source}_rassis"] += 1
                continue
            c[f"{source}_retenus"] += 1
            # ⛔ `_jour(ts)`, JAMAIS `_jour(maintenant)`. C'est la ligne que le
            #    lot 146 aurait ecrite de travers.
            lignes.append((u, _jour(ts), int(ts), source, floor, unite))
    return lignes, c


def fusionner(anciennes: list, nouvelles: list) -> tuple[list, dict]:
    """Fusionne le journal existant et les observations du run.

    ⭐⭐⭐ LA CLE EST (uuid, source, JOUR) — pas (uuid, source). Deux jours
    coexistent, c'est tout l'objet du journal ; deux lignes pour le MEME jour
    sont une erreur, et c'est le `ts` le plus grand qui tranche.

    ⛔ CE QUI EST DEJA AU JOURNAL N'EST JAMAIS RETIRE. Une ligne d'hier que le
    run d'aujourd'hui ne voit plus reste au journal : elle a ete observee. Un
    journal qui peut retrecir n'est pas un journal.
    -> [[regle-circuit-ouvert]]
    """
    par_cle: dict = {}
    for l in anciennes:
        par_cle[cle_de(l)] = l
    ecrasees = 0
    ajoutees = 0
    for l in nouvelles:
        k = cle_de(l)
        vieux = par_cle.get(k)
        if vieux is None:
            par_cle[k] = l
            ajoutees += 1
        elif int(l[2]) > int(vieux[2]):
            par_cle[k] = l
            ecrasees += 1
    sortie = sorted(par_cle.values(), key=lambda l: (l[1], l[3], l[0]))
    # 🔴🔴 LES DEUX TERMES CI-DESSOUS DOIVENT VALOIR ZERO — ET ILS SONT
    # ATTEIGNABLES. C'est toute la difference. Le lot 146 a laisse passer un
    # mutant parce qu'une somme de controle etait vraie PAR IDENTITE
    # ALGEBRIQUE : `len(x) - len(set(x))` sur une sortie deja dedoublonnee par
    # un dict ne peut pas etre non nul, donc ne mesure RIEN.
    #   · `perdues` est RECALCULE par difference d'ensembles, independamment du
    #     dict ci-dessus : si une refonte de cette fonction cesse de partir des
    #     anciennes, il devient non nul. (Mutant M5 : tue par ce terme.)
    #   · `doublons_entree` compte les cles en double DANS LE FICHIER RELU —
    #     cas reel : un journal concatene deux fois, ou edite a la main. Sans
    #     lui, ces doublons disparaissaient en silence dans le dict.
    cles_avant = [cle_de(l) for l in anciennes]
    rapport = {
        "avant": len(anciennes),
        "apres": len(sortie),
        "ajoutees": ajoutees,
        "rafraichies": ecrasees,
        "perdues": len(cles_perdues(anciennes, sortie)),
        "doublons_entree": len(cles_avant) - len(set(cles_avant)),
    }
    return sortie, rapport


def cle_de(ligne) -> tuple:
    """(uuid, source, jour). ⭐ Une seule definition, appelee partout — deux
    definitions de la meme cle qui divergent est le genre de faute qui sort
    verte pendant des semaines."""
    return (ligne[0], ligne[3], ligne[1])


def cles_perdues(anciennes: list, sortie: list) -> set:
    """L'INVARIANT, isole pour qu'un banc puisse l'ATTEINDRE.

    ⭐⭐⭐ POURQUOI IL EST A PART. Dans `fusionner`, une cle ne PEUT pas se
    perdre : le dict part des anciennes. Un compteur calcule la-dedans serait
    donc nul par construction — vrai par identite algebrique, et ne mesurant
    rien (mutant M4 du lot 146, mutant M10 de celui-ci). Sorti ici, le banc
    l'appelle avec une sortie amputee A LA MAIN et voit le compteur bouger.
    Un garde-fou dont on n'a jamais vu le rouge n'est pas un garde-fou.
    -> [[regle-banc-fabrique-la-condition]]
    """
    return {cle_de(l) for l in anciennes} - {cle_de(l) for l in sortie}


def lire(texte: str) -> list:
    """Relit un journal. ⛔ Une ligne illisible est SAUTEE, pas devinee — mais
    elle fait alors baisser `avant`, donc le garde-fou d'ecriture la verra."""
    if not texte.strip():
        return []
    out = []
    for r in csv.DictReader(io.StringIO(texte)):
        try:
            out.append((r["veve_uuid"], r["jour"], int(r["ts_obs"]),
                        r["source"], float(r["floor"]), r["unite"]))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def ecrire(lignes: list) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(COLONNES)
    w.writerows(lignes)
    return buf.getvalue()


def feu_vert(rapport: dict, compteurs: dict) -> tuple[bool, str]:
    """⭐⭐⭐ LE GARDE-FOU. Il ne juge PAS « est-ce que ca a marche » : il juge
    les TERMES QUI DOIVENT VALOIR ZERO, et le fait que le journal n'a pas
    retreci. Un garde-fou branche sur « y a-t-il des lignes ? » serait vert le
    jour ou l'etat se degrade — il y aurait toujours des lignes.
    -> [[regle-banc-deduit-au-lieu-de-compter]]
    """
    if rapport["perdues"]:
        return False, f"{rapport['perdues']} cle(s) PERDUE(S) — le journal ne retrecit jamais."
    if rapport["doublons_entree"]:
        return False, (f"{rapport['doublons_entree']} cle(s) EN DOUBLE dans le journal relu — "
                       f"fichier concatene ou edite a la main, on ne le reecrit pas.")
    if rapport["apres"] < rapport["avant"]:
        return False, f"journal plus court qu'avant ({rapport['apres']} < {rapport['avant']})."
    # 🔴🔴 CE SEUIL A ETE CORRIGE PAR LA MESURE, PAS PAR LE RAISONNEMENT.
    # Ecrit d'abord en « un seul futur bloque tout ». Rejoue sur l'etat reel :
    # 30 runs simules sur 30, TOUS BLOQUES. Cause : `last_refresh_ts` est ecrit
    # AU DEBUT du run de floor-watch, et la collecte continue ~30 min apres —
    # 6 192 des 7 143 observations lui sont donc POSTERIEURES. Le garde-fou
    # nommait « horloge suspecte » ce qui est le fonctionnement normal.
    # ⭐⭐⭐ UN GARDE-FOU QUI NOMME UNE CAUSE QU'IL NE DEPARTAGE PAS EST PIRE
    # QUE MUET : il aurait eteint le journal tous les jours en accusant
    # l'horloge. -> [[regle-banc-nomme-une-cause]]
    # Ce qui distingue une derive d'horloge d'un decalage normal, c'est la
    # PROPORTION : une horloge cassee decale TOUT, un jitter touche une poignee.
    retenus = compteurs.get("veve_retenus", 0) + compteurs.get("stackr_retenus", 0)
    futurs = compteurs.get("futurs_ecartes", 0)
    if futurs and futurs > max(20, 0.01 * (retenus + futurs)):
        return False, (f"{futurs} observation(s) au-dela de la tolerance d'horloge "
                       f"sur {retenus + futurs} — ce n'est plus du jitter, on n'ecrit pas.")
    if rapport["avant"] and not (rapport["ajoutees"] or rapport["rafraichies"]):
        # ⚠️ Pas un echec : un run peut n'apporter aucune observation fraiche
        #    (etat non rafraichi). On ecrit quand meme — le fichier est
        #    identique — mais on le DIT.
        return True, "aucun apport ce run (etat non rafraichi ?) — journal inchange."
    return True, ""


def resume(rapport: dict, compteurs: dict, mois: str) -> str:
    """⭐ IL COMPTE CE QUI A ETE ECRIT, IL NE DEDUIT PAS. Les termes a zero sont
    imprimes MEME a zero : un compteur qu'on n'imprime que s'il est non nul est
    indistinguable d'un compteur qui n'existe plus."""
    return (
        f"journal-prix {mois} : {rapport['apres']} ligne(s) "
        f"(avant {rapport['avant']}, +{rapport['ajoutees']} neuves, "
        f"{rapport['rafraichies']} rafraichies) · "
        f"veve {compteurs['veve_retenus']}/{compteurs['veve_total']} retenus "
        f"({compteurs['veve_rassis']} rassis, {compteurs['veve_sans_date']} sans date) · "
        f"stackr {compteurs['stackr_retenus']}/{compteurs['stackr_total']} retenus "
        f"({compteurs['stackr_rassis']} rassis, {compteurs['stackr_sans_date']} sans date) · "
        f"⚠️ DOIVENT VALOIR 0 -> perdues {rapport['perdues']}, "
        f"doublons_entree {rapport['doublons_entree']}, "
        f"futurs {compteurs['futurs_ecartes']}, malformes {compteurs['malformes']}"
    )


def mois_de(lignes: list, defaut: str) -> set:
    """Les mois touches par un lot de lignes. Un run de fin de mois en touche
    DEUX — et c'est le seul moment ou le bug de bascule se voit."""
    return {l[1][:7] for l in lignes} or {defaut}


# ─────────────────────────────────────────────────────────────────────────────
# LE PILOTE — lit des fichiers, n'appelle AUCUN reseau.
# Le workflow telecharge les mois existants AVANT, et televerse APRES.
# ─────────────────────────────────────────────────────────────────────────────
def main(argv=None) -> int:
    import argparse
    import gzip
    import json
    import os
    import sys
    import time

    p = argparse.ArgumentParser(description="Journal des prix — 1 point/piece/jour")
    p.add_argument("--etat", default="data/floor_state.json")
    p.add_argument("--dossier", default="data/journal",
                   help="ou vivent les prix-AAAA-MM.csv.gz deja telecharges")
    p.add_argument("--fenetre-h", type=float, default=24.0)
    p.add_argument("--maintenant", type=float, default=None,
                   help="epoch de reference (les bancs le fixent)")
    a = p.parse_args(argv)

    if not os.path.exists(a.etat):
        print("⛔ etat absent — rien a journaliser (ce n'est PAS 'tout va bien').")
        return 1
    etat = json.load(open(a.etat, encoding="utf-8"))
    # ⭐ On date sur l'horloge du RUN, pas sur `last_refresh_ts` de l'etat : si
    #   l'etat est vieux, il faut que ses lignes tombent HORS fenetre et que ca
    #   se voie. Se caler sur l'etat rendrait tout eternellement « frais ».
    maintenant = a.maintenant if a.maintenant is not None else time.time()

    lignes, compteurs = observations(etat, maintenant, a.fenetre_h)
    os.makedirs(a.dossier, exist_ok=True)
    mois_courant = _jour(maintenant)[:7]
    total_ok = True
    for mois in sorted(mois_de(lignes, mois_courant)):
        chemin = os.path.join(a.dossier, f"prix-{mois}.csv.gz")
        ancien = ""
        if os.path.exists(chemin):
            with gzip.open(chemin, "rt", encoding="utf-8") as f:
                ancien = f.read()
        avant = lire(ancien)
        du_mois = [l for l in lignes if l[1][:7] == mois]
        fusion, rapport = fusionner(avant, du_mois)
        ok, motif = feu_vert(rapport, compteurs)
        print(resume(rapport, compteurs, mois), flush=True)
        if motif:
            print(("⚠️ " if ok else "⛔ ") + motif, flush=True)
        if not ok:
            total_ok = False
            continue
        with gzip.open(chemin, "wt", encoding="utf-8", compresslevel=9) as f:
            f.write(ecrire(fusion))
        print(f"   ecrit {chemin} ({os.path.getsize(chemin)} o)", flush=True)
    return 0 if total_ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
