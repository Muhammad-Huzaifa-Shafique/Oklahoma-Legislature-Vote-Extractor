#I did my project with my classmates. we divided our project into different part and my role was to extract house votes
#https://chatgpt.com/share/6920e611-14c0-8005-b040-4788b0ac5bdc
# -*- coding: utf-8 -*-
"""
OK House/Senate vote harvester (ASYNC).
Auto mode now asks to download **Senate** (oksenate.gov) or **House** (former.okhouse.gov/Legislation/ShowVotes.aspx)
and pulls *all* votes across the requested year ranges into the same 3 CSVs.
"""

from __future__ import annotations
import os, re, io, csv, html, gzip, time, pathlib, plistlib, random, asyncio
from urllib import request as urlrequest
from urllib.parse import urlparse, urljoin
from urllib.error import HTTPError, URLError
from concurrent.futures import ThreadPoolExecutor, as_completed
import ssl

def _make_ssl_context():
    # HTTPS certificates are checked by default. If a site's certificate cannot be
    # verified on your machine, set the environment variable OKVOTES_INSECURE_SSL=1
    # to skip the check for that run. A warning is printed when it is skipped.
    if os.environ.get("OKVOTES_INSECURE_SSL") == "1":
        print("WARNING: HTTPS certificate checking is OFF (OKVOTES_INSECURE_SSL=1).", flush=True)
        return ssl._create_unverified_context()
    return ssl.create_default_context()

_SSL_CONTEXT = _make_ssl_context()

def get_url(url, timeout=8.0, headers=None):
    from urllib import request as _req
    req = _req.Request(url, headers=(headers or {"User-Agent":"Mozilla/5.0"}))
    with _req.urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as r:
        return r.read()

# ---- Optional GUI picker (we also support console fallback) ----
try:
    import tkinter as tk
    from tkinter import messagebox, filedialog, simpledialog
    TK_OK = True
except Exception:  # headless
    TK_OK = False
# --- Make asyncio stable on Windows; harmless on other OSes
try:
    if os.name == "nt":
        import asyncio
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
except Exception:
    pass

# ---- aiohttp (optional but recommended) ----
try:
    import aiohttp
except Exception:
    aiohttp = None
# ---- PDF text extraction ----
try:
    from PyPDF2 import PdfReader
except Exception:
    PdfReader = None

VOTE_BOUNDARY = "§§VOTE_BOUNDARY§§"

_OK_VOTES_HOST_RE = re.compile(r"(?i)lsb\.state\.ok\.us")
_OK_VOTES_PATH_RE = re.compile(r"(?i)/votes/")
# --- tiny timestamped logger ---
def _ts():
    return time.strftime("%H:%M:%S")

def log(msg: str):
    # Always flush so you see streaming progress in real time
    print(f"[{_ts()}] {msg}", flush=True)


# ================================================================
# HTML preprocessor to insert hard boundaries (unchanged)
def _preprocess_ok_votes_html(page_html: str) -> str:
    marked = page_html
    marked = re.sub(r'(?is)<a[^>]+?(?:name|id)=["\'][^"\']*["\'][^>]*>', f"\n{VOTE_BOUNDARY}\n", marked)
    marked = re.sub(r'(?is)<a[^>]+?(?:name|id)=["\'][A-Za-z]{1,3}\s*[\-_]?\d{1,5}[^"\']*["\'][^>]*>', f"\n{VOTE_BOUNDARY}\n", marked)
    marked = re.sub(r"(?is)</table>\s*<hr[^>]*>\s*<table", f"</table>\n{VOTE_BOUNDARY}\n<table", marked)
    marked = re.sub(r"(?is)<hr[^>]*>", f"\n{VOTE_BOUNDARY}\n", marked)
    marked = re.sub(r"(?is)</(tr|p|div|li|h[1-6])>", r"</\1>\n", marked)
    marked = re.sub(r"(?is)<br\s*/?>", "\n", marked)
    return marked
# ================================================================

# ========== CSV Schemas ==========
HEADINGS = [
    "Unique Index", "Bill Number", "Type", "Yea", "Nay", "CP",
    "Excused", "Vacant", "Result", "Location", "Date", "Time",
]
HEADINGS_ROLL = [
    "Unique Index", "Votes Detail Index", "Bill Number", "Member",
    "Vote", "Type", "Result", "Location", "Date", "Time",
]
HEADINGS_MEMBERS = ["Unique Index", "Chamber", "District", "Member"]

# ========== Small helpers ==========
_URL_CACHE: dict[str, str] = {}

def _normalize_text(s: str) -> str:
    s = (s or "").replace("\u00a0", " ").replace("\u2007", " ").replace("\u202f", " ")
    s = s.replace("\u2014", "-").replace("\u2013", "-").replace("\u2212", "-")
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"(?i)\bayes?\b", "Aye", s)
    s = re.sub(r"(?i)\byeas?\b", "Yea", s)
    s = re.sub(r"(?i)\bnoes?\b", "Nay", s)
    s = re.sub(r"(?i)\bnayes?\b", "Nay", s)
    s = re.sub(r"(?i)\bexcused\b", "Excused", s)
    s = re.sub(r"(?i)\bconstitutional\s+priv(?:ilege)?\b", "Constitutional Privilege", s)
    s = re.sub(r"(?i)\bvacanc(?:y|ies)\b", "Vacancies", s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s

def is_url(s: str) -> bool:
    try:
        u = urlparse(s)
        return u.scheme in ("http", "https") and bool(u.netloc)
    except Exception:
        return False

def _http_get(url: str, timeout=60) -> bytes:
    headers = {
        "User-Agent": "Mozilla/5.0 (OKVotes/4.0)",
        # ask for compressed, then decode it ourselves
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }
    req = urlrequest.Request(url, headers=headers)
    with urlrequest.urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as resp:
        raw = resp.read()
        enc = (resp.headers.get("Content-Encoding") or "").lower()
        if enc == "gzip":
            import gzip, io
            raw = gzip.decompress(raw)
        elif enc == "deflate":
            import zlib
            try:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            except zlib.error:
                raw = zlib.decompress(raw)
        elif enc == "br":
            try:
                import brotli
                raw = brotli.decompress(raw)
            except Exception:
                pass
        return raw


def _detect_charset_from_headers_or_html(raw: bytes, default="utf-8") -> str:
    head = raw[:4096].decode("latin-1", errors="ignore").lower()
    m = re.search(r'charset=["\']?([a-z0-9_\-]+)', head)
    return m.group(1) if m else default

def _pdf_bytes_to_text(raw: bytes) -> str:
    if PdfReader is None:
        return ""
    try:
        buf = io.BytesIO(raw)
        reader = PdfReader(buf)
        parts = []
        for page in reader.pages:
            try:
                t = page.extract_text() or ""
            except Exception:
                t = ""
            if t:
                parts.append(t)
        return _normalize_text("\n".join(parts))
    except Exception:
        return ""

def _looks_like_pdf(raw: bytes) -> bool:
    return raw[:5] == b"%PDF-"

def _same_measure(a: str, b: str) -> bool:
    if not b: return True
    return (a or "").strip().upper() == b.strip().upper()

def read_url_text(url: str) -> str:
    if url in _URL_CACHE:
        return _URL_CACHE[url]
    raw = _http_get(url)
    if _looks_like_pdf(raw):
        text = _pdf_bytes_to_text(raw); _URL_CACHE[url] = text; return text
    page = raw.decode(_detect_charset_from_headers_or_html(raw), errors="ignore")
    try:
        u = urlparse(url)
        if _OK_VOTES_HOST_RE.search(u.netloc) and _OK_VOTES_PATH_RE.search(u.path):
            page = _preprocess_ok_votes_html(page)
    except Exception:
        pass
    page = html.unescape(page)
    page = re.sub(r"(?i)<br\s*/?>", "\n", page)
    page = re.sub(r"(?i)</(p|div|li|tr|h[1-6])>", "\n", page)
    page = re.sub(r"<script.*?</script>", " ", page, flags=re.DOTALL | re.IGNORECASE)
    page = re.sub(r"<style.*?</style>", " ", page, flags=re.DOTALL | re.IGNORECASE)
    text = _normalize_text(re.sub(r"<[^>]+>", " ", page))
    _URL_CACHE[url] = text
    return text
# Async fetch helper (works with aiohttp; falls back to thread if aiohttp is None)
async def read_url_text_async(session, url: str, timeout_total: float = 20.0) -> str:
    if aiohttp is not None and session is not None:
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return ""
                raw = await resp.read()
        except Exception:
            return ""
        # PDF?
        if _looks_like_pdf(raw):
            return _pdf_bytes_to_text(raw)
        page = raw.decode(_detect_charset_from_headers_or_html(raw), errors="ignore")
        try:
            u = urlparse(url)
            if _OK_VOTES_HOST_RE.search(u.netloc) and _OK_VOTES_PATH_RE.search(u.path):
                page = _preprocess_ok_votes_html(page)
        except Exception:
            pass
        page = html.unescape(page)
        page = re.sub(r"(?i)<br\s*/?>", "\n", page)
        page = re.sub(r"(?i)</(p|div|li|tr|h[1-6])>", "\n", page)
        page = re.sub(r"<script.*?</script>", " ", page, flags=re.DOTALL | re.IGNORECASE)
        page = re.sub(r"<style.*?</style>", " ", page, flags=re.DOTALL | re.IGNORECASE)
        return _normalize_text(re.sub(r"<[^>]+>", " ", page))

    # Fallback: run the sync version in a thread so callers can still await it
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, read_url_text, url)

# ===== Name extraction (unchanged) =====
_PREFIX_JOIN = {"Mc", "Mac", "O"}
_TWO_WORD_SURNAME = {"De", "La", "Le", "Van", "Von", "St", "St."}
_NOISE_UPPER = {"NOT", "VOTING"}

def _split_dense_lastnames(tb: str):
    tokens = re.findall(r"[A-Z][A-Za-z'.\-]*|[A-Z]{2,}", tb)
    out = []; i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _NOISE_UPPER:
            i += 1; continue
        if tok in _PREFIX_JOIN and i + 1 < len(tokens):
            nxt = tokens[i+1]
            if re.match(r"[A-Z][A-Za-z'.\-]+", nxt):
                tok = ("O'" + nxt) if tok == "O" else (tok + nxt)
                i += 1
        elif tok in _TWO_WORD_SURNAME and i + 1 < len(tokens):
            nxt = tokens[i+1]
            if re.match(r"[A-Z][A-Za-z'.\-]+", nxt):
                tok = tok + " " + nxt; i += 1
        n = _clean_name(tok)
        if n: out.append(n)
        i += 1
    return out

NAME_TOKEN_RE = re.compile(r"^(?:[A-Z][A-Za-z'.\-]+|[A-Z]{2,})(?:\s+(?:[A-Z]\.?|[A-Z][A-Za-z'.\-]+|[A-Z]{2,}))*?(?:\s+\([A-Za-z]{1,3}\))?$")
NAME_SPLIT_RE = re.compile(r"\s*(?:,|;|\band\b|\u2022|\||—|–|-)\s*", re.IGNORECASE)
NAME_FINDER_RE = re.compile(r"(?:[A-Z][A-Za-z'.\-]+|[A-Z]{2,})(?:\s+(?:[A-Z]\.?|[A-Z][A-Za-z'.\-]+|[A-Z]{2,}))*?(?:\s+\([A-Za-z]{1,3}\))?")

def _strip_titles_and_noise(t: str) -> str:
    t = re.sub(r"^(?:Rep\.?|Representative|Sen\.?|Senator|Mr\.?|Mrs\.?|Ms\.?|Dr\.?)\s+", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\bMr\.?\s*Speaker\b", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\bSpeaker\b", "", t, flags=re.IGNORECASE)
    t = re.sub(r"(?i)^\s*the\s+following\s+members?.*?[:\-]\s*", "", t)
    t = re.sub(r"(?i)^\s*the\s+member\s+was\s+excused.*?$", "", t)
    t = re.sub(r"\s*(?:Jr\.?|Sr\.?|III|IV)\.?$", "", t).strip()
    t = re.sub(r"[—–-]\s*\d+\.?$", "", t)
    t = re.sub(r"^(Yeas?|Ayes?|Nays?|Noes?|Excused|Vacancies?|CP|Constitutional Privilege)\W+", "", t, flags=re.IGNORECASE)
    return t.strip(" .,")

def _clean_name(token: str) -> str:
    t = _strip_titles_and_noise(token.strip())
    if re.search(r"\d", t): return ""
    if t.lower() in {"", "none", "present", "the", "member", "members", "vacancy", "excused"}: return ""
    if t.lower() in {"nay","nays","no","yea","yeas","aye","ayes"}: return ""
    t = re.sub(r"\s*\([^)]+\)\s*$", "", t)
    return t if NAME_TOKEN_RE.match(t) else ""

def _column_split_names(text_block: str):
    out = []
    for line in (text_block or "").splitlines():
        line = line.strip()
        if not line: continue
        if re.search(r"(?i)\bthe\s+following\s+members?\b", line) or re.search(r"(?i)\b(was|were|is|are|moved|adopted|ordered)\b", line):
            continue
        cols = re.split(r"(?:\s{2,}|\t+)", line)
        for col in cols:
            col = col.strip()
            if not col: continue
            n = _clean_name(col)
            if not n: continue
            if " " in n and not re.search(r",|;| and |\u2022|\||—|–|-", n) and re.fullmatch(r"(?:[A-Z][a-z'.\-]+(?:\s+|$))+", n):
                out.extend(_split_dense_lastnames(n))
            else:
                out.append(n)
    return out

def extract_names(text_block: str):
    if not text_block: return []
    tb = text_block.strip()
    col_names = _column_split_names(tb)
    collapsed = re.sub(r"\s+", " ", tb)
    punct_names = []
    for p in NAME_SPLIT_RE.split(collapsed):
        n = _clean_name(p)
        if n: punct_names.append(n)
    if len(col_names) > 1:
        base = col_names
    elif len(punct_names) > 1:
        base = punct_names
    else:
        dense = _split_dense_lastnames(tb)
        base = dense if dense else []
    if not base:
        seen, out = set(), []
        for tok in re.split(r"\s+", tb):
            n = _clean_name(tok)
            if n and n not in seen:
                seen.add(n); out.append(n)
        return out
    return explode_concatenated_names(base := base)

def explode_concatenated_names(names_list: list[str]) -> list[str]:
    out = []
    for item in names_list or []:
        if item and re.match(r"^[A-Z][A-Za-z'.\-]*(?:\s+[A-Z][A-Za-z'.\-]*){1,}$", item):
            split = _split_dense_lastnames(item)
            out.extend(split if split else [item])
        else:
            out.append(item)
    seen, uniq = set(), []
    for n in out:
        if n not in seen:
            seen.add(n); uniq.append(n)
    return uniq

# ===== Session/Location helpers =====
def parse_session_info(text: str) -> tuple[str, str, str]:
    date_str = ""
    m = re.search(r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*,?\s*([A-Za-z]+)\s+(\d{1,2})\s*,?\s*(\d{4})", text)
    if m:
        mm = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,"jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}.get(m.group(2).lower()[:3], 0)
        if mm: date_str = f"{mm}/{int(m.group(3))}/{m.group(4)}"
    if not date_str:
        md = re.search(r"\b(\d{1,2})[\/\-.](\d{1,2})[\/\-.](\d{2,4})\b", text)
        if md:
            mm, dd, yy = int(md.group(1)), int(md.group(2)), md.group(3)
            if len(yy) == 2: yy = "20" + yy
            date_str = f"{mm}/{dd}/{yy}"
    mt = re.search(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?|AM|PM)?", text)
    time_fmt = ""
    if mt:
        h, mi, ampm = int(mt.group(1)), int(mt.group(2)), mt.group(4)
        if ampm:
            a = ampm.lower()
            if a.startswith("p") and h != 12: h += 12
            if a.startswith("a") and h == 12: h = 0
        time_fmt = f"{h}:{mi:02d}"
    if re.search(r"\bSenate Journal\b|\bsenate\b", text, re.IGNORECASE):
        doc_location = "Senate Floor"
    elif re.search(r"\bHouse Journal\b|\bhouse\b", text, re.IGNORECASE):
        doc_location = "House Floor"
    else:
        doc_location = ""
    return date_str, doc_location, time_fmt

# Stage/venue for Type (readings or committee names only)
_COMMITTEE_PATS = [
    r"((?:Joint\s+)?(?:Standing\s+|Select\s+|Special\s+)?Committee\s+(?:on|of)\s+[A-Z][A-Za-z &.\-]+)",
    r"(COMMITTEE\s+(?:ON|OF)\s+[A-Z][A-Z &.\-]+)",
    r"([A-Z][A-Za-z &.\-]+ Committee\s+(?:on|of)\s+[A-Z][A-Za-z &.\-]+)",
]
def _titlecase_committee(s: str) -> str:
    s = s.strip()
    if s.isupper():
        s = s.title().replace(" On ", " on ").replace(" Of ", " of ")
    return s

TYPE_READINGS = (
    ("Fourth Reading", re.compile(r"(?i)\bfourth\s+reading\b")),
    ("Third Reading",  re.compile(r"(?i)\bthird\s+reading\b")),
    ("General Order",  re.compile(r"(?i)\bgeneral\s+order\b")),
)
_COMMITTEE_EXTRA_PATS = [
    r"((?:Joint\s+)?(?:Standing\s+|Select\s+|Special\s+)?Committee\s+(?:on|of)\s+[A-Z][A-Za-z &.\-]+)",
    r"(COMMITTEE\s+(?:ON|OF)\s+[A-Z][A-Z &.\-]+)",
    r"([A-Z][A-Za-z &.\-]+ Committee\s+(?:on|of)\s+[A-Z][A-Za-z &.\-]+)",
    r"([A-Z][A-Za-z &.\-]+)\s+Committee\b(?!\s+Vote)",
]
def committee_from_text(ctx: str) -> str:
    for pat in _COMMITTEE_EXTRA_PATS:
        m = re.search(pat, ctx, re.IGNORECASE)
        if m:
            name = m.group(1).strip()
            if name.lower().endswith(" committee") and " on " not in name.lower():
                name = "Committee on " + name[:-10].strip()
            return _titlecase_committee(name)
    return ""

def detect_type_in_context(ctx: str) -> str:
    if not ctx: return ""
    for label, pat in TYPE_READINGS:
        if pat.search(ctx): return label
    comm = committee_from_text(ctx)
    if comm: return comm
    return ""

def _normalize_floor(line: str) -> str:
    if re.search(r"\bSenate\b", line, re.IGNORECASE) and re.search(r"\b(Vote|Voting|Floor|Chamber|Reading)\b", line, re.IGNORECASE):
        return "Senate Floor"
    if re.search(r"\bHouse\b", line, re.IGNORECASE) and re.search(r"\b(Vote|Voting|Floor|Chamber|Reading)\b", line, re.IGNORECASE):
        return "House Floor"
    if re.search(r"\bHouse Journal\b", line, re.IGNORECASE): return "House Floor"
    if re.search(r"\bSenate Journal\b", line, re.IGNORECASE): return "Senate Floor"
    return ""

def _find_location_around(lines, idx, fallback=""):
    n = len(lines); lo = max(0, idx - 100); hi = min(n, idx + 100)
    for j in range(idx, lo - 1, -1):
        line = lines[j].strip()
        for pat in _COMMITTEE_PATS:
            m = re.search(pat, line, re.IGNORECASE)
            if m:
                loc = m.group(1).strip()
                if re.search(r"(?i)\bcommittee\s+vote\b", loc): continue
                return _titlecase_committee(loc)
    for j in range(idx, hi):
        line = lines[j].strip()
        for pat in _COMMITTEE_PATS:
            m = re.search(pat, line, re.IGNORECASE)
            if m:
                loc = m.group(1).strip()
                if re.search(r"(?i)\bcommittee\s+vote\b", loc): continue
                return _titlecase_committee(loc)
    for j in range(idx, lo - 1, -1):
        norm = _normalize_floor(lines[j])
        if norm: return norm
    for j in range(idx, hi):
        norm = _normalize_floor(lines[j])
        if norm: return norm
    return fallback

def _find_dt_loc_around(lines, idx, win=120):
    date_str, time_fmt, loc = "", "", ""
    lo = max(0, idx - win); hi = min(len(lines), idx + win)
    for j in range(lo, hi):
        l = lines[j].strip()
        m = re.search(r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*,?\s*([A-Za-z]+)\s+(\d{1,2})\s*,?\s*(\d{4})", l, re.I)
        if m and not date_str:
            mm = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,"jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}.get(m.group(2).lower()[:3], 0)
            if mm: date_str = f"{mm}/{int(m.group(3))}/{m.group(4)}"
        if not date_str:
            m = re.search(r"\b(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{2,4})\b", l)
            if m:
                mm, dd, yy = int(m.group(1)), int(m.group(2)), m.group(3)
                if len(yy)==2: yy = "20"+yy
                date_str = f"{mm}/{dd}/{yy}"
    for j in range(lo, hi):
        l = lines[j].strip()
        m = re.search(r"\b(\d{1,2}):(\d{2})(?::\d{2})?\s*(A\.?M\.?|P\.?M\.?|AM|PM)\b", l, re.I)
        if m:
            h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3).lower()
            if ap.startswith("p") and h != 12: h += 12
            if ap.startswith("a") and h == 12: h = 0
            time_fmt = f"{h}:{mi:02d}"; break
    loc = _find_location_around(lines, idx, fallback="")
    return date_str, time_fmt, loc

# --- Bill matchers ---
DASH = r"(?:\u2014|\u2013|\u2212|-)"
HYPHENS = r"\u2014\u2013\u2012\u2011\-"
_BILL_RE = re.compile(r"\b((?:H(?:B|JR|CR)|S(?:B|JR|CR)))[\s\-]*?(\d{1,5})\b", re.IGNORECASE)
_BILL_FORMS = [
    (re.compile(r"\bSENATE\s+BILL\s+(\d{1,5})\b", re.IGNORECASE), "SB"),
    (re.compile(r"\bHOUSE\s+BILL\s+(\d{1,5})\b", re.IGNORECASE), "HB"),
    (re.compile(r"\bSENATE\s+JOINT\s+RESOLUTION\s+(\d{1,5})\b", re.IGNORECASE), "SJR"),
    (re.compile(r"\bHOUSE\s+JOINT\s+RESOLUTION\s+(\d{1,5})\b", re.IGNORECASE), "HJR"),
    (re.compile(r"\bSENATE\s+CONCURRENT\s+RESOLUTION\s+(\d{1,5})\b", re.IGNORECASE), "SCR"),
    (re.compile(r"\bHOUSE\s+CONCURRENT\s+RESOLUTION\s+(\d{1,5})\b", re.IGNORECASE), "HCR"),
]
def _find_bill_in_line(l: str) -> str | None:
    m = _BILL_RE.search(l)
    if m: return f"{m.group(1).upper()} {m.group(2)}"
    for pat, abbr in _BILL_FORMS:
        m2 = pat.search(l)
        if m2: return f"{abbr} {m2.group(1)}"
    return None

# --- Section parsing helpers ---
_INLINE_SECTION_RE = re.compile(
    r"(?i)\b(AYES?|YEAS?|NAYS?|NOES?|NO|EXC(?:USED)?|C/?P|CONSTITUTIONAL\s+PRIV(?:ILEGE)?|VACANC(?:Y|IES))\s*[:\-]\s*(\d+)\s*(.*?)\s*(?=(AYES?|YEAS?|NAYS?|NOES?|NO|EXC(?:USED)?|C/?P|CONSTITUTIONAL\s+PRIV(?:ILEGE)?|VACANC(?:Y|IES))\s*[:\-]|$)"
)
_LABEL_ONLY_RE = re.compile(r"(?i)^(AYES?|YEAS?|NAYS?|NOES?|NO|EXC(?:USED)?|C/?P|CONSTITUTIONAL\s+PRIV(?:ILEGE)?|VACANC(?:Y|IES))\s*[:\-]\s*(\d+)\.?\s*$")
_VOTE_PHRASE_RE = {
    "yea": re.compile(r"(?i)\b(aye?s?|yeas?|those\s+vot(?:ing|ed)\s+aye|vot(?:ing|ed)\s+aye)\b"),
    "nay": re.compile(r"(?i)\b(nays?|noes?|no|those\s+vot(?:ing|ed)\s+nay|vot(?:ing|ed)\s+nay)\b"),
    "excused": re.compile(r"(?i)\b(excused)\b"),
    "vacant": re.compile(r"(?i)\b(vacanc(?:y|ies)|vacant)\b"),
    "cp": re.compile(r"(?i)\b(c/p|constitutional\s+privilege)\b"),
}

def _label_to_key(label: str) -> str:
    l = label.lower()
    if l.startswith(("yea","aye")): return "yea"
    if l.startswith(("nay","noe","no")): return "nay"
    if l.startswith("exc"): return "excused"
    if l.startswith(("c/p","constitutional")): return "cp"
    if l.startswith("vacanc"): return "vacant"
    return ""

def _has_counts_or_names(rec) -> bool:
    if not rec: return False
    if any(rec.get(k, 0) for k in ("yea","nay","excused","vacant","cp")): return True
    return any(rec["names"].get(b) for b in ("yea","nay","excused","vacant","cp"))

def _block_context(lines, start_i, end_i, banner):
    lo = max(0, start_i - 25); hi = min(len(lines), end_i + 26)
    snippet = "\n".join(lines[lo:hi])
    date_str, doc_location, time_fmt = parse_session_info(snippet)
    loc = _find_location_around(lines, start_i, fallback=banner) or doc_location
    return date_str, time_fmt, loc

def _clean_loc(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"(?i)\bcommittee\s+vote\b.*$", "", s).strip()
    return _titlecase_committee(s) if s else s

DEBUG_RECORD_PARSE = False

def _finalize_record(current, records, lines, banner):
    for b in ("yea","nay","excused","vacant","cp"):
        current["names"][b] = explode_concatenated_names(current["names"].get(b, []))
    if not _has_counts_or_names(current): return
    if not current.get("type"): current["type"] = "Third Reading"
    # fill counts from names if missing
    for k in ("yea","nay","cp","excused","vacant"):
        if (current.get(k) or 0) == 0 and current["names"].get(k):
            current[k] = len(current["names"][k])
    try:
        d, t, loc = _find_dt_loc_around(lines, current.get("_start", 0), win=160)
    except Exception:
        d, t, loc = "", "", ""
    if not (d or t or loc):
        try:
            d2, t2, loc2 = _block_context(lines, current.get("_start", 0), current.get("_end", 0), banner)
            if d2 and not d: d = d2
            if t2 and not t: t = t2
            if loc2 and not loc: loc = loc2
        except Exception:
            pass
    if d: current["date"] = d
    if t: current["time"] = t
    if loc: current["location"] = _clean_loc(loc)
    records.append(current)

def parse_votes_from_page(text: str, url: str = "") -> list[dict]:
    out_records = []
    chunks = [c.strip() for c in text.split(VOTE_BOUNDARY) if c.strip()]
    last_bill_seen = ""
    for chunk in chunks:
        lines = chunk.splitlines()
        page_bill = infer_page_bill(chunk, url) if url else infer_page_bill(chunk)
        parsed = parse_third_reading_blocks(chunk)
        if not parsed:
            bill = page_bill or last_bill_seen or ""
            vtype = detect_type_in_context(chunk)
            date_str, doc_location, time_fmt = parse_session_info(chunk)
            if bill or vtype or re.search(r"\b(aye?s?|nays?|noes?|excused)\b", chunk, re.I):
                rec = {
                    "bill": bill, "type": vtype or "",
                    "yea": 0, "nay": 0, "cp": 0, "excused": 0, "vacant": 0,
                    "result": None,
                    "names": {"yea": [], "nay": [], "excused": [], "vacant": [], "cp": []},
                    "location": doc_location or "", "date": date_str or "", "time": time_fmt or "",
                }
                out_records.append(rec)
            continue
        for rec in parsed:
            if not rec.get("bill"):
                rec["bill"] = page_bill or last_bill_seen or rec.get("bill","")
            if rec.get("bill"): last_bill_seen = rec["bill"]
            if not rec.get("type"): rec["type"] = detect_type_in_context(chunk)
            if not rec.get("date") or not rec.get("time"):
                d, tm, loc = parse_session_info(chunk)
                if d and not rec.get("date"): rec["date"] = d
                if tm and not rec.get("time"): rec["time"] = tm
                if loc and not rec.get("location"): rec["location"] = loc
            out_records.append(rec)
    return out_records

def parse_third_reading_blocks(text: str):
    lines = text.splitlines()
    last_bill_seen = ""
    def new_record(bill, loc, dt="", tm="", vtype=""):
        return {
            "bill": bill, "type": vtype,
            "yea": 0, "nay": 0, "excused": 0, "vacant": 0, "cp": 0,
            "result": None,
            "names": {"yea": [], "nay": [], "excused": [], "vacant": [], "cp": []},
            "location": loc or "", "date": dt or "", "time": tm or "",
        }
    records, section_buffer = [], []
    current, section, banner = None, None, ""
    for i, raw in enumerate(lines):
        l = raw.strip()
        if not l: continue
        if l == VOTE_BOUNDARY:
            if current is not None:
                if section and section_buffer:
                    current["names"][section] = extract_names("\n".join(section_buffer))
                current["_end"] = i - 1
                _finalize_record(current, records, lines, banner)
            current, section, section_buffer = None, None, []; continue
        bf = _normalize_floor(l)
        if bf: banner = bf
        else:
            for pat in _COMMITTEE_PATS:
                m = re.search(pat, l, re.I)
                if m:
                    banner = m.group(1).strip(); break
        bill_here = _find_bill_in_line(l)
        lo = max(0, i - 3); hi = min(len(lines), i + 4)
        ctx = "\n".join(lines[lo:hi])
        type_here = detect_type_in_context(ctx)
        if bill_here: last_bill_seen = bill_here
        if type_here or bill_here:
            if current:
                if section and section_buffer:
                    current["names"][section] = extract_names("\n".join(section_buffer))
                current["_end"] = i - 1
                _finalize_record(current, records, lines, banner)
                current, section, section_buffer = None, None, []
            dt, tm, loc = _find_dt_loc_around(lines, i, win=100)
            bill_for_record = bill_here or last_bill_seen or ""
            if not loc and (type_here or "").lower().endswith("reading"):
                loc = "Senate Floor" if bill_for_record.upper().startswith(("S", "SB", "SJR", "SCR", "SR")) else "House Floor"
            current = new_record(bill_for_record, loc, dt, tm, type_here)
            current["_start"] = i
        # counts
        for pat, key in (
            (r"(?i)\b(aye?s?|yeas?)\s*(?:[:\-]|\s)\s*(\d+)", "yea"),
            (r"(?i)\b(nays?|noes?|no)\s*(?:[:\-]|\s)\s*(\d+)", "nay"),
            (r"(?i)\b(exc(?:used)?)\s*(?:[:\-]|\s)\s*(\d+)", "excused"),
            (r"(?i)\b(c/?p|constitutional\s+priv(?:ilege)?)\s*(?:[:\-]|\s)\s*(\d+)", "cp"),
            (r"(?i)\b(vacanc(?:y|ies))\s*(?:[:\-]|\s)\s*(\d+)", "vacant"),
        ):
            mhead = re.search(pat, l)
            if mhead and current:
                try: current[key] = int(mhead.group(2))
                except Exception: pass
        # inline sections
        last_inline_key = None
        for msec in _INLINE_SECTION_RE.finditer(l):
            lab, cnt, tail = msec.group(1), msec.group(2), msec.group(3) or ""
            key = _label_to_key(lab)
            if not key: continue
            if current is None:
                dt, tm, loc = _find_dt_loc_around(lines, i, win=28)
                current = new_record("", loc or banner, dt, tm, "")
                current["_start"] = i
            try: current[key] = int(cnt)
            except Exception: pass
            names_inline = extract_names(tail)
            if names_inline:
                existing = set(current["names"].get(key, []))
                for nm in names_inline:
                    if nm not in existing:
                        current["names"][key].append(nm); existing.add(nm)
            last_inline_key = key
        if last_inline_key:
            if section and section_buffer:
                current["names"][section] = extract_names("\n".join(section_buffer))
            section, section_buffer = last_inline_key, []; continue
        m_only = _LABEL_ONLY_RE.match(l)
        if m_only:
            key = _label_to_key(m_only.group(1))
            if section and section_buffer:
                current["names"][section] = extract_names("\n".join(section_buffer))
            section, section_buffer = key, []; continue
        for key, pat in _VOTE_PHRASE_RE.items():
            if pat.search(l):
                if section and section_buffer and current:
                    current["names"][section] = extract_names("\n".join(section_buffer))
                if current is None:
                    dt, tm, loc = _find_dt_loc_around(lines, i, win=28)
                    current = new_record("", loc or banner, dt, tm, "")
                    current["_start"] = i
                section, section_buffer = key, []; break
        if current is None:  # nothing to collect yet
            continue
        if section:
            if not re.match(r"^(Yeas?|Ayes?|Nays?|Noes?|Excused|Vacant|Vacancies|CP|Constitutional Privilege)\W*$", l, flags=re.IGNORECASE):
                section_buffer.append(raw)
            mtrail = re.search(rf"{DASH}{{1,2}}\s*(\d+)\.?\s*$", l) or re.search(rf"[{HYPHENS}]{{1,2}}\s*(\d+)\s*\.?$", l)
            if mtrail:
                try: current[section] = int(mtrail.group(1))
                except Exception: pass
                if section_buffer:
                    current["names"][section] = extract_names("\n".join(section_buffer))
                section, section_buffer = None, []
        if re.search(r"(?i)\b(passed|prevailed|adopted|do\s+pass)\b", l): current["result"] = "Pass"
        elif re.search(r"(?i)\b(failed|do\s+not\s+pass|did\s+not\s+pass|not\s+adopted)\b", l): current["result"] = "Fail"
    if current is not None:
        if section and section_buffer:
            current["names"][section] = extract_names("\n".join(section_buffer))
        current["_end"] = len(lines) - 1
        _finalize_record(current, records, lines, banner)
    return [r for r in records if _has_counts_or_names(r)]

# ===== Member helpers / roll-up =====
def derive_chamber(location: str) -> str:
    ll = (location or "").lower()
    if "house" in ll: return "House"
    if "senate" in ll: return "Senate"
    return ""

def collect_unique_members(records):
    seen, ordered = set(), []
    for rec in records:
        for k in ("yea", "nay", "cp", "excused", "vacant"):
            for nm in rec["names"].get(k, []):
                if nm not in seen:
                    seen.add(nm); ordered.append(nm)
    return ordered

def build_member_district_index(full_text: str):
    t = re.sub(r"\s+", " ", full_text)
    dist_pat = re.compile(r"\b(?:(?:House|Senate)\s*Dist(?:rict)?\.?\s*(\d{1,3})|(?:HD|SD)\s*(\d{1,3}))\b", re.IGNORECASE)
    idx: dict[str, str] = {}
    last_name_map: dict[str, set[str]] = {}
    WIN = 120
    for m in dist_pat.finditer(t):
        d = m.group(1) or m.group(2)
        if not d: continue
        start = max(0, m.start() - WIN); end = min(len(t), m.end() + WIN)
        window = t[start:end]
        for nm in NAME_FINDER_RE.findall(window):
            nm = _clean_name(nm.strip())
            if not nm: continue
            idx[nm] = d
            last = nm.split()[-1]
            last_name_map.setdefault(last, set()).add(d)
    return idx, last_name_map

def infer_member_district(name: str, full_text: str, exact_idx, last_idx) -> str:
    if name in exact_idx: return exact_idx[name]
    last = name.split()[-1]
    ds = last_idx.get(last)
    if ds and len(ds) == 1: return next(iter(ds))
    return ""

# ===== Table builders & CSV =====
def table_votes_detail(all_records):
    rows = []
    for rec in all_records:
        rows.append([
            rec["idx"], rec["bill"], rec.get("type") or "Vote",
            rec["yea"], rec["nay"], rec["cp"], rec["excused"], rec["vacant"],
            rec.get("result") or "", rec.get("location", ""), rec.get("date", ""), rec.get("time", ""),
        ])
    return rows

def table_vote_rollcall(all_records):
    rows = []; unique_idx = 1
    for rec in all_records:
        detail_idx = rec["idx"]; bill = rec["bill"]; vtype = rec.get("type") or "Vote"
        loc = rec.get("location", ""); date_str = rec.get("date", ""); time_fmt = rec.get("time", "")
        for bucket, vote_label in (("yea","Yea"), ("nay","Nay"), ("excused","Excused"), ("cp","CP")):
            for name in rec.get("names", {}).get(bucket, []):
                rows.append([unique_idx, detail_idx, bill, name, vote_label, vtype, rec.get("result",""), loc, date_str, time_fmt])
                unique_idx += 1
    return rows

from collections import defaultdict
def table_members(all_records, combined_text):
    exact_idx, last_idx = build_member_district_index(combined_text)
    seen = set(); ordered = []; name_to_chambers = defaultdict(set)
    for rec in all_records:
        chamber = derive_chamber(rec["location"])
        for k in ("yea", "nay", "excused", "cp"):
            for nm in rec["names"].get(k, []):
                if nm not in seen:
                    seen.add(nm); ordered.append(nm)
                if chamber: name_to_chambers[nm].add(chamber)
    rows = []
    for i, name in enumerate(ordered, 1):
        chs = name_to_chambers.get(name, set())
        if len(chs) == 1: chamber_val = next(iter(chs))
        elif "House" in chs: chamber_val = "House"
        elif "Senate" in chs: chamber_val = "Senate"
        else: chamber_val = ""
        district_val = infer_member_district(name, combined_text, exact_idx, last_idx)
        rows.append([i, chamber_val, district_val, name])
    return rows

def _safe_path(basepath):
    path = pathlib.Path(basepath); parent, stem, suffix = path.parent, path.stem, path.suffix
    i, out = 1, path
    while True:
        try:
            fh = open(out, "x"); fh.close(); os.remove(out); return str(out)
        except (FileExistsError, PermissionError):
            out = parent / f"{stem} ({i}){suffix}"; i += 1

def save_table(headers, rows, filename):
    downloads = pathlib.Path.home() / "Downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    csv_path = _safe_path(downloads / filename)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f); writer.writerow(headers); writer.writerows(rows)
    print(f"CSV saved: {csv_path}")

async def collect_house_legislation_urls_async(year_start=1999, year_end=2025, max_concurrency=32) -> list[str]:
    """Discover all House PrintVote links by year, following pagination and per-measure pages."""
    base_fmt = "https://former.okhouse.gov/Legislation/ShowVotes.aspx?Year={y}"

    log(f"House index crawl: years {year_start}..{year_end}")

    # ---------- sync fallback ----------
    if aiohttp is None:
        found: list[str] = []
        for y in range(year_start, year_end + 1):
            index_url = base_fmt.format(y=y)
            log(f"  Year {y}: {index_url}")
            try:
                html_text = read_url_text(index_url)  # this now handles gzip/deflate/br
            except Exception as e:
                log(f"    fetch failed: {e}")
                continue

            before = len(found)
            found.extend(_extract_house_vote_links(index_url, html_text))
            pages, measures = _extract_house_subpages(index_url, html_text)
            log(f"    +{len(found)-before} links on index; next pages={len(pages)}, measures={len(measures)}")

            for sub in pages + measures:
                try:
                    sub_html = read_url_text(sub)
                except Exception:
                    continue
                before = len(found)
                found.extend(_extract_house_vote_links(sub, sub_html))
                added = len(found) - before
                if added:
                    log(f"      +{added} links from {sub}")

        return _dedupe(found)

    # ---------- async path ----------
    headers = {"User-Agent": "Mozilla/5.0 (OKHouseVotes/4.0)", "Accept": "text/html,application/xhtml+xml,*/*;q=0.1"}
    timeout = aiohttp.ClientTimeout(total=20, connect=5)
    conn = aiohttp.TCPConnector(limit_per_host=max_concurrency, ttl_dns_cache=300)
    sem = asyncio.Semaphore(max_concurrency)

    found: list[str] = []

    async with aiohttp.ClientSession(headers=headers, timeout=timeout, connector=conn) as session:
        async def fetch(url: str) -> str:
            async with sem:
                try:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            return ""
                        raw = await resp.read()
                        return raw.decode(_detect_charset_from_headers_or_html(raw), errors="ignore")
                except Exception:
                    return ""

        for y in range(year_start, year_end + 1):
            index_url = base_fmt.format(y=y)
            log(f"  Year {y}: {index_url}")
            html0 = await fetch(index_url)
            if not html0:
                log("    empty response")
                continue

            before = len(found)
            found.extend(_extract_house_vote_links(index_url, html0))
            pages, measures = _extract_house_subpages(index_url, html0)
            log(f"    +{len(found)-before} links on index; next pages={len(pages)}, measures={len(measures)}")

            to_visit = pages + measures
            if to_visit:
                htmls = await asyncio.gather(*(fetch(u) for u in to_visit))
                for u, h in zip(to_visit, htmls):
                    if not h:
                        continue
                    before = len(found)
                    found.extend(_extract_house_vote_links(u, h))
                    added = len(found) - before
                    if added:
                        log(f"      +{added} links from {u}")

    return _dedupe(found)


# ---- House vote link extractors ----
HOUSE_LINK_RE = re.compile(r'href=["\']([^"\']*PrintVote\.aspx\?[^"\']+)["\']', re.I)
HOUSE_PRINT_JS_RE = re.compile(r'PrintVote\.aspx\?[^"\'<>\s)]+', re.I)  # also from onclick/js
HOUSE_TXT_RE = re.compile(r'href=["\']([^"\']+/\d{2,3}LEG/OKH\d{5}\.TXT)["\']', re.I)

# pagination and per-measure pages to follow
HOUSE_PAGE_RE = re.compile(
    r'href=["\']([^"\']*ShowVotes\.aspx\?[^"\']*?\bYear=\d{4}[^"\']*?\bPage=\d+[^"\']*)["\']',
    re.I
)
HOUSE_MEASURE_RE = re.compile(
    r'href=["\']([^"\']*ShowVotes\.aspx\?[^"\']*?\bMeasure=[^"\']+)["\']',
    re.I
)

def _extract_house_vote_links(base: str, page_html: str) -> list[str]:
    """Find PrintVote.aspx links and direct TXT files on a House page."""
    uhtml = html.unescape(page_html or "")
    links: list[str] = []

    # href=…PrintVote…
    for m in HOUSE_LINK_RE.finditer(uhtml):
        links.append(urljoin(base, m.group(1)))

    # direct TXT files sometimes appear
    for m in HOUSE_TXT_RE.finditer(uhtml):
        links.append(urljoin(base, m.group(1)))

    # PrintVote inside JS/onclick or plain text
    for m in HOUSE_PRINT_JS_RE.finditer(uhtml):
        links.append(urljoin(base, m.group(0)))

    return _dedupe(links)

def _extract_house_subpages(base: str, page_html: str) -> tuple[list[str], list[str]]:
    """Return (pagination_pages, per_measure_pages) to fetch next."""
    uhtml = html.unescape(page_html or "")
    pages: list[str] = []
    measures: list[str] = []

    for m in HOUSE_PAGE_RE.finditer(uhtml):
        pages.append(urljoin(base, m.group(1)))
    for m in HOUSE_MEASURE_RE.finditer(uhtml):
        measures.append(urljoin(base, m.group(1)))
    return _dedupe(pages), _dedupe(measures)

    for m in HOUSE_LINK_RE.finditer(page_html or ""):
        href = m.group(1)
        absu = urljoin(base, href)
        links.append(absu)
    # Some pages might directly expose TXT files too
    for m in re.finditer(r'href=["\']([^"\']+/\d{2,3}LEG/OKH\d{5}\.TXT)["\']', page_html or "", re.I):
        links.append(urljoin(base, m.group(1)))
    return links

# SENATE: crawl oksenate.gov/senate-votes and likely year subpages; capture PDFs/HTML in the votes area
async def collect_senate_vote_urls_async(year_start=2006, year_end=2025, max_concurrency=24, max_depth=2) -> list[str]:
    seed_pages = [f"https://oksenate.gov/senate-votes"]
    # try some common year patterns proactively
    for y in range(year_start, year_end + 1):
        seed_pages.append(f"https://oksenate.gov/senate-votes?year={y}")
        seed_pages.append(f"https://oksenate.gov/senate-votes/{y}")

    seen_pages, to_visit = set(), []
    for u in seed_pages:
        if u not in seen_pages:
            seen_pages.add(u); to_visit.append((u, 0))

    found_vote_urls: set[str] = set()

    async def fetch(session, url):
        try:
            async with session.get(url) as resp:
                if resp.status != 200: return ""
                raw = await resp.read()
                try:
                    return raw.decode(_detect_charset_from_headers_or_html(raw), errors="ignore")
                except Exception:
                    return ""
        except Exception:
            return ""

    def extract_links(base, html_text):
        links = []
        for m in re.finditer(r'href=["\']([^"\']+)["\']', html_text or "", re.I):
            href = m.group(1)
            absu = urljoin(base, href)
            if urlparse(absu).netloc.endswith("oksenate.gov"):
                links.append(absu)
        return links

    def looks_like_vote_content(u: str) -> bool:
        p = urlparse(u)
        if not p.netloc.endswith("oksenate.gov"): return False
        if p.path.lower().endswith((".pdf", ".htm", ".html")) and ("vote" in p.path.lower() or "votes" in p.path.lower()):
            return True
        # many sites store files under /sites/default/files/... with 'vote' in filename
        if "vote" in p.geturl().lower() and p.geturl().lower().endswith((".pdf", ".htm", ".html")):
            return True
        return False

    if aiohttp is None:
        # sync fallback coarse crawl
        while to_visit:
            url, depth = to_visit.pop(0)
            if depth > max_depth: continue
            try:
                html_text = read_url_text(url)
            except Exception:
                continue
            for link in extract_links(url, html_text):
                if looks_like_vote_content(link):
                    found_vote_urls.add(link)
                elif depth + 1 <= max_depth and link not in seen_pages and "/senate-votes" in link:
                    seen_pages.add(link); to_visit.append((link, depth+1))
        return _dedupe(list(found_vote_urls))

    headers = {"User-Agent": "Mozilla/5.0 (OKVotes/4.0)"}
    timeout = aiohttp.ClientTimeout(total=20, connect=5)
    conn = aiohttp.TCPConnector(limit_per_host=max_concurrency, ttl_dns_cache=300)
    sem = asyncio.Semaphore(max_concurrency)

    async with aiohttp.ClientSession(headers=headers, timeout=timeout, connector=conn) as session:
        while to_visit:
            url, depth = to_visit.pop(0)
            if depth > max_depth: continue
            async with sem:
                html_text = await fetch(session, url)
            if not html_text: continue
            for link in extract_links(url, html_text):
                if looks_like_vote_content(link):
                    found_vote_urls.add(link)
                elif depth + 1 <= max_depth and link not in seen_pages and "/senate" in link and ("vote" in link or "votes" in link):
                    seen_pages.add(link); to_visit.append((link, depth+1))
    return _dedupe(list(found_vote_urls))

def _dedupe(seq):
    out, seen = [], set()
    for x in seq:
        if x not in seen:
            seen.add(x); out.append(x)
    return out
# =================== END NEW DISCOVERY ============================

def infer_page_bill(text: str, url: str = "") -> str:
    m = _BILL_RE.search(text)
    if m: return f"{m.group(1).upper()} {m.group(2)}"
    for pat, abbr in _BILL_FORMS:
        m2 = pat.search(text)
        if m2: return f"{abbr} {m2.group(1)}"
    if url:
        mu = re.search(r"/(s(?:b|jr|cr)|h(?:b|jr|cr))\s*0*?(\d{1,5})", url.lower())
        if mu: return f"{mu.group(1).upper()} {mu.group(2)}"
    return ""

# ===== Async fetch + parse pipeline =====
def _infer_loc_from_bill_or_url(bill: str, doc_location: str, url: str) -> str:
    if bill:
        b = bill.upper().strip()
        if b.startswith(("SB", "SJR", "SCR", "SR")): return "Senate Floor"
        if b.startswith(("HB", "HJR", "HCR", "HR")): return "House Floor"
    host = urlparse(url).netloc.lower()
    if "oksenate.gov" in host: return "Senate Floor"
    if "okhouse.gov" in host: return "House Floor"
    return (doc_location or "").strip()

async def harvest_urls_async(
    urls: list[str],
    max_concurrency: int = 128,
    request_timeout_s: float = 15.0
) -> tuple[list[dict], str]:
    if not urls:
        return [], ""

    total = len(urls)
    log(f"Fetching {total} vote pages…")

    if aiohttp is None:
        all_records, combined_text = [], []
        for i, url in enumerate(urls, 1):
            t0 = time.time()
            log(f"START [{i}/{total}] {url}")
            try:
                text = read_url_text(url)
            except Exception:
                log(f"FAIL  [{i}/{total}] {url}")
                continue
            if not text.strip():
                log(f"EMPTY [{i}/{total}] {url}")
                continue
            parsed = parse_votes_from_page(text, url)
            date_str, doc_location, time_fmt = parse_session_info(text)
            page_bill = infer_page_bill(text, url)

            added = 0
            for rec in parsed:
                bill = (rec.get("bill") or page_bill or "").strip()
                if page_bill and not _same_measure(bill, page_bill):
                    continue
                if not bill and page_bill:
                    bill = page_bill

                loc = _clean_loc(rec.get("location") or "") or ("Senate Floor" if bill.upper().startswith(("SB","SJR","SCR","SR")) else "House Floor")
                vtype = rec.get("type") or detect_type_in_context(text) or ""

                all_records.append({
                    "idx": len(all_records) + 1,
                    "bill": bill,
                    "type": vtype,
                    "yea": rec["yea"], "nay": rec["nay"], "cp": rec["cp"],
                    "excused": rec["excused"], "vacant": rec["vacant"],
                    "result": rec.get("result") or "",
                    "location": loc,
                    "date": (rec.get("date") or date_str or "").strip(),
                    "time": (rec.get("time") or time_fmt or "").strip(),
                    "names": rec["names"],
                })
                added += 1

            combined_text.append(text)
            dt = time.time() - t0
            left = total - i
            log(f"DONE  [{i}/{total}] {url}  records+={added}  left={left}  {dt:.1f}s")
        return all_records, "\n".join(combined_text)

    # --- async path ---
    headers = {
        "User-Agent": "Mozilla/5.0 (OKHouseVotes/3.0)",
        "Accept": "text/html,application/xhtml+xml,text/plain,*/*;q=0.1",
        "Accept-Encoding": "gzip",
        "Connection": "keep-alive",
    }
    timeout = aiohttp.ClientTimeout(total=request_timeout_s, connect=5)
    connector = aiohttp.TCPConnector(limit_per_host=max_concurrency, ttl_dns_cache=300)
    sem = asyncio.Semaphore(max_concurrency)

    results: list[tuple[int, str, str, list[dict], str, str, str]] = []
    done = 0
    lock = asyncio.Lock()

    async with aiohttp.ClientSession(headers=headers, timeout=timeout, connector=connector) as session:
        async def worker(order: int, url: str):
            nonlocal done
            async with sem:
                t0 = time.time()
                idx = order + 1
                log(f"START [{idx}/{total}] {url}")
                try:
                    text = await read_url_text_async(session, url)
                except Exception:
                    text = ""
                if not text.strip():
                    log(f"EMPTY [{idx}/{total}] {url}")
                else:
                    date_str, doc_location, time_fmt = parse_session_info(text)
                    parsed = parse_votes_from_page(text, url)
                    results.append((order, url, text, parsed, date_str, doc_location, time_fmt))
                async with lock:
                    done += 1
                    left = total - done
                    dt = time.time() - t0
                    log(f"DONE  [{idx}/{total}] {url}  parsed={len(parsed) if text else 0}  left={left}  {dt:.1f}s")

        tasks = [asyncio.create_task(worker(i, u)) for i, u in enumerate(urls)]
        await asyncio.gather(*tasks)

    results.sort(key=lambda t: t[0])
    all_records: list[dict] = []
    combined_text_parts: list[str] = []
    next_idx = 1
    for _, url, text, parsed, date_str, doc_location, time_fmt in results:
        combined_text_parts.append(text)
        page_bill = infer_page_bill(text, url)
        for rec in parsed:
            bill = (rec.get("bill") or page_bill or "").strip()
            if page_bill and not _same_measure(bill, page_bill):
                continue
            if not bill and page_bill:
                bill = page_bill
            if not bill and not _has_counts_or_names(rec):
                continue
            if not rec.get("location"):
                if bill.startswith(("SB", "SJR", "SCR", "SR")):
                    loc = "Senate Floor"
                elif bill.startswith(("HB", "HJR", "HCR", "HR")):
                    loc = "House Floor"
                else:
                    loc = (doc_location or "").strip()
            else:
                loc = _clean_loc(rec["location"])
            vtype = rec.get("type") or detect_type_in_context(text) or ""
            all_records.append({
                "idx": next_idx,
                "bill": bill,
                "type": vtype,
                "yea": rec["yea"], "nay": rec["nay"], "cp": rec["cp"],
                "excused": rec["excused"], "vacant": rec["vacant"],
                "result": rec.get("result") or "",
                "location": loc,
                "date": (rec.get("date") or date_str or "").strip(),
                "time": (rec.get("time") or time_fmt or "").strip(),
                "names": rec["names"],
            })
            next_idx += 1

    return all_records, "\n".join(combined_text_parts)

# ====== NEW: Discover OK Senate PDFs by year (2006–2025) ======
async def discover_ok_senate_pdfs_async(
    start_year: int = 2006,
    end_year: int = 2025,
    max_id: int = 6000,
    max_concurrency: int = 8,        # polite
    timeout_total: float = 8.0,
) -> list[str]:
    # --- SYNC FALLBACK if aiohttp is missing ---
    if aiohttp is None:
        import urllib.request, urllib.error
        found_sync: list[str] = []
        def head_or_get(u: str) -> bool:
            req = urllib.request.Request(u, method="HEAD", headers={"User-Agent":"Mozilla/5.0"})
            try:
                with urllib.request.urlopen(req, timeout=6, context=_SSL_CONTEXT) as r:
                    ct = (r.headers.get("Content-Type","") or "").lower()
                    return (getattr(r, "status", 200) in (200, 206)) and ("pdf" in ct or u.lower().endswith(".pdf"))
            except Exception:
                # some servers disallow HEAD; try GET
                try:
                    req2 = urllib.request.Request(u, method="GET", headers={"User-Agent":"Mozilla/5.0"})
                    with urllib.request.urlopen(req2, timeout=6, context=_SSL_CONTEXT) as r2:
                        ct = (r2.headers.get("Content-Type","") or "").lower()
                        return (getattr(r2, "status", 200) in (200, 206)) and ("pdf" in ct or u.lower().endswith(".pdf"))
                except Exception:
                    return False

        for year in range(start_year, end_year + 1):
            base = f"https://oksenate.gov/sites/default/files/votes_documents/votes_{year}/"
            log(f"  Year {year}: probing …")
            for i in range(1, max_id + 1):
                u = f"{base}{i:05d}.pdf"
                if head_or_get(u):
                    found_sync.append(u)
            log(f"  Year {year}: total found so far = {len(found_sync)}")
        found_sync.sort()
        log(f"PDF probe complete. Total files: {len(found_sync)}")
        return found_sync

    # --- ASYNC PATH (unchanged) ---
    headers = {
        "User-Agent": "Mozilla/5.0 (OKSenateVotes/1.1)",
        "Accept": "application/pdf,*/*;q=0.1",
        "Connection": "keep-alive",
    }
    timeout   = aiohttp.ClientTimeout(total=timeout_total, connect=5)
    connector = aiohttp.TCPConnector(limit_per_host=max_concurrency, ttl_dns_cache=300)
    sem       = asyncio.Semaphore(max_concurrency)

    found: list[str] = []

    async with aiohttp.ClientSession(headers=headers, timeout=timeout, connector=connector) as session:
        async def probe(url: str):
            async with sem:
                for attempt in range(3):
                    try:
                        async with session.head(url, allow_redirects=True) as r:
                            ct = (r.headers.get("Content-Type","") or "").lower()
                            if r.status in (200, 206) and ("pdf" in ct or url.lower().endswith(".pdf")):
                                found.append(url); return
                        async with session.get(url) as r2:
                            ct2 = (r2.headers.get("Content-Type","") or "").lower()
                            if r2.status in (200, 206) and ("pdf" in ct2 or url.lower().endswith(".pdf")):
                                found.append(url); return
                    except Exception:
                        pass
                    await asyncio.sleep(0.25 * (attempt + 1))

        CHUNK = 100
        for year in range(start_year, end_year + 1):
            base = f"https://oksenate.gov/sites/default/files/votes_documents/votes_{year}/"
            log(f"  Year {year}: probing …")
            for start in range(1, max_id + 1, CHUNK):
                tasks = [asyncio.create_task(probe(f"{base}{i:05d}.pdf")) for i in range(start, min(max_id, start + CHUNK - 1) + 1)]
                if tasks:
                    await asyncio.gather(*tasks)
                await asyncio.sleep(0.5)
            log(f"  Year {year}: total found so far = {len(found)}")

    found.sort()
    log(f"PDF probe complete. Total files: {len(found)}")
    return found



# ====== Dedupe helper ======
def _signature(rec):
    return (
        (rec.get("bill","") or "").strip().upper(),
        (rec.get("type","") or "").strip().upper(),
        (rec.get("date","") or "").strip(),
        (rec.get("time","") or "").strip(),
        (rec.get("location","") or "").strip().upper(),
    )
def _score(rec):
    cnts = sum(int(rec.get(k, 0) or 0) for k in ("yea","nay","cp","excused","vacant"))
    names = sum(len(rec.get("names", {}).get(b, [])) for b in ("yea","nay","cp","excused","vacant"))
    return (cnts, names)
def dedupe_records(records):
    best = {}
    for r in records:
        key = _signature(r)
        if key not in best or _score(r) > _score(best[key]):
            best[key] = r
    return list(best.values())

def discover_printvote_range(*args, **kwargs) -> list[str]:
    # Minimal synchronous probe for OK House TXT files → PrintVote viewer URLs
    import urllib.request, urllib.error
    series   = kwargs.get("series", "60LEG")
    start    = kwargs.get("start", 1)
    end      = kwargs.get("end", 2500)
    prefix   = kwargs.get("prefix", "OKH")
    use_viewer = kwargs.get("use_viewer", True)

    def build(i: int) -> str:
        file_url = f"https://former.okhouse.gov/{series}/{prefix}{i:05d}.TXT"
        return f"https://former.okhouse.gov/Legislation/PrintVote.aspx?Filename={file_url}" if use_viewer else file_url

    hits = []
    for i in range(start, end + 1):
        url = build(i)
        try:
            req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6, context=_SSL_CONTEXT) as r:
                chunk = r.read(4096).lower()
                if b"yea" in chunk or b"nay" in chunk or b"vote" in chunk:
                    hits.append(url)
        except Exception:
            pass
    return hits

async def discover_printvote_range_async(
    series: str = "58LEG",
    start: int = 1,
    end: int = 2500,
    prefix: str = "OKH",
    use_viewer: bool = True,
    max_concurrency: int = 128,
    timeout_total: float = 10.0,
    progress_every: int = 250,   # NEW: log every N probes
) -> list[str]:
    if aiohttp is None:
        return discover_printvote_range(series, start, end, prefix, use_viewer, max_workers=16, timeout=int(timeout_total))

    base_file = f"http://former.okhouse.gov/{series}/{prefix}"
    headers = {
        "User-Agent": "Mozilla/5.0 (OKHouseVotes/3.0)",
        "Accept": "text/html,application/xhtml+xml,text/plain,*/*;q=0.1",
        "Connection": "keep-alive",
    }
    vote_re = re.compile(rb"\b(HB|SB|HCR|HJR|SCR|SJR)\b|\byeas?\b|\bayes?\b|\bnays?\b|\bexcused\b", re.I)

    def build(i: int) -> str:
        file_url = f"{base_file}{i:05d}.TXT"
        return f"https://former.okhouse.gov/Legislation/PrintVote.aspx?Filename={file_url}" if use_viewer else file_url

    sem = asyncio.Semaphore(max_concurrency)
    timeout = aiohttp.ClientTimeout(total=timeout_total, connect=5)
    connector = aiohttp.TCPConnector(limit_per_host=max_concurrency, ttl_dns_cache=300)

    out: list[tuple[int, str]] = []
    probed = 0
    lock = asyncio.Lock()

    log(f"Scanning {series} ids {start}..{end} ({end-start+1} candidates)")

    async with aiohttp.ClientSession(headers=headers, timeout=timeout, connector=connector) as session:
        async def probe(i: int, url: str):
            nonlocal probed
            async with sem:
                try:
                    async with session.get(url) as resp:
                        chunk = await resp.content.read(4096)
                        ok = (resp.status != 404) and chunk and (b"not found" not in chunk.lower()) and (b"does not exist" not in chunk.lower())
                        if ok and (vote_re.search(chunk) or len(chunk) > 300):
                            out.append((i, url))
                            log(f"  [+] {series} FOUND {url}")
                except Exception:
                    pass
                finally:
                    async with lock:
                        probed += 1
                        if progress_every and (probed % progress_every == 0 or probed == (end-start+1)):
                            left = (end-start+1) - probed
                            log(f"  … {series} progress: probed={probed}, left={left}")

        tasks = [asyncio.create_task(probe(i, build(i))) for i in range(start, end + 1)]
        await asyncio.gather(*tasks)

    out.sort(key=lambda t: t[0])
    log(f"{series} complete: discovered {len(out)} vote files/pages")
    return [u for _, u in out]

# ====== Mode chooser & auto target ======
def ask_run_mode() -> str | None:
    if TK_OK:
        try:
            root = tk.Tk(); root.withdraw()
            res = messagebox.askyesnocancel("Run mode", "Do you want to run automatically?\n\nYes = Auto  •  No = Manual")
            root.destroy()
            if res is None: return None
            return "auto" if res else "manual"
        except Exception:
            pass
    ans = input("Run mode [auto/manual]? ").strip().lower()
    if not ans: return "auto"
    return "auto" if ans.startswith("a") else "manual"
def ask_chamber() -> str:
    # Return 'senate' or 'house'
    if TK_OK:
        try:
            root = tk.Tk(); root.withdraw()
            res = messagebox.askyesno(
                "Chamber",
                "Download **Senate** votes?\n\nClick No for **House** votes."
            )
            root.destroy()
            return "senate" if res else "house"
        except Exception:
            pass
    ans = input("Which chamber? [senate/house] ").strip().lower()
    return "senate" if ans.startswith("s") else "house"

def ask_auto_target() -> str | None:
    """Return 'senate' or 'house' (or None to cancel)."""
    if TK_OK:
        try:
            root = tk.Tk(); root.withdraw()
            res = messagebox.askquestion("Auto: What to download?", "Download Senate votes or House votes?\n\nYes = Senate  •  No = House", icon="question", type="yesno")
            root.destroy()
            return "senate" if res == "yes" else "house"
        except Exception:
            pass
    ans = input("Auto target [senate/house]? ").strip().lower()
    if ans.startswith("s"): return "senate"
    if ans.startswith("h"): return "house"
    return None

def select_files():
    if TK_OK:
        root = tk.Tk(); root.withdraw()
        paths = filedialog.askopenfilenames(
            title="Select PDFs / HTML files / Internet shortcuts / URL lists",
            filetypes=[
                ("All supported", "*.pdf *.html *.htm *.url *.webloc *.desktop *.txt"),
                ("PDF", "*.pdf"), ("HTML", "*.html *.htm"),
                ("Internet Shortcuts", "*.url *.webloc *.desktop"),
                ("Text URL Lists", "*.txt"), ("All files", "*.*"),
            ],
        )
        root.destroy()
        return list(paths or [])
    else:
        print("Headless mode: enter file/URL paths (blank line to finish).")
        items = []
        while True:
            s = input("> ").strip()
            if not s: break
            items.append(s)
        return items

def extract_url_from_url_shortcut(path: str) -> str:
    ext = pathlib.Path(path).suffix.lower()
    try:
        if ext == ".url":
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if line.lower().startswith("url="):
                        return line.split("=", 1)[1].strip()
        elif ext == ".webloc":
            with open(path, "rb") as f:
                d = plistlib.load(f)
            url = d.get("URL", "") or d.get("url", "")
            return str(url).strip()
        elif ext == ".desktop":
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if line.lower().startswith("url="):
                        return line.split("=", 1)[1].strip()
    except Exception:
        return ""
    return ""

def extract_urls_from_textfile(path: str):
    urls = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                for u in re.findall(r"https?://\S+", line):
                    u = u.rstrip("),.;]"); urls.append(u)
    except Exception:
        pass
    return urls
# ====== NEW: Discover ALL OK House vote files across 47LEG–60LEG (1999–2025) ======
# ---- ADD: async wrappers the code is calling ----
# replace your current discover_ok_senate_all_years_async with this
async def discover_ok_senate_all_years_async(start_year: int = 2006,
                                             end_year: int = 2025) -> list[str]:
    # 1) PDFs first – reliable and fast
    urls_from_pdfs = await discover_ok_senate_pdfs_async(
        start_year=start_year, end_year=end_year, max_id=6000,
        max_concurrency=128, timeout_total=8.0
    )

    # 2) Only try crawling /senate-votes pages if aiohttp is available
    urls_from_pages = []
    if aiohttp is not None:
        try:
            urls_from_pages = await collect_senate_vote_urls_async(
                year_start=start_year, year_end=end_year, max_concurrency=24, max_depth=2
            )
        except Exception:
            urls_from_pages = []

    return _dedupe(urls_from_pdfs + urls_from_pages)

# ---- SYNC fallback used when aiohttp is missing ----
def discover_ok_senate_from_index_sync(start_year=2006, end_year=2025):
    import re, time, urllib.request, urllib.error
    from urllib.parse import urljoin
    base = "https://oksenate.gov/senate-votes"

    LINK_RE = re.compile(r'href=["\']([^"\']+\.(?:pdf|htm|html))["\']', re.I)
    PAGE_RE = re.compile(r'href=["\'][^"\']*\byear=(\d{4})[^"\']*?\bpage=(\d+)["\']', re.I)

    # Look like a real browser and keep-alive; use the shared SSL context
    HDRS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
        "Referer": "https://oksenate.gov/senate-votes",
        "Cache-Control": "no-cache",
    }

    def fetch(u, label):
        # retry a couple of times with backoff; print the exact reason if it fails
        for attempt in range(1, 4):
            try:
                req = urllib.request.Request(u, headers=HDRS, method="GET")
                with urllib.request.urlopen(req, timeout=8, context=_SSL_CONTEXT) as r:
                    return r.read().decode("utf-8", "ignore")
            except urllib.error.HTTPError as e:
                log(f"{label}: HTTP {e.code} — {e.reason}")
                return ""  # don’t retry on HTTP codes
            except urllib.error.URLError as e:
                log(f"{label}: URL error — {getattr(e, 'reason', e)} (try {attempt}/3)")
            except Exception as e:
                log(f"{label}: {type(e).__name__} — {e} (try {attempt}/3)")
            time.sleep(0.8 * attempt)
        return ""

    def parse_links(base_url, html_text):
        out = []
        for m in LINK_RE.finditer(html_text or ""):
            u = urljoin(base_url, m.group(1))
            lu = u.lower()
            if "vote" in lu or "journal" in lu or "/votes_" in lu or "/votes-" in lu:
                out.append(u)
        return out

    found = set()
    for year in range(start_year, end_year + 1):
        url = f"{base}?year={year}"
        log(f"YEAR {year}: loading index…")
        html0 = fetch(url, f"YEAR {year} page 0")
        if not html0:
            log(f"YEAR {year}: no response")
            continue

        for u in parse_links(url, html0):
            found.add(u)

        pages = {0}
        for m in PAGE_RE.finditer(html0):
            try:
                pages.add(int(m.group(2)))
            except Exception:
                pass

        for p in sorted(p for p in pages if p):
            purl = f"{base}?year={year}&page={p}"
            htmlp = fetch(purl, f"YEAR {year} page {p}")
            if not htmlp:
                continue
            for u in parse_links(purl, htmlp):
                found.add(u)

        log(f"YEAR {year}: total links so far = {len(found)}")

    return sorted(found)


# ---- ASYNC version with fallback to the sync one if aiohttp is missing ----
async def discover_ok_senate_from_index_async(start_year=2006, end_year=2025, max_concurrency=24):
    if aiohttp is None:
        # run the sync discoverer in a thread so callers can await it
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, discover_ok_senate_from_index_sync, start_year, end_year)

    import re
    from urllib.parse import urljoin
    LINK_RE = re.compile(r'href=["\']([^"\']+\.(?:pdf|htm|html))["\']', re.I)
    PAGE_RE = re.compile(r'href=["\'][^"\']*\byear=(\d{4})[^"\']*?\bpage=(\d+)["\']', re.I)

    headers = {"User-Agent": "Mozilla/5.0 (OKVotes/4.0)"}
    timeout = aiohttp.ClientTimeout(total=20, connect=5)
    conn = aiohttp.TCPConnector(limit_per_host=max_concurrency, ttl_dns_cache=300)
    sem = asyncio.Semaphore(max_concurrency)

    def parse_links(base_url, html_text):
        out = []
        for m in LINK_RE.finditer(html_text or ""):
            u = urljoin(base_url, m.group(1))
            lu = u.lower()
            if "vote" in lu or "journal" in lu or "/votes_" in lu or "/votes-" in lu:
                out.append(u)
        return out

    found: set[str] = set()
    async with aiohttp.ClientSession(headers=headers, timeout=timeout, connector=conn) as session:
        async def fetch(url):
            async with sem:
                try:
                    async with session.get(url) as r:
                        if r.status != 200: return ""
                        b = await r.read()
                        return b.decode("utf-8", "ignore")
                except Exception:
                    return ""

        for year in range(start_year, end_year + 1):
            url = f"https://oksenate.gov/senate-votes?year={year}"
            log(f"YEAR {year}: loading index…")
            html0 = await fetch(url)
            if not html0:
                log(f"YEAR {year}: no response")
                continue

            for u in parse_links(url, html0):
                found.add(u)

            pages = {0}
            for m in PAGE_RE.finditer(html0):
                try: pages.add(int(m.group(2)))
                except: pass
            # fetch remaining pages concurrently
            page_urls = [f"https://oksenate.gov/senate-votes?year={year}&page={p}" for p in sorted(p for p in pages if p)]
            if page_urls:
                pages_html = await asyncio.gather(*(fetch(u) for u in page_urls))
                for purl, htmlp in zip(page_urls, pages_html):
                    for u in parse_links(purl, htmlp):
                        found.add(u)

            log(f"YEAR {year}: total links so far = {len(found)}")

    return sorted(found)



async def discover_ok_house_all_series_by_ids_async(
    leg_from: int = 47,
    leg_to: int = 60,
    max_id: int = 20000,
    use_viewer: bool = True,
    max_concurrency: int = 128,   # kept for async path
    timeout_total: float = 8.0,
    progress_every: int = 500,
) -> list[str]:
    # ---- If aiohttp is not available, do a SYNC ID-PROBE per series ----
    if aiohttp is None:
        all_urls: list[str] = []
        for leg in range(leg_from, leg_to + 1):
            series = f"{leg}LEG"
            log(f"Scanning (sync) {series} ids 1..{max_id}")
            hits = discover_printvote_range(
                series=series,
                start=1,
                end=max_id,
                prefix="OKH",
                use_viewer=use_viewer,   # PrintVote.aspx?Filename=…
            )
            log(f"{series}: discovered {len(hits)} vote files/pages (sync)")
            all_urls.extend(hits)
        return _dedupe(all_urls)

    # ---- Async path (unchanged) ----
    all_urls: list[str] = []
    for leg in range(leg_from, leg_to + 1):
        series = f"{leg}LEG"
        urls = await discover_printvote_range_async(
            series=series,
            start=1,
            end=max_id,
            prefix="OKH",
            use_viewer=use_viewer,
            max_concurrency=max_concurrency,
            timeout_total=timeout_total,
            progress_every=progress_every,
        )
        all_urls.extend(urls)
    return _dedupe(all_urls)


def read_any_text(path_or_url):
    if is_url(path_or_url):
        return read_url_text(path_or_url)
    ext = pathlib.Path(path_or_url).suffix.lower()
    if ext == ".pdf":
        try:
            with open(path_or_url, "rb") as f:
                raw = f.read()
            if _looks_like_pdf(raw):
                return _pdf_bytes_to_text(raw)
        except Exception:
            return ""
        return ""
    if ext in (".html", ".htm"):
        raw = open(path_or_url, "r", encoding="utf-8", errors="ignore").read()
        raw = _preprocess_ok_votes_html(raw)
        raw = html.unescape(raw)
        raw = re.sub(r"(?i)<br\s*/?>", "\n", raw)
        raw = re.sub(r"(?i)</(p|div|li|tr|h[1-6])>", "\n", raw)
        raw = re.sub(r"<script.*?</script>", " ", raw, flags=re.DOTALL | re.IGNORECASE)
        raw = re.sub(r"<style.*?</style>", " ", raw, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", raw)
        return _normalize_text(text)
    try:
        with open(path_or_url, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:
        return ""

def quick_ping(url: str, timeout: float = 6.0) -> bool:
    from urllib import request as _req
    try:
        log(f"Ping {url}")
        with _req.urlopen(_req.Request(url, headers={"User-Agent": "Mozilla/5.0 (OKVotes/3.1)"}), timeout=timeout, context=_SSL_CONTEXT) as r:
            log(f"Ping OK {getattr(r,'status',200)}")
            return True
    except Exception as e:
        log(f"Ping FAIL: {type(e).__name__}: {e}")
        return False



# ====== Main ======
def main():
    mode = ask_run_mode()
    if mode is None:
        print("Cancelled."); return

    sources: list[dict] = []
    url_list: list[str] = []
    file_list: list[str] = []
    if mode == "auto":
        chamber = ask_chamber()

        if chamber == "senate":
            # Force sync index discovery so we get continuous progress logs
            # in main(), senate branch:
            log("Discovering Senate votes (2006–2025)…")
            auto_urls = asyncio.run(discover_ok_senate_all_years_async(2006, 2025))
            log(f"Senate: discovered {len(auto_urls)} vote files/pages.")

        else:  # house (ID-probe, same logic as 58/60 code)
            log("Discovering House votes via TXT id probe (47LEG–60LEG)…")
            # quick reachability check (any House endpoint is fine)
            if not quick_ping("https://former.okhouse.gov/Legislation/ShowVotes.aspx?Year=2025", timeout=6.0):
                log("Cannot reach the House site. Check VPN/SSL/proxy or firewall. If the certificate is the problem, see 'Certificates' in the README.")
                return
            auto_urls = asyncio.run(
                discover_ok_house_all_series_by_ids_async(
                    leg_from=47,
                    leg_to=60,
                    max_id=20000,  # adjust if you want; 12–15k is usually plenty
                    use_viewer=True,  # feed PrintVote.aspx?Filename=... to the parser
                    max_concurrency=128,
                    timeout_total=8.0,
                    progress_every=500,
                )
            )
            log(f"House: discovered {len(auto_urls)} vote files/pages.")

        if not auto_urls:
            log("No vote URLs discovered.")
            return

        sources = [{"kind": "url", "value": u} for u in auto_urls]
        url_list = auto_urls[:]  # <<< add this
        file_list = []  # <<< add


    else:
        items = select_files()
        if not items:
            print("No files selected."); return
        for p in items:
            if is_url(p):
                sources.append({"kind": "url", "value": p}); continue
            ext = pathlib.Path(p).suffix.lower()
            if ext in (".url", ".webloc", ".desktop"):
                u = extract_url_from_url_shortcut(p)
                sources.append({"kind": "url", "value": u}) if u else None
                continue
            if ext == ".txt":
                urls = extract_urls_from_textfile(p)
                if urls:
                    for u in urls: sources.append({"kind": "url", "value": u})
                else:
                    sources.append({"kind": "file", "value": p})
                continue
            sources.append({"kind": "file", "value": p})
        url_list = [s["value"] for s in sources if s["kind"] == "url"]
        file_list = [s["value"] for s in sources if s["kind"] == "file"]

    # Fetch+parse all URLs concurrently
    recs_from_urls, combined_text_urls = asyncio.run(
        harvest_urls_async(url_list, max_concurrency=128, request_timeout_s=20.0)
    )

    # Parse local files synchronously (rare)
    recs_from_files: list[dict] = []; combined_text_files: list[str] = []
    next_idx = (max([r["idx"] for r in recs_from_urls], default=0) + 1)
    for path in file_list:
        try:
            text = read_any_text(path)
        except Exception as e:
            print(f"Skipped unreadable source: {path} ({e})"); continue
        if not text.strip():
            print(f"Empty content from {path}; skipped."); continue
        combined_text_files.append(text)
        date_str, doc_location, time_fmt = parse_session_info(text)
        parsed = parse_votes_from_page(text, path)
        page_bill = infer_page_bill(text)
        for rec in parsed:
            bill = (rec.get("bill") or page_bill or "").strip()
            if page_bill and not _same_measure(bill, page_bill): continue
            if not bill and page_bill: bill = page_bill
            loc = _clean_loc(rec.get("location")) if rec.get("location") else ( "Senate Floor" if bill.startswith(("SB","SJR","SCR","SR")) else ("House Floor" if bill.startswith(("HB","HJR","HCR","HR")) else (doc_location or "").strip()) )
            vtype = rec.get("type") or detect_type_in_context(text) or ""
            recs_from_files.append({
                "idx": next_idx, "bill": bill, "type": vtype,
                "yea": rec["yea"], "nay": rec["nay"], "cp": rec["cp"],
                "excused": rec["excused"], "vacant": rec["vacant"],
                "result": rec.get("result") or "",
                "location": loc,
                "date": (rec.get("date") or date_str or "").strip(),
                "time": (rec.get("time") or time_fmt or "").strip(),
                "names": rec["names"],
            }); next_idx += 1

    all_records = recs_from_urls + recs_from_files
    all_records = dedupe_records(all_records)
    if not all_records:
        print("No votes found in the selected sources."); return

    combined_text = "\n".join([combined_text_urls] + combined_text_files)
    save_table(HEADINGS,      table_votes_detail(all_records),  "VotesDetail.csv")
    save_table(HEADINGS_ROLL, table_vote_rollcall(all_records), "VoteRollCall.csv")
    member_rows = table_members(all_records, combined_text)
    if member_rows:
        save_table(HEADINGS_MEMBERS, member_rows, "Members.csv")
    else:
        print("No member names detected; Members.csv was not created.")

if __name__ == "__main__":
    main()
