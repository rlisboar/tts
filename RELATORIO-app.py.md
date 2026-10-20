# Relatório técnico — `app.py` (TTS-STUDIO, 7513 linhas)

Escopo: leitura integral do arquivo, sem modificações. Referências `L<linha>`.
Nada foi executado contra o servidor; achados que dependem de runtime estão marcados **(a confirmar)**.

---

## 1. Arquitetura geral

**Natureza**: servidor FastAPI monolítico, single-process, "tudo local" (docstring L1-6). Todo o estado de modelo, cache, jobs, sessões de chat e sessões Live vive no processo. Não há `lifespan`, `startup`/`shutdown` handler, nem `signal` handler: o boot é feito por **efeito colateral de import** e o desligamento por `os._exit(0)`.

### Boot (tudo em tempo de import)
| Ordem | Efeito | Linha |
|---|---|---|
| 1 | Marca `_BUILD_INI`/`_BUILD_TS` (métrica de boot) | L27-28, usados em L1403 |
| 2 | `os.environ.setdefault("ORT_DISABLE_TELEMETRY","1")` antes dos imports locais | L49 |
| 3 | `_piso_nofile()` sobe `RLIMIT_NOFILE` até `min(hard, 65536)` | L55-63, L121 |
| 4 | `_instala_guarda_nofile()` **monkeypatcha `resource.setrlimit`** para nunca descer NOFILE (mlx_lm baixava o hard) | L66-122 |
| 5 | Importa `dsh_client`, `backends`, `common` | L125-139 |
| 6 | Cria `voices/`, `outputs/`; `mkdir(exist_ok=True)` | L147-148 |
| 7 | **Limpeza destrutiva de boot**: mata `worker.pid` órfãos com `killpg(SIGTERM)` e apaga `.job-*` sem dono vivo | L242-263 |
| 8 | Carrega/gera `settings.json` (com backup `.corrupt`) | L388-400 |
| 9 | Materializa chaves novas de settings no disco | L443-450 |
| 10 | `_load_apikeys()` — carrega, migra `.apikey`/env, ou **gera chave nova** | L576-635, chamada em L828 |
| 11 | `_chat_dsh_prewarm("startup")` — sobe processo `dsh` em thread se `chat_backend=dsh` | L1979-1982 |
| 12 | Thread `_auto_cleanup_loop` (limpeza a cada 60 s) | L4401 |
| 13 | `_build_hash()` congelado em `_BUILD_CODIGO` (lê 7 módulos) | L1366-1387 |
| 14 | Thread `_idle_unload_loop` | L4495-4532, L7495 |
| 15 | `add_middleware(CORSMiddleware)` e `app.mount("/", StaticFiles)` | L7500-7513 |

O ponto 7 explica um desenho de PID/`owner.pid` (L160-263): um segundo processo (pytest/IDE) não deve apagar os trechos do servidor vivo. `_job_dir_ativo` (L217-234) decide por `owner.pid` + `os.kill(pid,0)` + `ps` (L163-214); sem `ps`, atividade recente < 300 s (L160).

### Variáveis de ambiente (todas lidas, nenhuma validada contra injeção)
`TTS_ROD_MODEL` (L269), `TTS_ROD_API_KEY`/`TTS_ROD_ADMIN_KEY` (L521-522), `TTS_CORS_ORIGINS` (L849), `TTS_RATE_LIMIT`/`TTS_HEAVY_RATE_LIMIT`/`TTS_POLL_RATE_LIMIT`/`TTS_RATE_MAX_BUCKETS` (L863-890), `TTS_ROD_BASE_PATH` (L999), `TTS_ROD_WHISPER`/`TTS_ROD_PARAKEET`/`TTS_ROD_TRANSLATE`/`TTS_ROD_SER` (L504-507, L5087), `TTS_JOBS_ACTIVE_MAX` (L3412), `TTS_MAX_UPLOAD_MB` (L5271), `TTS_ROD_YT_COOKIES` (L5452), `TTS_CHAT_*` (L1732-1744, L1817-1856), `TTS_TRANSLATE_BASE_URL`/`TTS_TRANSLATE_MODEL`/`TTS_STT_BASE_URL` (L4617-4625, L4800-4808, L4834), `TTS_CHAT_DEBUG` (L2211), `TTS_LIVE_*` (L5913, L6088-6094, L6401-6412, L6897-6900), `LIVE_LOG`/`LIVE_DEBUG_TTS` (L5922, L6446).

### Estado global
- Modelo: `_model`, `_model_lock`, `_gen_lock` (Lock simples, não reentrante), `_model_state` (L1085-1091), cache LRU de condicionadores de voz `_conds_cache` (máx. 4, L1194-1195, L1226-1228).
- Jobs: `_jobs` (`OrderedDict`), `_JOBS_MAX=20`, `_JOBS_ACTIVE_MAX` (env, default 20), `_jobs_lock` (RLock, L3403-3420).
- Fila de falas: `_SpeechGate` com `threading.Condition`, tickets FIFO, `_free_at` (L3479-3605).
- Chat: `_chat_sessions`, `_chat_lock`, `_chat_executor` (8 threads) (L1696-1699).
- Live: `_live_sessions`, `_live_tickets`, `_live_historico`, `_live_lock`, `_provedor_estado` (L5924, L6099-6117, L6902).
- STT/MT/SER: `_mt`, `_mt_lock`, `_stt_lock`, `_pk`, `_ser`, `_ser_lock`, `_speaker_encoder` (L4424-4426, L4967, L5085-5086, L5551).
- Settings: `_settings` (dict **sem lock**) e `_SETTINGS_DEFAULTS` (84 chaves, L278-387).
- Chaves: `_apikeys`, `_apikeys_lock` (L523-524).

### Concorrência
Serialização correta no caminho caro: `_gen_lock` cobre load+geração local e o spawn/poll de worker (L3852); remoto não pega lock (`_NO_LOCK`, L1089, L4030). Threads criadas por requisição: job (`threading.Thread` em L4274, L5854, L5883), chat (L2150, L2183), limpeza agendada (L4487-4492), launchctl adiado (L2355-2369), prewarm (L1958), sweeper Live (L6243), observador de pendente (L6627), compressor de histórico (L7017). O WS tem **uma única task de envio** para não haver `send` concorrente (L6826-6884).

---

## 2. Autenticação / autorização

Modelo: **loopback é isento de tudo**; qualquer acesso pela LAN/internet exige chave válida (`_exige_chave`, L973-992). Só `127.0.0.1`/`::1`/`localhost` contam como local (`_is_local`, L725-736) — os IPs da própria máquina **não** são isentos, por causa do túnel SSH reverso (L726-731); `tunnel.sh` mira o IP da LAN e `cloudflare.sh` idem (`service":"http://$ORIG:7860"`), coerente.

- Extração da chave: `Authorization: Bearer` ou `X-API-Key` (L652-656). Comparação com `compare_digest` (L659-666).
- `_key_is_valid` aceita env, chave admin e chaves gerenciadas (L739-755). `TTS_ROD_ADMIN_KEY` também autentica (correção documentada em L742-746).
- Papéis: `admin` | `use` | `None` (legado). `role` vazio **sem** `TTS_ROD_ADMIN_KEY` → toda chave válida é admin (L772-790). `_normaliza_role` valida (L793-802).
- Mascaramento: `_mask_secret` devolve `••••••••` para segredos ≤10 chars e `abcd…wxyz` acima (L531-535); `_public_key_row` mascara e só inclui `secret` com `reveal` (L814-825). `GET /api/apikeys?reveal=1` revela se for loopback **ou** admin autenticado (L1473-1480). `_ENV_API_KEY` aparece como linha read-only `__env__` (L1484-1496), não pode ser renomeada/rotacionada/apagada (L1544, L1565, L1583).
- Segredos de serviços externos em settings: `_SETTINGS_SECRETS = remote_api_key, remote_stt_key, chat_api_key` (L2454) mascarados por `_mascara_setting_secret` com prefixo `_MASCARA_SETTING="••••"` (L2458-2463); no POST, valor que começa com `••••` é descartado e o guardado permanece (L2473-2476).
- Campos admin (23, `_SETTINGS_ADMIN`, L2443-2453): `model`, `translate_model`, `stt_whisper_repo`, todos os `remote_*` de destino/chave/modelo, `remote_tts`, `remote_translate`, `remote_stt`, e todos os `chat_*` de backend/dsh/chave. Chave de uso que manda campo admin **não** leva 403: o campo é removido e devolvido em `admin_ignored` (L2477-2484, L2632, L2654).
- `POST /api/tts` com `model` no body: aplica como global só se admin (L4266-4272); chave de uso gera com o modelo do pedido sem sequestrar settings.
- CORS: `TTS_CORS_ORIGINS` (lista) ou `*`, métodos/headers `*`, `expose_headers=["Retry-After"]`, registrado **por último** para ser a camada mais externa (L849-855, L7500-7510).
- Rate limit (L860-970): janela 60 s, default 120/min, pesado 20/min (`/v1/audio/speech`, `/api/transcribe`, `/api/stt-partial`, `/api/translate-speech`, `/api/modify-speech`, `/api/voices/import`, e `/api/tts` exato — L865-868), polling 1200/min (`/api/status`, `/api/mic-route`, `/api/tts/jobs/`, `/api/chat/`, `/api/outputs` — L873-875). **Loopback não é limitado** (L942-948). Identidade = sha256 de `x-api-key`/`authorization`/IP (L953-954). Buckets normalizam IDs em `*` e são podados acima de 10 000 (L890-935). Resposta 429 com `Retry-After: 60` (L965-968).
- Ordem das camadas: CORS (mais externa) → `_strip_base_path` → `_exige_chave` → `_rate_limit` → rota (registros L7500, L1038, L973, L938). Logo o 401 sai com cabeçalhos CORS, mas **não** consome cota de rate limit.
- Docs: `/openapi.json`, `/docs`, `/redoc` exigem chave quando não é loopback (L978-982). `/health` é aberto (L1352-1355).

---

## 3. Famílias de rotas

**Páginas**: `GET /` e `/index.html` com nonce CSP por resposta (L1066-1078); estático montado em `/` por último (L7513).

**Build/status/saúde**: `/health` (L1352), `/api/build` (versão, `boot_ts`, `boot_ms`, `codigo` congelado, `admin_fields` — L1390-1405), `/api/status` (L1422-1466).

**Chaves**: `GET/POST /api/apikeys`, `PATCH /{id}`, `POST /{id}/rotate`, `DELETE /{id}`, `POST /apikeys/enabled` (L1473-1609) — todos exigem admin exceto o GET.

**Catálogo/roteador de mic**: `/api/backends` (L1612), `GET/POST /api/mic-route` (L1630-1645), `GET /api/client/mic-router` (zip com `config.json` contendo `server_url` + **a chave primária em claro**, L1648-1688).

**Chat**: `POST /api/chat/start`, `POST/GET/DELETE /api/chat/{sid}`, `POST /api/chat-debug` (só com `TTS_CHAT_DEBUG=1`, grava `/tmp/tts-chat-debug.log`), `GET /api/chat/dsh/models` (descoberta de modelos do harness ACP, cache 300 s, inclui estado do patch de bridge — L2001-2031). Worker LLM com `gen` para descartar resposta invalidada por barge-in (L2074-2128); teto 150 s (L2122-2128).

**Túnel/shutdown**: `GET /api/tunnel/status` (pgrep, launchctl, probe HTTP em `/health` — L2276-2318), `POST /api/tunnel/{restart,stop,start}` (launchctl adiado 1,5 s porque o processo que serve a requisição morre — L2351-2416), `POST /api/shutdown` (`os._exit(0)` após 0,4 s, L2419-2427).

**Settings**: `GET/POST /api/settings` (L2487-2655), com validação/clamp campo a campo (L2681-2886) e rollback em RAM (L2632-2639).

**Vozes**: `GET /api/voices` (inclui presets virtuais não materializados, L2889-2909), `POST /api/voices/design` (gera amostra-semente do instruct+seed e salva como voz — L2916-2966), `POST /api/voices` (upload+denoise+duração mínima 3 s — L2969-3015), `POST /{id}/denoise` (L3018-3044), `GET /{id}/audio` (L3047-3052), `GET /api/voices/export` (zip em temp + `BackgroundTask` de unlink, L3055-3090), `POST /api/voices/import` (staging + validação + rollback, L3098-3225), `POST /api/audio/edit` (trim/cut/normalize/denoise/gain/fade/eq + soft-limit — L3245-3321), `POST /{id}/replace`, `GET /{id}/peaks`, `DELETE /{id}` (L3324-3393).

**Síntese**: `POST /api/tts` (L4188-4284), `GET /api/tts/jobs/{id}` (L4287-4292), `GET /api/tts/jobs/{id}/pieces/{index}` (bloqueado até `pieces` liberar — L4295-4311).

**Saídas**: `GET /api/outputs` (lista todo o `outputs/*.json`, sem paginação — L4314-4325), `GET /{id}/audio`, `DELETE /api/outputs` (apaga tudo), `DELETE /{id}` (L4328-4416).

**STT/tradução**: `POST /api/translate/warmup` (L5170-5202), `POST /api/models/unload` (L5205-5214), `POST /api/stt-partial` (prévia, sem filtros — L5313-5328), `POST /api/transcribe` (VAD + gate de locutor + `_stt_ok` — L5709-5746), `POST /api/translate-speech` (L5835-5862), `POST /api/modify-speech` (sem passo de LLM — L5865-5887).

**YouTube**: `POST /api/youtube-audio` (allowlist de host, validação de `/redirect?q=`, máx. 10 min, L5494-5541).

**Locutor**: `POST /api/speaker/enroll`, `GET /api/speaker/profiles`, `DELETE /api/speaker/{name}`, `POST /api/speaker/check` (L5643-5706).

**Live**: `POST /api/live/ticket` (L6169-6175), `WS /api/live/ws` (L7027).

**OpenAI-compatível**: `GET /v1/models` (lista fixa `tts-1`, `tts-1-hd`, `whisper-1` — L7351-7357), `POST /v1/audio/transcriptions`, `/v1/audio/translations`, `/v1/audio/speech` (síncrono, `t.join(600)`, 504 no estouro — L7418-7491). `prompt` e `temperature` são aceitos e ignorados (L7419-7432).

---

## 4. Pipeline de síntese

1. `POST /api/tts` valida **antes** de tocar em config: texto não vazio, ≤5000 chars (L4214-4217; `pre_prompt` é prefixado antes, L4200-4201), resolve família/`omni` (L4213), resolve voz (design, `_voice_path`, preset, `_resolve_voice`, L4222-4241) e 404 se o backend exige clone e a voz não existe (L4251-4255).
2. Admissão com teto de ativos → 429 + `Retry-After: 5` (L3666-3704, L4258).
3. Thread `_run_tts_job` (L3989): fila de falas opcional, resolução de backend; famílias em `_ISOLATED_FAMILIES` vão para **worker em subprocesso** (L3771-3777, L3792-3986) — `Popen` com `start_new_session=True`, Python do `.venv-mlx` (fallback `sys.executable`), config JSON com texto/voz/settings no dir do job, `status.json` pollado a 350 ms, `SIGSEGV` (rc -11/139) reportado com cauda do log (L3976-3985). Omni/Pocket ficam in-process.
4. In-process: `_split_text(_sanitize_text(text), max_chars=_settings["chunk_max_chars"])` (L4022; `split_text` em `common.py` L156-191, clamps 60–200 em L2697). Cada trecho ganha ponto final se faltar (L4103-4104) e passa por `_generate_chunk`; família com speed nativo (`NATIVE_SPEED_FAMILIES`) não recebe time-stretch (L1249-1255).
5. Retry: 2 tentativas com jitter de seed/temperatura **só** em voz clonada, nunca em voice design (L4109-4134); `_anomalo` (RMS < 0,01 ou áudio curto demais para o texto, L1311-1324) dispara o retry.
6. Pós: `_normalize` → `_trim_tail_silence` → `_apply_audio_fx` (EQ/ganho) → `_fade_edges` → silêncio entre trechos (`CHUNK_SILENCE_S`) → `sf.write` PCM16 (L4135-4138). `_release_mlx_memory()` a cada trecho (L4143-4145).
7. Final: `_write_wav_concat` gera `outputs/<id>.wav`, meta JSON com parâmetros e `elapsed` (L4149-4167), entrega pela fila (L4169-4172), status `done`; limpeza dos trechos em 90 s (L4175).
8. Cache: só `_conds_cache` (ref_tokens por voz+mtime+ref_max, LRU 4 — L1207-1228); **não há cache de áudio por texto**. Invalidação por `clear()` em denoise/replace/import/troca de modelo (L3033, L3220, L3356, L1161).
9. Formatos e ffmpeg: só a rota `/v1/audio/speech` converte (`mp3/wav/flac/aac/opus/pcm`, L7305-7312) via `_encode_audio` (timeout 120 s, L7337-7348), com `_atempo_chain` para speed; `/api/tts` entrega WAV. `FFMPEG` resolve `which` → `imageio_ffmpeg` → `/opt/homebrew/bin/ffmpeg` (L833-846).
10. Remoto: `_tts_remote_chunk` (L4681-4761) — 1 retry, timeout 300 s, reamostragem, time-stretch local; voz enviada por `_ensure_remote_voice` (L4643-4671).

---

## 5. Integração Live

**Ticket**: `POST /api/live/ticket` emite `token_urlsafe(24)` com TTL 60 s, uso único, teto 512 pendentes com evicção dos mais antigos (L6115-6136). Consumo com `pop` (L6139-6145). O motivo declarado é não deixar a chave em query string (L6108-6114).

**Auth do WS** (`_live_autentica`, L6148-6166): loopback → livre; auth desligada → livre; senão `?ticket=` → `?key=` → header; ticket inválido **não** curta o fluxo (L6154-6157), mas o ticket tentado é consumido mesmo assim. Em loopback o ticket **não** é consumido (L6158-6159).

**Handshake**: `await ws.accept()` **antes** de autenticar (L7029) — não há `accept(subprotocol=…)`, ou seja, **nenhum subprotocolo é negociado**; o cliente não oferece nenhum (index.html L7046-7047) e também não há tratamento de `h2`: o WS depende de HTTP/1.1 (upgrade), então atrás de proxy com HTTP/2 puro o caminho não existe — vive de nginx/`wss` com upgrade **(a confirmar em produção)**. Erros saem como JSON + `close(4401/4408/4400/1013)` em vez de rejeição de handshake (L7030-7052).
`setup` (10 s de timeout) valida voz, `session_id` (`[A-Za-z0-9_-]{1,64}`), `system` (4000 chars), `vad.silence_ms` 100–5000, `vad.prefix_ms` 0–2000, `history` até 200 itens (L6188-6227).

**Protocolo**: cliente→servidor: `setup`, frames binários PCM16 16 kHz, `end_of_speech`, `cancel`, `ping`. Servidor→cliente: `ready` (formato/sr/canais), `speech_start`, `transcript_user`, `assistant_text{delta}`, `turn_complete`, `interrupted`, `error{code,message}`, `prewarm`, `stats` (~4 Hz), `pong`, `turno_pendente`, áudio PCM16 24 kHz (L5890-5908, L7157-7161).

**Sessão**: registro em `_live_sessions` com teto 4 (`TTS_LIVE_MAX_SESSIONS`) checado atomicamente com a inserção (L7121-7137); TTL 300 s varrido a cada 15 s (L6230-6250); `session_id` do cliente é **adotado** e retoma histórico (L7071-7088) — quem adivinhar/obtiver um id assume o contexto alheio (decisão registrada L7073-7079).

**Barge-in**: `barge_in` da FSM → `_live_cancela` (L6699-6701, L6418-6443) incrementa `turno`, seta `cancelado`, cancela o pipeline; o pendente **sobrevive** (só `cancel` do cliente o descarta, L6421-6435). Janela intra-turno ligada/desligada por env (L6398-6412).

**Pendente**: fala com pipeline ocupado é concatenada (teto 30 s, corta o mais antigo, L6547-6587), com lock dedicado e contadores (L6526-6545, L6590-6610).

**Resumo de contexto**: acima de 24 msgs/12 000 chars, a metade antiga é resumida em thread no fim do turno, com o backend **efetivo** da sessão (dsh via pool, senão `_chat_llm`), falha mantém o histórico cru (L6965-7010). Retomada por `_live_historico` com TTL 1800 s, 32 registros, contador de geração contra socket zumbi (L6897-6963).

**Observabilidade**: `stats` só com metadados (L5910-5922, L6030-6085), `_live_stage` mede estágio, `_live_stats_ia` denuncia fallback dsh→openai (L6006-6027).

---

## 6. Segurança

**Pontos fortes**
- `_safe_id` (`[A-Za-z0-9_-]{1,128}`, 404) em voz, áudio, peaks, delete, outputs e dir de job (L3637-3643, L3024, L3049, L3329, L3366, L3384, L3622, L4330, L4407); `_piece_dir` também passa por ele (L3621-3622).
- `_voice_path` recusa id que escape de `voices/` **após resolver symlink** (L2658-2678).
- Import de vozes: regex no nome da entrada, descarte de >100 MB por arquivo, teto de 512 MB total, staging + publicação com rollback (L3093-3210).
- Uploads com teto (`TTS_MAX_UPLOAD_MB`, ≤2048, L5268-5310) e leitura em blocos; import com streaming (L3111-3126).
- Zip de export em arquivo temporário (sem dobrar RAM) (L3072-3090).
- Allowlist de YouTube com ponto obrigatório antes do domínio e validação do `q=` de `/redirect` (L5349-5362, L5501-5523).
- CSP por header com nonce por resposta e SRI (`sha384`) nos dois bundles de CDN (`static/index.html` L20-25); `frame-ancestors 'none'`, `base-uri 'none'`, `object-src 'none'` (L1007-1014). `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer`, `Permissions-Policy` (L1051-1056).
- Segredos em disco com `chmod 600`: `.apikeys.json` (L570), `.apikey` (L547), `settings.json` (L437), `.speaker-profiles.json` (L5589).
- `stt_whisper_repo` validado por regex (L2790-2791); `chat_dsh_model` validado pelo cliente dsh (L2850-2852); `instruct` reduzido a vocabulário fechado (L2530-2548).

**Riscos / pontas soltas**
1. **CSRF/DNS-rebinding em loopback.** Não há verificação de `Origin` em nenhuma rota. Qualquer página aberta no navegador pode chamar `http://127.0.0.1:7860` (o CORS default é `*`, L7502) e, como a origem é loopback, **pula auth e rate limit** — inclusive `GET /api/apikeys?reveal=1`, que devolve todos os segredos porque `local=True` (L1477-1480), `DELETE /api/outputs` (L4336), `POST /api/shutdown` (L2419) e `GET /api/client/mic-router`, cujo zip carrega a chave primária (L1669).
2. **Metade das rotas não distingue admin de uso**: shutdown, tunnel start/stop/restart, CRUD/import/export/delete de vozes, `DELETE /api/outputs`, `/api/models/unload`, `denoise`/`replace`, `speaker/*`, `live/ticket` — todas abertas a qualquer chave válida (L2377-2416, L2419, L2889-3393, L4336, L5205, L6169).
3. **Apagar a última chave desliga a auth**: `delete_apikey` permite e `_auth_enabled` fica falso sem chaves e sem env (L1579-1594, L638-649) → LAN aberta sem aviso.
4. `remote_tts_extra` não é campo admin e é aplicado **depois** de `text` no corpo remoto (L4706-4728) — chave de uso reescreve parâmetros e o próprio texto no modo remoto.
5. `os._exit(0)` no shutdown (L2424): sem fechar pipelines, workers filhos (sessão própria), dsh quente ou temporários.
6. `provedor_estado`, `mic_route`, `settings` são globais sem lock — leitura/escrita de dict é atômica por operação, mas atualizações compostas de settings não são.
7. `_git_version` roda `git` no import (timeout 3 s) e `/api/build` devolve `codigo`/`boot_ms` — fingerprinting (exige chave fora do loopback).
8. `host` do cabeçalho e `x-forwarded-proto` são confiados para montar URL/chave no zip do mic-router e para HSTS (L1664-1669, L1062).
9. `_index_com_nonce` faz `re.sub` de `<script>` na linha isolada (L1035) — qualquer mudança de formatação do `index.html` quebra o nonce silenciosamente.
10. `/docs`, `/redoc`, `/openapi.json` sem CSP (L1017).
11. `name` de voz não tem limite de tamanho (L3006) e vai para o JSON/meta renderizado na UI; não há escape no servidor.
12. `GET /api/outputs` lista todo o diretório sem paginação (hoje 2892 entradas) (L4314-4325).

---

## 7. Robustez / observabilidade

- `/api/status` (L1422-1466) expõe: estado do modelo + modelo **configurado** ao lado (`model_settings`, L1430), profundidade/tempo livre da fila de falas (`speech_queue_*`, L1439-1444), `api_auth_enabled`, `jobs_active`/`jobs_active_max`/`jobs_history_max` (L1449-1451), `vad_backend` real (L1455), `live_sessions`/`live_historicos`/teto, `rate_limit_buckets`/teto, `api_keys_count`, `lan_urls`.
- `/api/build` (L1390-1405) devolve `codigo` (sha256-8 **congelado no import**, L1379-1387) para diferenciar "commit novo" de "código carregado"; `/health` é liveness aberto.
- Erros: quase sempre `HTTPException` com mensagem acionável; `_stt_ok` devolve o **motivo** da rejeição (L5021-5041); falha do remoto no STT vira `remote_fallback` no corpo **e** headers `X-TTS-Remote-Fallback`/`X-TTS-Remote-Error` (L4970-4985) — sem confundir "vazio" com "provedor morto".
- Limpeza: `_auto_cleanup_once`/loop (L4346-4398) apaga saídas por idade, pula job ativo, e remove temporários >6 h mesmo com `auto_cleanup` desligado.
- Memória: `idle_unload` por motor com `_last_use` (L4495-4532), `free_local_on_remote` (L4535-4542), `_release_mlx_memory` agressivo, `torch.mps.empty_cache` (L4474-4483).
- Logs: `print(..., flush=True)` no stdout/stderr, e o logger `live` com formato próprio (WARNING com `LIVE_LOG=0`) — L5914-5922; `chat-dsh` escreve no **stderr** de propósito para não poluir o stdout lido por testes (L1863-1871).
- Fallbacks explícitos e logados: VAD ONNX → torch-jit (L4886-4892), ffmpeg do `imageio`, whisper remoto → local (L4924-4931), pipeline Live ausente → stub (L6328-6341, L6746-6759).

---

## 8. Dívidas, frágeis e bugs

**Não há marcadores `TODO`/`FIXME`/`XXX` no arquivo** (só comentários `#NNN` referenciando tasks). A dívida é implícita:

1. **Nenhum `lifespan`/graceful shutdown** — todo o ciclo de vida é efeito colateral de import (L121, L242-263, L828, L1979, L4401, L4495, L7513). Importar `app` já mata workers, apaga diretórios, abre threads e pode gerar chave.
2. **Monkeypatch global de `resource.setrlimit`** (L92-122) — afeta qualquer biblioteca do processo.
3. **Descompasso de unidades**: `_live_hist_tam` soma caracteres e compara com `_LIVE_RESUME_MAX_BYTES` = 4 MB (L6906-6921).
4. **Bug de telemetria**: `_live_cancela` emite `interrupted` com a chave `turn` (L6441, L6443) mas `_live_observa` lê `obj.get("turno")` (L5983) → log sempre `turno=None`.
5. `_expressão` de settings sem lock: rollback de `update_settings` restaura a RAM, mas efeitos colaterais já disparados (`_autofree_local`, prewarm dsh, descarga de modelo) **não** são desfeitos (L2640-2654, L2880-2885).
6. `_unload_local_models` pega `_gen_lock` (Lock simples, L4438) a partir de `/api/models/unload` (L5205) e do loop de ociosidade: com geração in-process longa o POST fica pendurado até o fim do job (o guarda de L2644 só existe em `update_settings`).
7. `_runtime` de chat: cada mensagem cria thread que fica **bloqueada** em `.result(timeout=150)` num executor de 8 (L2121-2122, L2183); `_chat_sessions` não tem teto, só TTL de 1 h limpo no `start` (L2069-2071).
8. Vários caches sem evicção: `_remote_voice_cache` (L4640), `_dsh_models_cache` (L1985), e `_live_tickets` só é podado na emissão (L6120-6136).
9. VAD e gate de locutor **falham abertos** (`return True`, L4912-4913; sem embedding em `label` segue, mas `enforce` rejeita — L5619-5621).
10. `_anomalo` e a heurística de retry misturam proxy de qualidade com seed — sem instrumentação de quantos retries salvam trechos (só contador `job["retries"]`, L4132).
11. `_sanitize_text` troca `/` por " ou " (common.py L146-148), o que soa estranho em "e/ou", "S/N".
12. `chat_debug` grava em `/tmp` fixo, sem rotação (L2214).
13. `_jobs` é volátil: restart perde todo o histórico e o cliente recebe 404 no meio do stream (só o dir em disco sobrevive, até a limpeza de boot L257-263).
14. Erros do worker anexam até 500 chars do log ao `error` do job (L3961-3985) — caminhos internos expostos na API.
15. `_downloader` do YouTube usa `extractor_args` heurísticos e depende de `yt-dlp` recente; falha vira 400 genérico (L5424-5540).
16. `_parse_time` aceita `HH:MM:SS` mas não valida ordem/limites além do teto de 600 s (L5331-5346, L5524-5529).
17. `delete_all_outputs` (L4336) e `delete_voice` (L3382) apagam sem `try` por arquivo — um `OSError` isolado derruba a resposta no meio, com remoção parcial.
18. `_settings` e `_apikeys` são reescritos por inteiro a cada mudança (`_save_settings` L406-439, `_save_apikeys` L554-573) — a mitigação de chaves desconhecidas (L409-434) cobre só settings, não apikeys/`.speaker-profiles.json`.
19. Ausência de `root_path` do FastAPI: o prefixo `/ttsproxy` é removido no middleware (L1038-1047) e o OpenAPI fica com os caminhos "crus".
20. O WS não negocia subprotocolo nem versão; evolução de protocolo depende de campos aditivos e de o cliente ignorar o que não conhece (L5902-5905).

---

### Resumo de uma linha
Servidor local bem instrumentado e cheio de correções cirúrgicas de concorrência/estado, com boa defesa em profundidade em paths e uploads — mas o eixo de autorização é binário (chave válida ≈ admin, loopback ≈ tudo), sem CSRF/Origin, com shutdown abrupto e um boot que age por import.
