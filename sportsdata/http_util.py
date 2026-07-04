"""Tiny resilient JSON HTTP helper (stdlib only — no extra deps)."""
import json, ssl, time, random, urllib.request, urllib.error

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"


def get_json(url, headers=None, timeout=25, retries=3, backoff=1.5):
    h = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        h.update(headers)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (404, 400):
                raise
            time.sleep(backoff * (attempt + 1) + random.random())
        except Exception as e:
            last = e
            time.sleep(backoff * (attempt + 1) + random.random())
    raise last


def get_text(url, headers=None, timeout=60, retries=3, backoff=2.0):
    """Fetch a URL as decoded text (handles UTF-8 BOM). For CSV endpoints."""
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                return r.read().decode("utf-8-sig", "replace")
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (404, 400):
                raise
            time.sleep(backoff * (attempt + 1) + random.random())
        except Exception as e:
            last = e
            time.sleep(backoff * (attempt + 1) + random.random())
    raise last


def to_int(v):
    try:
        if v in (None, "", "--", "-"):
            return None
        return int(float(str(v).replace("+", "")))
    except (ValueError, TypeError):
        return None


def to_num(v):
    try:
        if v in (None, "", "--", "-"):
            return None
        return float(str(v).replace("+", ""))
    except (ValueError, TypeError):
        return None


def split_made_att(v):
    """'6-11' -> (6, 11)."""
    if not v or "-" not in str(v):
        return None, None
    a, b = str(v).split("-", 1)
    return to_int(a), to_int(b)
