"""Pruebas del servicio de hosting con Google y PayPal simulados (sin red, sin credenciales reales)."""
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
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
