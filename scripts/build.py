"""Builds the AniCaribe metadata index.

Anime come from AniList. Manga, manhwa and manhua come from AniList, MangaDex, MyAnimeList (through
Jikan), MangaUpdates and the title lists of the manga servers the app reads from. Records of the
same work are merged into one entry, and the result is written as small gzipped JSON files that a
phone can fetch from a CDN.

    python scripts/build.py --out out --cache cache [--previous latest.json] [--limit N]

AniList and MangaDex are read in full on every run. The other catalogues cost many more requests
and change slowly. A copy of each is kept in --cache, and a catalogue is read again only when its
copy is a week old. If a catalogue cannot be read, its last copy is used. AniList and MangaDex must
both be read in full, or nothing is written.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
import time
import traceback
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import catalogs  # noqa: E402
from net import fetch, log  # noqa: E402
from text import clean_description, title_key, unique  # noqa: E402

ANILIST = "https://graphql.anilist.co"
MANGADEX = "https://api.mangadex.org"

ANILIST_FIELDS = (
    "id idMal type format status countryOfOrigin isAdult title{romaji english native} synonyms "
    "description(asHtml:false) startDate{year month day} endDate{year month day} season seasonYear "
    "episodes duration chapters volumes genres tags{name rank isMediaSpoiler} averageScore meanScore "
    "popularity favourites coverImage{extraLarge large color} bannerImage source "
    "studios(isMain:true){nodes{name}} relations{edges{relationType node{id type}}} "
    "nextAiringEpisode{airingAt episode} updatedAt"
)
IDS_PER_PAGE = 50
PAGES_PER_REQUEST = 8  # 400 ids a request keeps a full pass to a few hundred requests
DESCRIPTION_LIMIT = 800
SHORT_DESCRIPTION = 400
SEARCH_PART_ROWS = 50000        # about 3 MB compressed per part
FILE_LIMIT = 19e6               # jsDelivr will not serve a GitHub file over 20 MB
REFRESH_AFTER = timedelta(days=6, hours=12)
ROMANISED = {"en", "ja-ro", "ko-ro", "zh-ro", "zh-hk-ro"}
KIND_BY_COUNTRY = {"JP": "manga", "KR": "manhwa", "CN": "manhua", "TW": "manhua", "HK": "manhua"}
KIND_BY_LANGUAGE = {"ja": "manga", "ko": "manhwa", "zh": "manhua", "zh-hk": "manhua"}
ADULT_GENRES = {"hentai", "erotica", "adult", "smut"}


def compact(entry):
    return {k: v for k, v in entry.items() if v not in (None, [], {}, "")}


# ---------------------------------------------------------------- AniList

def date_text(value):
    if not value or not value.get("year"):
        return None
    parts = [f"{value['year']:04d}"]
    if value.get("month"):
        parts.append(f"{value['month']:02d}")
        if value.get("day"):
            parts.append(f"{value['day']:02d}")
    return "-".join(parts)


def anilist_entry(media):
    cover = media.get("coverImage") or {}
    kind = None
    if media["type"] == "MANGA":
        kind = "novel" if media.get("format") == "NOVEL" else KIND_BY_COUNTRY.get(media.get("countryOfOrigin"), "manga")
    return compact({
        "id": media["id"],
        "mal": media.get("idMal"),
        "type": media["type"].lower(),
        "kind": kind,
        "format": media.get("format"),
        "status": media.get("status"),
        "country": media.get("countryOfOrigin"),
        "adult": True if media.get("isAdult") else None,
        "title": compact(media.get("title") or {}),
        "synonyms": [s for s in media.get("synonyms") or [] if s],
        "description": clean_description(media.get("description"), DESCRIPTION_LIMIT),
        "start": date_text(media.get("startDate")),
        "end": date_text(media.get("endDate")),
        "season": media.get("season"),
        "seasonYear": media.get("seasonYear"),
        "episodes": media.get("episodes"),
        "duration": media.get("duration"),
        "chapters": media.get("chapters"),
        "volumes": media.get("volumes"),
        "genres": media.get("genres") or [],
        "tags": [t["name"] for t in media.get("tags") or [] if not t.get("isMediaSpoiler") and (t.get("rank") or 0) >= 60][:10],
        "score": media.get("averageScore"),
        "meanScore": media.get("meanScore"),
        "popularity": media.get("popularity"),
        "favourites": media.get("favourites"),
        "cover": cover.get("extraLarge") or cover.get("large"),
        "coverColor": cover.get("color"),
        "banner": media.get("bannerImage"),
        "source": media.get("source"),
        "studios": [n["name"] for n in (media.get("studios") or {}).get("nodes") or []],
        "relations": [
            [e["node"]["id"], e["node"]["type"].lower(), e["relationType"]]
            for e in (media.get("relations") or {}).get("edges") or [] if e.get("node")
        ],
        "nextEpisode": media.get("nextAiringEpisode"),
        "updatedAt": media.get("updatedAt"),
    })


def fetch_anilist(limit=None):
    """Every AniList entry, read by id range: AniList pages only 5,000 entries deep, ids are not."""
    latest, _ = fetch(ANILIST, body={"query": "{Page(perPage:1){media(sort:ID_DESC){id}}}"})
    top = latest["data"]["Page"]["media"][0]["id"] + 2000  # room for entries added during the run
    if limit:
        top = min(top, limit)
    found = {}
    chunk = IDS_PER_PAGE * PAGES_PER_REQUEST
    log(f"AniList: reading ids 1 to {top}")
    for start in range(1, top + 1, chunk):
        parts = []
        for k in range(PAGES_PER_REQUEST):
            first = start + k * IDS_PER_PAGE
            ids = ",".join(str(i) for i in range(first, first + IDS_PER_PAGE))
            parts.append(f"p{k}:Page(perPage:{IDS_PER_PAGE}){{media(id_in:[{ids}]){{{ANILIST_FIELDS}}}}}")
        began = time.monotonic()
        data, headers = fetch(ANILIST, body={"query": "{" + " ".join(parts) + "}"})
        if data.get("errors"):
            raise RuntimeError(f"AniList error at id {start}: {data['errors']}")
        for page in data["data"].values():
            for media in page["media"]:
                found[media["id"]] = anilist_entry(media)
        if (start // chunk) % 50 == 0:
            log(f"  AniList: id {start}, {len(found)} titles so far")
        # Stay under the published rate limit (requests per minute) with room to spare.
        per_minute = int(headers.get("X-RateLimit-Limit") or 30)
        spacing = 60 / max(per_minute - 3, 1)
        remaining = int(headers.get("X-RateLimit-Remaining") or per_minute)
        time.sleep(max(spacing - (time.monotonic() - began), 0) + (5 if remaining < 3 else 0))
    log(f"AniList: {len(found)} titles")
    return found


# ---------------------------------------------------------------- MangaDex

def first_text(values, preferred=("en", "ja-ro", "ko-ro", "zh-ro")):
    if not values:
        return None
    for language in preferred:
        if values.get(language):
            return values[language]
    return next(iter(values.values()), None)


def mangadex_entry(item):
    attributes = item["attributes"]
    titles = list((attributes.get("title") or {}).items())
    for alt in attributes.get("altTitles") or []:
        titles.extend(alt.items())
    names = unique(value for language, value in titles
                   if language in ROMANISED or language in ("ja", "ko", "zh", "zh-hk"))
    cover = next((r["attributes"]["fileName"] for r in item.get("relationships", [])
                  if r["type"] == "cover_art" and r.get("attributes")), None)
    links = attributes.get("links") or {}
    language = attributes.get("originalLanguage")
    return compact({
        "id": item["id"],
        "title": first_text(attributes.get("title")) or (names[0] if names else None),
        "names": names,
        "kind": KIND_BY_LANGUAGE.get(language, "manga"),
        "status": (attributes.get("status") or "").upper() or None,
        "year": attributes.get("year"),
        "adult": True if attributes.get("contentRating") in ("erotica", "pornographic") else None,
        "description": clean_description(first_text(attributes.get("description"), ("en",)), SHORT_DESCRIPTION),
        "genres": [t["attributes"]["name"].get("en") for t in attributes.get("tags") or []
                   if t["attributes"].get("group") in ("genre", "theme") and t["attributes"]["name"].get("en")][:10],
        "cover": f"{item['id']}/{cover}" if cover else None,
        "anilist": int(links["al"]) if str(links.get("al", "")).isdigit() else None,
        "mal": int(links["mal"]) if str(links.get("mal", "")).isdigit() else None,
        "mangaupdates": links.get("mu") if re.fullmatch(r"[0-9a-z]{5,}", str(links.get("mu", ""))) else None,
    })


def fetch_mangadex(limit=None):
    """Every MangaDex title. Its list stops at 10,000 entries, so it is read in windows of
    creation date, each starting where the last one ended."""
    found = {}
    since = "2000-01-01T00:00:00"
    log("MangaDex: reading all titles")
    while True:
        last_created = None
        for offset in range(0, 10_000, 100):
            query = [
                ("limit", "100"), ("offset", str(offset)), ("order[createdAt]", "asc"),
                ("createdAtSince", since), ("includes[]", "cover_art"),
            ] + [("contentRating[]", r) for r in ("safe", "suggestive", "erotica", "pornographic")]
            # MangaDex answers a burst of requests with a 400 page for a while; that is waited out.
            data, _ = fetch(f"{MANGADEX}/manga?{urllib.parse.urlencode(query)}", retry_on=(400, 403))
            items = data.get("data") or []
            for item in items:
                found[item["id"]] = mangadex_entry(item)
                last_created = item["attributes"]["createdAt"]
            time.sleep(0.5)
            if len(items) < 100 or (limit and len(found) >= limit):
                log(f"MangaDex: {len(found)} titles")
                return found
        log(f"  MangaDex: {len(found)} titles so far")
        next_since = last_created[:19]
        if next_since <= since:  # every title in a window shares one timestamp; step past it
            next_since = (datetime.fromisoformat(since) + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S")
        since = next_since


# ---------------------------------------------------------------- the slower catalogues

def slow_catalogues():
    named = {"mangaupdates": catalogs.fetch_mangaupdates, "myanimelist": catalogs.fetch_jikan}
    named.update({f"server-{name}": read for name, read in catalogs.SERVERS.items()})
    return named


def read_cache(cache, name):
    path = os.path.join(cache, f"{name}.json.gz")
    if not os.path.exists(path):
        return None
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def cached_catalogue(cache, name, read, limit, force, skip):
    """The catalogue's last copy if it is recent enough, otherwise a fresh read of it."""
    copy = read_cache(cache, name) if cache else None
    age = time.time() - copy["fetched"] if copy else None
    if copy and (name in skip or (not force and age < REFRESH_AFTER.total_seconds())):
        log(f"{name}: using the copy from {age / 86400:.1f} days ago ({len(copy['items'])} entries)")
        return copy["items"], False
    if name in skip:
        log(f"{name}: skipped, and there is no earlier copy")
        return {}, False
    try:
        log(f"{name}: reading")
        items = read(limit)
    except Exception as error:  # a site being down must not stop the whole build
        log(f"{name}: FAILED ({type(error).__name__}: {str(error)[:200]})")
        traceback.print_exc()
        if copy:
            log(f"{name}: using the copy from {age / 86400:.1f} days ago instead")
            return copy["items"], False
        return {}, False
    if copy and len(items) < len(copy["items"]) * 0.5 and not limit:
        log(f"{name}: only {len(items)} entries against {len(copy['items'])} last time; keeping the old copy")
        return copy["items"], False
    log(f"{name}: {len(items)} entries")
    return items, True


def save_cache(cache, name, items):
    os.makedirs(cache, exist_ok=True)
    raw = json.dumps({"fetched": time.time(), "items": items}, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(cache, f"{name}.json.gz"), "wb") as handle:
        handle.write(gzip.compress(raw.encode(), 9, mtime=0))


# ---------------------------------------------------------------- merging

def year_of(entry):
    if entry.get("start"):
        return int(entry["start"][:4])
    return entry.get("seasonYear") or entry.get("year")


def names_of(entry):
    title = entry.get("title")
    names = [title.get("english"), title.get("romaji"), title.get("native")] if isinstance(title, dict) else [title]
    return unique(names + entry.get("synonyms", []) + entry.get("altNames", []) + entry.get("names", []))


class TitleIndex:
    """Finds the work a name belongs to. The first work to claim a name keeps it, so works are
    added most trusted and most popular first."""

    def __init__(self, works):
        self.works = works
        self.by_key = {}

    def add(self, work_id):
        for name in names_of(self.works[work_id]):
            key = title_key(name)
            if key:
                self.by_key.setdefault(key, work_id)

    def find(self, names, year=None):
        for name in names:
            work_id = self.by_key.get(title_key(name))
            if work_id is None:
                continue
            known = year_of(self.works[work_id])
            if year and known and abs(int(year) - int(known)) > 2:
                continue
            return work_id
        return None


def merge_manga(anilist_manga, mangadex, myanimelist, mangaupdates, servers):
    works = {}
    for entry in sorted(anilist_manga.values(), key=lambda e: -(e.get("popularity") or 0)):
        works[entry["id"]] = dict(entry)
    by_mal = {e["mal"]: e["id"] for e in anilist_manga.values() if e.get("mal")}
    by_mu = {}
    index = TitleIndex(works)
    for work_id in list(works):
        index.add(work_id)

    def attach_names(work, names):
        known = {n.casefold() for n in names_of(work)}
        more = [n for n in names if n and n.casefold() not in known]
        if more:
            work["altNames"] = unique(work.get("altNames", []) + more)[:30]

    # MangaDex: joined by the AniList or MyAnimeList id it gives, otherwise its own entry.
    for md in mangadex.values():
        target = md.get("anilist") if md.get("anilist") in works else by_mal.get(md.get("mal"))
        if target is not None:
            work = works[target]
            work.setdefault("mangadex", md["id"])
            if md.get("mangaupdates"):
                work.setdefault("mangaupdates", md["mangaupdates"])
            attach_names(work, md.get("names", []))
        else:
            target = "md:" + md["id"]
            works[target] = compact({
                "id": target, "source": "mangadex", "title": md.get("title"),
                "names": [n for n in md.get("names", []) if n != md.get("title")],
                "kind": md.get("kind"), "status": md.get("status"), "year": md.get("year"),
                "adult": md.get("adult"), "description": md.get("description"), "genres": md.get("genres"),
                "cover": "md:" + md["cover"] if md.get("cover") else None,
                "mangadex": md["id"], "mangaupdates": md.get("mangaupdates"), "mal": md.get("mal"),
            })
            if md.get("mal"):
                by_mal.setdefault(md["mal"], target)
        if md.get("mangaupdates"):
            by_mu.setdefault(md["mangaupdates"], target)
    for work_id in works:
        if isinstance(work_id, str):
            index.add(work_id)

    # MyAnimeList: joined by its id, otherwise its own entry (novels are left out).
    for mal_id, row in myanimelist.items():
        names, kind, year, status, chapters, score, members, cover, genres, description = row
        target = by_mal.get(int(mal_id))
        if target is not None:
            attach_names(works[target], names)
            continue
        if kind == "novel" or not names:
            continue
        target = "mal:" + mal_id
        works[target] = compact({
            "id": target, "source": "myanimelist", "title": names[0], "names": names[1:], "kind": kind,
            "status": status, "year": year, "chapters": chapters, "score": score, "members": members,
            "adult": True if ADULT_GENRES & {g.casefold() for g in genres} else None,
            "description": description, "genres": genres,
            "cover": "mal:" + cover.split("/images/manga/", 1)[1] if cover and "/images/manga/" in cover else cover,
            "mal": int(mal_id),
        })
        index.add(target)

    # MangaUpdates: joined by the link MangaDex gives, or by name and year, otherwise its own entry.
    for mu_id, row in mangaupdates.items():
        title, kind, year, rating, votes, cover, genres, description = row
        target = by_mu.get(mu_id) or index.find([title], year)
        if target is not None:
            works[target].setdefault("mangaupdates", mu_id)
            continue
        target = "mu:" + mu_id
        works[target] = compact({
            "id": target, "source": "mangaupdates", "title": title, "kind": kind, "year": year,
            "rating": rating, "votes": votes,
            "adult": True if ADULT_GENRES & {g.casefold() for g in genres} else None,
            "description": description, "genres": genres,
            "cover": "mu:" + cover.rsplit("/image/", 1)[1] if cover and "/image/" in cover else cover,
            "mangaupdates": mu_id,
        })
        index.add(target)

    # The servers: each title is filed under the work its name matches. Titles no other
    # catalogue knows are gathered by name into works of their own.
    for server, items in servers.items():
        for item_id, names in items.items():
            target = index.find(names)
            if target is None:
                key = title_key(names[0])
                if not key:
                    continue
                target = "x:" + hashlib.sha1(key.encode()).hexdigest()[:12]
                if target not in works:
                    works[target] = {"id": target, "source": "servers", "title": names[0], "names": names[1:]}
                    index.add(target)
            works[target].setdefault("servers", {})[server] = item_id
    return works


# ---------------------------------------------------------------- output

def write_gz(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    # A fixed timestamp keeps unchanged files byte-identical between runs.
    with open(path, "wb") as handle:
        handle.write(gzip.compress(raw, 9, mtime=0))
    return len(raw)


def cover_file(url):
    """The part of an AniList cover address that changes; the app adds the size it wants."""
    if not url:
        return None
    match = re.search(r"/cover/(?:large|medium|small)/(.+)$", url)
    return match.group(1) if match else url


def extra_bucket(work_id):
    return hashlib.sha1(work_id.encode()).hexdigest()[:2]


def build(anilist, works, out, sources):
    anime = {i: e for i, e in anilist.items() if e["type"] == "anime"}
    manga = {i: e for i, e in works.items() if isinstance(i, int)}
    extra = {i: e for i, e in works.items() if isinstance(i, str)}
    sizes = {}

    def bucketed(folder, entries, key):
        buckets = {}
        for entry_id, entry in entries.items():
            buckets.setdefault(key(entry_id), {})[str(entry_id)] = entry
        for bucket, content in buckets.items():
            sizes[f"{folder}/{bucket}"] = write_gz(os.path.join(out, folder, f"{bucket}.json.gz"), content)
        return sorted(buckets)

    anime_buckets = bucketed("anime", anime, lambda i: i // 1000)
    manga_buckets = bucketed("manga", manga, lambda i: i // 1000)
    extra_buckets = bucketed("extra", extra, extra_bucket)

    def row(entry, kind_field):
        popularity = entry.get("popularity") or entry.get("members") or entry.get("votes") or 0
        return [entry["id"], entry.get(kind_field), year_of(entry), popularity, 1 if entry.get("adult") else 0,
                cover_file(entry.get("cover")), names_of(entry), sorted(entry.get("servers", {}))]

    search_anime = sorted((row(e, "format")[:7] for e in anime.values()), key=lambda r: -r[3])
    order = {"int": 0, "md:": 1, "mal": 2, "mu:": 3, "x:": 4}
    search_manga = sorted(
        (row(e, "kind") for e in works.values()),
        key=lambda r: (order["int"] if isinstance(r[0], int) else order.get(r[0][:3], order.get(r[0][:2], 5)), -r[3]))
    sizes["search/anime"] = write_gz(os.path.join(out, "search", "anime.json.gz"), search_anime)
    # jsDelivr serves files up to 20 MB, so the manga list is written in parts, in the same order.
    manga_parts = [search_manga[start:start + SEARCH_PART_ROWS] for start in range(0, len(search_manga), SEARCH_PART_ROWS)]
    for number, part in enumerate(manga_parts):
        sizes[f"search/manga/{number}"] = write_gz(os.path.join(out, "search", "manga", f"{number}.json.gz"), part)

    maps = {
        "mal": {
            "anime": {str(e["mal"]): e["id"] for e in anime.values() if e.get("mal")},
            "manga": {str(e["mal"]): e["id"] for e in works.values() if e.get("mal")},
        },
        "mangadex": {str(e["id"]): e["mangadex"] for e in works.values() if e.get("mangadex")},
        "mangaupdates": {str(e["id"]): e["mangaupdates"] for e in works.values() if e.get("mangaupdates")},
    }
    for name, value in maps.items():
        sizes[f"maps/{name}"] = write_gz(os.path.join(out, "maps", f"{name}.json.gz"), value)

    airing = sorted(
        [[e["id"], e["nextEpisode"]["episode"], e["nextEpisode"]["airingAt"]] for e in anime.values() if e.get("nextEpisode")],
        key=lambda r: r[2])
    sizes["airing"] = write_gz(os.path.join(out, "airing.json.gz"), airing)

    counts = {
        "anime": len(anime),
        "manga": len(manga),
        "manhwa": sum(1 for e in works.values() if e.get("kind") == "manhwa"),
        "manhua": sum(1 for e in works.values() if e.get("kind") == "manhua"),
        "mangaTotal": len(works),
        "onlyOnMangadex": sum(1 for i in extra if i.startswith("md:")),
        "onlyOnMyanimelist": sum(1 for i in extra if i.startswith("mal:")),
        "onlyOnMangaupdates": sum(1 for i in extra if i.startswith("mu:")),
        "onlyOnServers": sum(1 for i in extra if i.startswith("x:")),
        "onAServer": sum(1 for e in works.values() if e.get("servers")),
    }
    manifest = {
        "schema": 2,
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "counts": counts,
        "sources": sources,
        "buckets": {"anime": anime_buckets, "manga": manga_buckets, "extra": extra_buckets},
        "extraBucket": "first two hex characters of the SHA-1 of the id, e.g. sha1('mu:dkd0yz5')",
        "ids": {
            "number": "AniList id (anime/{id/1000} or manga/{id/1000})",
            "md:<uuid>": "only on MangaDex", "mal:<id>": "only on MyAnimeList",
            "mu:<id>": "only on MangaUpdates", "x:<hash>": "only on the manga servers",
        },
        "covers": {
            "anime": "https://s4.anilist.co/file/anilistcdn/media/anime/cover/{size}/{file}",
            "manga": "https://s4.anilist.co/file/anilistcdn/media/manga/cover/{size}/{file}",
            "md:": "https://uploads.mangadex.org/covers/{file}.{size}.jpg",
            "mal:": "https://cdn.myanimelist.net/images/manga/{file}",
            "mu:": "https://cdn.mangaupdates.com/image/{file}",
            "sizes": {"anilist": ["small", "medium", "large"], "mangadex": ["256", "512"]},
        },
        "search": {"anime": "search/anime.json.gz",
                   "manga": [f"search/manga/{n}.json.gz" for n in range(len(manga_parts))]},
        "searchRow": ["id", "format or kind", "year", "popularity", "adult", "cover", "titles", "servers"],
    }
    total = sum(os.path.getsize(os.path.join(root, f)) for root, _, files in os.walk(out) for f in files)
    log(f"Wrote {total / 1e6:.1f} MB compressed ({sum(sizes.values()) / 1e6:.1f} MB raw): {counts}")
    biggest = sorted(((v, k) for k, v in sizes.items()), reverse=True)[:5]
    log("  largest (raw): " + ", ".join(f"{k} {v / 1e6:.1f} MB" for v, k in biggest))
    manifest["bytes"] = total
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
    # jsDelivr will not serve a GitHub commit larger than 150 MB.
    largest_file = max((os.path.getsize(os.path.join(root, f)), os.path.join(root, f))
                       for root, _, files in os.walk(out) for f in files)
    if largest_file[0] > FILE_LIMIT:
        sys.exit(f"Refusing to publish: {largest_file[1]} is {largest_file[0] / 1e6:.1f} MB, over jsDelivr's 20 MB file limit")
    if total > 140e6:
        sys.exit(f"Refusing to publish: {total / 1e6:.1f} MB is too close to jsDelivr's 150 MB limit")
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--cache", help="folder holding the last copy of each slower catalogue")
    parser.add_argument("--previous", help="the last published latest.json, to refuse a build that lost titles")
    parser.add_argument("--limit", type=int, help="read only this many ids/titles from each source (testing)")
    parser.add_argument("--refresh", action="store_true", help="read every slower catalogue again now")
    parser.add_argument("--skip", default="", help="comma-separated catalogues to use only from the cache")
    args = parser.parse_args()
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}

    slow = slow_catalogues()
    with ThreadPoolExecutor(max_workers=len(slow) + 2) as pool:
        anilist_job = pool.submit(fetch_anilist, args.limit)
        mangadex_job = pool.submit(fetch_mangadex, args.limit)
        slow_jobs = {name: pool.submit(cached_catalogue, args.cache, name, read, args.limit, args.refresh, skip)
                     for name, read in slow.items()}
        anilist = anilist_job.result()
        mangadex = mangadex_job.result()
        results = {name: job.result() for name, job in slow_jobs.items()}

    if args.cache and not args.limit:
        for name, (items, fresh) in results.items():
            if fresh:
                save_cache(args.cache, name, items)

    servers = {name[len("server-"):]: items for name, (items, _) in results.items() if name.startswith("server-")}
    anilist_manga = {i: e for i, e in anilist.items() if e["type"] == "manga"}
    works = merge_manga(anilist_manga, mangadex, results["myanimelist"][0], results["mangaupdates"][0], servers)
    sources = {"anilist": len(anilist), "mangadex": len(mangadex)}
    sources.update({name: len(items) for name, (items, _) in results.items()})
    manifest = build(anilist, works, args.out, sources)

    if args.previous and os.path.exists(args.previous) and not args.limit:
        with open(args.previous, encoding="utf-8") as handle:
            before = json.load(handle).get("counts", {})
        for name in ("anime", "manga"):
            if before.get(name) and manifest["counts"][name] < before[name] * 0.9:
                sys.exit(f"Refusing to publish: {name} fell from {before[name]} to {manifest['counts'][name]}")


if __name__ == "__main__":
    main()
"""Builds the AniCaribe metadata index: every anime and manga on AniList, plus the MangaDex titles
AniList does not have, written as small gzipped JSON files a phone can fetch from a CDN.

    python scripts/build.py --out out [--previous latest.json] [--limit N]

Each run rebuilds everything from scratch, so titles removed upstream disappear and scores stay
current. Nothing is written to --out until both sources have been read in full; a run that fails
part-way leaves the published data as it was.
"""
from __future__ import annotations

import argparse
import gzip
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

USER_AGENT = "AniCaribe-meta/1.0 (+https://github.com/incogone/anicaribe-meta)"
ANILIST = "https://graphql.anilist.co"
MANGADEX = "https://api.mangadex.org"

ANILIST_FIELDS = (
    "id idMal type format status countryOfOrigin isAdult title{romaji english native} synonyms "
    "description(asHtml:false) startDate{year month day} endDate{year month day} season seasonYear "
    "episodes duration chapters volumes genres tags{name rank isMediaSpoiler} averageScore meanScore "
    "popularity favourites coverImage{extraLarge large color} bannerImage source "
    "studios(isMain:true){nodes{name}} relations{edges{relationType node{id type}}} "
    "nextAiringEpisode{airingAt episode} updatedAt"
)
IDS_PER_PAGE = 50
PAGES_PER_REQUEST = 8  # 400 ids a request keeps a full pass to a few hundred requests
DESCRIPTION_LIMIT = 800
ROMANISED = {"en", "ja-ro", "ko-ro", "zh-ro", "zh-hk-ro"}
KIND_BY_COUNTRY = {"JP": "manga", "KR": "manhwa", "CN": "manhua", "TW": "manhua", "HK": "manhua"}
KIND_BY_LANGUAGE = {"ja": "manga", "ko": "manhwa", "zh": "manhua", "zh-hk": "manhua"}


def log(*parts):
    print(datetime.now(timezone.utc).strftime("%H:%M:%S"), *parts, flush=True)


def request(url, body=None, attempts=8):
    """One HTTP call with retries; honours Retry-After and backs off on errors."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    delay = 5
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data, headers), timeout=90) as response:
                return json.load(response), response.headers
        except urllib.error.HTTPError as error:
            if error.code in (429, 500, 502, 503, 504) and attempt < attempts:
                wait = int(error.headers.get("Retry-After") or delay)
                log(f"  {error.code} from {url[:60]}; waiting {wait}s")
                time.sleep(wait + 1)
                delay = min(delay * 2, 120)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            if attempt < attempts:
                log(f"  {error} from {url[:60]}; waiting {delay}s")
                time.sleep(delay)
                delay = min(delay * 2, 120)
                continue
            raise
    raise RuntimeError("unreachable")


# ---------------------------------------------------------------- AniList

def clean_description(text):
    if not text:
        return None
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > DESCRIPTION_LIMIT:
        text = text[:DESCRIPTION_LIMIT].rsplit(" ", 1)[0].rstrip(",.;: ") + "…"
    return text or None


def date_text(value):
    if not value or not value.get("year"):
        return None
    parts = [f"{value['year']:04d}"]
    if value.get("month"):
        parts.append(f"{value['month']:02d}")
        if value.get("day"):
            parts.append(f"{value['day']:02d}")
    return "-".join(parts)


def compact(entry):
    return {k: v for k, v in entry.items() if v not in (None, [], {}, "")}


def anilist_entry(media):
    cover = media.get("coverImage") or {}
    kind = None
    if media["type"] == "MANGA":
        kind = "novel" if media.get("format") == "NOVEL" else KIND_BY_COUNTRY.get(media.get("countryOfOrigin"), "manga")
    return compact({
        "id": media["id"],
        "mal": media.get("idMal"),
        "type": media["type"].lower(),
        "kind": kind,
        "format": media.get("format"),
        "status": media.get("status"),
        "country": media.get("countryOfOrigin"),
        "adult": True if media.get("isAdult") else None,
        "title": compact(media.get("title") or {}),
        "synonyms": [s for s in media.get("synonyms") or [] if s],
        "description": clean_description(media.get("description")),
        "start": date_text(media.get("startDate")),
        "end": date_text(media.get("endDate")),
        "season": media.get("season"),
        "seasonYear": media.get("seasonYear"),
        "episodes": media.get("episodes"),
        "duration": media.get("duration"),
        "chapters": media.get("chapters"),
        "volumes": media.get("volumes"),
        "genres": media.get("genres") or [],
        "tags": [t["name"] for t in media.get("tags") or [] if not t.get("isMediaSpoiler") and (t.get("rank") or 0) >= 60][:10],
        "score": media.get("averageScore"),
        "meanScore": media.get("meanScore"),
        "popularity": media.get("popularity"),
        "favourites": media.get("favourites"),
        "cover": cover.get("extraLarge") or cover.get("large"),
        "coverColor": cover.get("color"),
        "banner": media.get("bannerImage"),
        "source": media.get("source"),
        "studios": [n["name"] for n in (media.get("studios") or {}).get("nodes") or []],
        "relations": [
            [e["node"]["id"], e["node"]["type"].lower(), e["relationType"]]
            for e in (media.get("relations") or {}).get("edges") or [] if e.get("node")
        ],
        "nextEpisode": media.get("nextAiringEpisode"),
        "updatedAt": media.get("updatedAt"),
    })


def latest_anilist_id():
    query = "{Page(perPage:1){media(sort:ID_DESC){id}}}"
    data, _ = request(ANILIST, {"query": query})
    return data["data"]["Page"]["media"][0]["id"]


def fetch_anilist(limit=None):
    """Every AniList entry, read by id range: AniList pages only 5,000 entries deep, ids are not."""
    top = latest_anilist_id() + 2000  # room for entries added while the run is going
    if limit:
        top = min(top, limit)
    found = {}
    chunk = IDS_PER_PAGE * PAGES_PER_REQUEST
    log(f"AniList: reading ids 1 to {top}")
    for start in range(1, top + 1, chunk):
        parts = []
        for k in range(PAGES_PER_REQUEST):
            first = start + k * IDS_PER_PAGE
            ids = ",".join(str(i) for i in range(first, first + IDS_PER_PAGE))
            parts.append(f"p{k}:Page(perPage:{IDS_PER_PAGE}){{media(id_in:[{ids}]){{{ANILIST_FIELDS}}}}}")
        began = time.monotonic()
        data, headers = request(ANILIST, {"query": "{" + " ".join(parts) + "}"})
        if data.get("errors"):
            raise RuntimeError(f"AniList error at id {start}: {data['errors']}")
        for page in data["data"].values():
            for media in page["media"]:
                found[media["id"]] = anilist_entry(media)
        if (start // chunk) % 25 == 0:
            log(f"  id {start}: {len(found)} titles so far")
        # Stay under the published rate limit (requests per minute) with room to spare.
        per_minute = int(headers.get("X-RateLimit-Limit") or 30)
        spacing = 60 / max(per_minute - 3, 1)
        remaining = int(headers.get("X-RateLimit-Remaining") or per_minute)
        time.sleep(max(spacing - (time.monotonic() - began), 0) + (5 if remaining < 3 else 0))
    log(f"AniList: {len(found)} titles")
    return found


# ---------------------------------------------------------------- MangaDex

def first_text(values, preferred=("en", "ja-ro", "ko-ro", "zh-ro")):
    if not values:
        return None
    for language in preferred:
        if values.get(language):
            return values[language]
    return next(iter(values.values()), None)


def mangadex_entry(item):
    attributes = item["attributes"]
    titles = []
    for language, value in (attributes.get("title") or {}).items():
        titles.append((language, value))
    for alt in attributes.get("altTitles") or []:
        titles.extend(alt.items())
    names = []
    for language, value in titles:
        if value and value not in names and (language in ROMANISED or language in ("ja", "ko", "zh", "zh-hk")):
            names.append(value)
    cover = next(
        (r["attributes"]["fileName"] for r in item.get("relationships", [])
         if r["type"] == "cover_art" and r.get("attributes")), None)
    links = attributes.get("links") or {}
    language = attributes.get("originalLanguage")
    tags = [t["attributes"]["name"].get("en") for t in attributes.get("tags") or []]
    return compact({
        "id": item["id"],
        "title": first_text(attributes.get("title")) or (names[0] if names else None),
        "names": names,
        "kind": KIND_BY_LANGUAGE.get(language, "manga"),
        "language": language,
        "status": (attributes.get("status") or "").upper() or None,
        "year": attributes.get("year"),
        "rating": attributes.get("contentRating"),
        "adult": True if attributes.get("contentRating") in ("erotica", "pornographic") else None,
        "description": clean_description(first_text(attributes.get("description"), ("en",))),
        "tags": [t for t in tags if t][:12],
        "lastChapter": attributes.get("lastChapter") or None,
        "cover": f"https://uploads.mangadex.org/covers/{item['id']}/{cover}" if cover else None,
        "anilist": int(links["al"]) if str(links.get("al", "")).isdigit() else None,
        "mal": int(links["mal"]) if str(links.get("mal", "")).isdigit() else None,
        "mangaupdates": links.get("mu"),
        "updatedAt": attributes.get("updatedAt"),
    })


def fetch_mangadex(limit=None):
    """Every MangaDex title. Its list stops at 10,000 entries, so it is read in windows of
    creation date, each starting where the last one ended."""
    found = {}
    since = "2000-01-01T00:00:00"
    log("MangaDex: reading all titles")
    while True:
        last_created = None
        for offset in range(0, 10_000, 100):
            query = [
                ("limit", "100"), ("offset", str(offset)), ("order[createdAt]", "asc"),
                ("createdAtSince", since), ("includes[]", "cover_art"),
            ] + [("contentRating[]", r) for r in ("safe", "suggestive", "erotica", "pornographic")]
            data, _ = request(f"{MANGADEX}/manga?{urllib.parse.urlencode(query)}")
            items = data.get("data") or []
            for item in items:
                found[item["id"]] = mangadex_entry(item)
                last_created = item["attributes"]["createdAt"]
            time.sleep(0.35)
            if len(items) < 100 or (limit and len(found) >= limit):
                log(f"MangaDex: {len(found)} titles")
                return found
        log(f"  {len(found)} titles so far")
        next_since = last_created[:19]
        if next_since <= since:  # every title in a window shares one timestamp; step past it
            next_since = (datetime.fromisoformat(since) + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S")
        since = next_since


# ---------------------------------------------------------------- output

def write_gz(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    # A fixed timestamp keeps unchanged files byte-identical between runs.
    with open(path, "wb") as handle:
        handle.write(gzip.compress(raw, 9, mtime=0))
    return len(raw)


def cover_file(url):
    """The part of an AniList cover address that changes; the app adds the size it wants."""
    if not url:
        return None
    match = re.search(r"/cover/(?:large|medium|small)/(.+)$", url)
    return match.group(1) if match else url


def unique(values):
    seen, kept = set(), []
    for value in values:
        key = (value or "").strip().casefold()
        if key and key not in seen:
            seen.add(key)
            kept.append(value.strip())
    return kept


def build(anilist, mangadex, out):
    anime = {i: e for i, e in anilist.items() if e["type"] == "anime"}
    manga = {i: e for i, e in anilist.items() if e["type"] == "manga"}

    # MangaDex titles linked to an AniList manga add their ids and names to it; the rest stand alone.
    extra = {}
    for md in mangadex.values():
        target = manga.get(md.get("anilist"))
        if target is None:
            extra[md["id"]] = md
            continue
        target.setdefault("mangadex", md["id"])
        if md.get("mangaupdates"):
            target.setdefault("mangaupdates", md["mangaupdates"])
        known = {t.casefold() for t in list(target.get("title", {}).values()) + target.get("synonyms", [])}
        more = [n for n in md.get("names", []) if n.casefold() not in known]
        if more:
            target["altNames"] = unique(target.get("altNames", []) + more)[:30]

    sizes = {}

    def bucketed(folder, entries, key):
        buckets = {}
        for entry_id, entry in entries.items():
            buckets.setdefault(key(entry_id), {})[str(entry_id)] = entry
        for bucket, content in buckets.items():
            sizes[f"{folder}/{bucket}"] = write_gz(os.path.join(out, folder, f"{bucket}.json.gz"), content)
        return sorted(buckets)

    anime_buckets = bucketed("anime", anime, lambda i: i // 1000)
    manga_buckets = bucketed("manga", manga, lambda i: i // 1000)
    dex_buckets = bucketed("mangadex", extra, lambda i: i[:2])

    def titles(entry):
        title = entry.get("title", {})
        names = [title.get("english"), title.get("romaji"), title.get("native")] if isinstance(title, dict) else [title]
        return unique(names + entry.get("synonyms", []) + entry.get("altNames", []) + entry.get("names", []))

    def year(entry):
        return int(entry["start"][:4]) if entry.get("start") else entry.get("seasonYear") or entry.get("year")

    search_anime = sorted(
        ([e["id"], e.get("format"), year(e), e.get("popularity", 0), 1 if e.get("adult") else 0,
          cover_file(e.get("cover")), titles(e)] for e in anime.values()),
        key=lambda row: -row[3])
    search_manga = sorted(
        ([e["id"], e.get("kind"), year(e), e.get("popularity", 0), 1 if e.get("adult") else 0,
          cover_file(e.get("cover")), titles(e)] for e in manga.values()),
        key=lambda row: -row[3])
    search_manga += [
        ["md:" + e["id"], e.get("kind"), e.get("year"), 0, 1 if e.get("adult") else 0,
         e["cover"].split("/covers/", 1)[1] if e.get("cover") else None, titles(e)]
        for e in sorted(extra.values(), key=lambda e: e["id"])
    ]
    sizes["search/anime"] = write_gz(os.path.join(out, "search", "anime.json.gz"), search_anime)
    sizes["search/manga"] = write_gz(os.path.join(out, "search", "manga.json.gz"), search_manga)

    mal = {
        "anime": {str(e["mal"]): e["id"] for e in anime.values() if e.get("mal")},
        "manga": {str(e["mal"]): e["id"] for e in manga.values() if e.get("mal")},
    }
    sizes["maps/mal"] = write_gz(os.path.join(out, "maps", "mal.json.gz"), mal)
    dex = {str(e["id"]): e["mangadex"] for e in manga.values() if e.get("mangadex")}
    sizes["maps/mangadex"] = write_gz(os.path.join(out, "maps", "mangadex.json.gz"), dex)

    airing = sorted(
        [[e["id"], e["nextEpisode"]["episode"], e["nextEpisode"]["airingAt"]] for e in anime.values() if e.get("nextEpisode")],
        key=lambda row: row[2])
    sizes["airing"] = write_gz(os.path.join(out, "airing.json.gz"), airing)

    counts = {
        "anime": len(anime),
        "manga": len(manga),
        "manhwa": sum(1 for e in manga.values() if e.get("kind") == "manhwa"),
        "manhua": sum(1 for e in manga.values() if e.get("kind") == "manhua"),
        "mangadexOnly": len(extra),
        "linkedToMangadex": len(dex),
    }
    manifest = {
        "schema": 1,
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "counts": counts,
        "buckets": {"anime": anime_buckets, "manga": manga_buckets, "mangadex": dex_buckets},
        "covers": {
            "anime": "https://s4.anilist.co/file/anilistcdn/media/anime/cover/{size}/{file}",
            "manga": "https://s4.anilist.co/file/anilistcdn/media/manga/cover/{size}/{file}",
            "mangadex": "https://uploads.mangadex.org/covers/{file}.{size}.jpg",
            "sizes": {"anilist": ["small", "medium", "large"], "mangadex": ["256", "512"]},
        },
        "searchRow": ["id", "format or kind", "year", "popularity", "adult", "cover file", "titles"],
        "sources": ["AniList (anilist.co)", "MangaDex (mangadex.org)"],
    }
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
    total = sum(os.path.getsize(os.path.join(root, f)) for root, _, files in os.walk(out) for f in files)
    log(f"Wrote {total / 1e6:.1f} MB compressed ({sum(sizes.values()) / 1e6:.1f} MB raw): {counts}")
    # jsDelivr will not serve a GitHub commit larger than 150 MB.
    if total > 140e6:
        sys.exit(f"Refusing to publish: {total / 1e6:.1f} MB is too close to jsDelivr's 150 MB limit")
    manifest["bytes"] = total
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--previous", help="the last published latest.json, to refuse a build that lost titles")
    parser.add_argument("--limit", type=int, help="read only this many ids/titles (for testing)")
    args = parser.parse_args()

    anilist = fetch_anilist(args.limit)
    mangadex = fetch_mangadex(args.limit)
    manifest = build(anilist, mangadex, args.out)

    if args.previous and os.path.exists(args.previous) and not args.limit:
        with open(args.previous, encoding="utf-8") as handle:
            before = json.load(handle).get("counts", {})
        for name in ("anime", "manga"):
            if before.get(name) and manifest["counts"][name] < before[name] * 0.9:
                sys.exit(f"Refusing to publish: {name} fell from {before[name]} to {manifest['counts'][name]}")


if __name__ == "__main__":
    main()
