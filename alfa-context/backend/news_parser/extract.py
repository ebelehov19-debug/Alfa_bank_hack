from __future__ import annotations

import html
import json
import re
from datetime import datetime
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
import feedparser

from .access import is_challenge
from .models import Candidate, canonical_url, parse_date, request_url


def soup_of(value: str) -> BeautifulSoup:
    return BeautifulSoup(value, "html.parser")


def clean_text(value: str) -> str:
    soup = soup_of(value)
    for tag in soup.select("script, style, nav, footer, header, aside, form, button, iframe"):
        tag.decompose()
    for tag in soup.select("br"):
        tag.replace_with("\n")
    for tag in soup.select("p, div, li, tr, h1, h2, h3"):
        tag.insert_after("\n")
    lines = [re.sub(r"[ \t\r\f\v\u00a0]+", " ", line).strip()
             for line in soup.get_text().splitlines()]
    return "\n".join(line for line in lines if line)


def rss_candidates(raw: str, source: str) -> list[Candidate]:
    feed = feedparser.parse(raw)
    if not feed.get("version"):
        raise ValueError("Expected RSS/Atom, received a different document")
    result = []
    for entry in feed.entries:
        try:
            url = request_url(entry.get("link", ""))
        except ValueError:
            continue
        cats = [t.term for t in entry.get("tags", []) if t.get("term")]
        if source == "tass" and not urlsplit(url).path.startswith("/ekonomika/"):
            continue
        body = "\n".join(clean_text(c.value) for c in entry.get("content", []) if c.get("value"))
        result.append(Candidate(url, html.unescape(entry.get("title", "")),
                                parse_date(entry.get("published")), cats, text=body))
    return result


def jsonld_nodes(soup):
    def walk(node):
        if isinstance(node, list):
            for item in node:
                yield from walk(item)
        elif isinstance(node, dict):
            typ = node.get("@type", [])
            typ = [typ] if isinstance(typ, str) else typ
            if any(t in {"NewsArticle", "Article", "ReportageNewsArticle"} for t in typ):
                yield node
            for value in node.values():
                if isinstance(value, (list, dict)):
                    yield from walk(value)
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            yield from walk(json.loads(script.string or script.get_text()))
        except (ValueError, TypeError):
            continue


BODY_SELECTORS = {
    "tass": ['[itemprop="articleBody"]', '.news-content .text-block', '.text-block', '.news-content',
             '[class*="NewsContent_text"]', '[class*="Text_text"]', 'article'],
    "moex": ['[itemprop="articleBody"]', '.news-detail__content', '.news__text',
             '.news-content', '.text-block', 'article'],
    "e_disclosure": ['.disclosure', '.event-message', '#cont_wrap',
                     '[itemprop="articleBody"]', '#content'],
}


def article_from_html(raw: str, source: str, candidate: Candidate, *,
                      observed_at: datetime | None = None,
                      started_at: datetime | None = None, rendered: bool = False) -> Candidate:
    soup = soup_of(raw)
    if is_challenge(raw):
        raise ValueError('Visitor check instead of an article')
    title = soup.select_one('title')
    heading = soup.select_one('h1')
    page_labels = [(node.get_text(' ', strip=True).lower()) for node in (title, heading) if node]
    if any(label in {'forbidden', 'access denied', 'just a moment...', 'доступ ограничен'}
           or label.startswith(('checking your browser', 'verify you are human', 'проверка браузера'))
           for label in page_labels) or soup.select_one('form[action*="captcha"]'):
        raise ValueError('Access challenge instead of an article')
    if source == 'tass':
        from .tass_extract import extract_tass
        return extract_tass(raw, candidate, observed_at=observed_at, started_at=started_at,
                            rendered=rendered)
    result = Candidate(**vars(candidate))
    for node in jsonld_nodes(soup):
        result.title = clean_text(node.get("headline", "")) or result.title
        result.published_at = parse_date(node.get("datePublished")) or result.published_at
        result.text = clean_text(node.get("articleBody", "")) or result.text
        section = node.get("articleSection", [])
        result.categories = ([section] if isinstance(section, str) else section) or result.categories
        if result.text:
            break
    if not result.title:
        h1 = soup.select_one('h1, [itemprop="headline"]')
        if h1:
            result.title = h1.get_text(" ", strip=True)
    for selector, attr in [('meta[property="article:published_time"]', 'content'),
                           ('meta[itemprop="datePublished"]', 'content'),
                           ('time[itemprop="datePublished"]', 'datetime'),
                           ('time[datetime]', 'datetime')]:
        node = soup.select_one(selector)
        if node:
            result.published_at = parse_date(node.get(attr)) or result.published_at
            if result.published_at:
                break
    if source == "e_disclosure":
        header = soup.select_one('#cont_wrap') or soup.select_one('#content') or soup.select_one('main') or soup
        if header:
            header_copy = soup_of(str(header))
            text = header_copy.get_text(" ", strip=True).split("1. Общие сведения")[0]
            m = re.search(r"\b\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}(?::\d{2})?", text)
            if m:
                result.published_at = parse_date(m[0]) or result.published_at
            h = header.select_one('h4, h3')
            if h:
                result.title = h.get_text(" ", strip=True)
                result.categories = [result.title]
            issuer_heading = header.select_one('h2')
            if issuer_heading:
                result.issuer = issuer_heading.get_text(" ", strip=True)
        issuer = soup.select_one('a[href*="company.aspx?id="], a[href*="company.aspx?ID="]')
        if issuer and not result.issuer:
            issuer_text = issuer.get_text(" ", strip=True)
            if issuer_text not in {"Карточка компании", "Компания"}:
                result.issuer = issuer_text
    if not result.text:
        for selector in BODY_SELECTORS[source]:
            node = soup.select_one(selector)
            if node:
                text = clean_text(str(node))
                if len(text) >= 40:
                    result.text = text
                    break
    if source == 'e_disclosure' and not result.text:
        text = clean_text(str(header))
        if re.search(r'1\.\s*Общие сведения', text):
            result.text = text
    if source == 'e_disclosure' and result.text:
        marker = re.search(r'1\.\s*Общие сведения', result.text)
        if marker:
            result.text = result.text[marker.start():]
            result.text = result.text.split('Настоящее сообщение предоставлено')[0].strip()
    return result


PATTERNS = {"tass": re.compile(r"^/ekonomika/\d+/?$"),
            "moex": re.compile(r"^/[a-z]{1,3}/n\d+/?$|^/n\d+/?$"),
            "e_disclosure": re.compile(r"^/portal/event\.aspx$", re.I)}


def listing_candidates(raw: str, base: str, source: str) -> list[Candidate]:
    soup = soup_of(raw)
    found = {}
    for a in soup.select('a[href]'):
        try:
            url = request_url(urljoin(base, a['href']))
        except ValueError:
            continue
        if not PATTERNS[source].search(urlsplit(url).path):
            continue
        host = urlsplit(url).hostname.removeprefix('www.')
        expected = {"tass": "tass.ru", "moex": "moex.com", "e_disclosure": "e-disclosure.ru"}[source]
        if host != expected:
            continue
        container = a.find_parent('tr') or a.find_parent('article') or a.parent
        time = container.select_one('time') if container else None
        date = parse_date(time.get('datetime', time.get_text(" ", strip=True))) if time else None
        if not date and container:
            date = parse_date(container.get_text(" ", strip=True))
        issuer = None
        if source == "e_disclosure" and container:
            c = container.select_one('a[href*="company.aspx"]')
            issuer = c.get_text(" ", strip=True) if c else None
        candidate = Candidate(url, a.get_text(" ", strip=True), date, issuer=issuer)
        if url not in found or (date and not found[url].published_at):
            found[url] = candidate
    return list(found.values())


def next_page(raw: str, base: str) -> str | None:
    soup = soup_of(raw)
    for a in soup.select('a[rel="next"], .pagination a, .pager a, a.next'):
        if 'next' in a.get('rel', []) or re.search(r"след|далее|next|[›»]", a.get_text(" ", strip=True), re.I):
            try:
                return request_url(urljoin(base, a['href']))
            except (ValueError, KeyError):
                pass
    return None
