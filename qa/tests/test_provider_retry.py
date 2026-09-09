import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import library, main


class ProviderRetryTests(unittest.TestCase):
    def test_mixed_local_error_uses_entry_provider_raw_id_and_retry_stage(self):
        with tempfile.TemporaryDirectory(prefix="deckpipe-provider-retry-") as temporary:
            playlist = Path(temporary) / "Renamed destination"
            playlist.mkdir()
            library.save_sidecar(
                playlist,
                {
                    "tracks": {
                        "sc:track-7": {
                            "provider": "sc",
                            "title": "Synthetic",
                            "artist": "Artist",
                            "file": "source.mp3",
                            "source_file": "source.mp3",
                            "position": 3,
                            "status": "verify_failed_convert",
                            "error": "conversion failed",
                        }
                    }
                },
            )
            with (
                patch.object(main, "fetch_playlists", AsyncMock(return_value=[])),
                patch.object(main, "_sc_sources", return_value=[]),
                patch.object(main, "_local_sources", return_value=[{"id": "test", "title": "Renamed destination"}]),
                patch.object(library, "playlist_dir", return_value=playlist),
            ):
                errors = asyncio.run(main.api_errors())

        self.assertEqual(1, len(errors))
        self.assertEqual("sc", errors[0]["provider"])
        self.assertEqual("sc", errors[0]["track"]["provider"])
        self.assertEqual("track-7", errors[0]["track"]["id"])
        self.assertEqual("convert", errors[0]["track"]["retry_stage"])

        with (
            patch.object(main, "_require_music_root", return_value=Path("D:/synthetic-root")),
            patch.object(main.catalog_service, "validate_destination", return_value=Path("D:/synthetic-root/Renamed destination")),
            patch.object(main.jobs, "enqueue", return_value="job-1") as enqueue,
        ):
            response = main.api_retry(main.RetryIn(**{
                "playlist_key": errors[0]["playlist_key"],
                "playlist_title": errors[0]["playlist_title"],
                "track": errors[0]["track"],
            }))
        self.assertEqual({"job_id": "job-1"}, response)
        queued = enqueue.call_args.args[2][0]
        self.assertEqual("sc", queued["provider"])
        self.assertEqual("track-7", queued["id"])
        self.assertEqual("convert", queued["retry_stage"])
        self.assertEqual("playlist_order", enqueue.call_args.kwargs["mode"])

    def test_retry_strips_one_legacy_provider_prefix_without_double_prefixing(self):
        body = main.RetryIn(
            playlist_key="local:test",
            playlist_title="Renamed",
            track={"id": "sc:track-9", "title": "Synthetic", "provider": "sc"},
        )
        with (
            patch.object(main, "_require_music_root", return_value=Path("D:/synthetic-root")),
            patch.object(main.catalog_service, "validate_destination", return_value=Path("D:/synthetic-root/Renamed")),
            patch.object(main.jobs, "enqueue", return_value="job-2") as enqueue,
        ):
            main.api_retry(body)
        queued = enqueue.call_args.args[2][0]
        self.assertEqual("sc", queued["provider"])
        self.assertEqual("track-9", queued["id"])


if __name__ == "__main__":
    unittest.main()
