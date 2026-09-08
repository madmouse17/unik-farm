#!/usr/bin/env python3
"""
captcha_solvers.py — Fallback Turnstile solvers via external services.

Used when the in-browser Camoufox auto-solve fails/never populates.
Chain: CapSolver -> 2Captcha. Returns the turnstile response token
(the same value the page would put into input[name='cf-turnstile-response']).

API keys read from .env (next to this file):
    API_CAPSOLVER = CAP-xxxx...
    API_2CAPTCHA = xxxx...
"""
import os
import time
import json

import requests

from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except Exception:
    pass

CAPSOLVER_ENDPOINT = "https://api.capsolver.com"
TWOCAPTCHA_ENDPOINT = "https://2captcha.com"

DEFAULT_SITEKEY = "0x4AAAAAAD83S5lYamgIOFL4"  # getunikey.ai turnstile
DEFAULT_WEBSITE = "https://www.getunikey.ai/sign-in"


def env():
    return {
        "capsolver": os.getenv("API_CAPSOLVER", "").strip(),
        "twocaptcha": os.getenv("API_2CAPTCHA", "").strip(),
    }


# ---------------------------------------------------------------------------
# CapSolver — AntiTurnstileTaskProxyLess
# ---------------------------------------------------------------------------
def _capsolver_poll(client_key, task_id, timeout=90):
    url = CAPSOLVER_ENDPOINT + "/getTaskResult"
    payload = {"clientKey": client_key, "taskId": task_id}
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            r = requests.post(url, json=payload, timeout=30)
            data = r.json()
        except Exception as e:
            last = str(e)
            time.sleep(2)
            continue

        status = data.get("status")
        if status == "ready":
            solution = data.get("solution", {})
            token = solution.get("token") or solution.get("turnstile")
            return (token, None) if token else (None, "ready but no token")
        if status == "failed":
            return None, "task failed: %s" % data.get("errorDescription", "unknown")
        last = "status=%s" % status
        time.sleep(2)
    return None, "poll timeout: %s" % (last or "no response")


def solve_turnstile_capsolver(sitekey=DEFAULT_SITEKEY, website=DEFAULT_WEBSITE, timeout=90):
    client_key = env()["capsolver"]
    if not client_key:
        return None, "no API_CAPSOLVER key in .env"

    url = CAPSOLVER_ENDPOINT + "/createTask"
    payload = {
        "clientKey": client_key,
        "task": {
            "type": "AntiTurnstileTaskProxyLess",
            "websiteURL": website,
            "websiteKey": sitekey,
            "metadata": {"type": "turnstile", "action": "login"},
        },
    }
    try:
        r = requests.post(url, json=payload, timeout=30)
        data = r.json()
    except Exception as e:
        return None, "createTask err: %s" % str(e)[:80]

    if data.get("status") != "ready":
        task_id = data.get("taskId")
        if not task_id:
            return None, "createTask fail: %s" % data.get("errorDescription", data)
        token, err = _capsolver_poll(client_key, task_id, timeout)
        return (token, None) if token else (None, err or "capsolver fail")

    solution = data.get("solution", {})
    token = solution.get("token") or solution.get("turnstile")
    if token:
        return token, None
    return None, "capsolver ready but no token: %s" % json.dumps(data)[:120]


# ---------------------------------------------------------------------------
# 2Captcha — method=turnstile
# ---------------------------------------------------------------------------
def _twocaptcha_poll(api_key, captcha_id, timeout=180):
    url = TWOCAPTCHA_ENDPOINT + "/res.php"
    params = {"key": api_key, "action": "get", "id": captcha_id, "json": 1}
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            r = requests.get(url, params=params, timeout=30)
            data = r.json()
        except Exception as e:
            last = str(e)
            time.sleep(3)
            continue

        if data.get("status") == 1:
            return data.get("request"), None
        if data.get("request") == "CAPCHA_NOT_READY":
            last = "not ready"
        else:
            return None, "2captcha error: %s" % data.get("request")
        time.sleep(3)
    return None, "2captcha poll timeout (%s)" % (last or "no response")


def solve_turnstile_2captcha(sitekey=DEFAULT_SITEKEY, website=DEFAULT_WEBSITE, timeout=180):
    api_key = env()["twocaptcha"]
    if not api_key:
        return None, "no API_2CAPTCHA key in .env"

    url = TWOCAPTCHA_ENDPOINT + "/in.php"
    params = {
        "key": api_key,
        "method": "turnstile",
        "sitekey": sitekey,
        "pageurl": website,
        "action": "login",
        "json": 1,
    }
    try:
        r = requests.post(url, data=params, timeout=30)
        data = r.json()
    except Exception as e:
        return None, "in.php err: %s" % str(e)[:80]

    if data.get("status") != 1:
        return None, "in.php fail: %s" % data.get("request")
    return _twocaptcha_poll(api_key, data["request"], timeout)


# ---------------------------------------------------------------------------
# Chain
# ---------------------------------------------------------------------------
def solve_turnstile_external(sitekey=DEFAULT_SITEKEY, website=DEFAULT_WEBSITE,
                             use_capsolver=True, use_2captcha=True, log=None):
    """Try CapSolver then 2Captcha. Returns (token, source_or_err, error)."""
    if use_capsolver:
        if log:
            log("CapSolver solving...")
        token, err = solve_turnstile_capsolver(sitekey, website)
        if token:
            return token, "capsolver", None
        if log:
            log("  capsolver fail: %s" % err)

    if use_2captcha:
        if log:
            log("2Captcha solving...")
        token, err = solve_turnstile_2captcha(sitekey, website)
        if token:
            return token, "2captcha", None
        if log:
            log("  2captcha fail: %s" % err)
        return None, "", err

    return None, "", "both solvers skipped"


if __name__ == "__main__":
    import sys
    print("Config: capsolver=%s twocaptcha=%s" % (
        "set" if env()["capsolver"] else "MISSING",
        "set" if env()["twocaptcha"] else "MISSING",
    ))
    t, src, err = solve_turnstile_external(log=print)
    if t:
        print("TOKEN (%s): %s..." % (src, t[:40]))
    else:
        print("FAILED: %s" % err)
        sys.exit(1)