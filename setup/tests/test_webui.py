"""Tests for the web wizard's server (deskmate_setup/webui.py), run against the fake steps in test_webui_fake.py.

They cover the one-time key and the cookie, the Host, Origin and Sec-Fetch-Site checks, the static files and
their headers, every API route, how secrets are kept off the page, install runs (with a failure and a retry),
and how serve() starts, prints its link and stops with the right exit code.
"""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import re
import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from deskmate_setup import webui  # noqa: E402
from test_webui_fake import (  # noqa: E402
    API_KEY, DISCORD_URL, FAKE_HUB_TOKEN, GOOD_TOKEN, REVOKED_TOKEN, STEP_IDS, FakeSteps,
)

WEB = HERE.parent / "deskmate_setup" / "web"


class Client:
    """Raw HTTP to the wizard, so a test can send any Host, Origin or Cookie it likes."""

    def __init__(self, port: int, key: str):
        self.port, self.key, self.cookie = port, key, ""

    def request(self, method, path, body=None, host=None, origin="auto", cookie=True, headers=None, raw_body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        h = {"Host": "127.0.0.1:%d" % self.port if host is None else host}
        if host == "":
            del h["Host"]  # a request with no Host header at all
        if cookie and self.cookie:
            h["Cookie"] = "other_app=1; " + self.cookie + "; weird=a=b"
        data = raw_body
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        if method == "POST":
            if origin == "auto":
                h["Origin"] = "http://127.0.0.1:%d" % self.port
            elif origin:
                h["Origin"] = origin
            h["Content-Length"] = str(len(data or b""))
        h.update(headers or {})
        for k, v in h.items():
            conn.putheader(k, v)
        conn.endheaders(data)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, raw

    def login(self):
        status, headers, _ = self.request("GET", "/?key=" + self.key, cookie=False)
        if "set-cookie" in headers:
            self.cookie = headers["set-cookie"].split(";")[0]
        return status, headers

    def api(self, method, path, body=None, **kw):
        status, _, raw = self.request(method, path, body, **kw)
        return status, json.loads(raw.decode("utf-8") or "{}"), raw.decode("utf-8")


class Server(Client):
    """A wizard on a free port with the fake steps; the HTTP server runs on a thread."""

    def __init__(self, fake=None):
        self.fake = fake or FakeSteps(speed=0.001)
        self.out = io.StringIO()
        self.wiz = webui.Wizard(self.fake, out=self.out)
        self.httpd = webui.make_server(self.wiz, 0)
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        super().__init__(self.wiz.port, self.wiz.key)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def values(self):
        return self.api("GET", "/api/model")[1]["values"]

    def run(self, values=None, timeout=20.0):
        status, body, _ = self.api("POST", "/api/apply", {"values": values if values is not None else self.values()})
        assert status == 202, body
        return self.wait(body["run"], timeout)

    def wait(self, rid, timeout=20.0):
        events, deadline = [], time.time() + timeout
        while time.time() < deadline:
            status, body, raw = self.api("GET", "/api/run/%s?since=%d" % (rid, len(events)))
            assert status == 200, body
            events += body["events"]
            if body["done"]:
                return events, body, raw
            time.sleep(0.01)
        raise AssertionError("the run did not finish")


@contextlib.contextmanager
def quiet_stderr():
    old, sys.stderr = sys.stderr, io.StringIO()
    try:
        yield sys.stderr
    finally:
        sys.stderr = old


class ServerCase(unittest.TestCase):
    fake_kw = {}

    def setUp(self):
        self.s = Server(FakeSteps(speed=0.001, **self.fake_kw))

    def tearDown(self):
        self.s.close()


class KeyAndCookieTest(ServerCase):
    def test_the_key_sets_a_strict_httponly_cookie_and_redirects(self):
        status, headers = self.s.login()
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/")
        cookie = headers["set-cookie"]
        self.assertTrue(cookie.startswith("deskmate_setup_%d=" % self.s.port))
        for part in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(part, cookie)
        self.assertNotIn(self.s.key, cookie)

    def test_the_key_works_once(self):
        self.s.login()
        status, headers, _ = self.s.request("GET", "/?key=" + self.s.key, cookie=False)
        self.assertEqual(status, 403)
        self.assertNotIn("set-cookie", headers)
        status, headers, _ = self.s.request("GET", "/?key=" + self.s.key)  # the same tab, opened again
        self.assertEqual(status, 303)
        self.assertNotIn("set-cookie", headers)

    def test_a_wrong_key_gets_nothing(self):
        status, headers, _ = self.s.request("GET", "/?key=not-the-key", cookie=False)
        self.assertEqual(status, 403)
        self.assertNotIn("set-cookie", headers)
        self.assertFalse(self.s.wiz.key_used)

    def test_the_api_needs_the_cookie(self):
        self.assertEqual(self.s.api("GET", "/api/model")[0], 401)
        self.s.login()
        status, body, _ = self.s.api("GET", "/api/model")
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], 1)
        self.assertEqual([st["id"] for st in body["steps"]], STEP_IDS)
        self.s.cookie = "deskmate_setup_%d=forged" % self.s.port
        self.assertEqual(self.s.api("GET", "/api/model")[0], 401)
        self.s.cookie = "deskmate_setup_1=" + self.s.wiz.session  # right value, another wizard's name
        self.assertEqual(self.s.api("GET", "/api/model")[0], 401)

    def test_the_page_itself_needs_no_cookie(self):
        status, headers, raw = self.s.request("GET", "/", cookie=False)
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("text/html"))
        self.assertIn(b"wizard.js", raw)

    def test_the_key_is_never_logged(self):
        with quiet_stderr() as err:
            self.s.login()
            self.s.request("GET", "/?key=wrong", cookie=False)
        self.assertNotIn(self.s.key, err.getvalue())
        self.assertNotIn(self.s.key, self.s.out.getvalue())


class HostAndOriginTest(ServerCase):
    def setUp(self):
        super().setUp()
        self.s.login()

    def test_only_this_server_as_host(self):
        p = self.s.port
        for host in ("evil.example:%d" % p, "127.0.0.1:1", "127.0.0.1", "localhost", "[::1]:%d" % p,
                     "127.0.0.1.nip.io:%d" % p, "", "127.0.0.1:%d.evil.example" % p):
            status, _, _ = self.s.request("GET", "/", host=host)
            self.assertEqual(status, 400, host)
            status, _, _ = self.s.request("GET", "/api/model", host=host)
            self.assertEqual(status, 400, host)
        self.assertEqual(self.s.request("GET", "/", host="localhost:%d" % p)[0], 200)
        self.assertEqual(self.s.request("GET", "/", host="LOCALHOST:%d" % p)[0], 200)

    def test_post_needs_this_servers_origin(self):
        p = self.s.port
        body = {"step": "you", "values": {}}
        for origin in (None, "http://evil.example", "http://localhost:%d" % p, "null", "http://127.0.0.1:%d" % (p + 1),
                       "https://127.0.0.1:%d" % p):
            status, _, _ = self.s.api("POST", "/api/validate", body, origin=origin)
            self.assertEqual(status, 403, origin)
        self.assertEqual(self.s.api("POST", "/api/validate", body)[0], 200)
        # Origin follows the Host the browser used
        status, _, _ = self.s.api("POST", "/api/validate", body, host="localhost:%d" % p, origin="http://localhost:%d" % p)
        self.assertEqual(status, 200)

    def test_post_needs_json(self):
        status, _, _ = self.s.request("POST", "/api/validate", raw_body=b"step=you", headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 415)
        status, _, _ = self.s.request("POST", "/api/validate", raw_body=b"{not json", headers={"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        status, _, _ = self.s.request("POST", "/api/validate", raw_body=b"[1, 2]", headers={"Content-Type": "application/json"})
        self.assertEqual(status, 400)

    def test_a_too_large_body_is_refused(self):
        status, _, _ = self.s.request("POST", "/api/validate", headers={"Content-Type": "application/json", "Content-Length": str(webui.MAX_BODY + 1)}, raw_body=b"")
        self.assertEqual(status, 413)

    def test_other_sites_are_refused_even_with_the_cookie(self):
        for site in ("cross-site", "same-site"):  # same-site: another port on 127.0.0.1
            self.assertEqual(self.s.api("GET", "/api/model", headers={"Sec-Fetch-Site": site})[0], 403, site)
            self.assertEqual(self.s.api("POST", "/api/validate", {"step": "you"}, headers={"Sec-Fetch-Site": site})[0], 403, site)
        self.assertEqual(self.s.api("GET", "/api/model", headers={"Sec-Fetch-Site": "same-origin"})[0], 200)

    def test_an_absolute_request_target_is_refused(self):
        status, _, _ = self.s.request("GET", "http://127.0.0.1:%d/api/model" % self.s.port)
        self.assertEqual(status, 400)

    def test_other_methods_are_refused(self):
        for method in ("PUT", "DELETE", "PATCH", "OPTIONS"):
            self.assertEqual(self.s.request(method, "/api/model")[0], 405, method)


class StaticTest(ServerCase):
    def test_the_assets(self):
        for path, ctype in (("/", "text/html"), ("/index.html", "text/html"), ("/wizard.css", "text/css"),
                            ("/wizard.js", "text/javascript"), ("/favicon.svg", "image/svg+xml"),
                            ("/fonts/IBMPlexSans-latin.woff2", "font/woff2"), ("/fonts/OFL.txt", "text/plain")):
            status, headers, raw = self.s.request("GET", path, cookie=False)
            self.assertEqual(status, 200, path)
            self.assertTrue(headers["content-type"].startswith(ctype), (path, headers["content-type"]))
            self.assertEqual(int(headers["content-length"]), len(raw))

    def test_nothing_outside_the_web_folder(self):
        for path in ("/../webui.py", "/%2e%2e/webui.py", "/fonts/../../webui.py", "/fonts/%2e%2e/%2e%2e/webui.py",
                     "/web/index.html", "/.hidden", "/webui.py", "/fonts/", "/fonts/x.woff2", "//etc/passwd",
                     "/fonts/..%2f..%2fwebui.py", "/index.html%00.css"):
            self.assertEqual(self.s.request("GET", path, cookie=False)[0], 404, path)

    def test_security_headers(self):
        _, h, _ = self.s.request("GET", "/", cookie=False)
        csp = h["content-security-policy"]
        for part in ("default-src 'none'", "script-src 'self'", "style-src 'self'", "connect-src 'self'", "frame-ancestors 'none'"):
            self.assertIn(part, csp)
        self.assertNotIn("unsafe", csp)
        self.assertEqual(h["x-content-type-options"], "nosniff")
        self.assertEqual(h["x-frame-options"], "DENY")
        self.assertEqual(h["referrer-policy"], "no-referrer")
        self.assertEqual(h["cache-control"], "no-store")
        self.s.login()
        _, h, _ = self.s.request("GET", "/api/model")
        self.assertEqual(h["cache-control"], "no-store")
        self.assertTrue(h["content-type"].startswith("application/json"))

    def test_the_page_needs_nothing_from_another_host_and_nothing_inline(self):
        html = (WEB / "index.html").read_text(encoding="utf-8")
        css = (WEB / "wizard.css").read_text(encoding="utf-8")
        js = (WEB / "wizard.js").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"<script(?![^>]*\bsrc=)", html), "inline script")
        self.assertNotIn("<style", html)
        for name, text in (("index.html", html), ("wizard.js", js)):
            self.assertIsNone(re.search(r"\sstyle=|\bon[a-z]+=\"", text), name)
            self.assertIsNone(re.search(r"(?:src|href)=[\"']https?:", text), name)
        self.assertIsNone(re.search(r"url\(\s*[\"']?https?:|@import", css))
        self.assertIsNone(re.search(r"\b(?:fetch|open)\(\s*[\"']https?:", js))

    def test_fonts_are_self_hosted_with_their_licence(self):
        css = (WEB / "wizard.css").read_text(encoding="utf-8")
        urls = re.findall(r"url\(([^)]+\.woff2)\)", css)
        self.assertEqual(len(urls), 4)
        for u in urls:
            data = (WEB / u).read_bytes()
            self.assertEqual(data[:4], b"wOF2", u)
        ofl = (WEB / "fonts" / "OFL.txt").read_text(encoding="utf-8")
        for words in ("SIL OPEN FONT LICENSE Version 1.1", "IBM Corp.", "Bricolage Grotesque Project Authors"):
            self.assertIn(words, ofl)


class ApiTest(ServerCase):
    def setUp(self):
        super().setUp()
        self.s.login()

    def test_the_model_passes_through_with_server_facts(self):
        status, body, _ = self.s.api("GET", "/api/model")
        self.assertEqual(status, 200)
        self.assertEqual(body["mode"], "install")
        self.assertEqual(body["server"]["port"], self.s.port)
        self.assertIsNone(body["server"]["run"])
        self.assertFalse(body["server"]["signin"])
        self.assertEqual(body["secrets"], {})
        tok = [f for st in body["steps"] for f in st["fields"] if f["key"] == "claude-token"][0]
        self.assertEqual(tok["value"], {"set": False, "masked": ""})

    def test_validate_reports_errors_and_keeps_the_step(self):
        values = self.s.values()
        values["DESKMATE_OWNER"] = "  "
        status, body, _ = self.s.api("POST", "/api/validate", {"step": "you", "values": values})
        self.assertEqual(status, 200)
        self.assertIn("DESKMATE_OWNER", body["errors"])
        self.assertEqual(body["step"]["id"], "you")
        self.assertNotIn("model", body)

    def test_a_valid_step_comes_back_with_the_new_model(self):
        values = self.s.values()
        values["SECRETARY"] = "off"
        status, body, _ = self.s.api("POST", "/api/validate", {"step": "secretary", "values": values})
        self.assertEqual(body["errors"], {})
        skipped = [st["id"] for st in body["model"]["steps"] if st.get("skip")]
        self.assertEqual(skipped, ["sessions", "docs"])

    def test_answers_survive_a_reload(self):
        values = self.s.values()
        values["DESKMATE_OWNER"] = "Sam"
        self.s.api("POST", "/api/model", {"values": values})
        self.assertEqual(self.s.values()["DESKMATE_OWNER"], "Sam")

    def test_action_names_are_checked(self):
        for name in ("../x", "", "Recheck", "a" * 41):
            self.assertEqual(self.s.api("POST", "/api/action", {"name": name, "values": {}})[0], 400, name)

    def test_a_checked_token_stays_on_the_server(self):
        values = self.s.values()
        values["claude-token"] = GOOD_TOKEN
        status, body, raw = self.s.api("POST", "/api/action", {"name": "check_token", "values": values})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertNotIn(GOOD_TOKEN, raw)
        view = body["secrets"]["claude-token"]
        self.assertTrue(view["set"])
        self.assertTrue(view["masked"].endswith(GOOD_TOKEN[-4:]))
        self.assertLess(len(view["masked"]), 20)
        status, model, raw = self.s.api("GET", "/api/model")
        self.assertNotIn(GOOD_TOKEN, raw)
        self.assertEqual(model["values"]["claude-token"]["masked"], view["masked"])
        self.assertNotIn(GOOD_TOKEN, json.dumps(self.s.wiz.values))
        # the page sends the mask back; the install gets the token
        events, final, raw = self.s.run(model["values"])
        self.assertTrue(final["ok"])
        self.assertEqual(self.s.fake.applied[-1]["claude-token"], GOOD_TOKEN)
        self.assertNotIn(GOOD_TOKEN, raw)

    def test_a_refused_token_is_not_kept_or_echoed(self):
        for tok in (REVOKED_TOKEN, API_KEY, "sk-ant-oat01-short"):
            values = self.s.values()
            values["claude-token"] = tok
            status, body, raw = self.s.api("POST", "/api/action", {"name": "check_token", "values": values})
            self.assertFalse(body["ok"], tok)
            self.assertNotIn(tok, raw)
            self.assertEqual(body["secrets"], {})
        self.s.run()
        self.assertEqual(self.s.fake.applied[-1]["claude-token"], {"set": False, "masked": ""})

    def test_a_webhook_is_taken_by_validate_and_tested_by_its_mask(self):
        values = self.s.values()
        values.update({"NOTIFY_KIND": "discord", "notify-url": DISCORD_URL})
        status, body, raw = self.s.api("POST", "/api/validate", {"step": "notify", "values": values})
        self.assertEqual(body["errors"], {})
        self.assertNotIn(DISCORD_URL, raw)
        self.assertNotIn(DISCORD_URL.rsplit("/", 1)[1], raw)
        self.assertEqual(body["secrets"]["notify-url"]["masked"], "https://discord.com/api/webhooks/1301…/••••••••")
        values["notify-url"] = body["secrets"]["notify-url"]
        status, body, raw = self.s.api("POST", "/api/action", {"name": "test_notify", "values": values})
        self.assertTrue(body["ok"])
        self.assertEqual(self.s.fake.actions[-1][0], "test_notify")

    def test_a_bad_webhook_is_refused_and_not_kept(self):
        values = self.s.values()
        values.update({"NOTIFY_KIND": "discord", "notify-url": "https://example.com/hooks/abc"})
        status, body, raw = self.s.api("POST", "/api/validate", {"step": "notify", "values": values})
        self.assertIn("notify-url", body["errors"])
        self.assertEqual(body["secrets"], {})
        self.assertNotIn("notify-url", self.s.wiz.pending)

    def test_discard_forgets_answers_and_secrets(self):
        values = self.s.values()
        values["claude-token"] = GOOD_TOKEN
        values["DESKMATE_OWNER"] = "Sam"
        self.s.api("POST", "/api/action", {"name": "check_token", "values": values})
        self.assertEqual(self.s.api("POST", "/api/discard", {})[0], 200)
        model = self.s.api("GET", "/api/model")[1]
        self.assertEqual(model["values"]["DESKMATE_OWNER"], "Alex")
        self.assertEqual(model["secrets"], {})

    def test_a_provider_error_is_a_500_and_the_server_lives_on(self):
        real = self.s.fake.model
        calls = []

        def broken(values=None):
            if not calls:
                calls.append(1)
                raise RuntimeError("model broke near " + GOOD_TOKEN)
            return real(values)

        self.s.fake.model = broken
        with quiet_stderr() as err:
            status, body, raw = self.s.api("GET", "/api/model")
        self.assertEqual(status, 500)
        self.assertIn("RuntimeError", body["error"])
        self.assertNotIn(GOOD_TOKEN, raw)
        self.assertNotIn(GOOD_TOKEN, err.getvalue())
        self.assertIn("Traceback", err.getvalue())
        self.assertEqual(self.s.api("GET", "/api/model")[0], 200)

    def test_ping_and_unknown_routes(self):
        self.assertEqual(self.s.api("POST", "/api/ping", {})[0], 200)
        self.assertEqual(self.s.api("GET", "/api/nothing")[0], 404)
        self.assertEqual(self.s.api("POST", "/api/nothing", {})[0], 404)
        self.assertEqual(self.s.api("GET", "/api/run/nope")[0], 404)
        self.assertEqual(self.s.api("GET", "/api/validate")[0], 404)


class RunTest(ServerCase):
    def setUp(self):
        super().setUp()
        self.s.login()

    def test_an_install_streams_its_phases_and_hands_the_page_the_signin_link(self):
        events, final, raw = self.s.run()
        phases = []
        for ev in events:
            for k in ("phase", "level", "text", "ts", "n", "i"):
                self.assertIn(k, ev)
            self.assertIn(ev["level"], webui.LEVELS)
            if ev["phase"] not in phases:
                phases.append(ev["phase"])
        self.assertEqual(phases, ["Save settings", "Prepare folders", "Build images", "Start Deskmate", "Connect Claude Code", "Health check"])
        self.assertEqual([ev["i"] for ev in events], list(range(len(events))))
        self.assertTrue(final["ok"])
        self.assertTrue(final["result"]["signin"])
        # The log masks the link; the result the page shows on the last step has it whole.
        self.assertNotIn(FAKE_HUB_TOKEN, json.dumps(events))
        self.assertTrue(any("login?t=" in ev["text"] for ev in events))
        self.assertEqual(final["result"]["signin_url"], "http://127.0.0.1:7800/login?t=" + FAKE_HUB_TOKEN)
        server = self.s.api("GET", "/api/model")[1]["server"]
        self.assertEqual(server["run"], {"id": final["id"], "done": True, "ok": True})
        self.assertTrue(server["signin"])
        out = self.s.out.getvalue()
        self.assertIn("Build images", out)
        self.assertIn(FAKE_HUB_TOKEN, out)  # the terminal prints the link, like ./deskmate open
        _, again, _ = self.s.api("GET", "/api/run/%s?since=3" % final["id"])
        self.assertEqual(again["events"][0]["i"], 3)
        self.assertEqual(again["n"], len(events))

    def test_the_signin_link_after_install_only(self):
        self.assertEqual(self.s.api("POST", "/api/signin", {})[0], 404)
        self.assertEqual(self.s.request("GET", "/signin")[0], 404)
        self.s.run()
        status, body, _ = self.s.api("POST", "/api/signin", {})
        self.assertEqual(status, 200)
        self.assertIn(FAKE_HUB_TOKEN, body["url"])
        status, headers, _ = self.s.request("GET", "/signin")
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], body["url"])
        self.assertEqual(self.s.request("GET", "/signin", cookie=False)[0], 401)
        self.assertEqual(self.s.request("GET", "/signin", headers={"Sec-Fetch-Site": "cross-site"})[0], 401)
        self.assertEqual(self.s.api("POST", "/api/signin", {}, origin="http://evil.example")[0], 403)

    def test_the_api_is_busy_while_installing(self):
        self.s.fake.speed = 0.05
        status, body, _ = self.s.api("POST", "/api/apply", {"values": self.s.values()})
        rid = body["run"]
        values = self.s.values()
        self.assertEqual(self.s.api("POST", "/api/validate", {"step": "you", "values": values})[0], 409)
        self.assertEqual(self.s.api("POST", "/api/action", {"name": "recheck", "values": values})[0], 409)
        self.assertEqual(self.s.api("POST", "/api/apply", {"values": values})[0], 409)
        self.assertEqual(self.s.api("POST", "/api/cancel", {})[0], 409)
        self.assertEqual(self.s.api("POST", "/api/finish", {})[0], 409)
        self.assertEqual(self.s.api("POST", "/api/discard", {})[0], 409)
        status, model, _ = self.s.api("GET", "/api/model")
        self.assertEqual(model["server"]["run"]["done"], False)
        self.assertFalse(self.s.wiz.stopped.is_set())
        self.s.wait(rid)


class FailedRunTest(ServerCase):
    fake_kw = {"fail_once": True}

    def setUp(self):
        super().setUp()
        self.s.login()

    def test_a_failed_build_then_a_retry(self):
        events, final, _ = self.s.run()
        self.assertFalse(final["ok"])
        self.assertEqual(final["result"]["failed_phase"], "Build images")
        self.assertIn("timed out", final["result"]["hint"])
        self.assertFalse(final["result"]["signin"])
        self.assertEqual(events[-1]["level"], "error")
        self.assertIn("Install stopped at Build images", self.s.out.getvalue())
        events, final, _ = self.s.run()
        self.assertTrue(final["ok"])
        self.assertEqual(len(self.s.fake.applied), 2)

    def test_an_exception_in_apply_is_a_failed_run(self):
        def boom(values, emit):
            emit({"phase": "Save settings", "level": "info", "text": "starting"})
            raise ValueError("broke while saving " + GOOD_TOKEN)

        self.s.fake.apply = boom
        with quiet_stderr() as err:
            events, final, raw = self.s.run()
        self.assertFalse(final["ok"])
        self.assertEqual(final["result"]["failed_phase"], "Save settings")
        self.assertIn("ValueError", final["result"]["hint"])
        self.assertNotIn(GOOD_TOKEN, raw)
        self.assertNotIn(GOOD_TOKEN, err.getvalue())
        self.assertIn("ValueError", err.getvalue())


class LeakTest(ServerCase):
    fake_kw = {"leak": True}

    def test_a_secret_in_a_log_line_is_masked(self):
        self.s.login()
        values = self.s.values()
        values["claude-token"] = GOOD_TOKEN
        body = self.s.api("POST", "/api/action", {"name": "check_token", "values": values})[1]
        values["claude-token"] = body["secrets"]["claude-token"]
        events, final, raw = self.s.run(values)
        self.assertNotIn(GOOD_TOKEN, raw)
        self.assertTrue(any("token to save: sk-ant-oat" in ev["text"] for ev in events))
        self.assertNotIn(GOOD_TOKEN, self.s.out.getvalue())


class EndedTest(ServerCase):
    def test_after_the_end_everything_is_gone(self):
        self.s.login()
        self.s.wiz.stop(0)
        status, body, _ = self.s.api("GET", "/api/model")
        self.assertEqual(status, 410)
        self.assertEqual(self.s.request("GET", "/")[0], 410)


class MaskTest(unittest.TestCase):
    def test_masks(self):
        m = webui.mask(GOOD_TOKEN)
        self.assertTrue(m.startswith("sk-ant-oat"))
        self.assertTrue(m.endswith(GOOD_TOKEN[-4:]))
        self.assertLess(len(m), 20)
        self.assertEqual(webui.mask(DISCORD_URL), "https://discord.com/api/webhooks/1301…/••••••••")
        self.assertEqual(webui.mask(""), "")

    def test_clean_reaches_every_string(self):
        obj = {"a": ["got " + GOOD_TOKEN], "b": {"c": DISCORD_URL, "d": 3}, "e": "http://127.0.0.1:7800/login?t=" + FAKE_HUB_TOKEN}
        text = json.dumps(webui._clean(obj, []))
        for secret in (GOOD_TOKEN, DISCORD_URL, FAKE_HUB_TOKEN):
            self.assertNotIn(secret, text)
        self.assertIn('"d": 3', text)
        self.assertNotIn("plain-secret-value", json.dumps(webui._clean({"x": "a plain-secret-value"}, ["plain-secret-value"])))


def start_serve(fake, **kw):
    """Run serve() on a thread; returns (thread, result, out, client) once it has printed its link."""
    out, result = io.StringIO(), {}

    def target():
        result["code"] = webui.serve(open_browser=kw.pop("open_browser", False), port=kw.pop("port", 0), provider=fake, out=out, **kw)

    t = threading.Thread(target=target, daemon=True)
    t.start()
    deadline = time.time() + 5
    m = None
    while time.time() < deadline and t.is_alive():
        m = re.search(r"http://127\.0\.0\.1:(\d+)/\?key=([\w-]+)", out.getvalue())
        if m:
            break
        time.sleep(0.01)
    client = Client(int(m.group(1)), m.group(2)) if m else None
    return t, result, out, client


class ServeTest(unittest.TestCase):
    def test_finish_after_an_install_returns_0(self):
        fake = FakeSteps(speed=0.001)
        t, result, out, c = start_serve(fake)
        c.login()
        rid = c.api("POST", "/api/apply", {"values": c.api("GET", "/api/model")[1]["values"]})[1]["run"]
        while not c.api("GET", "/api/run/" + rid)[1]["done"]:
            time.sleep(0.01)
        self.assertEqual(c.api("POST", "/api/finish", {})[0], 200)
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(result["code"], 0)
        self.assertIn("Setup is finished.", out.getvalue())
        with self.assertRaises(OSError):  # the port is closed
            socket.create_connection(("127.0.0.1", c.port), timeout=1).close()

    def test_cancel_before_installing_returns_130(self):
        t, result, out, c = start_serve(FakeSteps())
        c.login()
        self.assertEqual(c.api("POST", "/api/cancel", {})[0], 200)
        t.join(5)
        self.assertEqual(result["code"], 130)
        self.assertIn("Nothing was changed.", out.getvalue())

    def test_cancel_after_a_failed_install_returns_1(self):
        fake = FakeSteps(speed=0.001, fail_once=True)
        t, result, out, c = start_serve(fake)
        c.login()
        rid = c.api("POST", "/api/apply", {"values": {}})[1]["run"]
        while not c.api("GET", "/api/run/" + rid)[1]["done"]:
            time.sleep(0.01)
        c.api("POST", "/api/cancel", {})
        t.join(5)
        self.assertEqual(result["code"], 1)

    def test_idle_for_too_long_returns_130(self):
        t, result, out, c = start_serve(FakeSteps(), idle_timeout=0.3)
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(result["code"], 130)
        self.assertIn("without activity", out.getvalue())

    def test_a_taken_port_returns_1(self):
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen(1)
            out = io.StringIO()
            code = webui.serve(open_browser=False, port=busy.getsockname()[1], provider=FakeSteps(), out=out)
        self.assertEqual(code, 1)
        self.assertIn("--port", out.getvalue())

    def test_it_prints_the_link_and_over_ssh_a_tunnel(self):
        with mock.patch.dict(os.environ, {"SSH_CONNECTION": "10.0.0.2 50000 10.0.0.3 22"}):
            t, result, out, c = start_serve(FakeSteps())
            c.login()
            c.api("POST", "/api/cancel", {})
            t.join(5)
        text = out.getvalue()
        self.assertIn("http://127.0.0.1:%d/?key=" % c.port, text)
        self.assertIn("ssh -L %d:127.0.0.1:%d" % (c.port, c.port), text)
        self.assertIn("Ctrl+C", text)

    def test_the_browser_opens_only_when_asked(self):
        with mock.patch.object(webui.util, "open_url", return_value=True) as opener:
            t, result, out, c = start_serve(FakeSteps(), open_browser=False)
            c.login()
            c.api("POST", "/api/cancel", {})
            t.join(5)
            opener.assert_not_called()
            t, result, out, c = start_serve(FakeSteps(), open_browser=True)
            opener.assert_called_once()
            self.assertIn("/?key=", opener.call_args[0][0])
            self.assertIn("is open in your browser", out.getvalue())
            c.login()
            c.api("POST", "/api/cancel", {})
            t.join(5)

    def test_ctrl_c(self):
        wiz = webui.Wizard(FakeSteps(), out=io.StringIO())
        wiz.interrupted()
        self.assertEqual(wiz.exit_code, 130)
        wiz = webui.Wizard(FakeSteps(), out=io.StringIO())
        wiz.last_run = webui.Run("r1")
        wiz.last_run.finish(True, {})
        wiz.interrupted()
        self.assertEqual(wiz.exit_code, 0)


if __name__ == "__main__":
    unittest.main()
