"""
Réservation automatique Affluences - BU Joseph-Fourier
Validation 100% automatique via Playwright (Webmail UGA)
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import os
import re
import sys
import time
import unicodedata
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright

# ---------- Réglages ----------
SITE_SLUG = "bu-joseph-fourier"
SITE_ID = "888a64d4-3c8d-4ebf-83a5-d9441b5431b9"
TYPE_ID = "5249"
SALLES = ["RDC Sud n°4"]
SECOURS_SUD = "RDC Sud"
SECOURS_NORD = "Nord"
JOURS_AVANCE = 14
QUOTA_MAX_MINUTES = 180
NB_PERSONNES = int(os.environ.get("NB_PERSONNES", "2"))

PLANNING_CONTINU = [
    [[("11:30", "14:30")], [("11:30", "13:30"), ("13:30", "14:30")]],
    [[("14:30", "17:30")], [("14:30", "16:30"), ("16:30", "17:30")]],
    [[("17:30", "19:30"), ("19:30", "21:00")], [("17:30", "19:00"), ("19:00", "21:00")], [("17:00", "19:00"), ("19:00", "21:00")]],
]
PLANNING = {i: PLANNING_CONTINU for i in range(5)}
BLOCS_RATTRAPAGE = PLANNING_CONTINU

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

def log(msg): print(f"[{datetime.now(PARIS):%H:%M:%S.%f}"[:-3] + f"] {msg}", flush=True)

def telegram(msg):
    if not (TG_TOKEN and TG_CHAT): return
    try: HTTP.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data={"chat_id": TG_CHAT, "text": msg}, timeout=8)
    except Exception as e: log(f"Telegram KO : {e}")

def generer_pool_emails(raw_email):
    bases = [e.strip() for e in raw_email.split(",") if e.strip()]
    pool = []
    for b in bases:
        if b not in pool: pool.append(b)
    for i in range(1, 6):
        for b in bases:
            if "@" in b:
                local, dom = b.split("@", 1)
                local_pur = local.split("+")[0]
                alias = f"{local_pur}+{i}@{dom}"
                if alias not in pool: pool.append(alias)
    return pool

def heure_en_minutes(h_str):
    h, m = map(int, h_str.split(":"))
    return h * 60 + m

def duree_minutes(debut, fin): return heure_en_minutes(fin) - heure_en_minutes(debut)

def normaliser(txt):
    txt = unicodedata.normalize("NFKD", txt).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", txt.lower())

def determiner_dates_et_mode():
    auj = datetime.now(PARIS).date()
    arg = sys.argv[1].strip().lower() if len(sys.argv) > 1 else ""
    if arg in ("rattrapage", "all", "tout", "full"):
        dates = [auj + timedelta(days=delta) for delta in range(0, JOURS_AVANCE + 1) if (auj + timedelta(days=delta)).weekday() < 5]
        return dates, True
    if arg: return [datetime.strptime(arg, "%Y-%m-%d").date()], False
    return [auj + timedelta(days=JOURS_AVANCE)], False

def fetch_salles_jour(jour_txt):
    url = f"https://reservation.affluences.com/api/resources/{SITE_ID}/available?date={jour_txt}&type={TYPE_ID}"
    try:
        r = HTTP.get(url, timeout=10)
        if r.status_code != 200: return jour_txt, []
        data = r.json()
        if isinstance(data, dict): data = data.get("data", data.get("resources", []))
        return jour_txt, data
    except Exception: return jour_txt, []

def extraire_slots_libres(ressource_json):
    hours = ressource_json.get("hours")
    if not isinstance(hours, list) or not hours: return None
    libres = set()
    for slot in hours:
        if not isinstance(slot, dict): continue
        h_str = slot.get("hour") or slot.get("start_time") or slot.get("time")
        if not h_str or ":" not in str(h_str): continue
        state, dispo = slot.get("state"), slot.get("available")
        places = slot.get("places_available", slot.get("remaining_places", 1))
        if (state is None or state == 1 or state == "available") and (dispo is None or dispo is True) and (places is None or places > 0):
            try: libres.add(heure_en_minutes(str(h_str)[:5]))
            except Exception: pass
    return libres if libres else set()

def creneau_est_probablement_libre(slots_libres, debut, fin):
    if slots_libres is None: return True
    if not slots_libres: return False
    m_deb, m_fin = heure_en_minutes(debut), heure_en_minutes(fin)
    pas = 15 if any(m % 30 != 0 for m in slots_libres) else 30
    cur = m_deb
    while cur < m_fin:
        if cur not in slots_libres: return False
        cur += pas
    return True

def ordre_salles(data_salles):
    salles = [(s.get("resource_name", ""), s.get("resource_id"), extraire_slots_libres(s)) for s in data_salles]
    ordre = []
    for voulu in SALLES: ordre += [s for s in salles if normaliser(s[0]) == normaliser(voulu) and s not in ordre]
    ordre += [s for s in salles if normaliser(SECOURS_SUD) in normaliser(s[0]) and s not in ordre]
    ordre += [s for s in salles if normaliser(SECOURS_NORD) in normaliser(s[0]) and s not in ordre]
    ordre += [s for s in salles if s not in ordre]
    return ordre

def est_deja_reserve_sur_ce_creneau(txt_err): return any(m in txt_err.lower() for m in ("already_have_reservation", "déjà une réservation en cours sur ce créneau"))
def est_quota_depasse(txt_err): return any(m in txt_err.lower() for m in ("quota_overpassed", "quota journalier", "atteint votre quota", "limite journalière"))
def est_creneau_invalide(txt_err): return "invalid_timeslot" in txt_err.lower() or "créneau non valide" in txt_err.lower()

def reserver_sous_creneau_rapide(jour_txt, debut, fin, candidates, pool_emails, minutes_par_email):
    duree = duree_minutes(debut, fin)
    for nom, rid, slots_libres in list(candidates):
        if not creneau_est_probablement_libre(slots_libres, debut, fin): continue
        for email_courant in pool_emails:
            if minutes_par_email.get(email_courant, 0) + duree > QUOTA_MAX_MINUTES: continue
            try:
                r = HTTP.post(f"https://reservation.affluences.com/api/reserve/{rid}",
                              json={"email": email_courant, "date": jour_txt, "start_time": debut, "end_time": fin, "person_count": NB_PERSONNES, "note": ""},
                              timeout=8)
                status, txt = r.status_code, r.text
            except Exception as e:
                log(f"⚠️ Erreur POST {nom} : {e}"); break
            log(f"[{jour_txt} {debut}-{fin}] {nom} via {email_courant} -> {status} {txt[:120]}")
            if status in (200, 201):
                minutes_par_email[email_courant] = minutes_par_email.get(email_courant, 0) + duree
                if slots_libres is not None:
                    for m in range(heure_en_minutes(debut), heure_en_minutes(fin), 15): slots_libres.discard(m)
                return ("ATTENTE_MAIL", nom)
            if est_deja_reserve_sur_ce_creneau(txt): return ("DEJA_FAIT", nom)
            if est_quota_depasse(txt): minutes_par_email[email_courant] = QUOTA_MAX_MINUTES; continue
            if est_creneau_invalide(txt): return ("HORAIRE_INVALIDE", None)
            break
    return ("ECHEC", None)

def reserver_bloc_rapide(jour_txt, variantes, candidates, pool_emails, minutes_par_email):
    resultats = []
    liste_ordonnee = list(candidates)
    for nom_salle, rid_salle, slots_salle in liste_ordonnee:
        salle_unique = [(nom_salle, rid_salle, slots_salle)]
        for variante in variantes:
            d0, f0 = variante[0]
            if not creneau_est_probablement_libre(slots_salle, d0, f0): continue
            succes_variante, sous_resultats = True, []
            for idx, (debut, fin) in enumerate(variante):
                cibles = salle_unique if idx == 0 else liste_ordonnee
                statut, salle_retenue = reserver_sous_creneau_rapide(jour_txt, debut, fin, cibles, pool_emails, minutes_par_email)
                if statut in ("ATTENTE_MAIL", "DEJA_FAIT"): sous_resultats.append((debut, fin, salle_retenue, statut))
                else: succes_variante = False; break
            if succes_variante and sous_resultats: return sous_resultats
            elif sous_resultats: resultats.extend(sous_resultats); return resultats
    return resultats

# ============================================================
# LA PARTIE MAGIQUE : PLAYWRIGHT FAIT TOUT TOUT SEUL
# ============================================================
def valider_tous_les_mails(nb_attendus):
    if nb_attendus <= 0: return 0
    pairs = list(zip(UGA_USERS, UGA_PASSES))
    if not pairs: return 0

    log(f"🤖 Le robot s'éveille pour valider {nb_attendus} réservation(s)...")
    liens_valides = 0

    with sync_playwright() as p:
        # On lance un navigateur (mets headless=False si tu veux le voir faire en local)
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(locale="fr-FR")
        page = context.new_page()

        for user, pwd in pairs:
            log(f"🔗 Connexion au webmail UGA pour {user}...")
            try:
                # 1. On va sur le webmail UGA (qui redirige vers le SSO)
                page.goto("https://webmail.etu.univ-grenoble-alpes.fr/", wait_until="networkidle", timeout=30000)
                
                # 2. Remplissage du formulaire de login (Shibboleth / Microsoft)
                # On cherche les champs de saisie
                login_input = page.locator('input[name="username"], input[name="j_username"], input[type="email"], input[id="userNameInput"]').first
                if login_input.count() > 0:
                    login_input.fill(user)
                    # Bouton suivant
                    page.locator('button[type="submit"], input[type="submit"], #nextButton').first.click()
                    page.wait_for_timeout(2000)
                
                # Mot de passe
                pass_input = page.locator('input[name="password"], input[name="j_password"], input[type="password"], input[id="passwordInput"]').first
                if pass_input.count() > 0:
                    pass_input.fill(pwd)
                    page.locator('button[type="submit"], input[type="submit"], #submitButton').first.click()
                    page.wait_for_load_state("networkidle", timeout=30000)
                    page.wait_for_timeout(4000) # On laisse le temps à la boîte mail de charger

                # 3. On cherche le mail d'Affluences
                log(f"📩 Recherche du mail Affluences pour {user}...")
                
                # Sur Outlook / Zimbra, on peut chercher dans la page
                # On cherche le texte "Affluences" et on clique dessus
                mail_element = page.locator('text=Affluences').first
                if mail_element.count() > 0:
                    mail_element.click()
                    page.wait_for_timeout(2000) # Attente ouverture du mail

                    # 4. On trouve le lien de confirmation dans le mail
                    # On récupère tous les liens de la page
                    liens = page.eval_on_selector_all('a[href]', 'elements => elements.map(e => e.href)')
                    
                    for lien in liens:
                        if "affluences.com" in lien and any(k in lien.lower() for k in ("confirm", "valid", "token")):
                            log(f"✅ Lien de confirmation trouvé ! Validation...")
                            # On ouvre le lien pour valider
                            page.goto(lien, wait_until="networkidle", timeout=20000)
                            liens_valides += 1
                            break # On a validé pour ce compte, on passe au suivant
                else:
                    log(f"⚠️ Aucun mail Affluences trouvé pour {user}.")
                    
            except Exception as e:
                log(f"⚠️ Erreur robot pour {user} : {e}")
            
            # On se déconnecte pour passer au compte suivant
            try:
                page.goto("https://webmail.etu.univ-grenoble-alpes.fr/", wait_until="domcontentloaded")
                page.context.clear_cookies()
            except: pass

        browser.close()
    
    log(f"🏁 Robot terminé. {liens_valides} mail(s) validé(s).")
    return liens_valides

# ============================================================
# MAIN
# ============================================================
def main():
    t_start = time.time()
    dates_a_traiter, mode_rattrapage = determiner_dates_et_mode()
    pool_emails = generer_pool_emails(RAW_EMAIL)
    maintenant = datetime.now(PARIS)

    log(f"🚀 Démarrage ({'RATTRAPAGE' if mode_rattrapage else 'NORMAL'})...")
    jours_str = [d.isoformat() for d in dates_a_traiter if d.weekday() < 5]
    with ThreadPoolExecutor(max_workers=min(10, max(1, len(jours_str)))) as ex:
        resultats_fetch = dict(ex.map(fetch_salles_jour, jours_str))

    reservations_en_attente = []
    total_deja_fait = []

    for jour in dates_a_traiter:
        if jour.weekday() >= 5: continue
        jour_txt = jour.isoformat()
        data_salles = resultats_fetch.get(jour_txt, [])
        if not data_salles: continue

        candidates = ordre_salles(data_salles)
        blocs_du_jour = BLOCS_RATTRAPAGE if mode_rattrapage else PLANNING.get(jour.weekday(), [])
        minutes_par_email = {e: 0 for e in pool_emails}

        for variantes in blocs_du_jour:
            variantes_valides = []
            for v in variantes:
                if jour == maintenant.date():
                    heure_actuelle = f"{maintenant:%H:%M}"
                    v_filtree = [(d, f) for (d, f) in v if d > heure_actuelle]
                    if v_filtree: variantes_valides.append(v_filtree)
                else: variantes_valides.append(v)

            if not variantes_valides: continue
            res_bloc = reserver_bloc_rapide(jour_txt, variantes_valides, candidates, pool_emails, minutes_par_email)

            if res_bloc:
                for debut, fin, salle, statut in res_bloc:
                    if statut == "ATTENTE_MAIL": reservations_en_attente.append(f"✅ {jour_txt} : {salle} ({debut}-{fin})")
                    elif statut == "DEJA_FAIT": total_deja_fait.append(f"☑️ {jour_txt} : Déjà actif ({debut}-{fin})")

    # --- ÉTAPE FINALE : LE ROBOT VALIDE TOUT ---
    nb_a_valider = len(reservations_en_attente)
    nb_confirmes = valider_tous_les_mails(nb_a_valider) if nb_a_valider > 0 else 0

    duree_totale = time.time() - t_start
    log(f"🏁 Terminé en {duree_totale:.1f}s ! ({nb_confirmes}/{nb_a_valider} confirmée(s))")

    if reservations_en_attente:
        msg = f"⚡ Réservations BU ({nb_confirmes}/{nb_a_valider} validées) :\n" + "\n".join(reservations_en_attente)
        if total_deja_fait: msg += "\n\n" + "\n".join(total_deja_fait)
        telegram(msg)

if __name__ == "__main__":
    main()
