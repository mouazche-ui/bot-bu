"""
Robot Facteur UGA -> Gmail (Blindé : Zéro mail d'erreur GitHub + Anti-boucle Run failed)
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


def executer_facteur():
    if not all([UGA_USER, UGA_PASS, GMAIL_ADDRESS, GMAIL_APP_PASSWORD]):
        log("⚠️ Un ou plusieurs secrets sont vides.")
        return

    with sync_playwright() as p:
        nav = p.chromium.launch(args=["--dns-prefetch-disable", "--no-sandbox"])
        page = nav.new_page(locale="fr-FR")

        connecte = False
        for url in URLS_WEBMAIL:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
                connecte = True
                break
            except Exception as e:
                log(f"Avertissement ({url}) : {e}")

        if not connecte:
            log("⚠️ Serveur UGA temporairement injoignable.")
            nav.close()
            return

        page.fill("#username", UGA_USER)
        page.fill("#password", UGA_PASS)
        page.click("#submitBtn")
        page.wait_for_load_state("domcontentloaded", timeout=20000)
        page.wait_for_timeout(3000)

        recherche = page.evaluate(
            """async () => {
                const r = await fetch('/service/soap/SearchRequest', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/soap+xml; charset=utf-8'},
                    body: JSON.stringify({
                        Header: { context: { _jsns: "urn:zimbra", format: { type: "js" } } },
                        Body: {
                            SearchRequest: {
                                _jsns: "urn:zimbraMail",
                                types: "message",
                                limit: 30,
                                query: "in:inbox is:unread"
                            }
                        }
                    })
                });
                const text = await r.text();
                try {
                    return { ok: r.ok, status: r.status, data: JSON.parse(text) };
                } catch (e) {
                    return { ok: false, status: r.status };
                }
            }"""
        )

        if not recherche.get("ok"):
            log(f"⚠️ Réponse inattendue de Zimbra ({recherche.get('status')}).")
            nav.close()
            return

        messages = recherche["data"].get("Body", {}).get("SearchResponse", {}).get("m", [])
        if not messages:
            log("Aucun nouveau mail non lu. Tout est à jour !")
            nav.close()
            return

        log(f"{len(messages)} nouveau(x) mail(s) non lu(s) détecté(s) !")
        now_ms = int(time.time() * 1000)

        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as smtp:
            smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)

            for m in reversed(messages):
                msg_id = m["id"]
                sujet_apercu = m.get("su", "(Sans objet)")
                date_mail_ms = int(m.get("d", now_ms))
                age_minutes = (now_ms - date_mail_ms) / 60000.0

                # 1. ANTI-SPAM GITHUB : Si un vieux mail "Run failed" traîne dans Zimbra, on le marque lu sans le transférer !
                if "Run failed:" in sujet_apercu or "bot-bu" in sujet_apercu:
                    log(f"🗑️ Alerte GitHub ignorée et marquée lue : {sujet_apercu}")
                    marquer_comme_lu(page, msg_id)
                    continue

                # 2. AFFLUENCES : Laisse 4 min à reserver.py pour valider, puis transfère sur Gmail comme preuve
                if "Confirmez votre réservation : BU Joseph-Fourier" in sujet_apercu and age_minutes < 4.0:
                    log(f"⏳ Mail Affluences récent ({age_minutes:.1f} min) laissé pour reserver.py")
                    continue

                dl = page.evaluate(
                    """async (id) => {
                        const r = await fetch(`/service/home/~/?auth=co&id=${id}`);
                        const buf = await r.arrayBuffer();
                        const bytes = new Uint8Array(buf);
                        let bin = '';
                        const chunk = 8192;
                        for (let i = 0; i < bytes.length; i += chunk) {
                            bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
                        }
                        return btoa(bin);
                    }""",
                    msg_id,
                )

                raw_eml = base64.b64decode(dl)
                eml_pret, sujet, exp = preparer_eml_pour_gmail(raw_eml)

                smtp.sendmail(GMAIL_ADDRESS, [GMAIL_ADDRESS], eml_pret)
                log(f"✅ Transféré vers Gmail : {sujet} (de {exp})")
                marquer_comme_lu(page, msg_id)

        nav.close()
        log("🎉 Terminé avec succès !")


if __name__ == "__main__":
    try:
        executer_facteur()
    except Exception as err:
        log(f"⚠️ Erreur interceptée proprement (zéro mail d'échec GitHub) : {err}")
    sys.exit(0)
