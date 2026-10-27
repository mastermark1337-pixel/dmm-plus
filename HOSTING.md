# Render deployment

The Docker service builds `addon.py` and `torbox_provider.py` from this repository.

1. Keep the existing `TORBOX_API_KEY` environment value. No DMM token is required.
2. Deploy the latest commit from `main`.
3. Check `/health`: version `2.0.1`, provider list `torbox`, and `torbox.configured: true`.
4. Search a title and inspect `/health` for per-index success/errors and cached-file counts.
5. In Nuvio, install or refresh `https://dmm-plus.onrender.com/manifest.json`. Select **TorBox Plus**. The original **DMM Cast for TorBox** entry is a different add-on.

Render automatically supplies `PORT` and `RENDER_EXTERNAL_URL`. Public playback links are signed and expire. Viewer IP is used when choosing TorBox's CDN; minted links are cached separately by viewer.

Knaben and Peerflix are independent public indexes. Search results are filtered to cached playable TorBox files. Neither index receives the TorBox key. A source outage is reported and the other source can still provide results. Empty searches have a short cache lifetime to allow recovery.

Optional preferences: `PREFER_ENGLISH=true`, `HIDE_CAM=false`, `MAX_FILE_SIZE_GB=0`, and `TORBOX_MAX_STREAMS=100` (maximum 200). The free Render instance can sleep when idle, delaying its first request.
