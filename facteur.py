"""
Robot Facteur UGA -> Gmail (Ultra-rapide 100% API Zimbra / Zéro navigateur)
- S'exécute en < 1 seconde (contre 60s+ avec Playwright).
- Laisse 4 minutes à reserver.py pour valider un nouveau mail Affluences, puis le transfère
  automatiquement sur Gmail pour que tu aies la preuve de réservation sur ton téléphone !
"""
import email
from email.header import decode_header, make_header
import os
import re
import smtplib
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

UGA_USER = os.environ.get("UGA_USER", "").strip()
UGA_PASS = os.environ.get("UGA_PASS", "").strip()
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "").strip()
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()

PARIS = ZoneInfo("Europe/Paris")

URLS_ZIMBRA = [
    "https://webmail.etu.univ-grenoble-alpes.fr",
    "https://zimbra.univ-grenoble-alpes.fr",
]


def log(msg):
    print(f"[{datetime.now(PARIS):%H:%M:%S}] {msg}", flush=True)


def decoder_entete(val):
    if not val:
        return ""
    try:
        return str(make_header(decode_header(val)))
    except Exception:
        return val


def preparer_eml_pour_gmail(raw_bytes):
    msg = email.message_from_bytes(raw_bytes)
    exp_original = decoder_entete(msg.get("From", "Inconnu"))
    sujet_original = decoder_entete(msg.get("Subject", "(Sans objet)"))

    if "Reply-To" not in msg and msg.get("From"):
        msg["Reply-To"] = msg["From"]

    for h in ("From", "To", "Subject", "DKIM-Signature", "DomainKey-Signature", "Return-Path"):
        if h in msg:
            del msg[h]

    msg["From"] = f"UGA Mail ({exp_original}) <{GMAIL_ADDRESS}>"
    msg["To"] = GMAIL_ADDRESS
    msg["Subject"] = f"[UGA] {sujet_original}"

    return msg.as_bytes(), sujet_original, exp_original


def connecter_zimbra_http():
    """
    Authentifie une session HTTP directe sur Zimbra UGA (en < 0.3s, sans Playwright).
    """
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    })

    for base_url in URLS_ZIMBRA:
        try:
            # 1. Connexion via l'API SOAP AuthRequest de Zimbra
            soap_auth = {
                "Header": {"context": {"_jsns": "urn:zimbra", "format": {"type": "js"}}},
                "Body": {
                    "AuthRequest": {
                        "_jsns": "urn:zimbraAccount",
                        "account": {"by": "name", "_content": UGA_USER},
                        "password": {"_content": UGA_PASS},
                    }
                },
            }
            r = s.post(
                f"{base_url}/service/soap/AuthRequest",
                json=soap_auth,
                timeout=12,
            )
            if r.status_code == 200 and "AuthResponse" in r.text:
                token = r.json()["Body"]["AuthResponse"]["authToken"][0]["_content"]
                s.cookies.set("ZM_AUTH_TOKEN", token)
                log(f"⚡ Connecté à l'API Zimbra ({base_url}) en direct !")
                return s, base_url, token

            # 2. Fallback formulaire HTML standard Zimbra si SOAP Auth direct désactivé
            r_get = s.get(base_url, timeout=12)
            m_csrf = re.search(r'name="login_csrf"\s+value="([^"]+)"', r_get.text)
            payload = {
                "loginOp": "login",
                "username": UGA_USER,
                "password": UGA_PASS,
                "client": "preferred",
            }
            if m_csrf:
                payload["login_csrf"] = m_csrf.group(1)
            s.post(base_url + "/", data=payload, timeout=12)
            if "ZM_AUTH_TOKEN" in s.cookies:
                log(f"⚡ Connecté à Zimbra par formulaire ({base_url}) !")
                return s, base_url, s.cookies.get("ZM_AUTH_TOKEN")
        except Exception as e:
            log(f"Avertissement connexion ({base_url}) : {e}")

    return None, None, None


def main():
    if not all([UGA_USER, UGA_PASS, GMAIL_ADDRESS, GMAIL_APP_PASSWORD]):
        log("❌ ERREUR : Un ou plusieurs secrets sont vides.")
        sys.exit(0)  # Exit 0 pour ne jamais déclencher d'alerte mail d'erreur GitHub

    session, base_url, token = connecter_zimbra_http()
    if not session:
        log("⚠️ Serveur UGA temporairement injoignable. Nouvel essai au prochain passage.")
        return

    # Recherche des mails non lus dans la boîte de réception
    soap_search = {
        "Header": {
            "context": {
                "_jsns": "urn:zimbra",
                "authToken": {"_content": token},
                "format": {"type": "js"},
            }
        },
        "Body": {
            "SearchRequest": {
                "_jsns": "urn:zimbraMail",
                "types": "message",
                "limit": 30,
                "query": "in:inbox is:unread",
            }
        },
    }

    try:
        r_search = session.post(f"{base_url}/service/soap/SearchRequest", json=soap_search, timeout=12)
        data = r_search.json()
        messages = data.get("Body", {}).get("SearchResponse", {}).get("m", [])
    except Exception as e:
        log(f"⚠️ Erreur lors de la lecture des messages : {e}")
        return

    if not messages:
        log("Aucun nouveau mail non lu. Tout est à jour !")
        return

    log(f"{len(messages)} mail(s) non lu(s) détecté(s) !")
    now_ms = int(time.time() * 1000)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as smtp:
            smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)

            for m in reversed(messages):
                msg_id = m["id"]
                sujet_apercu = m.get("su", "(Sans objet)")
                date_mail_ms = int(m.get("d", now_ms))
                age_minutes = (now_ms - date_mail_ms) / 60000.0

                # Si c'est un mail de confirmation Affluences reçu il y a moins de 4 minutes,
                # on le laisse non lu au cas où reserver.py est en train de tourner.
                # Au-delà de 4 minutes, on le transfère sur Gmail pour que tu aies ta preuve sur téléphone !
                if "Confirmez votre réservation : BU Joseph-Fourier" in sujet_apercu and age_minutes < 4.0:
                    log(f"⏳ Mail Affluences récent ({age_minutes:.1f} min) laissé pour reserver.py : {sujet_apercu}")
                    continue

                # Téléchargement direct du fichier .eml brut
                r_dl = session.get(f"{base_url}/service/home/~/?auth=co&id={msg_id}", timeout=15)
                if r_dl.status_code != 200 or not r_dl.content:
                    continue

                eml_pret, sujet, exp = preparer_eml_pour_gmail(r_dl.content)
                smtp.sendmail(GMAIL_ADDRESS, [GMAIL_ADDRESS], eml_pret)
                log(f"✅ Transféré vers Gmail : {sujet} (de {exp})")

                # Marque le message comme lu dans Zimbra
                soap_read = {
                    "Header": {
                        "context": {
                            "_jsns": "urn:zimbra",
                            "authToken": {"_content": token},
                            "format": {"type": "js"},
                        }
                    },
                    "Body": {
                        "MsgActionRequest": {
                            "_jsns": "urn:zimbraMail",
                            "action": {"id": String_id := str(msg_id), "op": "read"},
                        }
                    },
                }
                session.post(f"{base_url}/service/soap/MsgActionRequest", json=soap_read, timeout=10)
    except Exception as e:
        log(f"⚠️ Erreur transfert SMTP : {e}")
        return

    log("🎉 Tous les nouveaux mails ont été transférés sur ton Gmail !")


if __name__ == "__main__":
    main()
