from __future__ import annotations

import asyncio
import json
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


class ErrorListingCoverageTests(unittest.TestCase):
    @unittest.expectedFailure
    def test_local_playlist_errors_are_included_in_global_error_listing(self) -> None:
        """Desired B5 contract; current endpoint only walks Deezer and SC sources."""
        from app import main

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
