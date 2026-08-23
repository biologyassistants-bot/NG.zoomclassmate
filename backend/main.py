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
from fastapi import FastAPI, UploadFile, File, Form, Request, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import secrets
import bcrypt

# Data location.
#   * Default: the repo's bundled ./data folder.
#   * On a host with a persistent disk (e.g. Render Starter + a mounted disk),
#     set DATA_DIR=/var/data so recordings, roster, config, question log and the
#     uploaded logo survive restarts and redeploys.
# The bundled ./data folder is always used to SEED an empty persistent disk on
# first boot, so your existing recordings show up the first time.
BUNDLED_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DATA_DIR = os.environ.get("DATA_DIR", "").strip() or BUNDLED_DATA_DIR


def _seed_data_dir():
    """If DATA_DIR is a separate (persistent) location, copy any files that are
    missing there from the bundled data folder. Never overwrites existing files,
    so teacher edits made on the live disk are preserved across deploys."""
    try:
        if os.path.abspath(DATA_DIR) == os.path.abspath(BUNDLED_DATA_DIR):
            return
        os.makedirs(DATA_DIR, exist_ok=True)
        if not os.path.isdir(BUNDLED_DATA_DIR):
            return
        import shutil
        for name in os.listdir(BUNDLED_DATA_DIR):
            src = os.path.join(BUNDLED_DATA_DIR, name)
            dst = os.path.join(DATA_DIR, name)
            if os.path.isfile(src) and not os.path.exists(dst):
                shutil.copy2(src, dst)
    except Exception as e:
        print(f"[data] seed warning: {e}")


_seed_data_dir()

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")
DATA_PATH = os.path.join(DATA_DIR, "recordings.json")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
QLOG_PATH = os.path.join(DATA_DIR, "question_log.json")
ROSTER_PATH = os.path.join(DATA_DIR, "roster.json")
NOTES_LIB_PATH = os.path.join(DATA_DIR, "notes_library.json")
# in-memory active student sessions: token -> {student_id, name, courses}
SESSIONS = {}


# ---------- shared notes library ----------
# Each note's text is stored ONCE here: {id, filename, chunks:[...], chars}.
# A recording references shared notes by id via rec["note_ids"] = [id, ...].
def load_notes_library():
    if os.path.exists(NOTES_LIB_PATH):
        with open(NOTES_LIB_PATH) as f:
            return json.load(f)
    return []


def save_notes_library(lib):
    with open(NOTES_LIB_PATH, "w") as f:
        json.dump(lib, f, ensure_ascii=False, indent=2)


def note_by_id(note_id, lib=None):
    lib = lib if lib is not None else load_notes_library()
    return next((n for n in lib if n["id"] == note_id), None)


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
# OpenAI key diagnostic endpoint (INLINE — no separate file needed).
# Open  /api/diag/openai  to confirm the key is set & working. It never
# returns the key itself, only its length + a masked preview.
# Delete this whole block once you're done debugging.
# ---------------------------------------------------------------------------
def _diag_mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 10:
        return key[:2] + "*" * (len(key) - 2)
    return key[:5] + "..." + key[-4:]


@app.get("/api/diag/openai")
async def diag_openai():
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").strip()
    report = {
        "env_var_present": bool(key),
        "key_length": len(key),
        "key_masked_preview": _diag_mask(key),
        "model": model,
        "base_url": base,
    }
    if not key:
        report["ok"] = False
        report["message"] = (
            "OPENAI_API_KEY is NOT set (or empty) on this server. Add it under "
            "Render -> Environment and redeploy."
        )
        return JSONResponse(status_code=200, content=report)

    import httpx
    try:
        timeout = httpx.Timeout(30.0, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": model,
                      "messages": [{"role": "user", "content": "ping"}],
                      "max_tokens": 1},
            )
    except httpx.RequestError as e:
        report["ok"] = False
        report["message"] = (f"Network error reaching {base}: {e}. On Render free "
                             "tier this can be a cold-start timeout; retry once warm.")
        return JSONResponse(status_code=200, content=report)

    if resp.status_code >= 400:
        detail = ""
        try:
            detail = resp.json().get("error", {}).get("message", "")
        except Exception:
            detail = resp.text[:200]
        report["ok"] = False
        report["provider_status"] = resp.status_code
        low = (detail or "").lower()
        if resp.status_code == 401 or "incorrect api key" in low or "invalid" in low:
            report["message"] = ("Key REJECTED (invalid/incorrect). Re-copy from "
                                 "platform.openai.com and update OPENAI_API_KEY, then redeploy.")
        elif resp.status_code == 429 or "quota" in low or "billing" in low:
            report["message"] = ("Key valid but NO CREDIT / rate-limited. Add "
                                 "billing/credits to the OpenAI account.")
        elif resp.status_code == 404 or ("model" in low and "not" in low):
            report["message"] = ("Key works but the model isn't available to this "
                                 "account. Set OPENAI_MODEL to one you can use.")
        else:
            report["message"] = f"Provider returned {resp.status_code}: {detail[:200]}"
        return JSONResponse(status_code=200, content=report)

    report["ok"] = True
    report["provider_status"] = resp.status_code
    report["message"] = "OPENAI_API_KEY is set AND the API call succeeded. The key is working."
    return JSONResponse(status_code=200, content=report)


def load_recordings():
    if os.path.exists(DATA_PATH):
        with open(DATA_PATH) as f:
            return json.load(f)
    return []


RECORDINGS = load_recordings()
REC_BY_ID = {r["id"]: r for r in RECORDINGS}


def _migrate_inline_notes_to_library():
    """One-time migration: older data stored notes inline on each recording as
    rec['notes'] = [{id, filename, chunks}]. Move them into the shared library and
    replace with rec['note_ids'] = [id,...]. Safe to run every startup (idempotent)."""
    lib = load_notes_library()
    lib_ids = {n["id"] for n in lib}
    changed_lib = False
    changed_recs = False
    for r in RECORDINGS:
        inline = r.get("notes")
        if inline:
            ids = list(r.get("note_ids") or [])
            for n in inline:
                nid = n.get("id") or secrets.token_hex(6)
                if nid not in lib_ids:
                    lib.append({"id": nid, "filename": n.get("filename") or "notes",
                                "chunks": n.get("chunks", []),
                                "chars": sum(len(c) for c in n.get("chunks", []))})
                    lib_ids.add(nid); changed_lib = True
                if nid not in ids:
                    ids.append(nid)
            r["note_ids"] = ids
            r.pop("notes", None)
            changed_recs = True
        elif r.get("note_ids") is None:
            r["note_ids"] = []
    if changed_lib:
        save_notes_library(lib)
    if changed_recs:
        save_recordings(RECORDINGS)


_migrate_inline_notes_to_library()


def fmt_ts(t):
    """Format a transcript start_time (which may be 'HH:MM:SS' or seconds) as mm:ss / h:mm:ss."""
    if t is None:
        return "?"
    s = str(t)
    if ":" in s:
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


_word_re = re.compile(r"[A-Za-z0-9\u00c0-\u024f\u0400-\u04ff\u0600-\u06ff]+")

def tokenize(text):
    return [w.lower() for w in _word_re.findall(text or "")]

# ---------- semantic retrieval (OpenAI Embeddings) ----------
async def get_embedding(text: str) -> list[float]:
    """Fetch a single embedding vector for the student's query."""
    import httpx
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{OPENAI_BASE_URL}/embeddings",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={"input": text, "model": "text-embedding-3-small"}
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]

def cosine_similarity(v1, v2):
    """Calculate how closely related two pieces of text are."""
    dot = sum(a * b for a, b in zip(v1, v2))
    mag = math.sqrt(sum(a * a for a in v1)) * math.sqrt(sum(b * b for b in v2))
    return dot / mag if mag else 0.0

async def build_index_async(rec):
    """Fetch embeddings for the entire transcript and cache them on the recording object."""
    if "embeddings" in rec:
        return rec["embeddings"]
        
    segs = rec["segments"]
    texts = [s.get("text", "") for s in segs]
    if not texts:
        return []
    
    import httpx
    embeddings = []
    batch_size = 1000  # Batch up to 1000 items to stay safely under OpenAI's 2048 limit
    
    async with httpx.AsyncClient(timeout=60) as client:
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            resp = await client.post(
                f"{OPENAI_BASE_URL}/embeddings",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                json={"input": batch, "model": "text-embedding-3-small"}
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])
            embeddings.extend([d["embedding"] for d in sorted(data, key=lambda x: x["index"])])
            
    rec["embeddings"] = embeddings  # Cache it so we only pay for this once per recording
    save_recordings(RECORDINGS)
    return embeddings

async def retrieve(rec, query, k=18, window=1):
    """Find the most relevant transcript segments using semantic similarity."""
    segs = rec["segments"]
    if not segs: 
        return []
    
    doc_embeddings = await build_index_async(rec)
    q_embedding = await get_embedding(query)
    
    scores = [cosine_similarity(q_embedding, doc_emb) for doc_emb in doc_embeddings]
    
    # Rank segments by relevance
    ranked = sorted(range(len(segs)), key=lambda i: scores[i], reverse=True)
    
    # 0.3 is a good semantic threshold to ignore irrelevant chatter
    top = [i for i in ranked if scores[i] > 0.3][:k] 
    
    if not top:
        # Fallback to broad sampling if no relevant segments are found
        step = max(1, len(segs) // 40)
        top = list(range(0, len(segs), step))[:40]
        
    chosen = set()
    for i in top:
        for j in range(max(0, i - window), min(len(segs), i + window + 1)):
            chosen.add(j)
    return sorted(chosen)


# ---------- teacher notes: extraction + retrieval ----------
def extract_text_from_upload(data: bytes, filename: str) -> str:
    """Extract plain text from an uploaded PDF / DOCX / TXT file (server-side only).
    The original file is never stored or served; only the extracted text is kept."""
    name = (filename or "").lower()
    if name.endswith(".txt") or name.endswith(".md"):
        for enc in ("utf-8", "utf-16", "latin-1"):
            try:
                return data.decode(enc)
            except Exception:
                continue
        return data.decode("utf-8", "replace")
    if name.endswith(".pdf"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    if name.endswith(".docx"):
        import docx
        doc = docx.Document(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs)
    raise ValueError("Unsupported file type. Please upload a PDF, DOCX, TXT or MD file.")


def chunk_note_text(text: str, target_chars=700):
    """Split note text into paragraph-ish chunks for retrieval. Keeps chunks small
    so the AI can quote precisely and we can rank them against a question."""
    text = re.sub(r"\r\n?", "\n", text or "")
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks = []
    buf = ""
    for p in paras:
        if len(buf) + len(p) + 1 <= target_chars:
            buf = f"{buf}\n{p}".strip()
        else:
            if buf:
                chunks.append(buf)
            # if a single paragraph is huge, hard-split it
            while len(p) > target_chars:
                chunks.append(p[:target_chars])
                p = p[target_chars:]
            buf = p
    if buf:
        chunks.append(buf)
    return [c for c in chunks if c.strip()]


def retrieve_note_chunks(rec, query, k=4):
    """Return the top-k most relevant note chunks (across all notes on this
    recording) for the query, as a list of {note_title, text}. tf-idf ranked."""
    lib = load_notes_library()
    notes = [note_by_id(nid, lib) for nid in (rec.get("note_ids") or [])]
    notes = [n for n in notes if n]
    entries = []  # (note_title, chunk_text)
    for note in notes:
        for ch in note.get("chunks", []):
            entries.append((note.get("filename") or "notes", ch))
    if not entries:
        return []
    docs = [tokenize(t) for (_, t) in entries]
    df = Counter()
    for d in docs:
        for w in set(d):
            df[w] += 1
    N = len(docs) or 1
    idf = {w: math.log(1 + N / c) for w, c in df.items()}
    q = Counter(tokenize(query))
    scores = []
    for d in docs:
        if not d:
            scores.append(0.0); continue
        tf = Counter(d)
        s = 0.0
        for w, qc in q.items():
            if w in tf:
                s += idf.get(w, 0.0) * (tf[w] / len(d)) * qc
        scores.append(s)
    ranked = sorted(range(len(entries)), key=lambda i: scores[i], reverse=True)
    chosen = [i for i in ranked if scores[i] > 0][:k]
    if not chosen:  # no lexical overlap → give the first couple of chunks as fallback
        chosen = list(range(min(2, len(entries))))
    return [{"note_title": entries[i][0], "text": entries[i][1]} for i in chosen]


def notes_context(rec, query, max_chars=8000):
    """Build a labeled notes context block for the LLM prompt (teacher-only source)."""
    chunks = retrieve_note_chunks(rec, query)
    if not chunks:
        return ""
    out, total = [], 0
    for c in chunks:
        block = f'[NOTE: {c["note_title"]}] {c["text"].strip()}'
        if total + len(block) > max_chars:
            break
        out.append(block)
        total += len(block)
    return "\n\n".join(out)


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
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").strip()


class LLMConfigError(Exception):
    """Raised when the LLM backend is not configured (no key, no fallback)."""


class LLMUpstreamError(Exception):
    """Raised when the OpenAI-compatible API returns an error."""


async def llm(messages, max_tokens=1200, temperature=0.1):
    if OPENAI_API_KEY:
        import httpx
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

    raise LLMConfigError(
        "The AI features are not configured on this server. "
        "Set the OPENAI_API_KEY environment variable (and redeploy)."
    )


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
        "summary": r.get("summary") or "",
        "topics": r.get("topics") or [],
        "has_notes": bool(r.get("note_ids")),
        "notes_count": len(r.get("note_ids") or []),
        "notes": (_card_notes_meta(r) if include_hidden else None),
    }


def _card_notes_meta(r):
    lib = load_notes_library()
    out = []
    for nid in (r.get("note_ids") or []):
        n = note_by_id(nid, lib)
        if n:
            out.append({"id": n["id"], "filename": n.get("filename"),
                        "chars": n.get("chars", sum(len(c) for c in n.get("chunks", []))),
                        "chunks": len(n.get("chunks", []))})
    return out


class RecListBody(BaseModel):
    token: str | None = None


@app.post("/api/recordings")
def list_recordings(body: RecListBody):
    sess = valid_session(body.token)
    if not sess:
        return JSONResponse({"error": "Your session has expired. Please log in again."}, status_code=401)
    my_courses = sess.get("courses", [])
    def allowed(r):
        if not r.get("visible", True):
            return False
        if not my_courses:
            return False
        return (r.get("unit") or "Unassigned") in my_courses
    def _date_key(r):
        return (r.get("date") or "9999-12-31 23:59:59")
    allowed_recs = sorted([r for r in RECORDINGS if allowed(r)], key=_date_key)
    out = [_card(r) for r in allowed_recs]
    units = []
    seen = set()
    for r in allowed_recs:
        u = r.get("unit") or "Unassigned"
        if u not in seen:
            seen.add(u)
            units.append(u)
    return {"recordings": out, "units": units}


class LoginBody(BaseModel):
    passcode: str


def check_passcode(passcode: str) -> bool:
    return passcode == load_config().get("passcode")


@app.post("/api/teacher/login")
def teacher_login(body: LoginBody):
    if check_passcode(body.passcode):
        return {"ok": True}
    return JSONResponse({"ok": False, "error": "Wrong passcode"}, status_code=401)


def _norm(s):
    return (s or "").strip().lower()


def normalize_courses(value):
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[;,]", value)
    elif isinstance(value, (list, tuple)):
        parts = []
        for v in value:
            if isinstance(v, str):
                parts.extend(re.split(r"[;,]", v))
            elif v is not None:
                parts.append(str(v))
    else:
        return []
    seen, out = set(), []
    for p in parts:
        p = (p or "").strip()
        if p and p.lower() not in seen:
            out.append(p); seen.add(p.lower())
    return out


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
                "courses": normalize_courses(st.get("courses")),
            }
            return {"ok": True, "token": token, "name": SESSIONS[token]["name"]}
    return JSONResponse(
        {"ok": False, "error": "That email and password don't match our class roster. Check with your teacher."},
        status_code=401,
    )


def valid_session(token: str):
    return SESSIONS.get(token or "")


# ---------- Cross-Device Study Plan Sync Endpoints ----------
class SavePlanBody(BaseModel):
    token: str
    plan: list | None = None

@app.post("/api/student/plan/save")
def save_student_plan(body: SavePlanBody):
    sess = valid_session(body.token)
    if not sess:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    roster = load_roster()
    student = next((s for s in roster if s["id"] == sess["student_id"]), None)
    if student:
        student["study_plan"] = body.plan
        save_roster(roster)
    return {"ok": True}

@app.post("/api/student/plan/get")
def get_student_plan(body: RecListBody):
    sess = valid_session(body.token)
    if not sess:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    roster = load_roster()
    student = next((s for s in roster if s["id"] == sess["student_id"]), None)
    plan = student.get("study_plan") if student else None
    return {"plan": plan}


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
        "courses": normalize_courses(s.get("courses")),
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
    courses = [c.strip() for c in (body.courses or "").split(";") if c.strip()]
    existing = next((s for s in roster if _norm(s.get("email")) == _norm(email)), None)
    if existing:
        prev = normalize_courses(existing.get("courses"))
        seen = {_norm(c) for c in prev}
        merged = list(prev)
        added_courses = []
        for c in courses:
            if _norm(c) not in seen:
                merged.append(c); seen.add(_norm(c)); added_courses.append(c)
        existing["courses"] = merged
        if name and name != email.split("@")[0]:
            existing["name"] = name
        if (body.password or "").strip():
            existing["password_hash"] = hash_pw(body.password)
        save_roster(roster)
        return {"ok": True, "merged": True}
    student = {
        "id": secrets.token_hex(6),
        "name": name or email.split("@")[0],
        "email": email,
        "courses": courses,
        "password_hash": hash_pw(body.password) if (body.password or "").strip() else "",
    }
    roster.append(student)
    save_roster(roster)
    return {"ok": True, "merged": False}


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
    
    idx = await retrieve(rec, body.question)
    ctx = context_from_indices(rec, idx)
    notes_ctx = notes_context(rec, body.question)
    
    system = "You are an expert Biology tutor. Answer strictly based on the recording excerpts and notes provided."
    user = f"Transcript:\n{ctx}\n\nNotes:\n{notes_ctx}\n\nQuestion: {body.question}"
    
    try:
        answer = await llm([{"role": "system", "content": system}, {"role": "user", "content": user}], max_tokens=1000)
    except (LLMConfigError, LLMUpstreamError) as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    
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
            "answer": answer,
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
    segs = rec["segments"]
    step = max(1, len(segs) // 60)
    idx = list(range(0, len(segs), step))
    ctx = context_from_indices(rec, idx, max_chars=60000)
    system = (
        "You are ClassMate, creating a quiz. Use ONLY the transcript content provided. "
        "Return STRICT JSON only, no markdown, no prose. "
        "Schema: {\"questions\":[{\"question\":str,\"options\":[str,str,str,str],"
        "\"answer_index\":int,\"explanation\":str,\"timestamp\":str}]}."
    )
    user = f"Class recording: {rec.get('display_title') or rec.get('topic')}\n\nTranscript excerpts:\n{ctx}"
    try:
        raw = await llm([{"role": "system", "content": system}, {"role": "user", "content": user}], max_tokens=2500)
        data = json.loads(raw[raw.find("{"):raw.rfind("}")+1])
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    return data


class FlashcardBody(BaseModel):
    recording_id: str
    token: str | None = None


@app.post("/api/flashcards")
async def generate_flashcards(body: FlashcardBody):
    sess = valid_session(body.token)
    if not sess:
        return JSONResponse({"error": "Your session has expired. Please log in again."}, status_code=401)
    rec = REC_BY_ID.get(body.recording_id)
    if not rec:
        return JSONResponse({"error": "Recording not found"}, status_code=404)

    transcript_text = "\n".join([f"[{s.get('start','')}] {s.get('text','')}" for s in rec.get("segments", [])])
    notes_text = notes_context(rec, "flashcards review summary", max_chars=8000)
    
    system = (
        "You are an expert Biology tutor. Generate 6 to 8 flashcards for active recall study. "
        "Return STRICT JSON only, no markdown, no prose. "
        "Schema: {\"flashcards\":[{\"front\":str,\"back\":str}]}."
    )
    user = f"TRANSCRIPT:\n{transcript_text[:12000]}\n\nNOTES:\n{notes_text[:4000]}"
    try:
        raw = await llm([{"role": "system", "content": system}, {"role": "user", "content": user}], max_tokens=1500)
        data = json.loads(raw[raw.find("{"):raw.rfind("}")+1])
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    return data


class StudyPlanBody(BaseModel):
    recording_ids: list[str]
    days: int
    hours_per_day: float
    focus: str
    token: str | None = None


@app.post("/api/student/plan")
async def generate_study_plan(body: StudyPlanBody):
    if not valid_session(body.token):
        return JSONResponse({"error": "Your session has expired. Please log in again."}, status_code=401)
    
    if not body.recording_ids:
        return JSONResponse({"error": "Please select at least one class to study."}, status_code=400)

    selected_recs = []
    for rid in body.recording_ids:
        rec = REC_BY_ID.get(rid)
        if rec:
            selected_recs.append(f"- Title: {rec.get('display_title') or rec.get('topic')}")

    recs_text = "\n".join(selected_recs)
    system = (
        "You are an expert academic coach for Biology students. Create a balanced, day-by-day study plan. "
        "Return STRICT JSON only. "
        "Schema: {\"plan\":[{\"day\":int,\"quote\":str,\"tasks\":[{\"title\":str,\"description\":str,\"est_minutes\":int}]}]}."
    )
    user = f"Days: {body.days}\nHours/day: {body.hours_per_day}\nFocus: {body.focus}\nClasses:\n{recs_text}"
    try:
        raw = await llm([{"role": "system", "content": system}, {"role": "user", "content": user}], max_tokens=2500)
        data = json.loads(raw[raw.find("{"):raw.rfind("}")+1])
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    return data


@app.get("/api/health")
def health():
    return {"status": "ok", "recordings": len(RECORDINGS)}


if os.path.isdir(FRONTEND_DIR):
    @app.get("/")
    def index():
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
    app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="static")
