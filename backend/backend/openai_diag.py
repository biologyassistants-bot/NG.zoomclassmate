"""
==============================================================================
 NG-ClassMate — OpenAI key diagnostic endpoint  (drop-in for YOUR main.py)
==============================================================================
 Tailored to your actual code: it checks the SAME env vars your app uses
 (OPENAI_API_KEY, OPENAI_MODEL, OPENAI_BASE_URL) and makes the test call the
 SAME way your llm() does — via httpx to {OPENAI_BASE_URL}/chat/completions.

 It NEVER returns the key itself (only length + masked preview).

------------------------------------------------------------------------------
 INSTALL (matches your project layout: backend/main.py + frontend/)
------------------------------------------------------------------------------
 1. Put this file next to main.py  (e.g. backend/diag_openai.py).
 2. In main.py, AFTER these lines already in your file:
        app = FastAPI(title="ClassMate API")
        app.add_middleware(CORSMiddleware, ...)
    add:
        from diag_openai import register_openai_diag
        register_openai_diag(app)                      # public
        # register_openai_diag(app, passcode="teach123")  # or guard with ?pass=
    IMPORTANT: add it BEFORE the `app.mount("/", StaticFiles(...))` line near the
    bottom — routes mounted under "/" can otherwise shadow new routes.
 3. Commit & push -> Render redeploys.
 4. Open:  https://ng-zoomclassmate.onrender.com/api/diag/openai
 5. Read the JSON (see DIAGNOSTIC_INSTRUCTIONS.md). Remove when done.
------------------------------------------------------------------------------
"""

import os
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse


def _mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 10:
        return key[:2] + "*" * (len(key) - 2)
    return key[:5] + "..." + key[-4:]


def _interpret(status: int, detail: str) -> str:
    low = (detail or "").lower()
    if status == 401 or "incorrect api key" in low or "invalid" in low:
        return ("Key REJECTED (invalid/incorrect). Re-copy from "
                "platform.openai.com and update OPENAI_API_KEY, then redeploy.")
    if status == 429 or "quota" in low or "billing" in low:
        return ("Key valid but NO CREDIT / rate-limited. Add billing/credits "
                "to the OpenAI account.")
    if status == 404 or ("model" in low and "not" in low):
        return ("Key works but the model isn't available to this account. "
                "Set OPENAI_MODEL to one you can use (e.g. gpt-4o-mini).")
    return f"Provider returned {status}: {detail[:200]}"


def register_openai_diag(app: FastAPI, passcode: str | None = None) -> None:

    @app.get("/api/diag/openai")
    async def diag_openai(pass_: str | None = Query(default=None, alias="pass")):
        if passcode is not None and pass_ != passcode:
            return JSONResponse(status_code=403,
                                content={"ok": False, "message": "Forbidden: wrong ?pass="})

        key = os.environ.get("OPENAI_API_KEY", "").strip()
        model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()
        base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").strip()

        report = {
            "env_var_present": bool(key),
            "key_length": len(key),
            "key_masked_preview": _mask(key),
            "model": model,
            "base_url": base,
            "sandbox_call_llm_present": "call_llm" in dir(__builtins__),
        }

        if not key:
            report["ok"] = False
            report["message"] = (
                "OPENAI_API_KEY is NOT set (or empty) on this server. This is "
                "almost certainly why Ask/Quiz fail: your llm() then falls back "
                "to the sandbox-only call_llm, which doesn't exist here and "
                "crashes with a 500 -> the frontend shows 'couldn't reach the "
                "server.' Fix: add OPENAI_API_KEY under Render -> Environment "
                "and redeploy."
            )
            return JSONResponse(status_code=200, content=report)

        # Make the SAME kind of call your app makes.
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
            report["message"] = (f"Network error reaching {base}: {e}. On Render "
                                  "free tier this can be a cold-start timeout; retry once warm.")
            return JSONResponse(status_code=200, content=report)

        if resp.status_code >= 400:
            detail = ""
            try:
                detail = resp.json().get("error", {}).get("message", "")
            except Exception:
                detail = resp.text[:200]
            report["ok"] = False
            report["provider_status"] = resp.status_code
            report["message"] = _interpret(resp.status_code, detail)
            return JSONResponse(status_code=200, content=report)

        report["ok"] = True
        report["provider_status"] = resp.status_code
        report["message"] = "OPENAI_API_KEY is set AND the API call succeeded. The key is working."
        return JSONResponse(status_code=200, content=report)
