"""
Robot Facteur UGA -> Gmail (Version Corrigée)
Transfère les mails UGA vers Gmail car l'IMAP est désactivé.
"""
import base64
from datetime import datetime
import email
from email.header import decode_header, make_header
import os
import smtplib
import sys
import time
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

UGA_USER = os.environ.get("UGA_USER", "").strip()
UGA_PASS = os.environ.get("UGA_PASS", "").strip()
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "").strip()
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()

PARIS = ZoneInfo("Europe/Paris")

URLS_WEBMAIL = [
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

def marquer_comme_lu(page, msg_id):
    try:
        page.evaluate(
            """async (id) => {
                await fetch('/service/soap/MsgActionRequest', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/soap+xml; charset=utf-8'},
                    body: JSON.stringify({
                        Header: { context: { _jsns: "urn:zimbra", format: { type: "js" } } },
                        Body: {
                            MsgActionRequest: {
                                _jsns: "urn:zimbraMail",
                                action: { id: String(id), op: "read" }
                            }
                        }
                    })
                });
            }""",
            msg_id,
        )
    except Exception as e:
        log(f"⚠️ Erreur marquage lu pour {msg_id} : {e}")

def executer_facteur():
    if not all([UGA_USER, UGA_PASS, GMAIL_ADDRESS, GMAIL_APP_PASSWORD]):
        log("⚠️ Secrets manquants (UGA_USER, UGA_PASS, GMAIL_ADDRESS ou GMAIL_APP_PASSWORD).")
        return

    with sync_playwright() as p:
        # Launch optimisé pour GitHub Actions
        nav = p.chromium.launch(args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"])
        page = nav.new_page(locale="fr-FR")

        connecte = False
        for url in URLS_WEBMAIL:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                connecte = True
                break
            except Exception as e:
                log(f"Avertissement ({url}) : {e}")

        if not connecte:
            log("⚠️ Serveurs UGA injoignables.")
            nav.close()
            return

        # Connexion Agalan / Webmail
        try:
            page.fill("#username", UGA_USER)
            page.fill("#password", UGA_PASS)
            page.click("#submitBtn")
            page.wait_for_load_state("networkidle", timeout=30000)
            page.wait_for_timeout(3000)
        except Exception as e:
            log(f"❌ Erreur lors de la connexion : {e}")
            nav.close()
            return

        # Recherche des mails non lus
        recherche = page.evaluate(
            """async () => {
                try {
                    const r = await fetch('/service/soap/SearchRequest', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/soap+xml; charset=utf-8'},
                        body: JSON.stringify({
                            Header: { context: { _jsns: "urn:zimbra", format: { type: "js" } } },
                            Body: {
                                SearchRequest: {
                                    _jsns: "urn:zimbraMail",
                                    types: "message",
                                    limit: 50,
                                    query: "in:inbox is:unread"
                                }
                            }
                        })
                    });
                    const text = await r.text();
                    return { ok: r.ok, status: r.status, data: JSON.parse(text) };
                } catch (e) {
                    return { ok: false, error: e.message };
                }
            }"""
        )

        if not recherche.get("ok"):
            log(f"⚠️ Erreur recherche Zimbra : {recherche.get('error') or recherche.get('status')}")
            nav.close()
            return

        messages = recherche["data"].get("Body", {}).get("SearchResponse", {}).get("m", [])
        if not messages:
            log("✅ Aucun nouveau mail. Tout est à jour !")
            nav.close()
            return

        log(f"📩 {len(messages)} nouveau(x) mail(s) détecté(s) !")
        
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as smtp:
                smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)

                for m in reversed(messages):
                    msg_id = m["id"]
                    sujet_apercu = m.get("su", "(Sans objet)")
                    
                    # --- CORRECTION FILTRE ANTI-SPAM ---
                    # On ne filtre QUE les mails de crash GitHub Actions, pas tout ce qui contient "bot-bu"
                    if sujet_apercu.startswith("Run failed:") or "github.com/mouazche-ui/bot-bu/actions/runs" in sujet_apercu:
                        log(f"🗑️ Notification crash GitHub ignorée : {sujet_apercu}")
                        marquer_comme_lu(page, msg_id)
                        continue
                    # ----------------------------------

                    try:
                        dl = page.evaluate(
                            """async (id) => {
                                const r = await fetch(`/service/home/~/?auth=co&id=${id}`);
                                if (!r.ok) throw new Error('HTTP ' + r.status);
                                const buf = await r.arrayBuffer();
                                return btoa(String.fromCharCode(...new Uint8Array(buf)));
                            }""",
                            msg_id,
                        )
                        
                        raw_eml = base64.b64decode(dl)
                        eml_pret, sujet, exp = preparer_eml_pour_gmail(raw_eml)

                        smtp.sendmail(GMAIL_ADDRESS, [GMAIL_ADDRESS], eml_pret)
                        log(f"✅ Transféré : {sujet} (de {exp})")
                        marquer_comme_lu(page, msg_id)
                    except Exception as e:
                        log(f"⚠️ Échec transfert mail {msg_id} : {e}")

        except Exception as e:
            log(f"❌ Erreur SMTP Gmail : {e}")
        
        nav.close()
        log("🎉 Terminé !")

if __name__ == "__main__":
    try:
        executer_facteur()
    except Exception as err:
        log(f"⚠️ Erreur fatale : {err}")
    sys.exit(0)
