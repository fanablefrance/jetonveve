# ⚠️ DEPOT : fanablefrance/jetonveve   ·   CHEMIN : tests/test_journal_prix.py
"""🔬 BANC DU JOURNAL DES PRIX — 12 sections, 45 controles, juges par 9 mutants.

🔴🔴 OU CE BANC TOURNE, ET POURQUOI CA COMPTE
---------------------------------------------
`fanablefrance/jetonveve` N'A AUCUN WORKFLOW DE TESTS. Un banc pose dans
`tests/` y serait donc **MUET** : zero run, zero rouge, et le silence
ressemblerait trait pour trait a un succes. -> [[regle-silence-du-non-execute]]

⇒ IL EST APPELE PAR `.github/workflows/floor-watch.yml`, **avant** l'etape qui
ecrit le journal, a chaque run horaire. Cout mesure : < 1 s. S'il rouge, le
journal n'est pas ecrit — et la recolte, elle, est sauvee quand meme par
l'etape `if: always()` qui suit. -> [[regle-banc-au-bon-moment]]

⛔ SANS PYTEST : jetonveve ne l'installe pas dans ce workflow, et ajouter une
dependance a une chaine horaire pour lancer quarante-cinq `assert` serait payer cher un
confort. `python tests/test_journal_prix.py` -> code de sortie 0 ou 1.

⭐⭐⭐ CE BANC A ETE JUGE EN LUI INJECTANT DU MAUVAIS CODE (9 mutants, table
dans LISEZ-MOI-LOT-147.md). Deux lecons du lot 146 y sont appliquees :
  · une somme vraie PAR IDENTITE ALGEBRIQUE ne mesure rien — il faut qu'un
    terme DOIVE VALOIR ZERO (ici : `perdues`, `doublons_entree`, `futurs_ecartes`) — et surtout, un
    terme a zero ne prouve rien s'il ne PEUT pas devenir non nul : les trois
    sont atteignables, et un controle les y amene ;
  · deux nœuds decides par UN SEUL fait ne se contredisent jamais — on ne
    compare donc jamais deux grandeurs issues du meme calcul.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper.journal_prix import (  # noqa: E402
    cle_de, cles_perdues, ecrire, feu_vert, fusionner, lire, mois_de, observations,
)

# 2026-08-13 12:00:00 UTC — reference fixe. ⛔ Jamais `time.time()` dans un
# banc : un banc qui depend de l'heure ou il tourne mesure l'heure.
T = 1786622400.0
JOUR = "2026-08-13"
HIER = "2026-08-12"

echecs = []


def v(nom, condition, detail=""):
    if condition:
        print(f"  ✅ {nom}")
    else:
        print(f"  ❌ {nom} — {detail}")
        echecs.append(nom)


def etat(vfloors=None, sfloors=None):
    return {"vfloors": vfloors or {}, "sfloors": sfloors or {}}


# ─────────────────────────────────────────────────────────────────────────────
print("§1 — LE JOUR VIENT DU `ts` D'OBSERVATION, JAMAIS DE L'HORLOGE DU RUN")
# ⭐⭐⭐ C'est LA faute du lot 146, transposee ici : dater d'aujourd'hui une
# observation d'hier. On la rend impossible a manquer en mettant les deux dans
# le MEME etat, au MEME run : si le code lisait l'horloge du run, les deux
# lignes porteraient le meme jour.
e = etat(vfloors={
    "aaa": [10.0, T - 1800],        # il y a 30 min  -> aujourd'hui
    "bbb": [20.0, T - 20 * 3600],   # il y a 20 h    -> HIER (T-20h = 16 h UTC hier)
})
lignes, c = observations(e, T, fenetre_h=24)
jours = {l[0]: l[1] for l in lignes}
v("deux observations, deux jours DIFFERENTS", jours.get("aaa") != jours.get("bbb"),
  f"aaa={jours.get('aaa')} bbb={jours.get('bbb')}")
v("la recente est datee du jour du run", jours.get("aaa") == JOUR, jours.get("aaa"))
v("la vieille est datee de LA VEILLE", jours.get("bbb") == HIER, jours.get("bbb"))

# ─────────────────────────────────────────────────────────────────────────────
print("§2 — LA FENETRE SE TESTE PAR SES DEUX BOUTS")
# -> [[regle-echantillon-ne-contient-pas]] : un intervalle dont on ne teste
#    qu'un bord laisse passer un `<` ecrit `<=` ou une fenetre doublee.
e = etat(vfloors={
    "dedans": [1.0, T - 23.5 * 3600],   # 23 h 30 -> retenu
    "dehors": [1.0, T - 24.5 * 3600],   # 24 h 30 -> ecarte
})
lignes, c = observations(e, T, fenetre_h=24)
gardes = {l[0] for l in lignes}
v("23 h 30 est RETENU", "dedans" in gardes)
v("24 h 30 est ECARTE", "dehors" not in gardes)
v("l'ecarte est COMPTE, pas perdu de vue", c["veve_rassis"] == 1, c["veve_rassis"])

# ─────────────────────────────────────────────────────────────────────────────
print("§3 — DEUX MARCHES, DEUX LIGNES — jamais fusionnes, jamais convertis")
e = etat(vfloors={"x": [9.99, T - 60]},
         sfloors={"x": [500000.0, "nom", "rare", "img", T - 60]})
lignes, c = observations(e, T)
v("la meme piece le meme jour donne 2 lignes", len(lignes) == 2, len(lignes))
unites = {l[3]: l[5] for l in lignes}
v("veve est en USD", unites.get("veve") == "USD", unites)
v("stackr est en OMI", unites.get("stackr") == "OMI", unites)
fus, r = fusionner([], lignes)
v("la fusion garde les DEUX (la source est dans la cle)", r["apres"] == 2, r)

# ─────────────────────────────────────────────────────────────────────────────
print("§4 — IDEMPOTENCE : 24 runs par jour ne font pas 24 lignes")
e = etat(vfloors={"z": [5.0, T - 60]})
l1, _ = observations(e, T)
j1, r1 = fusionner([], l1)
j2, r2 = fusionner(j1, l1)          # meme run, rejoue
v("le 2e passage n'ajoute RIEN", r2["ajoutees"] == 0, r2)
v("le journal ne grossit pas", len(j2) == len(j1), (len(j1), len(j2)))
v("terme qui DOIT valoir zero : perdues", r2["perdues"] == 0, r2)
v("terme qui DOIT valoir zero : doublons_entree", r2["doublons_entree"] == 0, r2)

# ─────────────────────────────────────────────────────────────────────────────
print("§5 — L'OBSERVATION LA PLUS RECENTE DU JOUR GAGNE (prix de cloture)")
tot, _ = fusionner([], [("z", JOUR, int(T - 7200), "veve", 5.0, "USD")])
tot, r = fusionner(tot, [("z", JOUR, int(T - 60), "veve", 8.0, "USD")])
v("une seule ligne pour le jour", len(tot) == 1, tot)
v("c'est la valeur la plus RECENTE", tot[0][4] == 8.0, tot)
v("et elle est comptee comme rafraichie, pas ajoutee",
  r["rafraichies"] == 1 and r["ajoutees"] == 0, r)
# ⭐ le sens inverse : une observation PLUS VIEILLE ne doit pas ecraser
tot2, r2 = fusionner(tot, [("z", JOUR, int(T - 9999), "veve", 1.0, "USD")])
v("une observation plus VIEILLE n'ecrase pas", tot2[0][4] == 8.0, tot2)

# ─────────────────────────────────────────────────────────────────────────────
print("§6 — LE JOURNAL NE PERD JAMAIS UNE LIGNE")
vieux = [("a", HIER, int(T - 90000), "veve", 1.0, "USD"),
         ("b", HIER, int(T - 90000), "veve", 2.0, "USD")]
neuf = [("c", JOUR, int(T - 60), "veve", 3.0, "USD")]
tot, r = fusionner(vieux, neuf)
v("les lignes d'hier survivent au run d'aujourd'hui", len(tot) == 3, len(tot))
# ⭐⭐ LE BANC RECOMPTE LUI-MEME au lieu de croire `rapport`. Deux nœuds decides
# par UN SEUL fait ne se contredisent jamais (lecon du lot 146, mutant M7) :
# si `fusionner` cessait de partir des anciennes, un controle qui se contente
# de lire `r["perdues"]` serait decide par le meme code que le bug.
cles_avant = {cle_de(l) for l in vieux}
cles_apres = {cle_de(l) for l in tot}
v("AUCUNE cle d'hier ne manque (recompte par le banc)",
  not (cles_avant - cles_apres), cles_avant - cles_apres)
v("terme qui DOIT valoir zero : perdues", r["perdues"] == 0, r)
# ⭐⭐⭐ ET ON VA CHERCHER SON ROUGE. `perdues` ne peut PAS devenir non nul a
# travers `fusionner` — le dict part des anciennes. Un compteur dont on n'a
# jamais vu le rouge est un compteur qu'on croit sur parole : on ampute la
# sortie A LA MAIN et on verifie que l'invariant le dit.
ampute = [l for l in tot if l[0] != "a"]
v("une sortie amputee EST detectee", cles_perdues(vieux, ampute) == {("a", "veve", HIER)},
  cles_perdues(vieux, ampute))
v("et une sortie complete ne declenche rien", cles_perdues(vieux, tot) == set(),
  cles_perdues(vieux, tot))
ok, motif = feu_vert(r, {"futurs_ecartes": 0, "malformes": 0})
v("feu vert", ok, motif)

# ─────────────────────────────────────────────────────────────────────────────
print("§7 — LE GARDE-FOU REFUSE UN JOURNAL QUI RETRECIT")
# ⭐ On ne lui donne PAS un rapport fabrique a la main sur les deux champs : on
#   lui donne un cas ou `apres < avant` SANS que `perdues` bouge, pour que le
#   controle ne puisse pas etre satisfait par un seul fait.
ok, motif = feu_vert({"perdues": 0, "doublons_entree": 0, "apres": 5, "avant": 9,
                      "ajoutees": 0, "rafraichies": 0},
                     {"futurs_ecartes": 0, "malformes": 0})
v("un journal plus court est REFUSE", not ok, motif)
ok, motif = feu_vert({"perdues": 3, "doublons_entree": 0, "apres": 9, "avant": 9,
                      "ajoutees": 0, "rafraichies": 0},
                     {"futurs_ecartes": 0, "malformes": 0})
v("des lignes perdues sont REFUSEES meme a longueur egale", not ok, motif)
# ⭐⭐⭐ ET LE CAS REEL, PAS UN RAPPORT FABRIQUE A LA MAIN : un journal relu
# qui porte DEUX FOIS la meme cle (fichier concatene, edite a la main). Sans ce
# controle, `doublons_entree` serait un compteur vrai par identite algebrique —
# c'est exactement ce qui a laisse passer le mutant M4 du lot 146.
double = [("a", HIER, int(T - 90000), "veve", 1.0, "USD"),
          ("a", HIER, int(T - 90000), "veve", 7.0, "USD")]
_, rd = fusionner(double, [])
v("un journal relu en double est DETECTE", rd["doublons_entree"] == 1, rd)
okd, motifd = feu_vert(rd, {"futurs_ecartes": 0, "malformes": 0})
v("et il est REFUSE a l'ecriture", not okd, motifd)

# ─────────────────────────────────────────────────────────────────────────────
print("§8 — UNE OBSERVATION DANS LE FUTUR ARRETE TOUT")
e = etat(vfloors={"f": [1.0, T + 4 * 3600]})
lignes, c = observations(e, T)
v("elle n'entre pas au journal", lignes == [], lignes)
v("elle est COMPTEE (pas ignoree en silence)", c["futurs_ecartes"] == 1, c)
# ⭐⭐⭐ LE SEUIL SE TESTE PAR SES DEUX BOUTS, ET IL A ETE CORRIGE PAR LA MESURE.
# Version d'origine : « un seul futur bloque tout ». Rejoue sur l'etat reel,
# elle bloquait 30 runs sur 30 — `last_refresh_ts` est pose AU DEBUT du run et
# 6 192 observations sur 7 143 lui sont posterieures. Un garde-fou qui accuse
# l'horloge pour le fonctionnement normal est pire que pas de garde-fou.
base = {"perdues": 0, "doublons_entree": 0, "apres": 1, "avant": 1,
        "ajoutees": 1, "rafraichies": 0}
jitter = {"futurs_ecartes": 7, "malformes": 0, "veve_retenus": 7000, "stackr_retenus": 143}
ok, motif = feu_vert(base, jitter)
v("7 futurs sur 7 150 = jitter, on ecrit quand meme", ok, motif)
casse = {"futurs_ecartes": 3000, "malformes": 0, "veve_retenus": 4000, "stackr_retenus": 143}
ok, motif = feu_vert(base, casse)
v("3 000 futurs sur 7 143 = horloge cassee, on n'ecrit PAS", not ok, motif)
petit = {"futurs_ecartes": 21, "malformes": 0, "veve_retenus": 5, "stackr_retenus": 0}
ok, motif = feu_vert(base, petit)
v("plancher absolu : 21 futurs sur 26 est REFUSE", not ok, motif)
# ⭐ le bord : 5 min dans le futur reste dans la tolerance d'horloge
e = etat(vfloors={"f": [1.0, T + 300]})
lignes, c = observations(e, T)
v("+5 min reste tolere (derive d'horloge normale)",
  len(lignes) == 1 and c["futurs_ecartes"] == 0, c)

# ─────────────────────────────────────────────────────────────────────────────
print("§9 — LA BASCULE DE MOIS PRODUIT DEUX FICHIERS, PAS UN")
# 2026-09-01 00:30 UTC : la fenetre de 24 h enjambe aout et septembre.
T2 = 1788222600.0
e = etat(vfloors={"a": [1.0, T2 - 60], "b": [2.0, T2 - 6 * 3600]})
lignes, c = observations(e, T2)
m = mois_de(lignes, "2026-09")
v("deux mois sont touches", m == {"2026-08", "2026-09"}, m)
v("aucune ligne n'est perdue a la bascule", len(lignes) == 2, lignes)

# ─────────────────────────────────────────────────────────────────────────────
print("§10 — ALLER-RETOUR CSV : ce qu'on ecrit est ce qu'on relit")
src = [("u1", JOUR, int(T), "veve", 12.34, "USD"),
       ("u2", HIER, int(T - 86400), "stackr", 999999.0, "OMI")]
relu = lire(ecrire(src))
v("aller-retour fidele", relu == src, relu)
v("un fichier vide se relit en liste vide", lire("") == [], "?")
v("un en-tete seul se relit en liste vide", lire("veve_uuid,jour,ts_obs,source,floor,unite\n") == [], "?")

# ─────────────────────────────────────────────────────────────────────────────
print("§11 — UNE ENTREE MALFORMEE EST COMPTEE, PAS DEVINEE")
e = etat(vfloors={"court": [1.0], "sans_ts": [1.0, None], "bon": [1.0, T - 60]},
         sfloors={"vieux_format": [1.0, "nom", "rare", "img"]})
lignes, c = observations(e, T)
v("seule la bonne entre", [l[0] for l in lignes] == ["bon"], lignes)
v("les sans-date sont comptees cote veve", c["veve_sans_date"] == 2, c)
v("les sans-date sont comptees cote stackr", c["stackr_sans_date"] == 1, c)
v("terme qui DOIT valoir zero : malformes", c["malformes"] == 0, c)
# ⭐ et un floor non numerique, lui, DOIT faire bouger `malformes`
e = etat(vfloors={"x": ["pas un prix", T - 60]})
_, c = observations(e, T)
v("un floor non numerique incremente `malformes`", c["malformes"] == 1, c)

# ─────────────────────────────────────────────────────────────────────────────
print("§12 — UN ETAT VIDE NE PRODUIT PAS UN JOURNAL VIDE")
# ⭐⭐⭐ Le scenario qui coute cher : l'etat se degrade, le run reste vert, et
#   le journal du mois est ecrase par un fichier a un en-tete. Le garde-fou
#   doit tenir SANS rien savoir de la cause.
avant = [("a", HIER, int(T - 90000), "veve", 1.0, "USD")]
tot, r = fusionner(avant, [])
v("le journal existant est intact", len(tot) == 1, tot)
ok, motif = feu_vert(r, {"futurs_ecartes": 0, "malformes": 0})
v("on ecrit (fichier identique) et on le DIT", ok and motif, motif)

# ─────────────────────────────────────────────────────────────────────────────
print()
if echecs:
    print(f"⛔ BANC DU JOURNAL : {len(echecs)} controle(s) en echec — {echecs}")
    sys.exit(1)
print("✅ BANC DU JOURNAL : tous les controles passent.")
sys.exit(0)
