import json
import os
import re
import math
from collections import Counter

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import secrets

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DATA_PATH = os.path.join(DATA_DIR, "recordings.json")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
QLOG_PATH = os.path.join(DATA_DIR, "question_log.json")
ROSTER_PATH = os.path.join(DATA_DIR, "roster.json")

# in-memory active student sessions: token -> {student_id, name}
SESSIONS = {}


def load_roster():
    if os.path.exists(ROSTER_PATH):
        with open(ROSTER_PATH) as f:
            return json.load(f)
    return []


def save_roster(roster):
    with open(ROSTER_PATH, "w") as f:
        json.dump(roster, f, ensure_ascii=False, indent=2)


def gen_pin():
    return f"{secrets.randbelow(10000):04d}"

# default teacher passcode; teacher can change it in the dashboard
DEFAULT_PASSCODE = "teach123"


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    cfg = {"passcode": DEFAULT_PASSCODE}
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def load_qlog():
    if os.path.exists(QLOG_PATH):
        with open(QLOG_PATH) as f:
            return json.load(f)
    return []


def save_qlog(log):
    with open(QLOG_PATH, "w") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


def save_recordings(recs):
    with open(DATA_PATH, "w") as f:
        json.dump(recs, f, ensure_ascii=False, indent=2)


app = FastAPI(title="ClassMate API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def load_recordings():
    with open(DATA_PATH) as f:
        return json.load(f)


RECORDINGS = load_recordings()
REC_BY_ID = {r["id"]: r for r in RECORDINGS}


def fmt_ts(t):
    """Format a transcript start_time (which may be 'HH:MM:SS' or seconds) as mm:ss / h:mm:ss."""
    if t is None:
        return "?"
    s = str(t)
    # Already looks like a time string
    if ":" in s:
        # normalize possible microseconds
        return s.split(".")[0]
    try:
        sec = float(s)
    except ValueError:
        return s
    sec = int(sec)
    h = sec // 3600
    m = (sec % 3600) // 60
    ss = sec % 60
    if h:
        return f"{h}:{m:02d}:{ss:02d}"
    return f"{m}:{ss:02d}"


# ---------- lightweight retrieval (BM25-ish tf-idf over segments) ----------
_word_re = re.compile(r"[A-Za-z0-9\u00c0-\u024f\u0400-\u04ff\u0600-\u06ff]+")


def tokenize(text):
    return [w.lower() for w in _word_re.findall(text or "")]


def build_index(rec):
    segs = rec["segments"]
    docs = [tokenize(s.get("text", "")) for s in segs]
    df = Counter()
    for d in docs:
        for w in set(d):
            df[w] += 1
    N = len(docs) or 1
    idf = {w: math.log(1 + N / (c)) for w, c in df.items()}
    return docs, idf


_INDEX_CACHE = {}


def get_index(rec):
    rid = rec["id"]
    if rid not in _INDEX_CACHE:
        _INDEX_CACHE[rid] = build_index(rec)
    return _INDEX_CACHE[rid]


def retrieve(rec, query, k=18, window=1):
    segs = rec["segments"]
    docs, idf = get_index(rec)
    q = tokenize(query)
    qset = Counter(q)
    scores = []
    for i, d in enumerate(docs):
        if not d:
            scores.append(0.0)
            continue
        tf = Counter(d)
        score = 0.0
        for w, qc in qset.items():
            if w in tf:
                score += idf.get(w, 0.0) * (tf[w] / len(d)) * qc
        scores.append(score)
    ranked = sorted(range(len(segs)), key=lambda i: scores[i], reverse=True)
    top = [i for i in ranked if scores[i] > 0][:k]
    if not top:
        # fall back to evenly sampled segments so quiz/summary still works
        step = max(1, len(segs) // 40)
        top = list(range(0, len(segs), step))[:40]
    # expand with neighbor windows for context, keep order
    chosen = set()
    for i in top:
        for j in range(max(0, i - window), min(len(segs), i + window + 1)):
            chosen.add(j)
    return sorted(chosen)


def context_from_indices(rec, indices, max_chars=45000):
    segs = rec["segments"]
    lines = []
    total = 0
    for i in indices:
        s = segs[i]
        ts = fmt_ts(s.get("start"))
        spk = s.get("speaker") or ""
        prefix = f"[{ts}]" + (f" {spk}:" if spk else "")
        line = f"{prefix} {s.get('text','').strip()}"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)


# ---------- LLM helper ----------
# Two modes:
#  * Deployed (cloud host): set env OPENAI_API_KEY (and optionally OPENAI_MODEL,
#    OPENAI_BASE_URL). Uses any OpenAI-compatible Chat Completions API.
#  * Sandbox/preview: if no API key is present, falls back to the built-in call_llm.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").strip()


async def llm(messages, max_tokens=1200, temperature=0.1):
    if OPENAI_API_KEY:
        # Production path: OpenAI-compatible API
        import httpx
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                f"{OPENAI_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                json={
                    "model": OPENAI_MODEL,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
    # Sandbox/preview path: use the injected built-in
    return await call_llm(  # noqa: F821  (injected sandbox built-in)
        messages=messages,
        model="claude-haiku-4-5-20251001",
        temperature=temperature,
        max_completion_tokens=max_tokens,
    )


# ---------- API ----------
def _card(r, include_hidden=False):
    return {
        "id": r["id"],
        "title": r.get("display_title") or r.get("topic"),
        "original_title": r.get("original_topic") or r.get("topic"),
        "date": r.get("date"),
        "unit": r.get("unit") or "Unassigned",
        "visible": r.get("visible", True),
        "segments": len(r.get("segments", [])),
        "has_summary": bool(r.get("summary")),
    }


@app.get("/api/recordings")
def list_recordings():
    """Student-facing: only visible recordings, grouped info included."""
    out = [_card(r) for r in RECORDINGS if r.get("visible", True)]
    units = []
    seen = set()
    for r in RECORDINGS:
        if not r.get("visible", True):
            continue
        u = r.get("unit") or "Unassigned"
        if u not in seen:
            seen.add(u)
            units.append(u)
    return {"recordings": out, "units": units}


# ---------- auth ----------
class LoginBody(BaseModel):
    passcode: str


def check_passcode(passcode: str) -> bool:
    return passcode == load_config().get("passcode")


@app.post("/api/teacher/login")
def teacher_login(body: LoginBody):
    if check_passcode(body.passcode):
        return {"ok": True}
    return JSONResponse({"ok": False, "error": "Wrong passcode"}, status_code=401)


# ---------- student roster auth ----------
def _norm(s):
    return (s or "").strip().lower()


class StudentLoginBody(BaseModel):
    name: str
    pin: str


@app.post("/api/student/login")
def student_login(body: StudentLoginBody):
    roster = load_roster()
    for st in roster:
        if _norm(st["name"]) == _norm(body.name) and st["pin"] == body.pin.strip():
            token = secrets.token_urlsafe(24)
            SESSIONS[token] = {"student_id": st["id"], "name": st["name"]}
            return {"ok": True, "token": token, "name": st["name"]}
    return JSONResponse(
        {"ok": False, "error": "That name and PIN don't match our class roster. Check with your teacher."},
        status_code=401,
    )


def valid_session(token: str):
    return SESSIONS.get(token or "")


# ---------- teacher roster management ----------
class RosterAuth(BaseModel):
    passcode: str


@app.post("/api/teacher/students")
def list_students(body: RosterAuth):
    if not check_passcode(body.passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {"students": load_roster()}


class AddStudentBody(BaseModel):
    passcode: str
    name: str
    email: str | None = ""


@app.post("/api/teacher/students/add")
def add_student(body: AddStudentBody):
    if not check_passcode(body.passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    name = body.name.strip()
    if not name:
        return JSONResponse({"error": "Name required"}, status_code=400)
    roster = load_roster()
    if any(_norm(s["name"]) == _norm(name) for s in roster):
        return JSONResponse({"error": "A student with that name already exists"}, status_code=400)
    student = {
        "id": secrets.token_hex(6),
        "name": name,
        "email": (body.email or "").strip(),
        "pin": gen_pin(),
    }
    roster.append(student)
    save_roster(roster)
    return {"ok": True, "student": student}


class RemoveStudentBody(BaseModel):
    passcode: str
    id: str


@app.post("/api/teacher/students/remove")
def remove_student(body: RemoveStudentBody):
    if not check_passcode(body.passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    roster = [s for s in load_roster() if s["id"] != body.id]
    save_roster(roster)
    # invalidate any active sessions for that student
    for tok in [t for t, v in SESSIONS.items() if v["student_id"] == body.id]:
        SESSIONS.pop(tok, None)
    return {"ok": True}


class ResetPinBody(BaseModel):
    passcode: str
    id: str


@app.post("/api/teacher/students/reset_pin")
def reset_pin(body: ResetPinBody):
    if not check_passcode(body.passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    roster = load_roster()
    for s in roster:
        if s["id"] == body.id:
            s["pin"] = gen_pin()
            save_roster(roster)
            return {"ok": True, "pin": s["pin"]}
    return JSONResponse({"error": "not found"}, status_code=404)


# ---------- teacher endpoints ----------
class TeacherAuth(BaseModel):
    passcode: str


@app.post("/api/teacher/recordings")
def teacher_recordings(body: TeacherAuth):
    if not check_passcode(body.passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    out = [_card(r, include_hidden=True) for r in RECORDINGS]
    units = sorted({(r.get("unit") or "Unassigned") for r in RECORDINGS})
    return {"recordings": out, "units": units}


class UpdateRecBody(BaseModel):
    passcode: str
    id: str
    display_title: str | None = None
    visible: bool | None = None
    unit: str | None = None


@app.post("/api/teacher/update")
def teacher_update(body: UpdateRecBody):
    if not check_passcode(body.passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    rec = REC_BY_ID.get(body.id)
    if not rec:
        return JSONResponse({"error": "not found"}, status_code=404)
    if body.display_title is not None and body.display_title.strip():
        rec["display_title"] = body.display_title.strip()
    if body.visible is not None:
        rec["visible"] = body.visible
    if body.unit is not None and body.unit.strip():
        rec["unit"] = body.unit.strip()
    save_recordings(RECORDINGS)
    return {"ok": True, "recording": _card(rec, include_hidden=True)}


class PasscodeBody(BaseModel):
    passcode: str
    new_passcode: str


@app.post("/api/teacher/passcode")
def change_passcode(body: PasscodeBody):
    if not check_passcode(body.passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not body.new_passcode.strip():
        return JSONResponse({"error": "empty passcode"}, status_code=400)
    cfg = load_config()
    cfg["passcode"] = body.new_passcode.strip()
    save_config(cfg)
    return {"ok": True}


@app.post("/api/teacher/questions")
def teacher_questions(body: TeacherAuth):
    if not check_passcode(body.passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    log = load_qlog()
    # newest first
    return {"questions": list(reversed(log))[:500]}


class AskBody(BaseModel):
    recording_id: str
    question: str
    language: str | None = None
    token: str | None = None


@app.post("/api/ask")
async def ask(body: AskBody):
    sess = valid_session(body.token)
    if not sess:
        return JSONResponse({"error": "Your session has expired. Please log in again."}, status_code=401)
    rec = REC_BY_ID.get(body.recording_id)
    if not rec:
        return JSONResponse({"error": "Recording not found"}, status_code=404)
    idx = retrieve(rec, body.question)
    ctx = context_from_indices(rec, idx)
    lang_line = ""
    if body.language and body.language.lower() not in ("auto", "same as question"):
        lang_line = f"\nRespond in this language: {body.language}."
    else:
        lang_line = "\nRespond in the same language the student's question is written in."

    system = (
        "You are ClassMate, a study assistant for students. You answer ONLY using the "
        "provided class recording transcript excerpts. Each excerpt is prefixed with a "
        "timestamp like [12:34] and sometimes a speaker name.\n"
        "RULES:\n"
        "1. Base every claim strictly on the transcript. Do NOT use outside knowledge.\n"
        "2. When you state something from the recording, cite the timestamp in parentheses, e.g. (at 12:34).\n"
        "3. If the answer is not covered in the excerpts, say clearly that it wasn't covered in this recording, and do not invent an answer.\n"
        "4. Be clear, friendly and concise, like a helpful tutor."
        + lang_line
    )
    user = (
        f"Class recording: {rec.get('display_title') or rec.get('topic')}\n\n"
        f"Transcript excerpts:\n{ctx}\n\n"
        f"Student question: {body.question}"
    )
    answer = await llm(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=1000,
    )
    # log the student question for the teacher
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        log = load_qlog()
        log.append({
            "student": sess["name"],
            "recording_id": rec["id"],
            "recording_title": rec.get("display_title") or rec.get("topic"),
            "unit": rec.get("unit") or "Unassigned",
            "question": body.question,
            "time": datetime.now(ZoneInfo("Africa/Cairo")).strftime("%Y-%m-%d %H:%M"),
        })
        save_qlog(log[-1000:])
    except Exception:
        pass
    return {"answer": answer, "cited_segments": len(idx)}


class QuizBody(BaseModel):
    recording_id: str
    num_questions: int = 5
    language: str | None = None
    difficulty: str | None = "mixed"
    token: str | None = None


@app.post("/api/quiz")
async def quiz(body: QuizBody):
    if not valid_session(body.token):
        return JSONResponse({"error": "Your session has expired. Please log in again."}, status_code=401)
    rec = REC_BY_ID.get(body.recording_id)
    if not rec:
        return JSONResponse({"error": "Recording not found"}, status_code=404)
    # sample broadly across the recording for a representative quiz
    segs = rec["segments"]
    step = max(1, len(segs) // 60)
    idx = list(range(0, len(segs), step))
    ctx = context_from_indices(rec, idx, max_chars=60000)
    lang_line = f"Write the quiz in this language: {body.language}." if body.language and body.language.lower() not in ("auto", "same as question") else "Write the quiz in English."
    n = max(1, min(10, body.num_questions))
    system = (
        "You are ClassMate, creating a quiz to help students review a class recording. "
        "Use ONLY the transcript content provided. Return STRICT JSON only, no markdown, no prose. "
        "Schema: {\"questions\":[{\"question\":str,\"options\":[str,str,str,str],"
        "\"answer_index\":int,\"explanation\":str,\"timestamp\":str}]}. "
        "The 'timestamp' is the transcript timestamp (like '12:34') where the topic is discussed. "
        "The 'explanation' must reference what was said in the recording. "
        f"Create exactly {n} multiple-choice questions ({body.difficulty} difficulty). {lang_line}"
    )
    user = (
        f"Class recording: {rec.get('display_title') or rec.get('topic')}\n\n"
        f"Transcript excerpts:\n{ctx}"
    )
    raw = await llm(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=2500,
    )
    # extract JSON
    data = None
    try:
        data = json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = None
    if not data or "questions" not in data:
        return JSONResponse({"error": "Could not generate quiz", "raw": raw[:500]}, status_code=500)
    return data


@app.get("/api/health")
def health():
    return {"status": "ok", "recordings": len(RECORDINGS)}


# ---------- serve frontend ----------
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(FRONTEND_DIR):
    @app.get("/")
    def index():
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))

    app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="static")
