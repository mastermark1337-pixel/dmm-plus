import json
import time
import unittest
from unittest.mock import patch

from torbox_provider import InvalidPlayLink, ProviderError, TorBoxProvider, quality, select_file


H1, H2 = "a" * 40, "b" * 40
FILE = {"id": 349, "name": "Movie/Movie.2026.1080p.mkv", "size": 5 * 1024**3}


class TorBoxTests(unittest.TestCase):
    def setUp(self):
        self.provider = TorBoxProvider("test-account-key")

    def test_resolution_uses_filename_before_release_group(self):
        self.assertEqual(quality("Movie.1080p.[4kHDHub.com].mkv"), "1080p")
        self.assertEqual(quality("Movie.2160p.mkv"), "4K")
        self.assertEqual(quality("Movie.1440p.mkv"), "1440p")

    def test_file_ids_are_not_file_indexes(self):
        self.assertEqual(select_file([FILE], "Movie.2026.1080p.mkv")["id"], 349)

    def test_ambiguous_and_wrong_files_not_selected(self):
        other = dict(FILE, name="Another/Movie.2026.1080p.mkv", id=7)
        self.assertIsNone(select_file([FILE, other], "Movie.2026.1080p.mkv"))
        self.assertIsNone(select_file([FILE], "Different.mkv"))
        self.assertIsNone(select_file([FILE, other]))

    def test_supplements_archives_and_infected_files_not_selected(self):
        for name in ("sample.mkv", "trailer.mp4", "Extras.mkv", "Movie.zip"):
            self.assertIsNone(select_file([dict(FILE, name=name)]))
        self.assertIsNone(select_file([dict(FILE, infected=True)]))

    def test_series_does_not_choose_largest_episode(self):
        files = [dict(FILE, name="Show.S01E01.mkv", id=123),
                 dict(FILE, name="Show.S01E02.mkv", id=987)]
        self.assertEqual(select_file(files, season=1, episode=2)["id"], 987)
        self.assertIsNone(select_file(files, season=1, episode=3))

    def test_listing_is_read_only_cached_only_and_has_no_five_cap(self):
        hashes = [f"{n:040x}" for n in range(1, 13)]
        upstream = {"streams": [{"infoHash": h, "fileIdx": 0,
                                  "behaviorHints": {"filename": "Movie.2026.1080p.mkv"}}
                                 for h in hashes]}
        cached = {h: {"files": [FILE]} for h in hashes[:-1]}
        with patch.object(self.provider, "_request", return_value=upstream) as search, \
                patch.object(self.provider, "_api", return_value=cached) as api:
            rows = self.provider.streams("tt34890576", "movie")
        self.assertEqual(len(rows), 11)
        self.assertEqual(api.call_args.args[0], "checkcached")
        self.assertEqual(api.call_count, 1)
        self.assertNotIn("test-account-key", json.dumps(rows))
        self.assertNotIn("test-account-key", search.call_args.args[0])
        self.assertTrue(all(row["source"] == "TorBox" for row in rows))

    def test_play_links_are_signed_and_expire(self):
        path = self.provider._play_path(H1, FILE["name"], None, None)
        token = path.rsplit("/", 1)[-1]
        self.assertEqual(self.provider._decode(token)["h"], H1)
        with self.assertRaises(InvalidPlayLink):
            self.provider._decode(token + "x")
        with patch("torbox_provider.time.time", return_value=time.time() + 7 * 3600):
            with self.assertRaises(InvalidPlayLink):
                self.provider._decode(token)

    def test_play_rechecks_cache_and_uses_torbox_file_id(self):
        token = self.provider._play_path(H1, FILE["name"], None, None).rsplit("/", 1)[-1]
        responses = [
            {H1: {"files": [FILE]}},
            {"torrent_id": 77},
            {"id": 77, "hash": H1, "download_finished": True, "files": [FILE]},
            "https://cdn.torbox.app/signed-video",
        ]
        with patch.object(self.provider, "_api", side_effect=responses) as api:
            self.assertEqual(self.provider.resolve(token), "https://cdn.torbox.app/signed-video")
            self.assertEqual(self.provider.resolve(token), "https://cdn.torbox.app/signed-video")
        self.assertEqual(api.call_count, 4)
        self.assertEqual(api.call_args_list[1].kwargs["fields"]["add_only_if_cached"], "true")
        self.assertEqual(api.call_args_list[1].kwargs["fields"]["seed"], "3")
        self.assertEqual(api.call_args_list[3].args[1]["file_id"], 349)
        self.assertEqual(api.call_args_list[3].args[1]["torrent_id"], 77)

    def test_disappeared_cache_does_not_start_download(self):
        token = self.provider._play_path(H1, FILE["name"], None, None).rsplit("/", 1)[-1]
        with patch.object(self.provider, "_api", return_value={}) as api:
            with self.assertRaisesRegex(ProviderError, "file_no_longer_cached"):
                self.provider.resolve(token)
        self.assertEqual(api.call_count, 1)

    def test_wrong_torrent_cannot_resolve(self):
        token = self.provider._play_path(H1, FILE["name"], None, None).rsplit("/", 1)[-1]
        responses = [{H1: {"files": [FILE]}}, {"torrent_id": 77},
                     {"id": 77, "hash": H2, "download_finished": True, "files": [FILE]}]
        with patch.object(self.provider, "_api", side_effect=responses) as api:
            with self.assertRaises(ProviderError):
                self.provider.resolve(token)
        self.assertEqual(api.call_count, 3)

    def test_without_key_no_network_or_playback(self):
        provider = TorBoxProvider("")
        with patch.object(provider, "_request") as request:
            self.assertEqual(provider.streams("tt34890576", "movie"), [])
        request.assert_not_called()
        with self.assertRaises(InvalidPlayLink):
            provider.resolve("anything")

    def test_partial_cache_response_does_not_advertise_unknown_files(self):
        upstream = {"streams": [{"infoHash": H1}, {"infoHash": H2}]}
        with patch.object(self.provider, "_request", return_value=upstream), \
                patch.object(self.provider, "_api", return_value={H1: {"files": []}}):
            self.assertEqual(self.provider.streams("tt34890576", "movie"), [])


if __name__ == "__main__":
    unittest.main()
