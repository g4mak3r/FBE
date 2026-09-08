"""Internal release smoke worker. Run ONLY inside a disposable source copy."""
from pathlib import Path
import json
import os
import sqlite3
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from connections import ConnectionStore

root = Path(__file__).resolve().parents[1]
if (root / 'config.json').exists() or (root / 'data/local/connections.json').exists():
    raise SystemExit('Smoke requires a fresh disposable source copy; existing state refused')
mode = sys.argv[1]
store = ConnectionStore(root)
if mode == 'demo':
    store.set_wb(token='SYNTHETIC-REAL-SECRET', profile={
        'sid': 'SYNTHETIC-REAL-SELLER', 'name': 'SYNTHETIC-REAL-COMPANY', 'tin': 'SYNTHETIC-REAL-INN'})
    store.update_marking({'suz_oms_id': 'SYNTHETIC-REAL-OMS', 'suz_contact_person': 'SYNTHETIC-REAL-CONTACT'})
store.set_mode(mode)
(root / 'config.json').write_text(json.dumps({
    'database_path': 'data/legacy-sentinel.db', 'wb_token': 'SYNTHETIC-LEGACY-SECRET',
    'suz_auth_inn': 'SYNTHETIC-LEGACY-INN', 'suz_true_certificate_number': 'SYNTHETIC-LEGACY-DOC',
    'dry_run_print': False, 'marking_direct_print_dry_run': False,
    'inventory_path': 'data/real-inventory.xlsx', 'catalog_auto_import_legacy': True}))
legacy = root / 'data/legacy-sentinel.db'
with sqlite3.connect(legacy) as db:
    db.execute('CREATE TABLE sentinel(value TEXT)')
    db.execute("INSERT INTO sentinel VALUES ('SYNTHETIC-REAL-DATA')")
original = legacy.read_bytes()
os.environ['FBE_DRY_RUN_PRINT'] = 'false'
os.environ['WB_TOKEN'] = 'SYNTHETIC-ENV-SECRET'
os.environ['CZ_AUTH_INN'] = 'SYNTHETIC-ENV-INN'

import requests
attempts = []
def no_http(self, method, url, **kwargs):
    attempts.append((method, url))
    raise AssertionError('Unexpected external HTTP')
requests.Session.request = no_http

import app
from fastapi.testclient import TestClient
with TestClient(app.app) as client:
    for page in ['/', '/catalog', '/economy', '/marking', '/marking/post-sale', '/manual-print', '/api/connections/status', '/api/fbe-info']:
        response = client.get(page)
        assert response.status_code == 200, (page, response.status_code)
        assert not any(v in response.text for v in ['SYNTHETIC-REAL', 'SYNTHETIC-ENV', 'SYNTHETIC-LEGACY'])
    if mode == 'demo':
        for endpoint in ['/api/connections/wb', '/api/connections/wb/disconnect', '/api/connections/wb/check', '/api/connections/marking']:
            response = client.post(endpoint, data={'token':'SYNTHETIC-ATTEMPT'})
            assert response.status_code == 403, (endpoint, response.status_code)
        assert app.config['dry_run_print'] and app.config['marking_direct_print_dry_run']
        assert app.config['wb_token'] == 'DEMO'
        assert 'SYNTHETIC-REAL' not in json.dumps(app.config)
        assert app.suz.list_certificates()[0]['subject'] == 'CN=FBE DEMO CERTIFICATE'
        import socket
        try:
            with socket.socket() as sock:
                sock.connect(('198.51.100.1', 443))
        except RuntimeError as exc:
            assert 'DEMO MODE' in str(exc)
        else:
            raise AssertionError('Outbound socket was allowed')
        import subprocess
        try:
            subprocess.run([sys.executable, '-c', 'pass'])
        except RuntimeError as exc:
            assert 'DEMO MODE' in str(exc)
        else:
            raise AssertionError('External process was allowed')
        from runtime_safety import print_is_dry_run
        assert print_is_dry_run({'mock_mode': False, 'dry_run_print': False})
        from printers.raw_windows import raw_print_windows
        from printers.wb_sticker import print_wb_sticker
        from printers.marking_direct import print_images_windows
        from PIL import Image
        image = Path(app.config['workspace_root']) / 'test-print.png'
        Image.new('RGB', (10, 10), 'white').save(image)
        raw_print_windows('SYNTHETIC-PRINTER', b'not sent')
        print_wb_sticker('SYNTHETIC-PRINTER', str(image), 'png', dry_run=False)
        assert print_images_windows(printer_name='SYNTHETIC-PRINTER', image_paths=[image],
            width_mm=30, height_mm=20, job_name='DEMO', dry_run=False,
            debug_path=image.parent / 'print.txt') == 'dry-run'
    else:
        assert app.config['wb_token'] == ''
        assert client.get('/api/connections/status').json()['first_run']
assert not attempts, attempts
assert legacy.read_bytes() == original
with sqlite3.connect(app.marking_db_path) as db:
    assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
print('RELEASE SMOKE PASS:', mode)
