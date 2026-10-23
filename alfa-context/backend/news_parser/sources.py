from __future__ import annotations

import html
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit

from .extract import (article_from_html, clean_text, listing_candidates, next_page,
                      rss_candidates)
from .models import Article, Candidate, Settings, canonical_url, news_id, parse_date, request_url

log = logging.getLogger(__name__)


def iss_rows(data, block):
    table = data[block]
    return [dict(zip(table['columns'], row)) for row in table['data']]


@dataclass
class SourceResult:
    source: str
    articles: list[Article] = field(default_factory=list)
    discovered: int = 0
    already_seen: int = 0
    outside_window: int = 0
    invalid: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)
    discovery_ok: bool = False
    invalid_reasons: dict[str, int] = field(default_factory=dict)
    invalid_records: list[dict] = field(default_factory=list)

    @property
    def status(self):
        if not self.discovery_ok:
            return "error"
        if self.errors or self.warnings or self.invalid:
            return "partial"
        return "ok"

    def to_dict(self):
        data = {"status": self.status, "exported": len(self.articles),
                "discovered": self.discovered, "already_seen": self.already_seen,
                "outside_window": self.outside_window, "invalid": self.invalid,
                "methods": list(dict.fromkeys(self.methods)),
                "warnings": self.warnings, "errors": self.errors}
        if self.source == 'tass':
            data.update(invalid_reasons=self.invalid_reasons, invalid_records=self.invalid_records)
        return data


class Source:
    name = ""
    rss = None
    listing_url = ""

    def __init__(self, transport, settings: Settings):
        self.transport = transport
        self.settings = settings

    async def discover(self, cutoff, now, result, target):
        found = {}
        if self.rss:
            try:
                for c in rss_candidates(await self.transport.get(self.rss), self.name):
                    found[c.url] = c
                result.methods.append("rss")
                result.discovery_ok = True
            except Exception as exc:
                result.warnings.append(f"RSS unavailable, using HTML: {exc}")
        url, visited = self.listing_url, set()
        http_failed = False
        for _ in range(self.settings.max_pages):
            if url in visited:
                break
            visited.add(url)
            try:
                raw = await self.transport.get(url)
                items = listing_candidates(raw, url, self.name)
                if not items:
                    http_failed = True
                    break
                result.discovery_ok = True
                result.methods.append("http_html")
                for c in items:
                    found.setdefault(c.url, c)
                if len(found) >= target:
                    break
                url = next_page(raw, url)
                if not url:
                    break
            except Exception as exc:
                http_failed = True
                log.info("%s HTTP list failed: %s", self.name, exc)
                break
        if self.settings.browser and (len(found) < target or http_failed):
            try:
                pages = await self.transport.listing(self.listing_url, self.name, target, cutoff)
                for raw in pages:
                    for c in listing_candidates(raw, self.listing_url, self.name):
                        found.setdefault(c.url, c)
                if pages and any(listing_candidates(p, self.listing_url, self.name) for p in pages):
                    result.discovery_ok = True
                    result.methods.append("chromium_html")
                else:
                    result.errors.append("Chromium list has no recognized article links; check access and selectors")
            except Exception as exc:
                result.errors.append(f"Chromium discovery: {exc}")
        elif http_failed:
            result.errors.append("HTTP listing unavailable and browser fallback disabled")
        if len(found) < target:
            result.warnings.append(
                "Public list/RSS is bounded: full 48h coverage is not confirmed; see README")
        return list(found.values())

    async def hydrate(self, candidate, result):
        if candidate.text and candidate.published_at and len(candidate.text) >= 40:
            return candidate
        try:
            raw = await self.transport.get(candidate.url)
            article = article_from_html(raw, self.name, candidate)
            if article.published_at and article.title and len(article.text) >= 40:
                result.methods.append("http_html")
                return article
        except Exception as exc:
            log.info("%s HTTP article failed: %s", self.name, exc)
        raw = await self.transport.render(candidate.url)
        result.methods.append("chromium_html")
        return article_from_html(raw, self.name, candidate)

    async def collect(self, cutoff: datetime, now: datetime, seen: set[str]):
        result = SourceResult(self.name)
        try:
            candidates = await self.discover(cutoff, now, result, self.settings.limit + len(seen))
        except Exception as exc:
            result.errors.append(f"Discovery failed: {exc}")
            return result
        candidates.sort(key=lambda c: c.published_at or now, reverse=True)
        all_dated = all(c.published_at for c in candidates)
        unique, accepted, failure_streak = set(), [], 0
        result.discovered = len(candidates)
        for candidate in candidates:
            if all_dated and len(accepted) >= self.settings.limit:
                break
            try:
                candidate.url = request_url(candidate.url)
                key = news_id(self.name, candidate.url)
                if key in unique:
                    continue
                unique.add(key)
                if key in seen:
                    result.already_seen += 1
                    continue
                if candidate.published_at and not cutoff <= candidate.published_at <= now:
                    result.outside_window += 1
                    continue
                full = await self.hydrate(candidate, result)
                failure_streak = 0
                if not full.published_at or not full.title.strip() or len(full.text.strip()) < 40:
                    result.invalid += 1
                    continue
                if not cutoff <= full.published_at <= now:
                    result.outside_window += 1
                    continue
                accepted.append(Article(key, self.name, candidate.url, html.unescape(full.title).strip(),
                                        full.text.strip(), full.published_at, now,
                                        list(dict.fromkeys(full.categories)), full.issuer))
            except Exception as exc:
                result.errors.append(f"Article {candidate.url}: {exc}")
                failure_streak += 1
                if failure_streak >= 5:
                    result.warnings.append("Stopped after 5 consecutive article failures; retry on the next run")
                    break
        accepted.sort(key=lambda a: (a.published_at, a.id), reverse=True)
        result.articles = accepted[:self.settings.limit]
        return result


class Moex(Source):
    name = "moex"
    rss = "https://www.moex.com/export/news.aspx?cat=100"
    listing_url = "https://www.moex.com/ru/news/"

    async def discover(self, cutoff, now, result, target):
        candidates = []
        try:
            start, ended = 0, False
            for _ in range(self.settings.max_pages):
                data = json.loads(await self.transport.get(
                    f"https://iss.moex.com/iss/sitenews.json?start={start}&iss.meta=off"))
                rows = iss_rows(data, "sitenews")
                result.methods.append("iss_api")
                result.discovery_ok = True
                if not rows:
                    ended = True
                    break
                for row in rows:
                    date = parse_date(row.get("published_at"))
                    if date and cutoff <= date <= now:
                        candidates.append(Candidate(f"https://www.moex.com/n{row['id']}",
                                                    html.unescape(row['title']), date,
                                                    source_id=str(row['id'])))
                dates = [parse_date(r.get('published_at')) for r in rows]
                if any(d and d < cutoff for d in dates) or len(candidates) >= target:
                    ended = True
                    break
                start += len(rows)
            if not ended:
                result.warnings.append("ISS pagination reached --max-pages")
            return candidates
        except Exception as exc:
            result.warnings.append(f"ISS API failed; falling back to RSS/HTML: {exc}")
            fallback = await super().discover(cutoff, now, result, target)
            return candidates + fallback

    async def hydrate(self, candidate, result):
        source_id = candidate.source_id
        if source_id:
            try:
                data = json.loads(await self.transport.get(
                    f"https://iss.moex.com/iss/sitenews/{source_id}.json?iss.meta=off"))
                row = iss_rows(data, "content")[0]
                article = Candidate(candidate.url, html.unescape(row['title']),
                                    parse_date(row.get('published_at')) or candidate.published_at,
                                    text=clean_text(row.get('body', '')), source_id=source_id)
                if len(article.text) >= 40:
                    result.methods.append("iss_api")
                    return article
            except Exception as exc:
                log.info("MOEX detail API fallback: %s", exc)
        return await super().hydrate(candidate, result)


class Tass(Source):
    name = "tass"
    rss = "https://tass.ru/rss/v2.xml"
    listing_url = "https://tass.ru/ekonomika"

    def __init__(self, transport, settings):
        class TassTransport:
            def __getattr__(self, name):
                return getattr(transport, name)

            async def render(self, url):
                return await getattr(transport, 'tass_render', transport.render)(url)

            async def listing(self, *args):
                return await getattr(transport, 'tass_listing', transport.listing)(*args)
        super().__init__(TassTransport(), settings)
        self._browser_only = False

    async def hydrate(self, candidate, result):
        from .tass_extract import clean_tass_text, invalid_fields
        from dataclasses import replace
        from .models import UTC
        best = None
        if candidate.text:
            best = replace(candidate, text=clean_tass_text(
                candidate.text, title=candidate.title, categories=candidate.categories))
            if not invalid_fields(best):
                return best
        if not self._browser_only:
            try:
                started_at = datetime.now(UTC)
                raw = await self.transport.get(candidate.url)
                best = article_from_html(raw, self.name, candidate,
                                         observed_at=datetime.now(UTC), started_at=started_at)
                if not invalid_fields(best):
                    result.methods.append('http_html')
                    return best
            except Exception as exc:
                message = str(exc).lower()
                if '403' in message or 'javascript access check' in message:
                    self._browser_only = True
                log.debug('TASS HTTP fallback: %s', exc)
        if self.settings.browser:
            try:
                started_at = datetime.now(UTC)
                raw = await self.transport.render(candidate.url)
                browser_article = article_from_html(raw, self.name, candidate,
                                                    observed_at=datetime.now(UTC), started_at=started_at,
                                                    rendered=True)
                result.methods.append('chromium_html')
                if best and not browser_article.published_at:
                    browser_article.published_at = best.published_at
                best = browser_article
            except Exception as exc:
                if best is None:
                    raise
                result.errors.append(f'Article browser fallback {candidate.url}: {exc}')
        if best is None:
            raise RuntimeError(f'TASS full article unavailable: {candidate.url}')
        reasons = invalid_fields(best)
        if reasons:
            for reason in reasons:
                result.invalid_reasons[reason] = result.invalid_reasons.get(reason, 0) + 1
            if len(result.invalid_records) < 200:
                result.invalid_records.append({'url': candidate.url, 'reasons': reasons})
        return best


class EDisclosure(Source):
    name = "e_disclosure"
    listing_url = "https://www.e-disclosure.ru/poisk-po-soobshheniyam"

    async def discover(self, cutoff, now, result, target):
        home = 'https://www.e-disclosure.ru/'
        initial = []
        try:
            try:
                raw = await self.transport.get(home)
                method = 'http_html'
            except Exception:
                if not self.settings.browser:
                    raise
                raw = await self.transport.render(home)
                method = 'chromium_html'
            initial = listing_candidates(raw, home, self.name)
            if initial:
                result.discovery_ok = True
                result.methods.append(method)
        except Exception as exc:
            log.info('e_disclosure home session bootstrap: %s', exc)
        if len(initial) >= target:
            return initial
        found = await super().discover(cutoff, now, result, target)
        return list({c.url: c for c in initial + found}.values())


ADAPTERS = {"moex": Moex, "tass": Tass, "e_disclosure": EDisclosure}
