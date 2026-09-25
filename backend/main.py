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
import asyncio
from collections import Counter
from fastapi import FastAPI, UploadFile, File, Form, Request, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import secrets
import bcrypt
import uuid
from datetime import date

# Data location.
#   * Default: the repo's bundled ./data folder.
#   * On a host with a persistent disk (e.g. Render Starter + a mounted disk),
#     set DATA_DIR=/var/data so recordings, roster, config, question log and the
#     uploaded logo survive restarts and redeploys.
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

# Deployment-safe frontend discovery. Supports the original backend/frontend
# layout, a root-level frontend folder, or a single-folder deployment where
# index.html and app.js sit beside main.py.
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_FRONTEND_CANDIDATES = [
    os.path.join(_BASE_DIR, "..", "frontend"),
    os.path.join(_BASE_DIR, "frontend"),
    _BASE_DIR,
]
FRONTEND_DIR = next(
    (p for p in _FRONTEND_CANDIDATES if os.path.isfile(os.path.join(p, "index.html"))),
    os.path.join(_BASE_DIR, "..", "frontend"),
)
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


def lexical_retrieve_indices(rec, query, k=15, window=1):
    """Fast local fallback when embedding search is unavailable or fails."""
    segs = rec.get("segments", []) or []
    if not segs:
        return []
    q_tokens = [w for w in tokenize(query) if len(w) > 2]
    if not q_tokens:
        return list(range(min(k, len(segs))))
    q = Counter(q_tokens)
    scored = []
    for i, seg in enumerate(segs):
        text = tokenize(seg.get("text", ""))
        if not text:
            continue
        tf = Counter(text)
        overlap = sum(min(q[w], tf.get(w, 0)) for w in q)
        if overlap:
            # Reward multi-word coverage and exact phrase occurrence.
            phrase_bonus = 0.0
            raw = str(seg.get("text", "")).lower()
            q_raw = str(query or "").strip().lower()
            if q_raw and len(q_raw) > 4 and q_raw in raw:
                phrase_bonus = 3.0
            scored.append((overlap + phrase_bonus, i))
    scored.sort(key=lambda x: (-x[0], x[1]))
    top = [i for _, i in scored[:k]]
    if not top:
        return []
    chosen = set()
    for i in top:
        for j in range(max(0, i - window), min(len(segs), i + window + 1)):
            chosen.add(j)
    return sorted(chosen)


async def retrieve(rec, query, k=15, window=1):
    """Robust transcript retrieval for AI Tutor. Prefer fast local matching and use
    semantic embeddings opportunistically with an 8-second timeout so a slow embedding
    service can never make the Ask button appear stuck."""
    segs = rec.get("segments", []) or []
    if not segs:
        return []

    lexical = lexical_retrieve_indices(rec, query, k=k, window=window)

    async def _semantic():
        doc_embeddings = await build_index_async(rec)
        q_embedding = await get_embedding(query)
        scores = [cosine_similarity(q_embedding, doc_emb) for doc_emb in doc_embeddings]
        ranked = sorted(range(len(segs)), key=lambda i: scores[i], reverse=True)
        top = [i for i in ranked if scores[i] > 0.3][:k]
        if not top:
            return []
        chosen = set()
        for i in top:
            for j in range(max(0, i - window), min(len(segs), i + window + 1)):
                chosen.add(j)
        return sorted(chosen)

    try:
        semantic = await asyncio.wait_for(_semantic(), timeout=8.0)
        if semantic:
            return semantic
    except Exception as e:
        print(f"[retrieve] semantic search unavailable/slow; using local retrieval: {e}")

    return lexical

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


def context_from_indices(rec, indices, max_chars=18000):
    """Optimized context length (18k chars) to prevent 429 Token-Per-Minute rate limits."""
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


# ---------- LLM helper with Exponential Backoff Retries on 429 ----------
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").strip()


class LLMConfigError(Exception):
    pass


class LLMUpstreamError(Exception):
    pass


async def llm(messages, max_tokens=1200, temperature=0.1, max_retries=4):
    if OPENAI_API_KEY:
        import httpx
        timeout = httpx.Timeout(90.0, connect=10.0)
        
        for attempt in range(max_retries):
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
                
                # If rate limited (429), automatically wait and retry
                if resp.status_code == 429 and attempt < max_retries - 1:
                    wait_time = 1.5 * (attempt + 1)
                    print(f"[OpenAI 429 Rate Limit] Retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})...")
                    await asyncio.sleep(wait_time)
                    continue

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

            except httpx.RequestError as e:
                if attempt == max_retries - 1:
                    raise LLMUpstreamError(f"Could not reach AI provider: {e}") from e
                await asyncio.sleep(1.0 * (attempt + 1))

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
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        r = await client.get(auth_url, headers=headers)
        if r.status_code == 200:
            text = r.text.strip()
            if "WEBVTT" in text or "-->" in text:
                return text
        r2 = await client.get(url, headers=headers)
        if r2.status_code == 200:
            text2 = r2.text.strip()
            if "WEBVTT" in text2 or "-->" in text2:
                return text2
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
    uuid = obj.get("uuid")
    mid = obj.get("id")
    meeting_id = str(uuid or mid or secrets.token_hex(6))
    numeric_id = str(mid) if mid else ""
    
    existing = REC_BY_ID.get(meeting_id) or (REC_BY_ID.get(numeric_id) if numeric_id else None)
    
    if existing and len(existing.get("segments", [])) > 0:
        return False

    topic = obj.get("topic", "Untitled class")
    start_time = (obj.get("start_time") or "")[:10]
    source = _detect_source(obj)
    files = obj.get("recording_files", [])

    if not files and (mid or uuid):
        try:
            files = await fetch_zoom_recording_files(mid or uuid)
        except Exception:
            files = []
    
    transcript = next(
        (f for f in files if (f.get("file_type") or "").upper() in ("TRANSCRIPT", "AUDIO_TRANSCRIPT", "CC") 
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

    if existing:
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
                sep = "&" if "?" in audio["download_url"] else "?"
                audio_url = f"{audio['download_url']}{sep}access_token={token}"
                async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=15.0), follow_redirects=True) as client:
                    ar = await client.get(audio_url, headers={"Authorization": f"Bearer {token}"})
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
    if numeric_id:
        REC_BY_ID[numeric_id] = new_rec
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
check_teacher = check_passcode

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
    ctx = context_from_indices(rec, idx, max_chars=18000)
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
    ctx = context_from_indices(rec, idx, max_chars=20000)
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
        # Give the model enough room for multi-day plans, especially when many classes are selected.
        raw = await llm(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=5000,
            temperature=0.2,
        )
    except (LLMConfigError, LLMUpstreamError) as e:
        return JSONResponse({"error": str(e)}, status_code=503)

    def parse_plan_json(raw_text: str):
        txt = (raw_text or "").strip()
        # Remove common markdown fences without assuming the exact fence language.
        if txt.startswith("```"):
            parts = txt.split("\n", 1)
            txt = parts[1] if len(parts) == 2 else txt.strip("`")
            if txt.endswith("```"):
                txt = txt[:-3].rstrip()

        # First try the whole response, then the largest JSON object in it.
        candidates = [txt]
        start, end = txt.find("{"), txt.rfind("}")
        if start >= 0 and end > start:
            candidates.append(txt[start:end + 1])

        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict) and isinstance(parsed.get("plan"), list):
                    return parsed
            except Exception:
                pass

        # Repair a common LLM formatting issue: trailing commas before ] or }.
        repaired = re.sub(r",\s*([}\]])", r"\1", txt)
        try:
            parsed = json.loads(repaired)
            if isinstance(parsed, dict) and isinstance(parsed.get("plan"), list):
                return parsed
        except Exception:
            pass
        return None

    data = parse_plan_json(raw)

    if not data:
        # Reliable non-AI fallback: never leave the student without a usable plan
        # just because the model returned malformed/truncated JSON.
        fallback = []
        daily_minutes = max(1, int(total_available_mins / max(1, body.days)))
        class_minutes = max(20, min(60, int(total_available_mins / max(1, num_classes))))
        focus_desc = {
            "First-time learning": "Review the lesson carefully, consolidate notes, and explain the key concepts in your own words.",
            "Reviewing and memorizing definitions": "Use active recall, definition drills, and flashcards to test the key terminology.",
            "Past paper and exam practice": "Review the lesson, then practise exam-style questions and check your wording against the mark scheme."
        }.get(body.focus, "Review the class and actively recall the key learning points.")

        for day_num in range(1, body.days + 1):
            day_tasks = []
            for idx, rec in enumerate(selected_recs):
                # Spread classes across the available days.
                if (idx % body.days) + 1 != day_num:
                    continue
                raw_title = rec.split("] ", 1)[-1].rsplit(" (Target review:", 1)[0]
                day_tasks.append({
                    "title": f"Review: {raw_title}",
                    "description": focus_desc,
                    "est_minutes": min(class_minutes, daily_minutes)
                })
            fallback.append({
                "day": day_num,
                "quote": "Small, consistent sessions build strong exam readiness.",
                "tasks": day_tasks
            })
        data = {"plan": fallback}

    # Normalize the model/fallback output so the frontend always receives the expected shape.
    normalized = []
    for i, day in enumerate(data.get("plan", []), start=1):
        tasks = []
        for task in (day.get("tasks") or []):
            tasks.append({
                "title": str(task.get("title") or "Study session"),
                "description": str(task.get("description") or "Review the selected class and practise active recall."),
                "est_minutes": max(1, int(task.get("est_minutes") or 1))
            })
        normalized.append({
            "day": int(day.get("day") or i),
            "quote": str(day.get("quote") or ""),
            "tasks": tasks
        })

    if not normalized:
        return JSONResponse({"error": "Could not generate a study plan. Please select at least one class and try again."}, status_code=500)

    return {"plan": normalized}


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
    context = context_from_indices(rec, idx, max_chars=20000)
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

# ==============================================================================
# PAST PAPER SOLVER MODULE (SEPARATE STORAGE & ENGINE)
# ==============================================================================
PAST_PAPER_CONFIG_PATH = os.path.join(DATA_DIR, "past_paper_config.json")
PAST_PAPER_SOLUTIONS_PATH = os.path.join(DATA_DIR, "past_paper_solutions.json")
PAST_PAPER_LIB_PATH = os.path.join(DATA_DIR, "pastpaper_library.json")

# Authoritative structured syllabus maps used by the Syllabus Map feature.
# Each course can have one teacher-uploaded official syllabus source parsed into
# exact topic/subtopic headings. Student mapping is anchored to this structure
# before recordings or past papers are searched.
SYLLABUS_MAP_PATH = os.path.join(DATA_DIR, "syllabus_map.json")
_SYLLABUS_TOPIC_EMBED_CACHE = {}


# Legacy document-library filenames used by earlier builds.
LEGACY_PAST_PAPER_DOC_PATHS = [
    os.path.join(DATA_DIR, "past_paper_docs.json"),
    os.path.join(DATA_DIR, "pastpaper_docs.json"),
]

# --- Past Paper Isolated Data Helpers ---
def load_pp_json(path):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {} if "config" in path or "solutions" in path else []

def save_pp_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_pp_documents():
    """Load the one canonical answered/reference-document library."""
    docs = load_pp_json(PAST_PAPER_LIB_PATH)
    if not isinstance(docs, list):
        docs = []
    clean_docs = [d for d in docs if isinstance(d, dict)]
    if len(clean_docs) != len(docs):
        docs = clean_docs
        changed = True
    else:
        changed = False
    existing_ids = {str(d.get("id")) for d in docs if isinstance(d, dict) and d.get("id")}

    # Fold documents from older builds into the canonical library.
    for legacy_path in LEGACY_PAST_PAPER_DOC_PATHS:
        legacy_docs = load_pp_json(legacy_path)
        if not isinstance(legacy_docs, list):
            continue
        for legacy_doc in legacy_docs:
            if not isinstance(legacy_doc, dict):
                continue
            legacy_id = str(legacy_doc.get("id") or f"ppdoc_{secrets.token_hex(6)}")
            if legacy_id in existing_ids:
                continue
            legacy_doc["id"] = legacy_id
            legacy_doc["filename"] = legacy_doc.get("filename") or "Uploaded Document"
            legacy_doc["course"] = (legacy_doc.get("course") or "").strip()
            legacy_doc["chunks"] = legacy_doc.get("chunks") or []
            legacy_doc["text_chars"] = int(legacy_doc.get("text_chars") or sum(len(c) for c in legacy_doc.get("chunks", []) if isinstance(c, str)))
            legacy_doc["uploaded_at"] = legacy_doc.get("uploaded_at") or ""
            docs.append(legacy_doc)
            existing_ids.add(legacy_id)
            changed = True

    for doc in docs:
        if not isinstance(doc, dict):
            continue
        before=(doc.get("filename"),doc.get("course"),doc.get("uploaded_at"),doc.get("chunks"),doc.get("text_chars"))
        doc["filename"] = doc.get("filename") or "Uploaded Document"
        doc["course"] = (doc.get("course") or "").strip()
        doc["uploaded_at"] = doc.get("uploaded_at") or ""
        doc["chunks"] = doc.get("chunks") or []
        doc["text_chars"] = int(doc.get("text_chars") or sum(len(c) for c in doc.get("chunks", []) if isinstance(c, str)))
        after=(doc.get("filename"),doc.get("course"),doc.get("uploaded_at"),doc.get("chunks"),doc.get("text_chars"))
        if before != after:
            changed=True
    if changed:
        save_pp_json(PAST_PAPER_LIB_PATH, docs)
    return docs

def save_pp_documents(docs):
    save_pp_json(PAST_PAPER_LIB_PATH, docs)

def pp_doc_by_id(doc_id):
    return next((doc for doc in load_pp_documents() if doc.get("id") == doc_id), None)

def _paper_digits(paper: str) -> str:
    return "".join(re.findall(r"\d", str(paper or "")))


def infer_pastpaper_is_mcq(course: str, paper: str, syllabus: str = "") -> bool:
    """Use Cambridge component numbering as a deterministic backstop for MCQ papers.

    Cambridge 9700: components beginning with 1 are Paper 1 (MCQ).
    Cambridge 0610: components beginning with 1 or 2 are the MCQ papers.
    For unknown boards/courses, return False and let the extracted question structure decide.
    """
    nums = _paper_digits(paper)
    first = nums[:1]
    context = f"{course or ''} {syllabus or ''}".lower()

    if "9700" in context and first == "1":
        return True
    if "0610" in context and first in {"1", "2"}:
        return True
    return False


def extract_mcq_options(text: str) -> dict:
    """Extract A/B/C/D options from OCR/PDF text when the AI parser omitted them."""
    text = re.sub(r"\r\n?", "\n", str(text or "")).strip()
    if not text:
        return {}

    pattern = re.compile(
        r"(?m)(?:^|\n)\s*\(?([ABCD])\)?\s*[\.\):\-]\s*(.+?)(?=\n\s*\(?[ABCD]\)?\s*[\.\):\-]\s+|\Z)",
        re.IGNORECASE | re.DOTALL,
    )
    found = {}
    for m in pattern.finditer(text):
        letter = m.group(1).upper()
        value = re.sub(r"\s+", " ", m.group(2)).strip()
        if value:
            found[letter] = value
    return {k: found[k] for k in ("A", "B", "C", "D") if k in found}


def infer_correct_option_from_ms(mark_scheme: str, options: dict | None = None) -> str:
    """Best-effort extraction of an MCQ answer letter from mark-scheme text."""
    text = str(mark_scheme or "").strip()
    if not text:
        return ""

    m = re.search(r"(?:correct\s+answer|answer|key)\s*[:\-]?\s*\(?([ABCD])\)?\b", text, re.IGNORECASE)
    if m:
        return m.group(1).upper()

    compact = re.sub(r"[^A-Za-z]", "", text).upper()
    if compact in {"A", "B", "C", "D"}:
        return compact

    # Cambridge mark schemes often put the key as a short standalone token.
    for m in re.finditer(r"(?:^|\n)\s*\(?([ABCD])\)?\s*(?:$|\n)", text, re.IGNORECASE):
        return m.group(1).upper()
    return ""


# --- Student Metadata Endpoint ---
@app.post("/api/student/pastpaper/meta")
def student_pp_meta(body: RecListBody):
    sess = valid_session(body.token)
    if not sess:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    courses = sess.get("courses", [])
    if not courses:
        courses = [r.get("unit") for r in RECORDINGS if r.get("unit") and r.get("unit").strip().lower() != "unassigned"]
    
    sols = load_pp_json(PAST_PAPER_SOLUTIONS_PATH)
    syllabus_map = load_pp_json(PAST_PAPER_CONFIG_PATH)
    library = []
    for v in sols.values():
        course_name = v.get("course", "")
        paper_name = str(v.get("paper", ""))
        syllabus_name = syllabus_map.get(course_name, "") if isinstance(syllabus_map, dict) else ""
        inferred_mcq = infer_pastpaper_is_mcq(course_name, paper_name, syllabus_name)
        library.append({
            "course": course_name,
            "year": str(v.get("year", "")),
            "series": v.get("series", ""),
            "paper": paper_name,
            "question": str(v.get("question", "")),
            "question_type": "mcq" if inferred_mcq else v.get("question_type", "written"),
        })
    return {
        "courses": sorted(list(set(courses))),
        "syllabi": load_pp_json(PAST_PAPER_CONFIG_PATH),
        "library": library
    }

# --- Student Solver Endpoint ---
# ============================================================================

# --- Student Syllabus Map ---
# This module deliberately uses a two-stage process:
# 1) resolve the student's intended biological context from the syllabus + teacher notes;
# 2) search transcripts/past papers using that resolved context, then apply strict evidence selection.
# This prevents ambiguous keywords such as "translocation" from matching unrelated meanings.

def load_syllabus_maps():
    data = load_pp_json(SYLLABUS_MAP_PATH)
    return data if isinstance(data, dict) else {}


def save_syllabus_maps(data):
    save_pp_json(SYLLABUS_MAP_PATH, data if isinstance(data, dict) else {})


def _syllabus_map_tokens(text):
    stop = {
        "the", "a", "an", "and", "or", "of", "to", "in", "is", "are", "was", "were", "be", "been",
        "for", "on", "at", "by", "with", "about", "from", "as", "do", "does", "did", "can", "could",
        "would", "should", "will", "may", "might", "not", "no", "yes", "please", "tell", "me", "my",
        "your", "our", "their", "his", "her", "its", "explain", "where", "what", "how", "why", "when",
        "which", "find", "show", "study", "class", "topic", "part", "lesson", "section", "related",
        "covered", "cover", "inside", "about", "this", "that", "these", "those"
    }
    return [w for w in tokenize(text) if len(w) > 2 and w not in stop]


def _syllabus_map_lexical_score(query, text):
    q = set(_syllabus_map_tokens(query) if isinstance(query, str) else query)
    t = set(_syllabus_map_tokens(text))
    if not q or not t:
        return 0.0
    overlap = len(q & t)
    return overlap + (overlap / max(1, len(q))) * 2.0


def _flatten_syllabus_topics(course, data):
    """Flatten teacher-uploaded syllabus hierarchy while preserving exact labels."""
    out = []
    if not isinstance(data, dict):
        return out

    def walk(nodes, parent_path=""):
        for node in nodes or []:
            if not isinstance(node, dict):
                continue
            topic_id = str(node.get("id") or node.get("code") or "").strip()
            title = str(node.get("title") or node.get("name") or "").strip()
            desc = str(node.get("description") or "").strip()
            keywords = [str(x).strip() for x in (node.get("keywords") or []) if str(x).strip()]
            if title:
                path = f"{parent_path} → {title}" if parent_path else title
                out.append({
                    "course": course,
                    "id": topic_id,
                    "title": title,
                    "description": desc[:1200],
                    "keywords": keywords[:30],
                    "path": path,
                    "level": int(node.get("level") or (path.count("→") + 1)),
                })
                walk(node.get("children") or node.get("subtopics") or [], path)

    walk(data.get("topics") or data.get("outline") or [])
    return out


def _syllabus_heading_candidates(text):
    """Extract likely numbered syllabus headings directly from source text."""
    lines = [re.sub(r"\s+", " ", x).strip() for x in re.sub(r"\r\n?", "\n", text or "").split("\n")]
    rows = []
    seen = set()
    heading_re = re.compile(r"^(\d+(?:\.\d+){0,3}[A-Za-z]?)\s+(.{3,180})$")
    for i, line in enumerate(lines):
        if not line or len(line) > 220:
            continue
        m = heading_re.match(line)
        if not m:
            continue
        code, title = m.group(1), m.group(2).strip(" .:-")
        # Avoid obvious page/mark references and sentences.
        if len(title.split()) > 24:
            continue
        key = code.lower() + "|" + title.lower()
        if key in seen:
            continue
        seen.add(key)
        rows.append({"code": code, "title": title})
    return rows[:1200]


async def _build_structured_syllabus(course, syllabus_name, source_text, filename):
    candidates = _syllabus_heading_candidates(source_text)
    if not candidates:
        # Still allow a source without numbered headings, but give the model the source directly.
        candidates = [{"code": "", "title": x[:180]} for x in re.split(r"\n+", source_text or "") if x.strip()][:400]

    # Preserve the source; the model is instructed to copy titles rather than inventing them.
    compact_source = re.sub(r"\s+", " ", source_text or "").strip()
    compact_source = compact_source[:70000]
    prompt = {
        "course": course,
        "syllabus": syllabus_name,
        "candidate_headings": candidates,
        "source_text": compact_source,
    }
    system = (
        "You extract an official Biology syllabus into a structured hierarchy. "
        "Use ONLY the supplied source. Do not invent, merge, rename, or paraphrase official topic titles. "
        "Copy headings as written whenever possible. Preserve numbering/codes. "
        "Build a hierarchy from the numbering; if numbering is missing, use the document's visible order. "
        "Descriptions must be concise and based only on source text. Keywords must be short biological terms that are explicitly present in the source. "
        "Return STRICT JSON: {\"topics\":[{\"id\":string,\"title\":string,\"description\":string,\"keywords\":string[],\"children\":[...]}]}."
    )
    raw = await llm(
        [{"role": "system", "content": system},
         {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}],
        max_tokens=6000,
        temperature=0.0,
    )
    txt = (raw or "").strip()
    a, b = txt.find("{"), txt.rfind("}")
    if a == -1 or b == -1:
        raise ValueError("The syllabus parser did not return valid JSON.")
    data = json.loads(txt[a:b + 1])
    topics = data.get("topics") if isinstance(data, dict) else None
    if not isinstance(topics, list) or not topics:
        raise ValueError("No syllabus topics were extracted from the uploaded document.")
    return {
        "course": course,
        "syllabus": syllabus_name,
        "source_filename": filename,
        "uploaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source_chars": len(source_text or ""),
        "topics": topics,
    }


@app.post("/api/teacher/syllabus-map/upload")
async def teacher_syllabus_map_upload(
    passcode: str = Form(...),
    course: str = Form(...),
    syllabus: str = Form(""),
    file: UploadFile = File(...),
):
    if not check_teacher(passcode):
        return JSONResponse({"error": "Unauthorized passcode."}, status_code=401)
    course = (course or "").strip()
    if not course:
        return JSONResponse({"error": "Please select a course."}, status_code=400)
    filename = (file.filename or "syllabus.pdf").strip()
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext not in {"pdf", "docx", "txt", "md"}:
        return JSONResponse({"error": "Please upload a PDF, DOCX, TXT or MD syllabus file."}, status_code=400)
    data = await file.read()
    if len(data) > 25 * 1024 * 1024:
        return JSONResponse({"error": "Syllabus file is too large (25 MB maximum)."}, status_code=400)
    try:
        source_text = extract_text_from_upload(data, filename)
        if len(source_text.strip()) < 200:
            return JSONResponse({"error": "The uploaded syllabus could not be read as text."}, status_code=422)
        built = await _build_structured_syllabus(course, syllabus or course, source_text, filename)
        store = load_syllabus_maps()
        store[course] = built
        save_syllabus_maps(store)
        # Invalidate topic embedding cache for this course.
        for key in list(_SYLLABUS_TOPIC_EMBED_CACHE.keys()):
            if key.startswith(course + "::"):
                _SYLLABUS_TOPIC_EMBED_CACHE.pop(key, None)
        count = len(_flatten_syllabus_topics(course, built))
        return {"ok": True, "course": course, "syllabus": built.get("syllabus"), "topics_count": count, "source_filename": filename}
    except (LLMConfigError, LLMUpstreamError) as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    except Exception as e:
        return JSONResponse({"error": f"Could not build syllabus map: {e}"}, status_code=500)


def _topic_cache_key(course, topic):
    return f"{course}::{topic.get('id','')}::{topic.get('path','')}"


async def _embedding_batch(texts):
    texts = [str(x or "").strip() for x in texts]
    if not texts:
        return []
    if not OPENAI_API_KEY:
        raise LLMConfigError("The AI features are not configured on this server. Set OPENAI_API_KEY.")
    import httpx
    async with httpx.AsyncClient(timeout=90, connect=15) as client:
        resp = await client.post(
            f"{OPENAI_BASE_URL}/embeddings",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={"input": texts, "model": "text-embedding-3-small"},
        )
        resp.raise_for_status()
        data = resp.json().get("data", []) or []
        data = sorted(data, key=lambda x: x.get("index", 0))
        return [d.get("embedding", []) for d in data]


async def _topic_embedding(course, topic):
    key = _topic_cache_key(course, topic)
    if key in _SYLLABUS_TOPIC_EMBED_CACHE:
        return _SYLLABUS_TOPIC_EMBED_CACHE[key]
    text = " | ".join([
        topic.get("path") or topic.get("title") or "",
        topic.get("description") or "",
        " ".join(topic.get("keywords") or []),
    ]).strip()
    emb = await get_embedding(text)
    _SYLLABUS_TOPIC_EMBED_CACHE[key] = emb
    return emb


async def _select_syllabus_topic(query, topics, note_evidence):
    """Select one authoritative syllabus topic with deterministic anchors first.

    The previous implementation relied too heavily on an LLM confidence gate, which could
    return no topic even for an exact syllabus phrase. This version uses exact/phrase/keyword
    matches as hard evidence, then uses embeddings + an LLM only to resolve ambiguity.
    """
    if not topics:
        return None, "low", []

    q = (query or "").strip().lower()
    q_tokens = set(_syllabus_map_tokens(query))

    def norm_phrase(x):
        return re.sub(r"[^a-z0-9]+", " ", str(x or "").lower()).strip()

    def phrase_hits(qtext, text):
        qn = norm_phrase(qtext)
        tn = norm_phrase(text)
        if not qn or not tn:
            return 0
        return 1 if qn in tn else 0

    # Deterministic lexical scoring. Exact title/path hits get a strong bonus.
    deterministic = []
    for idx, topic in enumerate(topics):
        title = str(topic.get("title") or "")
        path = str(topic.get("path") or title)
        desc = str(topic.get("description") or "")
        kws = " ".join(topic.get("keywords") or [])
        text = " | ".join([path, desc, kws])
        title_tokens = set(_syllabus_map_tokens(title))
        path_tokens = set(_syllabus_map_tokens(path))
        kw_tokens = set(_syllabus_map_tokens(kws))
        overlap_title = len(q_tokens & title_tokens)
        overlap_path = len(q_tokens & path_tokens)
        overlap_kw = len(q_tokens & kw_tokens)
        exact_title = phrase_hits(query, title)
        exact_path = phrase_hits(query, path)
        lexical = _syllabus_map_lexical_score(query, text)
        depth = int(topic.get("level") or path.count("→") + 1)
        # Favor the most specific matching child topic over a generic parent.
        depth_bonus = min(depth, 6) * 0.025
        score = (
            exact_title * 3.0 +
            exact_path * 2.5 +
            min(overlap_title, 5) * 0.45 +
            min(overlap_path, 7) * 0.22 +
            min(overlap_kw, 7) * 0.14 +
            min(lexical, 8) * 0.06 +
            depth_bonus
        )
        deterministic.append((score, idx, topic, exact_title, exact_path, overlap_title, overlap_path, overlap_kw))

    deterministic.sort(key=lambda x: x[0], reverse=True)

    # Strong exact syllabus match: do not ask an LLM to veto a literal official title.
    top_d = deterministic[0]
    if top_d[3] or top_d[4] or top_d[5] >= 2:
        # If two different topics share the exact same term, use notes to disambiguate.
        tied = [x for x in deterministic[:8] if abs(x[0] - top_d[0]) < 0.35]
        if len(tied) == 1:
            t = top_d[2]
            return t, "high", [{"index": i, "path": x[2].get("path"), "title": x[2].get("title"), "semantic_score": 0.0} for i, x in enumerate(deterministic[:10])]

    # Embedding ranking is secondary evidence.
    q_emb = None
    try:
        q_emb = (await _embedding_batch([query]))[0]
    except Exception as exc:
        print(f"[syllabus-map] topic embedding failed: {exc}")

    topic_texts = []
    for topic in topics:
        topic_texts.append(" | ".join([
            topic.get("path") or topic.get("title") or "",
            topic.get("description") or "",
            " ".join(topic.get("keywords") or []),
        ]).strip())

    # Only embed the strongest lexical candidates to reduce cost and keep matching focused.
    candidate_indices = [x[1] for x in deterministic[:40]]
    topic_embs = {}
    if q_emb is not None and candidate_indices:
        try:
            embs = await _embedding_batch([topic_texts[i] for i in candidate_indices])
            for i, emb in zip(candidate_indices, embs):
                topic_embs[i] = emb
        except Exception as exc:
            print(f"[syllabus-map] topic batch embedding failed: {exc}")

    ranked = []
    for base_score, idx, topic, *rest in deterministic[:40]:
        sem = cosine_similarity(q_emb, topic_embs[idx]) if q_emb is not None and topic_embs.get(idx) else 0.0
        note_bonus = 0.0
        topic_tokens = set(_syllabus_map_tokens(topic_texts[idx]))
        for n in note_evidence[:12]:
            nt = set(_syllabus_map_tokens(n.get("text") or ""))
            overlap = len(topic_tokens & nt)
            if overlap:
                note_bonus = max(note_bonus, min(overlap, 5) * 0.04)
        total = base_score + sem * 0.55 + note_bonus
        ranked.append((total, sem, topic, idx))
    ranked.sort(key=lambda x: x[0], reverse=True)
    if not ranked:
        return None, "low", []

    # Prepare candidates for the LLM only when ambiguity remains.
    top = ranked[:12]
    cand_payload = []
    for i, row in enumerate(top):
        cand_payload.append({
            "index": i,
            "course": row[2].get("course"),
            "id": row[2].get("id"),
            "path": row[2].get("path"),
            "title": row[2].get("title"),
            "description": row[2].get("description"),
            "keywords": row[2].get("keywords"),
            "semantic_score": round(row[1], 4),
            "deterministic_score": round(row[0], 4),
        })

    # If the top topic is clearly ahead and has meaningful evidence, return it directly.
    margin = top[0][0] - (top[1][0] if len(top) > 1 else 0.0)
    if (top[0][0] >= 1.15 and margin >= 0.22) or top[0][1] >= 0.67:
        return top[0][2], "high", cand_payload

    notes = [
        {"course": n.get("course"), "recording": n.get("title"), "note": n.get("note_title"), "text": str(n.get("text") or "")[:800]}
        for n in note_evidence[:10]
    ]
    system = (
        "Choose exactly one official syllabus topic for the student's meaning. "
        "Use the hierarchy, not a shared keyword alone. Prefer a specific child topic over a generic parent. "
        "If the student's wording exactly names a syllabus concept, select that topic. "
        "Return STRICT JSON: {\"topic_index\":integer,\"confidence\":\"high|medium|low\"}."
    )
    user = json.dumps({"query": query, "candidates": cand_payload, "teacher_note_context": notes}, ensure_ascii=False)
    parsed = {}
    try:
        raw = await llm(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=400,
            temperature=0.0,
        )
        txt = (raw or "").strip(); a, b = txt.find("{"), txt.rfind("}")
        parsed = json.loads(txt[a:b + 1]) if a != -1 and b != -1 else {}
    except Exception as exc:
        print(f"[syllabus-map] topic judge failed: {exc}")

    idx = int(parsed.get("topic_index", -1)) if str(parsed.get("topic_index", "-1")).lstrip("-").isdigit() else -1
    conf = str(parsed.get("confidence") or "low").lower()
    if 0 <= idx < len(top):
        return top[idx][2], conf if conf in {"high", "medium", "low"} else "medium", cand_payload

    # Final deterministic fallback: if there is enough lexical/semantic evidence, keep it.
    if top[0][0] >= 0.85 or top[0][1] >= 0.58:
        return top[0][2], "medium", cand_payload
    return None, "low", cand_payload


async def _syllabus_map_note_context_for_topic(query, topic, recordings, allowed_courses, max_items=10):
    rows = []
    topic_terms = _syllabus_map_tokens(" ".join([
        query, topic.get("path") or "", topic.get("description") or "", " ".join(topic.get("keywords") or [])
    ]))
    notes_lib = load_notes_library()
    notes_by_id = {str(n.get("id")): n for n in notes_lib if isinstance(n, dict)}
    for rec in recordings:
        unit = (rec.get("unit") or "Unassigned").strip()
        if allowed_courses and unit not in allowed_courses:
            continue
        for nid in rec.get("note_ids") or []:
            note = notes_by_id.get(str(nid))
            if not note:
                continue
            for chunk in note.get("chunks") or []:
                text = str(chunk or "").strip()
                if not text:
                    continue
                score = _syllabus_map_lexical_score(topic_terms, text)
                if score <= 0:
                    continue
                rows.append({"recording_id": rec.get("id"), "title": rec.get("display_title") or rec.get("topic"), "course": unit, "note_title": note.get("filename") or "Teacher notes", "text": text, "score": score})
    rows.sort(key=lambda x: x["score"], reverse=True)
    return rows[:max_items]


async def _syllabus_map_recording_evidence(query, topic, recordings, allowed_courses, note_evidence, max_recordings=20):
    context = " | ".join([
        query,
        topic.get("path") or "",
        topic.get("description") or "",
        " ".join(topic.get("keywords") or []),
    ]).strip()
    topic_tokens = set(_syllabus_map_tokens(" ".join([topic.get("title") or "", " ".join(topic.get("keywords") or [])])))
    try:
        q_emb = await get_embedding(context)
    except Exception:
        q_emb = None
    ranked = []
    for rec in recordings:
        unit = (rec.get("unit") or "Unassigned").strip()
        if allowed_courses and unit not in allowed_courses:
            continue
        if not rec.get("visible", True):
            continue
        segs = rec.get("segments") or []
        if not segs:
            continue
        rec_embs = []
        if q_emb is not None:
            try:
                rec_embs = await build_index_async(rec)
            except Exception:
                rec_embs = []
        for i, seg in enumerate(segs):
            text = str(seg.get("text") or "").strip()
            if not text:
                continue
            lex = _syllabus_map_lexical_score(topic_tokens, text)
            sem = 0.0
            if q_emb is not None and i < len(rec_embs):
                sem = cosine_similarity(q_emb, rec_embs[i])
            title_bonus = _syllabus_map_lexical_score(topic.get("path") or topic.get("title") or "", (rec.get("display_title") or rec.get("topic") or "")) * 0.08
            # The syllabus topic has already been selected authoritatively. Require either
            # meaningful semantic support or strong topic anchors; do not require both in
            # every case because lecture wording often differs from the syllabus wording.
            if sem < 0.42 and lex < 2 and title_bonus <= 0.08:
                continue
            if lex < 1 and sem < 0.52 and title_bonus <= 0.16:
                continue
            score = sem * 0.78 + min(lex, 5) * 0.10 + title_bonus
            ranked.append((score, sem, rec, i, text))
    ranked.sort(key=lambda x: x[0], reverse=True)
    out = []
    seen = set()
    for score, sem, rec, i, text in ranked:
        rid = rec.get("id")
        key = (rid, i)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "recording_id": rid,
            "title": rec.get("display_title") or rec.get("topic") or "Class recording",
            "course": rec.get("unit") or "Unassigned",
            "date": rec.get("date") or "",
            "segment_index": i,
            "timestamp": fmt_ts((rec.get("segments") or [])[i].get("start")),
            "text": text,
            "semantic_score": float(sem),
        })
        if len(out) >= max_recordings * 3:
            break
    return out


async def _syllabus_map_pastpaper_candidates(query, topic, sols, allowed_courses, max_items=30):
    if not isinstance(sols, dict):
        return []
    context = " | ".join([
        query,
        topic.get("path") or "",
        topic.get("description") or "",
        " ".join(topic.get("keywords") or []),
    ]).strip()
    try:
        q_emb = (await _embedding_batch([context]))[0]
    except Exception:
        q_emb = None

    candidates = []
    topic_tokens = _syllabus_map_tokens(" ".join([
        topic.get("path") or "", topic.get("description") or "", " ".join(topic.get("keywords") or [])
    ]))
    for key, item in sols.items():
        if not isinstance(item, dict):
            continue
        course = str(item.get("course") or "").strip()
        if allowed_courses and course not in allowed_courses:
            continue
        q = str(item.get("question") or "").strip()
        qp = str(item.get("qp_text") or "").strip()
        searchable = f"{q} {qp}".strip()
        if not searchable:
            continue
        lex = _syllabus_map_lexical_score(topic_tokens, searchable)
        candidates.append({
            "key": str(key), "course": course, "year": str(item.get("year") or ""), "series": str(item.get("series") or ""),
            "paper": str(item.get("paper") or ""), "question": q or str(item.get("question_number") or ""),
            "question_type": str(item.get("question_type") or ""), "text": searchable[:1400], "lex": lex,
        })
    # First reduce the candidate pool using the topic vocabulary; then use embeddings.
    candidates.sort(key=lambda x: x["lex"], reverse=True)
    candidates = candidates[:120]
    if not candidates:
        return []
    try:
        embs = await _embedding_batch([c["text"][:5000] for c in candidates]) if q_emb is not None else []
    except Exception:
        embs = []
    rows=[]
    for i,c in enumerate(candidates):
        sem = cosine_similarity(q_emb, embs[i]) if q_emb is not None and i < len(embs) and embs[i] else 0.0
        if sem < 0.46 and c["lex"] < 1.5:
            continue
        c2=dict(c)
        c2["score"] = sem * 0.84 + min(c["lex"],5) * 0.05
        rows.append(c2)
    rows.sort(key=lambda x:x["score"], reverse=True)
    for row in rows:
        row.pop("lex", None)
    return rows[:max_items]


@app.post("/api/student/syllabus-map")
async def student_syllabus_map(body: dict):
    token = str(body.get("token") or "")
    query = str(body.get("query") or "").strip()
    requested_course = str(body.get("course") or "").strip()
    sess = valid_session(token)
    if not sess:
        return JSONResponse({"error": "Your session has expired. Please log in again."}, status_code=401)
    if len(query) < 2:
        return JSONResponse({"error": "Please enter a specific concept or question to search."}, status_code=400)

    student_courses = [str(c).strip() for c in (sess.get("courses") or []) if str(c).strip()]
    allowed_courses = set(student_courses)
    if requested_course:
        if allowed_courses and requested_course not in allowed_courses:
            return JSONResponse({"error": "That course is not assigned to your account."}, status_code=403)
        allowed_courses = {requested_course}

    eligible_recordings = [
        r for r in RECORDINGS
        if r.get("visible", True) and ((not allowed_courses) or ((r.get("unit") or "Unassigned").strip() in allowed_courses))
    ]
    if not eligible_recordings:
        return JSONResponse({"error": "No class recordings are available for the selected course."}, status_code=404)

    maps = load_syllabus_maps()
    topics = []
    syllabus_refs = []
    courses_with_maps = []
    for course in sorted({(r.get("unit") or "Unassigned").strip() for r in eligible_recordings}):
        m = maps.get(course)
        if isinstance(m, dict) and (m.get("topics") or m.get("outline")):
            flat = _flatten_syllabus_topics(course, m)
            topics.extend(flat)
            courses_with_maps.append(course)
            syllabus_refs.append({"course": course, "syllabus": m.get("syllabus") or course, "source_filename": m.get("source_filename") or ""})

    if not topics:
        return {
            "query": query,
            "syllabus_topic": None,
            "syllabus_subtopic": "",
            "syllabus_course": "",
            "topic_explanation": "An official structured syllabus map has not been uploaded for this course yet.",
            "syllabus_reference": [],
            "classes": [], "past_papers": [],
            "message": "The teacher needs to upload the official syllabus for this course before Syllabus Map can make an accurate mapping."
        }

    # Notes help distinguish meanings, but they do not replace the official syllabus.
    raw_note_context = await _syllabus_map_note_context_for_topic(query, {"title": query, "keywords": _syllabus_map_tokens(query), "path": query}, eligible_recordings, allowed_courses, max_items=12)
    topic, confidence, topic_candidates = await _select_syllabus_topic(query, topics, raw_note_context)
    if not topic or confidence == "low":
        return {
            "query": query,
            "syllabus_topic": None,
            "syllabus_subtopic": "",
            "syllabus_course": "",
            "topic_explanation": "The query is not specific enough to map confidently to one official syllabus topic.",
            "syllabus_reference": syllabus_refs,
            "classes": [], "past_papers": [],
            "message": "Please add a little context so the concept can be mapped to the correct syllabus topic."
        }

    note_evidence = await _syllabus_map_note_context_for_topic(query, topic, eligible_recordings, allowed_courses, max_items=12)
    class_evidence = await _syllabus_map_recording_evidence(query, topic, eligible_recordings, allowed_courses, note_evidence, max_recordings=14)
    class_evidence.sort(key=lambda x: x.get("semantic_score", 0), reverse=True)
    pp_candidates = await _syllabus_map_pastpaper_candidates(query, topic, load_pp_json(PAST_PAPER_SOLUTIONS_PATH), allowed_courses, max_items=24)

    # Final judge is only allowed to reject candidates or keep them; it cannot invent a new topic.
    class_text = "\n".join(
        f"[{i}] course={c['course']} | recording={c['title']} | date={c['date']} | timestamp={c['timestamp']} | evidence={c['text']}"
        for i, c in enumerate(class_evidence[:30])
    )
    pp_text = "\n".join(
        f"[{i}] course={p['course']} | year={p['year']} | series={p['series']} | paper={p['paper']} | question={p['question']} | evidence={p['text']}"
        for i, p in enumerate(pp_candidates)
    )
    system = (
        "You are the final strict relevance filter for an academic syllabus map. "
        "The official syllabus topic has ALREADY been selected. Do not change it. "
        "Keep a class only if the evidence is genuinely about that exact syllabus topic, not merely a shared word. "
        "Keep a past-paper question only if the question itself tests that exact topic. Generic biological references are insufficient. "
        "Return STRICT JSON: {\"class_indices\":integer[],\"past_paper_indices\":integer[]}. "
        "It is valid and preferred to return empty arrays when evidence is weak."
    )
    user = json.dumps({
        "student_query": query,
        "official_syllabus_topic": topic.get("path"),
        "topic_description": topic.get("description"),
        "topic_keywords": topic.get("keywords"),
        "teacher_note_context": [n.get("text") for n in note_evidence[:8]],
        "class_candidates": class_text,
        "past_paper_candidates": pp_text,
    }, ensure_ascii=False)
    try:
        raw = await llm(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=700,
            temperature=0.0,
        )
        txt=(raw or "").strip(); a,b=txt.find("{"),txt.rfind("}")
        parsed=json.loads(txt[a:b+1]) if a!=-1 and b!=-1 else {}
    except Exception:
        parsed={}

    def valid(vals, size, limit):
        out=[]
        for v in vals if isinstance(vals, list) else []:
            try: i=int(v)
            except Exception: continue
            if 0<=i<size and i not in out: out.append(i)
            if len(out)>=limit: break
        return out

    class_indices=valid(parsed.get("class_indices"), len(class_evidence), 8)
    pp_indices=valid(parsed.get("past_paper_indices"), len(pp_candidates), 8)

    # If the final LLM filter is overly conservative or unavailable, retain only
    # high-confidence deterministic candidates rather than returning nothing.
    if not class_indices and class_evidence:
        strong = [
            i for i, c in enumerate(class_evidence[:16])
            if float(c.get("semantic_score") or 0) >= 0.55
        ]
        class_indices = strong[:8]
    if not pp_indices and pp_candidates:
        strong_pp = [
            i for i, p in enumerate(pp_candidates[:16])
            if float(p.get("score") or 0) >= 0.50
        ]
        pp_indices = strong_pp[:8]

    grouped={}; order=[]
    for i in class_indices:
        c=class_evidence[i]; rid=c["recording_id"]
        if rid not in grouped:
            grouped[rid]={"recording_id":rid,"title":c["title"],"course":c["course"],"date":c["date"],"timestamps":[]}; order.append(rid)
        if c.get("timestamp") and c["timestamp"] not in grouped[rid]["timestamps"]:
            grouped[rid]["timestamps"].append(c["timestamp"])
    for rid in order:
        grouped[rid]["timestamps"].sort(key=lambda x: [int(y) if y.isdigit() else 0 for y in re.split(r"[:.]", x)])

    classes=[grouped[rid] for rid in order]
    papers=[]
    for i in pp_indices:
        p=pp_candidates[i]
        papers.append({"course":p["course"],"year":p["year"],"series":p["series"],"paper":p["paper"],"question":p["question"],"question_type":p["question_type"]})

    return {
        "query": query,
        "syllabus_topic": topic.get("title") or topic.get("path") or "",
        "syllabus_subtopic": topic.get("path") or topic.get("title") or "",
        "syllabus_course": topic.get("course") or "",
        "topic_explanation": topic.get("description") or "Mapped using the uploaded official syllabus structure.",
        "syllabus_reference": syllabus_refs,
        "classes": classes,
        "past_papers": papers,
        "message": "" if classes or papers else "No sufficiently relevant class or past-paper match was found for this syllabus topic."
    }


@app.post("/api/student/pastpaper/solve")
async def student_pp_solve(
    token: str = Form(...),
    course: str = Form(...),
    year: str = Form(...),
    series: str = Form(...),
    paper: str = Form(...),
    question: str = Form(...),
    doubt: str = Form("")
):
    sess = valid_session(token)
    if not sess:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    syllabi = load_pp_json(PAST_PAPER_CONFIG_PATH)
    syllabus = syllabi.get(course, "Standard Exam Board Specification")

    sols = load_pp_json(PAST_PAPER_SOLUTIONS_PATH)
    key = f"{course.strip().lower()}:{year.strip().lower()}:{series.strip().lower()}:{paper.strip().lower()}:{question.strip().lower()}"
    custom_asset = sols.get(key)
    if custom_asset:
        custom_asset = dict(custom_asset)

    exam_ref = f"{year} {series} P{paper} Q{question}".strip()

    paper_is_mcq = infer_pastpaper_is_mcq(course, paper, syllabus)
    question_type = str((custom_asset or {}).get("question_type") or "").strip().lower()
    is_mcq = paper_is_mcq or question_type in {"mcq", "multiple_choice", "multiple choice", "paper 1"}

    # Cambridge Paper 1 must never fall back to the written-response workflow, even when
    # an older library record was extracted before MCQ classification was introduced.
    if custom_asset:
        custom_asset["question_type"] = "mcq" if is_mcq else "written"

    options = (custom_asset or {}).get("options") or {}
    if not isinstance(options, dict):
        options = {}
    if not isinstance(options, dict):
        options = {}
    normalized_options = {}
    for letter in ("A", "B", "C", "D"):
        val = options.get(letter)
        if val:
            normalized_options[letter] = str(val).strip()

    if is_mcq and len(normalized_options) < 3 and custom_asset:
        recovered = extract_mcq_options(custom_asset.get("qp_text", ""))
        if len(recovered) >= 3:
            normalized_options = recovered
            custom_asset["options"] = recovered

    correct_option = str((custom_asset or {}).get("correct_option") or "").strip().upper()
    if is_mcq and correct_option not in {"A", "B", "C", "D"}:
        recovered_correct = infer_correct_option_from_ms((custom_asset or {}).get("ms_text", ""), normalized_options)
        if recovered_correct:
            correct_option = recovered_correct
            if custom_asset:
                custom_asset["correct_option"] = correct_option
                custom_asset["correct_answer_text"] = normalized_options.get(correct_option, custom_asset.get("correct_answer_text", ""))

    if custom_asset:
        custom_asset["options"] = normalized_options if is_mcq else {}
        custom_asset["correct_option"] = correct_option if is_mcq else ""
        if is_mcq:
            custom_asset["correct_answer_text"] = normalized_options.get(correct_option, custom_asset.get("correct_answer_text", ""))
        # Persist the corrected classification so future dropdowns do not revert to WRITTEN.
        sols[key] = custom_asset
        options = normalized_options
        save_pp_json(PAST_PAPER_SOLUTIONS_PATH, sols)

    if is_mcq:
        system = (
            f"You are an elite Cambridge-style Biology examiner and senior Biology tutor. Course: '{course}', Syllabus: '{syllabus}'.\n"
            "THIS IS A MULTIPLE-CHOICE QUESTION (MCQ). Do NOT answer it like a written-response question.\n"
            "The official question paper options and extracted mark-scheme answer are authoritative. Do not invent, reorder, or silently replace options.\n"
            "STRICT STRUCTURED OUTPUT:\n"
            "### 1. Correct Choice\n"
            "- State exactly one option letter: A, B, C, or D, followed by the full option text.\n"
            "- State the answer letter clearly on its own line: **Answer: X**.\n"
            "### 2. Option-by-Option Analysis\n"
            "- Discuss A, B, C, and D separately. For EACH option, explicitly say **Correct** or **Incorrect**.\n"
            "- Explain why that statement is correct or why it is wrong, using the official question wording and referring back to the official mark scheme.\n"
            "- Do not merely say the distractor is wrong; identify the biological reason or the exact mismatch with the mark scheme.\n"
            "### 3. Mark Scheme Link\n"
            "- Quote/paraphrase only the relevant mark-scheme evidence needed to justify the selected option. Do not invent criteria that are not present.\n"
            "### 4. Exam Tip\n"
            "- Give one concise tip about the command, concept, or distractor pattern relevant to this MCQ.\n"
            "If the mark scheme only gives the correct letter and does not explicitly explain every distractor, make that limitation clear and derive the option explanations only from the supplied question wording plus directly supported biology; never pretend the mark scheme said something it did not.\n"
        )
    else:
        system = (
            f"You are an elite academic examiner and senior Biology tutor. Course: '{course}', Syllabus: '{syllabus}'.\n"
            "THIS IS A WRITTEN-RESPONSE QUESTION. Do NOT treat it as an MCQ.\n"
            "STRICT STRUCTURED OUTPUT:\n"
            "### 1. Complete Model Answer\n"
            "- Write a full, flawless model answer as expected on the official exam lines, embedding all mandatory terms.\n"
            "### 2. Mark Scheme Breakdown & Mandatory Keywords\n"
            "- Detail the exact point criteria. Bold compulsory marking keywords.\n"
            "### 3. Conceptual Link & Biological Mechanism\n"
            "- Explain the underlying biological principles clearly.\n"
            "### 4. Examiner Traps & Common Mistakes\n"
            "- If official Examiner Report notes are provided below, base your traps directly on what real candidates did wrong, including penalised phrasing and common confusions."
        )

    user_text = f"Exam Ref: {exam_ref}\nStudent Doubt: {doubt or 'Provide a full breakdown and solution.'}\n\n"

    if custom_asset:
        if custom_asset.get("qp_text"):
            user_text += f"OFFICIAL QUESTION PROMPT:\n{custom_asset['qp_text']}\n\n"
        if custom_asset.get("ms_text"):
            user_text += f"OFFICIAL MARK SCHEME:\n{custom_asset['ms_text']}\n\n"
        if custom_asset.get("examiner_notes"):
            user_text += f"OFFICIAL EXAMINER REPORT NOTES FOR THIS QUESTION:\n{custom_asset['examiner_notes']}\n\n"
        if is_mcq:
            user_text += f"QUESTION TYPE: MCQ\n"
            user_text += f"EXTRACTED CORRECT OPTION LETTER FROM MARK SCHEME: {correct_option or '[not extracted]'}\n"
            if options:
                user_text += "OFFICIAL MCQ OPTIONS (from the question paper):\n"
                for letter in ("A", "B", "C", "D"):
                    if options.get(letter):
                        user_text += f"{letter}. {options.get(letter)}\n"
                user_text += "\n"
            user_text += (
                "MCQ RULE: The final answer must identify exactly one of A/B/C/D. "
                "Then evaluate all four options individually as Correct/Incorrect and explain each using the supplied mark scheme and question wording. "
                "If the mark scheme only gives a letter, explicitly say that the option-by-option rationale is an explanatory inference from the official question/mark-scheme pairing, not a quotation from the mark scheme.\n\n"
            )

        doc_id = custom_asset.get("answered_doc_id")
        if doc_id:
            pp_doc = pp_doc_by_id(doc_id)
            if pp_doc:
                user_text += f"[TEACHER MODEL ANSWER / EXAM DOC: {pp_doc.get('filename')}]:\n"
                user_text += "\n".join(pp_doc.get("chunks", [])[:5]) + "\n\n"

    try:
        raw = await llm(
            [{"role": "system", "content": system}, {"role": "user", "content": user_text}],
            max_tokens=2500,
            temperature=0.1
        )
    except Exception as e:
        return JSONResponse({"error": f"AI Solver Error: {str(e)}"}, status_code=503)

    return {
        "ok": True,
        "exam_ref": exam_ref,
        "syllabus": syllabus,
        "paper_is_mcq": paper_is_mcq,
        "question_type": "mcq" if is_mcq else "written",
        "solution_markdown": raw,
        "teacher_asset": custom_asset
    }

# --- Teacher Past Paper Management Endpoints ---

@app.post("/api/teacher/pastpaper/config")
def teacher_pp_config(body: TeacherAuth):
    if not check_teacher(body.passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
        
    courses = {r.get("unit").strip() for r in RECORDINGS if r.get("unit") and r.get("unit").strip().lower() != "unassigned"}
    for s in load_roster():
        for c in s.get("courses", []):
            if c and c.strip():
                courses.add(c.strip())

    sols = load_pp_json(PAST_PAPER_SOLUTIONS_PATH)
    pp_lib = load_pp_documents()
    for d in pp_lib:
        c = (d.get("course") or "").strip()
        if c and c.lower() != "unassigned":
            courses.add(c)
    
    # --- Group solutions hierarchically: Course -> Exam -> Questions ---
    library_tree = {}
    for key, v in sols.items():
        c = v.get("course", "Unassigned")
        exam_id = f"{v.get('year', '')} {v.get('series', '')} Paper {v.get('paper', '')}".strip()
        
        if c not in library_tree:
            library_tree[c] = {}
        if exam_id not in library_tree[c]:
            library_tree[c][exam_id] = []
            
        library_tree[c][exam_id].append({
            "key": key,
            "question": v.get("question", ""),
            "qp_text": v.get("qp_text", ""),
            "ms_text": v.get("ms_text", ""),
            "examiner_notes": v.get("examiner_notes", ""),
            "video_url": v.get("video_url", ""),
            "answered_doc_id": v.get("answered_doc_id", "")
        })

    structured_maps = load_syllabus_maps()
    structured_summary = {}
    for c, m in structured_maps.items():
        if isinstance(m, dict):
            structured_summary[c] = {
                "source_filename": m.get("source_filename", ""),
                "syllabus": m.get("syllabus", ""),
                "uploaded_at": m.get("uploaded_at", ""),
                "topics_count": len(_flatten_syllabus_topics(c, m)),
            }

    return {
        "courses": sorted(list(courses)),
        "syllabi": load_pp_json(PAST_PAPER_CONFIG_PATH),
        "structured_syllabi": structured_summary,
        "pp_library": [
            {
                "id": d.get("id", ""),
                "filename": d.get("filename", "Uploaded Document"),
                "course": d.get("course", ""),
                "uploaded_at": d.get("uploaded_at", ""),
                "text_chars": d.get("text_chars", 0),
            }
            for d in pp_lib if d.get("id")
        ],
        "solutions_tree": library_tree,  # Grouped hierarchy
        "solutions": [{"key": k, **v} for k, v in sols.items()] # Backward compatibility
    }

@app.post("/api/teacher/pastpaper/upload-doc")
async def teacher_pp_upload_doc(
    passcode: str = Form(...),
    course: str = Form(...),
    file: UploadFile = File(...)
):
    if not check_passcode(passcode):
        return JSONResponse({"error": "Unauthorized passcode."}, status_code=401)

    selected_course = (course or "").strip()
    filename = (file.filename or "Uploaded Document").strip()
    if not selected_course:
        return JSONResponse({"error": "Please select a course for this document."}, status_code=400)

    try:
        content = await file.read()
        extracted_text = extract_text_from_upload(content, filename).strip()
        if not extracted_text:
            return JSONResponse({"error": "No readable text could be extracted from this document."}, status_code=422)

        doc_id = f"ppdoc_{uuid.uuid4().hex[:10]}"
        doc_data = {
            "id": doc_id,
            "filename": filename,
            "course": selected_course,
            "uploaded_at": date.today().isoformat(),
            "chunks": chunk_note_text(extracted_text),
            "text_chars": len(extracted_text),
        }
        docs = load_pp_documents()
        docs.append(doc_data)
        save_pp_documents(docs)
        return {"ok": True, "doc": {"id": doc_id, "filename": filename, "course": selected_course, "uploaded_at": doc_data["uploaded_at"], "text_chars": doc_data["text_chars"]}}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"Failed to upload doc: {str(e)}"}, status_code=500)


@app.get("/api/teacher/pastpaper/docs")
async def teacher_pp_get_docs(passcode: str = "", course: str = ""):
    if not check_passcode(passcode):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    requested_course = (course or "").strip().lower()
    result = []
    for d in load_pp_documents():
        doc_course = (d.get("course") or "").strip()
        if requested_course and doc_course and doc_course.lower() != requested_course:
            continue
        result.append({"id": d.get("id"), "filename": d.get("filename"), "course": doc_course, "uploaded_at": d.get("uploaded_at", ""), "text_chars": d.get("text_chars", 0)})
    result.sort(key=lambda d: (str(d.get("uploaded_at") or ""), (d.get("filename") or "").lower()), reverse=True)
    return {"docs": result}


@app.post("/api/teacher/pastpaper/docs/update-course")
async def update_doc_course(passcode: str = Form(...), doc_id: str = Form(...), course: str = Form(...)):
    if not check_passcode(passcode):
        return JSONResponse({"error": "Unauthorized passcode."}, status_code=401)
    docs = load_pp_documents()
    new_course = (course or "").strip()
    updated_doc = None
    for d in docs:
        if d.get("id") == doc_id:
            d["course"] = new_course
            updated_doc = d
            break
    if updated_doc is None:
        return JSONResponse({"error": "Document not found."}, status_code=404)
    save_pp_documents(docs)
    return {"ok": True, "doc": {"id": updated_doc.get("id"), "filename": updated_doc.get("filename"), "course": updated_doc.get("course", "")}}


@app.post("/api/teacher/pastpaper/delete_doc")
def teacher_pp_delete_doc(body: dict):
    if not check_passcode(body.get("passcode", "")):
        return JSONResponse({"error": "Unauthorized passcode."}, status_code=401)
    doc_id = str(body.get("doc_id") or "").strip()
    if not doc_id:
        return JSONResponse({"error": "Missing document id."}, status_code=400)
    docs = load_pp_documents()
    remaining = [d for d in docs if d.get("id") != doc_id]
    if len(remaining) == len(docs):
        return JSONResponse({"error": "Document not found."}, status_code=404)
    save_pp_documents(remaining)
    sols = load_pp_json(PAST_PAPER_SOLUTIONS_PATH)
    changed=False
    for item in sols.values():
        if item.get("answered_doc_id") == doc_id:
            item["answered_doc_id"] = ""
            item["answered_doc_name"] = ""
            changed=True
    if changed:
        save_pp_json(PAST_PAPER_SOLUTIONS_PATH, sols)
    return {"ok": True}


@app.post("/api/teacher/pastpaper/config/save")
def teacher_pp_save_syllabus(body: dict):
    if not check_teacher(body.get("passcode", "")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    cfg = load_pp_json(PAST_PAPER_CONFIG_PATH)
    cfg[body["course"]] = body["syllabus"]
    save_pp_json(PAST_PAPER_CONFIG_PATH, cfg)
    return {"ok": True}

@app.post("/api/teacher/pastpaper/solutions/delete")
def teacher_pp_delete_solution(body: dict):
    if not check_teacher(body.get("passcode", "")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    sols = load_pp_json(PAST_PAPER_SOLUTIONS_PATH)
    sols.pop(body.get("key", ""), None)
    save_pp_json(PAST_PAPER_SOLUTIONS_PATH, sols)
    return {"ok": True}


@app.post("/api/teacher/pastpaper/solutions/delete-exam")
def teacher_pp_delete_exam(body: dict):
    """Delete every curated question belonging to one complete exam pack.

    The uploaded model-answer/reference documents are intentionally left intact;
    this action removes only the question records from past_paper_solutions.json.
    """
    if not check_teacher(body.get("passcode", "")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    course = str(body.get("course") or "").strip()
    year = str(body.get("year") or "").strip()
    series = str(body.get("series") or "").strip()
    paper = str(body.get("paper") or "").strip()

    if not course or not year or not series or not paper:
        return JSONResponse(
            {"error": "Course, year, series, and paper are required."},
            status_code=400,
        )

    target = (
        course.casefold(),
        year.casefold(),
        series.casefold(),
        paper.casefold(),
    )

    sols = load_pp_json(PAST_PAPER_SOLUTIONS_PATH)
    if not isinstance(sols, dict):
        sols = {}

    removed_keys = []
    remaining = {}
    for key, item in sols.items():
        item_target = (
            str(item.get("course") or "").strip().casefold(),
            str(item.get("year") or "").strip().casefold(),
            str(item.get("series") or "").strip().casefold(),
            str(item.get("paper") or "").strip().casefold(),
        )
        if item_target == target:
            removed_keys.append(key)
        else:
            remaining[key] = item

    if not removed_keys:
        return JSONResponse({"error": "No questions found for that exam."}, status_code=404)

    save_pp_json(PAST_PAPER_SOLUTIONS_PATH, remaining)
    return {
        "ok": True,
        "deleted": len(removed_keys),
        "course": course,
        "year": year,
        "series": series,
        "paper": paper,
    }
class SavePPSolutionBody(BaseModel):
    passcode: str
    course: str
    year: str
    series: str
    paper: str
    question: str
    qp_text: str | None = ""
    ms_text: str | None = ""
    video_url: str | None = ""
    answered_doc_id: str | None = ""

@app.post("/api/teacher/pastpaper/solutions/save")
def teacher_pp_save_solution(body: SavePPSolutionBody):
    if not check_teacher(body.passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    sols = load_pp_json(PAST_PAPER_SOLUTIONS_PATH)
    key = f"{body.course.strip().lower()}:{body.year.strip().lower()}:{body.series.strip().lower()}:{body.paper.strip().lower()}:{body.question.strip().lower()}"
    doc = pp_doc_by_id(body.answered_doc_id) if body.answered_doc_id else None
    sols[key] = {
        "course": body.course,
        "year": body.year,
        "series": body.series,
        "paper": body.paper,
        "question": body.question,
        "qp_text": body.qp_text or "",
        "ms_text": body.ms_text or "",
        "video_url": body.video_url or "",
        "answered_doc_id": body.answered_doc_id or "",
        "answered_doc_name": doc.get("filename", "") if doc else ""
    }
    save_pp_json(PAST_PAPER_SOLUTIONS_PATH, sols)
    return {"ok": True}

@app.post("/api/teacher/pastpaper/bulk-upload")
async def teacher_pp_bulk_upload(
    passcode: str = Form(...),
    course: str = Form(...),
    year: str = Form(...),
    series: str = Form(...),
    paper: str = Form(...),
    video_url: str = Form(""),
    answered_doc_id: str = Form(""),
    qp_file: UploadFile = File(...),
    ms_file: UploadFile = File(...),
    er_file: UploadFile = File(None)
):
    """Ingest a whole exam pack. Paper 1/MCQ exams are parsed in small batches
    because a complete MCQ paper can otherwise exceed the LLM's structured-output
    budget even when the PDFs themselves extract perfectly."""
    if not check_passcode(passcode):
        return JSONResponse({"error": "Unauthorized passcode."}, status_code=401)

    try:
        qp_bytes = await qp_file.read()
        ms_bytes = await ms_file.read()
        qp_text = extract_text_from_upload(qp_bytes, qp_file.filename or "qp.pdf")
        ms_text = extract_text_from_upload(ms_bytes, ms_file.filename or "ms.pdf")
        er_text = ""
        if er_file and er_file.filename:
            er_bytes = await er_file.read()
            er_text = extract_text_from_upload(er_bytes, er_file.filename or "er.pdf")
    except Exception as e:
        return JSONResponse({"error": f"PDF reading error: {str(e)}"}, status_code=400)

    if len(qp_text.strip()) < 50 or len(ms_text.strip()) < 50:
        return JSONResponse(
            {"error": "Could not extract text from the Question Paper or Mark Scheme."},
            status_code=422
        )

    syllabus_map = load_pp_json(PAST_PAPER_CONFIG_PATH)
    syllabus_name = syllabus_map.get(course, "") if isinstance(syllabus_map, dict) else ""
    paper_is_mcq = (infer_pastpaper_is_mcq(course, paper, syllabus_name) or bool(re.search(r"multiple\s+choice", qp_text[:5000], re.I)) or bool(re.search(r"four\s+possible\s+answers\s+A\s*,?\s*B\s*,?\s*C\s*,?\s*and\s*D", qp_text[:8000], re.I)))

    # ------------------------------------------------------------------
    # MCQ / Paper 1 path: deterministic question splitting + official key
    # ------------------------------------------------------------------
    if paper_is_mcq:
        # Cambridge mark schemes such as 1 A 1 ... 40 A 1 give us the
        # authoritative answer letter without asking the model to infer it.
        answer_map = {
            str(n): letter.upper()
            for n, letter in re.findall(r"(?m)^\s*(\d{1,2})\s+([ABCD])\s+1(?:\s|$)", ms_text, re.I)
        }

        # Split the extracted paper by the sequential main question numbers.
        # This avoids accidentally treating internal statements (1, 2, 3) as
        # new questions because we advance strictly from Q1 -> Q2 -> ...
        main_starts = []
        cursor = 0
        for qn in range(1, 41):
            pat = re.compile(rf"(?m)^\s*{qn}\s+(?=[A-Z])")
            m = next((m for m in pat.finditer(qp_text) if m.start() >= cursor), None)
            if not m:
                break
            main_starts.append((qn, m.start()))
            cursor = m.start() + 1

        if len(main_starts) < 2 or len(answer_map) < 2:
            return JSONResponse({
                "error": (
                    "This Paper 1 could be read as MCQ, but the Question Paper/Mark Scheme "
                    "could not be segmented reliably. The PDFs contain extractable text, so this "
                    "is a paper-structure parsing issue rather than a file-upload issue."
                )
            }, status_code=422)

        question_blocks = []
        for i, (qn, pos) in enumerate(main_starts):
            end_pos = main_starts[i + 1][1] if i + 1 < len(main_starts) else len(qp_text)
            block = qp_text[pos:end_pos].strip()
            question_blocks.append((str(qn), block))

        all_items = []
        batch_size = 8
        for batch_start in range(0, len(question_blocks), batch_size):
            batch = question_blocks[batch_start:batch_start + batch_size]
            batch_numbers = [qn for qn, _ in batch]
            batch_text = "\n\n".join(
                f"===== QUESTION {qn} =====\n{block}" for qn, block in batch
            )
            batch_keys = "\n".join(
                f"Question {qn}: {answer_map.get(qn, '[answer not found]')}" for qn in batch_numbers
            )

            system_prompt = (
                "You are an exhaustive Cambridge Biology Paper 1 ingestion parser.\n"
                "The source is a multiple-choice paper. Parse ONLY the numbered questions supplied in this batch.\n"
                "Return one JSON object with a 'questions' array and exactly one object per supplied question.\n"
                "For each question return:\n"
                "- question_number: the supplied number\n"
                "- question_type: exactly 'mcq'\n"
                "- question_text: the question stem, cleaned of page headers/footers\n"
                "- options: object containing A, B, C, D when recoverable from the supplied text\n"
                "- correct_option: use the authoritative answer letter supplied separately; do not infer or change it\n"
                "- correct_answer_text: option text matching correct_option when recoverable\n"
                "- mark_scheme: exactly 'Official answer: X (1 mark)'\n"
                "- examiner_notes: empty string unless examiner-report text is supplied\n"
                "Do not invent missing diagram content. If a diagram-based option is not recoverable from text, keep the option text as the best faithful text extraction you can make.\n"
                "Return STRICT JSON only.\n"
                '{"questions":[{"question_number":"1","question_type":"mcq","question_text":"...","options":{"A":"...","B":"...","C":"...","D":"..."},"correct_option":"A","correct_answer_text":"...","mark_scheme":"Official answer: A (1 mark)","examiner_notes":""}]}'
            )
            user_prompt = (
                f"EXAM: {course} {year} {series} Paper {paper}\n\n"
                "AUTHORITATIVE MARK-SCHEME ANSWER LETTERS FOR THIS BATCH:\n"
                f"{batch_keys}\n\n"
                "QUESTION PAPER BATCH:\n"
                f"{batch_text}"
            )

            try:
                raw = await llm(
                    [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
                    max_tokens=5000,
                    temperature=0.0
                )
                clean_raw = (raw or "").strip()
                if clean_raw.startswith("```"):
                    clean_raw = clean_raw.strip("`")
                    if "\n" in clean_raw:
                        clean_raw = clean_raw.split("\n", 1)[-1]
                start_idx = clean_raw.find("{")
                end_idx = clean_raw.rfind("}")
                if start_idx == -1 or end_idx <= start_idx:
                    raise ValueError("AI returned no complete JSON object.")
                parsed = json.loads(clean_raw[start_idx:end_idx + 1])
                batch_items = parsed.get("questions", [])
                if not isinstance(batch_items, list):
                    raise ValueError("AI returned an invalid questions array.")
            except Exception as e:
                return JSONResponse({
                    "error": f"AI parsing failed while processing Paper 1 questions {batch_numbers[0]}–{batch_numbers[-1]}: {e}"
                }, status_code=500)

            # Normalize, enforce the official answer key, and collect.
            by_num = {str(item.get("question_number", "")).strip(): item for item in batch_items if isinstance(item, dict)}
            for qn, original_block in batch:
                item = dict(by_num.get(qn, {}))
                item["question_number"] = qn
                item["question_type"] = "mcq"
                options = item.get("options") if isinstance(item.get("options"), dict) else {}
                normalized = {
                    letter: str(options.get(letter)).strip()
                    for letter in ("A", "B", "C", "D")
                    if options.get(letter)
                }
                if len(normalized) < 3:
                    recovered = extract_mcq_options(item.get("question_text", ""))
                    if len(recovered) >= 3:
                        normalized = recovered
                official = answer_map.get(qn, "")
                item["options"] = normalized
                item["correct_option"] = official
                item["correct_answer_text"] = normalized.get(official, "")
                item["mark_scheme"] = f"Official answer: {official} (1 mark)" if official else ""
                item["examiner_notes"] = str(item.get("examiner_notes", "") or "").strip()
                if not item.get("question_text"):
                    item["question_text"] = original_block
                all_items.append(item)

        parsed_questions = all_items

    # ---------------------------------------------------------------
    # Written-response / non-MCQ path: retain the original whole-pack flow.
    # ---------------------------------------------------------------
    else:
        system_prompt = (
            "You are an exhaustive past-paper exam ingestion parser.\n"
            "Segment the Question Paper, Mark Scheme, and (if provided) Examiner Report into individual question items.\n"
            "For EVERY question, first classify it as either 'mcq' or 'written'. Use the actual question-paper structure, not guesses based on the topic.\n"
            "Do not force MCQ classification unless the question paper actually contains answer choices.\n"
            "For MCQs, preserve the option text exactly enough to distinguish A/B/C/D, and map the official mark-scheme answer letter to correct_option.\n"
            "For written questions, set options to {} and correct_option to ''.\n"
            "For each question, extract:\n"
            "1. question_number: e.g., '1(a)'\n"
            "2. question_type: exactly 'mcq' or 'written'\n"
            "3. question_text: Prompt text, including any statement/set-up needed to understand the question\n"
            "4. options: for MCQ only, an object with A, B, C, D option text; otherwise {}\n"
            "5. correct_option: for MCQ only, the official correct letter from the mark scheme (A/B/C/D); otherwise ''\n"
            "6. correct_answer_text: for MCQ only, the exact option text corresponding to correct_option if available; otherwise ''\n"
            "7. mark_scheme: Corresponding mark criteria and acceptable points exactly as supplied\n"
            "8. examiner_notes: Specific commentary, misconceptions, or candidate errors mentioned in the Examiner Report (leave empty string if not found or not provided).\n"
            "IMPORTANT: Do not invent a correct option. If the mark scheme answer cannot be confidently mapped, leave correct_option blank and retain the raw mark_scheme text.\n\n"
            "Return STRICT JSON only without prose or markdown fences:\n"
            '{"questions": [{"question_number": "1(a)", "question_type": "written", "question_text": "...", "options": {}, "correct_option": "", "correct_answer_text": "", "mark_scheme": "...", "examiner_notes": "..."}]}'
        )

        er_block = f"\n\n=== EXAMINER REPORT (FULL) ===\n{er_text[:60000]}" if er_text else ""
        user_prompt = (
            f"EXAM: {course} {year} {series} Paper {paper}\n\n"
            f"=== QUESTION PAPER (FULL) ===\n{qp_text[:60000]}\n\n"
            f"=== MARK SCHEME (FULL) ===\n{ms_text[:60000]}"
            f"{er_block}"
        )

        try:
            raw = await llm(
                [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
                max_tokens=8000,
                temperature=0.0
            )
            clean_raw = (raw or "").strip()
            if clean_raw.startswith("```"):
                clean_raw = clean_raw.strip("`")
                if "\n" in clean_raw:
                    clean_raw = clean_raw.split("\n", 1)[-1]

            start_idx = clean_raw.find("{")
            end_idx = clean_raw.rfind("}")
            if start_idx == -1 or end_idx <= start_idx:
                raise ValueError("AI returned no complete JSON object.")
            parsed = json.loads(clean_raw[start_idx:end_idx + 1])
            parsed_questions = parsed.get("questions", [])
        except Exception as e:
            return JSONResponse({"error": f"AI Parsing error: {str(e)}"}, status_code=500)

    sols = load_pp_json(PAST_PAPER_SOLUTIONS_PATH)
    doc = pp_doc_by_id(answered_doc_id) if answered_doc_id else None
    indexed_labels = []

    for item in parsed_questions:
        q_num = str(item.get("question_number", "")).strip()
        if not q_num:
            continue
        key = f"{course.strip().lower()}:{year.strip().lower()}:{series.strip().lower()}:{paper.strip().lower()}:{q_num.lower()}"
        raw_type = str(item.get("question_type", "")).strip().lower()
        options = item.get("options") or {}
        if not isinstance(options, dict):
            options = {}
        normalized_options = {}
        for letter in ("A", "B", "C", "D"):
            val = options.get(letter)
            if val:
                normalized_options[letter] = str(val).strip()
        is_mcq_item = paper_is_mcq or raw_type in {"mcq", "multiple_choice", "multiple choice", "paper 1"} or len(normalized_options) >= 3
        if is_mcq_item and len(normalized_options) < 3:
            recovered = extract_mcq_options(str(item.get("question_text", "")))
            if len(recovered) >= 3:
                normalized_options = recovered
        extracted_correct_option = str(item.get("correct_option", "")).strip().upper() if is_mcq_item else ""
        if is_mcq_item and extracted_correct_option not in {"A", "B", "C", "D"}:
            extracted_correct_option = infer_correct_option_from_ms(str(item.get("mark_scheme", "")), normalized_options)
        extracted_correct_text = normalized_options.get(extracted_correct_option, "") if extracted_correct_option else ""
        sols[key] = {
            "course": course.strip(),
            "year": year.strip(),
            "series": series.strip(),
            "paper": paper.strip(),
            "question": q_num,
            "question_type": "mcq" if is_mcq_item else "written",
            "options": normalized_options if is_mcq_item else {},
            "correct_option": extracted_correct_option,
            "correct_answer_text": extracted_correct_text or (str(item.get("correct_answer_text", "")).strip() if is_mcq_item else ""),
            "qp_text": str(item.get("question_text", "")).strip(),
            "ms_text": str(item.get("mark_scheme", "")).strip(),
            "examiner_notes": str(item.get("examiner_notes", "")).strip(),
            "video_url": video_url.strip(),
            "answered_doc_id": answered_doc_id or "",
            "answered_doc_name": doc.get("filename", "") if doc else ""
        }
        indexed_labels.append(q_num)

    save_pp_json(PAST_PAPER_SOLUTIONS_PATH, sols)
    return {"ok": True, "indexed": len(indexed_labels), "questions": indexed_labels}

if os.path.isdir(FRONTEND_DIR) and os.path.isfile(os.path.join(FRONTEND_DIR, "index.html")):
    @app.get("/")
    def index():
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
    app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="static")
else:
    print(f"[startup warning] Frontend directory not found: {FRONTEND_DIR}")
