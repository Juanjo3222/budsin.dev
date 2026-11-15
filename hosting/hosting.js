// Utilidades compartidas del panel y del admin: sesión de Google (Firebase Auth) y cliente de la API.
import { t, locale, reason, hasKey } from "./i18n.js";
import { initializeApp } from "https://www.gstatic.com/firebasejs/10.12.5/firebase-app.js";
import { getAuth, GoogleAuthProvider, signInWithPopup, signOut, onAuthStateChanged } from "https://www.gstatic.com/firebasejs/10.12.5/firebase-auth.js";

const app = initializeApp({
  apiKey: "AIzaSyCBN77CtgH-J5NYWhVyW6p1jldXdxbok-U",
  authDomain: "juanjo-games.firebaseapp.com",
  projectId: "juanjo-games",
  appId: "1:927529249414:web:410a686dc7f0da25ec3f07",
});
export const auth = getAuth(app);
export const API = "/api/hosting";

export const login = () => signInWithPopup(auth, new GoogleAuthProvider());
export const logout = () => signOut(auth);
export const onUser = (cb) => onAuthStateChanged(auth, cb);

export async function api(path, { method = "GET", body, raw, token = true, onProgress } = {}) {
  const headers = {};
  if (token) {
    if (!auth.currentUser) throw Object.assign(new Error("NO_USER"), { code: "no_user" });
    headers.Authorization = "Bearer " + (await auth.currentUser.getIdToken());
  }
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (raw) headers["Content-Type"] = "application/zip";
  const url = `${API}${path}${path.includes("?") ? "&" : "?"}_=${Date.now()}`;
  if (raw && onProgress) {
    // XHR: fetch no informa del progreso de subida
    return new Promise((resolve, reject) => {
      const x = new XMLHttpRequest();
      x.open(method, url);
      for (const [k, v] of Object.entries(headers)) x.setRequestHeader(k, v);
      x.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
      x.onload = () => {
        let data = {};
        try { data = JSON.parse(x.responseText); } catch (e) {}
        x.status >= 200 && x.status < 300 ? resolve(data) : reject(Object.assign(new Error(data.error || "HTTP_" + x.status), { code: data.error, data }));
      };
      x.onerror = () => reject(Object.assign(new Error("network"), { code: "network" }));
      x.send(raw);
    });
  }
  const res = await fetch(url, { method, headers, cache: "no-store", body: raw || (body !== undefined ? JSON.stringify(body) : undefined) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.error || "HTTP_" + res.status), { code: data.error, data });
  return data;
}

export const $ = (sel, root = document) => root.querySelector(sel);

// Aviso flotante: siempre visible aunque la persona esté abajo en la página. Los de error se quedan hasta cerrarlos.
export function toast(cls, msg) {
  let t = document.getElementById("toast-box");
  if (!t) {
    t = document.createElement("div");
    t.id = "toast-box";
    t.setAttribute("role", "status");
    t.setAttribute("aria-live", "polite");
    document.body.appendChild(t);
  }
  clearTimeout(t._timer);
  t.innerHTML = `<div class="alert ${cls}"><span></span><button type="button" aria-label="Cerrar">✕</button></div>`;
  t.querySelector("span").textContent = msg;
  t.querySelector("button").onclick = () => t.remove();
  if (cls !== "err") t._timer = setTimeout(() => t.remove(), 9000);
}
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export function bytes(n) {
  if (n >= 1024 ** 3) return (n / 1024 ** 3).toFixed(2) + " GB";
  if (n >= 1024 ** 2) return (n / 1024 ** 2).toFixed(1) + " MB";
  if (n >= 1024) return (n / 1024).toFixed(0) + " KB";
  return n + " B";
}

export function duration(sec) {
  sec = Math.max(0, Math.floor(sec));
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const p = (n) => String(n).padStart(2, "0");
  return (d ? d + "d " : "") + `${p(h)}:${p(m)}:${p(s)}`;
}

export const when = (iso) => (iso ? new Date(iso).toLocaleString(locale(), { dateStyle: "medium", timeStyle: "short" }) : "—");

export function explain(err) {
  const d = err.data || {};
  let msg = hasKey("err." + err.code) ? t("err." + err.code) : t("err.generic", { code: err.code || err.message });
  if (d.file) msg += t("err.file", { file: d.file + (d.ext ? " (" + d.ext + ")" : "") });
  if (d.reason) msg += " " + reason(d.reason) + ".";
  if (d.max && (err.code === "upload_too_large" || err.code === "file_too_large")) msg += t("err.max", { max: bytes(d.max) });
  if (d.max && err.code === "too_many_sites") msg += t("err.max", { max: d.max });
  return msg;
}
