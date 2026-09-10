# getunikey-bot

Bulk create getunikey.ai accounts + extract FULL unmasked API keys via pure API.

## ☕ Buy Me a Coffee

Kalau project ini bermanfaat dan kamu mau support, bisa traktir kopi ☕

<p align="center">
  <img src="qris.jpeg" alt="QRIS — Buy Me a Coffee" width="280">
</p>

Scan QRIS di atas via e-wallet apa saja (GoPay, OVO, DANA, ShopeePay, m-banking). Setiap kopi sangat berarti untuk terus develop tool gratis seperti ini 🙏

## How It Works

```
1. Camoufox opens /sign-in, auto-solves Cloudflare Turnstile
2. API login: POST challenge -> sign with wallet -> POST verify -> session cookie
3. POST /api/token/       -> create new API key (returns token ID)
4. POST /api/token/{id}/key -> fetch FULL unmasked key (sk-xxxx)
5. Append to wallet_apikey.txt immediately, one line per success
```

No web UI navigation. No clipboard tricks. Pure REST API.

Key extraction is rate-limited per IP: after ~10-15 challenges getunikey.ai returns HTTP 429.
The script handles that automatically:

- On failure it rotates to the next proxy (proxies.txt), then retries
- Rate-limit cooldowns ramp up (45s x attempt, plus extra pauses)
- Detected via status code + Chinese/English error keywords

## Prerequisites

- Python 3.10+ (tested on 3.11, 3.12)
- Camoufox (anti-detect Firefox)
- eth_account (Ethereum signing)
- PySocks (only needed for `socks5://` proxies)

## Setup

```bash
# 1. Create/activate venv
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Linux/Mac

# 2. Install dependencies
pip install -r requirements.txt

# 3. Download the camoufox browser binary (one time, ~493 MB)
camoufox fetch

# 4. Prepare wallet_info.json (see below)
```

### wallet_info.json format

```json
{
  "mnemonic": "your twelve word mnemonic phrase goes here ..."
}
```

This mnemonic is used to derive child wallets (m/44'/60'/0'/0/0, /1, /2, ...).
Each child wallet becomes a separate getunikey.ai account.

If `wallet_info.json` is missing or corrupt the script generates a brand new
master wallet automatically (old file is backed up as `.json.bak`).

## Usage

Run without arguments for an interactive wizard:

```bash
python multi_apikey.py
```

```
==================================================
 GETUNIKEY BOT — Account Creator
==================================================
Berapa akun mau dibuat? [1-100] > 5
Headless mode (no browser window)? [y/N] > n
```

You can also pass everything as arguments:

```bash
# Create 5 accounts, browser window visible
python multi_apikey.py 5

# Headless, no prompts (count given => only "delete keys" style Qs are auto)
python multi_apikey.py 10 --headless
```

### Options

| Flag | Description |
|------|-------------|
| `<count>`  | Number of accounts to create (optional; askes interactively if missing) |
| `--headless` | Run Camoufox in headless mode (no GUI) |

## Proxy rotation

The script starts on your **local IP**. Every time an account fails and the
retry loop is exhausted, the proxy router advances to the next entry in
`proxies.txt`:

```
local -> proxy1 -> proxy2 -> ... -> proxyN -> local -> ...
```

- **Success** on the current hop: it stays there for the next account.
- **Failure** after retries: it rotates to the next route in the file.
- Wraps back to local after the last proxy.

Because each rotation attempt uses a **fresh browser context**, cookies and
fingerprints are not reused across proxies.

### proxies.txt format

Start from the template `proxies.example` (copy to `proxies.txt`):

```
# host:port
123.45.67.89:8080

# host:port:user:pass
123.45.67.89:8080:username:password

# full URL
http://user:pass@123.45.67.89:8080
socks5://123.45.67.89:1080
```

`socks5://` proxies require PySocks (`pip install PySocks`, included in requirements.txt).

> `proxies.txt` is gitignored (real pool contains credentials). Only the
> credential-free `proxies.example` template is tracked.

## Output Files

### wallet_apikey.txt (main output, append-only)

Every successful account appends one line **immediately** (crash-safe, flushed):

```
wallet_address|sk-full-key
```

### multi_failed.txt (failures, append-only)

Failed accounts append one line each with the error, so nothing is lost:

```
wallet_address|error description
```

### multi_accounts.json (snapshot)

Full session summary written at the end of the run:

```json
[
  {
    "wallet": "0xe6dE38...",
    "key": "sk-dlkb1Bx...0Sqd",
    "status": "OK"
  }
]
```

## How Key Extraction Works

The getunikey.ai API masks keys in list responses:

```
GET  /api/token/         -> {"key": "VBq9**********QSWf"}   (masked)
POST /api/token/{id}/key -> {"key": "VBq97y4kQD...QSWf"}    (FULL)
```

The `sk-` prefix is added manually when saving (not returned by API).

**Important:** Keys MUST be created with `unlimited_quota: True`, otherwise they return 401 "Invalid token".

## Available Models

Model names are different from standard OpenAI. Use full provider/model format:

```
deepseek/deepseek-v4-flash
deepseek/deepseek-v4-pro
google/gemini-3.1-flash-lite
google/gemini-3.5-flash
google/gemini-3.1-pro-preview
x-ai/grok-4.3
z-ai/glm-5.1
moonshotai/kimi-k2.7-code
moonshotai/kimi-k3
gpt-5.5
gpt-5.6-sol
gpt-5.6-luna
gpt-5.6-terra
gpt-6-astra
gpt-image-2
minimax/minimax-m3
bytedance/seedance-2.5
```

Full list: `GET /v1/models` with Bearer auth.

## Troubleshooting

### Turnstile not solving

- Make sure `--headless` is OFF (visible browser helps)
- Check internet connection
- Turnstile sometimes needs a reload — the script auto-retries once

### 401 Unauthorized on API calls

- Session cookie expired
- Run the script again (fresh login each time)
### 429 rate-limited
- Normal for getunikey.ai after many rapid logins
- The script back-offs automatically (45s x attempt) and rotates proxies
- Add more proxies to `proxies.txt` to spread requests across IPs

### Key extraction returns empty

- Check if `/api/token/{id}/key` endpoint is still available
- The API may have changed — check `multi_failed.txt` / run non-headless to see live errors

## Security Notes

- `wallet_info.json` contains your HD mnemonic — NEVER commit it
- `wallet_apikey.txt` contains full API keys — NEVER commit it
- `proxies.txt` may contain proxy credentials — NEVER commit it
- `.gitignore` is configured to exclude all credential files
- All API keys are per-account, revocable from the getunikey.ai dashboard

## Auto-push to 9router

Every successful `sk-xxxx` key is immediately pushed as one entry
(`9router-<addr8>`) to the OpenAI-compatible provider node via `POST /api/providers`.

### Setup (follow in order)

```bash
# 1. Copy the example config first (DO NOT edit nine_router.example.json — the script never reads it)
cp nine_router.example.json nine_router.json

# 2. Fill in the DASHBOARD LOGIN password in nine_router.json
nano nine_router.json
```

> NOT the inference API key (`sk_9...` from Settings) — it only works for
> model usage (`/v1/*`) and is REJECTED by the add-key endpoint.
> File-less alternative: `export NINE_ROUTER_PASSWORD="your-password"`.

```bash
# 3. Check the connection with no side effects (must pass before bulk runs)
python nine_router.py --check

# 4. Run as usual (automatic push per successful account)
python multi_apikey.py 5

# Without push:
python multi_apikey.py 5 --no-push
```

Push status is recorded in `multi_accounts.json` under the `nine_router` field
(`PUSHED:<id>` / `PUSH-FAIL:<reason>` / `NOPUSH-NOCONFIG`).

### Troubleshooting `--check` unauthorized

1. `nine_router.json` doesn't exist yet (only the example) → repeat steps 1–2.
2. Wrong password / different from the one used to open the dashboard in the browser.
3. Filled in the inference API key instead → replace it with the dashboard password.
4. Dashboard uses SSO login → password doesn't apply, contact the server admin.

## File Structure

```
getunikey-bot/
  multi_apikey.py      # Main script (create accounts + extract keys)
  captcha_solvers.py   # Turnstile fallback solvers (CapSolver -> 2Captcha)
  proxy_manager.py     # Proxy router (local -> proxies.txt rotation)
  wallet_login.py      # Single account login utility
  nine_router.py       # Push key ke 9router provider node
  nine_router.json     # Password dashboard 9router (DO NOT COMMIT)
  wallet_info.json     # HD wallet mnemonic (DO NOT COMMIT)
  wallet_apikey.txt    # Output: wallet|sk-full-key (append-only, DO NOT COMMIT)
  multi_failed.txt     # Output: failed accounts (append-only, DO NOT COMMIT)
  multi_accounts.json  # End-of-run snapshot (DO NOT COMMIT)
  proxies.txt          # Proxy pool (DO NOT COMMIT)
  .env                 # API keys for captcha solvers (DO NOT COMMIT)
  .gitignore           # Protects credential files
  README.md            # This file
```

## ⚠️ Disclaimer

> **Project ini dibuat 100% untuk tujuan pembelajaran (educational purpose only).**
> Belajar browser automation, anti-detect fingerprinting, dan API integration itu legal —
> menyalahgunakan tools untuk aktivitas ilegal (fraud, spam, abuse layanan, atau apapun
> yang melanggar hukum & ToS) **BUKAN tanggung jawab author**.
>
> Kamu memakai script ini dengan risiko sendiri. Segala konsekuensi, sanksi, atau
> masalah hukum yang muncul dari penyalahgunaan sepenuhnya menjadi tanggung jawab
> pengguna. Gunakan dengan bijak, dan hormati sistem lain.