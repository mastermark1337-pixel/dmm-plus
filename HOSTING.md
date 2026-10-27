# Render deployment

Use the existing Docker web service. Its repository must contain `addon.py`, `torbox_provider.py`, and `Dockerfile` at the root.

1. Keep the existing `DMM_TOKEN` environment variable.
2. Add `TORBOX_API_KEY` from your TorBox account settings. Enter it in Render's Environment page; never commit it to GitHub.
3. Save the environment and deploy the latest commit from `main`.
4. Check `/health`: version must be `1.1.2` and `torbox.configured` must be `true`.
5. Open a title, then check `torbox.lastStatus` and `torbox.lastCount` in `/health`.

Render supplies `PORT` and `RENDER_EXTERNAL_URL`. The manifest remains at `https://dmm-plus.onrender.com/manifest.json`; Nuvio does not need a replacement URL. Refresh the title's stream list after deployment.

`TORBOX_MAX_STREAMS` defaults to 100 and is separate from the existing 60-entry hoster/DMM budget. Results are restricted to files confirmed cached by TorBox. No DMM sponsor key is required for the independent torrent provider.

If `torbox.lastStatus` contains `401`, `403`, or `BAD_TOKEN`, check the TorBox account key. `429` means wait for TorBox's rate-limit cooldown. An `ok` status with zero results means the index/cached file match produced no playable choices for that title; a higher limit cannot manufacture missing cached files.

The free Render instance may sleep when inactive. The first request after sleep can be delayed while it starts.
