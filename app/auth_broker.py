"""Private backend half of the native AuthBroker. Never a browser cookie endpoint.

The Rust broker owns browser attempts/state. Validation here is deliberately
separate from persistence: cancellation/expiry during provider I/O cannot commit.
The normal launch-bearer middleware remains required around this router.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

PROVIDERS = {'deezer': 'arl', 'sc': 'sc_oauth'}
MAX_BODY = 16384


def _provider(provider):
    if provider not in PROVIDERS:
        raise ValueError('AUTH_INVALID_PROVIDER')
    return provider


@dataclass(repr=False)
class _Candidate:
    request_id: str
    provider: str
    credential: str = field(repr=False)
    account: dict
    expires: float


class AuthService:
    def __init__(self, *, validate, persist, clear, invalidate, clock=time.monotonic, account_changed=None, transaction=None):
        self.validate = validate
        self.persist = persist
        self.clear = clear
        self.invalidate = invalidate
        self.clock = clock
        self.account_changed = account_changed
        self.transaction = transaction or _account_transaction
        self.candidate_ttl = 60
        self.validation_ttl = 120
        self._lock = threading.RLock()
        self._candidates = {}
        self._generation = {provider: 0 for provider in PROVIDERS}
        self._inflight = {}
        self._cancelled = {}
        self._timers = {}
        self._active_validations = 0

    def _timer(self, request_id, seconds):
        old = self._timers.pop(request_id, None)
        if old:
            old.cancel()
        timer = threading.Timer(seconds, self.discard, args=(request_id,))
        timer.daemon = True
        self._timers[request_id] = timer
        timer.start()

    def _forget_candidate(self, key):
        candidate = self._candidates.pop(key)
        candidate.credential = ''
        self._cancelled[candidate.request_id] = self.clock() + 600
        timer = self._timers.pop(candidate.request_id, None)
        if timer:
            timer.cancel()

    def _prune(self):
        for key, candidate in list(self._candidates.items()):
            if candidate.expires <= self.clock():
                self._forget_candidate(key)
        self._cancelled = {key: deadline for key, deadline in self._cancelled.items() if deadline > self.clock()}

    def prepare(self, request_id, provider, credential):
        _provider(provider)
        if not isinstance(credential, str) or not credential.strip() or len(credential) > 8192:
            raise ValueError('AUTH_INVALID_CREDENTIAL')
        with self._lock:
            self._prune()
            if request_id in self._cancelled:
                raise ValueError('AUTH_CANCELLED')
            if request_id in self._inflight or any(c.request_id == request_id for c in self._candidates.values()):
                raise ValueError('AUTH_BUSY')
            if len(self._candidates) + self._active_validations >= 4 or len(self._cancelled) >= 1024:
                raise ValueError('AUTH_BUSY')
            generation = self._generation[provider]
            marker = object()
            self._inflight[request_id] = (marker, provider)
            self._active_validations += 1
            deadline = self.clock() + self.validation_ttl
            self._timer(request_id, self.validation_ttl)
        try:
            account = self.validate(provider, credential.strip())
        except Exception:
            self.discard(request_id)
            raise ValueError('AUTH_PROVIDER_REJECTED') from None
        finally:
            with self._lock:
                self._active_validations -= 1
        # Only a tiny account summary is allowed back into the native/UI path.
        account = {'id': str(account.get('id', ''))[:128], 'name': str(account.get('name', ''))[:256]}
        if not account['id']:
            self.discard(request_id)
            raise ValueError('AUTH_PROVIDER_REJECTED')
        with self._lock:
            self._prune()
            current = self._inflight.pop(request_id, None)
            if current != (marker, provider) or generation != self._generation[provider] or self.clock() >= deadline:
                self.discard(request_id)
                raise ValueError('AUTH_CANCELLED')
            if len(self._candidates) >= 4:
                raise ValueError('AUTH_BUSY')
            key = secrets.token_hex(32)
            self._candidates[key] = _Candidate(request_id, provider, credential.strip(), account, self.clock() + self.candidate_ttl)
            self._timer(request_id, self.candidate_ttl)
            return {'validationId': key, 'account': account}

    def commit(self, request_id, validation_id):
        with self._lock:
            self._prune()
            candidate = self._candidates.get(validation_id)
            if candidate is None or not hmac.compare_digest(candidate.request_id, request_id):
                raise ValueError('AUTH_INVALID_CANDIDATE')
            with self.transaction(candidate.provider):
                self.persist(candidate.provider, candidate.credential)
                self.invalidate(candidate.provider)
                if self.account_changed:
                    self.account_changed(candidate.provider, candidate.account)
            self._forget_candidate(validation_id)
            return candidate.account

    def discard(self, request_id):
        with self._lock:
            self._prune()
            self._cancelled[request_id] = self.clock() + 600
            self._inflight.pop(request_id, None)
            timer = self._timers.pop(request_id, None)
            if timer:
                timer.cancel()
            for key, candidate in list(self._candidates.items()):
                if candidate.request_id == request_id:
                    self._forget_candidate(key)

    def logout(self, provider):
        _provider(provider)
        with self._lock:
            self._generation[provider] += 1
            for request_id, (_, active_provider) in list(self._inflight.items()):
                if active_provider == provider:
                    self.discard(request_id)
            for key, candidate in list(self._candidates.items()):
                if candidate.provider == provider:
                    self.discard(candidate.request_id)
            with self.transaction(provider):
                self.clear(provider)
                self.invalidate(provider)


def _validate(provider, credential):
    if provider == 'deezer':
        from .deezer_client import DeezerSession
        user = DeezerSession(credential).user
        return {'id': user['USER_ID'], 'name': user.get('BLOG_NAME') or user.get('EMAIL') or str(user['USER_ID'])}
    from . import soundcloud
    user = soundcloud.sc_validate(credential)
    return {'id': user['id'], 'name': user.get('username', '')}


def _persist(provider, credential):
    from .deezer_client import _secure_store
    _secure_store().set_secret(PROVIDERS[provider], credential)


def _clear(provider):
    from .deezer_client import _secure_store
    _secure_store().delete_secret(PROVIDERS[provider])


_invalidation_hooks = []


def register_invalidation_hook(callback):
    if callback not in _invalidation_hooks:
        _invalidation_hooks.append(callback)


def _invalidate_caches(provider):
    from . import deezer_client, soundcloud
    if provider == 'deezer':
        deezer_client._session_cache.update(session=None, arl=None, ts=0)
    else:
        soundcloud._cache.clear()
    for callback in tuple(_invalidation_hooks):
        callback(provider)


def _invalidate(provider):
    from . import deezer_client
    _invalidate_caches(provider)
    cfg = deezer_client.load_config()
    if provider == 'sc':
        cfg.pop('sc_username', None)
        cfg.pop('sc_account_id', None)
        cfg.pop('sc_last_sync', None)
    accounts = cfg.get('auth_accounts', {})
    if isinstance(accounts, dict):
        accounts.pop(provider, None)
        cfg['auth_accounts'] = accounts
    deezer_client.save_config(cfg)


def _save_account(provider, account):
    from .deezer_client import load_config, save_config
    cfg = load_config()
    accounts = cfg.get('auth_accounts', {})
    if not isinstance(accounts, dict):
        accounts = {}
    accounts[provider] = account
    cfg['auth_accounts'] = accounts
    if provider == 'sc':
        cfg['sc_username'] = account['name']
        cfg['sc_account_id'] = account['id']
    save_config(cfg)


def _account_snapshot(provider):
    from .deezer_client import load_config
    cfg = load_config()
    accounts = cfg.get('auth_accounts', {})
    account = accounts.get(provider) if isinstance(accounts, dict) else None
    account = {'id': str(account.get('id', ''))[:128], 'name': str(account.get('name', ''))[:256]} if isinstance(account, dict) else None
    fields = {key: cfg[key] for key in ('sc_username', 'sc_account_id', 'sc_last_sync') if provider == 'sc' and key in cfg}
    return {'provider': provider, 'account': account, 'fields': fields}


def _restore_account_snapshot(snapshot):
    from .deezer_client import load_config, save_config
    provider = _provider(snapshot['provider'])
    if not isinstance(snapshot.get('fields'), dict) or not set(snapshot['fields']).issubset({'sc_username', 'sc_account_id', 'sc_last_sync'}):
        raise ValueError('AUTH_INVALID_RECOVERY')
    cfg = load_config()
    accounts = cfg.get('auth_accounts', {})
    if not isinstance(accounts, dict):
        accounts = {}
    if snapshot['account'] is None:
        accounts.pop(provider, None)
    else:
        accounts[provider] = snapshot['account']
    cfg['auth_accounts'] = accounts
    if provider == 'sc':
        for key in ('sc_username', 'sc_account_id', 'sc_last_sync'):
            cfg.pop(key, None)
        cfg.update(snapshot['fields'])
    save_config(cfg)
    _invalidate_caches(provider)


def _account_transaction(provider):
    from .deezer_client import _secure_store
    return _secure_store().auth_transaction(lambda: _account_snapshot(provider), _restore_account_snapshot)


def recover_pending_transaction():
    from .deezer_client import _secure_store
    _secure_store().recover_auth_transaction(_restore_account_snapshot)


def _account_status():
    from .secure_store import _WRITE_LOCK
    with _WRITE_LOCK:
        recover_pending_transaction()
        return _account_status_recovered()


def _account_status_recovered():
    from .deezer_client import _secure_store, load_config
    store = _secure_store()
    metadata = load_config().get('auth_accounts', {})
    result = {}
    for provider, field in PROVIDERS.items():
        connected = store.has_secret(field)
        account = metadata.get(provider) if connected and isinstance(metadata, dict) else None
        result[provider] = {'connected': connected, 'account': {'id': str(account.get('id', ''))[:128], 'name': str(account.get('name', ''))[:256]} if isinstance(account, dict) else None}
    return result


auth_service = AuthService(validate=_validate, persist=_persist, clear=_clear, invalidate=_invalidate, account_changed=_save_account)
auth_router = APIRouter(prefix='/api/internal/auth')


def _no_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate')
        value[key] = item
    return value


async def _body(request, expected):
    expected_token = os.environ.get('DECKPIPE_AUTH_BROKER_TOKEN', '')
    supplied = request.headers.get('x-deckpipe-auth-broker', '')
    if not expected_token or not hmac.compare_digest(expected_token.encode('utf-8'), supplied.encode('utf-8')):
        raise HTTPException(403, 'AUTH_FORBIDDEN')
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_BODY:
            raise HTTPException(400, 'AUTH_INVALID_MESSAGE')
    try:
        value = json.loads(data, object_pairs_hook=_no_duplicates)
        if not isinstance(value, dict) or set(value) != set(expected):
            raise ValueError()
        if any(not isinstance(item, str) or not item or len(item) > limit for key, limit in expected.items() for item in [value[key]]):
            raise ValueError()
        return value
    except Exception:
        raise HTTPException(400, 'AUTH_INVALID_MESSAGE') from None
    finally:
        data[:] = b'\0' * len(data)


async def _call(callback, *args):
    try:
        return await run_in_threadpool(callback, *args)
    except Exception:
        raise HTTPException(400, 'AUTH_OPERATION_FAILED') from None


@auth_router.post('/complete')
async def complete(request: Request):
    body = await _body(request, {'requestId': 128, 'provider': 16, 'credential': 8192})
    return await _call(auth_service.prepare, body['requestId'], body['provider'], body['credential'])


@auth_router.post('/commit')
async def commit(request: Request):
    body = await _body(request, {'requestId': 128, 'validationId': 128})
    return {'account': await _call(auth_service.commit, body['requestId'], body['validationId'])}


@auth_router.post('/discard')
async def discard(request: Request):
    body = await _body(request, {'requestId': 128})
    await _call(auth_service.discard, body['requestId'])
    return {'ok': True}


@auth_router.post('/logout')
async def logout(request: Request):
    body = await _body(request, {'provider': 16})
    await _call(auth_service.logout, body['provider'])
    return {'ok': True}


@auth_router.post('/status')
async def status(request: Request):
    await _body(request, {})
    return {'accounts': await _call(_account_status)}
