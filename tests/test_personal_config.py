import contextlib
import http.client
import io
import json
import threading
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

import addon
from personal_config import ConfigError, ConfigVault, PersonalProviders, SetupLimiter, preferences
from torbox_provider import TorBoxProvider


KEY_A = "test-alice-torbox-key-0001"
KEY_B = "test-bob-torbox-key-00002"
HASH = "a" * 40


class FakeProvider(TorBoxProvider):
    def __init__(self, key):
        super().__init__(key, enable_knaben=False)

    def streams(self, *args):
        return [{"source": "TorBox", "name": "Test file", "title": "Movie.1080p.mkv",
                 "url": self._play_path(HASH, "Movie.1080p.mkv", None, None)}]

    def resolve(self, token, user_ip=None):
        self._decode(token)
        return "https://cdn.example/" + ("alice" if self.api_key == KEY_A else "bob")


class VaultTests(unittest.TestCase):
    def test_credentials_are_encrypted_and_tampering_is_rejected(self):
        vault = ConfigVault(Fernet.generate_key().decode())
        token = vault.seal(KEY_A, {"hideCam": True})
        self.assertNotIn(KEY_A, token)
        self.assertEqual(vault.open(token)["key"], KEY_A)
        self.assertTrue(vault.open(token)["preferences"]["hideCam"])
        with self.assertRaises(ConfigError):
            vault.open(token[:-9] + "tamperedx")
        with self.assertRaises(ConfigError):
            ConfigVault(Fernet.generate_key().decode()).open(token)

    def test_persistent_secret_survives_process_recreation(self):
        secret = Fernet.generate_key().decode()
        token = ConfigVault(secret).seal(KEY_A, {})
        self.assertEqual(ConfigVault(secret).open(token)["key"], KEY_A)

    def test_no_secret_fails_closed(self):
        with self.assertRaises(ConfigError):
            ConfigVault("").seal(KEY_A, {})

    def test_preference_bounds_and_types(self):
        for settings in ({"maxStreams": 999}, {"maxStreams": True}, {"hideCam": "false"},
                         {"maxFileSizeGB": float("nan")}, {"maxFileSizeGB": -1}):
            with self.assertRaises(ConfigError):
                preferences(settings)

    def test_registry_is_bounded_and_separates_accounts(self):
        vault = ConfigVault(Fernet.generate_key().decode())
        registry = PersonalProviders(vault, FakeProvider, limit=1)
        a = vault.seal(KEY_A, {})
        b = vault.seal(KEY_B, {})
        self.assertNotEqual(registry.get(a)[0], registry.get(b)[0])
        self.assertEqual(registry.get(b)[1].api_key, KEY_B)
        self.assertEqual(len(registry.entries), 1)


class PersonalHTTPTests(unittest.TestCase):
    def setUp(self):
        addon._cache.clear()
        self.vault = ConfigVault(Fernet.generate_key().decode())
        self.registry = PersonalProviders(self.vault, FakeProvider)
        self.log = io.StringIO()
        self.patches = [patch.object(addon, "CONFIG_VAULT", self.vault),
                        patch.object(addon, "PERSONAL_PROVIDERS", self.registry),
                        patch.object(addon, "SETUP_LIMITER", SetupLimiter()),
                        patch.object(addon, "PUBLIC_BASE_URL", ""),
                        patch.object(addon, "validate_account"),
                        contextlib.redirect_stderr(self.log)]
        self.entered = [p.__enter__() for p in self.patches]
        self.validator = self.entered[4]
        self.server = addon.Server(("127.0.0.1", 0), addon.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        for p in reversed(self.patches):
            p.__exit__(None, None, None)
        addon._cache.clear()

    def request(self, path, method="GET", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        status, response_headers, data = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return status, response_headers, data

    def install(self, key, settings=None):
        status, _, data = self.request("/api/configure", "POST",
            json.dumps({"apiKey": key, "preferences": settings or {}}),
            {"Content-Type": "application/json", "Origin": self.base})
        self.assertEqual(status, 200, data)
        result = json.loads(data)
        self.assertNotIn(key, data.decode())
        return result["manifestUrl"].removeprefix(self.base).removesuffix("/manifest.json")

    def test_personal_install_streams_and_playback_use_correct_account(self):
        alice, bob = self.install(KEY_A), self.install(KEY_B)
        manifests = [json.loads(self.request(p + "/manifest.json")[2]) for p in (alice, bob)]
        self.assertNotEqual(manifests[0]["id"], manifests[1]["id"])
        self.assertFalse(manifests[0]["behaviorHints"]["configurationRequired"])
        results = [json.loads(self.request(p + "/stream/movie/tt0137523.json")[2])["streams"] for p in (alice, bob)]
        alice_url, bob_url = results[0][0]["url"], results[1][0]["url"]
        self.assertIn(alice + "/torbox/play/", alice_url)
        self.assertIn(bob + "/torbox/play/", bob_url)
        self.assertNotEqual(alice_url, bob_url)
        self.assertEqual(self.request(alice_url.removeprefix(self.base))[1]["Location"], "https://cdn.example/alice")
        self.assertEqual(self.request(bob_url.removeprefix(self.base))[1]["Location"], "https://cdn.example/bob")
        # Swapping the config token must never resolve a different account's play grant.
        wrong = bob + "/torbox/play/" + alice_url.rsplit("/", 1)[-1]
        self.assertEqual(self.request(wrong)[0], 410)
        self.assertEqual(self.validator.call_count, 2)

    def test_public_link_cannot_use_server_owner_account(self):
        status, _, data = self.request("/manifest.json")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data)["behaviorHints"]["configurationRequired"])
        with patch.object(addon.TORBOX, "streams") as owner:
            status, _, data = self.request("/stream/movie/tt0137523.json")
        owner.assert_not_called()
        row = json.loads(data)["streams"][0]
        self.assertNotIn("url", row)
        self.assertTrue(row["externalUrl"].endswith("/configure"))
        self.assertEqual(self.request("/torbox/play/old-link")[0], 410)

    def test_invalid_personal_link_has_no_global_fallback(self):
        self.assertEqual(self.request("/c/not-a-valid-config/manifest.json")[0], 403)

    def test_setup_page_and_assets_have_no_credentials(self):
        for path in ("/configure", "/configure.js", "/configure.css"):
            status, headers, data = self.request(path)
            self.assertEqual(status, 200)
            self.assertEqual(headers["Referrer-Policy"], "no-referrer")
            self.assertIn("no-store", headers["Cache-Control"])
            self.assertNotIn(KEY_A.encode(), data)
            self.assertNotIn(KEY_B.encode(), data)

    def test_cross_origin_and_oversized_posts_are_rejected(self):
        body = json.dumps({"apiKey": KEY_A})
        self.assertEqual(self.request("/api/configure", "POST", body,
            {"Content-Type": "application/json", "Origin": "https://other.example"})[0], 403)
        self.assertEqual(self.request("/api/configure", "POST", " " * 4097,
            {"Content-Type": "application/json"})[0], 413)
        self.validator.assert_not_called()

    def test_rejected_key_does_not_issue_an_install_link(self):
        self.validator.side_effect = ConfigError("TorBox rejected this key")
        status, _, data = self.request("/api/configure", "POST", json.dumps({"apiKey": KEY_A}),
                                       {"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        self.assertNotIn("manifestUrl", json.loads(data))

    def test_personal_credentials_never_appear_in_access_logs(self):
        path = self.install(KEY_A)
        self.request(path + "/manifest.json")
        self.assertNotIn(path.split("/")[-1], self.log.getvalue())
        self.assertNotIn(KEY_A, self.log.getvalue())
        self.assertIn("[personal]", self.log.getvalue())


if __name__ == "__main__":
    unittest.main()
