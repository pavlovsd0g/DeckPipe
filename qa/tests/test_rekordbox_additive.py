"""Additive sync must preserve a DJ's existing collection and playlist rows."""
import json
import os
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

from app import rekordbox as rb
from app.rekordbox_adapter import PyrekordboxAdapter
from app.rekordbox_recovery import Journal, file_hash
from qa.tests.rekordbox_fixture import Fixture


class AdditiveSyncTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()

    def preview(self, desired, **kwargs):
        return rb.sync_playlist('Likes', desired, adapter_factory=self.fixture.factory, **kwargs)

    def apply(self, desired, preview=None, **kwargs):
        preview = preview or self.preview(desired, **kwargs)
        return self.preview(desired, dry_run=False, confirmation_token=rb.APPLY_CONFIRMATION_TOKEN,
                            expected_plan_hash=preview['plan']['hash'], **kwargs)

    def memberships(self):
        adapter = self.fixture.factory()
        try:
            return [song.to_dict() for song in sorted(adapter.db.get_playlist_songs(
                PlaylistID=self.fixture.target_id), key=lambda song: int(song.TrackNo))]
        finally:
            adapter.close()

    def files(self):
        return {str(path): file_hash(path) for path in self.fixture.root.rglob('*')
                if path.is_file() and path.name != 'deckpipe-rekordbox-mutation.lock'}

    def test_planner_reports_only_missing_memberships_in_append_order(self):
        desired = self.fixture.desired(('2', '3', '1'))
        desired[1]['content_id'] = '3'
        current = self.fixture.desired(('1', '2'))
        current[0].update(title='DJ title', path=str(self.fixture.root / 'kept-original.wav'))
        current.append(dict(current[1], provider_id='rb:extra', position=8))

        plan = rb.plan_playlist_sync(desired, current)

        self.assertEqual([item['provider_id'] for item in plan['add']], ['deezer:3'])
        self.assertEqual([item['position'] for item in plan['add']], [9])
        self.assertEqual([item['source_position'] for item in plan['add']], [2])
        for operation in ('remove', 'reorder', 'metadata', 'path'):
            self.assertEqual(plan[operation], [])
        self.assertEqual({key: plan['counts'][key] for key in
                          ('add', 'already_present', 'reuse', 'import', 'preserved')},
                         {'add': 1, 'already_present': 2, 'reuse': 1, 'import': 0, 'preserved': 1})

    def test_append_reuses_collection_content_and_preserves_existing_rows(self):
        before, memberships = self.fixture.protected(), self.memberships()
        desired = self.fixture.desired(('3',))
        fourth = self.fixture.root / 'four.wav'
        shutil.copyfile(self.fixture.paths[0], fourth)
        desired.append(dict(desired[0], provider_id='deezer:4', path=str(fourth), position=2))
        preview = self.preview(desired)

        result = self.apply(desired, preview)

        self.assertIsNone(result['error'], result)
        self.assertTrue(result['reconciled'])
        after = self.fixture.protected()
        self.assertEqual(before['content'], [row for row in after['content'] if row['ID'] in ('1', '2', '3')])
        self.assertEqual(before['cues'], after['cues'])
        self.assertEqual(before['other'], after['other'])
        actual = self.memberships()
        self.assertEqual(actual[:2], memberships)
        self.assertEqual([row['TrackNo'] for row in actual], [1, 2, 3, 4])
        self.assertEqual([row['ContentID'] for row in actual[:3]], ['1', '2', '3'])
        self.assertEqual(len(after['content']), 4)
        self.assertEqual({key: preview['plan']['counts'][key] for key in ('add', 'reuse', 'import', 'preserved')},
                         {'add': 2, 'reuse': 1, 'import': 1, 'preserved': 2})
        inventory, files = self.fixture.inventory(), self.files()
        again = self.apply(desired, preview)
        self.assertTrue(again['unchanged'], again)
        self.assertIsNone(again['backup_id'])
        self.assertEqual(self.fixture.inventory(), inventory)
        self.assertEqual(self.files(), files)

    def test_existing_source_members_with_different_order_or_empty_source_are_noop(self):
        for ids in (('2', '1'), ('2',), ()):
            with self.subTest(ids=ids):
                desired = self.fixture.desired(ids)
                inventory, files = self.fixture.inventory(), self.files()
                preview = self.preview(desired)
                self.assertTrue(preview['unchanged'], preview)
                result = self.apply(desired, preview)
                self.assertTrue(result['unchanged'], result)
                self.assertIsNone(result['backup_id'])
                self.assertEqual(self.fixture.inventory(), inventory)
                self.assertEqual(self.files(), files)

    def test_default_sync_never_relocates_existing_alias_even_while_appending(self):
        analysis, _ = self.fixture.analysis()
        before, memberships, anlz = self.fixture.protected(), self.memberships(), analysis.read_bytes()
        variant = self.fixture.root / 'variant.wav'
        shutil.copyfile(self.fixture.paths[0], variant)
        desired = self.fixture.desired(('1', '3'))
        desired[0].update(path=str(variant), source_path=str(self.fixture.paths[0]),
                          verified_path_sha256=file_hash(variant),
                          verified_source_sha256=file_hash(self.fixture.paths[0]))

        preview = self.preview(desired)
        result = self.apply(desired, preview)

        self.assertIsNone(result['error'], result)
        self.assertEqual(preview['plan']['path'], [])
        self.assertEqual(preview['plan']['shared_content'], [])
        self.assertEqual(self.fixture.protected(), before)
        self.assertEqual(self.memberships()[:2], memberships)
        self.assertEqual(analysis.read_bytes(), anlz)

    def test_explicit_wav_relocation_preserves_extra_memberships_and_existing_order(self):
        analysis, _ = self.fixture.analysis()
        memberships = self.memberships()
        variant = self.fixture.root / 'variant.wav'
        shutil.copyfile(self.fixture.paths[0], variant)
        desired = self.fixture.desired(('1',))
        desired[0].update(path=str(variant), source_path=str(self.fixture.paths[0]),
                          verified_path_sha256=file_hash(variant),
                          verified_source_sha256=file_hash(self.fixture.paths[0]))

        result = self.apply(desired, operation_kind='relocate')

        self.assertIsNone(result['error'], result)
        self.assertTrue(result['reconciled'])
        self.assertEqual(self.memberships(), memberships)
        adapter = self.fixture.factory()
        try:
            self.assertEqual(adapter.db.get_content(ID='1').FolderPath, str(variant))
            self.assertEqual(adapter.db.get_content(ID='2').FolderPath, str(self.fixture.paths[1]))
        finally:
            adapter.close()

    def test_additive_reconciliation_detects_lost_extra_membership_and_restores(self):
        desired = self.fixture.desired(('3',))
        preview = self.preview(desired)
        inventory = self.fixture.inventory()
        original = PyrekordboxAdapter.apply_operations

        def corrupt_apply(adapter, plan):
            original(adapter, plan)
            extra = adapter.db.get_playlist_songs(ID='member-1')
            if extra is not None:
                adapter.db.delete(extra)
                adapter.db.flush()

        with patch.object(PyrekordboxAdapter, 'apply_operations', corrupt_apply):
            result = self.apply(desired, preview)

        self.assertEqual((result.get('error') or {}).get('code'), 'reconcile_failed', result)
        self.assertFalse(result['reconciled'])
        self.assertEqual(self.fixture.inventory(), inventory)
        self.assertEqual(Journal(self.fixture.path).data['phase'], 'restored')

    def test_wav_reconciliation_restores_metadata_changes_to_selected_and_extra_content(self):
        for corrupted_content_id in ('1', '2'):
            with self.subTest(content_id=corrupted_content_id):
                self.fixture = Fixture()
                analysis, _ = self.fixture.analysis()
                variant = self.fixture.root / 'variant.wav'
                shutil.copyfile(self.fixture.paths[0], variant)
                desired = self.fixture.desired(('1',))
                desired[0].update(path=str(variant), source_path=str(self.fixture.paths[0]),
                                  verified_path_sha256=file_hash(variant),
                                  verified_source_sha256=file_hash(self.fixture.paths[0]))
                preview = self.preview(desired, operation_kind='relocate')
                inventory, original_analysis = self.fixture.inventory(), analysis.read_bytes()
                original = PyrekordboxAdapter.apply_operations

                def corrupt_apply(adapter, plan):
                    original(adapter, plan)
                    adapter.db.get_content(ID=corrupted_content_id).Title = 'Unintended metadata rewrite'
                    adapter.db.flush()

                with patch.object(PyrekordboxAdapter, 'apply_operations', corrupt_apply):
                    result = self.apply(desired, preview, operation_kind='relocate')

                self.assertEqual((result.get('error') or {}).get('code'), 'reconcile_failed', result)
                self.assertFalse(result['reconciled'])
                self.assertEqual(self.fixture.inventory(), inventory)
                self.assertEqual(analysis.read_bytes(), original_analysis)
                self.assertEqual(Journal(self.fixture.path).data['phase'], 'restored')

    def test_media_state_accepts_preserved_extras_and_existing_order(self):
        from app.rekordbox_media import MediaStore
        from app.rekordbox_service import media_state_from_plan
        desired = self.fixture.desired(('2',))
        plan = self.preview(desired)['plan']

        state = media_state_from_plan(desired, plan, MediaStore(self.fixture.root / 'media.json'))

        self.assertEqual(state['mode'], 'original')
        self.assertEqual(state['membership'], 'complete')
        self.assertEqual(state['missing'], 0)
        self.assertEqual(state['preserved'], 1)
        self.assertEqual(state['tracks'][0]['content_id'], '2')

    def test_reconciliation_restores_unintended_existing_content_metadata_change(self):
        desired = self.fixture.desired(('3',))
        preview, inventory = self.preview(desired), self.fixture.inventory()
        original = PyrekordboxAdapter.apply_operations

        def corrupt_apply(adapter, plan):
            original(adapter, plan)
            adapter.db.get_content(ID='1').Rating = 1
            adapter.db.flush()

        with patch.object(PyrekordboxAdapter, 'apply_operations', corrupt_apply):
            result = self.apply(desired, preview)

        self.assertEqual((result.get('error') or {}).get('code'), 'reconcile_failed', result)
        self.assertEqual(self.fixture.inventory(), inventory)
        self.assertEqual(Journal(self.fixture.path).data['phase'], 'restored')

    def test_media_state_disjoint_target_can_append_source_without_discarding_extras(self):
        from app.rekordbox_media import MediaStore
        from app.rekordbox_service import media_state_from_plan
        desired = self.fixture.desired(('3',))
        plan = self.preview(desired)['plan']

        state = media_state_from_plan(desired, plan, MediaStore(self.fixture.root / 'media.json'))

        self.assertEqual(state['mode'], 'empty')
        self.assertEqual(state['membership'], 'partial')
        self.assertEqual(state['missing'], 1)
        self.assertEqual(state['preserved'], 2)

    def test_persisted_activity_distinguishes_preview_apply_reconcile_and_refused_write(self):
        from app import activity_log
        log_root = self.fixture.root / 'activity'
        activity_log.initialize(log_root)
        self.addCleanup(activity_log.initialize, Path(os.environ['DECKPIPE_DATA_DIR']))
        desired = self.fixture.desired(('3',))

        preview = self.preview(desired)
        applied = self.apply(desired, preview)
        refused = self.preview(desired, dry_run=False, confirmation_token='synthetic-wrong-token',
                               expected_plan_hash=preview['plan']['hash'])

        self.assertTrue(applied['reconciled'], applied)
        self.assertEqual(refused['error']['code'], 'apply_not_confirmed')
        path = log_root / 'logs' / 'activity.jsonl'
        events = list(reversed(activity_log.list_entries()['entries']))
        self.assertEqual([row['stage'] for row in events if row['operation'] == 'rekordbox'],
                         ['preview_started', 'preview_ready', 'apply_started', 'reconciled', 'failed'])
        self.assertEqual(events, [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()])
        self.assertEqual(events[-1]['error_code'], 'apply_not_confirmed')
        self.assertNotIn('synthetic-wrong-token', path.read_text(encoding='utf-8'))
        self.assertNotIn(str(self.fixture.path), path.read_text(encoding='utf-8'))
        self.preview(desired, log_activity=False)
        self.assertEqual(events, list(reversed(activity_log.list_entries()['entries'])))

    def test_activity_reports_pending_recovery_and_verified_restore_without_false_failure(self):
        from app import activity_log
        from qa.tests.test_rekordbox_final_boundaries import interrupted
        activity_log.initialize(self.fixture.root / 'activity')
        self.addCleanup(activity_log.initialize, Path(os.environ['DECKPIPE_DATA_DIR']))
        inventory = self.fixture.inventory()
        interrupted(self.fixture)

        preview = rb.recover_operation(adapter_factory=self.fixture.factory)
        result = rb.recover_operation(adapter_factory=self.fixture.factory, dry_run=False,
                                      confirmation_token=rb.RECOVERY_CONFIRMATION_TOKEN,
                                      expected_plan_hash=preview['plan']['hash'])

        self.assertEqual(result['error']['code'], 'recovery_restored_preview_required')
        self.assertEqual(self.fixture.inventory(), inventory)
        events = list(reversed(activity_log.list_entries()['entries']))
        self.assertEqual([row['stage'] for row in events],
                         ['recovery_preview_started', 'recovery_required', 'recovery_started', 'recovered'])


if __name__ == '__main__':
    unittest.main()
