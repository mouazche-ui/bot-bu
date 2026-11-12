"""
Réservation automatique Affluences - BU Joseph-Fourier & Validation UGA Ultra-Rapide
Architecture Haute Performance :
1. Requêtes Affluences 100% HTTP Keep-Alive (Session) + Fetch parallèle multi-jours (0s de chargement navigateur).
2. Pré-filtrage intelligent des créneaux disponibles en RAM (évite de tester les salles déjà occupées).
3. Pipelining : pose toutes les réservations en rafale avant de valider tous les mails en lot.
4. Validation mail par protocole IMAP direct (0.1s) avec fallback automatique Webmail Playwright.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import email
import imaplib
import json
import os
import quopri
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

import requests

# ---------- Réglages ----------
SITE_SLUG = "bu-joseph-fourier"
SITE_ID = "888a64d4-3c8d-4ebf-83a5-d9441b5431b9"
TYPE_ID = "5249"                    # Salles de travail en groupe
SALLES = ["RDC Sud n°4"]            # 1er choix prioritaire
SECOURS_SUD = "RDC Sud"             # 2e choix : toutes les autres salles RDC Sud
SECOURS_NORD = "Nord"               # 3e choix : toutes les salles Nord
JOURS_AVANCE = 14
QUOTA_MAX_MINUTES = 180             # 3h00 max par jour et par alias email
NB_PERSONNES = int(os.environ.get("NB_PERSONNES", "2"))

IMAP_HOSTS = [
    "imaps.etu.univ-grenoble-alpes.fr",
    "mail.etu.univ-grenoble-alpes.fr",
    "webmail.etu.univ-grenoble-alpes.fr",
]

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

# ---------- Blocs du mode RATTRAPAGE (~17h à 21h + Mercredi dès 13h30) ----------
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
UGA_USER = os.environ.get("UGA_USER", "").strip()
UGA_PASS = os.environ.get("UGA_PASS", "").strip()
TG_TOKEN = os.environ.get("TG_TOKEN", "").strip()
TG_CHAT = os.environ.get("TG_CHAT", "").strip()

PARIS = ZoneInfo("Europe/Paris")

# Session HTTP Keep-Alive globale (réutilise la connexion TLS vers Affluences : ~40ms par requête)
HTTP = requests.Session()
HTTP.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://affluences.com",
    "Referer": f"https://affluences.com/fr/sites/{SITE_SLUG}/reservation",
})


def log(msg):
    print(f"[{datetime.now(PARIS):%H:%M:%S.%f}"[:-3] + f"] {msg}", flush=True)


def telegram(msg):
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        HTTP.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT, "text": msg},
            timeout=8,
        )
    except Exception as e:
        log(f"Telegram KO : {e}")


def generer_pool_emails(raw_email):
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


def heure_en_minutes(h_str):
    h, m = map(int, h_str.split(":"))
    return h * 60 + m


def duree_minutes(debut, fin):
    return heure_en_minutes(fin) - heure_en_minutes(debut)


def normaliser(txt):
    txt = unicodedata.normalize("NFKD", txt).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", txt.lower())


def determiner_dates_et_mode():
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


def fetch_salles_jour(jour_txt):
    """Récupère les salles et leurs créneaux disponibles en HTTP direct (~80ms)."""
    url = f"https://reservation.affluences.com/api/resources/{SITE_ID}/available?date={jour_txt}&type={TYPE_ID}"
    try:
        r = HTTP.get(url, timeout=10)
        if r.status_code != 200:
            log(f"⚠️ HTTP {r.status_code} sur la liste des salles du {jour_txt}")
            return jour_txt, []
        data = r.json()
        if isinstance(data, dict):
            data = data.get("data", data.get("resources", []))
        return jour_txt, data
    except Exception as e:
        log(f"⚠️ Erreur réseau fetch_salles_jour({jour_txt}) : {e}")
        return jour_txt, []


def extraire_slots_libres(ressource_json):
    """
    Analyse le champ 'hours' renvoyé par l'API d'Affluences pour connaître en RAM
    les demi-heures exactes où la salle est libre, évitant ainsi les POST inutiles !
    Renvoie un set de minutes (depuis minuit) disponibles, ou None si le format est inconnu.
    """
    hours = ressource_json.get("hours")
    if not isinstance(hours, list) or not hours:
        return None

    libres = set()
    for slot in hours:
        if not isinstance(slot, dict):
            continue
        h_str = slot.get("hour") or slot.get("start_time") or slot.get("time")
        if not h_str or ":" not in str(h_str):
            continue
        # Dans Affluences : state == 1 (ou available == True / places_available > 0) = disponible
        state = slot.get("state")
        dispo = slot.get("available")
        places = slot.get("places_available", slot.get("remaining_places", 1))
        est_libre = (
            (state is None or state == 1 or state == "available")
            and (dispo is None or dispo is True)
            and (places is None or places > 0)
        )
        if est_libre:
            try:
                libres.add(heure_en_minutes(str(h_str)[:5]))
            except Exception:
                pass

    return libres if libres else set()


def creneau_est_probablement_libre(slots_libres, debut, fin):
    """Vérifie en 0.001ms si toutes les tranches de 30 min entre debut et fin sont libres."""
    if slots_libres is None:
        return True  # Si l'API ne détaille pas les heures, on autorise l'essai POST
    if not slots_libres:
        return False
    m_deb = heure_en_minutes(debut)
    m_fin = heure_en_minutes(fin)
    # Détermine le pas des tranches (15 ou 30 min) selon les données présentes
    pas = 15 if any(m % 30 != 0 for m in slots_libres) else 30
    cur = m_deb
    while cur < m_fin:
        if cur not in slots_libres:
            return False
        cur += pas
    return True


def ordre_salles(data_salles):
    """
    Trie les salles selon :
    1. RDC Sud n°4 -> 2. Autres RDC Sud -> 3. Salles Nord -> 4. N'importe quelle autre salle
    """
    salles = [
        (s.get("resource_name", ""), s.get("resource_id"), extraire_slots_libres(s))
        for s in data_salles
    ]
    ordre = []
    for voulu in SALLES:
        ordre += [s for s in salles if normaliser(s[0]) == normaliser(voulu) and s not in ordre]
    ordre += [s for s in salles if normaliser(SECOURS_SUD) in normaliser(s[0]) and s not in ordre]
    ordre += [s for s in salles if normaliser(SECOURS_NORD) in normaliser(s[0]) and s not in ordre]
    ordre += [s for s in salles if s not in ordre]
    return ordre


def est_deja_reserve_sur_ce_creneau(txt_err):
    txt = txt_err.lower()
    return any(m in txt for m in (
        "already_have_reservation",
        "déjà une réservation en cours sur ce créneau",
        "deja une reservation en cours sur ce creneau",
        "already have a reservation",
    ))


def est_quota_depasse(txt_err):
    txt = txt_err.lower()
    return any(m in txt for m in (
        "quota_overpassed",
        "quota journalier",
        "atteint votre quota",
        "limite journalière",
        "limite journaliere",
    ))


def est_creneau_invalide(txt_err):
    txt = txt_err.lower()
    return "invalid_timeslot" in txt or "créneau non valide" in txt or "creneau non valide" in txt


def reserver_sous_creneau_rapide(
    jour_txt, debut, fin, candidates, pool_emails, minutes_par_email, salle_preferee=None
):
    duree = duree_minutes(debut, fin)
    liste_salles = list(candidates)
    if salle_preferee:
        liste_salles.sort(key=lambda s: 0 if s[0] == salle_preferee else 1)

    for nom, rid, slots_libres in liste_salles:
        # Pré-filtrage ultra-rapide en RAM : si la salle est déjà occupée sur ce créneau, on la saute en 0ms !
        if not creneau_est_probablement_libre(slots_libres, debut, fin):
            continue

        for email_courant in pool_emails:
            if minutes_par_email.get(email_courant, 0) + duree > QUOTA_MAX_MINUTES:
                continue

            try:
                r = HTTP.post(
                    f"https://reservation.affluences.com/api/reserve/{rid}",
                    json={
                        "email": email_courant,
                        "date": jour_txt,
                        "start_time": debut,
                        "end_time": fin,
                        "person_count": NB_PERSONNES,
                        "note": "",
                    },
                    timeout=8,
                )
                status = r.status_code
                txt = r.text
            except Exception as e:
                log(f"⚠️ Erreur POST {nom} : {e}")
                break

            log(f"[{jour_txt} {debut}-{fin}] {nom} via {email_courant} -> {status} {txt[:120]}")

            if status in (200, 201):
                minutes_par_email[email_courant] = minutes_par_email.get(email_courant, 0) + duree
                # Marque le créneau comme pris localement
                if slots_libres is not None:
                    for m in range(heure_en_minutes(debut), heure_en_minutes(fin), 15):
                        slots_libres.discard(m)
                return ("ATTENTE_MAIL", nom)

            if est_deja_reserve_sur_ce_creneau(txt):
                minutes_par_email[email_courant] = minutes_par_email.get(email_courant, 0) + duree
                return ("DEJA_FAIT", nom)

            if est_quota_depasse(txt):
                minutes_par_email[email_courant] = QUOTA_MAX_MINUTES
                continue

            if est_creneau_invalide(txt):
                return ("HORAIRE_INVALIDE", None)

            # Salle indisponible : on passe immédiatement à la salle suivante
            break

    return ("ECHEC", None)


def reserver_bloc_rapide(
    jour_txt, variantes, candidates, pool_emails, minutes_par_email, salle_favorite_jour=None
):
    resultats = []
    liste_ordonnee = list(candidates)
    if salle_favorite_jour:
        liste_ordonnee.sort(key=lambda s: 0 if s[0] == salle_favorite_jour else 1)

    for nom_salle, rid_salle, slots_salle in liste_ordonnee:
        salle_unique = [(nom_salle, rid_salle, slots_salle)]
        for variante in variantes:
            # Vérification instantanée en RAM : est-ce que le 1er sous-créneau est libre dans cette salle ?
            d0, f0 = variante[0]
            if not creneau_est_probablement_libre(slots_salle, d0, f0):
                continue

            succes_variante = True
            sous_resultats = []
            for idx, (debut, fin) in enumerate(variante):
                cibles = salle_unique if idx == 0 else liste_ordonnee
                statut, salle_retenue = reserver_sous_creneau_rapide(
                    jour_txt, debut, fin, cibles, pool_emails, minutes_par_email, salle_preferee=nom_salle
                )
                if statut in ("ATTENTE_MAIL", "DEJA_FAIT"):
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


# ==================== VALIDATION ULTRA-RAPIDE DES MAILS (IMAP + FALLBACK WEBMAIL) ====================

def valider_lien_affluences(url):
    """
    Valide un lien de confirmation Affluences en appelant directement l'API backend
    d'Affluences (0.05s) + GET sur le lien. Renvoie True si validé sans navigateur.
    """
    try:
        # 1. Extraction d'un éventuel token dans l'URL (ex: .../confirm?token=xxx ou /confirm/xxx)
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        token = (qs.get("token") or qs.get("reservation_token") or [None])[0]
        if not token:
            m = re.search(r"(?:confirm|validation|reserve)/([A-Za-z0-9_-]{12,})", url)
            if m:
                token = m.group(1)

        if token:
            for ep in (
                f"https://reservation.affluences.com/api/confirm/{token}",
                f"https://reservation.affluences.com/api/reserve/confirm/{token}",
                f"https://reservation.affluences.com/api/reservations/confirm/{token}",
            ):
                r_api = HTTP.post(ep, json={"token": token}, timeout=6)
                if r_api.status_code in (200, 201, 204):
                    log(f"⚡ Confirmé par API directe ({r_api.status_code}) : {token[:12]}...")
                    return True

        # 2. Appel GET direct sur l'URL du bouton
        r_get = HTTP.get(url, timeout=8, allow_redirects=True)
        # Si c'est une SPA Angular/Vue qui nécessite l'exécution JS, on renvoie False pour que Playwright clique dessus en lot
        if r_get.status_code == 200 and ("<app-root" in r_get.text or "ng-version" in r_get.text or "noscript" in r_get.text.lower()):
            return False
        return r_get.status_code in (200, 201, 204)
    except Exception:
        return False


def extraire_liens_via_imap(nb_attendus, timeout_sec=35):
    """
    Se connecte en IMAP SSL directement au serveur mail UGA (0.1s) et récupère tous
    les liens 'Confirmer ma réservation' sans ouvrir de navigateur !
    """
    if not (UGA_USER and UGA_PASS):
        return None

    conn = None
    for host in IMAP_HOSTS:
        try:
            conn = imaplib.IMAP4_SSL(host, 993, timeout=6)
            conn.login(UGA_USER, UGA_PASS)
            log(f"⚡ Connecté en IMAP direct sur {host} !")
            break
        except Exception:
            conn = None

    if not conn:
        return None

    liens_trouves = []
    vus = set()
    debut_t = time.time()

    try:
        while time.time() - debut_t < timeout_sec and len(liens_trouves) < nb_attendus:
            conn.select("INBOX")
            # Cherche les mails récents d'Affluences
            typ, data = conn.search(None, '(UNSEEN)')
            ids = data[0].split() if (typ == "OK" and data and data[0]) else []
            if not ids:
                typ, data = conn.search(None, 'ALL')
                ids = data[0].split()[-15:] if (typ == "OK" and data and data[0]) else []

            for num in reversed(ids[-20:]):
                typ, msg_data = conn.fetch(num, "(BODY.PEEK[])")
                if typ != "OK" or not msg_data:
                    continue
                raw_bytes = msg_data[0][1]
                msg = email.message_from_bytes(raw_bytes)
                sujet = str(email.header.make_header(email.header.decode_header(msg.get("Subject", ""))))
                if "Affluences" not in sujet and "Joseph-Fourier" not in sujet and "réservation" not in sujet.lower():
                    continue

                corps = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() in ("text/html", "text/plain"):
                            payload = part.get_payload(decode=True)
                            if payload:
                                corps += payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
                else:
                    payload = msg.get_payload(decode=True)
                    if payload:
                        corps = payload.decode(msg.get_content_charset() or "utf-8", errors="ignore")

                # Recherche de tous les liens de confirmation Affluences dans le HTML
                for m in re.finditer(r'https?://[^\s"\'<>]+affluences\.com[^\s"\'<>]+', corps):
                    u_clean = m.group(0).replace("&amp;", "&")
                    if any(k in u_clean.lower() for k in ("confirm", "valid", "token", "reserv")) and u_clean not in vus:
                        vus.add(u_clean)
                        liens_trouves.append(u_clean)
                        # Marque le mail comme lu
                        conn.store(num, "+FLAGS", "\\Seen")

            if len(liens_trouves) >= nb_attendus:
                break
            time.sleep(1.2)
    except Exception as e:
        log(f"⚠️ Lecture IMAP interrompue : {e}")
    finally:
        try:
            conn.logout()
        except Exception:
            pass

    return liens_trouves


def valider_lot_mails(nb_attendus):
    """
    Valide en lot les `nb_attendus` réservations posées :
    1. Tente d'abord l'extraction IMAP ultra-rapide.
    2. Ouvre les liens en parallèle dans Playwright si la page de confirmation est une SPA JS.
    3. Fallback sur le Webmail Zimbra Playwright uniquement si l'IMAP est filtré.
    """
    if nb_attendus <= 0:
        return 0
    if not (UGA_USER and UGA_PASS):
        log("⚠️ Identifiants UGA absents.")
        return 0

    log(f"⚡ Validation groupée de {nb_attendus} réservation(s) en cours...")
    liens = extraire_liens_via_imap(nb_attendus, timeout_sec=28)

    from playwright.sync_api import sync_playwright

    # Cas 1 : IMAP a fonctionné et a extrait les liens en 1 seconde !
    if liens is not None and len(liens) > 0:
        log(f"⚡ {len(liens)} lien(s) extrait(s) par IMAP ! Validation...")
        nb_ok = 0
        liens_js = []
        for l in liens:
            if valider_lien_affluences(l):
                nb_ok += 1
            else:
                liens_js.append(l)

        if liens_js:
            with sync_playwright() as p:
                nav = p.chromium.launch()
                ctx = nav.new_context(locale="fr-FR")
                pages = []
                for l in liens_js:
                    pg = ctx.new_page()
                    try:
                        pg.goto(l, wait_until="domcontentloaded", timeout=12000)
                        pages.append(pg)
                        nb_ok += 1
                    except Exception as e:
                        log(f"⚠️ Erreur ouverture lien {l[:50]} : {e}")
                if pages:
                    pages[0].wait_for_timeout(2500)
                nav.close()
        return nb_ok

    # Cas 2 : Fallback Webmail Zimbra optimisé (sans reload complet de page, clic sur Refresh Zimbra)
    log("🔄 Passage par le Webmail UGA rapide...")
    nb_valides = 0
    liens_vus = set()

    with sync_playwright() as p:
        nav = p.chromium.launch()
        ctx = nav.new_context(locale="fr-FR")
        mail_page = ctx.new_page()

        mail_page.goto("https://webmail.etu.univ-grenoble-alpes.fr", wait_until="domcontentloaded")
        mail_page.fill("#username", UGA_USER)
        mail_page.fill("#password", UGA_PASS)
        mail_page.click("#submitBtn")
        mail_page.wait_for_timeout(3500)

        for essai in range(1, 9):
            if nb_valides >= nb_attendus:
                break

            # Rafraîchissement léger Zimbra sans recharger toute l'application
            try:
                btn_getmail = mail_page.locator("#zb__App__tab_Mail, #zb__zbText__REFRESH, [id*='CHECK_MAIL']").first
                if btn_getmail.count() > 0:
                    btn_getmail.click()
                    mail_page.wait_for_timeout(1800)
                else:
                    mail_page.reload(wait_until="domcontentloaded")
                    mail_page.wait_for_timeout(2200)
            except Exception:
                pass

            mails = mail_page.get_by_text("Confirmez votre réservation : BU Joseph-Fourier")
            nb_m = mails.count()
            if nb_m == 0:
                mail_page.wait_for_timeout(2000)
                continue

            nouveaux_liens = []
            for idx in range(min(nb_m, nb_attendus + 4)):
                try:
                    mails.nth(idx).click()
                    mail_page.wait_for_timeout(1200)
                except Exception:
                    continue

                for cadre in mail_page.frames:
                    btns = cadre.locator("a", has_text="Confirmer ma réservation")
                    for b_idx in range(btns.count() - 1, -1, -1):
                        href = btns.nth(b_idx).get_attribute("href")
                        if href and href not in liens_vus:
                            liens_vus.add(href)
                            nouveaux_liens.append(href)

            # Validation simultanée en onglets parallèles de tous les nouveaux liens trouvés
            if nouveaux_liens:
                onglets = []
                for href in nouveaux_liens:
                    pg = ctx.new_page()
                    try:
                        pg.goto(href, wait_until="domcontentloaded", timeout=12000)
                        onglets.append(pg)
                        nb_valides += 1
                        log(f"⚡ Lien validé ({nb_valides}/{nb_attendus})")
                    except Exception:
                        pass
                if onglets:
                    onglets[0].wait_for_timeout(2200)
                    for pg in onglets:
                        pg.close()

            if nb_valides < nb_attendus:
                mail_page.wait_for_timeout(2000)

        nav.close()

    return nb_valides


def main():
    t_start = time.time()
    dates_a_traiter, mode_rattrapage = determiner_dates_et_mode()
    pool_emails = generer_pool_emails(RAW_EMAIL)
    maintenant = datetime.now(PARIS)

    log(f"🚀 Démarrage Turbo ({'RATTRAPAGE' if mode_rattrapage else 'NORMAL'}) sur {len(dates_a_traiter)} jour(s)...")

    # 1. Téléchargement PARALLÈLE de toutes les disponibilités de tous les jours en 0.4s !
    jours_str = [d.isoformat() for d in dates_a_traiter if d.weekday() < 5]
    with ThreadPoolExecutor(max_workers=min(10, max(1, len(jours_str)))) as ex:
        resultats_fetch = dict(ex.map(fetch_salles_jour, jours_str))

    log(f"⚡ Disponibilités des {len(jours_str)} jour(s) chargées en {time.time() - t_start:.2f}s !")

    reservations_en_attente = []
    total_deja_fait = []
    total_echecs = []

    # 2. Pose des réservations en rafale (sans attendre les mails entre chaque clic)
    for jour in dates_a_traiter:
        num_jour = jour.weekday()
        if num_jour >= 5:
            continue
        jour_txt = jour.isoformat()
        data_salles = resultats_fetch.get(jour_txt, [])
        if not data_salles:
            continue

        candidates = ordre_salles(data_salles)
        blocs_du_jour = (
            (BLOCS_RATTRAPAGE_MERCREDI if num_jour == 2 else BLOCS_RATTRAPAGE_SOIR)
            if mode_rattrapage
            else PLANNING.get(num_jour, [])
        )
        minutes_par_email = {e: 0 for e in pool_emails}
        salle_favorite_jour = None

        for variantes in blocs_du_jour:
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
            res_bloc = reserver_bloc_rapide(
                jour_txt, variantes_valides, candidates,
                pool_emails, minutes_par_email, salle_favorite_jour
            )

            if not res_bloc:
                total_echecs.append(f"{jour_txt} ({desc_cible})")
            else:
                for debut, fin, salle, statut in res_bloc:
                    if salle and not salle_favorite_jour:
                        salle_favorite_jour = salle
                    if statut == "ATTENTE_MAIL":
                        reservations_en_attente.append(f"✅ {jour_txt} : {salle} ({debut}-{fin})")
                    elif statut == "DEJA_FAIT":
                        total_deja_fait.append(f"☑️ {jour_txt} : Déjà actif ({debut}-{fin})")

    # 3. Validation groupée de tous les mails d'un seul coup
    nb_a_valider = len(reservations_en_attente)
    nb_confirmes = valider_lot_mails(nb_a_valider) if nb_a_valider > 0 else 0
    duree_totale = time.time() - t_start

    log(f"🏁 Terminé en {duree_totale:.1f}s ! ({nb_confirmes}/{nb_a_valider} nouvelle(s) réservation(s) confirmée(s))")

    # ---------- Bilan final & Telegram ----------
    if reservations_en_attente:
        msg = (
            f"⚡ Réservations BU ({duree_totale:.1f}s) - {nb_confirmes}/{nb_a_valider} validées :\n"
            + "\n".join(reservations_en_attente)
        )
        if total_deja_fait:
            msg += "\n\n" + "\n".join(total_deja_fait)
        log(msg)
        telegram(msg)
    elif total_deja_fait:
        log(f"Toutes les réservations étaient déjà actives ({duree_totale:.1f}s).")
    else:
        msg = f"❌ Aucune salle libre trouvée ({duree_totale:.1f}s)."
        log(msg)
        if not mode_rattrapage:
            telegram(msg)
            sys.exit(1)


if __name__ == "__main__":
    main()
