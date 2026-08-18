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
from collections import OrderedDict
# Bounded LRU cache: keep only the few most-recently-used recording indexes in
# RAM instead of letting all 40+ accumulate. Prevents unbounded memory growth
# as students query many different recordings.
_INDEX_CACHE = OrderedDict()
_INDEX_CACHE_MAX = int(os.environ.get("INDEX_CACHE_MAX", "8"))
def get_index(rec):
    rid = rec["id"]
    if rid in _INDEX_CACHE:
        _INDEX_CACHE.move_to_end(rid)
        return _INDEX_CACHE[rid]
    idx = build_index(rec)
    _INDEX_CACHE[rid] = idx
    _INDEX_CACHE.move_to_end(rid)
    while len(_INDEX_CACHE) > _INDEX_CACHE_MAX:
        _INDEX_CACHE.popitem(last=False)  # evict least-recently-used
    return idx
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
    notes = rec.get("notes") or []
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
OPENAI_TRANSCRIBE_MODEL = os.environ.get("OPENAI_TRANSCRIBE_MODEL", "whisper-1").strip()
# OpenAI's audio upload limit is 25 MB. We compress below this and, if still too
# large, split into time-based chunks that each stay under it.
WHISPER_MAX_BYTES = 25 * 1000 * 1000  # 25 MB (use decimal MB, matches provider)
_CHUNK_SAFETY_BYTES = 24 * 1000 * 1000  # aim comfortably under the hard limit


def _have_ffmpeg():
    import shutil
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _ffprobe_duration(path):
    """Return media duration in seconds (float), or 0.0 if it can't be read."""
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
    """Transcode (a slice of) src to a mono 16 kHz MP3 — small and speech-friendly."""
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
    """Turn a float number of seconds into an 'HH:MM:SS.mmm' timestamp string
    matching the format used by the existing Zoom .vtt transcripts."""
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
    """Send audio to an OpenAI-compatible Whisper endpoint and return
    [{start, speaker, text}, ...] segments. Whisper does not do speaker
    diarization, so 'speaker' is left blank (the app handles blank speakers).
    time_offset (seconds) is added to every segment start, so callers can
    transcribe chunks of a longer recording and stitch them together."""
    if not OPENAI_API_KEY:
        raise LLMConfigError(
            "Transcription needs OPENAI_API_KEY set on this server (and redeploy)."
        )
    import httpx
    timeout = httpx.Timeout(600.0, connect=15.0)
    files = {"file": (filename, audio_bytes, "application/octet-stream")}
    # Force English transcripts. Whisper's /audio/translations endpoint always
    # outputs English (translating from whatever language is spoken, e.g. Arabic).
    # Set TRANSCRIBE_ENGLISH_ONLY=0 to fall back to same-language transcription.
    english_only = os.environ.get("TRANSCRIBE_ENGLISH_ONLY", "1").strip() != "0"
    endpoint = "/audio/translations" if english_only else "/audio/transcriptions"
    data = {
        "model": OPENAI_TRANSCRIBE_MODEL,
        "response_format": "verbose_json",
        "timestamp_granularities[]": "segment",
    }
    if not english_only:
        # only meaningful for the transcriptions endpoint
        data["language"] = os.environ.get("TRANSCRIBE_LANGUAGE", "").strip() or None
        data = {k: v for k, v in data.items() if v is not None}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{OPENAI_BASE_URL}{endpoint}",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                data=data,
                files=files,
            )
    except httpx.RequestError as e:
        raise LLMUpstreamError(f"Could not reach the transcription provider: {e}") from e

    if resp.status_code >= 400:
        detail = ""
        try:
            detail = resp.json().get("error", {}).get("message", "")
        except Exception:
            detail = resp.text[:300]
        raise LLMUpstreamError(
            f"Transcription provider returned {resp.status_code}: {detail or 'unknown error'}"
        )

    try:
        payload = resp.json()
    except Exception as e:
        raise LLMUpstreamError(f"Unexpected transcription response shape: {e}") from e

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
    # Fallback: no per-segment data but we still got text -> one big segment.
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
    """Look up a meeting's cloud recording files from Zoom by meeting id/uuid.
    Returns the list of recording_files (may be empty)."""
    import httpx
    from urllib.parse import quote
    token = await zoom_token()
    # UUIDs that contain '/' or start with '/' must be double-URL-encoded per Zoom docs.
    mid = str(meeting_id)
    needs_double = mid.startswith("/") or "//" in mid or "/" in mid
    path_id = quote(quote(mid, safe=""), safe="") if needs_double else quote(mid, safe="")
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(
            f"https://api.zoom.us/v2/meetings/{path_id}/recordings",
            headers={"Authorization": f"Bearer {token}"},
        )
        if r.status_code != 200:
            raise LLMUpstreamError(
                f"Zoom recordings lookup returned {r.status_code}: {r.text[:300]}"
            )
        return r.json().get("recording_files", []) or []


async def fetch_zoom_recording_object(meeting_id):
    """Fetch a meeting's FULL cloud-recording object from Zoom (topic, start_time,
    type, recording_files, ...) by meeting id/uuid — the shape ingest_zoom_meeting expects."""
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
            raise LLMUpstreamError(
                "No cloud recording found for that meeting ID. Check the ID/link, or the "
                "recording may have been deleted from Zoom."
            )
        if r.status_code != 200:
            raise LLMUpstreamError(f"Zoom lookup returned {r.status_code}: {r.text[:300]}")
        return r.json()


def _parse_meeting_id(raw: str) -> str:
    """Accept a plain meeting ID/UUID OR a Zoom recording link and return the id/uuid.
    Handles URLs like:
      https://zoom.us/rec/share/<...>            (share link -> not an id; rejected)
      https://<acct>.zoom.us/recording/detail?meeting_id=<UUID>
      https://zoom.us/j/<meetingNumber>
    and bare numeric IDs or base64 UUIDs.
    """
    import re as _re
    from urllib.parse import urlparse, parse_qs, unquote
    s = (raw or "").strip()
    if not s:
        return ""
    if s.startswith("http"):
        u = urlparse(s)
        qs = parse_qs(u.query)
        # explicit meeting_id query param (recording detail pages)
        for key in ("meeting_id", "meetingId", "confId"):
            if key in qs and qs[key]:
                return unquote(qs[key][0])
        # /j/<number> or /rec/play|share/... — try to pull an id-ish path segment
        m = _re.search(r"/j/(\d{9,})", u.path)
        if m:
            return m.group(1)
        # a numeric meeting number anywhere
        m = _re.search(r"(\d{9,})", u.path)
        if m:
            return m.group(1)
        # otherwise we can't derive an ID from a share link
        return ""
    # bare id: strip spaces some people paste in meeting numbers
    return s.replace(" ", "")


def _pick_audio_file(files):
    """From a list of Zoom recording_files pick the best one to transcribe:
    prefer a dedicated audio-only (M4A) file, else fall back to the video (MP4)."""
    audio = next((f for f in files if (f.get("file_type") or "").upper() == "M4A"), None)
    if audio:
        return audio
    return next((f for f in files if (f.get("file_type") or "").upper() == "MP4"), None)


async def _transcribe_large_audio(src_path):
    """Compress a downloaded audio/video file to small mono MP3(s) and transcribe.
    If the compressed file is still over the provider's size limit, split it into
    time-based chunks and stitch the segment timestamps back together.
    Requires ffmpeg (provided by the Docker image)."""
    import os as _os
    import tempfile

    workdir = _os.path.dirname(src_path)
    full_mp3 = _os.path.join(workdir, "full.mp3")
    _ffmpeg_to_mp3(src_path, full_mp3)

    size = _os.path.getsize(full_mp3)
    if size <= _CHUNK_SAFETY_BYTES:
        with open(full_mp3, "rb") as f:
            data = f.read()
        return await transcribe_audio_bytes(data, filename="full.mp3")

    # Still too big -> split by time. Estimate chunk length from the MP3 bitrate.
    duration = _ffprobe_duration(full_mp3)
    if duration <= 0:
        # Can't determine duration; try the whole thing and let the caller surface errors.
        with open(full_mp3, "rb") as f:
            data = f.read()
        return await transcribe_audio_bytes(data, filename="full.mp3")

    bytes_per_sec = size / duration
    # target chunk length that stays under the safety limit, with a little headroom
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
        seg = await transcribe_audio_bytes(
            cdata, filename=f"chunk_{idx}.mp3", time_offset=start
        )
        all_segments.extend(seg)
        try:
            _os.remove(chunk_path)
        except OSError:
            pass
        start += this_len
        idx += 1
    return all_segments


async def transcribe_recording_by_id(meeting_id):
    """Full pipeline: find a recording's audio in Zoom, download it, run Whisper,
    store the segments on the recording, and persist. Returns the segment count.
    Handles files larger than the provider's 25 MB limit by compressing with
    ffmpeg and, if still too large, splitting into chunks."""
    import os as _os
    import tempfile
    rec = REC_BY_ID.get(meeting_id)
    if not rec:
        raise LLMUpstreamError("Recording not found.")
    files = await fetch_zoom_recording_files(meeting_id)
    audio = _pick_audio_file(files)
    if not audio:
        raise LLMUpstreamError(
            "No audio/video file is available for this recording in Zoom's cloud "
            "(it may have been deleted)."
        )
    import httpx
    token = await zoom_token()
    url = audio.get("download_url")
    ext = (audio.get("file_extension") or audio.get("file_type") or "m4a").lower()
    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=15.0),
                                 follow_redirects=True) as client:
        r = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        if r.status_code != 200:
            raise LLMUpstreamError(
                f"Could not download recording audio from Zoom ({r.status_code})."
            )
        audio_bytes = r.content

    # Small enough to send directly? Then skip ffmpeg entirely.
    if len(audio_bytes) <= _CHUNK_SAFETY_BYTES:
        segments = await transcribe_audio_bytes(audio_bytes, filename=f"{meeting_id}.{ext}")
    elif _have_ffmpeg():
        # Compress (and if needed split) using a temp working directory.
        with tempfile.TemporaryDirectory() as tmp:
            src_path = _os.path.join(tmp, f"src.{ext}")
            with open(src_path, "wb") as f:
                f.write(audio_bytes)
            segments = await _transcribe_large_audio(src_path)
    else:
        raise LLMUpstreamError(
            "This recording's audio is larger than the 25 MB transcription limit and "
            "ffmpeg is not available on the server to compress it. Deploy the Docker "
            "image (which installs ffmpeg) to transcribe long recordings."
        )

    rec["segments"] = segments
    save_recordings(RECORDINGS)
    # Free the (potentially large) audio buffer and this recording's stale index,
    # then run a GC pass so memory drops back down after a transcription spike.
    try:
        audio_bytes = None
    except Exception:
        pass
    _INDEX_CACHE.pop(meeting_id, None)
    import gc as _gc
    _gc.collect()
    return len(segments)


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
async def ingest_zoom_meeting(obj, allow_whisper_fallback=True):
    """Given a webhook payload's 'object', download its transcript and add a hidden recording.
    When allow_whisper_fallback is False (e.g. bulk backfill), recordings without a Zoom
    .vtt transcript are imported with empty segments and can be transcribed later on demand
    via the 'Generate transcript' button. This keeps bulk imports fast so they don't hit the
    host's request timeout (which shows up as a 502 / 'could not reach the server')."""
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

    # Auto-fallback: no Zoom .vtt transcript, but we have audio/video and an
    # OpenAI key -> transcribe the audio with Whisper so the recording is usable.
    # Skipped during bulk backfill to avoid long-running requests timing out (502).
    if allow_whisper_fallback and not segments and OPENAI_API_KEY:
        audio = _pick_audio_file(files)
        if audio and audio.get("download_url"):
            try:
                token = await zoom_token()
                import httpx
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(600.0, connect=15.0),
                    follow_redirects=True,
                ) as client:
                    ar = await client.get(
                        audio["download_url"],
                        headers={"Authorization": f"Bearer {token}"},
                    )
                if ar.status_code == 200:
                    ext = (audio.get("file_extension")
                           or audio.get("file_type") or "m4a").lower()
                    segments = await transcribe_audio_bytes(
                        ar.content, filename=f"{meeting_id}.{ext}"
                    )
            except Exception as e:
                # Never let a transcription failure block ingest; log and move on.
                print(f"[ingest] whisper fallback failed for {meeting_id}: {e}")
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
        "summary": r.get("summary") or "",
        "topics": r.get("topics") or [],
        # student-safe: only whether notes exist + how many, never their content
        "has_notes": bool(r.get("notes")),
        "notes_count": len(r.get("notes") or []),
        # teacher-only: filenames + sizes for management (no full text)
        "notes": ([
            {"id": n.get("id"), "filename": n.get("filename"),
             "chars": sum(len(c) for c in n.get("chunks", [])),
             "chunks": len(n.get("chunks", []))}
            for n in (r.get("notes") or [])
        ] if include_hidden else None),
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
    courses = [c.strip() for c in (body.courses or "").split(";") if c.strip()]
    # If the email already exists, MERGE the new course(s) into that account
    # instead of rejecting it or creating a duplicate.
    existing = next((s for s in roster if _norm(s.get("email")) == _norm(email)), None)
    if existing:
        prev = existing.get("courses", []) or []
        seen = {_norm(c) for c in prev}
        merged = list(prev)
        added_courses = []
        for c in courses:
            if _norm(c) not in seen:
                merged.append(c); seen.add(_norm(c)); added_courses.append(c)
        existing["courses"] = merged
        # only update name/password when a real value was supplied
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
    # invalidate any active sessions for that student
    for tok in [t for t, v in SESSIONS.items() if v["student_id"] == body.id]:
        SESSIONS.pop(tok, None)
    return {"ok": True}


class ResetPasswordBody(BaseModel):
    passcode: str
    id: str
    new_password: str | None = None  # if omitted, a random one is generated


def _gen_password(n=8):
    # readable, no ambiguous chars
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
    # force re-login: drop any active sessions for this student
    for tok in [t for t, v in SESSIONS.items() if v["student_id"] == body.id]:
        SESSIONS.pop(tok, None)
    # return the new password ONCE so the teacher can share it
    return {"ok": True, "email": student.get("email"), "new_password": new_pw}


class UpdateStudentBody(BaseModel):
    passcode: str
    id: str
    name: str | None = None
    email: str | None = None
    courses: list[str] | None = None      # full replacement list when provided
    new_password: str | None = None        # set only if a non-empty value is given


@app.post("/api/teacher/students/update")
def update_student(body: UpdateStudentBody):
    """Edit any field of a student in one call: name, email, full course list,
    and/or password. Only fields that are provided (non-None) are changed."""
    if not check_passcode(body.passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    roster = load_roster()
    student = next((s for s in roster if s["id"] == body.id), None)
    if not student:
        return JSONResponse({"error": "Student not found"}, status_code=404)

    # email change -> validate uniqueness (case-insensitive), ignoring self
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
        # de-dupe case-insensitively, preserve order
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
    # if email or password changed, drop active sessions so the student re-logs in
    if pw_changed or body.email is not None:
        for tok in [t for t, v in SESSIONS.items() if v.get("student_id") == body.id]:
            SESSIONS.pop(tok, None)
    return {"ok": True, "student": {
        "id": student["id"], "name": student.get("name", ""), "email": student.get("email", ""),
        "courses": student.get("courses", []), "has_password": bool(student.get("password_hash")),
    }}


# ---------- de-duplicate accounts by email ----------
def _find_email_duplicates(roster):
    """Group roster entries by normalized email. Return a dict of
    {normalized_email: [entries in creation order]} for emails with >1 entry."""
    groups = {}
    for s in roster:
        key = _norm(s.get("email"))
        if not key:
            continue
        groups.setdefault(key, []).append(s)
    return {k: v for k, v in groups.items() if len(v) > 1}


def _merge_group_courses(entries):
    """Union all courses across the group's entries, de-duped, order-preserving."""
    seen, merged = set(), []
    for e in entries:
        for c in (e.get("courses") or []):
            if _norm(c) not in seen:
                merged.append(c); seen.add(_norm(c))
    return merged


class DedupeAuth(BaseModel):
    passcode: str


@app.post("/api/teacher/students/dedupe-preview")
def dedupe_preview(body: DedupeAuth):
    """Show which emails have duplicate accounts and what a merge would do.
    Keeps the OLDEST account (first in creation order) as canonical."""
    if not check_passcode(body.passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    roster = load_roster()
    dups = _find_email_duplicates(roster)
    preview = []
    for email, entries in dups.items():
        keep = entries[0]                    # oldest wins
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
    """Merge all duplicate-email accounts: keep the oldest, union its courses with
    the duplicates', and delete the extras. Returns a summary of what changed."""
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
        # if the kept account has no password but a duplicate does, adopt it
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
    # invalidate sessions for any deleted accounts
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
            # MERGE, don't overwrite: add any new courses to the existing account
            # (case-insensitive de-dupe, preserving existing order then appending new).
            existing = s.get("courses", []) or []
            seen = {_norm(c) for c in existing}
            merged = list(existing)
            for c in courses:
                if _norm(c) not in seen:
                    merged.append(c)
                    seen.add(_norm(c))
            s["courses"] = merged
            # Only update name/password if the row actually provides one, so a
            # course-only re-import never wipes an existing name or password.
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
    notes_ctx = notes_context(rec, body.question)
    # Answers are always in English (school policy), regardless of the question's language.
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
    system = (
        "You are ClassMate, a study assistant for students. You answer ONLY using the "
        "provided class recording transcript excerpts and any teacher notes provided. "
        "Each transcript excerpt is prefixed with a timestamp like [12:34] and sometimes a speaker name.\n"
        "RULES:\n"
        "1. Base every claim strictly on the transcript and the teacher notes. Do NOT use outside knowledge.\n"
        "2. When you state something from the recording, cite the timestamp in parentheses, e.g. (at 12:34).\n"
        "3. If the answer is in neither the transcript nor the notes, say clearly that it wasn't covered, and do not invent an answer.\n"
        "4. Be clear, friendly and concise, like a helpful tutor."
        + notes_rules
        + lang_line
    )
    notes_block = f"\n\nTeacher notes for this class:\n{notes_ctx}" if notes_ctx else ""
    user = (
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
    # 3) Handle recording completed — for BOTH meetings and webinars.
    #    Zoom sends different event names depending on the source, e.g.:
    #      meeting: recording.completed / recording.transcript_completed
    #      webinar: webinar.recording_completed (and some accounts still use
    #               recording.completed with a webinar-type object)
    #    We accept any event that ends in a recording-completed variant so a
    #    webinar recording is never silently dropped.
    event = payload.get("event", "")
    print(f"[zoom webhook] received event: {event}")  # visible in Render logs
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
        obj = payload.get("payload", {}).get("object", {})
        try:
            added = await ingest_zoom_meeting(obj)
            print(f"[zoom webhook] ingest for '{obj.get('topic')}' "
                  f"(type={obj.get('type')}, source={_detect_source(obj)}) -> added={added}")
        except Exception as e:
            print("Zoom ingest error:", e)
    return {"ok": True}
# ---------- one-time backfill of EXISTING cloud recordings ----------
# Pulls recordings already stored in Zoom cloud (meetings AND webinars) via the
# Zoom REST API and ingests any that aren't in the app yet, reusing the exact
# same logic as the live webhook. Teacher-authenticated. Safe to run repeatedly
# (already-ingested recordings are skipped by ingest_zoom_meeting).
class BackfillBody(BaseModel):
    passcode: str
    from_date: str | None = None   # "YYYY-MM-DD"; default = 6 months ago
    to_date: str | None = None     # "YYYY-MM-DD"; default = today


async def _list_cloud_recordings(from_date: str, to_date: str):
    """Return a list of Zoom recording 'meeting' objects between the dates.
    Zoom caps each query at ~30 days, so we page month-by-month. This lists the
    account's own recordings for the S2S app user context ('me')."""
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
                    print(f"[backfill] list error {r.status_code}: {r.text[:300]}")
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
            # Fast import: don't run Whisper here (would time out on many recordings).
            # Recordings without a Zoom transcript get empty segments and can be
            # transcribed later via the per-recording "Generate transcript" button.
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
        except Exception as e:
            errors += 1
            print(f"[backfill] ingest error for {m.get('topic')}: {e}")
    print(f"[backfill] range {from_date}..{to_date}: found={len(meetings)} "
          f"added={added} skipped={skipped} errors={errors}")
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
    ref: str   # a Zoom meeting ID / UUID, or a Zoom recording link


@app.post("/api/teacher/import-one")
async def teacher_import_one(body: ImportOneBody):
    """Import a single specific Zoom cloud recording by meeting ID / UUID / link.
    Uses the Zoom .vtt transcript if present; otherwise imports with an empty
    transcript that the teacher can generate on demand (keeps this request fast)."""
    if not check_passcode(body.passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    meeting_id = _parse_meeting_id(body.ref)
    if not meeting_id:
        return JSONResponse(
            {"error": "Couldn't read a meeting ID from that. Paste the Zoom Meeting ID/UUID, "
                      "or a recording link that contains a meeting_id."},
            status_code=400,
        )
    try:
        obj = await fetch_zoom_recording_object(meeting_id)
    except LLMUpstreamError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    except Exception as e:
        return JSONResponse({"error": f"Could not fetch that recording: {e}"}, status_code=502)
    # Already imported?
    existing_id = str(obj.get("id") or obj.get("uuid") or meeting_id)
    if existing_id in REC_BY_ID:
        return JSONResponse(
            {"error": f"That recording is already imported: "
                      f"\"{REC_BY_ID[existing_id].get('display_title')}\"."},
            status_code=409,
        )
    try:
        # Fast import (Zoom transcript only); teacher can Generate transcript after.
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


# ---------- teacher notes endpoints ----------
NOTE_MAX_BYTES = 10 * 1024 * 1024  # 10 MB per file


@app.post("/api/teacher/notes/upload")
async def upload_note(passcode: str = Form(...), id: str = Form(...), file: UploadFile = File(...)):
    """Attach a note file to a recording. The file is parsed to text server-side;
    the original file is NOT stored and is never served to students."""
    if not check_passcode(passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    rec = REC_BY_ID.get(id)
    if not rec:
        return JSONResponse({"error": "not found"}, status_code=404)
    data = await file.read()
    if len(data) > NOTE_MAX_BYTES:
        return JSONResponse({"error": "File is too large (max 10 MB)."}, status_code=400)
    try:
        text = extract_text_from_upload(data, file.filename or "")
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"Could not read that file: {e}"}, status_code=422)
    chunks = chunk_note_text(text)
    if not chunks:
        return JSONResponse({"error": "No readable text found in that file."}, status_code=422)
    note = {
        "id": secrets.token_hex(6),
        "filename": (file.filename or "notes"),
        "chunks": chunks,
    }
    rec.setdefault("notes", []).append(note)
    save_recordings(RECORDINGS)
    return {"ok": True, "id": id, "note": {"id": note["id"], "filename": note["filename"],
            "chars": sum(len(c) for c in chunks), "chunks": len(chunks)},
            "recording": _card(rec, include_hidden=True)}


class DeleteNoteBody(BaseModel):
    passcode: str
    id: str          # recording id
    note_id: str


@app.post("/api/teacher/notes/delete")
def delete_note(body: DeleteNoteBody):
    if not check_passcode(body.passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    rec = REC_BY_ID.get(body.id)
    if not rec:
        return JSONResponse({"error": "not found"}, status_code=404)
    before = len(rec.get("notes") or [])
    rec["notes"] = [n for n in (rec.get("notes") or []) if n.get("id") != body.note_id]
    if len(rec["notes"]) == before:
        return JSONResponse({"error": "note not found"}, status_code=404)
    save_recordings(RECORDINGS)
    return {"ok": True, "recording": _card(rec, include_hidden=True)}


class TranscribeBody(BaseModel):
    passcode: str
    id: str


@app.post("/api/teacher/transcribe")
async def teacher_transcribe(body: TranscribeBody):
    """Generate a transcript for a recording that has none (or re-generate one)
    by downloading its Zoom cloud audio and running Whisper speech-to-text."""
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
        return JSONResponse(
            {"error": "Transcription produced no text (the audio may be silent or unusable)."},
            status_code=422,
        )
    return {"ok": True, "id": body.id, "segments": count,
            "recording": _card(rec, include_hidden=True)}


# ---------- delete recordings ----------
def _remove_recording(rid: str) -> bool:
    """Delete a recording by id from RECORDINGS + index caches. Returns True if removed."""
    global RECORDINGS
    rec = REC_BY_ID.get(rid)
    if not rec:
        return False
    RECORDINGS = [r for r in RECORDINGS if r.get("id") != rid]
    REC_BY_ID.pop(rid, None)
    _INDEX_CACHE.pop(rid, None)
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
    """Delete every recording whose unit is 'Unassigned' (or empty)."""
    if not check_passcode(body.passcode):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    targets = [r["id"] for r in RECORDINGS if (r.get("unit") or "Unassigned") == "Unassigned"]
    for rid in targets:
        _remove_recording(rid)
    if targets:
        save_recordings(RECORDINGS)
    return {"ok": True, "deleted": len(targets), "total_recordings_now": len(RECORDINGS)}


# ---------- summary & key topics ----------
async def generate_summary_and_topics(rec):
    """Use the LLM to produce a short summary + key-topic tags grounded in the
    recording transcript. Stored on the recording so students can browse them."""
    segs = rec.get("segments") or []
    if not segs:
        return None
    # sample across the whole recording for a representative context
    idx = retrieve(rec, rec.get("display_title") or rec.get("topic") or "lecture", k=30, window=1)
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
        return JSONResponse({"error": "This recording has no transcript yet. Generate a transcript first."}, status_code=422)
    try:
        await generate_summary_and_topics(rec)
    except LLMConfigError as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    except Exception as e:
        return JSONResponse({"error": f"Could not generate summary: {e}"}, status_code=500)
    save_recordings(RECORDINGS)
    return {"ok": True, "id": body.id, "summary": rec.get("summary", ""), "topics": rec.get("topics", [])}


# ---------- dashboard stats ----------
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


# ---------- question analytics ----------
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
    # most-asked keywords
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


# ---------- exports ----------
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
    # Minimal dependency-free PDF: render a simple text-based report.
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
    """Build a minimal multi-page PDF from plain text lines with no external deps."""
    def esc(s):
        return (s or "").replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    # paginate
    per_page = 48
    pages = [lines[i:i + per_page] for i in range(0, max(1, len(lines)), per_page)] or [[""]]
    objs = []
    # 1: catalog, 2: pages tree; page objs + content objs follow
    n_pages = len(pages)
    font_obj = 3 + n_pages * 2  # after pages+contents
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
    # content streams
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
    # assemble
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
# ---------- serve frontend ----------
if os.path.isdir(FRONTEND_DIR):
    @app.get("/")
    def index():
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
    app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="static")
