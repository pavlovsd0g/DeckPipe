"""Identity-preserving adapter for the pinned pyrekordbox 0.4.4 schema."""
from __future__ import annotations
import ntpath
import os
import threading
import uuid
import struct
import logging
import re
from pathlib import Path
from datetime import datetime
from .rekordbox_recovery import digest, file_hash

# The pinned registry buffer is class-global. Serialize handles in this process,
# including preview handles, to prevent one close() clearing another transaction.
_registry_lock = threading.RLock()
_RELOCATION_FIELDS = frozenset({'FolderPath', 'FileNameL', 'FileSize', 'FileType',
                                'SampleRate', 'BitDepth', 'rb_local_usn', 'updated_at'})


class _NoKeyLog(logging.Filter):
    def filter(self, record):
        return not str(record.msg).startswith('Key:')


logging.getLogger('pyrekordbox.db6.database').addFilter(_NoKeyLog())


def canonical_path(value):
    # Windows is case-insensitive but DOES NOT equate Unicode NFC and NFD.
    if os.name == 'nt' and value:
        value = str(Path(value).resolve(strict=False))
    return ntpath.normcase(ntpath.normpath(str(value).replace('/', '\\')))


def rewrite_anlz_path(data, path):
    """Replace PPTH only, preserving even tags unknown to the pinned parser."""
    if len(data) < 12 or data[:4] != b'PMAI':
        raise AdapterError('invalid_analysis_file')
    header_length, length = struct.unpack_from('>II', data, 4)
    if length != len(data) or header_length < 12:
        raise AdapterError('invalid_analysis_file')
    sections = []
    offset = header_length
    paths = 0
    while offset < length:
        if offset + 12 > length:
            raise AdapterError('invalid_analysis_file')
        tag = data[offset:offset + 4]
        tag_header, tag_length = struct.unpack_from('>II', data, offset + 4)
        if tag_length < tag_header or tag_header < 12 or offset + tag_length > length:
            raise AdapterError('invalid_analysis_file')
        section = data[offset:offset + tag_length]
        if tag == b'PPTH':
            encoded = path.replace('\\', '/').encode('utf-16-be') + b'\0\0'
            section = b'PPTH' + struct.pack('>III', 16, 16 + len(encoded), len(encoded)) + encoded
            paths += 1
        sections.append(section)
        offset += tag_length
    if paths != 1:
        raise AdapterError('ambiguous_analysis_path')
    header = bytearray(data[:header_length])
    struct.pack_into('>I', header, 8, header_length + sum(map(len, sections)))
    return bytes(header) + b''.join(sections)


class AdapterError(RuntimeError):
    pass


class PyrekordboxAdapter:
    def __init__(self, *, path=None, db_dir=None, key=None, running_check=None):
        from .rekordbox import db_path, rb_running
        import pyrekordbox
        from pyrekordbox import Rekordbox6Database
        if str(pyrekordbox.__version__) != '0.4.4':
            raise AdapterError('unsupported_pyrekordbox_version')
        self.db_path = Path(path) if path is not None else db_path()
        self._kwargs = {'path': self.db_path, 'db_dir': Path(db_dir) if db_dir else self.db_path.parent}
        if key is not None:
            self._kwargs['key'] = key
        self._running_check = running_check or rb_running
        self._closed = False
        _registry_lock.acquire()
        try:
            # The pinned constructor otherwise performs installation/config
            # discovery even when given a path and key. All paths are explicit.
            import pyrekordbox.config as config
            previous = config.__config__['rekordbox7']
            config.__config__['rekordbox7'] = {'db_path': self.db_path}
            try:
                self.db = Rekordbox6Database(**self._kwargs)
            finally:
                config.__config__['rekordbox7'] = previous
        except BaseException:
            _registry_lock.release()
            raise
        self._anlz = {}

    def reopen(self):
        return type(self)(**self._kwargs, running_check=self._running_check)

    def read_only(self):
        """Open SQLite with mode=ro, so preview cannot checkpoint/write the DB."""
        from sqlalchemy import create_engine
        self.db.close()
        self.db.engine.dispose()
        url = self.db.engine.url.set(database='file:' + self.db_path.as_posix(),
            query={'uri': 'true', 'mode': 'ro'})
        self.db.engine = create_engine(url, module=self.db.engine.dialect.dbapi)
        self.db.open()

    def close(self):
        if not self._closed:
            try:
                try:
                    self.db.close()
                finally:
                    self.db.engine.dispose()
            finally:
                self._closed = True
                _registry_lock.release()

    def mutation_lock_path(self):
        return self.db_path.with_name('deckpipe-rekordbox-mutation.lock')

    def is_rekordbox_running(self):
        return self._running_check()

    def list_playlists(self):
        return [{'id': str(p.ID), 'name': p.Name, 'count': len(p.Songs)}
            for p in self.db.get_playlist() if p.Attribute == 0]

    def target(self, name, playlist_id=None):
        candidates = [p for p in self.db.get_playlist() if p.Attribute == 0 and
            (str(p.ID) == str(playlist_id) if playlist_id is not None else str(p.Name) == name)]
        if len(candidates) > 1:
            raise AdapterError('ambiguous_playlist_target')
        if playlist_id is not None and not candidates:
            raise AdapterError('playlist_not_found')
        return candidates[0] if candidates else None

    def playlist_exists(self, name):
        return self.target(name) is not None

    def _content_paths(self):
        result = {}
        for content in self.db.get_content():
            result.setdefault(canonical_path(content.FolderPath or ''), []).append(content)
        return result

    def resolve(self, desired):
        paths = self._content_paths()
        resolved = []
        seen = set()
        for raw in desired:
            item = dict(raw)
            item.pop('content_id', None)
            item.pop('existing_path', None)
            keys = {canonical_path(item['path'])}
            if item.get('source_path'):
                keys.add(canonical_path(item['source_path']))
            candidates = {str(c.ID): c for key in keys for c in paths.get(key, [])}
            if len(candidates) > 1:
                raise AdapterError('ambiguous_content_path')
            content = next(iter(candidates.values()), None)
            if content is not None:
                if str(content.ID) in seen:
                    raise AdapterError('duplicate_desired_content')
                seen.add(str(content.ID))
                item['content_id'] = str(content.ID)
                item['existing_path'] = str(content.FolderPath)
            resolved.append(item)
        return resolved

    def snapshot_playlist(self, name, playlist_id=None, desired=None):
        target = self.target(name, playlist_id)
        if target is None:
            return []
        identities = {i['content_id']: i['provider_id'] for i in desired or [] if i.get('content_id')}
        contents = {str(c.ID): c for c in self.db.get_content()}
        result = []
        for song in sorted(self.db.get_playlist_songs(PlaylistID=target.ID), key=lambda s: int(s.TrackNo)):
            content = contents.get(str(song.ContentID))
            if content is None:
                raise AdapterError('missing_playlist_content')
            result.append({'provider_id': identities.get(str(content.ID), 'rb:' + str(content.ID)),
                'title': content.Title or '', 'artist': '', 'album': '',
                'duration': content.Length or 0, 'position': int(song.TrackNo),
                'path': content.FolderPath or '', 'content_id': str(content.ID),
                'membership_id': str(song.ID), 'membership_fingerprint': digest(song.to_dict())})
        if len({r['content_id'] for r in result}) != len(result):
            raise AdapterError('ambiguous_current_membership')
        return result

    def fingerprint(self):
        """Logical fingerprint includes all tables, metadata, registry and schema/WAL."""
        connection = self.db.session.connection()
        schema = list(connection.exec_driver_sql("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"))
        tables = [r[1] for r in schema if r[0] == 'table']
        state = [tuple(r) for r in schema]
        for name in tables:
            quoted = '"' + name.replace('"', '""') + '"'
            rows = [tuple(r) for r in connection.exec_driver_sql('SELECT * FROM ' + quoted)]
            state.append((name, sorted(rows, key=lambda r: repr(r))))
        return digest(state)

    def integrity_check(self):
        result = self.db.session.connection().exec_driver_sql('PRAGMA integrity_check').fetchall()
        if result != [('ok',)]:
            raise AdapterError('database_integrity_failed')

    def validate_recovery_schema(self):
        """Reject schema features that cannot be replayed without side effects."""
        connection = self.db.session.connection()
        schema = [tuple(r) for r in connection.exec_driver_sql(
            'SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name')]
        if any(r[0] == 'trigger' or (r[0] == 'table' and re.match(r'\s*CREATE\s+VIRTUAL\s+TABLE\b', r[3] or '', re.I)) for r in schema):
            raise AdapterError('unsupported_recovery_schema')
        tables = []
        for kind, name, _, sql in schema:
            if kind != 'table':
                continue
            quoted = '"' + name.replace('"', '""') + '"'
            columns = list(connection.exec_driver_sql('PRAGMA table_xinfo(' + quoted + ')'))
            if not columns or any(c[6] != 0 for c in columns):
                raise AdapterError('unsupported_recovery_schema')
            names = [c[1] for c in columns]
            rowid = None
            if not re.search(r'\bWITHOUT\s+ROWID\b', sql or '', re.I):
                rowid = next((candidate for candidate in ('rowid', '_rowid_', 'oid') if candidate not in {name.lower() for name in names}), None)
                if rowid is None:
                    raise AdapterError('unsupported_recovery_schema')
            tables.append((name, ([rowid] if rowid else []) + names))
        return schema, sorted(tables, key=lambda item: (item[0] == 'sqlite_sequence', item[0]))

    def restore_database_rows(self, backup_path):
        """Replay a verified same-schema backup inside the caller's write transaction.

        No database file replacement or sidecar deletion. BEGIN IMMEDIATE remains
        held through recognition, replay, external restoration and commit.
        """
        from sqlalchemy import create_engine
        schema, tables = self.validate_recovery_schema()
        destination = self.db.session.connection()
        if not destination.connection.driver_connection.in_transaction:
            raise AdapterError('recovery_transaction_required')
        url = self.db.engine.url.set(database='file:' + Path(backup_path).as_posix(),
            query={'uri': 'true', 'mode': 'ro'})
        engine = create_engine(url, module=self.db.engine.dialect.dbapi)
        quote = lambda name: '"' + name.replace('"', '""') + '"'
        try:
            with engine.connect() as source:
                backup_schema = [tuple(r) for r in source.exec_driver_sql(
                    'SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name')]
                if backup_schema != schema:
                    raise AdapterError('recovery_schema_changed')
                if source.exec_driver_sql('PRAGMA integrity_check').fetchall() != [('ok',)]:
                    raise AdapterError('recovery_backup_damaged')
                destination.exec_driver_sql('PRAGMA defer_foreign_keys=ON')
                for name, _ in tables:
                    destination.exec_driver_sql('DELETE FROM ' + quote(name))
                for name, columns in tables:
                    if name == 'sqlite_sequence':
                        # AUTOINCREMENT inserts above may repopulate this table.
                        destination.exec_driver_sql('DELETE FROM sqlite_sequence')
                    fields = ','.join(quote(c) for c in columns)
                    cursor = source.exec_driver_sql('SELECT ' + fields + ' FROM ' + quote(name))
                    insert = 'INSERT INTO ' + quote(name) + '(' + fields + ') VALUES (' + ','.join('?' for _ in columns) + ')'
                    while rows := cursor.fetchmany(500):
                        destination.exec_driver_sql(insert, [tuple(r) for r in rows])
        finally:
            engine.dispose()
        self.db.session.expire_all()

    def finish_restore(self):
        self.db.session.commit()
        self.db.registry.clear_buffer()

    def external_files_for_plan(self, plan):
        files = {self.db_path.parent / 'masterPlaylists6.xml'}
        if plan['operation_kind'] != 'relocate':
            return sorted(files, key=str)
        contents = {str(c.ID): c for c in self.db.get_content()}
        for item in plan['desired_resolved']:
            content = contents.get(item.get('content_id'))
            if content is not None and canonical_path(content.FolderPath) != canonical_path(item['path']):
                if content.AnalysisDataPath:
                    files.update(self._analysis_paths(content))
        return sorted(files, key=str)

    def _analysis_paths(self, content):
        directory = self.db.get_anlz_dir(content).resolve()
        if not directory.is_relative_to((self.db_path.parent / 'share').resolve()):
            raise AdapterError('invalid_analysis_location')
        paths = [Path(p) for p in self.db.get_anlz_paths(content).values() if p is not None]
        if not paths:
            raise AdapterError('missing_analysis_files')
        return paths

    def context(self, name, desired, playlist_id, operation_kind):
        target = self.target(name, playlist_id)
        resolved = self.resolve(desired)
        current = self.snapshot_playlist(name, playlist_id, resolved)
        preliminary = {'desired_resolved': resolved, 'operation_kind': operation_kind}
        files = self.external_files_for_plan(preliminary)
        media = set()
        for item in resolved:
            media.add(item['path'])
            if item.get('source_path'):
                media.add(item['source_path'])
        content_rows = {str(c.ID): c.to_dict() for c in self.db.get_content()}
        context = {'target': {'id': str(target.ID) if target else None,
                'name': str(target.Name) if target else name}, 'operation_kind': operation_kind,
            'database_fingerprint': self.fingerprint(),
            'media_fingerprints': {p: file_hash(p) for p in sorted(media)},
            'external_fingerprints': {str(p): file_hash(p) for p in files},
            'content_fingerprints': {content_id: digest(row) for content_id, row in content_rows.items()},
            'content_metadata_fingerprints': {content_id: digest({key: value for key, value in row.items()
                if key not in _RELOCATION_FIELDS}) for content_id, row in content_rows.items()}
                if operation_kind == 'relocate' else {},
            'current_memberships': current}
        return resolved, current, context

    def backup_database(self, path):
        # SQLCipher's online backup reads the committed logical DB including WAL.
        # Do not copy master.db alone or checkpoint a read-only preview.
        from sqlalchemy import create_engine
        engine = create_engine(self.db.engine.url.set(database=str(path)), module=self.db.engine.dialect.dbapi)
        source_engine = create_engine(self.db.engine.url, module=self.db.engine.dialect.dbapi)
        source = source_engine.raw_connection()
        target = engine.raw_connection()
        try:
            source.driver_connection.backup(target.driver_connection)
            if target.driver_connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise AdapterError('backup_verification_failed')
        finally:
            target.close()
            source.close()
            source_engine.dispose()
            engine.dispose()

    def begin(self):
        self.db.rollback()
        self.db.session.connection().exec_driver_sql('BEGIN IMMEDIATE')
        self.db.registry.clear_buffer()

    def _new_content(self, item):
        artist = self.db.get_artist(Name=item['artist']).one_or_none()
        if artist is None:
            artist = self.db.add_artist(name=item['artist'])
        album = self.db.get_album(Name=item['album']).one_or_none()
        if album is None:
            album = self.db.add_album(name=item['album'], artist=artist)
        return self.db.add_content(Path(item['path']), Title=item['title'], ArtistID=str(artist.ID),
            AlbumID=str(album.ID), Length=item['duration'])

    def apply_operations(self, plan):
        from pyrekordbox.db6 import tables
        target = self.target(plan['target']['name'], plan['target']['id'])
        if target is None:
            if not plan['_create_missing'] or plan['operation_kind'] == 'relocate':
                raise AdapterError('playlist_not_found')
            target = self.db.create_playlist(plan['target']['name'])
        contents = {str(c.ID): c for c in self.db.get_content()}
        songs = {str(s.ContentID): s for s in self.db.get_playlist_songs(PlaylistID=target.ID)}
        operations = plan['add'] if plan['operation_kind'] == 'sync' else plan['path']
        for item in operations:
            content = contents.get(item.get('content_id'))
            if content is None:
                if plan['operation_kind'] == 'relocate':
                    raise AdapterError('relocate_membership_change')
                content = self._new_content(item)
            if plan['operation_kind'] == 'relocate' and canonical_path(content.FolderPath) != canonical_path(item['path']):
                if str(content.ID) not in songs:
                    raise AdapterError('relocate_membership_change')
                if not item.get('source_path'):
                    raise AdapterError('unverified_path_alias')
                if content.AnalysisDataPath:
                    for path in self._analysis_paths(content):
                        self._anlz[Path(path)] = rewrite_anlz_path(Path(path).read_bytes(), item['path'])
                content.FolderPath = item['path']
                content.FileNameL = Path(item['path']).name
                content.FileSize = Path(item['path']).stat().st_size
                content.FileType = getattr(tables.FileType, Path(item['path']).suffix[1:].upper()).value
                from mutagen import File as AudioFile
                audio = AudioFile(item['path'])
                if audio is None or not getattr(audio, 'info', None):
                    raise AdapterError('invalid_relocation_media')
                content.SampleRate = int(audio.info.sample_rate)
                # Compressed formats do not expose an integer PCM bit depth.
                content.BitDepth = int(getattr(audio.info, 'bits_per_sample', 0) or 0)
            if plan['operation_kind'] == 'sync':
                song = songs.get(str(content.ID))
                if song is None:
                    song = tables.DjmdSongPlaylist.create(ID=str(uuid.uuid4()), UUID=str(uuid.uuid4()),
                        PlaylistID=str(target.ID), ContentID=str(content.ID), TrackNo=item['position'],
                        created_at=datetime.now(), updated_at=datetime.now())
                    self.db.add(song)
        if plan['operation_kind'] == 'sync':
            target.updated_at = datetime.now()
        self.db.flush()

    def prepare_commit(self):
        self.db.registry.autoincrement_local_update_count(set_row_usn=True)
        self.db.flush()
        payloads = dict(self._anlz)
        xml = self.db.playlist_xml
        if xml is not None:
            for p in self.db.get_playlist():
                record = xml.get(p.ID)
                if record is not None and abs((p.updated_at - record['Timestamp']).total_seconds()) > 1:
                    xml.update(p.ID, updated_at=p.updated_at)
            if xml.modified:
                payloads[Path(xml.path)] = xml.to_string().encode('utf-8')
        return payloads

    def commit(self):
        # db.commit() also publishes XML outside the transaction. Registry was
        # applied in prepare_commit; publish those bytes via the durable journal.
        self.db.session.commit()
        self.db.registry.clear_buffer()

    def rollback(self):
        try:
            self.db.rollback()
        finally:
            self.db.registry.enable_tracking()
