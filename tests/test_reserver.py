import asyncio
import json
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch

import reserver as bot


ACCOUNT = bot.Account("a@etu.example", "agalan", "p,ass")


def resource(name="RDC Sud n°4", unavailable=(), step=30):
    return {"resource_id": 553, "resource_name": name, "granularity": step,
            "capacity": 10, "time_slot_count": 6,
            "hours": [{"hour": bot.clock(m), "granularity": step,
                       "state": "full" if m in unavailable else "available",
                       "places_bookable": 0 if m in unavailable else 1}
                      for m in range(690, 1260, step)]}


def message(subject="Confirmez votre réservation : BU Joseph-Fourier - RDC Sud n°4",
            text="Mercredi 2 décembre 2026 de 11:30 à 14:30", stamp=None):
    return {"id": "12", "d": (stamp or time.time()) * 1000,
            "su": subject, "e": [{"t": "f", "a": "no-reply@affluences.com"},
                                  {"t": "t", "a": ACCOUNT.email}],
            "mp": [{"ct": "text/html", "content": text +
                    '<a href="https://affluences.com/fr/reservation/confirm?reservationToken=abcdefghijklmnop">Confirmer</a>'
                    '<a href="https://affluences.com/fr/reservation/cancel?reservationToken=abcdefghijklmnop">Annuler</a>'}]}


class CoreTests(unittest.TestCase):
    def test_password_with_comma_preserved_in_json(self):
        env = {"ACCOUNTS_JSON": json.dumps([{"email": ACCOUNT.email,
                 "username": ACCOUNT.username, "password": ACCOUNT.password}])}
        self.assertEqual(bot.load_accounts(env)[0].password, "p,ass")

    def test_misaligned_legacy_secrets_rejected(self):
        with self.assertRaises(ValueError):
            bot.load_accounts({"AFF_EMAIL": "a@x,b@y", "UGA_USER": "a", "UGA_PASS": "p,q"})

    def test_alias_does_not_increase_account_count(self):
        data = [{"email": "a@x", "username": "a", "password": "x"},
                {"email": "a+1@x", "username": "a", "password": "x"}]
        with self.assertRaises(ValueError):
            bot.load_accounts({"ACCOUNTS_JSON": json.dumps(data)})

    def test_only_exact_confirmation_links(self):
        good = "https://affluences.com/fr/reservation/confirm?reservationToken=abcdefghijklmnop"
        self.assertTrue(bot.confirmation_url(good))
        for value in [good.replace("confirm?", "cancel?"),
                      good.replace("affluences.com", "affluences.com.evil.test"),
                      good.replace("https:", "http:"), good.replace("/fr/reservation", "/tracking"),
                      good.replace("affluences.com", "user@affluences.com")]:
            self.assertFalse(bot.confirmation_url(value))

    def test_french_and_english_dates(self):
        self.assertEqual(bot.parse_slot("Mercredi 2 décembre 2026 de 11:30 à 14:30"),
                         ("2026-12-02", 690, 870))
        self.assertEqual(bot.parse_slot("Wednesday, December 2, 2026 from 5:30 PM to 9:00 PM"),
                         ("2026-12-02", 1050, 1260))

    def test_email_subject_controls_status(self):
        b, link = bot.parse_message(message(), ACCOUNT)
        self.assertEqual(b.status, "pending")
        self.assertIn("/confirm?", link)
        b, _ = bot.parse_message(message("Réservation confirmée : BU Joseph-Fourier - RDC Sud n°4"), ACCOUNT)
        self.assertEqual(b.status, "confirmed")
        b, _ = bot.parse_message(message("Your reservation has been cancelled : BU Joseph-Fourier - RDC Sud n°4"), ACCOUNT)
        self.assertEqual(b.status, "cancelled")

    def test_wrong_sender_and_recipient_ignored(self):
        m = message()
        m["e"][0]["a"] = "fake@other.example"
        self.assertIsNone(bot.parse_message(m, ACCOUNT))
        m = message()
        m["e"][1]["a"] = "b@etu.example"
        self.assertIsNone(bot.parse_message(m, ACCOUNT))

    def test_page_opening_is_not_confirmation(self):
        self.assertFalse(bot.page_confirmed("Confirmez votre réservation"))
        self.assertFalse(bot.page_confirmed("Votre réservation n'est pas confirmée"))
        self.assertFalse(bot.page_confirmed("Votre réservation est confirmée mais expirée"))
        self.assertTrue(bot.page_confirmed("Votre réservation a bien été confirmée"))
        self.assertTrue(bot.page_confirmed("Your reservation has been confirmed"))

    def test_room_priority_and_fallback(self):
        rooms = [resource("Etage Nord n°11"), resource("RDC Nord n°1"),
                 resource("RDC Sud n°5"), resource()]
        picked = bot.plan_next(rooms, [ACCOUNT], [], "2026-12-02", 690, 1260, 3)
        self.assertEqual(picked[0]["resource_name"], "RDC Sud n°4")
        self.assertEqual(picked[2:], (690, 870))
        picked = bot.plan_next([resource(unavailable=(690,)), resource("RDC Sud n°5")],
                               [ACCOUNT], [], "2026-12-02", 690, 1260, 3)
        self.assertEqual(picked[0]["resource_name"], "RDC Sud n°5")

    def test_three_accounts_never_cover_more_than_nine_hours(self):
        accounts = [bot.Account(f"{i}@x", str(i), "p") for i in range(3)]
        entries = []
        while True:
            result = bot.plan_next([resource()], accounts, entries, "2026-12-02", 690, 1260, 3)
            if result is None:
                break
            room, acc, begin, finish = result
            entries.append(bot.Booking(acc.email, "2026-12-02", begin, finish,
                                       room["resource_name"], "confirmed"))
        self.assertEqual(sum(x.end - x.start for x in entries), 540)
        self.assertEqual(entries[-1].end, 1230)

    def test_partial_availability_is_not_ignored(self):
        result = bot.plan_next([resource(unavailable=(750,))], [ACCOUNT], [],
                               "2026-12-02", 690, 1260, 3)
        self.assertEqual(result[2:], (690, 750))

    def test_uncertain_reservation_blocks_duplicate_and_consumes_quota(self):
        entries = [bot.Booking(ACCOUNT.email, "2026-12-02", 690, 870, "RDC Sud n°4", "uncertain")]
        self.assertIsNone(bot.plan_next([resource()], [ACCOUNT], entries, "2026-12-02", 690, 1260, 3))

    def test_insufficient_capacity_is_rejected(self):
        room = resource()
        room["capacity"] = 2
        self.assertIsNone(bot.plan_next([room], [ACCOUNT], [], "2026-12-02", 690, 1260, 3))

    def test_dates_no_weekends_and_bounds(self):
        days = bot.date_selection("rattrapage", 14, date(2026, 11, 18))
        self.assertEqual(len(days), 11)
        self.assertEqual(days[-1], "2026-12-02")
        self.assertEqual(bot.date_selection("2026-11-21", 14, date(2026, 11, 18)), [])
        with self.assertRaises(ValueError):
            bot.date_selection("2026-12-04", 14, date(2026, 11, 18))

    def test_journal_roundtrip_and_fail_closed_on_corruption(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "journal.json"
            j = bot.Journal(path)
            b = bot.Booking(ACCOUNT.email, "2099-12-02", 690, 870, "RDC Sud n°4", "pending", time.time())
            j.put(b)
            self.assertEqual(bot.Journal(path).day_entries(b.day)[0], b)
            path.write_text("bad")
            with self.assertRaises(ValueError):
                bot.Journal(path)


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_success_only_pending(self):
        response = AsyncMock()
        response.status = 201
        response.json.return_value = {"reservationToken": "SECRET"}
        http = AsyncMock()
        http.post.return_value = response
        with tempfile.TemporaryDirectory() as d:
            j = bot.Journal(Path(d) / "journal.json")
            b, _ = await bot.submit(http, j, resource(), ACCOUNT, "2099-12-02", 690, 870, 3)
            self.assertEqual(b.status, "pending")
            self.assertNotIn("SECRET", j.path.read_text())

    async def test_already_reserved_never_reported_confirmed(self):
        response = AsyncMock()
        response.status = 400
        response.json.return_value = {"error": "already_have_reservation"}
        http = AsyncMock()
        http.post.return_value = response
        with tempfile.TemporaryDirectory() as d:
            j = bot.Journal(Path(d) / "journal.json")
            b, _ = await bot.submit(http, j, resource(), ACCOUNT, "2099-12-02", 690, 870, 3)
            self.assertEqual(b.status, "uncertain")
            _, complete = bot.summary(j, [b.day], 690, 870)
            self.assertFalse(complete)

    async def test_quota_rejection_does_not_lock_slot(self):
        response = AsyncMock()
        response.status = 400
        response.json.return_value = {"error": "quota_overpassed"}
        http = AsyncMock()
        http.post.return_value = response
        with tempfile.TemporaryDirectory() as d:
            j = bot.Journal(Path(d) / "journal.json")
            await bot.submit(http, j, resource(), ACCOUNT, "2099-12-02", 690, 870, 3)
            self.assertEqual(j.day_entries("2099-12-02"), [])

    async def test_post_timeout_is_not_retried_and_journal_survives(self):
        http = AsyncMock()
        http.post.side_effect = TimeoutError("sensitive-url")
        with tempfile.TemporaryDirectory() as d:
            j = bot.Journal(Path(d) / "journal.json")
            with self.assertRaises(RuntimeError):
                await bot.submit(http, j, resource(), ACCOUNT, "2099-12-02", 690, 870, 3)
            self.assertEqual(http.post.await_count, 1)
            self.assertEqual(bot.Journal(j.path).day_entries("2099-12-02")[0].status, "uncertain")

    async def test_diagnostic_does_not_confirm_or_save(self):
        b, link = bot.parse_message(message(), ACCOUNT)
        mailbox = AsyncMock()
        mailbox.messages.return_value = [(b, link)]
        with tempfile.TemporaryDirectory() as d:
            j = bot.Journal(Path(d) / "journal.json")
            with patch.object(bot, "confirm", new_callable=AsyncMock) as click:
                await bot.reconcile([mailbox], j, [b.day], None, False)
                click.assert_not_called()
            self.assertFalse(j.path.exists())

    async def test_old_cancellation_cannot_erase_new_pending_request(self):
        old, _ = bot.parse_message(message(
            "Votre réservation a été annulée : BU Joseph-Fourier - RDC Sud n°4", stamp=time.time()-60), ACCOUNT)
        mailbox = AsyncMock()
        mailbox.messages.return_value = [(old, None)]
        with tempfile.TemporaryDirectory() as d:
            j = bot.Journal(Path(d) / "journal.json")
            new = bot.Booking(old.account, old.day, old.start, old.end, old.room, "pending", time.time())
            j.put(new, persist=False)
            await bot.reconcile([mailbox], j, [new.day], None, False)
            self.assertEqual(j.entries[new.key].status, "pending")

    async def test_cancelled_mail_wins_over_older_confirmation_request(self):
        pending, link = bot.parse_message(message(stamp=time.time()-60), ACCOUNT)
        cancelled, _ = bot.parse_message(message(
            "Votre réservation a été annulée : BU Joseph-Fourier - RDC Sud n°4"), ACCOUNT)
        mailbox = AsyncMock()
        mailbox.messages.return_value = [(cancelled, None), (pending, link)]
        with tempfile.TemporaryDirectory() as d:
            j = bot.Journal(Path(d) / "journal.json")
            with patch.object(bot, "confirm", new_callable=AsyncMock) as click:
                await bot.reconcile([mailbox], j, [pending.day], None, True)
                click.assert_not_called()
            self.assertEqual(j.entries[cancelled.key].status, "cancelled")


if __name__ == "__main__":
    unittest.main()
