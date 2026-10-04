"""settings: .env round trip, file modes, secrets, and the move from the old layout (migrate_legacy)."""

from __future__ import annotations

import re
import unittest

from test_core_support import FAKE_HUB_TOKEN, FAKE_TOKEN, FAKE_WEBHOOK, REAL_REPO, CoreCase, mode

from deskmate_setup import settings

# Shaped like .env.example at commit 3edd27b (the layout before 2026-10-04), with fake values.
LEGACY_ENV = """# Copy to .env (make env fills in the token). Never commit .env.
# Bearer token for /mcp and /hooks, and the sign-in link for the web UI.
DESKMATE_TOKEN={hub}
# The secretary's Claude login: run `claude setup-token` and paste the token here.
CLAUDE_CODE_OAUTH_TOKEN={tok}
HOST_UID=1000
HOST_GID=1000
# Sessions the secretary reads: project folders in ~/.claude/projects whose name starts with this.
SESSIONS_PREFIX={prefix}
SECRETARY_PAUSE_AT=0.60
SECRETARY_MAX_DIGESTS_PER_DAY=40
SECRETARY_DIGEST_MODEL=claude-haiku-4-5-20251001
SECRETARY_BRIEF_MODEL=claude-sonnet-5-5
# Local times for the daily brief and the morning Discord post.
TZ=Europe/London
BRIEF_AT=18:30
DISCORD_BRIEF_AT=09:15
MY_OWN_KEY=kept value
"""


def encode(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", path)


class SettingsRoundTrip(CoreCase):
    def test_save_load_and_modes(self):
        roots = self.projects()
        settings.save({"DESKMATE_OWNER": "Alex", "SESSIONS_ROOTS": str(roots), "DESK_MONITORS": "3",
                       "claude-token": FAKE_TOKEN, "SECRETARY": True})
        env = self.repo / ".env"
        self.assertEqual(mode(env), 0o600)
        text = self.env_text()
        self.assertNotIn(FAKE_TOKEN, text)
        self.assertNotIn("TOKEN=", text)
        data = settings.data_dir()
        self.assertEqual(data, self.home / ".local" / "share" / "deskmate")
        self.assertEqual(mode(data), 0o700)
        self.assertEqual(mode(data / "secrets"), 0o700)
        for name in ("hub-token", "claude-token"):
            self.assertEqual(mode(data / "secrets" / name), 0o600)
        self.assertEqual(settings.read_secret("claude-token"), FAKE_TOKEN)
        self.assertGreaterEqual(len(settings.read_secret("hub-token")), 40)
        values = settings.load()
        self.assertEqual(values["DESKMATE_OWNER"], "Alex")
        self.assertEqual(values["DESK_MONITORS"], "3")
        self.assertEqual(values["SESSIONS_ROOTS"], str(roots))
        self.assertEqual(values["SECRETARY"], "on")
        self.assertEqual(values["claude-token"], {"set": True, "masked": "sk-ant-oat…Qx7A"})
        self.assertEqual(values["COMPOSE_FILE"], "compose.yaml:deploy/net-host.yaml:compose.local.yaml")
        self.assertTrue((self.repo / "compose.local.yaml").is_file())

    def test_order_comments_quotes_and_kept_keys(self):
        (self.repo / ".env").write_text("SOMETHING_ELSE=1\n")
        settings.save({"DESKMATE_OWNER": "Alex Doe", "HOST_HOME": str(self.home)})
        text = self.env_text()
        keys = [ln.split("=", 1)[0] for ln in text.splitlines() if ln and not ln.startswith("#")]
        expected = [s.key for s in settings.SETTINGS if s.env and not s.secret]
        self.assertEqual(keys[: len(expected)], expected)
        self.assertEqual(keys[len(expected):], ["SOMETHING_ELSE"])
        self.assertIn("# Kept from before\nSOMETHING_ELSE=1", text)
        self.assertIn("DESKMATE_OWNER='Alex Doe'", text)
        for s in settings.SETTINGS:
            if s.env and not s.secret:
                self.assertRegex(text, rf"# [^\n]+\n{s.key}=", s.key)
        self.assertEqual(settings.read_env()["DESKMATE_OWNER"], "Alex Doe")
        self.assertNotIn("\r", text)

    def test_parse_env_forms(self):
        env = settings.parse_env("export A=1\nB='x y'\nC=\"q\\\"z\"\nD=plain # note\n# E=no\n  F = spaced\n")
        self.assertEqual(env, {"A": "1", "B": "x y", "C": 'q"z', "D": "plain", "F": "spaced"})
        for value in ("plain/path:x", "with space", "it's", "dollar $HOME"):
            back = settings.parse_env(f"K={settings.quote(value)}\n")["K"]
            self.assertEqual(back, value)

    def test_secret_names_and_masks(self):
        with self.assertRaises(ValueError):
            settings.write_secret("../etc/passwd", "x")
        settings.write_secret("notify-url", FAKE_WEBHOOK)
        st = settings.secret_state("notify-url")
        self.assertTrue(st["set"])
        self.assertNotIn("FAKE-webhook-token", st["masked"])
        self.assertEqual(settings.kind_from_url(FAKE_WEBHOOK), "discord")
        self.assertEqual(settings.kind_from_url("https://hooks.slack.com/services/T1/B2/c3"), "slack")
        self.assertEqual(settings.kind_from_url("https://ntfy.sh/topic"), "ntfy")
        self.assertEqual(settings.kind_from_url("https://example.com/x"), "webhook")
        self.assertEqual(settings.kind_from_url(""), "none")

    def test_notify_url_sets_kind_and_file(self):
        settings.save({"notify-url": FAKE_WEBHOOK})
        env = settings.read_env()
        self.assertEqual(env["NOTIFY_KIND"], "discord")
        self.assertEqual(env["NOTIFY_URL_FILE"], str(settings.data_dir() / "secrets" / "notify-url"))
        self.assertNotIn("webhooks", self.env_text())

    def test_hub_token_kept_on_resave(self):
        settings.save({})
        first = settings.read_secret("hub-token")
        settings.save({"DESK_MONITORS": "1"})
        self.assertEqual(settings.read_secret("hub-token"), first)

    def test_habits_hub_choices_have_no_clone(self):
        values = [c["value"] for c in settings.KEYS["HABITS_HUB"].choices]
        self.assertEqual(values, ["auto", "existing", "none"])
        self.assertNotIn("HABITS_HUB_URL", settings.KEYS)


class MigrateLegacy(CoreCase):
    def write_legacy(self, prefix: str):
        (self.repo / ".env").write_text(LEGACY_ENV.format(hub=FAKE_HUB_TOKEN, tok=FAKE_TOKEN, prefix=prefix))
        (self.repo / ".env").chmod(0o600)

    def test_migrates_the_old_layout(self):
        projects = self.projects()
        hook = self.home / ".config" / "claude-notify" / "discord-webhook.url"
        hook.parent.mkdir(parents=True)
        hook.write_text("https://example.invalid/not-a-real-webhook\n")
        old_text = LEGACY_ENV.format(hub=FAKE_HUB_TOKEN, tok=FAKE_TOKEN, prefix=encode(str(projects)))
        self.write_legacy(encode(str(projects)))
        self.assertTrue(settings.is_legacy())
        lines = settings.migrate_legacy()
        joined = "\n".join(lines)
        self.assertNotIn(FAKE_TOKEN, joined)
        self.assertNotIn(FAKE_HUB_TOKEN, joined)
        backups = list(self.repo.glob(".env.backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(mode(backups[0]), 0o600)
        self.assertEqual(backups[0].read_text(), old_text)
        # The same hub token, so signed-in browsers stay signed in; the login moved too.
        self.assertEqual(settings.read_secret("hub-token"), FAKE_HUB_TOKEN)
        self.assertEqual(settings.read_secret("claude-token"), FAKE_TOKEN)
        self.assertEqual(mode(settings.data_dir() / "secrets" / "hub-token"), 0o600)
        env = settings.read_env()
        for k in settings.LEGACY_KEYS:
            self.assertNotIn(k, env)
        text = self.env_text()
        self.assertNotIn(FAKE_TOKEN, text)
        self.assertNotIn(FAKE_HUB_TOKEN, text)
        self.assertEqual(mode(self.repo / ".env"), 0o600)
        self.assertEqual(env["SESSIONS_ROOTS"], str(projects))
        self.assertEqual(env["MORNING_POST_AT"], "09:15")
        self.assertEqual(env["NOTIFY_KIND"], "discord")
        self.assertEqual(env["NOTIFY_URL_FILE"], str(hook))
        self.assertEqual(env["COMPOSE_PROJECT_NAME"], "deskmate")
        self.assertEqual(env["TZ"], "Europe/London")
        self.assertEqual(env["MY_OWN_KEY"], "kept value")
        self.assertIn("Kept COMPOSE_PROJECT_NAME=deskmate, so the desk keeps its logins", lines)
        # Re-running changes nothing.
        self.assertEqual(settings.migrate_legacy(), [])
        self.assertEqual(len(list(self.repo.glob(".env.backup-*"))), 1)
        # The webhook file was pointed at, not copied.
        self.assertFalse((settings.data_dir() / "secrets" / "notify-url").exists())

    def test_prefix_without_a_folder_asks(self):
        self.write_legacy("-home-nobody-Nowhere")
        lines = settings.migrate_legacy()
        self.assertTrue(any("matches no single folder" in ln for ln in lines))
        self.assertEqual(settings.read_secret("hub-token"), FAKE_HUB_TOKEN)

    def test_load_reads_old_layout_without_writing(self):
        self.write_legacy(encode(str(self.projects())))
        values = settings.load()
        self.assertEqual(values["MORNING_POST_AT"], "09:15")
        self.assertEqual(values["HUB_PORT"], "7800")
        self.assertTrue(values["claude-token"]["set"])
        self.assertFalse(list(self.repo.glob(".env.backup-*")))
        self.assertIn("DESKMATE_TOKEN", settings.read_env())

    def test_save_migrates_first(self):
        self.write_legacy(encode(str(self.projects())))
        settings.save({"DESK_MONITORS": "1"})
        self.assertEqual(settings.read_secret("hub-token"), FAKE_HUB_TOKEN)
        self.assertNotIn("DESKMATE_TOKEN", self.env_text())
        self.assertEqual(settings.read_env()["DESK_MONITORS"], "1")

    def test_fixture_matches_the_old_example(self):
        """The fixture has the old example's keys (checked against git when the history is there)."""
        import subprocess

        try:
            old = subprocess.run(["git", "-C", str(REAL_REPO), "show", "3edd27b:.env.example"], capture_output=True,
                                 text=True, timeout=10).stdout
        except (OSError, subprocess.TimeoutExpired):
            old = ""
        if not old:
            self.skipTest("no git history here")
        old_keys = set(settings.parse_env(old))
        fixture_keys = set(settings.parse_env(LEGACY_ENV))
        self.assertLessEqual(old_keys, fixture_keys)


if __name__ == "__main__":
    unittest.main()
