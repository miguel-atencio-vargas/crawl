#!/usr/bin/env python3
"""
Site Link Auditor - Crawls JavaScript-rendered websites and reports broken links.
Uses Playwright to handle dynamic content.

Usage:
    python site_auditor.py https://example.com
    python site_auditor.py https://example.com --max-pages 100 --check-external
    python site_auditor.py https://example.com --search-term GMC
    python site_auditor.py https://example.com --skip-inventory --skip-blogs
"""

import argparse
import csv
import re
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from urllib.parse import urljoin, urlparse

import requests as req_lib

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
except ModuleNotFoundError:
    sync_playwright = None
    PlaywrightTimeout = TimeoutError


# Template-generated pages multiply into thousands of near-identical URLs and
# exhaust --max-pages before editorial pages are reached.
URL_SKIP_PRESETS = {
    "inventory": [
        # Vehicle detail pages: /new/Chevrolet/2026-...-<32 hex id>.htm
        r"/(new|used|certified)/.+-[0-9a-f]{32}\.htm",
        # Faceted inventory searches: /new-inventory/index.htm?model=...
        r"/[\w-]*inventory/[^?]*\?",
    ],
    "blogs": [r"/blog/"],
}


@dataclass
class LinkResult:
    url: str
    status: int | str
    source_page: str
    link_text: str
    is_external: bool
    redirect_url: str = ""
    error: str = ""


@dataclass
class SearchResult:
    page_url: str
    match_type: str
    matched_text: str
    context: str
    target_url: str = ""
    is_external: bool = False


@dataclass
class AuditReport:
    base_url: str
    total_pages_crawled: int = 0
    total_links_checked: int = 0
    broken_links: list = field(default_factory=list)
    redirects: list = field(default_factory=list)
    working_links: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    search_term: str = ""
    search_matches: list = field(default_factory=list)
    skipped_urls: set = field(default_factory=set)


class SiteAuditor:
    def __init__(
        self,
        base_url: str,
        max_pages: int = 0,
        check_external: bool = False,
        timeout: int = 60000,
        verbose: bool = True,
        max_retries: int = 2,
        search_term: str = "",
        skip_patterns: list[str] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.base_domain = urlparse(base_url).netloc
        self.max_pages = max_pages
        self.check_external = check_external
        self.timeout = timeout
        self.verbose = verbose
        self.max_retries = max_retries
        self.search_term = search_term.strip()
        self.search_pattern = (
            re.compile(re.escape(self.search_term), re.IGNORECASE)
            if self.search_term
            else None
        )

        self.skip_patterns = [re.compile(p, re.IGNORECASE) for p in (skip_patterns or [])]

        self.visited_urls: set = set()
        self.checked_links: set = set()
        self.to_crawl: deque = deque()
        self.report = AuditReport(base_url=base_url)
        self.report.search_term = self.search_term

    def log(self, message: str):
        if self.verbose:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

    def is_internal_url(self, url: str) -> bool:
        """Check if URL belongs to the same domain."""
        parsed = urlparse(url)
        return parsed.netloc == self.base_domain or parsed.netloc == ""

    def should_skip(self, url: str) -> bool:
        """Check if URL matches a configured skip pattern."""
        if url.rstrip("/") == self.base_url:
            return False
        parsed = urlparse(url)
        target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        return any(p.search(target) for p in self.skip_patterns)

    def normalize_url(self, url: str, current_page: str) -> str | None:
        """Normalize and validate a URL."""
        # Skip non-http links
        if url.startswith(("mailto:", "tel:", "javascript:", "#", "data:")):
            return None

        # Make absolute URL
        full_url = urljoin(current_page, url)

        # Parse and clean
        parsed = urlparse(full_url)

        # Only process http/https
        if parsed.scheme not in ("http", "https"):
            return None

        # Remove fragments
        clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if parsed.query:
            clean_url += f"?{parsed.query}"

        return clean_url

    def extract_links(self, page) -> list[tuple[str, str]]:
        """Extract all links from the current page."""
        links = []
        try:
            elements = page.query_selector_all("a[href]")
            for element in elements:
                href = element.get_attribute("href")
                text = element.inner_text().strip()[:50] or "[no text]"
                if href:
                    links.append((href, text))
        except Exception as e:
            self.log(f"  Error extracting links: {e}")
        return links

    def extract_page_text(self, page) -> str:
        """Extract visible body text from the current page."""
        try:
            return page.locator("body").inner_text(timeout=5000)
        except Exception as e:
            self.log(f"  Error extracting page text: {e}")
            return ""

    def _snippet_around_match(self, text: str, start: int, end: int, radius: int = 80) -> str:
        """Return a compact, single-line snippet around a text match."""
        snippet_start = max(0, start - radius)
        snippet_end = min(len(text), end + radius)
        snippet = text[snippet_start:snippet_end]
        snippet = re.sub(r"\s+", " ", snippet).strip()
        if snippet_start > 0:
            snippet = "…" + snippet
        if snippet_end < len(text):
            snippet += "…"
        return snippet

    def collect_search_matches(
        self,
        page,
        current_url: str,
        links: list[tuple[str, str]],
    ):
        """Collect page-text and link matches for the configured search term."""
        if not self.search_pattern:
            return

        page_text = self.extract_page_text(page)
        page_matches = list(self.search_pattern.finditer(page_text))
        for match in page_matches:
            self.report.search_matches.append(
                SearchResult(
                    page_url=current_url,
                    match_type="Page Text",
                    matched_text=match.group(0),
                    context=self._snippet_around_match(page_text, match.start(), match.end()),
                )
            )

        for href, link_text in links:
            normalized = self.normalize_url(href, current_url)
            if not normalized:
                continue

            link_blob = f"{link_text} {normalized}"
            link_match = self.search_pattern.search(link_blob)
            if not link_match:
                continue

            matched_text = link_match.group(0)
            context = link_text if self.search_pattern.search(link_text) else normalized
            self.report.search_matches.append(
                SearchResult(
                    page_url=current_url,
                    match_type="Link",
                    matched_text=matched_text,
                    context=context,
                    target_url=normalized,
                    is_external=not self.is_internal_url(normalized),
                )
            )

        if page_matches:
            self.log(f"  Found {len(page_matches)} page-text matches for '{self.search_term}'")

    def check_link(self, url: str, source_page: str, link_text: str) -> LinkResult:
        """Check if a link is working using requests library. Detects redirects by comparing final URL."""
        is_external = not self.is_internal_url(url)
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        link_timeout = 15  # seconds — much shorter than page nav timeout

        try:
            # Try HEAD first, fall back to GET (some servers reject HEAD)
            try:
                resp = req_lib.head(url, timeout=link_timeout, headers=headers, allow_redirects=True)
                if resp.status_code == 405:
                    resp = req_lib.get(url, timeout=link_timeout, headers=headers, allow_redirects=True, stream=True)
            except req_lib.exceptions.ConnectionError:
                resp = req_lib.get(url, timeout=link_timeout, headers=headers, allow_redirects=True, stream=True)

            status = resp.status_code
            final_url = resp.url

            # Detect redirects by comparing the final URL to the original
            redirected = final_url.rstrip("/") != url.rstrip("/")
            redirect_dest = final_url if redirected else ""

            result = LinkResult(
                url=url,
                status=status,
                source_page=source_page,
                link_text=link_text,
                is_external=is_external,
                redirect_url=redirect_dest,
            )

            if redirected:
                self.report.redirects.append(result)
            elif 200 <= status < 300:
                self.report.working_links.append(result)
            elif status >= 400:
                self.report.broken_links.append(result)

            return result

        except Exception as e:
            result = LinkResult(
                url=url,
                status="ERROR",
                source_page=source_page,
                link_text=link_text,
                is_external=is_external,
                error=str(e)[:200],
            )
            self.report.errors.append(result)
            return result

    def check_links_batch(self, links: list[tuple[str, str, str]]):
        """Check a batch of links in parallel using threads."""
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {}
            for url, source_page, link_text in links:
                f = executor.submit(self.check_link, url, source_page, link_text)
                futures[f] = url

            for future in as_completed(futures):
                url = futures[future]
                try:
                    result = future.result()
                    self.report.total_links_checked += 1
                    status_str = str(result.status)
                    if result.status == "ERROR" or (isinstance(result.status, int) and result.status >= 400):
                        self.log(f"  BROKEN [{status_str}] {url[:80]}")
                    elif result.redirect_url:
                        self.log(f"  REDIRECT [{status_str}] {url[:60]} -> {result.redirect_url[:60]}")
                except Exception as e:
                    self.log(f"  Exception checking {url}: {e}")

    def _goto_with_retry(self, page, context, url: str):
        """Navigate to a URL with retry logic and fallback wait strategies."""
        strategies = [
            ("domcontentloaded", self.timeout),
            ("commit", self.timeout),
        ]
        last_error = None
        for wait_until, timeout in strategies:
            try:
                response = page.goto(url, timeout=timeout, wait_until=wait_until)
                # Give JS a moment to render dynamic content
                try:
                    page.wait_for_load_state("load", timeout=5000)
                except PlaywrightTimeout:
                    pass  # load event is optional; domcontentloaded is enough
                page.wait_for_timeout(1500)
                return page, response
            except PlaywrightTimeout:
                self.log(f"  Timeout with wait_until='{wait_until}', retrying...")
                last_error = PlaywrightTimeout(f"Timeout loading {url}")
            except Exception as e:
                # Page might be crashed; create a fresh one
                self.log(f"  Page error ({e}), creating fresh page...")
                try:
                    page.close()
                except Exception:
                    pass
                page = context.new_page()
                last_error = e
        raise last_error

    def crawl(self):
        """Main crawl loop."""
        if sync_playwright is None:
            raise RuntimeError(
                "Playwright is not installed. Install dependencies with "
                "`pip install playwright requests` and then run `playwright install chromium`."
            )

        mode_label = (
            f"content search for '{self.search_term}'"
            if self.search_term
            else "broken-link audit"
        )
        self.log(f"Starting {mode_label} of {self.base_url}")
        pages_label = "unlimited" if self.max_pages == 0 else str(self.max_pages)
        self.log(f"Max pages: {pages_label}, Check external: {self.check_external}")
        if self.skip_patterns:
            self.log(f"Skip patterns: {', '.join(p.pattern for p in self.skip_patterns)}")
        self.log("-" * 60)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = context.new_page()

            # Start with base URL
            self.to_crawl.append(self.base_url)

            while self.to_crawl and (self.max_pages == 0 or self.report.total_pages_crawled < self.max_pages):
                current_url = self.to_crawl.popleft()

                if current_url in self.visited_urls:
                    continue

                self.visited_urls.add(current_url)
                self.report.total_pages_crawled += 1

                pages_label = "inf" if self.max_pages == 0 else str(self.max_pages)
                self.log(f"Crawling [{self.report.total_pages_crawled}/{pages_label}]: {current_url}")

                try:
                    # Navigate to page with retry and fallback strategies
                    page, response = self._goto_with_retry(page, context, current_url)

                    if response is None:
                        self.log(f"  No response from {current_url}")
                        continue

                    # Extract links
                    links = self.extract_links(page)
                    self.log(f"  Found {len(links)} links")

                    self.collect_search_matches(page, current_url, links)

                    to_check = []
                    for href, link_text in links:
                        normalized = self.normalize_url(href, current_url)
                        if not normalized:
                            continue

                        is_external = not self.is_internal_url(normalized)

                        if not is_external and self.should_skip(normalized):
                            self.report.skipped_urls.add(normalized)
                            continue

                        # Add internal links to crawl queue
                        if not is_external and normalized not in self.visited_urls:
                            self.to_crawl.append(normalized)

                        # Queue link for batch checking. In search mode, crawling still
                        # follows internal links, but link validation is intentionally
                        # skipped so the run answers "where does this term appear?"
                        if not self.search_term and normalized not in self.checked_links:
                            if not is_external or self.check_external:
                                self.checked_links.add(normalized)
                                to_check.append((normalized, current_url, link_text))

                    if to_check:
                        self.log(f"  Checking {len(to_check)} links in parallel...")
                        self.check_links_batch(to_check)

                except PlaywrightTimeout:
                    self.log(f"  Timeout loading {current_url} (all retries exhausted)")
                    self.report.errors.append(
                        LinkResult(
                            url=current_url,
                            status="TIMEOUT",
                            source_page="",
                            link_text="",
                            is_external=False,
                            error="Page load timeout after retries",
                        )
                    )
                except Exception as e:
                    self.log(f"  Error crawling {current_url}: {e}")
                    self.report.errors.append(
                        LinkResult(
                            url=current_url,
                            status="ERROR",
                            source_page="",
                            link_text="",
                            is_external=False,
                            error=str(e)[:200],
                        )
                    )

            browser.close()

        self.log("-" * 60)
        self.log("Crawl complete!")

    def generate_csv_report(self, filename: str):
        """Generate a CSV report of all findings."""
        if self.search_term:
            self.generate_search_csv_report(filename)
            return

        with open(filename, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Status", "URL", "Found On", "Link Text", "Type", "Redirects To", "Error"])

            # Write broken links first
            for link in self.report.broken_links:
                writer.writerow([
                    link.status,
                    link.url,
                    link.source_page,
                    link.link_text,
                    "External" if link.is_external else "Internal",
                    link.redirect_url,
                    link.error,
                ])

            # Then errors
            for link in self.report.errors:
                writer.writerow([
                    link.status,
                    link.url,
                    link.source_page,
                    link.link_text,
                    "External" if link.is_external else "Internal",
                    link.redirect_url,
                    link.error,
                ])

            # Then redirects
            for link in self.report.redirects:
                writer.writerow([
                    link.status,
                    link.url,
                    link.source_page,
                    link.link_text,
                    "External" if link.is_external else "Internal",
                    link.redirect_url,
                    "",
                ])

            # Then working links
            for link in self.report.working_links:
                writer.writerow([
                    link.status,
                    link.url,
                    link.source_page,
                    link.link_text,
                    "External" if link.is_external else "Internal",
                    link.redirect_url,
                    "",
                ])

        self.log(f"CSV report saved to: {filename}")

    def generate_search_csv_report(self, filename: str):
        """Generate a CSV report for content-search findings."""
        with open(filename, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "Search Term",
                "Page URL",
                "Match Type",
                "Matched Text",
                "Context",
                "Target URL",
                "Type",
            ])

            for result in self.report.search_matches:
                writer.writerow([
                    self.search_term,
                    result.page_url,
                    result.match_type,
                    result.matched_text,
                    result.context,
                    result.target_url,
                    "External" if result.is_external else "Internal",
                ])

        self.log(f"CSV search report saved to: {filename}")

    def generate_html_report(self, filename: str):
        """Generate an HTML report."""
        if self.search_term:
            self.generate_search_html_report(filename)
            return

        html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Site Audit Report - {self.base_url}</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 40px; background: #f5f5f5; }}
        .container {{ max-width: 1200px; margin: 0 auto; background: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        h1 {{ color: #333; border-bottom: 2px solid #007bff; padding-bottom: 10px; }}
        h2 {{ color: #555; margin-top: 30px; }}
        .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin: 20px 0; }}
        .stat {{ background: #f8f9fa; padding: 20px; border-radius: 8px; text-align: center; }}
        .stat-number {{ font-size: 2em; font-weight: bold; }}
        .stat-label {{ color: #666; margin-top: 5px; }}
        .broken {{ color: #dc3545; }}
        .redirect {{ color: #ffc107; }}
        .ok {{ color: #28a745; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 15px; }}
        th, td {{ padding: 12px; text-align: left; border-bottom: 1px solid #ddd; }}
        th {{ background: #f8f9fa; font-weight: 600; }}
        tr:hover {{ background: #f8f9fa; }}
        .url {{ max-width: 400px; word-break: break-all; }}
        .status-badge {{ padding: 4px 8px; border-radius: 4px; font-size: 0.85em; }}
        .status-4xx {{ background: #f8d7da; color: #721c24; }}
        .status-5xx {{ background: #f8d7da; color: #721c24; }}
        .status-3xx {{ background: #fff3cd; color: #856404; }}
        .status-error {{ background: #e2e3e5; color: #383d41; }}
        .external {{ font-size: 0.8em; color: #6c757d; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🔍 Site Audit Report</h1>
        <p><strong>URL:</strong> {self.base_url}<br>
        <strong>Date:</strong> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        
        <div class="summary">
            <div class="stat">
                <div class="stat-number">{self.report.total_pages_crawled}</div>
                <div class="stat-label">Pages Crawled</div>
            </div>
            <div class="stat">
                <div class="stat-number">{self.report.total_links_checked}</div>
                <div class="stat-label">Links Checked</div>
            </div>
            <div class="stat">
                <div class="stat-number broken">{len(self.report.broken_links)}</div>
                <div class="stat-label">Broken Links</div>
            </div>
            <div class="stat">
                <div class="stat-number redirect">{len(self.report.redirects)}</div>
                <div class="stat-label">Redirects</div>
            </div>
            <div class="stat">
                <div class="stat-number">{len(self.report.errors)}</div>
                <div class="stat-label">Errors</div>
            </div>
            <div class="stat">
                <div class="stat-number">{len(self.report.skipped_urls)}</div>
                <div class="stat-label">URLs Skipped</div>
            </div>
        </div>
"""

        # Broken Links Section
        if self.report.broken_links:
            html += """
        <h2>❌ Broken Links</h2>
        <table>
            <tr><th>Status</th><th>URL</th><th>Found On</th><th>Link Text</th></tr>
"""
            for link in self.report.broken_links:
                ext = ' <span class="external">(external)</span>' if link.is_external else ""
                status_class = "status-4xx" if str(link.status).startswith("4") else "status-5xx"
                html += f"""            <tr>
                <td><span class="status-badge {status_class}">{link.status}</span></td>
                <td class="url">{link.url}{ext}</td>
                <td class="url">{link.source_page}</td>
                <td>{link.link_text[:30]}</td>
            </tr>
"""
            html += "        </table>\n"

        # Errors Section
        if self.report.errors:
            html += """
        <h2>⚠️ Errors</h2>
        <table>
            <tr><th>Status</th><th>URL</th><th>Found On</th><th>Error</th></tr>
"""
            for link in self.report.errors:
                html += f"""            <tr>
                <td><span class="status-badge status-error">{link.status}</span></td>
                <td class="url">{link.url}</td>
                <td class="url">{link.source_page}</td>
                <td>{link.error}</td>
            </tr>
"""
            html += "        </table>\n"

        # Redirects Section
        if self.report.redirects:
            html += """
        <h2>Redirects</h2>
        <table>
            <tr><th>Status</th><th>Original URL</th><th>Redirects To</th><th>Found On</th><th>Link Text</th></tr>
"""
            for link in self.report.redirects:
                ext = ' <span class="external">(external)</span>' if link.is_external else ""
                html += f"""            <tr>
                <td><span class="status-badge status-3xx">{link.status}</span></td>
                <td class="url">{link.url}{ext}</td>
                <td class="url">{link.redirect_url}</td>
                <td class="url">{link.source_page}</td>
                <td>{link.link_text[:30]}</td>
            </tr>
"""
            html += "        </table>\n"

        html += """
    </div>
</body>
</html>
"""

        with open(filename, "w", encoding="utf-8") as f:
            f.write(html)

        self.log(f"HTML report saved to: {filename}")

    def generate_search_html_report(self, filename: str):
        """Generate an HTML report for content-search findings."""
        safe_base_url = escape(self.base_url)
        safe_search_term = escape(self.search_term)
        html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Site Search Report - {safe_base_url}</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 40px; background: #f5f5f5; }}
        .container {{ max-width: 1200px; margin: 0 auto; background: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        h1 {{ color: #333; border-bottom: 2px solid #007bff; padding-bottom: 10px; }}
        h2 {{ color: #555; margin-top: 30px; }}
        .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin: 20px 0; }}
        .stat {{ background: #f8f9fa; padding: 20px; border-radius: 8px; text-align: center; }}
        .stat-number {{ font-size: 2em; font-weight: bold; color: #007bff; }}
        .stat-label {{ color: #666; margin-top: 5px; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 15px; }}
        th, td {{ padding: 12px; text-align: left; border-bottom: 1px solid #ddd; vertical-align: top; }}
        th {{ background: #f8f9fa; font-weight: 600; }}
        tr:hover {{ background: #f8f9fa; }}
        .url {{ max-width: 360px; word-break: break-all; }}
        .context {{ max-width: 420px; }}
        .badge {{ display: inline-block; padding: 4px 8px; border-radius: 4px; background: #e7f1ff; color: #084298; font-size: 0.85em; }}
        .external {{ font-size: 0.8em; color: #6c757d; }}
        mark {{ background: #fff3cd; padding: 0 2px; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🔎 Site Search Report</h1>
        <p><strong>URL:</strong> {safe_base_url}<br>
        <strong>Search term:</strong> {safe_search_term}<br>
        <strong>Date:</strong> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>

        <div class="summary">
            <div class="stat">
                <div class="stat-number">{self.report.total_pages_crawled}</div>
                <div class="stat-label">Pages Crawled</div>
            </div>
            <div class="stat">
                <div class="stat-number">{len(self.report.search_matches)}</div>
                <div class="stat-label">Matches Found</div>
            </div>
            <div class="stat">
                <div class="stat-number">{len([m for m in self.report.search_matches if m.match_type == 'Page Text'])}</div>
                <div class="stat-label">Page Text Matches</div>
            </div>
            <div class="stat">
                <div class="stat-number">{len([m for m in self.report.search_matches if m.match_type == 'Link'])}</div>
                <div class="stat-label">Link Matches</div>
            </div>
            <div class="stat">
                <div class="stat-number">{len(self.report.skipped_urls)}</div>
                <div class="stat-label">URLs Skipped</div>
            </div>
        </div>
"""

        if self.report.search_matches:
            html += """
        <h2>Matches</h2>
        <table>
            <tr><th>Type</th><th>Page</th><th>Context</th><th>Target URL</th></tr>
"""
            for result in self.report.search_matches:
                ext = ' <span class="external">(external)</span>' if result.is_external else ""
                safe_context = escape(result.context)
                safe_context = self.search_pattern.sub(
                    lambda match: f"<mark>{escape(match.group(0))}</mark>",
                    safe_context,
                )
                target = escape(result.target_url) + ext if result.target_url else ""
                html += f"""            <tr>
                <td><span class="badge">{escape(result.match_type)}</span></td>
                <td class="url">{escape(result.page_url)}</td>
                <td class="context">{safe_context}</td>
                <td class="url">{target}</td>
            </tr>
"""
            html += "        </table>\n"
        else:
            html += f"        <p>No matches found for <strong>{safe_search_term}</strong>.</p>\n"

        html += """
    </div>
</body>
</html>
"""

        with open(filename, "w", encoding="utf-8") as f:
            f.write(html)

        self.log(f"HTML search report saved to: {filename}")

    def print_summary(self):
        """Print a summary to console."""
        if self.search_term:
            self.print_search_summary()
            return

        print("\n" + "=" * 60)
        print("AUDIT SUMMARY")
        print("=" * 60)
        print(f"Base URL:        {self.base_url}")
        print(f"Pages crawled:   {self.report.total_pages_crawled}")
        print(f"Links checked:   {self.report.total_links_checked}")
        print(f"Broken links:    {len(self.report.broken_links)}")
        print(f"Redirects:       {len(self.report.redirects)}")
        print(f"Errors:          {len(self.report.errors)}")
        print(f"URLs skipped:    {len(self.report.skipped_urls)}")
        print("=" * 60)

        if self.report.broken_links:
            print("\nBROKEN LINKS:")
            for link in self.report.broken_links[:20]:
                print(f"  [{link.status}] {link.url}")
                print(f"       Found on: {link.source_page}")
            if len(self.report.broken_links) > 20:
                print(f"  ... and {len(self.report.broken_links) - 20} more")

        if self.report.redirects:
            print("\nREDIRECTS:")
            for link in self.report.redirects[:20]:
                print(f"  [{link.status}] {link.url}")
                print(f"       -> {link.redirect_url}")
                print(f"       Found on: {link.source_page}")
            if len(self.report.redirects) > 20:
                print(f"  ... and {len(self.report.redirects) - 20} more")

        if self.report.errors:
            print("\nERRORS:")
            for link in self.report.errors[:20]:
                print(f"  [{link.status}] {link.url}")
                print(f"       Error: {link.error}")
            if len(self.report.errors) > 20:
                print(f"  ... and {len(self.report.errors) - 20} more")

    def print_search_summary(self):
        """Print a content-search summary to console."""
        text_matches = [m for m in self.report.search_matches if m.match_type == "Page Text"]
        link_matches = [m for m in self.report.search_matches if m.match_type == "Link"]

        print("\n" + "=" * 60)
        print("SEARCH SUMMARY")
        print("=" * 60)
        print(f"Base URL:        {self.base_url}")
        print(f"Search term:     {self.search_term}")
        print(f"Pages crawled:   {self.report.total_pages_crawled}")
        print(f"Matches found:   {len(self.report.search_matches)}")
        print(f"Page text:       {len(text_matches)}")
        print(f"Links:           {len(link_matches)}")
        print(f"URLs skipped:    {len(self.report.skipped_urls)}")
        print("=" * 60)

        if self.report.search_matches:
            print("\nMATCHES:")
            for result in self.report.search_matches[:20]:
                print(f"  [{result.match_type}] {result.page_url}")
                if result.target_url:
                    print(f"       Target: {result.target_url}")
                print(f"       Context: {result.context[:160]}")
            if len(self.report.search_matches) > 20:
                print(f"  ... and {len(self.report.search_matches) - 20} more")


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Crawl a website and check for broken links, or search crawled pages "
            "for a word/phrase (handles JavaScript sites)"
        )
    )
    parser.add_argument("url", help="The URL to audit")
    parser.add_argument(
        "--max-pages", type=int, default=0, help="Maximum pages to crawl (0 = unlimited, default: 0)"
    )
    parser.add_argument(
        "--check-external", action="store_true", help="Also check external links"
    )
    parser.add_argument(
        "--timeout", type=int, default=60000, help="Timeout in ms (default: 60000)"
    )
    parser.add_argument(
        "--output", "-o", default="audit_report", help="Output filename (without extension)"
    )
    parser.add_argument(
        "--format", "-f", choices=["html", "csv", "both"], default="both", help="Output format"
    )
    parser.add_argument(
        "--search-term",
        help=(
            "Search crawled pages and discovered links for this word/phrase instead "
            "of validating links, e.g. --search-term GMC"
        ),
    )
    parser.add_argument(
        "--skip-inventory",
        action="store_true",
        help="Skip template-generated inventory pages (vehicle detail pages and faceted searches)",
    )
    parser.add_argument(
        "--skip-blogs", action="store_true", help="Skip blog pages (anything under /blog/)"
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="REGEX",
        help="Skip internal URLs whose path+query matches this regex (repeatable)",
    )
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress verbose output")

    args = parser.parse_args()

    # Ensure URL has scheme
    url = args.url
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    skip_patterns = list(args.exclude)
    if args.skip_inventory:
        skip_patterns += URL_SKIP_PRESETS["inventory"]
    if args.skip_blogs:
        skip_patterns += URL_SKIP_PRESETS["blogs"]

    auditor = SiteAuditor(
        base_url=url,
        max_pages=args.max_pages,
        check_external=args.check_external,
        timeout=args.timeout,
        verbose=not args.quiet,
        search_term=args.search_term or "",
        skip_patterns=skip_patterns,
    )

    try:
        auditor.crawl()
        auditor.print_summary()

        if args.format in ("html", "both"):
            auditor.generate_html_report(f"{args.output}.html")
        if args.format in ("csv", "both"):
            auditor.generate_csv_report(f"{args.output}.csv")

    except KeyboardInterrupt:
        print("\n\nCrawl interrupted by user.")
        auditor.print_summary()
        auditor.generate_html_report(f"{args.output}_partial.html")
        auditor.generate_csv_report(f"{args.output}_partial.csv")


if __name__ == "__main__":
    main()
