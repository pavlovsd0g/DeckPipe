import asyncio
import importlib
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI

try:
    from app import activity_log
except ImportError:
    activity_log = None


EVIDENCE_ROOT = Path(r"D:\DeckPipe-RC-Lab\qa-evidence\github-release-20260916\log-tests")


async def request(app, method, path, *, query=b"", body=b"", headers=None):
    messages = []
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "scheme": "http", "method": method, "path": path, "raw_path": path.encode(),
        "query_string": query, "root_path": "", "headers": headers or [],
        "server": ("127.0.0.1", 12345), "client": ("127.0.0.1", 50000),
    }
    await app(scope, receive, send)
    status = next(item["status"] for item in messages if item["type"] == "http.response.start")
    payload = b"".join(item.get("body", b"") for item in messages if item["type"] == "http.response.body")
    return status, json.loads(payload) if payload else None


class ActivityLogTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(activity_log, "persistent activity log module is not implemented")
        EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="activity-", dir=EVIDENCE_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        state = patch.multiple(activity_log, _directory=None, _last_id=0, _warned=set())
        state.start()
        self.addCleanup(state.stop)
        activity_log.initialize(self.root)
        self.app = FastAPI()
        self.app.include_router(activity_log.router)

    def page(self, query=b""):
        status, payload = asyncio.run(request(self.app, "GET", "/api/logs", query=query))
        self.assertEqual(200, status)
        return payload

    def emit(self, index):
        activity_log.record("info", "download", "track_started", f"Track {index} started", job_id="job-1", track_id=index)

    def test_record_is_durable_and_readable_after_process_state_is_reloaded(self):
        self.emit(1)
        first = self.page()["entries"][0]
        self.assertEqual(("info", "download", "track_started", 1), (first["level"], first["operation"], first["stage"], first["track_id"]))
        self.assertRegex(first["time"], r"^\d{4}-\d\d-\d\dT.*Z$")
        self.assertRegex(first["id"], r"^\d{20}$")
        persisted = [json.loads(line) for line in (self.root / "logs" / "activity.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([first], persisted)
        importlib.reload(activity_log)
        activity_log.initialize(self.root)
        self.emit(2)
        entries = self.page()["entries"]
        self.assertEqual([2, 1], [row["track_id"] for row in entries])
        self.assertGreater(entries[0]["id"], first["id"])

    def test_pagination_is_exclusive_and_stable_when_new_entries_arrive(self):
        for index in range(5):
            self.emit(index)
        first = self.page(b"limit=2")
        self.assertEqual([4, 3], [row["track_id"] for row in first["entries"]])
        self.assertTrue(first["has_more"])
        self.assertEqual(first["entries"][-1]["id"], first["next_before"])
        self.emit(5)
        second = self.page(f"limit=2&before={first['next_before']}".encode())
        self.assertEqual([2, 1], [row["track_id"] for row in second["entries"]])
        third = self.page(f"limit=2&cursor={second['next_before']}".encode())
        self.assertEqual([0], [row["track_id"] for row in third["entries"]])
        self.assertFalse(third["has_more"])
        self.assertIsNone(third["next_before"])

    def test_limits_and_cursor_reject_invalid_or_conflicting_input(self):
        for query in (b"limit=0", b"limit=501", b"before=not-an-id", b"before=00000000000000000001&cursor=00000000000000000002"):
            with self.subTest(query=query):
                status, _ = asyncio.run(request(self.app, "GET", "/api/logs", query=query))
                self.assertIn(status, (400, 422))

    def test_empty_journal_returns_explicit_empty_page(self):
        self.assertEqual({"entries": [], "next_before": None, "has_more": False}, self.page())

    def test_retention_caps_disk_usage_and_keeps_latest_complete_records(self):
        with patch.object(activity_log, "MAX_FILE_BYTES", 4096), patch.object(activity_log, "MAX_FILES", 3):
            for index in range(120):
                activity_log.record("info", "download", "progress", "Synthetic progress " + "я" * 200, track_id=index)
            files = list((self.root / "logs").glob("activity*.jsonl"))
            self.assertLessEqual(len(files), 3)
            self.assertLessEqual(sum(path.stat().st_size for path in files), 3 * 4096)
            retained = self.page(b"limit=500")["entries"]
            self.assertLess(len(retained), 120)
            self.assertGreater(len(retained), 1)
            self.assertEqual(119, retained[0]["track_id"])
            self.assertEqual(list(range(119, 119 - len(retained), -1)), [row["track_id"] for row in retained])
            activity_log.initialize(self.root)
            self.assertEqual(retained, self.page(b"limit=500")["entries"])

    def test_sensitive_message_values_and_unapproved_context_never_reach_disk(self):
        examples = [
            ("Authorization: Bearer auth-secret-17", "auth-secret-17"),
            ("Cookie: arl=cookie-secret-18; session=private-session", "private-session"),
            ("ARL=arl-secret-19", "arl-secret-19"),
            ('password="two private words"', "two private words"),
            ('{"access_token": "access-secret-20", "refresh_token": "refresh-secret-21"}', "access-secret-20"),
            ("Provider failed at https://user:pass@example.test/private?token=url-secret-22", "example.test"),
            ("Retry ?search=query-secret-23&next=other-private", "query-secret-23"),
            ("token token-secret-24", "token-secret-24"),
            ("OAuth oauth-secret-25", "oauth-secret-25"),
            ("Bare credential " + "a" * 192, "a" * 192),
        ]
        for message, _ in examples:
            activity_log.record("error", "download", "failed", message, job_id="safe-job", track_id="safe-track", playlist_id="safe-playlist", error_code="provider_unavailable", password="context-secret", body={"token": "body-secret"}, path="private-path")
        data = (self.root / "logs" / "activity.jsonl").read_text(encoding="utf-8")
        for _, secret in examples:
            self.assertNotIn(secret, data)
        for secret in ("context-secret", "body-secret", "private-path", "refresh-secret-21", "cookie-secret-18", "other-private"):
            self.assertNotIn(secret, data)
        row = self.page()["entries"][0]
        self.assertEqual("safe-job", row["job_id"])
        self.assertEqual("provider_unavailable", row["error_code"])
        self.assertEqual({"id", "time", "level", "operation", "stage", "message", "job_id", "track_id", "playlist_id", "error_code"}, set(row))

    def test_non_scalar_context_and_invalid_labels_cannot_serialize_objects_or_secrets(self):
        class Dangerous:
            def __str__(self):
                raise RuntimeError("do not stringify objects")

        activity_log.record("CUSTOM-SECRET", "https://operation-secret.test", "password=stage-secret", "Safe message", job_id=Dangerous(), track_id={"token": "dict-secret"}, playlist_id=float("nan"), error_code="Authorization: Bearer error-secret")
        row = self.page()["entries"][0]
        self.assertEqual("info", row["level"])
        self.assertEqual("unknown", row["operation"])
        self.assertEqual("unknown", row["stage"])
        self.assertNotIn("job_id", row)
        self.assertNotIn("track_id", row)
        self.assertNotIn("playlist_id", row)
        self.assertNotIn("secret", json.dumps(row))

    def test_concurrent_writers_produce_unique_complete_ordered_events(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(self.emit, range(160)))
        rows = self.page(b"limit=500")["entries"]
        self.assertEqual(160, len(rows))
        self.assertEqual(set(range(160)), {row["track_id"] for row in rows})
        ids = [row["id"] for row in rows]
        self.assertEqual(160, len(set(ids)))
        self.assertEqual(sorted(ids, reverse=True), ids)
        self.assertEqual(160, len((self.root / "logs" / "activity.jsonl").read_text(encoding="utf-8").splitlines()))

    def test_partial_last_line_and_invalid_rows_do_not_hide_later_valid_events(self):
        self.emit(1)
        path = self.root / "logs" / "activity.jsonl"
        with path.open("ab") as stream:
            stream.write(b'{"not-an-entry":"ignored"}\n{"id":"partial')
        activity_log.initialize(self.root)
        self.emit(2)
        self.assertEqual([2, 1], [row["track_id"] for row in self.page()["entries"]])

    def test_unavailable_storage_is_best_effort_and_recovers_without_secret_error_dump(self):
        blocker = self.root / "blocked"
        blocker.write_text("synthetic blocker", encoding="utf-8")
        with self.assertLogs("app.activity_log", level="WARNING") as captured:
            activity_log.initialize(blocker)
            activity_log.record("error", "download", "failed", "password=private-message")
        self.assertNotIn(str(blocker), " ".join(captured.output))
        self.assertNotIn("private-message", " ".join(captured.output))
        activity_log.initialize(self.root)
        self.emit(2)
        self.assertEqual([2], [row["track_id"] for row in self.page()["entries"]])

    def test_failed_write_does_not_raise_or_remove_existing_events(self):
        self.emit(1)
        real_open = Path.open

        def unavailable(path, mode="r", *args, **kwargs):
            if path.name == "activity.jsonl" and "a" in mode:
                raise OSError("Authorization: Bearer disk-secret")
            return real_open(path, mode, *args, **kwargs)

        with patch.object(Path, "open", unavailable), self.assertLogs("app.activity_log", level="WARNING") as captured:
            self.emit(2)
        self.assertNotIn("disk-secret", " ".join(captured.output))
        self.assertEqual([1], [row["track_id"] for row in self.page()["entries"]])
        self.emit(3)
        self.assertEqual([3, 1], [row["track_id"] for row in self.page()["entries"]])

    def test_failed_fsync_cannot_reuse_id_when_the_clock_does_not_advance(self):
        with patch.object(activity_log.time, "time_ns", return_value=1700000000000000000):
            self.emit(1)
            with patch.object(activity_log.os, "fsync", side_effect=OSError("token=fsync-secret")), self.assertLogs("app.activity_log", level="WARNING") as captured:
                self.emit(2)
            self.emit(3)
        self.assertNotIn("fsync-secret", " ".join(captured.output))
        rows = self.page()["entries"]
        self.assertEqual([3, 2, 1], [row["track_id"] for row in rows])
        self.assertEqual(3, len({row["id"] for row in rows}))

    def test_failed_read_returns_empty_page_without_raw_exception(self):
        self.emit(1)
        real_open = Path.open

        def unavailable(path, mode="r", *args, **kwargs):
            if path.name.startswith("activity") and mode == "rb":
                raise OSError("private-read-path token=read-secret")
            return real_open(path, mode, *args, **kwargs)

        with patch.object(Path, "open", unavailable), self.assertLogs("app.activity_log", level="WARNING") as captured:
            self.assertEqual([], self.page()["entries"])
        self.assertNotIn("read-secret", " ".join(captured.output))
        self.assertNotIn("private-read-path", " ".join(captured.output))
        self.assertEqual([1], [row["track_id"] for row in self.page()["entries"]])

    def test_broken_fallback_handler_cannot_break_the_calling_operation(self):
        with patch.object(activity_log, "_directory", None), patch.object(activity_log._logger, "warning", side_effect=RuntimeError("broken logging handler")):
            self.assertIsNone(activity_log.record("info", "download", "started", "Started."))

    def test_default_page_is_bounded_and_larger_allowed_page_retains_older_entries(self):
        for index in range(220):
            self.emit(index)
        first = self.page()
        self.assertEqual(200, len(first["entries"]))
        self.assertTrue(first["has_more"])
        self.assertEqual(219, first["entries"][0]["track_id"])
        self.assertEqual(20, first["entries"][-1]["track_id"])
        self.assertEqual(220, len(self.page(b"limit=500")["entries"]))

    def test_dump_shaped_details_and_unpunctuated_cookies_are_redacted(self):
        for message in (
            "Cookies private-cookie-value",
            'Provider body {"unrecognized_field": "private-body-value"}',
            "Traceback (most recent call last):\n  File D:/private-path.py\nError: private-trace-value",
        ):
            activity_log.record("error", "download", "failed", message)
        serialized = (self.root / "logs" / "activity.jsonl").read_text(encoding="utf-8")
        for value in ("private-cookie-value", "private-body-value", "private-path.py", "private-trace-value"):
            self.assertNotIn(value, serialized)

    def test_ui_event_cancellation_is_mapped_and_arbitrary_event_fields_are_rejected(self):
        headers = [(b"content-type", b"application/json")]
        status, body = asyncio.run(request(self.app, "POST", "/api/logs/events", body=json.dumps({"event": "rekordbox_sync_cancelled", "message": "Cancelled; token=ui-secret"}).encode(), headers=headers))
        self.assertEqual((200, {"accepted": True}), (status, body))
        row = self.page()["entries"][0]
        self.assertEqual(("rekordbox", "sync_cancelled", "warning"), (row["operation"], row["stage"], row["level"]))
        self.assertNotIn("ui-secret", row["message"])
        for payload in ({"event": "arbitrary"}, {"event": "rekordbox_sync_failed", "level": "debug"}, {"event": "rekordbox_sync_succeeded", "message": "x" * 501}):
            status, _ = asyncio.run(request(self.app, "POST", "/api/logs/events", body=json.dumps(payload).encode(), headers=headers))
            self.assertEqual(422, status)
        self.assertEqual(1, len(self.page()["entries"]))

    def test_http_middleware_records_stage_and_status_without_request_or_response_secrets(self):
        async def downstream(scope, receive, send):
            await send({"type": "http.response.start", "status": 409, "headers": [(b"x-private", b"response-secret")]})
            await send({"type": "http.response.body", "body": b'{"detail":"body-secret"}'})

        wrapped = activity_log.ActivityLogMiddleware(downstream)
        status, _ = asyncio.run(request(wrapped, "POST", "/api/playlists/private-path-value/download", query=b"token=query-secret", body=b"request-body-secret", headers=[(b"authorization", b"Bearer header-secret")]))
        self.assertEqual(409, status)
        rows = self.page()["entries"]
        self.assertEqual(["request_failed", "request_started"], [row["stage"] for row in rows])
        self.assertEqual("http_409", rows[0]["error_code"])
        self.assertEqual("download", rows[0]["operation"])
        serialized = json.dumps(rows)
        for secret in ("private-path-value", "query-secret", "request-body-secret", "header-secret", "body-secret", "response-secret"):
            self.assertNotIn(secret, serialized)

    def test_http_exception_is_logged_safely_and_still_propagates(self):
        async def broken(scope, receive, send):
            raise ValueError("token=exception-secret")

        with self.assertRaisesRegex(ValueError, "exception-secret"):
            asyncio.run(request(activity_log.ActivityLogMiddleware(broken), "POST", "/api/rb/sync"))
        rows = self.page()["entries"]
        self.assertEqual("request_failed", rows[0]["stage"])
        self.assertEqual("request_exception", rows[0]["error_code"])
        self.assertNotIn("exception-secret", json.dumps(rows))

    def test_successful_http_operation_preserves_response_and_logs_completion(self):
        async def downstream(scope, receive, send):
            await send({"type": "http.response.start", "status": 202, "headers": []})
            await send({"type": "http.response.body", "body": b'{"accepted":true}'})

        result = asyncio.run(request(activity_log.ActivityLogMiddleware(downstream), "POST", "/api/library/scan"))
        self.assertEqual((202, {"accepted": True}), result)
        self.assertEqual(["request_finished", "request_started"], [row["stage"] for row in self.page()["entries"]])

    def test_auth_log_and_polling_routes_are_not_recorded_or_recursively_logged(self):
        async def downstream(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        wrapped = activity_log.ActivityLogMiddleware(downstream)
        for method, path in [("POST", "/api/auth/deezer/start"), ("POST", "/api/login/deezer"), ("GET", "/api/logs"), ("GET", "/api/jobs"), ("GET", "/api/jobs/job-1"), ("GET", "/api/library/status"), ("GET", "/api/rb/status")]:
            asyncio.run(request(wrapped, method, path))
        self.assertEqual([], self.page()["entries"])


if __name__ == "__main__":
    unittest.main()
