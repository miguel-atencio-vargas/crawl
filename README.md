# Site Link Auditor

Crawl a website with a real (headless) browser and get an HTML/CSV report of one of:

- **Broken links** — 4xx/5xx, redirects and connection errors (default mode), or
- **Where a word or phrase appears** — e.g. every page that mentions `"Royal Motors"` (`--search-term`), or
- **Images missing `alt` and/or `title`** (`--check-images`, or `--alt-only` / `--title-only`), or
- **Every link on every page** — text, URL, where it lands — with links that may go to the wrong page flagged (`--list-links`).

Any mode can be limited to the pages listed on the site's sitemap page (`--sitemap-only`).

A second tool, **`orphan_finder.py`**, lists **orphan pages**: pages that are live but not linked from anywhere on the site (see [Orphan pages](#orphan-pages-orphan_finderpy)).

Because it uses Playwright + Chromium, it sees links and text rendered by JavaScript that plain HTTP scrapers miss.

---

## Quick start

```bash
# one-time setup
conda create -n linkchecker python=3.11 -y
conda activate linkchecker
pip install playwright requests
playwright install chromium

# every run
conda activate linkchecker
mkdir -p reports
python site_auditor.py https://example.com -o reports/example
```

Open `reports/example.html` in a browser (or `reports/example.csv` in a spreadsheet).

---

## Installation

Requires **Python 3.10+**.

### With conda (recommended)

1. Install [Miniconda](https://docs.anaconda.com/miniconda/install/) if you don't have it.
2. Clone and set up:

   ```bash
   git clone <repo-url>
   cd crawl
   conda create -n linkchecker python=3.11 -y
   conda activate linkchecker          # prompt now shows (linkchecker)
   pip install playwright requests
   playwright install chromium         # downloads the headless browser
   ```

### With a plain virtualenv

```bash
python3 -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install playwright requests
playwright install chromium
```

> Forgot `playwright install chromium`? You'll get `Executable doesn't exist at ...`. Run it inside the same environment.

---

## Cheat sheet

| I want to… | Command |
|---|---|
| Find broken internal links | `python site_auditor.py https://example.com -o reports/example` |
| …and external links too | `python site_auditor.py https://example.com --check-external -o reports/example` |
| Find every page mentioning a word | `python site_auditor.py https://example.com --search-term GMC -o reports/gmc` |
| Find live pages not linked anywhere (orphans) | `python orphan_finder.py https://example.com --skip-inventory -o reports/orphans` |
| Find a multi-word phrase | `python site_auditor.py https://example.com --search-term "Royal Motors" -o reports/royal` |
| Find images without alt/title | `python site_auditor.py https://example.com --check-images -o reports/images` |
| …only missing alt | `python site_auditor.py https://example.com --alt-only -o reports/alt` |
| …only missing title | `python site_auditor.py https://example.com --title-only -o reports/title` |
| List every link per page and flag wrong destinations | `python site_auditor.py https://example.com --list-links -o reports/links` |
| …only content links (no header/footer/nav) | `python site_auditor.py https://example.com/sitemap.htm --sitemap-only --skip-inventory --list-links --skip-template-links -o reports/links` |
| …only on sitemap pages, without inventory | `python site_auditor.py https://example.com/sitemap.htm --sitemap-only --skip-inventory --check-images -o reports/images` |
| Test quickly on a few pages | add `--max-pages 20` |
| Ignore dealer inventory / blog pages | add `--skip-inventory --skip-blogs` |
| Ignore any other section | add `--exclude "/events/"` (repeatable) |
| Only one report format | add `-f html` or `-f csv` |
| Less console noise | add `-q` |
| Slow site | add `--timeout 90000` |

Full example:

```bash
python site_auditor.py https://example.com \
  --check-external --skip-inventory --skip-blogs \
  --max-pages 200 --timeout 90000 \
  -o reports/example_audit -f both
```

---

## The modes

### 1. Broken-link audit (default)

Crawls every internal page reachable from the starting URL and checks each link it finds.

- Internal links are always checked; external links only with `--check-external`.
- Each unique URL is checked once (first page where it was found is reported as *Found On*).
- Results are classified as **broken** (4xx/5xx), **redirect** (final URL differs from the link), **error** (request failed / page timed out) or **working**.

### 2. Content search (`--search-term`)

Crawls the same way but **does not check links**. Instead it reports:

- **Page Text** matches — the term appears in the page's visible text.
- **Link** matches — the term appears in a link's text or URL (internal or external links).

Notes:

- Case-insensitive, literal match (no regex). `GMC` also matches `gmc` and `GMC's`, and substrings like `"GMC"` inside `"GMCertified"`.
- Only **visible text** is searched — not meta tags, `alt`/`title` attributes, or the raw HTML source.
- Header/footer content is repeated on every page, so a term in the nav produces one match per page crawled.
- Links that point to skipped URLs (see below) are still searched on the pages where they appear; the skipped pages themselves are not opened.

### 3. Image alt/title audit (`--check-images`)

Crawls the same way but **does not check links**. Instead it reports every `<img>` on each page whose `alt` or `title` attribute is missing or empty.

| Flag | Checks |
|---|---|
| `--check-images` | both `alt` and `title` (an image is reported if either is missing) |
| `--alt-only` | only `alt` |
| `--title-only` | only `title` |

`--alt-only` and `--title-only` turn on image mode by themselves (no need to also pass `--check-images`) and can't be used together.

- **Missing** column values: `alt`, `title`, `alt (empty)`, `title (empty)`, or combinations like `alt, title` (only the attributes being checked).
- `alt=""` is reported as `alt (empty)`. It is valid for purely decorative images, so review those before fixing.
- 1×1 tracking pixels are ignored. Lazy-loaded images are covered when the URL is in `src` or `data-src`.
- Only `<img>` tags are checked — not CSS background images or inline `<svg>`. Images the site only adds after scrolling may be missed.
- Shared images (logo, header, footer) appear on every page. The HTML report lists each image **once** with the pages it appears on; the CSV has one row per page.
- None of the image flags can be combined with `--search-term` or `--list-links`.

### 4. Link list (`--list-links`)

Crawls the same way but, instead of a pass/fail check, **lists every link on every page** and tells you where each one actually lands, so you can spot links that go to the wrong page (e.g. "Get Pre-Approved" pointing to the Service page).

For each link it records:

| Field | Meaning |
|---|---|
| Link Text | Visible text; falls back to `aria-label`, `title`, or the `alt` of an image inside the link. `[no text]` if none. |
| Link URL | Absolute URL (`#fragment` removed). `tel:` and `mailto:` links are listed too; `javascript:` and `#anchor` links are not. |
| Section | Where on the page the link is: `header`, `nav`, `footer`, `aside`, `main`, or `body` (none of those). |
| Template | `Yes` for links in `header`/`nav`/`footer`, or repeated (same text + URL) on ≥ 80% of crawled pages (once at least 5 pages are crawled). Catches headers/footers built from plain `<div>`s. |
| Destination | HTTP status, the final URL if it redirects (**Lands On**), and the destination page's `<title>`. |
| Notes | Why the link was flagged (see below). |

**Flags** (hints, not verdicts — always confirm by opening the link):

| Note | Meaning |
|---|---|
| `broken (404)` | Destination returns 4xx/5xx. |
| `error` | Destination couldn't be loaded. |
| `redirects to homepage` | Link points to an inner page but ends up on the homepage — typical of a removed page. |
| `text not found in destination` | None of the meaningful words in the link text (ignoring generic ones like "view", "more", "click here") appear in the destination's title or URL. E.g. "Get Pre-Approved" → *Service Department*. Expect some false positives (e.g. "Meet our team" → *About Us*). |

How destinations are resolved:

- Pages the crawl visits use the browser's result (final URL and title after JavaScript).
- Internal links the crawl didn't visit (e.g. links on the pages listed by `--sitemap-only`) are fetched once with a plain HTTP request to get status, final URL and `<title>`.
- External links are only resolved with `--check-external`. Links to skipped URLs (`--skip-inventory`, etc.) are listed with status `SKIPPED` and are not resolved.

`--skip-template-links` leaves template links out of the report, so it only shows each page's own content links. Without it, template links are listed and marked `(template)`.

A link repeated on the same page with the same text and section is listed once, with a **Count**.

### Limiting to the sitemap (`--sitemap-only`)

Pass the site's sitemap page as the URL and add `--sitemap-only`: the tool opens the sitemap, visits each internal page it links to, and **does not follow links any further**. Works with every mode.

Dealer sitemaps also list hundreds of vehicle pages; add `--skip-inventory` to drop them and keep only the editorial pages:

```bash
python site_auditor.py https://osteensubaruofvaldostasoa.cms.dealer.com/sitemap.htm \
  --sitemap-only --skip-inventory --check-images \
  -o reports/osteen_images
```

- The sitemap page itself is also checked.
- Inventory landing pages (e.g. `/new-inventory/index.htm`) are still visited; add `--exclude "inventory"` to drop those too.
- It works with HTML sitemap pages (a page of links), not `sitemap.xml` files.
- In audit mode, the links on each listed page are still checked; only the crawl stops at one level.

---

## Orphan pages (`orphan_finder.py`)

Lists pages that are **live but not linked from anywhere on the site**: not in the desktop or mobile navigation, homepage content, footer, or any other page reachable from the homepage. For each one: **page title and live URL**.

```bash
python orphan_finder.py https://example.com --skip-inventory -o reports/example_orphans
```

A crawler can't discover orphan pages by following links (nothing links to them), so the tool compares two lists:

1. **Declared pages** — every page the site says it has:
   - the HTML sitemap (`/sitemap.htm`, or `--html-sitemap URL`),
   - the XML sitemap (`/sitemap.xml` plus any `Sitemap:` listed in `robots.txt`, or `--xml-sitemap URL`; sitemap indexes are followed),
   - optionally, a page list exported from the CMS (`--pages-file pages.csv`).
2. **Linked pages** — every page linked from a page reachable from the homepage. The site is crawled twice, **as desktop and as mobile** (phone screen and user agent), since sites can render different menus for each. Links hidden in collapsed menus count, as they are in the page.

**Orphan = declared, not linked by either crawl, and loads with 200 on its own URL.** Declared pages that redirect or don't load are listed separately as *Unlinked but Not Live*; pages the site kept rate limiting are listed as *Could Not Verify*.

Rules that matter:

- **The HTML sitemap is never a link source.** It links every page, so counting it would hide every orphan. The sitemap page is visited, but its links are ignored.
- **Pages are matched by path**, ignoring case, query string, trailing slash, `index.htm` and host. So `/About.htm`, `/about.htm?utm=x` and `https://www.live-domain.com/about.htm` are the same page. Hosts found in `sitemap.xml` (e.g. the live domain when you audit the `cms.dealer.com` host) count as the same site.
- A link that redirects makes its destination linked too.
- Files (`.pdf`, images, …) are never reported.
- The skip flags (`--skip-inventory`, `--skip-blogs`, `--exclude`) apply to both lists: skipped pages are neither crawled nor reported. Use `--skip-inventory` on dealer sites, or every vehicle page in `sitemap.xml` is checked.

**Pages hidden from the sitemaps** are the most likely orphans, and only a CMS export finds them. Pass any text or CSV file with one URL or path (`/page.htm`) per line; the first cell that looks like a URL/path is used and other columns or headers are ignored:

```bash
python orphan_finder.py https://example.com --skip-inventory --pages-file cms_pages.csv -o reports/orphans
```

**Report**

- **HTML**: summary (declared pages, pages loaded per device, orphans, could not verify, not live, crawl errors), then:
  - **⚠️ This list is not reliable yet** — shown when the result can't be trusted as is: the crawl hit `--max-pages`, pages failed to load during the crawl (their links weren't seen), one device crawl loaded far fewer pages than the other (it failed, e.g. blocked on mobile), or pages couldn't be verified. **Don't send the list to a client while this box is shown**; fix the cause and re-run.
  - **Orphan Pages**: #, page title, live URL, where it was declared.
  - **Could Not Verify**: unlinked pages that still answered 429/503 after retries. Probably orphans; check by hand or re-run with a higher `--delay`.
  - **Unlinked but Not Live**: status and redirect target.
  - **Linked on One Device Only**: not orphans, but only reachable on desktop *or* mobile (e.g. a page only in the mobile menu). Hidden when a device crawl failed, since every page would show up here.
  - **Crawl Errors**: pages that failed to load during the crawl (HTTP errors, rate limits, timeouts).
- **CSV**: one row per orphan with `Page Title`, `Live URL`, `Found In`, ready to send to the client (drop `Found In` if not needed). Unverified pages go to `<output>_unverified.csv`.

**Rate limiting**

Dealer sites (and CDNs in general) answer `429 Too Many Requests` when pages are loaded too fast, and a rate-limited page looks like a page with no links. The tool therefore:

- waits `--delay` seconds between page loads (default 2);
- on a 429/503, waits (`Retry-After` if the site sends it, else 15 s, 45 s, 90 s) and retries up to 3 times, and doubles the delay for the rest of the run (up to 10 s);
- records pages that still fail as crawl errors or *Could Not Verify*, and warns.

If the report still shows rate-limit warnings, re-run with a higher delay, e.g. `--delay 5`. A full run of a ~300-page dealer site takes roughly 30–60 minutes for both devices.

**Options**

| Option | Default | Description |
|---|---|---|
| `url` | *(required)* | Site homepage. Run it against the **live domain** to get live URLs in the report. |
| `--html-sitemap URL` | `<site>/sitemap.htm` | HTML sitemap page(s); repeatable. |
| `--xml-sitemap URL` | `<site>/sitemap.xml` + `robots.txt` | XML sitemap(s); repeatable. |
| `--pages-file FILE` | — | Extra declared pages, e.g. a CMS export. |
| `--desktop-only` | off | Skip the mobile crawl (about twice as fast). |
| `--delay SECONDS` | `2` | Pause between page loads; doubles after each 429 (up to 10). Raise it if you still get rate limited. |
| `--max-pages N` | `0` (unlimited) | Max pages per device crawl. **Leave unlimited for real audits**: a cut-short crawl reports linked pages as orphans (the report warns when this happens). |
| `--timeout MS` | `60000` | Page load timeout. |
| `-o`, `-f`, `-q` | | Output path, format and quiet mode, as in `site_auditor.py`. |
| `--skip-inventory`, `--skip-blogs`, `--exclude` | | See [Skipping pages](#skipping-pages). |

Notes:

- Ctrl+C stops without writing a report: a partial crawl would list reachable pages as orphans.
- Only real `<a href>` links count. A page reachable only through a button with a JavaScript click handler, a form, or a search box is reported as an orphan.
- Takes roughly two full crawls plus one page load per candidate orphan. Use `--desktop-only` for a quicker first pass.

## Skipping pages

Large sites (dealer sites especially) generate thousands of near-identical pages that eat the whole `--max-pages` budget before the important pages are reached. Skipped URLs are neither crawled nor link-checked; their count is shown as **URLs Skipped**.

| Flag | Skips | Still crawled |
|---|---|---|
| `--skip-inventory` | Vehicle detail pages: `/new\|used\|certified/...-<32 hex id>.htm`<br>Faceted searches: any `…inventory/…?query` (e.g. `/new-inventory/index.htm?model=Tahoe`) | Inventory landing pages without a query, e.g. `/new-inventory/index.htm` |
| `--skip-blogs` | Anything with `/blog/` in the path | — |
| `--exclude REGEX` | Internal URLs whose **path + query** match the regex (case-insensitive, matches anywhere) | — |

`--exclude` examples:

```bash
--exclude "/events/"                 # a section
--exclude "\.pdf$"                   # a file type
--exclude "^/es/" --exclude "^/fr/"  # several patterns (repeat the flag)
```

The starting URL is never skipped.

---

## Options

| Option | Default | Description |
|---|---|---|
| `url` | *(required)* | Starting URL. If you omit the scheme, `https://` is assumed. |
| `--max-pages N` | `0` (unlimited) | Stop after crawling N pages (failed/404 pages count too). |
| `--check-external` | off | Also check links to other domains (audit mode only). |
| `--timeout MS` | `60000` | Page **load** timeout in milliseconds. Link checks use a fixed 15 s timeout. |
| `-o`, `--output PATH` | `audit_report` | Report path **without extension**. The directory must already exist. |
| `-f`, `--format` | `both` | `html`, `csv` or `both`. |
| `--search-term TEXT` | — | Switch to content-search mode. Quote multi-word phrases. |
| `--check-images` | off | Switch to image mode: report `<img>` elements missing `alt` or `title`. |
| `--alt-only` | off | Image mode checking only `alt` (implies `--check-images`). |
| `--title-only` | off | Image mode checking only `title` (implies `--check-images`). |
| `--list-links` | off | Link-list mode: every link per page with its destination, flagging possible wrong links. |
| `--skip-template-links` | off | With `--list-links`, leave out header/nav/footer and site-wide repeated links. |
| `--sitemap-only` | off | Only visit the start URL and the internal pages it links to (pass the sitemap page as `url`). |
| `--skip-inventory` | off | Skip vehicle detail pages and faceted inventory searches. |
| `--skip-blogs` | off | Skip `/blog/` pages. |
| `--exclude REGEX` | — | Skip internal URLs matching REGEX (repeatable). |
| `-q`, `--quiet` | off | Hide per-page logging (the final summary is always printed). |
| `-h`, `--help` | | Show help. |

---

## Output

Each run prints a summary to the console and writes `<output>.html` and/or `<output>.csv`.

> ⚠️ Create the output directory first (`mkdir -p reports`). If it doesn't exist the script crashes **after** the crawl finishes and the results are lost.

### Broken-link reports

**HTML**: summary cards (pages crawled, links checked, broken, redirects, errors, URLs skipped) plus tables for broken links, errors and redirects. Working links are only in the CSV.

**CSV** — one row per checked link, ordered broken → errors → redirects → working:

| Column | Meaning |
|---|---|
| Status | HTTP status, or `ERROR` / `TIMEOUT`. For redirects this is the status of the **final** page (usually `200`), not the 301/302. |
| URL | The link that was checked (or the page that failed to load). |
| Found On | Page where the link was first found (empty for pages that failed to load). |
| Link Text | Anchor text (first 50 chars, `[no text]` if empty). |
| Type | `Internal` or `External`. |
| Redirects To | Final URL, if it redirected. |
| Error | Error message, if the request failed. |

### Search reports

**HTML**: summary cards (pages crawled, total matches, page-text matches, link matches, URLs skipped) and a matches table with the term highlighted.

**CSV** — one row per match:

| Column | Meaning |
|---|---|
| Search Term | The term as you typed it. |
| Page URL | Page where the match was found. |
| Match Type | `Page Text` or `Link`. |
| Matched Text | The matched text with its original casing. |
| Context | ~80 characters around a text match, or the link text/URL for link matches. |
| Target URL | Link destination (link matches only). |
| Type | `Internal`/`External` for the target link (always `Internal` for page-text matches). |

### Image reports

**HTML**: summary cards (pages crawled, images checked, missing alt and/or missing title, pages with issues, URLs skipped) and a table with one row per image: preview, URL, what's missing, and a collapsible list of pages where it appears. Most widespread images first.

**CSV** — one row per image per page:

| Column | Meaning |
|---|---|
| Page URL | Page where the image appears. |
| Image URL | Absolute image URL (`[no src]` if none). |
| Missing | `alt`, `title`, `alt (empty)`, `title (empty)` or a combination. |
| Alt | Current `alt` value (blank if missing or empty). |
| Title | Current `title` value (blank if missing or empty). |

Counts in the summary (Missing Alt / Missing Title) are per image occurrence, so a logo without a title on 40 pages counts 40.

### Link-list reports

**HTML**: summary cards (pages crawled, links listed, flagged links, template links, URLs skipped), then:

1. **Flagged Links** — each suspicious link listed **once**, with destination, notes and a collapsible list of pages where it appears (a wrong header link shows up as one row with "40 pages").
2. **Links by Page** — one collapsible block per page (with its title, link count and number flagged) containing every link on it.

**CSV** — one row per link per page, ready to filter:

| Column | Meaning |
|---|---|
| Page URL / Page Title | Page the link is on. |
| Section | `header`, `nav`, `footer`, `aside`, `main` or `body`. |
| Template | `Yes` / `No` (see above). |
| Link Text / Link URL | The link. |
| Type | `Internal`, `External`, `Email` or `Phone`. |
| New Tab | `Yes` if it opens in a new tab (`target="_blank"`). |
| Count | Times the same link appears in that section of the page. |
| Status | Destination HTTP status, `ERROR`, `TIMEOUT`, `SKIPPED`, or blank if not resolved. |
| Lands On | Final URL, only when it differs from the link URL. |
| Destination Title | `<title>` of the destination page. |
| Notes | Flags, separated by `;`. Filter non-empty Notes to get the suspicious links. |

### Interrupting a run

Press **Ctrl+C** at any time: the summary is printed and partial reports are saved as `<output>_partial.html` and `<output>_partial.csv` (always both formats, regardless of `-f`).
In link-list mode, destinations of links the crawl hadn't visited yet are left blank in partial reports.

---

## How it works

1. **Crawl** — Headless Chromium opens the starting URL, waits for the DOM plus ~1.5 s for JavaScript, and collects every `a[href]`. Internal links (same host) are queued breadth-first (with `--sitemap-only`, only the start page's links are queued). `mailto:`, `tel:`, `javascript:`, `#anchors` and `data:` links are ignored; `#fragments` are stripped. If a page times out it retries once with a lighter wait strategy before recording a `TIMEOUT`.
2. **Check links** (audit mode) — new links from each page are checked in parallel (10 workers) with an HTTP `HEAD`, falling back to `GET` if the server answers 405 or refuses the connection. Redirects are followed and detected by comparing the final URL with the original.
3. **Search** (search mode) — instead of step 2, the page's visible text and its links are scanned for the term.
   **Images** (image mode) — instead of step 2, every `<img>` in the rendered page is checked for `alt` and `title`.
   **Link list** (link-list mode) — instead of step 2, every link is recorded with its section; after the crawl, destinations not visited are fetched and each link is compared against where it lands.
4. **Report** — summary to console, then HTML/CSV files.

---

## Troubleshooting & gotchas

| Symptom | Cause / fix |
|---|---|
| Crawl stops after 1 page | The site redirects to another host (e.g. `example.com` → `www.example.com`) and absolute links are on that host, which counts as **external**. Start with the exact host the site uses: `https://www.example.com`. |
| `ERR_SSL_PROTOCOL_ERROR` on the first page | You omitted the scheme on an HTTP-only site; `https://` was assumed. Pass `http://...` explicitly. |
| `FileNotFoundError: ... .html` at the end | Output directory doesn't exist. `mkdir -p reports` before running. |
| `Executable doesn't exist at ...` | Run `playwright install chromium` in the active environment. |
| `Playwright is not installed` | Activate the environment (`conda activate linkchecker`) or `pip install playwright requests`. |
| Run never ends on a big site | Use `--max-pages`, `--skip-inventory`, `--skip-blogs`, `--exclude`, and avoid `--check-external` (the slowest part). |
| Many `TIMEOUT` errors | Slow server — raise `--timeout` (e.g. `90000`). |
| Same page reported twice | A redirecting URL and its destination are crawled as separate pages (e.g. `/old` → `/new`). Also query-string variants (`?a=1`, `?a=2`) are different URLs. |
| `--sitemap-only` visits just 1 page | The sitemap's links weren't found (check the `Found N links` line for the sitemap). Make sure the URL is the HTML sitemap page, and see the next row. |
| `Found 0 links` on a page that clearly has links | The site renders navigation without real `<a href>` elements (buttons, click handlers) or inside iframes. The tool can't follow those; audit the target pages directly by passing their URLs as the starting `url`. |
| Some links reported broken but work in a browser | Some servers block automated requests (403/429) or reject `HEAD`. Open the URL manually to confirm before fixing. |

---

## Project structure

```
crawl/
├── site_auditor.py    # Broken links, search, images, link list
├── orphan_finder.py   # Orphan pages (live but not linked)
├── crawl_common.py    # Shared by both: navigation, URL helpers, skip filters
├── README.md
└── reports/           # Your reports (create it; ignored by git)
```
