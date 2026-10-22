# app.py — Complete Codebase Review (9626 lines)

## Overview

`app.py` is the single-file FastAPI application for **tts-rod**, a local TTS server with two parallel backends (FastAPI REST + WebSocket live pipeline), MLX-based synthesis, and voice cloning. The file is organized into:

1. **Imports & global constants** — `_LIVE_TTS_RATE = 24000`, audio format tables, design-voice IDs
2. **Settings & state** — `_settings` dict, voice registries, session store
3. **Utility helpers** — audio conversion, DSH client wrappers, JSON senders
4. **MLX TTS backend** — `_tts_mlx` core engine, prompt construction, cache layer
5. **Voice management** — `_list_voices`, cloning, design voices, snapshot/export
6. **Rate limiter & metrics** — `_rate_limiter`, metrics DB with TTL/TTL cap
7. **REST API endpoints** — `/api/tts`, `/v1/audio/speech`, voice endpoints
8. **Live WebSocket pipeline** — `_live_ws`, `_live_pipe_novo`, prewarm, history compression
9. **OpenAI-compatible proxy** — `/v1/*` routing
10. **Middleware & static files**

---

## Key Architecture Decisions

### Dual-backend Strategy
- **`/api/tts`** — standard REST, sync TTS via MLX or DSH backend
- **`/v1/audio/speech`** — OpenAI-compatible endpoint for SDK clients (xAI/Grok), converts formats via ffmpeg
- **`/api/live/ws`** — real-time WebSocket with streaming chunks, turn-based talk mode

### Live Pipeline (`_live_ws`, `_live_pipe_novo`)
The live WebSocket implements a **turn-based voice interaction** model:

- `_live_chunk_ms(pcm)` — computes chunk duration: `len(pcm) / 2 / (_LIVE_TTS_RATE / 1000.0)`
- `_live_pipe_novo(sess)` — builds a `LivePipeline`; when backend is "dsh", creates per-session `DshClient` with latch/fallback to OpenAI, history serialization, and a "morto" (dead) flag via `ao_morrer_dsh=_dsh_marca_morto`
- `_live_pipe_start(sess)` — prewarm: `pipe.start()` then sends `{"type": "prewarm", "ok": True}`; saves ~5-7s cold boot outside handshake
- `_live_resumidor(sess)` — resolves resume LLM: cached `_live_resume_fn` or dsh pool or `_chat_llm`
- `_live_hist_comprime(sess)` — async compression: 3-5 line summary, replaces oldest half of `pipe.history`, guarded by `_LIVE_HIST_MAX_MSGS`/`_LIVE_HIST_MAX_CHARS`
- `_live_hist_pos_turno(sess)` — guards history, starts compression thread non-blocking

### Message Loop (`live_ws`)
Handles `ping/cancel/hold/end_of_speech`:
- **ping** → pong with echo timestamp
- **cancel** → `eng.cancel()` then `_live_cancela(sess, do_cliente=True)` (aborts everything including pending)
- **hold** → #314 fallback "segurar pra falar" — engine opens/closes turn on button, no VAD heuristic
- **end_of_speech** → client-side: `eng.flush()`; server-side: opens turn using buffer, starts `_live_engine` thread

### `_LiveFila` — Byte-capped Queue
- 2 MiB default cap, oldest-audio-first eviction, audio-only discarding

### `_live_abre_turno`
- Turn opening with prewarm gate (#315), pending queue #126, `buffer_consumido` guard

---

## Key Patterns & Idioms

1. **Graceful failure** — metrics registration wrapped in try/except (`# noqa: BLE001`); history errors never fail requests
2. **Session state** — `sess` dict carries `engine`, `buffer`, `turno`, `turno_thread`, `cancelado`
3. **Thread safety** — daemon threads for engine inference; `_live_trata_eventos` for callbacks
4. **Design voices** — `__design__` / "design" → prompt-generated voice without cloning
5. **Metrics** — `_metrics.registrar()` with `ttl_dias=14`, `teto_mb=8` caps
6. **Path normalization** — `_rate_path_normalizado`, `_estagio_da_rota` for consistent metrics
7. **Auth** — `_live_autentica` for WebSocket, standard API key middleware for REST
8. **Voice resolution** — `_resolve_voice` accepts id or name, falls back to default voice

---

## File End (lines 9360-9626)

From line 9360 onward:
- OpenAI-compatible proxy section (`POST /v1/audio/speech`) with `_AUDIO_FORMATS` table (mp3/wav/flac/aac/opus/pcm)
- `_resolve_voice` helper
- Metrics middleware `MetricsMiddleware` (lines ~9600-9623): records path, duration, status code, stage, backend with TTL/cap
- Static files mount: `app.mount("/", StaticFiles(directory=BASE / "static", html=True), name="static")`

The file ends with the static mount, registered last so `/api/*` and `/v1/*` routes take precedence.

---

## Test Suite Status
- `tests/test_qa_gate326.py` — 8 passed
- `tests/test_qa_gate328.py` — 15 passed
- All use `test_api` imports (`_voz_fake`, `auth`, `client`, `voices_tmp` fixture)

---

## Summary
The codebase is a complete, production-ready local TTS server with careful attention to latency (prewarm), resource constraints (`_LiveFila` byte caps), history management, and fault tolerance. The single-file architecture keeps all logic in `app.py` with `common.py` (675 lines) providing shared utilities. No new errors or blocking issues remain from this review.
