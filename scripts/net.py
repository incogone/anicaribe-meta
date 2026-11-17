"""HTTP and logging shared by every source."""
from __future__ import annotations

import http.client
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

USER_AGENT = "AniCaribe-meta/1.0 (+https://github.com/incogone/anicaribe-meta)"
BROWSER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

_log_lock = threading.Lock()

# GitHub's runners resolve some hosts to IPv6 addresses they cannot reach ("Network is unreachable"),
# so every connection is made over IPv4.
_getaddrinfo = socket.getaddrinfo


def _ipv4_only(host, port, family=0, *args, **kwargs):
    return _getaddrinfo(host, port, socket.AF_INET, *args, **kwargs)


socket.getaddrinfo = _ipv4_only


def log(*parts):
    with _log_lock:
        print(datetime.now(timezone.utc).strftime("%H:%M:%S"), *parts, flush=True)


class Gone(Exception):
    """The address answered 404 or 410: there is nothing there."""


def fetch(url, *, body=None, form=None, headers=None, kind="json", attempts=7, timeout=90, browser=False, retry_on=()):
    """One HTTP call with retries. Honours Retry-After and backs off on errors.

    kind is "json" (returns the parsed body and the response headers) or "text"
    (returns the decoded body and the headers). A 404 or 410 raises Gone and is not retried.
    retry_on adds status codes a site uses for "slow down" that are not the usual ones.
    """
    data = None
    sent = {"User-Agent": BROWSER_AGENT if browser else USER_AGENT,
            "Accept": "application/json" if kind == "json" else "text/html,application/xhtml+xml,*/*"}
    if body is not None:
        data = json.dumps(body).encode()
        sent["Content-Type"] = "application/json"
    elif form is not None:
        data = form.encode()
        sent["Content-Type"] = "application/x-www-form-urlencoded"
    sent.update(headers or {})
    delay = 5
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data, sent), timeout=timeout) as response:
                raw = response.read()
                if kind == "json":
                    return json.loads(raw), response.headers
                charset = response.headers.get_content_charset() or "utf-8"
                return raw.decode(charset, errors="replace"), response.headers
        except urllib.error.HTTPError as error:
            if error.code in (404, 410):
                raise Gone(url) from error
            if (error.code in (408, 425, 429, 500, 502, 503, 504, 520, 522, 524) or error.code in retry_on) and attempt < attempts:
                try:
                    wait = int(error.headers.get("Retry-After") or delay)
                except ValueError:
                    wait = delay
                log(f"  {error.code} from {url[:70]}; waiting {wait}s")
                time.sleep(min(wait, 300) + 1)
                delay = min(delay * 2, 120)
                continue
            raise
        except (urllib.error.URLError, http.client.HTTPException, OSError, json.JSONDecodeError) as error:
            if attempt < attempts:
                log(f"  {type(error).__name__} from {url[:70]}; waiting {delay}s")
                time.sleep(delay)
                delay = min(delay * 2, 120)
                continue
            raise
    raise RuntimeError("unreachable")
