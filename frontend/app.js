const API = "";
let state = { 
  name: "", 
  recordings: [], 
  current: null, 
  passcode: "", 
  token: "", 
  role: "",
  chatHistory: {},    // { recording_id: [{ role: 'user'|'bot', text: str }] }
  flashcardDeck: [],  // [{ id, recording_id, front, back, interval, reps, dueDate }]
  courseSyllabi: {}
};

// Local tracking for student dashboard stats
let studentStats = JSON.parse(localStorage.getItem('studentStats_NGClassMate') || '{"questions":0, "quizzes":0}');

// Unique declaration for Study Plan
let currentStudyPlan = null;

// Global cache for teacher roster search
let teacherStudentsCache = [];

function el(id) { return document.getElementById(id); }
function escapeHtml(s) { return (s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }

function show(sectionId) {
  ["landing", "gate", "teacherGate", "main", "teacher"].forEach(s => {
    const elem = el(s);
    if (elem) elem.classList.add("hidden");
  });
  const target = el(sectionId);
  if (target) target.classList.remove("hidden");
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
    else if (ol) { if (!inOl) { html += "<ol>"; inUl = true; } if (inUl) { html += "</ul>"; inUl = false; } html += `<li>${ol[1]}</li>`; }
    else { if (inUl) { html += "</ul>"; inUl = false; } if (inOl) { html += "</ol>"; inOl = false; } html += line + "\n"; }
  }
  if (inUl) html += "</ul>"; if (inOl) html += "</ol>";
  return html;
}

function saveStudentStats() {
  localStorage.setItem('studentStats_NGClassMate', JSON.stringify(studentStats));
  saveServerProfile(); 
}

// ---------- Cross-Device Sync Helpers ----------
async function fetchServerProfile() {
  if (!state.token) return;
  try {
    const res = await fetch(`${API}/api/student/profile`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: state.token })
    });
    const data = await res.json();
    if (res.ok) {
      if (data.study_plan !== undefined && data.study_plan !== null) {
        currentStudyPlan = data.study_plan;
      }
      if (data.student_stats) {
        studentStats = data.student_stats;
        localStorage.setItem('studentStats_NGClassMate', JSON.stringify(studentStats));
      }
      if (data.chat_history) {
        state.chatHistory = data.chat_history;
      }
      if (data.flashcard_deck) {
        state.flashcardDeck = data.flashcard_deck;
      }
      updateAlertBadge();
    }
  } catch (e) {}
}

async function saveServerProfile() {
  if (!state.token) return;
  try {
    await fetch(`${API}/api/student/sync`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ 
        token: state.token, 
        study_plan: currentStudyPlan, 
        student_stats: studentStats,
        chat_history: state.chatHistory,
        flashcard_deck: state.flashcardDeck
      })
    });
    updateAlertBadge();
  } catch (e) {}
}

// ---------- Safe Auto-Login Check on Refresh ----------
document.addEventListener("DOMContentLoaded", () => {
  try {
    const savedStudentToken = localStorage.getItem("ng_studentToken");
    const savedStudentName = localStorage.getItem("ng_studentName");
    const savedTeacherPasscode = localStorage.getItem("ng_teacherPasscode");

    if (savedTeacherPasscode) {
      teacherLogin(savedTeacherPasscode);
    } else if (savedStudentToken) {
      state.token = savedStudentToken;
      state.name = savedStudentName || "Student";
      
      if(el("whoName")) el("whoName").textContent = state.name;
      if(el("dashName")) el("dashName").textContent = state.name;
      
      show("main");
      fetchServerProfile().then(() => {
        loadRecordings().then(() => {
          switchStudentTab("Dash");
        }).catch(() => {
          signOut();
        });
      });
    }
  } catch (e) {
    signOut();
  }
});

// ---------- landing / role nav ----------
if(el("roleStudent")) el("roleStudent").addEventListener("click", () => { show("gate"); if(el("emailInput")) el("emailInput").focus(); });
if(el("roleTeacher")) el("roleTeacher").addEventListener("click", () => { show("teacherGate"); if(el("passInput")) el("passInput").focus(); });
document.querySelectorAll("[data-back]").forEach(b => b.addEventListener("click", () => show(b.dataset.back)));

// ---------- student gate ----------
async function enter() {
  const email = el("emailInput").value.trim();
  const password = el("passwordInput").value;
  const errEl = el("studentErr");
  if(errEl) errEl.classList.add("hidden");
  if (!email || !password) { if(errEl) { errEl.textContent = "Please enter your email and password."; errEl.classList.remove("hidden"); } return; }
  try {
    const res = await fetch(`${API}/api/student/login`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email, password })
    });
    const data = await res.json();
    if (!res.ok || !data.ok) { if(errEl) { errEl.textContent = data.error || "Login failed."; errEl.classList.remove("hidden"); } return; }
    
    state.name = data.name;
    state.token = data.token;
    
    localStorage.setItem("ng_studentToken", data.token);
    localStorage.setItem("ng_studentName", data.name);
    
    if(el("whoName")) el("whoName").textContent = data.name;
    if(el("dashName")) el("dashName").textContent = data.name;
    
    if(el("passwordInput")) el("passwordInput").value = "";
    show("main");
    await fetchServerProfile();
    await loadRecordings();
    switchStudentTab("Dash");
  } catch (e) {
    if(errEl) {
      errEl.textContent = "Couldn't reach the server. Try again.";
      errEl.classList.remove("hidden");
    }
  }
}
if(el("enterBtn")) el("enterBtn").addEventListener("click", enter);
if(el("emailInput")) el("emailInput").addEventListener("keydown", e => { if (e.key === "Enter" && el("passwordInput")) el("passwordInput").focus(); });
if(el("passwordInput")) el("passwordInput").addEventListener("keydown", e => { if (e.key === "Enter") enter(); });

// ---------- teacher gate ----------
async function teacherLogin(passcodeOverride = null) {
  const btn = el("passBtn");
  const errEl = el("passErr");
  const passInputEl = el("passInput");
  
  const p = passcodeOverride || (passInputEl ? passInputEl.value.trim() : "");
  
  if (!p) { 
    if(errEl) {
      errEl.textContent = "Please type your passcode."; 
      errEl.classList.remove("hidden"); 
    }
    return; 
  }

  if(btn) {
    btn.disabled = true;
    btn.textContent = "Connecting…";
  }
  if(errEl) errEl.classList.add("hidden");

  try {
    const res = await fetch(`${API}/api/teacher/login`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ passcode: p })
    });
    let data = {};
    try { data = await res.json(); } catch (e) {}
    
    if (res.ok && data.ok) {
      state.passcode = p;
      localStorage.setItem("ng_teacherPasscode", p);
      
      if(errEl) errEl.classList.add("hidden");
      show("teacher");
      loadTeacherRecordings();
      loadStats();
    } else {
      if (passcodeOverride) localStorage.removeItem("ng_teacherPasscode");
      
      if(errEl) {
        errEl.textContent = res.status === 401 ? "Wrong passcode. The default is teach123." : `Login failed (${res.status}).`;
        errEl.classList.remove("hidden");
      }
    }
  } catch (e) {
    if(errEl) {
      errEl.textContent = "Couldn't reach the server. Try again.";
      errEl.classList.remove("hidden");
    }
  } finally {
    if(btn) {
      btn.disabled = false;
      btn.textContent = "Unlock →";
    }
  }
}
if(el("passBtn")) el("passBtn").addEventListener("click", () => teacherLogin());
if(el("passInput")) el("passInput").addEventListener("keydown", e => { if (e.key === "Enter") teacherLogin(); });

// ================= STUDENT TABS & DASHBOARD =================
const studentTabs = ["Dash", "Tutor", "Planner", "PastPapers", "Alerts"];

studentTabs.forEach(t => {
  const btn = el(`tabStudent${t}`);
  if (btn) {
    btn.addEventListener("click", () => switchStudentTab(t));
  }
});

function switchStudentTab(name) {
  studentTabs.forEach(t => {
    if(el(`tabStudent${t}`)) el(`tabStudent${t}`).classList.toggle("active", t === name);
    if(el(`student${t}Pane`)) el(`student${t}Pane`).classList.toggle("hidden", t !== name);
  });

  if (name === "Dash") renderStudentDashboard();
  if (name === "Planner") {
    if (state.recordings.length === 0) {
      loadRecordings().then(() => initPlanner());
    } else {
      initPlanner();
    }
  }
  if (name === "PastPapers" && typeof initStudentPastPapers === "function") {
    initStudentPastPapers();
  }
  if (name === "Alerts") {
    renderAlerts();
  }
}

function calculatePlanProgress() {
  if (!currentStudyPlan) return 0;
  let tot = 0, comp = 0;
  currentStudyPlan.forEach(d => {
    d.tasks.forEach(t => { tot++; if(t.completed) comp++; });
  });
  return tot === 0 ? 0 : Math.round((comp/tot)*100);
}

function updateAlertBadge() {
  const badge = el("navAlertBadge");
  if (!badge) return;
  const now = new Date();
  const dueCardsCount = (state.flashcardDeck || []).filter(c => new Date(c.dueDate) <= now).length;
  if (dueCardsCount > 0) {
    badge.textContent = dueCardsCount;
    badge.classList.remove("hidden");
  } else {
    badge.classList.add("hidden");
  }
}

function getStudentWeakSpots() {
  const spots = [];
  const now = new Date();

  // 1. Identify topics from flashcards with low intervals or overdue reviews
  const failedRecCounts = {};
  (state.flashcardDeck || []).forEach(c => {
    if (c.interval <= 1 || new Date(c.dueDate) <= now) {
      failedRecCounts[c.recording_id] = (failedRecCounts[c.recording_id] || 0) + 1;
    }
  });

  Object.entries(failedRecCounts).forEach(([recId, count]) => {
    const rec = state.recordings.find(r => r.id === recId);
    if (rec) {
      spots.push({
        label: `${rec.title} (${count} cards struggling)`,
        badge: rec.unit || "Review",
        color: "danger"
      });
    }
  });

  // 2. Identify topics from active chat history where the student asked multiple questions
  if (state.chatHistory) {
    Object.entries(state.chatHistory).forEach(([recId, messages]) => {
      const qCount = messages.filter(m => m.role === "user").length;
      if (qCount >= 3) {
        const rec = state.recordings.find(r => r.id === recId);
        if (rec && !spots.some(s => s.label.startsWith(rec.title))) {
          spots.push({
            label: `${rec.title} (${qCount} doubts raised)`,
            badge: rec.unit || "Doubts",
            color: "warning"
          });
        }
      }
    });
  }

  // 3. Identify incomplete study plan tasks
  if (currentStudyPlan) {
    currentStudyPlan.forEach(day => {
      (day.tasks || []).forEach(t => {
        if (!t.completed && spots.length < 4) {
          spots.push({
            label: t.title,
            badge: `Day ${day.day} Plan`,
            color: "warning"
          });
        }
      });
    });
  }

  // 4. Fallback for new students: pull the latest classes from their enrolled courses
  if (spots.length === 0 && state.recordings.length > 0) {
    state.recordings.slice(0, 3).forEach(r => {
      spots.push({
        label: r.title,
        badge: r.unit || "Recommended",
        color: "info"
      });
    });
  }

  return spots;
}

function renderStudentDashboard() {
  const planPct = calculatePlanProgress();
  const statsBar = el("studentStatsBar");
  if (!statsBar) return;

  const courses = new Set(state.recordings.map(r => r.unit || "Unassigned"));
  const now = new Date();
  const dueCardsCount = (state.flashcardDeck || []).filter(c => new Date(c.dueDate) <= now).length;

  const cards = [
    { label: "Enrolled Courses", value: courses.size },
    { label: "Classes Available", value: state.recordings.length },
    { label: "Flashcards Due", value: dueCardsCount, sub: dueCardsCount > 0 ? "Review to prevent decay ⚠️" : "All caught up ✓" },
    { label: "Study Plan Progress", value: `${planPct}%`, sub: planPct === 100 ? "Completed! 🎉" : (currentStudyPlan ? "In progress" : "No active plan") },
    { label: "AI Questions Asked", value: studentStats.questions }
  ];

  let html = cards.map(c =>
    `<div class="stat-card">
      <div class="stat-value">${escapeHtml(String(c.value))}</div>
      <div class="stat-label">${escapeHtml(c.label)}</div>
      ${c.sub ? `<div class="stat-sub">${escapeHtml(c.sub)}</div>` : ''}
    </div>`
  ).join("");

  const weakSpots = getStudentWeakSpots();
  let chipsHtml = "";

  if (weakSpots.length === 0) {
    chipsHtml = '<span class="meta">No weak spots identified yet. Ask questions, complete quizzes, or build a study plan to see recommendations.</span>';
  } else {
    chipsHtml = weakSpots.map(s => {
      const isDanger = s.color === "danger";
      const isWarn = s.color === "warning";
      const bg = isDanger ? "rgba(255,107,107,0.1)" : isWarn ? "rgba(245,159,0,0.1)" : "rgba(11,191,191,0.1)";
      const color = isDanger ? "#e03131" : isWarn ? "#f59f00" : "var(--brand-d)";
      const border = isDanger ? "rgba(255,107,107,0.3)" : isWarn ? "rgba(245,159,0,0.3)" : "rgba(11,191,191,0.3)";

      return `<span class="course-chip" style="background: ${bg}; color: ${color}; border-color: ${border}; font-weight: 700;">
        [${escapeHtml(s.badge)}] ${escapeHtml(s.label)}
      </span>`;
    }).join("");
  }

  html += `
    <div style="grid-column: 1 / -1; margin-top: 10px; background: var(--panel); border: 1.5px solid var(--line); padding: 18px; border-radius: 14px;">
      <h3 style="font-size: 16px; font-weight: 800; margin-bottom: 8px;">🎯 Suggested Focus & Weak Spots</h3>
      <p class="meta" style="margin-bottom: 12px;">Dynamically calculated from your enrolled course activity, spaced repetition review decay, and asked questions:</p>
      <div style="display: flex; gap: 8px; flex-wrap: wrap;">
        ${chipsHtml}
      </div>
    </div>
  `;

  statsBar.innerHTML = html;
  updateAlertBadge();
}

// ================= ALERTS & NOTIFICATIONS =================
function renderAlerts() {
  const container = el("alertsContainer");
  const badge = el("navAlertBadge");
  if (!container) return;

  container.innerHTML = "";
  const now = new Date();
  let alertCount = 0;

  // 1. Spaced Repetition Due Cards
  const dueCards = (state.flashcardDeck || []).filter(c => new Date(c.dueDate) <= now);
  if (dueCards.length > 0) {
    alertCount += dueCards.length;
    const dueByRec = {};
    dueCards.forEach(c => {
      if (!dueByRec[c.recording_id]) dueByRec[c.recording_id] = [];
      dueByRec[c.recording_id].push(c);
    });

    Object.keys(dueByRec).forEach(recId => {
      const rec = state.recordings.find(r => r.id === recId);
      const title = rec ? rec.title : "Class Recording";
      const count = dueByRec[recId].length;

      const card = document.createElement("div");
      card.className = "setting-card";
      card.style.cssText = "display: flex; justify-content: space-between; align-items: center; border-left: 4px solid var(--brand); background: var(--panel); margin-bottom: 12px;";
      card.innerHTML = `
        <div>
          <h4 style="margin: 0 0 4px 0; font-size: 15px; color: var(--brand-d);">🃏 Spaced Repetition Due: ${escapeHtml(title)}</h4>
          <p class="meta" style="margin: 0;">You have <strong>${count}</strong> flashcard${count > 1 ? "s" : ""} scheduled for active recall review today.</p>
        </div>
        <button class="primary" style="padding: 8px 16px; font-size: 13px;">Review Deck →</button>
      `;

      card.querySelector("button").addEventListener("click", () => {
        if (rec) {
          switchStudentTab("Tutor");
          selectRecording(rec);
          if (el("flashcardBtn")) el("flashcardBtn").click();
        }
      });
      container.appendChild(card);
    });
  }

  // 2. Study Plan Daily Task Reminders
  const plan = currentStudyPlan || [];
  const pendingTasks = [];
  plan.forEach(day => {
    (day.tasks || []).forEach(task => {
      if (!task.completed) pendingTasks.push({ ...task, day: day.day });
    });
  });

  if (pendingTasks.length > 0) {
    alertCount += 1;
    const planCard = document.createElement("div");
    planCard.className = "setting-card";
    planCard.style.cssText = "display: flex; justify-content: space-between; align-items: center; border-left: 4px solid #f59f00; background: var(--panel); margin-bottom: 12px;";
    planCard.innerHTML = `
      <div>
        <h4 style="margin: 0 0 4px 0; font-size: 15px; color: #f59f00;">📅 Active Study Plan Tasks</h4>
        <p class="meta" style="margin: 0;">You have <strong>${pendingTasks.length}</strong> task${pendingTasks.length > 1 ? "s" : ""} waiting to be checked off your revision timetable.</p>
      </div>
      <button id="jumpToPlannerBtn" class="ghost" style="padding: 8px 16px; font-size: 13px;">Open Planner →</button>
    `;
    container.appendChild(planCard);

    const planBtn = planCard.querySelector("#jumpToPlannerBtn");
    if (planBtn) {
      planBtn.addEventListener("click", () => {
        switchStudentTab("Planner");
      });
    }
  }

  // 3. Fallback when caught up
  if (alertCount === 0) {
    container.innerHTML = `
      <div class="empty" style="padding: 40px 0;">
        <div class="empty-emoji">🎉</div>
        <h3>All caught up!</h3>
        <p class="meta">No overdue flashcards or revision tasks scheduled right now.</p>
      </div>
    `;
    if (badge) badge.classList.add("hidden");
  } else {
    if (badge) {
      badge.textContent = alertCount;
      badge.classList.remove("hidden");
    }
  }
}

// ================= STUDENT AI TUTOR & SAVED CHAT HISTORY =================
async function loadRecordings() {
  const res = await fetch(`${API}/api/recordings`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token: state.token })
  });
  const data = await res.json();
  state.recordings = data.recordings || [];
  
  populateStudentCourseFilter(data.units || []);
  applyStudentFilters();
}

function populateStudentCourseFilter(units) {
  const sel = el("studentCourseFilter");
  if (!sel) return; 
  const current = sel.value;
  sel.innerHTML = '<option value="">All courses</option>' + 
    units.map(u => `<option value="${escapeHtml(u)}">${escapeHtml(u)}</option>`).join("");
  if (current && units.includes(current)) sel.value = current;
}

function applyStudentFilters() {
  const searchEl = el("search");
  const q = searchEl ? (searchEl.value || "").toLowerCase() : "";
  const sel = el("studentCourseFilter");
  const selectedCourse = sel ? sel.value : "";

  const filtered = state.recordings.filter(r => {
    const matchesText = !q || (r.title || "").toLowerCase().includes(q) || (r.unit || "").toLowerCase().includes(q);
    const matchesCourse = !selectedCourse || (r.unit || "Unassigned") === selectedCourse;
    return matchesText && matchesCourse;
  });
  
  renderRecList(filtered);
}

function renderRecList(list) {
  const box = el("recList");
  if(!box) return;
  box.innerHTML = "";
  if (!list.length) { box.innerHTML = '<div class="rec-item"><div class="d">No recordings available yet.</div></div>'; return; }
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

if(el("search")) el("search").addEventListener("input", applyStudentFilters);
document.addEventListener("change", e => { if (e.target.id === "studentCourseFilter") applyStudentFilters(); });

function selectRecording(r) {
  state.current = r;
  applyStudentFilters();
  if(el("emptyState")) el("emptyState").classList.add("hidden");
  if(el("workspace")) el("workspace").classList.remove("hidden");
  if(el("wsTitle")) el("wsTitle").textContent = r.title;
  if(el("wsMeta")) el("wsMeta").innerHTML = `${escapeHtml(r.unit)} · ${escapeHtml(r.date || "")} · ${r.segments} transcript lines` +
    (r.has_notes ? ` · <span class="notes-flag">📎 includes extra class notes</span>` : "");
  
  const chat = el("chat");
  if(chat) chat.innerHTML = "";
  
  const savedHistory = state.chatHistory[r.id] || [];
  if (savedHistory.length > 0) {
    savedHistory.forEach(msg => {
      if (msg.role === 'user') addUser(msg.text, false);
      else addBot(msg.text, false);
    });
  } else {
    const notesLine = r.has_notes ? " This class also has extra notes from your teacher that I can draw on." : "";
    addBot(`Hi ${state.name}! Ask me anything about **${r.title}**. I'll answer using only what was said in this recording (with timestamps).${notesLine} 😊`, false);
  }
}

function addUser(text, save = true) { 
  const chat = el("chat");
  if(!chat) return;
  const d = document.createElement("div"); d.className = "msg user"; d.textContent = text; chat.appendChild(d); scrollChat(); 
  if (save && state.current) {
    if (!state.chatHistory[state.current.id]) state.chatHistory[state.current.id] = [];
    state.chatHistory[state.current.id].push({ role: 'user', text });
  }
}

function addBot(text, save = true) { 
  const chat = el("chat");
  if(!chat) return;
  const d = document.createElement("div"); d.className = "msg bot"; d.innerHTML = renderBotText(text); chat.appendChild(d); scrollChat(); 
  if (save && state.current) {
    if (!state.chatHistory[state.current.id]) state.chatHistory[state.current.id] = [];
    state.chatHistory[state.current.id].push({ role: 'bot', text });
  }
}

function addTyping() { 
  const chat = el("chat");
  if(!chat) return;
  const d = document.createElement("div"); d.className = "typing"; d.id = "typing"; d.innerHTML = 'ClassMate is reading the recording <span class="dot">●</span><span class="dot">●</span><span class="dot">●</span>'; chat.appendChild(d); scrollChat(); 
}
function removeTyping() { const t = el("typing"); if (t) t.remove(); }
function scrollChat() { const c = el("chat"); if(c) c.scrollTop = c.scrollHeight; }

if(el("clearChatBtn")) {
  el("clearChatBtn").addEventListener("click", async () => {
    if (!state.current) return;
    if (confirm("Clear your saved chat history for this recording?")) {
      delete state.chatHistory[state.current.id];
      await saveServerProfile();
      selectRecording(state.current);
    }
  });
}

if(el("askForm")) {
  el("askForm").addEventListener("submit", async e => {
    e.preventDefault();
    const qInput = el("questionInput");
    if(!qInput) return;
    const q = qInput.value.trim();
    if (!q || !state.current) return;
    qInput.value = "";
    if(el("askBtn")) el("askBtn").disabled = true;
    addUser(q, true); 
    addTyping();
    try {
      const res = await fetch(`${API}/api/ask`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ recording_id: state.current.id, question: q, language: "English", token: state.token })
      });
      const data = await res.json();
      removeTyping();
      if (data.error) addBot("Sorry, something went wrong: " + data.error, true);
      else {
        addBot(data.answer, true);
        studentStats.questions++;
        saveStudentStats();
        await saveServerProfile();
      }
    } catch (err) { removeTyping(); addBot("Sorry, I couldn't reach the server. Please try again.", true); }
    if(el("askBtn")) el("askBtn").disabled = false; 
    qInput.focus();
  });
}

// ---------- quiz ----------
let quizData = null;
if(el("quizBtn")) el("quizBtn").addEventListener("click", generateQuiz);

async function generateQuiz() {
  if (!state.current) return;
  if(el("quizModal")) el("quizModal").classList.remove("hidden");
  if(el("submitQuiz")) el("submitQuiz").classList.add("hidden"); 
  if(el("retryQuiz")) el("retryQuiz").classList.add("hidden");
  if(el("quizBody")) el("quizBody").innerHTML = '<div class="typing">Creating your quiz from the recording <span class="dot">●</span><span class="dot">●</span><span class="dot">●</span></div>';
  try {
    const res = await fetch(`${API}/api/quiz`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ recording_id: state.current.id, num_questions: 5, language: "English", token: state.token })
    });
    const data = await res.json();
    if (data.error || !data.questions) { if(el("quizBody")) el("quizBody").innerHTML = '<p>Sorry, I could not build a quiz for this recording. Try another one.</p>'; return; }
    quizData = data.questions; 
    renderQuiz();
    
    studentStats.quizzes++;
    saveStudentStats();
  } catch (e) { if(el("quizBody")) el("quizBody").innerHTML = '<p>Could not reach the server. Please try again.</p>'; }
}

if(el("closeQuiz")) el("closeQuiz").addEventListener("click", () => el("quizModal").classList.add("hidden"));
if(el("retryQuiz")) el("retryQuiz").addEventListener("click", generateQuiz);

function renderQuiz() {
  const body = el("quizBody"); 
  if(!body) return;
  body.innerHTML = "";
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
  if(el("submitQuiz")) el("submitQuiz").classList.remove("hidden"); 
  if(el("retryQuiz")) el("retryQuiz").classList.add("hidden");
}

if(el("submitQuiz")) {
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
      if(exp) {
        const ts = q.timestamp ? `<span class="ts-chip">⏱ ${escapeHtml(q.timestamp)}</span>` : "";
        exp.innerHTML = `✅ <strong>Answer:</strong> ${escapeHtml(q.options[q.answer_index])} ${ts}<br>${escapeHtml(q.explanation || "")}`;
        exp.classList.remove("hidden");
      }
    });
    const head = document.createElement("div"); head.className = "score";
    const pct = Math.round(100 * score / quizData.length);
    head.textContent = `You scored ${score} / ${quizData.length}  (${pct}%) ${pct >= 80 ? "🎉" : pct >= 50 ? "👍" : "📖 keep reviewing!"}`;
    if(el("quizBody")) el("quizBody").prepend(head);
    if(el("submitQuiz")) el("submitQuiz").classList.add("hidden"); 
    if(el("retryQuiz")) el("retryQuiz").classList.remove("hidden");
  });
}

// ================= SPACED REPETITION (SRS) FLASHCARDS =================
let currentDeckCards = [];
let currentCardIndex = 0;
let flashcardIsFlipped = false;

const flashcardBtn = el("flashcardBtn");
const flashcardModal = el("flashcardModal");
const closeFcModal = el("closeFcModal");
const fcCard = el("fcCard");
const fcFrontText = el("fcFrontText");
const fcBackText = el("fcBackText");
const fcRatingBtns = el("fcRatingBtns");
const fcProgress = el("fcProgress");
const fcHardBtn = el("fcHardBtn");
const fcGoodBtn = el("fcGoodBtn");
const fcEasyBtn = el("fcEasyBtn");
const fcPrevBtn = el("fcPrevBtn");
const fcNextBtn = el("fcNextBtn");
const genFreshCardsBtn = el("genFreshCardsBtn");
const srsStatusText = el("srsStatusText");

function getDueFlashcards(recId) {
  const now = new Date();
  return state.flashcardDeck.filter(c => c.recording_id === recId && new Date(c.dueDate) <= now);
}

function getRecordingCards(recId) {
  return state.flashcardDeck.filter(c => c.recording_id === recId);
}

function setFlashcardLoading(message) {
  if (fcFrontText) {
    fcFrontText.classList.remove("hidden");
    fcFrontText.innerHTML = escapeHtml(message);
  }
  if (fcBackText) {
    fcBackText.textContent = "";
    fcBackText.classList.add("hidden");
  }
  if (fcRatingBtns) fcRatingBtns.classList.add("hidden");
}

function renderCurrentCard() {
  if (!fcCard || !fcFrontText || !fcBackText) return;

  if (!currentDeckCards.length) {
    fcFrontText.textContent = 'No flashcards yet. Click “Generate New Flashcards” below.';
    fcBackText.classList.add("hidden");
    if (fcRatingBtns) fcRatingBtns.classList.add("hidden");
    if (fcProgress) fcProgress.textContent = "0 cards";
    if (fcPrevBtn) fcPrevBtn.disabled = true;
    if (fcNextBtn) fcNextBtn.disabled = true;
    return;
  }

  if (currentCardIndex >= currentDeckCards.length) currentCardIndex = 0;
  if (currentCardIndex < 0) currentCardIndex = currentDeckCards.length - 1;

  const card = currentDeckCards[currentCardIndex];
  flashcardIsFlipped = false;

  fcCard.style.background = 'var(--panel2)';
  fcCard.style.borderColor = 'var(--line)';
  fcFrontText.textContent = card.front || "(No question text)";
  fcFrontText.classList.remove("hidden");
  fcBackText.textContent = card.back || "(No answer text)";
  fcBackText.classList.add("hidden");
  if (fcRatingBtns) fcRatingBtns.classList.add("hidden");
  if (fcProgress) fcProgress.textContent = `Card ${currentCardIndex + 1} of ${currentDeckCards.length}`;
  if (fcPrevBtn) fcPrevBtn.disabled = currentDeckCards.length <= 1;
  if (fcNextBtn) fcNextBtn.disabled = currentDeckCards.length <= 1;
}

function flipFlashcard() {
  if (!currentDeckCards.length) return;
  flashcardIsFlipped = !flashcardIsFlipped;
  if (flashcardIsFlipped) {
    fcCard.style.background = 'rgba(11,191,191,0.08)';
    fcCard.style.borderColor = 'var(--brand)';
    fcFrontText.classList.add("hidden");
    fcBackText.classList.remove("hidden");
    if (fcRatingBtns) fcRatingBtns.classList.remove("hidden");
  } else {
    fcCard.style.background = 'var(--panel2)';
    fcCard.style.borderColor = 'var(--line)';
    fcFrontText.classList.remove("hidden");
    fcBackText.classList.add("hidden");
    if (fcRatingBtns) fcRatingBtns.classList.add("hidden");
  }
}

if (fcCard) fcCard.addEventListener("click", flipFlashcard);
if (fcPrevBtn) fcPrevBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  if (!currentDeckCards.length) return;
  currentCardIndex = (currentCardIndex - 1 + currentDeckCards.length) % currentDeckCards.length;
  renderCurrentCard();
});
if (fcNextBtn) fcNextBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  if (!currentDeckCards.length) return;
  currentCardIndex = (currentCardIndex + 1) % currentDeckCards.length;
  renderCurrentCard();
});

if (flashcardBtn) {
  flashcardBtn.addEventListener("click", async () => {
    if (!state.current || !flashcardModal) return;
    flashcardModal.classList.remove("hidden");
    const existing = getRecordingCards(state.current.id);
    if (existing.length === 0) {
      await generateNewFlashcards();
    } else {
      currentDeckCards = existing;
      currentCardIndex = 0;
      updateSrsHeader();
      renderCurrentCard();
    }
  });
}

if (genFreshCardsBtn) {
  genFreshCardsBtn.addEventListener("click", async (e) => {
    e.stopPropagation();
    await generateNewFlashcards();
  });
}

async function generateNewFlashcards() {
  if (!state.current) return;
  setFlashcardLoading("✨ Generating fresh flashcards for this class…");
  if (genFreshCardsBtn) {
    genFreshCardsBtn.disabled = true;
    genFreshCardsBtn.textContent = "Generating…";
  }

  const existingFronts = getRecordingCards(state.current.id).map(c => c.front).filter(Boolean);

  try {
    const res = await fetch(`${API}/api/flashcards`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        recording_id: state.current.id,
        existing_fronts: existingFronts,
        token: state.token
      })
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.error || !Array.isArray(data.flashcards) || data.flashcards.length === 0) {
      setFlashcardLoading(data.error || "Could not generate new flashcards for this class.");
      return;
    }

    const now = new Date().toISOString();
    const newCards = data.flashcards.map(fc => ({
      id: "fc_" + Math.random().toString(36).substring(2, 9),
      recording_id: state.current.id,
      front: String(fc.front || "").trim(),
      back: String(fc.back || "").trim(),
      interval: 1,
      reps: 0,
      dueDate: now
    })).filter(c => c.front && c.back);

    if (!newCards.length) {
      setFlashcardLoading("The AI returned no usable flashcards. Please try again.");
      return;
    }

    state.flashcardDeck.push(...newCards);
    await saveServerProfile();

    currentDeckCards = getRecordingCards(state.current.id);
    currentCardIndex = currentDeckCards.length - newCards.length;
    updateSrsHeader();
    renderCurrentCard();
  } catch (e) {
    setFlashcardLoading("Network error generating flashcards. Please try again.");
  } finally {
    if (genFreshCardsBtn) {
      genFreshCardsBtn.disabled = false;
      genFreshCardsBtn.textContent = "✨ Generate New Flashcards";
    }
  }
}

function updateSrsHeader() {
  if (!state.current || !srsStatusText) return;
  const due = getDueFlashcards(state.current.id).length;
  const total = getRecordingCards(state.current.id).length;
  srsStatusText.textContent = `🎯 Due for Review: ${due} / ${total} cards`;
}

function closeFlashcardModal() {
  if (flashcardModal) flashcardModal.classList.add("hidden");
  flashcardIsFlipped = false;
}
if (closeFcModal) closeFcModal.addEventListener("click", (e) => { e.stopPropagation(); closeFlashcardModal(); });

async function rateCard(ratingFactor) {
  if (!currentDeckCards.length) return;
  const card = currentDeckCards[currentCardIndex];
  const original = state.flashcardDeck.find(c => c.id === card.id);
  if (!original) return;

  const now = new Date();
  if (ratingFactor === 'hard') {
    original.interval = Math.max(1, Math.round((original.interval || 1) * 1.2));
  } else if (ratingFactor === 'good') {
    original.interval = Math.max(2, Math.round((original.interval || 1) * 2.5));
    original.reps = (original.reps || 0) + 1;
  } else if (ratingFactor === 'easy') {
    original.interval = Math.max(4, Math.round((original.interval || 1) * 3.5));
    original.reps = (original.reps || 0) + 1;
  }

  original.dueDate = new Date(now.getTime() + (original.interval * 24 * 60 * 60 * 1000)).toISOString();
  await saveServerProfile();
  updateSrsHeader();

  if (currentDeckCards.length > 1) {
    currentCardIndex = (currentCardIndex + 1) % currentDeckCards.length;
  }
  renderCurrentCard();
}

if (fcHardBtn) fcHardBtn.addEventListener("click", (e) => { e.stopPropagation(); rateCard('hard'); });
if (fcGoodBtn) fcGoodBtn.addEventListener("click", (e) => { e.stopPropagation(); rateCard('good'); });
if (fcEasyBtn) fcEasyBtn.addEventListener("click", (e) => { e.stopPropagation(); rateCard('easy'); });

// Robust modal close fallbacks for both current and legacy IDs.
document.addEventListener("click", (e) => {
  const t = e.target;
  if (!t) return;
  if (t.id === "closeFcModal" || t.closest?.("#closeFcModal")) closeFlashcardModal();
  if (t.id === "closeQuizModal" || t.closest?.("#closeQuizModal")) {
    const q = el("quizModal");
    if (q) q.classList.add("hidden");
  }
});

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  closeFlashcardModal();
  const q = el("quizModal");
  if (q) q.classList.add("hidden");
});

/* =========================================================
   STUDY PLAN FEATURE
   ========================================================= */
const planClassSelect = document.getElementById('planClassSelect');
const generatePlanBtn = document.getElementById('generatePlanBtn');
const resetPlanBtn = document.getElementById('resetPlanBtn');
const planEmptyState = document.getElementById('planEmptyState');
const planResult = document.getElementById('planResult');

const focusHints = {
  "First-time learning": "💡 Focuses on deep understanding, concept breakdowns, and taking structured notes.",
  "Reviewing and memorizing definitions": "💡 Combines rapid topic overviews with active recall, flashcards, and definition checks.",
  "Past paper and exam practice": "💡 Prioritizes past-paper style questions, command words, and Cambridge/Edexcel mark scheme tips."
};

const planFocusEl = el("planFocus");
if (planFocusEl) {
  planFocusEl.addEventListener("change", (e) => {
    const hintEl = el("focusHint");
    if (hintEl) hintEl.textContent = focusHints[e.target.value] || "";
  });
}

function initPlanner() {
  if (!planClassSelect) return;
  planClassSelect.innerHTML = '';
  if (state.recordings.length === 0) {
    planClassSelect.innerHTML = '<p class="meta" style="padding: 8px;">No classes available.</p>';
  } else {
    const courseGroups = {};
    state.recordings.forEach(r => {
      const courseName = (r.unit && r.unit.trim() !== "") ? r.unit.trim() : "Unassigned Course";
      if (!courseGroups[courseName]) courseGroups[courseName] = [];
      courseGroups[courseName].push(r);
    });

    Object.keys(courseGroups).sort().forEach(courseName => {
      const courseSection = document.createElement('div');
      courseSection.style.cssText = 'margin-bottom: 12px; background: var(--panel2); padding: 10px; border-radius: 8px; border: 1.5px solid var(--line);';
      
      const courseHeader = document.createElement('div');
      courseHeader.style.cssText = 'display: flex; justify-content: space-between; align-items: center; font-weight: 800; font-size: 13px; color: var(--brand-d); margin-bottom: 6px; border-bottom: 1px solid var(--line); padding-bottom: 4px;';
      
      const titleSpan = document.createElement('span');
      titleSpan.textContent = `📚 ${courseName}`;
      
      const courseToggleBtn = document.createElement('button');
      courseToggleBtn.type = 'button';
      courseToggleBtn.className = 'ghost-sm';
      courseToggleBtn.style.cssText = 'font-size: 11px; padding: 2px 6px;';
      courseToggleBtn.textContent = 'Select Course';
      
      const classesContainer = document.createElement('div');
      classesContainer.style.cssText = 'display: flex; flex-direction: column; gap: 4px; margin-top: 4px;';

      const checkboxesInCourse = [];

      courseGroups[courseName].forEach(r => {
        const label = document.createElement('label');
        label.style.cssText = 'display: flex; align-items: center; gap: 8px; cursor: pointer; padding: 2px 0;';
        
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.value = r.id;
        checkbox.className = 'class-checkbox';
        checkbox.dataset.course = courseName;
        checkbox.style.cssText = 'transform: scale(1.1); cursor: pointer;';

        checkboxesInCourse.push(checkbox);

        const span = document.createElement('span');
        span.style.cssText = 'font-size: 12.5px; font-weight: 600; color: var(--text);';
        span.textContent = r.title;

        label.appendChild(checkbox);
        label.appendChild(span);
        classesContainer.appendChild(label);
      });

      courseToggleBtn.addEventListener('click', () => {
        const allChecked = checkboxesInCourse.every(cb => cb.checked);
        checkboxesInCourse.forEach(cb => cb.checked = !allChecked);
      });

      courseHeader.appendChild(titleSpan);
      courseHeader.appendChild(courseToggleBtn);
      courseSection.appendChild(courseHeader);
      courseSection.appendChild(classesContainer);
      planClassSelect.appendChild(courseSection);
    });
  }

  const setupDiv = document.getElementById('planSetup');
  const formEls = setupDiv ? setupDiv.querySelectorAll('input, select') : [];
  
  if (currentStudyPlan) {
    formEls.forEach(elem => elem.disabled = true);
    if(generatePlanBtn) generatePlanBtn.classList.add('hidden');
    if(resetPlanBtn) resetPlanBtn.classList.remove('hidden');
    if(planEmptyState) planEmptyState.classList.add('hidden');
    if(planResult) planResult.classList.remove('hidden');
    renderPlan();
  } else {
    formEls.forEach(elem => elem.disabled = false);
    if(generatePlanBtn) generatePlanBtn.classList.remove('hidden');
    if(resetPlanBtn) resetPlanBtn.classList.add('hidden');
    if(planEmptyState) planEmptyState.classList.remove('hidden');
    if(planResult) planResult.classList.add('hidden');
  }
}

window.toggleAllClasses = function(selectState) {
  const checkboxes = document.querySelectorAll('.class-checkbox');
  checkboxes.forEach(cb => cb.checked = selectState);
};

if (generatePlanBtn) {
  generatePlanBtn.addEventListener('click', async () => {
    const selectedIds = Array.from(planClassSelect.querySelectorAll('.class-checkbox:checked')).map(cb => cb.value);
    if (selectedIds.length === 0) {
      alert("Please select at least one class to review.");
      return;
    }

    const days = document.getElementById('planDays').value;
    const hours = document.getElementById('planHours').value;
    const focus = document.getElementById('planFocus').value;

    const btnOrig = generatePlanBtn.innerText;
    generatePlanBtn.innerText = "⏳ Building your schedule...";
    generatePlanBtn.disabled = true;

    try {
      const res = await fetch(`${API}/api/student/plan`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          recording_ids: selectedIds,
          days: parseInt(days),
          hours_per_day: parseFloat(hours),
          focus: focus,
          token: state.token
        })
      });

      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Failed to generate plan");

      data.plan.forEach(day => {
        day.tasks.forEach(task => task.completed = false);
      });

      currentStudyPlan = data.plan;
      await saveServerProfile();
      initPlanner();
    } catch (err) {
      alert(err.message);
    } finally {
      generatePlanBtn.innerText = btnOrig;
      generatePlanBtn.disabled = false;
    }
  });
}

function renderPlan() {
  if (!planResult) return;
  planResult.innerHTML = '';
  let totalTasks = 0;
  let completedTasks = 0;

  const planContainer = document.createElement('div');
  planContainer.style.display = 'flex';
  planContainer.style.flexDirection = 'column';
  planContainer.style.gap = '16px';

  currentStudyPlan.forEach((day, dIdx) => {
    const dayCard = document.createElement('div');
    dayCard.className = 'q-block'; 
    
    const quoteHtml = day.quote ? `
      <div class="explain" style="margin-top:0; margin-bottom:12px; border-left: 3px solid var(--brand); font-style: italic; color: var(--brand-d);">
        💡 "${escapeHtml(day.quote)}"
      </div>` : '';

    const tasksContainer = document.createElement('div');
    tasksContainer.style.display = 'flex';
    tasksContainer.style.flexDirection = 'column';
    tasksContainer.style.gap = '8px';

    day.tasks.forEach((task, tIdx) => {
      totalTasks++;
      if (task.completed) completedTasks++;
      
      const taskCard = document.createElement('div');
      taskCard.style.cssText = `display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; padding: 12px; background: var(--panel); border: 1.5px solid var(--line); border-radius: 12px; opacity: ${task.completed ? '0.55' : '1'}; transition: 0.15s;`;
      
      const leftGroup = document.createElement('div');
      leftGroup.style.cssText = 'display: flex; align-items: flex-start; gap: 12px; flex: 1;';

      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.style.cssText = 'margin-top: 3px; transform: scale(1.3); cursor: pointer;';
      checkbox.checked = task.completed;
      
      checkbox.addEventListener('change', async (e) => {
        e.stopPropagation();
        await togglePlanTask(dIdx, tIdx);
      });

      const textDiv = document.createElement('div');
      textDiv.style.flex = '1';
      textDiv.innerHTML = `
        <strong style="display:block; font-size: 14.5px; margin-bottom: 3px; color: ${task.completed ? 'var(--muted)' : 'var(--text)'};">
          ${escapeHtml(task.title)} <span class="meta" style="font-weight:800; color: var(--brand-d);">(${task.est_minutes}m)</span>
        </strong>
        <span style="font-size: 13px; color: var(--muted); font-weight:600; line-height: 1.4; display: block;">${escapeHtml(task.description)}</span>
      `;

      leftGroup.appendChild(checkbox);
      leftGroup.appendChild(textDiv);

      const editBtn = document.createElement('button');
      editBtn.type = 'button';
      editBtn.className = 'ghost-sm';
      editBtn.style.cssText = 'font-size: 11px; padding: 4px 8px; white-space: nowrap;';
      editBtn.textContent = '✏️ Edit';

      editBtn.addEventListener('click', async () => {
        const newTitle = prompt("Edit task title:", task.title);
        if (newTitle === null) return;
        const newDesc = prompt("Edit task details / description:", task.description);
        if (newDesc === null) return;
        const newTime = prompt("Edit estimated minutes:", task.est_minutes);
        if (newTime === null) return;

        currentStudyPlan[dIdx].tasks[tIdx].title = newTitle.trim() || task.title;
        currentStudyPlan[dIdx].tasks[tIdx].description = newDesc.trim() || task.description;
        currentStudyPlan[dIdx].tasks[tIdx].est_minutes = parseInt(newTime) || task.est_minutes;

        await saveServerProfile();
        renderPlan();
      });

      taskCard.appendChild(leftGroup);
      taskCard.appendChild(editBtn);
      tasksContainer.appendChild(taskCard);
    });

    dayCard.innerHTML = `
      <div class="q-title"><span class="q-num" style="padding: 4px 12px; font-size: 13px;">Day ${day.day}</span></div>
      ${quoteHtml}
    `;
    dayCard.appendChild(tasksContainer);
    planContainer.appendChild(dayCard);
  });

  const pct = totalTasks === 0 ? 0 : Math.round((completedTasks / totalTasks) * 100);
  const isComplete = totalTasks > 0 && completedTasks === totalTasks;

  const progressHtml = `
    <div style="margin-bottom: 20px; background: var(--panel); border: 1.5px solid var(--line); padding: 16px; border-radius: 14px; box-shadow: var(--shadow-sm);">
      <div style="display: flex; justify-content: space-between; align-items: center; font-weight: 900; font-size: 15px; margin-bottom: 10px;">
        <span>Plan Progress</span>
        <span style="color: var(--brand-d);">${pct}% Completed</span>
      </div>
      <div class="bulk-bar" style="max-width: 100%; height: 14px; background: var(--bg2); margin-bottom: 14px;">
        <div class="bulk-bar-fill" style="width: ${pct}%; border-radius: 20px;"></div>
      </div>
      
      <button id="finishPlanBtn" class="primary" ${isComplete ? '' : 'disabled'} style="width: 100%; opacity: ${isComplete ? '1' : '0.5'}; cursor: ${isComplete ? 'pointer' : 'not-allowed'}; background: ${isComplete ? 'linear-gradient(135deg, var(--ok), #0ca678)' : 'var(--line)'};">
        ${isComplete ? '✅ Plan Completed' : '🔒 Complete all tasks to finish plan'}
      </button>
    </div>
  `;

  planResult.innerHTML = progressHtml;
  planResult.appendChild(planContainer);

  const finishBtn = document.getElementById('finishPlanBtn');
  if (finishBtn && isComplete) {
    finishBtn.addEventListener('click', async () => {
      if (confirm("Congratulations on completing your study plan! 🎉 Would you like to wrap this up and clear it so you can start a new one?")) {
        currentStudyPlan = null;
        await saveServerProfile();
        initPlanner();
      }
    });
  }
}

async function togglePlanTask(dIdx, tIdx) {
  currentStudyPlan[dIdx].tasks[tIdx].completed = !currentStudyPlan[dIdx].tasks[tIdx].completed;
  await saveServerProfile();
  renderPlan(); 
}

if (resetPlanBtn) {
  resetPlanBtn.addEventListener('click', async () => {
    if(confirm("Are you sure you want to delete your current plan and start over?")) {
      currentStudyPlan = null;
      await saveServerProfile();
      initPlanner(); 
    }
  });
}

// ================= TEACHER VIEW =================
if(el("tabRecordings")) el("tabRecordings").addEventListener("click", () => switchTab("Recordings"));
if(el("tabStudents")) el("tabStudents").addEventListener("click", () => switchTab("Students"));
if(el("tabPastPapers")) el("tabPastPapers").addEventListener("click", () => switchTab("PastPapers"));
if(el("tabQuestions")) el("tabQuestions").addEventListener("click", () => switchTab("Questions"));
if(el("tabAnalytics")) el("tabAnalytics").addEventListener("click", () => switchTab("Analytics"));
if(el("tabSettings")) el("tabSettings").addEventListener("click", () => switchTab("Settings"));

function switchTab(name) {
  // 1. Toggle Active Header Button
  if(el("tabRecordings")) el("tabRecordings").classList.toggle("active", name === "Recordings");
  if(el("tabStudents")) el("tabStudents").classList.toggle("active", name === "Students");
  if(el("tabPastPapers")) el("tabPastPapers").classList.toggle("active", name === "PastPapers");
  if(el("tabQuestions")) el("tabQuestions").classList.toggle("active", name === "Questions");
  if(el("tabAnalytics")) el("tabAnalytics").classList.toggle("active", name === "Analytics");
  if(el("tabSettings")) el("tabSettings").classList.toggle("active", name === "Settings");
  
  // 2. Unhide Selected Pane and Hide Others
  if(el("teacherRecordings")) el("teacherRecordings").classList.toggle("hidden", name !== "Recordings");
  if(el("teacherStudents")) el("teacherStudents").classList.toggle("hidden", name !== "Students");
  if(el("teacherPastPapers")) el("teacherPastPapers").classList.toggle("hidden", name !== "PastPapers");
  if(el("teacherQuestions")) el("teacherQuestions").classList.toggle("hidden", name !== "Questions");
  if(el("teacherAnalytics")) el("teacherAnalytics").classList.toggle("hidden", name !== "Analytics");
  if(el("teacherSettings")) el("teacherSettings").classList.toggle("hidden", name !== "Settings");
  
  // 3. Trigger Data Loaders
  if (name === "Recordings") { loadTeacherRecordings(); loadStats(); }
  if (name === "Students") loadStudents();
  if (name === "PastPapers") loadTeacherPastPaperHub();
  if (name === "Questions") loadQuestions();
  if (name === "Analytics") loadAnalytics();
}

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

async function loadStats() {
  const bar = el("statsBar");
  if (!bar) return;
  if (!bar.dataset.loaded) bar.innerHTML = '<div class="stat-skeleton"></div>'.repeat(5);
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
  } catch (e) {}
}

async function loadAnalytics() {
  const box = el("analyticsBody");
  if(!box) return;
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

function downloadUrl(path) {
  const url = `${API}${path}${path.includes("?") ? "&" : "?"}passcode=${encodeURIComponent(state.passcode)}`;
  const a = document.createElement("a");
  a.href = url; a.download = ""; document.body.appendChild(a); a.click(); a.remove();
}
if(el("exportQCsv")) el("exportQCsv").addEventListener("click", () => { downloadUrl("/api/teacher/export/questions.csv"); toast("Downloading questions CSV…", "info"); });
if(el("exportQPdf")) el("exportQPdf").addEventListener("click", () => { downloadUrl("/api/teacher/export/questions.pdf"); toast("Downloading questions PDF…", "info"); });
if(el("exportRosterCsv")) el("exportRosterCsv").addEventListener("click", () => { downloadUrl("/api/teacher/export/roster.csv"); toast("Downloading roster CSV…", "info"); });

async function loadStudents() {
  try {
    const res = await fetch(`${API}/api/teacher/students`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ passcode: state.passcode })
    });
    const data = await res.json();
    teacherStudentsCache = data.students || [];
    renderStudents(teacherStudentsCache);

    const studentSearchInput = el("studentSearchInput");
    if (studentSearchInput && !studentSearchInput.dataset.bound) {
      studentSearchInput.dataset.bound = "true";
      studentSearchInput.addEventListener("input", (e) => {
        const query = (e.target.value || "").toLowerCase().trim();
        if (!query) {
          renderStudents(teacherStudentsCache);
          return;
        }
        const filtered = teacherStudentsCache.filter(s => 
          (s.name || "").toLowerCase().includes(query) || 
          (s.email || "").toLowerCase().includes(query)
        );
        renderStudents(filtered);
      });
    }
  } catch (e) {
    const box = el("studentList");
    if(box) box.innerHTML = '<div class="roster-empty">Could not load students.</div>';
  }
}

function renderStudents(list) {
  const box = el("studentList"); 
  if(!box) return;
  box.innerHTML = "";
  if (!list.length) { box.innerHTML = '<div class="roster-empty">No students found matching your search.</div>'; return; }
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

let editingStudent = null;
let editCourses = [];

function renderEditCourses() {
  const wrap = el("edCourseList");
  if(!wrap) return;
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
  if(el("edName")) el("edName").value = s.name || "";
  if(el("edEmail")) el("edEmail").value = s.email || "";
  if(el("edPassword")) el("edPassword").value = "";
  if(el("edCourseInput")) el("edCourseInput").value = "";
  if(el("edStatus")) el("edStatus").textContent = "";
  renderEditCourses();
  if(el("studentModal")) el("studentModal").classList.remove("hidden");
}
function closeStudentEditor() { 
  if(el("studentModal")) el("studentModal").classList.add("hidden"); 
  editingStudent = null; 
}

if(el("closeStudentModal")) el("closeStudentModal").addEventListener("click", closeStudentEditor);
if(el("edCancel")) el("edCancel").addEventListener("click", closeStudentEditor);

if(el("edCourseAddBtn")) {
  el("edCourseAddBtn").addEventListener("click", () => {
    const input = el("edCourseInput");
    if(!input) return;
    const v = input.value.trim();
    if (!v) return;
    if (!editCourses.some(c => c.toLowerCase() === v.toLowerCase())) editCourses.push(v);
    input.value = "";
    renderEditCourses();
  });
  if(el("edCourseInput")) el("edCourseInput").addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); el("edCourseAddBtn").click(); } });
}

if(el("edGenPw")) {
  el("edGenPw").addEventListener("click", () => {
    const abc = "abcdefghijkmnpqrstuvwxyz23456789";
    let p = ""; for (let i = 0; i < 8; i++) p += abc[Math.floor(Math.random() * abc.length)];
    if(el("edPassword")) el("edPassword").value = p;
  });
}

if(el("edSave")) {
  el("edSave").addEventListener("click", async () => {
    if (!editingStudent) return;
    const payload = {
      passcode: state.passcode,
      id: editingStudent.id,
      name: el("edName") ? el("edName").value.trim() : "",
      email: el("edEmail") ? el("edEmail").value.trim() : "",
      courses: editCourses,
    };
    const np = el("edPassword") ? el("edPassword").value.trim() : "";
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
        const stat = el("edStatus");
        if(stat) {
          stat.textContent = data.error || "Could not save.";
          stat.className = "ed-status err";
        }
      }
    } catch (e) {
      const stat = el("edStatus");
      if(stat) {
        stat.textContent = "Network error while saving."; 
        stat.className = "ed-status err";
      }
    } finally { el("edSave").disabled = false; }
  });
}

if(el("edDelete")) {
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
}

if(el("addStudentBtn")) {
  el("addStudentBtn").addEventListener("click", async () => {
    const name = el("newStudentName") ? el("newStudentName").value.trim() : "";
    const email = el("newStudentEmail") ? el("newStudentEmail").value.trim() : "";
    const password = el("newStudentPassword") ? el("newStudentPassword").value : "";
    const courses = el("newStudentCourses") ? el("newStudentCourses").value.trim() : "";
    const err = el("addStudentErr"); 
    if(err) err.classList.add("hidden");
    if (!email) { if(err) { err.textContent = "Enter a student email."; err.classList.remove("hidden"); } return; }
    const res = await fetch(`${API}/api/teacher/students/add`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ passcode: state.passcode, name, email, password, courses })
    });
    const data = await res.json();
    if (!res.ok || data.error) { if(err) { err.textContent = data.error || "Could not add student."; err.classList.remove("hidden"); } return; }
    if(el("newStudentName")) el("newStudentName").value = ""; 
    if(el("newStudentEmail")) el("newStudentEmail").value = ""; 
    if(el("newStudentPassword")) el("newStudentPassword").value = ""; 
    if(el("newStudentCourses")) el("newStudentCourses").value = "";
    if (data.merged) toast(data.message || "Student updated.", "success", 5000);
    else toast("Student added.", "success");
    loadStudents();
  });
}

if(el("importBtn")) {
  el("importBtn").addEventListener("click", async () => {
    const fileInput = el("excelFile");
    const msg = el("importMsg"); const err = el("importErr");
    if(msg) msg.classList.add("hidden"); 
    if(err) err.classList.add("hidden");
    if (!fileInput || !fileInput.files.length) { if(err) { err.textContent = "Choose an .xlsx file first."; err.classList.remove("hidden"); } return; }
    const fd = new FormData();
    fd.append("file", fileInput.files[0]);
    fd.append("passcode", state.passcode);
    try {
      const res = await fetch(`${API}/api/teacher/students/import`, { method: "POST", body: fd });
      const data = await res.json();
      if (!res.ok || data.error) { if(err) { err.textContent = data.error || "Import failed."; err.classList.remove("hidden"); } return; }
      if(msg) {
        msg.textContent = `Imported ✓  ${data.added} added, ${data.updated} updated.`;
        msg.classList.remove("hidden");
      }
      fileInput.value = "";
      loadStudents();
    } catch (e) {
      if(err) {
        err.textContent = "Couldn't reach the server. Try again.";
        err.classList.remove("hidden");
      }
    }
  });
}
// ================= ROSTER DEDUPLICATION =================
let duplicatesData = [];

if (el("dedupeBtn")) {
  el("dedupeBtn").addEventListener("click", async () => {
    const modal = el("dedupeModal");
    const body = el("dedupeBody");
    const applyBtn = el("dedupeApply");
    if (modal) modal.classList.remove("hidden");
    if (body) body.innerHTML = '<div class="typing">Scanning roster for duplicate accounts <span class="dot">●</span><span class="dot">●</span><span class="dot">●</span></div>';
    if (applyBtn) applyBtn.classList.add("hidden");

    try {
      const res = await fetch(`${API}/api/teacher/students/duplicates`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ passcode: state.passcode })
      });
      const data = await res.json();
      duplicatesData = data.duplicates || [];

      if (!duplicatesData.length) {
        if (body) body.innerHTML = '<div class="roster-empty">No duplicate student emails found. Your roster is clean! ✓</div>';
        return;
      }

      let html = `<p class="meta" style="margin-bottom: 12px;">Found <strong>${duplicatesData.length}</strong> duplicate student record(s). Merging will consolidate courses and retain active passwords.</p>`;
      duplicatesData.forEach(d => {
        html += `
          <div class="q-block" style="margin-bottom: 8px; padding: 10px 14px;">
            <strong>${escapeHtml(d.email)}</strong> (${escapeHtml(d.name || "No name")})<br>
            <span class="meta">Combined courses: ${escapeHtml(d.courses ? d.courses.join(", ") : "None")}</span>
          </div>
        `;
      });
      if (body) body.innerHTML = html;
      if (applyBtn) applyBtn.classList.remove("hidden");
    } catch (e) {
      if (body) body.innerHTML = '<div class="roster-empty">Failed to scan for duplicate accounts.</div>';
    }
  });
}

if (el("closeDedupe")) el("closeDedupe").addEventListener("click", () => el("dedupeModal").classList.add("hidden"));
if (el("dedupeCancel")) el("dedupeCancel").addEventListener("click", () => el("dedupeModal").classList.add("hidden"));

if (el("dedupeApply")) {
  el("dedupeApply").addEventListener("click", async () => {
    const btn = el("dedupeApply");
    btn.disabled = true;
    try {
      const res = await fetch(`${API}/api/teacher/students/dedupe`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ passcode: state.passcode })
      });
      const data = await res.json();
      if (res.ok) {
        toast("Duplicate accounts merged successfully ✓", "success");
        if (el("dedupeModal")) el("dedupeModal").classList.add("hidden");
        loadStudents();
        loadStats();
      } else {
        toast(data.error || "Merge failed.", "error");
      }
    } catch (e) {
      toast("Network error while merging.", "error");
    } finally {
      btn.disabled = false;
    }
  });
}
// ================= ZOOM RECORDINGS & BULK CONTROLS =================
if (el("importRecBtn")) {
  el("importRecBtn").addEventListener("click", async () => {
    const btn = el("importRecBtn");
    const status = el("importRecStatus");
    btn.disabled = true;
    if (status) status.textContent = "Checking Zoom cloud for recordings…";
    try {
      const res = await fetch(`${API}/api/teacher/import-recordings`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ passcode: state.passcode })
      });
      const data = await res.json();
      if (res.ok) {
        toast(`Import complete: ${data.added || 0} new recording(s) added.`, "success");
        if (status) status.textContent = `Imported ${data.added || 0} new.`;
        loadTeacherRecordings();
        loadStats();
      } else {
        toast(data.error || "Import failed.", "error");
        if (status) status.textContent = "";
      }
    } catch (e) {
      toast("Network error during import.", "error");
      if (status) status.textContent = "";
    } finally {
      btn.disabled = false;
    }
  });
}

if (el("importOneBtn")) {
  el("importOneBtn").addEventListener("click", async () => {
    const input = el("importOneInput");
    const status = el("importOneStatus");
    const btn = el("importOneBtn");
    const val = input ? input.value.trim() : "";
    if (!val) { toast("Enter a Zoom meeting ID or recording URL.", "info"); return; }
    btn.disabled = true;
    if (status) status.textContent = "Importing…";
    try {
      const res = await fetch(`${API}/api/teacher/import-one`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ passcode: state.passcode, query: val })
      });
      const data = await res.json();
      if (res.ok) {
        toast("Recording imported successfully ✓", "success");
        if (status) status.textContent = "Done ✓";
        input.value = "";
        loadTeacherRecordings();
        loadStats();
      } else {
        toast(data.error || "Failed to import recording.", "error");
        if (status) status.textContent = "";
      }
    } catch (e) {
      toast("Network error during import.", "error");
      if (status) status.textContent = "";
    } finally {
      btn.disabled = false;
    }
  });
}

if (el("deleteUnassignedBtn")) {
  el("deleteUnassignedBtn").addEventListener("click", async () => {
    if (!confirm("Are you sure you want to delete all recordings without an assigned unit/course?")) return;
    const btn = el("deleteUnassignedBtn");
    btn.disabled = true;
    try {
      const res = await fetch(`${API}/api/teacher/recordings/delete-unassigned`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ passcode: state.passcode })
      });
      const data = await res.json();
      if (res.ok) {
        toast("Unassigned recordings deleted.", "success");
        loadTeacherRecordings();
        loadStats();
      } else {
        toast(data.error || "Delete failed.", "error");
      }
    } catch (e) {
      toast("Network error.", "error");
    } finally {
      btn.disabled = false;
    }
  });
}

if (el("transcribeAllBtn")) {
  el("transcribeAllBtn").addEventListener("click", async () => {
    if (!confirm("Transcribe all recordings that are missing transcripts? This runs in the background.")) return;
    const btn = el("transcribeAllBtn");
    const progressBox = el("bulkProgress");
    const fill = el("bulkBarFill");
    const txt = el("bulkProgressText");
    btn.disabled = true;
    if (progressBox) progressBox.classList.remove("hidden");
    if (fill) fill.style.width = "15%";
    if (txt) txt.textContent = "Starting transcription queue…";

    try {
      const res = await fetch(`${API}/api/teacher/transcribe-all`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ passcode: state.passcode })
      });
      const data = await res.json();
      if (res.ok) {
        toast("Transcription queue started.", "info");
        if (fill) fill.style.width = "100%";
        if (txt) txt.textContent = data.message || "Background transcription in progress.";
        setTimeout(() => {
          if (progressBox) progressBox.classList.add("hidden");
          loadTeacherRecordings();
        }, 3000);
      } else {
        toast(data.error || "Failed to start queue.", "error");
        if (progressBox) progressBox.classList.add("hidden");
      }
    } catch (e) {
      toast("Network error.", "error");
      if (progressBox) progressBox.classList.add("hidden");
    } finally {
      btn.disabled = false;
    }
  });
}
let teacherRecordings = [];

async function loadTeacherRecordings() {
  const res = await fetch(`${API}/api/teacher/recordings`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ passcode: state.passcode })
  });
  const data = await res.json();
  teacherRecordings = (data.recordings || []).map((r, i) => ({ ...r, _order: i }));
  populateCourseFilter(teacherRecordings);
  applyRecFilters();
}

function populateCourseFilter(list) {
  const sel = el("recCourseFilter");
  if(!sel) return;
  const current = sel.value;
  const units = Array.from(new Set(list.map(r => r.unit || "Unassigned"))).sort();
  sel.innerHTML = '<option value="">All courses</option>' +
    units.map(u => `<option value="${escapeHtml(u)}">${escapeHtml(u)}</option>`).join("");
  if (current && units.includes(current)) sel.value = current;
}

function applyRecFilters() {
  const searchEl = el("recSearch");
  const courseEl = el("recCourseFilter");
  const typeEl = el("recTypeFilter");
  const sortEl = el("recSort");

  const q = searchEl ? (searchEl.value || "").toLowerCase() : "";
  const course = courseEl ? courseEl.value : "";
  const type = typeEl ? typeEl.value : "";
  const sort = sortEl ? sortEl.value : "date_asc";

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
  list.sort(sorters[sort] || sorters.date_asc);

  const countEl = el("recCount");
  if(countEl) countEl.textContent = `${list.length} of ${teacherRecordings.length} recording${teacherRecordings.length === 1 ? "" : "s"}`;
  renderTeacherRecordings(list);
}

if(el("recSearch")) el("recSearch").addEventListener("input", applyRecFilters);
if(el("recCourseFilter")) el("recCourseFilter").addEventListener("change", applyRecFilters);
if(el("recTypeFilter")) el("recTypeFilter").addEventListener("change", applyRecFilters);
if(el("recSort")) el("recSort").addEventListener("change", applyRecFilters);

function renderTeacherRecordings(list) {
  const box = el("tRecList"); 
  if(!box) return;
  box.innerHTML = "";
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

    async function refreshLibDropdown() {
      try {
        const res = await fetch(`${API}/api/teacher/notes/library`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ passcode: state.passcode, for_recording: r.id })
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
      } catch (e) {}
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
  const box = el("qLog"); 
  if(!box) return;
  box.innerHTML = "";
  const qs = data.questions || [];
  if (!qs.length) { box.innerHTML = '<div class="q-empty">No questions yet. They\'ll show up here as students ask.</div>'; return; }
  
  qs.forEach(q => {
    const row = document.createElement("div"); 
    row.className = "q-row";
    row.style.cssText = "display: flex; flex-direction: column; gap: 8px; padding: 14px; background: var(--panel); border: 1.5px solid var(--line); border-radius: 12px; margin-bottom: 12px;";
    
    row.innerHTML = `
      <div class="q-meta" style="font-weight: 700; color: var(--brand-d);">
        👤 ${escapeHtml(q.student)} · 📁 ${escapeHtml(q.recording_title)} (${escapeHtml(q.unit)}) · 🕒 ${escapeHtml(q.time)}
      </div>
      <div style="font-size: 14px; font-weight: 800; color: var(--text);">
        ❓ Q: ${escapeHtml(q.question)}
      </div>
      <div style="font-size: 13.5px; color: var(--muted); background: var(--bg); padding: 10px; border-radius: 8px; border-left: 3px solid var(--brand); line-height: 1.4;">
        🤖 <strong>AI Answer:</strong> ${escapeHtml(q.answer || "No answer recorded.")}
      </div>
    `;
    box.appendChild(row);
  });
}

if(el("savePass")) {
  el("savePass").addEventListener("click", async () => {
    const newPassEl = el("newPass");
    if(!newPassEl) return;
    const np = newPassEl.value.trim();
    if (!np) return;
    const res = await fetch(`${API}/api/teacher/passcode`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ passcode: state.passcode, new_passcode: np })
    });
    if (res.ok) { 
      state.passcode = np; 
      newPassEl.value = ""; 
      if(el("passSaved")) {
        el("passSaved").classList.remove("hidden"); 
        setTimeout(() => el("passSaved").classList.add("hidden"), 2000); 
      }
    }
  });
}

function signOut() {
  state.name = ""; state.token = ""; state.passcode = ""; state.current = null; state.recordings = [];
  localStorage.removeItem("ng_studentToken");
  localStorage.removeItem("ng_studentName");
  localStorage.removeItem("ng_teacherPasscode");
  const p = el("passInput"); if (p) p.value = "";
  const em = el("emailInput"); if (em) em.value = "";
  const pw = el("passwordInput"); if (pw) pw.value = "";
  show("landing");
}
if(el("studentSignOut")) el("studentSignOut").addEventListener("click", signOut);
if(el("teacherSignOut")) el("teacherSignOut").addEventListener("click", signOut);

if(el("saveLogo")) {
  el("saveLogo").addEventListener("click", async () => {
    const fileInput = el("logoFile");
    const ok = el("logoSaved"); const err = el("logoErr");
    if(ok) ok.classList.add("hidden"); 
    if(err) err.classList.add("hidden");
    if (!fileInput || !fileInput.files.length) { if(err) { err.textContent = "Choose an image file first."; err.classList.remove("hidden"); } return; }
    const fd = new FormData();
    fd.append("file", fileInput.files[0]);
    fd.append("passcode", state.passcode);
    try {
      const res = await fetch(`${API}/api/teacher/logo`, { method: "POST", body: fd });
      const data = await res.json();
      if (!res.ok || data.error) { if(err) { err.textContent = data.error || "Upload failed."; err.classList.remove("hidden"); } return; }
      if(ok) {
        ok.classList.remove("hidden"); 
        setTimeout(() => ok.classList.add("hidden"), 2500);
      }
      fileInput.value = "";
      applyLogo(data.logo + "?t=" + Date.now());
    } catch (e) {
      if(err) {
        err.textContent = "Couldn't reach the server. Try again.";
        err.classList.remove("hidden");
      }
    }
  });
}

function applyLogo(url) {
  if (!url) return;
  document.querySelectorAll(".logo, .logo-sm").forEach(node => {
    if (node.dataset.emoji === undefined) node.dataset.emoji = node.innerHTML;
    const img = document.createElement("img");
    img.alt = "logo";
    img.addEventListener("error", () => { node.innerHTML = node.dataset.emoji; });
    img.src = url;
    node.innerHTML = "";
    node.appendChild(img);
  });
  const preview = el("logoPreview");
  if (preview) { preview.src = url; preview.classList.remove("hidden"); }
}

async function loadBranding() {
  try {
    const res = await fetch(`${API}/api/branding`);
    const data = await res.json();
    if (data.logo) applyLogo(data.logo + "?t=" + Date.now());
  } catch (e) {}
}

// ==============================================================================
// STUDENT PAST PAPER SOLVER (DIRECT LIBRARY SELECTION)
// ==============================================================================
let ppStudentLibrary = [];

async function initStudentPastPapers() {
  const sel = el("ppCourseSelect");
  if (!sel) return;

  const courseSet = new Set();
  (state.recordings || []).forEach(r => {
    const u = (r.unit || "").trim();
    if (u && u.toLowerCase() !== "unassigned") courseSet.add(u);
  });

  try {
    const res = await fetch(`${API}/api/student/pastpaper/meta`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: state.token })
    });
    if (res.ok) {
      const data = await res.json();
      state.courseSyllabi = data.syllabi || {};
      ppStudentLibrary = data.library || [];
      (data.courses || []).forEach(c => {
        if (c && c.trim() && c.trim().toLowerCase() !== "unassigned") courseSet.add(c.trim());
      });
    }
  } catch (e) {
    console.error("Error loading past paper metadata:", e);
  }

  const courses = Array.from(courseSet).sort();
  sel.innerHTML = '<option value="">Select course...</option>';
  if (courses.length === 0) {
    sel.innerHTML += '<option value="" disabled>(No courses available)</option>';
  } else {
    courses.forEach(c => sel.appendChild(new Option(c, c)));
  }

  sel.onchange = () => {
    const chosen = sel.value;
    const badge = el("ppSyllabusBadge");
    if (badge) {
      badge.textContent = state.courseSyllabi[chosen]
        ? `🎯 Syllabus: ${state.courseSyllabi[chosen]}`
        : "Standard Exam Board Specification";
    }
    populateStudentCascade("year");
  };
}

function populateStudentCascade(level) {
  const course = el("ppCourseSelect").value;
  const yearSel = el("ppYearSelect");
  const seriesSel = el("ppSeriesSelect");
  const paperSel = el("ppPaperSelect");
  const qSel = el("ppQuestionSelect");

  if (!course) return;

  if (level === "year") {
    const years = [...new Set(ppStudentLibrary.filter(x => x.course === course).map(x => x.year))].sort().reverse();
    yearSel.innerHTML = '<option value="">Select Year...</option>';
    years.forEach(y => yearSel.appendChild(new Option(y, y)));
    yearSel.disabled = years.length === 0;
    seriesSel.disabled = true; paperSel.disabled = true; qSel.disabled = true;
    yearSel.onchange = () => populateStudentCascade("series");
  } else if (level === "series") {
    const year = yearSel.value;
    const series = [...new Set(ppStudentLibrary.filter(x => x.course === course && x.year === year).map(x => x.series))];
    seriesSel.innerHTML = '<option value="">Select Series...</option>';
    series.forEach(s => seriesSel.appendChild(new Option(s, s)));
    seriesSel.disabled = series.length === 0;
    paperSel.disabled = true; qSel.disabled = true;
    seriesSel.onchange = () => populateStudentCascade("paper");
  } else if (level === "paper") {
    const year = yearSel.value;
    const series = seriesSel.value;
    const papers = [...new Set(ppStudentLibrary.filter(x => x.course === course && x.year === year && x.series === series).map(x => x.paper))];
    paperSel.innerHTML = '<option value="">Select Paper...</option>';
    papers.forEach(p => paperSel.appendChild(new Option(p, p)));
    paperSel.disabled = papers.length === 0;
    qSel.disabled = true;
    paperSel.onchange = () => populateStudentCascade("question");
  } else if (level === "question") {
    const year = yearSel.value;
    const series = seriesSel.value;
    const paper = paperSel.value;
    const qs = [...new Set(ppStudentLibrary.filter(x => x.course === course && x.year === year && x.series === series && x.paper === paper).map(x => x.question))];
    qSel.innerHTML = '<option value="">Select Question...</option>';
    qs.forEach(q => qSel.appendChild(new Option(q, q)));
    qSel.disabled = qs.length === 0;
  }
}

if (el("solvePastPaperBtn")) {
  el("solvePastPaperBtn").addEventListener("click", async () => {
    const course = el("ppCourseSelect").value;
    const year = el("ppYearSelect").value;
    const series = el("ppSeriesSelect").value;
    const paper = el("ppPaperSelect").value;
    const question = el("ppQuestionSelect").value;

    if (!course) { toast("Please select a course.", "info"); return; }
    if (!year || !series || !paper || !question) {
      toast("Please complete all dropdowns to pick a question.", "info");
      return;
    }

    const fd = new FormData();
    fd.append("token", state.token);
    fd.append("course", course);
    fd.append("year", year);
    fd.append("series", series);
    fd.append("paper", paper);
    fd.append("question", question);
    fd.append("doubt", el("ppDoubtInput") ? el("ppDoubtInput").value.trim() : "");

    const btn = el("solvePastPaperBtn");
    const origText = btn.innerText;
    btn.innerText = "⏳ Generating Official Solution & Model Answer...";
    btn.disabled = true;

    try {
      const res = await fetch(`${API}/api/student/pastpaper/solve`, { method: "POST", body: fd });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Solving failed.");
      renderStudentPastPaperSolution(data);
    } catch (err) {
      toast(err.message, "error");
    } finally {
      btn.innerText = origText;
      btn.disabled = false;
    }
  });
}

function renderStudentPastPaperSolution(data) {
  if (el("ppEmptyState")) el("ppEmptyState").classList.add("hidden");
  const resBox = el("ppSolutionResult");
  if (!resBox) return;
  resBox.classList.remove("hidden");
  resBox.innerHTML = "";

  // Smoothly scroll the container to the top of the answer
  const pane = el("studentPastPapersPane");
  if (pane) pane.scrollTo({ top: 0, behavior: "smooth" });
  
  // 1. Optional Teacher Resource Card
  if (data.teacher_asset) {
    const asset = data.teacher_asset;
    const assetCard = document.createElement("div");
    assetCard.style.cssText = "background: linear-gradient(135deg, rgba(11,191,191,0.08), rgba(12,166,120,0.12)); border: 1.5px solid var(--brand); border-radius: 12px; padding: 14px; margin-bottom: 16px;";
    
    let links = "";
    if (asset.video_url) {
      links += `<a href="${escapeHtml(asset.video_url)}" target="_blank" class="primary" style="display:inline-flex; align-items:center; gap:6px; padding:6px 12px; font-size:12px; text-decoration:none; margin-right:8px; border-radius:8px;">🎥 Watch Video Walkthrough</a>`;
    }
    if (asset.answered_doc_name) {
      links += `<span class="ghost-sm" style="padding:6px 12px; font-size:12px; border-radius:8px;">📄 Model Answer Doc: <strong>${escapeHtml(asset.answered_doc_name)}</strong></span>`;
    }

    assetCard.innerHTML = `
      <div style="font-weight:800; font-size:13.5px; color:var(--brand-d); margin-bottom:6px;">👨‍🏫 Teacher Materials Linked</div>
      <div>${links || '<span class="meta">Teacher resources indexed for this question.</span>'}</div>
    `;
    resBox.appendChild(assetCard);
  }

  const formatText = (txt) => {
    let clean = escapeHtml(txt || "");
    return clean.replace(/\*\*(.+?)\*\*/g, '<strong style="color: var(--brand-d); background: rgba(11,191,191,0.12); padding: 1px 6px; border-radius: 4px; font-weight: 700;">$1</strong>');
  };

  // 2. Parse the 4 Sections
  const raw = data.solution_markdown || "";
  const sec1Match = raw.match(/###\s*1\.[^\n]*\n([\s\S]*?)(?=###\s*2\.|$)/i);
  const sec2Match = raw.match(/###\s*2\.[^\n]*\n([\s\S]*?)(?=###\s*3\.|$)/i);
  const sec3Match = raw.match(/###\s*3\.[^\n]*\n([\s\S]*?)(?=###\s*4\.|$)/i);
  const sec4Match = raw.match(/###\s*4\.[^\n]*\n([\s\S]*?)$/i);

  const sec1Raw = sec1Match ? sec1Match[1].trim() : "";
  const sec2Raw = sec2Match ? sec2Match[1].trim() : "";
  const sec3Raw = sec3Match ? sec3Match[1].trim() : "";
  const sec4Raw = sec4Match ? sec4Match[1].trim() : "";

  const wrap = document.createElement("div");
  wrap.style.cssText = "display: flex; flex-direction: column; gap: 16px;";

  // Header Banner
  wrap.innerHTML = `
    <div style="display: flex; justify-content: space-between; align-items: center; background: var(--panel); border: 1.5px solid var(--line); border-radius: 12px; padding: 12px 18px;">
      <div style="display: flex; align-items: center; gap: 10px;">
        <span class="q-num" style="font-size: 13.5px; font-weight: 800; padding: 4px 12px; border-radius: 20px;">${escapeHtml(data.exam_ref)}</span>
        <span style="font-size: 13px; font-weight: 700; color: var(--text);">Exam Solution &amp; Examiner Guide</span>
      </div>
      <span class="meta" style="font-weight: 800; color: var(--brand-d); font-size: 12px;">${escapeHtml(data.syllabus)}</span>
    </div>
  `;

  // CARD 1: Complete Model Answer (A* Student Paper View)
  if (sec1Raw) {
    const card1 = document.createElement("div");
    card1.style.cssText = "background: linear-gradient(180deg, var(--panel), var(--panel2)); border: 2px solid var(--brand); border-radius: 14px; padding: 18px 20px; box-shadow: var(--shadow-sm);";

    const paras = sec1Raw.split(/\n\s*\n/).filter(Boolean);
    const bodyHtml = paras.map(p => `<p style="font-size: 14px; line-height: 1.7; color: var(--text); margin-bottom: 10px; font-style: normal;">${formatText(p.trim())}</p>`).join("");

    card1.innerHTML = `
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; border-bottom: 1.5px solid var(--line); padding-bottom: 8px;">
        <div style="display: flex; align-items: center; gap: 8px;">
          <span style="font-size: 18px;">🏆</span>
          <h4 style="margin: 0; font-size: 15px; font-weight: 800; color: var(--brand-d);">1. Complete Model Answer (Full Marks)</h4>
        </div>
        <span style="font-size: 11px; font-weight: 800; background: var(--brand); color: white; padding: 3px 8px; border-radius: 12px;">A* EXAM SCRIPT</span>
      </div>
      <div style="background: var(--bg); padding: 14px 18px; border-radius: 10px; border-left: 4px solid var(--brand);">${bodyHtml}</div>
    `;
    wrap.appendChild(card1);
  }

  // CARD 2: Mark Scheme Breakdown & Mandatory Keywords
  if (sec2Raw) {
    const card2 = document.createElement("div");
    card2.style.cssText = "background: var(--panel); border: 1.5px solid var(--line); border-radius: 14px; padding: 18px 20px; box-shadow: var(--shadow-sm);";

    const lines = sec2Raw.split("\n").map(l => l.trim()).filter(Boolean);
    let rubricHtml = "";

    lines.forEach(line => {
      const numMatch = line.match(/^(\d+)\.\s*(.*)/);
      if (numMatch) {
        rubricHtml += `
          <div style="display: flex; gap: 12px; align-items: flex-start; padding: 8px 0; border-bottom: 1px dashed var(--line);">
            <span style="background: var(--brand-d); color: #fff; font-weight: 800; font-size: 11px; min-width: 24px; height: 24px; border-radius: 50%; display: flex; align-items: center; justify-content: center; margin-top: 1px;">${numMatch[1]}</span>
            <div style="font-size: 13.5px; line-height: 1.5; color: var(--text); flex: 1;">${formatText(numMatch[2])}</div>
          </div>
        `;
      } else {
        rubricHtml += `<p style="font-size: 13px; color: var(--muted); margin-bottom: 8px;">${formatText(line)}</p>`;
      }
    });

    card2.innerHTML = `
      <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 12px; border-bottom: 1.5px solid var(--line); padding-bottom: 8px;">
        <span style="font-size: 17px;">🎯</span>
        <h4 style="margin: 0; font-size: 15px; font-weight: 800; color: var(--brand-d);">2. Mark Scheme Breakdown &amp; Mandatory Keywords</h4>
      </div>
      <div>${rubricHtml}</div>
    `;
    wrap.appendChild(card2);
  }

  // CARD 3: Mechanism & Conceptual Link
  if (sec3Raw) {
    const card3 = document.createElement("div");
    card3.style.cssText = "background: var(--panel); border: 1.5px solid var(--line); border-radius: 14px; padding: 18px 20px; box-shadow: var(--shadow-sm);";

    const paras = sec3Raw.split(/\n\s*\n/).filter(Boolean);
    const bodyHtml = paras.map(p => `<p style="font-size: 13.5px; line-height: 1.65; color: var(--text); margin-bottom: 10px;">${formatText(p.trim())}</p>`).join("");

    card3.innerHTML = `
      <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 12px; border-bottom: 1.5px solid var(--line); padding-bottom: 8px;">
        <span style="font-size: 17px;">🧬</span>
        <h4 style="margin: 0; font-size: 15px; font-weight: 800; color: var(--text);">3. Conceptual Link &amp; Biological Mechanism</h4>
      </div>
      <div>${bodyHtml}</div>
    `;
    wrap.appendChild(card3);
  }

  // CARD 4: Examiner Traps & Common Mistakes
  if (sec4Raw) {
    const card4 = document.createElement("div");
    card4.style.cssText = "background: rgba(245, 159, 0, 0.05); border: 1.5px solid rgba(245, 159, 0, 0.35); border-radius: 14px; padding: 18px 20px;";

    const trapLines = sec4Raw.split("\n").map(l => l.trim()).filter(Boolean);
    let trapsHtml = "";

    trapLines.forEach(line => {
      const cleanLine = line.replace(/^[-*]\s*/, "");
      trapsHtml += `
        <div style="display: flex; gap: 10px; align-items: flex-start; margin-bottom: 8px;">
          <span style="color: #f59f00; font-size: 14px; margin-top: 2px;">⚠️</span>
          <div style="font-size: 13px; line-height: 1.5; color: var(--text);">${formatText(cleanLine)}</div>
        </div>
      `;
    });

    card4.innerHTML = `
      <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 12px; border-bottom: 1px solid rgba(245, 159, 0, 0.25); padding-bottom: 8px;">
        <h4 style="margin: 0; font-size: 15px; font-weight: 800; color: #f59f00;">4. Examiner Traps &amp; Common Mistakes</h4>
      </div>
      <div>${trapsHtml}</div>
    `;
    wrap.appendChild(card4);
  }

  // Fallback if headings were altered
  if (!sec1Raw && !sec2Raw && !sec3Raw && !sec4Raw) {
    const fallback = document.createElement("div");
    fallback.className = "q-block";
    fallback.style.padding = "20px";
    fallback.innerHTML = `<div style="line-height: 1.65; font-size: 13.5px;">${formatText(raw).replace(/\n/g, '<br>')}</div>`;
    wrap.appendChild(fallback);
  }

  resBox.appendChild(wrap);
}

// ==============================================================================
// TEACHER PAST PAPER HUB (ISOLATED LIBRARY)
// ==============================================================================
async function loadTeacherPastPaperHub() {
  return refreshPastPaperHub();
}

// 1. Save Syllabus Mapping with Direct UI Feedback
let teacherSyllabiCache = {};

if (el("saveSyllabusBtn")) {
  el("saveSyllabusBtn").addEventListener("click", async () => {
    const course = el("tppCourseSelect").value;
    const syllabus = el("tppSyllabusCode").value;
    const btn = el("saveSyllabusBtn");

    if (!course) {
      toast("Please select a course first.", "info");
      return;
    }

    const passcode = state.passcode || localStorage.getItem("ng_teacherPasscode") || "";
    if (!passcode) {
      toast("Session expired: please sign in again.", "error");
      return;
    }

    const origText = btn.textContent;
    btn.textContent = "Saving…";
    btn.disabled = true;

    try {
      const res = await fetch(`${API}/api/teacher/pastpaper/config/save`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ passcode, course, syllabus })
      });
      const data = await res.json();

      if (res.ok) {
        btn.textContent = "Saved ✓";
        btn.style.background = "var(--ok, #0ca678)";
        toast(`Mapped "${course}" to ${syllabus} ✓`, "success", 4000);
        teacherSyllabiCache[course] = syllabus;
        setTimeout(() => {
          btn.textContent = origText;
          btn.style.background = "";
          btn.disabled = false;
        }, 2000);
      } else {
        btn.textContent = origText;
        btn.disabled = false;
        toast(data.error || "Failed to save mapping.", "error");
      }
    } catch (e) {
      btn.textContent = origText;
      btn.disabled = false;
      toast("Network error saving syllabus.", "error");
    }
  });
}

// Automatically display the existing saved syllabus when switching courses
if (el("tppCourseSelect")) {
  el("tppCourseSelect").addEventListener("change", () => {
    const selectedCourse = el("tppCourseSelect").value;
    if (selectedCourse && teacherSyllabiCache[selectedCourse]) {
      el("tppSyllabusCode").value = teacherSyllabiCache[selectedCourse];
      toast(`Loaded current mapping: ${teacherSyllabiCache[selectedCourse]}`, "info", 2500);
    }
  });
}

// ==========================================
// PAST PAPER CURATED QUESTION LIBRARY
// ==========================================

function parseExamPackId(examKey) {
  const m = String(examKey || "").match(/^(\d{4})\s+(.+?)\s+Paper\s+(.+)$/i);
  if (!m) return { year: "", series: "", paper: "" };
  return { year: m[1], series: m[2].trim(), paper: m[3].trim() };
}

function renderTeacherOverrides(list) {
  const container = el("tppOverridesList");
  if (!container) return;
  container.innerHTML = "";

  if (!Array.isArray(list) || !list.length) {
    container.innerHTML = '<p class="meta">No questions added to the library yet.</p>';
    return;
  }

  const tree = {};
  list.forEach(item => {
    const course = item.course || "Unassigned Course";
    const examKey = `${item.year || ""} ${item.series || ""} Paper ${item.paper || ""}`.trim();
    if (!tree[course]) tree[course] = {};
    if (!tree[course][examKey]) tree[course][examKey] = [];
    tree[course][examKey].push(item);
  });

  Object.keys(tree).sort((a,b) => a.localeCompare(b)).forEach(courseName => {
    const courseItems = tree[courseName];
    const examKeys = Object.keys(courseItems).sort((a,b) => b.localeCompare(a, undefined, {numeric:true}));
    const courseQuestionCount = examKeys.reduce((n, k) => n + courseItems[k].length, 0);

    const courseDetails = document.createElement("details");
    courseDetails.style.cssText = "width:100%;box-sizing:border-box;background:var(--panel);border:1.5px solid var(--line);border-radius:12px;margin-bottom:14px;overflow:hidden;box-shadow:var(--shadow-sm);";

    const courseSummary = document.createElement("summary");
    courseSummary.style.cssText = "list-style:none;cursor:pointer;padding:13px 16px;background:var(--panel2);font-weight:800;color:var(--brand-d);display:flex;align-items:center;justify-content:space-between;gap:12px;";
    courseSummary.innerHTML = `
      <span>📚 Course: ${escapeHtml(courseName)}</span>
      <span class="meta" style="font-size:11.5px;background:var(--panel);padding:3px 9px;border-radius:12px;">${examKeys.length} exam${examKeys.length === 1 ? "" : "s"} · ${courseQuestionCount} question${courseQuestionCount === 1 ? "" : "s"}</span>`;
    courseDetails.appendChild(courseSummary);

    const courseBody = document.createElement("div");
    courseBody.style.cssText = "padding:12px 14px;";

    examKeys.forEach(examKey => {
      const qItems = courseItems[examKey].slice().sort((a,b) => String(a.question).localeCompare(String(b.question), undefined, {numeric:true}));
      const parsed = parseExamPackId(examKey);

      const examDetails = document.createElement("details");
      examDetails.style.cssText = "border:1px solid var(--line);border-radius:10px;background:var(--panel2);margin-bottom:10px;overflow:hidden;";

      const examSummary = document.createElement("summary");
      examSummary.style.cssText = "list-style:none;cursor:pointer;padding:11px 13px;display:flex;align-items:center;justify-content:space-between;gap:12px;";

      const examTitle = document.createElement("span");
      examTitle.innerHTML = `<strong style="font-size:13.5px;color:var(--text);">📝 ${escapeHtml(examKey)}</strong><span class="meta" style="margin-left:8px;">${qItems.length} question${qItems.length===1?"":"s"}</span>`;

      const deleteExamBtn = document.createElement("button");
      deleteExamBtn.type = "button";
      deleteExamBtn.className = "ghost-sm danger-btn";
      deleteExamBtn.textContent = "🗑️ Delete Full Exam";
      deleteExamBtn.title = "Delete every curated question in this exam pack";
      deleteExamBtn.style.cssText = "padding:5px 10px;font-size:11px;white-space:nowrap;";
      deleteExamBtn.addEventListener("click", async e => {
        e.preventDefault();
        e.stopPropagation();
        const confirmText = `Delete the FULL exam\n\n${courseName} — ${examKey}\n\nThis will permanently remove ${qItems.length} question${qItems.length===1?"":"s"} from the Past Paper Library.\n\nUploaded answered/reference documents will NOT be deleted.\n\nContinue?`;
        if (!confirm(confirmText)) return;
        deleteExamBtn.disabled = true;
        const oldText = deleteExamBtn.textContent;
        deleteExamBtn.textContent = "Deleting…";
        try {
          const res = await fetch(`${API}/api/teacher/pastpaper/solutions/delete-exam`, {
            method:"POST",
            headers:{"Content-Type":"application/json"},
            body:JSON.stringify({passcode:state.passcode,course:courseName,year:parsed.year,series:parsed.series,paper:parsed.paper})
          });
          const data = await res.json().catch(() => ({}));
          if (!res.ok) {
            toast(data.error || "Could not delete the exam.", "error");
            return;
          }
          const deleted = data.deleted ?? qItems.length;
          toast(`Deleted ${deleted} question${deleted === 1 ? "" : "s"} from ${examKey} ✓`, "success", 4500);
          await refreshPastPaperHub();
        } catch (err) {
          console.error(err);
          toast("Network error while deleting the exam.", "error");
        } finally {
          deleteExamBtn.disabled = false;
          deleteExamBtn.textContent = oldText;
        }
      });

      examSummary.appendChild(examTitle);
      examSummary.appendChild(deleteExamBtn);
      examDetails.appendChild(examSummary);

      const qContainer = document.createElement("div");
      qContainer.style.cssText = "display:flex;flex-direction:column;gap:6px;padding:0 10px 10px;";
      qItems.forEach(q => {
        const row = document.createElement("div");
        row.style.cssText = "display:flex;justify-content:space-between;align-items:center;gap:10px;padding:8px 10px;background:var(--bg);border:1px solid var(--line);border-radius:8px;font-size:12px;";
        const metaBits = [];
        if (q.examiner_notes && q.examiner_notes.trim()) metaBits.push("📊 Examiner Report");
        if (q.video_url) metaBits.push("🎥 Video");
        if (q.answered_doc_name) metaBits.push(`📄 ${q.answered_doc_name}`);
        row.innerHTML = `
          <div style="min-width:0;display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
            <strong style="color:var(--brand-d);">Q${escapeHtml(q.question)}</strong>
            ${metaBits.length ? `<span class="meta">${escapeHtml(metaBits.join(" · "))}</span>` : ""}
          </div>
          <button type="button" class="ghost-sm danger-btn pp-question-delete" style="padding:2px 8px;font-size:10.5px;">🗑️ Delete</button>`;
        row.querySelector(".pp-question-delete").addEventListener("click", async () => {
          if (!confirm(`Delete question Q${q.question} from ${examKey}?`)) return;
          const btn = row.querySelector(".pp-question-delete");
          btn.disabled = true;
          try {
            const res = await fetch(`${API}/api/teacher/pastpaper/solutions/delete`, {
              method:"POST",
              headers:{"Content-Type":"application/json"},
              body:JSON.stringify({passcode:state.passcode,key:q.key})
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
              toast(data.error || "Could not delete the question.", "error");
              return;
            }
            toast(`Q${q.question} deleted ✓`, "success");
            await refreshPastPaperHub();
          } catch (err) {
            toast("Network error while deleting the question.", "error");
          } finally {
            btn.disabled = false;
          }
        });
        qContainer.appendChild(row);
      });
      examDetails.appendChild(qContainer);
      courseBody.appendChild(examDetails);
    });

    courseDetails.appendChild(courseBody);
    container.appendChild(courseDetails);
  });
}

// ==========================================
// FIX: PAST PAPER HUB COURSES & DOCUMENT LIBRARY
// ==========================================

async function refreshPastPaperHub() {
  const passcode = (typeof state !== "undefined" && state.passcode)
    || localStorage.getItem("ng_teacherPasscode")
    || "";
  if (!passcode) return;

  let ppConfig = { courses: [], pp_library: [], syllabi: {}, solutions: [] };
  try {
    const res = await fetch(`${API}/api/teacher/pastpaper/config`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ passcode })
    });
    if (!res.ok) return;
    ppConfig = await res.json();
  } catch (err) {
    console.error("Failed to load past-paper config:", err);
    return;
  }

  const recordings = (typeof teacherRecordings !== "undefined" ? teacherRecordings : [])
    || (typeof state !== "undefined" ? state.recordings : [])
    || [];
  const courseSet = new Set();

  recordings.forEach(r => {
    const course = (r.unit || r.course || "").trim();
    if (course && course.toLowerCase() !== "unassigned") courseSet.add(course);
  });
  (ppConfig.courses || []).forEach(course => {
    if (course && course.trim() && course.trim().toLowerCase() !== "unassigned") {
      courseSet.add(course.trim());
    }
  });

  const allCourses = Array.from(courseSet).sort((a,b) => a.localeCompare(b));
  const ppDocs = Array.isArray(ppConfig.pp_library) ? ppConfig.pp_library : [];
  if (ppConfig.syllabi && typeof ppConfig.syllabi === "object") teacherSyllabiCache = ppConfig.syllabi;

  // Teacher course selectors only. Student course selection comes from the authenticated session.
  ["tppCourseSelect","bulkCourseSelect","tqCourseSelect","docCourseSelect"].forEach(id => {
    const select = document.getElementById(id);
    if (!select) return;
    const current = select.value;
    select.innerHTML = '<option value="">-- Select Course --</option>';
    allCourses.forEach(c => select.appendChild(new Option(c,c)));
    if (current && allCourses.includes(current)) select.value = current;
  });

  // Render the shared document library and allow every existing document to be reassigned.
  const container = document.getElementById("existingDocsContainer");
  const badge = document.getElementById("docCountBadge");
  if (badge) badge.textContent = String(ppDocs.length);

  if (container) {
    container.innerHTML = "";
    if (!ppDocs.length) {
      container.innerHTML = '<p class="meta" style="margin:0;font-size:12.5px;">No course reference/answered documents uploaded yet.</p>';
    } else {
      ppDocs.forEach(doc => {
        const row = document.createElement("div");
        row.className = "pastpaper-doc-row";
        row.style.cssText = [
          "display:grid",
          "grid-template-columns:minmax(260px,1fr) minmax(190px,auto) auto",
          "align-items:center",
          "gap:14px",
          "width:100%",
          "box-sizing:border-box",
          "padding:11px 12px",
          "background:var(--bg)",
          "border:1px solid var(--line)",
          "border-radius:9px"
        ].join(";");

        const options = [
          '<option value="">-- Unassigned / Shared --</option>',
          ...allCourses.map(c => {
            const selected = (doc.course || "").trim().toLowerCase() === c.toLowerCase() ? " selected" : "";
            return `<option value="${escapeHtml(c)}"${selected}>${escapeHtml(c)}</option>`;
          })
        ].join("");

        const fileName = doc.filename || "Untitled document";
        row.innerHTML = `
          <div style="min-width:0;display:flex;flex-direction:column;gap:4px;">
            <span class="pastpaper-doc-name" title="${escapeHtml(fileName)}" style="font-size:13px;font-weight:800;white-space:normal;overflow:visible;text-overflow:clip;overflow-wrap:anywhere;word-break:break-word;">📄 ${escapeHtml(fileName)}</span>
            <span class="meta" style="font-size:10.5px;">${doc.uploaded_at ? `Uploaded ${escapeHtml(doc.uploaded_at)} · ` : ""}${Number(doc.text_chars || 0).toLocaleString()} characters indexed</span>
          </div>
          <label style="display:flex;align-items:center;gap:7px;font-size:11px;font-weight:800;white-space:nowrap;">
            Course
            <select class="doc-course-assign" data-docid="${escapeHtml(doc.id)}" style="min-width:180px;padding:6px 9px;font-size:11.5px;border-radius:7px;border:1px solid var(--line);">${options}</select>
          </label>
          <button class="ghost-sm danger-btn doc-delete-btn" type="button" title="Delete document" style="padding:7px 10px;">🗑️</button>`;

        const assign = row.querySelector(".doc-course-assign");
        assign.addEventListener("change", async e => {
          const oldValue = doc.course || "";
          const fd = new FormData();
          fd.append("passcode", passcode);
          fd.append("doc_id", doc.id);
          fd.append("course", e.target.value);
          e.target.disabled = true;
          try {
            const res = await fetch(`${API}/api/teacher/pastpaper/docs/update-course`, { method: "POST", body: fd });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
              toast(data.error || "Failed to update document course.", "error");
              e.target.value = oldValue;
              return;
            }
            doc.course = e.target.value;
            toast(`"${fileName}" assigned to ${e.target.value || "Shared / Unassigned"} ✓`, "success");
          } catch (err) {
            console.error(err);
            toast("Network error updating document course.", "error");
            e.target.value = oldValue;
          } finally {
            e.target.disabled = false;
          }
        });
        row.querySelector(".doc-delete-btn").addEventListener("click", () => deletePastPaperDoc(doc.id, fileName));
        container.appendChild(row);
      });
    }
  }

  function populateLinkedDocSelect(id, selectedCourse) {
    const select = document.getElementById(id);
    if (!select) return;
    const current = select.value;
    const course = (selectedCourse || "").trim().toLowerCase();
    const eligible = ppDocs.filter(doc => {
      const dc = (doc.course || "").trim().toLowerCase();
      return !course || !dc || dc === course;
    });
    select.innerHTML = '<option value="">-- Optional: Link Reference Document --</option>';
    eligible.forEach(doc => {
      select.appendChild(new Option(`${doc.filename || "Untitled"} — ${doc.course || "Shared / Unassigned"}`, doc.id));
    });
    if (current && eligible.some(d => d.id === current)) select.value = current;
  }

  populateLinkedDocSelect("bulkDocSelect", document.getElementById("bulkCourseSelect")?.value || "");
  populateLinkedDocSelect("tqAnsweredDocSelect", document.getElementById("tqCourseSelect")?.value || "");

  ["bulkCourseSelect","tqCourseSelect"].forEach(id => {
    const select = document.getElementById(id);
    if (!select || select.dataset.ppDocBinding === "1") return;
    select.dataset.ppDocBinding = "1";
    select.addEventListener("change", () => {
      if (id === "bulkCourseSelect") populateLinkedDocSelect("bulkDocSelect", select.value);
      else populateLinkedDocSelect("tqAnsweredDocSelect", select.value);
    });
  });

  // Upload into the same canonical library used by the dropdown and manage-existing list.
  const uploadBtn = document.getElementById("uploadPpDocBtn");
  if (uploadBtn && uploadBtn.dataset.ppUploadBinding !== "1") {
    uploadBtn.dataset.ppUploadBinding = "1";
    uploadBtn.addEventListener("click", async () => {
      const fileInput = document.getElementById("ppDocFile");
      const course = document.getElementById("docCourseSelect")?.value || "";
      const file = fileInput?.files?.[0] || null;
      if (!course) return toast("Please select a course for this document.", "info");
      if (!file) return toast("Please choose a PDF, Word document, or text file.", "info");

      const fd = new FormData();
      fd.append("passcode", passcode);
      fd.append("course", course);
      fd.append("file", file);
      const oldText = uploadBtn.textContent;
      uploadBtn.disabled = true;
      uploadBtn.textContent = "Uploading…";
      try {
        const res = await fetch(`${API}/api/teacher/pastpaper/upload-doc`, { method: "POST", body: fd });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          toast(data.error || "Failed to upload document.", "error");
          return;
        }
        fileInput.value = "";
        toast(`"${data.doc?.filename || file.name}" uploaded to ${course} ✓`, "success");
        await refreshPastPaperHub();
      } catch (err) {
        console.error(err);
        toast("Network error uploading document.", "error");
      } finally {
        uploadBtn.disabled = false;
        uploadBtn.textContent = oldText;
      }
    });
  }

  renderTeacherOverrides(ppConfig.solutions || []);
}


// Bind the Bulk Upload & Auto-Extract action once.  The previous build had the
// button in the HTML and the API endpoint in Python, but no frontend handler,
// so clicking the button did nothing.
function bindPastPaperBulkUpload() {
  const btn = document.getElementById("processBulkBtn");
  if (!btn || btn.dataset.ppBulkBinding === "1") return;
  btn.dataset.ppBulkBinding = "1";

  btn.addEventListener("click", async (event) => {
    event.preventDefault();

    const passcode = (typeof state !== "undefined" && state.passcode)
      || localStorage.getItem("ng_teacherPasscode")
      || "";
    const course = document.getElementById("bulkCourseSelect")?.value?.trim() || "";
    const year = document.getElementById("bulkYearInput")?.value?.trim() || "";
    const series = document.getElementById("bulkSeriesSelect")?.value?.trim() || "";
    const paper = document.getElementById("bulkPaperInput")?.value?.trim() || "";
    const videoUrl = document.getElementById("bulkVideoUrl")?.value?.trim() || "";
    const answeredDocId = document.getElementById("bulkDocSelect")?.value || "";
    const qpFile = document.getElementById("bulkQpFile")?.files?.[0] || null;
    const msFile = document.getElementById("bulkMsFile")?.files?.[0] || null;
    const erFile = document.getElementById("bulkErFile")?.files?.[0] || null;

    if (!passcode) return toast("Your teacher session has expired. Please sign in again.", "error");
    if (!course) return toast("Please select a course.", "info");
    if (!year) return toast("Please enter the exam year.", "info");
    if (!series) return toast("Please select the exam series.", "info");
    if (!paper) return toast("Please enter the paper number.", "info");
    if (!qpFile) return toast("Please choose the Question Paper PDF.", "info");
    if (!msFile) return toast("Please choose the Mark Scheme PDF.", "info");

    const allowedPdf = /\.pdf$/i;
    if (!allowedPdf.test(qpFile.name) || !allowedPdf.test(msFile.name) || (erFile && !allowedPdf.test(erFile.name))) {
      return toast("Question Paper, Mark Scheme, and Examiner Report must be PDF files.", "error");
    }

    const fd = new FormData();
    fd.append("passcode", passcode);
    fd.append("course", course);
    fd.append("year", year);
    fd.append("series", series);
    fd.append("paper", paper);
    fd.append("video_url", videoUrl);
    fd.append("answered_doc_id", answeredDocId);
    fd.append("qp_file", qpFile, qpFile.name);
    fd.append("ms_file", msFile, msFile.name);
    if (erFile) fd.append("er_file", erFile, erFile.name);

    const oldText = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = "⏳ Extracting questions…";

    try {
      toast("Uploading the exam files and extracting questions…", "info", 5000);
      const res = await fetch(`${API}/api/teacher/pastpaper/bulk-upload`, {
        method: "POST",
        body: fd
      });

      const data = await res.json().catch(async () => ({
        error: (await res.text().catch(() => "")) || `HTTP ${res.status}`
      }));

      if (!res.ok || !data.ok) {
        const detail = data.error || data.detail || `Upload failed (HTTP ${res.status})`;
        console.error("Past-paper bulk upload failed:", res.status, data);
        toast(detail, "error", 7000);
        return;
      }

      const count = Number(data.indexed || 0);
      toast(`✅ Auto-extraction complete: ${count} question${count === 1 ? "" : "s"} indexed.`, "success", 6000);

      // Clear only the file inputs; keep the metadata so another paper can be uploaded.
      ["bulkQpFile", "bulkMsFile", "bulkErFile"].forEach(id => {
        const input = document.getElementById(id);
        if (input) input.value = "";
      });

      // Refresh the hub so the new questions / linked-answer state are visible immediately.
      await refreshPastPaperHub();
    } catch (err) {
      console.error("Network error during past-paper bulk upload:", err);
      toast(`Network error during upload: ${err?.message || err}`, "error", 7000);
    } finally {
      btn.disabled = false;
      btn.innerHTML = oldText;
    }
  });
}

async function deletePastPaperDoc(docId, filename = "this document") {
  if (!confirm(`Delete "${filename}" from the Past Paper document library?`)) return;
  const passcode = (typeof state !== "undefined" && state.passcode) || localStorage.getItem("ng_teacherPasscode") || "";
  try {
    const res = await fetch(`${API}/api/teacher/pastpaper/delete_doc`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ passcode, doc_id: docId })
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) return toast(data.error || "Failed to delete document.", "error");
    toast(`"${filename}" deleted ✓`, "success");
    await refreshPastPaperHub();
  } catch (err) {
    console.error(err);
    toast("Network error deleting document.", "error");
  }
}

// FIX: EDIT STUDENT SAVE HANDLER WITH DIAGNOSTICS
// ==========================================

async function executeStudentSave() {
  // 1. Locate Student ID across possible modal implementations
  const studentId = 
    document.getElementById('editStudentId')?.value || 
    document.getElementById('studentId')?.value ||
    document.getElementById('editStudentModal')?.dataset?.studentId || 
    window.editingStudentId;

  if (!studentId) {
    alert("⚠️ Save Failed: Could not find the Student ID on this modal. Check that your hidden input has id='editStudentId'.");
    return;
  }

  // 2. Collect field values (with fallbacks for various field naming conventions)
  const nameInput = document.getElementById('editStudentName') || document.getElementById('studentName');
  const emailInput = document.getElementById('editStudentEmail') || document.getElementById('studentEmail');
  const phoneInput = document.getElementById('editStudentPhone') || document.getElementById('studentPhone');
  const courseInput = document.getElementById('editStudentCourse') || document.getElementById('studentCourse');

  const passcode = window.state?.passcode || (typeof state !== "undefined" ? state.passcode : "");

  const payload = {
    passcode: passcode,
    student_id: studentId,
    id: studentId,
    name: nameInput ? nameInput.value.trim() : "",
    email: emailInput ? emailInput.value.trim() : "",
    phone: phoneInput ? phoneInput.value.trim() : "",
    course: courseInput ? courseInput.value : ""
  };

  // 3. UI Feedback - Loading State
  const saveBtn = document.getElementById('saveStudentBtn') || 
                  document.getElementById('saveEditStudentBtn') || 
                  document.querySelector('#editStudentModal button[type="submit"]');

  if (saveBtn) {
    saveBtn.disabled = true;
    saveBtn.innerText = "Saving...";
  }

  // 4. Try updating via primary teacher endpoint, then admin endpoint fallback
  const endpoints = [
    '/api/teacher/students/update',
    '/api/admin/students/update',
    '/api/students/update'
  ];

  let success = false;
  let lastError = "Server network error";

  for (const url of endpoints) {
    try {
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });

      if (res.ok) {
        success = true;
        break;
      } else {
        const errData = await res.json().catch(() => ({}));
        lastError = errData.error || errData.message || `HTTP ${res.status}`;
      }
    } catch (e) {
      lastError = e.message;
    }
  }

  // 5. Handle Outcome
  if (saveBtn) {
    saveBtn.disabled = false;
    saveBtn.innerText = "Save Changes";
  }

  if (success) {
    alert("✅ Student updated successfully!");
    
    // Close modal
    const modal = document.getElementById('editStudentModal') || document.getElementById('studentEditModal');
    if (modal) {
      modal.style.display = 'none';
      modal.classList.remove('active', 'show');
    }

    // Refresh student list
    if (typeof loadStudents === 'function') loadStudents();
    if (typeof renderStudents === 'function') renderStudents();
  } else {
    alert(`❌ Could not save changes.\nError: ${lastError}`);
  }
}

// Bind to click and submit events across the entire document
document.addEventListener('click', (e) => {
  const t = e.target;
  if (t && (t.id === 'saveStudentBtn' || t.id === 'saveEditStudentBtn' || t.classList.contains('save-student-btn'))) {
    e.preventDefault();
    executeStudentSave();
  }
});

document.addEventListener('submit', (e) => {
  if (e.target && (e.target.id === 'editStudentForm' || e.target.id === 'studentEditForm')) {
    e.preventDefault();
    executeStudentSave();
  }
});

bindPastPaperBulkUpload();
loadBranding();
