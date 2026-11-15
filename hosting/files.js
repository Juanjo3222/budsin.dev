// Administrador de archivos de un sitio (Budsin Hosting): listar, subir, crear, renombrar, borrar y editar.
import { api, toast, esc, bytes, when, explain } from "./hosting.js";
import { t } from "./i18n.js";

const enc = encodeURIComponent;
const q = (p) => "?path=" + enc(p);
const join = (a, b) => (a ? a + "/" + b : b);
const TYPES = { html: "text/html", htm: "text/html", css: "text/css", js: "text/javascript", json: "application/json", txt: "text/plain" };

// Abre el administrador. onChange(me) se llama tras cada cambio para refrescar el panel (almacenamiento, contadores).
export function openFiles(site, siteUrl, onChange) {
  closeFiles();
  const root = document.createElement("div");
  root.className = "fm-back";
  root.id = "fm-modal";
  root.innerHTML = `<div class="fm" role="dialog" aria-modal="true" aria-label="${esc(t("fm.title", { site }))}">
    <div class="fm-head"><strong>${esc(t("fm.title", { site }))}</strong><button class="btn btn-secondary h-btn-sm" data-act="close">${esc(t("fm.close"))}</button></div>
    <div class="fm-crumbs" id="fm-crumbs"></div>
    <div class="fm-tools">
      <button class="btn btn-primary h-btn-sm" data-act="upload">${esc(t("fm.upload"))}</button>
      <button class="btn btn-secondary h-btn-sm" data-act="folder">${esc(t("fm.newfolder"))}</button>
      <button class="btn btn-secondary h-btn-sm" data-act="file">${esc(t("fm.newfile"))}</button>
      <input type="file" id="fm-pick" multiple hidden>
    </div>
    <div class="bar fm-prog" id="fm-prog" style="display:none"><i></i></div>
    <div class="fm-list" id="fm-list"><p class="h-muted">…</p></div>
    <p class="h-muted fm-drop">${esc(t("fm.drop"))}</p>
    <div id="fm-editor"></div>
  </div>`;
  document.body.appendChild(root);
  document.body.style.overflow = "hidden";

  let cwd = "";
  const $$ = (s) => root.querySelector(s);
  const fail = (e) => toast("err", explain(e));
  const changed = async () => { try { onChange && onChange(await api("/me")); } catch (e) {} };

  async function load(path = cwd) {
    try {
      const d = await api(`/sites/${enc(site)}/files${q(path)}`);
      cwd = d.path || "";
      draw(d.entries);
    } catch (e) { fail(e); if (path) load(""); }
  }

  function draw(entries) {
    const parts = cwd ? cwd.split("/") : [];
    $$("#fm-crumbs").innerHTML = [`<a href="#" data-go="">${esc(site)} / ${esc(t("fm.root"))}</a>`]
      .concat(parts.map((p, i) => `<a href="#" data-go="${esc(parts.slice(0, i + 1).join("/"))}">${esc(p)}</a>`)).join(" / ");
    const rows = entries.map((e) => {
      const path = join(cwd, e.name);
      const name = e.type === "dir"
        ? `<a href="#" data-go="${esc(path)}">📁 ${esc(e.name)}</a>`
        : `<a href="${esc(siteUrl.replace(/\/$/, "") + "/" + path.split("/").map(enc).join("/"))}" target="_blank" rel="noopener">📄 ${esc(e.name)}</a>`;
      return `<tr><td class="fm-n">${name}</td><td>${e.type === "dir" ? "" : esc(bytes(e.size))}</td><td class="fm-d">${esc(when(e.mtime))}</td>
        <td class="fm-a">${e.editable ? `<button class="btn btn-secondary h-btn-sm" data-edit="${esc(path)}">${esc(t("fm.edit"))}</button>` : ""}
        <button class="btn btn-secondary h-btn-sm" data-ren="${esc(path)}">${esc(t("fm.rename"))}</button>
        <button class="btn btn-danger h-btn-sm" data-rm="${esc(path)}" data-dir="${e.type === "dir" ? 1 : 0}">${esc(t("fm.delete"))}</button></td></tr>`;
    }).join("");
    $$("#fm-list").innerHTML = entries.length
      ? `<div class="h-scroll"><table class="hist fm-table"><thead><tr><th>${esc(t("fm.col.name"))}</th><th>${esc(t("fm.col.size"))}</th><th class="fm-d">${esc(t("fm.col.date"))}</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`
      : `<p class="h-muted">${esc(t("fm.empty"))}</p>`;
  }

  async function send(files) {
    files = [...files];
    if (!files.length) return;
    const prog = $$("#fm-prog"), bar = prog.firstElementChild;
    prog.style.display = ""; bar.style.width = "0";
    let ok = 0;
    for (let i = 0; i < files.length; i++) {
      const f = files[i];
      // si viene de arrastrar una carpeta, webkitRelativePath conserva la estructura
      const rel = join(cwd, f.webkitRelativePath || f.name);
      toast("ok", t("fm.uploading", { i: i + 1, n: files.length }));
      try {
        await api(`/sites/${enc(site)}/file${q(rel)}`, { method: "PUT", raw: f, rawType: f.type || TYPES[f.name.split(".").pop().toLowerCase()] || "application/octet-stream", onProgress: (p) => (bar.style.width = Math.round(((i + p) / files.length) * 100) + "%") });
        ok++;
      } catch (e) { toast("err", f.name + ": " + explain(e)); }
    }
    prog.style.display = "none";
    if (ok) toast("ok", t("fm.uploaded", { n: ok }));
    await load(); changed();
  }

  async function edit(path) {
    let d;
    try { d = await api(`/sites/${enc(site)}/file${q(path)}`); } catch (e) { return fail(e); }
    const box = $$("#fm-editor");
    box.innerHTML = `<div class="fm-edit"><div class="fm-head"><strong>${esc(path)}</strong>
      <span><button class="btn btn-primary h-btn-sm" data-act="save">${esc(t("fm.save"))}</button> <button class="btn btn-secondary h-btn-sm" data-act="ecancel">${esc(t("fm.cancel"))}</button></span></div>
      <textarea class="h-input" id="fm-text" spellcheck="false" wrap="off"></textarea><p class="h-muted">${esc(t("fm.editor.note"))}</p></div>`;
    const ta = $$("#fm-text");
    ta.value = d.content;
    box.dataset.path = path;
    ta.focus();
    box.scrollIntoView({ block: "nearest" });
  }

  async function save() {
    const path = $$("#fm-editor").dataset.path;
    try {
      await api(`/sites/${enc(site)}/file${q(path)}`, { method: "PUT", raw: new Blob([$$("#fm-text").value]), rawType: "text/plain" });
      toast("ok", t("fm.saved")); $$("#fm-editor").innerHTML = ""; await load(); changed();
    } catch (e) { fail(e); }
  }

  async function mutate(fn) { try { await fn(); await load(); changed(); } catch (e) { fail(e); } }

  root.addEventListener("click", (ev) => {
    if (ev.target === root) return closeFiles();
    const el = ev.target.closest("a[data-go],button");
    if (!el) return;
    if (el.dataset.go !== undefined) { ev.preventDefault(); $$("#fm-editor").innerHTML = ""; return load(el.dataset.go); }
    const act = el.dataset.act;
    if (act === "close") return closeFiles();
    if (act === "upload") return $$("#fm-pick").click();
    if (act === "ecancel") return ($$("#fm-editor").innerHTML = "");
    if (act === "save") return save();
    if (act === "folder") {
      const n = prompt(t("fm.prompt.folder"));
      return n && n.trim() && mutate(() => api(`/sites/${enc(site)}/mkdir${q(join(cwd, n.trim()))}`, { method: "POST" }));
    }
    if (act === "file") {
      const n = prompt(t("fm.prompt.file"));
      return n && n.trim() && mutate(() => api(`/sites/${enc(site)}/file${q(join(cwd, n.trim()))}`, { method: "PUT", raw: new Blob([""]), rawType: "text/plain" }));
    }
    if (el.dataset.edit) return edit(el.dataset.edit);
    if (el.dataset.ren) {
      const from = el.dataset.ren, n = prompt(t("fm.prompt.rename"), from.split("/").pop());
      if (!n || !n.trim()) return;
      // sin «/» se renombra dentro de la misma carpeta; con «/» es una ruta completa desde la raíz del sitio
      const to = n.includes("/") ? n.trim() : join(from.split("/").slice(0, -1).join("/"), n.trim());
      return mutate(() => api(`/sites/${enc(site)}/move`, { method: "POST", body: { from, to } }));
    }
    if (el.dataset.rm) {
      if (confirm(t("fm.confirm.delete", { name: el.dataset.rm }))) mutate(() => api(`/sites/${enc(site)}/file${q(el.dataset.rm)}`, { method: "DELETE" }));
    }
  });
  $$("#fm-pick").onchange = (ev) => { send(ev.target.files); ev.target.value = ""; };
  const fm = $$(".fm");
  ["dragenter", "dragover"].forEach((n) => fm.addEventListener(n, (e) => { e.preventDefault(); fm.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((n) => fm.addEventListener(n, (e) => { e.preventDefault(); if (n === "drop" || e.target === fm) fm.classList.remove("drag"); }));
  fm.addEventListener("drop", (e) => send(e.dataTransfer.files));
  document.addEventListener("keydown", onKey);
  function onKey(e) { if (e.key === "Escape" && !document.querySelector("#fm-text")) closeFiles(); }
  root._cleanup = () => document.removeEventListener("keydown", onKey);
  load("");
}

export function closeFiles() {
  const m = document.getElementById("fm-modal");
  if (!m) return;
  m._cleanup && m._cleanup();
  m.remove();
  document.body.style.overflow = "";
}
