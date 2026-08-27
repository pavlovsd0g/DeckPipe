from __future__ import annotations

import contextlib
import asyncio
import importlib
import io
import json
import os
import sys
import tempfile
import traceback
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
TASK_MODULES = (
    "app.main",
    "app.bugreport",
    "app.soundcloud",
    "app.deezer_client",
    "app.secure_store",
)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def generated_values() -> dict[str, str]:
    return {
        "deezer": f"generated-dz-{uuid.uuid4().hex}",
        "soundcloud": f"generated-sc-{uuid.uuid4().hex}",
        "telegram": f"{uuid.uuid4().int % 900000000 + 100000000}:{uuid.uuid4().hex}",
    }


@contextlib.contextmanager
def isolated_app_modules(data_dir: Path):
    old_data_dir = os.environ.get("DECKPIPE_DATA_DIR")
    old_token = os.environ.get("DECKPIPE_API_TOKEN")
    old_port = os.environ.get("DECKPIPE_BOUND_PORT")
    os.environ["DECKPIPE_DATA_DIR"] = str(data_dir)
    os.environ["DECKPIPE_API_TOKEN"] = "secure-store-test-launch-token"
    os.environ["DECKPIPE_BOUND_PORT"] = "8123"
    for name in TASK_MODULES:
        sys.modules.pop(name, None)
    try:
        yield
    finally:
        for name in TASK_MODULES:
            sys.modules.pop(name, None)
        if old_data_dir is None:
            os.environ.pop("DECKPIPE_DATA_DIR", None)
        else:
            os.environ["DECKPIPE_DATA_DIR"] = old_data_dir
        if old_token is None:
            os.environ.pop("DECKPIPE_API_TOKEN", None)
        else:
            os.environ["DECKPIPE_API_TOKEN"] = old_token
        if old_port is None:
            os.environ.pop("DECKPIPE_BOUND_PORT", None)
        else:
            os.environ["DECKPIPE_BOUND_PORT"] = old_port


class RecordingDpapi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str | None, bytes | None]] = []

    def protect(self, plaintext: bytes, *, description: str | None, optional_entropy, flags: int) -> bytes:
        self.calls.append(("protect", flags, description, optional_entropy))
        return b"recording-dpapi:" + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, optional_entropy, flags: int) -> bytes:
        self.calls.append(("unprotect", flags, None, optional_entropy))
        prefix = b"recording-dpapi:"
        if not ciphertext.startswith(prefix):
            raise RuntimeError("recording decrypt failure")
        return ciphertext[len(prefix):][::-1]


class FailingDpapi:
    def protect(self, plaintext: bytes, *, description: str | None, optional_entropy, flags: int) -> bytes:
        raise RuntimeError("injected dependency saw " + plaintext.decode("utf-8", errors="replace"))

    def unprotect(self, ciphertext: bytes, *, optional_entropy, flags: int) -> bytes:
        raise RuntimeError("injected dependency failure " + getattr(self, "leak_value", ""))


def formatted_exception(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


@unittest.skipUnless(os.name == "nt", "Windows current-user DPAPI is required")
class SecureStoreTests(unittest.TestCase):
    maxDiff = None

    def test_real_dpapi_roundtrip_encrypts_one_purpose_bound_envelope(self) -> None:
        values = generated_values()
        with tempfile.TemporaryDirectory() as tmp:
            with isolated_app_modules(Path(tmp)):
                secure_store = importlib.import_module("app.secure_store")
                store_path = Path(tmp) / "secrets.dpapi"
                store = secure_store.SecureCredentialStore(store_path)

                store.set_deezer_arl(values["deezer"])
                store.set_soundcloud_oauth(values["soundcloud"])

                self.assertTrue(store.get_deezer_arl() == values["deezer"])
                self.assertTrue(store.get_soundcloud_oauth() == values["soundcloud"])

                raw = read_text(store_path)
                self.assertFalse(
                    any(value in raw for value in values.values()),
                    "secure store contains a generated secret value",
                )
                payload = json.loads(raw)
                self.assertEqual({"version", "purpose", "ciphertext"}, set(payload))
                self.assertEqual(1, payload["version"])
                self.assertEqual(secure_store.STORE_FILE_PURPOSE, payload["purpose"])
                self.assertIsInstance(payload["ciphertext"], str)
                self.assertNotIn("records", payload)
                self.assertNotIn("deezer_arl", raw)
                self.assertNotIn("soundcloud_oauth", raw)

    def test_dpapi_boundary_uses_current_user_no_ui_and_exact_native_signatures(self) -> None:
        values = generated_values()
        with tempfile.TemporaryDirectory() as tmp:
            with isolated_app_modules(Path(tmp)):
                secure_store = importlib.import_module("app.secure_store")
                recorder = RecordingDpapi()
                store = secure_store.SecureCredentialStore(Path(tmp) / "secrets.dpapi", dpapi=recorder)

                store.set_deezer_arl(values["deezer"])
                self.assertTrue(store.get_deezer_arl() == values["deezer"])

                self.assertGreaterEqual(len(recorder.calls), 2)
                for _kind, flags, description, entropy in recorder.calls:
                    self.assertEqual(secure_store.CRYPTPROTECT_UI_FORBIDDEN, flags)
                    self.assertFalse(flags & secure_store.CRYPTPROTECT_LOCAL_MACHINE)
                    self.assertIsNone(description)
                    self.assertIsNone(entropy)

                self.assertEqual("CryptProtectData", secure_store._crypt32().CryptProtectData.__name__)
                self.assertIsNotNone(secure_store._crypt32().CryptProtectData.argtypes)
                self.assertIsNotNone(secure_store._crypt32().CryptUnprotectData.argtypes)
                self.assertIsNotNone(secure_store._kernel32().LocalFree.argtypes)

    def test_store_preserves_records_and_fails_closed_on_corrupt_or_wrong_schema(self) -> None:
        values = generated_values()
        with tempfile.TemporaryDirectory() as tmp:
            with isolated_app_modules(Path(tmp)):
                secure_store = importlib.import_module("app.secure_store")
                store_path = Path(tmp) / "secrets.dpapi"
                store = secure_store.SecureCredentialStore(store_path)
                store.set_deezer_arl(values["deezer"])
                store.set_soundcloud_oauth(values["soundcloud"])
                store.set_deezer_arl(values["deezer"] + "-rotated")

                self.assertTrue(store.get_deezer_arl() == values["deezer"] + "-rotated")
                self.assertTrue(store.get_soundcloud_oauth() == values["soundcloud"])

                for payload in [
                    "not json",
                    json.dumps({"version": 1, "purpose": "wrong", "ciphertext": "AA=="}),
                    json.dumps({"version": 1, "purpose": secure_store.STORE_FILE_PURPOSE, "ciphertext": "not-base64"}),
                ]:
                    store_path.write_text(payload, encoding="utf-8")
                    with self.assertRaises(secure_store.SecureStoreError) as captured:
                        store.get_deezer_arl()
                    self.assertNotIn(values["deezer"], str(captured.exception))
                    self.assertNotIn(values["soundcloud"], str(captured.exception))

                recorder = RecordingDpapi()
                protected = recorder.protect(
                    json.dumps({"version": 1, "purpose": "wrong", "records": {}}).encode("utf-8"),
                    description=None,
                    optional_entropy=None,
                    flags=secure_store.CRYPTPROTECT_UI_FORBIDDEN,
                )
                store_path.write_text(
                    json.dumps(
                        {
                            "version": 1,
                            "purpose": secure_store.STORE_FILE_PURPOSE,
                            "ciphertext": secure_store._b64encode(protected),
                        }
                    ),
                    encoding="utf-8",
                )
                with self.assertRaises(secure_store.SecureStoreError):
                    secure_store.SecureCredentialStore(store_path, dpapi=recorder).get_deezer_arl()

    def test_preferences_never_return_or_accept_legacy_secret_fields(self) -> None:
        values = generated_values()
        with tempfile.TemporaryDirectory() as tmp:
            with isolated_app_modules(Path(tmp)):
                deezer_client = importlib.import_module("app.deezer_client")

                deezer_client.save_config({"music_root": "X:\\DeckPipeTest", "numbering": False})
                deezer_client.set_deezer_arl(values["deezer"])
                deezer_client.set_soundcloud_oauth(values["soundcloud"])

                loaded = deezer_client.load_config()
                self.assertEqual({"music_root", "numbering"}, set(loaded))
                self.assertFalse(any(value in json.dumps(loaded) for value in values.values()))
                self.assertTrue(deezer_client.get_deezer_arl() == values["deezer"])
                self.assertTrue(deezer_client.get_soundcloud_oauth() == values["soundcloud"])

                for field in ("arl", "sc_oauth"):
                    with self.assertRaises(ValueError):
                        deezer_client.save_config({field: values["deezer"], "music_root": "X:\\DeckPipeTest"})

                config_path = Path(tmp) / "config.local.json"
                config_path.write_text(
                    json.dumps(
                        {
                            "telegram": {
                                "bot_token": values["telegram"],
                                "chat_id": "generated-chat",
                                "enabled": True,
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                loaded = deezer_client.load_config()
                self.assertEqual({"chat_id": "generated-chat", "enabled": True}, loaded["telegram"])
                self.assertNotIn(values["telegram"], json.dumps(loaded))
                with self.assertRaises(ValueError):
                    deezer_client.save_config({"telegram": {"bot_token": values["telegram"], "chat_id": "generated-chat"}})

    def test_data_dir_override_is_source_test_only_and_import_is_not_mutating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            override = base / "override"
            appdata = base / "appdata"
            fake_home = base / "home"
            with patch.object(sys, "frozen", False, create=True):
                with isolated_app_modules(override):
                    deezer_client = importlib.import_module("app.deezer_client")
                    self.assertEqual(override, deezer_client.ROOT)
                    self.assertFalse(override.exists())

            with patch.object(sys, "frozen", True, create=True):
                with patch.dict(
                    os.environ,
                    {
                        "DECKPIPE_DATA_DIR": str(override),
                        "APPDATA": str(appdata),
                        "DECKPIPE_API_TOKEN": "secure-store-test-launch-token",
                        "DECKPIPE_BOUND_PORT": "8123",
                    },
                    clear=False,
                ):
                    for name in TASK_MODULES:
                        sys.modules.pop(name, None)
                    try:
                        deezer_client = importlib.import_module("app.deezer_client")
                        self.assertEqual(appdata / "DeckPipe", deezer_client.ROOT)
                        self.assertFalse((appdata / "DeckPipe").exists())
                    finally:
                        for name in TASK_MODULES:
                            sys.modules.pop(name, None)

            with patch.object(sys, "frozen", False, create=True):
                with patch.object(Path, "home", return_value=fake_home):
                    with patch.dict(
                        os.environ,
                        {
                            "DECKPIPE_API_TOKEN": "secure-store-test-launch-token",
                            "DECKPIPE_BOUND_PORT": "8123",
                        },
                        clear=True,
                    ):
                        for name in TASK_MODULES:
                            sys.modules.pop(name, None)
                        with self.assertRaises(RuntimeError) as captured:
                            importlib.import_module("app.deezer_client")
                        formatted = formatted_exception(captured.exception)
                        self.assertIn("DeckPipe data directory is unavailable", formatted)
                        self.assertNotIn(str(fake_home), formatted)
                        self.assertFalse((fake_home / ".deckpipe").exists())
                        for name in TASK_MODULES:
                            sys.modules.pop(name, None)

    def test_migration_is_explicit_crash_safe_idempotent_and_readback_verified(self) -> None:
        values = generated_values()
        with tempfile.TemporaryDirectory() as tmp:
            with isolated_app_modules(Path(tmp)):
                secure_store = importlib.import_module("app.secure_store")
                config_path = Path(tmp) / "config.local.json"
                store_path = Path(tmp) / "secrets.dpapi"
                legacy = {
                    "arl": values["deezer"],
                    "sc_oauth": values["soundcloud"],
                    "telegram": {
                        "bot_token": values["telegram"],
                        "chat_id": "generated-chat",
                        "enabled": True,
                    },
                    "sc_username": "generated-user",
                    "music_root": "X:\\DeckPipeTest",
                    "numbering": True,
                }
                config_path.write_text(json.dumps(legacy, ensure_ascii=False, indent=2), encoding="utf-8")
                original = read_text(config_path)

                def fail_store_replace(src: Path, dst: Path) -> None:
                    if Path(dst) == store_path:
                        raise secure_store.SecureStoreError("injected atomic replace failure")
                    os.replace(src, dst)

                def fail_readback(_store, _records) -> None:
                    raise secure_store.SecureStoreError("injected readback failure")

                def fail_config_replace(src: Path, dst: Path) -> None:
                    if Path(dst) == config_path:
                        raise secure_store.SecureStoreError("injected config replace failure")
                    os.replace(src, dst)

                failure_cases = (
                    {"replace": fail_store_replace},
                    {"readback_validator": fail_readback},
                    {"replace": fail_config_replace},
                )
                for kwargs in failure_cases:
                    store_path.unlink(missing_ok=True)
                    config_path.write_text(original, encoding="utf-8")
                    with self.assertRaises(secure_store.SecureStoreError):
                        secure_store.migrate_legacy_config(
                            config_path=config_path,
                            store_path=store_path,
                            **kwargs,
                        )
                    self.assertEqual(original, read_text(config_path))
                    for artifact in Path(tmp).iterdir():
                        if artifact.is_file():
                            raw = artifact.read_text(encoding="utf-8", errors="ignore")
                            if artifact.name != "config.local.json":
                                self.assertFalse(
                                    any(value in raw for value in values.values()),
                                    "migration created a plaintext credential artifact",
                                )

                result = secure_store.migrate_legacy_config(config_path=config_path, store_path=store_path)
                self.assertEqual(("arl", "sc_oauth", "telegram_bot_token"), result.migrated_fields)
                self.assertFalse(any(value in repr(result) for value in values.values()))

                migrated = json.loads(read_text(config_path))
                self.assertEqual(
                    {
                        "telegram": {"chat_id": "generated-chat", "enabled": True},
                        "sc_username": "generated-user",
                        "music_root": "X:\\DeckPipeTest",
                        "numbering": True,
                    },
                    migrated,
                )
                store = secure_store.SecureCredentialStore(store_path)
                self.assertTrue(store.get_deezer_arl() == values["deezer"])
                self.assertTrue(store.get_soundcloud_oauth() == values["soundcloud"])
                self.assertTrue(store.get_telegram_bot_token() == values["telegram"])

                second = secure_store.migrate_legacy_config(config_path=config_path, store_path=store_path)
                self.assertEqual((), second.migrated_fields)
                self.assertEqual(("arl", "sc_oauth", "telegram_bot_token"), second.already_present_fields)

    def test_startup_lifespan_runs_migration_only_inside_injected_data_dir(self) -> None:
        values = generated_values()
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            with isolated_app_modules(data_dir):
                config_path = data_dir / "config.local.json"
                config_path.write_text(
                    json.dumps(
                        {
                            "arl": values["deezer"],
                            "sc_oauth": values["soundcloud"],
                            "telegram": {"bot_token": values["telegram"], "chat_id": "generated-chat"},
                            "theme": "dark",
                        }
                    ),
                    encoding="utf-8",
                )
                main = importlib.import_module("app.main")
                self.assertIn("arl", json.loads(read_text(config_path)))

                async def enter_lifespan() -> None:
                    async with main.app.router.lifespan_context(main.app):
                        pass

                asyncio.run(enter_lifespan())

                self.assertEqual({"telegram": {"chat_id": "generated-chat"}, "theme": "dark"}, json.loads(read_text(config_path)))
                secure_store = importlib.import_module("app.secure_store")
                store = secure_store.SecureCredentialStore(data_dir / "secrets.dpapi")
                self.assertTrue(store.get_deezer_arl() == values["deezer"])
                self.assertTrue(store.get_soundcloud_oauth() == values["soundcloud"])
                self.assertTrue(store.get_telegram_bot_token() == values["telegram"])

    def test_consumers_use_explicit_secret_accessors_and_no_browser_bridge_route(self) -> None:
        sources = {
            "app/deezer_client.py": read_text(ROOT / "app" / "deezer_client.py"),
            "app/soundcloud.py": read_text(ROOT / "app" / "soundcloud.py"),
            "app/main.py": read_text(ROOT / "app" / "main.py"),
        }
        combined = "\n".join(sources.values())

        self.assertNotIn('load_config().get("arl")', combined)
        self.assertNotIn('load_config().get("sc_oauth")', combined)
        self.assertNotIn('cfg["arl"]', combined)
        self.assertNotIn('cfg["sc_oauth"]', combined)
        self.assertNotIn('tg.get("bot_token")', read_text(ROOT / "app" / "bugreport.py"))
        self.assertIn("get_telegram_bot_token(", read_text(ROOT / "app" / "bugreport.py"))
        self.assertNotIn("LoginFromBrowserIn", sources["app/main.py"])
        self.assertNotIn('/api/login/from-browser', sources["app/main.py"])
        self.assertIn("set_deezer_arl(", sources["app/main.py"])
        self.assertIn("set_soundcloud_oauth(", sources["app/main.py"])
        save_config_block = sources["app/deezer_client.py"].split("def save_config", 1)[1].split("def sanitize_filename", 1)[0]
        self.assertNotIn("password", save_config_block)

    def test_soundcloud_yt_dlp_receives_only_scoped_in_memory_cookie_jar(self) -> None:
        values = generated_values()
        captured: dict[str, object] = {}

        class FakeYoutubeDL:
            def __init__(self, opts):
                captured["opts"] = opts

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def extract_info(self, _url, download=False):
                return {
                    "id": "likes",
                    "title": "Likes",
                    "entries": [{"id": "1", "title": "Track", "uploader": "User", "duration": 12, "url": "https://soundcloud.com/u/t"}],
                }

        with tempfile.TemporaryDirectory() as tmp:
            with isolated_app_modules(Path(tmp)):
                soundcloud = importlib.import_module("app.soundcloud")
                with patch.object(soundcloud, "sc_oauth_token", return_value=values["soundcloud"]):
                    with patch.object(soundcloud.yt_dlp, "YoutubeDL", FakeYoutubeDL):
                        result = soundcloud._resolve_likes("https://soundcloud.com/generated/likes")

                self.assertEqual("likes", result["id"])
                opts = captured["opts"]
                self.assertNotIn("cookiefile", opts)
                self.assertNotIn("cookiesfrombrowser", opts)
                jar = opts.get("cookiejar")
                self.assertIsNotNone(jar)
                cookies = list(jar)
                self.assertEqual(1, len(cookies))
                cookie = cookies[0]
                self.assertEqual(".soundcloud.com", cookie.domain)
                self.assertEqual("/", cookie.path)
                self.assertTrue(cookie.secure)
                self.assertTrue(cookie.value == values["soundcloud"])
                self.assertFalse(any(Path(tmp).rglob(".sc_cookies.txt")))
                self.assertFalse(any(Path(tmp).rglob("*.cookies.txt")))

    def test_secure_store_errors_do_not_emit_secret_values(self) -> None:
        values = generated_values()
        with tempfile.TemporaryDirectory() as tmp:
            with isolated_app_modules(Path(tmp)):
                secure_store = importlib.import_module("app.secure_store")
                store_path = Path(tmp) / "secrets.dpapi"
                store = secure_store.SecureCredentialStore(store_path, dpapi=FailingDpapi())
                with contextlib.redirect_stdout(io.StringIO()) as stdout:
                    with contextlib.redirect_stderr(io.StringIO()) as stderr:
                        with self.assertRaises(secure_store.SecureStoreError) as captured:
                            store.set_deezer_arl(values["deezer"])
                combined = formatted_exception(captured.exception) + stdout.getvalue() + stderr.getvalue()
                self.assertFalse(any(value in combined for value in values.values()))

                def leaking_replace(_src: Path, _dst: Path) -> None:
                    raise secure_store.SecureStoreError("replace leaked " + values["deezer"])

                store = secure_store.SecureCredentialStore(store_path, replace=leaking_replace)
                with self.assertRaises(secure_store.SecureStoreError) as replace_capture:
                    store.set_deezer_arl(values["deezer"])
                self.assertFalse(any(value in formatted_exception(replace_capture.exception) for value in values.values()))

                recorder = RecordingDpapi()
                protected = recorder.protect(
                    json.dumps(
                        {
                            "version": secure_store.STORE_VERSION,
                            "purpose": secure_store.STORE_PAYLOAD_PURPOSE,
                            "records": {"deezer_arl": values["deezer"]},
                        }
                    ).encode("utf-8"),
                    description=None,
                    optional_entropy=None,
                    flags=secure_store.CRYPTPROTECT_UI_FORBIDDEN,
                )
                store_path.write_text(
                    json.dumps(
                        {
                            "version": secure_store.STORE_VERSION,
                            "purpose": secure_store.STORE_FILE_PURPOSE,
                            "ciphertext": secure_store._b64encode(protected),
                        }
                    ),
                    encoding="utf-8",
                )
                failing = FailingDpapi()
                failing.leak_value = values["deezer"]
                with self.assertRaises(secure_store.SecureStoreError) as unprotect_capture:
                    secure_store.SecureCredentialStore(store_path, dpapi=failing).get_deezer_arl()
                self.assertFalse(any(value in formatted_exception(unprotect_capture.exception) for value in values.values()))

    def test_bugreport_uses_secure_telegram_token_and_never_returns_remote_leaks(self) -> None:
        values = generated_values()

        class HostileResponse:
            ok = False
            text = "remote body leaked " + values["telegram"]

            def json(self):
                return {"ok": False, "description": "remote json leaked " + values["telegram"]}

        def hostile_post(url, **_kwargs):
            raise RuntimeError("request exception leaked " + url + " " + values["telegram"])

        with tempfile.TemporaryDirectory() as tmp:
            with isolated_app_modules(Path(tmp)):
                bugreport = importlib.import_module("app.bugreport")
                cfg = {"telegram": {"chat_id": "generated-chat"}, "wav_mode": "source"}
                with patch.object(bugreport, "load_config", return_value=cfg):
                    with patch.object(bugreport, "get_telegram_bot_token", return_value=values["telegram"], create=True):
                        with patch.object(bugreport.requests, "post", return_value=HostileResponse()):
                            failed_response = bugreport.send_report("synthetic report")
                        with patch.object(bugreport.requests, "post", side_effect=hostile_post):
                            failed_exception = bugreport.send_report("synthetic report")

                combined = json.dumps([failed_response, failed_exception], ensure_ascii=False)
                self.assertEqual(False, failed_response["ok"])
                self.assertEqual(False, failed_exception["ok"])
                self.assertNotIn(values["telegram"], combined)
                self.assertNotIn("remote body", combined)
                self.assertNotIn("request exception", combined)
                self.assertNotIn("api.telegram.org", combined)


if __name__ == "__main__":
    unittest.main(verbosity=2)
