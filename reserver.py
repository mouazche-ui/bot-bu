"""
Réservation automatique Affluences - BU Joseph-Fourier & Validation Multi-Comptes UGA
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import email
import imaplib
import os
import re
import sys
import time
import unicodedata
import urllib.parse
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

# ---------- Planning normal à J+14 (11h30 à 21h00 non-stop) ----------
PLANNING_CONTINU = [
    # Bloc 1 : Midi (11h30 - 14h30) -> Compte 1
    [[("11:30", "14:30")], [("11:30", "13:30"), ("13:30", "14:30")]],
    # Bloc 2 : Après-midi (14h30 - 17h30) -> Compte 2
    [[("14:30", "17:30")], [("14:30", "16:30"), ("16:30", "17:30")]],
    # Bloc 3 : Soirée (17h30 - 21h00) -> Compte 3 (découpé en 2 sous-créneaux ≤3h)
    [
        [("17:30", "19:30"), ("19:30", "21:00")],
        [("17:30", "19:00"), ("19:00", "21:00")],
        [("17:00", "19:00"), ("19:00", "21:00")],
    ],
]

PLANNING = {
    0: PLANNING_CONTINU,  # Lundi
    1: PLANNING_CONTINU,  # Mardi
    2: PLANNING_CONTINU,  # Mercredi
    3: PLANNING_CONTINU,  # Jeudi
    4: PLANNING_CONTINU,  # Vendredi
}

# ---------- Blocs du mode RATTRAPAGE (11h30 à 21h00) ----------
BLOCS_RATTRAPAGE = PLANNING_CONTINU

# ---------- Secrets ----------
RAW_EMAIL = os.environ.get("AFF_EMAIL", "").strip()
UGA_USERS = [u.strip() for u in os.environ.get("UGA_USER", "").split(",") if u.strip()]
UGA_PASSES = [p.strip() for p in os.environ.get("UGA_PASS", "").split(",") if p.strip()]
TG_TOKEN = os.environ.get("TG_TOKEN", "").strip()
TG_CHAT = os.environ.get("TG_CHAT", "").strip()

PARIS = ZoneInfo("Europe/Paris")

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
    """Génère un pool en alternant les 3 comptes principaux puis leurs alias."""
    bases = [e.strip() for e in raw_email.split(",") if e.strip()]
    pool = []

    # 1. Injection des comptes principaux dans l'ordre
    for b in bases:
        if b not in pool:
            pool.append(b)

    # 2. Injection des alias (+1 sur C1, C2, C3 puis +2 sur C1, C2, C3...)
    for i in range(1, 6):
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
    if slots_libres is None:
        return True
    if not slots_libres:
        return False
    m_deb = heure_en_minutes(debut)
    m_fin = heure_en_minutes(fin)
    pas = 15 if any(m % 30 != 0 for m in slots_libres) else 30
    cur = m_deb
    while cur < m_fin:
        if cur not in slots_libres:
            return False
        cur += pas
    return True


def ordre_salles(data_salles):
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
    jour_txt, debut, fin, candidates, pool_emails, minutes_par_email
):
    duree = duree_minutes(debut, fin)
    liste_salles = list(candidates)

    for nom, rid, slots_libres in liste_salles:
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

            break

    return ("ECHEC", None)


def reserver_bloc_rapide(jour_txt, variantes, candidates, pool_emails, minutes_par_email):
    resultats = []
    liste_ordonnee = list(candidates)

    for nom_salle, rid_salle, slots_salle in liste_ordonnee:
        salle_unique = [(nom_salle, rid_salle, slots_salle)]
        for variante in variantes:
            d0, f0 = variante[0]
            if not creneau_est_probablement_libre(slots_salle, d0, f0):
                continue

            succes_variante = True
            sous_resultats = []
            for idx, (debut, fin) in enumerate(variante):
                cibles = salle_unique if idx == 0 else liste_ordonnee
                statut, salle_retenue = reserver_sous_creneau_rapide(
                    jour_txt, debut, fin, cibles, pool_emails, minutes_par_email
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


def valider_lien_affluences(url):
    try:
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

        r_get = HTTP.get(url, timeout=8, allow_redirects=True)
        if r_get.status_code == 200 and ("<app-root" in r_get.text or "ng-version" in r_get.text or "noscript" in r_get.text.lower()):
            return False
        return r_get.status_code in (200, 201, 204)
    except Exception:
        return False


def extraire_liens_d_un_compte(user, password, nb_attendus):
    """Connexion IMAP sur UN compte spécifique pour extraire tous les liens de confirmation."""
    conn = None
    for host in IMAP_HOSTS:
        try:
            conn = imaplib.IMAP4_SSL(host, 993, timeout=5)
            conn.login(user, password)
            log(f"⚡ Connecté en IMAP direct sur {user} !")
            break
        except Exception:
            conn = None

    if not conn:
        log(f"⚠️ Échec connexion IMAP pour {user}")
        return []

    liens_trouves = []
    try:
        conn.select("INBOX")
        typ, data = conn.search(None, '(UNSEEN)')
        ids = data[0].split() if (typ == "OK" and data and data[0]) else []
        if not ids:
            typ, data = conn.search(None, 'ALL')
            ids = data[0].split()[-20:] if (typ == "OK" and data and data[0]) else []

        for num in reversed(ids[-25:]):
            typ, msg_data = conn.fetch(num, "(BODY.PEEK[])")
            if typ != "OK" or not msg_data:
                continue
            raw_bytes = msg_data[0][1]
            msg = email.message_from_bytes(raw_bytes)

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

            for m in re.finditer(r'https?://[^\s"\'<>]+affluences\.com[^\s"\'<>]+', corps):
                u_clean = m.group(0).replace("&amp;", "&")
                if any(k in u_clean.lower() for k in ("confirm", "valid", "token", "reserv")):
                    liens_trouves.append(u_clean)
                    conn.store(num, "+FLAGS", "\\Seen")

            if len(liens_trouves) >= nb_attendus:
                break
    except Exception as e:
        log(f"⚠️ Erreur IMAP {user} : {e}")
    finally:
        try:
            conn.logout()
        except Exception:
            pass

    return liens_trouves


def valider_lot_mails(nb_attendus):
    if nb_attendus <= 0:
        return 0

    pairs = list(zip(UGA_USERS, UGA_PASSES))
    if not pairs:
        log("⚠️ Aucun identifiant UGA renseigné dans UGA_USER/UGA_PASS.")
        return 0

    log(f"⚡ Validation groupée de {nb_attendus} réservation(s) sur {len(pairs)} compte(s)...")

    liens_globaux = []
    for user, pwd in pairs:
        liens = extraire_liens_d_un_compte(user, pwd, nb_attendus)
        for l in liens:
            if l not in liens_globaux:
                liens_globaux.append(l)

    if not liens_globaux:
        log("⚠️ Aucun lien de confirmation trouvé dans les boîtes IMAP.")
        return 0

    log(f"⚡ {len(liens_globaux)} lien(s) de confirmation extrait(s) au total ! Activation...")
    nb_ok = 0
    liens_js = []
    for l in liens_globaux:
        if valider_lien_affluences(l):
            nb_ok += 1
        else:
            liens_js.append(l)

    if liens_js:
        from playwright.sync_api import sync_playwright
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


def main():
    t_start = time.time()
    dates_a_traiter, mode_rattrapage = determiner_dates_et_mode()
    pool_emails = generer_pool_emails(RAW_EMAIL)
    maintenant = datetime.now(PARIS)

    log(f"🚀 Démarrage Turbo ({'RATTRAPAGE' if mode_rattrapage else 'NORMAL'}) sur {len(dates_a_traiter)} jour(s)...")

    jours_str = [d.isoformat() for d in dates_a_traiter if d.weekday() < 5]
    with ThreadPoolExecutor(max_workers=min(10, max(1, len(jours_str)))) as ex:
        resultats_fetch = dict(ex.map(fetch_salles_jour, jours_str))

    log(f"⚡ Disponibilités des {len(jours_str)} jour(s) chargées en {time.time() - t_start:.2f}s !")

    reservations_en_attente = []
    total_deja_fait = []
    total_echecs = []

    for jour in dates_a_traiter:
        num_jour = jour.weekday()
        if num_jour >= 5:
            continue
        jour_txt = jour.isoformat()
        data_salles = resultats_fetch.get(jour_txt, [])
        if not data_salles:
            continue

        candidates = ordre_salles(data_salles)
        blocs_du_jour = BLOCS_RATTRAPAGE if mode_rattrapage else PLANNING.get(num_jour, [])
        minutes_par_email = {e: 0 for e in pool_emails}

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
                pool_emails, minutes_par_email
            )

            if not res_bloc:
                total_echecs.append(f"{jour_txt} ({desc_cible})")
            else:
                for debut, fin, salle, statut in res_bloc:
                    if statut == "ATTENTE_MAIL":
                        reservations_en_attente.append(f"✅ {jour_txt} : {salle} ({debut}-{fin})")
                    elif statut == "DEJA_FAIT":
                        total_deja_fait.append(f"☑️ {jour_txt} : Déjà actif ({debut}-{fin})")

    nb_a_valider = len(reservations_en_attente)
    nb_confirmes = valider_lot_mails(nb_a_valider) if nb_a_valider > 0 else 0
    duree_totale = time.time() - t_start

    log(f"🏁 Terminé en {duree_totale:.1f}s ! ({nb_confirmes}/{nb_a_valider} nouvelle(s) réservation(s) confirmée(s))")

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
