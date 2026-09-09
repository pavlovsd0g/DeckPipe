from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import library


BAD_GLYPHS = {"\ufffd", "\u25a0", "\u25a1", "\u25af"}


def track(track_id: str, title: str, artist: str) -> dict[str, object]:
    return {
        "id": track_id,
        "title": title,
        "artist": artist,
        "album": "Тестовый альбом",
        "duration": 180,
    }


class PlaylistScanIsolationTests(unittest.TestCase):
    def test_missing_unicode_track_preserves_metadata_without_creating_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deckpipe-core-qa-") as temporary:
            playlist = Path(temporary) / "Несуществующий плейлист"
            source = track("1", "Танцы по расчёту", "Стереополина")

            result = library.scan_playlist(playlist, [source])

            self.assertEqual("missing", result[0]["status"])
            self.assertEqual(source["title"], result[0]["title"])
            self.assertFalse(any(char in result[0]["title"] for char in BAD_GLYPHS))
            self.assertFalse(playlist.exists())

    def test_adopted_cyrillic_file_is_written_to_utf8_sidecar_without_escapes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deckpipe-core-qa-") as temporary:
            playlist = Path(temporary) / "Русская волна"
            playlist.mkdir()
            audio = playlist / "01 - Молчат Дома - Судно.flac"
            audio.write_bytes(b"")
            source = track("2", "Судно", "Молчат Дома")

            result = library.scan_playlist(playlist, [source])
            sidecar_path = playlist / library.SIDECAR_NAME
            sidecar_bytes = sidecar_path.read_bytes()
            sidecar = json.loads(sidecar_bytes.decode("utf-8"))

            self.assertEqual("ok", result[0]["status"])
            self.assertEqual(audio.name, result[0]["file"])
            self.assertEqual(source["title"], sidecar["tracks"]["2"]["title"])
            self.assertIn("Судно".encode("utf-8"), sidecar_bytes)
            self.assertNotIn(b"\\u04", sidecar_bytes.lower())

    def test_verify_failure_maps_to_error_without_reencoding_text(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deckpipe-core-qa-") as temporary:
            playlist = Path(temporary) / "Ошибки"
            playlist.mkdir()
            library.save_sidecar(
                playlist,
                {
                    "tracks": {
                        "3": {
                            "title": "Группа крови",
                            "artist": "Кино",
                            "file": "03 - Кино - Группа крови.flac",
                            "format": "flac",
                            "status": "verify_failed_metadata",
                            "error": "Название не совпало",
                        }
                    }
                },
            )

            result = library.scan_playlist(
                playlist, [track("3", "Группа крови", "Кино")]
            )

            self.assertEqual("error", result[0]["status"])
            self.assertEqual("Название не совпало", result[0]["error"])
            self.assertEqual("Группа крови", result[0]["title"])

    def test_missing_file_updates_existing_sidecar_status(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deckpipe-core-qa-") as temporary:
            playlist = Path(temporary) / "Missing"
            playlist.mkdir()
            library.save_sidecar(
                playlist,
                {
                    "tracks": {
                        "4": {
                            "title": "Нежность",
                            "artist": "Маяк",
                            "file": "04 - Маяк - Нежность.flac",
                            "format": "flac",
                            "status": "ok",
                        }
                    }
                },
            )

            result = library.scan_playlist(
                playlist, [track("4", "Нежность", "Маяк")]
            )
            saved = library.load_sidecar(playlist)

            self.assertEqual("missing", result[0]["status"])
            self.assertEqual("missing", saved["tracks"]["4"]["status"])

    def test_shortened_title_does_not_adopt_a_different_version(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deckpipe-fuzzy-qa-") as temporary:
            playlist = Path(temporary) / "playlist"
            playlist.mkdir()
            correct = playlist / "Artist - Song.flac"
            correct.write_bytes(b"")
            (playlist / "Other - Song.flac").write_bytes(b"")
            (playlist / "Noise - Radio.flac").write_bytes(b"")

            result = library.scan_playlist(
                playlist,
                [{"id": "fuzzy", "title": "Song (Radio Edit)", "artist": "Artist", "album": "", "duration": 180}],
            )

            self.assertEqual("missing", result[0]["status"])
            self.assertEqual("", result[0]["file"])
            counters = library.get_last_scan_counters()
            self.assertEqual(1, counters["enumerations"])
            self.assertEqual(3, counters["normalized_stems"])
            self.assertEqual(0, counters["candidate_checks"])

    def test_provider_collision_scan_keeps_deezer_and_soundcloud_identities_separate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deckpipe-provider-collision-") as temporary:
            playlist = Path(temporary) / "playlist"
            playlist.mkdir()
            (playlist / "Deezer Artist - Same.flac").write_bytes(b"")
            (playlist / "SC Artist - Same.mp3").write_bytes(b"")

            result = library.scan_playlist(
                playlist,
                [
                    {"id": "1", "title": "Same", "artist": "Deezer Artist", "album": "", "duration": 180},
                    {"id": "1", "title": "Same", "artist": "SC Artist", "album": "", "duration": 180, "provider": "sc"},
                ],
            )
            sidecar = library.load_sidecar(playlist)

            self.assertEqual(["ok", "ok"], [item["status"] for item in result])
            self.assertIn("1", sidecar["tracks"])
            self.assertIn("sc:1", sidecar["tracks"])

    def test_ready_entry_rejects_partial_names_case_insensitively(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deckpipe-ready-entry-") as temporary:
            playlist = Path(temporary)
            (playlist / "Final.flac").write_bytes(b"ok")
            (playlist / "Upper.PART.FLAC").write_bytes(b"partial")

            self.assertTrue(library.is_ready_entry(playlist, {"status": "ok", "file": "Final.flac"}))
            self.assertFalse(library.is_ready_entry(playlist, {"status": "ok", "file": "Upper.PART.FLAC"}))
            self.assertFalse(library.is_ready_entry(playlist, {"status": "ok", "file": ""}))

    def test_ready_entry_requires_regular_file_inside_playlist(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deckpipe-ready-entry-strict-") as temporary:
            root = Path(temporary)
            playlist = root / "playlist"
            playlist.mkdir()
            (playlist / "Final.flac").write_bytes(b"ok")
            (playlist / "Directory.flac").mkdir()
            escape = root / "outside.flac"
            escape.write_bytes(b"outside")

            self.assertTrue(library.is_ready_entry(playlist, {"status": "ok", "file": "Final.flac"}))
            self.assertFalse(library.is_ready_entry(playlist, {"status": "ok", "file": "../outside.flac"}))
            self.assertFalse(library.is_ready_entry(playlist, {"status": "ok", "file": str(escape)}))
            self.assertFalse(library.is_ready_entry(playlist, {"status": "ok", "file": "Directory.flac"}))
            try:
                symlink = playlist / "escape.flac"
                symlink.symlink_to(escape)
            except OSError:
                symlink = None
            if symlink is not None:
                self.assertFalse(library.is_ready_entry(playlist, {"status": "ok", "file": symlink.name}))


class ErrorListingCoverageTests(unittest.TestCase):
    def test_local_playlist_errors_are_included_in_global_error_listing(self) -> None:
        old_token = os.environ.get("DECKPIPE_API_TOKEN")
        old_port = os.environ.get("DECKPIPE_BOUND_PORT")
        os.environ["DECKPIPE_API_TOKEN"] = "isolated-core-test-token"
        os.environ["DECKPIPE_BOUND_PORT"] = "8123"
        try:
            from app import main
        finally:
            if old_token is None:
                os.environ.pop("DECKPIPE_API_TOKEN", None)
            else:
                os.environ["DECKPIPE_API_TOKEN"] = old_token
            if old_port is None:
                os.environ.pop("DECKPIPE_BOUND_PORT", None)
            else:
                os.environ["DECKPIPE_BOUND_PORT"] = old_port

        async def no_deezer_playlists() -> list[dict[str, object]]:
            return []

        with tempfile.TemporaryDirectory(prefix="deckpipe-errors-qa-") as temporary:
            playlist = Path(temporary) / "Локальный плейлист"
            playlist.mkdir()
            library.save_sidecar(
                playlist,
                {
                    "tracks": {
                        "5": {
                            "title": "Локальная ошибка",
                            "artist": "Тест",
                            "file": "missing.flac",
                            "status": "verify_failed_metadata",
                            "error": "fixture",
                        }
                    }
                },
            )

            with (
                patch.object(main, "fetch_playlists", no_deezer_playlists),
                patch.object(main, "_sc_sources", return_value=[]),
                patch.object(
                    main,
                    "_local_sources",
                    return_value=[{"id": "fixture", "title": "Локальный плейлист"}],
                ),
                patch.object(library, "playlist_dir", return_value=playlist),
            ):
                errors = asyncio.run(main.api_errors())

            self.assertEqual(1, len(errors))
            self.assertEqual("local", errors[0]["provider"])
            self.assertEqual("local:fixture", errors[0]["playlist_key"])

    def test_ready_predicate_controls_counts_and_rb_sync_never_receives_partial(self) -> None:
        old_token = os.environ.get("DECKPIPE_API_TOKEN")
        old_port = os.environ.get("DECKPIPE_BOUND_PORT")
        os.environ["DECKPIPE_API_TOKEN"] = "isolated-core-test-token"
        os.environ["DECKPIPE_BOUND_PORT"] = "8123"
        try:
            from app import main
        finally:
            if old_token is None:
                os.environ.pop("DECKPIPE_API_TOKEN", None)
            else:
                os.environ["DECKPIPE_API_TOKEN"] = old_token
            if old_port is None:
                os.environ.pop("DECKPIPE_BOUND_PORT", None)
            else:
                os.environ["DECKPIPE_BOUND_PORT"] = old_port

        with tempfile.TemporaryDirectory(prefix="deckpipe-ready-api-") as temporary:
            playlist = Path(temporary) / "playlist"
            playlist.mkdir()
            partial = playlist / "Artist - Bad.DECKPIPE-STAGE-X.PART.FLAC"
            partial.write_bytes(b"partial")
            library.save_sidecar(
                playlist,
                {"tracks": {"1": {"title": "Bad", "artist": "Artist", "file": partial.name, "status": "ok"}}},
            )

            with (
                patch.object(main, "_local_sources", return_value=[{"id": "fixture", "title": "Ready API"}]),
                patch.object(library, "playlist_dir", return_value=playlist),
                patch.object(main, "_require_music_root", return_value=playlist),
            ):
                listed = main.api_local_playlists()

            self.assertEqual(0, listed[0]["ok"])
            with (
                patch.object(library, "playlist_dir", return_value=playlist),
                patch.object(main.rb, "sync_playlist", side_effect=AssertionError("partial reached RB")),
            ):
                with self.assertRaises(Exception):
                    main.api_rb_sync(main.RbSyncIn(playlist_key="local:fixture", playlist_title="Ready API"))

            with patch.object(main.jobs, "enqueue_flip", side_effect=AssertionError("flip enqueue reached live worker")):
                with self.assertRaises(Exception):
                    main.api_flip(main.FlipIn(playlist_key="local:fixture", playlist_title="Ready API", to_wav=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
