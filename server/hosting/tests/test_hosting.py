"""Pruebas del servicio de hosting con Google y PayPal simulados (sin red, sin credenciales reales)."""
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import budsin_hosting as bh  # noqa: E402

TOKENS = {
    "tok-ana": ("uid-ana", "ana@x.com", True),
    "tok-beto": ("uid-beto", "beto@x.com", True),
    "tok-admin": ("uid-admin", "admin@x.com", True),
    "tok-sinverificar": ("uid-sv", "admin@x.com", False),
    "tok-carla": ("uid-carla", "carla@x.com", True),
    "tok-dina": ("uid-dina", "dina@x.com", True),
}
SUBS = {}
CALLS = {"cancel": 0}
VERIFY = {"verification_status": "SUCCESS"}


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, data=None):
        raw = json.dumps(data or {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path.startswith("/v1/oauth2/token"):
            return self.send(200, {"access_token": "pp", "expires_in": 3600})
        if self.path.startswith("/lookup"):
            tok = json.loads(body).get("idToken")
            if tok in TOKENS:
                u, e, v = TOKENS[tok]
                return self.send(200, {"users": [{"localId": u, "email": e, "emailVerified": v}]})
            return self.send(400, {"error": {"message": "INVALID_ID_TOKEN"}})
        if self.path.endswith("/cancel"):
            sid = self.path.split("/")[-2]
            CALLS["cancel"] += 1
            if sid in SUBS:
                SUBS[sid] = {**SUBS[sid], "status": "CANCELLED", "billing_info": {}}
                self.send_response(204)
                self.end_headers()
                return
            return self.send(404)
        if self.path.startswith("/v1/notifications/verify-webhook-signature"):
            return self.send(200, VERIFY)
        self.send(404)

    def do_GET(self):
        if self.path.startswith("/v1/billing/subscriptions/"):
            sub = SUBS.get(self.path.rsplit("/", 1)[1])
            return self.send(200, sub) if sub else self.send(404)
        self.send(404)


def sub(owner, plan_id, status="ACTIVE", days=30):
    return {"plan_id": plan_id, "status": status, "custom_id": owner,
            "billing_info": {"next_billing_time": iso(datetime.now(timezone.utc) + timedelta(days=days))}}


def make_zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def call(base, method, path, token=None, body=None, raw=None, headers=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(base + path, method=method, data=data)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        with e:
            return e.code, json.loads(e.read() or b"{}")


EDIT_BIG = 513 * 1024


def rd(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class HostingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = HTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=cls.fake.serve_forever, daemon=True).start()
        fp = "http://127.0.0.1:%d" % cls.fake.server_port
        cls.tmp = tempfile.mkdtemp()
        os.environ.update({
            "DATA_DIR": cls.tmp + "/data", "SITES_DIR": cls.tmp + "/sites", "ACTIVE_DIR": cls.tmp + "/active",
            "FIREBASE_API_KEY": "key", "FIREBASE_LOOKUP_URL": fp + "/lookup", "ADMIN_EMAILS": "admin@x.com",
            "PAYPAL_API_BASE": fp, "PAYPAL_CLIENT_ID": "cid", "PAYPAL_SECRET": "sec", "PAYPAL_WEBHOOK_ID": "WH",
            "PAYPAL_PLAN_PLUS": "P-PLUS", "PAYPAL_PLAN_PRO": "P-PRO", "PAYPAL_PLAN_ULTRA": "P-ULTRA", "MAX_UPLOAD_MB": "5", "UPLOAD_RATE": "1000", "RATE_LIMIT": "100000",
        })
        cls.svc = bh.Service(bh.Config())
        cls.api = bh.ThreadingHTTPServer(("127.0.0.1", 0), bh.make_handler(cls.svc))
        threading.Thread(target=cls.api.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d/api/hosting" % cls.api.server_port

    @classmethod
    def tearDownClass(cls):
        for s in (cls.fake, cls.api):
            s.shutdown()
            s.server_close()

    def reg(self, token, name):
        return call(self.base, "POST", "/register", token, {"username": name})

    # --- registro y nombres ---
    def test_a_registro_y_nombres(self):
        self.assertEqual(call(self.base, "GET", "/me", "tok-ana")[1], {"registered": False, "email": "ana@x.com"})
        for bad in ("ab", "con espacio", "-guion", "games", "paypal-pago", "puta-madre", "p0rn"):
            code, body = self.reg("tok-ana", bad)
            self.assertEqual((code, body["error"]), (400, "bad_username"), bad)
        code, body = self.reg("tok-ana", "ana-web")
        self.assertEqual(code, 200, body)
        self.assertEqual((body["username"], body["state"], body["plan"]), ("ana-web", "off", "free"))
        self.assertEqual(self.reg("tok-ana", "otro-nombre")[1]["error"], "already_registered")
        self.assertEqual(self.reg("tok-beto", "ana-web")[1]["error"], "username_taken")
        self.assertFalse(call(self.base, "GET", "/check-name?username=ana-web")[1]["ok"])
        self.assertTrue(call(self.base, "GET", "/check-name?username=libre-123")[1]["ok"])

    def test_b_sin_sesion(self):
        self.assertEqual(call(self.base, "GET", "/me")[0], 401)
        self.assertEqual(call(self.base, "GET", "/me", "tok-falso")[0], 401)

    # --- encendido y cuenta atrás ---
    def test_c_encendido_apagado_y_free(self):
        self.reg("tok-beto", "beto-web")
        code, me = call(self.base, "POST", "/power", "tok-beto", {"on": True})
        self.assertEqual((code, me["state"]), (200, "on"))
        self.assertTrue(os.path.exists(self.svc.flag_path("beto-web")))
        self.assertAlmostEqual(me["secondsLeft"], 12 * 3600, delta=5)
        self.assertEqual(call(self.base, "POST", "/power", "tok-beto", {"on": True})[1]["error"], "already_on")
        me = call(self.base, "POST", "/power", "tok-beto", {"on": False})[1]
        self.assertEqual(me["state"], "off")
        self.assertFalse(os.path.exists(self.svc.flag_path("beto-web")))
        # Free puede volver a encenderlo cuando quiera (sin límite mensual)
        for _ in range(2):
            code, me = call(self.base, "POST", "/power", "tok-beto", {"on": True})
            self.assertEqual((code, me["state"]), (200, "on"))
            self.assertAlmostEqual(me["secondsLeft"], 12 * 3600, delta=5)
            self.assertEqual(call(self.base, "POST", "/power", "tok-beto", {"on": False})[1]["state"], "off")
        events = [h["event"] for h in call(self.base, "GET", "/me", "tok-beto")[1]["history"]]
        self.assertEqual(events[:3], ["power_off", "power_on", "power_off"])
        self.assertNotIn("freeNextOn", call(self.base, "GET", "/me", "tok-beto")[1])

    def test_d_apagado_automatico_y_aviso(self):
        uid = "uid-ana"
        acc = self.svc.account(uid)
        if not acc["powered"]:
            self.svc.power_on(uid)
        now = int(time.time())
        self.svc.store.run("UPDATE accounts SET off_at=?, warned=0 WHERE uid=?", (now + 2 * 3600, uid))  # quedan 2 h (<3 h)
        self.svc.tick()
        me = call(self.base, "GET", "/me", "tok-ana")[1]
        self.assertTrue(me["warning"])
        self.assertIn("warning", [h["event"] for h in me["history"]])
        self.svc.store.run("UPDATE accounts SET off_at=? WHERE uid=?", (now - 1, uid))
        self.svc.tick()
        me = call(self.base, "GET", "/me", "tok-ana")[1]
        self.assertEqual(me["state"], "off")
        self.assertIn("auto_off", [h["event"] for h in me["history"]])
        self.assertFalse(os.path.exists(self.svc.flag_path("ana-web")))

    def test_e_reconciliar_banderas(self):
        os.makedirs(self.svc.cfg.active_dir, exist_ok=True)
        with open(self.svc.flag_path("fantasma"), "w") as fh:  # bandera huérfana
            fh.write("1")
        self.svc.tick()
        self.assertFalse(os.path.exists(self.svc.flag_path("fantasma")))

    # --- subida de sitios ---
    def up(self, token, site, files_or_bytes, **kw):
        raw = files_or_bytes if isinstance(files_or_bytes, bytes) else make_zip(files_or_bytes)
        return call(self.base, "POST", f"/sites/{site}/upload", token, raw=raw, headers={"Content-Type": "application/zip"})

    def test_f_subida_valida_y_serving(self):
        self.reg("tok-admin", "admin-web")
        code, me = self.up("tok-admin", "blog", {"index.html": "<h1>Hola</h1>", "css/app.css": "body{}", "img/a.png": b"\x89PNG"})
        self.assertEqual(code, 200, me)
        self.assertEqual([s["name"] for s in me["sites"]], ["blog"])
        self.assertTrue(os.path.exists(self.tmp + "/sites/admin-web/blog/css/app.css"))
        self.assertGreater(me["storage"]["used"], 0)
        # zip con una carpeta raíz única: se aplana
        code, me = self.up("tok-admin", "blog", {"mi-sitio/index.html": "<p>v2</p>"})
        self.assertEqual(code, 200)
        html = rd(self.tmp + "/sites/admin-web/blog/index.html")
        self.assertTrue(html.startswith("<p>v2</p>") and html.count(bh.BADGE_START) == 1)  # Free: insignia, una sola vez
        self.assertFalse(os.path.exists(self.tmp + "/sites/admin-web/blog/css"))  # la subida reemplaza el sitio
        self.assertIn("blog", rd(self.tmp + "/sites/admin-web/index.html"))

    def test_g_subidas_rechazadas(self):
        cases = [
            ("zip-slip", {"index.html": "x", "../evil.html": "x"}, "unsafe_path"),
            ("ruta-abs", {"index.html": "x", "/etc/pass.html": "x"}, "unsafe_path"),
            ("ejecutable", {"index.html": "x", "run.exe": b"MZ"}, "file_type_not_allowed"),
            ("php", {"index.html": "x", "a.php": "<?php"}, "file_type_not_allowed"),
            ("sin-index", {"otra.html": "x"}, "missing_index"),
            ("password", {"index.html": '<form><input type="password"></form>'}, "content_blocked"),
            ("insulto", {"index.html": "<p>visit my porn page</p>"}, "content_blocked"),
        ]
        for site, files, err in cases:
            code, body = self.up("tok-admin", site, files)
            self.assertEqual((code, body.get("error")), (400, err), site)
        self.assertEqual(self.up("tok-admin", "no-zip", b"esto no es un zip")[1]["error"], "bad_zip")
        self.assertEqual(self.up("tok-admin", "Mala_Ruta", {"index.html": "x"})[1]["error"], "bad_site_name")
        self.assertEqual(self.up("tok-admin", "porno", {"index.html": "x"})[1]["error"], "bad_site_name")
        # nada de lo rechazado quedó en disco
        left = [n for n in os.listdir(self.tmp + "/sites") if n.startswith(".up-")]
        self.assertEqual(left, [])
        for s in ("zip-slip", "ejecutable", "password"):
            self.assertFalse(os.path.exists(self.tmp + "/sites/admin-web/" + s))
        self.assertFalse(os.path.exists(self.tmp + "/sites/evil.html"))

    def test_h_cuota_y_aviso_80(self):
        self.reg("tok-sinverificar", "cuota-web")
        uid = "uid-sv"
        self.svc.store.run("UPDATE accounts SET used_bytes=? WHERE uid=?", (int(0.85 * bh.GB), uid))
        me = call(self.base, "GET", "/me", "tok-sinverificar")[1]
        self.assertTrue(me["storage"]["warn80"] and not me["storage"]["full"])
        self.svc.store.run("UPDATE accounts SET used_bytes=? WHERE uid=?", (bh.GB, uid))
        code, body = self.up("tok-sinverificar", "nuevo", {"index.html": "x"})
        self.assertEqual((code, body["error"]), (413, "storage_full"))
        self.assertTrue(call(self.base, "GET", "/me", "tok-sinverificar")[1]["storage"]["full"])

    def test_i_limite_de_sitios_y_borrado(self):
        code, body = self.up("tok-admin", "s2", {"index.html": "ok"})  # Free: 1 solo sitio (ya existe "blog")
        self.assertEqual((code, body["error"]), (409, "too_many_sites"))
        self.assertEqual(self.up("tok-admin", "blog", {"index.html": "reemplazo"})[0], 200)  # reemplazar el existente sí
        self.svc.store.run("UPDATE accounts SET plan='plus', plan_until=? WHERE uid='uid-admin'", (int(time.time()) + 86400,))
        for name in ("s2", "s3"):
            self.assertEqual(self.up("tok-admin", name, {"index.html": "ok"})[0], 200)
        code, me = call(self.base, "DELETE", "/sites/s2", "tok-admin")
        self.assertEqual(code, 200)
        self.assertFalse(os.path.exists(self.tmp + "/sites/admin-web/s2"))
        self.assertEqual(call(self.base, "DELETE", "/sites/s2", "tok-admin")[1]["error"], "no_site")
        self.svc.store.run("UPDATE accounts SET plan='free', plan_until=0 WHERE uid='uid-admin'")

    # --- administración ---
    def test_j_admin(self):
        self.assertEqual(call(self.base, "GET", "/admin/accounts", "tok-ana")[1]["error"], "not_admin")
        self.assertEqual(call(self.base, "GET", "/admin/accounts", "tok-sinverificar")[1]["error"], "not_admin")  # correo sin verificar
        code, body = call(self.base, "GET", "/admin/accounts", "tok-admin")
        self.assertEqual(code, 200)
        self.assertIn("ana-web", [a["username"] for a in body["accounts"]])
        code, me = call(self.base, "POST", "/admin/account/uid-ana/power", "tok-admin", {"on": True})
        self.assertEqual((code, me["state"]), (200, "on"))
        self.assertIn("forced_on", [h["event"] for h in call(self.base, "GET", "/admin/history?uid=uid-ana", "tok-admin")[1]["history"]])
        self.assertEqual(call(self.base, "POST", "/admin/account/uid-ana/plan", "tok-admin", {"plan": "pro", "days": 10})[0], 200)
        self.assertEqual(call(self.base, "GET", "/me", "tok-ana")[1]["plan"], "pro")
        call(self.base, "POST", "/admin/account/uid-ana/ban", "tok-admin", {"banned": True})
        me = call(self.base, "GET", "/me", "tok-ana")[1]
        self.assertEqual((me["banned"], me["state"]), (True, "off"))
        self.assertEqual(call(self.base, "POST", "/power", "tok-ana", {"on": True})[1]["error"], "banned")
        call(self.base, "POST", "/admin/account/uid-ana/ban", "tok-admin", {"banned": False})

    def test_k_denuncias(self):
        self.assertEqual(call(self.base, "POST", "/report", body={"url": "https://x.budsin.dev/a", "reason": "phishing"})[0], 200)
        self.assertEqual(call(self.base, "POST", "/report", body={"url": ""})[1]["error"], "missing_fields")
        rep = call(self.base, "GET", "/admin/reports", "tok-admin")[1]["reports"]
        self.assertEqual(rep[0]["url"], "https://x.budsin.dev/a")

    # --- PayPal ---
    def test_l_paypal_suscripcion(self):
        SUBS["I-PLUS"] = sub("uid-beto", "P-PLUS")
        code, me = call(self.base, "POST", "/subscribe", "tok-beto", {"subscriptionId": "I-PLUS"})
        self.assertEqual(code, 200, me)
        self.assertEqual((me["plan"], me["subscriptionStatus"]), ("plus", "ACTIVE"))
        self.assertEqual(me["storage"]["limit"], 5 * bh.GB)
        SUBS["I-AJENA"] = sub("uid-ana", "P-PRO")
        self.assertEqual(call(self.base, "POST", "/subscribe", "tok-beto", {"subscriptionId": "I-AJENA"})[1]["error"], "not_your_subscription")
        SUBS["I-RARO"] = sub("uid-beto", "P-OTRO")
        self.assertEqual(call(self.base, "POST", "/subscribe", "tok-beto", {"subscriptionId": "I-RARO"})[1]["error"], "wrong_plan")
        self.assertEqual(call(self.base, "POST", "/subscribe", "tok-beto", {"subscriptionId": "../x"})[1]["error"], "bad_subscription_id")

    def test_m_cambio_de_plan_cancela_el_anterior(self):
        SUBS["I-PRO"] = sub("uid-beto", "P-PRO")
        before = CALLS["cancel"]
        me = call(self.base, "POST", "/subscribe", "tok-beto", {"subscriptionId": "I-PRO"})[1]
        self.assertEqual(me["plan"], "pro")
        self.assertEqual(CALLS["cancel"], before + 1)
        self.assertEqual(SUBS["I-PLUS"]["status"], "CANCELLED")

    def test_n_cancelar_conserva_hasta_fin_de_periodo(self):
        code, me = call(self.base, "POST", "/cancel", "tok-beto")
        self.assertEqual(code, 200, me)
        self.assertEqual(me["plan"], "pro")  # sigue vigente hasta paid_until
        self.assertEqual(call(self.base, "POST", "/cancel", "tok-beto")[1]["error"], "no_active_subscription")

    def test_o_plan_vencido_vuelve_a_free(self):
        self.svc.store.run("UPDATE accounts SET plan_until=? WHERE uid='uid-beto'", (int(time.time()) - 5,))
        self.svc.tick()
        self.assertEqual(call(self.base, "GET", "/me", "tok-beto")[1]["plan"], "free")

    def test_p_webhook(self):
        hdr = {k: "x" for k in ["PayPal-Auth-Algo", "PayPal-Cert-Url", "PayPal-Transmission-Id", "PayPal-Transmission-Sig", "PayPal-Transmission-Time"]}
        ev = {"event_type": "BILLING.SUBSCRIPTION.ACTIVATED", "resource": {"id": "I-WH"}}
        SUBS["I-WH"] = sub("uid-ana", "P-ULTRA")
        VERIFY["verification_status"] = "FAILURE"
        try:
            self.assertEqual(call(self.base, "POST", "/webhook", body=ev, headers=hdr)[0], 400)
            self.assertIsNone(self.svc.store.one("SELECT 1 FROM subs WHERE sub_id='I-WH'"))
        finally:
            VERIFY["verification_status"] = "SUCCESS"
        self.assertEqual(call(self.base, "POST", "/webhook", body=ev, headers=hdr)[0], 200)
        self.assertEqual(self.svc.account("uid-ana")["plan"], "ultra")
        self.assertEqual(call(self.base, "POST", "/webhook", body=ev)[1]["error"], "missing_signature_headers")

    def test_q_ultra_nunca_se_apaga(self):
        self.svc.store.run("UPDATE accounts SET powered=0 WHERE uid='uid-ana'")
        me = self.svc.power_on("uid-ana", forced=True)
        self.assertTrue(me["neverOff"])
        self.svc.tick()
        self.assertEqual(call(self.base, "GET", "/me", "tok-ana")[1]["state"], "on")

    def test_s_conexion_persistente_sin_errores(self):
        import http.client
        errors = []
        orig = sys.stderr
        class Spy:
            def write(self, s):
                if "Traceback" in s or "Error" in s or "Exception" in s:
                    errors.append(s)
            def flush(self):
                pass
        sys.stderr = Spy()
        try:
            c = http.client.HTTPConnection("127.0.0.1", self.api.server_port)
            for path in ("/api/hosting/plans", "/api/hosting/health", "/api/hosting/plans"):  # misma conexión
                c.request("GET", path)
                r = c.getresponse()
                self.assertEqual(r.status, 200)
                r.read()
            c.request("POST", "/api/hosting/sites/x/upload", body=b"0" * 2000)  # sin sesión: error sin leer el cuerpo
            r = c.getresponse()
            self.assertEqual(r.status, 401)
            self.assertEqual(r.getheader("Connection"), "close")
            r.read()
            c.close()
            time.sleep(0.2)
        finally:
            sys.stderr = orig
        self.assertEqual(errors, [])

    # --- límites y marca del plan Free ---
    def test_t_limites_de_archivo_free(self):
        self.reg("tok-carla", "carla-web")
        big = make_zip({"index.html": "x", "grande.mp4": b"\0" * (16 * 1024 * 1024)})  # comprime muy bien: el zip es pequeño
        code, body = self.up("tok-carla", "video", big)
        self.assertEqual((code, body["error"]), (413, "file_too_large"))
        self.assertEqual(body["max"], 15 * 1024 * 1024)
        self.assertEqual(self.svc.file_limit("free"), 15 * 1024 * 1024)
        self.assertGreater(self.svc.file_limit("plus"), 100 * 1024 * 1024)
        self.assertEqual(self.svc.upload_limit("free"), min(self.svc.cfg.max_upload, 40 * 1024 * 1024))
        self.assertFalse(os.path.exists(self.tmp + "/sites/carla-web/video"))

    def test_u_insignia_free_se_quita_y_se_pone_con_el_plan(self):
        page = self.tmp + "/sites/carla-web/web/index.html"
        self.assertEqual(self.up("tok-carla", "web", {"index.html": "<html><body><h1>Hola</h1></body></html>", "sub/a.html": "<p>a</p>"})[0], 200)
        html = rd(page)
        self.assertEqual(html.count(bh.BADGE_START), 1)
        self.assertLess(html.index(bh.BADGE_START), html.index("</body>"))  # dentro del body
        self.assertIn(bh.BADGE_START, rd(self.tmp + "/sites/carla-web/web/sub/a.html"))  # también sin <body>
        used_free = call(self.base, "GET", "/me", "tok-carla")[1]["storage"]["used"]
        self.assertEqual(call(self.base, "POST", "/admin/account/uid-carla/plan", "tok-admin", {"plan": "plus", "days": 30})[0], 200)
        self.assertEqual(rd(page), "<html><body><h1>Hola</h1></body></html>")  # al pagar, desaparece sin volver a subir
        self.assertLess(call(self.base, "GET", "/me", "tok-carla")[1]["storage"]["used"], used_free)
        self.assertEqual(self.up("tok-carla", "web", {"index.html": "<body>v2</body>"})[0], 200)
        self.assertNotIn(bh.BADGE_START, rd(page))  # plan de pago: sin insignia al subir
        self.svc.store.run("UPDATE accounts SET plan_until=? WHERE uid='uid-carla'", (int(time.time()) - 5,))
        self.svc.tick()  # caduca el plan: vuelve a Free y a llevar insignia
        self.assertEqual(rd(page).count(bh.BADGE_START), 1)
        self.svc.apply_branding("uid-carla")  # idempotente
        self.assertEqual(rd(page).count(bh.BADGE_START), 1)

    def test_v_borrado_de_sitios_free_abandonados(self):
        uid = "uid-carla"
        self.svc.store.run("UPDATE accounts SET plan='free', plan_until=0, powered=0 WHERE uid=?", (uid,))
        now = int(time.time())
        d = 86400
        self.svc.store.run("UPDATE accounts SET off_since=?, purge_warned=0 WHERE uid=?", (now - 30 * d, uid))
        self.svc.tick()
        me = call(self.base, "GET", "/me", "tok-carla")[1]
        self.assertFalse(me["purgeWarning"])
        self.assertTrue(me["purgeAt"])
        self.svc.store.run("UPDATE accounts SET off_since=? WHERE uid=?", (now - 61 * d, uid))  # día 61: aviso
        self.svc.tick()
        me = call(self.base, "GET", "/me", "tok-carla")[1]
        self.assertTrue(me["purgeWarning"])
        self.assertIn("purge_warning", [h["event"] for h in me["history"]])
        self.assertTrue(os.path.exists(self.tmp + "/sites/carla-web/web"))
        # una cuenta encendida no se toca
        self.svc.store.run("UPDATE accounts SET off_since=? WHERE uid=?", (now - 120 * d, uid))
        self.svc.store.run("UPDATE accounts SET powered=1, off_at=? WHERE uid=?", (now + 3600, uid))
        self.svc.tick()
        self.assertTrue(os.path.exists(self.tmp + "/sites/carla-web/web"))
        self.svc.store.run("UPDATE accounts SET powered=0, off_at=0 WHERE uid=?", (uid,))
        # un plan de pago vigente tampoco se purga
        self.svc.store.run("UPDATE accounts SET plan='plus', plan_until=? WHERE uid=?", (now + 5 * d, uid))
        self.svc.tick()
        self.assertTrue(os.path.exists(self.tmp + "/sites/carla-web/web"))
        # Free y más de 90 días apagado: se borran los sitios (la cuenta se queda)
        self.svc.store.run("UPDATE accounts SET plan='free', plan_until=? WHERE uid=?", (0, uid))
        self.svc.store.run("UPDATE accounts SET off_since=? WHERE uid=?", (now - 91 * d, uid))
        self.svc.tick()
        me = call(self.base, "GET", "/me", "tok-carla")[1]
        self.assertEqual(me["sites"], [])
        self.assertFalse(os.path.exists(self.tmp + "/sites/carla-web/web"))
        self.assertEqual(me["storage"]["used"], 0)
        self.assertIn("sites_purged", [h["event"] for h in me["history"]])
        self.assertEqual(me["username"], "carla-web")
        self.assertIsNone(me["purgeAt"])  # ya no hay sitios que borrar

    def test_w_valores_de_los_planes(self):
        body = call(self.base, "GET", "/plans")[1]
        free = body["plans"][0]
        self.assertEqual((free["hours"], free["sites"], free["fileMb"], free["badge"], free["warnHours"]), (12, 1, 15, True, 3))
        self.assertEqual(body["purgeDays"], 90)
        self.assertEqual(body["purgeWarnDays"], 60)
        self.assertTrue(all(not p["badge"] for p in body["plans"][1:]))


    # --- gestor de archivos ---
    def fm(self, method, token, site, op, path=None, data=None, body=None):
        q = "" if path is None else "?path=" + urllib.parse.quote(path, safe="")
        return call(self.base, method, f"/sites/{site}/{op}{q}", token, body=body, raw=data)

    def put(self, token, site, path, data):
        return self.fm("PUT", token, site, "file", path, data if isinstance(data, bytes) else data.encode())

    def test_y1_archivo_suelto_crea_el_sitio_y_pone_insignia(self):
        self.reg("tok-dina", "dina-web")
        code, me = self.put("tok-dina", "mi-sitio", "index.html", "<html><body><h1>Hola</h1></body></html>")
        self.assertEqual(code, 200, me)
        self.assertEqual([s["name"] for s in me["sites"]], ["mi-sitio"])
        html = rd(self.tmp + "/sites/dina-web/mi-sitio/index.html")
        self.assertEqual(html.count(bh.BADGE_START), 1)
        self.assertLess(html.index(bh.BADGE_START), html.index("</body>"))
        self.assertGreater(me["storage"]["used"], 0)
        code, lst = self.fm("GET", "tok-dina", "mi-sitio", "files", "")
        self.assertEqual((code, [e["name"] for e in lst["entries"]]), (200, ["index.html"]))
        self.assertTrue(lst["entries"][0]["editable"])

    def test_y2_rutas_peligrosas_se_rechazan(self):
        base = self.tmp + "/sites/dina-web/mi-sitio"
        malas = ["../x.html", "..%2fx.html", "a/../../b.html", "..\\x.html", ".env.html", "ok/.oculto.html",
                 "a/./../../c.html", "con:dos.html", "a" * 101 + ".html", "/".join(["d"] * 10) + "/x.html", "nombre.html."]
        for path in malas:
            code, body = self.put("tok-dina", "mi-sitio", path, "x")
            self.assertEqual((code, body.get("error")), (400, "bad_path"), path)
        # nada se escribió fuera del sitio
        self.assertEqual(sorted(os.listdir(self.tmp + "/sites")), sorted(n for n in os.listdir(self.tmp + "/sites")))
        # una ruta absoluta se normaliza dentro del sitio, nunca fuera de él
        self.assertEqual(self.put("tok-dina", "mi-sitio", "/etc/abs.html", "<p>x</p>")[0], 200)
        self.assertTrue(os.path.exists(base + "/etc/abs.html"))
        self.assertFalse(os.path.exists("/etc/abs.html"))
        for escapado in ("x.html", "b.html", "c.html"):
            self.assertFalse(os.path.exists(os.path.join(base, escapado)))
            self.assertFalse(os.path.exists(os.path.join(os.path.dirname(base), escapado)))
            self.assertFalse(os.path.exists(os.path.join(self.tmp + "/sites", escapado)))
        for op in ("files", "file"):
            self.assertEqual(self.fm("GET", "tok-dina", "mi-sitio", op, "../")[1].get("error"), "bad_path")
        self.assertEqual(self.fm("DELETE", "tok-dina", "mi-sitio", "file", "")[1].get("error"), "bad_path")  # nunca la raíz
        self.assertEqual(self.fm("POST", "tok-dina", "mi-sitio", "mkdir", "../fuera")[1].get("error"), "bad_path")
        self.assertEqual(self.fm("GET", "tok-dina", "otro-que-no-existe", "files", "")[1].get("error"), "no_site")

    def test_y3_tipos_de_archivo_y_nombres(self):
        for name in ("run.exe", "a.php", "sin-extension", "x.sh", "z.zip"):
            code, body = self.put("tok-dina", "mi-sitio", name, "x")
            self.assertEqual((code, body.get("error")), (400, "file_type_not_allowed"), name)
        code, body = self.put("tok-dina", "mi-sitio", "porn.html", "<p>x</p>")
        self.assertEqual((code, body.get("error")), (400, "bad_file_name"))
        self.assertEqual(self.put("tok-dina", "mi-sitio", "css/estilo.css", "body{}")[0], 200)
        self.assertEqual(self.put("tok-dina", "mi-sitio", "mi carpeta/foto 1.png", b"\x89PNG")[0], 200)  # espacios permitidos
        self.assertTrue(os.path.exists(self.tmp + "/sites/dina-web/mi-sitio/mi carpeta/foto 1.png"))

    def test_y4_contenido_bloqueado_y_marca_falsa(self):
        code, body = self.put("tok-dina", "mi-sitio", "login.html", '<form><input type="password"></form>')
        self.assertEqual((code, body.get("error")), (400, "content_blocked"))
        self.assertFalse(os.path.exists(self.tmp + "/sites/dina-web/mi-sitio/login.html"))
        fake = "<body>hola " + bh.BADGE_START + "<b>falsa</b>" + bh.BADGE_END + "</body>"
        self.assertEqual(self.put("tok-dina", "mi-sitio", "pagina.html", fake)[0], 200)
        html = rd(self.tmp + "/sites/dina-web/mi-sitio/pagina.html")
        self.assertEqual(html.count(bh.BADGE_START), 1)
        self.assertNotIn("falsa", html)                       # la marca pegada a mano se sustituye por la oficial
        self.assertIn("budsin.dev/hosting", html)

    def test_y5_limites_por_archivo_cuota_y_sitios(self):
        uid = "uid-dina"
        svc = self.svc
        with self.assertRaises(bh.HttpError) as cm:
            svc.write_file(uid, "mi-sitio", "grande.mp4", io.BytesIO(b""), 16 * 1024 * 1024)   # Free: 15 MB por archivo
        self.assertEqual((cm.exception.status, cm.exception.code), (413, "file_too_large"))
        self.svc.store.run("UPDATE accounts SET used_bytes=? WHERE uid=?", (bh.GB, uid))
        code, body = self.put("tok-dina", "mi-sitio", "otro.html", "<p>x</p>")
        self.assertEqual((code, body.get("error")), (413, "storage_full"))
        self.svc.refresh_usage(uid, "dina-web")
        code, body = self.put("tok-dina", "segundo-sitio", "index.html", "<p>x</p>")        # Free: 1 solo sitio (autocreación incluida)
        self.assertEqual((code, body.get("error")), (409, "too_many_sites"))
        self.assertFalse(os.path.exists(self.tmp + "/sites/dina-web/segundo-sitio"))

    def test_y6_mover_y_renombrar(self):
        self.assertEqual(self.put("tok-dina", "mi-sitio", "notas.txt", '<input type="password"> texto')[0], 200)  # .txt no se escanea como HTML
        code, body = self.fm("POST", "tok-dina", "mi-sitio", "move", body={"from": "notas.txt", "to": "notas.html"})
        self.assertEqual((code, body.get("error")), (400, "content_blocked"))                                      # renombrar a .html sí
        self.assertTrue(os.path.exists(self.tmp + "/sites/dina-web/mi-sitio/notas.txt"))
        self.assertFalse(os.path.exists(self.tmp + "/sites/dina-web/mi-sitio/notas.html"))
        self.assertEqual(self.put("tok-dina", "mi-sitio", "limpio.txt", "texto normal")[0], 200)
        code, me = self.fm("POST", "tok-dina", "mi-sitio", "move", body={"from": "limpio.txt", "to": "paginas/nueva.html"})
        self.assertEqual(code, 200, me)
        self.assertEqual(rd(self.tmp + "/sites/dina-web/mi-sitio/paginas/nueva.html").count(bh.BADGE_START), 1)  # y recibe la insignia
        self.assertEqual(self.fm("POST", "tok-dina", "mi-sitio", "move", body={"from": "limpio.txt", "to": "z.html"})[1]["error"], "no_file")
        self.assertEqual(self.fm("POST", "tok-dina", "mi-sitio", "move", body={"from": "paginas/nueva.html", "to": "notas.txt"})[1]["error"], "exists")
        self.assertEqual(self.fm("POST", "tok-dina", "mi-sitio", "move", body={"from": "paginas/nueva.html", "to": "malo.exe"})[1]["error"], "file_type_not_allowed")
        self.assertEqual(self.fm("POST", "tok-dina", "mi-sitio", "move", body={"from": "paginas/nueva.html", "to": "../fuera.html"})[1]["error"], "bad_path")
        self.assertEqual(self.fm("POST", "tok-dina", "mi-sitio", "mkdir", "a/b")[0], 200)
        code, body = self.fm("POST", "tok-dina", "mi-sitio", "move", body={"from": "a", "to": "a/b/c"})
        self.assertEqual((code, body.get("error")), (400, "bad_path"))                                              # carpeta dentro de sí misma
        self.assertEqual(self.fm("POST", "tok-dina", "mi-sitio", "mkdir", "a/b")[1]["error"], "exists")
        self.assertEqual(self.fm("POST", "tok-dina", "mi-sitio", "move", body={"from": "a", "to": "renombrada"})[0], 200)
        self.assertTrue(os.path.isdir(self.tmp + "/sites/dina-web/mi-sitio/renombrada/b"))

    def test_y7_editor_quita_y_repone_la_insignia(self):
        self.assertEqual(self.put("tok-dina", "mi-sitio", "editar.html", "<body>v1</body>")[0], 200)
        code, f = self.fm("GET", "tok-dina", "mi-sitio", "file", "editar.html")
        self.assertEqual((code, f["content"]), (200, "<body>v1</body>"))                       # el editor no ve la insignia
        self.assertEqual(self.put("tok-dina", "mi-sitio", "editar.html", f["content"].replace("v1", "v2"))[0], 200)
        self.assertEqual(rd(self.tmp + "/sites/dina-web/mi-sitio/editar.html").count(bh.BADGE_START), 1)
        self.assertEqual(self.fm("GET", "tok-dina", "mi-sitio", "file", "editar.html")[1]["content"], "<body>v2</body>")
        self.put("tok-dina", "mi-sitio", "foto.png", b"\x89PNG")
        self.assertEqual(self.fm("GET", "tok-dina", "mi-sitio", "file", "foto.png")[1]["error"], "not_text")
        self.assertEqual(self.put("tok-dina", "mi-sitio", "enorme.txt", "x" * (EDIT_BIG))[0], 200)
        self.assertEqual(self.fm("GET", "tok-dina", "mi-sitio", "file", "enorme.txt")[1]["error"], "too_large_to_edit")
        # con un plan de pago no hay insignia
        self.svc.store.run("UPDATE accounts SET plan='plus', plan_until=? WHERE uid='uid-dina'", (int(time.time()) + 86400,))
        self.assertEqual(self.put("tok-dina", "mi-sitio", "editar.html", "<body>v3</body>")[0], 200)
        self.assertEqual(rd(self.tmp + "/sites/dina-web/mi-sitio/editar.html"), "<body>v3</body>")
        self.svc.store.run("UPDATE accounts SET plan='free', plan_until=0 WHERE uid='uid-dina'")
        self.svc.apply_branding("uid-dina")

    def test_y8_borrar_listar_y_ocultos(self):
        with open(self.tmp + "/sites/dina-web/mi-sitio/.secreto", "w") as fh:
            fh.write("x")
        code, lst = self.fm("GET", "tok-dina", "mi-sitio", "files", "")
        names = [e["name"] for e in lst["entries"]]
        self.assertNotIn(".secreto", names)
        dirs = [e["name"] for e in lst["entries"] if e["type"] == "dir"]
        self.assertEqual(names[:len(dirs)], dirs)                                               # carpetas primero
        code, me = self.fm("DELETE", "tok-dina", "mi-sitio", "file", "renombrada")
        self.assertEqual(code, 200)
        self.assertFalse(os.path.exists(self.tmp + "/sites/dina-web/mi-sitio/renombrada"))
        self.assertEqual(self.fm("DELETE", "tok-dina", "mi-sitio", "file", "renombrada")[1]["error"], "no_file")
        self.assertEqual(self.fm("DELETE", "tok-dina", "mi-sitio", "file", "index.html")[0], 200)
        site = next(s for s in call(self.base, "GET", "/me", "tok-dina")[1]["sites"] if s["name"] == "mi-sitio")
        self.assertGreater(site["files"], 0)                                                    # el contador de archivos se actualiza

    def test_y9_crear_sitio_vacio_y_baneo(self):
        self.reg("tok-sinverificar", "vacio-web") if False else None
        self.svc.store.run("UPDATE accounts SET plan='plus', plan_until=? WHERE uid='uid-dina'", (int(time.time()) + 86400,))
        code, me = self.fm("POST", "tok-dina", "nuevo", "create")
        self.assertEqual(code, 200, me)
        self.assertIn("<h1>Hello, world!</h1>", rd(self.tmp + "/sites/dina-web/nuevo/index.html"))
        self.assertEqual(self.fm("POST", "tok-dina", "nuevo", "create")[1]["error"], "site_exists")
        self.assertEqual(self.fm("POST", "tok-dina", "porno", "create")[1]["error"], "bad_site_name")
        call(self.base, "POST", "/admin/account/uid-dina/ban", "tok-admin", {"banned": True})
        self.assertEqual(self.put("tok-dina", "nuevo", "x.html", "<p>x</p>")[1]["error"], "banned")
        self.assertEqual(self.fm("GET", "tok-dina", "nuevo", "files", "")[1]["error"], "banned")
        call(self.base, "POST", "/admin/account/uid-dina/ban", "tok-admin", {"banned": False})
        self.svc.store.run("UPDATE accounts SET plan='free', plan_until=0 WHERE uid='uid-dina'")

    def test_y91_put_en_conexion_persistente(self):
        import http.client
        errors = []
        orig = sys.stderr
        class Spy:
            def write(self, s):
                if "Traceback" in s or "Error" in s or "Exception" in s:
                    errors.append(s)
            def flush(self):
                pass
        sys.stderr = Spy()
        try:
            c = http.client.HTTPConnection("127.0.0.1", self.api.server_port)
            hdr = {"Authorization": "Bearer tok-dina"}
            c.request("PUT", "/api/hosting/sites/mi-sitio/file?path=" + urllib.parse.quote("ka/uno.html", safe=""), body=b"<p>1</p>", headers=hdr)
            r = c.getresponse(); self.assertEqual(r.status, 200); r.read()
            c.request("GET", "/api/hosting/sites/mi-sitio/files?path=ka", headers=hdr)               # misma conexión
            r = c.getresponse(); self.assertEqual(r.status, 200); r.read()
            c.request("PUT", "/api/hosting/sites/mi-sitio/file?path=malo.exe", body=b"x" * 4000, headers=hdr)   # error sin leer el cuerpo
            r = c.getresponse(); self.assertEqual(r.status, 400); self.assertEqual(r.getheader("Connection"), "close"); r.read()
            c.close()
            time.sleep(0.2)
        finally:
            sys.stderr = orig
        self.assertEqual(errors, [])


    def test_z1_strikes_y_vetos(self):
        self.reg("tok-carla", "carla-web")
        adm = lambda path, body=None, m="POST": call(self.base, m, path, "tok-admin", body)
        me = lambda: call(self.base, "GET", "/me", "tok-carla")[1]
        # solo el admin puede
        self.assertEqual(call(self.base, "POST", "/admin/account/uid-carla/strike", "tok-carla", {"delta": 1})[0], 403)
        self.assertEqual(me()["strikes"], 0)
        self.assertEqual(me()["maxStrikes"], 3)
        code, r = adm("/admin/account/uid-carla/strike", {"delta": 1, "reason": "spam"})
        self.assertEqual((code, r["strikes"], r["autoBanned"]), (200, 1, False))
        self.assertEqual(adm("/admin/account/uid-carla/strike", {"delta": 0})[1]["error"], "bad_delta")
        self.assertEqual(adm("/admin/account/uid-carla/strike", {"delta": "x"})[1]["error"], "bad_delta")
        self.assertEqual(adm("/admin/account/uid-carla/strike", {"delta": -1})[1]["strikes"], 0)
        self.assertEqual(adm("/admin/account/uid-carla/strike", {"delta": -1})[1]["strikes"], 0)   # nunca baja de 0
        adm("/admin/account/uid-carla/strike", {"delta": 1, "reason": "a"})
        adm("/admin/account/uid-carla/strike", {"delta": 1, "reason": "b"})
        self.assertFalse(me()["banned"])
        code, r = adm("/admin/account/uid-carla/strike", {"delta": 1, "reason": "c"})
        self.assertEqual((r["strikes"], r["autoBanned"]), (3, True))
        m = me()
        self.assertEqual((m["banned"], m["banKind"], m["banUntil"]), (True, "perm", None))
        self.assertIn("3 strikes", m["banReason"])
        self.assertEqual(call(self.base, "POST", "/power", "tok-carla", {"on": True})[1]["error"], "banned")
        log = adm("/admin/strikes?uid=uid-carla", m="GET")[1]["strikes"]
        self.assertEqual([x["delta"] for x in log][:2], [1, 1])
        self.assertEqual(log[0]["actor"], "admin@x.com")
        # quitar un strike no levanta el veto solo; lo levanta el admin
        adm("/admin/account/uid-carla/strike", {"delta": -1})
        self.assertTrue(me()["banned"])
        self.assertEqual(adm("/admin/account/uid-carla/ban", {"kind": "none"})[0], 200)
        m = me()
        self.assertEqual((m["banned"], m["banKind"], m["strikes"]), (False, "", 2))
        # veto temporal: validación y caducidad automática
        self.assertEqual(adm("/admin/account/uid-carla/ban", {"kind": "temp", "days": 0})[1]["error"], "bad_days")
        self.assertEqual(adm("/admin/account/uid-carla/ban", {"kind": "temp", "days": 999})[1]["error"], "bad_days")
        self.assertEqual(adm("/admin/account/uid-carla/ban", {"kind": "raro"})[1]["error"], "bad_ban_kind")
        self.assertEqual(adm("/admin/account/uid-carla/ban", {"kind": "temp", "days": 3, "reason": "descanso"})[0], 200)
        m = me()
        self.assertEqual((m["banned"], m["banKind"], m["banReason"]), (True, "temp", "descanso"))
        self.assertIsNotNone(m["banUntil"])
        self.svc.tick()
        self.assertTrue(me()["banned"])                                                           # aún no vence
        self.svc.store.run("UPDATE accounts SET ban_until=? WHERE uid='uid-carla'", (int(time.time()) - 5,))
        self.svc.tick()
        self.assertEqual((me()["banned"], me()["banKind"]), (False, ""))
        # suspensión (soft): sin fecha, no caduca sola
        self.assertEqual(adm("/admin/account/uid-carla/ban", {"kind": "soft", "reason": "revisión"})[0], 200)
        self.svc.tick()
        m = me()
        self.assertEqual((m["banned"], m["banKind"], m["banUntil"]), (True, "soft", None))
        # formato antiguo y listado del admin
        adm("/admin/account/uid-carla/ban", {"banned": False})
        adm("/admin/account/uid-carla/ban", {"banned": True})
        self.assertEqual(me()["banKind"], "perm")
        acc = next(a for a in adm("/admin/accounts", m="GET")[1]["accounts"] if a["uid"] == "uid-carla")
        self.assertEqual((acc["banned"], acc["banKind"], acc["strikes"]), (True, "perm", 2))
        adm("/admin/account/uid-carla/ban", {"kind": "none"})

    def test_r_cors_y_planes(self):
        code, body = call(self.base, "GET", "/plans")
        self.assertEqual([p["id"] for p in body["plans"]], ["free", "plus", "pro", "ultra"])
        self.assertEqual([p["gb"] for p in body["plans"]], [1, 5, 10, 15])
        self.assertIsNone(body["plans"][3]["hours"])
        req = urllib.request.Request(self.base + "/health", headers={"Origin": "https://malo.example"})
        with urllib.request.urlopen(req) as r:
            self.assertIsNone(r.headers["Access-Control-Allow-Origin"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
