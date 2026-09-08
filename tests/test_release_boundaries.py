from __future__ import annotations
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from connections import ConnectionStore
from settings import Settings
from workspaces import seller_workspace, RUNTIME_PATHS
from tools.git_safety_check import scan
from tools.adopt_legacy_workspace import adopt

ROOT = Path(__file__).resolve().parents[1]


def test_sid_workspace_switch_restart_and_demo_return(tmp_path, monkeypatch):
    store = ConnectionStore(tmp_path)
    legacy = tmp_path / 'legacy.db'
    with sqlite3.connect(legacy) as db:
        db.execute('CREATE TABLE sentinel(value TEXT)')
        db.execute("INSERT INTO sentinel VALUES ('legacy untouched')")
    legacy_bytes = legacy.read_bytes()
    (tmp_path / 'config.json').write_text(json.dumps({'database_path': str(legacy), 'wb_token': 'legacy-secret'}))
    fresh = Settings(tmp_path / 'config.json')
    assert fresh.data['wb_token'] == ''
    assert fresh.data['workspace_sid'] == ''
    assert not fresh.connection_public_state()['wb']['connected']
    snapshots = {}
    for sid in ('seller-A', 'seller-B'):
        store.set_wb(token='synthetic-' + sid, profile={'sid': sid, 'name': sid})
        settings = Settings(tmp_path / 'config.json')
        settings.update_external_api_settings({'suz_oms_id': 'SYNTHETIC-' + sid})
        root = seller_workspace(tmp_path, sid)
        root.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(settings.data['database_path']) as db:
            db.execute('CREATE TABLE sentinel(value TEXT)')
            db.execute('INSERT INTO sentinel VALUES (?)', (sid,))
        for key in RUNTIME_PATHS:
            assert Path(settings.data[key]).is_relative_to(root)
        snapshots[sid] = settings.data['database_path']
    assert snapshots['seller-A'] != snapshots['seller-B']
    store.set_mode('demo')
    monkeypatch.setenv('WB_TOKEN', 'ENV-REAL-TOKEN')
    monkeypatch.setenv('CZ_AUTH_INN', 'ENV-REAL-INN')
    monkeypatch.setenv('FBE_DRY_RUN_PRINT', 'false')
    demo = Settings(tmp_path / 'config.json')
    assert demo.data['dry_run_print'] and demo.data['marking_direct_print_dry_run']
    assert demo.data['wb_token'] == 'DEMO'
    assert 'seller-B' not in json.dumps(demo.data)
    assert 'seller-B' not in json.dumps(demo.connection_public_state())
    for fn in (lambda: demo.set_value('dry_run_print', False),
               lambda: demo.set_runtime_wb_connection('new', {'sid': 'new'}),
               lambda: demo.clear_runtime_wb_connection(),
               lambda: demo.update_external_api_settings({'suz_oms_id': 'other'})):
        with pytest.raises(ValueError):
            fn()
    store.set_mode('real')
    restored = Settings(tmp_path / 'config.json')
    assert restored.data['database_path'] == snapshots['seller-B']
    assert restored.data['wb_token'] == 'synthetic-seller-B'
    assert restored.data['suz_oms_id'] == 'SYNTHETIC-seller-B'
    store.set_wb(token='synthetic-seller-A', profile={'sid': 'seller-A'})
    restored = Settings(tmp_path / 'config.json')
    assert restored.data['suz_oms_id'] == 'SYNTHETIC-seller-A'
    with sqlite3.connect(restored.data['database_path']) as db:
        assert db.execute('SELECT value FROM sentinel').fetchone()[0] == 'seller-A'
    assert legacy.read_bytes() == legacy_bytes
    store.clear_wb()
    assert Settings(tmp_path / 'config.json').data['wb_token'] == ''


def test_sid_path_is_safe_and_case_sensitive(tmp_path):
    a = seller_workspace(tmp_path, '../CON/../../seller')
    assert a.parent == tmp_path / 'data/workspaces'
    assert seller_workspace(tmp_path, 'A') != seller_workspace(tmp_path, 'a')


@pytest.fixture
def connection_app(tmp_path, monkeypatch):
    import app as fbe
    settings = Settings(tmp_path / 'config.json')
    monkeypatch.setattr(fbe, 'settings', settings)
    monkeypatch.setattr(fbe, 'config', settings.data)
    monkeypatch.setattr(fbe, '_restart_pending', False)
    monkeypatch.setattr(fbe, '_BACKGROUND_JOBS', {})
    monkeypatch.setattr(fbe, '_schedule_application_restart', lambda: setattr(fbe, '_restart_pending', True))
    return fbe, TestClient(fbe.app)


def test_connect_is_read_only_restart_bound_and_secret_free(connection_app, monkeypatch, caplog, capsys):
    fbe, client = connection_app
    calls = []
    def profile(self):
        calls.append(self.token)
        return {'sid': 'seller-new', 'name': 'Synthetic company', 'tin': '000000000000'}
    monkeypatch.setattr(fbe.WBClient, 'get_seller_info', profile)
    before_db = fbe.config['database_path']
    before_token = fbe.wb.token
    response = client.post('/api/connections/wb', data={'token': 'synthetic-test-secret'})
    assert response.status_code == 200 and response.json()['restart']
    assert calls == ['synthetic-test-secret']
    assert fbe.config['database_path'] == before_db and fbe.wb.token == before_token
    assert fbe.config['wb_token'] == ''
    assert client.get('/catalog').status_code == 503
    assert client.get('/api/fbe-info').json()['restart_pending']
    status = client.get('/api/connections/status')
    restarted = Settings(fbe.settings.path)
    assert restarted.data['workspace_sid'] == 'seller-new'
    assert restarted.data['wb_token'] == 'synthetic-test-secret'
    assert 'synthetic-test-secret' not in response.text + status.text + caplog.text + capsys.readouterr().out


@pytest.mark.parametrize('outcome', ['error', 'no_sid'])
def test_invalid_connection_leaves_previous_binding(connection_app, monkeypatch, outcome):
    fbe, client = connection_app
    def profile(self):
        if outcome == 'error':
            raise RuntimeError('synthetic-secret-in-remote-error')
        return {'name': 'Synthetic'}
    monkeypatch.setattr(fbe.WBClient, 'get_seller_info', profile)
    response = client.post('/api/connections/wb', data={'token': 'synthetic-secret-in-remote-error'})
    assert response.status_code == 400
    assert 'synthetic-secret' not in response.text
    assert not fbe.settings.connection_store.path.exists()
    assert not fbe._restart_pending


def test_busy_job_and_cross_origin_block_switch(connection_app, monkeypatch):
    fbe, client = connection_app
    monkeypatch.setattr(fbe, '_BACKGROUND_JOBS', {'job': {'state': 'running'}})
    assert client.post('/api/connections/mode', data={'mode': 'demo'}).status_code == 409
    assert client.post('/api/connections/mode', data={'mode': 'demo'}, headers={'Origin':'https://foreign.invalid'}).status_code == 403
    assert not fbe.settings.connection_store.path.exists()


def test_legacy_adoption_copies_without_overwriting(tmp_path):
    source = tmp_path / 'old.db'
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE example (value TEXT)')
        db.execute("INSERT INTO example VALUES ('SYNTHETIC')")
    original = source.read_bytes()
    target = adopt(source, tmp_path, 'seller-A')
    with sqlite3.connect(target) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert db.execute('SELECT value FROM example').fetchone()[0] == 'SYNTHETIC'
    with pytest.raises(FileExistsError):
        adopt(source, tmp_path, 'seller-A')
    assert source.read_bytes() == original


@pytest.mark.parametrize('relative', ['connections.json', 'data/workspaces/a/ids.json',
    'data/print/job.csv', 'exports/labels.pdf', 'backup.db-wal', 'credentials.json', 'private.btw'])
def test_git_scan_rejects_runtime_files_without_echoing_values(tmp_path, relative):
    file = tmp_path / relative
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text('synthetic-secret-never-print')
    result = scan(tmp_path)
    assert result and 'synthetic-secret-never-print' not in str(result)


def test_git_scan_checks_placeholder_content_and_index(tmp_path):
    file = tmp_path / 'config.example.json'
    file.write_text(json.dumps({'wb_token': 'accidentally-real-credential'}))
    assert scan(tmp_path)
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    subprocess.run(['git', '-C', str(tmp_path), 'add', 'config.example.json'], check=True)
    file.write_text(json.dumps({'wb_token': 'PASTE_TOKEN_HERE'}))
    assert any('index' in message for message in scan(tmp_path))


@pytest.mark.parametrize('mode', ['real', 'demo'])
def test_clean_process_startup_pages_and_network_blocked_demo(tmp_path, mode):
    case = tmp_path / 'fbe'
    shutil.copytree(ROOT, case, ignore=shutil.ignore_patterns(
        '__pycache__', '.pytest_cache', 'config.json', 'workspaces', 'unconnected',
        'local', 'demo', '*.db', '*.db-wal', '*.db-shm', '*.btw', 'logs', '.git'))
    result = subprocess.run([sys.executable, str(case / 'tools/release_smoke.py'), mode],
                            cwd=case, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'RELEASE SMOKE PASS' in result.stdout


def test_live_uvicorn_restart_real_demo_real(tmp_path):
    """Exercise the actual exit-75 protocol over loopback, with WB HTTP blocked."""
    import socket
    import time
    import httpx
    case = tmp_path / 'fbe'
    shutil.copytree(ROOT, case, ignore=shutil.ignore_patterns(
        '__pycache__', '.pytest_cache', 'config.json', 'workspaces', 'unconnected',
        'local', 'demo', '*.db', '*.db-wal', '*.db-shm', '*.btw', 'logs', '.git'))
    store = ConnectionStore(case)
    store.set_wb(token='SYNTHETIC-NETWORK-BLOCKED', profile={'sid': 'SYNTHETIC-SELLER'})
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    launch = (
        'import requests; '
        'requests.Session.request=lambda *a,**kw: (_ for _ in ()).throw(RuntimeError("HTTP blocked by gate")); '
        'import app,uvicorn; '
        f'uvicorn.run(app.app,host="127.0.0.1",port={port},log_level="error",loop="asyncio")'
    )
    seen = set()
    for mode, next_mode in [('real', 'demo'), ('demo', 'real'), ('real', None)]:
        proc = subprocess.Popen([sys.executable, '-c', launch], cwd=case,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=2, trust_env=False) as client:
                deadline = time.monotonic() + 10
                while True:
                    try:
                        info = client.get('/api/fbe-info').json()
                        if info['instance_id'] not in seen:
                            break
                    except (httpx.HTTPError, ValueError):
                        pass
                    assert time.monotonic() < deadline, 'Server failed to start'
                    time.sleep(.1)
                assert info['mode'] == mode
                seen.add(info['instance_id'])
                state = client.get('/api/connections/status').json()
                assert state['wb']['sid'] == ('SYNTHETIC-SELLER' if mode == 'real' else '00000000-0000-4000-8000-000000000017')
                if next_mode:
                    response = client.post('/api/connections/mode', data={'mode': next_mode})
                    assert response.status_code == 200 and response.json()['restart']
                    assert proc.wait(timeout=8) == 75
        finally:
            if proc.poll() is None:
                proc.terminate()
            out, err = proc.communicate(timeout=8)
            assert 'SYNTHETIC-NETWORK-BLOCKED' not in out + err
    assert len(seen) == 3


def test_example_env_cannot_hide_opaque_credentials(tmp_path):
    file = tmp_path / '.env.example'
    file.write_text('WB_TOKEN=' + 'accidental-opaque-credential')
    messages = scan(tmp_path)
    assert messages and 'accidental-opaque-credential' not in str(messages)
    file.write_text('WB_TOKEN=PASTE_TOKEN_HERE')
    assert scan(tmp_path) == []


def test_pending_bartender_templates_import_into_verified_seller_workspace(tmp_path):
    from local_templates import pending_template_status, import_pending_templates
    stage = tmp_path / 'data/local/template_import/pending'
    stage.mkdir(parents=True)
    (stage / 'default.btw').write_bytes(b'SYNTHETIC-BTW-DEFAULT')
    (stage / 'perfume.btw').write_bytes(b'SYNTHETIC-BTW-PERFUME')
    manifest = stage.parent / 'manifest.json'
    manifest.write_text(json.dumps({
        'version': 1, 'source_kind': 'legacy_root', 'expected_sid': '',
        'files': ['default.btw', 'perfume.btw']
    }), encoding='utf-8')

    assert pending_template_status(tmp_path, '')['count'] == 2
    state = pending_template_status(tmp_path, 'seller-A')
    assert state['pending'] and state['can_import'] and state['seller_match']

    result = import_pending_templates(tmp_path, 'seller-A')
    assert result['ok'] and sorted(result['imported']) == ['default.btw', 'perfume.btw']
    target = seller_workspace(tmp_path, 'seller-A') / 'templates'
    assert (target / 'default.btw').read_bytes() == b'SYNTHETIC-BTW-DEFAULT'
    assert (target / 'perfume.btw').read_bytes() == b'SYNTHETIC-BTW-PERFUME'
    assert not pending_template_status(tmp_path, 'seller-A')['pending']


def test_pending_bartender_templates_never_cross_seller_or_overwrite(tmp_path):
    from local_templates import pending_template_status, import_pending_templates
    stage = tmp_path / 'data/local/template_import/pending'
    stage.mkdir(parents=True)
    (stage / 'default.btw').write_bytes(b'OLD-PRIVATE-TEMPLATE')
    (stage.parent / 'manifest.json').write_text(json.dumps({
        'version': 1, 'source_kind': 'seller_workspace', 'expected_sid': 'seller-A',
        'files': ['default.btw']
    }), encoding='utf-8')

    mismatch = pending_template_status(tmp_path, 'seller-B')
    assert mismatch['pending'] and not mismatch['seller_match'] and not mismatch['can_import']
    with pytest.raises(ValueError):
        import_pending_templates(tmp_path, 'seller-B')
    assert not seller_workspace(tmp_path, 'seller-B').exists()

    target = seller_workspace(tmp_path, 'seller-A') / 'templates'
    target.mkdir(parents=True)
    (target / 'default.btw').write_bytes(b'NEWER-SELLER-TEMPLATE')
    result = import_pending_templates(tmp_path, 'seller-A')
    assert not result['ok'] and result['conflicts'] == ['default.btw']
    assert (target / 'default.btw').read_bytes() == b'NEWER-SELLER-TEMPLATE'
    assert (stage / 'default.btw').read_bytes() == b'OLD-PRIVATE-TEMPLATE'
