# Hosted deployment

This addon is a normal HTTP server: no disk writes, no background state beyond an
in-memory cache, no dependencies. Any container host works. Stdlib only — the
image is ~60 MB and builds in seconds.

Pick **one** of these.

## Render (free tier, no card)

1. Push the `dmm-plus` folder to a repo.
2. Render → **New → Web Service** → connect the repo.
3. Runtime **Docker** (it finds the `Dockerfile` automatically).
4. Environment:

   ```
   DMM_TOKEN=_oKCkbeH_FFa
   ```

5. Deploy. Your addon URL:

   ```
   https://<service-name>.onrender.com/manifest.json
   ```

Free tier sleeps after 15 min idle; the first request after that takes ~30 s.
Paid instances stay warm.

## Railway

```bash
railway init
railway up
railway variables --set DMM_TOKEN=_oKCkbeH_FFa
railway domain
```

Railway injects `PORT`; the server already reads it. No Dockerfile needed, but it
picks it up anyway.

## Fly.io

```toml
# fly.toml
app = "dmm-plus"
primary_region = "lhr"

[build]
  dockerfile = "Dockerfile"

[http_service]
  internal_port = 7100
  force_https = true
  auto_stop_machines = "stop"
  auto_start_machines = true
  min_machines_running = 0

[env]
  PORT = "7100"
```

```bash
fly launch --no-deploy
fly secrets set DMM_TOKEN=_oKCkbeH_FFa
fly deploy
```

`auto_stop_machines = "stop"` keeps it at roughly zero cost; cold start is ~2 s.

## Any VPS / Raspberry Pi / NAS with Docker

```bash
docker build -t dmm-plus .
docker run -d --name dmm-plus -p 7100:7100 \
  -e DMM_TOKEN=_oKCkbeH_FFa \
  --restart unless-stopped \
  dmm-plus
```

Put Caddy or nginx in front for TLS. Caddy is one line:

```
dmm.example.com {
    reverse_proxy 127.0.0.1:7100
}
```

## PythonAnywhere (no Docker)

Upload `addon.py`, then in a *Web* app with a **manual config** WSGI setup, or
simply run it as an always-on task:

```
python3 addon.py
```

PythonAnywhere's free tier allows one always-on task; only outbound HTTP is
available on free accounts, which is all this needs.

## Environment variables

| Var | Default | Set it because |
|---|---|---|
| `DMM_TOKEN` | `_oKCkbeH_FFa` | **required in practice** — this is your library |
| `PORT` | `7100` | hosts inject their own; already read |
| `MAX_STREAMS` | `60` | cap on the merged list |
| `SOFT_DEADLINE` | `14` | lower it on a slow box; fewer CHQ entries |
| `PROVIDER_TIMEOUT` | `14` | per-provider network ceiling |
| `CACHE_TTL` | `180` | signed URLs expire; keep short |
| `DISABLED_PROVIDERS` | — | `vidsrc` saves one wasted request |

## Operational notes

- **The DMM token is in the URL path.** Hosting makes the addon public unless you
  put auth in front. Anyone with the URL can read your DMM cast list. Add basic
  auth at the proxy (Caddy `basicauth`, nginx `auth_basic`) or a long random
  path prefix if that matters.
- **No inbound scraping.** This is outbound-only HTTP; no ports other than the
  one HTTP listener, no database, no volume.
- **Cold starts cost streams.** CHQ answers in ~10 s; if the platform's proxy
  times out at 10–15 s, raise `SOFT_DEADLINE` or drop CHQ with
  `DISABLED_PROVIDERS=chq` and keep the faster four.
- **Live TV is not here.** That catalog belongs to the `ukturks-stremio` addon,
  which hosts the same way if you want it back.
- **Health check**: `GET /health` → `{"status":"ok","providers":[...]}`.
