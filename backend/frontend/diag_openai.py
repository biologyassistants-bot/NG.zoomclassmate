"""
==============================================================================
 NG-ClassMate — OpenAI API key diagnostic endpoint  (drop-in for FastAPI)
==============================================================================

WHAT IT DOES
  Adds a single safe endpoint:  GET /api/diag/openai
  It reports, WITHOUT ever exposing the actual key:
    - whether the OPENAI_API_KEY env var is present
    - the key's length + a masked preview (e.g. "sk-...Ab3d") so you can tell
      you pasted the RIGHT key, not a truncated / wrong one
    - which openai library version is installed
    - the result of a REAL tiny test call to OpenAI (1-token completion),
      so you know the key actually authenticates and the network path works
    - clear, human-readable error text if the call fails (auth? quota? timeout?)

WHY IT'S SAFE
    - It never returns the key itself, only length + masked preview.
    - The test call costs a fraction of a cent (max_tokens=1).
    - You can DELETE this file / route after you've confirmed things work.
    - Optional: protect it with your teacher passcode via ?pass=... (see below).

------------------------------------------------------------------------------
 HOW TO INSTALL  (2 minutes)
------------------------------------------------------------------------------
 1. Copy this whole file into your backend project, e.g. as  diag_openai.py
    (next to your main.py / app.py).

 2. In your main app file where you create the FastAPI app, add TWO lines.
    Suppose your file has:

        from fastapi import FastAPI
        app = FastAPI()          # <-- your existing app

    Add right after the app is created:

        from diag_openai import register_openai_diag
        register_openai_diag(app)          # optional: register_openai_diag(app, passcode="teach123")

 3. Commit & push. Render will redeploy.

 4. Visit:   https://ng-zoomclassmate.onrender.com/api/diag/openai
    (or add  ?pass=YOUR_PASSCODE  if you enabled the passcode guard)

 5. Read the JSON. It tells you exactly what's wrong. When done, remove the
    two lines (and this file) and redeploy to take it back down.
------------------------------------------------------------------------------
"""

import os
import time

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse


def _mask_key(key: str) -> str:
    """Return a safe masked preview of the key — never the full value."""
    if not key:
        return ""
    if len(key) <= 10:
        return key[:2] + "..." + "*" * (len(key) - 2)
    return key[:5] + "..." + key[-4:]


def _openai_version() -> str:
    try:
        import openai
        return getattr(openai, "__version__", "unknown")
    except Exception as e:  # pragma: no cover
        return f"NOT INSTALLED ({e})"


def _run_test_call(api_key: str, timeout: float = 20.0) -> dict:
    """
    Make a minimal real call to OpenAI to prove the key authenticates.
    Works with BOTH the new (>=1.0) and old (<1.0) openai python SDKs.
    Returns a dict describing what happened.
    """
    # ---- Try the NEW SDK first (openai >= 1.0) --------------------------
    try:
        from openai import OpenAI  # new-style import; only exists on >=1.0
        try:
            client = OpenAI(api_key=api_key, timeout=timeout)
            t0 = time.time()
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
            )
            dt = round(time.time() - t0, 2)
            model = getattr(resp, "model", "gpt-4o-mini")
            return {
                "ok": True,
                "sdk_style": "new (openai>=1.0)",
                "model_used": model,
                "round_trip_seconds": dt,
                "message": "OpenAI key is VALID and the API call succeeded.",
            }
        except Exception as e:
            return {
                "ok": False,
                "sdk_style": "new (openai>=1.0)",
                "error_type": type(e).__name__,
                "error": str(e),
                "message": _interpret_error(str(e)),
            }
    except ImportError:
        pass  # fall through to old SDK

    # ---- Fall back to the OLD SDK (openai < 1.0) ------------------------
    try:
        import openai
        openai.api_key = api_key
        t0 = time.time()
        resp = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
            request_timeout=timeout,
        )
        dt = round(time.time() - t0, 2)
        return {
            "ok": True,
            "sdk_style": "old (openai<1.0)",
            "model_used": resp.get("model", "gpt-3.5-turbo"),
            "round_trip_seconds": dt,
            "message": "OpenAI key is VALID and the API call succeeded.",
        }
    except Exception as e:
        return {
            "ok": False,
            "sdk_style": "old (openai<1.0)",
            "error_type": type(e).__name__,
            "error": str(e),
            "message": _interpret_error(str(e)),
        }


def _interpret_error(err: str) -> str:
    """Turn a raw OpenAI error string into plain-English guidance."""
    low = err.lower()
    if "incorrect api key" in low or "invalid api key" in low or "401" in low:
        return ("The key was REJECTED (invalid/incorrect). Re-copy the key from "
                "platform.openai.com and update OPENAI_API_KEY on Render.")
    if "quota" in low or "insufficient_quota" in low or "429" in low or "billing" in low:
        return ("The key is valid but has NO CREDIT / hit a rate or quota limit. "
                "Add billing/credits to the OpenAI account.")
    if "model" in low and ("does not exist" in low or "not found" in low or "access" in low):
        return ("The key works but the account can't access this model. "
                "Switch to a model your account has (e.g. gpt-4o-mini or gpt-3.5-turbo).")
    if "timeout" in low or "timed out" in low:
        return ("The request TIMED OUT reaching OpenAI. On Render free tier this "
                "often coincides with cold starts. Try again once the server is warm.")
    if "connection" in low or "network" in low or "getaddrinfo" in low:
        return ("Network/DNS problem reaching OpenAI from the server. Check outbound "
                "connectivity / any proxy settings on the host.")
    return "Unexpected error — see the raw 'error' field above for details."


def register_openai_diag(app: FastAPI, passcode: str | None = None) -> None:
    """
    Register  GET /api/diag/openai  on your FastAPI app.

    If `passcode` is provided, the caller must pass  ?pass=<passcode>
    or the endpoint returns 403. Recommended so it's not fully public.
    """

    @app.get("/api/diag/openai")
    def diag_openai(pass_: str | None = Query(default=None, alias="pass")):
        # optional guard
        if passcode is not None and pass_ != passcode:
            return JSONResponse(
                status_code=403,
                content={"ok": False, "message": "Forbidden: missing or wrong ?pass= value."},
            )

        key = os.environ.get("OPENAI_API_KEY", "")
        report = {
            "env_var_present": bool(key),
            "env_var_name_checked": "OPENAI_API_KEY",
            "key_length": len(key) if key else 0,
            "key_masked_preview": _mask_key(key),
            "openai_library_version": _openai_version(),
        }

        if not key:
            report["ok"] = False
            report["message"] = (
                "OPENAI_API_KEY is NOT set in the environment. Add it under "
                "Render → your service → Environment, then redeploy. "
                "(If your code reads a DIFFERENT variable name, tell me which.)"
            )
            return JSONResponse(status_code=200, content=report)

        # Key exists — now prove it actually works with a tiny real call.
        report["test_call"] = _run_test_call(key)
        report["ok"] = report["test_call"].get("ok", False)
        return JSONResponse(status_code=200, content=report)
