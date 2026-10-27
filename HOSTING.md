# Render deployment

The Docker image installs the pinned cryptography dependency and includes the Python server plus the setup page's HTML, CSS and JavaScript.

1. Save a persistent Fernet key in Render as `CONFIG_SECRET`. Generate it once and retain it across redeployments. Changing it invalidates personal links.
2. Deploy the latest commit from `main`.
3. Check `/health`: version `3.0.0`, `personalLinksEnabled: true`.
4. Open `/configure`, enter your own TorBox API key, and copy the personal manifest URL into Nuvio.
5. Share only `https://dmm-plus.onrender.com/configure` with friends. Every person uses their own key.

The bare manifest no longer grants the server owner's streams. Existing bare-URL installations must be migrated to a personal link. `TORBOX_API_KEY` is not used as a public-account fallback.

Render provides `PORT` and `RENDER_EXTERNAL_URL` automatically. No account database or persistent disk is needed: personal URLs contain authenticated encrypted configuration. In-memory instances are bounded and caches are tenant-specific. Application access logs redact config and playback tokens. Keep personal links private because they grant access through this add-on.

The setup form validates keys only against TorBox, limits request size, rate-limits attempts, and rejects cross-origin form submissions. Page assets contain no credentials. API keys never go to Peerflix or Knaben. Optional preferences are encrypted into each install link.

Viewer IP is passed to TorBox when selecting the CDN. Successful playback URLs are cached separately per account, file and viewer. The free Render instance can sleep when idle, delaying initial requests.
