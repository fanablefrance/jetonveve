# ⚠️ DEPOT : fanablefrance/jetonveve   ·   CHEMIN : scraper/sentinelle_sources.py
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
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

# Seuils. Un pourcentage seul ne veut rien dire sur 3 requetes : il faut un
# MINIMUM d'observations avant de prononcer un verdict, sinon la sentinelle
# devient elle-meme une source de fausses alertes.
MIN_OBS = int(os.environ.get("SENTINELLE_MIN_OBS", "30"))
SEUIL_FERME_PCT = float(os.environ.get("SENTINELLE_FERME_PCT", "5"))
SEUIL_LENTE_PCT = float(os.environ.get("SENTINELLE_LENTE_PCT", "20"))
# Pause additionnelle conseillee quand une source nous repousse (secondes).
PAUSE_MAX_S = float(os.environ.get("SENTINELLE_PAUSE_MAX_S", "30"))

_REPOUSSE = (401, 403, 429)


class Sentinelle:
    """Un compteur par source. Instancier une fois par run."""

    def __init__(self) -> None:
        self.obs: Dict[str, Dict[str, int]] = {}

    # ---------------------------------------------------------------- mesure
    def noter(self, source: str, code: Optional[int] = None,
              erreur: Optional[str] = None) -> None:
        """Une reponse observee. `code` = statut HTTP, ou None si la requete
        n'a meme pas abouti (timeout, DNS, connexion) — auquel cas `erreur`
        porte le texte. Ne leve jamais."""
        d = self.obs.setdefault(source, {"total": 0, "ok": 0, "repousse": 0,
                                         "serveur": 0, "reseau": 0})
        d["total"] += 1
        if code is None:
            d["reseau"] += 1
        elif code in _REPOUSSE:
            d["repousse"] += 1
        elif code >= 500:
            d["serveur"] += 1
        elif code < 400:
            d["ok"] += 1
        else:                       # 4xx qui ne sont pas un refus de nous
            d["serveur"] += 0       # ni bon ni mauvais signe : on ne compte que
            d["ok"] += 0            # dans le total (visible par difference)

    # --------------------------------------------------------------- verdict
    def verdict(self, source: str) -> str:
        d = self.obs.get(source)
        if not d or d["total"] < MIN_OBS:
            return "ouverte"
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
        if not d or d["total"] < MIN_OBS or not d["repousse"]:
            return 0.0
        taux = d["repousse"] / d["total"]
        return round(min(PAUSE_MAX_S, PAUSE_MAX_S * taux * 4), 1)

    def doit_crier(self) -> Tuple[bool, str]:
        """(faut-il prevenir un humain, quoi lui dire).

        ⭐ UNE SOURCE MUETTE N'EST PAS UN MARCHE CALME. C'est la lecon qui a
        coute trois semaines de silence : sans ce message, un blocage se lit
        comme « rien a signaler »."""
        fermees = [s for s in self.obs if self.verdict(s) == "se_ferme"]
        if not fermees:
            return False, ""
        lignes = ["🚨 **Une source nous repousse** — ce n'est pas un marche calme."]
        for s in sorted(fermees):
            d = self.obs[s]
            lignes.append(
                f"· **{s}** : {d['repousse']} refus (429/403) sur {d['total']} "
                f"requetes ({100.0 * d['repousse'] / d['total']:.0f} %).")
        lignes.append("Les alertes qui dependent de cette source sont "
                      "peut-etre incompletes SANS que rien d'autre le dise.")
        return True, "\n".join(lignes)

    # ---------------------------------------------------------------- releve
    def resume(self) -> str:
        """Le releve, une ligne par source. C'est l'audit empirique qui
        manquait : il se lit dans le log de CHAQUE run, gratuitement."""
        if not self.obs:
            return "🩺 sentinelle : aucune requete observee."
        icone = {"ouverte": "🟢", "lente": "🟠", "se_ferme": "🔴"}
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
            if d["total"] < MIN_OBS:
                detail.append(f"< {MIN_OBS} obs : verdict suspendu")
            l.append(f"  {icone[v]} {s:<14} {d['total']:>5} requete(s)"
                     + ("  — " + ", ".join(detail) if detail else "  — RAS"))
        return "\n".join(l)
