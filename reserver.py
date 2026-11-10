"""
Réservation automatique Affluences - BU Joseph-Fourier & Validation Webmail UGA
Multi-créneaux (Midi + Après-midi/Soir) avec rotation automatique d'alias (+1, +2...)
pour contourner le quota journalier de 3h00 par adresse e-mail.
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
SALLES = ["RDC Sud n°4"]            # 1er choix prioritaire
SECOURS_SUD = "RDC Sud"             # 2e choix : toutes les autres salles RDC Sud
SECOURS_NORD = "Nord"               # 3e choix : toutes les salles Nord
JOURS_AVANCE = 14
QUOTA_MAX_MINUTES = 180             # Quota Affluences : 3h00 (180 min) max par jour et par email
NB_PERSONNES = int(os.environ.get("NB_PERSONNES", "2"))

# ---------- Planning par jour (0 = Lundi ... 4 = Vendredi) ----------
# Chaque bloc contient des sous-créneaux <= 3h00 (avec variantes de secours si un horaire est complet)
PLANNING = {
    0: [  # Lundi : 11h30-14h30 et 17h00-21h00 (découpé en 17h-19h + 19h-21h)
        [[("11:30", "14:30")], [("11:30", "13:30"), ("13:30", "14:30")]],
        [
            [("17:00", "19:00"), ("19:00", "21:00")],
            [("17:00", "20:00"), ("20:00", "21:00")],
            [("16:30", "19:00"), ("19:00", "21:00")],
        ],
    ],
    1: [  # Mardi : 11h30-14h30 et 17h00-21h00 (découpé en 17h-19h + 19h-21h)
        [[("11:30", "14:30")], [("11:30", "13:30"), ("13:30", "14:30")]],
        [
            [("17:00", "19:00"), ("19:00", "21:00")],
            [("17:00", "20:00"), ("20:00", "21:00")],
            [("16:30", "19:00"), ("19:00", "21:00")],
        ],
    ],
    2: [  # Mercredi : 11h30-14h30 + 13h30-21h00 (couverture continue de 11h30 à 21h00)
        [[("11:30", "14:30")], [("11:30", "13:30")]],
        [
            [("14:30", "17:30")],
            [("13:30", "16:30")],
            [("14:30", "17:00")],
        ],
        [
            [("17:30", "19:30"), ("19:30", "21:00")],
            [("17:00", "19:00"), ("19:00", "21:00")],
            [("16:30", "19:00"), ("19:00", "21:00")],
        ],
    ],
    3: [  # Jeudi : 11h30-14h30 et 17h00-21h00 (découpé en 17h-19h + 19h-21h)
        [[("11:30", "14:30")], [("11:30", "13:30"), ("13:30", "14:30")]],
        [
            [("17:00", "19:00"), ("19:00", "21:00")],
            [("17:00", "20:00"), ("20:00", "21:00")],
            [("16:30", "19:00"), ("19:00", "21:00")],
        ],
    ],
    4: [  # Vendredi : 11h30-14h30 et 17h00-21h00 (découpé en 17h-19h + 19h-21h)
        [[("11:30", "14:30")], [("11:30", "13:30"), ("13:30", "14:30")]],
        [
            [("17:00", "19:00"), ("19:00", "21:00")],
            [("17:00", "20:00"), ("20:00", "21:00")],
            [("16:30", "19:00"), ("19:00", "21:00")],
            [("17:00", "19:00")],
        ],
    ],
}

# ---------- Secrets ----------
RAW_EMAIL = os.environ["AFF_EMAIL"].strip()
UGA_USER = os.environ.get("UGA_USER")
UGA_PASS = os.environ.get("UGA_PASS")
TG_TOKEN = os.environ.get("TG_TOKEN")
TG_CHAT = os.environ.get("TG_CHAT")

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


def generer_pool_emails(raw_email):
    """
    Génère l'adresse principale + les alias +1, +2, +3, +4, +5 qui arrivent tous
    dans la même boîte mail UGA, offrant 6 x 3h = 18h de quota par jour.
    """
    bases = [e.strip() for e in raw_email.split(",") if e.strip()]
    pool = []
    for b in bases:
        if b not in pool:
            pool.append(b)
    for i in range(1, 6):
        for b in bases:
            if "@" in b:
                local, dom = b.split("@", 1)
                local_pur = local.split("+")[0]
                alias = f"{local_pur}+{i}@{dom}"
                if alias not in pool:
                    pool.append(alias)
    return pool


def duree_minutes(debut, fin):
    h1, m1 = map(int, debut.split(":"))
    h2, m2 = map(int, fin.split(":"))
    return (h2 * 60 + m2) - (h1 * 60 + m1)


def normaliser(txt):
    txt = unicodedata.normalize("NFKD", txt).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", txt.lower())


def date_cible():
    d = datetime.now(PARIS).date() + timedelta(days=JOURS_AVANCE)
    if len(sys.argv) > 1 and sys.argv[1].strip():
        d = datetime.strptime(sys.argv[1].strip(), "%Y-%m-%d").date()
    return d


def api(page, method, url, body=None):
    """Appel API isolé (sans cookies partagés) depuis la page affluences.com."""
    return page.evaluate(
        """async ([method, url, body]) => {
            const r = await fetch(url, {
                method,
                credentials: "omit",
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
    """
    Ordre de priorité :
    1. RDC Sud n°4
    2. Autres salles RDC Sud
    3. Salles Nord (Étage Nord, RDC Nord, etc.)
    4. N'importe quelle autre salle disponible
    """
    ordre = []
    for voulu in SALLES:
        ordre += [s for s in salles if normaliser(s[0]) == normaliser(voulu) and s not in ordre]
    ordre += [
        s for s in salles
        if normaliser(SECOURS_SUD) in normaliser(s[0]) and s not in ordre
    ]
    ordre += [
        s for s in salles
        if normaliser(SECOURS_NORD) in normaliser(s[0]) and s not in ordre
    ]
    ordre += [s for s in salles if s not in ordre]
    return ordre


def est_deja_reserve_sur_ce_creneau(txt_err):
    """Vrai uniquement si une réservation est déjà active sur ce créneau précis."""
    txt = txt_err.lower()
    motifs = (
        "already_have_reservation",
        "déjà une réservation en cours sur ce créneau",
        "deja une reservation en cours sur ce creneau",
        "already have a reservation",
    )
    return any(m in txt for m in motifs)


def est_quota_depasse(txt_err):
    """Vrai si l'adresse e-mail a atteint son quota journalier de 03:00."""
    txt = txt_err.lower()
    motifs = (
        "quota_overpassed",
        "quota journalier",
        "atteint votre quota",
        "limite journalière",
        "limite journaliere",
    )
    return any(m in txt for m in motifs)


def est_creneau_invalide(txt_err):
    """Vrai si Affluences refuse le format ou la durée du créneau."""
    txt = txt_err.lower()
    return "invalid_timeslot" in txt or "créneau non valide" in txt or "creneau non valide" in txt


def connecter_webmail(mail_page):
    """Ouvre et connecte la session Webmail UGA."""
    if not (UGA_USER and UGA_PASS):
        log("Identifiants UGA absents dans les secrets GitHub.")
        return False
    try:
        log("Connexion au webmail UGA...")
        mail_page.goto("https://webmail.etu.univ-grenoble-alpes.fr", wait_until="domcontentloaded")
        if mail_page.locator("#username").count() > 0:
            mail_page.fill("#username", UGA_USER)
            mail_page.fill("#password", UGA_PASS)
            mail_page.click("#submitBtn")
            mail_page.wait_for_timeout(5000)
        return True
    except Exception as e:
        log(f"Erreur connexion webmail : {e}")
        return False


def confirmer_mail(nav, mail_page, liens_deja_valides):
    """Cherche le nouveau mail Affluences non encore validé et clique sur le lien."""
    if not (UGA_USER and UGA_PASS):
        return False

    for essai in range(1, 8):
        log(f"Recherche du mail Affluences (essai {essai}/7)...")
        mail_page.wait_for_timeout(7000)

        try:
            mail_page.reload(wait_until="domcontentloaded")
            mail_page.wait_for_timeout(3500)
        except Exception:
            pass

        mails = mail_page.get_by_text("Confirmez votre réservation : BU Joseph-Fourier")
        nb_mails = mails.count()
        if nb_mails == 0:
            continue

        lien_trouve = None
        for idx in range(min(nb_mails, 5)):
            try:
                mails.nth(idx).click()
                mail_page.wait_for_timeout(3000)
            except Exception:
                continue

            for cadre in mail_page.frames:
                btns = cadre.locator("a", has_text="Confirmer ma réservation")
                for b_idx in range(btns.count() - 1, -1, -1):
                    href = btns.nth(b_idx).get_attribute("href")
                    if href and href not in liens_deja_valides:
                        lien_trouve = href
                        break
                if lien_trouve:
                    break
            if lien_trouve:
                break

        if lien_trouve:
            log("Nouveau lien de confirmation extrait ! Validation en cours...")
            liens_deja_valides.add(lien_trouve)
            confirm_page = nav.new_page(locale="fr-FR")
            try:
                confirm_page.goto(lien_trouve, wait_until="networkidle", timeout=20000)
                confirm_page.wait_for_timeout(2500)
            finally:
                confirm_page.close()
            return True

    log("❌ Échec : aucun nouveau mail de confirmation trouvé à temps.")
    return False


def reserver_sous_creneau(
    aff_page, nav, mail_page, jour_txt, debut, fin, candidates,
    pool_emails, minutes_par_email, liens_deja_valides, salle_preferee=None
):
    """
    Réserve le sous-créneau (debut -> fin) en sélectionnant automatiquement un alias email
    qui dispose d'assez de quota (< 180 min) et en basculant sur l'alias suivant si quota_overpassed.
    """
    duree = duree_minutes(debut, fin)
    liste_salles = list(candidates)
    if salle_preferee:
        liste_salles.sort(key=lambda s: 0 if s[0] == salle_preferee else 1)

    for nom, rid in liste_salles:
        # On parcourt le pool d'emails (base, +1, +2...) pour cette salle
        for email_courant in pool_emails:
            if minutes_par_email.get(email_courant, 0) + duree > QUOTA_MAX_MINUTES:
                continue

            r = api(aff_page, "POST", f"https://reservation.affluences.com/api/reserve/{rid}", {
                "email": email_courant,
                "date": jour_txt,
                "start_time": debut,
                "end_time": fin,
                "person_count": NB_PERSONNES,
                "note": "",
            })
            log(f"[{debut}-{fin}] {nom} (id {rid}) via {email_courant} -> {r['status']} {r['text'][:180]}")

            if r["status"] in (200, 201):
                minutes_par_email[email_courant] = minutes_par_email.get(email_courant, 0) + duree
                log(f"✅ Réservation posée : {nom} ({debut}-{fin}) via {email_courant}. Validation mail...")
                ok_mail = confirmer_mail(nav, mail_page, liens_deja_valides)
                return ("OK" if ok_mail else "MAIL_KO", nom)

            # Si ce créneau précis est déjà réservé par cet email
            if est_deja_reserve_sur_ce_creneau(r["text"]):
                minutes_par_email[email_courant] = minutes_par_email.get(email_courant, 0) + duree
                log(f"ℹ️ Créneau {debut}-{fin} déjà réservé sur ton compte ({email_courant}).")
                return ("DEJA_FAIT", nom)

            # Si cet email a atteint son quota de 3h00, on le marque plein et on teste immédiatement l'alias + suivant sur la MÊME salle !
            if est_quota_depasse(r["text"]):
                log(f"🔄 Quota de 3h atteint sur {email_courant} -> bascule automatique sur l'alias suivant...")
                minutes_par_email[email_courant] = QUOTA_MAX_MINUTES
                continue

            # Si l'horaire lui-même est refusé par Affluences, inutile de tester les autres salles
            if est_creneau_invalide(r["text"]):
                return ("HORAIRE_INVALIDE", None)

            # Sinon (la salle est déjà prise par quelqu'un d'autre sur ce créneau) :
            # on garde le même email_courant et on sort de la boucle d'emails pour tester la salle suivante !
            break

    return ("ECHEC", None)


def reserver_bloc_intelligent(
    aff_page, nav, mail_page, jour_txt, variantes, candidates,
    pool_emails, minutes_par_email, liens_deja_valides
):
    """
    Garantit qu'on essaie d'abord d'avoir la même salle prioritaire (ex: RDC Sud n°4)
    sur tous les sous-créneaux consécutifs (ex: 17h-19h puis 19h-21h).
    """
    resultats = []

    for nom_salle, rid_salle in candidates:
        salle_unique = [(nom_salle, rid_salle)]
        for variante in variantes:
            succes_variante = True
            sous_resultats = []
            for idx, (debut, fin) in enumerate(variante):
                cibles = salle_unique if idx == 0 else candidates
                statut, salle_retenue = reserver_sous_creneau(
                    aff_page, nav, mail_page, jour_txt, debut, fin, cibles,
                    pool_emails, minutes_par_email, liens_deja_valides, salle_preferee=nom_salle
                )
                if statut in ("OK", "MAIL_KO", "DEJA_FAIT"):
                    sous_resultats.append((debut, fin, salle_retenue, statut))
                else:
                    succes_variante = False
                    break

            if succes_variante and sous_resultats:
                return sous_resultats
            elif sous_resultats:
                resultats.extend(sous_resultats)
                return resultats

    return resultats


def main():
    jour = date_cible()
    num_jour = jour.weekday()
    if num_jour >= 5:
        log(f"{jour} est un week-end, rien à faire.")
        return

    jour_txt = jour.isoformat()
    blocs_du_jour = PLANNING.get(num_jour, [])
    pool_emails = generer_pool_emails(RAW_EMAIL)
    minutes_par_email = {e: 0 for e in pool_emails}

    log(f"=== Objectif pour le {jour_txt} (jour {num_jour}) : {len(blocs_du_jour)} grands créneaux ===")
    log(f"Pool d'alias activé ({len(pool_emails)} adresses) : {', '.join(pool_emails[:4])}...")

    bilan_succes = []
    bilan_deja_fait = []
    bilan_echecs = []
    liens_deja_valides = set()

    with sync_playwright() as p:
        nav = p.chromium.launch()
        aff_page = nav.new_page(locale="fr-FR")
        mail_page = nav.new_page(locale="fr-FR")

        connecter_webmail(mail_page)

        aff_page.goto(
            f"https://affluences.com/fr/sites/{SITE_SLUG}/reservation?type={TYPE_ID}&date={jour_txt}",
            wait_until="networkidle",
        )

        candidates = ordre_salles(trouver_salles(aff_page, jour_txt))
        log("Ordre de priorité : " + ", ".join(n for n, _ in candidates))

        for num_bloc, variantes in enumerate(blocs_du_jour, start=1):
            desc_cible = " + ".join(f"{d}-{f}" for d, f in variantes[0])
            log(f"--- Traitement du bloc {num_bloc}/{len(blocs_du_jour)} (cible : {desc_cible}) ---")

            res_bloc = reserver_bloc_intelligent(
                aff_page, nav, mail_page, jour_txt, variantes, candidates,
                pool_emails, minutes_par_email, liens_deja_valides
            )

            if not res_bloc:
                bilan_echecs.append(desc_cible)
            else:
                for debut, fin, salle, statut in res_bloc:
                    if statut == "OK":
                        bilan_succes.append(f"✅ {salle} ({debut}-{fin})")
                    elif statut == "MAIL_KO":
                        bilan_echecs.append(f"⚠️ {salle} ({debut}-{fin} : mail non validé)")
                    elif statut == "DEJA_FAIT":
                        bilan_deja_fait.append(f"☑️ Déjà actif ({debut}-{fin})")

        nav.close()

    # ---------- Résumé final & Notification Telegram ----------
    if bilan_succes:
        msg = f"🎉 Réservations BU confirmées pour le {jour_txt} :\n" + "\n".join(bilan_succes)
        if bilan_deja_fait:
            msg += "\n\n" + "\n".join(bilan_deja_fait)
        if bilan_echecs:
            msg += "\n\n❌ Créneaux incomplets :\n" + "\n".join(bilan_echecs)
        log(msg)
        telegram(msg)
    elif bilan_deja_fait and not bilan_echecs:
        log(f"Toutes les réservations du {jour_txt} étaient déjà actives. Rien à signaler.")
    else:
        msg = f"❌ Échec sur les réservations BU du {jour_txt} :\n" + "\n".join(bilan_echecs)
        log(msg)
        telegram(msg)
        sys.exit(1)


if __name__ == "__main__":
    main()
