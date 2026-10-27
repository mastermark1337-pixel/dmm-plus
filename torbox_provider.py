"""Independent torrent search with cache-only TorBox playback (stdlib only).

The search index receives only an IMDb id. TORBOX_API_KEY is sent exclusively
to api.torbox.app. Listing streams never adds a torrent; playback does a
cache-only add and maps filenames to TorBox's own file IDs.
"""

import base64
import concurrent.futures
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import unicodedata


class ProviderError(Exception):
    """Safe to log: never contains credentials, request URLs, or API bodies."""


class InvalidPlayLink(ProviderError):
    pass


VIDEO = re.compile(r"\.(?:mkv|mp4|avi|m4v|mov|ts|m2ts|webm)$", re.I)
SUPPLEMENT = re.compile(r"(?:^|[\W_])(?:sample|trailer|extras?|featurettes?|bonus)(?:[\W_]|$)", re.I)
HASH = re.compile(r"^[a-f0-9]{40}$")
UA = "DMM-Plus/1.3"
SEARCH_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="index")


def words(text):
    text = unicodedata.normalize("NFKD", str(text)).casefold()
    return re.findall(r"[a-z0-9]+", "".join(c for c in text if not unicodedata.combining(c)))


def release_tags(text):
    """Filename-derived labels; no guessed languages or codec properties."""
    text = str(text)
    cam = bool(re.search(r"(?:^|[\W_])(?:cam(?:rip)?|hdcam|hdts|hdtc|telesync|telecine|ts|tc)(?:[\W_]|$)", text, re.I))
    source = ("CAM/TS" if cam else "Blu-ray" if re.search(r"blu[ ._-]?ray|b[dr]rip|remux", text, re.I)
              else "WEB" if re.search(r"web[ ._-]?(?:dl|rip)|\bweb\b", text, re.I) else "")
    codec = "HEVC" if re.search(r"x265|h[ .]?265|hevc", text, re.I) else "H264" if re.search(r"x264|h[ .]?264|avc", text, re.I) else ""
    hdr = "Dolby Vision" if re.search(r"(?:^|[\W_])(?:dovi|dv)(?:[\W_]|$)", text, re.I) else "HDR" if re.search(r"hdr", text, re.I) else ""
    english = bool(re.search(r"(?:^|[\W_])(?:english|eng|en)(?:[\W_]|$)|🇬🇧|🇺🇸", text, re.I))
    foreign = bool(re.search(r"(?:^|[\W_])(?:russian|rus|ru|ukrainian|ukr|hindi|hin|tamil|dubbed|dublado|latino)(?:[\W_]|$)|🇷🇺|🇺🇦", text, re.I))
    language = "English" if english else "Multi audio" if re.search(r"(?:^|[\W_])multi(?:[\W_]|$)", text, re.I) else ""
    return {"cam": cam, "languageRank": 0 if english else 2 if foreign else 1,
            "labels": [s for s in (source, codec, hdr, language) if s]}


def quality(text):
    # Resolution tokens win over a site/release-group name containing "4k".
    match = re.search(r"(?<!\d)(2160|1440|1080|720|576|480|360)p\b", text or "", re.I)
    if match:
        return "4K" if match[1] == "2160" else match[1] + "p"
    if re.search(r"\b(?:4k|uhd)\b", text or "", re.I):
        return "4K"
    return "Auto"


def basename(name):
    return str(name).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def file_identity(name):
    # Downloaded copies often acquire " (1)" before the extension. File size is
    # checked separately, so different encodes are never merged by name alone.
    return re.sub(r"\s*\([1-9]\d?\)(?=\.[a-z0-9]{2,5}$)", "", basename(name).casefold())


def episode_matches(name, season, episode):
    name = basename(name)
    pairs = re.findall(r"(?<![a-z0-9])s(\d{1,3})[ ._-]*e(\d{1,4})(?!\d)", name, re.I)
    pairs += re.findall(r"(?<![a-z0-9])(\d{1,3})x(\d{1,4})(?!\d)", name, re.I)
    return (int(season), int(episode)) in {(int(s), int(e)) for s, e in pairs}


def select_file(files, target="", season=None, episode=None):
    videos = [f for f in files if isinstance(f, dict)
              and VIDEO.search(basename(f.get("name", "")))
              and not SUPPLEMENT.search(basename(f.get("name", "")))
              and not f.get("infected") and not f.get("zipped")]
    if target:
        exact = [f for f in videos if str(f.get("name", "")).replace("\\", "/").casefold()
                 == target.replace("\\", "/").casefold()]
        if len(exact) == 1:
            return exact[0]
        named = [f for f in videos if basename(f.get("name", "")).casefold()
                 == basename(target).casefold()]
        if len(named) == 1:
            return named[0]
        # Never silently play a different file from a pack if the named one is absent.
        return None
    if season is not None and episode is not None:
        videos = [f for f in videos if episode_matches(f.get("name", ""), season, episode)]
    # Ambiguous packs stay out; the upstream file index is not a TorBox file id.
    return videos[0] if len(videos) == 1 else None


class TorBoxProvider:
    def __init__(self, api_key, *, timeout=10, deadline=14, max_candidates=300,
                 search_base="https://addon.peerflix.mov", public_base="", enable_knaben=True):
        self.api_key = api_key.strip()
        self.timeout = max(1, float(timeout))
        self.deadline = max(1, float(deadline))
        self.max_candidates = max(1, min(int(max_candidates), 1000))
        self.search_base = search_base.rstrip("/")
        parsed = urllib.parse.urlsplit(self.search_base)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
            raise ValueError("Torrent search base must be an HTTPS URL without credentials or a query")
        self.public_base = public_base.rstrip("/")
        self.search_label = parsed.hostname
        self.enable_knaben = bool(enable_knaben)
        self._search_pool = SEARCH_POOL
        self._metadata = {}
        self._sign_key = hashlib.sha256(("DMM-Plus-play:" + self.api_key).encode()).digest()
        self._lock = threading.Lock()
        self._play_lock = threading.Lock()
        self._links = {}
        self._blocked_until = 0.0
        self.last_status = "ready" if self.api_key else "not_configured"
        self.last_count = 0
        self.last_checked = None
        self.last_sources = {}

    def _index_streams(self, imdb_id, media_type, season, episode, end):
        kind = "series" if media_type == "tv" else "movie"
        ident = f"{imdb_id}:{season}:{episode}" if kind == "series" else imdb_id
        url = self.search_base + "/stream/" + kind + "/" + urllib.parse.quote(ident, safe=":") + ".json"
        rows = self._request(url, end=end).get("streams", [])
        if not isinstance(rows, list):
            raise ProviderError("invalid_search_response")
        return [dict(row, _index=self.search_label) for row in rows if isinstance(row, dict)]

    def _knaben_streams(self, imdb_id, media_type, season, episode, end):
        kind = "series" if media_type == "tv" else "movie"
        key = (kind, imdb_id)
        with self._lock:
            hit = self._metadata.get(key)
        if hit and hit[1] > time.monotonic():
            meta = hit[0]
        else:
            meta = self._request(f"https://v3-cinemeta.strem.io/meta/{kind}/{imdb_id}.json", end=end).get("meta")
            if not isinstance(meta, dict) or not meta.get("name") or meta.get("id") != imdb_id:
                raise ProviderError("metadata_unavailable")
            with self._lock:
                if len(self._metadata) >= 512:
                    self._metadata.pop(next(iter(self._metadata)))
                self._metadata[key] = (meta, time.monotonic() + 86400)
        title = meta["name"]
        year = re.match(r"(?:19|20)\d{2}", str(meta.get("releaseInfo", "")))
        if media_type == "movie" and not year:
            raise ProviderError("metadata_year_unavailable")
        query = title + (f" S{season:02d}" if media_type == "tv" else " " + year[0])
        url = "https://api.knaben.org/v2/search?" + urllib.parse.urlencode({
            "q": query, "sf": "title", "o": "seeders", "d": "desc", "s": min(300, self.max_candidates)})
        payload = self._request(url, end=end)
        hits = payload.get("hits", [])
        if not isinstance(hits, list):
            raise ProviderError("invalid_knaben_response")
        required = words(title)
        out = []
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            release = str(hit.get("title", ""))
            tokens = words(release)
            # Search engines fuzzy-match; do not offer a different title/year.
            if not required or not all(token in tokens for token in required):
                continue
            if media_type == "movie" and year[0] not in tokens:
                continue
            infohash = str(hit.get("hash", "")).lower()
            if not HASH.fullmatch(infohash):
                continue
            out.append({"infoHash": infohash, "title": release, "_index": "Knaben"})
        return out

    def _search(self, imdb_id, media_type, season, episode, end):
        if not self.enable_knaben:
            rows = self._index_streams(imdb_id, media_type, season, episode, end)
            self.last_sources = {self.search_label: {"status": "ok", "candidates": len(rows)}}
            return rows
        # Keep time for the cache check even when an index is slow or unavailable.
        search_end = min(end - 2, time.monotonic() + min(7, self.deadline * 0.55))
        jobs = {
            self.search_label: self._search_pool.submit(self._index_streams, imdb_id, media_type, season, episode, search_end),
            "Knaben": self._search_pool.submit(self._knaben_streams, imdb_id, media_type, season, episode, search_end),
        }
        concurrent.futures.wait(jobs.values(), timeout=max(0, search_end - time.monotonic()))
        rows, statuses = [], {}
        for name, future in jobs.items():
            if not future.done():
                future.cancel()
                statuses[name] = {"status": "timeout", "candidates": 0}
                continue
            try:
                found = future.result()
                rows.extend(found)
                statuses[name] = {"status": "ok", "candidates": len(found)}
            except Exception as exc:
                statuses[name] = {"status": str(exc) if isinstance(exc, ProviderError) else "unavailable", "candidates": 0}
        self.last_sources = statuses
        if not any(status["status"] == "ok" for status in statuses.values()):
            raise ProviderError("all_search_sources_unavailable")
        return rows

    def _request(self, url, *, data=None, headers=None, end=None, torbox=False):
        remaining = (end - time.monotonic()) if end is not None else self.timeout
        if remaining <= 0:
            raise ProviderError("deadline_exceeded")
        if torbox and time.monotonic() < self._blocked_until:
            raise ProviderError("torbox_rate_limited")
        request_headers = {"User-Agent": UA, "Accept": "application/json"}
        if headers:
            request_headers.update(headers)
        if torbox:
            request_headers["Authorization"] = "Bearer " + self.api_key
        request = urllib.request.Request(url, data=data, headers=request_headers)
        # No credential-bearing redirects, including to a different hostname.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, hdrs, newurl):
                return None
        try:
            with urllib.request.build_opener(NoRedirect).open(
                    request, timeout=min(self.timeout, remaining)) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise ProviderError("response_too_large")
                payload = json.loads(raw)
        except urllib.error.HTTPError as exc:
            if torbox and exc.code == 429:
                try:
                    retry = float(exc.headers.get("Retry-After", "300"))
                except (ValueError, TypeError):
                    retry = 300
                self._blocked_until = time.monotonic() + max(1, min(retry, 3600))
            raise ProviderError(("torbox" if torbox else "torrent_search") + "_http_" + str(exc.code)) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise ProviderError(("torbox" if torbox else "torrent_search") + "_unavailable") from None
        if not isinstance(payload, dict):
            raise ProviderError("invalid_response")
        if torbox and payload.get("success") is not True:
            code = str(payload.get("error") or "request_failed")
            code = code if re.fullmatch(r"[A-Z_]{1,80}", code) else "request_failed"
            raise ProviderError("torbox_" + code)
        return payload

    def _api(self, endpoint, params=None, *, fields=None, end=None):
        url = "https://api.torbox.app/v1/api/torrents/" + endpoint
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data, headers = None, {}
        if fields is not None:
            boundary = "dmmplus" + secrets.token_hex(16)
            parts = []
            for key, value in fields.items():
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n')
            parts.append(f"--{boundary}--\r\n")
            data = "".join(parts).encode()
            headers["Content-Type"] = "multipart/form-data; boundary=" + boundary
        return self._request(url, data=data, headers=headers, end=end, torbox=True).get("data")

    def _cached(self, hashes, end):
        data = self._api("checkcached", {"hash": ",".join(hashes), "format": "object",
                                       "list_files": "true"}, end=end)
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise ProviderError("invalid_cache_response")
        return {h.lower(): value for h, value in data.items() if isinstance(h, str)
                and isinstance(value, dict) and HASH.fullmatch(h.lower())}

    def _play_path(self, infohash, filename, season, episode):
        payload = {"h": infohash, "f": filename, "s": season, "e": episode,
                   "x": int(time.time()) + 6 * 3600}
        encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
        signature = hmac.new(self._sign_key, encoded.encode(), hashlib.sha256).hexdigest()
        return "/torbox/play/" + encoded + "." + signature

    def _decode(self, token):
        if not self.api_key or len(token) > 16384:
            raise InvalidPlayLink("invalid_play_link")
        try:
            encoded, signature = token.rsplit(".", 1)
            expected = hmac.new(self._sign_key, encoded.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError()
            payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            if (not isinstance(payload, dict) or not isinstance(payload.get("h"), str)
                    or not HASH.fullmatch(payload["h"]) or not isinstance(payload.get("f"), str)
                    or not payload["f"] or type(payload.get("x")) is not int
                    or payload["x"] < time.time()):
                raise ValueError()
            return payload
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise InvalidPlayLink("invalid_or_expired_play_link") from None

    def streams(self, imdb_id, media_type, season=None, episode=None):
        if not self.api_key:
            return []
        end = time.monotonic() + self.deadline
        try:
            rows = self._search(imdb_id, media_type, season, episode, end)
            candidates, seen = [], set()
            for row in rows:
                if not isinstance(row, dict):
                    continue
                infohash = str(row.get("infoHash", "")).lower()
                hints = row.get("behaviorHints") or {}
                target = hints.get("filename", "") if isinstance(hints, dict) else ""
                if not isinstance(target, str):
                    target = ""
                ident = (infohash, target.casefold())
                if not HASH.fullmatch(infohash) or ident in seen:
                    continue
                seen.add(ident)
                candidates.append((infohash, target, row))
                if len(candidates) >= self.max_candidates:
                    break
            hashes = list(dict.fromkeys(h for h, _, _ in candidates))
            cached = {}
            for start in range(0, len(hashes), 100):
                cached.update(self._cached(hashes[start:start + 100], end))
            out, emitted = [], set()
            for infohash, target, row in candidates:
                item = cached.get(infohash)
                if not item:
                    continue
                files = item.get("files")
                if not isinstance(files, list):
                    continue
                selected = select_file(files, target, season, episode)
                if not selected:
                    continue
                filename = selected["name"]
                # The same exact file is often available under multiple hashes.
                identity = (file_identity(filename), selected.get("size"))
                if identity in emitted:
                    continue
                emitted.add(identity)
                size = selected.get("size") or 0
                try:
                    size = max(0, int(size))
                except (TypeError, ValueError, OverflowError):
                    size = 0
                q = quality(filename)
                tags = release_tags(filename)
                size_label = f" • {size / 1024**3:.2f} GB" if size else ""
                out.append({"name": f"TorBox • {q}{size_label}",
                            "title": basename(filename) + ("\n" + " • ".join(tags["labels"]) if tags["labels"] else "") + "\nTorBox cached",
                            "url": self.public_base + self._play_path(infohash, filename, season, episode),
                            "source": "TorBox", "quality": q,
                            "_torrentHash": infohash, "_filename": filename, "_bytes": size,
                            "_cam": tags["cam"], "_languageRank": tags["languageRank"],
                            "behaviorHints": {"bingeGroup": "dmmplus-tb:" + infohash,
                                              "filename": basename(filename), "videoSize": size}})
            self.last_status, self.last_count = "ok", len(out)
            return out
        except ProviderError as exc:
            self.last_status, self.last_count = str(exc), 0
            raise
        finally:
            self.last_checked = int(time.time())

    def resolve(self, token, user_ip=None):
        payload = self._decode(token)
        try:
            address = ipaddress.ip_address(user_ip) if user_ip else None
            user_ip = str(address) if address and address.is_global else None
        except ValueError:
            user_ip = None
        cache_key = (payload["h"], payload["f"], user_ip)
        end = time.monotonic() + 25
        # Resolution is serialized, keeping repeated clicks from adding/minting
        # the same torrent concurrently and bounding this single account's load.
        if not self._play_lock.acquire(timeout=1):
            raise ProviderError("playback_busy_retry")
        try:
            with self._lock:
                hit = self._links.get(cache_key)
            if hit and hit[1] > time.monotonic():
                return hit[0]
            item = self._cached([payload["h"]], end).get(payload["h"])
            if not item or not select_file(item.get("files") or [], payload["f"], payload.get("s"), payload.get("e")):
                raise ProviderError("file_no_longer_cached")
            created = self._api("createtorrent", fields={
                "magnet": "magnet:?xt=urn:btih:" + payload["h"], "seed": "3",
                "allow_zip": "false", "as_queued": "false", "add_only_if_cached": "true",
            }, end=end)
            torrent_id = created.get("torrent_id") if isinstance(created, dict) else None
            if type(torrent_id) is not int or torrent_id < 0:
                raise ProviderError("torrent_not_ready_retry")
            torrent = self._api("mylist", {"id": torrent_id, "bypass_cache": "true"}, end=end)
            if isinstance(torrent, list):
                torrent = next((t for t in torrent if isinstance(t, dict) and t.get("id") == torrent_id), None)
            if (not isinstance(torrent, dict) or torrent.get("id") != torrent_id
                    or str(torrent.get("hash", "")).lower() != payload["h"]
                    or not (torrent.get("download_finished") or torrent.get("download_state") in ("cached", "completed"))):
                raise ProviderError("torrent_not_ready_retry")
            selected = select_file(torrent.get("files") or [], payload["f"], payload.get("s"), payload.get("e"))
            if not selected or type(selected.get("id")) is not int or selected["id"] < 0:
                raise ProviderError("file_not_found")
            # TorBox IDs need not equal the index of the file in any listing.
            params = {"token": self.api_key, "torrent_id": torrent_id, "file_id": selected["id"]}
            if user_ip:
                # The browser/player needs its nearest CDN, rather than the
                # server's region. Keep minted URLs separate for each viewer.
                params["user_ip"] = user_ip
            link = self._api("requestdl", params, end=end)
            parsed = urllib.parse.urlsplit(link) if isinstance(link, str) else None
            if (not parsed or parsed.scheme != "https" or not parsed.hostname
                    or parsed.username or parsed.password or "\r" in link or "\n" in link):
                raise ProviderError("invalid_download_link")
            with self._lock:
                now = time.monotonic()
                self._links = {k: v for k, v in self._links.items() if v[1] > now}
                if len(self._links) >= 256:
                    self._links.pop(next(iter(self._links)))
                self._links[cache_key] = (link, now + 180)
            return link
        finally:
            self._play_lock.release()
