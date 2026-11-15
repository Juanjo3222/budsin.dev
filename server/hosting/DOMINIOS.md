# Dominios propios (planes de pago)

Cómo funciona: el usuario añade `midominio.com` en su panel y crea un `CNAME` hacia `custom.budsin.dev`.
El servicio registra el dominio como **Custom Hostname** en Cloudflare (Cloudflare for SaaS), que verifica el DNS y emite el HTTPS.
Cuando Cloudflare lo da por activo y la cuenta está encendida, el servicio crea un enlace en
`/var/www/hosting/domains/<dominio>` (apunta a la carpeta del sitio) y una bandera en `/var/www/hosting/active-domains/<dominio>`.
Caddy (`caddy-custom.txt`, bloque `http://:8080`) sirve solo los hosts con bandera; el resto recibe la página 503.

Límites: Plus 1 · Pro 3 · Ultra 10 (`PLANS[...]["domains"]`). Un dominio sin verificar se elimina a los 7 días.
Al apagar el servidor, vetar la cuenta o perder el plan, el dominio deja de servirse (se vuelve a servir al revertirlo).

## Puesta en marcha (una sola vez, en Cloudflare)

1. **Custom Hostnames**: zona `budsin.dev` → SSL/TLS → Custom Hostnames → activar. (Según la documentación de Cloudflare los
   primeros 100 hostnames son gratis; puede pedir un método de pago. Confírmalo en tu panel.)
2. **Origen de reserva**: crea un registro DNS `custom` (CNAME, con proxy naranja) hacia `<ID-DEL-TÚNEL>.cfargotunnel.com` y,
   en Custom Hostnames, define `custom.budsin.dev` como *Fallback Origin*.
3. **Túnel**: Zero Trust → Networks → Tunnels → tu túnel → Public Hostnames: la regla final (catch-all, la que hoy da 404) debe
   apuntar a `http://127.0.0.1:8080`, para que los dominios de los clientes lleguen a Caddy.
4. **Token de API**: Mi perfil → API Tokens → crear token personalizado con permiso *Zone → SSL and Certificates → Edit* solo para
   la zona `budsin.dev`. Copia también el *Zone ID* (resumen de la zona).
5. **En el VPS**: reinstala el servicio (`install.sh`, crea las carpetas y el bloque de Caddy) y guarda los datos:
   ```
   budsin-hosting-set CF_API_TOKEN   # se pide sin mostrarse
   budsin-hosting-set CF_ZONE_ID
   ```
   Hasta que existan token y zona, el panel muestra «Los dominios propios todavía no están disponibles».

## Prueba

Con un plan de pago y un sitio subido, añade un dominio tuyo en el panel, crea el CNAME y pulsa «Comprobar».
Si no pasa a «Activo» tras unos minutos, mira el estado del hostname en Cloudflare → Custom Hostnames.
