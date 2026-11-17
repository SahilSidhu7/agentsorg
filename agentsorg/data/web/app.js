// agents.org dashboard — vanilla JS, no build step.
const qs = new URLSearchParams(location.search);
let TOKEN = qs.get("token") || "";
try { if (TOKEN) sessionStorage.setItem("aotok", TOKEN); else TOKEN = sessionStorage.getItem("aotok") || ""; } catch (e) {}
if (qs.get("token")) history.replaceState(null, "", location.pathname + location.hash);

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n) => (n || 0).toLocaleString();
const ago = (ts) => { const s = Date.now() / 1000 - ts; return s < 60 ? `${s | 0}s` : s < 3600 ? `${(s / 60) | 0}m` : s < 86400 ? `${(s / 3600) | 0}h` : `${(s / 86400) | 0}d`; };
const mBadge = (m) => { const k = /haiku/.test(m) ? "H" : /sonnet/.test(m) ? "S" : /opus/.test(m) ? "O" : "Z"; return `<span class="m ${k}">${k === "Z" ? "–" : k}</span>`; };

async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { "Content-Type": "application/json", "X-Token": TOKEN, ...(opts.headers || {}) },
    body: opts.body ? JSON.stringify(opts.body) : undefined });
  if (r.status === 401) { banner("Bad or missing token. Open the URL printed by `agentsorg start`."); throw new Error("401"); }
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}
function banner(msg) { const b = $("#banner"); b.textContent = msg || ""; b.classList.toggle("hide", !msg); }

let S = null;          // /api/state
let view = "today", viewArg = null;
// deep links: #clients, #project/12, #ask/<question> (opens Ask and asks it)
const HASH = decodeURIComponent(location.hash.slice(1)).split("/");
if (HASH[0] && HASH[0] !== "ask") { view = HASH[0]; viewArg = HASH.slice(1).join("/") || null; }
const errText = (err) => { try { return JSON.parse(err.message).detail || err.message; } catch (e) { return err.message; } };
const dname = (id) => S.depts.find((d) => d.id === id)?.name || id;
// pages that live under Company (the sidebar shows four places only)
const COMPANY_TABS = [["company", "Business"], ["teams", "Teams"], ["projects", "History"], ["files", "Files"], ["keys", "Keys"],
  ["log", "Activity log"], ["kb", "Knowledge"], ["usage", "Usage"], ["office", "Office"], ["services", "Services"], ["settings", "Settings"]];
const UNDER_COMPANY = new Set([...COMPANY_TABS.map(([v]) => v), "dept", "newdept", "deliverables"]);
const tabs = (cur) => `<div class="row tabs" style="margin:-4px 0 14px">${COMPANY_TABS.map(([v, l]) => `<button class="${v === cur ? "" : "ghost "}sm" data-view="${v}">${l}</button>`).join("")}</div>`;
const STATUS = { planning: "planning", waiting: "waiting for plan usage", awaiting: "waiting for your OK", meeting: "in the conference room", running: "in progress",
  combining: "putting it together", review: "ready for review", done: "done", failed: "failed", cancelled: "cancelled" };

async function loadState() {
  S = await api("/api/state");
  $("#company").textContent = S.company;
  const b = S.budget, w = b.windows || {};
  const week = w.seven_day?.utilization, now5 = w.five_hour?.utilization;
  const meter = (label, pct, cap, tip) => `<div class="meter" title="${esc(tip)}">${label} <b>${pct == null ? "?" : pct.toFixed(0) + "%"}</b>
    <div class="bar"><div class="fill" style="width:${Math.min(pct || 0, 100)}%"></div>${cap ? `<div class="cap" style="left:${cap}%" title="Auto stops here"></div>` : ""}</div></div>`;
  $("#meters").innerHTML = (Object.keys(w).length
    ? meter("Plan usage this week", week, null, `Your Claude plan's weekly limit. Commands run up to ${b.manual_max_percent}%; your schedules stop at ${b.auto_max_percent}%.`)
      + meter("last 5 hours", now5, null, "Claude's short-term limit")
      + (b.quota_stale ? `<div class="meter" title="${esc(b.quota_reason)}">usage reading <b>${ago(Date.now() / 1000 - b.quota_age)} old</b></div>` : "")
    : `<div class="meter" title="${esc(b.quota_reason)}">Plan usage <b>not read yet</b><div class="hint">${esc(b.quota_reason || "counting calls instead")}</div></div>`)
    + `<div class="meter" title="model calls today">calls today <b>${b.calls_today}</b></div>`
    + (S.running ? `<div class="meter">working on <b>${S.running}</b> task(s)</div>` : "")
    + (S.waiting_budget ? `<div class="meter" title="They start by themselves when plan usage frees up">waiting for plan usage <b>${S.waiting_budget}</b></div>` : "");
  const pill = (sel, n) => { const x = $(sel); if (x) { x.textContent = n; x.classList.toggle("hide", !n); } };
  pill("#ibcount", S.inbox);
  $("#pauseB").textContent = b.paused ? "Resume" : "Pause all";
  $("#pauseB").classList.toggle("danger", b.paused);
  $("#hb").textContent = "heartbeat: " + S.heartbeat;
  const ob = $("#obcount"); ob.textContent = S.outbox_pending; ob.classList.toggle("hide", !S.outbox_pending);
  banner(!S.cli ? "claude CLI not found — install Claude Code or set claude_bin in company.yaml." : b.paused ? "Paused: no agent will run until you resume." : "");
  const navView = view === "project" || view === "inbox" ? "today" : UNDER_COMPANY.has(view) ? "company" : view;
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("on", a.dataset.view === navView));
}

function taskRow(t) {
  return `<div class="t" data-task="${t.id}">
    <span class="id">#${t.id}</span>
    <span class="who">${esc(t.dept)}/${esc(t.member)}</span>
    <span class="what">${esc(t.title)}${t.done_line || t.error ? `<small>${esc(t.done_line || t.error)}</small>` : ""}</span>
    <span class="tok">${t.model ? mBadge(t.model) : ""}${t.tokens ? fmt(t.tokens) : ""}</span>
    <span class="st ${t.status}">${t.status.replace("_", " ")}</span></div>`;
}

// what the owner can switch on per department; each one adds tool tokens to that department's calls
const ABIL = {
  read: ["read files", "Read the company's files and its linked folders."],
  edit: ["edit files", "Create and change files inside its own department folder."],
  web: ["web research", "Search the web and read pages. Costs more tokens per task: switch on for research-heavy departments."],
  shell: ["shell + keys", "Run shell commands, so it can call APIs with the keys you give it. Only for departments you trust with that."],
};

function commandBox() {
  return `<div class="card cmdbox">
    <textarea id="ceoT" placeholder="Tell the company what to do, e.g. Find 20 clinics in Leeds that still book by phone and draft a first email to each"></textarea>
    <div class="row"><button id="ceoGo">Send to the company</button>
      <span class="hint">One thing at a time works best · @sales … goes straight to one team · !opus uses the strongest model</span></div></div>`;
}

async function openChange(id) {
  const c = await api(`/api/changes/${id}`);
  $("#mbody").innerHTML = `<h1 style="margin-top:0">Changes to ${esc(c.source)}</h1>
    <p class="sub">Branch <code>${esc(c.branch)}</code> · ${c.files} file(s) · made for project #${c.project_id}. Nothing is pushed anywhere.</p>
    <pre class="out">${esc(c.stat)}</pre>
    <p class="hint">${c.status === "ready" ? "Accepting the result merges this branch; discarding the result throws it away." : esc(c.status)}</p>
    <h2>Diff</h2><pre class="out" style="max-height:60vh">${esc(c.diff)}${c.truncated ? "\n…(cut; see the branch for the rest)" : ""}</pre>`;
  $("#modal").classList.remove("hide");
}

async function openClient(id) {
  const r = await api(`/api/clients/${id}`), c = r.contact;
  const STAGES = ["lead", "contacted", "replied", "qualified", "proposal", "won", "lost", "churned"];
  const KIND = { email_out: "you wrote", email_in: "they wrote", stage: "moved", payment: "paid", note: "note", event: "event" };
  $("#mbody").innerHTML = `<h1 style="margin-top:0">${esc(c.name || c.email)}</h1>
    <p class="sub">${esc(c.email)}${c.org ? " · " + esc(c.org) : ""} · from ${esc(c.source || "?")}${c.trust === "untrusted" ? " · <b>unverified</b>: found by an agent on the web, check before writing" : ""}</p>
    <div class="row"><select id="ctStage">${STAGES.map((s) => `<option ${s === c.stage ? "selected" : ""}>${s}</option>`).join("")}</select>
      <input id="ctStep" value="${esc(c.next_step || "")}" placeholder="next step" style="max-width:260px"><input id="ctDate" type="date" value="${esc(c.next_date || "")}" style="max-width:150px"></div>
    <div class="row"><input id="ctNote" placeholder="add a note (a call, a meeting, what they said)" style="max-width:520px"><button id="ctSave" data-cid="${c.id}">Save</button></div>
    <div class="row"><input id="ctAmt" type="number" step="0.01" placeholder="amount" style="max-width:110px"><input id="ctCur" placeholder="EUR" style="max-width:70px">
      <select id="ctKind"><option>one-off</option><option>recurring</option><option>refund</option></select><button class="ghost" id="ctPay" data-cid="${c.id}">Record payment</button></div>
    <h2>History</h2><div class="card">${r.timeline.map((i) => `<div class="dr-task"><span><b>${esc(KIND[i.kind] || i.kind)}</b> <span style="white-space:pre-wrap">${esc(i.summary)}</span></span><span class="hint">${ago(i.at)} ago${i.task_id ? ` · <a data-task="${i.task_id}">task</a>` : ""}</span></div>`).join("") || `<span class="hint">Nothing yet.</span>`}</div>
    ${r.revenue.length ? `<h2>Payments</h2><div class="card">${r.revenue.map((x) => `<div class="dr-task"><span>${(+x.amount).toLocaleString()} ${esc(x.currency || "")} · ${esc(x.kind)}</span><span class="hint">${ago(x.at)} ago</span></div>`).join("")}</div>` : ""}
    <div class="row"><button class="ghost danger" id="ctDel" data-cid="${c.id}">Delete client</button></div>`;
  $("#modal").classList.remove("hide");
}

function download(name, text) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type: "text/markdown" }));
  a.download = (name || "deliverable").replace(/[^\w.-]+/g, "-").slice(0, 60) + ".md";
  a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

// ---------- views ----------
const V = {
  async office() { return Office.html(); },
  async ceo() { return V.company(); },
  async clients() {
    const r = await api("/api/clients" + (viewArg ? `?stage=${encodeURIComponent(viewArg)}` : ""));
    const n = r.numbers || {}, due = new Set(r.due);
    const money = (v) => (v == null ? "0" : (+v).toLocaleString(undefined, { maximumFractionDigits: 2 }));
    const strip = `<div class="pipe">${r.stages.map((s) => `<a class="pstage ${viewArg === s ? "on" : ""}" data-view="clients" data-arg="${viewArg === s ? "" : s}">
      <b>${r.counts[s] || 0}</b><span>${esc(s)}</span></a>`).join("")}</div>`;
    const rows = r.contacts.map((c) => `<tr data-client="${c.id}" style="cursor:pointer"><td><b>${esc(c.name || c.email)}</b>${c.org ? `<div class="hint">${esc(c.org)}</div>` : ""}</td>
      <td class="hint">${esc(c.email)}</td><td><span class="st ${c.stage === "won" ? "done" : ["lost", "churned"].includes(c.stage) ? "failed" : "running"}">${esc(c.stage)}</span>${c.trust === "untrusted" ? ' <span class="chip" title="found by an agent with web access">unverified</span>' : ""}</td>
      <td class="hint">${esc(c.next_step || "")}${c.next_date ? `<div class="${due.has(c.id) ? "due" : ""}">${esc(c.next_date)}</div>` : ""}</td><td class="hint">${esc(c.source || "")}</td></tr>`).join("");
    return `<h1>Clients</h1><p class="sub">Everyone the company sells to, where they stand, and the money that came in. Agents add people they find and move them along; every email still waits for your Send.</p>
      <div class="metrics"><div class="metric"><span>leads in the pipeline</span><b>${n.leads ?? 0}</b></div><div class="metric"><span>reply rate</span><b>${n.reply_rate ?? 0}%</b></div>
        <div class="metric"><span>customers</span><b>${n.customers ?? 0}</b></div><div class="metric"><span>revenue this month</span><b>${money(n.revenue_month)}</b></div>
        <div class="metric"><span>recurring, last 31 days</span><b>${money(n.mrr)}</b></div></div>
      ${strip}
      <div class="card" style="margin-top:14px">${rows ? `<table class="svc"><tr><th>who</th><th>email</th><th>stage</th><th>next</th><th>source</th></tr>${rows}</table>`
        : `<span class="hint">${viewArg ? "Nobody at this stage." : "No clients yet. Add one below, import your list, or ask the company to find leads."}</span>`}</div>
      <h2>Add or import</h2><div class="card"><div class="row"><input id="clEmail" placeholder="email" style="max-width:220px"><input id="clName" placeholder="name" style="max-width:160px">
        <input id="clOrg" placeholder="organisation" style="max-width:180px"><select id="clStage">${r.stages.map((s) => `<option>${s}</option>`).join("")}</select><button class="ghost" id="clAdd">Add</button></div>
        <p class="hint">Moving a business in? Paste a CSV with a header row (name, email, company, status, value). A plain list of email addresses works too.</p>
        <textarea id="clCsv" placeholder="name,email,company,status,value" style="min-height:70px"></textarea><div class="row"><button class="ghost" id="clImport">Import</button><span class="hint" id="clErr"></span></div></div>
      ${r.revenue.length ? `<h2>Money in</h2><div class="card">${r.revenue.map((x) => `<div class="dr-task"><span><b>${money(x.amount)} ${esc(x.currency || "")}</b> ${esc(x.kind)} · ${esc(x.name || x.email || "no client")}</span><span class="hint">${esc(x.source)} · ${ago(x.at)} ago</span></div>`).join("")}</div>` : ""}`;
  },
  async today() {
    if (!S.designed) return `<h1>Set up your company</h1><p class="sub">Describe what you're building. Claude designs the teams for it; you edit or accept.</p>
      <div class="row"><button data-view="office">Start the setup</button></div>`;
    const [r, ps] = await Promise.all([api("/api/inbox"), api("/api/projects?limit=12")]);
    const btn = (a) => `<button class="${a.primary ? "" : "ghost "}sm" data-ia="${esc(a.do)}" data-iid="${esc(a.id ?? "")}" data-iview="${esc(a.view || "")}" data-iarg="${esc(a.arg || "")}" data-iact="${esc(a.act || "")}">${esc(a.label)}</button>`;
    const LABEL = { result: "Review", send: "Send", question: "Answer", problem: "Fix" };
    const card = (i) => `<div class="ib"><span class="u u${i.urgency}"></span>
        <div><span class="chip">${LABEL[i.kind] || i.kind}</span> <b>${esc(i.title)}</b>
          ${i.kind === "question" ? `<ul class="hint" style="margin:4px 0 6px 18px">${(i.asks || []).map((a) => `<li>${esc(a)}</li>`).join("")}</ul>
            <div class="row"><input id="dc_${esc(i.id)}" placeholder="your answer: saved, never asked again" style="flex:1;min-width:220px;max-width:560px"></div>` : `<span class="hint">${esc(i.detail)}</span>`}</div>
        <div class="acts">${i.actions.map(btn).join("")}</div></div>`;
    const more = S.inbox - r.items.length;
    const running = ps.filter((p) => ["planning", "running", "combining", "waiting", "awaiting", "meeting"].includes(p.status));
    const recent = ps.filter((p) => ["done", "review"].includes(p.status)).slice(0, 5);
    const pRow = (p) => `<div class="dr-task" data-project="${p.id}" style="cursor:pointer"><span><b>${esc(p.title)}</b>
        <div class="hint">${p.tasks_done}/${p.tasks} steps · ${ago(p.updated)} ago</div></span><span class="st ${p.status}">${esc(STATUS[p.status] || p.status)}</span></div>`;
    return `<h1>Today</h1><p class="sub">${esc(S.about || "Tell the company what to do. It plans, does the work, and brings you one result.")}</p>
      ${commandBox()}
      <h2>Needs you</h2>${r.items.map(card).join("") || `<div class="card"><b>Nothing needs you.</b><p class="hint">Give the company its next job above.</p></div>`}
      ${more > 0 ? `<p class="hint">${more} more after these. Results you don't open close by themselves after 7 days.</p>` : ""}
      ${running.length ? `<h2>Working on</h2><div class="card">${running.map(pRow).join("")}</div>` : ""}
      ${recent.length ? `<h2>Recent results</h2><div class="card">${recent.map(pRow).join("")}</div>` : ""}`;
  },
  async inbox() { return V.today(); },
  async projects() {
    const ps = await api("/api/projects?limit=60");
    return `<h1>Company</h1>${tabs("projects")}<p class="sub">Every command you gave and what came of it.</p>
      <div class="card">${ps.map((p) => `<div class="dr-task" data-project="${p.id}" style="cursor:pointer"><span><b>#${p.id}</b> ${esc(p.title)}
        <div class="hint">${p.tasks_done}/${p.tasks} steps · ${ago(p.updated)} ago${p.final ? " · accepted" : ""}</div></span>
        <span class="st ${p.status}">${esc(STATUS[p.status] || p.status)}</span></div>`).join("") || `<span class="hint">No commands yet.</span>`}</div>`;
  },
  async teams() {
    const ABN = { read: "reads files", edit: "edits files", web: "web", shell: "shell + keys" };
    return `<h1>Company</h1>${tabs("teams")}<p class="sub">The teams Claude designed for your business. Open one to change its people, rules and abilities.</p>
      <div class="grid">${S.depts.map((d) => `<div class="card" data-view="dept" data-arg="${d.id}" style="cursor:pointer"><div class="rep-h"><span><b>${esc(d.name)}</b></span>
        <span class="hint">${d.members.length} member(s)</span></div><p class="hint" style="margin:4px 0">${esc(d.mission)}</p>
        <div class="hint">${(d.functions || []).map(esc).join(", ")}${d.abilities.length ? " · " + d.abilities.map((a) => ABN[a] || a).join(", ") : " · text only"}</div></div>`).join("")}</div>
      <div class="row"><button class="ghost" data-view="newdept">+ Add a team</button></div>`;
  },
  async company() {
    const [g, rt, biz] = await Promise.all([api("/api/goals"), api("/api/routines"), api("/api/business")]);
    const num = (v) => (v == null ? "?" : (+v).toLocaleString());
    const b = biz.business || {};
    const bf = (k, label, ph) => `<label for="bz_${k}">${label}</label><input id="bz_${k}" value="${esc(b[k] || "")}" placeholder="${esc(ph)}">`;
    const business = `<div class="card" style="margin-bottom:14px"><div class="rep-h"><span>The business</span><span class="hint">every agent works from this; unknown is better than a guess</span></div>
      <div class="form-grid" style="margin-top:8px">
        ${bf("customer", "Customer", "who pays, and the problem you solve for them")}
        ${bf("offer", "Offer", "what you sell")}
        ${bf("pricing", "Pricing", "leave empty until decided: agents then never quote a price")}
        <label for="bz_stage">Stage</label><select id="bz_stage"><option value="">not set</option>${biz.stages.map((s) => `<option value="${s}" ${b.stage === s ? "selected" : ""}>${esc(biz.stage_names[s])}</option>`).join("")}</select>
        ${bf("channels", "Channels", "how customers find you: cold email, referrals, search…")}
        ${bf("target_90d", "In 90 days", "one number, e.g. 10 paying clinics")}
        ${bf("constraints", "Constraints", "what the company must never do or promise")}
      </div><div class="row"><button id="bzSave">Save</button><span class="hint" id="bzErr"></span></div></div>`;
    const obs = (g.objectives || []).filter((o) => o.metric);
    const goals = `<div class="card" style="margin-bottom:14px"><div class="rep-h"><span>Goals</span><span class="hint">every agent works toward these</span></div>
      ${obs.map((o) => `<div class="metric"><span>${esc(o.text)}${o.due ? ` · by ${esc(o.due)}` : ""}</span>
        <b>${num(o.value)}</b><span class="hint"> / ${num(o.target)} ${esc(o.metric.replace(/_/g, " "))}</span>${o.percent != null ? `<div class="bar"><div class="fill" style="width:${o.percent}%"></div></div>` : ""}</div>`).join("")}
      <textarea id="goalsT" placeholder="One goal per line, e.g.&#10;10 paying clinics by 31 Dec" style="min-height:70px">${esc(g.goals.join("\n"))}</textarea>
      <div class="row"><button class="ghost sm" id="goalsSave">Save goals</button></div></div>`;
    const routines = `<div class="card"><div class="rep-h"><span>Schedules</span><span class="hint">the only work that runs without a fresh command: only what you add here</span></div>
      ${rt.routines.length ? `<table class="svc"><tr><th>when</th><th>team</th><th>task</th><th>next / last</th><th></th></tr>${rt.routines.map((r) => `<tr>
        <td><b>${esc(r.every)}</b></td><td>${esc(dname(r.dept))}</td><td>${esc(r.text)}</td>
        <td class="hint">${r.enabled ? "next " + esc(r.next) : "paused"}${r.last_task ? `<br><a data-task="${r.last_task.id}">last: #${r.last_task.id} ${esc(r.last_task.status)}</a>` : ""}</td>
        <td class="svc-act"><button class="ghost sm" data-rtedit="${r.id}" data-every="${esc(r.every)}" data-text="${esc(r.text)}">edit</button><button class="ghost sm" data-rtrun="${r.id}">run now</button><button class="ghost sm" data-rttoggle="${r.id}" data-on="${r.enabled ? 1 : 0}">${r.enabled ? "pause" : "resume"}</button><button class="ghost sm" data-rtdel="${r.id}">×</button></td></tr>`).join("")}</table>` : `<p class="hint">None. Add one only for work you want repeated, e.g. a Monday pipeline review.</p>`}
      <div class="row"><input id="rtEvery" placeholder="mon 09:00" style="max-width:150px" title="hourly · daily 09:00 · weekdays 09:00 · mon 09:00 · mon,thu 17:30">
        <select id="rtDept">${S.depts.map((d) => `<option value="${d.id}">${esc(d.name)}</option>`).join("")}</select>
        <input id="rtText" placeholder="e.g. Review last week's replies and list the 3 best next leads" style="max-width:480px">
        <button class="ghost" id="rtAdd">Add schedule</button></div><p class="hint" id="rtErr"></p></div>`;
    return `<h1>${esc(S.company)}</h1>${tabs("company")}${business}${goals}${routines}`;
  },
  async project(id) {
    const p = await api(`/api/projects/${id}`);
    const plan = p.tasks.length ? `<div class="steps">${p.tasks.map((t, i) => `<div class="step" data-task="${t.id}" style="cursor:pointer"><span class="n">${i + 1}</span>
        <span><b>${esc(dname(t.dept))}</b> ${esc(t.title)}${t.kind === "question" ? ' <span class="chip">question</span>' : ""}<div class="hint">${esc(t.done_line || t.error || "")}</div></span>
        <span class="st ${t.status}">${esc(t.status)}</span></div>`).join("")}</div>` : "";
    const acts = [];
    const code = (p.changes || []).filter((c) => c.status === "ready").length;
    if (p.status === "awaiting") acts.push(`<button data-pact2="approve" data-pid="${p.id}">Start it</button>`);
    if (p.status === "failed" && !p.tasks.length) acts.push(`<button data-pact2="retry" data-pid="${p.id}">Try again</button>`);
    if (p.status === "review") acts.push(`<button data-pact2="accept" data-pid="${p.id}">${code ? "Accept and merge the code" : "Accept"}</button>`);
    if (["review", "done"].includes(p.status) && p.tasks.length) acts.push(`<button class="ghost" data-revise="${p.id}">Redo with a note</button>`);
    if (p.status === "review") acts.push(`<button class="ghost" data-pact2="discard" data-pid="${p.id}">Discard</button>`);
    if (!["done", "cancelled", "failed", "review"].includes(p.status)) acts.push(`<button class="ghost" data-pact2="cancel" data-pid="${p.id}">Stop</button>`);
    if (p.status === "review" || p.status === "done") acts.push(`<button class="ghost" data-dl="${esc(p.title)}" data-dltext="${esc(p.result || "")}">Download .md</button>`);
    return `<p><a data-view="today">← Today</a></p><h1>${esc(p.title)}</h1>
      <p class="sub"><span class="st ${p.status}">${esc(STATUS[p.status] || p.status)}</span> · ${fmt(p.tokens_used)} tokens so far${p.final ? " · accepted as final" : ""}</p>
      ${p.answer ? `<div class="card"><b>Planner:</b> ${esc(p.answer)}</div>` : ""}
      ${p.error ? `<div class="card needs">${esc(p.error)}</div>` : ""}
      <div class="row">${acts.join("")}</div>
      <div id="reviseBox"></div>
      ${p.review && p.review.requirements.length ? `<h2>Delivered vs asked</h2><div class="card"><table><tr><th>requirement</th><th>status</th><th>evidence</th></tr>${p.review.requirements.map((r) => `<tr><td>${esc(r.need)}</td><td><span class="st ${r.status === "met" ? "done" : r.status === "partial" ? "backlog" : "failed"}">${esc(r.status)}</span></td><td class="hint">${esc(r.evidence)}</td></tr>`).join("")}</table></div>` : ""}
      ${(p.changes || []).filter((c) => c.status !== "discarded").length ? `<h2>Changes to your code</h2><div class="card">${p.changes.filter((c) => c.status !== "discarded").map((c) => `<div class="dr-task"><span><b>${esc(c.source)}</b> on <code>${esc(c.branch)}</code> · ${c.files || 0} file(s)</span><span><span class="st ${c.status === "ready" ? "review" : c.status === "merged" ? "done" : "running"}">${esc(c.status === "open" ? "in progress" : c.status)}</span>${c.status === "ready" ? ` <button class="sm" data-change="${c.id}">See the diff</button>` : ""}</span></div>`).join("")}</div>` : ""}
      ${p.result ? `<h2>Result</h2><div class="card"><div class="out md">${md(p.result)}</div></div>` : ""}
      ${(p.suggestions || []).filter((x) => x.state === "open" && x.kind === "question").length ? `<h2>Questions for you</h2><div class="card">${p.suggestions.filter((x) => x.state === "open" && x.kind === "question").map((x) => `<div class="dr-task"><span>${x.kind === "question" ? '<span class="chip">needs you</span> ' : ""}<b>${esc(dname(x.dept))}</b> ${esc(x.text)}
          ${x.kind === "question" ? `<div class="row"><input id="qa_${x.id}" placeholder="your answer" style="max-width:420px"><label class="hint"><input type="checkbox" id="qs_${x.id}" checked> remember it for the whole company</label>
            <button class="sm" data-qans="${x.id}" data-pid="${p.id}">Answer and continue</button></div>` : ""}</span>
          <span><button class="ghost sm" data-sugg="${x.id}" data-pid="${p.id}" data-sact="dismiss">Dismiss</button></span></div>`).join("")}</div>` : ""}
      ${(p.suggestions || []).filter((x) => x.state === "answered").length ? `<h2>You answered</h2><div class="card">${p.suggestions.filter((x) => x.state === "answered").map((x) => `<div class="dr-task"><span>${esc(x.text.replace(/\s*\(Context:.*$/s, ""))}<div class="hint">→ ${esc(x.answer)}</div></span></div>`).join("")}</div>` : ""}
      ${plan ? `<h2>Plan</h2>${plan}` : ""}
      ${p.messages.length ? `<h2>Questions between teams</h2><div class="card">${p.messages.map((m) => `<div class="dr-task"><span><b>${esc(dname(m.from_dept))} → ${esc(dname(m.to_dept))}</b> ${esc(m.question)}</span>${m.answer_task ? `<a data-task="${m.answer_task}">answer</a>` : ""}</div>`).join("")}</div>` : ""}
      ${p.board ? `<h2>Board</h2><p class="hint">The shared page every task in this project reads.</p><pre class="out">${esc(p.board)}</pre>` : ""}
      <p class="hint" style="margin-top:14px">Command: ${esc(p.command)}</p>`;
  },
  async deliverables() {
    const q = viewArg || "";
    const r = await api("/api/deliverables" + (q ? `?q=${encodeURIComponent(q)}` : ""));
    return `<h1>Deliverables</h1><p class="sub">Every finished piece of work in one place. Mark the ones you use as final; agents' later work builds on them.</p>
      <div class="row"><input id="dvQ" placeholder="search titles and text…" value="${esc(q)}" style="max-width:340px"><button class="ghost" id="dvS">Search</button></div>
      ${r.projects.length ? `<h2>Project results</h2><div class="card">${r.projects.map((p) => `<div class="dr-task" data-project="${p.id}" style="cursor:pointer"><span><b>${esc(p.title)}</b></span><span class="hint">${p.final ? "final · " : ""}${ago(p.updated)} ago</span></div>`).join("")}</div>` : ""}
      <h2>Pieces of work</h2><div class="card">${r.items.map((t) => `<div class="dr-task"><span><a data-task="${t.id}"><b>${esc(t.title)}</b></a>
          <div class="hint">${esc(dname(t.dept))}${t.project ? ` · <a data-project="${t.project_id}">${esc(t.project)}</a>` : ""} · ${ago(t.updated)} ago${t.summary ? " · " + esc(t.summary.split("\n")[0]).slice(0, 120) : ""}</div></span>
          <span class="acts"><button class="${t.final ? "" : "ghost "}sm" data-final="${t.id}" data-on="${t.final ? 1 : 0}">${t.final ? "final ✓" : "mark final"}</button><button class="ghost sm" data-toob="${t.id}" title="put it in the Outbox to send">to Outbox</button></span></div>`).join("")
        || `<span class="hint">Nothing finished yet.</span>`}</div>`;
  },
  async settings() {
    const [c, ctx] = await Promise.all([api("/api/company/settings"), api("/api/context")]);
    const days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
    const f = (k, label, input) => `<label for="cs_${k}">${label}</label>${input}`;
    const num = (k, min, max, hint) => f(k, hint, `<input id="cs_${k}" type="number" min="${min}" max="${max}" value="${c[k] ?? ""}">`);
    return `<h1>Company</h1>${tabs("settings")}<p class="sub">How the company runs. Changes apply at once and are saved in company.yaml.</p>
      <div class="card"><b>Company</b><div class="form-grid" style="margin-top:10px">
        ${f("name", "Name", `<input id="cs_name" value="${esc(c.name)}">`)}
        ${f("about", "What it does", `<input id="cs_about" value="${esc(c.about)}" style="max-width:520px">`)}
        ${f("quiet_hours", "Quiet hours (no schedules)", `<input id="cs_quiet_hours" value="${esc((c.quiet_hours || []).join(", "))}" placeholder="22:00-07:00">`)}
        ${num("auto_max_percent", 0, 90, "Schedules stop at (% of plan)")}
        ${num("manual_max_percent", 10, 100, "Your commands stop at (%)")}
        ${num("concurrency", 1, 6, "Tasks at the same time")}
        ${num("task_timeout", 60, 3600, "Stop a task after (seconds)")}
        ${num("keep_days", 14, 3650, "Keep old work for (days)")}
      </div><div class="row"><button id="csSave">Save settings</button><span class="hint" id="csErr"></span></div></div>
      <div class="card" style="margin-top:14px"><b>Project context</b>
        <p class="hint">${ctx.exists ? `Read ${ctx.built ? ago(ctx.built) + " ago" : ""} from ${esc(ctx.folders.join(", "))}. Every agent starts from this instead of exploring your project.` : "Link your project folder on Files &amp; project, then read it once so agents start informed."}</p>
        <div class="row"><button class="ghost sm" id="ctxBuild">${ctx.exists ? "Read the project again" : "Read the project now"}</button>${ctx.exists ? `<button class="ghost sm" id="ctxShow">Show what agents see</button>` : ""}</div></div>`;
  },
  async newdept() {
    return `<p><a data-view="teams">← Teams</a></p><h1>New team</h1><p class="sub">Starts with one person on the cheapest model and no web or shell access; add people and abilities on its page.</p>
      <div class="card"><div class="form-grid">
        <label>Name</label><input id="ndName" placeholder="e.g. Customer Success">
        <label>Mission</label><input id="ndMission" placeholder="one line: what it's for" style="max-width:520px">
        <label>Head's first name</label><input id="ndHead" placeholder="e.g. Kai">
        <label>Working rules</label><textarea id="ndRules" placeholder="optional: how this department should work" style="min-height:70px"></textarea>
      </div><div class="row"><button id="ndGo">Create department</button><span class="hint" id="ndErr"></span></div></div>`;
  },
  async dept(id) {
    const d = S.depts.find((x) => x.id === id);
    const hist = await api(`/api/tasks?dept=${id}&limit=40`);
    const tierN = ["", "Haiku", "Sonnet", "Opus"];
    return `<p><a data-view="teams">← Teams</a></p><h1>${esc(d.name)} <button class="ghost sm" id="deEdit">edit</button></h1><p class="sub">${esc(d.mission)}${d.tools.length ? ` · tools: ${esc(d.tools.join(", "))} (works only inside its folder ${esc(id)}/, can't edit its own instructions)` : " · text-only (no tool tokens)"}</p>
      <div class="members">${d.members.map((m) => `<div class="mem"><b>${esc(m.name)}</b><span>${esc(m.role)} · ${tierN[m.tier]}${m.max_tier > m.tier ? "→" + tierN[m.max_tier] : ""}</span>
        <button class="ghost sm" data-editmem="${esc(m.id)}" title="edit">edit</button><button class="ghost sm" data-delmem="${esc(m.id)}" title="remove">×</button></div>`).join("")}
        <button class="ghost sm" id="addMem">+ member</button></div>
      <div class="row abil"><span class="hint">Abilities:</span>${Object.entries(ABIL).map(([k, [label, tip]]) => `<label title="${esc(tip)}"><input type="checkbox" data-abil="${k}" ${d.abilities.includes(k) ? "checked" : ""}> ${label}</label>`).join("")}
        ${d.abilities.includes("shell") ? `<span class="hint">${d.keys.length ? "keys: " + esc(d.keys.join(", ")) : "no keys yet (Keys &amp; tokens page)"}</span>` : ""}</div>
      <div class="card" style="margin-top:14px">
        <textarea id="dT" placeholder="Give ${esc(d.name)} a task directly"></textarea>
        <div class="row">
          <select id="dM">${d.members.map((m) => `<option value="${m.id}">${esc(m.name)}</option>`).join("")}</select>
          <select id="dTier"><option value="">model: auto</option><option value="1">Haiku</option><option value="2">Sonnet</option><option value="3">Opus</option></select>
          <button id="dGo">Run now</button>
        </div>
      </div>
      <h2>History</h2><div class="feed">${hist.filter((t) => t.status !== "backlog").map(taskRow).join("") || `<div class="t"><span class="what">No tasks yet.</span></div>`}</div>
      <p style="margin-top:24px"><button class="ghost sm" id="deRemove">Remove this department</button> <span class="hint">Its folder is kept under an archive name.</span></p>`;
  },
  async kb() {
    const q = viewArg || "";
    const items = await api("/api/kb" + (q ? `?q=${encodeURIComponent(q)}` : ""));
    return `<h1>Company</h1>${tabs("kb")}<p class="sub">One shared memory. Agents receive only the top 3 matching snippets per task, never the whole KB. They add facts with <code>KB+:</code> lines.</p>
      <div class="row"><input id="kbQ" placeholder="search…" value="${esc(q)}" style="max-width:340px"><button class="ghost" id="kbS">Search</button></div>
      <div class="card" style="margin-top:14px"><div class="row" style="margin:0"><input id="kbTi" placeholder="title" style="max-width:260px">
        <select id="kbD"><option value="">all depts</option>${S.depts.map((d) => `<option value="${d.id}">${esc(d.name)}</option>`).join("")}</select></div>
        <textarea id="kbB" placeholder="fact (keep it short: it is injected into prompts)" style="margin-top:10px;min-height:60px"></textarea>
        <div class="row"><button id="kbAdd">Add fact</button><span class="hint">${items.length} entries shown</span></div></div>
      <div style="margin-top:10px">${items.map((k) => `<div class="kbi"><b>${esc(k.title)}</b> <span class="hint">${esc(k.dept || "all")} · ${ago(k.updated)} ago</span>
        <div class="md">${md(k.body)}</div><button class="ghost sm" data-kbedit="${k.id}">edit</button> <button class="ghost sm" data-kbdel="${k.id}">delete</button></div>`).join("")}</div>`;
  },
  async log() {
    const who = viewArg || "";
    const r = await api(`/api/activity?hours=72&limit=400${who ? `&actor=${who}` : ""}`);
    const tab = (k, label) => `<button class="${k === who ? "" : "ghost "}sm" data-view="log" data-arg="${k}">${label}</button>`;
    const when = (t) => new Date(t * 1000).toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" });
    const row = (e) => {
      const ref = [e.dept ? `${esc(e.dept)} #${e.task}` : e.task ? `#${e.task}` : "", e.project ? `<a data-view="project" data-arg="${e.project}">project ${e.project}</a>` : "", e.tokens ? `${(+e.tokens).toLocaleString()} tokens` : ""].filter(Boolean).join(" · ");
      const extra = [e.asked ? `<div class="hint">asked: ${esc(e.asked)}</div>` : "",
        ...(e.handed_out || []).map((t) => `<div class="hint">→ ${esc(t)}</div>`),
        ...(e.asks || []).map((a) => `<div class="hint">asked another team: ${esc(a)}</div>`),
        e.fields ? `<div class="hint">${esc(Object.entries(e.fields).map(([k, v]) => `${k}=${v}`).join(" "))}</div>` : ""].join("");
      return `<tr><td class="hint" style="white-space:nowrap">${when(e.at)}</td><td>${esc(e.actor)}</td><td><b>${esc(e.event)}</b> ${ref ? `<span class="hint">${ref}</span>` : ""}${e.text ? `<div>${esc(e.text)}</div>` : ""}${extra}</td></tr>`;
    };
    return `<h1>Company</h1>${tabs("log")}<p class="hint">What you asked, how the CEO understood it, and what each team did. Last 3 days, newest first. Key values are never logged.</p>
      <div class="row" style="margin-bottom:10px">${tab("", "Everything")}${tab("owner", "You")}${tab("ceo", "CEO")}${tab("agent", "Teams")}${tab("system", "By itself")}</div>
      <div class="card"><table class="tbl">${r.entries.slice().reverse().map(row).join("") || `<tr><td class="hint">Nothing yet.</td></tr>`}</table></div>`;
  },
  async outbox() {
    const r = await api("/api/outbox?view=" + (viewArg || "pending")), v = r.view;
    const tab = (k, label) => `<button class="${k === v ? "" : "ghost "}sm" data-view="outbox" data-arg="${k}">${label} (${r.counts[k]})</button>`;
    const setup = `<a data-view="keys">set up email on Keys &amp; tokens</a>`;
    const sendBtn = (o, label) => `<button class="sm" data-obsend="${o.id}" data-to="${esc(o.send.to.join(", "))}" data-subj="${esc(o.send.subject)}">${label}</button>`;
    const btns = (o) => {
      const e = o.send || {}, ok = e.email && !e.problem;
      if (o.status === "pending") {
        if (ok) return `${sendBtn(o, "Approve &amp; send")}<button class="ghost sm" data-ob="${o.id}" data-act="approved">Approve, send later</button><button class="ghost sm" data-obtest="${o.id}" title="send it to OWNER_EMAIL first">Send a test to me</button><button class="ghost sm" data-ob="${o.id}" data-act="rejected">Reject</button>
          <span class="hint">to ${esc(e.to.join(", "))} · ${esc(e.subject)}</span>`;
        return `<button class="sm" data-ob="${o.id}" data-act="approved">Approve</button><button class="ghost sm" data-ob="${o.id}" data-act="rejected">Reject</button>
          <span class="hint">${e.email ? `Email to ${esc(e.to.join(", "))}. ${r.channel ? esc(e.problem) : `Can't send from here yet: ${setup}, or send it yourself and mark it sent.`}`
            : "Not an email (no To: line). Approving files it under Done; copy it where it goes."}</span>`;
      }
      if (o.status === "approved")
        return `${ok ? sendBtn(o, "Send email") : ""}<button class="${ok ? "ghost " : ""}sm" data-ob="${o.id}" data-act="sent">${ok ? "Mark sent" : "I sent it myself"}</button>
          <button class="ghost sm" data-ob="${o.id}" data-act="pending">Back to review</button>
          <span class="hint">${ok ? `to ${esc(e.to.join(", "))} · ${esc(e.subject)}` : r.channel ? esc(e.problem) : `Can't send from here yet: ${setup}.`}</span>`;
      return `<button class="ghost sm" data-ob="${o.id}" data-act="pending">Back to review</button>${o.note ? `<span class="hint">${esc(o.note)}</span>` : ""}`;
    };
    const help = { pending: "What agents want to send outside the company (emails, posts). Edit the text, then approve or reject. Nothing leaves without your click.",
      approved: "Approved emails waiting for you to send. Send them from here, or send them yourself and mark them sent.",
      done: "Sent, rejected, and approved non-email drafts (posts, docs) you copy where they go." }[v];
    const warn = v !== "done" && !r.channel && r.items.some((o) => o.send?.email)
      ? `<div class="card needs" style="margin-top:12px"><b>Email sending isn't set up yet</b><p class="hint">Approved emails stay here until you send them. To send with one click, add <b>RESEND_API_KEY</b> + <b>MAIL_FROM</b> (or <b>SMTP_HOST</b>, <b>SMTP_USER</b>, <b>SMTP_PASS</b> + <b>MAIL_FROM</b>). Agents can't use these keys.</p><div class="row"><button class="sm" data-view="keys">Open Keys &amp; tokens</button></div></div>` : "";
    return `<h1>Outbox</h1><p class="sub">${help}</p><div class="row">${tab("pending", "To review")}${tab("approved", "Ready to send")}${tab("done", "Done")}</div>${warn}
      ${r.items.map((o) => `<div class="card" style="margin:10px 0"><div class="rep-h"><span>${esc(o.dept === "ask" ? "from Ask" : o.dept)}${o.task_id ? ` · task #${o.task_id}` : ""} · ${ago(o.created)} ago</span><span class="st ${o.status === "pending" ? "backlog" : o.status === "rejected" ? "failed" : "done"}">${o.status === "approved" ? "ready to send" : o.status}</span></div>
        ${o.status === "pending" ? `<textarea class="obtext" data-obedit="${o.id}" style="min-height:140px">${esc(o.body)}</textarea>` : `<div class="out md">${md(o.body)}</div>`}
        <div class="row">${btns(o)}<button class="ghost sm" data-obcopy="${o.id}">Copy</button></div></div>`).join("") || `<p class="hint" style="margin-top:14px">Nothing here.</p>`}`;
  },
  async keys() {
    const r = await api("/api/env"), keys = Object.entries(r.keys);
    const dsel = (id, cur = []) => `<select id="${id}" multiple size="${Math.min(S.depts.length, 5)}" title="none selected = all departments"><option value="none">no agent (only you, e.g. email sending)</option>${S.depts.map((d) => `<option value="${d.id}" ${cur.includes(d.id) ? "selected" : ""}>${esc(d.name)}</option>`).join("")}</select>`;
    return `<h1>Company</h1>${tabs("keys")}<p class="sub">API keys, webhooks and tokens your agents may use. Values are stored on this machine with owner-only permissions, are never shown again, never go into a prompt, and are masked if an agent repeats one. Agents with shell tools get them as environment variables and see only the names and your note.</p>
      ${keys.length ? `<table class="svc"><tr><th>name</th><th>value</th><th>who gets it</th><th>note</th><th></th></tr>${keys.map(([k, e]) => `<tr>
        <td><b>${esc(k)}</b></td><td class="hint">${esc(e.masked)}</td>
        <td>${e.claude ? "every Claude call (login)" : e.depts.includes("none") ? "no agent (only you)" : esc(e.depts.map((d) => S.depts.find((x) => x.id === d)?.name || d).join(", ") || "all departments")}</td>
        <td class="hint">${esc(e.note)}</td>
        <td class="svc-act"><button class="ghost sm" data-envedit="${esc(k)}">replace</button><button class="ghost sm" data-envdel="${esc(k)}">×</button></td></tr>`).join("")}</table>`
        : `<p class="hint" style="margin-top:14px">No keys yet. Add the ones your departments need, for example an email API key for Sales or a deploy token for Engineering.</p>`}
      <div class="card" style="margin-top:18px"><b>Add a key</b>
        <div class="row" style="margin-top:8px"><input id="evName" placeholder="NAME, e.g. RESEND_API_KEY" style="max-width:260px" autocomplete="off">
          <input id="evVal" type="password" placeholder="value" autocomplete="new-password" style="max-width:320px"></div>
        <div class="row"><input id="evNote" placeholder="what it's for (agents see this)" style="max-width:584px"></div>
        <div class="row"><div><div class="hint">Departments (none selected = all)</div>${dsel("evDepts")}</div></div>
        <div class="row"><button id="evSave">Save key</button><span class="hint" id="evErr"></span></div>
        <p class="hint">Claude login: save <b>CLAUDE_CODE_OAUTH_TOKEN</b> (from <code>claude setup-token</code>) so agents never stop on an expired login. <b>ANTHROPIC_API_KEY</b> switches billing to your API account instead of the subscription.</p>
        <p class="hint">Sending approved emails from the Outbox: <b>RESEND_API_KEY</b> + <b>MAIL_FROM</b>, or <b>SMTP_HOST</b>, <b>SMTP_PORT</b>, <b>SMTP_USER</b>, <b>SMTP_PASS</b> + <b>MAIL_FROM</b>. These stay out of every agent's reach by default: only you can send, by clicking Send.</p></div>
      <div class="card" style="margin-top:12px"><b>Import a .env file</b><p class="hint">Paste KEY=value lines. Comments and blank lines are skipped.</p>
        <textarea id="evImport" placeholder="STRIPE_SECRET_KEY=sk_live_..." style="min-height:90px"></textarea>
        <div class="row"><button class="ghost" id="evImportGo">Import</button></div></div>`;
  },
  async services() {
    const r = await api("/api/services");
    const dot = (s) => `<span class="svc-dot ${s === "active" ? "on" : s === "failed" ? "bad" : ""}"></span>`;
    const blocked = r.services.filter((s) => !s.can_control);
    return `<h1>Company</h1>${tabs("services")}<p class="sub">Everything the company runs on this machine: status, start, stop, restart and logs. Agents can see whether a service is up, but only you can start or stop one, here.</p>
      <div class="row"><button id="svDiscover">Find services on this machine</button><span class="hint">Looks for systemd units and timers that use the company's live folders or match its name. Nothing is added until you choose.</span></div>
      <div id="svFound"></div>
      ${r.services.length ? `<table class="svc"><tr><th></th><th>service</th><th>state</th><th>department</th><th>since / next run</th><th></th></tr>
        ${r.services.map((s) => `<tr><td>${dot(s.state)}</td>
          <td><b>${esc(s.name)}</b><div class="hint">${esc(s.about || s.description || s.unit)}</div><div class="hint">${esc(s.unit)} · ${esc(s.scope)}</div></td>
          <td>${esc(s.state)}<div class="hint">${esc(s.detail || "")}</div></td>
          <td><select data-svdept="${esc(s.name)}"><option value="">none</option>${S.depts.map((d) => `<option value="${d.id}" ${d.id === s.dept ? "selected" : ""}>${esc(d.name)}</option>`).join("")}</select></td>
          <td class="hint">${esc(s.next ? "next: " + s.next : s.since || "")}</td>
          <td class="svc-act">${["start", "stop", "restart"].map((a) => `<button class="ghost sm" data-svact="${a}" data-sv="${esc(s.name)}" ${s.can_control ? "" : "disabled title=\"needs the one-time permission below\""}>${a}</button>`).join("")}
            <button class="ghost sm" data-svlogs="${esc(s.name)}">logs</button><button class="ghost sm" data-svdel="${esc(s.name)}" title="stop tracking (the service is untouched)">×</button></td></tr>`).join("")}</table>`
        : `<p class="hint" style="margin-top:16px">No services yet. Use "Find services on this machine".</p>`}
      ${blocked.length && r.permissions.text ? `<div class="card" style="margin-top:18px"><b>One-time permission for ${blocked.length} service(s)</b>
        <p class="hint">System services need root to start or stop. This file allows exactly start, stop and restart of the services listed above, for this user only. Run on the server:</p>
        <div class="cmdline"><code>${esc(r.permissions.install)}</code><button class="ghost sm" data-copy="${esc(r.permissions.install)}">Copy</button></div>
        <pre class="out" style="max-height:160px">${esc(r.permissions.text)}</pre></div>` : ""}`;
  },
  async files() {
    const f = await api("/api/files");
    const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "never");
    const cmd = (label, text) => `<div class="hint" style="margin-top:6px">${label}</div><div class="cmdline"><code>${esc(text)}</code><button class="ghost sm" data-copy="${esc(text)}">Copy</button></div>`;
    return `<h1>Company</h1>${tabs("files")}<p class="sub">The whole project in one folder. Each department has <b>sources/</b>, <b>outputs/</b> (every finished task, saved as a file), and there's a company-wide <b>shared/</b>. Folders on this server show up in <b>sources/</b> as links to where they really are: live, nothing copied. Folders on other PCs are copied in over ssh and tar; push never overwrites the agents' edits.</p>
      <div class="card"><div class="row" style="margin:0">
        <input id="lkName" placeholder="name, e.g. app-code" style="max-width:200px">
        <select id="lkDept">${S.depts.map((d) => `<option value="${d.id}">${esc(d.name)}</option>`).join("")}</select>
        <input id="lkOrigin" placeholder="where it lives, e.g. PC1 C:\\Users\\me\\app" style="max-width:340px">
        <button id="lkAdd">Link folder</button></div></div>
      ${f.depts.map((d) => `<h2>${esc(d.name)}</h2><div class="grid">
        ${d.sources.map((s) => s.mode === "direct" ? `<div class="card"><div class="rep-h"><span>sources/${esc(s.name)}</span><span class="hint">${s.files} files · live</span></div>
          <p class="hint" style="margin:0">${s.link ? `<b>${esc(s.link)}</b> → ` : ""}${esc(s.path)}</p>
          <p class="hint" style="margin:4px 0 0">Used in place, never copied. Agents ${s.writable ? "may edit it" : "read it only"}${s.git ? "; for projects they change it on their own git branch, which you review and merge" : ""}.</p>
          <div class="row"><input data-testcmd="${esc(s.name)}" value="${esc(s.test || "")}" placeholder="${esc(s.test_suggested ? "test command, e.g. " + s.test_suggested : "test command (optional)")}" style="max-width:360px">
            <button class="ghost sm" data-testsave="${esc(s.name)}">Save</button><button class="ghost sm" data-testrun="${esc(s.name)}" ${s.test ? "" : "disabled"}>Check</button></div>
          ${s.test_last ? `<p class="hint">tests ${s.test_last.ok ? "passed" : "failed"} ${ago(s.test_last.at)} ago${s.test_last.hint ? ": " + esc(s.test_last.hint) : ""}</p>` : ""}
          <div class="row">${s.git ? "" : `<button class="ghost sm" data-track="${esc(s.name)}" title="one baseline commit; agents then work on a branch you merge by accepting the result">Let agents change it safely (git)</button>`}<button class="ghost sm" data-unlink="${esc(s.name)}">Unlink</button></div></div>`
        : `<div class="card"><div class="rep-h"><span>sources/${esc(s.name)}</span><span class="hint">${s.files} files</span></div>
          <p class="hint" style="margin:0">from ${esc(s.origin || "?")} · synced ${when(s.synced)}</p>
          <p class="hint" style="margin:4px 0 0">${s.changed_by_agents.length} changed by agents${s.conflicts.length ? ` · <b style="color:var(--bad)">${s.conflicts.length} conflict(s)</b>` : ""}</p>
          ${s.local ? `<div class="row"><button class="sm" data-syncnow="${esc(s.name)}">Sync now</button><span class="hint">copies ${esc(s.local)} in (your folder is untouched)</span></div>
            ${cmd("Bring the agents' changes back", s.commands.pull_local)}`
          : `${cmd("Windows: push your folder", s.commands.push_powershell)}${cmd("Windows: pull agents' changes", s.commands.pull_powershell)}
          ${cmd("macOS/Linux: push", s.commands.push_bash)}${cmd("macOS/Linux: pull", s.commands.pull_bash)}`}
          <div class="row"><button class="ghost sm" data-unlink="${esc(s.name)}">Unlink</button></div></div>`).join("")}
        <div class="card"><div class="rep-h"><span>outputs/</span><span class="hint">${d.outputs.length ? "latest " + d.outputs.length : "empty"}</span></div>
          ${d.outputs.map((o) => `<div class="dr-task" data-output="${d.id}/${esc(o.name)}"><span>${esc(o.name)}</span><span class="hint">${fmt(o.size)}b</span></div>`).join("") || `<p class="hint">Finished tasks will appear here as files.</p>`}</div>
      </div>`).join("")}
      <p class="hint" style="margin-top:20px">On the server: ${esc(f.root)}</p>`;
  },
  async usage() {
    const u = await api("/api/usage");
    const b = u.budget;
    return `<h1>Company</h1>${tabs("usage")}<p class="sub">Your commands run up to ${b.manual_max_percent}% of your plan; your schedules stop at ${b.auto_max_percent}%. Now: ${esc(b.manual_ok[1])}.</p>
      <h2>By model · 7 days</h2><table><tr><th>model</th><th>calls</th><th>input</th><th>output</th><th>cache read</th><th>cache write</th><th>≈ API $</th></tr>
        ${u.by_model.map((r) => `<tr><td>${mBadge(r.model)}${esc(r.model)}</td><td>${r.calls}</td><td>${fmt(r.input)}</td><td>${fmt(r.output)}</td><td>${fmt(r.cache_read)}</td><td>${fmt(r.cache_create)}</td><td>${(r.cost || 0).toFixed(3)}</td></tr>`).join("")}</table>
      <h2>By department · 7 days</h2><table><tr><th>dept</th><th>calls</th><th>auto</th><th>tokens</th><th>≈ API $</th></tr>
        ${u.by_dept.map((r) => `<tr><td>${esc(r.dept)}</td><td>${r.calls}</td><td>${r.auto_calls}</td><td>${fmt(r.tokens)}</td><td>${(r.cost || 0).toFixed(3)}</td></tr>`).join("")}</table>
      <h2>By day</h2><table><tr><th>day</th><th>calls</th><th>tokens</th></tr>${u.by_day.map((r) => `<tr><td>${r.day}</td><td>${r.calls}</td><td>${fmt(r.tokens)}</td></tr>`).join("")}</table>
      <p class="hint">≈ API $ is the API-equivalent value reported by Claude Code. On a subscription you are not billed this amount.</p>`;
  },
};

async function render() {
  try {
    await loadState();
    const keep = ["ceoT", "rvNotes", "dvQ", "dT", "kbTi", "kbB", "lkName", "lkOrigin", "rtText", "rtEvery", "evName", "evNote"].map((i) => [i, $("#" + i)?.value]).filter(([, v]) => v);
    if (!V[view]) { view = "today"; viewArg = null; }
    $("#view").innerHTML = await V[view](viewArg);
    keep.forEach(([i, v]) => { const el = $("#" + i); if (el) el.value = v; });
    if (view === "office") Office.mount(); else Office.unmount();
    if (view === "office" && S.designed) $("#view").insertAdjacentHTML("afterbegin", `<h1>Company</h1>${tabs("office")}`);
  } catch (e) { if (e.message !== "401") banner("Error: " + e.message.slice(0, 200)); }
}
function go(v, arg = null) { view = v; viewArg = arg; history.replaceState(null, "", `#${v}${arg ? "/" + encodeURIComponent(arg) : ""}`); render(); }

async function openTask(id) {
  const t = await api(`/api/task/${id}`);
  const u = t.usage.map((r) => `${mBadge(r.model)}in ${fmt(r.input)} · out ${fmt(r.output)} · cache ${fmt(r.cache_read)}`).join("<br>");
  $("#mbody").innerHTML = `<h1 style="margin-top:0">#${t.id} · ${esc(t.dept)}/${esc(t.member)}</h1>
    <p class="hint"><span class="st ${t.status}">${t.status}</span> · ${t.model ? mBadge(t.model) + esc(t.model) : "no model"} · ${fmt(t.tokens)} tok · ${esc(t.source)}</p>
    <h2>Task</h2><div class="out md">${md(t.body)}</div>
    <h2>Result</h2><div class="out md">${md(t.result || t.error || "(none yet)")}</div>
    ${t.next_line ? `<p class="hint">NEXT → backlog: ${esc(t.next_line)}</p>` : ""}
    ${u ? `<p class="hint">${u}</p>` : ""}
    <div class="row"><select id="rtTier"><option value="">retry: same model</option><option value="1">retry on Haiku</option><option value="2">retry on Sonnet</option><option value="3">retry on Opus</option></select>
      <button class="ghost" id="rtGo" data-id="${t.id}">Retry</button>
      ${["queued", "backlog", "waiting", "running", "held", "queued_blocked", "routine_blocked"].includes(t.status) ? `<button class="ghost" id="rtCancel" data-id="${t.id}">${t.status === "running" ? "Stop it" : "Cancel"}</button>` : ""}
      ${t.project_id ? `<button class="ghost" data-project="${t.project_id}">Open project</button>` : ""}
      <button class="ghost" data-copy="${esc(t.result || "")}">Copy result</button></div>`;
  $("#modal").classList.remove("hide");
}

// ---------- events ----------
document.addEventListener("click", async (e) => {
  const el = e.target.closest("[data-client],[data-qans],[data-change],[data-project],[data-ia],[data-final],[data-toob],[data-pact2],[data-revise],[data-dl],[data-rtedit],[data-view],[data-task],[data-delmem],[data-kbdel],[data-kbedit],[data-ob],[data-copy],[data-output],[data-unlink],[data-track],[data-syncnow],[data-svact],[data-svlogs],[data-svdel],[data-svadd],button");
  if (!el) return;
  if (el.dataset.svact) {
    if (el.dataset.svact === "stop" && !confirm(`Stop ${el.dataset.sv}?`)) return;
    el.disabled = true; el.textContent = "…";
    try { const r = await api(`/api/services/${encodeURIComponent(el.dataset.sv)}/${el.dataset.svact}`, { method: "POST" }); banner(r.message); }
    catch (err) { banner(err.message.replace(/^\{"detail":"|"\}$/g, "").slice(0, 220)); }
    return render();
  }
  if (el.dataset.svlogs) {
    const r = await api(`/api/services/${encodeURIComponent(el.dataset.svlogs)}/logs?lines=150`);
    $("#mbody").innerHTML = `<h1 style="margin-top:0">${esc(r.name)} · logs</h1><pre class="out">${esc(r.text)}</pre>
      <div class="row"><button class="ghost" data-svlogs="${esc(r.name)}">Refresh</button></div>`;
    $("#modal").classList.remove("hide"); return;
  }
  if (el.dataset.svdel) {
    if (confirm(`Stop tracking ${el.dataset.svdel}? The service itself keeps running.`)) await api(`/api/services/${encodeURIComponent(el.dataset.svdel)}`, { method: "DELETE" });
    return render();
  }
  if (el.dataset.svadd) {
    const f = JSON.parse(el.dataset.svadd);
    el.disabled = true; await api("/api/services", { method: "POST", body: { name: f.name, unit: f.unit, scope: f.scope, about: f.about } });
    el.textContent = "added"; return render();
  }
  if (el.id === "svDiscover") {
    el.disabled = true; el.textContent = "Looking…";
    const r = await api("/api/services/discover", { method: "POST", body: {} });
    $("#svFound").innerHTML = r.found.length ? `<div class="card" style="margin-top:12px"><b>Found ${r.found.length}</b>
      ${r.found.map((f) => `<div class="dr-task"><span>${esc(f.unit)} <span class="hint">${esc(f.about)} · ${esc(f.scope)} · matched ${esc(f.why)}</span></span><button class="sm" data-svadd="${esc(JSON.stringify(f))}">Add</button></div>`).join("")}</div>`
      : `<p class="hint">Nothing new found. Link the folders your services run from (Files page), then try again.</p>`;
    el.disabled = false; el.textContent = "Find services on this machine"; return;
  }
  if (el.dataset.syncnow) {
    el.disabled = true; el.textContent = "Syncing…";
    try { const r = await api(`/api/files/${encodeURIComponent(el.dataset.syncnow)}/sync`, { method: "POST" });
      banner(`${el.dataset.syncnow}: +${r.added} new, ${r.updated} updated, ${r.deleted} deleted${r.conflicts.length ? `, ${r.conflicts.length} conflict(s)` : ""}`); }
    catch (err) { banner("Sync failed: " + err.message.slice(0, 160)); }
    return render();
  }
  if (el.dataset.output) {
    const [dept, name] = el.dataset.output.split("/");
    const o = await api(`/api/files/${dept}/outputs/${encodeURIComponent(name)}`);
    $("#mbody").innerHTML = `<h1 style="margin-top:0">${esc(o.name)}</h1>${/\.(md|txt)$/i.test(o.name) ? `<div class="out md">${md(o.text)}</div>` : `<pre class="out">${esc(o.text)}</pre>`}
      <div class="row"><button class="ghost" data-copy="${esc(o.text)}">Copy</button></div>`;
    $("#modal").classList.remove("hide"); return;
  }
  if (el.dataset.track) {
    try { const r = await api(`/api/files/${encodeURIComponent(el.dataset.track)}/track`, { method: "POST" }); banner(r.message); } catch (err) { banner(errText(err)); }
    return render();
  }
  if (el.dataset.unlink) {
    if (confirm(`Unlink "${el.dataset.unlink}"? Agents stop using it. The folder itself is untouched.`))
      await api(`/api/files/${encodeURIComponent(el.dataset.unlink)}`, { method: "DELETE" });
    return render();
  }
  if (el.id === "lkAdd") {
    const name = $("#lkName").value.trim(); if (!name) { $("#lkName").focus(); return; }
    try { await api("/api/files", { method: "POST", body: { name, dept: $("#lkDept").value, origin: $("#lkOrigin").value.trim() } }); $("#lkName").value = ""; $("#lkOrigin").value = ""; }
    catch (err) { banner("Not linked: " + err.message.slice(0, 160)); }
    return render();
  }
  if (el.dataset.ia) {                                       // inbox actions
    const d = el.dataset, id = d.iid;
    try {
      if (d.ia === "view") return go(d.iview, d.iarg || null);
      if (d.ia === "project") return go("project", id);
      if (d.ia === "retry") await api(`/api/task/${id}/retry`, { method: "POST", body: { text: "" } });
      if (d.ia === "dismiss") await api(`/api/task/${id}`, { method: "DELETE" });
      if (d.ia === "change") return openChange(id);
      if (d.ia === "read_project") { await api("/api/context/build", { method: "POST" }); banner("Project read: agents now start from it."); }
      if (d.ia === "retry_project") await api(`/api/projects/${id}/retry`, { method: "POST", body: {} });
      if (d.ia === "cancel_project") await api(`/api/projects/${id}/cancel`, { method: "POST", body: { why: "dismissed" } });
      if (d.ia === "dismiss_heartbeat") await api("/api/inbox/heartbeat/dismiss", { method: "POST" });
      if (d.ia === "decision") {
        const inp = document.getElementById(`dc_${id}`), text = inp.value.trim(); if (!text) return inp.focus();
        const r = await api(`/api/decisions/open/${encodeURIComponent(id)}/answer`, { method: "POST", body: { text, save: true } });
        banner(`Answered ${r.answered} question(s); ${r.started.length} team(s) continue with it.`);
      }
      if (d.ia === "decision_drop") await api(`/api/decisions/open/${encodeURIComponent(id)}/dismiss`, { method: "POST" });
      if (d.ia === "client") return openClient(id);
    } catch (err) { banner(errText(err)); }
    return render();
  }
  if (el.dataset.change) return openChange(el.dataset.change);
  if (el.dataset.client) return openClient(el.dataset.client);
  if (el.dataset.qans) {
    const text = $(`#qa_${el.dataset.qans}`).value.trim(); if (!text) return $(`#qa_${el.dataset.qans}`).focus();
    try { await api(`/api/projects/${el.dataset.pid}/suggestions/${el.dataset.qans}/answer`, { method: "POST", body: { text, save: $(`#qs_${el.dataset.qans}`).checked, run: true } });
      banner("Answered. The team continues with it."); } catch (err) { banner(errText(err)); }
    return render();
  }
  if (el.id === "bzSave") {
    const body = {};
    for (const k of ["customer", "offer", "pricing", "stage", "channels", "target_90d", "constraints"]) body[k] = $(`#bz_${k}`).value.trim();
    try { await api("/api/business", { method: "PUT", body }); el.textContent = "Saved"; await loadState(); }
    catch (err) { $("#bzErr").textContent = errText(err); }
    return;
  }
  if (el.id === "obAdd") {
    const text = $("#obText").value.trim(); if (!text) return $("#obText").focus();
    const g = await api("/api/goals");
    const cur = (g.objectives || []).map((o) => { const x = { text: o.text }; for (const k of ["metric", "target", "due", "dept"]) if (o[k] != null && o[k] !== "") x[k] = o[k]; return x; });
    const o = { text, metric: $("#obMetric").value }; if ($("#obTarget").value) o.target = +$("#obTarget").value; if ($("#obDue").value) o.due = $("#obDue").value;
    try { await api("/api/goals", { method: "PUT", body: { goals: [...cur, o] } }); } catch (err) { banner(errText(err)); }
    return render();
  }
  if (el.id === "clAdd") {
    const email = $("#clEmail").value.trim(); if (!email) return $("#clEmail").focus();
    try { await api("/api/clients", { method: "POST", body: { email, name: $("#clName").value.trim(), org: $("#clOrg").value.trim(), stage: $("#clStage").value } }); }
    catch (err) { return ($("#clErr").textContent = errText(err)); }
    return render();
  }
  if (el.id === "clImport") {
    const text = $("#clCsv").value.trim(); if (!text) return $("#clCsv").focus();
    try { const r = await api("/api/clients/import", { method: "POST", body: { text } }); banner(`Imported: ${r.added} new, ${r.updated} updated, ${r.skipped} without an email.`); }
    catch (err) { return ($("#clErr").textContent = errText(err)); }
    return render();
  }
  if (el.id === "ctSave") {
    const id = el.dataset.cid, body = { stage: $("#ctStage").value, next_step: $("#ctStep").value, next_date: $("#ctDate").value };
    if ($("#ctNote").value.trim()) body.note = $("#ctNote").value.trim();
    try { await api(`/api/clients/${id}`, { method: "PATCH", body }); } catch (err) { return banner(errText(err)); }
    return openClient(id);
  }
  if (el.id === "ctPay") {
    const id = el.dataset.cid;
    try { await api(`/api/clients/${id}/revenue`, { method: "POST", body: { amount: $("#ctAmt").value, currency: $("#ctCur").value, kind: $("#ctKind").value } }); }
    catch (err) { return banner(errText(err)); }
    return openClient(id);
  }
  if (el.id === "ctDel") {
    if (!confirm("Delete this client and their history? This can't be undone.")) return;
    await api(`/api/clients/${el.dataset.cid}`, { method: "DELETE" }); $("#modal").classList.add("hide"); return render();
  }
  if (el.dataset.testsave) {
    const v = $(`[data-testcmd="${el.dataset.testsave}"]`).value;
    try { await api(`/api/files/${encodeURIComponent(el.dataset.testsave)}/test`, { method: "PUT", body: { command: v } }); banner("Test command saved."); } catch (err) { banner(errText(err)); }
    return render();
  }
  if (el.dataset.testrun) {
    el.disabled = true; el.textContent = "Running…";
    try { const r = await api(`/api/files/${encodeURIComponent(el.dataset.testrun)}/test`, { method: "POST" });
      $("#mbody").innerHTML = `<h1 style="margin-top:0">Tests ${r.ok ? "passed" : "failed"}</h1>${r.hint ? `<div class="card needs">${esc(r.hint)}</div>` : ""}<pre class="out">${esc(r.output)}</pre>`;
      $("#modal").classList.remove("hide"); } catch (err) { banner(errText(err)); }
    return render();
  }
  if (el.dataset.project) { e.stopPropagation(); return go("project", el.dataset.project); }
  if (el.dataset.pact2) {
    const a = el.dataset.pact2, pid = el.dataset.pid;
    if (a === "cancel" && !confirm("Cancel this project? Running work is stopped.")) return;
    if (a === "discard" && !confirm("Discard this result? Any code changes in it are thrown away.")) return;
    try { const r = await api(`/api/projects/${pid}/${a}`, { method: "POST", body: {} });
      banner(a === "accept" ? ["Accepted.", ...(r.merged || [])].join("  ·  ") : a === "approve" ? "Started." : a === "retry" ? "Trying again." : a === "discard" ? "Discarded." : "Stopped."); }
    catch (err) { banner(errText(err)); }
    return render();
  }
  if (el.dataset.revise) {
    const box = $("#reviseBox");
    if (!$("#rvNotes")) {
      box.innerHTML = `<div class="card"><b>What should change?</b><textarea id="rvNotes" placeholder="e.g. Shorter, friendlier, and mention the free trial" style="min-height:70px"></textarea>
        <div class="row"><button data-revise="${el.dataset.revise}">Send back for changes</button></div></div>`;
      return $("#rvNotes").focus();
    }
    const notes = $("#rvNotes").value.trim(); if (!notes) return $("#rvNotes").focus();
    try { await api(`/api/projects/${el.dataset.revise}/revise`, { method: "POST", body: { notes } }); banner("Sent back with your notes."); }
    catch (err) { banner(errText(err)); }
    return render();
  }
  if (el.dataset.dl !== undefined) return download(el.dataset.dl, el.dataset.dltext);
  if (el.dataset.final) { await api(`/api/task/${el.dataset.final}/final`, { method: "POST", body: { final: el.dataset.on !== "1" } }); return render(); }
  if (el.dataset.toob) { await api(`/api/task/${el.dataset.toob}/outbox`, { method: "POST" }); banner("In the Outbox for review."); return; }
  if (el.dataset.rtedit) {
    const every = prompt("When (hourly · daily 09:00 · weekdays 09:00 · mon 09:00 · mon,thu 17:30)", el.dataset.every); if (every === null) return;
    const text = prompt("Task", el.dataset.text); if (text === null) return;
    try { await api(`/api/routines/${el.dataset.rtedit}`, { method: "PATCH", body: { every, text } }); } catch (err) { banner(errText(err)); }
    return render();
  }
  if (el.dataset.view) return go(el.dataset.view, el.dataset.arg || null);
  if (el.dataset.copy !== undefined) { navigator.clipboard?.writeText(el.dataset.copy); el.textContent = "Copied"; return; }
  if (el.dataset.task) return openTask(el.dataset.task);
  if (el.dataset.delmem) { if (confirm("Remove member?")) { await api(`/api/dept/${viewArg}/members/${el.dataset.delmem}`, { method: "DELETE" }); } return render(); }
  if (el.dataset.kbdel) { if (confirm("Delete entry?")) await api(`/api/kb/${el.dataset.kbdel}`, { method: "DELETE" }); return render(); }
  if (el.dataset.kbedit) {
    const k = (await api("/api/kb")).find((x) => x.id == el.dataset.kbedit);
    const body = prompt("Edit fact", k.body); if (body == null) return;
    await api(`/api/kb/${k.id}`, { method: "PUT", body: { title: k.title, body, dept: k.dept || "", tags: k.tags || "" } }); return render();
  }
  if (el.id === "goalsSave") {
    await api("/api/goals", { method: "PUT", body: { goals: $("#goalsT").value.split("\n") } });
    el.textContent = "Saved"; return;
  }
  if (el.id === "rtAdd") {
    const text = $("#rtText").value.trim(), every = $("#rtEvery").value.trim() || "mon 09:00"; if (!text) return $("#rtText").focus();
    try { await api("/api/routines", { method: "POST", body: { dept: $("#rtDept").value, text, every } }); $("#rtText").value = ""; $("#rtEvery").value = ""; }
    catch (err) { $("#rtErr").textContent = (() => { try { return JSON.parse(err.message).detail; } catch (e) { return err.message; } })(); return; }
    return render();
  }
  if (el.dataset.rtrun) { el.disabled = true; await api(`/api/routines/${el.dataset.rtrun}/run`, { method: "POST" }); return render(); }
  if (el.dataset.rttoggle) { await api(`/api/routines/${el.dataset.rttoggle}`, { method: "PATCH", body: { enabled: el.dataset.on !== "1" } }); return render(); }
  if (el.dataset.rtdel) { if (confirm("Delete this routine?")) await api(`/api/routines/${el.dataset.rtdel}`, { method: "DELETE" }); return render(); }
  if (el.id === "evSave") {
    const name = $("#evName").value.trim(), value = $("#evVal").value;
    if (!name) return $("#evName").focus();
    const picked = [...$("#evDepts").selectedOptions].map((o) => o.value);
    const depts = picked.includes("none") ? ["none"] : picked.length ? picked : (/^(SMTP_|MAIL_|RESEND_)/i.test(name) ? null : []);
    try { await api(`/api/env/${encodeURIComponent(name)}`, { method: "PUT", body: { value, depts, note: $("#evNote").value } }); }
    catch (err) { $("#evErr").textContent = (() => { try { return JSON.parse(err.message).detail; } catch (e) { return err.message; } })(); return; }
    return render();
  }
  if (el.dataset.envedit) {
    const v = prompt(`New value for ${el.dataset.envedit} (leave empty to keep it)`); if (!v) return;
    await api(`/api/env/${encodeURIComponent(el.dataset.envedit)}`, { method: "PUT", body: { value: v } }); return render();
  }
  if (el.dataset.envdel) {
    if (confirm(`Delete ${el.dataset.envdel}? Agents lose access to it.`)) await api(`/api/env/${encodeURIComponent(el.dataset.envdel)}`, { method: "DELETE" });
    return render();
  }
  if (el.id === "evImportGo") {
    const r = await api("/api/env/import", { method: "POST", body: { text: $("#evImport").value } });
    banner(r.added.length ? `Imported ${r.added.join(", ")}` : "Nothing imported: use KEY=value lines."); return render();
  }
  if (el.dataset.dismiss) { await api(`/api/task/${el.dataset.dismiss}`, { method: "DELETE" }); return render(); }
  if (el.dataset.obsend) {
    if (!confirm(`Send this email now?\n\nTo: ${el.dataset.to}\nSubject: ${el.dataset.subj}`)) return;
    const box = $(`[data-obedit="${el.dataset.obsend}"]`);     // keep the edits made in review
    if (box) await api(`/api/outbox/${el.dataset.obsend}`, { method: "PATCH", body: { body: box.value } });
    el.disabled = true; el.textContent = "Sending…";
    try { const r = await api(`/api/outbox/${el.dataset.obsend}/send`, { method: "POST" }); banner(r.message); }
    catch (err) { banner("Not sent: " + (() => { try { return JSON.parse(err.message).detail; } catch (e) { return err.message; } })()); }
    return render();
  }
  if (el.dataset.obtest) {
    const box = $(`[data-obedit="${el.dataset.obtest}"]`);
    if (box) await api(`/api/outbox/${el.dataset.obtest}`, { method: "PATCH", body: { body: box.value } });
    try { const r = await api(`/api/outbox/${el.dataset.obtest}/test`, { method: "POST" }); banner("Test: " + r.message); } catch (err) { banner(errText(err)); }
    return;
  }
  if (el.dataset.obcopy) {
    const box = $(`[data-obedit="${el.dataset.obcopy}"]`) || el.closest(".card").querySelector("pre");
    navigator.clipboard?.writeText(box.value ?? box.textContent); el.textContent = "Copied"; return;
  }
  if (el.dataset.ob) {
    const box = $(`[data-obedit="${el.dataset.ob}"]`);     // keep the edits made before approving
    if (box) await api(`/api/outbox/${el.dataset.ob}`, { method: "PATCH", body: { body: box.value } });
    const r = await api(`/api/outbox/${el.dataset.ob}/${el.dataset.act}`, { method: "POST" });
    await render();
    if (r.status === "done") banner("Approved and filed under Done: it isn't an email, so copy it where it goes.");
    return;
  }
  const id = el.id;
  if (id === "mclose") return $("#modal").classList.add("hide");
  if (id === "pauseB") { await api("/api/settings", { method: "POST", body: { paused: !S.budget.paused } }); return render(); }
  if (id === "ceoGo") {
    const text = $("#ceoT").value.trim(); if (!text) return $("#ceoT").focus();
    el.disabled = true; el.textContent = id === "ceoGo" ? "Planning…" : "Saving…";
    let r = null;
    try {
      r = await api("/api/ceo", { method: "POST", body: { text } });
      if (!r.error) $("#ceoT").value = "";
    } catch (err) { r = { error: errText(err) }; }
    if (r.project && id === "ceoGo") { await go("project", r.project); }
    else await render();
    banner(r.error || [r.held, r.answer, ...(r.actions || [])].filter(Boolean).join("  ·  "));
    return;
  }
  if (id === "dvS") return go("deliverables", $("#dvQ").value.trim() || null);
  if (id === "csSave") {
    const body = {}, val = (k) => $("#cs_" + k)?.value;
    for (const k of ["name", "about"]) body[k] = val(k);
    for (const k of ["auto_max_percent", "manual_max_percent", "concurrency", "task_timeout", "keep_days"]) if (val(k) !== "") body[k] = +val(k);
    body.quiet_hours = val("quiet_hours").split(",").map((x) => x.trim()).filter(Boolean);
    try { await api("/api/company/settings", { method: "PUT", body }); banner("Settings saved."); return render(); }
    catch (err) { $("#csErr").textContent = errText(err); return; }
  }
  if (id === "ctxBuild") {
    el.disabled = true; el.textContent = "Reading your project (one call)…";
    try { const r = await api("/api/context/build", { method: "POST" }); banner(r.skipped || "Project read: agents now start from it."); } catch (err) { banner(errText(err)); }
    return render();
  }
  if (id === "ctxShow") {
    const c = await api("/api/context");
    $("#mbody").innerHTML = `<h1 style="margin-top:0">What agents know about your project</h1><div class="out md">${md(c.pack)}</div>`;
    $("#modal").classList.remove("hide"); return;
  }
  if (id === "ndGo") {
    const name = $("#ndName").value.trim(); if (!name) return $("#ndName").focus();
    try { const r = await api("/api/depts", { method: "POST", body: { name, mission: $("#ndMission").value, head: $("#ndHead").value || "Lead", instructions: $("#ndRules").value } });
      return go("dept", r.id); }
    catch (err) { $("#ndErr").textContent = errText(err); return; }
  }
  if (id === "deEdit") {
    const d = S.depts.find((x) => x.id === viewArg), f = await api(`/api/depts/${viewArg}/files`);
    $("#mbody").innerHTML = `<h1 style="margin-top:0">Edit ${esc(d.name)}</h1><div class="form-grid">
      <label>Name</label><input id="deName" value="${esc(d.name)}"><label>Mission</label><input id="deMission" value="${esc(d.mission)}" style="max-width:520px"></div>
      <h2>Working rules</h2><p class="hint">Every agent in this department follows these (instructions.md).</p><textarea id="deIns" style="min-height:120px">${esc(f.instructions)}</textarea>
      <h2>Know-how</h2><p class="hint">Reusable knowledge for this kind of work (skills/domain.md).</p><textarea id="deSkill" style="min-height:120px">${esc(f.skill)}</textarea>
      <div class="row"><button id="deSave">Save</button></div>`;
    $("#modal").classList.remove("hide"); return;
  }
  if (id === "deSave") {
    try { await api(`/api/depts/${viewArg}`, { method: "PATCH", body: { name: $("#deName").value, mission: $("#deMission").value, instructions: $("#deIns").value, skill: $("#deSkill").value } });
      $("#modal").classList.add("hide"); } catch (err) { banner(errText(err)); }
    return render();
  }
  if (id === "deRemove") {
    if (!confirm("Remove this department? Its open work is archived and its folder is kept under an archive name.")) return;
    try { await api(`/api/depts/${viewArg}`, { method: "DELETE" }); return go("teams"); } catch (err) { banner(errText(err)); return; }
  }
  if (id === "dGo") {
    const text = $("#dT").value.trim(); if (!text) return;
    const tier = $("#dTier").value ? +$("#dTier").value : null;
    await api(`/api/dept/${viewArg}`, { method: "POST", body: { text, member: $("#dM").value, tier } });
    $("#dT").value = ""; return render();
  }
  if (id === "addMem" || el.dataset.editmem) {
    const d = S.depts.find((x) => x.id === viewArg), m = d.members.find((x) => x.id === el.dataset.editmem) || { id: "", name: "", role: "", tier: 1, max_tier: 2 };
    const opt = (v, cur, label) => `<option value="${v}" ${v === cur ? "selected" : ""}>${label}</option>`;
    $("#mbody").innerHTML = `<h1 style="margin-top:0">${m.id ? "Edit " + esc(m.name) : "New member of " + esc(d.name)}</h1><div class="form-grid">
      <label>First name</label><input id="mmName" value="${esc(m.name)}">
      <label>Role</label><input id="mmRole" value="${esc(m.role)}" placeholder="e.g. growth marketer">
      <label>Usual model</label><select id="mmTier">${opt(1, m.tier, "Haiku (cheapest, quick work)")}${opt(2, m.tier, "Sonnet (real work: code, writing, analysis)")}</select>
      <label>May step up to</label><select id="mmMax">${opt(1, m.max_tier, "Haiku")}${opt(2, m.max_tier, "Sonnet")}${opt(3, m.max_tier, "Opus (hard problems only)")}</select></div>
      <div class="row"><button id="mmSave" data-mid="${esc(m.id)}">Save</button><span class="hint" id="mmErr"></span></div>`;
    $("#modal").classList.remove("hide"); return;
  }
  if (id === "mmSave") {
    const name = $("#mmName").value.trim(); if (!name) return $("#mmName").focus();
    const tier = +$("#mmTier").value, max_tier = Math.max(+$("#mmMax").value, tier);
    try { await api(`/api/dept/${viewArg}/members`, { method: "POST", body: { id: el.dataset.mid || name, name, role: $("#mmRole").value, tier, max_tier } });
      $("#modal").classList.add("hide"); } catch (err) { $("#mmErr").textContent = errText(err); return; }
    return render();
  }
  if (id === "kbS") return go("kb", $("#kbQ").value.trim() || null);
  if (id === "kbAdd") {
    const title = $("#kbTi").value.trim(), body = $("#kbB").value.trim(); if (!title || !body) return;
    await api("/api/kb", { method: "POST", body: { title, body, dept: $("#kbD").value } }); return render();
  }
  if (id === "tick") { el.disabled = true; const r = await api("/api/heartbeat", { method: "POST" }); alert(r.msg); return render(); }
  if (id === "rtGo") {
    const tier = $("#rtTier").value ? +$("#rtTier").value : null;
    await api(`/api/task/${el.dataset.id}/retry`, { method: "POST", body: { text: "", tier } });
    $("#modal").classList.add("hide"); return render();
  }
  if (id === "rtCancel") { await api(`/api/task/${el.dataset.id}`, { method: "DELETE" }); $("#modal").classList.add("hide"); return render(); }
});
document.addEventListener("change", async (e) => {
  if (e.target.dataset.abil) {
    const picked = [...document.querySelectorAll("[data-abil]")].filter((x) => x.checked).map((x) => x.dataset.abil);
    if (e.target.dataset.abil === "shell" && e.target.checked
        && !confirm("Let this department run shell commands? It needs this to use API keys.")) { e.target.checked = false; return; }
    const r = await fetch(`/api/dept/${viewArg}/abilities`, { method: "PUT", headers: { "Content-Type": "application/json", "X-Token": TOKEN }, body: JSON.stringify({ abilities: picked }) });
    if (r.status === 428) {
      const why = (await r.json()).detail;
      if (confirm(why + "\n\nAllow it anyway?")) {
        try { await api(`/api/dept/${viewArg}/abilities`, { method: "PUT", body: { abilities: picked, accept_unsandboxed: true } }); } catch (err) { banner(errText(err)); }
      }
    } else if (!r.ok) banner(errText(new Error(await r.text())));
    return render();
  }
  if (e.target.dataset.svdept !== undefined) {
    await api(`/api/services/${encodeURIComponent(e.target.dataset.svdept)}`, { method: "PATCH", body: { dept: e.target.value } });
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { if (!$("#modal").classList.contains("hide")) $("#modal").classList.add("hide"); else Ask.close(); }
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { if (e.target.id === "ceoT") $("#ceoGo").click(); if (e.target.id === "dT") $("#dGo").click(); }
  if (e.key === "Enter" && e.target.id === "dvQ") $("#dvS").click();
  if (e.key === "Enter" && e.target.id === "kbQ") $("#kbS").click();
});

// ---------- Ask: quick questions about the company (one short call; nothing starts, nothing is sent) ----------
const Ask = {
  hist: (() => { try { return JSON.parse(sessionStorage.getItem("aoask") || "[]"); } catch (e) { return []; } })(),
  save() { try { sessionStorage.setItem("aoask", JSON.stringify(this.hist.slice(-20))); } catch (e) {} },
  chips: ["What do we charge?", "Who replied recently, and what's next with them?", "What should I reply to my latest client reply?", "What's waiting for me?"],
  draw() {
    const log = $("#askLog");
    log.innerHTML = (this.hist.length ? "" : `<p class="hint" style="margin:0">Answers come from what the company already knows: your business profile, prices and decisions, clients, recent results. It never sends anything.</p>
      <div class="askchips">${this.chips.map((c) => `<button class="ghost sm" data-askchip="${esc(c)}">${esc(c)}</button>`).join("")}</div>`)
      + this.hist.map((h, i) => `<div class="askm me">${esc(h.q)}</div>${h.a == null ? `<div class="askm bot hint">Thinking…</div>` : `<div class="askm bot"><div class="md">${md(h.a)}</div>
        <div class="row"><button class="ghost sm" data-askcopy="${i}">Copy</button>${h.draft ? `<button class="ghost sm" data-askob="${i}">Put in Outbox</button>` : ""}${h.tokens ? `<span class="hint">${fmt(h.tokens)} tokens</span>` : ""}</div></div>`}`).join("");
    log.scrollTop = log.scrollHeight;
  },
  open() { $("#askP").classList.remove("hide"); $("#askOpen").classList.add("hide"); this.draw(); $("#askT").focus(); },
  close() { $("#askP").classList.add("hide"); $("#askOpen").classList.remove("hide"); },
  async send(q) {
    q = (q || "").trim(); if (!q) return $("#askT").focus();
    const prev = this.hist.filter((h) => h.a != null).slice(-6).map((h) => ({ q: h.q, a: h.a }));
    const h = { q, a: null }; this.hist.push(h); $("#askT").value = ""; this.draw();
    try { const r = await api("/api/ask", { method: "POST", body: { question: q, history: prev } }); Object.assign(h, { a: r.answer, draft: r.draft, tokens: r.tokens }); }
    catch (err) { h.a = "Couldn't answer: " + errText(err); }
    this.save(); this.draw();
  },
};
document.addEventListener("click", async (e) => {
  const el = e.target.closest("#askOpen,#askClose,#askGo,[data-askchip],[data-askcopy],[data-askob]"); if (!el) return;
  e.stopPropagation();
  if (el.id === "askOpen") return Ask.open();
  if (el.id === "askClose") return Ask.close();
  if (el.id === "askGo") return Ask.send($("#askT").value);
  if (el.dataset.askchip) return Ask.send(el.dataset.askchip);
  const h = Ask.hist[+(el.dataset.askcopy ?? el.dataset.askob)]; if (!h) return;
  if (el.dataset.askcopy !== undefined) { navigator.clipboard?.writeText(h.a); el.textContent = "Copied"; return; }
  try { await api("/api/ask/outbox", { method: "POST", body: { text: h.a } }); el.textContent = "In the Outbox"; el.disabled = true; loadState().catch(() => {}); }
  catch (err) { el.textContent = errText(err).slice(0, 60); }
}, true);
document.addEventListener("keydown", (e) => { if (e.target.id === "askT" && e.key === "Enter" && !e.shiftKey) { e.preventDefault(); Ask.send(e.target.value); } });

// light polling only while something is moving (no websocket overhead)
setInterval(async () => {
  if (document.hidden || !$("#modal").classList.contains("hide")) return;
  if (view === "office") { loadState().catch(() => {}); return; }   // the floor polls /api/office itself
  const active = document.activeElement;
  if (active && ["TEXTAREA", "INPUT", "SELECT"].includes(active.tagName)) { loadState().catch(() => {}); return; }
  const busy = S && S.depts.some((d) => d.counts.running || d.counts.queued || d.counts.waiting);
  if (busy || ["today", "dept", "projects", "project"].includes(view)) render();
}, 4000);
render();
if (HASH[0] === "ask") { Ask.open(); if (HASH[1]) Ask.send(HASH.slice(1).join("/")); }
