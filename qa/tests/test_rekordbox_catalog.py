"""Stage C HTTP boundaries, using generated audio and offline provider doubles."""
import json
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from types import SimpleNamespace
from urllib.parse import urlsplit
from qa.tests.test_security_contract import asgi_request
from qa.tests.test_library_catalog import wav_file, track


class Client:
    def __init__(self, app):
        from app.security import LoopbackSecurityMiddleware
        self.app = app
        settings = [middleware.kwargs['settings'] for middleware in app.user_middleware
                    if middleware.cls is LoopbackSecurityMiddleware]
        if len(settings) != 1:
            raise AssertionError('Client requires exactly one production loopback security middleware')
        # The target app captured immutable settings at import/launch. Other
        # suites can launch another listener and change os.environ afterwards;
        # those new process values do not reconfigure this existing ASGI app.
        self.settings = settings[0]

    def request(self, method, url, json=None, *, headers=None):
        import json as codec
        split = urlsplit(url)
        host = self.settings.bound_host
        authority = f'[{host}]' if ':' in host else host
        request_headers = {'host': f'{authority}:{self.settings.bound_port}',
                           'authorization': 'Bearer ' + self.settings.api_token,
                           'content-type': 'application/json'}
        request_headers.update(headers or {})
        async def with_query(scope, receive, send):
            scope['query_string'] = split.query.encode()
            scope['server'] = (host, self.settings.bound_port)
            await self.app(scope, receive, send)
        status, _, body = asyncio.run(asgi_request(with_query, method, split.path, request_headers,
            codec.dumps(json).encode() if json is not None else b''))
        return SimpleNamespace(status_code=status, text=body.decode(), json=lambda: codec.loads(body))

    def post(self, url, json):
        return self.request('POST', url, json)

    def get(self, url):
        return self.request('GET', url)


class ClientLaunchBindingTests(unittest.TestCase):
    def test_client_targets_immutable_app_launch_after_other_suite_changes_environment(self):
        from fastapi import FastAPI
        from app.security import LoopbackSecurityMiddleware, SecuritySettings
        app = FastAPI()
        settings = SecuritySettings.for_launch('127.0.0.1', 7241, 'synthetic-client-launch')
        app.add_middleware(LoopbackSecurityMiddleware, settings=settings)
        calls = []
        @app.get('/api/probe')
        def probe():
            calls.append('authorized')
            return {'ok': True}
        client = Client(app)
        with patch.dict(os.environ, {'DECKPIPE_BOUND_PORT': '7242', 'DECKPIPE_API_TOKEN': 'synthetic-later-launch'}):
            response = client.get('/api/probe')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(calls, ['authorized'])
        self.assertEqual(response.json(), {'ok': True})

        # The helper submits real credentials; it does not strip or bypass the
        # middleware. Explicitly bad credentials and origins still stop routing.
        for headers, expected_status in [({'authorization': 'Bearer synthetic-wrong'}, 401),
                                         ({'host': '127.0.0.1:7242'}, 403),
                                         ({'origin': 'https://untrusted.invalid'}, 403)]:
            with self.subTest(headers=headers):
                refused = client.request('GET', '/api/probe', headers=headers)
                self.assertEqual(refused.status_code, expected_status, refused.text)
                self.assertNotIn(settings.api_token, refused.text)
                self.assertEqual(calls, ['authorized'])


class CatalogApiTests(unittest.TestCase):
    def setUp(self):
        from app import main, deezer_client, library, catalog_service
        self.main = main
        self.temp = tempfile.TemporaryDirectory(prefix='rb-catalog-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.music = self.base / 'Music'
        self.music.mkdir()
        (self.music / 'Local').mkdir()
        self.external = self.base / 'External'
        self.external.mkdir()
        self.config = {'music_root': str(self.music), 'bindings': {'local:fixture': str(self.music / 'Local'), 'external': str(self.external)},
                       'local_sources': [{'id': 'fixture', 'title': 'Local', 'count': 0}],
                       'sc_sources': [{'id': 'likes', 'title': 'SC', 'url': 'https://soundcloud.com/fixture/likes'}]}
        for module in (main, deezer_client, library, catalog_service):
            p = patch.object(module, 'load_config', side_effect=lambda: self.config)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(deezer_client, 'ROOT', self.base / 'state')
        p.start()
        self.addCleanup(p.stop)
        self.paths = [wav_file(self.music / 'one' / 'Artist - One.wav'),
                      wav_file(self.music / 'two' / 'nested' / 'Artist - Two.wav'),
                      wav_file(self.external / 'Artist - Three.wav')]
        self.tracks = [track(str(i), t) for i, t in enumerate(('One', 'Two', 'Three'), 1)]
        self.client = Client(main.app)
        self.body = {'playlist_key': '123', 'playlist_title': 'Likes'}

    def fake_core(self, name, desired, **kwargs):
        from app.rekordbox import plan_playlist_sync
        plan = plan_playlist_sync(desired, [])
        stale = not kwargs['dry_run'] and kwargs.get('expected_plan_hash') != plan['hash']
        return dict(dry_run=kwargs['dry_run'], applied=not kwargs['dry_run'] and not stale,
                    reconciled=False, plan=plan, unresolved=plan['unresolved'], backup_id=None,
                    backup=None, error={'code': 'stale_preview'} if stale else None)

    def test_likes_cross_folder_order_refresh_and_stale_apply(self):
        with patch.object(self.main, 'fetch_tracks', side_effect=lambda *a, **k: list(self.tracks)) as fetch, \
             patch.object(self.main.rb, 'sync_playlist', side_effect=self.fake_core):
            result = self.client.post('/api/rb/sync', json=self.body).json()
            self.assertIsNone(result['error'])
            self.assertEqual([str(p) for p in self.paths], [t['path'] for t in result['plan']['desired_resolved']])
            self.assertTrue(result['dry_run'])
            self.assertFalse(result['applied'])
            self.assertTrue(fetch.call_args.kwargs['force_refresh'])
            self.tracks.reverse()
            result = self.client.post('/api/rb/sync?dry_run=false&confirmation_token=APPLY_REKORDBOX_CHANGES',
                                      json={**self.body, 'expected_plan_hash': result['plan']['hash']}).json()
            self.assertEqual(result['error']['code'], 'stale_preview')

    def test_reused_local_add_persists_order_and_projection_without_provider_or_job(self):
        tracks = [self.tracks[1], {**self.tracks[0], 'id': 'sc:1', 'provider': 'sc'}]
        with patch.object(self.main.jobs, 'enqueue', side_effect=AssertionError('reused files must not queue')), \
             patch.object(self.main, 'fetch_tracks', side_effect=AssertionError('local must not call Deezer')):
            result = self.client.post('/api/search/download', json={'target_key': 'local:fixture', 'target_title': 'Local', 'tracks': tracks})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertIsNone(result.json()['job_id'])
            projected = self.client.get('/api/playlists/local:fixture/tracks').json()['tracks']
            self.assertEqual(['2', '1'], [t['id'] for t in projected])
            self.assertEqual(['deezer', 'sc'], [t['provider'] for t in projected])
            # Registry count was zero; reload derives the durable membership.
            listing = self.client.get('/api/local/playlists').json()[0]
            self.assertEqual(listing['count'], 2)
            self.assertIsNone(listing['membership_error'])
            with patch.object(self.main.rb, 'sync_playlist', side_effect=self.fake_core):
                desired = self.client.post('/api/rb/sync', json={'playlist_key': 'local:fixture', 'playlist_title': 'Local'}).json()['plan']['desired_resolved']
            self.assertEqual(['deezer:2', 'sc:1'], [t['provider_id'] for t in desired])

    def test_local_count_unknown_for_missing_corrupt_and_known_for_legacy_membership(self):
        from app import library
        directory = self.music / 'Local'
        for payload in (None, b'{broken'):
            if payload is not None:
                library.sidecar_path(directory).write_bytes(payload)
            result = self.client.get('/api/local/playlists')
            self.assertEqual(result.status_code, 200, result.text)
            self.assertIsNone(result.json()[0]['count'])
            self.assertEqual(result.json()[0]['membership_error']['code'], 'local_membership_unavailable')
        library.save_sidecar(directory, {'tracks': {'1': {**self.tracks[0], 'position':1}}})
        self.assertEqual(self.client.get('/api/local/playlists').json()[0]['count'], 1)

    def test_frontend_ordered_subset_and_retry_preserve_durable_whole_membership(self):
        from app import library, jobs
        from app.rekordbox_service import persist_local_membership
        from qa.tests.test_frontend_contract import run_frontend_app_probe
        from qa.tests.rekordbox_fixture import Fixture
        members = [*self.tracks, track('4', 'Missing')]
        directory = self.music / 'Local'
        persist_local_membership(directory, members)
        # A failed saved member must survive a different missing-only job.
        library.update_track_status(directory, '3', {**self.tracks[2], 'position':3, 'status':'verify_failed_metadata', 'custom':'keep'})
        before = library.load_sidecar(directory)
        browser = run_frontend_app_probe('''
libraryConfigured = libraryReady = true;
current = {kind:'local', id:'local:fixture', title:'Local'};
tracks = ''' + json.dumps([{**t, 'status':status} for t, status in zip(members, ['ok','missing','error','missing'])]) + ''';
globalThis.confirm = () => true;
const calls=[];
globalThis.fetch=async(url, request)=>{calls.push({url,body:JSON.parse(request.body)}); return {ok:true,json:async()=>({renamed:0,already_present:1,needs_attention:0,job_id:'synthetic-job'})};};
await syncPlaylistOrder();
console.log(JSON.stringify(calls));
''')
        self.assertEqual(browser.returncode, 0, browser.stdout)
        requests = json.loads(browser.stdout)
        self.assertEqual([t['id'] for t in requests[1]['body']['tracks']], ['2','4'])
        jobs.initialize(self.base / 'jobs', start_worker=False)
        for request in requests:
            response = self.client.post(request['url'], request['body'])
            self.assertEqual(response.status_code, 200, response.text)
        job_id = response.json()['job_id']
        self.assertEqual(response.json()['already_present'], 1)
        jobs.initialize(self.base / 'jobs', start_worker=False)
        self.assertEqual([t['id'] for t in jobs.get_job(job_id)['tracks']], ['4'])
        saved = library.load_sidecar(directory)
        self.assertEqual(saved['local_membership'], before['local_membership'])
        self.assertEqual(saved['tracks']['3']['custom'], 'keep')
        # Retry transport ordering must also preserve full source membership.
        retry = self.client.post('/api/playlists/local:fixture/download?title=Local&mode=playlist_order',
            {'tracks':[members[3]]})
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(library.load_sidecar(directory)['local_membership'], before['local_membership'])
        wav_file(self.music / 'four' / 'Artist - Missing.wav')
        library.update_track_status(directory, '3', {'status':'ok'})
        fixture = Fixture()
        with patch.object(self.main.rb, '_PyrekordboxAdapter', fixture.factory):
            result = self.client.post('/api/rb/sync', {'playlist_key':'local:fixture', 'playlist_title':'Local'}).json()
        self.assertIsNone(result['error'], result)
        self.assertEqual([t['provider_id'] for t in result['plan']['desired_resolved']], ['deezer:1','deezer:2','deezer:3','deezer:4'])
        self.assertEqual(self.client.get('/api/local/playlists').json()[0]['count'], 4)

    def test_missing_local_soundcloud_retains_url_through_queue_and_download_consumer(self):
        from app import jobs, library
        from unittest.mock import MagicMock
        jobs.initialize(self.base / 'jobs', start_worker=False)
        requested = {'id': 'sc:sc:404', 'provider': 'sc', 'title': 'Absent SC', 'artist': 'Artist',
                     'duration': 1, 'url': 'https://soundcloud.com/synthetic-fixture/absent', 'position': 7, 'total': 9}
        with patch.object(self.main, 'fetch_tracks', side_effect=AssertionError('local must not call Deezer')):
            response = self.client.post('/api/search/download', json={'target_key': 'local:fixture', 'target_title': 'Local', 'tracks': [requested]})
        self.assertEqual(response.status_code, 200, response.text)
        job = jobs.get_job(response.json()['job_id'])
        queued = job['tracks'][0]
        self.assertEqual((queued['provider'], queued['id']), ('sc', '404'))
        self.assertEqual(queued['url'], requested['url'])
        self.assertEqual((queued['position'], queued['total']), (7, 9))
        # Read back the durable queue before exercising its real SC consumer.
        jobs.initialize(self.base / 'jobs', start_worker=False)
        queued = jobs.get_job(job['id'])['tracks'][0]
        ydl = MagicMock()
        ydl.__enter__.return_value.extract_info.side_effect = RuntimeError('synthetic network boundary stop')
        sc = self.main.soundcloud
        with patch.object(sc, 'sc_oauth_token', return_value=None), patch.object(sc, '_youtube_dl_opts_with_oauth', side_effect=lambda options, token: options), \
             patch.object(sc, '_bind_cookiejar'), patch.object(sc.yt_dlp, 'YoutubeDL', return_value=ydl), \
             patch.object(jobs, 'AUTO_RETRIES', 0), patch.object(jobs, 'get_session', side_effect=AssertionError('Deezer access')):
            jobs._process_track(job, self.music / 'Local', queued, {'ds': None}, {'base': 0, 'n': 0, 'digits': 2})
        ydl.__enter__.return_value.extract_info.assert_called_once_with(requested['url'], download=True)
        saved = library.load_sidecar(self.music / 'Local')['local_membership'][0]
        self.assertEqual((saved['provider'], saved['id'], saved['url']), ('sc', '404', requested['url']))
        projected = self.client.get('/api/playlists/local:fixture/tracks').json()['tracks'][0]
        self.assertEqual((projected['provider'], projected['id'], projected['url']), ('sc', '404', requested['url']))
        self.assertEqual(projected['status'], 'error')

    def test_gaps_partial_sources_unknown_and_offline_block_without_core(self):
        cases = [([*self.tracks, track('4', 'Missing')], '123'), (self.tracks, 'unknown:source')]
        with patch.object(self.main.rb, 'sync_playlist', side_effect=AssertionError('unresolved source must block')):
            for tracks, key in cases:
                with patch.object(self.main, 'fetch_tracks', return_value=tracks):
                    result = self.client.post('/api/rb/sync', json={**self.body, 'playlist_key': key}).json()
                    self.assertTrue(result['unresolved'])
                    self.assertFalse(result['applied'])
            with patch.object(self.main.soundcloud, 'resolve', return_value={'tracks': [], 'errors': [{'code': 'partial'}]}):
                result = self.client.post('/api/rb/sync', json={**self.body, 'playlist_key': 'sc:likes'}).json()
                self.assertEqual(result['error']['code'], 'source_incomplete')
            self.config['bindings']['offline'] = str(self.base / 'Offline')
            with patch.object(self.main, 'fetch_tracks', return_value=[]):
                result = self.client.post('/api/rb/sync', json=self.body).json()
                self.assertEqual(result['error']['code'], 'music_root_unavailable')

    def test_fresh_ambiguous_and_unreadable_audio_remain_explicit(self):
        wav_file(self.music / 'duplicate' / 'Artist - One.wav')
        self.paths[1].write_bytes(b'unreadable')
        with patch.object(self.main, 'fetch_tracks', return_value=self.tracks), \
             patch.object(self.main.rb, 'sync_playlist', side_effect=AssertionError('invalid source reached core')):
            result = self.client.post('/api/rb/sync', json=self.body).json()
        self.assertEqual(result['error']['code'], 'source_unresolved')
        self.assertEqual([r['code'] for r in result['unresolved']], ['catalog_ambiguous', 'catalog_missing'])

    def test_local_append_keeps_unresolved_members_and_existing_metadata(self):
        from app import library
        local = self.music / 'Local'
        library.save_sidecar(local, {'tracks': {'2': {'provider': 'deezer', 'title': 'Custom', 'artist': 'Manual',
            'position': 1, 'file': 'kept.flac', 'status': 'verify_failed_metadata', 'custom': 'preserve'}}})
        before = library.load_sidecar(local)['tracks']
        requested = [self.tracks[0], track('4', 'Missing')]
        with patch.object(self.main.jobs, 'enqueue', return_value='synthetic-job') as enqueue:
            response = self.client.post('/api/search/download', json={'target_key': 'local:fixture', 'target_title': 'ignored caller title', 'tracks': requested})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([t['id'] for t in enqueue.call_args.args[2]], ['4'])
        persisted = library.load_sidecar(local)
        self.assertEqual(persisted['tracks'], before)
        self.assertEqual([t['id'] for t in persisted['local_membership']], ['2', '1', '4'])
        with patch.object(self.main, 'fetch_tracks', side_effect=AssertionError('local provider access')):
            projected = self.client.get('/api/playlists/local:fixture/tracks').json()['tracks']
        self.assertEqual([t['id'] for t in projected], ['2', '1', '4'])
        self.assertEqual([t['status'] for t in projected], ['error', 'ok', 'missing'])
        with patch.object(self.main.rb, 'sync_playlist', side_effect=AssertionError('unresolved local reached core')):
            result = self.client.post('/api/rb/sync', json={'playlist_key': 'local:fixture', 'playlist_title': 'Local'}).json()
        self.assertEqual(result['error']['code'], 'source_unresolved')

    def test_corrupt_or_lost_local_membership_is_not_a_genuine_empty_source(self):
        local = self.music / 'Local' / '.deckpipe.json'
        for payload in (None, '{}broken'):
            if payload is not None:
                local.write_text(payload, encoding='utf-8')
            with patch.object(self.main.rb, 'sync_playlist', side_effect=AssertionError('lost membership reached core')):
                result = self.client.post('/api/rb/sync', json={'playlist_key': 'local:fixture', 'playlist_title': 'Local'}).json()
            self.assertEqual(result['error']['code'], 'local_membership_unavailable')

    def test_genuine_empty_authoritative_source_remains_an_explicit_preview(self):
        with patch.object(self.main, 'fetch_tracks', return_value=[]), patch.object(self.main.rb, 'sync_playlist', side_effect=self.fake_core) as core:
            result = self.client.post('/api/rb/sync', json=self.body).json()
        self.assertIsNone(result['error'])
        self.assertEqual(core.call_args.args[1], [])
        self.assertEqual(result['plan']['counts']['desired'], 0)

    def test_provider_refresh_bypasses_ui_cache_and_public_total_is_checked(self):
        self.main._cache['tracks']['123'] = (10 ** 12, [])
        self.addCleanup(self.main._cache['tracks'].pop, '123', None)
        with patch.object(self.main, '_fetch_tracks_public', return_value=self.tracks) as fetch:
            self.assertEqual(self.main.fetch_tracks('123'), [])
            self.assertEqual(self.main.fetch_tracks('123', force_refresh=True), self.tracks)
        fetch.assert_called_once()
        response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'data': [], 'total': 3})
        with patch.object(self.main.requests, 'get', return_value=response), self.assertRaises(self.main.HTTPException):
            self.main._fetch_tracks_public('123')

    def test_missing_rekordbox_is_a_full_http_result_not_http_500(self):
        with patch.object(self.main, 'fetch_tracks', return_value=self.tracks), \
             patch.object(self.main.rb, '_PyrekordboxAdapter', side_effect=FileNotFoundError('synthetic absent DB')):
            response = self.client.post('/api/rb/sync', json=self.body)
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result['error']['code'], 'adapter_open_failed')
        self.assertTrue(result['dry_run'])
        self.assertFalse(result['applied'])
        self.assertFalse(result['reconciled'])
        self.assertFalse(result['recovery'])
        self.assertIsNone(result['backup_id'])
        self.assertIsNone(result['backup'])
        self.assertEqual(result['plan']['counts']['desired'], 3)
        self.assertEqual(result['media_state']['mode'], 'blocked')

    def test_provider_list_gains_or_loses_ready_members_invalidates_hash(self):
        with patch.object(self.main, 'fetch_tracks', side_effect=lambda *a, **k: list(self.tracks)), \
             patch.object(self.main.rb, 'sync_playlist', side_effect=self.fake_core):
            preview = self.client.post('/api/rb/sync', json=self.body).json()
            self.tracks.pop()
            result = self.client.post('/api/rb/sync?dry_run=false&confirmation_token=APPLY_REKORDBOX_CHANGES', json={**self.body, 'expected_plan_hash': preview['plan']['hash']}).json()
            self.assertEqual(result['error']['code'], 'stale_preview')
            self.paths.append(wav_file(self.music / 'four' / 'Artist - Four.wav'))
            self.tracks.append(track('4', 'Four'))
            result = self.client.post('/api/rb/sync?dry_run=false&confirmation_token=APPLY_REKORDBOX_CHANGES', json={**self.body, 'expected_plan_hash': preview['plan']['hash']}).json()
            self.assertEqual(result['error']['code'], 'stale_preview')

    def test_soundcloud_playlist_advertised_total_preserves_partial_errors(self):
        sc = self.main.soundcloud
        for count, expected_error in ((0, False), (1, True), (-1, True), ('0', True)):
            with self.subTest(count=count), patch.object(sc, '_api_get', return_value={'kind': 'playlist', 'id': '1', 'tracks': [], 'track_count': count}):
                result = sc._resolve_api('https://soundcloud.com/fixture/set', None)
                self.assertEqual(bool(result['errors']), expected_error)
                self.assertEqual(result['track_count'], count)
        with patch.object(sc, '_api_get', return_value={'kind': 'playlist', 'id': '1', 'track_count': 0}), self.assertRaises(self.main.HTTPException):
            sc._resolve_api('https://soundcloud.com/fixture/set', None)

    def test_soundcloud_likes_missing_entries_and_advertised_total_fail_closed(self):
        sc = self.main.soundcloud
        from unittest.mock import MagicMock
        for info, raises, error in [({'title': 'Likes', 'playlist_count': 0}, True, True),
                                    ({'entries': [], 'playlist_count': 0}, False, False),
                                    ({'entries': [], 'playlist_count': 2}, False, True),
                                    ({'entries': [None], 'playlist_count': 1}, False, True),
                                    ({'entries': [], 'playlist_count': '0'}, False, True)]:
            ydl = MagicMock()
            ydl.__enter__.return_value.extract_info.return_value = info
            with self.subTest(info=info), patch.object(sc, 'sc_oauth_token', return_value='synthetic-test-only'), \
                 patch.object(sc, '_youtube_dl_opts_with_oauth', return_value={}), patch.object(sc, '_bind_cookiejar'), \
                 patch.object(sc.yt_dlp, 'YoutubeDL', return_value=ydl):
                if raises:
                    with self.assertRaises(self.main.HTTPException):
                        sc._resolve_likes('https://soundcloud.com/fixture/likes')
                else:
                    result = sc._resolve_likes('https://soundcloud.com/fixture/likes')
                    self.assertEqual(bool(result['errors']), error)
                    self.assertEqual(result['playlist_count'], info['playlist_count'])


if __name__ == '__main__':
    unittest.main()
