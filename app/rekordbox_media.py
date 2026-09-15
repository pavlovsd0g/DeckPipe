"""Durable, non-destructive WAV variants and exact native-timeline verification."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import uuid
import wave
from pathlib import Path

import mutagen

from .atomic_io import atomic_write_json, cleanup_owned_stages, file_lock, publish_staged_file
from .converter import FFMPEG, convert_to_wav


class MediaError(RuntimeError):
    def __init__(self, code='media_verification_failed'):
        self.code = code
        super().__init__(code)


def path_key(path):
    return os.path.normcase(str(Path(path).resolve()))


def file_digest(path):
    """Fingerprint exact bytes and reject replacements/edits during the read."""
    path = Path(path)
    try:
        before = path.stat()
        if path.is_symlink() or not path.is_file():
            raise MediaError()
        digest = hashlib.sha256()
        with path.open('rb') as handle:
            opened = os.fstat(handle.fileno())
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(chunk)
            after = os.fstat(handle.fileno())
        def stamp(s):
            # CPython 3.12 Windows path.stat and fstat expose different ctime
            # meanings; file ID, length and last-write time agree across both.
            return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns
        if stamp(before) != stamp(opened) or stamp(opened) != stamp(after) or stamp(after) != stamp(path.stat()):
            raise MediaError('media_verification_changed')
        return digest.hexdigest()
    except OSError:
        raise MediaError('media_file_unavailable') from None


def _decode(path, bit_depth):
    try:
        info = mutagen.File(path).info
        rate, channels = int(info.sample_rate), int(info.channels)
        if rate <= 0 or channels <= 0:
            raise ValueError()
    except Exception:
        raise MediaError('media_metadata_invalid') from None
    # No -ar/-ac: ffmpeg retains native rate/channels. Only sample format changes.
    digest, count = hashlib.sha256(), 0
    with tempfile.TemporaryFile() as errors:
        try:
            process = subprocess.Popen([FFMPEG, '-nostdin', '-v', 'error', '-xerror', '-i', str(path),
                '-map', '0:a:0', '-vn', '-c:a', f'pcm_s{bit_depth}le', '-f', f's{bit_depth}le', 'pipe:1'],
                stdout=subprocess.PIPE, stderr=errors)
        except OSError:
            raise MediaError('media_decoder_unavailable') from None
        try:
            for chunk in iter(lambda: process.stdout.read(1024 * 1024), b''):
                count += len(chunk)
                digest.update(chunk)
            code = process.wait()
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill()
                process.wait()
    frame_bytes = channels * (bit_depth // 8)
    if code or not count or count % frame_bytes:
        raise MediaError('media_decode_failed')
    frames = count // frame_bytes
    return dict(sample_rate=rate, channels=channels, frames=frames,
                duration=frames / rate, bit_depth=bit_depth, sha256=digest.hexdigest())


def verify_timeline(source, variant, bit_depth=16):
    if bit_depth not in (16, 24, 32):
        raise MediaError('media_bit_depth_invalid')
    source, variant = Path(source), Path(variant)
    first = file_digest(source), file_digest(variant)
    try:
        with wave.open(str(variant), 'rb') as wav:
            if wav.getsampwidth() * 8 != bit_depth or wav.getcomptype() != 'NONE':
                raise ValueError()
            native = wav.getframerate(), wav.getnchannels(), wav.getnframes()
    except (ValueError, OSError, wave.Error):
        raise MediaError('media_variant_format_invalid') from None
    source_pcm, variant_pcm = _decode(source, bit_depth), _decode(variant, bit_depth)
    if native != (variant_pcm['sample_rate'], variant_pcm['channels'], variant_pcm['frames']):
        raise MediaError('media_timeline_mismatch')
    if source_pcm != variant_pcm:
        raise MediaError('media_timeline_mismatch')
    if first != (file_digest(source), file_digest(variant)):
        raise MediaError('media_verification_changed')
    return dict(source_sha256=first[0], variant_sha256=first[1], pcm=source_pcm)


class MediaStore:
    def __init__(self, path=None):
        if path is None:
            from .deezer_client import ROOT
            path = ROOT / 'rekordbox-media.json'
        self.path = Path(path)

    def load(self):
        if not self.path.exists():
            return {'version': 1, 'entries': {}}
        try:
            value = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('entries'), dict):
                raise ValueError()
            for key, entry in value['entries'].items():
                if not isinstance(entry, dict):
                    raise ValueError()
                if key != path_key(entry['source']) or not Path(entry['source']).is_absolute() or not Path(entry['variant']).is_absolute():
                    raise ValueError()
                if path_key(entry['source']) == path_key(entry['variant']) or Path(entry['source']).parent != Path(entry['variant']).parent:
                    raise ValueError()
                if Path(entry['variant']).suffix.lower() != '.wav' or entry['bit_depth'] not in (16, 24, 32):
                    raise ValueError()
                if entry['state'] not in ('prepared', 'publishing'):
                    raise ValueError()
                for field in ('source_sha256', 'variant_sha256'):
                    digest = entry[field]
                    if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
                        raise ValueError()
            return value
        except (ValueError, KeyError, TypeError, OSError):
            raise MediaError('media_mapping_invalid') from None

    def entry(self, source):
        return self.load()['entries'].get(path_key(source))

    def verify(self, source):
        entry = self.entry(source)
        if not entry:
            raise MediaError('wav_not_prepared')
        # Fingerprints pinned at creation forbid a changed original even when its
        # newly decoded samples happen to be identical (e.g. changed tags).
        if file_digest(entry['source']) != entry['source_sha256'] or file_digest(entry['variant']) != entry['variant_sha256']:
            raise MediaError('media_verification_changed')
        proof = verify_timeline(entry['source'], entry['variant'], entry['bit_depth'])
        if proof['source_sha256'] != entry['source_sha256'] or proof['variant_sha256'] != entry['variant_sha256']:
            raise MediaError('media_verification_changed')
        return {**entry, **proof}

    def prepare(self, source, bit_depth=16):
        source = Path(source).resolve()
        if bit_depth not in (16, 24, 32):
            raise MediaError('media_bit_depth_invalid')
        with file_lock(self.path):
            data = self.load()
            key = path_key(source)
            if key in data['entries'] and data['entries'][key]['state'] == 'publishing' and not Path(data['entries'][key]['variant']).exists():
                # Explicit retry after intent was saved but publication never
                # happened. No extant file is touched; retain source continuity.
                if file_digest(source) != data['entries'][key]['source_sha256']:
                    raise MediaError('media_verification_changed')
                del data['entries'][key]
                atomic_write_json(self.path, data)
            if key in data['entries']:
                entry = self.verify(source)
                if entry['bit_depth'] != bit_depth:
                    raise MediaError('media_bit_depth_conflict')
                if entry['state'] != 'prepared':
                    data['entries'][key]['state'] = 'prepared'
                    atomic_write_json(self.path, data)
                return data['entries'][key], False
            stage = None
            before = file_digest(source)
            try:
                stage = convert_to_wav(source, bit_depth)
                proof = verify_timeline(source, stage, bit_depth)
                if proof['source_sha256'] != before:
                    raise MediaError('media_verification_changed')
                # Unique output avoids collisions with unrelated same-name WAVs.
                variant = source.with_name(source.stem + '.deckpipe-wav-' + uuid.uuid4().hex + '.wav')
                entry = dict(source=str(source), variant=str(variant), bit_depth=bit_depth, state='publishing', **proof)
                data['entries'][key] = entry
                # Intent + verified hashes precede publication; after a crash a
                # fresh verification can reuse exactly this owned pair.
                atomic_write_json(self.path, data)
                publish_staged_file(stage, variant)
                stage = None
                self.verify(source)
                entry['state'] = 'prepared'
                atomic_write_json(self.path, data)
                return entry, True
            finally:
                cleanup_owned_stages(stage)

    def owned_variants(self):
        """Exclude only recorded variants whose exact source+target bytes match.

        The PCM proof was durable before publication. Changed files re-enter the
        ordinary catalog, so user replacements are never hidden by their name.
        """
        excluded = set()
        for entry in self.load()['entries'].values():
            try:
                if file_digest(entry['source']) == entry['source_sha256'] and file_digest(entry['variant']) == entry['variant_sha256']:
                    excluded.add(path_key(entry['variant']))
            except MediaError:
                continue
        return excluded
