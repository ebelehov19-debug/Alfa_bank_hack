"""Extract a release's own HTML content, excluding related-news blocks."""
from __future__ import annotations

from datetime import datetime
import json
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from .access import is_challenge, is_denied
from .extract import clean_text
from .models import Candidate, MSK, UTC, canonical_url, parse_date, request_url


RELEASE_PATH = re.compile(r"^/news/t/release/[^/]+/?$")
LIST_PATH = re.compile(r"^/news/t/release(?:/page/\d+)?/?$")
MONTHS = {name: number for number, name in enumerate((
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
), 1)}
RUSSIAN_DATE = re.compile(
    r"(?<!\d)(\d{1,2})\s+(" + "|".join(MONTHS) + r")"
    r"(?:\s+(\d{4})(?:\s*г\.?)?)?(?:\s*(?:в\s*)?(\d{1,2}):(\d{2})(?::(\d{2}))?)?", re.I,
)
RELATED_HEADINGS = {"читайте также", "рекомендуем", "рекомендуемые новости", "другие новости",
                    "вам может быть интересно", "вас может заинтересовать", "похожие новости",
                    "главное про инвестиции"}


def alfa_url(value, base, *, listing=False):
    url = request_url(urljoin(base, value))
    parsed = urlsplit(url)
    pattern = LIST_PATH if listing else RELEASE_PATH
    if parsed.hostname not in {"alfabank.ru", "www.alfabank.ru"} or not pattern.fullmatch(parsed.path):
        raise ValueError("URL is not an Alfa release")
    if listing:
        return url
    canonical = urlsplit(canonical_url(url))
    return canonical._replace(path=canonical.path.rstrip('/') + '/', query='').geturl()


def parse_alfa_date(value, *, now=None, card=False):
    """Full publication timestamps are mandatory; cards supply day-level hints only."""
    if not isinstance(value, str):
        return None
    value = re.sub(r"\s+", " ", value).strip()
    parsed = parse_date(value)
    if parsed:
        return parsed
    matches = list(RUSSIAN_DATE.finditer(value))
    if not matches:
        if card and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            try:
                return datetime.fromisoformat(value).replace(tzinfo=MSK).astimezone(UTC)
            except ValueError:
                pass
        return None
    for match in matches:
        day, month, year, hour, minute, second = match.groups()
        if not card and (not year or hour is None):
            continue
        if not year and now is None:
            continue
        local_now = now.astimezone(MSK) if now is not None else None
        year = int(year) if year else local_now.year
        month = MONTHS[month.lower()]
        # Day-only hints around New Year belong to the preceding calendar year.
        if match[3] is None and month - local_now.month > 6:
            year -= 1
        try:
            return datetime(year, month, int(day), int(hour or 0), int(minute or 0),
                            int(second or 0), tzinfo=MSK).astimezone(UTC)
        except ValueError:
            continue
    return None


def alfa_listing_candidates(raw, base, *, now):
    if is_challenge(raw) or is_denied(raw):
        raise ValueError("Access check instead of Alfa release list")
    soup = BeautifulSoup(raw, "html.parser")
    root = soup.select_one('main, [role="main"]') or soup
    found = {}
    for anchor in root.select('a[href]'):
        try:
            url = alfa_url(anchor['href'], base)
        except ValueError:
            continue
        container = anchor
        # Find the nearest card, without borrowing another card's date/title.
        for parent in [anchor, *list(anchor.parents)[:5]]:
            links = set()
            for link in parent.select('a[href]'):
                try:
                    links.add(alfa_url(link['href'], base))
                except ValueError:
                    pass
            if len(links) > 1:
                break
            if parse_alfa_date(parent.get_text(' ', strip=True), now=now, card=True):
                container = parent
                break
        time = container.select_one('time, [itemprop="datePublished"]')
        value = (time.get('datetime') or time.get('content') or time.get_text(' ', strip=True)) if time else container.get_text(' ', strip=True)
        hint = parse_alfa_date(value, now=now, card=True)
        title = container.select_one('h2, h3, [itemprop="headline"], [class*="title" i]')
        title = title.get_text(' ', strip=True) if title else anchor.get_text(' ', strip=True)
        if url not in found or (hint and not found[url].published_at):
            found[url] = Candidate(url, title, hint, ["Пресс-релизы"], "Альфа-Банк")
    return list(found.values())


def alfa_next_page(raw, base):
    soup = BeautifulSoup(raw, "html.parser")
    query = parse_qs(urlsplit(base).query)
    current = next((int(values[0]) for key, values in query.items()
                    if re.fullmatch(r'page|pagen_\d+', key, re.I) and values[0].isdigit()), 1)
    path_page = re.search(r'/page/(\d+)/?$', urlsplit(base).path)
    if path_page:
        current = int(path_page[1])
    numbered = []
    for anchor in soup.select('a[href]'):
        if anchor.get('aria-disabled') == 'true' or 'disabled' in anchor.get('class', []):
            continue
        try:
            url = alfa_url(anchor['href'], base, listing=True)
        except ValueError:
            continue
        if url == canonical_url(base):
            continue
        labels = [anchor.get('aria-label', '').strip(), anchor.get_text(' ', strip=True)]
        if 'next' in anchor.get('rel', []) or any(re.search(r'следующ|далее|next|[›»→]', label, re.I) for label in labels):
            return url
        for label in labels:
            if label.isdigit() and int(label) > current:
                numbered.append((int(label), url))
                break
    return min(numbered)[1] if numbered else None


def _trim_related(root):
    for node in list(root.select('aside, nav, footer, form, button, script, style, iframe, '
                                  '.afCfF8, .sharing, '
                                  '[class*="recommend" i], [class*="related" i], '
                                  '[data-testid*="recommend" i]')):
        if node.parent is not None:
            node.decompose()
    for node in list(root.find_all(['h2', 'h3', 'h4', 'p', 'div'])):
        if node.parent is None or node.get_text(' ', strip=True).strip(':').lower() not in RELATED_HEADINGS:
            continue
        cursor = node
        while cursor is not root:
            for sibling in list(cursor.next_siblings):
                sibling.extract()
            parent = cursor.parent
            if cursor is node:
                cursor.decompose()
            cursor = parent
        break


def _state_article(soup, url):
    script = soup.select_one('script#app_state[type="application/json"]')
    if script is None:
        return None
    try:
        state = json.loads(script.string or script.get_text())
    except (ValueError, TypeError):
        return None
    if not isinstance(state, dict) or not isinstance(state.get('fetchResults'), list):
        return None
    for entry in state['fetchResults']:
        if not isinstance(entry, dict) or entry.get('name') != 'get-pressrelease-data':
            continue
        response = entry.get('response', {})
        if not isinstance(response, dict) or response.get('status') != 'fulfilled':
            continue
        value = response.get('value', {})
        article = value.get('article') if isinstance(value, dict) else None
        if not isinstance(article, dict):
            continue
        # Only this release is eligible, never recommended articles or widget text.
        if article.get('path') != urlsplit(url).path.rstrip('/').rsplit('/', 1)[-1]:
            raise ValueError('Alfa HTML contains a different release')
        if not isinstance(article.get('publicationType'), dict) or article['publicationType'].get('code') != 'release':
            raise ValueError('Alfa HTML is not a press release')
        return article
    return None


def _description_text(value):
    if not isinstance(value, str):
        return ''
    # description is the release's Markdown; footers are separate fields.
    value = re.sub(r'!\[[^\]]*\]\([^\n)]*\)', '', value)
    value = re.sub(r'\[([^\]]+)\]\([^\n)]*\)', r'\1', value)
    value = re.sub(r'(\*\*|__)(.+?)\1', r'\2', value, flags=re.S)
    value = re.sub(r'(?<!\w)(\*|_)([^\n]+?)\1(?!\w)', r'\2', value)
    value = re.sub(r'^\s{0,3}#{1,6}\s+', '', value, flags=re.M)
    return clean_text(value)


def extract_alfa(raw, candidate):
    if is_challenge(raw) or is_denied(raw):
        raise ValueError("Access check instead of Alfa release")
    soup = BeautifulSoup(raw, "html.parser")
    url = alfa_url(candidate.url, candidate.url)
    canonical = soup.select_one('link[rel="canonical"][href]')
    if canonical and alfa_url(canonical['href'], url) != url:
        raise ValueError('Alfa canonical URL does not match the requested release')
    article = _state_article(soup, url)
    if article is not None:
        title = clean_text(article.get('title', '')) if isinstance(article.get('title'), str) else ''
        published = parse_alfa_date(article.get('publishedDate'))
        text = _description_text(article.get('description'))
        if title and published and len(text) >= 40:
            return Candidate(url, title, published, ['Пресс-релизы'], 'Альфа-Банк', text)
    heading = soup.select_one('h1, [itemprop="headline"]')
    title_meta = soup.select_one('meta[property="og:title"][content]')
    title = heading.get_text(' ', strip=True) if heading else (title_meta['content'] if title_meta else '')
    if not title:
        raise ValueError("Alfa release has no heading")
    published = None
    for node in soup.select('meta[property="article:published_time"], meta[itemprop="datePublished"], '
                            'header time[datetime], header [itemprop="datePublished"]'):
        published = parse_alfa_date(node.get('content') or node.get('datetime') or node.get_text(' ', strip=True))
        if published:
            break
    if published is None:
        for header in soup.select('header'):
            if title not in header.get_text(' ', strip=True):
                continue
            published = parse_alfa_date(header.get_text(' ', strip=True))
            if published:
                break
    if published is None and heading:
        # Semantic article headers may use time as a sibling of h1.
        parent = heading.parent
        if parent and parent.name not in {'body', 'html', '[document]'}:
            for time in parent.select('time[datetime], [itemprop="datePublished"]'):
                published = parse_alfa_date(time.get('datetime') or time.get('content') or time.get_text(' ', strip=True))
                if published:
                    break
    root = soup.select_one('[itemprop="articleBody"], .news-detail__content, .news-detail__text, '
                           '.news__text, .news-content, .article-content, [data-testid="news-content"], '
                           '.cfCfF8:not(.afCfF8)')
    if root is None and heading:
        root = heading.find_parent('article')
    if root is None:
        raise ValueError("Alfa release has no recognized article body")
    body = BeautifulSoup(str(root), 'html.parser')
    _trim_related(body)
    for node in list(body.select('h1, [itemprop="headline"], time, [itemprop="datePublished"]')):
        node.decompose()
    lines = clean_text(str(body)).splitlines()
    text = '\n'.join(line for line in lines if line != title and line != 'Пресс-релизы').strip()
    return Candidate(url, title, published, ["Пресс-релизы"], "Альфа-Банк", text)
