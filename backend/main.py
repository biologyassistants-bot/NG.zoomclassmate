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
BUNDLED_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")[cite: 3]
DATA_DIR = os.environ.get("DATA_DIR", "").strip() or BUNDLED_DATA_DIR[cite: 3]


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
        os.makedirs(DATA_DIR, exist_ok=True)[cite: 3]
        if not os.path.isdir(BUNDLED_DATA_DIR):
            return
        import shutil
        for name in os.listdir(BUNDLED_DATA_DIR):
            src = os.path.join(BUNDLED_DATA_DIR, name)[cite: 3]
            dst = os.path.join(DATA_DIR, name)[cite: 3]
            if os.path.isfile(src) and not os.path.exists(dst):[cite: 3]
                shutil.copy2(src, dst)[cite: 3]
    except Exception as e:
        print(f"[data] seed warning: {e}")[cite: 3]


_seed_data_dir()[cite: 3]

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")[cite: 3]
DATA_PATH = os.path.join(DATA_DIR, "recordings.json")[cite: 3]
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")[cite: 3]
QLOG_PATH = os.path.join(DATA_DIR, "question_log.json")[cite: 3]
ROSTER_PATH = os.path.join(DATA_DIR, "roster.json")[cite: 3]
NOTES_LIB_PATH = os.path.join(DATA_DIR, "notes_library.json")[cite: 3]
# in-memory active student sessions: token -> {student_id, name, courses}
SESSIONS = {}[cite: 3]

# Clean heavy embeddings from persistent disk before loading into memory
_clean_recordings_disk_file(DATA_PATH)


# ---------- shared notes library ----------
# Each note's text is stored ONCE here: {id, filename, chunks:[...], chars}.
# A recording references shared notes by id via rec["note_ids"] = [id, ...].
def load_notes_library():
    if os.path.exists(NOTES_LIB_PATH):[cite: 3]
        with open(NOTES_LIB_PATH) as f:[cite: 3]
            return json.load(f)[cite: 3]
    return [][cite: 3]


def save_notes_library(lib):
    with open(NOTES_LIB_PATH, "w") as f:[cite: 3]
        json.dump(lib, f, ensure_ascii=False, indent=2)[cite: 3]


def note_by_id(note_id, lib=None):
    lib = lib if lib is not None else load_notes_library()[cite: 3]
    return next((n for n in lib if n["id"] == note_id), None)[cite: 3]


def load_roster():
    if os.path.exists(ROSTER_PATH):[cite: 3]
        with open(ROSTER_PATH) as f:[cite: 3]
            return json.load(f)[cite: 3]
    return [][cite: 3]


def save_roster(roster):
    with open(ROSTER_PATH, "w") as f:[cite: 3]
        json.dump(roster, f, ensure_ascii=False, indent=2)[cite: 3]


def gen_pin():
    return f"{secrets.randbelow(10000):04d}"[cite: 3]


def hash_pw(pw: str) -> str:
    # bcrypt only accepts up to 72 bytes; truncate defensively.
    pw_bytes = (pw or "").encode("utf-8")[:72][cite: 3]
    return bcrypt.hashpw(pw_bytes, bcrypt.gensalt()).decode("utf-8")[cite: 3]


def verify_pw(pw: str, hashed: str) -> bool:
    if not hashed:[cite: 3]
        return False[cite: 3]
    try:
        pw_bytes = (pw or "").encode("utf-8")[:72][cite: 3]
        return bcrypt.checkpw(pw_bytes, hashed.encode("utf-8"))[cite: 3]
    except Exception:
        return False[cite: 3]


# default teacher passcode; teacher can change it in the dashboard
DEFAULT_PASSCODE = "teach123"[cite: 3]


def load_config():
    if os.path.exists(CONFIG_PATH):[cite: 3]
        with open(CONFIG_PATH) as f:[cite: 3]
            return json.load(f)[cite: 3]
    cfg = {"passcode": DEFAULT_PASSCODE}[cite: 3]
    with open(CONFIG_PATH, "w") as f:[cite: 3]
        json.dump(cfg, f, ensure_ascii=False, indent=2)[cite: 3]
    return cfg[cite: 3]


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:[cite: 3]
        json.dump(cfg, f, ensure_ascii=False, indent=2)[cite: 3]


def load_qlog():
    if os.path.exists(QLOG_PATH):[cite: 3]
        with open(QLOG_PATH) as f:[cite: 3]
            return json.load(f)[cite: 3]
    return [][cite: 3]


def save_qlog(log):
    with open(QLOG_PATH, "w") as f:[cite: 3]
        json.dump(log, f, ensure_ascii=False, indent=2)[cite: 3]


def save_recordings(recs):
    clean_recs = []
    for r in recs:
        r_copy = dict(r)
        r_copy.pop("embeddings", None)
        clean_recs.append(r_copy)
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(clean_recs, f, ensure_ascii=False, indent=2)


def load_recordings():
    if os.path.exists(DATA_PATH):[cite: 3]
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
    return [][cite: 3]


RECORDINGS = load_recordings()[cite: 3]
REC_BY_ID = {r["id"]: r for r in RECORDINGS}[cite: 3]


app = FastAPI(title="ClassMate API")[cite: 3]
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)[cite: 3]


# ---------------------------------------------------------------------------
# OpenAI key diagnostic endpoint
# ---------------------------------------------------------------------------
def _diag_mask(key: str) -> str:
    if not key:[cite: 3]
        return ""[cite: 3]
    if len(key) <= 10:[cite: 3]
        return key[:2] + "*" * (len(key) - 2)[cite: 3]
    return key[:5] + "..." + key[-4:][cite: 3]


@app.get("/api/diag/openai")
async def diag_openai():
    key = os.environ.get("OPENAI_API_KEY", "").strip()[cite: 3]
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()[cite: 3]
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").strip()[cite: 3]
    report = {
        "env_var_present": bool(key),
        "key_length": len(key),
        "key_masked_preview": _diag_mask(key),
        "model": model,
        "base_url": base,
    }[cite: 3]
    if not key:[cite: 3]
        report["ok"] = False[cite: 3]
        report["message"] = (
            "OPENAI_API_KEY is NOT set (or empty) on this server. Add it under "
            "Render -> Environment and redeploy."
        )[cite: 3]
        return JSONResponse(status_code=200, content=report)[cite: 3]

    import httpx
    try:
        timeout = httpx.Timeout(30.0, connect=10.0)[cite: 3]
        async with httpx.AsyncClient(timeout=timeout) as client:[cite: 3]
            resp = await client.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": model,
                      "messages": [{"role": "user", "content": "ping"}],
                      "max_tokens": 1},
            )[cite: 3]
    except httpx.RequestError as e:[cite: 3]
        report["ok"] = False[cite: 3]
        report["message"] = (f"Network error reaching {base}: {e}. On Render free "
                             "tier this can be a cold-start timeout; retry once warm.")[cite: 3]
        return JSONResponse(status_code=200, content=report)[cite: 3]

    if resp.status_code >= 400:[cite: 3]
        detail = ""[cite: 3]
        try:
            detail = resp.json().get("error", {}).get("message", "")[cite: 3]
        except Exception:
            detail = resp.text[:200][cite: 3]
        report["ok"] = False[cite: 3]
        report["provider_status"] = resp.status_code[cite: 3]
        low = (detail or "").lower()[cite: 3]
        if resp.status_code == 401 or "incorrect api key" in low or "invalid" in low:[cite: 3]
            report["message"] = ("Key REJECTED (invalid/incorrect). Re-copy from "
                                 "platform.openai.com and update OPENAI_API_KEY, then redeploy.")[cite: 3]
        elif resp.status_code == 429 or "quota" in low or "billing" in low:[cite: 3]
            report["message"] = ("Key valid but NO CREDIT / rate-limited. Add "
                                 "billing/credits to the OpenAI account.")[cite: 3]
        elif resp.status_code == 404 or ("model" in low and "not" in low):[cite: 3]
            report["message"] = ("Key works but the model isn't available to this "
                                 "account. Set OPENAI_MODEL to one you can use.")[cite: 3]
        else:
            report["message"] = f"Provider returned {resp.status_code}: {detail[:200]}"[cite: 3]
        return JSONResponse(status_code=200, content=report)[cite: 3]

    report["ok"] = True[cite: 3]
    report["provider_status"] = resp.status_code[cite: 3]
    report["message"] = "OPENAI_API_KEY is set AND the API call succeeded. The key is working."[cite: 3]
    return JSONResponse(status_code=200, content=report)[cite: 3]


def _migrate_inline_notes_to_library():
    """One-time migration: older data stored notes inline on each recording as
    rec['notes'] = [{id, filename, chunks}]. Move them into the shared library and
    replace with rec['note_ids'] = [id,...]. Safe to run every startup (idempotent)."""
    lib = load_notes_library()[cite: 3]
    lib_ids = {n["id"] for n in lib}[cite: 3]
    changed_lib = False[cite: 3]
    changed_recs = False[cite: 3]
    for r in RECORDINGS:[cite: 3]
        inline = r.get("notes")[cite: 3]
        if inline:[cite: 3]
            ids = list(r.get("note_ids") or [])[cite: 3]
            for n in inline:[cite: 3]
                nid = n.get("id") or secrets.token_hex(6)[cite: 3]
                if nid not in lib_ids:[cite: 3]
                    lib.append({"id": nid, "filename": n.get("filename") or "notes",
                                "chunks": n.get("chunks", []),
                                "chars": sum(len(c) for c in n.get("chunks", []))})[cite: 3]
                    lib_ids.add(nid); changed_lib = True[cite: 3]
                if nid not in ids:[cite: 3]
                    ids.append(nid)[cite: 3]
            r["note_ids"] = ids[cite: 3]
            r.pop("notes", None)[cite: 3]
            changed_recs = True[cite: 3]
        elif r.get("note_ids") is None:[cite: 3]
            r["note_ids"] = [][cite: 3]
    if changed_lib:[cite: 3]
        save_notes_library(lib)[cite: 3]
    if changed_recs:[cite: 3]
        save_recordings(RECORDINGS)[cite: 3]


_migrate_inline_notes_to_library()[cite: 3]


def fmt_ts(t):
    """Format a transcript start_time (which may be 'HH:MM:SS' or seconds) as mm:ss / h:mm:ss."""
    if t is None:[cite: 3]
        return "?"[cite: 3]
    s = str(t)[cite: 3]
    if ":" in s:[cite: 3]
        return s.split(".")[0][cite: 3]
    try:
        sec = float(s)[cite: 3]
    except ValueError:
        return s[cite: 3]
    sec = int(sec)[cite: 3]
    h = sec // 3600[cite: 3]
    m = (sec % 3600) // 60[cite: 3]
    ss = sec % 60[cite: 3]
    if h:[cite: 3]
        return f"{h}:{m:02d}:{ss:02d}"[cite: 3]
    return f"{m}:{ss:02d}"[cite: 3]


_word_re = re.compile(r"[A-Za-z0-9\u00c0-\u024f\u0400-\u04ff\u0600-\u06ff]+")[cite: 3]

def tokenize(text):
    return [w.lower() for w in _word_re.findall(text or "")][cite: 3]


# ---------- semantic retrieval (OpenAI Embeddings) ----------
async def get_embedding(text: str) -> list[float]:
    """Fetch a single embedding vector for the student's query."""
    import httpx
    async with httpx.AsyncClient(timeout=30) as client:[cite: 3]
        resp = await client.post(
            f"{OPENAI_BASE_URL}/embeddings",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={"input": text, "model": "text-embedding-3-small"}
        )[cite: 3]
        resp.raise_for_status()[cite: 3]
        return resp.json()["data"][0]["embedding"][cite: 3]


def cosine_similarity(v1, v2):
    """Calculate how closely related two pieces of text are."""
    dot = sum(a * b for a, b in zip(v1, v2))[cite: 3]
    mag = math.sqrt(sum(a * a for a in v1)) * math.sqrt(sum(b * b for b in v2))[cite: 3]
    return dot / mag if mag else 0.0[cite: 3]


async def build_index_async(rec):
    """Fetch embeddings for the entire transcript and cache them in-memory only."""
    if "embeddings" in rec and rec["embeddings"]:[cite: 3]
        return rec["embeddings"][cite: 3]
        
    segs = rec.get("segments", [])[cite: 3]
    texts = [s.get("text", "") for s in segs][cite: 3]
    if not texts:[cite: 3]
        return [][cite: 3]
    
    import httpx
    embeddings = [][cite: 3]
    batch_size = 500
    
    async with httpx.AsyncClient(timeout=60) as client:[cite: 3]
        for i in range(0, len(texts), batch_size):[cite: 3]
            batch = texts[i:i + batch_size][cite: 3]
            resp = await client.post(
                f"{OPENAI_BASE_URL}/embeddings",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                json={"input": batch, "model": "text-embedding-3-small"}
            )[cite: 3]
            resp.raise_for_status()[cite: 3]
            data = resp.json().get("data", [])[cite: 3]
            embeddings.extend([d["embedding"] for d in sorted(data, key=lambda x: x["index"])])[cite: 3]
            
    rec["embeddings"] = embeddings
    return embeddings


async def retrieve(rec, query, k=18, window=1):
    """Find the most relevant transcript segments using semantic similarity."""
    segs = rec.get("segments", [])[cite: 3]
    if not segs:[cite: 3]
        return [][cite: 3]
    
    doc_embeddings = await build_index_async(rec)[cite: 3]
    q_embedding = await get_embedding(query)[cite: 3]
    
    scores = [cosine_similarity(q_embedding, doc_emb) for doc_emb in doc_embeddings][cite: 3]
    ranked = sorted(range(len(segs)), key=lambda i: scores[i], reverse=True)[cite: 3]
    top = [i for i in ranked if scores[i] > 0.3][:k][cite: 3]
    
    if not top:[cite: 3]
        step = max(1, len(segs) // 40)[cite: 3]
        top = list(range(0, len(segs), step))[:40][cite: 3]
        
    chosen = set()[cite: 3]
    for i in top:[cite: 3]
        for j in range(max(0, i - window), min(len(segs), i + window + 1)):[cite: 3]
            chosen.add(j)[cite: 3]
    return sorted(chosen)[cite: 3]


# ---------- teacher notes: extraction + retrieval ----------
def extract_text_from_upload(data: bytes, filename: str) -> str:
    """Extract plain text from an uploaded PDF / DOCX / TXT file (server-side only)."""
    name = (filename or "").lower()[cite: 3]
    if name.endswith(".txt") or name.endswith(".md"):[cite: 3]
        for enc in ("utf-8", "utf-16", "latin-1"):[cite: 3]
            try:
                return data.decode(enc)[cite: 3]
            except Exception:
                continue[cite: 3]
        return data.decode("utf-8", "replace")[cite: 3]
    if name.endswith(".pdf"):[cite: 3]
        from pypdf import PdfReader[cite: 3]
        reader = PdfReader(io.BytesIO(data))[cite: 3]
        return "\n".join((page.extract_text() or "") for page in reader.pages)[cite: 3]
    if name.endswith(".docx"):[cite: 3]
        import docx[cite: 3]
        doc = docx.Document(io.BytesIO(data))[cite: 3]
        return "\n".join(p.text for p in doc.paragraphs)[cite: 3]
    raise ValueError("Unsupported file type. Please upload a PDF, DOCX, TXT or MD file.")[cite: 3]


def chunk_note_text(text: str, target_chars=700):
    """Split note text into paragraph-ish chunks for retrieval."""
    text = re.sub(r"\r\n?", "\n", text or "")[cite: 3]
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()][cite: 3]
    chunks = [][cite: 3]
    buf = ""[cite: 3]
    for p in paras:[cite: 3]
        if len(buf) + len(p) + 1 <= target_chars:[cite: 3]
            buf = f"{buf}\n{p}".strip()[cite: 3]
        else:
            if buf:[cite: 3]
                chunks.append(buf)[cite: 3]
            while len(p) > target_chars:[cite: 3]
                chunks.append(p[:target_chars])[cite: 3]
                p = p[target_chars:][cite: 3]
            buf = p[cite: 3]
    if buf:[cite: 3]
        chunks.append(buf)[cite: 3]
    return [c for c in chunks if c.strip()][cite: 3]


def retrieve_note_chunks(rec, query, k=4):
    """Return the top-k most relevant note chunks for the query."""
    lib = load_notes_library()[cite: 3]
    notes = [note_by_id(nid, lib) for nid in (rec.get("note_ids") or [])][cite: 3]
    notes = [n for n in notes if n][cite: 3]
    entries = [][cite: 3]
    for note in notes:[cite: 3]
        for ch in note.get("chunks", []):[cite: 3]
            entries.append((note.get("filename") or "notes", ch))[cite: 3]
    if not entries:[cite: 3]
        return [][cite: 3]
    docs = [tokenize(t) for (_, t) in entries][cite: 3]
    df = Counter()[cite: 3]
    for d in docs:[cite: 3]
        for w in set(d):[cite: 3]
            df[w] += 1[cite: 3]
    N = len(docs) or 1[cite: 3]
    idf = {w: math.log(1 + N / c) for w, c in df.items()}[cite: 3]
    q = Counter(tokenize(query))[cite: 3]
    scores = [][cite: 3]
    for d in docs:[cite: 3]
        if not d:[cite: 3]
            scores.append(0.0); continue[cite: 3]
        tf = Counter(d)[cite: 3]
        s = 0.0[cite: 3]
        for w, qc in q.items():[cite: 3]
            if w in tf:[cite: 3]
                s += idf.get(w, 0.0) * (tf[w] / len(d)) * qc[cite: 3]
        scores.append(s)[cite: 3]
    ranked = sorted(range(len(entries)), key=lambda i: scores[i], reverse=True)[cite: 3]
    chosen = [i for i in ranked if scores[i] > 0][:k][cite: 3]
    if not chosen:[cite: 3]
        chosen = list(range(min(2, len(entries))))[cite: 3]
    return [{"note_title": entries[i][0], "text": entries[i][1]} for i in chosen][cite: 3]


def notes_context(rec, query, max_chars=8000):
    """Build a labeled notes context block for the LLM prompt."""
    chunks = retrieve_note_chunks(rec, query)[cite: 3]
    if not chunks:[cite: 3]
        return ""[cite: 3]
    out, total = [], 0[cite: 3]
    for c in chunks:[cite: 3]
        block = f'[NOTE: {c["note_title"]}] {c["text"].strip()}'[cite: 3]
        if total + len(block) > max_chars:[cite: 3]
            break[cite: 3]
        out.append(block)[cite: 3]
        total += len(block)[cite: 3]
    return "\n\n".join(out)[cite: 3]


def context_from_indices(rec, indices, max_chars=45000):
    segs = rec.get("segments", [])[cite: 3]
    lines = [][cite: 3]
    total = 0[cite: 3]
    for i in indices:[cite: 3]
        s = segs[i][cite: 3]
        ts = fmt_ts(s.get("start"))[cite: 3]
        spk = s.get("speaker") or ""[cite: 3]
        prefix = f"[{ts}]" + (f" {spk}:" if spk else "")[cite: 3]
        line = f"{prefix} {s.get('text','').strip()}"[cite: 3]
        if total + len(line) > max_chars:[cite: 3]
            break[cite: 3]
        lines.append(line)[cite: 3]
        total += len(line)[cite: 3]
    return "\n".join(lines)[cite: 3]


# ---------- LLM helper ----------
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()[cite: 3]
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()[cite: 3]
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").strip()[cite: 3]


class LLMConfigError(Exception):
    pass[cite: 3]


class LLMUpstreamError(Exception):
    pass[cite: 3]


async def llm(messages, max_tokens=1200, temperature=0.1):
    if OPENAI_API_KEY:[cite: 3]
        import httpx
        timeout = httpx.Timeout(90.0, connect=10.0)[cite: 3]
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:[cite: 3]
                resp = await client.post(
                    f"{OPENAI_BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                    json={
                        "model": OPENAI_MODEL,
                        "messages": messages,
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                    },
                )[cite: 3]
        except httpx.RequestError as e:[cite: 3]
            raise LLMUpstreamError(f"Could not reach AI provider: {e}") from e[cite: 3]

        if resp.status_code >= 400:[cite: 3]
            detail = ""[cite: 3]
            try:
                detail = resp.json().get("error", {}).get("message", "")[cite: 3]
            except Exception:
                detail = resp.text[:300][cite: 3]
            raise LLMUpstreamError(f"AI provider returned {resp.status_code}: {detail or 'unknown error'}")[cite: 3]

        try:
            return resp.json()["choices"][0]["message"]["content"][cite: 3]
        except Exception as e:
            raise LLMUpstreamError(f"Unexpected AI response shape: {e}") from e[cite: 3]

    raise LLMConfigError("The AI features are not configured on this server. Set OPENAI_API_KEY.")[cite: 3]


OPENAI_TRANSCRIBE_MODEL = os.environ.get("OPENAI_TRANSCRIBE_MODEL", "whisper-1").strip()[cite: 3]
WHISPER_MAX_BYTES = 25 * 1000 * 1000[cite: 3]
_CHUNK_SAFETY_BYTES = 24 * 1000 * 1000[cite: 3]


def _have_ffmpeg():
    import shutil
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None[cite: 3]


def _ffprobe_duration(path):
    import subprocess
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=120,
        )[cite: 3]
        return float((out.stdout or "0").strip() or 0.0)[cite: 3]
    except Exception:
        return 0.0[cite: 3]


def _ffmpeg_to_mp3(src_path, dst_path, start=None, duration=None, bitrate="48k"):
    import subprocess
    cmd = ["ffmpeg", "-y", "-v", "quiet"][cite: 3]
    if start is not None:[cite: 3]
        cmd += ["-ss", str(start)][cite: 3]
    cmd += ["-i", src_path][cite: 3]
    if duration is not None:[cite: 3]
        cmd += ["-t", str(duration)][cite: 3]
    cmd += ["-ac", "1", "-ar", "16000", "-b:a", bitrate, dst_path][cite: 3]
    subprocess.run(cmd, check=True, timeout=1800)[cite: 3]


def _fmt_seconds_to_ts(seconds):
    try:
        seconds = float(seconds)[cite: 3]
    except (TypeError, ValueError):
        seconds = 0.0[cite: 3]
    if seconds < 0:[cite: 3]
        seconds = 0.0[cite: 3]
    ms = int(round((seconds - int(seconds)) * 1000))[cite: 3]
    total = int(seconds)[cite: 3]
    h = total // 3600[cite: 3]
    m = (total % 3600) // 60[cite: 3]
    s = total % 60[cite: 3]
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"[cite: 3]


async def transcribe_audio_bytes(audio_bytes, filename="audio.m4a", time_offset=0.0):
    if not OPENAI_API_KEY:[cite: 3]
        raise LLMConfigError("Transcription needs OPENAI_API_KEY set on this server.")[cite: 3]
    import httpx
    timeout = httpx.Timeout(600.0, connect=15.0)[cite: 3]
    files = {"file": (filename, audio_bytes, "application/octet-stream")}[cite: 3]
    english_only = os.environ.get("TRANSCRIBE_ENGLISH_ONLY", "1").strip() != "0"[cite: 3]
    endpoint = "/audio/translations" if english_only else "/audio/transcriptions"[cite: 3]
    data = {
        "model": OPENAI_TRANSCRIBE_MODEL,
        "response_format": "verbose_json",
        "timestamp_granularities[]": "segment",
    }[cite: 3]
    if not english_only:[cite: 3]
        data["language"] = os.environ.get("TRANSCRIBE_LANGUAGE", "").strip() or None[cite: 3]
        data = {k: v for k, v in data.items() if v is not None}[cite: 3]
    async with httpx.AsyncClient(timeout=timeout) as client:[cite: 3]
        resp = await client.post(
            f"{OPENAI_BASE_URL}{endpoint}",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            data=data,
            files=files,
        )[cite: 3]
    resp.raise_for_status()[cite: 3]
    payload = resp.json()[cite: 3]
    segments = [][cite: 3]
    for seg in payload.get("segments", []) or []:[cite: 3]
        text = (seg.get("text") or "").strip()[cite: 3]
        if not text:[cite: 3]
            continue[cite: 3]
        segments.append({
            "start": _fmt_seconds_to_ts(float(seg.get("start", 0)) + time_offset),
            "speaker": "",
            "text": text,
        })[cite: 3]
    if not segments:[cite: 3]
        whole = (payload.get("text") or "").strip()[cite: 3]
        if whole:[cite: 3]
            segments.append({
                "start": _fmt_seconds_to_ts(time_offset),
                "speaker": "",
                "text": whole,
            })[cite: 3]
    return segments[cite: 3]


async def fetch_zoom_recording_files(meeting_id):
    import httpx
    from urllib.parse import quote
    token = await zoom_token()[cite: 3]
    mid = str(meeting_id)[cite: 3]
    needs_double = mid.startswith("/") or "//" in mid or "/" in mid[cite: 3]
    path_id = quote(quote(mid, safe=""), safe="") if needs_double else quote(mid, safe="")[cite: 3]
    async with httpx.AsyncClient(timeout=60) as client:[cite: 3]
        r = await client.get(
            f"https://api.zoom.us/v2/meetings/{path_id}/recordings",
            headers={"Authorization": f"Bearer {token}"},
        )[cite: 3]
        if r.status_code != 200:[cite: 3]
            raise LLMUpstreamError(f"Zoom recordings lookup returned {r.status_code}: {r.text[:300]}")[cite: 3]
        return r.json().get("recording_files", []) or [][cite: 3]


async def fetch_zoom_recording_object(meeting_id):
    import httpx
    from urllib.parse import quote
    token = await zoom_token()[cite: 3]
    mid = str(meeting_id)[cite: 3]
    needs_double = mid.startswith("/") or "//" in mid or "/" in mid[cite: 3]
    path_id = quote(quote(mid, safe=""), safe="") if needs_double else quote(mid, safe="")[cite: 3]
    async with httpx.AsyncClient(timeout=60) as client:[cite: 3]
        r = await client.get(
            f"https://api.zoom.us/v2/meetings/{path_id}/recordings",
            headers={"Authorization": f"Bearer {token}"},
        )[cite: 3]
        if r.status_code == 404:[cite: 3]
            raise LLMUpstreamError("No cloud recording found for that meeting ID.")[cite: 3]
        if r.status_code != 200:[cite: 3]
            raise LLMUpstreamError(f"Zoom lookup returned {r.status_code}: {r.text[:300]}")[cite: 3]
        return r.json()[cite: 3]


def _parse_meeting_id(raw: str) -> str:
    import re as _re
    from urllib.parse import urlparse, parse_qs, unquote
    s = (raw or "").strip()[cite: 3]
    if not s:[cite: 3]
        return ""[cite: 3]
    if s.startswith("http"):[cite: 3]
        u = urlparse(s)[cite: 3]
        qs = parse_qs(u.query)[cite: 3]
        for key in ("meeting_id", "meetingId", "confId"):[cite: 3]
            if key in qs and qs[key]:[cite: 3]
                return unquote(qs[key][0])[cite: 3]
        m = _re.search(r"/j/(\d{9,})", u.path)[cite: 3]
        if m:[cite: 3]
            return m.group(1)[cite: 3]
        m = _re.search(r"(\d{9,})", u.path)[cite: 3]
        if m:[cite: 3]
            return m.group(1)[cite: 3]
        return ""[cite: 3]
    return s.replace(" ", "")[cite: 3]


def _pick_audio_file(files):
    audio = next((f for f in files if (f.get("file_type") or "").upper() == "M4A"), None)[cite: 3]
    if audio:[cite: 3]
        return audio[cite: 3]
    return next((f for f in files if (f.get("file_type") or "").upper() == "MP4"), None)[cite: 3]


async def _transcribe_large_audio(src_path):
    import os as _os
    workdir = _os.path.dirname(src_path)[cite: 3]
    full_mp3 = _os.path.join(workdir, "full.mp3")[cite: 3]
    _ffmpeg_to_mp3(src_path, full_mp3)[cite: 3]

    size = _os.path.getsize(full_mp3)[cite: 3]
    if size <= _CHUNK_SAFETY_BYTES:[cite: 3]
        with open(full_mp3, "rb") as f:[cite: 3]
            data = f.read()[cite: 3]
        return await transcribe_audio_bytes(data, filename="full.mp3")[cite: 3]

    duration = _ffprobe_duration(full_mp3)[cite: 3]
    if duration <= 0:[cite: 3]
        with open(full_mp3, "rb") as f:[cite: 3]
            data = f.read()[cite: 3]
        return await transcribe_audio_bytes(data, filename="full.mp3")[cite: 3]

    bytes_per_sec = size / duration[cite: 3]
    chunk_secs = max(60.0, (_CHUNK_SAFETY_BYTES / bytes_per_sec) * 0.9)[cite: 3]

    all_segments = [][cite: 3]
    start = 0.0[cite: 3]
    idx = 0[cite: 3]
    while start < duration:[cite: 3]
        this_len = min(chunk_secs, duration - start)[cite: 3]
        chunk_path = _os.path.join(workdir, f"chunk_{idx}.mp3")[cite: 3]
        _ffmpeg_to_mp3(src_path, chunk_path, start=start, duration=this_len)[cite: 3]
        with open(chunk_path, "rb") as f:[cite: 3]
            cdata = f.read()[cite: 3]
        seg = await transcribe_audio_bytes(cdata, filename=f"chunk_{idx}.mp3", time_offset=start)[cite: 3]
        all_segments.extend(seg)[cite: 3]
        try:
            _os.remove(chunk_path)[cite: 3]
        except OSError:
            pass
        start += this_len[cite: 3]
        idx += 1[cite: 3]
    return all_segments[cite: 3]


async def transcribe_recording_by_id(meeting_id):
    import os as _os
    import tempfile
    rec = REC_BY_ID.get(meeting_id)[cite: 3]
    if not rec:[cite: 3]
        raise LLMUpstreamError("Recording not found.")[cite: 3]
    files = await fetch_zoom_recording_files(meeting_id)[cite: 3]
    audio = _pick_audio_file(files)[cite: 3]
    if not audio:[cite: 3]
        raise LLMUpstreamError("No audio/video file is available for this recording in Zoom's cloud.")[cite: 3]
    import httpx
    token = await zoom_token()[cite: 3]
    url = audio.get("download_url")[cite: 3]
    ext = (audio.get("file_extension") or audio.get("file_type") or "m4a").lower()[cite: 3]
    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=15.0), follow_redirects=True) as client:[cite: 3]
        r = await client.get(url, headers={"Authorization": f"Bearer {token}"})[cite: 3]
        if r.status_code != 200:[cite: 3]
            raise LLMUpstreamError(f"Could not download audio from Zoom ({r.status_code}).")[cite: 3]
        audio_bytes = r.content[cite: 3]

    if len(audio_bytes) <= _CHUNK_SAFETY_BYTES:[cite: 3]
        segments = await transcribe_audio_bytes(audio_bytes, filename=f"{meeting_id}.{ext}")[cite: 3]
    elif _have_ffmpeg():[cite: 3]
        with tempfile.TemporaryDirectory() as tmp:[cite: 3]
            src_path = _os.path.join(tmp, f"src.{ext}")[cite: 3]
            with open(src_path, "wb") as f:[cite: 3]
                f.write(audio_bytes)[cite: 3]
            segments = await _transcribe_large_audio(src_path)[cite: 3]
    else:
        raise LLMUpstreamError("Audio file exceeds 25 MB and ffmpeg is unavailable.")[cite: 3]

    rec["segments"] = segments[cite: 3]
    save_recordings(RECORDINGS)[cite: 3]
    try:
        audio_bytes = None
    except Exception:
        pass
    rec.pop("embeddings", None)[cite: 3]
    gc.collect()[cite: 3]
    return len(segments)[cite: 3]


# ---------- Zoom integration ----------
ZOOM_ACCOUNT_ID     = os.environ.get("ZOOM_ACCOUNT_ID", "").strip()[cite: 3]
ZOOM_CLIENT_ID      = os.environ.get("ZOOM_CLIENT_ID", "").strip()[cite: 3]
ZOOM_CLIENT_SECRET  = os.environ.get("ZOOM_CLIENT_SECRET", "").strip()[cite: 3]
ZOOM_WEBHOOK_SECRET = os.environ.get("ZOOM_WEBHOOK_SECRET", "").strip()[cite: 3]
_zoom_tok = {"token": None, "exp": 0}[cite: 3]


async def zoom_token():
    if _zoom_tok["token"] and _zoom_tok["exp"] > time.time():[cite: 3]
        return _zoom_tok["token"][cite: 3]
    creds = base64.b64encode(f"{ZOOM_CLIENT_ID}:{ZOOM_CLIENT_SECRET}".encode()).decode()[cite: 3]
    import httpx
    async with httpx.AsyncClient(timeout=30) as client:[cite: 3]
        r = await client.post(
            "https://zoom.us/oauth/token",
            headers={"Authorization": f"Basic {creds}"},
            params={"grant_type": "account_credentials", "account_id": ZOOM_ACCOUNT_ID},
        )[cite: 3]
        r.raise_for_status()[cite: 3]
        d = r.json()[cite: 3]
    _zoom_tok["token"] = d["access_token"][cite: 3]
    _zoom_tok["exp"] = time.time() + d.get("expires_in", 3600) - 60[cite: 3]
    return _zoom_tok["token"][cite: 3]


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
    segments = [][cite: 3]
    blocks = re.split(r"\n\s*\n", text.strip())[cite: 3]
    for b in blocks:[cite: 3]
        lines = [l for l in b.splitlines() if l.strip()][cite: 3]
        if not lines:[cite: 3]
            continue[cite: 3]
        tline_i = next((i for i, l in enumerate(lines) if "-->" in l), None)[cite: 3]
        if tline_i is None:[cite: 3]
            continue[cite: 3]
        start = lines[tline_i].split("-->")[0].strip().split(".")[0][cite: 3]
        body = " ".join(lines[tline_i + 1:]).strip()[cite: 3]
        speaker = ""[cite: 3]
        m = re.match(r"^([^:]{1,40}):\s*(.*)$", body)[cite: 3]
        if m:[cite: 3]
            speaker, body = m.group(1).strip(), m.group(2).strip()[cite: 3]
        if body:[cite: 3]
            segments.append({"start": start, "speaker": speaker, "text": body})[cite: 3]
    return segments[cite: 3]


def _detect_source(obj):
    t = obj.get("type")[cite: 3]
    try:
        if int(t) in (5, 6, 9):[cite: 3]
            return "webinar"[cite: 3]
    except (TypeError, ValueError):
        if isinstance(t, str) and "webinar" in t.lower():[cite: 3]
            return "webinar"[cite: 3]
    return "meeting"[cite: 3]


async def ingest_zoom_meeting(obj, allow_whisper_fallback=True):
    meeting_id = str(obj.get("id") or obj.get("uuid") or secrets.token_hex(6))[cite: 3]
    
    # If the recording already exists AND has transcripts, skip
    if meeting_id in REC_BY_ID and len(REC_BY_ID[meeting_id].get("segments", [])) > 0:
        return False

    topic = obj.get("topic", "Untitled class")[cite: 3]
    start_time = (obj.get("start_time") or "")[:10][cite: 3]
    source = _detect_source(obj)[cite: 3]
    files = obj.get("recording_files", [])[cite: 3]

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
    )[cite: 3]
    
    segments = [][cite: 3]
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

    if allow_whisper_fallback and not segments and OPENAI_API_KEY:[cite: 3]
        audio = _pick_audio_file(files)[cite: 3]
        if audio and audio.get("download_url"):[cite: 3]
            try:
                token = await zoom_token()[cite: 3]
                import httpx
                async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=15.0), follow_redirects=True) as client:[cite: 3]
                    ar = await client.get(audio["download_url"], headers={"Authorization": f"Bearer {token}"})[cite: 3]
                if ar.status_code == 200:[cite: 3]
                    ext = (audio.get("file_extension") or audio.get("file_type") or "m4a").lower()[cite: 3]
                    segments = await transcribe_audio_bytes(ar.content, filename=f"{meeting_id}.{ext}")[cite: 3]
            except Exception as e:
                print(f"[ingest] whisper fallback failed for {meeting_id}: {e}")[cite: 3]
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
    }[cite: 3]
    RECORDINGS.append(new_rec)[cite: 3]
    REC_BY_ID[meeting_id] = new_rec[cite: 3]
    save_recordings(RECORDINGS)[cite: 3]
    print(f"[zoom] Successfully imported '{topic}' with {len(segments)} lines.")
    return True[cite: 3]


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
    }[cite: 3]


def _card_notes_meta(r):
    lib = load_notes_library()[cite: 3]
    out = [][cite: 3]
    for nid in (r.get("note_ids") or []):[cite: 3]
        n = note_by_id(nid, lib)[cite: 3]
        if n:[cite: 3]
            out.append({"id": n["id"], "filename": n.get("filename"),
                        "chars": n.get("chars", sum(len(c) for c in n.get("chunks", []))),
                        "chunks": len(n.get("chunks", []))})[cite: 3]
    return out[cite: 3]


class RecListBody(BaseModel):
    token: str | None = None[cite: 3]


@app.post("/api/recordings")
def list_recordings(body: RecListBody):
    sess = valid_session(body.token)[cite: 3]
    if not sess:[cite: 3]
        return JSONResponse({"error": "Your session has expired. Please log in again."}, status_code=401)[cite: 3]
    my_courses = sess.get("courses", [])[cite: 3]
    def allowed(r):
        if not r.get("visible", True):[cite: 3]
            return False[cite: 3]
        if not my_courses:[cite: 3]
            return False[cite: 3]
        return (r.get("unit") or "Unassigned") in my_courses[cite: 3]
    def _date_key(r):
        return (r.get("date") or "9999-12-31 23:59:59")[cite: 3]
    allowed_recs = sorted([r for r in RECORDINGS if allowed(r)], key=_date_key)[cite: 3]
    out = [_card(r) for r in allowed_recs][cite: 3]
    units = [][cite: 3]
    seen = set()[cite: 3]
    for r in allowed_recs:[cite: 3]
        u = r.get("unit") or "Unassigned"[cite: 3]
        if u not in seen:[cite: 3]
            seen.add(u)[cite: 3]
            units.append(u)[cite: 3]
    return {"recordings": out, "units": units}[cite: 3]


class LoginBody(BaseModel):
    passcode: str[cite: 3]


def check_passcode(passcode: str) -> bool:
    return passcode == load_config().get("passcode")[cite: 3]


@app.post("/api/teacher/login")
def teacher_login(body: LoginBody):
    if check_passcode(body.passcode):[cite: 3]
        return {"ok": True}[cite: 3]
    return JSONResponse({"ok": False, "error": "Wrong passcode"}, status_code=401)[cite: 3]


def _norm(s):
    return (s or "").strip().lower()[cite: 3]


def normalize_courses(value):
    if value is None:[cite: 3]
        return [][cite: 3]
    if isinstance(value, str):[cite: 3]
        parts = re.split(r"[;,]", value)[cite: 3]
    elif isinstance(value, (list, tuple)):[cite: 3]
        parts = [][cite: 3]
        for v in value:[cite: 3]
            if isinstance(v, str):[cite: 3]
                parts.extend(re.split(r"[;,]", v))[cite: 3]
            elif v is not None:[cite: 3]
                parts.append(str(v))[cite: 3]
    else:
        return [][cite: 3]
    seen, out = set(), [][cite: 3]
    for p in parts:[cite: 3]
        p = (p or "").strip()[cite: 3]
        if p and p.lower() not in seen:[cite: 3]
            out.append(p); seen.add(p.lower())[cite: 3]
    return out[cite: 3]


def _repair_roster_courses():
    try:
        roster = load_roster()[cite: 3]
        changed = False[cite: 3]
        for s in roster:[cite: 3]
            fixed = normalize_courses(s.get("courses"))[cite: 3]
            if fixed != s.get("courses"):[cite: 3]
                s["courses"] = fixed[cite: 3]
                changed = True[cite: 3]
        if changed:[cite: 3]
            save_roster(roster)[cite: 3]
            print("[roster] normalized course lists on startup")[cite: 3]
    except Exception as e:
        print(f"[roster] repair warning: {e}")[cite: 3]


_repair_roster_courses()[cite: 3]


class StudentLoginBody(BaseModel):
    email: str[cite: 3]
    password: str[cite: 3]


@app.post("/api/student/login")
def student_login(body: StudentLoginBody):
    roster = load_roster()[cite: 3]
    for st in roster:[cite: 3]
        if _norm(st.get("email")) == _norm(body.email) and verify_pw(body.password, st.get("password_hash")):[cite: 3]
            token = secrets.token_urlsafe(24)[cite: 3]
            SESSIONS[token] = {
                "student_id": st["id"],
                "name": st.get("name") or st.get("email"),
                "courses": normalize_courses(st.get("courses")),
            }[cite: 3]
            return {"ok": True, "token": token, "name": SESSIONS[token]["name"]}[cite: 3]
    return JSONResponse(
        {"ok": False, "error": "That email and password don't match our class roster. Check with your teacher."},
        status_code=401,
    )[cite: 3]


def valid_session(token: str):
    return SESSIONS.get(token or "")[cite: 3]


# ---------- Cross-Device Sync Endpoints ----------
class StudentSyncBody(BaseModel):
    token: str[cite: 3]
    study_plan: list | None = None[cite: 3]
    student_stats: dict | None = None[cite: 3]
    chat_history: dict | None = None
    flashcard_deck: list | None = None


@app.post("/api/student/sync")
def sync_student_data(body: StudentSyncBody):
    sess = valid_session(body.token)[cite: 3]
    if not sess:[cite: 3]
        return JSONResponse({"error": "Unauthorized"}, status_code=401)[cite: 3]
    roster = load_roster()[cite: 3]
    student = next((s for s in roster if s["id"] == sess["student_id"]), None)[cite: 3]
    if student:
        if body.study_plan is not None:[cite: 3]
            student["study_plan"] = body.study_plan[cite: 3]
        if body.student_stats is not None:[cite: 3]
            student["student_stats"] = body.student_stats[cite: 3]
        if body.chat_history is not None:
            student["chat_history"] = body.chat_history
        if body.flashcard_deck is not None:
            student["flashcard_deck"] = body.flashcard_deck
        save_roster(roster)[cite: 3]
    return {"ok": True}[cite: 3]


@app.post("/api/student/profile")
def get_student_profile(body: RecListBody):
    sess = valid_session(body.token)[cite: 3]
    if not sess:[cite: 3]
        return JSONResponse({"error": "Unauthorized"}, status_code=401)[cite: 3]
    roster = load_roster()[cite: 3]
    student = next((s for s in roster if s["id"] == sess["student_id"]), None)[cite: 3]
    if not student:[cite: 3]
        return JSONResponse({"error": "Student not found"}, status_code=404)[cite: 3]
    return {
        "study_plan": student.get("study_plan"),[cite: 3]
        "student_stats": student.get("student_stats"),[cite: 3]
        "chat_history": student.get("chat_history", {}),
        "flashcard_deck": student.get("flashcard_deck", [])
    }


# ---------- Teacher Roster Management ----------
class RosterAuth(BaseModel):
    passcode: str[cite: 3]


@app.post("/api/teacher/students")
def list_students(body: RosterAuth):
    if not check_passcode(body.passcode):[cite: 3]
        return JSONResponse({"error": "unauthorized"}, status_code=401)[cite: 3]
    safe = [{
        "id": s["id"],
        "name": s.get("name", ""),
        "email": s.get("email", ""),
        "courses": normalize_courses(s.get("courses")),
        "has_password": bool(s.get("password_hash")),
    } for s in load_roster()][cite: 3]
    return {"students": safe}[cite: 3]


class AddStudentBody(BaseModel):
    passcode: str[cite: 3]
    name: str[cite: 3]
    email: str | None = ""[cite: 3]
    password: str | None = ""[cite: 3]
    courses: str | None = ""[cite: 3]


@app.post("/api/teacher/students/add")
def add_student(body: AddStudentBody):
    if not check_passcode(body.passcode):[cite: 3]
        return JSONResponse({"error": "unauthorized"}, status_code=401)[cite: 3]
    name = body.name.strip()[cite: 3]
    email = (body.email or "").strip()[cite: 3]
    if not email:[cite: 3]
        return JSONResponse({"error": "Email required"}, status_code=400)[cite: 3]
    roster = load_roster()[cite: 3]
    courses = [c.strip() for c in (body.courses or "").split(";") if c.strip()][cite: 3]
    existing = next((s for s in roster if _norm(s.get("email")) == _norm(email)), None)[cite: 3]
    if existing:[cite: 3]
        prev = normalize_courses(existing.get("courses"))[cite: 3]
        seen = {_norm(c) for c in prev}[cite: 3]
        merged = list(prev)[cite: 3]
        added_courses = [][cite: 3]
        for c in courses:[cite: 3]
            if _norm(c) not in seen:[cite: 3]
                merged.append(c); seen.add(_norm(c)); added_courses.append(c)[cite: 3]
        existing["courses"] = merged[cite: 3]
        if name and name != email.split("@")[0]:[cite: 3]
            existing["name"] = name[cite: 3]
        if (body.password or "").strip():[cite: 3]
            existing["password_hash"] = hash_pw(body.password)[cite: 3]
        save_roster(roster)[cite: 3]
        if added_courses:[cite: 3]
            msg = f"Added course(s) {', '.join(added_courses)} to existing student {existing.get('email')}."[cite: 3]
        else:
            msg = f"{existing.get('email')} already had those course(s); nothing to add."[cite: 3]
        return {"ok": True, "merged": True, "message": msg, "student": {
            "id": existing["id"], "name": existing.get("name"), "email": existing.get("email"),
            "courses": existing.get("courses", []), "has_password": bool(existing.get("password_hash")),
        }}[cite: 3]
    student = {
        "id": secrets.token_hex(6),
        "name": name or email.split("@")[0],
        "email": email,
        "courses": courses,
        "password_hash": hash_pw(body.password) if (body.password or "").strip() else "",
    }[cite: 3]
    roster.append(student)[cite: 3]
    save_roster(roster)[cite: 3]
    return {"ok": True, "merged": False, "student": {
        "id": student["id"], "name": student["name"], "email": student["email"],
        "courses": student["courses"], "has_password": bool(student["password_hash"]),
    }}[cite: 3]


class RemoveStudentBody(BaseModel):
    passcode: str[cite: 3]
    id: str[cite: 3]


@app.post("/api/teacher/students/remove")
def remove_student(body: RemoveStudentBody):
    if not check_passcode(body.passcode):[cite: 3]
        return JSONResponse({"error": "unauthorized"}, status_code=401)[cite: 3]
    roster = [s for s in load_roster() if s["id"] != body.id][cite: 3]
    save_roster(roster)[cite: 3]
    for tok in [t for t, v in SESSIONS.items() if v["student_id"] == body.id]:[cite: 3]
        SESSIONS.pop(tok, None)[cite: 3]
    return {"ok": True}[cite: 3]


class ResetPasswordBody(BaseModel):
    passcode: str[cite: 3]
    id: str[cite: 3]
    new_password: str | None = None[cite: 3]


def _gen_password(n=8):
    alphabet = "abcdefghijkmnpqrstuvwxyz23456789"[cite: 3]
    return "".join(secrets.choice(alphabet) for _ in range(n))[cite: 3]


@app.post("/api/teacher/students/reset-password")
def reset_student_password(body: ResetPasswordBody):
    if not check_passcode(body.passcode):[cite: 3]
        return JSONResponse({"error": "unauthorized"}, status_code=401)[cite: 3]
    roster = load_roster()[cite: 3]
    student = next((s for s in roster if s["id"] == body.id), None)[cite: 3]
    if not student:[cite: 3]
        return JSONResponse({"error": "Student not found"}, status_code=404)[cite: 3]
    new_pw = (body.new_password or "").strip() or _gen_password()[cite: 3]
    student["password_hash"] = hash_pw(new_pw)[cite: 3]
    save_roster(roster)[cite: 3]
    for tok in [t for t, v in SESSIONS.items() if v["student_id"] == body.id]:[cite: 3]
        SESSIONS.pop(tok, None)[cite: 3]
    return {"ok": True, "email": student.get("email"), "new_password": new_pw}[cite: 3]


class UpdateStudentBody(BaseModel):
    passcode: str[cite: 3]
    id: str[cite: 3]
    name: str | None = None[cite: 3]
    email: str | None = None[cite: 3]
    courses: list[str] | None = None[cite: 3]
    new_password: str | None = None[cite: 3]


@app.post("/api/teacher/students/update")
def update_student(body: UpdateStudentBody):
    if not check_passcode(body.passcode):[cite: 3]
        return JSONResponse({"error": "unauthorized"}, status_code=401)[cite: 3]
    roster = load_roster()[cite: 3]
    student = next((s for s in roster if s["id"] == body.id), None)[cite: 3]
    if not student:[cite: 3]
        return JSONResponse({"error": "Student not found"}, status_code=404)[cite: 3]

    if body.email is not None:[cite: 3]
        new_email = body.email.strip()[cite: 3]
        if not new_email:[cite: 3]
            return JSONResponse({"error": "Email can't be empty."}, status_code=400)[cite: 3]
        clash = any(_norm(s.get("email")) == _norm(new_email) and s["id"] != body.id for s in roster)[cite: 3]
        if clash:[cite: 3]
            return JSONResponse({"error": "Another student already uses that email."}, status_code=400)[cite: 3]
        student["email"] = new_email[cite: 3]

    if body.name is not None:[cite: 3]
        student["name"] = body.name.strip() or (student.get("email") or "").split("@")[0][cite: 3]

    if body.courses is not None:[cite: 3]
        seen, cleaned = set(), [][cite: 3]
        for c in body.courses:[cite: 3]
            c = (c or "").strip()[cite: 3]
            if c and _norm(c) not in seen:[cite: 3]
                cleaned.append(c); seen.add(_norm(c))[cite: 3]
        student["courses"] = cleaned[cite: 3]

    pw_changed = False[cite: 3]
    if body.new_password is not None and body.new_password.strip():[cite: 3]
        student["password_hash"] = hash_pw(body.new_password.strip())[cite: 3]
        pw_changed = True[cite: 3]

    save_roster(roster)[cite: 3]
    if pw_changed or body.email is not None:[cite: 3]
        for tok in [t for t, v in SESSIONS.items() if v.get("student_id") == body.id]:[cite: 3]
            SESSIONS.pop(tok, None)[cite: 3]
    return {"ok": True, "student": {
        "id": student["id"], "name": student.get("name", ""), "email": student.get("email", ""),
        "courses": student.get("courses", []), "has_password": bool(student.get("password_hash")),
    }}[cite: 3]


# ---------- de-duplicate accounts by email ----------
def _find_email_duplicates(roster):
    groups = {}[cite: 3]
    for s in roster:[cite: 3]
        key = _norm(s.get("email"))[cite: 3]
        if not key:[cite: 3]
            continue[cite: 3]
        groups.setdefault(key, []).append(s)[cite: 3]
    return {k: v for k, v in groups.items() if len(v) > 1}[cite: 3]


def _merge_group_courses(entries):
    seen, merged = set(), [][cite: 3]
    for e in entries:[cite: 3]
        for c in normalize_courses(e.get("courses")):[cite: 3]
            if _norm(c) not in seen:[cite: 3]
                merged.append(c); seen.add(_norm(c))[cite: 3]
    return merged[cite: 3]


class DedupeAuth(BaseModel):
    passcode: str[cite: 3]


@app.post("/api/teacher/students/dedupe-preview")
def dedupe_preview(body: DedupeAuth):
    if not check_passcode(body.passcode):[cite: 3]
        return JSONResponse({"error": "unauthorized"}, status_code=401)[cite: 3]
    roster = load_roster()[cite: 3]
    dups = _find_email_duplicates(roster)[cite: 3]
    preview = [][cite: 3]
    for email, entries in dups.items():[cite: 3]
        keep = entries[0][cite: 3]
        remove = entries[1:][cite: 3]
        preview.append({
            "email": keep.get("email"),
            "duplicate_count": len(entries),
            "keep": {"id": keep["id"], "name": keep.get("name"),
                     "courses": keep.get("courses", []),
                     "has_password": bool(keep.get("password_hash"))},
            "will_delete": [{"id": e["id"], "name": e.get("name"),
                             "courses": e.get("courses", [])} for e in remove],
            "merged_courses": _merge_group_courses(entries),
        })[cite: 3]
    return {
        "duplicate_emails": len(dups),
        "accounts_to_delete": sum(len(e) - 1 for e in dups.values()),
        "groups": preview,
    }[cite: 3]


@app.post("/api/teacher/students/dedupe-apply")
def dedupe_apply(body: DedupeAuth):
    if not check_passcode(body.passcode):[cite: 3]
        return JSONResponse({"error": "unauthorized"}, status_code=401)[cite: 3]
    roster = load_roster()[cite: 3]
    dups = _find_email_duplicates(roster)[cite: 3]
    if not dups:[cite: 3]
        return {"ok": True, "merged_emails": 0, "deleted_accounts": 0, "message": "No duplicates found."}[cite: 3]
    delete_ids = set()[cite: 3]
    merged_emails = 0[cite: 3]
    for email, entries in dups.items():[cite: 3]
        keep = entries[0][cite: 3]
        keep["courses"] = _merge_group_courses(entries)[cite: 3]
        if not keep.get("password_hash"):[cite: 3]
            for e in entries[1:]:[cite: 3]
                if e.get("password_hash"):[cite: 3]
                    keep["password_hash"] = e["password_hash"][cite: 3]
                    break[cite: 3]
        for e in entries[1:]:[cite: 3]
            delete_ids.add(e["id"])[cite: 3]
        merged_emails += 1[cite: 3]
    new_roster = [s for s in roster if s["id"] not in delete_ids][cite: 3]
    save_roster(new_roster)[cite: 3]
    for tok in [t for t, v in SESSIONS.items() if v.get("student_id") in delete_ids]:[cite: 3]
        SESSIONS.pop(tok, None)[cite: 3]
    return {
        "ok": True,
        "merged_emails": merged_emails,
        "deleted_accounts": len(delete_ids),
        "total_now": len(new_roster),
    }[cite: 3]


@app.post("/api/teacher/students/import")
async def import_students(passcode: str = Form(...), file: UploadFile = File(...)):
    if not check_passcode(passcode):[cite: 3]
        return JSONResponse({"error": "unauthorized"}, status_code=401)[cite: 3]
    try:
        import openpyxl[cite: 3]
        content = await file.read()[cite: 3]
        wb = openpyxl.load_workbook(io.BytesIO(content))[cite: 3]
        ws = wb.active[cite: 3]
        rows = list(ws.iter_rows(values_only=True))[cite: 3]
    except Exception as e:
        return JSONResponse({"error": f"Could not read the Excel file: {e}"}, status_code=400)[cite: 3]
    if not rows:[cite: 3]
        return JSONResponse({"error": "The sheet is empty."}, status_code=400)[cite: 3]
    header = [str(h).strip().lower() if h is not None else "" for h in rows[0]][cite: 3]
    def col(name):
        return header.index(name) if name in header else -1[cite: 3]
    ei, pi, ni, ci = col("email"), col("password"), col("name"), col("courses")[cite: 3]
    if ei < 0 or pi < 0:[cite: 3]
        return JSONResponse({"error": "The sheet must have 'email' and 'password' columns."}, status_code=400)[cite: 3]
    roster = load_roster()[cite: 3]
    by_email = {_norm(s.get("email")): s for s in roster if s.get("email")}[cite: 3]
    added = updated = 0[cite: 3]
    for row in rows[1:]:[cite: 3]
        if not row or ei >= len(row) or not row[ei]:[cite: 3]
            continue[cite: 3]
        email = str(row[ei]).strip()[cite: 3]
        pw = str(row[pi]).strip() if pi < len(row) and row[pi] else ""[cite: 3]
        name = str(row[ni]).strip() if ni >= 0 and ni < len(row) and row[ni] else email.split("@")[0][cite: 3]
        courses = [][cite: 3]
        if ci >= 0 and ci < len(row) and row[ci]:[cite: 3]
            courses = [c.strip() for c in str(row[ci]).split(";") if c.strip()][cite: 3]
        key = _norm(email)[cite: 3]
        if key in by_email:[cite: 3]
            s = by_email[key][cite: 3]
            existing = normalize_courses(s.get("courses"))[cite: 3]
            seen = {_norm(c) for c in existing}[cite: 3]
            merged = list(existing)[cite: 3]
            for c in courses:[cite: 3]
                if _norm(c) not in seen:[cite: 3]
                    merged.append(c)[cite: 3]
                    seen.add(_norm(c))[cite: 3]
            s["courses"] = merged[cite: 3]
            if name and name != email.split("@")[0]:[cite: 3]
                s["name"] = name[cite: 3]
            if pw:[cite: 3]
                s["password_hash"] = hash_pw(pw)[cite: 3]
            updated += 1[cite: 3]
        else:
            roster.append({
                "id": secrets.token_hex(6),
                "name": name,
                "email": email,
                "courses": courses,
                "password_hash": hash_pw(pw) if pw else "",
            })[cite: 3]
            added += 1[cite: 3]
    save_roster(roster)[cite: 3]
    return {"ok": True, "added": added, "updated": updated, "total": len(roster)}[cite: 3]


# ---------- teacher endpoints ----------
class TeacherAuth(BaseModel):
    passcode: str[cite: 3]


@app.post("/api/teacher/recordings")
def teacher_recordings(body: TeacherAuth):
    if not check_passcode(body.passcode):[cite: 3]
        return JSONResponse({"error": "unauthorized"}, status_code=401)[cite: 3]
    out = [_card(r, include_hidden=True) for r in RECORDINGS][cite: 3]
    units = sorted({(r.get("unit") or "Unassigned") for r in RECORDINGS})[cite: 3]
    return {"recordings": out, "units": units}[cite: 3]


class UpdateRecBody(BaseModel):
    passcode: str[cite: 3]
    id: str[cite: 3]
    display_title: str | None = None[cite: 3]
    visible: bool | None = None[cite: 3]
    unit: str | None = None[cite: 3]


@app.post("/api/teacher/update")
def teacher_update(body: UpdateRecBody):
    if not check_passcode(body.passcode):[cite: 3]
        return JSONResponse({"error": "unauthorized"}, status_code=401)[cite: 3]
    rec = REC_BY_ID.get(body.id)[cite: 3]
    if not rec:[cite: 3]
        return JSONResponse({"error": "not found"}, status_code=404)[cite: 3]
    if body.display_title is not None and body.display_title.strip():[cite: 3]
        rec["display_title"] = body.display_title.strip()[cite: 3]
    if body.visible is not None:[cite: 3]
        rec["visible"] = body.visible[cite: 3]
    if body.unit is not None and body.unit.strip():[cite: 3]
        rec["unit"] = body.unit.strip()[cite: 3]
    save_recordings(RECORDINGS)[cite: 3]
    return {"ok": True, "recording": _card(rec, include_hidden=True)}[cite: 3]


class PasscodeBody(BaseModel):
    passcode: str[cite: 3]
    new_passcode: str[cite: 3]


ALLOWED_LOGO_EXT = {"png": "png", "jpg": "jpg", "jpeg": "jpg", "webp": "webp", "gif": "gif", "svg": "svg"}[cite: 3]
LOGO_MAX_BYTES = 2 * 1024 * 1024[cite: 3]
LOGO_MIME = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp",
             "gif": "image/gif", "svg": "image/svg+xml"}[cite: 3]
LOGO_PATH_BASE = os.path.join(DATA_DIR, "logo")[cite: 3]


def _current_logo_file():
    for e in set(ALLOWED_LOGO_EXT.values()):[cite: 3]
        p = f"{LOGO_PATH_BASE}.{e}"[cite: 3]
        if os.path.exists(p):[cite: 3]
            return p, e[cite: 3]
    return None, None[cite: 3]


def _migrate_frontend_logo_to_disk():
    try:
        existing, _ = _current_logo_file()[cite: 3]
        if existing:[cite: 3]
            return
        for e in set(ALLOWED_LOGO_EXT.values()):[cite: 3]
            fe = os.path.join(FRONTEND_DIR, f"logo.{e}")[cite: 3]
            if os.path.exists(fe):[cite: 3]
                import shutil[cite: 3]
                shutil.copy2(fe, f"{LOGO_PATH_BASE}.{e}")[cite: 3]
                break[cite: 3]
    except Exception as ex:
        print(f"[logo] migrate warning: {ex}")[cite: 3]


_migrate_frontend_logo_to_disk()[cite: 3]


@app.post("/api/teacher/logo")
async def upload_logo(passcode: str = Form(...), file: UploadFile = File(...)):
    if not check_passcode(passcode):[cite: 3]
        return JSONResponse({"error": "unauthorized"}, status_code=401)[cite: 3]
    ext = (file.filename or "").rsplit(".", 1)[-1].lower() if "." in (file.filename or "") else ""[cite: 3]
    if ext not in ALLOWED_LOGO_EXT:[cite: 3]
        return JSONResponse({"error": "Please upload a PNG, JPG, WEBP, GIF or SVG image."}, status_code=400)[cite: 3]
    data = await file.read()[cite: 3]
    if len(data) > LOGO_MAX_BYTES:[cite: 3]
        return JSONResponse({"error": "Image is too large (max 2 MB)."}, status_code=400)[cite: 3]
    save_ext = ALLOWED_LOGO_EXT[ext][cite: 3]
    os.makedirs(DATA_DIR, exist_ok=True)[cite: 3]
    for e in set(ALLOWED_LOGO_EXT.values()):[cite: 3]
        old = f"{LOGO_PATH_BASE}.{e}"[cite: 3]
        if os.path.exists(old):[cite: 3]
            try:
                os.remove(old)[cite: 3]
            except OSError:
                pass
    with open(f"{LOGO_PATH_BASE}.{save_ext}", "wb") as f:[cite: 3]
        f.write(data)[cite: 3]
    cfg = load_config()[cite: 3]
    cfg["logo"] = "/logo"[cite: 3]
    cfg["logo_ext"] = save_ext[cite: 3]
    save_config(cfg)[cite: 3]
    return {"ok": True, "logo": "/logo"}[cite: 3]


@app.get("/logo")
def get_logo():
    path, e = _current_logo_file()[cite: 3]
    if not path:[cite: 3]
        return JSONResponse({"error": "no logo"}, status_code=404)[cite: 3]
    return FileResponse(path, media_type=LOGO_MIME.get(e, "application/octet-stream"))[cite: 3]


@app.get("/api/branding")
def branding():
    path, _ = _current_logo_file()[cite: 3]
    return {"logo": "/logo" if path else ""}[cite: 3]


@app.post("/api/teacher/passcode")
def change_passcode(body: PasscodeBody):
    if not check_passcode(body.passcode):[cite: 3]
        return JSONResponse({"error": "unauthorized"}, status_code=401)[cite: 3]
    if not body.new_passcode.strip():[cite: 3]
        return JSONResponse({"error": "empty passcode"}, status_code=400)[cite: 3]
    cfg = load_config()[cite: 3]
    cfg["passcode"] = body.new_passcode.strip()[cite: 3]
    save_config(cfg)[cite: 3]
    return {"ok": True}[cite: 3]


@app.post("/api/teacher/questions")
def teacher_questions(body: TeacherAuth):
    if not check_passcode(body.passcode):[cite: 3]
        return JSONResponse({"error": "unauthorized"}, status_code=401)[cite: 3]
    log = load_qlog()[cite: 3]
    return {"questions": list(reversed(log))[:500]}[cite: 3]


class AskBody(BaseModel):
    recording_id: str[cite: 3]
    question: str[cite: 3]
    language: str | None = None[cite: 3]
    token: str | None = None[cite: 3]


@app.post("/api/ask")
async def ask(body: AskBody):
    sess = valid_session(body.token)[cite: 3]
    if not sess:[cite: 3]
        return JSONResponse({"error": "Your session has expired. Please log in again."}, status_code=401)[cite: 3]
    rec = REC_BY_ID.get(body.recording_id)[cite: 3]
    if not rec:[cite: 3]
        return JSONResponse({"error": "Recording not found"}, status_code=404)[cite: 3]
    
    idx = await retrieve(rec, body.question)[cite: 3]
    ctx = context_from_indices(rec, idx)[cite: 3]
    notes_ctx = notes_context(rec, body.question)[cite: 3]
    
    lang_line = "\nAlways respond in English, even if the student's question is written in another language."[cite: 3]
    notes_rules = ""[cite: 3]
    if notes_ctx:[cite: 3]
        notes_rules = (
            "\n5. You also have TEACHER NOTES, shown as blocks prefixed with [NOTE: filename]. "
            "These are extra study material for this class. You MAY use them to answer.\n"
            "6. When you use information from the notes, quote the relevant part in \"quotation marks\" "
            "and attribute it, e.g. According to the class notes: \"...\".\n"
            "7. NEVER reproduce a note in full or dump large portions verbatim — quote only the parts "
            "directly relevant to the question. The notes are not downloadable by students."
        )[cite: 3]

    course_name = rec.get('unit') or "Unassigned Course"[cite: 3]

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
    )[cite: 3]

    notes_block = f"\n\nTeacher notes for this class:\n{notes_ctx}" if notes_ctx else ""[cite: 3]
    user = (
        f"Course: {course_name}\n"
        f"Class recording: {rec.get('display_title') or rec.get('topic')}\n\n"
        f"Transcript excerpts:\n{ctx}"
        f"{notes_block}\n\n"
        f"Student question: {body.question}"
    )[cite: 3]
    
    try:
        answer = await llm(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=1000,
        )[cite: 3]
    except (LLMConfigError, LLMUpstreamError) as e:[cite: 3]
        return JSONResponse({"error": str(e)}, status_code=503)[cite: 3]
    
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        log = load_qlog()[cite: 3]
        log.append({
            "student": sess["name"],
            "recording_id": rec["id"],
            "recording_title": rec.get("display_title") or rec.get("topic"),
            "unit": rec.get("unit") or "Unassigned",
            "question": body.question,
            "answer": answer,
            "time": datetime.now(ZoneInfo("Africa/Cairo")).strftime("%Y-%m-%d %H:%M"),
        })[cite: 3]
        save_qlog(log[-1000:])[cite: 3]
    except Exception:
        pass
    return {"answer": answer, "cited_segments": len(idx)}[cite: 3]


class QuizBody(BaseModel):
    recording_id: str[cite: 3]
    num_questions: int = 5[cite: 3]
    language: str | None = None[cite: 3]
    difficulty: str | None = "mixed"[cite: 3]
    token: str | None = None[cite: 3]


@app.post("/api/quiz")
async def quiz(body: QuizBody):
    if not valid_session(body.token):[cite: 3]
        return JSONResponse({"error": "Your session has expired. Please log in again."}, status_code=401)[cite: 3]
    rec = REC_BY_ID.get(body.recording_id)[cite: 3]
    if not rec:[cite: 3]
        return JSONResponse({"error": "Recording not found"}, status_code=404)[cite: 3]
    segs = rec.get("segments", [])[cite: 3]
    step = max(1, len(segs) // 60)[cite: 3]
    idx = list(range(0, len(segs), step))[cite: 3]
    ctx = context_from_indices(rec, idx, max_chars=60000)[cite: 3]
    lang_line = "Write the quiz in English."[cite: 3]
    n = max(1, min(10, body.num_questions))[cite: 3]
    system = (
        "You are ClassMate, creating a quiz to help students review a class recording. "
        "Use ONLY the transcript content provided. Return STRICT JSON only, no markdown, no prose. "
        "Schema: {\"questions\":[{\"question\":str,\"options\":[str,str,str,str],"
        "\"answer_index\":int,\"explanation\":str,\"timestamp\":str}]}. "
        "The 'timestamp' is the transcript timestamp (like '12:34') where the topic is discussed. "
        "The 'explanation' must reference what was said in the recording. "
        f"Create exactly {n} multiple-choice questions ({body.difficulty} difficulty). {lang_line}"
    )[cite: 3]
    user = (
        f"Class recording: {rec.get('display_title') or rec.get('topic')}\n\n"
        f"Transcript excerpts:\n{ctx}"
    )[cite: 3]
    try:
        raw = await llm(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=2500,
        )[cite: 3]
    except (LLMConfigError, LLMUpstreamError) as e:[cite: 3]
        return JSONResponse({"error": str(e)}, status_code=503)[cite: 3]
    data = None[cite: 3]
    try:
        data = json.loads(raw)[cite: 3]
    except Exception:
        m = re.search(r"\{.*\}", raw, re.DOTALL)[cite: 3]
        if m:[cite: 3]
            try:
                data = json.loads(m.group(0))[cite: 3]
            except Exception:
                data = None[cite: 3]
    if not data or "questions" not in data:[cite: 3]
        return JSONResponse({"error": "Could not generate quiz", "raw": raw[:500]}, status_code=500)[cite: 3]
    return data[cite: 3]


# ---------- automated flashcards generation (Fresh & Unique Cards) ----------
class FlashcardBody(BaseModel):
    recording_id: str[cite: 3]
    existing_fronts: list[str] | None = []
    token: str | None = None[cite: 3]


@app.post("/api/flashcards")
async def generate_flashcards(body: FlashcardBody):
    sess = valid_session(body.token)[cite: 3]
    if not sess:[cite: 3]
        return JSONResponse({"error": "Your session has expired. Please log in again."}, status_code=401)[cite: 3]
    
    rec = REC_BY_ID.get(body.recording_id)[cite: 3]
    if not rec:[cite: 3]
        return JSONResponse({"error": "Recording not found"}, status_code=404)[cite: 3]

    transcript_text = "\n".join([f"[{s.get('timestamp','')}] {s.get('text','')}" for s in rec.get("segments", [])])[cite: 3]
    notes_text = notes_context(rec, "flashcards review summary", max_chars=8000)[cite: 3]
    
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
    )[cite: 3]
    
    try:
        raw = await llm(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=1500,
            temperature=0.7,
        )[cite: 3]
    except (LLMConfigError, LLMUpstreamError) as e:[cite: 3]
        return JSONResponse({"error": str(e)}, status_code=503)[cite: 3]

    data = None[cite: 3]
    txt = (raw or "").strip()[cite: 3]
    if txt.startswith("```"):[cite: 3]
        txt = txt.strip("`")[cite: 3]
        if "\n" in txt:[cite: 3]
            txt = txt.split("\n", 1)[-1][cite: 3]
            
    try:
        start = txt.find("{")[cite: 3]
        end = txt.rfind("}")[cite: 3]
        if start != -1 and end != -1:[cite: 3]
            data = json.loads(txt[start:end+1])[cite: 3]
    except Exception:
        data = None[cite: 3]
                
    if not data or "flashcards" not in data:[cite: 3]
        return JSONResponse({"error": "Could not generate flashcards.", "raw": raw[:500]}, status_code=500)[cite: 3]
        
    return data[cite: 3]


# ---------- study plan generation (100% Mandatory Coverage & Dynamic Allocation) ----------
class StudyPlanBody(BaseModel):
    recording_ids: list[str][cite: 3]
    days: int[cite: 3]
    hours_per_day: float[cite: 3]
    focus: str[cite: 3]
    token: str | None = None[cite: 3]


@app.post("/api/student/plan")
async def generate_study_plan(body: StudyPlanBody):
    if not valid_session(body.token):[cite: 3]
        return JSONResponse({"error": "Your session has expired. Please log in again."}, status_code=401)[cite: 3]
    
    if not body.recording_ids:[cite: 3]
        return JSONResponse({"error": "Please select at least one class to study."}, status_code=400)[cite: 3]

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

    if not selected_recs:[cite: 3]
        return JSONResponse({"error": "Selected recordings not found."}, status_code=404)[cite: 3]

    recs_text = "\n".join(selected_recs)[cite: 3]
    
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
    except (LLMConfigError, LLMUpstreamError) as e:[cite: 3]
        return JSONResponse({"error": str(e)}, status_code=503)[cite: 3]

    data = None[cite: 3]
    txt = (raw or "").strip()[cite: 3]
    if txt.startswith("```"):[cite: 3]
        txt = txt.strip("`")[cite: 3]
        if "\n" in txt:[cite: 3]
            txt = txt.split("\n", 1)[-1][cite: 3]
            
    try:
        start = txt.find("{")[cite: 3]
        end = txt.rfind("}")[cite: 3]
        if start != -1 and end != -1:[cite: 3]
            data = json.loads(txt[start:end+1])[cite: 3]
    except Exception as e:
        print(f"[Study Plan Error] Could not parse JSON: {e}")[cite: 3]
                
    if not data or "plan" not in data:[cite: 3]
        return JSONResponse({"error": "Could not generate the plan. Please try again.", "raw": raw[:500]}, status_code=500)[cite: 3]
        
    return data[cite: 3]


# ---------- Zoom webhook ----------
@app.post("/api/zoom/webhook")
async def zoom_webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()[cite: 3]
    payload = await request.json()[cite: 3]
    
    if payload.get("event") == "endpoint.url_validation":[cite: 3]
        plain = payload["payload"]["plainToken"][cite: 3]
        sig = hmac.new(ZOOM_WEBHOOK_SECRET.encode(), plain.encode(), hashlib.sha256).hexdigest()[cite: 3]
        return {"plainToken": plain, "encryptedToken": sig}[cite: 3]
        
    ts = request.headers.get("x-zm-request-timestamp", "")[cite: 3]
    got = request.headers.get("x-zm-signature", "")[cite: 3]
    message = f"v0:{ts}:{body.decode('utf-8')}".encode()[cite: 3]
    expected = "v0=" + hmac.new(ZOOM_WEBHOOK_SECRET.encode(), message, hashlib.sha256).hexdigest()[cite: 3]
    
    if not hmac.compare_digest(expected, got):[cite: 3]
        return JSONResponse({"error": "bad signature"}, status_code=401)[cite: 3]
        
    event = payload.get("event", "")[cite: 3]
    print(f"[zoom webhook] received event: {event}")[cite: 3]
    
    recording_events = {
        "recording.completed",
        "recording.transcript_completed",
        "webinar.recording_completed",
        "webinar.recording_transcript_completed",
    }[cite: 3]
    
    is_recording_event = (
        event in recording_events
        or ("recording" in event and ("completed" in event or "transcript" in event))
    )[cite: 3]
    
    if is_recording_event:
        p_load = payload.get("payload", {})[cite: 3]
        obj = p_load.get("object", {}) or p_load.get("webinar", {})[cite: 3]
        
        if not obj.get("id") and not obj.get("uuid"):[cite: 3]
            obj = p_load.get("object", {})[cite: 3]
            
        if obj.get("id") or obj.get("uuid"):[cite: 3]
            background_tasks.add_task(ingest_zoom_meeting, obj)[cite: 3]
            print(f"[zoom webhook] queued background ingest for webinar/meeting: '{obj.get('topic')}'")[cite: 3]
        else:
            print(f"[zoom webhook warning] could not extract meeting/webinar ID from payload: {payload}")[cite: 3]
        
    return {"ok": True}[cite: 3]


class BackfillBody(BaseModel):
    passcode: str[cite: 3]
    from_date: str | None = None[cite: 3]
    to_date: str | None = None[cite: 3]


async def _list_cloud_recordings(from_date: str, to_date: str):
    import httpx
    from datetime import datetime, timedelta

    token = await zoom_token()[cite: 3]
    results = [][cite: 3]
    start = datetime.strptime(from_date, "%Y-%m-%d")[cite: 3]
    end = datetime.strptime(to_date, "%Y-%m-%d")[cite: 3]
    async with httpx.AsyncClient(timeout=60) as client:[cite: 3]
        window_start = start[cite: 3]
        while window_start <= end:[cite: 3]
            window_end = min(window_start + timedelta(days=29), end)[cite: 3]
            next_token = ""[cite: 3]
            while True:[cite: 3]
                params = {
                    "from": window_start.strftime("%Y-%m-%d"),
                    "to": window_end.strftime("%Y-%m-%d"),
                    "page_size": 300,
                }[cite: 3]
                if next_token:[cite: 3]
                    params["next_page_token"] = next_token[cite: 3]
                r = await client.get(
                    "https://api.zoom.us/v2/users/me/recordings",
                    headers={"Authorization": f"Bearer {token}"},
                    params=params,
                )[cite: 3]
                if r.status_code != 200:[cite: 3]
                    break[cite: 3]
                data = r.json()[cite: 3]
                results.extend(data.get("meetings", []))[cite: 3]
                next_token = data.get("next_page_token") or ""[cite: 3]
                if not next_token:[cite: 3]
                    break[cite: 3]
            window_start = window_end + timedelta(days=1)[cite: 3]
    return results[cite: 3]


@app.post("/api/teacher/backfill")
async def teacher_backfill(body: BackfillBody):
    if not check_passcode(body.passcode):[cite: 3]
        return JSONResponse({"error": "unauthorized"}, status_code=401)[cite: 3]
    from datetime import datetime, timedelta
    to_date = body.to_date or datetime.utcnow().strftime("%Y-%m-%d")[cite: 3]
    from_date = body.from_date or (datetime.utcnow() - timedelta(days=180)).strftime("%Y-%m-%d")[cite: 3]
    try:
        meetings = await _list_cloud_recordings(from_date, to_date)[cite: 3]
    except Exception as e:
        return JSONResponse({"error": f"Could not list cloud recordings: {e}"}, status_code=502)[cite: 3]
    added = 0[cite: 3]
    skipped = 0[cite: 3]
    errors = 0[cite: 3]
    details = [][cite: 3]
    for m in meetings:[cite: 3]
        try:
            was_added = await ingest_zoom_meeting(m, allow_whisper_fallback=False)[cite: 3]
            if was_added:[cite: 3]
                added += 1[cite: 3]
                details.append({
                    "topic": m.get("topic"),
                    "date": (m.get("start_time") or "")[:10],
                    "source": _detect_source(m),
                })[cite: 3]
            else:
                skipped += 1[cite: 3]
        except Exception:
            errors += 1[cite: 3]
    return {
        "ok": True,
        "range": {"from": from_date, "to": to_date},
        "found": len(meetings),
        "added": added,
        "skipped_already_present": skipped,
        "errors": errors,
        "added_recordings": details,
        "total_recordings_now": len(RECORDINGS),
    }[cite: 3]


class ImportOneBody(BaseModel):
    passcode: str[cite: 3]
    ref: str[cite: 3]


@app.post("/api/teacher/import-one")
async def teacher_import_one(body: ImportOneBody):
    if not check_passcode(body.passcode):[cite: 3]
        return JSONResponse({"error": "unauthorized"}, status_code=401)[cite: 3]
    meeting_id = _parse_meeting_id(body.ref)[cite: 3]
    if not meeting_id:[cite: 3]
        return JSONResponse(
            {"error": "Couldn't read a meeting ID from that. Paste the Zoom Meeting ID/UUID, or a recording link."},
            status_code=400,
        )[cite: 3]
    try:
        obj = await fetch_zoom_recording_object(meeting_id)[cite: 3]
    except LLMUpstreamError as e:[cite: 3]
        return JSONResponse({"error": str(e)}, status_code=502)[cite: 3]
    except Exception as e:
        return JSONResponse({"error": f"Could not fetch that recording: {e}"}, status_code=502)[cite: 3]
    
    existing_id = str(obj.get("id") or obj.get("uuid") or meeting_id)[cite: 3]
    if existing_id in REC_BY_ID:[cite: 3]
        return JSONResponse(
            {"error": f"That recording is already imported: \"{REC_BY_ID[existing_id].get('display_title')}\"."},
            status_code=409,
        )[cite: 3]
    try:
        added = await ingest_zoom_meeting(obj, allow_whisper_fallback=False)[cite: 3]
    except Exception as e:
        return JSONResponse({"error": f"Import failed: {e}"}, status_code=500)[cite: 3]
    if not added:[cite: 3]
        return JSONResponse({"error": "That recording is already imported."}, status_code=409)[cite: 3]
    rec = REC_BY_ID.get(existing_id)[cite: 3]
    return {
        "ok": True,
        "recording": _card(rec, include_hidden=True) if rec else None,
        "has_transcript": bool(rec and rec.get("segments")),
        "total_recordings_now": len(RECORDINGS),
    }[cite: 3]


NOTE_MAX_UPLOAD_BYTES = 60 * 1024 * 1024[cite: 3]
NOTE_MAX_TEXT_CHARS = 2 * 1024 * 1024[cite: 3]


@app.post("/api/teacher/notes/upload")
async def upload_note(passcode: str = Form(...), id: str = Form(...), file: UploadFile = File(...)):
    if not check_passcode(passcode):[cite: 3]
        return JSONResponse({"error": "unauthorized"}, status_code=401)[cite: 3]
    rec = REC_BY_ID.get(id)[cite: 3]
    if not rec:[cite: 3]
        return JSONResponse({"error": "not found"}, status_code=404)[cite: 3]
    data = await file.read()[cite: 3]
    if len(data) > NOTE_MAX_UPLOAD_BYTES:[cite: 3]
        return JSONResponse({"error": "File is unusually large."}, status_code=400)[cite: 3]
    try:
        text = extract_text_from_upload(data, file.filename or "")[cite: 3]
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)[cite: 3]
    except Exception as e:
        return JSONResponse({"error": f"Could not read that file: {e}"}, status_code=422)[cite: 3]
    
    original_len = len(text)[cite: 3]
    trimmed = False[cite: 3]
    if original_len > NOTE_MAX_TEXT_CHARS:[cite: 3]
        text = text[:NOTE_MAX_TEXT_CHARS][cite: 3]
        trimmed = True[cite: 3]
    chunks = chunk_note_text(text)[cite: 3]
    if not chunks:[cite: 3]
        return JSONResponse({"error": "No readable text found in that file."}, status_code=422)[cite: 3]
    kept = sum(len(c) for c in chunks)[cite: 3]
    
    lib = load_notes_library()[cite: 3]
    note = {"id": secrets.token_hex(6), "filename": (file.filename or "notes"),
            "chunks": chunks, "chars": kept}[cite: 3]
    lib.append(note)[cite: 3]
    save_notes_library(lib)[cite: 3]
    ids = list(rec.get("note_ids") or [])[cite: 3]
    if note["id"] not in ids:[cite: 3]
        ids.append(note["id"])[cite: 3]
    rec["note_ids"] = ids[cite: 3]
    save_recordings(RECORDINGS)[cite: 3]
    return {"ok": True, "id": id,
            "note": {"id": note["id"], "filename": note["filename"], "chars": kept, "chunks": len(chunks)},
            "file_bytes": len(data), "text_chars": kept, "trimmed": trimmed,
            "recording": _card(rec, include_hidden=True)}[cite: 3]


class ListLibraryBody(BaseModel):
    passcode: str[cite: 3]
    for_recording: str | None = None[cite: 3]


@app.post("/api/teacher/notes/library")
def notes_library(body: ListLibraryBody):
    if not check_passcode(body.passcode):[cite: 3]
        return JSONResponse({"error": "unauthorized"}, status_code=401)[cite: 3]
    lib = load_notes_library()[cite: 3]
    usage = {}[cite: 3]
    for r in RECORDINGS:[cite: 3]
        for nid in (r.get("note_ids") or []):[cite: 3]
            usage[nid] = usage.get(nid, 0) + 1[cite: 3]

    allowed_ids = None[cite: 3]
    if body.for_recording:[cite: 3]
        target = REC_BY_ID.get(body.for_recording)[cite: 3]
        target_unit = (target.get("unit") or "Unassigned") if target else "Unassigned"[cite: 3]
        if target_unit != "Unassigned":[cite: 3]
            allowed_ids = set()[cite: 3]
            for r in RECORDINGS:[cite: 3]
                if (r.get("unit") or "Unassigned") == target_unit:[cite: 3]
                    for nid in (r.get("note_ids") or []):[cite: 3]
                        allowed_ids.add(nid)[cite: 3]

    out = [][cite: 3]
    for n in lib:[cite: 3]
        if allowed_ids is not None and n["id"] not in allowed_ids:[cite: 3]
            continue[cite: 3]
        out.append({"id": n["id"], "filename": n.get("filename"),
                    "chars": n.get("chars", sum(len(c) for c in n.get("chunks", []))),
                    "used_by": usage.get(n["id"], 0)})[cite: 3]
    return {"library": out}[cite: 3]


class AttachNoteBody(BaseModel):
    passcode: str[cite: 3]
    id: str[cite: 3]
    note_id: str[cite: 3]


@app.post("/api/teacher/notes/attach")
def attach_note(body: AttachNoteBody):
    if not check_passcode(body.passcode):[cite: 3]
        return JSONResponse({"error": "unauthorized"}, status_code=401)[cite: 3]
    rec = REC_BY_ID.get(body.id)[cite: 3]
    if not rec:[cite: 3]
        return JSONResponse({"error": "recording not found"}, status_code=404)[cite: 3]
    if not note_by_id(body.note_id):[cite: 3]
        return JSONResponse({"error": "note not found in library"}, status_code=404)[cite: 3]
    ids = list(rec.get("note_ids") or [])[cite: 3]
    if body.note_id in ids:[cite: 3]
        return JSONResponse({"error": "That note is already attached to this recording."}, status_code=409)[cite: 3]
    ids.append(body.note_id)[cite: 3]
    rec["note_ids"] = ids[cite: 3]
    save_recordings(RECORDINGS)[cite: 3]
    return {"ok": True, "recording": _card(rec, include_hidden=True)}[cite: 3]


class DetachNoteBody(BaseModel):
    passcode: str[cite: 3]
    id: str[cite: 3]
    note_id: str[cite: 3]


@app.post("/api/teacher/notes/detach")
def detach_note(body: DetachNoteBody):
    if not check_passcode(body.passcode):[cite: 3]
        return JSONResponse({"error": "unauthorized"}, status_code=401)[cite: 3]
    rec = REC_BY_ID.get(body.id)[cite: 3]
    if not rec:[cite: 3]
        return JSONResponse({"error": "not found"}, status_code=404)[cite: 3]
    ids = list(rec.get("note_ids") or [])[cite: 3]
    if body.note_id not in ids:[cite: 3]
        return JSONResponse({"error": "note not attached"}, status_code=404)[cite: 3]
    rec["note_ids"] = [x for x in ids if x != body.note_id][cite: 3]
    save_recordings(RECORDINGS)[cite: 3]
    return {"ok": True, "recording": _card(rec, include_hidden=True)}[cite: 3]


class DeleteLibraryNoteBody(BaseModel):
    passcode: str[cite: 3]
    note_id: str[cite: 3]


@app.post("/api/teacher/notes/library/delete")
def delete_library_note(body: DeleteLibraryNoteBody):
    if not check_passcode(body.passcode):[cite: 3]
        return JSONResponse({"error": "unauthorized"}, status_code=401)[cite: 3]
    lib = load_notes_library()[cite: 3]
    if not note_by_id(body.note_id, lib):[cite: 3]
        return JSONResponse({"error": "note not found"}, status_code=404)[cite: 3]
    lib = [n for n in lib if n["id"] != body.note_id][cite: 3]
    save_notes_library(lib)[cite: 3]
    detached_from = 0[cite: 3]
    for r in RECORDINGS:[cite: 3]
        ids = r.get("note_ids") or [][cite: 3]
        if body.note_id in ids:[cite: 3]
            r["note_ids"] = [x for x in ids if x != body.note_id][cite: 3]
            detached_from += 1[cite: 3]
    if detached_from:[cite: 3]
        save_recordings(RECORDINGS)[cite: 3]
    return {"ok": True, "detached_from": detached_from}[cite: 3]


class DeleteNoteBody(BaseModel):
    passcode: str[cite: 3]
    id: str[cite: 3]
    note_id: str[cite: 3]


@app.post("/api/teacher/notes/delete")
def delete_note(body: DeleteNoteBody):
    if not check_passcode(body.passcode):[cite: 3]
        return JSONResponse({"error": "unauthorized"}, status_code=401)[cite: 3]
    rec = REC_BY_ID.get(body.id)[cite: 3]
    if not rec:[cite: 3]
        return JSONResponse({"error": "not found"}, status_code=404)[cite: 3]
    ids = list(rec.get("note_ids") or [])[cite: 3]
    if body.note_id not in ids:[cite: 3]
        return JSONResponse({"error": "note not found"}, status_code=404)[cite: 3]
    rec["note_ids"] = [x for x in ids if x != body.note_id][cite: 3]
    save_recordings(RECORDINGS)[cite: 3]
    return {"ok": True, "recording": _card(rec, include_hidden=True)}[cite: 3]


class TranscribeBody(BaseModel):
    passcode: str[cite: 3]
    id: str[cite: 3]


@app.post("/api/teacher/transcribe")
async def teacher_transcribe(body: TranscribeBody):
    if not check_passcode(body.passcode):[cite: 3]
        return JSONResponse({"error": "unauthorized"}, status_code=401)[cite: 3]
    rec = REC_BY_ID.get(body.id)[cite: 3]
    if not rec:[cite: 3]
        return JSONResponse({"error": "not found"}, status_code=404)[cite: 3]
    try:
        count = await transcribe_recording_by_id(body.id)[cite: 3]
    except LLMConfigError as e:[cite: 3]
        return JSONResponse({"error": str(e)}, status_code=503)[cite: 3]
    except LLMUpstreamError as e:[cite: 3]
        return JSONResponse({"error": str(e)}, status_code=502)[cite: 3]
    except Exception as e:
        return JSONResponse({"error": f"Transcription failed: {e}"}, status_code=500)[cite: 3]
    if count == 0:[cite: 3]
        return JSONResponse({"error": "Transcription produced no text."}, status_code=422)[cite: 3]
    return {"ok": True, "id": body.id, "segments": count, "recording": _card(rec, include_hidden=True)}[cite: 3]


def _remove_recording(rid: str) -> bool:
    global RECORDINGS[cite: 3]
    rec = REC_BY_ID.get(rid)[cite: 3]
    if not rec:[cite: 3]
        return False[cite: 3]
    RECORDINGS = [r for r in RECORDINGS if r.get("id") != rid][cite: 3]
    REC_BY_ID.pop(rid, None)[cite: 3]
    return True[cite: 3]


class DeleteRecBody(BaseModel):
    passcode: str[cite: 3]
    id: str[cite: 3]


@app.post("/api/teacher/recordings/delete")
def teacher_delete_recording(body: DeleteRecBody):
    if not check_passcode(body.passcode):[cite: 3]
        return JSONResponse({"error": "unauthorized"}, status_code=401)[cite: 3]
    if not _remove_recording(body.id):[cite: 3]
        return JSONResponse({"error": "not found"}, status_code=404)[cite: 3]
    save_recordings(RECORDINGS)[cite: 3]
    return {"ok": True, "id": body.id, "total_recordings_now": len(RECORDINGS)}[cite: 3]


class DeleteUnassignedBody(BaseModel):
    passcode: str[cite: 3]


@app.post("/api/teacher/recordings/delete-unassigned")
def teacher_delete_unassigned(body: DeleteUnassignedBody):
    if not check_passcode(body.passcode):[cite: 3]
        return JSONResponse({"error": "unauthorized"}, status_code=401)[cite: 3]
    targets = [r["id"] for r in RECORDINGS if (r.get("unit") or "Unassigned") == "Unassigned"][cite: 3]
    for rid in targets:[cite: 3]
        _remove_recording(rid)[cite: 3]
    if targets:[cite: 3]
        save_recordings(RECORDINGS)[cite: 3]
    return {"ok": True, "deleted": len(targets), "total_recordings_now": len(RECORDINGS)}[cite: 3]


async def generate_summary_and_topics(rec):
    segs = rec.get("segments") or [][cite: 3]
    if not segs:[cite: 3]
        return None[cite: 3]
    idx = await retrieve(rec, rec.get("display_title") or rec.get("topic") or "lecture", k=30, window=1)[cite: 3]
    context = context_from_indices(rec, idx, max_chars=40000)[cite: 3]
    system = (
        "You summarize a class recording for students. Use ONLY the transcript. "
        "Always write in English. "
        "Return STRICT JSON: {\"summary\": string (2-4 sentences), "
        "\"topics\": string[] (4-8 short topic tags, each 1-4 words)}. No markdown, no extra text."
    )[cite: 3]
    raw = await llm(
        [{"role": "system", "content": system},
         {"role": "user", "content": f"Transcript excerpts:\n{context}"}],
        max_tokens=500, temperature=0.2,
    )[cite: 3]
    import json as _json
    txt = (raw or "").strip()[cite: 3]
    if txt.startswith("```"):[cite: 3]
        txt = txt.strip("`")[cite: 3]
        txt = txt.split("\n", 1)[-1] if "\n" in txt else txt[cite: 3]
    try:
        data = _json.loads(txt[txt.find("{"): txt.rfind("}") + 1])[cite: 3]
    except Exception:
        data = {"summary": txt[:400], "topics": []}[cite: 3]
    rec["summary"] = (data.get("summary") or "").strip()[cite: 3]
    rec["topics"] = [t.strip() for t in (data.get("topics") or []) if t.strip()][:8][cite: 3]
    return rec[cite: 3]


class SummaryBody(BaseModel):
    passcode: str[cite: 3]
    id: str[cite: 3]


@app.post("/api/teacher/summary")
async def teacher_summary(body: SummaryBody):
    if not check_passcode(body.passcode):[cite: 3]
        return JSONResponse({"error": "unauthorized"}, status_code=401)[cite: 3]
    rec = REC_BY_ID.get(body.id)[cite: 3]
    if not rec:[cite: 3]
        return JSONResponse({"error": "not found"}, status_code=404)[cite: 3]
    if not rec.get("segments"):[cite: 3]
        return JSONResponse({"error": "This recording has no transcript yet."}, status_code=422)[cite: 3]
    try:
        await generate_summary_and_topics(rec)[cite: 3]
    except LLMConfigError as e:[cite: 3]
        return JSONResponse({"error": str(e)}, status_code=503)[cite: 3]
    except Exception as e:
        return JSONResponse({"error": f"Could not generate summary: {e}"}, status_code=500)[cite: 3]
    save_recordings(RECORDINGS)[cite: 3]
    return {"ok": True, "id": body.id, "summary": rec.get("summary", ""), "topics": rec.get("topics", [])}[cite: 3]


@app.post("/api/teacher/stats")
def teacher_stats(body: TeacherAuth):
    if not check_passcode(body.passcode):[cite: 3]
        return JSONResponse({"error": "unauthorized"}, status_code=401)[cite: 3]
    from datetime import datetime, timedelta
    roster = load_roster()[cite: 3]
    log = load_qlog()[cite: 3]
    total = len(RECORDINGS)[cite: 3]
    transcribed = sum(1 for r in RECORDINGS if r.get("segments"))[cite: 3]
    visible = sum(1 for r in RECORDINGS if r.get("visible", True))[cite: 3]
    unassigned = sum(1 for r in RECORDINGS if (r.get("unit") or "Unassigned") == "Unassigned")[cite: 3]
    week_ago = datetime.utcnow() - timedelta(days=7)[cite: 3]
    q_week = 0[cite: 3]
    for q in log:[cite: 3]
        try:
            if datetime.strptime((q.get("time") or "")[:10], "%Y-%m-%d") >= week_ago:[cite: 3]
                q_week += 1[cite: 3]
        except Exception:
            pass
    courses = len({(r.get("unit") or "Unassigned") for r in RECORDINGS})[cite: 3]
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
    }[cite: 3]


_STOPWORDS = set("the a an and or of to in is are was were be been what how why when where "
                 "which who whom this that these those i you he she it we they for on at by "
                 "with about from as do does did can could would should will shall may might "
                 "not no yes please tell me my your our their his her its me can't cant explain "
                 "give show list define describe difference between them then than".split())[cite: 3]


@app.post("/api/teacher/analytics")
def teacher_analytics(body: TeacherAuth):
    if not check_passcode(body.passcode):[cite: 3]
        return JSONResponse({"error": "unauthorized"}, status_code=401)[cite: 3]
    log = load_qlog()[cite: 3]
    kw = Counter()[cite: 3]
    per_student = Counter()[cite: 3]
    per_course = Counter()[cite: 3]
    per_day = Counter()[cite: 3]
    for q in log:[cite: 3]
        for w in tokenize(q.get("question", "")):[cite: 3]
            if len(w) > 2 and w not in _STOPWORDS:[cite: 3]
                kw[w] += 1[cite: 3]
        per_student[q.get("student") or "Unknown"] += 1[cite: 3]
        per_course[q.get("unit") or "Unassigned"] += 1[cite: 3]
        d = (q.get("time") or "")[:10][cite: 3]
        if d:[cite: 3]
            per_day[d] += 1[cite: 3]
    return {
        "total": len(log),
        "top_keywords": kw.most_common(15),
        "top_students": per_student.most_common(10),
        "by_course": per_course.most_common(20),
        "by_day": sorted(per_day.items()),
    }[cite: 3]


@app.get("/api/teacher/export/questions.csv")
def export_questions_csv(passcode: str = Query(...)):
    if not check_passcode(passcode):[cite: 3]
        return JSONResponse({"error": "unauthorized"}, status_code=401)[cite: 3]
    import csv
    log = load_qlog()[cite: 3]
    buf = io.StringIO()[cite: 3]
    w = csv.writer(buf)[cite: 3]
    w.writerow(["Time", "Student", "Recording", "Unit", "Question"])[cite: 3]
    for q in reversed(log):[cite: 3]
        w.writerow([q.get("time", ""), q.get("student", ""), q.get("recording_title", ""),
                    q.get("unit", ""), q.get("question", "")])[cite: 3]
    data = buf.getvalue().encode("utf-8-sig")[cite: 3]
    from fastapi.responses import Response
    return Response(content=data, media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=questions.csv"})[cite: 3]


@app.get("/api/teacher/export/roster.csv")
def export_roster_csv(passcode: str = Query(...)):
    if not check_passcode(passcode):[cite: 3]
        return JSONResponse({"error": "unauthorized"}, status_code=401)[cite: 3]
    import csv
    roster = load_roster()[cite: 3]
    buf = io.StringIO()[cite: 3]
    w = csv.writer(buf)[cite: 3]
    w.writerow(["Name", "Email", "Courses"])[cite: 3]
    for s in roster:[cite: 3]
        w.writerow([s.get("name", ""), s.get("email", ""), ", ".join(s.get("courses", []) or [])])[cite: 3]
    data = buf.getvalue().encode("utf-8-sig")[cite: 3]
    from fastapi.responses import Response
    return Response(content=data, media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=roster.csv"})[cite: 3]


@app.get("/api/teacher/export/questions.pdf")
def export_questions_pdf(passcode: str = Query(...)):
    if not check_passcode(passcode):[cite: 3]
        return JSONResponse({"error": "unauthorized"}, status_code=401)[cite: 3]
    from fastapi.responses import Response
    from datetime import datetime
    log = load_qlog()[cite: 3]
    lines = [f"NG-ClassMate — Student Questions Report",
             f"Generated {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC",
             f"Total questions: {len(log)}", ""][cite: 3]
    for q in reversed(log):[cite: 3]
        lines.append(f"{q.get('time','')}  |  {q.get('student','')}  |  {q.get('unit','')}")[cite: 3]
        lines.append(f"  Q: {q.get('question','')}")[cite: 3]
        lines.append(f"  Recording: {q.get('recording_title','')}")[cite: 3]
        lines.append("")[cite: 3]
    pdf_bytes = _simple_text_pdf(lines)[cite: 3]
    return Response(content=pdf_bytes, media_type="application/pdf",
                    headers={"Content-Disposition": "attachment; filename=questions.pdf"})[cite: 3]


def _simple_text_pdf(lines):
    def esc(s):
        return (s or "").replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")[cite: 3]
    per_page = 48[cite: 3]
    pages = [lines[i:i + per_page] for i in range(0, max(1, len(lines)), per_page)] or [[""]][cite: 3]
    objs = [][cite: 3]
    n_pages = len(pages)[cite: 3]
    font_obj = 3 + n_pages * 2[cite: 3]
    kids = [][cite: 3]
    body_objs = {}[cite: 3]
    obj_num = 3[cite: 3]
    content_nums = [][cite: 3]
    page_nums = [][cite: 3]
    for pi, pg in enumerate(pages):[cite: 3]
        page_no = obj_num; obj_num += 1[cite: 3]
        content_no = obj_num; obj_num += 1[cite: 3]
        page_nums.append(page_no); content_nums.append(content_no)[cite: 3]
    font_no = obj_num[cite: 3]
    for pi, pg in enumerate(pages):[cite: 3]
        text_cmds = ["BT", "/F1 10 Tf", "12 TL", "40 800 Td"][cite: 3]
        for ln in pg:[cite: 3]
            text_cmds.append(f"({esc(ln)[:180]}) Tj")[cite: 3]
            text_cmds.append("T*")[cite: 3]
        text_cmds.append("ET")[cite: 3]
        stream = "\n".join(text_cmds)[cite: 3]
        body_objs[content_nums[pi]] = f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"[cite: 3]
        body_objs[page_nums[pi]] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_no} 0 R >> >> /Contents {content_nums[pi]} 0 R >>"
        )[cite: 3]
    body_objs[font_no] = "<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>"[cite: 3]
    kids_str = " ".join(f"{pn} 0 R" for pn in page_nums)[cite: 3]
    body_objs[1] = "<< /Type /Catalog /Pages 2 0 R >>"[cite: 3]
    body_objs[2] = f"<< /Type /Pages /Kids [{kids_str}] /Count {n_pages} >>"[cite: 3]
    out = "%PDF-1.4\n"[cite: 3]
    offsets = {}[cite: 3]
    for num in sorted(body_objs):[cite: 3]
        offsets[num] = len(out.encode("latin-1", "replace"))[cite: 3]
        out += f"{num} 0 obj\n{body_objs[num]}\nendobj\n"[cite: 3]
    xref_pos = len(out.encode("latin-1", "replace"))[cite: 3]
    max_num = max(body_objs)[cite: 3]
    out += f"xref\n0 {max_num + 1}\n0000000000 65535 f \n"[cite: 3]
    for num in range(1, max_num + 1):[cite: 3]
        out += f"{offsets.get(num, 0):010d} 00000 n \n"[cite: 3]
    out += f"trailer\n<< /Size {max_num + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF"[cite: 3]
    return out.encode("latin-1", "replace")[cite: 3]


@app.get("/api/health")
def health():
    return {"status": "ok", "recordings": len(RECORDINGS)}[cite: 3]


if os.path.isdir(FRONTEND_DIR):[cite: 3]
    @app.get("/")
    def index():
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))[cite: 3]
    app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="static")[cite: 3]
