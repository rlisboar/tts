# Projeto tts-rod — Entendimento Completo (Resumo Executivo)

> Gerado automaticamente. Atual: October 2025. Arquivos analisados: README.md, LIVE.md, CONVERSA-IA.md, app.py (9626 linhas), live_pipeline.py (1617), live_turns.py (1011), backends.py, common.py (675), tts_worker.py, settings.json, requirements.txt, tests/, client/, scripts/.

---

## 1. O que é

**TTS-STUDIO**: aplicativo web 100% local de clonagem de voz TTS para Mac (Apple Silicon). Browser → grava voz → sintetiza. Roda em `http://127.0.0.1:7860` via `./run.sh`. Nenhum áudio ou texto sai da máquina por padrão.

**Modelo principal**: OmniVoice (Xiaomi/k2-fsa, masked-diffusion, ~0.6B params, Apache-2.0), quantizado MLX bf16, zero-shot, 646 idiomas, RTF ~0.8 em M3.

**API compatível com OpenAI**: `/v1/audio/speech`, `/v1/audio/transcriptions`, `/v1/models`.

---

## 2. Arquitetura (3 camadas)

```
┌─────────────────────────────────────────────────────┐
│  Browser (static/index.html + live-worklet.js)      │
│  AudioWorklet PCM16 16kHz capture                    │
│  Mic router (BlackHole) via client/mic_router.py     │
└──────────────┬───────────────────────────┬──────────┘
               │ HTTP              WebSocket (live)
┌──────────────▼───────────────────▼─────────────────┐
│  app.py — FastAPI + uvicorn                         │
│  /api/* (REST)   /v1/* (OpenAI compat)              │
│  /api/live/ws (real-time engine)                    │
├───────────────┬───────────────┬────────────────────┤
│  TTS sync     │  Worker       │  Live pipeline     │
│  (thread MLX) │  (subprocess) │  TurnEngine + VAD   │
└───────────────┴───────┬───────┴────────────────────┘
                        │ MLX Metal
┌───────────────────────▼────────────────────────────┐
│  backends.py — catálogo (OmniVoice, Kokoro,          │
│    Voxtral, Fish, Qwen3, PocketTTS...)               │
│  common.py — utilitários compartilhados              │
└─────────────────────────────────────────────────────┘
```

---

## 3. Componentes principais (o que cada arquivo faz)

### `app.py` (9626 linhas — o core)
Servidor FastAPI único. Responsabilidades:
- **Rotas REST** (`/api/*`, `/v1/*`): TTS, STT, chat, vozes, settings, metrics, tunnel, shutdown
- **Live WebSocket** (`/api/live/ws`): sessão por conversa, ticket one-use, FSM de turnos
- **Autenticação**: `.apikeys.json`, roles `admin`/`use`, rate limiting, origin validation
- **Geração TTS**: sync (thread MLX) + async (subprocesso via `tts_worker.py`), `_SpeechGate` para fila de falas
- **Admin**: settings clamps, keys management, metrics (ttl 14 dias/8MB)
- **Segurança**: CORS por último, path traversal guard (`_voice_path`), zip bomb 512MB, key masking

### `live_pipeline.py` (1617 linhas)
Engine de conversa em tempo real (estilo Gemini Live):
- `_live_ws`: gestão do WebSocket, autenticação (`?key=`/`?ticket=`)
- `_live_pipe_novo`: cria `LivePipeline` + `TurnEngine` + RNNoise pré-filtro
- `_live_sender(sess, ws)`: único escritor; PCM16 24kHz server→client
- `_live_trata_eventos`: traduz eventos da FSM (barge_in, speech_start, speech_end)
- `_live_stats`: ~4Hz (`ia.pedido/backend`, `stt.ia`, `tts.sr`)
- Config: `_LIVE_TTL_S=300`, `_LIVE_MAX_SESSIONS=4`, `_LIVE_ESPERA_PREWARM_S=2`
- History compression: `_live_hist_comprime` (resumo 3-5 linhas), `1800s` resume

### `live_turns.py` (1011 linhas)
FSM de turnos:
- `TurnEngine` FSM: `_processar()` com `_avaliar_abertura`, `_avaliar_turno`, `_calibrar`
- Barge-in: `_barge_com_turno_aberto`, `_degrau_sobre_eco`
- Eventos: `speech_start`, `speech_end`, `turn_complete`, `interrupted`
- Config: `barge_in_ms` (150–600), `playback_por_duracao`, `eco_so_tocando`

### `backends.py` (35KB)
Catálogo de backends TTS/STT com adaptador unificado. Rotaciona entre local e remoto com fallback. Backends isolados (subprocesso) para proteger o servidor de SIGSEGV: `qwen3_tts, qwen3_custom, qwen3_design, fish, chatterbox, voxcpm2, voxtral_tts, kokoro, moss_nano, indextts, generic`.

### `common.py` (675 linhas)
Utilitários compartilhados entre app e worker: sanitização de texto, funções DSP (fade, eq, denoise), splits de texto, carregamento de modelos.

### `tts_worker.py` (15KB)
Worker isolado em subprocesso: jobs em `.job-*`, `status.json` de progresso, crash handling (SIGSEGV rc=-11/139), cleanup automático.

---

## 4. Fluxo de geração TTS

```
Client → /api/tts (POST) ou /v1/audio/speech
  ↓ autenticação (API key + role)
  ↓ rate limiter (429 se cheio)
  ↓ _jobs_admit (capacidade; TTS_JOBS_ACTIVE_MAX=20)
  ↓ Thread nova (MLX stream é thread-local)
    ↓ chunk texto → resolve voz (VOICES_DIR/{voice_id}.wav)
    ↓ local MLX OR remote backend
    ↓ _SpeechGate (fila: espera duração anterior + speech_queue_gap_s 0.35s)
    ↓ writes OUTPUTS_DIR/{job_id}.wav
  ↓ _encode_audio (ffmpeg → mp3/wav/flac/aac/opus/pcm)
  ↓ Response(content=data, media_type=mime)
```

**Async (default)**: job dir `.job-*` → worker process via `_run_tts_job_isolated`; status via `status.json`.

---

## 5. Fluxo Live (WebSocket)

```
Client → POST /api/live/ticket (one-use) → WS /api/live/ws?key=…|ticket=…
  ↓ autenticação, slot verificação (_LIVE_MAX_SESSIONS=4)
  ↓ _live_pipe_novo → TurnEngine + RNNoise + prewarm (2s)
  ↓ _live_loop:
      audio PCM16 16kHz → RNNoise → TurnEngine.feed(pcm)
      eventos → _live_trata_eventos (barge_in/speech_start/speech_end)
      speech_end → _live_abre_turno (buffer, prewarm gate)
  ↓ _live_sender(sess, ws): PCM16 24kHz server→client (binary)
  ↓ stats ~4Hz + pong/ping
  ↓ cleanup: _live_solta_slot (TTL sweep a cada 15s)
```

**Estados da sessão**: `ready` → `thinking` → `chatting` → `confirmed`. Expire 1h sem uso.

---

## 6. Segurança (pontos críticos)

1. **API Keys**: `.apikeys.json`, roles `admin`/`use`. Key de uso: gera áudio/transcreve, mas não acessa configurações/remote.
2. **Rate Limiting**: `_RATE_DEFAULT=120`, `_RATE_HEAVY=20` (rotas pesadas), `429` com `Retry-After`.
3. **CORS**: registrado por último para que 401/429 cruzem CORS.
4. **Path Traversal**: `_voice_path` valida parent == `VOICES_DIR`; `_safe_id` regex sanitização.
5. **Zip Bomb**: `_IMPORT_MAX_TOTAL = 512MB`.
6. **Key Masking**: segredos mascarados (`••••abcd`) para non-admin.
7. **MLX Thread Safety**: stream GPU thread-local; jobs em threads novas.
8. **Anti-hallucinação STT**: blacklist ("e aí"), fragment detection, VAD Silero pré-filtro.
9. **Upload Limits**: `_upload_limit_bytes` capped 0–2048MB, blocos de 1MB.
10. **WebSocket Auth**: `4401` unauthorized, `4408` setup timeout, `1013` busy.

---

## 7. Configuração

**`settings.json`** (~90 chaves): modelo, voz padrão (`af_heart`/Kokoro, `alba`/Pocket, `casual_male`/Voxtral), parâmetros MLX (num_steps 64, guidance 2.0, seed 42), STT (whisper-mlx beam 9), denoise 0.7, chat backend ([dsflash, deepseek-flash-41]).

**`requirements.txt`**: pins rigorosos (mlx-audio 0.4.4, mlx-whisper 0.4.3, mlx-lm 0.31.3, transformers 5.10.2, silero-vad 6.2.1). Teste `test_requirements_pinos.py` exige `==` ou allowlist.

**`run.sh`**: eleva ulimit, cria DSH_HOME dedicado (~/.dsh-tts), valida venv `.venv-mlx` (Python 3.12), loop auto-restart uvicorn, sweeper de sessões >48h.

**`pytest.ini`**: timeout 90s thread, `tmp_path_retention_policy = failed` (segurança: não vazar chaves no CI).

---

## 8. Testes

~56 arquivos: `pytest` (pure functions + TestClient, sem MLX), smokes shell, e2e.
- **Cobertura mínima**: 70% overall (`app` 64%, `common` 81%, `live_pipeline` 81%, `live_turns` 93%).
- Arquivos chave: `test_app.py`, `test_api.py`, `test_common.py`, `test_backends.py`, `test_live_*.py` (14 arquivos), `test_dsh_client.py`, `test_mic_router.py`.
- CI gate: pyflakes/ruff, tests with 180s timeout.

---

## 9. Rede / Acesso Remoto (default OFF)

| Controle | O que sai | Destino |
|---|---|---|
| `remote_tts` | Texto + referência | `remote_tts_url` |
| `remote_stt` | Áudio | `remote_stt_base_url` |
| `remote_translate` | Texto | `remote_base_url` |
| `chat_base_url` | Histórico + preprompt | LLM remoto |
| `chat_backend: "dsh"` | Texto do turno | Provider route |

Hugging Face metadata check no Whisper (DNS, zero áudio). `HF_HUB_OFFLINE=1` desativa.

Tunnels: SSH reverse (`tunnel.sh`), Cloudflare (`cloudflare.sh`). Apontam para LAN IP, nunca 127.0.0.1.

---

## 10. Métricas (`/api/metrics`)

Append-only, `99 bytes`/linha: ts, rota normalizada, ms, HTTP code, estágio (stt/llm/tts/live), backend id. Sem texto/áudio/chaves. `GET /api/metrics?janela_min=60&rota=/api/tts` (admin).

---

## Como eu devo usar este conhecimento

- Perguntas sobre fluxo de dados → consultar esta documentação + arquivos específicos
- Debugar problemas → `tests/`, `evidence/` (logs de issues), `.tmp-lock/`
- Mudanças em `tts_worker.py`/`live_pipeline.py` → rodar smoke test obrigatório
- Qualquer operação de escrita na raiz do projeto precisa justificação sandbox
