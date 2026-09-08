"""
proxy_manager.py — Proxy rotation for multi_apikey.py.

Pool file: proxies.txt (next to this file). One proxy per line.
Lines starting with '#' are ignored.

Supported formats:
  host:port
  host:port:user:pass
  http://user:pass@host:port
  socks5://host:port

Each entry is parsed into a browser-ready dict:
  {"server": "http://host:port", "username": "u", "password": "p"}
plus a `url` key for requests-based calls.

Rotation policy (per-account):
  local -> proxy1 -> proxy2 -> ... -> proxyN -> local -> ...
  - Success on the current route: keep it.
  - Account failed after retries: advance to the next route.
"""
from pathlib import Path

PROXY_FILE = Path(__file__).parent / "proxies.txt"


def _parse(raw):
    """Parse one proxy line -> dict | None (comment/blank)."""
    s = (raw or "").strip()
    if not s or s.startswith("#"):
        return None
    scheme = "http"
    user = password = None
    host = port = None

    if "://" in s:
        scheme, rest = s.split("://", 1)
        scheme = (scheme or "http").lower()
        if "@" in rest:
            creds, hostport = rest.split("@", 1)
            if ":" in creds:
                user, password = creds.split(":", 1)
        else:
            hostport = rest
        if ":" in hostport:
            host, port = hostport.rsplit(":", 1)
    else:
        parts = s.split(":")
        if len(parts) == 2:
            host, port = parts
        elif len(parts) == 4:
            host, port, user, password = parts
        else:
            return None

    if not host or not port:
        return None
    if not host.replace(".", "").replace(":", "").isalnum() and "[" not in host:
        pass  # allow hostnames

    entry = {
        "server": "%s://%s:%s" % (scheme, host, port),
        "url": "%s://%s:%s" % (scheme, host, port),
    }
    if user is not None:
        entry["username"] = user
        entry["password"] = password or ""
        entry["url"] = "%s://%s:%s@%s:%s" % (scheme, user, password or "", host, port)
    return entry


def load_proxies(path=PROXY_FILE):
    """Return list of proxy dicts (empty if none/missing)."""
    if not Path(path).exists():
        return []
    out = []
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            e = _parse(line)
            if e:
                out.append(e)
    except OSError:
        pass
    return out


class ProxyManager:
    def __init__(self, proxies):
        self.sequence = [None] + list(proxies)  # None = local
        self.cursor = 0
        self.fail_count = 0
        self.success_count = 0

    def current(self):
        """Proxy entry dict, or None for local connection."""
        return self.sequence[self.cursor]

    def browser_proxy(self):
        """Dict for Camoufox new_context(proxy=...) — None when local."""
        cur = self.current()
        if cur is None:
            return None
        return {k: v for k, v in cur.items() if k != "url"}

    def requests_proxy(self):
        """URL string for requests Session.proxies, or None when local."""
        cur = self.current()
        if cur is None:
            return None
        # remove browser-only key
        return {k: v for k, v in cur.items() if k in ("url",)}

    def desc(self):
        """Human label without creds."""
        cur = self.current()
        if cur is None:
            return "local"
        return cur["server"].split("://")[-1][:44]

    def advance(self, reason=""):
        self.fail_count += 1
        self.cursor = (self.cursor + 1) % len(self.sequence)
        print("[proxy] fail (%s) -> switch to %s" % (reason[:40], self.desc()), flush=True)
        return self.desc()

    def confirm(self):
        self.success_count += 1

    def summary(self):
        return "fail=%d ok=%d current=%s" % (self.fail_count, self.success_count, self.desc())