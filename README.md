# AniCaribe metadata index

A free, static index of anime and manga, including manhwa and manhua, built for the AniCaribe app.
A GitHub Action rebuilds it every day, and jsDelivr serves it. There is no server.

| | Source | How often it is read |
| --- | --- | --- |
| Anime | [AniList](https://anilist.co) | every day |
| Manga, manhwa, manhua | [AniList](https://anilist.co), [MangaDex](https://mangadex.org) | every day |
| | [MyAnimeList](https://myanimelist.net) (through [Jikan](https://jikan.moe)), [MangaUpdates](https://www.mangaupdates.com) | weekly |
| | The title lists of the manga servers the app reads from: Weeb Central, MangaPill, MangaFox, MangaRead, ManhuaPlus, WebtoonClub, BaeToon, MangaBall, Mangago, Mangakakalot, Manganato, Toonily | weekly |

Records of the same work are merged into one entry. Three things link them:

- the ids each site gives for the others (AniList ↔ MyAnimeList, MangaDex → AniList, MyAnimeList and MangaUpdates)
- for MangaUpdates, the title and year
- for the servers, the title

A work that only one catalogue knows still gets an entry of its own. That includes titles that are
only on the servers.

## Where to read it

Start from `latest.json` on `main`. It names the commit that holds the current data:

```
https://cdn.jsdelivr.net/gh/incogone/anicaribe-meta@main/latest.json
```

```json
{
  "built": "2026-11-16T05:01:09Z",
  "commit": "<sha>",
  "base": "https://cdn.jsdelivr.net/gh/incogone/anicaribe-meta@<sha>/",
  "fallback": "https://raw.githubusercontent.com/incogone/anicaribe-meta/<sha>/",
  "counts": { "anime": 0, "manga": 0, "manhwa": 0, "manhua": 0, "mangaTotal": 0, "onAServer": 0 }
}
```

Every other file is read from `base`. A commit's files never change, so they can be cached for as
long as you like. jsDelivr can take up to 12 hours to notice a new `latest.json`. Until it does,
the previous commit keeps working.

## Ids

| Id | Meaning | Full entry in |
| --- | --- | --- |
| `21` (a number) | AniList id | `anime/{id/1000}.json.gz` or `manga/{id/1000}.json.gz` |
| `md:<uuid>` | only on MangaDex | `extra/{bucket}.json.gz` |
| `mal:<id>` | only on MyAnimeList | `extra/{bucket}.json.gz` |
| `mu:<id>` | only on MangaUpdates | `extra/{bucket}.json.gz` |
| `x:<hash>` | only on the manga servers | `extra/{bucket}.json.gz` |

`{bucket}` is the first two hex characters of the SHA-1 of the id. For example, `mu:dkd0yz5` is in
`extra/` + `sha1("mu:dkd0yz5")[:2]` + `.json.gz`.

## Files (all gzipped JSON)

| File | Contents |
| --- | --- |
| `manifest.json` | Build time, counts, how many entries each source gave, bucket lists, cover address patterns (plain JSON) |
| `search/anime.json.gz` | One row per anime: `[id, format, year, popularity, adult, cover, [titles…]]`, most popular first |
| `search/manga/{n}.json.gz` | One row per manga work: `[id, kind, year, popularity, adult, cover, [titles…], [servers…]]`, in parts of 50,000 rows (jsDelivr serves files up to 20 MB). `manifest.json` lists the parts under `search.manga`. AniList entries come first, then the others. `servers` lists the app's server ids that carry the work |
| `anime/{n}.json.gz` | Full anime entries keyed by AniList id |
| `manga/{n}.json.gz` | Full manga entries keyed by AniList id, plus `mangadex`, `mangaupdates`, `altNames`, and `servers` (server id → that server's id for the title) |
| `extra/{bucket}.json.gz` | Entries for works AniList does not have |
| `maps/mal.json.gz` | MyAnimeList id → entry id, for anime and manga |
| `maps/mangadex.json.gz` | Entry id → MangaDex id |
| `maps/mangaupdates.json.gz` | Entry id → MangaUpdates id |
| `airing.json.gz` | Airing anime: `[id, nextEpisode, airingAt]`, soonest first |

Covers are stored short. `manifest.json` gives the address pattern for each kind:

- a plain file name is an AniList cover
- `md:…` is a MangaDex cover
- `mal:…` is a MyAnimeList cover
- `mu:…` is a MangaUpdates cover

## How it updates

`.github/workflows/build.yml` runs every day at 04:23 UTC. It can also be started from the
Actions tab. Tick "refresh" to read the weekly catalogues again straight away.

1. `scripts/build.py` reads all of AniList (400 ids per request, at its rate limit) and all of
   MangaDex.
2. The weekly catalogues are kept on the `cache` branch and read again once their copy is a week
   old. A catalogue that fails, or comes back with less than half of what it had, keeps its last
   copy.
3. The result is pushed as the only commit on the `data` branch, tagged `data-YYYYMMDD-HHMM`. Only
   the last 14 tags are kept.
4. `latest.json` on `main` is updated to point at it.

A build is not published if any of these happens. The previous data stays in place.

- AniList or MangaDex fails.
- The build loses more than 10% of its anime or AniList manga.
- The data comes close to jsDelivr's 150 MB limit, or one file is over its 20 MB limit.

GitHub pauses scheduled workflows in repositories that have had no activity for 60 days. If the
daily runs stop, re-enable them from the Actions tab.

## Sources and terms

This index is a cache of other sites' data and is not affiliated with any of them. Their terms
apply to the data. The code in this repository is MIT-0 licensed.
