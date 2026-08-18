const API = "";
let state = { name: "", recordings: [], current: null, passcode: "", token: "" };

function el(id) { return document.getElementById(id); }
function escapeHtml(s) { return (s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }

function show(sectionId) {
  ["landing", "gate", "teacherGate", "main", "teacher"].forEach(s => el(s).classList.add("hidden"));
  el(sectionId).classList.remove("hidden");
}

// ---------- markdown-lite for bot answers ----------
function renderBotText(text) {
  let t = escapeHtml(text);
  t = t.replace(/\(?\bat\s+(\d{1,2}:\d{2}(?::\d{2})?)\)?/g, '<span class="ts-chip">⏱ $1</span>');
  t = t.replace(/\((\d{1,2}:\d{2}(?::\d{2})?)\)/g, '<span class="ts-chip">⏱ $1</span>');
  t = t.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  t = t.replace(/^#{1,6}\s*(.+)$/gm, "<h3>$1</h3>");
  const lines = t.split("\n");
  let html = "", inUl = false, inOl = false;
  for (let line of lines) {
    const ul = line.match(/^\s*[-*]\s+(.*)/);
    const ol = line.match(/^\s*\d+\.\s+(.*)/);
    if (ul) { if (!inUl) { html += "<ul>"; inUl = true; } if (inOl) { html += "</ol>"; inOl = false; } html += `<li>${ul[1]}</li>`; }
    else if (ol) { if (!inOl) { html += "<ol>"; inOl = true; } if (inUl) { html += "</ul>"; inUl = false; } html += `<li>${ol[1]}</li>`; }
    else { if (inUl) { html += "</ul>"; inUl = false; } if (inOl) { html += "</ol>"; inOl = false; } html += line + "\n"; }
  }
  if (inUl) html += "</ul>"; if (inOl) html += "</ol>";
  return html;
}

// ---------- landing / role nav ----------
el("roleStudent").addEventListener("click", () => { show("gate"); el("emailInput").focus(); });
el("roleTeacher").addEventListener("click", () => { show("teacherGate"); el("passInput").focus(); });
document.querySelectorAll("[data-back]").forEach(b => b.addEventListener("click", () => show(b.dataset.back)));

// ---------- student gate (email + password against roster) ----------
async function enter() {
  const email = el("emailInput").value.trim();
  const password = el("passwordInput").value;
  const errEl = el("studentErr");
  errEl.classList.add("hidden");
  if (!email || !password) { errEl.textContent = "Please enter your email and password."; errEl.classList.remove("hidden"); return; }
  try {
    const res = await fetch(`${API}/api/student/login`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email, password })
    });
    const data = await res.json();
    if (!res.ok || !data.ok) { errEl.textContent = data.error || "Login failed."; errEl.classList.remove("hidden"); return; }
    state.name = data.name;
    state.token = data.token;
    el("whoName").textContent = data.name;
    el("passwordInput").value = "";
    show("main");
    loadRecordings();
  } catch (e) {
    errEl.textContent = "Couldn't reach the server. Try again.";
    errEl.classList.remove("hidden");
  }
}
el("enterBtn").addEventListener("click", enter);
el("emailInput").addEventListener("keydown", e => { if (e.key === "Enter") el("passwordInput").focus(); });
el("passwordInput").addEventListener("keydown", e => { if (e.key === "Enter") enter(); });

// ---------- teacher gate ----------
async function teacherLogin() {
  const btn = el("passBtn");
  const errEl = el("passErr");
  const p = el("passInput").value.trim();
  if (!p) { errEl.textContent = "Please type your passcode."; errEl.classList.remove("hidden"); return; }

  // visible feedback so a slow/asleep server never looks "frozen"
  const originalLabel = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Connecting… (server may be waking up)";
  errEl.classList.add("hidden");

  try {
    const res = await fetch(`${API}/api/teacher/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ passcode: p })
    });

    let data = {};
    try { data = await res.json(); } catch (e) { /* non-JSON response */ }

    if (res.ok && data.ok) {
      state.passcode = p;
      errEl.classList.add("hidden");
      show("teacher");
      loadTeacherRecordings();
    } else if (res.status === 401) {
      errEl.textContent = "Wrong passcode. The default is teach123.";
      errEl.classList.remove("hidden");
    } else {
      errEl.textContent = `Login failed (server responded ${res.status}). Please try again.`;
      errEl.classList.remove("hidden");
    }
  } catch (e) {
    // network error, CORS, timeout, or server still asleep
    errEl.textContent = "Couldn't reach the server. It may be waking up — wait ~30s and try again, or check your connection.";
    errEl.classList.remove("hidden");
  } finally {
    btn.disabled = false;
    btn.textContent = originalLabel;
  }
}
el("passBtn").addEventListener("click", teacherLogin);
el("passInput").addEventListener("keydown", e => { if (e.key === "Enter") teacherLogin(); });

// ================= STUDENT VIEW =================
async function loadRecordings() {
  const res = await fetch(`${API}/api/recordings`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token: state.token })
  });
  const data = await res.json();
  state.recordings = data.recordings || [];
  renderRecList(state.recordings);
}

function renderRecList(list) {
  const box = el("recList");
  box.innerHTML = "";
  if (!list.length) { box.innerHTML = '<div class="rec-item"><div class="d">No recordings available yet.</div></div>'; return; }
  // group by unit
  const groups = {};
  list.forEach(r => { const u = r.unit || "Unassigned"; (groups[u] = groups[u] || []).push(r); });
  Object.keys(groups).sort().forEach(unit => {
    const g = document.createElement("div");
    g.className = "unit-group";
    g.innerHTML = `<div class="unit-label">${escapeHtml(unit)}</div>`;
    groups[unit].forEach(r => {
      const item = document.createElement("div");
      item.className = "rec-item" + (state.current && state.current.id === r.id ? " active" : "");
      const notesBadge = r.has_notes ? ' <span class="notes-dot" title="This class has extra notes">📎</span>' : "";
      item.innerHTML = `<div class="t">${escapeHtml(r.title)}${notesBadge}</div><div class="d">${escapeHtml(r.date || "")} · ${r.segments} lines</div>`;
      item.addEventListener("click", () => selectRecording(r));
      g.appendChild(item);
    });
    box.appendChild(g);
  });
}

el("search").addEventListener("input", e => {
  const q = e.target.value.toLowerCase();
  renderRecList(state.recordings.filter(r => (r.title || "").toLowerCase().includes(q) || (r.unit || "").toLowerCase().includes(q)));
});

function selectRecording(r) {
  state.current = r;
  const q = el("search").value.toLowerCase();
  renderRecList(state.recordings.filter(x => (x.title || "").toLowerCase().includes(q) || (x.unit || "").toLowerCase().includes(q)));
  el("emptyState").classList.add("hidden");
  el("workspace").classList.remove("hidden");
  el("wsTitle").textContent = r.title;
  el("wsMeta").innerHTML = `${escapeHtml(r.unit)} · ${escapeHtml(r.date || "")} · ${r.segments} transcript lines` +
    (r.has_notes ? ` · <span class="notes-flag">📎 includes extra class notes</span>` : "");
  el("chat").innerHTML = "";
  const notesLine = r.has_notes
    ? " This class also has extra notes from your teacher that I can draw on."
    : "";
  addBot(`Hi ${state.name}! Ask me anything about **${r.title}**. I'll answer using only what was said in this recording (with timestamps).${notesLine} 😊`);
}

function addUser(text) { const d = document.createElement("div"); d.className = "msg user"; d.textContent = text; el("chat").appendChild(d); scrollChat(); }
function addBot(text) { const d = document.createElement("div"); d.className = "msg bot"; d.innerHTML = renderBotText(text); el("chat").appendChild(d); scrollChat(); }
function addTyping() { const d = document.createElement("div"); d.className = "typing"; d.id = "typing"; d.innerHTML = 'ClassMate is reading the recording <span class="dot">●</span><span class="dot">●</span><span class="dot">●</span>'; el("chat").appendChild(d); scrollChat(); }
function removeTyping() { const t = el("typing"); if (t) t.remove(); }
function scrollChat() { const c = el("chat"); c.scrollTop = c.scrollHeight; }

el("askForm").addEventListener("submit", async e => {
  e.preventDefault();
  const q = el("questionInput").value.trim();
  if (!q || !state.current) return;
  el("questionInput").value = "";
  el("askBtn").disabled = true;
  addUser(q); addTyping();
  try {
    const res = await fetch(`${API}/api/ask`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ recording_id: state.current.id, question: q, language: "English", token: state.token })
    });
    const data = await res.json();
    removeTyping();
    if (data.error) addBot("Sorry, something went wrong: " + data.error);
    else addBot(data.answer);
  } catch (err) { removeTyping(); addBot("Sorry, I couldn't reach the server. Please try again."); }
  el("askBtn").disabled = false; el("questionInput").focus();
});

// ---------- quiz ----------
let quizData = null;
el("quizBtn").addEventListener("click", generateQuiz);
el("closeQuiz").addEventListener("click", () => el("quizModal").classList.add("hidden"));
el("retryQuiz").addEventListener("click", generateQuiz);

async function generateQuiz() {
  if (!state.current) return;
  el("quizModal").classList.remove("hidden");
  el("submitQuiz").classList.add("hidden"); el("retryQuiz").classList.add("hidden");
  el("quizBody").innerHTML = '<div class="typing">Creating your quiz from the recording <span class="dot">●</span><span class="dot">●</span><span class="dot">●</span></div>';
  try {
    const res = await fetch(`${API}/api/quiz`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ recording_id: state.current.id, num_questions: 5, language: "English", token: state.token })
    });
    const data = await res.json();
    if (data.error || !data.questions) { el("quizBody").innerHTML = '<p>Sorry, I could not build a quiz for this recording. Try another one.</p>'; return; }
    quizData = data.questions; renderQuiz();
  } catch (e) { el("quizBody").innerHTML = '<p>Could not reach the server. Please try again.</p>'; }
}

function renderQuiz() {
  const body = el("quizBody"); body.innerHTML = "";
  quizData.forEach((q, qi) => {
    const block = document.createElement("div"); block.className = "q-block";
    let opts = "";
    q.options.forEach((opt, oi) => { opts += `<label class="opt" data-q="${qi}" data-o="${oi}"><input type="radio" name="q${qi}" value="${oi}" />${escapeHtml(opt)}</label>`; });
    block.innerHTML = `<div class="q-title"><span class="q-num">Q${qi + 1}</span>${escapeHtml(q.question)}</div>${opts}<div class="explain hidden" id="exp${qi}"></div>`;
    body.appendChild(block);
  });
  body.querySelectorAll(".opt").forEach(l => l.addEventListener("click", () => {
    const qi = l.dataset.q;
    body.querySelectorAll(`.opt[data-q="${qi}"]`).forEach(x => x.classList.remove("sel"));
    l.classList.add("sel");
  }));
  el("submitQuiz").classList.remove("hidden"); el("retryQuiz").classList.add("hidden");
}

el("submitQuiz").addEventListener("click", () => {
  let score = 0;
  quizData.forEach((q, qi) => {
    const chosen = document.querySelector(`input[name="q${qi}"]:checked`);
    const ci = chosen ? parseInt(chosen.value) : -1;
    document.querySelectorAll(`.opt[data-q="${qi}"]`).forEach((lab, oi) => {
      lab.style.pointerEvents = "none";
      if (oi === q.answer_index) lab.classList.add("correct");
      else if (oi === ci) lab.classList.add("wrong");
    });
    if (ci === q.answer_index) score++;
    const exp = el(`exp${qi}`);
    const ts = q.timestamp ? `<span class="ts-chip">⏱ ${escapeHtml(q.timestamp)}</span>` : "";
    exp.innerHTML = `✅ <strong>Answer:</strong> ${escapeHtml(q.options[q.answer_index])} ${ts}<br>${escapeHtml(q.explanation || "")}`;
    exp.classList.remove("hidden");
  });
  const head = document.createElement("div"); head.className = "score";
  const pct = Math.round(100 * score / quizData.length);
  head.textContent = `You scored ${score} / ${quizData.length}  (${pct}%) ${pct >= 80 ? "🎉" : pct >= 50 ? "👍" : "📖 keep reviewing!"}`;
  el("quizBody").prepend(head);
  el("submitQuiz").classList.add("hidden"); el("retryQuiz").classList.remove("hidden");
});

// ================= TEACHER VIEW =================
el("tabRecordings").addEventListener("click", () => switchTab("Recordings"));
el("tabStudents").addEventListener("click", () => switchTab("Students"));
el("tabQuestions").addEventListener("click", () => switchTab("Questions"));
el("tabAnalytics").addEventListener("click", () => switchTab("Analytics"));
el("tabSettings").addEventListener("click", () => switchTab("Settings"));

function switchTab(name) {
  el("tabRecordings").classList.toggle("active", name === "Recordings");
  el("tabStudents").classList.toggle("active", name === "Students");
  el("tabQuestions").classList.toggle("active", name === "Questions");
  el("tabAnalytics").classList.toggle("active", name === "Analytics");
  el("tabSettings").classList.toggle("active", name === "Settings");
  el("teacherRecordings").classList.toggle("hidden", name !== "Recordings");
  el("teacherStudents").classList.toggle("hidden", name !== "Students");
  el("teacherQuestions").classList.toggle("hidden", name !== "Questions");
  el("teacherAnalytics").classList.toggle("hidden", name !== "Analytics");
  el("teacherSettings").classList.toggle("hidden", name !== "Settings");
  if (name === "Questions") loadQuestions();
  if (name === "Recordings") { loadTeacherRecordings(); loadStats(); }
  if (name === "Students") loadStudents();
  if (name === "Analytics") loadAnalytics();
}

// ---------- toast notifications ----------
function toast(msg, kind = "info", ms = 3200) {
  const host = el("toastHost");
  if (!host) return;
  const t = document.createElement("div");
  t.className = `toast toast-${kind}`;
  t.textContent = msg;
  host.appendChild(t);
  requestAnimationFrame(() => t.classList.add("show"));
  setTimeout(() => { t.classList.remove("show"); setTimeout(() => t.remove(), 300); }, ms);
}

// ---------- dashboard stats ----------
async function loadStats() {
  const bar = el("statsBar");
  if (bar && !bar.dataset.loaded) bar.innerHTML = '<div class="stat-skeleton"></div>'.repeat(5);
  try {
    const res = await fetch(`${API}/api/teacher/stats`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ passcode: state.passcode })
    });
    const s = await res.json();
    if (!res.ok) return;
    const cards = [
      { label: "Recordings", value: s.recordings_total, sub: `${s.recordings_visible} visible` },
      { label: "Transcribed", value: `${s.recordings_transcribed}/${s.recordings_total}`, sub: s.recordings_missing ? `${s.recordings_missing} missing` : "all done ✓" },
      { label: "Courses", value: s.courses, sub: s.recordings_unassigned ? `${s.recordings_unassigned} unassigned` : "all assigned" },
      { label: "Students", value: s.students, sub: "on roster" },
      { label: "Questions", value: s.questions_total, sub: `${s.questions_this_week} this week` },
    ];
    bar.innerHTML = cards.map(c =>
      `<div class="stat-card"><div class="stat-value">${escapeHtml(String(c.value))}</div><div class="stat-label">${escapeHtml(c.label)}</div><div class="stat-sub">${escapeHtml(c.sub)}</div></div>`
    ).join("");
    bar.dataset.loaded = "1";
  } catch (e) { /* non-blocking */ }
}

// ---------- question analytics ----------
async function loadAnalytics() {
  const box = el("analyticsBody");
  box.innerHTML = '<div class="stat-skeleton" style="height:120px;"></div>';
  try {
    const res = await fetch(`${API}/api/teacher/analytics`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ passcode: state.passcode })
    });
    const a = await res.json();
    if (!res.ok) { box.innerHTML = `<div class="q-empty">${escapeHtml(a.error || "Could not load analytics.")}</div>`; return; }
    if (!a.total) { box.innerHTML = '<div class="q-empty">No questions yet — analytics will appear once students start asking.</div>'; return; }
    const maxKw = Math.max(...a.top_keywords.map(k => k[1]), 1);
    const kwHtml = a.top_keywords.map(([w, c]) =>
      `<div class="bar-row"><span class="bar-label">${escapeHtml(w)}</span><span class="bar-track"><span class="bar-fill" style="width:${Math.round(c / maxKw * 100)}%"></span></span><span class="bar-num">${c}</span></div>`
    ).join("");
    const studentsHtml = a.top_students.map(([n, c]) => `<li>${escapeHtml(n)} <span class="pill">${c}</span></li>`).join("");
    const courseHtml = a.by_course.map(([n, c]) => `<li>${escapeHtml(n)} <span class="pill">${c}</span></li>`).join("");
    box.innerHTML = `
      <div class="analytics-grid">
        <div class="analytics-card">
          <h3>Most-asked keywords</h3>
          <div class="bars">${kwHtml || '<p class="meta">Not enough data yet.</p>'}</div>
        </div>
        <div class="analytics-card">
          <h3>Most active students</h3>
          <ul class="rank-list">${studentsHtml || '<li class="meta">No data</li>'}</ul>
        </div>
        <div class="analytics-card">
          <h3>Questions by course</h3>
          <ul class="rank-list">${courseHtml || '<li class="meta">No data</li>'}</ul>
        </div>
      </div>`;
  } catch (e) { box.innerHTML = '<div class="q-empty">Network error loading analytics.</div>'; }
}

// ---------- exports ----------
function downloadUrl(path) {
  const url = `${API}${path}${path.includes("?") ? "&" : "?"}passcode=${encodeURIComponent(state.passcode)}`;
  const a = document.createElement("a");
  a.href = url; a.download = ""; document.body.appendChild(a); a.click(); a.remove();
}
el("exportQCsv").addEventListener("click", () => { downloadUrl("/api/teacher/export/questions.csv"); toast("Downloading questions CSV…", "info"); });
el("exportQPdf").addEventListener("click", () => { downloadUrl("/api/teacher/export/questions.pdf"); toast("Downloading questions PDF…", "info"); });
el("exportRosterCsv").addEventListener("click", () => { downloadUrl("/api/teacher/export/roster.csv"); toast("Downloading roster CSV…", "info"); });

// ---------- merge duplicate accounts ----------
el("dedupeBtn").addEventListener("click", async () => {
  const body = el("dedupeBody");
  el("dedupeApply").classList.add("hidden");
  body.innerHTML = '<p class="meta">Scanning for duplicate emails…</p>';
  el("dedupeModal").classList.remove("hidden");
  try {
    const res = await fetch(`${API}/api/teacher/students/dedupe-preview`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ passcode: state.passcode })
    });
    const data = await res.json();
    if (!res.ok) { body.innerHTML = `<p class="ed-status err">${escapeHtml(data.error || "Could not scan.")}</p>`; return; }
    if (!data.groups || !data.groups.length) {
      body.innerHTML = '<p class="dedupe-clean">✅ No duplicate accounts found — every email is unique.</p>';
      return;
    }
    let html = `<p class="meta">Found <strong>${data.duplicate_emails}</strong> email(s) with duplicates. `
      + `Merging keeps the <strong>oldest</strong> account, combines all courses into it, and deletes `
      + `<strong>${data.accounts_to_delete}</strong> extra account(s).</p>`;
    data.groups.forEach(g => {
      html += `<div class="dedupe-group">
        <div class="dg-email">📧 ${escapeHtml(g.email)} <span class="dg-count">${g.duplicate_count} accounts</span></div>
        <div class="dg-line"><strong>Keep:</strong> ${escapeHtml(g.keep.name || "(no name)")} ${g.keep.has_password ? "🔑" : "⚠️ no pw"}</div>
        <div class="dg-line"><strong>Delete:</strong> ${g.will_delete.map(d => escapeHtml(d.name || "(no name)")).join(", ")}</div>
        <div class="dg-line"><strong>Final courses:</strong> ${g.merged_courses.length ? g.merged_courses.map(c => `<span class="course-chip">${escapeHtml(c)}</span>`).join(" ") : "— none —"}</div>
      </div>`;
    });
    body.innerHTML = html;
    el("dedupeApply").classList.remove("hidden");
  } catch (e) { body.innerHTML = '<p class="ed-status err">Network error while scanning.</p>'; }
});
el("closeDedupe").addEventListener("click", () => el("dedupeModal").classList.add("hidden"));
el("dedupeCancel").addEventListener("click", () => el("dedupeModal").classList.add("hidden"));
el("dedupeApply").addEventListener("click", async () => {
  el("dedupeApply").disabled = true;
  try {
    const res = await fetch(`${API}/api/teacher/students/dedupe-apply`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ passcode: state.passcode })
    });
    const data = await res.json();
    if (res.ok) {
      toast(`Merged ${data.merged_emails} email(s), deleted ${data.deleted_accounts} duplicate account(s).`, "success", 6000);
      el("dedupeModal").classList.add("hidden");
      loadStudents(); loadStats();
    } else { toast(data.error || "Merge failed.", "error"); }
  } catch (e) { toast("Network error during merge.", "error"); }
  finally { el("dedupeApply").disabled = false; }
});

// ---------- bulk: transcribe all missing ----------
el("transcribeAllBtn").addEventListener("click", async () => {
  const missing = teacherRecordings.filter(r => !r.segments);
  if (!missing.length) { toast("All recordings already have transcripts 🎉", "success"); return; }
  if (!confirm(`Generate transcripts for ${missing.length} recording(s) with none? This runs one at a time and can take several minutes.`)) return;
  const btn = el("transcribeAllBtn");
  btn.disabled = true;
  const prog = el("bulkProgress"), fill = el("bulkBarFill"), txt = el("bulkProgressText");
  prog.classList.remove("hidden");
  let done = 0, ok = 0, failed = 0;
  for (const r of missing) {
    txt.textContent = `Transcribing ${done + 1} of ${missing.length}: ${r.title}…`;
    try {
      const res = await fetch(`${API}/api/teacher/transcribe`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ passcode: state.passcode, id: r.id })
      });
      const data = await res.json();
      if (res.ok) ok++; else failed++;
    } catch (e) { failed++; }
    done++;
    fill.style.width = `${Math.round(done / missing.length * 100)}%`;
  }
  txt.textContent = `Finished: ${ok} transcribed, ${failed} failed.`;
  toast(`Transcribe-all done: ${ok} succeeded${failed ? `, ${failed} failed` : ""}.`, failed ? "error" : "success", 5000);
  btn.disabled = false;
  setTimeout(() => prog.classList.add("hidden"), 4000);
  await loadTeacherRecordings(); loadStats();
});

// ---------- import one specific recording ----------
el("importOneBtn").addEventListener("click", async () => {
  const ref = el("importOneInput").value.trim();
  const status = el("importOneStatus");
  if (!ref) { status.textContent = "Paste a Zoom Meeting ID/UUID or a recording link first."; return; }
  const btn = el("importOneBtn");
  btn.disabled = true;
  status.textContent = "Contacting Zoom and importing…";
  try {
    const res = await fetch(`${API}/api/teacher/import-one`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ passcode: state.passcode, ref })
    });
    const data = await res.json();
    if (!res.ok) { status.textContent = ""; toast(data.error || "Import failed.", "error", 5000); }
    else {
      status.textContent = "";
      const t = data.recording ? data.recording.title : "recording";
      const note = data.has_transcript ? "with its Zoom transcript" : "— no Zoom transcript, use “Generate transcript”";
      toast(`Imported “${t}” ${note}. It starts hidden until you assign a course & make it visible.`, "success", 6000);
      el("importOneInput").value = "";
      await loadTeacherRecordings(); loadStats();
    }
  } catch (e) { status.textContent = ""; toast("Network error during import.", "error"); }
  finally { btn.disabled = false; }
});
el("importOneInput").addEventListener("keydown", e => { if (e.key === "Enter") el("importOneBtn").click(); });

// ---------- bulk: delete unassigned ----------
el("deleteUnassignedBtn").addEventListener("click", async () => {
  const unassigned = teacherRecordings.filter(r => (r.unit || "Unassigned") === "Unassigned");
  if (!unassigned.length) { toast("There are no unassigned recordings.", "info"); return; }
  if (!confirm(`Permanently delete ${unassigned.length} unassigned recording(s)? This cannot be undone.`)) return;
  try {
    const res = await fetch(`${API}/api/teacher/recordings/delete-unassigned`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ passcode: state.passcode })
    });
    const data = await res.json();
    if (res.ok) { toast(`Deleted ${data.deleted} unassigned recording(s).`, "success"); await loadTeacherRecordings(); loadStats(); }
    else toast(data.error || "Delete failed.", "error");
  } catch (e) { toast("Network error during delete.", "error"); }
});

// ---------- roster management ----------
async function loadStudents() {
  const res = await fetch(`${API}/api/teacher/students`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ passcode: state.passcode })
  });
  const data = await res.json();
  renderStudents(data.students || []);
}

function renderStudents(list) {
  const box = el("studentList"); box.innerHTML = "";
  if (!list.length) { box.innerHTML = '<div class="roster-empty">No students yet. Import a sheet or add one above.</div>'; return; }
  list.forEach(s => {
    const row = document.createElement("div"); row.className = "student-row";
    const courses = (s.courses && s.courses.length) ? s.courses.join(", ") : "— no course —";
    const pw = s.has_password
      ? '<span class="pw-status ok">🔑 Password set</span>'
      : '<span class="pw-status warn">⚠️ No password</span>';
    row.innerHTML = `
      <div>
        <div class="s-name">${escapeHtml(s.name)}</div>
        ${s.email ? `<div class="s-email">${escapeHtml(s.email)}</div>` : ""}
        <div class="s-email">Courses: ${escapeHtml(courses)}</div>
        ${pw}
      </div>
      <button class="s-btn edit-btn">✏️ Edit</button>`;
    row.querySelector(".edit-btn").addEventListener("click", () => openStudentEditor(s));
    box.appendChild(row);
  });
}

// ---------- student editor modal ----------
let editingStudent = null;
let editCourses = [];

function renderEditCourses() {
  const wrap = el("edCourseList");
  wrap.innerHTML = "";
  if (!editCourses.length) { wrap.innerHTML = '<span class="ed-no-course">No courses — this student will see nothing until you add one.</span>'; return; }
  editCourses.forEach((c, i) => {
    const chip = document.createElement("span");
    chip.className = "course-chip";
    chip.innerHTML = `${escapeHtml(c)} <button type="button" class="chip-x" title="Remove">✕</button>`;
    chip.querySelector(".chip-x").addEventListener("click", () => { editCourses.splice(i, 1); renderEditCourses(); });
    wrap.appendChild(chip);
  });
}

function openStudentEditor(s) {
  editingStudent = s;
  editCourses = Array.isArray(s.courses) ? [...s.courses] : [];
  el("edName").value = s.name || "";
  el("edEmail").value = s.email || "";
  el("edPassword").value = "";
  el("edCourseInput").value = "";
  el("edStatus").textContent = "";
  renderEditCourses();
  el("studentModal").classList.remove("hidden");
}
function closeStudentEditor() { el("studentModal").classList.add("hidden"); editingStudent = null; }

el("closeStudentModal").addEventListener("click", closeStudentEditor);
el("edCancel").addEventListener("click", closeStudentEditor);

el("edCourseAddBtn").addEventListener("click", () => {
  const v = el("edCourseInput").value.trim();
  if (!v) return;
  if (!editCourses.some(c => c.toLowerCase() === v.toLowerCase())) editCourses.push(v);
  el("edCourseInput").value = "";
  renderEditCourses();
});
el("edCourseInput").addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); el("edCourseAddBtn").click(); } });

el("edGenPw").addEventListener("click", () => {
  const abc = "abcdefghijkmnpqrstuvwxyz23456789";
  let p = ""; for (let i = 0; i < 8; i++) p += abc[Math.floor(Math.random() * abc.length)];
  el("edPassword").value = p;
});

el("edSave").addEventListener("click", async () => {
  if (!editingStudent) return;
  const payload = {
    passcode: state.passcode,
    id: editingStudent.id,
    name: el("edName").value.trim(),
    email: el("edEmail").value.trim(),
    courses: editCourses,
  };
  const np = el("edPassword").value.trim();
  if (np) payload.new_password = np;
  el("edSave").disabled = true;
  try {
    const res = await fetch(`${API}/api/teacher/students/update`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (res.ok) {
      let msg = "Student updated.";
      if (np) msg += " New password set — they'll need to log in again.";
      toast(msg, "success", 5000);
      closeStudentEditor();
      loadStudents();
    } else {
      el("edStatus").textContent = data.error || "Could not save.";
      el("edStatus").className = "ed-status err";
    }
  } catch (e) { el("edStatus").textContent = "Network error while saving."; el("edStatus").className = "ed-status err"; }
  finally { el("edSave").disabled = false; }
});

el("edDelete").addEventListener("click", async () => {
  if (!editingStudent) return;
  if (!confirm(`Delete ${editingStudent.name || editingStudent.email}? This permanently removes their account and access.`)) return;
  try {
    const res = await fetch(`${API}/api/teacher/students/remove`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ passcode: state.passcode, id: editingStudent.id })
    });
    if (res.ok) { toast("Student deleted.", "success"); closeStudentEditor(); loadStudents(); }
    else { const d = await res.json(); toast(d.error || "Could not delete.", "error"); }
  } catch (e) { toast("Network error during delete.", "error"); }
});

el("addStudentBtn").addEventListener("click", async () => {
  const name = el("newStudentName").value.trim();
  const email = el("newStudentEmail").value.trim();
  const password = el("newStudentPassword").value;
  const courses = el("newStudentCourses").value.trim();
  const err = el("addStudentErr"); err.classList.add("hidden");
  if (!email) { err.textContent = "Enter a student email."; err.classList.remove("hidden"); return; }
  const res = await fetch(`${API}/api/teacher/students/add`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ passcode: state.passcode, name, email, password, courses })
  });
  const data = await res.json();
  if (!res.ok || data.error) { err.textContent = data.error || "Could not add student."; err.classList.remove("hidden"); return; }
  el("newStudentName").value = ""; el("newStudentEmail").value = ""; el("newStudentPassword").value = ""; el("newStudentCourses").value = "";
  if (data.merged) toast(data.message || "Student updated.", "success", 5000);
  else toast("Student added.", "success");
  loadStudents();
});

// ---------- Excel import ----------
el("importBtn").addEventListener("click", async () => {
  const fileInput = el("excelFile");
  const msg = el("importMsg"); const err = el("importErr");
  msg.classList.add("hidden"); err.classList.add("hidden");
  if (!fileInput.files.length) { err.textContent = "Choose an .xlsx file first."; err.classList.remove("hidden"); return; }
  const fd = new FormData();
  fd.append("file", fileInput.files[0]);
  fd.append("passcode", state.passcode);
  try {
    const res = await fetch(`${API}/api/teacher/students/import`, { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok || data.error) { err.textContent = data.error || "Import failed."; err.classList.remove("hidden"); return; }
    msg.textContent = `Imported ✓  ${data.added} added, ${data.updated} updated.`;
    msg.classList.remove("hidden");
    fileInput.value = "";
    loadStudents();
  } catch (e) {
    err.textContent = "Couldn't reach the server. Try again.";
    err.classList.remove("hidden");
  }
});

let teacherRecordings = [];

async function loadTeacherRecordings() {
  const res = await fetch(`${API}/api/teacher/recordings`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ passcode: state.passcode })
  });
  const data = await res.json();
  // remember original array order as "date added" (Zoom ingest appends newest last)
  teacherRecordings = (data.recordings || []).map((r, i) => ({ ...r, _order: i }));
  populateCourseFilter(teacherRecordings);
  applyRecFilters();
}

function populateCourseFilter(list) {
  const sel = el("recCourseFilter");
  const current = sel.value;
  const units = Array.from(new Set(list.map(r => r.unit || "Unassigned"))).sort();
  sel.innerHTML = '<option value="">All courses</option>' +
    units.map(u => `<option value="${escapeHtml(u)}">${escapeHtml(u)}</option>`).join("");
  // keep the previous selection if it still exists
  if (current && units.includes(current)) sel.value = current;
}

function applyRecFilters() {
  const q = (el("recSearch").value || "").toLowerCase();
  const course = el("recCourseFilter").value;
  const type = el("recTypeFilter").value;
  const sort = el("recSort").value;

  let list = teacherRecordings.filter(r => {
    const matchesCourse = !course || (r.unit || "Unassigned") === course;
    const matchesType = !type || (r.source || "meeting") === type;
    const matchesText = !q ||
      (r.title || "").toLowerCase().includes(q) ||
      (r.original_title || "").toLowerCase().includes(q) ||
      (r.unit || "").toLowerCase().includes(q);
    return matchesCourse && matchesType && matchesText;
  });

  const byDate = (a, b) => String(a.date || "").localeCompare(String(b.date || ""));
  const byCourse = (a, b) => (a.unit || "Unassigned").localeCompare(b.unit || "Unassigned");
  const byTitle = (a, b) => (a.title || "").localeCompare(b.title || "");
  const sorters = {
    added_desc: (a, b) => b._order - a._order,
    added_asc: (a, b) => a._order - b._order,
    date_desc: (a, b) => byDate(b, a),
    date_asc: (a, b) => byDate(a, b),
    course_az: (a, b) => byCourse(a, b) || byTitle(a, b),
    title_az: (a, b) => byTitle(a, b),
  };
  list.sort(sorters[sort] || sorters.added_desc);

  el("recCount").textContent = `${list.length} of ${teacherRecordings.length} recording${teacherRecordings.length === 1 ? "" : "s"}`;
  renderTeacherRecordings(list);
}

el("recSearch").addEventListener("input", applyRecFilters);
el("recCourseFilter").addEventListener("change", applyRecFilters);
el("recTypeFilter").addEventListener("change", applyRecFilters);
el("recSort").addEventListener("change", applyRecFilters);

function renderTeacherRecordings(list) {
  const box = el("tRecList"); box.innerHTML = "";
  if (!list.length) { box.innerHTML = '<div class="roster-empty">No recordings match your filter.</div>'; return; }
  list.forEach(r => {
    const row = document.createElement("div"); row.className = "t-rec";
    const isWebinar = (r.source || "meeting") === "webinar";
    const badge = `<span class="type-badge ${isWebinar ? "webinar" : "meeting"}">${isWebinar ? "📢 Webinar" : "🎥 Meeting"}</span>`;
    const noTranscript = !r.segments;
    const btnLabel = noTranscript ? "Generate transcript" : "Re-transcribe";
    const topicsHtml = (r.topics && r.topics.length)
      ? `<div class="rec-topics">${r.topics.map(t => `<span class="topic-tag">${escapeHtml(t)}</span>`).join("")}</div>` : "";
    const summaryHtml = r.summary ? `<div class="rec-summary">${escapeHtml(r.summary)}</div>` : "";
    const summaryLabel = r.summary ? "Regenerate summary" : "Generate summary";
    row.innerHTML = `
      <div>
        <input class="title-in" value="${escapeHtml(r.title)}" />
        <div class="orig">${badge}Original: ${escapeHtml(r.original_title)} · ${escapeHtml(r.date || "")} · <span class="seg-count">${r.segments}</span> lines</div>
        <div class="transcribe-wrap">
          <button class="transcribe-btn ${noTranscript ? "needs" : ""}">${btnLabel}</button>
          <button class="summary-btn ghost-sm" ${noTranscript ? "disabled title='Transcribe first'" : ""}>${summaryLabel}</button>
          <button class="delete-btn danger-btn">🗑️ Delete</button>
          <span class="transcribe-status"></span>
        </div>
        ${summaryHtml}
        ${topicsHtml}
        <div class="notes-box">
          <div class="notes-head">📎 Teacher notes <span class="notes-hint">(used by the AI to answer; students can't view or download them)</span></div>
          <div class="notes-list"></div>
          <div class="notes-attach">
            <select class="note-lib-select"><option value="">Attach existing note ▾</option></select>
            <button class="note-attach-btn ghost-sm">Attach</button>
          </div>
          <div class="notes-add">
            <input type="file" class="note-file" accept=".pdf,.docx,.txt,.md" />
            <button class="note-upload-btn ghost-sm">Upload new note</button>
            <span class="note-status"></span>
          </div>
        </div>
      </div>
      <input class="unit-in" value="${escapeHtml(r.unit)}" placeholder="Unit / class" />
      <div>
        <div class="vis-toggle"><span class="switch ${r.visible ? "on" : ""}"></span><span class="vis-label">${r.visible ? "Visible" : "Hidden"}</span></div>
        <div class="saved-flash">saved ✓</div>
      </div>`;
    const titleIn = row.querySelector(".title-in");
    const unitIn = row.querySelector(".unit-in");
    const sw = row.querySelector(".switch");
    const visLabel = row.querySelector(".vis-label");
    const flash = row.querySelector(".saved-flash");
    const transBtn = row.querySelector(".transcribe-btn");
    const transStatus = row.querySelector(".transcribe-status");
    const segCount = row.querySelector(".seg-count");
    const summaryBtn = row.querySelector(".summary-btn");
    const deleteBtn = row.querySelector(".delete-btn");
    let visible = r.visible;

    // generate / regenerate summary + topics
    summaryBtn.addEventListener("click", async () => {
      if (summaryBtn.disabled) return;
      summaryBtn.disabled = true;
      const prev = summaryBtn.textContent;
      summaryBtn.textContent = "Summarizing…";
      try {
        const res = await fetch(`${API}/api/teacher/summary`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ passcode: state.passcode, id: r.id })
        });
        const data = await res.json();
        if (!res.ok) { toast(data.error || "Summary failed.", "error"); summaryBtn.textContent = prev; }
        else {
          toast("Summary generated ✓", "success");
          const cached = teacherRecordings.find(x => x.id === r.id);
          if (cached) { cached.summary = data.summary; cached.topics = data.topics; }
          applyRecFilters();
        }
      } catch (e) { toast("Network error.", "error"); summaryBtn.textContent = prev; }
      finally { summaryBtn.disabled = false; }
    });

    // delete single recording
    deleteBtn.addEventListener("click", async () => {
      if (!confirm(`Permanently delete "${r.title}"? This cannot be undone.`)) return;
      deleteBtn.disabled = true;
      try {
        const res = await fetch(`${API}/api/teacher/recordings/delete`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ passcode: state.passcode, id: r.id })
        });
        const data = await res.json();
        if (res.ok) {
          toast(`Deleted "${r.title}".`, "success");
          teacherRecordings = teacherRecordings.filter(x => x.id !== r.id);
          applyRecFilters(); loadStats();
        } else { toast(data.error || "Delete failed.", "error"); deleteBtn.disabled = false; }
      } catch (e) { toast("Network error during delete.", "error"); deleteBtn.disabled = false; }
    });

    // ---- teacher notes: render + attach-from-library + upload + detach ----
    const notesList = row.querySelector(".notes-list");
    const noteFile = row.querySelector(".note-file");
    const noteUploadBtn = row.querySelector(".note-upload-btn");
    const noteStatus = row.querySelector(".note-status");
    const noteLibSelect = row.querySelector(".note-lib-select");
    const noteAttachBtn = row.querySelector(".note-attach-btn");

    function renderNotes(notes) {
      notesList.innerHTML = "";
      if (!notes || !notes.length) { notesList.innerHTML = '<span class="notes-empty">No notes attached yet.</span>'; return; }
      notes.forEach(n => {
        const item = document.createElement("div");
        item.className = "note-item";
        item.innerHTML = `<span class="note-name">📄 ${escapeHtml(n.filename)}</span><span class="note-meta">${n.chars.toLocaleString()} chars</span><button class="note-del danger-btn">Detach</button>`;
        item.querySelector(".note-del").addEventListener("click", async () => {
          if (!confirm(`Detach "${n.filename}" from this recording?\n(The note stays in your library and on any other recordings using it.)`)) return;
          try {
            const res = await fetch(`${API}/api/teacher/notes/detach`, {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ passcode: state.passcode, id: r.id, note_id: n.id })
            });
            const data = await res.json();
            if (res.ok) {
              toast("Note detached from this recording.", "success");
              const cached = teacherRecordings.find(x => x.id === r.id);
              if (cached) cached.notes = data.recording.notes;
              renderNotes(data.recording.notes);
            } else toast(data.error || "Could not detach note.", "error");
          } catch (e) { toast("Network error.", "error"); }
        });
        notesList.appendChild(item);
      });
    }
    renderNotes(r.notes);

    // populate the library dropdown (notes not already attached here)
    async function refreshLibDropdown() {
      try {
        const res = await fetch(`${API}/api/teacher/notes/library`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ passcode: state.passcode })
        });
        const data = await res.json();
        const attachedIds = new Set((r.notes || []).map(n => n.id));
        noteLibSelect.innerHTML = '<option value="">Attach existing note ▾</option>';
        (data.library || []).filter(n => !attachedIds.has(n.id)).forEach(n => {
          const opt = document.createElement("option");
          opt.value = n.id;
          opt.textContent = `${n.filename} (used by ${n.used_by})`;
          noteLibSelect.appendChild(opt);
        });
      } catch (e) { /* silent */ }
    }
    refreshLibDropdown();

    noteAttachBtn.addEventListener("click", async () => {
      const nid = noteLibSelect.value;
      if (!nid) { toast("Pick a note from the list first.", "info"); return; }
      try {
        const res = await fetch(`${API}/api/teacher/notes/attach`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ passcode: state.passcode, id: r.id, note_id: nid })
        });
        const data = await res.json();
        if (res.ok) {
          toast("Shared note attached ✓", "success");
          const cached = teacherRecordings.find(x => x.id === r.id);
          if (cached) cached.notes = data.recording.notes;
          renderNotes(data.recording.notes);
          refreshLibDropdown();
        } else toast(data.error || "Could not attach note.", "error");
      } catch (e) { toast("Network error.", "error"); }
    });

    noteUploadBtn.addEventListener("click", async () => {
      const f = noteFile.files[0];
      if (!f) { toast("Choose a PDF, DOCX or TXT file first.", "info"); return; }
      noteUploadBtn.disabled = true;
      noteStatus.textContent = "Uploading & reading…";
      const fd = new FormData();
      fd.append("passcode", state.passcode);
      fd.append("id", r.id);
      fd.append("file", f);
      try {
        const res = await fetch(`${API}/api/teacher/notes/upload`, { method: "POST", body: fd });
        const data = await res.json();
        if (res.ok) {
          noteStatus.textContent = "";
          const fileKB = Math.round((data.file_bytes || 0) / 1024);
          const textKB = Math.max(1, Math.round((data.text_chars || 0) / 1024));
          let msg = `Note "${data.note.filename}" attached ✓ (${fileKB} KB file → ${textKB} KB text stored)`;
          if (data.trimmed) msg += ` — very long, trimmed to the first ${textKB} KB of text.`;
          toast(msg, "success", data.trimmed ? 6000 : 4000);
          const cached = teacherRecordings.find(x => x.id === r.id);
          if (cached) cached.notes = data.recording.notes;
          renderNotes(data.recording.notes);
          refreshLibDropdown();
          noteFile.value = "";
        } else { noteStatus.textContent = ""; toast(data.error || "Upload failed.", "error"); }
      } catch (e) { noteStatus.textContent = ""; toast("Network error during upload.", "error"); }
      finally { noteUploadBtn.disabled = false; }
    });

    transBtn.addEventListener("click", async () => {
      if (transBtn.disabled) return;
      transBtn.disabled = true;
      transStatus.className = "transcribe-status working";
      transStatus.textContent = "Transcribing… this can take a few minutes.";
      try {
        const res = await fetch(`${API}/api/teacher/transcribe`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ passcode: state.passcode, id: r.id })
        });
        const data = await res.json();
        if (!res.ok) {
          transStatus.className = "transcribe-status error";
          transStatus.textContent = data.error || "Transcription failed.";
        } else {
          segCount.textContent = data.segments;
          transStatus.className = "transcribe-status ok";
          transStatus.textContent = `Done — ${data.segments} lines.`;
          transBtn.textContent = "Re-transcribe";
          transBtn.classList.remove("needs");
          const cached = teacherRecordings.find(x => x.id === r.id);
          if (cached) cached.segments = data.segments;
        }
      } catch (e) {
        transStatus.className = "transcribe-status error";
        transStatus.textContent = "Network error — please try again.";
      } finally {
        transBtn.disabled = false;
      }
    });

    function save() {
      fetch(`${API}/api/teacher/update`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ passcode: state.passcode, id: r.id, display_title: titleIn.value, unit: unitIn.value, visible })
      }).then(() => {
        flash.classList.add("show"); setTimeout(() => flash.classList.remove("show"), 1200);
        // keep local state in sync so filters/sort stay accurate
        const cached = teacherRecordings.find(x => x.id === r.id);
        if (cached) { cached.title = titleIn.value; cached.unit = unitIn.value; cached.visible = visible; }
        populateCourseFilter(teacherRecordings);
      });
    }
    titleIn.addEventListener("change", save);
    unitIn.addEventListener("change", save);
    sw.addEventListener("click", () => {
      visible = !visible;
      sw.classList.toggle("on", visible);
      visLabel.textContent = visible ? "Visible" : "Hidden";
      save();
    });
    box.appendChild(row);
  });
}

async function loadQuestions() {
  const res = await fetch(`${API}/api/teacher/questions`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ passcode: state.passcode })
  });
  const data = await res.json();
  const box = el("qLog"); box.innerHTML = "";
  const qs = data.questions || [];
  if (!qs.length) { box.innerHTML = '<div class="q-empty">No questions yet. They\'ll show up here as students ask.</div>'; return; }
  qs.forEach(q => {
    const row = document.createElement("div"); row.className = "q-row";
    row.innerHTML = `<div class="q-q">${escapeHtml(q.question)}</div>
      <div class="q-meta"><span class="q-who">${escapeHtml(q.student)}</span> · ${escapeHtml(q.recording_title)} (${escapeHtml(q.unit)}) · ${escapeHtml(q.time)}</div>`;
    box.appendChild(row);
  });
}

el("savePass").addEventListener("click", async () => {
  const np = el("newPass").value.trim();
  if (!np) return;
  const res = await fetch(`${API}/api/teacher/passcode`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ passcode: state.passcode, new_passcode: np })
  });
  if (res.ok) { state.passcode = np; el("newPass").value = ""; el("passSaved").classList.remove("hidden"); setTimeout(() => el("passSaved").classList.add("hidden"), 2000); }
});

// ---------- sign out ----------
function signOut() {
  state.name = ""; state.token = ""; state.passcode = ""; state.current = null; state.recordings = [];
  const p = el("passInput"); if (p) p.value = "";
  const em = el("emailInput"); if (em) em.value = "";
  const pw = el("passwordInput"); if (pw) pw.value = "";
  show("landing");
}
el("studentSignOut").addEventListener("click", signOut);
el("teacherSignOut").addEventListener("click", signOut);

// ---------- logo upload ----------
el("saveLogo").addEventListener("click", async () => {
  const fileInput = el("logoFile");
  const ok = el("logoSaved"); const err = el("logoErr");
  ok.classList.add("hidden"); err.classList.add("hidden");
  if (!fileInput.files.length) { err.textContent = "Choose an image file first."; err.classList.remove("hidden"); return; }
  const fd = new FormData();
  fd.append("file", fileInput.files[0]);
  fd.append("passcode", state.passcode);
  try {
    const res = await fetch(`${API}/api/teacher/logo`, { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok || data.error) { err.textContent = data.error || "Upload failed."; err.classList.remove("hidden"); return; }
    ok.classList.remove("hidden"); setTimeout(() => ok.classList.add("hidden"), 2500);
    fileInput.value = "";
    // cache-bust so the new logo shows immediately everywhere
    applyLogo(data.logo + "?t=" + Date.now());
  } catch (e) {
    err.textContent = "Couldn't reach the server. Try again.";
    err.classList.remove("hidden");
  }
});

// ---------- branding (apply custom logo if one was uploaded) ----------
function applyLogo(url) {
  if (!url) return;
  const imgTag = `<img src="${url}" alt="logo" />`;
  document.querySelectorAll(".logo, .logo-sm").forEach(node => { node.innerHTML = imgTag; });
  const preview = el("logoPreview");
  if (preview) { preview.src = url; preview.classList.remove("hidden"); }
}

async function loadBranding() {
  try {
    const res = await fetch(`${API}/api/branding`);
    const data = await res.json();
    if (data.logo) applyLogo(data.logo);
  } catch (e) { /* ignore — keep emoji fallback */ }
}
loadBranding();
// ---------- Import existing Zoom cloud recordings (backfill) ----------
// Adds a button to the Recordings pane that pulls recordings already stored in
// Zoom cloud (meetings + webinars) into the app via POST /api/teacher/backfill.
// Self-contained + CSP-safe (no inline handlers). Injects its own button so it
// works regardless of small differences in index.html markup.
(function setupImportRecordings() {
  function injectImportButton() {
    const pane = el("teacherRecordings");
    if (!pane) return;

    // If the button already exists in the HTML markup, just bind the handler
    // once and stop (don't create a duplicate).
    let btn = el("importRecBtn");
    if (btn) {
      if (!btn._backfillBound) {
        btn.addEventListener("click", runBackfill);
        btn._backfillBound = true;
      }
      return;
    }

    // Otherwise create the button + status span dynamically (fallback).
    const head = pane.querySelector(".pane-head") || pane;

    const bar = document.createElement("div");
    bar.className = "import-rec-bar";
    bar.style.cssText = "display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-top:12px;";

    btn = document.createElement("button");
    btn.id = "importRecBtn";
    btn.className = "primary";
    btn.type = "button";
    btn.textContent = "⬇️ Import cloud recordings";

    const status = document.createElement("span");
    status.id = "importRecStatus";
    status.className = "meta";
    status.style.cssText = "font-size:0.9em;color:#555;";

    bar.appendChild(btn);
    bar.appendChild(status);
    head.appendChild(bar);

    btn.addEventListener("click", runBackfill);
    btn._backfillBound = true;
  }

  async function runBackfill() {
    const btn = el("importRecBtn");
    const status = el("importRecStatus");
    if (!btn) return;
    if (!state.passcode) { if (status) status.textContent = "Please sign in as teacher first."; return; }

    btn.disabled = true;
    const original = btn.textContent;
    btn.textContent = "Importing…";
    if (status) status.textContent = "Contacting Zoom and importing recordings — this can take a minute…";

    try {
      const res = await fetch(`${API}/api/teacher/backfill`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ passcode: state.passcode }),
      });
      const data = await res.json();
      if (!res.ok) {
        if (status) status.textContent = "Import failed: " + (data.error || ("HTTP " + res.status));
      } else {
        const added = data.added || 0;
        const skipped = data.skipped_already_present || 0;
        const found = data.found || 0;
        if (status) {
          status.textContent =
            `Done — imported ${added} new recording${added === 1 ? "" : "s"} ` +
            `(${skipped} already present, ${found} found in Zoom). ` +
            `New ones are hidden until you make them visible.`;
        }
        if (typeof loadTeacherRecordings === "function") loadTeacherRecordings();
      }
    } catch (e) {
      if (status) status.textContent = "Import failed: could not reach the server. Try again.";
    } finally {
      btn.disabled = false;
      btn.textContent = original;
    }
  }

  const tabRec = el("tabRecordings");
  if (tabRec) tabRec.addEventListener("click", () => setTimeout(injectImportButton, 0));
  document.addEventListener("DOMContentLoaded", injectImportButton);
  injectImportButton();
})();
