"""Transfère les messages non lus de la boîte étudiante UGA vers Gmail."""
from datetime import datetime
import email
from email.header import decode_header, make_header
import os
import smtplib
import asyncio
import ssl
from urllib.parse import urlsplit
from email.utils import formataddr, parseaddr
import time
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright, TimeoutError as BrowserTimeout
from types import SimpleNamespace

MAIL = "https://webmail.etu.univ-grenoble-alpes.fr"

UGA_USER = os.environ.get("UGA_USER", "").strip()
UGA_PASS = os.environ.get("UGA_PASS", "").strip()
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "").strip()
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()

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
    msg = email.message_from_bytes(raw_bytes)
    exp_original = decoder_entete(msg.get("From", "Inconnu"))
    sujet_original = decoder_entete(msg.get("Subject", "(Sans objet)"))

    if "Reply-To" not in msg and msg.get("From"):
        msg["Reply-To"] = msg["From"]

    for h in ("From", "To", "Subject", "DKIM-Signature", "DomainKey-Signature", "Return-Path"):
        if h in msg:
            del msg[h]

    msg["From"] = formataddr((f"UGA Mail ({parseaddr(exp_original)[0] or parseaddr(exp_original)[1]})", GMAIL_ADDRESS))
    msg["To"] = GMAIL_ADDRESS
    msg["Subject"] = f"[UGA] {sujet_original}"

    return msg.as_bytes(), sujet_original, exp_original


async def ouvrir_boite(browser):
    boite = SimpleNamespace(csrf="", csrf_seen=asyncio.Event())
    boite.context = await browser.new_context(locale="fr-FR", timezone_id="Europe/Paris")
    page = await boite.context.new_page()

    def observer(requete):
        url = urlsplit(requete.url)
        if (url.scheme, url.netloc) == ("https", urlsplit(MAIL).netloc) and url.path.startswith("/service/soap"):
            jeton = requete.headers.get("x-zimbra-csrf-token")
            if jeton:
                boite.csrf = jeton
                boite.csrf_seen.set()

    page.on("request", observer)
    await page.goto(MAIL, wait_until="domcontentloaded", timeout=45000)
    await page.locator("#username").wait_for(timeout=30000)
    if urlsplit(page.url)[:2] != ("https", "authentification.univ-grenoble-alpes.fr"):
        raise RuntimeError("Destination de connexion Agalan inattendue")
    await page.locator("#username").fill(UGA_USER)
    await page.locator("#password").fill(UGA_PASS)
    await page.locator("#submitBtn").click()
    try:
        await page.wait_for_url(lambda url: urlsplit(url)[:2] == urlsplit(MAIL)[:2],
                                wait_until="domcontentloaded", timeout=45000)
    except BrowserTimeout:
        raise RuntimeError("Agalan n’a pas renvoyé la session au webmail après connexion") from None
    try:
        await asyncio.wait_for(boite.csrf_seen.wait(), timeout=8)
    except asyncio.TimeoutError:
        pass
    return boite


async def soap(boite, operation, arguments):
    headers = {"Content-Type": "application/json", "Referer": MAIL + "/"}
    if boite.csrf:
        headers["X-Zimbra-Csrf-Token"] = boite.csrf
    reponse = await boite.context.request.post(
        MAIL + "/service/soap/" + operation,
        headers=headers, max_redirects=0, timeout=20000,
        data={"Header": {"context": {"_jsns": "urn:zimbra", "format": {"type": "js"}}},
              "Body": {operation: dict(arguments, _jsns="urn:zimbraMail")}})
    try:
        try:
            body = (await reponse.json()).get("Body", {})
        except (ValueError, AttributeError):
            raise RuntimeError(f"{operation} : réponse non SOAP (HTTP {reponse.status})") from None
        attendu = operation.removesuffix("Request") + "Response"
        if not reponse.ok or attendu not in body or "Fault" in body:
            faute = body.get("Fault", {}).get("Detail", {}).get("Error", {}).get("Code", "inconnue")
            raise RuntimeError(f"{operation} : erreur Zimbra {faute} (HTTP {reponse.status})")
        return body[attendu]
    finally:
        await reponse.dispose()


async def executer():
    if not all((UGA_USER, UGA_PASS, GMAIL_ADDRESS, GMAIL_APP_PASSWORD)):
        raise RuntimeError("Un des quatre secrets du workflow est vide")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            boite = await ouvrir_boite(browser)
            resultat = await soap(boite, "SearchRequest", {
                "types": "message", "limit": 30, "sortBy": "dateAsc", "read": 0,
                "query": {"_content": "in:inbox is:unread"},
            })
            messages = resultat.get("m", [])
            log(f"Accès Zimbra confirmé, {len(messages)} message(s) non lu(s) dans ce lot.")
            if not messages or os.environ.get("FACTEUR_DIAGNOSTIC") == "true":
                return
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30,
                                  context=ssl.create_default_context()) as smtp:
                smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
                for m in messages:
                    ident = str(m["id"])
                    sujet = m.get("su", "")
                    if not sujet.startswith(("Run failed:", "Run cancelled:")):
                        if ("Confirmez votre réservation : BU Joseph-Fourier" in sujet
                                and time.time() - int(m["d"]) / 1000 < 240):
                            continue
                        reponse = await boite.context.request.get(
                            MAIL + "/service/home/~/", params={"auth": "co", "id": ident,
                                                              "fmt": "eml", "read": "0"},
                            max_redirects=0, timeout=30000)
                        try:
                            if not reponse.ok:
                                raise RuntimeError(f"Lecture du message impossible (HTTP {reponse.status})")
                            raw = await reponse.body()
                        finally:
                            await reponse.dispose()
                        if not email.message_from_bytes(raw).get("From"):
                            raise RuntimeError("Le webmail n’a pas retourné un message MIME valide")
                        eml, _, _ = preparer_eml_pour_gmail(raw)
                        smtp.sendmail(GMAIL_ADDRESS, [GMAIL_ADDRESS], eml)
                    await soap(boite, "MsgActionRequest", {"action": {"id": ident, "op": "read"}})
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(executer())
