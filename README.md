# TorBox Plus 2.0.1

Independent Nuvio/Stremio add-on using your TorBox account. It queries Peerflix and Knaben, verifies matching files against TorBox cache, removes duplicates, and returns signed playback links. It does not call DMM Cast and has no five-result limit.

## Install

Add `https://dmm-plus.onrender.com/manifest.json` in Nuvio. The displayed add-on name is **TorBox Plus**. The existing URL and internal manifest ID are retained so previous DMM Plus installations can update. The original **DMM Cast for TorBox** is a separate add-on and retains its own limits.

## How results work

- Only cache-confirmed video files are offered. Browsing never adds a torrent.
- Peerflix searches by IMDb id. Knaben searches using public Cinemeta metadata, verifies the title/year, and checks matching files against TorBox.
- English-labelled releases are preferred; unknown language is kept, not guessed. CAM/TS releases are ranked below better copies within the language group. CAM entries stay visible by default for new titles.
- Resolution, codec, HDR and release type labels come from the filename. File size comes from TorBox.
- Identical filenames and byte sizes are deduplicated across hashes/indexes.
- A TV pack is offered only if its cached file listing identifies the requested episode unambiguously. An index's file number is never treated as a TorBox file ID.
- Selecting a result rechecks its cache status, performs a cache-only add, maps the actual TorBox file ID and redirects to a CDN chosen for the viewer. Added cached torrents remain in your TorBox library.
- One index failure does not hide the other index. `/health` reports safe per-index status and candidate counts.

Counts depend on index coverage and what TorBox currently caches. There is no promise of a fixed count or a complete copy of any other service's search database.

## Configuration

Only `TORBOX_API_KEY` is required. Save it in Render's environment, never in the repository. Indexes receive the title/IMDb identifier, never the account key. The key is sent exclusively to TorBox's API.

| Variable | Default | Purpose |
| --- | --- | --- |
| `TORBOX_API_KEY` | unset | Your TorBox account key |
| `TORBOX_MAX_STREAMS` | `100` | Maximum unique results per title, configurable up to 200 |
| `TORBOX_MAX_CANDIDATES` | `300` | Index candidates checked, up to 1,000; Knaben returns up to 300 |
| `PREFER_ENGLISH` | `true` | Prefer explicitly English-labelled releases |
| `HIDE_CAM` | `false` | Exclude CAM/TS copies if enabled |
| `MAX_FILE_SIZE_GB` | `0` | Optional size ceiling; zero means unlimited |
| `KNABEN_ENABLED` | `true` | Enable the independent Knaben search |
| `TORRENT_SEARCH_URL` | `https://addon.peerflix.mov` | Primary compatible HTTPS torrent index |
| `PUBLIC_BASE_URL` | `RENDER_EXTERNAL_URL` | Public origin for signed playback links |
| `PORT` | `7100` | HTTP port; supplied by Render |
| `PROVIDER_TIMEOUT` | `14` | Per-call network timeout |
| `SOFT_DEADLINE` | `14` | Response budget, including source fan-out and cache checks |
| `CACHE_TTL` | `180` | Successful search cache lifetime; empty results expire within 10 seconds |

`DMM_TOKEN` is no longer read or needed. UKTurks remains a separate add-on.

## Development

Run `python addon.py`. Test with `python -m unittest discover -s tests -v`. No dependencies beyond Python 3.9+ standard library.

Routes: `/manifest.json`, `/health`, `/stream/movie/{imdb}.json`, `/stream/series/{imdb}:{season}:{episode}.json`, and signed `/torbox/play/...` paths.

Documentation: [TorBox API](https://api-docs.torbox.app/), [Knaben API](https://knaben.org/api/v2/), [Stremio protocol](https://stremio.github.io/stremio-addon-sdk/protocol.html).
