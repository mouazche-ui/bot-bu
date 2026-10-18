"""
Robot Facteur UGA -> Gmail (Avec repli DNS et anti-crash réseau)
"""
import base64
import email
import os
import smtplib
import sys
import time
from datetime import datetime
from email.header import decode_header, make_header
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


def ouvrir_webmail(page):
    """Tente d'ouvrir le webmail UGA avec plusieurs essais et URL de secours."""
    for essai in range(1, 4):
        for url in URLS_WEBMAIL:
            try:
                log(f"Tentative de connexion à {url} (essai {essai}/3)...")
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                return True
            except Exception as e:
                log(f"Avertissement ({url}) : {e}")
                time.sleep(3)
    return False


def main():
    if not all([UGA_USER, UGA_PASS, GMAIL_ADDRESS, GMAIL_APP_PASSWORD]):
        log("❌ ERREUR : Un ou plusieurs secrets sont vides.")
        sys.exit(1)

    with sync_playwright() as p:
        nav = p.chromium.launch(args=["--dns-prefetch-disable"])
        page = nav.new_page(locale="fr-FR")

        if not ouvrir_webmail(page):
            log("⚠️ Serveur UGA temporairement injoignable (coupure DNS/maintenance). Nouvel essai dans 5 min.")
            nav.close()
            return  # On quitte sans erreur (exit 0) pour éviter les spams d'échec GitHub

        page.fill("#username", UGA_USER)
        page.fill("#password", UGA_PASS)
        page.click("#submitBtn")

        page.wait_for_load_state("networkidle", timeout=30000)
        page.wait_for_timeout(5000)

        recherche = page.evaluate("""async () => {
            const r = await fetch('/service/soap/SearchRequest', {
                method: 'POST',
                headers: {'Content-Type': 'application/soap+xml; charset=utf-8'},
                body: JSON.stringify({
                    Header: { context: { _jsns: "urn:zimbra", format: { type: "js" } } },
                    Body: {
                        SearchRequest: {
                            _jsns: "urn:zimbraMail",
                            types: "message",
                            limit: 25,
                            query: "in:inbox is:unread"
                        }
                    }
                })
            });
            const text = await r.text();
            try {
                return { ok: r.ok, status: r.status, data: JSON.parse(text) };
            } catch (e) {
                return { ok: false, status: r.status, raw: text.slice(0, 300) };
            }
        }""")

        if not recherche.get("ok"):
            log(f"⚠️ Réponse inattendue de Zimbra ({recherche.get('status')}). Réessai au prochain passage.")
            nav.close()
            return

        messages = recherche["data"].get("Body", {}).get("SearchResponse", {}).get("m", [])
        if not messages:
            log("Aucun nouveau mail non lu. Tout est à jour !")
            nav.close()
            return

        log(f"{len(messages)} nouveau(x) mail(s) non lu(s) détecté(s) !")

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)

            for m in reversed(messages):
                msg_id = m["id"]
                sujet_apercu = m.get("su", "(Sans objet)")

                if "Confirmez votre réservation : BU Joseph-Fourier" in sujet_apercu:
                    log(f"Mail Affluences ignoré : {sujet_apercu}")
                    continue

                dl = page.evaluate("""async (id) => {
                    const r = await fetch(`/service/home/~/?auth=co&id=${id}`);
                    const buf = await r.arrayBuffer();
                    const bytes = new Uint8Array(buf);
                    let bin = '';
                    const chunk = 8192;
                    for (let i = 0; i < bytes.length; i += chunk) {
                        bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
                    }
                    return btoa(bin);
                }""", msg_id)

                raw_eml = base64.b64decode(dl)
                eml_pret, sujet, exp = preparer_eml_pour_gmail(raw_eml)

                smtp.sendmail(GMAIL_ADDRESS, [GMAIL_ADDRESS], eml_pret)
                log(f"✅ Transféré vers Gmail : {sujet} (de {exp})")

                page.evaluate("""async (id) => {
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
                }""", msg_id)

        nav.close()
        log("🎉 Tous les nouveaux mails ont été transférés sur ton Gmail !")


if __name__ == "__main__":
    main()
