#!/usr/bin/env python3
"""
Orphan Page Finder - lists pages that are live but not linked from anywhere on the site.

1. Collects every page the site declares: its HTML sitemap, its sitemap.xml (and any
   sitemap listed in robots.txt) and, optionally, a list of pages exported from the CMS.
2. Crawls the site from the homepage, as desktop and as mobile, following internal
   links but never using the HTML sitemap as a link source.
3. Reports declared pages that no crawled page links to, after confirming each one
   loads on its own URL (title + live URL).

Usage:
    python orphan_finder.py https://example.com
    python orphan_finder.py https://example.com --skip-inventory --pages-file cms_pages.txt
"""

import argparse
import csv
import re
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from html import escape, unescape
from urllib.parse import urlparse

import requests

from crawl_common import (
    PLAYWRIGHT_MISSING,
    USER_AGENT,
    PlaywrightTimeout,
    add_skip_args,
    compile_skip_patterns,
    ensure_scheme,
    goto_with_retry,
    matches_skip,
    normalize_url,
    skip_patterns_from_args,
    sync_playwright,
)

MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

# Browser settings for each crawl pass. Sites can render different navigation
# for each, so a page counts as linked if either pass finds a link to it.
PROFILES = {
    "desktop": {"user_agent": USER_AGENT, "viewport": {"width": 1366, "height": 900}},
    "mobile": {
        "user_agent": MOBILE_USER_AGENT,
        "viewport": {"width": 390, "height": 844},
        "is_mobile": True,
        "has_touch": True,
    },
}

# Files, not pages: never reported as orphans.
ASSET_EXTENSIONS = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".css", ".js",
    ".xml", ".txt", ".zip", ".mp4", ".mp3", ".doc", ".docx", ".xls", ".xlsx", ".csv",
)

MAX_XML_SITEMAPS = 50


@dataclass
class DeclaredPage:
    url: str
    sources: set = field(default_factory=set)


@dataclass
class CheckedPage:
    url: str
    sources: set
    status: int | str
    final_url: str
    title: str


class OrphanFinder:
    def __init__(
        self,
        base_url: str,
        html_sitemaps: list[str] | None = None,
        xml_sitemaps: list[str] | None = None,
        pages_file: str = "",
        profiles: tuple[str, ...] = ("desktop", "mobile"),
        max_pages: int = 0,
        timeout: int = 60000,
        skip_patterns: list[str] | None = None,
        verbose: bool = True,
    ):
        parsed = urlparse(base_url)
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        self.base_host = parsed.netloc.lower()
        bare_host = self.base_host.removeprefix("www.")
        # Hosts that are the same site; links to them count as internal.
        self.site_hosts = {bare_host, f"www.{bare_host}"}

        self.html_sitemaps = html_sitemaps or [f"{self.origin}/sitemap.htm"]
        self.xml_sitemaps = xml_sitemaps
        self.pages_file = pages_file
        self.profiles = profiles
        self.max_pages = max_pages
        self.timeout = timeout
        self.skip_patterns = compile_skip_patterns(skip_patterns)
        self.verbose = verbose

        self.sitemap_keys = {self.page_key(u) for u in self.html_sitemaps}
        self.declared: dict[str, DeclaredPage] = {}
        # page key -> {profile: first page linking to it}
        self.linked: dict[str, dict[str, str]] = {}
        self.pages_crawled = {profile: 0 for profile in profiles}
        self.crawl_errors: list[tuple[str, str, str]] = []
        self.truncated: list[str] = []  # profiles whose crawl hit --max-pages
        self.orphans: list[CheckedPage] = []
        self.not_live: list[CheckedPage] = []

    def log(self, message: str):
        if self.verbose:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

    # --- URL helpers -------------------------------------------------------

    @staticmethod
    def page_key(url: str) -> str:
        """Identity of a page: lowercased path without index.htm, trailing slash or query.

        The host is ignored so the same page on www/non-www or the CMS host matches.
        """
        path = urlparse(url).path.lower()
        path = re.sub(r"/index\.html?$", "/", path)
        return path.rstrip("/") or "/"

    def is_site_url(self, url: str) -> bool:
        return urlparse(url).netloc.lower() in self.site_hosts

    def on_base_host(self, url: str) -> str:
        """Rewrite a same-site URL onto the host being audited."""
        parsed = urlparse(url)
        rewritten = f"{self.origin}{parsed.path or '/'}"
        return rewritten + (f"?{parsed.query}" if parsed.query else "")

    def is_page(self, url: str) -> bool:
        return not urlparse(url).path.lower().endswith(ASSET_EXTENSIONS)

    def should_skip(self, url: str) -> bool:
        return self.page_key(url) != "/" and matches_skip(url, self.skip_patterns)

    # --- Step 1: declared pages --------------------------------------------

    def declare(self, url: str, source: str):
        if not self.is_site_url(url) or not self.is_page(url) or self.should_skip(url):
            return
        url = self.on_base_host(url.split("#")[0])
        key = self.page_key(url)
        page = self.declared.setdefault(key, DeclaredPage(url=url))
        page.sources.add(source)

    def collect_html_sitemaps(self, context):
        page = context.new_page()
        for sitemap_url in self.html_sitemaps:
            self.log(f"Reading HTML sitemap: {sitemap_url}")
            try:
                page, response = goto_with_retry(page, context, sitemap_url, self.timeout, self.log)
                if response is None or response.status >= 400:
                    status = response.status if response else "no response"
                    self.log(f"  HTML sitemap not available ({status})")
                    continue
                hrefs = page.eval_on_selector_all("a[href]", "els => els.map(a => a.getAttribute('href'))")
            except Exception as e:
                self.log(f"  Could not read HTML sitemap: {e}")
                continue
            before = len(self.declared)
            for href in hrefs:
                url = normalize_url(href or "", page.url)
                if url:
                    self.declare(url, "HTML sitemap")
            self.log(f"  {len(self.declared) - before} new pages declared")
        page.close()

    def _get_text(self, url: str) -> str | None:
        try:
            resp = requests.get(url, timeout=30, headers={"User-Agent": USER_AGENT})
        except requests.RequestException as e:
            self.log(f"  Could not fetch {url}: {e}")
            return None
        if resp.status_code >= 400:
            self.log(f"  {url} not available ({resp.status_code})")
            return None
        return resp.text

    def collect_xml_sitemaps(self):
        queue = deque(self.xml_sitemaps if self.xml_sitemaps is not None else self._default_xml_sitemaps())
        seen = set()
        while queue and len(seen) < MAX_XML_SITEMAPS:
            sitemap_url = queue.popleft()
            if sitemap_url in seen:
                continue
            seen.add(sitemap_url)
            self.log(f"Reading XML sitemap: {sitemap_url}")
            text = self._get_text(sitemap_url)
            if not text:
                continue
            locs = [unescape(loc).strip() for loc in re.findall(r"<loc>(.*?)</loc>", text, re.DOTALL)]
            if "<sitemapindex" in text:
                queue.extend(locs)  # index of other sitemaps
                continue
            # The site's own sitemap names its canonical host(s): treat them as the same site.
            for loc in locs:
                host = urlparse(loc).netloc.lower()
                if host:
                    self.site_hosts.add(host)
            before = len(self.declared)
            for loc in locs:
                self.declare(loc, "sitemap.xml")
            self.log(f"  {len(self.declared) - before} new pages declared")

    def _default_xml_sitemaps(self) -> list[str]:
        """/sitemap.xml plus any Sitemap: entries in robots.txt."""
        sitemaps = [f"{self.origin}/sitemap.xml"]
        robots = self._get_text(f"{self.origin}/robots.txt") or ""
        for match in re.finditer(r"^\s*sitemap:\s*(\S+)", robots, re.IGNORECASE | re.MULTILINE):
            url = match.group(1)
            if url not in sitemaps:
                sitemaps.append(url)
        return sitemaps

    def collect_pages_file(self):
        if not self.pages_file:
            return
        self.log(f"Reading page list: {self.pages_file}")
        before = len(self.declared)
        with open(self.pages_file, newline="", encoding="utf-8-sig") as f:
            for row in csv.reader(f):
                # First cell that looks like a URL or a path; headers and notes are ignored.
                cell = next((c.strip() for c in row if c.strip().startswith(("http://", "https://", "/"))), "")
                if cell:
                    self.declare(cell if cell.startswith("http") else f"{self.origin}{cell}", "page list")
        self.log(f"  {len(self.declared) - before} new pages declared")

    # --- Step 2: crawl -----------------------------------------------------

    def mark_linked(self, url: str, profile: str, source: str):
        self.linked.setdefault(self.page_key(url), {}).setdefault(profile, source)

    def crawl_profile(self, browser, profile: str):
        self.log("-" * 60)
        self.log(f"Crawling as {profile} (the HTML sitemap is not used as a link source)")
        context = browser.new_context(**PROFILES[profile])
        page = context.new_page()

        home = f"{self.origin}/"
        self.mark_linked(home, profile, "(homepage)")
        queue = deque([home])
        queued = {self.page_key(home)}

        while queue and (self.max_pages == 0 or self.pages_crawled[profile] < self.max_pages):
            url = queue.popleft()
            self.pages_crawled[profile] += 1
            limit = "inf" if self.max_pages == 0 else str(self.max_pages)
            self.log(f"[{profile} {self.pages_crawled[profile]}/{limit}] {url}")

            try:
                page, response = goto_with_retry(page, context, url, self.timeout, self.log)
                if response is None:
                    continue
                # A link that redirects makes the destination reachable too.
                if self.is_site_url(page.url):
                    self.mark_linked(page.url, profile, f"(redirect from {url})")
                if self.page_key(page.url) in self.sitemap_keys or self.page_key(url) in self.sitemap_keys:
                    self.log("  Sitemap page: links not followed")
                    continue
                hrefs = page.eval_on_selector_all("a[href]", "els => els.map(a => a.getAttribute('href'))")
            except PlaywrightTimeout:
                self.log("  Timeout (all retries exhausted)")
                self.crawl_errors.append((profile, url, "timeout"))
                continue
            except Exception as e:
                self.log(f"  Error: {e}")
                self.crawl_errors.append((profile, url, str(e)[:200]))
                continue

            new = 0
            for href in hrefs:
                link = normalize_url(href or "", page.url)
                if not link or not self.is_site_url(link):
                    continue
                self.mark_linked(link, profile, url)
                key = self.page_key(link)
                if key in queued or not self.is_page(link) or self.should_skip(link):
                    continue
                queued.add(key)
                queue.append(self.on_base_host(link))
                new += 1
            self.log(f"  {len(hrefs)} links, {new} new pages queued")

        if queue:
            self.truncated.append(profile)
            self.log(f"  --max-pages reached with {len(queue)} pages left: results will include false orphans")
        context.close()

    # --- Step 3: verify ----------------------------------------------------

    def check_pages(self, browser, pages: list[DeclaredPage]):
        """Load each unlinked page to confirm it's live on its own URL and get its title."""
        if not pages:
            return
        self.log("-" * 60)
        self.log(f"Checking {len(pages)} declared pages that nothing links to...")
        context = browser.new_context(**PROFILES["desktop"])
        page = context.new_page()
        for declared in sorted(pages, key=lambda p: p.url):
            status, final_url, title = "ERROR", "", ""
            try:
                page, response = goto_with_retry(page, context, declared.url, self.timeout, self.log)
                if response is not None:
                    status, final_url, title = response.status, page.url, page.title().strip()
            except PlaywrightTimeout:
                status = "TIMEOUT"
            except Exception as e:
                title = str(e)[:200]

            result = CheckedPage(declared.url, declared.sources, status, final_url, title)
            redirected = final_url and self.page_key(final_url) != self.page_key(declared.url)
            if status == 200 and not redirected:
                self.orphans.append(result)
                self.log(f"  ORPHAN  {declared.url}  ({title})")
            else:
                self.not_live.append(result)
                detail = f"-> {final_url}" if redirected else ""
                self.log(f"  not live [{status}] {declared.url} {detail}")
        context.close()

    def run(self):
        if sync_playwright is None:
            raise RuntimeError(PLAYWRIGHT_MISSING)

        self.log(f"Finding orphan pages on {self.origin}")
        self.collect_xml_sitemaps()
        self.collect_pages_file()

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            self.collect_html_sitemaps(browser.new_context(**PROFILES["desktop"]))
            self.log(f"{len(self.declared)} declared pages in total")
            if not self.declared:
                self.log("No declared pages found: nothing to compare against.")
                browser.close()
                return

            for profile in self.profiles:
                self.crawl_profile(browser, profile)

            unlinked = [p for key, p in self.declared.items() if key not in self.linked]
            self.check_pages(browser, unlinked)
            browser.close()

        self.log("-" * 60)
        self.log("Done!")

    # --- Reports -----------------------------------------------------------

    def one_device_only(self) -> list[tuple[DeclaredPage, str, str]]:
        """Declared pages linked on only one of the crawled profiles."""
        if len(self.profiles) < 2:
            return []
        rows = []
        for key, declared in sorted(self.declared.items()):
            found = self.linked.get(key, {})
            if len(found) == 1:
                profile, source = next(iter(found.items()))
                rows.append((declared, profile, source))
        return rows

    def generate_csv_report(self, filename: str):
        with open(filename, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Page Title", "Live URL", "Found In"])
            for page in self.orphans:
                writer.writerow([page.title, page.url, ", ".join(sorted(page.sources))])
        self.log(f"CSV report saved to: {filename}")

    def generate_html_report(self, filename: str):
        crawled = ", ".join(f"{n} {p}" for p, n in self.pages_crawled.items())
        one_device = self.one_device_only()

        html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Orphan Pages - {escape(self.origin)}</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 40px; background: #f5f5f5; }}
        .container {{ max-width: 1200px; margin: 0 auto; background: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        h1 {{ color: #333; border-bottom: 2px solid #007bff; padding-bottom: 10px; }}
        h2 {{ color: #555; margin-top: 30px; }}
        .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 20px; margin: 20px 0; }}
        .stat {{ background: #f8f9fa; padding: 20px; border-radius: 8px; text-align: center; }}
        .stat-number {{ font-size: 2em; font-weight: bold; }}
        .stat-label {{ color: #666; margin-top: 5px; }}
        .broken {{ color: #dc3545; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
        th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid #ddd; vertical-align: top; }}
        th {{ background: #f8f9fa; font-weight: 600; }}
        tr:hover {{ background: #f8f9fa; }}
        td {{ word-break: break-word; }}
        .muted {{ color: #6c757d; font-size: 0.9em; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🧭 Orphan Pages</h1>
        <p><strong>Site:</strong> {escape(self.origin)}<br>
        <strong>Date:</strong> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}<br>
        <span class="muted">Pages declared in the site's sitemaps{" or page list" if self.pages_file else ""} that load on their own URL
        but are not linked from any page reachable from the homepage ({" or ".join(self.profiles)} navigation, content, footer).
        The HTML sitemap is not counted as a link.</span></p>

        <div class="summary">
            <div class="stat"><div class="stat-number">{len(self.declared)}</div><div class="stat-label">Declared Pages</div></div>
            <div class="stat"><div class="stat-number">{sum(self.pages_crawled.values())}</div><div class="stat-label">Pages Crawled ({escape(crawled)})</div></div>
            <div class="stat"><div class="stat-number broken">{len(self.orphans)}</div><div class="stat-label">Orphan Pages</div></div>
            <div class="stat"><div class="stat-number">{len(self.not_live)}</div><div class="stat-label">Unlinked but Not Live</div></div>
            <div class="stat"><div class="stat-number">{len(self.crawl_errors)}</div><div class="stat-label">Crawl Errors</div></div>
        </div>
"""
        if self.truncated:
            html += f"""
        <p class="broken"><strong>⚠️ The {" and ".join(self.truncated)} crawl stopped at --max-pages before visiting
        every page, so some pages below may actually be linked. Run without --max-pages for a reliable list.</strong></p>
"""
        if self.orphans:
            html += """
        <h2>Orphan Pages</h2>
        <table>
            <tr><th>#</th><th>Page Title</th><th>Live URL</th><th>Found In</th></tr>
"""
            for i, page in enumerate(self.orphans, 1):
                html += f"""            <tr>
                <td>{i}</td>
                <td>{escape(page.title) or '<span class="muted">(no title)</span>'}</td>
                <td><a href="{escape(page.url)}" target="_blank" rel="noopener">{escape(page.url)}</a></td>
                <td class="muted">{escape(", ".join(sorted(page.sources)))}</td>
            </tr>
"""
            html += "        </table>\n"
        else:
            html += "        <p>No orphan pages found. 🎉</p>\n"

        if self.not_live:
            html += """
        <h2>Unlinked but Not Live</h2>
        <p class="muted">Declared and not linked, but they redirect elsewhere or don't load, so they aren't orphan pages.</p>
        <table>
            <tr><th>URL</th><th>Status</th><th>Redirects To</th><th>Found In</th></tr>
"""
            for page in self.not_live:
                redirect = page.final_url if page.final_url and self.page_key(page.final_url) != self.page_key(page.url) else ""
                html += f"""            <tr>
                <td>{escape(page.url)}</td>
                <td>{escape(str(page.status))}</td>
                <td>{escape(redirect)}</td>
                <td class="muted">{escape(", ".join(sorted(page.sources)))}</td>
            </tr>
"""
            html += "        </table>\n"

        if one_device:
            html += """
        <h2>Linked on One Device Only</h2>
        <p class="muted">Not orphans, but only one of the crawls found a link to them (e.g. a page only in the mobile menu).</p>
        <table>
            <tr><th>URL</th><th>Linked Only On</th><th>First Linked From</th></tr>
"""
            for declared, profile, source in one_device:
                html += f"""            <tr>
                <td>{escape(declared.url)}</td>
                <td>{escape(profile)}</td>
                <td class="muted">{escape(source)}</td>
            </tr>
"""
            html += "        </table>\n"

        if self.crawl_errors:
            html += """
        <h2>Crawl Errors</h2>
        <p class="muted">Pages that failed to load during the crawl. Links on them weren't seen, so orphans linked only from these pages may be false positives.</p>
        <table>
            <tr><th>Device</th><th>URL</th><th>Error</th></tr>
"""
            for profile, url, error in self.crawl_errors:
                html += f"            <tr><td>{escape(profile)}</td><td>{escape(url)}</td><td>{escape(error)}</td></tr>\n"
            html += "        </table>\n"

        html += """
    </div>
</body>
</html>
"""
        with open(filename, "w", encoding="utf-8") as f:
            f.write(html)
        self.log(f"HTML report saved to: {filename}")

    def print_summary(self):
        print("\n" + "=" * 60)
        print("ORPHAN PAGES SUMMARY")
        print("=" * 60)
        print(f"Site:            {self.origin}")
        print(f"Declared pages:  {len(self.declared)}")
        for profile, count in self.pages_crawled.items():
            print(f"{'Crawled ' + profile + ':':<17}{count}")
        print(f"Orphan pages:    {len(self.orphans)}")
        print(f"Not live:        {len(self.not_live)}")
        print(f"Crawl errors:    {len(self.crawl_errors)}")
        print("=" * 60)
        if self.truncated:
            print("WARNING: crawl stopped at --max-pages; some orphans may be false positives.")
        if self.orphans:
            print("\nORPHAN PAGES:")
            for page in self.orphans:
                print(f"  {page.title or '(no title)'}")
                print(f"       {page.url}")


def main():
    parser = argparse.ArgumentParser(
        description=(
            "List pages that are live but not linked from anywhere on the site "
            "(declared in its sitemaps or a page list, unreachable from the homepage)"
        )
    )
    parser.add_argument("url", help="The site's homepage URL")
    parser.add_argument(
        "--html-sitemap",
        action="append",
        metavar="URL",
        help="HTML sitemap page(s) (default: <site>/sitemap.htm). Never used as a link source.",
    )
    parser.add_argument(
        "--xml-sitemap",
        action="append",
        metavar="URL",
        help="XML sitemap(s) (default: <site>/sitemap.xml plus Sitemap: entries in robots.txt)",
    )
    parser.add_argument(
        "--pages-file",
        metavar="FILE",
        help="Text/CSV file with page URLs or paths (e.g. exported from the CMS), one per line",
    )
    parser.add_argument(
        "--desktop-only", action="store_true", help="Skip the mobile crawl (about twice as fast)"
    )
    parser.add_argument(
        "--max-pages", type=int, default=0, help="Max pages to crawl per device (0 = unlimited, default: 0)"
    )
    parser.add_argument(
        "--timeout", type=int, default=60000, help="Page load timeout in ms (default: 60000)"
    )
    parser.add_argument(
        "--output", "-o", default="orphan_pages", help="Output filename (without extension)"
    )
    parser.add_argument(
        "--format", "-f", choices=["html", "csv", "both"], default="both", help="Output format"
    )
    add_skip_args(parser)
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress verbose output")
    args = parser.parse_args()

    finder = OrphanFinder(
        base_url=ensure_scheme(args.url),
        html_sitemaps=[ensure_scheme(u) for u in args.html_sitemap] if args.html_sitemap else None,
        xml_sitemaps=[ensure_scheme(u) for u in args.xml_sitemap] if args.xml_sitemap else None,
        pages_file=args.pages_file or "",
        profiles=("desktop",) if args.desktop_only else ("desktop", "mobile"),
        max_pages=args.max_pages,
        timeout=args.timeout,
        skip_patterns=skip_patterns_from_args(args),
        verbose=not args.quiet,
    )

    try:
        finder.run()
    except KeyboardInterrupt:
        # A partial crawl would report reachable pages as orphans.
        print("\n\nInterrupted: no report written (an incomplete crawl gives false orphans).")
        sys.exit(1)

    finder.print_summary()
    if args.format in ("html", "both"):
        finder.generate_html_report(f"{args.output}.html")
    if args.format in ("csv", "both"):
        finder.generate_csv_report(f"{args.output}.csv")


if __name__ == "__main__":
    main()
