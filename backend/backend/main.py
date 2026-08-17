import json
import os
import re
import math
import io
import base64
import time
import hashlib
import hmac
from collections import Counter
from fastapi import FastAPI, UploadFile, File, Form, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import secrets
import bcrypt
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")
DATA_PATH = os.path.join(DATA_DIR, "recordings.json")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
QLOG_PATH = os.path.join(DATA_DIR, "question_log.json")
ROSTER_PATH = os.path.join(DATA_DIR, "roster.json")
# in-memory active student sessions: token -> {student_id, name, courses}
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
def hash_pw(pw: str) -> str:
    # bcrypt only accepts up to 72 bytes; truncate defensively.
    pw_bytes = (pw or "").encode("utf-8")[:72]
    return bcrypt.hashpw(pw_bytes, bcrypt.gensalt()).decode("utf-8")
def verify_pw(pw: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        pw_bytes = (pw or "").encode("utf-8")[:72]
        return bcrypt.checkpw(pw_bytes, hashed.encode("utf-8"))
    except Exception:
        return False
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

# ---------------------------------------------------------------------------
# OpenAI key diagnostic endpoint (safe: never returns the key itself).
# Open  /api/diag/openai  to confirm the key is set & working.
# Remove these two lines (and diag_openai.py) once you're done debugging.
# ---------------------------------------------------------------------------
try:
    from diag_openai import register_openai_diag
    register_openai_diag(app)   # or: register_openai_diag(app, passcode="teach123")
except Exception as _diag_err:  # never let the diagnostic break the app
    print("openai diag not registered:", _diag_err)

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
#  * Sandbox/preview: if no API key is present AND a sandbox call_llm builtin
#    exists, falls back to it. On a real server with neither, it raises a clear
#    LLMConfigError instead of crashing with NameError.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").strip()


class LLMConfigError(Exception):
    """Raised when the LLM backend is not configured (no key, no fallback)."""


class LLMUpstreamError(Exception):
    """Raised when the OpenAI-compatible API returns an error."""


async def llm(messages, max_tokens=1200, temperature=0.1):
    # ---- Production path: OpenAI-compatible API -----------------------------
    if OPENAI_API_KEY:
        import httpx
        # connect fast, allow the model time to think
        timeout = httpx.Timeout(90.0, connect=10.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
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
        except httpx.RequestError as e:
            raise LLMUpstreamError(f"Could not reach the AI provider: {e}") from e

        if resp.status_code >= 400:
            detail = ""
            try:
                detail = resp.json().get("error", {}).get("message", "")
            except Exception:
                detail = resp.text[:300]
            raise LLMUpstreamError(
                f"AI provider returned {resp.status_code}: {detail or 'unknown error'}"
            )

        try:
            return resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            raise LLMUpstreamError(f"Unexpected AI response shape: {e}") from e

    # ---- Sandbox/preview path: ONLY if the builtin truly exists -------------
    _fallback = globals().get("call_llm", None)
    if _fallback is None:
        import builtins
        _fallback = getattr(builtins, "call_llm", None)
    if _fallback is not None:
        return await _fallback(
            messages=messages,
            model="claude-haiku-4-5-20251001",
            temperature=temperature,
            max_completion_tokens=max_tokens,
        )

    # ---- Neither key nor fallback -> clear, actionable error ----------------
    raise LLMConfigError(
        "The AI features are not configured on this server. "
        "Set the OPENAI_API_KEY environment variable (and redeploy)."
    )
# ---------- Zoom integration (server-to-server OAuth + webhook) ----------
ZOOM_ACCOUNT_ID     = os.environ.get("ZOOM_ACCOUNT_ID", "").strip()
ZOOM_CLIENT_ID      = os.environ.get("ZOOM_CLIENT_ID", "").strip()
ZOOM_CLIENT_SECRET  = os.environ.get("ZOOM_CLIENT_SECRET", "").strip()
ZOOM_WEBHOOK_SECRET = os.environ.get("ZOOM_WEBHOOK_SECRET", "").strip()
_zoom_tok = {"token": None, "exp": 0}
async def zoom_token():
    if _zoom_tok["token"] and _zoom_tok["exp"] > time.time():
        return _zoom_tok["token"]
    creds = base64.b64encode(f"{ZOOM_CLIENT_ID}:{ZOOM_CLIENT_SECRET}".encode()).decode()
    import httpx
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://zoom.us/oauth/token",
            headers={"Authorization": f"Basic {creds}"},
            params={"grant_type": "account_credentials", "account_id": ZOOM_ACCOUNT_ID},
        )
        r.raise_for_status()
        d = r.json()
    _zoom_tok["token"] = d["access_token"]
    _zoom_tok["exp"] = time.time() + d.get("expires_in", 3600) - 60
    return _zoom_tok["token"]
def parse_vtt(text):
    """Turn a WEBVTT transcript into [{start, speaker, text}, ...]."""
    segments = []
    blocks = re.split(r"\n\s*\n", text.strip())
    for b in blocks:
        lines = [l for l in b.splitlines() if l.strip()]
        if not lines:
            continue
        # find the timing line "00:00:01.000 --> 00:00:04.000"
        tline_i = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if tline_i is None:
            continue
        start = lines[tline_i].split("-->")[0].strip().split(".")[0]  # HH:MM:SS
        body = " ".join(lines[tline_i + 1:]).strip()
        speaker = ""
        m = re.match(r"^([^:]{1,40}):\s*(.*)$", body)
        if m:
            speaker, body = m.group(1).strip(), m.group(2).strip()
        if body:
            segments.append({"start": start, "speaker": speaker, "text": body})
    return segments
def _detect_source(obj):
    """Return 'webinar' or 'meeting' based on the Zoom recording object.
    Zoom webinar meeting_type values are 5, 6 and 9; regular meetings are 1/2/3/4/8.
    We also treat an explicit 'webinar_*' or a 'type' string containing 'webinar' as a webinar.
    """
    t = obj.get("type")
    try:
        if int(t) in (5, 6, 9):
            return "webinar"
    except (TypeError, ValueError):
        if isinstance(t, str) and "webinar" in t.lower():
            return "webinar"
    return "meeting"
async def ingest_zoom_meeting(obj):
    """Given a webhook payload's 'object', download its transcript and add a hidden recording."""
    meeting_id = str(obj.get("id") or obj.get("uuid") or secrets.token_hex(6))
    if meeting_id in REC_BY_ID:
        return False
    topic = obj.get("topic", "Untitled class")
    start_time = (obj.get("start_time") or "")[:10]
    source = _detect_source(obj)
    files = obj.get("recording_files", [])
    transcript = next((f for f in files if f.get("file_type") == "TRANSCRIPT"), None)
    segments = []
    if transcript:
        token = await zoom_token()
        url = transcript.get("download_url")
        import httpx
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            # Zoom accepts the OAuth token as a bearer header for downloads
            r = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            if r.status_code == 200:
                segments = parse_vtt(r.text)
    new_rec = {
        "id": meeting_id,
        "topic": topic,
        "original_topic": topic,
        "display_title": topic,
        "date": start_time,
        "source": source,     # "meeting" or "webinar"
        "unit": "",            # teacher assigns later
        "visible": False,      # hidden until teacher reviews
        "segments": segments,
    }
    RECORDINGS.append(new_rec)
    REC_BY_ID[meeting_id] = new_rec
    save_recordings(RECORDINGS)
    return True
# ---------- API ----------
def _card(r, include_hidden=False):
    return {
        "id": r["id"],
        "title": r.get("display_title") or r.get("topic"),
        "original_title": r.get("original_topic") or r.get("topic"),
        "date": r.get("date"),
        "source": r.get("source") or "meeting",
        "unit": r.get("unit") or "Unassigned",
        "visible": r.get("visible", True),
        "segments": len(r.get("segments", [])),
        "has_summary": bool(r.get("summary")),
    }
class RecListBody(BaseModel):
    token: str | None = None
@app.post("/api/recordings")
def list_recordings(body: RecListBody):
    """Student-facing: only visible recordings the student's courses allow."""
    sess = valid_session(body.token)
    if not sess:
        return JSONResponse({"error": "Your session has expired. Please log in again."}, status_code=401)
    my_courses = sess.get("courses", [])
    def allowed(r):
        if not r.get("visible", True):
            return False
        if not my_courses:          # no courses assigned -> see nothing
            return False
        return (r.get("unit") or "Unassigned") in my_courses
    out = [_card(r) for r in RECORDINGS if allowed(r)]
    units = []
    seen = set()
    for r in RECORDINGS:
        if not allowed(r):
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
    email: str
    password: str
@app.post("/api/student/login")
def student_login(body: StudentLoginBody):
    roster = load_roster()
    for st in roster:
        if _norm(st.get("email")) == _norm(body.email) and verify_pw(body.password, st.get("password_hash")):
            token = secrets.token_urlsafe(24)
            SESSIONS[token] = {
                "student_id": st["id"],
                "name": st.get("name") or st.get("email"),
                "courses": st.get("courses", []),
            }
            return {"ok": True, "token": token, "name": SESSIONS[token]["name"]}
    return JSONResponse(
        {"ok": False, "error": "That email and password don't match our class roster. Check with your teacher."},
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
    safe = [{
        "id": s["id"],
        "name": s.get("name", ""),
        "email": s.get("email", ""),
        "courses": s.get("courses", []),
        "has_password": bool(s.get("password_hash")),
    } for s in load_roster()]
    return {"students": safe}
class AddStudentBody(BaseModel):
    passcode: str
    name: str
    email: str | None = ""
    password: str | None = ""
    courses: str | None = ""
@app.post("/api/teacher/students/add")
def add_student(body: AddStudentBody):
    if not check_passcode(body.passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    name = body.name.strip()
    email = (body.email or "").strip()
    if not email:
        return JSONResponse({"error": "Email required"}, status_code=400)
    roster = load_roster()
    if any(_norm(s.get("email")) == _norm(email) for s in roster):
        return JSONResponse({"error": "A student with that email already exists"}, status_code=400)
    courses = [c.strip() for c in (body.courses or "").split(";") if c.strip()]
    student = {
        "id": secrets.token_hex(6),
        "name": name or email.split("@")[0],
        "email": email,
        "courses": courses,
        "password_hash": hash_pw(body.password) if (body.password or "").strip() else "",
    }
    roster.append(student)
    save_roster(roster)
    return {"ok": True, "student": {
        "id": student["id"], "name": student["name"], "email": student["email"],
        "courses": student["courses"], "has_password": bool(student["password_hash"]),
    }}
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
@app.post("/api/teacher/students/import")
async def import_students(passcode: str = Form(...), file: UploadFile = File(...)):
    if not check_passcode(passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        import openpyxl
        content = await file.read()
        wb = openpyxl.load_workbook(io.BytesIO(content))
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
    except Exception as e:
        return JSONResponse({"error": f"Could not read the Excel file: {e}"}, status_code=400)
    if not rows:
        return JSONResponse({"error": "The sheet is empty."}, status_code=400)
    header = [str(h).strip().lower() if h is not None else "" for h in rows[0]]
    def col(name):
        return header.index(name) if name in header else -1
    ei, pi, ni, ci = col("email"), col("password"), col("name"), col("courses")
    if ei < 0 or pi < 0:
        return JSONResponse({"error": "The sheet must have 'email' and 'password' columns."}, status_code=400)
    roster = load_roster()
    by_email = {_norm(s.get("email")): s for s in roster if s.get("email")}
    added = updated = 0
    for row in rows[1:]:
        if not row or ei >= len(row) or not row[ei]:
            continue
        email = str(row[ei]).strip()
        pw = str(row[pi]).strip() if pi < len(row) and row[pi] else ""
        name = str(row[ni]).strip() if ni >= 0 and ni < len(row) and row[ni] else email.split("@")[0]
        courses = []
        if ci >= 0 and ci < len(row) and row[ci]:
            courses = [c.strip() for c in str(row[ci]).split(";") if c.strip()]
        key = _norm(email)
        if key in by_email:
            s = by_email[key]
            s["name"] = name
            s["courses"] = courses
            if pw:
                s["password_hash"] = hash_pw(pw)
            updated += 1
        else:
            roster.append({
                "id": secrets.token_hex(6),
                "name": name,
                "email": email,
                "courses": courses,
                "password_hash": hash_pw(pw) if pw else "",
            })
            added += 1
    save_roster(roster)
    return {"ok": True, "added": added, "updated": updated, "total": len(roster)}
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
ALLOWED_LOGO_EXT = {"png": "png", "jpg": "jpg", "jpeg": "jpg", "webp": "webp", "gif": "gif", "svg": "svg"}
LOGO_MAX_BYTES = 2 * 1024 * 1024  # 2 MB
@app.post("/api/teacher/logo")
async def upload_logo(passcode: str = Form(...), file: UploadFile = File(...)):
    if not check_passcode(passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    ext = (file.filename or "").rsplit(".", 1)[-1].lower() if "." in (file.filename or "") else ""
    if ext not in ALLOWED_LOGO_EXT:
        return JSONResponse({"error": "Please upload a PNG, JPG, WEBP, GIF or SVG image."}, status_code=400)
    data = await file.read()
    if len(data) > LOGO_MAX_BYTES:
        return JSONResponse({"error": "Image is too large (max 2 MB)."}, status_code=400)
    if not os.path.isdir(FRONTEND_DIR):
        return JSONResponse({"error": "Frontend directory not found on server."}, status_code=500)
    save_ext = ALLOWED_LOGO_EXT[ext]
    # remove any previous logo variants so only one remains
    for e in set(ALLOWED_LOGO_EXT.values()):
        old = os.path.join(FRONTEND_DIR, f"logo.{e}")
        if os.path.exists(old):
            try:
                os.remove(old)
            except OSError:
                pass
    with open(os.path.join(FRONTEND_DIR, f"logo.{save_ext}"), "wb") as f:
        f.write(data)
    cfg = load_config()
    cfg["logo"] = f"/logo.{save_ext}"
    save_config(cfg)
    return {"ok": True, "logo": cfg["logo"]}
@app.get("/api/branding")
def branding():
    """Public: lets the frontend know if a custom logo has been uploaded."""
    cfg = load_config()
    return {"logo": cfg.get("logo") or ""}
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
    try:
        answer = await llm(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=1000,
        )
    except (LLMConfigError, LLMUpstreamError) as e:
        return JSONResponse({"error": str(e)}, status_code=503)
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
    try:
        raw = await llm(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=2500,
        )
    except (LLMConfigError, LLMUpstreamError) as e:
        return JSONResponse({"error": str(e)}, status_code=503)
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
# ---------- Zoom webhook ----------
@app.post("/api/zoom/webhook")
async def zoom_webhook(request: Request):
    body = await request.body()
    payload = await request.json()
    # 1) Zoom URL validation handshake
    if payload.get("event") == "endpoint.url_validation":
        plain = payload["payload"]["plainToken"]
        sig = hmac.new(ZOOM_WEBHOOK_SECRET.encode(), plain.encode(), hashlib.sha256).hexdigest()
        return {"plainToken": plain, "encryptedToken": sig}
    # 2) Verify the signature of real events
    ts = request.headers.get("x-zm-request-timestamp", "")
    got = request.headers.get("x-zm-signature", "")
    message = f"v0:{ts}:{body.decode('utf-8')}".encode()
    expected = "v0=" + hmac.new(ZOOM_WEBHOOK_SECRET.encode(), message, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, got):
        return JSONResponse({"error": "bad signature"}, status_code=401)
    # 3) Handle recording completed
    if payload.get("event") in ("recording.completed", "recording.transcript_completed"):
        obj = payload.get("payload", {}).get("object", {})
        try:
            await ingest_zoom_meeting(obj)
        except Exception as e:
            print("Zoom ingest error:", e)
    return {"ok": True}
@app.get("/api/health")
def health():
    return {"status": "ok", "recordings": len(RECORDINGS)}
# ---------- serve frontend ----------
if os.path.isdir(FRONTEND_DIR):
    @app.get("/")
    def index():
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
    app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="static")
