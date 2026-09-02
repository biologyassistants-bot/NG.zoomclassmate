const API = "";
let state = { 
  name: "", 
  recordings: [], 
  current: null, 
  passcode: "", 
  token: "", 
  role: "",
  chatHistory: {},
  flashcardDeck: [],
  courseSyllabi: {}
};

// Local tracking for student dashboard stats
let studentStats = JSON.parse(localStorage.getItem('studentStats_NGClassMate') || '{"questions":0, "quizzes":0}');
let currentStudyPlan = null;
let teacherStudentsCache = [];

function el(id) { return document.getElementById(id); }
function escapeHtml(s) { return (s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }

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
  } catch (e) {
    console.error("Error fetching profile:", e);
  }
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
  } catch (e) {
    console.error("Error syncing profile:", e);
  }
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
        }).catch(err => {
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
  
  if (!email || !password) { 
    if(errEl) { errEl.textContent = "Please enter your email and password."; errEl.classList.remove("hidden"); } 
    return; 
  }
  
  try {
    const res = await fetch(`${API}/api/student/login`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email, password })
    });
    const data = await res.json();
    
    if (!res.ok || !data.ok) { 
      if(errEl) { errEl.textContent = data.error || "Login failed."; errEl.classList.remove("hidden"); } 
      return; 
    }
    
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
if(el("tabStudentDash")) el("tabStudentDash").addEventListener("click", () => switchStudentTab("Dash"));
if(el("tabStudentTutor")) el("tabStudentTutor").addEventListener("click", () => switchStudentTab("Tutor"));
if(el("tabStudentPlanner")) el("tabStudentPlanner").addEventListener("click", () => switchStudentTab("Planner"));
if(el("tabStudentPastPapers")) el("tabStudentPastPapers").addEventListener("click", () => switchStudentTab("PastPapers"));
if(el("tabStudentAlerts")) el("tabStudentAlerts").addEventListener("click", () => switchStudentTab("Alerts"));

function switchStudentTab(name) {
  if(el("tabStudentDash")) el("tabStudentDash").classList.toggle("active", name === "Dash");
  if(el("tabStudentTutor")) el("tabStudentTutor").classList.toggle("active", name === "Tutor");
  if(el("tabStudentPlanner")) el("tabStudentPlanner").classList.toggle("active", name === "Planner");
  if(el("tabStudentPastPapers")) el("tabStudentPastPapers").classList.toggle("active", name === "PastPapers");
  if(el("tabStudentAlerts")) el("tabStudentAlerts").classList.toggle("active", name === "Alerts");
  
  if(el("studentDashPane")) el("studentDashPane").classList.toggle("hidden", name !== "Dash");
  if(el("studentTutorPane")) el("studentTutorPane").classList.toggle("hidden", name !== "Tutor");
  if(el("studentPlannerPane")) el("studentPlannerPane").classList.toggle("hidden", name !== "Planner");
  if(el("studentPastPapersPane")) el("studentPastPapersPane").classList.toggle("hidden", name !== "PastPapers");
  if(el("studentAlertsPane")) el("studentAlertsPane").classList.toggle("hidden", name !== "Alerts");

  if (name === "Dash") renderStudentDashboard();
  if (name === "Planner") {
    if (state.recordings.length === 0) {
      loadRecordings().then(() => initPlanner());
    } else {
      initPlanner();
    }
  }
  if (name === "PastPapers") initStudentPastPapers();
  if (name === "Alerts") renderAlertsPane();
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

function renderStudentDashboard() {
  const planPct = calculatePlanProgress();
  const statsBar = el("studentStatsBar");
  if(!statsBar) return;
  
  const courses = new Set(state.recordings.map(r => r.unit || "Unassigned"));
  const now = new Date();
  const dueCardsCount = (state.flashcardDeck || []).filter(c => new Date(c.dueDate) <= now).length;
  
  const cards = [
    { label: "Enrolled Courses", value: courses.size },
    { label: "Classes Available", value: state.recordings.length },
    { label: "Flashcards Due", value: dueCardsCount, sub: dueCardsCount > 0 ? "Review to prevent decay ⚠️" : "All caught up ✓" },
    { label: "Study Plan Progress", value: `${planPct}%`, sub: planPct === 100 ? "Completed! 🎉" : (currentStudyPlan ? "In progress" : "No active plan") },
    { label: "AI Questions Asked", value: studentStats.questions },
    { label: "Quizzes Generated", value: studentStats.quizzes }
  ];
  
  let html = cards.map(c => 
    `<div class="stat-card">
      <div class="stat-value">${escapeHtml(String(c.value))}</div>
      <div class="stat-label">${escapeHtml(c.label)}</div>
      ${c.sub ? `<div class="stat-sub">${escapeHtml(c.sub)}</div>` : ''}
    </div>`
  ).join("");

  html += `
    <div style="grid-column: 1 / -1; margin-top: 10px; background: var(--panel); border: 1.5px solid var(--line); padding: 18px; border-radius: 14px;">
      <h3 style="font-size: 16px; font-weight: 800; margin-bottom: 8px;">🎯 Suggested Focus & Weak Spots</h3>
      <p class="meta" style="margin-bottom: 12px;">Based on your recent activity and questions, here are topics to review:</p>
      <div style="display: flex; gap: 8px; flex-wrap: wrap;">
        <span class="course-chip" style="background: rgba(255,107,107,0.1); color: #e03131; border-color: rgba(255,107,107,0.3);">⚠️ Enzyme Kinetics & Inhibition</span>
        <span class="course-chip" style="background: rgba(255,107,107,0.1); color: #e03131; border-color: rgba(255,107,107,0.3);">⚠️ Oxidative Phosphorylation</span>
      </div>
    </div>
  `;

  statsBar.innerHTML = html;
  updateAlertBadge();
}

// ================= PAST PAPER SOLVER (STUDENT) =================
async function initStudentPastPapers() {
  const sel = el("ppCourseSelect");
  if (!sel) return;
  sel.innerHTML = '<option value="">Select course...</option>';

  try {
    const res = await fetch(`${API}/api/student/pastpaper/meta`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: state.token })
    });
    const data = await res.json();
    state.courseSyllabi = data.syllabi || {};
    
    (data.courses || []).forEach(c => {
      const opt = document.createElement("option");
      opt.value = c;
      opt.textContent = c;
      sel.appendChild(opt);
    });

    sel.addEventListener("change", () => {
      const chosen = sel.value;
      const badge = el("ppSyllabusBadge");
      if (badge) {
        badge.textContent = state.courseSyllabi[chosen] ? `🎯 Syllabus: ${state.courseSyllabi[chosen]}` : "No specific syllabus mapped yet";
      }
    });
  } catch (e) {
    console.error("Failed to load past paper metadata:", e);
  }
}

const solvePastPaperBtn = el("solvePastPaperBtn");
if (solvePastPaperBtn) {
  solvePastPaperBtn.addEventListener("click", async () => {
    const course = el("ppCourseSelect").value;
    if (!course) { alert("Please select a course."); return; }

    const year = el("ppYearInput").value;
    const series = el("ppSeriesSelect").value;
    const paper = el("ppPaperInput").value.trim();
    const question = el("ppQuestionInput").value.trim();
    const topicText = el("ppTopicInput").value.trim();

    if (!paper || !question) { alert("Please specify both Paper/Variant and Question Part."); return; }

    const origText = solvePastPaperBtn.innerText;
    solvePastPaperBtn.innerText = "⏳ Retrieving Mark Scheme & Notes...";
    solvePastPaperBtn.disabled = true;

    try {
      const res = await fetch(`${API}/api/student/pastpaper/solve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          token: state.token,
          course,
          year: parseInt(year),
          series,
          paper,
          question,
          question_text: topicText
        })
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Solver failed.");

      renderPastPaperSolution(data);
    } catch (err) {
      alert(err.message);
    } finally {
      solvePastPaperBtn.innerText = origText;
      solvePastPaperBtn.disabled = false;
    }
  });
}

function renderPastPaperSolution(data) {
  if (el("ppEmptyState")) el("ppEmptyState").classList.add("hidden");
  const resBox = el("ppSolutionResult");
  if (!resBox) return;
  resBox.classList.remove("hidden");
  resBox.innerHTML = "";

  // 1. Teacher Custom Override Asset Banner (if available)
  if (data.teacher_asset) {
    const asset = data.teacher_asset;
    const assetCard = document.createElement("div");
    assetCard.style.cssText = "background: linear-gradient(135deg, rgba(11,191,191,0.12), rgba(12,166,120,0.15)); border: 2px solid var(--brand); border-radius: 14px; padding: 16px;";
    
    let linksHtml = "";
    if (asset.video_url) {
      linksHtml += `<a href="${escapeHtml(asset.video_url)}" target="_blank" class="primary" style="display:inline-flex; align-items:center; gap:6px; padding:7px 14px; font-size:12.5px; text-decoration:none; margin-right:10px;">🎥 Watch Teacher Video Solution</a>`;
    }
    if (asset.note_filename) {
      linksHtml += `<span class="ghost-sm" style="padding:6px 12px; font-size:12.5px;">📄 Annotated Model Note: <strong>${escapeHtml(asset.note_filename)}</strong></span>`;
    }

    assetCard.innerHTML = `
      <div style="font-weight:900; font-size:15px; color:var(--brand-d); margin-bottom:6px;">👨‍🏫 Teacher's Prepared Solution</div>
      ${asset.teacher_tip ? `<p style="font-size:13.5px; font-weight:700; margin-bottom:12px; color:var(--text);">💡 <strong>Teacher Tip:</strong> ${escapeHtml(asset.teacher_tip)}</p>` : ''}
      <div style="margin-top:8px;">${linksHtml}</div>
    `;
    resBox.appendChild(assetCard);
  }

  // 2. AI Structured Breakdown
  const solutionCard = document.createElement("div");
  solutionCard.className = "q-block";
  solutionCard.style.padding = "20px";
  solutionCard.innerHTML = `
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:14px; border-bottom:1px solid var(--line); padding-bottom:8px;">
      <span class="q-num" style="font-size:13px;">${escapeHtml(data.exam_ref)}</span>
      <span class="meta" style="font-weight:800; color:var(--brand-d);">Syllabus: ${escapeHtml(data.syllabus)}</span>
    </div>
    <div style="line-height:1.6; font-size:14px;">${renderBotText(data.solution_markdown)}</div>
  `;
  resBox.appendChild(solutionCard);
}

// ================= ALERTS & NOTIFICATIONS PAGE =================
function renderAlertsPane() {
  const container = el("alertsContainer");
  if (!container) return;
  container.innerHTML = "";

  const now = new Date();
  const dueCards = (state.flashcardDeck || []).filter(c => new Date(c.dueDate) <= now);
  
  // Group due cards by recording
  const dueByRec = {};
  dueCards.forEach(c => {
    if (!dueByRec[c.recording_id]) dueByRec[c.recording_id] = [];
    dueByRec[c.recording_id].push(c);
  });

  let hasAlerts = false;

  // 1. Spaced Repetition Due Cards Section
  if (dueCards.length > 0) {
    hasAlerts = true;
    Object.keys(dueByRec).forEach(recId => {
      const rec = state.recordings.find(r => r.id === recId);
      const title = rec ? rec.title : "Class Recording";
      const count = dueByRec[recId].length;

      const card = document.createElement("div");
      card.className = "q-block";
      card.style.cssText = "display: flex; justify-content: space-between; align-items: center; padding: 18px; border-left: 4px solid #ff6b6b;";
      card.innerHTML = `
        <div>
          <div style="font-weight: 800; font-size: 15px; margin-bottom: 4px;">
            🔴 Flashcard Review Due: <span style="color: var(--text);">${escapeHtml(title)}</span>
          </div>
          <div class="meta">
            <strong>${count} card${count > 1 ? 's' : ''}</strong> are ready for active recall review according to your schedule.
          </div>
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
  if (currentStudyPlan) {
    let uncompletedCount = 0;
    currentStudyPlan.forEach(d => {
      d.tasks.forEach(t => { if (!t.completed) uncompletedCount++; });
    });

    if (uncompletedCount > 0) {
      hasAlerts = true;
      const planAlert = document.createElement("div");
      planAlert.className = "q-block";
      planAlert.style.cssText = "display: flex; justify-content: space-between; align-items: center; padding: 18px; border-left: 4px solid var(--brand);";
      planAlert.innerHTML = `
        <div>
          <div style="font-weight: 800; font-size: 15px; margin-bottom: 4px;">
            📅 Active Study Plan in Progress
          </div>
          <div class="meta">
            You have <strong>${uncompletedCount} task${uncompletedCount > 1 ? 's' : ''}</strong> left to complete your scheduled goal.
          </div>
        </div>
        <button class="ghost" style="padding: 8px 16px; font-size: 13px;">Open Planner →</button>
      `;
      planAlert.querySelector("button").addEventListener("click", () => switchStudentTab("Planner"));
      container.appendChild(planAlert);
    }
  }

  if (!hasAlerts) {
    container.innerHTML = `
      <div class="empty" style="padding: 40px 20px;">
        <div class="empty-emoji">🎉</div>
        <h3>You are completely caught up!</h3>
        <p class="meta">No spaced repetition flashcards or pending plan alerts right now. Keep up the momentum!</p>
      </div>
    `;
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
  
  // Render Persistent Chat History
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

const flashcardBtn = el("flashcardBtn");
const flashcardModal = el("flashcardModal");
const closeFlashcards = el("closeFlashcards");
const flashcardBody = el("flashcardBody");
const prevCardBtn = el("prevCardBtn");
const nextCardBtn = el("nextCardBtn");
const cardCountIndicator = el("cardCountIndicator");
const srsRatingControls = el("srsRatingControls");
const genFreshCardsBtn = el("genFreshCardsBtn");
const srsStatusText = el("srsStatusText");

function getDueFlashcards(recId) {
  const now = new Date();
  return state.flashcardDeck.filter(c => c.recording_id === recId && new Date(c.dueDate) <= now);
}

function getRecordingCards(recId) {
  return state.flashcardDeck.filter(c => c.recording_id === recId);
}

if (flashcardBtn) {
  flashcardBtn.addEventListener("click", async () => {
    if (!state.current) return;
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
  genFreshCardsBtn.addEventListener("click", () => generateNewFlashcards());
}

async function generateNewFlashcards() {
  if (!state.current) return;
  flashcardBody.innerHTML = '<div class="typing">Crafting fresh flashcards from class <span class="dot">●</span><span class="dot">●</span><span class="dot">●</span></div>';
  if (srsRatingControls) srsRatingControls.classList.add("hidden");
  
  const existingFronts = getRecordingCards(state.current.id).map(c => c.front);

  try {
    const res = await fetch(`${API}/api/flashcards`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ 
        recording_id: state.current.id, 
        existing_fronts: existingFronts,
        token: state.token 
      })
    });
    const data = await res.json();
    if (data.error || !data.flashcards || data.flashcards.length === 0) {
      flashcardBody.innerHTML = '<p>Could not generate new cards for this class.</p>';
      return;
    }

    const now = new Date().toISOString();
    const newCards = data.flashcards.map(fc => ({
      id: "fc_" + Math.random().toString(36).substring(2, 9),
      recording_id: state.current.id,
      front: fc.front,
      back: fc.back,
      interval: 1,
      reps: 0,
      dueDate: now
    }));

    state.flashcardDeck.push(...newCards);
    await saveServerProfile();

    currentDeckCards = getRecordingCards(state.current.id);
    currentCardIndex = currentDeckCards.length - newCards.length;
    updateSrsHeader();
    renderCurrentCard();
  } catch (e) {
    flashcardBody.innerHTML = '<p>Network error generating flashcards.</p>';
  }
}

function updateSrsHeader() {
  if (!state.current || !srsStatusText) return;
  const due = getDueFlashcards(state.current.id).length;
  const total = getRecordingCards(state.current.id).length;
  srsStatusText.textContent = `🎯 Due for Review: ${due} / ${total} cards`;
}

if (closeFlashcards) closeFlashcards.addEventListener("click", () => flashcardModal.classList.add("hidden"));

function renderCurrentCard() {
  if (!currentDeckCards.length) {
    flashcardBody.innerHTML = '<p class="meta">No flashcards in deck. Click "Generate New Cards" above!</p>';
    if (srsRatingControls) srsRatingControls.classList.add("hidden");
    return;
  }
  
  const card = currentDeckCards[currentCardIndex];
  cardCountIndicator.textContent = `${currentCardIndex + 1} / ${currentDeckCards.length}`;
  if (srsRatingControls) srsRatingControls.classList.add("hidden");

  let isFlipped = false;
  flashcardBody.innerHTML = `
    <div id="activeFlashcard" style="width: 100%; height: 200px; background: var(--panel2); border: 2px solid var(--line); border-radius: 16px; display: flex; align-items: center; justify-content: center; padding: 20px; cursor: pointer; text-align: center; box-shadow: var(--shadow-sm); transition: 0.2s;">
      <div style="font-size: 16px; font-weight: 700; color: var(--text);" id="cardTextContent">
        💡 <strong>Front (Question):</strong><br><br>${escapeHtml(card.front)}
        <div class="meta" style="font-size: 11px; margin-top: 10px; font-weight: 600;">(Click card to reveal answer & Spaced Repetition ratings)</div>
      </div>
    </div>
  `;
  
  const cardElem = el("activeFlashcard");
  const textElem = el("cardTextContent");
  
  cardElem.addEventListener("click", () => {
    isFlipped = !isFlipped;
    if (isFlipped) {
      cardElem.style.background = 'rgba(11,191,191,0.08)';
      cardElem.style.borderColor = 'var(--brand)';
      textElem.innerHTML = `✅ <strong>Back (Answer):</strong><br><br>${escapeHtml(card.back)}`;
      if (srsRatingControls) srsRatingControls.classList.remove("hidden");
    } else {
      cardElem.style.background = 'var(--panel2)';
      cardElem.style.borderColor = 'var(--line)';
      textElem.innerHTML = `💡 <strong>Front (Question):</strong><br><br>${escapeHtml(card.front)}`;
      if (srsRatingControls) srsRatingControls.classList.add("hidden");
    }
  });
}

async function rateCard(ratingFactor) {
  if (!currentDeckCards.length) return;
  const card = currentDeckCards[currentCardIndex];
  const original = state.flashcardDeck.find(c => c.id === card.id);
  if (!original) return;

  const now = new Date();
  if (ratingFactor === 'again') {
    original.interval = 1;
    original.reps = 0;
  } else if (ratingFactor === 'hard') {
    original.interval = Math.max(1, Math.round((original.interval || 1) * 1.2));
  } else if (ratingFactor === 'good') {
    original.interval = Math.max(2, Math.round((original.interval || 1) * 2.5));
    original.reps = (original.reps || 0) + 1;
  } else if (ratingFactor === 'easy') {
    original.interval = Math.max(4, Math.round((original.interval || 1) * 3.5));
    original.reps = (original.reps || 0) + 1;
  }

  const nextReview = new Date(now.getTime() + (original.interval * 24 * 60 * 60 * 1000));
  original.dueDate = nextReview.toISOString();

  await saveServerProfile();
  updateSrsHeader();

  if (currentCardIndex < currentDeckCards.length - 1) {
    currentCardIndex++;
  } else {
    currentCardIndex = 0;
  }
  renderCurrentCard();
}

if (el("srsAgainBtn")) el("srsAgainBtn").addEventListener("click", () => rateCard('again'));
if (el("srsHardBtn")) el("srsHardBtn").addEventListener("click", () => rateCard('hard'));
if (el("srsGoodBtn")) el("srsGoodBtn").addEventListener("click", () => rateCard('good'));
if (el("srsEasyBtn")) el("srsEasyBtn").addEventListener("click", () => rateCard('easy'));

if (prevCardBtn) prevCardBtn.addEventListener("click", () => { if (currentCardIndex > 0) { currentCardIndex--; renderCurrentCard(); } });
if (nextCardBtn) nextCardBtn.addEventListener("click", () => { if (currentCardIndex < currentDeckCards.length - 1) { currentCardIndex++; renderCurrentCard(); } });

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
    formEls.forEach(el => el.disabled = true);
    if(generatePlanBtn) generatePlanBtn.classList.add('hidden');
    if(resetPlanBtn) resetPlanBtn.classList.remove('hidden');
    if(planEmptyState) planEmptyState.classList.add('hidden');
    if(planResult) planResult.classList.remove('hidden');
    renderPlan();
  } else {
    formEls.forEach(el => el.disabled = false);
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
  if(el("tabRecordings")) el("tabRecordings").classList.toggle("active", name === "Recordings");
  if(el("tabStudents")) el("tabStudents").classList.toggle("active", name === "Students");
  if(el("tabPastPapers")) el("tabPastPapers").classList.toggle("active", name === "PastPapers");
  if(el("tabQuestions")) el("tabQuestions").classList.toggle("active", name === "Questions");
  if(el("tabAnalytics")) el("tabAnalytics").classList.toggle("active", name === "Analytics");
  if(el("tabSettings")) el("tabSettings").classList.toggle("active", name === "Settings");
  
  if(el("teacherRecordings")) el("teacherRecordings").classList.toggle("hidden", name !== "Recordings");
  if(el("teacherStudents")) el("teacherStudents").classList.toggle("hidden", name !== "Students");
  if(el("teacherPastPapers")) el("teacherPastPapers").classList.toggle("hidden", name !== "PastPapers");
  if(el("teacherQuestions")) el("teacherQuestions").classList.toggle("hidden", name !== "Questions");
  if(el("teacherAnalytics")) el("teacherAnalytics").classList.toggle("hidden", name !== "Analytics");
  if(el("teacherSettings")) el("teacherSettings").classList.toggle("hidden", name !== "Settings");
  
  if (name === "Questions") loadQuestions();
  if (name === "Recordings") { loadTeacherRecordings(); loadStats(); }
  if (name === "Students") loadStudents();
  if (name === "PastPapers") loadTeacherPastPaperHub();
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
  } catch (e) {
    console.error("Error loading stats:", e);
  }
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
  } catch (e) { 
    box.innerHTML = '<div class="q-empty">Network error loading analytics.</div>'; 
  }
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
    
    try {
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
    } catch(e) {
      if(err) { err.textContent = "Network error. Try again."; err.classList.remove("hidden"); }
    }
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

let teacherRecordings = [];

async function loadTeacherRecordings() {
  try {
    const res = await fetch(`${API}/api/teacher/recordings`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ passcode: state.passcode })
    });
    const data = await res.json();
    teacherRecordings = (data.recordings || []).map((r, i) => ({ ...r, _order: i }));
    populateCourseFilter(teacherRecordings);
    applyRecFilters();
  } catch (e) {
    console.error("Failed to load recordings", e);
  }
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
  
  if (!list.length) { 
    box.innerHTML = '<div class="roster-empty">No recordings match your filter.</div>'; 
    return; 
  }
  
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

if(el("importRecBtn")) {
  el("importRecBtn").addEventListener("click", async () => {
    el("importRecBtn").disabled = true;
    el("importRecStatus").textContent = "Checking Zoom cloud recordings…";
    try {
      const res = await fetch(`${API}/api/teacher/backfill`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ passcode: state.passcode })
      });
      const data = await res.json();
      if (res.ok) {
        toast(`Imported ${data.added} new recordings.`, "success");
        loadTeacherRecordings();
        loadStats();
      } else {
        toast(data.error || "Import failed.", "error");
      }
    } catch (e) { toast("Import failed.", "error"); }
    finally { el("importRecBtn").disabled = false; el("importRecStatus").textContent = ""; }
  });
}

if(el("deleteUnassignedBtn")) {
  el("deleteUnassignedBtn").addEventListener("click", async () => {
    if (!confirm("Delete all unassigned recordings?")) return;
    try {
      const res = await fetch(`${API}/api/teacher/recordings/delete-unassigned`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ passcode: state.passcode })
      });
      if (res.ok) { toast("Unassigned recordings removed.", "success"); loadTeacherRecordings(); loadStats(); }
    } catch (e) { toast("Failed.", "error"); }
  });
}

if(el("importOneBtn")) {
  el("importOneBtn").addEventListener("click", async () => {
    const val = el("importOneInput").value.trim();
    if (!val) return;
    el("importOneBtn").disabled = true;
    el("importOneStatus").textContent = "Importing…";
    try {
      const res = await fetch(`${API}/api/teacher/import-one`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ passcode: state.passcode, ref: val })
      });
      const data = await res.json();
      if (res.ok) {
        toast("Recording imported ✓", "success");
        el("importOneInput").value = "";
        loadTeacherRecordings();
        loadStats();
      } else { toast(data.error || "Failed", "error"); }
    } catch (e) { toast("Network error.", "error"); }
    finally { el("importOneBtn").disabled = false; el("importOneStatus").textContent = ""; }
  });
}

// ================= TEACHER PAST PAPERS HUB =================
async function loadTeacherPastPaperHub() {
  const courseSel = el("tppCourseSelect");
  const tqCourseSel = el("tqCourseSelect");
  const noteLibSel = el("tqNoteLibSelect");

  try {
    const res = await fetch(`${API}/api/teacher/pastpaper/config`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ passcode: state.passcode })
    });
    const data = await res.json();

    if (courseSel) {
      courseSel.innerHTML = '<option value="">Select course...</option>';
      (data.courses || []).forEach(c => {
        const opt = document.createElement("option");
        opt.value = c; opt.textContent = c;
        courseSel.appendChild(opt);
      });
    }

    if (tqCourseSel) {
      tqCourseSel.innerHTML = '<option value="">Select course...</option>';
      (data.courses || []).forEach(c => {
        const opt = document.createElement("option");
        opt.value = c; opt.textContent = c;
        tqCourseSel.appendChild(opt);
      });
    }

    if (noteLibSel) {
      noteLibSel.innerHTML = '<option value="">None</option>';
      (data.notes_library || []).forEach(n => {
        const opt = document.createElement("option");
        opt.value = n.id;
        opt.textContent = n.filename;
        noteLibSel.appendChild(opt);
      });
    }

    renderTeacherOverrides(data.solutions || []);
  } catch (e) {
    console.error("Error loading Past Paper Hub:", e);
  }
}

if(el("saveSyllabusBtn")) {
  el("saveSyllabusBtn").addEventListener("click", async () => {
    const course = el("tppCourseSelect").value;
    const syllabus = el("tppSyllabusCode").value;
    if (!course) {
      toast("Please select a course first.", "info");
      return;
    }

    try {
      const res = await fetch(`${API}/api/teacher/pastpaper/config/save`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ passcode: state.passcode, course, syllabus })
      });
      if (res.ok) toast("Course syllabus mapping saved ✓", "success");
    } catch (e) { toast("Error saving syllabus.", "error"); }
  });
}

if(el("saveQuestionAssetBtn")) {
  el("saveQuestionAssetBtn").addEventListener("click", async () => {
    const course = el("tqCourseSelect").value;
    const year = el("tqYearInput").value;
    const series = el("tqSeriesSelect").value;
    const paper = el("tqPaperInput").value.trim();
    const question = el("tqQuestionInput").value.trim();
    const videoUrl = el("tqVideoUrl").value.trim();
    const noteId = el("tqNoteLibSelect").value;
    const tip = el("tqTeacherTip").value.trim();

    if (!course) { toast("Please select a course.", "info"); return; }
    if (!paper || !question) { toast("Enter paper and question.", "info"); return; }

    try {
      const res = await fetch(`${API}/api/teacher/pastpaper/solutions/save`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          passcode: state.passcode,
          course, year: parseInt(year), series, paper, question,
          video_url: videoUrl, note_id: noteId, teacher_tip: tip
        })
      });
      const data = await res.json();
      if (res.ok) {
        toast("Question solution saved ✓", "success");
        loadTeacherPastPaperHub();
      }
    } catch (e) { toast("Failed to save.", "error"); }
  });
}

function renderTeacherOverrides(list) {
  const container = el("tppOverridesList");
  if (!container) return;
  container.innerHTML = "";

  if (!list.length) {
    container.innerHTML = '<p class="meta">No question solutions attached yet.</p>';
    return;
  }

  list.forEach(item => {
    const card = document.createElement("div");
    card.className = "student-row";
    card.innerHTML = `
      <div>
        <div style="font-weight:800; font-size:14px; color:var(--brand-d);">${escapeHtml(item.course)} · ${item.year} ${escapeHtml(item.series)} P${escapeHtml(item.paper)} Q${escapeHtml(item.question)}</div>
        <div class="meta">${item.video_url ? `🎥 Video: ${escapeHtml(item.video_url)}` : 'No video attached'} | ${item.teacher_tip ? `💡 Tip: ${escapeHtml(item.teacher_tip)}` : 'No tip'}</div>
      </div>
      <button class="ghost-sm danger-btn">🗑️ Remove</button>
    `;
    card.querySelector("button").addEventListener("click", async () => {
      if (!confirm("Delete this question asset?")) return;
      await fetch(`${API}/api/teacher/pastpaper/solutions/delete`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ passcode: state.passcode, key: item.key })
      });
      loadTeacherPastPaperHub();
    });
    container.appendChild(card);
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
loadBranding();
