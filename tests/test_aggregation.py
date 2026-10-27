import time
import unittest
from unittest.mock import patch

import addon


class AggregationTests(unittest.TestCase):
    def setUp(self):
        addon._cache.clear()

    def tearDown(self):
        addon._cache.clear()

    def test_more_than_five_files_are_returned(self):
        with patch.object(addon, 'TORBOX_API_KEY', 'test-key'), patch.object(addon, 'PROVIDERS', {
            'torbox': lambda *args: [{'url': f'/torbox/play/{n}', 'source': 'TorBox'} for n in range(20)]
        }):
            rows = addon.collect('tt12345', 'movie', None, None)
        self.assertEqual(len(rows), 20)

    def test_slow_source_does_not_hold_response_beyond_deadline(self):
        def slow(*args):
            time.sleep(0.2)
            return []
        with patch.object(addon, 'SOFT_DEADLINE', 0.04), patch.object(addon, 'TORBOX_API_KEY', 'test-key'), patch.object(addon, 'PROVIDERS', {
            'slow': slow,
            'torbox': lambda *args: [{'url': '/torbox/play/fast', 'source': 'TorBox'}],
        }):
            start = time.monotonic()
            rows = addon.collect('tt12345', 'movie', None, None)
            elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.15)
        self.assertEqual(len(rows), 1)
        time.sleep(0.2)

    def test_same_file_from_different_indexes_is_shown_once(self):
        rows = [{'url': '/torbox/play/one', 'source': 'TorBox', '_torrentHash': 'a' * 40,
                 '_filename': 'Movie.1080p.mkv', '_bytes': 2000},
                {'url': '/torbox/play/two', 'source': 'TorBox', '_torrentHash': 'b' * 40,
                 '_filename': 'Movie.1080p.mkv', '_bytes': 2000}]
        self.assertEqual(len(addon.dedupe_streams(rows)), 1)

    def test_cam_copy_ranks_below_web_copy(self):
        web = {'url': 'https://example.com/web', 'title': 'Movie.1080p.WEB-DL.English.mkv', 'quality': '1080p'}
        cam = {'url': 'https://example.com/cam', 'title': 'Movie.2160p.CAM.English.mkv', 'quality': '4K'}
        self.assertLess(addon.rank(web), addon.rank(cam))

    def test_size_limit_and_cam_filter(self):
        rows = [{'url': 'https://example.com/cam', '_filename': 'Movie.CAM.mkv', '_bytes': 1024**3},
                {'url': 'https://example.com/large', '_filename': 'Movie.WEB.mkv', '_bytes': 20 * 1024**3},
                {'url': 'https://example.com/ok', '_filename': 'Movie.WEB.mkv', '_bytes': 2 * 1024**3}]
        with patch.object(addon, 'HIDE_CAM', True), patch.object(addon, 'MAX_FILE_SIZE_GB', 10):
            result = addon.dedupe_streams(rows)
        self.assertEqual([s['url'] for s in result], ['https://example.com/ok'])

    def test_camrip_is_identified_and_duplicate_copy_suffix_is_removed(self):
        self.assertTrue(addon.release_tags('Movie.1080p.CAMRip.ENG.mkv')['cam'])
        rows = [{'url': '/a', '_filename': 'Movie.1080p.mkv', '_bytes': 2000},
                {'url': '/b', '_filename': 'Movie.1080p (1).mkv', '_bytes': 2000},
                {'url': '/c', '_filename': 'Movie.1080p (1).mkv', '_bytes': 3000}]
        self.assertEqual(len(addon.dedupe_streams(rows)), 2)


if __name__ == '__main__':
    unittest.main()
