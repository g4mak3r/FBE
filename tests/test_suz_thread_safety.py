from __future__ import annotations

import threading

from suz_client import SuzClient


def test_suz_http_sessions_are_thread_local(tmp_path):
    client = SuzClient(base_dir=tmp_path, settings_loader=lambda: {})
    main_session = client._http_session()
    seen = []

    def worker():
        first = client._http_session()
        second = client._http_session()
        seen.append((first, second))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=2)

    assert len(seen) == 1
    assert seen[0][0] is seen[0][1]
    assert seen[0][0] is not main_session
