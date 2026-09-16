"""Final review regressions on explicit synthetic SQLCipher and protected HTTP."""
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import rekordbox as rb
from app.rekordbox_recovery import Journal
from qa.tests.rekordbox_fixture import Fixture
from qa.tests.test_rekordbox_catalog import Client


def interrupted(fixture, name='Other'):
    adapter = fixture.factory()
    try:
        plan = rb._bound_plan(adapter, name, fixture.desired(), None, 'sync')
        journal = Journal(fixture.path)
        journal.prepare(adapter, plan)
        adapter.begin()
        adapter.apply_operations(plan)
        journal.stage_external(adapter.prepare_commit())
        journal.save(phase='commit_pending', database_after=adapter.fingerprint())
        adapter.commit()
        journal.save(phase='database_committed')
        return journal
    finally:
        adapter.close()


class FinalRecoveryBoundaries(unittest.TestCase):
    def test_normal_confirmation_cannot_authorize_explicit_recovery(self):
        with patch.object(rb, '_PyrekordboxAdapter', side_effect=AssertionError('no recovery authorization')):
            result = rb.recover_operation(dry_run=False, confirmation_token=rb.APPLY_CONFIRMATION_TOKEN,
                                          expected_plan_hash='some-normal-preview')
        self.assertEqual(result['error']['code'], 'recovery_not_confirmed')

    def test_recovery_rechecks_hash_inside_file_and_database_ownership(self):
        f = Fixture()
        journal = interrupted(f)
        preview = rb.recover_operation(adapter_factory=f.factory)
        original_restore = Journal.restore
        observed = []
        def change_after_admission(record, adapter, **kwargs):
            record.publish()
            observed.append((f.inventory(), record.path.read_bytes(), (f.root/'masterPlaylists6.xml').read_bytes()))
            return original_restore(record, adapter, **kwargs)
        with patch.object(Journal, 'restore', change_after_admission):
            result = rb.recover_operation(adapter_factory=f.factory, dry_run=False,
                confirmation_token=rb.RECOVERY_CONFIRMATION_TOKEN, expected_plan_hash=preview['plan']['hash'])
        self.assertEqual(result['error']['code'], 'recovery_stale_preview')
        self.assertEqual((f.inventory(), journal.path.read_bytes(), (f.root/'masterPlaylists6.xml').read_bytes()), observed[0])

    def test_changed_authoritative_state_and_running_process_refuse_without_writes(self):
        f = Fixture()
        journal = interrupted(f)
        preview = rb.recover_operation(adapter_factory=f.factory)
        journal.publish()  # Same operation, changed phase/external state.
        before = f.inventory(), journal.path.read_bytes()
        result = rb.recover_operation(adapter_factory=f.factory, dry_run=False,
            confirmation_token=rb.RECOVERY_CONFIRMATION_TOKEN, expected_plan_hash=preview['plan']['hash'])
        self.assertEqual(result['error']['code'], 'recovery_stale_preview')
        self.assertEqual((f.inventory(), journal.path.read_bytes()), before)
        preview = rb.recover_operation(adapter_factory=f.factory)
        from app.rekordbox_adapter import PyrekordboxAdapter
        with patch.object(PyrekordboxAdapter, 'is_rekordbox_running', return_value=True):
            result = rb.recover_operation(adapter_factory=f.factory, dry_run=False,
                confirmation_token=rb.RECOVERY_CONFIRMATION_TOKEN, expected_plan_hash=preview['plan']['hash'])
        self.assertEqual(result['error']['code'], 'rekordbox_running')
        self.assertEqual((f.inventory(), journal.path.read_bytes()), before)

    def test_recovery_routes_keep_bearer_and_origin_guards(self):
        from app import main
        client = Client(main.app)
        with patch.object(rb, 'recover_operation', side_effect=AssertionError('unauthorized recovery')):
            for method in ('GET', 'POST'):
                for headers in ({'authorization':'Bearer wrong'}, {'origin':'https://foreign.invalid'}):
                    result = client.request(method, '/api/rb/recovery', {}, headers=headers)
                    self.assertIn(result.status_code, (401, 403))

    def test_normal_preview_then_other_interruption_cannot_restore(self):
        f = Fixture()
        preview = rb.sync_playlist('Likes', f.desired(), adapter_factory=f.factory)
        journal = interrupted(f)
        before = f.inventory(), journal.path.read_bytes()
        result = rb.sync_playlist('Likes', f.desired(), adapter_factory=f.factory,
            dry_run=False, confirmation_token=rb.APPLY_CONFIRMATION_TOKEN,
            expected_plan_hash=preview['plan']['hash'])
        self.assertEqual(result['error']['code'], 'recovery_needed')
        self.assertEqual((f.inventory(), journal.path.read_bytes()), before)

    def test_recovery_hash_and_journal_replacement_are_bound(self):
        f = Fixture()
        journal = interrupted(f)
        preview = rb.recover_operation(adapter_factory=f.factory)
        before = f.inventory(), journal.path.read_bytes()
        wrong = rb.recover_operation(adapter_factory=f.factory, dry_run=False,
            confirmation_token=rb.RECOVERY_CONFIRMATION_TOKEN, expected_plan_hash='wrong')
        self.assertEqual(wrong['error']['code'], 'recovery_stale_preview')
        self.assertEqual((f.inventory(), journal.path.read_bytes()), before)
        # Valid replacement operation B must not be restored by confirmation A.
        other = f.factory()
        try:
            replacement = Journal(f.path)
            replacement.prepare(other, rb._bound_plan(other, 'Likes', f.desired(), None, 'sync'))
        finally:
            other.close()
        before = f.inventory(), journal.path.read_bytes()
        stale = rb.recover_operation(adapter_factory=f.factory, dry_run=False,
            confirmation_token=rb.RECOVERY_CONFIRMATION_TOKEN, expected_plan_hash=preview['plan']['hash'])
        self.assertEqual(stale['error']['code'], 'recovery_stale_preview')
        self.assertEqual((f.inventory(), journal.path.read_bytes()), before)

    def test_http_recovery_does_not_require_source_or_media(self):
        from app import main
        f = Fixture()
        before = f.inventory()
        journal = interrupted(f)
        (f.root / 'one.wav').unlink()  # Current source media is unavailable.
        client = Client(main.app)
        with patch.object(rb, '_PyrekordboxAdapter', f.factory), patch.object(rb, 'db_path', return_value=f.path), \
             patch('app.rekordbox_service.resolve', side_effect=AssertionError('source must not be read')):
            preview = client.get('/api/rb/recovery')
            self.assertEqual(preview.status_code, 200, preview.text)
            self.assertEqual(preview.json()['plan']['operation_id'], journal.data['id'])
            for key in ('local:missing', '123', 'sc:missing'):
                for route, extra in (('/api/rb/sync', {}), ('/api/flip', {'to_wav':True})):
                    normal = client.post(route, {'playlist_key':key, 'playlist_title':'Missing', **extra})
                    self.assertEqual(normal.json()['error']['code'], 'recovery_needed', normal.text)
                state = client.get('/api/rb/media-state?playlist_key='+key+'&playlist_title=Missing')
                self.assertEqual(state.json()['error']['code'], 'recovery_needed', state.text)
            restored = client.post('/api/rb/recovery?confirmation_token=RESTORE_REKORDBOX_OPERATION',
                {'expected_plan_hash':preview.json()['plan']['hash']})
            self.assertEqual(restored.json()['error']['code'], 'recovery_restored_preview_required', restored.text)
        self.assertEqual(f.inventory(), before)
        self.assertEqual(Journal(f.path).data['phase'], 'restored')


class WindowsProcessBoundaries(unittest.TestCase):
    def test_localized_bytes_and_present_absent(self):
        for output, expected in ((b'"rekordbox.exe","123","Console","1","20 K"\r\n', True),
                                 ('"explorer.exe","456","Консоль","1","20 КБ"\r\n'.encode('cp866'), False)):
            with self.subTest(expected=expected), patch.object(rb.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=output, stderr=b'')) as run:
                self.assertIs(rb.rb_running(), expected)
                self.assertFalse(run.call_args.kwargs.get('text', False))
                self.assertIn('timeout', run.call_args.kwargs)

    def test_failure_and_timeout_are_structured_and_fail_closed(self):
        from app import main
        for failure in (SimpleNamespace(returncode=1, stdout=b'', stderr=b'failed'),
                        SimpleNamespace(returncode=0, stdout=b'unknown output', stderr=b''),
                        SimpleNamespace(returncode=0, stdout=None, stderr=b''),
                        subprocess.TimeoutExpired('tasklist', 10)):
            kwargs = {'side_effect': failure} if isinstance(failure, Exception) else {'return_value': failure}
            with self.subTest(failure=str(failure)), patch.object(rb.subprocess, 'run', **kwargs), \
                 patch.object(rb, 'db_exists', return_value=False), patch.object(rb, 'get_recovery_status', return_value={'needed':False}):
                with self.assertRaisesRegex(rb.AdapterError, 'rekordbox_process_check_failed'):
                    rb.rb_running()
                result = Client(main.app).get('/api/rb/status')
                self.assertEqual(result.status_code, 200)
                self.assertIsNone(result.json()['running'])
                self.assertEqual(result.json()['error']['code'], 'rekordbox_process_check_failed')

    def test_unknown_process_status_blocks_real_database_apply(self):
        f = Fixture()
        preview = rb.sync_playlist('Likes', f.desired(), adapter_factory=f.factory)
        before = f.inventory()
        from app.rekordbox_adapter import PyrekordboxAdapter
        with patch.object(PyrekordboxAdapter, 'is_rekordbox_running', side_effect=rb.rb_running), \
             patch.object(rb.subprocess, 'run', side_effect=subprocess.TimeoutExpired('tasklist', 10)):
            result = rb.sync_playlist('Likes', f.desired(), adapter_factory=f.factory, dry_run=False,
                confirmation_token=rb.APPLY_CONFIRMATION_TOKEN, expected_plan_hash=preview['plan']['hash'])
        self.assertEqual(result['error']['code'], 'rekordbox_process_check_failed')
        self.assertEqual(f.inventory(), before)
        self.assertFalse(Journal(f.path).path.exists())


class MembershipMediaBoundaries(unittest.TestCase):
    def test_membership_is_separate_from_actual_verified_media_modes(self):
        from app.rekordbox_media import MediaStore
        from app.rekordbox_service import media_state_from_plan
        f = Fixture()
        desired = f.desired(('1','2'))
        store = MediaStore(f.root / 'media.json')
        variant, _ = store.prepare(desired[0]['path'])
        original = {'path':desired[0]['path'], 'content_id':'1'}
        wav = {'path':variant['variant'], 'content_id':'1'}
        two = {'path':desired[1]['path'], 'content_id':'2'}
        cases = [
            ('original-partial', [original], 'target', 'original', 'partial'),
            ('wav-partial', [wav], 'target', 'wav', 'partial'),
            ('existing-empty', [], 'target', 'empty', 'partial'),
            ('absent-target', [], None, 'empty', 'absent_target'),
            ('disjoint', [{'path':str(f.root/'three.wav')}], 'target', 'empty', 'partial'),
            ('mixed-formats', [wav,two], 'target', 'mixed', 'complete'),
        ]
        for name, current, target, mode, membership in cases:
            with self.subTest(name=name):
                state = media_state_from_plan(desired, {'target':{'id':target}, 'current_memberships':current}, store)
                self.assertEqual(state['mode'], mode)
                self.assertEqual(state['membership'], membership)
                self.assertEqual(state['target_exists'], target is not None)
        reordered = media_state_from_plan(desired,
            {'target':{'id':'target'}, 'current_memberships':[two,original], 'reorder':[desired[0]]}, store)
        self.assertEqual(reordered['mode'], 'original')
        self.assertEqual(reordered['membership'], 'complete')
