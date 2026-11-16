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
