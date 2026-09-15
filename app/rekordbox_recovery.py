"""Durable, hash-checked recovery for a single Rekordbox transaction.

The OS lock deliberately keeps its filename: ownership is the kernel lock, never
the existence or recorded PID. Journals and backups remain as diagnostic evidence.
"""
from __future__ import annotations
import hashlib
import json
import os
import shutil
import uuid
from contextlib import contextmanager, ExitStack
from .rekordbox_file_ownership import OwnedFile, OwnershipError
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), default=str).encode('utf-8')).hexdigest()


def file_hash(path):
    path = Path(path)
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def atomic_bytes(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with tmp.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@contextmanager
def mutation_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open('a+b')
    acquired = False
    try:
        if stream.seek(0, 2) == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            pass
        yield acquired
    finally:
        if acquired:
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_UN)
        stream.close()


class RecoveryError(RuntimeError):
    pass


class Journal:
    def __init__(self, db_path):
        self.db_path = Path(db_path).absolute()
        self.path = self.db_path.with_name('deckpipe-rekordbox-operation.json')
        self.data = None
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding='utf-8'))
                self._validate()
            except Exception:
                raise RecoveryError('recovery_journal_invalid') from None

    def _validate(self):
        def require(condition):
            if not condition:
                raise ValueError('Invalid journal record')
        def hex_value(value, length):
            return isinstance(value, str) and len(value) == length and all(c in '0123456789abcdef' for c in value)
        def path_value(value):
            require(isinstance(value, str) and bool(value) and '\0' not in value and Path(value).is_absolute())
            return os.path.normcase(os.path.normpath(value))
        data = self.data
        fields = {'version', 'id', 'phase', 'database_path', 'database_before', 'database_after',
            'backup', 'backup_hash', 'plan_hash', 'external'}
        require(isinstance(data, dict) and set(data) == fields)
        require(type(data['version']) is int and data['version'] == 1 and hex_value(data['id'], 32))
        phase = data['phase']
        require(isinstance(phase, str) and phase in {'prepared', 'commit_pending', 'database_committed',
            'external_published', 'complete', 'restoring', 'restored'})
        require(path_value(data['database_path']) == path_value(str(self.db_path)))
        root = self.db_path.parent / 'deckpipe-rekordbox-backups' / data['id']
        require(path_value(data['backup']) == path_value(str(root / 'master.db')))
        require(hex_value(data['database_before'], 64) and hex_value(data['backup_hash'], 64) and hex_value(data['plan_hash'], 64))
        if phase == 'prepared':
            require(data['database_after'] is None)
        elif phase in ('restoring', 'restored'):
            require(data['database_after'] is None or hex_value(data['database_after'], 64))
        else:
            require(hex_value(data['database_after'], 64))
        require(isinstance(data['external'], list))
        seen = set()
        for index, item in enumerate(data['external']):
            require(isinstance(item, dict) and {'path', 'backup', 'before', 'after'}.issubset(item)
                and set(item).issubset({'path', 'backup', 'before', 'after', 'staged', 'moves'}))
            original = path_value(item['path'])
            xml = path_value(str(self.db_path.parent / 'masterPlaylists6.xml'))
            share = path_value(str(self.db_path.parent / 'share')) + os.sep
            require(original == xml or original.startswith(share))
            require(original not in seen)
            seen.add(original)
            backup = root / ('external-' + str(index))
            require(path_value(item['backup']) == path_value(str(backup)))
            require(item['before'] is None or hex_value(item['before'], 64))
            require(item['after'] is None or hex_value(item['after'], 64))
            if 'staged' in item:
                require(path_value(item['staged']) == path_value(str(backup.with_suffix('.after'))))
                require(hex_value(item['after'], 64))
            else:
                require(item['after'] == item['before'])
            moves = item.get('moves', [])
            require(isinstance(moves, list))
            moved_paths = set()
            for move in moves:
                require(isinstance(move, dict) and set(move) == {'path', 'hash'})
                moved = path_value(move['path'])
                prefix = path_value(str(backup)) + '.moved-'
                require(moved.startswith(prefix) and hex_value(moved[len(prefix):], 32))
                require(moved not in moved_paths and hex_value(move['hash'], 64))
                require(move['hash'] in (item['before'], item['after']))
                moved_paths.add(moved)

    @property
    def pending(self):
        return self.data is not None and self.data['phase'] not in ('complete', 'restored')

    def save(self, **updates):
        self.data.update(updates)
        try:
            self._validate()
        except Exception:
            raise RecoveryError('recovery_journal_invalid') from None
        atomic_bytes(self.path, json.dumps(self.data, ensure_ascii=False,
            sort_keys=True).encode('utf-8'))

    def prepare(self, adapter, plan):
        adapter.validate_recovery_schema()
        operation_id = uuid.uuid4().hex
        root = adapter.db_path.parent / 'deckpipe-rekordbox-backups' / operation_id
        root.mkdir(parents=True)
        backup = root / 'master.db'
        adapter.backup_database(backup)
        inventory = []
        for index, path in enumerate(adapter.external_files_for_plan(plan)):
            saved = root / ('external-' + str(index))
            before = file_hash(path)
            if before is not None:
                shutil.copyfile(path, saved)
                if file_hash(saved) != before:
                    raise RecoveryError('backup_verification_failed')
            inventory.append({'path': str(path), 'backup': str(saved),
                'before': before, 'after': before})
        self.data = {'version': 1, 'id': operation_id, 'phase': 'prepared',
            'database_path': str(adapter.db_path.resolve()), 'database_before': adapter.fingerprint(),
            'database_after': None, 'backup': str(backup), 'backup_hash': file_hash(backup),
            'plan_hash': plan['hash'], 'external': inventory}
        self.save()

    def stage_external(self, payloads):
        inventory = self.data['external']
        by_path = {str(Path(item['path'])): item for item in inventory}
        for path, payload in payloads.items():
            item = by_path.get(str(Path(path)))
            if item is None:
                raise RecoveryError('unplanned_external_file')
            staged = Path(item['backup']).with_suffix('.after')
            atomic_bytes(staged, payload)
            item['staged'] = str(staged)
            item['after'] = file_hash(staged)
        self.save()

    def _own_external(self, stack):
        owners = {}
        for item in self.data['external']:
            try:
                owner = stack.enter_context(OwnedFile(item['path']))
            except FileNotFoundError:
                owner = None
            owners[item['path']] = owner
        return owners

    def _recognized_missing(self, item, stack):
        # An interrupted owned rename can leave a path absent. Accept that
        # transition only with the durably named, still verified displaced file.
        for move in reversed(item.get('moves', [])):
            try:
                owner = stack.enter_context(OwnedFile(move['path'], read_only=True))
            except FileNotFoundError:
                continue
            if owner.digest() == move['hash']:
                return True
        return False

    def _replace_owned(self, item, owner, payload, stack):
        replacement = None
        if payload is not None:
            temporary = Path(item['backup'] + '.publish-' + uuid.uuid4().hex)
            replacement = stack.enter_context(OwnedFile(temporary, create=True))
            replacement.write(payload)
        if owner is not None:
            displaced = item['backup'] + '.moved-' + uuid.uuid4().hex
            item.setdefault('moves', []).append({'path': displaced, 'hash': owner.digest()})
            self.save()  # Persist the transition before changing either name.
            owner.rename(displaced)
        if replacement is not None:
            # ReplaceIfExists=False is essential, including originally absent
            # paths and foreign creations after the old object was displaced.
            replacement.rename(item['path'])
        return replacement

    def _verify_external(self, owners, field):
        for item in self.data['external']:
            owner = owners[item['path']]
            if owner is None:
                if Path(item['path']).exists():
                    raise RecoveryError('recovery_external_changed')
                actual = None
            else:
                actual = owner.digest()
            if actual != item[field]:
                raise RecoveryError('recovery_external_changed')

    def publish(self):
        try:
            with ExitStack() as stack:
                owners = self._own_external(stack)
                self._verify_external(owners, 'before')
                for item in self.data['external']:
                    if 'staged' not in item or item['after'] == item['before']:
                        continue
                    payload = Path(item['staged']).read_bytes()
                    if hashlib.sha256(payload).hexdigest() != item['after']:
                        raise RecoveryError('recovery_staged_file_damaged')
                    owners[item['path']] = self._replace_owned(item, owners[item['path']], payload, stack)
                self._verify_external(owners, 'after')
                self.save(phase='external_published')
        except OwnershipError:
            raise RecoveryError('recovery_external_busy') from None

    def recovery_context(self, adapter, owned_hashes=None):
        # Hash the complete durable record and observed authoritative state.
        # Media/source-provider availability is irrelevant to row restoration.
        files = {self.data['backup']}
        for item in self.data['external']:
            files.update((item['path'], item['backup']))
            files.update(move['path'] for move in item.get('moves', []))
        context = {'operation_id': self.data['id'], 'phase': self.data['phase'],
                   'journal': self.data, 'database': adapter.fingerprint(),
                   'files': {path: owned_hashes[path] if owned_hashes and path in owned_hashes else file_hash(path)
                             for path in sorted(files)}}
        return {'operation_id': self.data['id'], 'phase': self.data['phase'],
                'external_files': len(self.data['external']), 'hash': digest(context)}

    def restore(self, adapter, *, expected_recovery_hash=None):
        """Restore under SQLite writer ownership and mandatory external handles."""
        data = self.data
        if str(adapter.db_path.resolve()) != data['database_path']:
            raise RecoveryError('recovery_database_unrecognized')
        if adapter.is_rekordbox_running():
            raise RecoveryError('rekordbox_running')
        try:
            with ExitStack() as stack:
                backup = stack.enter_context(OwnedFile(data['backup'], read_only=True))
                if backup.digest() != data['backup_hash']:
                    raise RecoveryError('recovery_backup_damaged')
                owners = self._own_external(stack)
                for item in data['external']:
                    if item['before'] is not None:
                        saved = stack.enter_context(OwnedFile(item['backup'], read_only=True))
                        if saved.digest() != item['before']:
                            raise RecoveryError('recovery_backup_damaged')
                    owner = owners[item['path']]
                    actual = owner.digest() if owner is not None else None
                    if actual not in (item['before'], item['after']):
                        if actual is not None or not self._recognized_missing(item, stack):
                            raise RecoveryError('recovery_external_changed')
                # The authoritative recognition is INSIDE the same SQLite write
                # transaction as restoration. No close/replace interval exists.
                adapter.begin()
                state = adapter.fingerprint()
                if state not in (data['database_before'], data['database_after']):
                    raise RecoveryError('recovery_database_changed')
                if expected_recovery_hash is not None:
                    # Re-read identity as well: an earlier preview cannot approve
                    # a replacement journal or a changed backup/file/DB state.
                    owned_hashes = {path: owner.digest() if owner else file_hash(path) for path, owner in owners.items()}
                    if Journal(self.db_path).data != data or self.recovery_context(adapter, owned_hashes)['hash'] != expected_recovery_hash:
                        raise RecoveryError('recovery_stale_preview')
                adapter.validate_recovery_schema()
                self.save(phase='restoring')
                if state != data['database_before']:
                    adapter.restore_database_rows(data['backup'])
                for item in data['external']:
                    owner = owners[item['path']]
                    actual = owner.digest() if owner is not None else None
                    if actual == item['before']:
                        continue
                    payload = None if item['before'] is None else Path(item['backup']).read_bytes()
                    if payload is not None and hashlib.sha256(payload).hexdigest() != item['before']:
                        raise RecoveryError('recovery_backup_damaged')
                    owners[item['path']] = self._replace_owned(item, owner, payload, stack)
                self._verify_external(owners, 'before')
                adapter.integrity_check()
                if adapter.fingerprint() != data['database_before']:
                    raise RecoveryError('rollback_verify_failed')
                adapter.finish_restore()
        except FileNotFoundError:
            adapter.rollback()
            raise RecoveryError('recovery_backup_damaged') from None
        except OwnershipError:
            adapter.rollback()
            raise RecoveryError('recovery_external_busy') from None
        except Exception:
            adapter.rollback()
            raise
        # Ownership is now released after the completed restore transaction;
        # a subsequent legitimate writer is preserved and causes refusal below.
        adapter.close()
        verifier = adapter.reopen()
        try:
            verifier.read_only()
            verifier.integrity_check()
            if verifier.fingerprint() != data['database_before']:
                raise RecoveryError('recovery_database_changed')
        finally:
            verifier.close()
        self.save(phase='restored')
