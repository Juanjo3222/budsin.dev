"""Filtros básicos de nombres y contenido. Detectan lo evidente; no sustituyen la revisión humana."""
import re

RESERVED = {
    "www", "mail", "api", "admin", "games", "budsin", "thiagift", "ftp", "smtp", "imap", "pop", "ns1", "ns2",
    "root", "support", "soporte", "help", "ayuda", "status", "blog", "hosting", "app", "cdn", "static", "assets",
    "login", "signin", "signup", "pay", "payment", "pagos", "billing", "dashboard", "panel", "docs", "dev",
    "test", "staging", "demo", "web", "webmail", "vpn", "git", "dns", "proxy", "server", "servidor", "cloud",
    "custom", "domains", "dominios",
}
# Marcas y entidades que se suelen suplantar: no pueden aparecer dentro de un nombre.
BRANDS = (
    "paypal", "google", "gmail", "facebook", "instagram", "whatsapp", "tiktok", "youtube", "microsoft", "outlook",
    "apple", "icloud", "amazon", "netflix", "spotify", "binance", "coinbase", "metamask", "mercadopago",
    "interbank", "scotiabank", "bbva", "yape", "sunat", "reniec", "banco", "bank",
)
# Se buscan como subcadena (son lo bastante largas para no dar falsos positivos).
STRONG_SUBSTRING = (
    "porn", "hentai", "xvideos", "xnxx", "nsfw", "onlyfans", "sexcam", "camgirl", "nazi", "hitler", "isis",
    "pedofil", "pedophil", "child-porn", "cheapxanax",
)
# Se buscan como palabra completa (para no marcar "computadora" por "puta").
WORDS = {
    "puta", "puto", "putas", "putos", "mierda", "pendejo", "pendeja", "cabron", "cabrona", "verga", "concha",
    "chingar", "chingada", "culo", "pene", "vagina", "sexo", "sexy", "nude", "nudes", "fuck", "fucking", "shit",
    "bitch", "cunt", "dick", "cock", "pussy", "slut", "whore", "rape", "violacion", "xxx", "porn", "porno", "nigger",
    "maricon", "marica", "gilipollas", "joder",
}
LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i"})


def _norm(text):
    return text.lower().translate(LEET)


def bad_name(name, subdomain=True):
    """Devuelve un motivo (texto) si el nombre no es admisible, o None.

    Con subdomain=True (nombres de cuenta, que forman parte del host) también se rechazan los nombres
    reservados y las marcas, para impedir la suplantación. Los nombres de sitio (rutas) solo pasan el filtro de lenguaje."""
    n = _norm(name)
    flat = n.replace("-", "")
    if subdomain:
        if name in RESERVED:
            return "nombre reservado"
        for brand in BRANDS:
            if brand in flat:
                return "no puede contener nombres de marcas o bancos"
    for bad in STRONG_SUBSTRING:
        if bad.replace("-", "") in flat:
            return "nombre no permitido"
    if any(tok in WORDS for tok in n.split("-")):
        return "nombre no permitido"
    return None


def bad_text(text):
    """Busca contenido evidentemente inapropiado en un texto. Devuelve un motivo o None."""
    n = _norm(text)
    words = set(re.findall(r"[a-záéíóúñ]+", n))
    hit = words & WORDS
    if hit:
        return "lenguaje inapropiado"
    flat = re.sub(r"[^a-z]", "", n)
    for bad in STRONG_SUBSTRING:
        if len(bad) >= 5 and bad.replace("-", "") in flat:
            return "contenido no permitido"
    return None


PASSWORD_INPUT = re.compile(r"<input[^>]+type\s*=\s*[\"']?password", re.I)


def phishing_hint(html):
    """Formularios que piden contraseñas: un sitio estático no los necesita y son la base del phishing."""
    return "los formularios de contraseña no están permitidos" if PASSWORD_INPUT.search(html) else None
