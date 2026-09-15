"""Real decoded-PCM media verification; no user audio or database discovery."""
import importlib
import math
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch


def audio_file(path, *, shift=0, frames=48000, rate=48000):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as output:
        output.setparams((2, 2, rate, frames, 'NONE', 'not compressed'))
        output.writeframes(b''.join(struct.pack('<hh', int(12000 * math.sin((i + shift) * .07)), int(9000 * math.cos((i + shift) * .03))) for i in range(frames)))
    return path


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.media = importlib.import_module('app.rekordbox_media')
        self.temp = tempfile.TemporaryDirectory(prefix='rb-media-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.media.MediaStore(self.root / 'state.json')
        self.wav = audio_file(self.root / 'source.wav')
        self.source = self.root / 'source.flac'
        from app.converter import FFMPEG
        subprocess.run([FFMPEG, '-v', 'error', '-i', str(self.wav), str(self.source)], check=True)

    def test_prepare_reuse_restart_no_clobber_and_pcm_proof(self):
        original_wav = self.wav.read_bytes()
        entry, created = self.store.prepare(self.source)
        self.assertTrue(created)
        self.assertNotEqual(str(self.wav), entry['variant'])
        self.assertEqual(original_wav, self.wav.read_bytes())
        self.assertTrue(self.source.exists())
        verified = self.store.verify(self.source)
        self.assertEqual(verified['pcm']['frames'], 48000)
        self.assertEqual(verified['pcm']['sample_rate'], 48000)
        self.assertEqual(verified['pcm']['channels'], 2)
        entry2, created = self.media.MediaStore(self.store.path).prepare(self.source)
        self.assertFalse(created)
        self.assertEqual(entry, entry2)

    def test_native_wav_preparation_is_an_explicit_owned_variant_then_reuse(self):
        before = self.wav.read_bytes()
        entry, created = self.store.prepare(self.wav)
        self.assertTrue(created)
        self.assertNotEqual(entry['variant'], str(self.wav))
        self.assertEqual(self.wav.read_bytes(), before)
        self.assertEqual(entry['pcm']['frames'], 48000)
        with patch.object(self.media, 'convert_to_wav', side_effect=AssertionError('repeat conversion')):
            again, created = self.store.prepare(self.wav)
        self.assertFalse(created)
        self.assertEqual(again, entry)

    def test_shift_truncation_and_changed_original_refuse_rebind(self):
        entry, _ = self.store.prepare(self.source)
        for shift, frames in ((100, 48000), (0, 47000)):
            with self.subTest(shift=shift, frames=frames):
                bad = audio_file(self.root / f'bad-{shift}.wav', shift=shift, frames=frames)
                with self.assertRaises(self.media.MediaError):
                    self.media.verify_timeline(self.source, bad)
        self.source.write_bytes(self.source.read_bytes() + b'changed original')
        with self.assertRaises(self.media.MediaError):
            self.store.verify(self.source)

    def test_resampled_zero_and_wrong_bit_depth_variants_fail(self):
        from app.converter import FFMPEG
        for name, args in [('resampled', ['-ar', '44100']), ('depth', ['-c:a', 'pcm_s24le'])]:
            bad = self.root / (name + '.wav')
            subprocess.run([FFMPEG, '-v', 'error', '-i', str(self.source), *args, str(bad)], check=True)
            with self.assertRaises(self.media.MediaError):
                self.media.verify_timeline(self.source, bad)
        empty = audio_file(self.root / 'empty.wav', frames=0)
        with self.assertRaises(self.media.MediaError):
            self.media.verify_timeline(self.source, empty)

    def test_decoder_failure_and_changes_during_decode_fail(self):
        from app import rekordbox_media as media
        entry, _ = self.store.prepare(self.source)
        decode = media._decode
        def changed(path, depth):
            result = decode(path, depth)
            if Path(path) == self.source:
                self.source.write_bytes(self.source.read_bytes() + b'changed')
            return result
        with patch.object(media, '_decode', side_effect=changed), self.assertRaises(media.MediaError):
            media.verify_timeline(self.source, entry['variant'])
        with patch.object(media, 'FFMPEG', 'nonexistent-synthetic-decoder'), self.assertRaises((OSError, media.MediaError)):
            media.verify_timeline(self.source, entry['variant'])

    def test_publication_failure_and_pending_mapping_retry(self):
        from app import rekordbox_media as media
        with patch.object(media, 'publish_staged_file', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                self.store.prepare(self.source)
        self.assertEqual(self.store.entry(self.source)['state'], 'publishing')
        self.assertEqual(list(self.root.glob('*.part.wav')), [])
        entry, created = self.store.prepare(self.source)
        self.assertTrue(created)
        self.assertEqual(entry['state'], 'prepared')

    def test_owned_variant_exclusion_requires_exact_durable_pair(self):
        entry, _ = self.store.prepare(self.source)
        self.assertIn(self.media.path_key(entry['variant']), self.store.owned_variants())
        variant = Path(entry['variant'])
        variant.write_bytes(variant.read_bytes() + b'edited user file')
        self.assertNotIn(self.media.path_key(variant), self.store.owned_variants())

    def test_crash_after_publication_reuses_pending_verified_pair(self):
        from app import rekordbox_media as media
        write = media.atomic_write_json
        def fail_complete(path, data):
            if any(e['state'] == 'prepared' for e in data['entries'].values()):
                raise OSError('synthetic bookkeeping failure')
            write(path, data)
        with patch.object(media, 'atomic_write_json', side_effect=fail_complete), self.assertRaises(OSError):
            self.store.prepare(self.source)
        pending = self.store.entry(self.source)
        self.assertEqual(pending['state'], 'publishing')
        self.assertTrue(Path(pending['variant']).is_file())
        with patch.object(media, 'convert_to_wav', side_effect=AssertionError('must reuse')):
            recovered, created = media.MediaStore(self.store.path).prepare(self.source)
        self.assertFalse(created)
        self.assertEqual(recovered['variant'], pending['variant'])
        self.assertEqual(recovered['state'], 'prepared')

    def test_real_mp3_and_supported_output_depths_preserve_native_pcm(self):
        from app.converter import FFMPEG
        mp3 = self.root / 'lossy.mp3'
        subprocess.run([FFMPEG, '-v', 'error', '-i', str(self.wav), '-c:a', 'libmp3lame', str(mp3)], check=True)
        entry, _ = self.store.prepare(mp3)
        self.assertEqual(entry['pcm']['frames'], 48000)
        for depth in (24, 32):
            with self.subTest(depth=depth):
                store = self.media.MediaStore(self.root / f'depth-{depth}.json')
                entry, _ = store.prepare(self.source, depth)
                self.assertEqual(entry['pcm']['bit_depth'], depth)
                self.assertEqual(entry['pcm']['frames'], 48000)
                store.verify(self.source)

    def test_malformed_mapping_and_nonzero_decoder_exit_fail_closed(self):
        import io
        import json
        from types import SimpleNamespace
        for payload in ([], {'version': 1, 'entries': {'invalid': []}}):
            self.store.path.write_text(json.dumps(payload), encoding='utf-8')
            with self.assertRaises(self.media.MediaError):
                self.store.load()
        process = SimpleNamespace(stdout=io.BytesIO(b'\0' * 192000), wait=lambda: 1, poll=lambda: 1)
        with patch.object(self.media.subprocess, 'Popen', return_value=process), self.assertRaises(self.media.MediaError) as caught:
            self.media._decode(self.source, 16)
        self.assertEqual(caught.exception.code, 'media_decode_failed')


class DatabaseMediaTests(unittest.TestCase):
    """Production HTTP/service/catalog/converter/core + generated SQLCipher/ANLZ."""
    def setUp(self):
        from qa.tests.rekordbox_fixture import Fixture
        from qa.tests.test_rekordbox_catalog import Client
        from app import main, library, catalog_service, deezer_client, rekordbox
        from app.converter import FFMPEG
        self.fixture = Fixture()
        self.main, self.rb = main, rekordbox
        self.root = self.fixture.root
        self.music = self.root / 'Music'
        self.music.mkdir()
        self.config = {'music_root': str(self.music), 'local_sources': [{'id': 'fixture', 'title': 'Local'}]}
        for module in (main, library, catalog_service, deezer_client):
            p = patch.object(module, 'load_config', side_effect=lambda: self.config)
            p.start()
            self.addCleanup(p.stop)
        for module, field, value in [(deezer_client, 'ROOT', self.root / 'state'), (rekordbox, '_PyrekordboxAdapter', self.fixture.factory)]:
            p = patch.object(module, field, value)
            p.start()
            self.addCleanup(p.stop)
        self.paths = []
        for index, name in enumerate(('One', 'Two'), 1):
            wav = audio_file(self.root / f'generated-{index}.wav', shift=index * 11)
            flac = self.music / ('folder-' + str(index)) / ('Artist - ' + name + '.flac')
            flac.parent.mkdir()
            subprocess.run([FFMPEG, '-v', 'error', '-i', str(wav), str(flac)], check=True)
            from mutagen.flac import FLAC
            tags = FLAC(flac)
            tags['title'], tags['artist'] = name, 'Artist'
            tags.save()
            self.paths.append(flac)
        self.anlz, self.tail = self.fixture.analysis()
        encoded = str(self.paths[0]).replace('\\', '/').encode('utf-16-be') + b'\0\0'
        tag = b'PPTH' + struct.pack('>III', 16, 16 + len(encoded), len(encoded)) + encoded
        payload = tag + self.tail
        self.anlz.write_bytes(b'PMAI' + struct.pack('>II', 28, 28 + len(payload)) + bytes(16) + payload)
        adapter = self.fixture.factory()
        try:
            for index, path in enumerate(self.paths, 1):
                content = adapter.db.get_content(ID=str(index))
                content.FolderPath, content.FileNameL = str(path), path.name
                content.FileType, content.SampleRate, content.BitDepth = 5, 48000, 16
                content.FileSize = path.stat().st_size
            adapter.db.commit()
        finally:
            adapter.close()
        self.client = Client(main.app)
        self.tracks = [dict(id=str(i), provider='deezer', title=t, artist='Artist', duration=1) for i, t in enumerate(('One', 'Two'), 1)]
        with patch.object(main.jobs, 'enqueue', side_effect=AssertionError('must reuse global files')):
            response = self.client.post('/api/search/download', json={'target_key': 'local:fixture', 'target_title': 'Local', 'tracks': self.tracks})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['already_present'], 2)
        self.body = dict(playlist_key='local:fixture', playlist_title='Likes', playlist_id=self.fixture.target_id)

    def preview(self, to_wav=True):
        response = self.client.post('/api/flip', json={**self.body, 'to_wav': to_wav}).json()
        self.assertIsNone(response['error'], response)
        return response

    def apply(self, preview, to_wav=True):
        return self.client.post('/api/flip?dry_run=false&confirmation_token=APPLY_REKORDBOX_CHANGES',
                                json={**self.body, 'to_wav': to_wav, 'expected_plan_hash': preview['plan']['hash']}).json()

    def test_complete_prepare_refresh_flip_restart_sync_revert_preserves_content_cues_anlz(self):
        from app import library, catalog_service, rekordbox_media
        before, anlz_before = self.fixture.protected(), self.anlz.read_bytes()
        sync = self.client.post('/api/rb/sync', json=self.body).json()
        self.assertIsNone(sync['error'], sync)
        unprepared = self.client.post('/api/flip', json={**self.body, 'to_wav': True}).json()
        self.assertEqual(unprepared['error']['code'], 'wav_not_prepared')
        self.assertEqual(before, self.fixture.protected())
        prepared = self.client.post('/api/rb/prepare-wav', json=self.body).json()
        self.assertEqual(prepared['prepared'], 2, prepared)
        self.assertEqual(before, self.fixture.protected())
        self.assertEqual(anlz_before, self.anlz.read_bytes())
        # Owned variants coexist in the common catalog without ambiguity.
        rows = catalog_service.playlist_tracks(library.playlist_dir('local:fixture', 'Local'), self.tracks)
        self.assertEqual([r['status'] for r in rows], ['ok', 'ok'])
        self.assertEqual([r['file_path'] for r in rows], [str(p) for p in self.paths])
        # A fresh inventory with no cached identities must also exclude the
        # verified variant carrying exactly the same title/artist metadata.
        from app.library_catalog import MusicCatalog
        fresh_catalog = MusicCatalog(self.root / 'fresh-catalog.sqlite3')
        scanned = fresh_catalog.scan([self.music])
        self.assertEqual(scanned['files'], 2)
        self.assertEqual([fresh_catalog.match(t)['status'] for t in self.tracks], ['ok', 'ok'])
        preview = self.preview()
        self.assertEqual(preview['media_state']['mode'], 'original')
        self.assertTrue(preview['plan']['shared_content'])
        applied = self.apply(preview)
        self.assertTrue(applied['applied'], applied)
        self.assertTrue(applied['reconciled'], applied)
        self.assertEqual(applied['media_state']['mode'], 'wav', applied)
        actual = self.fixture.protected()
        self.assertEqual(actual['cues'], before['cues'])
        self.assertEqual(actual['other'], before['other'])
        self.assertTrue(self.anlz.read_bytes().endswith(self.tail))
        for old, new in zip(before['content'][:2], actual['content'][:2]):
            self.assertEqual(new['ID'], old['ID'])
            self.assertEqual(new['Commnt'], old['Commnt'])
            self.assertEqual((new['FileType'], new['SampleRate'], new['BitDepth']), (11, 48000, 16))
            self.assertTrue(Path(old['FolderPath']).is_file())
        # New service/store construction derives state from DB, not a flag.
        self.assertEqual(len(rekordbox_media.MediaStore().load()['entries']), 2)
        # A separate Python process receives only the generated fixture root/ID.
        # Its adapter uses explicit synthetic connection parameters internally.
        script = '''
import json, sys
from pathlib import Path
from unittest.mock import patch
from qa.tests.rekordbox_fixture import Fixture
from app import main, library, catalog_service, deezer_client, rekordbox
root = Path(sys.argv[1])
fixture = Fixture(root=root, initialize=False)
cfg = {'music_root': str(root / 'Music'), 'local_sources': [{'id': 'fixture', 'title': 'Local'}]}
with patch.object(main, 'load_config', return_value=cfg), patch.object(library, 'load_config', return_value=cfg), patch.object(catalog_service, 'load_config', return_value=cfg), patch.object(deezer_client, 'ROOT', root / 'state'), patch.object(rekordbox, '_PyrekordboxAdapter', fixture.factory):
    print(json.dumps(main.api_rb_media_state('local:fixture', 'Likes', sys.argv[2])['media_state']))
'''
        child = subprocess.run([sys.executable, '-c', script, str(self.root), self.fixture.target_id], capture_output=True, text=True)
        self.assertEqual(child.returncode, 0, child.stderr)
        import json
        self.assertEqual(json.loads(child.stdout)['mode'], 'wav')
        state = self.client.get('/api/rb/media-state?playlist_key=local:fixture&playlist_title=Likes&playlist_id=' + self.fixture.target_id).json()
        self.assertEqual(state['media_state']['mode'], 'wav', state)
        normal = self.client.post('/api/rb/sync', json=self.body).json()
        self.assertEqual(normal['plan']['counts']['path'], 0, normal)
        revert = self.preview(False)
        reverted = self.apply(revert, False)
        self.assertTrue(reverted['reconciled'], reverted)
        self.assertEqual(reverted['media_state']['mode'], 'original')
        after = self.fixture.protected()
        self.assertEqual(after['cues'], before['cues'])
        for old, new in zip(before['content'][:2], after['content'][:2]):
            for field in ('ID', 'Commnt', 'FolderPath', 'FileNameL', 'FileType', 'SampleRate', 'BitDepth'):
                self.assertEqual(new[field], old[field], field)
        self.assertEqual(anlz_before, self.anlz.read_bytes())

    def test_changed_original_or_variant_after_preview_never_mutates(self):
        from app.rekordbox_media import MediaStore
        self.client.post('/api/rb/prepare-wav', json=self.body)
        preview, before = self.preview(), self.fixture.protected()
        original = self.paths[0].read_bytes()
        self.paths[0].write_bytes(original + b'changed tags')
        result = self.apply(preview)
        self.assertFalse(result['applied'], result)
        self.assertTrue(result['error'])
        self.assertEqual(before, self.fixture.protected())
        self.paths[0].write_bytes(original)
        entry = MediaStore().entry(self.paths[0])
        Path(entry['variant']).write_bytes(b'truncated')
        result = self.apply(preview)
        self.assertFalse(result['applied'], result)
        self.assertEqual(before, self.fixture.protected())

    def test_timeline_proof_is_bound_and_rechecked_under_core_lock(self):
        from app.rekordbox_media import MediaStore, file_digest
        self.client.post('/api/rb/prepare-wav', json=self.body)
        preview, before = self.preview(), self.fixture.protected()
        entry = MediaStore().entry(self.paths[0])
        desired = preview['plan']['desired_resolved'][0]
        self.assertEqual(desired['verified_path_sha256'], file_digest(entry['variant']))
        self.assertEqual(desired['verified_source_sha256'], file_digest(entry['source']))
        def factory():
            adapter = self.fixture.factory()
            begin = adapter.begin
            def changed_before_locked_plan():
                begin()
                path = Path(entry['variant'])
                path.write_bytes(path.read_bytes() + b'changed at lock')
            adapter.begin = changed_before_locked_plan
            return adapter
        with patch.object(self.rb, '_PyrekordboxAdapter', side_effect=factory):
            result = self.apply(preview)
        self.assertFalse(result['applied'], result)
        self.assertIn(result['error']['code'], ('stale_preview', 'media_verification_changed', 'unresolved_items'))
        self.assertEqual(before, self.fixture.protected())

    def test_post_commit_state_failure_and_callback_failure_preserve_applied_truth(self):
        from app import rekordbox_service
        self.client.post('/api/rb/prepare-wav', json=self.body)
        preview = self.preview()
        with patch.object(rekordbox_service, '_result_media_state', side_effect=OSError('state callback failed')):
            result = self.apply(preview)
        self.assertTrue(result['applied'], result)
        self.assertTrue(result['reconciled'], result)
        self.assertEqual(result['media_state']['mode'], 'blocked')
        state = self.main.api_rb_media_state('local:fixture', 'Likes', self.fixture.target_id)
        self.assertEqual(state['media_state']['mode'], 'wav', state)
        revert = self.preview(False)
        sync = self.rb.sync_playlist
        def callback_failed(name, desired, **kwargs):
            if not kwargs.get('dry_run', True):
                kwargs['on_reconciled'] = lambda result: (_ for _ in ()).throw(OSError('synthetic callback'))
            return sync(name, desired, **kwargs)
        with patch.object(self.rb, 'sync_playlist', side_effect=callback_failed):
            reverted = self.apply(revert, False)
        self.assertTrue(reverted['applied'], reverted)
        self.assertTrue(reverted['reconciled'], reverted)
        self.assertEqual(reverted['error']['code'], 'callback_failed', reverted)
        self.assertEqual(reverted['media_state']['mode'], 'original', reverted)

    def test_normal_sync_into_other_and_new_targets_retains_global_wav_content_and_replays(self):
        from app.rekordbox_media import MediaStore
        self.client.post('/api/rb/prepare-wav', json=self.body)
        flipped = self.apply(self.preview())
        self.assertTrue(flipped['reconciled'], flipped)
        before, analysis = self.fixture.protected(), self.anlz.read_bytes()
        variants = {str(i): MediaStore().entry(path)['variant'] for i, path in enumerate(self.paths, 1)}
        for target in ({'playlist_title': 'Other', 'playlist_id': self.fixture.other_id}, {'playlist_title': 'New Target'}):
            with self.subTest(target=target):
                body = {**self.body, **target}
                if 'playlist_id' not in target:
                    body.pop('playlist_id')
                preview = self.client.post('/api/rb/sync', json=body).json()
                self.assertIsNone(preview['error'], preview)
                self.assertEqual(preview['plan']['counts']['path'], 0, preview)
                self.assertEqual(preview['plan']['shared_content'], [])
                for item in preview['plan']['desired_resolved']:
                    self.assertEqual(item['path'], variants[item['content_id']])
                result = self.client.post('/api/rb/sync?dry_run=false&confirmation_token=APPLY_REKORDBOX_CHANGES',
                                          json={**body, 'expected_plan_hash': preview['plan']['hash']}).json()
                self.assertTrue(result['reconciled'], result)
                self.assertEqual(result['media_state']['mode'], 'wav', result)
                actual = self.fixture.protected()
                self.assertEqual(actual['content'], before['content'])
                self.assertEqual(actual['cues'], before['cues'])
                self.assertEqual(actual['other'], before['other'])
                self.assertEqual(self.anlz.read_bytes(), analysis)
                adapter = self.fixture.factory()
                try:
                    for name in ('Likes', target['playlist_title']):
                        membership = adapter.snapshot_playlist(name)
                        self.assertEqual([row['content_id'] for row in membership], ['1', '2'])
                        self.assertEqual([row['path'] for row in membership], [variants['1'], variants['2']])
                finally:
                    adapter.close()
                fingerprint = self.fixture.inventory()
                replay = self.client.post('/api/rb/sync?dry_run=false&confirmation_token=APPLY_REKORDBOX_CHANGES',
                                          json={**body, 'expected_plan_hash': preview['plan']['hash']}).json()
                self.assertTrue(replay['unchanged'], replay)
                self.assertFalse(replay['applied'], replay)
                self.assertIsNone(replay['backup_id'])
                self.assertEqual(fingerprint, self.fixture.inventory())

    def test_normal_sync_keeps_new_content_original_even_when_variant_is_prepared(self):
        from app.converter import FFMPEG
        from app.rekordbox_media import MediaStore
        self.client.post('/api/rb/prepare-wav', json=self.body)
        self.assertTrue(self.apply(self.preview())['reconciled'])
        before, analysis = self.fixture.protected(), self.anlz.read_bytes()
        wav = audio_file(self.root / 'new-source.wav', shift=333)
        original = self.music / 'new-folder' / 'Artist - New Original.flac'
        original.parent.mkdir()
        subprocess.run([FFMPEG, '-v', 'error', '-i', str(wav), str(original)], check=True)
        new_track = dict(id='new-original', provider='deezer', title='New Original', artist='Artist', duration=1)
        with patch.object(self.main.jobs, 'enqueue', side_effect=AssertionError('reuse must not download')):
            result = self.client.post('/api/search/download', json={'target_key': 'local:fixture', 'target_title': 'Local', 'tracks': [new_track]}).json()
        self.assertIsNone(result['job_id'])
        prepared = self.client.post('/api/rb/prepare-wav', json=self.body).json()
        self.assertEqual((prepared['prepared'], prepared['reused']), (1, 2), prepared)
        self.assertIsNotNone(MediaStore().entry(original))
        body = dict(playlist_key='local:fixture', playlist_title='New With Original')
        preview = self.client.post('/api/rb/sync', json=body).json()
        self.assertIsNone(preview['error'], preview)
        self.assertEqual(preview['plan']['counts']['path'], 0)
        desired = next(t for t in preview['plan']['desired_resolved'] if t['provider_id'] == 'deezer:new-original')
        self.assertFalse(desired.get('content_id'))
        self.assertEqual(desired['path'], str(original))
        applied = self.client.post('/api/rb/sync?dry_run=false&confirmation_token=APPLY_REKORDBOX_CHANGES',
                                   json={**body, 'expected_plan_hash': preview['plan']['hash']}).json()
        self.assertTrue(applied['reconciled'], applied)
        self.assertEqual(applied['media_state']['mode'], 'mixed')
        actual = self.fixture.protected()
        retained = {row['ID']: row for row in actual['content']}
        for row in before['content']:
            self.assertEqual(retained[row['ID']], row)
        added = [row for row in actual['content'] if row['ID'] not in {r['ID'] for r in before['content']}]
        self.assertEqual(len(added), 1)
        self.assertEqual((added[0]['FolderPath'], added[0]['FileType']), (str(original), 5))
        self.assertEqual(actual['cues'], before['cues'])
        self.assertEqual(actual['other'], before['other'])
        self.assertEqual(self.anlz.read_bytes(), analysis)
        fingerprint = self.fixture.inventory()
        replay = self.client.post('/api/rb/sync?dry_run=false&confirmation_token=APPLY_REKORDBOX_CHANGES',
                                  json={**body, 'expected_plan_hash': preview['plan']['hash']}).json()
        self.assertTrue(replay['unchanged'], replay)
        self.assertFalse(replay['applied'], replay)
        self.assertEqual(fingerprint, self.fixture.inventory())


if __name__ == '__main__':
    unittest.main()
