"""Authenticated, encrypted personal install links; no account database."""

import hashlib
import hmac
import json
import re
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict

from cryptography.fernet import Fernet, InvalidToken


class ConfigError(Exception):
    pass


def preferences(value):
    if not isinstance(value, dict):
        raise ConfigError("Invalid preferences")
    prefer = value.get("preferEnglish", True)
    hide = value.get("hideCam", False)
    maximum = value.get("maxStreams", 100)
    size = value.get("maxFileSizeGB", 0)
    if type(prefer) is not bool or type(hide) is not bool:
        raise ConfigError("Invalid preference switches")
    if type(maximum) is not int or maximum < 1 or maximum > 200:
        raise ConfigError("Choose between 1 and 200 results")
    if type(size) not in (int, float) or not 0 <= size <= 1000:
        raise ConfigError("Choose a size limit between 0 and 1000 GB")
    return {"preferEnglish": prefer, "hideCam": hide, "maxStreams": maximum,
            "maxFileSizeGB": size}


def clean_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._-]{16,256}", value.strip()):
        raise ConfigError("Enter your TorBox API key from TorBox account settings")
    return value.strip()


def validate_account(key):
    """Check the submitted key only at TorBox, without returning account data."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    request = urllib.request.Request("https://api.torbox.app/v1/api/user/me", headers={
        "Authorization": "Bearer " + key, "User-Agent": "TorBox-Plus/3.0",
        "Accept": "application/json"})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=10) as response:
            raw = response.read(512 * 1024 + 1)
            if len(raw) > 512 * 1024:
                raise ConfigError("TorBox returned an invalid response; try again later")
            data = json.loads(raw)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ConfigError("TorBox rejected this key. Copy the current API key from TorBox settings.") from None
        if exc.code == 429:
            raise ConfigError("TorBox is rate limiting requests. Please wait a few minutes.") from None
        raise ConfigError("TorBox is unavailable; try again shortly") from None
    except (urllib.error.URLError, OSError, ValueError):
        raise ConfigError("Could not reach TorBox; try again shortly") from None
    if not isinstance(data, dict) or data.get("success") is not True or not isinstance(data.get("data"), dict):
        raise ConfigError("TorBox could not verify this account")


class ConfigVault:
    def __init__(self, secret):
        self.secret = secret.strip()
        self.cipher = Fernet(self.secret.encode()) if self.secret else None

    def seal(self, key, settings):
        if self.cipher is None:
            raise ConfigError("Personal links are not enabled on this server yet")
        payload = {"v": 1, "key": clean_key(key), "preferences": preferences(settings)}
        return self.cipher.encrypt(json.dumps(payload, separators=(",", ":")).encode()).decode()

    def open(self, token):
        if self.cipher is None or not isinstance(token, str) or not 80 <= len(token) <= 2048:
            raise ConfigError("Invalid personal link. Create a new one on the setup page.")
        try:
            payload = json.loads(self.cipher.decrypt(token.encode()))
            if not isinstance(payload, dict) or payload.get("v") != 1:
                raise ValueError()
            return {"key": clean_key(payload["key"]),
                    "preferences": preferences(payload["preferences"])}
        except (InvalidToken, ValueError, TypeError, KeyError, UnicodeError):
            raise ConfigError("Invalid personal link. Create a new one on the setup page.") from None

    def manifest_id(self, key):
        identity = hmac.new(self.secret.encode(), key.encode(), hashlib.sha256).hexdigest()[:20]
        return "org.torboxplus.personal." + identity


class PersonalProviders:
    """Bounded in-memory instances; all account/URL caches stay tenant-specific."""
    def __init__(self, vault, factory, limit=32):
        self.vault, self.factory, self.limit = vault, factory, limit
        self.entries = OrderedDict()
        self.lock = threading.Lock()

    def get(self, token):
        identity = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            hit = self.entries.get(identity)
            if hit:
                self.entries.move_to_end(identity)
                return identity, hit[0], hit[1]
        payload = self.vault.open(token)
        with self.lock:
            if identity not in self.entries:
                self.entries[identity] = (self.factory(payload["key"]), payload)
                while len(self.entries) > self.limit:
                    self.entries.popitem(last=False)
            self.entries.move_to_end(identity)
            provider, payload = self.entries[identity]
        return identity, provider, payload


class SetupLimiter:
    def __init__(self):
        self.lock = threading.Lock()
        self.attempts = OrderedDict()

    def allow(self, address):
        now = time.monotonic()
        with self.lock:
            recent = [t for t in self.attempts.pop(address, []) if now - t < 60]
            allowed = len(recent) < 6
            if allowed:
                recent.append(now)
            self.attempts[address] = recent
            while len(self.attempts) > 2048:
                self.attempts.popitem(last=False)
        return allowed
