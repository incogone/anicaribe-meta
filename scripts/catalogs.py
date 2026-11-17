"""Catalogues read less often than AniList: MyAnimeList (through Jikan), MangaUpdates, and the
title lists of the manga servers the AniCaribe app reads from.

Each reader returns {id: row}. These change slowly and cost many requests, so build.py keeps the
last copy of each and reads a catalogue again only once its copy is a week old.
"""
from __future__ import annotations

import re
import time
import urllib.parse
from datetime import datetime, timezone
from html.parser import HTMLParser

from net import Gone, fetch, log
from text import clean_description, slug_title, unique

SHORT_DESCRIPTION = 400


# ---------------------------------------------------------------- MangaUpdates

MU_API = "https://api.mangaupdates.com/v1/series/search"
MU_TYPES = {"Manga": "manga", "Manhwa": "manhwa", "Manhua": "manhua", "OEL": "manga"}
MU_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
MU_CAP = 10_000  # the search stops listing after this many results


def _mu_pages(filters, items, limit):
    """Reads every page of one MangaUpdates search. Returns how many results it said it had."""
    page, total = 1, 0
    while True:
        data, _ = fetch(MU_API, body={**filters, "perpage": 100, "page": page})
        total = data.get("total_hits") or 0
        results = data.get("results") or []
        for result in results:
            record = result.get("record") or {}
            match = re.search(r"/series/([0-9a-z]+)", record.get("url") or "")
            if not match or not record.get("title"):
                continue
            image = ((record.get("image") or {}).get("url") or {}).get("original")
            year = str(record.get("year") or "")[:4]
            items[match.group(1)] = [
                record["title"],
                MU_TYPES.get(record.get("type"), "manga"),
                int(year) if year.isdigit() else None,
                record.get("bayesian_rating"),
                record.get("rating_votes") or 0,
                image,
                [g["genre"] for g in record.get("genres") or [] if g.get("genre")],
                clean_description(record.get("description"), SHORT_DESCRIPTION),
            ]
        time.sleep(0.4)
        if not results or page * 100 >= min(total, MU_CAP) or (limit and len(items) >= limit):
            return total
        page += 1


def fetch_mangaupdates(limit=None):
    """Every manga, manhwa, manhua and OEL series on MangaUpdates. Its search lists at most
    10,000 results, so it is read by type and first letter, and by year where that is still too many."""
    items = {}
    this_year = datetime.now(timezone.utc).year
    for kind in MU_TYPES:
        for letter in MU_LETTERS:
            filters = {"type": [kind], "letter": letter}
            if _mu_pages(filters, items, limit) >= MU_CAP:
                for year in range(1930, this_year + 2):
                    _mu_pages({**filters, "year": str(year)}, items, limit)
            if limit and len(items) >= limit:
                return items
        log(f"  MangaUpdates: {kind} done, {len(items)} series so far")
    return items


# ---------------------------------------------------------------- MyAnimeList (Jikan)

JIKAN = "https://api.jikan.moe/v4/manga"
JIKAN_KINDS = {"Manga": "manga", "Manhwa": "manhwa", "Manhua": "manhua", "One-shot": "manga",
               "Doujinshi": "manga", "Light Novel": "novel", "Novel": "novel"}
JIKAN_STATUS = {"Publishing": "RELEASING", "Finished": "FINISHED", "On Hiatus": "HIATUS",
                "Discontinued": "CANCELLED", "Not yet published": "NOT_YET_RELEASED"}


def fetch_jikan(limit=None):
    """Every manga on MyAnimeList, through Jikan's public mirror of it (no key needed)."""
    items, page = {}, 1
    while True:
        data, _ = fetch(f"{JIKAN}?page={page}&limit=25&order_by=mal_id&sort=asc")
        for manga in data.get("data") or []:
            genres = [g["name"] for key in ("genres", "explicit_genres", "themes", "demographics")
                      for g in manga.get(key) or []]
            published = ((manga.get("published") or {}).get("prop") or {}).get("from") or {}
            items[str(manga["mal_id"])] = [
                unique([t.get("title") for t in manga.get("titles") or []] + [manga.get("title")]),
                JIKAN_KINDS.get(manga.get("type"), "manga"),
                published.get("year"),
                JIKAN_STATUS.get(manga.get("status")),
                manga.get("chapters"),
                manga.get("score"),
                manga.get("members") or 0,
                ((manga.get("images") or {}).get("jpg") or {}).get("large_image_url"),
                genres,
                clean_description(manga.get("synopsis"), SHORT_DESCRIPTION),
            ]
        if page % 200 == 0:
            log(f"  MyAnimeList: page {page}, {len(items)} titles so far")
        if not (data.get("pagination") or {}).get("has_next_page") or (limit and len(items) >= limit):
            return items
        page += 1
        time.sleep(1.1)  # Jikan allows 60 requests a minute


# ---------------------------------------------------------------- manga servers

class _Links(HTMLParser):
    """Every link on a page with its title attribute and its text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self._open = [], []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            attrs = dict(attrs)
            self._open.append([attrs.get("href") or "", attrs.get("title") or "", []])
        elif tag == "img" and self._open:
            alt = dict(attrs).get("alt")
            if alt and not self._open[-1][1]:
                self._open[-1][1] = alt

    def handle_data(self, data):
        for link in self._open:
            link[2].append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._open:
            href, title, text = self._open.pop()
            self.links.append((href.strip(), title.strip(), " ".join("".join(text).split())))


def _links(page, base):
    parser = _Links()
    parser.feed(page)
    return [(urllib.parse.urljoin(base, href), title, text) for href, title, text in parser.links if href]


CRAWL_BUDGET = 150 * 60  # seconds one server's title list may take


def _crawl(name, address, pattern, *, make_id=None, headers=None, limit=None, delay=0.6, first=1, max_pages=6000):
    """Reads a server's title list page by page until a page brings nothing new."""
    items = {}
    regex = re.compile(pattern)
    stale = 0
    started = time.monotonic()
    for page in range(first, max_pages + 1):
        if time.monotonic() - started > CRAWL_BUDGET:
            # A slow server must not use up the whole job: keep what was read, read the rest next time.
            log(f"  {name}: stopping at page {page} after {CRAWL_BUDGET // 60} minutes")
            break
        url = address(page)
        try:
            text, _ = fetch(url, kind="text", headers=headers, browser=True)
        except Gone:
            break
        except Exception as error:
            if len(items) < 1000:
                raise
            log(f"  {name}: stopping at page {page} ({type(error).__name__}: {error})")
            break
        fresh = 0
        for href, title, label in _links(text, url):
            match = regex.match(href)
            if not match:
                continue
            item_id = make_id(match) if make_id else match.group(0)
            if item_id not in items:
                fresh += 1
                items[item_id] = []
            items[item_id].extend(n for n in (title, label) if _is_title(n))
        if page % 100 == 0:
            log(f"  {name}: page {page}, {len(items)} titles so far")
        # Lists ordered by latest update shift while they are read, so one page of titles already
        # seen does not mean the end; three in a row does.
        stale = 0 if fresh else stale + 1
        if stale >= 3 or (limit and len(items) >= limit):
            break
        time.sleep(delay)
    # A card links its title several times: on the cover, the name, a "HOT" badge. The longest
    # real name wins.
    named = {}
    for item_id, names in items.items():
        names = sorted(unique(names), key=len, reverse=True)
        if names:
            named[item_id] = names[:4]
    return named


_JUNK = re.compile(r"(?i)^(hot|new|end|completed|ongoing|read (more|now)|continue reading|"
                   r"(chapter|ch\.?|episode|ep\.?|vol\.?)\s*[\d.]+.*|[\d.]+|\W*)$")


def _is_title(text):
    return bool(text) and len(text) > 1 and not _JUNK.match(text.strip())


def _sitemap(urls, pattern, make_title, limit=None):
    items = {}
    regex = re.compile(pattern)
    for url in urls:
        text, _ = fetch(url, kind="text", browser=True)
        for match in regex.finditer(text):
            items.setdefault(match.group(1), [make_title(match)])
        if limit and len(items) >= limit:
            break
        time.sleep(0.5)
    return items


def weebcentral(limit=None):
    return _sitemap(
        ["https://weebcentral.com/sitemap.xml"],
        r"https://weebcentral\.com/series/([0-9A-Z]{20,})/([^<\s\"]+)",
        lambda m: slug_title(urllib.parse.unquote(m.group(2))), limit)


def webtoonclub(limit=None):
    index, _ = fetch("https://webtoonclub.com/sitemap.xml", kind="text", browser=True)
    maps = re.findall(r"<loc>\s*(https://webtoonclub\.com/sitemaps/[^<\s]+)\s*</loc>", index)
    return _sitemap(maps, r"https://webtoonclub\.com/webtoon/([^/<\s\"]+)(?=[<\s\"])",
                    lambda m: slug_title(m.group(1)), limit)


def mangapill(limit=None):
    index, _ = fetch("https://mangapill.com/sitemap.xml", kind="text", browser=True)
    maps = re.findall(r"<loc>\s*(https://mangapill\.com/static/sitemaps/[^<\s]+)\s*</loc>", index)
    return _sitemap(maps, r"https://mangapill\.com(/manga/\d+/([^/<\s\"]+))(?=[<\s\"])",
                    lambda m: slug_title(m.group(2)), limit)


def mangafox(limit=None):
    return _crawl("MangaFox", lambda p: "https://fanfox.net/directory/" if p == 1 else f"https://fanfox.net/directory/{p}.html",
                  r"^https://fanfox\.net(/manga/[^/?#]+/)$", make_id=lambda m: m.group(1),
                  headers={"Cookie": "isAdult=1"}, limit=limit)


def madara(name, base, listing, headers=None):
    host = re.escape(urllib.parse.urlparse(base).netloc)

    def read(limit=None):
        return _crawl(name, lambda p: f"{base}/{listing}/page/{p}/?m_orderby=alphabet",
                      rf"^https://{host}/(?:manga|webtoon|serie|series|manhua|manhwa)/[^/?#]+/?$",
                      make_id=lambda m: m.group(0).rstrip("/") + "/", headers=headers, limit=limit)
    return read


def mangago(limit=None):
    return _crawl("Mangago", lambda p: f"https://www.mangago.me/list/directory/all/{p}/",
                  r"^https://www\.mangago\.me/read-manga/[^/?#]+/$", limit=limit)


def mangabox(name, host, listing):
    def read(limit=None):
        return _crawl(name, lambda p: f"https://{host}/{listing}?page={p}",
                      rf"^https://{re.escape(host)}/manga/[^/?#]+$", limit=limit, delay=1.2)
    return read


def baetoon(limit=None):
    items, page = {}, 1
    while True:
        data, _ = fetch(f"https://baetoon.com/api/titles/search?text=&page={page}", browser=True)
        for title in data.get("data") or []:
            if title.get("id") and title.get("title"):
                items[title["id"]] = [title["title"]]
        pages = (data.get("pagination") or {}).get("totalPages") or 0
        if page >= pages or not data.get("data") or (limit and len(items) >= limit):
            return items
        page += 1
        time.sleep(0.5)


def mangaball(limit=None):
    items, page = {}, 1
    while True:
        data, _ = fetch("https://mangaball.com/api/v1/title/search", browser=True,
                        body={"keyword": "", "page": page, "limit": 100, "adult_mode": "all"})
        for title in data.get("data") or []:
            title_id = title.get("id") or title.get("_id")
            if title_id and title.get("name"):
                # Only names in Latin letters help match a title against other sites.
                alternates = [a for a in title.get("alternateName") or []
                              if isinstance(a, str) and re.fullmatch(r"[\x20-\x7EÀ-ɏ]+", a)]
                items[title_id] = unique([title["name"]] + alternates)[:8]
        pages = (data.get("pagination") or {}).get("total_pages") or 0
        if page % 200 == 0:
            log(f"  MangaBall: page {page} of {pages}, {len(items)} titles so far")
        if page >= pages or not data.get("data") or (limit and len(items) >= limit):
            return items
        page += 1
        time.sleep(0.5)


# The app's server ids, in the app's own order, with how each one's titles are listed.
SERVERS = {
    "weebcentral": weebcentral,
    "mangapill": mangapill,
    "mangafox": mangafox,
    "mangaread": madara("MangaRead", "https://www.mangaread.org", "manga"),
    "manhuaplus": madara("ManhuaPlus", "https://manhuaplus.com", "manga"),
    "webtoonclub": webtoonclub,
    "baetoon": baetoon,
    "mangaball": mangaball,
    "mangago": mangago,
    "mangakakalot": mangabox("Mangakakalot", "www.kakamanga.com", "manga-list/latest-manga"),
    "manganato": mangabox("Manganato", "www.manganato.gg", "genre/all"),
    "toonily": madara("Toonily", "https://toonily.com", "webtoons", headers={"Cookie": "toonily-mature=1"}),
}
