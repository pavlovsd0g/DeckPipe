from __future__ import annotations

import importlib
import json
import multiprocessing
import os
import tempfile
import threading
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
            self.assertIn("validator", err)
            self.assertEqual(b"old valid final", final.read_bytes())
            self.assertFalse(stage.exists())
            sidecar = library.load_sidecar(playlist)
            stored_file = sidecar["tracks"]["1"].get("file", "")
            self.assertNotIn(".part", stored_file)
            self.assertNotIn(".deckpipe-stage-", stored_file)


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

    def test_journal_recovers_backup_requeues_only_unfinished_idempotent_jobs_and_interrupts_flip(self) -> None:
        jobs = self.jobs
        self.assertTrue(hasattr(jobs, "initialize"), "jobs.initialize must explicitly bind a data root")
        jobs.initialize(self.data_root, start_worker=False)
        download_id = jobs.enqueue("playlist", "Playlist", [_track("1"), _track("2")], mode="append", start_worker=False)
        flip_id = jobs.enqueue_flip("playlist", "Playlist", to_wav=True, start_worker=False)
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
        self.assertEqual(["1"], [item["id"] for item in download["results"]])
        self.assertEqual(["2"], [item["id"] for item in reloaded.pending_track_ids(download_id)])
        self.assertEqual("done", flip["state"])
        self.assertEqual("interrupted", flip["outcome"])
        self.assertIn("restart", flip["terminal_error"]["code"])

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
        jobs.mark_terminal(job_id, outcome="failed", error={"code": "synthetic", "message": "synthetic failed"})

        self.assertEqual(4, len(writes))
        reloaded = importlib.reload(jobs)
        reloaded.initialize(self.data_root, start_worker=False)
        job = reloaded.get_job(job_id)
        self.assertEqual("done", job["state"])
        self.assertEqual("failed", job["outcome"])
        self.assertEqual({"code": "synthetic", "message": "synthetic failed"}, job["terminal_error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
