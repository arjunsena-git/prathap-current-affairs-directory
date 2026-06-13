#!/usr/bin/env python3
"""Build a static current-affairs page from public free sources."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import feedparser
import requests
from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning
from jinja2 import Environment, FileSystemLoader, select_autoescape

IST = timezone(timedelta(hours=5, minutes=30), "IST")
ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "templates"
CACHE_PATH = ROOT / "data" / "current_affairs_cache.json"
OUTPUT_PATH = ROOT / "index.html"
REPORT_OUTPUT_PATH = ROOT / "year-to-date.html"
USER_AGENT = "KAS-UPSC-Current-Affairs-Bot/1.0 (+https://github.com/)"
MAX_ITEMS_PER_SOURCE = 12
KEEP_DAYS = 7
HTTP_TIMEOUT = (8, 12)
warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    kind: str
    default_category: str


SOURCES = [
    Source("PIB", "https://pib.gov.in/rssfeed", "rss", "National"),
    Source("DD News", "https://ddnews.gov.in/rss", "rss", "National"),
    Source("The Hindu", "https://www.thehindu.com/news/national/feeder/default.rss", "rss", "National"),
    Source("The Hindu Karnataka", "https://www.thehindu.com/news/national/karnataka/feeder/default.rss", "rss", "Karnataka/State"),
    Source("GKToday", "https://www.gktoday.in/current-affairs/", "html", "National"),
    Source("KPSC", "https://kpsc.kar.nic.in/", "html", "Karnataka/State"),
    Source("Karnataka DIPR", "https://karnatakavarthe.org/en/", "html", "Karnataka/State"),
]

CATEGORIES = [
    "National",
    "Karnataka/State",
    "Economy",
    "Science & Tech",
    "Environment",
    "International",
]

CATEGORY_KEYWORDS = {
    "Karnataka/State": [
        "karnataka",
        "bengaluru",
        "bangalore",
        "mysuru",
        "mangaluru",
        "belagavi",
        "kpsc",
        "vidhana soudha",
    ],
    "Economy": [
        "rbi",
        "budget",
        "finance",
        "economy",
        "gdp",
        "inflation",
        "tax",
        "gst",
        "bank",
        "sebi",
        "trade",
    ],
    "Science & Tech": [
        "science",
        "technology",
        "isro",
        "space",
        "satellite",
        "ai",
        "digital",
        "quantum",
        "research",
        "cyber",
    ],
    "Environment": [
        "climate",
        "environment",
        "forest",
        "wildlife",
        "biodiversity",
        "pollution",
        "renewable",
        "conservation",
        "river",
    ],
    "International": [
        "united nations",
        "foreign",
        "global",
        "international",
        "world",
        "summit",
        "bilateral",
        "g20",
        "brics",
        "asean",
    ],
}

EXAM_HOOKS = {
    "National": "Link with polity, governance, schemes, social justice, internal security, and GS-II/essay examples.",
    "Karnataka/State": "Use for KAS state-specific notes: Karnataka administration, society, economy, geography, and local governance.",
    "Economy": "Revise with growth, fiscal policy, monetary policy, banking, taxation, trade, and inclusive development.",
    "Science & Tech": "Connect with applications, institutions, missions, risks, ethics, and prelims-ready technical terms.",
    "Environment": "Map to climate, biodiversity, conservation, disasters, pollution, and sustainable development.",
    "International": "Use for IR notes: India's interests, bilateral relations, groupings, global institutions, and diaspora.",
}


def now_ist() -> datetime:
    return datetime.now(IST)


def normalize_text(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    return value


def parse_date(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        try:
            dt = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            dt = now_ist()
    else:
        dt = now_ist()

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def summarize(text: str, fallback: str) -> str:
    cleaned = normalize_text(BeautifulSoup(text or "", "html.parser").get_text(" "))
    if not cleaned:
        cleaned = fallback
    if len(cleaned) <= 240:
        return cleaned
    truncated = cleaned[:237].rsplit(" ", 1)[0]
    return f"{truncated}..."


def strip_trailing_fragment(text: str) -> str:
    text = normalize_text(text)
    text = re.sub(r"\s*\.\.\.$", "", text)
    parts = re.split(r"(?<=[.!?])\s+", text)
    if len(parts) > 1 and not re.search(r"[.!?]$", parts[-1]):
        text = " ".join(parts[:-1])
    return text.strip()


def concise_note(title: str, summary: str, limit: int = 210) -> str:
    title = strip_trailing_fragment(title)
    summary = strip_trailing_fragment(summary)
    if not summary or title_key(summary) == title_key(title) or title_key(summary).startswith(title_key(title)):
        note = title
    else:
        note = f"{title}: {summary}"
    if len(note) <= limit:
        return note
    trimmed = note[: limit - 1].rsplit(" ", 1)[0].rstrip(",:;")
    return f"{trimmed}."


def categorize(title: str, summary: str, default: str) -> str:
    haystack = f"{title} {summary}".lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(keyword in haystack for keyword in keywords):
            return category
    return default if default in CATEGORIES else "National"


def item_id(title: str, link: str) -> str:
    basis = title_key(title) or link
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def title_key(title: str) -> str:
    return re.sub(r"[\W_]+", " ", title.casefold(), flags=re.UNICODE).strip()


def year_start() -> datetime:
    current = now_ist()
    return datetime(current.year, 1, 1, tzinfo=IST)


def request_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/rss+xml,application/xml;q=0.9,*/*;q=0.8"})
    return session


def robots_allows(session: requests.Session, url: str) -> bool:
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    parser = RobotFileParser()
    parser.set_url(robots_url)
    try:
        response = session.get(robots_url, timeout=HTTP_TIMEOUT)
        if response.status_code >= 400:
            logging.info("robots.txt unavailable for %s; proceeding cautiously", parsed.netloc)
            return True
        parser.parse(response.text.splitlines())
        allowed = parser.can_fetch(USER_AGENT, url) and parser.can_fetch("*", url)
        if not allowed:
            logging.warning("robots.txt disallows scraping %s", url)
        return allowed
    except requests.RequestException as exc:
        logging.warning("Could not read robots.txt for %s: %s", url, exc)
        return True


def fetch_rss(source: Source, session: requests.Session) -> list[dict[str, Any]]:
    response = session.get(source.url, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    feed = feedparser.parse(response.content)
    items: list[dict[str, Any]] = []

    for entry in feed.entries[:MAX_ITEMS_PER_SOURCE]:
        title = normalize_text(entry.get("title", ""))
        if not title:
            continue
        link = entry.get("link", source.url)
        published = entry.get("published") or entry.get("updated") or entry.get("created")
        date = parse_date(published)
        raw_summary = entry.get("summary") or entry.get("description") or ""
        summary = summarize(raw_summary, title)
        category = categorize(title, summary, source.default_category)
        items.append(make_item(source, title, link, summary, date, category, unavailable=False))

    return items


def fetch_html(source: Source, session: requests.Session) -> list[dict[str, Any]]:
    if not robots_allows(session, source.url):
        raise RuntimeError(f"robots.txt disallows {source.url}")

    response = session.get(source.url, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    candidates = html_candidates(source, soup)
    items: list[dict[str, Any]] = []

    for candidate in candidates[:MAX_ITEMS_PER_SOURCE]:
        title = normalize_text(candidate["title"])
        if not title or len(title) < 8:
            continue
        link = urljoin(source.url, candidate.get("link") or source.url)
        summary = summarize(candidate.get("summary", ""), title)
        date = parse_date(candidate.get("date"))
        category = categorize(title, summary, source.default_category)
        items.append(make_item(source, title, link, summary, date, category, unavailable=False))

    return items


def html_candidates(source: Source, soup: BeautifulSoup) -> list[dict[str, str]]:
    if source.name == "GKToday":
        cards = soup.select("article, .post, .td_module_wrap, .entry, li")
        results = []
        for card in cards:
            anchor = card.select_one("h1 a, h2 a, h3 a, .entry-title a, a")
            if not anchor:
                continue
            title = anchor.get_text(" ", strip=True)
            href = anchor.get("href", "")
            if is_gktoday_non_article(title, href):
                continue
            if "current affairs" not in title.lower() and len(title) < 18:
                continue
            summary_node = card.select_one(".entry-content, .td-excerpt, p")
            date_node = card.select_one("time, .date, .entry-date")
            results.append(
                {
                    "title": title,
                    "link": href,
                    "summary": summary_node.get_text(" ", strip=True) if summary_node else "",
                    "date": date_node.get("datetime") or date_node.get_text(" ", strip=True) if date_node else "",
                }
            )
        if results:
            return results

    if source.name == "KPSC":
        results = []
        for anchor in soup.select("a"):
            title = anchor.get_text(" ", strip=True)
            if re.search(r"notification|notice|result|selection|exam|recruitment|key answer", title, re.I):
                results.append({"title": title, "link": anchor.get("href", ""), "summary": "KPSC notice or update.", "date": ""})
        return results

    if source.name == "Karnataka DIPR":
        results = []
        for card in soup.select("article, .post, .news-item, .entry, li"):
            anchor = card.select_one("h1 a, h2 a, h3 a, .entry-title a, a")
            if not anchor:
                continue
            title = anchor.get_text(" ", strip=True)
            summary_node = card.select_one("p, .entry-content, .excerpt")
            date_node = card.select_one("time, .date, .entry-date")
            results.append(
                {
                    "title": title,
                    "link": anchor.get("href", ""),
                    "summary": summary_node.get_text(" ", strip=True) if summary_node else "Karnataka government press update.",
                    "date": date_node.get("datetime") or date_node.get_text(" ", strip=True) if date_node else "",
                }
            )
        return results

    return []


def is_gktoday_non_article(title: str, href: str) -> bool:
    text = f"{title} {href}".lower()
    if title.strip().lower() in {"current affairs", "all gk questions categories"}:
        return True
    blocked = [
        "all gk questions",
        "mcq",
        "quiz",
        "previous months",
        "archive",
        "ca articles",
        "monthly",
        "pdf",
        "quizbase",
        "current-affairs-monthly",
        "category/current-affairs",
    ]
    return any(token in text for token in blocked)


def make_item(source: Source, title: str, link: str, summary: str, date: datetime, category: str, unavailable: bool) -> dict[str, Any]:
    return {
        "id": item_id(title, link),
        "title": title,
        "summary": summary,
        "handbook_note": concise_note(title, summary),
        "exam_hook": EXAM_HOOKS.get(category, EXAM_HOOKS["National"]),
        "source": source.name,
        "link": link,
        "date": date.isoformat(),
        "date_label": date.strftime("%d %b %Y"),
        "category": category,
        "unavailable": unavailable,
    }


def load_cache() -> dict[str, Any]:
    if not CACHE_PATH.exists():
        return {"items": [], "sources": {}}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.warning("Ignoring invalid cache at %s", CACHE_PATH)
        return {"items": [], "sources": {}}


def fallback_items(source: Source, cache: dict[str, Any], error: Exception) -> list[dict[str, Any]]:
    cached = [item for item in cache.get("items", []) if item.get("source") == source.name]
    if not cached:
        return []
    logging.warning("Using cached items for %s after error: %s", source.name, error)
    items = []
    for item in cached[:MAX_ITEMS_PER_SOURCE]:
        copy = dict(item)
        copy["unavailable"] = True
        copy["status_note"] = "source unavailable"
        items.append(copy)
    return items


def scrape_sources() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    session = request_session()
    cache = load_cache()
    all_items: list[dict[str, Any]] = []
    source_status: dict[str, dict[str, Any]] = {}

    for source in SOURCES:
        try:
            items = fetch_rss(source, session) if source.kind == "rss" else fetch_html(source, session)
            source_status[source.name] = {"ok": True, "count": len(items), "url": source.url}
            all_items.extend(items)
            logging.info("%s: %s items", source.name, len(items))
        except Exception as exc:
            logging.error("%s failed: %s", source.name, exc)
            items = fallback_items(source, cache, exc)
            source_status[source.name] = {"ok": False, "count": len(items), "url": source.url, "error": str(exc)}
            all_items.extend(items)

    return all_items, source_status


def dedupe_and_filter(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cutoff = now_ist() - timedelta(days=KEEP_DAYS)
    return dedupe_since(items, cutoff, keep_unavailable=True)


def dedupe_since(items: list[dict[str, Any]], cutoff: datetime, keep_unavailable: bool = False) -> list[dict[str, Any]]:
    seen: set[str] = set()
    filtered: list[dict[str, Any]] = []

    for item in sorted(items, key=lambda row: row.get("date", ""), reverse=True):
        dedupe_key = title_key(item.get("title", "")) or item.get("link", "")
        if not dedupe_key or dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        try:
            item_date = datetime.fromisoformat(item["date"]).astimezone(IST)
        except (KeyError, ValueError):
            item_date = now_ist()
            item["date"] = item_date.isoformat()
            item["date_label"] = item_date.strftime("%d %b %Y")

        if item_date < cutoff and not (keep_unavailable and item.get("unavailable")):
            continue
        filtered.append(item)

    return filtered


def merge_archive(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in existing + incoming:
        key = title_key(item.get("title", "")) or item.get("link", "")
        if not key:
            continue
        current = enrich_item(dict(item))
        if key in merged and merged[key].get("unavailable") and not current.get("unavailable"):
            merged[key] = current
        else:
            merged.setdefault(key, current)
    return dedupe_since(list(merged.values()), year_start(), keep_unavailable=False)


def enrich_item(item: dict[str, Any]) -> dict[str, Any]:
    category = item.get("category", "National")
    item["handbook_note"] = concise_note(item.get("title", ""), item.get("summary", ""))
    item["exam_hook"] = EXAM_HOOKS.get(category, EXAM_HOOKS["National"])
    return item


def group_items(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped = {category: [] for category in CATEGORIES}
    for item in items:
        grouped.setdefault(item.get("category", "National"), []).append(item)
    return grouped


def repo_url() -> str:
    repo = os.getenv("GITHUB_REPOSITORY")
    if repo:
        return f"https://github.com/{repo}"
    return "https://github.com/arjunsena-git/prathap-current-affairs-directory"


def render(items: list[dict[str, Any]], source_status: dict[str, dict[str, Any]]) -> str:
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=select_autoescape(["html", "xml"]))
    template = env.get_template("index.html.j2")
    updated = now_ist()
    repository_url = repo_url()
    return template.render(
        categories=CATEGORIES,
        grouped_items=group_items(items),
        item_count=len(items),
        ytd_count=0,
        source_status=source_status,
        last_updated_iso=updated.isoformat(),
        last_updated_label=updated.strftime("%d %b %Y, %I:%M %p IST"),
        repository_url=repository_url,
        actions_url=f"{repository_url}/actions/workflows/update-current-affairs.yml",
    )


def render_home(items: list[dict[str, Any]], ytd_items: list[dict[str, Any]], source_status: dict[str, dict[str, Any]]) -> str:
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=select_autoescape(["html", "xml"]))
    template = env.get_template("index.html.j2")
    updated = now_ist()
    repository_url = repo_url()
    return template.render(
        categories=CATEGORIES,
        grouped_items=group_items(items),
        item_count=len(items),
        ytd_count=len(ytd_items),
        source_status=source_status,
        last_updated_iso=updated.isoformat(),
        last_updated_label=updated.strftime("%d %b %Y, %I:%M %p IST"),
        ytd_start_label=year_start().strftime("%d %b %Y"),
        repository_url=repository_url,
        actions_url=f"{repository_url}/actions/workflows/update-current-affairs.yml",
    )


def render_report(items: list[dict[str, Any]], source_status: dict[str, dict[str, Any]]) -> str:
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=select_autoescape(["html", "xml"]))
    template = env.get_template("year-to-date.html.j2")
    updated = now_ist()
    return template.render(
        categories=CATEGORIES,
        grouped_items=group_items(items),
        item_count=len(items),
        source_status=source_status,
        last_updated_iso=updated.isoformat(),
        last_updated_label=updated.strftime("%d %b %Y, %I:%M %p IST"),
        ytd_start_label=year_start().strftime("%d %b %Y"),
        year=updated.year,
    )


def save_cache(items: list[dict[str, Any]], source_status: dict[str, dict[str, Any]]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_updated": now_ist().isoformat(),
        "items": items,
        "sources": source_status,
    }
    CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    global CACHE_PATH

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(OUTPUT_PATH), help="HTML file to write")
    parser.add_argument("--report-output", default=str(REPORT_OUTPUT_PATH), help="Year-to-date report HTML file to write")
    parser.add_argument("--cache", default=str(CACHE_PATH), help="Cache file to write")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    CACHE_PATH = Path(args.cache)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s: %(message)s")
    logging.getLogger("urllib3.connection").setLevel(logging.ERROR)
    scraped_items, source_status = scrape_sources()
    cache = load_cache()
    ytd_items = merge_archive(cache.get("items", []), scraped_items)
    items = dedupe_and_filter(ytd_items)
    html = render_home(items, ytd_items, source_status)
    report_html = render_report(ytd_items, source_status)
    Path(args.output).write_text(html, encoding="utf-8")
    Path(args.report_output).write_text(report_html, encoding="utf-8")
    save_cache(ytd_items, source_status)
    logging.warning("Generated %s with %s recent items and %s year-to-date items", args.output, len(items), len(ytd_items))
    return 0


if __name__ == "__main__":
    sys.exit(main())
