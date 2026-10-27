"""TorBox Plus: independent cache-only torrent search for Nuvio/Stremio.

Set TORBOX_API_KEY in the environment, then run python addon.py.
Search indexes return hashes; only cache-confirmed files become playable links.
"""

import concurrent.futures
import json
import ipaddress
import os
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from torbox_provider import (InvalidPlayLink, ProviderError, TorBoxProvider,
                             basename, file_identity, release_tags, quality as release_quality)

# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "7100"))

PROVIDER_TIMEOUT = float(os.environ.get("PROVIDER_TIMEOUT", "14"))
SOFT_DEADLINE = float(os.environ.get("SOFT_DEADLINE", "14"))
CACHE_TTL = int(os.environ.get("CACHE_TTL", "180"))
PREFER_ENGLISH = os.environ.get("PREFER_ENGLISH", "true").lower() not in ("false", "0", "no")
HIDE_CAM = os.environ.get("HIDE_CAM", "false").lower() in ("true", "1", "yes")
MAX_FILE_SIZE_GB = max(0, float(os.environ.get("MAX_FILE_SIZE_GB", "0")))
TORBOX_API_KEY = os.environ.get("TORBOX_API_KEY", "").strip()
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", os.environ.get("RENDER_EXTERNAL_URL", "")).rstrip("/")
TORBOX_MAX_STREAMS = max(1, min(200, int(os.environ.get("TORBOX_MAX_STREAMS", "100"))))
TORBOX = TorBoxProvider(
    TORBOX_API_KEY, timeout=PROVIDER_TIMEOUT, deadline=SOFT_DEADLINE,
    max_candidates=int(os.environ.get("TORBOX_MAX_CANDIDATES", "300")),
    search_base=os.environ.get("TORRENT_SEARCH_URL", "https://addon.peerflix.mov"),
    public_base=PUBLIC_BASE_URL,
    enable_knaben=os.environ.get("KNABEN_ENABLED", "true").lower() not in ("false", "0", "no"),
)
DISABLED = {
    p.strip().lower()
    for p in os.environ.get("DISABLED_PROVIDERS", "").split(",")
    if p.strip()
}


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------


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


PROVIDERS = {
    "torbox": TORBOX.streams,
}

# --------------------------------------------------------------------------
# aggregation
# --------------------------------------------------------------------------

_cache = {}
_cache_lock = threading.Lock()
_provider_pool = concurrent.futures.ThreadPoolExecutor(max_workers=16, thread_name_prefix="provider")


def cache_get(key):
    with _cache_lock:
        hit = _cache.get(key)
    if not hit:
        return None
    value, expiry = hit
    if expiry < time.monotonic():
        with _cache_lock:
            _cache.pop(key, None)
        return None
    return value


def cache_put(key, value):
    with _cache_lock:
        _cache[key] = (value, time.monotonic() + (CACHE_TTL if value else min(10, CACHE_TTL)))
        if len(_cache) > 512:
            now = time.monotonic()
            for k in [k for k, (_, e) in _cache.items() if e < now]:
                _cache.pop(k, None)
            while len(_cache) > 512:
                _cache.pop(next(iter(_cache)))


def rank(stream):
    q = stream.get("quality") or quality_from(stream.get("name", "") + stream.get("title", ""))
    try:
        qidx = QUALITY_ORDER.index(q)
    except ValueError:
        qidx = len(QUALITY_ORDER) - 1
    text = str(stream.get("_filename") or stream.get("title", ""))
    tags = release_tags(text)
    size = stream.get("_bytes", 0) / 1024**3 or size_gb(stream.get("name", "") + stream.get("title", "")) or 0.0
    return (0 if stream.get("url") else 1,
            stream.get("_languageRank", tags["languageRank"]) if PREFER_ENGLISH else 0,
            1 if stream.get("_cam", tags["cam"]) else 0, qidx, size)


def dedupe_streams(merged):
    seen_hashes, seen_files, seen_urls, out = set(), set(), set(), []
    # Prefer a cache-verified direct TorBox link when the same file is cast too.
    for stream in sorted(merged, key=lambda s: 0 if s.get("source") == "TorBox" else 1):
        filename = file_identity(stream.get("_filename", ""))
        infohash = stream.get("_torrentHash") or stream.get("infoHash")
        hash_key = (infohash, filename or stream.get("fileIdx")) if infohash else None
        file_key = (filename, stream.get("_bytes")) if filename and stream.get("_bytes") else None
        url = stream.get("url") or stream.get("externalUrl")
        if not url and not infohash:
            continue
        if (hash_key and hash_key in seen_hashes) or (file_key and file_key in seen_files) or (url and url in seen_urls):
            continue
        tags = release_tags(str(stream.get("_filename") or stream.get("title", "")))
        if HIDE_CAM and stream.get("_cam", tags["cam"]):
            continue
        size = stream.get("_bytes", 0) / 1024**3 or size_gb(stream.get("name", "")) or 0
        if MAX_FILE_SIZE_GB and size > MAX_FILE_SIZE_GB:
            continue
        if hash_key:
            seen_hashes.add(hash_key)
        if file_key:
            seen_files.add(file_key)
        if url:
            seen_urls.add(url)
        out.append(stream)
    return out


def collect(imdb_id, media_type, season, episode):
    """Fan out to every provider, merge, dedupe, sort, cap."""
    key = (imdb_id, media_type, season, episode)
    cached = cache_get(key)
    if cached is not None:
        return cached

    deadline = time.monotonic() + SOFT_DEADLINE
    jobs = {}

    for name, fn in PROVIDERS.items():
        if name in DISABLED or (name == "torbox" and not TORBOX_API_KEY):
            continue
        jobs[name] = _provider_pool.submit(fn, imdb_id, media_type, season, episode)

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
            merged.extend(rows)
            sys.stderr.write("provider %s: %d results\n" % (name, len(rows)))
        except Exception as exc:
            # API credentials and upstream URLs must never appear in logs.
            detail = str(exc) if isinstance(exc, ProviderError) else type(exc).__name__
            sys.stderr.write("provider %s: %s\n" % (name, detail))
    deduped = dedupe_streams(merged)

    deduped.sort(key=rank)
    capped = deduped[:TORBOX_MAX_STREAMS]
    cache_put(key, capped)
    return capped


# --------------------------------------------------------------------------
# server
# --------------------------------------------------------------------------

MANIFEST = {
    "id": "org.dmmplus.aggregator",
    "version": "2.0.1",
    "name": "TorBox Plus",
    "description": (
        "Independent cached TorBox streams from Peerflix and Knaben. "
        "No DMM Cast dependency or five-result limit. Clean labels, duplicate removal and English-first sorting."
    ),
    "logo": "https://torbox.app/favicon.ico",
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
                # Render sets X-Forwarded-For. Use the first valid public client
                # address for CDN selection, falling back to the socket peer.
                addresses = self.headers.get("X-Forwarded-For", "").split(",") + [self.client_address[0]]
                viewer_ip = None
                for candidate in addresses:
                    try:
                        address = ipaddress.ip_address(candidate.strip())
                        if address.is_global:
                            viewer_ip = str(address)
                            break
                    except ValueError:
                        continue
                link = TORBOX.resolve(path[len("/torbox/play/"):], user_ip=viewer_ip)
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
                          "lastCount": TORBOX.last_count, "lastChecked": TORBOX.last_checked,
                          "searchSources": TORBOX.last_sources},
                "preferences": {"preferEnglish": PREFER_ENGLISH, "hideCam": HIDE_CAM,
                                "maxFileSizeGB": MAX_FILE_SIZE_GB}})

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
            stream = {k: v for k, v in stream.items() if not k.startswith("_")}
            if str(stream.get("url", "")).startswith("/torbox/play/"):
                stream = dict(stream, url=base + stream["url"])
            result.append(stream)
        return self.send_json({"streams": result})


def main():
    server = Server((HOST, PORT), Handler)
    sys.stderr.write(
        "TorBox Plus on http://%s:%d/manifest.json\n"
        "  torbox    : %s\n"
        "  providers : %s\n"
        "  max streams: %d\n"
        % (HOST, PORT, "configured" if TORBOX_API_KEY else "not configured",
           ", ".join(p for p in PROVIDERS if p not in DISABLED), TORBOX_MAX_STREAMS)
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
