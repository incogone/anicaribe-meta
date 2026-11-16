# AniCaribe metadata index

A free, static index of every anime and manga (including manhwa and manhua) on AniList, plus the
MangaDex titles that AniList does not list. A GitHub Action rebuilds it from scratch every day,
and jsDelivr serves it. There is no server.

## Where to read it

Start from `latest.json` on `main`. It names the commit that holds the current data:

```
https://cdn.jsdelivr.net/gh/incogone/anicaribe-meta@main/latest.json
```

```json
{
  "built": "2026-11-16T04:41:09Z",
  "commit": "<sha>",
  "base": "https://cdn.jsdelivr.net/gh/incogone/anicaribe-meta@<sha>/",
  "fallback": "https://raw.githubusercontent.com/incogone/anicaribe-meta/<sha>/",
  "counts": { "anime": 0, "manga": 0, "manhwa": 0, "manhua": 0, "mangadexOnly": 0 }
}
```

Every other file is read from `base`. A commit's files never change, so they can be cached for as
long as you like. jsDelivr can take up to 12 hours to notice a new `latest.json`. Until it does,
the previous commit keeps working.

## Files (all gzipped JSON)

| File | Contents |
| --- | --- |
| `manifest.json` | Build time, counts, bucket lists, and cover address patterns (plain JSON) |
| `search/anime.json.gz` | One row per anime: `[id, format, year, popularity, adult, coverFile, [titles…]]`, most popular first |
| `search/manga.json.gz` | The same for manga, manhwa, manhua and novels, where the second field is the kind. MangaDex-only rows have ids like `md:<uuid>` |
| `anime/{id/1000}.json.gz` | Full anime entries keyed by AniList id |
| `manga/{id/1000}.json.gz` | Full manga entries keyed by AniList id, with `mangadex`, `mangaupdates` and `altNames` where MangaDex links them |
| `mangadex/{first 2 characters of uuid}.json.gz` | Titles found only on MangaDex |
| `maps/mal.json.gz` | MyAnimeList id → AniList id, for anime and manga |
| `maps/mangadex.json.gz` | AniList manga id → MangaDex id |
| `airing.json.gz` | Airing anime: `[id, nextEpisode, airingAt]`, soonest first |

Cover files are turned into addresses with the patterns in `manifest.json`. For AniList, sizes are
`small`, `medium` and `large`. For MangaDex, they are `256` and `512`.

## How it updates

`.github/workflows/build.yml` runs every day at 04:23 UTC, and can also be started from the
Actions tab:

1. `scripts/build.py` reads every AniList entry, 400 ids per request, at AniList's rate limit,
   then every MangaDex title. This takes about half an hour.
2. The result is pushed as the only commit on the `data` branch, tagged `data-YYYYMMDD-HHMM`.
   Only the last 14 tags are kept.
3. `latest.json` on `main` is updated to point at it.

A build that fails part-way, loses more than 10% of its titles, or approaches jsDelivr's 150 MB
limit is not published. The previous data stays in place.

GitHub pauses scheduled workflows in repositories that have had no activity for 60 days. If the
daily runs stop, re-enable them from the Actions tab.

## Sources and terms

Data comes from [AniList](https://anilist.co) and [MangaDex](https://mangadex.org). This index is
a cache and is not affiliated with either site. Their terms apply to the data. The code in this
repository is MIT-0 licensed.
