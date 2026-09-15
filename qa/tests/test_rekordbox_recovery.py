"""Task 1 review regressions; synthetic SQLCipher fixtures only."""
import json
import unittest
import copy
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch
from app import rekordbox as rb
from app.rekordbox_recovery import Journal, RecoveryError, file_hash
from app.rekordbox_file_ownership import OwnedFile
from app.rekordbox_adapter import PyrekordboxAdapter
from qa.tests.rekordbox_fixture import Fixture


class ReviewRecoveryTests(unittest.TestCase):
    def prepare_external(self, fixture, *, absent=False):
        adapter = fixture.factory()
        try:
            path = fixture.root / 'masterPlaylists6.xml'
            if absent:
                path.unlink()
            plan = rb._bound_plan(adapter, 'Likes', fixture.desired(), None, 'sync')
            journal = Journal(fixture.path)
            journal.prepare(adapter, plan)
            payload = b'<MASTER_PLAYLIST><PLAYLISTS/><OWNED/></MASTER_PLAYLIST>'
            journal.stage_external({path: payload})
            journal.save(phase='database_committed', database_after=adapter.fingerprint())
            return journal, path, payload
        finally:
            adapter.close()

    def test_restore_close_race_preserves_independently_committed_edit(self):
        fixture = Fixture()
        preview = rb.sync_playlist('Likes', fixture.desired(), adapter_factory=fixture.factory)
        original_restore = Journal.restore
        injected = []
        def restore_with_competing_close(journal, adapter):
            close = adapter.close
            def close_and_commit_other_edit():
                close()
                if not injected:
                    injected.append(True)
                    other = fixture.factory()
                    try:
                        other.db.get_content(ID='3').Commnt = 'Unrelated edit during restore'
                        other.db.commit()
                        self.assertEqual(other.db.get_content(ID='3').Commnt, 'Unrelated edit during restore')
                    finally:
                        other.close()
            adapter.close = close_and_commit_other_edit
            return original_restore(journal, adapter)
        with patch.object(Journal, 'publish', side_effect=RuntimeError('postcommit failure')), \
             patch.object(Journal, 'restore', restore_with_competing_close):
            result = rb.sync_playlist('Likes', fixture.desired(), adapter_factory=fixture.factory,
                dry_run=False, confirmation_token=rb.APPLY_CONFIRMATION_TOKEN,
                expected_plan_hash=preview['plan']['hash'])
        self.assertEqual(injected, [True])
        other = fixture.factory()
        try:
            self.assertEqual(other.db.get_content(ID='3').Commnt, 'Unrelated edit during restore')
        finally:
            other.close()
        self.assertIn(result['error']['code'], ('rollback_verify_failed', 'recovery_database_changed'))
        self.assertTrue(Journal(fixture.path).pending)

    def test_structurally_invalid_terminal_record_is_not_healthy(self):
        fixture = Fixture()
        path = fixture.root / 'deckpipe-rekordbox-operation.json'
        path.write_text(json.dumps({'version': 1, 'id': 'a' * 32, 'phase': 'complete',
            'database_path': None, 'database_before': None, 'database_after': None,
            'backup': None, 'backup_hash': None, 'plan_hash': None, 'external': [None]}), encoding='utf-8')
        result = rb.get_recovery_status(database_path=fixture.path)
        self.assertTrue(result['needed'])
        self.assertEqual(result['error']['code'], 'recovery_journal_invalid')

    def test_competing_sqlite_process_cannot_write_during_restore_transaction(self):
        fixture = Fixture()
        preview = rb.sync_playlist('Likes', fixture.desired(), adapter_factory=fixture.factory)
        restore_rows = PyrekordboxAdapter.restore_database_rows
        observed = []
        def race(adapter, backup):
            code = '''
import sys
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlcipher3 import dbapi2 as sqlcipher
from qa.tests.rekordbox_fixture import SYNTHETIC_KEY
engine=create_engine(f'sqlite+pysqlcipher://:{SYNTHETIC_KEY}@/{sys.argv[1]}',
module=sqlcipher,connect_args={'timeout':0.05})
try:
    with engine.begin() as conn:
        conn.exec_driver_sql("UPDATE djmdContent SET Commnt='competing writer' WHERE ID='3'")
    print('COMMITTED')
except OperationalError:
    print('BLOCKED')
finally:
    engine.dispose()
'''
            child = subprocess.run([sys.executable, '-c', code, str(fixture.path)], capture_output=True, timeout=10)
            self.assertEqual(child.returncode, 0, child.stderr.decode(errors='replace'))
            observed.append(child.stdout.decode().strip())
            return restore_rows(adapter, backup)
        with patch.object(Journal, 'publish', side_effect=RuntimeError('postcommit failure')), \
             patch.object(PyrekordboxAdapter, 'restore_database_rows', race):
            result = rb.sync_playlist('Likes', fixture.desired(), adapter_factory=fixture.factory,
                dry_run=False, confirmation_token=rb.APPLY_CONFIRMATION_TOKEN,
                expected_plan_hash=preview['plan']['hash'])
        self.assertEqual(observed, ['BLOCKED'])
        self.assertEqual(result['error']['code'], 'transaction_failed')
        self.assertEqual(Journal(fixture.path).data['phase'], 'restored')

    def test_unsupported_restore_schema_is_refused_before_backup_and_mutation(self):
        fixture = Fixture()
        adapter = fixture.factory()
        adapter.db.session.connection().exec_driver_sql(
            'CREATE TRIGGER fixture_trigger AFTER UPDATE ON djmdContent BEGIN SELECT 1; END')
        adapter.db.session.commit()
        adapter.close()
        before = fixture.inventory()
        preview = rb.sync_playlist('Likes', fixture.desired(), adapter_factory=fixture.factory)
        result = rb.sync_playlist('Likes', fixture.desired(), adapter_factory=fixture.factory,
            dry_run=False, confirmation_token=rb.APPLY_CONFIRMATION_TOKEN,
            expected_plan_hash=preview['plan']['hash'])
        self.assertEqual(result['error']['code'], 'unsupported_recovery_schema')
        self.assertEqual(fixture.inventory(), before)
        self.assertFalse((fixture.root / 'deckpipe-rekordbox-backups').exists())

    def test_existing_external_writes_deletes_and_replacement_are_excluded(self):
        fixture = Fixture()
        journal, path, payload = self.prepare_external(fixture)
        replace = Journal._replace_owned
        attempts = []
        def race(journal, item, owner, data, stack):
            foreign = fixture.root / 'foreign.xml'
            foreign.write_bytes(b'foreign')
            for name, operation in (
                    ('write', lambda: path.write_bytes(b'foreign')),
                    ('delete', path.unlink),
                    ('replace', lambda: os.replace(foreign, path))):
                with self.assertRaises(PermissionError):
                    operation()
                attempts.append(name)
            code = '''
import sys
from pathlib import Path
try:
    Path(sys.argv[1]).write_bytes(b'foreign process')
    print('WROTE')
except PermissionError:
    print('BLOCKED')
'''
            child = subprocess.run([sys.executable, '-c', code, str(path)], capture_output=True, timeout=10)
            self.assertEqual(child.returncode, 0)
            self.assertEqual(child.stdout.decode().strip(), 'BLOCKED')
            return replace(journal, item, owner, data, stack)
        with patch.object(Journal, '_replace_owned', race):
            journal.publish()
        self.assertEqual(attempts, ['write', 'delete', 'replace'])
        self.assertEqual(path.read_bytes(), payload)

    def test_foreign_creation_in_existing_name_gap_is_preserved(self):
        fixture = Fixture()
        journal, path, _ = self.prepare_external(fixture)
        rename = OwnedFile.rename
        foreign = b'<MASTER_PLAYLIST><PLAYLISTS/><FOREIGN/></MASTER_PLAYLIST>'
        injected = []
        def race(owner, target):
            original = owner.path
            result = rename(owner, target)
            if original == path and '.moved-' in str(target):
                path.write_bytes(foreign)
                injected.append(True)
            return result
        with patch.object(OwnedFile, 'rename', race), self.assertRaises(RecoveryError):
            journal.publish()
        self.assertEqual(injected, [True])
        self.assertEqual(path.read_bytes(), foreign)
        self.assertTrue(Journal(fixture.path).pending)
        adapter = fixture.factory()
        try:
            with self.assertRaisesRegex(RecoveryError, 'recovery_external_changed'):
                journal.restore(adapter)
        finally:
            adapter.close()
        self.assertEqual(path.read_bytes(), foreign)

    def test_foreign_creation_at_originally_absent_path_is_preserved(self):
        fixture = Fixture()
        journal, path, _ = self.prepare_external(fixture, absent=True)
        replace = Journal._replace_owned
        foreign = b'<MASTER_PLAYLIST><PLAYLISTS/><FOREIGN/></MASTER_PLAYLIST>'
        def race(journal, item, owner, data, stack):
            self.assertIsNone(owner)
            path.write_bytes(foreign)
            return replace(journal, item, owner, data, stack)
        with patch.object(Journal, '_replace_owned', race), self.assertRaises(RecoveryError):
            journal.publish()
        self.assertEqual(path.read_bytes(), foreign)
        self.assertTrue(Journal(fixture.path).pending)

    def test_restore_external_file_writers_are_excluded(self):
        fixture = Fixture()
        journal, path, _ = self.prepare_external(fixture)
        original_hash = journal.data['external'][0]['before']
        journal.publish()
        replace = Journal._replace_owned
        attempts = []
        def race(journal, item, owner, data, stack):
            with self.assertRaises(PermissionError):
                path.write_bytes(b'foreign restore write')
            attempts.append(True)
            return replace(journal, item, owner, data, stack)
        adapter = fixture.factory()
        try:
            with patch.object(Journal, '_replace_owned', race):
                journal.restore(adapter)
        finally:
            adapter.close()
        self.assertEqual(attempts, [True])
        self.assertEqual(file_hash(path), original_hash)

    def test_full_terminal_journal_validation(self):
        fixture = Fixture()
        journal, _, _ = self.prepare_external(fixture)
        valid = copy.deepcopy(journal.data)
        valid['phase'] = 'complete'
        changes = [
            lambda d: d.update(version=True), lambda d: d.update(database_before=None),
            lambda d: d.update(database_after=None), lambda d: d.update(backup_hash=123),
            lambda d: d.update(plan_hash='z' * 64), lambda d: d.update(database_path='relative.db'),
            lambda d: d.update(backup=str(fixture.root / 'outside.db')),
            lambda d: d.update(external=[None]),
            lambda d: d['external'][0].update(before=45),
            lambda d: d['external'][0].update(path=str(fixture.root / 'unrelated.wav')),
            lambda d: d['external'][0].update(staged=None),
            lambda d: d['external'][0].update(after=None),
            lambda d: d['external'][0].update(moves=[None]),
            lambda d: d.update(phase='prepared'),
        ]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                broken = copy.deepcopy(valid)
                change(broken)
                journal.path.write_text(json.dumps(broken), encoding='utf-8')
                result = rb.get_recovery_status(database_path=fixture.path)
                self.assertTrue(result['needed'])
                self.assertEqual(result['error']['code'], 'recovery_journal_invalid')

    def test_foreign_creation_during_restore_to_absence_is_preserved(self):
        fixture = Fixture()
        journal, path, _ = self.prepare_external(fixture, absent=True)
        journal.publish()
        rename = OwnedFile.rename
        foreign = b'<MASTER_PLAYLIST><PLAYLISTS/><FOREIGN/></MASTER_PLAYLIST>'
        def race(owner, target):
            original = owner.path
            result = rename(owner, target)
            if original == path and '.moved-' in str(target):
                path.write_bytes(foreign)
            return result
        adapter = fixture.factory()
        try:
            with patch.object(OwnedFile, 'rename', race), self.assertRaisesRegex(RecoveryError, 'recovery_external_changed'):
                journal.restore(adapter)
        finally:
            adapter.close()
        self.assertEqual(path.read_bytes(), foreign)
        self.assertTrue(Journal(fixture.path).pending)

    def test_killed_file_displacement_and_uncommitted_restore_are_recoverable(self):
        for phase in ('displaced', 'restore_commit'):
            with self.subTest(phase=phase):
                fixture = Fixture()
                analysis, _ = fixture.analysis()
                original = analysis.read_bytes()
                before = fixture.inventory()
                (fixture.root / 'variant.wav').write_bytes((fixture.root / 'one.wav').read_bytes())
                code = '''
import os,sys
from pathlib import Path
from qa.tests.rekordbox_fixture import Fixture
from app import rekordbox as rb
from app.rekordbox_recovery import Journal,file_hash
from app.rekordbox_adapter import PyrekordboxAdapter
from app.rekordbox_file_ownership import OwnedFile
f=Fixture(sys.argv[1],initialize=False)
desired=f.desired(('1','2'))
source=desired[0]['path']; target=str(f.root/'variant.wav')
desired[0].update(path=target,source_path=source,
verified_path_sha256=file_hash(target),verified_source_sha256=file_hash(source))
preview=rb.sync_playlist('Likes',desired,adapter_factory=f.factory,operation_kind='relocate')
if sys.argv[2]=='displaced':
    rename=OwnedFile.rename
    def crash(self,target):
        old=self.path
        rename(self,target)
        if old.name=='ANLZ0000.DAT' and '.moved-' in str(target): os._exit(74)
    OwnedFile.rename=crash
else:
    publish=Journal.publish
    def fail(self):
        publish(self)
        raise RuntimeError('post-publication failure')
    Journal.publish=fail
    PyrekordboxAdapter.finish_restore=lambda self: os._exit(74)
rb.sync_playlist('Likes',desired,adapter_factory=f.factory,operation_kind='relocate',dry_run=False,
confirmation_token=rb.APPLY_CONFIRMATION_TOKEN,expected_plan_hash=preview['plan']['hash'])
'''
                child = subprocess.run([sys.executable, '-c', code, str(fixture.root), phase], capture_output=True, timeout=30)
                self.assertEqual(child.returncode, 74, child.stderr.decode(errors='replace'))
                journal = Journal(fixture.path)
                self.assertTrue(journal.pending)
                result = rb.recover_operation(adapter_factory=fixture.factory,
                    dry_run=False, confirmation_token=rb.RECOVERY_CONFIRMATION_TOKEN,
                    expected_plan_hash=rb.recover_operation(adapter_factory=fixture.factory)['plan']['hash'])
                self.assertEqual(result['error']['code'], 'recovery_restored_preview_required', result)
                self.assertEqual(fixture.inventory(), before)
                self.assertEqual(analysis.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
