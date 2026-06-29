import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import yaml
from bs4 import BeautifulSoup


SOURCES_FILE = Path("sources.yaml")
LAST_SEEN_FILE = Path("data/last_seen.json")
ERRORS_FILE = Path("data/errors.json")
LATEST_ALERT_FILE = Path("alerts/latest-alert.json")

TIMEOUT_SECONDS = 10
MAX_RETRIES = 3

USER_AGENT = (
    "choice-points-promo-watcher/0.1 "
    "(https://github.com/; checks public promotion pages)"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_sources() -> list[str]:
    with SOURCES_FILE.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file)

    if isinstance(data, list):
        sources = data
    elif isinstance(data, dict) and isinstance(data.get("sources"), list):
        sources = data["sources"]
    else:
        raise ValueError("sources.yaml must contain a YAML list or a sources list")

    urls = [str(source).strip() for source in sources if str(source).strip()]
    if not urls:
        raise ValueError("sources.yaml does not contain any URLs")
    return urls


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def write_json_if_changed(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return
    path.write_text(text, encoding="utf-8")


def fetch_html(url: str) -> str:
    headers = {"User-Agent": USER_AGENT}
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(url, headers=headers, timeout=TIMEOUT_SECONDS)
            response.raise_for_status()
            return response.text
        except requests.RequestException as error:
            last_error = error
            if attempt < MAX_RETRIES:
                time.sleep(attempt)

    raise RuntimeError(f"failed after {MAX_RETRIES} attempts: {last_error}")


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text(" ", strip=True)


def extract_percent(patterns: list[str], text: str) -> int | None:
    matches: list[int] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            value = int(match.group(1))
            if 0 < value <= 100:
                matches.append(value)
    return max(matches) if matches else None


def extract_bonus_percent(text: str) -> int | None:
    return extract_percent(
        [
            r"(\d{1,3})\s*%\s*(?:bonus|more|extra)",
            r"(?:bonus|earn|get|receive)\D{0,40}(\d{1,3})\s*%",
        ],
        text,
    )


def extract_discount_percent(text: str) -> int | None:
    return extract_percent(
        [
            r"(\d{1,3})\s*%\s*(?:discount|off)",
            r"(?:discount|save|off)\D{0,40}(\d{1,3})\s*%",
        ],
        text,
    )


def priority_for(bonus_percent: int | None, discount_percent: int | None) -> str:
    bonus = bonus_percent or 0
    discount = discount_percent or 0
    if bonus >= 50 or discount >= 45:
        return "critical"
    if bonus >= 40 or discount >= 40:
        return "high"
    if bonus >= 35 or discount >= 30:
        return "normal"
    return "low"


def find_snippet(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    match = re.search(
        r"choice privileges.{0,240}(?:bonus|discount|off|buy points|points)",
        normalized,
        flags=re.IGNORECASE,
    )
    if not match:
        match = re.search(
            r"(?:bonus|discount|off|buy points|points).{0,240}choice privileges",
            normalized,
            flags=re.IGNORECASE,
        )
    if not match:
        match = re.search(
            r"(?:bonus|discount|off).{0,240}",
            normalized,
            flags=re.IGNORECASE,
        )
    return match.group(0)[:300] if match else normalized[:300]


def detect_alert(url: str, text: str) -> dict[str, Any] | None:
    lower_text = text.lower()
    has_choice = "choice privileges" in lower_text
    has_points = "buy points" in lower_text or "points" in lower_text
    has_offer = (
        "bonus" in lower_text or "discount" in lower_text or re.search(r"\boff\b", lower_text)
    )

    if not (has_choice and has_points and has_offer):
        return None

    bonus_percent = extract_bonus_percent(text)
    discount_percent = extract_discount_percent(text)
    snippet = find_snippet(text)
    fingerprint_source = "|".join(
        [
            url,
            str(bonus_percent or ""),
            str(discount_percent or ""),
            re.sub(r"\s+", " ", snippet).lower(),
        ]
    )
    fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()

    return {
        "url": url,
        "priority": priority_for(bonus_percent, discount_percent),
        "bonus_percent": bonus_percent,
        "discount_percent": discount_percent,
        "fingerprint": fingerprint,
        "snippet": snippet,
    }


def main() -> int:
    checked_at = utc_now()
    sources = load_sources()
    last_seen = read_json(LAST_SEEN_FILE, {"fingerprints": {}})
    seen_fingerprints = last_seen.setdefault("fingerprints", {})

    new_alerts: list[dict[str, Any]] = []
    all_detected: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for url in sources:
        try:
            html = fetch_html(url)
            text = html_to_text(html)
            alert = detect_alert(url, text)
            if alert is None:
                continue

            all_detected.append(alert)
            fingerprint = alert["fingerprint"]
            if fingerprint not in seen_fingerprints:
                alert["detected_at"] = checked_at
                new_alerts.append(alert)
                seen_fingerprints[fingerprint] = {
                    "url": url,
                    "first_seen_at": checked_at,
                    "priority": alert["priority"],
                    "bonus_percent": alert["bonus_percent"],
                    "discount_percent": alert["discount_percent"],
                }
        except Exception as error:
            errors.append(
                {
                    "url": url,
                    "error": str(error),
                }
            )

    latest_alert = {
        "new_alert_count": len(new_alerts),
        "detected_count": len(all_detected),
        "alerts": new_alerts,
    }
    if new_alerts:
        latest_alert["checked_at"] = checked_at

    if new_alerts:
        last_seen["updated_at"] = checked_at
    write_json_if_changed(LAST_SEEN_FILE, last_seen)
    write_json_if_changed(LATEST_ALERT_FILE, latest_alert)
    write_json_if_changed(ERRORS_FILE, {"errors": errors})

    if new_alerts:
        print(json.dumps(latest_alert, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("No new alerts.")

    if errors:
        print(f"{len(errors)} source(s) failed. See {ERRORS_FILE}.", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
