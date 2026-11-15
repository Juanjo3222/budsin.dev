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
FREE_COOLDOWN = 30 * DAY         # Free: un encendido cada 30 días
TOKEN_TTL = 300
MAX_FILES = 5000
MAX_FILE_BYTES = 200 * 1024 * 1024
SCAN_BYTES = 2 * 1024 * 1024

PLANS = {
    "free":  {"name": "Free",  "gb": 1,  "hours": 24,      "warn_hours": 12, "price": 0.00, "sites": 3},
    "plus":  {"name": "Plus",  "gb": 5,  "hours": 5 * 24,  "warn_hours": 24, "price": 1.99, "sites": 10},
    "pro":   {"name": "Pro",   "gb": 10, "hours": 14 * 24, "warn_hours": 24, "price": 3.99, "sites": 25},
    "ultra": {"name": "Ultra", "gb": 15, "hours": None,    "warn_hours": 0,  "price": 6.99, "sites": 50},
}
PLAN_ORDER = ["free", "plus", "pro", "ultra"]

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
        self.upload_rate = int(env("UPLOAD_RATE", "12"))    # subidas por minuto y por IP
        self.max_upload = int(float(env("MAX_UPLOAD_MB", "95")) * 1024 * 1024)
        self.total_cap = int(float(env("TOTAL_CAP_GB", "40")) * GB)
        self.paypal_api = env("PAYPAL_API_BASE", "https://api-m.sandbox.paypal.com").rstrip("/")
        self.paypal_client = env("PAYPAL_CLIENT_ID")
        self.paypal_secret = env("PAYPAL_SECRET")
        self.paypal_webhook = env("PAYPAL_WEBHOOK_ID")
        self.paypal_plans = {p: env("PAYPAL_PLAN_" + p.upper()) for p in ("plus", "pro", "ultra")}

    def auth_ready(self):
        return bool(self.firebase_key)

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


def dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            with contextlib.suppress(OSError):
                total += os.path.getsize(os.path.join(root, f))
    return total


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
                    used_bytes INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS sites (
                    uid TEXT NOT NULL, name TEXT NOT NULL, bytes INTEGER NOT NULL DEFAULT 0,
                    files INTEGER NOT NULL DEFAULT 0, updated INTEGER NOT NULL, PRIMARY KEY (uid, name));
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT NOT NULL, ts INTEGER NOT NULL,
                    event TEXT NOT NULL, detail TEXT);
                CREATE INDEX IF NOT EXISTS history_uid ON history(uid, id);
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
        for d in (cfg.sites_dir, cfg.active_dir):
            os.makedirs(d, mode=0o755, exist_ok=True)
        self._tokens = {}
        self._hits = {}
        self._uploads = {}
        self._pp_token = ("", 0)
        self._lock = threading.Lock()
        self._upload_lock = threading.Lock()

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
        if plan == "free" and not forced and acc["last_free_on"] and now - acc["last_free_on"] < FREE_COOLDOWN:
            raise HttpError(429, "free_cooldown", next_available=iso(acc["last_free_on"] + FREE_COOLDOWN))
        hours = PLANS[plan]["hours"]
        off_at = now + hours * HOUR if hours else 0
        self.store.run(
            "UPDATE accounts SET powered=1, powered_since=?, off_at=?, warned=0, last_free_on=CASE WHEN ?='free' AND ?=0 THEN ? ELSE last_free_on END WHERE uid=?",
            (now, off_at, plan, 1 if forced else 0, now, uid),
        )
        self.set_flag(acc["username"], True)
        self.store.log(uid, "power_on" if not forced else "forced_on", (actor or "") + f" plan={plan}")
        return self.me(uid)

    def power_off(self, uid, event="power_off", detail=""):
        acc = self.need_account(uid)
        if not acc["powered"] and not os.path.exists(self.flag_path(acc["username"])):
            return self.me(uid)
        self.store.run("UPDATE accounts SET powered=0, off_at=0, warned=0 WHERE uid=?", (uid,))
        self.set_flag(acc["username"], False)
        self.store.log(uid, event, detail)
        return self.me(uid)

    def me(self, uid):
        acc = self.need_account(uid)
        now = int(time.time())
        plan = self.effective_plan(acc, now)
        p = PLANS[plan]
        limit = p["gb"] * GB
        used = acc["used_bytes"]
        remaining = max(0, acc["off_at"] - now) if acc["powered"] and acc["off_at"] else None
        cooldown = None
        if plan == "free" and acc["last_free_on"] and now - acc["last_free_on"] < FREE_COOLDOWN and not acc["powered"]:
            cooldown = iso(acc["last_free_on"] + FREE_COOLDOWN)
        warn = bool(acc["powered"] and acc["off_at"] and p["warn_hours"] and acc["off_at"] - now <= p["warn_hours"] * HOUR)
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
            "banned": bool(acc["banned"]),
            "state": "on" if acc["powered"] else "off", "poweredSince": iso(acc["powered_since"]) if acc["powered"] else None,
            "offAt": iso(acc["off_at"]) if acc["powered"] and acc["off_at"] else None, "secondsLeft": remaining,
            "neverOff": bool(acc["powered"] and not acc["off_at"]),
            "warning": warn, "freeNextOn": cooldown,
            "storage": {"used": used, "limit": limit, "percent": round(100 * used / limit, 1) if limit else 0,
                        "warn80": used >= 0.8 * limit, "full": used >= limit},
            "maxSites": p["sites"], "sites": sites, "history": hist,
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
            b = dir_size(os.path.join(self.cfg.sites_dir, username, r["name"]))
            total += b
            self.store.run("UPDATE sites SET bytes=? WHERE uid=? AND name=?", (b, uid, r["name"]))
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
        if length > self.cfg.max_upload:
            raise HttpError(413, "upload_too_large", max=self.cfg.max_upload)
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
            nfiles, nbytes = self.extract_zip(zpath, extracted, acc, limit, site)
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

    def extract_zip(self, zpath, dest, acc, limit, site):
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
                if info.file_size > MAX_FILE_BYTES:
                    raise HttpError(413, "file_too_large", file=name)
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
            return len(plan_files), written

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

    def delete_site(self, uid, site):
        acc = self.need_account(uid)
        if not self.store.one("SELECT 1 FROM sites WHERE uid=? AND name=?", (uid, site)):
            raise HttpError(404, "no_site")
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
            if acc["powered"] and not acc["off_at"]:
                self.store.run("UPDATE accounts SET off_at=? WHERE uid=?", (now + PLANS["free"]["hours"] * HOUR, acc["uid"]))
        self.reconcile_flags()

    def reconcile_flags(self):
        """El archivo-bandera debe existir exactamente para las cuentas encendidas y no vetadas."""
        want = {a["username"] for a in self.store.q("SELECT username FROM accounts WHERE powered=1 AND banned=0")}
        have = set(os.listdir(self.cfg.active_dir))
        for name in want - have:
            self.set_flag(name, True)
        for name in have - want:
            self.set_flag(name, False)

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
        return {"ok": True}

    def admin_ban(self, uid, banned, actor):
        acc = self.need_account(uid)
        self.store.run("UPDATE accounts SET banned=? WHERE uid=?", (1 if banned else 0, uid))
        if banned:
            self.power_off(uid, "banned", f"por {actor}")
        else:
            self.store.log(uid, "unbanned", f"por {actor}")
        return {"ok": True}

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
        limit = self.cfg.rate_limit if bucket == "api" else self.cfg.upload_rate
        now = time.time()
        with self._lock:
            store = self._hits if bucket == "api" else self._uploads
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
         "price": PLANS[pid]["price"], "sites": PLANS[pid]["sites"], "warnHours": PLANS[pid]["warn_hours"]}
        for pid in PLAN_ORDER],
        "paypal": {"clientId": svc.cfg.paypal_client, "plans": svc.cfg.paypal_plans, "ready": svc.cfg.paypal_ready(),
                   "sandbox": "sandbox" in svc.cfg.paypal_api},
        "maxUploadMb": round(svc.cfg.max_upload / 1024 / 1024),
        "baseDomain": svc.cfg.base_domain}


class CountingReader:
    """Envuelve rfile para saber cuántos bytes del cuerpo se leyeron (y cerrar la conexión si sobran)."""

    def __init__(self, raw):
        self.raw, self.n = raw, 0

    def read(self, size=-1):
        data = self.raw.read(size)
        self.n += len(data)
        return data


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
                self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
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
            query = dict(p.split("=", 1) for p in (parsed[1].split("&") if len(parsed) > 1 else []) if "=" in p)
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
            if method == "DELETE" and sub.startswith("/sites/"):
                return svc.delete_site(uid, sub[len("/sites/"):])
            if not svc.cfg.paypal_ready() and sub in ("/subscribe", "/cancel"):
                raise HttpError(503, "paypal_not_configured")
            if method == "POST" and sub == "/subscribe":
                return svc.subscribe(uid, str(self.read_json().get("subscriptionId", "")))
            if method == "POST" and sub == "/cancel":
                return svc.cancel_subscription(uid)
            raise HttpError(404, "not_found")

        def admin(self, method, sub, query, actor):
            if method == "GET" and sub == "/accounts":
                return svc.admin_accounts()
            if method == "GET" and sub == "/history":
                return svc.admin_history(query.get("uid", ""))
            if method == "GET" and sub == "/reports":
                return {"reports": [dict(r) for r in svc.store.q("SELECT * FROM reports ORDER BY id DESC LIMIT 200")]}
            m = re.fullmatch(r"/reports/(\d+)", sub)
            if method == "POST" and m:
                svc.store.run("UPDATE reports SET status=? WHERE id=?", (str(self.read_json().get("status", "closed"))[:20], int(m.group(1))))
                return {"ok": True}
            m = re.fullmatch(r"/account/([A-Za-z0-9_-]{1,128})/(power|plan|ban)", sub)
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
                return svc.admin_ban(uid, bool(body.get("banned")), actor)
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
    threading.Thread(target=scheduler, args=(svc,), daemon=True).start()
    srv = ThreadingHTTPServer((cfg.host, cfg.port), make_handler(svc))
    sys.stderr.write("budsin-hosting en %s:%d (auth=%s, paypal=%s)\n" % (cfg.host, cfg.port, cfg.auth_ready(), cfg.paypal_ready()))
    srv.serve_forever()


if __name__ == "__main__":
    main()
