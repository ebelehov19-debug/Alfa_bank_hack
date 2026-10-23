FEED_ROOT = '[class*="FeedPageContainer_wrapper"]'
FEED_CARDS = '[class*="CardsGrid_cards"] a[href]'
FEED_LOADER = '[class*="InfiniteLoading_container"]'

SCROLL_FEED = r'''({rootSelector, cardSelector, loaderSelector, reset}) => {
    const root = document.querySelector(rootSelector) || document;
    const loader = root.querySelector(loaderSelector);
    const cards = [...root.querySelectorAll(cardSelector)].filter(a =>
        /^\/ekonomika\/\d+\/?$/.test(new URL(a.href, location.href).pathname));
    const target = loader && loader.getClientRects().length ? loader : cards.at(-1);
    if (!target) return false;
    if (reset) {
        // Re-enter the observer's viewport after a timed-out attempt. This also
        // handles a feed inside an independently scrollable container.
        let parent = target.parentElement;
        while (parent && parent !== document.body) {
            const overflow = getComputedStyle(parent).overflowY;
            if (/(auto|scroll)/.test(overflow) && parent.scrollHeight > parent.clientHeight) {
                parent.scrollBy(0, -Math.max(parent.clientHeight, 100));
                return true;
            }
            parent = parent.parentElement;
        }
        window.scrollBy(0, -Math.max(innerHeight, 100));
    } else {
        // body.scrollHeight is deliberately NOT used: the real feed's section
        // has min-height:5000vh, far beyond its InfiniteLoading sentinel.
        target.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
    }
    return true;
}'''

FEED_GREW = r'''({rootSelector, cardSelector, known}) => {
    const root = document.querySelector(rootSelector) || document;
    const id = value => {
        try {
            const u = new URL(value, location.href);
            if (!['tass.ru', 'www.tass.ru'].includes(u.hostname)) return null;
            const m = u.pathname.match(/^\/ekonomika\/(\d+)\/?$/);
            return m ? m[1] : null;
        } catch { return null; }
    };
    const seen = new Set(known.map(id).filter(Boolean));
    // Compare IDs, not element count: duplicate cards do not signal progress,
    // and a virtualized feed may replace cards without increasing their count.
    return [...root.querySelectorAll(cardSelector)].some(a => {
        const key = id(a.href);
        return key !== null && !seen.has(key);
    });
}'''
