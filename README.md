# DMM Plus 1.2.0

Stremio/Nuvio stream add-on combining DMM Cast and independent cached TorBox torrent search. Python standard library only. UKTurks and the CHQ, PrimeSrc, 2Embed, MultiEmbed and VidSrc hosters are not queried; install UKTurks separately if wanted.

## What fixes the five-result problem

DMM Cast limits shared results upstream. This add-on does not change that limit. With `TORBOX_API_KEY` configured, it separately searches Peerflix, checks the results against TorBox's cache, and offers up to 100 cached file entries under `TorBox`.

The search index receives the IMDb id, never the TorBox key. The key stays on the server and is sent only to TorBox. Streams use expiring signed playback paths. Listing results does not add torrents. Selecting a TorBox stream performs a cache-only add, maps the exact filename to TorBox's own file id, then redirects the player to the download link. A cached torrent added for playback remains in your TorBox library.

Counts vary with index coverage and cache availability. This is independent search, not a complete export of DMM's website results.

## Run

Set `DMM_TOKEN` and `TORBOX_API_KEY` in your environment, then run `python addon.py`. Install `http://localhost:7100/manifest.json` for local use. On Render, keep the existing HTTPS manifest URL.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `DMM_TOKEN` | unset | Cast token from your DMM TorBox manifest; preserves existing Cast results |
| `TORBOX_API_KEY` | unset | TorBox account key; enables independent cached torrent results |
| `TORBOX_MAX_STREAMS` | `100` | Maximum independent TorBox entries, separate from the hoster budget |
| `TORBOX_MAX_CANDIDATES` | `300` | Maximum index candidates checked, bounded to 1,000 |
| `TORRENT_SEARCH_URL` | `https://addon.peerflix.mov` | HTTPS base of the torrent search add-on |
| `PUBLIC_BASE_URL` | `RENDER_EXTERNAL_URL` | Public origin for signed playback links; Render supplies its URL automatically |
| `PORT` | `7100` | HTTP port; Render overrides it |
| `MAX_STREAMS` | `60` | Combined budget for DMM Cast results |
| `PROVIDER_TIMEOUT` | `14` | Network-call timeout in seconds |
| `SOFT_DEADLINE` | `14` | Aggregation response budget in seconds |
| `CACHE_TTL` | `180` | Result-cache lifetime in seconds |
| `TMDB_API_KEY` | unset | Optional metadata fallback; Cinemeta provides the normal key-free mapping |
| `DISABLED_PROVIDERS` | unset | Comma-separated provider names: dmm,torbox |

## Verification

`GET /health` reports the version, enabled providers and the TorBox provider's last status/count, without keys. `GET /stream/movie/{imdb}.json` returns movie streams; series use `/stream/series/{imdb}:{season}:{episode}.json`.

Run regression tests with `python -m unittest discover -s tests -v`. Tests cover more than five cached entries, no additions during discovery, expired/tampered playback links, correct TorBox file IDs, series matching, cache disappearance, provider failure isolation, and bounded aggregation waits.

Upstream documentation: [TorBox API](https://api-docs.torbox.app/), [Torrentio source](https://github.com/TheBeastLT/torrentio-scraper), [DMM source](https://github.com/debridmediamanager/debrid-media-manager).
