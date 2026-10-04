"""The secretary's redactor: removes secrets from every text before it is stored or sent to a model.

Stdlib only, Python >= 3.12 (possessive quantifiers). Design §3.5: transcripts hold secrets, so every
text read from a transcript or the docs goes through scrub() before SQLite, and again before a model call.

    >>> from app.secretary.redact import redact
    >>> redact("PGPASSWORD=s3cr3tPw psql -h db")
    ('PGPASSWORD=[redacted:password] psql -h db', 1)

Every secret becomes a typed marker ``[redacted:<kind>]`` (kinds: see KINDS).
Key names, quotes, URL scheme/user/host and header names are kept, so a
condensed transcript still reads naturally.

Rules run in order: anchored token formats first (PEM blocks, provider keys,
JWTs, webhook URLs), then structure (URL user:password@, auth headers,
cookies, command-line flags, SQL and Redis ACL), and the generic KEY=VALUE /
KEY: VALUE rule last.  Markers never match a rule, so redact() is idempotent.
Works on decoded text and on raw JSONL lines: escaped quotes (\\") and the
escapes \\n \\t \\r count as boundaries, and the output of a valid JSONL line
stays valid JSON.

Speed and safety: every rule regex starts with a literal (boundaries are
lookbehinds placed after the literal), so CPython's literal-prefix scan is
used; all repeats are bounded or possessive (no catastrophic backtracking);
rules are gated by lowercase substring hints; the generic KEY=VALUE regex is
only tried at keyword positions found with str.find, iteratively.
"""
from __future__ import annotations

import base64
import binascii
import functools
import re
from typing import Callable

__all__ = ["redact", "scrub", "scrub_obj", "KINDS", "MARKER_RE"]

MARKER_RE = re.compile(r"\[redacted:[a-z0-9-]+\]")
_W = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_")
_WD = _W | {"-"}
_KEYCHARS = _W | {"-", "."}
_NB = r"[A-Za-z0-9_]"
_NBD = r"[A-Za-z0-9_-]"


def _mark(kind: str) -> str:
    return "[redacted:" + kind + "]"


def _lit(prefix: str, cls: str = _NBD) -> str:
    """Fixed-width regex `prefix` not preceded by a `cls` char; a raw-JSON escape (\\n \\t \\r) is a boundary."""
    return prefix + r"(?<!(?<!\\)" + cls + prefix + ")"


def _boundary(s: str, a: int, chars: frozenset[str]) -> bool:
    """True when position `a` starts a token: start of text, a non-`chars` char, or a \\n \\t \\r escape."""
    if a == 0 or s[a - 1] not in chars:
        return True
    return a >= 2 and s[a - 2] == "\\" and s[a - 1] in "ntr"


Check = Callable[[re.Match[str]], "bool | str | None"]
Handler = Callable[[re.Match[str], list[int]], str]


class _Rule:
    __slots__ = ("kind", "rx", "hints", "check", "wb", "handler", "groups", "gate")

    def __init__(self, kind: str, pattern: str, hints: tuple[str, ...], check: Check | None = None,
                 wb: frozenset[str] | None = None, handler: Handler | None = None) -> None:
        self.kind = kind
        self.rx = re.compile(pattern)
        self.hints = tuple(h.lower() for h in hints)   # rule runs only if one of these is in text.lower()
        self.check = check      # check(m) -> falsy: keep; True: redact as `kind`; str: redact as that kind
        self.wb = wb            # chars that must not precede the match (checked in Python)
        self.handler = handler  # custom replacement, handler(m, cnt) -> str
        self.groups = tuple(g for g in ("s", "s2") if g in self.rx.groupindex)  # secret spans; none = whole match
        self.gate = len(self.hints) <= 2  # on long text, many hint scans cost more than the literal-prefixed regex


# --------------------------------------------------------------------------- predicates
_REF_FULL = re.compile(
    r"\$[A-Za-z_][A-Za-z0-9_]*|\$\{[^}\n]*\}|%\(\w+\)[sd]|%\w+%|\{\{.*?\}\}|\{\w*\}|<[^<>\n]{1,60}>"
)
_MASK = re.compile(r"[*xX\u2022\u00b7.\u2026#_-]{3,}|\[?(?i:redacted|hidden|masked|removed|omitted)\]?")
_HAS_DIGIT = re.compile(r"\d")
_HAS_ALPHA = re.compile(r"[A-Za-z]")
_ALNUM = re.compile(r"[A-Za-z0-9]")
_COOKIE_PAIR = re.compile(r"(?<![\w.-])[\w.-]{1,256}=[^\s;]{6,}")


def _is_ref(v: str) -> bool:
    """Variable reference / template / placeholder / our own marker: not a secret."""
    return bool(_REF_FULL.fullmatch(v)) or v.startswith(
        ("${", "$(", "[redacted:", "{{", "#{", "<%", "process.env", "os.environ")
    )


def _is_mask(v: str) -> bool:
    return bool(_MASK.fullmatch(v))


def _tokenish(v: str, minlen: int) -> bool:
    """Looks like a credential rather than a word: long enough, letters and digits."""
    return len(v) >= minlen and bool(_HAS_DIGIT.search(v)) and bool(_HAS_ALPHA.search(v))


def _not_ref(*groups: str) -> Check:
    def chk(m: re.Match[str]) -> bool:
        for g in groups:
            v = m.group(g)
            if v is not None:
                v = v.strip("\"'")
                if not v or _is_ref(v) or _is_mask(v) or not _ALNUM.search(v):
                    return False
        return True
    return chk


def _basic_ok(m: re.Match[str]) -> bool:
    v = m.group("s")
    try:
        txt = base64.b64decode(v + "=" * (-len(v) % 4), validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return False
    return ":" in txt and txt.isprintable()


def _authz_check(m: re.Match[str]) -> bool | str:
    if m.start() == 0 or m.string[m.start() - 1] not in "Aa":    # pattern starts after the A of Authorization
        return False
    v = m.group("s")
    scheme = (m.group("scheme") or "").strip().lower()
    if _is_ref(v) or _is_mask(v):
        return False
    if scheme == "basic":
        return "basic-auth" if _basic_ok(m) else False
    if scheme:
        return _tokenish(v, 8) or (len(v) >= 24 and not v.isalpha())
    return _tokenish(v, 16)


def _cookie_check(m: re.Match[str]) -> bool:
    v = m.group("s")
    if "${" in v or "{{" in v or _is_ref(v.strip()):
        return False
    return bool(_COOKIE_PAIR.search(v))


def _after_space(chk: Check | None = None) -> Check:
    def c(m: re.Match[str]) -> bool | str | None:
        a = m.start()
        if a > 0 and m.string[a - 1] not in " \t":
            return False
        return chk(m) if chk else True
    return c


def _env_default_check(m: re.Match[str]) -> bool:
    before = m.string[max(0, m.start() - 12):m.start()]
    return before.endswith(("getenv", "environ.get", "env", "config")) and _not_ref("s")(m)


def _url_token_check(m: re.Match[str]) -> bool:
    return (m.string[max(0, m.start() - 5):m.start()].lower().endswith(("http", "https"))
            and _tokenish(m.group("s"), 20))


def _line_start(m: re.Match[str]) -> bool:
    i = m.start() - 1
    s = m.string
    while i >= 0 and s[i] in " \t":
        i -= 1
    return i < 0 or s[i] in "\n\r" or (i >= 1 and s[i - 1] == "\\" and s[i] in "nr")


# --------------------------------------------------------------------------- rules
_RULES: list[_Rule] = []


def _r(kind: str, pattern: str, hints: tuple[str, ...], **kw) -> None:
    _RULES.append(_Rule(kind, pattern, hints, **kw))


# PEM private keys (RSA/EC/DSA/OPENSSH/ENCRYPTED/PKCS#8/PGP), real or JSON-escaped newlines, with or
# without the END line (truncated output).  Public certificates are left alone.
_r("private-key",
   r"-----BEGIN[ A-Z0-9]{0,40}PRIVATE KEY(?: BLOCK)?-----"
   r"(?:[A-Za-z0-9+/=\s\\:,.()-]{0,8192}?-----END[ A-Z0-9]{0,40}PRIVATE KEY(?: BLOCK)?-----"
   r"|(?:[A-Za-z0-9+/=\r\n]|\\[nr]){0,8192})",
   ("private key",))
_r("private-key", r"PuTTY-User-Key-File-\d: [^\n]{0,256}(?:\n[^\n]{0,256}){0,40}?Private-MAC: [0-9a-f]{1,128}",
   ("putty-user-key-file",))

# Provider tokens with a recognisable prefix.
_r("anthropic-token", _lit("sk-ant-") + r"[A-Za-z0-9_-]{8,1024}+", ("sk-ant-",))
_r("openrouter-key", _lit("sk-or-v1-") + r"[0-9a-f]{20,256}+", ("sk-or-",))
_r("openai-key", _lit("sk-") + r"(?:(?:proj|svcacct|admin)-[A-Za-z0-9_-]{20,1024}+"
   r"|(?=[A-Za-z0-9_-]{0,200}\d)(?=[A-Za-z0-9_-]{0,200}[A-Za-z])[A-Za-z0-9_-]{32,1024}+)", ("sk-",))
_r("github-token", _lit("gh[pousr]_", _NB) + r"[A-Za-z0-9]{36,255}+(?![A-Za-z0-9_])",
   ("ghp_", "gho_", "ghu_", "ghs_", "ghr_"))
_r("github-token", _lit("github_pat_", _NB) + r"[A-Za-z0-9_]{22,255}+", ("github_pat_",))
_r("gitlab-token", r"gl(?:pat|dt|ptt|rt)-[A-Za-z0-9_-]{20,256}+", ("glpat-", "gldt-", "glptt-", "glrt-"), wb=_W)
_r("aws-access-key-id", r"A(?:KIA|SIA|BIA|CCA|GPA|IDA|IPA|NPA|NVA|PKA|ROA|SCA)[A-Z0-9]{16}(?![A-Za-z0-9_])",
   ("akia", "asia", "abia", "acca", "agpa", "aida", "aipa", "anpa", "anva", "apka", "aroa", "asca"), wb=_W)
_r("aws-signature", r"X-Amz-(?:Signature|Security-Token|Credential)=(?P<s>[^&\s\"'<>\\]{1,2048}+)", ("x-amz-",),
   check=_not_ref("s"))
_r("aws-signature", r"Signature=(?P<s>[0-9a-f]{64})(?![0-9a-f])", ("signature=",), wb=_W)
_r("google-api-key", _lit("AIza", _NB) + r"[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])", ("aiza",))
_r("google-oauth", r"GOCSPX-[A-Za-z0-9_-]{20,256}+", ("gocspx-",))
_r("google-oauth", _lit(r"ya29\.", _NB) + r"[0-9A-Za-z_-]{20,2048}+", ("ya29.",))
_r("google-oauth", _lit("1//0", r"[A-Za-z0-9_/]") + r"[0-9A-Za-z_-]{40,512}+", ("1//0",))
_r("slack-token", _lit("xox[abposre]-", _NB) + r"[0-9A-Za-z-]{10,256}+", ("xox",))
_r("slack-token", r"xapp-\d-[A-Z0-9]{1,32}-\d{1,32}-[a-f0-9]{1,256}", ("xapp-",), wb=_W)
_r("slack-webhook", r"https://hooks\.slack\.com/(?:services|workflows|triggers)/[A-Za-z0-9/_-]{10,256}+",
   ("hooks.slack.com",))
_r("discord-webhook",
   r"https?://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/api(?:/v\d{1,3})?/webhooks/\d{1,32}/[A-Za-z0-9_-]{20,256}+",
   ("/webhooks/",))
_r("discord-bot-token", r"[MNO][A-Za-z0-9_-]{23,27}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,40}(?![A-Za-z0-9_-])",
   ("discord",), wb=_WD)
_r("stripe-key", r"(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,256}+", ("sk_live_", "sk_test_", "rk_live_", "rk_test_"),
   wb=_W)
_r("stripe-key", r"whsec_[A-Za-z0-9]{24,256}+", ("whsec_",), wb=_W)
_r("sendgrid-key", r"SG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])", ("sg.",), wb=_W)
_r("npm-token", r"npm_[A-Za-z0-9]{36}(?![A-Za-z0-9_])", ("npm_",), wb=_W)
_r("pypi-token", r"pypi-AgE[A-Za-z0-9_-]{50,1024}+", ("pypi-age",), wb=_W)
_r("huggingface-token", r"hf_[A-Za-z]{34}(?![A-Za-z0-9_])", ("hf_",), wb=_W)
_r("docker-token", r"dckr_pat_[A-Za-z0-9_-]{20,256}+", ("dckr_pat_",), wb=_W)
_r("digitalocean-token", r"do[opr]_v1_[a-f0-9]{64}(?![a-f0-9])", ("_v1_",), wb=_W)
_r("telegram-bot-token", r"\d{8,10}:AA[A-Za-z0-9_-]{33}(?![A-Za-z0-9_-])", (":aa",), wb=_W)
_r("age-secret-key", r"AGE-SECRET-KEY-1[0-9A-Z]{58}", ("age-secret-key-",), wb=_W)
_r("fcm-server-key", r"AAAA[A-Za-z0-9_-]{7}:APA91b[A-Za-z0-9_-]{100,512}+", (":apa91b",), wb=_W)
_r("django-secret-key", r"django-insecure-[^\s'\"\\`]{20,256}+", ("django-insecure-",))
_r("password-hash", r"pbkdf2_sha(?:1|256)\$\d{1,8}\$[A-Za-z0-9./+]{1,256}+\$[A-Za-z0-9./+=]{1,256}+", ("pbkdf2_",),
   wb=_W)
_r("password-hash", r"\$2[aby]\$\d\d\$[./A-Za-z0-9]{53}", ("$2a$", "$2b$", "$2y$"))
_r("password-hash", r"\$argon2(?:id|i|d)\$v=\d{1,4}\$[^\s'\"\\]{1,512}+", ("$argon2",))

# JWT / JWS (header.payload[.sig]; signature optional so truncated tokens go too) and JWE (5 parts).
_r("jwt", _lit("eyJ") + r"[A-Za-z0-9_-]{8,8192}+\.(?:eyJ[A-Za-z0-9_-]{8,16384}+(?:\.[A-Za-z0-9_-]{0,8192}+)?"
   r"|[A-Za-z0-9_-]{0,8192}+\.[A-Za-z0-9_-]{8,16384}+\.[A-Za-z0-9_-]{8,8192}+\.[A-Za-z0-9_-]{8,8192}+)", ("eyj",))

_r("otpauth-url", r"otpauth(?:-migration)?://[^\s\"'<>`\\]{1,2048}+", ("otpauth",))

# scheme://user:PASSWORD@host (postgres, redis, amqp, mongodb+srv, https...).  `@` may occur inside
# an unencoded password: the lazy class grows until the `@` that is followed by the host.
_r("url-password",
   r"://[^\s:/@\[\]\"'`<>\\]{0,256}:(?P<s>[^\s/\"'`<>\\]{1,512}?)"
   r"(?=@[A-Za-z0-9\[_${(-][^\s@/\"'`<>\\]{0,512}(?:[/\s\"'`<>\\?#]|$))",
   ("://",), check=_not_ref("s"))
_r("url-token", r"://(?P<s>[A-Za-z0-9_-]{20,256}+)(?=@[A-Za-z0-9.-]{1,256})", ("://",), check=_url_token_check)

# --------------------------------------------------------------------------- headers
_AUTHZ_TAIL = (r"\\?[\"']?[ \t]*[:=][ \t]*\\?[\"']?[ \t]*"
               r"(?P<scheme>(?i:bearer|token|basic|jwt|key|apikey|api-key|oauth|sso-key|digest|negotiate|ntlm)[ \t]+)?"
               r"(?P<s>[A-Za-z0-9._~+/=:-]{6,4096}+)")
_r("auth-token", "uthorization" + _AUTHZ_TAIL, ("authorization",), check=_authz_check)
_r("auth-token", "UTHORIZATION" + _AUTHZ_TAIL, ("authorization",), check=_authz_check)
_r("bearer-token", r"[Bb]earer[ \t]+(?P<s>[A-Za-z0-9._~+/=-]{16,4096}+)", ("bearer",), wb=_W,
   check=lambda m: _tokenish(m.group("s"), 16))
_r("basic-auth", r"Basic[ \t]+(?P<s>[A-Za-z0-9+/]{8,4096}+={0,2})(?![A-Za-z0-9+/=])", ("basic",), wb=_W,
   check=_basic_ok)
_r("cookie", r"[Cc][Oo][Oo][Kk][Ii][Ee]\\?[\"']?[ \t]*:[ \t]*\\?[\"']?(?P<s>[^\r\n\"'\\]{8,4096}+)", ("cookie",),
   wb=_W, check=_cookie_check)
_r("cookie", r"-(?:b|-cookie)[ \t]+[\"']?(?P<s>[^\s\"'\\=]{0,256}=[^\s\"'\\]{6,4096}+)", ("curl",),
   check=_after_space(_not_ref("s")))

# --------------------------------------------------------------------------- commands and configs
_ACL_PW = re.compile(r"(?<=[ \t\"'])(?P<p>[>#<!])(?P<s>[^\s\"'\\]{3,1024}+)")


def _acl_repl(m: re.Match[str], cnt: list[int]) -> str:
    def one(p: re.Match[str]) -> str:
        v = p.group("s")
        if _is_ref(v) or v.endswith(">"):
            return p.group(0)
        cnt[0] += 1
        return p.group("p") + _mark("redis-password")
    return _ACL_PW.sub(one, m.group(0))


def _acl_file_repl(m: re.Match[str], cnt: list[int]) -> str:
    return _acl_repl(m, cnt) if _line_start(m) else m.group(0)


_CMDLINE = r"[^\n|;&]{0,256}?"   # rest of the same shell command, bounded
_r("redis-password", r"(?:ACL|acl)[ \t]+(?:SETUSER|setuser)\b[^\n]{0,4096}", ("setuser",), handler=_acl_repl, wb=_W)
_r("redis-password", r"user[ \t]+\S{1,256}[ \t]+(?:on|off)\b[^\n]{0,4096}", ("user ",), handler=_acl_file_repl)
_r("redis-password", r"redis-cli" + _CMDLINE + r"[ \t](?:-a|--pass)[ \t]+[\"']?(?P<s>[^\s\"'\\]{1,1024}+)",
   ("redis-cli",), check=_not_ref("s"))
_r("redis-password", r"(?:requirepass|masterauth)[ \t]+[\"']?(?P<s>[^\s\"'\\]{1,1024}+)", ("requirepass", "masterauth"),
   wb=_W, check=_not_ref("s"))
_r("cli-password", r"sshpass[ \t]+-p[ \t]*[\"']?(?P<s>[^\s\"'\\]{1,1024}+)", ("sshpass",), check=_not_ref("s"))
_r("cli-password", r"mysql\w{0,16}" + _CMDLINE + r"[ \t]-p(?P<s>[^\s\"'\\-][^\s\"'\\]{0,1024}+)", ("mysql",), wb=_W,
   check=_not_ref("s"))
_r("cli-password", r"curl" + _CMDLINE + r"[ \t](?:-u|--user)[ \t]*[\"']?[^\s:\"']{0,256}:(?P<s>[^\s\"'\\]{1,1024}+)",
   ("curl",), wb=_W, check=_not_ref("s"))
_r("cli-password", r"docker[ \t]+login" + _CMDLINE + r"[ \t](?:-p|--password)[ \t=]+[\"']?(?P<s>[^\s\"'\\]{1,1024}+)",
   ("docker",), check=_not_ref("s"))
_r("cli-password",
   r"--?(?:password|passwd|passin|passout|pass|pwd|token|api-?key|secret|client-secret|access-token|auth-token"
   r"|refresh-token|bearer-token|requirepass|masterauth|storepass|keypass|srcstorepass|deststorepass"
   r"|db-password|admin-password|smtp-password)(?:=|[ \t]+)[\"']?(?P<s>(?!-)[^\s\"'\\]{3,1024}+)",
   ("pass", "pwd", "token", "key", "secret"), wb=_WD, check=_not_ref("s"))
_r("s3-credentials", r"mc[ \t]+(?:alias[ \t]+set|config[ \t]+host[ \t]+add)[ \t]+\S{1,256}[ \t]+\S{1,1024}[ \t]+"
   r"(?P<s>[^\s\\]{1,1024}+)[ \t]+(?P<s2>[^\s\\]{1,1024}+)", ("mc ",), wb=_W, check=_not_ref("s", "s2"))
_r("netrc-password", r"machine[ \t]+\S{1,256}[ \t]+login[ \t]+\S{1,256}[ \t]+password[ \t]+(?P<s>[^\s\\]{1,1024}+)",
   ("machine",), wb=_W, check=_not_ref("s"))
_r("sql-password",
   r"(?:IDENTIFIED[ \t]+BY|identified[ \t]+by|PASSWORD|Password|password)[ \t]+'(?P<s>[^'\n\\]{1,1024}+)(?=')",
   ("identified", "password '", "password\t'"), wb=_W, check=_not_ref("s"))
_r("sudo-password",
   r"echo[ \t]+(?P<s>\\\"[^\"\n]{0,256}?\\\"|\"[^\"\n]{0,256}+\"|'[^'\n]{0,256}+'|[^\s|\\]{1,256}+)"
   r"(?=[ \t]*\|[ \t]*sudo[ \t]+(?:-\w{1,16}[ \t]+){0,8}-S\b)",
   ("sudo",), wb=_W, check=_not_ref("s"))
_r("password", r"_password(?:_hash)?\([ \t]*b?[\"'](?P<s>[^\"'\n\\]{1,1024}+)", ("_password(", "_password_hash("),
   check=lambda m: m.string[max(0, m.start() - 8):m.start()].endswith(("set", "make", "check", "hash", "generate"))
   and _not_ref("s")(m))
_r("password", r"hashpw\([ \t]*b?[\"'](?P<s>[^\"'\n\\]{1,1024}+)", ("hashpw(",), wb=_W, check=_not_ref("s"))
_r("secret", r"\([ \t]*[\"']\w{0,64}?(?i:password|passwd|secret|token|key|pass)\w{0,64}[\"'][ \t]*,[ \t]*"
   r"(?:default[ \t]*=[ \t]*)?[\"'](?P<s>[^\"'\n\\]{1,1024}+)", ("getenv", "environ.get", "env(", "config("),
   check=_env_default_check)
_r("totp-secret", r"TOTP\([ \t]*[\"'](?P<s>[A-Z2-7]{16,256}+=*)", ("totp(",))

# --------------------------------------------------------------------------- generic KEY=VALUE / KEY: VALUE
_KW = (r"passw(?:or)?d|passphrase|pass|pwd|pw|secret|token|api[_-]?key|private[_-]?key|access[_-]?key"
       r"|credential|auth|session|sessid|sid|csrf|xsrf|cookie")
_KV_FIND = ("pass", "pw", "secret", "token", "api", "private", "access", "credential", "auth", "sess", "sid",
            "csrf", "xsrf", "cookie")
_KV_KEY_RX = re.compile(
    r"(?P<kw>" + _KW + r")"
    r"(?P<rest>[A-Za-z0-9_.\-]{0,64}+)"
    r"(?P<kq>\\?[\"'`])?\]?(?:\*\*|__)?"
    r"(?P<sp1>[ \t]{0,16})(?P<sep>===?|!==?|=>|:=|=|:(?!:))(?P<sp2>[ \t]{0,16})(?:(?:\*\*|__)[ \t]{0,4})?",
    re.I,
)
_KV_VAL_RX = re.compile(
    r"\\\"(?P<v1>[^\"\n]{0,2048}?)\\\""
    r"|\"(?P<v2>(?:[^\"\\\n]|\\.){0,2048}+)\""
    r"|'(?P<v3>(?:[^'\\\n]|\\.){0,2048}+)'"
    r"|`(?P<v4>[^`\n]{0,2048}+)`"
    r"|(?P<v5>[\"'`]?[^\s\"'`\\]{1,2048}+)"
    r"(?P<tt>(?:[ \t]{0,8}\|[ \t]{0,8}[\w.\[\]]{1,64}){0,3}[ \t]{0,8}=[ \t]{0,8}(?P<tq>[\"'])"
    r"(?P<tv>[^\"'\n]{1,2048}+)(?P=tq))?"
)
_XML_RX = re.compile(r"<(?P<tag>[\w:.-]{0,40}(?i:pass|pwd|secret|token|key|credential)[\w:.-]{0,40})>"
                     r"(?P<s>[^<\n\\]{3,512})</(?P=tag)>")
_SPLIT_RX = re.compile(r"[_.\-]+|(?<=[a-z0-9])(?=[A-Z])")
_EXACT = {"pass": "password", "pwd": "password", "pw": "password", "token": "token", "auth": "auth",
          "sid": "session", "sessid": "session", "sessionid": "session", "session": "session", "csrf": "session",
          "xsrf": "session", "cookie": "cookie", "authkey": "secret", "signingkey": "secret",
          "encryptionkey": "secret", "masterkey": "secret", "secretkey": "secret", "totpseed": "secret",
          "otpseed": "secret", "mfaseed": "secret"}
_STRONG = (("privatekey", "private-key"), ("password", "password"), ("passwd", "password"),
           ("passphrase", "password"), ("secret", "secret"), ("apikey", "api-key"), ("accesskey", "access-key"),
           ("credential", "credential"))
_RANK = {"private-key": 0, "password": 1, "totp-secret": 2, "secret": 2, "api-key": 3, "access-key": 4, "token": 5,
         "credential": 6, "session": 7, "cookie": 8, "auth": 9}
_NEVER = frozenset({"nopasswd", "passwordless", "tokens", "sessions", "cookies", "secrets", "credentialless"})
# last key segment that makes the key *about* a secret rather than holding one (token_type, password_min_length)
_META = frozenset("""type types kind count counts len length size min max limit limits budget usage used url uri
    urls endpoint endpoints host hostname domain path paths file files filename dir directory folder location name
    names label labels title placeholder hint help description desc text message msg error errors field fields
    column header prefix suffix format pattern regex schema policy policies rule rules validator validators hasher
    hashers strength score expiry expires expire expiration expired exp lifetime ttl age timeout interval at on date
    time timestamp version algorithm alg method methods mode provider providers backend backends strategy scheme
    class classes engine model view views form serializer controller service module store slice reducer hook
    handler callback listener id ids ref refs arn keys list map mapping index flag flags enabled disabled required
    optional valid invalid status state step page screen route link button btn input modal dialog icon color style
    test tests spec mock user username users email login owner account group groups role roles scope scopes
    audience issuer subject source sources changed reset forgot total cost events event secure httponly
    samesite origins middleware exempt env var vars variable variables rate ratio percent tool tools cli cmd
    command origin""".split())
_DESIGN = frozenset("""color colour colors theme spacing font radius shadow design typography palette motion
    elevation opacity border""".split())
_PASS_NOUNS = frozenset("""staff season movie day guest vip boarding gate free comp complimentary event festival gift
    member annual monthly weekly bus redeem lookup""".split())
_VERB_FIRST = frozenset("""has is use uses should can enable show hide toggle allow require requires needs validate
    verify check get fetch load handle on forgot with without no skip must generate create make build parse decode
    encode encrypt decrypt sign mint rotate revoke save clear send resolve extract format normalize mask redact
    ensure find lookup read write update delete remove""".split())
_FILE_EXTS = frozenset("""py pyc ts tsx js jsx mjs cjs dart md mdx json jsonl yaml yml toml ini cfg conf sh bash
    zsh sql html htm css scss go rs java kt swift rb php txt log lock vue svelte xml csv properties gradle""".split())
_ALWAYS_SKIP = frozenset("true false null none nil undefined yes no on off required optional nan".split())
_CODE_SKIP = frozenset("""string str number int integer float bool boolean any unknown bytes buffer object dict list
    array secretstr text char varchar uuid date datetime void never function await async new this self cls typeof
    lambda return default not is in or and env environment header headers query body cookie cookies session
    bearer basic token jwt oauth oauth2 apikey api_key password secret include omit same-origin hidden masked
    placeholder digest""".split())
_IDENT = re.compile(r"[A-Za-z_$][\w$]*+")
_CODE_EXPR = re.compile(r"[A-Za-z_$][\w$]*+(?:\??\.[A-Za-z_$(]|\(|\[|<|->|::)")
_CAMEL = re.compile(r"[a-z][A-Z]")
_WORDY_IDENT = re.compile(r"_*+[A-Za-z]++(?:[_.:-][A-Za-z]++)*+[_.:-]?")   # possessive: linear time
_HEX_INT = re.compile(r"0[xX][0-9a-fA-F]+")
_KW_HINT = re.compile(r"pass|pwd|token|secret|key|cred|auth|session|cookie|hash", re.I)
_LOWER_PATH = re.compile(r"(?:~|\.{1,2})?/[a-z0-9._~@{}:/-]*+")
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_CSS_COLOR = re.compile(r"#[0-9a-fA-F]{3,8}|(?:rgb|rgba|hsl|hsla|var)\(.*")
_UPPER_KEY = re.compile(r"[A-Z][A-Z0-9_]*+")
_NUMERICISH = re.compile(r"[-+]?[\d.,:%]++[kKmMbB]?")
_KV_LIST_NEXT = re.compile(r"[ \t]+[A-Za-z_][\w.-]*=")
_LIST_SPLIT = re.compile(r"[,;][ \t]*[\w.-]+[=:]")
_QUERY_NEXT = re.compile(r"&[\w.-]+=")
_MINLEN = {"password": 3, "session": 16, "auth": 12, "cookie": 12, "credential": 12}
_WEAK_KINDS = frozenset({"session", "auth", "cookie", "credential"})
# A key whose last word is one of these holds the secret itself (cookiePassword, DB_PASS, sessionSecret), so a
# quoted literal on it is the secret even when it reads like words ('admin-pwd', 'keyboard-cat'). Keys ending in
# another word name a place for one (TOKEN_KEY = "auth_token", PASSWORD_FIELD), where wordy values are names.
_SLOT_WORDS = frozenset({"password", "passwd", "pass", "pwd", "pw", "passphrase", "secret"})


@functools.lru_cache(maxsize=4096)
def _key_kind(key: str) -> str | None:
    """Sensitivity class of a key name, or None when the key does not hold a secret."""
    if not key or key in ("PWD", "OLDPWD"):
        return None
    dot = key.rfind(".")
    if dot > 0 and key[dot + 1:].lower() in _FILE_EXTS:      # grep output: auth.service.ts:12:
        return None
    segs = [p.lower() for p in _SPLIT_RX.split(key) if p]
    if not segs:
        return None
    if not _DESIGN.isdisjoint(segs) or "keys" in segs[:-1]:
        return None                        # ColorToken.navBar, tokens.color.x, SecureKeys.token (names of things)
    cands = segs + [a + b for a, b in zip(segs, segs[1:])] + [a + b + c for a, b, c in zip(segs, segs[1:], segs[2:])]
    best = None
    for j, s in enumerate(cands):
        if s in _NEVER or s.startswith("secretar"):
            continue
        if s == "pass" and 0 < j < len(segs) and segs[j - 1] in _PASS_NOUNS:
            continue                       # staff_pass, season_pass: a ticket pass, not a password
        kind = _EXACT.get(s)
        if kind is None:
            for needle, k in _STRONG:
                if needle in s:
                    kind = k
                    break
            else:
                if len(s) > 5 and s.endswith("token"):
                    kind = "token"
        if kind and (best is None or _RANK[kind] < _RANK[best]):
            best = kind
    if best is None:
        return None
    last = segs[-1]
    if last in _META and not (last == "id" and len(segs) >= 3 and segs[-3:-1] == ["access", "key"]):
        return None
    if segs[0] in _VERB_FIRST and len(segs) > 1:
        return None
    if best in ("secret", "api-key", "token") and any(s in ("totp", "otp", "mfa", "2fa") or s.startswith("totp")
                                                       for s in segs):
        return "totp-secret"
    return best


def _is_slot(key: str, kind: str) -> bool:
    """The key is the secret's own slot: its last word is password/secret/... (see _SLOT_WORDS)."""
    if kind not in ("password", "secret"):
        return False
    segs = [p.lower() for p in _SPLIT_RX.split(key) if p]
    return bool(segs) and segs[-1] in _SLOT_WORDS


def _value_is_secret(kind: str, v: str, quoted: bool, ctx: str, s: str, vend: int, slot: bool = False) -> bool:
    """ctx: "env" (UPPER_KEY=value, always literal), "yaml" (UPPER_KEY: value) or "code" (anything else).
    slot: the key is the secret's own slot (_is_slot), so a quoted wordy literal still counts."""
    if kind in _WEAK_KINDS and not _tokenish(v, _MINLEN[kind]):
        return False                       # session/auth/cookie/credential values must look like tokens
    if not v or not _ALNUM.search(v) or _is_ref(v) or _is_mask(v) or "[redacted:" in v:
        return False
    vl = v.lower()
    if vl in _ALWAYS_SKIP:
        return False
    if "${" in v or "{{" in v or "#{" in v or _CSS_COLOR.fullmatch(v):
        return False
    if kind in ("session", "auth", "credential", "cookie") and _UUID.fullmatch(v):
        return False                       # e.g. Claude Code "sessionId": "<uuid>"
    if kind == "credential" and vl in ("include", "omit", "same-origin"):
        return False                       # fetch(..., {credentials: 'include'})
    if v.isdigit():
        return (kind == "password" and len(v) >= 4) or len(v) >= 16
    if _NUMERICISH.fullmatch(v) and _HAS_DIGIT.search(v):
        return False                       # 0.9  12:30  2026-10-04  45%
    if _LOWER_PATH.fullmatch(v):
        return False                       # /auth/reset-password/  ~/.config/x
    if ctx != "env" and _WORDY_IDENT.fullmatch(v) and _KW_HINT.search(v) and not (quoted and slot):
        return False                       # TOKEN_KEY = "auth_token" (a storage-key name, not a secret)
    if _HEX_INT.fullmatch(v):
        return False                       # 0xFF1A2B3C colour / int literals
    if ctx == "yaml" and not quoted and (vl in _CODE_SKIP or _CODE_EXPR.match(v) or v[0] in "(["
                                         or (_IDENT.fullmatch(v) and v[0].isupper() and not _HAS_DIGIT.search(v))):
        return False                       # ROLE_BY_TOKEN: Record<...>  TOKEN_REFUSED: Final  (typed constants)
    if ctx == "code":
        if v.count(" ") >= 3:
            return False                   # prose / error messages, not a credential
        if vl in _CODE_SKIP:
            return False
        if not quoted:
            if _CODE_EXPR.match(v) or v[0] in "([" or "=>" in v:
                return False               # os.environ[...]  getToken()  self.password  (n) => ...
            if _IDENT.fullmatch(v) and not _HAS_DIGIT.search(v):
                if _KW_HINT.search(v):
                    return False           # password: hashedPassword
                if _CAMEL.search(v) or "_" in v or "$" in v or v[0].isupper() or kind != "password":
                    return False           # variables, types, token: someWord
                rest = s[vend:vend + 80]
                if not (rest[:1] in ("", "\n", "\r") or rest.startswith(("\\n", "\\r"))
                        or rest.lstrip(" \t")[:1] in ("", "#", "\n", "\r") or _KV_LIST_NEXT.match(rest)):
                    return False           # prose: "password: stored in the vault"
    return len(v) >= _MINLEN.get(kind, 6)


def _trim_bare(v: str, query_ctx: bool) -> str:
    """The secret part of a bare value: cut at list / query / statement delimiters and unbalanced closers."""
    cut = len(v)
    amp = v.find("&")
    if amp > 0 and (query_ctx or _QUERY_NEXT.search(v)):
        cut = amp
    m = _LIST_SPLIT.search(v, 0, cut)
    if m and m.start() > 0:
        cut = m.start()
    val = v[:cut]
    while val and val[-1] in ",;.:":
        val = val[:-1]
    for close, open_ in ((")", "("), ("]", "["), ("}", "{"), (">", "<")):
        while val.endswith(close) and val.count(close) > val.count(open_):
            val = val[:-1]
    return val


def _kv_sub(text: str, cnt: list[int], low: str | None = None) -> str:
    """Apply the KEY=VALUE rule at keyword positions (found with str.find), left to right, no recursion.

    Values that are not secrets are rescanned for nested `key=value` pairs (query strings, minified JSON)
    until a work budget proportional to the text length is used up; after that a rejected value is skipped
    whole, which keeps adversarial input (`auth=auth=auth=...`) linear.
    """
    if len(text) < 4:
        return text
    if low is None or len(low) != len(text):
        low = text.lower()
    if len(low) != len(text):              # rare: lower() changed the length; find positions case-insensitively
        pos = [m.start() for m in re.finditer("|".join(_KV_FIND), text, re.I)]
    else:
        pos = []
        for k in _KV_FIND:
            i = low.find(k)
            while i != -1:
                pos.append(i)
                i = low.find(k, i + 1)
        pos.sort()
    if not pos:
        return text
    out: list[str] = []
    last = 0
    budget = 16 * len(text) + 65536
    key_match = _KV_KEY_RX.match
    for p in pos:
        if p < last:
            continue
        km = key_match(text, p)
        if km is None:
            continue
        rep, resume, cost = _kv_repl(km, cnt, budget <= 0)
        budget -= cost
        if rep is None:
            continue
        out.append(text[last:p])
        out.append(rep)
        last = resume
    if not out:
        return text
    out.append(text[last:])
    return "".join(out)


def _kv_repl(km: re.Match[str], cnt: list[int], skip: bool) -> tuple[str | None, int, int]:
    """(replacement for text[km.start():resume] or None to leave it, resume, work done)."""
    s = km.string
    ks = km.start()
    i, lo = ks, max(0, ks - 64)
    while i > lo and s[i - 1] in _KEYCHARS:
        if i >= 2 and s[i - 2] == "\\" and s[i - 1] in "ntr":
            break                          # raw JSON: "\nDB_PASSWORD=..." - the key starts after the escape
        i -= 1
    key = (s[i:ks] + km.group("kw") + km.group("rest")).strip(".-")
    kind = _key_kind(key)
    if kind is None:
        return None, 0, km.end() - ks
    vm = _KV_VAL_RX.match(s, km.end())
    if vm is None:
        return None, 0, km.end() - ks
    cost = vm.end() - ks
    vg = next(g for g in ("v1", "v2", "v3", "v4", "v5") if vm.group(g) is not None)
    va, vb = vm.span(vg)
    val = vm.group(vg)
    quoted = vg != "v5"
    if vg == "v5" and val[:1] in "\"'`":
        va, val, quoted = va + 1, val[1:], True
    if vg == "v5" and not quoted:
        val = _trim_bare(val, i > 0 and s[i - 1] in "?&")
    sep, spaced = km.group("sep"), bool(km.group("sp1") or km.group("sp2"))
    ctx = "code"
    if _UPPER_KEY.fullmatch(key):
        if sep == "=" and not spaced:
            ctx = "env"
        elif sep == ":":
            ctx = "yaml"
    if _value_is_secret(kind, val, quoted, ctx, s, va + len(val), _is_slot(key, kind)):
        cnt[0] += 1
        return s[ks:va] + _mark(kind), va + len(val), cost
    if vm.group("tt") is not None:         # `key: Type = "literal"`: the literal is the value
        ta, tb = vm.span("tv")
        if _value_is_secret(kind, vm.group("tv"), True, "code", s, tb):
            cnt[0] += 1
            return s[ks:ta] + _mark(kind), tb, cost
    if skip:                               # budget used up: do not rescan inside this value
        return s[ks:vm.end()], vm.end(), cost
    return None, 0, cost                   # not a secret: keep scanning inside the value


def _xml_repl(m: re.Match[str], cnt: list[int]) -> str:
    kind = _key_kind(m.group("tag").split(":")[-1])
    v = m.group("s").strip()
    if kind is None or not _value_is_secret(kind, v, True, "code", m.string, m.end("s")):
        return m.group(0)
    cnt[0] += 1
    a, b = m.span("s")
    return m.group(0)[: a - m.start()] + _mark(kind) + m.group(0)[b - m.start():]


_r("password", _XML_RX.pattern, ("</",), handler=_xml_repl)

KINDS: tuple[str, ...] = tuple(dict.fromkeys(
    [r.kind for r in _RULES] + ["basic-auth", "password", "secret", "api-key", "access-key", "token", "credential",
                                "session", "cookie", "auth", "totp-secret"]))
_KV_INDEX = len(_RULES)                    # the generic rule runs last
_TRIGGERS = tuple(sorted({h for r in _RULES for h in r.hints} | set(_KV_FIND)))
_TRIG_RULES: dict[str, tuple[int, ...]] = {
    t: tuple(i for i, r in enumerate(_RULES) if t in r.hints) + ((_KV_INDEX,) if t in _KV_FIND else ())
    for t in _TRIGGERS}


# --------------------------------------------------------------------------- driver
def _apply(rule: _Rule, text: str, cnt: list[int]) -> str:
    groups, wb, check, handler, kind0 = rule.groups, rule.wb, rule.check, rule.handler, rule.kind

    def repl(m: re.Match[str]) -> str:
        s = m.string
        a = m.start()
        if wb is not None and not _boundary(s, a, wb):
            return m.group(0)
        if handler is not None:
            return handler(m, cnt)
        kind = kind0
        if check is not None:
            r = check(m)
            if not r:
                return m.group(0)
            if r is not True:
                kind = r
        live = [g for g in groups if m.group(g) is not None]
        if not live:
            cnt[0] += 1
            return _mark(kind)
        out, pos = [], a
        for g in live:
            ga, gb = m.span(g)
            out.append(s[pos:ga])
            out.append(_mark(kind))
            pos = gb
            cnt[0] += 1
        out.append(s[pos:m.end()])
        return "".join(out)

    return rule.rx.sub(repl, text)


def redact(text: str) -> tuple[str, int]:
    """Return (text with every detected secret replaced by [redacted:<kind>], number of replacements)."""
    if len(text) < 6:
        return text, 0
    low = text.lower()
    cnt = [0]
    if len(text) <= 4096:                  # short: one pass over all triggers selects the rules to run
        present = [t for t in _TRIGGERS if t in low]
        if not present:
            return text, 0
        for i in sorted({i for t in present for i in _TRIG_RULES[t]}):
            if i == _KV_INDEX:
                text = _kv_sub(text, cnt, low if cnt[0] == 0 else None)
            else:
                text = _apply(_RULES[i], text, cnt)
        return text, cnt[0]
    contains = low.__contains__
    for rule in _RULES:
        if rule.gate and not any(map(contains, rule.hints)):
            continue
        text = _apply(rule, text, cnt)
    text = _kv_sub(text, cnt, low if cnt[0] == 0 else None)
    return text, cnt[0]


def scrub(text: str | None) -> str:
    """redact() for callers that only want the text. None becomes ''."""
    if not text:
        return ""
    return redact(text)[0]


def scrub_obj(obj):
    """Redact every string inside a JSON-like value (dicts, lists, strings)."""
    if isinstance(obj, str):
        return scrub(obj)
    if isinstance(obj, list):
        return [scrub_obj(x) for x in obj]
    if isinstance(obj, dict):
        return {k: scrub_obj(v) for k, v in obj.items()}
    return obj
