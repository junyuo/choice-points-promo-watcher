# choice-points-promo-watcher

Python MVP for watching public Choice Privileges points promotion pages.

The watcher reads URLs from `sources.yaml`, fetches each page with `requests`, parses visible HTML text with BeautifulSoup, looks for Choice Privileges points promotion language, extracts bonus or discount percentages, and writes the latest new alerts to `alerts/latest-alert.json`.

This is not a sample-data project. The script performs real HTTP requests against the configured sources.

## Local Run

Requires Python 3.11.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python watcher.py
```

The script prints new alerts to stdout. Source fetch errors are written to `data/errors.json` and do not stop the full run.

## GitHub Actions Manual Run

1. Open the repository on GitHub.
2. Go to **Actions**.
3. Select **Watch Choice Points Promos**.
4. Click **Run workflow**.

The workflow also runs every 6 hours. If `data` or `alerts` files change, it commits those updates back to the repository.

## Current Limits

- No login support.
- No personalized offer support.
- No guarantee that JavaScript-rendered dynamic content will be captured.
- No frontend.
- No database.
- No email sending.
- No automatic point purchases.
