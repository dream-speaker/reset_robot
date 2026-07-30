#!/usr/bin/env python3
"""Watch Tibo's public RSS mirrors for Codex reset announcements."""

from __future__ import annotations

import html
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable


X_USERNAME = os.getenv("X_USERNAME", "thsottiaux")
STATE_FILE = Path(os.getenv("STATE_FILE", "state/last_seen.json"))

# Twiiit checks public Nitter instances regularly and redirects to one that is
# currently online. The direct mirrors are fallbacks if the redirector fails.
DEFAULT_FEED_URLS = (
    "https://twiiit.com/{username}/rss",
    "https://xcancel.com/{username}/rss",
    "https://nitter.net/{username}/rss",
    "https://nitter.poast.org/{username}/rss",
)

PRODUCT_RE = re.compile(
    r"\b(codex|chatgpt\s+work|work\s+and\s+codex|codexer(?:s)?)\b", re.I
)
RESET_RE = re.compile(
    r"\b(reset(?:s|ting)?|banked\s+reset|usage\s+limits?\s+(?:have\s+been\s+)?reset)\b",
    re.I,
)
FUTURE_RE = re.compile(
    r"\b(tomorrow|soon|next\s+hour|next\s+few\s+hours|will|going\s+to|"
    r"about\s+to|lands?\s+in|grant(?:ing)?|coming)\b",
    re.I,
)
COMPLETED_RE = re.compile(
    r"\b(have\s+reset|has\s+reset|reset\s+(?:usage|everyone|all)|"
    r"limits?\s+(?:have|has)\s+been\s+reset|added\s+a\s+banked\s+reset|"
    r"did\s+a\s+.*reset)\b",
    re.I,
)
STATUS_ID_RE = re.compile(r"/status(?:es)?/(\d+)")
TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class Post:
    id: str
    text: str
    created_at: datetime

    @property
    def url(self) -> str:
        return f"https://x.com/{X_USERNAME}/status/{self.id}"


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def configured_feed_urls() -> list[str]:
    configured = os.getenv("RSS_FEED_URLS", "").strip()
    templates = (
        [item.strip() for item in re.split(r"[\n,]", configured) if item.strip()]
        if configured
        else list(DEFAULT_FEED_URLS)
    )
    return [template.format(username=X_USERNAME) for template in templates]


def clean_feed_text(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(html.unescape(TAG_RE.sub(" ", value)).split())


def parse_feed_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        result = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def extract_post_id(*values: str | None) -> str | None:
    for value in values:
        if not value:
            continue
        match = STATUS_ID_RE.search(value)
        if match:
            return match.group(1)
        if value.strip().isdigit():
            return value.strip()
    return None


def parse_rss(xml_bytes: bytes) -> list[Post]:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise RuntimeError(f"Mirror returned invalid or empty RSS: {exc}") from exc

    posts: dict[str, Post] = {}
    items = root.findall(".//item")
    if items:
        for item in items:
            link = item.findtext("link")
            guid = item.findtext("guid")
            post_id = extract_post_id(link, guid)
            if not post_id:
                continue
            text = clean_feed_text(item.findtext("title") or item.findtext("description"))
            created = parse_feed_datetime(item.findtext("pubDate"))
            posts[post_id] = Post(post_id, text, created)
    else:
        namespace = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall(".//atom:entry", namespace):
            link_element = entry.find("atom:link", namespace)
            link = link_element.get("href") if link_element is not None else None
            entry_id = entry.findtext("atom:id", namespaces=namespace)
            post_id = extract_post_id(link, entry_id)
            if not post_id:
                continue
            text = clean_feed_text(
                entry.findtext("atom:title", namespaces=namespace)
                or entry.findtext("atom:content", namespaces=namespace)
                or entry.findtext("atom:summary", namespaces=namespace)
            )
            created = parse_feed_datetime(
                entry.findtext("atom:published", namespaces=namespace)
                or entry.findtext("atom:updated", namespaces=namespace)
            )
            posts[post_id] = Post(post_id, text, created)

    if not posts:
        raise RuntimeError("Mirror RSS contained no recognizable X posts.")
    return sorted(posts.values(), key=lambda post: (post.created_at, post.id))


def fetch_posts() -> tuple[list[Post], str]:
    errors: list[str] = []
    for url in configured_feed_urls():
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; TiboCodexResetBot/2.0; "
                    "+https://github.com/dream-speaker/tibo-codex-reset-bot)"
                ),
                "Accept": "application/rss+xml, application/atom+xml, text/xml;q=0.9",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                posts = parse_rss(response.read())
            return posts, url
        except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError) as exc:
            errors.append(f"{url}: {exc}")
    raise RuntimeError("All RSS mirrors failed:\n- " + "\n- ".join(errors))


def classify_reset(text: str) -> str | None:
    """Return 'upcoming', 'completed', or None."""
    normalized = " ".join(text.split())
    if not PRODUCT_RE.search(normalized) or not RESET_RE.search(normalized):
        return None
    if FUTURE_RE.search(normalized) and not COMPLETED_RE.search(normalized):
        return "upcoming"
    return "completed"


def load_seen_ids() -> set[str]:
    if not STATE_FILE.exists():
        return set()
    try:
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        seen = {str(value) for value in payload.get("seen_ids", [])}
        # Migrate the original X-API state format without breaking a live bot.
        if payload.get("last_seen_id"):
            seen.add(str(payload["last_seen_id"]))
        return seen
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(f"Invalid state file {STATE_FILE}: {exc}") from exc


def select_new_posts(
    posts: Iterable[Post],
    seen_ids: set[str],
    now: datetime,
    first_run_lookback_minutes: int,
) -> list[Post]:
    posts = list(posts)
    if seen_ids:
        return [post for post in posts if post.id not in seen_ids]
    cutoff = now - timedelta(minutes=first_run_lookback_minutes)
    return [post for post in posts if post.created_at >= cutoff]


def write_state(seen_ids: Iterable[str]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # X snowflake IDs are numeric and time ordered. Keeping a bounded history
    # protects against mirror feeds returning pinned or temporarily missing posts.
    bounded = sorted({str(value) for value in seen_ids}, key=int)[-500:]
    payload = {
        "seen_ids": bounded,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "username": X_USERNAME,
        "source": "public-rss-mirrors",
    }
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(STATE_FILE)


def build_email(matches: list[tuple[Post, str]]) -> EmailMessage:
    upcoming = any(kind == "upcoming" for _, kind in matches)
    label = "即将发放" if upcoming else "已经重置"
    subject = f"【Codex 重置提醒】Tibo 宣布{label}"

    text_sections = []
    html_sections = []
    for post, kind in matches:
        status = "可能即将发放" if kind == "upcoming" else "已宣布重置"
        created = post.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        text_sections.append(
            f"{status}\n发布时间：{created}\n\n{post.text}\n\n查看原推：{post.url}"
        )
        html_sections.append(
            "<section style='margin:0 0 24px'>"
            f"<h2 style='font-size:18px'>{html.escape(status)}</h2>"
            f"<p style='color:#666'>{html.escape(created)}</p>"
            f"<p style='white-space:pre-wrap'>{html.escape(post.text)}</p>"
            f"<p><a href='{html.escape(post.url)}'>查看 X 原文</a></p>"
            "</section>"
        )

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = os.getenv("EMAIL_FROM", required_env("SMTP_USER"))
    message["To"] = required_env("EMAIL_TO")
    message.set_content(
        "检测到 Tibo 发布了与 Codex 重置有关的新消息：\n\n"
        + "\n\n---\n\n".join(text_sections)
        + "\n\n此邮件由 Tibo Codex Reset Bot 自动发送。"
    )
    message.add_alternative(
        "<html><body>"
        "<h1 style='font-size:22px'>Codex 重置提醒</h1>"
        "<p>检测到 Tibo 发布了与 Codex 重置有关的新消息：</p>"
        + "".join(html_sections)
        + "<p style='color:#777;font-size:12px'>此邮件由 Tibo Codex Reset Bot 自动发送。</p>"
        "</body></html>",
        subtype="html",
    )
    return message


def send_email(message: EmailMessage) -> None:
    host = required_env("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "465"))
    username = required_env("SMTP_USER")
    password = required_env("SMTP_PASSWORD")
    use_ssl = os.getenv("SMTP_SSL", "true").lower() not in ("0", "false", "no")

    context = ssl.create_default_context()
    if use_ssl:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as server:
            server.login(username, password)
            server.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(username, password)
            server.send_message(message)


def main() -> int:
    now = datetime.now(timezone.utc)
    lookback = int(os.getenv("FIRST_RUN_LOOKBACK_MINUTES", "60"))
    posts, source = fetch_posts()
    seen_ids = load_seen_ids()
    new_posts = select_new_posts(posts, seen_ids, now, lookback)
    matches = [
        (post, kind)
        for post in new_posts
        if (kind := classify_reset(post.text)) is not None
    ]

    if matches:
        send_email(build_email(matches))
        print(f"Sent one alert email for {len(matches)} matching post(s).")
    else:
        print(f"Checked {len(new_posts)} new post(s); no reset announcement found.")

    # Advance only after any required alert has been sent successfully.
    write_state(seen_ids | {post.id for post in posts})
    print(f"RSS source used: {source}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
