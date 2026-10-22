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
    if p.hostname in {'e-disclosure.ru', 'www.e-disclosure.ru'}:
        if '/portal/event.aspx' in p.path.lower():
            return 'общие сведения' in lower or ('newsarticle' in lower and 'articlebody' in lower)
        return ('выбрать дату публикации' in lower or
                bool(re.search(r'<a\b[^>]+href=[\"\x27][^\"\x27]*event\.aspx\?', lower)))
    return True


def browser_request_allowed(url: str, resource_type: str) -> bool:
    from urllib.parse import urlsplit
    from .models import HOSTS
    host = urlsplit(url).hostname or ''
    official = host in HOSTS or host.endswith(('.tass.ru', '.moex.com', '.e-disclosure.ru'))
    access_service = host == 'servicepipe.tech' or host.endswith('.servicepipe.tech')
    return resource_type != 'media' and (official or access_service)
