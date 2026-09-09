import concurrent.futures
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.library_catalog import MusicCatalog
from test_library_catalog import wav_file


class CatalogConcurrencyTests(unittest.TestCase):
    def test_first_scan_and_status_can_open_a_fresh_database_concurrently(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            music = base / 'Music'
            wav_file(music / 'Artist - One.wav')
            with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
                for iteration in range(12):
                    database = base / f'fresh-{iteration}.sqlite3'
                    ready = threading.Barrier(12)

                    def open_catalog(index):
                        ready.wait(timeout=10)
                        catalog = MusicCatalog(database)
                        if index == 0:
                            return catalog.scan([music])
                        return catalog.status()

                    # Exercise real concurrent SQLite opens/WAL initialization,
                    # matching the first scan thread plus status/API readers.
                    results = list(pool.map(open_catalog, range(12)))
                    self.assertEqual(results[0]['files'], 1)
                    self.assertEqual(MusicCatalog(database).status()['files'], 1)

    def test_status_reader_does_not_wait_for_an_entire_scan(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            music = base / 'Music'
            wav_file(music / 'Artist - One.wav')
            catalog = MusicCatalog(base / 'catalog.sqlite3')
            catalog.scan([music])
            entered, release = threading.Event(), threading.Event()
            original = MusicCatalog._index_file

            def paused_index(*args, **kwargs):
                entered.set()
                if not release.wait(timeout=10):
                    raise TimeoutError('scan test was not released')
                return original(*args, **kwargs)

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                with patch.object(MusicCatalog, '_index_file', paused_index):
                    scan = pool.submit(catalog.scan, [music])
                    try:
                        self.assertTrue(entered.wait(timeout=5))
                        status = pool.submit(lambda: MusicCatalog(catalog.path).status())
                        self.assertEqual(status.result(timeout=3)['files'], 1)
                    finally:
                        release.set()
                    self.assertEqual(scan.result(timeout=5)['files'], 1)


if __name__ == '__main__':
    unittest.main()
