"""Recognize access-check documents before treating them as article HTML."""
import re


def is_challenge(raw: str) -> bool:
    lower = raw.lower()
    return any(marker in lower for marker in (
        '<js-challenge-loader', 'id="id_captcha_frame_div"',
        'function get_cookie_spsn()', 'function get_cookie_spid()',
    )) or bool(re.search(r'<title[^>]*>\s*(?:just a moment|checking your browser)', lower))


def is_denied(raw: str) -> bool:
    return bool(re.search(
        r'<(?:title|h1)[^>]*>\s*(?:forbidden|access denied|доступ ограничен)\s*</',
        raw, re.I))


def has_source_content(raw: str, url: str) -> bool:
    """Intermediate check/CAPTCHA pages are not a successful browser load."""
    from urllib.parse import urlsplit
    p = urlsplit(url)
    lower = raw.lower()
    if is_challenge(raw) or is_denied(raw):
        return False
    if p.hostname in {'tass.ru', 'www.tass.ru'}:
        if re.search(r'/ekonomika/\d+', p.path):
            return bool(re.search(r'<article\b|itemprop=[\"\x27]articlebody|newsarticle|class=[\"\x27][^\"\x27]*(?:news-content|text-block)', lower))
        return bool(re.search(r'<a\b[^>]+href=[\"\x27][^\"\x27]*/ekonomika/\d+', lower))
    if p.hostname in {'alfabank.ru', 'www.alfabank.ru'}:
        return _has_alfa_content(raw, url)
    return True


def _has_alfa_content(raw: str, url: str) -> bool:
    """Wait for release data or its DOM, rather than accepting an empty app shell."""
    import json
    from urllib.parse import urljoin, urlsplit
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(raw, 'html.parser')
    path = urlsplit(url).path
    if re.fullmatch(r'/news/t/release/(?:page/\d+/)?', path):
        for anchor in soup.select('a[href]'):
            target = urlsplit(urljoin(url, anchor['href']))
            if (target.scheme in {'http', 'https'}
                    and target.hostname in {'alfabank.ru', 'www.alfabank.ru'}
                    and re.fullmatch(r'/news/t/release/(?!page/?$)[^/]+/?', target.path)
                    and anchor.get_text(' ', strip=True)):
                return True
        return False
    if not re.fullmatch(r'/news/t/release/(?!page/?$)[^/]+/?', path):
        return False

    for script in soup.select('script#app_state'):
        try:
            state = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        if not isinstance(state, dict) or not isinstance(state.get('fetchResults'), list):
            continue
        for result in state['fetchResults']:
            if not isinstance(result, dict) or result.get('name') != 'get-pressrelease-data':
                continue
            response = result.get('response')
            if not isinstance(response, dict) or response.get('status') != 'fulfilled':
                continue
            value = response.get('value')
            article = value.get('article') if isinstance(value, dict) else None
            if (isinstance(article, dict)
                    and all(isinstance(article.get(key), str) and article[key].strip()
                            for key in ('title', 'publishedDate', 'description'))
                    and len(article['description'].strip()) >= 40):
                return True

    heading = soup.select_one('h1, [itemprop="headline"], main > header, [role="main"] > header')
    bodies = soup.select('[itemprop="articleBody"], article, .news-detail__content, '
                         '.news-detail__text, .news__text, .news-content, .article-content, '
                         '[data-testid="news-content"], .cfCfF8')
    return bool(heading and heading.get_text(' ', strip=True)
                and any(len(body.get_text(' ', strip=True)) >= 40 for body in bodies))


def browser_request_allowed(url: str, resource_type: str) -> bool:
    from urllib.parse import urlsplit
    from .models import HOSTS
    host = urlsplit(url).hostname or ''
    official = (host in HOSTS or host.endswith(('.tass.ru', '.moex.com', '.alfabank.ru'))
                or host == 'alfabank.servicecdn.ru')
    access_service = host == 'servicepipe.tech' or host.endswith('.servicepipe.tech')
    return resource_type != 'media' and (official or access_service)
