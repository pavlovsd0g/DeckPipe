from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import atomic_io, jobs, library


def _track(track_id: str, *, title: str = "Song", artist: str = "Artist", provider: str = "deezer") -> dict[str, object]:
    return {
        "id": track_id,
        "title": title,
        "artist": artist,
        "album": f"Album {track_id}",
        "duration": 1,
        "provider": provider,
        "url": f"https://example.invalid/{provider}/{track_id}",
    }


def _ready(path: Path, *, title: str = "Song", artist: str = "Artist", provider: str = "deezer") -> dict[str, object]:
    return {
        "file": path.name,
        "status": "ok",
        "title": title,
        "artist": artist,
        "provider": provider,
        "format": path.suffix.lstrip("."),
    }


class ExactLocalIdentityTests(unittest.TestCase):
    def test_same_title_from_wrong_artist_is_not_adopted(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deckpipe-stage-a-artist-") as temporary:
            playlist = Path(temporary)
            wrong = playlist / "Other Artist - Home.wav"
            wrong.write_bytes(b"other")

            scanned = library.scan_playlist(
                playlist,
                [_track("wanted", title="Home", artist="Wanted Artist")],
            )

            self.assertEqual("missing", scanned[0]["status"])
            self.assertEqual("", scanned[0]["file"])
            self.assertFalse((playlist / library.SIDECAR_NAME).exists())

    def test_mix_does_not_take_file_preowned_by_plain_title(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deckpipe-stage-a-version-") as temporary:
            playlist = Path(temporary)
            original = playlist / "Artist - Song.wav"
            original.write_bytes(b"original")
            library.save_sidecar(playlist, {"tracks": {"plain": _ready(original)}})

            scanned = library.scan_playlist(
                playlist,
                [
                    _track("mix", title="Song (Extended Mix)"),
                    _track("plain"),
                ],
            )

            self.assertEqual(["missing", "ok"], [item["status"] for item in scanned])
            self.assertEqual(original.name, scanned[1]["file"])
            self.assertNotIn("mix", library.load_sidecar(playlist)["tracks"])

    def test_equal_exact_candidates_are_ambiguous_and_not_adopted(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deckpipe-stage-a-ambiguous-") as temporary:
            playlist = Path(temporary)
            (playlist / "01 - Artist - Song.wav").write_bytes(b"one")
            (playlist / "02 - Artist - Song.wav").write_bytes(b"two")

            scanned = library.scan_playlist(playlist, [_track("ambiguous")])

            self.assertEqual("missing", scanned[0]["status"])
            self.assertEqual("", scanned[0]["file"])
            self.assertFalse((playlist / library.SIDECAR_NAME).exists())


class CollisionSafePublicationTests(unittest.TestCase):
    def _process(self, playlist: Path, track: dict[str, object], payload: bytes) -> tuple[bool, str, str]:
        class FakeSession:
            def download_track(self, _track_id, out_dir, prefer="FLAC"):
                stage = atomic_io.make_staged_path(Path(out_dir) / "Artist - Song.flac")
                stage.write_bytes(payload)
                return stage, "FLAC", {
                    "DURATION": "1",
                    "SNG_TITLE": track["title"],
                    "ART_NAME": track["artist"],
                    "ALB_TITLE": track["album"],
                }

        with (
            patch.object(jobs, "_numbering_on", return_value=False),
            patch.object(jobs, "_wav_mode", return_value="source"),
            patch.object(jobs, "verify_file", return_value=(True, "", 1.0)),
            patch("app.tagger.write_tags", return_value=None),
        ):
            return jobs._process_track(
                {"mode": "append"},
                playlist,
                track,
                {"ds": FakeSession()},
                {"base": 0, "n": 0, "digits": 2},
            )

    def test_same_visible_metadata_for_two_ids_publishes_distinct_bytes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deckpipe-stage-a-id-collision-") as temporary:
            playlist = Path(temporary)
            first_bytes = b"verified payload one"
            second_bytes = b"verified payload two"

            self.assertTrue(self._process(playlist, _track("one"), first_bytes)[0])
            self.assertTrue(self._process(playlist, _track("two"), second_bytes)[0])

            sidecar = library.load_sidecar(playlist)["tracks"]
            first = playlist / sidecar["one"]["file"]
            second = playlist / sidecar["two"]["file"]
            self.assertNotEqual(first, second)
            self.assertEqual(first_bytes, first.read_bytes())
            self.assertEqual(second_bytes, second.read_bytes())

    def test_existing_unowned_download_target_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deckpipe-stage-a-unowned-download-") as temporary:
            playlist = Path(temporary)
            unowned = playlist / "Artist - Song.flac"
            unowned.write_bytes(b"manual media")

            ok, error, _quality = self._process(playlist, _track("download"), b"new media")

            self.assertTrue(ok, error)
            self.assertEqual(b"manual media", unowned.read_bytes())
            entry = library.load_sidecar(playlist)["tracks"]["download"]
            self.assertNotEqual(unowned.name, entry["file"])
            self.assertEqual(b"new media", (playlist / entry["file"]).read_bytes())

    def test_existing_unowned_wav_target_is_preserved_and_conversion_uses_identity_name(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deckpipe-stage-a-unowned-wav-") as temporary:
            playlist = Path(temporary)
            unowned = playlist / "Artist - Song.wav"
            unowned.write_bytes(b"manual wav")

            class FakeSession:
                def download_track(self, _track_id, out_dir, prefer="FLAC"):
                    stage = atomic_io.make_staged_path(Path(out_dir) / "Artist - Song.flac")
                    stage.write_bytes(b"source media")
                    return stage, "FLAC", {
                        "DURATION": "1",
                        "SNG_TITLE": "Song",
                        "ART_NAME": "Artist",
                        "ALB_TITLE": "Album wav",
                    }

            def fake_convert(source: Path) -> Path:
                stage = atomic_io.make_staged_path(source.with_suffix(".wav"))
                stage.write_bytes(b"converted wav")
                return stage

            with (
                patch.object(jobs, "_numbering_on", return_value=False),
                patch.object(jobs, "_wav_mode", return_value="wav_keep"),
                patch.object(jobs, "verify_file", return_value=(True, "", 1.0)),
                patch("app.converter.convert_to_wav", side_effect=fake_convert),
                patch("app.tagger.write_tags", return_value=None),
            ):
                ok, error, _quality = jobs._process_track(
                    {"mode": "append"},
                    playlist,
                    _track("wav"),
                    {"ds": FakeSession()},
                    {"base": 0, "n": 0, "digits": 2},
                )

            self.assertTrue(ok, error)
            self.assertEqual(b"manual wav", unowned.read_bytes())
            entry = library.load_sidecar(playlist)["tracks"]["wav"]
            self.assertEqual("wav", entry["format"])
            self.assertNotEqual(unowned.name, entry["file"])
            self.assertEqual(b"converted wav", (playlist / entry["file"]).read_bytes())

    def test_failed_conversion_retry_keeps_provider_source_and_does_not_redownload(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deckpipe-stage-a-convert-retry-") as temporary:
            playlist = Path(temporary)
            source = playlist / "Artist - Song.flac"
            source.write_bytes(b"source")
            library.save_sidecar(
                playlist,
                {
                    "tracks": {
                        "sc:retry": {
                            **_ready(source, provider="sc"),
                            "source_file": source.name,
                            "status": "verify_failed_convert",
                            "error": "conversion failed",
                            "duration_actual": 1.0,
                        }
                    }
                },
            )

            class NoDownload:
                def download_track(self, *_args, **_kwargs):
                    raise AssertionError("failed conversion retry redownloaded source")

            with patch.object(jobs, "_wav_step", return_value=(None, "conversion failed")):
                ok, error, _quality = jobs._process_track(
                    {"mode": "append"},
                    playlist,
                    _track("retry", provider="sc"),
                    {"ds": NoDownload()},
                    {"base": 0, "n": 0, "digits": 2},
                )

            self.assertFalse(ok)
            self.assertEqual("conversion failed", error)
            entry = library.load_sidecar(playlist)["tracks"]["sc:retry"]
            self.assertEqual("sc", entry["provider"])
            self.assertEqual(source.name, entry["source_file"])
            self.assertEqual("verify_failed_convert", entry["status"])
            self.assertTrue(source.exists())

    def test_failed_conversion_without_owned_source_stays_at_conversion_stage(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deckpipe-stage-a-convert-source-missing-") as temporary:
            root = Path(temporary)
            playlist = root / "playlist"
            playlist.mkdir()
            outside = root / "outside.flac"
            outside.write_bytes(b"unowned outside")

            for source_name in ("missing.flac", "../outside.flac"):
                with self.subTest(source_name=source_name):
                    library.save_sidecar(
                        playlist,
                        {
                            "tracks": {
                                "retry": {
                                    "title": "Song",
                                    "artist": "Artist",
                                    "provider": "deezer",
                                    "file": source_name,
                                    "source_file": source_name,
                                    "status": "verify_failed_convert",
                                    "error": "conversion failed",
                                    "duration_actual": 1.0,
                                }
                            }
                        },
                    )
                    calls = 0

                    class NoDownload:
                        def download_track(self, *_args, **_kwargs):
                            nonlocal calls
                            calls += 1
                            raise AssertionError("conversion retry attempted a provider download")

                    ok, error, _quality = jobs._process_track(
                        {"mode": "append"},
                        playlist,
                        _track("retry"),
                        {"ds": NoDownload()},
                        {"base": 0, "n": 0, "digits": 2},
                    )

                    self.assertFalse(ok)
                    self.assertEqual("conversion failed", error)
                    self.assertEqual(0, calls)
                    entry = library.load_sidecar(playlist)["tracks"]["retry"]
                    self.assertEqual("deezer", entry["provider"])
                    self.assertEqual(source_name, entry["source_file"])
                    self.assertEqual("verify_failed_convert", entry["status"])
                    self.assertEqual(b"unowned outside", outside.read_bytes())


class DurableStateAndRenameTests(unittest.TestCase):
    def test_backup_only_state_survives_failed_primary_publication(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deckpipe-stage-a-backup-only-") as temporary:
            primary = Path(temporary) / "state.json"
            backup = atomic_io.backup_path(primary)
            original = {"tracks": {"one": {"status": "ok"}}}
            backup.write_text(json.dumps(original), encoding="utf-8")
            before = backup.read_bytes()

            def fail_primary(src: Path, dst: Path) -> None:
                if Path(dst) == primary:
                    raise OSError("injected primary publication failure")
                os.replace(src, dst)

            with self.assertRaises(atomic_io.AtomicIOError):
                atomic_io.atomic_write_json(
                    primary,
                    {"tracks": {"two": {"status": "ok"}}},
                    backup=True,
                    replace=fail_primary,
                )

            self.assertFalse(primary.exists())
            self.assertEqual(before, backup.read_bytes())
            self.assertEqual(original, atomic_io.atomic_load_json(primary, default={}, backup=True))

    def test_failed_sidecar_save_after_rename_recovers_mapping_from_journal(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deckpipe-stage-a-rename-fail-") as temporary:
            playlist = Path(temporary)
            original = playlist / "01 - Artist - Song.wav"
            original.write_bytes(b"media")
            library.save_sidecar(playlist, {"tracks": {"original": _ready(original)}})

            with patch.object(library, "save_sidecar", side_effect=OSError("injected sidecar save failure")):
                with self.assertRaises(OSError):
                    library.renumber_playlist(playlist, ["absent", "original"], 2)

            renamed = playlist / "02 - Artist - Song.wav"
            self.assertFalse(original.exists())
            self.assertTrue(renamed.exists())
            recovered = library.load_sidecar(playlist)
            self.assertEqual(renamed.name, recovered["tracks"]["original"]["file"])
            self.assertEqual(b"media", renamed.read_bytes())
            self.assertFalse((playlist / ".deckpipe-renames.json").exists())

    def test_physical_rename_failure_keeps_journal_and_recovers(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deckpipe-stage-a-rename-io-fail-") as temporary:
            playlist = Path(temporary)
            original = playlist / "01 - Artist - Song.wav"
            original.write_bytes(b"media")
            library.save_sidecar(playlist, {"tracks": {"original": _ready(original)}})
            real_move = library.rename_journal.move_new
            calls = 0

            def fail_second_move(source: Path, destination: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise atomic_io.AtomicIOError("injected rename failure")
                real_move(source, destination)

            with patch.object(library.rename_journal, "move_new", side_effect=fail_second_move):
                with self.assertRaises(atomic_io.AtomicIOError):
                    library.renumber_playlist(playlist, ["absent", "original"], 2)

            self.assertTrue((playlist / ".deckpipe-renames.json").exists())
            recovered = library.load_sidecar(playlist)
            renamed = playlist / "02 - Artist - Song.wav"
            self.assertEqual(renamed.name, recovered["tracks"]["original"]["file"])
            self.assertEqual(b"media", renamed.read_bytes())
            self.assertFalse((playlist / ".deckpipe-renames.json").exists())

    def test_recovery_never_clobbers_unexpected_destination_or_removes_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deckpipe-stage-a-rename-conflict-") as temporary:
            playlist = Path(temporary)
            original = playlist / "01 - Artist - Song.wav"
            original.write_bytes(b"media")
            library.save_sidecar(playlist, {"tracks": {"original": _ready(original)}})
            operation = {
                "key": "original",
                "source": original.name,
                "temporary": ".deckpipe-rename-fixture.part.wav",
                "destination": "02 - Artist - Song.wav",
                "position": 2,
            }
            library.rename_journal.save(playlist, [operation])
            temporary_path = playlist / operation["temporary"]
            library.rename_journal.move_new(original, temporary_path)
            unexpected = playlist / operation["destination"]
            unexpected.write_bytes(b"unrelated")

            with self.assertRaises(RuntimeError):
                library.load_sidecar(playlist)

            self.assertEqual(b"unrelated", unexpected.read_bytes())
            self.assertEqual(b"media", temporary_path.read_bytes())
            self.assertTrue((playlist / ".deckpipe-renames.json").exists())
            persisted = json.loads((playlist / library.SIDECAR_NAME).read_text(encoding="utf-8"))
            self.assertEqual(original.name, persisted["tracks"]["original"]["file"])

    def test_hard_exit_after_physical_rename_is_recovered_by_fresh_process(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deckpipe-stage-a-rename-exit-") as temporary:
            playlist = Path(temporary)
            original = playlist / "01 - Artist - Song.wav"
            original.write_bytes(b"media")
            library.save_sidecar(playlist, {"tracks": {"original": _ready(original)}})

            exit_script = """
import os
import sys
from pathlib import Path
from unittest.mock import patch
from app import library

playlist = Path(sys.argv[1])
with patch.object(library, "save_sidecar", side_effect=lambda *_args, **_kwargs: os._exit(91)):
    library.renumber_playlist(playlist, ["absent", "original"], 2)
"""
            exited = subprocess.run(
                [sys.executable, "-c", exit_script, str(playlist)],
                cwd=Path(__file__).resolve().parents[2],
                env=os.environ.copy(),
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(91, exited.returncode, exited.stderr)

            load_script = """
import json
import sys
from pathlib import Path
from app import library
print(json.dumps(library.load_sidecar(Path(sys.argv[1])), ensure_ascii=False))
"""
            loaded = subprocess.run(
                [sys.executable, "-c", load_script, str(playlist)],
                cwd=Path(__file__).resolve().parents[2],
                env=os.environ.copy(),
                capture_output=True,
                text=True,
                timeout=20,
                check=True,
            )
            recovered = json.loads(loaded.stdout)
            renamed = playlist / "02 - Artist - Song.wav"
            self.assertEqual(renamed.name, recovered["tracks"]["original"]["file"])
            self.assertEqual(b"media", renamed.read_bytes())
            self.assertFalse((playlist / ".deckpipe-renames.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
