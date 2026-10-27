"""DMM Cast, independent cache-only TorBox search, and hoster aggregation.

Set DMM_TOKEN and TORBOX_API_KEY in the environment, then run python addon.py.
Independent TorBox results do not depend on the upstream Cast stream budget.
"""

import concurrent.futures
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from torbox_provider import InvalidPlayLink, ProviderError, TorBoxProvider, quality as release_quality

# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "7100"))

DMM_TOKEN = os.environ.get("DMM_TOKEN", "").strip()
DMM_BASE = f"https://debridmediamanager.com/api/stremio-tb/{DMM_TOKEN}"

TMDB_KEY = os.environ.get("TMDB_API_KEY", "").strip()

PROVIDER_TIMEOUT = float(os.environ.get("PROVIDER_TIMEOUT", "14"))
SOFT_DEADLINE = float(os.environ.get("SOFT_DEADLINE", "14"))
CACHE_TTL = int(os.environ.get("CACHE_TTL", "180"))
MAX_STREAMS = int(os.environ.get("MAX_STREAMS", "60"))
TORBOX_API_KEY = os.environ.get("TORBOX_API_KEY", "").strip()
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", os.environ.get("RENDER_EXTERNAL_URL", "")).rstrip("/")
TORBOX_MAX_STREAMS = max(1, min(200, int(os.environ.get("TORBOX_MAX_STREAMS", "100"))))
TORBOX = TorBoxProvider(
    TORBOX_API_KEY, timeout=PROVIDER_TIMEOUT, deadline=SOFT_DEADLINE,
    max_candidates=int(os.environ.get("TORBOX_MAX_CANDIDATES", "300")),
    search_base=os.environ.get("TORRENT_SEARCH_URL", "https://torrentio.strem.fun"),
    public_base=PUBLIC_BASE_URL,
)
DISABLED = {
    p.strip().lower()
    for p in os.environ.get("DISABLED_PROVIDERS", "").split(",")
    if p.strip()
}

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)

CHQ_API = "https://chq-api.ddns.me/api/streams/{tmdb}{se}"
PRIMESRC_API = "https://primesrc.me/api/v1"
VSEMBED = "https://vsembed.ru"
TWOEMBED = "https://www.2embed.online"

# DMM's own hosts are GFW-blocked behind Cloudflare in some networks; TLS
# verification is relaxed for the embed CDNs only, matching what the hosters
# themselves do to the app.
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------


def http(url, headers=None, method="GET", data=None, timeout=PROVIDER_TIMEOUT,
         form=None, json_body=None):
    """Return (ok, text, status). Never raises."""
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    body = data
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        h.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif json_body is not None:
        body = json.dumps(json_body).encode()
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
            return True, r.read().decode("utf-8", "ignore"), r.status
    except urllib.error.HTTPError as e:
        try:
            return False, e.read().decode("utf-8", "ignore"), e.code
        except Exception:
            return False, "", e.code
    except Exception:
        return False, "", 0


def absolute(url, base=""):
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/") and base:
        return base.rstrip("/") + url
    return url


def origin(url):
    m = re.match(r"^https?://[^/]+", url or "")
    return m.group(0) if m else ""


# --------------------------------------------------------------------------
# helpers shared by providers
# --------------------------------------------------------------------------

QUALITY_ORDER = ["4K", "1440p", "1080p", "720p", "576p", "480p", "360p", "Auto"]


def quality_from(text):
    resolved = release_quality(text)
    if resolved != "Auto":
        return resolved
    t = (text or "").lower()
    if "2160" in t or re.search(r"\b(?:4k|uhd)\b", t):
        return "4K"
    if "1080" in t or "fhd" in t:
        return "1080p"
    if "720" in t:
        return "720p"
    if "480" in t or " sd" in t:
        return "480p"
    if "360" in t:
        return "360p"
    return "Auto"


def size_gb(title):
    """Pull a size label out of a release title ('104.92 GB' -> 104.92)."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*(GB|MB)\b", title or "", re.I)
    if not m:
        return None
    value = float(m.group(1))
    return value if m.group(2).upper() == "GB" else value / 1024.0


def norm(tmdb, media_type, season, episode):
    if media_type == "tv" and season is not None and episode is not None:
        return f"/{season}/{episode}"
    return ""


# --------------------------------------------------------------------------
# DMM (Debrid Media Manager) -- passthrough, entries kept verbatim
# --------------------------------------------------------------------------


def dmm(imdb_id, media_type, season, episode):
    if not DMM_TOKEN:
        return []
    if media_type == "tv":
        path = f"/stream/series/{imdb_id}:{season}:{episode}.json"
    else:
        path = f"/stream/movie/{imdb_id}.json"
    ok, text, _ = http(DMM_BASE + path, timeout=PROVIDER_TIMEOUT)
    if not ok:
        return []
    try:
        payload = json.loads(text)
    except ValueError:
        return []

    out = []
    for s in payload.get("streams", []):
        entry = {
            "name": s.get("name", "DMM"),
            "title": s.get("title", ""),
            "source": "DMM",
        }
        if s.get("url"):
            entry["url"] = s["url"]
        if s.get("externalUrl"):
            entry["externalUrl"] = s["externalUrl"]
        if s.get("behaviorHints"):
            entry["behaviorHints"] = s["behaviorHints"]
        if s.get("infoHash"):
            entry["infoHash"] = s["infoHash"]
        if "fileIdx" in s:
            entry["fileIdx"] = s["fileIdx"]
        out.append(entry)
    return out


# --------------------------------------------------------------------------
# CHQ
# --------------------------------------------------------------------------

DOWNLOAD_ONLY = ["10gbps", "pixeldrain", "hubcloud.ist", "gofile", "1fichier",
                 "buzzheavier", "pixel.hubcloud"]


def chq(tmdb, media_type, season, episode):
    url = CHQ_API.format(tmdb=tmdb, se=norm(tmdb, media_type, season, episode))
    ok, text, _ = http(url, timeout=PROVIDER_TIMEOUT)
    if not ok:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return []
    out = []
    for s in data.get("streams", []):
        u = s.get("url") or ""
        if not u.startswith("http"):
            continue
        if any(d in u.lower() for d in DOWNLOAD_ONLY):
            continue
        title = (s.get("title") or "").replace("\n", " ").strip()
        out.append({
            "name": f"CHQ - {quality_from(title)}",
            "title": title or s.get("provider", "CHQ"),
            "url": u,
            "quality": quality_from(title),
            "source": "CHQ",
            "headers": {"User-Agent": UA},
        })
    return out


# --------------------------------------------------------------------------
# PrimeSrc -- API now returns {info, servers}
# --------------------------------------------------------------------------


def primesrc(tmdb, media_type, season, episode):
    if media_type == "tv" and season is not None and episode is not None:
        q = f"tmdb={tmdb}&season={season}&episode={episode}&type=tv"
    else:
        q = f"tmdb={tmdb}&type=movie"
    ok, text, _ = http(f"{PRIMESRC_API}/s?{q}", timeout=PROVIDER_TIMEOUT)
    if not ok:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return []
    servers = data.get("servers") if isinstance(data, dict) else data
    if not isinstance(servers, list):
        return []
    out = []
    for s in servers[:20]:
        key = s.get("key")
        if not key:
            continue
        out.append({
            "name": f"PrimeSrc - {s.get('name', 'hoster')}",
            "title": s.get("file_name", "") or "PrimeSrc",
            "url": f"https://primesrc.me/embed/{key}",
            "quality": quality_from(s.get("file_name", "")),
            "source": "PrimeSrc",
            "headers": {"Referer": "https://primesrc.me/", "User-Agent": UA},
        })
    return out


# --------------------------------------------------------------------------
# 2Embed -- embed page sets srcUrl, player ships a plain sources JSON block
# --------------------------------------------------------------------------


def twoembed(tmdb, media_type, season, episode):
    base = TWOEMBED
    if media_type == "tv":
        embed = f"{base}/embed/tv/{tmdb}/{season}/{episode}"
    else:
        embed = f"{base}/embed/movie/{tmdb}"
    hdr = {"Referer": base + "/"}
    ok, html, _ = http(embed, headers=hdr, timeout=PROVIDER_TIMEOUT)
    if not ok:
        return []
    m = re.search(r'srcUrl\s*=\s*["\']([^"\']+)["\']', html)
    if not m:
        return []
    player = m.group(1).replace("\\/", "/")
    ok, page, _ = http(player, headers={"Referer": base + "/"},
                       timeout=PROVIDER_TIMEOUT)
    if not ok:
        return []
    out = []
    pat = re.compile(
        r'"url":"(https?://[^"]+?)","file":"[^"]*","type":"([^"]*)","label":"([^"]*)"'
    )
    for url, kind, label in pat.findall(page):
        out.append({
            "name": f"2Embed - {label or 'hoster'}",
            "title": label or "2Embed",
            "url": url.replace("\\/", "/"),
            "quality": quality_from(label),
            "source": "2Embed",
            "headers": {"Referer": base + "/", "User-Agent": UA},
        })
    return out


# --------------------------------------------------------------------------
# MultiEmbed -- body is the playback URL itself
# --------------------------------------------------------------------------


def multiembed(tmdb, media_type, season, episode):
    link = f"https://getsuperembed.link/?video_id={tmdb}&tmdb=1"
    if media_type == "tv":
        link += f"&s={season}&e={episode}"
    ok, body, _ = http(link, headers={"Referer": "https://multiembed.mov/"},
                       timeout=PROVIDER_TIMEOUT)
    if not ok:
        return []
    body = body.strip()
    urls = []
    if body.startswith("http") and "\n" not in body and len(body) < 2048:
        urls.append(body)
    else:
        urls = re.findall(
            r'["\'](https?://(?:streamingnow\.mov|getsuperembed\.link)[^"\']+)["\']',
            body,
        )
    return [{
        "name": "MultiEmbed",
        "title": "MultiEmbed",
        "url": u.replace("\\/", "/"),
        "quality": "Auto",
        "source": "MultiEmbed",
        "headers": {"Referer": "https://streamingnow.mov/", "User-Agent": UA},
    } for u in urls]


# --------------------------------------------------------------------------
# VidSrc -- data-api -> vs_src.php -> CFG.playerUrl -> CONFIG.api
# The final call is bot-gated (403) from many networks; returns [] there.
# --------------------------------------------------------------------------


def vidsrc(tmdb, media_type, season, episode):
    hdr = {"Referer": VSEMBED + "/"}
    if media_type == "tv":
        start = f"{VSEMBED}/embed/tv?tmdb={tmdb}&season={season}&episode={episode}"
    else:
        start = f"{VSEMBED}/embed/movie?tmdb={tmdb}"

    ok, html, _ = http(start, headers=hdr, timeout=PROVIDER_TIMEOUT)
    if not ok:
        return []

    api = re.search(r'data-api=["\']([^"\']+)["\']', html)
    if not api:
        direct = re.search(r'["\'](https?://[^"\']+\.m3u8[^"\']*)["\']', html)
        if direct:
            return [{
                "name": "VidSrc",
                "title": "VidSrc",
                "url": direct.group(1),
                "quality": quality_from(direct.group(1)),
                "source": "VidSrc",
                "headers": hdr,
            }]
        return []

    api_path = api.group(1).replace("&amp;", "&")
    ok, body, _ = http(absolute(api_path, VSEMBED), headers=hdr,
                       timeout=PROVIDER_TIMEOUT)
    if not ok:
        return []
    try:
        page_url = json.loads(body).get("src")
    except (ValueError, AttributeError):
        return []
    if not page_url:
        return []

    host = origin(page_url)
    ok, page, _ = http(page_url, headers={"Referer": VSEMBED + "/",
                                          "User-Agent": UA},
                       timeout=PROVIDER_TIMEOUT)
    if not ok:
        return []

    cfg = re.search(r"window\.CFG\s*=\s*(\{.*?\});", page, re.S)
    if not cfg:
        m3u = re.search(r'["\'](https?://[^"\']+\.m3u8[^"\']*)["\']', page)
        if m3u:
            return [{
                "name": "VidSrc",
                "title": "VidSrc",
                "url": m3u.group(1),
                "quality": quality_from(m3u.group(1)),
                "source": "VidSrc",
                "headers": {"Referer": page_url, "User-Agent": UA},
            }]
        return []
    try:
        cfg_obj = json.loads(cfg.group(1))
    except ValueError:
        return []

    player = absolute(cfg_obj.get("playerUrl", ""), host)
    if not player:
        return []

    ok, player_html, _ = http(player, headers={"Referer": page_url,
                                               "User-Agent": UA},
                              timeout=PROVIDER_TIMEOUT)
    if not ok:
        return []
    conf = re.search(r"window\.CONFIG\s*=\s*(\{.*?\});", player_html, re.S)
    if not conf:
        return []
    try:
        conf_obj = json.loads(conf.group(1))
    except ValueError:
        return []
    api_url = conf_obj.get("api")
    if not api_url:
        return []

    ok, final, _ = http(api_url, headers={"Referer": player, "Accept": "application/json",
                                          "User-Agent": UA},
                        timeout=PROVIDER_TIMEOUT)
    if not ok:
        return []

    out = []
    seen = set()
    for url in re.findall(r'"(?:url|file)"\s*:\s*"(https?://[^"]+?\.m3u8[^"]*)"', final):
        url = url.replace("\\/", "/")
        if url in seen:
            continue
        seen.add(url)
        out.append({
            "name": "VidSrc",
            "title": "VidSrc",
            "url": url,
            "quality": quality_from(url),
            "source": "VidSrc",
            "headers": {"Referer": page_url, "User-Agent": UA},
        })
    return out


# --------------------------------------------------------------------------
# TMDB -> imdb (CHQ and the hosters key on TMDB, DMM keys on IMDb, so the
# IMDb id comes from the request path itself; this is only a fallback)
# --------------------------------------------------------------------------


def tmdb_from_imdb(imdb_id, media_type):
    """IMDb -> TMDB numeric id. The hosters key on TMDB, DMM keys on IMDb."""
    kind_slug = "series" if media_type == "tv" else "movie"
    ok, text, _ = http(f"https://v3-cinemeta.strem.io/meta/{kind_slug}/{imdb_id}.json",
                       timeout=PROVIDER_TIMEOUT)
    if ok:
        try:
            value = json.loads(text).get("meta", {}).get("moviedb_id")
            if value is not None and str(value).isdigit() and int(value) > 0:
                return int(value)
        except (ValueError, AttributeError, TypeError):
            pass
    if not TMDB_KEY:
        return None
    kind = "tv_results" if media_type == "tv" else "movie_results"
    ok, text, _ = http(
        "https://api.themoviedb.org/3/find/"
        f"{imdb_id}?external_source=imdb_id&api_key={TMDB_KEY}",
        timeout=PROVIDER_TIMEOUT,
    )
    if not ok:
        return None
    try:
        results = json.loads(text).get(kind) or []
    except ValueError:
        return None
    return results[0]["id"] if results else None


PROVIDERS = {
    "dmm": dmm,
    "torbox": TORBOX.streams,
    "chq": chq,
    "primesrc": primesrc,
    "twoembed": twoembed,
    "multiembed": multiembed,
    "vidsrc": vidsrc,
}

# --------------------------------------------------------------------------
# aggregation
# --------------------------------------------------------------------------

_cache = {}
_cache_lock = threading.Lock()
_provider_pool = concurrent.futures.ThreadPoolExecutor(max_workers=16, thread_name_prefix="provider")
_metadata_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="metadata")


def cache_get(key):
    with _cache_lock:
        hit = _cache.get(key)
    if not hit:
        return None
    value, expiry = hit
    if expiry < time.time():
        with _cache_lock:
            _cache.pop(key, None)
        return None
    return value


def cache_put(key, value):
    with _cache_lock:
        _cache[key] = (value, time.time() + CACHE_TTL)
        if len(_cache) > 512:
            now = time.time()
            for k in [k for k, (_, e) in _cache.items() if e < now]:
                _cache.pop(k, None)


def rank(stream):
    q = stream.get("quality") or quality_from(stream.get("name", "") + stream.get("title", ""))
    try:
        qidx = QUALITY_ORDER.index(q)
    except ValueError:
        qidx = len(QUALITY_ORDER) - 1
    size = size_gb(stream.get("title", "")) or 0.0
    # Quality first, then smaller files (bigger isn't better on a debrid box
    # that has to stream a 100 GB remux over wifi).
    return (qidx, 0 if stream.get("url") else 1, size)


def collect(imdb_id, media_type, season, episode):
    """Fan out to every provider, merge, dedupe, sort, cap."""
    key = (imdb_id, media_type, season, episode)
    cached = cache_get(key)
    if cached is not None:
        return cached

    deadline = time.monotonic() + SOFT_DEADLINE
    jobs = {}

    def lookup_tmdb():
        tmdb = cache_get(("tmdb", imdb_id, media_type))
        if tmdb is None:
            tmdb = tmdb_from_imdb(imdb_id, media_type)
            if tmdb:
                cache_put(("tmdb", imdb_id, media_type), tmdb)
        return tmdb

    # Direct IMDb providers start immediately, even if metadata is unavailable.
    metadata = _metadata_pool.submit(lookup_tmdb)

    def with_tmdb(fn):
        tmdb = metadata.result(timeout=max(0, deadline - time.monotonic()))
        if not tmdb or time.monotonic() >= deadline:
            return []
        return fn(tmdb, media_type, season, episode)

    for name, fn in PROVIDERS.items():
        if name in DISABLED or (name == "torbox" and not TORBOX_API_KEY):
            continue
        jobs[name] = (_provider_pool.submit(fn, imdb_id, media_type, season, episode)
                      if name in ("dmm", "torbox") else _provider_pool.submit(with_tmdb, fn))

    if jobs:
        concurrent.futures.wait(jobs.values(), timeout=max(0, deadline - time.monotonic()))
    merged = []
    # Gather every completed job; a slow earlier provider must not hide later ones.
    for name, future in jobs.items():
        if not future.done():
            future.cancel()
            sys.stderr.write("provider %s: deadline_exceeded\n" % name)
            continue
        try:
            rows = future.result() or []
            if name == "torbox":
                rows = rows[:TORBOX_MAX_STREAMS]
            merged.extend(rows)
            sys.stderr.write("provider %s: %d results\n" % (name, len(rows)))
        except Exception as exc:
            # API credentials and upstream URLs must never appear in logs.
            detail = str(exc) if isinstance(exc, ProviderError) else type(exc).__name__
            sys.stderr.write("provider %s: %s\n" % (name, detail))
    metadata.cancel()

    # Dedupe on the playable URL; DMM entries also carry infoHash.
    seen = set()
    deduped = []
    for s in merged:
        ident = s.get("url") or s.get("infoHash") or s.get("externalUrl") or ""
        if not ident:
            continue
        if ident in seen:
            continue
        seen.add(ident)
        deduped.append(s)

    deduped.sort(key=rank)
    # Direct TorBox results have their own budget, so a hoster's twenty embed
    # pages cannot crowd the additional cached torrent choices out of the list.
    direct = [s for s in deduped if s.get("source") == "TorBox"][:TORBOX_MAX_STREAMS]
    others = [s for s in deduped if s.get("source") != "TorBox"][:MAX_STREAMS]
    capped = direct + others
    capped.sort(key=rank)
    cache_put(key, capped)
    return capped


# --------------------------------------------------------------------------
# server
# --------------------------------------------------------------------------

MANIFEST = {
    "id": "org.dmmplus.aggregator",
    "version": "1.1.0",
    "name": "DMM Plus",
    "description": (
        "DMM Cast plus independent TorBox cached torrent search when configured, "
        "CHQ, PrimeSrc, 2Embed, MultiEmbed and VidSrc. DMM Cast's upstream limit still applies."
    ),
    "logo": "https://static.debridmediamanager.com/dmmcast.png",
    "resources": ["stream"],
    "types": ["movie", "series"],
    "idPrefixes": ["tt"],
    "catalogs": [],
    "behaviorHints": {"adult": False, "p2p": False},
}


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Player clients drop connections mid-response constantly (seek, close,
        # range restarts). Those are not addon errors; keep the log readable.
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if self.path.startswith("/torbox/play/"):
            sys.stderr.write("%s TorBox playback request\n" % self.address_string())
            return
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        # Load balancers and uptime probes issue HEAD /; BaseHTTPRequestHandler
        # answers 501 otherwise, which some platforms read as unhealthy.
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/")

        if path.startswith("/torbox/play/"):
            if not TORBOX_API_KEY:
                return self.send_json({"error": "TorBox is not configured"}, 503)
            try:
                link = TORBOX.resolve(path[len("/torbox/play/"):])
            except InvalidPlayLink:
                return self.send_json({"error": "Play link expired or invalid; refresh the stream list"}, 410)
            except ProviderError as exc:
                return self.send_json({"error": str(exc)}, 503)
            except Exception as exc:
                sys.stderr.write("TorBox playback: %s\n" % type(exc).__name__)
                return self.send_json({"error": "TorBox playback unavailable"}, 503)
            self.send_response(302)
            self.send_header("Location", link)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            return

        if path in ("", "/", "/index"):
            return self.send_json({
                "name": MANIFEST["name"],
                "version": MANIFEST["version"],
                "addon": "/manifest.json",
                "health": "/health",
            })

        if path in ("/manifest.json", "/manifest"):
            return self.send_json(MANIFEST)

        if path in ("/health", "/healthz"):
            return self.send_json({"status": "ok", "version": MANIFEST["version"], "providers": [
                p for p in PROVIDERS if p not in DISABLED and (p != "torbox" or TORBOX_API_KEY)
            ], "torbox": {"configured": bool(TORBOX_API_KEY), "lastStatus": TORBOX.last_status,
                          "lastCount": TORBOX.last_count, "lastChecked": TORBOX.last_checked}})

        m = re.match(r"^/stream/(movie|series)/(.+)\.json$", path)
        if not m:
            return self.send_json({"error": "not found"}, 404)

        kind, ident = m.group(1), urllib.parse.unquote(m.group(2))
        media_type = "tv" if kind == "series" else "movie"
        season = episode = None

        if media_type == "tv":
            parts = ident.split(":")
            if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
                ident, season, episode = parts[0], int(parts[1]), int(parts[2])
            else:
                return self.send_json({"streams": []})

        if not re.fullmatch(r"tt\d{5,12}", ident):
            return self.send_json({"streams": []}, 400)

        try:
            streams = collect(ident, media_type, season, episode)
        except Exception as exc:  # a provider bug must not kill the addon
            sys.stderr.write("collect failed: %r\n" % (exc,))
            streams = []

        # Relative signed play links are made absolute per request, outside the
        # shared cache. Render supplies RENDER_EXTERNAL_URL automatically.
        host = self.headers.get("Host", "")
        if not re.fullmatch(r"[A-Za-z0-9.\-\[\]:]+", host):
            return self.send_json({"error": "Invalid host"}, 400)
        scheme = "https" if self.headers.get("X-Forwarded-Proto", "").split(",")[0].strip() == "https" else "http"
        base = PUBLIC_BASE_URL or f"{scheme}://{host}"
        result = []
        for stream in streams:
            if str(stream.get("url", "")).startswith("/torbox/play/"):
                stream = dict(stream, url=base + stream["url"])
            result.append(stream)
        return self.send_json({"streams": result})


def main():
    server = Server((HOST, PORT), Handler)
    sys.stderr.write(
        "DMM Plus on http://%s:%d/manifest.json\n"
        "  dmm token : %s\n"
        "  torbox    : %s\n"
        "  providers : %s\n"
        "  max streams: %d\n"
        % (HOST, PORT, "configured" if DMM_TOKEN else "not configured",
           "configured" if TORBOX_API_KEY else "not configured",
           ", ".join(p for p in PROVIDERS if p not in DISABLED), MAX_STREAMS)
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
