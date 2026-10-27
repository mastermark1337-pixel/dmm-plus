# DMM Plus

Stremio/Nuvio addon that **wraps Debrid Media Manager's TorBox cast addon** and
merges it with independent scrapers, so a title returns ~40 streams instead of 5.

```
DMM Cast TB          5 file entries + 1 "cast" entry   (hard cap, no query param)
CHQ                  up to 20
PrimeSrc             up to 20
2Embed               3–6
MultiEmbed           1
VidSrc               0 on bot-gated networks
--------------------------------------
merged               40–45, deduped, quality-sorted
```

Verified on this box:

| title | streams |
|---|---|
| `tt0137523` (movie) | **45** — DMM 6, CHQ 15, PrimeSrc 20, 2Embed 3, MultiEmbed 1 |
| `tt0944947:1:1` (series) | **40** — DMM 6, CHQ 10, PrimeSrc 20, 2Embed 3, MultiEmbed 1 |

## Why 5 was the ceiling

DMM's addon answers with a fixed 5 file entries plus a "Cast a file inside a
torrent" entry. `?limit=`, `?count=`, `?page=`, `?all=`, `?n=` are all ignored —
the response is byte-identical. There is no pagination and no catalog to walk
(`/catalog/movie/tb-casted-movies.json` returns `{"metas":[]}`). The only route
past five is to answer the Stremio request yourself, which is what this does.

## Run

```bash
python addon.py
# add http://<this-pc>:7100/manifest.json to Stremio / Nuvio
```

Windows: `run.bat`.

## Config (env vars)

| Var | Default | Meaning |
|---|---|---|
| `PORT` | `7100` | listen port (7000 is used by the UKTurks addon) |
| `DMM_TOKEN` | `_oKCkbeH_FFa` | the segment after `/api/stremio-tb/` in your DMM addon URL |
| `TMDB_API_KEY` | app's key | override if rate-limited |
| `PROVIDER_TIMEOUT` | `14` | per-provider network ceiling (seconds) |
| `SOFT_DEADLINE` | `8` | how long the merge waits before answering with what it has |
| `MAX_STREAMS` | `60` | hard cap on the merged list |
| `CACHE_TTL` | `180` | per-title cache, short because signed URLs expire |
| `DISABLED_PROVIDERS` | — | e.g. `vidsrc,multiembed` |

To use **your own** DMM library: find your addon URL in DMM → Cast → Stremio
addon, copy the token segment, then

```powershell
$env:DMM_TOKEN="your_token_here"; python addon.py
```

## Behaviour

- **DMM entries pass through verbatim** — name, title, url, externalUrl,
  `behaviorHints.bingeGroup`, play paths and their `h=` signature intact. This
  addon never rewrites a `/play/` URL; DMM's signature check would break it.
- **IMDb → TMDB** is resolved once per title via `/find?external_source=imdb_id`,
  cached. CHQ, PrimeSrc, 2Embed and MultiEmbed all key on the numeric TMDB id;
  DMM keys on IMDb.
- **Dedupe** on playable URL, then `infoHash`, then `externalUrl` — the same
  release surfacing from DMM and CHQ collapses to the DMM entry (it keeps the
  higher-priority slot).
- **Sort**: quality bucket first (4K → 1080p → 720p → …), then playable before
  metadata-only, then **smaller file first**. A 100 GB remux is not better than
  a 12 GB 1080p encode when it has to stream over wifi.
- **Download-only mirrors filtered** — CHQ returns `10gbps`, `pixeldrain` and
  `hubcloud.ist` links that stall every player. Dropped before they reach the
  list.
- **Failures are isolated.** A dead provider returns `[]`; the merge answers with
  whatever arrived before `SOFT_DEADLINE`. Nothing hangs.
- **No proxy hop.** URLs go to the player directly with per-host headers, so
  Referer-gated hosts behave the same as they do in the app.

## Files

```
addon.py     everything: http, 6 providers, merge, HTTP server
run.bat      windows launcher
```

Stdlib only — no pip step. Python 3.9+.
