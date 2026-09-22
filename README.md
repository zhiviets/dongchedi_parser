Thanks for providing the details! Based on your information, here's an example of how you might structure the README for the **Dongchedi Parser** project:

---

# Dongchedi Parser

## Overview

Dongchedi Parser is a tool designed to help users parse Chinese car marketplace and car configuration pages. It can translate the content of these pages and generate a PDF report with the parsed data. The project comes with a user-friendly interface built with Tkinter, allowing users to easily input a URL, select options, and parse car information.

## Features

- **Car Marketplace Parsing**: Automatically fetches car data from Dongchedi’s marketplace page and translates it.
- **Car Configuration Parsing**: Parses individual car configuration pages, allowing users to select and translate specific parameters.
- **PDF Generation**: After parsing, the tool generates a PDF file with the translated content.
- **User Interface**: A simple, easy-to-use graphical interface for interacting with the parser, built using Tkinter.
- **Translation Options**: Users can customize which parameters of the car data to translate.

## Installation

### Prerequisites

1. **Python 3.7+** (Make sure you have Python installed. You can download it from [python.org](https://www.python.org/downloads/)).
2. **Git** (for cloning the repository).

### Steps

1. **Clone the repository**:
    ```bash
    git clone <git@github.com:Father1993/website-parser-dongchedi.git>
    cd <project_directory>
    ```

2. **Install dependencies**:
    ```bash
    pip install -r requirements.txt
    ```

3. **Install Playwright (for browser automation)**:
    ```bash
    playwright install chromium
    ```

4. **Run the project**:
    - For the main app (car marketplace page parsing):
      ```bash
      python main_app.py
      ```
    - For the configuration parser app:
      ```bash
      python configuration_app.py
      ```

## Usage

### Main Interface (Marketplace Parsing)

1. Launch the application by running `python main_app.py`.
2. Input the URL of a Dongchedi car marketplace page into the provided field.
3. Press the "Parse" button to start the process.
4. The app will fetch, translate, and generate a PDF of the parsed data.

### Configuration Parser Interface

1. Launch the configuration parser by running `python configuration_app.py`.
2. Input the URL of a car configuration page.
3. Select which parameters you'd like to translate and include in the PDF report.
4. Click the "Parse" button to start the parsing process.

## Example

- To parse a Dongchedi car marketplace page, run the main app, input the URL of the marketplace page, and press the parse button.
- To parse specific configuration details, run the configuration parser, choose the desired parameters, and click the "Parse" button.

---

## Weekly batch scraper → bn-auto

`weekly_scraper.py` automates the desktop app's per-URL flow for a whole
catalog: it opens a Dongchedi used-car **list/search page**, collects
listing links from it (matching the `/usedcar/<id>` pattern the single-page
parser already relies on — this is deliberately not tied to any CSS class
name, since those are auto-generated and break on every frontend redeploy),
then parses each one with the same `CarParser` used by the GUI and pushes
the batch to the [bn-auto](https://github.com/zhiviets/bn-auto) live-listings
catalog.

```bash
pip install -r requirements-ci.txt
python -m playwright install chromium
python weekly_scraper.py
```

Run it from the repo root (it reads `config.json` and
`test_names_translation.json` from the current directory, same as the GUI).

### Environment variables

| Variable | Default | What it does |
|---|---|---|
| `DONGCHEDI_SEARCH_URL` | a guessed unfiltered listing URL | **Verify this before relying on it.** It was written without network access to dongchedi.com to confirm the URL scheme or DOM — open it in a browser first, and replace it if it 404s or the site's list page has moved. |
| `DONGCHEDI_MAX_LISTINGS` | `30` | How many listings to pull per run. |
| `DONGCHEDI_LISTING_TIMEOUT_MS` | `25000` | Per-listing page-load timeout — kept short so one dead/sold listing can't stall the whole weekly run. |
| `BN_AUTO_URL` | — | e.g. `https://bn-auto.up.railway.app`. Without this (and the token below) the script still scrapes and writes `weekly_batch.json` locally, it just skips the push. |
| `BN_AUTO_IMPORT_TOKEN` | — | Must match `SCRAPER_IMPORT_TOKEN` configured on the bn-auto server. |

### GitHub Actions

`.github/workflows/scrape.yml` runs the batch weekly (Mondays, 04:00 UTC)
and on manual dispatch. Configure these as repo secrets for the push to
bn-auto to work: `BN_AUTO_URL`, `BN_AUTO_IMPORT_TOKEN`, and optionally
`DONGCHEDI_SEARCH_URL` once you've confirmed the real listing-page URL.
The scraped batch is also uploaded as a workflow artifact for debugging,
whether or not the push succeeded.

Photos are downloaded and re-encoded as compressed `data:` URLs before
being sent to bn-auto — Dongchedi's image links are signed and expire
(`x-expires` in the query string), so they can't be hot-linked from the
catalog page.

---

This README includes all the necessary instructions, from setting up the environment to using the app's features, as well as an overview of what the project does and how users can interact with it. Let me know if you want to tweak anything!