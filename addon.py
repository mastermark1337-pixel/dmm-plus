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
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from torbox_provider import (InvalidPlayLink, ProviderError, TorBoxProvider,
                             basename, file_identity, release_tags, quality as release_quality)
from personal_config import (ConfigError, ConfigVault, PersonalProviders, SetupLimiter,
                             clean_key, preferences as clean_preferences, validate_account)

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
CONFIG_VAULT = ConfigVault(os.environ.get("CONFIG_SECRET", ""))
SETUP_LIMITER = SetupLimiter()
ASSETS = Path(__file__).resolve().parent
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


def personal_provider(key):
    return TorBoxProvider(key, timeout=PROVIDER_TIMEOUT, deadline=SOFT_DEADLINE,
        max_candidates=int(os.environ.get("TORBOX_MAX_CANDIDATES", "300")),
        search_base=os.environ.get("TORRENT_SEARCH_URL", "https://addon.peerflix.mov"),
        enable_knaben=os.environ.get("KNABEN_ENABLED", "true").lower() not in ("false", "0", "no"))


PERSONAL_PROVIDERS = PersonalProviders(CONFIG_VAULT, personal_provider)


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


def rank(stream, preferences=None):
    q = stream.get("quality") or quality_from(stream.get("name", "") + stream.get("title", ""))
    try:
        qidx = QUALITY_ORDER.index(q)
    except ValueError:
        qidx = len(QUALITY_ORDER) - 1
    text = str(stream.get("_filename") or stream.get("title", ""))
    tags = release_tags(text)
    size = stream.get("_bytes", 0) / 1024**3 or size_gb(stream.get("name", "") + stream.get("title", "")) or 0.0
    return (0 if stream.get("url") else 1,
            stream.get("_languageRank", tags["languageRank"]) if (preferences or {}).get("preferEnglish", PREFER_ENGLISH) else 0,
            1 if stream.get("_cam", tags["cam"]) else 0, qidx, size)


def dedupe_streams(merged, preferences=None):
    settings = preferences or {}
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
        if settings.get("hideCam", HIDE_CAM) and stream.get("_cam", tags["cam"]):
            continue
        size = stream.get("_bytes", 0) / 1024**3 or size_gb(stream.get("name", "")) or 0
        size_limit = settings.get("maxFileSizeGB", MAX_FILE_SIZE_GB)
        if size_limit and size > size_limit:
            continue
        if hash_key:
            seen_hashes.add(hash_key)
        if file_key:
            seen_files.add(file_key)
        if url:
            seen_urls.add(url)
        out.append(stream)
    return out


def collect(imdb_id, media_type, season, episode, *, provider=None, preferences=None, tenant_id="default"):
    """Fan out to every provider, merge, dedupe, sort, cap."""
    key = (tenant_id, imdb_id, media_type, season, episode)
    cached = cache_get(key)
    if cached is not None:
        return cached

    deadline = time.monotonic() + SOFT_DEADLINE
    jobs = {}

    providers = {"torbox": provider.streams} if provider is not None else PROVIDERS
    configured = bool(provider.api_key) if provider is not None else bool(TORBOX_API_KEY)
    for name, fn in providers.items():
        if name in DISABLED or (name == "torbox" and not configured):
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
    deduped = dedupe_streams(merged, preferences)

    deduped.sort(key=lambda stream: rank(stream, preferences))
    capped = deduped[:(preferences or {}).get("maxStreams", TORBOX_MAX_STREAMS)]
    cache_put(key, capped)
    return capped


# --------------------------------------------------------------------------
# server
# --------------------------------------------------------------------------

MANIFEST = {
    "id": "org.dmmplus.aggregator",
    "version": "3.0.0",
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
    "behaviorHints": {"adult": False, "p2p": False, "configurable": True, "configurationRequired": True},
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
        # Never put encrypted installation credentials, play grants or query
        # strings into access logs. Public route names alone are sufficient.
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/c/"):
            parts = path.split("/", 3)
            path = "/c/[personal]/" + (parts[3].split("/")[0] if len(parts) > 3 else "")
        elif path.startswith("/torbox/play/"):
            path = "/torbox/play/[signed]"
        sys.stderr.write("%s %s\n" % (self.command, path))

    def send_bytes(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_bytes(body, "application/json; charset=utf-8", status)

    def public_base(self):
        if PUBLIC_BASE_URL:
            return PUBLIC_BASE_URL
        host = self.headers.get("Host", "")
        if not re.fullmatch(r"[A-Za-z0-9.\-\[\]:]+", host):
            raise ConfigError("Invalid host")
        scheme = "https" if self.headers.get("X-Forwarded-Proto", "").split(",")[0].strip() == "https" else "http"
        return f"{scheme}://{host}"

    def viewer_ip(self):
        addresses = self.headers.get("X-Forwarded-For", "").split(",") + [self.client_address[0]]
        for candidate in addresses:
            try:
                address = ipaddress.ip_address(candidate.strip())
                if address.is_global:
                    return str(address)
            except ValueError:
                continue
        return None

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        self.close_connection = True
        if path != "/api/configure":
            return self.send_json({"error": "Not found"}, 404)
        if CONFIG_VAULT.cipher is None:
            return self.send_json({"error": "Personal links are not enabled on this server yet"}, 503)
        try:
            base = self.public_base()
        except ConfigError as exc:
            return self.send_json({"error": str(exc)}, 400)
        origin = self.headers.get("Origin")
        if origin and origin != base:
            return self.send_json({"error": "Open the setup page on this server to create a link"}, 403)
        if not SETUP_LIMITER.allow(self.viewer_ip() or self.client_address[0]):
            return self.send_json({"error": "Too many attempts. Please wait a minute."}, 429)
        if self.headers.get_content_type() != "application/json":
            return self.send_json({"error": "Send JSON from the setup page"}, 415)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self.send_json({"error": "Invalid request size"}, 400)
        if not 0 < length <= 4096:
            return self.send_json({"error": "Invalid request size"}, 413)
        try:
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ConfigError("Invalid setup request")
            key = clean_key(payload.get("apiKey"))
            settings = clean_preferences(payload.get("preferences", {}))
            validate_account(key)
            token = CONFIG_VAULT.seal(key, settings)
        except ConfigError as exc:
            return self.send_json({"error": str(exc)}, 400)
        except (ValueError, TypeError, UnicodeError):
            return self.send_json({"error": "Invalid setup request"}, 400)
        return self.send_json({"manifestUrl": base + "/c/" + token + "/manifest.json"})

    def do_HEAD(self):
        # Load balancers and uptime probes issue HEAD /; BaseHTTPRequestHandler
        # answers 501 otherwise, which some platforms read as unhealthy.
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        if len(path) > 20000:
            return self.send_json({"error": "Request path is too long"}, 414)
        if path in ("", "/", "/index", "/configure", "/configure.html"):
            return self.send_bytes((ASSETS / "configure.html").read_bytes(), "text/html; charset=utf-8")
        if path in ("/configure.js", "/configure.css"):
            content_type = "text/javascript; charset=utf-8" if path.endswith(".js") else "text/css; charset=utf-8"
            return self.send_bytes((ASSETS / path[1:]).read_bytes(), content_type)
        prefix, provider, settings, tenant_id, account_key = "", None, None, None, None
        match = re.match(r"^/c/([A-Za-z0-9_=-]+)/(.+)$", path)
        if match:
            token, relative = match.groups()
            try:
                tenant_id, provider, payload = PERSONAL_PROVIDERS.get(token)
            except ConfigError as exc:
                return self.send_json({"error": str(exc)}, 403)
            prefix = "/c/" + token
            path = "/" + relative
            settings, account_key = payload["preferences"], payload["key"]
            if path == "/configure":
                self.send_response(302)
                self.send_header("Location", "/configure")
                self.send_header("Content-Length", "0")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                return

        if path.startswith("/torbox/play/"):
            if provider is None:
                return self.send_json({"error": "Create a personal install link on /configure"}, 410)
            try:
                link = provider.resolve(path[len("/torbox/play/"):], user_ip=self.viewer_ip())
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
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            return

        if path in ("/manifest.json", "/manifest"):
            manifest = dict(MANIFEST)
            if provider is not None:
                manifest["id"] = CONFIG_VAULT.manifest_id(account_key)
                manifest["behaviorHints"] = dict(MANIFEST["behaviorHints"], configurationRequired=False)
            return self.send_json(manifest)

        if path in ("/health", "/healthz"):
            status = {"status": "ok", "version": MANIFEST["version"], "personalLinksEnabled": CONFIG_VAULT.cipher is not None}
            if provider is not None:
                status.update({"torbox": {"configured": True, "lastStatus": provider.last_status,
                    "lastCount": provider.last_count, "lastChecked": provider.last_checked,
                    "searchSources": provider.last_sources}, "preferences": settings})
            return self.send_json(status)

        if provider is None:
            if path.startswith("/stream/"):
                return self.send_json({"streams": [{"name": "TorBox Plus • Setup required",
                    "title": "Create your personal TorBox install link", "externalUrl": self.public_base() + "/configure"}]})
            return self.send_json({"error": "Not found"}, 404)

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
            streams = collect(ident, media_type, season, episode, provider=provider,
                              preferences=settings, tenant_id=tenant_id)
        except Exception as exc:  # a provider bug must not kill the addon
            sys.stderr.write("collect failed: %r\n" % (exc,))
            streams = []

        # Relative signed play links are made absolute per request, outside the
        # shared cache. Render supplies RENDER_EXTERNAL_URL automatically.
        try:
            base = self.public_base()
        except ConfigError as exc:
            return self.send_json({"error": str(exc)}, 400)
        result = []
        for stream in streams:
            stream = {k: v for k, v in stream.items() if not k.startswith("_")}
            if str(stream.get("url", "")).startswith("/torbox/play/"):
                stream = dict(stream, url=base + prefix + stream["url"])
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
