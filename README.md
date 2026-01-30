# Site Link Auditor

A command-line tool that crawls websites and generates detailed reports of broken links, redirects, and errors. Uses Playwright for browser automation, so it handles JavaScript-rendered pages that simple HTTP scrapers would miss.

## What It Does

- Crawls an entire website using headless Chromium (via Playwright)
- Discovers all internal pages via breadth-first traversal
- Checks every link found on each page (internal and optionally external)
- Detects broken links (4xx/5xx), redirects, and connection errors
- Generates reports in HTML and/or CSV format
- Handles interruptions gracefully (Ctrl+C saves a partial report)

## Requirements

- [Miniconda](https://docs.anaconda.com/miniconda/install/) (or Anaconda)
- Python 3.10+

## Setup

### 1. Install Miniconda

If you don't have Miniconda installed, download it from https://docs.anaconda.com/miniconda/install/ and follow the instructions for your OS.

### 2. Clone the repository

```bash
git clone <repo-url>
cd crawl
```

### 3. Create the conda environment

```bash
conda create -n linkchecker python=3.11 -y
```

### 4. Activate the environment

```bash
conda activate linkchecker
```

You should see `(linkchecker)` at the beginning of your terminal prompt.

### 5. Install Python dependencies

```bash
pip install playwright requests
```

### 6. Install browser binaries

Playwright needs a Chromium binary to run the headless browser:

```bash
playwright install chromium
```

### Quick setup (copy-paste)

```bash
conda create -n linkchecker python=3.11 -y
conda activate linkchecker
pip install playwright requests
playwright install chromium
```

## Usage

Make sure the conda environment is active before running:

```bash
conda activate linkchecker
```

### Basic audit (internal links only)

```bash
python site_auditor.py https://example.com
```

### Audit with external link checking

```bash
python site_auditor.py https://example.com --check-external
```

### Limit the number of pages crawled

```bash
python site_auditor.py https://example.com --max-pages 50
```

### Custom output filename and format

```bash
python site_auditor.py https://example.com -o my_report -f both
```

### Quiet mode (suppress per-page logging)

```bash
python site_auditor.py https://example.com -q
```

### Full example

```bash
python site_auditor.py https://example.com --check-external --max-pages 200 --timeout 90000 -o reports/example_audit -f both
```

## Command-Line Options

| Option | Default | Description |
|---|---|---|
| `url` | *(required)* | The website URL to audit |
| `--max-pages` | `0` (unlimited) | Maximum number of pages to crawl |
| `--check-external` | off | Also validate external links |
| `--timeout` | `60000` | Page load timeout in milliseconds |
| `-o`, `--output` | `audit_report` | Output filename (without extension) |
| `-f`, `--format` | `both` | Output format: `html`, `csv`, or `both` |
| `-q`, `--quiet` | off | Suppress verbose per-page output |

## Output

### HTML Report

A styled, self-contained HTML page with:

- Summary statistics (pages crawled, links checked, broken count, redirects, errors)
- Broken links table with status codes and source pages
- Errors table with failure details
- Redirects table showing original and destination URLs

Open it in any browser to review.

### CSV Report

A spreadsheet-compatible CSV with columns:

| Column | Description |
|---|---|
| Status | HTTP status code or `ERROR`/`TIMEOUT` |
| URL | The link that was checked |
| Found On | The page where the link was discovered |
| Link Text | The anchor text of the link |
| Type | `Internal` or `External` |
| Redirects To | Final URL if a redirect was detected |
| Error | Error message if the request failed |

Rows are ordered: broken links first, then errors, redirects, and working links.

## How It Works

1. **Crawl phase** -- Playwright launches headless Chromium and loads each page, waiting for JavaScript to render. Links are extracted from the DOM (`a[href]` elements). Internal links are queued for further crawling in breadth-first order.

2. **Link checking phase** -- Each unique link is validated using HTTP HEAD requests (falling back to GET if HEAD returns 405 or fails). Links are checked in parallel using a thread pool (10 concurrent workers). A link is classified as broken if it returns a 4xx/5xx status, a redirect if the final URL differs from the original, or an error if the request fails entirely.

3. **Report generation** -- Results are written to HTML and/or CSV files. If the crawl is interrupted with Ctrl+C, partial reports are saved with a `_partial` suffix.

## Project Structure

```
crawl/
├── site_auditor.py    # Main script (single file, no external config)
├── README.md
└── <output dirs>/     # Generated reports (created per audit run)
    ├── my_report.html
    └── my_report.csv
```

## Tips

- **Large sites**: For sites with thousands of pages, use `--max-pages` to limit scope or run without `--check-external` to speed things up.
- **Timeouts**: If pages are slow to load, increase `--timeout` (value is in milliseconds).
- **Interruptions**: Press Ctrl+C at any time. The tool will save whatever it has collected so far as a partial report.
- **Output directories**: Create the output directory before running if using a path (e.g., `mkdir -p reports` before `-o reports/my_report`).
