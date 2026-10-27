import time
import unittest
from unittest.mock import patch

import addon


class AggregationTests(unittest.TestCase):
    def setUp(self):
        addon._cache.clear()

    def tearDown(self):
        addon._cache.clear()

    def test_slow_metadata_does_not_block_direct_torbox(self):
        def slow(*args):
            time.sleep(0.2)
            return None
        with patch.object(addon, "SOFT_DEADLINE", 0.04), \
                patch.object(addon, "TORBOX_API_KEY", "test-key"), \
                patch.object(addon, "tmdb_from_imdb", slow), \
                patch.object(addon, "PROVIDERS", {
                    "dmm": lambda *args: [{"url": "https://dmm.example/play", "source": "DMM"}],
                    "torbox": lambda *args: [{"url": "/torbox/play/signed", "source": "TorBox"}],
                    "chq": lambda *args: [],
                }):
            start = time.monotonic()
            rows = addon.collect("tt12345", "movie", None, None)
            elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.15)
        self.assertEqual({row["source"] for row in rows}, {"DMM", "TorBox"})
        time.sleep(0.2)  # Finish the deliberately slow metadata stub before teardown.

    def test_hosters_cannot_crowd_out_direct_torbox(self):
        with patch.object(addon, "MAX_STREAMS", 2), \
                patch.object(addon, "TORBOX_API_KEY", "test-key"), \
                patch.object(addon, "tmdb_from_imdb", return_value=None), \
                patch.object(addon, "PROVIDERS", {
                    "dmm": lambda *args: [{"url": f"https://dmm.example/{n}", "source": "DMM"} for n in range(9)],
                    "torbox": lambda *args: [{"url": f"/torbox/play/{n}", "source": "TorBox"} for n in range(11)],
                }):
            rows = addon.collect("tt12345", "movie", None, None)
        self.assertEqual(sum(row["source"] == "TorBox" for row in rows), 11)
        self.assertEqual(len(rows), 13)

    def test_provider_failure_preserves_casts(self):
        def failed(*args):
            raise addon.ProviderError("torbox_http_401")
        with patch.object(addon, "TORBOX_API_KEY", "test-key"), \
                patch.object(addon, "tmdb_from_imdb", return_value=None), \
                patch.object(addon, "PROVIDERS", {
                    "dmm": lambda *args: [{"url": "https://dmm.example/play", "source": "DMM"}],
                    "torbox": failed,
                }):
            rows = addon.collect("tt12345", "movie", None, None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "DMM")


if __name__ == "__main__":
    unittest.main()
