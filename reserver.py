"""
Réservation automatique Affluences - BU Joseph-Fourier & Validation Webmail UGA
Multi-créneaux (Midi + Après-midi/Soir) avec découpage intelligent et priorités de salles.
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
NB_PERSONNES = int(os.environ.get("NB_PERSONNES", "2"))

# ---------- Planning par jour (0 = Lundi ... 4 = Vendredi) ----------
# Pour chaque créneau, on fournit une liste de variantes [Prioritaire, Découpage si max 3h, Repli horaire]
PLANNING = {
    0: [  # Lundi : 11h30-14h30 et 17h00-21h00
        [[("11:30", "14:30")], [("11:30", "13:30"), ("13:30", "14:30")]],
        [
            [("17:00", "21:00")],
            [("17:00", "19:00"), ("19:00", "21:00")],
            [("16:30", "19:00"), ("19:00", "21:00")],
            [("17:00", "19:00"), ("19:00", "20:00")],
        ],
    ],
    1: [  # Mardi : 11h30-14h30 et 17h00-21h00
        [[("11:30", "14:30")], [("11:30", "13:30"), ("13:30", "14:30")]],
        [
            [("17:00", "21:00")],
            [("17:00", "19:00"), ("19:00", "21:00")],
            [("16:30", "19:00"), ("19:00", "21:00")],
            [("17:00", "19:00"), ("19:00", "20:00")],
        ],
    ],
    2: [  # Mercredi : 11h30-14h30 + 13h30-21h00 (enchaîné sans chevauchement de 11h30 à 21h00)
        [[("11:30", "14:30")], [("11:30", "13:30")]],
        [
            [("14:30", "17:30")],
            [("13:30", "16:30")],
            [("14:30", "17:00")],
        ],
        [
            [("17:30", "21:00")],
            [("17:30", "19:30"), ("19:30", "21:00")],
            [("16:30", "19:00"), ("19:00", "21:00")],
            [("17:00", "19:00"), ("19:00", "21:00")],
        ],
    ],
    3: [  # Jeudi : 11h30-14h30 et 17h00-21h00
        [[("11:30", "14:30")], [("11:30", "13:30"), ("13:30", "14:30")]],
        [
            [("17:00", "21:00")],
            [("17:00", "19:00"), ("19:00", "21:00")],
            [("16:30", "19:00"), ("19:00", "21:00")],
            [("17:00", "19:00"), ("19:00", "20:00")],
        ],
    ],
    4: [  # Vendredi : 11h30-14h30 et 17h00-21h00
        [[("11:30", "14:30")], [("11:30", "13:30"), ("13:30", "14:30")]],
        [
            [("17:00", "21:00")],
            [("17:00", "19:00"), ("19:00", "21:00")],
            [("16:30", "19:00"), ("19:00", "21:00")],
            [("17:00", "19:00"), ("19:00", "20:00")],
            [("17:00", "19:00")],
        ],
    ],
}

# ---------- Secrets ----------
EMAIL = os.environ["AFF_EMAIL"].strip()
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


def normaliser(txt):
    txt = unicodedata.normalize("NFKD", txt).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", txt.lower())


def date_cible():
    d = datetime.now(PARIS).date() + timedelta(days=JOURS_AVANCE)
    if len(sys.argv) > 1 and sys.argv[1].strip():
        d = datetime.strptime(sys.argv[1].strip(), "%Y-%m-%d").date()
    return d


def api(page, method, url, body=None):
    """Appel API depuis la page affluences.com."""
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
    """
    Ordre de priorité :
    1. RDC Sud n°4
    2. Autres salles RDC Sud
    3. Salles Nord (Étage Nord, RDC Nord, etc.)
    4. N'importe quelle autre salle disponible
    """
    ordre = []
    # 1. Salle prioritaire (RDC Sud n°4)
    for voulu in SALLES:
        ordre += [s for s in salles if normaliser(s[0]) == normaliser(voulu) and s not in ordre]
    # 2. Autres salles RDC Sud
    ordre += [
        s for s in salles
        if normaliser(SECOURS_SUD) in normaliser(s[0]) and s not in ordre
    ]
    # 3. Salles Nord
    ordre += [
        s for s in salles
        if normaliser(SECOURS_NORD) in normaliser(s[0]) and s not in ordre
    ]
    # 4. Toutes les autres salles en dernier recours
    ordre += [s for s in salles if s not in ordre]
    return ordre


def est_deja_reserve_par_utilisateur(txt_err):
    """Détecte si l'utilisateur a déjà une réservation active sur ce créneau."""
    txt = txt_err.lower()
    motifs_perso = (
        "vous avez déjà",
        "vous avez deja",
        "already have",
        "user already",
        "email already",
        "quota",
        "limite de réservation",
        "limite de reservation",
        "maximum de réservations",
        "maximum de reservations",
    )
    return any(m in txt for m in motifs_perso)


def connecter_webmail(mail_page):
    """Ouvre et connecte la session Webmail UGA une seule fois."""
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
        mail_page.wait_for_timeout(8000)

        try:
            mail_page.reload(wait_until="domcontentloaded")
            mail_page.wait_for_timeout(3500)
        except Exception:
            pass

        mails = mail_page.get_by_text("Confirmez votre réservation : BU Joseph-Fourier")
        nb_mails = mails.count()
        if nb_mails == 0:
            continue

        # On parcourt les mails trouvés (du plus récent au plus ancien) pour trouver un lien inédit
        lien_trouve = None
        for idx in range(min(nb_mails, 4)):
            try:
                mails.nth(idx).click()
                mail_page.wait_for_timeout(3000)
            except Exception:
                continue

            for cadre in mail_page.frames:
                btns = cadre.locator("a", has_text="Confirmer ma réservation")
                # On inspecte du dernier au premier au cas où Zimbra affiche une conversation groupée
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
            log(f"Nouveau lien de confirmation extrait ! Validation en cours...")
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


def reserver_sous_creneau(aff_page, nav, mail_page, jour_txt, debut, fin, candidates, liens_deja_valides, salle_preferee=None):
    """
    Tente de réserver (debut -> fin) en privilégiant la salle_preferee (pour garder la même salle
    sur deux blocs consécutifs), puis en suivant l'ordre RDC Sud n°4 -> RDC Sud -> Nord -> Autres.
    """
    liste_essai = list(candidates)
    if salle_preferee:
        liste_essai.sort(key=lambda s: 0 if s[0] == salle_preferee else 1)

    for nom, rid in liste_essai:
        r = api(aff_page, "POST", f"https://reservation.affluences.com/api/reserve/{rid}", {
            "email": EMAIL,
            "date": jour_txt,
            "start_time": debut,
            "end_time": fin,
            "person_count": NB_PERSONNES,
            "note": "",
        })
        log(f"[{debut}-{fin}] {nom} (id {rid}) -> {r['status']} {r['text'][:180]}")

        if r["status"] in (200, 201):
            log(f"✅ Réservation posée : {nom} ({debut}-{fin}). Validation mail...")
            ok_mail = confirmer_mail(nav, mail_page, liens_deja_valides)
            return ("OK" if ok_mail else "MAIL_KO", nom)

        if est_deja_reserve_par_utilisateur(r["text"]):
            log(f"ℹ️ Créneau {debut}-{fin} déjà réservé par ton compte.")
            return ("DEJA_FAIT", nom)

    return ("ECHEC", None)


def reserver_bloc_intelligent(aff_page, nav, mail_page, jour_txt, variantes, candidates, liens_deja_valides):
    """
    Pour un grand bloc horaire (ex: 17h-21h) :
    Teste d'abord sur la meilleure salle (RDC Sud n°4) le créneau complet (17h-21h),
    et si Affluences refuse la durée de 4h, découpe immédiatement en 2x2h (17h-19h + 19h-21h)
    sur cette MÊME salle prioritaire avant de passer aux salles de secours !
    """
    resultats = []

    # 1. On essaie d'abord de garder la même salle de bout en bout (salle par salle dans l'ordre de priorité)
    for nom_salle, rid_salle in candidates:
        salle_unique = [(nom_salle, rid_salle)]
        for variante in variantes:
            succes_variante = True
            sous_resultats = []
            for idx, (debut, fin) in enumerate(variante):
                # Pour le 1er sous-créneau, on exige cette salle; pour le 2e, on privilégie cette salle puis les secours
                cibles = salle_unique if idx == 0 else candidates
                statut, salle_retenue = reserver_sous_creneau(
                    aff_page, nav, mail_page, jour_txt, debut, fin, cibles, liens_deja_valides, salle_preferee=nom_salle
                )
                if statut in ("OK", "MAIL_KO", "DEJA_FAIT"):
                    sous_resultats.append((debut, fin, salle_retenue, statut))
                else:
                    succes_variante = False
                    break

            if succes_variante and sous_resultats:
                return sous_resultats
            elif sous_resultats:
                # Si la 1re moitié de la variante a réussi sur cette salle mais que la 2e moitié a échoué partout
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
    log(f"=== Objectif pour le {jour_txt} (jour {num_jour}) : {len(blocs_du_jour)} grands créneaux à couvrir ===")

    bilan_succes = []
    bilan_deja_fait = []
    bilan_echecs = []
    liens_deja_valides = set()

    with sync_playwright() as p:
        nav = p.chromium.launch()
        aff_page = nav.new_page(locale="fr-FR")
        mail_page = nav.new_page(locale="fr-FR")

        # Connexion initiale au webmail UGA pour être prêt à valider instantanément
        connecter_webmail(mail_page)

        # Ouverture de la page Affluences
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
                aff_page, nav, mail_page, jour_txt, variantes, candidates, liens_deja_valides
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
