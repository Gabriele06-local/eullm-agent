// EuLLM Agent web interface. Everything shown comes from the Core API
// (/v1) with the token kept in this browser tab; text from runs, tools and
// models is always inserted as text, never as HTML.
"use strict";

const TOKEN_KEY = "eullm-agent-token";
const app = document.getElementById("app");
let timers = [];
let health = null;
let profiles = [];

// --- small helpers -----------------------------------------------------------

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

const DECISIONS = { allow: "consentito", deny: "negato", require_approval: "con approvazione" };

/// replaceChildren without the null and false of conditional parts.
function put(node, ...children) {
  node.replaceChildren(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
}

function token() {
  try { return sessionStorage.getItem(TOKEN_KEY); } catch { return null; }
}

function setToken(t) {
  try {
    if (t) sessionStorage.setItem(TOKEN_KEY, t);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch { /* private mode: the token lives until reload */ }
}

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(method, path, body) {
  const headers = { "Authorization": "Bearer " + (token() || "") };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const resp = await fetch(path, {
    method, headers, body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store", credentials: "omit",
  });
  let data = null;
  try { data = await resp.json(); } catch { /* empty body */ }
  if (resp.status === 401) {
    setToken(null);
    clearTimers();
    login("Token non valido o scaduto.");
    throw new ApiError(401, "Token non valido");
  }
  if (!resp.ok) throw new ApiError(resp.status, (data && data.error) || ("Errore " + resp.status));
  return data;
}

function every(ms, fn) {
  fn();
  timers.push(setInterval(fn, ms));
}

function clearTimers() {
  timers.forEach(clearInterval);
  timers = [];
}

const STATUS = {
  queued: ["In coda", ""],
  running: ["In corso", "blue"],
  waiting_approval: ["Attende approvazione", "amber"],
  succeeded: ["Completato", "green"],
  failed: ["Fallito", "red"],
  pending: ["Da decidere", "amber"],
  approved: ["Approvata", "green"],
  denied: ["Rifiutata", "red"],
  expired: ["Scaduta", ""],
};

function chip(status) {
  const [label, color] = STATUS[status] || [status, ""];
  return el("span", { class: "chip " + color }, label);
}

function when(ms) {
  if (!ms) return "";
  return new Date(ms).toLocaleString("it-IT", { dateStyle: "short", timeStyle: "medium" });
}

function num(n) {
  return (n || 0).toLocaleString("it-IT");
}

function money(n) {
  return (n || 0).toLocaleString("it-IT", { minimumFractionDigits: 2, maximumFractionDigits: 4 });
}

function done(status) {
  return status === "succeeded" || status === "failed";
}

// --- layout ------------------------------------------------------------------

const PAGES = [
  ["chat", "Chat"],
  ["runs", "Attività"],
  ["approvals", "Approvazioni"],
  ["usage", "Consumi"],
];

function shell(active, ...content) {
  const pending = el("span", { class: "chip amber", id: "pending-count", hidden: true });
  const nav = el("nav", { class: "nav", "aria-label": "Sezioni" },
    el("div", { class: "brand" },
      el("span", { class: "mark", "aria-hidden": "true" }, "E"),
      el("span", {}, el("strong", {}, "EuLLM Agent"), el("small", {}, "Core"))),
    PAGES.map(([id, label]) => el("a", {
      href: "#/" + id, class: active === id ? "on" : null,
      "aria-current": active === id ? "page" : null,
    }, label, id === "approvals" ? pending : null)),
    el("span", { class: "grow" }),
    el("div", { class: "who" },
      el("span", {}, health ? "Versione " + health.version : ""),
      el("button", { class: "link", type: "button", onclick: () => { setToken(null); render(); } }, "Esci")));
  const main = el("main", { class: "main" }, ...content);
  app.replaceChildren(el("div", { class: "shell" }, nav, main));
  every(5000, refreshPending);
  return main;
}

async function refreshPending() {
  const badge = document.getElementById("pending-count");
  if (!badge) return;
  try {
    const { approvals } = await api("GET", "/v1/approvals?status=pending");
    badge.textContent = approvals.length;
    badge.hidden = approvals.length === 0;
  } catch { /* shown on the page that needs it */ }
}

function header(title, lead) {
  return el("div", { class: "head" }, el("h1", {}, title), lead ? el("p", { class: "lead" }, lead) : null);
}

function errorBox(e) {
  return el("p", { class: "error", role: "alert" }, e.message || String(e));
}

// --- login -------------------------------------------------------------------

function login(message) {
  const input = el("input", { id: "token", type: "password", autocomplete: "off", required: true });
  const form = el("form", { class: "card pad" },
    el("label", { class: "field", for: "token" }, "Token API", input),
    el("button", { class: "btn pri", type: "submit" }, "Entra"));
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    setToken(input.value.trim());
    render();
  });
  app.replaceChildren(el("div", { class: "solo" },
    el("div", { class: "brand" },
      el("span", { class: "mark", "aria-hidden": "true" }, "E"),
      el("span", {}, el("strong", {}, "EuLLM Agent"), el("small", {}, "Core"))),
    el("p", { class: "lead" },
      "Apri il link che stampa ", el("code", {}, "eullm-agent start"),
      " oppure incolla un token API di questo Core."),
    message ? el("p", { class: "error", role: "alert" }, message) : null,
    form));
  input.focus();
}

// --- chat --------------------------------------------------------------------

const SESSION_RUNS = "eullm-agent-chat";

function chatRuns() {
  try { return JSON.parse(sessionStorage.getItem(SESSION_RUNS) || "[]"); } catch { return []; }
}

function addChatRun(id) {
  const ids = chatRuns().concat(id).slice(-30);
  try { sessionStorage.setItem(SESSION_RUNS, JSON.stringify(ids)); } catch { /* ignore */ }
}

function stepList(steps) {
  if (!steps || steps.length === 0) return null;
  return el("ul", { class: "steps" }, steps.map((s) => {
    if (s.kind === "llm_call") {
      return el("li", {},
        el("span", { class: "kind" }, "modello"),
        el("span", {}, s.provider + " · " + s.model),
        el("span", { class: "num" },
          (s.input_tokens ?? "?") + " → " + (s.output_tokens ?? "?") + " token · " + s.duration_ms + " ms"),
        s.error ? el("span", { class: "err" }, s.error) : null);
    }
    return el("li", {},
      el("span", { class: "kind" }, "strumento"),
      el("strong", {}, s.tool),
      el("span", { class: "chip" }, DECISIONS[s.decision] || s.decision),
      el("span", { class: "num" }, s.duration_ms + " ms"),
      s.decision_reason ? el("span", { class: "muted" }, s.decision_reason) : null,
      s.error ? el("span", { class: "err" }, s.error) : null);
  }));
}

function approvalBox(a, after) {
  const note = el("input", { type: "text", placeholder: "Nota (facoltativa)", "aria-label": "Nota" });
  const decide = async (decision, btns) => {
    btns.forEach((b) => { b.disabled = true; });
    try {
      await api("POST", "/v1/approvals/" + encodeURIComponent(a.id), { decision, note: note.value || null });
      if (after) after();
    } catch (e) {
      box.append(errorBox(e));
      btns.forEach((b) => { b.disabled = false; });
    }
  };
  const yes = el("button", { class: "btn ok sm", type: "button" }, "Approva");
  const no = el("button", { class: "btn no sm", type: "button" }, "Rifiuta");
  yes.addEventListener("click", () => decide("approve", [yes, no]));
  no.addEventListener("click", () => decide("deny", [yes, no]));
  const box = el("div", { class: "approve" },
    el("div", { class: "row" }, el("strong", {}, "L'agente chiede di usare " + a.tool), chip(a.status)),
    el("p", { class: "muted" }, a.reason),
    el("pre", {}, JSON.stringify(a.arguments, null, 2)),
    a.status === "pending" ? el("div", { class: "field" }, note) : null,
    a.status === "pending" ? el("div", { class: "row" }, yes, no) : null);
  return box;
}

async function runBubble(id) {
  const holder = el("div", { class: "msg" }, el("p", { class: "muted" }, "Caricamento…"));
  const me = el("div", { class: "msg me" });
  const wrap = el("div", { class: "thread" }, me, holder);
  let finished = false;
  const update = async () => {
    if (finished) return;
    let run;
    try { run = await api("GET", "/v1/runs/" + encodeURIComponent(id)); } catch (e) {
      holder.replaceChildren(errorBox(e));
      finished = e.status === 404;
      return;
    }
    me.replaceChildren(el("div", { class: "bubble" }, run.input),
      el("div", { class: "meta" }, when(run.created_at_ms) + " · profilo " + run.profile));
    const parts = [];
    if (run.status === "waiting_approval") {
      try {
        const { approvals } = await api("GET", "/v1/approvals?status=pending");
        approvals.filter((a) => a.run_id === id).forEach((a) => parts.push(approvalBox(a, update)));
      } catch { /* the run page shows it */ }
    }
    if (run.output) parts.push(el("div", { class: "bubble" }, run.output));
    if (run.error) parts.push(el("p", { class: "error" }, run.error));
    if (!done(run.status) && !run.output && run.status !== "waiting_approval") {
      parts.push(el("p", { class: "muted" }, "L'agente sta lavorando…"));
    }
    // Totals are written when the run ends; until then add up the steps.
    const used = done(run.status) ? run.input_tokens + run.output_tokens
      : run.steps.reduce((n, s) => n + (s.input_tokens || 0) + (s.output_tokens || 0), 0);
    const details = el("details", {}, el("summary", { class: "muted" },
      (run.steps.length === 1 ? "1 passo" : run.steps.length + " passi") + " · " + num(used) + " token"),
      stepList(run.steps));
    put(holder, ...parts,
      el("div", { class: "meta" }, chip(run.status), el("a", { href: "#/runs/" + id }, "Dettagli")),
      run.steps.length ? details : null);
    finished = done(run.status);
  };
  every(1500, update);
  return wrap;
}

async function chatPage() {
  const main = shell("chat", header("Chat", "Ogni messaggio è un compito per l'agente: lo esegue con i suoi strumenti e ti chiede il permesso prima delle azioni rischiose."));
  const thread = el("div", { class: "thread" });
  const input = el("textarea", { id: "task", required: true, placeholder: "Cosa deve fare l'agente?" });
  const select = el("select", { id: "profile", "aria-label": "Profilo" },
    profiles.map((p) => el("option", { value: p.name },
      p.name + (p.provider_model ? " · " + p.provider_model : ""))));
  const send = el("button", { class: "btn pri", type: "submit" }, "Invia");
  const status = el("div", {});
  const form = el("form", { class: "card pad compose" },
    el("label", { class: "field", for: "task" }, "Nuovo compito", input),
    el("div", { class: "row" },
      profiles.length > 1 ? el("label", { class: "field" }, "Profilo", select) : el("span", { class: "muted" },
        profiles[0] ? "Modello: " + (profiles[0].provider_model || profiles[0].model) : ""),
      send),
    status);
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) form.requestSubmit();
  });
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (!input.value.trim()) return;
    send.disabled = true;
    status.replaceChildren();
    try {
      const profile = profiles.length > 1 ? select.value : (profiles[0] ? profiles[0].name : "default");
      const { id } = await api("POST", "/v1/runs", { input: input.value, profile });
      addChatRun(id);
      input.value = "";
      thread.append(await runBubble(id));
      thread.lastChild.scrollIntoView({ behavior: "smooth", block: "end" });
    } catch (e) {
      status.replaceChildren(errorBox(e));
    } finally {
      send.disabled = false;
      input.focus();
    }
  });
  main.append(thread, form);
  for (const id of chatRuns()) thread.append(await runBubble(id));
  input.focus();
}

// --- runs --------------------------------------------------------------------

async function runsPage() {
  const main = shell("runs", header("Attività", "Gli ultimi compiti eseguiti da questo Core, da qualsiasi canale."));
  const card = el("div", { class: "card" }, el("p", { class: "empty" }, "Caricamento…"));
  main.append(card);
  every(5000, async () => {
    try {
      const { runs } = await api("GET", "/v1/runs?limit=100");
      if (runs.length === 0) {
        card.replaceChildren(el("p", { class: "empty" }, "Nessun compito ancora."));
        return;
      }
      card.replaceChildren(...runs.map((r) => el("div", { class: "item" },
        el("a", { class: "what", href: "#/runs/" + r.id },
          el("strong", {}, when(r.created_at_ms)),
          el("span", {}, "profilo " + r.profile + " · " + r.source)),
        chip(r.status))));
    } catch (e) {
      card.replaceChildren(errorBox(e));
    }
  });
}

async function runPage(id) {
  const main = shell("runs", el("a", { href: "#/runs" }, "← Attività"));
  const body = el("div", { class: "thread" }, el("p", { class: "muted" }, "Caricamento…"));
  main.append(body);
  let finished = false;
  every(2000, async () => {
    if (finished) return;
    let run;
    try { run = await api("GET", "/v1/runs/" + encodeURIComponent(id)); } catch (e) {
      body.replaceChildren(errorBox(e));
      finished = true;
      return;
    }
    const approvals = run.status === "waiting_approval"
      ? (await api("GET", "/v1/approvals?status=pending")).approvals.filter((a) => a.run_id === id)
      : [];
    put(body,
      header("Compito", null),
      el("div", { class: "card pad" },
        el("div", { class: "row" }, chip(run.status), run.tainted
          ? el("span", { class: "chip amber", title: "Ha letto contenuti esterni: le azioni con effetti chiedono approvazione" }, "contenuto esterno")
          : null),
        el("dl", { class: "kv" },
          el("dt", {}, "Iniziato"), el("dd", {}, when(run.created_at_ms)),
          el("dt", {}, "Finito"), el("dd", {}, when(run.finished_at_ms) || "—"),
          el("dt", {}, "Profilo"), el("dd", {}, run.profile),
          el("dt", {}, "Origine"), el("dd", {}, run.source),
          el("dt", {}, "Passi"), el("dd", {}, run.iterations),
          el("dt", {}, "Token"), el("dd", { class: "num" }, num(run.input_tokens) + " in · " + num(run.output_tokens) + " out"),
          el("dt", {}, "Costo"), el("dd", { class: "num" }, run.cost == null ? "—" : money(run.cost)))),
      el("div", { class: "card pad" }, el("h2", {}, "Richiesta"), el("pre", {}, run.input)),
      ...approvals.map((a) => approvalBox(a, () => { finished = false; })),
      run.output ? el("div", { class: "card pad" }, el("h2", {}, "Risultato"), el("pre", {}, run.output)) : null,
      run.error ? el("p", { class: "error" }, run.error) : null,
      el("div", { class: "card pad" }, el("h2", {}, "Passi"),
        stepList(run.steps) || el("p", { class: "muted" }, "Nessun passo registrato.")));
    finished = done(run.status);
  });
}

// --- approvals ---------------------------------------------------------------

async function approvalsPage() {
  const main = shell("approvals", header("Approvazioni",
    "Azioni che l'agente vuole fare e che la policy riserva a una persona. Senza risposta entro il tempo previsto valgono come rifiutate."));
  const pending = el("div", { class: "thread" });
  const recent = el("div", { class: "card" });
  main.append(el("h2", {}, "Da decidere"), pending, el("h2", {}, "Decise di recente"), recent);
  every(4000, async () => {
    try {
      const { approvals } = await api("GET", "/v1/approvals");
      const open = approvals.filter((a) => a.status === "pending");
      const closed = approvals.filter((a) => a.status !== "pending").slice(0, 30);
      // Keep a half-written note: redraw the pending list only when it changes.
      const key = open.map((a) => a.id).join(",");
      if (pending.dataset.key !== key) {
        pending.dataset.key = key;
        pending.replaceChildren(...(open.length
          ? open.map((a) => el("div", {}, approvalBox(a, null),
            el("a", { href: "#/runs/" + a.run_id, class: "muted" }, "Apri il compito")))
          : [el("p", { class: "muted" }, "Niente da decidere.")]));
      }
      recent.replaceChildren(...(closed.length
        ? closed.map((a) => el("div", { class: "item" },
          el("a", { class: "what", href: "#/runs/" + a.run_id },
            el("strong", {}, a.tool),
            el("span", {}, when(a.decided_at_ms || a.created_at_ms) + (a.decided_by ? " · " + a.decided_by : ""))),
          chip(a.status)))
        : [el("p", { class: "empty" }, "Nessuna decisione ancora.")]));
    } catch (e) {
      pending.replaceChildren(errorBox(e));
    }
  });
}

// --- usage -------------------------------------------------------------------

function stat(label, value, limit, fmt) {
  const f = fmt || num;
  const parts = [el("span", { class: "label" }, label), el("span", { class: "value" }, f(value))];
  if (limit != null) {
    const pct = limit > 0 ? Math.min(100, (value / limit) * 100) : 100;
    parts.push(el("div", { class: "bar" }, el("i", { class: pct >= 80 ? "warn" : null, style: null })),
      el("span", { class: "note" }, "limite " + f(limit)));
    parts[2].firstChild.style.width = pct.toFixed(1) + "%";
  }
  return el("div", { class: "card stat" }, ...parts);
}

async function usagePage() {
  const main = shell("usage", header("Consumi", "Oggi e questo mese (UTC), con i limiti del tenant quando sono impostati."));
  const body = el("div", { class: "thread" });
  main.append(body);
  every(10000, async () => {
    try {
      const u = await api("GET", "/v1/usage");
      const l = u.limits || {};
      body.replaceChildren(
        el("h2", {}, "Oggi"),
        el("div", { class: "stats" },
          stat("Compiti", u.day.runs, l.max_runs_per_day),
          stat("Chiamate al modello", u.day.llm_calls),
          stat("Token", u.day.input_tokens + u.day.output_tokens),
          stat("Pagine lette", u.day.fetches, l.max_fetches_per_day)),
        el("h2", {}, "Questo mese"),
        el("div", { class: "stats" },
          stat("Compiti", u.month.runs),
          stat("Token", u.month.input_tokens + u.month.output_tokens, l.max_tokens_per_month),
          stat("Costo", u.month.cost, l.max_cost_per_month, money),
          stat("Pagine lette", u.month.fetches)),
        el("p", { class: "muted" }, "Tenant: " + u.tenant));
    } catch (e) {
      body.replaceChildren(errorBox(e));
    }
  });
}

// --- router ------------------------------------------------------------------

async function render() {
  clearTimers();
  // `eullm-agent start` opens /#token=…: keep it for this tab, drop it from
  // the address bar and the history.
  if (location.hash.startsWith("#token=")) {
    setToken(decodeURIComponent(location.hash.slice(7)));
    history.replaceState(null, "", location.pathname + "#/chat");
  }
  if (!token()) return login();
  try {
    if (!health) health = await (await fetch("/v1/health", { cache: "no-store" })).json();
    profiles = (await api("GET", "/v1/profiles")).profiles;
  } catch (e) {
    if (e.status === 401) return; // the login form is already shown
    app.replaceChildren(el("div", { class: "solo" }, errorBox(e)));
    return;
  }
  const route = location.hash.replace(/^#\/?/, "").split("/");
  switch (route[0]) {
    case "runs": return route[1] ? runPage(route[1]) : runsPage();
    case "approvals": return approvalsPage();
    case "usage": return usagePage();
    default: return chatPage();
  }
}

window.addEventListener("hashchange", render);
render();
