#!/usr/bin/env python3
"""BU Joseph-Fourier: réservation et confirmation via webmail UGA, sans IMAP.

Python 3.9+; lancer --diagnostic avant --execute. Aucun secret dans les fichiers.
"""
import argparse
import asyncio
import html
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")
MAIL = "https://webmail.etu.univ-grenoble-alpes.fr"
API = "https://reservation.affluences.com/api"
SITE_ID = "888a64d4-3c8d-4ebf-83a5-d9441b5431b9"
TYPE_ID = 5249
QUOTA = 180
ACTIVE = {"confirmed", "pending", "uncertain", "submitting"}


def now():
    return datetime.now(PARIS)


def log(text):
    print(f"[{now():%H:%M:%S}] {text}", flush=True)


def norm(text):
    return "".join(c for c in unicodedata.normalize("NFKD", str(text).lower())
                   if not unicodedata.combining(c))


def minutes(value):
    h, m = map(int, value.split(":"))
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError("Heure incorrecte")
    return h * 60 + m


def clock(value):
    return f"{value // 60:02d}:{value % 60:02d}"


def base_email(value):
    local, domain = value.lower().strip().split("@", 1)
    return local.split("+", 1)[0] + "@" + domain


@dataclass
class Account:
    email: str
    username: str
    password: str


def load_accounts(env=os.environ):
    """JSON recommandé: les virgules dans les mots de passe sont préservées."""
    if env.get("ACCOUNTS_JSON", "").strip():
        values = json.loads(env["ACCOUNTS_JSON"])
        accounts = [Account(x["email"].strip().lower(), x["username"].strip(),
                            x["password"]) for x in values]
    else:
        addresses = [x.strip().lower() for x in env.get("AFF_EMAIL", "").split(",")]
        users = [x.strip() for x in env.get("UGA_USER", "").split(",")]
        passwords = env.get("UGA_PASS", "").split(",")
        if not (len(addresses) == len(users) == len(passwords)):
            raise ValueError("AFF_EMAIL, UGA_USER et UGA_PASS: nombres différents. Utiliser ACCOUNTS_JSON.")
        accounts = [Account(*x) for x in zip(addresses, users, passwords)]
    if not accounts or any(not a.email or "@" not in a.email or not a.username or not a.password
                           for a in accounts):
        raise ValueError("Comptes incomplets. Vérifier les secrets sans les afficher.")
    if len({base_email(a.email) for a in accounts}) != len(accounts):
        raise ValueError("Comptes dupliqués ou alias du même compte.")
    return accounts


@dataclass
class Booking:
    account: str
    day: str
    start: int
    end: int
    room: str
    status: str
    updated: float = 0.0

    @property
    def key(self):
        return (self.account, self.day, self.start, self.end, norm(self.room))


class Journal:
    def __init__(self, path):
        self.path = Path(path)
        self.entries = {}
        if self.path.exists():
            data = json.loads(self.path.read_text())
            if data.get("version") != 1:
                raise ValueError("Version du journal inconnue")
            for item in data["bookings"]:
                entry = Booking(**item)
                self.entries[entry.key] = entry

    def put(self, entry, persist=True):
        old = self.entries.get(entry.key)
        if old is None or entry.updated >= old.updated:
            self.entries[entry.key] = entry
        if persist:
            self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"version": 1, "bookings": [asdict(b) for b
                             in self.entries.values() if b.day >= now().date().isoformat()]},
                             ensure_ascii=False, indent=2))
        temporary.replace(self.path)

    def day_entries(self, day):
        return [b for b in self.entries.values() if b.day == day and b.status in ACTIVE]


class MailHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.links = [], []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for k, v in attrs:
                if k == "href" and v:
                    self.links.append(html.unescape(v))


def confirmation_url(url):
    """N'accepte jamais un lien d'annulation, de tracking ou un faux domaine."""
    try:
        u = urlsplit(html.unescape(url))
        return (u.scheme == "https" and u.hostname in {"affluences.com", "www.affluences.com"}
                and not u.username and not u.password and u.port in (None, 443)
                and re.fullmatch(r"/(?:[a-z]{2}/)?reservation/confirm/?", u.path) is not None
                and len(parse_qs(u.query).get("reservationToken", [""])[0]) >= 12)
    except ValueError:
        return False


MONTHS = {name: i for i, names in enumerate([
    "janvier january", "fevrier february", "mars march", "avril april", "mai may",
    "juin june", "juillet july", "aout august", "septembre september",
    "octobre october", "novembre november", "decembre december"], 1) for name in names.split()}


def parse_slot(text):
    text = norm(text).replace("\xa0", " ")
    d = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", text)
    if d:
        day = date(*map(int, d.groups()))
    else:
        d = re.search(r"\b(\d{1,2})[/.](\d{1,2})[/.](20\d{2})\b", text)
        if d:
            day = date(int(d[3]), int(d[2]), int(d[1]))
        else:
            names = "|".join(MONTHS)
            d = re.search(r"\b(\d{1,2})\s+(" + names + r")\s+(20\d{2})\b", text)
            e = re.search(r"\b(" + names + r")\s+(\d{1,2}),?\s+(20\d{2})\b", text)
            if d:
                day = date(int(d[3]), MONTHS[d[2]], int(d[1]))
            elif e:
                day = date(int(e[3]), MONTHS[e[1]], int(e[2]))
            else:
                return None
    # Le couple explicite évite de confondre l'heure du mail et celle du créneau.
    pair = re.search(r"(?:de|from|at)?\s*(\d{1,2})[:h](\d{2})\s*(am|pm)?\s*"
                     r"(?:a|to|[-–])\s*(\d{1,2})[:h](\d{2})\s*(am|pm)?", text)
    if not pair:
        return None
    h1, m1, mer1, h2, m2, mer2 = pair.groups()
    def convert(h, m, mer):
        h, m = int(h), int(m)
        if mer:
            h = h % 12 + (12 if mer == "pm" else 0)
        return minutes(f"{h}:{m}")
    start, end = convert(h1, m1, mer1), convert(h2, m2, mer2)
    return (day.isoformat(), start, end) if start < end else None


def string_value(value):
    return value.get("_content", "") if isinstance(value, dict) else str(value or "")


def mime_contents(node):
    values = []
    if isinstance(node, dict):
        if node.get("ct") in {"text/html", "text/plain"}:
            values.append(string_value(node.get("content")))
        for child in node.get("mp", []):
            values.extend(mime_contents(child))
    return values


def parse_message(message, account):
    subject = string_value(message.get("su"))
    if "bu joseph-fourier" not in norm(subject):
        return None
    senders = [x.get("a", "").lower() for x in message.get("e", []) if x.get("t") == "f"]
    if "no-reply@affluences.com" not in senders:
        return None
    recipients = [x.get("a", "") for x in message.get("e", []) if x.get("t") in {"t", "c"}]
    if not any(base_email(x) == base_email(account.email) for x in recipients if "@" in x):
        return None
    parser = MailHTML()
    parser.feed(" ".join(mime_contents(message)))
    slot = parse_slot(" ".join(parser.parts))
    if not slot:
        return None
    room_match = re.search(r"BU Joseph-Fourier\s*[-–:]\s*(.+)$", subject, re.I)
    if not room_match:
        return None
    s = norm(subject)
    if "annule" in s or "cancelled" in s or "canceled" in s:
        status = "cancelled"
    elif "reservation confirmee" in s or re.search(r"reservation (?:has been |is )?confirmed", s):
        status = "confirmed"
    elif "confirmez" in s or "confirm your reservation" in s:
        status = "pending"
    else:
        return None
    links = [u for u in parser.links if confirmation_url(u)]
    stamp = float(message.get("d", 0)) / 1000
    return Booking(base_email(account.email), *slot, room_match[1].strip(), status, stamp), next(iter(links), None)


class Mailbox:
    def __init__(self, account, number, browser):
        self.account, self.number, self.browser = account, number, browser
        self.context, self.page = None, None
        self.csrf, self.soap_url = "", MAIL + "/service/soap"
        self.read_cache = {}
        self.csrf_seen = asyncio.Event()

    async def login(self):
        self.context = await self.browser.new_context(locale="fr-FR", timezone_id="Europe/Paris")
        self.page = await self.context.new_page()
        def capture(request):
            u = urlsplit(request.url)
            if u.hostname == urlsplit(MAIL).hostname and u.path.startswith("/service/soap"):
                token = request.headers.get("x-zimbra-csrf-token")
                if token:
                    self.csrf = token
                    self.csrf_seen.set()
        self.page.on("request", capture)
        await self.page.goto(MAIL, wait_until="domcontentloaded", timeout=60000)
        await self.page.locator("#username").wait_for(timeout=45000)
        if urlsplit(self.page.url).hostname != "authentification.univ-grenoble-alpes.fr":
            raise RuntimeError(f"Compte {self.number}: destination de connexion inattendue")
        await self.page.locator("#username").fill(self.account.username)
        await self.page.locator("#password").fill(self.account.password)
        await self.page.locator("#submitBtn").click()
        try:
            await self.page.locator("#zi_search_inputfield").wait_for(timeout=60000)
        except Exception:
            raise RuntimeError(f"Compte {self.number}: connexion Agalan refusée ou étape supplémentaire") from None
        # Produit une requête normale du webmail et récupère son jeton CSRF en mémoire.
        await self.page.locator("#zi_search_inputfield").fill("from:no-reply@affluences.com")
        await self.page.locator("#zi_search_inputfield").press("Enter")
        try:
            await asyncio.wait_for(self.csrf_seen.wait(), timeout=8)
        except asyncio.TimeoutError:
            pass  # Certaines installations n'imposent pas de jeton CSRF.
        await self.search(limit=1)  # Vérifie aussi l'accès réel aux messages.
        log(f"Compte {self.number}: webmail accessible")

    async def soap(self, operation, args):
        args = dict(args, _jsns="urn:zimbraMail")
        headers = {"Content-Type": "application/json", "Referer": MAIL + "/mail"}
        if self.csrf:
            headers["X-Zimbra-Csrf-Token"] = self.csrf
        response = await self.context.request.post(self.soap_url, data={
            "Header": {"context": {"_jsns": "urn:zimbra"}}, "Body": {operation: args}},
            headers=headers, timeout=20000, max_redirects=0)
        try:
            data = await response.json()
        except Exception:
            raise RuntimeError(f"Compte {self.number}: réponse webmail illisible (HTTP {response.status})") from None
        finally:
            await response.dispose()
        body = data.get("Body", {})
        if "Fault" in body or operation.replace("Request", "Response") not in body:
            raise RuntimeError(f"Compte {self.number}: session webmail non utilisable; vérifier Agalan/CSRF")
        return body[operation.replace("Request", "Response")]

    async def search(self, offset=0, limit=100):
        after = (now().date() - timedelta(days=17)).strftime("%m/%d/%Y")
        return await self.soap("SearchRequest", {"types": "message", "sortBy": "dateDesc",
            "offset": offset, "limit": limit, "fetch": "all", "html": 1, "read": 0,
            "max": 200000, "query": {"_content": f'from:no-reply@affluences.com after:{after} "BU Joseph-Fourier"'}})

    async def messages(self):
        parsed, offset = [], 0
        for _ in range(30):
            data = await self.search(offset)
            messages = data.get("m", [])
            for message in messages:
                identifier = str(message["id"])
                if identifier not in self.read_cache:
                    if not message.get("mp"):
                        detail = await self.soap("GetMsgRequest", {"m": {"id": identifier,
                            "html": 1, "read": 0, "max": 200000}})
                        value = detail.get("m", [])
                        message = value[0] if isinstance(value, list) and value else value
                    value = parse_message(message, self.account)
                    if value is None and "bu joseph-fourier" in norm(string_value(message.get("su"))):
                        raise RuntimeError(f"Compte {self.number}: mail Affluences non interprétable; arrêt pour éviter les doublons")
                    self.read_cache[identifier] = value
                value = self.read_cache[identifier]
                if value:
                    parsed.append(value)
            if str(data.get("more", "0")).lower() not in {"1", "true"}:
                return sorted(parsed, key=lambda x: x[0].updated, reverse=True)
            if not messages:
                raise RuntimeError("Pagination du webmail incohérente")
            offset += len(messages)
        raise RuntimeError("Plus de 3000 messages Affluences récents: analyse incomplète, arrêt")


def room_priority(room):
    name = re.sub(r"[^a-z0-9]", "", norm(room))
    if name == "rdcsudn4":
        return (0, name)
    if name == "rdcsudn5":
        return (1, name)
    return (2 if "rdcsud" in name else 3 if "rdc" in name else 4, name)


def available_minutes(resource):
    result = set()
    for slot in resource.get("hours", []):
        if slot.get("state") == "available" and slot.get("places_bookable", 0) > 0:
            start = minutes(slot["hour"])
            result.update(range(start, start + int(slot.get("granularity", resource["granularity"])) ))
    return result


def plan_next(resources, accounts, entries, day, start, end, persons, blocked=None):
    """Choisit le premier trou, puis la salle prioritaire et le plus long créneau."""
    blocked = blocked or set()
    covered = set()
    used = {base_email(a.email): 0 for a in accounts}
    for b in entries:
        if b.day == day and b.status in ACTIVE:
            covered.update(range(b.start, b.end))
            used[b.account] = used.get(b.account, 0) + b.end - b.start
    for current in range(start, end, 30):
        if all(m in covered for m in range(current, min(current + 30, end))):
            continue
        for resource in sorted(resources, key=lambda r: room_priority(r["resource_name"])):
            if int(resource.get("capacity", 0)) < persons or int(resource.get("granularity", 0)) != 30:
                continue
            free = available_minutes(resource)
            for account in accounts:
                key = base_email(account.email)
                remaining = QUOTA - used.get(key, 0)
                if key in blocked or remaining < 30:
                    continue
                maximum = min(end, current + remaining,
                              current + int(resource.get("time_slot_count", 6)) * 30)
                finish = current
                while finish + 30 <= maximum:
                    piece = set(range(finish, finish + 30))
                    if not piece <= free or piece & covered:
                        break
                    finish += 30
                if finish > current:
                    return resource, account, current, finish
    return None


async def get_resources(http, day):
    for attempt in range(3):
        try:
            r = await http.get(f"{API}/resources/{SITE_ID}/available",
                               params={"date": day, "type": TYPE_ID}, timeout=15000)
            try:
                if r.status == 429 or r.status >= 500:
                    await asyncio.sleep(2 ** attempt)
                    continue
                if r.status != 200:
                    raise RuntimeError(f"Disponibilités: HTTP {r.status}")
                data = await r.json()
            finally:
                await r.dispose()
            if not isinstance(data, list) or any(not isinstance(x.get("hours"), list) for x in data):
                raise RuntimeError("Format des disponibilités modifié")
            return data
        except Exception:
            if attempt == 2:
                raise RuntimeError(f"Impossible de lire les disponibilités du {day}") from None
    raise RuntimeError(f"Service indisponible pour {day}")


def page_confirmed(text):
    text = norm(text)
    if any(s in text for s in ("n'a pas", "n’est pas", "n'est pas", "not confirmed",
                                "annulee", "cancelled", "expire", "invalide")):
        return False
    return bool(re.search(r"(?:votre|la) reservation (?:a (?:bien )?ete|est|deja) (?:bien )?confirmee"
                         r"|your reservation (?:has (?:already )?been |is (?:already )?)confirmed"
                         r"|reservation already confirmed", text))


async def confirm(browser, url):
    if not confirmation_url(url):
        return False
    context = await browser.new_context(locale="fr-FR")
    try:
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        for _ in range(12):
            if page_confirmed(await page.locator("body").inner_text()):
                return True
            await asyncio.sleep(0.5)
        return False
    except Exception:
        return False
    finally:
        await context.close()


async def reconcile(mailboxes, journal, days, browser, execute):
    results = await asyncio.gather(*(m.messages() for m in mailboxes))
    links, seen = [], set()
    for mailbox, messages in zip(mailboxes, results):
        for booking, url in messages:
            if booking.day not in days or booking.key in seen:
                continue
            seen.add(booking.key)
            old = journal.entries.get(booking.key)
            # Une confirmation de page plus récente prime sur l'ancien mail de demande.
            if old and old.updated > booking.updated:
                # Un ancien mail d'annulation ne doit jamais annuler une nouvelle demande.
                evidence = old.status in {"uncertain", "submitting"} and booking.status == "confirmed"
                waiting_mail = old.status == booking.status == "pending"
                if not (evidence or waiting_mail):
                    continue
            journal.put(booking, persist=False)
            if booking.status == "pending" and url and execute:
                # Ne valider que les demandes récentes dans la plage demandée.
                if (time.time() - booking.updated < 7200 and
                        minutes(os.getenv("START", "11:30")) <= booking.start < booking.end <= minutes(os.getenv("END", "21:00"))):
                    links.append((booking, url))
    # Au plus trois pages de confirmation en parallèle; résultat lié au bon créneau.
    limit = asyncio.Semaphore(3)
    async def one(booking, url):
        async with limit:
            if await confirm(browser, url):
                booking.status, booking.updated = "confirmed", time.time()
                journal.put(booking, persist=False)
                log(f"Confirmée: {booking.day} {clock(booking.start)}–{clock(booking.end)} {booking.room}")
    await asyncio.gather(*(one(b, u) for b, u in links))
    # Uniquement les données de réservation: aucun cookie, lien ou mot de passe.
    if execute:
        journal.save()


async def submit(http, journal, resource, account, day, start, end, persons):
    booking = Booking(base_email(account.email), day, start, end,
                      resource["resource_name"], "submitting", time.time())
    journal.put(booking)  # Écrit AVANT le POST pour survivre à une interruption.
    try:
        response = await http.post(f"{API}/reserve/{resource['resource_id']}", data={
            "email": account.email, "date": day, "start_time": clock(start), "end_time": clock(end),
            "person_count": persons, "note": ""}, timeout=15000, max_redirects=0)
        try:
            code = response.status
            try:
                payload = await response.json()
            except Exception:
                payload = {}
        finally:
            await response.dispose()
    except Exception:
        booking.status = "uncertain"
        journal.put(booking)
        raise RuntimeError("Réponse de réservation perdue: arrêt, vérification au prochain passage") from None
    if code in {200, 201}:
        booking.status = "pending"
    elif code == 400 and isinstance(payload, dict) and payload.get("error") == "already_have_reservation":
        booking.status = "uncertain"
    elif code == 400:
        booking.status = "rejected"
    elif code == 429 or code >= 500:
        booking.status = "uncertain"
        journal.put(booking)
        raise RuntimeError(f"Réservation HTTP {code}: état incertain, arrêt sans nouveau POST")
    else:
        booking.status = "uncertain"
        journal.put(booking)
        raise RuntimeError(f"Réservation HTTP {code}: vérification nécessaire")
    journal.put(booking)
    error = payload.get("error", "") if isinstance(payload, dict) else ""
    log(f"{day} {clock(start)}–{clock(end)} {booking.room}: {booking.status}" +
        (f" ({error})" if error in {"already_have_reservation", "quota_overpassed", "invalid_timeslot"} else ""))
    return booking, error


def date_selection(value, horizon, today=None):
    today = today or now().date()
    if value == "rattrapage":
        return [(today + timedelta(days=i)).isoformat() for i in range(horizon + 1)
                if (today + timedelta(days=i)).weekday() < 5]
    d = date.fromisoformat(value) if value else today + timedelta(days=horizon)
    if not today <= d <= today + timedelta(days=15):
        raise ValueError("Date hors de la fenêtre J à J+15")
    return [d.isoformat()] if d.weekday() < 5 else []


def summary(journal, days, start, end):
    lines, complete = [], True
    for day in days:
        confirmed, pending = set(), set()
        for b in journal.day_entries(day):
            (confirmed if b.status == "confirmed" else pending).update(range(max(start, b.start), min(end, b.end)))
        holes = set(range(start, end)) - confirmed
        complete &= not holes
        lines.append(f"{day}: {len(confirmed)//60}h{len(confirmed)%60:02d} confirmées; "
                     f"{len(pending)} min à vérifier; {len(holes)} min non confirmées")
    return lines, complete


async def notify(http, text):
    token, chat = os.getenv("TG_TOKEN"), os.getenv("TG_CHAT")
    if token and chat:
        try:
            r = await http.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                data={"chat_id": chat, "text": text[:4000]}, timeout=10000)
            if not r.ok:
                log("Notification Telegram non envoyée")
            await r.dispose()
        except Exception:
            log("Notification Telegram indisponible")


async def run(args):
    from playwright.async_api import async_playwright
    accounts = load_accounts()
    if args.opening:
        target = now().replace(hour=int(args.opening[:2]), minute=int(args.opening[3:]), second=0, microsecond=0)
        if target < now() - timedelta(minutes=15):
            target += timedelta(days=1)
        if (target - now()).total_seconds() > 900:
            log("Exécution trop éloignée de l'ouverture: rien à faire")
            return 0
    else:
        target = None
    horizon = int(os.getenv("DAYS_AHEAD", "14"))
    start, end = minutes(os.getenv("START", "11:30")), minutes(os.getenv("END", "21:00"))
    persons = int(os.getenv("NB_PERSONNES", "3"))
    if not (1 <= horizon <= 15 and start < end and start % 30 == end % 30 == 0 and persons >= 2):
        raise ValueError("Configuration dates/heures/personnes invalide")
    journal = Journal(os.getenv("STATE_FILE", "state/journal.json"))
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not args.headed)
        http = await p.request.new_context(extra_http_headers={
            "Origin": "https://affluences.com", "Referer": "https://affluences.com/",
            "Accept": "application/json"})
        try:
            mailboxes = [Mailbox(a, i, browser) for i, a in enumerate(accounts, 1)]
            # Barrière: aucune réservation tant que TOUTES les boîtes ne sont pas lisibles.
            await asyncio.gather(*(m.login() for m in mailboxes))
            days = date_selection(args.date, horizon,
                                  today=target.date() if target else now().date())
            if not days:
                log("Jour cible hors lundi–vendredi")
                return 0
            await reconcile(mailboxes, journal, days, browser, args.execute)
            if target and now() < target:
                log(f"Historique chargé; attente de l'ouverture {target:%Y-%m-%d %H:%M:%S} Paris")
            while target and now() < target:
                await asyncio.sleep(min(20, max(0.01, (target - now()).total_seconds())))
            if len(accounts) * QUOTA < end - start:
                log(f"Capacité: {len(accounts)*QUOTA} min pour {end-start} min demandées. "
                    "La couverture totale nécessite un compte participant supplémentaire ou un horaire plus court.")
            limiter = asyncio.Semaphore(3)
            async def fetch(day):
                async with limiter:
                    return day, await get_resources(http, day)
            snapshots = dict(await asyncio.gather(*(fetch(d) for d in days)))
            for day in days:
                resources = snapshots[day]
                blocked, attempts = set(), 0
                day_start = start
                if day == now().date().isoformat():
                    day_start = max(start, ((now().hour * 60 + now().minute) // 30 + 1) * 30)
                # Maximum borné: évite les boucles en cas de changements du service.
                while attempts < 40:
                    candidate = plan_next(resources, accounts, journal.day_entries(day), day,
                                          day_start, end, persons, blocked)
                    if candidate is None:
                        break
                    resource, account, begin, finish = candidate
                    if not args.execute:
                        log(f"SIMULATION {day} {clock(begin)}–{clock(finish)} {resource['resource_name']}")
                        journal.put(Booking(base_email(account.email), day, begin, finish,
                                            resource["resource_name"], "pending", time.time()), persist=False)
                        continue
                    attempts += 1
                    booking, error = await submit(http, journal, resource, account, day, begin, finish, persons)
                    if error == "quota_overpassed" or booking.status == "uncertain":
                        blocked.add(base_email(account.email))
                    elif booking.status == "rejected":
                        if error == "invalid_timeslot":
                            # API change/fermeture: pas d'essai aveugle sur vingt salles.
                            break
                        resources = await get_resources(http, day)
                        blocked.add(base_email(account.email))
                    # Les soumissions restent séquentielles pour préserver quotas et continuité.
                if args.execute:
                    await reconcile(mailboxes, journal, days, browser, True)
            if not args.execute:
                log("Diagnostic terminé: aucun POST de réservation, aucune confirmation, aucun état écrit.")
                return 0
            deadline = time.monotonic() + int(os.getenv("MAIL_WAIT_SECONDS", "120"))
            while any(b.status == "pending" for b in journal.entries.values() if b.day in days):
                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(5)
                await reconcile(mailboxes, journal, days, browser, True)
            lines, complete = summary(journal, days, start, end)
            for line in lines:
                log(line)
            text = "BU — " + ("couverture complète" if complete else "couverture incomplète") + "\n" + "\n".join(lines)
            Path("rapport.txt").write_text(text, encoding="utf-8")
            if os.getenv("GITHUB_STEP_SUMMARY"):
                with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
                    f.write(text + "\n")
            await notify(http, text)
            return 0 if complete else 2
        except Exception:
            error_text = "Échec technique: aucune couverture complète ne peut être annoncée. Consulter les logs."
            Path("rapport.txt").write_text(error_text, encoding="utf-8")
            await notify(http, error_text)
            raise
        finally:
            await http.dispose()
            await browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("date", nargs="?", default="", help="AAAA-MM-JJ, rattrapage ou vide = J+14")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--execute", action="store_true", help="Réserver et confirmer")
    modes.add_argument("--diagnostic", action="store_true", help="Lecture et simulation uniquement (défaut)")
    parser.add_argument("--headed", action="store_true", help="Afficher Chromium en local")
    parser.add_argument("--opening", choices=["00:00", "07:00"], help="Préchauffage avant l'ouverture, délai maximal 15 min")
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        # Les erreurs de bibliothèque peuvent contenir des URL avec jetons: ne pas les imprimer.
        message = str(e) if isinstance(e, (ValueError, RuntimeError)) else type(e).__name__
        message = re.sub(r"https?://\S+", "[URL masquée]", message)
        log("ÉCHEC: " + message[:300])
        return 1


if __name__ == "__main__":
    sys.exit(main())
