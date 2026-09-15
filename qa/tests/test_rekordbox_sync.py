# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib
import json
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch


class FakeRekordboxAdapter:
    def __init__(
        self,
        root: Path,
        snapshot: list[dict],
        *,
        fail_at: str | None = None,
        restored: list[dict] | None = None,
        lock_path: Path | None = None,
        shared: dict | None = None,
    ) -> None:
        self.root = Path(root)
        self._shared = {} if shared is None else shared
        self.db_path = self.root / "master.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.db_path.exists():
            self.db_path.write_bytes(b"db-before")
        self.anlz_path = self.root / "PIONEER" / "USBANLZ" / "track.DAT"
        self.anlz_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.anlz_path.exists():
            self.anlz_path.write_bytes(b"anlz-before")
        self.xml_path = self.root / "masterPlaylists6.xml"
        if not self.xml_path.exists():
            self.xml_path.write_text("<xml>before</xml>", encoding="utf-8")
        self.external_write_target = self.root / "PIONEER" / "USBANLZ" / "new-track.DAT"
        self.lock_path = lock_path or (self.root / "mutation.lock")
        self._shared.setdefault("snapshot", [dict(item) for item in snapshot])
        self._shared.setdefault("restored", [dict(item) for item in (restored or snapshot)])
        self._shared.setdefault("events", [])
        self._shared.setdefault("handles", [])
        self._shared.setdefault("open_handles", 0)
        self._shared.setdefault("reopen_snapshots", [])
        self._shared.setdefault("fail_fresh_verify", False)
        self._shared.setdefault("fail_restore", False)
        self._shared.setdefault("playlist_exists", True)
        self._snapshot = self._shared["snapshot"]
        self._restored = self._shared["restored"]
        self._force_reopen_snapshot = restored is not None
        self.fail_at = fail_at
        self.events: list[str] = self._shared["events"]
        self.begin_count = 0
        self.commit_count = 0
        self.rollback_count = 0
        self.close_count = 0
        self.closed = False
        self.restored_files: list[str] = []
        self.used_internal_commit_helper = False
        self.hold_apply_event: threading.Event | None = None
        self.release_apply_event: threading.Event | None = None
        self._shared["handles"].append(self)
        self._shared["open_handles"] += 1

    def mutation_lock_path(self) -> Path:
        if self._shared.get("fail_lock_path"):
            raise RuntimeError("lock path leaked C:\\Users\\secret\\master.db")
        return self.lock_path

    def is_rekordbox_running(self) -> bool:
        if self._shared.get("fail_running_check"):
            raise RuntimeError("status leaked D:\\private\\master.db")
        return False

    def playlist_exists(self, _playlist_name: str) -> bool:
        self.events.append("playlist_exists")
        return bool(self._shared.get("playlist_exists", True))

    def external_files_for_playlist(self, _playlist_name: str, _desired: list[dict]) -> list[Path]:
        return [self.anlz_path, self.xml_path, self.external_write_target]

    def snapshot_playlist(self, _playlist_name: str) -> list[dict]:
        if self.closed:
            raise RuntimeError("closed handle snapshot at C:\\Users\\secret\\master.db")
        if self._shared.get("assert_lock_exists_on_snapshot") and not self.lock_path.exists():
            raise RuntimeError("snapshot before lock at C:\\Users\\secret\\master.db")
        self.events.append("snapshot")
        if self._shared.get("fail_fresh_verify"):
            raise RuntimeError("verify failed at D:\\private\\master.db")
        if (
            self.db_path.exists()
            and self._shared.get("committed")
            and self.db_path.read_bytes() == b"db-before"
            and self._shared.get("snapshot") != self._restored
        ):
            self._shared["snapshot"] = [dict(item) for item in self._restored]
        return [dict(item) for item in self._shared["snapshot"]]

    def begin(self) -> None:
        self.begin_count += 1
        self.events.append("begin")
        if self.fail_at == "begin":
            raise RuntimeError("begin failed at C:\\Users\\secret\\master.db")

    def apply_operations(self, operations: dict) -> None:
        self.events.append("apply")
        if self.hold_apply_event is not None:
            self.hold_apply_event.set()
            if self.release_apply_event is not None:
                self.release_apply_event.wait(timeout=5)
        if self.fail_at == "apply":
            raise RuntimeError("C:\\Users\\secret\\apply failed")
        if not self._shared.get("playlist_exists", True):
            if operations.get("_create_missing"):
                self.events.append("create_playlist")
                self._shared["playlist_exists"] = True
            else:
                raise RuntimeError("playlist missing")
        self._shared["snapshot"] = [dict(item, rb_id=item.get("rb_id") or item["provider_id"]) for item in operations["desired_resolved"]]

    def save_external_files(self) -> None:
        self.events.append("external_save")
        self.external_write_target.write_bytes(b"new external")
        if self.fail_at == "external":
            raise RuntimeError("D:\\private\\anlz failed")

    def commit(self) -> None:
        self.commit_count += 1
        self.events.append("commit")
        if self.fail_at == "commit":
            raise RuntimeError("commit failed at E:\\music\\private")
        self.db_path.write_bytes(b"db-mutated")
        self._shared["committed"] = True

    def close(self) -> None:
        if self.closed:
            return
        self.close_count += 1
        self.events.append("close")
        self.closed = True
        self._shared["open_handles"] -= 1

    def reopen(self):
        self.events.append("reopen")
        if self.fail_at == "reopen" and not self._shared.get("reopen_failed_once"):
            self._shared["reopen_failed_once"] = True
            raise RuntimeError("reopen failed at C:\\Users\\secret\\master.db")
        snapshots = self._shared.get("reopen_snapshots") or []
        if snapshots:
            self._shared["snapshot"] = [dict(item) for item in snapshots.pop(0)]
        elif self._force_reopen_snapshot:
            self._shared["snapshot"] = [dict(item) for item in self._restored]
        return FakeRekordboxAdapter(self.root, self._shared["snapshot"], fail_at=self.fail_at, lock_path=self.lock_path, shared=self._shared)

    def rollback(self) -> None:
        if self.closed:
            raise RuntimeError("closed handle rollback")
        self.rollback_count += 1
        self.events.append("rollback")
        self._shared["snapshot"] = [dict(item) for item in self._restored]

    def restore_from_backup(self, backup: dict) -> None:
        self.events.append("restore")
        if self._shared.get("fail_restore"):
            raise RuntimeError("restore failed at E:\\private\\master.db")
        self._shared["snapshot"] = [dict(item) for item in self._restored]
        self.db_path.write_bytes(Path(backup["files"]["database"]["backup"]).read_bytes())
        for item in backup["files"]["external"]:
            original = Path(item["path"])
            if item["existed"]:
                original.write_bytes(Path(item["backup"]).read_bytes())
                self.restored_files.append(original.name)
            elif original.exists():
                original.unlink()
                self.restored_files.append(original.name)


def track(provider_id: str, title: str, artist: str, album: str, duration: int, position: int, path: Path, **extra) -> dict:
    item = {
        "provider_id": provider_id,
        "title": title,
        "artist": artist,
        "album": album,
        "duration": duration,
        "position": position,
        "path": str(path),
    }
    item.update(extra)
    return item


class RekordboxSyncPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        from app import rekordbox

        self.rb = importlib.reload(rekordbox)
        self.tempdir = tempfile.TemporaryDirectory(prefix="deckpipe-rb-plan-")
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def touch(self, name: str, content: bytes = b"media") -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_dry_run_is_deterministic_and_reports_all_diff_surfaces(self) -> None:
        alpha = self.touch("Alpha.flac")
        beta_new = self.touch("Beta-new.flac")
        dup = self.touch("Dup.flac")
        desired = [
            track("deezer:2", "Beta", "Artist B", "Album", 201, 1, beta_new),
            track("deezer:1", "Alpha", "Artist A", "Album", 180, 2, alpha),
            track("deezer:3", "Gamma", "Artist G", "Album", 199, 3, self.touch("Gamma.flac")),
            track("deezer:dup", "Dup A", "Artist", "Album", 111, 4, dup),
            track("deezer:dup", "Dup B", "Artist", "Album", 112, 5, dup),
            track("deezer:missing", "Missing", "Artist", "Album", 113, 6, self.root / "Missing.flac"),
            track("deezer:relative", "Relative", "Artist", "Album", 114, 7, Path("relative.flac")),
        ]
        current = [
            track("deezer:1", "Alpha Old", "Artist A", "Album", 180, 1, alpha, rb_id="rb1"),
            track("deezer:2", "Beta", "Artist B", "Album", 200, 2, self.touch("Beta-old.flac"), rb_id="rb2"),
            track("deezer:old", "Old", "Artist O", "Album", 120, 3, self.touch("Old.flac"), rb_id="rb-old"),
            track("deezer:amb", "Amb A", "Artist", "Album", 100, 4, self.touch("Amb-a.flac"), rb_id="rb-a"),
            track("deezer:amb", "Amb B", "Artist", "Album", 100, 5, self.touch("Amb-b.flac"), rb_id="rb-b"),
        ]

        first = self.rb.plan_playlist_sync(desired, current)
        second = self.rb.plan_playlist_sync(desired, current)

        self.assertEqual(first, second)
        self.assertEqual(first["counts"], {
            "desired": 7,
            "current": 5,
            "resolved": 3,
            "add": 1,
            "remove": 1,
            "reorder": 2,
            "metadata": 2,
            "path": 1,
            "unresolved": 4,
        })
        self.assertEqual(["deezer:3"], [item["provider_id"] for item in first["add"]])
        self.assertEqual(["deezer:old"], [item["provider_id"] for item in first["remove"]])
        self.assertEqual(["deezer:2", "deezer:1"], [item["provider_id"] for item in first["reorder"]])
        self.assertEqual(["deezer:2", "deezer:1"], [item["provider_id"] for item in first["metadata"]])
        self.assertEqual(["deezer:2"], [item["provider_id"] for item in first["path"]])
        self.assertEqual(
            ["ambiguous_current_identity", "duplicate_desired_identity", "missing_desired_path", "relative_desired_path"],
            [item["code"] for item in first["unresolved"]],
        )
        self.assertEqual(len(first["hash"]), 64)

    def test_unicode_boundary_round_trips_without_normalization_or_glyph_loss(self) -> None:
        nfc_title = "Café 😶‍🌫️ Привет"
        nfd_artist = "Cafe\u0301 Artist 🎛️"
        media = self.touch(("深" * 80) + ".flac")
        desired = [track("sc:💽-1", nfc_title, nfd_artist, "Альбом 🧪", 321, 1, media)]

        plan = self.rb.plan_playlist_sync(desired, [])

        added = plan["add"][0]
        self.assertEqual(nfc_title, added["title"])
        self.assertEqual(nfd_artist, added["artist"])
        self.assertEqual("Альбом 🧪", added["album"])
        self.assertEqual(str(media), added["path"])

    def test_planner_canonicalizes_permuted_inputs_and_rejects_bad_positions(self) -> None:
        one = self.touch("one.flac")
        two = self.touch("two.flac")
        dup_a = self.touch("dup-a.flac")
        dup_b = self.touch("dup-b.flac")
        zero = self.touch("zero.flac")
        desired_a = [
            track("sc:two", "Two", "B", "Album", 20, 3, two),
            track("deezer:dup-a", "Dup A", "A", "Album", 30, 2, dup_a),
            track("deezer:one", "One", "A", "Album", 10, 1, one),
            track("sc:dup-b", "Dup B", "B", "Album", 31, 2, dup_b),
            track("deezer:zero", "Zero", "Z", "Album", 0, 0, zero),
        ]
        desired_b = list(reversed(desired_a))
        current = [
            track("sc:two", "Two Old", "B", "Album", 20, 1, two),
            track("deezer:one", "One", "A", "Album", 10, 2, one),
        ]

        plan_a = self.rb.plan_playlist_sync(desired_a, current)
        plan_b = self.rb.plan_playlist_sync(desired_b, list(reversed(current)))

        self.assertEqual(plan_a, plan_b)
        self.assertEqual(["deezer:one", "sc:two"], [item["provider_id"] for item in plan_a["desired_resolved"]])
        self.assertEqual(
            ["duplicate_desired_position", "nonpositive_desired_position"],
            [item["code"] for item in plan_a["unresolved"]],
        )
        self.assertEqual(["deezer:dup-a", "sc:dup-b"], plan_a["unresolved"][0]["provider_ids"])
        self.assertEqual("deezer:zero", plan_a["unresolved"][1]["provider_id"])


# Stage C coordinator and mutation assertions now use real SQLCipher/ORM fixtures
# in test_rekordbox_database.py. Historical HTTP gates below migrate in Task 2.


class RekordboxApiAndFlipGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_token = os.environ.get("DECKPIPE_API_TOKEN")
        self.old_port = os.environ.get("DECKPIPE_BOUND_PORT")
        os.environ["DECKPIPE_API_TOKEN"] = "rekordbox-sync-test-token"
        os.environ["DECKPIPE_BOUND_PORT"] = "8123"
        from app import main
        from app import rekordbox
        self.main = importlib.reload(main)
        self.rb = importlib.reload(rekordbox)
        self.tempdir = tempfile.TemporaryDirectory(prefix="deckpipe-rb-api-")
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()
        os.environ.pop("DECKPIPE_RB_EXPERIMENTAL", None)
        if self.old_token is None:
            os.environ.pop("DECKPIPE_API_TOKEN", None)
        else:
            os.environ["DECKPIPE_API_TOKEN"] = self.old_token
        if self.old_port is None:
            os.environ.pop("DECKPIPE_BOUND_PORT", None)
        else:
            os.environ["DECKPIPE_BOUND_PORT"] = self.old_port

    def test_api_apply_preserves_intent_and_lets_coordinator_reject_unauthorized_before_adapter_open(self) -> None:
        playlist = self.root / "playlist"
        playlist.mkdir()
        ready = playlist / "ready.flac"
        ready.write_bytes(b"ready")
        from app import library

        library.save_sidecar(
            playlist,
            {
                "tracks": {
                    "1": {
                        "provider": "deezer",
                        "title": "Ready",
                        "artist": "Artist",
                        "album": "Album",
                        "duration_expected": 10,
                        "position": 1,
                        "file": ready.name,
                        "status": "ok",
                    }
                }
            },
        )
        body = self.main.RbSyncIn(playlist_key="local:fixture", playlist_title="Fixture")
        cases = [
            (None, None, "apply_not_confirmed", 0),
            ("1", None, "apply_not_confirmed", 0),
            (None, self.rb.APPLY_CONFIRMATION_TOKEN, "apply_not_confirmed", 0),
            ("1", self.rb.APPLY_CONFIRMATION_TOKEN, None, 2),
        ]

        with patch.object(self.main.library, "playlist_dir", return_value=playlist):
            for env_value, token, expected_error, expected_adapter_calls in cases:
                with self.subTest(env_value=env_value, token=bool(token)):
                    if env_value is None:
                        os.environ.pop("DECKPIPE_RB_EXPERIMENTAL", None)
                    else:
                        os.environ["DECKPIPE_RB_EXPERIMENTAL"] = env_value
                    shared: dict = {}
                    adapters: list[FakeRekordboxAdapter] = []

                    def adapter_factory():
                        adapter = FakeRekordboxAdapter(self.root / "api-apply", [], shared=shared)
                        adapters.append(adapter)
                        return adapter

                    with patch.object(self.main.rb, "_PyrekordboxAdapter", side_effect=adapter_factory):
                        result = self.main.api_rb_sync(body, dry_run=False, confirmation_token=token)

                    self.assertFalse(result["dry_run"])
                    self.assertEqual(expected_error, result["error"]["code"] if result["error"] else None)
                    self.assertEqual(expected_adapter_calls, len(adapters))
                    if expected_error is None:
                        self.assertTrue(result["applied"])
                        self.assertEqual(1, adapters[0].begin_count)
                        self.assertEqual(1, adapters[0].commit_count)
                        self.assertTrue(adapters[0].closed)
                        self.assertTrue(adapters[1].closed)
                    else:
                        self.assertFalse(result["applied"])

    def test_api_explicit_dry_run_opens_read_only_adapter_and_stays_dry_run(self) -> None:
        playlist = self.root / "playlist-dry-run"
        playlist.mkdir()
        ready = playlist / "ready.flac"
        ready.write_bytes(b"ready")
        from app import library

        library.save_sidecar(
            playlist,
            {
                "tracks": {
                    "1": {
                        "provider": "deezer",
                        "title": "Ready",
                        "artist": "Artist",
                        "album": "Album",
                        "duration_expected": 10,
                        "position": 1,
                        "file": ready.name,
                        "status": "ok",
                    }
                }
            },
        )
        body = self.main.RbSyncIn(playlist_key="local:fixture", playlist_title="Fixture")
        adapters: list[FakeRekordboxAdapter] = []

        def adapter_factory():
            adapter = FakeRekordboxAdapter(self.root / "api-dry-run", [])
            adapters.append(adapter)
            return adapter

        os.environ.pop("DECKPIPE_RB_EXPERIMENTAL", None)
        with patch.object(self.main.library, "playlist_dir", return_value=playlist), patch.object(self.main.rb, "_PyrekordboxAdapter", side_effect=adapter_factory):
            result = self.main.api_rb_sync(body, dry_run=True, confirmation_token=self.rb.APPLY_CONFIRMATION_TOKEN)

        self.assertTrue(result["dry_run"])
        self.assertFalse(result["applied"])
        self.assertIsNone(result["error"])
        self.assertEqual(1, len(adapters))
        self.assertEqual(["snapshot", "close"], adapters[0].events)
        self.assertEqual(0, adapters[0].begin_count)
        self.assertEqual(0, adapters[0].commit_count)

    def test_api_dry_run_uses_only_ready_regular_in_root_files_and_structured_schema(self) -> None:
        playlist = self.root / "playlist"
        playlist.mkdir()
        ready = playlist / "ready.flac"
        ready.write_bytes(b"ok")
        partial = playlist / "bad.deckpipe-stage-x.part.flac"
        partial.write_bytes(b"bad")
        outside = self.root / "outside.flac"
        outside.write_bytes(b"outside")
        from app import library

        library.save_sidecar(
            playlist,
            {
                "tracks": {
                    "1": {"provider": "deezer", "title": "Ready", "artist": "A", "album": "B", "duration_expected": 10, "position": 1, "file": ready.name, "status": "ok"},
                    "2": {"provider": "deezer", "title": "Partial", "artist": "A", "album": "B", "duration_expected": 11, "position": 2, "file": partial.name, "status": "ok"},
                    "3": {"provider": "deezer", "title": "Outside", "artist": "A", "album": "B", "duration_expected": 12, "position": 3, "file": str(outside), "status": "ok"},
                    "4": {"provider": "deezer", "title": "Missing", "artist": "A", "album": "B", "duration_expected": 13, "position": 4, "file": "missing.flac", "status": "ok"},
                }
            },
        )
        body = self.main.RbSyncIn(playlist_key="local:fixture", playlist_title="Fixture")

        with (
            patch.object(self.main.library, "playlist_dir", return_value=playlist),
            patch.object(self.main.jobs, "enqueue_flip", side_effect=AssertionError("dry-run enqueued conversion")),
            patch.object(self.main.rb, "sync_playlist", return_value={
                "dry_run": True,
                "applied": False,
                "reconciled": False,
                "unresolved": [],
                "backup_id": None,
                "error": None,
                "plan": {"counts": {"desired": 1}},
            }) as sync,
        ):
            result = self.main.api_rb_sync(body)

        desired = sync.call_args.args[1]
        self.assertEqual(1, len(desired))
        self.assertEqual("deezer:1", desired[0]["provider_id"])
        self.assertEqual(str(ready), desired[0]["path"])
        self.assertEqual({"dry_run", "applied", "reconciled", "unresolved", "backup_id", "error", "plan"}, set(result))

    def test_api_rb_sync_passes_through_structured_adapter_open_failure(self) -> None:
        body = self.main.RbSyncIn(playlist_key="local:fixture", playlist_title="Fixture")
        structured = {
            "dry_run": True,
            "applied": False,
            "reconciled": False,
            "unresolved": [],
            "backup_id": None,
            "error": {"code": "adapter_open_failed", "message": "Rekordbox sync failed"},
            "plan": {"counts": {"desired": 0}},
        }

        with patch.object(self.main, "_desired_tracks_for_rekordbox", return_value=[]), patch.object(self.main.rb, "sync_playlist", return_value=structured):
            result = self.main.api_rb_sync(body)

        self.assertEqual(structured["error"], result["error"])
        self.assertTrue(result["dry_run"])

    def test_default_flip_fails_closed_and_explicit_flip_uses_reconciled_callback_for_sidecar_advance(self) -> None:
        from app import jobs, library

        jobs.initialize(self.root / "data", start_worker=False)
        with self.assertRaises(RuntimeError):
            jobs.enqueue_flip("local:fixture", "Fixture", to_wav=True)

        playlist = self.root / "playlist"
        playlist.mkdir()
        source = playlist / "source.flac"
        source.write_bytes(b"source")
        wav = playlist / "source.wav"
        wav.write_bytes(b"wav")
        library.save_sidecar(
            playlist,
            {"tracks": {"1": {"provider": "deezer", "title": "Source", "artist": "Artist", "album": "Album", "duration_expected": 12, "position": 1, "file": source.name, "status": "ok"}}},
        )
        callbacks = []

        def fake_sync(_playlist_title, desired, **kwargs):
            self.assertEqual(1, len(desired))
            self.assertFalse(library.load_sidecar(playlist)["tracks"]["1"].get("flipped_to"))
            kwargs["on_reconciled"]({"ok": True})
            callbacks.append("after")
            return {"dry_run": False, "applied": True, "reconciled": True, "unresolved": [], "backup_id": "b", "error": None, "plan": {"counts": {"resolved": 1}}}

        with (
            patch.object(jobs, "playlist_dir", return_value=playlist),
            patch.object(jobs, "_wav_mode", return_value="source"),
            patch.object(jobs, "verify_file", return_value=(True, "", 12.0)),
            patch("app.jobs.convert_to_wav", create=True, return_value=wav),
            patch.object(jobs.rb, "sync_playlist", side_effect=fake_sync),
        ):
            job_id = jobs.enqueue_flip("local:fixture", "Fixture", to_wav=True, start_worker=False, allow_rekordbox_apply=True)
            jobs._flip_worker(job_id, True, 64)

        sidecar = library.load_sidecar(playlist)
        self.assertEqual("wav", sidecar["tracks"]["1"].get("flipped_to"))
        job = jobs.get_job(job_id)
        self.assertEqual("succeeded", job["outcome"])
        self.assertEqual(1, len(callbacks))
        self.assertLessEqual(job["workers"], jobs.MAX_REKORDBOX_FLIP_WORKERS)

    def test_flip_worker_fails_terminally_when_rekordbox_callback_failed_after_db_reconcile(self) -> None:
        from app import jobs, library

        jobs.initialize(self.root / "data-callback-fail", start_worker=False)
        playlist = self.root / "playlist-callback-fail"
        playlist.mkdir()
        source = playlist / "source.flac"
        source.write_bytes(b"source")
        wav = playlist / "source.wav"
        wav.write_bytes(b"wav")
        library.save_sidecar(
            playlist,
            {"tracks": {"1": {"provider": "deezer", "title": "Source", "artist": "Artist", "album": "Album", "duration_expected": 12, "position": 1, "file": source.name, "status": "ok"}}},
        )

        def fake_sync(_playlist_title, _desired, **_kwargs):
            return {
                "dry_run": False,
                "applied": True,
                "reconciled": True,
                "unresolved": [],
                "backup_id": "b-callback",
                "error": {"code": "callback_failed", "message": "Rekordbox sync callback failed"},
                "plan": {"counts": {"resolved": 1}},
            }

        with (
            patch.object(jobs, "playlist_dir", return_value=playlist),
            patch.object(jobs, "_wav_mode", return_value="source"),
            patch.object(jobs, "verify_file", return_value=(True, "", 12.0)),
            patch("app.jobs.convert_to_wav", create=True, return_value=wav),
            patch.object(jobs.rb, "sync_playlist", side_effect=fake_sync),
        ):
            job_id = jobs.enqueue_flip("local:fixture", "Fixture", to_wav=True, start_worker=False, allow_rekordbox_apply=True)
            jobs._flip_worker(job_id, True, 64)

        sidecar = library.load_sidecar(playlist)
        self.assertNotIn("flipped_to", sidecar["tracks"]["1"])
        job = jobs.get_job(job_id)
        self.assertEqual("failed", job["outcome"])
        self.assertEqual("callback_failed", job["terminal_error"]["code"])
        self.assertEqual(1, job["rb_updated"])

    def test_powershell_unicode_runner_requires_explicit_database_path_and_has_no_live_default(self) -> None:
        runner = Path("qa/run-rekordbox-unicode-audit.ps1")
        text = runner.read_text(encoding="utf-8")
        self.assertNotIn("APPDATA", text.upper())
        self.assertRegex(text, r"DatabasePath\s*=\s*''")
        self.assertRegex(text, r"DisposableCopy")
        self.assertNotRegex(text, r"&\s*\$PythonPath\s+\$auditPath\s+\$source\b")


if __name__ == "__main__":
    unittest.main(verbosity=2)
