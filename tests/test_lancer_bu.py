import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import lancer_bu as launch
import reserver as bot
from test_reserver import ACCOUNT, resource


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_403_detail_logged_without_token_and_without_retry(self):
        session = AsyncMock()
        response = AsyncMock()
        response.status = 403
        response.text.return_value = '{"error":"forbidden","errorMessage":"https://affluences.com/?token=SECRET"}'
        session.request.post.return_value = response
        client = launch.BookingHTTP(session)
        with patch.object(bot, "log") as output:
            result = await client.post("https://reservation.affluences.com/api/reserve/553", data={})
        self.assertIs(result, response)
        self.assertEqual(session.request.post.await_count, 1)
        self.assertEqual(client.last_status, 403)
        self.assertNotIn("SECRET", str(output.call_args_list))
        self.assertEqual(session.request.post.await_args.kwargs["headers"]["Referer"], launch.PAGE_URL)

    async def test_definite_new_403_rejected_but_previous_uncertainty_preserved(self):
        session = AsyncMock()
        response = AsyncMock()
        response.status = 403
        response.text.return_value = '{"error":"forbidden"}'
        response.json.return_value = {"error": "forbidden"}
        session.request.post.return_value = response
        with tempfile.TemporaryDirectory() as directory:
            journal = bot.Journal(Path(directory) / "journal.json")
            old = bot.Booking(ACCOUNT.email, "2099-12-01", 690, 870, "RDC Sud n°4", "uncertain", 0)
            journal.put(old)
            with patch.object(launch, "SESSION", session), patch.object(bot, "log"):
                with self.assertRaises(RuntimeError):
                    await launch.browser_submit(None, journal, resource(), ACCOUNT, "2099-12-02", 690, 870, 3)
            self.assertEqual(journal.day_entries("2099-12-02"), [])
            self.assertEqual(journal.entries[old.key].status, "uncertain")
            self.assertEqual(session.request.post.await_count, 1)

    async def test_missing_browser_session_sends_no_request(self):
        http = AsyncMock()
        with patch.object(launch, "SESSION", None):
            with self.assertRaises(RuntimeError):
                await launch.browser_submit(http, None, resource(), ACCOUNT, "2099-12-02", 690, 870, 3)
        http.post.assert_not_called()

    async def test_telegram_whitespace_trimmed_and_errors_reported(self):
        session = AsyncMock()
        response = AsyncMock()
        response.ok, response.status = False, 401
        response.json.return_value = {"ok": False, "description": "Unauthorized"}
        session.request.get.return_value = response
        with patch.dict("os.environ", {"TG_TOKEN": " token-secret \n"}), patch.object(launch, "SESSION", session):
            with patch.object(bot, "log") as output:
                self.assertFalse(await launch.telegram_call("getMe"))
        self.assertEqual(session.request.get.await_args.args[0], "https://api.telegram.org/bottoken-secret/getMe")
        self.assertNotIn("token-secret", str(output.call_args_list))
        self.assertIn("Unauthorized", str(output.call_args_list))

    async def test_network_exception_does_not_expose_telegram_url(self):
        session = AsyncMock()
        session.request.get.side_effect = RuntimeError("https://api.telegram.org/botSECRET/getMe")
        with patch.dict("os.environ", {"TG_TOKEN": "SECRET"}), patch.object(launch, "SESSION", session):
            with patch.object(bot, "log") as output:
                self.assertFalse(await launch.telegram_call("getMe"))
        self.assertNotIn("SECRET", str(output.call_args_list))


class BodyTests(unittest.TestCase):
    def test_html_summary_is_title_only(self):
        result = launch.response_summary(None, '<html><title>Access denied</title><script>SECRET</script></html>')
        self.assertEqual(result, "Access denied")
