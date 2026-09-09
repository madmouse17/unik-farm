#!/usr/bin/env python3
"""
nine_router.py — Push API key ke 9router provider node (OpenAI-compatible).

Kontrak API (9router open source, decolua/9router):
  POST {base}/api/auth/login      {"password": "..."} -> cookie auth_token
  POST {base}/api/providers       {"provider": "<node-id>", "apiKey": "sk-..", "name": ".."} -> 201

Auth: inference API key (/v1/*) TIDAK berlaku untuk /api/providers.
Pakai password login dashboard (NINE_ROUTER_PASSWORD / nine_router.json).

Config (env didahulukan, lalu nine_router.json):
  NINE_ROUTER_BASE      default https://9router.mibp.me
  NINE_ROUTER_PROVIDER  default openai-compatible-chat-61aec30e-bfbb-4caa-a02c-af563aca8298
  NINE_ROUTER_PASSWORD  password dashboard (disarankan)
  NINE_ROUTER_API_KEY   opsional, dicoba dulu bila tanpa password (kemungkinan 401)

Cek tanpa efek samping:  python nine_router.py --check
Self-check offline:      python nine_router.py --selfcheck
"""
import json
import os
import sys
import time
from pathlib import Path

import requests

DEFAULT_BASE = "https://9router.mibp.me"
DEFAULT_PROVIDER = "openai-compatible-chat-61aec30e-bfbb-4caa-a02c-af563aca8298"
CONFIG_FILE = Path(__file__).parent / "nine_router.json"
TIMEOUT = 30


class NineRouterError(Exception):
    pass


def load_config():
    cfg = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text())
        except Exception as e:
            raise NineRouterError("nine_router.json rusak: %s" % e)
    if not isinstance(cfg, dict):
        raise NineRouterError("nine_router.json harus berisi object JSON")
    return {
        "base": os.environ.get("NINE_ROUTER_BASE", cfg.get("base", DEFAULT_BASE)).rstrip("/"),
        "provider": os.environ.get("NINE_ROUTER_PROVIDER", cfg.get("provider", DEFAULT_PROVIDER)),
        "password": os.environ.get("NINE_ROUTER_PASSWORD", cfg.get("password", "")),
        "api_key": os.environ.get("NINE_ROUTER_API_KEY", cfg.get("api_key", "")),
    }


def build_push_payload(provider, name, api_key):
    return {"provider": provider, "apiKey": api_key, "name": name}


def has_credentials(cfg):
    """True bila ada password dashboard atau api_key (sudah di-strip)."""
    return bool((cfg.get("password") or "").strip() or (cfg.get("api_key") or "").strip())


def parse_push_response(status_code, data):
    """-> (ok, info): info = connection id bila ok, pesan error bila gagal."""
    if status_code == 201:
        conn = (data or {}).get("connection", {}) if isinstance(data, dict) else {}
        return True, str(conn.get("id", "ok"))
    err = (data or {}).get("error", "http-%d" % status_code) if isinstance(data, dict) else "http-%d" % status_code
    return False, str(err)[:80]


class NineRouter:
    def __init__(self, cfg):
        self.cfg = cfg
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": "unik-farm/1.0",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        if cfg["api_key"] and not cfg["password"]:
            self.s.headers["Authorization"] = "Bearer " + cfg["api_key"]
        self._logged_in = False

    def login(self):
        if not self.cfg["password"]:
            raise NineRouterError("butuh NINE_ROUTER_PASSWORD untuk login dashboard")
        try:
            r = self.s.post("%s/api/auth/login" % self.cfg["base"],
                            json={"password": self.cfg["password"]}, timeout=TIMEOUT)
        except Exception as e:
            raise NineRouterError("login gagal koneksi: %s" % str(e)[:60])
        if r.status_code == 429:
            raise NineRouterError("login di-rate-limit (terlalu banyak percobaan)")
        if r.status_code == 403:
            raise NineRouterError("login ditolak (password wajib diganti / SSO aktif)")
        try:
            data = r.json()
        except Exception:
            raise NineRouterError("login respons tak terduga (http-%d)" % r.status_code)
        if r.status_code != 200 or not data.get("success"):
            raise NineRouterError("password salah / tidak sah (http-%d)" % r.status_code)
        self._logged_in = True

    def push_key(self, name, api_key, retries=3):
        """Push 1 key. -> (True, connection_id) / (False, alasan). Tak pernah me-log secret."""
        if not has_credentials(self.cfg):
            return False, "skip-tanpa-kredensial"
        if not api_key or not api_key.startswith("sk-"):
            return False, "api-key-tidak-valid"
        if self.cfg["password"] and not self._logged_in:
            try:
                self.login()
            except NineRouterError as e:
                return False, str(e)
        payload = build_push_payload(self.cfg["provider"], name, api_key)
        relogin_tried = False
        for attempt in range(retries):
            try:
                r = self.s.post("%s/api/providers" % self.cfg["base"],
                                json=payload, timeout=TIMEOUT)
            except Exception as e:
                if attempt < retries - 1:
                    time.sleep(3 * (attempt + 1))
                    continue
                return False, "koneksi: %s" % str(e)[:50]
            try:
                data = r.json()
            except Exception:
                data = {}
            if r.status_code == 401 and self.cfg["password"] and not relogin_tried:
                relogin_tried = True  # sesi kedaluwarsa -> login sekali lalu ulangi
                try:
                    self._logged_in = False
                    self.login()
                except NineRouterError as e:
                    return False, str(e)
                continue
            if r.status_code == 401 and not self.cfg["password"]:
                return False, "unauthorized: inference key tak berlaku, isi NINE_ROUTER_PASSWORD"
            if r.status_code in (429,) or r.status_code >= 500:
                if attempt < retries - 1:
                    time.sleep(5 * (attempt + 1))
                    continue
            return parse_push_response(r.status_code, data)
        return False, "gagal-setelah-retry"

    def check(self):
        """Verifikasi auth tanpa efek samping. -> (True, ringkasan) / raise."""
        if self.cfg["password"] and not self._logged_in:
            self.login()
        r = self.s.get("%s/api/providers" % self.cfg["base"], timeout=TIMEOUT)
        if r.status_code == 401:
            raise NineRouterError("unauthorized: login gagal / password salah")
        if r.status_code != 200:
            raise NineRouterError("check http-%d" % r.status_code)
        conns = r.json().get("connections", [])
        mine = [c for c in conns if c.get("provider") == self.cfg["provider"]]
        return True, "%d connections (%d di node ini)" % (len(conns), len(mine))


def demo():
    # ponytail: satu self-check offline untuk logika non-trivial (payload + parsing).
    p = build_push_payload("node-1", "9router-abc12345", "sk-test")
    assert p == {"provider": "node-1", "apiKey": "sk-test", "name": "9router-abc12345"}, p
    assert parse_push_response(201, {"connection": {"id": "c-1"}}) == (True, "c-1")
    ok, info = parse_push_response(404, {"error": "OpenAI Compatible node not found"})
    assert ok is False and "node not found" in info, (ok, info)
    ok, info = parse_push_response(400, {"error": "API Key is required"})
    assert ok is False, (ok, info)
    print("selfcheck OK")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        demo()
    elif "--check" in sys.argv:
        try:
            nr = NineRouter(load_config())
            _, summary = nr.check()
            print("9router OK: %s" % summary)
        except NineRouterError as e:
            print("[!] %s" % e)
            sys.exit(1)
    else:
        print("Usage: python nine_router.py [--check | --selfcheck]")
        sys.exit(1)
