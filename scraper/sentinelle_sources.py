# ⚠️ DEPOT : fanablefrance/jetonveve  ET  VeVePreda/scrapeur-veve
# ⚠️ CHEMIN : scraper/sentinelle_sources.py  (le meme dans les deux)
#
# ⛔⛔ CE FICHIER VIT EN DOUBLE, A L'IDENTIQUE, DANS DEUX DEPOTS ET DEUX COMPTES
# GitHub. Ce n'est pas un accident : les deux depots collectent, aucun ne peut
# importer l'autre, et l'audit du 29/07 a nomme « le module en double qui a
# DIVERGE » comme le vrai risque du projet. D'ou la regle, portee par le
# fichier lui-meme parce qu'un A_LIRE se perd et qu'un commentaire voyage :
#     toute modification se porte DES DEUX COTES, et l'empreinte se met a jour
#     dans les DEUX `tests/test_sentinelle_partagee.py`.
# Le test tombe des que le fichier bouge, et affiche l'empreinte a coller.
"""🩺 sentinelle_sources — compter ce que les sources nous repondent.

POURQUOI CE MODULE EXISTE
-------------------------
L'audit du risque de bannissement (22/07) designait le meme manque a chaque
page : « detection + coupe-circuit par source » etait le garde-fou n°2, et il
n'existait pas. Consequence, notee noir sur blanc : *tant que ce n'est pas
mesure, les risques sont un raisonnement, pas un releve.*

Ce module est ce releve. Il ne fait qu'une chose : **compter les reponses par
source**, et le dire. C'est a la fois l'audit empirique qui manquait ET la
premiere moitie du coupe-circuit — les deux etaient le meme instrument.

⭐⭐ CE QU'IL NE FAIT PAS, VOLONTAIREMENT : il n'arrete jamais un run. Une
sentinelle qui coupe la collecte sur un faux positif fait exactement le degat
qu'elle pretend eviter — et on a deja paye trois fausses alertes dues a nos
propres instruments. Elle CONSEILLE une pause (`pause_conseillee`) et elle
CRIE (`doit_crier`). La decision d'arreter reste humaine.

⭐ ENTIEREMENT PURE ET HORS RESEAU : aucun import de `requests`, aucun etat sur
disque. Tout est testable sans toucher a une API — ce qui est la condition pour
qu'un garde-fou soit lui-meme sur.

LE VOCABULAIRE DES VERDICTS
---------------------------
  · `ouverte`   : la source repond normalement.
  · `lente`     : elle repond, mais avec des 5xx / timeouts — c'est SA sante,
                  pas notre faute. On patiente, on ne se cache pas.
  · `se_ferme`  : 429 / 403 au-dessus du seuil. C'est NOUS qu'elle repousse.
                  ⚠️ Ne pas confondre les deux : la reponse n'est pas la meme.
                  Ralentir face a un 5xx ne sert a rien ; face a un 429, si.

⭐⭐⭐ ET UN QUATRIEME CAS, QUI N'EST PAS UN VERDICT (06/08/2026) : LA REPONSE
QUI ARRIVE, EN HTTP 200, ET QUI NE PORTE AUCUNE DONNEE. GraphQL ne se sert
PAS du code HTTP pour dire « je n'ai pas cet objet » : il rend 200 et met
`errors: [{message: "Entity not found"}]` dans le CORPS. Mesure du 06/08 sur
l'API VeVe :
    id inconnu  -> HTTP 200 + errors[] « Entity not found »
    champ faux  -> HTTP 400 « Invalid request. »
Le second etait deja compte (`invalide`, lot 76). Le premier tombait dans
`ok`, et c'est ainsi qu'un run pouvait perdre des items en imprimant
    🟢 veve_graphql 7130 requete(s) — RAS
⛔ CE N'EST NI UN REFUS NI UNE REQUETE FAUSSE, et l'y ranger serait refaire
l'erreur que ce module existe pour eviter : ralentir n'y change rien, et
corriger la requete non plus — l'objet n'est plus la, ou notre identifiant a
derive. C'est un compteur A LUI, avec son propre conseil.
"""

from __future__ import annotations

import os
import threading
from typing import Dict, List, Optional, Tuple

# Seuils. Un pourcentage seul ne veut rien dire sur 3 requetes : il faut un
# MINIMUM d'observations avant de prononcer un verdict, sinon la sentinelle
# devient elle-meme une source de fausses alertes.
MIN_OBS = int(os.environ.get("SENTINELLE_MIN_OBS", "30"))
# ⭐⭐ A4 BIS (30/07/2026) — MIN_OBS COMPTE LES REQUETES DE CE RUN, PAS D'UNE
# HISTOIRE. `self.obs` vit en memoire et rien ne le persiste : une source
# appelee moins de MIN_OBS fois dans un processus n'atteint JAMAIS son verdict.
# Ce n'est pas un rodage de quelques jours, c'est un etat permanent.
# Mesure du 30/07 sur `daily` #122 : `stackr` 6 requetes/run, `tracker` 13 dans
# `scraper.run` -> deux sources durablement injugeables, affichees 🟢.
#
# ⛔ ON NE BAISSE PAS MIN_OBS : le raisonnement d'origine est juste, un
#    POURCENTAGE sur 6 requetes ne veut rien dire.
# ✅ ON SEPARE « je ne sais pas » de « tout va bien », et on ajoute la seule
#    regle qui n'a pas besoin d'echantillon : un refus TOTAL est un FAIT.
#    6 requetes sur 6 refusees, ce n'est pas un echantillon insuffisant, c'est
#    une source fermee.
#
# ⚠️ POURQUOI 5 ET PAS 3. Un banc existant (`test_sous_le_minimum_d_observations`)
# fige une lecon PAYEE : « 3 requetes refusees ne valent pas un verdict, on a
# deja paye trois fausses alertes en un jour ». Un seuil a 3 aurait contredit
# cette lecon. 5 laisse passer les hoquets courts ET attrape `stackr`, qui fait
# 6 requetes par run. ⭐ Le seuil s'accompagne d'une condition BEAUCOUP plus
# stricte qu'un pourcentage : le refus doit etre TOTAL. Un seul succes suffit a
# retomber en angle mort.
REFUS_ABSOLU = int(os.environ.get("SENTINELLE_REFUS_ABSOLU", "5"))
SEUIL_FERME_PCT = float(os.environ.get("SENTINELLE_FERME_PCT", "5"))
# ⭐ Le seuil de l'ABSENCE (06/08/2026). Il est volontairement plus haut que
# celui du refus : quelques items retires du store, c'est la vie normale d'un
# catalogue. C'est la PROPORTION qui parle — 10 % des reponses sans objet, ce
# n'est plus du churn, c'est un identifiant qui a derive ou un pan de
# catalogue qui a disparu. Mesure de reference du 06/08 : 0 sur 300.
SEUIL_ABSENT_PCT = float(os.environ.get("SENTINELLE_ABSENT_PCT", "10"))
SEUIL_LENTE_PCT = float(os.environ.get("SENTINELLE_LENTE_PCT", "20"))
# Pause additionnelle conseillee quand une source nous repousse (secondes).
PAUSE_MAX_S = float(os.environ.get("SENTINELLE_PAUSE_MAX_S", "30"))

_REPOUSSE = (401, 403, 429)


class Sentinelle:
    """Un compteur par source. Instancier une fois par run."""

    def __init__(self) -> None:
        self.obs: Dict[str, Dict[str, int]] = {}
        # ⚠️ `veve_detail` enrichit EN PARALLELE (session par thread) : deux
        # threads peuvent noter la meme source au meme instant. Un verrou
        # coute une nanoseconde et evite un compteur faux — et un compteur
        # faux dans un garde-fou, c'est une fausse alerte, donc a terme un
        # garde-fou qu'on desarme.
        self._verrou = threading.Lock()

    # ---------------------------------------------------------------- mesure
    def noter(self, source: str, code: Optional[int] = None,
              erreur: Optional[str] = None, absent: bool = False) -> None:
        """Une reponse observee. `code` = statut HTTP, ou None si la requete
        n'a meme pas abouti (timeout, DNS, connexion) — auquel cas `erreur`
        porte le texte. Ne leve jamais.

        `absent=True` : la reponse est ARRIVEE et son code dit « tout va
        bien », mais son corps dit qu'il n'y a pas d'objet (GraphQL 200 +
        `errors[]`). ⛔ C'est a l'APPELANT de le dire, pas a ce module : lui
        seul connait la forme du corps, et ce fichier n'a le droit d'importer
        ni `requests` ni un format de reponse — c'est ce qui le garde
        testable hors reseau, donc fiable."""
        with self._verrou:
            d = self.obs.setdefault(source, {"total": 0, "ok": 0, "repousse": 0,
                                             "serveur": 0, "reseau": 0,
                                             "invalide": 0, "absent": 0})
            d.setdefault("invalide", 0)     # etats plus anciens en memoire
            d.setdefault("absent", 0)
            d["total"] += 1
            if code is None:
                d["reseau"] += 1
            elif code in _REPOUSSE:
                d["repousse"] += 1
            elif code >= 500:
                d["serveur"] += 1
            elif code < 400:
                # ⭐ Un 200 qui ne porte pas d'objet n'est PAS un succes. On ne
                # le compte pas non plus comme une faute de la source : elle a
                # repondu, et correctement. Il a son propre seau.
                if absent:
                    d["absent"] += 1
                else:
                    d["ok"] += 1
            else:
                # 🔴🔴 LES 4xx QUI NE SONT PAS UN REFUS DE NOUS (05/08/2026)
                #
                # CE QU'IL Y AVAIT ICI : `d["serveur"] += 0` et `d["ok"] += 0`,
                # avec le commentaire « ni bon ni mauvais signe : on ne compte
                # que dans le total ». Deux additions de zero — donc RIEN.
                # Le raisonnement etait bon (un 400 n'est pas la source qui se
                # ferme) ; sa consequence ne l'etait pas : ces reponses
                # n'apparaissaient NULLE PART, et `resume()` affichait « RAS ».
                #
                # Mesure du 05/08 : le run `ENRICH_MODE=all` a essuye des
                # HTTP 400 sur `publicComicType`, et la sentinelle a imprime
                #     🟢 veve_graphql  7130 requete(s) — RAS
                # ⭐⭐⭐ **UN COMPTEUR QUI N'INCREMENTE RIEN N'EST PAS NEUTRE :
                # IL EST MUET.** Et un garde-fou muet se lit comme un
                # garde-fou rassurant.
                #
                # ⛔ ON NE LES RANGE PAS AVEC LES REFUS, ET C'EST LE POINT.
                # Un 429 dit « la source nous REPOUSSE » -> ralentir aide.
                # Un 400 dit « NOTRE REQUETE est fausse » -> ralentir n'aide
                # pas, il faut corriger le code. Ce sont deux problemes
                # opposes ; les additionner ferait ralentir un run qui n'a
                # aucun probleme de debit, et masquerait le vrai defaut.
                # C'est la meme distinction que le module fait deja entre
                # « se_ferme » (429) et « lente » (5xx).
                d["invalide"] += 1

    # --------------------------------------------------------------- verdict
    def verdict(self, source: str) -> str:
        """« ouverte » | « lente » | « se_ferme » | « angle_mort ».

        ⭐⭐ `angle_mort` est ne d'A4 bis : avant, un echantillon trop petit
        rendait « ouverte », c'est-a-dire que **l'ignorance se lisait comme une
        bonne nouvelle**. C'est exactement le defaut que ce module dit combattre
        — un canal muet pris pour un marche calme — reproduit a l'interieur du
        garde-fou lui-meme.
        """
        d = self.obs.get(source)
        if not d or not d["total"]:
            return "angle_mort"
        # ⭐ Un refus TOTAL ne demande pas d'echantillon : c'est un fait, pas un
        # taux. Sans cette ligne, `stackr` (6 requetes/run) pouvait etre refuse
        # 6 fois sur 6 et rester 🟢, pour toujours.
        if d["repousse"] >= REFUS_ABSOLU and d["repousse"] == d["total"]:
            return "se_ferme"
        if d["total"] < MIN_OBS:
            return "angle_mort"
        if 100.0 * d["repousse"] / d["total"] >= SEUIL_FERME_PCT:
            return "se_ferme"
        if 100.0 * (d["serveur"] + d["reseau"]) / d["total"] >= SEUIL_LENTE_PCT:
            return "lente"
        return "ouverte"

    def pause_conseillee(self, source: str) -> float:
        """Combien de secondes attendre EN PLUS avant de retaper cette source.
        Proportionnel au taux de refus, plafonne. 0 si la source va bien.

        ⭐ On ralentit UNIQUEMENT face a un refus (429/403). Face a des 5xx, la
        source est en peine : la marteler ne l'aide pas, mais ralentir ne
        change rien non plus a notre exposition — et allonger un run pour rien
        a un cout reel (le suivant chevauche)."""
        d = self.obs.get(source)
        if not d or not d["repousse"]:
            return 0.0
        # ⭐ A4 bis : sous MIN_OBS on ne calculait AUCUNE pause — donc une source
        # qui nous refusait tout en 6 requetes etait martelee au meme rythme.
        # Le refus total vaut desormais ralentissement, sans passer par un taux.
        if d["total"] < MIN_OBS and self.verdict(source) != "se_ferme":
            return 0.0
        taux = d["repousse"] / d["total"]
        return round(min(PAUSE_MAX_S, PAUSE_MAX_S * taux * 4), 1)

    def absence_criante(self, source: str) -> bool:
        """La source repond bien, et ne rend AUCUN objet — assez souvent pour
        que ce ne soit plus du churn de catalogue.

        ⭐ DEUX REGLES, ET LA PREMIERE NE DEMANDE PAS D'ECHANTILLON. C'est le
        meme raisonnement que `REFUS_ABSOLU` : une absence TOTALE est un FAIT,
        pas un taux. 6 requetes sur 6 sans objet, ce n'est pas un echantillon
        insuffisant, c'est qu'on demande les mauvais identifiants."""
        d = self.obs.get(source)
        if not d or not d.get("absent"):
            return False
        if d["absent"] >= REFUS_ABSOLU and d["absent"] == d["total"]:
            return True
        if d["total"] < MIN_OBS:
            return False
        return 100.0 * d["absent"] / d["total"] >= SEUIL_ABSENT_PCT

    def doit_crier(self) -> Tuple[bool, str]:
        """(faut-il prevenir un humain, quoi lui dire).

        ⭐ UNE SOURCE MUETTE N'EST PAS UN MARCHE CALME. C'est la lecon qui a
        coute trois semaines de silence : sans ce message, un blocage se lit
        comme « rien a signaler ».

        ⭐⭐ DEUX CAUSES, DEUX PARAGRAPHES, JAMAIS FONDUS (06/08/2026). « on
        nous repousse » et « la source n'a pas l'objet » demandent des gestes
        opposes. Un seul message qui melange les deux ferait chercher un
        blocage la ou il y a un item retire — et le prochain vrai blocage
        serait lu comme du bruit."""
        lignes: List[str] = []
        fermees = [s for s in self.obs if self.verdict(s) == "se_ferme"]
        if fermees:
            lignes.append("🚨 **Une source nous repousse** — ce n'est pas un marche calme.")
            for s in sorted(fermees):
                d = self.obs[s]
                lignes.append(
                    f"· **{s}** : {d['repousse']} refus (429/403) sur {d['total']} "
                    f"requetes ({100.0 * d['repousse'] / d['total']:.0f} %).")
            lignes.append("Les alertes qui dependent de cette source sont "
                          "peut-etre incompletes SANS que rien d'autre le dise.")
        vides = [s for s in self.obs if self.absence_criante(s)]
        if vides:
            if lignes:
                lignes.append("")
            lignes.append("🕳️ **Une source repond sans rendre d'objet** — "
                          "HTTP 200, corps vide de donnees.")
            for s in sorted(vides):
                d = self.obs[s]
                lignes.append(
                    f"· **{s}** : {d['absent']} reponse(s) sans objet sur "
                    f"{d['total']} ({100.0 * d['absent'] / d['total']:.0f} %).")
            lignes.append("⛔ Ne pas ralentir et ne pas relire la requete : "
                          "l'une et l'autre vont bien. Verifier que les "
                          "identifiants demandes existent encore.")
        if not lignes:
            return False, ""
        return True, "\n".join(lignes)

    # ---------------------------------------------------------------- releve
    def resume(self) -> str:
        """Le releve, une ligne par source. C'est l'audit empirique qui
        manquait : il se lit dans le log de CHAQUE run, gratuitement."""
        if not self.obs:
            return "🩺 sentinelle : aucune requete observee."
        # ⚪ = angle mort. ⭐ Une icone inconnue ne doit pas faire tomber le
        # releve : il est imprime par un `atexit`, un KeyError y sortirait une
        # trace de pile a la place du bilan.
        icone = {"ouverte": "🟢", "lente": "🟠", "se_ferme": "🔴",
                 "angle_mort": "⚪"}
        l: List[str] = ["🩺 SANTE DES SOURCES (ce run) :"]
        for s in sorted(self.obs):
            d, v = self.obs[s], self.verdict(s)
            detail = []
            if d["repousse"]:
                detail.append(f"{d['repousse']} refus")
            if d["serveur"]:
                detail.append(f"{d['serveur']} 5xx")
            if d["reseau"]:
                detail.append(f"{d['reseau']} reseau")
            # ⭐⭐ CE QUI REND « RAS » IMPOSSIBLE QUAND ON DEMANDE MAL.
            # `detail` non vide => la ligne ne peut plus finir par « RAS ».
            # On nomme la cause dans le message : un 400 n'appelle pas une
            # pause, il appelle une relecture de la requete.
            if d.get("invalide"):
                detail.append(f"🔧 {d['invalide']} requete(s) REFUSEE(S) "
                              f"(4xx) — c'est NOTRE requete, pas la source")
            # ⭐⭐ MEME RAISON, AUTRE CAUSE : `detail` non vide => plus de « RAS ».
            # Une reponse en 200 qui ne porte pas d'objet ne doit pas pouvoir
            # se lire comme un succes, sinon un catalogue peut fondre en
            # silence sous une ligne verte.
            if d.get("absent"):
                detail.append(f"🕳️ {d['absent']} reponse(s) SANS OBJET "
                              f"(HTTP 200 + errors[]) — la source va bien, "
                              f"l'objet n'y est pas")
            if v == "angle_mort":
                # ⭐⭐ Ne JAMAIS ecrire « RAS » ici. « verdict suspendu » se
                # lisait comme « ca va, patience » alors que ca ne mûrira pas :
                # les observations ne s'accumulent pas d'un run a l'autre.
                detail.append(f"ANGLE MORT — {d['total']} obs < {MIN_OBS} dans "
                              f"CE run, et rien ne s'accumule entre les runs")
            l.append(f"  {icone.get(v, '⚪')} {s:<14} {d['total']:>5} requete(s)"
                     + ("  — " + ", ".join(detail) if detail else "  — RAS"))
        aveugles = [s for s in self.obs if self.verdict(s) == "angle_mort"]
        if aveugles:
            l.append(f"  ⚪ {len(aveugles)} source(s) hors de portee du verdict : "
                     f"{', '.join(sorted(aveugles))}. Un refus TOTAL les ferait "
                     f"crier malgre tout ; un refus PARTIEL passera inapercu.")
        # 🔧 LE VERDICT NE PARLE QUE DE LA SOURCE — cette ligne parle de NOUS.
        # ⚠️ Necessaire parce qu'une source qui refuse 100 % de nos requetes
        # pour cause de requete invalide reste, elle, parfaitement « ouverte » :
        # l'icone serait 🟢 et elle aurait raison. ⭐⭐ *Le bon verdict sur la
        # mauvaise question rassure autant qu'un faux verdict.*
        mal = {s: d["invalide"] for s, d in self.obs.items() if d.get("invalide")}
        if mal:
            detail = ", ".join(f"{s} ({n}/{self.obs[s]['total']})"
                               for s, n in sorted(mal.items()))
            l.append(f"  🔧 {sum(mal.values())} requete(s) refusee(s) pour "
                     f"REQUETE INVALIDE (4xx hors 401/403/429) : {detail}.")
            l.append("     ⛔ Ralentir n'y changera rien — c'est un champ, un "
                     "identifiant ou un parametre a corriger DANS LE CODE.")
        # 🕳️ L'ABSENCE — ni la source, ni la requete, ni nous. L'objet.
        # ⭐⭐ Elle merite sa propre ligne parce que sa REPONSE est differente
        # des deux autres : on ne ralentit pas, on ne corrige pas la requete,
        # on va voir si l'item existe encore. Un conseil faux vaut moins
        # qu'aucun conseil : il envoie chercher au mauvais endroit.
        vide = {s: d["absent"] for s, d in self.obs.items() if d.get("absent")}
        if vide:
            detail = ", ".join(f"{s} ({n}/{self.obs[s]['total']})"
                               for s, n in sorted(vide.items()))
            l.append(f"  🕳️ {sum(vide.values())} reponse(s) SANS OBJET "
                     f"(HTTP 200 + errors[], ex. « Entity not found ») : {detail}.")
            l.append("     ⛔ Ni un refus, ni une requete fausse : la source a "
                     "repondu qu'elle n'a pas cet objet. Item retire du store, "
                     "ou identifiant qui a derive — ca se verifie au catalogue.")
        return "\n".join(l)


# ===========================================================================
# ⭐⭐ LA SENTINELLE PARTAGEE — A4, 29/07/2026
# ===========================================================================
# Jusqu'ici ce module n'etait importe QUE par `floor_watch.py`, qui creait sa
# propre instance. Portee reelle : StackR, une source sur quatre. Le tracker,
# CollectScan et le GraphQL VeVe — les sources IRREMPLACABLES — n'avaient
# aucun compteur.
#
# ⛔ Pourquoi une instance de module et pas une par run : dans `scrapeur-veve`
#    la frontiere HTTP vit dans des modules-bibliotheque (`veve_scraper`,
#    `veve_detail`, `collectchain`, `stackr_sales`) importes par une dizaine de
#    points d'entree differents. Passer une instance de main en main aurait
#    demande de toucher les dix — et le premier oubli aurait rendu un
#    collecteur muet SANS que rien ne le dise. Un processus = un run : le
#    module porte donc le compteur.
#
# ⭐ `floor_watch` garde son instance propre. Les deux coexistent sans se
#    gener, et le fichier reste IDENTIQUE dans les deux depots — condition
#    pour que le controle d'empreinte (`test_sentinelle_empreinte`) ait un
#    sens. Voir la note sur le module en double plus bas.
SENTINELLE = Sentinelle()


def noter_reponse(source: str, reponse=None, erreur=None,
                  absent: bool = False) -> None:
    """Noter une reponse HTTP sur la sentinelle de ce processus.

    ⭐⭐ LE GESTE QUI COMPTE : appeler ceci AVANT `raise_for_status()`.

    Les quatre collecteurs ont tous la meme forme :

        try:
            r = session.get(...)
            r.raise_for_status()      # <- le code devient une exception
            return r.json()
        except Exception as e:        # <- 429 et timeout deviennent EGAUX
            ...

    A ce stade le statut est perdu. Or « la source nous REPOUSSE » (429/403)
    et « la source est EN PEINE » (5xx, timeout) n'appellent pas la meme
    reaction : ralentir face a un 429 sert, face a un 5xx non. C'est
    exactement la distinction que ce module existe pour faire — et elle
    mourait dans le `except`.

    ⚠️ `reponse` est volontairement DUCK-TYPE : tout objet portant
    `.status_code` convient. Ce module n'importe pas `requests` et ne doit
    jamais le faire — c'est ce qui le rend testable sans reseau.

    ⭐ `absent` se PASSE, il ne se DEVINE pas ici. Reconnaitre un corps
    GraphQL vide demanderait de le lire — donc de connaitre son format, donc
    de le parser pour TOUTES les sources, y compris celles qui n'en ont pas.
    Le collecteur, lui, sait deja ce qu'il vient de recevoir.
    """
    code = getattr(reponse, "status_code", None) if reponse is not None else None
    SENTINELLE.noter(source, code, None if erreur is None else str(erreur),
                     absent=absent)


def resume() -> str:
    """Le releve du processus courant."""
    return SENTINELLE.resume()


def doit_crier() -> Tuple[bool, str]:
    """(faut-il prevenir un humain, quoi lui dire) pour le processus courant."""
    return SENTINELLE.doit_crier()

# ===========================================================================
# ⭐⭐ LE RELEVE SE POSE UNE FOIS, PAS DANS CHAQUE POINT D'ENTREE
# ===========================================================================
# Les collecteurs instrumentes sont des modules-BIBLIOTHEQUE, importes par une
# vingtaine de points d'entree (`run`, `dynamic_run`, `chain_run`,
# `stackr_sales`, `market_universe`, `compare_elements`…). Ajouter trois lignes
# a la fin de chacun, c'est se donner vingt occasions d'en oublier un — et un
# point d'entree oublie ne fait AUCUNE erreur : il rend simplement ses sources
# invisibles. C'est la forme exacte du defaut qu'on repare.
#
# `atexit` couvre donc TOUS les points d'entree, y compris ceux qui n'existent
# pas encore, et il se declenche AUSSI quand le run meurt en exception —
# c'est-a-dire precisement le jour ou une source nous a repousses.
#
# ⛔ AUCUN RESEAU ICI, et c'est deliberé :
#    · le module resterait testable hors ligne (sa raison d'etre) ;
#    · un cri Discord demanderait un secret de plus, et un secret jamais
#      renseigne est un garde-fou qui se croit arme. L'annotation GitHub
#      Actions ne demande rien et s'affiche dans le resume du run.
#    Quand les compteurs auront montre qu'un canal dedie vaut le coup, il se
#    posera SUR ces chiffres — pas a l'aveugle.
import atexit as _atexit


def _releve_de_fin() -> None:
    if not SENTINELLE.obs:
        return                      # aucune requete : rien a dire, on se tait
    try:
        print(SENTINELLE.resume(), flush=True)
        crier, texte = SENTINELLE.doit_crier()
        if crier:
            plat = texte.replace("\n", " · ")
            # ⭐ Le titre ne peut plus nommer UNE cause : depuis le 06/08 le
            # cri couvre aussi « la source repond sans objet ». Un titre qui
            # annonce un blocage devant un message d'absence enverrait
            # chercher au mauvais endroit — le texte, lui, nomme la cause.
            print(f"::warning title=Sentinelle des sources::{plat}", flush=True)
    except Exception:               # un releve ne fait jamais tomber un run
        pass


_atexit.register(_releve_de_fin)


# ⛔⛔ CE FICHIER EXISTE EN DEUX EXEMPLAIRES : `fanablefrance/jetonveve` et
# `VeVePreda/scrapeur-veve`. L'audit des depots du 29/07 a nomme le module en
# double qui a DIVERGE comme le vrai risque du projet — pas le fichier egare.
# Les deux copies doivent rester OCTET POUR OCTET identiques ; un test
# d'empreinte dans chaque depot le verifie. Si tu modifies ce fichier, tu le
# modifies DES DEUX COTES, et tu mets a jour l'empreinte dans les deux tests.
