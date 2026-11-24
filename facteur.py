"""
Robot Facteur UGA -> Gmail (RESTAURÉ de la version du 18 octobre qui marchait
+ filtre anti-crash GitHub + fenêtre Affluences 4 min + marquage lu).
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
def ouvrir_webmail(page):
    """Tente d'ouvrir le webmail UGA avec plusieurs essais et URL de secours (version qui marchait)."""
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
def marquer_comme_lu(page, msg_id):
    try:
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
    except Exception as e:
        log(f"Avertissement marquage lu {msg_id} : {e}")
def main():
    if not all([UGA_USER, UGA_PASS, GMAIL_ADDRESS, GMAIL_APP_PASSWORD]):
        log("❌ ERREUR : Un ou plusieurs secrets sont vides.")
        sys.exit(1)
    with sync_playwright() as p:
        # Args EXACTS de la version du 18 octobre qui marchait
        nav = p.chromium.launch(args=["--dns-prefetch-disable"])
        page = nav.new_page(locale="fr-FR")
        if not ouvrir_webmail(page):
            log("⚠️ Serveur UGA temporairement injoignable (coupure DNS/maintenance). Nouvel essai au prochain passage.")
            nav.close()
            return
        page.fill("#username", UGA_USER)
        page.fill("#password", UGA_PASS)
        page.click("#submitBtn")
        # === LE COEUR DU FIX : attente SOUPLE de la version qui marchait ===
        # Aucune attente stricte d'URL ou de sélecteur : on attend juste que le
        # réseau se calme, puis on tape l'API SOAP avec les cookies de session.
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass  # Certaines pages Zimbra ne sont jamais vraiment "idle"
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
                return { ok: false, status: r.status, raw: text.slice(0, 200) };
            }
        }""")
        if not recherche.get("ok"):
            log(f"⚠️ Réponse inattendue de Zimbra ({recherche.get('status')}). "
                f"Détail : {recherche.get('raw', '')[:120]} Réessai au prochain passage.")
            nav.close()
            return
        messages = recherche["data"].get("Body", {}).get("SearchResponse", {}).get("m", [])
        if not messages:
            log("Aucun nouveau mail non lu. Tout est à jour !")
            nav.close()
            return
        log(f"{len(messages)} nouveau(x) mail(s) non lu(s) détecté(s) !")
        now_ms = int(time.time() * 1000)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            for m in reversed(messages):
                msg_id = m["id"]
                sujet_apercu = m.get("su", "(Sans objet)")
                date_mail_ms = int(m.get("d", now_ms))
                age_minutes = (now_ms - date_mail_ms) / 60000.0
                # Filtre anti-spam PRÉCIS : uniquement les vraies alertes de crash GitHub
                if sujet_apercu.startswith("Run failed:") or sujet_apercu.startswith("Run cancelled:"):
                    log(f"🗑️ Alerte GitHub ignorée et marquée lue : {sujet_apercu}")
                    marquer_comme_lu(page, msg_id)
                    continue
                # Laisse 4 min à reserver.py pour confirmer, puis transfère comme preuve
                if "Confirmez votre réservation : BU Joseph-Fourier" in sujet_apercu and age_minutes < 4.0:
                    log(f"⏳ Mail Affluences récent ({age_minutes:.1f} min) laissé pour reserver.py")
                    continue
                try:
                    dl = page.evaluate("""async (id) => {
                        const r = await fetch(`/service/home/~/?auth=co&id=${id}`);
                        if (!r.ok) throw new Error('HTTP ' + r.status);
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
                    marquer_comme_lu(page, msg_id)
                except Exception as e:
                    log(f"⚠️ Échec transfert du mail {msg_id} ({sujet_apercu[:40]}) : {e}")
        nav.close()
        log("🎉 Tous les nouveaux mails ont été transférés sur ton Gmail !")
if __name__ == "__main__":
    main()
