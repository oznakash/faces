"""URL -> image manifest. Tech Spec §3.

Generic by design: Faces must work on any page a user can open in a browser,
so ingestion behaves like a browser — load the one page the user pasted,
scroll it, and read the images it displays. No platform APIs, no keys.

Two rules keep it honest:
  * Every adapter says whether it KNOWS the true total. Silent truncation is
    the most dangerous failure mode in this system; an adapter that can't
    verify completeness must say so rather than look complete.
  * Grids lazy-load thumbnails far too small for face embedding (600px wide
    crowd shots yield 12px faces). So harvested URLs are *upgraded* to a
    larger variant: by a pure URL-rewrite rule when the host's pattern is
    known, else by the linked photo page's og:image, else left as-is.
"""
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

log = logging.getLogger("faces.adapters")

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# SmugMug size ladder, smallest -> largest. URLs look like .../{SIZE}/NAME-{SIZE}.jpg
SM_SIZES = ["Ti", "Th", "S", "M", "L", "XL", "X2", "X3", "X4", "X5", "O"]
SM_KEY = re.compile(r"/(i-[A-Za-z0-9]{6,9})/")
SM_SIZE = re.compile(r"/(Ti|Th|S|M|L|XL|X2|X3|X4|X5|O)/[^/]+-(?:Ti|Th|S|M|L|XL|X2|X3|X4|X5|O)\.[a-z]{3,4}(?:$|\?)")
IMG_EXT = re.compile(r"\.(jpe?g|png|webp)(?:$|\?)", re.I)

# ---- URL upgrade rules: pure rewrites, no API. Add a host pattern per CDN.
SM_UPGRADE = re.compile(r"/(Ti|Th|S|M|L|XL|X2)/([^/]+)-(?:Ti|Th|S|M|L|XL|X2)\.([a-z]{3,4})(?=$|\?)")


def upgrade(url: str) -> str | None:
    """Return a larger-variant URL for a known host pattern, else None."""
    if "smugmug.com/" in url and SM_UPGRADE.search(url):
        # X3 = 1600px long edge; the path hash is not size-bound (verified).
        return SM_UPGRADE.sub(lambda m: f"/X3/{m.group(2)}-X3.{m.group(3)}", url)
    return None


@dataclass
class Manifest:
    adapter: str
    title: str | None
    images: list            # [{"source_url","page_url","key"}]
    expected_count: int | None = None
    completeness_known: bool = False
    notes: list = field(default_factory=list)


def canonical(url: str) -> str:
    p = urlparse(url.strip())
    if p.scheme not in ("http", "https") or not p.netloc:
        raise ValueError("INVALID_URL")
    path = p.path.rstrip("/") or "/"
    return f"{p.scheme}://{p.netloc.lower()}{path}"


def _sm_size_rank(url: str) -> int:
    m = SM_SIZE.search(url)
    return SM_SIZES.index(m.group(1)) if m else -1


def _dedupe_keep_largest(cands: list[dict]) -> list[dict]:
    """Collapse size variants of the same photo. Keyed by SmugMug image key when
    present, else by URL. Always keeps the largest variant we actually saw —
    we cannot upgrade a URL ourselves because the hash in the path is per-size."""
    best: dict[str, dict] = {}
    for c in cands:
        m = SM_KEY.search(c["source_url"])
        key = m.group(1) if m else c["source_url"]
        c["key"] = key
        cur = best.get(key)
        if cur is None or _sm_size_rank(c["source_url"]) > _sm_size_rank(cur["source_url"]):
            best[key] = c
    return list(best.values())


HARVEST_JS = """
() => {
  const out = [];
  const pick = (el) => {
    let best = el.currentSrc || el.src || el.getAttribute('data-src') || '';
    let bestW = 0;
    const ss = el.getAttribute('srcset') || el.getAttribute('data-srcset') || '';
    for (const part of ss.split(',')) {
      const [u, w] = part.trim().split(/\\s+/);
      const width = parseInt((w||'0').replace('w',''), 10) || 0;
      if (u && width > bestW) { best = u; bestW = width; }
    }
    return best;
  };
  for (const img of document.querySelectorAll('img')) {
    const src = pick(img);
    if (!src || src.startsWith('data:')) continue;
    const a = img.closest('a');
    out.push({ src, href: a ? a.href : null,
               w: img.naturalWidth || 0, h: img.naturalHeight || 0 });
  }
  // background-image tiles (some gallery themes)
  for (const el of document.querySelectorAll('[style*="background-image"]')) {
    const m = /url\\(["']?([^"')]+)["']?\\)/.exec(el.style.backgroundImage || '');
    if (m) { const a = el.closest('a'); out.push({ src: m[1], href: a ? a.href : null, w: 0, h: 0 }); }
  }
  return out;
}
"""


def headless(url: str, max_rounds: int = 400, progress=None) -> Manifest:
    """Scroll a JS-rendered gallery to the bottom and harvest what it lazy-loads."""
    from playwright.sync_api import sync_playwright

    notes = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1400, "height": 2200})
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(1500)
        html = page.content()
        title = page.title()

        expected = None
        m = re.search(r'"ImageCount":(\d+)', html)
        if m:
            expected = int(m.group(1))
            notes.append(f"page reports ImageCount={expected}")

        seen: dict[str, dict] = {}
        stable = 0
        last_h = 0
        for rnd in range(max_rounds):
            for c in page.evaluate(HARVEST_JS):
                if IMG_EXT.search(c["src"]) and c["src"] not in seen:
                    seen[c["src"]] = c
            if progress:
                progress(len(_dedupe_keep_largest([{"source_url": s, "page_url": None} for s in seen])))
            page.evaluate("window.scrollBy(0, document.documentElement.clientHeight * 0.9)")
            page.wait_for_timeout(700)
            h = page.evaluate("document.documentElement.scrollHeight")
            at_bottom = page.evaluate(
                "window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4")
            stable = stable + 1 if (h == last_h and at_bottom) else 0
            last_h = h
            if stable >= 4:
                break
        # one last sweep after settling
        page.wait_for_timeout(1500)
        for c in page.evaluate(HARVEST_JS):
            if IMG_EXT.search(c["src"]) and c["src"] not in seen:
                seen[c["src"]] = c
        browser.close()

    base = canonical(url)
    cands = []
    for src, c in seen.items():
        # skip obvious chrome: tiny icons, avatars, logos
        if any(t in src.lower() for t in ("/avatar", "logo", "favicon", "sprite", "/icons/")):
            continue
        key = SM_KEY.search(src)
        page_url = c.get("href") or (f"{base}/{key.group(1)}/A" if key else url)
        cands.append({"source_url": src, "page_url": page_url})
    images = _dedupe_keep_largest(cands)

    # Report what sizes the grid actually served — this decides detection quality.
    ranks = [_sm_size_rank(i["source_url"]) for i in images]
    if ranks and max(ranks) >= 0:
        from collections import Counter
        dist = Counter(SM_SIZES[r] if r >= 0 else "?" for r in ranks)
        notes.append("grid served: " + ", ".join(f"{k}={v}" for k, v in dist.most_common()))

    upgraded = 0
    for i in images:
        i["fallback_url"] = i["source_url"]
        up = upgrade(i["source_url"])
        if up:
            i["source_url"] = up
            upgraded += 1
    if upgraded:
        notes.append(f"upgraded {upgraded} URLs to a 1600px variant")

    known = expected is not None and len(images) == expected
    if expected is not None and not known:
        notes.append(f"harvested {len(images)} of reported {expected} — completeness NOT verified")
    return Manifest("headless", title, images, expected, known, notes)


def static_html(url: str) -> Manifest:
    """Plain <img>/<a href=*.jpg> harvesting for server-rendered pages. No total known."""
    import httpx
    r = httpx.get(url, headers={"User-Agent": UA}, follow_redirects=True, timeout=25)
    r.raise_for_status()
    html = r.text
    title = (re.search(r"<title>([^<]*)</title>", html, re.I) or [None, None])[1]
    urls = set()
    for m in re.finditer(r'(?:src|href|data-src)=["\']([^"\']+)["\']', html, re.I):
        u = urljoin(url, m.group(1))
        if IMG_EXT.search(u):
            urls.add(u)
    images = _dedupe_keep_largest([{"source_url": u, "page_url": url} for u in sorted(urls)])
    for i in images:
        i["fallback_url"] = i["source_url"]
        i["source_url"] = upgrade(i["source_url"]) or i["source_url"]
    return Manifest("html", title, images, None, False, ["static HTML; total unknown"])


SM_ANY_SIZE = re.compile(r"/(Ti|Th|S|M|L|XL|X2|X3|X4|X5|O)/([^/]+)-(?:Ti|Th|S|M|L|XL|X2|X3|X4|X5|O)\.([a-z]{3,4})(?=$|\?)")


def thumb_url(source_url: str, fallback_url: str | None) -> str:
    """A grid-sized image for tiles. The indexed source is 1600 px (~500 KB) —
    far too heavy for a phone grid. Known hosts get a rewrite to ~800 px; others
    fall back to the thumbnail the gallery grid itself served."""
    if "smugmug.com/" in source_url and SM_ANY_SIZE.search(source_url):
        return SM_ANY_SIZE.sub(lambda m: f"/L/{m.group(2)}-L.{m.group(3)}", source_url)
    return fallback_url or source_url


OG_IMAGE = re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.I)


def og_image(client, page_url: str) -> str | None:
    """Generic upgrade for unknown hosts: the photo's own page usually advertises a large og:image."""
    try:
        r = client.get(page_url, timeout=15, headers={"Accept": "text/html"})
        if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
            return None
        m = OG_IMAGE.search(r.text[:400_000])
        return m.group(1) if m else None
    except Exception:                               # noqa: BLE001
        return None


def resolve(url: str, progress=None) -> Manifest:
    """Headless first (it is what a browser does), static HTML as the fallback."""
    try:
        m = headless(url, progress=progress)
        if m.images:
            return m
        log.warning("headless found nothing, trying static html")
    except Exception as e:                      # noqa: BLE001
        log.warning("headless failed: %s", e)
    return static_html(url)
