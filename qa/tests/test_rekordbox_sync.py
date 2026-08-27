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
        self._shared = shared or {}
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
        self._shared.setdefault("reopen_snapshots", [])
        self._shared.setdefault("fail_fresh_verify", False)
        self._shared.setdefault("fail_restore", False)
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

    def mutation_lock_path(self) -> Path:
        return self.lock_path

    def is_rekordbox_running(self) -> bool:
        return False

    def external_files_for_playlist(self, _playlist_name: str, _desired: list[dict]) -> list[Path]:
        return [self.anlz_path, self.xml_path, self.external_write_target]

    def snapshot_playlist(self, _playlist_name: str) -> list[dict]:
        if self.closed:
            raise RuntimeError("closed handle snapshot at C:\\Users\\secret\\master.db")
        self.events.append("snapshot")
        if self._shared.get("fail_fresh_verify"):
            raise RuntimeError("verify failed at D:\\private\\master.db")
        return [dict(item) for item in self._shared["snapshot"]]

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

    def close(self) -> None:
        self.close_count += 1
        self.events.append("close")
        self.closed = True

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
                self.assertGreaterEqual(adapter.close_count, 1)
        self.assertEqual([False, False, False, True], observed)
        os.environ["DECKPIPE_RB_EXPERIMENTAL"] = "1"

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
        self.assertEqual(["snapshot", "begin", "apply", "external_save", "commit", "close", "reopen", "snapshot", "close"], adapter.events)
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
                self.assertIn("restore", adapter.events)
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
        self.assertEqual(["snapshot", "begin", "apply", "external_save", "commit", "close", "reopen", "snapshot", "close", "restore", "reopen", "snapshot", "close"], adapter.events)

    def test_rollback_verification_failure_is_distinct_after_fresh_handle_restore_check(self) -> None:
        desired, current = self.base_desired_current()
        shared = {"reopen_snapshots": [[dict(desired[0], title="Wrong")], [dict(current[0], title="Still Wrong")]]}
        adapter = FakeRekordboxAdapter(self.root / "adapter-reconcile-bad-restore", current, shared=shared)

        result = self.rb.sync_playlist("Playlist", desired, dry_run=False, adapter_factory=self.adapter_factory(adapter), confirmation_token=self.rb.APPLY_CONFIRMATION_TOKEN)

        self.assertEqual("rollback_verify_failed", result["error"]["code"])
        self.assertFalse(result["applied"])
        self.assertFalse(result["reconciled"])
        self.assertNotRegex(json.dumps(result, ensure_ascii=False), r"[A-Z]:\\|secret|private")

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

            def get_playlist(self):
                return [playlist]

            def get_content(self):
                return [keep, add, remove]

            def get_playlist_songs(self, **kwargs):
                self._assert_playlist(kwargs)
                return Query(songs)

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
        self.assertEqual("Artist New", keep.Artist.Name)
        self.assertEqual("Album New", keep.Album.Name)
        self.assertEqual(222, keep.Length)
        self.assertEqual("deezer:keep", keep.Commnt)
        self.assertEqual(str(media_keep), keep.FolderPath)
        self.assertEqual("keep.wav", keep.FileNameL)
        self.assertEqual([Path("C:/anlz/rb-add.DAT"), Path("C:/anlz/rb-keep.DAT")], sorted(adapter._staged_anlz))


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
