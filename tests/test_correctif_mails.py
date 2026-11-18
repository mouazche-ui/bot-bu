import unittest
from unittest.mock import AsyncMock, patch

import correctif_mails as fix
import reserver as bot
from test_reserver import ACCOUNT, message


class ReaderTests(unittest.TestCase):
    def test_singleton_mime_parts_and_charset(self):
        m = message()
        m["mp"] = m["mp"][0]
        m["mp"]["ct"] = "text/html; charset=UTF-8"
        parsed = fix.parse_message(m, ACCOUNT)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed[0].start, 690)

    def test_ordinal_date_and_until(self):
        self.assertEqual(fix.expanded_slot(
            "Wednesday, December 2nd, 2026 from 11:30 am until 2:30 pm"),
            ("2026-12-02", 690, 870))

    def test_spaced_html_times(self):
        self.assertEqual(fix.expanded_slot("2 décembre 2026 de 11 : 30 à 14 : 30"),
                         ("2026-12-02", 690, 870))

    def test_diagnostic_redacts_urls_emails_and_tokens(self):
        value = fix.safe_text("x@y.test https://affluences.com/fr/reservation/confirm?reservationToken=SECRET "
                              "ddbbcb80-c2bb-4dda-be6d-b389f2e2dbe2")
        self.assertNotIn("SECRET", value)
        self.assertNotIn("x@y", value)
        self.assertNotIn("ddbbcb80", value)


class FullReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_message_loaded_even_if_search_has_partial_mime(self):
        mailbox = AsyncMock()
        mailbox.number, mailbox.account, mailbox.read_cache = 2, ACCOUNT, {}
        partial = message()
        partial["mp"] = [{"ct": "text/html"}]
        mailbox.search.return_value = {"m": [partial], "more": False}
        mailbox.soap.return_value = {"m": [message()]}
        result = await fix.full_messages(mailbox)
        self.assertEqual(len(result), 1)
        self.assertEqual(mailbox.soap.await_args.args[0], "GetMsgRequest")
        self.assertEqual(mailbox.soap.await_args.args[1]["m"]["read"], 0)

    async def test_unknown_future_message_still_blocks(self):
        mailbox = AsyncMock()
        mailbox.number, mailbox.account, mailbox.read_cache = 2, ACCOUNT, {}
        m = message(text="Corps inconnu")
        mailbox.search.return_value = {"m": [m], "more": False}
        mailbox.soap.return_value = {"m": [m]}
        with patch.object(bot, "log"):
            with self.assertRaises(RuntimeError):
                await fix.full_messages(mailbox)

    async def test_invalid_past_message_does_not_block_future_quota(self):
        mailbox = AsyncMock()
        mailbox.number, mailbox.account, mailbox.read_cache = 2, ACCOUNT, {}
        m = message(subject="Message inconnu : BU Joseph-Fourier - RDC Sud n°4",
                    text="Mercredi 2 décembre 2020 de 11:30 à 14:30")
        mailbox.search.return_value = {"m": [m], "more": False}
        mailbox.soap.return_value = {"m": [m]}
        self.assertEqual(await fix.full_messages(mailbox), [])
