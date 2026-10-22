"""TASS-specific extraction. UI text must never become evidence for the LLM."""
from __future__ import annotations

from dataclasses import replace
import json
import re
from urllib.parse import urlsplit
from datetime import datetime, timezone

from .models import Candidate, news_id, parse_date

MONTHS = 'января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря'
DATELINE = re.compile(
    rf'(?m)^(?:[А-ЯЁA-Z][^\n]{{0,90}}),\s*\d{{1,2}}\s+(?:{MONTHS})'
    r'[^\n]{0,40}/(?:Корр\.\s*)?ТАСС\b[^/\n]*/\.', re.I)
UI_LINE = re.compile(r'^(?:Главная\s*Экономика.*|Редакция сайта ТАСС|Читайте ТАСС в|'
                     r'обновлено\s+\d{1,2}:\d{2}|©.*)$', re.I)
TAG_PREFIX = re.compile(r'^(?:Россия|США|Италия|Китай|Германия|Франция|Великобритания|'
                        r'Евросоюз|Белоруссия|Беларусь|Казахстан|Чечня|Краснодарский край)(?=$|[А-ЯЁ])')
NOISE_CLASS = re.compile(r'(?:^|[-_])(?:breadcrumb|related|readalso|read-also|share|'
                         r'social|caption|copyright|tags?|author|advert|recommendation)(?:$|[-_])', re.I)


def clean_tass_text(text: str, *, title: str = '', categories=()) -> str:
    """Conservative cleanup also usable for already exported JSON.

    A dateline is a boundary for stripping a page header, never a publication date.
    Quotes, numbers and paragraph contents are not rewritten.
    """
    lines = [re.sub(r'[ \t\r\u00a0]+', ' ', x).strip() for x in text.splitlines()]
    text = '\n'.join(x for x in lines if x)
    match = DATELINE.search(text)
    if match and match.start() <= 1600:
        prefix = text[:match.start()]
        if not prefix or any(label in prefix for label in ('Главная', 'Редакция сайта ТАСС', 'Читайте ТАСС в', '©')) or (title and title in prefix):
            text = text[match.start():]
    result, skip_related_title = [], False
    for line in text.splitlines():
        if line.lower() == 'читайте также':
            skip_related_title = True
            continue
        if skip_related_title:
            skip_related_title = False
            continue
        if UI_LINE.fullmatch(line) or (title and line == title):
            continue
        result.append(line)
    # Flat legacy exports have no tag DOM. Only strip short, punctuation-free
    # trailing lines identifiable as source topics/country tags.
    normalized_categories = {re.sub(r'\s+', '', str(c)) for c in categories}
    while len(result) > 1:
        tail = result[-1]
        category = re.sub(r'\s+', '', tail) in normalized_categories
        country_tags = len(tail) < 240 and not re.search(r'[.!?;:»"]$', tail) and (
            TAG_PREFIX.match(tail) or bool(re.match(
                r'^[А-ЯЁ][а-яё-]+,\s*[А-ЯЁ][а-яё-]+(?:\s+[А-ЯЁ][а-яё-]+)?Россия', tail)))
        if category or country_tags:
            result.pop()
        else:
            break
    return '\n'.join(result).strip()


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _same_article(node, url):
    current_id = urlsplit(url).path.rstrip('/').rsplit('/', 1)[-1]
    for key in ('url', 'canonicalUrl', 'canonical_url', 'link', '@id'):
        value = node.get(key)
        if isinstance(value, str) and '/ekonomika/' in value:
            try:
                return news_id('tass', value) == news_id('tass', url)
            except ValueError:
                return False
    return any(str(node.get(key, '')) == current_id for key in ('id', 'newsId', 'news_id'))


def _publication_date(value):
    if isinstance(value, (int, float)):
        if 1_000_000_000 <= value < 100_000_000_000_000:
            try:
                return datetime.fromtimestamp(value / 1000 if value > 100_000_000_000 else value,
                                              tz=timezone.utc)
            except (ValueError, OSError, OverflowError):
                return None
        return None
    return parse_date(value) if isinstance(value, (str, datetime)) else None


def _prune(soup):
    for node in list(soup.select('script, style, nav, header, footer, aside, figure, figcaption, '
                                 'button, form, iframe, h1, time, [itemprop="keywords"]')):
        if node.parent:
            node.decompose()
    for node in list(soup.find_all(class_=True)):
        if node.parent and any(NOISE_CLASS.search(c) for c in node.get('class', [])):
            node.decompose()
    # Legacy related blocks sometimes have no useful class name.
    for label in list(soup.find_all(string=lambda s: s and s.strip().lower() == 'читайте также')):
        block = label.find_parent(['aside', 'div', 'section'])
        has_body = block and any(len(p.get_text(' ', strip=True)) >= 100 for p in block.select('p'))
        if block and block.parent and not has_body and not block.find(['h1', 'article']) and len(block.get_text()) < 600:
            block.decompose()


def extract_tass(raw: str, candidate: Candidate) -> Candidate:
    from .extract import clean_text, jsonld_nodes, soup_of
    result = replace(candidate, categories=list(candidate.categories), text='')
    soup = soup_of(raw)
    structured_date = None
    structured_text = ''
    for node in jsonld_nodes(soup):
        # Ignore explicitly linked related articles in a JSON-LD graph.
        explicit = any(isinstance(node.get(k), str) and '/ekonomika/' in node[k] for k in ('url', '@id'))
        if explicit and not _same_article(node, candidate.url):
            continue
        result.title = clean_text(node.get('headline', '')) or result.title
        structured_date = _publication_date(node.get('datePublished')) or structured_date
        structured_text = clean_text(node.get('articleBody', '')) or structured_text
        section = node.get('articleSection')
        if isinstance(section, str):
            result.categories = [section]
        elif isinstance(section, list):
            result.categories = [x for x in section if isinstance(x, str)]
        if structured_text:
            break
    h1 = soup.select_one('h1')
    if h1:
        result.title = h1.get_text(' ', strip=True)
    # Publication-only fields take priority over the RSS timestamp.
    for selector in ('[itemprop="datePublished"]', 'meta[property="article:published_time"]',
                     'meta[property="og:published_time"]', 'meta[name="publish-date"]',
                     'meta[name="publication_date"]', 'meta[name="mediator_published_time"]'):
        tag = soup.select_one(selector)
        if tag:
            date = _publication_date(tag.get('content') or tag.get('datetime') or tag.get_text(' ', strip=True))
            if date:
                structured_date = date
                break
    if not structured_date:
        # A single unlabelled semantic time is accepted; explicit update times
        # and times inside unrelated sidebars/figures are never publication dates.
        primary_times = []
        for tag in soup.select('time[datetime]'):
            labels = ' '.join([str(tag.get('itemprop', '')), str(tag.get('class', '')),
                               tag.get_text(' ', strip=True),
                               str(tag.previous_sibling or '')[-80:]]).lower()
            if tag.find_parent(['aside', 'figure', 'footer']) or any(
                    word in labels for word in ('modified', 'updated', 'обновлено')):
                continue
            date = _publication_date(tag.get('datetime'))
            if date:
                primary_times.append(date)
        if len(primary_times) == 1:
            structured_date = primary_times[0]
    # Client-rendered pages can keep the publication time in their JSON state.
    # Only objects matching the current article ID/URL are eligible.
    if not structured_date:
        for script in soup.select('script[type="application/json"], script#__NEXT_DATA__'):
            try:
                data = json.loads(script.string or script.get_text())
            except (ValueError, TypeError):
                continue
            for node in _walk(data):
                if _same_article(node, candidate.url):
                    for key in ('datePublished', 'published_at', 'publishedAt', 'publicationDate', 'publishDate'):
                        date = _publication_date(node.get(key))
                        if date:
                            structured_date = date
                            break
                if structured_date:
                    break
            if structured_date:
                break
    result.published_at = structured_date or candidate.published_at
    if structured_text:
        result.text = clean_tass_text(structured_text, title=result.title, categories=result.categories)
        return result
    # Favor a semantic body. Broad <article> fallback is pruned before reading.
    for selector in ('[itemprop="articleBody"]', '.news-content', '[class*="NewsContent_text"]',
                     'article', 'main'):
        root = soup.select_one(selector)
        if not root:
            continue
        clone = soup_of(str(root))
        _prune(clone)
        paragraphs = clone.select('p')
        # Include section headings in DOM order; preserve all paragraphs, not
        # only the first text-block. Inline links remain part of the paragraph.
        if paragraphs:
            blocks = clone.select('p, h2, h3, h4, li')
            selected = [n for n in blocks if not n.find_parent(['p', 'li'])]
            text = '\n'.join(clean_text(str(n)) for n in selected)
        else:
            text = clean_text(str(clone))
        text = clean_tass_text(text, title=result.title, categories=result.categories)
        if len(text) >= 40 and (selector != 'main' or DATELINE.search(text)):
            result.text = text
            break
    return result


def invalid_fields(candidate: Candidate) -> list[str]:
    reasons = []
    if not candidate.title.strip():
        reasons.append('missing_title')
    if not candidate.published_at:
        reasons.append('missing_published_at')
    if len(candidate.text.strip()) < 40:
        reasons.append('missing_or_short_body')
    return reasons
