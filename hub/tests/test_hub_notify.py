"""Notifications: what each kind sends (checked against a fake HTTP server on 127.0.0.1), failures that
must not raise or leak the URL, the notify tool's limits and images, and knocks going the same way.

Runs with the hub's own dependencies (the hub image), from the hub folder:
    python -m unittest discover -s tests -p 'test_hub*.py'      (pytest works too)
Every URL and value here is fake; nothing leaves this machine.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlsplit

if "HUB_DATA" not in os.environ:  # the first hub test module to load sets up a world for all of them
    _tmp = Path(tempfile.mkdtemp(prefix="deskmate-hub-test-"))
    (_tmp / "secrets").mkdir()
    (_tmp / "secrets" / "hub_token").write_text("fake-hub-token-for-tests\n")
    (_tmp / "exchange").mkdir()
    os.environ.update(
        {
            "HUB_PORT": "7850",
            "HUB_DATA": str(_tmp / "data"),
            "SECRETS_DIR": str(_tmp / "secrets"),
            "RUN_DIR": str(_tmp / "run"),
            "EXCHANGE_DIR": str(_tmp / "exchange"),
            "DESKMATE_OWNER": "Alex",
            "HOST_HOME": "/home/alex",
            "DESKMATE_DATA_DIR": "/home/alex/.local/share/deskmate",
            "DESK_NETWORK": "host",
            "DESK_MONITORS": "2",
            "DESK_MONITOR_SIZE": "1280x800",
            "TZ": "UTC",
            "NOTIFY_KIND": "none",
        }
    )

from app import config, db, knock, notify, tools  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes" * 4


class _Recorder(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        self.server.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
        self.send_response(self.server.status)
        self.send_header("content-length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


class FakeWebhook:
    """An HTTP server on 127.0.0.1 that records what it receives and answers with `status`."""

    def __enter__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
        self.server.requests = []
        self.server.status = 204
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/hook/fake-topic?auth=fake-access"

    @property
    def requests(self) -> list[dict]:
        return self.server.requests


def set_url(url: str) -> None:
    (Path(os.environ["SECRETS_DIR"]) / "notify_url").write_text(url + "\n")


def send(kind: str, url: str, *args, **kw) -> dict:
    set_url(url)
    with mock.patch.object(config, "NOTIFY_KIND", kind):
        return asyncio.run(notify.send(*args, **kw))


class PayloadTest(unittest.TestCase):
    def test_discord(self):
        with FakeWebhook() as hook:
            r = send("discord", hook.url, "@everyone build is green " + "x" * 3000, title="Deskmate · api · fix login")
            req = hook.requests[0]
        self.assertTrue(r["ok"])
        self.assertEqual(r["detail"], "sent to Discord")
        body = json.loads(req["body"])
        self.assertEqual(body["allowed_mentions"], {"parse": []})
        self.assertTrue(body["content"].startswith("**Deskmate · api · fix login**\n@everyone build is green"))
        self.assertLessEqual(len(body["content"]), notify.TEXT_CAP)
        self.assertTrue(body["content"].endswith("…"))
        self.assertIn("auth=fake-access", req["path"])

    def test_discord_with_images(self):
        with FakeWebhook() as hook:
            r = send("discord", hook.url, "done", title="T", images=[("shot.png", PNG, "image/png")])
            req = hook.requests[0]
        self.assertTrue(r["ok"])
        self.assertTrue(req["headers"]["content-type"].startswith("multipart/form-data"))
        self.assertIn(b'name="payload_json"', req["body"])
        self.assertIn(b'name="files[0]"; filename="shot.png"', req["body"])
        self.assertIn(PNG, req["body"])
        self.assertIn(b'"allowed_mentions": {"parse": []}', req["body"])

    def test_slack(self):
        with FakeWebhook() as hook:
            r = send("slack", hook.url, "deploy <!channel> & done", title="Deskmate · web", images=[("a.png", PNG, "image/png")])
            body = json.loads(hook.requests[0]["body"])
        self.assertEqual(body, {"text": "*Deskmate · web*\ndeploy &lt;!channel&gt; &amp; done"})
        self.assertTrue(r["ok"])
        self.assertIn("images not sent", r["detail"])

    def test_ntfy(self):
        with FakeWebhook() as hook:
            r = send("ntfy", hook.url, "Tests pass — ready for review", title="Deskmate · api")
            req = hook.requests[0]
        self.assertTrue(r["ok"])
        self.assertEqual(req["body"].decode(), "Tests pass — ready for review")
        self.assertTrue(req["headers"]["content-type"].startswith("text/plain"))
        query = parse_qs(urlsplit(req["path"]).query)
        self.assertEqual(query["title"], ["Deskmate · api"])
        self.assertEqual(query["auth"], ["fake-access"])  # the URL's own parameters are kept

    def test_webhook(self):
        with FakeWebhook() as hook:
            r = send("webhook", hook.url, "Migrated", title="Deskmate · db", images=[("s.png", PNG, "image/png")], event="knock")
            body = json.loads(hook.requests[0]["body"])
        self.assertTrue(r["ok"])
        self.assertEqual(r["detail"], "sent to the webhook")
        self.assertEqual((body["source"], body["event"], body["title"], body["text"]), ("deskmate", "knock", "Deskmate · db", "Migrated"))
        self.assertEqual(body["images"][0]["name"], "s.png")
        self.assertEqual(body["images"][0]["type"], "image/png")

    def test_unknown_kind_is_guessed_from_the_url(self):
        with FakeWebhook() as hook:
            r = send("teams", hook.url, "hello")
        self.assertEqual(r["kind"], "webhook")


class FailureTest(unittest.TestCase):
    def test_off_sends_nothing(self):
        with FakeWebhook() as hook:
            r = send("none", hook.url, "hello")
            self.assertEqual(hook.requests, [])
        self.assertEqual(r, {"ok": False, "kind": "none", "status": None, "detail": "notifications are off"})

    def test_error_status(self):
        with FakeWebhook() as hook:
            hook.server.status = 404
            r = send("slack", hook.url, "hello")
        self.assertEqual((r["ok"], r["status"], r["detail"]), (False, 404, "Slack answered 404"))

    def test_unreachable_never_raises_or_shows_the_url(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]  # closed again before the send: nothing listens there
        url = f"http://127.0.0.1:{port}/fake-secret-path"
        with self.assertLogs("app.notify", level="WARNING") as logs:
            r = send("discord", url, "hello")
        self.assertFalse(r["ok"])
        self.assertTrue(r["detail"].startswith("could not reach Discord ("))
        self.assertNotIn("fake-secret-path", r["detail"] + " ".join(logs.output))

    def test_bad_or_missing_url(self):
        self.assertEqual(send("webhook", "file:///etc/passwd", "x")["detail"], "the notification address is not an http(s) URL")
        self.assertEqual(send("webhook", "", "x")["detail"], "no notification address is set up")

    def test_old_entry_point(self):
        with FakeWebhook() as hook:
            set_url(hook.url)
            with mock.patch.object(config, "NOTIFY_KIND", "discord"):
                self.assertEqual(asyncio.run(notify.discord("hi")), 204)

    def test_guess_kind(self):
        cases = {
            "https://discord.com/api/webhooks/000/FAKE": "discord",
            "https://ptb.discord.com/api/webhooks/000/FAKE": "discord",
            "https://hooks.slack.com/services/T000/B000/FAKE": "slack",
            "https://ntfy.sh/fake-topic": "ntfy",
            "https://ntfy.example.org/fake-topic": "ntfy",
            "https://example.com/hook": "webhook",
            "": "webhook",
        }
        for url, kind in cases.items():
            with self.subTest(url=url):
                self.assertEqual(notify.guess_kind(url), kind)


class LimitsTest(unittest.TestCase):
    def setUp(self):
        notify._last.clear()
        db.put("notify.daily", "")

    def test_one_message_per_two_minutes_per_session(self):
        self.assertIsNone(notify.limit("s1", now=1000.0))
        notify.count("s1", now=1000.0)
        self.assertIn("try again in 61 s", notify.limit("s1", now=1060.0))
        self.assertIsNone(notify.limit("s2", now=1060.0))  # another session is not held back
        self.assertIsNone(notify.limit("s1", now=1000.0 + notify.SESSION_GAP))

    def test_forty_a_day_in_all(self):
        db.put("notify.daily", f"{notify._today()} {notify.DAILY_MAX - 1}")
        self.assertIsNone(notify.limit("fresh"))
        notify.count("fresh")
        self.assertIn("40 messages today already", notify.limit("another"))

    def test_a_new_day_starts_from_zero(self):
        db.put("notify.daily", f"2001-01-01 {notify.DAILY_MAX}")
        self.assertIsNone(notify.limit("s"))
        notify.count("s")
        self.assertEqual(db.get("notify.daily"), f"{notify._today()} 1")


class ImagesTest(unittest.TestCase):
    def test_load_images(self):
        folder = Path(tempfile.mkdtemp(prefix="deskmate-exchange-"))
        for name in ("a.png", "b.jpg", "c.webp", "d.gif", "e.png"):
            (folder / name).write_bytes(PNG)
        (folder / "notes.txt").write_text("x")
        (folder / "big.png").write_bytes(b"0" * (notify.MAX_IMAGE_BYTES + 1))
        names = ["../../etc/passwd", "notes.txt", "missing.png", "big.png", "a.png", "b.jpg", "c.webp", "d.gif", "e.png"]
        images, notes = notify.load_images(names, folder)
        self.assertEqual([(n, m) for n, _d, m in images], [("a.png", "image/png"), ("b.jpg", "image/jpeg"), ("c.webp", "image/webp"), ("d.gif", "image/gif")])
        self.assertEqual(
            notes,
            [
                "passwd: not an image (png, jpg, gif or webp)",
                "notes.txt: not an image (png, jpg, gif or webp)",
                "missing.png: not in the exchange folder",
                "big.png: over 8 MB",
                "e.png: more than 4 images",
            ],
        )


def ctx(session: str):
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers={"mcp-session-id": session})))


class NotifyToolTest(unittest.TestCase):
    def setUp(self):
        notify._last.clear()
        db.put("notify.daily", "")

    def call(self, session: str, text: str, **kw) -> str:
        return asyncio.run(tools.send_notification(text, ctx=ctx(session), **kw))

    def test_off(self):
        with mock.patch.object(config, "NOTIFY_KIND", "none"):
            self.assertEqual(self.call("tool-a", "hello"), "Not sent: notifications are off. Tell Alex in your reply instead.")

    def test_sends_once_then_waits(self):
        (config.EXCHANGE_DIR / "shot.png").write_bytes(PNG)
        with FakeWebhook() as hook, mock.patch.object(config, "NOTIFY_KIND", "webhook"):
            set_url(hook.url)
            first = self.call("tool-b", "Milestone: tests pass\nall 120", images=["shot.png", "notes.txt"])
            second = self.call("tool-b", "again")
            body = json.loads(hook.requests[0]["body"])
            self.assertEqual(len(hook.requests), 1)
        self.assertEqual(first, "Sent to the webhook; notes.txt: not an image (png, jpg, gif or webp).")
        self.assertTrue(second.startswith("Not sent: one message per 2 minutes per session; try again in "))
        self.assertTrue(body["title"].startswith("Deskmate · "))
        self.assertEqual(body["text"], "Milestone: tests pass\nall 120")
        self.assertEqual(len(body["images"]), 1)
        rows = db.q("select status, arg, note from activity where tool = 'notify' order by id")
        self.assertEqual([r["status"] for r in rows][-2:], ["ok", "refused"])

    def test_failures_are_reported_not_raised(self):
        with FakeWebhook() as hook, mock.patch.object(config, "NOTIFY_KIND", "slack"):
            hook.server.status = 500
            set_url(hook.url)
            self.assertEqual(self.call("tool-c", "hello"), "Not sent: Slack answered 500.")
        self.assertEqual(self.call("tool-d", "   "), "Not sent: the text is empty.")
        with mock.patch.object(notify, "send", side_effect=RuntimeError("boom")), mock.patch.object(config, "NOTIFY_KIND", "slack"):
            self.assertEqual(self.call("tool-e", "hello"), "Not sent: RuntimeError.")


class KnockTest(unittest.TestCase):
    def test_knock_notifies_and_waits(self):
        async def scenario():
            k = await knock.post("knock-session", "api · login", "Enter the one-time code, then press Resume")
            self.assertEqual(knock.waiting()[0]["notified"], k.notified)
            self.assertTrue(knock.answer(k.id, "fake-123456"))
            return k, await knock.wait(k, 5)

        with FakeWebhook() as hook, mock.patch.object(config, "NOTIFY_KIND", "webhook"):
            set_url(hook.url)
            k, answer = asyncio.run(scenario())
            body = json.loads(hook.requests[0]["body"])
        self.assertEqual(answer, "fake-123456")
        self.assertEqual(k.notified, "sent to the webhook")
        self.assertEqual(body["event"], "knock")
        self.assertEqual(body["title"], "Deskmate · api · login needs you")
        self.assertIn(f"Answer at {config.HUB_URL}", body["text"])
        self.assertEqual(knock.waiting(), [])

    def test_knock_without_notifications_times_out(self):
        async def scenario():
            k = await knock.post("knock-quiet", "web", "Solve the CAPTCHA")
            return k, await knock.wait(k, 0.05)

        with mock.patch.object(config, "NOTIFY_KIND", "none"):
            k, answer = asyncio.run(scenario())
        self.assertIsNone(answer)
        self.assertEqual(k.notified, "notifications are off")


if __name__ == "__main__":
    unittest.main()
