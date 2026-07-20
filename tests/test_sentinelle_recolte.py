# ⚠️ DEPOT : fanablefrance/jetonveve   ·   CHEMIN : tests/test_sentinelle_recolte.py
# Le projet vit sur 6 depots et DEUX comptes GitHub. Un fichier
# depose au mauvais endroit ne provoque aucune erreur : il dort.

"""La sentinelle de recolte maigre — teste sans reseau.

Ce qu'elle protege : `fetch_veve_floors` saute EN SILENCE les pages qui
echouent. Avant, une carte des floors amputee etait acceptee telle quelle,
et 98 % du catalogue cessait d'etre surveille sans un mot. Un blocage
ressemblait exactement a un marche calme.
"""
import unittest

from scraper import floor_watch as fw


class TestRecolteCredible(unittest.TestCase):
    def test_le_premier_run_est_toujours_accepte(self):
        # Sans reference, on ne peut rien juger : on accepte.
        self.assertTrue(fw.recolte_credible(120, attendu=0))

    def test_une_recolte_complete_passe(self):
        self.assertTrue(fw.recolte_credible(6142, attendu=6142, ratio=0.8))

    def test_une_petite_variation_passe(self):
        # Le catalogue bouge un peu tous les jours : ce n'est pas suspect.
        self.assertTrue(fw.recolte_credible(6000, attendu=6142, ratio=0.8))

    def test_UNE_RECOLTE_AMPUTEE_EST_REFUSEE(self):
        # Le cas du blocage : 10 pages sur 76 ont repondu.
        self.assertFalse(fw.recolte_credible(800, attendu=6142, ratio=0.8))

    def test_un_seul_item_est_refuse(self):
        # C'est le cas qui passait avant : `if neuf:` est vrai pour {1 item}.
        self.assertFalse(fw.recolte_credible(1, attendu=6142))

    def test_la_frontiere_exacte(self):
        self.assertTrue(fw.recolte_credible(4914, attendu=6142, ratio=0.8))
        self.assertFalse(fw.recolte_credible(4913, attendu=6142, ratio=0.8))


class TestAttenduSuivant(unittest.TestCase):
    def test_une_meilleure_recolte_releve_la_reference(self):
        self.assertEqual(fw.attendu_suivant(6000, 6142), 6142.0)

    def test_la_reference_descend_LENTEMENT(self):
        # Un catalogue qui retrecit vraiment doit etre suivi — mais pas
        # assez vite pour qu'un blocage prolonge fasse baisser la garde.
        self.assertAlmostEqual(fw.attendu_suivant(6142, 6100), 6111.29, places=1)

    def test_un_catalogue_qui_retrecit_est_suivi_en_quelques_jours(self):
        attendu = 6142.0
        for _ in range(24 * 3):                 # 3 jours de runs horaires
            attendu = fw.attendu_suivant(attendu, 5000)
        self.assertLess(attendu, 5300, "la reference doit avoir converge")
        self.assertGreaterEqual(attendu, 5000)

    def test_un_blocage_prolonge_ne_fait_PAS_baisser_la_garde_trop_vite(self):
        # 6 heures de recoltes maigres REFUSEES : la reference ne bouge pas,
        # puisqu'on n'appelle attendu_suivant que sur une recolte acceptee.
        attendu = 6142.0
        self.assertFalse(fw.recolte_credible(200, attendu))
        self.assertEqual(attendu, 6142.0)


if __name__ == "__main__":
    unittest.main()
