# jetonveve/tests/test_histlow_transition.py
"""📊 et 🔊 : de l'ETAT vers la TRANSITION.

Le defaut corrige ici n'etait pas un mauvais reglage mais un BLOCAGE :
detect_hist_low redecouvrait a chaque run tout le stock d'items assis dans
la bande basse (92 a 5 %, mesure sur les donnees de prod du 20/07), le
garde-fou anti-avalanche rendait [] ET effaçait les cooldowns qu'il venait
de poser — donc le run suivant retrouvait le meme lot. Aucun seuil entre
5 % et 30 % ne publiait quoi que ce soit, et rien ne le disait.

Le test `test_ancien_blocage_est_leve` est celui qui garde la porte : il
echouerait si on revenait au comportement « on jette le lot entier ».
"""
import time
import unittest

from scraper.price_baseline import detect_hist_low, detect_vol_anomaly


# ⚠️ PIEGE DE TEST paye une fois : `alerts.get(uid, 0)` vaut 0 pour un item
# jamais alerte, donc avec des ts petits (1000, 2000...) TOUT tombe dans le
# cooldown de l'epoque et rien ne se declenche. On travaille sur des
# horodatages reels.
T0 = 1_750_000_000.0
H = 3600.0


def bl(n=200, mini=10.0, p5=12.0, p25=20.0, p50=40.0, p75=60.0, p95=90.0,
       maxi=100.0, lp50=4.0, lp90=8.0):
    return {"n_points": n, "floor_min": mini, "floor_p5": p5, "floor_p25": p25,
            "floor_p50": p50, "floor_p75": p75, "floor_p95": p95,
            "floor_max": maxi, "listings_p50": lp50, "listings_p90": lp90}


def vendu(*uids):
    return {u: [5.0, time.time()] for u in uids}


class HistLowTransition(unittest.TestCase):

    def lancer(self, state, floors, ts, **kw):
        opts = dict(on=True, pct=10.0, cooldown_h=6.0, maxn=10, min_points=30)
        opts.update(kw)
        return detect_hist_low(state, floors, {u: bl() for u in floors},
                               {}, vendu(*floors), ts=ts, **opts)

    def test_amorcage_muet_mais_memorisant(self):
        """Le tout premier passage apprend l'etat du monde SANS publier.
        Sans lui, l'amorcage serait lui-meme l'avalanche."""
        st = {}
        self.assertEqual(self.lancer(st, {"a": 10.5}, T0), [])
        self.assertIn("a", st["histlow_dedans"])

    def test_pas_de_re_tir_tant_qu_il_reste_dedans(self):
        """Le coeur du correctif : rester dans la bande n'est pas un
        evenement. C'est ce qui transforme un stock en flux."""
        st = {}
        self.lancer(st, {"a": 10.5}, T0)                 # amorcage
        st2 = dict(st)
        for i in range(1, 6):                                 # 5 runs, 30 h
            r = self.lancer(st2, {"a": 10.5}, T0 + i * 6 * H)
            self.assertEqual(r, [], f"re-tir au run {i}")

    def test_entree_dans_la_bande_declenche(self):
        st = {}
        self.lancer(st, {"a": 80.0}, T0)                  # amorcage, hors bande
        r = self.lancer(st, {"a": 10.5}, T0 + 10 * H)              # il entre
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["uuid"], "a")

    def test_sortie_puis_retour_redeclenche(self):
        """Un item qui remonte puis rechute est un nouvel evenement."""
        st = {}
        self.lancer(st, {"a": 80.0}, T0)
        self.assertEqual(len(self.lancer(st, {"a": 10.5}, T0 + 10 * H)), 1)
        self.assertEqual(self.lancer(st, {"a": 80.0}, T0 + 20 * H), [])   # ressort
        self.assertNotIn("a", st["histlow_dedans"])
        r = self.lancer(st, {"a": 10.5}, T0 + 30 * H)          # revient
        self.assertEqual(len(r), 1)

    def test_registre_tenu_meme_signal_eteint(self):
        """Le jour ou Preda allume 📊, il ne doit pas recevoir le stock
        entier : le registre est tenu meme quand `on=False`."""
        st = {}
        self.lancer(st, {u: 10.5 for u in "abcdef"}, T0, on=False)
        self.assertEqual(len(st["histlow_dedans"]), 6)
        self.assertEqual(self.lancer(st, {u: 10.5 for u in "abcdef"},
                                     T0 + 10 * H, on=True), [])

    def test_ancien_blocage_est_leve(self):
        """⭐ LE TEST QUI GARDE LA PORTE.
        60 items entrent d'un coup dans la bande, maxn=10. L'ancien code
        rendait [] pour toujours. Le nouveau publie les 10 meilleurs et
        RE-PROPOSE les 50 autres au run suivant."""
        uids = [f"u{i:02d}" for i in range(60)]
        st = {}
        self.lancer(st, {u: 80.0 for u in uids}, T0)       # amorcage haut
        r1 = self.lancer(st, {u: 10.5 for u in uids}, T0 + 10 * H)  # tous entrent
        self.assertEqual(len(r1), 10, "le lot entier a ete jete")
        r2 = self.lancer(st, {u: 10.5 for u in uids}, T0 + 20 * H)  # le reste suit
        self.assertEqual(len(r2), 10)
        self.assertFalse({a["uuid"] for a in r1} & {a["uuid"] for a in r2},
                         "un item a ete publie deux fois")

    def test_le_lot_est_vidange_en_entier(self):
        """Rien n'est enterre : au bout de N runs tout le monde est passe."""
        uids = [f"u{i:02d}" for i in range(60)]
        st = {}
        self.lancer(st, {u: 80.0 for u in uids}, T0)
        vus = set()
        for i in range(1, 12):
            for a in self.lancer(st, {u: 10.5 for u in uids}, T0 + 10 * H + i * 60):
                vus.add(a["uuid"])
        self.assertEqual(len(vus), 60, f"{60 - len(vus)} items perdus")

    def test_tri_par_pertinence_avant_troncature(self):
        """Les 10 publies doivent etre les 10 plus bas, pas 10 au hasard."""
        floors = {f"u{i:02d}": 10.0 + i for i in range(30)}
        st = {}
        self.lancer(st, {u: 80.0 for u in floors}, T0)
        r = self.lancer(st, floors, T0 + 10 * H, pct=100.0)
        self.assertEqual([a["uuid"] for a in r],
                         [f"u{i:02d}" for i in range(10)])

    def test_preuve_de_vente_toujours_exigee(self):
        st = {}
        detect_hist_low(st, {"a": 80.0}, {"a": bl()}, {}, {}, ts=T0,
                        on=True, pct=10.0, maxn=10, min_points=30)
        r = detect_hist_low(st, {"a": 10.5}, {"a": bl()}, {}, {}, ts=T0 + 10 * H,
                            on=True, pct=10.0, maxn=10, min_points=30)
        self.assertEqual(r, [])

    def test_historique_court_ecarte(self):
        st = {}
        b = {"a": bl(n=5)}
        detect_hist_low(st, {"a": 80.0}, b, {}, vendu("a"), ts=T0,
                        on=True, pct=10.0, maxn=10, min_points=30)
        r = detect_hist_low(st, {"a": 10.5}, b, {}, vendu("a"), ts=T0 + 10 * H,
                            on=True, pct=10.0, maxn=10, min_points=30)
        self.assertEqual(r, [])

    def test_purge_des_absents(self):
        """Le registre ne peut pas grossir indefiniment."""
        st = {}
        self.lancer(st, {"a": 10.5}, T0)
        self.lancer(st, {"b": 10.5}, T0 + 8 * 24 * H)
        self.assertNotIn("a", st["histlow_dedans"])


class VolTransition(unittest.TestCase):

    def lancer(self, state, listings, ts, **kw):
        opts = dict(on=True, ratio=3.0, cooldown_h=6.0, maxn=10,
                    min_points=30, min_listings=5)
        opts.update(kw)
        return detect_vol_anomaly(state, listings,
                                  {u: bl() for u in listings}, {}, ts=ts,
                                  **opts)

    def test_amorcage_muet(self):
        st = {}
        self.assertEqual(self.lancer(st, {"a": 40.0}, T0), [])
        self.assertIn("a", st["vol_dedans"])

    def test_pas_de_re_tir_tant_que_l_anomalie_dure(self):
        """🔊 souffrait du meme mal que 📊 : une flambee d'offres qui dure
        3 jours ne doit pas alerter 12 fois."""
        st = {}
        self.lancer(st, {"a": 40.0}, T0)
        for i in range(1, 6):
            self.assertEqual(self.lancer(st, {"a": 40.0}, T0 + i * 6 * H),
                             [])

    def test_montee_declenche(self):
        st = {}
        self.lancer(st, {"a": 4.0}, T0)
        r = self.lancer(st, {"a": 40.0}, T0 + 10 * H)
        self.assertEqual(len(r), 1)

    def test_debordement_rend_au_lieu_de_jeter(self):
        uids = [f"v{i:02d}" for i in range(25)]
        st = {}
        self.lancer(st, {u: 4.0 for u in uids}, T0)
        r1 = self.lancer(st, {u: 40.0 for u in uids}, T0 + 10 * H)
        self.assertEqual(len(r1), 10)
        self.assertEqual(len(self.lancer(st, {u: 40.0 for u in uids}, T0 + 20 * H)),
                         10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
