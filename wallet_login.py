#!/usr/bin/env python3
"""
getunikey.ai — Wallet login v5
Direct API approach: bypass Reown AppKit entirely.

Flow:
1. Solve Turnstile (via Camoufox)
2. POST /api/oauth/web3/challenge {wallet_address}
3. Sign challenge with eth-account (personal_sign)
4. POST /api/oauth/web3/verify {wallet_address, message, signature, turnstile}
5. Collect JWT + API key
"""
import json, time, sys, os, re
from pathlib import Path
from eth_account import Account, messages
Account.enable_unaudited_hdwallet_features()
import requests

SIGN_IN_URL = "https://www.getunikey.ai/sign-in"
API_BASE = "https://www.getunikey.ai"
INFO = Path(__file__).parent / "wallet_info.json"

# ---------------------------------------------------------------------------
# Load wallet
# ---------------------------------------------------------------------------
def load_wallet():
    with open(INFO) as f:
        data = json.load(f)
    mn = data.get("mnemonic", "")
    if mn:
        account = Account.from_mnemonic(mn)
    else:
        account = Account.create()
    return account

# ---------------------------------------------------------------------------
# Step 1: Solve Turnstile via Camoufox
# ---------------------------------------------------------------------------
def solve_turnstile():
    from camoufox.sync_api import Camoufox

    print("[1/5] Solving Turnstile via Camoufox...", flush=True)

    with Camoufox(headless=False) as browser:
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(SIGN_IN_URL, wait_until="domcontentloaded", timeout=60000)

        JS_LEN = (
            '(function(){ var el = document.querySelector("input[name=\\"cf-turnstile-response\\"]");'
            ' return el ? (el.value || "").length : 0; })()'
        )

        turnstile_token = None
        for i in range(60):
            time.sleep(1)
            try:
                ln = page.evaluate(JS_LEN)
                if ln > 10:
                    turnstile_token = page.evaluate(
                        '(function(){ var el = document.querySelector("input[name=\\"cf-turnstile-response\\"]");'
                        ' return el ? el.value : ""; })()'
                    )
                    print("  Turnstile solved at %ds (token len=%d)" % (i+1, len(turnstile_token)), flush=True)
                    break
            except:
                pass

        if not turnstile_token:
            print("  [!] Turnstile not solved, trying reload...", flush=True)
            page.reload(wait_until="domcontentloaded", timeout=30000)
            for i in range(60):
                time.sleep(1)
                try:
                    ln = page.evaluate(JS_LEN)
                    if ln > 10:
                        turnstile_token = page.evaluate(
                            '(function(){ var el = document.querySelector("input[name=\\"cf-turnstile-response\\"]");'
                            ' return el ? el.value : ""; })()'
                        )
                        print("  Turnstile solved at %ds (token len=%d)" % (i+1, len(turnstile_token)), flush=True)
                        break
                except:
                    pass

        # Also grab cookies
        cookies = ctx.cookies()
        cookie_dict = {c["name"]: c["value"] for c in cookies}

        ctx.close()

    if not turnstile_token:
        print("  [!] FAILED to solve Turnstile", flush=True)
        return None, {}

    return turnstile_token, cookie_dict


# ---------------------------------------------------------------------------
# Step 2-4: API flow
# ---------------------------------------------------------------------------
def wallet_login(account, turnstile_token, cookies):
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": API_BASE,
        "Referer": SIGN_IN_URL,
    })
    # Set cookies
    for name, value in cookies.items():
        session.cookies.set(name, value)

    wallet_address = account.address
    print("\n[2/5] Requesting wallet challenge for %s..." % wallet_address, flush=True)

    # POST /api/oauth/web3/challenge
    resp = session.post(
        "%s/api/oauth/web3/challenge" % API_BASE,
        json={"wallet_address": wallet_address},
        timeout=30,
    )
    print("  Status: %d" % resp.status_code, flush=True)

    try:
        challenge_data = resp.json()
        print("  Response: %s" % json.dumps(challenge_data, indent=2)[:500], flush=True)
    except:
        print("  Response text: %s" % resp.text[:500], flush=True)
        return None

    if not challenge_data.get("success"):
        print("  [!] Challenge failed: %s" % challenge_data.get("message", "unknown"), flush=True)
        return None

    # Extract message + nonce from challenge
    data = challenge_data.get("data", {})
    sign_message = data.get("message", "")
    nonce = data.get("nonce", "")

    print("\n[3/5] Signing challenge message...", flush=True)
    print("  Message: %s" % sign_message[:200], flush=True)
    print("  Nonce: %s" % nonce, flush=True)

    # Sign with personal_sign (eth_sign if hex, personal_sign if text)
    try:
        if sign_message.startswith("0x"):
            signed = account.sign_message(messages.encode_defunct(hexstr=sign_message))
        else:
            signed = account.sign_message(messages.encode_defunct(text=sign_message))
        signature = "0x" + signed.signature.hex()
        print("  Signature: %s..." % signature[:66], flush=True)
    except Exception as e:
        print("  [!] Signing failed: %s" % e, flush=True)
        return None

    print("\n[4/5] Verifying signature...", flush=True)

    # POST /api/oauth/web3/verify
    # Exact payload from JS source:
    #   action: "login", wallet_address, nonce, signature, chain_id: 56, turnstile, hcaptcha
    verify_payload = {
        "action": "login",
        "wallet_address": wallet_address,
        "nonce": nonce,
        "signature": signature,
        "chain_id": 56,
        "turnstile": turnstile_token,
        "hcaptcha": "",
    }

    resp = session.post(
        "%s/api/oauth/web3/verify" % API_BASE,
        json=verify_payload,
        params={"turnstile": turnstile_token, "hcaptcha": ""},
        timeout=30,
    )
    print("  Status: %d" % resp.status_code, flush=True)

    print("  Headers:", dict(resp.headers), flush=True)
    try:
        verify_data = resp.json()
        print("  Response: %s" % json.dumps(verify_data, indent=2)[:500], flush=True)
    except:
        print("  Response text: %s" % resp.text[:500], flush=True)
        return None

    if not verify_data.get("success"):
        print("  [!] Verify failed: %s" % verify_data.get("message", "unknown"), flush=True)
        return None

    # Extract JWT from response - the response sets session cookies (session=...)
    # Token is typically in Set-Cookie header or response body
    token = verify_data.get("data", {}).get("token") or verify_data.get("token")
    if isinstance(token, dict):
        token = token.get("session", "") or json.dumps(token)

    # Check all Set-Cookie headers
    session_cookies = session.cookies.get_dict()
    all_cookies = {}
    for cookie in resp.cookies:
        all_cookies[cookie.name] = cookie.value
    print("  Response cookies: %s" % list(all_cookies.keys()), flush=True)
    if "session" in all_cookies:
        token = all_cookies["session"]
        print("  Session token: %s..." % token[:60], flush=True)
    elif "session" in session_cookies:
        token = session_cookies["session"]
        print("  Session token: %s..." % token[:60], flush=True)

    print("\n  Final token: %s" % (str(token)[:60] if token else "None"), flush=True)

    return {"session": session, "token": token, "verify_data": verify_data, "cookies": session_cookies}


# ---------------------------------------------------------------------------
# Step 5: Collect API credentials
# ---------------------------------------------------------------------------
def collect_api(result):
    if not result:
        return {}

    session = result["session"]
    token = result.get("token")
    cookies = result.get("cookies", {})

    print("\n[5/5] Collecting API credentials...", flush=True)

    results = {"wallet": None, "username": None, "api_key": None, "token": token}

    # Build auth headers - New-Api-User header is required
    user_id = result["verify_data"]["data"]["id"]
    auth_headers = {"New-Api-User": str(user_id)}

    # Ensure session cookie is set
    if "session" in cookies:
        session.cookies.set("session", cookies["session"], domain=".getunikey.ai")
        session.cookies.set("session", cookies["session"], domain="getunikey.ai")

    print("  User ID: %d" % user_id, flush=True)
    print("  All cookies: %s" % list(session.cookies.get_dict().keys()), flush=True)

    # Get user info - try /api/user/self with cookie auth
    try:
        resp = session.get(
            "%s/api/user/self" % API_BASE,
            headers=auth_headers,
            timeout=30,
        )
        print("  /api/user/self status: %d" % resp.status_code, flush=True)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("success"):
                user = data.get("data", {})
                results["username"] = user.get("username", "?")
                results["wallet"] = user.get("wallet_address") or user.get("address", "?")
                print("  User: %s (id=%s)" % (results["username"], user.get("id")), flush=True)
            else:
                print("  /api/user/self failed: %s" % data.get("message"), flush=True)
        elif resp.status_code == 401:
            # Try without auth header, just cookies
            resp2 = session.get("%s/api/user/self" % API_BASE, timeout=30)
            print("  /api/user/self (cookie-only) status: %d" % resp2.status_code, flush=True)
            if resp2.status_code == 200:
                data = resp2.json()
                if data.get("success"):
                    user = data.get("data", {})
                    results["username"] = user.get("username", "?")
                    print("  User: %s (cookie auth works)" % results["username"], flush=True)
    except Exception as e:
        print("  /api/user/self error: %s" % e, flush=True)

    # Get API keys
    try:
        resp = session.get(
            "%s/api/token/" % API_BASE,
            headers=auth_headers,
            timeout=30,
        )
        print("  /api/token/ status: %d" % resp.status_code, flush=True)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("success"):
                items = data.get("data", {})
                if isinstance(items, dict):
                    items = items.get("items", [])
                existing_keys = []
                for t in items:
                    if isinstance(t, dict) and t.get("key"):
                        existing_keys.append(t["key"])
                        results["api_key"] = t["key"]
                        print("  Existing key: %s" % t["key"], flush=True)
                        break
                if not existing_keys:
                    print("  No API keys found, creating one...", flush=True)
                    resp2 = session.post(
                        "%s/api/token/" % API_BASE,
                        headers=auth_headers,
                        json={"name": "bot-key", "expired_time": 0},
                        timeout=30,
                    )
                    data2 = resp2.json()
                    if data2.get("success"):
                        print("  API key created (masked for security)", flush=True)
                        results["api_key_created"] = True
                    else:
                        print("  Failed to create API key: %s" % data2.get("message"), flush=True)
        elif resp.status_code == 401:
            print("  /api/token/ also 401 - auth not working", flush=True)
    except Exception as e:
        print("  /api/token/ error: %s" % e, flush=True)

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    account = load_wallet()
    print("[*] Wallet: %s" % account.address, flush=True)

    # Step 1: Solve Turnstile
    turnstile_token, cookies = solve_turnstile()
    if not turnstile_token:
        print("\n[!] Cannot proceed without Turnstile token", flush=True)
        sys.exit(1)

    # Steps 2-4: API flow
    result = wallet_login(account, turnstile_token, cookies)

    # Step 5: Collect API credentials
    results = collect_api(result)
    results["wallet"] = account.address

    # Save results (APPEND — never wipe history, one JSON line per run)
    results_file = Path(__file__).parent / "results.json"
    results["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(results_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(results) + "\n")
            f.flush()
    except OSError as e:
        print("[!] cannot append results: %s" % e, flush=True)
        results_file = None

    print("\n" + "="*60, flush=True)
    print("RESULTS:", flush=True)
    print("  wallet:    %s" % results.get("wallet"), flush=True)
    print("  username:  %s" % results.get("username"), flush=True)
    print("  api_key:   %s" % (results.get("api_key") or "not found"), flush=True)
    print("  token:     %s" % (str(results.get("token"))[:50] if results.get("token") else "not found"), flush=True)
    print("  saved to:  %s" % results_file, flush=True)
    print("="*60, flush=True)

    print("\n[PRIVATE KEY]: %s" % account.key.hex(), flush=True)


if __name__ == "__main__":
    main()