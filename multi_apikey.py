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
from camoufox.sync_api import Camoufox
from eth_account import Account, messages
Account.enable_unaudited_hdwallet_features()
import requests

from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.layout import Layout
from rich.text import Text
from rich.align import Align
from rich import box

# ===================== CONFIG =====================
MAX_RETRIES = 3
RETRY_DELAY = 3

CHAIN_ID = 56
KEY_NAME_PREFIX = "9router"
API_BASE = "https://www.getunikey.ai"
SIGN_IN_URL = "https://www.getunikey.ai/sign-in"
OUTPUT_FILE = Path(__file__).parent / "wallet_apikey.txt"
DETAILS_FILE = Path(__file__).parent / "multi_accounts.json"
TURNSTILE_JS = """(function(){
    var e = document.querySelector('input[name="cf-turnstile-response"]');
    return e ? e.value : '';
})()"""

# ===================== STATE =====================
class State:
    def __init__(self, total, headless, clean):
        self.total = total
        self.headless = headless
        self.clean = clean
        self.success = 0
        self.failed = 0
        self.current = 0
        self.status = "Initializing..."
        self.current_wallet = ""
        self.results = []  # (short_addr, full_key_or_err, status)
        self.lock = threading.RLock()
        self.start_time = time.time()
        self.avg_time = 0

state = None

def update_status(msg):
    with state.lock:
        state.status = msg

# ===================== HELPERS =====================
def load_wallet_info():
    with open(Path(__file__).parent / "wallet_info.json") as f:
        wallet = json.load(f)
    mnemonic = wallet["mnemonic"]
    master_acct = Account.from_mnemonic(mnemonic)
    return mnemonic, master_acct

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
        except Exception:
            pass
    return None

def safe_evaluate(page, js, retries=3, delay=2):
    """Evaluate JS with retry on Playwright serialize errors."""
    for attempt in range(retries):
        try:
            return page.evaluate(js)
        except Exception as e:
            err = str(e)
            if "JSON.parse" in err or "unexpected end" in err or "Target closed" in err:
                if attempt < retries - 1:
                    time.sleep(delay)
                    continue
            raise
    return None

def solve_turnstile(page, timeout=90):
    update_status("Loading sign-in page...")
    page.goto(SIGN_IN_URL, wait_until="load", timeout=60000)
    time.sleep(5)

    try:
        has_turnstile = safe_evaluate(page, 'document.querySelector(\'input[name="cf-turnstile-response"]\')')
    except:
        has_turnstile = None

    if not has_turnstile:
        update_status("Reloading page...")
        page.reload(wait_until="domcontentloaded", timeout=30000)
        time.sleep(8)

    update_status("Solving Turnstile...")
    turnstile = wait_turnstile(page, timeout)
    if turnstile:
        update_status("Turnstile solved! (%d chars)" % len(turnstile))
    else:
        update_status("FAIL: Turnstile not solved")
    return turnstile

def api_login(page, addr, privkey, turnstile):
    # Wait for page to be fully ready
    time.sleep(2)

    js_challenge = """(async function(){
        var r = await fetch('/api/oauth/web3/challenge',{
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({wallet_address:'""" + addr + """'})
        });
        var text = await r.text();
        try { return JSON.parse(text); } catch(e) { return {_raw: text.substring(0,200), _err: e.message}; }
    })()"""

    try:
        challenge = safe_evaluate(page, js_challenge, retries=3, delay=3)
    except Exception as e:
        return None, "evaluate error: %s" % str(e)[:30]

    if not challenge:
        return None, "evaluate returned None"

    if "_err" in challenge:
        return None, "JSON parse err: %s" % challenge.get("_raw", "")[:20]

    if not challenge.get("success"):
        return None, "challenge failed"

    msg = challenge["data"]["message"]
    nonce = challenge["data"]["nonce"]

    acct = Account.from_key(privkey)
    if msg.startswith("0x"):
        signed = acct.sign_message(messages.encode_defunct(hexstr=msg))
    else:
        signed = acct.sign_message(messages.encode_defunct(text=msg))
    sig = "0x" + signed.signature.hex()

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
        return await r.json();
    })()""")

    if not verify.get("success"):
        return None, "verify failed: %s" % verify.get("message", "unknown")

    return verify["data"], None

def get_session_cookie(ctx):
    for c in ctx.cookies():
        if c["name"] == "session":
            return c["value"]
    return None

def create_api_key(session_cookie, uid, key_name):
    s = requests.Session()
    s.cookies.set("session", session_cookie, domain="getunikey.ai")
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0",
        "New-Api-User": str(uid),
        "Accept": "application/json",
        "Content-Type": "application/json",
    })

    resp = s.post(
        "%s/api/token/" % API_BASE,
        json={"name": key_name, "expired_time": 0, "unlimited_quota": True},
        timeout=30,
    )
    data = resp.json()
    if not data.get("success"):
        return None, "create failed: %s" % data.get("message", "unknown")

    resp2 = s.get("%s/api/token/" % API_BASE, timeout=30)
    data2 = resp2.json()
    if not data2.get("success"):
        return None, "list failed"

    items = data2.get("data", {})
    if isinstance(items, dict):
        items = items.get("items", [])

    for t in items:
        if t.get("name") == key_name:
            return t["id"], None

    return None, "token not found after creation"

def get_full_key(session_cookie, uid, token_id):
    s = requests.Session()
    s.cookies.set("session", session_cookie, domain="getunikey.ai")
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0",
        "New-Api-User": str(uid),
        "Accept": "application/json",
        "Content-Type": "application/json",
    })

    resp = s.post("%s/api/token/%d/key" % (API_BASE, token_id), timeout=30)
    data = resp.json()
    if not data.get("success"):
        return None, "get key failed: %s" % data.get("message", "unknown")

    raw_key = data.get("data", {}).get("key", "")
    if not raw_key:
        return None, "empty key in response"

    if not raw_key.startswith("sk-"):
        raw_key = "sk-" + raw_key

    return raw_key, None

def delete_all_keys(session_cookie, uid):
    s = requests.Session()
    s.cookies.set("session", session_cookie, domain="getunikey.ai")
    s.headers.update({
        "User-Agent": "Mozilla/5.0",
        "New-Api-User": str(uid),
        "Accept": "application/json",
    })

    resp = s.get("%s/api/token/" % API_BASE, timeout=30)
    data = resp.json()
    items = data.get("data", {})
    if isinstance(items, dict):
        items = items.get("items", [])

    for t in items:
        s.delete("%s/api/token/%d" % (API_BASE, t["id"]), timeout=30)

    return len(items)


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
        clean = state.clean

    spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.time() * 10) % 10]

    inner = Table.grid(padding=(0, 2))
    inner.add_column(style="bold cyan", width=10)
    inner.add_column(style="white")
    inner.add_row("Status", f"{spinner} {status_text}")
    inner.add_row("Wallet", wallet[:25] + "..." if len(wallet) > 25 else wallet or "-")
    inner.add_row("Current", f"{current}/{total}")
    inner.add_row("Headless", "Yes" if headless else "No")
    inner.add_row("Clean", "Yes" if clean else "No")
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
def run_account(ctx, w, idx, total, clean):
    """Process one account. Returns True on success."""
    addr = w["address"]
    privkey = w["private_key"]
    short = addr[:8] + "..." + addr[-4:]
    uname = "wallet_" + addr[2:8]
    key_name = "%s-%s" % (KEY_NAME_PREFIX, addr[2:8])

    with state.lock:
        state.current_wallet = short

    page = None
    last_err = ""
    for attempt in range(MAX_RETRIES):
        try:
            # Fresh page per account
            if idx > 0 or attempt > 0:
                update_status("Fresh session (attempt %d)..." % (attempt + 1))
                ctx.clear_cookies()
                time.sleep(2)

            page = ctx.new_page()
            time.sleep(2)  # Let page stabilize before any evaluate

            # Step 1: Turnstile
            turnstile = solve_turnstile(page)
            if not turnstile:
                last_err = "turnstile timeout"
                if page:
                    try: page.close()
                    except: pass
                    page = None
                update_status("Turnstile fail, retry %d/%d..." % (attempt + 1, MAX_RETRIES))
                time.sleep(RETRY_DELAY)
                continue

            # Step 2: API login
            update_status("API login (attempt %d)..." % (attempt + 1))
            login_data, err = api_login(page, addr, privkey, turnstile)
            if not login_data:
                last_err = err
                if page:
                    try: page.close()
                    except: pass
                    page = None
                update_status("Login fail: %s, retry %d/%d..." % (err[:30], attempt + 1, MAX_RETRIES))
                time.sleep(RETRY_DELAY)
                continue

            uid = login_data["id"]
            actual_uname = login_data["username"]

            session = get_session_cookie(ctx)
            if not session:
                last_err = "no session cookie"
                if page:
                    try: page.close()
                    except: pass
                    page = None
                time.sleep(RETRY_DELAY)
                continue

            # Step 3: Create key
            update_status("Creating API key...")
            if clean:
                deleted = delete_all_keys(session, uid)
                if deleted > 0:
                    update_status("Deleted %d old keys..." % deleted)

            token_id, err = create_api_key(session, uid, key_name)
            if not token_id:
                last_err = err
                if page:
                    try: page.close()
                    except: pass
                    page = None
                time.sleep(RETRY_DELAY)
                continue

            # Step 4: Get full key
            update_status("Fetching full key...")
            full_key, err = get_full_key(session, uid, token_id)
            if full_key and full_key.startswith("sk-") and len(full_key) > 20:
                elapsed = time.time() - state.start_time
                with state.lock:
                    state.success += 1
                    state.results.append((short, full_key, "OK"))
                    if state.avg_time == 0:
                        state.avg_time = elapsed
                    else:
                        state.avg_time = state.avg_time * 0.7 + elapsed * 0.3
                update_status("OK %s" % actual_uname)
                return True
            else:
                last_err = err or "key error"
                if page:
                    try: page.close()
                    except: pass
                    page = None
                time.sleep(RETRY_DELAY)
                continue

        except Exception as e:
            last_err = str(e)[:50]
            update_status("ERROR: %s" % last_err)
            if page:
                try: page.close()
                except: pass
                page = None
            time.sleep(RETRY_DELAY)
            continue
        finally:
            if page:
                try: page.close()
                except: pass
                page = None

    # All retries exhausted
    with state.lock:
        state.failed += 1
        state.results.append((short, last_err[:50], "FAIL"))
    return False


def main():
    global state

    # --- Parse args ---
    if len(sys.argv) < 2:
        print("Usage: python multi_apikey.py <count> [options]")
        print("")
        print("  count       Number of accounts to create (required)")
        print("")
        print("Options:")
        print("  --headless  Run browser in headless mode (no GUI)")
        print("  --clean     Delete existing keys before creating new ones")
        print("")
        print("Examples:")
        print("  python multi_apikey.py 5              # 5 accounts, tanya clean")
        print("  python multi_apikey.py 5 --headless   # 5 accounts, headless")
        print("  python multi_apikey.py 3 --clean      # 3 accounts, hapus key lama")
        print("")
        print("Output:")
        print("  wallet_apikey.txt   wallet_address|sk-full-key")
        print("  multi_accounts.json detailed results per account")
        sys.exit(1)

    count = int(sys.argv[1])
    headless = "--headless" in sys.argv
    clean = "--clean" in sys.argv

    # Interactive prompt if --clean not specified
    if not clean:
        try:
            answer = input("Delete existing keys before creating new? [y/N] ").strip().lower()
            clean = answer in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            clean = False

    mnemonic, master_acct = load_wallet_info()
    wallets = derive_wallets(mnemonic, count)

    state = State(count, headless, clean)

    is_tty = sys.stdout.isatty()
    if is_tty:
        print("\033[?25l")  # hide cursor

    try:
        with Camoufox(headless=headless) as browser:
            ctx = browser.new_context()

            if is_tty:
                with Live(update_layout(), refresh_per_second=4, screen=True) as live:
                    for idx, w in enumerate(wallets):
                        with state.lock:
                            state.current = idx + 1
                        run_account(ctx, w, idx, count, clean)
                        live.update(update_layout())

                        # Delay between accounts
                        if idx < count - 1:
                            update_status("Cooldown 3s...")
                            live.update(update_layout())
                            time.sleep(3)

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
                    run_account(ctx, w, idx, count, clean)

            ctx.close()

    except KeyboardInterrupt:
        pass
    finally:
        if is_tty:
            print("\033[?25h")  # show cursor

    # --- Save results ---
    with state.lock:
        success = state.success
        failed = state.failed
        results = list(state.results)
        elapsed = time.time() - state.start_time
        avg = state.avg_time

    # Save wallet_apikey.txt (full keys from multi_accounts.json)
    # We need to re-read from the detailed results
    lines = []
    for r in results:
        addr, key_or_err, status = r
        if status == "OK":
            lines.append("%s|%s" % (addr, key_or_err))

    # Save wallet_apikey.txt
    with open(OUTPUT_FILE, "w") as f:
        for line in lines:
            f.write(line + "\n")

    # Save detailed results
    with open(DETAILS_FILE, "w") as f:
        json.dump([{"wallet": r[0], "key": r[1], "status": r[2]} for r in results], f, indent=2)

    # Summary
    if is_tty:
        print()
        print("=" * 60)
        print("SUMMARY: %d success, %d failed, %.0fs total" % (success, failed, elapsed))
        if avg:
            print("Avg time/account: %.1fs" % avg)
        print("Results saved to: %s" % DETAILS_FILE)
        print("=" * 60)
    else:
        print("%d/%d OK, %d FAIL" % (success, count, failed))


if __name__ == "__main__":
    main()
