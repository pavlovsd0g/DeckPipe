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


class RekordboxSyncCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        from app import rekordbox

        self.old_experimental = os.environ.get("DECKPIPE_RB_EXPERIMENTAL")
        os.environ["DECKPIPE_RB_EXPERIMENTAL"] = "1"
        self.rb = importlib.reload(rekordbox)
        self.tempdir = tempfile.TemporaryDirectory(prefix="deckpipe-rb-apply-")
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()
        if self.old_experimental is None:
            os.environ.pop("DECKPIPE_RB_EXPERIMENTAL", None)
        else:
            os.environ["DECKPIPE_RB_EXPERIMENTAL"] = self.old_experimental

    def touch(self, name: str, content: bytes = b"media") -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def adapter_factory(self, adapter: FakeRekordboxAdapter):
        return lambda: adapter.reopen() if adapter.closed else adapter

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
        self.assertEqual(1, adapter.close_count)
        self.assertEqual([], calls)
        self.assertFalse((adapter.db_path.parent / "deckpipe-rekordbox-backups").exists())

    def test_direct_sync_requires_env_flag_and_confirmation_token_before_mutation(self) -> None:
        desired, current = self.base_desired_current()
        observed = []
        for env_value, token, expected_code in [
            (None, None, "apply_not_confirmed"),
            ("1", None, "apply_not_confirmed"),
            (None, self.rb.APPLY_CONFIRMATION_TOKEN, "apply_not_confirmed"),
            ("1", self.rb.APPLY_CONFIRMATION_TOKEN, None),
        ]:
            with self.subTest(env_value=env_value, token=token):
                if env_value is None:
                    os.environ.pop("DECKPIPE_RB_EXPERIMENTAL", None)
                else:
                    os.environ["DECKPIPE_RB_EXPERIMENTAL"] = env_value
                adapter = FakeRekordboxAdapter(self.root / f"adapter-auth-{len(observed)}", current)

                result = self.rb.sync_playlist("Playlist", desired, dry_run=False, adapter_factory=self.adapter_factory(adapter), confirmation_token=token)

                observed.append(result["applied"])
                self.assertEqual(expected_code, result["error"]["code"] if result["error"] else None)
                self.assertEqual(0 if expected_code else 1, adapter.begin_count)
                self.assertEqual(0 if expected_code else 1, adapter.close_count)
        self.assertEqual([False, False, False, True], observed)
        os.environ["DECKPIPE_RB_EXPERIMENTAL"] = "1"

    def test_unauthorized_apply_returns_before_constructing_adapter(self) -> None:
        desired, _current = self.base_desired_current()
        os.environ.pop("DECKPIPE_RB_EXPERIMENTAL", None)
        calls = []

        def forbidden_factory():
            calls.append("called")
            return FakeRekordboxAdapter(self.root / "adapter-unauthorized", [])

        result = self.rb.sync_playlist("Playlist", desired, dry_run=False, adapter_factory=forbidden_factory, confirmation_token=self.rb.APPLY_CONFIRMATION_TOKEN)

        self.assertEqual([], calls)
        self.assertFalse(result["dry_run"])
        self.assertEqual("apply_not_confirmed", result["error"]["code"])
        self.assertNotRegex(json.dumps(result, ensure_ascii=False), r"[A-Z]:\\|secret|private")
        os.environ["DECKPIPE_RB_EXPERIMENTAL"] = "1"

    def test_adapter_open_failures_are_structured_for_dry_run_and_apply_without_raw_paths(self) -> None:
        desired, _current = self.base_desired_current()

        def failing_factory():
            raise RuntimeError("adapter open leaked C:\\Users\\secret\\master.db")

        for dry_run, token in [(True, None), (False, self.rb.APPLY_CONFIRMATION_TOKEN)]:
            with self.subTest(dry_run=dry_run):
                try:
                    result = self.rb.sync_playlist("Playlist", desired, dry_run=dry_run, adapter_factory=failing_factory, confirmation_token=token)
                except RuntimeError as exc:
                    self.fail(f"sync_playlist leaked adapter-open exception: {exc}")

                self.assertEqual(dry_run, result["dry_run"])
                self.assertFalse(result["applied"])
                self.assertFalse(result["reconciled"])
                self.assertEqual("adapter_open_failed", result["error"]["code"])
                self.assertNotRegex(json.dumps(result, ensure_ascii=False), r"[A-Z]:\\|secret|private")

    def test_apply_acquires_lock_before_authoritative_snapshot_and_status_backup(self) -> None:
        desired, current = self.base_desired_current()
        adapter = FakeRekordboxAdapter(self.root / "adapter-lock-order", current, shared={"assert_lock_exists_on_snapshot": True})

        result = self.rb.sync_playlist("Playlist", desired, dry_run=False, adapter_factory=self.adapter_factory(adapter), confirmation_token=self.rb.APPLY_CONFIRMATION_TOKEN)

        self.assertTrue(result["reconciled"])
        self.assertEqual(["snapshot", "playlist_exists", "begin"], [event for event in adapter.events if event in {"snapshot", "playlist_exists", "begin"}][:3])
        self.assertTrue(adapter.events.index("snapshot") < adapter.events.index("begin"))

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
        self.assertEqual(["snapshot", "playlist_exists", "begin", "apply", "external_save", "commit", "close", "reopen", "snapshot", "close"], adapter.events)
        self.assertEqual(1, len(callbacks))
        self.assertEqual("close", callbacks[0][0][-1])

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
                self.assertGreaterEqual(adapter.events.count("close"), 2)
                self.assertEqual(b"db-before", adapter.db_path.read_bytes())
                self.assertFalse(adapter.external_write_target.exists())

    def test_reconcile_failure_rolls_back_then_verifies_restored_snapshot_with_fresh_handle(self) -> None:
        desired, current = self.base_desired_current()
        shared = {"reopen_snapshots": [[dict(desired[0], title="Wrong")], current]}
        adapter = FakeRekordboxAdapter(self.root / "adapter-reconcile", current, shared=shared)

        result = self.rb.sync_playlist("Playlist", desired, dry_run=False, adapter_factory=self.adapter_factory(adapter), confirmation_token=self.rb.APPLY_CONFIRMATION_TOKEN)

        self.assertEqual("reconcile_failed", result["error"]["code"])
        self.assertFalse(result["applied"])
        self.assertFalse(result["reconciled"])
        self.assertEqual(["snapshot", "playlist_exists", "begin", "apply", "external_save", "commit", "close", "reopen", "snapshot", "close", "reopen", "snapshot", "close"], adapter.events)

    def test_rollback_verification_failure_is_distinct_after_fresh_handle_restore_check(self) -> None:
        desired, current = self.base_desired_current()
        shared = {"reopen_snapshots": [[dict(desired[0], title="Wrong")]]}
        adapter = FakeRekordboxAdapter(self.root / "adapter-reconcile-bad-restore", current, shared=shared)

        with patch.object(self.rb, "_restore_backup_files", side_effect=RuntimeError("restore failed at C:\\Users\\secret\\master.db")):
            result = self.rb.sync_playlist("Playlist", desired, dry_run=False, adapter_factory=self.adapter_factory(adapter), confirmation_token=self.rb.APPLY_CONFIRMATION_TOKEN)

        self.assertEqual("rollback_verify_failed", result["error"]["code"])
        self.assertFalse(result["applied"])
        self.assertFalse(result["reconciled"])
        self.assertNotRegex(json.dumps(result, ensure_ascii=False), r"[A-Z]:\\|secret|private")

    def test_rollback_restores_files_while_all_adapter_handles_are_closed_then_verifies_once(self) -> None:
        desired, current = self.base_desired_current()
        shared = {"open_handle_counts_during_restore": []}
        adapter = FakeRekordboxAdapter(self.root / "adapter-handle-free-restore", current, fail_at="external", shared=shared)

        def observed_restore(backup):
            shared["open_handle_counts_during_restore"].append(shared["open_handles"])
            Path(backup["files"]["database"]["path"]).write_bytes(Path(backup["files"]["database"]["backup"]).read_bytes())
            for item in backup["files"]["external"]:
                original = Path(item["path"])
                if item["existed"]:
                    original.write_bytes(Path(item["backup"]).read_bytes())
                else:
                    original.unlink(missing_ok=True)

        with patch.object(self.rb, "_restore_backup_files", side_effect=observed_restore, create=True):
            result = self.rb.sync_playlist("Playlist", desired, dry_run=False, adapter_factory=self.adapter_factory(adapter), confirmation_token=self.rb.APPLY_CONFIRMATION_TOKEN)

        self.assertEqual("external_save_failed", result["error"]["code"])
        self.assertEqual([0], shared["open_handle_counts_during_restore"])
        self.assertTrue(all(handle.closed for handle in shared["handles"]))
        self.assertEqual(2, shared["events"].count("snapshot"))
        self.assertEqual(2, len(shared["handles"]))
        self.assertEqual(b"db-before", adapter.db_path.read_bytes())
        self.assertFalse(adapter.external_write_target.exists())

    def test_rollback_verification_accepts_identical_snapshots_with_missing_paths(self) -> None:
        missing = self.root / "missing-after-rollback.flac"
        current = [track("deezer:missing", "Missing", "Artist", "Album", 12, 1, missing)]
        desired = [track("deezer:missing", "Changed", "Artist", "Album", 12, 1, self.touch("present.flac"))]
        adapter = FakeRekordboxAdapter(self.root / "adapter-missing-rollback", current, fail_at="external")

        result = self.rb.sync_playlist("Playlist", desired, dry_run=False, adapter_factory=self.adapter_factory(adapter), confirmation_token=self.rb.APPLY_CONFIRMATION_TOKEN)

        self.assertEqual("external_save_failed", result["error"]["code"])

    def test_apply_structures_adapter_lifecycle_failures_and_closes_handles(self) -> None:
        desired, current = self.base_desired_current()
        cases = [
            ({"fail_running_check": True}, None, "rekordbox_status_failed"),
            ({"fail_lock_path": True}, None, "lock_failed"),
            ({}, "begin", "begin_failed"),
        ]
        for shared_seed, fail_at, code in cases:
            with self.subTest(code=code):
                shared = dict(shared_seed)
                adapter = FakeRekordboxAdapter(self.root / f"adapter-{code}", current, fail_at=fail_at, shared=shared)

                try:
                    result = self.rb.sync_playlist("Playlist", desired, dry_run=False, adapter_factory=self.adapter_factory(adapter), confirmation_token=self.rb.APPLY_CONFIRMATION_TOKEN)
                except RuntimeError as exc:
                    self.fail(f"sync_playlist leaked lifecycle exception: {exc}")

                self.assertEqual(code, result["error"]["code"])
                self.assertFalse(result["applied"])
                self.assertFalse(result["reconciled"])
                self.assertNotRegex(json.dumps(result, ensure_ascii=False), r"[A-Z]:\\|secret|private")
                self.assertTrue(all(handle.closed for handle in shared["handles"]))

    def test_lock_write_failure_returns_structured_failure_and_removes_owned_lock(self) -> None:
        desired, current = self.base_desired_current()
        adapter = FakeRekordboxAdapter(self.root / "adapter-lock-write-fail", current)
        opened_fds: list[int] = []
        closed_fds: list[int] = []
        real_open = self.rb.os.open
        real_close = self.rb.os.close

        def tracking_open(path, flags, mode=0o777):
            fd = real_open(path, flags, mode)
            opened_fds.append(fd)
            return fd

        def tracking_close(fd):
            closed_fds.append(fd)
            return real_close(fd)

        with (
            patch.object(self.rb.os, "open", side_effect=tracking_open),
            patch.object(self.rb.os, "close", side_effect=tracking_close),
            patch.object(self.rb.os, "write", side_effect=OSError("write leaked C:\\Users\\secret\\mutation.lock")),
        ):
            try:
                result = self.rb.sync_playlist("Playlist", desired, dry_run=False, adapter_factory=self.adapter_factory(adapter), confirmation_token=self.rb.APPLY_CONFIRMATION_TOKEN)
            except OSError as exc:
                self.fail(f"sync_playlist leaked lock exception: {exc}")

        self.assertEqual("lock_failed", result["error"]["code"])
        self.assertFalse(result["applied"])
        self.assertTrue(adapter.closed)
        self.assertEqual(opened_fds, closed_fds)
        self.assertFalse(adapter.lock_path.exists())
        self.assertNotRegex(json.dumps(result, ensure_ascii=False), r"[A-Z]:\\|secret|private")

    def test_lock_mkdir_open_and_context_enter_failures_are_structured_and_close_adapter(self) -> None:
        desired, current = self.base_desired_current()

        cases = [
            ("mkdir", lambda: patch.object(self.rb.Path, "mkdir", side_effect=OSError("mkdir leaked C:\\Users\\secret\\mutation.lock"))),
            ("open", lambda: patch.object(self.rb.os, "open", side_effect=OSError("open leaked C:\\Users\\secret\\mutation.lock"))),
            ("enter", None),
        ]

        for name, patch_factory in cases:
            with self.subTest(name=name):
                adapter = FakeRekordboxAdapter(self.root / f"adapter-lock-{name}-fail", current)
                if name == "enter":
                    class BrokenLock:
                        def __init__(self):
                            self.exited = False

                        def __enter__(self):
                            raise OSError("enter leaked C:\\Users\\secret\\mutation.lock")

                        def __exit__(self, *_args):
                            self.exited = True

                    lock = BrokenLock()
                    patcher = patch.object(self.rb, "_exclusive_create_lock", return_value=lock)
                else:
                    lock = None
                    patcher = patch_factory()

                with patcher:
                    try:
                        result = self.rb.sync_playlist(
                            "Playlist",
                            desired,
                            dry_run=False,
                            adapter_factory=self.adapter_factory(adapter),
                            confirmation_token=self.rb.APPLY_CONFIRMATION_TOKEN,
                        )
                    except OSError as exc:
                        self.fail(f"sync_playlist leaked lock exception: {exc}")

                self.assertEqual("lock_failed", result["error"]["code"])
                self.assertFalse(result["applied"])
                self.assertTrue(adapter.closed)
                self.assertFalse(adapter.lock_path.exists())
                self.assertNotRegex(json.dumps(result, ensure_ascii=False), r"[A-Z]:\\|secret|private")
                if lock is not None:
                    self.assertFalse(lock.exited)

    def test_missing_playlist_default_creates_inside_transaction_false_fails_without_mutation(self) -> None:
        desired, _current = self.base_desired_current()
        create_shared = {"playlist_exists": False}
        create_adapter = FakeRekordboxAdapter(self.root / "adapter-create-playlist", [], shared=create_shared)

        created = self.rb.sync_playlist("Missing Playlist", desired, dry_run=False, adapter_factory=self.adapter_factory(create_adapter), confirmation_token=self.rb.APPLY_CONFIRMATION_TOKEN)

        self.assertTrue(created["reconciled"])
        self.assertLess(create_adapter.events.index("begin"), create_adapter.events.index("create_playlist"))
        self.assertLess(create_adapter.events.index("create_playlist"), create_adapter.events.index("commit"))

        fail_shared = {"playlist_exists": False}
        fail_adapter = FakeRekordboxAdapter(self.root / "adapter-no-create-playlist", [], shared=fail_shared)

        failed = self.rb.sync_playlist("Missing Playlist", desired, create_missing=False, dry_run=False, adapter_factory=self.adapter_factory(fail_adapter), confirmation_token=self.rb.APPLY_CONFIRMATION_TOKEN)

        self.assertEqual("playlist_not_found", failed["error"]["code"])
        self.assertEqual(0, fail_adapter.begin_count)
        self.assertNotIn("apply", fail_adapter.events)
        self.assertFalse(fail_shared["playlist_exists"])

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

    def test_backup_and_callback_failures_are_structured_and_sanitized(self) -> None:
        desired, current = self.base_desired_current()
        backup_adapter = FakeRekordboxAdapter(self.root / "adapter-backup", current)
        backup_adapter.db_path.unlink()

        backup_result = self.rb.sync_playlist("Playlist", desired, dry_run=False, adapter_factory=self.adapter_factory(backup_adapter), confirmation_token=self.rb.APPLY_CONFIRMATION_TOKEN)

        self.assertEqual("backup_failed", backup_result["error"]["code"])
        self.assertNotRegex(json.dumps(backup_result, ensure_ascii=False), r"[A-Z]:\\|secret|private")
        self.assertEqual(1, backup_adapter.close_count)

        callback_adapter = FakeRekordboxAdapter(self.root / "adapter-callback", current)
        calls = []

        def failing_callback(_payload):
            calls.append("called")
            raise RuntimeError("callback leaked C:\\Users\\secret\\sidecar.json")

        callback_result = self.rb.sync_playlist("Playlist", desired, dry_run=False, adapter_factory=self.adapter_factory(callback_adapter), confirmation_token=self.rb.APPLY_CONFIRMATION_TOKEN, on_reconciled=failing_callback)

        self.assertTrue(callback_result["applied"])
        self.assertTrue(callback_result["reconciled"])
        self.assertEqual("callback_failed", callback_result["error"]["code"])
        self.assertEqual(["called"], calls)
        self.assertNotRegex(json.dumps(callback_result, ensure_ascii=False), r"[A-Z]:\\|secret|private")


class RekordboxAdapterMutationContractTests(unittest.TestCase):
    def setUp(self) -> None:
        from app import rekordbox

        self.rb = importlib.reload(rekordbox)
        self.tempdir = tempfile.TemporaryDirectory(prefix="deckpipe-rb-adapter-")
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_apply_operations_uses_direct_session_primitives_and_stages_anlz_without_implicit_commit_helpers(self) -> None:
        playlist = SimpleNamespace(ID="pl1", Name="Playlist", Attribute=0)
        keep = SimpleNamespace(ID="rb-keep", Commnt="deezer:keep", Title="Keep Old", Artist=SimpleNamespace(Name="Old Artist"), Album=SimpleNamespace(Name="Old Album"), Length=111, FolderPath="C:/old/keep.flac", OrgFolderPath="C:/old/keep.flac", FileNameL="keep.flac")
        add = SimpleNamespace(ID="rb-add", Commnt="deezer:add", Title="Add Old", Artist=SimpleNamespace(Name="Old Add"), Album=SimpleNamespace(Name="Old Add Album"), Length=50, FolderPath="C:/old/add.flac", OrgFolderPath="C:/old/add.flac", FileNameL="add.flac")
        remove = SimpleNamespace(ID="rb-remove", Commnt="deezer:remove", Title="Remove", Artist=SimpleNamespace(Name="R"), Album=SimpleNamespace(Name="R"), Length=60, FolderPath="C:/old/remove.flac", OrgFolderPath="C:/old/remove.flac", FileNameL="remove.flac")
        songs = [
            SimpleNamespace(ID="song-keep", PlaylistID="pl1", ContentID="rb-keep", TrackNo=2, Content=keep),
            SimpleNamespace(ID="song-remove", PlaylistID="pl1", ContentID="rb-remove", TrackNo=1, Content=remove),
        ]

        class Query(list):
            def filter_by(self, **kwargs):
                return Query([item for item in self if all(getattr(item, key) == value for key, value in kwargs.items())])

            def order_by(self, _field):
                return Query(sorted(self, key=lambda item: getattr(item, "TrackNo", 0)))

            def one(self):
                if len(self) != 1:
                    raise AssertionError("expected one item")
                return self[0]

            def count(self):
                return len(self)

        class FakeRegistry:
            def on_create(self, _instance):
                pass

            def on_delete(self, _instance):
                pass

            def on_move(self, _items):
                pass

        class FakeDb:
            def __init__(self):
                self.registry = FakeRegistry()
                self.added = []
                self.deleted = []
                self.commits = 0
                self.used_forbidden_helper = False
                self.artists = []
                self.albums = []

            def get_playlist(self):
                return [playlist]

            def get_content(self):
                return [keep, add, remove]

            def get_playlist_songs(self, **kwargs):
                self._assert_playlist(kwargs)
                return Query(songs)

            def get_artist(self, **kwargs):
                return Query(self.artists).filter_by(**kwargs)

            def get_album(self, **kwargs):
                return Query(self.albums).filter_by(**kwargs)

            def add_artist(self, name, **_kwargs):
                artist = SimpleNamespace(ID=f"artist-{len(self.artists) + 1}", Name=name)
                self.artists.append(artist)
                return artist

            def add_album(self, name, artist=None, **_kwargs):
                if artist is None:
                    raise AssertionError("add_album must receive the resolved artist")
                artist_id = artist if isinstance(artist, str) else artist.ID
                album = SimpleNamespace(ID=f"album-{len(self.albums) + 1}", Name=name, AlbumArtistID=artist_id)
                self.albums.append(album)
                return album

            def read_anlz_files(self, content):
                return {Path(f"C:/anlz/{content.ID}.DAT"): SimpleNamespace(set_path=lambda value: self._stage_path(content, value), save=lambda _path: (_ for _ in ()).throw(AssertionError("ANLZ saved during apply")))}

            def _stage_path(self, content, value):
                content.staged_path = value

            def add(self, instance):
                self.added.append(instance)

            def delete(self, instance):
                self.deleted.append(instance)

            def commit(self):
                self.commits += 1

            def remove_from_playlist(self, *_args, **_kwargs):
                self.used_forbidden_helper = True
                raise AssertionError("remove_from_playlist commits internally")

            def update_content_path(self, *_args, **_kwargs):
                self.used_forbidden_helper = True
                raise AssertionError("update_content_path must not be used")

            def _assert_playlist(self, kwargs):
                if kwargs.get("PlaylistID") != "pl1":
                    raise AssertionError("wrong playlist")

        adapter = self.rb._PyrekordboxAdapter.__new__(self.rb._PyrekordboxAdapter)
        adapter.db = FakeDb()
        adapter.db_path = Path("C:/fake/master.db")
        adapter._staged_anlz = {}
        media_keep = self.root / "keep.wav"
        media_keep.write_bytes(b"keep")
        media_add = self.root / "add.wav"
        media_add.write_bytes(b"add")
        plan = self.rb.plan_playlist_sync(
            [
                track("deezer:keep", "Keep New", "Artist New", "Album New", 222, 1, media_keep),
                track("deezer:add", "Add New", "Artist Add", "Album Add", 333, 2, media_add),
            ],
            [
                track("deezer:remove", "Remove", "R", "R", 60, 1, Path("C:/old/remove.flac"), rb_id="rb-remove"),
                track("deezer:keep", "Keep Old", "Old Artist", "Old Album", 111, 2, Path("C:/old/keep.flac"), rb_id="rb-keep"),
            ],
        )

        adapter.apply_operations(plan)

        self.assertFalse(adapter.db.used_forbidden_helper)
        self.assertEqual(0, adapter.db.commits)
        self.assertEqual(["song-remove", "song-keep"], [item.ID for item in adapter.db.deleted])
        self.assertEqual(["rb-keep", "rb-add"], [item.ContentID for item in adapter.db.added])
        self.assertEqual([1, 2], [item.TrackNo for item in adapter.db.added])
        self.assertEqual("Keep New", keep.Title)
        self.assertEqual("Old Artist", keep.Artist.Name)
        self.assertEqual("Old Album", keep.Album.Name)
        self.assertEqual(222, keep.Length)
        self.assertEqual("deezer:keep", keep.Commnt)
        self.assertEqual(str(media_keep), keep.FolderPath)
        self.assertEqual("keep.wav", keep.FileNameL)
        self.assertEqual([Path("C:/anlz/rb-add.DAT"), Path("C:/anlz/rb-keep.DAT")], sorted(adapter._staged_anlz))

    def test_snapshot_playlist_orders_real_playlist_songs_by_track_number(self) -> None:
        playlist = SimpleNamespace(ID="pl1", Name="Playlist", Attribute=0)
        one = SimpleNamespace(ID="rb-one", Commnt="deezer:1", Title="One", Artist=SimpleNamespace(Name="A"), Album=SimpleNamespace(Name="B"), Length=1, FolderPath="C:/one.flac")
        two = SimpleNamespace(ID="rb-two", Commnt="deezer:2", Title="Two", Artist=SimpleNamespace(Name="A"), Album=SimpleNamespace(Name="B"), Length=2, FolderPath="C:/two.flac")
        songs = [
            SimpleNamespace(ContentID="rb-two", TrackNo=2),
            SimpleNamespace(ContentID="rb-one", TrackNo=1),
        ]

        class FakeDb:
            def get_content(self):
                return [one, two]

            def get_playlist(self):
                return [playlist]

            def get_playlist_songs(self, **_kwargs):
                return list(songs)

        adapter = self.rb._PyrekordboxAdapter.__new__(self.rb._PyrekordboxAdapter)
        adapter.db = FakeDb()

        snapshot = adapter.snapshot_playlist("Playlist")

        self.assertEqual(["deezer:1", "deezer:2"], [item["provider_id"] for item in snapshot])
        self.assertEqual([1, 2], [item["position"] for item in snapshot])

    def test_apply_operations_resolves_artist_album_ids_and_uses_canonical_add_content_payload(self) -> None:
        class RelationshipGuard:
            def __setattr__(self, key, value):
                if key in {"Artist", "Album"} and isinstance(value, str):
                    raise AssertionError(f"assigned string to relationship {key}")
                super().__setattr__(key, value)

        playlist = SimpleNamespace(ID="pl1", Name="Playlist", Attribute=0)
        old_artist = SimpleNamespace(ID="artist-old", Name="Old Artist")
        old_album = SimpleNamespace(ID="album-old", Name="Old Album")
        sibling = RelationshipGuard()
        sibling.ID = "rb-sibling"
        sibling.Commnt = "deezer:sibling"
        sibling.Title = "Sibling"
        sibling.Artist = old_artist
        sibling.ArtistID = old_artist.ID
        sibling.Album = old_album
        sibling.AlbumID = old_album.ID
        sibling.Length = 111
        sibling.FolderPath = "C:/old/sibling.flac"
        sibling.OrgFolderPath = "C:/old/sibling.flac"
        sibling.FileNameL = "sibling.flac"
        sibling.FileSize = 7
        sibling.FileType = 5
        keep = RelationshipGuard()
        keep.ID = "rb-keep"
        keep.Commnt = "deezer:keep"
        keep.Title = "Keep Old"
        keep.Artist = old_artist
        keep.ArtistID = old_artist.ID
        keep.Album = old_album
        keep.AlbumID = old_album.ID
        keep.Length = 222
        keep.FolderPath = "C:/old/keep.flac"
        keep.OrgFolderPath = "C:/old/keep.flac"
        keep.FileNameL = "keep.flac"
        keep.FileNameS = "keep.flac"
        keep.FileSize = 4
        keep.FileType = 5
        songs = [SimpleNamespace(ID="song-keep", PlaylistID="pl1", ContentID="rb-keep", TrackNo=1, Content=keep)]

        class Query(list):
            def filter_by(self, **kwargs):
                return Query([item for item in self if all(getattr(item, key) == value for key, value in kwargs.items())])

            def one_or_none(self):
                if len(self) > 1:
                    raise AssertionError("expected one or none")
                return self[0] if self else None

            def count(self):
                return len(self)

        class FakeDb:
            def __init__(self):
                self.artists = [old_artist]
                self.albums = [old_album]
                self.contents = [sibling, keep]
                self.add_content_calls = []
                self.commits = 0

            def get_playlist(self):
                return [playlist]

            def get_playlist_songs(self, **_kwargs):
                return list(songs)

            def get_content(self):
                return list(self.contents)

            def get_artist(self, **kwargs):
                return Query(self.artists).filter_by(**kwargs)

            def get_album(self, **kwargs):
                return Query(self.albums).filter_by(**kwargs)

            def add_artist(self, name, **_kwargs):
                artist = SimpleNamespace(ID=f"artist-{len(self.artists) + 1}", Name=name)
                self.artists.append(artist)
                return artist

            def add_album(self, name, artist=None, **_kwargs):
                if artist is None:
                    raise AssertionError("add_album must receive the resolved artist")
                artist_id = artist if isinstance(artist, str) else artist.ID
                album = SimpleNamespace(ID=f"album-{len(self.albums) + 1}", Name=name, AlbumArtistID=artist_id)
                self.albums.append(album)
                return album

            def add_content(self, path, **kwargs):
                self.add_content_calls.append((Path(path), dict(kwargs)))
                content = RelationshipGuard()
                content.ID = "rb-added"
                content.FolderPath = str(path)
                content.OrgFolderPath = str(path)
                content.FileNameL = Path(path).name
                content.FileNameS = Path(path).name
                content.FileSize = Path(path).stat().st_size
                content.FileType = 11
                content.Artist = next(item for item in self.artists if item.ID == kwargs["ArtistID"])
                content.ArtistID = kwargs["ArtistID"]
                content.Album = next(item for item in self.albums if item.ID == kwargs["AlbumID"])
                content.AlbumID = kwargs["AlbumID"]
                content.Title = kwargs["Title"]
                content.Commnt = kwargs["Commnt"]
                content.Length = kwargs["Length"]
                self.contents.append(content)
                return content

            def generate_unused_id(self, *_args, **_kwargs):
                return "legacy-created"

            def read_anlz_files(self, content):
                return {Path(f"C:/anlz/{content.ID}.DAT"): SimpleNamespace(set_path=lambda value: setattr(content, "staged_path", value), save=lambda _path: None)}

            def add(self, _instance):
                pass

            def delete(self, _instance):
                pass

            def commit(self):
                self.commits += 1

            def remove_from_playlist(self, *_args, **_kwargs):
                raise AssertionError("remove_from_playlist commits internally")

            def update_content_path(self, *_args, **_kwargs):
                raise AssertionError("update_content_path must not be used")

        adapter = self.rb._PyrekordboxAdapter.__new__(self.rb._PyrekordboxAdapter)
        adapter.db = FakeDb()
        adapter.db_path = Path("C:/fake/master.db")
        adapter._staged_anlz = {}
        keep_media = self.root / "keep.wav"
        keep_media.write_bytes(b"keep-new")
        add_media = self.root / "add.wav"
        add_media.write_bytes(b"added")
        plan = {
            "_playlist_name": "Playlist",
            "_create_missing": True,
            "desired_resolved": [
                track("deezer:keep", "Keep New", "Artist New", "Album New", 333, 1, keep_media),
                track("deezer:add", "Add New", "Artist Add", "Album Add", 444, 2, add_media),
            ],
        }

        adapter.apply_operations(plan)

        self.assertEqual("Old Artist", old_artist.Name)
        self.assertEqual("Old Album", old_album.Name)
        self.assertEqual("artist-2", keep.ArtistID)
        self.assertEqual("album-2", keep.AlbumID)
        self.assertEqual("artist-old", sibling.ArtistID)
        self.assertEqual("album-old", sibling.AlbumID)
        self.assertEqual("Old Artist", sibling.Artist.Name)
        self.assertEqual("Old Album", sibling.Album.Name)
        self.assertEqual(str(keep_media), keep.FolderPath)
        self.assertEqual("keep.wav", keep.FileNameL)
        self.assertEqual("keep.wav", keep.FileNameS)
        self.assertEqual(keep_media.stat().st_size, keep.FileSize)
        self.assertEqual(11, keep.FileType)
        self.assertEqual("deezer:keep", keep.Commnt)
        self.assertEqual(333, keep.Length)
        self.assertEqual([(add_media, {"Title": "Add New", "ArtistID": "artist-3", "AlbumID": "album-3", "Commnt": "deezer:add", "Length": 444})], adapter.db.add_content_calls)
        self.assertEqual([Path("C:/anlz/rb-keep.DAT")], sorted(adapter._staged_anlz))
        self.assertEqual(0, adapter.db.commits)

    def test_apply_operations_uses_non_null_empty_artist_album_rows_and_preserves_empty_snapshot_strings(self) -> None:
        playlist = SimpleNamespace(ID="pl1", Name="Playlist", Attribute=0)

        class Query(list):
            def filter_by(self, **kwargs):
                return Query([item for item in self if all(getattr(item, key) == value for key, value in kwargs.items())])

            def one_or_none(self):
                if len(self) > 1:
                    raise AssertionError("expected one or none")
                return self[0] if self else None

        class StrictMetadataDb:
            def __init__(self):
                self.artists = []
                self.albums = []
                self.contents = []
                self.songs = []
                self.add_content_calls = []

            def get_playlist(self):
                return [playlist]

            def get_playlist_songs(self, **_kwargs):
                return list(self.songs)

            def get_content(self):
                return list(self.contents)

            def get_artist(self, **kwargs):
                return Query(self.artists).filter_by(**kwargs)

            def get_album(self, **kwargs):
                return Query(self.albums).filter_by(**kwargs)

            def add_artist(self, name, **_kwargs):
                artist = SimpleNamespace(ID=f"artist-{len(self.artists) + 1}", Name=name)
                self.artists.append(artist)
                return artist

            def add_album(self, name, artist=None, **_kwargs):
                if artist is None:
                    raise AssertionError("add_album must receive the resolved artist")
                artist_id = artist if isinstance(artist, str) else artist.ID
                album = SimpleNamespace(ID=f"album-{len(self.albums) + 1}", Name=name, AlbumArtistID=artist_id)
                self.albums.append(album)
                return album

            def add_content(self, path, **kwargs):
                if kwargs.get("ArtistID") is None or kwargs.get("AlbumID") is None:
                    raise AssertionError("content IDs must be non-null")
                self.add_content_calls.append((Path(path), dict(kwargs)))
                artist = next(item for item in self.artists if item.ID == kwargs["ArtistID"])
                album = next(item for item in self.albums if item.ID == kwargs["AlbumID"])
                content = SimpleNamespace(
                    ID=f"content-{len(self.contents) + 1}",
                    FolderPath=str(path),
                    OrgFolderPath=str(path),
                    FileNameL=Path(path).name,
                    FileNameS=Path(path).name,
                    FileSize=Path(path).stat().st_size,
                    FileType=11,
                    Artist=artist,
                    ArtistID=artist.ID,
                    Album=album,
                    AlbumID=album.ID,
                    Title=kwargs["Title"],
                    Commnt=kwargs["Commnt"],
                    Length=kwargs["Length"],
                )
                self.contents.append(content)
                return content

            def read_anlz_files(self, _content):
                return {}

            def delete(self, _instance):
                pass

            def add(self, instance):
                self.songs.append(instance)

        adapter = self.rb._PyrekordboxAdapter.__new__(self.rb._PyrekordboxAdapter)
        adapter.db = StrictMetadataDb()
        adapter.db_path = Path("C:/fake/master.db")
        adapter._staged_anlz = {}
        media = self.root / "empty.wav"
        media.write_bytes(b"empty")
        plan = {
            "_playlist_name": "Playlist",
            "_create_missing": True,
            "desired_resolved": [track("deezer:empty", "", "", "", 0, 1, media)],
        }

        adapter.apply_operations(plan)
        snapshot = adapter.snapshot_playlist("Playlist")

        self.assertEqual("", snapshot[0]["title"])
        self.assertEqual("", snapshot[0]["artist"])
        self.assertEqual("", snapshot[0]["album"])
        self.assertEqual("artist-1", adapter.db.add_content_calls[0][1]["ArtistID"])
        self.assertEqual("album-1", adapter.db.add_content_calls[0][1]["AlbumID"])
        self.assertEqual("artist-1", adapter.db.albums[0].AlbumArtistID)


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
