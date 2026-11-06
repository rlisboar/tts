"""TTS-STUDIO — clonagem de voz local com gravação e gerenciamento de vozes.

Servidor FastAPI + backends TTS via MLX (Apple Silicon): OmniVoice, Qwen3-TTS,
Fish S2, Chatterbox, Kokoro, PocketTTS, VoxCPM2, Voxtral, etc.
Tudo local: nenhum áudio ou texto sai da máquina.
"""

import asyncio
import concurrent.futures
import faulthandler
import gc
import hashlib
import json
import logging
import os
import queue
import re
import secrets as _secrets
import signal
import shutil
import socket
import subprocess
import sys
import threading
import time

# Início do import do módulo: o `boot_ms` do `GET /api/build` (#190) é medido daqui —
# é o custo de subida do processo que o dono paga no `./run.sh`.
_BUILD_INI = time.monotonic()
_BUILD_TS = time.time()
import tempfile
import uuid
import wave
from collections import OrderedDict
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlsplit
from fastapi import (FastAPI, File, Form, HTTPException, Request, UploadFile,
                     WebSocket)
from fastapi import WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

# onnxruntime (caminho ONNX do VAD) tenta gravar o ID de telemetria em $HOME;
# com HOME não-gravável (sandbox/CI) ele larga um arquivo ":memory:.ses" no CWD.
# O run.sh já exporta isto para o servidor; aqui vale também para pytest, scripts
# e IDE, que carregam o VAD sem passar pelo run.sh. O app é local/offline: a
# telemetria não serve para nada e o warning suja a suíte. Fica ANTES dos
# imports locais porque o próprio onnxruntime lê a var ao ser importado.
os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")

# Piso de descritores no boot do servidor (antes de qualquer load): o dsh
# scandira ~/.dsh/sessions e some libs abrem fd em rajada; um teto duro
# herdado baixo (zsh/launchd já variou 256→2048→4096 — EMFILE real) mata o
# handshake. Subir SOFT até HARD não pede privilégio; o run.sh também tenta.
def _piso_nofile() -> None:
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        alvo = max(soft, min(hard, 65536))
        if alvo > soft:
            resource.setrlimit(resource.RLIMIT_NOFILE, (alvo, hard))
    except Exception:
        pass


def _nofile_nao_desce(atual: tuple[int, int], pedido: tuple[int, int]) -> tuple[int, int]:
    """Combina dois tetos de NOFILE sem nunca descer o que o processo já tem.

    mlx_lm/utils.py faz `setrlimit(2048, 4096)` no import, achando que o piso
    do macOS é 256. Se o boot já subiu (run.sh / `_piso_nofile`), esse call
    ABAIXA o hard — e sem privilégio o processo não recupera. O dsh depois
    morre com EMFILE. Subir continua valendo: num piso de 256, o (2048, 4096)
    do mlx ainda ajuda.
    """
    import resource
    inf = resource.RLIM_INFINITY
    cur_soft, cur_hard = atual
    req_soft, req_hard = pedido

    def _max_teto(a: int, b: int) -> int:
        if a == inf or b == inf:
            return inf
        return a if a > b else b

    new_hard = _max_teto(cur_hard, req_hard)
    new_soft = cur_soft if cur_soft > req_soft else req_soft
    if new_hard != inf and new_soft > new_hard:
        new_soft = new_hard
    return (new_soft, new_hard)


#: pedidos de NOFILE já registrados: a guarda era MUDA, e um monkeypatch global que
#: ninguém vê é pior de depurar (#282). Uma linha por valor distinto, em stderr.
_nofile_pedidos_vistos: set = set()


def _nofile_log(pedido, atual, aplicado) -> None:
    """Registra o pedido de setrlimit(NOFILE) UMA vez por valor (None = recusado)."""
    chave = tuple(pedido)
    if chave in _nofile_pedidos_vistos:
        return
    _nofile_pedidos_vistos.add(chave)
    acao = (f"aplicado {aplicado[0]}/{aplicado[1]}" if aplicado
            else f"recusado (mantido {atual[0]}/{atual[1]})")
    print(f"[nofile] setrlimit(NOFILE, {pedido[0]}/{pedido[1]}) {acao}",
          file=sys.stderr, flush=True)


def _guarda_setrlimit(orig, getrlimit):
    """`resource.setrlimit` que recusa descer RLIMIT_NOFILE. Os outros limites passam."""
    import resource

    def guarded(which, limits):
        if which != resource.RLIMIT_NOFILE:
            return orig(which, limits)
        try:
            atual = getrlimit(resource.RLIMIT_NOFILE)
            novo = _nofile_nao_desce(atual, (int(limits[0]), int(limits[1])))
            if novo == tuple(atual):
                _nofile_log(limits, atual, None)     # recusou descer: LOG
                return None
            _nofile_log(limits, atual, novo)
            return orig(which, novo)
        except Exception:
            return orig(which, limits)

    return guarded


def _instala_guarda_nofile() -> None:
    """Uma vez por processo, antes do import preguiçoso do mlx_lm."""
    import resource
    if getattr(resource.setrlimit, "_tts_guarda_nofile", False):
        return
    guarded = _guarda_setrlimit(resource.setrlimit, resource.getrlimit)
    guarded._tts_guarda_nofile = True  # type: ignore[attr-defined]
    resource.setrlimit = guarded


_piso_nofile()
_instala_guarda_nofile()


import dsh_client
from backends import (ALIASES as _CATALOGO_ALIASES, BACKENDS as _CATALOGO_BACKENDS,
                     generate_with_backend, list_backends, load_tts_model,
                     resolve_backend)

try:                                     # cache de áudio por texto (#325)
    import tts_cache as _tts_cache
except Exception:                        # noqa: BLE001 — sem o módulo, sem cache
    _tts_cache = None
from common import (GANHO_AVISO_DB, LUFS_ALVO, NATIVE_SPEED_FAMILIES, OMNI_ALIASES, PAD_CAUDA_S,
                    REGRA_FALA, RMS_AVISO, texto_para_fala,
                    resolve_omni_source, write_json_atomic,
                    apply_audio_fx,
                    atempo_chain as _atempo_chain,
                    biquad as _biquad,
                    fade_edges as _fade_edges,
                    normalize as _normalize,
                    pausa_apos as _pausa_apos,
                    pontua_corte as _pontua_corte,
                    motivo_anomalia as _motivo_anomalia,
                    normaliza_loudness as _normaliza_loudness,
                    release_mlx_memory as _release_mlx_memory,
                    sanitize_text as _sanitize_text,
                    split_text as _split_text,
                    time_stretch as _time_stretch,
                    trim_tail_silence as _trim_tail_silence,
                    write_wav_concat as _write_wav_concat)
import metrics_hist as _metrics

def _caminho_estado_env(nome: str, padrao: Path) -> Path:
    """Caminho de estado com override por ambiente (#461); vazio = o de sempre.

    Toda suíte/script que quer estado próprio montava launcher
    (`python -c "import app; app.X = …"`) ou monkeypatch e, quando esquecia,
    escrevia no arquivo REAL do dono (#292/#298/#435/#437). Com a variável de
    ambiente, isolar é uma linha de `env`. O default não muda sem ela.
    """
    bruto = (os.environ.get(nome) or "").strip()
    if not bruto:
        return padrao
    return Path(bruto).expanduser()


BASE = Path(__file__).resolve().parent


def _arbore_e_a_principal() -> bool:
    """A árvore é a RAIZ PRINCIPAL do repositório (não worktree, não cópia)?

    Só a raiz principal pode usar o estado real do dono: worktree/cópia/CI que
    caíssem no Application Support fariam todo harness escrever no dado real (#721).
    SÓ o `.git` DIRETÓRIO qualifica. Worktree (`.git` arquivo), cópia sem `.git`
    (as suítes fazem `git ls-files | tar` em /tmp e sobem servidor de lá) e CI caem
    em `.state-local` — qualquer uma delas escreveria no estado REAL do dono se o
    padrão fosse o diretório de aplicativo. Instalação de verdade fora de git, se um
    dia existir, passa `TTS_STATE_DIR` explícito."""
    return (BASE / ".git").is_dir()


def _dir_de_estado_padrao() -> Path:
    """Onde vive o estado do dono FORA do repositório (#721).

    Dentro da árvore, qualquer teste/worktree/script alcança o dado real — e a
    suíte já apagou o `settings.json` do dono 3 vezes. O diretório de aplicativo só
    vale na RAIZ PRINCIPAL; em worktree/cópia/CI o estado é local da árvore."""
    bruto = (os.environ.get("TTS_STATE_DIR") or "").strip()
    if bruto:
        return Path(bruto).expanduser()
    if not _arbore_e_a_principal():
        return BASE / ".state-local"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "tts-studio"
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "tts-studio"


#: diretório do estado do dono: settings, chaves e perfis (fase 1 do #721). Cada
#: `TTS_*_PATH` individual continua vencendo este diretório (#461).
STATE_DIR = _dir_de_estado_padrao()
VOICES_DIR = _caminho_estado_env("TTS_VOICES_DIR", BASE / "voices")
OUTPUTS_DIR = _caminho_estado_env("TTS_OUTPUTS_DIR", BASE / "outputs")
APIKEYS_PATH = _caminho_estado_env("TTS_APIKEYS_PATH", STATE_DIR / ".apikeys.json")
# embeddings de voz (fora do git)
SPEAKER_PATH = _caminho_estado_env("TTS_SPEAKER_PATH", STATE_DIR / ".speaker-profiles.json")
#: áudio ORIGINAL de cada amostra aceita (#658): permite reextrair os vetores numa
#: troca de extrator sem pedir regravação. Fica em `state/` (fora do git e de
#: qualquer export), 0600. Sem ele o recadastro exige gravar de novo.
SPEAKER_AUDIO_DIR = _caminho_estado_env("TTS_SPEAKER_AUDIO_DIR", BASE / "state" / "speaker-audio")
LEGACY_APIKEY_PATH = _caminho_estado_env("TTS_LEGACY_APIKEY_PATH", STATE_DIR / ".apikey")
VOICES_DIR.mkdir(parents=True, exist_ok=True)
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
for _p in (APIKEYS_PATH, SPEAKER_PATH, LEGACY_APIKEY_PATH):
    try:               # pai somente-leitura não pode derrubar o import (#721)
        _p.parent.mkdir(parents=True, exist_ok=True)   # `write_json_atomic` exige
    except OSError as _exc_pai:
        print(f"[estado] não criei o pai de {_p}: {_exc_pai}",
              file=sys.stderr, flush=True)


def _migra_estado_para_fora_do_repo(base: Path | None = None,
                                    destino: Path | None = None) -> None:
    """1ª partida com o estado ainda no repositório: COPIA para o `STATE_DIR` (#721).

    Nunca move nem apaga: os originais ficam na raiz até o dono decidir (e hoje
    estão com `uchg`, que só permite leitura). Destino que já existe não é tocado.
    Uma linha de log por arquivo copiado."""
    base = base or BASE
    destino = destino or STATE_DIR
    if destino == base:
        return                                   # já é a própria raiz: nada a fazer
    if destino == base / ".state-local":
        return                                   # worktree/cópia: estado local, não migra
    try:
        destino.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    for nome in ("settings.json", ".apikeys.json", ".apikey", ".speaker-profiles.json"):
        origem, alvo = base / nome, destino / nome
        if alvo.exists() or not origem.is_file():
            continue
        try:
            # `copyfile`, NÃO `copy2`: copy2 leva as flags do original e o
            # `settings.json` do dono está com `uchg` — o migrado nasceria
            # imutável e o app não conseguiria salvar (#721)
            shutil.copyfile(origem, alvo)
            alvo.chmod(0o600)
            print(f"[estado] copiado {nome} para {destino} (o original fica na raiz)",
                  file=sys.stderr, flush=True)
        except Exception as exc:  # noqa: BLE001 — migração é conveniência, não requisito
            print(f"[estado] não copiei {nome}: {exc}",
                  file=sys.stderr, flush=True)


try:                       # diretório somente-leitura não pode derrubar o import
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.chmod(0o700)
except OSError as _exc_estado:
    print(f"[estado] não preparei {STATE_DIR}: {_exc_estado}",
          file=sys.stderr, flush=True)
_migra_estado_para_fora_do_repo()

# ---------------------------------------------------------------------------
# Limpeza de boot dos diretórios de job (outputs/.job-*)
#
# Um `.job-*` é trabalho de ALGUMA instância do app — o servidor da máquina, um
# smoke, um `pytest` (a suíte importa app). A limpeza antiga apagava tudo no
# import, então um segundo processo derrubava a geração do servidor vivo
# (`System error` ao escrever o trecho e 404 no status) e ainda matava o worker
# filho dele. Agora só sai o órfão de verdade: dir com dono VIVO fica.
# ---------------------------------------------------------------------------

_JOB_ORFAO_JANELA_S = 300             # sem dono registrado: atividade recente segura


def _pid_cmd(pid) -> str:
    """Cmdline do pid ("" se o processo não existe ou o `ps` não respondeu).

    `ps` pode faltar (sandbox/CI nega o exec): por isso a vida do processo é
    medida com `os.kill(pid, 0)` e a cmdline é só refinamento opcional. E sem
    ela não dá para distinguir pid reciclado — daí o "não respondeu" nunca ser
    tratado como "morto"."""
    try:
        return subprocess.run(["ps", "-p", str(int(pid)), "-o", "command="],
                              capture_output=True, text=True, timeout=2).stdout.strip()
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""


# Segredo em argumento de linha de comando: `--token X`, `--token=X`, `Bearer X`,
# `?key=X`. A cmdline do pai é IMPRESSA no log de sinal, e esse log pode ir para
# arquivo em /tmp (0644) — sem máscara, um `cloudflared --token …` deixa o token
# em claro ali (#423, item 4 do gate #440).
_RE_ARG_SEGREDO = re.compile(
    r"(?i)((?:--?|/)(?:token|key|apikey|api[_-]key|secret|password|senha)\b\s*=?\s*)(\S+)")
_RE_BEARER = re.compile(r"(?i)(bearer\s+)(\S+)")
_RE_QS_SEGREDO = re.compile(r"(?i)([?&](?:key|token|apikey|secret)=)([^&\s]+)")


def _mascara_segredo(v: str) -> str:
    """`***` + os 4 últimos: dá para comparar dois logs sem expor o valor."""
    return "***" + v[-4:] if len(v) > 8 else "***"


def _mascara_cmd(cmd: str) -> str:
    """Cmdline com os valores sensíveis mascarados (para LOG, não para decidir)."""
    if not cmd:
        return cmd
    cmd = _RE_ARG_SEGREDO.sub(lambda m: m.group(1) + _mascara_segredo(m.group(2)), cmd)
    cmd = _RE_BEARER.sub(lambda m: m.group(1) + _mascara_segredo(m.group(2)), cmd)
    return _RE_QS_SEGREDO.sub(lambda m: m.group(1) + _mascara_segredo(m.group(2)), cmd)


def _pid_vivo(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                       # existe, mas é de outro usuário
    except OSError:
        return False


def _job_owner_pid(job_dir: Path):
    """Pid do processo que criou o job (None em dir de versão anterior)."""
    try:
        return int((job_dir / "owner.pid").read_text().strip())
    except (OSError, ValueError):
        return None


def _job_owner_vivo(job_dir: Path) -> bool:
    """True se o dono do job ainda é um processo python vivo desta app.

    Com a cmdline disponível exige "python": sem isso a reciclagem do pid por
    outro programa deixaria um dir velho preso para sempre. Sem `ps`, vale a
    vida do processo (o `owner.pid` é escrito por nós)."""
    pid = _job_owner_pid(job_dir)
    if not _pid_vivo(pid):
        return False
    cmd = _pid_cmd(pid)
    return "python" in cmd.lower() if cmd else True


def _job_dir_ativo(job_dir: Path) -> bool:
    """Dir de job que outra instância pode estar usando AGORA.

    `owner.pid` presente decide sozinho: dono vivo = fica, dono morto = órfão de
    crash e sai na hora (é o que queremos depois de um restart, em vez de deixar
    lixo 5 min). Só o dir SEM dono (versão anterior do app) cai na atividade
    recente — o dono reescreve status.json e cria trechos enquanto gera.
    `owner.pid` fica FORA dos mtimes: escrito uma vez no começo, ele mentiria
    "recente" num dir que já parou (o dir em si já conta a criação)."""
    if _job_owner_pid(job_dir) is not None:
        return _job_owner_vivo(job_dir)
    mtimes = []
    for alvo in (job_dir, job_dir / "status.json", job_dir / "0.wav"):
        try:
            mtimes.append(alvo.stat().st_mtime)
        except OSError:
            pass
    return bool(mtimes) and (time.time() - max(mtimes)) < _JOB_ORFAO_JANELA_S


# Workers filhos têm sessão própria e sobrevivem à morte do pai: recolhe só os
# que ficaram órfãos. A decisão é a MESMA do dir (`_job_dir_ativo`): dir vivo
# mantém o worker, senão o par ficava incoerente (dir preservado, worker morto,
# e quem pollava o worker era a instância antiga). Dir novo = dono vivo decide;
# dir de versão anterior cai na atividade recente.
for _pid_file in OUTPUTS_DIR.glob(".job-*/worker.pid"):
    try:
        _pid = int(_pid_file.read_text().strip())
        _cmd = _pid_cmd(_pid)
        if _cmd and "tts_worker.py" not in _cmd:
            continue                      # pid morto ou reciclado por outro
        if not _cmd and not _pid_vivo(_pid):
            continue                      # sem ps: só segue se o processo existe
        if _job_dir_ativo(_pid_file.parent):
            continue                      # instância viva ainda polla esse worker
        os.killpg(_pid, signal.SIGTERM)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass

# trechos parciais de job interrompido não sobrevivem a restart
for _d in OUTPUTS_DIR.glob(".job-*"):
    if _job_dir_ativo(_d):
        continue
    if _d.is_dir():
        shutil.rmtree(_d, ignore_errors=True)
    else:
        _d.unlink(missing_ok=True)        # arquivo solto: rmtree não o alcança

# OmniVoice: as conversões MLX publicadas vêm quebradas — app e worker usam o
# dir montado por common.assemble_omnivoice_path (backbone bf16 + audio_tokenizer
# COMPLETO em .omnivoice-bf16/). Outros backends usam o repo MLX direto (backends.py).
# atalho de backends.py (omnivoice, qwen3-0.6b, fish-s2…) ou id/dir MLX livre
MODEL_ID = os.environ.get("TTS_ROD_MODEL", "omnivoice")
# voz "virtual": gera só a partir da descrição textual (instruct), sem ref de clone
DESIGN_VOICE_ID = "__design__"

# ---------------------------------------------------------------------------
# Configurações padrão editáveis no dashboard (persistem em settings.json e
# valem para UI e API; parâmetro explícito na requisição sempre sobrepõe)
# ---------------------------------------------------------------------------
SETTINGS_PATH = _caminho_estado_env("TTS_SETTINGS_PATH", STATE_DIR / "settings.json")
try:                   # idem: o pai do settings é o diretório de estado
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
except OSError as _exc_set:
    print(f"[estado] não criei o pai de {SETTINGS_PATH}: {_exc_set}",
          file=sys.stderr, flush=True)
_SETTINGS_DEFAULTS = {
    "model": MODEL_ID,         # atalho (omnivoice, qwen3-0.6b…) ou id/dir MLX
    "pre_prompt": "",          # texto falado antes de toda geração
    "language": "auto",        # "auto" = OmniVoice detecta o idioma do texto (recomendado)
    "default_voice": None,     # id; None = voz mais recente
    "chunk_max_chars": 140,
    "speed": 1.0,              # velocidade da fala (UI + API); nativa do modelo (preserva o tom)
    "auto_cleanup": False,     # apaga áudios gerados automaticamente
    # #501/#502 — interrupção do Live: `inteligente` (decide pelo conteúdo),
    # `imediata` (o comportamento antigo, corta no limiar) ou `so_comando`.
    "live_interrupcao": "inteligente",
    # cadeia de filtros do barge (merge PARCIAL por filtro: mandar um item não
    # apaga os outros). Semeada COMPLETA para a tela e a API lerem a mesma forma;
    # `modelo` começa desligado e local até a medição do qa (#509).
    "live_barge_filtros": {
        "palavras_corte": {"on": True, "palavras": []},
        "eco": {"on": True},
        # identidade (#527): só voz cadastrada corta; TV/outra pessoa continua.
        # DESLIGADO por default — custa um passo do extrator por suspeita.
        # `far` escolhe a faixa do limiar (0.01 = FAR 1 %, o calibrado).
        "identidade": {"on": False, "perfis": [], "far": 0.01},
        "backchannel": {"on": True, "palavras": []},
        "ruido": {"on": True},
        "modelo": {"on": False, "provedor": "local", "limiar": 0.5, "timeout_ms": 400},
        "pergunta": {"on": True},
        "frase_longa": {"on": True, "min_palavras": 3},
        "fala_sustentada": {"on": True, "min_ms": 1200},
        # 2000 e não 1000: o backlog do motor corre ~1,1-1,3 s À FRENTE do
        # cliente (medido: fila real 0 ms com o motor dizendo 1300 ms). Com
        # 1000 a regra só disparava DEPOIS de o playback acabar, quando a
        # janela de barge já tinha fechado — e o último segundo nunca
        # anotava (#547). 2000 é o instante em que ainda há ~0,7 s de áudio
        # de verdade no alto-falante.
        "fim_de_resposta": {"on": True, "restante_ms": 2000},
    },
    # o que fazer quando a janela decide sem transcript útil
    "live_barge_sem_decisao": "continua",

    "auto_cleanup_minutes": 15,
    # teto de QUANTIDADE de áudios em outputs/ (0 = sem teto). Vale mesmo com
    # `auto_cleanup` desligado: é proteção do diretório, não preferência (#430)
    "auto_cleanup_max_outputs": 500,
    # OmniVoice — controles de geração (defaults = os da lib)
    "omni_num_steps": 16,             # passos de unmasking (4–64); 16 rápido, 32 qualidade
    # #315: passos do 2º chunk do LIVE (o 1º tem teto próprio, 12). Do 3º em diante
    # vale o `omni_num_steps`. Menor = menos engasgo no começo da resposta.
    "live_num_steps": 16,
    "omni_guidance_scale": 2.0,       # força do CFG (0–10): + = mais aderente ao texto/voz
    "omni_class_temperature": 0.0,    # temp. de amostragem de token (0 = greedy/estável)
    "omni_position_temperature": 5.0, # temp. da escolha de posição a revelar (0–20)
    "omni_layer_penalty_factor": 5.0, # penalidade por camada de codebook (0–20)
    "omni_t_shift": 0.1,              # deslocamento do cronograma de difusão (0–1)
    "omni_denoise": True,             # limpa ruído do áudio gerado (config do modelo)
    "omni_preprocess_prompt": True,   # pré-processa o prompt/texto antes de gerar
    "omni_postprocess_output": True,  # pós-processa o áudio de saída
    "omni_audio_chunk_duration": 15.0,   # chunking interno de texto longo: duração (s)
    "omni_audio_chunk_threshold": 30.0,  # chunking interno: limiar p/ dividir (s)
    "omni_instruct": "",              # voice design textual (ex.: "female, low pitch")
    "omni_seed": 42,                  # seed da geração: voz reprodutível (mesmo instruct=mesma voz). -1 = aleatório
    "omni_duration_s": None,          # força duração fixa em s (None = automático)
    "omni_ref_max_s": 10.0,           # quanto da amostra de referência usar (3–30 s)
    "omni_precision": "bf16",         # fp32 (repo F32) | bf16 (montado) | q8 | q4 (quantiza só o backbone)
    # Controles genéricos multi-backend (mapeados em _resolve_omni / generate_with_backend)
    "gen_temperature": 0.8,           # sampling AR (Qwen/Fish/Chatterbox/Pocket/Voxtral)
    "gen_top_p": 0.95,
    "gen_top_k": 50,
    "gen_repetition_penalty": 1.1,
    "gen_max_tokens": 2048,
    "gen_exaggeration": 0.5,          # Chatterbox expressividade
    "gen_cfg_weight": 0.5,            # Chatterbox CFG
    "gen_min_p": 0.05,                # Chatterbox min-p
    "gen_chunk_length": 300,          # Fish S2
    "gen_speaker": "Ryan",            # Qwen3 CustomVoice
    "gen_kokoro_voice": "af_heart",
    "gen_pocket_voice": "alba",
    "gen_voxtral_voice": "casual_male",
    "gen_lfm3_voice": "female",
    "voice_denoise": True,            # limpa ruído de fundo da amostra ao salvar a voz
    "voice_denoise_strength": 0.7,    # agressividade do spectral gating (0–1)
    "voice_limpar_motor": "dfn3",     # "dfn3" (ONNX) ou "gating"; cai no gating sozinho
    "voice_limpar_musica": False,     # separa música antes de limpar (Demucs, se houver)
    "voice_limpar_dereverb": False,   # dereverb opcional (se houver)
    "voice_timbre_min": 0.80,         # similaridade mínima antes/depois; abaixo recusa
    # Áudio de saída (pós-geração): ganho + EQ 3 bandas (dB)
    "audio_gain_db": 0.0,             # ganho geral (-15..+15)
    # cache de áudio por texto/voz/parâmetros (#325): OFF por padrão
    "tts_cache": False,
    "tts_cache_ttl_s": 3600,
    "tts_cache_max": 500,
    "audio_eq_low_db": 0.0,           # grave (shelf 150 Hz)
    "audio_eq_mid_db": 0.0,           # médio (peak 1.5 kHz)
    "audio_eq_high_db": 0.0,          # agudo (shelf 5 kHz)
    # Tradutor de voz — filtros anti-ruído da transcrição (rejeita alucinação do Whisper)
    "stt_min_words": 1,               # mínimo de palavras p/ aceitar (ignora ruído)
    "stt_min_chars": 2,               # mínimo de caracteres
    "stt_max_no_speech": 0.6,         # rejeita se prob. de "sem fala" acima disto (0–1)
    "stt_min_logprob": -1.0,          # rejeita se confiança média abaixo disto (-5–0)
    "stt_max_compression": 2.4,       # rejeita se repetitivo demais (alucinação) (1–10)
    "stt_anti_ruido": True,           # desliga todos os filtros acima (só VAD e tamanho)
    "stt_denoise": True,              # denoise ffmpeg (afftdn) antes de cada transcrição
    "stt_local_engine": "whisper",    # STT local no Mac: whisper | parakeet
    "stt_whisper_repo": "",           # repo HF do Whisper local; vazio = large-v3-turbo
    # #876: Whisper do LIVE (turno + decisão de barge), separado do de arquivo. Vazio =
    # turbo: no Live a latência manda e o large-v3 custa 1,5–2,4× sem ganho medido de
    # WER (evidence/876-stt-live.md). O `stt_whisper_repo` do dono segue no ditado/arquivo.
    "live_stt_whisper_repo": "",
    "stt_beam": 5,                    # beam do STT remoto: 1=rápido, 5=padrão, 8=qualidade
    # Histórico de operação (#376): TTL em dias do JSONL do /api/metrics e teto de
    # tamanho por arquivo. Política de RECURSO (não é admin): quem gera fala pode
    # ajustar sem repontar conexão. Ver `metrics_hist.py`.
    "metrics_ttl_dias": 14,
    "metrics_teto_mb": 8,
    # Modelos remotos (API OpenAI-compatível). base_url deve terminar em /v1
    # (ex.: http://rtx-host:8000/v1). api_key opcional. Tudo local por padrão.
    # base_url + api_key ficam locais (settings.json é gitignored). Vazio = local.
    "remote_tts": False,              # síntese (OmniVoice) numa máquina remota (ex.: RTX)
    "remote_tts_url": "",             # URL completa do endpoint, ex.: http://rtx-host:8800/tts
    "remote_tts_voice": "",           # nome/preset da voz no servidor remoto (vai como `voice`)
    "remote_tts_extra": "",           # JSON com params extras do servidor (speed, num_steps…)
    "remote_tts_model": "tts-1",      # (compat OpenAI) nome do modelo, se o servidor usar
    "remote_translate": False,        # tradução (LLM) via API remota
    "remote_stt": False,              # transcrição (STT) via API remota
    "remote_base_url": "",            # ex.: http://rtx-host:8000/v1
    "remote_api_key": "",             # chave do provedor (guardada localmente; opcional)
    "remote_translate_model": "gpt-4o-mini",
    "remote_stt_model": "whisper-1",
    # STT pode apontar p/ uma API DEDICADA (proxy externo) sem mexer no translate/TTS.
    # vazio = usa remote_base_url/remote_api_key compartilhados.
    "remote_stt_base_url": "",        # ex.: https://api.openai.com/v1 (OpenAI-compatível)
    "remote_stt_key": "",             # chave dessa API de STT (vazio = usa remote_api_key)
    # Conversa (decide o texto por chat com IA). Provedor OpenAI-compatível;
    # vazio = herda remote_base_url/remote_api_key/remote_translate_model.
    "chat_base_url": "",              # ex.: https://api.openai.com/v1
    "chat_model": "",                 # ex.: gpt-4o-mini
    "chat_api_key": "",               # vazio = usa remote_api_key
    "chat_system": "",                # instruções da IA (preprompt); vazio = prompt padrão
    "chat_extra": "",                 # JSON com params extras do LLM (reasoning_effort, top_p…)
    # Backend de IA alternativo: o harness `dsh` via ACP (ver dsh_client.py). "openai"
    # (default) = endpoint+chave acima, comportamento INTACTO; "dsh" = processo local
    # com o perfil sem tools. O default do modelo é a rota COM chave deste host (a
    # rota default do catálogo, `deepseek-official`, falha -32603 sem credencial).
    "chat_backend": "openai",
    # Backend SÓ do Live (#176): a recomendação de rota difere entre as duas telas
    # (no Live o dsh dá 1º token ~0,4 s contra 4–11 s do provedor remoto; na
    # Conversa o provedor atual serve). VAZIO = herda `chat_backend`.
    "chat_backend_live": "",
    "chat_dsh_bin": "dsh",            # `dsh` resolvido no PATH (ou caminho absoluto)
    "chat_dsh_profile": dsh_client.DSH_DEFAULT_PROFILE,
    "chat_dsh_model": dsh_client.DSH_DEFAULT_MODEL,   # par opaco JSON ["rota","modelo"]
    "chat_dsh_effort": "off",         # off|low|high|max (off = orçamento do Live)
    # Verificação de locutor (biometria de voz): off | enforce (só vozes
    # cadastradas) | label (transcreve todos e etiqueta quem falou)
    "speaker_gate": "off",
    "speaker_threshold": 0.6,         # rigor CRU (escore cosseno 0.35–0.9)
    # alvo de FAR em %: "1" ou "0,1"; vazio = usa `speaker_threshold` cru. Com alvo
    # o escore é normalizado (AS-norm) e o limiar vem da calibração do R2 (#525)
    # O seletor na tela entra junto do #528; até lá vale por `POST /api/settings`.
    "speaker_far_alvo": "",
    # Guardar o áudio de cada amostra aceita (#658, decisão do dono): é o que
    # permite reextrair sozinho numa troca de extrator. Não sai da máquina.
    "speaker_guardar_audio": True,
    # Anti-clone (#525): GANCHO do gate. Ligado, `_speaker_gate_ok` consulta a
    # MARCA d'água do áudio e rejeita clone em `enforce`; desligado, nem o campo sai.
    "speaker_anticlone": False,
    # Marca d'água AudioSeal em todo áudio GERADO (#657). O app clona voz, então
    # sem marca a saída é indistinguível de fala humana; é admin porque muda o áudio
    # de todo mundo. LIGADA no default: o ABX do dono passou (só o par 7 audível de
    # 8). Sem os pesos/`audioseal`, sai sem marca e o log diz.
    "tts_marca_dagua": True,
    # 1º PCM do Live também marcado (dono, 2026-10-28: marca em TODA saída).
    # False é o atalho do #901: só esse PCM sai sem AudioSeal. Quem desliga
    # leva a troca ao dono — o default fica no comportamento de hoje.
    "live_marca_primeiro_trecho": True,
    # "Só a minha voz" no Live (#533): portão ECAPA no TURNO. Desligado, nada
    # muda. Ligado, terceiro/TV não corta a resposta nem vira turno (o filtro
    # `identidade` do I4 reusa a MESMA verificação). `perfil` é o nome do perfil
    # de voz do dono; `rigor_far` ("joelho"/"1"/"0,1") troca o limiar —
    # vazio usa 0,44, o limiar do R4 (`evidence/532`; 0,35 passa voz parecida).
    # `separacao` é OPCIONAL e desligada por default: custa ~1 s e 1,16 GB.
    "live_so_minha_voz": {"on": False, "perfil": "", "rigor_far": "1.8",
                          "limiar": 0.0, "separacao": False},
    "translate_model": "",            # repo MLX do tradutor LOCAL; vazio = padrão (TRANSLATE_REPO)
    "free_local_on_remote": False,    # descarrega o modelo LOCAL correspondente quando o remoto está ativo
    # Memória: descarrega TTS/STT/tradutor/SER após N minutos sem uso (0 = nunca)
    "idle_unload_minutes": 10,
    # Whisper de ARQUIVO (large-v3) sai sozinho; o turbo do Live não entra neste
    # relógio (#891). Segundos: o de arquivo é pesado e o Live não precisa dele.
    # 0 = fica residente.
    "whisper_arquivo_ttl_s": 600,
    # Fila de falas: vários sistemas pedem TTS ao mesmo tempo sem sobrepor o áudio.
    # A espera principal = duração real do último áudio entregue; gap é só folga
    # extra (silêncio) entre uma fala e a próxima.
    "speech_queue": True,             # ligado por padrão (vários clientes na rede)
    "speech_queue_gap_s": 0.35,       # folga extra após a duração da fala (0–5 s)
}
class _StreamSeguro:
    """`sys.stdout`/`sys.stderr` que engolem stdout morto (#770).

    O `print` levanta `BrokenPipeError` quando o fd aponta para um pipe sem leitor
    (`run.sh` órfão depois de um hang do runner). No job isso já é tratado pelo
    `_log`; aqui a proteção vale para TODO print — Live, STT, boot e o que vier
    depois, sem ter de lembrar de usar o helper. Delega o resto ao stream real."""

    def __init__(self, base):
        self._base = base

    def write(self, texto):
        try:
            return self._base.write(texto)
        except (BrokenPipeError, OSError, ValueError):
            return len(texto)

    def flush(self):
        try:
            self._base.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass

    def __getattr__(self, nome):        # `buffer`, `isatty`, `fileno`…
        return getattr(self._base, nome)


def _protege_streams() -> None:
    """Envolve stdout/stderr uma vez só (idempotente)."""
    for nome in ("stdout", "stderr"):
        atual = getattr(sys, nome)
        if not isinstance(atual, _StreamSeguro):
            setattr(sys, nome, _StreamSeguro(atual))


_protege_streams()     # stdout morto não pode derrubar boot/Live/STT (#770)

_settings = dict(_SETTINGS_DEFAULTS)
if SETTINGS_PATH.exists():
    try:
        salvo = json.loads(SETTINGS_PATH.read_text())
        _settings.update({k: salvo[k] for k in _SETTINGS_DEFAULTS if k in salvo})
    except Exception as exc:  # noqa: BLE001
        # corrompido (crash na escrita): preserva p/ recuperação, segue com defaults
        try:
            SETTINGS_PATH.replace(SETTINGS_PATH.with_name("settings.json.corrupt"))
        except OSError:
            pass
        print(f"⚠ settings.json corrompido — backup em settings.json.corrupt; "
              f"usando defaults: {exc}", flush=True)


_settings_desconhecidas_avisadas: set = set()


def _save_settings():
    """Persiste settings.json com TODAS as chaves conhecidas (defaults + overrides).

    Chaves no DISCO que esta build não conhece são PRESERVADAS (#197): o arquivo é
    reescrito por inteiro a partir da RAM, e uma instância velha de pé (`run.sh` sem
    `--reload`) apagava o que uma build nova tinha gravado — o dono perdia a escolha
    feita na tela nova e o `admin_ignored` não acusava nada. Avisa UMA vez por chave
    desconhecida (é sinal de instância desatualizada, par do `GET /api/build`)."""
    # garante booleans/números estáveis (JSON true/false, não null)
    _settings["speech_queue"] = bool(_settings.get("speech_queue"))
    try:
        _settings["speech_queue_gap_s"] = float(_settings.get("speech_queue_gap_s") or 0.35)
    except (TypeError, ValueError):
        _settings["speech_queue_gap_s"] = 0.35
    payload = {k: _settings.get(k, v) for k, v in _SETTINGS_DEFAULTS.items()}
    try:
        disco = json.loads(SETTINGS_PATH.read_text())
    except Exception:  # noqa: BLE001 — sem arquivo/ilegível: o payload conhecido basta
        disco = {}
    if isinstance(disco, dict):
        extras = {k: v for k, v in disco.items() if k not in _SETTINGS_DEFAULTS}
        if extras:
            payload.update(extras)       # as nossas têm prioridade; as de fora sobrevivem
            novas = set(extras) - _settings_desconhecidas_avisadas
            if novas:
                _settings_desconhecidas_avisadas.update(novas)
                print(f"[settings] preservada(s) chave(s) que esta build não conhece: "
                      f"{', '.join(sorted(novas))} — instância desatualizada?",
                      file=sys.stderr, flush=True)
    write_json_atomic(SETTINGS_PATH, payload)
    try:
        os.chmod(SETTINGS_PATH, 0o600)
    except OSError:
        pass


# O IMPORT NÃO GRAVA o `settings.json` (#535). Antes, chave default ausente no disco
# disparava `_save_settings()` aqui: todo processo que só importava o app (suíte,
# driver, `python -c`, e todo launcher ANTES de reapontar `TTS_SETTINGS_PATH`)
# reescrevia o arquivo do dono inteiro, no formato canônico e com a ordem trocada —
# era a escrita sem autor que a guarda vinha acusando.
#
# O valor não se perde: o default já está na RAM (`_SETTINGS_DEFAULTS`, mesclado
# acima) e a PRIMEIRA gravação explícita (POST /api/settings, restore, migração)
# persiste o arquivo inteiro, já com as chaves novas. Quem quiser o arquivo
# materializado sem mexer em nada usa `POST /api/settings` vazio.
# Única exceção: arquivo CORROMPIDO segue sendo renomeado para
# `settings.json.corrupt` no import (bloco logo acima) — preservar a evidência
# antes de qualquer escrita é o ponto, e nesse caso o arquivo já está inválido.
# Textos maiores são gerados em trechos. OmniVoice é masked-diffusion não-AR (sem o
# problema de EOS do backend antigo), mas dividir permite tocar trecho-a-trecho — a
# fala começa após o 1º trecho, não no fim.
CHUNK_MAX_CHARS = 140

# OmniVoice (masked-diffusion não-AR): passos de unmasking iterativo. 16 = rápido
# (RTF ~0,8 no M3 com ref cacheada), 32 = qualidade (default da lib).
OMNI_STEPS_FAST = 16
OMNI_STEPS_HQ = 32
OMNI_REF_MAX_S = 10.0  # ref >20s é cortada no maior silêncio até este teto

# Vozes padrão do modelo: criadas por "voice design" (descrição `instruct`, sem
# gravação). Na 1ª utilização geramos uma amostra-semente e a salvamos como uma
# voz normal (.wav) — isso ANCORA o timbre para ficar consistente entre trechos.
OMNI_PRESET_SEED = ("Olá, esta é a minha voz. Vou narrar o seu texto com clareza, "
                    "ritmo natural e boa dicção, do começo ao fim.")
OMNI_PRESETS = {
    # instruct usa SÓ o vocabulário fechado do OmniVoice (gender/age/pitch/accent/whisper)
    "vd-narrador": {"name": "Narrador (masc., grave)",     "instruct": "male, adult, low pitch"},
    "vd-locutora": {"name": "Locutora (fem., suave)",      "instruct": "female, moderate pitch"},
    "vd-jovem-m":  {"name": "Jovem (masc., animado)",      "instruct": "male, young adult, high pitch"},
    "vd-jovem-f":  {"name": "Jovem (fem., animada)",       "instruct": "female, young adult, high pitch"},
    "vd-formal":   {"name": "Formal (masc., autoritário)", "instruct": "male, middle-aged, moderate pitch"},
    "vd-podcast":  {"name": "Podcast (fem., conversa)",    "instruct": "female, young adult, moderate pitch"},
}

# Idioma: o token é injetado cru no MLX (<|lang_start|>{x}<|lang_end|>) — o porte MLX
# NÃO faz o mapeamento nome->código que o upstream faz. Canônico = OmniVoice ID
# (código, ex.: "pt"); "None" = auto-detecção pelo texto (modo recomendado upstream).
_OMNI_LANG_NOMES = {
    "português": "pt", "portugues": "pt", "portuguese": "pt",
    "inglês": "en", "ingles": "en", "english": "en",
    "espanhol": "es", "español": "es", "spanish": "es",
    "francês": "fr", "frances": "fr", "french": "fr",
    "alemão": "de", "alemao": "de", "german": "de",
    "italiano": "it", "italian": "it",
}


def _omni_language(lang) -> str:
    """Resolve o valor de `language` aceito pelo OmniVoice no caminho MLX.

    vazio/"auto"/"none" -> "None" (auto-detecção pelo texto). Nome de idioma ->
    código canônico (OmniVoice ID). Caso contrário, assume que já é um código.
    """
    l = str(lang or "").strip().lower()
    if l in ("", "auto", "none", "null"):
        return "None"
    return _OMNI_LANG_NOMES.get(l, l)


# Tradutor de voz (PoC): STT (mlx-whisper) + tradução (mlx-lm) -> TTS na voz clonada.
WHISPER_REPO = os.environ.get("TTS_ROD_WHISPER", "mlx-community/whisper-large-v3-turbo")
# STT alternativo local: NVIDIA Parakeet TDT 0.6B v3 (multilíngue, ~30x tempo real)
PARAKEET_REPO = os.environ.get("TTS_ROD_PARAKEET", "mlx-community/parakeet-tdt-0.6b-v3")
TRANSLATE_REPO = os.environ.get("TTS_ROD_TRANSLATE", "mlx-community/Qwen2.5-3B-Instruct-4bit")
# código -> nome em inglês (para o prompt de tradução e o lang do OmniVoice)
LANG_DISPLAY = {
    "pt": "Portuguese", "en": "English", "es": "Spanish", "fr": "French",
    "de": "German", "it": "Italian", "ja": "Japanese", "zh": "Chinese",
    "ru": "Russian", "ko": "Korean", "ar": "Arabic", "nl": "Dutch",
}

# ---------------------------------------------------------------------------
# Chaves de API (multi): protegem /api/* e /v1/* na rede. Loopback e o próprio
# Mac (IP da LAN como origem) não exigem chave. Aceita Authorization: Bearer,
# Authorization: Bearer ou X-API-Key. Persistidas em
# .apikeys.json; migra de .apikey / TTS_ROD_API_KEY. Gestão na UI (Acesso).
# ---------------------------------------------------------------------------
#: Piso de força para chave vinda do AMBIENTE (#785): abaixo disto a chave não
#: autentica. O `run.sh` exportava o `.apikey` da raiz (3 bytes, arquivo que desde o
#: #721 nem é mais atualizado) e a instância aceitava como ADMIN uma chave quebrável
#: por força bruta na LAN.
_CHAVE_MIN_CHARS = 32


#: Alguma chave de ambiente é curta? Ela CONTINUA valendo (senão a LAN cai para
#: aberta se a instância depender só dela), mas deixa de dar admin (#785).
_ENV_CHAVE_FRACA = False


def _chave_de_ambiente(nome: str, valor: str) -> str:
    """Chave de ambiente: aceita, mas curta demais avisa e não vale como admin.

    Descartá-la fecharia o furo da chave fraca abrindo a rede — `_auth_enabled()`
    ficaria falso e a LAN passaria sem credencial. Fraca vira `use`."""
    global _ENV_CHAVE_FRACA
    if not valor or len(valor) >= _CHAVE_MIN_CHARS:
        return valor
    _ENV_CHAVE_FRACA = True
    print(f"[auth] {nome} tem {len(valor)} caractere(s) (< {_CHAVE_MIN_CHARS}): "
          f"autentica, mas NÃO vale como admin — rotacione (UI → Acesso)",
          file=sys.stderr, flush=True)
    return valor


_ENV_API_KEY = _chave_de_ambiente("TTS_ROD_API_KEY",
                                  (os.environ.get("TTS_ROD_API_KEY") or "").strip())
_ADMIN_API_KEY = _chave_de_ambiente("TTS_ROD_ADMIN_KEY",
                                    (os.environ.get("TTS_ROD_ADMIN_KEY") or "").strip())
# RLock (#419, achado no gate do shutdown): o handler de ROTATE segura este lock e
# chama `_primary_api_key()`, que o toma de novo — com `Lock` comum era
# AUTO-DEADLOCK determinístico (o request ficava pendurado e nada mais entrava).
# A reentrância é o que o código sempre pressupôs; a exclusão entre threads segue.
_apikeys_lock = threading.RLock()
_apikeys: dict = {"enabled": True, "keys": []}  # keys: id, name, secret, created_at


def _new_api_secret() -> str:
    return _secrets.token_hex(24)


def _mask_secret(secret: str) -> str:
    s = secret or ""
    if len(s) <= 10:
        return "••••••••"
    return f"{s[:4]}…{s[-4:]}"


def _sync_legacy_apikey_file():
    """Mantém .apikey = 1ª chave gerenciada (compat run.sh / scripts)."""
    # sem chaves gerenciadas o .apikey NÃO é apagado: run.sh e a migração de
    # _load_apikeys() ainda o usam como bootstrap da chave.
    try:
        keys = _apikeys.get("keys") or []
        if keys:
            novo = keys[0]["secret"] + "\n"
            # Grava só quando MUDA: `_load_apikeys()` roda no import deste módulo
            # (l.~1071), então todo processo que importava o app reescrevia o
            # `.apikey` — mtime novo com conteúdo velho, e o arquivo do dono
            # aparecia "mexido" a cada rodada de suíte (#535).
            try:
                # `errors="replace"`: `.apikey` editado à mão em latin-1 dava
                # UnicodeDecodeError, que NÃO é OSError — caía no `except` externo e o
                # reparo do espelho era pulado em silêncio (antes era sempre reescrito)
                atual = LEGACY_APIKEY_PATH.read_text(errors="replace")
            except OSError:
                atual = None
            if atual != novo:
                LEGACY_APIKEY_PATH.write_text(novo)
            try:
                os.chmod(LEGACY_APIKEY_PATH, 0o600)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass


def _role_do_disco(v) -> str | None:
    """`role` lido do arquivo: tolerante, e o legado é `None` de propósito.

    Ausente/vazio → None; inválido → None com aviso, e NÃO 400: um arquivo editado
    à mão não pode derrubar o boot (o import de `app` faz a leitura). O default da
    linha sem campo é a política ANTIGA (`_admin_is_allowed` trata None como "não é
    chave gerenciada com papel" e cai na regra legada, que administra quando não há
    `TTS_ROD_ADMIN_KEY`) — é o que uma chave criada antes de o campo existir esperava,
    e migração nenhuma muda o papel de chave existente por conta própria. Quem quer a
    separação põe `role` explícito (POST/PATCH gravam)."""
    if v is None:
        return None
    s = str(v).strip().lower()
    if not s:
        return None
    if s in ("admin", "use"):
        return s
    print(f"[apikeys] role {s!r} desconhecido no arquivo — tratando como legado (None), "
          f"a chave segue a política antiga", file=sys.stderr, flush=True)
    return None


#: Snapshot IMUTÁVEL das chaves (#799): `_exige_chave` roda no event loop e pegava
#: o `_apikeys_lock`, que rotas do threadpool seguram gravando disco. Quem escreve
#: chama `_apikeys_publica()`; a leitura vira atribuição, sem lock.
_apikeys_vivo: tuple = (True, ())


def _apikeys_publica() -> None:
    """Publica o estado atual para leitura sem lock (chamar após toda mutação)."""
    global _apikeys_vivo
    _apikeys_vivo = (bool(_apikeys.get("enabled", True)),
                     tuple((k.get("secret") or "", (k.get("role") or "").strip().lower())
                           for k in (_apikeys.get("keys") or [])))


def _save_apikeys():
    """Persiste .apikeys.json — inclusive o `role` (a separação admin/use só é
    real se sobreviver ao restart; sem o campo, `_load_apikeys` relia tudo como
    legado e a próxima subida devolvia admin a todas as chaves `use`)."""
    payload = {
        "enabled": bool(_apikeys.get("enabled", True)),
        "keys": [
            {
                "id": k["id"],
                "name": k.get("name") or "sem nome",
                "secret": k["secret"],
                "created_at": k.get("created_at") or "",
                # None (legado) é gravado como null EXPLÍCITO: distingue "papel
                # nunca definido" de "campo sumido por causa de uma build velha".
                "role": k.get("role") or None,
            }
            for k in (_apikeys.get("keys") or [])
            if k.get("secret")
        ],
    }
    _apikeys_publica()
    write_json_atomic(APIKEYS_PATH, payload)
    try:
        os.chmod(APIKEYS_PATH, 0o600)
    except Exception:  # noqa: BLE001
        pass
    _sync_legacy_apikey_file()


def _load_apikeys():
    """Carrega .apikeys.json; migra .apikey/env; gera chave padrão se vazio.

    NÃO GRAVA NADA no import (#601): o espelho `.apikey` e a migração do legado
    ficam só na RAM e são persistidos pela primeira operação EXPLÍCITA de chave
    (criar/rotacionar/apagar) ou por `POST /api/apikeys/persistir`. Antes o import
    reescrevia `.apikey` (e o `.apikeys.json` na migração) em todo processo que só
    importava o app — inclusive launcher ANTES de reapontar `TTS_APIKEYS_PATH`.
    Única exceção: arquivo CORROMPIDO é renomeado para `.apikeys.json.corrupt`,
    porque preservar a evidência antes de gerar chave nova é o ponto."""
    global _apikeys
    keys = []
    enabled = True
    if APIKEYS_PATH.exists():
        try:
            data = json.loads(APIKEYS_PATH.read_text())
            enabled = bool(data.get("enabled", True))
            for raw in data.get("keys") or []:
                sec = str(raw.get("secret") or "").strip()
                if not sec:
                    continue
                keys.append({
                    "id": str(raw.get("id") or uuid.uuid4().hex[:10]),
                    "name": str(raw.get("name") or "chave")[:64],
                    "secret": sec,
                    "created_at": str(raw.get("created_at") or ""),
                    # o papel é LIDO (antes era descartado e a chave `use` voltava
                    # admin no restart): normalize aqui, com o legado em None.
                    "role": _role_do_disco(raw.get("role")),
                })
        except Exception as exc:  # noqa: BLE001
            keys = []
            # corrompido: preserva p/ recuperação — abaixo uma chave nova é gerada
            try:
                APIKEYS_PATH.replace(APIKEYS_PATH.with_name(".apikeys.json.corrupt"))
            except OSError:
                pass
            print(f"⚠ .apikeys.json corrompido — backup em .apikeys.json.corrupt; "
                  f"uma chave nova será gerada: {exc}", flush=True)

    if not keys:
        # migra chave legada (.apikey) ou env
        legacy = ""
        if LEGACY_APIKEY_PATH.exists():
            try:
                legacy = LEGACY_APIKEY_PATH.read_text().strip()
            except Exception:  # noqa: BLE001
                legacy = ""
        seed = legacy or _ENV_API_KEY
        if seed:
            keys.append({
                "id": uuid.uuid4().hex[:10],
                "name": "padrão",
                "secret": seed,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
        else:
            keys.append({
                "id": uuid.uuid4().hex[:10],
                "name": "padrão",
                "secret": _new_api_secret(),
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
        _apikeys = {"enabled": True, "keys": keys}
        _apikeys_publica()
        return

    _apikeys = {"enabled": enabled, "keys": keys}
    _apikeys_publica()
    # o espelho `.apikey` (que o `run.sh` lê no boot) e a migração só vão ao disco
    # na próxima escrita — `_save_apikeys` chama `_sync_legacy_apikey_file`.


def _auth_enabled() -> bool:
    """Auth na rede se enabled e existe ao menos uma chave (arquivo ou env).

    `TTS_ROD_ADMIN_KEY` conta como chave: quem sobe o app só com ela não pode
    ficar com a rede ABERTA (era o caso: `_key_is_valid` não a conhecia e
    `_auth_enabled` só olhava TTS_ROD_API_KEY/chaves gerenciadas)."""
    ligado, chaves = _apikeys_vivo
    if not ligado:
        return False
    return bool(chaves) or bool(_ENV_API_KEY or _ADMIN_API_KEY)


def _extract_request_key(request) -> str:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.headers.get("x-api-key") or "").strip()


def _secrets_iguais(a: str, b: str) -> bool:
    """compare_digest sem estourar se os tamanhos diferem."""
    if not a or not b:
        return False
    try:
        return _secrets.compare_digest(a, b)
    except (TypeError, ValueError):
        return False


_own_ips_lock = threading.Lock()
_own_ips_cache: tuple[float, frozenset] = (0.0, frozenset())


def _peer_host(request) -> str:
    """IP do cliente TCP (sem o prefixo IPv4-mapeado em IPv6)."""
    host = (request.client.host if request.client else "") or ""
    if host.startswith("::ffff:"):
        host = host[7:]
    return host


def _own_ips() -> set[str]:
    """IPs deste Mac (loopback + interfaces). Cache curto: Wi-Fi muda."""
    global _own_ips_cache
    now = time.monotonic()
    with _own_ips_lock:
        ts, cached = _own_ips_cache
        if cached and (now - ts) < 15:
            return set(cached)
    ips = {"127.0.0.1", "::1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ip = info[4][0]
            if ip.startswith("::ffff:"):
                ip = ip[7:]
            if ip and ip not in ("0.0.0.0", "255.255.255.255", "::"):
                ips.add(ip)
    except Exception:  # noqa: BLE001
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("1.1.1.1", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:  # noqa: BLE001
        pass
    frozen = frozenset(ips)
    with _own_ips_lock:
        _own_ips_cache = (now, frozen)
    return ips


def _lan_urls() -> list[str]:
    """URLs IPv4 da LAN p/ a UI mostrar 'abra isto no outro dispositivo'."""
    urls = []
    for ip in sorted(_own_ips()):
        if ip in ("127.0.0.1", "::1") or ":" in ip:
            continue
        if ip.startswith("169.254."):
            continue
        urls.append(f"http://{ip}:7860")
    return urls


def _is_local(request) -> bool:
    """Só loopback é considerado local sem autenticação.

    Não isentamos os IPs da própria máquina: em um túnel SSH reverso o
    processo SSH pode abrir a conexão para o IP LAN do Mac usando justamente
    um IP local como origem, o que transformaria uma requisição da internet
    em uma requisição "local".
    """
    host = _peer_host(request)
    if host not in ("127.0.0.1", "::1", "localhost"):
        return False
    # DNS rebinding (#779): socket loopback com `Host` de fora NÃO é a UI — uma
    # página em `evil.tld` que resolve para 127.0.0.1 chega assim e não pode
    # herdar a isenção de chave.
    host = _host_da_requisicao(request)
    # nome do túnel/proxy configurado também conta como a própria UI (#779)
    return _host_de_loopback(host) or host in _ALLOWED_HOSTS


# ---------------------------------------------------------------------------
# Guarda de `Origin` no LOOPBACK (CSRF / DNS-rebinding) — auditoria #282
#
# O loopback é isento de auth E de rate limit, e o CORS default é `*`: qualquer
# página aberta no navegador do dono pode chamar `http://127.0.0.1:7860/...`
# (um `fetch` simples atravessa sem preflight) e — pior — LER a resposta, o que
# entrega `GET /api/apikeys?reveal=1` (todos os segredos), `GET /api/settings`
# (chaves de terceiros), o zip do mic-router (a chave primária) e permite
# `POST /api/shutdown` / `DELETE /api/outputs` às cegas. DNS-rebinding é o mesmo
# buraco com um hostname do atacante resolvido para 127.0.0.1.
#
# A regra NÃO afrouxa a LAN: só age quando o socket é loopback. Origem ausente
# (curl, SDK, scripts do repo) e origem da própria máquina seguem como antes.
# ---------------------------------------------------------------------------
#: Origens de FORA autorizadas, por igualdade EXATA (#813). Cobre extensão
#: (`chrome-extension://<id>`) e porta local (`http://localhost:5198`): o prefixo e a
#: porta sozinhos confiavam em qualquer extensão instalada e em qualquer porta de
#: loopback. Uma decisão só — auth, CORS e WS leem daqui.
#: Cliente de DESENVOLVIMENTO do repo (`client/`): é o default da lista, não uma
#: segunda decisão. Sem ele a primeira vítima de fechar a lista seria o próprio dev.
_CORS_ORIGENS_DEV = ("http://localhost:5198", "http://127.0.0.1:5198")
#: a lista é o cliente de dev SEMPRE mais o que o dono escrever (união, sem
#: duplicata): exportar a variável não pode derrubar o `client/` do repo.
_ORIGENS_PERMITIDAS = tuple(dict.fromkeys(
    list(_CORS_ORIGENS_DEV)
    + [x.strip().lower().rstrip("/")
       for x in (os.environ.get("TTS_ORIGENS_PERMITIDAS")
                 or os.environ.get("TTS_CORS_ORIGINS") or "").split(",") if x.strip()]))
_HOSTS_DA_MAQUINA = ("127.0.0.1", "localhost", "::1")

#: Nomes de `Host` aceitos além dos da máquina (#779). Lista por ambiente, com o
#: túnel/Cloudflare e o proxy por path; vírgula separa. Sem isto, uma página em
#: `evil.tld:7860` que resolve para 127.0.0.1 fala com a API como se fosse a UI.
_ALLOWED_HOSTS = tuple(
    x.strip().lower().rsplit(":", 1)[0] if x.strip().count(":") == 1 else x.strip().lower()
    for x in os.environ.get("TTS_ALLOWED_HOSTS", "").split(",") if x.strip())


def _host_da_requisicao(request) -> str:
    """`Host` normalizado: sem porta, sem colchetes de IPv6, minúsculo."""
    bruto = (request.headers.get("host") or "").strip().lower()
    if bruto.startswith("["):                       # [::1]:7860
        return bruto[1:bruto.find("]")] if "]" in bruto else bruto
    return bruto.rsplit(":", 1)[0] if bruto.count(":") == 1 else bruto


def _host_permitido(host: str) -> bool:
    """O `Host` é desta máquina (ou de um nome configurado)?"""
    if not host:
        return False
    if _host_de_loopback(host) or host in _ALLOWED_HOSTS:
        return True
    if host in _own_ips():
        return True
    if host.endswith(".local"):                     # Bonjour da própria máquina
        return True
    return False


def _host_de_loopback(host: str) -> bool:
    # `testserver` é o host padrão do `TestClient` (e só dele): sem isto a suíte
    # inteira viraria "LAN" e passaria a exigir chave (#779).
    return host in ("127.0.0.1", "localhost", "::1", "testserver")
#: rotas que REVELAM segredo para quem está no próprio Mac (a vista de admin):
#: com origem estrangeira elas passam a exigir chave em vez de 200 mudo.
_ROTAS_REVELAM_SEGREDO = ("/api/apikeys", "/api/settings", "/api/client/mic-router")
_origens_barradas: dict = {}
_origens_barradas_lock = threading.Lock()


def _mesma_origem(request) -> str:
    """Origem da própria requisição (`esquema://host`), para reconhecer a UI."""
    esquema = (request.headers.get("x-forwarded-proto") or request.url.scheme
               or "http").split(",")[0].strip()
    # WebSocket tem esquema próprio (`ws`/`wss`), mas o `Origin` do navegador vem
    # sempre em http(s): sem normalizar, a própria UI pareceria origem de fora (#813)
    esquema = {"ws": "http", "wss": "https"}.get(esquema, esquema)
    return f"{esquema}://{request.headers.get('host') or ''}"


#: Freio de força bruta (#785): N falhas de auth por IP em 60 s → 429. A
#: identidade é o IP de ORIGEM, não a chave apresentada (com a chave como
#: identidade cada chute caía num balde novo e nunca dava 429).
_AUTH_FALHAS_MAX = max(1, int(os.environ.get("TTS_AUTH_FALHAS_MAX", "20")))
_AUTH_FALHAS_JANELA_S = 60.0
_auth_falhas: dict[str, list] = {}          # ip → [n, inicio_da_janela]
_auth_falhas_lock = threading.Lock()


def _auth_freio(ip: str) -> float:
    """Segundos de espera se o IP estourou as falhas; 0 = liberado."""
    agora = time.monotonic()
    with _auth_falhas_lock:
        reg = _auth_falhas.get(ip)
        if not reg or agora - reg[1] > _AUTH_FALHAS_JANELA_S:
            return 0.0
        if reg[0] < _AUTH_FALHAS_MAX:
            return 0.0
        return max(1.0, _AUTH_FALHAS_JANELA_S - (agora - reg[1]))


def _auth_falhou(ip: str) -> None:
    agora = time.monotonic()
    with _auth_falhas_lock:
        reg = _auth_falhas.get(ip)
        if not reg or agora - reg[1] > _AUTH_FALHAS_JANELA_S:
            _auth_falhas[ip] = [1, agora]
            if len(_auth_falhas) > 512:             # teto: poda os mais antigos
                for velho in sorted(_auth_falhas, key=lambda k: _auth_falhas[k][1])[:128]:
                    _auth_falhas.pop(velho, None)
        else:
            reg[0] += 1


def _porta_do_servidor(request) -> int | None:
    """Porta em que o servidor foi chamado (do `Host`), com o default do esquema."""
    host = (request.headers.get("host") or "").strip()
    if ":" in host.rsplit("]", 1)[-1]:
        try:
            return int(host.rsplit(":", 1)[1])
        except ValueError:
            return None
    return 443 if (request.url.scheme or "").endswith("s") else 80


def _origem_confiavel(origem: str, request) -> bool:
    """A origem é a própria UI ou uma origem da lista `TTS_ORIGENS_PERMITIDAS`?

    Igualdade exata, nunca prefixo nem "é loopback": extensão e porta local só
    valem se estiverem escritas na lista (#813)."""
    if not origem:
        return True                      # sem `Origin`: cliente não-browser
    if origem == _mesma_origem(request):
        return True                      # a própria UI, mesma origem/porta
    normalizada = origem.lower().rstrip("/")
    if normalizada in _ORIGENS_PERMITIDAS:
        return True                      # lista explícita do dono (inclui extensão)
    # loopback na MESMA PORTA do servidor: `127.0.0.1:7860` chamado por
    # `localhost:7860` é a mesma máquina e o mesmo serviço — hoje só passava por
    # sorte de grafia. Porta DIFERENTE de loopback é outro servidor e não passa.
    # `Origin` é header de terceiro: porta inválida (`:abc`, `:99999`) faz o
    # `urlsplit().port` LEVANTAR ValueError — sem o `try` virava 500 em vez de 403
    try:
        partes = urlsplit(normalizada)
        porta = partes.port
        host = partes.hostname
    except ValueError:
        return False
    if host in _HOSTS_DA_MAQUINA and porta == _porta_do_servidor(request):
        return True
    # `*` desliga a guarda — escolha explícita de quem aceita CSRF de qualquer origem
    return "*" in _ORIGENS_PERMITIDAS


def _origem_estrangeira(request) -> bool:
    """Página de TERCEIRO chamando pelo loopback? (só importa quando é loopback)"""
    return not _origem_confiavel((request.headers.get("origin") or "").strip(), request)


def _rota_revela_segredo(path: str) -> bool:
    p = path.rstrip("/") or "/"
    return any(p == r or p.startswith(r + "/") for r in _ROTAS_REVELAM_SEGREDO)


def _marca_origem_barrada(request) -> None:
    """Conta/loga a origem barrada (uma linha por origem+path) — o dono precisa
    SABER que a tentativa houve; `/api/status` publica o acumulado."""
    origem = (request.headers.get("origin") or "").strip()[:120]
    chave = (origem, request.url.path)
    agora = time.time()
    with _origens_barradas_lock:
        reg = _origens_barradas.setdefault(chave, {"n": 0, "em": agora})
        reg["n"] += 1
        reg["em"] = agora
        if len(_origens_barradas) > 64:              # teto: poda os mais antigos
            for velha in sorted(_origens_barradas, key=lambda k: _origens_barradas[k]["em"])[:16]:
                _origens_barradas.pop(velha, None)
        primeira = reg["n"] == 1
    if primeira:
        metodo = getattr(request, "method", None) or "WS"   # WebSocket não tem verbo
        print(f"[origin] barrada {origem!r} em {metodo} {request.url.path}",
              file=sys.stderr, flush=True)


def _resumo_origens_barradas() -> dict:
    agora = time.time()
    with _origens_barradas_lock:
        # só a janela recente: tentativa de ontem não interessa ao painel
        recentes = {f"{o} {p}": {"n": r["n"], "segundos_atras": round(agora - r["em"], 1)}
                    for (o, p), r in _origens_barradas.items() if agora - r["em"] < 3600}
        total = sum(r["n"] for r in _origens_barradas.values())
    return {"total": total, "recentes": dict(list(recentes.items())[:12])}



def _key_is_valid(provided: str) -> bool:
    """A chave autentica em /api/* e /v1/*.

    A chave administrativa TAMBÉM vale como chave: ela é a credencial do
    operador, e sem isto o fluxo documentado ("defina TTS_ROD_ADMIN_KEY")
    devolvia 401 para quem só tinha a chave admin — e 403 de admin para quem
    só tinha a chave comum, ou seja, NADA administrava pela rede.
    """
    if not provided:
        return False
    if _ENV_API_KEY and _secrets_iguais(provided, _ENV_API_KEY):
        return True
    if _ADMIN_API_KEY and _secrets_iguais(provided, _ADMIN_API_KEY):
        return True
    return any(_secrets_iguais(provided, par[0]) for par in _apikeys_vivo[1] if par[0])


def _key_role(provided: str) -> str | None:
    """Papel da chave apresentada: "admin" | "use" | None (não é chave gerenciada).

    None = linha antiga, sem o campo — mantém a regra de compatibilidade (uma
    chave válida administra enquanto não existir `TTS_ROD_ADMIN_KEY`)."""
    if not provided:
        return None
    # sem lock (#799): `_admin_is_allowed` roda no event loop e o `_apikeys_lock` é
    # segurado por rotas do threadpool gravando disco
    for segredo, papel in _apikeys_vivo[1]:
        if segredo and _secrets_iguais(provided, segredo):
            return papel or None
    return None


def _admin_is_allowed(request) -> bool:
    """Admin local, chave administrativa dedicada ou chave com papel `admin`.

    O papel por chave (`role`) é a separação explícita "chave de uso x chave de
    administração" sem depender de variável de ambiente; linha sem `role`
    (arquivo anterior ao campo) preserva a política antiga.

    Loopback com `Origin` de TERCEIRO não conta como local (#282): senão qualquer
    página aberta no navegador do dono administrava a instalação — um `use` da
    LAN até, mas a página de fora não traz chave nenhuma e o privilégio vinha do
    socket. Com origem estrangeira quem decide é a credencial apresentada."""
    if _is_local(request) and not _origem_estrangeira(request):
        return True
    provided = _extract_request_key(request)
    # ASSIMETRIA DELIBERADA (#785): só a chave de USO fraca é demovida. A
    # `TTS_ROD_ADMIN_KEY` curta cai no ramo seguinte e administra — se ela for a
    # única chave, demovê-la deixaria o admin inalcançável pela LAN. Para a de uso,
    # o pior caso é o aparelho não administrar; para a de admin, seria não haver
    # admin nenhum.
    if _ENV_CHAVE_FRACA and _ENV_API_KEY and _secrets_iguais(provided, _ENV_API_KEY):
        return False
    if _ADMIN_API_KEY and _secrets_iguais(provided, _ADMIN_API_KEY):
        return True
    papel = _key_role(provided)
    if papel == "admin":
        return True
    if papel == "use":
        return False
    # Compatibilidade: chave sem papel e sem chave administrativa configurada
    # continua administrando a instalação, como nas versões anteriores.
    return not _ADMIN_API_KEY and _key_is_valid(provided)


def _exige_admin(request) -> None:
    """403 se a credencial não administra. Porta ÚNICA das rotas elevadas (#282).

    A régua é o DANO, não a conveniência: apagar/desligar/trocar o que é global
    é admin; CONSUMIR (gerar, editar, transcrever, ticket do Live, mic-route) é
    `use`. Antes, qualquer chave válida derrubava o servidor e apagava as vozes
    do dono. Com `role: use` a chave gera fala e não mexe na instalação; a chave
    legada (sem papel) segue administrando quando não existe `TTS_ROD_ADMIN_KEY`."""
    if not _admin_is_allowed(request):
        raise HTTPException(403, "Chave administrativa necessária (papel admin)")


def _normaliza_role(v) -> str | None:
    """`admin` | `use` para a chave; vazio/ausente = None (política legada)."""
    if v is None:
        return None
    s = str(v).strip().lower()
    if not s:
        return None
    if s not in ("admin", "use"):
        raise HTTPException(400, "role inválido (admin|use)")
    return s


def _primary_api_key() -> str:
    """Chave principal (a do header legado `X-API-Key` e a que abre tudo)."""
    with _apikeys_lock:
        keys = _apikeys.get("keys") or []
        if keys:
            return keys[0]["secret"]
    return _ENV_API_KEY or ""


#: nome da chave emitida para o cliente empacotado — reusada se já existir
_CHAVE_CLIENTE = "mic-router"


def _porta_do_host(bruto: str) -> str:
    """Porta do `Host`, inclusive em IPv6 (`[::1]:7860` tem três dois-pontos)."""
    if bruto.startswith("["):
        return bruto.rsplit("]:", 1)[1] if "]:" in bruto else ""
    return bruto.rsplit(":", 1)[1] if bruto.count(":") == 1 else ""


def _host_do_cliente(request) -> str:
    """Host do `server_url` do cliente: só nome que a OUTRA máquina alcança.

    O `Host` é o que o cliente mandou, não o que o servidor reconhece. Fora da
    allowlist o middleware já responde 421 (#779) — o que sobra é o `Host` ACEITO
    e inútil do outro lado: `localhost`/`127.0.0.1`/`testserver` valem para esta
    máquina e apontariam para o próprio cliente. Nesses casos, nome de confiança:
    túnel configurado, senão um IP desta máquina. A porta é sempre a do servidor.
    """
    bruto = (request.headers.get("host") or "").strip().lower()
    porta = _porta_do_host(bruto)
    nome = _host_da_requisicao(request)
    inuteis = {*_HOSTS_DA_MAQUINA, "testserver"}
    if nome and _host_permitido(nome) and nome not in inuteis:
        return bruto
    for candidato in (*_ALLOWED_HOSTS, *sorted(_own_ips())):
        if candidato and _host_permitido(candidato) and candidato not in inuteis:
            # IPv6 vai entre colchetes quando a porta acompanha (`[::1]:7860`)
            alvo = f"[{candidato}]" if ":" in candidato else candidato
            return f"{alvo}:{porta}" if porta else alvo
    return f"127.0.0.1:{porta}" if porta else "127.0.0.1:7860"


def _chave_para_cliente() -> str:
    """Chave de USO para o zip do cliente (#793).

    `keys[0]` é a primária e costuma ser `admin`: entregá-la no artefato dá ao
    cliente poder de mexer em conexão externa e ler segredo sem precisar. Aqui sai
    uma `use` — de baixo privilégio, então REAPROVEITAR a primeira que existir é
    aceitável; só quando não há nenhuma é criada uma chamada `mic-router`."""
    with _apikeys_lock:
        for k in _apikeys.get("keys") or []:
            if _normaliza_role(k.get("role")) == "use" and (k.get("secret") or ""):
                return k["secret"]
        row = {"id": uuid.uuid4().hex[:10], "name": _CHAVE_CLIENTE,
               "secret": _new_api_secret(),
               "created_at": time.strftime("%Y-%m-%d %H:%M:%S"), "role": "use"}
        _apikeys.setdefault("keys", []).append(row)
        _save_apikeys()
        return row["secret"]


def _public_key_row(k: dict, *, reveal: bool = False) -> dict:
    row = {
        "id": k["id"],
        "name": k.get("name") or "sem nome",
        "masked": _mask_secret(k.get("secret") or ""),
        "created_at": k.get("created_at") or "",
        "readonly": bool(k.get("readonly")),
        "role": k.get("role") or None,      # None = legado (regra antiga de admin)
    }
    if reveal:
        row["secret"] = k.get("secret") or ""
    return row


_load_apikeys()

# compat: scripts antigos / mic-router que leem API_KEY no módulo
API_KEY = _primary_api_key() or _ENV_API_KEY or None

def _ffmpeg_bin() -> str:
    """ffmpeg do sistema ou, ausente, o binário estático do imageio-ffmpeg
    (já é dependência do projeto) — Macs sem ffmpeg no PATH ficam cobertos."""
    ff = shutil.which("ffmpeg")
    if ff:
        return ff
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 — mantém fallback histórico
        return "/opt/homebrew/bin/ffmpeg"


FFMPEG = _ffmpeg_bin()

# Prefixo de proxy por PATH (ex.: https://dominio/ttsproxy/...). É lido do ambiente
# ANTES da construção do app porque vira `root_path` — assim o OpenAPI e o `/docs`
# anunciam o caminho certo (antes o schema saía "cru" e o Swagger atrás do proxy
# apontava para a raiz do domínio, #282). O roteamento continua funcionando pelo
# middleware `_strip_base_path`, que é quem remove o prefixo do path.
_BASE_PATH = os.environ.get("TTS_ROD_BASE_PATH", "/ttsproxy").rstrip("/")
app = FastAPI(title="TTS-STUDIO", root_path=_BASE_PATH or "")


# ---------------------------------------------------------------------------
# Quem mandou o sinal? (#423)
#
# A instância de :7860 caiu DUAS vezes com shutdown limpo no log — sinal, não
# crash — e o log não dizia quem. Um teste rodando no mesmo repo, um `kill` de
# script, `pkill` largo: sem a assinatura do autor, cada queda vira adivinhação e
# derruba toda suíte que dependia da instância. Estes handlers LOGAM o sinal, o pai
# e a linha de comando do pai e então ENCAMINHAM para o handler do uvicorn (que é
# quem faz o shutdown gracioso via lifespan) — sem isso, o registro viraria
# problema.
# ---------------------------------------------------------------------------
#: Quem era o pai no BOOT. Quando quem subiu a instância morre, o app é
#: reparentado para o launchd e `os.getppid()` vira 1 — o dado de boot é o único
#: que sobrevive a isso (#471).
_PAI_DO_BOOT: dict = {}


def _grava_pai_do_boot() -> None:
    try:
        ppid = os.getppid()
        _PAI_DO_BOOT.update(pid=ppid, lstart=_pid_lstart(ppid),
                            cmd=_mascara_cmd(_pid_cmd(ppid))[:160])
    except Exception:  # noqa: BLE001
        pass


def _pid_lstart(pid) -> str:
    """Início do processo (formato do `ps`), "" quando não dá para ler."""
    try:
        return subprocess.run(["ps", "-p", str(int(pid)), "-o", "lstart="],
                              capture_output=True, text=True, timeout=2).stdout.strip()
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""


def _processos_do_grupo(pgid: int, sid: int) -> str:
    """`pid:pai:pgid:cmd` dos processos do MESMO grupo ou sessão (mascarados).

    Com o app órfão, `pai=1` não diz nada: quem matou costuma ser um IRMÃO de
    sessão (outra coisa subida pela mesma shell). Esta lista é o que sobra para
    identificar o autor depois do fato."""
    try:
        saida = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,pgid=,command="],
                               capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    linhas = []
    for linha in saida.splitlines():
        partes = linha.split(None, 3)
        if len(partes) < 4:
            continue
        pid, ppid, pg, cmd = partes
        if pid.startswith(str(os.getpid())) and int(pid) == os.getpid():
            continue
        if cmd.startswith("ps -A"):        # a própria chamada não é autor
            continue
        if pg == str(pgid) or pg == str(sid):
            linhas.append(f"{pid}:{ppid}:{pg}:{_mascara_cmd(cmd)[:80]}")
        if len(linhas) >= 8:
            break
    return " | ".join(linhas)


def _assinatura_do_sinal(signum: int) -> None:
    try:
        ppid = os.getppid()
        # mascaradas: o log pode ir para arquivo em /tmp e o pai pode carregar
        # segredo em argumento (`cloudflared --token …`, gate #440 item 4)
        pai = _mascara_cmd(_pid_cmd(ppid))[:200] or "?"
        eu = _mascara_cmd(_pid_cmd(os.getpid()))[:160] or "?"
        try:
            pgid, sid = os.getpgid(0), os.getsid(0)
        except OSError:
            pgid = sid = -1
        quando = time.strftime("%Y-%m-%dT%H:%M:%S")     # estável, sem locale
        print(f"[sinal] {quando} {signal.Signals(signum).name} — pid={os.getpid()} "
              f"pgid={pgid} sid={sid} pai={ppid} ({pai}) · eu={eu}",
              file=sys.stderr, flush=True)
        # órfão: o pai já foi embora e não identifica nada — vai o grupo, o dado de
        # boot e quem mais ainda está na sessão
        if ppid == 1 or ppid == 0:
            print(f"[sinal] ÓRFÃO — pai de boot={_PAI_DO_BOOT.get('pid')} "
                  f"({_PAI_DO_BOOT.get('cmd') or '?'} em {_PAI_DO_BOOT.get('lstart') or '?'}) "
                  f"· grupo/sessão: {_processos_do_grupo(pgid, sid) or '(ninguém)'}",
                  file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 — registro nunca pode impedir o encerramento
        pass


def _instala_rastro_de_sinal() -> list[tuple[int, object]]:
    """Instala os handlers e devolve (sinal, handler anterior) para restaurar."""
    _grava_pai_do_boot()
    anteriores: list[tuple[int, object]] = []
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            anterior = signal.getsignal(signum)
            anteriores.append((signum, anterior))

            def _handler(n, _frame, _anterior=anterior):
                _assinatura_do_sinal(n)
                # encaminha: uvicorn instala o dele (ou some com SIG_DFL). Chamar o
                # anterior mantém o shutdown gracioso; com o default, reenvia o
                # sinal para si mesmo depois de restaurar.
                if callable(_anterior):
                    return _anterior(n, _frame)
                signal.signal(n, signal.SIG_DFL)
                os.kill(os.getpid(), n)

            signal.signal(signum, _handler)
        except (ValueError, OSError):
            continue          # fora da thread principal (ex.: import em thread)
    return anteriores
#: Origens de fora autorizadas a LER a resposta (#780). Sem curinga: o default é a
#: UI de desenvolvimento do repo (`client/`, porta 5198), que é outra origem; o
#: ALIAS histórico de `TTS_ORIGENS_PERMITIDAS` (o CORS lia daqui). É a MESMA tupla:
#: duas listas com a mesma semântica voltariam a divergir.
_CORS_ORIGINS = list(_ORIGENS_PERMITIDAS)
# NOTA: o CORS é registrado no FIM do módulo (depois dos @app.middleware) de
# propósito. `add_middleware` empilha na frente, então quem é registrado por
# último fica por FORA — e um 401/429 que sai do middleware de auth precisa
# atravessar o CORS para ganhar os cabeçalhos. Registrado aqui em cima ele
# ficava por dentro: o cliente cross-origin via "Failed to fetch" em vez do
# 401 "chave inválida" (o preflight passava, a resposta real não).

# Limite leve em memória: impede que uma chave/IP monopolize STT, importação ou
# criação de jobs. Não substitui rate limit no nginx, mas protege o servidor
# mesmo quando ele é usado diretamente na LAN.
_rate_lock = threading.Lock()
_rate_hits = defaultdict(deque)
_RATE_WINDOW = 60.0
_RATE_DEFAULT = int(os.environ.get("TTS_RATE_LIMIT", "120"))
_RATE_HEAVY = int(os.environ.get("TTS_HEAVY_RATE_LIMIT", "20"))
_RATE_HEAVY_PATHS = ("/v1/audio/speech", "/api/transcribe",
                     "/api/stt-partial", "/api/translate-speech", "/api/modify-speech",
                     "/api/voices/import",
                     # baixa e transcodifica de fora, até 300 MB por chamada (#790)
                     "/api/youtube-audio")
_RATE_HEAVY_EXATAS = ("/api/tts",)          # o POST que gera; consultar o job não é
# Caminhos que a própria UI polla de propósito (andamento do job, chat, fila do
# mic, status): 120/min derrubava o polling do próprio job numa geração longa e
# aparecia como 429 no log, com o áudio morrendo no meio da frase. Vem ANTES do
# pesado porque /api/tts/jobs/… começa com /api/tts e não é geração.
_RATE_POLL_PATHS = ("/api/status", "/api/mic-route", "/api/tts/jobs/", "/api/chat/",
                    "/api/outputs")
_RATE_POLL = int(os.environ.get("TTS_POLL_RATE_LIMIT", "1200"))


def _rate_limit_for(path: str) -> int:
    if path.startswith(_RATE_POLL_PATHS):
        return _RATE_POLL
    if path in _RATE_HEAVY_EXATAS or path.startswith(_RATE_HEAVY_PATHS):
        return _RATE_HEAVY
    return _RATE_DEFAULT


# Teto de buckets. A identidade é a chave/IP (efêmera) e o path tem id (job,
# voz, saída, sessão): sem nada disso o dict cresce por (chave, path) e o teto
# antigo só varria o que JÁ tinha expirado — uma rajada de paths distintos dentro
# da mesma janela não caberia em lugar nenhum e ficava tudo "vivo".
_RATE_MAX_BUCKETS = max(100, int(os.environ.get("TTS_RATE_MAX_BUCKETS", "10000")))
# Rotas cujo segmento seguinte é um ID — sem isso cada job/voz/saída vira um
# bucket próprio. O LIMITE continua saindo do path cru (`/api/voices/import`
# mantém o teto pesado); o que colapsa é só a CHAVE do bucket.
_RATE_PREFIXOS_ID = ("/api/tts/jobs/", "/api/voices/", "/api/outputs/",
                     "/api/speaker/", "/api/apikeys/", "/api/chat/")
_RATE_SUB_ROTAS = frozenset({"import", "export", "design", "audio", "peaks",
                             "denoise", "replace", "rotate", "pieces", "start",
                             "enroll", "profiles", "check", "enabled"})


def _rate_path_normalizado(path: str) -> str:
    """Path com o ID colapsado em `*` (`/api/tts/jobs/abc/pieces/0` →
    `/api/tts/jobs/*/pieces/*`). Sub-rotas estáticas (`voices/import`) ficam."""
    for pref in _RATE_PREFIXOS_ID:
        if not path.startswith(pref):
            continue
        partes = path[len(pref):].split("/")
        if not partes or not partes[0]:
            return path
        for i in range(len(partes)):
            if partes[i] not in _RATE_SUB_ROTAS:
                partes[i] = "*"
        return pref + "/".join(partes)
    return path


def _rate_poda(now: float) -> None:
    """Mantém `_rate_hits` abaixo do teto (chamar com `_rate_lock` tomado).

    Primeiro descarta o que expirou; se ainda estiver cheio (muitos paths ou
    identidades na MESMA janela) remove os buckets tocados há mais tempo. Drena
    20% de uma vez para o custo do sorted não cair em toda requisição — enquanto
    está abaixo do teto o caminho é uma comparação de int."""
    if len(_rate_hits) <= _RATE_MAX_BUCKETS:
        return
    for chave, bucket in list(_rate_hits.items()):
        if not bucket or now - bucket[-1] >= _RATE_WINDOW:
            _rate_hits.pop(chave, None)
    excesso = len(_rate_hits) - _RATE_MAX_BUCKETS
    if excesso <= 0:
        return
    for chave, _ in sorted(_rate_hits.items(),
                           key=lambda kv: kv[1][-1] if kv[1] else 0.0
                           )[:excesso + _RATE_MAX_BUCKETS // 5]:
        _rate_hits.pop(chave, None)


#: O que é público SEM chave pela LAN (#777). Lista FECHADA de propósito: o
#: default é negar, então rota nova nasce protegida em vez de nascer aberta.
_CAMINHOS_PUBLICOS = frozenset({
    "/", "/index.html",          # a UI (o HTML traz o nonce da CSP)
    "/health",                   # liveness de monitoramento
    "/live-worklet.js",          # AudioWorklet do Live: o navegador busca sozinho
    "/favicon.ico",
})


def _caminho_roteado(request) -> str:
    """Path que o ROTEADOR vai casar: prefixo do proxy removido UMA vez, normalizado.

    O `root_path` do FastAPI já é tirado pelo Starlette no roteamento; se o
    middleware também reescrevesse o `scope["path"]`, o prefixo duplo
    (`/ttsproxy/ttsproxy/...`) saía como `/ttsproxy/api/...`, não casava a lista de
    protegidos e passava sem chave (#777). A normalização mata `//`, `/./`, `..` e
    barra final: o veredito não pode depender da forma do path."""
    caminho = request.scope.get("path") or "/"
    if _BASE_PATH and (caminho == _BASE_PATH or caminho.startswith(_BASE_PATH + "/")):
        caminho = caminho[len(_BASE_PATH):] or "/"
    partes: list[str] = []
    for pedaco in caminho.split("/"):
        if pedaco in ("", "."):
            continue
        if pedaco == "..":
            if partes:
                partes.pop()
            continue
        partes.append(pedaco)
    return "/" + "/".join(partes)


# ---------------------------------------------------------------------------
# Teto de corpo ANTES do spool (#790)
#
# O formparser do Starlette espolava o corpo inteiro em disco e só então a rota
# pedia admin/tamanho: 80 MB anônimos viravam arquivo para terminar em 403. Aqui a
# recusa sai pelo `Content-Length` (sem ler nada) e, quando ele falta ou mente, o
# stream é cortado no limite. Os tetos são por rota: `restore`/`import` recebem zip
# grande de propósito, JSON é pequeno e abusável.
# ---------------------------------------------------------------------------
_TETO_CORPO_PADRAO = 70 * 1024 * 1024
#: mesmo teto do `_IMPORT_MAX_TOTAL`: um número maior aqui seria decorativo (a rota
#: recusaria antes, já com o corpo espoolado)
_TETO_CORPO_GRANDE = 512 * 1024 * 1024
#: JSON carrega histórico de conversa e `instruct` de design: 1 MB apertava
_TETO_CORPO_JSON = 8 * 1024 * 1024
#: rotas que aceitam corpo GRANDE (zip) de propósito
_CORPO_GRANDE = ("/api/restore", "/api/voices/import")
#: rotas `admin` que recebem corpo: a credencial é checada ANTES de ler o corpo
_ADMIN_COM_CORPO = ("/api/restore", "/api/voices/import", "/api/apikeys", "/api/shutdown",
                    "/api/models/unload")


def _teto_do_corpo(caminho: str, content_type: str) -> int:
    if caminho.startswith(_CORPO_GRANDE):
        return _TETO_CORPO_GRANDE
    if "json" in (content_type or "").lower():
        return _TETO_CORPO_JSON
    return _TETO_CORPO_PADRAO


class _TetoDeCorpo:
    """ASGI puro: 413 sem espoolar o corpo acima do teto da rota.

    CUSTO DECLARADO: quando o `Content-Length` falta ou mente, o corpo é acumulado
    em MEMÓRIA até o teto (no pior caso os 70 MB de áudio; os 512 MB de
    `restore`/`import` só nesse caminho) em vez de ir para o disco. É a troca
    consciente: o caminho normal, com header honesto, não bufferiza nada, e o
    anormal precisa de um retrato inteiro para decidir antes de a rota escrever.
    Espelhar em temporário é o passo seguinte se o pico de RSS incomodar."""

    def __init__(self, app):
        self.app = app

    async def _com_freio(self, scope, receive, send, ip: str):
        """Roda o app e conta 401/403 como falha de auth daquele IP."""
        status = {"codigo": 0}

        async def _send(msg):
            if msg.get("type") == "http.response.start":
                status["codigo"] = int(msg.get("status") or 0)
            await send(msg)

        await self.app(scope, receive, _send)
        if status["codigo"] in (401, 403):
            _auth_falhou(ip)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from starlette.requests import Request

        request = Request(scope)
        caminho = _caminho_roteado(request)
        # 0) `Host` fora da allowlist não fala com a API (#779). Vem antes de tudo,
        # inclusive de GET: é a defesa de DNS rebinding.
        if not _host_permitido(_host_da_requisicao(request)):
            from starlette.responses import JSONResponse

            return await JSONResponse(
                {"detail": f"Host não permitido: {_host_da_requisicao(request) or '(vazio)'}",
                 "hint": "Abra por 127.0.0.1/localhost, pelo IP da máquina ou por um "
                         "nome em TTS_ALLOWED_HOSTS."},
                status_code=421)(scope, receive, send)
        # freio de força bruta ANTES da auth (#785): passou do teto de falhas do
        # IP, 429 com `Retry-After` sem nem conferir a chave
        ip = _peer_host(request) or "?"
        espera = _auth_freio(ip)
        if espera > 0:
            from starlette.responses import JSONResponse

            return await JSONResponse(
                {"detail": f"Muitas falhas de autenticação deste IP — tente em "
                           f"{espera:.0f}s"},
                status_code=429, headers={"Retry-After": str(int(espera) + 1)})(
                scope, receive, send)

        # GET/HEAD/OPTIONS não têm corpo: o teto não se aplica
        if scope.get("method") in (None, "GET", "HEAD", "OPTIONS"):
            return await self._com_freio(scope, receive, send, ip)
        teto = _teto_do_corpo(caminho, request.headers.get("content-type", ""))

        async def _recusa(motivo: str):
            from starlette.responses import JSONResponse

            cab = {}
            origem = (request.headers.get("origin") or "").strip()
            if origem and _origem_confiavel(origem, request):
                # este middleware fica FORA do CORS (precisa vencer a auth): sem o
                # cabeçalho aqui o navegador não deixaria ler o 413
                cab["access-control-allow-origin"] = origem
            resp = JSONResponse({"detail": motivo,
                                 "hint": f"teto desta rota: {teto // (1024 * 1024)} MB"},
                                status_code=413, headers=cab)
            await resp(scope, receive, send)

        # 1) recusa barata: o `Content-Length` já diz que não cabe
        bruto = request.headers.get("content-length")
        if bruto:
            try:
                if int(bruto) > teto:
                    return await _recusa(
                        f"Corpo maior que o teto de {teto // (1024 * 1024)} MB")
            except ValueError:
                pass

        # 2) sem `Content-Length` (ou mentindo): lê até o limite e DECIDE antes de
        # chamar a rota. Cortar e seguir publicava corpo truncado — o parser dava
        # 400 ("áudio obrigatório") em vez de 413 (#790). O corpo fica em `blocos`
        # (RAM até o teto; ver o custo declarado na docstring da classe).
        blocos, recebidos = [], 0
        while True:
            msg = await receive()
            if msg.get("type") != "http.request":
                break
            pedaco = msg.get("body") or b""
            recebidos += len(pedaco)
            if recebidos > teto:
                return await _recusa(
                    f"Corpo maior que o teto de {teto // (1024 * 1024)} MB")
            blocos.append(pedaco)
            if not msg.get("more_body"):
                break
        corpo_int = b"".join(blocos)
        entregue = False

        async def _replay():
            nonlocal entregue
            if not entregue:
                entregue = True
                return {"type": "http.request", "body": corpo_int, "more_body": False}
            return {"type": "http.disconnect"}

        # 4) admin ANTES do corpo: `use` em rota admin não pode pagar o spool
        if caminho.startswith(_ADMIN_COM_CORPO) and _auth_enabled() \
                and not _admin_is_allowed(request):
            # mesma régua do `_exige_chave`: sem credencial nenhuma é 401; com
            # credencial que não administra é 403 (o corpo nem é lido)
            from starlette.responses import JSONResponse

            sem_chave = not _extract_request_key(request)
            detalhe = ("Não autorizado" if sem_chave
                       else "Chave administrativa necessária (papel admin)")
            return await JSONResponse({"detail": detalhe},
                                      status_code=401 if sem_chave else 403)(
                scope, receive, send)

        await self._com_freio(scope, _replay, send, ip)



@app.middleware("http")
async def _rate_limit(request, call_next):
    caminho = _caminho_roteado(request)
    if request.method == "OPTIONS" or not caminho.startswith(("/api/", "/v1/")):
        return await call_next(request)
    if _is_local(request):
        # O limitador existe para proteger a caixa exposta pela internet. O
        # navegador rodando no próprio Mac é o cliente nativo e polla a API em
        # sub-segundo por design — medir isso contra o teto de 120/min gerava 429
        # interno. Vale só para loopback: pela internet (túnel) a origem é o IP LAN
        # do Mac, que continua limitado.
        return await call_next(request)
    limit = max(0, _rate_limit_for(caminho))
    if limit:
        # Usa a chave quando presente para não agrupar todos os clientes atrás
        # do mesmo proxy; nunca registra o segredo, apenas um identificador curto.
        raw_identity = request.headers.get("x-api-key") or request.headers.get("authorization") or _peer_host(request) or "unknown"
        identity = hashlib.sha256(raw_identity.encode("utf-8", "ignore")).hexdigest()[:16]
        now = time.monotonic()
        chave = (identity, _rate_path_normalizado(caminho))
        with _rate_lock:
            # a poda vem ANTES de pegar o bucket: assim o bucket desta requisição
            # não corre o risco de ser evituado no mesmo request (deixaria a
            # referência `hits` órfã e a contagem se perderia).
            _rate_poda(now)
            hits = _rate_hits[chave]
            while hits and now - hits[0] >= _RATE_WINDOW:
                hits.popleft()
            if len(hits) >= limit:
                from fastapi.responses import JSONResponse
                return JSONResponse({"detail": "Muitas requisições; tente novamente em breve"},
                                    status_code=429, headers={"Retry-After": "60"})
            hits.append(now)
    return await call_next(request)


@app.middleware("http")
async def _exige_chave(request, call_next):
    # NOTA: o 421 de `Host` NÃO mora aqui — quem responde é o `_TetoDeCorpo`, que é
    # externo a este middleware. Duas cópias do mesmo veredito envelhecem mal (#790).
    # somente loopback dispensa chave; qualquer acesso pela LAN exige autenticação
    local = _is_local(request)
    # página de TERCEIRO chamando pelo loopback (#282): o socket é local, a origem
    # não. `local` deixa de valer como credencial, mas só nos casos que importam —
    # GET que não revela segredo continua livre, para não quebrar bookmark/SDK.
    estrangeira = local and _origem_estrangeira(request)
    caminho = _caminho_roteado(request)
    # NEGA POR PADRÃO (#777): só a lista fechada acima é pública. Antes a decisão
    # era `startswith(("/api/", "/v1/"))`, então qualquer path fora disso — e o
    # prefixo duplo do proxy caía fora — passava sem chave.
    protegido = caminho not in _CAMINHOS_PUBLICOS
    # Origem ESTRANGEIRA pede chave em TODO método, GET incluído (#780): com o
    # atalho do #282, qualquer site lia vozes, saídas e a amostra da voz do dono
    # (biometria) por fetch no loopback — e o `ACAO: *` deixava LER. Bookmark e
    # curl/SDK não mandam `Origin`, então seguem livres.
    if protegido and request.method != "OPTIONS" and _auth_enabled():
        from fastapi.responses import JSONResponse
        chave = _extract_request_key(request)
        if estrangeira and _rota_revela_segredo(caminho) \
                and not _key_is_valid(chave):
            # 403 e não 401: a credencial do app não é o ponto — quem chamou foi
            # uma página de fora. O motivo vai no corpo para o dono entender.
            _marca_origem_barrada(request)
            return JSONResponse(
                {"detail": "Origem desconhecida pedindo rota que revela segredo",
                 "origin": (request.headers.get("origin") or "")[:120],
                 "hint": "A página que chamou não é a UI. Mande a chave "
                         "(Authorization: Bearer ou X-API-Key), sirva a UI por "
                         "esta mesma origem/porta, ou autorize esta origem em "
                         "TTS_ORIGENS_PERMITIDAS."},
                status_code=403, headers={"X-TTS-Origin-Bloqueada": "1"})
        ok = (local and not estrangeira) or _key_is_valid(chave)
        if not ok:
            if estrangeira:
                _marca_origem_barrada(request)
            return JSONResponse(
                {"detail": "Não autorizado",
                 "hint": "Cole a chave da API. No Mac do servidor: "
                         "Configurações → Acesso → Revelar (ou o terminal do ./run.sh)."},
                status_code=401)
    return await call_next(request)


# Proxy reverso por PATH: o middleware REMOVE o prefixo antes do roteamento —
# requisições locais (sem prefixo) são intocadas. Registrado DEPOIS da auth => roda
# ANTES: a auth vê /api/... limpo e a exigência de chave vale também pelo proxy
# (origem = IP de LAN, não loopback). `_BASE_PATH` é definido lá em cima (é o
# `root_path` do app).


# CSP pelo HEADER (não pelo `<meta http-equiv>`): o Chromium ignora
# `frame-ancestors` quando a policy vem em meta — a UI podia ser emoldurada
# (clickjacking no botão Revelar da chave) e o console nascia com erro em toda
# carga. Texto IDÊNTICO ao do meta enquanto os dois existirem; o frontend remove o
# meta depois deste header entrar (aí ESTE vira a fonte da verdade).
#: os DOIS bundles que a página carrega do jsDelivr — caminho exato, versão
#: cravada. O host sozinho deixava qualquer injeção de HTML puxar script de um
#: pacote arbitrário do CDN e rodar com o nonce contornado (#792).
_JSDELIVR_VAD = "https://cdn.jsdelivr.net/npm/@ricky0123/vad-web@0.0.19/dist"
_JSDELIVR_ORT = "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.14.0/dist"
_CSP_POLICY = (
    "default-src 'self'; script-src 'self' 'wasm-unsafe-eval' "
    f"{_JSDELIVR_ORT}/ort.min.js {_JSDELIVR_VAD}/bundle.min.js "
    f"{_JSDELIVR_VAD}/vad.worklet.bundle.min.js; "
    "style-src 'self' 'unsafe-inline' "
    "https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src "
    "'self' data: blob:; media-src 'self' data: blob:; connect-src 'self' "
    "http://127.0.0.1:* http://localhost:* "          # o VAD busca o .onnx e o .wasm
    f"{_JSDELIVR_VAD}/ {_JSDELIVR_ORT}/; object-src 'none'; "
    "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)
# /docs e /redoc montam a própria página com CSS/JS de CDN: precisam de policy
# PRÓPRIA (a da UI não cobre o CDN no style-src) e não têm script inline nosso,
# então vão sem nonce. Antes ficavam SEM CSP nenhuma (#282).
#: o Swagger UI monta a página com UM bundle e UM css daquele caminho (#792)
_JSDELIVR_SWAGGER = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5"
_CSP_DOCS = (
    "default-src 'self'; script-src 'self' "
    f"{_JSDELIVR_SWAGGER}/swagger-ui-bundle.js "
    "'unsafe-inline'; style-src 'self' 'unsafe-inline' "
    f"{_JSDELIVR_SWAGGER}/swagger-ui.css "
    "https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com "
    "data:; img-src 'self' data: https://fastapi.tiangolo.com; connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
)
#: caminhos que recebem a policy de DOCS em vez da da UI
_CSP_DOCS_PATHS = ("/docs", "/redoc", "/docs/oauth2-redirect", "/openapi.json")
# O `<script>` inline precisa do nonce DESTA resposta (sem ele, sem
# 'unsafe-inline', o app não roda). A marcação no fonte é `<script>` cru: quem
# atribui o nonce é `_NONCE_RE`, não o texto do arquivo. Assim a injeção não
# depende da forma da tag — atributo (`type="module"`, `id`), código na mesma
# linha ou reflow não desligam a página inteira em silêncio, que era o risco da
# versão anterior (casava só a linha que fosse exatamente `<script>`).
# `src=` fica de fora: os dois bundles de CDN são cobertos pelo source do CDN e
# já carregam SRI (nonce neles não acrescentaria nada).
_NONCE_RE = re.compile(r"<script(?![^>]*\bnonce=)(?![^>]*\bsrc=)", re.I)


def _csp_com_nonce(nonce: str | None) -> str:
    """Policy com o nonce DESTA resposta no `script-src` (None = sem nonce)."""
    if not nonce:
        return _CSP_POLICY
    return _CSP_POLICY.replace("script-src 'self'",
                               f"script-src 'self' 'nonce-{nonce}'", 1)


def _index_com_nonce(nonce: str) -> str:
    """index.html com `nonce` em TODO `<script>` inline (ver `_NONCE_RE`)."""
    html = (BASE / "static" / "index.html").read_text(encoding="utf-8")
    return _NONCE_RE.sub(f'<script nonce="{nonce}"', html)


_rotas_conhecidas: dict = {}


def _rota_casa(path: str, method: str) -> str | None:
    """Classifica o par path+method para desfazer a mentira do `StaticFiles`.

    O mount `/` aceita QUALQUER path, então o arquivo estático responde 404/405
    no lugar do roteador. Devolve `"rota"` (um handler responde por path E
    método), `"metodo"` (o path existe, aquele método não) ou None (path
    desconhecido). Só roda no caminho de erro, com cache e teto."""
    from starlette.routing import Match
    chave = (path, method)
    if chave in _rotas_conhecidas:
        return _rotas_conhecidas[chave]
    escopo = {"type": "http", "path": path, "method": method}
    achado = None
    from starlette.routing import Mount
    for rota in app.router.routes:
        # o Mount (o `StaticFiles` em `/`) casa com QUALQUER path: ele é justamente
        # quem responde a mentira, então não serve como prova de existência
        if isinstance(rota, Mount) or not hasattr(rota, "path"):
            continue
        try:
            casou, _ = rota.matches(escopo)
        except Exception:  # noqa: BLE001 — rota exótica não derruba a decisão
            continue
        if casou is Match.FULL:
            achado = "rota"
            break
        if casou is Match.PARTIAL:
            achado = "metodo"
    if len(_rotas_conhecidas) > 256:
        _rotas_conhecidas.clear()
    _rotas_conhecidas[chave] = achado
    return achado


@app.middleware("http")
async def _strip_base_path(request, call_next):
    if _BASE_PATH and (request.url.path == _BASE_PATH
                       or request.url.path.startswith(_BASE_PATH + "/")):
        # preserva o path ORIGINAL (com prefixo) p/ handlers que precisam
        # reconstruir URLs absolutas (ex.: zip do mic-router). NÃO reescreve
        # `scope["path"]`: quem tira o `root_path` é o roteador do Starlette, e
        # reescrever aqui fazia o prefixo duplo sair do `root_path` duas vezes e
        # escapar da auth (#777). Quem decide path usa `_caminho_roteado`.
        request.scope["tts_prefix_path"] = request.url.path
    resp = await call_next(request)
    # 405 em path de API INEXISTENTE vira 404: quem responde 405 é o `StaticFiles`
    # do mount `/`, que aceita qualquer path. Só converte quando nenhum pattern
    # casa — `GET /api/tts` (path existe, método não) continua 405.
    caminho = _caminho_roteado(request)
    if resp.status_code in (404, 405) and caminho.startswith("/api/"):
        from fastapi.responses import JSONResponse
        classificado = _rota_casa(caminho, request.method)
        if classificado is None:
            resp = JSONResponse({"detail": "Rota não encontrada", "path": caminho},
                                status_code=404)
        elif classificado == "metodo" and resp.status_code == 404:
            # o path existe com outro método: 405 é a resposta honesta
            resp = JSONResponse({"detail": "Método não permitido para esta rota",
                                 "path": caminho, "method": request.method},
                                status_code=405)
    # HTML sempre revalidado (ETag) — updates da UI chegam no refresh;
    # cabeçalhos de segurança básicos
    if (resp.headers.get("content-type") or "").startswith("text/html"):
        resp.headers["Cache-Control"] = "no-cache"
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Permissions-Policy", "camera=(), geolocation=(), payment=()")
    # nonce é POR RESPOSTA e quem o cria é a rota do SPA (fica em request.state);
    # nas outras rotas ele não existe e o header sai SEM nonce. `/docs` e `/redoc`
    # usam a policy própria (CDN no style-src) em vez de ficarem sem CSP (#282).
    if request.url.path in _CSP_DOCS_PATHS:
        resp.headers.setdefault("Content-Security-Policy", _CSP_DOCS)
    else:
        resp.headers.setdefault("Content-Security-Policy",
                                _csp_com_nonce(getattr(request.state, "nonce", None)))
    if (request.headers.get("x-forwarded-proto") or request.url.scheme).lower() == "https":
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return resp

@app.get("/")
@app.get("/index.html")
def index_com_nonce(request: Request):
    """Serve o index.html com nonce no `<script>` inline (CSP sem 'unsafe-inline').

    Fica ANTES do `app.mount("/", StaticFiles(...))`: a rota tem precedência e o
    middleware de CSP lê este nonce do `request.state` para o header da resposta."""
    import secrets as _sec
    nonce = getattr(request.state, "nonce", None)
    if not nonce:
        nonce = _sec.token_urlsafe(16)
        request.state.nonce = nonce
    return HTMLResponse(_index_com_nonce(nonce))


# ---------------------------------------------------------------------------
# Modelo (carregamento preguiçoso — primeira síntese baixa/monta os pesos)
# ---------------------------------------------------------------------------

_model = None
_model_lock = threading.Lock()
# RLock: a cadeia de limpeza pode ser chamada de dentro de quem já segura a trava
_gen_lock = threading.RLock()  # geração LOCAL (MLX) não é thread-safe; serializa
import contextlib
_NO_LOCK = contextlib.nullcontext()  # remoto pode rodar concorrente (sem serializar)
_model_state = {"status": "idle", "device": None, "model": _settings["model"],
                "error": None, "progress": None, "precision": None, "path": None}


def _model_state_progress(msg):
    """Callback de progresso p/ download/montagem do modelo (common.py)."""
    _model_state.update(progress=msg)


def _current_backend(model: str | None = None) -> dict:
    """Metadados do backend selecionado em settings['model'].

    `model` opcional resolve SEM tocar nas settings — é o que permite validar um
    pedido com `model` no body antes de aplicá-lo globalmente."""
    return resolve_backend(model or _settings.get("model") or "omnivoice")


def _modelo_no_catalogo(modelo: str) -> bool:
    """O id é do catálogo E pode ser usado por chave `use` (#787/#849).

    Família que executa código remoto (`trust_remote_code`) só entra se tiver
    `revision` fixa: sem sha o artefato anda e o "catálogo" deixaria de significar
    "só o que o dono fixou". Entrada marcada `admin_only` também fica de fora."""
    chave = str(modelo or "").strip().lower()
    chave = _CATALOGO_ALIASES.get(chave, chave)
    meta = _CATALOGO_BACKENDS.get(chave)
    if not meta or meta.get("admin_only"):
        return False
    if meta.get("family") == "lfm3_audio" and not (meta.get("revision") or "").strip():
        return False
    return True


def _is_omnivoice() -> bool:
    return _current_backend()["family"] == "omnivoice"


def _resolve_model_path(model: str | None = None) -> str:
    """Resolve o modelo efetivo → path/id p/ load_model.

    OmniVoice: monta bf16 local (ou repo fp32). Outros atalhos: repo MLX do catálogo.
    String livre (HF/dir): repassada como está. `model` opcional = modelo do PEDIDO,
    sem depender de `_settings` (usado quando quem pediu não pode trocar o global)."""
    be = _current_backend(model)
    if be["family"] == "omnivoice" and (
            be["is_shortcut"] or str(be["path"]).strip().lower() in OMNI_ALIASES):
        return resolve_omni_source(_settings, BASE, progress=_model_state_progress)
    return be["path"]


def _quantize_backbone(model, bits: int):
    """Quantiza in-place SÓ o backbone (transformer) p/ q8/q4 — reduz RAM e acelera
    matmul. NÃO toca no audio_tokenizer (codec, preserva timbre) nem nos audio_heads
    (vocab 1025, não divisível pelo group_size). Camadas com última dim não múltipla
    de 64 são puladas pelo predicate (ficam em bf16). Só OmniVoice."""
    import mlx.nn as nn

    gs = 64

    def pred(_path, module):
        w = getattr(module, "weight", None)
        return w is not None and w.ndim >= 2 and w.shape[-1] % gs == 0

    if not hasattr(model, "backbone"):
        return
    nn.quantize(model.backbone, group_size=gs, bits=bits, class_predicate=pred)


def _get_model(model: str | None = None):
    """Modelo carregado para `model` (None = o das settings).

    A checagem de cache compara com o modelo EFETIVO do pedido: um cliente que manda
    `model` no body (e não pode trocar o global) carrega o modelo dele, e o próximo
    pedido com outro modelo recarrega — serializado pelo `_gen_lock`."""
    global _model
    modelo = model or _settings["model"]
    with _model_lock:
        prec = str(_settings.get("omni_precision", "bf16")).lower()
        be = _current_backend(modelo)
        if (_model is not None and _model_state.get("model") == modelo
                and _model_state.get("precision") == prec
                and _model_state.get("family") == be["family"]):
            _touch_use("tts")
            return _model
        # troca de modelo/precisão: libera o anterior com agressividade (Metal)
        old = _model
        _model = None
        _conds_cache.clear()
        _cache_limpa()          # modelo/precisão novos: áudio antigo não serve
        path = _resolve_model_path(modelo)
        label = be["meta"].get("label") or path
        _model_state.update(
            status="loading", device="mlx", model=modelo,
            precision=prec, family=be["family"], path=path,
            progress=f"carregando {label}…",
            backend_id=be.get("id"), backend_label=be["meta"].get("label"),
        )
        try:
            del old
            _release_mlx_memory(aggressive=True)
            _model = load_tts_model(path, be["family"],
                                    (be.get("meta") or {}).get("revision"))
            # quantização in-place só faz sentido no OmniVoice (backbone próprio)
            if be["family"] == "omnivoice" and prec in ("q8", "q4"):
                import mlx.core as mx
                _model_state.update(progress=f"quantizando backbone p/ {prec}…")
                _quantize_backbone(_model, 8 if prec == "q8" else 4)
                mx.eval(_model.parameters())
            _model_state.update(status="ready", error=None, progress=None)
            _touch_use("tts")
            return _model
        except Exception as exc:  # noqa: BLE001
            _model = None
            _model_state.update(status="error", error=str(exc), progress=None)
            raise


# ref_tokens por voz custam ~1,5s para preparar; cache LRU evita repetir.
# Chave inclui mtime (regravação invalida a voz). Cache pequeno: cada cond
# guarda tensores MLX (prompt acústico+semântico) que ficam na RAM/Metal.
_conds_cache: "OrderedDict[tuple, object]" = OrderedDict()
_CONDS_CACHE_MAX = 4

# Último uso de cada motor (timestamp) — idle unload descarrega o ocioso.
_last_use = {"tts": 0.0, "stt": 0.0, "mt": 0.0, "ser": 0.0}
# Relógio SÓ da transcrição de arquivo quando o repo não é o do Live (#891).
# Um turno do Live não mexe aqui: senão o turbo renovaria o large-v3 para sempre.
_whisper_arquivo_uso = 0.0


def _touch_use(*keys: str):
    now = time.time()
    for k in keys:
        _last_use[k] = now


def _cond_for(model, voice_id: str, voice_path: Path):
    ref_max = _clamp(_settings["omni_ref_max_s"], 3.0, 30.0, OMNI_REF_MAX_S)
    key = (voice_id, voice_path.stat().st_mtime_ns, round(ref_max, 1))
    cached = _conds_cache.get(key)
    if cached is not None:
        _conds_cache.move_to_end(key)
        return cached

    # ref_tokens (acústico + semântico) da amostra; reusados em toda geração.
    # ref_text=None aqui mantém a amostra curta (corta só acima de 20s) — ref curta
    # clona melhor e mais rápido. A transcrição da voz vai ao generate() (ref_text),
    # que é onde de fato melhora a clonagem.
    from mlx_audio.tts.models.omnivoice.utils import create_voice_clone_prompt

    cond = create_voice_clone_prompt(
        str(voice_path), ref_text=None,
        tokenizer=model.audio_tokenizer, max_duration_s=ref_max,
    )
    _conds_cache[key] = cond
    while len(_conds_cache) > _CONDS_CACHE_MAX:
        _conds_cache.popitem(last=False)
    return cond


def _generate_chunk(model, text: str, language: str, conds, ref_text, omni: dict,
                    ref_audio: str | None = None, family: str | None = None,
                    meta: dict | None = None, sr: int | None = None):
    """Gera um trecho com o adapter da família do backend ativo."""
    o = omni or {}
    be_family = family or _current_backend()["family"]
    be_meta = meta if meta is not None else _current_backend().get("meta") or {}
    # OmniVoice: passa o language cru (generate_with_backend resolve "None"/códigos).
    # Outros: passa o valor da UI (pt/en/auto).
    audio = generate_with_backend(
        model, be_family, text,
        language=language,
        ref_audio=ref_audio,
        ref_text=ref_text,
        ref_tokens=conds,
        omni=o,
        meta=be_meta,
    )
    # velocidade: time-stretch só se o backend NÃO aplicou speed nativo
    # (senão fish/chatterbox/qwen ficavam com velocidade²). Com sr, usa o
    # atempo do ffmpeg (qualidade >> phase vocoder em fala).
    speed = float(o.get("speed") or 1.0)
    if abs(speed - 1.0) > 1e-3 and be_family not in NATIVE_SPEED_FAMILIES:
        audio = _time_stretch(audio, speed, sr)
    return audio


def _denoise_audio(audio, sr: int, strength: float = 0.7):
    """Limpa ruído de fundo estacionário (hiss/zumbido/AC) da amostra de voz por
    spectral gating: estima o perfil de ruído nos quadros mais silenciosos e
    subtrai por banda, com piso e suavização p/ evitar 'musical noise'. Passa-alta
    em 70 Hz tira rumble. strength 0..1 = agressividade. numpy/scipy, sem deps."""
    import numpy as np
    from scipy.ndimage import uniform_filter
    from scipy.signal import butter, istft, sosfilt, stft

    x = np.asarray(audio, dtype=np.float32)
    if x.ndim > 1:
        x = x.mean(axis=1)
    if x.size < sr // 5:                       # < 0.2s: nada a fazer
        return x
    s = float(max(0.0, min(1.0, strength)))
    if s <= 0.0:
        return x

    # passa-alta 70 Hz (rumble/AC) antes da subtração espectral
    sos = butter(2, 70.0 / (sr / 2), btype="high", output="sos")
    x = sosfilt(sos, x).astype(np.float32)

    nperseg = 1024
    nover = nperseg * 3 // 4
    f, t, Z = stft(x, fs=sr, nperseg=nperseg, noverlap=nover)
    mag, phase = np.abs(Z), np.angle(Z)

    # quadros mais silenciosos (20% de menor energia) = estimativa do ruído por banda
    energy = mag.mean(axis=0)
    cut = np.percentile(energy, 20)
    noise = mag[:, energy <= cut]
    if noise.shape[1] < 4:
        noise = mag
    n_mean = noise.mean(axis=1, keepdims=True)
    n_std = noise.std(axis=1, keepdims=True)

    beta = 1.0 + 1.6 * s                        # sobre-subtração 1.0..2.6
    floor = 0.18 * (1.0 - s) + 0.04             # piso residual 0.22..0.04
    n_est = n_mean + 1.5 * n_std
    gain = 1.0 - beta * n_est / (mag + 1e-8)
    gain = np.clip(gain, floor, 1.0)
    gain = uniform_filter(gain, size=(2, 3))    # suaviza em freq/tempo

    Z2 = gain * mag * np.exp(1j * phase)
    _, y = istft(Z2, fs=sr, nperseg=nperseg, noverlap=nover)
    y = np.asarray(y, dtype=np.float32)
    # o `istft` (center=True) devolve ~nperseg-noverlap amostras A MAIS: o vetor
    # crescia a cada chamada e o mesmo arquivo "crescia" sendo limpo de novo
    # (#324). Corta de volta no tamanho de entrada.
    if y.size > x.size:
        y = y[:x.size]
    elif y.size < x.size:
        y = np.pad(y, (0, x.size - y.size))

    # gating espectral tira NÍVEL junto com o ruído; devolve o RMS de entrada
    # (dentro de ±3 dB), senão strength alto entrega uma referência muda e o
    # clone herda o volume (#324).
    rms_in = float(np.sqrt(np.mean(x ** 2)))
    rms_out = float(np.sqrt(np.mean(y ** 2)))
    if rms_in > 1e-6 and rms_out > 1e-6:
        ganho = rms_in / rms_out
        ganho = min(ganho, 10.0 ** (12.0 / 20.0))    # teto de segurança (12 dB)
        y *= ganho

    peak = float(np.abs(y).max() or 0.0)
    if peak > 0.99:                             # evita clip pós-processo
        y *= 0.99 / peak
    return y


#: DFN3 em ONNX (#523) — o vencedor do R1 para AMOSTRA. Mesmo contrato do
#: `models/README.md`; sem o arquivo a limpeza cai no gating de sempre.
_DFN3_DIR = _caminho_estado_env("TTS_DFN3_DIR", BASE / "models" / "dfn3-onnx")
_DFN3_ONNX = _DFN3_DIR / "deepfilter.onnx"
_modulo_dfn = None


def _dfn3_disponivel() -> bool:
    return _DFN3_ONNX.is_file()


def _carrega_modulo_dfn():
    """Importa `tools/bakeoff/dfn_onnx.py` — o DSP em volta do grafo mora lá."""
    global _modulo_dfn
    if _modulo_dfn is not None:
        return _modulo_dfn
    import importlib.util
    import os

    # o módulo resolve o diretório do modelo por env; ele é importado UMA vez
    os.environ.setdefault("TTS_DFN3_DIR", str(_DFN3_DIR))
    caminho = BASE / "tools" / "bakeoff" / "dfn_onnx.py"
    especificacao = importlib.util.spec_from_file_location("dfn_onnx_app", caminho)
    if especificacao is None or especificacao.loader is None:
        raise ImportError(f"não achei {caminho}")
    modulo = importlib.util.module_from_spec(especificacao)
    especificacao.loader.exec_module(modulo)
    _modulo_dfn = modulo
    return modulo


def _dfn3_limpa(audio, sr: int):
    """Limpa com o DeepFilterNet3 (48 kHz por dentro). None quando indisponível.

    ONNX no processo: não usa Metal nem torch, então não precisa do worker
    isolado (que existe para o segfault do MLX)."""
    import numpy as np
    from scipy.signal import resample_poly

    if not _dfn3_disponivel():
        return None
    try:
        modulo = _carrega_modulo_dfn()
        x = np.asarray(audio, dtype=np.float32).reshape(-1)
        x48 = resample_poly(x, 48000, sr).astype(np.float32) if sr != 48000 else x
        limpo = np.asarray(modulo.limpa(x48), dtype=np.float32)
        if sr != 48000:
            limpo = resample_poly(limpo, sr, 48000).astype(np.float32)
        return limpo[: x.size]
    except Exception as erro:  # noqa: BLE001
        print(f"[voice] DFN3 indisponível ({erro}) — caindo no gating", flush=True)
        return None


#: pesos do Demucs (MIT). Fora do diretório, a etapa degrade e diz o motivo —
#: NUNCA baixa: nada sai da máquina por padrão.
_DEMUCS_DIR = _caminho_estado_env("TTS_DEMUCS_DIR", BASE / "models" / "demucs")
_MUSICA_MOTIVO = ""
#: teto do worker de música. Filho mudo segurava o `_gen_lock` para sempre (#924).
_MUSICA_TIMEOUT_S = float(os.environ.get("TTS_MUSICA_TIMEOUT_S", "120"))


def _musica_motivo() -> str:
    """Por que a etapa de música não rodou (vazio = rodou ou nem foi pedida)."""
    return _MUSICA_MOTIVO


def _separa_musica(audio, sr: int):
    """Tira música/instrumental antes de limpar (import de YouTube). None quando
    o Demucs não está disponível — a etapa é opcional e some calada, mas o
    relatório registra que não rodou.

    Roda em WORKER (`musica_worker.py`): o Demucs é torch e o app é MLX; no mesmo
    processo as duas pilhas de Metal se misturam, e o pai só lê o wav pronto. O
    filho usa MPS, então a trava fica em volta do `subprocess.run` — soltar,
    como a marca (CPU), poria as duas pilhas juntas. O `timeout` é o que devolve
    a trava: filho mudo morre e a limpeza segue sem separar (#924)."""
    import numpy as np

    try:
        __import__("demucs")            # só a presença do pacote decide a etapa
    except Exception:  # noqa: BLE001
        _MUSICA_MOTIVO = "pacote demucs ausente"
        globals()["_MUSICA_MOTIVO"] = _MUSICA_MOTIVO
        return None
    # pesos LOCAIS: a etapa NUNCA baixa (nada sai da máquina por padrão)
    pesos = _DEMUCS_DIR
    if not (pesos / "htdemucs.yaml").is_file():
        globals()["_MUSICA_MOTIVO"] = f"pesos ausentes em {pesos}"
        return None
    try:
        import subprocess

        import soundfile as sf
        from scipy.signal import resample_poly

        with tempfile.TemporaryDirectory(prefix="musica-") as pasta:
            entrada = Path(pasta) / "in.wav"
            saida = Path(pasta) / "voz.wav"
            sf.write(str(entrada), np.asarray(audio, dtype=np.float32), sr, subtype="PCM_16")
            # MESMA exclusividade de Metal do TTS e do separador de voz: o worker
            # usa MPS. A trava fica no PAI, em volta da chamada — nunca no filho.
            # O timeout mata o filho mudo; sem ele o `_gen_lock` não voltava (#924).
            with _gen_lock:
                try:
                    r = subprocess.run(
                        [sys.executable, str(BASE / "musica_worker.py"),
                         str(entrada), str(saida)],
                        capture_output=True, timeout=_MUSICA_TIMEOUT_S,
                        env={**os.environ, "TTS_DEMUCS_DIR": str(pesos),
                             "HF_HUB_OFFLINE": "1"})
                except subprocess.TimeoutExpired:
                    motivo = f"filho mudo não respondeu em {_MUSICA_TIMEOUT_S:g}s"
                    globals()["_MUSICA_MOTIVO"] = motivo
                    print(
                        "[voice] separação de música: filho mudo "
                        f"não respondeu em {_MUSICA_TIMEOUT_S:g}s; "
                        "matei e o áudio segue sem separação",
                        flush=True,
                    )
                    return None
            if r.returncode != 0 or not saida.exists():
                motivo = (r.stderr or b"").decode().strip()[-160:] or "worker falhou"
                globals()["_MUSICA_MOTIVO"] = motivo
                print(f"[voice] separação de música falhou: {motivo}", flush=True)
                return None
            x, sr2 = sf.read(str(saida), dtype="float32")
            if sr2 != sr:
                x = resample_poly(x, sr, sr2).astype(np.float32)
            return np.asarray(x, dtype=np.float32)[: np.asarray(audio).size]
    except Exception as erro:  # noqa: BLE001
        globals()["_MUSICA_MOTIVO"] = str(erro)[:160]
        print(f"[voice] separação de música falhou ({erro})", flush=True)
        return None


def _dereverb(audio, sr: int):
    """Dereverb opcional. Sem modelo instalado devolve None (a etapa não existe
    hoje; o gancho fica para o WPE quando entrar)."""
    return None


def _cosseno(a, b) -> float:
    import numpy as np

    x = np.asarray(a, dtype=np.float64).reshape(-1)
    y = np.asarray(b, dtype=np.float64).reshape(-1)
    nx, ny = float(np.linalg.norm(x)), float(np.linalg.norm(y))
    if not nx or not ny:
        return 0.0
    return float(x @ y / (nx * ny))


#: DNSMOS P.835 offline (MIT, Microsoft) materializado pelo infra em `models/dnsmos`.
#: Executado por `onnxruntime` — o pacote `speechmos` NÃO é dependência (#655).
_DNSMOS_DIR = _caminho_estado_env("TTS_DNSMOS_DIR", BASE / "models" / "dnsmos")
_dnsmos_sessoes: dict = {}
DNSMOS_SR = 16000
_DNSMOS_TRECHO_S = 9.01        # janela que o modelo espera, do código de referência


def _dnsmos_disponivel() -> bool:
    return (_DNSMOS_DIR / "sig_bak_ovr.onnx").is_file() and \
        (_DNSMOS_DIR / "model_v8.onnx").is_file()


def _dnsmos_sessao(nome: str):
    """Sessão ONNX por arquivo, carregada uma vez e cacheada."""
    if nome not in _dnsmos_sessoes:
        import onnxruntime as ort

        _dnsmos_sessoes[nome] = ort.InferenceSession(
            str(_DNSMOS_DIR / nome), providers=["CPUExecutionProvider"])
    return _dnsmos_sessoes[nome]


def _dnsmos(audio, sr: int) -> dict | None:
    """DNSMOS P.835 do trecho: `{ovrl, sig, bak, p808}`. None se faltar modelo.

    Reimplementa o `speechmos` (mesmo modelo, mesmo pré-processamento, mesmos
    polinômios de correção) sem o pacote: o infra materializou os ONNX em
    `models/dnsmos` justamente para o número existir offline (#655). O áudio é
    reamostrado para 16 kHz e repetido até completar a janela de 9,01 s; a nota
    final é a média dos trechos de 1 s, como no original."""
    import numpy as np

    if not _dnsmos_disponivel():
        return None
    try:
        import librosa

        x = np.asarray(audio, dtype=np.float32).reshape(-1)
        if sr != DNSMOS_SR:
            x = librosa.resample(x, orig_sr=sr, target_sr=DNSMOS_SR)
        if not x.size or not np.isfinite(x).all():
            return None
        pico = float(np.abs(x).max())
        if pico > 1.0:                     # a entrada do modelo é [-1, 1]
            x = x / pico
        n = int(_DNSMOS_TRECHO_S * DNSMOS_SR)
        while x.size < n:
            x = np.append(x, x)
        saltos = int(np.floor(x.size / DNSMOS_SR) - _DNSMOS_TRECHO_S) + 1
        sig, bak, ovr, p808 = [], [], [], []
        prim = _dnsmos_sessao("sig_bak_ovr.onnx")
        v8 = _dnsmos_sessao("model_v8.onnx")
        for i in range(saltos):
            trecho = x[i * DNSMOS_SR: i * DNSMOS_SR + n]
            if trecho.size < n:
                continue
            mel = librosa.feature.melspectrogram(
                y=trecho[:-160], sr=DNSMOS_SR, n_fft=321, hop_length=160, n_mels=120)
            mel = (librosa.power_to_db(mel, ref=np.max) + 40) / 40
            p808.append(float(v8.run(None, {"input_1": mel.T.astype("float32")[None]})[0][0][0]))
            cru = prim.run(None, {"input_1": trecho.astype("float32")[None]})[0][0]
            sig.append(float(np.poly1d([-0.08397278, 1.22083953, 0.0052439])(cru[0])))
            bak.append(float(np.poly1d([-0.13166888, 1.60915514, -0.39604546])(cru[1])))
            ovr.append(float(np.poly1d([-0.06766283, 1.11546468, 0.04602535])(cru[2])))
        if not ovr:
            return None
        return {"ovrl": round(float(np.mean(ovr)), 3), "sig": round(float(np.mean(sig)), 3),
                "bak": round(float(np.mean(bak)), 3), "p808": round(float(np.mean(p808)), 3)}
    except Exception as erro:  # noqa: BLE001
        print(f"[voice] DNSMOS falhou ({erro}) — segue sem a nota", flush=True)
        return None


def _guarda_timbre(antes, depois, sr: int) -> float | None:
    """Similaridade do locutor antes x depois. None se o extrator não carregar.

    O denoiser pode entregar um áudio limpo que já não é a mesma pessoa; a amostra
    é o que o clone copia, então trocar a voz custa mais que o ruído restante."""
    import numpy as np
    import soundfile as sf

    try:
        with tempfile.TemporaryDirectory(prefix="timbre-") as pasta:
            pa = Path(pasta) / "antes.wav"
            pb = Path(pasta) / "depois.wav"
            sf.write(str(pa), np.asarray(antes, dtype=np.float32), sr, subtype="PCM_16")
            sf.write(str(pb), np.asarray(depois, dtype=np.float32), sr, subtype="PCM_16")
            ea = _speaker_embed(pa)
            eb = _speaker_embed(pb)
    except Exception:  # noqa: BLE001
        return None
    if ea is None or eb is None:
        return None
    return _cosseno(ea, eb)


def _denoise_para_referencia(audio, sr: int, strength: float):
    """Limpeza da AMOSTRA DE VOZ com as guardas de integridade (#324, #523).

    Devolve `(audio, recusado, relatorio)`. Recusa — mantendo o original — quando
    a limpeza derrubou o nível mais de 20 dB (referência muda que o clone copia) ou
    quando o TIMBRE caiu abaixo de `voice_timbre_min` (a limpeza entregou outra
    pessoa). O motor vem de `voice_limpar_motor`: o DFN3 do R1 quando o modelo
    está instalado, senão o gating espectral de sempre.
    """
    import numpy as np

    # `original` NUNCA é tocado: é ele que volta na recusa e é nele que o DNSMOS
    # "antes" é medido. `antes` é a cópia de trabalho e pode ter passado por
    # música/dereverb — devolver essa na recusa daria um áudio já mexido com o
    # rótulo de "original" (achado P3 do gate).
    original = np.asarray(audio, dtype=np.float32)
    antes = original
    relatorio = {"motor": "gating", "timbre": None, "recusado_por": None,
                 "musica": False, "musica_motivo": "", "dereverb": False,
                 "dnsmos_antes": None, "dnsmos_depois": None}
    # etapas opcionais, nesta ordem: música -> dereverb -> denoise
    if _settings.get("voice_limpar_musica"):
        sem_musica = _separa_musica(antes, sr)
        if sem_musica is not None:
            antes = sem_musica
            relatorio["musica"] = True
        else:
            relatorio["musica_motivo"] = _musica_motivo() or "indisponivel"
    if _settings.get("voice_limpar_dereverb"):
        seco = _dereverb(antes, sr)
        if seco is not None:
            antes = seco
            relatorio["dereverb"] = True
    limpo = None
    if str(_settings.get("voice_limpar_motor", "dfn3")).lower() == "dfn3":
        limpo = _dfn3_limpa(antes, sr)
        if limpo is not None:
            relatorio["motor"] = "dfn3"
    if limpo is None:
        limpo = _denoise_audio(antes, sr, strength)

    rms_antes = float(np.sqrt(np.mean(original ** 2)))
    rms_depois = float(np.sqrt(np.mean(limpo ** 2)))
    if rms_antes > 1e-6 and (rms_depois <= 1e-6 or rms_depois < rms_antes / 10.0):
        print(f"[voice] denoise derrubou o nível (rms {rms_antes:.4f} -> "
              f"{rms_depois:.4f}) — mantendo a amostra sem limpeza", flush=True)
        relatorio["recusado_por"] = "nivel"
        return original, True, relatorio

    # medidas antes/depois: o dono vê o que ganhou e o que a voz custou
    relatorio["dnsmos_antes"] = _dnsmos(original, sr)
    relatorio["dnsmos_depois"] = _dnsmos(limpo, sr)

    # guarda de timbre: só faz sentido depois que a limpeza produziu áudio
    timbre = _guarda_timbre(original, limpo, sr)
    relatorio["timbre"] = None if timbre is None else round(timbre, 4)
    if timbre is not None:
        minimo = float(_settings.get("voice_timbre_min", 0.80))
        if timbre < minimo:
            print(f"[voice] limpeza trocou o timbre (similaridade {timbre:.3f} < "
                  f"{minimo:.2f}) — mantendo a amostra sem limpeza", flush=True)
            relatorio["recusado_por"] = "timbre"
            return original, True, relatorio
    return limpo, False, relatorio


def _referencia_pronta(audio, sr: int):
    """Nível e bordas da amostra de referência antes de gravar (clone melhor)."""
    return _fade_edges(_normalize(audio), sr)


def _anomalo(audio, sr: int, chunk: str, speed: float = 1.0) -> bool:
    """Geração inaceitável (o motivo fica em `_motivo_anomalia`)."""
    return bool(_motivo_anomalia(audio, sr, chunk, speed))


def _apply_audio_fx(audio, sr: int):
    """EQ/ganho das settings (ver common.apply_audio_fx) — worker e remoto
    recebem os mesmos valores via cfg/omni."""
    return apply_audio_fx(
        audio, sr,
        g_low=float(_settings.get("audio_eq_low_db", 0.0)),
        g_mid=float(_settings.get("audio_eq_mid_db", 0.0)),
        g_high=float(_settings.get("audio_eq_high_db", 0.0)),
        gain_db=float(_settings.get("audio_gain_db", 0.0)),
    )


def _metricas_audio(caminho: Path) -> dict:
    """RMS, pico e crista (dB) do arquivo final. Arquivo ilegível = zeros."""
    import numpy as np
    import soundfile as sf

    try:
        y, _ = sf.read(str(caminho), dtype="float32")
    except Exception:  # noqa: BLE001 — métrica é registro, não bloqueia
        # zeros são "arquivo mudo" na leitura — ilegível é OUTRO motivo (#439)
        return {"rms": 0.0, "peak": 0.0, "crest_db": 0.0, "ilegivel": True}
    y = np.asarray(y, dtype=np.float64)
    rms = float(np.sqrt(np.mean(y ** 2))) if y.size else 0.0
    pico = float(np.abs(y).max()) if y.size else 0.0
    crista = 20.0 * float(np.log10(pico / rms)) if rms > 1e-9 and pico > 0 else 0.0
    return {"rms": rms, "peak": pico, "crest_db": crista, "ilegivel": False}


def _cache_audio():
    """Cache de áudio ligado? None = desligado (padrão) ou sem o módulo."""
    if _tts_cache is None or not _tts_cache.ligado(_settings):
        return None
    return _tts_cache.Cache(OUTPUTS_DIR / ".cache-tts")


def _cache_chave(chunk: str, *, voice_id: str, voice_path: Path, family: str,
                 model: str, language: str, alvo_remoto: bool, omni: dict) -> str:
    try:
        mtime = voice_path.stat().st_mtime_ns
    except OSError:
        mtime = 0
    # EQ/ganho entram depois da geração, mas mudam o áudio: vão na chave
    params = {k: _settings.get(k) for k in (
        "audio_eq_low_db", "audio_eq_mid_db", "audio_eq_high_db", "audio_gain_db")}
    return _tts_cache.chave(
        texto=chunk, voice_id=voice_id, voice_mtime=mtime, family=family,
        model=str(_settings.get("model") or ""), precision=str(_settings.get("omni_precision") or ""),
        language=language, motor="remoto" if alvo_remoto else "local",
        omni={**omni, **params})


def _cache_limpa() -> int:
    """Invalida o cache inteiro nos mesmos pontos que limpam `_conds_cache`."""
    if _tts_cache is None:
        return 0
    return _tts_cache.Cache(OUTPUTS_DIR / ".cache-tts").limpa()


def _pos_processa(audio, sr: int, pad_s: float = PAD_CAUDA_S):
    """Pós de UM trecho, num lugar só: trim → normalize → EQ/ganho → fade.

    Serve o job (lote e remoto) E o Live. Antes cada caminho tinha a sua — o Live,
    nenhuma: o mesmo texto saía 2-3 dB mais baixo lá, com clique nas bordas e
    áudio mudo aceito como bom (#305).

    `pad_s` é a cauda que o trim deixa para o fade não comer a fala (padrão de
    80 ms desde o #319: a pausa de verdade agora vem da pontuação, inserida pelo
    chamador). O Live passa 0,15 s, que é a pausa dele — lá não há inserção.
    """
    return _pos_processa_com_ganho(audio, sr, pad_s)[0]


def _pos_processa_com_ganho(audio, sr: int, pad_s: float = PAD_CAUDA_S):
    """Igual ao `_pos_processa`, mas devolve `(áudio, ganho_db)`.

    O ganho é o que a normalização REALMENTE aplicou ao trecho: com o RMS final
    sempre no alvo e o job ancorado em LUFS, o `rms` do meta não enxerga fonte
    baixa — quem enxerga é este número (#439).
    """
    import numpy as np

    antes = np.asarray(audio, dtype=np.float32)
    rms_antes = float(np.sqrt(np.mean(antes.astype(np.float64) ** 2))) if antes.size else 0.0
    y = _fade_edges(
        _apply_audio_fx(_normalize(_trim_tail_silence(audio, sr, pad_s=pad_s)), sr), sr)
    rms_depois = float(np.sqrt(np.mean(np.asarray(y, dtype=np.float64) ** 2))) if y.size else 0.0
    ganho = 20.0 * float(np.log10(rms_depois / rms_antes)) if rms_antes > 1e-9 else 0.0
    return y, ganho



def _wav_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as w:
            return round(w.getnframes() / w.getframerate(), 1)
    except Exception:  # noqa: BLE001
        return 0.0


# ---------------------------------------------------------------------------
# Vozes: voices/<id>.wav + voices/<id>.json
# ---------------------------------------------------------------------------


@app.get("/health")
def health():
    """Liveness p/ monitoramento externo — sem auth, sem detalhe interno.

    `boot_ts` e `rev` existem porque um 200 genérico não distingue "de pé" de "de
    pé no código de ontem": o probe guarda o `boot_ts` que viu (reinício aparece
    como mudança, instância travada não) e `rev` diz em qual commit. O
    fingerprint `codigo` continua só no `/api/build` (autenticado).

    Continua síncrona de propósito (#915): no pool do anyio, um /health async
    responderia com os workers presos e o monitor acharia o processo vivo.
    """
    # None em produção. A célula do watchdog pendura o worker AQUI para a pilha
    # despejada nomear esta função — tirar o gancho faz o teste morrer.
    gancho = _SAUDE_GANCHO
    if gancho is not None:
        gancho()
    return {"ok": True, "boot_ts": int(_BUILD_TS), "rev": _VERSION}


# ---------------------------------------------------------------------------
# Watchdog do /health (#915). O sample do pid 88948 (08:24) mostrou o loop no
# kevent, 39 dos 40 workers do anyio parados em rlock_acquire e uma thread
# Python girando em lookup de dict com a GIL. O probe síncrono não tem worker
# e estoura os 5 s; um timer DENTRO de health() não arma, porque a rota nem
# começa. O relógio fica no ASGI, antes do salto para o pool.
#
# Um vigia em thread Python não despeja se a GIL está presa numa chamada C
# (medido: 705 s sem uma linha). O prazo vive na thread C do faulthandler
# (`dump_traceback_later`), que não precisa da GIL para escrever. O laço e
# cada /health cancelam e armam de novo: se nenhum dos dois roda, o prazo
# estoura e o arquivo nasce.
# ---------------------------------------------------------------------------
class _SaudeEmVoo:
    """Pedidos de /health que entraram no ASGI e ainda não responderam."""

    def __init__(self) -> None:
        self._trava = threading.Lock()
        self._em_voo: dict[int, float] = {}
        self._seq = 0
        self._ultimo_despejo = 0.0

    def entra(self) -> int:
        with self._trava:
            self._seq += 1
            token = self._seq
            self._em_voo[token] = time.monotonic()
            return token

    def sai(self, token: int) -> None:
        with self._trava:
            self._em_voo.pop(token, None)
            if not self._em_voo:
                # o intervalo cobre o MESMO pedido preso; o próximo hang despeja já
                self._ultimo_despejo = 0.0

    def atraso(self) -> float:
        with self._trava:
            if not self._em_voo:
                return 0.0
            agora = time.monotonic()
            return max(agora - inicio for inicio in self._em_voo.values())

    def reserva_despejo(self, intervalo: float) -> bool:
        """True no máximo uma vez por `intervalo` enquanto houver pedido preso."""
        with self._trava:
            agora = time.monotonic()
            if agora - self._ultimo_despejo < intervalo:
                return False
            self._ultimo_despejo = agora
            return True


def _saude_float(nome: str, padrao: float) -> float:
    bruto = os.environ.get(nome)
    if not bruto:
        return padrao
    try:
        return float(bruto)
    except ValueError:
        return padrao


# Lidos a cada passo do laço, não congelados no import: a célula baixa o teto
# sem reimportar o módulo. 3 s fica abaixo do timeout de 5 s do probe.
_SAUDE_TRAVADO_S = _saude_float("TTS_HEALTH_TRAVADO_S", 3.0)
_SAUDE_DESPEJO_INTERVALO_S = _saude_float("TTS_HEALTH_DESPEJO_S", 30.0)
# Fora do estado do dono e fora do repo: a pilha não leva local (o faulthandler
# não imprime variável) e o arquivo não mistura com settings.
_SAUDE_WATCHDOG_ARQ = os.environ.get("TTS_WATCHDOG_LOG") or str(
    Path("/tmp") / "tts-watchdog.log")
_SAUDE_GANCHO = None
_saude_voo = _SaudeEmVoo()


def _saude_e_health(scope) -> bool:
    """Só o liveness. Casa o path cru e o mesmo path com o prefixo do proxy.

    Sem `endswith`: `/api/health` e `/healthz` não são o probe. Sem normalizar
    `..` — quem reescreve o path é o middleware de prefixo, mais para dentro.
    """
    if scope.get("type") != "http":
        return False
    caminho = scope.get("path") or "/"
    if caminho == "/health":
        return True
    base = _BASE_PATH
    return bool(base) and caminho == f"{base}/health"


class _SaudeWatchdog:
    """ASGI puro. Anota /health e segue; não lê o corpo nem responde no lugar."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if not _saude_e_health(scope):
            await self.app(scope, receive, send)
            return
        token = _saude_voo.entra()
        try:
            # Cada /health rearma o prazo C. Se a rota não volta e o laço
            # também não roda, ninguém cancela e o faulthandler despeja.
            try:
                _saude_rearma()
            except Exception:  # noqa: BLE001
                pass
            await self.app(scope, receive, send)
        finally:
            _saude_voo.sai(token)
            try:
                _saude_rearma()
            except Exception:  # noqa: BLE001
                pass


def _saude_despeja(atraso: float) -> None:
    """Cabeçalho + pilhas de todas as threads. Sem valor de variável local."""
    arq = _SAUDE_WATCHDOG_ARQ
    linha = (
        f"[watchdog] {time.strftime('%Y-%m-%dT%H:%M:%S%z')} "
        f"pid={os.getpid()} /health sem resposta ha {atraso:.1f}s\n"
    )
    try:
        with open(arq, "a", encoding="utf-8") as fh:
            fh.write(linha)
            fh.flush()
            faulthandler.dump_traceback(fh, all_threads=True)
            fh.write("\n")
            fh.flush()
        os.chmod(arq, 0o600)
    except Exception:  # noqa: BLE001 — o watchdog não pode derrubar o processo
        return
    try:
        print(
            f"[watchdog] /health sem resposta ha {atraso:.0f}s — pilhas em {arq}",
            file=sys.stderr,
            flush=True,
        )
    except Exception:  # noqa: BLE001
        pass


def _saude_watchdog_passo() -> None:
    limite = _SAUDE_TRAVADO_S
    atraso = _saude_voo.atraso()
    if atraso < limite:
        return
    if not _saude_voo.reserva_despejo(_SAUDE_DESPEJO_INTERVALO_S):
        return
    _saude_despeja(atraso)


# fd do dump_traceback_later. Tem de continuar aberto: o faulthandler guarda
# o número e escreve nele sem a GIL. Fechar reusa o número noutro arquivo.
_saude_log_fh = None
_saude_log_path = ""
_saude_arma_lock = threading.Lock()


def _saude_fh_trocou(caminho: str) -> bool:
    if (_saude_log_fh is None or _saude_log_path != caminho
            or _saude_log_fh.closed):
        return True
    try:
        no_fd = os.fstat(_saude_log_fh.fileno())
        no_path = os.stat(caminho)
    except OSError:
        return True
    return no_fd.st_ino != no_path.st_ino or no_fd.st_dev != no_path.st_dev


def _saude_garante_log():
    """Abre o log em 0600 e o segura. O modo não herda umask nem um unlink."""
    global _saude_log_fh, _saude_log_path
    caminho = _SAUDE_WATCHDOG_ARQ
    if not _saude_fh_trocou(caminho):
        try:
            os.chmod(caminho, 0o600)
        except OSError:
            pass
        return _saude_log_fh
    faulthandler.cancel_dump_traceback_later()
    if _saude_log_fh is not None:
        try:
            _saude_log_fh.close()
        except OSError:
            pass
        _saude_log_fh = None
    fd = os.open(
        caminho,
        os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_CLOEXEC,
        0o600,
    )
    try:
        os.chmod(caminho, 0o600)
    except OSError:
        pass
    _saude_log_fh = open(fd, "ab", buffering=0, closefd=True)
    _saude_log_path = caminho
    return _saude_log_fh


def _saude_rearma() -> None:
    """Cancela e arma o watchdog C com o prazo lido agora.

    Chamar de novo zera o relógio. Quem chama é o laço (heartbeat) e cada
    /health. Sem essas chamadas — GIL presa numa função C — a thread do
    faulthandler despeja sozinha, com `repeat=True`.
    """
    with _saude_arma_lock:
        timeout = _SAUDE_TRAVADO_S
        if timeout <= 0:
            faulthandler.cancel_dump_traceback_later()
            return
        fh = _saude_garante_log()
        faulthandler.cancel_dump_traceback_later()
        faulthandler.dump_traceback_later(timeout, repeat=True, file=fh)


def _loop_saude_watchdog() -> None:
    while True:
        try:
            _saude_watchdog_passo()
            _saude_rearma()
        except Exception:  # noqa: BLE001
            pass
        # Bem abaixo do prazo: um teto de teste curto (0,3 s) não pode
        # disparar o watchdog C só porque o laço dormiu 0,25 s.
        pausa = min(0.25, max(0.05, _SAUDE_TRAVADO_S / 4.0))
        time.sleep(pausa)


if not any(t.name == "health-watchdog" for t in threading.enumerate()):
    threading.Thread(
        target=_loop_saude_watchdog, name="health-watchdog", daemon=True,
    ).start()


# ---------------------------------------------------------------------------
# Build do processo VIVO (#190). O `run.sh` sobe `uvicorn` sem `--reload`, então a
# instância pode estar rodando código de dias atrás: o pin de um ticket não tinha
# como ser conferido contra ela (uma feature nova ficou INERTE no app do dono e o
# gate apontou para o alvo errado). `codigo` é o hash do CONTEÚDO dos módulos do
# servidor — o par (version, codigo) distingue "commit novo" de "código carregado".
# Fica sob `/api/` e portanto exige chave fora do loopback, como o resto.
# ---------------------------------------------------------------------------
_BUILD_MODULOS = ("app.py", "common.py", "backends.py", "tts_worker.py",
                  "live_pipeline.py", "live_turns.py", "dsh_client.py")
def _build_hash() -> str:
    """sha256-8 do conteúdo ATUAL dos módulos do servidor (lê o disco)."""
    h = hashlib.sha256()
    for nome in _BUILD_MODULOS:
        try:
            h.update((BASE / nome).read_bytes())
        except OSError:
            h.update(b"<ausente>")
    return h.hexdigest()[:8]


# CONGELADO NO IMPORT (#214): o campo promete o código CARREGADO. Calculado no 1º
# uso, uma instância que nunca serviu `/api/build` descrevia o disco do momento da
# checagem — editar um módulo sem reiniciar e chamar uma vez dava "bate" falso, com
# o processo rodando o código antigo. São 7 arquivos pequenos: mesmo custo do boot.
_BUILD_CODIGO = _build_hash()
# #220: instante EXATO do hash. Um módulo escrito DEPOIS daqui não é descrito por
# ele (o hash é do conteúdo daquele instante) — é o que o par de testes do /api/build
# usa para tolerar a árvore viva sem tolerar em silêncio.
_BUILD_TS_HASH = time.time()


@app.get("/api/build")
def build():
    """Qual código a INSTÂNCIA VIVA carregou, e há quanto tempo ela está de pé.

    Comparar `codigo` com o sha do ticket diz se a instância é a mesma que o commit;
    `boot_ms` é a idade do processo (medida do import) e `admin_fields` o tamanho do
    conjunto de campos administrativos — uma instância antiga tinha 17 campos quando
    o código já tinha 23, e `POST /api/settings` ignorava o campo novo em silêncio.

    `codigo` é o hash CONGELADO no import: comparar com o disco de agora é o que
    separa "código carregado" de "árvore editada depois" (#214)."""
    return {"ok": True, "version": _VERSION,
            "boot_ts": int(_BUILD_TS),
            "boot_ms": int((time.monotonic() - _BUILD_INI) * 1000),
            "codigo": _BUILD_CODIGO, "modulos": list(_BUILD_MODULOS),
            "admin_fields": len(_SETTINGS_ADMIN)}


def _git_version() -> str:
    """Hash curto do commit em execução — cai para 'dev' fora de um repo."""
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True,
            text=True, timeout=3, cwd=BASE,
        ).stdout.strip() or "dev"
    except Exception:  # noqa: BLE001
        return "dev"


_VERSION = _git_version()


@app.get("/api/status")
def status():
    st = dict(_model_state)
    st["version"] = _VERSION
    # `_model_state["model"]` é o ÚLTIMO modelo carregado — verdade sobre a VRAM, não
    # sobre a config: um cliente com `model` no body (chave de uso) carrega o modelo
    # dele sem trocar settings, então o painel passava a mostrar como "config" algo
    # que era um pedido. Aqui vai o valor configurado, ao lado, para desambiguar.
    st["model_settings"] = _settings.get("model")
    try:
        be = _current_backend()
        st.setdefault("family", be["family"])
        st.setdefault("backend_id", be["id"])
        st["backend_label"] = be["meta"].get("label")
    except Exception:  # noqa: BLE001
        pass
    # fila de falas: útil p/ clientes e UI verem se há espera
    st["speech_queue"] = bool(_settings.get("speech_queue"))
    st["speech_queue_depth"] = _speech_gate.depth if st["speech_queue"] else 0
    st["speech_queue_free_in"] = round(_speech_gate.free_in, 1) if st["speech_queue"] else 0.0
    st["speech_queue_last_duration"] = (
        round(_speech_gate.last_duration, 2) if st["speech_queue"] else 0.0
    )
    st["api_auth_enabled"] = _auth_enabled()
    # Admissão de jobs: sem isto um 429 por teto de ativos era indistinguível de
    # um 429 do limitador de taxa, e a UI não tinha como saber que a geração
    # esperava slot. jobs_active == jobs_active_max = próxima geração recusa.
    st["jobs_active"] = _jobs_ativos()
    st["jobs_active_max"] = _JOBS_ACTIVE_MAX
    st["jobs_history_max"] = _JOBS_MAX
    # Caminho do VAD realmente carregado ("onnx" | "torch-jit"); None = ainda não
    # carregou. Sem isto a única pista de um fallback para o torch seria o print
    # no stderr do servidor — /api/status prova em produção qual caminho está vivo.
    st["vad_backend"] = _vad_backend or None
    # Extrator de locutor em uso e, quando caiu para o legado, o motivo (#606) —
    # mesmo papel do `vad_backend`: o fallback tem de ser provado por fora, não
    # descoberto no stderr.
    st["extrator_locutor"] = _speaker_modelo_efetivo()
    # O encoder é carregado sob demanda e liberado por ociosidade (#612): estado e
    # TTL vigente ficam visíveis, senão o unload só se prova por RSS.
    st["extrator_carregado"] = _speaker_ecapa_enc is not None
    st["extrator_ttl_s"] = _SPEAKER_TTL_S
    # Tamanho do dict do rate limiter (buckets = identidade × path normalizado) e o
    # teto: era invisível e só aparecia como memória crescendo em caixa exposta.
    st["live_sessions"] = len(_live_sessions)
    st["live_historicos"] = len(_live_historico)
    st["live_historicos_max"] = _LIVE_MAX_HISTORICOS
    st["rate_limit_buckets"] = len(_rate_hits)
    st["rate_limit_buckets_max"] = _RATE_MAX_BUCKETS
    # tentativas de origem ESTRANGEIRA barradas (#282): a CSRF vira visível no
    # painel em vez de só um 403 mudo na aba alheia.
    st["origens_barradas"] = _resumo_origens_barradas()
    # processos dsh VIVOS no processo inteiro (pool + Live + descoberta) e o teto:
    # sem isto o teto seria invisível para quem opera (#287).
    st.update(_dsh_procs_resumo())
    with _apikeys_lock:
        st["api_keys_count"] = len(_apikeys.get("keys") or [])
    st["lan_urls"] = _lan_urls()
    return st


@app.get("/api/metrics")
def metrics(request: Request, janela_min: int = 60, rota: str = ""):
    """Histórico de operação agregado (#376) — admin, porque é diagnóstico.

    `janela_min` é a janela para trás em minutos (1..10080 = 7 dias); `rota` filtra
    uma rota normalizada. Devolve p50/p95, contagem e taxa de erro por rota, além
    do tamanho dos arquivos por dia (é o número que dimensiona o TTL).

    Sem texto e sem áudio: só metadados. O arquivo mora em `state/metrics/`, um
    JSONL por dia, podado por `metrics_ttl_dias`.
    """
    if not _admin_is_allowed(request):
        raise HTTPException(403, "Histórico é diagnóstico: exige chave administrativa")
    janela = max(1, min(int(janela_min), 10080)) * 60
    dados = _metrics.agregar(janela, (rota or "").strip() or None)
    dados["backend"] = _backend_para_historico()
    dados["retencao_dias"] = int(_settings.get("metrics_ttl_dias", 14))
    dados["teto_mb"] = int(_settings.get("metrics_teto_mb", 8))
    dados["arquivos"] = _metrics.tamanho_por_dia()
    dados["bytes_por_linha"] = _metrics.media_por_linha()
    return dados


# ---------------------------------------------------------------------------
# Gestão de chaves de API (UI: Configurações → Acesso)
# ---------------------------------------------------------------------------

@app.get("/api/apikeys")
def list_apikeys(request: Request, reveal: bool = False):
    """Lista chaves. `reveal=1` devolve o secret se for o Mac ou se a
    requisição já autenticou com uma chave válida.

    Loopback com `Origin` de terceiro não conta como "no Mac" (#282): o middleware
    já barra o `reveal` sem chave, e aqui a vista deixa de ser a de admin para a
    página de fora não receber nada em claro por acidente."""
    local = _is_local(request) and not _origem_estrangeira(request)
    tem_chave = _key_is_valid(_extract_request_key(request))
    can_reveal = bool(local) or (_admin_is_allowed(request) and tem_chave)
    do_reveal = bool(reveal) and can_reveal
    with _apikeys_lock:
        rows = [_public_key_row(k, reveal=do_reveal) for k in (_apikeys.get("keys") or [])]
        enabled = bool(_apikeys.get("enabled", True))
    env_row = None
    if _ENV_API_KEY:
        # só lista o env se não estiver já entre as chaves gerenciadas
        with _apikeys_lock:
            already = any(k.get("secret") == _ENV_API_KEY for k in (_apikeys.get("keys") or []))
        if not already:
            env_row = _public_key_row({
                "id": "__env__",
                "name": "TTS_ROD_API_KEY (ambiente)",
                "secret": _ENV_API_KEY,
                "created_at": "",
                "readonly": True,
            }, reveal=do_reveal)
    return {
        "enabled": enabled,
        "auth_active": _auth_enabled(),
        "keys": rows,
        "env_key": env_row,
        "can_reveal": can_reveal,
        "local": bool(local),
        "lan_urls": _lan_urls(),
    }


@app.post("/api/apikeys")
def create_apikey(request: Request, payload: dict):
    """Cria chave. Devolve o secret completo uma vez."""
    if not _admin_is_allowed(request):
        raise HTTPException(403, "Chave administrativa necessária")
    payload = payload or {}
    name = str(payload.get("name") or "nova chave").strip()[:64] or "nova chave"
    kid = uuid.uuid4().hex[:10]
    secret = _new_api_secret()
    row = {
        "id": kid,
        "name": name,
        "secret": secret,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        # role=use: chave que gera fala/transcreve mas não mexe em conexão externa
        # nem lê segredo. Ausente = política antiga (compatibilidade).
        "role": _normaliza_role(payload.get("role")),
    }
    with _apikeys_lock:
        _apikeys.setdefault("keys", []).append(row)
        _save_apikeys()
    global API_KEY
    API_KEY = _primary_api_key() or _ENV_API_KEY or None
    return {"ok": True, "key": _public_key_row(row, reveal=True)}


@app.patch("/api/apikeys/{key_id}")
def rename_apikey(request: Request, key_id: str, payload: dict):
    """Renomeia e/ou troca o papel da chave (admin | use | vazio = legado).

    Corpo parcial: `name` ausente OU vazio mantém o nome atual (o campo em branco
    na UI é "não mexi no nome"), `role` ausente mantém o papel. A UI manda os dois;
    a API pode mandar só um."""
    if not _admin_is_allowed(request):
        raise HTTPException(403, "Chave administrativa necessária")
    payload = payload or {}
    if key_id == "__env__":
        raise HTTPException(400, "Chave de ambiente não pode ser renomeada")
    name = str(payload.get("name") or "").strip()[:64]
    novo_role = _normaliza_role(payload["role"]) if "role" in payload else None
    with _apikeys_lock:
        for k in _apikeys.get("keys") or []:
            if k["id"] == key_id:
                if name:                    # vazio/ausente: mantém o que está
                    k["name"] = name
                if "role" in payload:
                    k["role"] = novo_role
                _save_apikeys()
                return {"ok": True, "key": _public_key_row(k)}
    raise HTTPException(404, "Chave não encontrada")


@app.post("/api/apikeys/{key_id}/rotate")
def rotate_apikey(request: Request, key_id: str):
    """Gera novo secret. A chave antiga deixa de valer na hora."""
    if not _admin_is_allowed(request):
        raise HTTPException(403, "Chave administrativa necessária")
    if key_id == "__env__":
        raise HTTPException(400, "Chave de ambiente não pode ser rotacionada pela UI")
    with _apikeys_lock:
        for k in _apikeys.get("keys") or []:
            if k["id"] == key_id:
                k["secret"] = _new_api_secret()
                k["created_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                _save_apikeys()
                global API_KEY
                API_KEY = _primary_api_key() or _ENV_API_KEY or None
                return {"ok": True, "key": _public_key_row(k, reveal=True)}
    raise HTTPException(404, "Chave não encontrada")


@app.delete("/api/apikeys/{key_id}")
def delete_apikey(request: Request, key_id: str, forcar: bool = False):
    """Apaga a chave. A ÚLTIMA pede confirmação (409) — sem chave e sem env a
    instalação fica sem autenticação nenhuma (achado P1 da auditoria #282).

    `_auth_enabled()` vira falso quando não sobra chave nem `TTS_ROD_API_KEY`/
    `TTS_ROD_ADMIN_KEY`, e aí `_exige_chave` é no-op: a LAN INTEIRA passa a
    acessar sem credencial. A resposta só dizia `auth_active:false` DEPOIS de
    apagar, então o dono descobria pelo vizinho. Agora a última remoção devolve
    409 com `forcar: true` e NÃO apaga; `?forcar=1` confirma e assume a LAN
    aberta (aí sim o corpo traz o aviso e `auth_active:false`).

    Com `TTS_ROD_ADMIN_KEY`/`TTS_ROD_API_KEY` no ambiente a auth não cai e a
    última chave gerenciada sai sem cerimônia. Com a exigência já DESLIGADA por
    `POST /api/apikeys/enabled` também: a postura aberta já era a escolhida."""
    if not _admin_is_allowed(request):
        raise HTTPException(403, "Chave administrativa necessária")
    if key_id == "__env__":
        raise HTTPException(400, "Chave de ambiente não pode ser apagada pela UI")
    with _apikeys_lock:
        keys = _apikeys.get("keys") or []
        kept = [k for k in keys if k["id"] != key_id]
        if len(kept) == len(keys):
            raise HTTPException(404, "Chave não encontrada")
        # a remoção DERRUBA a exigência de chave? (só a última, e só se nada mais
        # sustenta a auth)
        derruba = (not kept
                   and bool(_apikeys.get("enabled", True))
                   and not (_ENV_API_KEY or _ADMIN_API_KEY))
        if derruba and not forcar:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                {"detail": "Esta é a última chave: apagá-la desliga a exigência de "
                           "chave e a LAN inteira passa a acessar sem credencial",
                 "forcar": True,
                 "consequencia": "auth desligada para qualquer máquina da rede",
                 "hint": "Confirme com `?forcar=1`, ou defina TTS_ROD_ADMIN_KEY "
                         "para manter a exigência.",
                 "remaining": len(kept), "auth_active": True,
                 "auth_active_se_forcar": False},
                status_code=409)
        _apikeys["keys"] = kept
        _save_apikeys()
    global API_KEY
    API_KEY = _primary_api_key() or _ENV_API_KEY or None
    corpo = {"ok": True, "remaining": len(kept), "auth_active": _auth_enabled()}
    if derruba:
        corpo["avisos"] = ["Autenticação DESLIGADA (nenhuma chave e nenhum "
                           "TTS_ROD_*_KEY): a LAN inteira acessa sem credencial"]
        # sinal DURÁVEL: o `avisos` morre com a resposta; o log fica no journal
        print("[apikeys] ÚLTIMA chave apagada — autenticação DESLIGADA "
              "(a LAN inteira acessa sem credencial)", file=sys.stderr, flush=True)
    return corpo


@app.post("/api/apikeys/enabled")
def set_apikeys_enabled(request: Request, payload: dict):
    """Liga/desliga a exigência de chave na rede.

    Desligar é intencional (não pede 409), mas o corpo AVISA em `avisos` que a
    LAN ficou aberta — mesmo motivo do 409 da última chave (#282)."""
    if not _admin_is_allowed(request):
        raise HTTPException(403, "Chave administrativa necessária")
    payload = payload or {}
    if "enabled" not in payload:
        raise HTTPException(400, "Campo 'enabled' obrigatório")
    with _apikeys_lock:
        _apikeys["enabled"] = bool(payload["enabled"])
        _save_apikeys()
        enabled = bool(_apikeys["enabled"])
    corpo = {"ok": True, "enabled": enabled, "auth_active": _auth_enabled()}
    if not enabled:
        corpo["avisos"] = ["Exigência de chave DESLIGADA: qualquer máquina da rede "
                           "acessa sem credencial"]
        print("[apikeys] exigência de chave DESLIGADA pela UI — a LAN inteira "
              "acessa sem credencial", file=sys.stderr, flush=True)
    return corpo


@app.post("/api/apikeys/persistir")
def persistir_apikeys(request: Request):
    """Materializa em disco o estado de chaves que está na RAM (admin).

    O import não grava (#601): migração de `.apikey`, espelho do legado e chave
    padrão de instalação nova existem só em memória até uma escrita explícita. Esta
    rota é a escrita explícita sem mudar nada — quem quer o arquivo em disco (ou
    quem subiu só com env/`TTS_ROD_API_KEY` e quer o espelho do `run.sh`) chama
    aqui. Também é o que o boot confere para não perder a chave no restart."""
    _exige_admin(request)
    with _apikeys_lock:
        _save_apikeys()
        total = len(_apikeys.get("keys") or [])
    return {"ok": True, "keys": total, "path": str(APIKEYS_PATH),
            "legado": str(LEGACY_APIKEY_PATH)}


@app.get("/api/backends")
def api_backends():
    """Catálogo de backends TTS disponíveis (atalhos da UI + metadados)."""
    current = _current_backend()
    return {
        "current": current["id"],
        "current_family": current["family"],
        "current_path": current["path"],
        "backends": list_backends(),
    }


# Estado do roteador de microfone: "app" = a chamada ouve o TTS (voz do app);
# "real" = a chamada ouve o mic real -> o navegador silencia o TTS pra não somar.
# Em memória; volta a "app" quando o servidor reinicia (sem estado preso).
_mic_route = {"mode": "app"}


@app.get("/api/mic-route")
def get_mic_route():
    return _mic_route


@app.post("/api/mic-route")
async def set_mic_route(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    mode = (data or {}).get("mode")
    if mode not in ("app", "real"):
        raise HTTPException(400, "mode deve ser 'app' ou 'real'")
    _mic_route["mode"] = mode
    return _mic_route


@app.get("/api/client/mic-router")
def download_mic_router(request: Request):
    """Empacota o cliente roteador de microfone (client/) num .zip pra download.

    Injeta um config.json com o ENDEREÇO deste servidor (o header Host = como o
    navegador chegou aqui, já é o IP/hostname certo p/ a outra máquina) + a chave
    da API, pra o cliente avisar o modo sem config manual. Sob proxy por path
    (ex.: /ttsproxy), o prefixo vem no próprio path e é preservado na URL.

    ADMIN (#282): o zip entrega credencial UTILIZÁVEL em texto claro. A chave é de
    USO (#793) — quem instala o cliente só gera fala/transcreve, não precisa do
    poder de admin que a primária (`keys[0]`) costuma ter. Listagem mascarada fica
    aberta a qualquer chave; a credencial não.

    O `server_url` só usa o `Host` se ele passar pela MESMA allowlist do #779
    (`_host_permitido`): `Host` é o que o cliente mandou, não o que o servidor
    reconhece. Reprovado, monta com um nome de confiança (túnel configurado ou IP
    desta máquina) e mantém a porta.
    """
    import io
    import zipfile

    _exige_admin(request)
    src = BASE / "client"
    if not src.is_dir():
        raise HTTPException(404, "Cliente não encontrado")

    host = _host_do_cliente(request)
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
    url_path = request.scope.get("tts_prefix_path") or request.url.path
    sufixo = "/api/client/mic-router"
    prefixo = url_path[: -len(sufixo)] if url_path.endswith(sufixo) else ""
    cfg = {"server_url": f"{scheme}://{host}{prefixo}", "api_key": _chave_para_cliente()}

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(src.rglob("*")):
            # só os fontes; nada de venv/caches/config antigo
            if not p.is_file():
                continue
            rel = p.relative_to(src)
            if rel.parts and rel.parts[0] in (".venv", "__pycache__"):
                continue
            if p.suffix == ".pyc" or rel.name == "config.json":
                continue
            z.write(p, Path("mic-router") / rel)
        z.writestr("mic-router/config.json", json.dumps(cfg, indent=2))
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="tts-studio-mic-router.zip"'},
    )


# ── Conversa: decide por voz/chat o texto que o agente vai falar ───────────
# Consumo principal: The Dudes (agentes) — mas a tela de teste usa o mesmo contrato.
# Sessões stateful: start → posts até status "confirmed" → text.
CHAT_TTL = 3600          # sessão expira 1h sem uso
CHAT_MAX_MSGS = 60       # teto de mensagens enviadas ao LLM por rodada
# Teto de sessões: sem ele o dict só crescia (o TTL era a única poda e roda no
# `start`). Evicção pelo `updated` mais antigo, DEPOIS da poda por TTL.
_CHAT_MAX_SESSIONS = max(1, int(os.environ.get("TTS_CHAT_MAX_SESSIONS", "32")))
# Espera na FILA do executor ≠ espera do PROVEDOR (#282). O pool tinha 8 threads
# e o único timeout era 150 s contados de antes de entrar na fila: da 9ª conversa
# em diante o turno morria com "o provedor demorou" sem o provedor ter sido
# chamado. Agora o pool é maior, a espera de fila tem prazo próprio (estourou,
# o turno SEGUE na fila e o cliente acompanha pelo GET) e o prazo do provedor
# continua existindo, com mensagem separada.
_CHAT_FILA_ESPERA_S = float(os.environ.get("TTS_CHAT_FILA_ESPERA_S", "20"))
_CHAT_PROVEDOR_ESPERA_S = 150.0
#: quanto o `/api/models/unload` espera o `_gen_lock` antes de devolver 409
_GEN_ESPERA_UNLOAD_S = 3.0
#: teto do PRIMEIRO delta quando o dsh atende a CONVERSA (#287). Sem isto um
#: provedor que só pensa segurava o cliente e o `_turno_lock` por até 1 h e o
#: próximo turno herdava o cliente envenenado. No Live o teto é o do pipeline.
_CHAT_DSH_PRIMEIRO_DELTA_S = float(os.environ.get("TTS_CHAT_DSH_PRIMEIRO_DELTA_S", "25"))
#: teto do 1º delta quando o bridge está SEM patch (`clean`): não há delta a
#: esperar, a resposta vem inteira no fim
_DSH_FIRST_DELTA_S = 12.0
_DSH_FIRST_DELTA_CLEAN_S = float(os.environ.get("TTS_LIVE_DSH_TETO_CLEAN_S", "4"))
_chat_sessions: dict = {}
_chat_lock = threading.Lock()
_chat_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=max(2, int(os.environ.get("TTS_CHAT_LLM_WORKERS", "16"))),
    thread_name_prefix="chat-llm")

CHAT_SYSTEM = (
    "Você ajuda a decidir o TEXTO FINAL que será falado por um agente de voz (TTS). "
    "Converse em português, curto e objetivo: entenda o objetivo, faça perguntas de "
    "esclarecimento, proponha rascunhos e incorpore o feedback. É CONVERSA POR VOZ: "
    "responda em no máximo 2 frases curtas, como num papo por WhatsApp. FLUXO "
    "OBRIGATÓRIO: 1) proponha o rascunho e pergunte se está aprovado; 2) só marque "
    "final=true quando o usuário disser EXPLICITAMENTE para enviar (ex.: 'pode mandar', "
    "'envia', 'aprovado') DEPOIS de você ter proposto um rascunho. Comentários, ajustes e "
    "frases ambíguas NÃO são confirmação — nesses casos responda APENAS com JSON: "
    "{\"final\": false, \"reply\": \"<sua mensagem conversacional>\"}. "
    "Confirmação explícita → APENAS com JSON: "
    "{\"final\": true, \"text\": \"<texto aprovado, pronto p/ fala>\"}. "
    "O texto final deve conter só o que será falado — sem comentários sobre a conversa. "
    "DEPOIS DE ENTREGAR a conversa continua: pedido ou pergunta NOVA começa um "
    "RASCUNHO NOVO — nunca reenvie um texto já entregue, a não ser que peçam "
    "exatamente o mesmo. Se a fala misturar confirmação com pedido novo (ex.: "
    "'agora envia, e pergunta por que não conectou antes'), o pedido novo é o "
    "próximo rascunho: proponha ele e pergunte se está aprovado."
)


def _chat_provider() -> tuple[str, str, str]:
    """(base_url, model, api_key) efetivos — chat_* com fallback p/ tradução.

    `TTS_CHAT_BASE_URL` / `TTS_CHAT_MODEL` / `TTS_CHAT_API_KEY` (ambiente) têm
    PRECEDÊNCIA sobre as settings: é o que permite teste/smoke apontar para um stub
    SEM gravar no `settings.json` REAL. Motivo (incidente 2026-09-25): o smoke
    gravava via POST /api/settings, o `finally` do restore não roda se o run morre
    — e o snapshot seguinte já era o estado poluído, então o mecanismo não se
    auto-curava. Com env, o arquivo do dono fica intocado por construção.
    A precedência é POR CAMPO: setar só `TTS_CHAT_MODEL` não derruba a base URL."""
    base = (os.environ.get("TTS_CHAT_BASE_URL") or "").strip() \
        or (_settings.get("chat_base_url") or "").strip() \
        or (_settings.get("remote_base_url") or "").strip()
    model = (os.environ.get("TTS_CHAT_MODEL") or "").strip() \
        or (_settings.get("chat_model") or "").strip() \
        or (_settings.get("remote_translate_model") or "gpt-4o-mini")
    key = (os.environ.get("TTS_CHAT_API_KEY") or "").strip() \
        or (_settings.get("chat_api_key") or "").strip() \
        or (_settings.get("remote_api_key") or "").strip()
    if not base:
        raise HTTPException(400, "Provedor de conversa não configurado "
                                 "(Configurações → Rede: Base URL OpenAI-compat)")
    return base.rstrip("/"), model, key


def _chat_llm(messages: list) -> str:
    """Devolve o conteúdo da resposta. Despacha pelo backend (`chat_backend`)."""
    if _chat_backend() == "dsh":
        return _chat_llm_dsh(messages)
    return _chat_llm_openai(messages)


def _chat_llm_openai(messages: list) -> str:
    """Chama /chat/completions do provedor e devolve o conteúdo da resposta."""
    import urllib.request
    base, model, key = _chat_provider()
    headers = {"Content-Type": "application/json",
               "User-Agent": "Mozilla/5.0 (compatible; tts-studio/1.0)"}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    # JSON extra do usuário sobrepõe/adiciona (reasoning_effort, top_p…)
    try:
        extra = json.loads(_settings.get("chat_extra") or "{}")
        if not isinstance(extra, dict):
            extra = {}
    except (ValueError, TypeError):
        extra = {}

    def _corpo(effort: str | None) -> bytes:
        corpo = {"model": model, "messages": messages, "temperature": 0.4, **extra}
        if effort and "reasoning_effort" not in corpo:  # GLM/vLLM queimam tokens em reasoning
            corpo["reasoning_effort"] = effort
        return json.dumps(corpo).encode()

    import ssl
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 — sem certifi, segue com o trust store padrão
        ctx = ssl.create_default_context()

    ultimo_erro = ""
    # com reasoning_effort fixado pelo usuário não há retry (corpo idêntico não muda nada)
    for effort in ((None,) if "reasoning_effort" in extra else ("low", None)):
        req = urllib.request.Request(f"{base}/chat/completions", data=_corpo(effort),
                                     method="POST", headers=headers)
        try:
            _provedor_marca("chamando")
            with urllib.request.urlopen(req, timeout=120, context=ctx) as resp:
                _provedor_marca("ok", getattr(resp, "status", 200))
                dados = json.loads(resp.read())
            return dados["choices"][0]["message"]["content"] or ""
        except urllib.error.HTTPError as e:
            _provedor_marca("erro", e.code)
            ultimo_erro = f"HTTP {e.code}"
            if e.code not in (400, 422):
                raise HTTPException(502, f"Provedor de conversa falhou: HTTP {e.code}")
            # 400/422: provedor não aceita reasoning_effort — tenta sem o campo
        except HTTPException:
            raise
        except Exception as e:
            _provedor_marca("timeout" if "timed out" in str(e).lower() else "erro")
            raise HTTPException(502, f"Provedor de conversa falhou: {e}")
    raise HTTPException(502, f"Provedor de conversa falhou: {ultimo_erro}")


# ---------------------------------------------------------------------------
# Backend de IA "dsh" (harness via ACP) — task_129d2183. O `chat_backend` escolhe
# entre o endpoint OpenAI-compat acima e o processo local do dsh. `_chat_provider`
# não muda de assinatura; o caminho openai fica INTACTO. Env por CAMPO, no mesmo
# padrão do `TTS_CHAT_*`: `TTS_CHAT_BACKEND`/`TTS_CHAT_DSH_{BIN,PROFILE,MODEL,EFFORT}`.
# ---------------------------------------------------------------------------
def _chat_backend() -> str:
    """Backend do caminho da CONVERSA (e herança do Live — ver `_chat_backend_live`)."""
    v = (os.environ.get("TTS_CHAT_BACKEND") or "").strip().lower() \
        or str(_settings.get("chat_backend") or "openai").strip().lower()
    return v if v in ("openai", "dsh") else "openai"


def _chat_backend_live() -> str:
    """Backend do caminho do LIVE (#176) — `chat_backend_live` com fallback ao global.

    POR QUE SEPARAR: as duas telas têm recomendações diferentes (o Live ganha muito
    com o dsh; a Conversa vai bem no provedor do dono). Um campo global obrigava a
    escolher entre as duas. Vazio = herda, então quem não mexer nada não muda."""
    v = (os.environ.get("TTS_CHAT_BACKEND_LIVE") or "").strip().lower() \
        or str(_settings.get("chat_backend_live") or "").strip().lower()
    return v if v in ("openai", "dsh") else _chat_backend()


def _chat_dsh_cfg() -> dict:
    """Config do dsh com precedência POR CAMPO do ambiente (setar só um não derruba os outros)."""
    def campo(sufixo: str, setting: str, default: str) -> str:
        return (os.environ.get(f"TTS_CHAT_DSH_{sufixo}") or "").strip() \
            or str(_settings.get(setting) or "").strip() or default
    return {
        "bin": campo("BIN", "chat_dsh_bin", "dsh"),
        "profile": campo("PROFILE", "chat_dsh_profile", dsh_client.DSH_DEFAULT_PROFILE),
        "model": dsh_client.dsh_model_for_turn(
            campo("MODEL", "chat_dsh_model", dsh_client.DSH_DEFAULT_MODEL)),
        "effort": dsh_client.dsh_effort_valido(campo("EFFORT", "chat_dsh_effort", "off")),
    }


_chat_dsh_lock = threading.Lock()
_chat_dsh_livres: list = []          # processos quentes e OCIOSOS: [(chave, cli)]
# (o global `_chat_dsh_chave` saiu no #185: quem carrega a chave é o cliente, em
# `_pool_chave` — o global era escrito e nunca lido)
_chat_dsh_prewarm_thread: threading.Thread | None = None
_CHAT_DSH_POOL_MAX = 3

# ── Teto GLOBAL de processos `dsh` (#287) ──────────────────────────────────
# O pool (3) e o Live (1 por sessão, até `TTS_LIVE_MAX_SESSIONS`) e a descoberta
# somavam até 8 harnesses node, cada um com 2 threads leitoras, sem nenhum
# contador comum — no mini, que é a máquina mais apertada, isso é o que derruba.
# Quem não consegue slot recebe `dsh_ocupado` (a sessão Live segue no openai).
_DSH_MAX_PROCESSOS = max(1, int(os.environ.get("TTS_DSH_MAX_PROCESSOS", "5")))
_dsh_procs = 0
_dsh_procs_lock = threading.Lock()


def _dsh_slot_livre() -> bool:
    with _dsh_procs_lock:
        return _dsh_procs < _DSH_MAX_PROCESSOS


def _dsh_ocupa_slot(cli) -> bool:
    """Reserva um slot para o processo deste cliente. False = teto atingido.

    Marca o objeto (`_slot_contado`): quem fecha precisa saber que há slot a
    devolver — assim `close()` duplo ou cliente sem slot não desbalanceiam."""
    global _dsh_procs
    with _dsh_procs_lock:
        if _dsh_procs >= _DSH_MAX_PROCESSOS:
            return False
        _dsh_procs += 1
    try:
        cli._slot_contado = True
    except Exception:  # noqa: BLE001 — objeto exótico não impede a contagem
        pass
    return True


def _dsh_libera_slot(cli) -> None:
    global _dsh_procs
    if not getattr(cli, "_slot_contado", False):
        return
    try:
        cli._slot_contado = False
    except Exception:  # noqa: BLE001
        pass
    with _dsh_procs_lock:
        _dsh_procs = max(0, _dsh_procs - 1)


def _dsh_fecha(cli) -> None:
    """Fecha o cliente e devolve o slot, uma vez só."""
    try:
        cli.close()
    except Exception:  # noqa: BLE001 — fechar é best-effort
        pass
    finally:
        _dsh_libera_slot(cli)


def _dsh_marca_morto(motivo: str) -> None:
    """Handshake do dsh morreu: segura o caminho por `_DSH_LATCH_TTL_S`.

    A falha NÃO é específica da sessão (binário/perfil/node), então toda sessão
    nova pagava as 6 tentativas de novo. O latch só vale para FALHA — estouro de
    relógio (dsh lento, não morto) não liga isto."""
    global _dsh_morto_ate, _dsh_morto_motivo
    _dsh_morto_ate = time.monotonic() + _DSH_LATCH_TTL_S
    _dsh_morto_motivo = str(motivo)[:200]
    _dsh_log(f"dsh marcado morto por {_DSH_LATCH_TTL_S:.0f}s: {_dsh_morto_motivo}")


def _dsh_latch(motivo: str) -> str | None:
    """Motivo do latch ainda válido (None = pode tentar o dsh)."""
    global _dsh_morto_ate
    if not _dsh_morto_ate:
        return None
    if time.monotonic() >= _dsh_morto_ate:
        _dsh_morto_ate = 0.0
        return None
    return _dsh_morto_motivo or motivo


def _dsh_procs_resumo() -> dict:
    with _dsh_procs_lock:
        return {"dsh_procs": _dsh_procs, "dsh_procs_max": _DSH_MAX_PROCESSOS}


class _DshSemSlot(RuntimeError):
    """Teto global de processos `dsh` atingido — quem chamou decide (429/aviso)."""


class _DshComSlot:
    """Envelope que RESERVA um slot global ao nascer e o devolve no `close()`.

    Envelope (e não subclasse) porque os testes substituem `dsh_client.DshClient`
    por um falso: a classe é resolvida em TEMPO DE CHAMADA, então o falso também
    entra na contagem. Todo o resto do contrato é delegado por `__getattr__`."""

    def __init__(self, *args, **kwargs):
        # resolve AGORA (não na definição): quem faz `monkeypatch.setattr(
        # dsh_client, "DshClient", falso)` continua sendo respeitado
        self._cli = dsh_client.DshClient(*args, **kwargs)
        if not _dsh_ocupa_slot(self):
            try:
                self._cli.close()
            finally:
                raise _DshSemSlot(
                    f"teto de {_DSH_MAX_PROCESSOS} processos dsh atingido "
                    f"(TTS_DSH_MAX_PROCESSOS); feche uma sessão Live ou espere")

    def __getattr__(self, nome):
        # só roda para atributos que o envelope não tem; `_cli` é setado no
        # `__init__` (cuidado com recursão antes dele existir)
        if nome == "_cli":
            raise AttributeError(nome)
        return getattr(self._cli, nome)

    def __setattr__(self, nome, valor):
        if nome == "_cli":
            object.__setattr__(self, nome, valor)
        else:
            setattr(self._cli, nome, valor)

    @property
    def prompt_em_voo(self) -> bool:
        return bool(getattr(self._cli, "prompt_em_voo", False))

    def close(self) -> None:
        try:
            self._cli.close()
        finally:
            _dsh_libera_slot(self)
# campos cuja mudança invalida o processo quente da Conversa (pre-warm + pool)
_CAMPOS_DHS_BACKEND = ("chat_backend", "chat_backend_live", "chat_dsh_bin",
                       "chat_dsh_profile",
                       "chat_dsh_model", "chat_dsh_effort")


def _chat_dsh_chave_do(cfg: dict) -> tuple:
    return (cfg["bin"], cfg["profile"], cfg["model"], cfg["effort"])


def _dsh_log(msg: str, tag: str = "chat-dsh") -> None:
    """Log do caminho dsh (cliente e pre-warm) sai por STDERR, não pelo stdout.

    O stdout do processo é lido como DADO por quem faz `import app` — o teste de
    telemetria do ORT compara o stdout inteiro de um subprocesso — e o pre-warm
    sobe em thread, então a linha pode sair antes ou depois do fim do import. O
    canal separado deixa o stdout determinístico em vez de dependente da corrida
    (task_6db0e2cc)."""
    print(f"[{tag}] {msg}", file=sys.stderr, flush=True)


def _chat_dsh_novo(cfg: dict) -> "dsh_client.DshClient":
    return _DshComSlot(
        bin=cfg["bin"], profile=cfg["profile"], model=cfg["model"],
        effort=cfg["effort"], cwd=OUTPUTS_DIR / ".dsh-cwd",
        primeiro_delta_timeout=_CHAT_DSH_PRIMEIRO_DELTA_S,
        on_log=lambda m: _dsh_log(m))


def _chat_dsh_cliente() -> "dsh_client.DshClient":
    """Cliente quente do pool (processo dsh persistente). Fecha o de config ANTIGA.

    A chave (bin/perfil/modelo/effort) viaja CARIMBADA no cliente (`_pool_chave`),
    não num global: entre a entrega e a devolução o `/api/settings` pode ter trocado a
    config e o prewarm da nova já ter mexido nele — carimbar na devolução fazia um
    cliente da config ANTIGA voltar ao pool como se fosse da nova (o próximo turno
    rodava no modelo antigo, calado)."""
    cfg = _chat_dsh_cfg()
    chave = _chat_dsh_chave_do(cfg)
    with _chat_dsh_lock:
        antigos = [(k, c) for k, c in _chat_dsh_livres if k != chave or not c.alive]
        _chat_dsh_livres[:] = [(k, c) for k, c in _chat_dsh_livres
                               if k == chave and c.alive]
        cli = _chat_dsh_livres.pop()[1] if _chat_dsh_livres else None
    # FORA do lock: `close()` faz `session/close` com timeout de 60 s e um dsh vivo
    # mas mudo não responde — fechando dentro do lock, TODO uso do pool (o próximo
    # turno da Conversa, o resumo do Live) esperava o processo velho morrer.
    for _k, antigo in antigos:
        _dsh_fecha(antigo)
    if cli is None:
        try:
            cli = _chat_dsh_novo(cfg)
        except _DshSemSlot as exc:
            raise HTTPException(429, f"dsh_ocupado: {exc}") from exc
    cli._pool_chave = chave              # a chave viaja COM o cliente
    return cli


def _chat_dsh_devolve(cli: "dsh_client.DshClient") -> None:
    """Devolve ao pool com a chave DA ENTREGA (não a global de agora).

    Devolução DUPLA do MESMO objeto não pode virar duas entradas: `_chat_dsh_cliente`
    dá `pop()` numa e a outra fica na lista, então a entrega seguinte devolve o mesmo
    cliente — ainda em uso pelo chamador anterior. Dois turnos passam a dividir o slot
    de prompt (`stream` faz `_esperar_slot_prompt()`: não é erro, é espera) e o resumo
    do Live, que existe para NÃO disputar o slot com o turno em curso, volta a
    disputar (#218). A identidade (`is`) é a checagem certa porque a lista guarda o
    OBJETO; a entrada que já está lá não é fechada — fechar mataria o processo quente.

    O `close()` do descarte roda FORA do lock (mesmo motivo do `_chat_dsh_cliente`)."""
    chave = getattr(cli, "_pool_chave", None)
    with _chat_dsh_lock:
        if any(c is cli for _k, c in _chat_dsh_livres):
            return                                   # já está no pool: nem duplica nem fecha
        guardar = bool(chave is not None and cli.alive
                       and len(_chat_dsh_livres) < _CHAT_DSH_POOL_MAX)
        if guardar:
            _chat_dsh_livres.append((chave, cli))
    if not guardar:
        _dsh_fecha(cli)


def _chat_dsh_prewarm(motivo: str = "") -> threading.Thread | None:
    """Sobe processo + sessão do dsh FORA do 1º turno da Conversa (best-effort).

    Sem custo para quem usa `openai`: nada é spawnado. Nunca bloqueia o chamador
    (thread), nunca derruba nada e nunca muda o backend — falha só vira log. O
    `prewarm()` do cliente é idempotente e o processo volta ao pool quente."""
    global _chat_dsh_prewarm_thread
    if _chat_backend() != "dsh":
        return None
    with _chat_dsh_lock:
        atual = _chat_dsh_prewarm_thread
        if atual is not None and atual.is_alive():
            return atual

    def _rodar() -> None:
        cli = None
        try:
            cli = _chat_dsh_cliente()
            cli.prewarm()
            _dsh_log(f"pre-warm ({motivo}) em {cli.ultimo_boot_ms} ms "
                     f"(sessão {cli.session_id})")
        except Exception as exc:  # noqa: BLE001 — prewarm é otimização
            _dsh_log(f"pre-warm falhou ({motivo}): {exc} — segue")
        finally:
            if cli is not None:
                _chat_dsh_devolve(cli)

    th = threading.Thread(target=_rodar, name="chat-dsh-prewarm", daemon=True)
    with _chat_dsh_lock:
        _chat_dsh_prewarm_thread = th
    th.start()
    return th


def _chat_llm_dsh(messages: list) -> str:
    """`_chat_llm` pelo backend dsh. Modo histórico: a lista vai renderizada num
    prompt (assinatura preservada) — a Conversa não é sensível a latência."""
    cli = _chat_dsh_cliente()
    try:
        return cli.collect(messages)
    except dsh_client.DshError as exc:
        raise HTTPException(502, f"Backend dsh falhou: {exc}")
    finally:
        # prompt EM VOO não pode voltar ao pool: o próximo turno esperaria o
        # settle (até 300 s) e arriscaria `-32602`. Fecha e libera o slot (#287).
        if getattr(cli, "prompt_em_voo", False):
            _dsh_log("cliente com prompt em voo — fechando em vez de devolver ao pool")
            _dsh_fecha(cli)
        else:
            _chat_dsh_devolve(cli)


# Aquece o dsh no boot do app quando o backend já é dsh. Não bloqueia nada e, com
# `chat_backend=openai`, nem chama `which("dsh")`: nenhum processo é spawnado.
try:
    _chat_dsh_prewarm("startup")
except Exception as exc:  # noqa: BLE001 — prewarm é otimização
    _dsh_log(f"pre-warm inicial falhou: {exc} — segue")


_dsh_models_cache: dict = {}
_dsh_models_lock = threading.Lock()          # #213: 1 descoberta por vez no frio
_DSH_MODELS_TTL_S = 300
#: cache NEGATIVO da descoberta (#287): com o dsh quebrado cada GET pagava 6
#: tentativas e ~60 s de backoff, com o lock tomado — a tela que polla virava fila.
#: Falha recente responde na hora com o motivo; acerto limpa o registro.
_DSH_MODELS_ERRO_TTL_S = 30.0
_dsh_models_erro: dict = {}                  # {"motivo", "em"}
#: latch de PROCESSO: com o handshake do dsh morto, toda sessão Live nova repetia
#: as 6 tentativas. Uma falha real silencia o dsh para todas por este tempo.
_DSH_LATCH_TTL_S = 20.0
_dsh_morto_ate = 0.0
_dsh_morto_motivo = ""


_bridge_cache: dict = {}
_BRIDGE_CACHE_TTL_S = 60.0


def _dsh_bridge_estado_cached() -> dict:
    """`_dsh_bridge_estado` com cache curto: chamado no NASCIMENTO de cada sessão.

    Sem cache, cada sessão Live pagaria `npm root -g` quando a busca por caminho
    falha."""
    agora = time.monotonic()
    if _bridge_cache.get("dados") is not None and \
            agora - _bridge_cache.get("em", 0) < _BRIDGE_CACHE_TTL_S:
        return _bridge_cache["dados"]
    dados = _dsh_bridge_estado()
    _bridge_cache.update({"dados": dados, "em": agora})
    return dados


def _dsh_bridge_estado() -> dict:
    """Estado do patch do bridge ACP no host (task_159). Nunca levanta.

    O patch vive fora do repo e some em `npm install -g`; sem isto o produto não
    tem como saber que o caminho dsh regrediu para "resposta inteira no fim"."""
    try:
        return dsh_client.estado_bridge(bin=_chat_dsh_cfg()["bin"])
    except Exception as exc:  # noqa: BLE001 — campo informativo não derruba o endpoint
        return {"estado": "unknown", "motivo": f"{type(exc).__name__}: {exc}"}


@app.get("/api/chat/dsh/models")
def chat_dsh_models():
    """Descoberta de modelos/efforts do dsh (o frontend integra a partir daqui)."""
    cfg = _chat_dsh_cfg()
    chave = (cfg["bin"], cfg["profile"])
    agora = time.time()
    # o lock cobre só o caminho FRIO: a descoberta spawna um processo `dsh` e dois
    # GETs simultâneos (a tela chama no clique) subiam 2+ processos, com o último
    # sobrescrevendo o cache (#213). O caminho quente não espera.
    with _dsh_models_lock:
        if not _dsh_slot_livre():
            raise HTTPException(429, "dsh_ocupado: teto de processos atingido — "
                                     "tente de novo em instantes")
        if (_dsh_models_cache.get("chave") == chave
                and agora - _dsh_models_cache.get("em", 0) < _DSH_MODELS_TTL_S):
            dados = _dsh_models_cache["dados"]
        elif (_dsh_models_erro and _dsh_models_erro.get("chave") == chave
              and agora - _dsh_models_erro.get("em", 0) < _DSH_MODELS_ERRO_TTL_S):
            # falha RECENTE: responde na hora (sem spawnar, sem as 6 tentativas)
            raise HTTPException(
                502, f"descoberta do dsh falhou há pouco: {_dsh_models_erro.get('motivo')}",
                headers={"Retry-After": str(int(_DSH_MODELS_ERRO_TTL_S))})
        else:
            # a descoberta sobe um processo PRÓPRIO: reserva um slot enquanto roda
            # (senão o teto contaria tudo menos ela)
            reserva = type("_Reserva", (), {})()
            if not _dsh_ocupa_slot(reserva):
                raise HTTPException(429, "dsh_ocupado: teto de processos atingido")
            try:
                dados = dsh_client.descobrir_modelos(bin=cfg["bin"], profile=cfg["profile"])
            except dsh_client.DshError as exc:
                # a falha é da CONFIG (bin/perfil): trocar de bin não pode herdar
                _dsh_models_erro.update({"motivo": str(exc)[:400], "em": agora,
                                         "chave": chave})
                raise HTTPException(502, f"descoberta do dsh falhou: {exc}")
            finally:
                _dsh_libera_slot(reserva)
            _dsh_models_erro.clear()
            _dsh_models_cache.update({"dados": dados, "chave": chave, "em": agora})
    # o estado do bridge sobe um `npm root -g` quando o caminho falha: fica FORA
    # do lock (a descoberta é sobre o dsh, o bridge é informativo) — e é cacheados
    # com o resto porque a tela não paga leitura de arquivo por request.
    if "bridge_info" not in dados:
        dados = {**dados, "bridge_info": _dsh_bridge_estado()}
        with _dsh_models_lock:
            _dsh_models_cache.update({"dados": dados, "chave": chave, "em": agora})
    bridge = dados.get("bridge_info") or {"estado": "unknown"}
    return {"ok": True, "backend": "dsh",
            **{k: v for k, v in dados.items() if k != "bridge_info"},
            # `bridge` NÃO faz parte do dsh: diz se o HOST tem o patch que entrega
            # deltas (sem ele a resposta vem inteira no fim). A tela usa para avisar.
            "bridge": bridge.get("estado", "unknown"),
            "bridge_detalhe": bridge,
            "current": {"model": cfg["model"], "effort": cfg["effort"]},
            "default_model": dsh_client.DSH_DEFAULT_MODEL,
            "efforts": list(dsh_client.DSH_EFFORTS)}


def _chat_parse(conteudo: str) -> dict:
    """Extrai o JSON da resposta (tolerante a ```json e texto solto)."""
    txt = (conteudo or "").strip()
    m = re.search(r"```(?:json)?\s*(.+?)\s*```", txt, re.S)
    if m:
        txt = m.group(1)
    try:
        d = json.loads(txt)
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    i, j = (conteudo or "").find("{"), (conteudo or "").rfind("}")
    if 0 <= i < j:
        try:
            d = json.loads(conteudo[i:j + 1])
            if isinstance(d, dict):
                return d
        except Exception:
            pass
    return {"final": False, "reply": (conteudo or "").strip()}


def _chat_get(sid: str) -> dict:
    with _chat_lock:
        s = _chat_sessions.get(sid)
        if not s:
            raise HTTPException(404, "Sessão de conversa não encontrada (expirou?)")
        if time.time() - s["updated"] > CHAT_TTL:
            _chat_sessions.pop(sid, None)
            raise HTTPException(404, "Sessão de conversa expirou")
        s["updated"] = time.time()
        return s


def _chat_purge(now: float) -> None:
    """Poda por TTL e, se ainda passar do TETO, evicta as sessões mais antigas."""
    for sid in [k for k, d in _chat_sessions.items() if now - d["updated"] > CHAT_TTL]:
        _chat_sessions.pop(sid, None)
    while len(_chat_sessions) > _CHAT_MAX_SESSIONS:
        mais_velha = min(_chat_sessions, key=lambda k: _chat_sessions[k]["updated"])
        _chat_sessions.pop(mais_velha, None)


def _chat_worker(sid: str, gen: int = 0) -> None:
    """Chama o LLM em background e atualiza a sessão (status thinking → reply).
    Com teto hard de 150s: provedor pendurado não deixa a sessão em 'thinking'
    para sempre.
    `gen` é a geração do turno: se o humano interrompeu (barge-in) e mandou fala
    nova, a sessão já está em outra geração e este resultado é jogado fora.

    FILA × PROVEDOR (#282): `iniciou` é setado quando a thread do pool COMEÇA o
    turno. A espera por ele e a espera pelo provedor são medidas separadamente —
    estourar a fila não vira "o provedor demorou" (o turno segue na fila e o GET
    acompanha), e só a segunda vira erro na sessão."""
    iniciou = threading.Event()

    def _rodar() -> None:
        iniciou.set()
        try:
            with _chat_lock:
                s = _chat_sessions.get(sid)
                if not s or s.get("gen", 0) != gen:
                    return
                s["na_fila"] = False          # saiu da fila: agora é o provedor
                hist = s["messages"][:]
                if len(hist) > CHAT_MAX_MSGS + 1:
                    hist = [hist[0]] + hist[-(CHAT_MAX_MSGS + 1):]
            sys_prompt = (_settings.get("chat_system") or "").strip() or CHAT_SYSTEM
            # #871: regra de FALA ANEXADA depois da instrução do dono (#799), que
            # segue inteira e na frente. Uma mensagem de sistema só: há provedor
            # OpenAI-compatível que recusa `system` fora da 1ª posição.
            sys_prompt = (f"{sys_prompt}\n\n{REGRA_FALA} Isso vale para o conteúdo "
                          "de 'reply' e de 'text'; o formato JSON pedido continua valendo.")
            reply = _chat_llm([{"role": "system", "content": sys_prompt}] + hist)
            parsed = _chat_parse(reply)
            with _chat_lock:
                s = _chat_sessions.get(sid)
                if not s or s.get("gen", 0) != gen:
                    return   # interrompido: resposta velha não entra na sessão
                if parsed.get("final") and parsed.get("text"):
                    s["status"], s["text"] = "confirmed", str(parsed["text"])
                    s["reply"] = s["text"]
                    s["last_text"] = s["text"]   # último texto aprovado (persiste entre rodadas)
                    # a ENTREGA também é turno do assistente. Sem isto o histórico
                    # ficava com dois 'user' seguidos e sem registro do que foi
                    # entregue: na rodada seguinte o único rascunho visível era o
                    # antigo e o modelo reconfirmava o MESMO texto.
                    s["messages"].append({
                        "role": "assistant",
                        "content": json.dumps({"final": True, "text": s["text"]},
                                              ensure_ascii=False),
                    })
                else:
                    s["reply"] = str(parsed.get("reply") or "")
                    s["messages"].append({"role": "assistant", "content": s["reply"]})
                s["thinking"] = False
                s["updated"] = time.time()
        except Exception as e:  # noqa: BLE001 — erro vai pra sessão, não derruba o server
            with _chat_lock:
                s = _chat_sessions.get(sid)
                if s and s.get("gen", 0) == gen:
                    s["error"] = str(e)[:200]
                    s["thinking"] = False

    futuro = _chat_executor.submit(_rodar)
    if not iniciou.wait(_CHAT_FILA_ESPERA_S):
        # ainda na fila: NÃO é erro (o turno continua e o cliente polla o GET)
        with _chat_lock:
            s = _chat_sessions.get(sid)
            if s and s.get("gen", 0) == gen:
                s["na_fila"] = True
        return
    try:
        futuro.result(timeout=_CHAT_PROVEDOR_ESPERA_S)
    except concurrent.futures.TimeoutError:
        with _chat_lock:
            s = _chat_sessions.get(sid)
            if s and s.get("gen", 0) == gen and s.get("thinking"):
                s["error"] = (f"Provedor de IA demorou demais "
                              f"({_CHAT_PROVEDOR_ESPERA_S:.0f}s) — tente de novo")
                s["thinking"] = False


@app.post("/api/chat/start")
def chat_start(payload: dict):
    """Abre sessão p/ decidir, conversando, o texto que o agente vai falar.
    Assíncrono: responde na hora e a fala da IA chega via GET (polling)."""
    objetivo = str((payload or {}).get("objective") or "").strip()
    if not objetivo:
        raise HTTPException(400, "Campo 'objective' obrigatório")
    _chat_dsh_prewarm("chat/start")    # no-op com `openai`
    contexto = str((payload or {}).get("context") or "").strip()
    sid = uuid.uuid4().hex[:12]
    msgs = [{"role": "user",
             "content": f"Objetivo: {objetivo}" + (f"\nContexto: {contexto}" if contexto else "")}]
    now = time.time()
    with _chat_lock:
        _chat_sessions[sid] = {"objective": objetivo, "context": contexto,
                               "messages": msgs, "status": "chatting", "text": "",
                               "reply": "", "thinking": True, "error": "", "last_text": "",
                               "gen": 0, "created": now, "updated": now}
        # poda DEPOIS de inserir: antes dela, o dict ficava no teto + 1 (o novo
        # entrava depois da poda); a evicção pega o `updated` mais antigo, então
        # esta sessão nunca é a escolhida.
        _chat_purge(now)
    threading.Thread(target=_chat_worker, args=(sid, 0), daemon=True).start()
    return {"session_id": sid, "status": "thinking"}


@app.post("/api/chat/{sid}")
def chat_message(sid: str, payload: dict):
    """Fala nova do humano (transcrição do STT). A resposta da IA chega pelo
    GET — assíncrono p/ não estourar timeouts de proxy/Cloudflare."""
    s = _chat_get(sid)
    msg = str((payload or {}).get("message") or "").strip()
    if not msg:
        raise HTTPException(400, "Campo 'message' obrigatório")
    interromper = bool((payload or {}).get("interrupt"))
    with _chat_lock:
        if s.get("thinking") and not interromper:
            raise HTTPException(409, "IA ainda processando a fala anterior")
        interrompido = bool(s.get("thinking"))
        if interrompido:
            # barge-in: a resposta em voo é descartada (o worker vê a geração
            # nova e não escreve nada). A fala anterior ficou sem resposta, então
            # junta com a nova pra não mandar dois 'user' seguidos ao provedor.
            if s["messages"] and s["messages"][-1]["role"] == "user":
                s["messages"][-1]["content"] += "\n" + msg
            else:
                s["messages"].append({"role": "user", "content": msg})
        else:
            s["messages"].append({"role": "user", "content": msg})
        s["gen"] = s.get("gen", 0) + 1
        gen = s["gen"]
        s["status"], s["text"] = "chatting", ""
        s["reply"], s["error"] = "", ""
        s["thinking"] = True
        s["updated"] = time.time()
    threading.Thread(target=_chat_worker, args=(sid, gen), daemon=True).start()
    return {"ok": True, "status": "thinking", "interrupted": interrompido}


@app.get("/api/chat/{sid}")
def chat_get(sid: str):
    s = _chat_get(sid)
    with _chat_lock:
        status = "thinking" if s.get("thinking") else s["status"]
        # `reply`/`text` saem como o modelo escreveu (a UI exibe); os `_fala`
        # são o que vai ao TTS — sem markdown, emoji, URL nem código (#871)
        return {"session_id": sid, "status": status, "text": s["text"],
                "reply": s.get("reply", ""), "error": s.get("error", ""),
                "text_fala": texto_para_fala(s["text"]),
                "reply_fala": texto_para_fala(s.get("reply", "")),
                "fila": bool(s.get("na_fila")),
                "last_text": s.get("last_text", ""),
                "objective": s["objective"],
                "messages": [{"role": m["role"], "content": m["content"]} for m in s["messages"]]}


@app.delete("/api/chat/{sid}")
def chat_delete(sid: str):
    with _chat_lock:
        removida = _chat_sessions.pop(sid, None)
    if removida is None:
        raise HTTPException(404, "Sessão de conversa não encontrada")
    return {"ok": True}


#: teto do log de depuração da Conversa antes de rodar para `.log.1`
_CHAT_DEBUG_MAX_BYTES = 2 * 1024 * 1024


def _chat_debug_arquivo() -> Path:
    """Arquivo de depuração: `TTS_CHAT_DEBUG_LOG` ou o `/tmp` de sempre."""
    caminho = (os.environ.get("TTS_CHAT_DEBUG_LOG") or "").strip()
    return Path(caminho) if caminho else Path("/tmp/tts-chat-debug.log")


@app.post("/api/chat-debug")
def chat_debug(payload: dict):
    """Telemetria temporária do fluxo Conversa no navegador (diagnóstico)."""
    if os.environ.get("TTS_CHAT_DEBUG") != "1":
        return {"ok": True, "disabled": True}
    alvo = _chat_debug_arquivo()
    try:
        # rotação por tamanho ANTES de escrever: o arquivo era um append eterno em
        # /tmp, sem teto nem permissão restrita (#282)
        if alvo.exists() and alvo.stat().st_size > _CHAT_DEBUG_MAX_BYTES:
            alvo.replace(alvo.with_suffix(".log.1"))
        novo = not alvo.exists()
        with open(alvo, "a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {json.dumps(payload, ensure_ascii=False)}\n")
        if novo:
            try:
                os.chmod(alvo, 0o600)
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "arquivo": str(alvo)}


# ── Túnel / acesso pela internet (proxy por path) ──────────────────────────
# Dois caminhos possíveis, independentes: túnel SSH reverso até a VPS
# (tunnel.sh) e conector Cloudflare Tunnel na própria máquina (cloudflare.sh,
# gerenciado remotamente no dashboard). O app só reporta e liga/desliga os
# agentes instalados — nenhum dos dois é obrigatório.
TUNNEL_LABEL = "studio.tts.tunnel"
# A label do conector é configurável no `cloudflare.sh` (`CLOUDFLARE_LABEL`):
# fixá-la aqui fazia uma instalação com outra label aparecer como "não instalado"
# e o `/api/tunnel/*` recusar com 400 (#346). Mesma variável dos dois lados.
CF_LABEL = (os.environ.get("CLOUDFLARE_LABEL") or "").strip() or "com.local.cloudflared-tts"


def _ps_comandos() -> list[str]:
    """Linhas (pid, comando) da máquina — base do que o status reporta.

    `ps` e não `pgrep`: o que importa não é "casou", é **o que** está na linha de
    comando (o destino do `-R`, o token do conector)."""
    try:
        r = subprocess.run(["ps", "-Ao", "pid=,command="],
                           capture_output=True, timeout=5, text=True)
        return [l.strip() for l in (r.stdout or "").splitlines() if l.strip()]
    except Exception:
        return []


def _args_do_tunel_no_plist() -> tuple[str, str]:
    """`(host, porta)` do ProgramArguments do NOSSO LaunchAgent do túnel.

    É o que individualiza o `-R` do TTS entre os túneis da máquina: sem isso,
    qualquer `ssh -R` que apareça antes na lista de processos fazia o status
    dizer "túnel rodando" com o destino e a porta de OUTRO serviço (#496)."""
    try:
        texto = _tunnel_plist().read_text(errors="replace")
    except OSError:
        return "", ""
    args = re.findall(r"<string>([^<]*)</string>", texto)
    for i, arg in enumerate(args):
        if not arg.endswith("tunnel.sh"):
            continue
        resto = [a.strip() for a in args[i + 1:]]
        host = resto[0] if resto else ""
        porta = resto[1] if len(resto) > 1 and resto[1].isdigit() else ""
        return host, porta
    return "", ""


def _candidato_do_tunel(partes_ps: list[str]) -> tuple[str, str] | None:
    """`(destino, porta_remota)` do `-R 127.0.0.1:<porta>:<destino>:<porta>`."""
    for pedaco in partes_ps:
        if not pedaco.startswith("127.0.0.1:"):
            continue
        partes = pedaco.split(":")
        if len(partes) < 4:
            continue
        return partes[2], partes[1]
    return None


def _usa_a_chave_do_tts(partes_ps: list[str]) -> bool:
    """A linha do `ps` carrega `-i …/tts_tunnel` (a chave que o `tunnel.sh` usa)?"""
    for i, pedaco in enumerate(partes_ps[:-1]):
        if pedaco == "-i" and partes_ps[i + 1].rsplit("/", 1)[-1] == "tts_tunnel":
            return True
    return False


def _resumo_do_tunel(destino: str, porta: str) -> dict:
    try:
        ip = __import__("ipaddress").ip_address(destino)
        e_lan = ip.is_private and not ip.is_loopback
    except ValueError:
        e_lan = False
    return {"rodando": True, "destino": destino, "destino_e_lan": bool(e_lan),
            "porta_vps": porta}


def _destino_do_tunel() -> dict:
    """Destino do túnel SSH reverso que ESTE app serve, lido do processo.

    `-R 127.0.0.1:<porta>:<destino>:<porta>`: o `127.0.0.1` é o lado da VPS, o
    destino é o que decide a auth (por loopback a API entra sem chave). Antes da
    #346 o status casava o literal `ssh .*-R 127.0.0.1:7860` — não via o destino e
    quebrava com porta diferente da padrão. Casar QUALQUER `ssh -R` da máquina,
    porém, fazia o status mentir verde com o destino de OUTRO túnel quando ele
    viesse antes na lista (#496): a linha tem de ser a do nosso agente.

    A individualização vem do NOSSO plist (`host` e porta remota do
    ProgramArguments). Sem plist legível — instalação antiga/cópia do script — a
    ordem de preferência é a chave `tts_tunnel` e, no fim, aceitar (o status fica
    cego se recusar tudo, e cego é pior que o falso verde que existia).
    """
    host_alvo, porta_alvo = _args_do_tunel_no_plist()
    por_chave = generico = None
    for linha in _ps_comandos():
        partes_ps = linha.split()
        # executável é `partes_ps[1]` (o `ps -Ao pid=,command=` põe o pid antes):
        # casar por " ssh " perdia o caminho absoluto (`/usr/bin/ssh`).
        if len(partes_ps) < 3 or partes_ps[1].rsplit("/", 1)[-1] != "ssh":
            continue
        if "-R" not in partes_ps:
            continue
        candidato = _candidato_do_tunel(partes_ps)
        if candidato is None:
            continue
        if porta_alvo and candidato[1] != porta_alvo:
            continue
        if host_alvo and host_alvo not in partes_ps:
            continue
        if host_alvo or porta_alvo:
            return _resumo_do_tunel(*candidato)     # casa o nosso plist: é o nosso
        if _usa_a_chave_do_tts(partes_ps):
            por_chave = por_chave or candidato
        else:
            generico = generico or candidato
    if por_chave or generico:
        return _resumo_do_tunel(*(por_chave or generico))
    return {"rodando": False, "destino": "", "destino_e_lan": False, "porta_vps": ""}


def _token_do_plist(caminho: Path) -> str:
    """Valor de `--token-file` no plist (é o que individualiza o conector)."""
    try:
        texto = caminho.read_text(errors="replace")
    except OSError:
        return ""
    m = re.search(r"<string>--token-file</string>\s*<string>([^<]+)</string>", texto)
    return (m.group(1) if m else "").strip()


def _cf_estado() -> dict:
    """O conector do TTS está rodando? Casado pelo TOKEN do próprio plist.

    `pgrep -f "cloudflared tunnel"` casava qualquer conector da máquina — o do
    openwebui fazia o status do TTS dizer "rodando" com o do TTS parado (#346).
    """
    plist = _cf_plist()
    if not plist.exists():
        return {"rodando": False, "motivo": f"sem agente instalado na label {CF_LABEL}",
                "token_file": ""}
    token = _token_do_plist(plist)
    if not token:
        return {"rodando": False, "motivo": "plist sem --token-file (reinstale com cloudflare.sh)",
                "token_file": ""}
    for linha in _ps_comandos():
        partes_ps = linha.split()
        if len(partes_ps) < 3 or partes_ps[1].rsplit("/", 1)[-1] != "cloudflared":
            continue
        if token in partes_ps:
            return {"rodando": True, "motivo": "", "token_file": token}
    return {"rodando": False, "motivo": "conector do TTS parado", "token_file": token}


def _launchd_loaded(label: str) -> bool | None:
    """LaunchAgent carregado? None se não for macOS/sem launchctl."""
    if sys.platform != "darwin" or not hasattr(os, "getuid"):
        return None
    try:
        r = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return None


def _tunnel_launchd_loaded() -> bool | None:
    return _launchd_loaded(TUNNEL_LABEL)


def _cf_launchd_loaded() -> bool | None:
    return _launchd_loaded(CF_LABEL)


def _host_privado(host: str) -> bool:
    """O destino é da própria máquina/LAN? (loopback, RFC1918, link-local, CGNAT)

    Cobre IPv4 e IPv6 (`::1`, `fc00::/7`, `fe80::/10`); a forma com colchetes da URL
    chega aqui já descolchetada pelo `urlsplit().hostname`.

    Sem isso o `?url=` é um scanner da rede interna com oráculo de código/latência
    (#784). Nome que não resolve em IP cai no heurístico de sufixo."""
    import ipaddress

    nome = (host or "").strip().strip("[]").lower()
    if not nome:
        return True
    # NOME que não resolvemos: o heurístico de sufixo pega o caso comum, mas um
    # domínio público apontando para IP privado (ou o contrário) passa — brecha
    # conhecida e declarada (#784).
    if nome in ("localhost",) or nome.endswith((".local", ".lan", ".internal",
                                                ".home.arpa")):
        return True
    try:
        ip = ipaddress.ip_address(nome)
    except ValueError:
        return False                     # nome público (ou DNS que não resolvemos)
    # CGNAT (100.64/10) NÃO entra em `is_private` no 3.12 — a linha é necessária
    return (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified
            or ip in ipaddress.ip_network("100.64.0.0/10"))


import urllib.request  # noqa: E402  (só aqui embaixo; ver uso)


class _SemRedirect(urllib.request.HTTPRedirectHandler):
    """Não segue redirect: senão a allowlist de destino vira porta de entrada."""

    def redirect_request(self, req, fp, codigo, msg, headers, novo):
        return None


def _public_proxy_check(url: str, timeout: float = 6.0) -> dict:
    """Checa a URL pública do proxy a partir daqui (ida e volta completa:
    Mac → VPS → túnel → Mac). Responde {ok, status_code, latency_ms, error}.

    Destino privado/loopback NÃO é seguido: devolve `privado=True` (#784)."""
    url = (url or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "error": "URL inválida (use http/https)"}
    partes = urlsplit(url)
    if _host_privado(partes.hostname or ""):
        return {"ok": False, "privado": True,
                "error": f"destino interno ({partes.hostname}) — não é URL pública"}
    try:
        import urllib.request
        req = urllib.request.Request(f"{url}/health", method="GET")
        req.add_header("User-Agent", "tts-studio-tunnel-check")
        # o python.org framework build não tem CA store próprio — sem certifi a
        # checagem falha com "unable to get local issuer certificate" e a UI
        # mostra a internet como caída (mesmo bug já tratado em _chat_llm)
        import ssl
        try:
            import certifi
            ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:  # noqa: BLE001 — sem certifi, trust store padrão
            ctx = ssl.create_default_context()
        t0 = time.monotonic()
        aberto = urllib.request.build_opener(
            _SemRedirect(), urllib.request.HTTPSHandler(context=ctx))
        with aberto.open(req, timeout=timeout) as resp:
            body = resp.read(200)
            lat = round((time.monotonic() - t0) * 1000)
            ok = resp.status == 200 and b'"ok"' in body
            return {"ok": ok, "status_code": resp.status,
                    "latency_ms": lat, "error": None if ok else "resposta inesperada"}
    except Exception as e:
        return {"ok": False, "latency_ms": None, "error": str(e)[:200]}


@app.get("/api/tunnel/status")
def tunnel_status(request: Request, url: str = ""):
    """Status do acesso pela internet: agentes instalados/carregados (SSH e
    Cloudflare) e checagem fim a fim da URL pública (passada pela UI).

    `destino`/`destino_e_lan` são o que o status ANTES escondia: o túnel pode
    estar de pé apontando para loopback, e aí a auth pública é no-op. O campo
    `conector` diz por que o cloudflared está "parado" (sem agente, plist sem
    token, conector parado) em vez de só um booleano.
    """
    # `?url=` na query é SSRF de graça: o Mac busca o destino e devolve código e
    # latência (#784). Só admin manda destino; o resto usa a URL CONFIGURADA.
    if url.strip():
        _exige_admin(request)
    else:
        url = str(_settings.get("public_url") or "")
    pub = _public_proxy_check(url) if url.strip() else None
    if pub and pub.get("privado"):
        raise HTTPException(
            400, f"{pub.get('error') or 'destino interno'} — para checar uma URL "
                 "pública, defina `public_url` nas Configurações (ou mande `?url=` "
                 "com chave admin)")
    tunel = _destino_do_tunel()
    conector = _cf_estado()
    return {
        "tunnel_running": tunel["rodando"],
        "tunnel_destino": tunel["destino"],
        "destino_e_lan": tunel["destino_e_lan"],
        "launchd_loaded": _tunnel_launchd_loaded(),
        "cloudflared_label": CF_LABEL,
        "cloudflared_installed": _cf_plist().exists(),
        "cloudflared_running": conector["rodando"],
        "cloudflared_motivo": conector["motivo"],
        "cloudflared_loaded": _cf_launchd_loaded(),
        "public_check": pub,
    }


def _tunnel_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{TUNNEL_LABEL}.plist"


def _cf_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{CF_LABEL}.plist"


def _agentes_publicos() -> list[tuple[str, Path]]:
    """Agentes de acesso público instalados nesta máquina (label, plist)."""
    return [(label, plist) for label, plist in
            ((TUNNEL_LABEL, _tunnel_plist()), (CF_LABEL, _cf_plist()))
            if plist.exists()]


def _launchctl(args: list, timeout: int = 10) -> None:
    """launchctl com tratamento comum: erro -> HTTPException 500."""
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout, text=True)
        if r.returncode != 0:
            raise HTTPException(500, f"launchctl: {(r.stderr or r.stdout or 'erro').strip()[:200]}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Falha no launchctl: {e}")


# Parar/reiniciar o túnel mata justamente o processo que está servindo ESTA
# requisição — a resposta chegaria como 502 (o Cloudflare corta a conexão).
# Por isso esses comandos saem com um atraso, fora do caminho da resposta.
_LAUNCHCTL_ATRASO = 1.5
_launchctl_threads: list = []


def _launchctl_adiado(args: list, atraso: float | None = None) -> None:
    """Roda launchctl em thread, depois de `atraso` segundos (default 1.5)."""
    espera = _LAUNCHCTL_ATRASO if atraso is None else atraso

    def alvo():
        if espera:
            time.sleep(espera)
        try:
            subprocess.run(args, capture_output=True, timeout=10)
        except Exception:  # noqa: BLE001
            pass

    t = threading.Thread(target=alvo, daemon=True)
    _launchctl_threads.append(t)
    t.start()


def _tunnel_exige_macos() -> None:
    if sys.platform != "darwin" or not hasattr(os, "getuid"):
        raise HTTPException(400, "Disponível apenas no macOS com o LaunchAgent instalado (tunnel.sh install)")


@app.post("/api/tunnel/restart")
def tunnel_restart(request: Request):
    _exige_admin(request)
    """Reinicia os agentes do acesso pela internet (kickstart). Exige macOS."""
    _tunnel_exige_macos()
    agentes = _agentes_publicos()
    if not agentes:
        raise HTTPException(400, "Nenhum agente instalado — rode ./tunnel.sh install ou ./cloudflare.sh install")
    for label, _plist in agentes:
        _launchctl_adiado(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"])
    return {"ok": True, "msg": "Túnel reiniciado (aguarde ~5s e reavalie o status)"}


@app.post("/api/tunnel/stop")
def tunnel_stop(request: Request):
    _exige_admin(request)
    """Desliga o acesso pela internet: bootout dos agentes (o KeepAlive não
    reergue). A rede local continua valendo."""
    _tunnel_exige_macos()
    agentes = _agentes_publicos()
    if not agentes:
        raise HTTPException(400, "Nenhum agente instalado — rode ./tunnel.sh install ou ./cloudflare.sh install")
    for label, _plist in agentes:
        # adiado: o agente que serve esta requisição morre depois de responder
        _launchctl_adiado(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"])
    return {"ok": True, "msg": "Túnel desligado — a URL pública cai em ~2s"}


@app.post("/api/tunnel/start")
def tunnel_start(request: Request):
    _exige_admin(request)
    """Religa o acesso pela internet: bootstrap dos agentes instalados; agente
    já carregado vai de kickstart (bootstrap em carregado dá erro)."""
    _tunnel_exige_macos()
    agentes = _agentes_publicos()
    if not agentes:
        raise HTTPException(400, "Nenhum agente instalado — rode ./tunnel.sh install ou ./cloudflare.sh install")
    for label, plist in agentes:
        if not _launchd_loaded(label):
            _launchctl(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)])
        elif not (_cf_estado()["rodando"] if label == CF_LABEL else _destino_do_tunel()["rodando"]):
            _launchctl_adiado(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"])
    return {"ok": True, "msg": "Túnel ligado — aguarde ~5s e teste a URL pública"}


# ---------------------------------------------------------------------------
# Encerramento limpo (#358)
#
# O `os._exit(0)` cru não fechava nada: pipeline Live, worker de job, dsh quente e
# peça temporária ficavam para trás, e o boot seguinte só recolhia parte. Aqui o
# teardown é o MESMO nos dois caminhos: `/api/shutdown` (botão/API) e SIGTERM do
# `run.sh` (via `lifespan`). Sempre best-effort e com teto curto — encerrar não
# pode pendurar o processo.
# ---------------------------------------------------------------------------
_ENCERRANDO = threading.Lock()


def _live_encerra_todas(motivo: str = "shutdown") -> int:
    """Fecha todas as sessões Live abertas (idempotente com o `finally` do WS)."""
    with _live_lock:
        sessoes = list(_live_sessions.values())
    for sess in sessoes:
        try:
            _live_solta_slot(sess, motivo)
        except Exception:  # noqa: BLE001
            pass
    try:
        import live_intencao
        live_intencao.fecha_tudo()        # #595: worker do modelo não sobrevive ao app
    except Exception:                     # noqa: BLE001
        pass
    return len(sessoes)


def _chat_dsh_fecha_pool() -> int:
    """Fecha os processos dsh quentes do pool (a Conversa os manteria vivos)."""
    with _chat_dsh_lock:
        quentes = list(_chat_dsh_livres)
        _chat_dsh_livres.clear()
    for _chave, cli in quentes:
        try:
            cli.close()
        except Exception:  # noqa: BLE001
            pass
    return len(quentes)


def _jobs_mata_workers(somente_proprios: bool = True) -> int:
    """SIGTERM no grupo de cada worker de job vivo (sessão própria => killpg).

    Os `.job-*` FICAM: o histórico recarregado no próximo boot aponta para eles e
    as peças já geradas continuam baixáveis.

    SÓ O QUE É DESTE PROCESSO (`owner.pid` igual ao nosso, #423): o diretório é
    compartilhado com a instância de produção e com qualquer suíte rodando no mesmo
    repo. Sem a checagem, o teardown de um teste de `pytest` matava o worker do
    servidor vivo — o job dele morria com "worker morto" no meio da geração.
    `somente_proprios=False` reproduz o comportamento antigo (para diagnóstico).
    """
    meu = os.getpid()
    mortos = 0
    for pid_file in OUTPUTS_DIR.glob(".job-*/worker.pid"):
        dono = _job_owner_pid(pid_file.parent)
        # dono AUSENTE = dir de versão anterior (não atribuível): mantém o
        # comportamento antigo; dono conhecido e diferente do nosso respeita.
        if somente_proprios and dono is not None and dono != meu:
            continue
        try:
            pid = int(pid_file.read_text().strip())
        except (OSError, ValueError):
            continue
        if not _pid_vivo(pid):
            continue
        try:
            os.killpg(pid, signal.SIGTERM)
            mortos += 1
        except OSError:
            pass
    return mortos


def _encerra_tudo(motivo: str = "shutdown") -> dict:
    """Teardown único. Devolve o que fez (vai para o log e para o `/api/status`)."""
    if not _ENCERRANDO.acquire(blocking=False):
        return {"ja_encerrando": True}      # chamado duas vezes (API + lifespan)
    feito = {"motivo": motivo, "live": 0, "dsh": 0, "workers": 0, "hist": False}
    try:
        feito["live"] = _live_encerra_todas(motivo)
        feito["dsh"] = _chat_dsh_fecha_pool()
        feito["workers"] = _jobs_mata_workers()
        try:                      # filho do separador (#534): senão fica órfão com 670 MB
            import separa_voz

            separa_voz.fecha()
        except Exception:         # noqa: BLE001 — encerrar é best-effort
            pass
        try:                      # filho da marca (#657): idem
            import marca_dagua

            marca_dagua.fecha()
        except Exception:         # noqa: BLE001
            pass
        _jobs_hist_salva()
        feito["hist"] = True
        print(f"[shutdown] {motivo}: {feito['live']} sessão(ões) Live, "
              f"{feito['dsh']} dsh, {feito['workers']} worker(s) — histórico salvo",
              file=sys.stderr, flush=True)
    except Exception as exc:  # noqa: BLE001 — encerrar nunca pode levantar
        print(f"[shutdown] falha no teardown: {exc!r}", file=sys.stderr, flush=True)
    return feito


def _marca_prewarm_no_boot() -> None:
    """Sobe o filho da marca no boot. A abertura da sessão chega tarde.

    Um turno antes do `pronto` saía sem marca, e a decisão do dono é marca em
    tudo. Sob pytest o `TestClient` entra neste lifespan: não sobe o torch.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    if not _settings.get("tts_marca_dagua"):
        return
    try:
        import marca_dagua

        marca_dagua.prewarm()
    except Exception:  # noqa: BLE001 — sem marca o servidor segue
        pass


@contextlib.asynccontextmanager
async def _lifespan(_app):
    """Recarrega o histórico no start e faz o teardown limpo no SIGTERM/stop.

    É o que dá ao `run.sh` (que manda SIGTERM no uvicorn) o mesmo encerramento do
    botão: sem isto, matar o app deixava worker, Live e dsh para trás.
    """
    _jobs_hist_carrega()
    # A chave padrão (ou a migrada de `.apikey`/env) existe só na RAM porque o
    # import não grava (#601). Aqui o servidor está DE PÉ e o launcher já reapontou
    # os caminhos, então é o lugar de materializar — e só quando não há arquivo:
    # boot com `.apikeys.json` existente não reescreve nada.
    if not APIKEYS_PATH.exists():
        try:
            _save_apikeys()
        except Exception as exc:  # noqa: BLE001 — sem arquivo o app ainda autentica
            print(f"[apikeys] não materializei a chave no boot: {exc}", flush=True)
    # DEPOIS do uvicorn: `install_signal_handlers()` dele roda no serve e
    # sobrescreveria um handler posto no import. Aqui o nosso fica POR CIMA e
    # encaminha para o dele (guardado em `_RASTRO_DE_SINAL`), preservando o
    # shutdown gracioso.
    globals()["_RASTRO_DE_SINAL"] = _instala_rastro_de_sinal()
    # Boot, não a abertura da sessão: turno com o AudioSeal frio saía sem marca.
    _marca_prewarm_no_boot()
    yield
    await asyncio.get_running_loop().run_in_executor(None, _encerra_tudo, "lifespan")


# O `lifespan` é definido DEPOIS do `FastAPI(...)` (as funções do teardown moram
# aqui embaixo, junto do resto do ciclo de vida): plugar no router é o caminho.
app.router.lifespan_context = _lifespan


@app.post("/api/shutdown")
def shutdown(request: Request):
    """Desliga o servidor (botão na UI). Reiniciar: dois cliques em TTS-STUDIO.command.

    Passa pelo MESMO teardown do SIGTERM antes do `os._exit`: `os._exit` não roda
    lifespan, então sem esta chamada o encerramento limpo não aconteceria.
    """
    _exige_admin(request)

    def _stop():
        time.sleep(0.4)  # deixa a resposta HTTP voltar antes de sair
        _encerra_tudo("api")
        os._exit(0)

    threading.Thread(target=_stop, daemon=True).start()
    return {"ok": True, "msg": "Servidor desligando…"}


# ---------------------------------------------------------------------------
# /api/settings: separação entre chave de USO e administração
#
# Gerar fala é uma coisa; repontar a instalação para outro provedor (e ler a
# credencial dele) é outra. `_SETTINGS_ADMIN` = campos que só admin altera:
# conexões externas (URLs, chaves, toggles de envio remoto) e escolha de modelo
# (que dispara download/load de repo) e o formato do payload remoto
# (`remote_tts_extra` — ele sobrepõe `text`/`speed` de TODO mundo) e as políticas
# de LIMPEZA do `outputs/` (`auto_cleanup*`): uma chave `use` remota mandando
# `auto_cleanup_max_outputs: 1` faz o próximo laço apagar tudo menos 1 arquivo do
# dono (#543). Fora da lista: `remote_tts_voice` e políticas de recurso por
# geração (idle_unload, free_local_on_remote, speech_queue).
#
# Compatibilidade: quem não quer separação nenhuma não muda NADA — sem
# `TTS_ROD_ADMIN_KEY` e sem `role` nas chaves, toda chave válida segue admin.
# ---------------------------------------------------------------------------
_SETTINGS_ADMIN = frozenset({
    "model", "translate_model", "stt_whisper_repo", "live_stt_whisper_repo",
    "remote_tts", "remote_translate", "remote_stt",
    "remote_base_url", "remote_api_key", "remote_tts_url",
    "remote_stt_base_url", "remote_stt_key",
    "remote_tts_model", "remote_translate_model", "remote_stt_model",
    "chat_base_url", "chat_model", "chat_api_key",
    "chat_backend", "chat_backend_live", "chat_dsh_bin", "chat_dsh_profile",
    "chat_dsh_model",
    "chat_dsh_effort",
    # o override é JSON aplicado sobre o corpo de TODOS os trechos: uma chave de
    # uso que manda `{"text": ...}` reescreveria o job alheio (#282)
    "remote_tts_extra",
    # limpeza do `outputs/`: APAGA arquivo do dono (#543)
    "auto_cleanup", "auto_cleanup_minutes", "auto_cleanup_max_outputs",
    # retenção do histórico de operação: APAGA série histórica (#791) — mesma
    # classe dos `auto_cleanup*`, e o efeito só aparece dias depois
    "metrics_ttl_dias",
    # marca d'água: muda o áudio de TODO mundo (#657). A do 1º PCM do Live
    # também: desligar tira a marca de uma saída do TTS.
    "tts_marca_dagua", "live_marca_primeiro_trecho",
    # guarda áudio do dono em disco (#658)
    "speaker_guardar_audio",
    # destino público do túnel: é o que o status checa sem `?url=` (#784)
    "public_url",
    # IDENTIDADE (#786): quem é o dono, com que rigor o gate decide e se ele
    # existe. Uma chave `use` que mexesse aqui viraria "o dono" para o gate e para
    # o "só a minha voz" do Live — ou desligaria os dois.
    "speaker_gate", "speaker_threshold", "speaker_far_alvo", "speaker_anticlone",
    "live_so_minha_voz",
})
_SETTINGS_SECRETS = ("remote_api_key", "remote_stt_key", "chat_api_key")
# marcador da máscara: começa com "•" e nunca é confundido com chave de verdade.
# O POST que devolver a máscara (a UI recarrega o form e reenvia o blob) mantém
# o segredo guardado em vez de gravar "••••1234" por cima dele.
_MASCARA_SETTING = "••••"


def _mascara_setting_secret(v: str) -> str:
    s = str(v or "")
    return _MASCARA_SETTING + (s[-4:] if len(s) >= 4 else "")


def _settings_payload_adm(request, payload: dict) -> tuple[dict, list[str]]:
    """Limpa o payload antes de aplicar: tira máscara de segredo e, para quem não
    é admin, tira os campos administrativos (devolvidos em `ignorados`).

    Não é 403: a UI manda um blob único no "Salvar", então um 403 por causa de um
    campo remote_* travaria também a mudança de `speed`."""
    dados = dict(payload or {})
    for k in _SETTINGS_SECRETS:
        v = dados.get(k)
        if isinstance(v, str) and v.startswith(_MASCARA_SETTING):
            dados.pop(k, None)          # segredo mascarado: mantém o guardado
    if _admin_is_allowed(request):
        return dados, []
    ignorados = []
    for k in list(dados):
        if k in _SETTINGS_ADMIN:
            ignorados.append(k)
            dados.pop(k)
    return dados, ignorados


@app.get("/api/settings")
def get_settings(request: Request):
    st = dict(_settings)
    st["chat_system_default"] = CHAT_SYSTEM  # p/ a UI exibir o preprompt padrão
    st.update(_settings_vista(request))
    return st


def _settings_vista(request) -> dict:
    """Corta o que a requisição não pode VER: segredos de serviços externos.

    Chave de uso (e visitante sem chave admin) não recebe `remote_api_key`,
    `remote_stt_key` nem `chat_api_key` em claro — são credenciais de terceiros,
    não material para gerar fala. Admin/mac continua vendo tudo."""
    adm = _admin_is_allowed(request)
    extra = {"is_admin": adm, "admin_fields": sorted(_SETTINGS_ADMIN)}
    # Extrator de locutor em uso (#606): a tela mostra o fallback em vez de deixar
    # o dono achar que está no ECAPA. `motivo` só sai quando caiu para o legado.
    extra["extrator_efetivo"] = _speaker_modelo_efetivo()
    motivo = _speaker_ecapa_motivo()
    if motivo:
        extra["extrator_motivo"] = motivo
    # EGRESS do filtro `modelo` (#596/F3): a API tem de dizer o que a UI só pinta.
    # O dsh é processo LOCAL — o trecho sai da máquina só quando a rota dele é
    # remota; a resposta diz QUAL é o caso, em vez de um "sai da máquina" fixo.
    cfg_modelo = (_settings.get("live_barge_filtros") or {}).get("modelo") or {}
    egress = None
    if cfg_modelo.get("on"):
        if str(cfg_modelo.get("provedor") or "local").lower() == "dsh":
            remoto = _dsh_rota_remota()
            egress = ("dsh_remoto" if remoto else "dsh_local")
        else:
            egress = "local"
    extra["modelo_egress"] = egress
    if not adm:
        for k in _SETTINGS_SECRETS:
            if _settings.get(k):
                extra[k] = _mascara_setting_secret(_settings[k])
    return extra


def _dsh_rota_remota() -> bool:
    """A rota do dsh configurada sai da máquina? (perfil do `dsh_client`).

    O dsh é processo LOCAL: com perfil local nada sai. Perfil com host/URL
    explícito, ou backend de chat remoto, é egress."""
    # O dsh em si é processo LOCAL (`chat_dsh_bin` + `chat_dsh_profile`): nada sai
    # por ele. O que faz o trecho sair é o BACKEND de chat ser remoto.
    return str(_settings.get("chat_backend") or "").lower() in ("openai", "remote")


def _clamp(value, lo, hi, default):
    try:
        return min(hi, max(lo, float(value)))
    except (TypeError, ValueError):
        return default


def _resolve_duration_s(v, default):
    """None/vazio/0 = duração automática; senão clampa em 0,5–60 s."""
    if v in (None, "", 0, "0"):
        return None
    try:
        return min(60.0, max(0.5, float(v)))
    except (TypeError, ValueError):
        return default


# Vocabulário FECHADO do instruct do OmniVoice (igual ao _resolve_instruct do modelo).
# Qualquer item fora disto quebra a geração no servidor (clone vira voz default),
# então sanitizamos antes de enviar — emoção em texto livre é descartada.
_OMNI_INSTRUCT_VALID = {
    "male", "female", "child", "teenager", "young adult", "middle-aged", "elderly",
    "very low pitch", "low pitch", "moderate pitch", "high pitch", "very high pitch", "whisper",
    "american accent", "british accent", "australian accent", "canadian accent", "indian accent",
    "japanese accent", "korean accent", "portuguese accent", "russian accent", "chinese accent",
}


def _sanitize_instruct(s) -> str:
    """Mantém só tags válidas do OmniVoice (descarta texto livre/emoção)."""
    if not s:
        return ""
    seen, out = set(), []
    for tok in str(s).split(","):
        t = tok.strip().lower()
        if t in _OMNI_INSTRUCT_VALID and t not in seen:
            seen.add(t)
            out.append(t)
    return ", ".join(out)


def _resolve_omni(payload: dict, family: str | None = None) -> dict:
    """Resolve controles de geração p/ todos os backends.

    Campos OmniVoice (num_steps, guidance…) + gen_* multi-backend (temperature,
    top_p, exaggeration, speakers…). instruct: tags fechadas só no OmniVoice.
    `family` opcional — quando o request sobrescreve o modelo.
    """
    fam = family or _current_backend()["family"]
    raw_instruct = payload["instruct"] if payload.get("instruct") is not None \
        else _settings.get("omni_instruct", "")
    if fam == "omnivoice":
        instruct = _sanitize_instruct(raw_instruct)
    else:
        instruct = str(raw_instruct or "").strip()[:500]

    def _pick(key, setting_key, lo, hi, default, as_int=False):
        """payload[key] > settings[setting_key] > default, com clamp."""
        if key in payload and payload[key] is not None:
            v = payload[key]
        elif setting_key in payload and payload[setting_key] is not None:
            v = payload[setting_key]
        else:
            v = _settings.get(setting_key, default)
        n = _clamp(v, lo, hi, default)
        return int(n) if as_int else n

    return {
        # OmniVoice / VoxCPM2
        "num_steps": _pick("num_steps", "omni_num_steps", 4, 64, 16, as_int=True),
        "guidance_scale": _pick("guidance_scale", "omni_guidance_scale", 0.0, 10.0, 2.0),
        "class_temperature": _pick("class_temperature", "omni_class_temperature", 0.0, 2.0, 0.0),
        "position_temperature": _pick("position_temperature", "omni_position_temperature", 0.0, 20.0, 5.0),
        "layer_penalty_factor": _pick("layer_penalty_factor", "omni_layer_penalty_factor", 0.0, 20.0, 5.0),
        "t_shift": _pick("t_shift", "omni_t_shift", 0.0, 1.0, 0.1),
        "denoise": bool(payload["denoise"]) if "denoise" in payload else _settings.get("omni_denoise", True),
        "preprocess_prompt": bool(payload["preprocess_prompt"]) if "preprocess_prompt" in payload else _settings.get("omni_preprocess_prompt", True),
        "postprocess_output": bool(payload["postprocess_output"]) if "postprocess_output" in payload else _settings.get("omni_postprocess_output", True),
        "audio_chunk_duration": _pick("audio_chunk_duration", "omni_audio_chunk_duration", 1.0, 60.0, 15.0),
        "audio_chunk_threshold": _pick("audio_chunk_threshold", "omni_audio_chunk_threshold", 5.0, 120.0, 30.0),
        "instruct": instruct,
        "duration_s": (_resolve_duration_s(payload["duration_s"], _settings["omni_duration_s"])
                       if "duration_s" in payload else _settings["omni_duration_s"]),
        "speed": _clamp(payload.get("speed"), 0.25, 4.0, _settings["speed"]),
        "seed": int(payload["seed"]) if str(payload.get("seed", "")).lstrip("-").isdigit()
                else int(_settings.get("omni_seed", 42)),
        # multi-backend
        "temperature": _pick("temperature", "gen_temperature", 0.0, 2.0, 0.8),
        "top_p": _pick("top_p", "gen_top_p", 0.05, 1.0, 0.95),
        "top_k": _pick("top_k", "gen_top_k", 0, 500, 50, as_int=True),
        "repetition_penalty": _pick("repetition_penalty", "gen_repetition_penalty", 1.0, 2.5, 1.1),
        "max_tokens": _pick("max_tokens", "gen_max_tokens", 64, 8192, 2048, as_int=True),
        "exaggeration": _pick("exaggeration", "gen_exaggeration", 0.0, 2.0, 0.5),
        "cfg_weight": _pick("cfg_weight", "gen_cfg_weight", 0.0, 1.0, 0.5),
        "min_p": _pick("min_p", "gen_min_p", 0.0, 0.5, 0.05),
        "chunk_length": _pick("chunk_length", "gen_chunk_length", 50, 600, 300, as_int=True),
        "speaker": str(payload.get("speaker") or payload.get("gen_speaker")
                       or _settings.get("gen_speaker") or "Ryan"),
        "kokoro_voice": str(payload.get("kokoro_voice") or payload.get("gen_kokoro_voice")
                            or _settings.get("gen_kokoro_voice") or "af_heart"),
        "pocket_voice": str(payload.get("pocket_voice") or payload.get("gen_pocket_voice")
                            or _settings.get("gen_pocket_voice") or "alba"),
        "voxtral_voice": str(payload.get("voxtral_voice") or payload.get("gen_voxtral_voice")
                             or _settings.get("gen_voxtral_voice") or "casual_male"),
        "lfm3_voice": str(payload.get("lfm3_voice") or payload.get("gen_lfm3_voice")
                          or _settings.get("gen_lfm3_voice") or "female"),
    }


@app.post("/api/settings")
def update_settings(request: Request, payload: dict):
    """Aplica as settings de forma ATÔMICA (o corpo faz `_settings[k]=...` campo a
    campo e só grava o disco no fim).

    Sem rollback, o primeiro campo inválido (ex.: `chat_extra` que não é JSON,
    `stt_whisper_repo` sem "whisper") devolvia 400 DEPOIS de aplicar os anteriores
    na RAM, sem nunca chegar ao `_save_settings()` — a config passava a valer na
    hora, sobrevivia ao uso e SUMIA no restart, sem nenhum aviso de que não tinha
    sido gravada. Aqui: falhou, restaura o estado anterior (RAM == disco).

    Chave de uso: campos administrativos são IGNORADOS (não é 403 — a UI manda um
    blob único no "Salvar") e vêm em `admin_ignored`. Máscara de segredo é
    tratada como "mantém o que está guardado".
    """
    payload, ignorados = _settings_payload_adm(request, payload)
    _antes = dict(_settings)
    try:
        _r = _apply_settings(payload)
    except Exception:
        _settings.clear()
        _settings.update(_antes)
        raise
    avisos = []
    if str(_antes.get("omni_precision", "")).lower() != str(_settings.get("omni_precision", "")).lower():
        # Troca de dtype só pegaria no próximo load_model; descarrega o modelo com o
        # dtype antigo agora, senão continua servindo (e segurando ~GB de RAM com) ele.
        # Com job em voo não encosta: _gen_lock está tomado e o reload é lazy de qualquer jeito.
        if not any(j.get("status") in ("running", "queued")
                   for j in _jobs_snapshot()):
            try:
                _unload_local_models(tts=True, stt=False, mt=False, ser=False)
            except Exception as exc:     # noqa: BLE001 — efeito, não validação
                # a config JÁ está gravada; falhar a descarga não pode virar erro do
                # POST (o cliente perderia a informação de que salvou)
                avisos.append(f"não foi possível descarregar o modelo antigo: {exc}")
    # efeitos pós-commit, na ordem: primeiro libera recurso que a config nova tornou
    # redundante, depois sobe o que ela pediu
    try:
        _autofree_local()
    except Exception as exc:             # noqa: BLE001
        avisos.append(f"autofree não rodou: {exc}")
    if tuple(_settings.get(k) for k in _CAMPOS_DHS_BACKEND) != \
            tuple(_antes.get(k) for k in _CAMPOS_DHS_BACKEND):
        # trocou de backend/ajuste do dsh: já sobe o processo em thread, para quem
        # acabou de ligar `dsh` na UI não pagar o boot no 1º turno da Conversa
        _chat_dsh_prewarm("settings")
    # Cópia: `_apply_settings` devolve o dict vivo `_settings` (e `_save_settings`
    # persiste só as chaves de _SETTINGS_DEFAULTS) — não injetar metadado nele.
    out = dict(_r)
    # Mesma vista do GET: quem não é admin não recebe segredo de terceiro em claro
    # nem na resposta do Salvar (a resposta É as settings, não um ack).
    out.update(_settings_vista(request))
    # sempre presente: a UI/storybook não precisa distinguir "sem campo" de "vazio"
    out["admin_ignored"] = sorted(ignorados)
    if avisos:
        out["avisos"] = avisos
    return out


def _voice_path(v) -> Path | None:
    """Path do WAV de `v` — e só se ele ficar DENTRO de `voices/`.

    `VOICES_DIR / f"{v}.wav"` aceita qualquer coisa: `"../fora"` aponta para um
    arquivo fora do diretório de vozes e ele seguia para o worker/remoto como se
    fosse voz cadastrada.

    O critério segue o ARQUIVO de propósito (decisão do PM na #69): só vale o que
    está DENTRO de `voices/` mesmo depois do symlink resolvido. Assim
    `voices/link.wav -> /fora/x.wav` é recusado (o conteúdo vem de fora) e `../fora`
    também; um symlink para dentro de `voices/` continua valendo. Não uso `_safe_id`:
    ele rejeitaria id exótico já registrado (`<img src=x …>`). None = escapa ou não existe."""
    if not v or not isinstance(v, str):
        return None
    p = VOICES_DIR / f"{v}.wav"
    try:
        if p.resolve().parent != VOICES_DIR.resolve():
            return None
    except OSError:
        return None
    return p if p.exists() else None


_LIVE_INTERRUPCAO_MODOS = ("inteligente", "imediata", "so_comando")
# canônico é continua|interrompe; `corta` é o nome que a tela usa (#501)
_LIVE_SEM_DECISAO_ALIAS = {"corta": "interrompe", "cortar": "interrompe",
                           "continua": "continua", "continuar": "continua"}
# Ordem da cadeia (#501): o modelo é o 5º e só é consultado quando os rápidos não
# decidiram. O vocabulário vem FECHADO daqui para o `settings.json` não virar
# contrato aberto.
_LIVE_BARGE_FILTROS = ("palavras_corte", "eco", "identidade", "backchannel", "ruido",
                       "modelo", "pergunta", "frase_longa", "fala_sustentada",
                       "fim_de_resposta")
_LIVE_MAX_PALAVRAS = 100
_LIVE_MAX_PALAVRA_CHARS = 40


def _normaliza_filtro_barge(nome: str, cfg) -> dict:
    """Sanitiza o item de um filtro: `on` booleano e os campos numéricos/listas do
    vocabulário. Chave desconhecida dentro do filtro é descartada (não vira
    contrato) e ausência de campo mantém o que o motor já usa."""
    if not isinstance(cfg, dict):
        return {"on": True}
    fora: dict = {"on": bool(cfg.get("on", True))}
    if "palavras" in cfg:
        palavras = cfg["palavras"]
        if isinstance(palavras, str):
            palavras = [p for p in palavras.replace(",", " ").split() if p]
        if isinstance(palavras, list):
            fora["palavras"] = [str(p).strip()[: _LIVE_MAX_PALAVRA_CHARS]
                                for p in palavras[: _LIVE_MAX_PALAVRAS] if str(p).strip()]
    if "min_palavras" in cfg:
        fora["min_palavras"] = int(_clamp(cfg["min_palavras"], 1, 20, 3))
    if "min_ms" in cfg:
        fora["min_ms"] = int(_clamp(cfg["min_ms"], 150, 5000, 1200))
    if "passo_ms" in cfg:
        fora["passo_ms"] = int(_clamp(cfg["passo_ms"], 50, 1000, 150))
    if "restante_ms" in cfg:
        fora["restante_ms"] = int(_clamp(cfg["restante_ms"], 200, 5000, 2000))
    if nome == "identidade":
        # `perfis` vazio = o perfil do `live_so_minha_voz`; lista explícita manda
        perfis = cfg.get("perfis")
        if isinstance(perfis, str):
            perfis = [p for p in perfis.replace(",", " ").split() if p]
        if isinstance(perfis, list):
            fora["perfis"] = [str(p).strip()[:60] for p in perfis[:8] if str(p).strip()]
        fora["far"] = _clamp(cfg.get("far"), 0.001, 0.2, 0.01)
        fora["on"] = bool(cfg.get("on", False))     # #527: começa DESLIGADO
    if nome == "modelo":
        # OFERTA: só `local` (decisão do PM, #596). O `dsh` saiu — empata com a
        # régua de regras (91,4 %), custa 900 ms de prazo e traz egress. Um
        # settings antigo com `dsh` cai no local, sem erro.
        fora["provedor"] = "local"
        fora["limiar"] = _clamp(cfg.get("limiar"), 0.05, 0.95, 0.5)
        fora["timeout_ms"] = int(_clamp(cfg.get("timeout_ms"), 100, 3000, 400))
        fora["on"] = bool(cfg.get("on", False))     # #509: começa DESLIGADO
    return fora


def _apply_settings(payload: dict):
    # (o "antes" dos campos do dsh é do `update_settings` desde o #282: os efeitos
    # ficaram para depois do commit e é lá que a comparação acontece)
    if "model" in payload:
        m = str(payload["model"] or "").strip()
        if m:
            _settings["model"] = m  # carregado (e baixado/montado) na próxima geração
    if "pre_prompt" in payload:
        _settings["pre_prompt"] = str(payload["pre_prompt"] or "").strip()[:500]
    if "language" in payload:
        _settings["language"] = str(payload["language"] or "auto").lower()[:16]
    if "default_voice" in payload:
        v = payload["default_voice"]
        # `_voice_path` (não `exists()` no caminho cru): "../x" não fica gravado como
        # voz padrão e não contamina todo pedido que não manda voz própria
        _settings["default_voice"] = v if _voice_path(v) else None
    if "chunk_max_chars" in payload:
        _settings["chunk_max_chars"] = int(_clamp(payload["chunk_max_chars"], 60, 200, 140))
    if "speed" in payload:
        _settings["speed"] = _clamp(payload["speed"], 0.25, 4.0, 1.0)
    if "auto_cleanup" in payload:
        _settings["auto_cleanup"] = bool(payload["auto_cleanup"])
    if "live_interrupcao" in payload:
        v = str(payload["live_interrupcao"] or "").strip().lower()
        _settings["live_interrupcao"] = (v if v in _LIVE_INTERRUPCAO_MODOS
                                         else "inteligente")
    if "live_barge_sem_decisao" in payload:
        v = str(payload["live_barge_sem_decisao"] or "").strip().lower()
        # `corta` é ALIAS aceito (a tela usa esse nome) e é gravado canônico:
        # guardar `corta` fazia a cadeia comparar `== "interrompe"` e cair em
        # `continua` em silêncio
        v = _LIVE_SEM_DECISAO_ALIAS.get(v, v)
        _settings["live_barge_sem_decisao"] = (v if v in ("continua", "interrompe")
                                               else "continua")
    if "live_barge_filtros" in payload:
        alvo = payload["live_barge_filtros"]
        if isinstance(alvo, dict):
            # MERGE parcial: o item manda no próprio filtro e não toca nos outros
            atual = dict(_settings.get("live_barge_filtros") or {})
            for nome, cfg in alvo.items():
                if nome in _LIVE_BARGE_FILTROS:
                    atual[nome] = _normaliza_filtro_barge(nome, cfg)
            _settings["live_barge_filtros"] = atual
    if "auto_cleanup_minutes" in payload:
        _settings["auto_cleanup_minutes"] = int(_clamp(payload["auto_cleanup_minutes"], 1, 1440, 15))
    if "auto_cleanup_max_outputs" in payload:
        # 0 = sem teto de quantidade; teto alto o bastante para não ser surpresa
        _settings["auto_cleanup_max_outputs"] = int(
            _clamp(payload["auto_cleanup_max_outputs"], 0, 100000, 500))
    if "omni_num_steps" in payload:
        _settings["omni_num_steps"] = int(_clamp(payload["omni_num_steps"], 4, 64, 16))
    if "live_num_steps" in payload:
        _settings["live_num_steps"] = int(_clamp(payload["live_num_steps"], 4, 64, 16))
    if "omni_guidance_scale" in payload:
        _settings["omni_guidance_scale"] = _clamp(payload["omni_guidance_scale"], 0.0, 10.0, 2.0)
    if "omni_class_temperature" in payload:
        _settings["omni_class_temperature"] = _clamp(payload["omni_class_temperature"], 0.0, 2.0, 0.0)
    if "omni_position_temperature" in payload:
        _settings["omni_position_temperature"] = _clamp(payload["omni_position_temperature"], 0.0, 20.0, 5.0)
    if "omni_layer_penalty_factor" in payload:
        _settings["omni_layer_penalty_factor"] = _clamp(payload["omni_layer_penalty_factor"], 0.0, 20.0, 5.0)
    if "omni_t_shift" in payload:
        _settings["omni_t_shift"] = _clamp(payload["omni_t_shift"], 0.0, 1.0, 0.1)
    for chave in ("omni_denoise", "omni_preprocess_prompt", "omni_postprocess_output"):
        if chave in payload:
            _settings[chave] = bool(payload[chave])
    if "omni_audio_chunk_duration" in payload:
        _settings["omni_audio_chunk_duration"] = _clamp(payload["omni_audio_chunk_duration"], 1.0, 60.0, 15.0)
    if "omni_audio_chunk_threshold" in payload:
        _settings["omni_audio_chunk_threshold"] = _clamp(payload["omni_audio_chunk_threshold"], 5.0, 120.0, 30.0)
    if "omni_instruct" in payload:
        _settings["omni_instruct"] = str(payload["omni_instruct"] or "").strip()[:300]
    if "omni_seed" in payload:
        try:
            _settings["omni_seed"] = max(-1, min(2**31 - 1, int(payload["omni_seed"])))
        except (TypeError, ValueError):
            pass
    if "omni_duration_s" in payload:
        _settings["omni_duration_s"] = _resolve_duration_s(payload["omni_duration_s"], None)
    if "omni_ref_max_s" in payload:
        _settings["omni_ref_max_s"] = _clamp(payload["omni_ref_max_s"], 3.0, 30.0, 10.0)
    if "omni_precision" in payload:
        p = str(payload["omni_precision"] or "bf16").lower()
        _settings["omni_precision"] = p if p in ("fp32", "bf16", "q8", "q4") else "bf16"
    # multi-backend
    if "gen_temperature" in payload:
        _settings["gen_temperature"] = _clamp(payload["gen_temperature"], 0.0, 2.0, 0.8)
    if "gen_top_p" in payload:
        _settings["gen_top_p"] = _clamp(payload["gen_top_p"], 0.05, 1.0, 0.95)
    if "gen_top_k" in payload:
        _settings["gen_top_k"] = int(_clamp(payload["gen_top_k"], 0, 500, 50))
    if "gen_repetition_penalty" in payload:
        _settings["gen_repetition_penalty"] = _clamp(payload["gen_repetition_penalty"], 1.0, 2.5, 1.1)
    if "gen_max_tokens" in payload:
        _settings["gen_max_tokens"] = int(_clamp(payload["gen_max_tokens"], 64, 8192, 2048))
    if "gen_exaggeration" in payload:
        _settings["gen_exaggeration"] = _clamp(payload["gen_exaggeration"], 0.0, 2.0, 0.5)
    if "gen_cfg_weight" in payload:
        _settings["gen_cfg_weight"] = _clamp(payload["gen_cfg_weight"], 0.0, 1.0, 0.5)
    if "gen_min_p" in payload:
        _settings["gen_min_p"] = _clamp(payload["gen_min_p"], 0.0, 0.5, 0.05)
    if "gen_chunk_length" in payload:
        _settings["gen_chunk_length"] = int(_clamp(payload["gen_chunk_length"], 50, 600, 300))
    if "gen_speaker" in payload:
        _settings["gen_speaker"] = str(payload["gen_speaker"] or "Ryan").strip()[:64]
    if "gen_kokoro_voice" in payload:
        _settings["gen_kokoro_voice"] = str(payload["gen_kokoro_voice"] or "af_heart").strip()[:64]
    if "gen_pocket_voice" in payload:
        _settings["gen_pocket_voice"] = str(payload["gen_pocket_voice"] or "alba").strip()[:64]
    if "gen_voxtral_voice" in payload:
        _settings["gen_voxtral_voice"] = str(payload["gen_voxtral_voice"] or "casual_male").strip()[:64]
    if "gen_lfm3_voice" in payload:
        voz = str(payload["gen_lfm3_voice"] or "female").strip().lower()[:16]
        _settings["gen_lfm3_voice"] = voz if voz in ("female", "male") else "female"
    if "voice_denoise" in payload:
        _settings["voice_denoise"] = bool(payload["voice_denoise"])
    if "voice_denoise_strength" in payload:
        _settings["voice_denoise_strength"] = _clamp(payload["voice_denoise_strength"], 0.0, 1.0, 0.7)
    if "voice_limpar_motor" in payload:
        motor = str(payload["voice_limpar_motor"]).lower()
        _settings["voice_limpar_motor"] = motor if motor in ("dfn3", "gating") else "dfn3"
    if "voice_limpar_musica" in payload:
        _settings["voice_limpar_musica"] = _truthy(payload["voice_limpar_musica"])
    if "voice_limpar_dereverb" in payload:
        _settings["voice_limpar_dereverb"] = _truthy(payload["voice_limpar_dereverb"])
    if "voice_timbre_min" in payload:
        _settings["voice_timbre_min"] = _clamp(payload["voice_timbre_min"], 0.0, 1.0, 0.80)
    if "audio_gain_db" in payload:
        _settings["audio_gain_db"] = _clamp(payload["audio_gain_db"], -15.0, 15.0, 0.0)
    # cache de áudio por texto (#325): desligado por padrão
    if "tts_cache" in payload:
        _settings["tts_cache"] = bool(payload["tts_cache"])
    if "tts_cache_ttl_s" in payload:
        _settings["tts_cache_ttl_s"] = int(_clamp(payload["tts_cache_ttl_s"], 0, 86400, 3600))
    if "tts_cache_max" in payload:
        _settings["tts_cache_max"] = int(_clamp(payload["tts_cache_max"], 1, 20000, 500))
    for chave, lim in (("audio_eq_low_db", 12.0), ("audio_eq_mid_db", 12.0), ("audio_eq_high_db", 12.0)):
        if chave in payload:
            _settings[chave] = _clamp(payload[chave], -lim, lim, 0.0)
    if "stt_min_words" in payload:
        _settings["stt_min_words"] = int(_clamp(payload["stt_min_words"], 0, 10, 1))
    if "stt_min_chars" in payload:
        _settings["stt_min_chars"] = int(_clamp(payload["stt_min_chars"], 0, 40, 2))
    if "stt_max_no_speech" in payload:
        _settings["stt_max_no_speech"] = _clamp(payload["stt_max_no_speech"], 0.0, 1.0, 0.6)
    if "stt_min_logprob" in payload:
        _settings["stt_min_logprob"] = _clamp(payload["stt_min_logprob"], -5.0, 0.0, -1.0)
    if "stt_max_compression" in payload:
        _settings["stt_max_compression"] = _clamp(payload["stt_max_compression"], 1.0, 10.0, 2.4)
    if "stt_local_engine" in payload:
        eng = str(payload["stt_local_engine"] or "whisper").strip().lower()
        if eng not in ("whisper", "parakeet"):
            raise HTTPException(400, "stt_local_engine inválido (whisper|parakeet)")
        _settings["stt_local_engine"] = eng
    if "stt_whisper_repo" in payload:
        repo = str(payload["stt_whisper_repo"] or "").strip()[:200]
        if repo and (not re.fullmatch(r"[\w.\-/]+", repo) or "whisper" not in repo.lower()):
            raise HTTPException(400, "stt_whisper_repo inválido (esperado repo HF do whisper)")
        _settings["stt_whisper_repo"] = repo
    if "live_stt_whisper_repo" in payload:
        repo = str(payload["live_stt_whisper_repo"] or "").strip()[:200]
        if repo and (not re.fullmatch(r"[\w.\-/]+", repo) or "whisper" not in repo.lower()):
            raise HTTPException(400, "live_stt_whisper_repo inválido (esperado repo HF do whisper)")
        _settings["live_stt_whisper_repo"] = repo
    if "stt_beam" in payload:
        _settings["stt_beam"] = int(_clamp(payload["stt_beam"], 1, 10, 5))
    # Histórico de operação (#376): 1..365 dias (0 apaga tudo na próxima poda) e
    # 1..64 MB por arquivo antes de abrir sufixo `-2`, `-3`…
    if "metrics_ttl_dias" in payload:
        _settings["metrics_ttl_dias"] = int(_clamp(payload["metrics_ttl_dias"], 0, 365, 14))
    if "metrics_teto_mb" in payload:
        _settings["metrics_teto_mb"] = int(_clamp(payload["metrics_teto_mb"], 1, 64, 8))
    for chave in ("stt_anti_ruido", "stt_denoise"):
        if chave in payload:
            _settings[chave] = bool(payload[chave])
    for chave in ("remote_tts", "remote_translate", "remote_stt"):
        if chave in payload:
            _settings[chave] = bool(payload[chave])
    if "remote_tts_url" in payload:
        _settings["remote_tts_url"] = str(payload["remote_tts_url"] or "").strip()[:300]
    if "remote_tts_voice" in payload:
        _settings["remote_tts_voice"] = str(payload["remote_tts_voice"] or "").strip()[:120]
    if "remote_tts_extra" in payload:
        _settings["remote_tts_extra"] = str(payload["remote_tts_extra"] or "").strip()[:2000]
    if "remote_base_url" in payload:
        _settings["remote_base_url"] = str(payload["remote_base_url"] or "").strip()[:300]
    if "remote_api_key" in payload:
        _settings["remote_api_key"] = str(payload["remote_api_key"] or "").strip()[:300]
    if "remote_stt_base_url" in payload:
        _settings["remote_stt_base_url"] = str(payload["remote_stt_base_url"] or "").strip()[:300]
    if "remote_stt_key" in payload:
        _settings["remote_stt_key"] = str(payload["remote_stt_key"] or "").strip()[:300]
    if "chat_base_url" in payload:
        _settings["chat_base_url"] = str(payload["chat_base_url"] or "").strip()[:300]
    if "chat_model" in payload:
        _settings["chat_model"] = str(payload["chat_model"] or "").strip()[:80]
    if "chat_api_key" in payload:
        _settings["chat_api_key"] = str(payload["chat_api_key"] or "").strip()[:300]
    if "chat_system" in payload:
        _settings["chat_system"] = str(payload["chat_system"] or "").strip()[:4000]
    if "chat_extra" in payload:
        txt = str(payload["chat_extra"] or "").strip()[:2000]
        if txt:  # valida: precisa ser JSON objeto
            try:
                if not isinstance(json.loads(txt), dict):
                    raise ValueError
            except (ValueError, TypeError):
                raise HTTPException(400, "Extras do LLM: informe um JSON válido de objeto, ex. {\"reasoning_effort\": \"low\"}")
        _settings["chat_extra"] = txt
    if "chat_backend" in payload:
        b = str(payload["chat_backend"] or "openai").strip().lower()
        if b not in ("openai", "dsh"):
            raise HTTPException(400, "chat_backend inválido (openai|dsh)")
        _settings["chat_backend"] = b
    if "chat_backend_live" in payload:
        # vazio é VÁLIDO e significa "herda o global" (#176)
        bl = str(payload["chat_backend_live"] or "").strip().lower()
        if bl and bl not in ("openai", "dsh"):
            raise HTTPException(400, "chat_backend_live inválido (vazio=herda|openai|dsh)")
        _settings["chat_backend_live"] = bl
    if "chat_dsh_bin" in payload:
        _settings["chat_dsh_bin"] = str(payload["chat_dsh_bin"] or "dsh").strip()[:200] or "dsh"
    if "chat_dsh_profile" in payload:
        _settings["chat_dsh_profile"] = \
            str(payload["chat_dsh_profile"] or "").strip()[:80] or dsh_client.DSH_DEFAULT_PROFILE
    if "chat_dsh_model" in payload:
        m = str(payload["chat_dsh_model"] or "").strip()[:200]
        if m and not dsh_client.dsh_model_valido(m):
            raise HTTPException(400, 'chat_dsh_model inválido: use o par JSON do catálogo, '
                                     'ex. ["dsflash","deepseek-flash-41"]')
        _settings["chat_dsh_model"] = m or dsh_client.DSH_DEFAULT_MODEL
    if "chat_dsh_effort" in payload:
        e = str(payload["chat_dsh_effort"] or "off").strip().lower()
        if e not in dsh_client.DSH_EFFORTS:
            raise HTTPException(400, "chat_dsh_effort inválido (off|low|high|max)")
        _settings["chat_dsh_effort"] = e
    if "speaker_gate" in payload:
        g = str(payload["speaker_gate"] or "off").strip().lower()
        if g not in ("off", "enforce", "label"):
            raise HTTPException(400, "speaker_gate inválido (off|enforce|label)")
        _settings["speaker_gate"] = g
    if "speaker_threshold" in payload:
        _settings["speaker_threshold"] = _clamp(payload["speaker_threshold"], 0.35, 0.9, 0.6)
    if "speaker_far_alvo" in payload:
        v = str(payload["speaker_far_alvo"] or "").strip().replace(",", ".")
        _settings["speaker_far_alvo"] = v if v in ("1", "0.1") else ""
    if "tts_marca_dagua" in payload:
        v = payload["tts_marca_dagua"]
        _settings["tts_marca_dagua"] = (v if isinstance(v, bool)
                                        else str(v).strip().lower() in ("1", "true", "on"))
    if "live_marca_primeiro_trecho" in payload:
        v = payload["live_marca_primeiro_trecho"]
        _settings["live_marca_primeiro_trecho"] = (v if isinstance(v, bool)
                                                   else str(v).strip().lower() in ("1", "true", "on"))
    if "speaker_guardar_audio" in payload:
        v = payload["speaker_guardar_audio"]
        _settings["speaker_guardar_audio"] = (v if isinstance(v, bool)
                                              else str(v).strip().lower() in ("1", "true", "on"))
    if "speaker_anticlone" in payload:
        # booleano por aceitação explícita: "false"/"0"/"" desligam de verdade
        v = payload["speaker_anticlone"]
        _settings["speaker_anticlone"] = (v if isinstance(v, bool)
                                          else str(v).strip().lower() in ("1", "true", "on"))
    if "live_so_minha_voz" in payload:
        # "só a minha voz" no Live (#533): bloco próprio, só as chaves conhecidas
        # (o resto do bloco fica como está — a UI manda o objeto montado).
        cfg = payload["live_so_minha_voz"]
        if isinstance(cfg, dict):
            atual = dict(_settings.get("live_so_minha_voz") or {})
            for k, v in cfg.items():
                if k in ("on", "separacao"):
                    atual[k] = (v if isinstance(v, bool)
                                else str(v).strip().lower() in ("1", "true", "on"))
                elif k == "perfil":
                    atual[k] = str(v or "").strip()[:60]
                elif k == "limiar":
                    atual[k] = _clamp(v, 0.0, 9.9, 0.0)   # 0 = usa a tabela de FAR
                elif k == "rigor_far":
                    v = str(v or "").strip().replace(",", ".")
                    # "1.8" é o joelho medido no corpus do R4 (#533)
                    atual[k] = (v if v in ("1", "0.1") else _VOZ_RIGOR_JOELHO
                                if v in ("joelho", "1.8") else _VOZ_RIGOR_PADRAO)
            _settings["live_so_minha_voz"] = atual
    for chave in ("remote_tts_model", "remote_translate_model", "remote_stt_model"):
        if chave in payload:
            _settings[chave] = str(payload[chave] or "").strip()[:120]
    if "translate_model" in payload:   # repo MLX do tradutor local (recarrega sob demanda)
        _settings["translate_model"] = str(payload["translate_model"] or "").strip()[:120]
    if "free_local_on_remote" in payload:
        _settings["free_local_on_remote"] = bool(payload["free_local_on_remote"])
    if "idle_unload_minutes" in payload:
        # 0 = nunca descarrega por ociosidade; máx. 24 h
        _settings["idle_unload_minutes"] = int(_clamp(payload["idle_unload_minutes"], 0, 1440, 10))
    if "whisper_arquivo_ttl_s" in payload:
        # 0 = o large-v3 de arquivo fica residente; máx. 24 h. Não é admin:
        # mesma classe do idle_unload (política de RAM, não de modelo).
        _settings["whisper_arquivo_ttl_s"] = int(
            _clamp(payload["whisper_arquivo_ttl_s"], 0, 86400, 600))
    if "speech_queue" in payload:
        _settings["speech_queue"] = bool(payload["speech_queue"])
    if "speech_queue_gap_s" in payload:
        _settings["speech_queue_gap_s"] = _clamp(payload["speech_queue_gap_s"], 0.0, 5.0, 0.35)
    _save_settings()
    # EFEITOS ficam para o `update_settings`, DEPOIS do commit (#282): disparados
    # aqui dentro, um erro posterior (ex.: descarga de modelo) já os tinha feito e o
    # rollback só devolvia a RAM — o efeito colateral ficava.
    return _settings


@app.get("/api/voices")
def list_voices():
    voices = []
    existentes = set()
    for meta_file in sorted(VOICES_DIR.glob("*.json")):
        try:
            m = json.loads(meta_file.read_text())
            if not isinstance(m, dict) or not m.get("id"):
                continue
        except Exception:  # noqa: BLE001 — meta corrompido não pode derrubar a API
            continue
        # a tela só oferece "desfazer" quando existe o que desfazer (#443); o id
        # já passou pelo glob de .json, mas `_safe_id` é quem manda no caminho
        try:
            m["tem_original"] = (VOICES_DIR / f"{_safe_id(m['id'])}.orig.wav").exists()
        except HTTPException:
            m["tem_original"] = False
        voices.append(m)
        existentes.add(m["id"])
    voices.sort(key=lambda v: v.get("created_at", ""), reverse=True)
    # presets ainda não materializados entram como entradas virtuais ao final
    for pid, p in OMNI_PRESETS.items():
        if pid not in existentes:
            voices.append({"id": pid, "name": p["name"], "preset": True,
                           "materialized": False, "instruct": p["instruct"],
                           "duration": 0, "created_at": "", "tem_original": False})
    return voices


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "on", "yes", "sim")


@app.post("/api/voices/design")
def save_design_voice(payload: dict):
    """Materializa uma voz de VOICE DESIGN (instruct + seed) numa voz SALVA/nomeada.

    Gera uma amostra-semente com a descrição+seed e a salva como voz de referência
    (clone), igual aos presets. A voz passa a aparecer no /api/voices e a ser usável
    por id/nome (inclusive na API), com timbre estável (clonagem da amostra).
    """
    import numpy as np

    name = (payload.get("name") or "").strip()
    instruct = _sanitize_instruct(payload.get("instruct") if payload.get("instruct")
                                  else _settings.get("omni_instruct") or "")
    if not name:
        raise HTTPException(400, "name obrigatório")
    if not instruct:
        raise HTTPException(400, "instruct obrigatório (descrição da voz, ex.: 'female, young adult, high pitch')")
    seed = (int(payload["seed"]) if str(payload.get("seed", "")).lstrip("-").isdigit()
            else int(_settings.get("omni_seed", 42)))

    omni = {"num_steps": OMNI_STEPS_HQ, "guidance_scale": _settings["omni_guidance_scale"],
            "class_temperature": _settings["omni_class_temperature"],
            "position_temperature": _settings["omni_position_temperature"],
            "layer_penalty_factor": _settings["omni_layer_penalty_factor"],
            "t_shift": _settings["omni_t_shift"], "instruct": instruct,
            "duration_s": None, "speed": 1.0, "seed": seed}

    remote = _use_remote_tts()
    sr = 24000
    try:
        with (_NO_LOCK if remote else _gen_lock):
            if remote:
                audio = _tts_remote_chunk(OMNI_PRESET_SEED, _settings["language"], omni, sr, None)
            else:
                model = _get_model()
                sr = getattr(model, "sample_rate", 24000)
                audio = _generate_chunk(model, OMNI_PRESET_SEED, _settings["language"],
                                        None, None, omni, sr=sr)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Falha ao gerar a amostra do design: {repr(e)[:200]}")

    audio = _normalize(_trim_tail_silence(np.asarray(audio, dtype=np.float32), sr))
    voice_id = uuid.uuid4().hex[:10]
    # SEM marca d'água DE PROPÓSITO (#845): esta amostra é REFERÊNCIA, não saída.
    # Ela condiciona o clone — o modelo copia o timbre dela —, então marcar aqui
    # enfia a marca na linhagem da voz e todo áudio futuro herda uma marca que
    # ninguém pediu, sem que a detecção melhore (a saída já sai marcada no job).
    # Quem for "consertar" a ausência daqui precisa saber disto: o gancho é este
    # `sf.write`, e o par dele no caminho remoto é `_tts_remote_chunk` (que também
    # devolve áudio para o job marcar, não para a referência).
    _grava_wav_atomico(VOICES_DIR / f"{voice_id}.wav", audio, sr)
    meta = {"id": voice_id, "name": name, "from_design": True,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "duration": round(len(audio) / sr, 1), "ref_text": OMNI_PRESET_SEED,
            "instruct": instruct, "seed": seed}
    write_json_atomic(VOICES_DIR / f"{voice_id}.json", meta)
    return meta


@app.post("/api/voices")
def create_voice(name: str = Form(...), audio: UploadFile = None, ref_text: str = Form(""),
                 denoise: str = Form("1"), denoise_strength: str = Form("")):
    if audio is None:
        raise HTTPException(400, "Áudio obrigatório")
    import soundfile as sf

    voice_id = uuid.uuid4().hex[:10]
    wav_path = VOICES_DIR / f"{voice_id}.wav"
    tmp = VOICES_DIR / f".up-{voice_id}"
    _write_upload_limited(audio, tmp)
    try:
        data, sr = sf.read(str(tmp), dtype="float32")
    except Exception:
        tmp.unlink(missing_ok=True)
        raise HTTPException(400, "Áudio inválido")
    tmp.unlink(missing_ok=True)

    # limpa o ruído de fundo NA FONTE: a amostra salva (e os ref_tokens do clone)
    # passam a ser a versão limpa
    do_denoise = _truthy(denoise)
    try:
        strg = float(denoise_strength)
    except (TypeError, ValueError):
        strg = float(_settings.get("voice_denoise_strength", 0.7))
    strg = _clamp(strg, 0.0, 1.0, 0.7)
    recusado = False
    relatorio: dict = {}
    if do_denoise:
        data, recusado, relatorio = _denoise_para_referencia(data, sr, strg)
    data = _referencia_pronta(data, sr)      # nível + fade: o clone copia isso
    _grava_wav_atomico(wav_path, data, sr)

    duration = _wav_duration(wav_path)
    if duration < 3:
        wav_path.unlink(missing_ok=True)
        raise HTTPException(400, f"Gravação muito curta ({duration}s). Mínimo 3s, ideal 10–30s.")

    meta = {
        "id": voice_id,
        # teto do nome: ele é o único campo livre do meta e vai para a UI
        "name": (name.strip() or voice_id)[:120],
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration": duration,
        "denoised": bool(do_denoise and not recusado),
        "denoise_recusado": bool(recusado),
    }
    if relatorio:
        # motor usado, similaridade de timbre e motivo da recusa ficam no meta
        meta["limpeza"] = relatorio
    # transcrição opcional da amostra: clonagem do OmniVoice fica mais estável
    if ref_text.strip():
        meta["ref_text"] = ref_text.strip()[:500]
    write_json_atomic(VOICES_DIR / f"{voice_id}.json", meta)
    return meta


#: prefixo dos temporários DENTRO de `voices/` (#690). Inconfundível de propósito:
#: `voices/` é o dado insubstituível do dono e a poda APAGA ali — o prefixo tem de
#: não casar `<id>.wav`, `<id>.json`, `<id>.orig.wav` nem id com ponto no nome.
_TMP_VOICES_PREFIX = ".tmp-tts-"

#: Watchdog do worker isolado (#800). Sem isto um filho travado deixa o laço de
#: polling girando para SEMPRE segurando `_gen_lock` — e o app inteiro para de
#: gerar. Dois cortes: o job morre se o `status.json` não muda por `_JOB_PARADO_S`
#: (peça pequena não passa disso) e, de qualquer forma, no teto duro.
_JOB_PARADO_S = float(os.environ.get("TTS_JOB_PARADO_S", "180"))
_JOB_TETO_S = float(os.environ.get("TTS_JOB_TETO_S", "3600"))


def _guarda_original(voice_id: str) -> None:
    """Cópia intocável da referência ANTES da primeira sobrescrita (#443).

    `denoise` e `replace` são `use` por decisão (consumo não apaga voz), mas
    gravam NO LUGAR: com uma chave `use` num aparelho da LAN a voz clonada do
    dono se descaracterizava sem volta. O `.orig.wav` é criado uma vez e nunca
    reescrito — sobrescrever de novo não o toca."""
    origem = VOICES_DIR / f"{voice_id}.wav"
    destino = VOICES_DIR / f"{voice_id}.orig.wav"
    if destino.exists() or not origem.exists():
        return
    # RESERVA com O_EXCL: dois `denoise` concorrentes na mesma voz viam
    # `exists() == False`, os dois copiavam e o último replace vencia — se um write
    # entrasse no meio, o "original" guardado virava áudio JÁ limpo. Quem perde o
    # `O_EXCL` desiste e não sobrescreve.
    try:
        os.close(os.open(destino, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
    except FileExistsError:
        return
    tmp = VOICES_DIR / f"{_TMP_VOICES_PREFIX}orig-{voice_id}-{uuid.uuid4().hex[:8]}"
    try:
        shutil.copy2(origem, tmp)
        os.replace(tmp, destino)
    except Exception:  # noqa: BLE001 — sem cópia, a reserva vazia não serve
        tmp.unlink(missing_ok=True)
        destino.unlink(missing_ok=True)
        raise


def _copia_atomica(origem: Path, destino: Path) -> None:
    """Copia `origem` sobre `destino` sem publicar arquivo pela metade (#795-bis).

    Temporário de nome ÚNICO no diretório de DESTINO (mesmo sistema de arquivos,
    senão o `os.replace` vira cópia) e troca por `os.replace`. Dois restaurações
    simultâneas da mesma voz deixam de compartilhar o mesmo temporário — era o
    defeito do nome fixo `.rest-{id}`."""
    # arquivo vivo com `uchg` recusa o `os.replace` (rename de imutável): limpa a
    # flag antes, como o dono faria à mão (#721), e devolve 0600 sem flag depois.
    # Limpar ANTES é seguro no caminho de erro: se a cópia/o replace falhar o
    # conteúdo anterior fica intacto, só a flag sai — e o 0600 vem do próprio #721,
    # não é aperto novo. `chflags`/`st_flags` são de BSD/macOS: em Linux o `if` é
    # falso e a limpeza some CALADA (lá o imutável é `chattr +i`, outro mecanismo);
    # o `hasattr` evita `AttributeError` se algum dia `st_flags` existir sem
    # `chflags` — `except OSError` NÃO pega `AttributeError`.
    try:
        if (destino.exists() and hasattr(os, "chflags")
                and getattr(destino.stat(), "st_flags", 0) & 0x2):
            os.chflags(destino, 0)
    except OSError:
        pass
    tmp = destino.parent / f"{_TMP_VOICES_PREFIX}copia-{destino.name}-{uuid.uuid4().hex[:8]}"
    try:
        shutil.copy2(origem, tmp)
        os.replace(tmp, destino)
        try:
            destino.chmod(0o600)      # mesmo modo que o #721 deixava
        except OSError:
            pass
    finally:
        tmp.unlink(missing_ok=True)


def _grava_wav_atomico(destino: Path, data, sr: int) -> None:
    """Grava PCM novo sem publicar arquivo pela metade (#795-bis).

    Voz NOVA não tem referência anterior para corromper, mas um leitor concorrente
    (listagem, prévia, geração) pegava o `.wav` em construção. Mesmo desenho do
    `_copia_atomica`: temporário único ao lado do destino e `os.replace`."""
    import soundfile as sf

    # sufixo `.wav` no FIM: o soundfile tira o formato da extensão
    tmp = destino.parent / (f"{_TMP_VOICES_PREFIX}novo-{destino.stem}-"
                            f"{uuid.uuid4().hex[:8]}.wav")
    try:
        sf.write(str(tmp), data, sr, subtype="PCM_16")
        os.replace(tmp, destino)
    finally:
        tmp.unlink(missing_ok=True)


def _com_dnsmos_motivo(relatorio: dict) -> dict:
    """Marca o DNSMOS como NÃO MEDIDO só quando os modelos faltam (#523, #655).

    Sem os ONNX, os dois campos vêm `null` — e `null` sozinho a tela lê como "0",
    mostrando nota mínima. `dnsmos_motivo: "indisponivel"` manda escrever "não
    medido". Com os modelos instalados (o caminho normal agora) o motivo não sai,
    e uma falha de execução aparece como `erro` em vez de se passar por ausência."""
    if relatorio.get("dnsmos_antes") is None and relatorio.get("dnsmos_depois") is None:
        relatorio["dnsmos_motivo"] = ("indisponivel" if not _dnsmos_disponivel()
                                      else "erro")
    return relatorio


def _dnsmos_header(valor) -> str:
    """Nota OVRL em texto para header (vazio = não medido)."""
    try:
        return f"{float(valor['ovrl']):.2f}" if isinstance(valor, dict) else ""
    except (TypeError, ValueError, KeyError):
        return ""


@app.post("/api/voices/{voice_id}/denoise")
def denoise_voice(voice_id: str, payload: dict = None, previa: bool = False):
    """Limpa o ruído de fundo de uma voz JÁ existente, sobrescrevendo o .wav. O
    mtime muda -> o cache de ref_tokens invalida sozinho na próxima geração.

    `?previa=true` NÃO escreve nada (nem o `.orig` do #443): devolve 200 com WAV da
    versão limpa e os números em header (`X-Timbre`, `X-Dnsmos-Antes`,
    `X-Dnsmos-Depois`), ou o ORIGINAL com `X-Recusado: 1` e `X-Motivo`. A tela toca
    original × limpa em paralelo e só aplica quando o dono aprovar (#690)."""
    import io

    import soundfile as sf

    voice_id = _safe_id(voice_id)
    path = VOICES_DIR / f"{voice_id}.wav"
    if not path.exists():
        raise HTTPException(404, "Voz não encontrada")
    strg = _clamp((payload or {}).get("strength"), 0.0, 1.0,
                  float(_settings.get("voice_denoise_strength", 0.7)))
    data, sr = sf.read(str(path), dtype="float32")
    data, recusado, relatorio = _denoise_para_referencia(data, sr, strg)
    relatorio = _com_dnsmos_motivo(relatorio)
    if previa:
        audivel = data if recusado else _referencia_pronta(data, sr)
        buf = io.BytesIO()
        sf.write(buf, audivel, sr, format="WAV", subtype="PCM_16")
        cab = {"X-Recusado": "1" if recusado else "0",
               "X-Motivo": relatorio.get("recusado_por") or "",
               "X-Timbre": "" if relatorio.get("timbre") is None
               else f"{float(relatorio['timbre']):.3f}",
               "X-Dnsmos-Antes": _dnsmos_header(relatorio.get("dnsmos_antes")),
               "X-Dnsmos-Depois": _dnsmos_header(relatorio.get("dnsmos_depois")),
               "X-Dnsmos-Motivo": relatorio.get("dnsmos_motivo") or ""}
        return Response(content=buf.getvalue(), media_type="audio/wav", headers=cab)
    if recusado:
        # RECUSA é estado ESPERADO, não erro do cliente (#523, decisão do PM):
        # 200 com o motivo e o relatório; a amostra original fica intacta (nada é
        # escrito antes daqui). 4xx é só para voz inexistente/permissão.
        return {"ok": True, "recusado": True,
                "motivo": relatorio.get("recusado_por") or "piorou",
                "duration": _wav_duration(path), "limpeza": relatorio}
    data = _referencia_pronta(data, sr)
    _guarda_original(voice_id)          # antes de sobrescrever (#443)
    # troca ATÔMICA (#690): com a prévia lendo o mesmo `.wav` em paralelo, gravar
    # por cima deixava o outro lado lendo arquivo pela metade (cabeçalho truncado).
    # sufixo único: duas aplicações simultâneas da MESMA voz não compartilham o
    # temporário (a última troca vence, nenhuma lê a outra pela metade)
    tmp_denoise = VOICES_DIR / f"{_TMP_VOICES_PREFIX}denoise-{voice_id}-{uuid.uuid4().hex[:8]}.wav"
    sf.write(str(tmp_denoise), data, sr, subtype="PCM_16")
    os.replace(tmp_denoise, path)
    _conds_cache.clear()
    _cache_limpa()           # a amostra mudou: todo áudio dela em cache caiu
    duration = _wav_duration(path)
    jp = VOICES_DIR / f"{voice_id}.json"
    if jp.exists():
        try:
            m = json.loads(jp.read_text())
            m["duration"] = duration
            m["denoised"] = True
            m["limpeza"] = relatorio          # motor, timbre e recusa vão para o meta
            write_json_atomic(jp, m)
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "recusado": False, "duration": duration, "limpeza": relatorio}


@app.post("/api/voices/{voice_id}/restore-original")
def restore_voice_original(request: Request, voice_id: str):
    """Devolve a referência como era antes do primeiro denoise/replace (#443).

    O `.orig.wav` é guardado na primeira sobrescrita; restaurar devolve o arquivo
    byte a byte e re-invalida o clone em cache. O `.orig` NÃO é apagado — dá para
    restaurar de novo depois de outra edição."""
    _exige_admin(request)
    voice_id = _safe_id(voice_id)
    orig = VOICES_DIR / f"{voice_id}.orig.wav"
    if not orig.exists():
        raise HTTPException(404, "Esta voz não tem original guardado")
    _copia_atomica(orig, VOICES_DIR / f"{voice_id}.wav")
    _conds_cache.clear()
    _cache_limpa()
    duration = _wav_duration(VOICES_DIR / f"{voice_id}.wav")
    jp = VOICES_DIR / f"{voice_id}.json"
    if jp.exists():
        try:
            m = json.loads(jp.read_text())
            m["duration"] = duration
            m.pop("denoised", None)          # voltou ao original
            write_json_atomic(jp, m)
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "duration": duration}


@app.get("/api/voices/{voice_id}/audio")
def voice_audio(voice_id: str):
    path = VOICES_DIR / f"{_safe_id(voice_id)}.wav"
    if not path.exists():
        raise HTTPException(404, "Voz não encontrada")
    return FileResponse(path, media_type="audio/wav")


@app.get("/api/voices/export")
def export_voices(request: Request):
    _exige_admin(request)
    """Backup: zip com todas as vozes (.wav + .json). Botão '⬇ Backup' na UI.

    voices/ é gitignored e é o dado mais valioso do app (gravações + presets
    materializados) — sem isso, apagar o dir perde as vozes para sempre.

    STREAM de disco: antes o zip era montado num `BytesIO` e devolvido via
    `getvalue()` — o zip inteiro DUAS vezes na RAM (buffer + cópia do corpo), com
    cada .wav lido por inteiro pelo `z.write`. Com o teto de 512 MB anunciado no
    import isso chega perto de 1 GB de pico numa máquina que já carrega modelo.
    Agora grava num arquivo temporário e o `FileResponse` envia de lá, com o
    unlink no background (o import já usa o mesmo padrão de temporário).
    """
    import zipfile
    from starlette.background import BackgroundTask

    tmp = tempfile.NamedTemporaryFile(prefix=".vozes-", suffix=".zip",
                                      dir=OUTPUTS_DIR, delete=False)
    tmp.close()
    path = Path(tmp.name)
    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(VOICES_DIR.iterdir()):
                # só wav/json de voz; ignora temporários de upload (.up-*/.rep-*)
                if p.is_file() and p.suffix in (".wav", ".json") and not p.name.startswith("."):
                    z.write(p, f"voices/{p.name}")
    except Exception:  # noqa: BLE001 — não deixar temporário pela metade
        path.unlink(missing_ok=True)
        raise
    return FileResponse(
        path,
        media_type="application/zip",
        filename="tts-studio-vozes.zip",
        background=BackgroundTask(path.unlink, missing_ok=True),
    )


# teto do TOTAL descomprimido no import (zip bomba: o limite por entrada não
# protege contra muitas entradas grandes)
_IMPORT_MAX_TOTAL = 512 * 1024 * 1024

# Nome de arquivo de voz aceito DENTRO de um zip (import de vozes e restore do
# backup). O `<id>` é estrito porque as rotas remontam o caminho por ele; o
# satélite `.orig.wav` (#443) é opcional e carrega o `id` inteiro antes do
# sufixo, senão o próprio backup que o app gera voltaria como "arquivo
# inesperado" no restore. É a ÚNICA régua dos dois caminhos.
_NOME_VOZ_NO_ZIP = re.compile(r"[A-Za-z0-9_-]+(?:\.(?:orig|prev))?\.(?:wav|json)")


def _eh_satelite(nome: str) -> bool:
    """Arquivo que acompanha uma voz em vez de ser uma voz (`<id>.orig.wav`)."""
    return nome.endswith((".orig.wav", ".prev.wav"))


@app.post("/api/voices/import")
async def import_voices(request: Request, zip_file: UploadFile = File(...)):
    _exige_admin(request)
    """Restaura um backup gerado por /api/voices/export (zip com .wav + .json).

    Sobrescreve vozes com o mesmo id (é um restore). Só extrai .wav/.json com
    nome seguro (achata subpastas), incluindo o satélite `<id>.orig.wav` do #443
    — o resto do zip é ignorado. Arquivos órfãos (wav sem json ou vice-versa)
    entram, mas são avisados no response.
    """

    # Copia em streaming para não depender de UploadFile.size nem carregar o
    # upload inteiro na RAM antes de validar o limite.
    with tempfile.NamedTemporaryFile(prefix=".voice-import-", suffix=".zip",
                                     dir=OUTPUTS_DIR, delete=False) as incoming:
        incoming_path = Path(incoming.name)
        recebido = 0
        try:
            while True:
                bloco = await zip_file.read(1024 * 1024)
                if not bloco:
                    break
                recebido += len(bloco)
                if recebido > _IMPORT_MAX_TOTAL:
                    raise HTTPException(400, "Zip grande demais (máx. 512 MB)")
                incoming.write(bloco)
        except Exception:
            incoming_path.unlink(missing_ok=True)
            raise

    # extracao/validacao do zip FORA do loop (#799): ate 512 MB de sha256 e copia
    from starlette.concurrency import run_in_threadpool

    def _importa():
        return _importa_vozes_zip(incoming_path)

    try:
        return await run_in_threadpool(_importa)
    finally:
        incoming_path.unlink(missing_ok=True)


def _importa_vozes_zip(incoming_path: Path) -> dict:
    """Corpo sincrono do import (roda na thread). O chamador apaga o temporario."""
    import zipfile

    try:
        zf = zipfile.ZipFile(incoming_path)
    except zipfile.BadZipFile as exc:
        raise HTTPException(400, "Arquivo não é um zip válido") from exc

    importados, ignorados, renomeados = [], [], []
    total = 0
    preparados = []
    try:
        with zf:
            with tempfile.TemporaryDirectory(prefix=".voice-import-", dir=OUTPUTS_DIR) as staging:
                staging_dir = Path(staging)
                vistos = set()
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    total += info.file_size
                    if total > _IMPORT_MAX_TOTAL:
                        raise HTTPException(400, "Zip grande demais (total descomprimido > 512 MB)")
                    if info.file_size > 100 * 1024 * 1024:
                        ignorados.append(info.filename)
                        continue
                    nome = Path(info.filename).name
                    chave = nome.lower()
                    if (not _NOME_VOZ_NO_ZIP.fullmatch(nome)
                            or chave in vistos):
                        ignorados.append(info.filename)
                        continue
                    vistos.add(chave)
                    destino = staging_dir / nome
                    with zf.open(info) as origem, destino.open("wb") as saida:
                        shutil.copyfileobj(origem, saida, length=1024 * 1024)
                    if nome.endswith(".json"):
                        try:
                            dados = json.loads(destino.read_text(encoding="utf-8"))
                            if not isinstance(dados, dict):
                                raise ValueError("metadata deve ser objeto JSON")
                        except Exception:
                            ignorados.append(info.filename)
                            destino.unlink(missing_ok=True)
                            continue
                        # O `id` tem de ser o nome do arquivo: as rotas
                        # (/api/voices/{id}/audio|peaks) remontam o caminho pelo
                        # id com `_safe_id`, que é estrito — um id fora de
                        # [A-Za-z0-9_-] (ex.: `<img src=x onerror=…>`, de backup
                        # feito à mão) deixava a voz na lista e 404 no áudio/peaks,
                        # e um id ausente nem aparecia. Aqui o meta é alinhado ao
                        # stem (que JÁ passou pela regex no nome do arquivo).
                        stem = Path(nome).stem
                        if dados.get("id") != stem:
                            renomeados.append({"arquivo": nome,
                                               "de": dados.get("id"), "para": stem})
                            dados["id"] = stem
                            destino.write_text(
                                json.dumps(dados, ensure_ascii=False, indent=2),
                                encoding="utf-8")
                    preparados.append((nome, destino))

                # Só publica depois de validar TODAS as entradas: falha não
                # deixa restore parcialmente aplicado.
                publicados = []
                backups = {}
                try:
                    for nome, origem in preparados:
                        destino = VOICES_DIR / nome
                        if destino.exists():
                            backup = staging_dir / (".backup-" + nome)
                            shutil.copy2(destino, backup)
                            backups[nome] = backup
                        origem.replace(destino)
                        publicados.append(nome)
                        importados.append(nome)
                except Exception:
                    # Reverte o que já foi publicado caso o filesystem falhe
                    # no meio da troca.
                    for nome in publicados:
                        (VOICES_DIR / nome).unlink(missing_ok=True)
                    for nome, backup in backups.items():
                        if backup.exists():
                            backup.replace(VOICES_DIR / nome)
                    importados.clear()
                    raise
    finally:
        incoming_path.unlink(missing_ok=True)
    if not importados:
        raise HTTPException(400, "Nenhuma voz (.wav/.json) encontrada no zip")
    # órfãos: .wav sem .json (voz some da lista) ou .json sem .wav (clonagem quebra).
    # Satélite (`<id>.orig.wav`) não tem json próprio: sem tirá-lo da conta o
    # par legítimo apareceria como órfão.
    wav_stems = {Path(n).stem for n in importados
                 if n.endswith(".wav") and not _eh_satelite(n)}
    json_stems = {Path(n).stem for n in importados
                  if n.endswith(".json") and not _eh_satelite(n)}
    orfaos = sorted((wav_stems - json_stems) | (json_stems - wav_stems))
    # restore pode ter sobrescrito amostras em uso -> invalida o cache de refs
    _conds_cache.clear()
    _cache_limpa()
    return {"ok": True, "importados": len(importados),
            "vozes": len(wav_stems & json_stems),
            "orfaos": orfaos[:10], "ignorados": ignorados[:10],
            # ids alinhados ao nome do arquivo (o que faz a UI achar áudio/peaks)
            "renomeados": renomeados[:10]}


# ---------------------------------------------------------------------------
# Backup/restore do ESTADO (#377): os quatro blocos de valor num pacote só.
# Não leva áudio gerado — só configuração, chaves, perfis e vozes.
# ---------------------------------------------------------------------------

SCHEMA_BACKUP = 1
# Serializa backup e restore: um restore publicando enquanto outro monta o pacote
# entregaria zip meio-velho/meio-novo, e dois restores juntos misturariam os blocos.
_restore_lock = threading.Lock()
_ESTADO_SIMPLES = ("settings.json", ".apikeys.json", ".speaker-profiles.json")


def _sha256_arquivo(path: Path) -> str:
    import hashlib as _h

    h = _h.sha256()
    with path.open("rb") as fh:
        for bloco in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(bloco)
    return h.hexdigest()


@app.get("/api/backup")
def backup_estado(request: Request):
    """Pacote único e versionado do estado local (admin). Sem áudio de outputs/.

    Manifest com schema, data, rev do build, contagem e sha256 por arquivo; o zip
    é gravado em temporário e enviado por `FileResponse` com unlink no background,
    o mesmo padrão do export de vozes (nunca o zip inteiro na RAM).
    """
    _exige_admin(request)
    with _restore_lock:
        return _monta_backup()


def _monta_backup():
    import zipfile
    from starlette.background import BackgroundTask

    tmp = tempfile.NamedTemporaryFile(prefix=".backup-", suffix=".zip",
                                      dir=OUTPUTS_DIR, delete=False)
    tmp.close()
    path = Path(tmp.name)
    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            arquivos, contagens = {}, {"vozes": 0}
            for nome in _ESTADO_SIMPLES:
                origem = _caminho_estado(nome)
                if not origem.exists():
                    continue
                z.write(origem, nome)
                arquivos[nome] = _sha256_arquivo(origem)
                contagens[nome] = 1
            for p in sorted(VOICES_DIR.iterdir()) if VOICES_DIR.exists() else []:
                if not (p.is_file() and p.suffix in (".wav", ".json")):
                    continue
                z.write(p, f"voices/{p.name}")
                arquivos[f"voices/{p.name}"] = _sha256_arquivo(p)
                if p.suffix == ".wav" and not _eh_satelite(p.name):
                    contagens["vozes"] += 1       # o `.orig.wav` não é voz nova
            manifest = {
                "schema": SCHEMA_BACKUP,
                "criado_em": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "build": _BUILD_CODIGO,
                "arquivos": arquivos,
                "contagens": contagens,
            }
            z.writestr("manifest.json", json.dumps(manifest, indent=2))
    except Exception:  # noqa: BLE001 — não deixar temporário pela metade
        path.unlink(missing_ok=True)
        raise
    nome_saida = f"tts-studio-backup-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    return FileResponse(path, media_type="application/zip", filename=nome_saida,
                        background=BackgroundTask(path.unlink, missing_ok=True))


@app.post("/api/restore")
async def restore_estado(request: Request, arquivo: UploadFile = File(...),
                         espelho: bool = False, sem_vozes: bool = False):
    """Restaura um pacote do `GET /api/backup` — staging, validação e rollback.

    Recusa schema desconhecido e sha256 divergente ANTES de tocar em qualquer
    arquivo. O que já existe é copiado para um diretório de rollback e devolvido
    se a publicação falhar no meio. Permissões finais 0600 nos quatro blocos.
    """
    _exige_admin(request)
    with tempfile.NamedTemporaryFile(prefix=".restore-", suffix=".zip",
                                     dir=OUTPUTS_DIR, delete=False) as incoming:
        incoming_path = Path(incoming.name)
        recebido = 0
        try:
            while True:
                bloco = await arquivo.read(1024 * 1024)
                if not bloco:
                    break
                recebido += len(bloco)
                if recebido > _IMPORT_MAX_TOTAL:
                    raise HTTPException(400, "Pacote grande demais (máx. 512 MB)")
                incoming.write(bloco)
        except Exception:
            incoming_path.unlink(missing_ok=True)
            raise

    try:
        # zip de ate 512 MB: sha256, extracao e publicacao FORA do loop (#799)
        from starlette.concurrency import run_in_threadpool

        resultado = await run_in_threadpool(
            _executa_restore, incoming_path, espelho=espelho, sem_vozes=sem_vozes)
    finally:
        incoming_path.unlink(missing_ok=True)

    _recarrega_settings()
    _conds_cache.clear()
    _cache_limpa()
    return resultado


def _executa_restore(incoming_path: Path, espelho: bool = False,
                     sem_vozes: bool = False) -> dict:
    """Restore sob a trava — serializa com o backup e com outro restore."""
    with _restore_lock:
        return _aplica_restore(incoming_path, espelho=espelho, sem_vozes=sem_vozes)


def _aplica_restore(incoming_path: Path, espelho: bool = False,
                    sem_vozes: bool = False) -> dict:
    """Valida o pacote e publica; devolve o resumo. Roda sob `_restore_lock`."""
    import zipfile

    try:
        zf = zipfile.ZipFile(incoming_path)
    except zipfile.BadZipFile as exc:
        raise HTTPException(400, "Arquivo não é um pacote .zip") from exc
    with zf, tempfile.TemporaryDirectory(prefix=".restore-", dir=OUTPUTS_DIR) as staging:
        stag = Path(staging)
        try:
            manifest = json.loads(zf.read("manifest.json"))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, "Pacote sem manifest.json legível") from exc
        if not isinstance(manifest, dict):
            raise HTTPException(400, "manifest.json inválido")
        schema = manifest.get("schema")
        if schema != SCHEMA_BACKUP:
            raise HTTPException(
                400, f"Versão de pacote desconhecida ({schema!r}); esta build "
                     f"lê a {SCHEMA_BACKUP} — nada foi tocado")
        esperados = manifest.get("arquivos")
        if not isinstance(esperados, dict) or not esperados:
            raise HTTPException(400, "manifest.json sem a lista de arquivos")

        # staging + conferência de sha256 de TUDO antes de publicar
        preparados = []
        for nome, sha in esperados.items():
            if nome in _ESTADO_SIMPLES:
                alvo = stag / nome
            elif (nome.startswith("voices/")          # `_safe_id` LEVANTA em nome
                  and _NOME_VOZ_NO_ZIP.fullmatch(nome[7:])):
                alvo = stag / "voices" / Path(nome).name
            else:
                raise HTTPException(400, f"Arquivo inesperado no pacote: {nome}")
            alvo.parent.mkdir(parents=True, exist_ok=True)
            try:
                with zf.open(nome) as origem, alvo.open("wb") as saida:
                    shutil.copyfileobj(origem, saida, length=1024 * 1024)
            except KeyError as exc:
                raise HTTPException(400, f"Pacote não tem {nome}") from exc
            if _sha256_arquivo(alvo) != sha:
                raise HTTPException(400, f"sha256 divergente em {nome} "
                                         f"— pacote corrompido, nada foi tocado")
            preparados.append((nome, alvo))

        tem_vozes_hoje = VOICES_DIR.is_dir() and any(
            p.suffix == ".wav" for p in VOICES_DIR.iterdir() if p.is_file())
        vozes_pacote = [n for n in esperados
                        if n.endswith(".wav") and not _eh_satelite(n)]
        # pacote só com settings apagava TODAS as vozes no espelho (#788)
        if espelho and tem_vozes_hoje and not vozes_pacote and not sem_vozes:
            raise HTTPException(
                400, "O pacote não tem nenhuma voz e o `espelho=1` apagaria as "
                     f"{sum(1 for _ in VOICES_DIR.glob('*.wav'))} vozes atuais — "
                     "repita com `sem_vozes=1` para confirmar")

        # rollback (efêmero, para falha no meio) + SNAPSHOT DURÁVEL (#788): o
        # rollback some quando dá certo, então sem o durável o engano não tem volta
        rollback = stag / ".rollback"
        rollback.mkdir()
        _snapshot_estado(rollback)
        backup = _snapshot_duravel()
        try:
            apagados, trocados = _publica_estado(preparados, rollback, espelho=espelho)
        except Exception:
            _restaura_estado(rollback)
            raise
    return {"ok": True, "schema": schema, "restaurados": len(preparados),
            "vozes": len(vozes_pacote), "espelho": bool(espelho),
            "apagados": apagados, "trocados": trocados,
            "backup": str(backup), "build_do_pacote": manifest.get("build")}


#: Quantos snapshots duráveis de restore ficam guardados (#788)
_RESTORE_BACKUPS_MAX = max(1, int(os.environ.get("TTS_RESTORE_BACKUPS", "10")))


def _fsync_arquivo(caminho: Path) -> None:
    """Leva o conteúdo ao disco de verdade (`copy2` fecha, mas não dura)."""
    with open(caminho, "rb") as f:
        os.fsync(f.fileno())


def _fsync_dir(caminho: Path) -> None:
    """Durabilidade da ENTRADA no diretório (o arquivo só existe após o fsync dele)."""
    try:
        fd = os.open(caminho, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _snapshot_duravel() -> Path:
    """Cópia do estado ATUAL antes de publicar, em `STATE_DIR/restore-backups/<ts>`.

    O rollback em `TemporaryDirectory` morre no sucesso; sem esta cópia um restore
    errado (ou um pacote só com settings) não tem volta. Poda os mais antigos.

    DURÁVEL de verdade (#788): `copy2` fecha o arquivo, o que sobrevive a crash de
    processo mas NÃO a queda de energia — o conteúdo pode ficar no page cache com
    `voices/` já reescrito. Por isso `fsync` em cada arquivo e no diretório antes
    de devolver; se algum falhar, o restore ABORTA (cópia que não dura não serve
    de volta)."""
    raiz = STATE_DIR / "restore-backups"
    # sufixo curto: dois restores no MESMO segundo não podem dividir a pasta (o
    # segundo sobrescreveria o snapshot do primeiro) — #788
    carimbo = time.strftime("%Y%m%d-%H%M%S")
    destino = raiz / carimbo
    if destino.exists():
        destino = raiz / f"{carimbo}-{uuid.uuid4().hex[:6]}"
    destino.mkdir(parents=True)
    try:
        destino.chmod(0o700)
    except OSError:
        pass
    _snapshot_estado(destino)
    for arq in destino.rglob("*"):
        if not arq.is_file():
            continue
        try:
            arq.chmod(0o600)
        except OSError:
            pass
        _fsync_arquivo(arq)          # conteúdo
    _fsync_dir(destino)              # entradas do snapshot
    _fsync_dir(raiz)                 # o próprio diretório do snapshot
    antigos = sorted(p for p in raiz.iterdir() if p.is_dir())
    for velho in antigos[:-_RESTORE_BACKUPS_MAX]:
        shutil.rmtree(velho, ignore_errors=True)
    return destino


def _caminho_estado(nome: str) -> Path:
    """Caminho real de um bloco de estado (as constantes são patcháveis)."""
    return {"settings.json": SETTINGS_PATH,
            ".apikeys.json": APIKEYS_PATH,
            ".speaker-profiles.json": SPEAKER_PATH}[nome]


def _snapshot_estado(destino: Path) -> None:
    """Copia o estado atual para o rollback (arquivos ausentes ficam ausentes)."""
    for nome in _ESTADO_SIMPLES:
        if _caminho_estado(nome).exists():
            # `copyfile`, não `copy2`: flag do original (ex.: `uchg`) não pode
            # viajar para o rollback e voltar imutável na restauração (#721)
            shutil.copyfile(_caminho_estado(nome), destino / nome)
            (destino / nome).chmod(0o600)
    if VOICES_DIR.exists():
        (destino / "voices").mkdir(exist_ok=True)
        for p in VOICES_DIR.iterdir():
            if p.is_file() and p.suffix in (".wav", ".json"):
                shutil.copy2(p, destino / "voices" / p.name)


def _publica_estado(preparados, rollback: Path,
                    espelho: bool = False) -> tuple[list, list]:
    """Publica o pacote; sem `espelho` MESCLA, com `espelho` apaga o que sobra.

    Devolve `(apagados, trocados)` para a resposta dizer o que mudou de verdade."""
    for nome, origem in preparados:
        destino = (_caminho_estado(nome) if nome in _ESTADO_SIMPLES
                   else VOICES_DIR / Path(nome).name)
        destino.parent.mkdir(exist_ok=True)
        origem.replace(destino)
        try:
            destino.chmod(0o600)
        except OSError:
            pass
    trocados = [nome for nome, _o in preparados]
    apagados: list[str] = []
    if espelho and VOICES_DIR.exists():
        no_pacote = {Path(n).name for n, _o in preparados if n.startswith("voices/")}
        for p in VOICES_DIR.iterdir():
            if p.is_file() and p.suffix in (".wav", ".json") and p.name not in no_pacote:
                p.unlink()
                apagados.append(p.name)
    return apagados, trocados


def _restaura_estado(rollback: Path) -> None:
    """Devolve o estado anterior (best-effort, chamado só em falha)."""
    for nome in _ESTADO_SIMPLES:
        origem, destino = rollback / nome, _caminho_estado(nome)
        if origem.exists():
            _copia_atomica(origem, destino)   # falha no meio não trunca a voz
        else:
            destino.unlink(missing_ok=True)
    if VOICES_DIR.exists():
        for p in VOICES_DIR.iterdir():
            if p.is_file() and p.suffix in (".wav", ".json"):
                p.unlink()
    if (rollback / "voices").exists():
        for p in (rollback / "voices").iterdir():
            _copia_atomica(p, VOICES_DIR / p.name)


def _recarrega_settings() -> None:
    """Reler `settings.json` do disco depois de um restore (sem reiniciar)."""
    salvo = {}
    try:
        if SETTINGS_PATH.exists():
            salvo = json.loads(SETTINGS_PATH.read_text())
    except Exception:  # noqa: BLE001 — restore com settings ilegível: fica o atual
        return
    novo = dict(_SETTINGS_DEFAULTS)
    novo.update({k: salvo[k] for k in _SETTINGS_DEFAULTS if k in salvo})
    _settings.clear()
    _settings.update(novo)
    # as chaves de API também são estado: reler, senão a instância restaurada
    # segue aceitando as chaves velhas (e recusando as que vieram no pacote)
    try:
        _load_apikeys()
    except Exception as exc:  # noqa: BLE001
        print(f"[restore] não recarregou as chaves: {exc}", flush=True)


def _eq_custom(audio, sr, low_db, mid_db, high_db):
    import numpy as np
    from scipy.signal import sosfilt

    y = np.asarray(audio, dtype=np.float32)
    bands = []
    if abs(float(low_db)) >= 0.05:
        bands.append(_biquad("lowshelf", 150.0, float(low_db), sr))
    if abs(float(mid_db)) >= 0.05:
        bands.append(_biquad("peak", 1500.0, float(mid_db), sr, 1.0))
    if abs(float(high_db)) >= 0.05:
        bands.append(_biquad("highshelf", 5000.0, float(high_db), sr))
    for sos in bands:
        y = sosfilt(np.array([sos], dtype=np.float64), y).astype(np.float32)
    return y.astype(np.float32)


@app.post("/api/audio/edit")
def audio_edit(audio: UploadFile = File(...), op: str = Form(...)):
    """Aplica UMA operação de edição num WAV e devolve o WAV processado.
    op (JSON): {type: trim|cut|normalize|denoise|gain|fade|eq, ...params}."""
    import io as _io
    import json as _json

    import numpy as np
    import soundfile as sf

    try:
        dados = _read_upload_limited(audio)
        if not dados:
            raise HTTPException(400, "Áudio vazio")
        a, sr = sf.read(_io.BytesIO(dados), dtype="float32")
    except Exception:
        raise HTTPException(400, "Áudio inválido")
    if a.ndim > 1:
        a = a.mean(axis=1)
    try:
        o = _json.loads(op)
    except Exception:
        raise HTTPException(400, "op inválido (JSON)")
    kind = o.get("type")
    dur = len(a) / sr if sr else 0.0

    if kind == "trim":                       # mantém só a seleção
        i0 = max(0, int(float(o.get("start", 0.0)) * sr))
        i1 = min(len(a), int(float(o.get("end", dur)) * sr))
        if i1 - i0 < int(0.05 * sr):
            raise HTTPException(400, "Seleção muito curta (mín. 50ms)")
        a = a[i0:i1]
    elif kind == "cut":                       # remove a seleção
        i0 = max(0, int(float(o.get("start", 0.0)) * sr))
        i1 = min(len(a), int(float(o.get("end", 0.0)) * sr))
        a = np.concatenate([a[:i0], a[i1:]])
        if a.size < int(0.05 * sr):
            raise HTTPException(400, "Sobrou áudio de menos")
    elif kind == "normalize":
        a = _normalize(a)
    elif kind == "denoise":
        a = _denoise_audio(a, sr, _clamp(o.get("strength", 0.7), 0.0, 1.0, 0.7))
    elif kind == "gain":
        g = float(10 ** (_clamp(o.get("db", 0.0), -24.0, 24.0, 0.0) / 20.0))
        s, e = o.get("start"), o.get("end")
        if s is not None and e is not None:          # ganho só na seleção (envelope com rampa)
            i0 = max(0, int(float(s) * sr))
            i1 = min(len(a), int(float(e) * sr))
            if i1 > i0:
                env = np.ones(len(a), dtype=np.float32)
                env[i0:i1] = g
                ramp = min(int(0.006 * sr), (i1 - i0) // 2)   # 6ms cross-fade nas bordas (sem click)
                if ramp > 0:
                    env[i0:i0 + ramp] = np.linspace(1.0, g, ramp, dtype=np.float32)
                    env[i1 - ramp:i1] = np.linspace(g, 1.0, ramp, dtype=np.float32)
                a = (a * env).astype(np.float32)
        else:
            a = (a * g).astype(np.float32)
    elif kind == "fade":
        a = _fade_edges(a, sr, _clamp(o.get("ms", 12.0), 0.0, 1000.0, 12.0))
    elif kind == "eq":
        a = _eq_custom(a, sr, o.get("low", 0.0), o.get("mid", 0.0), o.get("high", 0.0))
    else:
        raise HTTPException(400, f"op desconhecido: {kind}")

    a = np.asarray(a, dtype=np.float32)
    # trava: soft-limit (linear até 0.98, tanh acima) -> só dobra os picos que
    # estouram; NÃO reescala o áudio inteiro (boost num trecho não baixa o resto).
    over = np.abs(a) > 0.98
    if over.any():
        s = np.sign(a); mag = np.abs(a)
        mag_lim = 0.98 + 0.02 * np.tanh((mag - 0.98) / 0.02)
        a = np.where(over, s * mag_lim, a).astype(np.float32)
    buf = _io.BytesIO()
    sf.write(buf, a, sr, format="WAV", subtype="PCM_16")
    return Response(content=buf.getvalue(), media_type="audio/wav",
                    headers={"X-Duration": f"{len(a)/sr:.3f}" if sr else "0"})


@app.post("/api/voices/{voice_id}/replace")
def replace_voice_audio(voice_id: str, audio: UploadFile = File(...)):
    """Substitui o áudio de uma voz existente (mantém o id) — usado pelo editor."""
    import soundfile as sf

    voice_id = _safe_id(voice_id)
    wav = VOICES_DIR / f"{voice_id}.wav"
    if not wav.exists():
        raise HTTPException(404, "Voz não encontrada")
    # VALIDA ANTES DE ENCOSTAR no destino (#795): o 400 de áudio inválido/curto
    # acontecia DEPOIS do `sf.write` no lugar, então a referência já estava
    # trocada — e todo TTS daquela voz saía errado sem ninguém ter pedido. O
    # upload vai para temporário, é decodificado e medido, e só então o destino é
    # tocado.
    # sufixo único: dois replaces simultâneos da MESMA voz não compartilham o
    # temporário (mesma razão do denoise, #690)
    sufixo = uuid.uuid4().hex[:8]
    tmp_up = VOICES_DIR / f"{_TMP_VOICES_PREFIX}rep-{voice_id}-{sufixo}.upload"
    tmp_novo = VOICES_DIR / f"{_TMP_VOICES_PREFIX}rep-{voice_id}-{sufixo}.wav"
    try:
        _write_upload_limited(audio, tmp_up)
        try:
            a, sr = sf.read(str(tmp_up), dtype="float32")
        except Exception:
            raise HTTPException(400, "Áudio inválido")
        if a.ndim > 1:
            a = a.mean(axis=1)
        dur = round(a.size / sr, 3) if sr else 0.0
        if dur < 1:
            raise HTTPException(400, f"Áudio muito curto ({dur}s)")
        sf.write(str(tmp_novo), a, sr, subtype="PCM_16")
        _guarda_original(voice_id)      # antes de sobrescrever (#443)
        os.replace(tmp_novo, wav)       # troca ATÔMICA: ninguém lê pela metade
    finally:
        tmp_up.unlink(missing_ok=True)
        tmp_novo.unlink(missing_ok=True)
    jp = VOICES_DIR / f"{voice_id}.json"
    if jp.exists():
        try:
            m = json.loads(jp.read_text())
            m["duration"] = dur
            # denoised NÃO é marcado: o áudio veio do editor, sem denoise garantido
            write_json_atomic(jp, m)
        except Exception:  # noqa: BLE001
            pass
    _conds_cache.clear()                      # invalida o clone em cache p/ esta voz
    _cache_limpa()
    return {"ok": True, "duration": dur}


@app.get("/api/voices/{voice_id}/peaks")
def voice_peaks(voice_id: str, n: int = 160):
    """Picos normalizados (0..1) p/ desenhar o waveform da voz salva."""
    import numpy as np
    import soundfile as sf

    voice_id = _safe_id(voice_id)
    path = VOICES_DIR / f"{voice_id}.wav"
    if not path.exists():
        raise HTTPException(404, "Voz não encontrada")
    n = int(_clamp(n, 20, 600, 160))
    a, _sr = sf.read(str(path), dtype="float32")
    if a.ndim > 1:
        a = a.mean(axis=1)
    if a.size == 0:
        return {"peaks": [0.0] * n}
    buckets = np.array_split(np.abs(a), n)
    peaks = np.array([float(b.max()) if b.size else 0.0 for b in buckets])
    mx = float(peaks.max()) or 1.0
    return {"peaks": [round(p, 4) for p in (peaks / mx).tolist()]}


@app.delete("/api/voices/{voice_id}")
def delete_voice(request: Request, voice_id: str):
    _exige_admin(request)
    voice_id = _safe_id(voice_id)
    # a entrada do cache remoto é por `voice_id`: recriar a voz com o MESMO id
    # (import) reusaria o envio antigo se o mtime coincidisse — some junto
    _remote_voice_cache.pop(voice_id, None)
    removed = False
    for ext in ("wav", "json", "orig.wav"):     # o original vai junto (#443)
        path = VOICES_DIR / f"{voice_id}.{ext}"
        if path.exists():
            path.unlink()
            removed = True
    # temporário de um denoise/replace interrompido não fica órfão até a poda: o id
    # vem seguido de `-`, então `abc` não leva o temporário de `abcd` junto
    for tmp in VOICES_DIR.glob(f"{_TMP_VOICES_PREFIX}*{voice_id}-*"):
        tmp.unlink(missing_ok=True)
    if not removed:
        raise HTTPException(404, "Voz não encontrada")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Síntese: outputs/<id>.wav + outputs/<id>.json
# ---------------------------------------------------------------------------


# Jobs de síntese: o servidor gera trecho a trecho e o navegador toca cada
# trecho assim que fica pronto — a fala começa após o 1º trecho, não no fim.
_jobs: "OrderedDict[str, dict]" = OrderedDict()
_JOBS_MAX = 20                          # teto do histórico (ver _evict_jobs)
# Teto de jobs EM VOO (running/queued) — é o que a admissão conta; acima disto o
# pedido novo leva 429 em vez de empurrar um job ATIVO para fora do histórico.
# Antes não existia: com os _JOBS_MAX já em voo, o evict não achava nenhum job
# terminado para descartar e caía no "último recurso" — apagava o job MAIS
# ANTIGO, que estava rodando (e o .job-* com os trechos), então o cliente perdia
# status e stream enquanto a thread seguia gerando. Padrão 20 = teto prático de
# antes (sem regressão); env TTS_JOBS_ACTIVE_MAX para apertar em máquina pequena.
_JOBS_ACTIVE_MAX = max(1, int(os.environ.get("TTS_JOBS_ACTIVE_MAX", "20")))
# Campos comuns de todo job novo (é o que as rotas de status/trechos consomem).
_JOB_BASE = {"status": "running", "pieces": 0, "total": None, "progress": None,
             "output": None, "error": None}
# Serializa admissão/evict. Endpoint `def` roda no threadpool do FastAPI, então
# checagem+insert concorrentes furavam o teto de ativos; e o `pop` do evict no
# meio de uma iteração alheia mexe no tamanho do dict. Reentrante porque a
# admissão chama helpers que também travam.
_jobs_lock = threading.RLock()

# ---------------------------------------------------------------------------
# Fila de falas (speech_queue): gate serial por ticket.
# - Vários jobs podem GERAR em paralelo (quando remoto / após liberar gen_lock).
# - Só um job ENTREGA por vez, na ordem de chegada.
# - Após entregar, bloqueia a próxima pela duração REAL do WAV (+ folga).
# - Trechos (pieces) só contam no job depois da entrega — endpoint também barra.
# ---------------------------------------------------------------------------

def _wav_duration_precise(path: Path) -> float:
    """Duração em segundos a partir do WAV (precisão de frames; 0 se falhar)."""
    try:
        with wave.open(str(path), "rb") as w:
            rate = w.getframerate() or 0
            if rate <= 0:
                return 0.0
            return w.getnframes() / float(rate)
    except Exception:  # noqa: BLE001
        return 0.0


def _pieces_duration(job_id: str) -> float:
    """Soma a duração dos trechos .wav do job (fallback se o final ainda não existe)."""
    pdir = _piece_dir(job_id)
    if not pdir.is_dir():
        return 0.0
    total = 0.0
    for p in sorted(pdir.glob("*.wav")):
        if not p.stem.isdigit():
            continue
        total += _wav_duration_precise(p)
    return total


def _resolve_speech_duration(duration_s=None, job_id: str | None = None,
                             output_meta: dict | None = None) -> float:
    """Duração da fala: prefere o maior valor confiável (WAV final / trechos / meta)."""
    candidates: list[float] = []

    def _add(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return
        if f > 0:
            candidates.append(f)

    _add(duration_s)
    if output_meta:
        _add(output_meta.get("duration"))
        oid = output_meta.get("id")
        if oid:
            _add(_wav_duration_precise(OUTPUTS_DIR / f"{oid}.wav"))
    if job_id:
        _add(_pieces_duration(job_id))
    return max(candidates) if candidates else 0.0


class _SpeechGate:
    """Gate FIFO de entrega: ticket + espera pela duração da fala anterior."""

    def __init__(self):
        self._cv = threading.Condition()
        self._ticket_seq = 0
        self._next_ticket = 0
        self._free_at = 0.0
        self._last_dur = 0.0
        self._depth = 0
        #: ticket → job dono, para detectar ticket órfão na espera FIFO (#796)
        self._donos: dict[int, str] = {}

    def _pula_morto(self, ticket: int) -> bool:
        """Dono saiu de running/queued? Avança o ticket e devolve True.

        True só quando o ticket andou. Sem dono (buraco) devolve False: quem
        espera faz `wait` curto e o prazo segue valendo. Continuar seco
        segurava o Condition num lookup de dict (#915).
        """
        job_id = self._donos.get(ticket)
        if job_id is None:
            return False
        job = _jobs.get(job_id)
        if job is None or job.get("status") not in ("running", "queued"):
            self._donos.pop(ticket, None)
            self._next_ticket += 1
            self._depth = max(0, self._depth - 1)
            self._cv.notify_all()
            print(f"[fila] ticket {ticket} (job {job_id}) sumiu — pulando", flush=True)
            return True
        return False

    @property
    def depth(self) -> int:
        with self._cv:
            return int(self._depth)

    @property
    def free_in(self) -> float:
        with self._cv:
            return max(0.0, self._free_at - time.time())

    @property
    def last_duration(self) -> float:
        with self._cv:
            return float(self._last_dur)

    def begin(self, job_id: str):
        if not _settings.get("speech_queue"):
            return None
        with self._cv:
            ticket = self._ticket_seq
            self._ticket_seq += 1
            self._depth += 1
            free_in = max(0.0, self._free_at - time.time())
            last = self._last_dur
            ahead = ticket - self._next_ticket
            # Mesma seção do ticket: a janela entre dois `with` deixava o
            # próximo ver ticket sem dono e girar no dict (#915).
            self._donos[ticket] = job_id
        token = {"ticket": ticket, "job_id": job_id, "done": False}
        job = _jobs.get(job_id)
        if job is not None:
            job["queue_token"] = token      # o cancelamento precisa achar o ticket
            job["queued"] = True
            job["status"] = "queued"
            job["queue_ticket"] = ticket
            if ahead > 0 or free_in > 0.05:
                job["progress"] = {
                    "stage": (
                        f"na fila (#{ahead + 1}"
                        + (f", ~{free_in:.0f}s" if free_in > 0.05 else "")
                        + (f", última {last:.1f}s" if last > 0 else "")
                        + ")…"
                    ),
                }
            else:
                job["progress"] = {"stage": "na fila de falas…"}
        return token

    def deliver(self, token, duration_s: float = 0.0,
                output_meta: dict | None = None) -> None:
        if not token or token.get("done"):
            return
        job_id = token.get("job_id") or ""
        job = _jobs.get(job_id)
        try:
            gap = max(0.0, min(5.0, float(_settings.get("speech_queue_gap_s") or 0.35)))
        except (TypeError, ValueError):
            gap = 0.35

        dur = _resolve_speech_duration(duration_s, job_id=job_id, output_meta=output_meta)
        if dur <= 0 and job is not None:
            text = (job.get("text") or "") if isinstance(job.get("text"), str) else ""
            if text:
                dur = max(0.8, min(120.0, len(text) / 14.0))
        # margem mínima: evita overlap por latência de rede/player do cliente
        if dur > 0:
            dur = max(dur, 0.4)

        limite = time.monotonic() + _SPEECH_FILA_ESPERA_S
        with self._cv:
            # 1) ordem FIFO — com PRAZO (#796): ticket de job que morreu ou que
            # espera demais é pulado, senão a fila inteira trava
            while token["ticket"] != self._next_ticket:
                if token.get("cancelado"):      # #835: cancelado sai na hora
                    self._avanca(token)
                    return
                if self._pula_morto(self._next_ticket):   # #796: True só se andou
                    continue
                if time.monotonic() > limite:             # #796: prazo da espera
                    print(f"[fila] ticket {token['ticket']} esperou "
                          f"{_SPEECH_FILA_ESPERA_S:.0f}s — seguindo", flush=True)
                    break
                if job is not None:
                    pos = token["ticket"] - self._next_ticket + 1
                    job["progress"] = {"stage": f"na fila (posição {max(1, pos)})…"}
                self._cv.wait(timeout=0.4)
            self._donos.pop(token["ticket"], None)

            # 2) espera o fim da fala anterior (duração medida na entrega anterior)
            while True:
                wait = self._free_at - time.time()
                if wait <= 0:
                    break
                if job is not None:
                    job["progress"] = {
                        "stage": (
                            f"aguardando fim da fala anterior "
                            f"(~{wait:.0f}s; durou {self._last_dur:.1f}s)…"
                        ),
                    }
                self._cv.wait(timeout=min(0.4, max(0.05, wait)))

            now = time.time()
            self._free_at = now + dur + gap
            self._last_dur = dur
            self._next_ticket += 1
            self._depth = max(0, self._depth - 1)
            token["done"] = True
            token["duration_s"] = dur
            if job is not None:
                job.pop("queued", None)
                job["speech_duration_s"] = round(dur, 3)
            self._cv.notify_all()

    def _avanca(self, token) -> None:
        """Passa o ticket adiante SEM reservar tempo de áudio (chamado já na vez)."""
        self._next_ticket += 1
        self._depth = max(0, self._depth - 1)
        token["done"] = True
        self._cv.notify_all()

    def cancel(self, token) -> None:
        """Marca o ticket como cancelado e acorda quem espera (#835).

        Não bloqueia: quem está na vez avança na hora; quem está atrás é pulado
        quando chegar a vez (o `deliver` dele vê a marca). Sem isto o lugar só era
        liberado quando o job chegava ao topo — a fila atrás esperava por um job
        que já não ia tocar nada."""
        if not token or token.get("done"):
            return
        with self._cv:
            token["cancelado"] = True
            if token["ticket"] == self._next_ticket:
                self._avanca(token)
            self._cv.notify_all()

    def abort(self, token) -> None:
        """Erro: avança o ticket sem reservar tempo de áudio."""
        if not token or token.get("done"):
            return
        job_id = token.get("job_id") or ""
        limite = time.monotonic() + _SPEECH_FILA_ESPERA_S
        with self._cv:
            while token["ticket"] != self._next_ticket:
                if self._pula_morto(self._next_ticket):   # #796: True só se andou
                    continue
                if time.monotonic() > limite:
                    print(f"[fila] abort do ticket {token['ticket']} esperou "
                          f"{_SPEECH_FILA_ESPERA_S:.0f}s — seguindo", flush=True)
                    break
                self._cv.wait(timeout=0.4)
            self._donos.pop(token["ticket"], None)
            self._next_ticket += 1
            self._depth = max(0, self._depth - 1)
            token["done"] = True
            self._cv.notify_all()
        job = _jobs.get(job_id)
        if job is not None:
            job.pop("queued", None)
            job.pop("queue_token", None)


#: Prazo máximo esperando a vez na fila (#796). Passou disso, o ticket segue — é
#: melhor um overlap do que a fila inteira presa. 0 = sem prazo.
_SPEECH_FILA_ESPERA_S = max(0.0, float(os.environ.get("TTS_SPEECH_FILA_ESPERA_S", "120")))
_speech_gate = _SpeechGate()


def _speech_queue_begin(job_id: str):
    return _speech_gate.begin(job_id)


def _speech_queue_deliver(token, duration_s: float = 0.0,
                          output_meta: dict | None = None) -> None:
    _speech_gate.deliver(token, duration_s=duration_s, output_meta=output_meta)


def _speech_queue_abort(token) -> None:
    _speech_gate.abort(token)


def _speech_queue_cancela(job_id: str) -> bool:
    """Tira o job da fila de falas sem bloquear (#835). Devolve se havia lugar."""
    job = _jobs.get(job_id) or {}
    token = job.get("queue_token")
    if not token or token.get("done"):
        return False
    _speech_gate.cancel(token)
    return True


def _piece_dir(job_id: str) -> Path:
    return OUTPUTS_DIR / f".job-{_safe_id(job_id)}"


def _job_owner_write(pdir: Path) -> None:
    """Marca o dir do job com o pid do processo dono.

    É o que a limpeza de boot de OUTRO processo consulta para não apagar os
    trechos de um job em voo (`_job_dir_ativo`). Falha aqui não derruba o job:
    sem o arquivo o boot cai na regra de atividade recente."""
    try:
        (pdir / "owner.pid").write_text(str(os.getpid()))
    except OSError:
        pass


def _safe_id(v: str) -> str:
    """Valida id vindo da rota antes de montar Path — defesa em profundidade
    contra path traversal em downloads (o roteador já impede '/', isto aqui
    garante mesmo se o roteamento mudar)."""
    if not v or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", v):
        raise HTTPException(404, "Id inválido")
    return v


def _jobs_ativos() -> int:
    """Jobs em voo (running/queued) — o que a admissão conta.

    `queued` conta: job parado na fila de falas ainda não entregou nada, então
    continua sendo um ativo que o cliente está esperando.
    """
    with _jobs_lock:
        return sum(1 for j in _jobs.values() if j.get("status") in ("running", "queued"))


def _jobs_snapshot() -> list:
    """Cópia dos jobs sob lock, para quem precisa ITERAR.

    Iterar a view viva (`_jobs.values()`) enquanto outra thread admite/evicta
    estoura "OrderedDict mutated during iteration" — e o `_jobs` foi feito para
    ser mexido de dentro de threads (admissão de rota + evict)."""
    with _jobs_lock:
        return list(_jobs.values())


def _outputs_presos() -> set[str]:
    """Ids que a limpeza e o DELETE não podem apagar enquanto o job está em `_jobs`.

    Inclui a chave do job (o diretório `.job-<id>`) e o `output.id`. O stem do
    WAV publicado NÃO é o job_id: a síntese grava outro uuid. Comparar o stem
    só com a chave deixava o arquivo de um job `done` sair, e o
    `/v1/audio/speech` lia esse path e respondia 500 (#884). `done` e `error`
    entram: o 500 aconteceu com o job já terminado, ainda na janela do histórico.
    """
    presos: set[str] = set()
    with _jobs_lock:
        for jid, j in _jobs.items():
            presos.add(str(jid))
            out = j.get("output") if isinstance(j, dict) else None
            if isinstance(out, dict) and out.get("id"):
                presos.add(str(out["id"]))
    return presos


def _jobs_admit(extra: dict | None = None) -> str:
    """Registra um job novo com admissão controlada; devolve o job_id.

    Estourou o teto de ativos → 429 (mesma família do limitador de taxa, que o
    cliente já sabe exibir e o navegador mostra pelo `detail`), ANTES de criar o
    job e de subir thread: o pedido recusado não deixa nada pela metade.
    `Retry-After` curto porque um slot libera assim que qualquer job termina.

    Checagem + insert acontecem sob `_jobs_lock`: endpoint `def` roda no
    threadpool do FastAPI, então duas requisições passavam juntas pela checagem
    e o teto era furado (medido no gate: teto 2 com 10 admissões simultâneas →
    4 aceitos). Com o lock o teto é exato; o invariante (nunca descartar ativo)
    valia antes e continua valendo.
    """
    with _jobs_lock:
        _jobs_capacity_check()
        job_id = uuid.uuid4().hex[:10]
        _jobs[job_id] = {**_JOB_BASE, **(extra or {})}
        _evict_jobs()
        return job_id


def _jobs_capacity_check():
    """Levanta 429 se não há slot de job ativo livre.

    Chamado cedo nas rotas que fazem STT/tradução antes de criar o job: recusar
    depois do STT inteiro custaria segundos de espera ao cliente para então
    devolver o erro (e o áudio teria de ser reenviado). Como não reserva slot,
    o probe pode passar e o `_jobs_admit` recusar logo depois — quem decide é
    sempre a admissão.
    """
    ativos = _jobs_ativos()
    if ativos >= _JOBS_ACTIVE_MAX:
        raise HTTPException(
            429,
            f"Limite de jobs em andamento atingido ({ativos}/{_JOBS_ACTIVE_MAX}) "
            "— aguarde um job terminar e tente de novo",
            headers={"Retry-After": "5"},
        )


def _evict_jobs():
    """Mantém no máximo _JOBS_MAX jobs no histórico — SEM tocar em job ativo.

    Só jobs terminados (done/error) saem: se todos os _JOBS_MAX estiverem em voo
    (admissão garante ativos ≤ _JOBS_ACTIVE_MAX) o dict fica acima do teto e
    volta a ele conforme os jobs terminam. Descartar um ativo aqui apagaria os
    trechos .job-* e o cliente perderia status/stream no meio da geração, com a
    thread ainda rodando (o bug: o job sumia, o áudio parava).

    Trava o `_jobs_lock` em volta do laço: é chamado da admissão (que já o
    segura — daí o RLock) e de testes, e o `pop` no meio de uma iteração
    concorrente mudaria o tamanho do dict.
    """
    with _jobs_lock:
        while len(_jobs) > _JOBS_MAX:
            alvo = next((jid for jid, j in _jobs.items()
                         if j.get("status") not in ("running", "queued")), None)
            if alvo is None:
                break                   # só ativos em voo: nenhum é descartado
            _jobs.pop(alvo, None)
            shutil.rmtree(_piece_dir(alvo), ignore_errors=True)


# ---------------------------------------------------------------------------
# Histórico de jobs em DISCO (#358)
#
# `_jobs` sozinho é volátil: um restart (ou o `/api/shutdown`) apagava o
# histórico e quem estava pollando levava 404 no meio do stream. Aqui o estado
# vai para um arquivo pequeno e atômico a cada transição TERMINAL, e é recarregado
# no boot — job que não terminou vira `error` com o motivo, nunca 404.
#
# Só metadado: id, status, contagem, progresso, erro e o `output` (que é o meta
# já público do job). Texto e áudio continuam fora.
# ---------------------------------------------------------------------------
# Em `state/`, NÃO em `outputs/`: a limpeza de boot varre `outputs/.job-*` e o
# padrão casaria com `.jobs-history.json` — o histórico morreria antes de ser lido.
_JOBS_HIST_PATH = _caminho_estado_env("TTS_JOBS_HIST_PATH",
                                      BASE / "state" / "jobs-history.json")
_JOBS_HIST_PATH.parent.mkdir(parents=True, exist_ok=True)
_JOBS_HIST_CAMPOS = ("status", "pieces", "total", "progress", "output", "error")
_MOTIVO_REINICIO = "reinício do servidor — job em voo não sobrevive (as peças já geradas podem continuar no disco)"


def _jobs_hist_salva() -> None:
    """Grava os últimos `_JOBS_MAX` jobs. Nunca levanta (histórico é acessório)."""
    try:
        _JOBS_HIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _jobs_lock:
            dados = []
            for jid, j in list(_jobs.items())[-_JOBS_MAX:]:
                registro = {"id": jid}
                registro.update({k: j.get(k) for k in _JOBS_HIST_CAMPOS if k in j})
                dados.append(registro)
        write_json_atomic(_JOBS_HIST_PATH, {"ts": int(time.time()), "jobs": dados})
    except Exception:  # noqa: BLE001
        pass


def _jobs_hist_carrega() -> None:
    """Recarrega o histórico no boot; job não-terminal vira `error` com motivo.

    Roda no start do lifespan (depois do import, quando `_jobs` já existe).
    `progress` de job interrompido é limpo: a etapa que estava rodando não volta.
    """
    try:
        dados = json.loads(_JOBS_HIST_PATH.read_text())
    except (OSError, ValueError):
        return
    with _jobs_lock:
        for item in (dados.get("jobs") or [])[-_JOBS_MAX:]:
            jid = item.get("id")
            if not jid or jid in _jobs:
                continue
            registro = {**_JOB_BASE, **{k: item.get(k) for k in _JOBS_HIST_CAMPOS if k in item}}
            if registro.get("status") not in ("done", "error"):
                registro.update(status="error", error=_MOTIVO_REINICIO, progress=None)
            _jobs[jid] = registro


def _voice_ref_text(voice_id: str):
    """Transcrição opcional da amostra (campo 'ref_text' no JSON da voz).

    Com a transcrição a clonagem fica mais estável; se ausente, a lib
    auto-transcreve com Whisper na 1ª geração da voz (mais lento, baixa o ASR).
    """
    try:
        meta = json.loads((VOICES_DIR / f"{voice_id}.json").read_text())
        return (meta.get("ref_text") or "").strip() or None
    except Exception:  # noqa: BLE001
        return None


def _materialize_preset(model, sr: int, pid: str):
    """Gera a amostra-semente de uma voz padrão (voice design) e a salva como voz.

    Roda uma vez por preset; o .wav resultante ancora o timbre (a clonagem por
    ref_tokens passa a valer para essa voz, mantendo-a consistente entre trechos).
    """
    import numpy as np
    import soundfile as sf

    p = OMNI_PRESETS[pid]
    # A semente é gerada UMA vez e vira o .wav de referência (os ref_tokens saem
    # dela): uma geração ruim ancorava a voz para sempre. Valida RMS e duração e
    # re-sorteia até 2× — o seed entra explícito para a nova tentativa ser OUTRA
    # (#329). Devolve o número de re-sorteios em `meta["tentativas"]`.
    semente_base = int(hashlib.sha256(pid.encode()).hexdigest()[:8], 16)
    audio, tentativas = None, 0
    for tentativa in range(3):
        import mlx.core as mx

        mx.random.seed((semente_base + tentativa) % (2 ** 31))
        audio = np.concatenate([
            np.array(r.audio, dtype=np.float32)
            for r in model.generate(
                text=OMNI_PRESET_SEED, instruct=p["instruct"], language="None",
                num_steps=OMNI_STEPS_HQ, guidance_scale=2.0, class_temperature=0.0,
            )
        ])
        audio = _normalize(_trim_tail_silence(audio, sr))
        rms = float(np.sqrt(np.mean(audio ** 2))) if audio.size else 0.0
        dur = (len(audio) / sr) if audio.size else 0.0
        tentativas = tentativa
        if rms >= 0.05 and dur >= 3.0:
            break
        print(f"[preset] {pid}: semente ruim (rms {rms:.3f}, {dur:.1f}s) — "
              f"re-sorteando ({tentativa + 1}/2)", flush=True)
    sf.write(VOICES_DIR / f"{pid}.wav", audio, sr, subtype="PCM_16")
    meta = {
        "id": pid, "name": p["name"], "preset": True, "materialized": True,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration": round(len(audio) / sr, 1),
        "ref_text": OMNI_PRESET_SEED, "instruct": p["instruct"],
        "seed": (semente_base + tentativas) % (2 ** 31), "tentativas": tentativas,
    }
    write_json_atomic(VOICES_DIR / f"{pid}.json", meta)


# Famílias que rodam em PROCESSO FILHO (SIGSEGV do Metal/Qwen não derruba o servidor).
# OmniVoice e PocketTTS ficam in-process (estáveis + mais rápidos no 2º request).
_ISOLATED_FAMILIES = frozenset({
    "qwen3_tts", "qwen3_custom", "qwen3_design",
    "fish", "chatterbox", "voxcpm2", "voxtral_tts",
    "kokoro", "moss_nano", "indextts", "generic",
})


def _sanitiza_caminhos(texto: str) -> str:
    """Troca caminho absoluto do repo/Home por `…/` na mensagem de erro do job.

    O job já anexava 500 chars do log do worker, com `/Users/<dono>/Documents/...`
    e caminhos de venv — ruído que não ajuda o cliente e expõe a instalação (#282).
    Só rótulos: a CAUDA do log (o motivo real) continua."""
    if not texto:
        return texto
    # do mais específico para o mais genérico: o repo vira caminho relativo, o
    # resto do home vira `~/…`
    texto = texto.replace(str(BASE) + "/", "")
    texto = texto.replace(str(Path.home()) + "/", "~/")
    return texto.replace(str(BASE), "repo").replace(str(Path.home()), "~")


def _corre_sem_gen_lock(fn):
    """Roda `fn` sem esta thread dona do `_gen_lock` (#915).

    A marca d'água é IPC de CPU: não usa o Metal. Segurá-la dentro da trava
    da geração emperra o Live e o próximo job pelo tempo do readline. Solta
    TODOS os níveis do RLock — um `release` só tira um — e repega o mesmo
    tanto no fim, para o `with` de fora continuar equilibrado.
    """
    niveis = 0
    while _gen_lock._is_owned():
        _gen_lock.release()
        niveis += 1
        if niveis > 16:          # trava desbalanceada: não gira para sempre
            break
    try:
        return fn()
    finally:
        for _ in range(niveis):
            _gen_lock.acquire()


def _marca_arquivo(caminho: Path) -> None:
    """Marca um wav no disco. Nunca levanta e não segura o `_gen_lock`."""
    if not _settings.get("tts_marca_dagua"):
        return
    if not Path(caminho).is_file():
        return

    def _faz() -> None:
        try:
            import marca_dagua

            if marca_dagua.disponivel():
                marca_dagua.marca_wav(caminho)
            else:
                _avisa_sem_marca(marca_dagua.motivo())
        except Exception as exc:  # noqa: BLE001 — sem marca o áudio segue
            _avisa_sem_marca(str(exc)[:120])

    _corre_sem_gen_lock(_faz)


def _marca_saida_isolada(pdir: Path, st: dict) -> None:
    """Trechos e wav final do isolado, com o filho já fora do Metal (#915).

    A concatenação copia o PCM cru. Marcar só o trecho deixa o arquivo final
    sem marca; marcar só o final deixa o trecho que o cliente pede cru.
    """
    n = int(st["pieces"]) if st.get("pieces") is not None else 0
    for i in range(max(0, n)):
        _marca_arquivo(pdir / f"{i}.wav")
    out = (st.get("output") or {}).get("id")
    if out:
        _marca_arquivo(OUTPUTS_DIR / f"{out}.wav")


def _unload_local_tts():
    """Libera o modelo MLX do processo principal (antes de spawnar worker)."""
    global _model
    with _model_lock:
        _model = None
        _conds_cache.clear()
        _cache_limpa()
        _release_mlx_memory(aggressive=True)
        if _model_state.get("status") != "error":
            _model_state.update(status="idle", progress=None, model=_settings.get("model"),
                                precision=None, path=None)


def _run_tts_job_isolated(job_id: str, text: str, voice_id: str, voice_path: Path,
                          language: str, omni: dict, be: dict, sq=None,
                          model: str | None = None):
    """Síntese em subprocesso: crash nativo (SIGSEGV) só mata o worker."""
    import subprocess
    import sys

    job = _jobs[job_id]
    pdir = _piece_dir(job_id)
    pdir.mkdir(exist_ok=True)
    _job_owner_write(pdir)          # dono: o boot de outro processo consulta
    status_path = pdir / "status.json"
    cfg_path = pdir / "config.json"
    label = (be.get("meta") or {}).get("label") or be.get("id")
    family = be["family"]
    hold_pieces = sq is not None  # fila ligada: não publica trechos até entregar

    # libera RAM do modelo in-process antes do filho carregar o Qwen/etc.
    _unload_local_tts()
    _model_state.update(
        status="loading", device="mlx-worker", model=model or _settings.get("model"),
        family=family, progress=f"worker: {label}…",
        backend_id=be.get("id"), backend_label=label,
    )
    job.update(status="running",
               progress={"stage": f"iniciando worker ({label})…",
                         "backend": be.get("id"), "family": family})

    cfg = {
        "job_id": job_id,
        "text": text,
        "voice_id": voice_id,
        "voice_path": str(voice_path) if voice_path else "",
        "language": language,
        "omni": omni,
        "model": model or _settings.get("model") or "omnivoice",
        "settings": {
            "chunk_max_chars": _settings.get("chunk_max_chars", 140),
            "omni_ref_max_s": _settings.get("omni_ref_max_s", 10.0),
            "omni_precision": _settings.get("omni_precision", "bf16"),
            # FX de saída (o worker aplica o mesmo EQ/ganho do caminho local)
            "audio_gain_db": _settings.get("audio_gain_db", 0.0),
            "audio_eq_low_db": _settings.get("audio_eq_low_db", 0.0),
            "audio_eq_mid_db": _settings.get("audio_eq_mid_db", 0.0),
            "audio_eq_high_db": _settings.get("audio_eq_high_db", 0.0),
        },
        # a marca NÃO viaja no cfg (#915): o filho grava cru e o pai marca
        # depois de soltar o `_gen_lock`. Marcar no filho segurava esta trava
        # pelo tempo do IPC.
        "piece_dir": str(pdir),
        "outputs_dir": str(OUTPUTS_DIR),
        "voices_dir": str(VOICES_DIR),
        "base_dir": str(BASE),
        "status_path": str(status_path),
    }
    write_json_atomic(cfg_path, cfg)
    py = str(BASE / ".venv-mlx" / "bin" / "python")
    if not Path(py).exists():
        py = sys.executable
    worker = str(BASE / "tts_worker.py")

    log_path = pdir / "worker.log"
    # desfecho resolvido SOB o lock e aplicado FORA dele (#796): `deliver`/`abort`
    # esperam a vez na fila, e esperar segurando o `_gen_lock` congelava TTS e Live.
    final: tuple[str, object] | None = None
    last_pieces = 0
    # serializa workers (um modelo pesado por vez no Metal). A marca fica
    # FORA deste with (#915).
    with _gen_lock:
        log_f = open(log_path, "w", encoding="utf-8")  # noqa: SIM115
        try:
            proc = subprocess.Popen(
                [py, worker, str(cfg_path)],
                cwd=str(BASE),
                stdout=log_f,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # grupo próprio: kill limpo se preciso
            )
            # Registro antes do polling: se o servidor morrer, o próximo
            # startup consegue localizar e encerrar este grupo de processos.
            (pdir / "worker.pid").write_text(str(proc.pid))
        except Exception as exc:  # noqa: BLE001
            log_f.close()
            job.update(status="error", error=f"falha ao iniciar worker: {exc}",
                       progress=None)
            _model_state.update(status="idle", progress=None)
            final = ("err", f"falha ao iniciar worker: {exc}")
            _jobs_hist_salva()
            # abort do Popen FORA do lock (#796)
            _speech_queue_abort(sq)
            return
        last_pieces = 0
        t0 = time.monotonic()
        ultimo_sinal = time.time()
        _visto = [0.0]            # maior mtime já visto do `status.json`

        def _mata_grupo() -> None:
            """Mata o grupo do worker (o filho tem sessão própria)."""
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:  # noqa: BLE001
                try:
                    proc.terminate()
                except Exception:  # noqa: BLE001
                    pass
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:  # noqa: BLE001
                    try:
                        proc.kill()
                    except Exception:  # noqa: BLE001
                        pass

        def _finish_abortido(motivo: str) -> None:
            """Cancelado ou travado: mesma saída — job em erro e nada segurando."""
            _speech_queue_abort(sq)
            job.update(status="error", error=motivo, progress=None, pieces=last_pieces)
            _jobs_hist_salva()
            _model_state.update(status="idle", progress=None, error=None)

        def _finish_ok(st: dict) -> None:
            out = st.get("output") or {}
            pieces = int(st["pieces"]) if st.get("pieces") is not None else last_pieces
            if st.get("total") is not None:
                job["total"] = int(st["total"])
            if sq is not None:
                job["progress"] = {"stage": "aguardando vez na fila…"}
                _speech_queue_deliver(sq, out.get("duration"), output_meta=out)
            job["pieces"] = pieces
            if job.get("cancelado"):      # saiu da fila sem tocar (#835)
                job.update(status="error", error="cancelado pelo usuário",
                           output=out, progress=None)
                _jobs_hist_salva()
                _model_state.update(status="idle", progress=None, error=None)
                return
            job.update(status="done", output=out, progress=None, error=None)
            _jobs_hist_salva()
            _model_state.update(status="ready", progress=None,
                                device="mlx-worker",
                                model=_settings.get("model"),
                                family=family,
                                backend_id=be.get("id"),
                                backend_label=label)

        def _finish_err(msg: str) -> None:
            _speech_queue_abort(sq)
            job.update(status="error", error=msg, progress=None, pieces=last_pieces)
            _jobs_hist_salva()
            _model_state.update(status="idle", progress=None, error=None)

        try:
            while True:
                rc = proc.poll()
                if status_path.exists():
                    try:
                        st = json.loads(status_path.read_text())
                        if st.get("pieces") is not None:
                            last_pieces = int(st["pieces"])
                            # não publica: o wav ainda está cru. `job["pieces"]`
                            # sai no `_finish_ok`, depois da marca (#915).
                        if st.get("total") is not None:
                            job["total"] = int(st["total"])
                        if st.get("progress") is not None and not hold_pieces:
                            job["progress"] = st["progress"]
                            stage = (st["progress"] or {}).get("stage")
                            if stage:
                                _model_state["progress"] = stage
                        elif st.get("progress") is not None and hold_pieces:
                            # ainda gera: mostra estágio sem liberar trechos
                            stage = (st["progress"] or {}).get("stage")
                            if stage:
                                job["progress"] = {"stage": stage, "backend": be.get("id"),
                                                   "family": family}
                                _model_state["progress"] = stage
                        if st.get("status") == "done" and st.get("output"):
                            try:
                                proc.wait(timeout=30)
                            except Exception:  # noqa: BLE001
                                pass
                            final = ("ok", st)
                            break
                        if st.get("status") == "error":
                            try:
                                proc.wait(timeout=10)
                            except Exception:  # noqa: BLE001
                                pass
                            final = ("err", st.get("error") or "erro no worker")
                            break
                    except Exception:  # noqa: BLE001
                        pass
                if rc is not None:
                    break
                # CANCELAMENTO do usuário (#800): vale a cada volta, antes do
                # watchdog — pedido explícito não espera o prazo.
                if job.get("cancelado"):
                    _mata_grupo()
                    final = ("err", "cancelado pelo cliente")
                    break
                # WATCHDOG (#800): filho vivo mas sem publicar nada há
                # `_JOB_PARADO_S`, ou passou do teto duro. Sem isto o laço gira
                # para sempre segurando `_gen_lock` e o app inteiro para de gerar.
                # O relógio do sinal é o de PAREDE (o `mtime` é); o teto é
                # `monotonic()`. Misturar os dois faz disparar na hora ou nunca.
                try:
                    mtime = status_path.stat().st_mtime
                    if mtime > _visto[0]:
                        _visto[0] = mtime
                        ultimo_sinal = time.time()
                except OSError:
                    pass
                parado = time.time() - ultimo_sinal
                if parado > _JOB_PARADO_S or (time.monotonic() - t0) > _JOB_TETO_S:
                    _mata_grupo()
                    motivo = (f"worker travou: sem progresso há {parado:.0f}s"
                              if parado > _JOB_PARADO_S
                              else f"worker passou do teto de {_JOB_TETO_S:.0f}s")
                    print(f"[tts] {motivo} — job {job_id} abortado", flush=True)
                    final = ("err", motivo)
                    break
                time.sleep(0.35)
        finally:
            if proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:  # noqa: BLE001
                    try:
                        proc.kill()
                    except Exception:  # noqa: BLE001
                        pass
            try:
                log_f.close()
            except Exception:  # noqa: BLE001
                pass
            (pdir / "worker.pid").unlink(missing_ok=True)

        # processo saiu sem status done
        rc = proc.returncode
        err_tail = ""
        try:
            if log_path.exists():
                err_tail = _sanitiza_caminhos(
                    log_path.read_text(encoding="utf-8", errors="replace")[-500:])
        except Exception:  # noqa: BLE001
            pass
        if status_path.exists():
            try:
                st = json.loads(status_path.read_text())
                if st.get("status") == "done" and st.get("output"):
                    final = ("ok", st)
                elif st.get("status") == "error":
                    final = ("err", st.get("error") or "erro no worker")
            except Exception:  # noqa: BLE001
                pass
        if final is None:
            if rc == -11 or rc == 139:  # SIGSEGV
                msg = ("Worker crashou (SIGSEGV / Metal-MLX) ao gerar com "
                       f"{label}. O servidor continua no ar — tente qwen3-0.6b, "
                       "PocketTTS ou OmniVoice, ou reinicie e tente de novo.")
            elif rc and rc < 0:
                msg = f"Worker morto por sinal {-rc} ({label}). Servidor OK."
            else:
                msg = f"Worker saiu com código {rc} ({label})."
            if err_tail:
                msg += " | " + err_tail.replace("\n", " ")[:300]
            final = ("err", msg)

    # FORA do `_gen_lock` (#796 fila, #915 marca). O filho já soltou o Metal.
    # Sem isto o cliente pede o trecho (ou o wav final) ainda cru.
    if final is not None and final[0] == "ok":
        _marca_saida_isolada(pdir, final[1])
    elif last_pieces:
        for _i in range(last_pieces):
            _marca_arquivo(pdir / f"{_i}.wav")

    # FORA do `_gen_lock` (#796): aqui se espera a vez na fila
    if final is not None:
        if final[0] == "ok":
            _finish_ok(final[1])
        else:
            _finish_err(str(final[1]))


def _log(msg: str) -> None:
    """`print` que NUNCA derruba quem chamou (#768).

    Com o stdout num pipe sem leitor (`run.sh` órfão), o `print` levanta
    `BrokenPipeError` — e o do LUFS, dentro do `try` do job, marcava como `error` um
    áudio JÁ gravado. O log é diagnóstico: se não dá para escrever, segue."""
    try:
        print(msg, flush=True)
    except (BrokenPipeError, OSError, ValueError):
        pass


def _run_tts_job(job_id: str, text: str, voice_id: str, voice_path: Path,
                 language: str, omni: dict, model_override: str | None = None,
                 use_queue: bool = True):
    """use_queue=False pula a fila de falas: quem controla o tempo é o cliente.
    A Conversa toca frase a frase no navegador — se o gate também segurasse a
    entrega, a reserva (duração da fala anterior) viraria silêncio em dobro, e
    depois de um barge-in ficaria reservando uma fala que ninguém ouviu."""
    import numpy as np
    import soundfile as sf

    job = _jobs[job_id]
    remote = False
    local_carregou = False     # modelo local carregado no fallback? (libera no fim)
    sq = _speech_queue_begin(job_id) if use_queue else None
    hold_pieces = sq is not None  # com fila: grava trechos mas só libera na entrega
    # `model_override` = modelo DESTE pedido. Vale sem trocar o global: quem não é
    # admin manda `model` no body e gera com ele (ver /api/tts); admin também
    # persiste, então os dois caminhos ficam idênticos aqui.
    try:
        remote = _use_remote_tts()
        be = _current_backend(model_override)
        family = be["family"]
        be_meta = be.get("meta") or {}

        # Qwen/Fish/etc.: processo isolado (não trava/derruba o servidor)
        if not remote and family in _ISOLATED_FAMILIES:
            _run_tts_job_isolated(job_id, text, voice_id, voice_path, language, omni, be,
                                  sq=sq, model=model_override)
            # worker morreu: garante pool Metal limpo no pai + apaga trechos depois
            _release_mlx_memory(aggressive=True)
            st = job.get("status")
            _schedule_job_cleanup(job_id, delay_s=90 if st == "done" else 30)
            return

        chunks = _split_text(_sanitize_text(text), max_chars=_settings["chunk_max_chars"])
        job["total"] = len(chunks)
        pdir = _piece_dir(job_id)
        pdir.mkdir(exist_ok=True)
        _job_owner_write(pdir)      # dono: o boot de outro processo consulta

        # remoto: sem lock global -> jobs concorrentes (o servidor RTX paraleliza);
        # local: serializa load+gen no _gen_lock (evita 2 modelos MLX em paralelo / segfault).
        with (_NO_LOCK if remote else _gen_lock):
            started = time.time()
            job["status"] = "running"
            if not remote:
                job["progress"] = {
                    "stage": f"carregando {be['meta'].get('label') or be['id']}…",
                    "backend": be.get("id"), "family": family,
                }
                _model_state["progress"] = job["progress"]["stage"]
            model = None if remote else _get_model(model_override)
            sr = 24000 if remote else int(getattr(model, "sample_rate", 24000) or 24000)
            # voz de VOICE DESIGN salva: REGENERA do instruct+seed (determinístico) em
            # vez de clonar a amostra -> é EXATAMENTE a voz projetada, sem drift de clone.
            is_design = voice_id == DESIGN_VOICE_ID
            jpath = voice_path.with_suffix(".json")
            if not is_design and jpath.exists():
                try:
                    _vm = json.loads(jpath.read_text())
                    if _vm.get("from_design"):
                        is_design = True
                        omni = {**omni, "instruct": _vm.get("instruct") or omni.get("instruct"),
                                "seed": _vm.get("seed", omni.get("seed"))}
                except Exception:  # noqa: BLE001
                    pass
            # voz padrão ainda não materializada: cria a amostra-semente 1x (só OmniVoice)
            conds = None
            ref_text = None
            ref_audio = None
            rvoice = None
            if remote:
                # voz de design: ignora a "Voz remota" fixa (o timbre vem do instruct+seed).
                # senão: campo "Voz remota" fixo OU sobe a voz local e usa o nome dela
                rvoice = None if is_design else ((_settings.get("remote_tts_voice") or "").strip() or None)
                if not rvoice and not is_design and voice_path.exists():
                    job["progress"] = {"stage": "enviando voz ao servidor remoto…"}
                    rvoice = _ensure_remote_voice(voice_id, voice_path)
                    if not rvoice:
                        # upload falhou (rede/endpoint): o job segue com a voz
                        # PADRÃO do servidor remoto — avisar, senão parece voz errada
                        job["warning"] = ("voz local não pôde ser enviada ao "
                                          "servidor remoto — usando voz padrão")
                        job["progress"] = {"stage": "⚠ " + job["warning"]}
            elif is_design:
                ref_text = None       # sem ref de clone -> o timbre vem só do instruct
                conds = None
            elif family == "omnivoice":
                if voice_id in OMNI_PRESETS and not voice_path.exists():
                    job["progress"] = {"stage": "criando voz padrão…"}
                    _materialize_preset(model, sr, voice_id)
                ref_text = _voice_ref_text(voice_id)
                conds = _cond_for(model, voice_id, voice_path)
            else:
                # demais backends: passam o .wav da voz (se houver) como ref_audio
                if voice_path.exists():
                    ref_audio = str(voice_path)
                    ref_text = _voice_ref_text(voice_id)
                elif family in ("kokoro", "qwen3_custom", "pocket_tts", "voxtral_tts"):
                    # preset interno do modelo — sem amostra
                    pass
                elif family in ("qwen3_design",) or be_meta.get("voice_design"):
                    pass  # instruct basta
                elif voice_id in OMNI_PRESETS and family == "omnivoice":
                    pass
                else:
                    # backend exige clone mas não há sample: tenta mesmo assim (preset)
                    pass
            # anomalias ficam no job e no meta: um trecho que saiu mudo/truncado
            # depois das 2 tentativas não pode passar sem sinal (#303)
            anomalias: list[str] = []
            nao_resolvidas: list[str] = []
            soma_quad, n_amostras = 0.0, 0
            ganho_max = 0.0     # maior amplificação aplicada pela normalização (#439)

            # CAMINHO efetivo do job. Começa remoto quando pedido e vira LOCAL de
            # vez se o remoto falhar ou insistir em devolver áudio anômalo: trecho
            # perdido nunca, o pior caso é local lento (#304).
            alvo_remoto = remote
            if remote:
                local = {"model": None, "sr": sr, "conds": None,
                         "ref_text": None, "ref_audio": None}
            else:
                local = {"model": model, "sr": sr, "conds": conds,
                         "ref_text": ref_text, "ref_audio": ref_audio}

            def _prepara_local() -> bool:
                """Carrega o modelo local UMA vez, só no fallback. False = não deu."""
                if local["model"] is not None:
                    return True
                nonlocal local_carregou
                try:
                    with _gen_lock:          # Metal exclusivo, como o job local
                        m = _get_model(model_override)
                except Exception as exc:     # noqa: BLE001
                    _log(f"[tts] fallback local não carregou "
                          f"({type(exc).__name__}: {exc})")
                    return False
                local_carregou = True
                local["model"] = m
                local["sr"] = int(getattr(m, "sample_rate", sr) or sr)
                if is_design:
                    pass
                elif family == "omnivoice":
                    if voice_id in OMNI_PRESETS and not voice_path.exists():
                        try:
                            _materialize_preset(m, local["sr"], voice_id)
                        except Exception:    # noqa: BLE001 — preset que falha não impede
                            pass
                    local["ref_text"] = _voice_ref_text(voice_id)
                    if voice_path.exists():
                        local["conds"] = _cond_for(m, voice_id, voice_path)
                elif voice_path.exists():
                    local["ref_audio"] = str(voice_path)
                    local["ref_text"] = _voice_ref_text(voice_id)
                return True

            def _gera_local(chunk: str, i: int, sob_lock: bool = False):
                """Trecho pelo modelo LOCAL — caminho normal E fallback do remoto.

                `sob_lock` só é verdadeiro no FALLBACK: lá o job segura `_NO_LOCK`
                (entrou pelo remoto), então sem pegar o `_gen_lock` aqui a geração
                local correria junto com outro job no mesmo Metal. No caminho local
                o job já segura o lock; a trava fica em volta do `generate` e não
                de `_prepara_local` — com `RLock` a reentrada seria tolerada, mas
                pegar duas vezes só para chamar quem já pega esconde o desenho, e
                o `_prepara_local` também é chamado de fora (troca de modelo).
                """
                if not _prepara_local():
                    raise RuntimeError("sem modelo local para o trecho")
                motivo = ""
                for tentativa in (1, 2):
                    o_try = omni
                    if tentativa > 1:
                        o_try = dict(omni)
                        # 2ª tentativa: com seed fixa + temp 0 (greedy) a
                        # regeneração reproduz o MESMO áudio anômalo. Jitter só em
                        # voz de CLONE (timbre vem da ref, não do seed); em voice
                        # design o seed ancora o timbre e não pode mudar.
                        if not is_design:
                            seed = o_try.get("seed")
                            if seed is not None and int(seed) >= 0:
                                o_try["seed"] = int(seed) + tentativa
                            if not float(o_try.get("class_temperature") or 0):
                                o_try["class_temperature"] = 0.4
                            if float(o_try.get("temperature") or 0) < 0.5:
                                o_try["temperature"] = 0.7
                    with (_gen_lock if sob_lock else _NO_LOCK):
                        audio = _generate_chunk(
                            local["model"], chunk, language, local["conds"],
                            local["ref_text"], o_try, ref_audio=local["ref_audio"],
                            family=family, meta=be_meta, sr=local["sr"],
                        )
                    audio = _reamostra(audio, local["sr"], sr)
                    motivo = _motivo_anomalia(
                        audio, sr, chunk, speed=float(omni.get("speed") or 1.0))
                    if not motivo:
                        break
                    job["retries"] = job.get("retries", 0) + 1
                    anomalias.append(f"trecho {i + 1} (tentativa {tentativa}): {motivo}")
                    _log(f"[tts] trecho {i + 1}/{len(chunks)} anômalo "
                          f"[{motivo}] — tentativa {tentativa}/2")
                    if tentativa == 2:
                        # a 2ª também saiu ruim: o trecho entra assim mesmo, mas
                        # com sinal no job E no meta (nunca mudo silencioso)
                        nao_resolvidas.append(f"trecho {i + 1}: {motivo}")
                        job["warning"] = (f"{len(nao_resolvidas)} trecho(s) saíram "
                                          f"anômalos mesmo após nova tentativa")
                        _log(f"[tts] trecho {i + 1}/{len(chunks)} ENTROU anômalo "
                              f"[{motivo}] após 2 tentativas")
                    # retry: limpa tensores intermediários do generate falho
                    _release_mlx_memory()
                return audio
            for i, chunk in enumerate(chunks):
                # cancelamento no caminho LOCAL (#800): o job pode ser parado
                # entre trechos sem esperar o job inteiro
                if job.get("cancelado"):
                    _speech_queue_abort(sq)
                    job.update(status="error", error="cancelado pelo usuário",
                               progress=None, pieces=i)
                    _jobs_hist_salva()
                    _model_state.update(status="idle", progress=None, error=None)
                    return
                job["progress"] = {
                    "current": i + 1, "total": len(chunks),
                    "backend": be.get("id"), "family": family,
                }
                # pontuação do corte preservada: vírgula/dois-pontos NÃO viram
                # ponto (fim de frase no meio da frase — #319)
                chunk = _pontua_corte(chunk, ultimo=(i == len(chunks) - 1))
                audio = None
                caiu_agora = False     # este trecho veio do remoto? (só aí a trava é nossa)
                if alvo_remoto:
                    motivo = ""
                    for tentativa in (1, 2):
                        try:
                            audio = _tts_remote_chunk(chunk, language, omni, sr, rvoice)
                        except Exception as exc:      # noqa: BLE001
                            audio = None
                            motivo = f"{type(exc).__name__}: {exc}"
                            _log(f"[tts] remoto falhou no trecho {i + 1} "
                                  f"(tentativa {tentativa}/2): {motivo[:160]}")
                            continue
                        motivo = _motivo_anomalia(
                            audio, sr, chunk, speed=float(omni.get("speed") or 1.0))
                        if not motivo:
                            break
                        job["retries"] = job.get("retries", 0) + 1
                        anomalias.append(f"trecho {i + 1} remoto (tentativa {tentativa}): {motivo}")
                        _log(f"[tts] trecho {i + 1}/{len(chunks)} REMOTO anômalo "
                              f"[{motivo}] — tentativa {tentativa}/2")
                    if audio is None or motivo:
                        if not _fallback_ligado():
                            # desligado: comportamento antigo (erro ou trecho ruim entra)
                            if audio is None:
                                raise RuntimeError(f"TTS remoto falhou: {motivo}")
                            _log(f"[tts] remoto anômalo e fallback desligado — trecho "
                                  f"{i + 1} entra assim")
                        else:
                            # remoto fora (2 erros) ou 2 tentativas anômalas: o job
                            # passa a LOCAL e ESTE trecho é regerado local (nunca error)
                            alvo_remoto = False
                            job["remote_fallback"] = True
                            aviso = (f"TTS remoto falhou no trecho {i + 1} ({motivo}) — "
                                     f"job segue LOCAL")
                            _log(f"[tts] {aviso}")
                            job["warning"] = (f"{job['warning']} | {aviso}"
                                              if job.get("warning") else aviso)
                            audio = None
                            caiu_agora = True
                # cache (desligado por padrão): guarda o PCM já pós-processado, então
                # o acerto PULA a geração e o pós dos dois caminhos (#325)
                cache = _cache_audio()
                chave_audio = _cache_chave(
                    chunk, voice_id=voice_id, voice_path=voice_path, family=family,
                    model=str(_settings.get("model") or ""), language=language,
                    alvo_remoto=alvo_remoto, omni=omni) if cache is not None else ""
                guardado = (cache.pega(chave_audio, _tts_cache._ttl_s(_settings))
                            if chave_audio else None)
                if guardado is not None:
                    audio = guardado
                    job["cache_hits"] = job.get("cache_hits", 0) + 1
                else:
                    if audio is None:
                        audio = _gera_local(chunk, i, sob_lock=caiu_agora)
                    audio, ganho_trecho = _pos_processa_com_ganho(audio, sr)
                    ganho_max = max(ganho_max, ganho_trecho)
                    if chave_audio:
                        cache.guarda(chave_audio, audio, sr,
                                     ttl_s=_tts_cache._ttl_s(_settings),
                                     teto=_tts_cache._teto(_settings))
                soma_quad += float(np.sum(np.asarray(audio, dtype=np.float64) ** 2))
                n_amostras += int(np.asarray(audio).size)
                if i < len(chunks) - 1:
                    # pausa pela pontuação do trecho: vírgula respira, ponto respira
                    # fundo (#319). O último trecho não leva pausa nenhuma.
                    pausa = int(_pausa_apos(chunk[-1]) * sr)
                    audio = np.concatenate(
                        [audio, np.zeros(pausa, dtype=np.float32)])
                sf.write(pdir / f"{i}.wav", audio, sr, subtype="PCM_16")
                # solta o `_gen_lock` só aqui (#915) e repega antes de publicar
                _marca_arquivo(pdir / f"{i}.wav")
                del audio
                if not hold_pieces:
                    job["pieces"] = i + 1  # publica só depois da marca no disco
                # MLX acumula blocos no pool Metal; sem clear a RAM sobe a cada trecho
                if not alvo_remoto:
                    _release_mlx_memory()
                    _touch_use("tts")
        elapsed = round(time.time() - started, 1)

        # arquivo final do histórico = exatamente o que foi tocado (stream, sem concat total)
        out_id = uuid.uuid4().hex[:10]
        duration = _write_wav_concat(pdir, len(chunks), OUTPUTS_DIR / f"{out_id}.wav", sr)
        # UM ganho no job inteiro até a loudness alvo (EBU R128): o `normalize`
        # igualou os TRECHOS entre si; aqui o arquivo montado é ancorado num nível
        # de verdade, com ganho constante (não mexe no envelope). Não medir é
        # degradação: fica no log E no aviso do job (#302).
        lufs_db = _normaliza_loudness(OUTPUTS_DIR / f"{out_id}.wav")
        if lufs_db is None:
            _av = "nível final não medido (ffmpeg indisponível) — arquivo sem ajuste de loudness"
            _log(f"[tts] {_av}")
            job["warning"] = f"{job['warning']} | {_av}" if job.get("warning") else _av
        elif abs(lufs_db) > 0.05:
            _log(f"[tts] nível final ajustado {lufs_db:+.2f} dB (alvo {LUFS_ALVO:g} LUFS)")
        # métricas do ARQUIVO FINAL (pós-ganho): é o que o usuário ouve, e é o que
        # denuncia regressão de nível. Sem isso a única prova era o ouvido (#323).
        medido = _metricas_audio(OUTPUTS_DIR / f"{out_id}.wav")
        aviso_nivel = ""
        if medido.get("ilegivel"):
            aviso_nivel = "arquivo final ilegível para medir (métrica perdida)"
        elif medido["rms"] < RMS_AVISO:
            aviso_nivel = (f"nível final baixo (rms {medido['rms']:.4f} < {RMS_AVISO}) — "
                           f"confira o áudio antes de publicar")
        # fonte muito baixa não muda o rms final (a normalização devolve ao alvo):
        # quem denuncia é o ganho aplicado (#439)
        if ganho_max > GANHO_AVISO_DB:
            aviso_nivel = (f"{aviso_nivel + ' | ' if aviso_nivel else ''}"
                           f"fonte muito baixa: normalização amplificou até "
                           f"{ganho_max:.1f} dB (aviso acima de {GANHO_AVISO_DB:g} dB)")
        if aviso_nivel:
            _log(f"[tts] {aviso_nivel}")
            job["warning"] = (f"{job['warning']} | {aviso_nivel}"
                              if job.get("warning") else aviso_nivel)
        meta = {
            "id": out_id,
            "text": text,
            "voice_id": voice_id,
            "language": _omni_language(language) if family == "omnivoice" else language,
            "backend": be.get("id"),
            "family": family,
            "num_steps": int(omni.get("num_steps") or OMNI_STEPS_FAST),
            "guidance_scale": omni.get("guidance_scale"),
            "class_temperature": omni.get("class_temperature"),
            "instruct": omni.get("instruct") or "",
            "chunks": len(chunks),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "duration": duration,
            "elapsed": elapsed,
            # sinal auditável de degradação (#303): quantos retries, quais trechos
            # entraram mesmo anômalos e o nível final (rms 0 = arquivo mudo)
            "retries": int(job.get("retries", 0)),
            "anomalias": len(anomalias),
            "anomalias_motivos": anomalias[:20],
            "warning": job.get("warning") or "",
            "rms": round(medido["rms"], 4),
            "peak": round(medido["peak"], 4),
            "crest_db": round(medido["crest_db"], 2),
            "ilegivel": bool(medido.get("ilegivel")),
            "ganho_normalizacao_db": round(ganho_max, 1),
            "rms_bruto": round(float(np.sqrt(soma_quad / n_amostras)) if n_amostras else 0.0, 4),
            "lufs_ganho_db": lufs_db,
            "remote_fallback": bool(job.get("remote_fallback")),
            "cache_hits": int(job.get("cache_hits", 0)),
        }
        write_json_atomic(OUTPUTS_DIR / f"{out_id}.json", meta)
        # fila: espera o fim da fala anterior (duração real do último WAV) antes de entregar
        if sq is not None:
            job["progress"] = {"stage": "aguardando vez na fila…"}
            _speech_queue_deliver(sq, duration, output_meta=meta)
            job["pieces"] = len(chunks)
            if job.get("cancelado"):      # saiu da fila sem tocar (#835)
                job.update(status="error", error="cancelado pelo usuário",
                           output=meta, progress=None)
                _jobs_hist_salva()
                _schedule_job_cleanup(job_id, delay_s=90)
                return
        job.update(status="done", output=meta)
        _jobs_hist_salva()
        # trechos já gravados no final: limpa após graça p/ o cliente terminar o stream
        _schedule_job_cleanup(job_id, delay_s=90)
    except Exception as exc:  # noqa: BLE001
        _speech_queue_abort(sq)
        job.update(status="error", error=str(exc))
        _jobs_hist_salva()
        _schedule_job_cleanup(job_id, delay_s=30)
    finally:
        job["progress"] = None
        if _model_state.get("status") != "error":
            _model_state["progress"] = None
        if not remote or local_carregou:
            _release_mlx_memory(aggressive=True)


@app.post("/api/tts")
def synthesize(request: Request, payload: dict):
    """Aceita o pedido e devolve o job; valida ANTES de mexer em qualquer config.

    `model` no body é o modelo DESTE pedido. Ele também vira config global quando
    quem manda é admin/loopback (é o que a UI espera do seletor: escolheu, ficou
    escolhido); para chave de uso ele vale só no pedido — coerente com `model` ser
    campo admin no /api/settings. E, dos dois jeitos, só DEPOIS do pedido passar:
    antes, um 400 (texto vazio) ou 404 (voz) já tinha trocado `settings['model']`
    na RAM e no disco, com um job que nem rodou.
    """
    text = (payload.get("text") or "").strip()
    # vazio é 400 ANTES do `pre_prompt`: com o prefixo, um pedido sem texto virava
    # o próprio pre_prompt e voltava 200 com job (o teste do 400 caía).
    if not text:
        raise HTTPException(400, "Texto vazio")
    if _settings["pre_prompt"]:
        text = f"{_settings['pre_prompt']} {text}".strip()
    language = (payload.get("language") or _settings["language"]).lower()
    # `model` no body: resolvido aqui SEM mutar nada (a família decide as regras de
    # voz/design abaixo, então precisa valer já na validação).
    model_override = None
    if payload.get("model"):
        m = str(payload["model"]).strip()
        if m and m != "__custom__":
            # CATÁLOGO FECHADO para chave `use` (#787): string livre vira
            # `snapshot_download` de repo arbitrário (GBs em disco, e com o LFM3
            # código remoto via `trust_remote_code`). Quem manda string livre
            # precisa administrar.
            if not _admin_is_allowed(request) and not _modelo_no_catalogo(m):
                raise HTTPException(
                    403, f"Modelo '{m[:80]}' fora do catálogo — com esta chave use "
                         "um id da lista (ou peça admin para carregar repo/caminho)")
            model_override = m
    be = _current_backend(model_override)
    family = be["family"]
    be_meta = be.get("meta") or {}
    omni = _resolve_omni(payload, family=family)
    if len(text) > 5000:
        raise HTTPException(400, "Texto longo demais (máx. 5000 caracteres)")
    # preset internos (sem sample) ou design
    no_sample_ok = family in (
        "kokoro", "qwen3_custom", "qwen3_design", "pocket_tts", "voxtral_tts",
    ) or be_meta.get("voice_design")
    raw_voice = payload.get("voice_id") or _settings["default_voice"]
    if raw_voice == DESIGN_VOICE_ID or (isinstance(raw_voice, str)
            and raw_voice.strip().lower() in (DESIGN_VOICE_ID, "design")):
        voice_id = DESIGN_VOICE_ID
    elif raw_voice and _voice_path(raw_voice):
        voice_id = raw_voice
    elif raw_voice in OMNI_PRESETS:
        voice_id = raw_voice
    elif no_sample_ok and not raw_voice:
        voice_id = DESIGN_VOICE_ID  # backend com voz interna
    else:
        # clone backends: resolve por nome/id ou cai na voz mais recente
        try:
            voice_id = _resolve_voice(raw_voice)
        except HTTPException:
            if no_sample_ok:
                voice_id = DESIGN_VOICE_ID
            else:
                raise

    voice_path = VOICES_DIR / f"{voice_id}.wav"
    if voice_id == DESIGN_VOICE_ID:
        if not (omni.get("instruct") or "").strip() and not be_meta.get("voice_design") \
                and family not in ("qwen3_design", "qwen3_custom", "kokoro", "pocket_tts"):
            # design sem instruct em OmniVoice: usa instruct das settings se houver
            if family == "omnivoice" and not (omni.get("instruct") or "").strip():
                raise HTTPException(400, "Voice design vazio — descreva a voz no campo instruct")
    elif _use_remote_tts():
        pass  # o servidor remoto valida/mapeia a voz
    elif not voice_path.exists() and voice_id not in OMNI_PRESETS:
        if no_sample_ok:
            pass
        else:
            raise HTTPException(404, "Voz não encontrada — grave uma voz ou escolha uma voz padrão")

    # text[:200]: facilita depurar relatos de áudio mudo
    job_id = _jobs_admit({"text": text[:200]})

    # Pedido ACEITO (passou na validação e tem slot). O job já recebe o modelo do
    # pedido explicitamente; o que falta é decidir se ele também vira config GLOBAL:
    # - admin/loopback: sim (contrato do seletor da UI: escolhe e fica escolhido);
    # - chave de USO: não — `model` é campo admin no /api/settings, então a chave de
    #   uso gera com o modelo pedido sem sequestrar a configuração da instalação.
    # `_save_settings` é melhor-esforço: falha de disco não derruba a geração.
    # `persistir: false` no corpo: o modelo vale SÓ neste job. A UI não manda o
    # campo e segue com o contrato do seletor ("escolheu, ficou escolhido"); quem
    # roda suíte/curl contra o app do dono manda `false` e não deixa config trocada
    # atrás (#488). Ausente = persiste, como sempre foi.
    quer_persistir = payload.get("persistir")
    quer_persistir = True if quer_persistir is None else bool(quer_persistir)
    persistido = bool(model_override) and quer_persistir and _admin_is_allowed(request)
    if persistido and _settings.get("model") != model_override:
        _settings["model"] = model_override
        try:
            _save_settings()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(
        target=_run_tts_job,
        args=(job_id, text, voice_id, voice_path, language, omni, model_override),
        daemon=True,
    ).start()
    out = {"job_id": job_id, "backend": be.get("id"), "family": family}
    if model_override:
        # verdadeiro = o global agora é esse modelo; falso = valeu só neste pedido
        out["model"] = model_override
        out["model_aplicado_global"] = _settings.get("model") == model_override
    return out


@app.get("/api/tts/jobs/{job_id}")
def job_status(job_id: str):
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Job não encontrado")
    # CÓPIA rasa (#800): o job é mutado pela thread que gera enquanto o FastAPI
    # serializa. Devolver o dict vivo dava snapshot inconsistente (e, em campo
    # aninhado trocado no meio, `RuntimeError: changed size during iteration`).
    # `dict()` é barato e o valor aninhado que muda já é substituído inteiro.
    return dict(job)


@app.post("/api/tts/jobs/{job_id}/cancel")
@app.post("/api/tts/jobs/{job_id}/cancel")
def job_cancel(job_id: str):
    """Pede o cancelamento do job (#857).

    Marca `cancelado` no job e o laço do worker derruba o processo na volta
    seguinte. O campo evita que a tela case TEXTO de erro para saber que foi o
    dono quem parou (falha real — OOM, crash — não tem o campo)."""
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Job não encontrado")
    if job.get("status") in ("done", "error"):
        return {"ok": True, "ja_terminado": True,
                "cancelado": bool(job.get("cancelado"))}
    job["cancelado"] = True
    # job na FILA de falas: o lugar tem de sair AGORA, senão a fila atrás espera
    # por um job que já não vai tocar nada (#835)
    liberou = _speech_queue_cancela(job_id)
    return {"ok": True, "cancelado": True, "lugar_liberado": liberou}


@app.get("/api/tts/jobs/{job_id}/pieces/{index}")
def job_piece(job_id: str, index: int):
    """Só devolve trecho se o job já liberou `pieces` (fila de falas respeitada)."""
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Job não encontrado")
    # com fila: pieces fica 0 até a entrega — impede stream/cliente de tocar cedo
    try:
        liberados = int(job.get("pieces") or 0)
    except (TypeError, ValueError):
        liberados = 0
    if index < 0 or index >= liberados:
        raise HTTPException(404, "Trecho ainda não liberado")
    path = _piece_dir(job_id) / f"{index}.wav"
    if not path.exists():
        raise HTTPException(404, "Trecho não encontrado")
    return FileResponse(path, media_type="audio/wav")


@app.get("/api/outputs")
def list_outputs(request: Request, limit: int | None = None, offset: int = 0,
                 q: str = "", since: str = ""):
    """Histórico PAGINADO (`limit` 1–500, default 50; `offset`; `q` busca; `since`).

    Antes era `json.loads` de TODO o diretório por chamada (2.870 metas / 24 MB
    medidos) e a UI renderizava tudo: cada refresh relia o histórico inteiro.

    Ordenação e corte NÃO precisam do meta: o `mtime` do `.json` é a hora de
    criação (o arquivo nasce e nunca é reescrito) e ordena igual ao `created_at`,
    então o caminho quente é `stat` de todos + `json.loads` só da PÁGINA. O `q`
    casa por substring no texto CRU do arquivo (sem parse) e por isso é o único
    caminho que lê conteúdo; `since` (ISO `YYYY-MM-DD`, ou epoch) corta por data
    no `mtime`.

    Resposta PAGINADA: `{"items", "total", "limit", "offset"}` — `total` é a
    contagem ANTES do corte, para o cabeçalho e o "mostrar mais". `X-Total-Count`
    repete o número no header. Meta corrompido continua sendo pulado.

    COMPATIBILIDADE: cliente que chama a rota SEM parâmetro nenhum recebe a LISTA
    de sempre (tudo, como antes) — o formato só vira objeto quando algum filtro ou
    paginação é pedido. Assim um cliente antigo não quebra e a UI, que sempre manda
    `limit`, já entra no contrato paginado."""
    from fastapi.responses import JSONResponse
    # `limit` ausente = lista completa (compat); presente = clampado em 1–500
    modo_lista = (limit is None and offset == 0 and not (q or "").strip()
                  and not (since or "").strip())
    limite = 50 if limit is None else max(1, min(int(limit), 500))
    offset = max(0, int(offset or 0))
    # candidatos por stat: barato e estável (mesmo relógio do created_at)
    candidatos = []
    for f in OUTPUTS_DIR.glob("*.json"):
        try:
            st = f.stat()
        except OSError:
            continue
        candidatos.append((st.st_mtime, f.name, f))
    texto = (q or "").strip().lower()
    if texto:
        filtrados = []
        for item in candidatos:
            try:
                if texto in item[2].read_text(errors="replace").lower():
                    filtrados.append(item)
            except OSError:
                continue
        candidatos = filtrados
    corte = (since or "").strip()
    if corte:
        limite_ts = _ts_do_since(corte)
        if limite_ts is not None:
            candidatos = [c for c in candidatos if c[0] >= limite_ts]
    # mais novo primeiro; nome desempata mtime igual (ordem determinística)
    candidatos.sort(key=lambda c: (c[0], c[1]), reverse=True)
    total = len(candidatos)
    items = []
    fatiados = candidatos if modo_lista else candidatos[offset:offset + limite]
    for _mtime, _nome, f in fatiados:
        try:
            o = json.loads(f.read_text())
        except Exception:  # noqa: BLE001 — meta corrompido não pode derrubar a API
            continue
        if isinstance(o, dict):
            items.append(o)
    if modo_lista:                           # cliente antigo: a lista crua de antes
        return JSONResponse(items, headers={"X-Total-Count": str(total)})
    return JSONResponse({"items": items, "total": total, "limit": limite, "offset": offset},
                        headers={"X-Total-Count": str(total)})


def _ts_do_since(valor: str) -> float | None:
    """`since` em epoch (número) ou data/hora ISO → timestamp; None se ilegível.

    Do MAIS específico para o menos: um valor com hora não pode cair no formato
    de dia inteiro e virar meia-noite (perderia a hora que o cliente mandou)."""
    v = (valor or "").strip()
    try:
        return float(v)
    except (TypeError, ValueError):
        pass
    # aceita `T` ou espaço entre data e hora; só o prefixo reconhecido vale
    tentativas = (("%Y-%m-%dT%H:%M:%S", 19), ("%Y-%m-%dT%H:%M", 16),
                  ("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16),
                  ("%Y-%m-%d", 10))
    for formato, tamanho in tentativas:
        if len(v) < tamanho:
            continue
        try:
            return time.mktime(time.strptime(v[:tamanho], formato))
        except ValueError:
            continue
    return None


@app.get("/api/outputs/{out_id}/audio")
def output_audio(out_id: str):
    path = OUTPUTS_DIR / f"{_safe_id(out_id)}.wav"
    if not path.exists():
        raise HTTPException(404, "Áudio não encontrado")
    return FileResponse(path, media_type="audio/wav", filename=f"tts-studio-{out_id}.wav")


@app.delete("/api/outputs")
def delete_all_outputs(request: Request):
    _exige_admin(request)
    removidos = 0
    preservados = 0
    falhas = []
    # job ainda em `_jobs` (inclusive done): o speech pode estar lendo o WAV (#884)
    presos = _outputs_presos()
    for meta in OUTPUTS_DIR.glob("*.json"):
        if meta.stem in presos:
            preservados += 1
            continue
        try:
            meta.with_suffix(".wav").unlink(missing_ok=True)
            meta.unlink(missing_ok=True)
            removidos += 1
        except OSError as exc:
            # um arquivo travado não pode derrubar a resposta no meio já tendo
            # apagado o resto: acumula e devolve o balanço (#282)
            falhas.append(f"{meta.name}: {exc.strerror or exc}")
    corpo = {"ok": True, "removidos": removidos}
    if preservados:
        corpo["preservados"] = preservados
    if falhas:
        corpo["falhas"] = falhas[:20]
        corpo["falhas_total"] = len(falhas)
    return corpo


def _auto_cleanup_once():
    """Apaga áudios gerados mais antigos que o limite configurado."""
    limite = time.time() - _settings["auto_cleanup_minutes"] * 60
    # qualquer entrada ainda em `_jobs`, não só running/queued: o WAV de um
    # done na janela do histórico é o que o `/v1/audio/speech` lê (#884)
    presos = _outputs_presos()
    removidos = 0
    for meta in OUTPUTS_DIR.glob("*.json"):
        if meta.stem in presos:
            continue
        if meta.stat().st_mtime < limite:
            meta.with_suffix(".wav").unlink(missing_ok=True)
            meta.unlink(missing_ok=True)
            removidos += 1
    # diretórios de jobs (sínteses longas): a limpeza de *.json/*.wav não os
    # alcança e órfãos se acumulam. Pula quem ainda está em `_jobs` (apagar
    # trechos no meio da síntese quebraria o stream; done sai pela limpeza
    # agendada de 90 s, não por esta) e o resto sai por idade.
    for d in OUTPUTS_DIR.glob(".job-*"):
        if not d.is_dir() or d.stat().st_mtime >= limite:
            continue
        jid = d.name[len(".job-"):]
        if jid in presos:
            continue
        shutil.rmtree(d, ignore_errors=True)
        removidos += 1
    # Temporários de uploads podem sobreviver a timeout/crash. Limpa somente
    # arquivos conhecidos e antigos, nunca outputs finais.
    return removidos + _limpa_temporarios() + _limpa_temporarios_de_voices()


def _limpa_temporarios() -> int:
    """Remove uploads/temporários órfãos (mais velhos que 6 h).

    Roda mesmo com `auto_cleanup` desligado: eles nunca são histórico."""
    removidos = 0
    temp_cutoff = time.time() - 6 * 3600
    # `.live-*` (#554): temporário do Live tem sufixo `.wav` e, sem entrar aqui,
    # era imortal — a poda também não o come (nome com ponto). 301 medidos no
    # diretório do dono antes disto.
    for pattern in (".stt-*", ".pstt-*", ".voz-*", ".vozchk-*", ".up-*", ".rep-*",
                    ".live-*"):
        for tmp in OUTPUTS_DIR.glob(pattern):
            try:
                if tmp.is_file() and tmp.stat().st_mtime < temp_cutoff:
                    tmp.unlink(missing_ok=True)
                    removidos += 1
            except OSError:
                pass
    return removidos


def _limpa_temporarios_de_voices() -> int:
    """Remove temporários órfãos de `voices/` — crash entre o write e o replace.

    `.denoise-*` e `.orig-*` vivem em `voices/` de propósito (mesmo sistema de
    arquivos, `os.replace` atômico). Se o processo cair no meio, sobra o arquivo:
    ele não aparece na listagem (que anda por `*.json`) nem no export (que ignora
    ponto), mas fica no disco para sempre sem esta poda."""
    removidos = 0
    corte = time.time() - 6 * 3600
    # `copia-*` também no diretório de ESTADO: o rollback do import copia para
    # `settings.json`/`apikeys`/etc., que vivem fora de `voices/` — o temporário
    # nasce lá (`destino.parent`) e sem esta varredura ficava órfão para sempre
    # num crash. O prefixo é nosso, então varrer a raiz não pega arquivo alheio.
    for pasta in (VOICES_DIR, BASE):
        for tmp in pasta.glob(f"{_TMP_VOICES_PREFIX}copia-*"):
            try:
                if tmp.is_file() and tmp.stat().st_mtime < corte:
                    tmp.unlink(missing_ok=True)
                    removidos += 1
            except OSError:
                pass
    # os dois nomes antigos entram enquanto houver main que os gerou
    for pattern in (_TMP_VOICES_PREFIX + "*", ".denoise-*", ".orig-*"):
        for tmp in VOICES_DIR.glob(pattern):
            try:
                if tmp.is_file() and tmp.stat().st_mtime < corte:
                    tmp.unlink(missing_ok=True)
                    removidos += 1
            except OSError:
                pass
    return removidos


def _poda_outputs_por_quantidade(teto: int | None = None) -> int:
    """Apaga os áudios MAIS ANTIGOS que passam do teto de quantidade (#430).

    O tempo sozinho não segura o diretório: com `auto_cleanup` desligado (o
    default) `outputs/` chegou a 3.528 `.wav` — e a maioria sem `.json`, sobra de
    gerações antigas que nem aparecem na listagem. Aqui o corte é por EXCESSO
    (os recentes sempre ficam) sobre TODO `.wav` do diretório, com ou sem meta, e
    o que está em `_jobs` (job ativo ou ainda na janela do histórico) nunca sai,
    senão o trecho sumiria debaixo de quem está ouvindo. O id publicado é
    `output.id`, não a chave do job (#884). `teto` 0/negativo = desliga.

    Cada candidato é um id: sai o `.wav`, o `.json` par e o diretório de trechos.
    Temporários de upload (`.algo`) têm função própria e não entram na conta.
    """
    if teto is None:
        teto = int(_settings.get("auto_cleanup_max_outputs") or 0)
    if teto <= 0 or not OUTPUTS_DIR.exists():
        return 0
    # um candidato por id: o `.wav` responde pela idade; diretório de trechos
    # órfão (sem `.wav`) entra pela própria idade para não ficar para sempre
    idades: dict[str, float] = {}
    for wav in OUTPUTS_DIR.glob("*.wav"):
        if wav.name.startswith("."):
            continue               # temporário com sufixo .wav
        try:
            idades[wav.stem] = wav.stat().st_mtime
        except OSError:            # sumiu entre o glob e o stat: não conta
            pass
    for d in OUTPUTS_DIR.glob(".job-*"):
        oid = d.name[len(".job-"):]
        if oid in idades or not d.is_dir():
            continue
        try:
            idades[oid] = d.stat().st_mtime
        except OSError:
            pass
    for meta in OUTPUTS_DIR.glob("*.json"):      # meta sem áudio também ocupa lugar
        if meta.stem not in idades:
            try:
                idades[meta.stem] = meta.stat().st_mtime
            except OSError:
                pass
    excesso = len(idades) - teto
    if excesso <= 0:
        return 0
    presos = _outputs_presos()
    removidos = 0
    for oid, _mtime in sorted(idades.items(), key=lambda par: par[1]):
        if removidos >= excesso:
            break
        if oid in presos:          # job_id ou output.id (#884): preserva
            continue
        for nome in (f"{oid}.wav", f"{oid}.json"):
            (OUTPUTS_DIR / nome).unlink(missing_ok=True)
        shutil.rmtree(OUTPUTS_DIR / f".job-{oid}", ignore_errors=True)
        removidos += 1
    return removidos


def _auto_cleanup_loop():
    while True:
        time.sleep(60)
        try:
            if _settings["auto_cleanup"]:
                _auto_cleanup_once()      # por tempo (já inclui os temporários)
            else:
                # Mesmo com histórico permanente, temporários órfãos devem sair.
                _limpa_temporarios()
            # o teto de quantidade não depende de `auto_cleanup`: é proteção do
            # diretório, não preferência de retenção — 0 desliga (#430)
            _poda_outputs_por_quantidade()
        except Exception:  # noqa: BLE001
            pass


threading.Thread(target=_auto_cleanup_loop, daemon=True).start()
# idle unload sobe depois que _ser/_mt existem (ver fim do bloco de modelos)


@app.delete("/api/outputs/{out_id}")
def delete_output(request: Request, out_id: str):
    _exige_admin(request)
    out_id = _safe_id(out_id)
    # 409, não um 200 que mente: o arquivo continua porque um job ainda o cita (#884)
    if out_id in _outputs_presos():
        raise HTTPException(409, "Áudio ainda referenciado por um job")
    removed = False
    for ext in ("wav", "json"):
        path = OUTPUTS_DIR / f"{out_id}.{ext}"
        if path.exists():
            path.unlink()
            removed = True
    if not removed:
        raise HTTPException(404, "Áudio não encontrado")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Tradutor de voz (PoC): fala -> texto (STT) -> tradução -> fala traduzida na
# voz clonada (TTS). STT = mlx-whisper; tradução = mlx-lm (LLM local).
# ---------------------------------------------------------------------------

_stt_lock = threading.Lock()      # mlx-whisper não é thread-safe; serializa
_mt = {"model": None, "tok": None, "repo": None}
_mt_lock = threading.Lock()


def _mt_repo() -> str:
    return (_settings.get("translate_model") or "").strip() or TRANSLATE_REPO


def _unload_local_models(tts=True, stt=True, mt=True, ser=True,
                         espera_gen_s: float | None = None) -> dict:
    """Libera RAM descarregando os modelos LOCAIS (MLX/torch). Cada flag controla um motor.

    `espera_gen_s`: prazo para pegar o `_gen_lock` (None = espera indefinida, que é
    o certo no caminho de ociosidade). A rota passa um prazo curto: com job
    in-process longo o POST ficava pendurado até o fim da geração, sem timeout e
    sem explicação (#282). Estourou → TTS não é descarregado e o motivo volta em
    `tts_pendente`, para a rota devolver 409."""
    global _model
    freed = []
    tts_pendente = False
    if tts:
        tomou = True
        if espera_gen_s is not None:
            tomou = _gen_lock.acquire(timeout=max(0.0, espera_gen_s))
            tts_pendente = not tomou
        else:
            _gen_lock.acquire()
        try:
            if not tomou:
                pass                     # geração em curso: não encosta no modelo
            else:
                with _model_lock:
                    if _model is not None:
                        _model = None
                        freed.append("tts")
                    _conds_cache.clear()
                    _cache_limpa()
                    if _model_state.get("status") != "error":
                        # precision/path junto com o status: sem isto /api/status segue
                        # afirmando "ready, precision X, path Y" de um modelo que já
                        # saiu da RAM (unload por ociosidade / troca de precisão).
                        _model_state.update(status="idle", error=None, progress=None,
                                            precision=None, path=None)
                # o flush tem de caber no MESMO lock (#800): soltar o Metal e
                # devolver a página com uma geração já alocando disputa o pool —
                # e é justamente o instante em que o modelo acabou de sair
                _release_mlx_memory(aggressive=True)
        finally:
            if tomou:
                _gen_lock.release()
    if mt:
        with _mt_lock:
            if _mt.get("model") is not None:
                _mt["model"] = _mt["tok"] = None
                _mt["repo"] = None
                freed.append("tradutor")
    if stt:                                  # mlx-whisper cacheia o modelo em ModelHolder (classe)
        with _stt_lock:                  # transcrição em curso não pode ver o holder sumir
            try:
                from mlx_whisper.transcribe import ModelHolder
                if ModelHolder.model is not None:
                    ModelHolder.model = None
                    ModelHolder.model_path = None
                    freed.append("whisper")
            except Exception:  # noqa: BLE001
                pass
            if _pk["model"] is not None:
                _pk["model"] = None
                _pk["repo"] = ""
                freed.append("parakeet")
    if ser:
        with _ser_lock:
            if _ser.get("clf") is not None:
                _ser["clf"] = None
                freed.append("ser")
    if not tts:                      # sem TTS não houve lock: libera aqui mesmo
        _release_mlx_memory(aggressive=True)
    # pipeline SER / transformers às vezes puxa torch — tenta soltar cache MPS/CPU
    try:
        import torch
        if hasattr(torch, "mps") and hasattr(torch.mps, "empty_cache"):
            torch.mps.empty_cache()
        if hasattr(torch, "cuda") and torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001
        pass
    corpo = {"unloaded": freed}
    if tts_pendente:
        corpo["tts_pendente"] = "geração em curso segurando o lock; tente depois"
    return corpo


def _schedule_job_cleanup(job_id: str, delay_s: float = 90):
    """Remove .job-* (trechos WAV) depois da graça — cliente já tem o áudio final."""
    def _clean():
        time.sleep(max(5.0, delay_s))
        shutil.rmtree(_piece_dir(job_id), ignore_errors=True)
    threading.Thread(target=_clean, daemon=True).start()


def _whisper_de_arquivo(caminho: str | None) -> bool:
    """True se este caminho é um Whisper que o Live não usa.

    O turbo (ou o repo que `live_stt_whisper_repo` apontar) fica. O large-v3
    da transcrição de arquivo, e qualquer outro repo diferente do Live, sai."""
    if not caminho:
        return False
    return str(caminho) != _whisper_repo(live=True)


def _whisper_arquivo_marca() -> None:
    """Marca o uso do Whisper de arquivo. O Live não entra neste relógio."""
    global _whisper_arquivo_uso
    if getattr(_stt_ctx, "live", False):
        return
    if not _whisper_de_arquivo(_whisper_repo(live=False)):
        return
    _whisper_arquivo_uso = time.monotonic()


def _whisper_arquivo_passo() -> bool:
    """Uma checagem do TTL do Whisper de arquivo. True = liberou agora.

    Separado do laço para a célula chamar direto (#891). `whisper_arquivo_ttl_s`
    0 desliga. O holder só é solto quando o caminho carregado não é o do Live,
    então o turbo residente do turno não sai por este relógio."""
    ttl = float(_settings.get("whisper_arquivo_ttl_s") or 0)
    if ttl <= 0:
        return False
    with _stt_lock:
        try:
            from mlx_whisper.transcribe import ModelHolder
        except Exception:  # noqa: BLE001 — sem a lib não há modelo a soltar
            return False
        if ModelHolder.model is None or not _whisper_de_arquivo(ModelHolder.model_path):
            return False
        if _whisper_arquivo_uso <= 0:
            return False
        if time.monotonic() - _whisper_arquivo_uso < ttl:
            return False
        modelo = ModelHolder.model
        ModelHolder.model = None
        ModelHolder.model_path = None
    del modelo
    gc.collect()
    _release_mlx_memory(aggressive=True)
    return True


def _idle_unload_loop():
    """Descarrega motores ociosos após idle_unload_minutes (0 = desligado).

    O large-v3 de arquivo tem relógio próprio e corre mesmo com o idle geral
    desligado: o Live renova `_last_use["stt"]` e seguraria o pesado (#891)."""
    while True:
        time.sleep(30)
        try:
            _whisper_arquivo_passo()
            mins = float(_settings.get("idle_unload_minutes") or 0)
            if mins <= 0:
                continue
            limit = mins * 60.0
            now = time.time()
            # não descarrega TTS no meio de um job (snapshot: iterar a view viva estoura
            # quando uma rota admite job na mesma hora)
            busy_tts = any(
                j.get("status") == "running" for j in _jobs_snapshot()
            )
            tts_idle = (not busy_tts) and _last_use["tts"] > 0 and (now - _last_use["tts"]) >= limit
            stt_idle = _last_use["stt"] > 0 and (now - _last_use["stt"]) >= limit
            mt_idle = _last_use["mt"] > 0 and (now - _last_use["mt"]) >= limit
            ser_idle = _last_use["ser"] > 0 and (now - _last_use["ser"]) >= limit
            if not (tts_idle or stt_idle or mt_idle or ser_idle):
                continue
            # só descarrega o que realmente está carregado e ocioso
            need_tts = tts_idle and _model is not None
            need_stt = stt_idle
            need_mt = mt_idle and _mt.get("model") is not None
            need_ser = ser_idle and _ser.get("clf") is not None
            # whisper: ModelHolder pode ter modelo sem _last_use se nunca tocou — ok
            if need_tts or need_stt or need_mt or need_ser:
                r = _unload_local_models(
                    tts=need_tts, stt=need_stt, mt=need_mt, ser=need_ser,
                )
                if r.get("unloaded"):
                    for k, flag in (("tts", need_tts), ("stt", need_stt),
                                    ("mt", need_mt), ("ser", need_ser)):
                        if flag:
                            _last_use[k] = 0.0
        except Exception:  # noqa: BLE001
            pass


def _autofree_local():
    """Se a opção estiver ligada, descarrega os locais cujo remoto está ativo."""
    if not _settings.get("free_local_on_remote"):
        return
    _unload_local_models(
        tts=_use_remote_tts(), stt=_use_remote_stt(),
        mt=_use_remote_translate(), ser=False,
    )


# alucinações comuns do Whisper em silêncio/ruído (pt + en). Comparadas após
# normalizar (lower + tira pontuação/aspas das pontas), então cobrem variações.
_STT_BLACKLIST = {
    "obrigado", "obrigada", "tchau", "valeu", "fim", "the end",
    "thank you", "thank you very much", "thanks for watching", "you", "bye", "okay", "ok",
    "legendas pela comunidade amara.org", "amara.org", "subtitles by the amara.org community",
    "♪", "...", ".", "music", "música", "applause", "aplausos",
    # interjeições/fragmentos curtos típicos de ruído (match é da frase INTEIRA)
    "e aí", "e ai", "aí", "hum", "hmm", "uhum", "ãhã", "ã", "ahn", "eh",
    "uh", "uhn", "um", "ó", "ahã", "mm", "mhm", "thanks",
}


def _ffmpeg_to_mono16k(audio_path: Path):
    """Decodifica qualquer formato que o FFMPEG entenda (webm/opus do
    MediaRecorder, m4a, mp4, ogg…) direto para float32 mono 16 kHz."""
    import numpy as np

    p = subprocess.run([FFMPEG, "-v", "error", "-i", str(audio_path),
                        "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
                       capture_output=True, timeout=120)
    if p.returncode != 0 or not p.stdout:
        raise RuntimeError((p.stderr or b"").decode("utf-8", "replace").strip()[:160]
                           or "ffmpeg não decodificou o áudio")
    return np.frombuffer(p.stdout, dtype="<f4").copy()


def _wav_to_mono16k(audio_path: Path):
    """Devolve array float32 mono a 16 kHz.

    Caminho normal: soundfile (sem processo externo), que já é o bastante para
    WAV vindo do navegador — evita o load_audio do whisper e o ffmpeg_read do
    transformers. Quando o libsndfile não reconhece o formato (ele não lê
    matroska: webm/opus do MediaRecorder, m4a, mp4), cai no FFMPEG do projeto
    — que tem o binário do imageio-ffmpeg como reserva, então não depende de
    ffmpeg no PATH. Sem esse fallback, quem chamasse isto com o blob cru
    levava LibsndfileError (ou, via `_vad_tem_fala`, um fail-open silencioso)."""
    import numpy as np
    import soundfile as sf

    try:
        audio, sr = sf.read(str(audio_path), dtype="float32")
    except Exception:  # noqa: BLE001 — formato que o libsndfile não lê: usa ffmpeg
        return _ffmpeg_to_mono16k(audio_path)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if int(sr) != 16000:
        from math import gcd
        from scipy.signal import resample_poly
        g = gcd(int(sr), 16000)
        audio = resample_poly(audio, 16000 // g, int(sr) // g)
    return np.ascontiguousarray(audio, dtype=np.float32)


# --- Modelos remotos (API OpenAI-compatível): tradução e transcrição opcionais ---
def _env_base(nome: str) -> str:
    """Base vinda do AMBIENTE (o override do #103/#107). Vazio = sem override."""
    return (os.environ.get(nome) or "").strip()


def _remote_ready() -> bool:
    # base_url basta; api_key é opcional (endpoints em LAN, ex.: RTX, não têm auth)
    return bool(_settings.get("remote_base_url"))


def _use_remote_translate() -> bool:
    """Caminho remoto do tradutor.

    ENV PRIMEIRO e já basta: com `TTS_TRANSLATE_BASE_URL` no ambiente o remoto está
    ativo mesmo com URLs/toggles vazios no settings — senão o override ficava
    INERTE e o cliente caía no local em silêncio (medido no gate #109: 200 com texto
    vazio em vez do erro do provedor). Sem env, a regra antiga (toggle + base)."""
    return bool(_env_base("TTS_TRANSLATE_BASE_URL")) or (
        bool(_settings.get("remote_translate")) and _remote_ready())


def _use_remote_stt() -> bool:
    # idem: env dedicado do STT (ou a base do tradutor) basta para o remoto
    return bool(_env_base("TTS_STT_BASE_URL") or _env_base("TTS_TRANSLATE_BASE_URL")) \
        or (bool(_settings.get("remote_stt")) and bool(
            _settings.get("remote_stt_base_url") or _settings.get("remote_base_url")))


def _fallback_ligado() -> bool:
    """Remoto que falha pode cair para LOCAL? Ligado por padrão (#304).

    `TTS_REMOTE_TTS_FALLBACK=0` (ou `remote_tts_fallback: false` nas settings)
    volta ao comportamento antigo: falha persistente do remoto derruba o job.
    """
    env = (os.environ.get("TTS_REMOTE_TTS_FALLBACK") or "").strip()
    if env:
        return env != "0"
    return bool(_settings.get("remote_tts_fallback", True))


def _use_remote_tts() -> bool:
    return bool(_settings.get("remote_tts")) and bool(_settings.get("remote_tts_url"))


def _extra_remoto(bruto) -> dict:
    """JSON de `remote_tts_extra` saneado: sem `text`/`voice` (por trecho).

    Chave fora de formato → dict vazio (o padrão do provedor vale). Avisa UMA vez
    por valor problemático: override ignorado é degradação silenciosa (#282)."""
    texto = (bruto or "").strip()
    if not texto:
        return {}
    try:
        extra = json.loads(texto)
    except (ValueError, TypeError):
        if texto not in _extra_remoto_avisados:
            _extra_remoto_avisados.add(texto)
            print("[tts-remoto] remote_tts_extra não é JSON válido — ignorado "
                  f"({texto[:60]!r})", file=sys.stderr, flush=True)
        return {}
    if not isinstance(extra, dict):
        return {}
    for campo in ("text", "voice"):
        if campo in extra:
            # uma vez por (campo, valor): o helper roda por TRECHO, e um job de 30
            # trechos repetiria a mesma linha 30x no stderr
            if (campo, texto) not in _extra_remoto_avisados:
                _extra_remoto_avisados.add((campo, texto))
                print(f"[tts-remoto] remote_tts_extra traz '{campo}' — não vale "
                      f"para todos os trechos, ignorado", file=sys.stderr, flush=True)
            extra = {k: v for k, v in extra.items() if k != campo}
    return extra


_extra_remoto_avisados: set = set()


def _remote_origin() -> str:
    """scheme://host:port da URL do TTS remoto (p/ os endpoints /voices).

    `urlsplit` vem do import do topo (desde a guarda de `Origin`, #290) — o
    import local daqui ficou redundante e saiu."""
    u = urlsplit((_settings.get("remote_tts_url") or "").strip())
    return f"{u.scheme}://{u.netloc}" if u.scheme and u.netloc else ""


#: `voice_id` -> (origin, nome, mtime_ns) do envio remoto já feito. Dict SEM teto
#: crescia com o uso e a entrada não saía na deleção da voz (#282): teto com
#: evicção do mais ANTIGO (ordem de inserção) + `pop` explícito na deleção.
_REMOTE_VOICE_CACHE_MAX = 64
_remote_voice_cache: dict = {}


def _remote_voice_cache_guarda(voice_id: str, key: tuple) -> None:
    _remote_voice_cache[voice_id] = key
    while len(_remote_voice_cache) > _REMOTE_VOICE_CACHE_MAX:
        _remote_voice_cache.pop(next(iter(_remote_voice_cache)), None)


def _ensure_remote_voice(voice_id: str, voice_path: Path):
    """Garante que a voz local exista no servidor remoto (sobe 1x por mtime).
    Devolve o nome remoto (sanitizado) ou None se não der."""
    import re as _re

    import requests

    origin = _remote_origin()
    if not origin or not voice_path.exists():
        return None
    name = _re.sub(r"[^a-zA-Z0-9_-]", "_", _voice_display_name(voice_id))[:64] or "voz"
    key = (origin, name, voice_path.stat().st_mtime_ns)
    if _remote_voice_cache.get(voice_id) == key:
        return name
    headers = {}
    if _settings.get("remote_api_key"):
        headers["Authorization"] = f"Bearer {_settings['remote_api_key']}"
    try:
        with open(voice_path, "rb") as fh:
            r = requests.post(f"{origin}/voices", headers=headers,
                              files={"audio": ("voz.wav", fh, "audio/wav")},
                              data={"name": name, "ref_text": _voice_ref_text(voice_id) or ""},
                              timeout=120)
        if r.ok:
            _remote_voice_cache_guarda(voice_id, key)
            return r.json().get("voice", name)
    except Exception:  # noqa: BLE001
        pass
    return None


def _voice_display_name(voice_id: str) -> str:
    try:
        return json.loads((VOICES_DIR / f"{voice_id}.json").read_text()).get("name") or voice_id
    except Exception:  # noqa: BLE001
        return voice_id


def _reamostra(audio, de: int, para: int):
    """Reamostra para o sample-rate do job (o modelo local pode não ser 24 kHz).

    O job remoto fixa `sr` em 24000; se o fallback local carregar um modelo de
    outra taxa, misturar as duas no mesmo WAV tocaria na velocidade errada.
    """
    import numpy as np

    audio = np.asarray(audio, dtype=np.float32)
    if not de or not para or int(de) == int(para):
        return audio
    from math import gcd

    from scipy.signal import resample_poly

    g = gcd(int(de), int(para))
    return np.asarray(resample_poly(audio, int(para) // g, int(de) // g),
                      dtype=np.float32)


def _tts_remote_chunk(text: str, language: str, omni: dict, sr: int = 24000, voice: str = None):
    """Encaminha um trecho ao servidor de TTS remoto (ex.: OmniVoice numa RTX) e
    devolve o áudio como array float32 no sample-rate local. Manda os params do
    OmniVoice configurados + text/language (+ voice se houver clone remoto)."""
    import io

    import numpy as np
    import requests
    import soundfile as sf

    headers = {"Content-Type": "application/json"}
    if _settings.get("remote_api_key"):
        headers["Authorization"] = f"Bearer {_settings['remote_api_key']}"
    # OmniVoice (masked-diffusion): params canônicos do generate
    url = _settings["remote_tts_url"].strip()
    body = {
        "num_steps": int(omni.get("num_steps") or OMNI_STEPS_FAST),
        "guidance_scale": omni.get("guidance_scale", 2.0),
        "class_temperature": omni.get("class_temperature", 0.0),
        "position_temperature": omni.get("position_temperature", 5.0),
        "layer_penalty_factor": omni.get("layer_penalty_factor", 5.0),
        "t_shift": omni.get("t_shift", 0.1),
        "speed": 1.0,   # servidor gera na duração natural; velocidade vira time-stretch local
    }
    # params extras do OmniVoiceGenerationConfig (o server RTX faz whitelist)
    for k in ("denoise", "preprocess_prompt", "postprocess_output",
              "audio_chunk_duration", "audio_chunk_threshold"):
        if omni.get(k) is not None:
            body[k] = omni[k]
    if (omni.get("instruct") or "").strip():
        body["instruct"] = omni["instruct"]
    if omni.get("seed") is not None and int(omni["seed"]) >= 0:
        body["seed"] = int(omni["seed"])
    if omni.get("duration_s") is not None:
        body["duration_s"] = omni["duration_s"]
    if voice:
        body["voice"] = voice
    body["text"] = text
    lang_name = LANG_DISPLAY.get((language or "").lower())
    if lang_name and "language" not in body:
        body["language"] = lang_name
    # JSON extra do usuário sobrepõe/adiciona — MENOS `text`/`voice`, que são
    # por TRECHO: com eles no extra, um único valor fixo sobrescrevia o texto de
    # todo trecho do job (o campo passou a ser admin, mas admin também se
    # surpreendia). Ver `_extra_remoto`.
    body.update(_extra_remoto(_settings.get("remote_tts_extra")))
    # 1 retry p/ falha transitória (rede/Wi-Fi/5xx): um erro no trecho 20 de 30
    # não pode matar o job inteiro
    r = None
    for tentativa in (1, 2):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=300)
        except requests.RequestException as exc:
            r = None
            if tentativa == 2:
                raise RuntimeError(f"TTS remoto inacessível (2 tentativas): {exc}") from exc
            time.sleep(2.0)
            continue
        if r.status_code < 500 or tentativa == 2:
            break
        time.sleep(2.0)   # 5xx: servidor ocupado/carregando modelo — tenta de novo
    if r is None or not r.ok:
        raise RuntimeError(f"TTS remoto falhou ({getattr(r, 'status_code', '?')}): "
                           f"{getattr(r, 'text', '')[:200]}")
    data, src_sr = sf.read(io.BytesIO(r.content), dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    if int(src_sr) != sr:
        from math import gcd

        from scipy.signal import resample_poly
        g = gcd(int(src_sr), sr)
        data = resample_poly(data, sr // g, int(src_sr) // g)
    data = np.asarray(data, dtype=np.float32)
    # velocidade: time-stretch (preserva tom, não pula palavras), igual ao local
    speed = float(omni.get("speed") or 1.0)
    if abs(speed - 1.0) > 1e-3:
        data = _time_stretch(data, speed, sr)
    return data


# rótulo PT da emoção -> palavra inglesa p/ o prompt do LLM
_EMO_EN = {
    "alegre": "happy and upbeat", "triste": "sad and downcast", "raiva": "angry and intense",
    "medo": "fearful and tense", "surpresa": "surprised and excited", "calmo": "calm and gentle",
    "desgosto": "displeased", "suave": "soft and gentle", "intenso": "intense and firm",
    "animado": "lively and enthusiastic", "ágil": "lively",
}


def _translate_prompt(text: str, target: str, emotion: str | None = None) -> str:
    nome = LANG_DISPLAY.get(target, target)
    if target == "pt":   # evita PT-europeu ("está a cair") — fixa o alvo no Brasil
        nome = "Brazilian Portuguese (português do Brasil, registro coloquial brasileiro)"
    emo_en = _EMO_EN.get((emotion or "").lower()) if emotion and emotion != "neutro" else None
    base = (
        f"You are an expert {nome} translator and localizer. Render the text into "
        f"natural, idiomatic {nome} exactly as a native speaker would say it out loud — "
        f"translate the MEANING and intent, never word-for-word. Use native phrasing, "
        f"idioms, contractions and the same register and tone; rephrase anything that "
        f"would sound literal, stiff or translated. Keep proper names. Do not add or omit "
        f"information. Output ONLY the {nome} translation — no quotes, no notes, no original."
    )
    if emo_en:
        base += (f" Word it so it sounds {emo_en} when spoken aloud "
                 f"(punctuation, emphasis, natural interjections) without changing the meaning.")
    return f"{base}\n\nText: {text}"


def _traducao_remota_cfg() -> tuple[str, str]:
    """(base_url, modelo) do tradutor remoto — env > settings, com erro EXPLICATIVO.

    NÃO passa pelo `_chat_provider` de propósito (decisão do PM no mini-gate): o
    tradutor fala com o 14B do RTX pelo OmniVoice, unificar mudaria a semântica.
    `TTS_TRANSLATE_BASE_URL`/`TTS_TRANSLATE_MODEL` existem para smoke isolado, no
    mesmo padrão do `TTS_CHAT_*`.
    Base vazia antes virava `MissingSchema: Invalid URL '/chat/completions'` cru."""
    base = (os.environ.get("TTS_TRANSLATE_BASE_URL") or "").strip() \
        or (_settings.get("remote_base_url") or "").strip()
    if not base:
        raise HTTPException(400, "Tradutor remoto não configurado — informe a Base URL "
                                 "do provedor (Configurações → Rede) ou defina "
                                 "TTS_TRANSLATE_BASE_URL")
    modelo = (os.environ.get("TTS_TRANSLATE_MODEL") or "").strip() \
        or (_settings.get("remote_translate_model") or "gpt-4o-mini")
    return base.rstrip("/"), modelo


def _translate_remote(text: str, target: str, emotion: str | None = None) -> str:
    import requests

    base, modelo = _traducao_remota_cfg()
    r = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {_settings['remote_api_key']}",
                 "Content-Type": "application/json"},
        json={"model": modelo,
              "temperature": 0.4 if emotion else 0.2,
              "messages": [{"role": "user", "content": _translate_prompt(text, target, emotion)}]},
        timeout=60,
    )
    if not r.ok:
        raise RuntimeError(f"tradução remota falhou ({r.status_code}): {r.text[:200]}")
    return r.json()["choices"][0]["message"]["content"].strip().strip('"').strip()


def _transcribe_remote(audio_path: Path, language: str | None):
    import requests

    # URL/chave dedicadas do STT, se definidas; senão as compartilhadas (RTX).
    # Mesmo defeito do tradutor se ambas estiverem vazias: mensagem explicativa.
    base = ((os.environ.get("TTS_STT_BASE_URL") or "").strip()
            or (_settings.get("remote_stt_base_url") or "").strip()
            or (_settings.get("remote_base_url") or "").strip())
    if not base:
        raise HTTPException(400, "STT remoto não configurado — informe a URL do "
                                 "provedor (Configurações → Rede) ou defina TTS_STT_BASE_URL")
    base = base.rstrip("/")
    key = _settings.get("remote_stt_key") or _settings.get("remote_api_key") or ""
    data = {"model": _settings.get("remote_stt_model") or "whisper-1",
            "response_format": "verbose_json",
            "beam_size": int(_settings.get("stt_beam", 5))}   # qualidade↔velocidade
    if language and language not in ("auto",):
        data["language"] = language
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    with open(audio_path, "rb") as fh:
        r = requests.post(
            f"{base}/audio/transcriptions",
            headers=headers,
            files={"file": ("audio.wav", fh, "audio/wav")}, data=data, timeout=(5, 60),
        )
    if not r.ok:
        raise RuntimeError(f"transcrição remota falhou ({r.status_code}): {r.text[:200]}")
    j = r.json()
    return {"text": j.get("text", ""), "language": (j.get("language") or "").strip().lower(),
            "segments": j.get("segments") or []}


_vad_model = None


_vad_backend = ""  # "onnx" | "torch-jit" — caminho realmente carregado
_vad_erro = ""     # último erro do Silero ("" = nenhum) — fail-open VISÍVEL
# O modelo é GLOBAL e o `get_speech_timestamps` chama `reset_states()`: sem trava,
# duas transcrições concorrentes embolam o estado RNN um do outro (#320). RLock
# porque o load pode acontecer dentro do caminho que já segura a trava.
_vad_lock = threading.RLock()


def _vad_load():
    """Silero VAD carregado sob demanda (~1MB, CPU, roda em tempo real).

    Pede o modelo ONNX EXPLICITAMENTE: `load_silero_vad()` sem argumento não
    basta porque o default mudou de versão — no silero-vad 5.x era onnx=True,
    no 6.x é onnx=False. Com 6.x instalado, o app carregava o jit do torch
    mesmo com onnxruntime presente, e o `except` nunca disparava (os dois
    ramos carregavam o mesmo modelo).

    NÃO é "mais leve": medido no M3, o ONNX soma ~30–40 MB de RSS (o silero_vad
    importa torch no topo, então o torch entra nos dois caminhos) e empata em
    velocidade fora do cold start. Fica ONNX por ser o caminho declarado no
    requirements, por não desserializar torchscript e por tornar o fallback
    explícito e logado. Sem onnxruntime o load estoura e o fallback é o jit do
    torch (mesmo resultado, dependência extra)."""
    global _vad_model, _vad_backend
    with _vad_lock:
        if _vad_model is None:
            from silero_vad import load_silero_vad
            try:
                _vad_model = load_silero_vad(onnx=True)
                _vad_backend = "onnx"
            except Exception as e:  # noqa: BLE001 — sem onnxruntime: cai no torch
                print(f"[vad] modelo ONNX indisponível ({str(e)[:120]}) — caindo no torch jit",
                      flush=True)
                _vad_backend = "torch-jit"
                _vad_model = load_silero_vad(onnx=False)
    return _vad_model


def _vad_fala_s(audio_path: Path) -> float | None:
    """Segundos de FALA pelo Silero (None = VAD indisponível, fail-open).

    Duração de fala, não de arquivo: um cadastro de 8 s com 2 s de voz e 6 s de
    silêncio não serve para biometria (#525)."""
    global _vad_erro
    try:
        import torch
        from silero_vad import get_speech_timestamps
        audio = _wav_to_mono16k(audio_path)
        with _vad_lock:
            speech = get_speech_timestamps(torch.from_numpy(audio), _vad_load(),
                                           sampling_rate=16000, threshold=0.6,
                                           speech_pad_ms=100)
        _vad_erro = ""
        return sum((t["end"] - t["start"]) for t in speech) / 16000
    except Exception as exc:  # noqa: BLE001 — Silero indisponível não bloqueia
        msg = f"{type(exc).__name__}: {exc}"
        if msg != _vad_erro:
            print(f"[vad] {msg[:160]} — fail-open, o trecho segue como fala", flush=True)
        _vad_erro = msg
        return None


def _snr_estimado(audio_path: Path) -> float | None:
    """SNR estimado em dB (None = não deu para medir).

    Piso de ruído = percentil 10 das potências por quadro; fala = percentil 95.
    A distância entre os dois é o que separa "gravei na ventoinha" de "gravei na
    sala" — o R2 mediu ruído a 5 dB degradando todos os modelos (#525)."""
    try:
        import numpy as np
        import soundfile as sf

        a, sr = sf.read(str(audio_path), dtype="float32")
        if a.ndim > 1:
            a = a.mean(axis=1)
        quadro = max(1, int(0.02 * sr))
        n = a.size // quadro
        if n < 5:
            return None
        pot = np.square(a[: n * quadro]).reshape(n, quadro).mean(axis=1)
        ruido = float(np.percentile(pot, 10)) or 1e-12
        fala = float(np.percentile(pot, 95))
        if fala <= 0:
            return None
        return round(10 * float(np.log10(fala / ruido)), 1)
    except Exception:  # noqa: BLE001 — sem medida, não bloqueia
        return None


def _vad_tem_fala(audio_path: Path, minimo_s: float = 0.3) -> bool:
    """Silero VAD: True se o wav tem fala de verdade (não ruído).

    Reamostra para 16 kHz antes do modelo: o Silero só aceita 8/16 kHz e o
    navegador manda WAV a 24 kHz — sem a reamostragem o modelo estourava e o
    `except` devolvia True, ou seja, o guarda anti-alucinação era no-op em todo
    áudio vindo de arquivo (ruído puro passava como fala).
    Em caso de erro no Silero, devolve True (não bloqueia o pipeline)."""
    global _vad_erro
    try:
        import torch
        from silero_vad import get_speech_timestamps
        audio = _wav_to_mono16k(audio_path)          # 16 kHz mono, sr suportado
        # a inferência vai sob a MESMA trava do load: o modelo guarda estado
        with _vad_lock:
            speech = get_speech_timestamps(torch.from_numpy(audio), _vad_load(),
                                           sampling_rate=16000, threshold=0.6,
                                           speech_pad_ms=100)
        dur_fala = sum((t["end"] - t["start"]) for t in speech) / 16000
        _vad_erro = ""
        return dur_fala >= minimo_s
    except Exception as exc:  # noqa: BLE001 — Silero indisponível não bloqueia o STT
        # fail-open é decisão de projeto, mas deixá-lo MUDO escondia o guarda
        # virando no-op: o erro fica no estado e no log (#320)
        msg = f"{type(exc).__name__}: {exc}"
        if msg != _vad_erro:
            print(f"[vad] {msg[:160]} — fail-open, o trecho segue como fala", flush=True)
        _vad_erro = msg
        return True


def _vad_estado() -> tuple[str, str]:
    """(caminho carregado, último erro) — diagnóstico sem mexer no modelo."""
    return _vad_backend, _vad_erro


#: teto de duração do áudio do STT: acima disto o upload é recusado com 413 em vez
#: de decodificar 1 h de áudio em float32 (~230 MB) e rodar o Whisper sobre tudo.
#: 30 min: uma reunião/entrevista de 20 min passa com folga (~77 MB no pico), e o
#: que passa disso é recusado com explicação em vez de estourar a RAM.
TETO_STT_S = 1800.0
JANELA_STT_S = 30.0
SOBREPOSICAO_STT_S = 1.0
# Teto de tokens por janela, PROPORCIONAL à duração (#870). Sem ele o whisper
# alucinando sobre eco/ruído ("Tch-tch-tch-…") decodifica até o limite do modelo
# (224 tokens) num trecho de 2,3 s: medido 13 s de decisão de barge no mini com o
# TTS disputando a GPU; com EOT suprimido (o mesmo laço) 2,3 s de áudio custam
# 2 741 ms sem teto e 1 314 ms com 43 tokens. 12 tok/s cobre com folga fala
# rápida em pt (~4 tok/s) mais os tokens de timestamp; a janela cheia de 30 s
# cai no teto do próprio modelo e não muda.
STT_TOKENS_POR_S = 12
STT_TOKENS_MIN = 32
STT_TOKENS_MAX = 224


def _stt_teto_tokens(n_amostras: int, sr: int = 16000) -> int:
    """`sample_len` do whisper para uma janela de `n_amostras`."""
    dur = max(0.0, n_amostras / float(sr))
    return int(min(STT_TOKENS_MAX, max(STT_TOKENS_MIN, STT_TOKENS_MIN + dur * STT_TOKENS_POR_S)))


def _duracao_audio(path) -> float:
    """Duração em segundos pelo cabeçalho (barato, sem decodificar). -1 = não deu."""
    import soundfile as sf

    try:
        info = sf.info(str(path))
    except Exception:  # noqa: BLE001 — formato que o libsndfile não lê
        return -1.0
    return float(info.frames) / float(info.samplerate or 1)


def _transcreve_em_janelas(audio, lang, sr: int = 16000, desistiu=None):
    """Transcreve em janelas de `JANELA_STT_S` e junta texto + segmentos.

    `desistiu` é consultado ENTRE janelas (#803): a passada de comando abandonada
    (o passo estourou o prazo) tem de desistir no próximo ponto seguro em vez de
    rodar a inferência inteira segurando o modelo."""
    import mlx_whisper

    passo = int((JANELA_STT_S - SOBREPOSICAO_STT_S) * sr)
    janela = int(JANELA_STT_S * sr)
    textos, segmentos = [], []
    anterior = ""
    for ini in range(0, max(1, len(audio)), passo):
        if desistiu is not None and desistiu():
            break
        pedaco = audio[ini:ini + janela]
        if not len(pedaco):
            continue
        r = mlx_whisper.transcribe(
            pedaco, path_or_hf_repo=_whisper_repo(), language=lang,
            temperature=0.0, condition_on_previous_text=False,
            # Limiares do PRÓPRIO whisper fazem ele descartar o trecho inteiro: o
            # texto volta vazio e o motivo se perde (medido com "Sim."). Quem julga
            # é o _stt_ok, que devolve a razão — então aqui se pede ao whisper para
            # não deitar nada fora por conta própria.
            no_speech_threshold=0.99, logprob_threshold=-3.0,
            compression_ratio_threshold=9.9,
            sample_len=_stt_teto_tokens(len(pedaco), sr),
        )
        t = (r.get("text") or "").strip()
        if t:
            # a sobreposição de 1 s transcreve de novo a palavra da emenda: tira o
            # maior sufixo do anterior que casa com o prefixo deste (sem comer
            # palavra que só exista de um lado)
            if anterior:
                p_ant, p_nov = anterior.split(), t.split()
                for k in range(min(6, len(p_ant), len(p_nov)), 0, -1):
                    if [w.lower() for w in p_ant[-k:]] == [w.lower() for w in p_nov[:k]]:
                        t = " ".join(p_nov[k:]).strip()
                        break
            if t:
                textos.append(t)
                anterior = t
        for seg in (r.get("segments") or []):
            segmentos.append({**seg,
                              "start": float(seg.get("start", 0.0)) + ini / sr,
                              "end": float(seg.get("end", 0.0)) + ini / sr})
    return {"text": " ".join(textos).strip(), "language": lang or "",
            "segments": segmentos}


#: Whisper do Live quando `live_stt_whisper_repo` está vazio (#876). Fixo, não o
#: `WHISPER_REPO`: o env do dono pode apontar o de arquivo para o large-v3.
LIVE_WHISPER_PADRAO = "mlx-community/whisper-large-v3-turbo"
_stt_ctx = threading.local()


class _stt_do_live:
    """Marca a thread: o `_transcribe` que rodar dentro usa o Whisper do Live.

    Thread-local, não argumento: o `_transcribe` é dublado em dezenas de células
    com a assinatura antiga, e o Live chega nele pelo `_stt_app` (mesma thread)."""

    def __enter__(self):
        self._antes = getattr(_stt_ctx, "live", False)
        _stt_ctx.live = True

    def __exit__(self, *exc):
        _stt_ctx.live = self._antes


def _whisper_repo(live: bool | None = None) -> str:
    """Repo HF do Whisper local: setting (ex. large-v3 completo p/ máxima
    precisão) com fallback p/ o default do env.

    `live=True` (#876): turno e barge do Live usam `live_stt_whisper_repo`
    (vazio = turbo). O `mlx_whisper` guarda UM modelo; alternar Live e arquivo
    com repos diferentes recarrega — custo aceito: as duas cargas não se cruzam
    numa mesma sessão."""
    if live is None:
        live = bool(getattr(_stt_ctx, "live", False))
    if live:
        return (_settings.get("live_stt_whisper_repo") or "").strip() or LIVE_WHISPER_PADRAO
    return (_settings.get("stt_whisper_repo") or "").strip() or WHISPER_REPO


def _com_vad_aviso(res: dict, aviso: str) -> dict:
    """Carimba o motivo do fail-open do VAD, quando houve."""
    if aviso and isinstance(res, dict) and "vad_aviso" not in res:
        res = {**res, "vad_aviso": aviso[:160]}
    return res


def _transcribe(audio_path: Path, language: str | None = None, allow_remote: bool = True):
    caiu_remoto, erro_remoto = False, ""
    if allow_remote and _use_remote_stt():
        try:
            return _transcribe_remote(audio_path, language)
        except Exception as e:  # noqa: BLE001 — RTX fora do ar cai pro local
            print(f"[stt] remoto indisponível ({str(e)[:120]}) — usando local", flush=True)
            # o cliente precisa distinguir "transcrição vazia" de "o remoto morreu":
            # vai no resultado (as rotas traduzem para header/campo)
            caiu_remoto, erro_remoto = True, str(e)[:200]

    # Silero VAD: se o áudio não tem fala, o STT nem roda (mata alucinação)
    fala = _vad_tem_fala(audio_path)
    if not fala:
        return _com_aviso_remoto({"text": "", "language": "", "segments": []},
                                 caiu_remoto, erro_remoto)
    # o VAD falha ABERTO por desenho (não bloqueia o pipeline), mas o cliente tem
    # de saber que o guarda anti-alucinação ficou desligado neste trecho (#282)
    vad_aviso = _vad_erro

    if (_settings.get("stt_local_engine") or "whisper").lower() == "parakeet":
        return _com_vad_aviso(
            _com_aviso_remoto(_transcribe_parakeet(audio_path), caiu_remoto, erro_remoto),
            vad_aviso)

    # language fixo (ex.: "pt") melhora muito a precisão em trechos curtos: o
    # whisper deixa de adivinhar o idioma a cada frase. None = auto-detecta.
    lang = language if language and language not in ("auto",) else None
    audio = _wav_to_mono16k(audio_path)
    with _stt_lock:
        # janelas de 30 s (com 1 s de sobreposição): o Whisper roda nativamente em
        # janelas desse tamanho, e passar a gravação inteira de uma vez fazia a
        # memória e o tempo crescerem com o arquivo (#329)
        # Antes da inferência: um passo no meio da transcrição não pode ver o
        # relógio velho e soltar o modelo que esta chamada está usando.
        _whisper_arquivo_marca()
        r = _transcreve_em_janelas(audio, lang)
    del audio
    _touch_use("stt")
    _release_mlx_memory()
    return _com_vad_aviso(_com_aviso_remoto(r, caiu_remoto, erro_remoto), vad_aviso)


_pk = {"model": None, "repo": ""}


def _com_aviso_remoto(res, caiu: bool, erro: str = ""):
    """Marca o resultado do STT quando o remoto falhou e o local assumiu."""
    if caiu and isinstance(res, dict):
        return {**res, "remote_fallback": True, "remote_error": erro}
    return res


def _aviso_remoto_headers(alvo, r: dict) -> None:
    """Aviso no HEADER (o `/v1/audio/*` também devolve text/srt/vtt, então campo no
    corpo não cobriria tudo). `alvo` é `response.headers` ou o header de um Response."""
    if not r.get("remote_fallback"):
        return
    alvo["X-TTS-Remote-Fallback"] = "1"
    msg = str(r.get("remote_error") or "")[:150]
    if msg:
        alvo["X-TTS-Remote-Error"] = msg.encode("ascii", "replace").decode()


def _transcribe_parakeet(audio_path: Path) -> dict:
    """STT local com NVIDIA Parakeet TDT 0.6B v3 (multilíngue, ~30x tempo real):
    pontuação/acento nativos, timestamps por sentença. Sem probs de no-speech —
    os filtros de alucinação são VAD + tamanho do texto + blacklist (_stt_ok)."""
    from parakeet_mlx import from_pretrained

    with _stt_lock:
        if _pk["model"] is None or _pk["repo"] != PARAKEET_REPO:
            _pk["model"] = from_pretrained(PARAKEET_REPO)
            _pk["repo"] = PARAKEET_REPO
        r = _pk["model"].transcribe(audio_path)
    _touch_use("stt")
    _release_mlx_memory()
    return {"text": (r.text or "").strip(),
            "language": "",
            "segments": [{"start": float(s.start), "end": float(s.end),
                          "text": (s.text or "").strip()} for s in (r.sentences or [])]}


def _aplica_speaker_alvo(tmp: Path, alvo: str) -> tuple[Path, dict]:
    """Extrai a voz do perfil `alvo` do áudio antes do STT (VOZ-I6, #534).

    Devolve (wav a transcrever, campos para a resposta). SEM o parâmetro devolve
    `tmp` e `{}` — o contrato fica idêntico ao de antes. Com o parâmetro:

    - perfil inexistente/vazio → 400 (nome errado em silêncio transcreveria o áudio
      inteiro e o cliente acharia que filtrou);
    - separador fora → segue com o áudio ORIGINAL e diz `separacao: "indisponivel"`
      (o motor existe no R4, mas o peso é opcional: não pode derrubar o STT);
    - sucesso → `separacao: "ok"` e `segmentos_descartados` (o fluxo do concorrente).

    A separação roda em processo filho e em MPS, então o pai segura `_gen_lock`: é a
    mesma exclusividade que a geração local usa para o Metal.
    """
    alvo = (alvo or "").strip()
    if not alvo:
        return tmp, {}
    with _speaker_lock:
        perfis = _speaker_load()
    if alvo not in perfis:
        raise HTTPException(400, f"Perfil de voz '{alvo}' não existe "
                                 f"(veja GET /api/speaker/profiles)")
    perfil = perfis[alvo]
    if not (perfil.get("vecs") or []):
        # existe mas sem amostra é outro problema: mandar cadastrar, não "não existe"
        raise HTTPException(400, f"Perfil de voz '{alvo}' está sem amostras — "
                                 f"grave com POST /api/speaker/enroll")
    import separa_voz

    if not separa_voz.disponivel():
        return tmp, {"separacao": "indisponivel", "separacao_motivo": separa_voz.motivo()}
    try:
        with _gen_lock:                      # MPS: prazo em separa_voz (#924)
            # o extrator é o DO PERFIL, não o efetivo: perfil legado tem vetor
            # 256-d e o ECAPA devolve 192-d (#534)
            novo, info = separa_voz.separa_alvo(
                tmp, perfil["vecs"], _speaker_modelo_do_perfil(perfil))
    except Exception as exc:  # noqa: BLE001 — o STT não cai por causa do filtro
        print(f"[separa] falhou para '{alvo}': {exc}", flush=True)
        return tmp, {"separacao": "falhou", "separacao_motivo": str(exc)[:160]}
    campos = {"separacao": "ok"}
    if info.get("descartados"):
        campos["segmentos_descartados"] = info["descartados"]
    if info.get("escores"):
        campos["separacao_escores"] = info["escores"]
    return novo, campos


def _separacao_do_turno(tmp: Path) -> tuple[Path, dict]:
    """Separação no turno do LIVE, atrás do turno já ABERTO (#533, item 2).

    Só com `live_so_minha_voz.separacao` ligado. Devolve `(wav a transcrever,
    campos para o `stats`)`; `tmp` volta quando não há separação. NUNCA levanta:
    separador fora, perfil sumido ou worker morto seguem com o áudio ORIGINAL e
    dizem o motivo — é a mesma queda do `/api/transcribe`, mas aqui sem HTTP.

    `separa_alvo` já veta as janelas que não são do alvo (viram silêncio), então
    o transcript sai sem a fala de terceiros."""
    cfg = _live_voz_ligado()
    if not cfg or not cfg.get("separacao"):
        return tmp, {}
    alvo = _live_voz_perfil(cfg)
    if alvo is None:
        return tmp, {"separacao": "indisponivel", "separacao_motivo": "perfil ausente"}
    _nome, perfil = alvo
    vecs = perfil.get("vecs") or []
    if not vecs:
        return tmp, {"separacao": "indisponivel", "separacao_motivo": "perfil sem amostras"}
    import separa_voz

    if not separa_voz.disponivel():
        return tmp, {"separacao": "indisponivel", "separacao_motivo": separa_voz.motivo()}
    t0 = time.monotonic()
    try:
        with _gen_lock:                  # MPS: prazo em separa_voz (#924)
            # extrator DO PERFIL (perfil legado tem 256-d; o ECAPA devolve 192-d)
            novo, info = separa_voz.separa_alvo(tmp, vecs, _speaker_modelo_do_perfil(perfil))
    except Exception as exc:             # noqa: BLE001 — o STT não cai pelo filtro
        print(f"[separa] turno falhou: {exc}", flush=True)
        return tmp, {"separacao": "falhou", "separacao_motivo": str(exc)[:160],
                     "separacao_ms": int((time.monotonic() - t0) * 1000)}
    campos = {"separacao": "ok", "separacao_ms": int((time.monotonic() - t0) * 1000)}
    if info.get("descartados"):
        campos["segmentos_descartados"] = info["descartados"]
    if info.get("escores"):
        campos["separacao_escores"] = info["escores"]
    return novo, campos


def _separa_prewarm() -> None:
    """Aquece o separador quando o modo está ligado (chamado no prewarm da sessão).

    Sem isto o PRIMEIRO turno com separação paga a carga do modelo e o
    aquecimento dentro do `first_audio_ms` (medido: 0,90 s + 0,65 s), estourando o
    piso de 1,2 s. Roda em thread, atrás do prewarm do pipeline, e falha em
    silêncio: prewarm é adiantamento, nunca pré-requisito."""
    cfg = _live_voz_ligado()
    if not cfg or not cfg.get("separacao"):
        return
    import separa_voz

    if not separa_voz.disponivel():
        return

    def _aquece():
        try:
            import numpy as np
            _fd, nome = tempfile.mkstemp(prefix="tts-separa-pre-", suffix=".wav")
            os.close(_fd)
            tmp = Path(nome)
            with wave.open(str(tmp), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                t = np.arange(8000) / 16000.0
                w.writeframes((np.sin(2 * np.pi * 180 * t) * 8000).astype("<i2").tobytes())
            with _gen_lock:              # MPS: prazo em separa_voz (#924)
                separa_voz.separa(tmp, tmp.parent)
            tmp.unlink(missing_ok=True)
        except Exception as exc:         # noqa: BLE001 — prewarm não derruba sessão
            print(f"[separa] prewarm falhou: {exc}", flush=True)

    threading.Thread(target=_aquece, name="separa-prewarm", daemon=True).start()


def _stt_ok(r: dict, text: str, forcar: bool = False):
    """Aceita só transcrição que pareça fala real (rejeita ruído/alucinação).

    `stt_anti_ruido=false` desliga tudo: o que continua valendo é o gate de
    silêncio (VAD) e o tamanho do áudio — para quem quer ver também o que o
    Whisper duvidou.

    `forcar` ignora o setting. O LIVE usa assim (#314, decisão do PM): lá não há
    turno de digitação para corrigir, então alucinação vira RESPOSTA — e o dono
    desligou o filtro para o ditado da Conversa, não para o Live."""
    if not forcar and not _settings.get("stt_anti_ruido", True):
        return True, ""
    t = text.strip()
    palavras = re.findall(r"[^\W\d_]+", t, flags=re.UNICODE)  # palavras (sem números/símbolos)
    if not t:
        # honesto: fala curta que o whisper descartou chegava aqui como vazio e era
        # reportada como "curto demais", que sugere filtro de tamanho
        return False, "não ouviu fala"
    if len(t) < _settings["stt_min_chars"]:
        return False, "curto demais"
    if len(palavras) < _settings["stt_min_words"]:
        return False, "sem palavras"
    if t.lower().strip(" .!?…\"'") in _STT_BLACKLIST:
        return False, "alucinação comum"
    segs = r.get("segments") or []
    if segs:
        nsp = max((s.get("no_speech_prob", 0.0) for s in segs), default=0.0)
        alp = min((s.get("avg_logprob", 0.0) for s in segs), default=0.0)
        cr = max((s.get("compression_ratio", 0.0) for s in segs), default=0.0)
        # no_speech_prob sozinho deitava fala curta e boa fora (medido: "Sim."
        # voltava sem texto). Só rejeita se a confiança também estiver fraca.
        if nsp > _settings["stt_max_no_speech"] and alp < _settings["stt_min_logprob"] + 0.5:
            return False, f"sem fala ({nsp:.2f}, confiança {alp:.2f})"
        if alp < _settings["stt_min_logprob"]:
            return False, f"baixa confiança ({alp:.2f})"
        if cr > _settings["stt_max_compression"]:
            return False, f"repetitivo ({cr:.2f})"
    return True, ""


_NOTE_RE = re.compile(
    r"^[\(\[\*]*\s*(note|nota|obs\b|observa|alternativ|"
    r"a (more|better) (natural|idiomatic|colloquial|common|literal)\b|"
    r"uma (forma|maneira|vers[aã]o) mais (natural|comum|idiom|coloquial))", re.I)


def _clean_translation(s: str) -> str:
    """Tira notas/alternativas/preâmbulos que o LLM às vezes anexa (senão o TTS fala isso)."""
    s = (s or "").strip().strip('"').strip()
    s = s.split("\n\n", 1)[0].strip()          # corta bloco extra após linha em branco (nota/alternativa)
    linhas = []
    for ln in s.split("\n"):
        if _NOTE_RE.match(ln.strip()):          # linha de nota inline -> para aqui
            break
        linhas.append(ln)
    return "\n".join(linhas).strip().strip('"').strip()


def _translate(text: str, target: str, emotion: str | None = None) -> str:
    if _use_remote_translate():
        return _clean_translation(_translate_remote(text, target, emotion))

    from mlx_lm import generate, load

    repo = _mt_repo()
    with _mt_lock:
        if _mt["model"] is None or _mt.get("repo") != repo:   # troca de modelo -> recarrega
            _mt["model"], _mt["tok"] = load(repo)
            _mt["repo"] = repo
        model, tok = _mt["model"], _mt["tok"]
        msgs = [{"role": "user", "content": _translate_prompt(text, target, emotion)}]
        prompt = tok.apply_chat_template(msgs, add_generation_prompt=True)
        out = generate(model, tok, prompt=prompt, max_tokens=512, verbose=False)
    _touch_use("mt")
    _release_mlx_memory()
    return _clean_translation(out)


# --- Captura de emoção -> alavancas que o OmniVoice TEM, sem quebrar o clone:
#     pitch (tag VÁLIDA do instruct) + velocidade (time-stretch, preserva o timbre).
#     Mapa: label -> (rótulo p/ exibir, tag de pitch, fator de velocidade).
_ser = {"clf": None}
_ser_lock = threading.Lock()
SER_REPO = os.environ.get("TTS_ROD_SER", "superb/wav2vec2-base-superb-er")
_SER_EMO = {
    "hap": ("alegre", "high pitch", 1.12), "happy": ("alegre", "high pitch", 1.12),
    "ang": ("raiva", "high pitch", 1.08), "angry": ("raiva", "high pitch", 1.08),
    "sad": ("triste", "low pitch", 0.88), "sadness": ("triste", "low pitch", 0.88),
    "neu": ("neutro", "", 1.0), "neutral": ("neutro", "", 1.0), "calm": ("calmo", "low pitch", 0.95),
    "fear": ("medo", "high pitch", 1.08), "fearful": ("medo", "high pitch", 1.08),
    "disgust": ("desgosto", "low pitch", 0.95), "surprise": ("surpresa", "very high pitch", 1.12),
}


def _prosody(path: Path) -> dict:
    """Pistas acústicas baratas: volume, dinâmica e duração."""
    import numpy as np
    import soundfile as sf

    a, sr = sf.read(str(path), dtype="float32")
    if a.ndim > 1:
        a = a.mean(axis=1)
    dur = max(0.1, len(a) / sr)
    w = max(1, int(0.025 * sr))
    en = np.array([float(np.sqrt(np.mean(a[i:i + w] ** 2)))
                   for i in range(0, max(1, len(a) - w), w)]) if len(a) > w else np.array([0.0])
    return {"rms": float(np.sqrt(np.mean(a ** 2))), "dyn": float(np.std(en)), "dur": dur}


def _emotion_light(path: Path, text: str):
    """Prosódia (energia + ritmo) -> (rótulo, pitch, velocidade). Determinística."""
    p = _prosody(path)
    rate = len(text) / p["dur"]
    forte, fraco, expr = p["rms"] > 0.16, p["rms"] < 0.06, p["dyn"] > 0.05
    rapido, lento = rate > 16, rate < 9
    pitch = "high pitch" if (forte and expr) else ("low pitch" if fraco else "")
    speed = 1.10 if rapido else (0.90 if lento else 1.0)
    if forte and expr:
        lbl = "animado"
    elif forte:
        lbl = "intenso"
    elif fraco:
        lbl = "suave"
    elif rapido:
        lbl = "ágil"
    elif lento:
        lbl = "calmo"
    else:
        lbl = "neutro"
    return (lbl, pitch, speed)


def _emotion_accurate(path: Path):
    """Modelo SER (wav2vec2) -> categoria -> (rótulo, pitch, velocidade), com gate."""
    audio = _wav_to_mono16k(path)  # array 16 kHz -> sem ffmpeg_read do transformers
    with _ser_lock:
        if _ser["clf"] is None:
            from transformers import pipeline
            _ser["clf"] = pipeline("audio-classification", model=SER_REPO)
        res = _ser["clf"]({"raw": audio, "sampling_rate": 16000}, top_k=None)
    del audio
    _touch_use("ser")
    if not res:
        return ("neutro", "", 1.0)
    top = max(res, key=lambda x: x.get("score", 0.0))
    lab = str(top.get("label", "")).lower()
    # baixa confiança ou neutro -> não força emoção (evita falso "alegre/raiva")
    if top.get("score", 0.0) < 0.5 or lab in ("neu", "neutral"):
        return ("neutro", "", 1.0)
    return _SER_EMO.get(lab, ("neutro", "", 1.0))


def _emotion_instruct(path: Path, text: str, mode: str):
    """Retorna (rótulo, pitch_tag, fator_velocidade, erro). mode: off|light|accurate."""
    try:
        if mode == "light":
            lbl, pitch, spd = _emotion_light(path, text)
            return (lbl, pitch, spd, None)
        if mode == "accurate":
            lbl, pitch, spd = _emotion_accurate(path)
            return (lbl, pitch, spd, None)
    except Exception as exc:  # noqa: BLE001
        return ("", "", 1.0, str(exc))
    return ("", "", 1.0, None)


@app.post("/api/translate/warmup")
def translate_warmup():
    """Carrega whisper + LLM de tradução em background — chamado quando o usuário
    abre/começa o tradutor, p/ os modelos ficarem quentes antes da 1ª frase."""
    # não aquece o que vai pro remoto quando "liberar local" está ligado
    skip_stt = _settings.get("free_local_on_remote") and _use_remote_stt()
    skip_mt = _settings.get("free_local_on_remote") and _use_remote_translate()

    def _warm():
        if not skip_stt:
            try:
                import numpy as np
                import mlx_whisper
                with _stt_lock:
                    mlx_whisper.transcribe(np.zeros(16000, dtype=np.float32),
                                           path_or_hf_repo=_whisper_repo(), language="pt")
                _touch_use("stt")
                _release_mlx_memory()
            except Exception:  # noqa: BLE001
                pass
        if not skip_mt:
            try:
                from mlx_lm import load
                repo = _mt_repo()
                with _mt_lock:
                    if _mt["model"] is None or _mt.get("repo") != repo:
                        _mt["model"], _mt["tok"] = load(repo)
                        _mt["repo"] = repo
                _touch_use("mt")
            except Exception:  # noqa: BLE001
                pass
    threading.Thread(target=_warm, daemon=True).start()
    return {"ok": True}


@app.post("/api/models/unload")
def unload_models(request: Request, payload: dict = None):
    """Descarrega modelos LOCAIS (MLX) p/ liberar RAM. Sem corpo = todos os locais.
    {"only_remote": true} = só os que têm remoto ativo.

    Com job in-process em voo o `_gen_lock` está tomado e o POST ficava PENDURADO
    até o fim da geração (#282): agora espera `_GEN_ESPERA_UNLOAD_S` e, se não der,
    devolve 409 explicando — sem descarregar o modelo por baixo da geração."""
    _exige_admin(request)
    from fastapi.responses import JSONResponse
    p = payload or {}
    if p.get("only_remote"):
        r = _unload_local_models(tts=_use_remote_tts(), stt=_use_remote_stt(),
                                 mt=_use_remote_translate(),
                                 espera_gen_s=_GEN_ESPERA_UNLOAD_S)
    else:
        r = _unload_local_models(espera_gen_s=_GEN_ESPERA_UNLOAD_S)
    if r.get("tts_pendente"):
        return JSONResponse(
            {"detail": "Geração local em curso segurando o modelo; nada foi "
                       "descarregado do TTS",
             "hint": "Espere o job terminar (ou use ociosidade) e tente de novo.",
             **r}, status_code=409)
    return r


def _save_audio_upload(upload: UploadFile, prefix: str = ".stt") -> Path:
    """Grava um UploadFile de áudio e devolve um WAV 16k mono limpo pro STT:
    decodifica qualquer formato via ffmpeg + denoise espectral (afftdn) +
    trim de silêncio nas pontas — o Whisper alucina em ruído puro.

    Recusa com **413** acima de `TETO_STT_S` (1800 s = 30 min; ~77 MB de float32 no
    pico) e com 400 em formato que o ffmpeg não lê — o teto é de DURAÇÃO, não só de
    bytes: 1 h de áudio decodificada passa de 230 MB."""
    ctype = (upload.content_type or "").split(";")[0].strip().lower()
    por_ctype = {"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mpeg": ".mp3",
                 "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/aac": ".aac",
                 "audio/flac": ".flac", "audio/wav": ".wav", "audio/x-wav": ".wav",
                 "audio/wave": ".wav"}
    nome = (upload.filename or "").lower()
    suffix = por_ctype.get(ctype)
    if not suffix:
        for ext, sfx in (".webm", ".webm"), (".m4a", ".m4a"), (".mp4", ".m4a"), \
                         (".mp3", ".mp3"), (".ogg", ".ogg"), (".flac", ".flac"), (".wav", ".wav"):
            if nome.endswith(ext):
                suffix = sfx
                break
        else:
            suffix = ".wav"
    # cap de tamanho: leitura limitada (sem confiar em Content-Length) —
    # evita encher disco/memória com upload gigante de quem tem a chave
    raw = OUTPUTS_DIR / f"{prefix}-{uuid.uuid4().hex[:10]}{suffix}"
    _write_upload_limited(upload, raw)
    wav = OUTPUTS_DIR / f"{prefix}-{uuid.uuid4().hex[:10]}.wav"
    try:
        # trim de silêncio nas pontas + 16k mono: o Whisper alucina em ruído puro —
        # sem isso o "nada" vira "E aí". O denoise espectral (afftdn) é opção: em
        # áudio limpo ele é neutro para a acurácia (medido por WER) e em áudio ruim
        # ajuda, então fica a critério de quem está capturando.
        filtro = "highpass=f=80"
        if _settings.get("stt_denoise", True):
            filtro += ",afftdn=nr=12"
        filtro += (",silenceremove=start_periods=1:start_threshold=-45dB,"
                   "areverse,silenceremove=start_periods=1:start_threshold=-45dB,areverse")
        p = subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(raw),
                            "-af", filtro, "-ar", "16000", "-ac", "1", str(wav)],
                           capture_output=True, timeout=120, text=True)
        stderr = (p.stderr or "").strip()
        if p.returncode != 0 or not wav.exists() or wav.stat().st_size == 0:
            print(f"[stt] ffmpeg ({ctype}, {raw.stat().st_size if raw.exists() else 0}B): {stderr[:160]}", flush=True)
            raise RuntimeError(stderr[:160] or "ffmpeg não produziu saída")
    except HTTPException:
        raise
    except Exception as e:
        raw.unlink(missing_ok=True)
        wav.unlink(missing_ok=True)
        raise HTTPException(400, f"Formato de áudio não suportado ({ctype or 'desconhecido'}): {e}") from e
    raw.unlink(missing_ok=True)
    # teto de DURAÇÃO (não só de bytes): 1 h decodificada em float32 passa de
    # 230 MB e o Whisper roda sobre tudo — recusa com explicação (#329)
    dur = _duracao_audio(wav)
    if dur > TETO_STT_S:
        wav.unlink(missing_ok=True)
        raise HTTPException(
            413, f"Áudio longo demais ({dur:.0f}s). O STT aceita até "
                 f"{TETO_STT_S:.0f}s por vez — corte o trecho e envie de novo.")
    return wav


def _read_upload_limited(upload: UploadFile, default_mb: int = 64) -> bytes:
    """Lê uploads em memória com teto comum para voz, edição e STT."""
    try:
        limite_mb = int(os.environ.get("TTS_MAX_UPLOAD_MB", str(default_mb)))
    except (TypeError, ValueError):
        limite_mb = default_mb
    limite_mb = max(0, min(limite_mb, 2048))
    limite = limite_mb * 1024 * 1024
    dados = upload.file.read(limite + 1)
    if len(dados) > limite:
        raise HTTPException(413, f"Áudio maior que {limite_mb} MB")
    return dados


def _upload_limit_bytes(default_mb: int = 64) -> tuple[int, int]:
    try:
        limite_mb = int(os.environ.get("TTS_MAX_UPLOAD_MB", str(default_mb)))
    except (TypeError, ValueError):
        limite_mb = default_mb
    limite_mb = max(0, min(limite_mb, 2048))
    return limite_mb, limite_mb * 1024 * 1024


def _write_upload_limited(upload: UploadFile, path: Path, default_mb: int = 64) -> None:
    """Copia multipart em blocos, sem materializar o áudio completo na RAM."""
    limite_mb, limite = _upload_limit_bytes(default_mb)
    total = 0
    try:
        with Path(path).open("wb") as out:
            while True:
                bloco = upload.file.read(min(1024 * 1024, limite - total + 1))
                if not bloco:
                    break
                total += len(bloco)
                if total > limite:
                    raise HTTPException(413, f"Áudio maior que {limite_mb} MB")
                out.write(bloco)
    except Exception:
        Path(path).unlink(missing_ok=True)
        raise
    if total == 0:
        Path(path).unlink(missing_ok=True)
        raise HTTPException(400, "Áudio vazio")


@app.post("/api/stt-partial")
def stt_partial(audio: UploadFile = None, source_lang: str = Form("auto")):
    """Transcrição parcial e rápida (sem tradução/TTS) — usada para mostrar as
    palavras na tela enquanto o usuário ainda fala. Sem filtro anti-ruído: é só
    prévia ao vivo, a versão final vem do /api/translate-speech.

    Mesmo teto do STT normal: 30 min (`TETO_STT_S`), acima disso 413."""
    if audio is None:
        raise HTTPException(400, "Áudio obrigatório")
    tmp = _save_audio_upload(audio, ".pstt")
    try:
        r = _transcribe(tmp, language=(source_lang or "auto").lower())
    except Exception:  # noqa: BLE001 — prévia: nunca derruba a UI
        return {"text": ""}
    finally:
        tmp.unlink(missing_ok=True)
    return {"text": (r.get("text") or "").strip(),
            "language": (r.get("language") or "").strip().lower()}


def _parse_time(v) -> "float | None":
    """Aceita segundos (12.5), MM:SS (1:23) ou HH:MM:SS (1:02:03). None se vazio."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        if ":" in s:
            sec = 0.0
            for part in s.split(":"):
                sec = sec * 60 + float(part)
            return sec
        return float(s)
    except (ValueError, TypeError):
        return None


# Allowlist do /api/youtube-audio. `host.endswith("youtube.com")` puro aceitava
# "evil-youtube.com" e "youtube.com.evil.com" (domínios registráveis por
# terceiros, com outro conteúdo) — exige-se o PONTO antes do domínio.
YT_ALLOWED_ZONE_HOSTS = ("youtube.com", "youtube-nocookie.com")
YT_ALLOWED_EXACT_HOSTS = ("youtu.be",)


def _yt_host_allowed(host: str) -> bool:
    """Só o domínio exato ou um subdomínio DELE (ponto obrigatório antes)."""
    h = (host or "").strip().lower().rstrip(".")      # "youtube.com." é o mesmo host
    if not h:
        return False
    return h in YT_ALLOWED_EXACT_HOSTS or h in YT_ALLOWED_ZONE_HOSTS or any(
        h.endswith("." + d) for d in YT_ALLOWED_ZONE_HOSTS)


def _url_parse(url: str):
    """`urlparse` que não estoura: IPv6 malformado ("http://[::1") levanta
    ValueError no PRÓPRIO urlparse() (e em `.hostname`). None = não parseou."""
    try:
        return urlparse(url or "")
    except ValueError:
        return None


def _yt_url_host(url: str) -> str:
    try:
        return (_url_parse(url).hostname or "").lower()
    except (ValueError, AttributeError):
        return ""


def _yt_final_host(info: object) -> str:
    """Host que o yt-dlp REALMENTE abriu ("" = não deu para saber).

    Um link de redirecionamento (youtube.com/redirect?q=…) faz o yt-dlp cair no
    extractor genérico e baixar de outro site; o host da página final é o único
    lugar onde isso aparece — a URL do STREAM (googlevideo.com) nunca bate com a
    allowlist e não serve para esta checagem."""
    if not isinstance(info, dict):
        return ""
    for key in ("webpage_url", "original_url"):
        host = _yt_url_host(info.get(key) or "")
        if host:
            return host
    entries = info.get("entries")
    if entries:
        for entry in entries:
            if isinstance(entry, dict):
                return _yt_url_host(entry.get("webpage_url") or "")
    return ""


def _yt_retryable(exc: BaseException) -> bool:
    """403/SABR: o client InnerTube escolhido devolveu URL que o CDN recusa.
    Outros erros (vídeo privado, indisponível) não valem retry."""
    msg = str(exc).lower()
    return any(s in msg for s in ("403", "forbidden", "unable to download video data",
                                  "sign in to confirm"))


def _youtube_audio(url: str, start_s: float, end_s: float) -> bytes:
    """Baixa o áudio do YouTube (yt-dlp) e recorta [start,end] em WAV 24k mono com
    o ffmpeg estático (imageio-ffmpeg) — não depende de ffmpeg do sistema."""
    import glob
    import subprocess
    import tempfile

    import imageio_ffmpeg
    import yt_dlp

    ff = imageio_ffmpeg.get_ffmpeg_exe()
    d = tempfile.mkdtemp(prefix="yt-")
    try:
        outtmpl = os.path.join(d, "src.%(ext)s")
        # YouTube SABR (2026-08): android_vr/web_safari devolvem URL sem stream
        # direto e o CDN responde 403. yt-dlp >= 2026.08.19 troca o default
        # (visionos); se ainda 403, cai para android/mweb/ios.
        # ejs:github resolve os desafios JS (n/sig) — sem isso faltam formatos.
        client_attempts = (None, ["android"], ["mweb"], ["ios"])
        last_err = None
        srcs = []
        for clients in client_attempts:
            for leftover in glob.glob(os.path.join(d, "src.*")):
                try:
                    os.remove(leftover)
                except OSError:
                    pass
            opts = {
                "format": "bestaudio/best",
                "outtmpl": outtmpl,
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "socket_timeout": 30,
                "max_filesize": 300 * 1024 * 1024,
                "retries": 3,
                "remote_components": ["ejs:github"],
            }
            if clients:
                opts["extractor_args"] = {"youtube": {"player_client": clients}}
            # vídeos que exigem login ("sign in to confirm"): cookies exportados
            # do navegador (formato Netscape) via TTS_ROD_YT_COOKIES=/caminho.txt
            cookies = (os.environ.get("TTS_ROD_YT_COOKIES") or "").strip()
            if cookies:
                ck = Path(cookies).expanduser()
                if ck.exists():
                    opts["cookiefile"] = str(ck)
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if not _yt_retryable(exc):
                    raise
                continue
            # Redirecionou para fora da allowlist (ex.: youtube.com/redirect?q=…):
            # não vale insistir nos outros clients, já baixou de outro host.
            fora = _yt_final_host(info)
            if fora and not _yt_host_allowed(fora):
                raise RuntimeError(
                    f"o link saiu do YouTube ({fora}) — use o endereço direto do vídeo "
                    "(youtube.com/… ou youtu.be/…)"
                )
            srcs = glob.glob(os.path.join(d, "src.*"))
            if srcs:
                break
            last_err = RuntimeError("nada baixado (vídeo indisponível ou maior que o limite)")
        else:
            raise last_err or RuntimeError("nada baixado (vídeo indisponível ou maior que o limite)")
        out = os.path.join(d, "out.wav")
        cmd = [ff, "-y", "-hide_banner", "-loglevel", "error",
               "-ss", f"{start_s}", "-t", f"{end_s - start_s}", "-i", srcs[0],
               "-ar", "24000", "-ac", "1", "-f", "wav", out]
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
        if p.returncode != 0 or not os.path.exists(out):
            raise RuntimeError(f"ffmpeg: {p.stderr.decode()[:200]}")
        if os.path.getsize(out) < 2000:   # ~vazio: trecho fora da duração do vídeo
            raise RuntimeError("trecho vazio — o tempo de fim passou da duração do vídeo?")
        with open(out, "rb") as fh:
            return fh.read()
    finally:
        shutil.rmtree(d, ignore_errors=True)


@app.post("/api/youtube-audio")
def youtube_audio(payload: dict):
    """Extrai um trecho de áudio de um link do YouTube -> WAV 24k mono. Usado como
    fonte de voz (treino) e de transcrição."""
    url = (payload.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "Informe o link do YouTube")
    u = _url_parse(url)
    # `urlparse` LEVANTA em IPv6 malformado ("http://[::1") — antes virava 500 em vez
    # de 400; `u.hostname` também, por isso o host sai do helper (que trata).
    host = _yt_url_host(url)
    if u is None or u.scheme not in ("http", "https") or not _yt_host_allowed(host):
        raise HTTPException(400, "Use um link do YouTube (youtube.com ou youtu.be)")
    # youtube.com/redirect?q=<url>: o yt-dlp segue o q e baixa de onde ele aponta. O
    # host final nem sempre aparece no info do yt-dlp (a checagem pós-download
    # passava batido), então o ALVO é validado aqui: 400 imediato, sem baixar para
    # depois descartar. O bloco pós-download segue como rede de segurança (cadeia).
    if u.path.rstrip("/") == "/redirect":
        alvo = (parse_qs(u.query).get("q") or [""])[0].strip()
        if alvo.startswith("//"):
            alvo = f"{u.scheme}:{alvo}"
        elif alvo.startswith("/"):
            alvo = f"{u.scheme}://{host}{alvo}"
        alvo_host = _yt_url_host(alvo) if alvo else ""
        if alvo_host and not _yt_host_allowed(alvo_host):
            raise HTTPException(
                400,
                f"o link saiu do YouTube ({alvo_host}) — use o endereço direto do vídeo "
                "(youtube.com/… ou youtu.be/…)",
            )
    start = max(0.0, _parse_time(payload.get("start")) or 0.0)
    end = _parse_time(payload.get("end"))
    if end is None or end <= start:
        raise HTTPException(400, "Informe início e fim (fim maior que início)")
    if end - start > 600:
        raise HTTPException(400, "Trecho longo demais (máx. 10 min)")
    try:
        data = _youtube_audio(url, start, end)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).strip()[:240]
        if _yt_retryable(exc):
            raise HTTPException(
                400,
                "YouTube recusou o download (403). Atualize o yt-dlp "
                "(pip install -U 'yt-dlp>=2026.08.19') e tente de novo.",
            ) from exc
        raise HTTPException(400, f"Falha ao extrair do YouTube: {msg}") from exc
    return Response(content=data, media_type="audio/wav")


# ---------------------------------------------------------------------------
# Verificação de locutor (biometria de voz, opcional): cadastra perfis com
# embeddings (Resemblyzer/GE2E, CPU) e usa no gate do STT — só vozes
# autorizadas são transcritas, ou etiqueta quem falou.

SPEAKER_MAX_SAMPLES = 5      # embeddings por perfil (média robuza o timbre)
#: versão do EXTRATOR. Embedding de um modelo não se compara com o de outro, então
#: o perfil carrega a dele (`modelo`) e o gate pontua cada perfil pelo extrator que
#: o gerou. Perfil sem o campo é do extrator legado — segue valendo, marcado como
#: "precisa recadastrar"; o `enforce` nunca tranca o dono numa atualização (#525).
SPEAKER_MODELO_LEGADO = "resemblyzer:v1"
#: extrator DESEJADO (vencedor do R2, #521). Sem os pesos instalados (#526) o
#: app não baixa nada em runtime: `_speaker_modelo_efetivo()` cai no legado.
SPEAKER_MODELO_ATUAL = "ecapa:v1"
#: diretório do extrator (layout do SpeechBrain: `hyperparams.yaml` +
#: `embedding_model.ckpt`). `TTS_SPEAKER_MODELO_DIR` é o contrato do
#: `models/README.md` para instalação fora do repo — antes o código apontava para
#: um `.pt` que nunca existiu e o extrator vencedor não ligava em lugar nenhum (#599).
_SPEAKER_MODELO_DIR = _caminho_estado_env("TTS_SPEAKER_MODELO_DIR",
                                          BASE / "models" / "ecapa-voxceleb")
#: `label_encoder.ckpt` entra na lista (#598): sem ele o `Pretrainer` busca o
#: arquivo no Hub e o load falha offline — a disponibilidade dizia True e o
#: embedding voltava None, calado.
_SPEAKER_ARQ_OBRIG = ("hyperparams.yaml", "embedding_model.ckpt",
                      "mean_var_norm_emb.ckpt", "label_encoder.ckpt")
_speaker_lock = threading.Lock()
_speaker_enc_lock = threading.Lock()   # só a criação do encoder
_speaker_encoder = None      # VoiceEncoder (legado) carregado sob demanda
_speaker_ecapa_enc = None    # EncoderClassifier (ECAPA) carregado sob demanda
_speaker_fallback_avisado = False   # o aviso de fallback sai UMA vez por processo
#: TTL de ociosidade do ECAPA (env): devolve os ~460 MB medidos no #526. Mesma
#: política do classificador do #507 — carga sob demanda e unload por TTL (#612).
_SPEAKER_TTL_S = float(os.environ.get("TTS_SPEAKER_TTL_S", "600"))
_SPEAKER_VIGIA_S = 5.0       # cadência do vigia (custo: um `sleep` por vez)
_speaker_ultimo_uso = 0.0
_speaker_vigia: threading.Thread | None = None


def _speaker_ecapa_motivo() -> str:
    """Por que o ECAPA está fora ("" = está disponível).

    A separação do motivo existe para o aviso ser ACIONÁVEL (#606): "pesos
    ausentes" manda rodar o script, "lib ausente" manda olhar o venv. Antes o
    fallback era silencioso e a instalação ficava no legado para sempre sem que
    ninguém soubesse por quê."""
    import importlib.util

    try:
        if importlib.util.find_spec("speechbrain") is None:
            return "speechbrain não instalado no venv"
    except (ImportError, ValueError):     # pacote quebrado no venv
        return "pacote speechbrain quebrado no venv"
    faltam = [a for a in _SPEAKER_ARQ_OBRIG if not (_SPEAKER_MODELO_DIR / a).is_file()]
    if faltam:
        return (f"pesos ausentes em {_SPEAKER_MODELO_DIR} (falta {', '.join(faltam)}) "
                f"— rode ./tools/baixa_modelos.sh")
    return ""


def _speaker_ecapa_ok() -> bool:
    """Dá para usar o ECAPA agora? (lib instalada E pesos presentes)

    Sem download em runtime (decisão do PM no #525): quem traz os pesos é o #526.
    Ausente = False e o app segue no Resemblyzer."""
    return not _speaker_ecapa_motivo()


def _speaker_modelo_efetivo() -> str:
    """Extrator que ESTA instalação usa de fato (o desejado, se disponível).

    Não carrega nada: só confere lib/pesos. A carga fica no primeiro embedding
    (decisão do PM no #525: nada de modelo no boot). A disponibilidade passa por
    `_speaker_ecapa_ok()` porque é ela que o resto do código injeta/dubla nos
    testes; o motivo só é apurado no caminho de queda."""
    if _speaker_ecapa_ok():
        return SPEAKER_MODELO_ATUAL
    global _speaker_fallback_avisado
    if not _speaker_fallback_avisado:
        _speaker_fallback_avisado = True
        print(f"[speaker] ECAPA indisponível — usando o extrator legado "
              f"(Resemblyzer): {_speaker_ecapa_motivo()}", file=sys.stderr, flush=True)
    return SPEAKER_MODELO_LEGADO


def _speaker_modelo_do_perfil(perfil: dict) -> str:
    """Versão do extrator que gerou o perfil; ausente = legado (Resemblyzer)."""
    return str((perfil or {}).get("modelo") or SPEAKER_MODELO_LEGADO)


def _speaker_embed(wav_path: Path, modelo: str | None = None) -> list[float] | None:
    """Embedding do wav 16k mono no extrator `modelo` (default: o atual).

    None = lib ausente/áudio curto demais. O `modelo` existe para o gate ainda
    pontuar perfil antigo depois de uma troca de extrator (#525): cada perfil é
    pontuado pelo extrator que o gerou, e os dois NÃO se misturam."""
    alvo = modelo or _speaker_modelo_efetivo()
    if alvo == SPEAKER_MODELO_ATUAL and _speaker_ecapa_ok():
        return _speaker_embed_ecapa(wav_path)
    try:
        from resemblyzer import VoiceEncoder, preprocess_wav
        global _speaker_encoder
        if _speaker_encoder is None:
            # dois enroll/check simultâneos carregavam dois encoders (RAM em dobro)
            with _speaker_enc_lock:
                if _speaker_encoder is None:
                    _speaker_encoder = VoiceEncoder()
        f32 = preprocess_wav(wav_path)  # carrega 16k mono + trim VAD próprio
        if len(f32) < 16000 * 0.6:      # < 0.6s não gera embedding confiável
            return None
        return [float(x) for x in _speaker_encoder.embed_utterance(f32)]
    except Exception as e:  # noqa: BLE001 — sem o gate não derruba o STT
        print(f"[speaker] embedding falhou: {e}", flush=True)
        return None


def _speaker_encoder_ecapa():
    """EncoderClassifier do ECAPA (192-d), criado UMA vez, em CPU.

    `source`/`savedir` no diretório LOCAL: com os pesos materializados o load não
    depende de rede (`pretrained_path` no `hyperparams.yaml` é id de HF e só é
    consultado se faltar arquivo). Mesmo lock do outro encoder — dois carregamentos
    simultâneos dobravam a RAM. Marca o uso e sobe o vigia de ociosidade (#612)."""
    global _speaker_ecapa_enc, _speaker_ultimo_uso
    if _speaker_ecapa_enc is None:
        with _speaker_enc_lock:
            if _speaker_ecapa_enc is None:
                from speechbrain.inference.speaker import EncoderClassifier

                _speaker_ecapa_enc = EncoderClassifier.from_hparams(
                    source=str(_SPEAKER_MODELO_DIR),
                    savedir=str(_SPEAKER_MODELO_DIR),
                    run_opts={"device": "cpu"})
                _speaker_ultimo_uso = time.monotonic()
                _speaker_vigia_sobe()
    _speaker_ultimo_uso = time.monotonic()
    return _speaker_ecapa_enc


def _libera_arena() -> None:
    """Pede ao ALOCADOR que devolva páginas livres (best effort, sem dependência).

    `gc.collect()` sozinho não devolve RSS: o objeto morre e a arena fica. Medido
    aqui: sem isto, 548 MB de pico continuavam 557 MB depois de liberar; com o
    pedido ao alocador, ~100 MB voltam na hora. O resto é arena do próprio torch e
    só sai encerrando o processo — por isso o unload é por OCIOSIDADE, não uma
    promessa de devolver tudo.

    macOS: `malloc_zone_pressure_relief`; glibc: `malloc_trim`. Quem não existir,
    simplesmente não é chamado."""
    import ctypes

    try:
        libc = ctypes.CDLL(None)
    except OSError:               # sem libc à mão: fica só o gc
        return
    for nome, args in (("malloc_zone_pressure_relief", (None, 0)),
                       ("malloc_trim", (0,))):
        fn = getattr(libc, nome, None)
        if fn is None:
            continue
        try:
            fn(*args)
        except Exception:  # noqa: BLE001 — assinatura diferente não pode quebrar
            pass


def _speaker_ecapa_libera() -> None:
    """Solta o encoder ECAPA e devolve a RAM que dá (pico de ~490 MB, #526).

    O encoder é referência de módulo; soltar = `None` + coleta + pedido ao
    alocador. Quem estiver DENTRO de um embedding segue com a referência local que
    já pegou, e a próxima consulta recarrega — o gate não muda de decisão por
    causa disso, só paga o load de novo."""
    global _speaker_ecapa_enc
    with _speaker_enc_lock:
        enc, _speaker_ecapa_enc = _speaker_ecapa_enc, None
    if enc is None:
        return
    del enc
    gc.collect()
    gc.collect()                  # 2ª passada: finalizadores podem soltar mais
    try:                          # cache do torch (CPU/MPS) não devolve sozinho
        import torch

        cache = getattr(getattr(torch, "mps", None), "empty_cache", None)
        if callable(cache):
            cache()
    except Exception:  # noqa: BLE001 — sem torch carregado não há cache a soltar
        pass
    _libera_arena()
    print("[speaker] extrator ECAPA liberado por ociosidade", flush=True)


def _speaker_vigia_passo() -> bool:
    """Uma checagem do TTL: True = liberou agora. Separado do laço para testar."""
    if _speaker_ecapa_enc is None:
        return False
    if time.monotonic() - _speaker_ultimo_uso <= _SPEAKER_TTL_S:
        return False
    _speaker_ecapa_libera()
    return True


def _speaker_vigia_ecapa() -> None:
    """Vigia do TTL: enquanto houver encoder, confere a cada `_SPEAKER_VIGIA_S`."""
    while _speaker_ecapa_enc is not None:
        time.sleep(_SPEAKER_VIGIA_S)
        _speaker_vigia_passo()


def _speaker_vigia_sobe() -> None:
    """Sobe UM vigia (thread daemon) se ainda não houver."""
    global _speaker_vigia
    if _speaker_vigia is not None and _speaker_vigia.is_alive():
        return
    _speaker_vigia = threading.Thread(target=_speaker_vigia_ecapa, name="speaker-ttl",
                                      daemon=True)
    _speaker_vigia.start()


def _speaker_embed_ecapa_x(x) -> list[float] | None:
    """Embedding de um sinal JÁ em memória (float32 16 kHz) — sem arquivo.

    O portão do Live chama uma vez por janela: gravar/ler um wav por janela
    custava mais que os 17 ms do encoder e estourava o piso de latência do
    card (#533). Mesmo cálculo e mesma normalização L2 do caminho por arquivo."""
    try:
        import numpy as np
        import torch

        if x.size < 16000 * 0.6:        # mesmo piso do caminho legado
            return None
        enc = _speaker_encoder_ecapa()
        with torch.no_grad():
            e = np.asarray(enc.encode_batch(torch.from_numpy(x)[None]).squeeze().numpy(),
                           dtype=np.float64)
        return [float(v) for v in e / (float(np.linalg.norm(e)) or 1e-9)]
    except Exception as exc:  # noqa: BLE001 — sem o gate não derruba o STT
        print(f"[speaker] embedding ECAPA falhou: {exc}", flush=True)
        return None


def _speaker_embed_ecapa(wav_path: Path) -> list[float] | None:
    """Embedding do arquivo (caminho de cadastro/identificação)."""
    return _speaker_embed_ecapa_x(_wav_to_mono16k(wav_path))


def _cos_sim(a, b) -> float:
    """Cosseno entre dois vetores — e ERRO se as dimensões não casarem.

    O `zip` engolia a diferença: comparar um vetor do ECAPA (192-d) com um centróide
    do Resemblyzer (256-d) devolvia 0,055 em vez de ~1,0 para a MESMA fala, sem
    aviso. Dimensão diferente é sempre bug de extrator, nunca ruído (#534).
    """
    import math

    if len(a) != len(b):
        raise ValueError(f"dimensões diferentes no cosseno: {len(a)} x {len(b)} "
                         f"(extrator trocado sem trocar o vetor?)")
    num = sum(x * y for x, y in zip(a, b))
    da = math.sqrt(sum(x * x for x in a))
    db = math.sqrt(sum(x * x for x in b))
    return num / (da * db) if da and db else 0.0


def _speaker_centroide(vecs: list) -> list:
    """Média normalizada dos vetores do perfil.

    O R2 (#521) mediu o centróide vencendo o máximo entre amostras em TODOS os
    modelos (ECAPA 3 s: 0,19 % x 1,34 % de EER): a média cancela o ruído de cada
    gravação, o máximo deixa uma amostra ruim decidir sozinha."""
    import math

    n = len(vecs)
    if n == 0:
        return []
    if n == 1:
        return list(vecs[0])
    media = [sum(v[i] for v in vecs) / n for i in range(len(vecs[0]))]
    norma = math.sqrt(sum(x * x for x in media))
    return [x / norma for x in media] if norma else media


def _speaker_load_estado() -> tuple[dict, str]:
    """(perfis, erro). `erro` só quando o arquivo EXISTE e não deu para ler.

    Antes corrompido virava `{}` e o gate em `enforce`, sem perfil nenhum, ficava
    INERTE sem avisar (#324) — ilegível é diferente de "ninguém cadastrado".
    """
    if not SPEAKER_PATH.exists():
        return {}, ""
    try:
        d = json.loads(SPEAKER_PATH.read_text())
    except Exception as exc:  # noqa: BLE001
        return {}, f"{type(exc).__name__}: {exc}"
    if not isinstance(d, dict):
        return {}, "formato inesperado (não é objeto)"
    return d, ""


def _speaker_load() -> dict:
    return _speaker_load_estado()[0]


def _speaker_slug(nome: str) -> str:
    """Pasta do esquema ANTIGO (slug do nome) — só para migrar (#658)."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in nome)[:40] or "perfil"


def _speaker_audio_id(perfil: dict, nome: str = "") -> str:
    """Id ESTÁVEL e único da pasta de áudio do perfil (não o nome).

    Slug de nome colide (`Ana Maria`, `Ana_Maria` e `Ana/Maria` caem no mesmo lugar
    e o delete de um apaga o áudio do outro) e o corte de 40 chars junta nomes
    longos. O id nasce no primeiro áudio guardado e vive no perfil."""
    ident = str(perfil.get("audio_id") or "")
    if not ident:
        ident = uuid.uuid4().hex[:16]
        perfil["audio_id"] = ident
        # MIGRAÇÃO do esquema de slug (#658): quem guardou áudio entre o `3dd6a70` e
        # o `c8134e9` tem a pasta velha — ela é adotada em vez de ficar órfã. Dois
        # nomes que dividiam a mesma pasta: o primeiro que migrar leva.
        antiga = SPEAKER_AUDIO_DIR / _speaker_slug(nome)
        if nome and antiga.is_dir() and any(antiga.glob("*.wav")):
            try:
                antiga.rename(SPEAKER_AUDIO_DIR / ident)
            except OSError:
                pass
    return ident


def _speaker_audio_dir(perfil: dict, nome: str = "") -> Path:
    return SPEAKER_AUDIO_DIR / _speaker_audio_id(perfil, nome)


def _speaker_guarda_audio(nome: str, origem: Path) -> None:
    """Copia a amostra aceita para o arquivo de cadastro (0600, best effort).

    Nunca levanta: sem o áudio guardado o recadastro volta a exigir gravação, e é
    só isso (#658)."""
    if not _settings.get("speaker_guardar_audio"):
        return
    try:
        with _speaker_lock:                 # o id do perfil pode precisar nascer
            perfis = _speaker_load()
            perfil = perfis.setdefault(nome, {"vecs": [], "updated": 0})
            novo_id = "audio_id" not in perfil
            pasta = _speaker_audio_dir(perfil, nome)
            if novo_id:
                _speaker_save(perfis)
        pasta.mkdir(parents=True, exist_ok=True)
        destino = pasta / f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}.wav"
        shutil.copyfile(origem, destino)
        destino.chmod(0o600)
    except Exception as exc:  # noqa: BLE001 — guardar é melhoria, não requisito
        print(f"[speaker] não guardei o áudio de '{nome}': {exc}", flush=True)


def _speaker_apaga_audio(perfil: dict | None) -> None:
    ident = str((perfil or {}).get("audio_id") or "")
    if not ident:
        return
    try:
        shutil.rmtree(SPEAKER_AUDIO_DIR / ident, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


def _speaker_reextrai(nome: str, perfil: dict) -> bool:
    """Reextrai os vetores do perfil a partir do áudio guardado (#658).

    Chamado quando o extrator mudou. Devolve True se conseguiu promover o perfil
    para o extrator ATUAL; até lá o antigo continua valendo (o `modelo` só muda no
    fim, com o mínimo de amostras)."""
    efetivo = _speaker_modelo_efetivo()
    if _speaker_modelo_do_perfil(perfil) == efetivo:
        return False
    pasta = _speaker_audio_dir(perfil, nome)
    if not pasta.is_dir():
        return False
    vecs: list = []
    for arq in sorted(pasta.glob("*.wav")):
        try:
            emb = _speaker_embed(arq, efetivo)
        except Exception as exc:  # noqa: BLE001 — um .wav corrompido não derruba o resto
            print(f"[speaker] áudio {arq.name} de '{nome}' falhou: {exc}", flush=True)
            continue
        if emb:
            vecs.append(emb)
        if len(vecs) >= SPEAKER_MAX_SAMPLES:
            break
    if len(vecs) < SPEAKER_MIN_AMOSTRAS:
        return False
    perfil["vecs"] = vecs[:SPEAKER_MAX_SAMPLES]
    perfil["modelo"] = efetivo
    perfil.pop("pendente", None)
    perfil["updated"] = time.time()
    print(f"[speaker] perfil '{nome}' reextraído no extrator {efetivo} "
          f"({len(vecs)} amostra(s)) — sem regravação", flush=True)
    return True


_speaker_reextrai_thread: threading.Thread | None = None


def _speaker_reextrai_todos() -> None:
    """Reextrai quem precisa, FORA do caminho do GET (o embed pode custar caro)."""
    with _speaker_lock:
        perfis = _speaker_load()
        mudou = False
        for nome, p in list(perfis.items()):
            try:
                if _speaker_reextrai(nome, p):
                    mudou = True
            except Exception as exc:  # noqa: BLE001 — um perfil ruim não para os outros
                print(f"[speaker] reextração de '{nome}' falhou: {exc}", flush=True)
        if mudou:
            _speaker_save(perfis)


def _speaker_reextrai_em_segundo_plano(perfis: dict) -> None:
    """Sobe a reextração se algum perfil precisa e não há uma rodando."""
    global _speaker_reextrai_thread
    efetivo = _speaker_modelo_efetivo()
    if not any(_speaker_modelo_do_perfil(p) != efetivo for p in perfis.values()):
        return
    if _speaker_reextrai_thread is not None and _speaker_reextrai_thread.is_alive():
        return
    _speaker_reextrai_thread = threading.Thread(target=_speaker_reextrai_todos,
                                                name="speaker-reextrai", daemon=True)
    _speaker_reextrai_thread.start()


def _speaker_save(perfis: dict) -> None:
    # tmp único + os.replace: crash no meio não deixa JSON truncado (#324)
    write_json_atomic(SPEAKER_PATH, perfis)
    try:
        SPEAKER_PATH.chmod(0o600)
    except OSError:
        pass


def _speaker_pontua(wav_path: Path, perfis: dict
                    ) -> tuple[str | None, float | None, bool, str]:
    """(melhor perfil, melhor escore, houve_embedding) contra TODOS os perfis.

    Cada perfil é pontuado pelo extrator que o gerou — um vetor do Resemblyzer não
    se compara com um do extrator novo (#525). O embedding de cada modelo presente
    sai UMA vez, não por perfil. Escore = cosseno contra o CENTRÓIDE das amostras
    do perfil (o R2 mostrou o centróide vencendo o máximo)."""
    modelos = {_speaker_modelo_do_perfil(p) for p in perfis.values()}
    modelos.add(_speaker_modelo_efetivo())   # o extrator em uso sempre participa
    embs = {}
    for m in modelos:
        v = _speaker_embed(wav_path, m)
        if v:
            embs[m] = v
    melhor, sim, modo = None, None, "cru"
    for nome, p in perfis.items():
        modelo = _speaker_modelo_do_perfil(p)
        emb = embs.get(modelo)
        vecs = p.get("vecs") or []
        if emb is None or not vecs:
            continue
        s_, modo_ = _speaker_asnorm(emb, vecs, modelo)
        if sim is None or s_ > sim:
            melhor, sim, modo = nome, s_, modo_
    return melhor, sim, bool(embs), modo


#: limiar normalizado por (modelo, FAR alvo), calibrado no R2 (#521). Só vale
#: para escore AS-normalizado — não se compara com o 0,6/0,75 cru.
_SPEAKER_LIMIAR_FAR = {
    "resemblyzer:v1": {"1": 2.906, "0.1": 3.173},
    # "1.8" é o JOELHO medido no corpus do R4 (#533): o 2,434 veio da calibração
    # do R2, com OUTRO corpus e outro cadastro, e no corpus do card custa a
    # cobertura do dono (57 %); no 1,8 são 90 % com 0 turno falso em 10 min.
    # as bandas são de FAR CALIBRADO (identidade: speaker_gate, anti-clone, "só a
    # voz de:" no STT/Tradutor). O joelho do Live NÃO entra aqui: ele é
    # `_VOZ_LIMIAR_JOELHO` (constante própria), senão mexer no portão do Live
    # mexeria na identidade (decisão do PM, #533).
    "ecapa:v1": {"1": 2.434, "0.1": 3.044},
}
#: artefato da coorte por extrator. O do ECAPA é o `.npy` publicado em `models/`
#: junto dos pesos (o `.json` ao lado é a ficha: modelo, dimensão, corpus) — antes o
#: código procurava `tools/bakeoff/cohorte_ecapa.json`, que não existe, e o alvo de
#: FAR nunca ligava (#599)
_SPEAKER_COHORTE_ARQ = {
    "resemblyzer:v1": BASE / "tools" / "bakeoff" / "cohorte_resemblyzer.json",
    "ecapa:v1": BASE / "models" / "asnorm-coorte-ecapa.npy",
}
_speaker_cohorte_cache: dict = {}


def _speaker_cohorte(modelo: str) -> list:
    """Coorte de impostores do modelo (embeddings), vazia se não houver arquivo.

    A coorte é POR MODELO: aplicar AS-norm do Resemblyzer num vetor de outro
    modelo piora em vez de ajudar (o R2 mediu 1,9 % → 5,0 % em 1 s).

    `TTS_ASNORM_COORTE` substitui o artefato do modelo ATUAL (contrato do
    `models/README.md`), `.npy` ou `.json`. A ficha ao lado do `.npy` (mesmo nome,
    `.json`) é conferida: rótulo do extrator e dimensão — coorte do extrator errado
    é pior que nenhuma."""
    if modelo in _speaker_cohorte_cache:
        return _speaker_cohorte_cache[modelo]
    caminho = _SPEAKER_COHORTE_ARQ.get(modelo)
    bruto = (os.environ.get("TTS_ASNORM_COORTE") or "").strip()
    if bruto and modelo == SPEAKER_MODELO_ATUAL:
        caminho = Path(bruto).expanduser()
    vecs: list = []
    if caminho is not None:
        try:
            ficha = {}
            try:
                ficha = json.loads(caminho.with_suffix(".json").read_text())
            except Exception:  # noqa: BLE001 — ficha é conferência, não requisito
                pass
            rotulo = str(ficha.get("modelo") or "")
            # "ecapa:v1" x "models/ecapa-voxceleb": casa pelo nome do extrator
            if rotulo and rotulo != modelo and modelo.split(":")[0] not in rotulo:
                raise ValueError(f"coorte de outro modelo ({rotulo})")
            if caminho.suffix == ".npy":
                import numpy as np

                matriz = np.load(caminho)
                if matriz.ndim != 2:
                    raise ValueError(f"matriz {matriz.shape} (esperado 2-D)")
                if ficha.get("dimensao") and int(ficha["dimensao"]) != matriz.shape[1]:
                    raise ValueError(f"dimensão {matriz.shape[1]} x ficha {ficha['dimensao']}")
                vecs = [[float(x) for x in linha] for linha in matriz]
            else:
                dados = json.loads(caminho.read_text())
                vetores = dados.get("vecs")
                if not isinstance(vetores, list):     # a ficha usa `vetores` como CONTAGEM
                    vetores = dados.get("vetores")
                if not isinstance(vetores, list):
                    raise ValueError("sem lista de vetores")
                vecs = [v for v in vetores if isinstance(v, list) and v]
            if vecs and len({len(v) for v in vecs}) > 1:
                raise ValueError("vetores de dimensões diferentes")
        except Exception as e:  # noqa: BLE001 — sem coorte cai no escore cru
            print(f"[speaker] coorte {modelo} ilegível ({caminho}): {e}", flush=True)
            vecs = []
    _speaker_cohorte_cache[modelo] = vecs
    return vecs


def _speaker_asnorm(emb, vecs_perfil: list, modelo: str,
                    alvo: str | None = None) -> tuple[float, str]:
    """(escore, modo). Sem coorte ou sem alvo de FAR, devolve o cosseno CRU.

    Com alvo, normaliza pelo top-10 da coorte de impostores: `(s - mu) / sd`. É a
    normalização que fez o ECAPA sair de 1,6 % para 0,19 % de EER no R2 — e é
    específica do modelo.

    `alvo` explícito vale para quem tem critério PRÓPRIO (o portão do Live usa o
    `rigor_far` do bloco, não o setting global de cadastro) — #533."""
    centroide = _speaker_centroide(vecs_perfil)
    cru = _cos_sim(emb, centroide)
    alvo = (str(_settings.get("speaker_far_alvo") or "").strip()
            if alvo is None else str(alvo or "").strip())
    coorte = _speaker_cohorte(modelo) if alvo else []
    if not alvo or not coorte:
        return cru, "cru"
    notas = sorted((_cos_sim(emb, c) for c in coorte), reverse=True)[:10]
    n = len(notas)
    if n < 2:
        return cru, "cru"
    mu = sum(notas) / n
    sd = (sum((x - mu) ** 2 for x in notas) / n) ** 0.5
    return (cru - mu) / (sd or 1e-6), "asnorm"


def _speaker_limiar(modelo: str) -> tuple[float, str]:
    """(limiar, modo). `speaker_threshold` cru explícito continua vencendo."""
    alvo = str(_settings.get("speaker_far_alvo") or "").strip()
    if alvo and _speaker_cohorte(modelo):
        lim = _SPEAKER_LIMIAR_FAR.get(modelo, {}).get(alvo)
        if lim is not None:
            return float(lim), "asnorm"
    return float(_settings.get("speaker_threshold")
                 or _SETTINGS_DEFAULTS["speaker_threshold"]), "cru"


def _speaker_identify(wav_path: Path) -> tuple[str | None, float]:
    """Quem fala no wav: (nome, melhor_sim) ou (None, melhor_sim) se ninguém."""
    melhor, sim, _houve, _modo = _speaker_pontua(wav_path, _speaker_load())
    return melhor, (sim or 0.0)


def _speaker_gate_ok(wav_path: Path) -> dict:
    """Aplica o gate configurado. Devolve dict p/ enriquecer a resposta:
    rejected=True bloqueia; speaker=<nome> etiqueta o locutor."""
    gate = (_settings.get("speaker_gate") or "off").lower()
    if gate == "off":
        return {}
    with _speaker_lock:              # leitura sob o mesmo lock da escrita
        perfis, erro = _speaker_load_estado()
    if erro:
        # arquivo ilegível com o gate ligado: em `enforce` FALHA FECHADO (antes o
        # `{}` deixava o gate inerte sem ninguém saber) e avisa nos dois modos
        print(f"[speaker] perfis ilegíveis ({erro}) — gate {gate}", flush=True)
        if gate == "enforce":
            return {"rejected": True,
                    "reason": f"perfis de voz ilegíveis ({erro[:80]})"}
        return {"speaker_aviso": f"perfis de voz ilegíveis ({erro[:80]})"}
    if not perfis:
        return {}  # sem perfis cadastrados: gate inerte (não é erro)
    melhor, sim, houve_emb, modo = _speaker_pontua(wav_path, perfis)
    if not houve_emb:
        # sem embedding (áudio curto/erro): enforce rejeita, label segue
        # `enforce` rejeita; `label` antes seguia MUDO — agora diz que seguiu sem
        # etiqueta por falta de embedding (#282)
        return ({"rejected": True, "reason": "áudio curto p/ identificar a voz"}
                if gate == "enforce" else
                {"speaker_aviso": "áudio curto p/ identificar a voz — segue sem "
                                  "etiqueta de locutor"})
    modelo_limiar = _speaker_modelo_do_perfil(perfis[melhor]) if melhor \
        else _speaker_modelo_efetivo()
    lim, modo_lim = _speaker_limiar(modelo_limiar)
    autorizado = melhor is not None and sim is not None and sim >= lim
    out: dict = {}
    # o modo só aparece quando é normalizado: escore AS-norm não se confunde com
    # o cru, e o caminho cru (default) mantém a resposta de antes
    if modo_lim == "asnorm":
        out["speaker_escore_modo"] = modo_lim
    if gate == "enforce" and not autorizado:
        out["rejected"] = True
        out["reason"] = (f"voz não autorizada (melhor: {melhor or 'ninguém'} "
                         f"{sim if sim is not None else 0:.2f} < {lim:.2f})" if melhor
                         else f"voz não reconhecida ({lim:.2f})")
    if gate == "label" and autorizado:
        out["speaker"] = melhor
        out["speaker_sim"] = round(sim, 3)
    # GANCHO anti-clone (#525/#522): com o setting DESLIGADO (default) nada muda —
    # nem campo na resposta. O R3 ainda não foi medido, então ligar hoje só diria
    # "pendente"; o detector entra é aqui.
    if _settings.get("speaker_anticlone"):
        anti = _speaker_anticlone(wav_path)
        out.update(anti)
        # VETO: áudio GERADO (clone) sai marcado; fala humana não. Só para áudio de
        # ARQUIVO (o Live tem o caminho dele) e só quando o detector respondeu —
        # `indisponivel` NÃO rejeita, porque marca ausente é a maioria da fala real.
        if anti.get("anticlone") == "marcado":
            # voz não autorizada já foi barrada pelo escore: manter AQUELE motivo
            ja_barrado = bool(out.get("rejected"))
            if gate == "enforce" and not ja_barrado:
                out["rejected"] = True
                out["reason"] = ("áudio com marca d'água de TTS (clone) — "
                                 f"detecção {anti.get('anticlone_prob')}")
            elif gate != "enforce":
                out["speaker_aviso"] = ("áudio parece CLONE: marca d'água do TTS "
                                        "detectada")
    return out


def _speaker_anticlone(wav_path: Path) -> dict:
    """Anti-clone do áudio pela MARCA d'água do AudioSeal (#657).

    Clone feito pelo app sai marcado; fala humana não. Devolve a probabilidade e o
    veredito; sem detector disponível diz `indisponivel` (nunca inventa aprovação).
    O limite honesto é o áudio curto/cortado (R3: 83 % com 60 % do trecho cortado),
    por isso a política de rejeitar em `enforce` é do #613, não daqui."""
    import marca_dagua

    if not marca_dagua.disponivel():
        return {"anticlone": "indisponivel", "anticlone_motivo": marca_dagua.motivo()}
    prob = marca_dagua.detecta_prob(wav_path)
    if prob is None:
        return {"anticlone": "indisponivel", "anticlone_motivo": "detecção falhou"}
    return {"anticlone": "marcado" if prob >= 0.5 else "cru",
            "anticlone_prob": round(prob, 3)}


# ── "só a minha voz" no Live (#533) ────────────────────────────────────────
# Protocolo do R4 (`evidence/532`): ECAPA em janela de 1 s, passo de 0,25 s,
# **2 janelas seguidas** acima do limiar e piso de energia (0,15 do p90). Falso
# turno medido: 0 em 10 min; custo 17 ms por janela com o modelo carregado — daí
# o PREWARM por sessão.
# LIMIAR: o escore é o ECAPA **AS-normalizado** com a mesma coorte do I3 e limiar
# por alvo de FAR (`rigor_far`; default "1.8" = 1,8 no escore AS-normalizado, o
# JOELHO medido no corpus do R4 — o "1" (2,434) vem da calibração do R2, com outro
# corpus e outro cadastro, e custa 33 p.p. de cobertura do dono). O 0,44 CRU é só
# QUEDA, quando não há coorte/alvo ou a normalização não pegou (0,35 cru passa voz
# parecida, `evidence/521`); perfil legado vai SEMPRE no cru.
_VOZ_JANELA_S = 1.0
_VOZ_PASSO_S = 0.25
_VOZ_CONFIRMACOES = 2
_VOZ_PISO_ENERGIA = 0.15
_VOZ_LIMIAR_PADRAO = 0.44         # queda CRU (sem coorte): nunca é o caminho feliz
#: JOELHO do portão do Live no corpus do R4 (#533): 90 % de cobertura do dono com
#: 0 turno falso em 10 min. NÃO é banda de FAR calibrado — vale SÓ aqui; a
#: identidade (`speaker_gate`, anti-clone, "só a voz de:") segue no FAR 1 %
#: (2,434), que é o número calibrado no R2 (decisão do PM).
_VOZ_LIMIAR_JOELHO = 1.8
#: nome da faixa do joelho (o `rigor_far` do dono aceita "1"/"joelho"/"0.1")
_VOZ_RIGOR_JOELHO = "joelho"
_VOZ_RIGOR_PADRAO = _VOZ_RIGOR_JOELHO


def _live_voz_ligado() -> dict | None:
    cfg = _settings.get("live_so_minha_voz") or {}
    return cfg if cfg.get("on") else None


def _live_voz_perfil(cfg: dict) -> tuple[str, dict] | None:
    """(nome, perfil) do dono. Sem escolha explícita usa o único/primeiro — o
    dono costuma ter um perfil só, e exigir o nome travaria o modo à toa."""
    with _speaker_lock:
        perfis, erro = _speaker_load_estado()
    if erro or not perfis:
        return None
    nome = str(cfg.get("perfil") or "")
    if nome and nome in perfis:
        return nome, perfis[nome]
    if nome:
        return None                       # escolhido e sumiu: não troca de voz sozinho
    primeiro = next(iter(perfis))
    return primeiro, perfis[primeiro]


def _voz_limiar(modelo: str, rigor: str, fixo: float = 0.0) -> tuple[float, str]:
    """(limiar, modo) do portão do Live.

    Ordem: `limiar` explícito do bloco (no espaço do escore em uso) > AS-norm com
    a coorte e o limiar por FAR (`_SPEAKER_LIMIAR_FAR`, o do cadastro) > 0,44 CRU
    quando não há coorte/alvo.

    AS-norm SÓ em perfil do extrator ATUAL (decisão do PM, achado do gate do
    #534): os perfis do dono hoje são legados e o escore normalizado de um
    extrator não atravessa para o outro. Perfil legado vai para o limiar CRU do
    Resemblyzer mesmo com coorte disponível."""
    asnorm_ok = (modelo == SPEAKER_MODELO_ATUAL and bool(rigor)
                 and bool(_speaker_cohorte(modelo)))
    if fixo and fixo > 0:
        return float(fixo), ("asnorm" if asnorm_ok else "cru")
    if asnorm_ok and rigor in (_VOZ_RIGOR_JOELHO, "1.8"):
        return _VOZ_LIMIAR_JOELHO, "asnorm"
    if asnorm_ok:
        lim = _SPEAKER_LIMIAR_FAR.get(modelo, {}).get(rigor)
        if lim is not None:
            return float(lim), "asnorm"
    return _VOZ_LIMIAR_PADRAO, "cru"


def _voz_do_dono(pcm16: bytes, cfg: dict | None = None) -> dict:
    """O trecho é o DONO falando? `{ok, score, janelas, latencia_ms, estado, motivo}`.

    `ok=True` também nos casos de INDISPONIBILIDADE (fail-open, nunca ficar surdo):
    o `estado` diz `indisponivel` e o `motivo` explica. Chamado pelo filtro
    `identidade` do barge e pela cabeça do `_stt_app` (uma verificação só).

    `cfg` é o bloco do modo (`live_so_minha_voz`). O filtro `identidade` passa o
    dele, então ele funciona SEM o modo I5 ligado — com `perfis`/`far` do próprio
    filtro."""
    t0 = time.monotonic()
    cfg = cfg or _live_voz_ligado()
    if not cfg:
        return {"ok": True, "estado": "off", "janelas": 0, "score": None,
                "latencia_ms": 0, "motivo": ""}

    def _fora(motivo: str, **extra) -> dict:
        return {"ok": True, "estado": "indisponivel", "janelas": 0, "score": None,
                "latencia_ms": int((time.monotonic() - t0) * 1000),
                "motivo": motivo, **extra}

    alvo = _live_voz_perfil(cfg)
    if alvo is None:
        return _fora("perfil de voz ausente")
    _nome, perfil = alvo
    vecs = perfil.get("vecs") or []
    if not vecs:
        return _fora("perfil sem amostras")
    modelo = _speaker_modelo_do_perfil(perfil)
    try:
        import numpy as np
        a = np.frombuffer(pcm16[: pcm16.size if hasattr(pcm16, "size") else len(pcm16)],
                          dtype="<i2").astype(np.float32) / 32768.0
    except Exception as exc:              # noqa: BLE001
        return _fora(f"áudio ilegível ({type(exc).__name__})")
    janela = int(16000 * _VOZ_JANELA_S)
    passo = int(16000 * _VOZ_PASSO_S)
    if a.size < janela:                   # curto demais p/ 1 janela cheia
        if a.size < 16000 * 0.6:          # piso do próprio extrator
            return _fora("áudio curto para identificar")
        janela = a.size
    # piso de ENERGIA: ventoinha/zumbido não confirmam ninguém (p90 do trecho)
    quadro = 512
    rms = np.array([float(np.sqrt((a[i:i + quadro] ** 2).mean()))
                    for i in range(0, max(quadro, a.size - quadro), quadro)] or [0.0])
    piso = float(np.percentile(rms, 90)) * _VOZ_PISO_ENERGIA if rms.size else 0.0
    lim, modo_lim = _voz_limiar(modelo, str(cfg.get("rigor_far") or _VOZ_RIGOR_PADRAO),
                                float(cfg.get("limiar") or 0.0))
    alvo_norm = str(cfg.get("rigor_far") or _VOZ_RIGOR_PADRAO) if modo_lim == "asnorm" else ""
    confirmacoes, scores = 0, []
    # 2 confirmações assim que cabem 2 janelas (1,25 s); abaixo disso, 1 — a fala
    # de 1 s num barge curto não tem como confirmar duas vezes (#533)
    necessarias = (1 if a.size < int(16000 * (_VOZ_JANELA_S + _VOZ_PASSO_S))
                   else _VOZ_CONFIRMACOES)
    for ini in range(0, max(1, a.size - janela + 1), passo):
        trecho = a[ini:ini + janela]
        if trecho.size < janela:
            break
        if float(np.sqrt((trecho ** 2).mean())) < piso and piso > 0:
            continue                      # só ruído de fundo nesta janela
        if modelo == SPEAKER_MODELO_ATUAL:
            # sem arquivo (#533): o gate chama por janela e o I/O dominava os 17 ms
            emb = _speaker_embed_ecapa_x(trecho)
        else:                             # extrator legado: só sabe ler caminho
            _fd, nome_tmp = tempfile.mkstemp(prefix="tts-voz-", suffix=".wav")
            os.close(_fd)
            tmp = Path(nome_tmp)
            try:
                with wave.open(str(tmp), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(16000)
                    w.writeframes((np.clip(trecho, -1, 1) * 32767).astype("<i2").tobytes())
                emb = _speaker_embed(tmp, modelo)
            finally:
                tmp.unlink(missing_ok=True)
        if not emb:
            break                         # extrator indisponível: fail-open
        score, modo_esc = _speaker_asnorm(emb, vecs, modelo, alvo=alvo_norm)
        if modo_esc != "asnorm":
            # a normalização não pegou (coorte ilegível): compara no CRU com a
            # queda — escore normalizado contra limiar cru erraria por muito
            lim, modo_lim = _VOZ_LIMIAR_PADRAO, "cru"
        scores.append(score)
        if score >= lim:
            confirmacoes += 1
            if confirmacoes >= necessarias:
                break
    if not scores:
        return _fora("nenhuma janela com voz")
    ok = confirmacoes >= necessarias
    return {"ok": ok, "estado": "ok" if ok else "recusado", "limiar": round(lim, 3),
            "escore": modo_lim,
            "janelas": len(scores), "score": round(max(scores), 3),
            "latencia_ms": int((time.monotonic() - t0) * 1000),
            "motivo": "" if ok else f"outra voz (melhor {max(scores):.2f} < {lim:.2f})"}


def _live_voz_anota(sess: dict, res: dict) -> None:
    """Publica o veredito no `stats` (o instrumento do aceite do #533)."""
    st = sess.setdefault("voz_stats", {"recusados": 0})
    st["ultimo"] = {"estado": res.get("estado"), "score": res.get("score"),
                    "janelas": res.get("janelas"),
                    "latencia_ms": res.get("latencia_ms"),
                    "motivo": res.get("motivo")}
    if res.get("estado") == "recusado":
        st["recusados"] = int(st.get("recusados", 0)) + 1


#: qualidade do cadastro, derivada dos números do R2 (#525)
SPEAKER_MIN_FALA_S = 3.0      # 1 s é o caso do barge, não do cadastro
SPEAKER_MIN_SNR_DB = 10.0     # ventoinha a 5 dB degrada todos os modelos
SPEAKER_MIN_AMOSTRAS = 3      # abaixo disso o EER multiplica (~5x com 1 amostra)


def _speaker_avalia_amostra(tmp: Path, emb: list | None, vecs: list, modelo: str) -> list[str]:
    """Problemas da amostra que vai entrar (vazio = serve para cadastro).

    Três réguas: FALA mínima (VAD, não duração de arquivo), SNR e CONSISTÊNCIA
    com as amostras já guardadas — a voz trocada/ruidosa é descartada pelo escore
    médio contra as outras, comparado ao limiar de FAR 1 %."""
    faltas = []
    fala = _vad_fala_s(tmp)
    if fala is not None and fala < SPEAKER_MIN_FALA_S:
        faltas.append(f"fala curta: {fala:.1f}s de voz (mínimo "
                      f"{SPEAKER_MIN_FALA_S:.0f}s — pausas não contam)")
    snr = _snr_estimado(tmp)
    if snr is not None and snr < SPEAKER_MIN_SNR_DB:
        faltas.append(f"ruído alto: SNR {snr:.0f} dB (mínimo "
                      f"{SPEAKER_MIN_SNR_DB:.0f} dB) — grave num ambiente quieto")
    if emb and len(vecs) >= 2:
        lim, _modo = _speaker_limiar(modelo)
        centroide = _speaker_centroide(vecs)
        media = _cos_sim(emb, centroide)
        if media < lim:
            faltas.append(f"amostra destoa das outras ({media:.2f} < {lim:.2f}) — "
                          f"outra pessoa ou outra qualidade de áudio?")
    return faltas


def _speaker_avalia(tmp: Path, vecs_antes: list) -> tuple[list | None, list[str]]:
    """(embedding, problemas) — o corpo bloqueante do enroll, para a threadpool."""
    modelo = _speaker_modelo_efetivo()
    emb = _speaker_embed(tmp, modelo)
    if not emb:
        return None, []
    return emb, _speaker_avalia_amostra(tmp, emb, vecs_antes, modelo)


@app.post("/api/speaker/enroll")
async def speaker_enroll(request: Request, name: str = Form(...),
                         audio: UploadFile = None, forcar: bool = False):
    """Cadastra/atualiza um perfil de voz com o áudio enviado (3 s+ de FALA; 8–30 s
    de gravação é o confortável).

    Roda FORA do event loop (`run_in_threadpool`): VAD, SNR e o extrator são CPU e
    seguram o servidor inteiro enquanto rodam (#525). `forcar` aceita a amostra
    mesmo com problema, devolvendo os motivos em `problemas`.

    Perfil de OUTRO extrator: gravar aqui É O RECADASTRO (decisão do PM no #534).
    As amostras novas vão para `pendente` (no espaço do extrator atual), a
    consistência só se compara com as do MESMO espaço, e o perfil continua
    autenticando pelo antigo até o novo ter o mínimo — então ele passa a valer e o
    `precisa_recadastrar` cai. Sem isso a primeira amostra zerava o perfil antigo e
    uma comparação entre espaços (192 x 256) estourava."""
    from starlette.concurrency import run_in_threadpool

    nome = (name or "").strip()[:40]
    if not nome:
        raise HTTPException(400, "Nome do perfil obrigatório")
    if audio is None:
        raise HTTPException(400, "Áudio obrigatório")
    efetivo = _speaker_modelo_efetivo()
    with _speaker_lock:
        _perfis_antes = _speaker_load()
    _antes = _perfis_antes.get(nome) or {}
    # PERFIL EXISTENTE (ou `forcar`) é admin: a chave `use` que cadastra por cima
    # vira "o dono" para o gate e, com `forcar`, empurra as amostras antigas para
    # fora (`vecs[-MAX:]`) — 5 enrolls de um aparelho da LAN zeravam o dono (#786).
    # Perfil NOVO continua livre: é o aparelho da LAN cadastrando a própria voz.
    # A recusa vem ANTES de qualquer escrita (upload, `pendente`, `vecs`): o 403 não
    # pode deixar rastro no arquivo de perfis.
    if _antes or forcar:
        _exige_admin(request)
    legado = _speaker_modelo_do_perfil(_antes) if _antes else efetivo
    _pend = _antes.get("pendente") or {}
    if legado == efetivo:
        vecs_antes = list(_antes.get("vecs") or [])
    else:      # só as do espaço novo contam para a consistência
        vecs_antes = list(_pend.get("vecs") or []) if _pend.get("modelo") == efetivo else []
    # ffmpeg + ECAPA FORA do loop (#799)
    def _extrai():
        tmp = _save_audio_upload(audio, ".voz")
        try:
            emb, problemas = _speaker_avalia(tmp, vecs_antes)
            # guardado AQUI, antes do unlink, e só se a amostra vai entrar: é o que
            # permite reextrair sozinho depois de uma troca de extrator (#658)
            if emb and (forcar or not problemas):
                _speaker_guarda_audio(nome, tmp)
            return emb, problemas
        finally:
            tmp.unlink(missing_ok=True)

    emb, problemas = await run_in_threadpool(_extrai)
    if not emb:
        raise HTTPException(400, "Não deu para extrair a voz — grave 8s+ falando")
    if problemas and not forcar:
        raise HTTPException(400, "Amostra recusada: " + "; ".join(problemas)
                            + ". Use `forcar=true` para entrar assim mesmo.")
    def _grava():
      # o lock (que a reextracao segura por inteiro) e a gravacao saem do loop (#799)
      with _speaker_lock:
        perfis = _speaker_load()
        p = perfis.get(nome, {"vecs": [], "updated": 0})
        if legado == efetivo:
            # mesmo espaço: acumula normalmente e o perfil fica no extrator atual
            vecs = list(p.get("vecs") or [])
            vecs.append(emb)
            p["vecs"] = vecs[-SPEAKER_MAX_SAMPLES:]
            p["modelo"] = efetivo
            p.pop("pendente", None)
            recadastrando = False
        else:
            # RECADASTRO: as amostras do espaço novo ficam separadas e o perfil
            # SEGUE no antigo (o gate não perde o dono no meio do caminho)
            pend = dict(p.get("pendente") or {})
            vecs = list(pend.get("vecs") or []) if pend.get("modelo") == efetivo else []
            vecs.append(emb)
            p["pendente"] = {"modelo": efetivo, "vecs": vecs[-SPEAKER_MAX_SAMPLES:]}
            recadastrando = True
            if len(p["pendente"]["vecs"]) >= SPEAKER_MIN_AMOSTRAS:
                # mínimo atingido: o espaço novo passa a valer e o antigo sai
                p["vecs"] = p["pendente"]["vecs"]
                p["modelo"] = efetivo
                p.pop("pendente", None)
                recadastrando = False
        p["updated"] = time.time()
        perfis[nome] = p
        _speaker_save(perfis)
        n = len(p["vecs"]) if "pendente" not in p else len(p["pendente"]["vecs"])
        return n, _speaker_modelo_do_perfil(p), recadastrando

    n, em_uso, recadastrando = await run_in_threadpool(_grava)
    faltam = max(0, SPEAKER_MIN_AMOSTRAS - n)
    return {"ok": True, "name": nome, "samples": n,
            "modelo": efetivo, "em_uso": em_uso, "recadastro": recadastrando,
            "problemas": problemas,
            # cadastro incompleto é AVISO, não erro: dá para ir gravando
            "completo": faltam == 0,
            "faltando": [f"mais {faltam} amostra(s)"] if faltam else []}


@app.get("/api/speaker/profiles")
def speaker_profiles():
    with _speaker_lock:
        perfis = _speaker_load()
    # troca de extrator: quem tem áudio guardado se recadastra SOZINHO, em segundo
    # plano — o embed pode custar segundos e não pode segurar a lista (#658)
    _speaker_reextrai_em_segundo_plano(perfis)
    return [{"name": n, "samples": len(p.get("vecs", [])),
             "updated": p.get("updated", 0),
             "modelo": _speaker_modelo_do_perfil(p),
             # perfil de outro extrator segue autenticando, mas o dono precisa saber
             # que ele sai daqui a um recadastro (a UI oferece a gravação de novo).
             # `pendente` é o recadastro EM ANDAMENTO: quantas amostras do extrator
             # novo já entraram (a tela guia até o mínimo e o antigo segue valendo)
             "precisa_recadastrar":
                 _speaker_modelo_do_perfil(p) != _speaker_modelo_efetivo(),
             "pendente": ({"modelo": p["pendente"].get("modelo"),
                           "samples": len(p["pendente"].get("vecs") or [])}
                          if p.get("pendente") else None)}
            for n, p in sorted(perfis.items())]


@app.delete("/api/speaker/{name}/pendente")
def speaker_pendente_descarta(request: Request, name: str):
    """Joga fora o recadastro PARCIAL e deixa o perfil como estava (admin, #534).

    Sem TTL: dado do dono não some por relógio (decisão do PM). O perfil segue
    autenticando pelo extrator antigo com os vetores dele intactos — só o espaço
    novo em construção sai."""
    _exige_admin(request)
    with _speaker_lock:
        perfis = _speaker_load()
        p = perfis.get(name)
        if p is None:
            raise HTTPException(404, "Perfil não encontrado")
        if p.pop("pendente", None) is None:
            raise HTTPException(404, "Este perfil não tem recadastro em andamento")
        p["updated"] = time.time()
        _speaker_save(perfis)
        em_uso = _speaker_modelo_do_perfil(p)
        amostras = len(p.get("vecs") or [])
    return {"ok": True, "name": name, "em_uso": em_uso, "samples": amostras}


@app.delete("/api/speaker/{name}")
def speaker_delete(request: Request, name: str):
    _exige_admin(request)
    with _speaker_lock:
        perfis = _speaker_load()
        removido = perfis.pop(name, None)
        if removido is None:
            raise HTTPException(404, "Perfil não encontrado")
        _speaker_save(perfis)
    _speaker_apaga_audio(removido)  # o áudio de cadastro vai junto (#658)
    return {"ok": True}


@app.post("/api/speaker/avalia")
async def speaker_avalia(audio: UploadFile = None):
    """Medidor AO VIVO do cadastro: réguas de qualidade sem tocar em perfil (#690).

    O assistente manda um trecho curto ~1×/s enquanto a pessoa fala, para ela
    corrigir ANTES de gravar. Devolve FALA (VAD), SNR e os MESMOS problemas do
    `enroll` — só as réguas de amostra, sem consistência (não há perfil aqui).
    É de USO, como o resto de `/api/speaker/*`."""
    from starlette.concurrency import run_in_threadpool

    if audio is None:
        raise HTTPException(400, "Áudio obrigatório")
    # o `_save_audio_upload` também é ffmpeg: vai junto para a thread (#799)
    def _mede():
        tmp = _save_audio_upload(audio, ".vozav")
        try:
            return _vad_fala_s(tmp), _snr_estimado(tmp)
        finally:
            tmp.unlink(missing_ok=True)

    fala, snr = await run_in_threadpool(_mede)
    problemas = []
    if fala is not None and fala < SPEAKER_MIN_FALA_S:
        problemas.append(f"fala curta: {fala:.1f}s de voz (mínimo "
                         f"{SPEAKER_MIN_FALA_S:.0f}s — pausas não contam)")
    if snr is not None and snr < SPEAKER_MIN_SNR_DB:
        problemas.append(f"ruído alto: SNR {snr:.0f} dB (mínimo "
                         f"{SPEAKER_MIN_SNR_DB:.0f} dB) — grave num ambiente quieto")
    return {"fala_s": None if fala is None else round(fala, 2),
            "snr_db": None if snr is None else round(snr, 1),
            "ok": not problemas, "problemas": problemas}


@app.post("/api/speaker/check")
async def speaker_check(audio: UploadFile = None):
    """Diagnóstico: quem é a voz do áudio e a similaridade — a MESMA régua do gate.

    Usa `_speaker_pontua` (centróide das amostras, escore do extrator do perfil) e
    devolve o limiar e o modo que o gate aplicaria, além do veredito `passaria`.
    Antes pontuava pelo MÁXIMO das amostras e devolvia só `sim`: a tela concluía
    "passa/não passa" com um número de outra escala e o teste de perfil podia
    contrariar o gate (#525/#590)."""
    if audio is None:
        raise HTTPException(400, "Áudio obrigatório")
    # FORA do event loop (#799): ffmpeg (até 120 s) e o ECAPA (carga no 1º uso)
    # seguravam TODAS as conexões — o Live incluso, que é quem mais sente.
    from starlette.concurrency import run_in_threadpool

    def _pontua():
        tmp = _save_audio_upload(audio, ".vozchk")
        try:
            with _speaker_lock:
                perfis = _speaker_load()
            return tmp, perfis, _speaker_pontua(tmp, perfis)
        finally:
            tmp.unlink(missing_ok=True)

    _tmp, perfis, (melhor, sim, houve_emb, _modo_escore) = \
        await run_in_threadpool(_pontua)
    if not houve_emb:
        raise HTTPException(400, "Áudio curto demais p/ identificar (grave 8s+)")
    modelo = (_speaker_modelo_do_perfil(perfis[melhor]) if melhor
              else _speaker_modelo_efetivo())
    limiar, modo_limiar = _speaker_limiar(modelo)
    passa = bool(melhor and sim is not None and sim >= limiar)
    return {"speaker": melhor, "sim": round(sim or 0.0, 3),
            "limiar": round(limiar, 3), "modo": modo_limiar,
            # veredito do servidor: com `asnorm` o `sim` não se compara ao rigor cru.
            # `passaria` é o contrato decidido no #590; `autorizado` é o nome anterior
            # (a tela já lê os dois por feature-detect) e fica enquanto houver cliente
            "passaria": passa, "autorizado": passa}


@app.post("/api/transcribe")
def transcribe_audio(response: Response, audio: UploadFile = None,
                     source_lang: str = Form("auto"), speaker_alvo: str = Form("")):
    """Transcrição pura (sem tradução/TTS): áudio -> texto + segmentos. Usa o
    Whisper local ou remoto, conforme as configurações de modelos remotos.

    `speaker_alvo` (nome de perfil, opcional): separa o áudio e transcreve SÓ a voz
    daquele perfil (#534). Sem o parâmetro o contrato é o de sempre."""
    if audio is None:
        raise HTTPException(400, "Áudio obrigatório")
    aviso = {}
    tmp = _save_audio_upload(audio)
    extra_alvo: dict = {}
    if _settings.get("speaker_anticlone"):
        try:                    # 1ª detecção custa 4,6 s: aquece fora do relógio
            import marca_dagua

            marca_dagua.prewarm()
        except Exception:       # noqa: BLE001 — prewarm é otimização
            pass
    try:
        gate = _speaker_gate_ok(tmp)
        if gate.get("rejected"):
            return {"rejected": True, "reason": gate["reason"], "text": ""}
        tmp_stt, extra_alvo = _aplica_speaker_alvo(tmp, speaker_alvo)
        try:
            r = _transcribe(tmp_stt, language=(source_lang or "auto").lower())
        finally:
            if tmp_stt != tmp:
                tmp_stt.unlink(missing_ok=True)
    finally:
        tmp.unlink(missing_ok=True)
    # remoto caiu e o local assumiu: o cliente PRECISA saber (senão "vazio" e
    # "provedor morto" ficam indistinguíveis num 200). Header sempre; campo no corpo
    # porque esta rota é JSON de ponta a ponta.
    _aviso_remoto_headers(response.headers, r)
    aviso = {"remote_fallback": True, "remote_error": r.get("remote_error", "")} \
        if r.get("remote_fallback") else {}
    # filtro anti-alucinação (blacklist "e aí", fragmentos, sem-fala): o
    # /api/transcribe alimenta a Conversa — ruído não vira mensagem
    texto = (r.get("text") or "").strip()
    ok, motivo = _stt_ok(r, texto)
    if not ok:
        return {**aviso, "rejected": True, "reason": motivo, "text": ""}
    segs = r.get("segments") or []
    out = {**aviso, "text": (r.get("text") or "").strip(),
           "language": (r.get("language") or "").strip().lower(),
           "segments": [{"start": float(s.get("start") or 0.0),
                         "end": float(s.get("end") or 0.0),
                         "text": (s.get("text") or "").strip()} for s in segs]}
    if gate.get("speaker"):
        out["speaker"] = gate["speaker"]
        out["speaker_sim"] = gate.get("speaker_sim")
        # escala do número junto do número: com FAR alvo o escore é AS-norm e não
        # se compara com o `speaker_threshold` cru (#525)
        if gate.get("speaker_escore_modo"):
            out["speaker_escore_modo"] = gate["speaker_escore_modo"]
    out.update(extra_alvo)          # `separacao`/`segmentos_descartados` quando pedido
    return out


def _preparar_fala_para_voz(audio: UploadFile, voice_id: str, source_lang: str,
                            emotion_mode: str, instruct: str, msg_voz_design: str,
                            com_gate: bool = False, speaker_alvo: str = "") -> dict:
    """Pipeline comum do tradutor e do modificador de fala.

    Valida a voz (gravada, padrão ou por descrição), aplica o gate de locutor,
    transcreve, filtra ruído/alucinação e idioma de entrada, captura a emoção e
    monta os controles de geração. Ordem dos efeitos: gate -> transcribe ->
    stt_ok -> idioma -> emoção -> unlink do temporário.

    Devolve {"resposta": {...}} quando o áudio é rejeitado (o endpoint só
    devolve esse corpo) ou os intermediários prontos p/ o passo da tradução:
    vid/vpath/gate/src_text/src_lang/lang/omni/emotion/emo_show/emo_err.
    """
    vid = voice_id or _settings["default_voice"]
    # id que ESCAPA de voices/ ('../x') não pode virar caminho do job: resolve para
    # uma voz registrada, como qualquer id desconhecido
    if vid and vid != DESIGN_VOICE_ID and vid not in OMNI_PRESETS and not _voice_path(vid):
        vid = _resolve_voice(vid)
    design = (vid == DESIGN_VOICE_ID)                       # voz por descrição (tags OmniVoice)
    des_instruct = _sanitize_instruct(instruct) if design else ""
    vpath = VOICES_DIR / f"{vid}.wav"
    if design:
        ok = des_instruct or _sanitize_instruct(_settings.get("omni_instruct") or "")
        if not ok:
            raise HTTPException(400, msg_voz_design)
    elif not vpath.exists() and vid not in OMNI_PRESETS:
        raise HTTPException(404, "Voz não encontrada — grave uma voz ou escolha uma voz padrão")

    exp = (source_lang or "auto").lower()
    tmp = _save_audio_upload(audio)
    gate = _speaker_gate_ok(tmp) if com_gate else {}
    if gate.get("rejected"):
        tmp.unlink(missing_ok=True)
        return {"resposta": {"rejected": True, "reason": gate["reason"], "source_text": ""}}
    emo_label, emo_pitch, emo_speed, emo_err = "", "", 1.0, None
    extra_alvo: dict = {}
    tmp_stt: Path = tmp
    try:
        tmp_stt, extra_alvo = _aplica_speaker_alvo(tmp, speaker_alvo)
        r = _transcribe(tmp_stt, language=exp)
        src_text = (r.get("text") or "").strip()
        src_lang = (r.get("language") or "").strip().lower()
        ok, motivo = _stt_ok(r, src_text)
        if not ok:
            # não é erro: ruído/silêncio — o cliente apenas ignora e segue ouvindo
            return {"resposta": {"rejected": True, "reason": motivo, "source_text": src_text}}
        # filtro de idioma de entrada: só segue se a fala estiver no idioma escolhido
        if exp not in ("", "auto") and src_lang and src_lang != exp:
            return {"resposta": {"rejected": True,
                                 "reason": f"idioma errado (detectou {src_lang})",
                                 "source_text": src_text, "source_lang": src_lang}}
        # captura de emoção (precisa do áudio ainda em disco)
        modo = (emotion_mode or "off").lower()
        if modo in ("light", "accurate"):
            emo_label, emo_pitch, emo_speed, emo_err = _emotion_instruct(tmp_stt, src_text, modo)
    finally:
        if tmp_stt != tmp:
            tmp_stt.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)

    emotivo = bool(emo_label) and emo_label != "neutro"
    omni = _resolve_omni({})
    if design:   # voz por descrição: o instruct pedido define a voz (vence o clone/emoção)
        omni["instruct"] = des_instruct or _sanitize_instruct(_settings.get("omni_instruct") or "")
    if emotivo:
        # pitch = tag válida (nudge, mantém o clone); em voice design o pitch da emoção
        # NÃO troca a voz desenhada (mantém o instruct) — a emoção age via velocidade
        # /expressividade + tom do texto. Velocidade = time-stretch clampado 0,5–2,0.
        ep = _sanitize_instruct(emo_pitch)
        if ep and not design:
            omni["instruct"] = ep
        if emo_speed and abs(float(emo_speed) - 1.0) > 1e-3:
            base = float(omni.get("speed") or 1.0)
            omni["speed"] = round(_clamp(base * float(emo_speed), 0.5, 2.0, base), 3)
        # mais expressivo/menos monótono (guidance↓, position_temperature↑)
        omni["guidance_scale"] = round(max(0.5, float(omni.get("guidance_scale") or 2.0) - 0.4), 2)
        omni["position_temperature"] = round(min(20.0, float(omni.get("position_temperature") or 5.0) + 4.0), 1)

    emo_show = None
    if emotivo:
        bits = [b for b in (emo_pitch, f"{omni['speed']}×" if abs(float(omni.get('speed') or 1) - 1) > 1e-3 else "") if b]
        emo_show = emo_label + (f" ({', '.join(bits)})" if bits else "")

    lang = exp if exp not in ("", "auto") else (src_lang or _settings["language"])
    return {"vid": vid, "vpath": vpath, "gate": gate, "src_text": src_text,
            "src_lang": src_lang, "lang": lang, "omni": omni,
            "emotion": emo_label if emotivo else None,
            "emo_show": emo_show, "emo_err": emo_err, "alvo": extra_alvo}


@app.post("/api/translate-speech")
def translate_speech(audio: UploadFile = None, target_lang: str = Form("en"),
                     voice_id: str = Form(""), source_lang: str = Form("auto"),
                     emotion_mode: str = Form("off"), instruct: str = Form(""),
                     speaker_alvo: str = Form("")):
    """fala (áudio) -> transcreve -> traduz -> dispara TTS na voz; devolve textos + job_id.

    O áudio passa pelo mesmo teto do STT: 30 min (`TETO_STT_S`), acima disso 413."""
    if audio is None:
        raise HTTPException(400, "Áudio obrigatório")
    _jobs_capacity_check()      # antes do STT+tradução: recusa cedo, não em segundos
    tgt = (target_lang or "en").lower()
    p = _preparar_fala_para_voz(audio, voice_id, source_lang, emotion_mode, instruct,
                                "Voice design vazio — descreva a voz p/ o tradutor",
                                com_gate=True, speaker_alvo=speaker_alvo)
    if "resposta" in p:
        return p["resposta"]
    src_text, src_lang, gate = p["src_text"], p["src_lang"], p["gate"]
    # o LLM já traduz no TOM da emoção (pontuação/ênfase) -> prosódia segue o texto
    translation = _translate(src_text, tgt, p["emotion"])

    job_id = _jobs_admit({"text": translation[:200]})
    threading.Thread(
        target=_run_tts_job,
        args=(job_id, translation, p["vid"], p["vpath"], tgt, p["omni"]),
        daemon=True,
    ).start()
    return {"job_id": job_id, "source_text": src_text, "source_lang": src_lang,
            "translation": translation, "target_lang": tgt,
            "emotion": p["emo_show"], "emotion_error": p["emo_err"],
            **({"speaker": gate["speaker"]} if gate.get("speaker") else {}),
            # idem: o modo do escore vai junto (AS-norm não se compara com o cru)
            **({"speaker_escore_modo": gate["speaker_escore_modo"]}
               if gate.get("speaker_escore_modo") else {}),
            **p["alvo"]}


@app.post("/api/modify-speech")
def modify_speech(audio: UploadFile = None, voice_id: str = Form(""),
                  source_lang: str = Form("auto"), emotion_mode: str = Form("off"),
                  instruct: str = Form(""), speaker_alvo: str = Form("")):
    """MODIFICADOR: fala -> transcreve -> TTS na voz escolhida, SEM traduzir (mesma
    língua, mesmas palavras). Igual ao tradutor, mas sem o passo do LLM."""
    if audio is None:
        raise HTTPException(400, "Áudio obrigatório")
    _jobs_capacity_check()      # antes do STT: recusa cedo, não em segundos
    p = _preparar_fala_para_voz(audio, voice_id, source_lang, emotion_mode, instruct,
                                "Voice design vazio — descreva a voz",
                                speaker_alvo=speaker_alvo)
    if "resposta" in p:
        return p["resposta"]
    src_text = p["src_text"]
    out_text = src_text                    # SEM tradução: fala o que foi dito
    lang = p["lang"]

    job_id = _jobs_admit({"text": out_text[:200]})
    threading.Thread(target=_run_tts_job, args=(job_id, out_text, p["vid"], p["vpath"],
                                                lang, p["omni"]), daemon=True).start()
    return {"job_id": job_id, "source_text": src_text, "source_lang": p["src_lang"],
            "translation": out_text, "target_lang": lang,
            "emotion": p["emo_show"], "emotion_error": p["emo_err"], **p["alvo"]}


# ---------------------------------------------------------------------------
# Live (LIVE-1) — WS /api/live/ws: conversa por áudio bidirecional.
#
# Protocolo (contrato de #93 pipeline e #94 UI):
#   cliente→servidor: JSON `setup` (1º frame; voice_id/system_instruction/vad/
#     history), frames BINÁRIOS PCM16 mono 16 kHz (~100 ms), JSON `end_of_speech`,
#     `cancel`, `ping`.
#   servidor→cliente: JSON `ready`{session_id,audio{format,sr}}, `speech_start`,
#     `transcript_user`{text}, `assistant_text`{delta}, `turn_complete`{ms,
#     audio_bytes}, `interrupted`, `error`{code,message}, `prewarm`{ok},
#     `stats`{telemetria, ~4 Hz; ver #118}, `pong`; áudio = frames
#     BINÁRIOS PCM16 mono 24 kHz (formato anunciado no `ready`).
#   `ready`, `prewarm`, `stats` e `pong` são aditivos ao desenho (handshake,
#     pre-warm, telemetria e keepalive); `turno_pendente` (#126) também é:
#     fala com pipeline ocupado é ACUMULADA num pendente (teto 30 s;
#     `cancel` do cliente limpa, barge-in mantém) em vez de descartada.
# Auth: mesma política das rotas (loopback dispensa, o resto exige chave válida).
# Browser não manda header em WebSocket -> `?key=` é aceito aqui (única exceção
# ao "chave só no header"); header também vale para cliente não-browser.
# ---------------------------------------------------------------------------
# LIVE-OBS-1 (#118): telemetria da sessão. Evento `stats` (~4 Hz) e log de
# METADADOS — NUNCA áudio nem texto transcrito/gerado (invariante de privacidade).
# Contrato do evento no comentário da task #118 e no LIVE.md.
_LIVE_STATS_MS = max(0, int(os.environ.get("TTS_LIVE_STATS_MS", "250")))
_live_log = logging.getLogger("live")
if not _live_log.handlers:                    # auto-suficiente: não depende do uvicorn
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s [live] %(message)s", "%H:%M:%S"))
    _live_log.addHandler(_h)
    # propagate ligado: o `caplog` dos testes (handler no root) precisa ver; sob o
    # uvicorn o root não tem handler, então o meu handler acima é quem imprime.
    _live_log.propagate = True
_live_log.setLevel(logging.WARNING if os.environ.get("LIVE_LOG") == "0" else logging.INFO)

_provedor_estado = {"estado": "desconhecido", "http": None, "ts": 0.0}


def _provedor_marca(estado: str, http: int | None = None) -> None:
    """Estado da última chamada ao provedor de chat (do PROCESSO, não da sessão).

    É o que torna visível nas telas o 530/timeout que antes só aparecia como 502."""
    _provedor_estado.update({"estado": estado, "http": http, "ts": time.monotonic()})


def _live_log_kv(evento: str, **campos) -> None:
    """Uma linha de metadados: `evento chave=valor …` (nada de áudio/texto)."""
    _live_log.info("%s %s", evento,
                   " ".join(f"{k}={v}" for k, v in campos.items() if v is not None))


def _live_stage(sess: dict, stage: str) -> None:
    """Estágio do turno (`idle|stt|llm|tts`) com o tempo do estágio anterior."""
    if sess.get("st_stage") == stage:
        return
    ini = sess.get("st_stage_ini")
    if ini is not None:
        sess["st_stage_ms"] = int((time.monotonic() - ini) * 1000)
        _live_log_kv("stage_fim", sess=sess["id"], stage=sess.get("st_stage"),
                     ms=sess["st_stage_ms"])
    else:
        sess["st_stage_ms"] = 0
    sess["st_stage"] = stage
    sess["st_stage_ini"] = time.monotonic() if stage != "idle" else None
    if stage != "idle":
        _live_log_kv("stage_inicio", sess=sess["id"], stage=stage)


def _live_observa(sess: dict, obj) -> None:
    """Espia os eventos que SAEM para derivar telemetria (não altera nada).

    Exceção (#250): `error{code:"pipeline"}` é TERMINAL — o turno morreu e a
    sessão sobrevive (por desenho), mas só `turn_complete`/`interrupted` fechavam
    o livro-caixa: `st_stage` ficava preso em "llm"/"tts" com `st_stage_ini`
    correndo (`stats.turno.ms` crescendo até o TTL, painel anunciando "IA
    pensando" num turno morto). Volta a "idle" AQUI; o estágio onde morreu fica
    preservado no `st_erro.stage` (gravado antes do reset)."""
    if not isinstance(obj, dict):
        return
    tipo = obj.get("type")
    if tipo == "error":
        sess["st_erro"] = {"code": obj.get("code"), "stage": sess.get("st_stage"),
                           "ts": time.monotonic()}
        _live_log_kv("erro", sess=sess["id"], code=obj.get("code"),
                     stage=sess.get("st_stage"))
        if obj.get("code") == "pipeline":      # #250: turno terminal, sessão viva
            _live_stage(sess, "idle")
    elif tipo == "transcript_user":
        _live_stage(sess, "llm")
    elif tipo == "turn_complete":
        if obj.get("descartado"):
            # `motivo` do pipeline (#314): "eco" (era o próprio TTS), "vad"/"stt"
            # (fala julgada e recusada). Sem ele todo descarte era registrado como
            # "eco/curto" e uma alucinação do Whisper ficava invisível no log.
            _live_log_kv("turno_descartado", sess=sess["id"],
                         motivo=obj.get("motivo") or "eco/curto",
                         detalhe=obj.get("detalhe") or None)
            sess["descartados_motivo"] = obj.get("motivo") or "eco/curto"
        _live_stage(sess, "idle")
    elif tipo == "interrupted":
        # a chave do evento é `turn` (vem do pipeline e do `_live_cancela`); o
        # `turno` do lado de cá é o do log/`stats`. #313: ler as duas — ler só
        # `turno` fazia todo `interrupted` sair `turno=None` (achado #4 do
        # RELATORIO-app.py.md e #8 do relatório de auditoria).
        _live_log_kv("interrupted", sess=sess["id"],
                     turno=obj.get("turn", obj.get("turno")),
                     motivo=obj.get("motivo") or None)
        _live_stage(sess, "idle")
    elif tipo == "speech_start":
        # FONTE ÚNICA do número do turno (#313): quem numera é o pipeline, que é
        # quem emite `speech_start`; a sessão só adota. Antes cada lado contava
        # por conta própria e `speech_start.turn` e `interrupted.turn` vinham de
        # sequências diferentes — o cliente não conseguia correlacionar.
        try:
            n = int(obj.get("turn"))
        except (TypeError, ValueError):
            n = None
        if n is not None:
            sess["turno"] = n
    elif tipo in ("latency", "turn_complete") and obj.get("separacao"):
        # separação do turno (#533): o `stats` é o instrumento do aceite; o
        # caminho do TURNO não tem sessão no pipeline, então os campos vêm no
        # evento e são copiados aqui
        st = sess.setdefault("voz_stats", {"recusados": 0})
        st["separacao"] = obj.get("separacao")
        for k in ("separacao_motivo", "separacao_ms", "segmentos_descartados"):
            if obj.get(k) is not None:
                st[k] = obj[k]
    elif tipo == "latency":
        # espera pelo `_gen_lock` medida no pipeline (#313); o TTS pode estar
        # atrás de um job de lote, e sem este campo o tempo some dentro do
        # `first_chunk_ms`, indistinguível de TTS lento.
        espera = obj.get("lock_espera_ms")
        if espera is not None:
            sess["st_lock_espera_ms"] = int(espera)
        # #315: fecha a conta do "fim-de-fala → 1º áudio". O `latency` do pipeline
        # conta de outro zero (o início da thread), então o trecho entre a DECISÃO
        # do VAD e o turno abrir entra por aqui: `decisao_ms` é a latência do
        # detector e `decisao_para_audio_ms` soma essa espera ao 1º áudio. Sem os
        # dois, o alvo do épico (1,5 s) não é auditável com o dado de produção.
        decisao, abre = sess.get("t_decisao"), sess.get("t_abre_turno")
        if decisao is not None:
            obj["decisao_ms"] = int(sess.get("t_decisao_ms") or 0)
        if decisao is not None and abre is not None and obj.get("first_audio_ms") is not None:
            obj["decisao_para_audio_ms"] = int(obj["first_audio_ms"]) + max(
                0, int(round((abre - decisao) * 1000)))
        sess["t_decisao"] = sess["t_abre_turno"] = None


def _live_marca_prov(sess: dict, prob: float) -> None:
    if prob:
        sess["st_prob"] = round(float(prob), 3)


def _live_estado_motor(sess: dict) -> str:
    """Estado para a TELA (deriva o que a FSM não precisa nomear)."""
    if sess.get("falando"):
        return "falando"
    if sess.get("st_stage") == "stt":
        return "fechando"
    eng = sess.get("engine")
    if eng is not None:
        if getattr(eng, "turno_aberto", False):
            return "ouvindo"
        return getattr(getattr(eng, "estado", None), "value", "ocioso")
    return "ouvindo" if sess.get("turno_aberto") else "ocioso"


def _live_stats_ia(sess: dict) -> dict:
    """Backend de IA efetivo da sessão Live (`stats.ia`) — para o painel OBS.

    `pedido` é o que está configurado; `backend` é o que de FATO responde o turno.
    Com `chat_backend=dsh` e o handshake do harness morrendo, o pipeline marca a
    sessão e passa a usar o openai (#146): sem este campo o painel mostraria "dsh"
    enquanto o texto vinha do endpoint.

    `backend` olha o CLIENTE da sessão, não o campo (#196): o `DshClient` nasce no
    connect e trocar o seletor com a sessão aberta NÃO reconstrói o pipe — seguir o
    configurado ali anunciava dsh com o texto vindo do endpoint (mesma classe do
    falso-verde do #151). Pipe sem `_dsh` (sessão ainda nascendo, stub de teste)
    segue o configurado."""
    pedido = _chat_backend_live()
    pipe = sess.get("pipe")
    caiu = bool(getattr(pipe, "_dsh_indisponivel", False))
    if pipe is None or not hasattr(pipe, "_dsh"):
        tem_dsh = pedido == "dsh"
    else:
        tem_dsh = pipe._dsh is not None
    estouros = int(getattr(pipe, "_dsh_estouros", 0) or 0)
    bridge = getattr(pipe, "bridge_estado", None)
    return {"pedido": pedido, "backend": "openai" if (caiu or not tem_dsh) else "dsh",
            "fallback": caiu, "motivo": str(getattr(pipe, "_dsh_motivo", "") or "")[:200],
            # estouro de RELÓGIO é diagnóstico separado de queda (#287): o turno caiu
            # no openai, mas o dsh continua vivo e volta a ser tentado
            "teto_estouro": estouros,
            # bridge `clean` explica um turno que caiu no openai sem o dsh estar
            # morto: sem o patch não há delta a esperar (#287)
            "bridge": bridge}


def _live_fila_itens(sess: dict) -> int:
    """Itens na fila de envio — tolerante ao dublê de teste (só tem `put`)."""
    fila = sess.get("fila")
    qsize = getattr(fila, "qsize", None)
    try:
        return int(qsize()) if qsize is not None else 0
    except Exception:                     # noqa: BLE001 — telemetria não derruba
        return 0


def _live_stats_rnnoise(sess: dict) -> dict:
    """Estado do filtro RNNoise — a degradação dele é INVISÍVEL sem isto (#313).

    `faltas` são BYTES que o filtro não entregou a tempo e que foram completados
    com zero: é buraco de áudio dentro do que o VAD lê. O contador é guardado na
    sessão porque o filtro é REMOVIDO quando cai ou atrasa (`_live_rnnoise_derruba`)
    e o número não pode sumir junto — `ativo: false` + `faltas` > 0 é o retrato da
    queda. `faltas_pct` é a fração do áudio do mic que virou silêncio."""
    ns = sess.get("rnnoise")
    if ns is not None:
        sess["rnnoise_faltas"] = int(getattr(ns, "faltas", 0) or 0)
    faltas = int(sess.get("rnnoise_faltas", 0) or 0)
    bytes_in = int(sess.get("st_bytes", 0) or 0)
    return {"ativo": ns is not None and bool(getattr(ns, "ativo", False)),
            "faltas": faltas,
            "faltas_pct": (round(100.0 * faltas / bytes_in, 2) if bytes_in else 0.0)}


def _live_stats(sess: dict) -> dict:
    """Payload do evento `stats` (ver contrato na task #118 / LIVE.md)."""
    agora = time.monotonic()
    ultimo = sess.get("st_ultimo_frame")
    eng = sess.get("engine")
    limiares = {}
    if eng is not None:
        try:
            limiares = {"limiar_dbfs": round(eng.limiar_energia_dbfs, 1),
                        "limiar_turno_dbfs": round(eng.limiar_energia_turno_dbfs, 1)}
            st = eng.estatisticas()
            # `barge_ativos` soma as DUAS portas do barge (turno novo e turno do
            # usuário já aberto): contar só a primeira deixava o número em 0
            # justamente no regime em que ele é a medida (mic ouvindo o próprio
            # TTS, #216). `barge_falsos` lia a chave errada (`barge_falso`) e saía
            # sempre 0 — a chave do motor é `barge_in_falso`.
            limiares.update({"barge_ativos": st.get("barge_in", 0)
                             + st.get("barge_in_com_turno_aberto", 0),
                             "barge_falsos": st.get("barge_in_falso", 0),
                             # #707: burst máximo do episódio (de `barge_in_ms`).
                             # Abaixo do alvo = a fala não sustentou; no alvo sem
                             # barge = outra coisa. Sem publicar, o campo não chega
                             # ao painel/harness e a medida vira adivinhação.
                             "barge_pico": st.get("barge_pico", 0),
                             "barge_alvo": getattr(
                                 getattr(eng, "config", None), "_frames_barge", 0)})
            # #313: o estado que EXPLICA um barge falso sai do motor (já calculado,
            # custo zero). Sem estes quatro o painel só mostra o sintoma.
            limiares.update({
                "eco_ausente": st.get("eco_ausente"),
                "tocando": st.get("tocando"),
                "turno_assistente_aberto": st.get("turno_assistente_aberto"),
                "taxa_falso_barge_in": st.get("taxa_falso_barge_in"),
                "eco_dbfs": st.get("eco_dbfs"), "noise_dbfs": st.get("noise_dbfs"),
                # #314: o fallback "segurar pra falar" era calculado e morria aqui —
                # `hold_to_talk_sugerido` é o aviso que a UI precisa receber e
                # `hold_to_talk` diz se o modo já está ligado (comando `hold`).
                "hold_to_talk": st.get("hold_to_talk"),
                "hold_to_talk_sugerido": st.get("hold_to_talk_sugerido"),
            })
        except Exception:                     # noqa: BLE001 — telemetria não derruba
            limiares = {}
    erro = sess.get("st_erro")
    ini = sess.get("st_stage_ini")
    dados = {
        "type": "stats", "t_ms": int((agora - sess["t0"]) * 1000),
        # #478: `None` = negociado ou cliente sem header (normal); texto = recusa
        "subprotocolo": sess.get("subprotocolo_recusado"),
        "mic": {"frames": sess.get("st_frames", 0), "bytes": sess.get("st_bytes", 0),
                "desde_ultimo_ms": (int((agora - ultimo) * 1000) if ultimo else None),
                "dbfs": sess.get("st_dbfs"), "prob": sess.get("st_prob")},
        "motor": {"estado": _live_estado_motor(sess), **limiares,
                  # #501: desfecho da interrupção inteligente e p50 do filtro
                  # `modelo` (timeouts contados à parte — são o sinal de que o
                  # classificador não está dando conta da máquina)
                  "barge": _live_stats_barge(sess),
                  # #533: veredito do portão "só a minha voz" e recusas contadas
                  "voz": sess.get("voz_stats") or {}},
        "rnnoise": _live_stats_rnnoise(sess),
        "turno": {"stage": sess.get("st_stage", "idle"),
                  "ms": (int((agora - ini) * 1000) if ini else 0),
                  "n": sess.get("turno", 0), "buffer_bytes": sess.get("buffer_bytes_turno", 0),
                  "t_decisao_ms": sess.get("t_decisao_ms", 0),
                  # #313: tempo esperando o `_gen_lock` (job de lote na frente).
                  # `None` = nenhuma geração medida ainda nesta sessão.
                  "lock_espera_ms": sess.get("st_lock_espera_ms"),
                  "pendente": bool(sess.get("turno_pendente")),
                  "pendentes_trechos": sess.get("pendentes_trechos", 0),
                  "pendentes_descartados_ms": sess.get("pendentes_descartados_ms", 0),
                  "pendentes_descartados": sess.get("pendentes_descartados", 0)},
        # `fila_*` é o BACKLOG real do envio (teto em `_LIVE_FILA_MAX_BYTES`): é o
        # número que mostra cliente lento e, em 0, o playback em dia. Sessões de
        # dublê podem não ter fila — daí o `getattr`, e `qsize` só com fila.
        "playback": {"speaking": bool(sess.get("falando")),
                     "chunks": sess.get("st_chunks", 0), "bytes": sess.get("st_audio_bytes", 0),
                     "fila_bytes": getattr(sess.get("fila"), "bytes", 0),
                     "fila_itens": _live_fila_itens(sess),
                     "fila_descartados": getattr(sess.get("fila"), "descartados", 0),
                     "underruns": sess.get("st_underruns", 0),
                     # #547: é a ENTRADA da regra do `fim_de_resposta` (o que o
                     # motor acha que o cliente ainda vai tocar). Sem ele no stats a
                     # decisão "cortar x anotar" não se explica por fora.
                     "restante_ms": (sess["engine"].playback_restante_ms()
                                     if sess.get("engine") is not None else None),
                     # a JANELA é o que ARMA o barge: com ela fechada o onset vira
                     # turno novo mesmo com backlog — é o par do `restante_ms`
                     "janela": (sess["engine"].estatisticas().get("playback_ativo")
                                if sess.get("engine") is not None else None)},
        "sessao": {"idade_s": int(time.time() - sess["criada"]),
                   "ocioso_ms": int((agora - sess["visto"]) * 1000),
                   "ttl_s": _LIVE_TTL_S, "criadas": len(_live_sessions),
                   "historicos": len(_live_historico)},
        # #146: o painel OBS não pode mentir sobre quem respondeu. `pedido` é o
        # backend escolhido (settings/env); `backend` é o EFEITO real — com o dsh
        # caído no handshake a sessão segue no openai e `fallback` fica true.
        "ia": _live_stats_ia(sess),
    }
    if erro:
        dados["erro"] = {"code": erro.get("code"), "stage": erro.get("stage"),
                         "idade_ms": int((agora - erro["ts"]) * 1000)}
    if _provedor_estado["ts"]:
        dados["provedor"] = {"estado": _provedor_estado["estado"],
                             "http": _provedor_estado["http"],
                             "idade_ms": int((agora - _provedor_estado["ts"]) * 1000)}
    return dados


_LIVE_MAX_SESSIONS = max(1, int(os.environ.get("TTS_LIVE_MAX_SESSIONS", "4")))
_LIVE_TTL_S = max(30, int(os.environ.get("TTS_LIVE_TTL_S", "300")))
_LIVE_SETUP_TIMEOUT_S = 10.0
#: Teto do FECHAMENTO da sessão (#798). `pipe.close()`/`dsh.close()` podem esperar
#: até 65 s; aqui é o event loop — estourou, a thread segue e a sessão sai do
#: registry do mesmo jeito (nada fica preso no loop).
_LIVE_FECHAMENTO_TIMEOUT_S = 10.0
#: Executor PRÓPRIO do fechamento (#798): `to_thread` usa o default do loop e um
#: `close()` pendurado seguraria vaga por até 65 s, atrasando até o `pega()` do
#: classificador. Vagas = teto de sessões: `wait_for` CANCELA o future no prazo e o
#: `_WorkItem.run` do CPython começa com `if self.fut.cancelled(): return` — item que
#: ainda estava na FILA nunca roda e o `close()` some calado. Com uma vaga por
#: sessão, todo fechamento ao menos COMEÇA.
_LIVE_FECHAMENTO_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=max(1, _LIVE_MAX_SESSIONS), thread_name_prefix="live-fecha")
_LIVE_MAX_BUFFER = 2 * 1024 * 1024      # PCM16 16k do turno em curso (~1 min de fala)
# #126: teto do pendente (fala acumulada com o pipeline ocupado). PCM16 mono
# 16 kHz = 32 kB/s; 30 s ~ 960 kB. Estourou, sai o trecho MAIS ANTIGO.
_LIVE_PENDENTE_MAX_S = max(1, int(os.environ.get("TTS_LIVE_PENDENTE_MAX_S", "30")))
_LIVE_PENDENTE_MAX_BYTES = _LIVE_PENDENTE_MAX_S * 32000
_LIVE_MAX_HISTORY = 200
# #313: teto de CHARS do histórico recebido no `setup` (ver `_live_valida_setup`).
_LIVE_SETUP_HIST_MAX_CHARS = max(1000, int(os.environ.get(
    "TTS_LIVE_SETUP_HIST_MAX_CHARS", "12000")))
# #313: janela de confirmação do barge (`vad.barge_ms`), no MESMO porte da medição
# do motor (`live_turns.Config.barge_in_ms`, default 300): abaixo de 150 a energia
# sustentada não existe e o eco volta a disparar; acima de 600 a interrupção late.
_LIVE_BARGE_MS_MIN = 150
_LIVE_BARGE_MS_MAX = 600
_LIVE_BARGE_MS_PADRAO = 300
# #315: quanto a PRIMEIRA fala espera o prewarm antes de o turno abrir mesmo frio.
# 2 s cobre a cauda de um prewarm em curso sem transformar a espera em espera longa
# (a frio o 1º áudio passa de 7 s); `0` restaura o comportamento antigo.
_LIVE_ESPERA_PREWARM_S = max(0.0, float(os.environ.get("TTS_LIVE_ESPERA_PREWARM_S", "2")))
_LIVE_AUDIO_SR = 24000                  # o que o servidor ENVIA (o cliente manda 16k)
_LIVE_TICK_S = 0.2                      # acorda a task de envio p/ checar TTL/fechamento
_live_lock = threading.Lock()
_live_sessions: dict = {}
_live_sweeper_iniciado = False


def _live_erro(codigo: str, mensagem: str) -> dict:
    return {"type": "error", "code": codigo, "message": str(mensagem)[:300]}


# Ticket efêmero (LIVE-1.5): browser não manda header em WebSocket, e a chave em
# query string vaza em access log do uvicorn, log de proxy/túnel e histórico do
# navegador. Então o cliente pede um ticket por HTTP (`POST /api/live/ticket`, auth
# normal no header), troca por `?ticket=` no WS e ele morre no handshake: UM uso,
# 60 s. `?key=` continua aceito (compat) e loopback segue dispensando tudo — em
# loopback o handshake nem chega a consumir o ticket (auth dispensada), então um
# smoke que testa "reuso recusado" precisa conectar pelo IP da LAN, não por 127.0.0.1.
_LIVE_TICKET_TTL_S = 60
_LIVE_TICKET_MAX = 512                  # tickets pendentes (memória limitada)
_live_tickets: dict = {}                # ticket -> expira em (monotonic)


def _live_ticket_limpa(agora: float | None = None) -> None:
    agora = agora if agora is not None else time.monotonic()
    for t, expira in list(_live_tickets.items()):
        if expira <= agora:
            _live_tickets.pop(t, None)


def _live_ticket_emite() -> str:
    with _live_lock:
        _live_ticket_limpa()
        sobra = len(_live_tickets) - _LIVE_TICKET_MAX + 1
        if sobra > 0:                    # os mais antigos saem primeiro
            for t in sorted(_live_tickets, key=_live_tickets.get)[:sobra]:
                _live_tickets.pop(t, None)
        ticket = _secrets.token_urlsafe(24)
        _live_tickets[ticket] = time.monotonic() + _LIVE_TICKET_TTL_S
    return ticket


def _live_ticket_consome(ticket: str) -> bool:
    """UM uso: o ticket SOME de vez quando é consumido (inclusive se a sessão cair).

    Ticket inválido/expirado/desconhecido também é descartado (não volta a valer),
    mas quem decide se ele AUTENTICA é o prazo: `_secrets_iguais`-style, aqui por
    comparação direta do vencimento."""
    if not ticket:
        return False
    with _live_lock:
        expira = _live_tickets.pop(ticket, None)
    return expira is not None and expira > time.monotonic()


def _live_ticket_vale(ticket: str) -> bool:
    """O ticket existe e está no prazo — SEM consumir (para tentar a chave antes
    de gastar o ticket, se o chamador quiser)."""
    if not ticket:
        return False
    with _live_lock:
        expira = _live_tickets.get(ticket)
    return expira is not None and expira > time.monotonic()


# Versão do protocolo do WS do Live (#478). O subprotocolo é NEGOCIADO, não
# exigido: cliente que não manda `Sec-WebSocket-Protocol` continua igual, e é o
# caminho antigo (`client/`, smokes) que a compatibilidade precisa preservar.
_LIVE_SUBPROTOCOLO = "tts-live.v1"


def _live_negocia_subprotocolo(ws) -> tuple[str | None, str | None]:
    """Devolve `(aceito, motivo_de_recusa)` para o `Sec-WebSocket-Protocol`.

    `aceito` vai no `accept(subprotocol=…)`: `None` sem header (comportamento de
    hoje). Se o cliente pedir uma versão que não existe, `motivo` explica no
    `stats` — em vez de o handshake falhar com o cliente sem saber por quê.
    """
    cru = (ws.headers.get("sec-websocket-protocol") or "").strip()
    if not cru:
        return None, None
    pedidos = [p.strip() for p in cru.split(",") if p.strip()]
    if _LIVE_SUBPROTOCOLO in pedidos:
        return _LIVE_SUBPROTOCOLO, None
    return None, f"subprotocolo pedido sem versão conhecida: {', '.join(pedidos)}"


def _live_autentica(ws) -> bool:
    """MESMA política das rotas: loopback dispensa; o resto precisa de credencial.

    Ordem: `?ticket=` (efêmero, de um uso — o caminho para o browser) → `?key=`
    (compat) → header (cliente não-browser, ex.: script/RN).

    O ticket não é o ÚLTIMO recurso: se ele não valer (usado/expirado/inventado),
    a checagem SEGUE para a chave/header (#212) — antes o `return` cortava ali e um
    cliente com credencial boa levava 4401 só porque repetiu o parâmetro. O ticket
    tentado continua sendo consumido: um uso segue um uso."""
    # Origem de TERCEIRO não herda a isenção do loopback (#781): o navegador não
    # aplica CORS a WebSocket, então quem confere é aqui — sem isto qualquer site
    # aberto no Mac abre sessão Live e fala com a voz do dono.
    estrangeira = _origem_estrangeira(ws)
    if _is_local(ws) and not estrangeira:
        return True
    if not _auth_enabled():
        return True
    _live_ticket_limpa()               # varre por TEMPO, não só na emissão
    ticket = (ws.query_params.get("ticket") or "").strip()
    if ticket and _live_ticket_consome(ticket):
        return True
    chave_query = (ws.query_params.get("key") or "").strip()
    chave = chave_query or _extract_request_key(ws)
    ok = _key_is_valid(chave)
    if not ok and estrangeira:
        _marca_origem_barrada(ws)
    if ok and chave_query:
        # chave em query vai para access log do uvicorn/proxy: o ticket existe para
        # isso, então a queda para `?key=` fica REGISTRADA em vez de invisível (#282)
        print("[live] auth por ?key= (chave em query string; prefira o ticket)",
              file=sys.stderr, flush=True)
    return ok


@app.post("/api/live/ticket")
def live_ticket():
    """Emite um ticket de um uso para o WS (60 s). Auth normal (header).

    O cliente chama isto, conecta em `/api/live/ws?ticket=<ticket>` e a chave de
    API deixa de aparecer em log/histórico. Loopback pode pular o ticket."""
    return {"ticket": _live_ticket_emite(), "expires_in": _LIVE_TICKET_TTL_S}


def _live_clamp_ms(valor, nome: str, lo: int, hi: int, default: int) -> int:
    if valor is None:
        return default
    try:
        n = int(valor)
    except (TypeError, ValueError):
        raise ValueError(f"vad.{nome} precisa ser inteiro (ms)") from None
    return int(min(hi, max(lo, n)))


def _live_valida_setup(msg: dict) -> dict:
    """Valida o `setup` com o rigor dos endpoints HTTP (#23/#33): nada entra por
    confiança, o erro diz o CAMPO e voz desconhecida cai no `_resolve_voice`."""
    if not isinstance(msg, dict):
        raise ValueError("setup precisa ser um objeto JSON")
    voz_pedida = str(msg.get("voice_id") or "").strip()
    voz = voz_pedida
    if not voz:
        voz = _resolve_voice(None)
    elif voz not in (DESIGN_VOICE_ID,) and voz not in OMNI_PRESETS and not _voice_path(voz):
        voz = _resolve_voice(voz)
    sid = str(msg.get("session_id") or "").strip()
    if sid and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", sid):
        raise ValueError("session_id inválido (letras, números, _ e -)")
    system = str(msg.get("system_instruction") or "").strip()[:4000]
    vad = msg.get("vad") or {}
    if not isinstance(vad, dict):
        raise ValueError("vad precisa ser um objeto JSON")
    hist = msg.get("history") or []
    if not isinstance(hist, list):
        raise ValueError("history precisa ser uma lista")
    history = []
    for i, h in enumerate(hist[:_LIVE_MAX_HISTORY]):
        if not isinstance(h, dict):
            raise ValueError(f"history[{i}] precisa ser um objeto")
        papel = str(h.get("role") or "").strip().lower()
        if papel not in ("user", "assistant"):
            raise ValueError(f"history[{i}].role precisa ser 'user' ou 'assistant'")
        texto = str(h.get("text") or "").strip()[:4000]
        if texto:
            history.append({"role": papel, "text": texto})
    # Teto de CHARS do histórico do `setup` (#313 / auditoria #283 item 10): 200
    # mensagens de 4000 chars são ~800 kB e iam INTEIRAS ao prompt de TODO turno no
    # caminho openai — o teto de 12 kB só existia na compressão, que roda depois e
    # corta pela metade. Estourou, saem as mensagens mais ANTIGAS (o contexto
    # recente é o que importa) e o quanto saiu vai no `ready`.
    mantido, chars, cortados = [], 0, 0
    for h in reversed(history):
        if chars + len(h["text"]) > _LIVE_SETUP_HIST_MAX_CHARS and mantido:
            cortados += 1
            continue
        mantido.append(h)
        chars += len(h["text"])
    history = list(reversed(mantido))
    return {
        "voice_id": voz,
        "voice_id_pedido": voz_pedida,
        "session_id": sid,
        "system": system,
        "vad": {"silence_ms": _live_clamp_ms(vad.get("silence_ms"), "silence_ms", 100, 5000, 600),
                "prefix_ms": _live_clamp_ms(vad.get("prefix_ms"), "prefix_ms", 0, 2000, 100),
                # #313: o custo de confirmação do barge é fixo (~barge_ms + rampa do
                # envelope) e o cliente não tinha como pedir outro valor.
                "barge_ms": _live_clamp_ms(vad.get("barge_ms"), "barge_ms",
                                           _LIVE_BARGE_MS_MIN, _LIVE_BARGE_MS_MAX,
                                           _LIVE_BARGE_MS_PADRAO)},
        "history": history,
        "history_descartados": cortados,
    }


def _live_sweep() -> list:
    """Fecha sessões ociosas (TTL) e varre o histórico de retomada. A marcação é vista pela task de envio, que é
    quem pode fechar o WS de forma assíncrona."""
    _live_hist_varre()
    agora = time.monotonic()
    with _live_lock:
        vencidas = [s for s, d in _live_sessions.items()
                    if agora - d["visto"] > _LIVE_TTL_S]
        for s in vencidas:
            _live_sessions[s]["vencida"] = True
    return vencidas


def _live_sweeper():
    while True:
        time.sleep(15)
        try:
            _live_sweep()
        except Exception:  # noqa: BLE001 — thread de fundo nunca pode morrer
            pass


def _live_enriquece(sess: dict, obj):
    """Estampa o ESTADO da sessão nos eventos que saem.

    `truncated` no `turn_complete`: o áudio daquele turno bateu no teto do buffer
    e o começo foi mantido (o resto é descartado). O `buffer_bytes` em si é do
    pipeline; até ele emitir, o estado da sessão vai por aqui — e o aviso é
    zerado depois de reportado, para valer por turno.

    `turn_complete`/`interrupted`/`error` também ACORDAM o observador do turno
    pendente (#126): o fechamento — mesmo em erro — é a janela em que a fala
    enfileirada pode abrir, e ela NÃO herda o erro do turno anterior."""
    if isinstance(obj, dict) and obj.get("type") in ("turn_complete", "interrupted",
                                                     "error"):
        if obj.get("type") == "turn_complete":
            if obj.get("descartado"):         # decisão do pipeline (eco x humano)
                sess["descartados"] = sess.get("descartados", 0) + 1
            obj.setdefault("truncated", bool(sess.get("truncado")))
            sess["truncado"] = False          # aviso vale por turno
            sess["buffer"] = bytearray()      # turno fechado: o áudio já foi consumido
            sess["buffer_consumido"] = 0      # (e o marcador volta a zero com ele)
            try:                              # histórico fora da conexão + compressão
                _live_hist_pos_turno(sess)
            except Exception:                 # noqa: BLE001 — nunca derruba o envio
                pass
        if sess.get("turno_pendente"):    # #126: turno fechou (ou ERROU) — o
            _live_acorda_pendente(sess)   # pendente segue e abre quando liberar
    return obj


def _live_janela_turno(sess: dict, aberto: bool) -> None:
    """#167: liga/desliga o TURNO do assistente no motor de turnos.

    A janela de playback do motor (`live_turns`) era dimensionada pela FILA de
    chunks: com um chunk por vez ela fechava `playback_janela_ms` (900 ms) depois do
    último ENVIO, e o onset do humano num VÃO de geração (LLM/TTS; medido até 20 s)
    deixava de ser lido como interrupção — virava turno novo. Aqui a marca segue o
    TURNO: abre no 1º áudio e fecha no evento terminal
    (`turn_complete`/`interrupted`). `set_turno_aberto` é additive no motor: sem
    este call site nada muda (e `TTS_LIVE_BARGE_JANELA_TURNO=0` desliga para A/B)."""
    eng = sess.get("engine")
    fn = getattr(eng, "set_turno_aberto", None)
    if fn is None:
        return
    try:
        fn(bool(aberto))
    except Exception:                    # noqa: BLE001 — motor não pode derrubar o WS
        pass


# Teto da FILA de envio, em BYTES de payload (0 = sem teto, como antes). O áudio do
# TTS sai a 48 kB/s (24 kHz PCM16): 2 MiB ~ 43 s de backlog, o mesmo porte do teto
# do buffer do mic (`_LIVE_MAX_BUFFER`). Passou disso o cliente já não alcança o
# playback ao vivo; e sem teto a fila cresce SEM VOLTA quando ninguém drena — que é
# exatamente o que acontece quando a task de envio morre (ver `_live_sender`).
_LIVE_FILA_MAX_BYTES = max(0, int(os.environ.get("TTS_LIVE_FILA_MAX_BYTES",
                                                str(2 * 1024 * 1024))))


class _LiveFila:
    """Fila da sessão com teto em BYTES — sai sempre o áudio mais ANTIGO.

    `queue.Queue(maxsize=n)` conta ITENS, e quem estoura a memória é o BYTE: 4
    chunks de 2 s valem 400 kB e 400 chunks de 20 ms valem os mesmos 400 kB. Daí o
    contador e o descarte por BYTE. Só o ÁUDIO é derrubado — é o único tipo cuja
    perda vira uma emenda no alto-falante e playback atrasado é justamente o caso;
    JSON (`stats`, `turn_complete`, `error`, `transcript_user`) nunca sai, porque
    tirá-lo desalinharia o cliente e ele custa pouco. Restou só JSON acima do teto,
    a fila passa do teto em alguns kB, sem laço.

    `put` devolve quantos itens de áudio antigos o teto derrubou (o chamador ajusta
    o `audio_pendente`); `queue.Queue` puro devolve `None`, então o mesmo call site
    serve os dublês de teste.
    """

    def __init__(self, teto_bytes: int = 0):
        self.teto = max(0, int(teto_bytes))
        self.descartados = 0
        self._q: queue.Queue = queue.Queue()
        self._trava = threading.Lock()   # só o contador: o `get` BLOQUEIA fora dela
        self._bytes = 0

    # -- leitura (o `stats` e os testes leem) --------------------------------
    @property
    def bytes(self) -> int:
        return self._bytes

    def qsize(self) -> int:
        return self._q.qsize()

    def empty(self) -> bool:
        return self._q.empty()

    @staticmethod
    def _peso(item) -> int:
        """Bytes que o item ocupa na fila (só áudio conta)."""
        try:
            return len(item[1]) if item is not None and item[0] == "audio" else 0
        except (TypeError, IndexError):
            return 0

    # -- escrita / consumo ---------------------------------------------------
    def put(self, item) -> int:
        """Enfileira e, se o teto estourou, derruba o áudio mais ANTIGO.

        A ordem é enfileirar-e-depois-medir: o item já está na fila quando conta,
        então contador e fila nunca divergem (e um único item maior que o teto
        permanece, em vez de a fila ficar com ele num limbo fora da conta).
        """
        self._q.put(item)
        peso = self._peso(item)
        if not peso:
            return 0
        derrubados = 0
        with self._trava:
            self._bytes += peso
            if self.teto and self._bytes > self.teto:
                derrubados = self._descartar_ate_caber(item)
        return derrubados

    def get(self, block: bool = True, timeout: float | None = None):
        item = self._q.get(block, timeout)
        peso = self._peso(item)
        if peso:                          # fora da trava: o item já saiu da fila
            with self._trava:
                self._bytes -= peso
        return item

    def _descartar_ate_caber(self, protegido=None) -> int:
        """Derruba o áudio mais antigo até caber. `_trava` já tomada."""
        n = 0
        while self.teto and self._bytes > self.teto:
            alvo = self._audio_mais_antigo(protegido)
            if alvo is None:
                break                     # só restou JSON/ele mesmo: não vale derrubar
            self._bytes -= self._peso(alvo)
            self.descartados += 1
            n += 1
        return n

    def _audio_mais_antigo(self, protegido=None):
        """Primeiro item de áudio na ordem de saída (a `deque` do Queue).

        `protegido` é o que acabou de entrar: se ele é o único, não sai."""
        for item in self._q.queue:
            if item is not None and item[0] == "audio" and item is not protegido:
                self._q.queue.remove(item)
                return item
        return None


def _live_envia_json(sess: dict, obj: dict) -> None:
    """Ponto de acoplamento do pipeline (#93): pode ser chamado de QUALQUER thread."""
    terminal = obj.get("type") in ("turn_complete", "interrupted") or (
        obj.get("type") == "error" and obj.get("code") == "pipeline")
    if terminal:
        # #167: turno acabou, fecha a janela. #250: `error{pipeline}` também —
        # turno que morreu DEPOIS de mandar áudio deixava a janela aberta até a
        # trava de frames e a fala seguinte era lida como barge de um turno que
        # não existe. Erros que NÃO fecham turno (`turno_em_curso`, `sem_audio`,
        # `busy`, `json`, `comando`) ficam de fora.
        _live_janela_turno(sess, False)
    _live_observa(sess, obj)
    sess["fila"].put(("json", _live_enriquece(sess, obj)))


def _live_conta_underrun(sess: dict, nbytes: int) -> None:
    """#492: incrementa `st_underruns` quando o vão entre dois chunks passa do
    próprio tamanho do anterior (+250 ms) — buffer de playback vazio no cliente.
    Quem demora porque o TTS é lento enche a fila, então não conta.
    Duração em 24 kHz mono 16 bits: 48 bytes/ms.
    """
    agora = time.monotonic()
    sess["st_underruns"] = sess.get("st_underruns", 0)
    anterior = sess.get("st_ultimo_chunk")
    if anterior is not None and (agora - anterior) * 1000 > sess.get("st_ultimo_dur_ms", 0) + 250:
        sess["st_underruns"] += 1
    sess["st_ultimo_chunk"] = agora
    sess["st_ultimo_dur_ms"] = nbytes / 48.0


# Quente ~90 ms (#908). 1,5 s de folga: filho mudo no Live não congela o
# turno os 60 s do job. Estouro devolve o PCM cru. Filho ainda carregando
# manda `subindo`; essa espera não é este prazo (teto `CARGA_S` no pai).
_LIVE_MARCA_PRAZO_S = 1.5


def _marca_pcm(pcm: bytes, sr: int = 16000) -> bytes:
    """Devolve o PCM marcado (AudioSeal), ou o MESMO pcm se a marca estiver fora.

    `sr` é a taxa REAL das samples. O Live passa `_LIVE_AUDIO_SR` (24 kHz): o
    cliente toca esse PCM. O worker desce para a grade de 16 kHz do AudioSeal,
    marca, e devolve a mesma taxa e o mesmo número de samples (#908). Gravado
    com cabeçalho 16 kHz, a conferência passa e a marca some no playback.

    Nunca levanta: marca é higiene anti-clone, não pode derrubar geração. O aviso
    de indisponibilidade sai uma vez por processo (`marca_dagua.motivo`).

    O prazo desta leitura é `_LIVE_MARCA_PRAZO_S`, não os 60 s do job. Se o
    filho não responde, o trecho sai cru e o log traz esse prazo."""
    if not pcm or not _settings.get("tts_marca_dagua"):
        return pcm
    import marca_dagua

    try:
        if not marca_dagua.disponivel():
            _avisa_sem_marca(marca_dagua.motivo())
            return pcm
        import numpy as np
        import soundfile as sf

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
            caminho = Path(fh.name)
        try:
            x = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
            sf.write(str(caminho), x, sr, subtype="PCM_16")
            # Fora do `_gen_lock` (#915), com o prazo curto do Live (1,5 s).
            # A taxa e o tamanho ficam os do PCM de entrada: o worker desce a
            # 16 kHz e volta (#908).
            _corre_sem_gen_lock(lambda: marca_dagua.marca_wav(
                caminho, prazo_s=_LIVE_MARCA_PRAZO_S))
            y = np.asarray(sf.read(str(caminho), dtype="int16")[0], dtype="<i2").reshape(-1)
            n = len(pcm) // 2
            # O contrato do Live é 48 bytes/ms a 24 kHz. A reamostra não pode
            # mudar o tamanho que o cliente já espera.
            if y.size != n:
                if y.size > n:
                    y = y[:n]
                else:
                    y = np.pad(y, (0, n - y.size))
            return np.ascontiguousarray(y, dtype="<i2").tobytes()
        finally:
            caminho.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001 — sem marca o áudio segue
        _avisa_sem_marca(str(exc)[:120])
        return pcm


_marca_avisada = False


def _avisa_sem_marca(motivo: str) -> None:
    """Uma linha por processo: marca fora não vira spam no stderr."""
    global _marca_avisada
    if _marca_avisada:
        return
    _marca_avisada = True
    print(f"[marca] áudio saindo SEM marca d'água: {motivo}", file=sys.stderr, flush=True)


def _live_marca_este_pcm(primeiro: bool) -> bool:
    """True = este PCM passa no AudioSeal antes de ir ao cliente.

    Default (chave ausente ou True) marca também o 1º PCM do turno: a decisão
    do dono de 2026-10-28 é marca em toda saída do TTS. `False` é o atalho do
    #901 e só tira a marca desse primeiro PCM."""
    if not primeiro:
        return True
    return bool(_settings.get("live_marca_primeiro_trecho", True))


def _live_envia_audio(sess: dict, pcm: bytes, *, marcar: bool = True) -> None:
    """Enfileira o PCM do Live. `marcar=False` só quando
    `live_marca_primeiro_trecho` está desligada no 1º PCM do turno (#901)."""
    if pcm:
        if marcar:
            pcm = _marca_pcm(pcm, _LIVE_AUDIO_SR)  # 24 kHz, grade 16 kHz por dentro (#908)
        _live_janela_turno(sess, True)   # #167: turno do assistente com áudio em voo
        sess["audio_pendente"] = sess.get("audio_pendente", 0) + 1
        # #877: contador MONOTÔNICO de áudio da resposta — a continuação só vale
        # enquanto o turno em curso não mandou nenhum chunk (depois disso é barge)
        sess["audio_enviado"] = sess.get("audio_enviado", 0) + 1
        derrubados = sess["fila"].put(("audio", pcm))
        if derrubados:
            # teto da fila: o áudio derrubado não vai mais ao ar, então sai da
            # conta de "em voo" — senão `falando`/`stats.playback.chunks` mentem
            # sobre um playback que já foi cortado.
            sess["audio_pendente"] = max(0, sess["audio_pendente"] - derrubados)
            sess["audio_descartados"] = sess.get("audio_descartados", 0) + derrubados


def _live_buffer(sess: dict) -> bytes:
    """Áudio do turno em curso (PCM16 16k). Trunca pelo INÍCIO acima do teto."""
    return bytes(sess["buffer"])


try:                                     # motor de turnos do LIVE-3 (#93)
    import live_pipeline as _live_mod
except Exception:                        # noqa: BLE001 — sem o módulo, o stub segura
    _live_mod = None

try:                                     # FSM de turnos do LIVE-2 (#92)
    import live_turns as _live_turns_mod
except Exception:                        # noqa: BLE001 — sem ela, só o PCM cru
    _live_turns_mod = None

try:                                     # RNNoise no PCM do Live, antes do feed (#278)
    import live_rnnoise as _live_rnnoise_mod
except Exception:                        # noqa: BLE001 — sem o módulo, o PCM segue cru
    _live_rnnoise_mod = None


def _live_rnnoise_novo():
    """Filtro da sessão Live. None = desligado (`TTS_LIVE_RNNOISE=0`) ou sem módulo.

    `abrir` roda em thread para não segurar o handshake. Se o modelo não
    carregar, o primeiro frame segue cru e o aviso sai uma vez."""
    if _live_rnnoise_mod is None or not _live_rnnoise_mod.ligado():
        return None
    ns = _live_rnnoise_mod.RnnoiseAoVivo()
    threading.Thread(target=ns.abrir, name="rnnoise-abre", daemon=True).start()
    return ns


def _live_rnnoise_derruba(sess: dict, motivo: str) -> None:
    """Tira o filtro da sessão. O frame que falhou segue cru; os próximos também."""
    ns = sess.pop("rnnoise", None)
    if ns is not None:
        try:
            ns.fechar()
        except Exception:                # noqa: BLE001
            pass
    _live_log.warning("rnnoise desligado (%s) — Live segue sem o filtro", motivo)


# ── Híbrida do #442: filtro no VAD, PCM CRU no STT ─────────────────────────
# O filtro salva a ABERTURA do turno sob ventoinha alta e degrada ~50% das palavras
# quando entra no caminho do STT (medido, `evidence/442-rnnoise-vad-x-stt.md`).
# Aqui o motor continua recebendo o filtrado (é ele que decide) e o áudio que vai ao
# STT sai deste ring do PCM cru, alinhado por AMOSTRA. `TTS_LIVE_RNNOISE_STT=filtrado`
# volta ao caminho antigo (é a mordida) e `TTS_LIVE_RNNOISE=0` desliga o filtro todo.
_TTS_LIVE_RNNOISE_STT = (os.environ.get("TTS_LIVE_RNNOISE_STT") or "cru").strip().lower()
_CRU_CAUDA_S = 10.0                  # pré-roll do motor + folga; ~320 kB por sessão
#: geração das passadas de comando (#803): a nova invalida a anterior, que desiste
#: no próximo ponto seguro — sem isso a abandonada seguia segurando o MLX.
_STT_CMD_GERACAO = 0
_CRU_MAX_BYTES = int(_CRU_CAUDA_S * 32000)


def _live_cru_guarda(sess: dict, pcm: bytes) -> None:
    """Guarda o frame CRU com a amostra absoluta dele (antes de qualquer filtro)."""
    ring = sess.get("cru")
    if ring is None:
        # [bytearray, amostra absoluta do 1º byte]
        ring = sess["cru"] = [bytearray(), 0]
    ring[0].extend(pcm)
    sobra = len(ring[0]) - _CRU_MAX_BYTES
    if sobra > 0:
        del ring[0][:sobra]
        ring[1] += sobra // 2


def _live_audio_cru(sess: dict, ev) -> bytes:
    """Trecho CRU do evento (`amostra` do motor), não o pré-roll filtrado dele.

    Sem o ring (sessão sem filtro, ou buraco), devolve o que o motor mandou — o
    turno nunca fica sem áudio por causa disto."""
    audio = bytes(getattr(ev, "audio", b"") or b"")
    if not audio or _TTS_LIVE_RNNOISE_STT != "cru" or sess.get("rnnoise") is None:
        return audio
    ring = sess.get("cru")
    if not ring or not ring[0]:
        return audio
    # #869: `amostra` é o INÍCIO do áudio só nos eventos de abertura; no
    # `speech_end` o motor carimba o FIM (`_fechar_turno`). Ler o fim como início
    # recortava o trecho DEPOIS da fala — que ainda nem tinha chegado, então o
    # tamanho não batia e o turno caía SEMPRE no filtrado: a híbrida do #481
    # nunca entregou o cru ao STT. E o motor conta amostras do FILTRADO, que
    # anda `atraso_amostras` atrás do cru: sem descontar, o recorte perdia o
    # ataque da 1ª palavra.
    n = len(audio) // 2
    inicio = int(getattr(ev, "amostra", 0) or 0)
    if getattr(ev, "tipo", "") == "speech_end":
        inicio -= n
    inicio -= int(getattr(sess.get("rnnoise"), "atraso_amostras", 0) or 0)
    desloc = (inicio - ring[1]) * 2
    if desloc < 0:
        return audio                     # o começo já saiu do ring: o filtrado serve
    pedaco = bytes(ring[0][desloc:desloc + len(audio)])
    return pedaco if len(pedaco) == len(audio) else audio


def _live_pcm_limpo(sess: dict, pcm: bytes) -> bytes:
    """RNNoise no frame, ANTES de `TurnEngine.feed`. Mesmo tamanho, ou o PCM cru.

    Limpar o wav depois do `speech_end` não tira a ventoinha do VAD nem do
    barge-in: a decisão lê este frame. Tamanho diferente quebraria o relógio
    (o motor conta amostras), então nesse caso o filtro sai e o cru entra."""
    ns = sess.get("rnnoise")
    if ns is None:
        return pcm
    if getattr(ns, "morto", False):      # caiu de vez (#375/F3)
        _live_rnnoise_derruba(sess, "filtro caiu")
        return pcm
    try:
        sai = ns.processa(pcm)
    except Exception as exc:             # noqa: BLE001 — o turno não pode morrer aqui
        _live_rnnoise_derruba(sess, f"{type(exc).__name__}: {exc}")
        return pcm
    if getattr(ns, "morto", False):
        # caiu NESTE frame: sai já, sem pagar mais um hop de thread pelo filtro
        _live_rnnoise_derruba(sess, "filtro caiu")
        return pcm
    if not isinstance(sai, (bytes, bytearray)) or len(sai) != len(pcm):
        _live_rnnoise_derruba(sess, "tamanho diferente do frame")
        return pcm
    return bytes(sai)


def _live_engine_novo(sess: dict):
    """FSM de turnos da sessão (None = sem o módulo: comportamento antigo).

    `prefix_ms`/`silence_ms`/`barge_ms` vêm do `setup` do cliente (validados lá). A
    fala do TTS entra por `set_speaking()` — é dela que sai o limiar adaptativo do
    eco."""
    if _live_turns_mod is None:
        return None
    try:
        cfg = _live_turns_mod.Config(
            prefix_ms=sess["vad"]["prefix_ms"],
            silence_ms=sess["vad"]["silence_ms"],
            # #313: janela de confirmação do barge pedida pelo cliente (o custo de
            # ~barge_ms + rampa do envelope era fixo e não dava para encurtar). O
            # `get`: dublês de teste montam `vad` só com `prefix_ms`/`silence_ms`.
            barge_in_ms=sess["vad"].get("barge_ms") or _LIVE_BARGE_MS_PADRAO,
            # #501: modo de interrupção do settings (clamp já feito em
            # `_apply_settings`; o `get` protege dublês de teste)
            interrupcao=str(_settings.get("live_interrupcao") or "inteligente"),
            barge_decisao_ms=int(_clamp(_settings.get("live_barge_decisao_ms"), 200,
                                        2000, 700)),
            # #167: janela de barge colada ao turno. LIGADA por default desde o
            # #216 (medição da COMBINAÇÃO; ver Config.barge_janela_turno):
            # desligável com `TTS_LIVE_BARGE_JANELA_TURNO=0` para A/B.
            barge_janela_turno=os.environ.get(
                "TTS_LIVE_BARGE_JANELA_TURNO", "1") != "0",
            # #167: janela pela duração real do chunk (ver Config.playback_por_duracao).
            # LIGADA por default desde o #216 — é ela que cobre o áudio que o
            # cliente ainda tem na fila quando o turno termina.
            playback_por_duracao=os.environ.get(
                "TTS_LIVE_PLAYBACK_DURACAO", "1") != "0",
            # #167 (direção c): o ECO só vale como referência enquanto há áudio
            # audível. Fica DESLIGADA: medida em COMBINAÇÃO (#216) ela piora os
            # dois sentidos (ver Config.eco_so_tocando).
            eco_so_tocando=os.environ.get(
                "TTS_LIVE_ECO_SO_TOCANDO", "0") != "0",
            # #870 S3: duck em ~96 ms e pausa reversível em ~480 ms. OFF até a
            # bancada medir falso corte. Env, não settings — a chave não pode
            # parar no estado do dono.
            duck_rapido=os.environ.get("TTS_LIVE_BARGE_DUCK_RAPIDO", "0") == "1")
    except Exception:                    # noqa: BLE001 — config inválida: usa o padrão
        cfg = None
    return _live_turns_mod.TurnEngine(config=cfg)


def _live_turno_em_curso(sess: dict) -> bool:
    """Há turno (ou áudio dele) para interromper? (#313)

    Serve ao `cancel`: cancelar com a sessão OCIOSA não pode emitir `interrupted`
    — o cliente piscava "interrompido" sem nada ter sido interrompido."""
    pipe = sess.get("pipe")
    if pipe is not None and pipe.ocupado:
        return True
    eng = sess.get("engine")
    if eng is not None and getattr(eng, "turno_aberto", False):
        return True
    return bool(sess.get("audio_pendente"))


def _live_fala_ms_corte(sess: dict, fala_ms) -> int | None:
    """`interrupted.fala_ms` só com o duck rápido. Flag off: o fio é o da main."""
    eng = sess.get("engine")
    if not getattr(getattr(eng, "config", None), "duck_rapido", False):
        return None
    if fala_ms is None:
        return None
    return int(fala_ms)


def _live_cancela(sess: dict, do_cliente: bool = False, *,
                  forcar: bool = False, fala_ms: int | None = None) -> None:
    """Derruba o turno em curso (barge-in ou `cancel` do cliente).

    `do_cliente` (comando `cancel`) limpa TAMBÉM o pendente: o usuário abortou
    tudo. No barge-in o pendente SEGUE — a fala que interrompeu abre turno
    próprio (ou já está nele) e é respondida depois.

    `forcar` (#313) desliga a checagem de ociosidade: o `barge_in` do motor é PROVA
    de interrupção, mesmo quando a sessão ainda não tem turno registrado (é o 1º
    evento do lote e pode chegar antes de qualquer áudio no fio).

    A ociosidade que conta é a da sessão SEM TURNO ALGUM: depois de um turno já
    havido, o `cancel` também serve para parar o que o cliente ainda tiver na fila
    dele, então segue emitindo `interrupted` (comportamento antigo preservado). O
    que sai é o disparo inventado numa sessão recém-aberta, em que nada foi falado.

    NÃO incrementa mais o `turno` (#313): quem numera é o pipeline, no
    `speech_start`, e a sessão adota (`_live_observa`). Antes cada lado contava por
    conta própria e os eventos carregavam números de sequências diferentes — o
    `interrupted` desta função anunciava um turno que nunca existiu. Com a fonte
    única ele aponta o turno que está sendo de fato interrompido."""
    em_curso = forcar or _live_turno_em_curso(sess) or bool(sess.get("turno"))
    sess["cancelado"].set()
    if do_cliente:
        # pop + zero na MESMA seção crítica do guarda (#221): sem isso o guarda
        # pode acumular entre o `pop` e o zero e o pendente NOVO nasce com o
        # contador zerado depois de já ter contado (mesma classe do perdedor)
        with _live_pend_lock(sess):
            if sess.pop("turno_pendente", None) is not None:
                sess["turno_pendente_barge"] = False
                sess["pendentes_descartados"] = sess.get("pendentes_descartados", 0) + 1
                _live_pendente_zera_contadores(sess)
                _live_log_kv("pend_cancelado", sess=sess["id"])
    _live_log_kv("cancel", sess=sess["id"], turno=sess["turno"], em_curso=em_curso)
    # O corte DESCARTA o playback que já saiu: o cliente chama `lxCancelaPlayback`
    # no `interrupted` e não vai tocar mais nada da fila. Sem avisar o motor, a
    # JANELA dele seguia aberta pelo backlog inteiro (9,5–19,4 s medidos) e, dentro
    # dela, a fala seguinte era lida como BARGE: suspeita nova → `interrupcao` →
    # matava a resposta que acabara de nascer, em cascata — e com o piso de ECO
    # (alto, de um TTS que já não toca) a fala nem passava do limiar, então o turno
    # não abria e a sessão ficava parada com o motor `ocioso` recebendo voz.
    eng = sess.get("engine")
    fn_cortado = getattr(eng, "playback_cortado", None)
    if fn_cortado is not None:
        try:
            fn_cortado()
        except Exception:                # noqa: BLE001 — motor não pode derrubar o WS
            pass
    pipe = sess.get("pipe")
    if pipe is not None:
        pipe.cancel()
        if pipe.ocupado:                 # com turno rodando, quem avisa é o pipeline
            return
    if not em_curso:
        # ocioso: nada a interromper. O stub lê esta flag para não abrir turno e
        # ela não pode ficar setada depois de um cancel que não cancelou nada.
        sess["cancelado"].clear()
        return
    aviso = {"type": "interrupted", "turn": sess["turno"]}
    # #881: ms desde o onset do VAD. Ausente no cancel do cliente e no ocioso.
    if fala_ms is not None:
        aviso["fala_ms"] = int(fala_ms)
    _live_envia_json(sess, aviso)


_DEBUG_LIVE = os.environ.get("LIVE_DEBUG_TTS") == "1"


def _live_abre_turno(sess: dict, pcm: bytes = b"", barge_in: bool = False) -> None:
    """Abre o turno com o áudio QUE VEIO NO EVENTO (pré-roll já incluído).

    Sem evento (caminho do comando `end_of_speech`), o áudio é o que a sessão
    acumulou — o tamanho fica registrado para o turno e o buffer é zerado quando o
    turno fecha.

    Com turno JÁ aberto (o cliente manda `end_of_speech` logo depois do
    `speech_end` automático do motor), o áudio não é empilhado de novo: o motor
    já entregou esse mesmo trecho no evento. Sem esta guarda, os bytes ficavam no
    buffer do pipeline e envenenavam o turno SEGUINTE (medido: turno 1 com 16 kB
    mudos + 1 kB, e o turno 2 nascendo com o áudio do anterior e truncado).

    O snapshot do buffer só vale o que AINDA não tem dono (`buffer_consumido`:
    bytes já entregues ao turno em curso ou a um pendente) — sem isso, a fala
    enfileirada durante um turno sairia com o áudio do turno ANTES repetido."""
    if not pcm and sess.get("buffer"):
        pcm = bytes(sess["buffer"][sess.get("buffer_consumido", 0):])
        # marcador: estes bytes já têm dono (turno aberto ou pendente)
        sess["buffer_consumido"] = len(sess["buffer"])
    sess["buffer_bytes_turno"] = len(pcm)
    # flag do evento: o pipeline usa para separar ECO de humano (decisão do PM);
    # vai na sessão E como kwarg quando o pipeline aceitar (transição).
    sess["turno_barge_in"] = bool(barge_in)
    # TRECHO DO BARGE (#674): o áudio do trecho suspeito entra ANTES do áudio do
    # turno. Ele já foi consumido do motor pela decisão, então o turno nascia só
    # com a cauda (224 ms medidos) e o VAD do STT recusava o turno inteiro como
    # `vad` — a fala do dono sumia e a sessão ficava parada. O `pop` garante uma
    # vez só (o caminho pendente carrega o trecho junto, então aqui não sobra).
    pcm = (sess.pop("barge_audio", b"") or b"") + (pcm or b"")
    # CONTINUAÇÃO (#877): a 1ª metade da frase (turno cancelado porque o dono
    # voltou a falar antes de qualquer áudio da resposta) vem NA FRENTE — o STT
    # transcreve a frase inteira e o LLM responde a ela, não à metade.
    pcm = (sess.pop("continuacao_audio", b"") or b"") + pcm
    pipe = sess.get("pipe")
    if pipe is None:                     # sem pipeline: stub mínimo
        # via `_live_envia_json` (#211), como o irmão `_live_engine`: a fila crua
        # pulava `_live_enriquece`/`_live_observa`/`_live_hist_pos_turno` e deixava
        # buffer sem zerar, `st_stage` preso em "stt" e o registro de retomada sem
        # gravar (o buffer de mic ainda acumulava até o teto de 2 MB).
        _live_envia_json(sess, {"type": "turn_complete", "stub": True,
                                "buffer_bytes": len(pcm)})
        return
    if _DEBUG_LIVE:
        print(f"[live][dbg] abre_turno pcm={len(pcm)} ocupado={pipe.ocupado} "
              f"turno={sess['turno']}", flush=True)
    if pipe.ocupado:
        if pcm:
            # #126: fala que chega com turno em curso (resposta longa pode levar
            # dezenas de s) não é mais descartada — vira 1 turno pendente e abre
            # quando o pipeline liberar. O dono lia o descarte como "parou de
            # captar". Sem áudio não há o que enfileirar: mantém o aviso antigo.
            _live_guarda_pendente(sess, pcm, barge_in)
        else:
            _live_envia_json(sess, _live_erro("turno_em_curso", "espere o turn_complete"))
        return
    # #315: gate do PREWARM. Falar antes de os modelos estarem quentes custa o
    # primeiro áudio inteiro (medido: 7,3 s a frio contra 0,78–0,87 s quente) e
    # nada segurava o turno. A fala vai para o pendente e o observador espera o
    # `prewarm.ok` até `TTS_LIVE_ESPERA_PREWARM_S`; estourou, ele avisa
    # (`prewarm_em_curso`) e abre o turno assim mesmo. `0` desliga.
    if (pcm and _LIVE_ESPERA_PREWARM_S > 0 and not sess.get("prewarm_ok")
            and not sess.get("prewarm_esperou")):
        if sess.get("prewarm_ate") is None:
            sess["prewarm_ate"] = time.monotonic() + _LIVE_ESPERA_PREWARM_S
        if _live_prewarm_pendente(sess):
            _live_guarda_pendente(sess, pcm, barge_in)
            return
    # FALHA ALTO e a sessão sobrevive (padrão do `prewarm`): se o pipeline mudar de
    # assinatura, o cliente VÊ o erro em vez de ficar com turno mudo — o modo
    # fantasma que a muleta antiga (`except TypeError` seguindo sem o flag) escondia.
    try:
        sess["t_abre_turno"] = time.monotonic()   # #315: origem do `decisao_para_audio_ms`
        # #802: entrega e disparo na MESMA trava — em dois passos, o observador do
        # pendente abria o turno no meio e o trecho saía duplicado ou perdido.
        _abrir = getattr(pipe, "abrir_turno", None)
        if _abrir is not None:
            abriu = _abrir(pcm, barge=bool(barge_in))
        else:
            if pcm:
                pipe.push_pcm(pcm, substituir=True)   # é o turno INTEIRO, não um pedaço
            abriu = pipe.end_of_speech(barge=bool(barge_in))
        if abriu and pcm and not barge_in:
            # candidato a CONTINUAÇÃO (#877): o que foi entregue agora, o instante
            # e quanto áudio de resposta já tinha saído antes dele
            sess["continuavel"] = {"pcm": pcm, "t": time.monotonic(),
                                   "audio0": sess.get("audio_enviado", 0)}
        elif abriu:
            sess.pop("continuavel", None)
        if not abriu:
            # perdeu a corrida (outro turno abriu entre a checagem e aqui):
            # a fala vira pendente em vez de sumir (mesma política do ocupado)
            if pcm:
                _live_guarda_pendente(sess, pcm, barge_in)
            else:
                _live_envia_json(sess, _live_erro("turno_em_curso", "espere o turn_complete"))
    except Exception as exc:                  # noqa: BLE001 — erro do pipeline, não da sessão
        # turno MORTO: sem `turn_complete`/`interrupted` vindos do pipeline, a janela
        # intra-turno (#167) ficaria aberta até a trava de segurança do motor.
        # Fechar aqui é idempotente (o `set_turno_aberto` do motor aceita repetição).
        _live_janela_turno(sess, False)
        _live_envia_json(sess, _live_erro("pipeline", f"{type(exc).__name__}: {exc}"))


def _live_pendente_zera_contadores(sess: dict) -> None:
    """Zera os contadores do evento `turno_pendente` (`trechos`, `descartados_ms`).

    Eles descrevem o pendente ATUAL: consumido (ou descartado pelo `cancel`), o
    próximo `turno_pendente` começa a contar do zero em vez de herdar o acúmulo
    do anterior. O `fim_de_resposta` é do pendente também (é o que diz à tela que
    a fala nasceu no último segundo): zerado junto, senão o próximo herda o
    rótulo do anterior."""
    sess["pendentes_trechos"] = 0
    sess["pendentes_descartados_ms"] = 0
    sess["fim_de_resposta"] = False


_pend_lock_cria = threading.Lock()


def _live_pend_lock(sess: dict) -> threading.Lock:
    """Lock do turno pendente — serializa o guarda (loop) contra o observador.

    Os dois fazem read-modify-write em `sess["turno_pendente"]` de THREADS
    diferentes (#208): o guarda acumula o trecho novo e o observador faz `pop`
    + re-arm. Sem o lock, quem escreve por último apaga o outro e o trecho de
    fala do usuário some. Criado na sessão; a criação preguiçosa é para as
    sessões de teste (dupla checagem com um lock de módulo: dois threads não
    podem ficar com locks diferentes)."""
    lock = sess.get("pend_lock")
    if lock is None:
        with _pend_lock_cria:
            lock = sess.get("pend_lock")
            if lock is None:
                lock = sess["pend_lock"] = threading.Lock()
    return lock


def _live_prewarm_pendente(sess: dict) -> bool:
    """O turno ainda precisa esperar o prewarm? (#315)

    Só enquanto o prewarm NÃO terminou e o prazo de espera não estourou. Sem prazo
    armado nada espera — quem arma é o `_live_abre_turno`, no 1º turno que chega
    antes do `prewarm.ok`."""
    ate = sess.get("prewarm_ate")
    return ate is not None and not sess.get("prewarm_ok") and time.monotonic() < ate


def _live_guarda_pendente(sess: dict, pcm: bytes, barge_in: bool,
                          fim_de_resposta: bool = False) -> None:
    """Acumula o trecho no ÚNICO pendente, para abrir quando o pipeline liberar.

    CONCATENA os pedaços (#126, ajuste do PM): a pessoa costuma completar a
    frase em dois. Teto de duração (`_LIVE_PENDENTE_MAX_S`): estourou, sai o
    MAIS ANTIGO — o fim é o que completa a frase — e o evento avisa `truncado`
    COM QUANTO saiu (`descartados_ms`): o booleano `substituido` não dizia se o
    corte foi de 20 ms ou de 20 s, e o cliente só podia adivinhar.
    O flag de barge-in é pegajoso: qualquer pedaço nascido no playback mantém a
    checagem de eco no turno pendente (transcript que casa com a própria fala
    não vira resposta).

    `fim_de_resposta` marca a fala que nasceu no ÚLTIMO segundo: ela não corta a
    resposta e vai ser respondida depois. O campo sai no evento (a tela lê) e no
    log — sem ele o caminho era indistinguível de "pipeline ocupado" (#569)."""
    if not pcm:
        return
    with _live_pend_lock(sess):                   # #208: contra o observador
        novo = (sess.get("turno_pendente") or b"") + pcm
        truncado = len(novo) > _LIVE_PENDENTE_MAX_BYTES
        if truncado:
            cortados = len(novo) - _LIVE_PENDENTE_MAX_BYTES
            novo = novo[-_LIVE_PENDENTE_MAX_BYTES:]
            sess["pendentes_descartados"] = sess.get("pendentes_descartados", 0) + 1
            # 32 bytes = 1 ms (PCM16 mono 16 kHz) — mesma base do teto
            sess["pendentes_descartados_ms"] = (sess.get("pendentes_descartados_ms", 0)
                                                + cortados // 32)
        sess["pendentes_trechos"] = sess.get("pendentes_trechos", 0) + 1  # ACUMULADOS
        sess["turno_pendente"] = novo
        if barge_in:
            sess["turno_pendente_barge"] = True   # pegajoso: eco-check segue valendo
        if fim_de_resposta:
            sess["fim_de_resposta"] = True
        # os contadores do evento são LIDOS AQUI, ainda no lock: fora dele o
        # observador pode zerá-los (o `pop` zera, #221) entre o incremento e a
        # leitura, e o evento sairia com `trechos = 0`
        trechos = sess["pendentes_trechos"]
        descartados_ms = sess.get("pendentes_descartados_ms", 0)
        barge = bool(sess.get("turno_pendente_barge"))
        fim = bool(sess.get("fim_de_resposta"))
    _live_log_kv("turno_pendente", sess=sess["id"], bytes=len(novo), trechos=trechos,
                 truncado=truncado, descartados_ms=descartados_ms, barge=barge,
                 fim_de_resposta=fim)
    # protocolo aditivo: "anotei, respondo já" — o turno em si vem na sequência
    # (speech_start/turn_complete quando o pipeline liberar)
    _live_envia_json(sess, {"type": "turno_pendente", "buffer_bytes": len(novo),
                            "trechos": trechos, "descartados_ms": descartados_ms,
                            "barge_in": barge, "truncado": truncado,
                            "fim_de_resposta": fim})
    _live_acorda_pendente(sess)


def _live_pend_rearma(sess: dict, pendente: bytes, barge: bool,
                      trechos: int = 0, descartados_ms: int = 0) -> None:
    """Devolve o pendente que o observador tirou — SEM apagar o que chegou depois.

    O guarda pode ter acumulado um trecho novo entre o `pop` e este re-arm: o
    re-arm antigo regravava o valor que ele leu e o trecho NOVO sumia (#208).
    Aqui o antigo vem PRIMEIRO (é o mais antigo na linha do tempo) e o barge é
    pegajoso — qualquer pedaço nascido no playback mantém a checagem de eco.

    Os contadores do pendente que VOLTA vêm por parâmetro e são SOMADOS aos do
    intervalo (#221): o `pop` os zerou (é o zero atômico com o guarda) e o
    pendente re-armado tem de voltar contando o que já acumulou — senão o
    próximo evento subconta."""
    with _live_pend_lock(sess):
        novo = sess.get("turno_pendente") or b""
        sess["turno_pendente"] = pendente + novo
        sess["pendentes_trechos"] = sess.get("pendentes_trechos", 0) + trechos
        sess["pendentes_descartados_ms"] = (sess.get("pendentes_descartados_ms", 0)
                                            + descartados_ms)
        if barge:
            sess["turno_pendente_barge"] = True


def _live_acorda_pendente(sess: dict) -> None:
    """Garante UM observador por sessão esperando o pipeline liberar.

    Chamado quando o pendente é guardado, quando um turno fecha (o turno em
    curso pode ter acabado) e pelo PRÓPRIO observador moribundo (re-arm no fim
    de `_live_descarrega_pendente`). Nesse último caso a thread ainda consta
    viva — é a current_thread —, então a checagem de `is_alive` não pode bloquear
    quem está saindo de armar o sucessor (gate #126/#127, achado 2: sem isso, a
    fala guardada na janela de morte ficava órfã até o próximo fechamento de
    turno — ou para sempre, com o pipeline livre)."""
    t = sess.get("pend_thread")
    if (t is not None and t is not threading.current_thread()
            and t.is_alive()):
        return
    t = threading.Thread(target=_live_descarrega_pendente, args=(sess,), daemon=True)
    sess["pend_thread"] = t
    t.start()


def _live_descarrega_pendente(sess: dict) -> None:
    """Espera o pipeline liberar e abre o turno pendente (1 no máx.).

    O `pop` decide o vencedor: com vários observadores acordados pelo mesmo
    fechamento, só o primeiro processa. Se o pipeline voltou a ficar ocupado
    (outra fala abriu turno no intervalo), o pendente VOLTA — o fechamento
    desse turno acorda um observador de novo. Daemon: morre com a sessão."""
    pipe = sess.get("pipe")
    if pipe is None:
        return
    try:
        while not sess.get("fechar") and (pipe.ocupado or _live_prewarm_pendente(sess)):
            time.sleep(0.1)
        # ESTOURO do prazo: consome o aviso e o estado na MESMA seção crítica.
        # `_live_acorda_pendente` não é atômico e o observador moribundo re-arma o
        # sucessor: com 2+ observadores acordados no mesmo prazo TODOS passavam o
        # `while` e cada um mandava o próprio `prewarm_em_curso` — o dono recebeu
        # vários no mesmo milissegundo (21:26, rev c6f54ea). Quem consome é um só:
        # `prewarm_ate` vira None para os demais e `prewarm_avisado` é pegajoso,
        # então o aviso é UM por sessão mesmo que um caminho re-arme o prazo.
        avisa = False
        with _live_pend_lock(sess):
            if (sess.get("prewarm_ate") is not None and not sess.get("prewarm_ok")
                    and not sess.get("prewarm_avisado")):
                avisa = True
                sess["prewarm_avisado"] = True
            sess["prewarm_ate"] = None
            # já se esperou o que dava: o próximo `_live_abre_turno` abre direto em
            # vez de armar outro prazo e prender a fala de novo (senão o pendente
            # entra em laço: abre, o gate re-arma, guarda, estoura, abre…)
            sess["prewarm_esperou"] = True
        if avisa:
            # o turno SEGUE (não descarta), mas o cliente VÊ por que o primeiro
            # áudio vai demorar
            _live_envia_json(sess, _live_erro(
                "prewarm_em_curso",
                "pipeline ainda aquecendo; o turno segue com os modelos frios"))
        # #221: `pop` e contadores na MESMA seção crítica, e só quem LEVOU o
        # pendente os toca. Antes o zero ficava fora do lock e o observador
        # PERDEDOR (pop = None, caso NORMAL quando dois acordam no mesmo
        # fechamento) zerava entre o `pop` do vencedor e o re-arm dele: o
        # pendente que voltava a existir ficava com `trechos = 0` e o próximo
        # evento SUBCONTAVA. Zerar aqui também é atômico com o guarda — o trecho
        # que ele acumular depois já conta para o pendente NOVO.
        with _live_pend_lock(sess):       # #208: o guarda pode estar acumulando
            pendente = sess.pop("turno_pendente", None)
            if pendente:
                barge = bool(sess.pop("turno_pendente_barge", False))
                trechos = sess.get("pendentes_trechos", 0)
                descartados_ms = sess.get("pendentes_descartados_ms", 0)
                _live_pendente_zera_contadores(sess)
        if not pendente or sess.get("fechar"):
            return
        # contadores do evento descrevem o pendente ATUAL: no re-arm voltam
        # JUNTO com ele (o re-arm SOMA, não regrava) — e o guarda já contou o
        # que chegou no intervalo
        if pipe.ocupado:                  # fechou e abriu outro no intervalo
            _live_pend_rearma(sess, pendente, barge, trechos, descartados_ms)
            return
        _live_abre_turno(sess, pendente, barge_in=barge)
    finally:
        # Fala guardada NA janela de morte deste observador (a guarda viu a
        # thread viva e não armou outra): sem o re-arm ela ficava órfã até o
        # próximo fechamento de turno — ou para sempre, com o pipeline livre
        # (gate #126/#127, achado 2). O `fechar` fora: encerrar a sessão não
        # pode virar nascedouro de observadores.
        if sess.get("turno_pendente") and not sess.get("fechar"):
            _live_acorda_pendente(sess)


def _live_transcreve_trecho(sess: dict, audio: bytes, cru: bool = False,
                            prazo_s: float = 0.0) -> str:
    """Transcreve o trecho suspeito.

    `cru=True` (passo do comando): vai DIRETO no whisper, sem o VAD e o juiz de
    alucinação do `_stt_app` — eles custam ~200 ms e aqui só interessa casar
    palavra de comando. A decisão final continua usando o caminho completo."""
    if not audio:
        return ""
    pipe = sess.get("pipe")
    idioma = getattr(pipe, "language", None)
    stt = getattr(pipe, "_stt", None)
    if cru:                              # o passo do comando não usa o `_stt`
        try:
            import concurrent.futures as _cf
            import numpy as _np
            a = _np.frombuffer(audio, dtype="<i2").astype(_np.float32) / 32768.0
            a = a[:24000]                      # ~1,5 s do INÍCIO: o comando vem no
                                               # ataque, não no fim do trecho
            if prazo_s <= 0:
                with _stt_do_live():     # #876: Whisper do Live
                    return (_transcreve_em_janelas(a, idioma).get("text") or "").strip()
            # PRAZO por passada: o STT sob carga leva segundos e sem prazo a fila
            # de passadas cresce até estourar o teto de 2 s do cliente (#503).
            # `shutdown(wait=False)` é ESSENCIAL (#547): com o `with`, o
            # `__exit__` espera o pool e a passada "abandonada" continuava
            # bloqueando a thread do passo pelos segundos do STT — o prazo era
            # decorativo e cada passo levava o tempo todo, atrasando a decisão em
            # até 76 s medidos. Abandonar de verdade é devolver na hora e deixar a
            # passada morrer sozinha.
            # UMA passada por vez (#803): o MLX é single-thread e duas passadas
            # concorrentes derrubam o servidor com `GPU Timeout` (exit 134). A
            # trava é a MESMA do whisper do turno (`_stt_lock`), e a geração
            # compartilhada faz a passada ABANDONADA desistir no próximo ponto
            # seguro em vez de rodar a inferência inteira segurando o modelo.
            global _STT_CMD_GERACAO
            _STT_CMD_GERACAO += 1
            geracao = _STT_CMD_GERACAO

            def _desistiu() -> bool:
                return geracao != _STT_CMD_GERACAO

            def _roda():
                with _stt_lock, _stt_do_live():     # #876: thread do pool, marca aqui
                    if _desistiu():
                        return ""
                    return _transcreve_em_janelas(a, idioma,
                                                  desistiu=_desistiu).get("text") or ""

            pool = _cf.ThreadPoolExecutor(max_workers=1)
            fut = pool.submit(_roda)
            try:
                return (fut.result(timeout=prazo_s) or "").strip()
            except _cf.TimeoutError:
                st = sess.setdefault("barge_stats", {"tipos": {}, "decisoes": [],
                                                     "modelo": []})
                st["passo_abandonado"] = int(st.get("passo_abandonado") or 0) + 1
                return ""
            finally:
                pool.shutdown(wait=False)
        except Exception as exc:           # noqa: BLE001
            _live_log_kv("barge_stt_erro", sess=sess["id"], erro=str(exc)[:100])
            return ""
    if stt is None:
        return ""
    try:
        return (stt(audio, idioma) or "").strip()
    except Exception as exc:              # noqa: BLE001
        _live_log_kv("barge_stt_erro", sess=sess["id"], erro=str(exc)[:100])
        return ""


def _cfg_identidade(cfg: dict) -> dict:
    """Bloco do `_voz_do_dono` a partir do FILTRO: `perfis` vazio = modo I5, e o
    `far` vira faixa (0.01 = FAR 1 %, o calibrado; o resto vai no joelho)."""
    perfis = [str(p) for p in (cfg.get("perfis") or []) if str(p).strip()]
    far = float(cfg.get("far") or 0.01)
    rigor = "1" if far <= 0.011 else ("0.1" if far >= 0.05 else _VOZ_RIGOR_JOELHO)
    base = _live_voz_ligado() or {"on": True, "perfil": ""}
    return {**base, "on": True, "perfil": (perfis[0] if perfis else base.get("perfil") or ""),
            "rigor_far": rigor}


def _live_avalia_filtros(sess: dict, texto: str, fala_ms: int,
                         filtros: dict, audio: bytes = b""
                         ) -> tuple[str, float | None, int | None, str]:
    """Roda a cadeia NA ORDEM do card e devolve (tipo, p_cortar, modelo_ms).

    Cada filtro só age se estiver `on`. `modelo` é o único assíncrono e o único
    que devolve `p_cortar`; sem classificador ligado ele é pulado. Quem chega ao
    fim sem decisão usa `live_barge_sem_decisao`."""
    from live_turns import (_BACKCHANNEL, _COMANDO_PARAR, _PERGUNTA, _RUIDO_ONO,
                            _palavras, _sem_acento,
                            classifica_barge)
    palavras = _palavras(texto)
    # REFERÊNCIA do eco: vem do PIPELINE (#801), que é quem sabe o que ainda está
    # tocando. A chave `sess["ultimo_assistente"]` nunca era gravada — o filtro
    # `eco` estava morto e o `modelo` decidia com `ref` vazia.
    _pipe_eco = sess.get("pipe")
    _fn_eco = getattr(_pipe_eco, "texto_em_reproducao", None)
    eco_txt = (_fn_eco() if _fn_eco is not None else "") or ""

    def ligado(nome: str, padrao: bool = True) -> bool:
        cfg = filtros.get(nome)
        return padrao if cfg is None else bool(cfg.get("on", padrao))

    # 1) palavras de corte — a lista do settings manda (cadastrável pelo dono).
    # DOIS detalhes que matavam o elo (#547):
    # · lista VAZIA (o default) caía no `get` com a chave existindo, então a lista
    #   interna `_COMANDO_PARAR` NUNCA era usada e `palavras_corte` não casava
    #   nada — nenhuma decisão saía por aqui, era tudo `frase_longa`/`conteudo`;
    # · a palavra valia só na 1ª/última posição: com o eco do assistente na
    #   frente do trecho ("...e aí, para") o comando ficava no meio e passava
    #   batido. A lista de comando é curta e distintiva — vale em QUALQUER
    #   posição (decisão do PM, hipótese 3 do card).
    if ligado("palavras_corte") and palavras:
        cadastradas = (filtros.get("palavras_corte") or {}).get("palavras") or []
        # a lista entra na MESMA normalização do texto (sem acento, minúscula):
        # cadastrar "não"/"olá" com acento nunca casaria com a fala normalizada
        lista = {_sem_acento(str(x).lower()).strip()
                 for x in (cadastradas or _COMANDO_PARAR)}
        if any(p in lista for p in palavras):
            return "interrupcao", None, None, "palavras_corte"
    # 2) eco
    if ligado("eco") and eco_txt:
        from live_pipeline import _parece_eco
        if _parece_eco(texto, eco_txt):
            return "eco", None, None, "eco"
    # 2b) IDENTIDADE (#527): só voz cadastrada corta. Vem depois do eco (áudio
    # idêntico ao TTS é eco, não importa quem fala) e antes do resto: TV/outra
    # pessoa não pode cortar por ser "fala longa". Sem áudio (dublês) é pulado.
    if ligado("identidade", False) and audio and not _live_voz_ligado():
        cfg_id = _cfg_identidade(filtros.get("identidade") or {})
        voz = _voz_do_dono(audio, cfg_id)
        _live_voz_anota(sess, voz)
        if voz.get("estado") == "recusado":
            return "ruido", voz.get("score"), None, "identidade"
    # 3) fim_de_resposta — perto do fim, deixar terminar vence backchannel/ruído
    if ligado("fim_de_resposta"):
        motor = sess.get("engine")
        restante = motor.playback_restante_ms() if motor is not None else None
        if restante is not None and restante <= int(
                (filtros.get("fim_de_resposta") or {}).get("restante_ms", 2000)):
            return "ruido", None, None, "fim_de_resposta"
    # 4) backchannel — também pela REGRA pura (fonte única): o `on` e a lista só
    # decidem se ele pode valer. Com a regra sabotada para "sempre interrupção",
    # o smoke REPROVA aqui (#503).
    if ligado("backchannel") and palavras:
        permitidas = {str(x).lower() for x in
                      (filtros.get("backchannel") or {}).get("palavras", _BACKCHANNEL)}
        if (len(palavras) <= 2 and all(x in permitidas for x in palavras)
                and classifica_barge(texto, fala_ms, ref_eco="") == "backchannel"):
            return "backchannel", None, None, "backchannel"
    # 4) ruído — SÓ com evidência positiva: o STT voltou vazio, mas o VAD já
    # confirmou voz no trecho, então vazio é falha do STT, não silêncio. Cortar é
    # a falha SEGURA (o dono queria interromper e virava `ruido` calado, #503).
    if ligado("ruido") and not palavras:
        if fala_ms >= 200:
            return "interrupcao", None, None, "stt_vazio"
        return "ruido", None, None, "ruido"
    # 5) MODELO (assíncrono, #509) — só quando os rápidos não decidiram
    cfg_modelo = filtros.get("modelo") or {}
    if cfg_modelo.get("on"):
        tipo, p = _live_modelo_decide(sess, texto, fala_ms, eco_txt, cfg_modelo)
        if tipo is not None:
            return tipo, p, None, "modelo"
    # 6) pergunta / 7) frase longa / 8) fala sustentada — TODAS vêm da regra pura
    # (`classifica_barge`), fonte ÚNICA: a cadeia não reimplementa mais nada aqui
    # (#503). O `on` de cada filtro só decide SE aquele motivo pode cortar.
    tipo_puro = classifica_barge(texto, fala_ms, ref_eco=eco_txt)
    if tipo_puro == "interrupcao":
        if "?" in (texto or "") or any(x in _PERGUNTA for x in palavras):
            if ligado("pergunta"):
                return "interrupcao", None, None, "pergunta"
        elif fala_ms >= int((filtros.get("fala_sustentada") or {}).get("min_ms", 1200)):
            if ligado("fala_sustentada"):
                return "interrupcao", None, None, "fala_sustentada"
        elif len(palavras) >= int((filtros.get("frase_longa") or {}).get("min_palavras", 3)):
            if ligado("frase_longa"):
                return "interrupcao", None, None, "frase_longa"
        elif (palavras and not all(p in _RUIDO_ONO for p in palavras)
              and (ligado("pergunta") or ligado("frase_longa") or ligado("fala_sustentada"))):
            # conteúdo PRÓPRIO curto ("tchau", "obrigado", "beleza mano"): a regra
            # pura já disse interrupção e nenhum dos três motivos acima se aplica.
            # Sem esta saída a decisão caía no `sem_decisao` (default `continua`) e
            # uma fala de 1–2 palavras com conteúdo NUNCA cortava — a fonte única
            # era anulada no último passo (#547). O `elif` deixa o modo
            # `so_comando` (os três desligados) com o comportamento dele: só
            # palavras de comando cortam.
            return "interrupcao", None, None, "conteudo"
    # 9) sem decisão
    sem = str(_settings.get("live_barge_sem_decisao") or "continua")
    return (("interrupcao" if sem == "interrompe" else "ruido"), None, None,
            "sem_decisao")


# O prompt é o CONTRATO do filtro: medido no corpus de `intencao_corpus.jsonl`
# pelo próprio `provedor: dsh`, a versão antiga (uma frase corrida pedindo "prob
# de interromper") dava 52,9 % — o modelo respondia alto para backchannel. Esta
# versão separa as duas faixas e pede UMA casa decimal: 29/32 nas duas metades
# medidas (16/16 em cortar, 13/16 em continuar).
_PROMPT_MODELO = (
    "Você classifica UM trecho de fala de um usuário que pode estar interrompendo "
    "um assistente que fala. Responda APENAS um número de 0 a 1, com uma casa "
    "decimal, e nada mais.\n"
    "0.0-0.4 = NÃO interrompe (concordância, 'sim', 'uhum', tosse, eco).\n"
    "0.6-1.0 = INTERROMPE (comando de parar, pergunta, frase com conteúdo próprio).\n"
    "Assistente estava falando: {ref}\nFala: {fala}\nNota:")


def _live_modelo_timeout(sess: dict, provedor: str) -> None:
    """Conta o estouro do filtro `modelo` — separado do p50 (#596).

    Prazo estourado NÃO é decisão: entrava no p50 e mascarava a latência real."""
    _live_log_kv("barge_modelo_timeout", sess=sess["id"], provedor=provedor)
    st = sess.setdefault("barge_stats", {"tipos": {}, "decisoes": [], "modelo": []})
    st["modelo_timeouts"] = int(st.get("modelo_timeouts") or 0) + 1


#: piso de prazo do filtro `modelo` no provedor `dsh` (ver `_live_modelo_dsh`)
_MODELO_DSH_PRAZO_MIN_S = 0.9


def _live_modelo_dsh(sess: dict, texto: str, eco_txt: str,
                     cfg: dict) -> tuple[str | None, float | None]:
    """`provedor=dsh`: mesma pergunta, pelo LLM do chat — FORA DA OFERTA desde o
    #596 (o normalizador coerce tudo para `local`). Fica aqui para quando o corpus
    voltar a justificar: medido no corpus real, 91,4 % (64/70), empate com a régua
    de regras e 900 ms de prazo.

    PRAZO REAL (#596): `_chat_llm` é bloqueante e `timeout_ms` era ignorado — a 1ª
    chamada medida levou 2 574 ms com o turno pagando tudo. Agora a chamada roda em
    thread e o prazo do settings é o TETO: estourou, a cadeia segue (nunca segura o
    turno). Thread daemon, sem cancelamento: o custo restante é do processo."""
    import re as _re
    if not texto:
        return None, None
    # PISO de 900 ms SÓ no dsh (#596): o barramento ACP custa ~350 ms por si
    # (medido: p50 371 ms), então com o `timeout_ms` do local (400) um quarto dos
    # trechos estourava e o filtro não decidia — 70 % de acerto contra 91,4 % com
    # o piso. O teto do duck são 2 s, então 900 ms ainda cabe com folga.
    prazo = max(_MODELO_DSH_PRAZO_MIN_S, float(cfg.get("timeout_ms") or 400) / 1000.0)
    caixa: dict = {}

    def _roda():
        try:
            caixa["bruto"] = _chat_llm([
                {"role": "system", "content": "Responda só o número."},
                {"role": "user",
                 "content": _PROMPT_MODELO.format(ref=eco_txt[:400], fala=texto[:400])},
            ])
        except Exception as exc:          # noqa: BLE001 — o erro volta pela caixa
            caixa["erro"] = exc

    t0 = time.monotonic()
    th = threading.Thread(target=_roda, daemon=True, name="barge-modelo-dsh")
    th.start()
    th.join(prazo)
    if th.is_alive():
        _live_modelo_timeout(sess, "dsh")
        return None, None
    if "erro" in caixa:
        _live_log_kv("barge_modelo_erro", sess=sess["id"], provedor="dsh",
                     erro=str(caixa["erro"])[:100])
        return None, None
    bruto = caixa.get("bruto")
    m = _re.search(r"([01](?:[.,]\d+)?)", str(bruto or ""))
    if not m:
        return None, None
    p = float(m.group(1).replace(",", "."))
    ms = int((time.monotonic() - t0) * 1000)
    sess.setdefault("barge_stats", {"tipos": {}, "decisoes": [], "modelo": []})["modelo"].append(ms)
    return ("interrupcao" if p >= float(cfg.get("limiar") or 0.5) else "backchannel"), p


def _live_modelo_decide(sess: dict, texto: str, fala_ms: int, eco_txt: str,
                        cfg: dict) -> tuple[str | None, float | None]:
    """Slot do filtro `modelo` (#509). Sem classificador instalado devolve
    `(None, None)` e a cadeia segue — nunca segura o turno."""
    decide = globals().get("_live_classificador_barge")
    # EGRESS (#509): `provedor=local` NUNCA sai da máquina — não usa o dsh nem
    # como fallback. Só `provedor=dsh` chama remoto, e aí é o LLM da conversa.
    provedor = "dsh" if str(cfg.get("provedor") or "local").lower() == "dsh" else "local"
    if provedor == "dsh" and decide is None:
        return _live_modelo_dsh(sess, texto, eco_txt, cfg)
    if decide is not None and provedor == "dsh":
        pass                              # dublê injetado manda nos dois casos
    cli = None
    if decide is None:
        # classificador real (audio-ml): UM worker por processo do app (#595 —
        # antes cada sessão instanciava o seu: ~2,2 GB de RSS cada e ninguém
        # fechava). `pega()` conta a referência; o `solta()` sai no fechamento da
        # sessão e o `fecha_tudo()` no shutdown. `aquece` aqui é barato quando o
        # worker está vivo e respeita o cooldown quando ele morreu.
        try:
            import live_intencao
            if not sess.get("intencao_ref"):
                sess["intencao"] = live_intencao.pega()
                sess["intencao_ref"] = True
            cli = sess["intencao"]
        except Exception as exc:          # noqa: BLE001 — sem classificador, segue
            _live_log_kv("barge_modelo_indisponivel", sess=sess["id"],
                         erro=str(exc)[:100])
            return None, None
        if not cli.aquece(espera_s=5.0):
            return None, None
        # `timeout_ms` do settings é POR CHAMADA: o worker é compartilhado, então
        # o prazo entra na instância antes de decidir (#595/#510)
        try:
            cli._timeout = max(0.05, float(cfg.get("timeout_ms") or 400) / 1000.0)
        except (TypeError, ValueError):
            pass
    limite = float(cfg.get("limiar") or 0.5)
    t0 = time.monotonic()
    try:
        if cli is None:
            p = decide(texto, fala_ms, ref_eco=eco_txt)
        else:
            r = cli.decide(eco_txt, texto) or {}
            p = r.get("p_cortar")
    except Exception as exc:              # noqa: BLE001 — timeout/erro segue adiante
        _live_log_kv("barge_modelo_erro", sess=sess["id"], erro=str(exc)[:100])
        st = sess.setdefault("barge_stats", {"tipos": {}, "decisoes": [], "modelo": []})
        st["modelo_timeouts"] = int(st.get("modelo_timeouts") or 0) + 1
        return None, None
    ms = int((time.monotonic() - t0) * 1000)
    st = sess.setdefault("barge_stats", {"tipos": {}, "decisoes": [], "modelo": []})
    st["modelo"].append(ms)
    if p is None:
        return None, None
    return ("interrupcao" if p >= limite else "backchannel"), float(p)


def _live_stats_barge(sess: dict) -> dict:
    """Contadores da decisão de barge (#501): tipos, p50 da decisão e do modelo."""
    st = sess.get("barge_stats") or {}
    dec = sorted(st.get("decisoes") or [])
    mod = sorted(st.get("modelo") or [])

    def p50(v):
        return v[len(v) // 2] if v else None

    return {"tipos": dict(st.get("tipos") or {}), "decisoes": len(dec),
            "decisao_p50_ms": p50(dec), "modelo": len(mod),
            "modelo_p50_ms": p50(mod),
            "modelo_timeouts": int(st.get("modelo_timeouts") or 0)}


def _live_fala_ms(sess: dict, ev) -> int:
    """Duração da fala em curso (ms) — para a decisão que não vem do `speech_end`.

    O `barge_suspeito` NÃO carrega `fala_ms` (o motor só o põe no `speech_end`),
    então toda decisão pelo PRAZO ou pelas passadas de comando chegava com 0: o
    `fala_sustentada` (>=1200 ms) nunca disparava e a decisão saía pelo TEXTO de
    um fragmento de 200 ms — medido: o mesmo "Eu preciso resolver" cortando a
    mesma fala quatro vezes seguidas. O motor sabe quanto já durou; ele conta os
    frames, então é ele que responde."""
    motor = sess.get("engine")
    try:
        ms = int(motor.fala_ms_atual())
    except Exception:                    # noqa: BLE001 — dublê sem o método
        ms = 0
    return ms or int(getattr(ev, "fala_ms", 0) or 0)


def _live_suspeita_barge(sess: dict, ev) -> None:
    """Onset durante o playback, sem decisão ainda (#501).

    Guarda o áudio e arma o PRAZO: quem não chegar a `speech_end` (fala que não
    fechou, STT rápido demais para a janela) decide pelo que tiver. O timer é
    cancelado se o `speech_end` chegar antes."""
    # SUSPEITA NOVA SUBSTITUI A VELHA (#870): a fusão (#802) reentra aqui com a
    # anterior ainda viva, e o timer e a thread de passadas dela seguiam rodando —
    # medido no mini: ~22 suspeitas em 1 s, ~22 timers decidindo e ~22 laços de
    # STT disputando o `_stt_lock`; a decisão real saiu em 13 s.
    velho = sess.pop("barge_timer", None)
    if velho is not None:
        velho.cancel()
    velho = sess.pop("barge_parar", None)
    if velho is not None:
        velho.set()
    sess["barge_suspeito"] = {"audio": bytes(getattr(ev, "audio", b"") or b""),
                              "t": time.monotonic()}
    # zera o marcador da janela incremental: sem isto a suspeita NOVA herdava a
    # posição da anterior e a 1ª passada já nascia no fim do buffer (#547)
    sess["barge_cru_ate"] = 0
    motor = sess.get("engine")
    prazo = getattr(getattr(motor, "config", None), "barge_decisao_ms", 700) / 1000.0
    # a época vai junto: o timer de uma suspeita já fundida não decide pela nova
    epoca = getattr(ev, "epoca", None)
    can = threading.Timer(prazo, lambda: _live_decide_barge(
        sess, b"", _live_fala_ms(sess, ev), por_prazo=True, epoca=epoca))
    can.daemon = True
    sess["barge_timer"] = can
    can.start()
    # ADENDO: comando cadastrado corta NA HORA. Em vez de esperar a janela
    # inteira, o trecho é transcrito em passos de `passo_ms` e, se um filtro de
    # comando disparar, a decisão sai ali (p50 ≤600 ms no smoke de latência).
    # 150 ms: o STT custa ~300-500 ms, então passo maior só empurra o p50 para
    # cima sem economizar nada (medido: 250→712 ms, 150→543 ms)
    cfg_corte = (_settings.get("live_barge_filtros") or {}).get("palavras_corte") or {}
    passo = int(cfg_corte.get("passo_ms", 150))
    # Passadas só existem para o filtro de COMANDO: desligado, cada passada era
    # whisper a cada 150 ms disputando a GPU com a decisão e o TTS sem nunca
    # poder decidir nada (#870).
    if passo > 0 and cfg_corte.get("on", True):
        parar = threading.Event()
        sess["barge_parar"] = parar

        def _passos():
            # 1ª passada IMEDIATA: o comando costuma vir no ataque e o STT já é o
            # gargalo (~300-500 ms) — esperar um passo antes só soma
            enquanto = 0.0
            while not parar.wait(enquanto):
                enquanto = passo / 1000.0
                if not sess.get("barge_suspeito"):
                    return
                _live_decide_barge(sess, b"", _live_fala_ms(sess, ev),
                                   so_comando=True)
                if not sess.get("barge_suspeito"):
                    return

        th = threading.Thread(target=_passos, daemon=True)
        sess["barge_passo_thread"] = th
        th.start()


#: filtros da cadeia que dependem do TRANSCRIPT (o `ruido` também: ele lê o
#: texto VAZIO como falha do STT e corta). `identidade` e `fim_de_resposta` não.
_BARGE_FILTROS_DE_TEXTO = ("palavras_corte", "eco", "backchannel", "ruido", "modelo",
                           "pergunta", "frase_longa", "fala_sustentada")


def _live_barge_le_texto(filtros: dict) -> bool:
    """Algum filtro ligado precisa do STT? (mesmo default `on` da cadeia)."""
    for nome in _BARGE_FILTROS_DE_TEXTO:
        cfg = filtros.get(nome)
        padrao = nome != "modelo"
        if (padrao if cfg is None else bool(cfg.get("on", padrao))):
            return True
    return False


def _live_decide_barge(sess: dict, audio_cru: bytes, fala_ms: int,
                       por_prazo: bool = False, so_comando: bool = False,
                       epoca: int | None = None) -> None:
    """Decide a suspeita pelo CONTEÚDO e emite o desfecho (#501).

    Segue a ordem do card: fala longa corta sem esperar STT; depois um filtro por
    vez; `sem_decisao` decide quando nada decidiu. Timeout/erro do filtro `modelo`
    caem no próximo. Sempre emite `barge_decisao` com o tipo."""
    sus = sess.get("barge_suspeito")
    if sus is None:
        return
    if epoca is not None and sess.get("barge_epoca") != epoca:
        # DECISÃO VELHA (#802): a suspeita já foi substituída — não consome nada e
        # não decide por cima da nova (era a decisão em dobro e a fala sumindo no
        # `_reset_turno` do `barge_retoma`).
        return
    if so_comando:
        # passo intermediário: só o filtro de COMANDO pode fechar agora
        # O áudio tem de ser o ACUMULADO da suspeita, não o retrato do onset: o
        # evento `barge_suspeito` congela o pré-roll e a fala continua chegando
        # depois. Lendo só o retrato, `barge_cru_ate` já nascia no fim do buffer
        # na 1ª passada e todas as seguintes reliam o mesmo rabo (#547).
        motor_sus = sess.get("engine")
        audio = ((motor_sus.audio_suspeito() if motor_sus is not None else b"")
                 or audio_cru or sus.get("audio") or b"")
        # INCREMENTAL: só a janela NOVA desde a última passada (+0,2 s de
        # sobreposição para não cortar palavra no meio). Re-transcrever o trecho
        # inteiro a cada passo fazia o custo crescer durante a fala.
        desde = int(sess.get("barge_cru_ate") or 0)
        novo = audio[max(0, desde - 6400):]
        sess["barge_cru_ate"] = len(audio)
        texto = _live_transcreve_trecho(sess, novo, cru=True, prazo_s=0.6)
        # os filtros RÁPIDOS que não decidem no passo ficam desligados, mas a lista
        # de palavras é a CONFIGURADA (o dono cadastra o que quiser)
        rapidos = dict(_settings.get("live_barge_filtros") or {})
        for lento in ("modelo", "pergunta", "frase_longa", "fala_sustentada"):
            rapidos[lento] = {"on": False}
        tipo, _p, _m, filtro = _live_avalia_filtros(sess, texto, fala_ms, rapidos,
                                                     audio=audio)
        if filtro not in ("palavras_corte",):
            return
        sess.pop("barge_suspeito", None)
        can = sess.get("barge_timer")
        if can is not None:
            can.cancel()
        eventos = sess.get("engine").decidir_barge(
            tipo, texto=texto, fala_ms=fala_ms, filtro=filtro) if sess.get("engine") else []
        _live_log_kv("barge_decisao", sess=sess["id"], tipo=tipo, filtro=filtro,
                     fala_ms=fala_ms, decisao_ms=0, texto=(texto or "")[:80])
        if tipo == "interrupcao":
            _live_cancela(sess, forcar=True,
                          fala_ms=_live_fala_ms_corte(sess, fala_ms))
        if eventos:
            _live_trata_eventos(sess, eventos)
        _live_conta_barge(sess, tipo, 0, None)
        return
    sus = sess.pop("barge_suspeito", None)
    if sus is None:
        return
    parar = sess.pop("barge_parar", None)
    if parar is not None:
        parar.set()
    can = sess.pop("barge_timer", None)
    if can is not None and not por_prazo:
        can.cancel()
    motor = sess.get("engine")
    filtros = _settings.get("live_barge_filtros") or {}
    # O áudio tem de ser o ACUMULADO: pelo PRAZO (`por_prazo`) o handler só tem o
    # retrato do onset (pré-roll, ~400 ms) e a fala chegou DEPOIS, no motor. Sem
    # isto a decisão transcrevia só o pré-roll e saía lixo ("Tchau.", "É isso.")
    # no lugar do comando — nada casava `palavras_corte` (#547).
    audio = (audio_cru
             or (motor.audio_suspeito() if motor is not None else b"")
             or sus.get("audio") or b"")
    t0 = time.monotonic()
    tipo, filtro = "ruido", "sem_decisao"
    texto, p_cortar, modelo_ms = "", None, None
    try:
        # "SÓ A MINHA VOZ" (#533): quem não é o dono não corta e não vira turno —
        # é o PRIMEIRO elo (a TV ou outra pessoa não pode cortar o assistente).
        # Mesma verificação que o `_stt_app` usa no turno (uma só, não duas).
        voz = _voz_do_dono(audio) if _live_voz_ligado() else None
        if voz is not None:
            _live_voz_anota(sess, voz)
        # ADENDO: perto do fim da resposta a fala vira turno pendente em vez de
        # cortar o restinho — cortar ali só encurta a frase sem ganhar nada.
        # A FALA LONGA atalha o STT, mas não pode atalhar o `fim_de_resposta`: ele
        # vem ANTES do `fala_sustentada` na precedência do PM (#503) e, com o
        # atalho na frente, o último segundo nunca chegava ao pendente — 3 s de
        # fala no fim cortavam a resposta em vez de anotar (#547).
        restante = motor.playback_restante_ms() if motor is not None else None
        perto_do_fim = (restante is not None and (filtros.get("fim_de_resposta") or {}).get(
            "on", True) and restante <= int(
                (filtros.get("fim_de_resposta") or {}).get("restante_ms", 2000)))
        if voz is not None and not voz.get("ok"):
            tipo, filtro = "ruido", "identidade"
        elif (fala_ms >= 1200 and (filtros.get("fala_sustentada") or {}).get("on", True)
                and not perto_do_fim):
            tipo, filtro = "interrupcao", "fala_sustentada"
        else:
            # Sem filtro que LEIA texto ligado, o transcript não muda o desfecho:
            # o STT era só custo (medido no mini com a cadeia toda desligada:
            # 13 s de whisper para sair `sem_decisao`, #870).
            texto = (_live_transcreve_trecho(sess, audio)
                     if _live_barge_le_texto(filtros) else "")
            tipo, p_cortar, modelo_ms, filtro = _live_avalia_filtros(
                sess, texto, fala_ms, filtros)
            if filtro == "fim_de_resposta":
                # a fala não pode sumir: entra no PENDENTE de verdade (mesma
                # máquina do #126, com buffer e contadores) e o turno abre quando
                # o playback acabar — antes só saía um JSON ao cliente (#569).
                # O `fim_de_resposta=True` põe o motivo no evento: sem ele o
                # caminho era indistinguível de "pipeline ocupado".
                _live_guarda_pendente(sess, audio, barge_in=True,
                                      fim_de_resposta=True)
    except Exception as exc:              # noqa: BLE001 — decisão nunca derruba o turno
        _live_log_kv("barge_decisao_erro", sess=sess["id"], erro=str(exc)[:120])
        tipo = str(_settings.get("live_barge_sem_decisao") or "continua")
        tipo = "interrupcao" if tipo == "interrompe" else "ruido"
        filtro = "erro"
    decisao_ms = int((time.monotonic() - t0) * 1000)
    if (tipo != "interrupcao" and filtro == "sem_decisao" and audio
            and motor is not None and motor.playback_restante_ms() == 0):
        # A RESPOSTA ACABOU DURANTE A DECISÃO (#870): não há mais nada a proteger,
        # e descartar o trecho jogava fora fala real — medido no mini: "Tchau."
        # (672 ms) decidido com `restante_ms=0` virou `ruido` e sumiu. Vai para o
        # PENDENTE com o flag de barge (o turno passa pelo STT + juiz + eco-check
        # do turno; eco ou alucinação morrem lá, com motivo). Só no `sem_decisao`:
        # eco/backchannel/identidade decidiram de propósito.
        _live_guarda_pendente(sess, audio, barge_in=True)
        filtro = "playback_acabou"
    # o modo `imediata`/`so_comando` já filtrou; aqui só o desfecho
    eventos = []
    if motor is not None:
        eventos = motor.decidir_barge(tipo, texto=texto, fala_ms=fala_ms,
                                      decisao_ms=decisao_ms, p_cortar=p_cortar,
                                      modelo_ms=modelo_ms, filtro=filtro, epoca=epoca)
    _live_log_kv("barge_decisao", sess=sess["id"], tipo=tipo, filtro=filtro,
                 fala_ms=fala_ms, decisao_ms=decisao_ms, texto=(texto or "")[:80],
                 # `restante` é o que separa "cortar" de "anotar" (fim_de_resposta):
                 # sem ele no log a decisão não se explica pelo estado do playback
                 restante_ms=restante,
                 # `trecho_ms` (#707): o VAD do STT exige ~300 ms de fala — abaixo
                 # disso o trecho volta VAZIO em 3 ms e a decisão fica sem texto.
                 # Sem o número no log o trecho curto e o STT lento são idênticos.
                 trecho_ms=len(audio) // 32)
    if tipo == "interrupcao":
        # o áudio do trecho vai JUNTO com o turno que vem depois: o pendente só
        # recebia a cauda (224 ms medidos) e o VAD recusava o turno como `vad`,
        # perdendo a fala do dono (#674)
        if audio:
            sess["barge_audio"] = audio
        _live_cancela(sess, forcar=True,
                      fala_ms=_live_fala_ms_corte(sess, fala_ms))
    if eventos:
        _live_trata_eventos(sess, eventos)
    elif tipo != "interrupcao" and sess.get("turno"):
        pass                               # retomada: a resposta segue
    _live_conta_barge(sess, tipo, decisao_ms, modelo_ms)


#: janela da CONTINUAÇÃO (#877), contada da abertura do turno anterior. O guarda
#: principal é "nenhum áudio da resposta saiu"; a janela só impede fundir uma fala
#: NOVA com um turno cujo LLM demorou muito (aí ela é outra pergunta: pendente).
_LIVE_CONTINUACAO_MS = int(os.environ.get("TTS_LIVE_CONTINUACAO_MS", "2500") or 0)


def _live_tenta_continuacao(sess: dict) -> bool:
    """Fala recomeçou com o turno anterior ainda SEM resposta audível (#877).

    O motor fecha o turno em `silence_ms` (600) e uma pausa de respiração no meio
    de uma frase longa passa disso: o pipeline respondia à 1ª metade e a resposta
    começava POR CIMA da 2ª (bancada E2E, #875). Aqui o turno em voo é cancelado
    sem gravar history e o áudio dele volta para a frente do próximo turno — o
    STT transcreve a frase inteira. Depois do 1º chunk de resposta não há
    continuação: a fala por cima é barge e segue a cadeia do #870."""
    cont = sess.get("continuavel")
    pipe = sess.get("pipe")
    if not cont or pipe is None or not getattr(pipe, "ocupado", False):
        return False
    if sess.get("audio_enviado", 0) != cont["audio0"]:
        return False                       # a resposta já soou: é barge
    idade_ms = int((time.monotonic() - cont["t"]) * 1000)
    if _LIVE_CONTINUACAO_MS <= 0 or idade_ms > _LIVE_CONTINUACAO_MS:
        return False
    sess.pop("continuavel", None)
    sess["continuacao_audio"] = cont["pcm"]
    pipe.cancel(sem_historico=True)
    _live_log_kv("turno_continuado", sess=sess["id"], idade_ms=idade_ms,
                 bytes=len(cont["pcm"]))
    return True


def _live_conta_barge(sess: dict, tipo: str, decisao_ms: int,
                      modelo_ms: int | None) -> None:
    st = sess.setdefault("barge_stats", {"tipos": {}, "decisoes": [], "modelo": []})
    st["tipos"][tipo] = st["tipos"].get(tipo, 0) + 1
    st["decisoes"].append(decisao_ms)
    if modelo_ms is not None:
        st["modelo"].append(modelo_ms)


def _live_trata_eventos(sess: dict, eventos) -> None:
    """Traduz a FSM (speech_start | speech_end | barge_in) para o protocolo.

    `barge_in` e o `speech_start{barge_in:true}` vêm no MESMO lote: o primeiro
    derruba o turno em curso, o segundo é a única abertura de turno.

    LATÊNCIA: `t_ms` é RETROAGIDO ao início do áudio (é posição no stream); a
    latência real da decisão é `t_decisao_ms`, que fica registrado na sessão — é
    dele que sai o orçamento do LIVE-3 (fim-de-fala → 1º áudio), não do `t_ms.
    DESCARTE: quem decide agora é o PIPELINE (compara o transcript com o texto do
    assistente em reprodução: casar = eco → `turn_complete{descartado:true}`;
    diferir = humano → segue). `curto`/`barge_falso` são dica e não barram mais o
    STT — a bancada mostrou fala REAL de 384 ms marcada como curta e eco puro de
    1280 ms passando como turno fantasma."""
    for ev in eventos:
        try:
            dados = ev.to_json()
        except Exception:                # noqa: BLE001 — evento de outro tipo
            continue
        if ev.tipo in ("barge_pausa", "barge_retoma"):
            # #870 S3 / #881: `turn` é o da RESPOSTA que está tocando, não um
            # turno novo do usuário. `fala_ms` e `epoca` vêm do motor.
            dados["turn"] = sess.get("turno", 0)
            dados["fala_ms"] = int(getattr(ev, "fala_ms", 0) or 0)
            epoca = int(getattr(ev, "epoca", 0) or 0)
            if epoca:
                dados["epoca"] = epoca
        _live_envia_json(sess, dados)
        _live_marca_prov(sess, getattr(ev, "prob", 0.0))
        if ev.tipo == "barge_suspeito":
            # #501: só avisa o cliente (duck). O corte vem na decisão.
            # #870 S3: o duck rápido usa o MESMO tipo (o cliente já abaixa), mas
            # não é suspeita — armar timer e whisper aqui reabria a rajada e a
            # janela incremental (#547). A suspeita de verdade não traz este detalhe.
            _live_log_kv("barge_suspeito", sess=sess["id"], t_ms=ev.t_ms,
                         detalhe=getattr(ev, "detalhe", None) or None)
            if getattr(ev, "detalhe", "") != "duck rapido":
                _live_suspeita_barge(sess, ev)
        elif ev.tipo == "barge_pausa":
            # Pausa reversível. O timer e o whisper seguem: o cancel só sai
            # da decisão por conteúdo.
            _live_log_kv("barge_pausa", sess=sess["id"], t_ms=ev.t_ms,
                         fala_ms=getattr(ev, "fala_ms", None),
                         epoca=getattr(ev, "epoca", None))
        elif ev.tipo == "barge_in":
            _live_log_kv("barge_in", sess=sess["id"], t_ms=ev.t_ms)
            # `forcar`: o barge do motor é prova de interrupção (o evento chega
            # antes de qualquer áudio no fio; ver `_live_cancela`).
            # `fala_ms` só no corte por conteúdo (#881) e só com a flag.
            # Flag off, ou o caminho antigo (fala_ms 0): o fio omite o campo.
            fala = int(getattr(ev, "fala_ms", 0) or 0)
            _live_cancela(sess, forcar=True,
                          fala_ms=_live_fala_ms_corte(sess, fala or None))
        elif ev.tipo == "speech_start":
            _live_log_kv("speech_start", sess=sess["id"], t_ms=ev.t_ms,
                         prob=getattr(ev, "prob", None))
            if not getattr(ev, "barge_in", False):
                _live_tenta_continuacao(sess)
        elif ev.tipo == "speech_end":
            _live_log_kv("speech_end", sess=sess["id"], t_ms=ev.t_ms,
                         fala_ms=getattr(ev, "fala_ms", None),
                         curto=getattr(ev, "curto", None),
                         barge_falso=getattr(ev, "barge_falso", None),
                         detalhe=getattr(ev, "detalhe", None))
            if sess.get("barge_suspeito"):
                # o trecho suspeito acabou: decide pelo conteúdo, antes de abrir
                # qualquer turno. EM THREAD (#674): a decisão transcreve o trecho
                # no caminho COMPLETO (VAD + whisper + juiz) e, feita aqui na
                # linha, travava o loop de eventos inteiro — os frames do cliente
                # ficavam parados no socket 12–14 s e o áudio da resposta nova não
                # saía. O timer da suspeita já chama esta função de outra thread,
                # então o caminho cruzado é o mesmo de antes.
                _audio_sus = _live_audio_cru(sess, ev)
                _fala_ms_sus = int(getattr(ev, "fala_ms", 0) or 0)
                threading.Thread(
                    target=_live_decide_barge, args=(sess, _audio_sus, _fala_ms_sus),
                    name="barge-decide", daemon=True).start()
                continue
            _live_stage(sess, "stt")
            sess["t_decisao_ms"] = int(getattr(ev, "t_decisao_ms", 0) or 0)
            # #315: relógio da DECISÃO. O alvo do épico é "fim-de-fala → 1º áudio",
            # e o `latency` do pipeline conta de outro zero (o início da thread) —
            # sem este carimbo o trecho entre a decisão do VAD e o turno abrir fica
            # fora de qualquer medida. `_live_observa` fecha a conta no `latency`.
            sess["t_decisao"] = time.monotonic()
            sess["turno_curto"] = bool(getattr(ev, "curto", False))
            sess["turno_barge_falso"] = bool(getattr(ev, "barge_falso", False))
            # áudio do STT: CRU (a decisão já foi tomada no filtrado, #481)
            _live_abre_turno(sess, _live_audio_cru(sess, ev),
                             barge_in=bool(getattr(ev, "barge_in", False)))


def _dbfs16(pcm: bytes) -> float:
    """dBFS de um chunk PCM16 — o detector de eco calibra o nível pelo playback."""
    import numpy as np
    if len(pcm) < 2:
        return -120.0
    a = np.frombuffer(pcm[:len(pcm) - (len(pcm) % 2)], dtype="<i2").astype("float32")
    if a.size == 0:
        return -120.0
    rms = float(np.sqrt(float((a * a).mean())))
    return 20.0 * float(np.log10(max(rms, 1e-6) / 32768.0))


# Taxa do áudio do TTS NO FIO (o pipeline publica em `model.sample_rate`, 24 kHz
# para o catálogo atual). O sender só tem os BYTES do chunk — daí a constante aqui:
# é dela que sai a duração real do chunk que dimensiona a janela de playback (#167).
_LIVE_TTS_RATE = 24000


def _live_chunk_ms(pcm: bytes) -> float:
    """Duração REAL do chunk PCM16 (ms) — o backlog de playback do motor (#167).

    O cliente BUFFERIZA: entre o envio e o alto-falante há a fila inteira, então a
    janela de barge tem de cobrir o áudio já mandado. Duração por bytes, sem
    decodificar — o motor só precisa da estimativa de tempo."""
    return len(pcm) / 2 / (_LIVE_TTS_RATE / 1000.0)


def _live_ref_16k(pcm: bytes):
    """PCM16 24 kHz do fio → float32 16 kHz, a taxa do mic (#870).

    A bancada reamostra o playback com `resample_poly(..., 2, 3)` antes de
    reinjetar o eco. A mesma razão aqui: sem isso a correlação não casa com o
    que volta no microfone. Vazio se não houver amostra.

    Passo 1,5 EXATO por chunk (2400 → 1600) em vez de `_reamostra`: o
    `resample_poly` reinicia a fase do filtro a cada chunk de 100 ms e a
    correlação do eco com a referência cai para ~0,5 (medido) — o duck então
    trata a sílaba do próprio TTS como voz.
    """
    import numpy as np

    if len(pcm) < 2:
        return np.zeros(0, dtype=np.float32)
    n = len(pcm) - (len(pcm) % 2)
    a = np.frombuffer(pcm[:n], dtype="<i2").astype(np.float32) / 32768.0
    if len(a) >= 3 and _LIVE_TTS_RATE != 16000:
        pos = np.arange(int(len(a) * 16000 / _LIVE_TTS_RATE),
                        dtype=np.float32) * (_LIVE_TTS_RATE / 16000.0)
        return np.interp(pos, np.arange(len(a), dtype=np.float32), a).astype(np.float32)
    return a


def _live_eco_dir() -> str:
    """O mesmo env do dump do turno (#869). Vazio = desligado, sem import."""
    return (os.environ.get("TTS_LIVE_DUMP_DIR") or "").strip()


def _live_eco_mic(sess: dict, pcm: bytes) -> None:
    """Cru no dump do eco. No-op sem o env. Nunca levanta."""
    if not pcm or not _live_eco_dir():
        return
    try:
        import live_eco_dump
        live_eco_dump.anota_mic(sess, pcm)
    except Exception:  # noqa: BLE001 — diagnóstico não derruba o frame
        pass


def _live_eco_ref(sess: dict, ref) -> None:
    """O float que o motor acabou de guardar. No-op sem o env."""
    if not _live_eco_dir():
        return
    try:
        if ref is None or len(ref) == 0:
            return
    except TypeError:
        return
    try:
        import live_eco_dump
        live_eco_dump.anota_ref(sess, ref)
    except Exception:  # noqa: BLE001
        pass


def _live_eco_marca(sess: dict, eng) -> None:
    """Relógio do playhead. No-op sem o env ou sem motor."""
    if eng is None or not _live_eco_dir():
        return
    try:
        import live_eco_dump
        live_eco_dump.marca(sess, eng)
    except Exception:  # noqa: BLE001
        pass


def _live_anota_ref_envio(sess: dict, eng, payload: bytes) -> None:
    """Referência do que saiu no fio. Sem flag e sem dump, não roda.

    O reamostrado e o ring de 60 s só existem para o duck ou para o dump
    do eco. Nos dois desligados o sender fica o da main: sem interp, sem
    lock extra no motor.
    """
    if eng is None:
        return
    if not (getattr(getattr(eng, "config", None), "duck_rapido", False)
            or _live_eco_dir()):
        return
    anota = getattr(eng, "anota_referencia", None)
    if anota is None:
        return
    try:
        ref16 = _live_ref_16k(payload)
        anota(ref16)
    except Exception:  # noqa: BLE001 — sem referência vale o nível
        pass
    else:
        # o dump grava o MESMO float, e só se o motor aceitou
        _live_eco_ref(sess, ref16)


def _live_eco_fecha(sess: dict) -> None:
    """Fecha o dump se a sessão chegou a abrir um. Não importa à toa."""
    if sess.get("_eco_dump") is None and not _live_eco_dir():
        return
    try:
        import live_eco_dump
        live_eco_dump.fecha(sess)
    except Exception:  # noqa: BLE001
        pass


def _live_pipe_novo(sess: dict):
    """Pipeline da sessão (None quando o módulo não está disponível -> stub).

    Os callbacks do pipeline caem na MESMA fila da sessão, então a única task que
    escreve no WS continua sendo a de envio (nada de send concorrente).

    DSH-2: com `chat_backend=dsh` a sessão ganha um `DshClient` PRÓPRIO (não o pool
    da Conversa): o `cancel` do barge-in casa por sessão e não vaza entre clientes,
    e o processo é fechado junto com a sessão. Cliente por sessão + `prewarm` no
    `_live_pipe_start` = boot de 17-18 s a frio FORA do turno. Construir o cliente
    é barato (não sobe processo aqui); se algo falhar, a sessão segue no backend
    openai em vez de nascer muda."""
    if _live_mod is None:
        return None
    historico = [{"role": h["role"], "content": h["text"]} for h in sess["history"]]
    dsh = None
    bridge, teto = "unknown", None
    if _chat_backend_live() == "dsh":
        travado = _dsh_latch("handshake do dsh falhou há pouco")
        if travado:
            # falha recente de handshake: não repete as 6 tentativas em cada sessão
            # nova — a sessão nasce e segue no openai (#287)
            sess["dsh_indisponivel"] = travado
            _live_envia_json(sess, {"type": "dsh_indisponivel", "fallback": "openai",
                                    "message": travado[:200]})
            print(f"[live] dsh em latch — sessão no backend openai: {travado}", flush=True)
        else:
            # estado do bridge: `clean` = sem o patch de streaming, a resposta vem
            # INTEIRA no fim e o teto longo do 1º delta é só atraso (#287)
            bridge = _dsh_bridge_estado_cached().get("estado", "unknown")
            teto = _DSH_FIRST_DELTA_S
            if bridge == "clean":
                teto = _DSH_FIRST_DELTA_CLEAN_S
                sess["bridge"] = "clean"
                print(f"[live] bridge ACP SEM patch (clean) — teto do 1º delta "
                      f"encurtado para {teto:g}s", flush=True)
            try:
                cfg = _chat_dsh_cfg()
                dsh = _DshComSlot(
                    bin=cfg["bin"], profile=cfg["profile"], model=cfg["model"],
                    effort=cfg["effort"], cwd=OUTPUTS_DIR / ".dsh-cwd",
                    on_log=lambda m: _dsh_log(m, "live-dsh"))
            except _DshSemSlot as exc:
                # teto GLOBAL: a sessão nasce e segue no openai, mas DIZ isso (#287)
                sess["dsh_ocupado"] = str(exc)
                _live_envia_json(sess, {"type": "dsh_ocupado", "fallback": "openai",
                                        "message": str(exc)[:200]})
                print(f"[live] dsh_ocupado ({exc}) — sessão no backend openai", flush=True)
            except Exception as exc:             # noqa: BLE001 — cai no openai
                print(f"[live] dsh indisponível ({type(exc).__name__}: {exc}) — "
                      f"sessão no backend openai", flush=True)
    sess["dsh"] = dsh
    # O pipeline ainda não existe quando o callback nasce, e ele só recebe o
    # PCM. A caixa deixa o emit ler `_primeiro_pcm` na hora. A chave
    # `live_marca_primeiro_trecho` (default True) decide se esse PCM também
    # leva AudioSeal — False é o atalho do #901.
    caixa: dict = {}

    def _envia_pcm(pcm, _caixa=caixa):
        pipe = _caixa.get("pipe")
        primeiro = bool(getattr(pipe, "_primeiro_pcm", False)) if pipe is not None else False
        _live_envia_audio(sess, pcm, marcar=_live_marca_este_pcm(primeiro))

    pipe = _live_mod.LivePipeline(
        lambda obj: _live_envia_json(sess, obj),
        _envia_pcm,
        voice_id=sess["voice_id"], system=sess["system"] or None, history=historico,
        dsh=dsh, ao_morrer_dsh=_dsh_marca_morto, bridge_estado=bridge,
        teto_primeiro_delta=teto)
    caixa["pipe"] = pipe
    return pipe


def _live_pipe_start(sess: dict) -> None:
    """Pre-warm (whisper + TTS frios custam ~5-7 s): fora do caminho do handshake.

    Avisa quando termina (`prewarm`): o 1º turno ANTES disso paga a compilação dos
    kernels dos modelos (medido: 7,3 s de 1º áudio contra 0,7 s depois) — quem
    mede o alvo do MVP precisa saber quando o pipeline está quente."""
    pipe = sess.get("pipe")
    if pipe is None:
        return
    # separação do turno (#533): aquece junto, atrás do prewarm do pipeline
    _separa_prewarm()
    try:
        pipe.start()
        _live_envia_json(sess, {"type": "prewarm", "ok": True})
    except Exception as exc:             # noqa: BLE001 — pre-warm falho não derruba a sessão
        _live_envia_json(sess, {"type": "prewarm", "ok": False,
                                "message": f"{type(exc).__name__}: {exc}"})
        _live_envia_json(sess, _live_erro("prewarm", f"{type(exc).__name__}: {exc}"))
    finally:
        # #315: solta o turno que está esperando o aquecimento (deu certo ou não,
        # não há mais o que esperar) — sem isto ele só sairia no estouro do prazo.
        sess["prewarm_ok"] = True
        sess["prewarm_esperou"] = True        # não há mais o que esperar
        if sess.get("turno_pendente"):
            _live_acorda_pendente(sess)


def _live_engine(sess: dict) -> None:
    """STUB do pipeline (o #93 substitui esta função).

    Emite o turno mínimo para o protocolo ser exercitável já: `speech_start` →
    `turn_complete`. O áudio que o #93 gerar sai por `_live_envia_audio`."""
    turno = sess["turno"]
    _live_envia_json(sess, {"type": "speech_start", "turn": turno})
    if sess["cancelado"].is_set():
        return
    _live_envia_json(sess, {"type": "turn_complete", "turn": turno, "stub": True,
                            "audio_bytes": 0, "buffer_bytes": len(sess["buffer"])})


def _live_motivo_fechamento(sess: dict) -> tuple[str, str] | None:
    """Por que a task de envio deve fechar o socket (None = a sessão segue).

    ORDEM (#628): `substituida` vence TODAS — é o motivo REAL (outra conexão
    assumiu o id) e o cliente precisa saber para NÃO retomar a sessão que já tem
    dono. Antes `vencida` vinha antes e a substituída-e-vencida saía como
    `session_ttl`, fazendo a aba antiga reabrir o id da nova.

    `fechar` tem código PRÓPRIO (`session_fechada`): era `session_ttl` com o texto
    "ociosa", que mente quando quem morreu foi a task de envio por outro motivo.
    `session_ttl` fica só para o vencimento de verdade (o único em que o cliente
    DEVE retomar)."""
    if sess.get("substituida"):
        return "session_substituida", ("outra conexão assumiu este session_id; "
                                       "esta foi fechada pelo servidor")
    if sess["vencida"]:
        return "session_ttl", f"sessão ociosa por {_LIVE_TTL_S}s"
    if sess.get("fechar"):
        return "session_fechada", "a sessão foi encerrada pelo servidor"
    return None


def _live_libera_sessao(sess: dict, motivo: str) -> None:
    """Devolve o que a sessão tomou emprestado e a tira do registry.

    É a parte COMUM entre a morte da task de envio (`_live_solta_slot`) e o
    `finally` do WS (cliente que fecha limpo) — antes só a primeira existia, e no
    fechamento normal a referência do classificador de intenção NUNCA voltava:
    `_refs` ficava acima de zero e o worker de 3,8 GB só saía pelo TTL.

    Idempotente: o pop de `intencao_ref` e a checagem de dono do registry
    garantem que duas chamadas não devolvem duas vezes nem apagam a sessão nova
    de uma retomada (#201)."""
    if sess.pop("intencao_ref", False):
        # devolve a referência do classificador compartilhado (#595): o worker
        # único só fecha quando a última sessão sai (ou pelo TTL)
        try:
            import live_intencao
            live_intencao.solta()
        except Exception:                 # noqa: BLE001 — fechar é best-effort
            pass
    with _live_lock:
        if _live_sessions.get(sess["id"]) is sess:
            _live_sessions.pop(sess["id"], None)
            _live_log_kv("solta_slot", sess=sess["id"], motivo=motivo,
                         fila_bytes=getattr(sess["fila"], "bytes", 0),
                         descartados=getattr(sess["fila"], "descartados", 0))


def _live_solta_slot(sess: dict, motivo: str) -> None:
    """Tira a sessão do registry AGORA e fecha a porta do lado de cá (#300).

    Chamado quando a task de ENVIO morre: a única escritora do WS se foi, mas o
    `_live_loop` pode continuar bloqueado no `receive()` (socket meio-aberto) e a
    limpeza do endpoint só corre quando ele volta. Sem sair do registry o `visto`
    (renovado a cada frame) impede o TTL de vencer e a sessão fica ZUMBÁ: muda,
    segurando 1 de `_LIVE_MAX_SESSIONS` (quem chega leva `busy`) e coletando tudo o
    que a IA gerar. Idempotente com o pop do `finally` (mesma checagem de dono)."""
    sess["fechar"] = True
    sess["cancelado"].set()
    pipe = sess.get("pipe")
    if pipe is not None:
        # ninguém mais vai ouvir: para a geração no próximo estágio, em vez de
        # deixá-la produzir para uma fila que já ninguém lê (o `close()` do
        # `finally` também cancela — a chamada é idempotente de propósito)
        try:
            pipe.cancel()
        except Exception:                 # noqa: BLE001 — cancelar é best-effort
            pass
    _live_libera_sessao(sess, motivo)


async def _live_manda_stats(sess: dict, ws) -> bool:
    """Manda um `stats` AGORA, direto no socket (fora da fila). False = socket morto.

    #313: o `stats` era gerado só quando a fila estava VAZIA e ENFILEIRADO junto do
    áudio. Num turno com áudio contínuo a fila nunca esvazia — então o painel
    congelava exatamente quando é útil — e, mesmo quando saía, o payload esperava
    atrás de todo o áudio pendente. Aqui ele é montado e enviado na hora: só esta
    task escreve no WS, logo não há corrida de `send`.
    """
    sess["st_proximo"] = time.monotonic() + _LIVE_STATS_MS / 1000.0
    try:
        await ws.send_json(_live_stats(sess))
    except Exception:                     # noqa: BLE001 — quem trata a morte é o laço
        return False
    return True


async def _live_sender(sess: dict, ws) -> None:
    """Única task que escreve no WS (evita corrida de send entre turno e eventos)."""
    fila = sess["fila"]
    while True:
        # `stats` por RELÓGIO (#313): a espera do `get` é encurtada até o próximo
        # tique para não depender de a fila drenar.
        espera = _LIVE_TICK_S
        if _LIVE_STATS_MS and sess["visto"]:
            espera = max(0.0, min(espera, sess.get("st_proximo", 0.0) - time.monotonic()))
        try:
            item = await asyncio.to_thread(fila.get, True, espera)
        except queue.Empty:
            item = None
        except asyncio.CancelledError:
            raise
        if _LIVE_STATS_MS and sess["visto"] and \
                time.monotonic() >= sess.get("st_proximo", 0.0):
            if not await _live_manda_stats(sess, ws):
                _live_solta_slot(sess, "sender_morto")
                try:
                    await ws.close(code=1011)
                except Exception:          # noqa: BLE001
                    pass
                return
        if item is not None:
            tipo, payload = item
            try:
                if tipo == "json":
                    await ws.send_json(payload)
                else:
                    await ws.send_bytes(payload)
            except Exception:  # noqa: BLE001 — o socket morreu (a limpeza de recursos
                # é do endpoint, quando o `_live_loop` voltar). O SLOT, porém, é
                # liberado aqui e já: sem consumidora a fila só cresce, e o loop
                # segue renovando `visto` — o TTL nunca venceria (#300). O
                # `ws.close` é o que faz o `receive()` pendente do loop voltar.
                _live_solta_slot(sess, "sender_morto")
                try:
                    await ws.close(code=1011)
                except Exception:      # noqa: BLE001
                    pass
                return
            if tipo == "audio":
                sess["st_chunks"] += 1
                sess["st_audio_bytes"] += len(payload)
                if sess.get("st_stage") in ("stt", "llm"):   # 1º áudio do turno
                    _live_stage(sess, "tts")
                    sess["st_ultimo_chunk"] = None      # #492: não conta o vão entre turnos
                _live_conta_underrun(sess, len(payload))   # #492
                # V1: o playback é marcado NO ENVIO (nivel do chunk mede o eco);
                # `#94` pode trazer feedback real de playback se o falso barge-in pedir.
                eng = sess.get("engine")
                if eng is not None:
                    if not sess.get("falando"):
                        sess["falando"] = True
                    # #167: o cliente BUFFERIZA e ainda vai tocar este chunk — a
                    # janela de barge tem de cobrir a duração REAL dele. A chamada
                    # vai a CADA chunk (não só no 1º do turno): o backlog do motor
                    # é a SOMA do que foi enviado e não tocou, e alimentá-lo só uma
                    # vez deixava a janela pós-turno com um chunk fixo — onset logo
                    # depois do `turn_complete`, com o cliente ainda tocando o
                    # ÚLTIMO trecho (que é longo), virava turno novo (resíduo
                    # medido). Repetir o `True` é inócuo: a recalibração do eco só
                    # dispara na borda (`não speaking` -> `speaking`).
                    eng.set_speaking(True, nivel_dbfs=_dbfs16(payload),
                                     duracao_ms=_live_chunk_ms(payload))
                    # #870: a referência é o que SAI no fio (já com a marca).
                    # Sem a flag e sem TTS_LIVE_DUMP_DIR isto não roda.
                    _live_anota_ref_envio(sess, eng, payload)
                    sess["audio_pendente"] = max(0, sess.get("audio_pendente", 1) - 1)
                    if sess["audio_pendente"] == 0:
                        sess["falando"] = False
                        eng.set_speaking(False)
        motivo = _live_motivo_fechamento(sess)
        if motivo is not None:
            try:
                await ws.send_json({"type": "error", "code": motivo[0],
                                    "message": motivo[1]})
                await ws.close(code=1000)
            except Exception:  # noqa: BLE001
                pass
            return


# ---------------------------------------------------------------------------
# LIVE-5 (#95) — sessão longa: histórico fora da conexão (resume) + compressão de
# contexto. Desenho no comentário da task; aqui o essencial:
#   · `_live_historico[sid]` guarda msgs/voz/system de uma sessão que caiu, com TTL
#     de retomada e tetos (por registro e total, evicção previsível — lição do #24);
#   · `setup.session_id` retoma (o cliente reconecta e continua o contexto);
#   · acima do limiar, a METADE MAIS ANTIGA vira UM resumo (LLM local, em thread,
#     fora do caminho da latência); o system_instruction fica separado e sempre no
#     prompt. Falha do resumo mantém o histórico cru.
# ---------------------------------------------------------------------------
_LIVE_RESUME_TTL_S = max(60, int(os.environ.get("TTS_LIVE_RESUME_TTL_S", "1800")))
_LIVE_HIST_MAX_MSGS = max(4, int(os.environ.get("TTS_LIVE_HIST_MAX_MSGS", "24")))
_LIVE_HIST_MAX_CHARS = max(1000, int(os.environ.get("TTS_LIVE_HIST_MAX_CHARS", "12000")))
_LIVE_MAX_HISTORICOS = max(1, int(os.environ.get("TTS_LIVE_MAX_HISTORICOS", "32")))
#: teto do ACERVO de retomada em CARACTERES (não bytes — `_live_hist_tam` soma
#: `len(str)`, e comparar isso com um número dito em bytes subestimava o
#: consumo: 4 MiB de ASCII ≈ 4 M chars, mas 1 char acentuado ocupa 2+ bytes no
#: buffer do Python/JSON). O teto por CONTAGEM (32 registros) mascara hoje; vale
#: quando o resumo falha e o histórico cresce (#282).
_LIVE_RESUME_MAX_CHARS = 4 * 1024 * 1024
_live_historico: dict = {}          # sid -> {msgs, resumo, voz, system, visto, bytes}
_live_resume_fn = None              # callable(msgs) -> str; default = _chat_llm


def _live_hist_tam(reg: dict) -> int:
    """CHARS do registro retomável — `msgs` MAIS o `system` e o `resumo` (#313).

    Contar só as mensagens deixava os dois campos de fora e o teto de
    `_LIVE_RESUME_MAX_CHARS` (4 M chars) subcontava: o `system` são até 4000 chars por
    sessão que ficavam invisíveis para a evicção."""
    total = sum(len(m.get("content") or m.get("text") or "") for m in reg.get("msgs") or [])
    return total + len(reg.get("system") or "") + len(reg.get("resumo") or "")


def _live_hist_varre(agora: float | None = None) -> list:
    """TTL de retomada + tetos. `visto` só é tocado em uso/conexão — a sessão que
    caiu fica retomável; quem nunca volta expira. Evicção pelo mais antigo."""
    agora = agora if agora is not None else time.monotonic()
    with _live_lock:
        for sid, reg in list(_live_historico.items()):
            if agora - reg.get("visto", 0) > _LIVE_RESUME_TTL_S:
                _live_historico.pop(sid, None)
        while len(_live_historico) > _LIVE_MAX_HISTORICOS:
            mais_antigo = min(_live_historico, key=lambda s: _live_historico[s].get("visto", 0))
            _live_historico.pop(mais_antigo, None)
        while sum(_live_hist_tam(r) for r in _live_historico.values()) > _LIVE_RESUME_MAX_CHARS:
            mais_antigo = min(_live_historico, key=lambda s: _live_historico[s].get("visto", 0))
            _live_historico.pop(mais_antigo, None)
    return list(_live_historico)


def _live_hist_guarda(sess: dict) -> None:
    """Grava o contexto da sessão no registro de retomada (fim de turno/desconexão)."""
    pipe = sess.get("pipe")
    msgs = []
    if pipe is not None and getattr(pipe, "history", None):
        msgs = [{"role": m.get("role"), "content": m.get("content", "")}
                for m in pipe.history if m.get("content")]
    elif sess.get("history"):
        msgs = [{"role": m["role"], "content": m["text"]} for m in sess["history"]]
    if not msgs:
        return
    with _live_lock:
        reg = _live_historico.setdefault(sess["id"], {})
        # geração: o registro é do `sid`, mas quem manda nele é a sessão MAIS NOVA.
        # Sem isto, a sessão antiga (socket zumbi) que morre depois regrava o
        # contexto velho por cima do da sessão que a retomou.
        if reg.get("geracao", 0) > sess.get("geracao", 0):
            return
        reg.update({"msgs": msgs[-_LIVE_HIST_MAX_MSGS:], "visto": time.monotonic(),
                    "geracao": sess.get("geracao", 0),
                    "voz": sess["voice_id"], "system": sess["system"]})
        reg["resumo"] = sess.get("resumo") or reg.get("resumo") or ""
    _live_hist_varre()


def _live_hist_pega(sid: str) -> dict | None:
    """Registro retomável (None = desconhecido/expirado)."""
    if not sid:
        return None
    _live_hist_varre()
    with _live_lock:
        reg = _live_historico.get(sid)
        if reg is None:
            return None
        reg["visto"] = time.monotonic()
        return dict(reg)


def _live_resumidor(sess: dict):
    """Resumidor da compressão do LIVE — o backend EFETIVO da sessão, não o da Conversa.

    POR QUE: `_chat_llm` é o caminho da CONVERSA. Com Live=dsh e Conversa no endpoint
    (a combinação que o #175 recomenda) o resumo saía pelo provedor REMOTO — egress e
    segundos — e, sem endpoint configurado, falhava 400 e o Live NUNCA comprimia (o
    contexto só crescia até reabrir a sessão ACP). Segue o backend EFETIVO
    (`_live_stats_ia`, que já considera o fallback do #146) e, no dsh, usa o POOL: o
    resumo roda em thread no fim do turno e o cliente DA SESSÃO já pode estar no
    próximo prompt (-32602, lição do #162/#170)."""
    if _live_resume_fn is not None:
        return _live_resume_fn
    return _chat_llm_dsh if _live_stats_ia(sess)["backend"] == "dsh" else _chat_llm


def _live_hist_comprime(sess: dict) -> bool:
    """Resume a metade mais antiga (assíncrono, no fim do turno). True = comprimiu."""
    pipe = sess.get("pipe")
    if pipe is None:
        return False
    msgs = list(getattr(pipe, "history", []) or [])
    if len(msgs) <= _LIVE_HIST_MAX_MSGS and \
            sum(len(m.get("content") or "") for m in msgs) <= _LIVE_HIST_MAX_CHARS:
        return False
    meio = max(1, len(msgs) // 2)
    antigas, recentes = msgs[:meio], msgs[meio:]
    try:
        resumidor = _live_resumidor(sess)
        resumo = str(resumidor([
            {"role": "system", "content":
             "Resuma a conversa abaixo em 3-5 linhas, PRESERVANDO nomes, números e "
             "decisões combinadas. Responda só o resumo."},
            *[{"role": m.get("role", "user"), "content": m.get("content", "")} for m in antigas],
        ]) or "").strip()
    except Exception as exc:             # noqa: BLE001 — resumo é otimização
        sess["resumo_erro"] = f"{type(exc).__name__}: {exc}"
        return False
    if not resumo:
        return False
    novo = [{"role": "system", "content": f"Resumo do que já foi dito: {resumo}"}] + recentes
    try:
        pipe.history[:] = novo           # o pipeline usa a MESMA lista
    except Exception:                    # noqa: BLE001 — instância trocada: recria
        pipe.history = novo
    sess["resumo"] = resumo
    return True


def _live_hist_pos_turno(sess: dict) -> None:
    """Fim de turno: guarda o contexto e, se passou do limiar, comprime em thread."""
    _live_hist_guarda(sess)
    if not _live_hist_ja_comprimindo(sess):
        thr = threading.Thread(target=_live_hist_comprime, args=(sess,), daemon=True)
        sess["compr_thread"] = thr
        thr.start()


def _live_hist_ja_comprimindo(sess: dict) -> bool:
    thr = sess.get("compr_thread")
    return bool(thr and thr.is_alive())


@app.websocket("/api/live/ws")
async def live_ws(ws: WebSocket):
    # #478: negocia a versão do protocolo no handshake. Sem header o aceite é o de
    # sempre; com o header certo, o `Sec-WebSocket-Protocol` é respondido.
    _sub, _motivo_sub = _live_negocia_subprotocolo(ws)
    await ws.accept(subprotocol=_sub)
    if not _live_autentica(ws):
        de_fora = _origem_estrangeira(ws)
        # 4403 para origem de terceiro (o cliente entende "não é a UI") e 4401 para
        # o resto; a mensagem diz o caminho em vez de só negar (#781)
        await ws.send_json(_live_erro(
            "unauthorized",
            ("Origem desconhecida: pegue um ticket em POST /api/live/ticket e use "
             "?ticket=<t> (ou ?key=<chave>)") if de_fora else
            ("Sem credencial: pegue um ticket em POST /api/live/ticket e use "
             "?ticket=<t> (ou ?key=<chave>); loopback dispensa")))
        await ws.close(code=4403 if de_fora else 4401)
        return
    _live_sweep()
    # o teto NÃO é checado aqui: entre esta linha e o registro há o `await` do setup
    # (duas conexões passavam juntas e furavam o limite) e só depois do setup se sabe
    # o `session_id` — uma retomada SUBSTITUI a entrada e não pode levar `busy`.
    try:
        primeiro = await asyncio.wait_for(ws.receive(), _LIVE_SETUP_TIMEOUT_S)
    except asyncio.TimeoutError:
        await ws.send_json(_live_erro("setup_timeout", "mande o `setup` primeiro"))
        await ws.close(code=4408)
        return
    if primeiro.get("type") == "websocket.disconnect":
        return
    if "text" not in primeiro:
        await ws.send_json(_live_erro("setup_binario", "o 1º frame é o JSON `setup`"))
        await ws.close(code=4400)
        return
    try:
        cfg = _live_valida_setup(json.loads(primeiro["text"]))
    except json.JSONDecodeError:
        await ws.send_json(_live_erro("setup_json", "setup não é JSON válido"))
        await ws.close(code=4400)
        return
    except HTTPException as exc:
        # `_resolve_voice` levanta HTTPException (404 sem voz gravada) e ela NÃO é
        # ValueError: sem este ramo o handshake morria sem NENHUM frame (o cliente
        # ficava pendurado no receive e não sabia que faltava voz).
        await ws.send_json(_live_erro("setup_invalido", str(exc.detail)))
        await ws.close(code=4400)
        return
    except ValueError as exc:
        await ws.send_json(_live_erro("setup_invalido", str(exc)))
        await ws.close(code=4400)
        return

    pedido = cfg.get("session_id") or ""
    retomado = _live_hist_pega(pedido)
    # o id é do CLIENTE quando ele manda um (#210): antes ele só era adotado se JÁ
    # existisse registro, então um cliente com id próprio estável nunca retomava
    # nada — o `ready` devolvia sid aleatório a cada reconexão e o registro enchia
    # de sids órfãos. O id já vem validado por `_live_valida_setup`
    # ([A-Za-z0-9_-]{1,64}); adotá-lo também deixa um cliente retomar a sessão de
    # outro se adivinhar o id (40 bits quando o servidor gera, e não há vínculo
    # com a chave) — registrado aqui como decisão de contrato.
    sid = pedido or uuid.uuid4().hex[:10]
    if retomado:
        # retomada: voz/system do SETUP vencem quando vieram; senão, os guardados
        cfg = {**cfg,
               "voice_id": cfg["voice_id"] if cfg.get("voice_id_pedido") else
                           (retomado.get("voz") or cfg["voice_id"]),
               "system": cfg["system"] or retomado.get("system", ""),
               "history": [{"role": m.get("role", "user"), "text": m.get("content", "")}
                           for m in (retomado.get("msgs") or [])] or cfg["history"]}
    sess = {
        "id": sid, "criada": time.time(), "visto": time.monotonic(),
        # geração: quem nasce depois MANDA no registro de retomada — a sessão antiga
        # (socket zumbi) não pode sobrescrever o contexto da que a retomou.
        "geracao": time.monotonic(),
        "voice_id": cfg["voice_id"], "system": cfg["system"], "vad": cfg["vad"],
        "history": list(cfg["history"]), "buffer": bytearray(),
        "resumo": (retomado or {}).get("resumo") or "",
        # #300: fila com teto em BYTES — derruba o áudio mais antigo e nunca JSON
        "truncado": False, "fila": _LiveFila(_LIVE_FILA_MAX_BYTES), "turno": 0,
        "cancelado": threading.Event(), "vencida": False, "substituida": False,
        "fechar": False,
        # #478: cliente que pediu versão de protocolo desconhecida fica sem
        # subprotocolo negociado — o motivo vai no `stats` para não ser silencioso.
        "subprotocolo_recusado": _motivo_sub,
        "turno_thread": None, "pipe": None, "engine": None,
        "dsh": None,                      # DSH-2: DshClient PRÓPRIO da sessão Live
        "falando": False, "audio_pendente": 0, "buffer_bytes_turno": 0,
        "t_decisao_ms": 0, "descartados": 0, "turno_barge_in": False,
        # #315: relógios do turno (decisão do VAD e abertura) — a diferença entre
        # eles entra no `latency` como `decisao_para_audio_ms`; `prewarm_ok` /
        # `prewarm_ate` são o gate do aquecimento.
        "t_decisao": None, "t_abre_turno": None,
        "prewarm_ok": False, "prewarm_ate": None, "prewarm_esperou": False,
        "turno_curto": False, "turno_barge_falso": False,
        # #126: 1 turno de fala pendente (pipeline ocupado) + o observador dele
        "turno_pendente": None, "turno_pendente_barge": False, "pend_thread": None,
        "pendentes_descartados": 0, "buffer_consumido": 0,
        # contadores do evento `turno_pendente` (o `substituido` booleano virou
        # isto): n de trechos acumulados e ms que saíram no teto do pendente
        "pendentes_trechos": 0, "pendentes_descartados_ms": 0,
        # telemetria (#118): contadores da sessão, sem trabalho extra relevante
        "t0": time.monotonic(), "st_frames": 0, "st_bytes": 0, "st_ultimo_frame": None,
        "st_dbfs": None, "st_prob": None, "st_chunks": 0, "st_audio_bytes": 0,
        # #492: fome do buffer de playback — contada quando o intervalo entre
        # dois chunks passa do próprio tamanho do anterior (+250 ms). Não pega
        # quem demora porque o TTS é lento (aí a fila cresce, não esvazia).
        "st_underruns": 0, "st_ultimo_chunk": None, "st_ultimo_dur_ms": 0,
        "st_erro": None, "st_stage": "idle", "st_stage_ini": None, "st_stage_ms": 0,
        "st_proximo": 0.0,
    }
    sess["engine"] = _live_engine_novo(sess)
    # AQUECIMENTO (#596): o card pede o classificador pronto na ABERTURA. Antes o
    # import+aquece caía no primeiro barge, dentro do laço do WS — 3,4 s medidos
    # contra um cap de 5 s, e com o `_morto` do #507 podia ser a única tentativa.
    # Some quando o filtro `modelo` está desligado (não sobe 2 GB à toa).
    if ((_settings.get("live_barge_filtros") or {}).get("modelo") or {}).get("on"):
        try:
            import live_intencao

            # `pega()` sobe worker e pode esperar 60 s: fora do loop (#798)
            sess["intencao"] = await asyncio.to_thread(live_intencao.pega)
            sess["intencao_ref"] = True      # `solta()` sai no fechamento
        except Exception as exc:             # noqa: BLE001 — sem filtro, segue
            _live_log_kv("barge_modelo_indisponivel", sess=sid, erro=str(exc)[:100])
    _live_log_kv("abre" if not retomado else "resume", sess=sid,
                 voz=sess["voice_id"], retomado=bool(retomado),
                 history=len(sess["history"]), criadas=len(_live_sessions))
    with _live_lock:
        # teto atômico com a inserção (ver comentário no topo do handler)
        anterior = _live_sessions.get(sid)
        ocupado = (anterior is None and len(_live_sessions) >= _LIVE_MAX_SESSIONS)
        if not ocupado:
            if anterior is not None:
                # #222: o id do cliente JÁ está vivo (reconexão que não fechou o
                # socket antigo, ou dois clientes com o mesmo id). A entrada é
                # sobrescrita e a sessão antiga continuaria VIVA e FORA do
                # registro — fora do sweep (nunca vencida, só morria quando o
                # cliente lembrasse de fechar) e fora da contagem do teto, que
                # assim era contornável reusando o id. Política: quem nasce
                # depois MANDA (mesma regra do registro de retomada) e a antiga
                # sai pelo caminho do fechamento, com motivo PRÓPRIO — ela não
                # ficou ociosa, foi substituída.
                anterior["substituida"] = True
            _live_sessions[sid] = sess
    if anterior is not None and not ocupado:
        _live_log_kv("substitui", sess=sid, antiga_s=int(time.time() - anterior["criada"]),
                     turnos=anterior.get("turno", 0))
    if ocupado:
        # FORA do lock (#209): `_live_lock` é `threading.Lock` e o mesmo lock é
        # pego por código SÍNCRONO no handler (`_live_sweep`, `_live_hist_*`).
        # Com o `await` lá dentro, um send que suspende (peer lento) deixava o
        # lock preso; a outra corrotina bloqueava a THREAD do event loop — e a
        # primeira só retomava se o loop rodasse. O slot já foi decidido aqui,
        # nada mais depende do lock.
        await ws.send_json(_live_erro(
            "busy", f"máximo de {_LIVE_MAX_SESSIONS} sessões simultâneas"))
        await ws.close(code=1013)
        return
    global _live_sweeper_iniciado
    if not _live_sweeper_iniciado:
        _live_sweeper_iniciado = True
        threading.Thread(target=_live_sweeper, daemon=True).start()

    await ws.send_json({"type": "ready", "session_id": sid, "resumed": bool(retomado),
                        "voice_id": sess["voice_id"],
                        "audio": {"format": "pcm16", "sr": _LIVE_AUDIO_SR, "channels": 1},
                        "in_audio": {"format": "pcm16", "sr": 16000, "channels": 1},
                        # #313: quantas mensagens do `setup` caíram no teto de chars
                        # (o cliente enxerga o que foi encurtado) e a `vad` APLICADA,
                        # com `barge_ms` junto — o efetivo, não o pedido.
                        "history_descartados": cfg.get("history_descartados", 0),
                        "vad": sess["vad"]})
    if _settings.get("tts_marca_dagua"):
        try:                    # #657: sobe o marcador fora do caminho do 1º áudio
            import marca_dagua

            marca_dagua.prewarm()
        except Exception:       # noqa: BLE001 — sem marca a sessão segue
            pass
    sender = None
    try:
        # dentro do try: o `finally` mata o ffmpeg mesmo se o pipeline não nascer.
        # O `abrir` corre enquanto o cliente liga o microfone. O NS do navegador
        # continua no Live; este é o outro filtro, no PCM, e não passa por afftdn.
        sess["rnnoise"] = _live_rnnoise_novo()
        try:
            # DENTRO do try: um pipeline que não nasce deixava a sessão presa no
            # registry para sempre (sem task de envio, nada a fechava) e o processo
            # dsh já criado órfão. Aqui ele degrada — a sessão continua no ar.
            sess["pipe"] = _live_pipe_novo(sess)
        except Exception as exc:         # noqa: BLE001 — sessão já está no ar
            sess["pipe"] = None
            print(f"[live] pipeline indisponível ({type(exc).__name__}: {exc}) — "
                  f"sessão segue sem pipeline", flush=True)
            _live_envia_json(sess, _live_erro("pipeline", f"{type(exc).__name__}: {exc}"))
        if sess["pipe"] is not None:
            threading.Thread(target=_live_pipe_start, args=(sess,), daemon=True).start()
        sender = asyncio.create_task(_live_sender(sess, ws))
        await _live_loop(sess, ws)
    except WebSocketDisconnect:
        pass
    finally:
        # antes do close do filtro: ele pode esperar ~0,3 s e o dump tem de
        # fechar o wav mesmo assim. Idempotente se o sender ainda escrever.
        _live_eco_fecha(sess)
        ns = sess.pop("rnnoise", None)
        if ns is not None:
            # thread: `fechar` espera o processo até ~0,3 s (espera do filtro +
            # `wait`), e aqui é o event loop — reconexão em rajada atrasaria os
            # frames das OUTRAS sessões (#375/F4)
            try:
                await asyncio.to_thread(ns.fechar)
            except Exception:            # noqa: BLE001
                pass
        sess["fechar"] = True
        sess["cancelado"].set()
        # FORA do event loop (#798): `close()` espera processo até 65 s e uma
        # sessão fechando congelava TODAS as outras. Executor PRÓPRIO (não o
        # default do loop) e com prazo: estourou, a thread segue e a sessão sai do
        # registry do mesmo jeito.
        # O pipe/dsh não definem `__bool__`/`__len__` (sempre verdadeiros), então o
        # `or` cobre o caso "pipeline não nasceu, o dsh é órfão" sem trocar semântica.
        # PRIMEIRO que tudo: sai do registry (#798). O fechamento pode levar até o
        # prazo (pool ocupado por outra sessão inclusa), o histórico vai ao disco e
        # o sender pode esperar 2 s — nada disso deve manter a sessão viva na
        # contagem do teto nem no sweep.
        # SÓ sai quem ainda é o dono da entrada: numa retomada o `sid` é o do
        # cliente, então o pop incondicional da sessão ANTIGA apagava a NOVA (ela
        # ficava fora do sweep/TTL e fora da contagem do teto). A checagem vai
        # DENTRO do lock: fora dele, uma retomada que nascesse entre o `get` e o
        # `pop` inseria a sessão nova e o pop da antiga levava a nova junto (#201).
        # Aqui também se DEVOLVE a referência do classificador de intenção: o
        # cliente que fecha limpo não passa pelo `_live_solta_slot`, e sem isto o
        # worker de 3,8 GB só saía pelo TTL (e podia nunca sair).
        _live_libera_sessao(sess, "cliente_fechou")
        alvo = sess.get("pipe") or sess.get("dsh")
        # log ANTES do await: o fechamento pode estourar o prazo e a thread seguir
        # escrevendo em `sess`; lendo aqui o retrato é consistente (e o cliente que
        # fecha não corre com o log)
        _live_log_kv("fecha", sess=sid, idade_s=int(time.time() - sess["criada"]),
                     frames=sess.get("st_frames", 0), bytes=sess.get("st_bytes", 0),
                     chunks=sess.get("st_chunks", 0), turnos=sess.get("turno", 0))
        if alvo is not None:
            try:
                await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(
                        _LIVE_FECHAMENTO_POOL, alvo.close),
                    _LIVE_FECHAMENTO_TIMEOUT_S)
            except asyncio.TimeoutError:
                # o future cancelado pode nem ter começado (o `run` desiste antes):
                # sem esta linha o processo órfão não aparece em lugar nenhum
                print(f"[live] fechamento de {sid} passou de "
                      f"{_LIVE_FECHAMENTO_TIMEOUT_S:g}s — worker pode ficar órfão",
                      file=sys.stderr, flush=True)
            except Exception:            # noqa: BLE001
                pass
        try:
            _live_hist_guarda(sess)       # retomável pelo session_id
        except Exception:                 # noqa: BLE001
            pass
        sess["fila"].put(None)
        if sender is not None:
            try:
                await asyncio.wait_for(sender, 2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                sender.cancel()


async def _live_loop(sess: dict, ws) -> None:
    """Loop de recepção: binário = áudio do turno, texto = comando do protocolo."""
    _ultimo_rx = time.monotonic()
    while True:
        msg = await ws.receive()
        _agora_rx = time.monotonic()
        if _agora_rx - _ultimo_rx > 0.5:
            # DIAGNÓSTICO (#674): quem trava o loop deixa rastro no intervalo entre
            # frames — sem isto o atraso aparece como "cliente não mandou nada"
            _live_log_kv("loop_atraso", sess=sess["id"],
                         ms=int((_agora_rx - _ultimo_rx) * 1000),
                         stage=sess.get("st_stage"))
        _ultimo_rx = _agora_rx
        if msg.get("type") == "websocket.disconnect":
            return
        if sess.get("fechar"):
            # #300: a task de envio morreu e soltou o slot. O socket pode demorar
            # a devolver o `disconnect` (cliente meio-aberto); sem esta saída o
            # loop fica renovando `visto` e a sessão fica zumbá no registry.
            return
        sess["visto"] = time.monotonic()
        if msg.get("bytes"):
            cru = bytes(msg["bytes"])
            # o CRU é guardado ANTES de qualquer filtro: é dele que o STT vai
            # receber o áudio do turno (#481)
            _live_cru_guarda(sess, cru)
            _live_eco_mic(sess, cru)   # #870: cru do eco; no-op sem TTS_LIVE_DUMP_DIR
            # thread: o prime do ffmpeg pode levar ~0,2 s no 1º frame e o
            # sender (TTS) precisa continuar no event loop. Sem filtro, o
            # cru entra direto — a suíte não paga o hop.
            if sess.get("rnnoise") is not None:
                pcm = await asyncio.to_thread(_live_pcm_limpo, sess, cru)
            else:
                pcm = cru
            sess["st_frames"] += 1
            if sess["st_frames"] % 10 == 0:
                # DIAGNÓSTICO (#674): o frame CHEGOU? Com que estado do motor?
                _live_log_kv("frame", sess=sess["id"], n=sess["st_frames"],
                             stage=sess.get("st_stage"),
                             estado=getattr(getattr(sess.get("engine"), "estado", None),
                                            "value", None))
            sess["st_bytes"] += len(pcm)
            sess["st_ultimo_frame"] = time.monotonic()
            sess["st_dbfs"] = round(_dbfs16(pcm), 1)
            eng = sess.get("engine")
            if eng is not None:
                # #870: a correlação do duck lê o CRU (antes do RNNoise). O
                # filtrado segue no `feed` — Silero e nível não mudam. Sem a
                # flag o ring não enche e o motor usa o histórico do feed.
                if getattr(getattr(eng, "config", None), "duck_rapido", False):
                    anota_cru = getattr(eng, "anota_mic_cru", None)
                    if anota_cru is not None:
                        try:
                            anota_cru(cru)
                        except Exception:  # noqa: BLE001 — sem cru vale o histórico
                            pass
                _live_trata_eventos(sess, eng.feed(pcm))
                # depois do feed: _proxima_amostra já inclui o frame que correlacionou
                _live_eco_marca(sess, eng)
            elif sess.get("pipe") is not None:
                sess["pipe"].push_pcm(pcm)   # sem FSM: pipeline por frame
            buf = sess["buffer"]
            buf.extend(pcm)
            if len(buf) > _LIVE_MAX_BUFFER:      # mantém o INÍCIO do turno
                del buf[_LIVE_MAX_BUFFER:]
                # consumido em `_live_enriquece` (estampa `truncated` no
                # `turn_complete`); com o pipeline ele também rastreia o próprio
                # flag e o `setdefault` deixa o dele vencer. Este é o ÚNICO
                # emissor quando não há pipeline (stub) ou engine.
                sess["truncado"] = True
            continue
        if not msg.get("text"):
            continue
        try:
            dados = json.loads(msg["text"])
        except json.JSONDecodeError:
            _live_envia_json(sess, _live_erro("json", "frame de texto não é JSON"))
            continue
        tipo = str((dados or {}).get("type") or "")
        if tipo == "ping":
            _live_envia_json(sess, {"type": "pong", "t": dados.get("t")})
        elif tipo == "cancel":
            eng = sess.get("engine")
            if eng is not None:
                eng.cancel()
            _live_cancela(sess, do_cliente=True)   # abortou tudo: pendente sai junto
        elif tipo == "hold":
            # #314: fallback "segurar pra falar" — o motor abre/fecha o turno no
            # BOTÃO, sem heurístico de VAD. O aviso chega pelo
            # `stats.motor.hold_to_talk_sugerido`; sem ele o recurso era morto.
            eng = sess.get("engine")
            fn = getattr(eng, "set_hold", None)
            if fn is None:
                _live_envia_json(sess, _live_erro(
                    "comando", "hold sem motor de turnos nesta sessão"))
                continue
            _live_trata_eventos(sess, fn(bool((dados or {}).get("on"))))
        elif tipo == "end_of_speech":
            eng = sess.get("engine")
            if eng is not None:          # comando é do cliente: força o fim do turno
                eventos_do_flush = eng.flush()
                _live_trata_eventos(sess, eventos_do_flush)
                # Motor OCIOSO no comando (turno anterior já fechou e nada foi
                # alimentado desde então): o `flush` devolve [] e NADA abriria o
                # turno — o cliente ficava esperando o `turn_complete` que nunca
                # vinha (2º turno travado, medido no smoke do próprio repo).
                # Abre pelo buffer da sessão, exatamente como o caminho sem motor.
                pipe = sess.get("pipe")
                if (not eventos_do_flush and pipe is not None and not pipe.ocupado
                        and sess.get("buffer")):
                    # não houve evento do motor: a DECISÃO foi o comando do cliente,
                    # então a origem da conta do #315 é aqui (sem isto o turno by
                    # flush com motor ocioso sai sem `decisao_para_audio_ms`)
                    sess["t_decisao"] = time.monotonic()
                    sess["t_decisao_ms"] = 0
                    _live_abre_turno(sess)
                continue
            if sess.get("pipe") is not None:
                _live_abre_turno(sess)               # usa o buffer da sessão
                continue
            if not sess["buffer"]:
                _live_envia_json(sess, _live_erro("sem_audio", "nenhum frame de áudio recebido"))
                continue
            if sess["turno_thread"] and sess["turno_thread"].is_alive():
                _live_envia_json(sess, _live_erro("turno_em_curso", "espere o turn_complete"))
                continue
            sess["cancelado"].clear()
            sess["turno"] += 1
            sess["buffer_bytes_turno"] = len(sess["buffer"])   # p/ observabilidade
            thr = threading.Thread(target=_live_engine, args=(sess,), daemon=True)
            sess["turno_thread"] = thr
            thr.start()
        else:
            _live_envia_json(sess, _live_erro("comando", f"comando desconhecido: {tipo!r}"))

# ---------------------------------------------------------------------------
# API compatível com OpenAI (POST /v1/audio/speech) — funciona com o SDK da
# OpenAI e clientes xAI/Grok apontando base_url para http://127.0.0.1:7860/v1
# ---------------------------------------------------------------------------

# formato -> (args do ffmpeg, content-type)
_AUDIO_FORMATS = {
    "mp3": (["-f", "mp3", "-b:a", "128k"], "audio/mpeg"),
    "wav": (["-f", "wav"], "audio/wav"),
    "flac": (["-f", "flac"], "audio/flac"),
    "aac": (["-f", "adts", "-c:a", "aac"], "audio/aac"),
    "opus": (["-f", "ogg", "-c:a", "libopus"], "audio/ogg"),
    "pcm": (["-f", "s16le", "-ar", "24000", "-ac", "1"], "audio/pcm"),
}


def _resolve_voice(voice) -> str:
    """Aceita id ou nome; desconhecida cai na voz padrão do dashboard ou na mais recente."""
    # voz virtual de VOICE DESIGN: gera só do instruct (sem clone). Aceita
    # "__design__" ou "design" -> a API pode pedir uma voz projetada por texto.
    if voice and str(voice).strip().lower() in (DESIGN_VOICE_ID, "design"):
        return DESIGN_VOICE_ID
    voices = list_voices()
    if not voices:
        raise HTTPException(404, "Nenhuma voz gravada — grave uma na UI primeiro")
    for v in voices:
        if voice and (v["id"] == voice or v["name"].lower() == str(voice).lower()):
            return v["id"]
    padrao = _settings["default_voice"]
    if padrao and any(v["id"] == padrao for v in voices):
        return padrao
    # fallback precisa de voz SINTETIZÁVEL: sem .wav a síntese por clone quebra
    # com FileNotFoundError no meio da frase da IA (toda resposta muda). Vozes
    # virtuais só servem para DESIGN (tratado acima), não para este endpoint.
    usaveis = [v for v in voices if (_p := _voice_path(v["id"])) is not None and _p.exists()]
    return (usaveis or voices)[0]["id"]  # mais recente sintetizável


def _encode_audio(wav_path: Path, fmt: str, speed: float) -> tuple[bytes, str]:
    args, mime = _AUDIO_FORMATS[fmt]
    if fmt == "wav" and abs(speed - 1.0) < 1e-3:
        return wav_path.read_bytes(), mime
    cmd = [FFMPEG, "-v", "error", "-i", str(wav_path)]
    if abs(speed - 1.0) >= 1e-3:
        cmd += ["-filter:a", _atempo_chain(speed)]
    cmd += args + ["pipe:1"]
    proc = subprocess.run(cmd, capture_output=True, timeout=120)
    if proc.returncode != 0:
        raise HTTPException(500, f"Conversão de áudio falhou: {proc.stderr.decode()[:200]}")
    return proc.stdout, mime


@app.get("/v1/models")
def openai_models():
    agora = int(time.time())
    return {"object": "list", "data": [
        {"id": m, "object": "model", "created": agora, "owned_by": "tts-studio"}
        for m in ("tts-1", "tts-1-hd", "whisper-1")
    ]}


def _ts_srt(x: float, sep: str = ",") -> str:
    h = int(x // 3600); mm = int((x % 3600) // 60); s = x % 60
    return f"{h:02d}:{mm:02d}:{s:06.3f}".replace(".", sep)


def _segs_to_srt(segs) -> str:
    return "\n".join(f"{i}\n{_ts_srt(s['start'])} --> {_ts_srt(s['end'])}\n{s['text']}\n"
                     for i, s in enumerate(segs, 1))


def _segs_to_vtt(segs) -> str:
    body = "\n".join(f"{_ts_srt(s['start'], '.')} --> {_ts_srt(s['end'], '.')}\n{s['text']}\n" for s in segs)
    return "WEBVTT\n\n" + body


def _openai_stt(file, language, response_format, translate, response=None,
                speaker_alvo=""):
    """STT OpenAI-compatível: áudio -> texto (+ segmentos). translate=True traduz p/ inglês.

    `response` é a resposta injetada pela rota: o aviso de fallback do remoto vai no
    HEADER (vale para json/verbose_json/text/srt/vtt; um campo no corpo não cobriria
    os formatos de texto)."""
    if file is None:
        raise HTTPException(400, "Campo 'file' obrigatório")
    tmp = _save_audio_upload(file)
    extra_alvo: dict = {}
    try:
        tmp_stt, extra_alvo = _aplica_speaker_alvo(tmp, speaker_alvo)
        try:
            r = _transcribe(tmp_stt, language=(language or "auto").lower())
        finally:
            if tmp_stt != tmp:
                tmp_stt.unlink(missing_ok=True)
    finally:
        tmp.unlink(missing_ok=True)
    text = (r.get("text") or "").strip()
    lang = (r.get("language") or "").strip().lower()
    segs = [{"start": float(s.get("start") or 0.0), "end": float(s.get("end") or 0.0),
             "text": (s.get("text") or "").strip()} for s in (r.get("segments") or [])]
    if translate and text:                       # /translations -> inglês (agrega; srt/vtt ficam no original)
        text = _translate(text, "en")
    if response is not None:
        _aviso_remoto_headers(response.headers, r)
    rf = (response_format or "json").lower()
    if rf == "text":
        resp = Response(text + "\n", media_type="text/plain; charset=utf-8")
        _aviso_remoto_headers(resp.headers, r)
        return resp
    if rf == "srt":
        resp = Response(_segs_to_srt(segs), media_type="application/x-subrip; charset=utf-8")
        _aviso_remoto_headers(resp.headers, r)
        return resp
    if rf == "vtt":
        resp = Response(_segs_to_vtt(segs), media_type="text/vtt; charset=utf-8")
        _aviso_remoto_headers(resp.headers, r)
        return resp
    if rf == "verbose_json":
        dur = max((s["end"] for s in segs), default=0.0)
        return {"task": "translate" if translate else "transcribe", "language": lang,
                "duration": round(dur, 3), "text": text,
                "segments": [{"id": i, "start": s["start"], "end": s["end"], "text": s["text"]}
                             for i, s in enumerate(segs)], **extra_alvo}
    return {"text": text}


@app.post("/v1/audio/transcriptions")
def openai_transcriptions(response: Response, file: UploadFile = File(...),
                          model: str = Form("whisper-1"), language: str = Form(None),
                          prompt: str = Form(None), response_format: str = Form("json"),
                          temperature: float = Form(0.0), speaker_alvo: str = Form("")):
    """STT compatível com OpenAI Whisper. response_format: json|text|srt|verbose_json|vtt.

    `speaker_alvo` é extensão NOSSA (campo de formulário): campo desconhecido não
    quebra cliente OpenAI, e sem ele o contrato é o de sempre."""
    return _openai_stt(file, language, response_format, translate=False,
                       response=response, speaker_alvo=speaker_alvo)


@app.post("/v1/audio/translations")
def openai_translations(response: Response, file: UploadFile = File(...),
                        model: str = Form("whisper-1"), prompt: str = Form(None),
                        response_format: str = Form("json"), temperature: float = Form(0.0)):
    """STT + tradução p/ inglês (compatível com OpenAI). response_format igual ao de transcriptions."""
    return _openai_stt(file, None, response_format, translate=True, response=response)


@app.post("/v1/audio/speech")
def openai_speech(payload: dict):
    # NOTA: síncrono por design — o SDK OpenAI espera o áudio na resposta. O
    # t.join() abaixo segura 1 thread do threadpool do Starlette (40 por
    # default) até 10 min por request; a fila de falas serializa clientes em
    # série, então o pool não esgota no uso normal (LAN).
    text = (payload.get("input") or "").strip()
    if not text:
        raise HTTPException(400, "Campo 'input' vazio")
    if len(text) > 5000:
        raise HTTPException(400, "Texto longo demais (máx. 5000 caracteres)")

    if _settings["pre_prompt"]:
        text = f"{_settings['pre_prompt']} {text}".strip()
    fmt = payload.get("response_format", "mp3")
    if fmt not in _AUDIO_FORMATS:
        raise HTTPException(400, f"response_format inválido. Suportados: {', '.join(_AUDIO_FORMATS)}")
    voice_id = _resolve_voice(payload.get("voice"))
    language = (payload.get("language") or _settings["language"]).lower()
    omni = _resolve_omni(payload)  # inclui speed -> aplicado nativamente pelo modelo
    if voice_id == DESIGN_VOICE_ID and not (omni.get("instruct") or "").strip():
        raise HTTPException(400, "voice='__design__' exige 'instruct' (descrição da voz) — "
                                 "ex.: 'female, young adult, high pitch' — ou defina omni_instruct nas settings")
    if _settings.get("tts_marca_dagua"):
        try:                    # #657: o cliente OpenAI espera o áudio AQUI; a carga
            import marca_dagua  # do marcador (~2,6 s) não pode entrar nesse relógio

            marca_dagua.prewarm()
        except Exception:       # noqa: BLE001 — prewarm é otimização
            pass
    # tts-1-hd força mais passos de difusão (qualidade); senão vale o padrão/override
    if str(payload.get("model", "tts-1")).endswith("-hd") and "num_steps" not in payload:
        omni["num_steps"] = OMNI_STEPS_HQ

    # queue=false: cliente controla o tempo da fala (Conversa faz pipeline no
    # navegador). Default true — o contrato do SDK OpenAI não muda.
    q = payload.get("queue", payload.get("speech_queue", _settings.get("speech_queue", True)))
    use_queue = q is not False and str(q).lower() not in ("false", "0", "no")

    # reusa o pipeline de jobs de forma síncrona (histórico incluso)
    job_id = _jobs_admit({"text": text[:200]})
    # Referência PRÓPRIA ao job: assim que a thread termina o job vira
    # descartável e outro pedido pode evictá-lo do _jobs antes da leitura abaixo
    # (o evict dá pop no dict, não invalida o objeto) — sem isto era KeyError/500.
    job = _jobs[job_id]
    # mesma mecânica do /api/tts: MLX exige thread "nova" (stream GPU é
    # thread-local e o threadpool do FastAPI reusa threads sem stream)
    t = threading.Thread(
        target=_run_tts_job,
        args=(job_id, text, voice_id, VOICES_DIR / f"{voice_id}.wav", language, omni),
        kwargs={"use_queue": use_queue},
        daemon=True,
    )
    t.start()
    t.join(timeout=600)
    if t.is_alive():
        raise HTTPException(504, "Síntese excedeu 10 minutos")
    if job["status"] != "done":
        raise HTTPException(500, f"Falha na síntese: {job.get('error')}")

    wav_path = OUTPUTS_DIR / f"{job['output']['id']}.wav"
    # limpeza ou DELETE podem ter levado o arquivo depois do status done (#884).
    # A irmã GET /api/outputs/{id}/audio já responde 404; sem isto era 500
    # (read_bytes) ou "Conversão de áudio falhou" (ffmpeg).
    if not wav_path.is_file():
        raise HTTPException(404, "Áudio não encontrado")
    try:
        # velocidade já aplicada nativamente pelo modelo; aqui ffmpeg só converte o formato
        data, mime = _encode_audio(wav_path, fmt, 1.0)
    except FileNotFoundError:
        # sumiu entre o is_file e o read_bytes
        raise HTTPException(404, "Áudio não encontrado") from None
    except HTTPException as exc:
        # o mesmo buraco no caminho do ffmpeg: o 500 só vale se o arquivo ainda está lá
        if exc.status_code == 500 and not wav_path.is_file():
            raise HTTPException(404, "Áudio não encontrado") from None
        raise
    return Response(content=data, media_type=mime)


# descarrega TTS/STT/tradutor/SER ociosos (idle_unload_minutes; 0 = off)
threading.Thread(target=_idle_unload_loop, daemon=True).start()

# CORS por ÚLTIMO = camada mais externa (ver nota no topo do módulo): assim
# 401/429 do middleware de auth e erros de rota saem com os cabeçalhos que o
# navegador cross-origin exige — sem eles o fetch vira "Failed to fetch".
app.add_middleware(
    CORSMiddleware,
    # sem curinga por padrão (#780): `ACAO: *` no loopback é justamente o que
    # deixava a página de terceiro LER a resposta. A UI é same-origin e não usa
    # CORS; quem é de fora entra em `TTS_CORS_ORIGINS`.
    # a MESMA decisão da auth (#813): o que passa em `_origem_confiavel` também
    # precisa de `ACAO`, senão o navegador autentica e não deixa LER
    allow_origins=list(_ORIGENS_PERMITIDAS),
    allow_methods=["*"],
    allow_headers=["*"],
    # `Retry-After` não é header safelisted de CORS: sem expor, cliente
    # cross-origin (o client/ do repo, SDK de fora) lê null e negocia o retry no
    # escuro. Os nossos 429 (admissão de jobs e rate limiter) pedem um tempo
    # explícito — ele tem que chegar a quem vai repetir.
    expose_headers=["Retry-After"],
)

# Estágio do histórico a partir da rota (#376). Serve para ler "quanto custa o
# STT" sem abrir o código; rota fora do mapa entra sem estágio.
_ESTAGIO_POR_ROTA = (
    ("/api/tts", "tts"), ("/v1/audio/speech", "tts"),
    ("/api/live", "live"), ("/api/chat", "llm"), ("/v1/chat", "llm"),
    ("/api/translate", "llm"), ("/v1/audio/transcriptions", "stt"),
    ("/api/transcribe", "stt"),
)


def _estagio_da_rota(rota: str) -> str:
    for prefixo, estagio in _ESTAGIO_POR_ROTA:
        if rota.startswith(prefixo):
            return estagio
    return ""


def _backend_para_historico() -> str:
    """Id do backend efetivo, curto. Nunca a chave nem a URL."""
    try:
        return str((_model_state.get("backend_id") or ""))[:24]
    except Exception:  # noqa: BLE001
        return ""


@app.middleware("http")
async def _historico_operacao(request, call_next):
    """Grava uma linha por requisição de API no histórico (#376).

    Registrado por último na pilha => roda por FORA: vê o status FINAL, inclusive
    o 401 da auth e o 429 do limitador (é o que responde "quantos 429 de
    admissão"). O preço é medir também o trabalho deles — para `/api/status` isso
    é ruído desprezível. Fora de `/api/` e `/v1/` não grava: asset não é operação.

    Falha do histórico nunca vira falha do request: o `registrar` já engole, e aqui
    só há aritmética de tempo.
    """
    caminho = request.url.path
    if not caminho.startswith(("/api/", "/v1/")):
        return await call_next(request)
    t0 = time.monotonic()
    resposta = await call_next(request)
    try:
        _metrics.registrar(_rate_path_normalizado(caminho),
                           (time.monotonic() - t0) * 1000.0,
                           int(getattr(resposta, "status_code", 0) or 0),
                           _estagio_da_rota(caminho), _backend_para_historico(),
                           ttl_dias=int(_settings.get("metrics_ttl_dias", 14)),
                           teto_mb=int(_settings.get("metrics_teto_mb", 8)))
    except Exception:  # noqa: BLE001
        pass
    return resposta

# UI estática (registrada por último para não engolir /api/* e /v1/*)
app.mount("/", StaticFiles(directory=BASE / "static", html=True), name="static")


# Registrado POR ÚLTIMO = camada mais EXTERNA (o Starlette monta do último para o
# primeiro): o 421 de `Host` e o 413 de corpo precisam sair ANTES da auth (#790). O
# CORS fica por dentro e o `_recusa` devolve o `ACAO` à mão.
app.add_middleware(_TetoDeCorpo)
# Por FORA do teto (#915): o relógio inclui a espera na fila do anyio. É
# pass-through — não responde, então o 421/413 do `_TetoDeCorpo` continua saindo.
app.add_middleware(_SaudeWatchdog)
