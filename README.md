# TorBox Plus 3.0 — personal installation links

Independent Nuvio/Stremio add-on using each viewer's own TorBox account. Peerflix and Knaben supply search candidates; only matching files confirmed cached on TorBox become playable links. No DMM Cast dependency or five-result limit.

## Share with friends

Send **https://dmm-plus.onrender.com/configure**. Each person enters their own TorBox API key, chooses optional preferences and copies their personal manifest URL into Nuvio's Addons settings.

Personal manifest URLs are private bearer links: someone who has one can use that account through the add-on. Share the setup page, not a personal manifest. TorBox API keys are encrypted and authenticated in installation links using Fernet. They are not stored in an account database. The server decrypts a key when serving that person's requests, and retains a bounded number of account clients in memory. Only TorBox receives the key from this server; the search indexes receive title/IMDb identifiers.

The bare `/manifest.json` requires configuration and cannot stream using the server owner's account. Existing installations using that bare URL must install a personal URL. Old public playback paths are no longer accepted.

## Behaviour

- Cached files only. Browsing never adds torrents. Selecting a file performs a cache-only add, then maps the actual TorBox file ID and chooses a CDN for the viewer. Added cached torrents remain in that user's library.
- Search/result/playback caches are separated by encrypted installation identity. Keys, personal config tokens and playback grants are redacted from application access logs.
- Filenames drive resolution/codec/HDR/release labels. English-labelled releases are preferred, unknown language remains visible, and CAM/TS copies are demoted. Exact duplicate files, including copy-number suffixes, are collapsed using filename and byte size.
- TV packs are offered only when the requested episode is identified unambiguously in the cached file listing. Upstream file indexes are never used as TorBox file IDs.
- One failed index does not hide results from the other. Counts vary with available releases and cache coverage.
- UKTurks stays separate.

## Server configuration

`CONFIG_SECRET` is required for personal links. Generate it once with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` and save it privately in the host's environment. Keep it stable across deploys; replacing it invalidates all existing personal installation links. Never commit this secret or account keys.

| Variable | Default | Purpose |
| --- | --- | --- |
| `CONFIG_SECRET` | unset | Persistent Fernet encryption key for personal links |
| `PORT` | `7100` | HTTP port; supplied by Render |
| `PUBLIC_BASE_URL` | `RENDER_EXTERNAL_URL` | Public HTTPS origin |
| `TORBOX_MAX_CANDIDATES` | `300` | Maximum search candidates checked, bounded to 1,000 |
| `KNABEN_ENABLED` | `true` | Enable Knaben alongside the primary index |
| `TORRENT_SEARCH_URL` | `https://addon.peerflix.mov` | Primary compatible HTTPS index |
| `PROVIDER_TIMEOUT` | `14` | Per-call network timeout |
| `SOFT_DEADLINE` | `14` | Search and aggregation response budget |
| `CACHE_TTL` | `180` | Successful search cache lifetime; empty results expire within 10 seconds |

Each personal link carries its own preferences: prefer English, hide CAM, maximum results (1–200), and a file-size limit. `TORBOX_API_KEY` is not needed for shared deployments. There is no public account fallback.

## Development

Python 3.12 recommended. Install `pip install -r requirements.txt`, then run `python addon.py`. Run tests with `python -m unittest discover -s tests -v`.

Public routes: `/configure`, `/api/configure` (POST), `/manifest.json`, `/health`. Personal routes live under `/c/{encrypted-config}/`: `manifest.json`, `stream/movie/{imdb}.json`, `stream/series/{imdb}:{season}:{episode}.json`, `health`, and signed `torbox/play/...` paths.

Account revocation: reset the API key in TorBox and generate a new installation link. There is no central user-account database or per-link revoke list. The encrypted key remains accessible to the server operator; this is server-side encryption, not end-to-end encryption.

Documentation: [TorBox API](https://api-docs.torbox.app/), [Fernet](https://cryptography.io/en/latest/fernet/), [Knaben API](https://knaben.org/api/v2/), [Stremio protocol](https://stremio.github.io/stremio-addon-sdk/protocol.html).
