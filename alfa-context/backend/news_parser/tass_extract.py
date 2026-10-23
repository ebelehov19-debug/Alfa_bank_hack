from __future__ import annotations

from dataclasses import replace
import json
import re
from urllib.parse import urljoin, urlsplit
from itertools import chain
from datetime import datetime, timedelta, timezone

from .models import Candidate, MSK, news_id, parse_date

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
DISPLAY_DATE = re.compile(
    rf'(?:(?P<day>\d{{1,2}})\s+(?P<month>{MONTHS})(?:\s+(?P<year>\d{{4}})(?:\s*г\.?)?)?'
    r'\s*,?\s*|(?P<relative>сегодня|вчера)\s*,?\s*)?'
    r'(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?', re.I)


def parse_tass_display_date(value: str, *, observed_at: datetime,
                            started_at: datetime | None = None) -> datetime | None:
    if observed_at.tzinfo is None or (started_at is not None and started_at.tzinfo is None):
        return None
    local = observed_at.astimezone(MSK)
    value = re.sub(r'\s+', ' ', value).strip()
    publication = re.split(r'(?<![а-яёa-z])(?:обновлено|обновлён|обновлен|updated)\b', value, maxsplit=1,
                           flags=re.I)[0].strip()
    match = DISPLAY_DATE.fullmatch(publication)
    if not match:
        return None
    fields = match.groupdict()
    if fields['day']:
        day = int(fields['day'])
        month = MONTHS.split('|').index(fields['month'].lower()) + 1
        if fields['year']:
            year = int(fields['year'])
        else:
            year = local.year - ((month, day) > (local.month, local.day))
    else:
        if started_at and started_at.astimezone(MSK).date() != local.date():
            return None
        offset = 1 if fields['relative'] and fields['relative'].lower() == 'вчера' else 0
        date = local.date() - timedelta(days=offset)
        year, month, day = date.year, date.month, date.day
    try:
        parsed = datetime(year, month, day, int(fields['hour']), int(fields['minute']),
                          int(fields['second'] or 0), tzinfo=MSK)
    except ValueError:
        return None
    if not fields['day'] and parsed > local:
        return None
    return parsed.astimezone(timezone.utc)


def _is_update_or_unrelated(tag):
    for node in (tag, *tag.parents):
        if node.name in {'aside', 'figure', 'footer', 'nav'}:
            return True
        if node.get('itemprop') == 'articleBody' or any(
                c in {'news-content', 'text-block'} or 'NewsContent_text' in c
                for c in node.get('class', [])):
            return True
        labels = ' '.join((str(node.get('itemprop', '')), str(node.get('class', ''))))
        if re.search(r'modified|updated|(?:^|[_\W])update(?:[_\W]|$)|related|readalso', labels, re.I):
            return True
    return False


def _visible_publication_date(soup, *, observed_at, started_at, rendered=False):
    page_dates = soup.select('[class*="PageDateLabel_published"]')
    if page_dates and not rendered:
        return None
    for tag in page_dates:
        if not _is_update_or_unrelated(tag):
            date = parse_tass_display_date(tag.get_text(' ', strip=True),
                                           observed_at=observed_at, started_at=started_at)
            if date:
                return date
    for tag in soup.select('[itemprop="datePublished"]'):
        if not _is_update_or_unrelated(tag):
            date = parse_tass_display_date(
                tag.get('content') or tag.get('datetime') or tag.get_text(' ', strip=True),
                observed_at=observed_at, started_at=started_at)
            if date:
                return date
    dates = set()
    authors = []
    for label in soup.find_all(string=re.compile(r'редакция', re.I)):
        node = label.parent
        for _ in range(3):
            if node is None:
                break
            text = re.sub(r'\s+', ' ', node.get_text(' ', strip=True)).lower()
            if text == 'редакция сайта тасс':
                authors.append(node)
                break
            if len(text) > 100:
                break
            node = node.parent
    for node in authors:
        for _ in range(6):
            if node is None or node.name in {'body', 'html', '[document]'}:
                break
            if not _is_update_or_unrelated(node):
                text = node.get_text(' ', strip=True)
                if len(text) <= 500:
                    from .extract import soup_of
                    block = soup_of(str(node))
                    for part in list(block.find_all(True)):
                        if part.parent and _is_update_or_unrelated(part):
                            part.decompose()
                    text = re.sub(r'\s+', ' ', block.get_text(' ', strip=True))
                    suffix = re.split(r'редакция сайта тасс', text, maxsplit=1, flags=re.I)[-1].strip()
                    date = parse_tass_display_date(suffix, observed_at=observed_at,
                                                   started_at=started_at)
                    if date:
                        dates.add(date)
                        break
            node = node.parent
    return dates.pop() if len(dates) == 1 else None


def clean_tass_text(text: str, *, title: str = '', categories=()) -> str:
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
    values = [node.get(key) for key in ('url', 'canonicalUrl', 'canonical_url', 'link', '@id')]
    entity = node.get('mainEntityOfPage')
    values.append(entity.get('@id') if isinstance(entity, dict) else entity)
    for value in values:
        if isinstance(value, str) and '/ekonomika/' in value:
            try:
                absolute = urljoin(url, value)
                if urlsplit(absolute).hostname not in {'tass.ru', 'www.tass.ru'}:
                    return False
                return news_id('tass', absolute) == news_id('tass', url)
            except ValueError:
                return False
    return any(str(node.get(key, '')) == current_id for key in ('id', 'newsId', 'news_id'))


NEXT_PUSH = re.compile(
    r'(?:self\.__next_[sf]|\(self\.__next_[sf]\s*=\s*self\.__next_[sf]\s*\|\|\s*\[\]\))'
    r'\s*\.push\s*\(\s*')


def _queued_jsonld_nodes(soup, url):
    decoder = json.JSONDecoder()
    flight = []
    payloads = []
    for script in soup.select('script:not([src])'):
        text = script.string or script.get_text()
        for match in NEXT_PUSH.finditer(text):
            try:
                payload, end = decoder.raw_decode(text, match.end())
            except (ValueError, RecursionError):
                continue
            if not isinstance(payload, list) or len(payload) != 2 or not text[end:].lstrip().startswith(')'):
                continue
            if '__next_f' in match[0]:
                if payload[0] == 1 and isinstance(payload[1], str):
                    flight.append(payload[1])
            else:
                payloads.append(payload)
    stream = ''.join(flight)
    for match in re.finditer(r'(?:^|\n)[0-9a-fA-F]+:', stream):
        try:
            value, end = decoder.raw_decode(stream, match.end())
        except (ValueError, RecursionError):
            continue
        if isinstance(value, list) and (end == len(stream) or stream[end] == '\n'):
            payloads.append(value)
    for payload in payloads:
        for props in _walk(payload):
            if props.get('type') != 'application/ld+json':
                continue
            content = props.get('children')
            if not isinstance(content, str):
                inner = props.get('dangerouslySetInnerHTML')
                content = inner.get('__html') if isinstance(inner, dict) else None
            if not isinstance(content, str):
                continue
            try:
                document = json.loads(content)
            except (ValueError, RecursionError):
                continue
            for node in _walk(document):
                types = node.get('@type', [])
                types = [types] if isinstance(types, str) else types
                if isinstance(types, list) and any(t in ('NewsArticle', 'Article', 'ReportageNewsArticle')
                                                  for t in types) and _same_article(node, url):
                    yield node


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
    for label in list(soup.find_all(string=lambda s: s and s.strip().lower() == 'читайте также')):
        block = label.find_parent(['aside', 'div', 'section'])
        has_body = block and any(len(p.get_text(' ', strip=True)) >= 100 for p in block.select('p'))
        if block and block.parent and not has_body and not block.find(['h1', 'article']) and len(block.get_text()) < 600:
            block.decompose()


def extract_tass(raw: str, candidate: Candidate, *, observed_at: datetime | None = None,
                 started_at: datetime | None = None, rendered: bool = False) -> Candidate:
    from .extract import clean_text, jsonld_nodes, soup_of
    result = replace(candidate, categories=list(candidate.categories), text='')
    soup = soup_of(raw)
    structured_date = None
    structured_text = ''
    for node in chain(jsonld_nodes(soup), _queued_jsonld_nodes(soup, candidate.url)):
        entity = node.get('mainEntityOfPage')
        explicit = any(isinstance(node.get(k), str) and '/ekonomika/' in node[k] for k in ('url', '@id')) or bool(entity)
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
    if not result.published_at:
        result.published_at = _visible_publication_date(
            soup, observed_at=observed_at or datetime.now(timezone.utc), started_at=started_at,
            rendered=rendered)
    if structured_text:
        result.text = clean_tass_text(structured_text, title=result.title, categories=result.categories)
        return result
    for selector in ('[itemprop="articleBody"]', '.news-content', '[class*="NewsContent_text"]',
                     'article', 'main'):
        root = soup.select_one(selector)
        if not root:
            continue
        clone = soup_of(str(root))
        _prune(clone)
        paragraphs = clone.select('p')
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
