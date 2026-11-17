// Office floor: rooms, desks, walking Claude avatars, CEO cabin, drawers. Pure client-side animation (0 tokens).
const Office = (() => {
  const POS = new Map();       // avatar key -> {x,y,tx,ty,wait,seated} (percent of room floor), survives re-renders
  const SEEN = new Set();      // dispatch ids already animated
  const LAST = new Map();      // avatar key -> last finished task id shown
  let data = null, mounted = false, raf = 0, poll = 0, lastT = 0, first = true;
  const SCARF = ["#2f6f5e", "#3b5bab", "#8a3fa0", "#b7791f", "#c2410c", "#0e7490"];
  const SPEED = 46;            // px per second

  // ---------- avatar art (pixel Claude) ----------
  function svg({ head = false, ceo = false, scarf = "#2f6f5e" } = {}) {
    return `<svg viewBox="0 0 18 17" shape-rendering="crispEdges" aria-hidden="true">
      ${head || ceo ? `<g fill="#e8b23a"><rect x="5" y="0" width="8" height="2"/><rect x="5" y="-1" width="1" height="1"/><rect x="8.5" y="-1.5" width="1" height="1.5"/><rect x="12" y="-1" width="1" height="1"/></g>` : ""}
      <rect class="bd" x="3" y="2" width="12" height="10"/>
      <rect class="sh" x="3" y="11" width="12" height="1"/>
      <rect class="bd arm l" x="1" y="6" width="2" height="3"/>
      <rect class="bd arm r" x="15" y="6" width="2" height="3"/>
      <rect class="eye" x="6" y="5" width="1.6" height="2.4"/><rect class="eye" x="10.4" y="5" width="1.6" height="2.4"/>
      ${ceo ? `<rect x="8.4" y="9" width="1.2" height="3" fill="#1d1b18"/><rect x="3" y="8.6" width="12" height=".8" fill="#fff" opacity=".7"/>`
            : `<rect x="3" y="9.4" width="12" height="1.2" fill="${scarf}"/>`}
      <rect class="bd lg a" x="4" y="12" width="1.5" height="3.5"/><rect class="bd lg b" x="7" y="12" width="1.5" height="3.5"/>
      <rect class="bd lg a" x="9.5" y="12" width="1.5" height="3.5"/><rect class="bd lg b" x="12.5" y="12" width="1.5" height="3.5"/>
    </svg>`;
  }
  const deskX = (i, n) => (100 * (i + 1)) / (n + 1);
  const DESK_TOP = 30, SEAT_Y = 37;
  const rnd = (a, b) => a + Math.random() * (b - a);

  // ---------- skeleton ----------
  function roomHTML(d) {
    const n = d.members.length;
    return `<div class="room" data-room="${d.id}">
      <div class="room-h"><span class="rn" data-goto="${d.id}" title="Open department view">${esc(d.name)}</span><span class="rc" data-rc></span>
        <button class="sm" data-headcmd="${d.id}" title="Command the whole department through its head">Command dept</button></div>
      <div class="room-f" data-floor="${d.id}">
        <div class="wall"></div><div class="board" data-goto="${d.id}" data-board>0 done today</div><div class="plant"></div>
        ${d.members.map((m, i) => `<div class="desk" data-desk="${d.id}/${m.id}" style="left:${deskX(i, n)}%;top:${DESK_TOP}%"><span class="plate">${esc(m.name)}</span><div class="mon"></div></div>`).join("")}
      </div></div>`;
  }
  function html() {
    const depts = S.depts;
    const rooms = depts.map(roomHTML);
    rooms.splice(Math.min(1, rooms.length), 0, `<div class="room cabin" data-room="ceo">
      <div class="room-h"><span class="rn" data-ceo>Chief of Staff</span><span class="rc" data-rc>routes free · plans on Sonnet</span>
        <button class="sm" data-ceo>Command company</button></div>
      <div class="room-f" data-floor="ceo" data-ceo>
        <div class="rug"></div><div class="glass"></div><div class="stats" data-stats data-gotoview="log">–</div>
        <div class="bigdesk"><span class="plate">CHIEF OF STAFF</span></div>
      </div></div>`);
    const onboard = S.designed ? "" : `<div class="card onboard" id="onboard">
        <h2 style="margin-top:0">Welcome. What should your company do?</h2>
        <p class="sub">Say what you're building, for whom, and where you are. One Sonnet call designs the teams your startup needs: two to five, Haiku by default, stronger models only where a role needs them. You accept or edit them first.</p>
        <textarea id="dsT0" placeholder="e.g. Booking software for independent physio clinics that still book by phone. 3 paying clinics so far, 49 EUR a month. Goal: 10 clinics by January."></textarea>
        <div class="row"><button id="dsGo0">Design my company</button><button class="ghost" id="dsSkip">Start with a lean office</button>
          <span class="hint">Lean = Product, Growth and Operations, one or two people each. You can change it any time.</span></div></div>`;
    return `${onboard}<div class="office-head"><div><h1>${esc(S.company)}</h1>${S.about ? `<p class="hint" style="margin:0 0 4px">${esc(S.about)}</p>` : ""}
        <p class="sub" style="margin:0">Click any Claude to see its work and give it a task. Heads (👑) lead their team. The Chief of Staff turns your commands into small plans and hands them out.</p></div>
        <div class="legend"><span><i style="background:var(--acc)"></i>working</span><span><i style="background:var(--warn)"></i>queued</span><span><i style="background:var(--acc2)"></i>just finished</span><span><i style="background:var(--line)"></i>idle (0 tokens)</span></div></div>
      <div class="floor" id="floor">${rooms.join("")}</div>`;
  }

  // ---------- avatars ----------
  function ensureAvatar(floor, key, opts) {
    let el = floor.querySelector(`[data-av="${CSS.escape(key)}"]`);
    if (el) return el;
    el = document.createElement("div");
    el.className = "av" + (opts.ceo ? " ceo" : "");
    el.dataset.av = key;
    el.innerHTML = `${svg(opts)}<div class="ring"></div><div class="tag">${esc(opts.name)}${opts.head ? " 👑" : ""}</div>`;
    floor.appendChild(el);
    if (!POS.has(key)) {
      const p = opts.ceo ? { x: 50, y: 50 } : { x: rnd(10, 90), y: rnd(58, 92) };
      POS.set(key, { ...p, tx: p.x, ty: p.y, wait: rnd(0.5, 3), seated: false });
    }
    return el;
  }

  // bubbles live on the floor layer (above desks) and follow their avatar each frame
  const bubOf = (el) => (el._bub && el._bub.isConnected ? el._bub : null);
  function bubble(el, htmlStr, cls = "") {
    let b = bubOf(el);
    if (!htmlStr) { b?.remove(); el._bub = null; return; }
    if (!b) { b = document.createElement("div"); el.parentElement.appendChild(b); el._bub = b; place(el); }
    const v = htmlStr + cls + (el.classList.contains("ceo") ? " ceo" : "");
    if (b.dataset.v === v) return;
    b.className = "bub " + cls + (el.classList.contains("ceo") ? " ceo" : ""); b.innerHTML = htmlStr; b.dataset.v = v;
  }
  function place(el) {
    const b = el._bub; if (!b) return;
    const W = el.parentElement.clientWidth, half = b.offsetWidth / 2 + 4;
    const x = (parseFloat(el.style.left) / 100) * W;
    b.style.left = Math.max(half, Math.min(W - half, x)) + "px"; b.style.top = el.style.top;
  }

  function apply() {
    if (!mounted || !data) return;
    const floorRoot = $("#floor"); if (!floorRoot) return;
    const now = data.now;
    data.depts.forEach((d) => {
      const room = floorRoot.querySelector(`[data-room="${d.id}"]`), floor = room?.querySelector("[data-floor]");
      if (!floor) return;
      let working = 0, queued = 0, tok = 0, done = 0;
      d.members.forEach((m, i) => {
        const key = `${d.id}/${m.id}`;
        const el = ensureAvatar(floor, key, { name: m.name, head: m.head, scarf: SCARF[i % SCARF.length] });
        el.dataset.dept = d.id; el.dataset.mid = m.id;
        const p = POS.get(key);
        const desk = floor.querySelector(`[data-desk="${CSS.escape(key)}"]`);
        tok += m.tokens; done += m.done;
        el.classList.toggle("working", m.state === "working");
        desk?.classList.toggle("on", m.state === "working");
        if (m.state === "working" || m.state === "queued") {
          if (m.state === "working") working++; else queued++;
          p.goDesk = true; p.tx = deskX(i, d.members.length) - 4; p.ty = SEAT_Y;
          bubble(el, m.state === "working" ? `${mBadge(m.task?.model || "")}${esc(m.task?.title || "working")}` : `⏳ queued: ${esc(m.task?.title || "")}`);
        } else {
          if (p.goDesk) { p.goDesk = false; p.seated = false; p.wait = rnd(0.3, 1.2); p.tx = rnd(10, 90); p.ty = rnd(58, 92); }
          const last = m.last;
          if (last && now - last.at < 90) {
            if (LAST.get(key) !== last.id && !first) flash(room);
            LAST.set(key, last.id);
            bubble(el, `${last.status === "failed" ? "✗" : "✓"} ${esc(last.line)}`, last.status === "failed" ? "bad" : "ok");
          } else if (!bubOf(el)?.classList.contains("idle")) bubble(el, null);
        }
      });
      room.classList.toggle("busy", working > 0);
      room.querySelector("[data-rc]").textContent = working || queued
        ? `${working} working${queued ? ` · ${queued} queued` : ""} · ${fmt(tok)} tok today`
        : `idle${tok ? ` · ${fmt(tok)} tok today` : ""}`;
      room.querySelector("[data-board]").textContent = `${done} done today`;
    });
    // CEO
    const cab = floorRoot.querySelector('[data-floor="ceo"]');
    if (cab) {
      const el = ensureAvatar(cab, "ceo", { name: "CEO", ceo: true });
      const p = POS.get("ceo"); p.tx = 50; p.ty = 47; p.goDesk = true;
      el.classList.toggle("working", data.ceo.state === "working");
      if (data.ceo.state === "working") bubble(el, `🧭 routing: ${esc(data.ceo.task?.title || "")}`);
      else if (data.routes[0]) bubble(el, `📨 ${esc(data.routes[0].title.slice(0, 40))}`, "ok");
      else bubble(el, null);
      const all = data.depts.flatMap((d) => d.members);
      const w = all.filter((m) => m.state === "working").length;
      const done = all.reduce((a, m) => a + m.done, 0), tk = all.reduce((a, m) => a + m.tokens, 0);
      cab.querySelector("[data-stats]").innerHTML = `${w} working now<br>${done} done today<br>${fmt(tk)} tok · activity log →`;
      floorRoot.querySelector('[data-room="ceo"] [data-rc]').textContent = data.ceo.state === "working" ? "planning…" : "routes free · plans on Sonnet";
    }
    // memos for fresh CEO dispatches
    data.dispatches.forEach((t) => {
      if (SEEN.has(t.id)) return;
      SEEN.add(t.id);
      if (!first) memo(t.dept, `${t.dept}/${t.member}`);
    });
    first = false;
  }

  function flash(room) { room.animate([{ boxShadow: "0 0 0 4px var(--acc2)" }, { boxShadow: "0 0 0 0 transparent" }], { duration: 1200 }); }

  function memo(dept, key) {
    const root = $("#floor"); if (!root) return;
    const from = root.querySelector('[data-room="ceo"] .bigdesk'), to = root.querySelector(`[data-av="${CSS.escape(key)}"]`) || root.querySelector(`[data-room="${dept}"]`);
    if (!from || !to) return;
    const R = root.getBoundingClientRect(), a = from.getBoundingClientRect(), b = to.getBoundingClientRect();
    const m = document.createElement("div");
    m.className = "memo"; m.textContent = "📨";
    m.style.left = `${a.left - R.left + a.width / 2}px`; m.style.top = `${a.top - R.top}px`;
    root.appendChild(m);
    requestAnimationFrame(() => requestAnimationFrame(() => { m.style.left = `${b.left - R.left + b.width / 2}px`; m.style.top = `${b.top - R.top}px`; }));
    setTimeout(() => { m.style.opacity = "0"; }, 1150);
    setTimeout(() => m.remove(), 1500);
  }

  // ---------- movement ----------
  const IDLE = ["☕", "💭", "📚", "…", "🙂"];
  function step(t) {
    if (!mounted) return;
    const dt = Math.min(0.05, (t - (lastT || t)) / 1000); lastT = t;
    document.querySelectorAll("#floor [data-floor]").forEach((floor) => {
      const W = floor.clientWidth, H = floor.clientHeight;
      floor.querySelectorAll(".av").forEach((el) => {
        const p = POS.get(el.dataset.av); if (!p) return;
        const dx = ((p.tx - p.x) / 100) * W, dy = ((p.ty - p.y) / 100) * H, dist = Math.hypot(dx, dy);
        if (dist > 1.5) {
          const k = Math.min(1, (SPEED * dt) / dist);
          p.x += (dx * k * 100) / W; p.y += (dy * k * 100) / H;
          el.classList.add("walking"); el.classList.remove("seated");
          if (Math.abs(dx) > 2) el.classList.toggle("flip", dx < 0);
          p.seated = false;
        } else {
          el.classList.remove("walking");
          if (p.goDesk) { p.seated = true; el.classList.add("seated"); el.classList.remove("flip"); }
          else if ((p.wait -= dt) <= 0) {
            p.wait = rnd(2, 7); p.tx = rnd(8, 92); p.ty = rnd(58, 93);
            if (Math.random() < 0.12 && !bubOf(el)) {
              bubble(el, IDLE[(Math.random() * IDLE.length) | 0], "idle");
              setTimeout(() => { if (bubOf(el)?.classList.contains("idle")) bubble(el, null); }, 2500);
            }
          }
        }
        el.style.left = p.x + "%"; el.style.top = p.y + "%";
        place(el);
      });
    });
    raf = requestAnimationFrame(step);
  }

  async function refresh() {
    if (!mounted || document.hidden) return;
    try { data = await api("/api/office"); apply(); } catch (e) {}
  }

  function mount() {
    mounted = true; first = !data;
    if (data) apply();
    refresh();
    cancelAnimationFrame(raf); lastT = 0; raf = requestAnimationFrame(step);
    clearInterval(poll); poll = setInterval(refresh, 2500);
  }
  function unmount() { mounted = false; cancelAnimationFrame(raf); clearInterval(poll); }

  // ---------- drawers ----------
  const drawer = () => $("#drawer");
  function openDrawer(inner) { $("#drbody").innerHTML = inner; drawer().classList.add("open"); }
  function closeDrawer() { drawer().classList.remove("open"); document.querySelectorAll(".av.sel").forEach((a) => a.classList.remove("sel")); }
  const tierName = ["", "Haiku", "Sonnet", "Opus"];

  async function openMember(dept, mid, focusDelegate = false) {
    const r = await api(`/api/member/${dept}/${mid}`);
    const idx = S.depts.find((d) => d.id === dept).members.findIndex((m) => m.id === mid);
    document.querySelectorAll(".av.sel").forEach((a) => a.classList.remove("sel"));
    document.querySelector(`[data-av="${CSS.escape(dept + "/" + mid)}"]`)?.classList.add("sel");
    openDrawer(`<div class="dr-h"><div class="dr-av">${svg({ head: r.head, scarf: SCARF[idx % SCARF.length] })}</div>
        <div><h2>${esc(r.name)}${r.head ? " 👑" : ""}</h2><div class="hint">${esc(r.role)} · ${esc(r.dept_name)}</div>
        <div style="margin-top:6px"><span class="chip">${tierName[r.tier]}${r.max_tier > r.tier ? " → " + tierName[r.max_tier] : ""}</span>${r.head ? `<span class="chip">head of ${r.team}</span>` : ""}</div></div></div>
      <div class="dr-sec"><h3>Today's report</h3><ul class="dr-rep">${r.report.map((l) => `<li>${esc(l)}</li>`).join("")}</ul></div>
      <div class="dr-sec"><h3>Give ${esc(r.name)} work</h3>
        <textarea id="drT" placeholder="${r.head ? `Tell ${esc(r.name)} what you need…` : `What should ${esc(r.name)} do?`}"></textarea>
        <div class="row"><select id="drTier"><option value="">model: auto</option><option value="1">Haiku</option><option value="2">Sonnet</option><option value="3">Opus</option></select>
          <button id="drAssign" data-dept="${dept}" data-mid="${mid}">Assign to ${esc(r.name)}</button>
</div>
        ${r.head ? `<div class="delegate ${focusDelegate ? "hl" : ""}"><b>Command all of ${esc(r.dept_name)}</b>
          <p class="hint" style="margin:4px 0 10px">${esc(r.name)} splits the command across ${r.team} teammates with one cheap Haiku call. Each teammate then works on the cheapest model that fits.</p>
          <button id="drDelegate" data-dept="${dept}">Delegate to team</button></div>` : ""}
      </div>
      <div class="dr-sec"><h3>Recent work</h3>${r.tasks.map((t) => `<div class="dr-task" data-task="${t.id}"><span>${esc(t.title)}</span>
        ${t.model ? mBadge(t.model) : ""}<span class="st ${t.status}">${t.status.replace("_", " ")}</span></div>`).join("") || `<p class="hint">No work yet.</p>`}</div>`);
    setTimeout(() => $("#drT")?.focus(), 260);
  }

  async function openCEO() {
    const routes = await api("/api/tasks?dept=ceo&limit=6");
    const all = (data?.depts || []).map((d) => {
      const done = d.members.reduce((a, m) => a + m.done, 0), tok = d.members.reduce((a, m) => a + m.tokens, 0);
      const w = d.members.filter((m) => m.state === "working").length;
      return `<li>${esc(d.name)}: ${done} done${w ? `, ${w} working` : ""} · ${fmt(tok)} tok</li>`;
    }).join("");
    openDrawer(`<div class="dr-h"><div class="dr-av">${svg({ ceo: true })}</div><div><h2>CEO Cabin</h2>
        <div class="hint">Your command goes to a Haiku router that splits it across departments.</div></div></div>
      <div class="dr-sec"><h3>Command the company</h3>
        <textarea id="drT" placeholder="e.g. Plan our launch: pricing tiers, landing copy, a competitor scan and a 4-week budget"></textarea>
        <div class="row"><button id="drCeo">Dispatch</button></div>
        <p class="hint">@marketing … sends straight to a department with 0 routing tokens · !opus forces a model</p></div>
      <div class="dr-sec"><h3>Company today</h3><ul class="dr-rep">${all}<li></li></ul>
        <div class="row"><button class="ghost sm" data-gotoview="log">Activity log</button><button class="ghost sm" data-gotoview="usage">Usage &amp; budget</button><button class="ghost sm" data-gotoview="kb">Knowledge base</button><button class="ghost sm" id="dsOpen">Redesign office</button></div></div>
      <div class="dr-sec"><h3>Recent routings</h3>${routes.map((t) => `<div class="dr-task" data-task="${t.id}"><span>${esc(t.title)}</span>
        <span class="hint">${esc(t.done_line || "")}</span><span class="st ${t.status}">${t.status}</span></div>`).join("") || `<p class="hint">None yet.</p>`}</div>`);
    setTimeout(() => $("#drT")?.focus(), 260);
  }

  // ---------- office designer ----------
  let lastDesc = "", lastPlan = null;
  function designerForm(text = "") {
    $("#mbody").innerHTML = `<h1 style="margin-top:0">Design your office</h1>
      <p class="sub">Describe what you do and what you want done. The designer builds the smallest team that covers it, so every task runs on the cheapest model that fits. Current tasks and the knowledge base are kept.</p>
      <p class="hint">Add your existing folders, one per line, and they'll be linked to the right department: <code>PC1 C:\\work\\app = backend code</code>, <code>PC2 D:\\brand = logos, copy docs</code></p>
      <textarea id="dsT" style="min-height:130px">${esc(text)}</textarea>
      <div class="row"><button id="dsGo">Design (1 Sonnet call)</button><span class="hint">You'll see a preview before anything changes.</span></div>`;
    $("#modal").classList.remove("hide");
    setTimeout(() => $("#dsT")?.focus(), 50);
  }
  function preview(plan) {
    const tn = ["", "Haiku", "Sonnet", "Opus"];
    $("#mbody").innerHTML = `<h1 style="margin-top:0">${esc(plan.name)}</h1><p class="sub">${esc(plan.about)}</p>
      <p class="hint">${esc(plan.summary)} · designed with ${fmt(plan.tokens)} tokens</p>
      <div class="grid" style="margin-top:12px">${plan.departments.map((d) => `<div class="card"><div class="rep-h"><span>${esc(d.name)}</span>
        <span class="hint">${d.tools.length ? "files/code" : "text-only"}</span></div><p class="hint" style="margin:0 0 8px">${esc(d.mission)}</p>
        ${d.members.map((m, i) => `<div style="display:flex;gap:8px;align-items:center;margin:5px 0"><span style="width:26px;height:24px">${svg({ head: i === 0, scarf: SCARF[i % SCARF.length] })}</span>
          <span style="flex:1"><b>${esc(m.name)}</b> <span class="hint">${esc(m.role)}</span></span><span class="chip">${tn[m.tier]}${m.max_tier > m.tier ? "→" + tn[m.max_tier] : ""}</span></div>`).join("")}</div>`).join("")}</div>
      ${(plan.goals || []).length ? `<h2>Goals</h2>${plan.goals.map((g) => `<div class="dr-task"><span>${esc(g.goal)}</span><span class="hint">${g.metric ? esc(g.metric.replace(/_/g, " ")) + (g.target != null ? " → " + g.target : "") : ""}</span></div>`).join("")}` : ""}
      ${(plan.projects || []).length ? `<h2>First projects</h2><p class="hint">They wait in your Inbox; nothing starts until you click.</p>${plan.projects.map((x) => `<div class="dr-task"><span><b>${esc(x.title)}</b> <span class="hint">${esc(x.command)}</span></span><span class="hint">${esc(x.dept)}</span></div>`).join("")}` : ""}
      ${(plan.routines || []).length ? `<h2>Routines</h2>${plan.routines.map((r) => `<div class="dr-task"><span>${esc(r.task)}</span><span class="hint">${esc(r.every)} · ${esc(r.dept)}</span></div>`).join("")}` : ""}
      ${(plan.sources || []).length ? `<h2>Folders to link</h2>${plan.sources.map((s) => `<div class="dr-task"><span>${esc(s.origin)}</span><span class="hint">→ ${esc(s.dept)}/sources/${esc(s.name)}${s.why ? " · " + esc(s.why) : ""}</span></div>`).join("")}` : ""}
      <div class="row" style="margin-top:16px"><button id="dsApply">Build this office</button><button class="ghost" id="dsBack">Change description</button></div>`;
  }
  let lastAnswers = {};
  async function design(text, btn, answers = null) {
    if (!text.trim()) return;
    lastDesc = text;
    if (answers === null) {                     // a few questions first (free), only what the description doesn't say
      const q = await api("/api/design/questions", { method: "POST", body: { text } });
      if (q.questions.length) {
        $("#mbody").innerHTML = `<h1 style="margin-top:0">A few quick questions</h1><p class="sub">Answer what you can; skip the rest. They shape the goals and first projects.</p>
          ${q.questions.map((x) => `<label class="hint" for="dq_${x.id}">${esc(x.q)}</label><input id="dq_${x.id}" data-dq="${x.id}" style="margin:4px 0 12px">`).join("")}
          <div class="row"><button id="dsAns">Design my office (1 Sonnet call)</button></div>`;
        $("#modal").classList.remove("hide"); return;
      }
      answers = {};
    }
    lastAnswers = answers;
    btn.disabled = true; btn.textContent = "Designing… (about 30s)";
    try {
      lastPlan = await api("/api/design", { method: "POST", body: { text, answers } });
      $("#modal").classList.remove("hide"); preview(lastPlan);
    } catch (e) { toast(errText(e).slice(0, 160)); btn.disabled = false; btn.textContent = "Try again"; }
  }

  function toast(msg) {
    const t = document.createElement("div"); t.className = "toast"; t.textContent = msg; document.body.appendChild(t);
    setTimeout(() => t.remove(), 2600);
  }

  async function onClick(e) {
    const ds = e.target.closest("#dsGo0,#dsSkip,#dsGo,#dsApply,#dsBack,#dsOpen,#dsAns");
    if (ds) {
      if (ds.id === "dsGo0") return design($("#dsT0").value, ds);
      if (ds.id === "dsGo") return design($("#dsT").value, ds);
      if (ds.id === "dsAns") {
        const answers = {}; document.querySelectorAll("[data-dq]").forEach((i) => { if (i.value.trim()) answers[i.dataset.dq] = i.value.trim(); });
        return design(lastDesc, ds, answers);
      }
      if (ds.id === "dsOpen") { closeDrawer(); return designerForm(lastDesc); }
      if (ds.id === "dsBack") return designerForm(lastDesc);
      if (ds.id === "dsSkip") { await api("/api/design/skip", { method: "POST", body: {} }); POS.clear(); data = null; return render(); }
      if (ds.id === "dsApply") {
        ds.disabled = true;
        const r = await api("/api/design/apply", { method: "POST", body: { plan: lastPlan, description: lastDesc } });
        $("#modal").classList.add("hide"); POS.clear(); data = null;
        toast(r.linked && r.linked.length ? `Company built. ${r.linked.length} folder(s) linked. Give it its first command on Today.` : "Company built. Give it its first command on Today.");
        return render();
      }
    }
    const av = e.target.closest(".av");
    if (av && av.dataset.av === "ceo") return openCEO();
    if (av) return openMember(av.dataset.dept, av.dataset.mid);
    const el = e.target.closest("[data-headcmd],[data-ceo],[data-goto],[data-gotoview],#drAssign,#drBk,#drDelegate,#drCeo,#drCeoBk,#drclose");
    if (!el) return false;
    if (el.id === "drclose") return closeDrawer();
    if (el.dataset.headcmd) { const d = S.depts.find((x) => x.id === el.dataset.headcmd); return openMember(d.id, d.members[0].id, true); }
    if (el.dataset.ceo !== undefined) return openCEO();
    if (el.dataset.gotoview) { closeDrawer(); return go(el.dataset.gotoview); }
    if (el.dataset.goto) { closeDrawer(); return go("dept", el.dataset.goto); }
    const text = $("#drT")?.value.trim(); if (!text) { $("#drT")?.focus(); return; }
    el.disabled = true;
    try {
      let r;
      if (el.id === "drAssign" || el.id === "drBk") {
        const tier = $("#drTier").value ? +$("#drTier").value : null;
        r = await api(`/api/dept/${el.dataset.dept}`, { method: "POST", body: { text, member: el.dataset.mid, tier } });
        toast("Assigned");
      } else if (el.id === "drDelegate") {
        r = await api(`/api/dept/${el.dataset.dept}/delegate`, { method: "POST", body: { text } });
        toast(r.error ? r.error : `Delegated to ${r.tasks.length} teammate task(s) · ${fmt(r.tokens)} tok`);
      } else {
        r = await api("/api/ceo", { method: "POST", body: { text } });
        toast(r.error ? r.error : `Routed to ${r.tasks.length} task(s) · ${fmt(r.tokens || 0)} tok`);
        if (!r.error && (r.answer || (r.actions || []).length)) {
          $("#mbody").innerHTML = `<h1 style="margin-top:0">CEO</h1>${r.answer ? `<div class="md">${md(r.answer)}</div>` : ""}
            ${(r.actions || []).map((a) => `<p class="hint" style="color:${a.startsWith("✓") ? "var(--acc2)" : "var(--bad)"}">${esc(a)}</p>`).join("")}
            <p class="hint">${r.tasks.length} task(s) started.</p>`;
          $("#modal").classList.remove("hide");
        }
      }
      if (!r.error) { closeDrawer(); refresh(); }
    } finally { el.disabled = false; }
  }
  document.addEventListener("click", (e) => { if (e.target.closest("#drawer, #floor, #modal, #onboard")) onClick(e); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeDrawer();
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && e.target.id === "drT") ($("#drCeo") || $("#drAssign"))?.click();
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });

  return { html, mount, unmount, refresh, openCEO };
})();
