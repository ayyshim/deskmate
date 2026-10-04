"""Runs last (unittest discovers modules in name order): no test may have touched this machine's real Deskmate.

The snapshot is taken when the module is imported, which discovery does before any test runs. A test that
falls back to the real settings once edited the real installed.json; this catches that class of leak.
"""

import hashlib
import os
import unittest
from pathlib import Path


def _real_data_dir() -> Path:
    env = Path(__file__).resolve().parents[2] / ".env"
    try:
        for line in env.read_text().splitlines():
            if line.startswith("DESKMATE_DATA_DIR="):
                return Path(line.split("=", 1)[1].strip())
    except OSError:
        pass
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return Path(base) / "deskmate"


def _snapshot() -> dict:
    data = _real_data_dir()
    out = {}
    for rel in ("installed.json", "secrets/hub-token", "secrets/claude-token", "secrets/notify-url", "bin/mcp-headers"):
        p = data / rel
        try:
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            out[rel] = None
    env = Path(__file__).resolve().parents[2] / ".env"
    try:
        out[".env"] = hashlib.sha256(env.read_bytes()).hexdigest()
    except OSError:
        out[".env"] = None
    return out


BEFORE = _snapshot()


class RealMachineUntouched(unittest.TestCase):
    def test_no_test_changed_the_real_install(self):
        after = _snapshot()
        changed = [k for k in BEFORE if BEFORE[k] != after[k]]
        self.assertEqual(changed, [], f"tests changed the real Deskmate install: {changed}")


if __name__ == "__main__":
    unittest.main()
