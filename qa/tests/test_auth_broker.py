import asyncio
from contextlib import nullcontext
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI

def post(app, path, payload, token=None):
    messages = []
    body = json.dumps(payload).encode()
    headers = [(b'host', b'127.0.0.1'), (b'content-type', b'application/json')]
    if token:
        headers.append((b'x-deckpipe-auth-broker', token.encode()))
    async def receive():
        return {'type': 'http.request', 'body': body, 'more_body': False}
    async def send(message):
        messages.append(message)
    asyncio.run(app({'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'method': 'POST', 'scheme': 'http', 'path': path, 'raw_path': path.encode(), 'query_string': b'', 'headers': headers, 'client': ('127.0.0.1', 123), 'server': ('127.0.0.1', 80)}, receive, send))
    return messages[0]['status'], b''.join(m.get('body', b'') for m in messages).decode()


class AuthTests(unittest.TestCase):
    def service(self):
        from app.auth_broker import AuthService
        self.writes = []
        self.invalidations = []
        self.clock = [10.0]
        return AuthService(
            validate=lambda provider, credential: {'id': '42', 'name': 'Test'},
            persist=lambda provider, credential: self.writes.append((provider, credential)),
            clear=lambda provider: self.writes.append((provider, None)),
            invalidate=self.invalidations.append,
            clock=lambda: self.clock[0],
            transaction=lambda provider: nullcontext(),
        )

    def test_cancel_during_validation_rejects_late_result(self):
        service = self.service()
        started, release = threading.Event(), threading.Event()
        errors = []
        def validate(*args):
            started.set()
            release.wait(2)
            return {'id': '42'}
        service.validate = validate
        def prepare():
            try:
                service.prepare('late', 'sc', 'synthetic-secret')
            except ValueError as error:
                errors.append(str(error))
        worker = threading.Thread(target=prepare)
        worker.start()
        self.assertTrue(started.wait(1))
        service.discard('late')
        release.set()
        worker.join(2)
        self.assertEqual(errors, ['AUTH_CANCELLED'])
        self.assertEqual(service._candidates, {})
        with self.assertRaisesRegex(ValueError, 'AUTH_CANCELLED'):
            service.prepare('late', 'sc', 'synthetic-secret')

    def test_candidate_evicted_without_followup_calls(self):
        import time
        service = self.service()
        service.candidate_ttl = 0.03
        service.prepare('timer', 'sc', 'synthetic-secret')
        deadline = time.monotonic() + 1
        while service._candidates and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(service._candidates, {})

    def test_cancelled_private_round_does_not_poison_new_cookie_round(self):
        service = self.service()
        started, release = threading.Event(), threading.Event()
        errors = []
        def validate(provider, credential):
            if credential == 'synthetic-old':
                started.set()
                release.wait(2)
            return {'id': credential.rsplit('-', 1)[-1]}
        service.validate = validate
        def old_round():
            try:
                service.prepare('private-old-round', 'sc', 'synthetic-old')
            except ValueError as error:
                errors.append(str(error))
        worker = threading.Thread(target=old_round)
        worker.start()
        self.assertTrue(started.wait(1))
        service.discard('private-old-round')
        candidate = service.prepare('private-new-round', 'sc', 'synthetic-new')
        account = service.commit('private-new-round', candidate['validationId'])
        release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, ['AUTH_CANCELLED'])
        self.assertEqual(account['id'], 'new')
        self.assertEqual(self.writes, [('sc', 'synthetic-new')])
        self.assertEqual(service._candidates, {})

    def test_validation_finishing_after_timer_cannot_create_candidate(self):
        service = self.service()
        service.validation_ttl = 0.03
        def validate(*args):
            threading.Event().wait(0.08)
            return {'id': '42'}
        service.validate = validate
        with self.assertRaisesRegex(ValueError, 'AUTH_CANCELLED'):
            service.prepare('timed-validation', 'sc', 'synthetic-secret')
        self.assertEqual(service._candidates, {})

    def test_failed_rollback_blocks_credentials_and_recovery_survives_restart(self):
        from app import auth_broker as auth, deezer_client as dc
        from app.secure_store import SecureCredentialStore, SecureStoreError
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            store = SecureCredentialStore(base / 'secrets.dpapi')
            with patch.object(dc, 'SECRET_STORE_PATH', store.path), patch.object(dc, 'CONFIG_PATH', base / 'config.local.json'):
                store.set_secret('sc_oauth', 'synthetic-old')
                auth._save_account('sc', {'id': 'old', 'name': 'Old'})
                service = auth.AuthService(validate=lambda *args: {'id': 'new'}, persist=auth._persist, clear=auth._clear, invalidate=auth._invalidate, account_changed=auth._save_account)
                candidate = service.prepare('recovery', 'sc', 'synthetic-new')
                with patch.object(dc, 'save_config', side_effect=OSError('disk unavailable')):
                    with self.assertRaises(Exception):
                        service.commit('recovery', candidate['validationId'])
                    with self.assertRaises(SecureStoreError):
                        store.get_secret('sc_oauth')
                    with self.assertRaises(SecureStoreError):
                        store.set_secret('arl', 'synthetic-unrelated')
                journal = store.auth_journal_path.read_text()
                self.assertNotIn('synthetic-old', journal)
                self.assertNotIn('synthetic-new', journal)
                # Recovery needs no surviving candidate, service, or store instance.
                recovered = SecureCredentialStore(store.path)
                recovered.recover_auth_transaction(auth._restore_account_snapshot)
                self.assertEqual(recovered.get_secret('sc_oauth'), 'synthetic-old')
                self.assertEqual(auth._account_status()['sc']['account']['id'], 'old')
                self.assertFalse(store.auth_journal_path.exists())

    def test_transaction_faults_restore_secret_and_metadata_and_allow_retry(self):
        from app import auth_broker as auth, deezer_client as dc
        from app.secure_store import SecureCredentialStore
        for stage in ('persist', 'invalidate', 'account_changed'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                store = SecureCredentialStore(base / 'secrets.dpapi')
                with patch.object(dc, 'SECRET_STORE_PATH', store.path), patch.object(dc, 'CONFIG_PATH', base / 'config.local.json'):
                    store.set_secret('sc_oauth', 'synthetic-old')
                    store.set_secret('arl', 'synthetic-unrelated')
                    auth._save_account('sc', {'id': 'old', 'name': 'Old'})
                    service = auth.AuthService(validate=lambda *args: {'id': 'new', 'name': 'New'}, persist=auth._persist, clear=auth._clear, invalidate=auth._invalidate, account_changed=auth._save_account)
                    candidate = service.prepare('tx', 'sc', 'synthetic-new')
                    original = getattr(service, stage)
                    def fail_after(*args):
                        original(*args)
                        raise OSError('injected failure')
                    setattr(service, stage, fail_after)
                    with self.assertRaises(Exception):
                        service.commit('tx', candidate['validationId'])
                    self.assertEqual(store.get_secret('sc_oauth'), 'synthetic-old')
                    self.assertEqual(store.get_secret('arl'), 'synthetic-unrelated')
                    self.assertEqual(dc.load_config()['auth_accounts']['sc']['id'], 'old')
                    self.assertIn(candidate['validationId'], service._candidates)
                    setattr(service, stage, original)
                    service.commit('tx', candidate['validationId'])
                    self.assertEqual(store.get_secret('sc_oauth'), 'synthetic-new')
                    self.assertEqual(dc.load_config()['auth_accounts']['sc']['id'], 'new')

    def test_validation_does_not_persist_and_commit_is_one_time(self):
        service = self.service()
        candidate = service.prepare('request', 'sc', 'synthetic-secret')
        self.assertEqual(self.writes, [])
        self.assertNotIn('synthetic-secret', json.dumps(candidate))
        result = service.commit('request', candidate['validationId'])
        self.assertEqual(result, {'id': '42', 'name': 'Test'})
        self.assertEqual(self.writes, [('sc', 'synthetic-secret')])
        self.assertEqual(self.invalidations, ['sc'])
        with self.assertRaises(ValueError):
            service.commit('request', candidate['validationId'])

    def test_expired_or_wrong_request_cannot_commit(self):
        service = self.service()
        candidate = service.prepare('request', 'deezer', 'synthetic-secret')
        with self.assertRaises(ValueError):
            service.commit('wrong', candidate['validationId'])
        self.clock[0] += 61
        with self.assertRaises(ValueError):
            service.commit('request', candidate['validationId'])
        self.assertEqual(self.writes, [])

    def test_logout_discards_candidates_and_invalidates_only_provider(self):
        service = self.service()
        candidate = service.prepare('request', 'sc', 'synthetic-secret')
        service.logout('sc')
        with self.assertRaises(ValueError):
            service.commit('request', candidate['validationId'])
        self.assertEqual(self.writes, [('sc', None)])
        self.assertEqual(self.invalidations, ['sc'])

    def test_private_endpoint_rejects_missing_token_bad_schema_without_echo(self):
        from app.auth_broker import auth_router
        app = FastAPI()
        app.include_router(auth_router)
        with patch.dict(os.environ, {'DECKPIPE_AUTH_BROKER_TOKEN': 'private-test-token'}):
            status, body = post(app, '/api/internal/auth/complete', {'credential': 'synthetic-secret'})
            self.assertEqual(status, 403)
            status, body = post(app, '/api/internal/auth/complete', {'credential': 'synthetic-secret'}, 'private-test-token')
            self.assertEqual(status, 400)
            self.assertNotIn('synthetic-secret', body)

    def test_delete_secret_retains_other_provider_dpapi(self):
        from app.secure_store import SecureCredentialStore
        with tempfile.TemporaryDirectory() as directory:
            store = SecureCredentialStore(Path(directory) / 'secret.dpapi')
            store.set_secret('arl', 'synthetic-deezer')
            store.set_secret('sc_oauth', 'synthetic-sc')
            store.delete_secret('arl')
            self.assertIsNone(store.get_secret('arl'))
            self.assertEqual(store.get_secret('sc_oauth'), 'synthetic-sc')

    def test_logout_racing_provider_validation_cannot_restore_credentials(self):
        service = self.service()
        started = threading.Event()
        release = threading.Event()
        errors = []
        def validate(provider, credential):
            started.set()
            release.wait(2)
            return {'id': '42', 'name': 'Test'}
        service.validate = validate
        def prepare():
            try:
                service.prepare('request', 'sc', 'synthetic-secret')
            except ValueError as error:
                errors.append(str(error))
        worker = threading.Thread(target=prepare)
        worker.start()
        self.assertTrue(started.wait(1))
        service.logout('sc')
        release.set()
        worker.join(2)
        self.assertEqual(errors, ['AUTH_CANCELLED'])
        self.assertEqual(self.writes, [('sc', None)])

    def test_private_route_keeps_launch_auth_and_origin_boundary(self):
        from app.auth_broker import auth_router
        from app.security import LoopbackSecurityMiddleware, SecuritySettings
        from qa.tests.test_security_contract import asgi_request
        inner = FastAPI()
        inner.include_router(auth_router)
        app = LoopbackSecurityMiddleware(inner, SecuritySettings.for_launch('127.0.0.1', 7100, 'launch-test'))
        with patch.dict(os.environ, {'DECKPIPE_AUTH_BROKER_TOKEN': 'private-test'}):
            for headers, expected in [
                ({'Host': '127.0.0.1:7100', 'X-DeckPipe-Auth-Broker': 'private-test'}, 401),
                ({'Host': '127.0.0.1:7100', 'Authorization': 'Bearer launch-test'}, 403),
                ({'Host': '127.0.0.1:7100', 'Authorization': 'Bearer launch-test', 'X-DeckPipe-Auth-Broker': 'private-test', 'Origin': 'https://evil.example'}, 403),
                ({'Host': '127.0.0.1:7100', 'Authorization': 'Bearer launch-test', 'X-DeckPipe-Auth-Broker': 'private-test'}, 400),
            ]:
                status, _, body = asyncio.run(asgi_request(app, 'POST', '/api/internal/auth/complete', headers, b'{"credential":"synthetic-secret"}'))
                self.assertEqual(status, expected)
                self.assertNotIn(b'synthetic-secret', body)

    def test_failed_validation_cannot_write_or_echo_credentials(self):
        service = self.service()
        def reject(provider, credential):
            raise RuntimeError('synthetic-secret')
        service.validate = reject
        with self.assertRaisesRegex(ValueError, '^AUTH_PROVIDER_REJECTED$'):
            service.prepare('request', 'sc', 'synthetic-secret')
        self.assertEqual(self.writes, [])


if __name__ == '__main__':
    unittest.main()
