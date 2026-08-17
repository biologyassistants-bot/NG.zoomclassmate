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
  const p = el("passInput").value.trim();
  if (!p) return;
  const res = await fetch(`${API}/api/teacher/login`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ passcode: p })
  });
  if (res.ok) {
    state.passcode = p;
    el("passErr").classList.add("hidden");
    show("teacher");
    loadTeacherRecordings();
  } else {
    el("passErr").classList.remove("hidden");
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
  if (!list.length) { box.innerHTML = '<div class="roster-empty">No students yet. Import a sheet or add one above.</div>'; return; }
  list.forEach(s => {
    const row = document.createElement("div"); row.className = "student-row";
    const courses = (s.courses && s.courses.length) ? s.courses.join(", ") : "— no course —";
    row.innerHTML = `
      <div>
        <div class="s-name">${escapeHtml(s.name)}</div>
        ${s.email ? `<div class="s-email">${escapeHtml(s.email)}</div>` : ""}
        <div class="s-email">Courses: ${escapeHtml(courses)}</div>
      </div>
      <div class="pin-badge">${s.has_password ? "🔑 set" : "no pw"}</div>
      <button class="s-btn danger remove">Remove</button>`;
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
    row.innerHTML = `
      <div>
        <input class="title-in" value="${escapeHtml(r.title)}" />
        <div class="orig">Original: ${escapeHtml(r.original_title)} · ${escapeHtml(r.date || "")} · ${r.segments} lines</div>
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
    let visible = r.visible;

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
