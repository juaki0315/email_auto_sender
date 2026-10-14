/* AG Asesor — Envío de correos a clientes. Lógica de la interfaz.
 * Las validaciones replican las del backend (core.py), que es quien decide.
 * Todo el texto procedente de datos se inserta con textContent (nunca innerHTML).
 */
(function () {
  "use strict";

  const MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
    "agosto", "septiembre", "octubre", "noviembre", "diciembre"];
  const EMAIL_RE = /^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$/;
  const AMOUNT_PLAIN_RE = /^(\d+)(?:[.,](\d{1,2}))?$/;
  const AMOUNT_GROUPED_RE = /^(\d{1,3}(?:\.\d{3})+)(?:,(\d{1,2}))?$/;
  const MAX_AMOUNT_CENTS = 1000000000;
  // Igual que core.PLACEHOLDER_RE.
  const PLACEHOLDER_RE = /\{([A-Za-zÁÉÍÓÚÜÑáéíóúüñ_][A-Za-zÁÉÍÓÚÜÑáéíóúüñ0-9_]*)\}/g;
  const BUILTIN = ["nombre", "periodo"];
  const AMOUNT_FIELD = "importe";
  const SIMULATION = document.body.dataset.simulation === "true";
  const DRAFT_KEY = "agasesor.borrador.v2" + (SIMULATION ? ".sim" : "");
  const CSRF = document.querySelector('meta[name="csrf-token"]').content;

  const $ = (id) => document.getElementById(id);

  let templates = [];
  let clients = [];            // [{id, name, email}] guardados en la aplicación
  let selected = new Set();    // ids de clientes marcados
  let values = {};             // {id_cliente: {campo: texto}} solo para este envío
  let touched = {};            // {id_cliente: {campo: true}}
  let editingClientId = null;  // null = alta de cliente nuevo
  let editingTemplateId = null;
  let showAllErrors = false;
  let lastPreview = null;
  let pollTimer = null;
  let currentSender = "";

  // ---------------------------------------------------------------- utilidades

  async function api(method, url, body) {
    const opts = { method, headers: { "Accept": "application/json" } };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.headers["X-CSRF-Token"] = CSRF;
      opts.body = JSON.stringify(body);
    }
    let resp;
    try {
      resp = await fetch(url, opts);
    } catch (e) {
      return { ok: false, status: 0, data: { error: "No se pudo contactar con la aplicación. ¿Se cerró la ventana del programa?" } };
    }
    let data = {};
    try { data = await resp.json(); } catch (e) { /* sin cuerpo */ }
    return { ok: resp.ok, status: resp.status, data };
  }

  function el(tag, opts, children) {
    const node = document.createElement(tag);
    if (opts) {
      if (opts.text !== undefined) node.textContent = opts.text;
      if (opts.cls) node.className = opts.cls;
      if (opts.attrs) for (const k in opts.attrs) node.setAttribute(k, opts.attrs[k]);
    }
    (children || []).forEach((c) => node.appendChild(c));
    return node;
  }

  function parseAmountCents(raw) {
    const s = String(raw || "").trim().replace(/€/g, "").replace(/[\s ]/g, "");
    if (!s) return { error: "El importe es obligatorio." };
    if (s.startsWith("-")) return { error: "El importe no puede ser negativo." };
    let m = AMOUNT_GROUPED_RE.exec(s);
    let units;
    if (m) {
      units = m[1].replace(/\./g, "");
    } else {
      m = AMOUNT_PLAIN_RE.exec(s);
      if (!m) return { error: "Importe no válido (ej.: 45,50; máx. 2 decimales)." };
      units = m[1];
    }
    if (units.length > 9) return { error: "El importe es demasiado alto. Revise si es correcto." };
    const cents = parseInt(units, 10) * 100 + parseInt((m[2] || "").padEnd(2, "0"), 10);
    if (cents > MAX_AMOUNT_CENTS) return { error: "El importe es demasiado alto. Revise si es correcto." };
    return { cents };
  }

  function formatCents(cents) {
    const units = Math.floor(cents / 100);
    const dec = String(cents % 100).padStart(2, "0");
    return String(units).replace(/\B(?=(\d{3})+(?!\d))/g, ".") + "," + dec;
  }

  function periodLabel() {
    const m = parseInt($("month").value, 10);
    const y = parseInt($("year").value, 10);
    if (!(m >= 1 && m <= 12) || !(y >= 2000 && y <= 2100)) return null;
    return MONTHS[m - 1] + " de " + y;
  }

  function fieldLabel(name) {
    const t = name.replace(/_/g, " ").trim();
    return t.charAt(0).toUpperCase() + t.slice(1);
  }

  function templateFields(subject, body) {
    const out = [];
    [subject, body].forEach((text) => {
      for (const m of String(text || "").matchAll(PLACEHOLDER_RE)) {
        const name = m[1].toLowerCase();
        if (!BUILTIN.includes(name) && !out.includes(name)) out.push(name);
      }
    });
    return out;
  }

  function renderText(text, vals) {
    return String(text || "").replace(PLACEHOLDER_RE, (all, name) => {
      const v = vals[name.toLowerCase()];
      return v === undefined ? all : v;
    });
  }

  function currentTemplate() {
    const id = parseInt($("tpl-select").value, 10);
    return templates.find((t) => t.id === id) || null;
  }

  function currentFields() {
    const t = currentTemplate();
    return t ? t.fields.map((f) => f.name) : [];
  }

  function matchesSearch(c) {
    const q = $("client-search").value.trim().toLowerCase();
    return !q || c.name.toLowerCase().includes(q) || c.email.toLowerCase().includes(q);
  }

  // ---------------------------------------------------------------- validación

  function validateSelection() {
    const fields = currentFields();
    const errors = new Map(); // id -> {campo: mensaje}
    let total = 0, valid = 0, invalid = 0;
    clients.forEach((c) => {
      if (!selected.has(c.id)) return;
      const vals = values[c.id] || {};
      const errs = {};
      fields.forEach((f) => {
        const raw = String(vals[f] || "");
        if (f === AMOUNT_FIELD) {
          const a = parseAmountCents(raw);
          if (a.error) errs[f] = a.error; else total += a.cents;
        } else if (!raw.trim()) {
          errs[f] = "Obligatorio.";
        } else if (raw.trim().length > 500) {
          errs[f] = "Máximo 500 caracteres.";
        }
      });
      if (Object.keys(errs).length) { invalid++; errors.set(c.id, errs); } else valid++;
    });
    return { errors, total, valid, invalid };
  }

  function refresh() {
    const v = validateSelection();
    const t = currentTemplate();
    const hasAmount = currentFields().includes(AMOUNT_FIELD);

    Array.from($("clients-body").rows).forEach((tr) => {
      const id = Number(tr.dataset.id);
      const errs = v.errors.get(id) || {};
      tr.classList.toggle("selected", selected.has(id));
      tr.querySelectorAll("input[data-field]").forEach((input) => {
        const f = input.dataset.field;
        const show = errs[f] && (showAllErrors || (touched[id] && touched[id][f]));
        input.disabled = !selected.has(id);
        input.parentElement.classList.toggle("invalid", !!show);
        input.parentElement.querySelector(".field-error").textContent = show ? errs[f] : "";
        input.setAttribute("aria-invalid", show ? "true" : "false");
      });
    });
    const all = $("select-all");
    if (all) {
      const visible = clients.filter(matchesSearch);
      const n = visible.filter((c) => selected.has(c.id)).length;
      all.checked = visible.length > 0 && n === visible.length;
      all.indeterminate = n > 0 && n < visible.length;
    }

    const label = periodLabel();
    $("subject-preview").textContent = t ? renderText(t.subject, { periodo: label || "{periodo}" }) : "—";
    $("sum-rows").textContent = v.valid + v.invalid;
    $("sum-errors").textContent = v.invalid;
    $("sum-errors").classList.toggle("has-errors", v.invalid > 0);
    $("sum-period").textContent = label || "—";
    $("sum-total-box").hidden = !hasAmount;
    $("sum-total").textContent = formatCents(v.total) + " €";
    $("clients-count").textContent = clients.length ? "(" + clients.length + " guardados)" : "";

    let hint = "";
    if (!t) hint = "Cree o elija una plantilla.";
    else if (v.valid + v.invalid === 0) hint = clients.length ? "Marque los clientes a los que quiere enviar el correo." : "";
    else if (v.invalid) hint = "Rellene los datos marcados en rojo de los clientes seleccionados.";
    else if (!label) hint = "Seleccione un periodo válido.";
    $("review-hint").textContent = hint;
    $("review-hint").hidden = !hint;
    $("review-hint").classList.toggle("info", !v.invalid);
    // Con errores el botón sigue activo: al pulsarlo se muestran todos los errores.
    $("btn-review").disabled = !(t && v.valid + v.invalid > 0 && label);
    saveDraft();
    return v;
  }

  // ---------------------------------------------------------------- tabla de clientes

  function renderClients() {
    const fields = currentTemplate() ? currentTemplate().fields : [];
    const head = $("clients-head");
    head.textContent = "";
    const all = el("input", { attrs: { type: "checkbox", id: "select-all", "aria-label": "Seleccionar todos" } });
    head.appendChild(el("th", { cls: "col-check" }, [all]));
    head.appendChild(el("th", { text: "Nombre del cliente" }));
    head.appendChild(el("th", { text: "Correo electrónico" }));
    fields.forEach((f) => head.appendChild(el("th", {
      text: f.amount ? "Importe (€)" : f.label, cls: f.amount ? "col-amount" : "col-field",
    })));
    head.appendChild(el("th", { cls: "col-actions" }, [el("span", { cls: "sr-only", text: "Acciones" })]));

    const tbody = $("clients-body");
    tbody.textContent = "";
    const visible = clients.filter(matchesSearch);
    visible.forEach((c) => {
      const check = el("input", { attrs: { type: "checkbox", "data-action": "select", "aria-label": "Enviar a " + c.name } });
      check.checked = selected.has(c.id);
      const tr = el("tr", { attrs: { "data-id": c.id } }, [
        el("td", { cls: "col-check" }, [check]),
        el("td", { text: c.name, cls: "client-name" }),
        el("td", { text: c.email, cls: "client-email" }),
      ]);
      fields.forEach((f) => {
        const input = el("input", {
          attrs: {
            type: "text", "data-field": f.name, autocomplete: "off", maxlength: "500",
            "aria-label": (f.amount ? "Importe" : f.label) + " de " + c.name,
          },
        });
        if (f.amount) { input.setAttribute("inputmode", "decimal"); input.setAttribute("placeholder", "0,00"); }
        input.value = (values[c.id] && values[c.id][f.name]) || "";
        tr.appendChild(el("td", { cls: f.amount ? "col-amount" : "col-field" }, [input, el("span", { cls: "field-error" })]));
      });
      tr.appendChild(el("td", { cls: "col-actions" }, [
        el("button", { cls: "btn-icon edit", text: "✎", attrs: { type: "button", "data-action": "edit", title: "Editar cliente", "aria-label": "Editar " + c.name } }),
        el("button", { cls: "btn-icon", text: "✕", attrs: { type: "button", "data-action": "delete", title: "Eliminar cliente", "aria-label": "Eliminar " + c.name } }),
      ]));
      tbody.appendChild(tr);
    });
    $("empty-msg").hidden = clients.length > 0;
    $("no-match-msg").hidden = !(clients.length > 0 && visible.length === 0);
    $("clients-table").hidden = clients.length === 0;
    refresh();
  }

  async function loadClients() {
    const res = await api("GET", "/api/clients");
    if (!res.ok) { alert(res.data.error || "No se pudo cargar la lista de clientes."); return; }
    clients = res.data.clients;
    const ids = new Set(clients.map((c) => c.id));
    selected = new Set(Array.from(selected).filter((id) => ids.has(id)));
    renderClients();
  }

  $("clients-head").addEventListener("change", (e) => {
    if (e.target.id !== "select-all") return;
    clients.filter(matchesSearch).forEach((c) => {
      if (e.target.checked) selected.add(c.id); else selected.delete(c.id);
    });
    renderClients();
  });

  $("clients-body").addEventListener("change", (e) => {
    if (e.target.dataset.action !== "select") return;
    const id = Number(e.target.closest("tr").dataset.id);
    if (e.target.checked) selected.add(id); else selected.delete(id);
    refresh();
    if (e.target.checked) {
      const first = e.target.closest("tr").querySelector("input[data-field]");
      if (first && !first.value) first.focus();
    }
  });

  $("clients-body").addEventListener("input", (e) => {
    const input = e.target;
    if (!input.dataset.field) return;
    const id = Number(input.closest("tr").dataset.id);
    (values[id] = values[id] || {})[input.dataset.field] = input.value;
    refresh();
  });

  $("clients-body").addEventListener("focusout", (e) => {
    const input = e.target;
    if (!input.dataset || !input.dataset.field) return;
    const id = Number(input.closest("tr").dataset.id);
    if (input.value.trim()) { (touched[id] = touched[id] || {})[input.dataset.field] = true; refresh(); }
  });

  $("clients-body").addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-action]");
    if (!btn) return;
    const id = Number(btn.closest("tr").dataset.id);
    const c = clients.find((x) => x.id === id);
    if (!c) return;
    if (btn.dataset.action === "edit") openClientForm(c);
    if (btn.dataset.action === "delete") {
      if (!confirm("¿Eliminar a «" + c.name + "» (" + c.email + ") de la lista de clientes?\n\nEl historial de envíos se conserva.")) return;
      const res = await api("DELETE", "/api/clients/" + id, {});
      if (!res.ok && res.status !== 404) { alert(res.data.error || "No se pudo eliminar el cliente."); return; }
      selected.delete(id);
      delete values[id];
      await loadClients();
    }
  });

  $("client-search").addEventListener("input", renderClients);

  // ---------------------------------------------------------------- alta y edición de clientes

  function openClientForm(c) {
    editingClientId = c ? c.id : null;
    $("client-form-title").textContent = c ? "Editar cliente" : "Nuevo cliente";
    $("cf-save").textContent = c ? "Guardar cambios" : "Guardar cliente";
    $("cf-name").value = c ? c.name : "";
    $("cf-email").value = c ? c.email : "";
    $("cf-error").hidden = true;
    $("client-form").hidden = false;
    $("cf-name").focus();
  }

  function closeClientForm() {
    $("client-form").hidden = true;
    editingClientId = null;
  }

  $("btn-add").addEventListener("click", () => openClientForm(null));
  $("cf-cancel").addEventListener("click", closeClientForm);

  $("client-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = $("cf-name").value.trim().replace(/\s+/g, " ");
    const email = $("cf-email").value.trim();
    let error = "";
    if (!name) error = "El nombre es obligatorio.";
    else if (!email) error = "El correo es obligatorio.";
    else if (email.length > 254 || !EMAIL_RE.test(email)) error = "El correo no tiene un formato válido.";
    if (error) { $("cf-error").textContent = error; $("cf-error").hidden = false; return; }

    $("cf-save").disabled = true;
    const res = editingClientId === null
      ? await api("POST", "/api/clients", { name, email })
      : await api("PUT", "/api/clients/" + editingClientId, { name, email });
    $("cf-save").disabled = false;
    if (!res.ok) { $("cf-error").textContent = res.data.error || "No se pudo guardar el cliente."; $("cf-error").hidden = false; return; }
    const isNew = editingClientId === null;
    // Un cliente recién añadido queda marcado para este envío.
    if (isNew) selected.add(res.data.client.id);
    closeClientForm();
    await loadClients();
    if (isNew) {
      const tr = $("clients-body").querySelector('tr[data-id="' + res.data.client.id + '"]');
      if (tr) {
        tr.scrollIntoView({ block: "nearest" });
        const first = tr.querySelector("input[data-field]");
        if (first) first.focus();
      }
    }
  });

  // ---------------------------------------------------------------- plantillas (selección)

  function fillTemplateSelect(preferId) {
    const select = $("tpl-select");
    const keep = preferId || parseInt(select.value, 10);
    select.textContent = "";
    if (!templates.length) {
      select.appendChild(el("option", { text: "No hay plantillas", attrs: { value: "" } }));
      return;
    }
    templates.forEach((t) => select.appendChild(el("option", { text: t.name, attrs: { value: t.id } })));
    if (templates.some((t) => t.id === keep)) select.value = String(keep);
  }

  async function loadTemplates(preferId) {
    const res = await api("GET", "/api/templates");
    if (!res.ok) { alert(res.data.error || "No se pudieron cargar las plantillas."); return; }
    templates = res.data.templates;
    fillTemplateSelect(preferId);
    renderTemplateList();
    renderClients();
  }

  $("tpl-select").addEventListener("change", () => { showAllErrors = false; renderClients(); });
  $("month").addEventListener("change", refresh);
  $("year").addEventListener("input", refresh);
  $("btn-goto-templates").addEventListener("click", () => switchTab("templates"));

  // ---------------------------------------------------------------- plantillas (gestión)

  function renderTemplateList() {
    const tbody = $("tpl-list");
    tbody.textContent = "";
    $("tpl-empty").hidden = templates.length > 0;
    templates.forEach((t) => {
      const fields = el("td");
      if (!t.fields.length) fields.appendChild(el("span", { cls: "muted", text: "Ninguno" }));
      t.fields.forEach((f) => fields.appendChild(el("span", { cls: "chip static", text: "{" + f.name + "}" })));
      const edit = el("button", { cls: "btn btn-small", text: "Editar", attrs: { type: "button" } });
      const del = el("button", { cls: "btn btn-small", text: "Eliminar", attrs: { type: "button" } });
      edit.addEventListener("click", () => openTemplateEditor(t));
      del.addEventListener("click", () => deleteTemplate(t));
      tbody.appendChild(el("tr", null, [
        el("td", { text: t.name, cls: "strong" }),
        el("td", { text: t.subject }),
        fields,
        el("td", { cls: "col-actions" }, [el("div", { cls: "row-actions" }, [edit, del])]),
      ]));
    });
  }

  function openTemplateEditor(t) {
    editingTemplateId = t ? t.id : null;
    $("tpl-editor-title").textContent = t ? "Editar plantilla" : "Nueva plantilla";
    $("te-name").value = t ? t.name : "";
    $("te-subject").value = t ? t.subject : "";
    $("te-body").value = t ? t.body : "Estimado/a {nombre}:\n\n\n\nAtentamente,\n\nAG Asesor";
    $("te-error").hidden = true;
    $("tpl-editor").hidden = false;
    $("tpl-list-card").hidden = true;
    updateTemplatePreview();
    $("te-name").focus();
  }

  function closeTemplateEditor() {
    $("tpl-editor").hidden = true;
    $("tpl-list-card").hidden = false;
    editingTemplateId = null;
  }

  function updateTemplatePreview() {
    const subject = $("te-subject").value;
    const body = $("te-body").value;
    const fields = templateFields(subject, body);
    const box = $("te-fields");
    box.textContent = "";
    if (!fields.length) box.appendChild(el("span", { cls: "muted small", text: "Ninguno: el correo solo usa campos automáticos." }));
    fields.forEach((f) => box.appendChild(el("span", {
      cls: "chip static" + (f === AMOUNT_FIELD ? " amount" : ""),
      text: (f === AMOUNT_FIELD ? "Importe (€)" : fieldLabel(f)) + "  {" + f + "}",
    })));
    const sample = { nombre: "Ana López", periodo: periodLabel() || "octubre de 2026" };
    fields.forEach((f) => { sample[f] = f === AMOUNT_FIELD ? "45,50" : "[" + fieldLabel(f) + "]"; });
    $("te-prev-subject").textContent = renderText(subject, sample);
    $("te-prev-body").textContent = renderText(body, sample);
  }

  let lastTemplateInput = null;
  ["te-subject", "te-body"].forEach((id) => {
    $(id).addEventListener("input", updateTemplatePreview);
    $(id).addEventListener("focus", () => { lastTemplateInput = $(id); });
  });

  function insertPlaceholder(name) {
    const target = lastTemplateInput || $("te-body");
    const text = "{" + name + "}";
    const start = target.selectionStart ?? target.value.length;
    const end = target.selectionEnd ?? target.value.length;
    target.value = target.value.slice(0, start) + text + target.value.slice(end);
    target.focus();
    target.setSelectionRange(start + text.length, start + text.length);
    updateTemplatePreview();
  }

  document.querySelectorAll("[data-insert]").forEach((b) => b.addEventListener("click", () => insertPlaceholder(b.dataset.insert)));

  $("te-insert-field").addEventListener("click", () => {
    const name = $("te-new-field").value.trim().toLowerCase().replace(/\s+/g, "_");
    if (!/^[a-záéíóúüñ_][a-záéíóúüñ0-9_]*$/.test(name)) {
      alert("Escriba un nombre de campo sin espacios ni símbolos, por ejemplo: fecha_limite");
      return;
    }
    $("te-new-field").value = "";
    insertPlaceholder(name);
  });

  $("btn-tpl-new").addEventListener("click", () => openTemplateEditor(null));
  $("te-cancel").addEventListener("click", closeTemplateEditor);

  $("te-save").addEventListener("click", async () => {
    const body = { name: $("te-name").value, subject: $("te-subject").value, body: $("te-body").value };
    let error = "";
    if (!body.name.trim()) error = "Escriba un nombre para la plantilla.";
    else if (!body.subject.trim()) error = "Escriba el asunto.";
    else if (/[\r\n]/.test(body.subject)) error = "El asunto no puede tener saltos de línea.";
    else if (!body.body.trim()) error = "Escriba el cuerpo del correo.";
    if (error) { $("te-error").textContent = error; $("te-error").hidden = false; return; }
    $("te-save").disabled = true;
    const res = editingTemplateId === null
      ? await api("POST", "/api/templates", body)
      : await api("PUT", "/api/templates/" + editingTemplateId, body);
    $("te-save").disabled = false;
    if (!res.ok) { $("te-error").textContent = res.data.error || "No se pudo guardar la plantilla."; $("te-error").hidden = false; return; }
    closeTemplateEditor();
    await loadTemplates(res.data.template.id);
  });

  async function deleteTemplate(t) {
    if (!confirm("¿Eliminar la plantilla «" + t.name + "»?\n\nLos envíos ya realizados con ella se siguen viendo en el Historial.")) return;
    const res = await api("DELETE", "/api/templates/" + t.id, {});
    if (!res.ok && res.status !== 404) { alert(res.data.error || "No se pudo eliminar la plantilla."); return; }
    await loadTemplates();
  }

  // ---------------------------------------------------------------- borrador

  function saveDraft() {
    try {
      localStorage.setItem(DRAFT_KEY, JSON.stringify({
        template: $("tpl-select").value, selected: Array.from(selected), values,
      }));
    } catch (e) { /* almacenamiento no disponible */ }
  }

  function loadDraft() {
    try {
      const d = JSON.parse(localStorage.getItem(DRAFT_KEY) || "null");
      if (!d || typeof d !== "object") return null;
      const vals = {};
      if (d.values && typeof d.values === "object") {
        Object.keys(d.values).forEach((id) => {
          const row = d.values[id];
          if (!row || typeof row !== "object") return;
          vals[id] = {};
          Object.keys(row).forEach((f) => { vals[id][f] = String(row[f] ?? ""); });
        });
      }
      return {
        template: parseInt(d.template, 10) || null,
        selected: Array.isArray(d.selected) ? d.selected.filter((x) => Number.isInteger(x)) : [],
        values: vals,
      };
    } catch (e) { return null; }
  }

  // ---------------------------------------------------------------- confirmación

  function payload() {
    const fields = currentFields();
    const t = currentTemplate();
    return {
      month: $("month").value,
      year: $("year").value,
      template_id: t ? t.id : null,
      recipients: clients.filter((c) => selected.has(c.id)).map((c) => {
        const f = {};
        fields.forEach((name) => { f[name] = (values[c.id] && values[c.id][name]) || ""; });
        return { client_id: c.id, fields: f };
      }),
    };
  }

  $("btn-review").addEventListener("click", async () => {
    showAllErrors = true;
    const v = refresh();
    if (v.invalid) {
      // Se muestran también los seleccionados ocultos por la búsqueda.
      if ($("client-search").value) { $("client-search").value = ""; renderClients(); }
      return;
    }
    $("btn-review").disabled = true;
    const res = await api("POST", "/api/preview", payload());
    $("btn-review").disabled = false;
    if (!res.ok) { alert(res.data.error || "No se pudo preparar el envío."); return; }
    if (!res.data.valid) {
      alert("Hay datos que revisar en " + res.data.summary.error_count + " cliente(s). La lista se ha actualizado.");
      await loadClients();
      return;
    }
    lastPreview = res.data;
    openConfirm(res.data);
  });

  function fillList(ul, items, fmt) {
    ul.textContent = "";
    items.forEach((c) => ul.appendChild(el("li", { text: fmt(c) })));
  }

  function renderConflicts(conflicts) {
    const blocking = conflicts.filter((c) => !c.resendable);
    const resend = conflicts.filter((c) => c.resendable);
    const when = (c) => c.attempted_at ? " — " + c.attempted_at.replace("T", " ") : "";
    const amount = (c) => c.amount ? ": " + c.amount + " €" : "";
    $("c-blocking").hidden = blocking.length === 0;
    fillList($("c-blocking-list"), blocking, (c) => c.name + " <" + c.email + ">: " + c.status_label + when(c));
    $("c-resend").hidden = resend.length === 0;
    fillList($("c-resend-list"), resend, (c) => c.name + " <" + c.email + ">" + amount(c) + when(c));
    $("c-resend-ack").checked = false;
  }

  function openConfirm(p) {
    $("confirm-step").hidden = false;
    $("progress-step").hidden = true;
    $("c-template").textContent = p.summary.template_name;
    $("c-period").textContent = p.summary.period_label;
    $("c-count").textContent = p.summary.valid_count;
    $("c-total-box").hidden = !p.summary.has_amount;
    $("c-total").textContent = (p.summary.total || "") + " €";
    $("c-amount-head").hidden = !p.summary.has_amount;
    renderConflicts(p.conflicts);

    const tbody = $("c-recipients");
    tbody.textContent = "";
    p.recipients.forEach((r) => {
      const cells = [el("td", { text: r.name }), el("td", { text: r.email })];
      if (p.summary.has_amount) cells.push(el("td", { text: r.amount + " €", cls: "col-amount" }));
      tbody.appendChild(el("tr", null, cells));
    });
    $("c-sample-to").textContent = p.sample.to;
    $("c-sample-subject").textContent = p.sample.subject;
    $("c-sample-body").textContent = p.sample.body;

    $("c-sender").textContent = currentSender || "sin seleccionar";
    $("c-ack").checked = false;
    $("c-error").hidden = !!currentSender;
    $("c-error").textContent = currentSender ? "" : "Seleccione primero la cuenta de envío en la pestaña Configuración.";
    const n = p.summary.valid_count;
    $("c-send").textContent = (SIMULATION ? "Simular envío de " : "Enviar ") + n + (n === 1 ? " correo" : " correos");
    updateSendButton();
    $("confirm-dialog").showModal();
  }

  function updateSendButton() {
    const conflicts = (lastPreview && lastPreview.conflicts) || [];
    const blocked = conflicts.some((c) => !c.resendable);
    const needsResendAck = conflicts.some((c) => c.resendable);
    $("c-send").disabled = !currentSender || blocked || !$("c-ack").checked || (needsResendAck && !$("c-resend-ack").checked);
  }

  $("c-ack").addEventListener("change", updateSendButton);
  $("c-resend-ack").addEventListener("change", updateSendButton);
  $("c-cancel").addEventListener("click", () => $("confirm-dialog").close());

  $("confirm-dialog").addEventListener("cancel", (e) => {
    // Durante el envío no se puede cerrar el diálogo con Esc.
    if (!$("progress-step").hidden && $("p-close").hidden) e.preventDefault();
  });

  $("c-send").addEventListener("click", async () => {
    $("c-send").disabled = true;
    const body = payload();
    body.confirm = true;
    body.template_updated_at = lastPreview.summary.template_updated_at;
    body.resend_ack = $("c-resend-ack").checked
      ? lastPreview.conflicts.filter((c) => c.resendable).map((c) => c.email)
      : [];
    const res = await api("POST", "/api/send", body);
    if (res.status === 202) {
      startProgress(res.data.batch_id);
      return;
    }
    if (res.status === 409 && res.data.conflicts) {
      lastPreview.conflicts = res.data.conflicts;
      renderConflicts(res.data.conflicts);
    }
    $("c-error").textContent = res.data.error || "No se pudo iniciar el envío.";
    $("c-error").hidden = false;
    updateSendButton();
  });

  // ---------------------------------------------------------------- progreso

  function startProgress(batchId) {
    $("confirm-step").hidden = true;
    $("progress-step").hidden = false;
    $("p-close").hidden = true;
    $("p-history").hidden = true;
    $("p-warning").hidden = false;
    $("p-title").textContent = SIMULATION ? "Simulando envío…" : "Enviando…";
    $("p-bar").className = "progress-bar";
    if (!$("confirm-dialog").open) $("confirm-dialog").showModal();
    clearInterval(pollTimer);
    pollTimer = setInterval(() => pollBatch(batchId), 700);
    pollBatch(batchId);
  }

  async function pollBatch(batchId) {
    const res = await api("GET", "/api/batches/" + batchId);
    if (!res.ok) return;
    const b = res.data;
    const done = b.total - b.counts.pending - b.counts.sending;
    $("p-bar").style.width = (b.total ? Math.round((done / b.total) * 100) : 100) + "%";
    $("p-text").textContent = b.template_name + " · " + b.period_label + ": " + done + " de " + b.total + " procesados.";

    const counts = $("p-counts");
    counts.textContent = "";
    [["accepted", "Aceptados por Outlook"], ["error", "Errores"], ["uncertain", "No confirmados"]].forEach(([k, label]) => {
      if (b.counts[k]) counts.appendChild(el("span", { cls: "status " + k, text: label + ": " + b.counts[k] }));
    });

    const tbody = $("p-body");
    tbody.textContent = "";
    b.recipients.forEach((r) => tbody.appendChild(el("tr", null, [
      el("td", { text: r.name }), el("td", { text: r.email }),
      el("td", null, [el("span", { cls: "status " + r.status, text: r.status_label })]),
      el("td", { cls: "detail", text: r.error || "" }),
    ])));

    if (b.finished) {
      clearInterval(pollTimer);
      const allOk = b.counts.accepted === b.total;
      $("p-title").textContent = allOk ? "Tanda finalizada" : "Tanda finalizada con incidencias";
      $("p-bar").classList.add(allOk ? "done-ok" : "done-warn");
      $("p-close").hidden = false;
      $("p-history").hidden = false;
      $("p-warning").hidden = true;
      checkStatus();
    }
  }

  function resetSelection() {
    selected = new Set();
    values = {};
    touched = {};
    showAllErrors = false;
    renderClients();
  }

  $("p-close").addEventListener("click", () => {
    $("confirm-dialog").close();
    resetSelection();
  });

  $("p-history").addEventListener("click", () => {
    $("confirm-dialog").close();
    resetSelection();
    switchTab("history");
  });

  // ---------------------------------------------------------------- historial

  function switchTab(name) {
    document.querySelectorAll(".tab").forEach((t) => {
      const active = t.dataset.tab === name;
      t.classList.toggle("active", active);
      t.setAttribute("aria-selected", active ? "true" : "false");
    });
    $("tab-batch").hidden = name !== "batch";
    $("tab-templates").hidden = name !== "templates";
    $("tab-history").hidden = name !== "history";
    $("tab-settings").hidden = name !== "settings";
    if (name === "history") loadHistory();
    if (name === "templates") { closeTemplateEditor(); renderTemplateList(); }
    if (name === "settings") loadAccounts();
  }

  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => switchTab(t.dataset.tab)));

  async function loadHistory() {
    const params = new URLSearchParams();
    if ($("h-month").value) { params.set("month", $("h-month").value); params.set("year", $("h-year").value); }
    if ($("h-status").value) params.set("status", $("h-status").value);
    $("h-year").disabled = !$("h-month").value;
    const res = await api("GET", "/api/history?" + params.toString());
    const tbody = $("history-body");
    tbody.textContent = "";
    if (!res.ok) { alert(res.data.error || "No se pudo cargar el historial."); return; }
    $("history-empty").hidden = res.data.rows.length > 0;
    res.data.rows.forEach((r) => {
      const detail = el("td", { cls: "detail" });
      if (r.sender) detail.appendChild(el("div", { text: "Desde " + r.sender }));
      if (r.error) detail.appendChild(el("div", { text: r.error }));
      if (r.resolution) detail.appendChild(el("div", { text: r.resolution }));
      if (r.status === "uncertain") {
        const yes = el("button", { cls: "btn btn-small", text: "Sí se envió", attrs: { type: "button" } });
        const no = el("button", { cls: "btn btn-small", text: "No se envió", attrs: { type: "button" } });
        yes.addEventListener("click", () => resolve(r, true));
        no.addEventListener("click", () => resolve(r, false));
        detail.appendChild(el("div", { cls: "resolve-actions" }, [yes, no]));
      }
      tbody.appendChild(el("tr", null, [
        el("td", { text: (r.attempted_at || r.batch_created_at || "").replace("T", " ") }),
        el("td", { text: r.template_name || "" }),
        el("td", { text: r.period_label }),
        el("td", { text: r.name }),
        el("td", { text: r.email }),
        el("td", { cls: "col-amount", text: r.amount ? r.amount + " €" : "—" }),
        el("td", null, [el("span", { cls: "status " + r.status, text: r.status_label })]),
        detail,
      ]));
    });
  }

  async function resolve(r, sent) {
    const q = sent
      ? "¿Ha comprobado en Outlook (Elementos enviados) que el correo a " + r.email + " SÍ salió?\n\nQuedará registrado como enviado y no se le volverá a enviar sin confirmación."
      : "¿Ha comprobado en Outlook que el correo a " + r.email + " NO salió (no está en Elementos enviados ni en la Bandeja de salida)?\n\nQuedará como error y podrá incluirlo en una nueva tanda.";
    if (!confirm(q)) return;
    const res = await api("POST", "/api/recipients/" + encodeURIComponent(r.uid) + "/resolve", { sent });
    if (!res.ok) alert(res.data.error || "No se pudo actualizar el registro.");
    loadHistory();
    checkStatus();
  }

  ["h-month", "h-status"].forEach((id) => $(id).addEventListener("change", loadHistory));
  $("h-year").addEventListener("change", loadHistory);
  $("btn-h-refresh").addEventListener("click", loadHistory);

  // ---------------------------------------------------------------- estado de Outlook

  async function checkStatus() {
    const box = $("outlook-status");
    box.dataset.state = "checking";
    $("outlook-status-text").textContent = "Comprobando Outlook…";
    const res = await api("GET", "/api/status");
    if (!res.ok && res.status === 0) {
      box.dataset.state = "error";
      $("outlook-status-text").textContent = res.data.error;
      return null;
    }
    const d = res.data;
    currentSender = d.sender || "";
    $("header-sender").textContent = currentSender || "sin seleccionar";
    box.dataset.state = d.ok ? "ok" : "error";
    $("outlook-status-text").textContent = (d.simulation ? "Simulado · " : "") + d.message;
    const badge = $("uncertain-badge");
    badge.hidden = !d.uncertain;
    badge.textContent = d.uncertain || "";
    badge.title = d.uncertain ? d.uncertain + " envío(s) pendientes de revisar" : "";
    return d;
  }

  $("btn-recheck").addEventListener("click", checkStatus);

  // ---------------------------------------------------------------- configuración

  function accountMessage(text, kind) {
    const msg = $("acc-msg");
    msg.textContent = text || "";
    msg.className = "settings-msg " + (kind || "");
    msg.hidden = !text;
  }

  async function loadAccounts() {
    const select = $("acc-select");
    select.disabled = true;
    $("btn-acc-save").disabled = true;
    select.textContent = "";
    select.appendChild(el("option", { text: "Leyendo cuentas de Outlook…" }));
    accountMessage("");
    const res = await api("GET", "/api/accounts");
    select.textContent = "";
    const d = res.data;
    if (!res.ok || !d.ok) {
      select.appendChild(el("option", { text: "No disponible" }));
      accountMessage(d.message || d.error || "No se pudieron leer las cuentas de Outlook.", "error");
      return;
    }
    if (!d.accounts.length) {
      select.appendChild(el("option", { text: "Outlook no tiene cuentas configuradas" }));
      accountMessage("Outlook no tiene ninguna cuenta configurada.", "error");
      return;
    }
    if (!d.current) select.appendChild(el("option", { text: "— Elija una cuenta —", attrs: { value: "" } }));
    d.accounts.forEach((a) => {
      const opt = el("option", { text: a, attrs: { value: a } });
      if (a === d.current) opt.selected = true;
      select.appendChild(opt);
    });
    if (d.current && !d.accounts.includes(d.current)) {
      accountMessage("La cuenta guardada (" + d.current + ") ya no está en Outlook. Elija otra.", "error");
    }
    select.disabled = false;
    updateAccountButton();
  }

  function updateAccountButton() {
    const v = $("acc-select").value;
    $("btn-acc-save").disabled = !v || v === currentSender;
  }

  $("acc-select").addEventListener("change", updateAccountButton);
  $("btn-acc-reload").addEventListener("click", loadAccounts);

  $("btn-acc-save").addEventListener("click", async () => {
    const sender = $("acc-select").value;
    if (!sender) return;
    $("btn-acc-save").disabled = true;
    const res = await api("POST", "/api/settings/sender", { sender });
    if (!res.ok) {
      accountMessage(res.data.error || "No se pudo guardar la cuenta.", "error");
      updateAccountButton();
      return;
    }
    await checkStatus();
    updateAccountButton();
    accountMessage("A partir de ahora los correos se enviarán desde " + res.data.sender + ".", "ok");
  });

  // ---------------------------------------------------------------- inicio

  (async function init() {
    const draft = loadDraft();
    if (draft) {
      selected = new Set(draft.selected);
      values = draft.values;
    }
    await loadTemplates(draft && draft.template);
    await loadClients();
    const st = await checkStatus();
    if (st && st.active_batch) startProgress(st.active_batch); // recarga durante un envío
    else if (st && !st.sender) switchTab("settings"); // primera vez: elegir la cuenta
  })();
})();
