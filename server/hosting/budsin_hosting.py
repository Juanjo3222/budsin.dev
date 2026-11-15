#!/usr/bin/env python3
"""Budsin Hosting: alojamiento de sitios estáticos con planes, apagado automático y cobro por PayPal.

Solo biblioteca estándar de Python 3 (sin dependencias que instalar).

- Identidad: token de sesión de Firebase Auth, validado contra `accounts:lookup` de Google.
- Cada cuenta tiene un subdominio (usuario.budsin.dev) y varios sitios en rutas (usuario.budsin.dev/sitio).
- Caddy sirve los sitios desde SITES_DIR solo si existe el archivo-bandera ACTIVE_DIR/<usuario>:
  encender = crear la bandera, apagar = borrarla. Los datos nunca se borran al apagar.
- PayPal es la fuente de verdad de los pagos: se consulta con el Secret y se comprueba plan, estado y dueño.

Configuración por variables de entorno (ver env.example). Los secretos nunca van en el repositorio.
"""
import base64
import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import moderation  # noqa: E402

PREFIX = "/api/hosting"
GB = 1024 ** 3
HOUR = 3600
DAY = 24 * HOUR
GRACE_SECONDS = 2 * DAY          # margen tras la fecha de cobro de PayPal
FALLBACK_PERIOD = 32 * DAY
TOKEN_TTL = 300
MAX_FILES = 5000
MAX_FILE_BYTES = 200 * 1024 * 1024
SCAN_BYTES = 2 * 1024 * 1024

# file_mb / upload_mb: tope por archivo y por subida (None = el máximo del servidor). badge: insignia en los .html.
PLANS = {
    "free":  {"name": "Free",  "gb": 1,  "hours": 12,      "warn_hours": 3,  "price": 0.00, "sites": 1,
              "file_mb": 15, "upload_mb": 40, "badge": True, "domains": 0},
    "plus":  {"name": "Plus",  "gb": 5,  "hours": 5 * 24,  "warn_hours": 24, "price": 1.99, "sites": 10,
              "file_mb": None, "upload_mb": None, "badge": False, "domains": 1},
    "pro":   {"name": "Pro",   "gb": 10, "hours": 14 * 24, "warn_hours": 24, "price": 3.99, "sites": 25,
              "file_mb": None, "upload_mb": None, "badge": False, "domains": 3},
    "ultra": {"name": "Ultra", "gb": 15, "hours": None,    "warn_hours": 0,  "price": 6.99, "sites": 50,
              "file_mb": None, "upload_mb": None, "badge": False, "domains": 10},
}
DOMAIN_PENDING_DAYS = 7   # un dominio sin verificar libera su nombre pasado este tiempo
PURGE_DAYS = 90        # los sitios del plan Free que llevan tanto tiempo apagados se borran
PURGE_WARN_DAYS = 60   # desde aquí se avisa en el panel y el historial
BADGE_START, BADGE_END = "<!--budsin-badge-->", "<!--/budsin-badge-->"
BADGE_HTML = (
    BADGE_START
    + '<a href="https://budsin.dev/hosting/" target="_blank" rel="noopener" style="position:fixed;right:10px;bottom:10px;'
    'z-index:2147483647;font:600 11px/1 system-ui,sans-serif;color:#fff;background:rgba(10,10,11,.78);padding:7px 10px;'
    'border-radius:99px;text-decoration:none;letter-spacing:.02em">Hosted by Budsin Hosting</a>'
    + BADGE_END
)
BADGE_RE = re.compile(re.escape(BADGE_START).encode() + rb".*?" + re.escape(BADGE_END).encode(), re.S)
BODY_CLOSE_RE = re.compile(rb"</body\s*>(?!.*</body\s*>)", re.I | re.S)
PLAN_ORDER = ["free", "plus", "pro", "ultra"]
MAX_STRIKES = 3        # al llegar aquí la cuenta queda vetada para siempre
BAN_KINDS = ("soft", "temp", "perm")

USERNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,28})[a-z0-9]$")
SITE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}$")
ALLOWED_EXT = {
    ".html", ".htm", ".css", ".js", ".mjs", ".json", ".txt", ".md", ".xml", ".svg", ".csv", ".map", ".webmanifest",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico", ".bmp",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp3", ".ogg", ".wav", ".m4a", ".mp4", ".webm", ".pdf",
    ".wasm", ".data", ".unityweb", ".br", ".gz",
}
TEXT_EXT = {".html", ".htm", ".css", ".js", ".mjs", ".json", ".txt", ".md", ".xml", ".svg", ".csv"}
EDITABLE_EXT = TEXT_EXT | {".webmanifest"}   # los que se pueden abrir en el editor del panel
EDIT_MAX = 512 * 1024
MAX_DEPTH = 8
NAME_BAD = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')
STARTER_HTML = (
    '<!doctype html>\n<html lang="en">\n<head>\n  <meta charset="utf-8">\n'
    '  <meta name="viewport" content="width=device-width, initial-scale=1">\n  <title>{name}</title>\n</head>\n'
    '<body>\n  <h1>Hello, world!</h1>\n  <p>Edit this page from your Budsin Hosting dashboard.</p>\n</body>\n</html>\n'
)


def env(name, default=""):
    return os.environ.get(name, default).strip()


class Config:
    def __init__(self):
        self.host = env("HOST", "127.0.0.1")
        self.port = int(env("PORT", "3102"))
        self.data_dir = env("DATA_DIR", "/var/lib/budsin-hosting")
        self.sites_dir = env("SITES_DIR", "/var/www/hosting/sites")
        self.active_dir = env("ACTIVE_DIR", os.path.join(self.data_dir, "active"))
        self.base_domain = env("BASE_DOMAIN", "budsin.dev")
        self.firebase_key = env("FIREBASE_API_KEY")
        self.lookup_url = env("FIREBASE_LOOKUP_URL", "https://identitytoolkit.googleapis.com/v1/accounts:lookup")
        self.admins = {e.strip().lower() for e in env("ADMIN_EMAILS", "juvaldiviam@gmail.com").split(",") if e.strip()}
        self.origins = [o.strip() for o in env("ALLOWED_ORIGINS", "https://budsin.dev").split(",") if o.strip()]
        self.rate_limit = int(env("RATE_LIMIT", "90"))      # peticiones por minuto y por IP
        self.upload_rate = int(env("UPLOAD_RATE", "12"))    # subidas de .zip por minuto y por IP
        self.files_rate = int(env("FILES_RATE", "300"))     # operaciones del gestor de archivos por minuto y por IP
        self.max_upload = int(float(env("MAX_UPLOAD_MB", "95")) * 1024 * 1024)
        self.total_cap = int(float(env("TOTAL_CAP_GB", "40")) * GB)
        self.paypal_api = env("PAYPAL_API_BASE", "https://api-m.sandbox.paypal.com").rstrip("/")
        self.paypal_client = env("PAYPAL_CLIENT_ID")
        self.paypal_secret = env("PAYPAL_SECRET")
        self.paypal_webhook = env("PAYPAL_WEBHOOK_ID")
        self.paypal_plans = {p: env("PAYPAL_PLAN_" + p.upper()) for p in ("plus", "pro", "ultra")}
        # Dominios propios (Cloudflare for SaaS: Custom Hostnames)
        self.cf_api = env("CF_API_BASE", "https://api.cloudflare.com/client/v4").rstrip("/")
        self.cf_token = env("CF_API_TOKEN")
        self.cf_zone = env("CF_ZONE_ID")
        self.cname_target = env("CUSTOM_CNAME_TARGET", "custom.budsin.dev").lower()
        self.domains_dir = env("DOMAINS_DIR", "/var/www/hosting/domains")
        self.active_domains_dir = env("ACTIVE_DOMAINS_DIR", "/var/www/hosting/active-domains")

    def auth_ready(self):
        return bool(self.firebase_key)

    def domains_ready(self):
        return bool(self.cf_token and self.cf_zone)

    def paypal_ready(self):
        return bool(self.paypal_client and self.paypal_secret and all(self.paypal_plans.values()))


class HttpError(Exception):
    def __init__(self, status, code, **extra):
        super().__init__(code)
        self.status, self.code, self.extra = status, code, extra


def http_json(url, data=None, headers=None, method=None, timeout=15):
    body = None
    hdrs = dict(headers or {})
    if data is not None:
        body = data if isinstance(data, bytes) else json.dumps(data).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as err:
        raw = err.read()
        try:
            return err.code, json.loads(raw) if raw else {}
        except ValueError:
            return err.code, {}


def parse_time(value):
    if not value:
        return 0
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None


def dir_stats(path):
    total, count = 0, 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            with contextlib.suppress(OSError):
                total += os.path.getsize(os.path.join(root, f))
                count += 1
    return total, count


def dir_size(path):
    return dir_stats(path)[0]


class Store:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with self.conn() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    uid TEXT PRIMARY KEY, email TEXT, username TEXT UNIQUE NOT NULL,
                    plan TEXT NOT NULL DEFAULT 'free', plan_until INTEGER NOT NULL DEFAULT 0,
                    powered INTEGER NOT NULL DEFAULT 0, powered_since INTEGER NOT NULL DEFAULT 0,
                    off_at INTEGER NOT NULL DEFAULT 0, last_free_on INTEGER NOT NULL DEFAULT 0,
                    warned INTEGER NOT NULL DEFAULT 0, banned INTEGER NOT NULL DEFAULT 0,
                    used_bytes INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL,
                    off_since INTEGER NOT NULL DEFAULT 0, purge_warned INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS sites (
                    uid TEXT NOT NULL, name TEXT NOT NULL, bytes INTEGER NOT NULL DEFAULT 0,
                    files INTEGER NOT NULL DEFAULT 0, updated INTEGER NOT NULL, PRIMARY KEY (uid, name));
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT NOT NULL, ts INTEGER NOT NULL,
                    event TEXT NOT NULL, detail TEXT);
                CREATE INDEX IF NOT EXISTS history_uid ON history(uid, id);
                CREATE TABLE IF NOT EXISTS strikes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT NOT NULL, ts INTEGER NOT NULL,
                    delta INTEGER NOT NULL, total INTEGER NOT NULL, reason TEXT, actor TEXT);
                CREATE INDEX IF NOT EXISTS strikes_uid ON strikes(uid, id);
                CREATE TABLE IF NOT EXISTS domains (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT NOT NULL, site TEXT NOT NULL,
                    domain TEXT UNIQUE NOT NULL, status TEXT NOT NULL DEFAULT 'pending', cf_id TEXT,
                    info TEXT NOT NULL DEFAULT '{}', created INTEGER NOT NULL, checked INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS domains_uid ON domains(uid, id);
                CREATE TABLE IF NOT EXISTS subs (
                    sub_id TEXT PRIMARY KEY, uid TEXT NOT NULL, plan TEXT NOT NULL, status TEXT,
                    paid_until INTEGER NOT NULL DEFAULT 0, last_checked INTEGER NOT NULL DEFAULT 0,
                    created INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS subs_uid ON subs(uid);
                CREATE TABLE IF NOT EXISTS reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, url TEXT, reason TEXT,
                    contact TEXT, status TEXT NOT NULL DEFAULT 'open');
                """
            )

        with self.conn() as db:  # bases creadas antes de añadir estas columnas
            cols = {r["name"] for r in db.execute("PRAGMA table_info(accounts)")}
            for col in ("off_since", "purge_warned", "strikes", "ban_until"):
                if col not in cols:
                    db.execute(f"ALTER TABLE accounts ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
            for col in ("ban_kind", "ban_reason"):
                if col not in cols:
                    db.execute(f"ALTER TABLE accounts ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
            db.execute("UPDATE accounts SET ban_kind='perm' WHERE banned=1 AND ban_kind=''")

    @contextlib.contextmanager
    def conn(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            with db:
                yield db
        finally:
            db.close()

    def q(self, sql, args=()):
        with self.conn() as db:
            return db.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def run(self, sql, args=()):
        with self.conn() as db:
            return db.execute(sql, args).rowcount

    def log(self, uid, event, detail=""):
        self.run("INSERT INTO history (uid, ts, event, detail) VALUES (?,?,?,?)", (uid, int(time.time()), event, detail))


class Service:
    def __init__(self, cfg):
        self.cfg = cfg
        self.store = Store(os.path.join(cfg.data_dir, "hosting.sqlite3"))
        for d in (cfg.sites_dir, cfg.active_dir, cfg.domains_dir, cfg.active_domains_dir):
            os.makedirs(d, mode=0o755, exist_ok=True)
        self._tokens = {}
        self._buckets = {"api": {}, "upload": {}, "files": {}}
        self._pp_token = ("", 0)
        self._lock = threading.Lock()
        self._upload_lock = threading.Lock()
        self._domain_lock = threading.Lock()

    # ---------- identidad ----------
    def verify_user(self, bearer):
        if not bearer or not bearer.lower().startswith("bearer "):
            raise HttpError(401, "missing_token")
        token = bearer[7:].strip()
        key = hashlib.sha256(token.encode()).hexdigest()
        now = time.time()
        with self._lock:
            hit = self._tokens.get(key)
            if hit and hit[3] > now:
                return hit[:3]
        status, data = http_json(f"{self.cfg.lookup_url}?key={self.cfg.firebase_key}", {"idToken": token})
        users = data.get("users") if isinstance(data, dict) else None
        if status != 200 or not users or not users[0].get("localId"):
            raise HttpError(401, "invalid_token")
        u = users[0]
        who = (u["localId"], (u.get("email") or "").lower(), bool(u.get("emailVerified")))
        with self._lock:
            self._tokens = {k: v for k, v in self._tokens.items() if v[3] > now}
            self._tokens[key] = who + (now + TOKEN_TTL,)
        return who

    def require_admin(self, bearer):
        uid, email, verified = self.verify_user(bearer)
        if not (verified and email in self.cfg.admins):
            raise HttpError(403, "not_admin")
        return uid, email

    # ---------- cuentas y planes ----------
    def effective_plan(self, acc, now=None):
        now = now or int(time.time())
        if acc["plan"] != "free" and acc["plan_until"] > now:
            return acc["plan"]
        return "free"

    def account(self, uid):
        return self.store.one("SELECT * FROM accounts WHERE uid=?", (uid,))

    def need_account(self, uid):
        acc = self.account(uid)
        if not acc:
            raise HttpError(404, "no_account")
        return acc

    def flag_path(self, username):
        return os.path.join(self.cfg.active_dir, username)

    def set_flag(self, username, on):
        path = self.flag_path(username)
        if on:
            with open(path, "w") as fh:
                fh.write("1")
            os.chmod(path, 0o644)
        else:
            with contextlib.suppress(FileNotFoundError):
                os.remove(path)

    def site_url(self, username, site=None):
        base = f"https://{username}.{self.cfg.base_domain}/"
        return base + (site + "/" if site else "")

    def register(self, uid, email, username):
        username = (username or "").strip().lower()
        if self.account(uid):
            raise HttpError(409, "already_registered")
        if USERNAME_RE.match(username) and self.store.one("SELECT 1 FROM accounts WHERE username=?", (username,)):
            raise HttpError(409, "username_taken")
        reason = self.check_username(username)
        if reason:
            raise HttpError(400, "bad_username", reason=reason)
        try:
            self.store.run(
                "INSERT INTO accounts (uid, email, username, created) VALUES (?,?,?,?)",
                (uid, email, username, int(time.time())),
            )
        except sqlite3.IntegrityError:
            raise HttpError(409, "username_taken")
        os.makedirs(os.path.join(self.cfg.sites_dir, username), mode=0o755, exist_ok=True)
        self.render_index(username, [])
        self.store.log(uid, "register", username)
        return self.me(uid)

    def check_username(self, username):
        if not USERNAME_RE.match(username):
            return "usa de 3 a 30 letras minúsculas, números o guiones (sin empezar ni acabar en guion)"
        bad = moderation.bad_name(username)
        if bad:
            return bad
        if self.store.one("SELECT 1 FROM accounts WHERE username=?", (username,)):
            return "ya está en uso"
        return None

    def power_on(self, uid, forced=False, actor=None):
        acc = self.need_account(uid)
        now = int(time.time())
        if acc["banned"] and not forced:
            raise HttpError(403, "banned")
        if acc["powered"]:
            raise HttpError(409, "already_on")
        plan = self.effective_plan(acc, now)
        hours = PLANS[plan]["hours"]
        off_at = now + hours * HOUR if hours else 0
        self.store.run(
            "UPDATE accounts SET powered=1, powered_since=?, off_at=?, warned=0 WHERE uid=?",
            (now, off_at, uid),
        )
        self.set_flag(acc["username"], True)
        self.store.log(uid, "power_on" if not forced else "forced_on", (actor or "") + f" plan={plan}")
        self.sync_domains()
        return self.me(uid)

    def power_off(self, uid, event="power_off", detail=""):
        acc = self.need_account(uid)
        if not acc["powered"] and not os.path.exists(self.flag_path(acc["username"])):
            return self.me(uid)
        self.store.run("UPDATE accounts SET powered=0, off_at=0, warned=0, off_since=?, purge_warned=0 WHERE uid=?",
                       (int(time.time()), uid))
        self.set_flag(acc["username"], False)
        self.store.log(uid, event, detail)
        self.sync_domains()
        return self.me(uid)

    def purge_time(self, acc, plan, now=None):
        """Cuándo se borrarán los sitios de una cuenta Free apagada (None si no corre peligro)."""
        if plan != "free" or acc["powered"] or acc["banned"]:
            return None
        if not self.store.one("SELECT 1 FROM sites WHERE uid=?", (acc["uid"],)):
            return None
        return (acc["off_since"] or acc["created"]) + PURGE_DAYS * DAY

    def me(self, uid):
        acc = self.need_account(uid)
        now = int(time.time())
        plan = self.effective_plan(acc, now)
        p = PLANS[plan]
        limit = p["gb"] * GB
        used = acc["used_bytes"]
        remaining = max(0, acc["off_at"] - now) if acc["powered"] and acc["off_at"] else None
        warn = bool(acc["powered"] and acc["off_at"] and p["warn_hours"] and acc["off_at"] - now <= p["warn_hours"] * HOUR)
        purge_at = self.purge_time(acc, plan, now)
        sites = [
            {"name": s["name"], "bytes": s["bytes"], "files": s["files"], "updated": iso(s["updated"]),
             "url": self.site_url(acc["username"], s["name"])}
            for s in self.store.q("SELECT * FROM sites WHERE uid=? ORDER BY name", (uid,))
        ]
        hist = [
            {"ts": iso(h["ts"]), "event": h["event"], "detail": h["detail"]}
            for h in self.store.q("SELECT * FROM history WHERE uid=? ORDER BY id DESC LIMIT 100", (uid,))
        ]
        return {
            "username": acc["username"], "email": acc["email"], "url": self.site_url(acc["username"]),
            "plan": plan, "planName": p["name"], "planUntil": iso(acc["plan_until"]) if plan != "free" else None,
            "banned": bool(acc["banned"]), "banKind": acc["ban_kind"] if acc["banned"] else "",
            "banUntil": iso(acc["ban_until"]) if acc["banned"] and acc["ban_until"] else None,
            "banReason": acc["ban_reason"] if acc["banned"] else "",
            "strikes": acc["strikes"], "maxStrikes": MAX_STRIKES,
            "state": "on" if acc["powered"] else "off", "poweredSince": iso(acc["powered_since"]) if acc["powered"] else None,
            "offAt": iso(acc["off_at"]) if acc["powered"] and acc["off_at"] else None, "secondsLeft": remaining,
            "neverOff": bool(acc["powered"] and not acc["off_at"]),
            "warning": warn, "purgeAt": iso(purge_at) if purge_at else None,
            "purgeWarning": bool(purge_at and purge_at - now <= (PURGE_DAYS - PURGE_WARN_DAYS) * DAY),
            "badge": PLANS[plan]["badge"], "fileMb": PLANS[plan]["file_mb"], "uploadMb": round(self.upload_limit(plan) / 1024 / 1024),
            "storage": {"used": used, "limit": limit, "percent": round(100 * used / limit, 1) if limit else 0,
                        "warn80": used >= 0.8 * limit, "full": used >= limit},
            "maxSites": p["sites"], "domainsLimit": p["domains"], "sites": sites, "history": hist,
            "paypalReady": self.cfg.paypal_ready(),
        }

    # ---------- sitios ----------
    def render_index(self, username, sites):
        links = "".join(f'<li><a href="/{html_escape(s)}/">{html_escape(s)}</a></li>' for s in sites) or "<li>Aún no hay sitios.</li>"
        page = (
            '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{html_escape(username)}</title>"
            "<style>body{font:16px system-ui;background:#0a0a0b;color:#fafafa;max-width:560px;margin:15vh auto;padding:0 20px}"
            "a{color:#a78bfa}small{color:#a1a1aa}</style>"
            f"<h1>{html_escape(username)}</h1><ul>{links}</ul>"
            '<p><small>Alojado con <a href="https://budsin.dev/hosting/">Budsin Hosting</a></small></p>'
        )
        path = os.path.join(self.cfg.sites_dir, username, "index.html")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(page)
        os.chmod(path, 0o644)

    def refresh_usage(self, uid, username):
        rows = self.store.q("SELECT name FROM sites WHERE uid=?", (uid,))
        total = 0
        for r in rows:
            b, n = dir_stats(os.path.join(self.cfg.sites_dir, username, r["name"]))
            total += b
            self.store.run("UPDATE sites SET bytes=?, files=?, updated=? WHERE uid=? AND name=?", (b, n, int(time.time()), uid, r["name"]))
        self.store.run("UPDATE accounts SET used_bytes=? WHERE uid=?", (total, uid))
        self.render_index(username, [r["name"] for r in rows])
        return total

    def upload(self, uid, site, body_stream, length):
        site = (site or "").strip().lower()
        if not SITE_RE.match(site):
            raise HttpError(400, "bad_site_name", reason="usa letras minúsculas, números o guiones (máx. 39)")
        reason = moderation.bad_name(site, subdomain=False)
        if reason:
            raise HttpError(400, "bad_site_name", reason=reason)
        acc = self.need_account(uid)
        if acc["banned"]:
            raise HttpError(403, "banned")
        plan = self.effective_plan(acc)
        limit = PLANS[plan]["gb"] * GB
        if acc["used_bytes"] >= limit:
            raise HttpError(413, "storage_full", limit=limit)
        if length <= 0:
            raise HttpError(400, "empty_body")
        max_upload = self.upload_limit(plan)
        if length > max_upload:
            raise HttpError(413, "upload_too_large", max=max_upload)
        tmpdir = tempfile.mkdtemp(prefix=".up-", dir=self.cfg.sites_dir)
        try:
            zpath = os.path.join(tmpdir, "in.zip")
            got = 0
            with open(zpath, "wb") as fh:
                while got < length:
                    chunk = body_stream.read(min(1024 * 1024, length - got))
                    if not chunk:
                        break
                    fh.write(chunk)
                    got += len(chunk)
            if got != length:
                raise HttpError(400, "truncated_upload")
            extracted = os.path.join(tmpdir, "site")
            nfiles, nbytes = self.extract_zip(zpath, extracted, acc, limit, site, plan)
            with self._upload_lock:
                acc = self.need_account(uid)
                existing = self.store.one("SELECT bytes FROM sites WHERE uid=? AND name=?", (uid, site))
                nsites = self.store.one("SELECT COUNT(*) AS n FROM sites WHERE uid=?", (uid,))["n"]
                if not existing and nsites >= PLANS[plan]["sites"]:
                    raise HttpError(409, "too_many_sites", max=PLANS[plan]["sites"])
                after = acc["used_bytes"] - (existing["bytes"] if existing else 0) + nbytes
                if after > limit:
                    raise HttpError(413, "storage_full", limit=limit, needed=after)
                total_used = self.store.one("SELECT COALESCE(SUM(used_bytes),0) AS s FROM accounts")["s"]
                if total_used - (existing["bytes"] if existing else 0) + nbytes > self.cfg.total_cap:
                    raise HttpError(507, "server_full")
                dest = os.path.join(self.cfg.sites_dir, acc["username"], site)
                os.makedirs(os.path.dirname(dest), mode=0o755, exist_ok=True)
                old = dest + ".old-" + str(int(time.time() * 1000))
                if os.path.exists(dest):
                    os.rename(dest, old)
                os.rename(extracted, dest)
                shutil.rmtree(old, ignore_errors=True)
                for root, dirs, files in os.walk(dest):
                    os.chmod(root, 0o755)
                    for f in files:
                        os.chmod(os.path.join(root, f), 0o644)
                self.store.run(
                    "INSERT INTO sites (uid,name,bytes,files,updated) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(uid,name) DO UPDATE SET bytes=excluded.bytes, files=excluded.files, updated=excluded.updated",
                    (uid, site, nbytes, nfiles, int(time.time())),
                )
                self.refresh_usage(uid, acc["username"])
            self.store.log(uid, "upload", f"{site}: {nfiles} archivos, {nbytes} bytes")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        return self.me(uid)

    def upload_limit(self, plan):
        mb = PLANS[plan]["upload_mb"]
        return min(self.cfg.max_upload, mb * 1024 * 1024) if mb else self.cfg.max_upload

    def file_limit(self, plan):
        mb = PLANS[plan]["file_mb"]
        return mb * 1024 * 1024 if mb else MAX_FILE_BYTES

    def extract_zip(self, zpath, dest, acc, limit, site, plan="free"):
        try:
            zf = zipfile.ZipFile(zpath)
        except zipfile.BadZipFile:
            raise HttpError(400, "bad_zip")
        with zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            if not infos:
                raise HttpError(400, "empty_zip")
            if len(infos) > MAX_FILES:
                raise HttpError(400, "too_many_files", max=MAX_FILES)
            names = [i.filename.replace("\\", "/") for i in infos]
            # Una carpeta raíz única se aplana: así vale tanto un zip de la carpeta como de su contenido.
            tops = {n.split("/", 1)[0] for n in names}
            strip = len(tops) == 1 and all("/" in n for n in names)
            total = 0
            plan_files = []
            for info, name in zip(infos, names):
                rel = name.split("/", 1)[1] if strip else name
                parts = [p for p in rel.split("/") if p not in ("", ".")]
                if not parts or any(p == ".." for p in parts) or rel.startswith("/") or ":" in parts[0]:
                    raise HttpError(400, "unsafe_path", file=name)
                if (info.external_attr >> 28) == 0xA:  # enlace simbólico
                    raise HttpError(400, "symlink_not_allowed", file=name)
                if any(p.startswith(".") for p in parts):
                    continue  # se ignoran archivos ocultos (.git, .DS_Store...)
                if parts[0] == "__MACOSX":
                    continue
                ext = os.path.splitext(parts[-1])[1].lower()
                if ext not in ALLOWED_EXT:
                    raise HttpError(400, "file_type_not_allowed", file=name, ext=ext or "(sin extensión)")
                if info.file_size > self.file_limit(plan):
                    raise HttpError(413, "file_too_large", file=name, max=self.file_limit(plan))
                total += info.file_size
                if total > limit:
                    raise HttpError(413, "storage_full", limit=limit, needed=total)
                plan_files.append((info, "/".join(parts), ext))
            if not plan_files:
                raise HttpError(400, "empty_zip")
            if not any(p == "index.html" for _i, p, _e in plan_files):
                raise HttpError(400, "missing_index", reason="el .zip debe incluir un index.html en su raíz")
            os.makedirs(dest, exist_ok=True)
            written = 0
            for info, rel, ext in plan_files:
                target = os.path.join(dest, *rel.split("/"))
                if os.path.commonpath([dest, os.path.abspath(target)]) != os.path.abspath(dest):
                    raise HttpError(400, "unsafe_path", file=rel)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as out:
                    size = 0
                    while True:
                        buf = src.read(1024 * 1024)
                        if not buf:
                            break
                        size += len(buf)
                        if size > info.file_size + 1024:  # cabecera mentirosa: bomba de compresión
                            raise HttpError(400, "bad_zip")
                        out.write(buf)
                written += size
                if ext in TEXT_EXT and size <= SCAN_BYTES:
                    self.scan_file(target, rel, ext)
                if ext in (".html", ".htm") and PLANS[plan]["badge"]:
                    self.brand_file(target)
            return len(plan_files), dir_size(dest)

    def brand_file(self, path):
        """Añade la insignia de Budsin Hosting al final del <body> (una sola vez)."""
        with open(path, "rb") as fh:
            data = fh.read()
        data = BADGE_RE.sub(b"", data)  # se quita cualquier marca existente (también una pegada a mano) y se pone la oficial
        badge = BADGE_HTML.encode()
        m = BODY_CLOSE_RE.search(data)
        data = data[: m.start()] + badge + data[m.start():] if m else data + badge
        with open(path, "wb") as fh:
            fh.write(data)

    def unbrand_file(self, path):
        with open(path, "rb") as fh:
            data = fh.read()
        new = BADGE_RE.sub(b"", data)
        if new != data:
            with open(path, "wb") as fh:
                fh.write(new)

    def apply_branding(self, uid):
        """Pone o quita la insignia en todos los .html de la cuenta según su plan actual."""
        acc = self.account(uid)
        if not acc:
            return
        want = PLANS[self.effective_plan(acc)]["badge"]
        base = os.path.join(self.cfg.sites_dir, acc["username"])
        for site in self.store.q("SELECT name FROM sites WHERE uid=?", (uid,)):
            for root, _dirs, files in os.walk(os.path.join(base, site["name"])):
                for f in files:
                    if f.lower().endswith((".html", ".htm")):
                        path = os.path.join(root, f)
                        with contextlib.suppress(OSError):
                            (self.brand_file if want else self.unbrand_file)(path)
        self.refresh_usage(uid, acc["username"])
        self.sync_domains()

    def scan_file(self, path, rel, ext):
        with open(path, "rb") as fh:
            text = fh.read(SCAN_BYTES).decode("utf-8", "ignore")
        if ext in (".html", ".htm"):
            visible = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
            visible = re.sub(r"(?s)<[^>]+>", " ", visible)
            hint = moderation.phishing_hint(text)
            if hint:
                raise HttpError(400, "content_blocked", file=rel, reason=hint)
            text = visible
        elif ext in (".js", ".css", ".json", ".map", ".svg", ".xml"):
            return  # solo se revisa texto visible
        reason = moderation.bad_text(text)
        if reason:
            raise HttpError(400, "content_blocked", file=rel, reason=reason)

    # ---------- gestor de archivos ----------
    def clean_rel(self, rel, allow_empty=True):
        """Convierte una ruta relativa del cliente en una lista de componentes seguros."""
        rel = (rel or "").replace("\\", "/")
        parts = []
        for part in rel.split("/"):
            if part in ("", "."):
                continue
            if part == ".." or part.startswith(".") or NAME_BAD.search(part) or len(part) > 100 or part != part.strip() or part.endswith("."):
                raise HttpError(400, "bad_path")
            parts.append(part)
        if len(parts) > MAX_DEPTH or (not parts and not allow_empty):
            raise HttpError(400, "bad_path")
        return parts

    def site_base(self, acc, site):
        site = (site or "").strip().lower()
        if not SITE_RE.match(site):
            raise HttpError(400, "bad_site_name", reason="usa letras minúsculas, números o guiones (máx. 39)")
        return site, os.path.join(self.cfg.sites_dir, acc["username"], site)

    def inside(self, base, parts):
        """Ruta real dentro del sitio; falla si un enlace o un componente intentara salirse de él."""
        target = os.path.join(base, *parts) if parts else base
        real_base, real = os.path.realpath(base), os.path.realpath(target)
        if real != real_base and not real.startswith(real_base + os.sep):
            raise HttpError(400, "bad_path")
        return target

    def existing_site(self, uid, site):
        acc = self.need_account(uid)
        if acc["banned"]:
            raise HttpError(403, "banned")
        site, base = self.site_base(acc, site)
        if not self.store.one("SELECT 1 FROM sites WHERE uid=? AND name=?", (uid, site)):
            raise HttpError(404, "no_site")
        return acc, site, base

    def ensure_site(self, uid, acc, site, plan):
        """Crea el sitio si no existe (respetando el límite del plan). Se llama con _upload_lock tomado."""
        site, base = self.site_base(acc, site)
        if self.store.one("SELECT 1 FROM sites WHERE uid=? AND name=?", (uid, site)):
            return site, base, False
        reason = moderation.bad_name(site, subdomain=False)
        if reason:
            raise HttpError(400, "bad_site_name", reason=reason)
        if self.store.one("SELECT COUNT(*) AS n FROM sites WHERE uid=?", (uid,))["n"] >= PLANS[plan]["sites"]:
            raise HttpError(409, "too_many_sites", max=PLANS[plan]["sites"])
        os.makedirs(base, mode=0o755, exist_ok=True)
        self.store.run("INSERT INTO sites (uid,name,bytes,files,updated) VALUES (?,?,0,0,?)", (uid, site, int(time.time())))
        return site, base, True

    def check_file_name(self, name):
        ext = os.path.splitext(name)[1].lower()
        if ext not in ALLOWED_EXT:
            raise HttpError(400, "file_type_not_allowed", file=name, ext=ext or "(sin extensión)")
        if moderation.bad_text(os.path.splitext(name)[0]):
            raise HttpError(400, "bad_file_name", file=name)
        return ext

    def entry_info(self, path, name):
        st = os.stat(path)
        is_dir = os.path.isdir(path)
        ext = os.path.splitext(name)[1].lower()
        return {"name": name, "type": "dir" if is_dir else "file", "size": 0 if is_dir else st.st_size, "mtime": iso(int(st.st_mtime)),
                "editable": (not is_dir) and ext in EDITABLE_EXT and st.st_size <= EDIT_MAX}

    def list_files(self, uid, site, rel):
        acc, site, base = self.existing_site(uid, site)
        parts = self.clean_rel(rel)
        target = self.inside(base, parts)
        if not os.path.isdir(target):
            raise HttpError(404, "no_file")
        entries = []
        with os.scandir(target) as it:
            for e in it:
                if not e.name.startswith("."):
                    with contextlib.suppress(OSError):
                        entries.append(self.entry_info(e.path, e.name))
        entries.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
        return {"site": site, "path": "/".join(parts), "entries": entries, "url": self.site_url(acc["username"], site)}

    def read_file(self, uid, site, rel):
        acc, site, base = self.existing_site(uid, site)
        parts = self.clean_rel(rel, allow_empty=False)
        target = self.inside(base, parts)
        if not os.path.isfile(target):
            raise HttpError(404, "no_file")
        ext = os.path.splitext(parts[-1])[1].lower()
        if ext not in EDITABLE_EXT:
            raise HttpError(415, "not_text")
        if os.path.getsize(target) > EDIT_MAX:
            raise HttpError(413, "too_large_to_edit", max=EDIT_MAX)
        with open(target, "rb") as fh:
            data = BADGE_RE.sub(b"", fh.read())  # el editor no enseña la insignia: se vuelve a poner al guardar
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise HttpError(415, "not_text")
        return {"site": site, "path": "/".join(parts), "content": text, "size": len(data)}

    def write_file(self, uid, site, rel, body_stream, length):
        acc = self.need_account(uid)
        if acc["banned"]:
            raise HttpError(403, "banned")
        parts = self.clean_rel(rel, allow_empty=False)
        name = parts[-1]
        ext = self.check_file_name(name)
        plan = self.effective_plan(acc)
        if length < 0:
            raise HttpError(400, "bad_length")
        if length > self.file_limit(plan):
            raise HttpError(413, "file_too_large", file=name, max=self.file_limit(plan))
        if length > self.upload_limit(plan):
            raise HttpError(413, "upload_too_large", max=self.upload_limit(plan))
        limit = PLANS[plan]["gb"] * GB
        fd, tmp = tempfile.mkstemp(prefix=".up-", dir=self.cfg.sites_dir)
        try:
            got = 0
            with os.fdopen(fd, "wb") as fh:
                while got < length:
                    chunk = body_stream.read(min(1024 * 1024, length - got))
                    if not chunk:
                        break
                    fh.write(chunk)
                    got += len(chunk)
            if got != length:
                raise HttpError(400, "truncated_upload")
            if ext in TEXT_EXT and length <= SCAN_BYTES:
                self.scan_file(tmp, name, ext)
            if ext in (".html", ".htm"):
                if PLANS[plan]["badge"]:
                    self.brand_file(tmp)
                else:
                    self.unbrand_file(tmp)
            new_size = os.path.getsize(tmp)
            with self._upload_lock:
                acc = self.need_account(uid)
                site, base, _created = self.ensure_site(uid, acc, site, plan)
                target = self.inside(base, parts)
                if os.path.isdir(target):
                    raise HttpError(409, "is_dir", file=name)
                old = os.path.getsize(target) if os.path.isfile(target) else None
                if old is None and dir_stats(base)[1] >= MAX_FILES:
                    raise HttpError(400, "too_many_files", max=MAX_FILES)
                if acc["used_bytes"] - (old or 0) + new_size > limit:
                    raise HttpError(413, "storage_full", limit=limit, needed=acc["used_bytes"] - (old or 0) + new_size)
                total_used = self.store.one("SELECT COALESCE(SUM(used_bytes),0) AS s FROM accounts")["s"]
                if total_used - (old or 0) + new_size > self.cfg.total_cap:
                    raise HttpError(507, "server_full")
                try:
                    os.makedirs(os.path.dirname(target), mode=0o755, exist_ok=True)
                except (FileExistsError, NotADirectoryError):
                    raise HttpError(400, "bad_path")
                os.replace(tmp, target)
                tmp = None
                os.chmod(target, 0o644)
                self.refresh_usage(uid, acc["username"])
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        return self.me(uid)

    def make_dir(self, uid, site, rel):
        acc, site, base = self.existing_site(uid, site)
        parts = self.clean_rel(rel, allow_empty=False)
        check = parts[-1]
        if moderation.bad_text(check):
            raise HttpError(400, "bad_file_name", file=check)
        target = self.inside(base, parts)
        with self._upload_lock:
            if os.path.lexists(target):
                raise HttpError(409, "exists", file=check)
            try:
                os.makedirs(target, mode=0o755)
            except (FileExistsError, NotADirectoryError):
                raise HttpError(400, "bad_path")
        return {"ok": True}

    def delete_path(self, uid, site, rel):
        acc, site, base = self.existing_site(uid, site)
        parts = self.clean_rel(rel, allow_empty=False)   # nunca la raíz del sitio: para eso está borrar el sitio
        target = self.inside(base, parts)
        with self._upload_lock:
            if not os.path.lexists(target):
                raise HttpError(404, "no_file")
            if os.path.isdir(target) and not os.path.islink(target):
                shutil.rmtree(target)
            else:
                os.remove(target)
            self.refresh_usage(uid, acc["username"])
        return self.me(uid)

    def move_path(self, uid, site, src_rel, dst_rel):
        acc, site, base = self.existing_site(uid, site)
        src_parts = self.clean_rel(src_rel, allow_empty=False)
        dst_parts = self.clean_rel(dst_rel, allow_empty=False)
        src, dst = self.inside(base, src_parts), self.inside(base, dst_parts)
        plan = self.effective_plan(acc)
        with self._upload_lock:
            if not os.path.lexists(src):
                raise HttpError(404, "no_file")
            if os.path.lexists(dst):
                raise HttpError(409, "exists", file=dst_parts[-1])
            if os.path.isdir(src):
                if dst_parts[:len(src_parts)] == src_parts:
                    raise HttpError(400, "bad_path")   # una carpeta no puede entrar en sí misma
                if moderation.bad_text(dst_parts[-1]):
                    raise HttpError(400, "bad_file_name", file=dst_parts[-1])
            else:
                ext = self.check_file_name(dst_parts[-1])
                if ext in TEXT_EXT and os.path.getsize(src) <= SCAN_BYTES:
                    self.scan_file(src, dst_parts[-1], ext)   # renombrar a .html no esquiva el escaneo
            try:
                os.makedirs(os.path.dirname(dst), mode=0o755, exist_ok=True)
            except (FileExistsError, NotADirectoryError):
                raise HttpError(400, "bad_path")
            os.rename(src, dst)
            if os.path.isfile(dst) and os.path.splitext(dst)[1].lower() in (".html", ".htm"):
                (self.brand_file if PLANS[plan]["badge"] else self.unbrand_file)(dst)   # ni la insignia
            self.refresh_usage(uid, acc["username"])
        return self.me(uid)

    def create_site(self, uid, site):
        acc = self.need_account(uid)
        if acc["banned"]:
            raise HttpError(403, "banned")
        plan = self.effective_plan(acc)
        with self._upload_lock:
            site, base, created = self.ensure_site(uid, acc, site, plan)
            if not created:
                raise HttpError(409, "site_exists")
            index = os.path.join(base, "index.html")
            with open(index, "w", encoding="utf-8") as fh:
                fh.write(STARTER_HTML.format(name=html_escape(site)))
            os.chmod(index, 0o644)
            if PLANS[plan]["badge"]:
                self.brand_file(index)
            self.refresh_usage(uid, acc["username"])
        self.store.log(uid, "create_site", site)
        return self.me(uid)

    def delete_site(self, uid, site):
        acc = self.need_account(uid)
        if not self.store.one("SELECT 1 FROM sites WHERE uid=? AND name=?", (uid, site)):
            raise HttpError(404, "no_site")
        self.drop_site_domains(uid, site)
        shutil.rmtree(os.path.join(self.cfg.sites_dir, acc["username"], site), ignore_errors=True)
        self.store.run("DELETE FROM sites WHERE uid=? AND name=?", (uid, site))
        self.refresh_usage(uid, acc["username"])
        self.store.log(uid, "delete_site", site)
        return self.me(uid)

    # ---------- avisos y apagado automático ----------
    def tick(self):
        now = int(time.time())
        for acc in self.store.q("SELECT * FROM accounts WHERE powered=1 AND off_at>0 AND off_at<=?", (now,)):
            self.power_off(acc["uid"], "auto_off", f"plan={self.effective_plan(acc, now)}")
        for acc in self.store.q("SELECT * FROM accounts WHERE powered=1 AND off_at>0 AND warned=0"):
            p = PLANS[self.effective_plan(acc, now)]
            if p["warn_hours"] and acc["off_at"] - now <= p["warn_hours"] * HOUR:
                self.store.run("UPDATE accounts SET warned=1 WHERE uid=?", (acc["uid"],))
                self.store.log(acc["uid"], "warning", f"se apagará el {iso(acc['off_at'])}")
        for acc in self.store.q("SELECT * FROM accounts WHERE plan!='free' AND plan_until>0 AND plan_until<=?", (now,)):
            self.store.run("UPDATE accounts SET plan='free', plan_until=0 WHERE uid=?", (acc["uid"],))
            self.store.log(acc["uid"], "plan_expired", acc["plan"])
            self.apply_branding(acc["uid"])
            if acc["powered"] and not acc["off_at"]:
                self.store.run("UPDATE accounts SET off_at=? WHERE uid=?", (now + PLANS["free"]["hours"] * HOUR, acc["uid"]))
        self.lift_expired_bans(now)
        self.poll_domains(now)
        self.purge_abandoned(now)
        self.reconcile_flags()

    def purge_abandoned(self, now):
        for acc in self.store.q("SELECT * FROM accounts WHERE powered=0 AND banned=0"):
            plan = self.effective_plan(acc, now)
            at = self.purge_time(acc, plan, now)
            if not at:
                continue
            if now >= at:
                for s in self.store.q("SELECT name FROM sites WHERE uid=?", (acc["uid"],)):
                    self.drop_site_domains(acc["uid"], s["name"])
                    shutil.rmtree(os.path.join(self.cfg.sites_dir, acc["username"], s["name"]), ignore_errors=True)
                    self.store.run("DELETE FROM sites WHERE uid=? AND name=?", (acc["uid"], s["name"]))
                self.refresh_usage(acc["uid"], acc["username"])
                self.store.log(acc["uid"], "sites_purged", f"apagado más de {PURGE_DAYS} días (plan Free)")
            elif at - now <= (PURGE_DAYS - PURGE_WARN_DAYS) * DAY and not acc["purge_warned"]:
                self.store.run("UPDATE accounts SET purge_warned=1 WHERE uid=?", (acc["uid"],))
                self.store.log(acc["uid"], "purge_warning", f"tus sitios se borrarán el {iso(at)} si no lo enciendes")

    def migrate_branding(self):
        for acc in self.store.q("SELECT uid FROM accounts"):
            self.apply_branding(acc["uid"])

    def reconcile_flags(self):
        """El archivo-bandera debe existir exactamente para las cuentas encendidas y no vetadas."""
        want = {a["username"] for a in self.store.q("SELECT username FROM accounts WHERE powered=1 AND banned=0")}
        have = set(os.listdir(self.cfg.active_dir))
        for name in want - have:
            self.set_flag(name, True)
        for name in have - want:
            self.set_flag(name, False)
        self.sync_domains()


    # ---------- dominios propios (Cloudflare for SaaS) ----------
    DOMAIN_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")

    def clean_domain(self, raw):
        d = (raw or "").strip().lower().rstrip(".")
        if d.startswith(("http://", "https://")) or "/" in d or ":" in d or " " in d:
            raise HttpError(400, "bad_domain", reason="escribe solo el dominio, por ejemplo midominio.com")
        labels = d.split(".")
        if len(d) > 253 or len(labels) < 2 or not all(self.DOMAIN_LABEL.match(l) for l in labels):
            raise HttpError(400, "bad_domain", reason="dominio no válido")
        tld = labels[-1]
        if not (tld.isalpha() or tld.startswith("xn--")) or len(tld) < 2:
            raise HttpError(400, "bad_domain", reason="dominio no válido")
        base = self.cfg.base_domain
        if d == base or d.endswith("." + base) or d == self.cfg.cname_target:
            raise HttpError(400, "bad_domain", reason="los subdominios de budsin.dev ya están incluidos; usa un dominio propio")
        stem = "-".join(labels[:-1])
        flat = stem.lower().translate(moderation.LEET).replace("-", "")
        if moderation.bad_name(stem, subdomain=False) or moderation.bad_text(stem.replace("-", " ")) or any(b in flat for b in moderation.BRANDS):
            raise HttpError(400, "bad_domain", reason="nombre no permitido")
        return d

    def cf(self, method, path, body=None):
        if not self.cfg.domains_ready():
            raise HttpError(503, "domains_not_configured")
        try:
            status, data = http_json(f"{self.cfg.cf_api}/zones/{self.cfg.cf_zone}{path}", body,
                                     {"Authorization": f"Bearer {self.cfg.cf_token}"}, method, timeout=20)
        except (urllib.error.URLError, OSError, ValueError):
            raise HttpError(502, "cloudflare_error")
        if status >= 400 or not data.get("success", status < 300):
            msg = " ".join(str(e.get("message", "")) for e in (data.get("errors") or []))[:200]
            return status, {"error": msg or "error"}
        return status, data.get("result") or {}

    @staticmethod
    def domain_state(result):
        ssl = result.get("ssl") or {}
        records, errors = [], []
        ov = result.get("ownership_verification") or {}
        if ov.get("name") and ov.get("value"):
            records.append({"type": str(ov.get("type", "txt")).upper(), "name": ov["name"], "value": ov["value"], "purpose": "ownership"})
        for r in ssl.get("validation_records") or []:
            if r.get("txt_name") and r.get("txt_value"):
                records.append({"type": "TXT", "name": r["txt_name"], "value": r["txt_value"], "purpose": "ssl"})
        for e in (result.get("verification_errors") or []) + [x.get("message") for x in ssl.get("validation_errors") or [] if isinstance(x, dict)]:
            if e:
                errors.append(str(e)[:200])
        if result.get("status") == "active" and ssl.get("status") == "active":
            status = "active"
        elif result.get("status") in ("blocked", "moved", "deleted"):
            status = "error"
        else:
            status = "pending"
        return status, {"records": records, "errors": errors[:3], "cf": result.get("status"), "ssl": ssl.get("status")}

    def domain_limit(self, acc):
        return PLANS[self.effective_plan(acc)]["domains"]

    def domain_view(self, row):
        info = json.loads(row["info"] or "{}")
        return {"domain": row["domain"], "site": row["site"], "status": row["status"], "created": iso(row["created"]),
                "cname": {"type": "CNAME", "name": row["domain"], "value": self.cfg.cname_target},
                "records": info.get("records", []), "errors": info.get("errors", [])}

    def list_domains(self, uid):
        acc = self.need_account(uid)
        rows = self.store.q("SELECT * FROM domains WHERE uid=? ORDER BY id", (uid,))
        return {"domains": [self.domain_view(r) for r in rows], "limit": self.domain_limit(acc), "used": len(rows),
                "available": self.cfg.domains_ready(), "cnameTarget": self.cfg.cname_target}

    def add_domain(self, uid, domain, site):
        acc = self.need_account(uid)
        if acc["banned"]:
            raise HttpError(403, "banned")
        limit = self.domain_limit(acc)
        if not limit:
            raise HttpError(403, "plan_required")
        if not self.cfg.domains_ready():
            raise HttpError(503, "domains_not_configured")
        d = self.clean_domain(domain)
        site = (site or "").strip().lower()
        if not self.store.one("SELECT 1 FROM sites WHERE uid=? AND name=?", (uid, site)):
            raise HttpError(404, "no_site")
        with self._domain_lock:
            if self.store.one("SELECT 1 FROM domains WHERE domain=?", (d,)):
                raise HttpError(409, "domain_taken")
            if self.store.one("SELECT COUNT(*) c FROM domains WHERE uid=?", (uid,))["c"] >= limit:
                raise HttpError(409, "too_many_domains", max=limit)
            status, res = self.cf("POST", "/custom_hostnames", {
                "hostname": d, "ssl": {"method": "http", "type": "dv", "settings": {"min_tls_version": "1.2"}}})
            if status >= 400:
                taken = any(w in res.get("error", "").lower() for w in ("already", "duplicate", "exists"))
                raise HttpError(409 if taken else 502, "domain_taken" if taken else "cloudflare_error", reason=None if taken else res.get("error"))
            state, info = self.domain_state(res)
            now = int(time.time())
            self.store.run("INSERT INTO domains (uid,site,domain,status,cf_id,info,created,checked) VALUES (?,?,?,?,?,?,?,?)",
                           (uid, site, d, state, res.get("id"), json.dumps(info), now, now))
        self.store.log(uid, "domain_add", f"{d} → {site}")
        self.sync_domains()
        return self.list_domains(uid)

    def refresh_domain(self, row):
        status, res = self.cf("GET", f"/custom_hostnames/{row['cf_id']}")
        now = int(time.time())
        if status == 404:
            state, info = "error", {"records": [], "errors": ["not_found"]}
        elif status >= 400:
            self.store.run("UPDATE domains SET checked=? WHERE id=?", (now, row["id"]))
            return
        else:
            state, info = self.domain_state(res)
        self.store.run("UPDATE domains SET status=?, info=?, checked=? WHERE id=?", (state, json.dumps(info), now, row["id"]))
        if state == "active" and row["status"] != "active":
            self.store.log(row["uid"], "domain_active", row["domain"])

    def check_domain(self, uid, domain):
        row = self.store.one("SELECT * FROM domains WHERE uid=? AND domain=?", (uid, (domain or "").lower()))
        if not row:
            raise HttpError(404, "no_domain")
        self.refresh_domain(row)
        self.sync_domains()
        return self.list_domains(uid)

    def remove_domain(self, uid, domain, actor=None):
        row = self.store.one("SELECT * FROM domains WHERE domain=? AND (uid=? OR ?)", ((domain or "").lower(), uid, 1 if actor else 0))
        if not row:
            raise HttpError(404, "no_domain")
        if row["cf_id"] and self.cfg.domains_ready():
            status, res = self.cf("DELETE", f"/custom_hostnames/{row['cf_id']}")
            if status >= 400 and status != 404:
                raise HttpError(502, "cloudflare_error", reason=res.get("error"))
        self.store.run("DELETE FROM domains WHERE id=?", (row["id"],))
        self.store.log(row["uid"], "domain_removed", row["domain"] + (f" por {actor}" if actor else ""))
        self.sync_domains()
        return self.list_domains(row["uid"]) if not actor else {"ok": True}

    def drop_site_domains(self, uid, site):
        for row in self.store.q("SELECT domain FROM domains WHERE uid=? AND site=?", (uid, site)):
            with contextlib.suppress(HttpError):
                self.remove_domain(uid, row["domain"])

    def sync_domains(self):
        """Enlaces y archivos-bandera de los dominios: solo los verificados, de cuentas encendidas, no vetadas y dentro del límite de su plan."""
        want = {}
        used = {}
        for row in self.store.q("SELECT * FROM domains ORDER BY id"):
            if row["status"] != "active":
                continue
            acc = self.account(row["uid"])
            if not acc or not acc["powered"] or acc["banned"]:
                continue
            used[row["uid"]] = used.get(row["uid"], 0) + 1
            if used[row["uid"]] > self.domain_limit(acc):
                continue
            want[row["domain"]] = os.path.join(self.cfg.sites_dir, acc["username"], row["site"])
        for d, target in want.items():
            link = os.path.join(self.cfg.domains_dir, d)
            if not (os.path.islink(link) and os.readlink(link) == target):
                with contextlib.suppress(FileNotFoundError):
                    os.remove(link)
                os.symlink(target, link)
            flag = os.path.join(self.cfg.active_domains_dir, d)
            if not os.path.exists(flag):
                with open(flag, "w") as fh:
                    fh.write("1")
                os.chmod(flag, 0o644)
        for folder in (self.cfg.domains_dir, self.cfg.active_domains_dir):
            for name in os.listdir(folder):
                if name not in want:
                    with contextlib.suppress(OSError):
                        os.remove(os.path.join(folder, name))

    def poll_domains(self, now):
        for row in self.store.q("SELECT * FROM domains WHERE status!='active' AND checked<=? ", (now - 60,)):
            if now - row["created"] > DOMAIN_PENDING_DAYS * DAY:
                with contextlib.suppress(HttpError):
                    self.remove_domain(row["uid"], row["domain"])
                    self.store.log(row["uid"], "domain_expired", row["domain"])
                continue
            with contextlib.suppress(HttpError):
                self.refresh_domain(row)

    def admin_domains(self):
        return {"domains": [dict(self.domain_view(r), email=(self.account(r["uid"]) or {"email": ""})["email"]) for r in self.store.q("SELECT * FROM domains ORDER BY id DESC")]}

    # ---------- administración ----------
    def admin_accounts(self):
        now = int(time.time())
        out = []
        for a in self.store.q("SELECT * FROM accounts ORDER BY created DESC"):
            plan = self.effective_plan(a, now)
            out.append({
                "uid": a["uid"], "email": a["email"], "username": a["username"], "plan": plan,
                "planUntil": iso(a["plan_until"]) if a["plan_until"] else None,
                "state": "on" if a["powered"] else "off", "offAt": iso(a["off_at"]) if a["powered"] and a["off_at"] else None,
                "used": a["used_bytes"], "limit": PLANS[plan]["gb"] * GB, "banned": bool(a["banned"]),
                "banKind": a["ban_kind"] if a["banned"] else "", "banUntil": iso(a["ban_until"]) if a["banned"] and a["ban_until"] else None,
                "banReason": a["ban_reason"] if a["banned"] else "", "strikes": a["strikes"],
                "created": iso(a["created"]),
                "sites": [s["name"] for s in self.store.q("SELECT name FROM sites WHERE uid=?", (a["uid"],))],
            })
        return {"accounts": out, "totalUsed": sum(x["used"] for x in out), "totalCap": self.cfg.total_cap}

    def admin_history(self, uid):
        return {"history": [{"ts": iso(h["ts"]), "event": h["event"], "detail": h["detail"]}
                            for h in self.store.q("SELECT * FROM history WHERE uid=? ORDER BY id DESC LIMIT 500", (uid,))]}

    def admin_set_plan(self, uid, plan, days, actor):
        if plan not in PLANS:
            raise HttpError(400, "bad_plan")
        self.need_account(uid)
        until = int(time.time()) + int(days) * DAY if plan != "free" else 0
        self.store.run("UPDATE accounts SET plan=?, plan_until=? WHERE uid=?", (plan, until, uid))
        self.store.log(uid, "admin_plan", f"{plan} {days}d por {actor}")
        self.apply_branding(uid)
        return {"ok": True}

    def admin_ban(self, uid, kind, days=0, reason="", actor=""):
        """kind: none (levantar), soft (suspensión sin fecha), temp (hasta una fecha, se levanta sola) o perm."""
        acc = self.need_account(uid)
        if kind is True:
            kind = "perm"
        if kind in (False, None, "", "none"):
            self.store.run("UPDATE accounts SET banned=0, ban_kind='', ban_until=0, ban_reason='' WHERE uid=?", (uid,))
            self.store.log(uid, "unbanned", f"por {actor}")
            return {"ok": True}
        if kind not in BAN_KINDS:
            raise HttpError(400, "bad_ban_kind")
        until = 0
        if kind == "temp":
            try:
                days = int(days)
            except (TypeError, ValueError):
                days = 0
            if not 1 <= days <= 365:
                raise HttpError(400, "bad_days")
            until = int(time.time()) + days * DAY
        reason = (reason or "").strip()[:300]
        self.store.run("UPDATE accounts SET banned=1, ban_kind=?, ban_until=?, ban_reason=? WHERE uid=?", (kind, until, reason, uid))
        detail = f"{kind}" + (f" hasta {iso(until)}" if until else "") + (f" · {reason}" if reason else "") + f" · por {actor}"
        self.store.log(uid, "ban_" + kind, detail)
        self.power_off(uid, "banned", f"por {actor}")
        return {"ok": True}

    def admin_strike(self, uid, delta, reason, actor):
        """Suma (delta>0) o quita (delta<0) strikes. Al llegar a MAX_STRIKES se veta la cuenta para siempre."""
        acc = self.need_account(uid)
        try:
            delta = int(delta)
        except (TypeError, ValueError):
            raise HttpError(400, "bad_delta")
        if delta == 0 or abs(delta) > MAX_STRIKES:
            raise HttpError(400, "bad_delta")
        reason = (reason or "").strip()[:300]
        total = max(0, min(99, acc["strikes"] + delta))
        self.store.run("UPDATE accounts SET strikes=? WHERE uid=?", (total, uid))
        self.store.run("INSERT INTO strikes (uid,ts,delta,total,reason,actor) VALUES (?,?,?,?,?,?)", (uid, int(time.time()), delta, total, reason, actor))
        self.store.log(uid, "strike" if delta > 0 else "strike_removed", f"{delta:+d} → {total}/{MAX_STRIKES}" + (f" · {reason}" if reason else "") + f" · por {actor}")
        auto = False
        if delta > 0 and total >= MAX_STRIKES and not (acc["banned"] and acc["ban_kind"] == "perm"):
            self.admin_ban(uid, "perm", 0, f"{MAX_STRIKES} strikes" + (f": {reason}" if reason else ""), "automático")
            auto = True
        return {"ok": True, "strikes": total, "autoBanned": auto}

    def admin_strikes(self, uid):
        return {"strikes": [{"ts": iso(r["ts"]), "delta": r["delta"], "total": r["total"], "reason": r["reason"], "actor": r["actor"]}
                            for r in self.store.q("SELECT * FROM strikes WHERE uid=? ORDER BY id DESC LIMIT 200", (uid,))]}

    def lift_expired_bans(self, now):
        for a in self.store.q("SELECT uid FROM accounts WHERE banned=1 AND ban_kind='temp' AND ban_until>0 AND ban_until<=?", (now,)):
            self.store.run("UPDATE accounts SET banned=0, ban_kind='', ban_until=0, ban_reason='' WHERE uid=?", (a["uid"],))
            self.store.log(a["uid"], "ban_expired", "el veto temporal terminó")

    # ---------- denuncias ----------
    def report(self, url, reason, contact):
        url, reason, contact = (url or "")[:300], (reason or "")[:1000], (contact or "")[:200]
        if not url or not reason:
            raise HttpError(400, "missing_fields")
        self.store.run("INSERT INTO reports (ts,url,reason,contact) VALUES (?,?,?,?)", (int(time.time()), url, reason, contact))
        return {"ok": True}

    # ---------- PayPal ----------
    def paypal_token(self):
        token, exp = self._pp_token
        if token and exp > time.time() + 30:
            return token
        basic = base64.b64encode(f"{self.cfg.paypal_client}:{self.cfg.paypal_secret}".encode()).decode()
        status, data = http_json(f"{self.cfg.paypal_api}/v1/oauth2/token", b"grant_type=client_credentials",
                                 {"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"}, "POST")
        if status != 200 or "access_token" not in data:
            raise HttpError(502, "paypal_auth_failed")
        self._pp_token = (data["access_token"], time.time() + int(data.get("expires_in", 300)))
        return self._pp_token[0]

    def pp(self, method, path, body=None):
        return http_json(f"{self.cfg.paypal_api}{path}", body, {"Authorization": f"Bearer {self.paypal_token()}"}, method)

    def plan_for_paypal_id(self, plan_id):
        for name, pid in self.cfg.paypal_plans.items():
            if pid and pid == plan_id:
                return name
        return None

    def refresh_sub(self, sub_id, expect_uid=None):
        if not sub_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", sub_id):
            raise HttpError(400, "bad_subscription_id")
        status, sub = self.pp("GET", f"/v1/billing/subscriptions/{sub_id}")
        if status == 404:
            raise HttpError(404, "subscription_not_found")
        if status != 200:
            raise HttpError(502, "paypal_error")
        plan = self.plan_for_paypal_id(sub.get("plan_id"))
        if not plan:
            raise HttpError(400, "wrong_plan")
        owner = sub.get("custom_id") or ""
        if expect_uid is not None and owner != expect_uid:
            raise HttpError(403, "not_your_subscription")
        known = self.store.one("SELECT * FROM subs WHERE sub_id=?", (sub_id,))
        uid = known["uid"] if known else owner
        if not uid or not self.account(uid):
            raise HttpError(400, "no_owner")
        if known and expect_uid is not None and known["uid"] != expect_uid:
            raise HttpError(403, "not_your_subscription")
        state = sub.get("status", "")
        paid_until = known["paid_until"] if known else 0
        if state == "ACTIVE":
            info = sub.get("billing_info") or {}
            nxt = parse_time(info.get("next_billing_time"))
            last = parse_time((info.get("last_payment") or {}).get("time"))
            base = nxt or (last + FALLBACK_PERIOD if last else int(time.time()) + FALLBACK_PERIOD)
            paid_until = max(paid_until, base + GRACE_SECONDS)
        now = int(time.time())
        self.store.run(
            "INSERT INTO subs (sub_id,uid,plan,status,paid_until,last_checked,created) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(sub_id) DO UPDATE SET status=excluded.status, paid_until=excluded.paid_until, last_checked=excluded.last_checked",
            (sub_id, uid, plan, state, paid_until, now, now),
        )
        self.apply_subs(uid, newest=sub_id if state == "ACTIVE" else None)
        return self.store.one("SELECT * FROM subs WHERE sub_id=?", (sub_id,))

    def apply_subs(self, uid, newest=None):
        """Deja en la cuenta el plan de pago más alto vigente y cancela el cobro de suscripciones anteriores."""
        now = int(time.time())
        subs = [s for s in self.store.q("SELECT * FROM subs WHERE uid=?", (uid,)) if s["paid_until"] > now]
        acc = self.account(uid)
        if subs:
            best = max(subs, key=lambda s: (PLAN_ORDER.index(s["plan"]), s["paid_until"]))
            if (acc["plan"], acc["plan_until"]) != (best["plan"], best["paid_until"]):
                self.store.run("UPDATE accounts SET plan=?, plan_until=? WHERE uid=?", (best["plan"], best["paid_until"], uid))
                self.store.log(uid, "plan_set", f"{best['plan']} hasta {iso(best['paid_until'])}")
                self.apply_branding(uid)
                if acc["powered"] and not acc["off_at"] and best["plan"] != "ultra":
                    pass
                elif acc["powered"] and best["plan"] == "ultra":
                    self.store.run("UPDATE accounts SET off_at=0, warned=0 WHERE uid=?", (uid,))
        if newest:
            for s in self.store.q("SELECT * FROM subs WHERE uid=? AND status='ACTIVE' AND sub_id!=?", (uid, newest)):
                st, _ = self.pp("POST", f"/v1/billing/subscriptions/{s['sub_id']}/cancel", {"reason": "Cambio de plan en Budsin Hosting"})
                if st in (200, 204, 422):
                    self.store.run("UPDATE subs SET status='CANCELLED' WHERE sub_id=?", (s["sub_id"],))
                    self.store.log(uid, "sub_replaced", s["plan"])

    def subscribe(self, uid, sub_id):
        row = self.refresh_sub(sub_id, expect_uid=uid)
        out = self.me(uid)
        out["subscriptionStatus"] = row["status"]
        return out

    def cancel_subscription(self, uid):
        active = [s for s in self.store.q("SELECT * FROM subs WHERE uid=? AND status='ACTIVE'", (uid,))]
        if not active:
            raise HttpError(404, "no_active_subscription")
        for s in active:
            st, _ = self.pp("POST", f"/v1/billing/subscriptions/{s['sub_id']}/cancel", {"reason": "Cancelado por el usuario"})
            if st not in (200, 204, 422):
                raise HttpError(502, "paypal_cancel_failed")
            self.refresh_sub(s["sub_id"])
            self.store.log(uid, "sub_cancelled", s["plan"])
        return self.me(uid)

    def webhook(self, headers, raw_body):
        if not self.cfg.paypal_webhook:
            raise HttpError(503, "webhook_not_configured")
        try:
            event = json.loads(raw_body)
        except ValueError:
            raise HttpError(400, "bad_json")
        need = ["paypal-auth-algo", "paypal-cert-url", "paypal-transmission-id", "paypal-transmission-sig", "paypal-transmission-time"]
        if any(not headers.get(h) for h in need):
            raise HttpError(400, "missing_signature_headers")
        status, res = self.pp("POST", "/v1/notifications/verify-webhook-signature", {
            "auth_algo": headers["paypal-auth-algo"], "cert_url": headers["paypal-cert-url"],
            "transmission_id": headers["paypal-transmission-id"], "transmission_sig": headers["paypal-transmission-sig"],
            "transmission_time": headers["paypal-transmission-time"], "webhook_id": self.cfg.paypal_webhook,
            "webhook_event": event})
        if status != 200 or res.get("verification_status") != "SUCCESS":
            raise HttpError(400, "bad_signature")
        kind = event.get("event_type", "")
        obj = event.get("resource") or {}
        sub_id = obj.get("billing_agreement_id") if kind.startswith("PAYMENT.") else obj.get("id")
        if sub_id and (kind.startswith("BILLING.SUBSCRIPTION.") or kind == "PAYMENT.SALE.COMPLETED"):
            try:
                self.refresh_sub(sub_id)
            except HttpError as err:
                if err.status >= 500:
                    raise
        return {"ok": True}

    # ---------- límites ----------
    def rate_ok(self, ip, bucket="api"):
        limit = {"api": self.cfg.rate_limit, "upload": self.cfg.upload_rate, "files": self.cfg.files_rate}[bucket]
        now = time.time()
        with self._lock:
            store = self._buckets[bucket]
            hits = [t for t in store.get(ip, []) if now - t < 60]
            hits.append(now)
            store[ip] = hits
            if len(store) > 5000:
                for k in [k for k, v in store.items() if not v or now - v[-1] >= 60]:
                    del store[k]
        return len(hits) <= limit


def html_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def plans_payload(svc):
    return {"plans": [
        {"id": pid, "name": PLANS[pid]["name"], "gb": PLANS[pid]["gb"], "hours": PLANS[pid]["hours"],
         "price": PLANS[pid]["price"], "sites": PLANS[pid]["sites"], "warnHours": PLANS[pid]["warn_hours"],
         "domains": PLANS[pid]["domains"], "fileMb": PLANS[pid]["file_mb"], "uploadMb": svc.upload_limit(pid) // (1024 * 1024), "badge": PLANS[pid]["badge"]}
        for pid in PLAN_ORDER],
        "paypal": {"clientId": svc.cfg.paypal_client, "plans": svc.cfg.paypal_plans, "ready": svc.cfg.paypal_ready(),
                   "sandbox": "sandbox" in svc.cfg.paypal_api},
        "maxUploadMb": round(svc.cfg.max_upload / 1024 / 1024),
        "purgeDays": PURGE_DAYS, "purgeWarnDays": PURGE_WARN_DAYS,
        "baseDomain": svc.cfg.base_domain, "domainsReady": svc.cfg.domains_ready(), "cnameTarget": svc.cfg.cname_target}


class CountingReader:
    """Envuelve rfile para saber cuántos bytes del cuerpo se leyeron (y cerrar la conexión si sobran)."""

    def __init__(self, raw):
        self.raw, self.n = raw, 0

    def read(self, size=-1):
        data = self.raw.read(size)
        self.n += len(data)
        return data

    def __getattr__(self, name):  # readline, close, etc.: el resto va directo al lector real
        return getattr(self.raw, name)


def make_handler(svc):
    class Handler(BaseHTTPRequestHandler):
        server_version = "BudsinHosting"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

        def cors(self):
            origin = self.headers.get("Origin", "")
            if origin in svc.cfg.origins:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
                self.send_header("Access-Control-Max-Age", "600")

        def reply(self, status, payload):
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("CDN-Cache-Control", "no-store")
            self.send_header("Cloudflare-CDN-Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            if self.close_connection:
                self.send_header("Connection", "close")
            self.cors()
            self.end_headers()
            self.wfile.write(raw)

        def client_ip(self):
            return (self.headers.get("CF-Connecting-IP") or self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                    or self.client_address[0])

        def read_body(self, limit=64 * 1024):
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise HttpError(400, "bad_length")
            if length > limit:
                raise HttpError(413, "too_large")
            return self.rfile.read(length) if length else b""

        def read_json(self):
            try:
                return json.loads(self.read_body() or b"{}")
            except ValueError:
                raise HttpError(400, "bad_json")

        def route(self, method):
            parsed = self.path.split("?", 1)
            path = parsed[0]
            query = dict(urllib.parse.parse_qsl(parsed[1], keep_blank_values=True)) if len(parsed) > 1 else {}
            if not path.startswith(PREFIX):
                raise HttpError(404, "not_found")
            sub = path[len(PREFIX):].rstrip("/") or "/"
            auth = self.headers.get("Authorization")
            if method == "GET" and sub == "/health":
                return {"ok": True, "auth": svc.cfg.auth_ready(), "paypal": svc.cfg.paypal_ready(),
                        "webhook": bool(svc.cfg.paypal_webhook), "env": "sandbox" if "sandbox" in svc.cfg.paypal_api else "live"}
            if not svc.rate_ok(self.client_ip()):
                raise HttpError(429, "rate_limited")
            if method == "GET" and sub == "/plans":
                return plans_payload(svc)
            if method == "GET" and sub == "/check-name":
                name = query.get("username", "").lower()
                reason = svc.check_username(name)
                return {"ok": reason is None, "reason": reason}
            if method == "POST" and sub == "/report":
                body = self.read_json()
                return svc.report(body.get("url"), body.get("reason"), body.get("contact"))
            if method == "POST" and sub == "/webhook":
                return svc.webhook({k.lower(): v for k, v in self.headers.items()}, self.read_body())
            if not svc.cfg.auth_ready():
                raise HttpError(503, "not_configured")
            if sub.startswith("/admin"):
                _uid, email = svc.require_admin(auth)
                return self.admin(method, sub[len("/admin"):] or "/", query, email)
            uid, email, _ = svc.verify_user(auth)
            if method == "GET" and sub == "/me":
                acc = svc.account(uid)
                return svc.me(uid) if acc else {"registered": False, "email": email}
            if method == "POST" and sub == "/register":
                return svc.register(uid, email, self.read_json().get("username"))
            if method == "POST" and sub == "/power":
                on = bool(self.read_json().get("on"))
                return svc.power_on(uid) if on else svc.power_off(uid, "power_off", "manual")
            if method == "POST" and sub.startswith("/sites/") and sub.endswith("/upload"):
                if not svc.rate_ok(self.client_ip(), "upload"):
                    raise HttpError(429, "rate_limited")
                site = sub[len("/sites/"):-len("/upload")]
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    raise HttpError(400, "bad_length")
                return svc.upload(uid, site, self.rfile, length)
            m = re.fullmatch(r"/sites/([a-z0-9-]{1,39})/(files|file|mkdir|move|create)", sub)
            if m:   # gestor de archivos
                if not svc.rate_ok(self.client_ip(), "files"):
                    raise HttpError(429, "rate_limited")
                site, op = m.groups()
                rel = query.get("path", "")
                if op == "files" and method == "GET":
                    return svc.list_files(uid, site, rel)
                if op == "file" and method == "GET":
                    return svc.read_file(uid, site, rel)
                if op == "file" and method == "PUT":
                    try:
                        length = int(self.headers.get("Content-Length", "0"))
                    except ValueError:
                        raise HttpError(400, "bad_length")
                    return svc.write_file(uid, site, rel, self.rfile, length)
                if op == "file" and method == "DELETE":
                    return svc.delete_path(uid, site, rel)
                if op == "mkdir" and method == "POST":
                    return svc.make_dir(uid, site, rel)
                if op == "move" and method == "POST":
                    body = self.read_json()
                    return svc.move_path(uid, site, body.get("from"), body.get("to"))
                if op == "create" and method == "POST":
                    return svc.create_site(uid, site)
                raise HttpError(404, "not_found")
            if method == "DELETE" and re.fullmatch(r"/sites/[a-z0-9-]{1,39}", sub):
                return svc.delete_site(uid, sub[len("/sites/"):])
            if not svc.cfg.paypal_ready() and sub in ("/subscribe", "/cancel"):
                raise HttpError(503, "paypal_not_configured")
            if method == "POST" and sub == "/subscribe":
                return svc.subscribe(uid, str(self.read_json().get("subscriptionId", "")))
            if method == "POST" and sub == "/cancel":
                return svc.cancel_subscription(uid)
            if sub == "/domains" and method == "GET":
                return svc.list_domains(uid)
            if sub == "/domains" and method == "POST":
                body = self.read_json()
                return svc.add_domain(uid, str(body.get("domain", "")), str(body.get("site", "")))
            m = re.fullmatch(r"/domains/([a-z0-9.-]{1,253})(/check)?", sub)
            if m:
                if m.group(2) and method == "POST":
                    return svc.check_domain(uid, m.group(1))
                if not m.group(2) and method == "DELETE":
                    return svc.remove_domain(uid, m.group(1))
            raise HttpError(404, "not_found")

        def admin(self, method, sub, query, actor):
            if method == "GET" and sub == "/accounts":
                return svc.admin_accounts()
            if method == "GET" and sub == "/history":
                return svc.admin_history(query.get("uid", ""))
            if method == "GET" and sub == "/strikes":
                return svc.admin_strikes(query.get("uid", ""))
            if method == "GET" and sub == "/domains":
                return svc.admin_domains()
            if method == "POST" and sub == "/domains/remove":
                return svc.remove_domain("", str(self.read_json().get("domain", "")), actor)
            if method == "GET" and sub == "/reports":
                return {"reports": [dict(r) for r in svc.store.q("SELECT * FROM reports ORDER BY id DESC LIMIT 200")]}
            m = re.fullmatch(r"/reports/(\d+)", sub)
            if method == "POST" and m:
                svc.store.run("UPDATE reports SET status=? WHERE id=?", (str(self.read_json().get("status", "closed"))[:20], int(m.group(1))))
                return {"ok": True}
            m = re.fullmatch(r"/account/([A-Za-z0-9_-]{1,128})/(power|plan|ban|strike)", sub)
            if method == "POST" and m:
                uid, action = m.group(1), m.group(2)
                body = self.read_json()
                if action == "power":
                    if body.get("on"):
                        acc = svc.need_account(uid)
                        if acc["powered"]:
                            svc.power_off(uid, "forced_off", f"reinicio por {actor}")
                        return svc.power_on(uid, forced=True, actor=actor)
                    return svc.power_off(uid, "forced_off", f"por {actor}")
                if action == "plan":
                    return svc.admin_set_plan(uid, str(body.get("plan")), int(body.get("days", 30)), actor)
                if action == "strike":
                    return svc.admin_strike(uid, body.get("delta", 1), str(body.get("reason", "")), actor)
                kind = body.get("kind")
                if kind is None:  # compatibilidad con el formato antiguo {banned: true|false}
                    kind = "perm" if body.get("banned") else "none"
                return svc.admin_ban(uid, kind, body.get("days", 0), str(body.get("reason", "")), actor)
            raise HttpError(404, "not_found")

        def handle_any(self, method):
            self.rfile = CountingReader(self.rfile) if not isinstance(self.rfile, CountingReader) else self.rfile
            self.rfile.n = 0
            try:
                self.reply(200, self.route(method))
            except HttpError as err:
                self.finish_body()
                self.reply(err.status, {"error": err.code, **err.extra})
            except Exception as err:  # no filtrar detalles al cliente
                sys.stderr.write("error: %r\n" % (err,))
                self.finish_body()
                self.reply(500, {"error": "internal"})

        def finish_body(self):
            """Si el cliente envió un cuerpo que no leímos, hay que cerrar: esos bytes ensuciarían la siguiente petición."""
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length > self.rfile.n:
                self.close_connection = True

        def do_GET(self):
            self.handle_any("GET")

        def do_POST(self):
            self.handle_any("POST")

        def do_DELETE(self):
            self.handle_any("DELETE")

        def do_PUT(self):
            self.handle_any("PUT")

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.cors()
            self.end_headers()

    return Handler


def scheduler(svc):
    while True:
        try:
            svc.tick()
        except Exception as err:
            sys.stderr.write("tick error: %r\n" % (err,))
        time.sleep(30)


def main():
    cfg = Config()
    svc = Service(cfg)
    svc.tick()
    svc.migrate_branding()
    threading.Thread(target=scheduler, args=(svc,), daemon=True).start()
    srv = ThreadingHTTPServer((cfg.host, cfg.port), make_handler(svc))
    sys.stderr.write("budsin-hosting en %s:%d (auth=%s, paypal=%s)\n" % (cfg.host, cfg.port, cfg.auth_ready(), cfg.paypal_ready()))
    srv.serve_forever()


if __name__ == "__main__":
    main()
