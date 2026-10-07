/* FRIDAY command center. No framework, no inline script; every dynamic string goes through textContent. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const store = {
    get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
    set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* storage unavailable: the token lives in memory only */ } },
    del: (k) => { try { localStorage.removeItem(k); } catch { /* ignore */ } },
  };
  let token = null, role = "device", activeTab = "activity", timer = null, conv = null, busy = false;

  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") el.className = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined && v !== false) el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    return el;
  }
  const clear = (el) => { while (el.firstChild) el.removeChild(el.firstChild); return el; };

  async function api(path, opts = {}) {
    const r = await fetch(path, {
      method: opts.method || "GET",
      headers: { Authorization: "Bearer " + token, ...(opts.body ? { "Content-Type": "application/json" } : {}) },
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    if (r.status === 401) { lock("Token rejected."); throw new Error("unauthorized"); }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || r.statusText);
    return data;
  }

  // ------------------------------------------------------------ login
  function lock(msg) {
    token = null; store.del("friday_token"); clearInterval(timer);
    $("app").hidden = true; $("login").hidden = false; $("loginErr").textContent = msg || "";
  }
  async function connect(t) {
    token = t.trim();
    try {
      const s = await api("/v1/status");
      store.set("friday_token", token);
      role = s.role; $("login").hidden = true; $("app").hidden = false;
      $("device").textContent = s.device + " · " + s.role;
      await loadHistory(); await refresh(true);
      clearInterval(timer); timer = setInterval(() => refresh(false), 3000);
    } catch (e) { if (e.message !== "unauthorized") lock("Could not connect: " + e.message); }
  }
  $("loginForm").addEventListener("submit", (e) => { e.preventDefault(); connect($("tokenInput").value); });
  $("logout").addEventListener("click", () => lock(""));

  // ------------------------------------------------------------ chat
  function addMsg(role_, text, actions, pending) {
    const body = h("div", { class: "msg " + role_ }, h("span", { class: "who" }, role_ === "user" ? "you" : "friday"), text);
    if (actions && actions.length) {
      body.append(h("div", { class: "chips" }, actions.map((a) => {
        const cls = a.status === "ok" ? (a.verified ? "ok" : "info") : a.status === "approval_required" ? "wait" : a.status === "proposed" ? "info" : "bad";
        const label = a.tool + " · " + a.status + (a.status === "ok" ? (a.verified ? " ✓ verified" : a.verified === false ? " ✗" : "") : "");
        return h("span", { class: "chip " + cls, title: a.summary || "" }, label);
      })));
    }
    const log = $("log"); log.append(body); log.scrollTop = log.scrollHeight;
    return body;
  }
  async function loadHistory() {
    clear($("log"));
    try {
      const rows = await api("/v1/conversations/main/messages?limit=40");
      for (const m of rows) addMsg(m.role, m.content, m.meta && m.meta.actions);
    } catch { /* no history yet */ }
    if (!$("log").children.length) addMsg("assistant", "Good to see you. Ask for “executive briefing”, or tell me what to do.");
  }
  async function send(text) {
    if (!text.trim() || busy) return;
    busy = true; $("send").disabled = true;
    addMsg("user", text); const wait = addMsg("assistant", "…");
    try {
      const r = await api("/v1/chat", { method: "POST", body: { message: text, conversation_id: conv } });
      conv = r.conversation_id; wait.remove();
      addMsg("assistant", r.reply, r.actions, r.pending_approvals);
      if ($("speak").checked && "speechSynthesis" in window) speechSynthesis.speak(new SpeechSynthesisUtterance(r.reply.slice(0, 600)));
      refresh(false);
    } catch (e) { wait.remove(); addMsg("assistant", "Request failed: " + e.message); }
    busy = false; $("send").disabled = false; $("msg").focus();
  }
  $("chatForm").addEventListener("submit", (e) => { e.preventDefault(); const t = $("msg").value; $("msg").value = ""; send(t); });

  // voice: browser-native speech recognition (Chromium/Edge/Safari). Audio goes to the browser vendor's service, not to FRIDAY.
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) { $("mic").disabled = true; $("mic").title = "Speech recognition is not supported in this browser"; }
  else {
    const rec = new SR(); rec.lang = "en-US"; rec.interimResults = false;
    rec.onresult = (ev) => { const t = ev.results[0][0].transcript; $("msg").value = t; send(t); $("msg").value = ""; };
    rec.onend = () => $("mic").classList.remove("primary");
    rec.onerror = () => $("mic").classList.remove("primary");
    $("mic").addEventListener("click", () => { $("mic").classList.add("primary"); try { rec.start(); } catch { /* already listening */ } });
  }

  // ------------------------------------------------------------ side panels
  const when = (iso) => iso ? new Date(iso).toLocaleString([], { weekday: "short", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "";

  function renderApprovals(rows) {
    $("apCount").textContent = rows.length || ""; const box = clear($("approvals"));
    if (!rows.length) box.append(h("div", { class: "muted" }, "Nothing waiting."));
    for (const a of rows) {
      const cmd = a.tool === "shell_run" ? (a.display_args.command || "") : Object.entries(a.display_args).map(([k, v]) => k + "=" + String(v).slice(0, 80)).join(", ");
      box.append(h("div", { class: "item act" },
        h("div", { class: "row" }, h("b", {}, "#" + a.id + " " + a.tool), h("span", { class: "chip wait" }, a.risk.replace(/_/g, " "))),
        h("div", { class: "sub mono" }, cmd), h("div", { class: "sub" }, a.why_needed || ""),
        h("div", { class: "row" },
          h("button", { class: "btn small ok", onclick: () => decide(a.id, true, false) }, "Approve"),
          a.risk !== "mandatory_approval" ? h("button", { class: "btn small", title: "Pre-approve this exact action in future", onclick: () => decide(a.id, true, true) }, "Always") : null,
          h("button", { class: "btn small no", onclick: () => decide(a.id, false) }, "Deny"))));
    }
  }
  async function decide(id, approve, remember) {
    try {
      const r = await api("/v1/approvals/" + id + (approve ? "/approve" : "/deny"), { method: "POST", body: approve ? { remember: !!remember } : undefined });
      if (approve) addMsg("assistant", "Approved #" + id + ": " + r.result.summary + (r.result.verification ? "\nVerification: " + (r.result.verification.verified === true ? "✓ " : r.result.verification.verified === false ? "✗ " : "") + r.result.verification.method + " — " + r.result.verification.detail : ""), [{ tool: r.approval.tool, status: r.result.status, verified: r.result.verification && r.result.verification.verified, summary: r.result.summary }]);
      else addMsg("assistant", "Denied #" + id + ".");
    } catch (e) { addMsg("assistant", "Could not decide #" + id + ": " + e.message); }
    refresh(false);
  }

  function renderTasks(rows) {
    $("taskCount").textContent = rows.length || ""; const box = clear($("tasks"));
    if (!rows.length) box.append(h("div", { class: "muted" }, "No open tasks."));
    for (const t of rows.slice(0, 25)) {
      box.append(h("div", { class: "item" },
        h("div", { class: "row" }, h("span", {}, "#" + t.id + " " + t.title),
          h("button", { class: "btn small ghost", title: "Mark done", "aria-label": "Complete task " + t.id, onclick: async () => { await api("/v1/tasks/" + t.id + "/complete", { method: "POST" }); refresh(false); } }, "✓")),
        h("div", { class: "sub" }, [t.priority_name, t.project, t.deadline ? "due " + when(t.deadline) : null, t.status !== "todo" ? t.status : null].filter(Boolean).join(" · "))));
    }
  }
  $("taskForm").addEventListener("submit", async (e) => {
    e.preventDefault(); const v = $("taskTitle").value.trim(); if (!v) return; $("taskTitle").value = "";
    await api("/v1/tasks", { method: "POST", body: { title: v } }); refresh(false);
  });

  function renderNotes(rows) {
    const unread = rows.filter((n) => !n.read_at); $("noteCount").textContent = unread.length || "";
    const box = clear($("notes"));
    if (!unread.length) box.append(h("div", { class: "muted" }, "All clear."));
    for (const n of unread.slice(0, 12)) {
      box.append(h("div", { class: "item " + (n.priority === "CRITICAL" ? "crit" : n.priority === "ACTION_REQUIRED" ? "act" : "") },
        h("div", { class: "row" }, h("b", {}, n.title), h("span", { class: "chip" }, n.priority.replace("_", " ").toLowerCase())),
        n.body ? h("div", { class: "sub" }, n.body) : null));
    }
  }
  $("ackAll").addEventListener("click", async () => { await api("/v1/notifications/ack", { method: "POST", body: {} }); refresh(false); });

  // ------------------------------------------------------------ header
  function renderStatus(s) {
    const stopped = s.emergency_stop && s.emergency_stop.engaged;
    $("orb").className = "orb" + (stopped ? " stopped" : s.model.degraded ? " degraded" : "");
    $("brain").textContent = "brain: " + s.model.provider + (s.model.degraded ? " (offline rules)" : ":" + s.model.model);
    $("brain").title = s.model.reason || "Language model backend in use";
    $("autonomy").value = String(s.autonomy.level); $("autonomy").disabled = role !== "owner";
    $("estopBanner").hidden = !stopped; $("resume").hidden = role !== "owner";
  }
  $("autonomy").addEventListener("change", async (e) => {
    try { await api("/v1/security/autonomy", { method: "PUT", body: { level: Number(e.target.value) } }); } catch (err) { alert(err.message); }
    refresh(false);
  });
  $("estop").addEventListener("click", async () => { await api("/v1/security/estop", { method: "POST" }); addMsg("assistant", "Emergency stop engaged. All tool execution is disabled."); refresh(false); });
  $("resume").addEventListener("click", async () => { try { await api("/v1/security/resume", { method: "POST" }); } catch (err) { alert(err.message); } refresh(false); });
  $("briefBtn").addEventListener("click", () => send("executive briefing"));

  // ------------------------------------------------------------ tabs
  document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => {
    activeTab = b.dataset.tab;
    document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === b));
    document.querySelectorAll(".tabpane").forEach((p) => (p.hidden = p.id !== "tab-" + activeTab));
    refreshTab();
  }));
  const stClass = (s) => (s === "ok" ? "st-ok" : s === "approval_required" ? "st-wait" : s === "proposed" ? "st-info" : "st-bad");

  async function refreshTab() {
    const pane = $("tab-" + activeTab);
    if (activeTab === "activity") {
      const [rows, v] = await Promise.all([api("/v1/activity?limit=40"), api("/v1/activity/verify")]);
      clear(pane).append(h("div", { class: "muted mb" }, "Audit log hash-chain: " + (v.intact ? "intact ✓" : "BROKEN at row " + v.first_bad_id)),
        h("table", {}, h("thead", {}, h("tr", {}, ["Time", "Tool", "Status", "Verified", "What happened"].map((x) => h("th", {}, x)))),
          h("tbody", {}, rows.map((r) => h("tr", {}, h("td", { class: "n" }, when(r.ts)), h("td", { class: "mono" }, r.tool),
            h("td", { class: stClass(r.status) }, r.status), h("td", {}, r.verified === 1 ? "✓" : r.verified === 0 ? "✗" : "—"), h("td", {}, r.summary))))));
    } else if (activeTab === "memory") {
      const q = pane.dataset.q || "";
      const [rows, cats] = await Promise.all([api("/v1/memory?limit=60" + (q ? "&q=" + encodeURIComponent(q) : "")), api("/v1/memory-categories")]);
      clear(pane).append(
        h("div", { class: "toolbar" },
          h("input", { placeholder: "Search memory…", value: q, "aria-label": "Search memory", onkeydown: (e) => { if (e.key === "Enter") { pane.dataset.q = e.target.value; refreshTab(); } } }),
          h("a", { class: "btn", href: "#", onclick: async (e) => { e.preventDefault(); const d = await api("/v1/memory-export"); const a = h("a", { href: URL.createObjectURL(new Blob([JSON.stringify(d, null, 2)], { type: "application/json" })), download: "friday-memory.json" }); a.click(); } }, "Export JSON"),
          cats.map((c) => h("label", { class: "pill" }, h("input", { type: "checkbox", checked: c.enabled, disabled: role !== "owner", onchange: async (e) => { await api("/v1/memory-categories/" + c.category, { method: "PUT", body: { enabled: e.target.checked } }); refreshTab(); } }), " " + c.category + " (" + c.count + ")"))),
        h("table", {}, h("thead", {}, h("tr", {}, ["#", "Category", "Memory", ""].map((x) => h("th", {}, x)))),
          h("tbody", {}, rows.map((m) => h("tr", {}, h("td", {}, m.id), h("td", {}, m.category + (m.project ? " · " + m.project : "")), h("td", {}, m.content.slice(0, 400)),
            h("td", {}, h("button", { class: "btn small no", onclick: async () => { await api("/v1/memory/" + m.id, { method: "DELETE" }); refreshTab(); } }, "Forget")))))));
    } else if (activeTab === "business") {
      const [b, projects] = await Promise.all([api("/v1/briefing"), api("/v1/projects")]);
      clear(pane).append(
        h("div", { class: "kv" }, [["Open priorities", b.priorities.length], ["Due today", b.due_today.length], ["Overdue", b.overdue.length], ["Blocked", b.blocked.length], ["Done today", b.completed_today.length]].map(([k, v]) => h("div", {}, h("b", {}, v), h("span", {}, k)))),
        h("pre", { class: "brief" }, b.text),
        h("p", { class: "muted" }, "Projects: " + projects.map((p) => p.name).join(" · ")));
    } else if (activeTab === "devices") {
      const rows = await api("/v1/devices");
      clear(pane).append(
        role === "owner" ? h("form", { class: "toolbar", onsubmit: async (e) => { e.preventDefault(); const n = e.target.elements.n.value.trim(); if (!n) return; const k = e.target.elements.k.value; const r = await api("/v1/devices", { method: "POST", body: { name: n, kind: k } }); window.prompt("Token for " + r.name + " (shown once — copy it now):", r.token); refreshTab(); } },
          h("input", { name: "n", placeholder: "New device name (e.g. Android phone)" }), h("select", { name: "k", class: "grant" }, ["phone", "pc", "browser", "bot", "other"].map((o) => h("option", { value: o }, o))), h("button", { class: "btn" }, "Register device")) : null,
        h("table", {}, h("thead", {}, h("tr", {}, ["#", "Device", "Kind", "Role", "Last seen", "State", ""].map((x) => h("th", {}, x)))),
          h("tbody", {}, rows.map((d) => h("tr", {}, h("td", {}, d.id), h("td", {}, d.name), h("td", {}, d.kind), h("td", {}, d.role), h("td", {}, when(d.last_seen)), h("td", {}, d.revoked ? "revoked" : "active"),
            h("td", {}, role === "owner" && !d.revoked ? h("button", { class: "btn small no", onclick: async () => { try { await api("/v1/devices/" + d.id, { method: "DELETE" }); } catch (e) { alert(e.message); } refreshTab(); } }, "Revoke") : null))))));
    } else if (activeTab === "security") {
      const s = await api("/v1/security");
      clear(pane).append(
        h("p", { class: "muted" }, "Every capability is permission-based. “confirm” means FRIDAY must ask each time. Scopes marked reserved have no tool yet, but their safe default is already in force."),
        h("table", {}, h("thead", {}, h("tr", {}, ["Scope", "Grant", "What it covers", ""].map((x) => h("th", {}, x)))),
          h("tbody", {}, s.permissions.map((p) => h("tr", {}, h("td", { class: "mono" }, p.scope),
            h("td", {}, h("select", { class: "grant", disabled: role !== "owner", "aria-label": "Grant for " + p.scope, onchange: async (e) => { await api("/v1/security/permissions/" + p.scope, { method: "PUT", body: { grant: e.target.value } }); refreshTab(); } },
              ["allow", "confirm", "deny"].map((g) => h("option", { value: g, selected: g === p.grant }, g)))),
            h("td", {}, p.description), h("td", { class: "muted" }, p.implemented ? "" : "reserved"))))),
        s.standing_approvals.length ? h("p", {}, "Standing approvals: " + s.standing_approvals.length + " (exact actions you chose to always allow)") : null);
    }
  }

  async function refresh(first) {
    try {
      const [s, ap, tasks, notes] = await Promise.all([api("/v1/status"), api("/v1/approvals"), api("/v1/tasks?view=open"), api("/v1/notifications")]);
      renderStatus(s); renderApprovals(ap); renderTasks(tasks); renderNotes(notes);
      if (first || activeTab === "activity" || activeTab === "business") await refreshTab();
    } catch (e) { /* transient network error; next tick retries */ }
  }

  // ------------------------------------------------------------ boot
  const frag = new URLSearchParams(location.hash.slice(1));
  const fromHash = frag.get("token");
  if (fromHash) history.replaceState(null, "", location.pathname + location.search);  // never leave the token in the URL
  const saved = fromHash || store.get("friday_token");
  if (saved) connect(saved); else lock("");
})();
