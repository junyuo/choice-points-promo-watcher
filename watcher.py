import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


SOURCES_FILE = Path("sources.yaml")
LAST_SEEN_FILE = Path("data/last_seen.json")
ERRORS_FILE = Path("data/errors.json")
LATEST_ALERT_FILE = Path("alerts/latest-alert.json")
DEBUG_DIR = Path("data/debug")

TIMEOUT_SECONDS = 20
CHOICE_TIMEOUT_SECONDS = 30
PLAYWRIGHT_TIMEOUT_SECONDS = 25
MAX_RETRIES = 3
RETRY_DELAYS_SECONDS = [2, 5, 10]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}
ARTICLE_KEYWORDS = [
    "choice privileges",
    "choice points",
    "buy points",
    "bonus",
    "discount",
    "off",
]
MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}


def debug_dump_enabled() -> bool:
    return os.environ.get("DEBUG_DUMP") == "1"


def safe_debug_name(url: str) -> str:
    parsed_url = urlparse(url)
    host = parsed_url.netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    parts = [host]
    if parsed_url.query:
        parts.append("search")
    else:
        parts.extend(part for part in parsed_url.path.split("/") if part)

    filename = "_".join(parts) or "source"
    return re.sub(r"[^a-z0-9]+", "_", filename.lower()).strip("_")


def write_debug_text(path: Path, text: str) -> None:
    if not debug_dump_enabled():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def dump_source_text(url: str, text: str) -> None:
    write_debug_text(DEBUG_DIR / f"{safe_debug_name(url)}.txt", text)


def is_access_denied(text: str) -> bool:
    lower_text = text.lower()
    return (
        "access denied" in lower_text
        or "you don't have permission to access" in lower_text
    )


def is_search_result_url(url: str) -> bool:
    return "?s=" in url


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
    timeout = CHOICE_TIMEOUT_SECONDS if is_official_buy_points_url(url) else TIMEOUT_SECONDS
    last_error: Exception | None = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            response = requests.get(url, headers=REQUEST_HEADERS, timeout=timeout)
            response.raise_for_status()
            return response.text
        except requests.RequestException as error:
            last_error = error
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAYS_SECONDS[attempt])

    raise RuntimeError(f"failed after {MAX_RETRIES} retries: {last_error}")


def fetch_with_playwright(url: str) -> str:
    deadline = time.monotonic() + PLAYWRIGHT_TIMEOUT_SECONDS
    timeout_ms = PLAYWRIGHT_TIMEOUT_SECONDS * 1000

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            page.set_default_timeout(timeout_ms)
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
            page.wait_for_load_state("load", timeout=remaining_ms)

            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
            body_text = page.locator("body").inner_text(timeout=remaining_ms)

            if debug_dump_enabled() and is_official_buy_points_url(url):
                DEBUG_DIR.mkdir(parents=True, exist_ok=True)
                write_debug_text(DEBUG_DIR / "choicehotels_body_text.txt", body_text)
                write_debug_text(DEBUG_DIR / "choicehotels_page_title.txt", page.title())
                write_debug_text(DEBUG_DIR / "choicehotels_url.txt", page.url)
                page.screenshot(path=str(DEBUG_DIR / "choicehotels_screenshot.png"))

            return body_text
        finally:
            browser.close()


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text(" ", strip=True)


def article_keyword_match(text: str) -> bool:
    lower_text = unquote(text).lower()
    return any(
        keyword in lower_text if keyword != "off" else re.search(r"\boff\b", lower_text)
        for keyword in ARTICLE_KEYWORDS
    )


def extract_article_links(html: str, base_url: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    links: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        title = anchor.get_text(" ", strip=True)
        absolute_url = urljoin(base_url, href)
        parsed_url = urlparse(absolute_url)
        if parsed_url.scheme not in {"http", "https"}:
            continue

        candidate_text = f"{title} {absolute_url}"
        if not article_keyword_match(candidate_text) or absolute_url in seen_urls:
            continue

        seen_urls.add(absolute_url)
        links.append({"url": absolute_url, "title": title})

    return links


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def has_deadline(text: str) -> bool:
    lower_text = text.lower()
    return any(
        term in lower_text
        for term in [
            "through",
            "ends",
            "until",
            "offer ends",
            "only until",
            "valid until",
            "ends in",
            "offer ends in",
        ]
    )


def has_points_purchase(text: str) -> bool:
    lower_text = text.lower()
    return (
        "buy points" in lower_text
        or "purchase points" in lower_text
        or "get more points" in lower_text
        or "more points with your purchase" in lower_text
        or re.search(r"buy\s+[\d,]+\+?\s+points", lower_text) is not None
    )


def has_offer_term(text: str) -> bool:
    lower_text = text.lower()
    return (
        "bonus" in lower_text
        or "discount" in lower_text
        or "% off" in lower_text
        or "more points" in lower_text
        or re.search(r"\d{1,3}%\s+more\s+points", lower_text) is not None
        or re.search(r"get\s+\d{1,3}%\s+more", lower_text) is not None
    )


def has_percent(text: str) -> bool:
    return re.search(r"\d{1,3}\s*%", text) is not None


def context_has_percent_offer(text: str, offer_terms: list[str]) -> bool:
    normalized = normalize_text(text)
    for match in re.finditer(r"\d{1,3}\s*%", normalized):
        start = max(0, match.start() - 80)
        end = min(len(normalized), match.end() + 80)
        window = normalized[start:end].lower()
        if any(term in window for term in offer_terms):
            return True
    return False


def keyword_distance_score(text: str, position: int) -> int:
    lower_text = text.lower()
    keywords = [
        "choice privileges",
        "buy points",
        "purchase points",
        "get more points",
        "more points with your purchase",
        "bonus",
        "discount",
        "more",
    ]
    distances = [
        abs(match.start() - position)
        for keyword in keywords
        for match in re.finditer(re.escape(keyword), lower_text)
    ]
    return min(distances) if distances else len(text)


def extract_contextual_percent(text: str, offer_terms: list[str]) -> int | None:
    normalized = normalize_text(text)
    candidates: list[tuple[int, int, int]] = []

    for match in re.finditer(r"(\d{1,3})\s*%", normalized):
        value = int(match.group(1))
        if not 0 < value <= 100:
            continue

        start = max(0, match.start() - 80)
        end = min(len(normalized), match.end() + 80)
        window = normalized[start:end].lower()
        if not any(term in window for term in offer_terms):
            continue

        candidates.append((keyword_distance_score(normalized, match.start()), -value, value))

    if not candidates:
        return None

    candidates.sort()
    return candidates[0][2]


def extract_bonus_percent(text: str) -> int | None:
    return extract_contextual_percent(text, ["bonus", "more"])


def extract_discount_percent(text: str) -> int | None:
    return extract_contextual_percent(text, ["discount", "off"])


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
    normalized = normalize_text(text)
    snippet_candidates: list[tuple[int, int, str]] = []

    for match in re.finditer(r"\d{1,3}\s*%", normalized):
        start = max(0, match.start() - 220)
        end = min(len(normalized), match.end() + 220)
        snippet = normalized[start:end].strip()
        lower_snippet = snippet.lower()
        score = 0
        if "choice privileges" in lower_snippet:
            score += 1
        if has_points_purchase(snippet):
            score += 1
        if has_offer_term(snippet):
            score += 1
        if has_deadline(snippet):
            score += 1
        snippet_candidates.append((-score, keyword_distance_score(normalized, match.start()), snippet))

    if snippet_candidates:
        snippet_candidates.sort()
        return snippet_candidates[0][2][:460]

    match = re.search(
        r"choice privileges.{0,300}(?:bonus|discount|off|buy points|purchase points)",
        normalized,
        flags=re.IGNORECASE,
    )
    return match.group(0)[:460] if match else normalized[:460]


def is_official_buy_points_url(url: str) -> bool:
    return "choicehotels.com/choice-privileges/buy-points" in url.lower()


def validate_promo(alert: dict[str, Any], page_text: str, url: str) -> dict[str, Any]:
    reasons: list[str] = []
    confidence = 0
    lower_text = page_text.lower()
    snippet = alert.get("snippet") or ""
    lower_snippet = snippet.lower()
    is_search_page = "?s=" in url
    is_official_page = is_official_buy_points_url(url)

    if "choice privileges" in lower_text or is_official_page:
        confidence += 20
        if is_official_page and "choice privileges" not in lower_text:
            reasons.append("Official Choice buy-points URL supplies Choice Privileges context.")
        else:
            reasons.append("Page contains Choice Privileges.")
    else:
        reasons.append("Missing Choice Privileges on page.")

    if has_points_purchase(page_text):
        confidence += 20
        reasons.append("Page contains buy points or purchase points.")
    else:
        reasons.append("Missing buy points or purchase points on page.")

    percent_context_valid = False
    if alert.get("bonus_percent") is not None and context_has_percent_offer(
        page_text, ["bonus", "more"]
    ):
        percent_context_valid = True
    if alert.get("discount_percent") is not None and context_has_percent_offer(
        page_text, ["discount", "off"]
    ):
        percent_context_valid = True

    if percent_context_valid:
        confidence += 20
        reasons.append("Percentage appears near bonus, more, discount, or off.")
    else:
        reasons.append("No percentage appears near bonus, more, discount, or off.")

    if has_offer_term(page_text):
        reasons.append("Page contains bonus, discount, or % off.")
    else:
        reasons.append("Missing bonus, discount, or % off on page.")

    if has_deadline(page_text):
        confidence += 20
        reasons.append("Page contains through, ends, until, or offer ends.")
    else:
        reasons.append("Missing promotion deadline language on page.")

    snippet_has_choice = "choice privileges" in lower_snippet
    snippet_has_points = has_points_purchase(snippet)
    snippet_has_offer = has_offer_term(snippet)
    snippet_has_deadline = has_deadline(snippet)
    snippet_has_percent = has_percent(snippet)
    if is_official_page:
        snippet_valid = (
            "points" in lower_snippet
            and snippet_has_offer
            and snippet_has_deadline
            and snippet_has_percent
        )
    else:
        snippet_valid = (
            snippet_has_choice
            and snippet_has_points
            and snippet_has_offer
            and snippet_has_deadline
            and snippet_has_percent
        )

    if snippet_valid:
        confidence += 20
        if is_official_page:
            reasons.append("Official buy-points snippet contains points, percentage, offer terms, and deadline.")
        else:
            reasons.append("Snippet contains promotion name, percentage, and deadline.")
    else:
        reasons.append("Snippet does not contain promotion name, percentage, and deadline.")

    required_page_terms = (
        ("choice privileges" in lower_text or is_official_page)
        and has_points_purchase(page_text)
        and has_offer_term(page_text)
        and has_deadline(page_text)
    )
    is_valid = required_page_terms and percent_context_valid and snippet_valid and confidence >= 80

    if is_search_page:
        if not (snippet_has_percent and snippet_has_deadline):
            is_valid = False
            reasons.append("Search result page lacks clear snippet percentage and deadline.")
        if alert.get("priority") in {"critical", "high"}:
            alert["priority"] = "normal"
            reasons.append("Search result page priority capped at normal.")

    return {
        "is_valid": is_valid,
        "confidence": confidence,
        "validation_reasons": reasons,
    }


def contains_expired_marker(text: str) -> bool:
    return re.search(r"\b(expired|dead)\b", text, flags=re.IGNORECASE) is not None


def extract_article_dates(text: str) -> list[datetime]:
    dates: list[datetime] = []
    current_year = datetime.now(timezone.utc).year
    month_pattern = "|".join(MONTHS)

    for match in re.finditer(
        rf"\b({month_pattern})\.?\s+(\d{{1,2}})(?:,?\s+(\d{{4}}))?\b",
        text,
        flags=re.IGNORECASE,
    ):
        month = MONTHS[match.group(1).lower().rstrip(".")]
        day = int(match.group(2))
        year = int(match.group(3)) if match.group(3) else current_year
        try:
            dates.append(datetime(year, month, day, tzinfo=timezone.utc))
        except ValueError:
            continue

    for match in re.finditer(
        rf"\b(\d{{1,2}})\s+({month_pattern})\.?(?:\s+(\d{{4}}))?\b",
        text,
        flags=re.IGNORECASE,
    ):
        day = int(match.group(1))
        month = MONTHS[match.group(2).lower().rstrip(".")]
        year = int(match.group(3)) if match.group(3) else current_year
        try:
            dates.append(datetime(year, month, day, tzinfo=timezone.utc))
        except ValueError:
            continue

    return dates


def validate_article_promo(
    alert: dict[str, Any], page_text: str, url: str, title: str
) -> dict[str, Any]:
    combined_text = f"{title} {page_text}"
    lower_text = combined_text.lower()
    reasons: list[str] = []
    confidence = 0

    has_choice_context = "choice privileges" in lower_text or "choice points" in lower_text
    has_buy_context = (
        "buy points" in lower_text
        or "buying choice points" in lower_text
        or "purchase points" in lower_text
    )
    has_offer_context = (
        "bonus" in lower_text or "discount" in lower_text or re.search(r"\boff\b", lower_text)
    )
    has_expiry_context = any(
        term in lower_text
        for term in ["through", "until", "ends", "expires", "valid through"]
    )
    expired_marker = contains_expired_marker(combined_text)

    if has_choice_context:
        confidence += 20
        reasons.append("Article contains Choice Privileges or Choice points.")
    else:
        reasons.append("Article missing Choice Privileges or Choice points.")

    if has_buy_context:
        confidence += 20
        reasons.append("Article contains buy points, buying Choice points, or purchase points.")
    else:
        reasons.append("Article missing buy-points purchase wording.")

    if has_offer_context:
        confidence += 20
        reasons.append("Article contains bonus, discount, or off.")
    else:
        reasons.append("Article missing bonus, discount, or off.")

    if has_expiry_context:
        confidence += 20
        reasons.append("Article contains through, until, ends, expires, or valid through.")
    else:
        reasons.append("Article missing expiry wording.")

    article_dates = extract_article_dates(combined_text)
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    has_future_date = any(date >= today for date in article_dates)
    if article_dates and has_future_date:
        confidence += 20
        reasons.append("Article contains a date that has not passed.")
    elif article_dates:
        reasons.append("Article dates appear to be expired.")
    else:
        confidence = min(confidence, 70)
        reasons.append("Article date could not be determined; confidence capped at 70.")

    if expired_marker:
        reasons.append("Article title or content marks the offer expired or dead.")

    is_valid = (
        has_choice_context
        and has_buy_context
        and has_offer_context
        and has_expiry_context
        and has_future_date
        and not expired_marker
        and confidence >= 80
    )

    return {
        "is_valid": is_valid,
        "confidence": confidence,
        "validation_reasons": reasons,
    }


def detect_alert(url: str, text: str) -> dict[str, Any] | None:
    lower_text = text.lower()
    has_choice = "choice privileges" in lower_text or is_official_buy_points_url(url)
    has_points = has_points_purchase(text) or "points" in lower_text
    has_offer = has_offer_term(text) or re.search(r"\boff\b", lower_text)

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
    source_status: list[dict[str, Any]] = []

    for url in sources:
        try:
            fetch_method = "requests"
            try:
                html = fetch_html(url)
                text = html_to_text(html)
            except Exception as request_error:
                if not is_official_buy_points_url(url):
                    raise
                fetch_method = "playwright"
                try:
                    text = fetch_with_playwright(url)
                except Exception as playwright_error:
                    raise RuntimeError(
                        f"requests failed: {request_error}; playwright failed: {playwright_error}"
                    ) from playwright_error

            if (
                is_official_buy_points_url(url)
                and fetch_method == "requests"
                and len(text) < 1000
                and not is_access_denied(text)
            ):
                fetch_method = "playwright"
                text = fetch_with_playwright(url)

            dump_source_text(url, text)
            if is_access_denied(text):
                source_status.append(
                    {
                        "url": url,
                        "status": "blocked",
                        "fetch_method": fetch_method,
                        "error": "Access Denied",
                        "text_length": len(text),
                    }
                )
                continue

            source_status.append(
                {
                    "url": url,
                    "status": "success",
                    "fetch_method": fetch_method,
                    "error": None,
                    "text_length": len(text),
                }
            )

            if is_search_result_url(url):
                for article in extract_article_links(html, url):
                    article_url = article["url"]
                    article_title = article["title"]
                    try:
                        article_html = fetch_html(article_url)
                        article_text = html_to_text(article_html)
                        dump_source_text(article_url, article_text)
                        article_status = "blocked" if is_access_denied(article_text) else "success"
                        source_status.append(
                            {
                                "url": article_url,
                                "status": article_status,
                                "fetch_method": "requests",
                                "error": "Access Denied" if article_status == "blocked" else None,
                                "text_length": len(article_text),
                            }
                        )
                        if article_status == "blocked":
                            continue

                        alert = detect_alert(article_url, article_text)
                        if alert is None:
                            continue

                        validation = validate_article_promo(
                            alert, article_text, article_url, article_title
                        )
                        alert.update(validation)
                        if not alert["is_valid"]:
                            continue

                        all_detected.append(alert)
                        fingerprint = alert["fingerprint"]
                        if fingerprint not in seen_fingerprints:
                            alert["detected_at"] = checked_at
                            new_alerts.append(alert)
                            seen_fingerprints[fingerprint] = {
                                "url": article_url,
                                "first_seen_at": checked_at,
                                "priority": alert["priority"],
                                "bonus_percent": alert["bonus_percent"],
                                "discount_percent": alert["discount_percent"],
                            }
                    except Exception as article_error:
                        source_status.append(
                            {
                                "url": article_url,
                                "status": "failed",
                                "fetch_method": "requests",
                                "error": str(article_error),
                                "text_length": 0,
                            }
                        )
                continue

            alert = detect_alert(url, text)
            if alert is None:
                continue

            validation = validate_promo(alert, text, url)
            alert.update(validation)
            if not alert["is_valid"]:
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
            error_message = str(error)
            errors.append(
                {
                    "url": url,
                    "error": error_message,
                }
            )
            source_status.append(
                {
                    "url": url,
                    "status": "failed",
                    "fetch_method": (
                        "playwright" if is_official_buy_points_url(url) else "requests"
                    ),
                    "error": error_message,
                    "text_length": 0,
                }
            )

    latest_alert = {
        "checked_at": checked_at,
        "new_alert_count": len(new_alerts),
        "detected_count": len(all_detected),
        "alerts": new_alerts,
        "validation_note": None,
        "source_status": source_status,
    }
    if not all_detected:
        latest_alert["validation_note"] = (
            "No validated Choice Privileges buy-points promotion found from successfully fetched sources."
        )

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
