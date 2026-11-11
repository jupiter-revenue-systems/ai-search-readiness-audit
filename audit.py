#!/usr/bin/env python3
"""ai-search-readiness-audit: check whether a website is ready for AI search.

Covers what Google AI Overviews and AI Mode, ChatGPT search, Perplexity,
Claude, Gemini and Copilot need to find, read and cite a site.

Usage:
  python3 audit.py https://example.com
  python3 audit.py https://example.com --pages 10
  python3 audit.py https://example.com --json > report.json
  python3 audit.py https://example.com --markdown > report.md

Read-only: it only sends GET requests. Standard library only, Python 3.9+.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

__version__ = "1.0.0"
REPO_URL = "https://github.com/jupiter-revenue-systems/ai-search-readiness-audit"
USER_AGENT = "ai-search-readiness-audit/%s (+%s)" % (__version__, REPO_URL)

MAX_BYTES = 5 * 1024 * 1024  # never read more than 5 MB from one URL
MAX_PAGES = 50  # upper limit for --pages, to stay polite
MAX_CHILD_SITEMAPS = 5
WORKERS = 4

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
FACTOR = {PASS: 1.0, WARN: 0.5, FAIL: 0.0}

# (user-agent name, what it powers, role)
CRAWLERS = [
    ("Googlebot", "Google Search, AI Overviews, AI Mode", "search"),
    ("Bingbot", "Bing, Copilot, ChatGPT partner results", "search"),
    ("OAI-SearchBot", "ChatGPT search index", "answer"),
    ("ChatGPT-User", "ChatGPT live page fetches", "answer"),
    ("Claude-SearchBot", "Claude search index", "answer"),
    ("PerplexityBot", "Perplexity index", "answer"),
    ("GPTBot", "OpenAI model training", "training"),
    ("ClaudeBot", "Anthropic model training", "training"),
    ("Google-Extended", "Gemini training and grounding", "training"),
    ("Applebot-Extended", "Apple Intelligence training", "training"),
    ("CCBot", "Common Crawl, used by many models", "training"),
]

KEY_SCHEMA_TYPES = ["Organization", "Person", "WebSite", "Article", "FAQPage", "Product", "BreadcrumbList"]
ORG_TYPES = {
    "Organization", "Corporation", "LocalBusiness", "ProfessionalService", "OnlineBusiness",
    "OnlineStore", "NGO", "EducationalOrganization", "GovernmentOrganization", "MedicalOrganization",
    "NewsMediaOrganization", "SportsOrganization", "Store", "Restaurant", "MedicalBusiness",
    "LegalService", "FinancialService", "HomeAndConstructionBusiness", "HealthAndBeautyBusiness",
}
ARTICLE_TYPES = {"Article", "BlogPosting", "NewsArticle", "TechArticle", "Report", "ScholarlyArticle",
                 "SocialMediaPosting", "AnalysisNewsArticle", "OpinionNewsArticle"}
PRODUCT_TYPES = {"Product", "ProductGroup", "SoftwareApplication", "WebApplication", "MobileApplication"}

CATEGORIES = ["Crawl access", "Indexing", "Content", "Structured data", "Security and speed"]
CHECK_ORDER = [
    "robots_txt", "crawlers_search", "crawlers_answer", "crawlers_training", "sitemap", "llms_txt",
    "noindex", "canonical", "title", "description", "h1", "text_in_html", "lang", "open_graph",
    "jsonld", "entity_schema", "website_schema", "same_as", "https", "response_time", "html_weight",
]


# --------------------------------------------------------------------------- fetching

class Response:
    """Result of one GET. Never raises; errors land in .error with status 0."""

    def __init__(self, url: str, status: int = 0, final_url: Optional[str] = None,
                 headers: Optional[Dict[str, str]] = None, body: bytes = b"",
                 elapsed: float = 0.0, error: str = "", transfer_bytes: Optional[int] = None,
                 truncated: bool = False):
        self.url = url
        self.status = status
        self.final_url = final_url or url
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.body = body
        self.elapsed = elapsed
        self.error = error
        self.transfer_bytes = len(body) if transfer_bytes is None else transfer_bytes
        self.truncated = truncated

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def header(self, name: str) -> str:
        return self.headers.get(name.lower(), "")

    @property
    def content_type(self) -> str:
        return self.header("content-type").split(";")[0].strip().lower()

    @property
    def text(self) -> str:
        m = re.search(r"charset=[\"']?([\w.:-]+)", self.header("content-type"), re.I)
        enc = m.group(1) if m else "utf-8"
        try:
            return self.body.decode(enc, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

    def looks_like_html(self) -> bool:
        head = self.body[:512].lstrip().lower()
        return self.content_type in ("text/html", "application/xhtml+xml") or head.startswith(b"<!doctype html") \
            or head.startswith(b"<html")


def _describe_error(exc: BaseException) -> str:
    reason = getattr(exc, "reason", None)
    if reason is not None:
        exc = reason if isinstance(reason, BaseException) else exc
    name = type(exc).__name__
    msg = str(exc).strip() or name
    if "timed out" in msg.lower() or name in ("timeout", "TimeoutError"):
        return "timed out"
    return msg[:160]


def _maybe_gunzip(raw: bytes, encoding: str) -> bytes:
    if "gzip" in encoding.lower() or raw[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(raw)[:MAX_BYTES]
        except Exception:
            return raw
    return raw


class _RedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow 308 redirects too (Python 3.9 and 3.10 don't)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, 307 if code == 308 else code, msg, headers, newurl)

    http_error_308 = urllib.request.HTTPRedirectHandler.http_error_302


_OPENER = urllib.request.build_opener(_RedirectHandler)


def fetch(url: str, timeout: float = 15.0) -> Response:
    """GET a URL politely. Follows redirects, caps size, never raises."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.5",
        "Accept-Encoding": "gzip",
    }
    start = time.monotonic()
    try:
        req = urllib.request.Request(url, headers=headers)
        resp = _OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        elapsed = time.monotonic() - start
        raw = b""
        try:
            raw = e.read(MAX_BYTES) or b""
        except Exception:
            pass
        hdrs = dict(e.headers.items()) if e.headers is not None else {}
        body = _maybe_gunzip(raw, hdrs.get("Content-Encoding", "") or hdrs.get("content-encoding", ""))
        return Response(url, e.code, e.geturl() or url, hdrs, body, elapsed, transfer_bytes=len(raw))
    except Exception as e:  # DNS, TLS, refused, timeout, bad URL
        return Response(url, 0, url, {}, b"", time.monotonic() - start, error=_describe_error(e))
    try:
        with resp:
            elapsed = time.monotonic() - start
            hdrs = dict(resp.headers.items())
            raw = resp.read(MAX_BYTES + 1)
            truncated = len(raw) > MAX_BYTES
            raw = raw[:MAX_BYTES]
            body = _maybe_gunzip(raw, resp.headers.get("Content-Encoding", "") or "")
            return Response(url, resp.status, resp.geturl(), hdrs, body, elapsed,
                            transfer_bytes=len(raw), truncated=truncated)
    except Exception as e:
        return Response(url, 0, url, {}, b"", time.monotonic() - start, error=_describe_error(e))


Fetcher = Callable[[str, float], Response]


# --------------------------------------------------------------------------- robots.txt

def parse_robots(text: str) -> Tuple[List[dict], List[str]]:
    """Parse robots.txt into groups [{agents: [...], rules: [(allow, path)]}] and sitemap URLs."""
    groups: List[dict] = []
    sitemaps: List[str] = []
    current: Optional[dict] = None
    last_was_agent = False
    for raw in text.replace("\ufeff", "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key, val = key.strip().lower(), val.strip()
        if key in ("user-agent", "useragent", "user agent"):
            if current is None or not last_was_agent:
                current = {"agents": [], "rules": []}
                groups.append(current)
            current["agents"].append(val.lower())
            last_was_agent = True
        elif key in ("allow", "disallow"):
            last_was_agent = False
            if current is not None:
                current["rules"].append((key == "allow", val))
        elif key == "sitemap":
            if val:
                sitemaps.append(val)
        else:
            last_was_agent = False
    return groups, sitemaps


def _agent_matches(agent_line: str, name: str) -> bool:
    return agent_line.split("/")[0].strip() == name.lower()


def rules_for(groups: List[dict], name: str) -> Tuple[List[Tuple[bool, str]], str]:
    """Rules that apply to a crawler: its own named groups, else the * group."""
    named = [g for g in groups if any(_agent_matches(a, name) for a in g["agents"])]
    if named:
        chosen, source = named, "named"
    else:
        chosen = [g for g in groups if "*" in g["agents"]]
        source = "*" if chosen else "none"
    rules: List[Tuple[bool, str]] = []
    for g in chosen:
        rules.extend(g["rules"])
    return rules, source


def _rule_matches(pattern: str, path: str) -> bool:
    regex = "".join(".*" if c == "*" else re.escape(c) for c in pattern)
    if regex.endswith(r"\$"):
        regex = regex[:-2] + "$"
    return re.match(regex, path) is not None


def path_allowed(rules: List[Tuple[bool, str]], path: str) -> bool:
    """Longest matching rule wins; Allow wins a tie; no match means allowed."""
    best: Optional[Tuple[int, bool]] = None
    for allow, pattern in rules:
        if not pattern:
            continue
        if _rule_matches(pattern, path):
            length = len(pattern)
            if best is None or length > best[0] or (length == best[0] and allow):
                best = (length, allow)
    return True if best is None else best[1]


def crawler_status(groups: List[dict], name: str, path: str = "/") -> Dict[str, str]:
    rules, source = rules_for(groups, name)
    if not path_allowed(rules, path or "/"):
        status = "blocked"
    elif any((not allow) and pattern for allow, pattern in rules):
        status = "partial"
    else:
        status = "allowed"
    return {"status": status, "group": source}


# --------------------------------------------------------------------------- sitemap

def parse_sitemap(data: bytes) -> Tuple[str, List[str]]:
    """Return ("urlset" | "sitemapindex", [loc, ...]). Raises ValueError if not a sitemap."""
    data = _maybe_gunzip(data, "").lstrip(b"\xef\xbb\xbf \t\r\n")
    if not data:
        raise ValueError("empty file")
    if b"<!ENTITY" in data or b"<!DOCTYPE" in data[:2048].upper():
        raise ValueError("DOCTYPE or ENTITY declarations are not allowed in a sitemap")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ValueError("not valid XML (%s)" % e)
    kind = root.tag.rsplit("}", 1)[-1].lower()
    if kind not in ("urlset", "sitemapindex"):
        raise ValueError("not a sitemap (root element <%s>)" % kind)
    locs = []
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1].lower() == "loc" and el.text and el.text.strip():
            locs.append(el.text.strip())
    return kind, locs


# --------------------------------------------------------------------------- HTML

class PageParser(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lang: Optional[str] = None
        self._html_seen = False
        self.title_parts: List[str] = []
        self._in_title = False
        self._title_done = False
        self.metas: List[Dict[str, str]] = []
        self.links: List[Dict[str, str]] = []
        self.h1_count = 0
        self.jsonld_blocks: List[str] = []
        self._jsonld: Optional[List[str]] = None
        self._skip_depth = 0
        self.text_parts: List[str] = []

    def _start(self, tag: str, attrs, self_closing: bool) -> None:
        a = {k.lower(): (v or "") for k, v in attrs if k}
        if tag == "html" and not self._html_seen:
            self._html_seen = True
            self.lang = a.get("lang", "").strip() or a.get("xml:lang", "").strip() or None
        elif tag == "meta":
            self.metas.append(a)
        elif tag == "link":
            self.links.append(a)
        elif tag == "h1":
            self.h1_count += 1
        elif tag == "title" and self._skip_depth == 0 and not self._title_done and not self_closing:
            self._in_title = True
        if tag == "script" and "ld+json" in a.get("type", "").lower() and not self_closing:
            self._jsonld = []
        if tag in self.SKIP and not self_closing:
            self._skip_depth += 1

    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs, False)

    def handle_startendtag(self, tag, attrs):
        self._start(tag, attrs, True)

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag == "script" and self._jsonld is not None:
            self.jsonld_blocks.append("".join(self._jsonld))
            self._jsonld = None
        if tag == "title" and self._in_title:
            self._in_title = False
            self._title_done = True

    def handle_data(self, data):
        if self._jsonld is not None:
            self._jsonld.append(data)
        elif self._in_title:
            self.title_parts.append(data)
        elif self._skip_depth == 0:
            self.text_parts.append(data)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _short_type(t: str) -> str:
    return re.split(r"[/:#]", str(t).strip())[-1]


def analyze_jsonld(blocks: List[str]) -> dict:
    types: List[str] = []
    same_as: List[str] = []
    errors: List[str] = []
    valid = 0

    def walk(node, depth=0):
        if depth > 30:
            return
        if isinstance(node, dict):
            t = node.get("@type")
            for item in (t if isinstance(t, list) else [t]):
                if isinstance(item, str) and item.strip():
                    short = _short_type(item)
                    if short not in types:
                        types.append(short)
            sa = node.get("sameAs")
            for item in (sa if isinstance(sa, list) else [sa]):
                if isinstance(item, str) and item.strip().startswith("http") and item.strip() not in same_as:
                    same_as.append(item.strip())
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                walk(v, depth + 1)

    for block in blocks:
        s = block.strip()
        s = re.sub(r"^(<!--|<!\[CDATA\[)", "", s).strip()
        s = re.sub(r"(-->|\]\]>)$", "", s).strip()
        if not s:
            continue
        try:
            data = json.loads(s)
        except ValueError as e:
            errors.append("invalid JSON (%s)" % str(e)[:70])
            continue
        valid += 1
        if "{{" in s:
            errors.append("unfilled {{placeholder}} text")
        walk(data)

    found = {
        "Organization": any(t in ORG_TYPES or t.endswith("Organization") for t in types),
        "Person": "Person" in types,
        "WebSite": "WebSite" in types,
        "Article": any(t in ARTICLE_TYPES for t in types),
        "FAQPage": "FAQPage" in types,
        "Product": any(t in PRODUCT_TYPES for t in types),
        "BreadcrumbList": "BreadcrumbList" in types,
    }
    return {"blocks": len([b for b in blocks if b.strip()]), "valid": valid, "types": types,
            "same_as": same_as, "errors": errors, "found": found}


def analyze_html(html_text: str, base_url: str) -> dict:
    p = PageParser()
    try:
        p.feed(html_text)
        p.close()
    except Exception:
        pass  # keep whatever was parsed before the bad markup
    description = ""
    robots_directives: List[str] = []
    og: Dict[str, str] = {}
    for m in p.metas:
        name = m.get("name", "").strip().lower()
        prop = m.get("property", "").strip().lower() or name
        content = _clean(m.get("content", ""))
        if name == "description" and not description:
            description = content
        if name in ("robots", "googlebot", "bingbot"):
            robots_directives.append(content.lower())
        if prop.startswith("og:") and prop not in og and content:
            og[prop] = content
    canonicals = []
    for link in p.links:
        rel = link.get("rel", "").lower().split()
        href = link.get("href", "").strip()
        if "canonical" in rel and href:
            canonicals.append(urljoin(base_url, href))
    text = _clean(" ".join(p.text_parts))
    return {
        "title": _clean("".join(p.title_parts)),
        "description": description,
        "canonicals": canonicals,
        "robots_meta": robots_directives,
        "og": og,
        "lang": p.lang,
        "h1_count": p.h1_count,
        "word_count": len(re.findall(r"\w+", text)),
        "jsonld": analyze_jsonld(p.jsonld_blocks),
    }


# --------------------------------------------------------------------------- checks

def make_check(cid: str, name: str, category: str, weight: int, status: str, detail: str, fix: str) -> dict:
    return {"id": cid, "name": name, "category": category, "weight": weight, "status": status,
            "detail": detail, "fix": fix if status != PASS else ""}


def _norm_url(u: str) -> str:
    p = urlparse(u)
    path = p.path.rstrip("/") or "/"
    q = ("?" + p.query) if p.query else ""
    return "%s://%s%s%s" % (p.scheme.lower(), p.netloc.lower(), path, q)


def _kb(n: int) -> str:
    if n < 1024:
        return "%d bytes" % n
    return "%.0f KB" % (n / 1024.0) if n < 1024 * 1024 else "%.1f MB" % (n / 1048576.0)


def page_checks(resp: Response) -> Tuple[List[dict], Optional[dict]]:
    """Checks that run on every audited page. Returns (checks, parsed page data)."""
    loaded = resp.ok and resp.looks_like_html() and bool(resp.body)
    if not loaded:
        if resp.status == 0:
            why = "Page didn't load (%s)" % (resp.error or "no response")
        elif not resp.ok:
            why = "Page didn't load (HTTP %d)" % resp.status
        else:
            why = "Not an HTML page (%s)" % (resp.content_type or "unknown type")
        fix = "Make sure the URL returns HTTP 200 with HTML to anonymous visitors and bots."
        specs = PAGE_CHECK_SPECS
        return [make_check(cid, name, cat, w, FAIL, why, fix) for cid, name, cat, w in specs], None

    d = analyze_html(resp.text, resp.final_url)
    checks: List[dict] = []
    add = checks.append

    # Indexing: noindex / nosnippet
    xrt = resp.header("x-robots-tag").lower()
    directives = " ".join(d["robots_meta"] + [xrt])
    if re.search(r"\b(noindex|none)\b", directives):
        where = "X-Robots-Tag header" if re.search(r"\b(noindex|none)\b", xrt) else "meta robots tag"
        add(make_check("noindex", "Indexable (no noindex)", "Indexing", 12, FAIL,
                       "noindex found in the %s" % where,
                       "Remove noindex from this page so search and AI engines can index and cite it."))
    elif re.search(r"\bnosnippet\b|max-snippet\s*:\s*0\b", directives):
        add(make_check("noindex", "Indexable (no noindex)", "Indexing", 12, WARN,
                       "Indexable, but nosnippet/max-snippet:0 is set",
                       "Remove nosnippet so Google can quote this page in AI Overviews and AI Mode."))
    else:
        add(make_check("noindex", "Indexable (no noindex)", "Indexing", 12, PASS, "No noindex directive", ""))

    # Canonical
    cans = list(dict.fromkeys(d["canonicals"]))
    if not cans:
        add(make_check("canonical", "Canonical URL", "Indexing", 6, WARN, "No canonical link",
                       'Add <link rel="canonical" href="..."> pointing to this page\'s preferred URL.'))
    elif len(cans) > 1:
        add(make_check("canonical", "Canonical URL", "Indexing", 6, WARN,
                       "%d different canonicals (engines may ignore all of them)" % len(cans),
                       "Keep exactly one canonical link per page."))
    elif urlparse(cans[0]).netloc.lower() != urlparse(resp.final_url).netloc.lower():
        add(make_check("canonical", "Canonical URL", "Indexing", 6, WARN, "Points to another host: %s" % cans[0],
                       "Point the canonical at this site's own host unless the page is a deliberate copy."))
    elif _norm_url(cans[0]) != _norm_url(resp.final_url):
        add(make_check("canonical", "Canonical URL", "Indexing", 6, WARN, "Points to a different URL: %s" % cans[0],
                       "Use a self-referencing canonical unless this page is a duplicate of that URL."))
    else:
        add(make_check("canonical", "Canonical URL", "Indexing", 6, PASS, "Self-referencing", ""))

    # Title
    tl = len(d["title"])
    if tl == 0:
        add(make_check("title", "Meta title", "Content", 6, FAIL, "Missing <title>",
                       "Add a unique 30 to 60 character title that names the topic and the brand."))
    elif 30 <= tl <= 60:
        add(make_check("title", "Meta title", "Content", 6, PASS, "%d chars" % tl, ""))
    else:
        add(make_check("title", "Meta title", "Content", 6, WARN,
                       "%d chars (%s)" % (tl, "too short" if tl < 30 else "may be cut off"),
                       "Rewrite the title to 30 to 60 characters: topic first, brand last."))

    # Description
    dl = len(d["description"])
    if dl == 0:
        add(make_check("description", "Meta description", "Content", 5, FAIL, "Missing meta description",
                       "Add a 70 to 160 character description that answers what the page is and who it's for."))
    elif 70 <= dl <= 160:
        add(make_check("description", "Meta description", "Content", 5, PASS, "%d chars" % dl, ""))
    else:
        add(make_check("description", "Meta description", "Content", 5, WARN,
                       "%d chars (%s)" % (dl, "too short" if dl < 70 else "may be cut off"),
                       "Rewrite the description to 70 to 160 characters."))

    # H1
    h1 = d["h1_count"]
    if h1 == 1:
        add(make_check("h1", "Single H1", "Content", 4, PASS, "1 H1", ""))
    elif h1 == 0:
        add(make_check("h1", "Single H1", "Content", 4, FAIL, "No H1",
                       "Add one H1 that states the page topic in plain words."))
    else:
        add(make_check("h1", "Single H1", "Content", 4, WARN, "%d H1 tags" % h1,
                       "Keep one H1 per page and turn the others into H2s."))

    # Text in the raw HTML (most AI crawlers don't run JavaScript)
    wc = d["word_count"]
    if wc >= 150:
        add(make_check("text_in_html", "Text in raw HTML", "Content", 6, PASS, "%d words without JavaScript" % wc, ""))
    elif wc >= 50:
        add(make_check("text_in_html", "Text in raw HTML", "Content", 6, WARN, "Only %d words without JavaScript" % wc,
                       "Server-render the main content; most AI crawlers read the HTML and don't run JavaScript."))
    else:
        add(make_check("text_in_html", "Text in raw HTML", "Content", 6, FAIL, "Only %d words without JavaScript" % wc,
                       "Server-render the main content; most AI crawlers read the HTML and don't run JavaScript."))

    # lang
    if d["lang"]:
        add(make_check("lang", "Language attribute", "Content", 3, PASS, 'lang="%s"' % d["lang"], ""))
    else:
        add(make_check("lang", "Language attribute", "Content", 3, WARN, "No lang on <html>",
                       'Add lang to the html tag, for example <html lang="en">.'))

    # Open Graph
    need = ["og:title", "og:description", "og:image"]
    have = [k for k in need if d["og"].get(k)]
    missing = [k for k in need if k not in have]
    if not missing:
        add(make_check("open_graph", "Open Graph tags", "Content", 4, PASS, "og:title, og:description, og:image", ""))
    elif have:
        add(make_check("open_graph", "Open Graph tags", "Content", 4, WARN, "Missing " + ", ".join(missing),
                       "Add the missing Open Graph tags so shared links and AI previews show a title, summary and image."))
    else:
        add(make_check("open_graph", "Open Graph tags", "Content", 4, FAIL, "No Open Graph tags",
                       "Add og:title, og:description and og:image meta tags."))

    # JSON-LD
    j = d["jsonld"]
    types_txt = ", ".join(j["types"][:8]) + (" ..." if len(j["types"]) > 8 else "")
    if j["blocks"] == 0:
        add(make_check("jsonld", "JSON-LD structured data", "Structured data", 7, FAIL, "No JSON-LD found",
                       "Add a JSON-LD block (Organization or Person, plus WebSite) so engines know who's behind the site."))
    elif j["valid"] == 0:
        add(make_check("jsonld", "JSON-LD structured data", "Structured data", 7, FAIL,
                       "JSON-LD present but broken: " + "; ".join(j["errors"][:2]),
                       "Fix the JSON syntax and test it at validator.schema.org."))
    elif j["errors"]:
        add(make_check("jsonld", "JSON-LD structured data", "Structured data", 7, WARN,
                       "Types: %s. Problems: %s" % (types_txt or "none", "; ".join(j["errors"][:2])),
                       "Fix the broken block and test it at validator.schema.org."))
    else:
        add(make_check("jsonld", "JSON-LD structured data", "Structured data", 7, PASS,
                       "%d block%s; types: %s" % (j["valid"], "" if j["valid"] == 1 else "s", types_txt or "none"), ""))

    # Speed and weight
    ms = int(round(resp.elapsed * 1000))
    if resp.elapsed <= 0.8:
        add(make_check("response_time", "Server response time", "Security and speed", 5, PASS, "%d ms" % ms, ""))
    elif resp.elapsed <= 1.8:
        add(make_check("response_time", "Server response time", "Security and speed", 5, WARN, "%d ms" % ms,
                       "Aim for under 800 ms: use a CDN, caching and fewer redirects."))
    else:
        add(make_check("response_time", "Server response time", "Security and speed", 5, FAIL, "%d ms" % ms,
                       "Aim for under 800 ms: use a CDN, caching and fewer redirects."))

    size = len(resp.body)
    size_txt = _kb(size) + (" (%s transferred)" % _kb(resp.transfer_bytes) if resp.transfer_bytes != size else "")
    if resp.truncated:
        size_txt = "over %s" % _kb(MAX_BYTES)
    if size <= 500 * 1024 and not resp.truncated:
        add(make_check("html_weight", "HTML page weight", "Security and speed", 3, PASS, size_txt, ""))
    elif size <= 2 * 1024 * 1024 and not resp.truncated:
        add(make_check("html_weight", "HTML page weight", "Security and speed", 3, WARN, size_txt,
                       "Trim inline scripts, styles and data so the HTML stays under 500 KB."))
    else:
        add(make_check("html_weight", "HTML page weight", "Security and speed", 3, FAIL, size_txt,
                       "Cut the HTML well below 2 MB; crawlers may stop reading long files."))
    return checks, d


# (id, name, category, weight) for page checks, used when a page fails to load
PAGE_CHECK_SPECS = [
    ("noindex", "Indexable (no noindex)", "Indexing", 12),
    ("canonical", "Canonical URL", "Indexing", 6),
    ("title", "Meta title", "Content", 6),
    ("description", "Meta description", "Content", 5),
    ("h1", "Single H1", "Content", 4),
    ("text_in_html", "Text in raw HTML", "Content", 6),
    ("lang", "Language attribute", "Content", 3),
    ("open_graph", "Open Graph tags", "Content", 4),
    ("jsonld", "JSON-LD structured data", "Structured data", 7),
    ("response_time", "Server response time", "Security and speed", 5),
    ("html_weight", "HTML page weight", "Security and speed", 3),
]


def entity_checks(page: Optional[dict]) -> List[dict]:
    """Site identity checks, read from the start page."""
    out = []
    if page is None:
        why = "Start page didn't load"
        out.append(make_check("entity_schema", "Organization or Person schema", "Structured data", 5, FAIL, why,
                              "Add Organization (or Person) JSON-LD to the home page."))
        out.append(make_check("website_schema", "WebSite schema", "Structured data", 2, FAIL, why,
                              "Add WebSite JSON-LD with the site name and URL to the home page."))
        out.append(make_check("same_as", "sameAs profile links", "Structured data", 3, FAIL, why,
                              "List your official profiles in a sameAs array on the Organization or Person."))
        return out
    j = page["jsonld"]
    f = j["found"]
    if f["Organization"] or f["Person"]:
        which = " and ".join(k for k in ("Organization", "Person") if f[k])
        out.append(make_check("entity_schema", "Organization or Person schema", "Structured data", 5, PASS,
                              "%s found" % which, ""))
    else:
        out.append(make_check("entity_schema", "Organization or Person schema", "Structured data", 5, WARN,
                              "No Organization or Person on the start page",
                              "Add Organization (or Person) JSON-LD with name, url, logo and sameAs."))
    if f["WebSite"]:
        out.append(make_check("website_schema", "WebSite schema", "Structured data", 2, PASS, "WebSite found", ""))
    else:
        out.append(make_check("website_schema", "WebSite schema", "Structured data", 2, WARN, "No WebSite schema",
                              "Add WebSite JSON-LD with the site name and URL to the home page."))
    n = len(j["same_as"])
    if n >= 2:
        out.append(make_check("same_as", "sameAs profile links", "Structured data", 3, PASS, "%d profile links" % n, ""))
    else:
        out.append(make_check("same_as", "sameAs profile links", "Structured data", 3, WARN,
                              "%d profile link%s" % (n, "" if n == 1 else "s"),
                              "Add sameAs links to your official profiles (LinkedIn, YouTube, Crunchbase, etc.)."))
    return out


def robots_checks(robots: Response, path: str) -> Tuple[List[dict], List[dict], List[str]]:
    """Return (checks, per-crawler rows, sitemap URLs from robots.txt)."""
    checks: List[dict] = []
    rows: List[dict] = []
    unknown = False
    groups: List[dict] = []
    sitemaps: List[str] = []
    is_html = robots.ok and robots.looks_like_html()

    if robots.ok and not is_html:
        groups, sitemaps = parse_robots(robots.text)
        extra = "sitemap listed" if sitemaps else "no Sitemap line"
        if sitemaps:
            checks.append(make_check("robots_txt", "robots.txt", "Crawl access", 3, PASS,
                                     "Found, %d group%s, %s" % (len(groups), "" if len(groups) == 1 else "s", extra), ""))
        else:
            checks.append(make_check("robots_txt", "robots.txt", "Crawl access", 3, WARN,
                                     "Found, %d group%s, %s" % (len(groups), "" if len(groups) == 1 else "s", extra),
                                     "Add a line like 'Sitemap: https://yoursite.com/sitemap.xml' to robots.txt."))
    elif is_html:
        checks.append(make_check("robots_txt", "robots.txt", "Crawl access", 3, WARN,
                                 "Returns an HTML page, not robots rules",
                                 "Serve a plain-text robots.txt at the site root with your rules and Sitemap line."))
    elif 400 <= robots.status < 500:
        checks.append(make_check("robots_txt", "robots.txt", "Crawl access", 3, WARN,
                                 "Not found (HTTP %d); every crawler is allowed by default" % robots.status,
                                 "Add a robots.txt that allows search and AI crawlers and lists your sitemap."))
    else:
        unknown = True
        why = robots.error or ("HTTP %d" % robots.status)
        checks.append(make_check("robots_txt", "robots.txt", "Crawl access", 3, FAIL,
                                 "Unreachable (%s); crawlers may stop crawling the site" % why,
                                 "Make robots.txt return 200 (or 404); server errors can pause crawling."))

    for name, powers, role in CRAWLERS:
        if unknown:
            st = {"status": "unknown", "group": "none"}
        else:
            st = crawler_status(groups, name, path)
        rows.append({"crawler": name, "role": role, "powers": powers, "status": st["status"], "group": st["group"]})

    def group_check(cid, label, role, weight, block_status, fix):
        sub = [r for r in rows if r["role"] == role]
        names = ", ".join(r["crawler"] for r in sub)
        if unknown:
            return make_check(cid, label, "Crawl access", weight, FAIL, "Unknown: robots.txt unreachable",
                              "Fix robots.txt so it returns 200 or 404.")
        blocked = [r["crawler"] for r in sub if r["status"] == "blocked"]
        partial = [r["crawler"] for r in sub if r["status"] == "partial"]
        if blocked:
            return make_check(cid, label, "Crawl access", weight, block_status, "Blocked: " + ", ".join(blocked), fix)
        if partial:
            return make_check(cid, label, "Crawl access", weight, PASS,
                              "Allowed (some paths disallowed for %s)" % ", ".join(partial), "")
        return make_check(cid, label, "Crawl access", weight, PASS, "All allowed: " + names, "")

    checks.append(group_check("crawlers_search", "Search engine crawlers", "search", 10, FAIL,
                              "Allow Googlebot and Bingbot in robots.txt; AI Overviews and Copilot depend on them."))
    checks.append(group_check("crawlers_answer", "AI answer engine crawlers", "answer", 10, FAIL,
                              "Allow OAI-SearchBot, ChatGPT-User, Claude-SearchBot and PerplexityBot in robots.txt."))
    checks.append(group_check("crawlers_training", "AI training crawlers", "training", 2, WARN,
                              "Your call: blocking these doesn't remove you from AI search, but models learn less about you."))
    return checks, rows, sitemaps


def llms_check(resp: Response) -> dict:
    if resp.ok and resp.body.strip() and not resp.looks_like_html():
        lines = [ln for ln in resp.text.splitlines() if ln.strip()]
        if lines and lines[0].lstrip().startswith("#"):
            return make_check("llms_txt", "llms.txt", "Crawl access", 2, PASS,
                              "Found, %d lines" % len(lines), "")
        return make_check("llms_txt", "llms.txt", "Crawl access", 2, WARN,
                          "Found, but it doesn't start with a '# Site name' line",
                          "Start llms.txt with '# Site name', then a '>' summary and links to key pages.")
    why = "Returns an HTML page" if resp.ok else ("Not found (HTTP %d)" % resp.status if resp.status else
                                                  "Not reachable (%s)" % (resp.error or "no response"))
    return make_check("llms_txt", "llms.txt", "Crawl access", 2, WARN, why + "; optional, few engines read it yet",
                      "Optional: add /llms.txt with a short site summary and links to your key pages.")


def https_check(main: Response, http_variant: Optional[Response]) -> dict:
    scheme = urlparse(main.final_url).scheme.lower()
    if main.status == 0 and not main.ok:
        return make_check("https", "HTTPS", "Security and speed", 8, FAIL,
                          "Site didn't respond (%s)" % (main.error or "no response"),
                          "Make the site reachable over HTTPS.")
    if scheme != "https":
        return make_check("https", "HTTPS", "Security and speed", 8, FAIL, "Served over plain HTTP",
                          "Install a TLS certificate and 301 redirect every http:// URL to https://.")
    if http_variant is None or http_variant.status == 0:
        return make_check("https", "HTTPS", "Security and speed", 8, PASS, "HTTPS (plain HTTP not served)", "")
    if urlparse(http_variant.final_url).scheme.lower() == "https":
        return make_check("https", "HTTPS", "Security and speed", 8, PASS, "HTTPS, and http:// redirects to it", "")
    return make_check("https", "HTTPS", "Security and speed", 8, WARN, "HTTPS works, but http:// doesn't redirect",
                      "Add a 301 redirect from http:// to https:// for every URL.")


def sitemap_check(fetcher: Fetcher, origin: str, listed: List[str], timeout: float) -> Tuple[dict, List[str], dict]:
    candidates = list(dict.fromkeys(listed))[:3] or [origin + "/sitemap.xml", origin + "/sitemap_index.xml"]
    problems = []
    for sm_url in candidates:
        resp = fetcher(sm_url, timeout)
        if not resp.ok:
            problems.append("%s: %s" % (sm_url, resp.error or "HTTP %d" % resp.status))
            continue
        try:
            kind, locs = parse_sitemap(resp.body)
        except ValueError as e:
            problems.append("%s: %s" % (sm_url, e))
            continue
        urls: List[str] = []
        note = ""
        if kind == "sitemapindex":
            children = locs[:MAX_CHILD_SITEMAPS]
            with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                results = list(ex.map(lambda u: fetcher(u, timeout), children))
            for child in results:
                if child.ok:
                    try:
                        ck, clocs = parse_sitemap(child.body)
                        if ck == "urlset":
                            urls.extend(clocs)
                    except ValueError:
                        pass
            note = " (index of %d sitemap%s, read %d)" % (len(locs), "" if len(locs) == 1 else "s", len(children))
        else:
            urls = locs
        urls = list(dict.fromkeys(urls))
        info = {"url": sm_url, "kind": kind, "url_count": len(urls)}
        detail = "%s: %d URLs%s" % (sm_url, len(urls), note)
        if not urls:
            return (make_check("sitemap", "XML sitemap", "Crawl access", 8, WARN, detail + ", no page URLs",
                               "List every indexable page in the sitemap."), urls, info)
        if not listed:
            return (make_check("sitemap", "XML sitemap", "Crawl access", 8, WARN, detail + ", not listed in robots.txt",
                               "Add 'Sitemap: %s' to robots.txt." % sm_url), urls, info)
        return make_check("sitemap", "XML sitemap", "Crawl access", 8, PASS, detail, ""), urls, info
    detail = "Not found or unreadable" + (": " + problems[0] if problems else "")
    return (make_check("sitemap", "XML sitemap", "Crawl access", 8, FAIL, detail,
                       "Publish /sitemap.xml with every indexable URL and list it in robots.txt."),
            [], {"url": None, "kind": None, "url_count": 0})


# --------------------------------------------------------------------------- scoring

def score_checks(checks: List[dict]) -> int:
    total = sum(c["weight"] for c in checks)
    if not total:
        return 0
    got = sum(c["weight"] * FACTOR[c["status"]] for c in checks)
    return int(round(100.0 * got / total))


def overall_score(site: List[dict], pages: List[List[dict]]) -> int:
    total = sum(c["weight"] for c in site)
    got = sum(c["weight"] * FACTOR[c["status"]] for c in site)
    # each page check counts once, averaged over every audited page
    by_id: Dict[str, List[dict]] = {}
    for page in pages:
        for ch in page:
            by_id.setdefault(ch["id"], []).append(ch)
    for items in by_id.values():
        weight = items[0]["weight"]
        total += weight
        got += weight * sum(FACTOR[ch["status"]] for ch in items) / len(items)
    return int(round(100.0 * got / total)) if total else 0


def grade(score: int) -> str:
    if score >= 90:
        return "Ready"
    if score >= 70:
        return "Mostly ready"
    if score >= 50:
        return "Needs work"
    return "Not ready"


def sample_urls(urls: List[str], n: int, host: str, exclude: str) -> List[str]:
    pool = [u for u in urls if urlparse(u).netloc.lower() == host.lower() and _norm_url(u) != _norm_url(exclude)]
    if n <= 0 or not pool:
        return []
    if len(pool) <= n:
        return pool
    step = len(pool) / float(n)
    return [pool[int(i * step)] for i in range(n)]


# --------------------------------------------------------------------------- audit

def normalize_start_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("no URL given")
    if not re.match(r"^[a-z][a-z0-9+.-]*://", raw, re.I):
        raw = "https://" + raw
    p = urlparse(raw)
    if p.scheme.lower() not in ("http", "https") or not p.netloc or " " in p.netloc:
        raise ValueError("not a valid http(s) URL: %s" % raw)
    return raw if p.path else raw + "/"


def run_audit(url: str, pages: int = 0, timeout: float = 15.0, fetcher: Optional[Fetcher] = None) -> dict:
    fetcher = fetcher or fetch
    start_url = normalize_start_url(url)
    pages = max(0, min(int(pages), MAX_PAGES))
    main = fetcher(start_url, timeout)
    final = main.final_url if main.status else start_url
    fp = urlparse(final)
    origin = "%s://%s" % (fp.scheme, fp.netloc)
    path = fp.path or "/"

    http_url = "http://%s%s" % (fp.netloc, path) if fp.scheme == "https" else None
    jobs = {"robots": origin + "/robots.txt", "llms": origin + "/llms.txt"}
    if http_url:
        jobs["http"] = http_url
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {k: ex.submit(fetcher, v, timeout) for k, v in jobs.items()}
        got = {k: f.result() for k, f in futures.items()}

    site_checks, crawler_rows, listed = robots_checks(got["robots"], path)
    site_checks.append(llms_check(got["llms"]))
    sm_check, sm_urls, sm_info = sitemap_check(fetcher, origin, listed, timeout)
    site_checks.append(sm_check)
    site_checks.append(https_check(main, got.get("http")))

    main_checks, main_data = page_checks(main)
    site_checks.extend(entity_checks(main_data))

    sampled = sample_urls(sm_urls, pages, fp.netloc, final)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        sample_resps = list(ex.map(lambda u: fetcher(u, timeout), sampled))

    page_reports = []
    all_page_checks = [main_checks]
    page_reports.append(_page_report(main, main_checks, main_data))
    for resp in sample_resps:
        checks, data = page_checks(resp)
        all_page_checks.append(checks)
        page_reports.append(_page_report(resp, checks, data))

    score = overall_score(site_checks, all_page_checks)
    schema = dict(main_data["jsonld"]["found"]) if main_data else {k: False for k in KEY_SCHEMA_TYPES}
    return {
        "tool": "ai-search-readiness-audit",
        "version": __version__,
        "url": start_url,
        "final_url": final,
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "score": score,
        "grade": grade(score),
        "pages_audited": len(page_reports),
        "checks": sorted(site_checks + main_checks, key=lambda ch: CHECK_ORDER.index(ch["id"])),
        "crawlers": crawler_rows,
        "schema_types": schema,
        "sitemap": sm_info,
        "pages": page_reports,
    }


def _page_report(resp: Response, checks: List[dict], data: Optional[dict]) -> dict:
    return {
        "url": resp.url,
        "final_url": resp.final_url,
        "http_status": resp.status,
        "error": resp.error or None,
        "score": score_checks(checks),
        "title": data["title"] if data else None,
        "word_count": data["word_count"] if data else None,
        "response_time_ms": int(round(resp.elapsed * 1000)),
        "html_bytes": len(resp.body),
        "schema_types": data["jsonld"]["types"] if data else [],
        "checks": checks,
    }


# --------------------------------------------------------------------------- output

COLORS = {PASS: "\033[32m", WARN: "\033[33m", FAIL: "\033[31m"}
BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"


def render_text(report: dict, color: bool = False) -> str:
    def c(s, code):
        return "%s%s%s" % (code, s, RESET) if color else s

    lines = []
    lines.append(c("AI Search Readiness Audit v%s" % report["version"], BOLD))
    lines.append("URL      %s" % report["final_url"])
    lines.append("Checked  %s, %d page%s" % (report["checked_at"].replace("T", " ").replace("Z", " UTC"),
                                             report["pages_audited"], "" if report["pages_audited"] == 1 else "s"))
    lines.append("")
    lines.append(c("Score    %d/100  %s" % (report["score"], report["grade"]), BOLD))
    by_cat: Dict[str, List[dict]] = {}
    for ch in report["checks"]:
        by_cat.setdefault(ch["category"], []).append(ch)
    width = max(len(ch["name"]) for ch in report["checks"]) + 2
    for cat in CATEGORIES:
        if cat not in by_cat:
            continue
        lines.append("")
        lines.append(c(cat, BOLD))
        for ch in by_cat[cat]:
            lines.append("  %s  %s%s" % (c(ch["status"], COLORS[ch["status"]]), ch["name"].ljust(width), ch["detail"]))
            if ch["status"] != PASS and ch["fix"]:
                lines.append("        " + c("fix: " + ch["fix"], DIM))

    lines.append("")
    lines.append(c("AI crawler access (robots.txt)", BOLD))
    for r in report["crawlers"]:
        st = r["status"]
        code = COLORS[FAIL] if st in ("blocked", "unknown") else (COLORS[WARN] if st == "partial" else COLORS[PASS])
        lines.append("  %s %s %s %s" % (r["crawler"].ljust(18), r["role"].ljust(9), c(st.ljust(8), code), r["powers"]))

    lines.append("")
    lines.append(c("Schema types on the start page", BOLD))
    lines.append("  " + "  ".join("[%s] %s" % ("x" if v else " ", k) for k, v in report["schema_types"].items()))

    if len(report["pages"]) > 1:
        lines.append("")
        lines.append(c("Pages (start page + %d from the sitemap)" % (len(report["pages"]) - 1), BOLD))
        for p in report["pages"]:
            issues = ["%s %s" % (ch["status"], ch["name"]) for ch in p["checks"] if ch["status"] != PASS]
            path = urlparse(p["url"]).path or "/"
            lines.append("  %3d  %s" % (p["score"], path))
            if issues:
                lines.append("       " + c("; ".join(issues), DIM))

    lines.append("")
    lines.append("JSON: --json   Markdown: --markdown   More pages: --pages N")
    return "\n".join(lines) + "\n"


def _md(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def render_markdown(report: dict) -> str:
    host = urlparse(report["final_url"]).netloc
    out = ["# AI Search Readiness Audit: %s" % host, ""]
    out.append("**Score: %d/100 (%s)**  " % (report["score"], report["grade"]))
    out.append("URL: %s  " % report["final_url"])
    out.append("Checked: %s, %d page%s, ai-search-readiness-audit v%s" % (
        report["checked_at"], report["pages_audited"], "" if report["pages_audited"] == 1 else "s", report["version"]))
    out.append("")
    out.append("## Checks")
    out.append("")
    out.append("| Status | Check | Result | Fix |")
    out.append("|---|---|---|---|")
    for cat in CATEGORIES:
        for ch in report["checks"]:
            if ch["category"] == cat:
                out.append("| %s | %s | %s | %s |" % (ch["status"], _md(ch["name"]), _md(ch["detail"]), _md(ch["fix"])))
    out.append("")
    out.append("## AI crawler access (robots.txt)")
    out.append("")
    out.append("| Crawler | Role | Status | Powers |")
    out.append("|---|---|---|---|")
    for r in report["crawlers"]:
        out.append("| %s | %s | %s | %s |" % (r["crawler"], r["role"], r["status"], _md(r["powers"])))
    out.append("")
    out.append("## Schema types on the start page")
    out.append("")
    for k, v in report["schema_types"].items():
        out.append("- [%s] %s" % ("x" if v else " ", k))
    if len(report["pages"]) > 1:
        out.append("")
        out.append("## Pages")
        out.append("")
        out.append("| Score | URL | Issues |")
        out.append("|---|---|---|")
        for p in report["pages"]:
            issues = "; ".join("%s %s" % (ch["status"], ch["name"]) for ch in p["checks"] if ch["status"] != PASS)
            out.append("| %d | %s | %s |" % (p["score"], _md(p["url"]), _md(issues or "none")))
    out.append("")
    out.append("Generated by [ai-search-readiness-audit](%s)." % REPO_URL)
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="ai-search-audit",
        description="Audit a website for AI search readiness (Google AI Overviews and AI Mode, "
                    "ChatGPT search, Perplexity, Claude, Gemini, Copilot).",
        epilog=REPO_URL)
    ap.add_argument("url", help="site or page to audit, for example https://example.com")
    fmt = ap.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="print the report as JSON")
    fmt.add_argument("--markdown", action="store_true", help="print the report as Markdown")
    ap.add_argument("--pages", type=int, default=0, metavar="N",
                    help="also audit N URLs sampled from the sitemap (max %d)" % MAX_PAGES)
    ap.add_argument("--timeout", type=float, default=15.0, metavar="SEC", help="per-request timeout (default 15)")
    ap.add_argument("--fail-under", type=int, default=None, metavar="SCORE",
                    help="exit with code 1 if the score is below SCORE (for CI)")
    ap.add_argument("--no-color", action="store_true", help="plain terminal output")
    ap.add_argument("--version", action="version", version="%(prog)s " + __version__)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    try:
        report = run_audit(args.url, pages=args.pages, timeout=max(1.0, args.timeout))
    except ValueError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
        return 130
    except Exception as e:  # last line of defense: report, don't crash
        print("error: audit failed (%s: %s)" % (type(e).__name__, e), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    elif args.markdown:
        print(render_markdown(report), end="")
    else:
        color = sys.stdout.isatty() and not args.no_color and not os.environ.get("NO_COLOR")
        print(render_text(report, color=color), end="")
    if args.fail_under is not None and report["score"] < args.fail_under:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
