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
# ⚠️ `.get(nom, defaut)` rend la CHAINE VIDE si la variable existe mais
# est vide — ce qui est exactement ce que fait un champ de lancement laisse
# blanc. Le defaut n'est alors JAMAIS applique, et urlopen("") echoue sur
# « unknown url type ». Vu en vrai au 1er lancement du 20/07.
_DEFAUT_BASELINES = ("https://github.com/fanablefrance/jetonveve/releases/"
                     "download/prices-full/prices_baselines.csv.gz")
BASELINES = os.environ.get("BASELINES_SRC", "").strip() or _DEFAUT_BASELINES

# Au-dela, un prix n'est pas un prix : c'est une offre farfelue ou une
# faute de frappe. Sert d'etalon a l'audit (et de plafond propose au code).
PRIX_MAX = float(os.environ.get("FLOOR_PRIX_MAX", "").strip() or 50000)
PLANCHER = float(os.environ.get("VEVE_PRIX_PLANCHER", "").strip() or 1)


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
    # ⚠️ 🎯 et 📚 n'ont PAS de registre `alerts_*` : ils utilisent
    # `mints_vus` et `comics_vus`. Les oublier, c'est passer a cote des
    # DEUX signaux les plus actifs — l'erreur que j'ai faite le 19/07 en
    # concluant a tort que le canal principal etait muet.
    DET = [("mints_vus", "🎯 numeros"),
           ("comics_vus", "📚 comics"),
           ("alerts", "🚨 affaires (arbitrage)"),
           ("alerts_steal", "🩸 chute du floor"),
           ("alerts_atl", "📉 plus-bas"),
           ("alerts_ath", "🆕 plus-haut"),
           ("alerts_spike", "📈 vente au-dessus du floor"),
           ("alerts_pic", "🔥 pic d'activite"),
           ("alerts_atl_stackr", "📉 plus-bas StackR"),
           ("alerts_histlow", "📊 bas de distribution"),
           ("alerts_vol", "🔊 anomalie d'offres")]
    tous = [t for k, _ in DET for t in (etat.get(k) or {}).values()
            if isinstance(t, (int, float))]
    if not tous:
        print("  (aucun cooldown en etat : rien n'a jamais tire)")
        return
    t0, t1 = min(tous), max(tous)
    jours = max((t1 - t0) / 86400, 0.01)
    print(f"  fenetre : {_j(t0)} -> {_j(t1)}  ({jours:.1f} jours)\n")
    print(f"  {'signal':<32}{'n':>5}{'/jour':>8}   dernier tir")
    for cle, nom in DET:
        v = [t for t in (etat.get(cle) or {}).values()
             if isinstance(t, (int, float))]
        dernier = _j(max(v)) if v else "jamais"
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
        print(f"\n  `atl_seen` couvre {_j(vieux)} -> {_j(recent)}, soit "
              f"{(recent - vieux)/86400:.1f} jours.")
        print("  ⚠️ NE PAS EN CONCLURE que 📉 ne connait que ces jours-la :")
        print("     `detect_atl` prend min(ATL du catalogue, plus-bas observe),")
        print("     et l'ATL du catalogue vient de `allTimeLowest` — le vrai")
        print("     plus-bas publie par VeVe. Voir la section 3bis.")


def presence(etat):
    """⭐ Combien de fois le collecteur a-t-il REELLEMENT tourne ?

    On ne peut pas interroger GitHub Actions depuis ici. Mais
    `hprix_emitted` garde {uuid: [floor, ts]} : le ts du moment ou ce floor
    a ete pousse vers H-PRIX. Une emission n'a lieu qu'a un
    rafraichissement — les timestamps distincts sont donc les empreintes
    des runs reels.

    ⚠️ UN ARTEFACT A ECARTER AVANT DE CONCLURE : seule la DERNIERE
    emission par item est gardee. Si le tampon etait plein, il masquerait
    les runs anciens. On verifie donc d'abord qu'il reste de la place :
    si `total / emis_par_run` est nettement superieur au nombre de runs
    observes, le tampon n'est pas la contrainte, et le compte est reel.
    """
    emis = [v[1] for v in (etat.get("hprix_emitted") or {}).values()
            if isinstance(v, list) and len(v) > 1]
    if len(emis) < 50:
        print("  (pas assez d'emissions en etat pour conclure)")
        return
    par_run = {}
    for t in emis:
        cle = round(t / 60) * 60          # un run emet tout au meme instant
        par_run[cle] = par_run.get(cle, 0) + 1
    runs = sorted(par_run)
    tailles = sorted(par_run.values())
    med = tailles[len(tailles) // 2]
    capacite = len(emis) / max(med, 1)
    span_h = (runs[-1] - runs[0]) / 3600

    print(f"  runs identifies      : {len(runs)}")
    print(f"  fenetre couverte     : {span_h:.0f} h "
          f"({_j(runs[0])} -> {_j(runs[-1])})")
    print(f"  attendus sur cette fenetre (cron horaire) : {span_h:.0f}")
    print()
    print(f"  capacite du tampon   : ~{capacite:.0f} runs "
          f"({len(emis)} emissions / {med} par run)")
    if capacite > len(runs) * 1.5:
        print("  ✅ le tampon n'est PAS la contrainte : il reste de la place,")
        print("     donc le compte de runs est reel, pas tronque.")
        print()
        print(f"  ➡️  TAUX DE PRESENCE : {len(runs)/max(span_h,1)*100:.0f} % "
              f"— un run toutes les {span_h/max(len(runs)-1,1):.1f} h "
              "au lieu d'une heure.")
    else:
        print("  ⚠️ le tampon est peut-etre plein : le compte est un MINORANT,")
        print("     seule la duree couverte est exploitable.")
    print()
    print("  Reserve : un run qui n'emet AUCUN changement n'apparait pas.")
    print("  Avec ~140 floors qui bougent par run c'est peu probable, mais")
    print("  possible la nuit — le chiffre est donc un intervalle MAXIMUM.")


def reference_atl(etat, catalogue=None):
    """Sur quoi 📉 s'appuie-t-il vraiment : le catalogue ou l'observation ?

    `detect_atl` prend `min(atl_catalogue, atl_observe)`. L'ATL du
    catalogue vient de `stats.allTimeLowest` — le vrai plus-bas publie par
    VeVe. Il est donc faux de croire que le signal ne connait que quelques
    jours d'histoire : verifions la part de chacun.
    """
    import csv
    chemin = catalogue or os.environ.get("AUDIT_CATALOGUE", "_preda/data/elements.csv")
    try:
        with open(chemin, encoding="utf-8", errors="replace") as f:
            cat = {}
            for l in csv.DictReader(f):
                u = (l.get("uuid") or l.get("veve_uuid") or "").strip()
                try:
                    cat[u] = float(l.get("atl") or 0)
                except (TypeError, ValueError):
                    pass
    except FileNotFoundError:
        print(f"  (catalogue introuvable : {chemin} — section sautee)")
        return
    vf = _paires(etat, "vfloors")
    seen = _paires(etat, "atl_seen")
    sains = [u for u in vf if PLANCHER < vf[u] <= PRIX_MAX and u in cat and u in seen]
    if not sains:
        print("  (aucun item commun)")
        return
    cat_gagne = sum(1 for u in sains if 0 < cat[u] < seen[u])
    sans_cat = sum(1 for u in sains if cat[u] <= 0)
    print(f"  items examines : {len(sains)}")
    print(f"  reference = ATL du CATALOGUE (VeVe, multi-annees) : "
          f"{cat_gagne} ({cat_gagne/len(sains)*100:.0f} %)")
    print(f"  reference = plus-bas observe en direct           : "
          f"{len(sains)-cat_gagne-sans_cat} "
          f"({(len(sains)-cat_gagne-sans_cat)/len(sains)*100:.0f} %)")
    print(f"  aucun ATL au catalogue                           : {sans_cat}")


def balayage_histlow(etat, base):
    """📊 : combien d'items seraient « dans le bas de leur histoire », pour
    chaque valeur de HISTLOW_PCT ?

    ⚠️ BUG CORRIGE LE 20/07 : je cherchais des cles `p5`, `p25`… alors que
    les baselines s'appellent `floor_p5`, `floor_p25`. Toutes les cles
    etaient donc None, et le balayage rendait ZERO candidat a tous les
    seuils — un resultat qui avait l'air d'une mesure.
    Parade : on n'ecrit plus le calcul, on APPELLE `price_baseline.pct_rank`,
    la fonction que le vrai detecteur utilise. Le banc ne peut plus diverger
    du detecteur qu'il est cense mesurer.
    """
    if not base:
        print("  (pas de baselines : section sautee)")
        return
    try:
        from scraper.price_baseline import pct_rank
    except ImportError:
        print("  ⚠️ scraper/price_baseline.py introuvable — section sautee.")
        return

    vf = _paires(etat, "vfloors")
    ventes = _paires(etat, "sales")
    communs = [k for k in vf if k in base]
    print(f"  items avec une baseline multi-annees : {len(communs)} "
          f"(sur {len(vf)} suivis)\n")

    # Le rang de chaque item est calcule UNE fois, par la fonction du
    # detecteur ; les seuils ne font ensuite que decouper cette liste.
    rangs = []
    for k in communs:
        if not (PLANCHER < vf[k] <= PRIX_MAX):
            continue
        r = pct_rank(base[k], vf[k])
        if r is None:
            continue
        rangs.append((r, ventes.get(k, 0) > 0,
                      (base[k].get("n_points") or 0) >= 30))
    if not rangs:
        print("  ⚠️ aucun rang calculable — les baselines n'ont pas les")
        print("     colonnes attendues (floor_min, floor_p5, …).")
        return

    print(f"  rangs calcules : {len(rangs)}")
    tries = sorted(r for r, _, _ in rangs)
    print(f"  distribution des rangs : min {tries[0]:.0f} · "
          f"p25 {tries[len(tries)//4]:.0f} · median {tries[len(tries)//2]:.0f} · "
          f"max {tries[-1]:.0f}\n")
    tot = len(rangs)
    print(f"  {'HISTLOW_PCT':>12} {'dans la bande':>14} {'avec vente':>12}"
          f" {'>=30 points':>12} {'% du suivi':>11}")
    for pct in (5, 10, 15, 20, 25, 30):
        sel = [(v, n) for r, v, n in rangs if r <= pct]
        avec = sum(1 for v, _ in sel if v)
        assez = sum(1 for v, n in sel if v and n)
        print(f"  {pct:>11} % {len(sel):>14} {avec:>12} {assez:>12}"
              f" {100.0*assez/max(tot,1):>10.1f} %")
    print()
    print("  ⚠️ CE TABLEAU EST UN STOCK, PAS UN DEBIT.")
    print("  Il compte les items ASSIS dans la bande a l'instant du releve.")
    print("  Depuis le correctif du 20/07, 📊 ne signale plus un etat mais une")
    print("  ENTREE dans la bande — comme 📉 signale un franchissement. Le")
    print("  stock ci-dessus sera donc absorbe EN SILENCE au premier run")
    print("  (l'amorcage), et tu ne recevras ensuite que les entrees reelles.")
    print()
    print("  ➡️ Lis ces chiffres comme une SELECTIVITE, pas comme un volume :")
    print("     plus la colonne « >=30 points » est petite, plus la bande est")
    print("     etroite, et plus une entree y est un evenement rare.")
    print("  ➡️ Le debit reel ne se deduit pas d'un seul releve : il demande")
    print("     deux instantanes. Le plafond, lui, est garanti par HISTLOW_MAX")
    print("     (10 par run), et un debordement se DIT desormais sur stderr.")
    print("  ➡️ Depart conseille : HISTLOW_PCT=5. Tu peux l'elargir apres")
    print("     quelques jours ; l'inverse est plus penible a vivre.")


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
        ("3bis. SUR QUOI 📉 S'APPUIE-T-IL VRAIMENT ?",
         lambda: reference_atl(etat)),
        ("3ter. ⭐ COMBIEN DE FOIS LE COLLECTEUR A-T-IL TOURNE ?",
         lambda: presence(etat)),
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
