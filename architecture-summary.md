# Arquitetura do `tts-rod/app.py` — Resumo Completo

Arquiteto principal: servidor FastAPI (Python) com backends TTS baseados em MLX. Operação local/offline ("nada de áudio ou texto sai da máquina"). 9626 linhas, ~64 rotas de API.

---

## 1. HTTP Routes

### `/api/*` (primária)
| Rota | Método | Função |
|------|--------|--------|
| `/api/health` | GET | Health check |
| `/api/build` | GET | Info de build |
| `/api/status` | GET | Status do servidor |
| `/api/metrics` | GET | Métricas de uso |
| `/api/apikeys` | GET/POST/PATCH/DELETE/ROTATE/ENABLED | Gerenciamento de chaves API |
| `/api/backends` | GET/POST/PUT | Configuração de backends TTS |
| `/api/mic-route` | GET | Roteador de microfone |
| `/api/client/mic-router` | GET/POST | Router do mic do cliente |
| `/api/chat/start`, `/api/chat/post`, `/api/chat/get`, `/api/chat/delete`, `/api/chat/debug`, `/api/chat/dsh/models` | POST/GET | Subsistema de chat (sessions, DSH local pool, OpenAI remote fallback) |
| `/api/tunnel/*` | — | Tunnel/remote access (cloudflared connector, launchd agents) |
| `/api/shutdown` | POST | Desligamento seguro |
| `/api/settings` | GET/POST | Settings management (`settings.json`) |
| `/api/voices/*` | — | Gerenciamento de vozes (list, import, export, design) |
| `/api/audio/edit` | POST | Edição de áudio (trim/cut/normalize/denoise/gain/fade/eq) |
| `/api/live/ticket` | POST | Gera ticket de acesso ao live WS |
| `/api/live/ws` | WEBSOCKET | Live interaction (PCM16 16kHz client→server, PCM16 24kHz server→client) |
| `/api/tts` | POST/GET | Geração TTS principal (sync + async worker) |
| `/api/transcribe` | POST | STT local (Whisper/Parakeet) + remoto fallback |
| `/api/stt-partial` | POST | STT parcial/streaming |
| `/api/translate-speech` | POST | Tradução de voz |
| `/api/modify-speech` | POST | Modificar fala (transcreve → TTS novo) |
| `/api/voices/import` | POST | Importação de vozes (zip, 512MB limit) |
| `/api/mic-router` | — | Mic routing |

### `/v1/*` (OpenAI-compatible)
| Rota | Método | Função |
|------|--------|--------|
| `/v1/models` | GET | Lista `tts-1`, `tts-1-hd`, `whisper-1` |
| `/v1/audio/transcriptions` | POST | STT compatível Whisper (json/text/srt/verbose_json/vtt) |
| `/v1/audio/translations` | POST | STT + tradução p/ inglês |
| `/v1/audio/speech` | POST | TTS compatível OpenAI (mp3/wav/flac/aac/opus/pcm) |

### Estágio por rota (`_ESTAGIO_POR_ROTA`)
- `/api/tts`, `/v1/audio/speech` → `tts`
- `/api/live` → `live`
- `/api/chat`, `/v1/chat` → `llm`
- `/api/translate` → `llm`
- `/v1/audio/transcriptions`, `/api/transcribe` → `stt`

---

## 2. Main Components

### Classes
- `_SpeechGate`: FIFO delivery gate com tickets. `begin()`, `deliver()`, `abort()`. Espera duração da fala anterior + `speech_queue_gap_s` (default 0.35s). Cap 5.0s. Props: `depth`, `free_in`, `last_duration`.
- `_LiveFila`: Fila byte-capped com `bytes`, `descartados`, `get()`.
- `TurnEngine`: FSM de turnos (VAD, barge-in, speech_start/end). Config: `prefix_ms`, `silence_ms`, `barge_ms` (150–600, default 300).
- `_LiveFila` (external module referenced): byte-capped queue.

### Módulos/Backends TTS
`_ISOLATED_FAMILIES`: `qwen3_tts, qwen3_custom, qwen3_design, fish, chatterbox, voxcpm2, voxtral_tts, kokoro, moss_nano, indextts, generic` — roda em subprocesso para proteger o servidor de SIGSEGV.
- OmniVoice, Qwen3-TTS, Fish S2, Chatterbox, Kokoro, PocketTTS, VoxCPM2, Voxtral

### Funções-Chave de Geração
- `_run_tts_job(job_id, text, voice_id, voice_path, language, omni, model_override=None, use_queue=True)`: geração principal. Split text in chunks, resolve local/remote, family-specific behavior, retry anomalous chunks (max 2), design-voice regeneration.
- `_run_tts_job_isolated(job_id, text, voice_id, voice_path, language, omni, be, sq=None, model=None)`: subprocesso. `pdir/worker.pid`, `status.json`, `config.json`, log streaming, cleanup in finally, crash handling (SIGSEGV rc=-11/139).
- `_materialize_preset(model, sr, pid)`: gera seed sample para presets com retry (3 tentativas), RMS>=0.05, dur>=3.0s, writes VOICES_DIR/{pid}.wav e .json.
- `_resolve_speech_duration(duration_s, job_id, output_meta)`: prefere max de WAV final / chunks / meta.

### Worker
- `tts_worker.py` (módulo externo): integração com processo worker. Jobs em diretórios `.job-*`.

### Áudio Pipeline
- `_post_process`, `_denoise_audio` (spectral gating), `_time_stretch`, `_wav_duration`.
- `_wav_duration_precise(path)`: wave module frames.
- `_pieces_duration(job_id)`: soma .wav no job dir.

### Chat Subsystem
- Sessions com DSH local agent pool (`_CHAT_DSH_POOL_MAX=3`, `_DSH_MAX_PROCESSOS=5`), OpenAI remote fallback.
- Histórico: `_live_hist_guarda`, `_live_hist_comprime` (.compressa metade mais antiga; limiar `_LIVE_HIST_MAX_MSGS`/`_LIVE_HIST_MAX_CHARS`), `_live_hist_pos_turno`, `_live_hist_ja_comprimindo`.
- Resumidor: `_live_resumidor(sess)` via `TTS_TRANSLATE_BASE_URL`/remote.

### Live WS (`/api/live/ws`)
- **Autenticação**: `?key=` ou `?ticket=`; loopback dispensa.
- **Protocolo**: eventos `ready`, `speech_start`, `transcript_user`, `assistant_text`, `turn_complete`, `interrupted`, `error`, `prewarm`, `stats` (~4Hz), `pong`.
- **Config**: `_LIVE_TICK_S=0.2`, `_LIVE_STATS_MS` default 250, `_LIVE_TTL_S=300`, `_LIVE_MAX_SESSIONS=4`, `_LIVE_PENDENTE_MAX_S=30`.
- **FSM**: `live_turns.TurnEngine`, RNNoise pre-filter (`_live_rnnoise_novo`), `_live_abre_turno` com prewarm wait (`_LIVE_ESPERA_PREWARM_S=2`), pending-turn guard.
- **Sender**: `_live_sender(sess, ws)` — single writer task; stats by clock (`st_proximo`), `_live_manda_stats` writes directly to socket.
- **Events**: `_live_trata_eventos(sess, eventos)` traduz FSM events (barge_in, speech_start, speech_end) to protocol.
- **Pending**: `_live_guarda_pendente(sess, pcm, barge_in)` accumulates PCM with concat e cap `_LIVE_PENDENTE_MAX_BYTES`; `_live_acorda_pendente(sess)` observer wakes when pendente non-empty.
- **Cleanup**: `_live_solta_slot(sess, motivo)` removes from registry; sets `fechar`, `cancelado`; `_live_sender` breaks on socket death; sweeper every 15s (`_live_sweeper`).
- **Stats**: `_live_stats(sess)` returns `ia.pedido/backend`, `stt.ia` (engine model), `tts.sr`, `fs`, `slots_usados`, plus `erro`/`provedor`.

### Speaker Verification
- Resemblyzer `VoiceEncoder`, 256-d embeddings, up to `SPEAKER_MAX_SAMPLES=5` por profile, stored in `_speakers.json`, cosine similarity matching.

### Emotion Detection
- `_emotion_light` (prosody RMS/dyn/pace rules) and `_emotion_accurate` (wav2vec2 SER model), mode off/light/accurate, gate 0.5 confidence threshold.

### YouTube Audio
- yt-dlp com host allowlist (`youtube.com`, `youtube-nocookie.com`, `youtu.be`), redirect safety checks, 10min max, ffmpeg post-processing.

### Remote Integration
- `_ensure_remote_voice` posts `.wav` to remote origin `/voices`, 64-entry LRU cache keyed by (origin, name, mtime_ns).
- `_translate_remote` uses `remote_api_key`; STT uses dedicated `remote_stt_key` or shared key.
- `_upload_limit_bytes(default_mb=64)`: env `TTS_MAX_UPLOAD_MB` capped 0–2048, streams write in 1MB blocks.

### Startup
- `RESOURCE.RLIMIT_NOFILE` guards against EMFILE errors during boot.
- `_idle_unload_loop`: descarrega TTS/STT/tradutor/SER ociosos (`idle_unload_minutes`; 0=off).
- DSH prewarm on backend change.

---

## 3. Authentication Flow

### API Keys
- Persistidas em `.apikeys.json`. Roles: `admin` (full access) / `use` (restricted).
- Migração legacy de `.apikey` env.

### Segurança
- CSP/CSRF/HSTS security headers.
- `_origem_confiavel`: Origin validation on loopback.
- `_resolve_speech_duration`, `_safe_id(v)`: regex `r"[A-Za-z0-9_-]{1,128}"`, raise HTTPException(404, "Id inválido").
- `_voice_path(v)`: resolves `VOICES_DIR/{v}.wav`, validates resolved parent equals `VOICES_DIR` (previne symlink escape).
- Rate limiting com `_RATE_DEFAULT=120`, `_RATE_HEAVY=20`, `_RATE_POLL=1200`; heavy paths: `/v1/audio/speech`, `/api/transcribe`, `/api/stt-partial`, `/api/translate-speech`, `/api/modify-speech`, `/api/voices/import`.
- CORS registrado por último para que 401/429 cruzem CORS.

### Settings View
- `_settings_vista` mascara segredos (`remote_api_key`, `remote_stt_key`, `chat_api_key`) para non-admin.
- `_SETTINGS_ADMIN` fields hidden from non-admin POST.
- `_apply_settings` clamps: `speed` 0.25–4.0, `chunk_max_chars` 60–200 (default 140), `auto_cleanup_minutes` 1–1440, `omni_num_steps` 4–64, `live_num_steps` 4–64, `omni_guidance_scale` 0.0–10.0, `gen_temperature` 0.0–2.0, `speech_queue_gap_s` 0.0–5.0, `speaker_threshold` 0.35–0.9, `idle_unload_minutes` 0–1440 (0=never).
- `_sanitize_instruct`: restringe OmniVoice a vocabulary fechado (`_OMNI_INSTRUCT_VALID`).

---

## 4. TTS Generation Flow

### Sync (`/api/tts` sync mode, `/v1/audio/speech`)
1. Valida input (`input` texto, voz, idioma, format).
2. `_jobs_admit({"text": text[:200]})` — registers job com capacity check; 429 when full (lock-protected, via `_jobs_capacity_check`).
3. Thread: `_run_tts_job(job_id, text, voice_id, VOICES_DIR/{voice_id}.wav, language, omni, use_queue=...)`.
4. MLX exige thread "nova" (stream GPU é thread-local; threadpool do FastAPI reusa threads sem stream).
5. Join com timeout (600s); 504 se exceder.
6. `_encode_audio(wav_path, fmt, speed)`: ffmpeg conversion (mp3/wav/flac/aac/opus/pcm).
7. Retorna `Response(content=data, media_type=mime)`.

### Async (`/api/tts` default with queue)
- `_jobs_admit` → job dir `.job-*` → worker process (`tts_worker.py`) via `_run_tts_job_isolated`.
- Job status via `status.json`; job ownership via `owner.pid` (cross-process boot cleanup).
- `_jobs_ativos()` conta running/queued; `_jobs_snapshot()` cópia sob lock.
- `_ISOLATED_FAMILIES` roda em subprocesso.

### Fila de Falas (`_SpeechGate`)
- Serializa falas: espera prior speech duration + `speech_queue_gap_s`.
- Cap 5.0s; tickets FIFO.

### Jobs Capacity
- `_jobs_admit(extra)`: registers job with capacity check; 429 when full.
- `_jobs_capacity_check` raises 429 before creating job/thread.

---

## 5. Administrative Routes

### `/api/settings` (GET/POST)
- `settings.json` com preservação de chaves desconhecidas.
- `_save_settings()` escreve `settings.json`.
- Campos admin (`_SETTINGS_ADMIN`) ocultos para non-admin POST.

### `/api/apikeys` (GET/POST/PATCH/DELETE/ROTATE/ENABLED)
- Gerenciamento completo de chaves API.

### `/api/backends` (GET/POST/PUT)
- Configuração de backends TTS.

### `/api/metrics` (GET)
- Métricas de uso, `metrics_ttl_dias` (default 14), `metrics_teto_mb` (default 8).

### `/api/shutdown` (POST)
- Desligamento seguro do servidor.

### `/api/tunnel/*`
- cloudflared connector, launchd agents on macOS para acesso remoto.

### `/api/status` (GET)
- Status do servidor, backends, jobs.

---

## 6. Notable Security Points

1. **Rate Limiting**: `_RATE_DEFAULT=120`, `_RATE_HEAVY=20` (paths heavy), `_RATE_POLL=1200`. 429 com `Retry-After` header (exposed via CORS).
2. **CORS**: Registrado por último; `Retry-After` exposto para clientes cross-origin.
3. **Origin Validation**: `_origem_confiavel` no loopback.
4. **Path Traversal**: `_voice_path(v)` valida parent equals `VOICES_DIR`; `_safe_id` regex sanitização.
5. **Zip Bomb Protection**: `_IMPORT_MAX_TOTAL = 512MB` limit para imports de voz.
6. **Sandbox Memory**: `RESOURCE.RLIMIT_NOFILE` contra EMFILE no boot.
7. **Isolated Families**: Backends com SIGSEGV risk rodem em subprocesso (`_ISOLATED_FAMILIES`).
8. **Key Masking**: `_settings_vista` mascara segredos para não-admin.
9. **Input Sanitization**: `chunk_max_chars` clamp 60–200; texto máximo 5000 chars no `/v1/audio/speech`; `speed` clamp 0.25–4.0.
10. **Upload Limits**: `_upload_limit_bytes` — `TTS_MAX_UPLOAD_MB` capped 0–2048, writes em blocos de 1MB.
11. **WebSocket Auth**: Ticket/key required; `4401` code for unauthorized; `4408` for setup timeout; `1013` for busy.
12. **Session Management**: `_live_lock` protege registry; TTL sweep a cada 15s; max 4 sessions; `fechar`/`cancelado` flags.
13. **CSP/CSRF/HSTS**: Headers de segurança em todas as respostas.
14. **MLX Thread Safety**: Stream GPU thread-local; jobs em threads novas.
15. **Anti-Hallucination (STT)**: `_stt_ok` filters blacklist ("e aí"), fragment detection; VAD Silero pre-filter; speaker gate Resemblyzer.

---

## 7. Data Flow Summary

```
Client → /api/tts (or /v1/audio/speech)
    ↓ auth middleware (API key, role check)
    ↓ rate limiter
    ↓ _jobs_admit (capacity check → 429 if full)
    ↓ Thread: _run_tts_job
        ↓ chunk text → resolve voice (VOICES_DIR)
        ↓ local MLX model OR remote backend
        ↓ _SpeechGate (queue mode: wait prior speech + gap)
        ↓ writes OUTPUTS_DIR/{job_id}.wav
    ↓ _resolve_speech_duration (max of WAV/chunks/meta)
    ↓ _encode_audio (ffmpeg → requested format)
    ↓ Response(content=data, media_type=mime)

Live WS: Client → /api/live/ws
    ↓ auth (?key=?ticket=)
    ↓ setup (history, vad config, barge_ms)
    ↓ _live_sweep / registry check (_LIVE_MAX_SESSIONS)
    ↓ _live_pipe_novo (pipeline/turns engine + RNNoise)
    ↓ _live_loop: binary=audio, text=command
        ↓ audio → _live_pcm_limpo (RNNoise) → TurnEngine.feed(pcm)
        ↓ events → _live_trata_eventos (barge_in/speech_start/speech_end)
        ↓ speech_end → _live_abre_turno (prewarm wait, pending guard)
    ↓ _live_sender(sess, ws): binary PCM16 24kHz
    ↓ stats ~4Hz (ia.pedido/backend, stt.ia, tts.sr)
    ↓ cleanup: _live_solta_slot, pipe.close(), registry pop
```

---

## 8. Environment Variables (Key)

- `TTS_MAX_UPLOAD_MB`: upload limit (0–2048)
- `TTS_TRANSLATE_BASE_URL` / `remote_base_url`: translation/LLM endpoint
- `remote_api_key`, `remote_stt_key`, `chat_api_key`: remote service keys
- `VOICES_DIR`, `OUTPUTS_DIR`, `BASE`: paths
- `_RATE_DEFAULT`, `_RATE_HEAVY`, `_RATE_POLL`: rate limits
- `speech_queue_gap_s`: queue gap (0.0–5.0)
- `idle_unload_minutes`: idle unload (0–1440)
- `metrics_ttl_dias`, `metrics_teto_mb`: metrics retention
