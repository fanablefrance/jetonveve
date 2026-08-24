# ⚠️ DEPOT : fanablefrance/jetonveve   ·   CHEMIN : tests/test_fiches_stackr.py
# Le projet vit sur 6 depots et DEUX comptes GitHub. Un fichier depose au
# mauvais endroit ne provoque aucune erreur : il dort.
"""📊 LOT 188 — LES FICHES STACKR : ce qui est ecrit, et ce qui ne l'est jamais.

Preda, 24/08/2026 : « sur stackr il y a aussi le floor veve et le floor stackr
en Omi et en $, il faudra collecter l'infos ».

⭐⭐⭐ CE BANC NE TOUCHE PAS LE RESEAU, ET C'EST DELIBERE. Un banc qui appelle
StackR mesure StackR : il rougirait un jour de panne chez eux, vert un jour ou
notre code serait casse mais leur API genereuse. On mesure donc la TRANSFORMATION
(des reponses fabriquees -> des lignes) et les GARDES du workflow.
⚠️ Ce que ce banc ne peut PAS dire : que l'API existe encore, que les noms de
champs n'ont pas change. Cela s'est mesure a la main le 24/08, sur 20 items
reels, et devra se remesurer le jour ou les colonnes se videront.
"""

import csv
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper import stackr_fiches as sf          # noqa: E402

KO = 0


def dit(ok, titre, detail=""):
    global KO
    print(f"  {'✅' if ok else '❌'} {titre}{'   — ' + detail if detail else ''}")
    if not ok:
        KO += 1


# ═══════════════════════════════════════════════════════════════════════════
print("\n1. les deux types du meme champ — le piege mesure le 24/08")
# ═══════════════════════════════════════════════════════════════════════════
# 🔴 `floor_market_price` est un NOMBRE sur un collectible (700) et une CHAINE
#    sur un comic ('8.98000000000000000000'). Le meme champ, deux types, dans
#    la meme API. Un `float()` nu casse sur l'un ; un `or 0` transformerait
#    « inconnu » en « gratuit ».
dit(sf._f(700) == 700.0, "un nombre est lu")
dit(sf._f("8.98000000000000000000") == 8.98, "une chaine decimale est lue",
    str(sf._f("8.98000000000000000000")))
# ⛔ LE CONTROLE QUI COMPTE : l'absence reste l'absence.
dit(sf._f(None) is None, "un champ absent rend None, JAMAIS 0")
dit(sf._f("") is None and sf._f("nan-ish") is None,
    "une valeur illisible rend None, JAMAIS 0",
    "un zero invente est indistinguable d'un zero mesure")
dit(sf._i("51") == 51 and sf._i(None) is None, "les entiers suivent la meme regle")

# ═══════════════════════════════════════════════════════════════════════════
print("\n2. une fiche -> une ligne : les deux marches, chacun dans son unite")
# ═══════════════════════════════════════════════════════════════════════════
# ⚠️ LA REPONSE EST RECOPIEE DE LA MESURE DU 24/08, champ pour champ (fiche
#    « Master Splinter - Black & White Variant », celle de la capture de Preda).
FICHE = {
    "id": "ed1bdbf0-17f4-470e-b85a-494194f71b94",
    "name": "Master Splinter - Black & White Variant",
    "element_type": "collectible",
    "editions_burnt": 0, "in_circulation": 51, "issued": 87,
    "floor_market_price": 700, "stackr_floor_price": 2000000,
    "market_fee": 0.06, "rarity": "COMMON",
}
L = sf.ligne_de(FICHE["id"], "collectible", FICHE, 9, 1787593267.0)
dit(L["floor_veve_usd"] == 700.0, "le plancher VeVe est en DOLLARS", str(L["floor_veve_usd"]))
dit(L["floor_stackr_omi"] == 2000000.0, "le plancher StackR est en OMI", str(L["floor_stackr_omi"]))
# ⭐⭐⭐ LE CONTROLE LE PLUS IMPORTANT DU BANC, ET IL DIT « NON ».
#   L'ecran de StackR montre QUATRE chiffres : chaque plancher en OMI *et* en $.
#   L'API n'en rend que DEUX. Les deux autres sont calcules par leur site avec
#   le cours OMI/USD (700 / 0,000208 = 3 365 384 — le compte tombe juste).
#   ⛔ Les collecter figerait un cours dans un fichier qui vieillirait sans le
#     dire. La conversion appartient au site, qui lit `omi_usd.csv` (lot 181).
#   *Deux chiffres a l'ecran ne sont pas deux mesures : c'est parfois la meme
#   mesure vue dans deux unites.*
dit(not any(c.endswith("_usd") for c in sf.COLONNES if "stackr" in c),
    "AUCUNE colonne ne pretend donner le plancher StackR en dollars",
    "ce chiffre est un CALCUL de leur site, pas une mesure")
dit(not any("omi" in c for c in sf.COLONNES if "veve" in c),
    "AUCUNE colonne ne pretend donner le plancher VeVe en OMI")
dit(L["offres_en_cours"] == 9, "le nombre d'offres en cours est ecrit", "9 — comme la capture")
dit(L["editions_burnt"] == 0 and L["in_circulation"] == 51 and L["issued"] == 87,
    "brulees, circulation et emis sont ecrits tels quels")

print("\n2 bis. ce qui manque reste VIDE — jamais comble")
MAIGRE = {"id": FICHE["id"], "element_type": "collectible"}
V = sf.ligne_de(FICHE["id"], "collectible", MAIGRE, None, 1787593267.0)
dit(V["floor_veve_usd"] == "" and V["floor_stackr_omi"] == "",
    "une fiche sans plancher rend des colonnes VIDES, pas des zeros")
dit(V["offres_en_cours"] == "", "« offres inconnues » n'est pas « zero offre »",
    "un item sans offre vaut 0 ; un item non interroge vaut vide — deux etats differents")
# ⭐ LA CONTRE-EPREUVE : sans elle, une fonction qui rendrait TOUJOURS vide
#   passerait les deux lignes ci-dessus. Un banc muet ressemble a un succes.
dit(L["floor_veve_usd"] != "", "...et le cas normal remplit toujours (la fonction n'est pas morte)")
print("\n2 ter. un item SANS aucune offre vaut zero, et zero s'ecrit")
Z = sf.ligne_de(FICHE["id"], "collectible", FICHE, 0, 1787593267.0)
dit(Z["offres_en_cours"] == 0,
    "0 offre s'ecrit « 0 » et ne retombe pas dans le vide",
    "c'est l'item que plus personne ne vend — le plus interessant de tous")

# ═══════════════════════════════════════════════════════════════════════════
print("\n3. la forme des identifiants : liste blanche, jamais liste noire")
# ═══════════════════════════════════════════════════════════════════════════
# ⚠️ Ces identifiants viennent d'un XML DISTANT et servent a composer une URL.
dit(bool(sf.RE_UUID.match("ed1bdbf0-17f4-470e-b85a-494194f71b94")), "un uuid passe")
for mauvais in ("../../evil", "sample-0000-582307", "", "ED1BDBF0-17F4-470E-B85A-494194F71B94"):
    dit(not sf.RE_UUID.match(mauvais), f"« {mauvais[:24] or '(vide)'} » est refuse")

# ═══════════════════════════════════════════════════════════════════════════
print("\n4. la rotation : elle CUMULE, et elle ne saute jamais une tranche")
# ═══════════════════════════════════════════════════════════════════════════
# 🔴🔴 SANS RELECTURE, CHAQUE RUN ECRASERAIT LE FICHIER AVEC SA SEULE TRANCHE :
#    on servirait 2 000 items au lieu de 19 774, et le fichier aurait
#    exactement l'air d'un fichier complet. Le piege est SILENCIEUX.
with tempfile.TemporaryDirectory() as d:
    chemin = os.path.join(d, "f.csv")
    sf.OUT_PATH = chemin
    a = {"aaaaaaaa-1111-2222-3333-444444444444": dict(
        zip(sf.COLONNES, ["aaaaaaaa-1111-2222-3333-444444444444", "collectible",
                          "1787593267", 1.0, 2.0, 3, 0, 4, 5, 0.06, ""]))}
    sf.ecrire_sortie(a)
    relu = sf.charger_sortie()
    dit(len(relu) == 1 and "aaaaaaaa-1111-2222-3333-444444444444" in relu,
        "ce qui est ecrit se relit", f"{len(relu)} ligne(s)")
    b = dict(relu)
    b["bbbbbbbb-1111-2222-3333-444444444444"] = dict(
        zip(sf.COLONNES, ["bbbbbbbb-1111-2222-3333-444444444444", "comic",
                          "1787593268", 9.0, "", 1, 0, 2, 0, 0.085, ""]))
    sf.ecrire_sortie(b)
    dit(len(sf.charger_sortie()) == 2,
        "un second run AJOUTE au lieu d'ecraser", "2 lignes apres 1 + 1")
    # ⛔ UNE LIGNE A IDENTIFIANT INVALIDE NE RENTRE PAS PAR LA RELECTURE. Le
    #   fichier vient de la Release : c'est une entree comme une autre.
    with open(chemin, "a", encoding="utf-8") as f:
        f.write("../../evil,collectible,1,1,1,1,1,1,1,1,\n")
    dit(len(sf.charger_sortie()) == 2,
        "une ligne a identifiant invalide est ecartee a la relecture")

# ═══════════════════════════════════════════════════════════════════════════
print("\n4 bis. les DETENTEURS : meme cumul, et un menage par piece")
# ═══════════════════════════════════════════════════════════════════════════
# 🔴🔴🔴 CE § EXISTE PARCE QUE LE CHEMIN DORMAIT, ET QU'UN CHEMIN QUI DORT
#    N'EST PAS UN CHEMIN SUR — C'EST UN CHEMIN NON MESURE.
# Les detenteurs etaient ETEINTS par defaut. Preda a repondu « oui » le 24/08,
# et en allumant l'interrupteur j'ai trouve que le fichier etait ECRASE a
# chaque tranche : apres dix jours de rotation il aurait contenu 2 000 pieces
# sur 19 774, en ayant exactement l'air d'un fichier complet.
# ⭐⭐⭐ Le CSV principal, lui, avait sa relecture cumulative ET une faute
#   injectee qui la tenait (Q3). Celui-ci n'avait ni l'une ni l'autre, parce
#   qu'il ne s'executait jamais. *Une fonctionnalite qu'on allume rend
#   mesurable un defaut qui existait deja.*
with tempfile.TemporaryDirectory() as d:
    sf.HOLDERS_PATH = os.path.join(d, "h.csv")
    A = "aaaaaaaa-1111-2222-3333-444444444444"
    B = "bbbbbbbb-1111-2222-3333-444444444444"
    W1, W2, W3 = "0x" + "1" * 40, "0x" + "2" * 40, "0x" + "3" * 40

    sf.ecrire_holders([[A, W1, "alice", 6, "1"], [A, W2, "bob", 3, "1"]])
    dit(len(sf.charger_holders()) == 2, "une tranche s'ecrit", "2 couples")

    # ⭐ LE CUMUL : une AUTRE piece s'ajoute, elle n'ecrase pas la premiere.
    sf.ecrire_holders([[B, W3, "carol", 9, "2"]])
    apres = sf.charger_holders()
    dit(len(apres) == 3 and (A, W1) in apres and (B, W3) in apres,
        "la tranche suivante AJOUTE au lieu d'ecraser",
        f"{len(apres)} couples — sans ca, dix jours de rotation ne serviraient qu'un jour")

    # ⭐⭐ LE MENAGE : une piece REVUE remplace ses lignes. Sans ca, un
    #   collectionneur qui a tout vendu resterait detenteur pour toujours, et
    #   le classement des plus gros porteurs serait faux.
    sf.ecrire_holders([[A, W1, "alice", 6, "3"]])
    apres = sf.charger_holders()
    dit((A, W2) not in apres, "une piece revue REMPLACE ses anciennes lignes",
        "un vendeur parti doit disparaitre du fichier")
    # ⛔ MAIS SEULEMENT CELLE-LA. « pas mesure » n'est pas « plus de
    #   detenteurs » : purger les pieces absentes de la tranche viderait le
    #   fichier a chaque run, ce qui est le defaut d'origine sous un autre nom.
    dit((B, W3) in apres, "...et SEULEMENT celle-la : les pieces non revues restent",
        "« pas mesure » n'est pas « plus de detenteurs »")

    # 🔴🔴 LA CLE EST LE COUPLE, ET CE CONTROLE EST NE D'UNE INJECTION QUI
    #    N'AVAIT PAS MORDU. Mon premier § utilisait trois portefeuilles TOUS
    #    DIFFERENTS : j'ai remplace la cle par le portefeuille seul, et le banc
    #    est reste vert — la collision ne pouvait pas se produire dans mon jeu
    #    d'essai. Or elle est la NORME dans la vraie vie : un collectionneur
    #    detient plusieurs pieces, et c'est meme ce qui fait de lui un whale.
    #    ⭐⭐⭐ *Un jeu d'essai sans collision ne peut pas mesurer une regle de
    #    deduplication.* Ici le MEME portefeuille detient DEUX pieces.
    # ⚠️ LES DEUX LIGNES DANS LE **MEME APPEL**, ET C'EST TOUT LE POINT.
    #    Deuxieme lecon de la meme injection : je les avais d'abord passees en
    #    DEUX appels, et le banc restait vert. Normal — entre deux appels, les
    #    cles sont reconstruites depuis les COLONNES du fichier, donc une cle
    #    bancale se repare toute seule a la relecture. Le seul moment ou elle
    #    peut perdre une ligne, c'est A L'INTERIEUR d'une passe.
    #    ⭐⭐⭐ *Une regle de deduplication ne se mesure que la ou elle
    #    s'applique.* Et le cas est la NORME : une tranche de 2 000 pieces
    #    contient forcement le meme collectionneur sur plusieurs d'entre elles.
    sf.ecrire_holders([[A, W1, "alice", 6, "4"], [B, W1, "alice", 4, "4"]])
    apres = sf.charger_holders()
    dit((A, W1) in apres and (B, W1) in apres,
        "un meme portefeuille sur DEUX pieces, dans la meme passe : les deux restent",
        f"{len(apres)} couples — une cle par portefeuille seul en perdrait une")

    # ⛔ MEME LISTE BLANCHE QU'AILLEURS : le fichier vient de la Release.
    avant_sales = len(sf.charger_holders())
    with open(sf.HOLDERS_PATH, "a", encoding="utf-8") as f:
        f.write("../../evil,0xdead,x,1,1\n")
        f.write(f"{A},pas-un-wallet,x,1,1\n")
    dit(len(sf.charger_holders()) == avant_sales,
        "une ligne a identifiant ou portefeuille invalide est ecartee",
        f"{len(sf.charger_holders())} couples retenus sur {avant_sales} valides + 2 salies")

# ⭐ ET L'INTERRUPTEUR EST BIEN SUR « ALLUME » (reponse de Preda du 24/08).
dit(sf.AVEC_HOLDERS is True,
    "les detenteurs sont collectes par defaut",
    "`FICHES_HOLDERS=false` les eteint sans rien casser")

# ═══════════════════════════════════════════════════════════════════════════
print("\n5. le workflow : ses gardes, relues dans le fichier lui-meme")
# ═══════════════════════════════════════════════════════════════════════════
# ⭐ ON RELIT LE WORKFLOW, on ne recopie pas sa logique ici : une copie de la
#   regle dans le banc mesurerait le banc.
YML = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   ".github", "workflows", "fiches-stackr.yml")
try:
    y = open(YML, encoding="utf-8").read()
except OSError:
    y = ""
if not y:
    print("  ⏸️  sans objet — `fiches-stackr.yml` illisible depuis ce chemin.")
else:
    dit('[ -s data/fiches_stackr.csv ]' in y and 'wc -l < data/fiches_stackr.csv' in y,
        "un CSV vide n'ECRASE PAS celui de la release",
        "le fichier est cumulatif sur ~10 jours : l'ecraser efface un tour complet")
    # ⭐⭐ LA GARDE LA PLUS SUBTILE DU LOT : publier un curseur avance sans le
    #   CSV correspondant ferait SAUTER une tranche au prochain run,
    #   definitivement, et le trou ne se verrait jamais.
    dit('[ -s data/fiches_stackr_state.json ] && [ -s data/fiches_stackr.csv ]' in y,
        "l'etat de rotation ne se publie QUE si le CSV l'a ete",
        "sinon le curseur avance sur une tranche jamais ecrite — un trou definitif")
    dit('cancel-in-progress: false' in y,
        "deux runs ne se marchent pas dessus",
        "deux runs concurrents ecriraient chacun leur tranche par-dessus l'autre")

print(f"\n{'✅ fiches StackR : conforme' if KO == 0 else f'❌ fiches StackR : {KO} ecart(s)'}")
sys.exit(0 if KO == 0 else 1)
