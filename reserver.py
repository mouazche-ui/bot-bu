"""
Réservation automatique Affluences - BU Joseph-Fourier
Réserve la salle choisie, de 11h30 à 14h30, 14 jours à l'avance.
La confirmation (lien reçu par mail, 2 h max) reste à faire à la main :
le bot envoie une notification Telegram pour y penser.
"""
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

# ---------- Réglages (modifiables) ----------
SITE_SLUG = "bu-joseph-fourier"
SITE_ID = "888a64d4-3c8d-4ebf-83a5-d9441b5431b9"
TYPE_ID = "5249"                      # Salles de travail en groupe
SALLES = ["RDC Sud n°4"]              # ordre de préférence
SECOURS = "RDC Sud"                   # si prises : n'importe quelle salle dont le nom commence par ça
HEURE_DEBUT = "11:30"
HEURE_FIN = "14:30"
JOURS_AVANCE = 14
NB_PERSONNES = int(os.environ.get("NB_PERSONNES", "2"))

# ---------- Secrets (GitHub > Settings > Secrets) ----------
EMAIL = os.environ["AFF_EMAIL"].strip()          # mail universitaire utilisé pour réserver
TG_TOKEN = os.environ.get("TG_TOKEN")            # optionnel : notification Telegram
TG_CHAT = os.environ.get("TG_CHAT")              # optionnel

PARIS = ZoneInfo("Europe/Paris")


def log(msg):
    print(f"[{datetime.now(PARIS):%H:%M:%S}] {msg}", flush=True)


def telegram(msg):
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": msg}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data, timeout=10)
    except Exception as e:
        log(f"Telegram KO : {e}")


def normaliser(txt):
    txt = unicodedata.normalize("NFKD", txt).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", txt.lower())


def date_cible():
    d = datetime.now(PARIS).date() + timedelta(days=JOURS_AVANCE)
    if len(sys.argv) > 1:                       # test manuel : python reserver.py 2026-10-28
        d = datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
    return d


def api(page, method, url, body=None):
    """Appel fait depuis la page affluences.com (passe la protection anti-robot)."""
    return page.evaluate(
        """async ([method, url, body]) => {
            const r = await fetch(url, {
                method,
                headers: {"Content-Type": "application/json", "Accept": "application/json"},
                body: body ? JSON.stringify(body) : undefined,
            });
            return {status: r.status, text: await r.text()};
        }""",
        [method, url, body],
    )


def trouver_salles(page, jour):
    url = f"https://reservation.affluences.com/api/resources/{SITE_ID}/available?date={jour}&type={TYPE_ID}"
    r = api(page, "GET", url)
    if r["status"] != 200:
        raise RuntimeError(f"Liste des salles KO ({r['status']}) : {r['text'][:300]}")
    data = json.loads(r["text"])
    if isinstance(data, dict):
        data = data.get("data", data.get("resources", []))
    log("Salles trouvées : " + ", ".join(s.get("resource_name", "?") for s in data))
    return [(s.get("resource_name", ""), s.get("resource_id")) for s in data]


def ordre_salles(salles):
    """Salles préférées d'abord, puis toutes les autres 'RDC Sud'."""
    ordre = []
    for voulu in SALLES:
        ordre += [s for s in salles if normaliser(s[0]) == normaliser(voulu)]
    ordre += [s for s in salles
              if normaliser(s[0]).startswith(normaliser(SECOURS)) and s not in ordre]
    return ordre


def main():
    jour = date_cible()
    if jour.weekday() >= 5:
        log(f"{jour} est un week-end, rien à faire.")
        return
    jour_txt = jour.isoformat()
    log(f"Objectif : {jour_txt} de {HEURE_DEBUT} à {HEURE_FIN}")

    with sync_playwright() as p:
        nav = p.chromium.launch()
        page = nav.new_page(locale="fr-FR")
        page.goto(f"https://affluences.com/fr/sites/{SITE_SLUG}/reservation?type={TYPE_ID}&date={jour_txt}",
                  wait_until="networkidle")

        candidates = ordre_salles(trouver_salles(page, jour_txt))
        log("Ordre d'essai : " + ", ".join(n for n, _ in candidates))
        reservee = None
        for nom, rid in candidates:
            r = api(page, "POST", f"https://reservation.affluences.com/api/reserve/{rid}", {
                "email": EMAIL,
                "date": jour_txt,
                "start_time": HEURE_DEBUT,
                "end_time": HEURE_FIN,
                "person_count": NB_PERSONNES,
                "note": "",
            })
            log(f"{nom} (id {rid}) -> {r['status']} {r['text'][:200]}")
            if r["status"] in (200, 201):
                reservee = nom
                break

        nav.close()

    if not reservee:
        telegram(f"❌ Réservation BU ratée pour le {jour_txt}. Voir les logs GitHub.")
        sys.exit(1)

    log(f"Réservation demandée ✅ : {reservee}")
    telegram(f"📚 {reservee} demandée pour le {jour_txt}, {HEURE_DEBUT}-{HEURE_FIN}.\n"
             f"⚠️ Clique « Confirmer » dans le mail Affluences (2 h max).\n"
             f"📬 Ta messagerie : https://webmail.univ-grenoble-alpes.fr")


if __name__ == "__main__":
    main()
