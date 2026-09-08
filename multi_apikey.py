#!/usr/bin/env python3
"""
multi_apikey.py — Pure API approach: create accounts + extract FULL unmasked API keys.
Rich TUI dashboard (ref: unifarm register_camoufox.py).

Flow per account:
1. Solve Turnstile (Camoufox) — one-time, reused for all accounts
2. API login: challenge -> sign -> verify -> session cookie
3. Create API key (unlimited_quota) via POST /api/token/
4. Get FULL unmasked key via POST /api/token/{id}/key
5. Save: wallet|sk-xxxxx
"""
import time, json, sys, threading
from pathlib import Path
from eth_account import Account, messages
Account.enable_unaudited_hdwallet_features()
import requests

import captcha_solvers as cs
import proxy_manager as pm

from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.layout import Layout
from rich.text import Text
from rich.align import Align
from rich import box

# ===================== CONFIG =====================
MAX_RETRIES = 3
RETRY_DELAY = 5
RATE_LIMIT_DELAY = 45          # pause when server 429 / "too frequent"
RATE_LIMIT_KEYWORDS = ("429", "频繁", "too frequent", "rate limit", "rate-limit", "retry later")

CHAIN_ID = 56
KEY_NAME_PREFIX = "9router"
API_BASE = "https://www.getunikey.ai"
SIGN_IN_URL = "https://www.getunikey.ai/sign-in"
OUTPUT_FILE = Path(__file__).parent / "wallet_apikey.txt"
FAILED_FILE = Path(__file__).parent / "multi_failed.txt"
DETAILS_FILE = Path(__file__).parent / "multi_accounts.json"
TURNSTILE_JS = """(function(){
    var e = document.querySelector('input[name="cf-turnstile-response"]');
    return e ? e.value : '';
})()"""

# ===================== STATE =====================
class State:
    def __init__(self, total, headless):
        self.total = total
        self.headless = headless
        self.success = 0
        self.failed = 0
        self.current = 0
        self.status = "Initializing..."
        self.current_wallet = ""
        self.results = []  # (short_addr, full_key_or_err, status)
        self.lock = threading.RLock()
        self.start_time = time.time()
        self.avg_time = 0
        self.ratelimit_streak = 0
        self.rate_paused = False
        self.proxy_desc = "local"

state = None
proxy_mgr = None

def update_status(msg):
    with state.lock:
        state.status = msg


def is_rate_limited(err):
    """True if error string smells like server 429 / too-many-requests."""
    if not err:
        return False
    el = err.lower()
    return any(k in err or k in el for k in RATE_LIMIT_KEYWORDS)


def sleep_backoff(err, attempt=0):
    """Sleep short (normal retry) or long (rate limit detected)."""
    if is_rate_limited(err):
        wait = RATE_LIMIT_DELAY * (attempt + 1)
        update_status("Rate limited! Pausing %ds..." % wait)
        time.sleep(wait)
        return wait
    time.sleep(RETRY_DELAY)
    return RETRY_DELAY


def mark_ratelimit():
    """Track consecutive rate-limit hits. First pair triggers a longer global pause."""
    with state.lock:
        state.ratelimit_streak += 1
        streak = state.ratelimit_streak
        paused = state.rate_paused
        if streak >= 2 and not paused:
            state.rate_paused = True
    if paused is False and streak >= 2:
        update_status("Persistent rate limit! Pausing 120s...")
        time.sleep(120)


def mark_success():
    """Reset rate-limit streak counters on any success."""
    with state.lock:
        state.ratelimit_streak = 0
        state.rate_paused = False

# ===================== HELPERS =====================
BASE = Path(__file__).parent
WALLET_INFO = BASE / "wallet_info.json"


def safe_json(resp):
    """Parse resp.json() without crashing on non-JSON bodies."""
    try:
        return resp.json()
    except Exception:
        return {"_raw": resp.text[:300]}


def load_wallet_info():
    """Load master mnemonic, auto-create + persist a new one if missing/corrupt."""
    if WALLET_INFO.exists():
        try:
            data = json.loads(WALLET_INFO.read_text(encoding="utf-8"))
            m = (data.get("mnemonic") or "").strip()
            acct = Account.from_mnemonic(m)
            if m and acct.address:
                return m, acct
            raise ValueError("empty mnemonic in file")
        except Exception as e:
            print("[!] wallet_info.json corrupt (%s) - backing up and generating new..." % e)
            try:
                WALLET_INFO.rename(WALLET_INFO.with_suffix(".json.bak"))
            except Exception:
                pass

    print("[!] No wallet_info.json found. Generating a NEW master wallet...")
    res = Account.create_with_mnemonic()
    if isinstance(res, tuple):
        acct, mnemonic = res
    else:
        acct, mnemonic = res, res.mnemonic
    payload = {
        "mnemonic": mnemonic,
        "address": acct.address,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    WALLET_INFO.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print("[+] Saved new master wallet -> %s" % WALLET_INFO)
    return mnemonic, acct


def derive_wallets(mnemonic, count):
    wallets = []
    for i in range(count):
        acct = Account.from_mnemonic(mnemonic, account_path=f"m/44'/60'/0'/0/{i}")
        wallets.append({
            "address": acct.address,
            "private_key": "0x" + acct.key.hex(),
            "index": i,
        })
    return wallets

def wait_turnstile(page, timeout=60):
    for i in range(timeout):
        time.sleep(1)
        try:
            val = page.evaluate(TURNSTILE_JS)
            if val and len(val) > 10:
                return val
        except:
            pass
    return None

def solve_turnstile(page, timeout=60):
    update_status("Loading sign-in page...")
    try:
        page.goto(SIGN_IN_URL, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        update_status("goto err: %s" % str(e)[:30])
        return None
    time.sleep(3)

    if not page.evaluate('document.querySelector(\'input[name="cf-turnstile-response"]\')'):
        update_status("Reloading page...")
        try:
            page.reload(wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass
        time.sleep(5)

    update_status("Solving Turnstile...")
    turnstile = wait_turnstile(page, timeout)
    if turnstile:
        update_status("Turnstile solved! (%d chars)" % len(turnstile))
    else:
        update_status("FAIL: Turnstile not solved")
    return turnstile

def _decode_json(text):
    """Parse server JSON safely. Returns (dict, err)."""
    if not text or not text.strip():
        return {}, "empty response body"
    try:
        return json.loads(text), None
    except Exception as e:
        return {"_raw": text[:200]}, "non-JSON: %s" % str(e)[:30]


def api_login(page, addr, privkey, turnstile):
    try:
        challenge = page.evaluate("""(async function(){
            var r = await fetch('/api/oauth/web3/challenge',{
                method:'POST',
                headers:{'Content-Type':'application/json'},
                body:JSON.stringify({wallet_address:'""" + addr + """'})
            });
            var text = await r.text();
            return {status: r.status, text: text};
        })()""")
    except Exception as e:
        return None, "challenge eval err: %s" % str(e)[:40]

    if not isinstance(challenge, dict):
        return None, "challenge bad result: %s" % str(challenge)[:40]

    cstatus = challenge.get("status", 0)
    challenge, jerr = _decode_json(challenge.get("text", ""))
    if jerr and not isinstance(challenge, dict):
        return None, "challenge non-JSON (status %s): %s" % (cstatus, jerr)

    if cstatus != 200 or not challenge.get("success"):
        msg = challenge.get("message", "unknown")
        return None, "challenge failed (status %s): %s" % (cstatus, msg)

    data = challenge.get("data") or {}
    msg = data.get("message")
    nonce = data.get("nonce")
    if not msg or not nonce:
        return None, "challenge data incomplete"

    acct = Account.from_key(privkey)
    if msg.startswith("0x"):
        signed = acct.sign_message(messages.encode_defunct(hexstr=msg))
    else:
        signed = acct.sign_message(messages.encode_defunct(text=msg))
    sig = "0x" + signed.signature.hex()

    try:
        verify = page.evaluate("""(async function(){
            var r = await fetch('/api/oauth/web3/verify?turnstile=""" + turnstile + """&hcaptcha=',{
                method:'POST',
                headers:{'Content-Type':'application/json'},
                body:JSON.stringify({
                    action:'login',
                    wallet_address:'""" + addr + """',
                    nonce:'""" + nonce + """',
                    signature:'""" + sig + """',
                    chain_id:56,
                    turnstile:'""" + turnstile + """',
                    hcaptcha:''
                })
            });
            var text = await r.text();
            return {status: r.status, text: text};
        })()""")
    except Exception as e:
        return None, "verify eval err: %s" % str(e)[:40]

    if not isinstance(verify, dict):
        return None, "verify bad result: %s" % str(verify)[:40]

    vstatus = verify.get("status", 0)
    verify, verr = _decode_json(verify.get("text", ""))
    if verr and not isinstance(verify, dict):
        return None, "verify non-JSON (status %s): %s" % (vstatus, verr)

    if vstatus != 200 or not verify.get("success"):
        vmsg = verify.get("message", "unknown")
        return None, "verify failed (status %s): %s" % (vstatus, vmsg)

    vdata = verify.get("data") or {}
    if not vdata.get("id"):
        return None, "verify OK but no user id in data"
    return vdata, None

def get_session_cookie(ctx):
    for c in ctx.cookies():
        if c["name"] == "session":
            return c["value"]
    return None

def create_api_key(session_cookie, uid, key_name, proxy=None):
    s = requests.Session()
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    s.cookies.set("session", session_cookie, domain="getunikey.ai")
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0",
        "New-Api-User": str(uid),
        "Accept": "application/json",
        "Content-Type": "application/json",
    })

    try:
        resp = s.post(
            "%s/api/token/" % API_BASE,
            json={"name": key_name, "expired_time": 0, "unlimited_quota": True},
            timeout=30,
        )
        data = safe_json(resp)
    except requests.exceptions.RequestException as e:
        return None, "create req err: %s" % str(e)[:50]
    if not data.get("success"):
        return None, "create failed: %s" % data.get("message", data.get("_raw", "unknown"))

    try:
        resp2 = s.get("%s/api/token/" % API_BASE, timeout=30)
        data2 = safe_json(resp2)
    except requests.exceptions.RequestException as e:
        return None, "list req err: %s" % str(e)[:50]
    if not data2.get("success"):
        return None, "list failed: %s" % data2.get("_raw", "unknown")

    items = data2.get("data", {})
    if isinstance(items, dict):
        items = items.get("items", [])

    for t in items:
        if t.get("name") == key_name:
            return t["id"], None

    return None, "token not found after creation"

def get_full_key(session_cookie, uid, token_id, proxy=None):
    s = requests.Session()
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    s.cookies.set("session", session_cookie, domain="getunikey.ai")
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0",
        "New-Api-User": str(uid),
        "Accept": "application/json",
        "Content-Type": "application/json",
    })

    try:
        resp = s.post("%s/api/token/%d/key" % (API_BASE, token_id), timeout=30)
        data = safe_json(resp)
    except requests.exceptions.RequestException as e:
        return None, "get key req err: %s" % str(e)[:50]
    if not data.get("success"):
        return None, "get key failed: %s" % data.get("message", data.get("_raw", "unknown"))

    raw_key = data.get("data", {}).get("key", "")
    if not raw_key:
        return None, "empty key in response"

    if not raw_key.startswith("sk-"):
        raw_key = "sk-" + raw_key

    return raw_key, None

def delete_all_keys(session_cookie, uid, proxy=None):
    s = requests.Session()
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    s.cookies.set("session", session_cookie, domain="getunikey.ai")
    s.headers.update({
        "User-Agent": "Mozilla/5.0",
        "New-Api-User": str(uid),
        "Accept": "application/json",
    })

    try:
        resp = s.get("%s/api/token/" % API_BASE, timeout=30)
        data = safe_json(resp)
    except requests.exceptions.RequestException as e:
        return 0, "list req err: %s" % str(e)[:50]
    items = data.get("data", {})
    if isinstance(items, dict):
        items = items.get("items", [])

    deleted = 0
    for t in items:
        try:
            s.delete("%s/api/token/%d" % (API_BASE, t["id"]), timeout=30)
            deleted += 1
        except requests.exceptions.RequestException:
            continue

    return deleted, None


# ===================== TUI =====================
def make_banner():
    b = Text(justify="center")
    b.append("GETUNIKEY BOT", style="bold white")
    b.append("\n")
    b.append("Camoufox + API Key Extraction | Pure REST, No Web UI", style="dim cyan")
    return Panel(Align.center(b, vertical="middle"), border_style="bold cyan", box=box.HEAVY)


def make_status_box():
    with state.lock:
        status_text = state.status
        current = state.current
        total = state.total
        wallet = state.current_wallet
        elapsed = time.time() - state.start_time
        avg = state.avg_time
        headless = state.headless
        proxy_desc = state.proxy_desc

    spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.time() * 10) % 10]

    inner = Table.grid(padding=(0, 2))
    inner.add_column(style="bold cyan", width=10)
    inner.add_column(style="white")
    inner.add_row("Status", f"{spinner} {status_text}")
    inner.add_row("Wallet", wallet[:25] + "..." if len(wallet) > 25 else wallet or "-")
    inner.add_row("Current", f"{current}/{total}")
    inner.add_row("Headless", "Yes" if headless else "No")
    inner.add_row("Proxy", proxy_desc)
    inner.add_row("Avg", f"{avg:.1f}s" if avg else "-")
    inner.add_row("Elapsed", f"{elapsed:.0f}s")
    return Panel(inner, title="[bold cyan]Status[/]", border_style="cyan", box=box.ROUNDED)


def make_stats_box():
    with state.lock:
        success = state.success
        failed = state.failed
    done = success + failed
    pct = f"{success/done*100:.0f}%" if done > 0 else "0%"
    inner = Table.grid(padding=(0, 2))
    inner.add_column(justify="center")
    inner.add_column(justify="center")
    inner.add_row(Text(f"{success}", style="bold green"), Text(f"{failed}", style="bold red"))
    inner.add_row(Text("OK", style="green"), Text("FAIL", style="red"))
    inner.add_row(Text(""), Text(""))
    inner.add_row(Text(pct, style="bold yellow"), Text(f"{done}/{state.total}", style="dim"))
    return Panel(inner, title="[bold cyan]Stats[/]", border_style="cyan", box=box.ROUNDED)


def make_progress_box():
    with state.lock:
        total = state.total
        done = state.success + state.failed
    bar_width = 40
    filled = int(done / total * bar_width) if total > 0 else 0
    bar = Text()
    bar.append("█" * filled, style="bold green")
    bar.append("░" * (bar_width - filled), style="dim")
    inner = Table.grid(padding=(0, 1))
    inner.add_column()
    inner.add_row(bar)
    inner.add_row(Align.center(Text(f" {done}/{total} ", style="bold white")))
    return Panel(inner, title="[bold cyan]Progress[/]", border_style="cyan", box=box.ROUNDED)


def make_results_box():
    with state.lock:
        results = list(state.results)
    if not results:
        return Panel(Align.center(Text("Waiting for first account...", style="dim italic")),
                     title="[bold cyan]Results[/]", border_style="cyan", box=box.ROUNDED)
    table = Table(box=box.SIMPLE, header_style="bold cyan", border_style="dim cyan")
    table.add_column("#", style="dim", width=3)
    table.add_column("Wallet", style="green")
    table.add_column("Key", style="yellow")
    table.add_column("Status", justify="center")
    for i, (w, k, s) in enumerate(results[-12:], 1):
        style = "bold green" if s == "OK" else "bold red"
        preview = k[:12] + "..." + k[-6:] if s == "OK" and len(k) > 20 else k
        table.add_row(str(i), w, preview, Text(s, style=style))
    return Panel(table, title="[bold cyan]Results[/]", border_style="cyan", box=box.ROUNDED)


def make_layout():
    layout = Layout()
    layout.split(Layout(name="banner", size=3), Layout(name="main"), Layout(name="footer", size=1))
    layout["main"].split(Layout(name="top"), Layout(name="bottom"))
    layout["top"].split_row(
        Layout(name="status", ratio=2),
        Layout(name="stats", ratio=1),
    )
    layout["bottom"].split_row(
        Layout(name="progress", ratio=1),
        Layout(name="results", ratio=2),
    )
    return layout


def update_layout():
    layout = make_layout()
    layout["banner"].update(make_banner())
    layout["status"].update(make_status_box())
    layout["stats"].update(make_stats_box())
    layout["progress"].update(make_progress_box())
    layout["results"].update(make_results_box())
    return layout


# ===================== MAIN =====================
_append_lock = threading.Lock()


def _append_line(path, line):
    """Append one line to a file immediately. Crash-safe + thread-safe."""
    try:
        with _append_lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
    except OSError as e:
        print("[!] cannot append to %s: %s" % (path.name, e), flush=True)


def append_result(short_addr, full_key):
    """Append a successful key line to wallet_apikey.txt immediately."""
    _append_line(OUTPUT_FILE, "%s|%s" % (short_addr, full_key))


def append_failed(short_addr, err):
    """Append a failed account to multi_failed.txt immediately."""
    _append_line(FAILED_FILE, "%s|%s" % (short_addr, err))


def run_account(browser, w, idx, total):
    """Process one account over a proxy-rotating attempt loop. Returns True on success."""
    addr = w["address"]
    privkey = w["private_key"]
    short = addr[:8] + "..." + addr[-4:]
    key_name = "%s-%s" % (KEY_NAME_PREFIX, addr[2:8])

    with state.lock:
        state.current_wallet = short
        state.proxy_desc = proxy_mgr.desc()

    last_err = ""
    for attempt in range(MAX_RETRIES):
        ctx = None
        page = None
        try:
            cur = proxy_mgr.current()
            rurl = None if cur is None else cur.get("url")
            with state.lock:
                state.proxy_desc = proxy_mgr.desc()
            update_status("Proxy %s (attempt %d/%d)..." % (proxy_mgr.desc(), attempt + 1, MAX_RETRIES))

            # Fresh context per attempt = fresh cookies + fingerprint + proxy
            bp = proxy_mgr.browser_proxy()
            if bp:
                ctx = browser.new_context(proxy=bp)
            else:
                ctx = browser.new_context()

            page = ctx.new_page()

            # Step 1: Turnstile (browser auto-solve, fallback CapSolver -> 2Captcha)
            turnstile = solve_turnstile(page)
            if not turnstile:
                update_status("Turnstile fail -> CapSolver/2Captcha...")
                turnstile, src, serr = cs.solve_turnstile_external(log=update_status)
                if turnstile:
                    update_status("Turnstile solved via %s" % src)
                else:
                    last_err = "turnstile fail (%s)" % (serr or "browser timeout")[:50]
                    proxy_mgr.advance(last_err)
                    if page:
                        try: page.close()
                        except: pass
                        page = None
                    if ctx:
                        try: ctx.close()
                        except: pass
                        ctx = None
                    update_status("Turnstile fail, retry %d/%d..." % (attempt + 1, MAX_RETRIES))
                    sleep_backoff(serr, attempt)
                    continue

            # Step 2: API login
            update_status("API login (attempt %d)..." % (attempt + 1))
            login_data, err = api_login(page, addr, privkey, turnstile)
            if not login_data:
                last_err = err
                if is_rate_limited(err):
                    mark_ratelimit()
                proxy_mgr.advance(last_err)
                if page:
                    try: page.close()
                    except: pass
                    page = None
                if ctx:
                    try: ctx.close()
                    except: pass
                    ctx = None
                update_status("Login fail: %s -> next proxy, retry %d/%d..." % (err[:30], attempt + 1, MAX_RETRIES))
                sleep_backoff(err, attempt)
                continue

            uid = login_data["id"]
            actual_uname = login_data["username"]

            session = get_session_cookie(ctx)
            if not session:
                last_err = "no session cookie"
                proxy_mgr.advance(last_err)
                if page:
                    try: page.close()
                    except: pass
                    page = None
                if ctx:
                    try: ctx.close()
                    except: pass
                    ctx = None
                sleep_backoff(None, attempt)
                continue

            # Step 3: Create key
            update_status("Creating API key...")
            token_id, err = create_api_key(session, uid, key_name, proxy=rurl)
            if not token_id:
                last_err = err
                if is_rate_limited(err):
                    mark_ratelimit()
                proxy_mgr.advance(last_err)
                if page:
                    try: page.close()
                    except: pass
                    page = None
                if ctx:
                    try: ctx.close()
                    except: pass
                    ctx = None
                sleep_backoff(err, attempt)
                continue

            # Step 4: Get full key
            update_status("Fetching full key...")
            full_key, err = get_full_key(session, uid, token_id, proxy=rurl)
            if full_key and full_key.startswith("sk-") and len(full_key) > 20:
                elapsed = time.time() - state.start_time
                with state.lock:
                    state.success += 1
                    state.results.append((short, full_key, "OK"))
                    if state.avg_time == 0:
                        state.avg_time = elapsed
                    else:
                        state.avg_time = state.avg_time * 0.7 + elapsed * 0.3
                mark_success()
                proxy_mgr.confirm()
                # Immediate per-line append so nothing is lost on crash
                append_result(short, full_key)
                update_status("OK %s (%s)" % (actual_uname, proxy_mgr.desc()))
                return True
            else:
                last_err = err or "key error"
                if is_rate_limited(err):
                    mark_ratelimit()
                proxy_mgr.advance(last_err)
                if page:
                    try: page.close()
                    except: pass
                    page = None
                if ctx:
                    try: ctx.close()
                    except: pass
                    ctx = None
                sleep_backoff(err, attempt)
                continue

        except Exception as e:
            last_err = str(e)[:50]
            update_status("ERROR: %s" % last_err)
            if is_rate_limited(str(e)):
                mark_ratelimit()
            proxy_mgr.advance(last_err)
            if page:
                try: page.close()
                except: pass
                page = None
            if ctx:
                try: ctx.close()
                except: pass
                ctx = None
            sleep_backoff(str(e), attempt)
            continue
        finally:
            if page:
                try: page.close()
                except: pass
                page = None
            if ctx:
                try: ctx.close()
                except: pass
                ctx = None

    # All retries exhausted
    with state.lock:
        state.failed += 1
        state.results.append((short, last_err[:50], "FAIL"))
    append_failed(short, last_err[:100])
    return False


def save_results():
    """Persist multi_accounts.json (MERGE — never wipe) + print summary. Crash-safe.

    wallet_apikey.txt is append-only (see append_result) — never rewritten.
    multi_accounts.json keeps ALL history across runs: existing records are
    loaded, new records appended (deduped by wallet address), then written back.
    """
    if state is None:
        return
    with state.lock:
        success = state.success
        failed = state.failed
        results = list(state.results)
        elapsed = time.time() - state.start_time
        avg = state.avg_time

    try:
        # Load previously saved records (if any)
        old = []
        if DETAILS_FILE.exists():
            try:
                old = json.loads(DETAILS_FILE.read_text(encoding="utf-8"))
                if not isinstance(old, list):
                    old = []
            except Exception:
                old = []  # corrupt file -> start fresh but do NOT delete it

        # New records from this run
        fresh = [{"wallet": r[0], "key": r[1], "status": r[2]} for r in results]

        # Merge: keep old records, append new ones not already present (by wallet+status)
        seen = set()
        merged = []
        for rec in old + fresh:
            dedupe_key = (rec.get("wallet"), rec.get("status"))
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            merged.append(rec)

        with open(DETAILS_FILE, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2)
    except OSError as e:
        print("[!] cannot write results: %s" % e)

    if sys.stdout.isatty():
        print()
        print("=" * 60)
        print("SUMMARY: %d success, %d failed, %.0fs total" % (success, failed, elapsed))
        if avg:
            print("Avg time/account: %.1fs" % avg)
        try:
            print("Results saved to: %s" % DETAILS_FILE)
            print("Keys saved to:   %s" % OUTPUT_FILE)
        except Exception:
            pass
        print("=" * 60)
    else:
        print("%d/%d OK, %d FAIL" % (success, failed + success, failed))


def parse_count_interactive():
    """Return count. Use argv number if provided, else prompt (retry on bad input)."""
    for a in sys.argv[1:]:
        if a.lstrip("-").isdigit():
            v = int(a)
            if v > 0:
                return v
            raise SystemExit("[!] Count must be > 0. Got: %s" % a)
    # Interactive prompt with retry
    while True:
        try:
            raw = input("Berapa akun mau dibuat? [1-100] > ").strip()
            if not raw:
                raw = "1"
            count = int(raw)
            if count < 1 or count > 100:
                print("[!] Invalid count '%s'. Must be 1-100." % raw)
                continue
            return count
        except ValueError:
            print("[!] Must be a number. Got: %s" % raw)
            continue
        except (EOFError, KeyboardInterrupt):
            print("\n[!] Aborted.")
            sys.exit(1)


def main():
    global state

    headless = "--headless" in sys.argv
    has_count = any(a.lstrip("-").isdigit() for a in sys.argv[1:])

    # Interactive (no count arg): show banner + ask questions
    if not has_count:
        print("=" * 50)
        print(" GETUNIKEY BOT — Account Creator")
        print("=" * 50)

    count = parse_count_interactive()

    # Headless prompt (skip if already set via flag)
    if not headless and not has_count:
        try:
            ha = input("Headless mode (no browser window)? [y/N] > ").strip().lower()
            headless = ha in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            headless = False

    mnemonic, master_acct = load_wallet_info()
    wallets = derive_wallets(mnemonic, count)

    state = State(count, headless)

    # Proxy rotation: start local, rotate through proxies.txt on failures
    global proxy_mgr
    proxy_mgr = pm.ProxyManager(pm.load_proxies())
    print("[proxy] %d proxy loaded, start: %s" % (len(proxy_mgr.sequence) - 1, proxy_mgr.desc()))

    # Lazy import: only needed to actually launch the browser
    try:
        from camoufox.sync_api import Camoufox
    except ImportError as e:
        print("[!] camofox/playwright missing. Install:  pip install -r requirements.txt")
        print("    Detail: %s" % e)
        sys.exit(1)

    is_tty = sys.stdout.isatty()
    if is_tty:
        print("\033[?25l")  # hide cursor

    try:
        try:
            from camoufox.exceptions import CamoufoxNotInstalled
        except ImportError:
            CamoufoxNotInstalled = None
        with Camoufox(headless=headless) as browser:
            if is_tty:
                with Live(update_layout(), refresh_per_second=4, screen=True) as live:
                    for idx, w in enumerate(wallets):
                        with state.lock:
                            state.current = idx + 1
                        run_account(browser, w, idx, count)
                        live.update(update_layout())

                        # Delay between accounts (longer after rate-limit streak)
                        if idx < count - 1:
                            with state.lock:
                                rl = state.ratelimit_streak
                                paused = state.rate_paused
                            if rl >= 1 and paused:
                                update_status("Rate-limit cooldown 30s...")
                                live.update(update_layout())
                                time.sleep(30)
                            else:
                                update_status("Cooldown 4s...")
                                live.update(update_layout())
                                time.sleep(4)

                    update_status("Done!")
                    live.update(update_layout())
                    time.sleep(1)
            else:
                # Non-TTY: plain output
                for idx, w in enumerate(wallets):
                    with state.lock:
                        state.current = idx + 1
                    short = w["address"][:8] + "..." + w["address"][-4:]
                    print("[%d/%d] %s" % (idx + 1, count, short), flush=True)
                    run_account(browser, w, idx, count)
                    if idx < count - 1:
                        with state.lock:
                            rl = state.ratelimit_streak
                            paused = state.rate_paused
                        if rl >= 1 and paused:
                            print("[*] Rate-limit cooldown 30s...", flush=True)
                            time.sleep(30)
                        else:
                            time.sleep(4)

    except KeyboardInterrupt:
        print("\n[!] Interrupted by user.", flush=True)
    except CamoufoxNotInstalled:
        print("\n[!] Camoufox browser binary missing.", flush=True)
        try:
            ans = input("Install it now (downloads ~493MB)? [y/N] > ").strip().lower()
            if ans in ("y", "yes"):
                import subprocess
                print("  Running: camoufox fetch (this may take a while)...", flush=True)
                code = subprocess.call(["camoufox", "fetch"])
                if code != 0:
                    print("  [!] camoufox fetch failed (exit %d)." % code)
                    print("      Run manually:  camoufox fetch")
                else:
                    print("  [+] Camoufox installed. Re-run the script.")
            else:
                print("  Manual install:  camoufox fetch")
        except (EOFError, KeyboardInterrupt):
            print("  Manual install:  camoufox fetch")
        except OSError as e:
            print("  [!] cannot run camoufox fetch: %s" % e)
    except Exception as e:
        print("\n[!] Unexpected error: %s" % e, flush=True)
        import traceback
        traceback.print_exc()
    finally:
        if is_tty:
            print("\033[?25h")  # show cursor
        save_results()


if __name__ == "__main__":
    main()
