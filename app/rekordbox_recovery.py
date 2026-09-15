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
from contextlib import contextmanager
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
        self.path = Path(db_path).with_name('deckpipe-rekordbox-operation.json')
        self.data = None
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding='utf-8'))
                if self.data['version'] != 1:
                    raise ValueError()
                required = {'id', 'phase', 'database_path', 'database_before', 'database_after',
                    'backup', 'backup_hash', 'plan_hash', 'external'}
                if not required.issubset(self.data) or self.data['phase'] not in {
                        'prepared', 'commit_pending', 'database_committed', 'external_published', 'complete', 'restored'}:
                    raise ValueError()
                if len(self.data['id']) != 32 or any(c not in '0123456789abcdef' for c in self.data['id']):
                    raise ValueError()
                if not isinstance(self.data['external'], list):
                    raise ValueError()
            except Exception:
                raise RecoveryError('recovery_journal_invalid') from None

    @property
    def pending(self):
        return self.data is not None and self.data['phase'] not in ('complete', 'restored')

    def save(self, **updates):
        self.data.update(updates)
        atomic_bytes(self.path, json.dumps(self.data, ensure_ascii=False,
            sort_keys=True).encode('utf-8'))

    def prepare(self, adapter, plan):
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

    def publish(self):
        for item in self.data['external']:
            if 'staged' not in item:
                continue
            if file_hash(item['path']) != item['before']:
                raise RecoveryError('recovery_external_changed')
            payload = Path(item['staged']).read_bytes()
            if hashlib.sha256(payload).hexdigest() != item['after']:
                raise RecoveryError('recovery_staged_file_damaged')
            atomic_bytes(item['path'], payload)
        self.save(phase='external_published')

    def restore(self, adapter):
        """Restore only a recognized pre/post DB and recognized external files."""
        data = self.data
        if str(adapter.db_path.resolve()) != data['database_path']:
            raise RecoveryError('recovery_database_unrecognized')
        backup_root = adapter.db_path.parent / 'deckpipe-rekordbox-backups' / data['id']
        if not backup_root.resolve().is_relative_to((adapter.db_path.parent / 'deckpipe-rekordbox-backups').resolve()):
            raise RecoveryError('recovery_journal_invalid')
        if Path(data['backup']).resolve() != (backup_root / 'master.db').resolve():
            raise RecoveryError('recovery_journal_invalid')
        for item in data['external']:
            original = Path(item['path']).resolve()
            if original != (adapter.db_path.parent / 'masterPlaylists6.xml').resolve() and not original.is_relative_to((adapter.db_path.parent / 'share').resolve()):
                raise RecoveryError('recovery_journal_invalid')
            if not Path(item['backup']).resolve().is_relative_to(backup_root.resolve()):
                raise RecoveryError('recovery_journal_invalid')
        if adapter.is_rekordbox_running():
            raise RecoveryError('rekordbox_running')
        state = adapter.fingerprint()
        if state not in (data['database_before'], data['database_after']):
            raise RecoveryError('recovery_database_changed')
        if file_hash(data['backup']) != data['backup_hash']:
            raise RecoveryError('recovery_backup_damaged')
        for item in data['external']:
            if item['before'] is not None and file_hash(item['backup']) != item['before']:
                raise RecoveryError('recovery_backup_damaged')
            if file_hash(item['path']) not in (item['before'], item['after']):
                raise RecoveryError('recovery_external_changed')
        # All handles must be disposed before replacement; WAL is included by the
        # consistent online backup and removed only after recognizing this DB.
        adapter.close()
        if state != data['database_before']:
            for suffix in ('-wal', '-shm'):
                Path(str(adapter.db_path) + suffix).unlink(missing_ok=True)
            atomic_bytes(adapter.db_path, Path(data['backup']).read_bytes())
        for item in data['external']:
            if file_hash(item['path']) == item['before']:
                continue
            if item['before'] is None:
                Path(item['path']).unlink(missing_ok=True)
            else:
                atomic_bytes(item['path'], Path(item['backup']).read_bytes())
        verifier = adapter.reopen()
        try:
            verifier.integrity_check()
            if verifier.fingerprint() != data['database_before']:
                raise RecoveryError('rollback_verify_failed')
        finally:
            verifier.close()
        self.save(phase='restored')
