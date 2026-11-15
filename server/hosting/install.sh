#!/usr/bin/env bash
# Instala Budsin Hosting en el VPS (Ubuntu + Caddy). Ejecutar como root desde esta carpeta:  bash install.sh
# Es idempotente. No toca los datos (cuentas, sitios) ni los secretos ya guardados.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Ejecuta como root"; exit 1; }
here=$(cd "$(dirname "$0")" && pwd)
caddyfile=/etc/caddy/Caddyfile
envfile=/etc/budsin-hosting.env

id budsin-hosting >/dev/null 2>&1 || useradd --system --home-dir /var/lib/budsin-hosting --shell /usr/sbin/nologin budsin-hosting
install -d -o budsin-hosting -g budsin-hosting -m 0750 /var/lib/budsin-hosting
install -d -m 0755 /var/www/hosting
install -d -o budsin-hosting -g budsin-hosting -m 0755 /var/www/hosting/sites /var/www/hosting/active
install -m 0644 "$here/off.html" /var/www/hosting/off.html
install -d -m 0755 /opt/budsin-hosting
install -m 0644 "$here/budsin_hosting.py" "$here/moderation.py" /opt/budsin-hosting/

if [ ! -f "$envfile" ]; then
  install -m 0640 -o root -g budsin-hosting "$here/env.example" "$envfile"
  echo "Creado $envfile (faltan los datos de PayPal: usa budsin-hosting-set)"
fi

cat > /usr/local/sbin/budsin-hosting-set <<'SH'
#!/usr/bin/env bash
# Guarda un valor en /etc/budsin-hosting.env y reinicia el servicio. El Secret se pide sin mostrarse.
#   budsin-hosting-set PAYPAL_SECRET        (silencioso)
#   budsin-hosting-set PAYPAL_PLAN_PLUS     (visible)
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Ejecuta con sudo"; exit 1; }
key=${1:-}; [[ "$key" =~ ^[A-Z_]+$ ]] || { echo "Uso: budsin-hosting-set CLAVE"; exit 1; }
if [ "$key" = PAYPAL_SECRET ]; then read -rsp "$key: " val; echo; else read -rp "$key: " val; fi
[ -n "$val" ] || { echo "Vacío, no se cambia nada"; exit 1; }
python3 - "$key" "$val" <<'PY'
import re, sys
p, k, v = "/etc/budsin-hosting.env", sys.argv[1], sys.argv[2]
t = open(p).read()
line = k + "=" + v
t = re.sub(r"^" + k + r"=.*$", lambda m: line, t, flags=re.M) if re.search(r"^" + k + "=", t, re.M) else t.rstrip("\n") + "\n" + line + "\n"
open(p, "w").write(t)
PY
chmod 0640 /etc/budsin-hosting.env
systemctl restart budsin-hosting
echo "$key guardado y servicio reiniciado."
SH
chmod 0755 /usr/local/sbin/budsin-hosting-set

install -m 0644 "$here/budsin-hosting.service" /etc/systemd/system/budsin-hosting.service
systemctl daemon-reload
systemctl enable budsin-hosting >/dev/null
systemctl restart budsin-hosting

# El webroot de budsin.dev se sincroniza con el repo: el backend (server/) no debe publicarse.
if [ -f /usr/local/sbin/budsin-dev-sync ] && ! grep -q -- "--exclude server" /usr/local/sbin/budsin-dev-sync; then
  sed -i 's|--exclude .git |--exclude .git --exclude server |' /usr/local/sbin/budsin-dev-sync
  rm -rf /var/www/budsin-dev/public/server
  echo "budsin-dev-sync ahora excluye server/"
fi

# Caddy: ruta de la API y URLs limpias en budsin.dev, y el comodín *.budsin.dev para los sitios de usuarios.
if grep -q "api/hosting" "$caddyfile" && grep -q "labels.2" "$caddyfile"; then
  echo "Caddy ya tiene Budsin Hosting"
else
  backup="$caddyfile.bak.$(date +%Y%m%d%H%M%S)"
  cp -a "$caddyfile" "$backup"
  # Los registros se crean antes con el dueño correcto: 'caddy validate' como root los crearía con dueño root.
  touch /var/log/caddy/hosting-access.log && chown caddy:caddy /var/log/caddy/hosting-access.log && chmod 600 /var/log/caddy/hosting-access.log
  python3 - "$caddyfile" "$here/caddy-wildcard.txt" <<'PY'
import sys
path, wild = sys.argv[1], open(sys.argv[2]).read().rstrip("\n")
t = open(path).read()
if "api/hosting" not in t:
    anchor = "\troot * /var/www/budsin-dev/public\n"
    assert t.count(anchor) == 1, "no encuentro el sitio budsin.dev en el Caddyfile"
    t = t.replace(anchor, anchor + """\thandle /api/hosting/* {
\t\treverse_proxy 127.0.0.1:3102 {
\t\t\tflush_interval -1
\t\t\ttransport http {
\t\t\t\tread_timeout 10m
\t\t\t\twrite_timeout 10m
\t\t\t}
\t\t}
\t}
\ttry_files {path} {path}.html {path}/index.html
""")
if "labels.2" not in t:
    t = t.rstrip("\n") + "\n" + wild + "\n"
open(path, "w").write(t)
PY
  if caddy validate --config "$caddyfile" --adapter caddyfile >/dev/null 2>&1; then
    systemctl reload caddy
    echo "Caddy actualizado (copia: $backup)"
  else
    cp -a "$backup" "$caddyfile"
    echo "Caddyfile no valida; restaurado desde $backup. Nada cambió en Caddy."
    exit 1
  fi
fi

echo "--- comprobación local"
# El servicio tarda unos segundos en escuchar (al arrancar revisa las cuentas): se reintenta hasta 30 s.
for _ in $(seq 1 30); do curl -sf -o /dev/null http://127.0.0.1:3102/api/hosting/health && break; sleep 1; done
curl -s http://127.0.0.1:3102/api/hosting/health; echo
curl -s -o /dev/null -w "budsin.dev /about (URL limpia) %{http_code}\n" -H "Host: budsin.dev" http://127.0.0.1:8080/about
curl -s -o /dev/null -w "budsin.dev /api/hosting/plans %{http_code}\n" -H "Host: budsin.dev" http://127.0.0.1:8080/api/hosting/plans
curl -s -o /dev/null -w "sitio inexistente (debe ser 503) %{http_code}\n" -H "Host: nadie.budsin.dev" http://127.0.0.1:8080/
for h in games.budsin.dev budsin.dev thiagift.budsin.dev; do curl -s -o /dev/null -w "$h %{http_code}\n" -H "Host: $h" http://127.0.0.1:8080/; done
systemctl is-active caddy budsin-hosting
