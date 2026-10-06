#!/usr/bin/env python3
"""
extract.py - deterministic brand extraction from a website. Standard library only.

Usage:
    python3 extract.py <url> --out <work-dir> [--press-url URL] [--max-css 25] [--products 12]
    python3 extract.py <url> --out <work-dir> --mirror mirror-1.json [--mirror mirror-2.json] [--rendered]

Writes:
    <work-dir>/candidates.json          every measured value with provenance
    <work-dir>/raw/index.html           homepage HTML as fetched
    <work-dir>/raw/styles/NN.css        each stylesheet, numbered
    <work-dir>/raw/logo-candidates/     downloaded logo candidates
    <work-dir>/missing-urls.json        with --mirror: URLs the mirror did not have (feed back to capture.js)

The script measures. It never decides. Selection happens in selections.json (see references/selection-rules.md).
"""
import argparse
import base64
import hashlib
import json
import os
import re
import struct
import sys
import time
from collections import Counter
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

EXTRACTOR_VERSION = "1.0.0"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

GENERIC_FONTS = {"sans-serif", "serif", "monospace", "system-ui", "-apple-system", "blinkmacsystemfont",
                 "inherit", "initial", "unset", "cursive", "fantasy", "ui-sans-serif", "ui-serif",
                 "ui-monospace", "segoe ui", "roboto", "helvetica neue", "helvetica", "arial",
                 "apple color emoji", "segoe ui emoji", "segoe ui symbol", "noto color emoji", "var"}
# Roboto/Helvetica/Arial are common system fallbacks; they are kept in the pool but flagged as fallbacks.
SYSTEM_FALLBACKS = {"roboto", "helvetica neue", "helvetica", "arial", "segoe ui"}

LOGO_REJECT_HARD = re.compile(  # third-party marks: never the customer's logo, even when the filename says "logo"
    # Whole words only (plural allowed), so "elle" does not fire on aria-labelledby or bestsellers.
    r"(?<![a-z0-9])(?:payment|visa|mastercard|amex|american-?express|discover|paypal|apple-?pay|shop-?pay|google-?pay|amazon-?pay|"
    r"klarna|afterpay|affirm|sezzle|zip-?pay|app-?store|google-?play|facebook|instagram|twitter|tiktok|youtube|"
    r"pinterest|linkedin|trustpilot|bbb|norton|mcafee|as-?seen|press-?logo|partner-?logo|award|"
    r"stripe|shopify|braintree|recaptcha|yotpo|okendo|judge\.?me|klaviyo|truemed|"
    r"cgmp|gmp|tga|nsf|fda|certif\w*|b-?corp|climate-?neutral|usda|non-?gmo|kosher|halal|"
    r"nbc|cbs|abc-?news|fox|cnn|people|forbes|vogue|new-?york-?post|nypost|nytimes|ny-?times|wsj|wall-?street|"
    r"today-?show|buzzfeed|wired|techcrunch|oprah|good-?housekeeping|allure|elle|bazaar|gq|esquire|"
    r"usa-?today|business-?insider|bloomberg|reuters|huffpost|refinery|popsugar)s?(?![a-z0-9])|"
    r"svg-inline--fa|\bfa-|fontawesome|material-icons", re.I)
LOGO_REJECT_SOFT = re.compile(  # UI chrome: reject unless something says "logo"
    r"(badge|verified|secure|ssl|star|rating|flag|arrow|icon-|-icon|sprite|placeholder|spinner|loading|"
    r"hamburger|cart|search|close|chevron|caret|menu|account|user|\bbag\b|play-?button|checkmark)", re.I)
LOGO_HINT = re.compile(r"(logo|brand|wordmark|logotype)", re.I)
PRESS_HINT = re.compile(r"(press|media[-_ ]?kit|brand[-_ ]?(assets|kit|guidelines|resources)|newsroom|logos?\b)", re.I)

FRAMEWORK_DEFAULTS = {  # colors that ship with Bootstrap 4/5, BigCommerce Cornerstone, Foundation: never brand unless the screenshot agrees
    "#007bff", "#6c757d", "#28a745", "#dc3545", "#ffc107", "#17a2b8", "#343a40", "#f8f9fa", "#0d6efd", "#198754", "#0dcaf0",
    "#6610f2", "#6f42c1", "#e83e8c", "#fd7e14", "#20c997", "#212529", "#adb5bd", "#dee2e6", "#e9ecef", "#ced4da", "#495057",
    "#444444", "#666666", "#cccccc", "#1e90ff", "#3b82f6", "#2563eb", "#ef4444", "#10b981", "#f59e0b", "#0f172a", "#1f2937", "#111827",
}
NAMED_COLORS = {"white": "#ffffff", "black": "#000000", "red": "#ff0000", "blue": "#0000ff",
                "green": "#008000", "navy": "#000080", "gray": "#808080", "grey": "#808080"}


# ---------------------------------------------------------------- fetching
def fetch(url, timeout=25, binary=False, max_bytes=6_000_000):
    # urllib also opens file:// and ftp://; a page could point those at local files. Web and inline data only.
    if urlparse(url).scheme.lower() not in ("http", "https", "data"):
        raise ValueError(f"unsupported URL scheme: {url[:60]}")
    req = Request(url, headers={"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "identity",
                                "Accept-Language": "en-US,en;q=0.9"})
    with urlopen(req, timeout=timeout) as resp:
        data = resp.read(max_bytes)
        ctype = resp.headers.get("Content-Type", "")
        final = resp.geturl()
    if binary:
        return data, final, ctype
    enc = "utf-8"
    m = re.search(r"charset=([\w-]+)", ctype)
    if m:
        enc = m.group(1)
    try:
        return data.decode(enc, errors="replace"), final, ctype
    except LookupError:
        return data.decode("utf-8", errors="replace"), final, ctype


CHALLENGE = re.compile(r"(Just a moment|cf-chl|_cf_chl_opt|challenge-platform|Attention Required|Access denied|Verify you are human|"
                       r"Press (?:&|and|&amp;) Hold|Before we continue|not a bot|px-captcha|perimeterx|hcaptcha|g-recaptcha|"
                       r"Pardon Our Interruption|Request unsuccessful|Incapsula|distil_r_captcha)", re.I)
_chrome_state = {"checked": False, "chrome": None, "used": 0}


def chrome_fetch(url, timeout=45):
    """Fetch a page through headless Chrome (rendered DOM). Used when urllib is blocked by a bot challenge."""
    if not _chrome_state["checked"]:
        _chrome_state["checked"] = True
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import render  # noqa: WPS433
            _chrome_state["chrome"] = render.find_chrome()
            _chrome_state["render"] = render
        except Exception:  # noqa: BLE001
            _chrome_state["chrome"] = None
    if not _chrome_state["chrome"] or _chrome_state["used"] >= 8:
        return None
    _chrome_state["used"] += 1
    r = _chrome_state["render"].run_chrome(_chrome_state["chrome"], ["--window-size=1440,1100", "--timeout=15000", "--dump-dom", url], timeout=timeout)
    out = r.stdout or ""
    if len(out) < 500 or CHALLENGE.search(out[:3000]):
        return None
    return out


# Cowork and claude.ai sandboxes often cannot reach customer sites. scripts/capture.js runs in the user's browser and
# saves what it fetched to one JSON file: {url: {"status", "ctype", "body", "b64"?, "final"?, "error"?}, "__rendered__": {...}}.
# With --mirror, every fetch reads from those files instead of the network, and URLs they lack are logged for another pass.
_mirror = {"pages": None, "rendered": None, "meta": {}, "missing": [], "hits": 0, "unverified": []}


def _utf8(s):
    try:
        return s.encode("utf-8")
    except UnicodeEncodeError:  # lone surrogates: the browser's TextEncoder writes U+FFFD for them
        return s.encode("utf-16", "surrogatepass").decode("utf-16", "replace").encode("utf-8")


def mirror_digest(data):
    """Same SHA-256 capture.js stamps into __meta__.digest: every entry's url, status and body, in file order."""
    h = hashlib.sha256()
    for url, entry in data.items():
        if url == "__meta__" or not isinstance(entry, dict):
            continue
        status = entry.get("status")
        h.update(_utf8(f"{url}\n{'' if status is None else status}\n{entry.get('body') or ''}\n"))
    return h.hexdigest()


def _mkey(url):
    """Lookup key that ignores scheme, a leading www., an empty path and the fragment."""
    p = urlparse(url.split("#")[0])
    host = (p.hostname or "").lower().removeprefix("www.")
    return f"{host}{p.path or '/'}" + (f"?{p.query}" if p.query else "")


def load_mirrors(paths):
    pages = {}
    for path in paths:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if (data.get("__meta__") or {}).get("digest") != mirror_digest(data):
            _mirror["unverified"].append(os.path.basename(path))
        if "__rendered__" in data:
            _mirror["rendered"] = data["__rendered__"]
        _mirror["meta"] = data.get("__meta__") or _mirror["meta"]
        for url, entry in data.items():
            if url.startswith("__") or not isinstance(entry, dict):
                continue
            key = _mkey(url)
            if key in pages and pages[key].get("status") == 200:  # a later pass never overwrites a good copy
                continue
            pages[key] = dict(entry, _url=url)
    _mirror["pages"] = pages
    return pages


def mirror_fetch(url, binary=False, max_bytes=6_000_000, **_):
    if url.lower().startswith("data:"):  # inline data needs no network; capture.js never stores it
        try:
            return fetch(url, binary=binary, max_bytes=max_bytes)
        except Exception as e:  # noqa: BLE001
            return None, url, f"error: {e.__class__.__name__}: {e}"
    entry = _mirror["pages"].get(_mkey(url))
    if entry is None:
        if url not in _mirror["missing"]:
            _mirror["missing"].append(url)
        return None, url, "error: not in mirror"
    status = int(entry.get("status") or 0)
    if entry.get("error") or not 200 <= status < 300:  # capture.js tried and failed: same as a failed fetch
        return None, url, f"error: HTTP {status} in mirror" + (f" ({entry['error']})" if entry.get("error") else "")
    _mirror["hits"] += 1
    body = entry.get("body") or ""
    if entry.get("b64"):
        data = base64.b64decode(body)[:max_bytes]
        if not binary:
            data = data.decode("utf-8", errors="replace")
    else:
        data = body.encode("utf-8")[:max_bytes] if binary else body[:max_bytes]
    return data, entry.get("final") or entry["_url"], entry.get("ctype") or ""


def safe_fetch(url, **kw):
    """urllib first; on a bot challenge (Cloudflare etc.) fall back to headless Chrome for HTML and JSON."""
    if _mirror["pages"] is not None:
        return mirror_fetch(url, **kw)
    try:
        data, final, ctype = fetch(url, **kw)
        blocked = False
        if not kw.get("binary"):
            blocked = CHALLENGE.search(data[:4000]) is not None and ("text/html" in ctype)
        if not blocked:
            return data, final, ctype
        err = "challenge page"
    except Exception as e:  # noqa: BLE001
        err = f"error: {e.__class__.__name__}: {e}"
        if kw.get("binary") or not (("403" in err) or ("503" in err) or ("429" in err)):
            return None, url, err
    dom = chrome_fetch(url)
    if dom is None:
        return None, url, f"error: {err} (chrome fallback unavailable or also blocked)"
    if re.search(r"^\s*<html[^>]*><head></head><body><pre[^>]*>", dom, re.I) or url.endswith((".json",)) or ".json?" in url:
        m = re.search(r"<pre[^>]*>(.*?)</pre>", dom, re.S | re.I)
        if m:
            import html as _html
            return _html.unescape(m.group(1)), url, "application/json (via chrome)"
    return dom, url, "text/html (via chrome)"


def normalize_origin(url):
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, "", "", "", ""))


# ---------------------------------------------------------------- color utils
def clamp(n):
    return max(0, min(255, int(round(n))))


def normalize_color(value):
    """Return lowercase #rrggbb or None. Handles hex, rgb(a), hsl(a), 'r, g, b' triplets, a few names."""
    if value is None:
        return None
    v = value.strip().lower().rstrip(";").strip()
    v = re.sub(r"\s*!important$", "", v)
    if v in NAMED_COLORS:
        return NAMED_COLORS[v]
    m = re.fullmatch(r"#([0-9a-f]{3,8})", v)
    if m:
        h = m.group(1)
        if len(h) == 3:
            return "#" + "".join(c * 2 for c in h)
        if len(h) == 4:
            return "#" + "".join(c * 2 for c in h[:3])
        if len(h) == 6:
            return "#" + h
        if len(h) == 8:
            return "#" + h[:6]
        return None
    m = re.fullmatch(r"rgba?\(\s*([\d.]+)\s*[, ]\s*([\d.]+)\s*[, ]\s*([\d.]+)\s*(?:[,/]\s*[\d.%]+\s*)?\)", v)
    if m:
        return "#%02x%02x%02x" % tuple(clamp(float(x)) for x in m.groups())
    m = re.fullmatch(r"(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})", v)  # Shopify triplet
    if m:
        return "#%02x%02x%02x" % tuple(clamp(float(x)) for x in m.groups())
    m = re.fullmatch(r"hsla?\(\s*([\d.]+)(?:deg)?\s*[, ]\s*([\d.]+)%\s*[, ]\s*([\d.]+)%\s*(?:[,/]\s*[\d.%]+\s*)?\)", v)
    if m:
        h, s, l = float(m.group(1)) / 360, float(m.group(2)) / 100, float(m.group(3)) / 100
        import colorsys
        r, g, b = colorsys.hls_to_rgb(h, l, s)
        return "#%02x%02x%02x" % (clamp(r * 255), clamp(g * 255), clamp(b * 255))
    m = re.fullmatch(r"ok(lch|lab)\(\s*([\d.]+%?|none)\s+([-\d.]+%?|none)\s+([-\d.]+(?:deg)?%?|none)\s*(?:/\s*[\d.%]+\s*)?\)", v)
    if m:  # Tailwind v4 and other modern CSS: convert OKLCH/OKLab to sRGB hex (out-of-gamut values clipped)
        return _oklab_hex(*m.groups())
    return None


def _oklab_hex(kind, L, x, y):
    import math

    def num(s, pct_scale):
        if s == "none":
            return 0.0
        return float(s.rstrip("%")) * pct_scale / 100 if s.endswith("%") else float(s.replace("deg", ""))
    L = num(L, 1.0)
    if kind == "lch":
        C, H = num(x, 0.4), math.radians(num(y, 360))
        a, b = C * math.cos(H), C * math.sin(H)
    else:
        a, b = num(x, 0.4), num(y, 0.4)
    l_ = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    lin = (4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
           -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
           -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_)

    def gamma(c):
        c = min(1.0, max(0.0, c))
        return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055
    return "#%02x%02x%02x" % tuple(clamp(gamma(c) * 255) for c in lin)


COLOR_TOKEN_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)|hsla?\([^)]*\)|okl(?:ch|ab)\([^)]*\)", re.I)


# ---------------------------------------------------------------- CSS parsing
def strip_css_comments(css):
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


STRUCT_RE = re.compile(r"[{};]")


def _parse_decls(text):
    decls = {}
    for part in text.split(";"):
        if ":" not in part:
            continue
        prop, _, val = part.partition(":")
        prop, val = prop.strip().lower(), val.strip()
        if prop and val and re.fullmatch(r"[-\w]+", prop):
            decls[prop] = val
    return decls


def _split_block(inner):
    """Split a block body into (top-level declaration text, [(prelude, nested_inner), ...]) honoring nesting."""
    decl_parts, nested = [], []
    seg_start, depth, prelude_start = 0, 0, 0
    i, n = 0, len(inner)
    for m in STRUCT_RE.finditer(inner):
        ch, pos = m.group(0), m.start()
        if depth == 0:
            if ch == ";":
                decl_parts.append(inner[seg_start:pos])
                seg_start = pos + 1
            elif ch == "{":
                prelude = inner[seg_start:pos].strip()
                depth, prelude_start, block_start = 1, pos, pos + 1
                current_prelude = prelude
            elif ch == "}":
                seg_start = pos + 1  # stray close brace
        else:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    nested.append((current_prelude, inner[block_start:pos]))
                    seg_start = pos + 1
    if depth == 0 and seg_start < n:
        decl_parts.append(inner[seg_start:])
    return ";".join(decl_parts), nested


def parse_rules(css, source):
    """Yield (selector, {prop: value}, source). Handles @media/@supports/@container/@layer wrappers and CSS nesting
    (declarations and nested rules inside :root or any selector). @keyframes bodies are skipped."""
    css = strip_css_comments(css)
    out = []

    def process(inner, current_sel):
        decl_text, nested = _split_block(inner)
        if current_sel is not None:
            decls = _parse_decls(decl_text)
            if decls:
                out.append((current_sel, decls, source))
        for prelude, block in nested:
            p = re.sub(r"\s+", " ", prelude).strip()
            if not p:
                continue
            if p.startswith("@"):
                low = p.lower()
                if low.startswith(("@keyframes", "@-webkit-keyframes", "@counter-style", "@property", "@font-feature-values")):
                    continue
                if low.startswith("@font-face"):
                    d = _parse_decls(_split_block(block)[0])
                    if d:
                        out.append(("@font-face", d, source))
                    continue
                process(block, current_sel)  # conditional wrapper: inner content belongs to the same selector context
                continue
            if current_sel is None:
                sel = p
            elif "&" in p:
                sel = p.replace("&", current_sel)
            else:
                sel = current_sel + " " + p
            process(block, sel)

    process(css, None)
    return out


def resolve_var(value, varmap, depth=0):
    """Replace var(--x, fallback) with the mapped value when known."""
    if value is None or depth > 6 or "var(" not in value:
        return value

    def repl(m):
        name = m.group(1).strip()
        fallback = m.group(2)
        if name in varmap:
            return varmap[name]
        return fallback.strip() if fallback else m.group(0)

    new = re.sub(r"var\(\s*(--[\w-]+)\s*(?:,\s*([^()]*(?:\([^()]*\))?[^()]*))?\)", repl, value)
    if new == value:
        return value
    return resolve_var(new, varmap, depth + 1)


ICON_FONT = re.compile(r"(icon|glyph|fontawesome|font-awesome|font awesome|material symbols|material icons|swiper|slick|flickity|fontello)", re.I)


def first_family(value):
    value = re.sub(r"!important", "", value, flags=re.I).replace("\\", "")
    fam = value.split(",")[0].strip().strip("'\"").strip()
    if not fam or re.fullmatch(r"[\d.]+", fam) or fam.lower().startswith(("var(", "inherit", "initial")):
        return ""
    return fam


# ---------------------------------------------------------------- HTML parsing
class PageParser(HTMLParser):
    VOID = {"img", "link", "meta", "br", "hr", "input", "source"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.stylesheets = []
        self.style_blocks = []
        self._in_style = False
        self.icons = []
        self.meta = {}
        self.title = ""
        self._in_title = False
        self.imgs = []
        self.links = []
        self.headings = []
        self._heading_buf = None
        self.buttons = []
        self._button_buf = None
        self.link_text_buf = None
        self.scripts_src = []
        self.body_classes = ""
        self.html_attrs = {}
        self._display_open = []
        self.display_blocks = []
        self.inline_vars = []  # custom properties from style="" attributes

    def _in(self, tag_or_pred):
        for t, attrs in self.stack:
            if callable(tag_or_pred):
                if tag_or_pred(t, attrs):
                    return True
            elif t == tag_or_pred:
                return True
        return False

    def in_header(self):
        return self._in(lambda t, a: t == "header" or re.search(r"(^|\s|-|_)(header|masthead|site-nav|announcement)",
                                                                 (a.get("class", "") + " " + a.get("id", "")), re.I) is not None)

    def in_footer(self):
        return self._in(lambda t, a: t == "footer" or re.search(r"(^|\s|-|_)footer", a.get("class", "") + " " + a.get("id", ""), re.I) is not None)

    def in_nav(self):
        return self._in(lambda t, a: t == "nav" or a.get("role") == "navigation")

    def enclosing_link(self):
        for t, a in reversed(self.stack):
            if t == "a":
                return a.get("href", "")
        return None

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}  # bare attributes (e.g. <img alt>) parse as None
        if tag == "html":
            self.html_attrs = a
        if tag == "body":
            self.body_classes = a.get("class", "")
        if tag == "link":
            rel = (a.get("rel") or "").lower()
            href = a.get("href")
            if href:
                if "stylesheet" in rel:
                    self.stylesheets.append({"href": href, "media": a.get("media", "")})
                if "icon" in rel:
                    self.icons.append({"rel": rel, "href": href, "sizes": a.get("sizes", ""), "type": a.get("type", "")})
                if "preload" in rel and a.get("as") == "style":
                    self.stylesheets.append({"href": href, "media": ""})
        if tag == "meta":
            key = a.get("property") or a.get("name")
            if key:
                self.meta[key.lower()] = a.get("content", "")
        if tag == "style":
            self._in_style = True
            self.style_blocks.append("")
        if tag == "title":
            self._in_title = True
        if tag == "script" and a.get("src"):
            self.scripts_src.append(a["src"])
        if tag == "img":
            src = a.get("src") or a.get("data-src") or ""
            srcset = a.get("srcset") or a.get("data-srcset") or ""
            self.imgs.append({
                "src": src, "srcset": srcset, "alt": a.get("alt", ""), "class": a.get("class", ""),
                "width": a.get("width", ""), "height": a.get("height", ""),
                "in_header": self.in_header(), "in_footer": self.in_footer(), "in_nav": self.in_nav(),
                "link": self.enclosing_link(), "pos": self.getpos()[0],
            })
        if tag == "a":
            self.link_text_buf = {"href": a.get("href", ""), "text": "", "in_nav": self.in_nav(),
                                  "in_header": self.in_header(), "in_footer": self.in_footer(),
                                  "class": a.get("class", ""), "aria": a.get("aria-label", "")}
        if tag in ("h1", "h2", "h3"):
            self._heading_buf = {"tag": tag, "text": "", "class": a.get("class", "")}
        st = a.get("style", "")
        if "--" in st and len(self.inline_vars) < 4000:
            for part in st.split(";"):
                prop, _, val = part.partition(":")
                prop, val = prop.strip(), val.strip()
                if prop.startswith("--") and val:
                    self.inline_vars.append({"name": prop, "value": val, "selector": f"<{tag} style> " + (a.get("class", "")[:40] or a.get("id", "")), "source": "index.html inline style attribute"})
        cls = a.get("class", "")
        if tag == "button" or a.get("role") == "button" or (tag == "a" and (
                re.search(r"(btn|button|cta)", cls, re.I) or
                (re.search(r"\bbg-[a-z]", cls) and re.search(r"(rounded|px-|py-|border)", cls)))):
            self._button_buf = {"tag": tag, "class": cls, "text": ""}
        if re.search(r"(text-(?:3|4|5|6|7|8|9)xl|text-\[(?:[3-9]\d|1\d\d)px\]|\b(?:h1|h2|display|headline|hero-title|hero__title|heading|title)\b)", cls, re.I) \
                and tag not in ("html", "body", "header", "nav", "footer", "section", "main", "ul", "li", "a", "button", "img", "svg"):
            self._display_open.append({"tag": tag, "class": cls[:160], "text": "", "depth": len(self.stack)})
        if tag not in self.VOID:
            self.stack.append((tag, a))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID and self.stack and self.stack[-1][0] == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        if tag == "style":
            self._in_style = False
        if tag == "title":
            self._in_title = False
        if tag == "a" and self.link_text_buf is not None:
            lt = self.link_text_buf
            lt["text"] = re.sub(r"\s+", " ", lt["text"]).strip()
            self.links.append(lt)
            self.link_text_buf = None
        if tag in ("h1", "h2", "h3") and self._heading_buf is not None:
            hb = self._heading_buf
            hb["text"] = re.sub(r"\s+", " ", hb["text"]).strip()
            if hb["text"]:
                self.headings.append(hb)
            self._heading_buf = None
        if tag in ("button", "a") and self._button_buf is not None and self._button_buf["tag"] == tag:
            bb = self._button_buf
            bb["text"] = re.sub(r"\s+", " ", bb["text"]).strip()
            if bb["text"]:
                self.buttons.append(bb)
            self._button_buf = None
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break
        while self._display_open and self._display_open[-1]["depth"] >= len(self.stack):
            blk = self._display_open.pop()
            txt = re.sub(r"\s+", " ", blk["text"]).strip()
            if 2 <= len(txt) <= 140:
                self.display_blocks.append({"tag": blk["tag"], "class": blk["class"], "text": txt})

    def handle_data(self, data):
        if self._in_style and self.style_blocks:
            self.style_blocks[-1] += data
        if self._in_title:
            self.title += data
        if self.link_text_buf is not None:
            self.link_text_buf["text"] += data
        if self._heading_buf is not None:
            self._heading_buf["text"] += data
        if self._button_buf is not None:
            self._button_buf["text"] += data
        for blk in self._display_open:
            blk["text"] += data


# ---------------------------------------------------------------- inline SVG extraction (regex, keeps case)
def find_inline_svgs(html):
    out = []
    header_spans = [(m.start(), m.end()) for m in re.finditer(r"<(?:header|nav)\b.*?</(?:header|nav)>", html, re.S | re.I)]
    header_spans += [(m.start(), m.end()) for m in re.finditer(r"<[a-z]+\b[^>]*role=[\"']banner[\"'].*?</(?:div|section)>", html, re.S | re.I)]
    body_start = (re.search(r"<body\b", html, re.I) or re.search(r"<html\b", html, re.I))
    body_start = body_start.start() if body_start else 0
    footer_spans = [(m.start(), m.end()) for m in re.finditer(r"<footer\b.*?</footer>", html, re.S | re.I)]
    for m in re.finditer(r"<svg\b.*?</svg>", html, re.S | re.I):
        start = m.start()
        svg = m.group(0)
        if len(svg) > 400_000:
            continue
        before = html[max(0, start - 800):start]
        link_m = list(re.finditer(r"<a\b[^>]*href=[\"']([^\"']*)[\"'][^>]*>", before, re.I))
        enclosing = link_m[-1].group(1) if link_m and "</a>" not in before[link_m[-1].end():] else None
        wrapper = link_m[-1].group(0) if enclosing is not None else ""
        opening = re.match(r"<svg\b[^>]*>", svg, re.I).group(0)
        title_m = re.search(r"<title[^>]*>(.*?)</title>", svg, re.S | re.I)
        out.append({
            "markup": svg, "pos": start, "bytes": len(svg),
            "in_header": any(s <= start < e for s, e in header_spans) or (start - body_start < 12000 and enclosing in ("/", "./")),
            "in_footer": any(s <= start < e for s, e in footer_spans),
            "link": enclosing,
            "class": (re.search(r"class=[\"']([^\"']*)[\"']", opening, re.I) or [None, ""])[1],
            "aria": (re.search(r"aria-label=[\"']([^\"']*)[\"']", opening, re.I) or [None, ""])[1],
            "title": (title_m.group(1).strip() if title_m else ""),
            "viewbox": (re.search(r"viewBox=[\"']([^\"']*)[\"']", opening, re.I) or [None, ""])[1],
            "wrapper": wrapper[:300],
        })
    return out


# ---------------------------------------------------------------- SVG sprite resolution
_sprite_cache = {}


def resolve_svg_use(markup, page_html, base_url):
    """If an inline <svg> only references a <symbol> via <use>, return a standalone SVG built from that symbol."""
    m = re.search(r"<use\b[^>]*(?:xlink:)?href=[\"']([^\"']+)[\"']", markup, re.I)
    if not m:
        return None, None
    ref = m.group(1)
    file_part, _, frag = ref.partition("#")
    if not frag:
        return None, None
    if file_part:
        url = urljoin(base_url, file_part)
        if url not in _sprite_cache:
            data, _, ct = safe_fetch(url, max_bytes=4_000_000)
            _sprite_cache[url] = data if data and not str(ct).startswith("error") else ""
        source_doc, source_label = _sprite_cache[url], url
    else:
        source_doc, source_label = page_html, "inline symbol"
    if not source_doc:
        return None, None
    sym = re.search(r"<symbol\b[^>]*\bid=[\"']" + re.escape(frag) + r"[\"'][^>]*>(.*?)</symbol>", source_doc, re.S | re.I)
    if not sym:
        sym = re.search(r"<(?:g|svg)\b[^>]*\bid=[\"']" + re.escape(frag) + r"[\"'][^>]*>(.*?)</(?:g|svg)>", source_doc, re.S | re.I)
    if not sym:
        return None, None
    opening = sym.group(0)[:sym.group(0).find(">") + 1]
    vb = re.search(r"viewBox=[\"']([^\"']+)[\"']", opening, re.I)
    if not vb:
        vb = re.search(r"viewBox=[\"']([^\"']+)[\"']", markup[:400], re.I)
    inner = sym.group(1)
    has_fill = re.search(r"\bfill=[\"'](?!none)", inner) is not None
    root = '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"'
    if vb:
        root += f' viewBox="{vb.group(1)}"'
    if not has_fill:
        root += ' fill="currentColor" style="color:#000000"'
    root += ">"
    # carry <defs> (gradients, clip paths) from the sprite when the symbol references ids
    defs = ""
    if re.search(r"url\(#", inner):
        d = re.search(r"<defs\b[^>]*>(.*?)</defs>", source_doc, re.S | re.I)
        if d:
            defs = "<defs>" + d.group(1) + "</defs>"
    return root + defs + inner + "</svg>", source_label


# ---------------------------------------------------------------- image dimension sniffing
def image_dims(data, fmt):
    try:
        if fmt == "png" and data[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", data[16:24])
            return w, h
        if fmt in ("jpg", "jpeg") and data[:2] == b"\xff\xd8":
            i = 2
            while i < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
                i += 2 + seg_len
        if fmt == "svg":
            txt = data.decode("utf-8", errors="ignore")[:4000]
            vb = re.search(r"viewBox=[\"']\s*[\d.-]+[\s,]+[\d.-]+[\s,]+([\d.]+)[\s,]+([\d.]+)", txt)
            if vb:
                return int(float(vb.group(1))), int(float(vb.group(2)))
            w = re.search(r"\bwidth=[\"']([\d.]+)", txt)
            h = re.search(r"\bheight=[\"']([\d.]+)", txt)
            if w and h:
                return int(float(w.group(1))), int(float(h.group(1)))
        if fmt == "webp" and data[:4] == b"RIFF":
            if data[12:16] == b"VP8X":
                w = 1 + int.from_bytes(data[24:27], "little")
                h = 1 + int.from_bytes(data[27:30], "little")
                return w, h
    except Exception:  # noqa: BLE001
        pass
    return None, None


def guess_format(url, ctype, data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:2] == b"\xff\xd8":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if b"<svg" in data[:2000].lower():
        return "svg"
    if data[:4] == b"\x00\x00\x01\x00":
        return "ico"
    ext = os.path.splitext(urlparse(url).path)[1].lower().lstrip(".")
    if ext in ("png", "jpg", "jpeg", "svg", "webp", "gif", "ico"):
        return "jpeg" if ext == "jpg" else ext
    if "svg" in ctype:
        return "svg"
    if "png" in ctype:
        return "png"
    return "bin"


def largest_from_srcset(srcset):
    best, best_w = None, -1
    for part in srcset.split(","):
        bits = part.strip().split()
        if not bits:
            continue
        url = bits[0]
        w = 0
        if len(bits) > 1:
            m = re.match(r"([\d.]+)(w|x)", bits[1])
            if m:
                w = float(m.group(1)) * (1000 if m.group(2) == "x" else 1)
        if w > best_w:
            best, best_w = url, w
    return best


# ---------------------------------------------------------------- element text lookup by class
def element_text_by_class(html, cls, max_len=160):
    """Return (tag, inner text) of the first element whose class attribute contains `cls`, or (None, None)."""
    m = re.search(r"<([a-z][a-z0-9]*)\b[^>]*class=[\"'][^\"']*(?<![\w-])" + re.escape(cls) + r"(?![\w-])[^\"']*[\"'][^>]*>", html, re.I)
    if not m:
        return None, None
    tag = m.group(1).lower()
    i, depth, start = m.end(), 1, m.end()
    tok = re.compile(r"<(/?)" + re.escape(tag) + r"\b[^>]*>", re.I)
    while depth > 0:
        t = tok.search(html, i)
        if not t or t.start() - start > 20000:
            break
        depth += -1 if t.group(1) else 1
        i = t.end()
    inner = html[start:i]
    inner = re.sub(r"<!--.*?-->", " ", inner, flags=re.S)
    inner = re.sub(r"<(script|style)\b.*?</\1>", " ", inner, flags=re.S | re.I)
    import html as _html
    text = _html.unescape(re.sub(r"<[^>]+>", " ", inner))
    text = re.sub(r"[\s\u00a0]+", " ", text).strip()
    return tag, text[:max_len]


def px(value):
    """font-size to px when expressible (px, rem, em at 16px); else None."""
    if not value:
        return None
    m = re.match(r"^\s*([\d.]+)\s*(px|rem|em)?\s*(?:!important)?\s*$", value)
    if not m:
        return None
    n, unit = float(m.group(1)), (m.group(2) or "px")
    return n if unit == "px" else n * 16


# ---------------------------------------------------------------- JSON-LD and product cards
def _ld_text(v):
    """JSON-LD text fields may be a string, a list of strings, or {"@value": ...}. Return a string or None."""
    if isinstance(v, list):
        v = next((x for x in v if x), None)
    if isinstance(v, dict):
        v = v.get("@value") or v.get("name")
    if v is None or isinstance(v, (dict, list)):
        return None
    return str(v).strip() or None


def _ld_dict(v):
    """First dict of a value that may be a dict, a list of dicts, or junk."""
    if isinstance(v, list):
        v = next((x for x in v if isinstance(x, dict)), None)
    return v if isinstance(v, dict) else {}


def _walk_ld(node, out, in_breadcrumb=False):
    if isinstance(node, list):
        for n in node:
            _walk_ld(n, out, in_breadcrumb)
        return
    if not isinstance(node, dict):
        return
    t = node.get("@type")
    types = t if isinstance(t, list) else ([t] if t else [])
    if any(str(ty) == "BreadcrumbList" for ty in types):
        in_breadcrumb = True
    for ty in types:
        out["types"].append(str(ty))
    if any(str(ty) in ("Organization", "Brand", "Corporation", "OnlineStore", "Store", "LocalBusiness") for ty in types):
        lg = node.get("logo")
        if isinstance(lg, dict):
            lg = lg.get("url") or lg.get("contentUrl")
        if isinstance(lg, str) and lg:
            out["org_logos"].append(lg)
        if node.get("name"):
            out["org_names"].append(str(node["name"]))
    if any(str(ty) == "ListItem" for ty in types) and not in_breadcrumb:
        item = node.get("item") if isinstance(node.get("item"), dict) else {}
        name = _ld_text(item.get("name")) or _ld_text(node.get("name"))
        url = _ld_text(item.get("url")) or _ld_text(item.get("@id")) or _ld_text(node.get("url"))
        if name and url and not any(str(t) == "Product" for t in ([item.get("@type")] if isinstance(item.get("@type"), str) else (item.get("@type") or []))):
            img = item.get("image")
            if isinstance(img, list):
                img = img[0] if img else ""
            if isinstance(img, dict):
                img = img.get("url") or ""
            offers = item.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            out["products"].append({"title": name, "url": url, "image": img or "", "price": (str(offers.get("price")) if offers.get("price") is not None else None),
                                    "currency": offers.get("priceCurrency"), "brand": None, "product_type": None,
                                    "body_html_excerpt": re.sub(r"<[^>]+>", " ", str(item.get("description") or ""))[:240].strip(),
                                    "list_position": node.get("position")})
    if any(str(ty) == "Product" for ty in types):
        offers = node.get("offers")
        if isinstance(offers, list):
            with_price = [o for o in offers if isinstance(o, dict) and (o.get("price") or o.get("lowPrice") or o.get("priceSpecification"))]
            offers = with_price[0] if with_price else (offers[0] if offers and isinstance(offers[0], dict) else {})
        offers = offers if isinstance(offers, dict) else {}
        if not offers.get("price") and isinstance(offers.get("priceSpecification"), (dict, list)):
            ps = offers["priceSpecification"]
            ps = ps[0] if isinstance(ps, list) and ps else ps
            if isinstance(ps, dict) and ps.get("price"):
                offers = {**offers, "price": ps.get("price"), "priceCurrency": ps.get("priceCurrency") or offers.get("priceCurrency")}
        if not offers.get("price") and offers.get("lowPrice"):
            offers = {**offers, "price": offers.get("lowPrice")}
        img = node.get("image")
        if isinstance(img, list):
            img = img[0] if img else ""
        if isinstance(img, dict):
            img = img.get("url") or img.get("contentUrl") or ""
        brand = _ld_text(node.get("brand"))
        rating = _ld_dict(node.get("aggregateRating"))
        price = offers.get("price") or offers.get("lowPrice")
        out["products"].append({
            "title": _ld_text(node.get("name")), "url": _ld_text(node.get("url")) or _ld_text(offers.get("url")), "image": img,
            "price": str(price) if price is not None else None, "currency": offers.get("priceCurrency"),
            "brand": brand, "sku": node.get("sku"), "product_type": node.get("category"),
            "body_html_excerpt": re.sub(r"<[^>]+>", " ", str(node.get("description") or ""))[:240].strip(),
            "rating": rating.get("ratingValue"),
            "review_count": rating.get("reviewCount"),
        })
    for k, v in node.items():
        if k in ("@context",):
            continue
        if isinstance(v, (dict, list)):
            _walk_ld(v, out, in_breadcrumb)


def parse_jsonld(html):
    out = {"types": [], "org_logos": [], "org_names": [], "products": []}
    for m in re.finditer(r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", html, re.S | re.I):
        txt = m.group(1).strip()
        if not txt:
            continue
        try:
            _walk_ld(json.loads(txt), out)
        except Exception:  # noqa: BLE001
            # some sites embed several objects or trailing commas; try a lenient split
            try:
                _walk_ld(json.loads(re.sub(r",\s*([}\]])", r"\1", txt)), out)
            except Exception:  # noqa: BLE001
                continue
    out["types"] = sorted(set(out["types"]))
    seen = set()
    out["products"] = [p for p in out["products"] if p.get("title") and not (p["title"].lower() in seen or seen.add(p["title"].lower()))]
    return out


PRICE_RE = re.compile(r"(?:[$€£]\s?\d[\d,]*(?:\.\d{2})?|\d[\d,]*(?:\.\d{2})?\s?(?:USD|EUR|GBP))")
REAL_PRICE_RE = re.compile(r"(?:[$€£]\s?\d{1,3}(?:,\d{3})*\.\d{2}|[$€£]\s?\d{2,5}\b(?!\s?(?:off|%|/mo|per))|\d[\d,]*\.\d{2}\s?(?:USD|EUR|GBP))", re.I)
UTILITY_LINK = re.compile(r"(reward|loyalty|account|login|sign-?in|help|contact|order|gift-?card|store-?locator|locations?\b|blog|about|career|press|policy|terms|privacy|faq|app-?store|play\.google|javascript:)", re.I)
NAV_TITLES = {"home", "all products", "shop", "shop all", "products", "menu", "search", "cart", "bag", "account", "sign in", "log in", "next", "previous", "close", "learn more", "shop now"}


def product_cards(html, base_url):
    """Heuristic product cards: an anchor that wraps an image and sits near a price. Bounded, provenance-tagged."""
    out, seen = [], set()
    for m in re.finditer(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.{0,4000}?)</a>", html, re.S | re.I):
        href, inner = m.group(1), m.group(2)
        if "<img" not in inner.lower():
            continue
        window = html[m.start():m.end() + 1200]
        price = REAL_PRICE_RE.search(window)
        if not price:
            continue
        absu = urljoin(base_url, href)
        if absu in seen or UTILITY_LINK.search(absu) or not re.search(r"(product|/p/|/p$|/p\?|/dp/|item|shop|\.html|/\d{5,})", absu, re.I):
            continue
        img = re.search(r"<img[^>]*(?:data-src|src)=[\"']([^\"']+)[\"']", inner, re.I)
        alt = re.search(r"alt=[\"']([^\"']*)[\"']", inner, re.I)
        text = re.sub(r"<[^>]+>", " ", inner)
        text = re.sub(r"\s+", " ", text).strip()
        title = (alt.group(1).strip() if alt and alt.group(1).strip() else text[:80]).strip()
        if not title or len(title) < 3 or title.lower() in NAV_TITLES or re.search(r"(logo|rewards|icon)", title, re.I):
            continue
        seen.add(absu)
        out.append({"title": title, "url": absu, "image": urljoin(base_url, img.group(1)) if img else "",
                    "price": price.group(0).strip(), "body_html_excerpt": text[:200]})
        if len(out) >= 12:
            break
    return out


def embedded_json_products(html, base_url, cap=12):
    """Products serialized into script JSON: objects carrying name + url + a price. Escaped JSON is unescaped first."""
    txt = html.replace('\\"', '"').replace("\\/", "/")
    out, seen = [], set()
    price_pats = [
        r'"price"\s*:\s*\{[^{}]{0,200}?"formatted"\s*:\s*"([^"]+)"',          # BigCommerce {"price":{"without_tax":{"formatted":"$15.00"}}}
        r'"price"\s*:\s*"?(\d+(?:\.\d{1,2})?)"?\s*[,}]',                       # "price":"29.99" / "price":29.99
        r'"(?:priceValue|salePrice|currentPrice|price_min|minPrice)"\s*:\s*"?(\d+(?:\.\d{1,2})?)"?',
    ]
    for pat in price_pats:
        for m in re.finditer(pat, txt):
            window = txt[max(0, m.start() - 3000):m.start()]
            names = re.findall(r'"(?:name|title|productName|displayName)"\s*:\s*"([^"]{3,120})"', window)
            urls = re.findall(r'"(?:url|productUrl|href|path|handle)"\s*:\s*"([^"]{2,300})"', window)
            imgs = re.findall(r'"(?:image|img|imageUrl|src|data|featured_image)"\s*:\s*(?:\{[^{}]{0,200}?"(?:url|src|data)"\s*:\s*)?"(https?://[^"]+\.(?:jpe?g|png|webp|avif)[^"]*)"', window)
            if not names:
                continue
            name = names[-1].strip()
            if name.lower() in NAV_TITLES or name.lower() in seen or re.search(r"(logo|icon|banner)", name, re.I):
                continue
            url = urls[-1] if urls else ""
            if url and UTILITY_LINK.search(url):
                url = ""
            price = m.group(1)
            if not re.search(r"\d", price):
                continue
            seen.add(name.lower())
            out.append({"title": name, "url": urljoin(base_url, url) if url else "", "image": imgs[-1] if imgs else "",
                        "price": price, "body_html_excerpt": ""})
            if len(out) >= cap:
                return out
        if out:
            break
    return out


def _json_blobs(html):
    """Yield parsed JSON objects embedded in <script> tags (application/json, window.X = {...}, JSON strings)."""
    for m in re.finditer(r"<script\b[^>]*>(.*?)</script>", html, re.S | re.I):
        body = m.group(1).strip()
        if len(body) < 200:
            continue
        starts = [i for i in (body.find("{"), body.find("[")) if i >= 0]
        if not starts:
            continue
        i = min(starts)
        opener, closer = body[i], ("}" if body[i] == "{" else "]")
        depth, j, in_str, esc = 0, i, False, False
        while j < len(body):
            ch = body[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch in "{[":
                    depth += 1
                elif ch in "}]":
                    depth -= 1
                    if depth == 0:
                        break
            j += 1
        chunk = body[i:j + 1]
        parsed = None
        if len(chunk) >= 200:
            try:
                parsed = json.loads(chunk)
            except Exception:  # noqa: BLE001
                parsed = None
        if parsed is None and '\\"' in body:
            # JSON serialized inside a JS string literal (BigCommerce Stencil bootstrap, JSON.parse("...")): unescape and retry
            un = body.replace('\\"', '"').replace("\\/", "/").replace("\\\\", "\\")
            k = un.find("{")
            if k >= 0:
                depth, j2, in_str, esc = 0, k, False, False
                while j2 < len(un):
                    ch = un[j2]
                    if in_str:
                        if esc:
                            esc = False
                        elif ch == "\\":
                            esc = True
                        elif ch == '"':
                            in_str = False
                    else:
                        if ch == '"':
                            in_str = True
                        elif ch in "{[":
                            depth += 1
                        elif ch in "}]":
                            depth -= 1
                            if depth == 0:
                                break
                    j2 += 1
                try:
                    parsed = json.loads(un[k:j2 + 1])
                except Exception:  # noqa: BLE001
                    parsed = None
        if parsed is not None:
            yield parsed


def _price_from(v):
    if isinstance(v, (int, float)) and v > 0:
        return f"{v:.2f}"
    if isinstance(v, str):
        m = re.search(r"\d[\d,]*(?:\.\d{1,2})?", v)
        if m and float(m.group(0).replace(",", "")) > 0:
            return m.group(0).replace(",", "")
        return None
    if isinstance(v, dict):
        for k in ("without_tax", "with_tax", "value", "amount", "price", "formatted", "min", "minPrice", "sale", "current"):
            if k in v:
                r = _price_from(v[k])
                if r:
                    return r
    if isinstance(v, list) and v:
        return _price_from(v[0])
    return None


def _image_from(v):
    if isinstance(v, str) and re.search(r"\.(jpe?g|png|webp|avif)(\?|$)", v, re.I):
        return v
    if isinstance(v, dict):
        for k in ("url", "src", "data", "original", "large", "path"):
            r = _image_from(v.get(k))
            if r:
                return r
    if isinstance(v, list) and v:
        return _image_from(v[0])
    return None


def json_walk_products(html, base_url, cap=12):
    out, seen = [], set()

    def walk(node, depth=0):
        if len(out) >= cap or depth > 40:
            return
        if isinstance(node, list):
            for n in node:
                walk(n, depth + 1)
            return
        if isinstance(node, str):
            if len(node) > 2000 and node.lstrip()[:1] in "{[":
                try:
                    walk(json.loads(node), depth + 1)
                except Exception:  # noqa: BLE001
                    pass
            return
        if not isinstance(node, dict):
            return
        name = node.get("name") or node.get("title") or node.get("productName") or node.get("displayName")
        price_key = next((k for k in ("price", "prices", "salePrice", "priceRange", "price_range", "currentPrice", "pricing") if k in node), None)
        if isinstance(name, str) and price_key:
            price = _price_from(node.get(price_key))
            url = next((node.get(k) for k in ("url", "productUrl", "path", "link", "href", "custom_url") if isinstance(node.get(k), str)), None)
            if not url and isinstance(node.get("custom_url"), dict):
                url = node["custom_url"].get("url")
            if not url and isinstance(node.get("handle"), str):
                url = "/products/" + node["handle"]
            img = _image_from(node.get("image") or node.get("images") or node.get("featured_image") or node.get("thumbnail") or node.get("img"))
            key = name.strip().lower()
            if price and key not in seen and key not in NAV_TITLES and not re.search(r"(landing|banner|logo|icon|category page)", key):
                seen.add(key)
                out.append({"title": name.strip()[:120], "url": urljoin(base_url, url) if url and not UTILITY_LINK.search(url) else "",
                            "image": urljoin(base_url, img) if img else "", "price": price,
                            "sku": node.get("sku") if isinstance(node.get("sku"), str) else None,
                            "body_html_excerpt": re.sub(r"<[^>]+>", " ", str(node.get("description") or ""))[:200].strip()})
        for v in node.values():
            if isinstance(v, (dict, list, str)):
                walk(v, depth + 1)

    for blob in _json_blobs(html):
        walk(blob)
        if len(out) >= cap:
            break
    return out


PDP_LINK = re.compile(r"(/products?/[^/?#]+/?$|/p/[^/?#]+|/dp/[^/?#]+|/\d{5,}\.html$|/[a-z0-9-]+/\d{4,}\.html$|/[a-z0-9-]+-p\d+\b|/item/)", re.I)


def pdp_link_probe(links, final_origin, cap=8):
    """Fetch product-detail pages linked from the homepage and read their schema.org Product data."""
    out, seen, fetched = [], set(), 0
    for l in links:
        href = l.get("href") or ""
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absu = urljoin(final_origin + "/", href).split("#")[0]
        if not absu.startswith(final_origin) or absu in seen or not PDP_LINK.search(urlparse(absu).path) or UTILITY_LINK.search(absu):
            continue
        seen.add(absu)
        if fetched >= cap:
            break
        pp, _, ppct = safe_fetch(absu, max_bytes=2_500_000)
        fetched += 1
        if not pp or str(ppct).startswith("error"):
            continue
        found = [f for f in parse_jsonld(pp)["products"] if f.get("title")]
        if found:
            f0 = found[0]
            f0["url"] = absu
            f0["provenance"] = "pdp-json-ld:" + absu
            if not f0.get("price"):
                mp = re.search(r'(?:product:price:amount|og:price:amount)[\"\']\s+content=[\"\']([\d.,]+)', pp)
                if mp:
                    f0["price"] = mp.group(1)
                else:
                    jp = json_walk_products(pp, absu, cap=3)
                    if jp:
                        f0["price"] = jp[0]["price"]
                        f0["provenance"] += " + price from page JSON"
            out.append(f0)
    return out


# ---------------------------------------------------------------- main extraction
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--out", required=True)
    ap.add_argument("--press-url", default=None, help="Optional official press/brand assets page")
    ap.add_argument("--max-css", type=int, default=25)
    ap.add_argument("--products", type=int, default=12)
    ap.add_argument("--html", default=None, help="Use this saved HTML (e.g. raw/rendered.html from render.py dom) instead of fetching the homepage")
    ap.add_argument("--mirror", action="append", default=[], help="capture.js output; read every URL from it instead of the network (repeat for later passes)")
    ap.add_argument("--rendered", action="store_true", help="With --mirror, read the homepage from the browser's rendered DOM (JS-rendered sites)")
    args = ap.parse_args()

    out = args.out
    raw = os.path.join(out, "raw")
    os.makedirs(os.path.join(raw, "styles"), exist_ok=True)
    os.makedirs(os.path.join(raw, "logo-candidates"), exist_ok=True)

    origin = normalize_origin(args.url)
    notes = []
    t0 = time.time()
    if args.mirror:
        load_mirrors(args.mirror)
        notes.append(f"Offline run from browser mirror ({', '.join(os.path.basename(m) for m in args.mirror)}); nothing fetched from the network.")
        if _mirror["unverified"]:
            notes.append(f"UNVERIFIED mirror: {', '.join(_mirror['unverified'])} is not untouched capture.js output (digest missing or "
                         "does not match). Values read from it are not measured; recapture with capture.js.")
        site = _mkey(origin + "/").split("/")[0]
        if not any(k.split("/")[0] == site for k in _mirror["pages"]):  # never measure one site's capture as another's
            hosts = sorted({k.split("/")[0] for k in _mirror["pages"]})[:5]
            print(json.dumps({"status": "error", "message": f"The mirror has no pages from {site}; it holds {', '.join(hosts) or 'nothing'}.",
                              "hint": "Run capture.js in a tab open on this site, or pass the URL capture.js ran on."}))
            sys.exit(2)

    rendered = _mirror["rendered"] if args.mirror else None
    home_entry = _mirror["pages"].get(_mkey(origin + "/")) if args.mirror else None
    if args.html:
        html = open(args.html, encoding="utf-8", errors="replace").read()
        final_url, ctype = origin + "/", "text/html (from --html file)"
        notes.append(f"Homepage HTML read from {args.html} (rendered DOM), not fetched.")
    elif rendered and (args.rendered or home_entry is None):
        html = rendered.get("body") or ""
        final_url = (home_entry or {}).get("final") or (home_entry or {}).get("_url") or _mirror["meta"].get("origin", origin).rstrip("/") + "/"
        ctype = "text/html (rendered DOM from browser mirror)"
        notes.append("Homepage HTML is the browser's rendered DOM from the mirror.")
    else:
        html, final_url, ctype = safe_fetch(origin + "/")
        hint = ("If this environment cannot reach the site (Cowork, claude.ai), use the browser mirror: "
                "see 'Site can't be reached' in SKILL.md.") if not args.mirror else \
               "The mirror has no homepage for this URL. Check the URL matches the site capture.js ran on."
        if html is None:
            print(json.dumps({"status": "error", "message": f"Could not fetch {origin}: {ctype}", "hint": hint}))
            sys.exit(2)
        if str(ctype).startswith("error"):
            print(json.dumps({"status": "error", "message": ctype, "hint": hint}))
            sys.exit(2)
    final_origin = normalize_origin(final_url)
    with open(os.path.join(raw, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)

    page = PageParser()
    try:
        page.feed(html)
    except Exception as e:  # noqa: BLE001
        notes.append(f"HTML parser stopped early: {e}")

    # ---- platform detection
    platform = "unknown"
    lower = html.lower()
    if "cdn.shopify.com" in lower or "shopify.theme" in lower or "myshopify.com" in lower:
        platform = "shopify"
    elif "wp-content" in lower and "woocommerce" in lower:
        platform = "woocommerce"
    elif "wp-content" in lower:
        platform = "wordpress"
    # Match asset paths and markup attributes, never a bare product name: pages mention "Webflow" or "Magento" in copy,
    # and a bare "mage/" matched "image/" and tagged most sites Magento.
    elif re.search(r"cdn\d*\.bigcommerce\.com|data-stencil|stencil-utils", lower):
        platform = "bigcommerce"
    elif re.search(r"demandware\.static|/on/demandware|dwstatic", lower):
        platform = "salesforce-commerce-cloud"
    elif re.search(r"x-magento-init|data-mage-init|magento_[a-z]|/mage/(?:cookies|requirejs|translate|storage)|requirejs-config\.js", lower):
        platform = "magento"
    elif re.search(r"data-wf-(?:site|page)|website-files\.com|webflow\.js", lower):
        platform = "webflow"
    elif re.search(r"static1\.squarespace\.com|squarespace-cdn\.com|data-squarespace", lower):
        platform = "squarespace"
    elif "__next" in lower or "_next/static" in lower:
        platform = "nextjs"
    elif "__nuxt" in lower or "data-nuxt" in lower or "/_nuxt/" in lower:
        platform = "nuxt"

    # ---- stylesheets
    styles = []  # list of (source_label, css_text)
    for i, blk in enumerate(page.style_blocks):
        if blk.strip():
            styles.append((f"inline-style-{i}", blk))
    seen = set()
    fetched = 0
    for s in page.stylesheets:
        if fetched >= args.max_css:
            notes.append(f"Stylesheet cap reached ({args.max_css}); some stylesheets not fetched.")
            break
        href = s["href"]
        if href.startswith("data:"):
            continue
        absu = urljoin(final_url, href)
        if absu in seen:
            continue
        seen.add(absu)
        css, _, ct = safe_fetch(absu, max_bytes=3_000_000)
        if css is None or str(ct).startswith("error"):
            notes.append(f"Stylesheet fetch failed: {absu}")
            continue
        fetched += 1
        label = f"{fetched:02d}.css"
        with open(os.path.join(raw, "styles", label), "w", encoding="utf-8") as f:
            f.write(f"/* source: {absu} */\n" + css)
        styles.append((label + " <- " + absu, css))
        for imp in re.findall(r"@import\s+(?:url\()?[\"']?([^\"')\s;]+)", css):
            impu = urljoin(absu, imp)
            if impu in seen or fetched >= args.max_css:
                continue
            seen.add(impu)
            icss, _, ict = safe_fetch(impu, max_bytes=3_000_000)
            if icss and not str(ict).startswith("error"):
                fetched += 1
                label = f"{fetched:02d}.css"
                with open(os.path.join(raw, "styles", label), "w", encoding="utf-8") as f:
                    f.write(f"/* source: {impu} */\n" + icss)
                styles.append((label + " <- " + impu, icss))

    # ---- font service links
    font_services = []
    for s in page.stylesheets:
        h = s["href"]
        if "fonts.googleapis.com" in h:
            fams = re.findall(r"family=([^&:]+)", h)
            font_services.append({"service": "google-fonts", "url": h, "families": [f.replace("+", " ") for f in fams]})
        elif "use.typekit.net" in h or "typekit" in h:
            font_services.append({"service": "adobe-fonts", "url": h, "families": []})
        elif "fonts.com" in h or "fast.fonts.net" in h:
            font_services.append({"service": "monotype", "url": h, "families": []})
    for src in page.scripts_src:
        if "use.typekit.net" in src:
            font_services.append({"service": "adobe-fonts", "url": src, "families": []})
    _seen_fs, _fs = set(), []
    for fs in font_services:
        if fs["url"] not in _seen_fs:
            _seen_fs.add(fs["url"])
            _fs.append(fs)
    font_services = _fs

    # ---- parse CSS
    rules = []
    for label, css in styles:
        for sel, decls, src in parse_rules(css, label):
            rules.append((sel, decls, src))

    varmap = {}
    css_vars = []
    for sel, decls, src in rules:
        for prop, val in decls.items():
            if prop.startswith("--"):
                css_vars.append({"name": prop, "value": val, "selector": sel[:120], "source": src})
                # first definition on :root/html/body wins for resolution; others recorded
                if prop not in varmap and re.match(r"^(:root|html|body|\.color-scheme|\[data)", sel.strip()):
                    varmap[prop] = val
    for iv in page.inline_vars:  # <html style> / <body style> declarations rank with :root
        css_vars.append(iv)
        if iv["name"] not in varmap and iv["selector"].startswith(("<html", "<body")):
            varmap[iv["name"]] = iv["value"]
    for cv in css_vars:
        if cv["name"] not in varmap:
            varmap[cv["name"]] = cv["value"]

    def color_entries(decl_val, sel, src, prop):
        resolved = resolve_var(decl_val, varmap)
        found = []
        for tok in COLOR_TOKEN_RE.findall(resolved):
            hexv = normalize_color(tok)
            if hexv:
                found.append({"hex": hexv, "raw": decl_val, "property": prop, "selector": sel[:160], "source": src})
        hexv = normalize_color(resolved)
        if hexv and not found:
            found.append({"hex": hexv, "raw": decl_val, "property": prop, "selector": sel[:160], "source": src})
        return found

    buttons, links_css, body_css, headings_css, radii, transforms, shadows = [], [], [], [], Counter(), Counter(), Counter()
    families = Counter()
    family_sources = {}
    font_faces = []
    color_freq = Counter()
    color_examples = {}
    BUTTON_SEL = re.compile(r"(\bbutton\b|\.btn\b|\.button\b|btn-|button-|button_|--button|\bcta\b|shopify-payment-button|\.product-form__submit)", re.I)
    STATE = re.compile(r":(hover|focus|active|disabled|visited|focus-visible)", re.I)

    for sel, decls, src in rules:
        if sel.startswith("@font-face"):
            fam = decls.get("font-family", "").strip("'\" ")
            font_faces.append({"family": fam, "src": decls.get("src", "")[:300], "weight": decls.get("font-weight", ""),
                               "style": decls.get("font-style", ""), "source": src})
            continue
        ff = decls.get("font-family")
        if ff:
            fam = first_family(resolve_var(ff, varmap))
            if fam and fam.lower() not in GENERIC_FONTS and not fam.startswith("--"):
                families[fam] += 1
                family_sources.setdefault(fam, []).append({"selector": sel[:120], "source": src})
        for prop, val in decls.items():
            if prop in ("color", "background", "background-color", "border-color", "fill", "border", "outline-color"):
                for ce in color_entries(val, sel, src, prop):
                    color_freq[ce["hex"]] += 1
                    color_examples.setdefault(ce["hex"], ce)
            if prop == "border-radius":
                radii[resolve_var(val, varmap)] += 1
            if prop == "text-transform":
                transforms[val] += 1
            if prop == "box-shadow" and val not in ("none", "0"):
                shadows[resolve_var(val, varmap)[:120]] += 1
        s_clean = sel.strip()
        cursor_button = ("pointer" in decls.get("cursor", "") and ("background" in decls or "background-color" in decls)
                         and ("padding" in decls or "padding-top" in decls or "border-radius" in decls))
        if (BUTTON_SEL.search(s_clean) or cursor_button) and not re.search(r"(icon|close|menu|hamburger|search|cart|drawer|carousel|slider|nav)", s_clean, re.I):
            entry = {"selector": s_clean[:160], "source": src, "state": "hover" if STATE.search(s_clean) else "base"}
            for prop in ("background", "background-color", "color", "border", "border-radius", "padding", "text-transform",
                         "font-weight", "letter-spacing", "font-family", "font-size", "border-color"):
                if prop in decls:
                    entry[prop] = resolve_var(decls[prop], varmap)
            hexes = {p: normalize_color(entry[p]) for p in ("background", "background-color", "color", "border-color")
                     if p in entry and not entry[p].strip().startswith("#") and normalize_color(entry[p])}
            if hexes:  # oklch()/rgb() fills restated as the hex that appears in colors.pool
                entry["hex"] = hexes
            if "padding-top" in decls and "padding" not in entry:
                entry["padding"] = f"{decls.get('padding-top','')} {decls.get('padding-right','')}".strip()
            if cursor_button:
                entry["via"] = "cursor-pointer rule"
                m1 = re.fullmatch(r"\.((?:[\w-]|\\.)+)", s_clean)
                if m1:
                    _, txt = element_text_by_class(html, re.sub(r"\\(.)", r"\1", m1.group(1)), 60)
                    if txt:
                        entry["text"] = txt
            if any(k in entry for k in ("background", "background-color", "color", "border-radius")):
                buttons.append(entry)
        if re.fullmatch(r"a|a:link|a:not\([^)]*\)|\.link", s_clean) and "color" in decls:
            links_css.append({"selector": s_clean, "color": resolve_var(decls["color"], varmap), "source": src})
        if re.fullmatch(r"(html|body|html,\s*body|body,\s*html|:root)", s_clean):
            e = {"selector": s_clean, "source": src}
            for prop in ("font-family", "color", "background", "background-color", "font-size", "line-height"):
                if prop in decls:
                    e[prop] = resolve_var(decls[prop], varmap)
            if len(e) > 2:
                body_css.append(e)
        if re.match(r"^(h1|h2|h3|\.h1|\.h2|\.h3|\.heading|\.title|h1,|\.rte h1)", s_clean) and not STATE.search(s_clean):
            e = {"selector": s_clean[:120], "source": src}
            for prop in ("font-family", "font-weight", "text-transform", "letter-spacing", "font-size", "line-height", "color"):
                if prop in decls:
                    e[prop] = resolve_var(decls[prop], varmap)
            if len(e) > 2:
                headings_css.append(e)

    # named palette utility classes (.color-cataire-auburn, .bg-sand, .text-ink) give brand names for colors
    named_palette = []
    NAMED_SEL = re.compile(r"^\.(?:color|bg|background|text|fill|brand|palette|swatch)-([a-z0-9][a-z0-9-]{1,40})$", re.I)
    for sel, decls, src in rules:
        for single in [x.strip() for x in sel.split(",")]:
            m = NAMED_SEL.match(single)
            if not m:
                continue
            for prop in ("color", "background-color", "background", "fill"):
                if prop in decls:
                    hexv = normalize_color(resolve_var(decls[prop], varmap))
                    if hexv:
                        named_palette.append({"name": m.group(1).lower(), "hex": hexv, "property": prop, "selector": single, "source": src})
    SCHEME_SEL = re.compile(r"^\.(?:color-)?scheme-([a-z0-9][\w-]{0,60})$|^\[data-(?:color-)?scheme=[\"']?([\w-]+)[\"']?\]$", re.I)
    for sel, decls, src in rules:
        for single in [x.strip() for x in sel.split(",")]:
            m = SCHEME_SEL.match(single)
            if not m:
                continue
            scheme = (m.group(1) or m.group(2) or "").lower()
            if re.fullmatch(r"[0-9a-f-]{20,}", scheme):  # uuid-named schemes carry no meaning
                continue
            for prop, val in decls.items():
                if prop.startswith("--color") and not prop.endswith("-rgb"):
                    hexv = normalize_color(resolve_var(val, varmap))
                    if hexv:
                        named_palette.append({"name": f"{scheme}/{prop[2:].replace('color-', '')}", "hex": hexv, "property": prop, "selector": single, "source": src})
    seen_np = set()
    named_palette = [np_ for np_ in named_palette if not (np_["name"], np_["hex"]) in seen_np and not seen_np.add((np_["name"], np_["hex"]))]
    for np_ in named_palette:
        color_freq[np_["hex"]] += 2
        color_examples.setdefault(np_["hex"], {"hex": np_["hex"], "raw": np_["hex"], "property": np_["property"], "selector": np_["selector"], "source": np_["source"]})

    # JSON-LD: Organization.logo is the site's own declared logo; Product entries are real catalog items
    jsonld = parse_jsonld(html)

    # large type: classes with font-size >= 28px are display/heading styles; pull their text and measured sizes
    large_type = {}
    for sel, decls, src in rules:
        m1 = re.fullmatch(r"\.((?:[\w-]|\\.)+)", sel.strip())
        if not m1 or "font-size" not in decls:
            continue
        size = px(resolve_var(decls["font-size"], varmap))
        if size is None or size < 28:
            if size is None or m1.group(1) not in large_type:
                continue
        entry = large_type.setdefault(m1.group(1), {"class": m1.group(1), "font_sizes": [], "source": src})
        if size is not None and size not in entry["font_sizes"]:
            entry["font_sizes"].append(size)
        for prop in ("font-weight", "letter-spacing", "line-height", "color", "font-family", "text-transform"):
            if prop in decls and prop not in entry:
                entry[prop] = resolve_var(decls[prop], varmap)
    # companion classes (.f4 + .f4-desktop, .heading + .heading-lg) split one style across two selectors; merge the
    # family/weight from the base class into its size variants so the entry is complete
    for cls, entry in large_type.items():
        base = re.sub(r"(-desktop|-mobile|-lg|-md|-sm|-xl)$", "", cls)
        if base != cls and base in large_type:
            for prop in ("font-family", "font-weight", "letter-spacing", "text-transform"):
                if prop not in entry and prop in large_type[base]:
                    entry[prop] = large_type[base][prop]
    large_type = {k: v for k, v in large_type.items() if v["font_sizes"] and max(v["font_sizes"]) >= 28}
    large_type_out = []
    for cls, entry in list(large_type.items())[:40]:
        tag, txt = element_text_by_class(html, re.sub(r"\\(.)", r"\1", cls), 140)
        if txt:
            entry["tag"], entry["text"] = tag, txt
        large_type_out.append(entry)
    large_type_out.sort(key=lambda e: (0 if e.get("text") else 1, -max(e["font_sizes"])))  # used classes first, then unused utilities

    # color vars (resolved) - the most trustworthy signal
    color_vars = []
    for cv in css_vars:
        resolved = resolve_var(cv["value"], varmap)
        hexv = normalize_color(resolved)
        if hexv is None:
            toks = COLOR_TOKEN_RE.findall(resolved)
            hexv = normalize_color(toks[0]) if toks else None
        if hexv:
            color_vars.append({**cv, "hex": hexv})
            color_freq[hexv] += 2  # variables get extra weight
            color_examples.setdefault(hexv, {"hex": hexv, "raw": cv["value"], "property": cv["name"],
                                              "selector": cv["selector"], "source": cv["source"]})
    theme_color = normalize_color(page.meta.get("theme-color", ""))
    if theme_color:
        color_freq[theme_color] += 3
        color_examples.setdefault(theme_color, {"hex": theme_color, "raw": page.meta.get("theme-color"),
                                                 "property": "meta theme-color", "selector": "<meta>", "source": "index.html"})

    # ---- logo candidates
    candidates, rejected = [], []
    # the site's own name never counts as a third-party mark (a payment company's own logo carries its own name)
    own_brand = re.sub(r"[^a-z0-9]", "", (urlparse(final_origin).hostname or "").lower().removeprefix("www.").split(".")[0]
                       + " " + page.meta.get("og:site_name", "").lower())

    def third_party(blob):
        for m in LOGO_REJECT_HARD.finditer(blob):
            term = re.sub(r"[^a-z0-9]", "", m.group(0).lower())
            if not (term and own_brand and (term in own_brand or term.rstrip("s") in own_brand)):
                return m
        return None

    def add_candidate(url, source, score, role_hint, **extra):
        u = urljoin(final_url, url)
        blob = " ".join(str(v) for v in [u, extra.get("alt", ""), extra.get("class_", ""), extra.get("aria", ""), extra.get("title", "")])
        own = " ".join(str(v) for v in [os.path.basename(urlparse(u).path), extra.get("alt", ""), extra.get("class_", ""), extra.get("aria", ""), extra.get("title", "")])
        hard = third_party(blob)
        soft = LOGO_REJECT_SOFT.search(own) if source not in ("favicon", "apple-touch-icon", "mask-icon", "json-ld-organization", "press-page") else None
        if hard or (soft and not LOGO_HINT.search(blob)):
            rejected.append({"url": u, "source": source, "reason": ("third-party mark: " if hard else "ui chrome: ") + (hard or soft).group(0)})
            return
        if any(c["url"] == u for c in candidates):
            return
        candidates.append({"url": u, "source": source, "score": score, "role_hint": role_hint, **extra})

    for im in page.imgs:
        src = largest_from_srcset(im["srcset"]) if im["srcset"] else im["src"]
        if not src or src.startswith("data:image/gif"):
            continue
        blob = " ".join([src, im["alt"], im["class"]])
        home_link = im["link"] in ("/", final_origin, final_origin + "/", origin, origin + "/")
        if im["in_header"] and (home_link or LOGO_HINT.search(blob)):
            add_candidate(src, "header-img", 95 if home_link else 90, "primary", alt=im["alt"], class_=im["class"], link=im["link"])
        elif LOGO_HINT.search(blob) and im["in_footer"]:
            add_candidate(src, "footer-img", 70, "on-dark-or-secondary", alt=im["alt"], class_=im["class"], link=im["link"])
        elif LOGO_HINT.search(blob):
            add_candidate(src, "body-img", 60, "unknown", alt=im["alt"], class_=im["class"], link=im["link"])
        elif home_link and im["pos"] < 400:
            add_candidate(src, "early-home-link-img", 55, "primary", alt=im["alt"], class_=im["class"], link=im["link"])

    inline_svgs = find_inline_svgs(html)
    svg_n = 0
    for sv in inline_svgs:
        own = " ".join([sv["class"], sv["aria"], sv["title"]])
        blob = own + " " + sv["wrapper"]
        home_link = sv["link"] in ("/", final_origin, final_origin + "/", origin, origin + "/")
        is_logo_hint = LOGO_HINT.search(blob) is not None
        hard = third_party(blob)
        if hard or (LOGO_REJECT_SOFT.search(own) and not LOGO_HINT.search(own)):
            m_ = hard or LOGO_REJECT_SOFT.search(own)
            rejected.append({"url": f"inline-svg@{sv['pos']}", "source": "inline-svg", "reason": ("third-party mark: " if hard else "ui chrome: ") + m_.group(0)})
            continue
        if sv["in_header"] and (home_link or is_logo_hint):
            score = 100 if home_link else 92
            role = "primary"
        elif sv["in_footer"] and (home_link or is_logo_hint):
            score, role = 72, "on-dark-or-secondary"
        elif is_logo_hint:
            score, role = 58, "unknown"
        else:
            continue
        svg_n += 1
        fname = f"inline-svg-{svg_n:02d}.svg"
        markup = sv["markup"]
        sprite_note = None
        if re.search(r"<use\b", markup, re.I) and not re.search(r"<(path|rect|circle|polygon|ellipse|text|image)\b", markup, re.I):
            resolved, src_label = resolve_svg_use(markup, html, final_url)
            if resolved:
                markup = resolved
                sprite_note = f"Built from SVG sprite symbol ({src_label})."
            else:
                sprite_note = "Inline <use> reference could not be resolved; file is a sprite reference and will render empty."
        if "xmlns=" not in markup[:300]:
            markup = markup.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
        opening_end = markup.find(">") + 1
        body_has_fill = re.search(r"\bfill=[\"'](?!none)", markup[opening_end:]) is not None
        fill_fixed = False
        if not body_has_fill:
            root = markup[:opening_end]
            if re.search(r"\bfill=[\"']none[\"']", root):
                root = re.sub(r"\bfill=[\"']none[\"']", 'fill="currentColor"', root, count=1)
                fill_fixed = True
            elif "fill=" not in root:
                root = root.replace("<svg", '<svg fill="currentColor"', 1)
                fill_fixed = True
            if fill_fixed and "style=" not in root:
                root = root.replace("<svg", '<svg style="color:#000000"', 1)
            markup = root + markup[opening_end:]
        if sprite_note and "could not be resolved" in sprite_note:
            score = min(score, 20)
        with open(os.path.join(raw, "logo-candidates", fname), "w", encoding="utf-8") as f:
            f.write(markup)
        w, h = image_dims(markup.encode("utf-8"), "svg")
        candidates.append({"url": f"inline-svg@{sv['pos']}", "source": "inline-svg-" + ("header" if sv["in_header"] else "footer" if sv["in_footer"] else "body"),
                           "score": score, "role_hint": role, "local_path": f"raw/logo-candidates/{fname}", "format": "svg",
                           "width": w, "height": h, "bytes": sv["bytes"], "class_": sv["class"], "aria": sv["aria"],
                           "title": sv["title"], "link": sv["link"], "downloaded": True,
                           "fill_note": ("Fill inherited from a CSS class on the site; file set to currentColor with color #000000. "
                                         "Recolor by changing the style color.") if fill_fixed else None,
                           "sprite_note": sprite_note})

    for ic in page.icons:
        rel = ic["rel"]
        if "apple-touch-icon" in rel:
            add_candidate(ic["href"], "apple-touch-icon", 50, "icon", sizes=ic["sizes"])
        elif "mask-icon" in rel:
            add_candidate(ic["href"], "mask-icon", 48, "icon-mono", sizes=ic["sizes"])
        elif "icon" in rel:
            sc = 45 if ic["href"].lower().endswith(".svg") or "svg" in ic["type"] else 30
            add_candidate(ic["href"], "favicon", sc, "icon", sizes=ic["sizes"], type=ic["type"])
    for lg in jsonld["org_logos"]:
        add_candidate(lg, "json-ld-organization", 97, "official (schema.org Organization.logo)")
    if page.meta.get("og:image"):
        add_candidate(page.meta["og:image"], "og:image", 10, "social-preview (rarely the logo)")

    # ---- press / brand page probe
    press_pages = []
    site_host = (urlparse(final_origin).hostname or "").lower().removeprefix("www.")

    def same_site(u):
        h = (urlparse(u).hostname or "").lower().removeprefix("www.")
        return h == site_host or h.endswith("." + site_host)

    # only the brand's own pages count as its press kit; a partner's site linked as "logo" is not official
    press_links = [l for l in page.links if PRESS_HINT.search(l["href"] + " " + l["text"]) and same_site(urljoin(final_url, l["href"]))]
    probe_urls = []
    if args.press_url:
        probe_urls.append(args.press_url)
    for l in press_links[:4]:
        probe_urls.append(urljoin(final_url, l["href"]))
    for path in ("/press", "/pages/press", "/newsroom", "/media", "/pages/media", "/brand", "/pages/brand-assets", "/media-kit"):
        probe_urls.append(final_origin + path)
    probed = set()
    for pu in probe_urls:
        if pu in probed or len(press_pages) >= 3:
            continue
        probed.add(pu)
        ph, pfinal, pct = safe_fetch(pu, max_bytes=2_000_000)
        if not ph or str(pct).startswith("error") or "text/html" not in str(pct):
            continue
        if re.search(r"<title[^>]*>[^<]*(404|not found)", ph, re.I):
            continue
        if urlparse(pfinal).path.strip("/") == "" or ph == html:  # the homepage (any query string) or a soft 404 serving it
            continue
        if pu != args.press_url and not same_site(pfinal):
            continue
        # download links only (<a>, not <link rel=icon>); extension checked on the path, so fin.ai is not an .ai file
        asset_links = [a for a in re.findall(r"<a\b[^>]*?\bhref=[\"']([^\"']+)[\"']", ph, re.I)
                       if urlparse(a).path.lower().endswith((".svg", ".png", ".zip", ".eps", ".ai", ".pdf"))]
        logo_assets = [urljoin(pfinal, a) for a in asset_links
                       if LOGO_HINT.search(a) or urlparse(a).path.lower().endswith((".zip", ".eps", ".ai"))]
        press_pages.append({"url": pfinal, "requested": pu, "logo_assets": logo_assets[:20], "asset_count": len(asset_links)})
        for a in logo_assets[:6]:
            if urlparse(a).path.lower().endswith((".svg", ".png")):
                add_candidate(a, "press-page", 98, "official")

    # ---- download candidates
    candidates.sort(key=lambda c: -c["score"])
    n = 0
    for c in candidates:
        if c.get("downloaded"):
            continue
        if n >= 14:
            c["downloaded"] = False
            c["note"] = "download cap reached"
            continue
        data, _, ct = safe_fetch(c["url"], binary=True, max_bytes=8_000_000)
        if data is None or str(ct).startswith("error"):
            c["downloaded"] = False
            c["note"] = str(ct)
            continue
        fmt = guess_format(c["url"], ct, data)
        n += 1
        if fmt in ("jpg", "jpeg") and c["source"] in ("body-img", "early-home-link-img") and c["score"] > 25:
            c["score"] = 25
            c["note"] = "JPEG in page body: usually a press strip or banner, not a logo"
        fname = f"{n:02d}-{c['source']}.{fmt}"
        with open(os.path.join(raw, "logo-candidates", fname), "wb") as f:
            f.write(data)
        w, h = image_dims(data, fmt)
        c.update({"local_path": f"raw/logo-candidates/{fname}", "format": fmt, "width": w, "height": h,
                  "bytes": len(data), "downloaded": True})
    for c in candidates:
        c.pop("class_", None) if not c.get("class_") else None

    # ---- products (Shopify first, then JSON-LD, then a bounded probe of shop pages)
    products, collections = [], []
    product_source = "none"
    if platform == "shopify":
        pj, _, pct = safe_fetch(final_origin + f"/products.json?limit={args.products}", max_bytes=4_000_000)
        if pj and not str(pct).startswith("error"):
            try:
                for p in json.loads(pj).get("products", []):
                    variants = p.get("variants") or []
                    avail = [v for v in variants if v.get("available")] or variants
                    v0 = avail[0] if avail else {}
                    img = (p.get("images") or [{}])[0].get("src", "")
                    products.append({
                        "title": p.get("title"), "handle": p.get("handle"), "url": f"{final_origin}/products/{p.get('handle')}",
                        "product_type": p.get("product_type"), "vendor": p.get("vendor"), "tags": (p.get("tags") or [])[:6],
                        "price": v0.get("price"), "compare_at_price": v0.get("compare_at_price"),
                        "variant_title": v0.get("title"), "variant_count": len(variants), "image": img,
                        "options": [o.get("name") for o in (p.get("options") or [])],
                        "body_html_excerpt": re.sub(r"<[^>]+>", " ", p.get("body_html") or "")[:240].strip(),
                    })
            except Exception as e:  # noqa: BLE001
                notes.append(f"products.json parse failed: {e}")
        else:
            notes.append("products.json not reachable (store may block it).")
        if products:
            product_source = "shopify-products.json"
        cj, _, cct = safe_fetch(final_origin + "/collections.json?limit=12", max_bytes=1_000_000)
        if cj and not str(cct).startswith("error"):
            try:
                collections = [{"title": c.get("title"), "handle": c.get("handle")} for c in json.loads(cj).get("collections", [])]
            except Exception:  # noqa: BLE001
                pass

    if not products:
        products = jsonld["products"][:args.products]
        if products:
            product_source = "json-ld-homepage"
    if not products:
        probe = []
        req = args.url if re.match(r"^https?://", args.url, re.I) else "https://" + args.url
        req_path = urlparse(req).path
        if req_path and req_path != "/":
            probe.append(req.split("#")[0])
        for l in page.links:
            t = (l["text"] or "").lower()
            h = l["href"] or ""
            if not h or h.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            if re.search(r"(shop|product|collection|catalog|store|all-|/c/|/s/|category|bundles)", h + " " + t, re.I) and \
               not re.search(r"(blog|about|support|help|account|login|cart|checkout|faq|career|press|return|policy|gift-?card|locator|app)", h + " " + t, re.I):
                absu = urljoin(final_url, h)
                if absu.startswith(final_origin) and absu not in probe and absu.rstrip("/") != final_origin:
                    probe.append(absu)
        seen_titles = set()
        for pu in probe[:5]:
            ph, pfinal, pct = safe_fetch(pu, max_bytes=3_000_000)
            if not ph or str(pct).startswith("error"):
                continue
            found = parse_jsonld(ph)["products"]
            src_label = "json-ld:" + pfinal
            if not found:
                found = json_walk_products(ph, pfinal)
                src_label = "embedded-json:" + pfinal
            if not found:
                found = product_cards(ph, pfinal)
                src_label = "card-heuristic:" + pfinal
            for pr in found:
                key = (pr.get("title") or "").lower()
                if key and key not in seen_titles:
                    seen_titles.add(key)
                    pr["provenance"] = src_label
                    products.append(pr)
            if len(products) >= args.products:
                break
        if len(products) < 4:  # thin results: read product pages linked from the homepage directly
            for pr in pdp_link_probe(page.links, final_origin):
                key = (pr.get("title") or "").lower()
                if key and key not in seen_titles:
                    seen_titles.add(key)
                    products.append(pr)
        if len(products) < 4:
            for pr in json_walk_products(html, final_url):
                key = (pr.get("title") or "").lower()
                if key and key not in seen_titles:
                    seen_titles.add(key)
                    pr["provenance"] = "embedded-json:homepage"
                    products.append(pr)
        products = products[:args.products]
        enriched = 0
        for pr in products:
            if pr.get("url"):
                pr["url"] = urljoin(final_origin + "/", pr["url"])
            if pr.get("image"):
                pr["image"] = urljoin(final_origin + "/", pr["image"])
            if pr.get("price") or enriched >= 8 or not (pr.get("url") or "").startswith(final_origin):
                continue
            pp, _, ppct = safe_fetch(pr["url"], max_bytes=2_500_000)
            enriched += 1
            if not pp or str(ppct).startswith("error"):
                continue
            found = parse_jsonld(pp)["products"]
            found = [f for f in found if f.get("price")]
            if found:
                f0 = found[0]
                pr["price"], pr["currency"] = f0.get("price"), f0.get("currency")
                for k in ("brand", "sku", "rating", "review_count", "body_html_excerpt", "product_type"):
                    if f0.get(k) and not pr.get(k):
                        pr[k] = f0[k]
                pr["provenance"] = (pr.get("provenance") or "") + " + price from product page JSON-LD"
            elif re.search(r"og:type[\"']\s+content=[\"']product|\"@type\"\s*:\s*\"Product\"|itemtype=[\"'][^\"']*schema.org/Product", pp, re.I):
                m_price = PRICE_RE.search(re.sub(r"<[^>]+>", " ", pp[:200000]))
                if m_price:
                    pr["price"] = m_price.group(0).strip()
                    pr["provenance"] = (pr.get("provenance") or "") + " + first price on product page (verify)"
        if products:
            product_source = "probe (see each item's provenance)"
            notes.append(f"Products came from page probing ({len(probe[:4])} pages), not a catalog API. Verify titles and prices against the site before use.")

    # ---- copy / voice
    nav_labels, seen_nav = [], set()
    for l in page.links:
        t = l["text"] or l["aria"]
        if (l["in_nav"] or l["in_header"]) and t and 1 < len(t) <= 32 and t.lower() not in seen_nav:
            seen_nav.add(t.lower())
            nav_labels.append({"label": t, "href": l["href"]})
    footer_labels = []
    seen_f = set()
    for l in page.links:
        t = l["text"]
        if l["in_footer"] and t and 1 < len(t) <= 40 and t.lower() not in seen_f:
            seen_f.add(t.lower())
            footer_labels.append(t)
    headlines, seen_h = [], set()
    for h in page.headings:
        t = h["text"]
        if t and len(t) <= 140 and t.lower() not in seen_h:
            seen_h.add(t.lower())
            headlines.append({"tag": h["tag"], "text": t})
    for d in page.display_blocks:
        t = d["text"]
        if t.lower() not in seen_h and not re.fullmatch(r"[\W\d]+", t):
            seen_h.add(t.lower())
            headlines.append({"tag": f"{d['tag']}.{d['class'].split(' ')[0][:30]}", "text": t})
    for e in large_type_out:
        t = e.get("text")
        if t and t.lower() not in seen_h and not re.fullmatch(r"[\W\d]+", t):
            seen_h.add(t.lower())
            headlines.append({"tag": f"{e.get('tag','div')}@{int(max(e['font_sizes']))}px", "text": t})
    cta_labels, seen_c = [], set()
    button_classes = []
    for b in page.buttons:
        t = b["text"]
        if t and len(t) <= 40 and t.lower() not in seen_c:
            seen_c.add(t.lower())
            cta_labels.append(t)
            util = [c for c in b["class"].split() if re.match(r"^(bg-|text-|border|rounded|px-|py-|font-|uppercase|tracking-|shadow|h-\d|w-\d|hover:bg-|hover:text-)", c)]
            if util:
                button_classes.append({"label": t, "tag": b["tag"], "classes": " ".join(util)[:200]})
    for b in buttons:  # buttons found through CSS rules (Builder.io blocks, utility-styled anchors)
        t = b.get("text")
        if t and len(t) <= 40 and t.lower() not in seen_c:
            seen_c.add(t.lower())
            cta_labels.append(t)

    # ---- assemble
    # Tailwind v4 ships its stock palette as --color-<hue>-<step> variables; those hexes are framework defaults
    # unless the site also names the same hex itself.
    TW_STOCK = re.compile(r"^--color-(?:slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|"
                          r"blue|indigo|violet|purple|fuchsia|pink|rose|mauve|olive|mist|taupe)-\d{2,3}$")
    own_hexes = {cv["hex"] for cv in color_vars if not TW_STOCK.match(cv["name"]) and not cv["name"].startswith("--tw-")}
    stock_hexes = set()
    for cv in color_vars:
        if TW_STOCK.match(cv["name"]):
            cv["framework_default"] = True
            if cv["hex"] not in own_hexes:
                stock_hexes.add(cv["hex"])
    framework_hexes = FRAMEWORK_DEFAULTS | stock_hexes
    top_colors = [{"hex": hx, "weight": cnt, "example": color_examples.get(hx), "framework_default": hx in framework_hexes}
                  for hx, cnt in color_freq.most_common(40)]
    for np_ in named_palette:
        if np_["hex"] in framework_hexes:
            np_["framework_default"] = True
    fams_out = [{"family": fam, "declarations": cnt, "is_system_fallback": fam.lower() in SYSTEM_FALLBACKS,
                 "is_icon_font": ICON_FONT.search(fam) is not None,
                 "examples": family_sources[fam][:3]} for fam, cnt in families.most_common(15)]

    if _chrome_state["used"]:
        notes.append(f"Bot challenge on {_chrome_state['used']} request(s); fetched through headless Chrome instead.")
    result = {
        "pack_version": "1.0",
        "extractor_version": EXTRACTOR_VERSION,
        "extracted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "elapsed_seconds": round(time.time() - t0, 1),
        "tier": "css-parse",
        "source": {"requested": args.url, "origin": final_origin, "final_url": final_url, "platform": platform,
                   "stylesheets_fetched": fetched, "inline_style_blocks": len(page.style_blocks), "rules_parsed": len(rules)},
        "meta": {"title": re.sub(r"\s+", " ", page.title).strip(), "description": page.meta.get("description", ""),
                 "og_site_name": page.meta.get("og:site_name", ""), "og_title": page.meta.get("og:title", ""),
                 "og_image": page.meta.get("og:image", ""), "theme_color": theme_color, "lang": page.html_attrs.get("lang", "")},
        "logos": {"candidates": candidates, "rejected": rejected[:40], "press_pages": press_pages,
                  "rules": "Selection rules live in references/selection-rules.md. Score is a hint, not a decision."},
        "colors": {"variables": color_vars[:200], "theme_color": theme_color, "buttons": buttons[:60], "links": links_css[:20],
                   "body": body_css[:10], "frequency": top_colors,
                   "pool": sorted(color_freq.keys())},
        "typography": {"font_faces": font_faces[:40], "families": fams_out, "services": font_services,
                       "headings": headings_css[:30], "body": body_css[:10], "large_type": large_type_out[:24],
                       "text_transforms": transforms.most_common(5),
                       "pool": sorted({f["family"] for f in fams_out if not f["is_icon_font"]} |
                                      {ff["family"] for ff in font_faces if ff["family"] and not ICON_FONT.search(ff["family"])} |
                                      {fam for svc in font_services for fam in svc["families"]})},
        "shape": {"border_radius_frequency": radii.most_common(12), "box_shadow_frequency": shadows.most_common(6)},
        "products": products, "product_source": product_source, "collections": collections,
        "named_palette": named_palette[:60], "jsonld_types": jsonld["types"],
        "copy": {"headlines": headlines[:48], "nav": nav_labels[:16], "cta_labels": cta_labels[:12], "footer_links": footer_labels[:24],
                 "button_classes": button_classes[:12],
                 "tagline_candidates": [page.meta.get("og:description", ""), page.meta.get("description", "")]},
        "notes": notes,
    }
    with open(os.path.join(out, "candidates.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    # ---- console summary (short: this is what the model reads)
    print(f"origin: {final_origin}   platform: {platform}   css files: {fetched}   rules: {len(rules)}   {round(time.time()-t0,1)}s")
    print(f"title: {result['meta']['title'][:90]}")
    print(f"theme-color: {theme_color}   color vars: {len(color_vars)}   button rules: {len(buttons)}   font-faces: {len(font_faces)}")
    print("top colors: " + ", ".join(f"{c['hex']}({c['weight']})" for c in top_colors[:10]))
    print("families: " + ", ".join(f"{f['family']}({f['declarations']})" for f in fams_out[:6]))
    print(f"logo candidates: {len(candidates)} (rejected {len(rejected)})   press pages: {len(press_pages)}")
    for c in candidates[:8]:
        print(f"  [{c['score']:>3}] {c['source']:<22} {c.get('format','?'):<4} {str(c.get('width'))+'x'+str(c.get('height')):<10} {c.get('local_path') or c['url'][:70]}")
    print(f"named palette classes: {len(named_palette)}   json-ld types: {jsonld['types'][:8]}")
    print(f"large type classes: {len(large_type_out)}   e.g. " + "; ".join(f"{int(max(e['font_sizes']))}px '{(e.get('text') or '')[:30]}'" for e in large_type_out[:3]))
    print(f"products: {len(products)} via {product_source}   collections: {len(collections)}   headlines: {len(headlines)}   nav: {len(nav_labels)}   ctas: {len(cta_labels)}")
    for n_ in notes:
        print("note: " + n_)
    print(f"wrote {os.path.join(out, 'candidates.json')}")
    if args.mirror:
        missing_path = os.path.join(out, "missing-urls.json")
        with open(missing_path, "w", encoding="utf-8") as f:
            json.dump(_mirror["missing"], f, indent=1)
        print(f"mirror: {_mirror['hits']} urls read, {len(_mirror['missing'])} missing -> {missing_path}"
              + ("   (run capture.js again with these URLs, then rerun with every mirror file)" if _mirror["missing"] else ""))
        if _mirror["unverified"]:
            print(f"mirror: UNVERIFIED {', '.join(_mirror['unverified'])} (not untouched capture.js output; recapture before building)")


if __name__ == "__main__":
    main()
