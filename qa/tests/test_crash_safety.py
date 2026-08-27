from __future__ import annotations

import importlib
import json
import multiprocessing
import os
import tempfile
import threading
import time
import traceback
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


def _track(track_id: str, provider: str = "deezer") -> dict[str, object]:
    return {
        "id": track_id,
        "title": f"Title {track_id}",
        "artist": f"Artist {track_id}",
        "album": "Synthetic",
        "duration": 180,
        "provider": provider,
        "url": f"https://example.invalid/{track_id}",
    }


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _process_sidecar_update(directory: str, start: int, count: int) -> None:
    from app import library

    playlist = Path(directory)
    for index in range(start, start + count):
        library.update_track_status(
            playlist,
            f"mp-{index}",
            {"title": f"MP {index}", "artist": "Worker", "file": f"{index}.flac", "status": "ok"},
        )


class CrashSafeSidecarTests(unittest.TestCase):
    def test_concurrent_thread_and_process_sidecar_updates_do_not_lose_tracks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deckpipe-sidecar-concurrent-") as temporary:
            playlist = Path(temporary) / "playlist"
            playlist.mkdir()
            from app import library

            threads = [
                threading.Thread(
                    target=lambda offset=offset: [
                        library.update_track_status(
                            playlist,
                            f"th-{index}",
                            {"title": f"TH {index}", "artist": "Thread", "file": f"{index}.flac", "status": "ok"},
                        )
                        for index in range(offset, offset + 25)
                    ]
                )
                for offset in (0, 25, 50, 75)
            ]
            for thread in threads:
                thread.start()
            process = multiprocessing.Process(target=_process_sidecar_update, args=(str(playlist), 100, 50))
            process.start()
            for thread in threads:
                thread.join()
            process.join(20)

            self.assertEqual(0, process.exitcode)
            tracks = library.load_sidecar(playlist)["tracks"]
            self.assertEqual(150, len(tracks))
            self.assertTrue(all(f"th-{index}" in tracks for index in range(100)))
            self.assertTrue(all(f"mp-{index}" in tracks for index in range(100, 150)))

    def test_corrupt_primary_recovers_from_valid_backup_without_replacing_backup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deckpipe-sidecar-recover-") as temporary:
            playlist = Path(temporary) / "playlist"
            playlist.mkdir()
            from app import library

            valid = {"tracks": {"1": {"title": "Good", "artist": "A", "file": "good.flac", "status": "ok"}}}
            library.save_sidecar(playlist, valid)
            backup = playlist / f"{library.SIDECAR_NAME}.bak"
            backup.write_text(json.dumps(valid, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            before_backup = backup.read_bytes()
            (playlist / library.SIDECAR_NAME).write_text('{"tracks": {"broken": ', encoding="utf-8")

            loaded = library.load_sidecar(playlist)

            self.assertEqual(valid, loaded)
            self.assertEqual(before_backup, backup.read_bytes())

    def test_failed_atomic_sidecar_write_preserves_old_state_and_cleans_owned_temps(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deckpipe-sidecar-fail-") as temporary:
            playlist = Path(temporary) / "playlist"
            playlist.mkdir()
            try:
                from app import atomic_io, library
            except ImportError:
                self.fail("app.atomic_io must provide injectable atomic replace behavior")

            original = {"tracks": {"keep": {"title": "Keep", "artist": "A", "file": "keep.flac", "status": "ok"}}}
            library.save_sidecar(playlist, original)
            sentinel = f"generated-secret-like-{uuid.uuid4().hex}"

            def fail_replace(_src: Path, _dst: Path) -> None:
                raise OSError("replace failed for " + sentinel)

            with patch.object(atomic_io, "replace_file", fail_replace):
                with self.assertRaises(Exception) as captured:
                    library.update_track_status(
                        playlist,
                        "new",
                        {"title": sentinel, "artist": "A", "file": "new.flac", "status": "ok"},
                    )

            formatted = "".join(
                traceback.format_exception(type(captured.exception), captured.exception, captured.exception.__traceback__)
            )
            self.assertNotIn(sentinel, formatted)
            self.assertEqual(original, _read_json(playlist / library.SIDECAR_NAME))
            self.assertFalse([p for p in playlist.iterdir() if ".deckpipe-stage-" in p.name or p.name.endswith(".part")])

    def test_partial_files_are_never_ok_even_when_sidecar_references_them(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deckpipe-sidecar-partial-") as temporary:
            playlist = Path(temporary) / "playlist"
            playlist.mkdir()
            from app import library

            partial = playlist / "Artist - Title.deckpipe-stage-abc.part.flac"
            partial.write_bytes(b"synthetic")
            library.save_sidecar(
                playlist,
                {"tracks": {"1": {"title": "Title 1", "artist": "Artist 1", "file": partial.name, "status": "ok"}}},
            )

            result = library.scan_playlist(playlist, [_track("1")])

            self.assertEqual("missing", result[0]["status"])
            self.assertEqual("", result[0]["file"])

    def test_update_track_status_merges_current_entry_and_preserves_convert_fields(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deckpipe-sidecar-merge-") as temporary:
            playlist = Path(temporary) / "playlist"
            playlist.mkdir()
            from app import library

            library.save_sidecar(
                playlist,
                {
                    "tracks": {
                        "1": {
                            "title": "Title",
                            "artist": "Artist",
                            "file": "source.flac",
                            "source_file": "source.flac",
                            "duration_actual": 179.8,
                            "position": 7,
                            "status": "verify_failed_convert",
                        }
                    }
                },
            )

            library.update_track_status(playlist, "1", {"status": "ok", "error": ""})
            saved = library.load_sidecar(playlist)["tracks"]["1"]

            self.assertEqual("source.flac", saved["source_file"])
            self.assertEqual(179.8, saved["duration_actual"])
            self.assertEqual(7, saved["position"])
            self.assertEqual("ok", saved["status"])

    def test_reserved_stage_detection_is_case_insensitive_and_final_path_uses_rightmost_marker(self) -> None:
        from app import atomic_io

        final = Path("Artist .deckpipe-stage- literal.part Song.flac")
        stage = atomic_io.make_staged_path(final)
        upper_stage = Path("Artist.DECKPIPE-STAGE-owned.PART.flac")

        self.assertTrue(atomic_io.is_partial_path("TRACK.PART.FLAC"))
        self.assertEqual(final, atomic_io.final_path_from_stage(stage))
        self.assertEqual(Path("Artist.flac"), atomic_io.final_path_from_stage(upper_stage))

    def test_first_atomic_backup_write_creates_primary_and_valid_backup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deckpipe-atomic-first-backup-") as temporary:
            from app import atomic_io, library

            path = Path(temporary) / ".deckpipe.json"
            payload = {"tracks": {"first": {"status": "ok", "file": "first.flac"}}}

            atomic_io.atomic_write_json(path, payload, validator=library._validate_sidecar, backup=True)

            self.assertEqual(payload, _read_json(path))
            self.assertEqual(payload, _read_json(atomic_io.backup_path(path)))

    def test_atomic_backup_snapshot_failure_preserves_old_primary_backup_and_cleans_temps(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deckpipe-atomic-backup-fail-") as temporary:
            from app import atomic_io, library

            playlist = Path(temporary) / "playlist"
            playlist.mkdir()
            path = playlist / library.SIDECAR_NAME
            old = {"tracks": {"old": {"status": "ok", "file": "old.flac"}}}
            backup = {"tracks": {"backup": {"status": "ok", "file": "backup.flac"}}}
            atomic_io.atomic_write_json(path, old, validator=library._validate_sidecar, backup=False)
            atomic_io.atomic_write_json(atomic_io.backup_path(path), backup, validator=library._validate_sidecar)

            def fail_backup(src: Path, dst: Path) -> None:
                if Path(dst).name.endswith(".bak"):
                    raise OSError("backup replacement failed generated-secret-" + uuid.uuid4().hex)
                os.replace(src, dst)

            with patch.object(atomic_io, "replace_file", fail_backup):
                with self.assertRaises(Exception):
                    atomic_io.atomic_write_json(
                        path,
                        {"tracks": {"new": {"status": "ok", "file": "new.flac"}}},
                        validator=library._validate_sidecar,
                        backup=True,
                    )

            self.assertEqual(old, _read_json(path))
            self.assertEqual(backup, _read_json(atomic_io.backup_path(path)))
            self.assertFalse([p for p in playlist.iterdir() if atomic_io.is_partial_path(p) or ".deckpipe-stage-" in p.name])

    def test_atomic_primary_publish_failure_preserves_prior_state_for_first_write_and_update(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deckpipe-atomic-primary-fail-") as temporary:
            from app import atomic_io, library

            root = Path(temporary)
            first = root / "first.json"

            def fail_primary_replace(src: Path, dst: Path) -> None:
                if Path(dst).name == "first.json":
                    raise OSError("primary replace failed generated-token-" + uuid.uuid4().hex)
                os.replace(src, dst)

            with patch.object(atomic_io, "replace_file", fail_primary_replace):
                with self.assertRaises(Exception):
                    atomic_io.atomic_write_json(
                        first,
                        {"tracks": {"new": {"status": "ok", "file": "new.flac"}}},
                        validator=library._validate_sidecar,
                        backup=True,
                    )

            self.assertFalse(first.exists())
            self.assertFalse(atomic_io.backup_path(first).exists())
            self.assertFalse([p for p in root.iterdir() if ".deckpipe-stage-" in p.name or atomic_io.is_partial_path(p)])

            path = root / "update.json"
            old = {"tracks": {"old": {"status": "ok", "file": "old.flac"}}}
            atomic_io.atomic_write_json(path, old, validator=library._validate_sidecar, backup=True)

            def fail_update_primary_replace(src: Path, dst: Path) -> None:
                if Path(dst).name == "update.json":
                    raise OSError("primary replace failed generated-token-" + uuid.uuid4().hex)
                os.replace(src, dst)

            with patch.object(atomic_io, "replace_file", fail_update_primary_replace):
                with self.assertRaises(Exception):
                    atomic_io.atomic_write_json(
                        path,
                        {"tracks": {"new": {"status": "ok", "file": "new.flac"}}},
                        validator=library._validate_sidecar,
                        backup=True,
                    )

            self.assertEqual(old, _read_json(path))
            self.assertEqual(old, _read_json(atomic_io.backup_path(path)))
            self.assertFalse([p for p in root.iterdir() if ".deckpipe-stage-" in p.name or atomic_io.is_partial_path(p)])

    def test_atomic_fsync_failure_preserves_prior_state_for_first_write_and_update(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deckpipe-atomic-fsync-fail-") as temporary:
            from app import atomic_io, library

            root = Path(temporary)
            payload = {"tracks": {"new": {"status": "ok", "file": "new.flac"}}}

            for fail_call, path_name in ((1, "first-backup-fsync.json"), (2, "first-primary-fsync.json")):
                path = root / path_name
                calls = {"count": 0}

                def fail_fsync(_fd: int) -> None:
                    calls["count"] += 1
                    if calls["count"] == fail_call:
                        raise OSError("fsync failed generated-token-" + uuid.uuid4().hex)
                    original_fsync(_fd)

                original_fsync = atomic_io.os.fsync
                with patch.object(atomic_io.os, "fsync", fail_fsync):
                    with self.assertRaises(Exception):
                        atomic_io.atomic_write_json(path, payload, validator=library._validate_sidecar, backup=True)

                self.assertFalse(path.exists())
                self.assertFalse(atomic_io.backup_path(path).exists())

            path = root / "update-fsync.json"
            old = {"tracks": {"old": {"status": "ok", "file": "old.flac"}}}
            atomic_io.atomic_write_json(path, old, validator=library._validate_sidecar, backup=True)
            for fail_call in (1, 2):
                calls = {"count": 0}

                def fail_fsync(_fd: int) -> None:
                    calls["count"] += 1
                    if calls["count"] == fail_call:
                        raise OSError("fsync failed generated-token-" + uuid.uuid4().hex)
                    original_fsync(_fd)

                original_fsync = atomic_io.os.fsync
                with patch.object(atomic_io.os, "fsync", fail_fsync):
                    with self.assertRaises(Exception):
                        atomic_io.atomic_write_json(path, payload, validator=library._validate_sidecar, backup=True)

                self.assertEqual(old, _read_json(path))
                self.assertEqual(old, _read_json(atomic_io.backup_path(path)))
            self.assertFalse([p for p in root.iterdir() if ".deckpipe-stage-" in p.name or atomic_io.is_partial_path(p)])


class StagedPublicationTests(unittest.TestCase):
    def test_soundcloud_download_uses_unique_same_directory_partial_template_with_real_suffix(self) -> None:
        captured: list[dict[str, object]] = []

        class FakeYoutubeDL:
            def __init__(self, opts):
                captured.append(opts)

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def extract_info(self, _url, download=False):
                self.info = {"duration": 180, "ext": "mp3", "id": "sc-1"}
                path = Path(captured[-1]["outtmpl"].replace("%(ext)s", "mp3"))
                path.write_bytes(b"synthetic staged media")
                return self.info

            def prepare_filename(self, info):
                return captured[-1]["outtmpl"].replace("%(ext)s", info["ext"])

        with tempfile.TemporaryDirectory(prefix="deckpipe-soundcloud-stage-") as temporary:
            from app import soundcloud

            out_dir = Path(temporary)
            track = {**_track("sc-1", "sc"), "url": "https://soundcloud.example/track"}
            with patch.object(soundcloud, "sc_oauth_token", return_value=None):
                with patch.object(soundcloud.yt_dlp, "YoutubeDL", FakeYoutubeDL):
                    first, _, _, _ = soundcloud.download_track(track, out_dir)
                    second, _, _, _ = soundcloud.download_track(track, out_dir)

            self.assertEqual(out_dir, first.parent)
            self.assertEqual(out_dir, second.parent)
            self.assertNotEqual(first.name, second.name)
            self.assertIn(".deckpipe-stage-", first.name)
            self.assertIn(".part.", first.name)
            self.assertEqual(".mp3", first.suffix)
            self.assertFalse((out_dir / "Artist sc-1 - Title sc-1.mp3").exists())

    def test_failed_staged_validation_does_not_overwrite_final_or_store_partial_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deckpipe-publish-fail-") as temporary:
            from app import jobs, library

            playlist = Path(temporary) / "playlist"
            playlist.mkdir()
            final = playlist / "001 - Artist 1 - Title 1.flac"
            final.write_bytes(b"old valid final")
            stage = playlist / "Artist 1 - Title 1.deckpipe-stage-owned.part.flac"

            class FakeSession:
                def download_track(self, _track_id, out_dir, prefer="FLAC"):
                    self.calls = getattr(self, "calls", 0) + 1
                    stage.write_bytes(b"bad staged media")
                    return stage, "FLAC", {"DURATION": "180", "SNG_TITLE": "Title 1", "ART_NAME": "Artist 1"}

            job = {"mode": "append", "results": []}
            counter = {"base": 0, "n": 0, "digits": 3}
            with (
                patch.object(jobs, "get_session", return_value=FakeSession()),
                patch.object(jobs, "verify_file", return_value=(False, "synthetic validator failed", 0.0)),
                patch.object(jobs, "_numbering_on", return_value=True),
            ):
                ok, err, _quality = jobs._process_track(job, playlist, _track("1"), {"ds": None}, counter)

            self.assertFalse(ok)
            self.assertEqual("media validation failed", err)
            self.assertEqual(b"old valid final", final.read_bytes())
            self.assertFalse(stage.exists())
            sidecar = library.load_sidecar(playlist)
            stored_file = sidecar["tracks"]["1"].get("file", "")
            self.assertNotIn(".part", stored_file)
            self.assertNotIn(".deckpipe-stage-", stored_file)

    def test_tag_failure_blocks_publication_and_sanitizes_state_and_traceback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deckpipe-tag-fail-") as temporary:
            from app import jobs, library

            playlist = Path(temporary) / "playlist"
            playlist.mkdir()
            final = playlist / "001 - Artist 1 - Title 1.flac"
            final.write_bytes(b"old valid final")
            stage = playlist / "Artist 1 - Title 1.deckpipe-stage-owned.part.flac"
            secretish = "generated-token-" + uuid.uuid4().hex

            class FakeSession:
                def download_track(self, _track_id, out_dir, prefer="FLAC"):
                    stage.write_bytes(b"good staged media")
                    return stage, "FLAC", {"DURATION": "180", "SNG_TITLE": "Title 1", "ART_NAME": "Artist 1"}

            def raising_tags(_path: Path, _meta: dict) -> None:
                raise RuntimeError(f"tag failed {stage.name} {secretish}")

            job = {"mode": "append", "results": []}
            counter = {"base": 0, "n": 0, "digits": 3}
            with (
                patch.object(jobs, "get_session", return_value=FakeSession()),
                patch.object(jobs, "verify_file", return_value=(True, "", 180.0)),
                patch.object(jobs, "_numbering_on", return_value=True),
                patch("app.tagger.write_tags", raising_tags),
            ):
                ok, err, _quality = jobs._process_track(job, playlist, _track("1"), {"ds": None}, counter)

            self.assertFalse(ok)
            self.assertEqual(b"old valid final", final.read_bytes())
            self.assertFalse(stage.exists())
            sidecar = library.load_sidecar(playlist)
            entry = sidecar["tracks"]["1"]
            combined = json.dumps(entry, ensure_ascii=False) + err
            self.assertEqual("verify_failed_metadata", entry["status"])
            self.assertNotIn(stage.name, combined)
            self.assertNotIn(secretish, combined)
            self.assertNotIn(".deckpipe-stage-", combined)

    def test_tagger_log_records_generic_metadata_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deckpipe-tagger-log-") as temporary:
            from app import tagger

            stage = Path(temporary) / ("Track.deckpipe-stage-" + uuid.uuid4().hex + ".part.flac")
            stage.write_bytes(b"synthetic")
            secretish = "generated-token-" + uuid.uuid4().hex

            def raising_tag(_path: Path, _meta: dict) -> None:
                raise RuntimeError("mutagen failed " + str(stage) + " " + secretish)

            with patch.object(tagger, "_tag_flac", raising_tag):
                with self.assertLogs("app.tagger", level="WARNING") as captured:
                    with self.assertRaises(tagger.TagWriteError):
                        tagger.write_tags(stage, {"title": "T", "artist": "A", "album": "", "album_artist": "", "isrc": "", "date": "", "track_number": "", "cover": None})

            logs = "\n".join(captured.output)
            self.assertIn("tag write failed", logs)
            self.assertNotIn(str(stage), logs)
            self.assertNotIn(stage.name, logs)
            self.assertNotIn(secretish, logs)
            self.assertNotIn(".deckpipe-stage-", logs)

    def test_soundcloud_fallback_ignores_stale_stage_and_cleans_current_partial_on_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deckpipe-sc-stale-") as temporary:
            from app import soundcloud

            out_dir = Path(temporary)
            stale = out_dir / "Artist sc-1 - Title sc-1.deckpipe-stage-stale.part.mp3"
            stale.write_bytes(b"stale")
            captured: list[str] = []

            class FakeYoutubeDL:
                def __init__(self, opts):
                    captured.append(opts["outtmpl"])

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def extract_info(self, _url, download=False):
                    current = Path(captured[-1].replace("%(ext)s", "mp3"))
                    current.write_bytes(b"current partial")
                    raise RuntimeError("yt-dlp signed-url " + current.name)

            with patch.object(soundcloud, "sc_oauth_token", return_value=None):
                with patch.object(soundcloud.yt_dlp, "YoutubeDL", FakeYoutubeDL):
                    with self.assertRaises(RuntimeError):
                        soundcloud.download_track({**_track("sc-1", "sc"), "url": "https://soundcloud.example/t"}, out_dir)

            self.assertTrue(stale.exists())
            self.assertEqual([stale], list(out_dir.iterdir()))

    def test_soundcloud_fallback_uses_literal_current_prefix_and_rejects_stale_prepared_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deckpipe-sc-literal-prefix-") as temporary:
            from app import soundcloud

            out_dir = Path(temporary)
            stale = out_dir / "Artist - Brackets [demo] _.deckpipe-stage-stale.part.mp3"
            stale.write_bytes(b"stale")
            captured: list[str] = []

            class FakeYoutubeDL:
                def __init__(self, opts):
                    captured.append(opts["outtmpl"])

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def extract_info(self, _url, download=False):
                    self.info = {"duration": 180, "ext": "mp3", "id": "sc-1"}
                    current = Path(captured[-1].replace("%(ext)s", "mp3"))
                    current.write_bytes(b"current")
                    return self.info

                def prepare_filename(self, _info):
                    return str(stale)

            with patch.object(soundcloud, "sc_oauth_token", return_value=None):
                with patch.object(soundcloud.yt_dlp, "YoutubeDL", FakeYoutubeDL):
                    fpath, _fmt, _duration, _info = soundcloud.download_track(
                        {**_track("sc-1", "sc"), "title": "Brackets [demo] *", "url": "https://soundcloud.example/t"},
                        out_dir,
                    )

            self.assertNotEqual(stale, fpath)
            self.assertTrue(stale.exists())
            self.assertTrue(fpath.exists())
            self.assertIn(".deckpipe-stage-", fpath.name)


class DurableJobJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="deckpipe-jobs-")
        self.data_root = Path(self.tmp.name) / "data"
        import app.jobs as jobs

        self.jobs = importlib.reload(jobs)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_init_requires_explicit_data_root_and_list_get_return_snapshots(self) -> None:
        jobs = self.jobs
        self.assertTrue(hasattr(jobs, "initialize"), "jobs.initialize must explicitly bind a data root")
        with self.assertRaises(RuntimeError):
            jobs.initialize()
        jobs.initialize(self.data_root, start_worker=False)

        job_id = jobs.enqueue("playlist", "Playlist", [_track("1")], mode="append", start_worker=False)
        listed = jobs.list_jobs()
        got = jobs.get_job(job_id)
        listed[0]["state"] = "mutated"
        got["results"].append({"id": "bad"})

        self.assertEqual("queued", jobs.get_job(job_id)["state"])
        self.assertEqual([], jobs.get_job(job_id)["results"])

    def test_enqueue_canonicalizes_tracks_and_never_persists_or_snapshots_unknown_fields(self) -> None:
        jobs = self.jobs
        jobs.initialize(self.data_root, start_worker=False)
        dirty_track = {
            **_track("1"),
            "credential": "generated-token-" + uuid.uuid4().hex,
            "nested": {"oauth_token": "generated-token-" + uuid.uuid4().hex},
            "duration": 180.5,
            "position": 4,
            "total": 9,
        }

        job_id = jobs.enqueue("playlist", "Playlist", [dirty_track], mode="append", start_worker=False)

        for snapshot in (jobs.get_job(job_id), jobs.list_jobs()[0]):
            stored_track = snapshot["tracks"][0]
            self.assertEqual(
                {
                    "id": "1",
                    "title": "Title 1",
                    "artist": "Artist 1",
                    "album": "Synthetic",
                    "duration": 180.5,
                    "provider": "deezer",
                    "url": "https://example.invalid/1",
                    "position": 4,
                    "total": 9,
                },
                stored_track,
            )
            self.assertNotIn("credential", json.dumps(snapshot, ensure_ascii=False))
            self.assertNotIn("oauth_token", json.dumps(snapshot, ensure_ascii=False))

        for path in (self.data_root / "jobs.json", self.data_root / "jobs.json.bak"):
            serialized = path.read_text(encoding="utf-8")
            self.assertNotIn("credential", serialized)
            self.assertNotIn("oauth_token", serialized)
            self.assertEqual(["provider", "id", "title", "artist", "album", "url", "duration", "position", "total"],
                             list(_read_json(path)["jobs"][job_id]["tracks"][0].keys()))

    def test_journal_recovers_backup_requeues_only_unfinished_idempotent_jobs_and_interrupts_flip(self) -> None:
        jobs = self.jobs
        self.assertTrue(hasattr(jobs, "initialize"), "jobs.initialize must explicitly bind a data root")
        jobs.initialize(self.data_root, start_worker=False)
        download_id = jobs.enqueue("playlist", "Playlist", [_track("1"), _track("2")], mode="append", start_worker=False)
        flip_id = jobs.enqueue_flip("playlist", "Playlist", to_wav=True, start_worker=False)
        jobs.mark_running(download_id)
        jobs.mark_item_complete(download_id, _track("1"), ok=True, error="", quality="flac")

        journal = self.data_root / "jobs.json"
        backup = self.data_root / "jobs.json.bak"
        before_backup = backup.read_bytes()
        journal.write_text('{"jobs": {"broken": ', encoding="utf-8")

        reloaded = importlib.reload(jobs)
        reloaded.initialize(self.data_root, start_worker=False)
        download = reloaded.get_job(download_id)
        flip = reloaded.get_job(flip_id)

        self.assertEqual(before_backup, backup.read_bytes())
        self.assertEqual("queued", download["state"])
        self.assertEqual("pending", download["outcome"])
        self.assertEqual([], download["results"])
        self.assertEqual(["1", "2"], [item["id"] for item in reloaded.pending_track_ids(download_id)])
        self.assertEqual("done", flip["state"])
        self.assertEqual("interrupted", flip["outcome"])
        self.assertIn("restart", flip["terminal_error"]["code"])

    def test_initialize_rejects_same_root_worker_after_queue_pop_before_running_without_mutating_state(self) -> None:
        jobs = self.jobs
        before_running = threading.Event()
        release_running = threading.Event()
        calls: list[str] = []
        calls_lock = threading.Lock()
        original_mark_running = jobs.mark_running

        def blocked_mark_running(job_id: str, **kwargs) -> None:
            before_running.set()
            self.assertTrue(release_running.wait(1))
            original_mark_running(job_id, **kwargs)

        def fake_process(_job, _pl_dir, track, *_args):
            with calls_lock:
                calls.append(f"{track.get('provider', 'deezer')}:{track['id']}")
            return True, "", "flac"

        jobs.initialize(self.data_root, start_worker=False)
        with (
            patch.object(jobs, "mark_running", side_effect=blocked_mark_running),
            patch.object(jobs, "playlist_dir", return_value=Path(self.tmp.name) / "playlist"),
            patch.object(jobs, "_process_track", side_effect=fake_process),
        ):
            job_id = jobs.enqueue("playlist", "Playlist", [_track("1")], mode="append", start_worker=True)
            self.assertTrue(before_running.wait(1))
            before_generation = jobs._worker_generation
            before_worker = jobs._worker_thread
            before_journal = jobs._journal_path
            before_queue = list(jobs._queue)
            before_job = jobs.get_job(job_id)

            with self.assertRaises(RuntimeError):
                jobs.initialize(self.data_root, start_worker=True)

            self.assertEqual(before_generation, jobs._worker_generation)
            self.assertIs(before_worker, jobs._worker_thread)
            self.assertEqual(before_journal, jobs._journal_path)
            self.assertEqual(before_queue, jobs._queue)
            self.assertEqual(before_job, jobs.get_job(job_id))

            release_running.set()
            deadline = time.time() + 1
            while time.time() < deadline and jobs.get_job(job_id)["state"] != "done":
                threading.Event().wait(0.01)

        self.assertEqual(["deezer:1"], calls)
        job = jobs.get_job(job_id)
        self.assertEqual("done", job["state"])
        self.assertEqual("succeeded", job["outcome"])
        self.assertEqual(1, job["done"])
        self.assertEqual([], jobs._queue)

    def test_initialize_cleans_dirty_completed_job_tracks_in_primary_and_backup(self) -> None:
        jobs = self.jobs
        marker = "generated-token-" + uuid.uuid4().hex
        jobs.initialize(self.data_root, start_worker=False)
        job_id = jobs.enqueue("playlist", "Playlist", [_track("1")], mode="append", start_worker=False)
        jobs.mark_running(job_id)
        jobs.mark_item_complete(job_id, _track("1"), ok=True, error="", quality="flac")
        jobs.mark_terminal(job_id, outcome="succeeded")
        journal = self.data_root / "jobs.json"
        backup = self.data_root / "jobs.json.bak"

        dirty = _read_json(journal)
        dirty["jobs"][job_id]["tracks"][0]["credential"] = marker
        dirty["jobs"][job_id]["tracks"][0]["nested"] = {"oauth_token": marker}
        serialized = json.dumps(dirty, ensure_ascii=False, indent=2) + "\n"
        journal.write_text(serialized, encoding="utf-8")
        backup.write_text(serialized, encoding="utf-8")

        reloaded = importlib.reload(jobs)
        reloaded.initialize(self.data_root, start_worker=False)

        for snapshot in (reloaded.get_job(job_id), reloaded.list_jobs()[0]):
            encoded = json.dumps(snapshot, ensure_ascii=False)
            self.assertNotIn(marker, encoded)
            self.assertNotIn("oauth_token", encoded)
            self.assertNotIn("credential", encoded)

        for path in (journal, backup):
            raw = _read_json(path)
            self.assertEqual(raw, reloaded._validate_journal(raw))
            serialized = path.read_text(encoding="utf-8")
            self.assertNotIn(marker, serialized)
            self.assertNotIn("oauth_token", serialized)
            self.assertNotIn("credential", serialized)

    def test_job_journal_persists_once_per_logical_transition_and_keeps_terminal_error(self) -> None:
        jobs = self.jobs
        self.assertTrue(hasattr(jobs, "initialize"), "jobs.initialize must explicitly bind a data root")
        writes: list[Path] = []

        def recorder(path: Path, payload: object, **kwargs) -> None:
            writes.append(Path(path))
            jobs._real_atomic_write_json(path, payload, **kwargs)

        jobs.initialize(self.data_root, start_worker=False, write_json=recorder)
        job_id = jobs.enqueue("playlist", "Playlist", [_track("1")], mode="append", start_worker=False)
        jobs.mark_running(job_id)
        jobs.mark_item_complete(job_id, _track("1"), ok=False, error="synthetic failed", quality="")
        jobs.mark_item_complete(job_id, _track("1"), ok=False, error="duplicate", quality="")
        jobs.mark_terminal(job_id, outcome="failed", error={"code": "synthetic", "message": "synthetic failed"})
        with self.assertRaises(RuntimeError):
            jobs.mark_terminal(job_id, outcome="succeeded")

        self.assertEqual(4, len(writes))
        reloaded = importlib.reload(jobs)
        reloaded.initialize(self.data_root, start_worker=False)
        job = reloaded.get_job(job_id)
        self.assertEqual("done", job["state"])
        self.assertEqual("failed", job["outcome"])
        self.assertEqual({"code": "synthetic", "message": "synthetic failed"}, job["terminal_error"])

    def test_enqueue_write_failure_does_not_publish_memory_queue_or_start_worker(self) -> None:
        jobs = self.jobs
        jobs.initialize(self.data_root, start_worker=False)
        journal = self.data_root / "jobs.json"
        before_disk = _read_json(journal) if journal.exists() else None
        starts: list[str] = []

        def fail_write(_path: Path, _payload: object, **_kwargs) -> None:
            raise RuntimeError("journal write failed generated-token-" + uuid.uuid4().hex)

        jobs._write_json = fail_write
        with patch.object(jobs, "_ensure_worker_locked", side_effect=lambda: starts.append("started")):
            with self.assertRaises(Exception):
                jobs.enqueue("playlist", "Playlist", [_track("1")], mode="append", start_worker=True)

        self.assertEqual([], starts)
        self.assertEqual([], jobs.list_jobs())
        self.assertEqual([], jobs._queue)
        self.assertEqual(before_disk, _read_json(journal) if journal.exists() else None)

    def test_progress_and_terminal_write_failures_keep_memory_and_disk_unchanged(self) -> None:
        jobs = self.jobs
        jobs.initialize(self.data_root, start_worker=False)
        job_id = jobs.enqueue("playlist", "Playlist", [_track("1")], mode="append", start_worker=False)
        jobs.mark_running(job_id)
        before_progress = jobs.get_job(job_id)
        before_disk = _read_json(self.data_root / "jobs.json")

        def fail_write(_path: Path, _payload: object, **_kwargs) -> None:
            raise RuntimeError("journal write failed generated-token-" + uuid.uuid4().hex)

        jobs._write_json = fail_write
        with self.assertRaises(Exception):
            jobs.mark_item_complete(job_id, _track("1"), ok=False, error="raw " + str(self.data_root), quality="")
        self.assertEqual(before_progress, jobs.get_job(job_id))
        self.assertEqual(before_disk, _read_json(self.data_root / "jobs.json"))

        jobs._write_json = jobs._real_atomic_write_json
        jobs.mark_item_complete(job_id, _track("1"), ok=False, error="synthetic failed", quality="")
        before_terminal = jobs.get_job(job_id)
        before_disk = _read_json(self.data_root / "jobs.json")
        jobs._write_json = fail_write
        with self.assertRaises(Exception):
            jobs.mark_terminal(job_id, outcome="failed", error={"code": "failed", "message": str(self.data_root)})
        self.assertEqual(before_terminal, jobs.get_job(job_id))
        self.assertEqual(before_disk, _read_json(self.data_root / "jobs.json"))

    def test_invalid_terminal_outcome_and_duplicate_progress_do_not_write(self) -> None:
        jobs = self.jobs
        writes: list[object] = []

        def recorder(path: Path, payload: object, **kwargs) -> None:
            writes.append(json.loads(json.dumps(payload)))
            jobs._real_atomic_write_json(path, payload, **kwargs)

        jobs.initialize(self.data_root, start_worker=False, write_json=recorder)
        job_id = jobs.enqueue("playlist", "Playlist", [_track("1")], mode="append", start_worker=False)
        jobs.mark_running(job_id)
        jobs.mark_item_complete(job_id, _track("1"), ok=True, error="", quality="flac")
        count_after_first_progress = len(writes)
        before_duplicate = jobs.get_job(job_id)
        jobs.mark_item_complete(job_id, _track("1"), ok=False, error="duplicate", quality="")
        self.assertEqual(count_after_first_progress, len(writes))
        self.assertEqual(before_duplicate, jobs.get_job(job_id))

        before_invalid = jobs.get_job(job_id)
        with self.assertRaises(Exception):
            jobs.mark_terminal(job_id, outcome="nonsense")
        self.assertEqual(before_invalid, jobs.get_job(job_id))
        self.assertEqual(count_after_first_progress, len(writes))

    def test_start_worker_false_reinitialize_stops_stale_worker_from_consuming_new_queue(self) -> None:
        jobs = self.jobs
        old_root = self.data_root / "old"
        new_root = self.data_root / "new"
        calls: list[str] = []
        sleeper_ready = threading.Event()
        release_sleep = threading.Event()

        def fake_sleep(_seconds: float) -> None:
            sleeper_ready.set()
            release_sleep.wait(0.01)

        def fake_process(*_args, **_kwargs):
            calls.append("processed")
            return True, "", "flac"

        jobs.initialize(old_root, start_worker=False)
        with (
            patch.object(jobs.time, "sleep", fake_sleep),
            patch.object(jobs, "playlist_dir", return_value=Path(self.tmp.name) / "playlist"),
            patch.object(jobs, "_process_track", side_effect=fake_process),
        ):
            with jobs._lock:
                jobs._ensure_worker_locked()
            self.assertTrue(sleeper_ready.wait(1))
            jobs.initialize(new_root, start_worker=False)
            job_id = jobs.enqueue("playlist", "Playlist", [_track("1")], mode="append", start_worker=False)
            release_sleep.set()
            threading.Event().wait(0.05)

        self.assertEqual([], calls)
        self.assertEqual("queued", jobs.get_job(job_id)["state"])
        self.assertEqual([job_id], jobs._queue)

    def test_reinitialize_does_not_let_old_inflight_worker_mutate_new_journal(self) -> None:
        jobs = self.jobs
        old_root = self.data_root / "old-inflight"
        new_root = self.data_root / "new-inflight"
        entered = threading.Event()
        release = threading.Event()

        def fake_process(*_args, **_kwargs):
            entered.set()
            release.wait(1)
            return True, "", "flac"

        jobs.initialize(old_root, start_worker=False)
        with (
            patch.object(jobs, "playlist_dir", return_value=Path(self.tmp.name) / "playlist"),
            patch.object(jobs, "_process_track", side_effect=fake_process),
        ):
            old_id = jobs.enqueue("playlist", "Playlist", [_track("1")], mode="append", start_worker=True)
            self.assertTrue(entered.wait(1))
            jobs.initialize(new_root, start_worker=False)
            new_id = jobs.enqueue("playlist", "Playlist", [_track("2")], mode="append", start_worker=False)
            release.set()
            threading.Event().wait(0.05)

        self.assertIsNone(jobs.get_job(old_id))
        self.assertEqual("queued", jobs.get_job(new_id)["state"])
        self.assertEqual([], jobs.get_job(new_id)["results"])

    def test_same_root_reinitialize_during_inflight_processing_does_not_duplicate_or_restart_item(self) -> None:
        jobs = self.jobs
        entered = threading.Event()
        release = threading.Event()
        calls: list[str] = []
        calls_lock = threading.Lock()

        def fake_process(_job, _pl_dir, track, *_args):
            with calls_lock:
                calls.append(f"{track.get('provider', 'deezer')}:{track['id']}")
            entered.set()
            release.wait(1)
            return True, "", "flac"

        jobs.initialize(self.data_root, start_worker=False)
        with (
            patch.object(jobs, "playlist_dir", return_value=Path(self.tmp.name) / "playlist"),
            patch.object(jobs, "_process_track", side_effect=fake_process),
        ):
            job_id = jobs.enqueue("playlist", "Playlist", [_track("1")], mode="append", start_worker=True)
            self.assertTrue(entered.wait(1))
            try:
                jobs.initialize(self.data_root, start_worker=True)
            except RuntimeError:
                pass
            threading.Event().wait(0.05)
            release.set()
            deadline = time.time() + 1
            while time.time() < deadline and jobs.get_job(job_id)["state"] != "done":
                threading.Event().wait(0.01)

        self.assertEqual(["deezer:1"], calls)
        job = jobs.get_job(job_id)
        self.assertIsNotNone(job)
        self.assertEqual("done", job["state"])
        self.assertEqual("succeeded", job["outcome"])
        self.assertEqual(1, job["total"])
        self.assertEqual(1, job["done"])
        self.assertEqual(["deezer:1"], [item["key"] for item in job["results"]])

    def test_worker_exception_persists_only_public_error_text(self) -> None:
        jobs = self.jobs
        jobs.initialize(self.data_root, start_worker=False)
        with tempfile.TemporaryDirectory(prefix="deckpipe-worker-sanitize-") as temporary:
            playlist = Path(temporary) / "playlist"
            playlist.mkdir()
            secretish = "generated-token-" + uuid.uuid4().hex
            stage_name = "Track.deckpipe-stage-owned.part.flac"
            processed = threading.Event()

            def raising_process(*_args, **_kwargs):
                processed.set()
                raise RuntimeError("raw failure " + str(playlist / stage_name) + " " + secretish)

            with (
                patch.object(jobs, "playlist_dir", return_value=playlist),
                patch.object(jobs, "_process_track", side_effect=raising_process),
            ):
                job_id = jobs.enqueue("playlist", "Playlist", [_track("1")], mode="append", start_worker=True)
                self.assertTrue(processed.wait(1))
                deadline = time.time() + 1
                while time.time() < deadline and jobs.get_job(job_id)["state"] != "done":
                    threading.Event().wait(0.01)

            job = jobs.get_job(job_id)
            persisted = json.dumps(_read_json(self.data_root / "jobs.json"), ensure_ascii=False)
            combined = json.dumps(job, ensure_ascii=False) + persisted
            self.assertEqual("done", job["state"])
            self.assertNotIn(str(playlist), combined)
            self.assertNotIn(stage_name, combined)
            self.assertNotIn(secretish, combined)
            self.assertNotIn(".deckpipe-stage-", combined)

    def test_progress_requires_running_and_provider_qualified_results_do_not_collide(self) -> None:
        jobs = self.jobs
        jobs.initialize(self.data_root, start_worker=False)
        job_id = jobs.enqueue("playlist", "Playlist", [_track("1", "deezer"), _track("1", "sc")],
                              mode="append", start_worker=False)
        with self.assertRaises(RuntimeError):
            jobs.mark_item_complete(job_id, _track("1", "deezer"), ok=True, error="", quality="flac")

        jobs.mark_running(job_id)
        jobs.mark_item_complete(job_id, _track("1", "deezer"), ok=True, error="", quality="flac")
        pending = jobs.pending_track_ids(job_id)
        job = jobs.get_job(job_id)

        self.assertEqual(["sc"], [item["provider"] for item in pending])
        self.assertEqual("deezer:1", job["results"][0]["key"])
        self.assertEqual("deezer", job["results"][0]["provider"])

    def test_duplicate_same_provider_tracks_process_once_and_same_raw_id_across_providers_stays_distinct(self) -> None:
        jobs = self.jobs
        calls: list[str] = []

        def fake_process(_job, _pl_dir, track, *_args):
            calls.append(f"{track.get('provider', 'deezer')}:{track['id']}")
            return True, "", "flac"

        jobs.initialize(self.data_root, start_worker=False)
        with (
            patch.object(jobs, "playlist_dir", return_value=Path(self.tmp.name) / "playlist"),
            patch.object(jobs, "_process_track", side_effect=fake_process),
        ):
            job_id = jobs.enqueue(
                "playlist",
                "Playlist",
                [_track("1", "deezer"), _track("1", "deezer"), _track("1", "sc")],
                mode="append",
                start_worker=True,
            )
            deadline = time.time() + 1
            while time.time() < deadline and jobs.get_job(job_id)["state"] != "done":
                threading.Event().wait(0.01)

        job = jobs.get_job(job_id)
        self.assertEqual(["deezer:1", "sc:1"], calls)
        self.assertEqual("done", job["state"])
        self.assertEqual("succeeded", job["outcome"])
        self.assertEqual(2, job["total"])
        self.assertEqual(2, job["done"])
        self.assertEqual(0, job["failed"])
        self.assertEqual(["deezer:1", "sc:1"], [item["key"] for item in job["results"]])

    def test_restart_reconciles_ready_sidecar_without_calling_downloader(self) -> None:
        jobs = self.jobs
        jobs.initialize(self.data_root, start_worker=False)
        with tempfile.TemporaryDirectory(prefix="deckpipe-reconcile-") as temporary:
            from app import library

            playlist = Path(temporary) / "playlist"
            playlist.mkdir()
            ready_file = playlist / "Artist 1 - Title 1.flac"
            ready_file.write_bytes(b"ready")
            library.save_sidecar(
                playlist,
                {"tracks": {"1": {"status": "ok", "file": ready_file.name, "provider": "deezer", "title": "Title 1"}}},
            )
            with patch.object(jobs, "playlist_dir", return_value=playlist):
                job_id = jobs.enqueue("playlist", "Playlist", [_track("1")], mode="append", start_worker=False)
                reloaded = importlib.reload(jobs)
                with (
                    patch.object(reloaded, "playlist_dir", return_value=playlist),
                    patch.object(reloaded, "_process_track", side_effect=AssertionError("downloader must not run")),
                ):
                    reloaded.initialize(self.data_root, start_worker=False)

            job = reloaded.get_job(job_id)
            self.assertEqual("done", job["state"])
            self.assertEqual("succeeded", job["outcome"])
            self.assertEqual("reconciled", job["results"][0]["quality"])
            self.assertEqual([], reloaded.pending_track_ids(job_id))

    def test_restart_reconciles_duplicate_provider_once_and_same_raw_id_per_provider(self) -> None:
        jobs = self.jobs
        jobs.initialize(self.data_root, start_worker=False)
        with tempfile.TemporaryDirectory(prefix="deckpipe-reconcile-dedupe-") as temporary:
            from app import library

            playlist = Path(temporary) / "playlist"
            playlist.mkdir()
            dz = playlist / "Deezer Artist - Same.flac"
            sc = playlist / "SC Artist - Same.mp3"
            dz.write_bytes(b"ready")
            sc.write_bytes(b"ready")
            library.save_sidecar(
                playlist,
                {
                    "tracks": {
                        "1": {"status": "ok", "file": dz.name, "provider": "deezer", "title": "Same"},
                        "sc:1": {"status": "ok", "file": sc.name, "provider": "sc", "title": "Same"},
                    }
                },
            )
            tracks = [_track("1", "deezer"), _track("1", "deezer"), _track("1", "sc")]
            with patch.object(jobs, "playlist_dir", return_value=playlist):
                job_id = jobs.enqueue("playlist", "Playlist", tracks, mode="append", start_worker=False)
                reloaded = importlib.reload(jobs)
                with patch.object(reloaded, "playlist_dir", return_value=playlist):
                    reloaded.initialize(self.data_root, start_worker=False)

            job = reloaded.get_job(job_id)
            self.assertEqual("done", job["state"])
            self.assertEqual(2, job["done"])
            self.assertEqual(2, job["total"])
            self.assertEqual("succeeded", job["outcome"])
            self.assertEqual(["deezer:1", "sc:1"], [item["key"] for item in job["results"]])

    def test_malformed_nested_journal_recovers_valid_backup(self) -> None:
        jobs = self.jobs
        jobs.initialize(self.data_root, start_worker=False)
        valid_id = jobs.enqueue("playlist", "Playlist", [_track("1")], mode="append", start_worker=False)
        journal = self.data_root / "jobs.json"
        backup = self.data_root / "jobs.json.bak"
        valid_backup = backup.read_bytes()
        malformed_cases = [
            {"version": 1, "jobs": {"bad": {"id": "bad", "state": "queued", "outcome": "pending", "tracks": [], "results": []}}},
            {"version": 1, "jobs": {"bad": {"id": "bad", "playlist_id": "p", "title": "t", "state": "queued", "outcome": "pending", "mode": "append", "created_at": 1, "total": 1, "done": 2, "failed": 0, "tracks": [], "results": [], "current": None, "terminal_error": None}}},
            {"version": 1, "jobs": {"bad": {"id": "bad", "playlist_id": "p", "title": "t", "state": "done", "outcome": "pending", "mode": "append", "created_at": 1, "total": 1, "done": 0, "failed": 0, "tracks": [], "results": [], "current": None, "terminal_error": None}}},
            {"version": 1, "jobs": {"bad": {"id": "bad", "playlist_id": "p", "title": "t", "state": "queued", "outcome": "pending", "mode": "append", "created_at": 1, "total": 1, "done": 0, "failed": 0, "tracks": [{"id": "", "title": "t"}], "results": [], "current": None, "terminal_error": None}}},
            {
                "version": 1,
                "jobs": {
                    "bad": {
                        "id": "bad",
                        "playlist_id": "p",
                        "title": "t",
                        "state": "done",
                        "outcome": "failed",
                        "mode": "append",
                        "created_at": 1,
                        "total": 1,
                        "done": 1,
                        "failed": 1,
                        "tracks": [{"id": "1", "title": "t", "provider": "deezer"}],
                        "results": [
                            {
                                "id": "1",
                                "provider": "bad/provider",
                                "key": "1",
                                "title": "t",
                                "ok": False,
                                "error": "",
                                "quality": "",
                            }
                        ],
                        "current": None,
                        "terminal_error": {"code": "failed"},
                    }
                },
            },
        ]

        for payload in malformed_cases:
            journal.write_text(json.dumps(payload), encoding="utf-8")
            reloaded = importlib.reload(jobs)
            reloaded.initialize(self.data_root, start_worker=False)
            self.assertIsNotNone(reloaded.get_job(valid_id))
            self.assertEqual(valid_backup, backup.read_bytes())

    def test_incoherent_journal_primary_recovers_backup_without_replacing_backup(self) -> None:
        jobs = self.jobs
        jobs.initialize(self.data_root, start_worker=False)
        valid_id = jobs.enqueue("playlist", "Playlist", [_track("1")], mode="append", start_worker=False)
        journal = self.data_root / "jobs.json"
        backup = self.data_root / "jobs.json.bak"
        valid_backup = backup.read_bytes()
        incoherent = {
            "version": 1,
            "jobs": {
                "bad": {
                    "id": "bad",
                    "playlist_id": "p",
                    "title": "t",
                    "state": "queued",
                    "outcome": "pending",
                    "mode": "append",
                    "created_at": 1,
                    "total": 1,
                    "done": 1,
                    "failed": 0,
                    "tracks": [{"id": "1", "title": "t", "provider": "deezer"}],
                    "results": [],
                    "current": None,
                    "terminal_error": None,
                }
            },
        }
        journal.write_text(json.dumps(incoherent), encoding="utf-8")

        reloaded = importlib.reload(jobs)
        reloaded.initialize(self.data_root, start_worker=False)

        self.assertIsNotNone(reloaded.get_job(valid_id))
        self.assertEqual(valid_backup, backup.read_bytes())

    def test_strict_journal_schema_rejects_result_key_and_count_incoherence(self) -> None:
        jobs = self.jobs
        jobs.initialize(self.data_root, start_worker=False)
        valid_id = jobs.enqueue("playlist", "Playlist", [_track("1")], mode="append", start_worker=False)
        journal = self.data_root / "jobs.json"
        backup = self.data_root / "jobs.json.bak"
        valid_backup = backup.read_bytes()

        def job_fixture(**overrides: object) -> dict[str, object]:
            job = {
                "id": "bad",
                "playlist_id": "p",
                "title": "t",
                "state": "done",
                "outcome": "succeeded",
                "mode": "append",
                "created_at": 1,
                "total": 1,
                "done": 1,
                "failed": 0,
                "tracks": [{"id": "1", "title": "t", "provider": "deezer"}],
                "results": [
                    {"id": "1", "provider": "deezer", "key": "deezer:1", "title": "t", "ok": True, "error": "", "quality": "flac"}
                ],
                "current": None,
                "terminal_error": None,
            }
            job.update(overrides)
            return job

        malformed_cases = [
            job_fixture(
                done=2,
                results=[
                    {"id": "1", "provider": "deezer", "key": "deezer:1", "title": "t", "ok": True, "error": "", "quality": "flac"},
                    {"id": "1", "provider": "deezer", "key": "deezer:1", "title": "t", "ok": True, "error": "", "quality": "flac"},
                ],
            ),
            job_fixture(
                results=[
                    {"id": "2", "provider": "deezer", "key": "deezer:2", "title": "other", "ok": True, "error": "", "quality": "flac"}
                ],
            ),
            job_fixture(results=[]),
            job_fixture(failed=1),
            job_fixture(outcome="succeeded", failed=1, results=[
                {"id": "1", "provider": "deezer", "key": "deezer:1", "title": "t", "ok": False, "error": "failed", "quality": ""}
            ]),
        ]

        for malformed in malformed_cases:
            journal.write_text(json.dumps({"version": 1, "jobs": {"bad": malformed}}), encoding="utf-8")
            reloaded = importlib.reload(jobs)
            reloaded.initialize(self.data_root, start_worker=False)
            self.assertIsNotNone(reloaded.get_job(valid_id))
            self.assertEqual(valid_backup, backup.read_bytes())

    def test_default_flip_entrypoints_are_disabled_until_task5(self) -> None:
        jobs = self.jobs
        jobs.initialize(self.data_root, start_worker=False)
        with patch.object(jobs, "_flip_worker", side_effect=AssertionError("flip worker must not start")):
            with self.assertRaises(RuntimeError):
                jobs.enqueue_flip("playlist", "Playlist", to_wav=True)
        flip_id = jobs.enqueue_flip("playlist", "Playlist", to_wav=True, start_worker=False)
        self.assertEqual("queued", jobs.get_job(flip_id)["state"])

    def test_wav_delete_retry_updates_sidecar_before_deleting_source(self) -> None:
        jobs = self.jobs
        with tempfile.TemporaryDirectory(prefix="deckpipe-wav-delete-order-") as temporary:
            from app import library

            playlist = Path(temporary) / "playlist"
            playlist.mkdir()
            source = playlist / "source.flac"
            source.write_bytes(b"source")
            wav = playlist / "source.wav"
            wav.write_bytes(b"wav")
            library.save_sidecar(
                playlist,
                {
                    "tracks": {
                        "1": {
                            "status": "verify_failed_convert",
                            "file": source.name,
                            "source_file": source.name,
                            "duration_actual": 180.0,
                            "provider": "deezer",
                        }
                    }
                },
            )

            observed: dict[str, object] = {}

            def unlink_after_sidecar_check(self_path: Path, *args, **kwargs):
                observed["sidecar_at_unlink"] = library.load_sidecar(playlist)["tracks"]["1"]["file"]
                return original_unlink(self_path, *args, **kwargs)

            original_unlink = Path.unlink
            with (
                patch.object(jobs, "_wav_step", return_value=(wav, "")),
                patch.object(jobs, "_wav_mode", return_value="wav_delete"),
                patch.object(Path, "unlink", unlink_after_sidecar_check),
            ):
                ok, err, quality = jobs._process_track(
                    {"mode": "append"}, playlist, _track("1"), {"ds": None}, {"base": 0, "n": 0, "digits": 2}
                )

            self.assertTrue(ok)
            self.assertEqual("wav", quality)
            self.assertEqual("source.wav", observed["sidecar_at_unlink"])

    def test_wav_delete_unlink_failure_keeps_ready_wav_and_source_not_deleted_flag(self) -> None:
        jobs = self.jobs
        with tempfile.TemporaryDirectory(prefix="deckpipe-wav-delete-unlink-fail-") as temporary:
            from app import library

            playlist = Path(temporary) / "playlist"
            playlist.mkdir()

            source = playlist / "source.flac"
            source.write_bytes(b"source")
            wav = playlist / "source.wav"
            wav.write_bytes(b"wav")
            library.save_sidecar(
                playlist,
                {
                    "tracks": {
                        "1": {
                            "status": "verify_failed_convert",
                            "file": source.name,
                            "source_file": source.name,
                            "duration_actual": 180.0,
                            "provider": "deezer",
                        }
                    }
                },
            )

            def fake_unlink(self_path: Path, *args, **kwargs):
                if self_path == source:
                    raise PermissionError("synthetic unlink denied " + str(source))
                return original_unlink(self_path, *args, **kwargs)

            original_unlink = Path.unlink
            with (
                patch.object(jobs, "_wav_step", return_value=(wav, "")),
                patch.object(jobs, "_wav_mode", return_value="wav_delete"),
                patch.object(Path, "unlink", fake_unlink),
            ):
                ok, err, _quality = jobs._process_track(
                    {"mode": "append"}, playlist, _track("1"), {"ds": None}, {"base": 0, "n": 0, "digits": 2}
                )

            self.assertTrue(ok)
            self.assertEqual("", err)
            entry = library.load_sidecar(playlist)["tracks"]["1"]
            self.assertEqual("ok", entry["status"])
            self.assertEqual(wav.name, entry["file"])
            self.assertFalse(entry.get("source_deleted"))
            self.assertTrue(source.exists())

    def test_wav_delete_fresh_unlink_failure_keeps_completed_wav_ready(self) -> None:
        jobs = self.jobs
        with tempfile.TemporaryDirectory(prefix="deckpipe-wav-delete-fresh-unlink-fail-") as temporary:
            from app import library

            playlist = Path(temporary) / "playlist"
            playlist.mkdir()
            stage = playlist / "Artist 1 - Title 1.deckpipe-stage-owned.part.flac"
            source = playlist / "Artist 1 - Title 1.flac"
            wav = playlist / "Artist 1 - Title 1.wav"
            wav.write_bytes(b"wav")
            conversion_done = False

            class FakeSession:
                def download_track(self, _track_id, _out_dir, prefer="FLAC"):
                    stage.write_bytes(b"source")
                    return stage, "FLAC", {"DURATION": "180", "SNG_TITLE": "Title 1", "ART_NAME": "Artist 1"}

            def fake_wav_step(fpath: Path, _actual: float):
                nonlocal conversion_done
                self.assertEqual(source, fpath)
                conversion_done = True
                return wav, ""

            def fake_unlink(self_path: Path, *args, **kwargs):
                if conversion_done and self_path == source:
                    raise PermissionError("synthetic unlink denied " + str(source))
                return original_unlink(self_path, *args, **kwargs)

            original_unlink = Path.unlink
            with (
                patch.object(jobs, "get_session", return_value=FakeSession()),
                patch.object(jobs, "verify_file", return_value=(True, "", 180.0)),
                patch.object(jobs, "_numbering_on", return_value=False),
                patch.object(jobs, "_wav_step", side_effect=fake_wav_step),
                patch.object(jobs, "_wav_mode", return_value="wav_delete"),
                patch.object(Path, "unlink", fake_unlink),
                patch("app.tagger.write_tags", lambda _path, _meta: None),
            ):
                ok, err, _quality = jobs._process_track(
                    {"mode": "append"}, playlist, _track("1"), {"ds": None}, {"base": 0, "n": 0, "digits": 2}
                )

            self.assertTrue(ok)
            self.assertEqual("", err)
            entry = library.load_sidecar(playlist)["tracks"]["1"]
            self.assertEqual("ok", entry["status"])
            self.assertEqual(wav.name, entry["file"])
            self.assertFalse(entry.get("source_deleted"))
            self.assertTrue(source.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
