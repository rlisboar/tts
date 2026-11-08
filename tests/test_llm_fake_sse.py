"""Régua do pos_llm (#1022): o LLM falso fala o contrato SSE que o Live consome."""
from __future__ import annotations

import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import llm_fake_sse as fake  # noqa: E402

import live_pipeline as lp  # noqa: E402


def test_stream_do_fake_chega_inteiro_no_ritmo_declarado(monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), fake.faz_handler(0.05, 0.01, "Oi, tudo bem? Sim."))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        import app
        monkeypatch.setattr(app, "_chat_provider",
                            lambda: (f"http://127.0.0.1:{srv.server_port}/v1", "fake", ""))
        t0 = time.monotonic()
        deltas = list(lp._llm_stream_app([{"role": "user", "content": "oi"}]))
        levou = time.monotonic() - t0
    finally:
        srv.shutdown()
    assert "".join(deltas) == "Oi, tudo bem? Sim."
    assert deltas == fake.tokens("Oi, tudo bem? Sim.")
    assert levou >= 0.05 + 0.01 * (len(deltas) - 1)   # TTFT + ritmo respeitados
