// Idiomas de Budsin Hosting: se elige según el navegador y la persona puede cambiarlo con el selector.
// El español es la base (siempre cargado y valor de reserva); los demás idiomas se descargan solo si hacen falta.
import ES from "./lang/es.js";

export const LANGS = { es: "Español", en: "English", pt: "Português", fr: "Français", de: "Deutsch" };
const LOCALES = { es: "es-PE", en: "en-US", pt: "pt-BR", fr: "fr-FR", de: "de-DE" };
const PAYPAL_LOCALES = { es: "es_ES", en: "en_US", pt: "pt_BR", fr: "fr_FR", de: "de_DE" };
const KEY = "budsin_lang";

let dict = ES;
export let lang = "es";

export const locale = () => LOCALES[lang];
export const paypalLocale = () => PAYPAL_LOCALES[lang];
export const hasKey = (key) => key in dict || key in ES;

export function t(key, vars) {
  let s = dict[key] ?? ES[key] ?? key;
  if (vars) s = s.replace(/\{(\w+)\}/g, (m, k) => (vars[k] ?? m));
  return s;
}

// Los motivos que devuelve el servidor llegan en español: se reconocen y se traducen.
const REASONS = Object.fromEntries(Object.keys(ES).filter((k) => k.startsWith("reason.")).map((k) => [ES[k], k]));
export const reason = (text) => (REASONS[text] ? t(REASONS[text]) : text);

export function detect() {
  try {
    const saved = localStorage.getItem(KEY);
    if (LANGS[saved]) return saved;
  } catch (e) {}
  for (const l of navigator.languages && navigator.languages.length ? navigator.languages : [navigator.language]) {
    const code = String(l || "").slice(0, 2).toLowerCase();
    if (LANGS[code]) return code;
  }
  return "en";
}

function applyStatic() {
  document.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
  document.querySelectorAll("[data-i18n-html]").forEach((el) => { el.innerHTML = t(el.dataset.i18nHtml); });
  document.querySelectorAll("[data-i18n-content]").forEach((el) => { el.setAttribute("content", t(el.dataset.i18nContent)); });
}

function mountSelector() {
  const nav = document.querySelector(".nav-links");
  if (!nav || document.getElementById("lang-select")) return;
  const sel = document.createElement("select");
  sel.id = "lang-select";
  sel.className = "lang-select";
  sel.setAttribute("aria-label", t("lang.label"));
  sel.title = t("lang.label");
  for (const [code, name] of Object.entries(LANGS)) sel.add(new Option(name, code));
  sel.value = lang;
  sel.onchange = () => {
    try { localStorage.setItem(KEY, sel.value); } catch (e) {}
    location.reload();
  };
  nav.appendChild(sel);
}

export async function initI18n({ force, selector = true } = {}) {
  lang = force || detect();
  if (lang !== "es") {
    try { dict = (await import(`./lang/${lang}.js`)).default; } catch (e) { lang = "es"; dict = ES; }
  }
  document.documentElement.lang = lang;
  applyStatic();
  if (selector) mountSelector();
  document.documentElement.classList.remove("i18n-pending");
}
