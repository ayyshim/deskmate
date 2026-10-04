"""Tests for the secretary's redactor (app/secretary/redact.py). Every secret below is FAKE.

Ported from the redaction research; fixtures use generic names (Alex, example projects).
"""
from __future__ import annotations

import base64
import json
import re
from collections import Counter

from app.secretary.redact import redact, scrub, scrub_obj  # noqa: E402

F = "FAKEfake0123456789"                       # 18 chars
HEX64 = "0123456789abcdef" * 4
B64U = lambda s: base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")  # noqa: E731
JWT = B64U('{"alg":"HS256","typ":"JWT"}') + "." + B64U('{"sub":"FAKE-user","exp":0}') + "." + B64U("FAKE-signature-not-real")
PEM_RSA = ("-----BEGIN RSA PRIVATE KEY-----\n" + "\n".join(["MIIEFAKEfakeFAKEfakeFAKEfakeFAKEfakeFAKEfakeFAKEfakeFAKEfake0123"] * 4)
           + "\n-----END RSA PRIVATE KEY-----")
PEM_SSH = ("-----BEGIN OPENSSH PRIVATE KEY-----\n" + "b3BlbnNzaC1rZXktdjEAAAAAFAKEfakeFAKEfake\n" * 3
           + "-----END OPENSSH PRIVATE KEY-----")
PEM_ENC = ("-----BEGIN RSA PRIVATE KEY-----\nProc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,00FAKE00FAKE00FAKE\n\n"
           + "FAKEfakeFAKEfakeFAKEfakeFAKEfake0123456789+/=\n-----END RSA PRIVATE KEY-----")
PEM_JSON_ESCAPED = ('"private_key": "-----BEGIN PRIVATE KEY-----\\nMIIEvFAKEfakeFAKEfakeFAKE0123456789\\n'
                    'FAKEfakeFAKEfake0123456789+/\\n-----END PRIVATE KEY-----\\n"')
PEM_TRUNCATED = "-----BEGIN EC PRIVATE KEY-----\nMHcCAQEEIFAKEfakeFAKEfakeFAKEfake0123456789\nAwEHoUQDQgAEFAKE"

# (case id, text, expected marker counts, fake secret substrings that must disappear)
POSITIVE: list[tuple[str, str, dict[str, int], list[str]]] = [
    ("pem-rsa", "key:\n" + PEM_RSA + "\ndone", {"private-key": 1}, ["MIIEFAKE"]),
    ("pem-openssh", PEM_SSH, {"private-key": 1}, ["b3BlbnNzaC1rZXktdjEAAAAAFAKE"]),
    ("pem-encrypted", PEM_ENC, {"private-key": 1}, ["00FAKE00FAKE", "FAKEfakeFAKEfakeFAKEfakeFAKEfake0123"]),
    ("pem-json-escaped", "{" + PEM_JSON_ESCAPED + "}", {"private-key": 1}, ["MIIEvFAKE", "FAKEfakeFAKEfake0123456789+/"]),
    ("pem-truncated", PEM_TRUNCATED, {"private-key": 1}, ["MHcCAQEEIFAKE", "AwEHoUQDQgAEFAKE"]),
    ("anthropic-api", "ANTHROPIC_API_KEY=sk-ant-api03-" + F * 5 + "AA", {"anthropic-token": 1}, [F]),
    ("anthropic-oauth", "token sk-ant-oat01-" + F * 5 + " expires", {"anthropic-token": 1}, [F]),
    ("anthropic-partial", "it starts with sk-ant-api03-FAKEfake0123 and", {"anthropic-token": 1}, ["FAKEfake0123"]),
    ("openai-proj", "key sk-proj-" + F * 4, {"openai-key": 1}, [F]),
    ("openai-legacy", "key sk-" + "FAKEfake01234567" * 3, {"openai-key": 1}, ["FAKEfake01234567"]),
    ("openrouter", "OPENROUTER sk-or-v1-" + HEX64, {"openrouter-key": 1}, [HEX64]),
    ("github-ghp", "git push https://github.com x ghp_" + F * 2 + " y", {"github-token": 1}, [F]),
    ("github-gho", "gho_" + F * 2, {"github-token": 1}, [F]),
    ("github-pat", "github_pat_11FAKE0000000000000000_" + "FAKEfake" * 8, {"github-token": 1}, ["FAKEfake"]),
    ("gitlab", "PRIVATE glpat" + "-FAKEfake0123456789xy", {"gitlab-token": 1}, ["FAKEfake0123456789xy"]),
    ("aws-key-id", "aws_access_key_id = AKIAFAKE0000EXAMPLE0", {"aws-access-key-id": 1}, ["AKIAFAKE0000EXAMPLE0"]),
    ("aws-secret-kv", "AWS_SECRET_ACCESS_KEY=FAKEfakeFAKEfake/0123456789+FAKEfakeFAKE", {"secret": 1},
     ["FAKEfakeFAKEfake/0123456789+FAKEfakeFAKE"]),
    ("aws-presigned", "https://s3.example.com/b/o?X-Amz-Credential=FAKEKEY%2F20261004%2Fus-east-1&X-Amz-Signature=" + HEX64,
     {"aws-signature": 2}, [HEX64, "FAKEKEY%2F2026"]),
    ("google-api-key", "apiKey AIza" + (F * 2)[:35] + " end", {"google-api-key": 1}, [(F * 2)[:35]]),
    ("google-oauth-secret", "client GOCSPX-FAKEfake0123456789FAKEfake01", {"google-oauth": 1}, ["FAKEfake0123456789FAKEfake01"]),
    ("google-access-token", "ya29." + F * 3, {"google-oauth": 1}, [F]),
    ("slack-token", "xox" + "b-000000000000-0000000000000-FAKEfakeFAKEfakeFAKEfake", {"slack-token": 1}, ["FAKEfakeFAKEfakeFAKEfake"]),
    ("slack-webhook", "post to https://hooks.slack.com/" + "services/T00000000/B00000000/FAKEFAKEFAKEFAKEFAKEFAKE now",
     {"slack-webhook": 1}, ["FAKEFAKEFAKEFAKEFAKEFAKE", "T00000000"]),
    ("discord-webhook", "curl -X POST https://discord.com/api/webhooks/000000000000000000/FAKE-fake_" + F * 3 + " -d x",
     {"discord-webhook": 1}, [F, "000000000000000000"]),
    ("discord-bot-token", "DISCORD MFAKEfakeFAKEfakeFAKEfake0.FAKE01.FAKEfakeFAKEfakeFAKEfakeFAKEf ok",
     {"discord-bot-token": 1}, ["MFAKEfakeFAKEfakeFAKEfake0"]),
    ("stripe", "sk_live_FAKEfakeFAKEfake0123 and whsec_" + "FAKEfake" * 4, {"stripe-key": 2}, ["FAKEfakeFAKEfake0123", "whsec_FAKE"]),
    ("sendgrid", "SG.FAKEfakeFAKEfake0123_-." + ("FAKEfake0123456789" * 3)[:42] + "-", {"sendgrid-key": 1}, ["SG.FAKE"]),
    ("npm", "//registry.npmjs.org/:_authToken=npm_" + "FAKEfake0123456789" * 2, {"npm-token": 1}, ["npm_FAKE"]),
    ("pypi", "password = pypi-AgEIcHlwaS5vcmc" + F * 3, {"pypi-token": 1}, ["pypi-AgE"]),
    ("huggingface", "HF hf_" + "FAKEfake" * 4 + "FA", {"huggingface-token": 1}, ["hf_FAKE"]),
    ("docker-pat", "dckr_pat_FAKEfake0123456789FAKE_-x", {"docker-token": 1}, ["dckr_pat_FAKE"]),
    ("digitalocean", "dop_v1_" + HEX64, {"digitalocean-token": 1}, [HEX64]),
    ("telegram", "bot 123456789:AA" + ("FAKEfake0123456789" * 2)[:33], {"telegram-bot-token": 1}, ["123456789:AA"]),
    ("age", "AGE-SECRET-KEY-1" + "FAKE" * 14 + "FA", {"age-secret-key": 1}, ["AGE-SECRET-KEY-1FAKE"]),
    ("fcm-server-key", "key=AAAAFAKEfak:APA91b" + "FAKEfake0123456789" * 8, {"fcm-server-key": 1}, ["APA91bFAKE"]),
    ("django-insecure", "SECRET_KEY = 'django-insecure-fake!fake@fake#0123456789fakefake'", {"django-secret-key": 1},
     ["fake!fake@fake#0123456789"]),
    ("pbkdf2", "password | pbkdf2_sha256$600000$FAKEsalt0123$FAKEhash0123456789abcdefFAKEhash0123456789=",
     {"password-hash": 1}, ["FAKEsalt0123", "FAKEhash0123"]),
    ("bcrypt", "hash $2b$12$" + ("FAKEfake0123456789./" * 3)[:53], {"password-hash": 1}, ["FAKEfake0123456789./"]),
    ("argon2", "$argon2id$v=19$m=65536,t=3,p=4$RkFLRXNhbHQ$RkFLRWhhc2hGQUtFaGFzaA", {"password-hash": 1}, ["RkFLRWhhc2hGQUtF"]),
    ("jwt", '{"access": "' + JWT + '"}', {"jwt": 1}, [JWT.split(".")[1]]),
    ("jwt-unsigned-truncated", "token " + JWT.rsplit(".", 1)[0] + " (truncated)", {"jwt": 1}, [JWT.split(".")[1]]),
    ("otpauth", "otpauth://totp/Tower:alex?secret=FAKEFAKEFAKEFAKE2345&issuer=Tower", {"otpauth-url": 1}, ["FAKEFAKEFAKEFAKE2345"]),
    ("url-postgres", "DATABASE_URL=postgresql+asyncpg://tower:FakePass123@db:5432/tower", {"url-password": 1}, ["FakePass123"]),
    ("url-redis-empty-user", "REDIS_URL=redis://:FakeRedisPw99@redis:6379/0", {"url-password": 1}, ["FakeRedisPw99"]),
    ("url-redis-acl-user", "redis://tower_ro:Fake-Pw_123@10.0.0.89:6379", {"url-password": 1}, ["Fake-Pw_123"]),
    ("url-https-token", "git clone https://x-access-token:FAKEfake0123@github.com/org/repo.git", {"url-password": 1}, ["FAKEfake0123"]),
    ("url-at-in-password", "postgres://u:p@ss@db/x", {"url-password": 1}, ["p@ss"]),
    ("url-mongodb-srv", "mongodb+srv://u:Fake%40Pw1@cluster0.example.net/db", {"url-password": 1}, ["Fake%40Pw1"]),
    ("url-host-variable", "postgresql://app:FakePw9@${DB_HOST}:5432/app", {"url-password": 1}, ["FakePw9"]),
    ("credential-tokenish", 'credential: "cr3d-0123456789abcd"', {"credential": 1}, ["cr3d-0123456789abcd"]),
    ("url-token-as-user", "https://FAKEfake0123456789FAKEfake@github.com/org/repo.git", {"url-token": 1}, ["FAKEfake0123456789FAKEfake"]),
    ("authz-bearer", 'curl -H "Authorization: Bearer FAKEtoken0123456789abcdef" https://x', {"auth-token": 1}, ["FAKEtoken0123456789abcdef"]),
    ("authz-drf-token", "-H 'Authorization: Token 0123456789abcdef0123456789abcdef01234567'", {"auth-token": 1},
     ["0123456789abcdef0123456789abcdef01234567"]),
    ("authz-json", '{"Authorization": "Token FAKE0123456789"}', {"auth-token": 1}, ["FAKE0123456789"]),
    ("authz-basic", "Authorization: Basic " + base64.b64encode(b"fakeuser:fakepass").decode(), {"basic-auth": 1},
     [base64.b64encode(b"fakeuser:fakepass").decode()]),
    ("bearer-standalone", "use bearer FAKE0123456789abcdefFAKE here", {"bearer-token": 1}, ["FAKE0123456789abcdefFAKE"]),
    ("basic-standalone", "header Basic " + base64.b64encode(b"qa:fake-pw-1").decode(), {"basic-auth": 1},
     [base64.b64encode(b"qa:fake-pw-1").decode()]),
    ("cookie-header", "Cookie: sessionid=fake0123456789abcdefghijklmnopqr; csrftoken=FAKEcsrf0123456789", {"cookie": 1},
     ["fake0123456789abcdef", "FAKEcsrf0123456789"]),
    ("set-cookie", "Set-Cookie: sessionid=fake0123456789abcdefghij; Path=/; HttpOnly", {"cookie": 1}, ["fake0123456789abcdefghij"]),
    ("curl-cookie", "curl -b 'sessionid=fake0123456789abcdef' https://x/admin/", {"cookie": 1}, ["fake0123456789abcdef"]),
    ("session-kv", "sessionid=fake0123456789abcdefghij", {"session": 1}, ["fake0123456789abcdefghij"]),
    ("connect-sid", "connect.sid=s%3AFAKE0123456789.fakeSig", {"session": 1}, ["FAKE0123456789"]),
    ("redis-cli-a", "redis-cli -h 10.0.0.89 -a FakeRedisPass123 ping", {"redis-password": 1}, ["FakeRedisPass123"]),
    ("redis-acl-setuser", "ACL SETUSER tower_ro on >FakeAclPw123 ~* +@read", {"redis-password": 1}, ["FakeAclPw123"]),
    ("redis-acl-file-hash", "user tower_ro on #" + HEX64 + " ~* &* +@read", {"redis-password": 1}, [HEX64]),
    ("redis-requirepass", "bind 0.0.0.0\nrequirepass FakeRequire123\n", {"redis-password": 1}, ["FakeRequire123"]),
    ("sshpass", "sshpass -p 'FakeSshPw1' ssh dwpvmaster uptime", {"cli-password": 1}, ["FakeSshPw1"]),
    ("mysql-p", "mysql -uroot -pFakeMysql1 arena", {"cli-password": 1}, ["FakeMysql1"]),
    ("curl-u", "curl -u admin:FakeCurlPw1 https://x/api", {"cli-password": 1}, ["FakeCurlPw1"]),
    ("docker-login", "docker login -u bot -p FakeDockerPw1 registry.example.com", {"cli-password": 1}, ["FakeDockerPw1"]),
    ("flag-password", "pg_dump --password=FakePgDump1 -h db", {"cli-password": 1}, ["FakePgDump1"]),
    ("keytool", "keytool -genkey -storepass FakeStore1 -keypass FakeKey1 -alias x", {"cli-password": 2}, ["FakeStore1", "FakeKey1"]),
    ("openssl-passin", "openssl rsa -in k.pem -passin pass:FakeOpenssl1", {"cli-password": 1}, ["FakeOpenssl1"]),
    ("mc-alias", "mc alias set local http://minio:9000 FAKEACCESSKEY FakeMinioSecret1", {"s3-credentials": 2},
     ["FAKEACCESSKEY", "FakeMinioSecret1"]),
    ("netrc", "machine github.com login bot password FakeNetrc1", {"netrc-password": 1}, ["FakeNetrc1"]),
    ("sql-identified", "CREATE USER app IDENTIFIED BY 'FakeSql1';", {"sql-password": 1}, ["FakeSql1"]),
    ("sql-with-password", "ALTER ROLE app WITH PASSWORD 'FakeSql2';", {"sql-password": 1}, ["FakeSql2"]),
    ("sudo-echo", "echo 'FakeSudo1' | sudo -S systemctl restart tower", {"sudo-password": 1}, ["FakeSudo1"]),
    ("set-password", 'user.set_password("FakeSetPw1")', {"password": 1}, ["FakeSetPw1"]),
    ("env-default", 'DB_PASS = os.getenv("DB_PASSWORD", "FakeDefault1")', {"secret": 1}, ["FakeDefault1"]),
    ("pyotp", 'pyotp.TOTP("FAKEFAKEFAKEFAKE2345").now()', {"totp-secret": 1}, ["FAKEFAKEFAKEFAKE2345"]),
    ("totp-kv", "totp_secret: FAKEFAKEFAKEFAKE2345", {"totp-secret": 1}, ["FAKEFAKEFAKEFAKE2345"]),
    ("kv-env-wordy-password", "sh -c 'PGPASSWORD=fake-devpass psql -h db'", {"password": 1}, ["fake-devpass"]),
    ("kv-pgpassword", "PGPASSWORD=FakePg123 psql -h db -U app -c 'select 1'", {"password": 1}, ["FakePg123"]),
    ("kv-env-quoted-spaces", 'export DB_PASSWORD="Fake Pw With Space"', {"password": 1}, ["Fake Pw With Space"]),
    ("kv-api-key-env", "API_KEY=FAKEfake0123456789", {"api-key": 1}, ["FAKEfake0123456789"]),
    ("kv-python-const", "SECRET_KEY = 'fake-secret-key-value-0123'", {"secret": 1}, ["fake-secret-key-value-0123"]),
    # Found on real data: a hardcoded default quoted in a review. A wordy value that names a secret word ('…-pwd',
    # 'keyboard-…') on the secret's own key is the secret, not a storage-key name.
    ("kv-code-slot-wordy", "AdminJS ships `cookiePassword: 'fake-pwd'` and `secret = 'keyboard-cat'`",
     {"password": 1, "secret": 1}, ["fake-pwd", "keyboard-cat"]),
    ("kv-code-slot-wordy-key", "{ sessionSecret: 'fake-session-key', DB_PASS: 'fake-pass' }", {"secret": 1, "password": 1},
     ["fake-session-key", "fake-pass"]),
    ("kv-superuser", "DJANGO_SUPERUSER_PASSWORD=FakeAdmin1 python manage.py createsuperuser --noinput", {"password": 1}, ["FakeAdmin1"]),
    ("kv-compose-yaml", "    environment:\n      POSTGRES_PASSWORD: postgres\n", {"password": 1}, ["POSTGRES_PASSWORD: postgres"]),
    ("kv-docker-e", "docker run -e REDIS_PASSWORD=FakeRedis9 redis:7", {"password": 1}, ["FakeRedis9"]),
    ("kv-json", '{"username": "qa", "password": "FakeJson1"}', {"password": 1}, ["FakeJson1"]),
    ("kv-json-escaped", '{\\"username\\":\\"qa\\",\\"password\\":\\"FakeEsc1\\"}', {"password": 1}, ["FakeEsc1"]),
    ("kv-python-kwargs", 'client.login(username="qa", password="FakeKw1")', {"password": 1}, ["FakeKw1"]),
    ("kv-js-object", 'const cfg = { apiKey: "FAKEfake0123456789" };', {"api-key": 1}, ["FAKEfake0123456789"]),
    ("kv-query-string", "GET https://api.example.com/v1?token=FAKEtok0123456789&page=2&api_key=FAKEkey0123456789",
     {"token": 1, "api-key": 1}, ["FAKEtok0123456789", "FAKEkey0123456789"]),
    ("kv-x-api-key", "X-API-Key: 0123456789abcdef0123456789abcdef", {"api-key": 1}, ["0123456789abcdef0123456789abcdef"]),
    ("kv-private-token", "PRIVATE-TOKEN: FAKEfake0123456789", {"token": 1}, ["FAKEfake0123456789"]),
    ("kv-markdown-bold", "- **Password:** FakeMd1", {"password": 1}, ["FakeMd1"]),
    ("kv-typed-python", 'api_key: str = "FAKEfake0123456789"', {"api-key": 1}, ["FAKEfake0123456789"]),
    ("kv-typed-ts", 'const token: string = "FAKEtoken0123456789";', {"token": 1}, ["FAKEtoken0123456789"]),
    ("kv-numeric-password", "password: 482910", {"password": 1}, ["482910"]),
    ("kv-shell-default", "${POSTGRES_PASSWORD:-FakeDefault2}", {"password": 1}, ["FakeDefault2"]),
    ("kv-xml", "<password>FakeXml1</password>", {"password": 1}, ["FakeXml1"]),
    ("kv-key-properties", "storePassword=FakeStore2\nkeyAlias=upload", {"password": 1}, ["FakeStore2"]),
    ("kv-conninfo", 'psql "host=db user=app password=postgres dbname=app"', {"password": 1}, ["password=postgres"]),
    ("kv-compare", 'if password == "FakeCmp1":', {"password": 1}, ["FakeCmp1"]),
    ("kv-after-newline", "qa@yopmail.com\npassword: FakeQa1!", {"password": 1}, ["FakeQa1!"]),
    ("raw-escape-boundaries", '"out":"x\\nDB_PASSWORD=postgres\\nsk-ant-api03-' + F * 5 + '\\nrequirepass FakeReq1\\n"',
     {"password": 1, "anthropic-token": 1, "redis-password": 1}, ["=postgres", F, "FakeReq1"]),
    ("raw-jsonl-literal-newlines", '"DB_PASSWORD=FakeEnv1\\nAPI_TOKEN=FakeTok0123456789\\n"', {"password": 1, "token": 1},
     ["FakeEnv1", "FakeTok0123456789"]),
]

# Ordinary transcript lines that must come back byte-identical with count 0.
NEGATIVE: list[str] = [
    'password = request.data.get("password")',
    "const token = await getToken();",
    "password: string;",
    "token?: string | null;",
    "def login(username: str, password: str) -> Token:",
    "if (!token) return null;",
    "headers: { Authorization: `Bearer ${token}` },",
    'api_key = os.environ["API_KEY"]',
    'SECRET_KEY = env("SECRET_KEY")',
    "password: hashedPassword,",
    "token: localStorage.getItem('token'),",
    "fetch(url, { credentials: 'include' })",
    '"private": true,',
    "max_tokens: 1024",
    '{"input_tokens": 3, "output_tokens": 120, "cache_read_input_tokens": 51234}',
    'token_type: "Bearer"',
    "ACCESS_TOKEN_LIFETIME: timedelta(minutes=5),",
    "AUTH_PASSWORD_VALIDATORS = [",
    "PASSWORD_HASHERS = [",
    "secretary_model: haiku",
    "%admin ALL=(ALL) NOPASSWD: ALL",
    "PWD=/home/alex/Projects/deskmate",
    "arena/app/auth.service.ts:12:  async login() {",
    'auth_method: "password"',
    "secretName: arena-tls",
    "password_min_length: 8",
    'echo "$PW" | docker login --password-stdin ghcr.io',
    "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>",
    'curl -H "Authorization: Bearer $TOKEN" https://api.example.com',
    'curl -H "Authorization: Token ${API_TOKEN}" http://localhost:8000/api/',
    "DATABASE_URL=postgres://${DB_USER}:${DB_PASSWORD}@db:5432/app",
    "Bearer authentication is required for this endpoint.",
    "Use Basic Authentication over TLS only.",
    "the key looks like sk-ant-oat01-... and is stored in local.cred.env",
    '{"sessionId":"0b8f6a8e-1c2d-4e5f-8a9b-0c1d2e3f4a5b","type":"user"}',
    "session_id: 42",
    "cancelToken: source.token,",
    ".btn { color: #ff00aa; --token-primary: #1a2b3c; }",
    '"integrity": "sha512-Fq0lBZW3rTGjVxdC2VgR5xU9iYLmLfXN0kU7f3Y0hZJ8cWl6xZ4o3pIeA0v9z2PqH8rK1mT5sN7uJbQ4wE6yDg==",',
    "commit 3f2a9c1b7e5d4a6f8b0c2e4d6f8a0b1c3d5e7f9a",
    "index 83db48f..bf2a3c1 100644",
    "a1b2c3d (HEAD -> main, origin/main) fix(auth): rotate token on refresh",
    "2c1f6f0e-9d1a-4b7e-8f3c-5a6b7c8d9e0f",
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==",
    '"image": "ghcr.io/org/app@sha256:4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e"',
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFAKEfakeFAKEfakeFAKEfakeFAKEfakeFAKEfake alex@laptop",
    "-----BEGIN CERTIFICATE-----\nMIIBFAKEfakeFAKEfakeFAKEfake0123456789\n-----END CERTIFICATE-----",
    "A password reset link was sent to your email.",
    "Enter your password:",
    "if password == confirm_password:",
    "token_count = len(tokens)",
    "https://github.com/examplecorp/arena/pull/42",
    "git@github.com:examplecorp/arena.git",
    "http://localhost:8000/api/v1/auth/login/",
    "2026-10-04T12:34:56.789Z INFO auth.middleware: request ok",
    "ssh dwpvmaster 'docker service ls'",
    '"tool_use_id": "toolu_01ABCdefGHIjklMNOpqrSTUv"',
    "except AuthenticationFailed as exc:",
    "from django.contrib.auth import authenticate",
    "# TODO: rotate the token after deploy",
    "Tokens: 1.2M in, 34k out",
    "password-flags=22",
    "ColorToken.chipSelectedBackground: Color(0xFF1A2B3C),",
    "fcm_token__isnull=True",
    "final authControllerProvider = NotifierProvider<AuthController, AuthState>(AuthController.new);",
    "botocore.credentials: Found credentials in environment variables.",
    '"x-api-key": process.env.TOWER_API_KEY,',
    "x-api-key-user: alex",
    "AWS_ACCESS_KEY_ID: ${{ secrets.AWS_ACCESS_KEY_ID }}",
    "X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Date=20261004T000000Z",
    'redis-cli -a "$REDIS_PASSWORD" ping',
    'PGPASSWORD="$DB_PASSWORD" psql -h db',
    "password: ********",
    "token: [redacted:token]",
    "Password: stored in the vault, not here",
    "pass_rate: 0.93",
    "movie_session: 12",
    "has_password: true",
    "credential_name: stage-ssh",
    "sessionExemptPaths = [",
    "See https://docs.djangoproject.com/en/5.0/topics/auth/passwords/",
    "| Session | Started | Tokens |",
    "--password-stdin",
    "password: ''",
    "auth: true",
    'REDEEM_STAFF_PASS = "redeem_staff_pass"',
    "REDEEM_STAFF_PASS: 'staff_pass.redeem'",
    'TOKEN_KEY = "auth_token"',
    'PASSWORD_FIELD = "user_password"',
    "SESSION_COOKIE_NAME = 'sessionid'",
    'API_KEY_HEADER = "x-api-key"',
    "secret-tool: /usr/bin/secret-tool",
    "staff_pass: 10234",
    "ColorToken.navBarBackground: 0xFF101828,",
    "SecureKeys.token: 'auth_token',",
    'credential: "Ubuntu prod"',
    "TokenOrigin: TokenOrigin.refresh,",
    "ROLE_BY_TOKEN: Record<string, Role> = {",
    "TOKEN_REFUSED: Final = 4001",
    "TOKEN_REGISTRY: dict[str, Token] = {}",
    "const generateApiKey = (len) => crypto.randomBytes(len)",
]


def _kinds(text: str) -> Counter:
    return Counter(re.findall(r"\[redacted:([a-z0-9-]+)\]", text))


def test_positive_cases():
    failures = []
    for cid, text, kinds, secrets in POSITIVE:
        out, n = redact(text)
        got = _kinds(out)
        problems = []
        for s in secrets:
            if s in out:
                problems.append(f"leaked {s[:24]!r}")
        if dict(got) != kinds:
            problems.append(f"kinds {dict(got)} != {kinds}")
        if n != sum(kinds.values()):
            problems.append(f"count {n} != {sum(kinds.values())}")
        if problems:
            failures.append(f"{cid}: {'; '.join(problems)} -> {out[:160]!r}")
    assert not failures, "\n".join(failures)


def test_negative_lines_untouched():
    failures = []
    for line in NEGATIVE:
        out, n = redact(line)
        if out != line or n:
            failures.append(f"{line[:70]!r} -> {out[:90]!r} (n={n})")
    assert not failures, "\n".join(failures)


def test_idempotent():
    for _cid, text, _k, _s in POSITIVE:
        once, _ = redact(text)
        twice, n2 = redact(once)
        assert twice == once and n2 == 0, (_cid, once, twice)


def test_raw_jsonl_line_stays_valid_json():
    line = json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {
            "command": "PGPASSWORD=FakePg456 psql -h db -c \"select 1\" && curl -H \"Authorization: Token "
                       + "0123456789abcdef" * 2 + "\" http://localhost:8000/api/ && cat key.pem",
        }}]},
        "toolUseResult": {"stdout": PEM_RSA + "\nDB_PASSWORD=FakeEnv2\n"},
        "sessionId": "0b8f6a8e-1c2d-4e5f-8a9b-0c1d2e3f4a5b",
        "usage": {"input_tokens": 3, "output_tokens": 120},
    })
    out, n = redact(line)
    obj = json.loads(out)  # still valid JSON
    assert n == 4, (n, out)
    for s in ("FakePg456", "0123456789abcdef" * 2, "MIIEFAKE", "FakeEnv2"):
        assert s not in out
    assert obj["sessionId"] == "0b8f6a8e-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
    assert obj["usage"] == {"input_tokens": 3, "output_tokens": 120}


def test_no_catastrophic_backtracking():
    import time
    blobs = ["a" * 20000 + "1", "A1" * 10000, "_" * 20000, "1" * 20000 + "x", "-" * 20000, "a." * 10000 + "!",
             "\\" * 20000, "%" * 20000 + "z", "=" * 20000, "a:" * 10000, "a_" * 10000 + "1",
             "auth=" * 4000, "token=token" * 2000, "curl " * 4000, "eyJ" * 7000, "sk-ant-" * 3000,
             "echo '" * 3000, "mysql " * 3000, "Cookie: a=" * 2000, "x" * 15 + "1" * 20000]
    heads = ["password: ", "password=", "token: '", 'api_key="', "Authorization: Bearer ", "Cookie: ",
             "postgres://u:", "https://", "curl -u a:", "redis-cli -a ", "echo ", 'os.getenv("SECRET", "',
             "ACL SETUSER u on >", "user u on >", "-----BEGIN PRIVATE KEY-----\n", "eyJ", "sk-ant-", "<password>",
             "X-Amz-Signature=", "mysql -p", "secret: ", "sessionid=", "credential: ", "staff_pass: "]
    worst = 0.0
    for h in heads:
        for b in blobs:
            for text in (h + b, b + h + b):
                t0 = time.perf_counter()
                redact(text)
                worst = max(worst, time.perf_counter() - t0)
    assert worst < 0.5, f"slowest pathological input took {worst:.2f}s"


def test_empty_and_plain():
    assert redact("") == ("", 0)
    assert redact("hello world") == ("hello world", 0)


def test_scrub_helpers():
    assert scrub(None) == "" and scrub("plain words") == "plain words"
    out = scrub_obj({"a": ["TOKEN=FAKEfake0123456789x"], "n": 3})
    assert "FAKEfake0123456789x" not in str(out) and out["n"] == 3
