"""
Shared helpers for the crawl tools (site_auditor.py, orphan_finder.py):
browser navigation, URL normalization and the URL skip filters.
"""

import argparse
import re
from urllib.parse import urljoin, urlparse

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
except ModuleNotFoundError:
    sync_playwright = None
    PlaywrightTimeout = TimeoutError


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

PLAYWRIGHT_MISSING = (
    "Playwright is not installed. Install dependencies with "
    "`pip install playwright requests` and then run `playwright install chromium`."
)

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


def ensure_scheme(url: str) -> str:
    """Assume https:// when the URL has no scheme."""
    return url if url.startswith(("http://", "https://")) else "https://" + url


def normalize_url(url: str, current_page: str) -> str | None:
    """Make a link absolute and drop its #fragment; None for non-http(s) links."""
    if url.startswith(("mailto:", "tel:", "javascript:", "#", "data:")):
        return None

    parsed = urlparse(urljoin(current_page, url))
    if parsed.scheme not in ("http", "https"):
        return None

    clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    if parsed.query:
        clean_url += f"?{parsed.query}"
    return clean_url


def add_skip_args(parser: argparse.ArgumentParser):
    """Add --skip-inventory, --skip-blogs and --exclude to a CLI."""
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


def skip_patterns_from_args(args) -> list[str]:
    """Regexes for the skip flags added by add_skip_args()."""
    patterns = list(args.exclude)
    if args.skip_inventory:
        patterns += URL_SKIP_PRESETS["inventory"]
    if args.skip_blogs:
        patterns += URL_SKIP_PRESETS["blogs"]
    return patterns


def compile_skip_patterns(patterns: list[str] | None) -> list[re.Pattern]:
    return [re.compile(p, re.IGNORECASE) for p in (patterns or [])]


def matches_skip(url: str, skip_patterns: list[re.Pattern]) -> bool:
    """True if the URL's path+query matches any skip pattern."""
    parsed = urlparse(url)
    target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    return any(p.search(target) for p in skip_patterns)


def goto_with_retry(page, context, url: str, timeout: int, log):
    """Navigate to a URL with fallback wait strategies; returns (page, response).

    A crashed page is replaced with a fresh one, so callers must use the
    returned page.
    """
    last_error = None
    for wait_until in ("domcontentloaded", "commit"):
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
            log(f"  Timeout with wait_until='{wait_until}', retrying...")
            last_error = PlaywrightTimeout(f"Timeout loading {url}")
        except Exception as e:
            # Page might be crashed; create a fresh one
            log(f"  Page error ({e}), creating fresh page...")
            try:
                page.close()
            except Exception:
                pass
            page = context.new_page()
            last_error = e
    raise last_error
