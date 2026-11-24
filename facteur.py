"""Transfère les messages non lus de la boîte étudiante UGA vers Gmail."""
from datetime import datetime
import email
from email.header import decode_header, make_header
import os
import json
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

UGA_USER = (os.environ.get("FACTEUR_UGA_USER") or os.environ.get("UGA_USER", "")).strip()
UGA_PASS = os.environ.get("FACTEUR_UGA_PASS") if os.environ.get("FACTEUR_UGA_USER") else os.environ.get("UGA_PASS", "")
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


def compte_facteur():
    if os.environ.get("FACTEUR_UGA_USER") or "," not in UGA_USER:
        return UGA_USER, UGA_PASS
    choix = os.environ.get("FACTEUR_COMPTE", "").strip()
    if not choix:
        raise RuntimeError("Plusieurs comptes UGA : indiquer le login de la boîte à transférer dans la variable FACTEUR_COMPTE.")
    if os.environ.get("ACCOUNTS_JSON"):
        comptes = [(a["username"].strip(), a["password"]) for a in json.loads(os.environ["ACCOUNTS_JSON"])]
    else:
        noms = [u.strip() for u in UGA_USER.split(",")]
        mots = UGA_PASS.split(",")
        if len(noms) != len(mots):
            raise RuntimeError("Liste de mots de passe ambiguë : utiliser la paire de secrets dédiée au facteur.")
        comptes = list(zip(noms, mots))
    correspondances = [item for item in comptes if item[0] == choix]
    if len(correspondances) != 1:
        raise RuntimeError("La variable FACTEUR_COMPTE ne désigne pas un compte unique.")
    return correspondances[0]


async def ouvrir_boite(browser):
    login, mot_de_passe = compte_facteur()
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
    await page.locator("#username").fill(login)
    await page.locator("#password").fill(mot_de_passe)
    await page.locator("#submitBtn").click()
    try:
        await page.wait_for_url(lambda url: urlsplit(url)[:2] == urlsplit(MAIL)[:2],
                                wait_until="domcontentloaded", timeout=45000)
    except BrowserTimeout:
        hote = urlsplit(page.url).hostname
        texte = (await page.locator("body").inner_text(timeout=5000)).casefold()
        if any(x in texte for x in ("identifiants incorrects", "invalid credentials", "identifiant ou mot de passe incorrect", "mot de passe incorrect", "credentials you provided cannot be determined to be authentic")):
            raison = "identifiants refusés"
        elif any(x in texte for x in ("double authentification", "code de vérification", "authentification multifacteur", "one-time password", "second facteur")):
            raison = "validation supplémentaire demandée"
        elif await page.locator("input[type=password]").count():
            raison = "formulaire de connexion toujours affiché"
        else:
            raison = "redirection vers le webmail inachevée"
        raise RuntimeError(f"Agalan : {raison} (hôte : {hote})") from None
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
            messages = {}
            offset = 0
            diagnostic = os.environ.get("FACTEUR_DIAGNOSTIC") == "true"
            while True:
                resultat = await soap(boite, "SearchRequest", {
                    "types": "message", "limit": 100, "offset": offset, "sortBy": "dateAsc",
                    "read": 0, "query": {"_content": "in:inbox is:unread"},
                })
                lot = resultat.get("m", [])
                avant = len(messages)
                messages.update((str(m["id"]), m) for m in lot)
                if diagnostic or str(resultat.get("more", 0)).lower() not in ("1", "true"):
                    break
                if len(messages) == avant:
                    raise RuntimeError("Pagination Zimbra bloquée")
                offset += len(lot)
            log(f"Accès Zimbra confirmé, {len(messages)} non lu(s) recensé(s).")
            if diagnostic or not messages:
                return
            acceptes = ignores = differes = erreurs = 0
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30,
                                  context=ssl.create_default_context()) as smtp:
                smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
                for m in messages.values():
                    ident = str(m["id"])
                    sujet = m.get("su", "")
                    if not sujet.startswith(("Run failed:", "Run cancelled:")):
                        if ("Confirmez votre réservation : BU Joseph-Fourier" in sujet
                                and time.time() - int(m["d"]) / 1000 < 240):
                            differes += 1
                            continue
                        try:
                            reponse = await boite.context.request.get(
                                MAIL + "/service/home/~/", params={"auth": "co", "id": ident,
                                                                  "read": "0"},
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
                        except (smtplib.SMTPDataError, smtplib.SMTPRecipientsRefused, ValueError, RuntimeError) as exc:
                            erreurs += 1
                            log(f"Message {ident} en attente ({type(exc).__name__}, code {getattr(exc, 'smtp_code', '-')}).")
                            if getattr(exc, "smtp_code", 0) in (421, 450, 451, 452):
                                raise RuntimeError("Gmail demande de différer les transferts") from None
                            continue
                        acceptes += 1
                    else:
                        ignores += 1
                    await soap(boite, "MsgActionRequest", {"action": {"id": ident, "op": "read"}})
                log(f"Gmail a accepté {acceptes} message(s). Alertes filtrées : {ignores}. "
                    f"Réservations différées : {differes}. Erreurs : {erreurs}.")
                if erreurs:
                    raise RuntimeError("Des messages restent à transférer")
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(executer())
