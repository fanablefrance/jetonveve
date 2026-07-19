"""Pont floor_watch -> bot Discord (etape 4 du bot).

⚠️ CE FICHIER VA DANS LE DEPOT `jetonveve`, dans `scraper/`.
   PAS dans `veve-bot`. Ce sont deux depots differents.

═══ CE QUE CE FICHIER FAIT, ET SURTOUT CE QU'IL NE FAIT PAS ═══

floor_watch continue de pousser ses alertes dans ses canaux communs par
webhook, EXACTEMENT comme aujourd'hui. Ce module ajoute un SECOND
destinataire — le bot — qui les rediffuse dans le fil prive de chaque
abonne, filtrees selon ses reglages.

Rien n'est debranche, rien n'est migre. Bot eteint, casse ou en
redeploiement : les alertes arrivent quand meme. C'est ce choix qui ramene
le risque de cette etape a zero.

⭐ REGLE ABSOLUE : CE MODULE NE PEUT PAS FAIRE ECHOUER UN RUN.
Tout est en try/except, avec un timeout court. Un cron de collecte qui
casse parce que le bot a hoquete serait exactement le couplage qu'on a
voulu eviter.

═══ POURQUOI LA TABLE `PRIX` EXISTE ═══

Les dictionnaires d'alerte de floor_watch n'ont PAS de schema commun.
Releve sur le code reel : le prix s'appelle `usd`, ou `floor`, ou `vente`,
selon le detecteur. Si le bot devait deviner, il lirait une cle absente,
recevrait None sans erreur, et appliquerait un filtre qui laisse tout
passer sans que personne ne le voie.

La parade : c'est ICI, du cote qui SAIT, qu'on nomme le champ de prix de
chaque canal. Le bot ne devine jamais. Un champ absent lui arrive comme
absent — jamais comme zero.
"""
import os
import time

import requests

URL = os.environ.get("BOT_ALERTES_URL", "").strip()
JETON = os.environ.get("BOT_ALERTES_JETON", "").strip()
TIMEOUT = float(os.environ.get("BOT_ALERTES_TIMEOUT", "5") or 5)

# Quel champ porte le prix, pour CHAQUE canal.
# None = ce signal n'a pas de prix, et n'en aura jamais (🔥 compte des
# ventes, 🔊 compte des offres). C'est DECLARE, pas deduit : cote bot, un
# signal declare sans prix n'est pas soumis au filtre de prix, et ne
# declenche pas la sentinelle « contrat casse ».
PRIX = {
    "mint":       "usd",
    "comics":     "usd",
    "steal":      "floor",
    "spike":      "vente",     # le prix de la VENTE, pas le floor
    "ath":        "floor",
    "atl":        "floor",
    "histlow":    "floor",
    "atl_stackr": "usd",
    "affaires":   "usd",
    "whale":      "usd",
    "pic":        None,        # signal de VOLUME : aucun prix
    "vol":        None,        # signal de VOLUME : aucun prix
}


def actif():
    """Vrai si le pont est configure. Variables absentes = rien ne part,
    et c'est le comportement voulu tant que le bot n'est pas deploye."""
    return bool(URL and JETON)


def _nombre(valeur):
    """Rend un float, ou None. Ne transforme JAMAIS une absence en 0."""
    if valeur is None or isinstance(valeur, bool):
        return None
    try:
        return float(valeur)
    except (TypeError, ValueError):
        return None


def _entier(valeur):
    nombre = _nombre(valeur)
    return None if nombre is None else int(nombre)


def faits(canal, a, embed):
    """Construit le bloc normalise attendu par le bot.

    Ce qui n'est pas la reste ABSENT de la charge : le bot le lira comme
    inconnu. Envoyer `"supply": 0` pour dire « je ne sais pas » ferait
    passer l'item sous n'importe quel filtre « supply max ».
    """
    bloc = {
        "canal": canal,
        "uuid": str(a.get("uuid") or a.get("element_id") or "").strip(),
        "nom": a.get("name") or a.get("nom"),
        "categorie": a.get("categorie"),
        "ts": int(time.time()),
        "embed": embed,
    }
    champ = PRIX.get(canal, "usd")
    if champ:
        prix = _nombre(a.get(champ))
        if prix is not None:
            bloc["prix_usd"] = prix
    for cle, lecteur in (("supply", _entier), ("listings", _entier)):
        valeur = lecteur(a.get(cle))
        if valeur is not None:
            bloc[cle] = valeur
    return bloc


def pousser_lot(canal, alerts, embeds, simuler=False):
    """Envoie un lot d'alertes au bot. Rend le nombre d'alertes acceptees.

    N'EMET AUCUNE EXCEPTION, quoi qu'il arrive.
    """
    if simuler or not actif() or not canal or not alerts:
        return 0
    envoyees = 0
    for a, embed in zip(alerts, embeds):
        try:
            bloc = faits(canal, a, embed)
        except Exception as e:                            # noqa: BLE001
            print("  bot : alerte illisible, ignoree ({})".format(e), flush=True)
            continue
        if not bloc["uuid"]:
            # Sans identifiant, le bot ne peut ni dedoublonner ni confronter
            # a une liste de surveillance. (Cas reel : les « gros transferts »
            # du suivi whale n'ont pas d'uuid.)
            continue
        try:
            r = requests.post(URL, json=bloc, timeout=TIMEOUT,
                              headers={"X-Jeton": JETON})
            if r.status_code < 300:
                envoyees += 1
            else:
                print("  bot : refuse ({}) {}".format(
                    r.status_code, r.text[:200]), flush=True)
        except Exception as e:                            # noqa: BLE001
            # On note et on sort. Le bot n'est jamais un point de panne pour
            # la collecte : s'il est injoignable, inutile d'attendre 10 fois
            # le timeout.
            print("  bot injoignable ({}) — les canaux communs, eux, sont "
                  "partis normalement.".format(e), flush=True)
            return envoyees
    if envoyees:
        print("  bot : {} alerte(s) transmise(s) aux abonnes.".format(envoyees),
              flush=True)
    return envoyees
