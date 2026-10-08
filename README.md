# Site Link Auditor

Crawl a website with a real (headless) browser and get an HTML/CSV report of either:

- **Broken links** — 4xx/5xx, redirects and connection errors (default mode), or
- **Where a word or phrase appears** — e.g. every page that mentions `"Royal Motors"` (`--search-term`), or
- **Images missing `alt` and/or `title`** (`--check-images`, or `--alt-only` / `--title-only`).

Any mode can be limited to the pages listed on the site's sitemap page (`--sitemap-only`).

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
| Find a multi-word phrase | `python site_auditor.py https://example.com --search-term "Royal Motors" -o reports/royal` |
| Find images without alt/title | `python site_auditor.py https://example.com --check-images -o reports/images` |
| …only missing alt | `python site_auditor.py https://example.com --alt-only -o reports/alt` |
| …only missing title | `python site_auditor.py https://example.com --title-only -o reports/title` |
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

## The two modes

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
- None of the image flags can be combined with `--search-term`.

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

### Interrupting a run

Press **Ctrl+C** at any time: the summary is printed and partial reports are saved as `<output>_partial.html` and `<output>_partial.csv` (always both formats, regardless of `-f`).

---

## How it works

1. **Crawl** — Headless Chromium opens the starting URL, waits for the DOM plus ~1.5 s for JavaScript, and collects every `a[href]`. Internal links (same host) are queued breadth-first (with `--sitemap-only`, only the start page's links are queued). `mailto:`, `tel:`, `javascript:`, `#anchors` and `data:` links are ignored; `#fragments` are stripped. If a page times out it retries once with a lighter wait strategy before recording a `TIMEOUT`.
2. **Check links** (audit mode) — new links from each page are checked in parallel (10 workers) with an HTTP `HEAD`, falling back to `GET` if the server answers 405 or refuses the connection. Redirects are followed and detected by comparing the final URL with the original.
3. **Search** (search mode) — instead of step 2, the page's visible text and its links are scanned for the term.
   **Images** (image mode) — instead of step 2, every `<img>` in the rendered page is checked for `alt` and `title`.
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
├── site_auditor.py    # The whole tool (single file, no config)
├── README.md
└── reports/           # Your reports (create it; ignored by git)
```
