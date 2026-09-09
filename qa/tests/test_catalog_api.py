import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import deezer_client as dc, main
from app.library_catalog import MusicRootRequired
from app.remote_actions import RemoteActionStore
from test_library_catalog import track, wav_file
from test_security_contract import asgi_request


class CatalogApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'Music'
        self.root.mkdir()
        for name, value in [('ROOT', self.base / 'State'), ('CONFIG_PATH', self.base / 'State' / 'config.local.json')]:
            patcher = patch.object(dc, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def configure(self):
        dc.save_config({'music_root': str(self.root)})

    def test_config_does_not_advertise_a_default_music_root(self):
        with patch.object(main, 'get_session', side_effect=AssertionError('config must stay local')), patch.object(main, 'get_deezer_arl', return_value=None):
            result = main.api_config()
        self.assertEqual(result['music_root'], '')
        self.assertFalse(result['music_root_configured'])

    def test_unconfigured_track_read_and_download_do_not_access_provider_or_queue(self):
        with patch.object(main, 'fetch_tracks') as fetch, patch.object(main.jobs, 'enqueue') as enqueue:
            with self.assertRaises(MusicRootRequired):
                main.api_tracks('12', 'Likes')
            with self.assertRaises(MusicRootRequired):
                main.api_download('12', main.DownloadIn(tracks=[track(1, 'One')]), 'Likes')
        fetch.assert_not_called()
        enqueue.assert_not_called()

    def test_likes_api_shows_existing_paths_and_enqueues_only_missing(self):
        self.configure()
        audio = wav_file(self.root / 'Manually sorted' / 'Artist - One.wav')
        requested = [track(1, 'One'), track(2, 'Two')]
        with patch.object(main, 'fetch_tracks', return_value=requested):
            view = main.api_tracks('12', 'Likes')
        self.assertEqual(Path(view['tracks'][0]['file_path']), audio)
        with patch.object(main.jobs, 'enqueue', return_value='test-job') as enqueue:
            result = main.api_download('12', main.DownloadIn(tracks=requested), 'Likes')
        self.assertEqual([t['id'] for t in enqueue.call_args.args[2]], ['2'])
        self.assertEqual(result['already_present'], 1)
        self.assertTrue(audio.exists())

    def test_search_reuses_local_file_but_reconciles_original_remote_selection(self):
        self.configure()
        wav_file(self.root / 'Elsewhere' / 'Artist - One.wav')
        client = SimpleNamespace(add_tracks_to_playlist=AsyncMock(return_value=SimpleNamespace(added_track_ids=['1'])))
        body = main.SearchDownloadIn(target_key='123', target_title='Likes', tracks=[track(1, 'One')])
        with patch.object(main.jobs, 'enqueue') as enqueue, patch.object(main, '_deezer_gql_client', return_value=client), patch.object(main, '_remote_action_store', RemoteActionStore(self.base / 'remote.json')):
            result = asyncio.run(main.api_search_download(body))
        enqueue.assert_not_called()
        client.add_tracks_to_playlist.assert_awaited_once_with(playlist_id='123', track_ids=['1'])
        self.assertIsNone(result['job_id'])
        self.assertEqual(result['already_present'], 1)

    def test_binding_requires_root_and_accepts_different_existing_folder(self):
        folder = self.base / 'Different name'
        folder.mkdir()
        with self.assertRaises(MusicRootRequired):
            main.api_bind('123', main.BindIn(path=str(folder)))
        self.configure()
        result = main.api_bind('123', main.BindIn(path=str(folder)))
        self.assertEqual(Path(result['path']), folder)
        self.assertEqual(Path(dc.load_config()['bindings']['123']), folder)

    def test_packaged_legacy_login_cannot_bypass_native_broker(self):
        with patch.dict(os.environ, {'DECKPIPE_AUTH_BROKER_TOKEN': 'synthetic-native-private'}):
            with self.assertRaises(main.HTTPException) as captured:
                main.api_login_deezer(main.LoginDeezerIn(arl='synthetic'))
        self.assertEqual(captured.exception.status_code, 403)
        self.assertEqual(captured.exception.detail['code'], 'native_auth_required')

    def test_missing_root_http_error_is_structured(self):
        # The shared ASGI app is configured with the synthetic launch environment.
        host = '127.0.0.1:' + os.environ['DECKPIPE_BOUND_PORT']
        status, _, body = asyncio.run(asgi_request(main.app, 'POST', '/api/playlists/12/download',
            {'host': host, 'authorization': 'Bearer ' + os.environ['DECKPIPE_API_TOKEN'], 'content-type': 'application/json'},
            json.dumps({'tracks': [track(1, 'One')]}).encode()))
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)['detail']['code'], 'music_root_required')

    def test_error_inventory_does_not_report_empty_success_when_provider_is_unavailable(self):
        self.configure()
        with patch.object(main, 'fetch_playlists', AsyncMock(side_effect=ConnectionError('private response'))), patch.object(main, '_sc_sources', return_value=[]), patch.object(main, '_local_sources', return_value=[]):
            result = asyncio.run(main.api_errors(include_status=True))
            self.assertEqual(result['items'], [])
            self.assertEqual(result['errors'][0]['code'], 'provider_unavailable')
            self.assertNotIn('private response', str(result))
            with self.assertRaises(main.HTTPException):
                asyncio.run(main.api_errors())


if __name__ == '__main__':
    unittest.main()
