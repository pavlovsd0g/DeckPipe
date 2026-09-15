"""Generated fixture only. Never invokes Rekordbox discovery or credential lookup."""
from pathlib import Path
import uuid
import wave
import struct
from datetime import datetime
from sqlalchemy import create_engine, MetaData
from sqlcipher3 import dbapi2 as sqlcipher
from pyrekordbox.db6 import tables
from app.rekordbox_adapter import PyrekordboxAdapter

LAB = Path('D:/DeckPipe-RC-Lab/qa-evidence/rekordbox-stage-c-20260915')
SYNTHETIC_KEY = '402fd-deckpipe-synthetic-fixture-not-a-real-key'


def row(model, **kwargs):
    for column in model.__table__.columns:
        if column.type.__class__.__name__ == "DateTime" and column.name not in kwargs:
            kwargs[column.name] = datetime.now()
    return model.create(**kwargs)


class Fixture:
    def __init__(self, root=None, initialize=True):
        self.root = Path(root) if root else LAB / ('db-' + uuid.uuid4().hex)
        if not self.root.resolve().is_relative_to(LAB.resolve()):
            raise ValueError('Fixture must stay in the owned Stage C lab')
        self.path = self.root / 'master.db'
        if not initialize:
            return
        self.root.mkdir(parents=True)
        self.paths = []
        for name in ('one', 'two', 'three'):
            path = self.root / (name + '.wav')
            with wave.open(str(path), 'wb') as out:
                out.setparams((1, 2, 44100, 44100, 'NONE', 'not compressed'))
                out.writeframes(b'\0\0' * 44100)
            self.paths.append(path)
        engine = create_engine(f'sqlite+pysqlcipher://:{SYNTHETIC_KEY}@/{self.path}', module=sqlcipher)
        # The pinned ORM annotations infer NOT NULL for optional legacy fields;
        # real Rekordbox SQLite permits NULL there. Copy schema, never mutate ORM.
        metadata = MetaData()
        for table in tables.Base.metadata.tables.values():
            copied = table.to_metadata(metadata)
            for column in copied.columns:
                if not column.primary_key:
                    column.nullable = True
        metadata.create_all(engine)
        engine.dispose()
        (self.root / 'masterPlaylists6.xml').write_text(
            '<MASTER_PLAYLIST Version="3.0.0" AutomaticSync="0"><PRODUCT Version="6.8.0"/><PLAYLISTS/></MASTER_PLAYLIST>', encoding='utf-8')
        adapter = self.factory()
        db = adapter.db
        db.add(row(tables.AgentRegistry, registry_id='localUpdateCount', int_1=0))
        db.add(row(tables.DjmdDevice, ID='1', MasterDBID='fixture', Name='Synthetic'))
        db.add(row(tables.DjmdMenuItems, ID='1', Name='TRACK', rb_local_usn=1))
        for index, path in enumerate(self.paths, 1):
            db.add(row(tables.DjmdContent, ID=str(index), UUID='content-' + str(index),
                Title='User title ' + str(index), Commnt='User comment ' + str(index),
                FolderPath=str(path), FileNameL=path.name, Length=1, BPM=12345,
                Rating=4, AnalysisDataPath='', FileType=1, FileSize=path.stat().st_size))
        target = db.create_playlist('Likes')
        other = db.create_playlist('Other')
        self.target_id = str(target.ID)
        self.other_id = str(other.ID)
        for content_id in ('1', '2'):
            db.add(row(tables.DjmdSongPlaylist, ID='member-' + content_id, UUID='member-' + content_id,
                PlaylistID=target.ID, ContentID=content_id, TrackNo=int(content_id)))
        db.add(row(tables.DjmdSongPlaylist, ID='other-member', UUID='other-member',
            PlaylistID=other.ID, ContentID='1', TrackNo=1))
        db.add(row(tables.DjmdCue, ID='cue-1', ContentID='1', InMsec=231,
            OutMsec=650, Kind=1, Comment='Keep cue', BeatLoopSize=4))
        db.commit()
        adapter.close()

    def factory(self):
        return PyrekordboxAdapter(path=self.path, db_dir=self.root,
            key=SYNTHETIC_KEY, running_check=lambda: False)

    def desired(self, ids=('2', '1', '3')):
        return [{'provider_id': 'deezer:' + i, 'title': 'Source title ' + i,
            'artist': 'Source artist', 'album': 'Source album', 'duration': 1,
            'position': position, 'path': str(self.root / ({'1': 'one', '2': 'two', '3': 'three'}[i] + '.wav'))}
            for position, i in enumerate(ids, 1)]

    def inventory(self):
        adapter = self.factory()
        try:
            return adapter.fingerprint()
        finally:
            adapter.close()

    def analysis(self):
        path = self.root / 'share' / 'fixture' / 'ANLZ0000.DAT'
        path.parent.mkdir(parents=True)
        encoded = str(self.root / 'one.wav').replace('\\', '/').encode('utf-16-be') + b'\0\0'
        path_tag = b'PPTH' + struct.pack('>III', 16, 16 + len(encoded), len(encoded)) + encoded
        grid = b'PQTZ' + struct.pack('>IIIIIHHI', 24, 32, 0, 0x80000, 1, 1, 12345, 231)
        # Preserve future/unknown sections byte-for-byte as well.
        unknown = b'ZZZZ' + struct.pack('>II', 12, 16) + b'KEEP'
        payload = path_tag + grid + unknown
        path.write_bytes(b'PMAI' + struct.pack('>II', 28, 28 + len(payload)) + bytes(16) + payload)
        adapter = self.factory()
        adapter.db.get_content(ID='1').AnalysisDataPath = '/fixture/ANLZ0000.DAT'
        adapter.db.commit()
        adapter.close()
        return path, grid + unknown

    def protected(self):
        adapter = self.factory()
        try:
            return {'content': [c.to_dict() for c in adapter.db.get_content()],
                'cues': [c.to_dict() for c in adapter.db.get_cue()],
                'other': adapter.db.get_playlist_songs(ID='other-member').to_dict()}
        finally:
            adapter.close()
