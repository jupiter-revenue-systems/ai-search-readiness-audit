"""Offline tests for audit.py. Run: python3 -m unittest discover -s tests -v"""
import contextlib
import gzip
import io
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import audit  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


def fixture(name):
    with open(os.path.join(FIX, name), "rb") as f:
        return f.read()


class FakeSite:
    """Serves fixtures by URL instead of touching the network."""

    def __init__(self, routes, elapsed=0.1):
        self.routes = routes
        self.elapsed = elapsed
        self.calls = []

    def __call__(self, url, timeout=15.0):
        self.calls.append(url)
        if url not in self.routes:
            return audit.Response(url, 404, url, {"Content-Type": "text/html"}, b"<h1>Not found</h1>", self.elapsed)
        route = self.routes[url]
        if isinstance(route, audit.Response):
            return route
        status, body, ctype = route
        return audit.Response(url, status, url, {"Content-Type": ctype}, body, self.elapsed)


def good_site():
    html = fixture("good_page.html")
    return FakeSite({
        "https://acme.test/": (200, html, "text/html; charset=utf-8"),
        "https://acme.test/widgets": (200, html.replace(b'href="https://acme.test/"', b'href="https://acme.test/widgets"'),
                                      "text/html"),
        "https://acme.test/about": (200, html.replace(b'href="https://acme.test/"', b'href="https://acme.test/about"'),
                                    "text/html"),
        "https://acme.test/robots.txt": (200, b"User-agent: *\nAllow: /\n\nSitemap: https://acme.test/sitemap.xml\n",
                                         "text/plain"),
        "https://acme.test/llms.txt": (200, fixture("llms.txt"), "text/plain"),
        "https://acme.test/sitemap.xml": (200, fixture("sitemap.xml"), "application/xml"),
        "http://acme.test/": audit.Response("http://acme.test/", 200, "https://acme.test/",
                                            {"Content-Type": "text/html"}, b"<html></html>", 0.1),
    })


def by_id(checks):
    return {c["id"]: c for c in checks}


class RobotsTests(unittest.TestCase):
    def setUp(self):
        self.groups, self.sitemaps = audit.parse_robots(fixture("robots_mixed.txt").decode())

    def test_groups_and_sitemap(self):
        self.assertEqual(len(self.groups), 4)
        self.assertEqual(self.groups[1]["agents"], ["gptbot", "ccbot"])
        self.assertEqual(self.sitemaps, ["https://acme.test/sitemap.xml"])

    def test_named_groups_block(self):
        for bot in ("GPTBot", "CCBot", "PerplexityBot"):
            self.assertEqual(audit.crawler_status(self.groups, bot)["status"], "blocked", bot)

    def test_fallback_to_star_group(self):
        st = audit.crawler_status(self.groups, "ClaudeBot")
        self.assertEqual(st, {"status": "partial", "group": "*"})

    def test_named_group_overrides_star(self):
        rules, source = audit.rules_for(self.groups, "Googlebot")
        self.assertEqual(source, "named")
        self.assertTrue(audit.path_allowed(rules, "/admin/"))  # * rules don't apply to Googlebot
        self.assertFalse(audit.path_allowed(rules, "/private/x"))
        self.assertTrue(audit.path_allowed(rules, "/private/press"))
        self.assertFalse(audit.path_allowed(rules, "/private/press-kit"))

    def test_wildcards_and_ties(self):
        rules = [(False, "/*.pdf$"), (True, "/a"), (False, "/a")]
        self.assertFalse(audit.path_allowed(rules, "/files/x.pdf"))
        self.assertTrue(audit.path_allowed(rules, "/files/x.pdf?v=1"))
        self.assertTrue(audit.path_allowed(rules, "/a"))  # Allow wins a tie

    def test_empty_disallow_allows_all(self):
        groups, _ = audit.parse_robots("User-agent: *\nDisallow:\n")
        self.assertEqual(audit.crawler_status(groups, "GPTBot")["status"], "allowed")

    def test_agent_case_and_version(self):
        groups, _ = audit.parse_robots("User-Agent: gptbot/1.2\nDisallow: /\n")
        self.assertEqual(audit.crawler_status(groups, "GPTBot")["status"], "blocked")

    def test_robots_checks(self):
        resp = audit.Response("https://acme.test/robots.txt", 200, None, {"Content-Type": "text/plain"},
                              fixture("robots_mixed.txt"))
        checks, rows, sitemaps = audit.robots_checks(resp, "/")
        c = by_id(checks)
        self.assertEqual(c["robots_txt"]["status"], audit.PASS)
        self.assertEqual(c["crawlers_answer"]["status"], audit.FAIL)
        self.assertIn("PerplexityBot", c["crawlers_answer"]["detail"])
        self.assertEqual(c["crawlers_search"]["status"], audit.PASS)
        self.assertEqual(c["crawlers_training"]["status"], audit.WARN)
        self.assertEqual(len(rows), 11)
        self.assertEqual(sitemaps, ["https://acme.test/sitemap.xml"])

    def test_missing_and_broken_robots(self):
        missing = audit.Response("u", 404, None, {"Content-Type": "text/html"}, b"<html></html>")
        checks, rows, _ = audit.robots_checks(missing, "/")
        self.assertEqual(by_id(checks)["robots_txt"]["status"], audit.WARN)
        self.assertTrue(all(r["status"] == "allowed" for r in rows))
        broken = audit.Response("u", 503)
        checks, rows, _ = audit.robots_checks(broken, "/")
        self.assertEqual(by_id(checks)["crawlers_search"]["status"], audit.FAIL)
        self.assertTrue(all(r["status"] == "unknown" for r in rows))


class SitemapTests(unittest.TestCase):
    def test_urlset(self):
        kind, locs = audit.parse_sitemap(fixture("sitemap.xml"))
        self.assertEqual(kind, "urlset")
        self.assertEqual(len(locs), 5)

    def test_index_and_gzip(self):
        kind, locs = audit.parse_sitemap(gzip.compress(fixture("sitemap_index.xml")))
        self.assertEqual(kind, "sitemapindex")
        self.assertEqual(locs, ["https://acme.test/sitemap-pages.xml"])

    def test_rejects_html_entities_and_junk(self):
        for bad in (b"<html><body>hi</body></html>", b"", b"not xml at all",
                    b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><urlset>&a;</urlset>'):
            with self.assertRaises(ValueError):
                audit.parse_sitemap(bad)

    def test_sitemap_index_is_followed(self):
        site = FakeSite({
            "https://acme.test/sitemap.xml": (200, fixture("sitemap_index.xml"), "application/xml"),
            "https://acme.test/sitemap-pages.xml": (200, fixture("sitemap.xml"), "application/xml"),
        })
        check, urls, info = audit.sitemap_check(site, "https://acme.test", ["https://acme.test/sitemap.xml"], 5)
        self.assertEqual(check["status"], audit.PASS)
        self.assertEqual(len(urls), 5)
        self.assertEqual(info["kind"], "sitemapindex")

    def test_sitemap_missing(self):
        check, urls, _ = audit.sitemap_check(FakeSite({}), "https://acme.test", [], 5)
        self.assertEqual(check["status"], audit.FAIL)
        self.assertEqual(urls, [])


class HtmlTests(unittest.TestCase):
    def test_good_page_parse(self):
        d = audit.analyze_html(fixture("good_page.html").decode(), "https://acme.test/")
        self.assertEqual(d["title"], "Acme Widgets: Durable Widgets for Small Shops")  # not the <svg><title>
        self.assertEqual(d["lang"], "en")
        self.assertEqual(d["h1_count"], 1)
        self.assertEqual(d["canonicals"], ["https://acme.test/"])
        self.assertGreaterEqual(d["word_count"], 150)
        self.assertNotIn("logo", " ".join(d["og"].values()))
        j = d["jsonld"]
        self.assertEqual(j["errors"], [])
        for t in ("Organization", "WebSite", "FAQPage", "Question", "Answer"):
            self.assertIn(t, j["types"])
        self.assertEqual(len(j["same_as"]), 2)
        self.assertTrue(j["found"]["Organization"] and j["found"]["WebSite"] and j["found"]["FAQPage"])
        self.assertFalse(j["found"]["Person"] or j["found"]["Product"] or j["found"]["Article"])

    def test_script_text_not_counted(self):
        d = audit.analyze_html("<html><body><p>one two</p><script>var a = 'three four five';</script></body></html>",
                               "https://x.test/")
        self.assertEqual(d["word_count"], 2)

    def test_good_page_checks(self):
        resp = audit.Response("https://acme.test/", 200, None, {"Content-Type": "text/html"},
                              fixture("good_page.html"), 0.2)
        checks, data = audit.page_checks(resp)
        self.assertTrue(all(c["status"] == audit.PASS for c in checks), [c for c in checks if c["status"] != "PASS"])
        self.assertTrue(all(c["fix"] == "" for c in checks))
        self.assertEqual([c["id"] for c in checks], [s[0] for s in audit.PAGE_CHECK_SPECS])

    def test_bad_page_checks(self):
        resp = audit.Response("https://acme.test/", 200, None, {"Content-Type": "text/html"},
                              fixture("bad_page.html"), 2.5)
        c = by_id(audit.page_checks(resp)[0])
        self.assertEqual(c["noindex"]["status"], audit.FAIL)
        self.assertEqual(c["canonical"]["status"], audit.WARN)
        self.assertEqual(c["title"]["status"], audit.WARN)
        self.assertEqual(c["description"]["status"], audit.FAIL)
        self.assertEqual(c["h1"]["status"], audit.WARN)
        self.assertEqual(c["text_in_html"]["status"], audit.FAIL)
        self.assertEqual(c["lang"]["status"], audit.WARN)
        self.assertEqual(c["open_graph"]["status"], audit.WARN)
        self.assertEqual(c["jsonld"]["status"], audit.FAIL)
        self.assertEqual(c["response_time"]["status"], audit.FAIL)
        for check in c.values():
            if check["status"] != audit.PASS:
                self.assertTrue(check["fix"], check["id"])
                self.assertNotIn("\n", check["fix"])

    def test_header_noindex_and_nosnippet(self):
        html = fixture("good_page.html")
        resp = audit.Response("https://acme.test/", 200, None,
                              {"Content-Type": "text/html", "X-Robots-Tag": "noindex"}, html)
        self.assertEqual(by_id(audit.page_checks(resp)[0])["noindex"]["status"], audit.FAIL)
        html2 = html.replace(b"<head>", b'<head><meta name="robots" content="nosnippet">')
        resp2 = audit.Response("https://acme.test/", 200, None, {"Content-Type": "text/html"}, html2)
        self.assertEqual(by_id(audit.page_checks(resp2)[0])["noindex"]["status"], audit.WARN)

    def test_unloaded_page(self):
        checks, data = audit.page_checks(audit.Response("https://acme.test/", 0, error="timed out"))
        self.assertIsNone(data)
        self.assertTrue(all(c["status"] == audit.FAIL for c in checks))
        self.assertIn("timed out", checks[0]["detail"])

    def test_malformed_html_does_not_crash(self):
        d = audit.analyze_html("<html lang=en><title>x<h1><<<</p></div><script type='application/ld+json'>{",
                               "https://x.test/")
        self.assertEqual(d["lang"], "en")


class ScoreTests(unittest.TestCase):
    def mk(self, status, weight=10, cid="x"):
        return audit.make_check(cid, "n", "Content", weight, status, "d", "f")

    def test_score_bounds(self):
        self.assertEqual(audit.score_checks([self.mk("PASS"), self.mk("PASS")]), 100)
        self.assertEqual(audit.score_checks([self.mk("FAIL"), self.mk("FAIL")]), 0)
        self.assertEqual(audit.score_checks([self.mk("PASS"), self.mk("WARN"), self.mk("FAIL")]), 50)

    def test_pages_are_averaged(self):
        site = [self.mk("PASS", 10, "a")]
        pages = [[self.mk("PASS", 10, "b")], [self.mk("FAIL", 10, "b")]]
        self.assertEqual(audit.overall_score(site, pages), 75)

    def test_grade(self):
        self.assertEqual(audit.grade(95), "Ready")
        self.assertEqual(audit.grade(10), "Not ready")

    def test_sample_urls_same_host_only(self):
        urls = ["https://acme.test/", "https://acme.test/a", "https://other.test/b", "https://acme.test/c"]
        self.assertEqual(audit.sample_urls(urls, 5, "acme.test", "https://acme.test/"),
                         ["https://acme.test/a", "https://acme.test/c"])
        self.assertEqual(len(audit.sample_urls(["https://acme.test/%d" % i for i in range(100)], 4, "acme.test", "")), 4)


class EndToEndTests(unittest.TestCase):
    def test_good_site(self):
        site = good_site()
        report = audit.run_audit("acme.test", pages=5, fetcher=site)
        self.assertEqual(report["final_url"], "https://acme.test/")
        self.assertEqual(report["pages_audited"], 4)  # start page + 3 same-host sitemap URLs
        contact = [p for p in report["pages"] if p["url"].endswith("/contact")][0]
        self.assertEqual((contact["http_status"], contact["score"]), (404, 0))
        self.assertNotIn("https://elsewhere.test/not-ours", site.calls)
        c = by_id(report["checks"])
        self.assertEqual(c["https"]["status"], audit.PASS)
        self.assertEqual(c["llms_txt"]["status"], audit.PASS)
        self.assertEqual(c["sitemap"]["status"], audit.PASS)
        self.assertEqual(c["entity_schema"]["status"], audit.PASS)
        self.assertTrue(70 <= report["score"] < 100, report["score"])
        self.assertEqual(audit.run_audit("acme.test", fetcher=good_site())["score"], 100)
        json.loads(json.dumps(report))
        text = audit.render_text(report)
        self.assertIn("Score    %d/100" % report["score"], text)
        self.assertIn("GPTBot", text)
        md = audit.render_markdown(report)
        self.assertIn("| PASS | robots.txt |", md)
        self.assertIn(audit.REPO_URL, md)

    def test_every_check_has_known_id(self):
        report = audit.run_audit("https://acme.test/", fetcher=good_site())
        ids = [c["id"] for c in report["checks"]]
        self.assertEqual(ids, audit.CHECK_ORDER)

    def test_dead_site_never_crashes(self):
        dead = lambda url, timeout=15.0: audit.Response(url, 0, error="connection refused")  # noqa: E731
        report = audit.run_audit("https://dead.test", pages=3, fetcher=dead)
        self.assertLess(report["score"], 20)
        self.assertEqual(report["grade"], "Not ready")
        audit.render_text(report, color=True)
        audit.render_markdown(report)

    def test_garbage_responses_never_crash(self):
        junk = b"\x00\xff<<<>>>{{{" * 50
        site = lambda url, timeout=15.0: audit.Response(url, 200, url, {"Content-Type": "text/html"}, junk)  # noqa
        report = audit.run_audit("https://junk.test", pages=2, fetcher=site)
        self.assertIsInstance(report["score"], int)

    def test_bad_url(self):
        with self.assertRaises(ValueError):
            audit.normalize_start_url("ftp://acme.test")
        with self.assertRaises(ValueError):
            audit.normalize_start_url("")
        self.assertEqual(audit.normalize_start_url("acme.test"), "https://acme.test/")


class CliTests(unittest.TestCase):
    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("audit.fetch", good_site()), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = audit.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_json_output(self):
        code, out, _ = self.run_main(["https://acme.test", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["tool"], "ai-search-readiness-audit")
        self.assertIn("score", data)

    def test_markdown_and_text(self):
        code, out, _ = self.run_main(["https://acme.test", "--markdown"])
        self.assertTrue(out.startswith("# AI Search Readiness Audit: acme.test"))
        code, out, _ = self.run_main(["https://acme.test", "--no-color"])
        self.assertIn("AI crawler access (robots.txt)", out)
        self.assertNotIn("\033[", out)

    def test_fail_under_and_bad_url(self):
        code, _, _ = self.run_main(["https://acme.test", "--json", "--fail-under", "101"])
        self.assertEqual(code, 1)
        code, _, err = self.run_main(["ftp://acme.test"])
        self.assertEqual(code, 2)
        self.assertIn("error:", err)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/old":
            self.send_response(308)
            self.send_header("Location", "/page")
            self.end_headers()
            return
        if self.path == "/page":
            body = gzip.compress(fixture("good_page.html"))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args):
        pass


class FetchTests(unittest.TestCase):
    """Uses a local server on 127.0.0.1 only; no internet needed."""

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_gzip_and_308_redirect(self):
        r = audit.fetch(self.base + "/old", timeout=5)
        self.assertEqual(r.status, 200)
        self.assertTrue(r.final_url.endswith("/page"))
        self.assertIn(b"Durable widgets", r.body)
        self.assertLess(r.transfer_bytes, len(r.body))

    def test_404(self):
        r = audit.fetch(self.base + "/missing", timeout=5)
        self.assertEqual(r.status, 404)
        self.assertFalse(r.ok)

    def test_unreachable_never_raises(self):
        r = audit.fetch("http://127.0.0.1:1/", timeout=2)
        self.assertEqual(r.status, 0)
        self.assertTrue(r.error)
        r = audit.fetch("not a url", timeout=2)
        self.assertEqual(r.status, 0)

    def test_user_agent_names_repo(self):
        self.assertIn(audit.REPO_URL, audit.USER_AGENT)
        self.assertTrue(audit.USER_AGENT.startswith("ai-search-readiness-audit/"))


if __name__ == "__main__":
    unittest.main()
