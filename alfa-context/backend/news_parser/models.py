from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo
import re
from email.utils import parsedate_to_datetime

UTC = timezone.utc
MSK = ZoneInfo("Europe/Moscow")
SOURCES = ("moex", "tass", "e_disclosure")
HOSTS = {"www.moex.com", "moex.com", "iss.moex.com", "tass.ru", "www.tass.ru",
         "e-disclosure.ru", "www.e-disclosure.ru"}


def canonical_url(value: str) -> str:
    p = urlsplit(value.strip())
    host = (p.hostname or "").lower()
    if p.scheme not in {"http", "https"} or host not in HOSTS or p.username or p.password:
        raise ValueError(f"URL outside supported sources: {host}")
    if p.port not in (None, 80, 443):
        raise ValueError("Nonstandard URL port")
    host = host.removeprefix("www.")
    pairs = [(k.lower(), v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in {"from", "yclid"}]
    return urlunsplit(("https", host, p.path or "/", urlencode(sorted(pairs)), ""))


def news_id(source: str, url: str) -> str:
    p = urlsplit(canonical_url(url))
    if source == "moex" and (m := re.search(r"/n(\d+)", p.path)):
        key = m[1]
    elif source == "tass" and (m := re.search(r"/(\d+)/?$", p.path)):
        key = m[1]
    elif source == "e_disclosure" and (event := dict(parse_qsl(p.query)).get("eventid")):
        key = event
    else:
        key = canonical_url(url)
    return source + ":" + sha256(key.encode()).hexdigest()[:24]


def parse_date(value: str | datetime | None) -> datetime | None:
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=MSK)).astimezone(UTC)
    if not value:
        return None
    value = re.sub(r"\s+", " ", value).strip()
    if not re.search(r'\d{1,2}:\d{2}', value):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            result = parsedate_to_datetime(value)
        except (ValueError, TypeError, IndexError):
            m = re.search(r"\b(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2}(?::\d{2})?)", value)
            if not m:
                return None
            fmt = "%d.%m.%Y %H:%M:%S" if len(m[2]) == 8 else "%d.%m.%Y %H:%M"
            try:
                result = datetime.strptime(m[1] + " " + m[2], fmt)
            except ValueError:
                return None
    return (result if result.tzinfo else result.replace(tzinfo=MSK)).astimezone(UTC)


@dataclass
class Candidate:
    url: str
    title: str = ""
    published_at: datetime | None = None
    categories: list[str] = field(default_factory=list)
    issuer: str | None = None
    text: str = ""
    source_id: str | None = None


@dataclass
class Article:
    id: str
    source: str
    url: str
    title: str
    text: str
    published_at: datetime
    collected_at: datetime
    categories: list[str] = field(default_factory=list)
    issuer: str | None = None
    language: str = "ru"

    def to_dict(self):
        d = asdict(self)
        for key in ("published_at", "collected_at"):
            d[key] = d[key].astimezone(UTC).isoformat()
        return d


@dataclass
class Settings:
    hours: int = 48
    limit: int = 200
    timeout: float = 20
    delay: float = 0.5
    retries: int = 2
    max_pages: int = 20
    browser: bool = True

    def __post_init__(self):
        if not 1 <= self.hours <= 48 or not 1 <= self.limit <= 200:
            raise ValueError("hours must be 1..48, limit must be 1..200")
        if self.timeout <= 0 or self.delay < 0 or not 0 <= self.retries <= 5 or self.max_pages < 1:
            raise ValueError("Invalid network or pagination settings")

    def cutoff(self, now: datetime) -> datetime:
        return now - timedelta(hours=self.hours)
