#!/usr/bin/env python3
"""
Site Link Auditor - Crawls JavaScript-rendered websites and reports broken links.
Uses Playwright to handle dynamic content.

Usage:
    python site_auditor.py https://example.com
    python site_auditor.py https://example.com --max-pages 100 --check-external
"""

import argparse
import csv
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests as req_lib
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout


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
class AuditReport:
    base_url: str
    total_pages_crawled: int = 0
    total_links_checked: int = 0
    broken_links: list = field(default_factory=list)
    redirects: list = field(default_factory=list)
    working_links: list = field(default_factory=list)
    errors: list = field(default_factory=list)


class SiteAuditor:
    def __init__(
        self,
        base_url: str,
        max_pages: int = 0,
        check_external: bool = False,
        timeout: int = 60000,
        verbose: bool = True,
        max_retries: int = 2,
    ):
        self.base_url = base_url.rstrip("/")
        self.base_domain = urlparse(base_url).netloc
        self.max_pages = max_pages
        self.check_external = check_external
        self.timeout = timeout
        self.verbose = verbose
        self.max_retries = max_retries

        self.visited_urls: set = set()
        self.checked_links: set = set()
        self.to_crawl: deque = deque()
        self.report = AuditReport(base_url=base_url)

    def log(self, message: str):
        if self.verbose:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

    def is_internal_url(self, url: str) -> bool:
        """Check if URL belongs to the same domain."""
        parsed = urlparse(url)
        return parsed.netloc == self.base_domain or parsed.netloc == ""

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
        self.log(f"Starting audit of {self.base_url}")
        pages_label = "unlimited" if self.max_pages == 0 else str(self.max_pages)
        self.log(f"Max pages: {pages_label}, Check external: {self.check_external}")
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

                    to_check = []
                    for href, link_text in links:
                        normalized = self.normalize_url(href, current_url)
                        if not normalized:
                            continue

                        is_external = not self.is_internal_url(normalized)

                        # Add internal links to crawl queue
                        if not is_external and normalized not in self.visited_urls:
                            self.to_crawl.append(normalized)

                        # Queue link for batch checking
                        if normalized not in self.checked_links:
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

    def generate_html_report(self, filename: str):
        """Generate an HTML report."""
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

    def print_summary(self):
        """Print a summary to console."""
        print("\n" + "=" * 60)
        print("AUDIT SUMMARY")
        print("=" * 60)
        print(f"Base URL:        {self.base_url}")
        print(f"Pages crawled:   {self.report.total_pages_crawled}")
        print(f"Links checked:   {self.report.total_links_checked}")
        print(f"Broken links:    {len(self.report.broken_links)}")
        print(f"Redirects:       {len(self.report.redirects)}")
        print(f"Errors:          {len(self.report.errors)}")
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


def main():
    parser = argparse.ArgumentParser(
        description="Crawl a website and check for broken links (handles JavaScript sites)"
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
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress verbose output")

    args = parser.parse_args()

    # Ensure URL has scheme
    url = args.url
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    auditor = SiteAuditor(
        base_url=url,
        max_pages=args.max_pages,
        check_external=args.check_external,
        timeout=args.timeout,
        verbose=not args.quiet,
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
