"""web.py's caching headers and the desk-down answers.

Static files must be revalidated (no-cache), so an update never leaves a browser on last version's ES
modules; "/" (the app or the sign-in page, by cookie) is never stored. With the desk down, the Downloads
list answers 503 and one sentence instead of a 500 with a traceback in the log every few seconds.
Every value here is fake. Nothing reaches a desk, a browser or the network.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

from starlette.testclient import TestClient  # noqa: E402

from app import auth, config, desk, web  # noqa: E402


def client(signed_in: bool = True) -> TestClient:
    c = TestClient(web.app, base_url=f"http://127.0.0.1:{config.PORT}")
    if signed_in:
        c.cookies.set(auth.COOKIE, auth.ui_secret())
    return c


class CachingTest(unittest.TestCase):
    def test_static_files_are_revalidated(self):
        c = client(signed_in=False)
        for path in ("/static/app.js", "/static/secretary.js", "/static/secretary/common.js", "/static/style.css"):
            with self.subTest(path=path):
                r = c.get(path)
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.headers["cache-control"], "no-cache")
                again = c.get(path, headers={"if-none-match": r.headers["etag"]})
                self.assertEqual(again.status_code, 304)  # revalidation is cheap

    def test_the_page_and_the_sign_in_page_are_never_stored(self):
        r = client().get("/")
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertIn('id="view-secretary"', r.text)
        r = client(signed_in=False).get("/")
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertNotIn('id="view-secretary"', r.text)


class DeskDownTest(unittest.TestCase):
    """RUN_DIR has no deskd socket here, so the desk is down exactly as when its container is stopped."""

    def test_downloads_answer_503_with_one_sentence(self):
        for path in ("/api/downloads", "/api/downloads/report.pdf"):
            with self.subTest(path=path):
                r = client().get(path)
                self.assertEqual(r.status_code, 503)
                self.assertEqual(r.json(), {"detail": web.DESK_DOWN})

    def test_a_missing_file_is_404_while_the_desk_is_up(self):
        async def act(action, **params):
            if action == "list_files":
                return {"entries": [{"name": "a.pdf", "size": 3}, {"name": "b.crdownload", "size": 1},
                                    {"name": "dir", "dir": True}]}
            raise desk.DeskError("ENOENT: no such file")

        with mock.patch.object(desk, "act", act):
            self.assertEqual(client().get("/api/downloads").json(), [{"name": "a.pdf", "size": 3}])
            r = client().get("/api/downloads/gone.pdf")
            self.assertEqual(r.status_code, 404)

    def test_downloads_still_need_the_cookie(self):
        self.assertEqual(client(signed_in=False).get("/api/downloads").status_code, 401)


if __name__ == "__main__":
    unittest.main()
