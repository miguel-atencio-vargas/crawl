#!/usr/bin/env python3
"""
Site Link Auditor - Crawls JavaScript-rendered websites and reports broken links.
Uses Playwright to handle dynamic content.

Usage:
    python site_auditor.py https://example.com
    python site_auditor.py https://example.com --max-pages 100 --check-external
    python site_auditor.py https://example.com --search-term GMC
    python site_auditor.py https://example.com --skip-inventory --skip-blogs
    python site_auditor.py https://example.com/sitemap.htm --sitemap-only --check-images --skip-inventory
    python site_auditor.py https://example.com --alt-only
    python site_auditor.py https://example.com --list-links --skip-template-links
"""

import argparse
import csv
import html as html_lib
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

# Link-text words too generic to say anything about where a link should go.
GENERIC_LINK_WORDS = {
    "the", "and", "for", "our", "you", "your", "with", "from", "this", "that",
    "now", "more", "view", "click", "here", "learn", "shop", "see", "all", "get",
    "visit", "read", "find", "out", "today", "details", "info", "information", "page",
}

# Links in these landmarks are site template (repeated on every page).
TEMPLATE_SECTIONS = {"header", "nav", "footer"}

# A link (same URL + text) on at least this share of crawled pages is template too.
TEMPLATE_REPEAT_RATIO = 0.8
TEMPLATE_REPEAT_MIN_PAGES = 5


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
class ImageResult:
    page_url: str
    image_url: str
    missing: str
    alt: str | None
    title: str | None


@dataclass
class LinkEntry:
    page_url: str
    name: str
    url: str
    link_type: str  # Internal, External, Email, Phone
    section: str
    new_tab: bool
    count: int = 1


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
    images_checked: int = 0
    image_issues: list = field(default_factory=list)
    link_entries: list = field(default_factory=list)
    # Normalized URL (no trailing slash) -> {"status", "final_url", "title"}
    destinations: dict = field(default_factory=dict)


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
        check_images: bool = False,
        image_attrs: tuple[str, ...] = ("alt", "title"),
        sitemap_only: bool = False,
        list_links: bool = False,
        skip_template_links: bool = False,
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

        self.skip_patterns = compile_skip_patterns(skip_patterns)
        self.check_images = check_images
        self.image_attrs = image_attrs
        self.image_label = "/".join(image_attrs)  # "alt/title", "alt" or "title"
        self.sitemap_only = sitemap_only
        self.list_links = list_links
        self.skip_template_links = skip_template_links
        # Search, image and link-list modes answer a different question, so
        # they skip the broken-link validation.
        self.validate_links = not (self.search_term or self.check_images or self.list_links)
        self._link_rows_cache: list[dict] | None = None

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
        return matches_skip(url, self.skip_patterns)

    def normalize_url(self, url: str, current_page: str) -> str | None:
        """Normalize and validate a URL."""
        return normalize_url(url, current_page)

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

    def collect_image_issues(self, page, current_url: str):
        """Record <img> elements whose checked attributes (alt/title) are missing or empty."""
        if not self.check_images:
            return

        try:
            images = page.evaluate(
                """() => Array.from(document.images).map(img => ({
                    src: img.currentSrc || img.getAttribute('src') || img.getAttribute('data-src') || '',
                    alt: img.getAttribute('alt'),
                    title: img.getAttribute('title'),
                    // 1x1 tracking pixels are not content images
                    pixel: (img.complete && img.naturalWidth === 1 && img.naturalHeight === 1)
                        || (img.getAttribute('width') === '1' && img.getAttribute('height') === '1'),
                }))"""
            )
        except Exception as e:
            self.log(f"  Error extracting images: {e}")
            return

        issues = 0
        for img in images:
            if img["pixel"]:
                continue
            self.report.images_checked += 1

            missing = []
            for attr in self.image_attrs:
                value = img[attr]
                if value is None:
                    missing.append(attr)
                elif not value.strip():
                    missing.append(f"{attr} (empty)")
            if not missing:
                continue

            src = img["src"]
            if src.startswith("data:"):
                image_url = src[:60] + "…"  # inline images can be huge
            else:
                image_url = urljoin(current_url, src) if src else ""
            self.report.image_issues.append(
                ImageResult(
                    page_url=current_url,
                    image_url=image_url or "[no src]",
                    missing=", ".join(missing),
                    alt=img["alt"],
                    title=img["title"],
                )
            )
            issues += 1

        if issues:
            self.log(f"  Found {issues} images missing {self.image_label}")

    def collect_link_entries(self, page, current_url: str):
        """Record every link on the page with its text, section and target."""
        if not self.list_links:
            return

        try:
            raw_links = page.evaluate(
                """() => Array.from(document.querySelectorAll('a[href]')).map(a => {
                    const img = a.querySelector('img[alt]');
                    const name = (a.innerText || '').replace(/\\s+/g, ' ').trim()
                        || a.getAttribute('aria-label') || a.getAttribute('title')
                        || (img && img.getAttribute('alt')) || '';
                    const landmarks = [
                        ['header', 'header, [role=banner]'],
                        ['footer', 'footer, [role=contentinfo]'],
                        ['nav', 'nav, [role=navigation]'],
                        ['aside', 'aside, [role=complementary]'],
                        ['main', 'main, [role=main]'],
                    ];
                    const hit = landmarks.find(([, sel]) => a.closest(sel));
                    return {
                        href: a.getAttribute('href'),
                        name: name.trim().slice(0, 150),
                        section: hit ? hit[0] : 'body',
                        newTab: a.getAttribute('target') === '_blank',
                    };
                })"""
            )
        except Exception as e:
            self.log(f"  Error extracting link list: {e}")
            return

        entries: dict[tuple, LinkEntry] = {}
        for raw in raw_links:
            href = (raw["href"] or "").strip()
            if href.startswith(("mailto:", "tel:")):
                url = href
                link_type = "Email" if href.startswith("mailto:") else "Phone"
            else:
                url = self.normalize_url(href, current_url)
                if not url:
                    continue  # javascript:, #anchors, data:
                link_type = "Internal" if self.is_internal_url(url) else "External"
                if link_type == "Internal" and self.should_skip(url):
                    self.report.skipped_urls.add(url)

            name = raw["name"] or "[no text]"
            key = (url, name, raw["section"])
            if key in entries:
                entries[key].count += 1
            else:
                entries[key] = LinkEntry(
                    page_url=current_url,
                    name=name,
                    url=url,
                    link_type=link_type,
                    section=raw["section"],
                    new_tab=raw["newTab"],
                )

        self.report.link_entries.extend(entries.values())
        self._link_rows_cache = None

    def record_destination(self, url: str, status, final_url: str, title: str):
        """Remember where a URL ended up, so links to it can be compared against it."""
        self.report.destinations[url.rstrip("/")] = {
            "status": status,
            "final_url": final_url,
            "title": title,
        }

    def fetch_destination(self, url: str):
        """Fetch a page that wasn't crawled to learn its status, final URL and <title>."""
        headers = {"User-Agent": USER_AGENT}
        try:
            resp = req_lib.get(url, timeout=15, headers=headers, allow_redirects=True, stream=True)
            head = resp.raw.read(262144, decode_content=True).decode(resp.encoding or "utf-8", "replace")
            resp.close()
            match = re.search(r"<title[^>]*>(.*?)</title>", head, re.IGNORECASE | re.DOTALL)
            title = re.sub(r"\s+", " ", html_lib.unescape(match.group(1))).strip() if match else ""
            self.record_destination(url, resp.status_code, resp.url, title)
        except Exception as e:
            self.record_destination(url, "ERROR", "", str(e)[:200])

    def resolve_link_destinations(self):
        """Fetch destinations of listed links that the crawl didn't visit."""
        if not self.list_links:
            return

        pending = set()
        for entry in self.report.link_entries:
            if entry.link_type not in ("Internal", "External"):
                continue
            if entry.url.rstrip("/") in self.report.destinations:
                continue
            if entry.link_type == "External" and not self.check_external:
                continue
            if entry.link_type == "Internal" and self.should_skip(entry.url):
                continue
            pending.add(entry.url)

        if not pending:
            return
        self.log(f"Fetching {len(pending)} link destinations that weren't crawled...")
        with ThreadPoolExecutor(max_workers=10) as executor:
            list(executor.map(self.fetch_destination, pending))
        self._link_rows_cache = None

    def _is_homepage(self, url: str) -> bool:
        return urlparse(url).path in ("", "/", "/index.htm", "/index.html")

    def _link_notes(self, entry: LinkEntry, dest: dict | None) -> list[str]:
        """Heuristics that point at links going to the wrong page."""
        if not dest:
            return []
        status = dest["status"]
        if status in ("ERROR", "TIMEOUT"):
            return [f"error: {dest['title']}" if status == "ERROR" and dest["title"] else "error"]

        notes = []
        if isinstance(status, int) and status >= 400:
            notes.append(f"broken ({status})")
        final_url = dest["final_url"] or entry.url
        if self._is_homepage(final_url) and not self._is_homepage(entry.url):
            notes.append("redirects to homepage")

        if not self._is_homepage(entry.url) and not notes:
            words = [
                w for w in re.findall(r"[a-z0-9]+", entry.name.lower())
                if len(w) >= 3 and w not in GENERIC_LINK_WORDS
            ]
            haystack = f"{dest['title']} {urlparse(final_url).path} {urlparse(final_url).query}"
            haystack = re.sub(r"[-_/+=&?.%]", " ", haystack.lower())
            if words and not any((w[:5] if len(w) > 5 else w) in haystack for w in words):
                notes.append("text not found in destination")
        return notes

    def link_rows(self) -> list[dict]:
        """Listed links enriched with destination info, template flag and notes."""
        if self._link_rows_cache is not None:
            return self._link_rows_cache

        pages = {e.page_url for e in self.report.link_entries}
        pages_per_link: dict[tuple, set] = {}
        for e in self.report.link_entries:
            pages_per_link.setdefault((e.url, e.name), set()).add(e.page_url)
        repeat_threshold = (
            max(TEMPLATE_REPEAT_MIN_PAGES, TEMPLATE_REPEAT_RATIO * len(pages))
            if len(pages) >= TEMPLATE_REPEAT_MIN_PAGES
            else float("inf")
        )

        rows = []
        for e in self.report.link_entries:
            template = (
                e.section in TEMPLATE_SECTIONS
                or len(pages_per_link[(e.url, e.name)]) >= repeat_threshold
            )
            if template and self.skip_template_links:
                continue
            dest = self.report.destinations.get(e.url.rstrip("/"))
            if dest is None and e.link_type == "Internal" and self.should_skip(e.url):
                status = "SKIPPED"
            else:
                status = dest["status"] if dest else ""
            final_url = dest["final_url"] if dest else ""
            rows.append({
                "entry": e,
                "template": template,
                "status": status,
                "lands_on": final_url if final_url and final_url.rstrip("/") != e.url.rstrip("/") else "",
                "dest_title": dest["title"] if dest and dest["status"] != "ERROR" else "",
                "notes": self._link_notes(e, dest),
            })
        self._link_rows_cache = rows
        return rows

    def check_link(self, url: str, source_page: str, link_text: str) -> LinkResult:
        """Check if a link is working using requests library. Detects redirects by comparing final URL."""
        is_external = not self.is_internal_url(url)
        headers = {"User-Agent": USER_AGENT}
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
        return goto_with_retry(page, context, url, self.timeout, self.log)

    def crawl(self):
        """Main crawl loop."""
        if sync_playwright is None:
            raise RuntimeError(PLAYWRIGHT_MISSING)

        if self.search_term:
            mode_label = f"content search for '{self.search_term}'"
        elif self.check_images:
            mode_label = f"image {self.image_label} audit"
        elif self.list_links:
            mode_label = "link listing"
        else:
            mode_label = "broken-link audit"
        self.log(f"Starting {mode_label} of {self.base_url}")
        if self.sitemap_only:
            self.log("Sitemap only: visiting pages linked from the start URL, not following further")
        pages_label = "unlimited" if self.max_pages == 0 else str(self.max_pages)
        self.log(f"Max pages: {pages_label}, Check external: {self.check_external}")
        if self.skip_patterns:
            self.log(f"Skip patterns: {', '.join(p.pattern for p in self.skip_patterns)}")
        self.log("-" * 60)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=USER_AGENT
            )
            page = context.new_page()

            # Start with base URL (depth 0)
            self.to_crawl.append((self.base_url, 0))

            while self.to_crawl and (self.max_pages == 0 or self.report.total_pages_crawled < self.max_pages):
                current_url, depth = self.to_crawl.popleft()

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
                    self.collect_image_issues(page, current_url)
                    if self.list_links:
                        self.record_destination(current_url, response.status, page.url, page.title())
                        self.collect_link_entries(page, current_url)

                    # With --sitemap-only, only the start page's links are followed.
                    follow_links = not self.sitemap_only or depth == 0
                    if not follow_links and not self.validate_links:
                        continue

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
                        if follow_links and not is_external and normalized not in self.visited_urls:
                            self.to_crawl.append((normalized, depth + 1))

                        # Queue link for batch checking. In search and image modes,
                        # crawling still follows internal links, but link validation
                        # is intentionally skipped.
                        if self.validate_links and normalized not in self.checked_links:
                            if not is_external or self.check_external:
                                self.checked_links.add(normalized)
                                to_check.append((normalized, current_url, link_text))

                    if to_check:
                        self.log(f"  Checking {len(to_check)} links in parallel...")
                        self.check_links_batch(to_check)

                except PlaywrightTimeout:
                    self.log(f"  Timeout loading {current_url} (all retries exhausted)")
                    self.record_destination(current_url, "TIMEOUT", "", "")
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
                    self.record_destination(current_url, "ERROR", "", str(e)[:200])
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

        self.resolve_link_destinations()
        self.log("-" * 60)
        self.log("Crawl complete!")

    def generate_csv_report(self, filename: str):
        """Generate a CSV report of all findings."""
        if self.search_term:
            self.generate_search_csv_report(filename)
            return
        if self.check_images:
            self.generate_images_csv_report(filename)
            return
        if self.list_links:
            self.generate_links_csv_report(filename)
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

    def generate_images_csv_report(self, filename: str):
        """Generate a CSV report with one row per image with a missing attribute."""
        with open(filename, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Page URL", "Image URL", "Missing", "Alt", "Title"])
            for result in self.report.image_issues:
                writer.writerow([
                    result.page_url,
                    result.image_url,
                    result.missing,
                    "" if result.alt is None else result.alt,
                    "" if result.title is None else result.title,
                ])

        self.log(f"CSV image report saved to: {filename}")

    def flagged_link_groups(self) -> list[tuple[dict, list[str]]]:
        """Flagged links, each listed once with the pages it appears on."""
        groups: dict[tuple, tuple[dict, list[str]]] = {}
        for r in self.link_rows():
            if not r["notes"]:
                continue
            e = r["entry"]
            key = (e.url, e.name, tuple(r["notes"]))
            if key not in groups:
                groups[key] = (r, [])
            if e.page_url not in groups[key][1]:
                groups[key][1].append(e.page_url)
        return sorted(groups.values(), key=lambda g: -len(g[1]))

    def generate_links_csv_report(self, filename: str):
        """Generate a CSV report with one row per link per page."""
        page_titles = {url: d["title"] for url, d in self.report.destinations.items()}
        with open(filename, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "Page URL", "Page Title", "Section", "Template", "Link Text", "Link URL",
                "Type", "New Tab", "Count", "Status", "Lands On", "Destination Title", "Notes",
            ])
            for row in self.link_rows():
                e = row["entry"]
                writer.writerow([
                    e.page_url,
                    page_titles.get(e.page_url.rstrip("/"), ""),
                    e.section,
                    "Yes" if row["template"] else "No",
                    e.name,
                    e.url,
                    e.link_type,
                    "Yes" if e.new_tab else "",
                    e.count,
                    row["status"],
                    row["lands_on"],
                    row["dest_title"],
                    "; ".join(row["notes"]),
                ])

        self.log(f"CSV link list saved to: {filename}")

    def _group_image_issues(self) -> list[tuple[str, str, list[str]]]:
        """Group image issues by (image URL, missing) -> pages, most widespread first."""
        groups: dict[tuple[str, str], list[str]] = {}
        for result in self.report.image_issues:
            pages = groups.setdefault((result.image_url, result.missing), [])
            if result.page_url not in pages:
                pages.append(result.page_url)
        return sorted(
            ((image_url, missing, pages) for (image_url, missing), pages in groups.items()),
            key=lambda g: -len(g[2]),
        )

    def _count_missing(self, attr: str) -> int:
        return sum(
            1 for r in self.report.image_issues
            if any(part.startswith(attr) for part in r.missing.split(", "))
        )

    def generate_html_report(self, filename: str):
        """Generate an HTML report."""
        if self.search_term:
            self.generate_search_html_report(filename)
            return
        if self.check_images:
            self.generate_images_html_report(filename)
            return
        if self.list_links:
            self.generate_links_html_report(filename)
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

    def generate_images_html_report(self, filename: str):
        """Generate an HTML report of images with missing attributes, grouped by image."""
        safe_base_url = escape(self.base_url)
        groups = self._group_image_issues()
        pages_with_issues = len({r.page_url for r in self.report.image_issues})
        missing_stats = "".join(
            f"""            <div class="stat">
                <div class="stat-number broken">{self._count_missing(attr)}</div>
                <div class="stat-label">Missing {attr.capitalize()}</div>
            </div>
"""
            for attr in self.image_attrs
        )
        html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Image {self.image_label.title()} Report - {safe_base_url}</title>
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
        table {{ width: 100%; border-collapse: collapse; margin-top: 15px; }}
        th, td {{ padding: 12px; text-align: left; border-bottom: 1px solid #ddd; vertical-align: top; }}
        th {{ background: #f8f9fa; font-weight: 600; }}
        tr:hover {{ background: #f8f9fa; }}
        .url {{ max-width: 380px; word-break: break-all; }}
        .thumb {{ max-width: 120px; max-height: 80px; background: #eee; }}
        .badge {{ display: inline-block; padding: 4px 8px; border-radius: 4px; background: #f8d7da; color: #721c24; font-size: 0.85em; white-space: nowrap; }}
        details ul {{ margin: 6px 0 0; padding-left: 18px; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🖼️ Image {self.image_label.title()} Report</h1>
        <p><strong>URL:</strong> {safe_base_url}{" (sitemap only)" if self.sitemap_only else ""}<br>
        <strong>Date:</strong> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>

        <div class="summary">
            <div class="stat">
                <div class="stat-number">{self.report.total_pages_crawled}</div>
                <div class="stat-label">Pages Crawled</div>
            </div>
            <div class="stat">
                <div class="stat-number">{self.report.images_checked}</div>
                <div class="stat-label">Images Checked</div>
            </div>
{missing_stats}            <div class="stat">
                <div class="stat-number">{pages_with_issues}</div>
                <div class="stat-label">Pages With Issues</div>
            </div>
            <div class="stat">
                <div class="stat-number">{len(self.report.skipped_urls)}</div>
                <div class="stat-label">URLs Skipped</div>
            </div>
        </div>
"""

        if groups:
            html += f"""
        <h2>Images Missing {self.image_label.title()}</h2>
        <p>Each image is listed once, with the pages where it appears. The CSV has one row per page.</p>
        <table>
            <tr><th>Preview</th><th>Image URL</th><th>Missing</th><th>Found On</th></tr>
"""
            for image_url, missing, pages in groups:
                safe_image = escape(image_url)
                preview = (
                    f'<img class="thumb" src="{safe_image}" loading="lazy" alt="">'
                    if image_url.startswith(("http://", "https://"))
                    else ""
                )
                page_items = "".join(f"<li>{escape(p)}</li>" for p in pages)
                label = "1 page" if len(pages) == 1 else f"{len(pages)} pages"
                html += f"""            <tr>
                <td>{preview}</td>
                <td class="url">{safe_image}</td>
                <td><span class="badge">{escape(missing)}</span></td>
                <td class="url"><details><summary>{label}</summary><ul>{page_items}</ul></details></td>
            </tr>
"""
            html += "        </table>\n"
        else:
            html += f"        <p>No images missing {self.image_label}. 🎉</p>\n"

        html += """
    </div>
</body>
</html>
"""

        with open(filename, "w", encoding="utf-8") as f:
            f.write(html)

        self.log(f"HTML image report saved to: {filename}")

    def generate_links_html_report(self, filename: str):
        """Generate an HTML report: flagged links first, then every link grouped by page."""
        safe_base_url = escape(self.base_url)
        rows = self.link_rows()
        flagged = self.flagged_link_groups()
        by_page: dict[str, list[dict]] = {}
        for r in rows:
            by_page.setdefault(r["entry"].page_url, []).append(r)
        template_count = sum(1 for r in rows if r["template"])
        template_label = "Template Links Hidden" if self.skip_template_links else "Template Links"
        if self.skip_template_links:
            template_count = len(self.report.link_entries) - len(rows)

        def link_cell(r):
            e = r["entry"]
            ext = ' <span class="muted">(new tab)</span>' if e.new_tab else ""
            return f'<a href="{escape(e.url)}" target="_blank" rel="noopener">{escape(e.url)}</a>{ext}'

        def dest_cell(r):
            parts = []
            if r["status"] != "":
                parts.append(f'<span class="badge">{escape(str(r["status"]))}</span>')
            if r["lands_on"]:
                parts.append(f'→ {escape(r["lands_on"])}')
            if r["dest_title"]:
                parts.append(f'<div class="title">{escape(r["dest_title"])}</div>')
            return " ".join(parts)

        def notes_cell(r):
            return "".join(f'<span class="flag">{escape(n)}</span>' for n in r["notes"])

        html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Link List - {safe_base_url}</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 40px; background: #f5f5f5; }}
        .container {{ max-width: 1400px; margin: 0 auto; background: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        h1 {{ color: #333; border-bottom: 2px solid #007bff; padding-bottom: 10px; }}
        h2 {{ color: #555; margin-top: 30px; }}
        .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 20px; margin: 20px 0; }}
        .stat {{ background: #f8f9fa; padding: 20px; border-radius: 8px; text-align: center; }}
        .stat-number {{ font-size: 2em; font-weight: bold; }}
        .stat-label {{ color: #666; margin-top: 5px; }}
        .broken {{ color: #dc3545; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 0.92em; }}
        th, td {{ padding: 8px 10px; text-align: left; border-bottom: 1px solid #ddd; vertical-align: top; }}
        th {{ background: #f8f9fa; font-weight: 600; }}
        tr:hover {{ background: #f8f9fa; }}
        td {{ word-break: break-word; }}
        a {{ color: #0d6efd; }}
        .muted {{ color: #6c757d; font-size: 0.85em; }}
        .title {{ color: #333; margin-top: 3px; }}
        .badge {{ display: inline-block; padding: 2px 6px; border-radius: 4px; background: #e9ecef; font-size: 0.85em; }}
        .flag {{ display: inline-block; margin: 0 4px 4px 0; padding: 2px 6px; border-radius: 4px; background: #f8d7da; color: #721c24; font-size: 0.85em; }}
        .tpl {{ color: #6c757d; }}
        td details {{ border: none; margin: 0; padding: 0; }}
        td details ul {{ margin: 6px 0 0; padding-left: 18px; }}
        details {{ border: 1px solid #e5e5e5; border-radius: 6px; margin: 10px 0; padding: 8px 12px; }}
        summary {{ cursor: pointer; font-weight: 600; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🔗 Link List</h1>
        <p><strong>URL:</strong> {safe_base_url}{" (sitemap only)" if self.sitemap_only else ""}<br>
        <strong>Date:</strong> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>

        <div class="summary">
            <div class="stat"><div class="stat-number">{self.report.total_pages_crawled}</div><div class="stat-label">Pages Crawled</div></div>
            <div class="stat"><div class="stat-number">{len(rows)}</div><div class="stat-label">Links Listed</div></div>
            <div class="stat"><div class="stat-number broken">{len(flagged)}</div><div class="stat-label">Flagged Links (unique)</div></div>
            <div class="stat"><div class="stat-number">{template_count}</div><div class="stat-label">{template_label}</div></div>
            <div class="stat"><div class="stat-number">{len(self.report.skipped_urls)}</div><div class="stat-label">URLs Skipped</div></div>
        </div>
"""

        if flagged:
            html += """
        <h2>⚠️ Flagged Links</h2>
        <p class="muted">Hints, not verdicts: check that each link goes where its text says. Each link is listed once; the CSV has one row per page.</p>
        <table>
            <tr><th>Link Text</th><th>Link URL</th><th>Destination</th><th>Notes</th><th>Found On</th></tr>
"""
            for r, pages in flagged:
                page_items = "".join(f"<li>{escape(p)}</li>" for p in pages)
                label = "1 page" if len(pages) == 1 else f"{len(pages)} pages"
                html += f"""            <tr>
                <td>{escape(r["entry"].name)}</td>
                <td>{link_cell(r)}</td>
                <td>{dest_cell(r)}</td>
                <td>{notes_cell(r)}</td>
                <td><details><summary>{label}</summary><ul>{page_items}</ul></details></td>
            </tr>
"""
            html += "        </table>\n"

        html += """
        <h2>Links by Page</h2>
"""
        for page_url, page_rows in by_page.items():
            page_title = self.report.destinations.get(page_url.rstrip("/"), {}).get("title", "")
            n_flagged = sum(1 for r in page_rows if r["notes"])
            flag_label = f' · <span class="broken">{n_flagged} flagged</span>' if n_flagged else ""
            html += f"""        <details>
            <summary>{escape(page_url)} <span class="muted">{escape(page_title)} · {len(page_rows)} links</span>{flag_label}</summary>
            <table>
                <tr><th>Section</th><th>Link Text</th><th>Link URL</th><th>Type</th><th>Destination</th><th>Notes</th></tr>
"""
            for r in page_rows:
                e = r["entry"]
                section = escape(e.section) + (' <span class="tpl">(template)</span>' if r["template"] else "")
                count = f' <span class="muted">×{e.count}</span>' if e.count > 1 else ""
                html += f"""                <tr>
                    <td>{section}</td>
                    <td>{escape(e.name)}{count}</td>
                    <td>{link_cell(r)}</td>
                    <td>{escape(e.link_type)}</td>
                    <td>{dest_cell(r)}</td>
                    <td>{notes_cell(r)}</td>
                </tr>
"""
            html += """            </table>
        </details>
"""

        html += """
    </div>
</body>
</html>
"""

        with open(filename, "w", encoding="utf-8") as f:
            f.write(html)

        self.log(f"HTML link list saved to: {filename}")

    def print_summary(self):
        """Print a summary to console."""
        if self.search_term:
            self.print_search_summary()
            return
        if self.check_images:
            self.print_images_summary()
            return
        if self.list_links:
            self.print_links_summary()
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


    def print_images_summary(self):
        """Print an image attribute summary to console."""
        groups = self._group_image_issues()

        print("\n" + "=" * 60)
        print(f"IMAGE {self.image_label.upper()} SUMMARY")
        print("=" * 60)
        print(f"Base URL:        {self.base_url}")
        print(f"Pages crawled:   {self.report.total_pages_crawled}")
        print(f"Images checked:  {self.report.images_checked}")
        for attr in self.image_attrs:
            print(f"{'Missing ' + attr + ':':<17}{self._count_missing(attr)}")
        print(f"Unique images:   {len(groups)}")
        print(f"URLs skipped:    {len(self.report.skipped_urls)}")
        print("=" * 60)

        if groups:
            print(f"\nIMAGES MISSING {self.image_label.upper()}:")
            for image_url, missing, pages in groups[:20]:
                print(f"  [{missing}] {image_url}")
                print(f"       On {len(pages)} page(s), e.g. {pages[0]}")
            if len(groups) > 20:
                print(f"  ... and {len(groups) - 20} more")

    def print_links_summary(self):
        """Print a link-list summary to console."""
        rows = self.link_rows()
        flagged = self.flagged_link_groups()

        print("\n" + "=" * 60)
        print("LINK LIST SUMMARY")
        print("=" * 60)
        print(f"Base URL:        {self.base_url}")
        print(f"Pages crawled:   {self.report.total_pages_crawled}")
        print(f"Links listed:    {len(rows)}")
        if self.skip_template_links:
            print(f"Template hidden: {len(self.report.link_entries) - len(rows)}")
        else:
            print(f"Template links:  {sum(1 for r in rows if r['template'])}")
        print(f"Flagged links:   {len(flagged)} unique")
        print(f"URLs skipped:    {len(self.report.skipped_urls)}")
        print("=" * 60)

        if flagged:
            print("\nFLAGGED LINKS:")
            for r, pages in flagged[:20]:
                e = r["entry"]
                print(f"  [{'; '.join(r['notes'])}] \"{e.name}\" -> {e.url}")
                if r["dest_title"]:
                    print(f"       Destination: {r['dest_title']}")
                print(f"       On {len(pages)} page(s), e.g. {pages[0]}")
            if len(flagged) > 20:
                print(f"  ... and {len(flagged) - 20} more")


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Crawl a website and check for broken links, search crawled pages "
            "for a word/phrase, find images missing alt/title, or list every link per "
            "page (handles JavaScript sites)"
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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--search-term",
        help=(
            "Search crawled pages and discovered links for this word/phrase instead "
            "of validating links, e.g. --search-term GMC"
        ),
    )
    mode.add_argument(
        "--check-images",
        action="store_true",
        help="Report <img> elements missing alt or title instead of validating links",
    )
    mode.add_argument(
        "--list-links",
        action="store_true",
        help=(
            "List every link on every page (text, URL, section, where it lands) and "
            "flag links that may point to the wrong page, instead of validating links"
        ),
    )
    parser.add_argument(
        "--skip-template-links",
        action="store_true",
        help="With --list-links, leave out header/nav/footer links and links repeated on most pages",
    )
    image_attr = parser.add_mutually_exclusive_group()
    image_attr.add_argument(
        "--alt-only",
        action="store_true",
        help="Image mode, checking only the alt attribute (implies --check-images)",
    )
    image_attr.add_argument(
        "--title-only",
        action="store_true",
        help="Image mode, checking only the title attribute (implies --check-images)",
    )
    parser.add_argument(
        "--sitemap-only",
        action="store_true",
        help=(
            "Only visit the start URL and the internal pages it links to (e.g. pass "
            "the site's /sitemap.htm); don't follow links any further"
        ),
    )
    add_skip_args(parser)
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress verbose output")

    args = parser.parse_args()

    if args.alt_only or args.title_only:
        if args.search_term or args.list_links:
            parser.error("--alt-only/--title-only cannot be combined with --search-term or --list-links")
        args.check_images = True
    if args.skip_template_links and not args.list_links:
        parser.error("--skip-template-links only works with --list-links")
    if args.alt_only:
        image_attrs = ("alt",)
    elif args.title_only:
        image_attrs = ("title",)
    else:
        image_attrs = ("alt", "title")

    auditor = SiteAuditor(
        base_url=ensure_scheme(args.url),
        max_pages=args.max_pages,
        check_external=args.check_external,
        timeout=args.timeout,
        verbose=not args.quiet,
        search_term=args.search_term or "",
        skip_patterns=skip_patterns_from_args(args),
        check_images=args.check_images,
        image_attrs=image_attrs,
        sitemap_only=args.sitemap_only,
        list_links=args.list_links,
        skip_template_links=args.skip_template_links,
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
