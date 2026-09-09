import importlib
import json
import os
import tempfile
import unicodedata
import unittest
import wave
from pathlib import Path

from mutagen.id3 import TIT2, TPE1
from mutagen.wave import WAVE


def wav_file(path, *, title=None, artist=None, seconds=1):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b'\x00\x00' * (8000 * seconds))
    if title or artist:
        audio = WAVE(path)
        audio.add_tags()
        if title:
            audio.tags.add(TIT2(encoding=3, text=title))
        if artist:
            audio.tags.add(TPE1(encoding=3, text=artist))
        audio.save()
    return path


def track(tid, title, artist='Artist', provider='deezer', duration=1):
    return dict(id=str(tid), title=title, artist=artist, provider=provider, duration=duration)


class LibraryCatalogTests(unittest.TestCase):
    def setUp(self):
        try:
            self.module = importlib.import_module('app.library_catalog')
        except ModuleNotFoundError as exc:
            self.fail(f'Persistent common music catalog has not been implemented: {exc.name}')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'Music'
        self.root.mkdir()
        self.db = self.base / 'state' / 'catalog.sqlite3'
        self.catalog = self.module.MusicCatalog(self.db)

    def test_likes_resolve_files_across_folders_and_survive_restart(self):
        one = wav_file(self.root / 'Warmup' / 'Artist - One.wav')
        two = wav_file(self.root / 'Peak time' / 'Artist - Two.wav')
        self.assertEqual(self.catalog.scan([self.root])['files'], 2)
        results = [self.catalog.match(track(i, name)) for i, name in enumerate(('One', 'Two', 'Three'))]
        self.assertEqual([r['status'] for r in results], ['ok', 'ok', 'missing'])
        self.assertEqual(Path(results[0]['locations'][0]['path']), one)
        self.assertEqual(Path(results[1]['locations'][0]['folder']), two.parent)
        restarted = self.module.MusicCatalog(self.db)
        self.assertEqual(restarted.match(track(0, 'One'))['status'], 'ok')
        self.assertEqual(sorted(p for p in self.root.rglob('*') if p.is_file()), sorted([one, two]))

    def test_manual_tags_are_read_even_when_filename_is_unrelated(self):
        audio = wav_file(self.root / 'old' / 'recording 42.wav', title='Море (Extended Mix)', artist='Ёлка')
        self.catalog.scan([self.root])
        result = self.catalog.match(track(42, 'Море (Extended Mix)', 'Ёлка'))
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(Path(result['locations'][0]['path']), audio)
        self.assertEqual(result['match_source'], 'metadata')

    def test_title_only_different_artist_and_different_mix_do_not_match(self):
        wav_file(self.root / 'Other Artist - Home.wav')
        wav_file(self.root / 'Artist - Song.wav')
        wav_file(self.root / 'Only title.wav')
        self.catalog.scan([self.root])
        for requested in [track(1, 'Home', 'Wanted Artist'), track(2, 'Song (Extended Mix)'), track(3, 'Only title')]:
            self.assertEqual(self.catalog.match(requested)['status'], 'missing')

    def test_duration_mismatch_does_not_auto_match_same_name(self):
        wav_file(self.root / 'Artist - Song.wav', seconds=1)
        self.catalog.scan([self.root])
        self.assertEqual(self.catalog.match(track(1, 'Song', duration=20))['status'], 'missing')

    def test_confirmed_owner_does_not_make_other_sidecar_identity_trusted(self):
        from app import library
        audio = wav_file(self.root / 'Artist - Song.wav')
        library.save_sidecar(self.root, {'tracks': {
            '1': {'provider': 'deezer', 'status': 'ok', 'file': audio.name},
            '2': {'provider': 'deezer', 'status': 'ok', 'file': audio.name}}})
        self.catalog.scan([self.root])
        self.assertEqual(self.catalog.match(track(2, 'Song'))['status'], 'ambiguous')
        self.catalog.confirm(track(1, 'Song'), str(audio))
        self.assertEqual(self.catalog.match(track(1, 'Song'))['status'], 'ok')
        self.assertEqual(self.catalog.match(track(2, 'Song'))['status'], 'ambiguous')

    def test_filename_underscores_are_separators_without_losing_mix(self):
        wav_file(self.root / 'Artist_Name - Song_Name_(Extended_Mix).wav')
        self.catalog.scan([self.root])
        self.assertEqual(self.catalog.match(track(1, 'Song Name (Extended Mix)', 'Artist Name'))['status'], 'ok')
        self.assertEqual(self.catalog.match(track(2, 'Song Name', 'Artist Name'))['status'], 'missing')

    def test_replacing_audio_invalidates_old_identity_even_with_unchanged_sidecar(self):
        from app import library
        audio = wav_file(self.root / 'Artist - Old.wav')
        library.save_sidecar(self.root, {'tracks': {'1': {'provider': 'deezer', 'status': 'ok', 'file': audio.name}}})
        self.catalog.scan([self.root])
        self.assertEqual(self.catalog.match(track(1, 'Old'))['status'], 'ok')
        replacement = wav_file(self.base / 'Replacement.wav', title='New', artist='Different', seconds=4)
        os.replace(replacement, audio)
        self.catalog.scan([self.root])
        self.assertEqual(self.catalog.match(track(1, 'Old'))['status'], 'missing')
        self.catalog.scan([self.root])
        self.assertEqual(self.catalog.match(track(1, 'Old'))['status'], 'missing')
        self.assertEqual(self.catalog.match(track(2, 'New', 'Different', duration=4))['status'], 'ok')

    def test_sidecar_waits_for_missing_file_before_recording_import(self):
        from app import library
        library.save_sidecar(self.root, {'tracks': {'1': {'provider': 'deezer', 'status': 'ok', 'file': 'unrelated.wav'}}})
        self.catalog.scan([self.root])
        self.assertEqual(self.catalog.match(track(1, 'Original'))['status'], 'missing')
        wav_file(self.root / 'unrelated.wav')
        self.catalog.scan([self.root])
        self.assertEqual(self.catalog.match(track(1, 'Original'))['status'], 'ok')

    def test_ambiguous_candidates_require_explicit_choice(self):
        first = wav_file(self.root / 'One' / 'Artist - Song.wav')
        second = wav_file(self.root / 'Two' / 'Artist - Song.wav')
        requested = track(1, 'Song')
        self.catalog.scan([self.root])
        result = self.catalog.match(requested)
        self.assertEqual(result['status'], 'ambiguous')
        self.assertEqual(len(result['locations']), 2)
        self.catalog.confirm(requested, str(second))
        self.assertEqual(Path(self.catalog.match(requested)['locations'][0]['path']), second)
        self.assertTrue(first.exists())
        self.assertEqual(self.module.MusicCatalog(self.db).match(requested)['status'], 'ok')

    def test_confirmation_rejects_unindexed_or_outside_file(self):
        outside = wav_file(self.base / 'Elsewhere' / 'Artist - Song.wav')
        self.catalog.scan([self.root])
        with self.assertRaises(ValueError):
            self.catalog.confirm(track(1, 'Song'), str(outside))

    def test_sidecar_id_matches_without_relying_on_name(self):
        audio = wav_file(self.root / 'Set' / 'some filename.wav')
        (audio.parent / '.deckpipe.json').write_text(json.dumps({'tracks': {
            'sc:42': {'provider': 'sc', 'title': 'Song', 'artist': 'Artist', 'file': audio.name, 'status': 'ok'}
        }}), encoding='utf-8')
        self.catalog.scan([self.root])
        result = self.catalog.match(track(42, 'Song', provider='sc'))
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['match_source'], 'sidecar')
        self.assertEqual(self.catalog.match(track(42, 'Song', provider='deezer'))['status'], 'missing')

    def test_different_provider_id_does_not_steal_owned_file_by_same_title(self):
        audio = wav_file(self.root / 'Artist - Song.wav')
        (audio.parent / '.deckpipe.json').write_text(json.dumps({'tracks': {
            '1': {'provider': 'deezer', 'title': 'Song', 'artist': 'Artist', 'file': audio.name, 'status': 'ok'}
        }}), encoding='utf-8')
        self.catalog.scan([self.root])
        self.assertEqual(self.catalog.match(track(1, 'Song'))['status'], 'ok')
        self.assertEqual(self.catalog.match(track(2, 'Song'))['status'], 'missing')

    def test_confirmed_file_move_is_recovered_without_network(self):
        old = wav_file(self.root / 'Set A' / 'unknown.wav')
        self.catalog.scan([self.root])
        requested = track(7, 'Song')
        self.catalog.confirm(requested, str(old))
        new = self.root / 'Set B' / 'renamed.wav'
        new.parent.mkdir()
        old.rename(new)
        self.catalog.scan([self.root])
        result = self.catalog.match(requested)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(Path(result['locations'][0]['path']), new)

    def test_automatic_match_remembers_identity_after_filename_changes(self):
        old = wav_file(self.root / 'Artist - Song.wav')
        requested = track(9, 'Song')
        self.catalog.scan([self.root])
        self.assertEqual(self.catalog.match(requested)['status'], 'ok')
        new = self.root / 'A different filename.wav'
        old.rename(new)
        self.catalog.scan([self.root])
        result = self.catalog.match(requested)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(Path(result['locations'][0]['path']), new)

    def test_conflicting_sidecar_ids_do_not_both_become_ready(self):
        audio = wav_file(self.root / 'Artist - Song.wav')
        entries = {str(i): dict(provider='deezer', file=audio.name, status='ok') for i in (1, 2)}
        (self.root / '.deckpipe.json').write_text(json.dumps({'tracks': entries}), encoding='utf-8')
        self.catalog.scan([self.root])
        self.assertEqual(self.catalog.match(track(1, 'Song'))['status'], 'ambiguous')
        self.assertEqual(self.catalog.match(track(2, 'Song'))['status'], 'ambiguous')

    def test_changed_file_is_not_ready_until_revalidated(self):
        audio = wav_file(self.root / 'Artist - Song.wav')
        self.catalog.scan([self.root])
        requested = track(1, 'Song')
        self.assertEqual(self.catalog.match(requested)['status'], 'ok')
        audio.write_bytes(b'corrupted file replacement')
        self.assertEqual(self.catalog.match(requested)['status'], 'missing')
        self.catalog.scan([self.root])
        self.assertEqual(self.catalog.match(requested)['status'], 'missing')

    def test_missing_file_is_rechecked_even_without_rescan(self):
        audio = wav_file(self.root / 'Artist - Song.wav')
        self.catalog.scan([self.root])
        self.catalog.confirm(track(1, 'Song'), str(audio))
        audio.unlink()
        self.assertEqual(self.catalog.match(track(1, 'Song'))['status'], 'missing')

    def test_offline_root_is_distinct_from_missing_track(self):
        wav_file(self.root / 'Artist - Song.wav')
        self.catalog.scan([self.root])
        self.root.rename(self.base / 'Disconnected')
        self.catalog.scan([self.root])
        self.assertEqual(self.catalog.match(track(1, 'Song'))['status'], 'offline')
        self.assertEqual(self.catalog.match(track(2, 'Unknown'))['status'], 'offline')

    def test_removed_root_cannot_remain_a_source_of_ready_matches(self):
        wav_file(self.root / 'Artist - Song.wav')
        self.catalog.scan([self.root])
        other = self.base / 'Different music'
        other.mkdir()
        self.catalog.scan([other])
        self.assertEqual(self.catalog.match(track(1, 'Song'))['status'], 'missing')

    def test_overlapping_roots_and_repeated_scan_do_not_duplicate_files(self):
        wav_file(self.root / 'Set' / 'Artist - Song.wav')
        for _ in range(2):
            result = self.catalog.scan([self.root, self.root / 'Set', self.root])
            self.assertEqual(result['files'], 1)
        self.assertEqual(len(self.catalog.match(track(1, 'Song'))['locations']), 1)

    def test_nfc_nfd_and_case_are_equivalent_but_mix_is_preserved(self):
        wav_file(self.root / 'Artist - Café (Live).wav')
        self.catalog.scan([self.root])
        self.assertEqual(self.catalog.match(track(1, unicodedata.normalize('NFD', 'CAFÉ (Live)'), 'ARTIST'))['status'], 'ok')
        self.assertEqual(self.catalog.match(track(2, 'Café (Studio)'))['status'], 'missing')

    def test_partial_and_invalid_media_are_not_ready(self):
        (self.root / 'Artist - Song.wav').write_bytes(b'not a valid wav')
        wav_file(self.root / 'Artist - Other.deckpipe-stage-test.part.wav')
        self.catalog.scan([self.root])
        self.assertEqual(self.catalog.match(track(1, 'Song'))['status'], 'missing')
        self.assertEqual(self.catalog.match(track(2, 'Other'))['status'], 'missing')

    def test_symlink_escape_is_not_indexed(self):
        outside = wav_file(self.base / 'outside' / 'Artist - Song.wav')
        link = self.root / 'linked'
        try:
            link.symlink_to(outside.parent, target_is_directory=True)
        except OSError:
            self.skipTest('Creating symlinks is unavailable for this Windows account')
        self.catalog.scan([self.root])
        self.assertEqual(self.catalog.match(track(1, 'Song'))['status'], 'missing')

    def test_global_root_is_explicit_and_bindings_are_preserved(self):
        with self.assertRaises(self.module.MusicRootRequired):
            self.module.configured_music_roots({})
        with self.assertRaises(self.module.MusicRootRequired):
            self.module.configured_music_roots({'music_root': ''})
        extra = self.base / 'Different playlist folder name'
        extra.mkdir()
        roots = self.module.configured_music_roots({'music_root': str(self.root), 'bindings': {'123': str(extra)}})
        self.assertEqual(set(roots), {self.root, extra})


if __name__ == '__main__':
    unittest.main()
