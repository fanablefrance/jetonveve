# ⚠️ DEPOT : fanablefrance/jetonveve   ·   CHEMIN : scraper/alertes_audit.py
# Le projet vit sur 6 depots et DEUX comptes GitHub. Un fichier
# depose au mauvais endroit ne provoque aucune erreur : il dort.

"""🔍 Le banc d'essai des alertes — AUCUN envoi, AUCUNE ecriture.

Ce script ne detecte rien et ne publie rien. Il REGARDE l'etat reel de
production (`data/floor_state.json`, ecrit par floor_watch a chaque run) et
les baselines multi-annees, puis il repond a trois questions qu'on ne peut
pas trancher a l'oeil :

  1. Qu'est-ce qui a VRAIMENT tire, et a quelle cadence ?
  2. Quelles valeurs aberrantes vont polluer les cartes ?
  3. Combien d'alertes produirait CHAQUE seuil, si on le changeait ?

═══ POURQUOI UN SCRIPT A PART, ET PAS UN MODE DE floor_watch ═══

floor_watch fait 2 220 lignes et tourne en production toutes les heures.
Un calibrage n'a pas besoin de son reseau, de ses webhooks, ni de ses
verrous — il a besoin de ses DONNEES. Les lire de l'exterieur ne peut rien
casser, et permet de relancer le banc autant de fois qu'on veut sans
toucher au collecteur.

═══ CE QU'IL FAUT SAVOIR AVANT DE LIRE SES CHIFFRES ═══

`floor_state.json` ne garde que l'ETAT COURANT, pas une serie temporelle.
On peut donc mesurer exactement :
  · ce qui a tire (les cooldowns `alerts_*` sont horodates) ;
  · ce qui tirerait MAINTENANT, seuil par seuil.
On ne peut PAS rejouer l'histoire heure par heure — pour ca il faudrait
`prices-full`, que ce script lit justement pour 📊 et 🔊.
Un chiffre qu'on ne peut pas etablir doit etre annonce comme tel, jamais
comble par une estimation qui aura l'air d'une mesure.
"""
import datetime as dt
import gzip
import json
import os
import sys
import urllib.request

ETAT = os.environ.get("AUDIT_ETAT", "data/floor_state.json")
BASELINES = os.environ.get(
    "BASELINES_SRC",
    "https://github.com/fanablefrance/jetonveve/releases/download/"
    "prices-full/prices_baselines.csv.gz")

# Au-dela, un prix n'est pas un prix : c'est une offre farfelue ou une
# faute de frappe. Sert d'etalon a l'audit (et de plafond propose au code).
PRIX_MAX = float(os.environ.get("FLOOR_PRIX_MAX", "100000"))
PLANCHER = float(os.environ.get("VEVE_PRIX_PLANCHER", "1"))


def _j(ts):
    return dt.datetime.utcfromtimestamp(ts).strftime("%d/%m %H:%M")


def charger_etat(chemin=ETAT):
    with open(chemin, "r", encoding="utf-8") as f:
        return json.load(f)


def charger_baselines(url=BASELINES, timeout=60):
    """{uuid: {floor_min, p5, p25, p50, ...}} ou {} si injoignable.

    Ne leve jamais : un banc d'essai qui plante parce qu'une release est
    momentanement indisponible ne sert a personne.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            brut = r.read()
        texte = gzip.decompress(brut).decode("utf-8") if url.endswith(".gz") \
            else brut.decode("utf-8")
    except Exception as err:                                # noqa: BLE001
        print(f"  ⚠️ baselines injoignables ({err}) — sections 📊/🔊 sautees.")
        return {}
    lignes = texte.splitlines()
    if not lignes:
        return {}
    entetes = lignes[0].split(",")
    out = {}
    for l in lignes[1:]:
        ch = l.split(",")
        if len(ch) != len(entetes):
            continue
        d = dict(zip(entetes, ch))
        cle = d.get("veve_uuid") or d.get("uuid")
        if not cle:
            continue
        conv = {}
        for k, v in d.items():
            if k in ("veve_uuid", "uuid"):
                continue
            try:
                conv[k] = float(v)
            except (TypeError, ValueError):
                pass
        out[cle] = conv
    return out


# ─────────────────────────────────────────────────── outils de mesure
def _paires(etat, cle, i=0):
    """{uuid: valeur} depuis une entree {uuid: [v, ts, …]} de l'etat."""
    return {k: v[i] for k, v in (etat.get(cle) or {}).items()
            if isinstance(v, list) and len(v) > i}


def cadence(etat):
    """Ce qui a REELLEMENT tire, par signal et par jour."""
    DET = [("alerts", "🚨 affaires (canal principal)"),
           ("alerts_steal", "🩸 chute du floor"),
           ("alerts_atl", "📉 plus-bas"),
           ("alerts_ath", "🆕 plus-haut"),
           ("alerts_spike", "📈 vente au-dessus du floor"),
           ("alerts_pic", "🔥 pic d'activite"),
           ("alerts_atl_stackr", "📉 plus-bas StackR"),
           ("alerts_histlow", "📊 bas de distribution"),
           ("alerts_vol", "🔊 anomalie d'offres")]
    tous = [t for k, _ in DET for t in (etat.get(k) or {}).values()]
    if not tous:
        print("  (aucun cooldown en etat : rien n'a jamais tire)")
        return
    t0, t1 = min(tous), max(tous)
    jours = max((t1 - t0) / 86400, 0.01)
    print(f"  fenetre : {_j(t0)} -> {_j(t1)}  ({jours:.1f} jours)\n")
    print(f"  {'signal':<32}{'n':>5}{'/jour':>8}   dernier tir")
    for cle, nom in DET:
        v = etat.get(cle) or {}
        dernier = _j(max(v.values())) if v else "jamais"
        print(f"  {nom:<32}{len(v):>5}{len(v)/jours:>8.1f}   {dernier}")
    print("\n  ⚠️ Un signal muet depuis plusieurs jours n'est pas forcement")
    print("     calme : il peut etre eteint, ou baillonne par un garde-fou.")


def aberrations(etat):
    """Les valeurs qui produiraient des cartes invraisemblables."""
    vf = _paires(etat, "vfloors")
    ventes = _paires(etat, "sales")
    hauts = {k: v for k, v in vf.items() if v > PRIX_MAX}
    gros = {k: v for k, v in ventes.items() if v > PRIX_MAX}
    print(f"  seuil de vraisemblance retenu : {PRIX_MAX:,.0f} $\n")
    print(f"  floors au-dessus  : {len(hauts):>4} / {len(vf)} items")
    for k, v in sorted(hauts.items(), key=lambda x: -x[1])[:8]:
        vend = ventes.get(k, 0)
        note = f"  (vente reelle vue : {vend:,.0f} $)" if vend else \
               "  (aucune vente — REQUIRE_SALE l'ecarte deja)"
        print(f"      {k[:8]}…  {v:>20,.0f} ${note}")
    print(f"\n  ventes au-dessus  : {len(gros):>4} / {len(ventes)} items")
    for k, v in sorted(gros.items(), key=lambda x: -x[1])[:8]:
        f = vf.get(k, 0)
        r = f" -> ratio ×{v / f:,.0f} pour 📈" if f > 0 else ""
        print(f"      {k[:8]}…  {v:>20,.0f} ${r}")
    print("\n  ⭐ Une seule carte « vendu 18 666 667 $ » decredibilise le canal")
    print("     entier. Le volume n'est pas le sujet ici, la vraisemblance si.")
    return hauts, gros


def balayage_atl(etat):
    """📉 : combien d'items sont a leur plus-bas connu, et depuis quand
    cette « histoire » existe-t-elle vraiment ?"""
    vf = _paires(etat, "vfloors")
    atl = _paires(etat, "atl_seen")
    ts_atl = _paires(etat, "atl_seen", 1)
    elig = {k: v for k, v in vf.items() if PLANCHER < v <= PRIX_MAX}
    au_bas = [k for k in elig if 0 < atl.get(k, 0) and elig[k] <= atl[k]]
    print(f"  items eligibles (entre {PLANCHER:g} $ et {PRIX_MAX:,.0f} $) : {len(elig)}")
    print(f"  ...dont le floor EST leur plus-bas connu          : {len(au_bas)}")
    if ts_atl:
        vieux = min(ts_atl.values())
        recent = max(ts_atl.values())
        print(f"\n  ⭐ profondeur reelle de la reference : {_j(vieux)} -> {_j(recent)}")
        print(f"     soit {(recent - vieux)/86400:.1f} jours d'histoire.")
        print("     Le signal s'appelle « plus-bas HISTORIQUE ». S'il ne repose")
        print("     que sur quelques jours, le nom promet plus que la donnee.")
        print("     C'est exactement le trou que 📊 (multi-annees) doit combler.")


def balayage_histlow(etat, base):
    """📊 : combien d'items seraient « dans le bas de leur histoire », pour
    chaque valeur de HISTLOW_PCT ?"""
    if not base:
        print("  (pas de baselines : section sautee)")
        return
    vf = _paires(etat, "vfloors")
    ventes = _paires(etat, "sales")
    communs = [k for k in vf if k in base]
    print(f"  items avec une baseline multi-annees : {len(communs)} "
          f"(sur {len(vf)} suivis)\n")
    print(f"  {'HISTLOW_PCT':>12} {'candidats':>10} {'avec vente':>12}"
          f" {'>=30 points':>12}")
    for pct in (5, 10, 15, 20, 25, 30):
        n = nv = np_ = 0
        for k in communs:
            b = base[k]
            seuil = b.get(f"p{pct}") or b.get("p5")
            if seuil is None or not (PLANCHER < vf[k] <= PRIX_MAX):
                continue
            if vf[k] <= seuil:
                n += 1
                if ventes.get(k, 0) > 0:
                    nv += 1
                if (b.get("n_points") or 0) >= 30:
                    np_ += 1
        print(f"  {pct:>11} % {n:>10} {nv:>12} {np_:>12}")
    print("\n  Lire la colonne « avec vente » : c'est elle qui compte, le code")
    print("  exige une preuve de vente. Vise un volume que TU accepterais de")
    print("  lire chaque jour — pas le seuil qui produit le plus de candidats.")


def balayage_vol(etat, base):
    """🔊 : combien d'items ont un nombre d'offres anormal, par ratio ?"""
    if not base:
        print("  (pas de baselines : section sautee)")
        return
    listings = _paires(etat, "vlistings") or {}
    if not listings:
        print("  ⚠️ l'etat ne contient pas le nombre d'offres courant")
        print("     (`vlistings`) : ce balayage exige que floor_watch le")
        print("     memorise. Sans lui, 🔊 ne peut pas etre calibre a froid —")
        print("     il faudra le regler par un run en `simuler=oui`.")
        return
    print(f"  {'VOL_RATIO':>10} {'candidats':>10}")
    for ratio in (2, 3, 4, 5, 8):
        n = 0
        for k, l in listings.items():
            b = base.get(k)
            if not b:
                continue
            med = b.get("listings_p50") or 0
            p90 = b.get("listings_p90") or 0
            if med > 0 and l >= ratio * med and l > p90:
                n += 1
        print(f"  {ratio:>9}× {n:>10}")


def main():
    try:
        etat = charger_etat()
    except FileNotFoundError:
        print(f"❌ {ETAT} introuvable. Ce script se lance depuis la racine du "
              "depot jetonveve, apres au moins un run de floor_watch.",
              file=sys.stderr)
        return 2

    titres = [
        ("1. CE QUI A VRAIMENT TIRE", lambda: cadence(etat)),
        ("2. LES VALEURS ABERRANTES", lambda: aberrations(etat)),
        ("3. 📉 PLUS-BAS : MATIERE DISPONIBLE ET PROFONDEUR REELLE",
         lambda: balayage_atl(etat)),
    ]
    for titre, fn in titres:
        print("\n" + "═" * 70)
        print(f"  {titre}")
        print("═" * 70)
        fn()

    print("\n" + "═" * 70)
    print("  4. 📊 BAS DE DISTRIBUTION — BALAYAGE DE SEUILS")
    print("═" * 70)
    base = charger_baselines()
    if base:
        print(f"  baselines chargees : {len(base)} items\n")
    balayage_histlow(etat, base)

    print("\n" + "═" * 70)
    print("  5. 🔊 ANOMALIE D'OFFRES — BALAYAGE DE SEUILS")
    print("═" * 70)
    balayage_vol(etat, base)

    print("\n" + "═" * 70)
    print("  Aucun message n'a ete envoye, aucun fichier ecrit.")
    print("═" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
