import json
import os
import re
import math
import io
import base64
import time
import hashlib
import hmac
import gc
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


def _clean_recordings_disk_file(file_path):
    """Streams through recordings.json line-by-line to strip out heavy float
    arrays directly on disk without consuming RAM, preventing 2GB+ boot crashes."""
    if not os.path.exists(file_path):
        return
    if os.path.getsize(file_path) < 500 * 1024:
        return

    tmp_path = file_path + ".clean.tmp"
    try:
        has_embeddings = False
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if '"embeddings"' in line:
                    has_embeddings = True
                    break
        if not has_embeddings:
            return

        print(f"[startup] Sanitizing {file_path} to prevent memory exhaustion...")
        with open(file_path, "r", encoding="utf-8", errors="ignore") as fin, \
             open(tmp_path, "w", encoding="utf-8") as fout:
            
            skipping_embeddings = False
            bracket_depth = 0
            prev_line = None

            for line in fin:
                if not skipping_embeddings:
                    if '"embeddings"' in line:
                        if '[' in line:
                            bracket_depth = line.count('[') - line.count(']')
                            if bracket_depth > 0:
                                skipping_embeddings = True
                                continue
                            else:
                                continue
                        else:
                            skipping_embeddings = True
                            bracket_depth = 0
                            continue
                    
                    if prev_line is not None:
                        stripped = line.strip()
                        if (stripped.startswith("}") or stripped.startswith("]")) and prev_line.rstrip().endswith(","):
                            prev_clean = prev_line.rstrip()[:-1] + "\n"
                            fout.write(prev_clean)
                        else:
                            fout.write(prev_line)
                    prev_line = line
                else:
                    bracket_depth += line.count('[') - line.count(']')
                    if bracket_depth <= 0:
                        skipping_embeddings = False
                        continue

            if prev_line is not None:
                fout.write(prev_line)

        os.replace(tmp_path, file_path)
        print(f"[startup] Cleaned {file_path}. Memory usage stabilized.")
    except Exception as e:
        print(f"[startup cleaner error]: {e}")
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


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

# Clean heavy embeddings from persistent disk before loading into memory
_clean_recordings_disk_file(DATA_PATH)


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
    clean_recs = []
    for r in recs:
        r_copy = dict(r)
        r_copy.pop("embeddings", None)
        clean_recs.append(r_copy)
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(clean_recs, f, ensure_ascii=False, indent=2)


def load_recordings():
    if os.path.exists(DATA_PATH):
        try:
            with open(DATA_PATH, "r", encoding="utf-8") as f:
                recs = json.load(f)
                for r in recs:
                    r.pop("embeddings", None)
                return recs
        except Exception as e:
            print(f"[recordings] primary load error: {e}")
            try:
                bundled_path = os.path.join(BUNDLED_DATA_DIR, "recordings.json")
                if os.path.exists(bundled_path) and os.path.abspath(bundled_path) != os.path.abspath(DATA_PATH):
                    with open(bundled_path, "r", encoding="utf-8") as bf:
                        return json.load(bf)
            except Exception:
                pass
    return []


RECORDINGS = load_recordings()
REC_BY_ID = {r["id"]: r for r in RECORDINGS}


app = FastAPI(title="ClassMate API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# OpenAI key diagnostic endpoint
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
    """Fetch embeddings for the entire transcript and cache them in-memory only."""
    if "embeddings" in rec and rec["embeddings"]:
        return rec["embeddings"]
        
    segs = rec.get("segments", [])
    texts = [s.get("text", "") for s in segs]
    if not texts:
        return []
    
    import httpx
    embeddings = []
    batch_size = 500
    
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
            
    rec["embeddings"] = embeddings
    return embeddings


async def retrieve(rec, query, k=18, window=1):
    """Find the most relevant transcript segments using semantic similarity."""
    segs = rec.get("segments", [])
    if not segs: 
        return []
    
    doc_embeddings = await build_index_async(rec)
    q_embedding = await get_embedding(query)
    
    scores = [cosine_similarity(q_embedding, doc_emb) for doc_emb in doc_embeddings]
    ranked = sorted(range(len(segs)), key=lambda i: scores[i], reverse=True)
    top = [i for i in ranked if scores[i] > 0.3][:k] 
    
    if not top:
        step = max(1, len(segs) // 40)
        top = list(range(0, len(segs), step))[:40]
        
    chosen = set()
    for i in top:
        for j in range(max(0, i - window), min(len(segs), i + window + 1)):
            chosen.add(j)
    return sorted(chosen)


# ---------- teacher notes: extraction + retrieval ----------
def extract_text_from_upload(data: bytes, filename: str) -> str:
    """Extract plain text from an uploaded PDF / DOCX / TXT file (server-side only)."""
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
    """Split note text into paragraph-ish chunks for retrieval."""
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
            while len(p) > target_chars:
                chunks.append(p[:target_chars])
                p = p[target_chars:]
            buf = p
    if buf:
        chunks.append(buf)
    return [c for c in chunks if c.strip()]


def retrieve_note_chunks(rec, query, k=4):
    """Return the top-k most relevant note chunks for the query."""
    lib = load_notes_library()
    notes = [note_by_id(nid, lib) for nid in (rec.get("note_ids") or [])]
    notes = [n for n in notes if n]
    entries = []
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
    if not chosen:
        chosen = list(range(min(2, len(entries))))
    return [{"note_title": entries[i][0], "text": entries[i][1]} for i in chosen]


def notes_context(rec, query, max_chars=8000):
    """Build a labeled notes context block for the LLM prompt."""
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
    segs = rec.get("segments", [])
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
    pass


class LLMUpstreamError(Exception):
    pass


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
            raise LLMUpstreamError(f"Could not reach AI provider: {e}") from e

        if resp.status_code >= 400:
            detail = ""
            try:
                detail = resp.json().get("error", {}).get("message", "")
            except Exception:
                detail = resp.text[:300]
            raise LLMUpstreamError(f"AI provider returned {resp.status_code}: {detail or 'unknown error'}")

        try:
            return resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            raise LLMUpstreamError(f"Unexpected AI response shape: {e}") from e

    raise LLMConfigError("The AI features are not configured on this server. Set OPENAI_API_KEY.")


OPENAI_TRANSCRIBE_MODEL = os.environ.get("OPENAI_TRANSCRIBE_MODEL", "whisper-1").strip()
WHISPER_MAX_BYTES = 25 * 1000 * 1000
_CHUNK_SAFETY_BYTES = 24 * 1000 * 1000


def _have_ffmpeg():
    import shutil
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _ffprobe_duration(path):
    import subprocess
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=120,
        )
        return float((out.stdout or "0").strip() or 0.0)
    except Exception:
        return 0.0


def _ffmpeg_to_mp3(src_path, dst_path, start=None, duration=None, bitrate="48k"):
    import subprocess
    cmd = ["ffmpeg", "-y", "-v", "quiet"]
    if start is not None:
        cmd += ["-ss", str(start)]
    cmd += ["-i", src_path]
    if duration is not None:
        cmd += ["-t", str(duration)]
    cmd += ["-ac", "1", "-ar", "16000", "-b:a", bitrate, dst_path]
    subprocess.run(cmd, check=True, timeout=1800)


def _fmt_seconds_to_ts(seconds):
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        seconds = 0.0
    if seconds < 0:
        seconds = 0.0
    ms = int(round((seconds - int(seconds)) * 1000))
    total = int(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


async def transcribe_audio_bytes(audio_bytes, filename="audio.m4a", time_offset=0.0):
    if not OPENAI_API_KEY:
        raise LLMConfigError("Transcription needs OPENAI_API_KEY set on this server.")
    import httpx
    timeout = httpx.Timeout(600.0, connect=15.0)
    files = {"file": (filename, audio_bytes, "application/octet-stream")}
    english_only = os.environ.get("TRANSCRIBE_ENGLISH_ONLY", "1").strip() != "0"
    endpoint = "/audio/translations" if english_only else "/audio/transcriptions"
    data = {
        "model": OPENAI_TRANSCRIBE_MODEL,
        "response_format": "verbose_json",
        "timestamp_granularities[]": "segment",
    }
    if not english_only:
        data["language"] = os.environ.get("TRANSCRIBE_LANGUAGE", "").strip() or None
        data = {k: v for k, v in data.items() if v is not None}
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{OPENAI_BASE_URL}{endpoint}",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            data=data,
            files=files,
        )
    resp.raise_for_status()
    payload = resp.json()
    segments = []
    for seg in payload.get("segments", []) or []:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        segments.append({
            "start": _fmt_seconds_to_ts(float(seg.get("start", 0)) + time_offset),
            "speaker": "",
            "text": text,
        })
    if not segments:
        whole = (payload.get("text") or "").strip()
        if whole:
            segments.append({
                "start": _fmt_seconds_to_ts(time_offset),
                "speaker": "",
                "text": whole,
            })
    return segments


async def fetch_zoom_recording_files(meeting_id):
    import httpx
    from urllib.parse import quote
    token = await zoom_token()
    mid = str(meeting_id)
    needs_double = mid.startswith("/") or "//" in mid or "/" in mid
    path_id = quote(quote(mid, safe=""), safe="") if needs_double else quote(mid, safe="")
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(
            f"https://api.zoom.us/v2/meetings/{path_id}/recordings",
            headers={"Authorization": f"Bearer {token}"},
        )
        if r.status_code != 200:
            raise LLMUpstreamError(f"Zoom recordings lookup returned {r.status_code}: {r.text[:300]}")
        return r.json().get("recording_files", []) or []


async def fetch_zoom_recording_object(meeting_id):
    import httpx
    from urllib.parse import quote
    token = await zoom_token()
    mid = str(meeting_id)
    needs_double = mid.startswith("/") or "//" in mid or "/" in mid
    path_id = quote(quote(mid, safe=""), safe="") if needs_double else quote(mid, safe="")
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(
            f"https://api.zoom.us/v2/meetings/{path_id}/recordings",
            headers={"Authorization": f"Bearer {token}"},
        )
        if r.status_code == 404:
            raise LLMUpstreamError("No cloud recording found for that meeting ID.")
        if r.status_code != 200:
            raise LLMUpstreamError(f"Zoom lookup returned {r.status_code}: {r.text[:300]}")
        return r.json()


def _parse_meeting_id(raw: str) -> str:
    import re as _re
    from urllib.parse import urlparse, parse_qs, unquote
    s = (raw or "").strip()
    if not s:
        return ""
    if s.startswith("http"):
        u = urlparse(s)
        qs = parse_qs(u.query)
        for key in ("meeting_id", "meetingId", "confId"):
            if key in qs and qs[key]:
                return unquote(qs[key][0])
        m = _re.search(r"/j/(\d{9,})", u.path)
        if m:
            return m.group(1)
        m = _re.search(r"(\d{9,})", u.path)
        if m:
            return m.group(1)
        return ""
    return s.replace(" ", "")


def _pick_audio_file(files):
    audio = next((f for f in files if (f.get("file_type") or "").upper() == "M4A"), None)
    if audio:
        return audio
    return next((f for f in files if (f.get("file_type") or "").upper() == "MP4"), None)


async def _transcribe_large_audio(src_path):
    import os as _os
    workdir = _os.path.dirname(src_path)
    full_mp3 = _os.path.join(workdir, "full.mp3")
    _ffmpeg_to_mp3(src_path, full_mp3)

    size = _os.path.getsize(full_mp3)
    if size <= _CHUNK_SAFETY_BYTES:
        with open(full_mp3, "rb") as f:
            data = f.read()
        return await transcribe_audio_bytes(data, filename="full.mp3")

    duration = _ffprobe_duration(full_mp3)
    if duration <= 0:
        with open(full_mp3, "rb") as f:
            data = f.read()
        return await transcribe_audio_bytes(data, filename="full.mp3")

    bytes_per_sec = size / duration
    chunk_secs = max(60.0, (_CHUNK_SAFETY_BYTES / bytes_per_sec) * 0.9)

    all_segments = []
    start = 0.0
    idx = 0
    while start < duration:
        this_len = min(chunk_secs, duration - start)
        chunk_path = _os.path.join(workdir, f"chunk_{idx}.mp3")
        _ffmpeg_to_mp3(src_path, chunk_path, start=start, duration=this_len)
        with open(chunk_path, "rb") as f:
            cdata = f.read()
        seg = await transcribe_audio_bytes(cdata, filename=f"chunk_{idx}.mp3", time_offset=start)
        all_segments.extend(seg)
        try:
            _os.remove(chunk_path)
        except OSError:
            pass
        start += this_len
        idx += 1
    return all_segments


async def transcribe_recording_by_id(meeting_id):
    import os as _os
    import tempfile
    rec = REC_BY_ID.get(meeting_id)
    if not rec:
        raise LLMUpstreamError("Recording not found.")
    files = await fetch_zoom_recording_files(meeting_id)
    audio = _pick_audio_file(files)
    if not audio:
        raise LLMUpstreamError("No audio/video file is available for this recording in Zoom's cloud.")
    import httpx
    token = await zoom_token()
    url = audio.get("download_url")
    ext = (audio.get("file_extension") or audio.get("file_type") or "m4a").lower()
    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=15.0), follow_redirects=True) as client:
        r = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        if r.status_code != 200:
            raise LLMUpstreamError(f"Could not download audio from Zoom ({r.status_code}).")
        audio_bytes = r.content

    if len(audio_bytes) <= _CHUNK_SAFETY_BYTES:
        segments = await transcribe_audio_bytes(audio_bytes, filename=f"{meeting_id}.{ext}")
    elif _have_ffmpeg():
        with tempfile.TemporaryDirectory() as tmp:
            src_path = _os.path.join(tmp, f"src.{ext}")
            with open(src_path, "wb") as f:
                f.write(audio_bytes)
            segments = await _transcribe_large_audio(src_path)
    else:
        raise LLMUpstreamError("Audio file exceeds 25 MB and ffmpeg is unavailable.")

    rec["segments"] = segments
    save_recordings(RECORDINGS)
    try:
        audio_bytes = None
    except Exception:
        pass
    rec.pop("embeddings", None)
    gc.collect()
    return len(segments)


# ---------- Zoom integration ----------
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


async def _download_zoom_text(url: str, token: str) -> str:
    """Download transcript (.vtt) safely through Zoom S3 redirects by appending token."""
    sep = "&" if "?" in url else "?"
    auth_url = f"{url}{sep}access_token={token}"
    import httpx
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        r = await client.get(auth_url, headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 200:
            return r.text
    return ""


def parse_vtt(text):
    segments = []
    blocks = re.split(r"\n\s*\n", text.strip())
    for b in blocks:
        lines = [l for l in b.splitlines() if l.strip()]
        if not lines:
            continue
        tline_i = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if tline_i is None:
            continue
        start = lines[tline_i].split("-->")[0].strip().split(".")[0]
        body = " ".join(lines[tline_i + 1:]).strip()
        speaker = ""
        m = re.match(r"^([^:]{1,40}):\s*(.*)$", body)
        if m:
            speaker, body = m.group(1).strip(), m.group(2).strip()
        if body:
            segments.append({"start": start, "speaker": speaker, "text": body})
    return segments


def _detect_source(obj):
    t = obj.get("type")
    try:
        if int(t) in (5, 6, 9):
            return "webinar"
    except (TypeError, ValueError):
        if isinstance(t, str) and "webinar" in t.lower():
            return "webinar"
    return "meeting"


async def ingest_zoom_meeting(obj, allow_whisper_fallback=True):
    meeting_id = str(obj.get("id") or obj.get("uuid") or secrets.token_hex(6))
    
    # If the recording already exists AND has transcripts, skip
    if meeting_id in REC_BY_ID and len(REC_BY_ID[meeting_id].get("segments", [])) > 0:
        return False

    topic = obj.get("topic", "Untitled class")
    start_time = (obj.get("start_time") or "")[:10]
    source = _detect_source(obj)
    files = obj.get("recording_files", [])

    if not files:
        try:
            files = await fetch_zoom_recording_files(meeting_id)
        except Exception:
            files = []
    
    transcript = next(
        (f for f in files if (f.get("file_type") or "").upper() in ("TRANSCRIPT", "AUDIO_TRANSCRIPT") 
         or (f.get("file_extension") or "").upper() == "VTT"
         or f.get("recording_type") == "audio_transcript"), 
        None
    )
    
    segments = []
    if transcript and transcript.get("download_url"):
        try:
            token = await zoom_token()
            vtt_text = await _download_zoom_text(transcript["download_url"], token)
            if vtt_text:
                segments = parse_vtt(vtt_text)
        except Exception as e:
            print(f"[zoom] VTT transcript download failed for {meeting_id}: {e}")

    # If recording already exists without transcript, update it when transcript arrives
    if meeting_id in REC_BY_ID:
        existing = REC_BY_ID[meeting_id]
        if segments:
            existing["segments"] = segments
            save_recordings(RECORDINGS)
            print(f"[zoom] Attached {len(segments)} transcript lines to: '{existing.get('display_title')}'")
            return True
        return False

    if allow_whisper_fallback and not segments and OPENAI_API_KEY:
        audio = _pick_audio_file(files)
        if audio and audio.get("download_url"):
            try:
                token = await zoom_token()
                import httpx
                async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=15.0), follow_redirects=True) as client:
                    ar = await client.get(audio["download_url"], headers={"Authorization": f"Bearer {token}"})
                if ar.status_code == 200:
                    ext = (audio.get("file_extension") or audio.get("file_type") or "m4a").lower()
                    segments = await transcribe_audio_bytes(ar.content, filename=f"{meeting_id}.{ext}")
            except Exception as e:
                print(f"[ingest] whisper fallback failed for {meeting_id}: {e}")
    new_rec = {
        "id": meeting_id,
        "topic": topic,
        "original_topic": topic,
        "display_title": topic,
        "date": start_time,
        "source": source,
        "unit": "",
        "visible": False,
        "segments": segments,
        "note_ids": []
    }
    RECORDINGS.append(new_rec)
    REC_BY_ID[meeting_id] = new_rec
    save_recordings(RECORDINGS)
    print(f"[zoom] Successfully imported '{topic}' with {len(segments)} lines.")
    return True


# ---------- API Endpoints ----------
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


def _repair_roster_courses():
    try:
        roster = load_roster()
        changed = False
        for s in roster:
            fixed = normalize_courses(s.get("courses"))
            if fixed != s.get("courses"):
                s["courses"] = fixed
                changed = True
        if changed:
            save_roster(roster)
            print("[roster] normalized course lists on startup")
    except Exception as e:
        print(f"[roster] repair warning: {e}")


_repair_roster_courses()


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


# ---------- Cross-Device Sync Endpoints ----------
class StudentSyncBody(BaseModel):
    token: str
    study_plan: list | None = None
    student_stats: dict | None = None
    chat_history: dict | None = None
    flashcard_deck: list | None = None


@app.post("/api/student/sync")
def sync_student_data(body: StudentSyncBody):
    sess = valid_session(body.token)
    if not sess:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    roster = load_roster()
    student = next((s for s in roster if s["id"] == sess["student_id"]), None)
    if student:
        if body.study_plan is not None:
            student["study_plan"] = body.study_plan
        if body.student_stats is not None:
            student["student_stats"] = body.student_stats
        if body.chat_history is not None:
            student["chat_history"] = body.chat_history
        if body.flashcard_deck is not None:
            student["flashcard_deck"] = body.flashcard_deck
        save_roster(roster)
    return {"ok": True}


@app.post("/api/student/profile")
def get_student_profile(body: RecListBody):
    sess = valid_session(body.token)
    if not sess:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    roster = load_roster()
    student = next((s for s in roster if s["id"] == sess["student_id"]), None)
    if not student:
        return JSONResponse({"error": "Student not found"}, status_code=404)
    return {
        "study_plan": student.get("study_plan"),
        "student_stats": student.get("student_stats"),
        "chat_history": student.get("chat_history", {}),
        "flashcard_deck": student.get("flashcard_deck", [])
    }


# ---------- Teacher Roster Management ----------
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
        if added_courses:
            msg = f"Added course(s) {', '.join(added_courses)} to existing student {existing.get('email')}."
        else:
            msg = f"{existing.get('email')} already had those course(s); nothing to add."
        return {"ok": True, "merged": True, "message": msg, "student": {
            "id": existing["id"], "name": existing.get("name"), "email": existing.get("email"),
            "courses": existing.get("courses", []), "has_password": bool(existing.get("password_hash")),
        }}
    student = {
        "id": secrets.token_hex(6),
        "name": name or email.split("@")[0],
        "email": email,
        "courses": courses,
        "password_hash": hash_pw(body.password) if (body.password or "").strip() else "",
    }
    roster.append(student)
    save_roster(roster)
    return {"ok": True, "merged": False, "student": {
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
    for tok in [t for t, v in SESSIONS.items() if v["student_id"] == body.id]:
        SESSIONS.pop(tok, None)
    return {"ok": True}


class ResetPasswordBody(BaseModel):
    passcode: str
    id: str
    new_password: str | None = None


def _gen_password(n=8):
    alphabet = "abcdefghijkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))


@app.post("/api/teacher/students/reset-password")
def reset_student_password(body: ResetPasswordBody):
    if not check_passcode(body.passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    roster = load_roster()
    student = next((s for s in roster if s["id"] == body.id), None)
    if not student:
        return JSONResponse({"error": "Student not found"}, status_code=404)
    new_pw = (body.new_password or "").strip() or _gen_password()
    student["password_hash"] = hash_pw(new_pw)
    save_roster(roster)
    for tok in [t for t, v in SESSIONS.items() if v["student_id"] == body.id]:
        SESSIONS.pop(tok, None)
    return {"ok": True, "email": student.get("email"), "new_password": new_pw}


class UpdateStudentBody(BaseModel):
    passcode: str
    id: str
    name: str | None = None
    email: str | None = None
    courses: list[str] | None = None
    new_password: str | None = None


@app.post("/api/teacher/students/update")
def update_student(body: UpdateStudentBody):
    if not check_passcode(body.passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    roster = load_roster()
    student = next((s for s in roster if s["id"] == body.id), None)
    if not student:
        return JSONResponse({"error": "Student not found"}, status_code=404)

    if body.email is not None:
        new_email = body.email.strip()
        if not new_email:
            return JSONResponse({"error": "Email can't be empty."}, status_code=400)
        clash = any(_norm(s.get("email")) == _norm(new_email) and s["id"] != body.id for s in roster)
        if clash:
            return JSONResponse({"error": "Another student already uses that email."}, status_code=400)
        student["email"] = new_email

    if body.name is not None:
        student["name"] = body.name.strip() or (student.get("email") or "").split("@")[0]

    if body.courses is not None:
        seen, cleaned = set(), []
        for c in body.courses:
            c = (c or "").strip()
            if c and _norm(c) not in seen:
                cleaned.append(c); seen.add(_norm(c))
        student["courses"] = cleaned

    pw_changed = False
    if body.new_password is not None and body.new_password.strip():
        student["password_hash"] = hash_pw(body.new_password.strip())
        pw_changed = True

    save_roster(roster)
    if pw_changed or body.email is not None:
        for tok in [t for t, v in SESSIONS.items() if v.get("student_id") == body.id]:
            SESSIONS.pop(tok, None)
    return {"ok": True, "student": {
        "id": student["id"], "name": student.get("name", ""), "email": student.get("email", ""),
        "courses": student.get("courses", []), "has_password": bool(student.get("password_hash")),
    }}


# ---------- de-duplicate accounts by email ----------
def _find_email_duplicates(roster):
    groups = {}
    for s in roster:
        key = _norm(s.get("email"))
        if not key:
            continue
        groups.setdefault(key, []).append(s)
    return {k: v for k, v in groups.items() if len(v) > 1}


def _merge_group_courses(entries):
    seen, merged = set(), []
    for e in entries:
        for c in normalize_courses(e.get("courses")):
            if _norm(c) not in seen:
                merged.append(c); seen.add(_norm(c))
    return merged


class DedupeAuth(BaseModel):
    passcode: str


@app.post("/api/teacher/students/dedupe-preview")
def dedupe_preview(body: DedupeAuth):
    if not check_passcode(body.passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    roster = load_roster()
    dups = _find_email_duplicates(roster)
    preview = []
    for email, entries in dups.items():
        keep = entries[0]
        remove = entries[1:]
        preview.append({
            "email": keep.get("email"),
            "duplicate_count": len(entries),
            "keep": {"id": keep["id"], "name": keep.get("name"),
                     "courses": keep.get("courses", []),
                     "has_password": bool(keep.get("password_hash"))},
            "will_delete": [{"id": e["id"], "name": e.get("name"),
                             "courses": e.get("courses", [])} for e in remove],
            "merged_courses": _merge_group_courses(entries),
        })
    return {
        "duplicate_emails": len(dups),
        "accounts_to_delete": sum(len(e) - 1 for e in dups.values()),
        "groups": preview,
    }


@app.post("/api/teacher/students/dedupe-apply")
def dedupe_apply(body: DedupeAuth):
    if not check_passcode(body.passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    roster = load_roster()
    dups = _find_email_duplicates(roster)
    if not dups:
        return {"ok": True, "merged_emails": 0, "deleted_accounts": 0, "message": "No duplicates found."}
    delete_ids = set()
    merged_emails = 0
    for email, entries in dups.items():
        keep = entries[0]
        keep["courses"] = _merge_group_courses(entries)
        if not keep.get("password_hash"):
            for e in entries[1:]:
                if e.get("password_hash"):
                    keep["password_hash"] = e["password_hash"]
                    break
        for e in entries[1:]:
            delete_ids.add(e["id"])
        merged_emails += 1
    new_roster = [s for s in roster if s["id"] not in delete_ids]
    save_roster(new_roster)
    for tok in [t for t, v in SESSIONS.items() if v.get("student_id") in delete_ids]:
        SESSIONS.pop(tok, None)
    return {
        "ok": True,
        "merged_emails": merged_emails,
        "deleted_accounts": len(delete_ids),
        "total_now": len(new_roster),
    }


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
            existing = normalize_courses(s.get("courses"))
            seen = {_norm(c) for c in existing}
            merged = list(existing)
            for c in courses:
                if _norm(c) not in seen:
                    merged.append(c)
                    seen.add(_norm(c))
            s["courses"] = merged
            if name and name != email.split("@")[0]:
                s["name"] = name
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
LOGO_MAX_BYTES = 2 * 1024 * 1024
LOGO_MIME = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp",
             "gif": "image/gif", "svg": "image/svg+xml"}
LOGO_PATH_BASE = os.path.join(DATA_DIR, "logo")


def _current_logo_file():
    for e in set(ALLOWED_LOGO_EXT.values()):
        p = f"{LOGO_PATH_BASE}.{e}"
        if os.path.exists(p):
            return p, e
    return None, None


def _migrate_frontend_logo_to_disk():
    try:
        existing, _ = _current_logo_file()
        if existing:
            return
        for e in set(ALLOWED_LOGO_EXT.values()):
            fe = os.path.join(FRONTEND_DIR, f"logo.{e}")
            if os.path.exists(fe):
                import shutil
                shutil.copy2(fe, f"{LOGO_PATH_BASE}.{e}")
                break
    except Exception as ex:
        print(f"[logo] migrate warning: {ex}")


_migrate_frontend_logo_to_disk()


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
    save_ext = ALLOWED_LOGO_EXT[ext]
    os.makedirs(DATA_DIR, exist_ok=True)
    for e in set(ALLOWED_LOGO_EXT.values()):
        old = f"{LOGO_PATH_BASE}.{e}"
        if os.path.exists(old):
            try:
                os.remove(old)
            except OSError:
                pass
    with open(f"{LOGO_PATH_BASE}.{save_ext}", "wb") as f:
        f.write(data)
    cfg = load_config()
    cfg["logo"] = "/logo"
    cfg["logo_ext"] = save_ext
    save_config(cfg)
    return {"ok": True, "logo": "/logo"}


@app.get("/logo")
def get_logo():
    path, e = _current_logo_file()
    if not path:
        return JSONResponse({"error": "no logo"}, status_code=404)
    return FileResponse(path, media_type=LOGO_MIME.get(e, "application/octet-stream"))


@app.get("/api/branding")
def branding():
    path, _ = _current_logo_file()
    return {"logo": "/logo" if path else ""}


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
    
    idx = await retrieve(rec, body.question)
    ctx = context_from_indices(rec, idx)
    notes_ctx = notes_context(rec, body.question)
    
    lang_line = "\nAlways respond in English, even if the student's question is written in another language."
    notes_rules = ""
    if notes_ctx:
        notes_rules = (
            "\n5. You also have TEACHER NOTES, shown as blocks prefixed with [NOTE: filename]. "
            "These are extra study material for this class. You MAY use them to answer.\n"
            "6. When you use information from the notes, quote the relevant part in \"quotation marks\" "
            "and attribute it, e.g. According to the class notes: \"...\".\n"
            "7. NEVER reproduce a note in full or dump large portions verbatim — quote only the parts "
            "directly relevant to the question. The notes are not downloadable by students."
        )

    course_name = rec.get('unit') or "Unassigned Course"

    system = (
        f"You are an expert Biology tutor for Cambridge IGCSE, AS/A Level, and Pearson Edexcel. "
        f"You are currently answering a question for a student in the course: '{course_name}'. "
        "Your ONLY goal is to help students learn and review concepts taught in the provided class recording and teacher notes.\n\n"
        "GUIDELINES & FLEXIBILITY:\n"
        "1. INTENT RECOGNITION: Be flexible and conversational. If the student asks for a 'summary', 'overview', "
        "'explain [topic]', or 'what was covered', use the provided transcript excerpts and teacher notes to give a "
        "clear, helpful overview of the class contents, even if they didn't use specific keywords.\n"
        "2. STRICT GROUNDING: Base your explanations *exclusively* on the provided class recording excerpts and teacher notes. "
        "Do NOT invent outside biological facts or syllabus details. If a specific biological concept or question is "
        "completely absent from both the transcript and notes, reply: 'This topic wasn't covered in this specific class or the attached notes.'\n"
        "3. EXAM BOARD ACCURACY: Use the exact terminology, mark scheme phrasing, and conventions found in the provided text. "
        "Never mix Cambridge and Edexcel terminology.\n"
        "4. CITATIONS: When answering specific conceptual questions, cite your source by including the timestamp in parentheses, e.g. (at 12:34).\n"
        "5. NO HALLUCINATION: Do not invent, infer, or guess unmentioned facts."
        + notes_rules
        + lang_line
    )

    notes_block = f"\n\nTeacher notes for this class:\n{notes_ctx}" if notes_ctx else ""
    user = (
        f"Course: {course_name}\n"
        f"Class recording: {rec.get('display_title') or rec.get('topic')}\n\n"
        f"Transcript excerpts:\n{ctx}"
        f"{notes_block}\n\n"
        f"Student question: {body.question}"
    )
    
    try:
        answer = await llm(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=1000,
        )
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
    segs = rec.get("segments", [])
    step = max(1, len(segs) // 60)
    idx = list(range(0, len(segs), step))
    ctx = context_from_indices(rec, idx, max_chars=60000)
    lang_line = "Write the quiz in English."
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


# ---------- automated flashcards generation (Fresh & Unique Cards) ----------
class FlashcardBody(BaseModel):
    recording_id: str
    existing_fronts: list[str] | None = []
    token: str | None = None


@app.post("/api/flashcards")
async def generate_flashcards(body: FlashcardBody):
    sess = valid_session(body.token)
    if not sess:
        return JSONResponse({"error": "Your session has expired. Please log in again."}, status_code=401)
    
    rec = REC_BY_ID.get(body.recording_id)
    if not rec:
        return JSONResponse({"error": "Recording not found"}, status_code=404)

    transcript_text = "\n".join([f"[{s.get('timestamp','')}] {s.get('text','')}" for s in rec.get("segments", [])])
    notes_text = notes_context(rec, "flashcards review summary", max_chars=8000)
    
    avoid_block = ""
    if body.existing_fronts and len(body.existing_fronts) > 0:
        avoid_block = "\nAVOID REPEATING these existing question concepts:\n" + "\n".join([f"- {f}" for f in body.existing_fronts[:15]])

    system = (
        "You are an expert Biology and science tutor. Based on the following class transcript and teacher notes, "
        "generate 5 to 7 fresh, high-yield flashcards for active recall study. "
        "Focus on varied definitions, processes, comparisons, and mechanisms. "
        "Return STRICT JSON only, no markdown, no prose. "
        "Schema: {\"flashcards\":[{\"front\":str,\"back\":str}]}."
        + avoid_block
    )
    
    user = (
        f"Class recording: {rec.get('display_title') or rec.get('topic')}\n\n"
        f"TRANSCRIPT:\n{transcript_text[:12000]}\n\n"
        f"TEACHER NOTES:\n{notes_text[:4000]}"
    )
    
    try:
        raw = await llm(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=1500,
            temperature=0.7,
        )
    except (LLMConfigError, LLMUpstreamError) as e:
        return JSONResponse({"error": str(e)}, status_code=503)

    data = None
    txt = (raw or "").strip()
    if txt.startswith("```"):
        txt = txt.strip("`")
        if "\n" in txt:
            txt = txt.split("\n", 1)[-1]
            
    try:
        start = txt.find("{")
        end = txt.rfind("}")
        if start != -1 and end != -1:
            data = json.loads(txt[start:end+1])
    except Exception:
        data = None
                
    if not data or "flashcards" not in data:
        return JSONResponse({"error": "Could not generate flashcards.", "raw": raw[:500]}, status_code=500)
        
    return data


# ---------- study plan generation (100% Mandatory Coverage & Dynamic Allocation) ----------
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

    num_classes = len(body.recording_ids)
    total_available_mins = int(body.days * body.hours_per_day * 60)
    
    target_review_mins = max(20, min(60, int((total_available_mins * 0.6) / num_classes)))

    selected_recs = []
    for rid in body.recording_ids:
        rec = REC_BY_ID.get(rid)
        if rec:
            title = rec.get('display_title') or rec.get('topic') or "Class"
            unit = rec.get('unit') or "General"
            selected_recs.append(f"- [{unit}] {title} (Target review: ~{target_review_mins} mins)")

    if not selected_recs:
        return JSONResponse({"error": "Selected recordings not found."}, status_code=404)

    recs_text = "\n".join(selected_recs)
    
    system = (
        "You are an expert academic coach for Biology students. "
        "Your task is to create a structured, day-by-day study schedule based on the student's constraints.\n\n"
        "STRICT MANDATORY RULES:\n"
        f"1. TOTAL COVERAGE (CRITICAL): You MUST schedule EVERY SINGLE ONE of the {num_classes} classes provided below across the {body.days} days. Do NOT skip or omit any class.\n"
        f"2. TIME BUDGET: Each day has approximately {body.hours_per_day} hours ({int(body.hours_per_day * 60)} minutes). Distribute tasks evenly so daily task times sum to ~{int(body.hours_per_day * 60)} minutes.\n"
        "3. FOCUS MODE SPECIALIZATION:\n"
        "   - 'First-time learning': Dedicate more time to thorough topic review, process understanding, and notes consolidation.\n"
        "   - 'Reviewing and memorizing definitions': Pair class reviews with active recall tasks, flashcards, and keyword drills.\n"
        "   - 'Past paper and exam practice': Pair class reviews with exam question practice, command word checks, and mark scheme alignment.\n"
        "4. Return STRICT JSON only.\n"
        "Schema: {\"plan\":[{\"day\":int,\"quote\":str,\"tasks\":[{\"title\":str,\"description\":str,\"est_minutes\":int}]}]}"
    )
    
    user = (
        f"Generate a {body.days}-day plan for {body.hours_per_day} hours/day (Total budget: {total_available_mins} mins).\n"
        f"Study Focus: {body.focus}\n"
        f"Classes to cover ({num_classes} total - ALL MUST BE INCLUDED):\n{recs_text}"
    )

    try:
        raw = await llm(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=3000,
            temperature=0.3,
        )
    except (LLMConfigError, LLMUpstreamError) as e:
        return JSONResponse({"error": str(e)}, status_code=503)

    data = None
    txt = (raw or "").strip()
    if txt.startswith("```"):
        txt = txt.strip("`")
        if "\n" in txt:
            txt = txt.split("\n", 1)[-1]
            
    try:
        start = txt.find("{")
        end = txt.rfind("}")
        if start != -1 and end != -1:
            data = json.loads(txt[start:end+1])
    except Exception as e:
        print(f"[Study Plan Error] Could not parse JSON: {e}")
                
    if not data or "plan" not in data:
        return JSONResponse({"error": "Could not generate the plan. Please try again.", "raw": raw[:500]}, status_code=500)
        
    return data


# ---------- Zoom webhook ----------
@app.post("/api/zoom/webhook")
async def zoom_webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    payload = await request.json()
    
    if payload.get("event") == "endpoint.url_validation":
        plain = payload["payload"]["plainToken"]
        sig = hmac.new(ZOOM_WEBHOOK_SECRET.encode(), plain.encode(), hashlib.sha256).hexdigest()
        return {"plainToken": plain, "encryptedToken": sig}
        
    ts = request.headers.get("x-zm-request-timestamp", "")
    got = request.headers.get("x-zm-signature", "")
    message = f"v0:{ts}:{body.decode('utf-8')}".encode()
    expected = "v0=" + hmac.new(ZOOM_WEBHOOK_SECRET.encode(), message, hashlib.sha256).hexdigest()
    
    if not hmac.compare_digest(expected, got):
        return JSONResponse({"error": "bad signature"}, status_code=401)
        
    event = payload.get("event", "")
    print(f"[zoom webhook] received event: {event}")
    
    recording_events = {
        "recording.completed",
        "recording.transcript_completed",
        "webinar.recording_completed",
        "webinar.recording_transcript_completed",
    }
    
    is_recording_event = (
        event in recording_events
        or ("recording" in event and ("completed" in event or "transcript" in event))
    )
    
    if is_recording_event:
        p_load = payload.get("payload", {})
        obj = p_load.get("object", {}) or p_load.get("webinar", {})
        
        if not obj.get("id") and not obj.get("uuid"):
            obj = p_load.get("object", {})
            
        if obj.get("id") or obj.get("uuid"):
            background_tasks.add_task(ingest_zoom_meeting, obj)
            print(f"[zoom webhook] queued background ingest for webinar/meeting: '{obj.get('topic')}'")
        else:
            print(f"[zoom webhook warning] could not extract meeting/webinar ID from payload: {payload}")
        
    return {"ok": True}


class BackfillBody(BaseModel):
    passcode: str
    from_date: str | None = None
    to_date: str | None = None


async def _list_cloud_recordings(from_date: str, to_date: str):
    import httpx
    from datetime import datetime, timedelta

    token = await zoom_token()
    results = []
    start = datetime.strptime(from_date, "%Y-%m-%d")
    end = datetime.strptime(to_date, "%Y-%m-%d")
    async with httpx.AsyncClient(timeout=60) as client:
        window_start = start
        while window_start <= end:
            window_end = min(window_start + timedelta(days=29), end)
            next_token = ""
            while True:
                params = {
                    "from": window_start.strftime("%Y-%m-%d"),
                    "to": window_end.strftime("%Y-%m-%d"),
                    "page_size": 300,
                }
                if next_token:
                    params["next_page_token"] = next_token
                r = await client.get(
                    "https://api.zoom.us/v2/users/me/recordings",
                    headers={"Authorization": f"Bearer {token}"},
                    params=params,
                )
                if r.status_code != 200:
                    break
                data = r.json()
                results.extend(data.get("meetings", []))
                next_token = data.get("next_page_token") or ""
                if not next_token:
                    break
            window_start = window_end + timedelta(days=1)
    return results


@app.post("/api/teacher/backfill")
async def teacher_backfill(body: BackfillBody):
    if not check_passcode(body.passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    from datetime import datetime, timedelta
    to_date = body.to_date or datetime.utcnow().strftime("%Y-%m-%d")
    from_date = body.from_date or (datetime.utcnow() - timedelta(days=180)).strftime("%Y-%m-%d")
    try:
        meetings = await _list_cloud_recordings(from_date, to_date)
    except Exception as e:
        return JSONResponse({"error": f"Could not list cloud recordings: {e}"}, status_code=502)
    added = 0
    skipped = 0
    errors = 0
    details = []
    for m in meetings:
        try:
            was_added = await ingest_zoom_meeting(m, allow_whisper_fallback=False)
            if was_added:
                added += 1
                details.append({
                    "topic": m.get("topic"),
                    "date": (m.get("start_time") or "")[:10],
                    "source": _detect_source(m),
                })
            else:
                skipped += 1
        except Exception:
            errors += 1
    return {
        "ok": True,
        "range": {"from": from_date, "to": to_date},
        "found": len(meetings),
        "added": added,
        "skipped_already_present": skipped,
        "errors": errors,
        "added_recordings": details,
        "total_recordings_now": len(RECORDINGS),
    }


class ImportOneBody(BaseModel):
    passcode: str
    ref: str


@app.post("/api/teacher/import-one")
async def teacher_import_one(body: ImportOneBody):
    if not check_passcode(body.passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    meeting_id = _parse_meeting_id(body.ref)
    if not meeting_id:
        return JSONResponse(
            {"error": "Couldn't read a meeting ID from that. Paste the Zoom Meeting ID/UUID, or a recording link."},
            status_code=400,
        )
    try:
        obj = await fetch_zoom_recording_object(meeting_id)
    except LLMUpstreamError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    except Exception as e:
        return JSONResponse({"error": f"Could not fetch that recording: {e}"}, status_code=502)
    
    existing_id = str(obj.get("id") or obj.get("uuid") or meeting_id)
    if existing_id in REC_BY_ID:
        return JSONResponse(
            {"error": f"That recording is already imported: \"{REC_BY_ID[existing_id].get('display_title')}\"."},
            status_code=409,
        )
    try:
        added = await ingest_zoom_meeting(obj, allow_whisper_fallback=False)
    except Exception as e:
        return JSONResponse({"error": f"Import failed: {e}"}, status_code=500)
    if not added:
        return JSONResponse({"error": "That recording is already imported."}, status_code=409)
    rec = REC_BY_ID.get(existing_id)
    return {
        "ok": True,
        "recording": _card(rec, include_hidden=True) if rec else None,
        "has_transcript": bool(rec and rec.get("segments")),
        "total_recordings_now": len(RECORDINGS),
    }


NOTE_MAX_UPLOAD_BYTES = 60 * 1024 * 1024
NOTE_MAX_TEXT_CHARS = 2 * 1024 * 1024


@app.post("/api/teacher/notes/upload")
async def upload_note(passcode: str = Form(...), id: str = Form(...), file: UploadFile = File(...)):
    if not check_passcode(passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    rec = REC_BY_ID.get(id)
    if not rec:
        return JSONResponse({"error": "not found"}, status_code=404)
    data = await file.read()
    if len(data) > NOTE_MAX_UPLOAD_BYTES:
        return JSONResponse({"error": "File is unusually large."}, status_code=400)
    try:
        text = extract_text_from_upload(data, file.filename or "")
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"Could not read that file: {e}"}, status_code=422)
    
    original_len = len(text)
    trimmed = False
    if original_len > NOTE_MAX_TEXT_CHARS:
        text = text[:NOTE_MAX_TEXT_CHARS]
        trimmed = True
    chunks = chunk_note_text(text)
    if not chunks:
        return JSONResponse({"error": "No readable text found in that file."}, status_code=422)
    kept = sum(len(c) for c in chunks)
    
    lib = load_notes_library()
    note = {"id": secrets.token_hex(6), "filename": (file.filename or "notes"),
            "chunks": chunks, "chars": kept}
    lib.append(note)
    save_notes_library(lib)
    ids = list(rec.get("note_ids") or [])
    if note["id"] not in ids:
        ids.append(note["id"])
    rec["note_ids"] = ids
    save_recordings(RECORDINGS)
    return {"ok": True, "id": id,
            "note": {"id": note["id"], "filename": note["filename"], "chars": kept, "chunks": len(chunks)},
            "file_bytes": len(data), "text_chars": kept, "trimmed": trimmed,
            "recording": _card(rec, include_hidden=True)}


class ListLibraryBody(BaseModel):
    passcode: str
    for_recording: str | None = None


@app.post("/api/teacher/notes/library")
def notes_library(body: ListLibraryBody):
    if not check_passcode(body.passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    lib = load_notes_library()
    usage = {}
    for r in RECORDINGS:
        for nid in (r.get("note_ids") or []):
            usage[nid] = usage.get(nid, 0) + 1

    allowed_ids = None
    if body.for_recording:
        target = REC_BY_ID.get(body.for_recording)
        target_unit = (target.get("unit") or "Unassigned") if target else "Unassigned"
        if target_unit != "Unassigned":
            allowed_ids = set()
            for r in RECORDINGS:
                if (r.get("unit") or "Unassigned") == target_unit:
                    for nid in (r.get("note_ids") or []):
                        allowed_ids.add(nid)

    out = []
    for n in lib:
        if allowed_ids is not None and n["id"] not in allowed_ids:
            continue
        out.append({"id": n["id"], "filename": n.get("filename"),
                    "chars": n.get("chars", sum(len(c) for c in n.get("chunks", []))),
                    "used_by": usage.get(n["id"], 0)})
    return {"library": out}


class AttachNoteBody(BaseModel):
    passcode: str
    id: str
    note_id: str


@app.post("/api/teacher/notes/attach")
def attach_note(body: AttachNoteBody):
    if not check_passcode(body.passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    rec = REC_BY_ID.get(body.id)
    if not rec:
        return JSONResponse({"error": "recording not found"}, status_code=404)
    if not note_by_id(body.note_id):
        return JSONResponse({"error": "note not found in library"}, status_code=404)
    ids = list(rec.get("note_ids") or [])
    if body.note_id in ids:
        return JSONResponse({"error": "That note is already attached to this recording."}, status_code=409)
    ids.append(body.note_id)
    rec["note_ids"] = ids
    save_recordings(RECORDINGS)
    return {"ok": True, "recording": _card(rec, include_hidden=True)}


class DetachNoteBody(BaseModel):
    passcode: str
    id: str
    note_id: str


@app.post("/api/teacher/notes/detach")
def detach_note(body: DetachNoteBody):
    if not check_passcode(body.passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    rec = REC_BY_ID.get(body.id)
    if not rec:
        return JSONResponse({"error": "not found"}, status_code=404)
    ids = list(rec.get("note_ids") or [])
    if body.note_id not in ids:
        return JSONResponse({"error": "note not attached"}, status_code=404)
    rec["note_ids"] = [x for x in ids if x != body.note_id]
    save_recordings(RECORDINGS)
    return {"ok": True, "recording": _card(rec, include_hidden=True)}


class DeleteLibraryNoteBody(BaseModel):
    passcode: str
    note_id: str


@app.post("/api/teacher/notes/library/delete")
def delete_library_note(body: DeleteLibraryNoteBody):
    if not check_passcode(body.passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    lib = load_notes_library()
    if not note_by_id(body.note_id, lib):
        return JSONResponse({"error": "note not found"}, status_code=404)
    lib = [n for n in lib if n["id"] != body.note_id]
    save_notes_library(lib)
    detached_from = 0
    for r in RECORDINGS:
        ids = r.get("note_ids") or []
        if body.note_id in ids:
            r["note_ids"] = [x for x in ids if x != body.note_id]
            detached_from += 1
    if detached_from:
        save_recordings(RECORDINGS)
    return {"ok": True, "detached_from": detached_from}


class DeleteNoteBody(BaseModel):
    passcode: str
    id: str
    note_id: str


@app.post("/api/teacher/notes/delete")
def delete_note(body: DeleteNoteBody):
    if not check_passcode(body.passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    rec = REC_BY_ID.get(body.id)
    if not rec:
        return JSONResponse({"error": "not found"}, status_code=404)
    ids = list(rec.get("note_ids") or [])
    if body.note_id not in ids:
        return JSONResponse({"error": "note not found"}, status_code=404)
    rec["note_ids"] = [x for x in ids if x != body.note_id]
    save_recordings(RECORDINGS)
    return {"ok": True, "recording": _card(rec, include_hidden=True)}


class TranscribeBody(BaseModel):
    passcode: str
    id: str


@app.post("/api/teacher/transcribe")
async def teacher_transcribe(body: TranscribeBody):
    if not check_passcode(body.passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    rec = REC_BY_ID.get(body.id)
    if not rec:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        count = await transcribe_recording_by_id(body.id)
    except LLMConfigError as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    except LLMUpstreamError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    except Exception as e:
        return JSONResponse({"error": f"Transcription failed: {e}"}, status_code=500)
    if count == 0:
        return JSONResponse({"error": "Transcription produced no text."}, status_code=422)
    return {"ok": True, "id": body.id, "segments": count, "recording": _card(rec, include_hidden=True)}


def _remove_recording(rid: str) -> bool:
    global RECORDINGS
    rec = REC_BY_ID.get(rid)
    if not rec:
        return False
    RECORDINGS = [r for r in RECORDINGS if r.get("id") != rid]
    REC_BY_ID.pop(rid, None)
    return True


class DeleteRecBody(BaseModel):
    passcode: str
    id: str


@app.post("/api/teacher/recordings/delete")
def teacher_delete_recording(body: DeleteRecBody):
    if not check_passcode(body.passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not _remove_recording(body.id):
        return JSONResponse({"error": "not found"}, status_code=404)
    save_recordings(RECORDINGS)
    return {"ok": True, "id": body.id, "total_recordings_now": len(RECORDINGS)}


class DeleteUnassignedBody(BaseModel):
    passcode: str


@app.post("/api/teacher/recordings/delete-unassigned")
def teacher_delete_unassigned(body: DeleteUnassignedBody):
    if not check_passcode(body.passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    targets = [r["id"] for r in RECORDINGS if (r.get("unit") or "Unassigned") == "Unassigned"]
    for rid in targets:
        _remove_recording(rid)
    if targets:
        save_recordings(RECORDINGS)
    return {"ok": True, "deleted": len(targets), "total_recordings_now": len(RECORDINGS)}


async def generate_summary_and_topics(rec):
    segs = rec.get("segments") or []
    if not segs:
        return None
    idx = await retrieve(rec, rec.get("display_title") or rec.get("topic") or "lecture", k=30, window=1)
    context = context_from_indices(rec, idx, max_chars=40000)
    system = (
        "You summarize a class recording for students. Use ONLY the transcript. "
        "Always write in English. "
        "Return STRICT JSON: {\"summary\": string (2-4 sentences), "
        "\"topics\": string[] (4-8 short topic tags, each 1-4 words)}. No markdown, no extra text."
    )
    raw = await llm(
        [{"role": "system", "content": system},
         {"role": "user", "content": f"Transcript excerpts:\n{context}"}],
        max_tokens=500, temperature=0.2,
    )
    import json as _json
    txt = (raw or "").strip()
    if txt.startswith("```"):
        txt = txt.strip("`")
        txt = txt.split("\n", 1)[-1] if "\n" in txt else txt
    try:
        data = _json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
    except Exception:
        data = {"summary": txt[:400], "topics": []}
    rec["summary"] = (data.get("summary") or "").strip()
    rec["topics"] = [t.strip() for t in (data.get("topics") or []) if t.strip()][:8]
    return rec


class SummaryBody(BaseModel):
    passcode: str
    id: str


@app.post("/api/teacher/summary")
async def teacher_summary(body: SummaryBody):
    if not check_passcode(body.passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    rec = REC_BY_ID.get(body.id)
    if not rec:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not rec.get("segments"):
        return JSONResponse({"error": "This recording has no transcript yet."}, status_code=422)
    try:
        await generate_summary_and_topics(rec)
    except LLMConfigError as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    except Exception as e:
        return JSONResponse({"error": f"Could not generate summary: {e}"}, status_code=500)
    save_recordings(RECORDINGS)
    return {"ok": True, "id": body.id, "summary": rec.get("summary", ""), "topics": rec.get("topics", [])}


@app.post("/api/teacher/stats")
def teacher_stats(body: TeacherAuth):
    if not check_passcode(body.passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    from datetime import datetime, timedelta
    roster = load_roster()
    log = load_qlog()
    total = len(RECORDINGS)
    transcribed = sum(1 for r in RECORDINGS if r.get("segments"))
    visible = sum(1 for r in RECORDINGS if r.get("visible", True))
    unassigned = sum(1 for r in RECORDINGS if (r.get("unit") or "Unassigned") == "Unassigned")
    week_ago = datetime.utcnow() - timedelta(days=7)
    q_week = 0
    for q in log:
        try:
            if datetime.strptime((q.get("time") or "")[:10], "%Y-%m-%d") >= week_ago:
                q_week += 1
        except Exception:
            pass
    courses = len({(r.get("unit") or "Unassigned") for r in RECORDINGS})
    return {
        "recordings_total": total,
        "recordings_transcribed": transcribed,
        "recordings_missing": total - transcribed,
        "recordings_visible": visible,
        "recordings_unassigned": unassigned,
        "courses": courses,
        "students": len(roster),
        "questions_total": len(log),
        "questions_this_week": q_week,
    }


_STOPWORDS = set("the a an and or of to in is are was were be been what how why when where "
                 "which who whom this that these those i you he she it we they for on at by "
                 "with about from as do does did can could would should will shall may might "
                 "not no yes please tell me my your our their his her its me can't cant explain "
                 "give show list define describe difference between them then than".split())


@app.post("/api/teacher/analytics")
def teacher_analytics(body: TeacherAuth):
    if not check_passcode(body.passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    log = load_qlog()
    kw = Counter()
    per_student = Counter()
    per_course = Counter()
    per_day = Counter()
    for q in log:
        for w in tokenize(q.get("question", "")):
            if len(w) > 2 and w not in _STOPWORDS:
                kw[w] += 1
        per_student[q.get("student") or "Unknown"] += 1
        per_course[q.get("unit") or "Unassigned"] += 1
        d = (q.get("time") or "")[:10]
        if d:
            per_day[d] += 1
    return {
        "total": len(log),
        "top_keywords": kw.most_common(15),
        "top_students": per_student.most_common(10),
        "by_course": per_course.most_common(20),
        "by_day": sorted(per_day.items()),
    }


@app.get("/api/teacher/export/questions.csv")
def export_questions_csv(passcode: str = Query(...)):
    if not check_passcode(passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    import csv
    log = load_qlog()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Time", "Student", "Recording", "Unit", "Question"])
    for q in reversed(log):
        w.writerow([q.get("time", ""), q.get("student", ""), q.get("recording_title", ""),
                    q.get("unit", ""), q.get("question", "")])
    data = buf.getvalue().encode("utf-8-sig")
    from fastapi.responses import Response
    return Response(content=data, media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=questions.csv"})


@app.get("/api/teacher/export/roster.csv")
def export_roster_csv(passcode: str = Query(...)):
    if not check_passcode(passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    import csv
    roster = load_roster()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Name", "Email", "Courses"])
    for s in roster:
        w.writerow([s.get("name", ""), s.get("email", ""), ", ".join(s.get("courses", []) or [])])
    data = buf.getvalue().encode("utf-8-sig")
    from fastapi.responses import Response
    return Response(content=data, media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=roster.csv"})


@app.get("/api/teacher/export/questions.pdf")
def export_questions_pdf(passcode: str = Query(...)):
    if not check_passcode(passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    from fastapi.responses import Response
    from datetime import datetime
    log = load_qlog()
    lines = [f"NG-ClassMate — Student Questions Report",
             f"Generated {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC",
             f"Total questions: {len(log)}", ""]
    for q in reversed(log):
        lines.append(f"{q.get('time','')}  |  {q.get('student','')}  |  {q.get('unit','')}")
        lines.append(f"  Q: {q.get('question','')}")
        lines.append(f"  Recording: {q.get('recording_title','')}")
        lines.append("")
    pdf_bytes = _simple_text_pdf(lines)
    return Response(content=pdf_bytes, media_type="application/pdf",
                    headers={"Content-Disposition": "attachment; filename=questions.pdf"})


def _simple_text_pdf(lines):
    def esc(s):
        return (s or "").replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    per_page = 48
    pages = [lines[i:i + per_page] for i in range(0, max(1, len(lines)), per_page)] or [[""]]
    objs = []
    n_pages = len(pages)
    font_obj = 3 + n_pages * 2
    kids = []
    body_objs = {}
    obj_num = 3
    content_nums = []
    page_nums = []
    for pi, pg in enumerate(pages):
        page_no = obj_num; obj_num += 1
        content_no = obj_num; obj_num += 1
        page_nums.append(page_no); content_nums.append(content_no)
    font_no = obj_num
    for pi, pg in enumerate(pages):
        text_cmds = ["BT", "/F1 10 Tf", "12 TL", "40 800 Td"]
        for ln in pg:
            text_cmds.append(f"({esc(ln)[:180]}) Tj")
            text_cmds.append("T*")
        text_cmds.append("ET")
        stream = "\n".join(text_cmds)
        body_objs[content_nums[pi]] = f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"
        body_objs[page_nums[pi]] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_no} 0 R >> >> /Contents {content_nums[pi]} 0 R >>"
        )
    body_objs[font_no] = "<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>"
    kids_str = " ".join(f"{pn} 0 R" for pn in page_nums)
    body_objs[1] = "<< /Type /Catalog /Pages 2 0 R >>"
    body_objs[2] = f"<< /Type /Pages /Kids [{kids_str}] /Count {n_pages} >>"
    out = "%PDF-1.4\n"
    offsets = {}
    for num in sorted(body_objs):
        offsets[num] = len(out.encode("latin-1", "replace"))
        out += f"{num} 0 obj\n{body_objs[num]}\nendobj\n"
    xref_pos = len(out.encode("latin-1", "replace"))
    max_num = max(body_objs)
    out += f"xref\n0 {max_num + 1}\n0000000000 65535 f \n"
    for num in range(1, max_num + 1):
        out += f"{offsets.get(num, 0):010d} 00000 n \n"
    out += f"trailer\n<< /Size {max_num + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF"
    return out.encode("latin-1", "replace")


@app.get("/api/health")
def health():
    return {"status": "ok", "recordings": len(RECORDINGS)}


if os.path.isdir(FRONTEND_DIR):
    @app.get("/")
    def index():
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
    app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="static")
