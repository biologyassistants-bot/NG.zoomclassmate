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
el("roleStudent").addEventListener("click", () => { show("gate"); el("nameInput").focus(); });
el("roleTeacher").addEventListener("click", () => { show("teacherGate"); el("passInput").focus(); });
document.querySelectorAll("[data-back]").forEach(b => b.addEventListener("click", () => show(b.dataset.back)));

// ---------- student gate (name + PIN against roster) ----------
async function enter() {
  const n = el("nameInput").value.trim();
  const pin = el("pinInput").value.trim();
  const errEl = el("studentErr");
  errEl.classList.add("hidden");
  if (!n || !pin) { errEl.textContent = "Please enter your name and PIN."; errEl.classList.remove("hidden"); return; }
  try {
    const res = await fetch(`${API}/api/student/login`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: n, pin })
    });
    const data = await res.json();
    if (!res.ok || !data.ok) { errEl.textContent = data.error || "Login failed."; errEl.classList.remove("hidden"); return; }
    state.name = data.name;
    state.token = data.token;
    el("whoName").textContent = data.name;
    el("pinInput").value = "";
    show("main");
    loadRecordings();
  } catch (e) {
    errEl.textContent = "Couldn't reach the server. Try again.";
    errEl.classList.remove("hidden");
  }
}
el("enterBtn").addEventListener("click", enter);
el("nameInput").addEventListener("keydown", e => { if (e.key === "Enter") el("pinInput").focus(); });
el("pinInput").addEventListener("keydown", e => { if (e.key === "Enter") enter(); });

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
  const res = await fetch(`${API}/api/recordings`);
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
      item.innerHTML = `<div class="t">${escapeHtml(r.title)}</div><div class="d">${escapeHtml(r.date || "")} · ${r.segments} lines</div>`;
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
  el("wsMeta").textContent = `${r.unit} · ${r.date || ""} · ${r.segments} transcript lines`;
  el("chat").innerHTML = "";
  addBot(`Hi ${state.name}! Ask me anything about **${r.title}**. I'll answer using only what was said in this recording, with timestamps. 😊`);
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
      body: JSON.stringify({ recording_id: state.current.id, question: q, language: el("langSelect").value, token: state.token })
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
      body: JSON.stringify({ recording_id: state.current.id, num_questions: 5, language: el("langSelect").value, token: state.token })
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
el("tabSettings").addEventListener("click", () => switchTab("Settings"));

function switchTab(name) {
  el("tabRecordings").classList.toggle("active", name === "Recordings");
  el("tabStudents").classList.toggle("active", name === "Students");
  el("tabQuestions").classList.toggle("active", name === "Questions");
  el("tabSettings").classList.toggle("active", name === "Settings");
  el("teacherRecordings").classList.toggle("hidden", name !== "Recordings");
  el("teacherStudents").classList.toggle("hidden", name !== "Students");
  el("teacherQuestions").classList.toggle("hidden", name !== "Questions");
  el("teacherSettings").classList.toggle("hidden", name !== "Settings");
  if (name === "Questions") loadQuestions();
  if (name === "Recordings") loadTeacherRecordings();
  if (name === "Students") loadStudents();
}

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
  if (!list.length) { box.innerHTML = '<div class="roster-empty">No students yet. Add your first student above.</div>'; return; }
  list.forEach(s => {
    const row = document.createElement("div"); row.className = "student-row";
    row.innerHTML = `
      <div><div class="s-name">${escapeHtml(s.name)}</div>${s.email ? `<div class="s-email">${escapeHtml(s.email)}</div>` : ""}</div>
      <div class="pin-badge">${escapeHtml(s.pin)}</div>
      <button class="s-btn reset">New PIN</button>
      <button class="s-btn danger remove">Remove</button>`;
    row.querySelector(".reset").addEventListener("click", async () => {
      const r = await fetch(`${API}/api/teacher/students/reset_pin`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ passcode: state.passcode, id: s.id })
      });
      const d = await r.json();
      if (d.ok) { row.querySelector(".pin-badge").textContent = d.pin; }
    });
    row.querySelector(".remove").addEventListener("click", async () => {
      await fetch(`${API}/api/teacher/students/remove`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ passcode: state.passcode, id: s.id })
      });
      loadStudents();
    });
    box.appendChild(row);
  });
}

el("addStudentBtn").addEventListener("click", async () => {
  const name = el("newStudentName").value.trim();
  const email = el("newStudentEmail").value.trim();
  const err = el("addStudentErr"); err.classList.add("hidden");
  if (!name) { err.textContent = "Enter a student name."; err.classList.remove("hidden"); return; }
  const res = await fetch(`${API}/api/teacher/students/add`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ passcode: state.passcode, name, email })
  });
  const data = await res.json();
  if (!res.ok || data.error) { err.textContent = data.error || "Could not add student."; err.classList.remove("hidden"); return; }
  el("newStudentName").value = ""; el("newStudentEmail").value = "";
  loadStudents();
});

async function loadTeacherRecordings() {
  const res = await fetch(`${API}/api/teacher/recordings`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ passcode: state.passcode })
  });
  const data = await res.json();
  renderTeacherRecordings(data.recordings || []);
}

function renderTeacherRecordings(list) {
  const box = el("tRecList"); box.innerHTML = "";
  list.forEach(r => {
    const row = document.createElement("div"); row.className = "t-rec";
    const noTranscript = !r.segments;
    const btnLabel = noTranscript ? "Generate transcript" : "Re-transcribe";
    row.innerHTML = `
      <div>
        <input class="title-in" value="${escapeHtml(r.title)}" />
        <div class="orig">Original: ${escapeHtml(r.original_title)} · ${escapeHtml(r.date || "")} · <span class="seg-count">${r.segments}</span> lines</div>
        <div class="transcribe-wrap">
          <button class="transcribe-btn ${noTranscript ? "needs" : ""}">${btnLabel}</button>
          <span class="transcribe-status"></span>
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
    let visible = r.visible;

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
      }).then(() => { flash.classList.add("show"); setTimeout(() => flash.classList.remove("show"), 1200); });
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
