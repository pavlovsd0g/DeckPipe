"""Stage C synthetic-only SQLCipher integration tests."""
import unittest
from app import rekordbox as rb
from qa.tests.rekordbox_fixture import Fixture
from app.rekordbox_recovery import file_hash, Journal
from unittest.mock import patch
import shutil
import json
import os
import subprocess
import sys
from pathlib import Path
from sqlalchemy import create_engine
from sqlcipher3 import dbapi2 as sqlcipher
from qa.tests.rekordbox_fixture import SYNTHETIC_KEY
from app.rekordbox_adapter import PyrekordboxAdapter, canonical_path


class PreviewContractTests(unittest.TestCase):
    def test_missing_hash_rejected_before_adapter_creation(self):
        called = []
        result = rb.sync_playlist('Likes', [], dry_run=False,
            confirmation_token=rb.APPLY_CONFIRMATION_TOKEN,
            expected_plan_hash=None, adapter_factory=lambda: called.append(True))
        self.assertEqual(result['error']['code'], 'preview_required')
        self.assertEqual(called, [])


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()

    def preview(self, desired=None, **kwargs):
        return rb.sync_playlist('Likes', desired or self.fixture.desired(), adapter_factory=self.fixture.factory, **kwargs)

    def apply(self, preview, desired=None, **kwargs):
        return self.preview(desired, dry_run=False, confirmation_token=rb.APPLY_CONFIRMATION_TOKEN,
            expected_plan_hash=preview['plan']['hash'], **kwargs)

    def test_preview_preserves_all_files_and_database(self):
        before = {str(p): file_hash(p) for p in self.fixture.root.rglob('*') if p.is_file()}
        result = self.preview()
        self.assertIsNone(result['error'], result)
        self.assertEqual(result['plan']['counts']['add'], 1)
        self.assertEqual(before, {str(p): file_hash(p) for p in self.fixture.root.rglob('*') if p.is_file()})

    def test_preserves_content_cues_other_playlist_and_membership_ids(self):
        before = self.fixture.protected()
        result = self.apply(self.preview())
        self.assertIsNone(result['error'], result)
        self.assertTrue(result['reconciled'])
        self.assertEqual(before, self.fixture.protected())
        adapter = self.fixture.factory()
        try:
            rows = adapter.snapshot_playlist('Likes')
            self.assertEqual([r['content_id'] for r in rows], ['1', '2', '3'])
            self.assertEqual([r['membership_id'] for r in rows[:2]], ['member-1', 'member-2'])
        finally:
            adapter.close()
        inventory = self.fixture.inventory()
        files = {str(p): file_hash(p) for p in self.fixture.root.rglob('*') if p.is_file()}
        again = self.apply(result)
        self.assertTrue(again['unchanged'], again)
        self.assertIsNone(again['backup_id'])
        self.assertEqual(inventory, self.fixture.inventory())
        self.assertEqual(files, {str(p): file_hash(p) for p in self.fixture.root.rglob('*') if p.is_file()})

    def test_stale_media_and_database_refuse_before_backup(self):
        preview = self.preview()
        with self.fixture.paths[0].open('ab') as out:
            out.write(b'changed')
        result = self.apply(preview)
        self.assertEqual(result['error']['code'], 'stale_preview', result)
        self.assertFalse((self.fixture.root / 'deckpipe-rekordbox-backups').exists())

    def test_comment_is_never_identity(self):
        adapter = self.fixture.factory()
        adapter.db.get_content(ID='3').Commnt = 'deezer:1'
        adapter.db.commit()
        adapter.close()
        result = self.apply(self.preview())
        self.assertIsNone(result['error'], result)

    def test_ambiguous_target_and_duplicate_path_fail_closed(self):
        adapter = self.fixture.factory()
        adapter.db.create_playlist('Likes')
        adapter.db.commit()
        adapter.close()
        result = self.preview()
        self.assertEqual(result['error']['code'], 'ambiguous_playlist_target')
        selected = self.preview(playlist_id=self.fixture.target_id)
        self.assertIsNone(selected['error'], selected)

    def test_relocate_cannot_modify_membership(self):
        result = self.preview(operation_kind='relocate')
        self.assertIn({'code': 'relocate_membership_change'}, result['unresolved'])

    def test_commit_failure_restores_and_verifies_fresh_database(self):
        before = self.fixture.inventory()
        preview = self.preview()
        with patch('app.rekordbox_adapter.PyrekordboxAdapter.commit', side_effect=RuntimeError('injected')):
            result = self.apply(preview)
        self.assertEqual(result['error']['code'], 'transaction_failed', result)
        self.assertEqual(before, self.fixture.inventory())
        self.assertEqual(Journal(self.fixture.path).data['phase'], 'restored')

    def test_source_subset_keeps_all_target_memberships(self):
        before = self.fixture.protected()
        desired = self.fixture.desired(('1',))
        result = self.apply(self.preview(desired), desired)
        self.assertIsNone(result['error'], result)
        self.assertTrue(result['unchanged'], result)
        self.assertEqual(before, self.fixture.protected())

    def test_canonical_path_preserves_unicode_and_folds_windows_aliases(self):
        self.assertEqual(canonical_path('D:/MUSIC/A/../Track.wav'), canonical_path('d:\\music\\track.wav'))
        self.assertNotEqual(canonical_path('D:/café.wav'), canonical_path('D:/cafe\u0301.wav'))

    def test_duplicate_desired_path_and_database_paths_block(self):
        desired = self.fixture.desired()
        desired[1]['path'] = desired[0]['path']
        self.assertEqual(self.preview(desired)['error']['code'], 'duplicate_desired_content')
        adapter = self.fixture.factory()
        adapter.db.get_content(ID='3').FolderPath = str(self.fixture.paths[0])
        adapter.db.commit()
        adapter.close()
        self.assertEqual(self.preview()['error']['code'], 'ambiguous_content_path')

    def test_stale_unrelated_metadata_blocks_without_backup(self):
        preview = self.preview()
        adapter = self.fixture.factory()
        adapter.db.get_content(ID='3').Rating = 2
        adapter.db.commit()
        adapter.close()
        result = self.apply(preview)
        self.assertEqual(result['error']['code'], 'stale_preview', result)
        self.assertIsNone(result['backup_id'])

    def test_relocation_preserves_grid_unknown_tags_cues_and_memberships(self):
        analysis, preserved = self.fixture.analysis()
        original = self.fixture.paths[0]
        target = self.fixture.root / 'variant.wav'
        shutil.copyfile(original, target)
        desired = self.fixture.desired(('1', '2'))
        desired[0].update(path=str(target), source_path=str(original),
            verified_path_sha256=file_hash(target), verified_source_sha256=file_hash(original))
        before = self.fixture.protected()
        preview = self.preview(desired, operation_kind='relocate')
        self.assertIsNone(preview['error'], preview)
        self.assertEqual(len(preview['plan']['shared_content'][0]['playlist_ids']), 2)
        result = self.apply(preview, desired, operation_kind='relocate')
        self.assertIsNone(result['error'], result)
        self.assertTrue(analysis.read_bytes().endswith(preserved))
        after = self.fixture.protected()
        self.assertEqual(before['cues'], after['cues'])
        self.assertEqual(before['other'], after['other'])
        for old, new in zip(before['content'], after['content']):
            for key in ('FolderPath', 'FileNameL', 'FileSize', 'FileType', 'SampleRate', 'BitDepth', 'rb_local_usn', 'updated_at'):
                old.pop(key, None)
                new.pop(key, None)
            self.assertEqual(old, new)
        reverse = self.fixture.desired(('1', '2'))
        reverse[0]['source_path'] = str(target)
        reverse[0]['verified_path_sha256'] = file_hash(original)
        reverse[0]['verified_source_sha256'] = file_hash(target)
        revert = self.apply(self.preview(reverse, operation_kind='relocate'), reverse, operation_kind='relocate')
        self.assertIsNone(revert['error'], revert)
        self.assertTrue(analysis.read_bytes().endswith(preserved))

    def test_new_content_uses_importer_without_provider_comment(self):
        path = self.fixture.root / 'four.wav'
        shutil.copyfile(self.fixture.paths[0], path)
        desired = self.fixture.desired()
        desired.append(dict(desired[-1], provider_id='deezer:4', path=str(path), position=4))
        result = self.apply(self.preview(desired), desired)
        self.assertIsNone(result['error'], result)
        adapter = self.fixture.factory()
        try:
            content = adapter.db.get_content(FolderPath=str(path)).one()
            self.assertNotEqual(content.Commnt, 'deezer:4')
            self.assertEqual(content.Title, desired[-1]['title'])
        finally:
            adapter.close()

    def test_wal_backup_contains_committed_rows_missing_from_main_file(self):
        engine = create_engine(f'sqlite+pysqlcipher://:{SYNTHETIC_KEY}@/{self.fixture.path}', module=sqlcipher)
        keeper = engine.raw_connection()
        try:
            keeper.execute('PRAGMA journal_mode=WAL')
            keeper.execute('PRAGMA wal_autocheckpoint=0')
            keeper.execute("UPDATE djmdContent SET Commnt='WAL-only user edit' WHERE ID='3'")
            keeper.commit()
            self.assertTrue(Path(str(self.fixture.path) + '-wal').stat().st_size)
            before = self.fixture.inventory()
            preview = self.preview()
            with patch.object(PyrekordboxAdapter, 'commit', side_effect=RuntimeError('injected')):
                result = self.apply(preview)
            self.assertEqual(result['error']['code'], 'transaction_failed', result)
            self.assertEqual(before, self.fixture.inventory())
            journal = Journal(self.fixture.path)
            saved = PyrekordboxAdapter(path=journal.data['backup'], db_dir=self.fixture.root,
                key=SYNTHETIC_KEY, running_check=lambda: False)
            try:
                self.assertEqual(saved.db.get_content(ID='3').Commnt, 'WAL-only user edit')
                self.assertEqual(saved.fingerprint(), before)
            finally:
                saved.close()
        finally:
            keeper.close()
            engine.dispose()

    def test_crash_after_database_commit_recovers_without_deleting_lock_file(self):
        before = self.fixture.inventory()
        code = '''
import os, sys
from qa.tests.rekordbox_fixture import Fixture
from app import rekordbox as rb
from app.rekordbox_recovery import Journal
f=Fixture(sys.argv[1], initialize=False)
preview=rb.sync_playlist('Likes',f.desired(),adapter_factory=f.factory)
save=Journal.save
def crash(self, **updates):
    save(self, **updates)
    if updates.get('phase') == 'database_committed': os._exit(73)
Journal.save=crash
rb.sync_playlist('Likes',f.desired(),adapter_factory=f.factory,dry_run=False,
confirmation_token=rb.APPLY_CONFIRMATION_TOKEN,expected_plan_hash=preview['plan']['hash'])
'''
        child = subprocess.run([sys.executable, '-c', code, str(self.fixture.root)],
            capture_output=True, timeout=30)
        self.assertEqual(child.returncode, 73, child.stderr.decode(errors='replace'))
        journal = Journal(self.fixture.path)
        self.assertEqual(journal.data['phase'], 'database_committed')
        self.assertEqual(self.preview()['error']['code'], 'recovery_needed')
        result = rb.recover_operation(adapter_factory=self.fixture.factory,
            dry_run=False, confirmation_token=rb.RECOVERY_CONFIRMATION_TOKEN,
            expected_plan_hash=rb.recover_operation(adapter_factory=self.fixture.factory)['plan']['hash'])
        self.assertEqual(result['error']['code'], 'recovery_restored_preview_required', result)
        self.assertEqual(self.fixture.inventory(), before)
        self.assertTrue((self.fixture.root / 'deckpipe-rekordbox-mutation.lock').exists())
        self.assertIsNone(self.apply(self.preview())['error'])

    def test_recovery_refuses_unrelated_database_change_and_damaged_backup(self):
        for damaged in (False, True):
            with self.subTest(damaged=damaged):
                fixture = Fixture()
                preview = rb.sync_playlist('Likes', fixture.desired(), adapter_factory=fixture.factory)
                # Simulate a durable prepared operation without changing the DB.
                adapter = fixture.factory()
                journal = Journal(fixture.path)
                journal.prepare(adapter, preview['plan'])
                if damaged:
                    Path(journal.data['backup']).write_bytes(b'damaged')
                else:
                    adapter.db.get_content(ID='3').Commnt = 'Unrelated external change'
                    adapter.db.commit()
                adapter.close()
                before = fixture.inventory()
                result = rb.recover_operation(adapter_factory=fixture.factory, dry_run=False,
                    confirmation_token=rb.RECOVERY_CONFIRMATION_TOKEN,
                    expected_plan_hash=rb.recover_operation(adapter_factory=fixture.factory)['plan']['hash'])
                expected = 'recovery_backup_damaged' if damaged else 'recovery_database_changed'
                self.assertEqual(result['error']['code'], expected, result)
                self.assertEqual(fixture.inventory(), before)
                self.assertTrue(Journal(fixture.path).pending)

    def test_transaction_failure_surfaces_restore_database_and_external_files(self):
        for method in ('apply_operations', 'prepare_commit', 'commit'):
            with self.subTest(method=method):
                before = self.fixture.inventory()
                preview = self.preview()
                xml = file_hash(self.fixture.root / 'masterPlaylists6.xml')
                with patch.object(PyrekordboxAdapter, method, side_effect=RuntimeError('injected')):
                    result = self.apply(preview)
                self.assertEqual(result['error']['code'], 'transaction_failed', result)
                self.assertEqual(before, self.fixture.inventory())
                self.assertEqual(xml, file_hash(self.fixture.root / 'masterPlaylists6.xml'))

    def test_postcommit_publication_failure_restores_database(self):
        before = self.fixture.inventory()
        preview = self.preview()
        with patch.object(Journal, 'publish', side_effect=RuntimeError('injected')):
            result = self.apply(preview)
        self.assertEqual(result['error']['code'], 'transaction_failed', result)
        self.assertEqual(before, self.fixture.inventory())

    def test_rollback_failure_is_distinct_and_preserves_recovery_evidence(self):
        preview = self.preview()
        with patch.object(PyrekordboxAdapter, 'commit', side_effect=RuntimeError('injected')), \
             patch.object(Journal, 'restore', side_effect=RuntimeError('restore failed')):
            result = self.apply(preview)
        self.assertEqual(result['error']['code'], 'rollback_verify_failed', result)
        self.assertTrue(Journal(self.fixture.path).pending)

    def test_callback_failure_does_not_undo_reconciled_database(self):
        preview = self.preview()
        def fail(_result):
            raise RuntimeError('injected')
        result = self.apply(preview, on_reconciled=fail)
        self.assertEqual(result['error']['code'], 'callback_failed')
        self.assertTrue(result['applied'] and result['reconciled'])
        self.assertEqual(Journal(self.fixture.path).data['phase'], 'complete')
        self.assertTrue(self.apply(result)['unchanged'])

    def test_rekordbox_running_blocks_before_backup(self):
        preview = self.preview()
        with patch.object(PyrekordboxAdapter, 'is_rekordbox_running', return_value=True):
            result = self.apply(preview)
        self.assertEqual(result['error']['code'], 'rekordbox_running')
        self.assertIsNone(result['backup_id'])

    def test_competing_process_lock_and_abandoned_filename(self):
        from app.rekordbox_recovery import mutation_lock
        lock_path = self.fixture.root / 'deckpipe-rekordbox-mutation.lock'
        preview = self.preview()
        with mutation_lock(lock_path) as acquired:
            self.assertTrue(acquired)
            result = self.apply(preview)
            self.assertEqual(result['error']['code'], 'concurrent_apply')
        self.assertTrue(lock_path.exists())
        self.assertIsNone(self.apply(preview)['error'])

    def test_empty_target_create_and_create_missing_false(self):
        preview = rb.sync_playlist('New', [], adapter_factory=self.fixture.factory)
        denied = rb.sync_playlist('New', [], create_missing=False, dry_run=False,
            adapter_factory=self.fixture.factory, confirmation_token=rb.APPLY_CONFIRMATION_TOKEN,
            expected_plan_hash=preview['plan']['hash'])
        self.assertEqual(denied['error']['code'], 'playlist_not_found')
        result = rb.sync_playlist('New', [], dry_run=False, adapter_factory=self.fixture.factory,
            confirmation_token=rb.APPLY_CONFIRMATION_TOKEN, expected_plan_hash=preview['plan']['hash'])
        self.assertIsNone(result['error'], result)

    def test_missing_hash_or_confirmation_and_adapter_open_error(self):
        def fail():
            raise RuntimeError('injected private path')
        missing = rb.sync_playlist('Likes', [], dry_run=False, adapter_factory=fail)
        self.assertEqual(missing['error']['code'], 'apply_not_confirmed')
        opened = rb.sync_playlist('Likes', [], adapter_factory=fail)
        self.assertEqual(opened['error']['code'], 'adapter_open_failed')
        self.assertNotIn('private path', json.dumps(opened))

    def test_explicit_fixture_adapter_never_runs_config_discovery(self):
        with patch('pyrekordbox.config.update_config', side_effect=AssertionError('discovery forbidden')):
            adapter = self.fixture.factory()
            adapter.close()

    def test_verified_media_hash_cannot_be_replaced_by_a_fresh_hash(self):
        original = self.fixture.paths[0]
        target = self.fixture.root / 'variant.wav'
        shutil.copyfile(original, target)
        desired = self.fixture.desired(('1', '2'))
        desired[0].update(path=str(target), source_path=str(original),
            verified_path_sha256=file_hash(target), verified_source_sha256=file_hash(original))
        preview = self.preview(desired, operation_kind='relocate')
        target.write_bytes(target.read_bytes() + b'changed-after-PCM-check')
        fresh = self.preview(desired, operation_kind='relocate')
        self.assertTrue(any(i['code'] == 'media_verification_changed' for i in fresh['unresolved']))
        result = self.apply(preview, desired, operation_kind='relocate')
        self.assertEqual(result['error']['code'], 'unresolved_items')
        self.assertIsNone(result['backup_id'])

    def test_external_change_after_commit_is_not_clobbered_by_restore(self):
        preview = self.preview()
        xml = self.fixture.root / 'masterPlaylists6.xml'
        def external_change(_journal):
            xml.write_bytes(b'<MASTER_PLAYLIST><PLAYLISTS/><EXTERNAL/></MASTER_PLAYLIST>')
            raise RuntimeError('injected')
        with patch.object(Journal, 'publish', external_change):
            result = self.apply(preview)
        self.assertEqual(result['error']['code'], 'recovery_external_changed', result)
        self.assertEqual(xml.read_bytes(), b'<MASTER_PLAYLIST><PLAYLISTS/><EXTERNAL/></MASTER_PLAYLIST>')
        self.assertTrue(Journal(self.fixture.path).pending)

    def test_crash_before_commit_and_after_external_publication(self):
        for phase in ('commit_pending', 'external_published'):
            with self.subTest(phase=phase):
                fixture = Fixture()
                before = fixture.inventory()
                code = '''
import os, sys
from qa.tests.rekordbox_fixture import Fixture
from app import rekordbox as rb
from app.rekordbox_recovery import Journal
f=Fixture(sys.argv[1], initialize=False)
preview=rb.sync_playlist('Likes',f.desired(),adapter_factory=f.factory)
save=Journal.save
def crash(self, **updates):
    save(self, **updates)
    if updates.get('phase') == sys.argv[2]: os._exit(73)
Journal.save=crash
rb.sync_playlist('Likes',f.desired(),adapter_factory=f.factory,dry_run=False,
confirmation_token=rb.APPLY_CONFIRMATION_TOKEN,expected_plan_hash=preview['plan']['hash'])
'''
                child = subprocess.run([sys.executable, '-c', code, str(fixture.root), phase],
                    capture_output=True, timeout=30)
                self.assertEqual(child.returncode, 73, child.stderr.decode(errors='replace'))
                journal = Journal(fixture.path)
                result = rb.recover_operation(adapter_factory=fixture.factory,
                    dry_run=False, confirmation_token=rb.RECOVERY_CONFIRMATION_TOKEN,
                    expected_plan_hash=rb.recover_operation(adapter_factory=fixture.factory)['plan']['hash'])
                self.assertEqual(result['error']['code'], 'recovery_restored_preview_required', result)
                self.assertEqual(fixture.inventory(), before)

    def test_begin_backup_and_snapshot_failures_are_structured(self):
        for method in ('context', 'begin', 'backup_database', 'integrity_check'):
            with self.subTest(method=method):
                preview = self.preview()
                before = self.fixture.inventory()
                with patch.object(PyrekordboxAdapter, method, side_effect=RuntimeError('private details')):
                    result = self.apply(preview)
                self.assertIsNotNone(result['error'])
                self.assertNotIn('private details', json.dumps(result))
                self.assertEqual(before, self.fixture.inventory())

    def test_recovery_status_is_read_only_and_handles_invalid_journal(self):
        before = {str(p): file_hash(p) for p in self.fixture.root.rglob('*') if p.is_file()}
        with patch.object(rb, '_PyrekordboxAdapter', side_effect=AssertionError('must not open DB')):
            status = rb.get_recovery_status(database_path=self.fixture.path)
        self.assertFalse(status['needed'])
        self.assertEqual(before, {str(p): file_hash(p) for p in self.fixture.root.rglob('*') if p.is_file()})
        journal_path = self.fixture.root / 'deckpipe-rekordbox-operation.json'
        journal_path.write_text('{"version":1}', encoding='utf-8')
        status = rb.get_recovery_status(database_path=self.fixture.path)
        self.assertTrue(status['needed'])
        self.assertEqual(status['error']['code'], 'recovery_journal_invalid')

    def test_crash_during_real_anlz_publication_restores_database_and_analysis(self):
        analysis, _ = self.fixture.analysis()
        original_bytes = analysis.read_bytes()
        before = self.fixture.inventory()
        target = self.fixture.root / 'variant.wav'
        shutil.copyfile(self.fixture.paths[0], target)
        code = '''
import os, sys
from pathlib import Path
from qa.tests.rekordbox_fixture import Fixture
from app import rekordbox as rb
from app import rekordbox_recovery as recovery
f=Fixture(sys.argv[1], initialize=False)
desired=f.desired(('1','2'))
source=desired[0]['path']
target=str(f.root/'variant.wav')
desired[0].update(path=target,source_path=source,
verified_path_sha256=recovery.file_hash(target),verified_source_sha256=recovery.file_hash(source))
preview=rb.sync_playlist('Likes',desired,adapter_factory=f.factory,operation_kind='relocate')
from app.rekordbox_file_ownership import OwnedFile
publish=OwnedFile.rename
def crash(self, path):
    publish(self,path)
    if Path(path).name == 'ANLZ0000.DAT': os._exit(73)
OwnedFile.rename=crash
rb.sync_playlist('Likes',desired,adapter_factory=f.factory,dry_run=False,operation_kind='relocate',
confirmation_token=rb.APPLY_CONFIRMATION_TOKEN,expected_plan_hash=preview['plan']['hash'])
'''
        child = subprocess.run([sys.executable, '-c', code, str(self.fixture.root)], capture_output=True, timeout=30)
        self.assertEqual(child.returncode, 73, child.stderr.decode(errors='replace'))
        self.assertNotEqual(analysis.read_bytes(), original_bytes)
        journal = Journal(self.fixture.path)
        result = rb.recover_operation(adapter_factory=self.fixture.factory,
            dry_run=False, confirmation_token=rb.RECOVERY_CONFIRMATION_TOKEN,
            expected_plan_hash=rb.recover_operation(adapter_factory=self.fixture.factory)['plan']['hash'])
        self.assertEqual(result['error']['code'], 'recovery_restored_preview_required', result)
        self.assertEqual(self.fixture.inventory(), before)
        self.assertEqual(analysis.read_bytes(), original_bytes)


if __name__ == '__main__':
    unittest.main()
