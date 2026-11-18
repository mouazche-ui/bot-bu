"""Lance le bot avec les lecteurs corrigés et une session navigateur Affluences."""
import asyncio
import json
import os
import re
import sys
import time
from html.parser import HTMLParser
from urllib.parse import urlsplit

import correctif_mails as mails
import reserver as bot

PAGE_URL = "https://affluences.com/fr/sites/bu-joseph-fourier/reservation?type=5249"
OLD_LOGIN = bot.Mailbox.login
OLD_SUBMIT = bot.submit
SESSION = None
SESSION_LOCK = None


def response_summary(payload, text=""):
    if isinstance(payload, dict):
        values = []
        for key in ("error", "errorMessage", "message", "description"):
            value = payload.get(key)
            if isinstance(value, (str, int, bool)):
                values.append(f"{key}: {value}")
        if values:
            return mails.safe_text(" | ".join(values))[:380]
    title = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    if title:
        return mails.safe_text(re.sub(r"<[^>]+>", " ", title[1]))[:240]
    if "<" not in text:
        return mails.safe_text(text)[:240]
    return "Réponse HTML reçue; aucun message JSON exploitable"


class BookingHTTP:
    """Client de la même session que la page officielle, sans réessai de POST."""
    def __init__(self, session):
        self.session = session
        self.last_status = None
        self.last_detail = ""

    async def post(self, url, **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        headers.update({"Origin": "https://affluences.com", "Referer": PAGE_URL,
                        "Accept": "application/json"})
        response = await self.session.request.post(url, headers=headers, **kwargs)
        self.last_status = response.status
        if response.status >= 400:
            try:
                text = await response.text()
            except Exception:
                text = ""
            try:
                payload = json.loads(text)
            except (ValueError, TypeError):
                payload = None
            self.last_detail = response_summary(payload, text)
            bot.log(f"Affluences HTTP {response.status}: {self.last_detail}")
        return response


async def telegram_call(method, data=None):
    token = os.getenv("TG_TOKEN", "").strip()
    if not token or SESSION is None:
        return False
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        if data is None:
            response = await SESSION.request.get(url, timeout=10000, max_redirects=0)
        else:
            response = await SESSION.request.post(url, data=data, timeout=10000, max_redirects=0)
        try:
            try:
                result = await response.json()
            except Exception:
                result = {}
            if not response.ok or not result.get("ok"):
                bot.log(f"Telegram HTTP {response.status}: {response_summary(result)}")
                return False
            return True
        finally:
            await response.dispose()
    except Exception as error:
        # L'exception Playwright peut contenir le jeton dans l'URL: type seulement.
        bot.log(f"Telegram: erreur réseau ({type(error).__name__})")
        return False


async def browser_login(self):
    global SESSION, SESSION_LOCK
    await OLD_LOGIN(self)
    if SESSION_LOCK is None:
        SESSION_LOCK = asyncio.Lock()
    async with SESSION_LOCK:
        if SESSION is not None:
            return
        context = await self.browser.new_context(locale="fr-FR", timezone_id="Europe/Paris")
        page = await context.new_page()
        response = await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=45000)
        if response is None or response.status >= 400:
            status = response.status if response else "sans réponse"
            raise RuntimeError(f"Page officielle Affluences inaccessible: HTTP {status}")
        if urlsplit(page.url).hostname not in {"affluences.com", "www.affluences.com"}:
            raise RuntimeError("Redirection Affluences inattendue")
        try:
            await page.get_by_role("heading", name="Salles de travail en groupe", exact=True).wait_for(timeout=30000)
        except Exception:
            raise RuntimeError("La page Affluences n'affiche pas le formulaire attendu") from None
        SESSION = context
        bot.log("Session navigateur Affluences prête")
        if os.getenv("TG_TOKEN", "").strip():
            if await telegram_call("getMe"):
                bot.log("Telegram: jeton reconnu")
                chat = os.getenv("TG_CHAT", "").strip()
                if chat and await telegram_call("getChat", {"chat_id": chat}):
                    bot.log("Telegram: conversation accessible")


async def browser_submit(http, journal, resource, account, day, start, end, persons):
    if SESSION is None:
        raise RuntimeError("Session Affluences absente: aucune demande envoyée")
    client = BookingHTTP(SESSION)
    try:
        return await OLD_SUBMIT(client, journal, resource, account, day, start, end, persons)
    except RuntimeError:
        if client.last_status in {401, 403}:
            # Ce POST a reçu un refus explicite. Les anciens états incertains restent intacts.
            key = (bot.base_email(account.email), day, start, end, bot.norm(resource["resource_name"]))
            booking = journal.entries.get(key)
            if booking:
                booking.status, booking.updated = "rejected", time.time()
                journal.put(booking)
            raise RuntimeError(f"Affluences refuse la demande (HTTP {client.last_status}). "
                               "Consulter la ligne Affluences HTTP ci-dessus; aucun réessai automatique.") from None
        raise


async def notify(http, text):
    chat = os.getenv("TG_CHAT", "").strip()
    if chat:
        if await telegram_call("sendMessage", {"chat_id": chat, "text": text[:4000]}):
            bot.log("Notification Telegram envoyée")


def install():
    mails.install()
    bot.Mailbox.login = browser_login
    bot.submit = browser_submit
    bot.notify = notify


if __name__ == "__main__":
    install()
    sys.exit(bot.main())
