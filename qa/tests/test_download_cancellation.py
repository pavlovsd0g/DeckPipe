from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import activity_log, atomic_io, deezer_client, download_control, jobs, library, main, soundcloud
from test_security_contract import asgi_request


def track(track_id="1", provider="deezer"):
    return {"id": track_id, "provider": provider, "title": "Song " + track_id,
            "artist": "Artist", "album": "Test", "duration": 1,
            "url": "https://example.invalid/" + track_id}


class DownloadCancellationTests(unittest.TestCase):
    def setUp(self):
        self.stop_worker()
        self.temp = tempfile.TemporaryDirectory(prefix="cancel-")
        self.base = Path(self.temp.name)
        self.music = self.base / "Music"
        self.playlist = self.music / "Playlist"
        self.playlist.mkdir(parents=True)
        self.state = self.base / "State"
        self.patchers = [
            patch.object(deezer_client, "ROOT", self.state),
            patch.object(deezer_client, "CONFIG_PATH", self.state / "config.local.json"),
            patch.object(jobs, "playlist_dir", return_value=self.playlist),
            patch.object(activity_log, "_directory", None),
            patch.object(activity_log, "_last_id", 0),
        ]
        for patcher in self.patchers:
            patcher.start()
        deezer_client.save_config({"music_root": str(self.music), "numbering": False})
        activity_log.initialize(self.state)
        jobs.initialize(self.state, start_worker=False)
        self.releases = []
        self.processes = []

    def tearDown(self):
        for event in self.releases:
            event.set()
        for process in self.processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
        self.stop_worker()
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp.cleanup()

    def stop_worker(self):
        jobs._worker_stop.set()
        if jobs._worker_thread is not None:
            jobs._worker_thread.join(5)
            self.assertFalse(jobs._worker_thread.is_alive(), "worker must release files before test cleanup")

    def cancel(self, job_id):
        self.assertTrue(callable(getattr(jobs, "cancel_job", None)), "download cancellation is missing")
        return jobs.cancel_job(job_id)

    def wait_for(self, predicate):
        until = time.monotonic() + 5
        while time.monotonic() < until:
            if predicate():
                return
            threading.Event().wait(0.01)
        self.fail("worker did not reach the expected safe boundary")

    def start(self):
        with jobs._lock:
            jobs._ensure_worker_locked()

    def request(self, method, path):
        settings = main.app.user_middleware[0].kwargs["settings"]
        return asyncio.run(asgi_request(main.app, method, path, {
            "host": f"127.0.0.1:{settings.bound_port}",
            "authorization": "Bearer " + settings.api_token,
        }))

    def test_queued_cancellation_is_durable_and_never_resumes(self):
        # A cancelled queue entry must not reappear as queued after restart.
        job_id = jobs.enqueue("p", "Playlist", [track()])
        saved = self.playlist / "saved.flac"
        saved.write_bytes(b"existing music")
        result = self.cancel(job_id)
        self.assertEqual(("cancelled", "cancelled", 0, 0),
                         (result["state"], result["outcome"], result["done"], result["failed"]))
        self.assertEqual(result, self.cancel(job_id))
        jobs.initialize(self.state, start_worker=False)
        self.assertEqual("cancelled", jobs.get_job(job_id)["state"])
        self.assertEqual([], jobs._queue)
        self.assertEqual(b"existing music", saved.read_bytes())

    def test_activity_log_identifies_failed_processing_step_without_raw_exception(self):
        for step, error in [('validation', 'media validation failed'), ('metadata', 'metadata tagging failed'),
                            ('conversion', 'conversion failed'), ('publication', 'publication failed'),
                            ('download', 'download failed'), ('track', 'https://example.invalid/?token=private-value')]:
            with self.subTest(step=step):
                item = track(step)
                job_id = jobs.enqueue('p', 'Playlist', [item])
                jobs.mark_running(job_id)
                jobs.mark_item_complete(job_id, item, ok=False, error=error, quality='')
                events = [row for row in activity_log.list_entries()['entries'] if row.get('job_id') == job_id]
                self.assertEqual(step + '_failed', events[0]['stage'])
                self.assertEqual(step + '_failed', events[0]['error_code'])
                self.assertNotIn('private-value', json.dumps(events))
    def test_clear_completed_hides_only_terminal_records_and_preserves_music(self):
        # Clearing history must neither remove active jobs nor touch disk music/sidecars.
        complete = jobs.enqueue("p", "Playlist", [track()])
        jobs.mark_running(complete)
        jobs.mark_item_complete(complete, track(), ok=True, error="", quality="FLAC")
        jobs.mark_terminal(complete, outcome="succeeded")
        failed = jobs.enqueue("p", "Playlist", [track("2")])
        jobs.mark_running(failed)
        jobs.mark_terminal(failed, outcome="failed")
        cancelled = jobs.enqueue("p", "Playlist", [track("3")])
        self.cancel(cancelled)
        queued = jobs.enqueue("p", "Playlist", [track("4")])
        running = jobs.enqueue("p", "Playlist", [track("5")])
        jobs.mark_running(running)
        audio = self.playlist / "saved.flac"
        audio.write_bytes(b"complete music")
        library.update_track_status(self.playlist, "1", {"file": audio.name, "status": "ok"})
        sidecar = library.sidecar_path(self.playlist).read_bytes()

        status, _, body = self.request("POST", "/api/jobs/clear-completed")
        self.assertEqual(200, status)
        self.assertEqual({complete, failed, cancelled}, set(json.loads(body)["job_ids"]))
        self.assertEqual(3, json.loads(body)["cleared"])
        self.assertEqual({queued, running}, {job["id"] for job in jobs.list_jobs()})
        self.assertEqual(b"complete music", audio.read_bytes())
        self.assertEqual(sidecar, library.sidecar_path(self.playlist).read_bytes())
        jobs.initialize(self.state, start_worker=False)
        self.assertIsNone(jobs.get_job(complete))

    def test_cancel_http_contract_rejects_unknown_and_non_download_jobs(self):
        # Route dispatch and structured status must match the frontend contract.
        job_id = jobs.enqueue("p", "Playlist", [track()])
        status, _, body = self.request("POST", f"/api/jobs/{job_id}/cancel")
        self.assertEqual(200, status)
        self.assertEqual("cancelled", json.loads(body)["state"])
        self.assertEqual(404, self.request("POST", "/api/jobs/unknown/cancel")[0])
        flip = jobs.enqueue_flip("p", "Playlist", True, start_worker=False)
        self.assertEqual(409, self.request("POST", f"/api/jobs/{flip}/cancel")[0])

    def test_cancel_write_failure_leaves_memory_and_queue_unchanged(self):
        # A failed durable cancellation must not silently stop a queued download.
        job_id = jobs.enqueue("p", "Playlist", [track()])
        before = jobs.get_job(job_id)
        self.assertTrue(callable(getattr(jobs, "cancel_job", None)), "download cancellation is missing")
        with patch.object(jobs, "_write_json", side_effect=OSError("synthetic disk full")):
            with self.assertRaises(OSError):
                jobs.cancel_job(job_id)
        self.assertEqual(before, jobs.get_job(job_id))
        self.assertIn(job_id, jobs._queue)

    def test_clear_write_failure_keeps_terminal_history(self):
        # History must remain visible when its durable removal failed.
        job_id = jobs.enqueue("p", "Playlist", [track()])
        self.cancel(job_id)
        with patch.object(jobs, "_write_json", side_effect=OSError("synthetic disk full")):
            with self.assertRaises(OSError):
                jobs.clear_completed()
        self.assertEqual("cancelled", jobs.get_job(job_id)["state"])
        jobs.initialize(self.state, start_worker=False)
        self.assertEqual("cancelled", jobs.get_job(job_id)["state"])

    def test_restart_during_cancellation_does_not_resume_provider_work(self):
        # Recovering a cancelling journal must respect the user's durable stop request.
        job_id = jobs.enqueue("p", "Playlist", [track()])
        jobs.mark_running(job_id)
        self.assertEqual("cancelling", self.cancel(job_id)["state"])
        jobs.initialize(self.state, start_worker=False)
        self.assertEqual("cancelled", jobs.get_job(job_id)["state"])
        self.assertEqual([], jobs._queue)

    def register_stage(self, job_id, stage, *, prefix=False):
        self.assertTrue(callable(getattr(jobs, "register_owned_stage", None)), "durable stage ownership is missing")
        jobs.register_owned_stage(job_id, stage, prefix=prefix)

    def test_restart_cancellation_cleans_only_durably_owned_stages(self):
        # A crash after cancellation must not abandon the exact files the job owns.
        job_id = jobs.enqueue("p", "Playlist", [track()])
        jobs.mark_running(job_id)
        owned = atomic_io.make_staged_path(self.playlist / "Song.flac")
        self.register_stage(job_id, owned)
        owned.write_bytes(b"partial owned download")
        foreign = atomic_io.make_staged_path(self.playlist / "Other.flac")
        foreign.write_bytes(b"other job")
        final = self.playlist / "Song.flac"
        final.write_bytes(b"previous completed music")
        self.cancel(job_id)
        self.assertNotIn(str(owned), json.dumps(jobs.get_job(job_id)))
        self.assertNotIn(str(owned), json.dumps(jobs.list_jobs()))

        jobs.initialize(self.state, start_worker=False)

        self.assertFalse(owned.exists())
        self.assertEqual(b"other job", foreign.read_bytes())
        self.assertEqual(b"previous completed music", final.read_bytes())
        self.assertEqual("cancelled", jobs.get_job(job_id)["state"])
        self.assertEqual([], jobs._queue)

    def test_restart_cancellation_cleans_owned_soundcloud_fragments_by_exact_prefix(self):
        # The registered yt-dlp prefix owns its extension changes and fragment suffixes only.
        job_id = jobs.enqueue("p", "Playlist", [track(provider="sc")])
        jobs.mark_running(job_id)
        template = atomic_io.make_staged_path(self.playlist / "Song.download").with_suffix(".%(ext)s")
        prefix = template.name.split("%(ext)s")[0]
        self.register_stage(job_id, self.playlist / prefix, prefix=True)
        owned = [self.playlist / (prefix + suffix) for suffix in ("mp3", "mp3.part", "m4a.ytdl")]
        for path in owned:
            path.write_bytes(b"owned partial")
        foreign = atomic_io.make_staged_path(self.playlist / "Song.mp3")
        foreign.write_bytes(b"foreign partial")
        self.cancel(job_id)

        jobs.initialize(self.state, start_worker=False)

        self.assertTrue(all(not path.exists() for path in owned))
        self.assertTrue(foreign.exists())
        self.assertEqual("cancelled", jobs.get_job(job_id)["state"])

    def test_restart_cleanup_failure_is_truthful_and_cannot_discard_ownership(self):
        # A failed unlink after restart must retain recovery ownership and an actionable error.
        job_id = jobs.enqueue("p", "Playlist", [track()])
        jobs.mark_running(job_id)
        owned = atomic_io.make_staged_path(self.playlist / "Song.flac")
        self.register_stage(job_id, owned)
        owned.write_bytes(b"locked partial")
        self.cancel(job_id)
        original = Path.unlink

        def locked(path, *args, **kwargs):
            if path == owned:
                raise PermissionError("synthetic file lock")
            return original(path, *args, **kwargs)

        with patch.object(Path, "unlink", locked):
            jobs.initialize(self.state, start_worker=False)
        result = jobs.get_job(job_id)
        self.assertEqual(("done", "failed", "cancel_cleanup_failed"),
                         (result["state"], result["outcome"], result["terminal_error"]["code"]))
        self.assertEqual([], jobs.clear_completed()["job_ids"])
        self.assertTrue(owned.exists())
        jobs.initialize(self.state, start_worker=False)
        self.assertFalse(owned.exists())
        self.assertEqual("cancelled", jobs.get_job(job_id)["state"])

    def test_wav_source_removal_finishes_before_a_racing_cancellation_is_accepted(self):
        # Accepting cancellation and then deleting committed source audio is forbidden.
        source, wav = self.playlist / "source.flac", self.playlist / "source.wav"
        source.write_bytes(b"complete source")
        wav.write_bytes(b"complete wav")
        job_id = jobs.enqueue("p", "Playlist", [track()])
        jobs.mark_running(job_id)
        entered, release, cancel_entered, cancel_done = (threading.Event() for _ in range(4))
        self.releases.append(release)
        original = Path.unlink
        errors = []

        def slow_unlink(path, *args, **kwargs):
            if path == source:
                entered.set()
                release.wait(5)
            return original(path, *args, **kwargs)

        def remove_source():
            try:
                with download_control.cancellation_scope(
                        lambda: jobs._job_cancellation_requested(job_id, jobs._worker_generation), jobs._lock):
                    jobs._publish_wav_delete_state(self.playlist, "1", source, "deezer",
                                                  {"file": wav.name, "format": "wav", "status": "ok"})
            except Exception as exc:
                errors.append(type(exc).__name__)

        def cancel():
            cancel_entered.set()
            self.cancel(job_id)
            cancel_done.set()

        with patch.object(Path, "unlink", slow_unlink):
            remover = threading.Thread(target=remove_source)
            canceller = threading.Thread(target=cancel)
            remover.start()
            try:
                self.assertTrue(entered.wait(5))
                canceller.start()
                self.assertTrue(cancel_entered.wait(5))
                self.assertFalse(cancel_done.wait(0.2), "cancel returned before a committed source deletion finished")
            finally:
                release.set()
                remover.join(5)
                if canceller.ident is not None:
                    canceller.join(5)
        self.assertEqual([], errors)
        self.assertTrue(cancel_done.is_set())
        self.assertFalse(source.exists())
        self.assertTrue(wav.exists())

    def test_cancellation_after_queue_pop_before_running_never_downloads(self):
        # Removing the queued ID cannot be undone by an already-woken worker.
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = jobs.mark_running

        def delayed_mark_running(job_id, **kwargs):
            entered.set()
            release.wait(5)
            return original(job_id, **kwargs)

        job_id = jobs.enqueue("p", "Playlist", [track()])
        with patch.object(jobs, "mark_running", side_effect=delayed_mark_running):
            self.start()
            self.assertTrue(entered.wait(5))
            self.assertEqual("cancelled", self.cancel(job_id)["state"])
            # Clearing this terminal job is safe even while the obsolete worker wakes up.
            self.assertEqual([job_id], jobs.clear_completed()["job_ids"])
            release.set()
            self.wait_for(lambda: jobs._active_worker_item is None)
        self.assertIsNone(jobs.get_job(job_id))
        self.assertEqual([], list(self.playlist.iterdir()))

    def test_publication_wins_racing_cancellation_and_remains_in_results(self):
        # Cancellation after final rename must keep its file and truthful completed result.
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = jobs._set_track

        def after_publication(pl_dir, tid, **entry):
            if entry.get("status") == "ok":
                entered.set()
                release.wait(5)
            return original(pl_dir, tid, **entry)

        def download(_track_id, out_dir, prefer="FLAC"):
            stage = atomic_io.make_staged_path(out_dir / "Artist - Song 1.flac")
            stage.write_bytes(b"committed source")
            return stage, "FLAC", {"DURATION": "1", "SNG_TITLE": "Song 1", "ART_NAME": "Artist"}

        job_id = jobs.enqueue("p", "Playlist", [track(), track("2")])
        with (patch.object(jobs, "get_session", return_value=SimpleNamespace(download_track=download)),
              patch.object(jobs, "verify_file", return_value=(True, "", 1.0)),
              patch("app.tagger.write_tags", return_value=None),
              patch.object(jobs, "_set_track", side_effect=after_publication)):
            self.start()
            self.assertTrue(entered.wait(5))
            self.cancel(job_id)
            release.set()
            self.wait_for(lambda: jobs.get_job(job_id)["state"] in {"done", "cancelled"})
        result = jobs.get_job(job_id)
        self.assertEqual(("cancelled", 1, 0), (result["state"], result["done"], result["failed"]))
        self.assertEqual(["1"], [item["id"] for item in result["results"]])
        self.assertEqual(b"committed source", (self.playlist / "Artist - Song 1.flac").read_bytes())
        self.assertEqual("ok", library.load_sidecar(self.playlist)["tracks"]["1"]["status"])

    def test_stream_cancellation_cleans_owned_partial_without_retry_or_catalog_error(self):
        # Losing the cancellation exception in the quality/retry loop downloads again.
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        foreign = self.playlist / "foreign.deckpipe-stage-other.part.flac"
        foreign.write_bytes(b"other job")
        existing = self.playlist / "Artist - Song 1.flac"
        existing.write_bytes(b"previous music")
        responses = []

        class Response:
            def __enter__(self):
                responses.append(self)
                return self

            def __exit__(self, *_args):
                self.closed = True

            def raise_for_status(self):
                pass

            def iter_content(self, chunk_size):
                yield b"first audio chunk"
                entered.set()
                release.wait(5)
                yield b"second audio chunk"

        session = object.__new__(deezer_client.DeezerSession)
        session.gw = lambda *_args, **_kwargs: {
            "SNG_TITLE": "Song 1", "ART_NAME": "Artist", "DURATION": "1",
            "TRACK_TOKEN": "synthetic", "FILESIZE_FLAC": "100", "FILESIZE_MP3_320": "100"}
        session.media_url = lambda *_args: "https://example.invalid/audio"
        session.s = SimpleNamespace(get=lambda *_args, **_kwargs: Response())
        job_id = jobs.enqueue("p", "Playlist", [track(), track("2")])
        with patch.object(jobs, "get_session", return_value=session):
            self.start()
            self.assertTrue(entered.wait(5))
            persisted = json.loads((self.state / "jobs.json").read_text(encoding="utf-8"))["jobs"][job_id]
            self.assertTrue(persisted.get("owned_stages"), "provider must journal ownership before writing stream bytes")
            self.assertEqual("cancelling", self.cancel(job_id)["state"])
            release.set()
            self.wait_for(lambda: jobs.get_job(job_id)["state"] in {"done", "cancelled"})
        result = jobs.get_job(job_id)
        self.assertEqual(("cancelled", 0, 0, []),
                         (result["state"], result["done"], result["failed"], result["results"]))
        self.assertEqual(1, len(responses))
        self.assertTrue(responses[0].closed)
        self.assertEqual({foreign.name, existing.name}, {p.name for p in self.playlist.iterdir()})
        self.assertEqual({}, library.load_sidecar(self.playlist)["tracks"])
        self.assertEqual(b"previous music", existing.read_bytes())
        events = [event for event in activity_log.list_entries()["entries"] if event.get("job_id") == job_id]
        self.assertEqual(["cancelled", "cancel_requested", "track_started", "started", "queued"],
                         [event["stage"] for event in events])
        self.assertNotIn("https://example.invalid", json.dumps(events))

    def test_cancellation_after_download_before_publication_removes_stage(self):
        # A late provider return must be checked before validation, tagging, and commit.
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)

        def download(_track_id, out_dir, prefer="FLAC"):
            stage = atomic_io.make_staged_path(out_dir / "Artist - Song 1.flac")
            stage.write_bytes(b"downloaded audio")
            entered.set()
            release.wait(5)
            return stage, "FLAC", {"DURATION": "1", "SNG_TITLE": "Song 1", "ART_NAME": "Artist"}

        job_id = jobs.enqueue("p", "Playlist", [track()])
        with (patch.object(jobs, "get_session", return_value=SimpleNamespace(download_track=download)),
              patch.object(jobs, "verify_file", return_value=(True, "", 1.0)),
              patch("app.tagger.write_tags", return_value=None)):
            self.start()
            self.assertTrue(entered.wait(5))
            self.cancel(job_id)
            release.set()
            self.wait_for(lambda: jobs.get_job(job_id)["state"] in {"done", "cancelled"})
        self.assertEqual("cancelled", jobs.get_job(job_id)["state"])
        self.assertEqual([], list(self.playlist.iterdir()))
        self.assertEqual({}, library.load_sidecar(self.playlist)["tracks"])

    def test_locked_partial_reports_cleanup_failure_instead_of_successful_cancellation(self):
        # Windows file locks must not produce a false cancelled-and-clean terminal state.
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        owned = []
        original_unlink = Path.unlink

        def download(_track_id, out_dir, prefer="FLAC"):
            stage = atomic_io.make_staged_path(out_dir / "Artist - Song 1.flac")
            stage.write_bytes(b"downloaded audio")
            owned.append(stage)
            entered.set()
            release.wait(5)
            return stage, "FLAC", {"DURATION": "1", "SNG_TITLE": "Song 1", "ART_NAME": "Artist"}

        def locked_unlink(path, *args, **kwargs):
            if path in owned:
                raise PermissionError("synthetic locked stage")
            return original_unlink(path, *args, **kwargs)

        job_id = jobs.enqueue("p", "Playlist", [track()])
        with (patch.object(jobs, "get_session", return_value=SimpleNamespace(download_track=download)),
              patch.object(Path, "unlink", locked_unlink)):
            self.start()
            self.assertTrue(entered.wait(5))
            self.cancel(job_id)
            release.set()
            self.wait_for(lambda: jobs.get_job(job_id)["state"] in {"done", "cancelled"})
        result = jobs.get_job(job_id)
        self.assertEqual(("done", "failed"), (result["state"], result["outcome"]))
        self.assertEqual("cancel_cleanup_failed", result["terminal_error"]["code"])
        self.assertNotIn(str(owned[0]), json.dumps(result))
        self.assertEqual({}, library.load_sidecar(self.playlist)["tracks"])

    def test_soundcloud_progress_cancellation_cleans_only_its_download_and_fragments(self):
        # yt-dlp can wrap a hook exception; cancellation must still bypass worker retries.
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        foreign = self.playlist / "Song.deckpipe-stage-other.part.mp3"
        foreign.write_bytes(b"another download")
        attempts = []

        class YoutubeDL:
            def __init__(self, opts):
                self.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def extract_info(self, url, download=True):
                attempts.append(url)
                stage = Path(self.opts["outtmpl"].replace("%(ext)s", "mp3"))
                stage.write_bytes(b"partial audio")
                stage.with_name(stage.name + ".part").write_bytes(b"partial fragment")
                entered.set()
                release.wait(5)
                try:
                    for hook in self.opts.get("progress_hooks", []):
                        hook({"status": "downloading", "filename": str(stage)})
                except Exception:
                    raise RuntimeError("wrapped provider hook error") from None
                return {"duration": 1}

            def prepare_filename(self, info):
                return self.opts["outtmpl"].replace("%(ext)s", "mp3")

        job_id = jobs.enqueue("p", "Playlist", [track(provider="sc"), track("2", provider="sc")])
        with (patch.object(soundcloud.yt_dlp, "YoutubeDL", YoutubeDL),
              patch.object(soundcloud, "sc_oauth_token", return_value=None)):
            self.start()
            self.assertTrue(entered.wait(5))
            self.cancel(job_id)
            release.set()
            self.wait_for(lambda: jobs.get_job(job_id)["state"] in {"done", "cancelled"})
        result = jobs.get_job(job_id)
        self.assertEqual(("cancelled", 0, []), (result["state"], result["failed"], result["results"]))
        self.assertEqual(1, len(attempts))
        self.assertEqual([foreign], list(self.playlist.iterdir()))

    def _soundcloud_subprocess_cancellation(self, entrypoint):
        # A remux/HLS child owned by this job must stop; an unrelated child must survive.
        from yt_dlp.downloader import external
        from yt_dlp.postprocessor import ffmpeg
        unrelated = []
        children = []
        original_init = subprocess.Popen.__init__

        def capture_child(process, *args, **kwargs):
            original_init(process, *args, **kwargs)
            if "unrelated" not in args[0][-1]:
                children.append(process)
            self.processes.append(process)

        class YoutubeDL:
            def __init__(self, opts):
                self.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def extract_info(self, url, download=True):
                stage = Path(self.opts["outtmpl"].replace("%(ext)s", "m4a"))
                stage.write_bytes(b"owned remux partial")
                # This thread has no job ContextVar although the scoped bridge
                # is currently installed. Its child must remain unaffected.
                other_thread = threading.Thread(target=lambda: unrelated.append(ffmpeg.Popen(
                    [sys.executable, "-c", "import time; unrelated = True; time.sleep(60)"])))
                other_thread.start()
                other_thread.join(5)
                command = [sys.executable, "-c", "import time; time.sleep(60)"]
                if entrypoint == "remux":
                    ffmpeg.Popen.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                else:
                    with external.Popen(command, stdin=subprocess.PIPE) as process:
                        process.wait()
                return {"duration": 1}

            def prepare_filename(self, info):
                return self.opts["outtmpl"].replace("%(ext)s", "m4a")

        job_id = jobs.enqueue("p", "Playlist", [track(provider="sc")])
        with (patch.object(soundcloud.yt_dlp, "YoutubeDL", YoutubeDL),
              patch.object(soundcloud, "sc_oauth_token", return_value=None),
              patch.object(subprocess.Popen, "__init__", capture_child)):
            try:
                self.start()
                self.wait_for(lambda: bool(children))
                self.cancel(job_id)
                self.wait_for(lambda: jobs.get_job(job_id)["state"] in {"done", "cancelled"})
                self.assertEqual("cancelled", jobs.get_job(job_id)["state"])
                self.assertTrue(all(child.poll() is not None for child in children))
                self.assertEqual(1, len(unrelated))
                self.assertIsNone(unrelated[0].poll())
                self.assertEqual([], list(self.playlist.iterdir()))
            finally:
                for child in children:
                    if child.poll() is None:
                        child.kill()
                self.stop_worker()

    def test_soundcloud_remux_subprocess_is_cancelled_and_reaped(self):
        self._soundcloud_subprocess_cancellation("remux")

    def test_soundcloud_hls_subprocess_is_cancelled_and_reaped(self):
        self._soundcloud_subprocess_cancellation("hls")

    def test_cancelled_conversion_reaps_process_and_preserves_committed_source(self):
        # Cancellation must reap the encoder before cleaning its stage, preserving source media.
        self.assertTrue(callable(getattr(jobs, "cancel_job", None)), "download cancellation is missing")
        entered = threading.Event()
        processes = []
        stage_files = []

        def download(_track_id, out_dir, prefer="FLAC"):
            stage = atomic_io.make_staged_path(out_dir / "Artist - Song 1.flac")
            stage.write_bytes(b"complete source")
            return stage, "FLAC", {"DURATION": "1", "SNG_TITLE": "Song 1", "ART_NAME": "Artist"}

        real_popen = subprocess.Popen

        def encoder(command, **kwargs):
            output = Path(command[-1])
            stage_files.append(output)
            process = real_popen([sys.executable, "-c",
                "import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_bytes(b'partial wav'); time.sleep(60)",
                str(output)], **kwargs)
            processes.append(process)
            self.processes.append(process)
            entered.set()
            return process

        job_id = jobs.enqueue("p", "Playlist", [track(), track("2")])
        with (patch.object(jobs, "get_session", return_value=SimpleNamespace(download_track=download)),
              patch.object(jobs, "verify_file", return_value=(True, "", 1.0)),
              patch.object(jobs, "_wav_mode", return_value="wav_delete"),
              patch("app.tagger.write_tags", return_value=None),
              patch.object(download_control.subprocess, "Popen", side_effect=encoder)):
            self.start()
            self.assertTrue(entered.wait(5))
            self.wait_for(lambda: stage_files[0].exists())
            self.cancel(job_id)
            self.wait_for(lambda: jobs.get_job(job_id)["state"] in {"done", "cancelled"})
        self.assertEqual("cancelled", jobs.get_job(job_id)["state"])
        self.assertTrue(all(p.poll() is not None for p in processes))
        self.assertFalse(stage_files[0].exists())
        source = self.playlist / "Artist - Song 1.flac"
        self.assertEqual(b"complete source", source.read_bytes())
        entry = library.load_sidecar(self.playlist)["tracks"]["1"]
        self.assertEqual(("ok", source.name), (entry["status"], entry["file"]))
        self.assertEqual(0, jobs.get_job(job_id)["failed"])


if __name__ == "__main__":
    unittest.main()
