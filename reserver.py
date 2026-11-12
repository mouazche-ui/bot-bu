"""
Réservation automatique Affluences - BU Joseph-Fourier & Validation Webmail UGA
Supporte :
- Le mode quotidien automatique (J+14 : Midi + Après-midi/Soir)
- Le mode 'rattrapage' (scanne tous les jours d'aujourd'hui à J+14 pour prendre tout ce qui est libre de ~17h à 21h)
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

# ---------- Planning normal à J+14 (0 = Lundi ... 4 = Vendredi) ----------
PLANNING = {
    0: [  # Lundi
        [[("11:30", "14:30")], [("11:30", "13:30"), ("13:30", "14:30")]],
        [
            [("17:00", "19:00"), ("19:00", "21:00")],
            [("17:00", "20:00"), ("20:00", "21:00")],
            [("16:30", "19:00"), ("19:00", "21:00")],
        ],
    ],
    1: [  # Mardi
        [[("11:30", "14:30")], [("11:30", "13:30"), ("13:30", "14:30")]],
        [
            [("17:00", "19:00"), ("19:00", "21:00")],
            [("17:00", "20:00"), ("20:00", "21:00")],
            [("16:30", "19:00"), ("19:00", "21:00")],
        ],
    ],
    2: [  # Mercredi (11h30-14h30 + 13h30-21h00 en continu)
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
    3: [  # Jeudi
        [[("11:30", "14:30")], [("11:30", "13:30"), ("13:30", "14:30")]],
        [
            [("17:00", "19:00"), ("19:00", "21:00")],
            [("17:00", "20:00"), ("20:00", "21:00")],
            [("16:30", "19:00"), ("19:00", "21:00")],
        ],
    ],
    4: [  # Vendredi
        [[("11:30", "14:30")], [("11:30", "13:30"), ("13:30", "14:30")]],
        [
            [("17:00", "19:00"), ("19:00", "21:00")],
            [("17:00", "20:00"), ("20:00", "21:00")],
            [("16:30", "19:00"), ("19:00", "21:00")],
            [("17:00", "19:00")],
        ],
    ],
}

# ---------- Blocs du mode RATTRAPAGE (prend le maximum libre entre ~17h et 21h) ----------
# En séparant la soirée en 2 blocs indépendants (17h-19h et 19h-21h) avec des variantes souples,
# même si une salle n'est libre que de 17h à 19h ou de 18h30 à 21h sur un jour déjà entamé, il la prend !
BLOCS_RATTRAPAGE_SOIR = [
    [
        [("17:00", "19:00")],
        [("16:30", "19:00")],
        [("17:00", "18:30")],
        [("17:30", "19:00")],
    ],
    [
        [("19:00", "21:00")],
        [("18:30", "21:00")],
        [("19:00", "20:30")],
        [("19:30", "21:00")],
    ],
]

# Le mercredi en rattrapage : tente aussi le début d'après-midi (13h30-17h00) en plus de 17h-21h
BLOCS_RATTRAPAGE_MERCREDI = [
    [
        [("13:30", "15:30")],
        [("14:00", "15:30")],
    ],
    [
        [("15:30", "17:00")],
        [("15:00", "17:00")],
    ],
] + BLOCS_RATTRAPAGE_SOIR

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
    """Génère l'adresse principale + les alias +1 à +6 (7 x 3h = 21h de quota par jour)."""
    bases = [e.strip() for e in raw_email.split(",") if e.strip()]
    pool = []
    for b in bases:
        if b not in pool:
            pool.append(b)
    for i in range(1, 7):
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


def determiner_dates_et_mode():
    """
    - Sans argument (ou vide) : réserve J+14 (mode normal)
    - Argument 'rattrapage' ou 'all' : réserve tous les jours ouvrés d'aujourd'hui à J+14
    - Argument 'AAAA-MM-JJ' : réserve la date précise
    """
    auj = datetime.now(PARIS).date()
    arg = sys.argv[1].strip().lower() if len(sys.argv) > 1 else ""

    if arg in ("rattrapage", "all", "tout", "full"):
        dates = []
        for delta in range(0, JOURS_AVANCE + 1):
            d = auj + timedelta(days=delta)
            if d.weekday() < 5:
                dates.append(d)
        return dates, True

    if arg:
        return [datetime.strptime(arg, "%Y-%m-%d").date()], False

    return [auj + timedelta(days=JOURS_AVANCE)], False


def api(page, method, url, body=None):
    """Appel API isolé depuis la page affluences.com."""
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
    return [(s.get("resource_name", ""), s.get("resource_id")) for s in data]


def ordre_salles(salles):
    """1. RDC Sud n°4 -> 2. Autres RDC Sud -> 3. Nord -> 4. N'importe quelle salle."""
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
    txt = txt_err.lower()
    motifs = (
        "already_have_reservation",
        "déjà une réservation en cours sur ce créneau",
        "deja une reservation en cours sur ce creneau",
        "already have a reservation",
    )
    return any(m in txt for m in motifs)


def est_quota_depasse(txt_err):
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
    txt = txt_err.lower()
    return "invalid_timeslot" in txt or "créneau non valide" in txt or "creneau non valide" in txt


def connecter_webmail(mail_page):
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
        for idx in range(min(nb_mails, 6)):
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
    duree = duree_minutes(debut, fin)
    liste_salles = list(candidates)
    if salle_preferee:
        liste_salles.sort(key=lambda s: 0 if s[0] == salle_preferee else 1)

    for nom, rid in liste_salles:
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
            log(f"[{jour_txt} {debut}-{fin}] {nom} via {email_courant} -> {r['status']} {r['text'][:140]}")

            if r["status"] in (200, 201):
                minutes_par_email[email_courant] = minutes_par_email.get(email_courant, 0) + duree
                log(f"✅ Réservation posée : {nom} ({debut}-{fin}) via {email_courant}. Validation mail...")
                ok_mail = confirmer_mail(nav, mail_page, liens_deja_valides)
                return ("OK" if ok_mail else "MAIL_KO", nom)

            if est_deja_reserve_sur_ce_creneau(r["text"]):
                minutes_par_email[email_courant] = minutes_par_email.get(email_courant, 0) + duree
                log(f"ℹ️ Créneau {debut}-{fin} déjà réservé sur ton compte ({email_courant}).")
                return ("DEJA_FAIT", nom)

            if est_quota_depasse(r["text"]):
                log(f"🔄 Quota 3h atteint sur {email_courant} -> passage à l'alias suivant...")
                minutes_par_email[email_courant] = QUOTA_MAX_MINUTES
                continue

            if est_creneau_invalide(r["text"]):
                return ("HORAIRE_INVALIDE", None)

            # Salle déjà occupée par quelqu'un d'autre sur ce créneau : on passe à la salle suivante
            break

    return ("ECHEC", None)


def reserver_bloc_intelligent(
    aff_page, nav, mail_page, jour_txt, variantes, candidates,
    pool_emails, minutes_par_email, liens_deja_valides, salle_favorite_jour=None
):
    resultats = []
    liste_ordonnee = list(candidates)
    if salle_favorite_jour:
        liste_ordonnee.sort(key=lambda s: 0 if s[0] == salle_favorite_jour else 1)

    for nom_salle, rid_salle in liste_ordonnee:
        salle_unique = [(nom_salle, rid_salle)]
        for variante in variantes:
            succes_variante = True
            sous_resultats = []
            for idx, (debut, fin) in enumerate(variante):
                cibles = salle_unique if idx == 0 else liste_ordonnee
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


def traiter_journee(aff_page, nav, mail_page, jour, mode_rattrapage, pool_emails, liens_deja_valides):
    num_jour = jour.weekday()
    if num_jour >= 5:
        return [], [], []

    jour_txt = jour.isoformat()
    maintenant = datetime.now(PARIS)

    if mode_rattrapage:
        blocs_du_jour = BLOCS_RATTRAPAGE_MERCREDI if num_jour == 2 else BLOCS_RATTRAPAGE_SOIR
    else:
        blocs_du_jour = PLANNING.get(num_jour, [])

    minutes_par_email = {e: 0 for e in pool_emails}
    bilan_succes, bilan_deja_fait, bilan_echecs = [], [], []

    log(f"================ {jour_txt} ({'RATTRAPAGE' if mode_rattrapage else 'NORMAL'}) ================")

    try:
        aff_page.goto(
            f"https://affluences.com/fr/sites/{SITE_SLUG}/reservation?type={TYPE_ID}&date={jour_txt}",
            wait_until="networkidle",
            timeout=25000,
        )
        salles_dispos = trouver_salles(aff_page, jour_txt)
    except Exception as e:
        log(f"⚠️ Impossible de charger les salles pour le {jour_txt} : {e}")
        return [], [], [f"{jour_txt} (erreur réseau)"]

    if not salles_dispos:
        log(f"Aucune salle ouverte à la réservation le {jour_txt} (BU fermée ou jour férié).")
        return [], [], []

    candidates = ordre_salles(salles_dispos)
    salle_favorite_jour = None

    for variantes in blocs_du_jour:
        # Si c'est aujourd'hui, on filtre les variantes dont l'heure de fin est déjà passée
        variantes_valides = []
        for v in variantes:
            if jour == maintenant.date():
                heure_actuelle = f"{maintenant:%H:%M}"
                v_filtree = [(d, f) for (d, f) in v if d > heure_actuelle]
                if v_filtree:
                    variantes_valides.append(v_filtree)
            else:
                variantes_valides.append(v)

        if not variantes_valides:
            continue

        desc_cible = " + ".join(f"{d}-{f}" for d, f in variantes_valides[0])
        res_bloc = reserver_bloc_intelligent(
            aff_page, nav, mail_page, jour_txt, variantes_valides, candidates,
            pool_emails, minutes_par_email, liens_deja_valides, salle_favorite_jour
        )

        if not res_bloc:
            bilan_echecs.append(f"{jour_txt} ({desc_cible})")
        else:
            for debut, fin, salle, statut in res_bloc:
                if salle and not salle_favorite_jour:
                    salle_favorite_jour = salle
                if statut == "OK":
                    bilan_succes.append(f"✅ {jour_txt} : {salle} ({debut}-{fin})")
                elif statut == "MAIL_KO":
                    bilan_echecs.append(f"⚠️ {jour_txt} : {salle} ({debut}-{fin} mail non validé)")
                elif statut == "DEJA_FAIT":
                    bilan_deja_fait.append(f"☑️ {jour_txt} : Déjà actif ({debut}-{fin})")

    return bilan_succes, bilan_deja_fait, bilan_echecs


def main():
    dates_a_traiter, mode_rattrapage = determiner_dates_et_mode()
    pool_emails = generer_pool_emails(RAW_EMAIL)

    total_succes = []
    total_deja_fait = []
    total_echecs = []
    liens_deja_valides = set()

    with sync_playwright() as p:
        nav = p.chromium.launch()
        aff_page = nav.new_page(locale="fr-FR")
        mail_page = nav.new_page(locale="fr-FR")

        connecter_webmail(mail_page)

        for jour in dates_a_traiter:
            s, d, e = traiter_journee(
                aff_page, nav, mail_page, jour, mode_rattrapage, pool_emails, liens_deja_valides
            )
            total_succes.extend(s)
            total_deja_fait.extend(d)
            total_echecs.extend(e)

        nav.close()

    # ---------- Bilan final ----------
    if total_succes:
        msg = "🎉 Réservations BU confirmées :\n" + "\n".join(total_succes)
        if total_deja_fait:
            msg += "\n\n" + "\n".join(total_deja_fait)
        if total_echecs and not mode_rattrapage:
            msg += "\n\n❌ Créneaux complets :\n" + "\n".join(total_echecs)
        log(msg)
        telegram(msg)
    elif total_deja_fait and not total_echecs:
        log("Toutes les réservations ciblées étaient déjà actives.")
    else:
        msg = "❌ Aucune nouvelle salle réservée :\n" + "\n".join(total_echecs[:15])
        log(msg)
        if not mode_rattrapage:
            telegram(msg)
            sys.exit(1)


if __name__ == "__main__":
    main()
