# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib
import json
import os
import tempfile
import threading
import unittest
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
    ) -> None:
        self.root = Path(root)
        self.db_path = self.root / "master.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.write_bytes(b"db-before")
        self.anlz_path = self.root / "PIONEER" / "USBANLZ" / "track.DAT"
        self.anlz_path.parent.mkdir(parents=True, exist_ok=True)
        self.anlz_path.write_bytes(b"anlz-before")
        self.xml_path = self.root / "masterPlaylists6.xml"
        self.xml_path.write_text("<xml>before</xml>", encoding="utf-8")
        self.external_write_target = self.root / "PIONEER" / "USBANLZ" / "new-track.DAT"
        self.lock_path = lock_path or (self.root / "mutation.lock")
        self._snapshot = [dict(item) for item in snapshot]
        self._restored = [dict(item) for item in (restored or snapshot)]
        self._force_reopen_snapshot = restored is not None
        self.fail_at = fail_at
        self.events: list[str] = []
        self.begin_count = 0
        self.commit_count = 0
        self.rollback_count = 0
        self.close_count = 0
        self.restored_files: list[str] = []
        self.used_internal_commit_helper = False
        self.hold_apply_event: threading.Event | None = None
        self.release_apply_event: threading.Event | None = None

    def mutation_lock_path(self) -> Path:
        return self.lock_path

    def is_rekordbox_running(self) -> bool:
        return False

    def external_files_for_playlist(self, _playlist_name: str, _desired: list[dict]) -> list[Path]:
        return [self.anlz_path, self.xml_path, self.external_write_target]

    def snapshot_playlist(self, _playlist_name: str) -> list[dict]:
        self.events.append("snapshot")
        return [dict(item) for item in self._snapshot]

    def begin(self) -> None:
        self.begin_count += 1
        self.events.append("begin")

    def apply_operations(self, operations: dict) -> None:
        self.events.append("apply")
        if self.hold_apply_event is not None:
            self.hold_apply_event.set()
            if self.release_apply_event is not None:
                self.release_apply_event.wait(timeout=5)
        if self.fail_at == "apply":
            raise RuntimeError("C:\\Users\\secret\\apply failed")
        self._snapshot = [dict(item, rb_id=item.get("rb_id") or item["provider_id"]) for item in operations["desired_resolved"]]

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

    def close(self) -> None:
        self.close_count += 1
        self.events.append("close")

    def reopen(self):
        self.events.append("reopen")
        if self.fail_at == "reopen":
            raise RuntimeError("reopen failed at C:\\Users\\secret\\master.db")
        if self._force_reopen_snapshot:
            self._snapshot = [dict(item) for item in self._restored]
        return self

    def rollback(self) -> None:
        self.rollback_count += 1
        self.events.append("rollback")
        self._snapshot = [dict(item) for item in self._restored]

    def restore_from_backup(self, backup: dict) -> None:
        self.events.append("restore")
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


class RekordboxSyncCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        from app import rekordbox

        self.rb = importlib.reload(rekordbox)
        self.tempdir = tempfile.TemporaryDirectory(prefix="deckpipe-rb-apply-")
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def touch(self, name: str, content: bytes = b"media") -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def adapter_factory(self, adapter: FakeRekordboxAdapter):
        return lambda: adapter

    def base_desired_current(self) -> tuple[list[dict], list[dict]]:
        old = self.touch("old.flac")
        new = self.touch("new.wav")
        return (
            [track("deezer:1", "Title", "Artist", "Album", 180, 1, new, rb_id="rb1")],
            [track("deezer:1", "Old", "Artist", "Album", 179, 1, old, rb_id="rb1")],
        )

    def test_dry_run_performs_zero_mutation_backup_or_callback(self) -> None:
        desired, current = self.base_desired_current()
        adapter = FakeRekordboxAdapter(self.root / "adapter", current)
        calls = []

        result = self.rb.sync_playlist(
            "Playlist",
            desired,
            dry_run=True,
            adapter_factory=self.adapter_factory(adapter),
            on_reconciled=lambda _result: calls.append("called"),
        )

        self.assertTrue(result["dry_run"])
        self.assertFalse(result["applied"])
        self.assertFalse(result["reconciled"])
        self.assertIsNone(result["backup_id"])
        self.assertEqual(0, adapter.begin_count)
        self.assertEqual([], calls)
        self.assertFalse((adapter.db_path.parent / "deckpipe-rekordbox-backups").exists())

    def test_apply_creates_unique_verified_backup_with_db_anlz_xml_and_missing_external_inventory(self) -> None:
        desired, current = self.base_desired_current()
        adapter = FakeRekordboxAdapter(self.root / "adapter", current)

        first = self.rb.sync_playlist("Playlist", desired, dry_run=False, adapter_factory=self.adapter_factory(adapter), confirmation_token=self.rb.APPLY_CONFIRMATION_TOKEN)
        second = self.rb.sync_playlist("Playlist", desired, dry_run=False, adapter_factory=self.adapter_factory(adapter), confirmation_token=self.rb.APPLY_CONFIRMATION_TOKEN)

        self.assertNotEqual(first["backup_id"], second["backup_id"])
        self.assertEqual(["master.db", "track.DAT", "masterPlaylists6.xml", "new-track.DAT"], first["backup"]["inventory_names"])
        self.assertEqual(["new-track.DAT"], first["backup"]["originally_missing"])
        self.assertTrue(first["backup"]["verified"])
        self.assertEqual(2, len(list((adapter.db_path.parent / "deckpipe-rekordbox-backups").iterdir())))

    def test_apply_fails_closed_when_another_process_holds_mutation_lock(self) -> None:
        desired, current = self.base_desired_current()
        adapter = FakeRekordboxAdapter(self.root / "adapter", current)
        adapter.hold_apply_event = threading.Event()
        adapter.release_apply_event = threading.Event()
        outcomes: list[dict] = []

        first = threading.Thread(
            target=lambda: outcomes.append(
                self.rb.sync_playlist("Playlist", desired, dry_run=False, adapter_factory=self.adapter_factory(adapter), confirmation_token=self.rb.APPLY_CONFIRMATION_TOKEN)
            )
        )
        first.start()
        self.assertTrue(adapter.hold_apply_event.wait(timeout=5))
        second = self.rb.sync_playlist("Playlist", desired, dry_run=False, adapter_factory=self.adapter_factory(adapter), confirmation_token=self.rb.APPLY_CONFIRMATION_TOKEN)
        adapter.release_apply_event.set()
        first.join(timeout=5)

        self.assertEqual("concurrent_apply", second["error"]["code"])
        self.assertEqual(1, adapter.begin_count)

    def test_apply_commits_once_closes_reopens_reconciles_exactly_then_calls_callback_once(self) -> None:
        desired, current = self.base_desired_current()
        adapter = FakeRekordboxAdapter(self.root / "adapter", current)
        callbacks = []

        result = self.rb.sync_playlist(
            "Playlist",
            desired,
            dry_run=False,
            adapter_factory=self.adapter_factory(adapter),
            confirmation_token=self.rb.APPLY_CONFIRMATION_TOKEN,
            on_reconciled=lambda payload: callbacks.append((list(adapter.events), payload["plan_hash"])),
        )

        self.assertTrue(result["applied"])
        self.assertTrue(result["reconciled"])
        self.assertEqual(1, adapter.begin_count)
        self.assertEqual(1, adapter.commit_count)
        self.assertFalse(adapter.used_internal_commit_helper)
        self.assertEqual(["snapshot", "begin", "apply", "external_save", "commit", "close", "reopen", "snapshot"], adapter.events)
        self.assertEqual(1, len(callbacks))
        self.assertIn("snapshot", callbacks[0][0][-1])

    def test_rollback_restores_snapshot_and_suppresses_callback_for_each_failure_surface(self) -> None:
        for fail_at, code in [
            ("apply", "adapter_apply_failed"),
            ("external", "external_save_failed"),
            ("commit", "commit_failed"),
            ("reopen", "reopen_failed"),
        ]:
            with self.subTest(fail_at=fail_at):
                desired, current = self.base_desired_current()
                adapter = FakeRekordboxAdapter(self.root / f"adapter-{fail_at}", current, fail_at=fail_at)
                callbacks = []

                result = self.rb.sync_playlist(
                    "Playlist",
                    desired,
                    dry_run=False,
                    adapter_factory=self.adapter_factory(adapter),
                    confirmation_token=self.rb.APPLY_CONFIRMATION_TOKEN,
                    on_reconciled=lambda _payload: callbacks.append("called"),
                )

                self.assertFalse(result["applied"])
                self.assertFalse(result["reconciled"])
                self.assertEqual(code, result["error"]["code"])
                self.assertNotRegex(json.dumps(result, ensure_ascii=False), r"[A-Z]:\\|secret|private")
                self.assertEqual([], callbacks)
                self.assertIn("restore", adapter.events)
                self.assertEqual(b"db-before", adapter.db_path.read_bytes())
                self.assertFalse(adapter.external_write_target.exists())

    def test_reconcile_failure_rolls_back_and_rollback_verify_failure_is_distinct(self) -> None:
        desired, current = self.base_desired_current()
        wrong = [dict(desired[0], title="Wrong")]
        adapter = FakeRekordboxAdapter(self.root / "adapter-reconcile", current, restored=wrong)

        result = self.rb.sync_playlist("Playlist", desired, dry_run=False, adapter_factory=self.adapter_factory(adapter), confirmation_token=self.rb.APPLY_CONFIRMATION_TOKEN)

        self.assertEqual("rollback_verify_failed", result["error"]["code"])
        self.assertFalse(result["applied"])
        self.assertFalse(result["reconciled"])

    def test_add_operation_preserves_non_empty_metadata_not_path_only(self) -> None:
        media = self.touch("new.flac")
        desired = [track("deezer:42", "T", "A", "B", 42, 1, media)]
        adapter = FakeRekordboxAdapter(self.root / "adapter-add", [])

        result = self.rb.sync_playlist("Playlist", desired, dry_run=False, adapter_factory=self.adapter_factory(adapter), confirmation_token=self.rb.APPLY_CONFIRMATION_TOKEN)

        self.assertTrue(result["reconciled"])
        self.assertEqual("T", result["plan"]["add"][0]["title"])
        self.assertEqual("A", result["plan"]["add"][0]["artist"])
        self.assertEqual("B", result["plan"]["add"][0]["album"])
        self.assertEqual(42, result["plan"]["add"][0]["duration"])


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

    def test_env_and_confirmation_token_gate_matrix(self) -> None:
        body = self.main.RbSyncIn(playlist_key="local:fixture", playlist_title="Fixture")
        observed = []
        for env_value, token, expected_apply in [
            (None, None, False),
            ("1", None, False),
            (None, self.rb.APPLY_CONFIRMATION_TOKEN, False),
            ("1", self.rb.APPLY_CONFIRMATION_TOKEN, True),
        ]:
            if env_value is None:
                os.environ.pop("DECKPIPE_RB_EXPERIMENTAL", None)
            else:
                os.environ["DECKPIPE_RB_EXPERIMENTAL"] = env_value
            with patch.object(self.main, "_desired_tracks_for_rekordbox", return_value=[]), patch.object(
                self.main.rb,
                "sync_playlist",
                return_value={"dry_run": not expected_apply, "applied": expected_apply, "reconciled": expected_apply, "unresolved": [], "backup_id": None, "error": None, "plan": {}},
            ) as sync:
                result = self.main.api_rb_sync(body, dry_run=False, confirmation_token=token)
            observed.append(result["applied"])
            self.assertEqual(expected_apply, sync.call_args.kwargs["dry_run"] is False)
        self.assertEqual([False, False, False, True], observed)

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

    def test_powershell_unicode_runner_requires_explicit_database_path_and_has_no_live_default(self) -> None:
        runner = Path("qa/run-rekordbox-unicode-audit.ps1")
        text = runner.read_text(encoding="utf-8")
        self.assertNotIn("APPDATA", text.upper())
        self.assertRegex(text, r"DatabasePath\s*=\s*''")
        self.assertRegex(text, r"DisposableCopy")
        self.assertNotRegex(text, r"&\s*\$PythonPath\s+\$auditPath\s+\$source\b")


if __name__ == "__main__":
    unittest.main(verbosity=2)
