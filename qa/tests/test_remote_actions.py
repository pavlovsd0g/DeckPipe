import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from app import main
from app.remote_actions import RemoteActionStore


def track_page(ids: list[str], *, has_next: bool = False, cursor: str | None = None):
    edges = []
    for track_id in ids:
        node = NS(
            id=track_id,
            title=f"Track {track_id}",
            duration=1,
            album=None,
            contributors=NS(edges=[]),
        )
        edges.append(NS(node=node))
    return NS(tracks=NS(edges=edges, page_info=NS(has_next_page=has_next, end_cursor=cursor)))


class RemoteActionTests(unittest.TestCase):
    def test_failed_remote_add_is_durable_independent_of_local_job(self):
        with tempfile.TemporaryDirectory(prefix="deckpipe-remote-actions-") as temporary:
            store = RemoteActionStore(Path(temporary) / "remote-actions.json")
            client = NS(add_tracks_to_playlist=AsyncMock(side_effect=ConnectionError("secret provider body")))
            body = main.SearchDownloadIn(
                target_key="123",
                target_title="Synthetic",
                tracks=[{"id": "1", "title": "One", "provider": "deezer"}],
            )
            with (
                patch.object(main, "_require_music_root", return_value=Path(temporary)),
                patch.object(main.catalog_service, "prepare_download", return_value={
                    "tracks": list(body.tracks), "already_present": 0, "needs_attention": 0,
                }),
                patch.object(main.jobs, "enqueue", return_value="job-1"),
                patch.object(main, "_remote_action_store", store),
                patch.object(main, "_deezer_gql_client", return_value=client),
            ):
                result = asyncio.run(main.api_search_download(body))

            self.assertEqual("job-1", result["job_id"])
            self.assertEqual(0, result["added_to_deezer"])
            self.assertEqual("failed", result["remote_action"]["state"])
            self.assertNotIn("secret", str(result["remote_action"]))

            restarted = RemoteActionStore(Path(temporary) / "remote-actions.json")
            persisted = restarted.get(result["remote_action"]["id"])
            self.assertEqual("failed", persisted["state"])
            self.assertEqual(["1"], persisted["track_ids"])

    def test_deezer_add_error_result_is_not_recorded_as_success(self):
        with tempfile.TemporaryDirectory(prefix="deckpipe-remote-error-result-") as temporary:
            store = RemoteActionStore(Path(temporary) / "remote-actions.json")
            client = NS(add_tracks_to_playlist=AsyncMock(return_value=NS(is_not_allowed=True)))
            body = main.SearchDownloadIn(
                target_key="123",
                target_title="Synthetic",
                tracks=[{"id": "1", "title": "One", "provider": "deezer"}],
            )
            with (
                patch.object(main, "_require_music_root", return_value=Path(temporary)),
                patch.object(main.catalog_service, "prepare_download", return_value={
                    "tracks": list(body.tracks), "already_present": 0, "needs_attention": 0,
                }),
                patch.object(main.jobs, "enqueue", return_value="job-error-result"),
                patch.object(main, "_remote_action_store", store),
                patch.object(main, "_deezer_gql_client", return_value=client),
            ):
                result = asyncio.run(main.api_search_download(body))
        self.assertEqual(0, result["added_to_deezer"])
        self.assertEqual("failed", result["remote_action"]["state"])
        self.assertEqual("provider_access_denied", result["remote_action"]["last_error"]["code"])

    def test_retry_reconciles_membership_then_adds_only_missing_ids(self):
        with tempfile.TemporaryDirectory(prefix="deckpipe-remote-retry-") as temporary:
            path = Path(temporary) / "remote-actions.json"
            first_store = RemoteActionStore(path)
            action = first_store.create_deezer_add("123", ["1", "2"])
            first_store.mark_failed(action["id"], {"code": "provider_unavailable", "service": "deezer", "message": "Deezer is temporarily unavailable", "retryable": True})

            restarted_store = RemoteActionStore(path)
            client = NS(
                get_playlist=AsyncMock(return_value=track_page(["1"])),
                add_tracks_to_playlist=AsyncMock(return_value=NS(added_track_ids=["2"])),
            )
            with (
                patch.object(main, "_remote_action_store", restarted_store),
                patch.object(main, "_deezer_gql_client", return_value=client),
            ):
                result = asyncio.run(main.api_remote_action_retry(action["id"]))

            self.assertEqual("succeeded", result["state"])
            self.assertEqual(["2"], result["added_track_ids"])
            client.add_tracks_to_playlist.assert_awaited_once_with(playlist_id="123", track_ids=["2"])

    def test_retry_with_all_tracks_present_performs_no_duplicate_mutation(self):
        with tempfile.TemporaryDirectory(prefix="deckpipe-remote-reconcile-") as temporary:
            store = RemoteActionStore(Path(temporary) / "remote-actions.json")
            action = store.create_deezer_add("123", ["1", "2"])
            client = NS(
                get_playlist=AsyncMock(return_value=track_page(["1", "2"])),
                add_tracks_to_playlist=AsyncMock(),
            )
            with (
                patch.object(main, "_remote_action_store", store),
                patch.object(main, "_deezer_gql_client", return_value=client),
            ):
                result = asyncio.run(main.api_remote_action_retry(action["id"]))
            self.assertEqual("succeeded", result["state"])
            self.assertEqual([], result["added_track_ids"])
            client.add_tracks_to_playlist.assert_not_awaited()

    def test_list_route_exposes_restart_safe_actions_newest_first(self):
        with tempfile.TemporaryDirectory(prefix="deckpipe-remote-list-") as temporary:
            store = RemoteActionStore(Path(temporary) / "remote-actions.json")
            first = store.create_deezer_add("123", ["1"])
            second = store.create_deezer_add("456", ["2"])
            restarted = RemoteActionStore(store.path)
            with patch.object(main, "_remote_action_store", restarted):
                actions = main.api_remote_actions()
        self.assertEqual([second["id"], first["id"]], [action["id"] for action in actions])

    def test_retry_failure_is_persisted_and_http_error_has_no_raw_response(self):
        with tempfile.TemporaryDirectory(prefix="deckpipe-remote-failed-retry-") as temporary:
            store = RemoteActionStore(Path(temporary) / "remote-actions.json")
            action = store.create_deezer_add("123", ["1"])
            client = NS(get_playlist=AsyncMock(side_effect=ConnectionError("raw secret response")))
            with (
                patch.object(main, "_remote_action_store", store),
                patch.object(main, "_deezer_gql_client", return_value=client),
            ):
                with self.assertRaises(Exception) as captured:
                    asyncio.run(main.api_remote_action_retry(action["id"]))
            persisted = RemoteActionStore(store.path).get(action["id"])
        self.assertEqual("failed", persisted["state"])
        self.assertEqual("provider_unavailable", captured.exception.detail["code"])
        self.assertNotIn("raw secret", str(captured.exception.detail))


if __name__ == "__main__":
    unittest.main()
