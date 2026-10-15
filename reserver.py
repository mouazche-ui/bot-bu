"""
Réservation automatique Affluences - BU Joseph-Fourier & Validation Webmail UGA
"""
import json
import os
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

# ---------- Réglages ----------
SITE_SLUG = "bu-joseph-fourier"
SITE_ID = "888a64d4-3c8d-4ebf-83a5-d9441b5431b9"
TYPE_ID = "5249"                    # Salles de travail en groupe
SALLES = ["RDC Sud n°4"]            # Ordre de préférence prioritaire
SECOURS = "RDC Sud"                 # Si prises : n'importe quelle salle dont le nom commence par ça
HEURE_DEBUT = "11:30"
HEURE_FIN = "14:30"
JOURS_AVANCE = 14
NB_PERSONNES = int(os.environ.get("NB_PERSONNES", "2"))

# ---------- Secrets (GitHub > Settings > Secrets) ----------
EMAIL = os.environ["AFF_EMAIL"].strip()
UGA_USER = os.environ.get("UGA_USER")
UGA_PASS = os.environ.get("UGA_PASS")
TG_TOKEN = os.environ.get("TG_TOKEN")             # Optionnel : alerte en cas d'échec
TG_CHAT = os.environ.get("TG_CHAT")               # Optionnel

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
    if len(sys.argv) > 1 and sys.argv[1].strip():
        d = datetime.strptime(sys.argv[1].strip(), "%Y-%m-%d").date()
    return d


def api(page, method, url, body=None):
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
    ordre = []
    for voulu in SALLES:
        ordre += [s for s in salles if normaliser(s[0]) == normaliser(voulu)]
    ordre += [s for s in salles
              if normaliser(s[0]).startswith(normaliser(SECOURS)) and s not in ordre]
    return ordre


def confirmer_mail(page):
    if not (UGA_USER and UGA_PASS):
        log("Identifiants UGA absents.")
        return False

    log("Connexion au webmail UGA...")
    page.goto("https://webmail.etu.univ-grenoble-alpes.fr", wait_until="domcontentloaded")
    page.fill("#username", UGA_USER)
    page.fill("#password", UGA_PASS)
    page.click("#submitBtn")
    page.wait_for_timeout(5000)

    for essai in range(1, 7):
        log(f"Recherche du mail Affluences (essai {essai}/6)...")
        page.wait_for_timeout(10000)

        mails = page.get_by_text("Confirmez votre réservation : BU Joseph-Fourier")
        if mails.count() == 0:
            page.reload(wait_until="domcontentloaded")
            continue

        log("Mail trouvé, ouverture...")
        mails.first.click()
        page.wait_for_timeout(4000)

        lien = None
        for cadre in page.frames:
            btn = cadre.locator("a", has_text="Confirmer ma réservation")
            if btn.count() > 0:
                lien = btn.first.get_attribute("href")
                break

        if lien:
            log("Lien de confirmation extrait ! Validation en cours...")
            page.goto(lien, wait_until="networkidle")
            page.wait_for_timeout(3000)
            return True

    log("❌ Échec : le mail de confirmation n'a pas été trouvé à temps.")
    return False


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

        if not reservee:
            telegram(f"❌ Réservation BU ratée pour le {jour_txt}. Salles pleines.")
            nav.close()
            sys.exit(1)

        log(f"Réservation demandée sur Affluences ✅ : {reservee}")
        
        succes_validation = confirmer_mail(page)
        nav.close()

    if succes_validation:
        log("🎉 Réservation complètement validée et confirmée !")
        telegram(f"🎉 SUCCÈS : {reservee} réservée et validée automatiquement pour le {jour_txt} ({HEURE_DEBUT}-{HEURE_FIN}) !")
    else:
        telegram(f"⚠️ {reservee} demandée pour le {jour_txt}, mais la validation automatique du mail a échoué.")
        sys.exit(1)


if __name__ == "__main__":
    main()
