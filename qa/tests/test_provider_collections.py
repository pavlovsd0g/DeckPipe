import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from app import main, soundcloud
from app.provider_errors import provider_exception


def playlist_page(start: int, count: int, *, has_next: bool, cursor: str | None):
    edges = [
        NS(
            node=NS(
                id=str(index),
                title=f"Playlist {index}",
                estimated_tracks_count=index,
                picture=None,
            )
        )
        for index in range(start, start + count)
    ]
    return NS(playlists=NS(edges=edges, page_info=NS(has_next_page=has_next, end_cursor=cursor)))


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class ProviderCollectionTests(unittest.TestCase):
    def setUp(self):
        self.cache = {"playlists": (0, None), "tracks": {}}

    def test_fetch_playlists_reads_all_120_items_with_cursor_contract(self):
        client = NS(
            get_user_playlists=AsyncMock(
                side_effect=[
                    playlist_page(0, 50, has_next=True, cursor="cursor-1"),
                    playlist_page(50, 50, has_next=True, cursor="cursor-2"),
                    playlist_page(100, 20, has_next=False, cursor=None),
                ]
            )
        )
        with patch.object(main, "_cache", self.cache), patch.object(main, "_deezer_gql_client", return_value=client):
            result = asyncio.run(main.fetch_playlists())

        self.assertEqual(120, len(result))
        self.assertEqual(
            [
                unittest.mock.call(first=50, after=None),
                unittest.mock.call(first=50, after="cursor-1"),
                unittest.mock.call(first=50, after="cursor-2"),
            ],
            client.get_user_playlists.await_args_list,
        )

    def test_playlist_page_boundaries_0_1_50_51_are_complete(self):
        cases = {
            0: [playlist_page(0, 0, has_next=False, cursor=None)],
            1: [playlist_page(0, 1, has_next=False, cursor=None)],
            50: [playlist_page(0, 50, has_next=False, cursor=None)],
            51: [
                playlist_page(0, 50, has_next=True, cursor="next"),
                playlist_page(50, 1, has_next=False, cursor=None),
            ],
        }
        for count, pages in cases.items():
            with self.subTest(count=count):
                client = NS(get_user_playlists=AsyncMock(side_effect=pages))
                cache = {"playlists": (0, None), "tracks": {}}
                with patch.object(main, "_cache", cache), patch.object(main, "_deezer_gql_client", return_value=client):
                    result = asyncio.run(main.fetch_playlists())
                self.assertEqual(count, len(result))
                self.assertEqual(2 if count == 51 else 1, client.get_user_playlists.await_count)

    def test_graphql_tracks_read_all_pages_and_reject_repeated_cursor(self):
        def track(track_id: str):
            return NS(
                node=NS(
                    id=track_id,
                    title=f"Track {track_id}",
                    duration=1,
                    album=None,
                    contributors=NS(edges=[]),
                )
            )

        pages = [
            NS(tracks=NS(edges=[track("1")], page_info=NS(has_next_page=True, end_cursor="next"))),
            NS(tracks=NS(edges=[track("2")], page_info=NS(has_next_page=False, end_cursor=None))),
        ]
        client = NS(get_playlist=AsyncMock(side_effect=pages))
        result = asyncio.run(main._fetch_tracks_graphql_with_client(client, "playlist"))
        self.assertEqual(["1", "2"], [item["id"] for item in result])

        repeated = NS(tracks=NS(edges=[track("2")], page_info=NS(has_next_page=True, end_cursor="next")))
        client = NS(get_playlist=AsyncMock(side_effect=[pages[0], repeated]))
        with self.assertRaises(HTTPException) as captured:
            asyncio.run(main._fetch_tracks_graphql_with_client(client, "playlist"))
        self.assertEqual("provider_collection_incomplete", captured.exception.detail["code"])

    def test_repeated_or_empty_playlist_cursor_is_explicit_and_never_cached(self):
        for bad_cursor in ("", "cursor-1"):
            with self.subTest(cursor=bad_cursor):
                pages = [playlist_page(0, 1, has_next=True, cursor="cursor-1")]
                if bad_cursor == "":
                    pages = [playlist_page(0, 1, has_next=True, cursor="")]
                else:
                    pages.append(playlist_page(1, 1, has_next=True, cursor=bad_cursor))
                client = NS(get_user_playlists=AsyncMock(side_effect=pages))
                cache = {"playlists": (0, None), "tracks": {}}
                with patch.object(main, "_cache", cache), patch.object(main, "_deezer_gql_client", return_value=client):
                    with self.assertRaises(HTTPException) as captured:
                        asyncio.run(main.fetch_playlists())
                self.assertEqual("provider_collection_incomplete", captured.exception.detail["code"])
                self.assertIsNone(cache["playlists"][1])

    def test_playlist_network_failure_is_http_503_detail_object(self):
        with (
            patch.object(main, "_require_music_root", return_value=Path("D:/synthetic-root")),
            patch.object(main, "fetch_playlists", AsyncMock(side_effect=ConnectionError("secret response body"))),
        ):
            with self.assertRaises(HTTPException) as captured:
                asyncio.run(main.api_playlists())
        self.assertEqual(503, captured.exception.status_code)
        self.assertEqual(
            {"code": "provider_unavailable", "service": "deezer", "message": "Deezer is temporarily unavailable", "retryable": True},
            captured.exception.detail,
        )
        self.assertNotIn("secret", str(captured.exception.detail))

    def test_deezer_gql_auth_error_is_explicit_without_using_raw_message(self):
        gql_auth_error = type("GraphQLClientAuthError", (Exception,), {})("raw ARL response")
        error = provider_exception("deezer", gql_auth_error)
        self.assertEqual(401, error.status_code)
        self.assertEqual("provider_auth_required", error.detail["code"])
        self.assertNotIn("ARL", str(error.detail))

    def test_provider_http_statuses_map_to_public_403_429_and_5xx_errors(self):
        cases = [
            (403, 403, "provider_access_denied", False),
            (429, 429, "provider_rate_limited", True),
            (500, 503, "provider_unavailable", True),
        ]
        for upstream, public, code, retryable in cases:
            with self.subTest(upstream=upstream):
                exc = type("SyntheticHttpError", (Exception,), {"status_code": upstream})("raw secret body")
                error = provider_exception("deezer", exc)
                self.assertEqual(public, error.status_code)
                self.assertEqual(code, error.detail["code"])
                self.assertEqual(retryable, error.detail["retryable"])
                self.assertNotIn("raw secret", str(error.detail))

    def test_album_follows_trusted_next_pages_and_rejects_untrusted_origin(self):
        first = {
            "title": "Long Album",
            "tracks": {
                "data": [{"id": "1", "title": "One", "artist": {"name": "A"}, "duration": 1}],
                "next": "https://api.deezer.com/album/long/tracks?index=1",
            },
        }
        second = {
            "data": [{"id": "2", "title": "Two", "artist": {"name": "A"}, "duration": 2}],
        }
        with patch.object(main.requests, "get", side_effect=[FakeResponse(first), FakeResponse(second)]) as request:
            tracks = main.api_dz_album("long")
        self.assertEqual(["1", "2"], [track["id"] for track in tracks])
        self.assertEqual(2, request.call_count)

        hostile = {**first, "tracks": {**first["tracks"], "next": "https://evil.example/steal"}}
        with patch.object(main.requests, "get", return_value=FakeResponse(hostile)) as request:
            with self.assertRaises(HTTPException) as captured:
                main.api_dz_album("long")
        self.assertEqual("provider_pagination_invalid", captured.exception.detail["code"])
        self.assertEqual(1, request.call_count)

    def test_soundcloud_account_collection_reports_partial_page_failure(self):
        own_page = {
            "collection": [{"id": 1, "title": "Mine", "permalink_url": "https://soundcloud.com/me/mine", "track_count": 1}],
            "next_href": "https://evil.example/next",
        }
        likes_page = {"collection": [], "next_href": None}
        with (
            patch.object(soundcloud, "sc_validate", return_value={"id": 7}),
            patch.object(soundcloud, "sc_client_id", return_value="synthetic-client"),
            patch.object(soundcloud.requests, "get", side_effect=[FakeResponse(own_page), FakeResponse(likes_page)]) as request,
        ):
            result = soundcloud.sc_account_playlists("synthetic-token")
        self.assertEqual(["1"], [item["id"] for item in result])
        self.assertEqual("provider_pagination_invalid", result.errors[0]["code"])
        self.assertEqual(2, request.call_count)

    def test_soundcloud_account_collection_reads_all_pages(self):
        def page(start: int, count: int, next_href: str | None):
            return {
                "collection": [
                    {"id": index, "title": f"Set {index}", "permalink_url": f"https://soundcloud.com/me/{index}", "track_count": 1}
                    for index in range(start, start + count)
                ],
                "next_href": next_href,
            }

        responses = [
            FakeResponse(page(0, 50, "https://api-v2.soundcloud.com/users/7/playlists?cursor=one")),
            FakeResponse(page(50, 50, "https://api-v2.soundcloud.com/users/7/playlists?cursor=two")),
            FakeResponse(page(100, 20, None)),
            FakeResponse({"collection": [], "next_href": None}),
        ]
        with (
            patch.object(soundcloud, "sc_validate", return_value={"id": 7}),
            patch.object(soundcloud, "sc_client_id", return_value="synthetic-client"),
            patch.object(soundcloud.requests, "get", side_effect=responses) as request,
        ):
            result = soundcloud.sc_account_playlists("synthetic-token")
        self.assertEqual(120, len(result))
        self.assertEqual([], result.errors)
        self.assertEqual(4, request.call_count)

    def test_soundcloud_liked_playlists_use_user_likes_and_preserve_pagination(self):
        own = {"id": 1, "title": "Mine", "permalink_url": "https://soundcloud.com/me/mine", "track_count": 1}
        liked = {"id": 2, "title": "Liked", "permalink_url": "https://soundcloud.com/artist/set", "track_count": 3}
        next_url = "https://api-v2.soundcloud.com/users/7/likes?cursor=next"
        pages = [
            FakeResponse({"collection": [own]}),
            FakeResponse({"collection": [{"kind": "like", "track": {"id": 3}},
                                         {"kind": "like", "playlist": own}], "next_href": next_url}),
            FakeResponse({"collection": [{"kind": "like", "playlist": liked}]}),
        ]
        with (
            patch.object(soundcloud, "sc_validate", return_value={"id": 7}),
            patch.object(soundcloud, "sc_client_id", return_value="synthetic-client"),
            patch.object(soundcloud.requests, "get", side_effect=pages) as request,
        ):
            result = soundcloud.sc_account_playlists("synthetic-token")
        self.assertEqual(["1", "2"], [item["id"] for item in result])
        self.assertEqual("♥ Liked", result[1]["title"])
        self.assertEqual([], result.errors)
        self.assertEqual("https://api-v2.soundcloud.com/users/7/likes", request.call_args_list[1].args[0])
        self.assertEqual(next_url, request.call_args_list[2].args[0])
        for call in request.call_args_list:
            headers = call.kwargs['headers']
            self.assertEqual('OAuth synthetic-token', headers['Authorization'])
            self.assertEqual('https://soundcloud.com', headers.get('Origin'))
            self.assertEqual('https://soundcloud.com/', headers.get('Referer'))
            self.assertIn('Mozilla/', headers.get('User-Agent', ''))

    def test_soundcloud_malformed_liked_playlist_is_reported_as_partial(self):
        with (
            patch.object(soundcloud, "sc_validate", return_value={"id": 7}),
            patch.object(soundcloud, "_account_pages", side_effect=[
                iter([{"collection": []}]), iter([{"collection": [{"kind": "like", "playlist": None}]}]),
            ]),
        ):
            result = soundcloud.sc_account_playlists("synthetic-token")
        self.assertEqual("provider_collection_incomplete", result.errors[0]["code"])

    def test_search_preserves_partial_results_and_adds_safe_service_errors(self):
        def sc_get(path, _token, **_params):
            if path == "/search/tracks":
                return {"collection": [{"id": 1, "title": "Found", "user": {"username": "Artist"}, "duration": 1000}]}
            raise ConnectionError("raw provider response")

        with patch.object(soundcloud, "_api_get", side_effect=sc_get):
            result = soundcloud.search("query")
        self.assertEqual(["1"], [track["id"] for track in result["tracks"]])
        self.assertEqual(2, len(result["errors"]))
        self.assertTrue(all(error["service"] == "soundcloud" for error in result["errors"]))
        self.assertNotIn("raw provider response", str(result))

    def test_soundcloud_search_marks_malformed_success_page_incomplete(self):
        def sc_get(path, _token, **_params):
            if path == "/search/albums":
                return {}
            return {"collection": []}

        with patch.object(soundcloud, "_api_get", side_effect=sc_get):
            result = soundcloud.search("query")
        self.assertEqual(1, len(result["errors"]))
        self.assertEqual("provider_collection_incomplete", result["errors"][0]["code"])

    def test_soundcloud_playlist_keeps_valid_tracks_and_marks_incomplete_entries(self):
        payload = {
            "kind": "playlist",
            "id": 8,
            "title": "Synthetic",
            "tracks": [
                {"id": 1, "title": "One", "user": {"username": "Artist"}, "duration": 1000},
                {"title": "Unavailable"},
            ],
        }
        with patch.object(soundcloud, "_api_get", return_value=payload):
            result = soundcloud._resolve_api("https://soundcloud.com/synthetic/set", None)
        self.assertEqual(["1"], [track["id"] for track in result["tracks"]])
        self.assertEqual("provider_collection_incomplete", result["errors"][0]["code"])

    def test_soundcloud_partial_resolve_is_not_cached_as_complete(self):
        partial = {
            "id": "set-1",
            "title": "Partial",
            "tracks": [{"id": "1", "title": "One"}],
            "errors": [{
                "code": "provider_collection_incomplete",
                "service": "soundcloud",
                "message": "SoundCloud returned an incomplete collection",
                "retryable": True,
            }],
        }
        cache = {}
        with (
            patch.object(soundcloud, "_cache", cache),
            patch.object(soundcloud, "sc_oauth_token", return_value=None),
            patch.object(soundcloud, "_resolve_api", return_value=partial),
        ):
            result = soundcloud.resolve("https://soundcloud.com/synthetic/set")
        self.assertEqual(partial, result)
        self.assertEqual({}, cache)

    def test_sc_source_visibility_retains_manual_and_legacy_but_hides_other_accounts(self):
        cfg = {
            "sc_account_id": "account-a",
            "sc_sources": [
                {"id": "manual", "source": "manual"},
                {"id": "legacy"},
                {"id": "mine", "source": "account", "account_id": "account-a"},
                {"id": "other", "source": "account", "account_id": "account-b"},
            ],
        }
        with patch.object(main, "load_config", return_value=cfg):
            visible = main._sc_sources()
        self.assertEqual(["manual", "legacy", "mine"], [source["id"] for source in visible])

    def test_account_import_does_not_let_hidden_other_account_block_current_source(self):
        cfg = {
            "sc_account_id": "account-a",
            "sc_sources": [
                {"id": "other", "url": "https://soundcloud.com/shared/set", "source": "account", "account_id": "account-b"},
            ],
        }
        saved = []
        with (
            patch.object(main, "load_config", return_value=cfg),
            patch.object(main, "_sc_save_sources", side_effect=lambda sources: saved.extend(sources)),
        ):
            result = main.api_sc_import(main.ScImportIn(items=[{
                "id": "mine", "url": "https://soundcloud.com/shared/set", "title": "Mine",
            }]))
        self.assertEqual({"added": 1}, result)
        self.assertEqual("account-a", saved[-1]["account_id"])


if __name__ == "__main__":
    unittest.main()
