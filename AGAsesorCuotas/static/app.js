/* AG Asesor — Envío de cuotas. Lógica de la interfaz.
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
  const SIMULATION = document.body.dataset.simulation === "true";
  const DRAFT_KEY = "agasesor.borrador.v1" + (SIMULATION ? ".sim" : "");
  const CSRF = document.querySelector('meta[name="csrf-token"]').content;

  const $ = (id) => document.getElementById(id);

  let rows = [];
  let nextId = 1;
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
    const s = String(raw || "").trim().replace(/€/g, "").replace(/[\s ]/g, "");
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

  function isEmptyRow(r) {
    return !r.name.trim() && !r.email.trim() && !r.amount.trim();
  }

  // ---------------------------------------------------------------- validación

  function validateRows() {
    const result = new Map(); // id -> {field: msg}
    const seen = new Map();
    let total = 0, valid = 0, invalid = 0;
    rows.forEach((r, idx) => {
      if (isEmptyRow(r)) return;
      const errs = {};
      const name = r.name.trim().replace(/\s+/g, " ");
      if (!name) errs.name = "El nombre es obligatorio.";
      else if (name.length > 200) errs.name = "Máximo 200 caracteres.";

      const email = r.email.trim();
      if (!email) errs.email = "El correo es obligatorio.";
      else if (email.length > 254 || !EMAIL_RE.test(email)) errs.email = "El correo no tiene un formato válido.";
      else {
        const key = email.toLowerCase();
        if (seen.has(key)) errs.email = "Correo repetido (fila " + (seen.get(key) + 1) + ").";
        else seen.set(key, idx);
      }

      const amount = parseAmountCents(r.amount);
      if (amount.error) errs.amount = amount.error;

      if (Object.keys(errs).length) { invalid++; result.set(r.id, errs); }
      else { valid++; total += amount.cents; }
    });
    return { errors: result, total, valid, invalid };
  }

  function refresh() {
    const v = validateRows();
    const tbody = $("clients-body");
    Array.from(tbody.rows).forEach((tr) => {
      const id = Number(tr.dataset.id);
      const r = rows.find((x) => x.id === id);
      const errs = v.errors.get(id) || {};
      tr.querySelectorAll("input[data-field]").forEach((input) => {
        const f = input.dataset.field;
        const show = errs[f] && (showAllErrors || r.touched[f]);
        input.parentElement.classList.toggle("invalid", !!show);
        input.parentElement.querySelector(".field-error").textContent = show ? errs[f] : "";
        input.setAttribute("aria-invalid", show ? "true" : "false");
      });
    });

    const label = periodLabel();
    $("subject-preview").textContent = label ? "Cuota correspondiente a " + label : "Periodo no válido";
    $("sum-rows").textContent = v.valid + v.invalid;
    $("sum-valid").textContent = v.valid;
    $("sum-errors").textContent = v.invalid;
    $("sum-errors").classList.toggle("has-errors", v.invalid > 0);
    $("sum-period").textContent = label || "—";
    $("sum-total").textContent = formatCents(v.total) + " €";
    $("empty-msg").hidden = rows.length > 0;

    let hint = "";
    if (v.valid + v.invalid === 0) hint = "";
    else if (v.invalid) hint = "Corrija las filas marcadas en rojo para poder enviar.";
    else if (!label) hint = "Seleccione un periodo válido.";
    $("review-hint").textContent = hint;
    $("review-hint").hidden = !hint;
    // Con errores el botón sigue activo: al pulsarlo se muestran todos los errores.
    $("btn-review").disabled = !((v.valid > 0 || v.invalid > 0) && label);
    saveDraft();
    return v;
  }

  // ---------------------------------------------------------------- tabla

  function addRow(data, focus) {
    const r = {
      id: nextId++,
      name: (data && data.name) || "",
      email: (data && data.email) || "",
      amount: (data && data.amount) || "",
      touched: {},
    };
    if (data) r.touched = { name: true, email: true, amount: true };
    rows.push(r);
    const tr = $("row-template").content.firstElementChild.cloneNode(true);
    tr.dataset.id = r.id;
    tr.querySelectorAll("input[data-field]").forEach((input) => { input.value = r[input.dataset.field]; });
    $("clients-body").appendChild(tr);
    renumber();
    if (focus) tr.querySelector('input[data-field="name"]').focus();
    return r;
  }

  function renumber() {
    Array.from($("clients-body").rows).forEach((tr, i) => { tr.cells[0].textContent = i + 1; });
  }

  function deleteRow(id) {
    rows = rows.filter((r) => r.id !== id);
    const tr = $("clients-body").querySelector('tr[data-id="' + id + '"]');
    if (tr) tr.remove();
    renumber();
    refresh();
  }

  function setRows(list) {
    rows = [];
    $("clients-body").textContent = "";
    list.forEach((c) => addRow(c, false));
    showAllErrors = false;
    refresh();
  }

  $("clients-body").addEventListener("input", (e) => {
    const input = e.target;
    if (!input.dataset.field) return;
    const id = Number(input.closest("tr").dataset.id);
    const r = rows.find((x) => x.id === id);
    r[input.dataset.field] = input.value;
    refresh();
  });

  $("clients-body").addEventListener("focusout", (e) => {
    const input = e.target;
    if (!input.dataset || !input.dataset.field) return;
    const id = Number(input.closest("tr").dataset.id);
    const r = rows.find((x) => x.id === id);
    if (r && input.value.trim()) { r.touched[input.dataset.field] = true; refresh(); }
  });

  $("clients-body").addEventListener("keydown", (e) => {
    // Intro en el importe de la última fila añade una fila nueva.
    if (e.key === "Enter" && e.target.dataset.field === "amount") {
      const tr = e.target.closest("tr");
      if (!tr.nextElementSibling) { e.preventDefault(); addRow(null, true); refresh(); }
    }
  });

  $("clients-body").addEventListener("click", (e) => {
    const btn = e.target.closest('[data-action="delete"]');
    if (!btn) return;
    const id = Number(btn.closest("tr").dataset.id);
    const r = rows.find((x) => x.id === id);
    if (r && !isEmptyRow(r) && !confirm("¿Eliminar la fila de «" + (r.name.trim() || r.email.trim() || "sin nombre") + "»?")) return;
    deleteRow(id);
  });

  $("btn-add").addEventListener("click", () => { addRow(null, true); refresh(); });

  $("btn-clear").addEventListener("click", () => {
    if (rows.some((r) => !isEmptyRow(r)) && !confirm("¿Vaciar la tabla? Se eliminarán todas las filas.")) return;
    setRows([]);
  });

  $("btn-load-last").addEventListener("click", async () => {
    const res = await api("GET", "/api/last-clients");
    if (!res.ok) { alert(res.data.error || "No se pudo cargar la última tanda."); return; }
    if (!res.data.clients.length) { alert("Todavía no hay ninguna tanda registrada."); return; }
    if (rows.some((r) => !isEmptyRow(r)) && !confirm("¿Sustituir las filas actuales por los clientes de la última tanda?")) return;
    setRows(res.data.clients);
  });

  $("month").addEventListener("change", refresh);
  $("year").addEventListener("input", refresh);

  // ---------------------------------------------------------------- borrador

  function saveDraft() {
    try {
      localStorage.setItem(DRAFT_KEY, JSON.stringify({
        rows: rows.map((r) => ({ name: r.name, email: r.email, amount: r.amount })),
      }));
    } catch (e) { /* almacenamiento no disponible */ }
  }

  function loadDraft() {
    try {
      const d = JSON.parse(localStorage.getItem(DRAFT_KEY) || "null");
      if (d && Array.isArray(d.rows)) return d.rows.filter((r) => r && typeof r === "object")
        .map((r) => ({ name: String(r.name || ""), email: String(r.email || ""), amount: String(r.amount || "") }));
    } catch (e) { /* ignorar */ }
    return null;
  }

  // ---------------------------------------------------------------- confirmación

  function payload() {
    return {
      month: $("month").value,
      year: $("year").value,
      clients: rows.filter((r) => !isEmptyRow(r)).map((r) => ({ name: r.name, email: r.email, amount: r.amount })),
    };
  }

  $("btn-review").addEventListener("click", async () => {
    showAllErrors = true;
    const v = refresh();
    if (v.invalid) return;
    $("btn-review").disabled = true;
    const res = await api("POST", "/api/preview", payload());
    $("btn-review").disabled = false;
    if (!res.ok) { alert(res.data.error || "No se pudo preparar la tanda."); return; }
    if (!res.data.valid) {
      alert("El servidor ha detectado errores en " + res.data.summary.error_count + " fila(s). Revise la tabla.");
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
    $("c-blocking").hidden = blocking.length === 0;
    fillList($("c-blocking-list"), blocking, (c) => c.name + " <" + c.email + ">: " + c.status_label + when(c));
    $("c-resend").hidden = resend.length === 0;
    fillList($("c-resend-list"), resend, (c) => c.name + " <" + c.email + ">: " + c.amount + " €" + when(c));
    $("c-resend-ack").checked = false;
  }

  function openConfirm(p) {
    $("confirm-step").hidden = false;
    $("progress-step").hidden = true;
    $("c-period").textContent = p.summary.period_label;
    $("c-count").textContent = p.summary.valid_count;
    $("c-total").textContent = p.summary.total + " €";
    renderConflicts(p.conflicts);

    const tbody = $("c-recipients");
    tbody.textContent = "";
    p.recipients.forEach((r) => tbody.appendChild(el("tr", null, [
      el("td", { text: r.name }), el("td", { text: r.email }), el("td", { text: r.amount + " €", cls: "col-amount" }),
    ])));
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
    $("p-text").textContent = "Periodo " + b.period_label + ": " + done + " de " + b.total + " procesados.";

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

  $("p-close").addEventListener("click", () => {
    $("confirm-dialog").close();
    setRows([]);
    addRow(null, true);
    refresh();
  });

  $("p-history").addEventListener("click", () => {
    $("confirm-dialog").close();
    setRows([]);
    addRow(null, false);
    refresh();
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
    $("tab-history").hidden = name !== "history";
    $("tab-settings").hidden = name !== "settings";
    if (name === "history") loadHistory();
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
        el("td", { text: r.period_label }),
        el("td", { text: r.name }),
        el("td", { text: r.email }),
        el("td", { cls: "col-amount", text: r.amount + " €" }),
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
    if (draft && draft.length) setRows(draft);
    else { addRow(null, false); refresh(); }
    const st = await checkStatus();
    if (st && st.active_batch) startProgress(st.active_batch); // recarga durante un envío
    else if (st && !st.sender) switchTab("settings"); // primera vez: elegir la cuenta
  })();
})();
