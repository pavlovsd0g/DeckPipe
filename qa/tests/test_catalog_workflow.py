import importlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_library_catalog import track, wav_file


class CatalogWorkflowTests(unittest.TestCase):
    def setUp(self):
        try:
            self.service = importlib.import_module('app.catalog_service')
        except ModuleNotFoundError:
            self.fail('The required-root/global-library workflow is not implemented')
        from app import deezer_client, library
        self.dc, self.library = deezer_client, library
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'Music'
        self.root.mkdir()
        self.state = self.base / 'AppState'
        self.state.mkdir()
        for name, value in [('ROOT', self.state), ('CONFIG_PATH', self.state / 'config.local.json')]:
            patcher = patch.object(self.dc, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def configure(self, **extra):
        self.dc.save_config({'music_root': str(self.root), **extra})

    def test_missing_root_blocks_sync_instead_of_using_default(self):
        from app.library_catalog import MusicRootRequired
        with self.assertRaises(MusicRootRequired):
            self.service.playlist_tracks(self.root / 'Likes', [track(1, 'One')])
        with self.assertRaises(MusicRootRequired):
            self.service.prepare_download(self.root / 'Likes', [track(1, 'One')])

    def test_likes_reuse_other_folders_without_creating_target_sidecar(self):
        self.configure()
        first = wav_file(self.root / 'Warmup' / 'Artist - One.wav')
        second = wav_file(self.root / 'Peak' / 'Artist - Two.wav')
        target = self.root / 'Likes'
        requested = [track(1, 'One'), track(2, 'Two'), track(3, 'Three')]
        rows = self.service.playlist_tracks(target, requested)
        self.assertEqual([r['status'] for r in rows], ['ok', 'ok', 'missing'])
        self.assertEqual([Path(r['file_path']) for r in rows[:2]], [first, second])
        self.assertEqual(rows[0]['location_scope'], 'library')
        self.assertFalse((target / '.deckpipe.json').exists())
        prepared = self.service.prepare_download(target, requested)
        self.assertEqual([t['id'] for t in prepared['tracks']], ['3'])
        self.assertEqual(prepared['already_present'], 2)
        self.assertTrue(first.exists() and second.exists())

    def test_explicit_folder_binding_and_global_root_work_together(self):
        external = self.base / 'Totally different name'
        audio = wav_file(external / 'Artist - One.wav')
        self.configure(bindings={'99': str(external)})
        self.assertEqual(self.library.playlist_dir('99', 'Streaming title'), external)
        rows = self.service.playlist_tracks(self.root / 'Likes', [track(1, 'One')])
        self.assertEqual(Path(rows[0]['file_path']), audio)

    def test_ambiguous_and_offline_do_not_trigger_automatic_download(self):
        self.configure()
        wav_file(self.root / 'A' / 'Artist - One.wav')
        wav_file(self.root / 'B' / 'Artist - One.wav')
        prepared = self.service.prepare_download(self.root / 'Likes', [track(1, 'One'), track(3, 'Three')])
        self.assertEqual([t['id'] for t in prepared['tracks']], ['3'])
        self.assertEqual(prepared['needs_attention'], 1)
        self.root.rename(self.base / 'Unplugged')
        with self.assertRaises(self.service.LibraryUnavailable):
            self.service.prepare_download(self.root / 'Likes', [track(3, 'Three')])

    def test_failed_conversion_remains_retryable_even_with_source_audio(self):
        self.configure()
        target = self.root / 'Playlist'
        audio = wav_file(target / 'Artist - One.wav')
        self.library.save_sidecar(target, {'tracks': {'1': {
            'provider': 'deezer', 'title': 'One', 'artist': 'Artist', 'file': audio.name,
            'source_file': audio.name, 'status': 'verify_failed_convert', 'error': 'conversion failed'
        }}})
        rows = self.service.playlist_tracks(target, [track(1, 'One')])
        self.assertEqual(rows[0]['status'], 'error')
        self.assertEqual(rows[0]['error'], 'conversion failed')

    def test_queued_track_rechecks_library_before_any_provider_call(self):
        from app import jobs
        self.configure()
        audio = wav_file(self.root / 'Other playlist' / 'Artist - One.wav')
        self.service.playlist_tracks(self.root / 'Likes', [track(1, 'One')])
        with patch.object(jobs, 'get_session', side_effect=AssertionError('network must not run')) as session:
            result = jobs._process_track({'mode': 'append'}, self.root / 'Likes', track(1, 'One'),
                                         {'ds': None}, {'base': 0, 'n': 0, 'digits': 2})
        self.assertTrue(result[0])
        session.assert_not_called()
        self.assertTrue(audio.exists())
        self.assertFalse((self.root / 'Likes' / '.deckpipe.json').exists())

    def test_queued_track_detects_file_created_after_initial_scan(self):
        self.configure()
        requested = track(1, 'One')
        self.assertEqual(len(self.service.prepare_download(self.root / 'Likes', [requested])['tracks']), 1)
        audio = wav_file(self.root / 'New folder' / 'Artist - One.wav')
        found = self.service.existing_download(requested)
        self.assertIsNotNone(found)
        self.assertEqual(Path(found['locations'][0]['path']), audio)

    def test_local_filename_cannot_bypass_duration_or_global_ambiguity(self):
        self.configure()
        target = self.root / 'Likes'
        wav_file(target / 'Artist - One.wav')
        wrong_duration = self.service.prepare_download(target, [track(1, 'One', duration=20)])
        self.assertEqual(wrong_duration['already_present'], 0)
        self.assertEqual(len(wrong_duration['tracks']), 1)
        wav_file(self.root / 'Other' / 'Artist - One.wav')
        ambiguous = self.service.prepare_download(target, [track(1, 'One')])
        self.assertEqual(ambiguous['already_present'], 0)
        self.assertEqual(ambiguous['needs_attention'], 1)
        self.assertFalse((target / '.deckpipe.json').exists())

    def test_successful_reuse_reconciles_failed_download_entry(self):
        from app import jobs
        self.configure()
        target = self.root / 'Likes'
        wav_file(self.root / 'Other' / 'Artist - One.wav')
        self.library.save_sidecar(target, {'tracks': {'1': {'title': 'One', 'artist': 'Artist',
            'provider': 'deezer', 'status': 'verify_failed_download', 'file': '', 'error': 'download failed'}}})
        with patch.object(jobs, 'get_session', side_effect=AssertionError('network must not run')):
            self.assertTrue(jobs._process_track({'mode': 'append'}, target, track(1, 'One'), {'ds': None}, {'base': 0, 'n': 0, 'digits': 2})[0])
        self.assertEqual(self.service.playlist_tracks(target, [track(1, 'One')])[0]['status'], 'ok')
        self.assertFalse(self.library.load_sidecar(target)['tracks'])

    def test_worker_does_not_recreate_a_disconnected_root(self):
        import threading
        import time
        from app import jobs
        self.configure()
        jobs.initialize(self.state / 'worker', start_worker=False)
        job_id = jobs.enqueue('likes', 'Likes', [track(1, 'One')], start_worker=False)
        self.root.rename(self.base / 'Disconnected')
        stop = threading.Event()
        worker = threading.Thread(target=jobs._worker, args=(jobs._worker_generation, stop), daemon=True)
        with patch.object(jobs, '_process_track') as process:
            worker.start()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and jobs.get_job(job_id)['state'] != 'done':
                stop.wait(0.02)
            stop.set()
            worker.join(2)
        self.assertFalse(self.root.exists())
        process.assert_not_called()
        self.assertEqual(jobs.get_job(job_id)['terminal_error']['code'], 'music_root_unavailable')


if __name__ == '__main__':
    unittest.main()
