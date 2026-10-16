"""
Robot Facteur UGA -> Gmail
Récupère tous les mails non lus sur le Webmail UGA (Zimbra) et les transfère sur Gmail.
"""
import email
import os
import smtplib
from datetime import datetime
from email.header import decode_header, make_header
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

UGA_USER = os.environ["UGA_USER"].strip()
UGA_PASS = os.environ["UGA_PASS"].strip()
GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"].strip()
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "").strip()

PARIS = ZoneInfo("Europe/Paris")


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
    """Adapte les en-têtes du mail original pour que Gmail accepte le transfert avec pièces jointes."""
    msg = email.message_from_bytes(raw_bytes)

    exp_original = decoder_entete(msg.get("From", "Inconnu"))
    sujet_original = decoder_entete(msg.get("Subject", "(Sans objet)"))

    # On garde trace de l'expéditeur original et on permet de lui répondre directement depuis Gmail
    if "Reply-To" not in msg and msg.get("From"):
        msg["Reply-To"] = msg["From"]

    if "From" in msg:
        del msg["From"]
    msg["From"] = f"UGA Mail ({exp_original}) <{GMAIL_ADDRESS}>"

    if "To" in msg:
        del msg["To"]
    msg["To"] = GMAIL_ADDRESS

    if "Subject" in msg:
        del msg["Subject"]
    msg["Subject"] = f"[UGA] {sujet_original}"

    # Nettoyage des signatures DKIM/Return-Path du serveur d'origine pour éviter un rejet SMTP
    for h in ("DKIM-Signature", "DomainKey-Signature", "Return-Path"):
        if h in msg:
            del msg[h]

    return msg.as_bytes(), sujet_original, exp_original


def main():
    log("Connexion au webmail UGA...")
    with sync_playwright() as p:
        nav = p.chromium.launch()
        page = nav.new_page(locale="fr-FR")

        # 1. Authentification via l'interface web (contourne le blocage IMAP)
        page.goto("https://webmail.etu.univ-grenoble-alpes.fr", wait_until="domcontentloaded")
        page.fill("#username", UGA_USER)
        page.fill("#password", UGA_PASS)
        page.click("#submitBtn")
        page.wait_for_timeout(5000)

        # 2. Recherche des mails non lus via l'API interne de Zimbra
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
            return await r.json();
        }""")

        messages = recherche.get("Body", {}).get("SearchResponse", {}).get("m", [])
        if not messages:
            log("Aucun nouveau mail non lu. Tout est à jour !")
            nav.close()
            return

        log(f"{len(messages)} nouveau(x) mail(s) non lu(s) détecté(s) !")

        # 3. Connexion au serveur d'envoi de Gmail (SMTP SSL)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)

            # On traite du plus ancien au plus récent
            for m in reversed(messages):
                msg_id = m["id"]
                sujet_apercu = m.get("su", "(Sans objet)")

                # On ignore les mails de confirmation Affluences pour ne pas gêner le bot BU
                if "Confirmez votre réservation : BU Joseph-Fourier" in sujet_apercu:
                    log(f"Mail Affluences ignoré (laissé pour le bot BU) : {sujet_apercu}")
                    continue

                # Téléchargement du mail complet brut (.eml avec pièces jointes) depuis Zimbra
                dl = page.evaluate("""async (id) => {
                    const r = await fetch(`/service/home/~/?auth=co&id=${id}`);
                    const buf = await r.arrayBuffer();
                    const bytes = new Uint8Array(buf);
                    let bin = '';
                    for (let i = 0; i < bytes.byteLength; i++) {
                        bin += String.fromCharCode(bytes[i]);
                    }
                    return btoa(bin);
                }""", msg_id)

                import base64
                raw_eml = base64.b64decode(dl)
                eml_pret, sujet, exp = preparer_eml_pour_gmail(raw_eml)

                # Envoi sur ton Gmail
                smtp.sendmail(GMAIL_ADDRESS, [GMAIL_ADDRESS], eml_pret)
                log(f"✅ Transféré vers Gmail : {sujet} (de {exp})")

                # 4. Marquage du mail comme 'Lu' sur Zimbra pour ne pas le renvoyer dans 5 min
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
