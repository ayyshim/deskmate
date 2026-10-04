"""./deskmate: the command line. Each subcommand is a thin layer over the modules that do the work
(settings, compose, steps, doctor here; claude_connect and habits from their own parts), so the web
wizard, the terminal wizard and these commands all behave the same.

Exit codes: 0 ok, 1 failed, 2 usage error (bad flags, a bad answers file), 3 prerequisites missing,
130 cancelled.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

from . import VERSION, compose, detect, doctor, settings, util

OK, FAILED, USAGE, PREREQ, CANCELLED = 0, 1, 2, 3, 130


class _Parser(argparse.ArgumentParser):
    """argparse, but a usage error exits 2 with one plain line (argparse's own default, kept explicit)."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write(f"./deskmate: {message}\n")
        raise SystemExit(USAGE)


def _out(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _err(text: str) -> None:
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


def _printer(style: util.Style | None = None):
    """An emit() that prints events as lines, with a heading whenever the phase changes."""
    style = style or util.Style()
    last = {"phase": None}

    def emit(ev: dict) -> None:
        phase = ev.get("phase") or ""
        if phase and phase != last["phase"]:
            last["phase"] = phase
            _out(style.bold(f"{style.arrow} {phase}"))
        level = ev.get("level", "info")
        text = util.scrub(str(ev.get("text", "")))
        if level == "info":
            _out(f"  {text}")
        else:
            _out(f"  {style.mark(level)} {text}")

    return emit


def _need_setup() -> int:
    if not settings.installed():
        _err("Deskmate isn't set up yet. Run ./deskmate setup first.")
        return FAILED
    return OK


def _plain(values: dict) -> dict:
    """Settings without the secret states, for the parts that never see secrets."""
    return {k: v for k, v in values.items() if not isinstance(v, dict)}


def _confirm(question: str, default: bool = False, yes: bool = False) -> bool:
    if yes:
        return True
    hint = "[Y/n]" if default else "[y/N]"
    try:
        sys.stdout.write(f"{question} {hint} ")
        sys.stdout.flush()
        line = sys.stdin.readline()
    except KeyboardInterrupt:
        _out()
        return False
    if not line:
        _out()
        return False
    ans = line.strip().lower()
    return default if not ans else ans in ("y", "yes")


# ---------------------------------------------------------------- setup


def cmd_setup(args) -> int:
    from . import term

    if args.answers or args.defaults:
        return term.noninteractive(defaults=args.defaults, answers_file=args.answers, yes=args.yes,
                                   dry_run=args.dry_run, update=getattr(args, "update", False))
    if args.dry_run:
        _err("--dry-run needs --defaults or --answers FILE.")
        return USAGE
    web = args.web or (not args.terminal and sys.stdin.isatty() and util.can_open_browser())
    if web:
        try:
            from . import webui
        except ImportError:
            webui = None
        if webui is not None:
            return int(webui.serve(open_browser=not args.no_open, port=args.port or 0) or 0)
        if args.web:
            _err("This copy of Deskmate has no web wizard; using the terminal one.")
    return term.run(yes=args.yes)


# ---------------------------------------------------------------- doctor and status


def cmd_doctor(args) -> int:
    checks = doctor.run()
    if args.json:
        _out(json.dumps({"checks": checks, "summary": doctor.summary(checks)}, indent=2))
    else:
        _out(util.Style().bold("Deskmate doctor"))
        doctor.print_report(checks)
    return FAILED if any(c["status"] == "fail" for c in checks) else OK


def cmd_status(args) -> int:
    if _need_setup():
        return FAILED
    values = settings.load()
    style = util.Style()
    port = values.get("HUB_PORT") or "7800"
    if not detect.docker()["running"]:
        _out(f"  {style.mark('fail')} Docker isn't running")
        return FAILED
    rows = {r["service"]: r for r in compose.ps(_plain(values))}
    bad = False
    for svc in ("desk", "hub"):
        r = rows.get(svc)
        if not r:
            _out(f"  {style.mark('fail')} {svc}: not created (./deskmate up)")
            bad = True
            continue
        st = "ok" if r["state"] == "running" else "fail"
        bad = bad or st != "ok"
        _out(f"  {style.mark(st)} {svc}: {r['status'] or r['state']}")
    code, _, _ = compose.http(port, "/")
    if code == 200:
        _out(f"  {style.mark('ok')} Deskmate answers on http://127.0.0.1:{port} (./deskmate open signs you in)")
    else:
        _out(f"  {style.mark('fail')} Nothing answers on 127.0.0.1:{port}")
        bad = True
    return FAILED if bad else OK


# ---------------------------------------------------------------- running it


def _prepare(values: dict) -> bool:
    """Folders, secret files and compose.local.yaml in step with .env, before any `up`."""
    return compose.prepare(_plain(values), _printer())


def cmd_up(args) -> int:
    if _need_setup():
        return FAILED
    values = settings.load()
    if not _prepare(values):
        return FAILED
    cmd = ["up", "-d", "--remove-orphans"] + (["--build"] if args.build else [])
    code = compose.passthrough(cmd, _plain(values))
    if code != 0:
        return FAILED
    port = values.get("HUB_PORT") or "7800"
    if compose.wait_for_hub(port, 90, _printer()):
        _out(f"Deskmate is up on http://127.0.0.1:{port}. ./deskmate open signs you in.")
        return OK
    _err(f"The hub didn't answer on 127.0.0.1:{port} within 90 seconds. ./deskmate logs hub shows why.")
    return FAILED


def cmd_down(args) -> int:
    if _need_setup():
        return FAILED
    return OK if compose.passthrough(["down"], _plain(settings.load())) == 0 else FAILED


def cmd_restart(args) -> int:
    if _need_setup():
        return FAILED
    values = settings.load()
    if not _prepare(values):
        return FAILED
    svcs = [args.service] if args.service else []
    # `up --force-recreate` rather than `restart`: a restart would keep the settings the containers were made with.
    return OK if compose.passthrough(["up", "-d", "--force-recreate"] + svcs, _plain(values)) == 0 else FAILED


def cmd_logs(args) -> int:
    if _need_setup():
        return FAILED
    cmd = ["logs", "--tail", str(args.tail)] + (["-f"] if not args.no_follow else []) + ([args.service] if args.service else [])
    return OK if compose.passthrough(cmd, _plain(settings.load())) in (0, 130) else FAILED


def cmd_open(args) -> int:
    if _need_setup():
        return FAILED
    url = compose.signin_url(_plain(settings.load()))
    if not url:
        _err("The hub token is missing. Run ./deskmate setup.")
        return FAILED
    _out("Sign in to Deskmate with this link (it holds your sign-in token; don't share it):")
    _out(f"  {url}")
    if not args.no_open and util.can_open_browser():
        util.open_url(url)
    return OK


# ---------------------------------------------------------------- update and uninstall


def cmd_update(args) -> int:
    if _need_setup():
        return FAILED
    repo = settings.repo_dir()
    if (repo / ".git").exists() and not args.no_pull:
        _out(util.Style().bold("→ git pull --ff-only"))
        code, out, err = util.run(["git", "-C", str(repo), "pull", "--ff-only"], timeout=300)
        text = util.scrub((out + err).strip())
        if text:
            _out("  " + text.replace("\n", "\n  "))
        if code != 0:
            _err("git pull failed: commit or stash your own changes in this folder first, then run ./deskmate update again.")
            return FAILED
    # The pulled code may differ from what runs now: the rest runs as a fresh ./deskmate.
    cmd = [sys.executable, str(repo / "deskmate"), "setup", "--defaults", "--update"] + (["--yes"] if args.yes else [])
    try:
        return subprocess.call(cmd, cwd=str(repo))
    except KeyboardInterrupt:
        return CANCELLED


def _is_deskmate_data(path) -> bool:
    """Only a folder that looks like Deskmate's data folder is ever deleted."""
    p = os.path.realpath(str(path))
    if p in ("/", os.path.realpath(detect.home())) or not os.path.isdir(p):
        return False
    names = set(os.listdir(p))
    return bool(names) and names <= {"hub", "exchange", "secrets", "bin", "installed.json", "backups", "setup.log",
                                     "state", "habits"} and "secrets" in names


def cmd_uninstall(args) -> int:
    style = util.Style()
    emit = _printer(style)
    values = settings.load() if settings.installed() else {}
    data = settings.data_dir(values or None)
    _out("This removes Deskmate from this computer: Claude Code's connection to it, the working habits it set up, "
         "its containers and images" + ("" if args.keep_data else ", and its data folder") + ".")
    if not _confirm("Uninstall Deskmate?", default=False, yes=args.yes):
        _out("Nothing changed.")
        return FAILED
    failed = False
    try:
        from . import claude_connect
    except ImportError:
        claude_connect = None
    if claude_connect is not None:
        try:
            res = claude_connect.disconnect(emit, remove_plugins=True) or {}
            failed = failed or res.get("ok") is False
        except Exception as exc:  # noqa: BLE001 - carry on with the rest, say what failed
            emit({"phase": "Disconnect Claude Code", "level": "error", "text": f"{exc.__class__.__name__}: {exc}"})
            failed = True
    try:
        from . import habits
    except ImportError:
        habits = None
    if habits is not None:
        try:
            res = habits.remove(emit) or {}
            failed = failed or res.get("ok") is False
        except Exception as exc:  # noqa: BLE001
            emit({"phase": "Working habits", "level": "error", "text": f"{exc.__class__.__name__}: {exc}"})
            failed = True
    if settings.installed() and detect.docker()["running"]:
        drop_volumes = not args.keep_data and _confirm(
            "Also delete the desk's own home (its browser logins and downloads, a Docker volume)?", default=False,
            yes=args.yes)
        cmd = ["down", "--rmi", "all", "--remove-orphans"] + (["-v"] if drop_volumes else [])
        code = compose.run(cmd, emit, phase="Remove containers", values=_plain(values))
        failed = failed or code != 0
    if not args.keep_data:
        if _is_deskmate_data(data):
            shutil.rmtree(str(data), ignore_errors=True)
            emit({"phase": "Remove files", "level": "ok", "text": f"Deleted the data folder {util.tilde(str(data))}"})
        elif data.exists():
            emit({"phase": "Remove files", "level": "warn",
                  "text": f"Left {util.tilde(str(data))} alone: it holds files Deskmate didn't make"})
    client = settings.config_home()
    for name in ("client.env",):
        f = client / name
        if f.exists():
            f.unlink()
    local = compose.local_path()
    if local.exists():
        local.unlink()
    if args.purge and settings.env_path().exists():
        settings.env_path().unlink()
        emit({"phase": "Remove files", "level": "ok", "text": "Deleted .env"})
    elif settings.env_path().exists():
        emit({"phase": "Remove files", "level": "info",
              "text": ".env is kept, so ./deskmate setup starts from your answers (--purge deletes it)"})
    _out("Deskmate is uninstalled." if not failed else "Uninstalled, with problems: see above.")
    return FAILED if failed else OK


# ---------------------------------------------------------------- config


def _show(key: str, values: dict) -> str:
    v = values.get(key)
    if isinstance(v, dict):
        return (v.get("masked") or "(set)") if v.get("set") else "(not set)"
    return "" if v is None else str(v)


def cmd_config(args) -> int:
    if args.action == "list":
        values = settings.load()
        for s in settings.SETTINGS:
            if s.env or s.secret:
                _out(f"{s.key}={_show(s.key, values)}")
        _out(f"hub-token={_show('x', {'x': settings.secret_state('hub-token', values)})}")
        return OK
    if args.action == "get":
        if not args.key:
            _err("Usage: ./deskmate config get KEY")
            return USAGE
        key = settings.ALIASES.get(args.key, args.key)
        if key == "hub-token":
            _out(_show("x", {"x": settings.secret_state("hub-token")}))
            return OK
        if key not in settings.KEYS:
            _err(f"Unknown setting {args.key}. ./deskmate config list shows them all.")
            return USAGE
        _out(_show(key, settings.load()))
        return OK
    if args.action == "set":
        if not args.key or args.value is None:
            _err("Usage: ./deskmate config set KEY VALUE")
            return USAGE
        key = args.key
        s = settings.KEYS.get(key)
        if key in settings.SECRET_NAMES or key in settings.ALIASES or (s is not None and s.secret):
            _err("Secrets never go on the command line: ./deskmate config set-secret NAME reads it from the keyboard.")
            return USAGE
        if s is None or not s.env or key in settings.DERIVED:
            _err(f"Unknown setting {key}. ./deskmate config list shows them all.")
            return USAGE
        from . import steps

        if s.step:
            res = steps.validate(s.step, {key: args.value})
            msg = (res.get("errors") or {}).get(key)
            if msg:
                _err(f"{key}: {msg}")
                return USAGE
        settings.save({key: args.value})
        _out(f"Saved {key}. ./deskmate up applies it.")
        return OK
    if args.action == "set-secret":
        name = settings.ALIASES.get(args.key or "", args.key or "")
        if name not in settings.SECRET_NAMES:
            _err("Usage: ./deskmate config set-secret claude-token|notify-url|hub-token (the value is read from stdin)")
            return USAGE
        if sys.stdin.isatty():
            import getpass

            value = getpass.getpass(f"{name} (hidden; empty {'makes a new one' if name == 'hub-token' else 'clears it'}): ")
        else:
            value = sys.stdin.readline()
        value = value.strip()
        from . import steps

        if name == "claude-token" and value:
            msg = steps.token_problem(value)
            if msg:
                _err(msg)
                return USAGE
        if name == "notify-url" and value:
            kind = settings.read_env().get("NOTIFY_KIND") or settings.kind_from_url(value)
            msg = steps.url_problem(kind if kind != "none" else settings.kind_from_url(value), value)
            if msg:
                _err(msg)
                return USAGE
        if name == "hub-token":
            import secrets as _secrets

            settings.write_secret("hub-token", value or _secrets.token_urlsafe(32))
            _out("Saved a new hub token. ./deskmate restart hub and ./deskmate connect use it; sign in again "
                 "with ./deskmate open.")
            return OK
        settings.save({name: value})
        _out(f"Saved {name} ({settings.secret_state(name).get('masked') or 'cleared'}). ./deskmate restart hub applies it.")
        return OK
    _err("Usage: ./deskmate config list|get KEY|set KEY VALUE|set-secret NAME")
    return USAGE


# ---------------------------------------------------------------- Claude Code and habits


def cmd_connect(args) -> int:
    if _need_setup():
        return FAILED
    try:
        from . import claude_connect
    except ImportError:
        _err("This copy of Deskmate has no claude_connect module.")
        return FAILED
    if not detect.claude_cli()["installed"]:
        _err("Claude Code wasn't found. Install it (https://code.claude.com), then run ./deskmate connect.")
        return PREREQ
    values = _plain(settings.load())
    if args.remove:
        values["CONNECT_REMOVE"] = args.remove
    res = claude_connect.connect(values, _printer()) or {}
    if res.get("ok") is False:
        _err(util.scrub(str(res.get("error") or "Connecting Claude Code failed.")))
        return FAILED
    _out("Claude Code is connected. Restart sessions that were already open to give them the desk.")
    return OK


def cmd_disconnect(args) -> int:
    try:
        from . import claude_connect
    except ImportError:
        _err("This copy of Deskmate has no claude_connect module.")
        return FAILED
    res = claude_connect.disconnect(_printer(), remove_plugins=not args.keep_plugins) or {}
    return FAILED if res.get("ok") is False else OK


def cmd_habits(args) -> int:
    try:
        from . import habits
    except ImportError:
        _err("This copy of Deskmate has no habits module.")
        return FAILED
    if args.action == "status":
        st = habits.status() or {}
        if args.json:
            _out(json.dumps(st, indent=2, default=str))
        elif hasattr(habits, "format_status"):
            _out(habits.format_status(st))
        else:
            _out("Working habits are set up" if st.get("installed") else "Working habits aren't set up")
            for p in st.get("problems") or []:
                _out(f"  ! {p}")
        return FAILED if st.get("problems") else OK
    if args.action == "remove":
        res = habits.remove(_printer(), folder=args.folder) or {}
        return FAILED if res.get("ok") is False else OK
    from . import steps

    values = _plain(settings.load())
    values["HABITS"] = "on"
    if args.folder:
        values["HABITS_FOLDER"] = os.path.abspath(os.path.expanduser(args.folder))
    res = habits.apply(steps.habits_values(values), _printer()) or {}
    if res.get("ok") is False:
        for e in res.get("errors") or []:
            _err(f"  {e}")
        return FAILED
    if not util.is_on(settings.read_env().get("HABITS", "off")):
        settings.save({"HABITS": "on"})
    return OK


# ---------------------------------------------------------------- the parser


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="./deskmate", description="Deskmate: a shared desk for your Claude Code sessions.")
    p.add_argument("--version", action="version", version=f"Deskmate {VERSION}")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    s = sub.add_parser("setup", help="set Deskmate up, or change its settings (the wizard)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--terminal", action="store_true", help="the wizard in this terminal")
    g.add_argument("--web", action="store_true", help="the wizard in your browser")
    s.add_argument("--defaults", action="store_true", help="take every detected or current value, ask nothing")
    s.add_argument("--answers", metavar="FILE", help="KEY=value answers (no secrets; those come from the environment)")
    s.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    s.add_argument("--dry-run", action="store_true", help="write .env and compose.local.yaml, start nothing")
    s.add_argument("--no-open", action="store_true", help="print the web wizard's link instead of opening it")
    s.add_argument("--port", type=int, default=0, help="the web wizard's port (default: a free one)")
    s.add_argument("--update", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_setup)

    s = sub.add_parser("doctor", help="check that everything works")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("up", help="start the desk and the hub")
    s.add_argument("--build", action="store_true", help="rebuild the images first")
    s.set_defaults(fn=cmd_up)
    sub.add_parser("down", help="stop them").set_defaults(fn=cmd_down)
    s = sub.add_parser("restart", help="restart them with the current settings")
    s.add_argument("service", nargs="?", choices=["desk", "hub"])
    s.set_defaults(fn=cmd_restart)
    sub.add_parser("status", help="are they running?").set_defaults(fn=cmd_status)
    s = sub.add_parser("logs", help="follow the logs")
    s.add_argument("service", nargs="?", choices=["desk", "hub"])
    s.add_argument("--tail", type=int, default=100)
    s.add_argument("--no-follow", action="store_true")
    s.set_defaults(fn=cmd_logs)
    s = sub.add_parser("open", help="open Deskmate's page, signed in")
    s.add_argument("--no-open", action="store_true", help="only print the link")
    s.set_defaults(fn=cmd_open)

    s = sub.add_parser("update", help="git pull, rebuild, reconnect")
    s.add_argument("--yes", "-y", action="store_true")
    s.add_argument("--no-pull", action="store_true", help="skip git pull")
    s.set_defaults(fn=cmd_update)
    s = sub.add_parser("uninstall", help="remove Deskmate from this computer")
    s.add_argument("--keep-data", action="store_true", help="keep the data folder and the desk's logins")
    s.add_argument("--yes", "-y", action="store_true")
    s.add_argument("--purge", action="store_true", help="also delete .env")
    s.set_defaults(fn=cmd_uninstall)

    s = sub.add_parser("config", help="list, get or set one setting")
    s.add_argument("action", choices=["list", "get", "set", "set-secret"])
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")
    s.set_defaults(fn=cmd_config)

    s = sub.add_parser("connect", help="connect Claude Code to Deskmate")
    s.add_argument("--remove", metavar="IDS", help="comma-separated ids of competing tools to remove (with a backup)")
    s.set_defaults(fn=cmd_connect)
    s = sub.add_parser("disconnect", help="undo connect")
    s.add_argument("--keep-plugins", action="store_true")
    s.set_defaults(fn=cmd_disconnect)

    s = sub.add_parser("habits", help="the working-habits pack")
    s.add_argument("action", choices=["status", "apply", "remove"])
    s.add_argument("--folder", metavar="PATH")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_habits)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as exc:
        return int(exc.code or 0) if isinstance(exc.code, int) else USAGE
    if not getattr(args, "fn", None):
        parser.print_help()
        return USAGE
    try:
        return int(args.fn(args) or 0)
    except KeyboardInterrupt:
        _out()
        return CANCELLED
