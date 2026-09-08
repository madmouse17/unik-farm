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
5. Save to wallet_apikey.txt
```

No web UI navigation. No clipboard tricks. Pure REST API.

## Prerequisites

- Python 3.10+ (tested on 3.11, 3.12)
- Camoufox (anti-detect Firefox)
- eth_account (Ethereum signing)

## Setup

```bash
# 1. Create/activate venv
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Linux/Mac

# 2. Install dependencies
pip install camoufox eth-account requests

# 3. Prepare wallet_info.json
# (must contain HD wallet mnemonic for deriving accounts)
```

### wallet_info.json format

```json
{
  "mnemonic": "your twelve word mnemonic phrase goes here ..."
}
```

This mnemonic is used to derive child wallets (m/44'/60'/0'/0/0, /1, /2, ...).
Each child wallet becomes a separate getunikey.ai account.

## Usage

```bash
# Create 1 account (browser window visible, tanya clean)
python multi_apikey.py 1

# Create 5 accounts
python multi_apikey.py 5

# Headless mode (no browser window)
python multi_apikey.py 5 --headless

# Force clean old keys (skip prompt)
python multi_apikey.py 3 --clean

# Combine flags
python multi_apikey.py 10 --headless --clean
```

### Options

| Flag | Description |
|------|-------------|
| `<count>` | **(required)** Number of accounts to create |
| `--headless` | Run Camoufox in headless mode (no GUI) |
| `--clean` | Delete existing API keys before creating new ones |

Without `--clean`, the script will ask `Delete existing keys? [y/N]` (default: N = keep old keys).

## Output Files

### wallet_apikey.txt (main output)

Format: `wallet_address|sk-full-unmasked-key`

```
0xe6dE38E6bB8de3CC96f342c12707F0c8AC8973a8|sk-dlkb1BxTxFbL51csrZcee0KGMGBULoXymZ45VOp4oaqz0Sqd
0x6dE90e1aA70Cce61C9660152cbe5759722Ee1eBC|sk-aB3x...full...key...here
```

### multi_accounts.json (detailed results)

```json
[
  {
    "wallet": "0xe6dE...",
    "username": "wallet_0xe6dE38",
    "user_id": 157604,
    "session": "...",
    "api_key": "sk-dlkb1Bx...0Sqd",
    "token_id": 156906,
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

### Key extraction returns empty
- Check if `/api/token/{id}/key` endpoint is still available
- The API may have changed — the script logs full responses for debugging

## Security Notes

- `wallet_info.json` contains your HD mnemonic — NEVER commit it
- `wallet_apikey.txt` contains full API keys — NEVER commit it
- `.gitignore` is configured to exclude all credential files
- All API keys are per-account, revocable from the getunikey.ai dashboard

## File Structure

```
getunikey-bot/
  multi_apikey.py      # Main script (create accounts + extract keys)
  wallet_login.py      # Single account login utility
  wallet_info.json     # HD wallet mnemonic (DO NOT COMMIT)
  wallet_apikey.txt    # Output: wallet|sk-full-key (DO NOT COMMIT)
  multi_accounts.json  # Detailed results (DO NOT COMMIT)
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
