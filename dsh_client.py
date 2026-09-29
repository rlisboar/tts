"""Backend de IA "dsh" — cliente ACP v1 (Agent Client Protocol) sobre stdio NDJSON.

O `dsh` é o harness de código (DeepSeek Harness); aqui ele é usado só como LLM de
conversa: um perfil próprio (`tts-studio`) com os plugins de tool desligados, para
nenhum schema de agente ir ao modelo. Este módulo é o TRANSPORTE + a política de
turno mínima — quem decide o que fazer com o texto é o chamador (`_chat_llm` para
a Conversa, o `live_pipeline` para o Live).

Protocolo (referência: claudinhos/daemon/src/runners/turns/dsh.ts):
  spawn `dsh --profile <perfil>` → `initialize{protocolVersion:1}` →
  `session/new{cwd, mcpServers:[]}` → `session/set_config_option(model|reasoning_effort)`
  → `session/prompt{prompt:[{type:"text",text}]}`. O texto chega em `session/update`
  com `update.sessionUpdate == "agent_message_chunk"`. `session/cancel` cancela o
  turno em voo (notification). `session/close` + SIGTERM/SIGKILL no fim.
  stdout é SÓ protocolo; logs do filho vão para stderr (-> on_log).

Decisões (fase 0, task_129d2183): o boot do processo custa 2–4 s e o 1º prompt de
uma sessão paga 1,3–7 s sozinho — então o processo é PERSISTENTE e `_garantir()`
faz pre-warm (initialize + sessão + um prompt curto descartado). Com prewarm e
effort="off" o 1º token do turno real fica em ~0,45 s.

Sem dependência nova: apenas stdlib.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Iterator

# ---------------------------------------------------------------------------
# Constantes do contrato
# ---------------------------------------------------------------------------
#: par opaco [rota, modelo]. A rota default do catálogo (`deepseek-official`) está
#: SEM chave neste host e o prompt falha com -32603; este é o default seguro.
DSH_DEFAULT_MODEL = '["dsflash","deepseek-flash-41"]'
DSH_KEYLESS_ROUTE = "deepseek-official"
DSH_DEFAULT_PROFILE = "tts-studio"
DSH_EFFORTS = ("off", "low", "high", "max")
_EFFORT_ALIAS = {"none": "off", "minimal": "off", "medium": "low",
                 "xhigh": "max", "maximum": "max"}

_CONTROL_TIMEOUT_S = 60.0
_SESSION_TIMEOUT_S = 120.0
_PROMPT_TIMEOUT_S = 3600.0
_RESTART_DELAY_S = 0.5
_RESTART_MAX_TRIES = 5
_RESTART_CAP_S = 30.0
_TERMINATE_GRACE_S = 2.0
_KILL_GRACE_S = 3.0
_CANCEL_ESPERA_S = 15.0
_NODE_MIN = (24, 2)
_PREWARM_ESPERA_S = 90.0
#: espera pelo slot de prompt da sessão (o harness só aceita um por vez)
_PROMPT_LIVRE_ESPERA_S = 300.0
#: quando um turno REAL chega com o prewarm em voo: cancelar o prewarm e esperar
#: o settle dele por este tempo antes de mandar o turno (#172)
_PREWARM_CANCEL_ESPERA_S = 5.0
#: retry de erro TRANSITÓRIO do harness (recusa por prompt em voo, -32602).
#: MECANISMO PRINCIPAL é a ESPERA do slot; isto é o CINTO, para o caso de o slot
#: ter sido liberado à força por cima de um prompt que ainda não settleou.
#: Esperas crescentes: uma tentativa por espera (#172).
_TRANSIENTE_ESPERAS = (0.25, 0.75, 2.0)
_TRANSIENTE_TENTATIVAS = 1 + len(_TRANSIENTE_ESPERAS)


class DshError(Exception):
    """Falha do backend dsh. A mensagem é sempre acionável (path, perfil, motivo)."""


class _SettlePendente(DshError):
    """O prompt foi ACEITO e ainda está em voo quando o prazo do cliente estourou.

    Não é falha: o slot continua tomado e o leitor o libera quando a resposta
    chegar. Existe para separar "espera" de "falha" (#172)."""


# ---------------------------------------------------------------------------
# Helpers de modelo/effort (puros — testáveis sem processo)
# ---------------------------------------------------------------------------
def dsh_model_route(value) -> str | None:
    """Rota (1º elemento do par opaco `["rota","modelo"]`) ou None se fora do formato."""
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return None
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], str):
        return parsed[0]
    return None


def dsh_model_valido(value) -> bool:
    """True se `value` é um par opaco JSON [rota, modelo] com strings não vazias."""
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return False
    return (isinstance(parsed, list) and len(parsed) >= 2
            and all(isinstance(x, str) and x.strip() for x in parsed))


def dsh_model_for_turn(model) -> str:
    """Modelo que o TURNO usa: sem modelo ou rota sem chave → default seguro."""
    m = str(model or "").strip()
    if not m:
        return DSH_DEFAULT_MODEL
    return DSH_DEFAULT_MODEL if dsh_model_route(m) == DSH_KEYLESS_ROUTE else m


def dsh_effort_valido(value) -> str:
    """Normaliza o effort p/ off|low|high|max (inválido → off)."""
    v = str(value or "").strip().lower()
    v = _EFFORT_ALIAS.get(v, v)
    return v if v in DSH_EFFORTS else "off"


def node_versao(env: dict | None = None) -> tuple[int, int]:
    """Versão do node no PATH. Levanta DshError explicativo se ausente/antiga.

    Abaixo de Node 24.2 o launcher do dsh sai MUDO com rc=0 (o `import.meta.main`
    não existe) e o handshake só estoura por timeout — então checamos ANTES."""
    caminho = (env or {}).get("PATH") or os.environ.get("PATH")
    exe = shutil.which("node", path=caminho) or shutil.which("node")
    if not exe:
        raise DshError("node não encontrado no PATH (o dsh exige Node >= "
                       f"{_NODE_MIN[0]}.{_NODE_MIN[1]})")
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise DshError(f"não foi possível rodar node --version: {exc}") from exc
    txt = (r.stdout or r.stderr or "").strip().lstrip("v")
    try:
        partes = tuple(int(x) for x in txt.split("-")[0].split(".")[:2])
    except ValueError:
        raise DshError(f"versão de node ilegível ({txt!r})") from None
    if len(partes) < 2:
        raise DshError(f"versão de node ilegível ({txt!r})")
    if partes < _NODE_MIN:
        raise DshError(f"node {txt} é antigo: o dsh exige >= "
                       f"{_NODE_MIN[0]}.{_NODE_MIN[1]} (abaixo disso o launcher sai "
                       "MUDO com rc=0)")
    return partes  # type: ignore[return-value]


def resolver_bin(binario: str | None = None, *, checa_node: bool = True) -> str:
    """Caminho absoluto do binário `dsh` (default: `dsh` no PATH).

    Três casos com mensagens DIFERENTES (#163): não achou, achou mas não executa,
    e achou e executável (aí quem falhar é o boot/handshake, não o PATH)."""
    nome = str(binario or "dsh").strip() or "dsh"
    exe = nome if os.path.isabs(nome) else shutil.which(nome)
    if not exe:
        raise DshError(f"binário {nome!r} não encontrado no PATH (ajuste chat_dsh_bin)")
    if not os.path.isfile(exe):
        raise DshError(f"{exe!r} não existe (ajuste chat_dsh_bin)")
    if not os.access(exe, os.X_OK):
        raise DshError(f"{exe!r} existe mas não é executável "
                       "(chmod +x ou ajuste chat_dsh_bin)")
    if checa_node and Path(exe).name.startswith("dsh"):
        node_versao()
    return exe


def _env_minimo() -> dict:
    """Env do filho: só PATH/HOME/TMPDIR (+ locale/DSH_HOME). As NOSSAS chaves não
    vão — o dsh lê ~/.dsh/.credentials.yaml por conta própria e o app nunca copia."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": os.environ.get("HOME", str(Path.home())),
           "TMPDIR": os.environ.get("TMPDIR", tempfile.gettempdir()),
           "LANG": os.environ.get("LANG", "en_US.UTF-8")}
    for extra in ("DSH_HOME", "USER", "LOGNAME", "SHELL"):
        if os.environ.get(extra):
            env[extra] = os.environ[extra]
    return env


def _prompt_em_voo(exc: Exception) -> bool:
    """Erro TRANSITÓRIO do harness: o slot de prompt da sessão estava ocupado.

    O ACP recusa o 2º prompt com `-32602 Invalid params: a prompt is already in
    flight for this session`. Não é defeito da sessão — vale uma nova tentativa."""
    msg = str(exc).lower()
    return "-32602" in msg and "in flight" in msg


def _erro_acp(err) -> str:
    err = err if isinstance(err, dict) else {}
    detalhe = ""
    data = err.get("data")
    if isinstance(data, dict) and isinstance(data.get("details"), str):
        detalhe = f" — {data['details']}"
    msg = f"acp {err.get('code', '?')}: {err.get('message', 'erro')}{detalhe}"
    if "-32603" in msg and "api key" in msg.lower():
        msg += " (a rota do modelo está sem chave — use uma rota com credencial)"
    return msg


# ---------------------------------------------------------------------------
# Estado do bridge ACP no HOST (patch do DSH-4a) — task_159
#
# O patch que faz o harness entregar deltas vive FORA do repo (node_modules global)
# e some em qualquer `npm install -g`. Aqui o produto passa a SABER disso.
# A fonte é o marcador `DSH4A_PATCH_V1` no `lib/index.js` do pacote resolvido — o
# MESMO arquivo que o `--status` do scripts/dsh-acp-stream-patch.mjs inspeciona
# (ele sai com rc 0/1 pela presença desse marcador). Ler o arquivo dá o mesmo
# veredito sem gastar um processo node por request; `npm root -g` é só o FALLBACK
# quando a busca pura pelo caminho não acha o pacote.
# ---------------------------------------------------------------------------
DSH_PATCH_MARKER = "DSH4A_PATCH_V1"
DSH_BRIDGE_ESTADOS = ("patched", "clean", "unknown")


def _pacote_acp_por_caminho(exe: str) -> Path | None:
    """`lib/index.js` do @deepseek-ai/dsh-acp subindo pelos parents do binário."""
    alvo = Path(exe).resolve()
    for base in (alvo.parent, *alvo.parents):
        p = base / "node_modules" / "@deepseek-ai" / "dsh-acp" / "lib" / "index.js"
        if p.is_file():
            return p
    return None


def _pacote_acp_por_npm() -> Path | None:
    """Fallback: raiz global do npm (subprocess). Só roda quando o caminho falha."""
    try:
        r = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True,
                           timeout=20)
        raiz = (r.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not raiz:
        return None
    for sufixo in ("@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-acp",
                   "@deepseek-ai/dsh-acp"):
        p = Path(raiz) / sufixo / "lib" / "index.js"
        if p.is_file():
            return p
    return None


def estado_bridge(bin=None) -> dict:
    """Estado do patch do bridge: `patched` | `clean` | `unknown`. NUNCA levanta.

    `unknown` é o caminho de degradação (binário/node/pacote ausente, arquivo
    ilegível, marcador não encontrado): o chamador segue com o resto da descoberta."""
    info = {"estado": "unknown", "arquivo": None, "versao": None, "motivo": ""}
    try:
        exe = resolver_bin(bin, checa_node=False)
    except DshError as exc:
        info["motivo"] = str(exc)
        return info
    info["arquivo_bin"] = exe
    alvo = _pacote_acp_por_caminho(exe) or _pacote_acp_por_npm()
    if alvo is None:
        info["motivo"] = "pacote @deepseek-ai/dsh-acp não encontrado no host"
        return info
    info["arquivo"] = str(alvo)
    try:
        conteudo = alvo.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        info["motivo"] = f"não foi possível ler o pacote: {exc}"
        return info
    try:
        pkg = json.loads((alvo.parent.parent / "package.json").read_text(encoding="utf-8"))
        info["versao"] = str(pkg.get("version") or "") or None
    except (OSError, ValueError, TypeError):
        pass
    info["estado"] = "patched" if DSH_PATCH_MARKER in conteudo else "clean"
    info["motivo"] = ("bridge ACP com o patch de streaming (DSH-4a)"
                      if info["estado"] == "patched"
                      else "bridge ACP SEM o patch — o harness entrega a resposta "
                           "inteira no fim (some em `npm install -g`)")
    return info


def _lcp(a: str, b: str) -> int:
    """Tamanho do prefixo comum (o texto já emitido é sempre prefixo do acumulado)."""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def _ignoravel(c: str) -> bool:
    """Espaço/pontuação: não conta no casamento do texto já emitido com o comitado."""
    return c.isspace() or not (c.isalnum() or c == "_")


def _prefixo_significativo(texto: str, emitido: str) -> tuple[int, int]:
    """(índice em `texto`, índice em `emitido`) do fim do prefixo que casa IGNORANDO
    espaço/pontuação. É o que distingue "o comitado é o acumulado com pontuação
    diferente" de "este chunk é um delta novo"."""
    i = j = 0
    while i < len(texto) and j < len(emitido):
        if _ignoravel(texto[i]):
            i += 1
            continue
        if _ignoravel(emitido[j]):
            j += 1
            continue
        if texto[i].lower() != emitido[j].lower():
            break
        i += 1
        j += 1
    return i, j


def _repeticoes(emitido: str, texto: str) -> bool:
    """O acumulado é o MESMO texto repetido (`"ha"` -> acumulado `"haha"`).

    É o que separa "o modelo repetiu a palavra" de "chegou a mensagem comitada":
    o comitado de um turno com vários deltas é o texto inteiro, não uma repetição
    do último pedaço."""
    return bool(texto) and len(emitido) % len(texto) == 0 \
        and emitido == texto * (len(emitido) // len(texto))


def _delta_repetido(texto: str, emitido: str, ultimo_bruto: str) -> bool:
    """Chunk SEM marca que repete todo o acumulado: é um delta repetido ou o
    comitado? Sinal: um delta repetido é igual ao ÚLTIMO chunk bruto e o acumulado
    é esse mesmo texto repetido; um comitado é o texto inteiro do turno.

    Ambiguidade residual (documentada, e irrelevante com a marca do patch): um
    turno que chegou num ÚNICO delta seguido do comitado idêntico tem os dois
    sinais — aqui vale "delta", porque engolir palavra repetida é pior que uma
    repetição (o `comitado` puro, de hoje, não passa por aqui: 1 chunk só)."""
    return bool(ultimo_bruto) and texto == ultimo_bruto and _repeticoes(emitido, texto)


def _nao_duplicar(texto: str, parcial: bool | None, emitido: str,
                  ultimo_bruto: str = "") -> str:
    """Trecho NOVO a emitir ("" = descarta). Garante que nada já emitido se repita.

    Cobre os três cenários, sem depender do patch do bridge (DSH-4a):
      (a) só deltas               → cada delta entra como veio (inclusive repetido);
      (b) deltas + mensagem comitada → o comitado (igual ou quase igual ao
          acumulado) só contribui com o que ainda falta;
      (c) só comitado (hoje)      → 1 chunk, `emitido` vazio, entra inteiro.

    A ordem importa: a MARCA do patch (`parcial`) decide ANTES da igualdade de
    texto, senão um delta legitimamente repetido ("ha", "ha") seria confundido com
    a mensagem comitada e sumiria (achado do gate #138). Sem marca vale o texto,
    com a ambiguidade residual documentada em `_delta_repetido`."""
    if not texto:
        return ""
    if not emitido:
        return texto
    if parcial is True:                        # DELTA marcado: entra como veio
        # só um snapshot cumulativo (estritamente maior e prefixado) vira sufixo
        if len(texto) > len(emitido) and texto.startswith(emitido):
            return texto[len(emitido):]
        return texto
    if texto == emitido:
        if _delta_repetido(texto, emitido, ultimo_bruto):
            return texto                       # mesmo delta de novo (sem marca)
        return ""                              # comitado idêntico ao que já saiu
    if texto.startswith(emitido):              # snapshot cumulativo do próprio texto
        return texto[len(emitido):]
    if parcial is False:                       # comitado marcado: só o que falta
        return texto[_prefixo_significativo(texto, emitido)[0]:]
    if len(texto) <= len(emitido):
        return texto                           # delta curto: entra como veio
    # Sem marca: um chunk do MESMO porte (ou maior) do acumulado é a mensagem
    # comitada. Um delta puro é mais curto que o acumulado. O casamento ignora
    # pontuação, então o comitado com vírgula/ponto final também é reconhecido.
    i, j = _prefixo_significativo(texto, emitido)
    if j >= len(emitido) or all(_ignoravel(c) for c in emitido[j:]):
        return texto[i:]
    return texto


# ---------------------------------------------------------------------------
# Cliente
# ---------------------------------------------------------------------------
class DshClient:
    """UM cliente = UM processo `dsh` persistente + UMA sessão ACP.

    Não é thread-safe para 2 turnos ao mesmo tempo na MESMA instância: um
    `stream` por vez (um cliente por sessão Live). `cancel()` pode ser chamado
    de outra thread.

    `stream(str, turno_id)`  → manda SÓ o turno novo; o contexto fica na sessão ACP.
    `stream(list, turno_id)` → renderiza o histórico num prompt, em sessão efêmera
                               (o processo quente é reutilizado). É o caminho do
                               `_chat_llm(msgs)`, que preserva a assinatura antiga.
    """

    def __init__(self, *, bin=None, profile=DSH_DEFAULT_PROFILE, model=None,
                 effort="off", cwd=None, on_log=None, extra_args=(), env_extra=None,
                 prompt_timeout=_PROMPT_TIMEOUT_S, prewarm=True, restart_delay=_RESTART_DELAY_S,
                 cancel_espera=_CANCEL_ESPERA_S, prewarm_timeout=_PREWARM_ESPERA_S,
                 prewarm_cancel_espera=_PREWARM_CANCEL_ESPERA_S):
        self._bin_req = bin
        self._bin: str | None = None
        self._profile = str(profile or "").strip() or DSH_DEFAULT_PROFILE
        self._model = dsh_model_for_turn(model)
        self._effort = dsh_effort_valido(effort)
        self._cwd = Path(cwd) if cwd else None
        self._cwd_tmp: Path | None = None
        self._on_log = on_log
        self._extra_args = tuple(extra_args)
        self._env_extra = dict(env_extra or {})
        self._prompt_timeout = prompt_timeout
        self._prewarm_timeout = float(prewarm_timeout)
        self._restart_delay = float(restart_delay)
        self._cancel_espera = float(cancel_espera)
        self._prewarm_on_start = bool(prewarm)

        self._proc: subprocess.Popen | None = None
        self._session_id: str | None = None
        self._config_options: list = []
        self._next_id = 1
        self._pend: dict[int, tuple[threading.Event, dict]] = {}
        self._pend_lock = threading.Lock()
        self._write_lock = threading.Lock()

        self._cond = threading.Condition()
        self._buffer: list[tuple[str, bool | None]] = []
        self._fim_turno: str | None = None   # turno cujo prompt settleou (por identidade)
        self._turno_atual: str | None = None
        self._cancelados: list[str] = []
        self._turno_lock = threading.Lock()
        #: slot de prompt da sessão (o harness aceita UM por vez): livre = sem prompt
        #: EM VOO. Quem libera é o LEITOR, quando a resposta daquele prompt chega
        #: de fato (#170: um timeout do cliente NÃO significa que o harness
        #: liberou a sessão) — nunca um `finally` por decreto.
        self._prompt_livre = threading.Event()
        self._prompt_livre.set()
        self._voo_lock = threading.Lock()
        self._prompts_em_voo: dict[int, None] = {}   # rid -> None (ordem de envio)
        self._prewarm_rid: int | None = None         # prompt de prewarm em voo
        self._ultimo_rid_prompt: int | None = None   # rid do último prompt enviado
        self._toma_slot_lock = threading.Lock()      # wait+clear atômicos
        self._prewarm_cancel_espera = float(prewarm_cancel_espera)
        self._boot_lock = threading.Lock()
        self._handshake_fails = 0
        self._ultimo_erro = ""
        self._ultimo_rc: int | None = None      # rc da última morte (mensagem de boot)
        self._stderr_ultimas: list[str] = []    # últimas linhas do filho (mensagem de boot)
        self._stopping = False
        self._contador_turno = 0
        # métricas (expostas ao chamador; o Live usa para o painel/log)
        self.ultimo_boot_ms = 0
        self.ultimo_ttft_ms = 0
        self.ultimas_thought_chars = 0
        self.chunks_emitidos = 0        # deltas efetivamente entregues (métrica)
        self.chunks_duplicados = 0      # chunks absorvidos pela anti-duplicação

    # ------------------------------------------------------------- properties
    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def modelo(self) -> str:
        return self._model

    @property
    def effort(self) -> str:
        return self._effort

    @property
    def config_options(self) -> list:
        return list(self._config_options)

    # ------------------------------------------------------------ config/ciclo
    def _log(self, msg: str) -> None:
        if self._on_log:
            try:
                self._on_log(msg)
            except Exception:  # noqa: BLE001 — log nunca derruba o turno
                pass

    def _dir_cwd(self) -> Path:
        if self._cwd is not None:
            self._cwd.mkdir(parents=True, exist_ok=True)
            return self._cwd
        if self._cwd_tmp is None:
            self._cwd_tmp = Path(tempfile.mkdtemp(prefix="dsh-cwd-"))
        return self._cwd_tmp

    def _argv(self) -> list[str]:
        argv = [self._bin or "dsh", *self._extra_args]
        if self._profile:
            argv += ["--profile", self._profile]
        return argv

    def _erro_boot(self, exc: Exception) -> str:
        """Erro FINAL do boot: diz se o binário existe e como ele morreu (#163).

        Sem isto, um binário que existe e falha (ex.: `/usr/bin/false` no smoke de
        fallback) produzia a mesma cara de "não encontrado no PATH"."""
        partes = [f"handshake do dsh falhou {self._handshake_fails}x seguidas — PARADO. "
                  f"Último motivo: {self._ultimo_erro}"]
        if self._ultimo_rc is not None:
            partes.append(f"o binário {self._bin!r} existe e saiu com rc={self._ultimo_rc} "
                          "(o problema é o boot/handshake, não o PATH)")
        if self._stderr_ultimas:
            partes.append("stderr: " + " | ".join(self._stderr_ultimas[-3:]))
        partes.append("comando: " + " ".join(self._argv()))
        return " · ".join(partes)

    def _garantir(self) -> None:
        """Sobe/resume o processo e a sessão, com backoff LIMITADO (lição T-726)."""
        if self.alive and self._session_id:
            return
        with self._boot_lock:
            if self.alive and self._session_id:
                return
            if self._bin is None:
                self._bin = resolver_bin(self._bin_req)
            tentativa = 0
            while True:
                try:
                    self._subir()
                    self._handshake_fails = 0
                    return
                except DshError as exc:
                    self._ultimo_erro = str(exc)
                    self._handshake_fails += 1
                    self._matar()
                    tentativa += 1
                    if self._handshake_fails > _RESTART_MAX_TRIES:
                        raise DshError(self._erro_boot(exc)) from exc
                    atraso = min(self._restart_delay * 2 ** (tentativa - 1), _RESTART_CAP_S)
                    self._log(f"[dsh] handshake falhou ({exc}) — nova tentativa em "
                              f"{atraso:.1f}s")
                    time.sleep(atraso)

    def _subir(self) -> None:
        """spawn → initialize → sessão + pre-warm. Levanta DshError em qualquer falha."""
        t0 = time.perf_counter()
        cwd = self._dir_cwd()
        argv = self._argv()
        self._log(f"[dsh] subindo: {' '.join(argv)} (cwd={cwd})")
        try:
            proc = subprocess.Popen(
                argv, cwd=str(cwd), env={**_env_minimo(), **self._env_extra},
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as exc:
            raise DshError(f"não foi possível executar {argv[0]!r}: {exc}") from exc
        self._proc = proc
        self._stopping = False
        self._ultimo_rc = None
        self._stderr_ultimas.clear()          # a mensagem de boot fala da ÚLTIMA tentativa
        threading.Thread(target=self._ler_stdout, args=(proc,), daemon=True).start()
        threading.Thread(target=self._ler_stderr, args=(proc,), daemon=True).start()
        try:
            self._request("initialize",
                          {"protocolVersion": 1,
                           "clientCapabilities": {"fs": {"readTextFile": False,
                                                         "writeTextFile": False}}},
                          timeout=_CONTROL_TIMEOUT_S)
            self._abrir_sessao()
            if self._prewarm_on_start:
                self._prewarm()
        except Exception as exc:  # noqa: BLE001 — reempacota p/ o backoff
            raise DshError(str(exc)) from exc
        self.ultimo_boot_ms = int((time.perf_counter() - t0) * 1000)
        self._log(f"[dsh] pronto em {self.ultimo_boot_ms} ms (sessão {self._session_id})")

    def _abrir_sessao(self) -> None:
        res = self._request("session/new", {"cwd": str(self._dir_cwd()),
                                            "mcpServers": []},
                            timeout=_SESSION_TIMEOUT_S)
        sid = (res or {}).get("sessionId")
        if not sid:
            raise DshError("session/new não devolveu sessionId")
        self._session_id = sid
        self._config_options = list((res or {}).get("configOptions") or [])
        self._aplicar_config(self._config_options)

    def _aplicar_config(self, config_options: list) -> None:
        """Seta model e reasoning_effort quando os configOptions os expõem."""
        ids = {str(o.get("id") or "") for o in config_options or []}
        if "model" in ids:
            self._request("session/set_config_option",
                          {"sessionId": self._session_id, "configId": "model",
                           "value": self._model},
                          timeout=_CONTROL_TIMEOUT_S)
        if "reasoning_effort" in ids:
            self._request("session/set_config_option",
                          {"sessionId": self._session_id,
                           "configId": "reasoning_effort", "value": self._effort},
                          timeout=_CONTROL_TIMEOUT_S)

    def _prewarm(self) -> None:
        """Prompt curto descartado: absorve o cold start (1,3–7 s) do 1º turno.

        O harness aceita UM prompt por sessão (`-32602 a prompt is already in
        flight`), então o prewarm ocupa o MESMO slot do turno (#162). Se ele não
        settlear no prazo (#170/#172), o slot NÃO é declarado livre: quem libera é
        o leitor, quando a resposta chegar — e um turno que chega antes CANCELA
        este prompt em vez de esperar por ele.

        Falha aqui NÃO pode marcar a sessão como indisponível: `_prewarm` engole."""
        if not self._tomar_slot(_PROMPT_LIVRE_ESPERA_S):
            self._log("[dsh] pre-warm: slot de prompt ocupado, pulando")
            return
        # marcado ANTES de enviar: um turno que chega NO MEIO precisa reconhecer o
        # prewarm para poder cancelá-lo (senão só descobre depois do envio)
        self._prewarm_rid = -1
        try:
            self._reiniciar_turno()
            try:
                self._enviar_prompt("oi", timeout=self._prewarm_timeout)
                with self._voo_lock:             # settleou: nada a cancelar
                    if self._prewarm_rid == -1:
                        self._prewarm_rid = None
            except _SettlePendente as exc:
                # ESCOPO EXPLÍCITO: o prompt foi aceito e segue em voo, então o slot
                # continua TOMADO (o leitor libera no settle) — é o caso do provedor
                # girando 60 s no meio da geração do prewarm.
                with self._voo_lock:
                    self._prewarm_rid = self._ultimo_rid_prompt
                self._log(f"[dsh] pre-warm ainda em voo ({exc}) — o slot libera no settle")
        except DshError as exc:
            self._log(f"[dsh] pre-warm falhou ({exc}) — segue")
            with self._voo_lock:
                self._prewarm_rid = None
            self._liberar_prompt(self._ultimo_rid_prompt or -1)

    def _enviar_prompt(self, texto: str, timeout: float) -> int:
        """Manda um `session/prompt`; devolve o rid e trata o transitório (#172).

        Timeout vira `_SettlePendente` (o prompt foi aceito e segue em voo) e não
        erro comum: o slot continua tomado, o leitor libera no settle."""
        for tentativa in range(_TRANSIENTE_TENTATIVAS):
            try:
                self._request("session/prompt",
                              {"sessionId": self._session_id,
                               "prompt": [{"type": "text", "text": texto}]},
                              timeout=timeout, prompt=True)
                return self._ultimo_rid_prompt  # type: ignore[return-value]
            except DshError as exc:
                if "timeout após" in str(exc):
                    raise _SettlePendente(str(exc)) from exc
                if tentativa + 1 < _TRANSIENTE_TENTATIVAS and _prompt_em_voo(exc):
                    self._log(f"[dsh] prompt recusado em voo, repetindo: {exc}")
                    time.sleep(_TRANSIENTE_ESPERAS[tentativa])
                    continue
                raise
        raise DshError("session/prompt: sem tentativas")   # pragma: no cover

    def _prewarm_em_voo(self) -> bool:
        """True enquanto o prewarm está em curso (mesmo ANTES do rid existir)."""
        with self._voo_lock:
            return self._prewarm_rid is not None

    def _reabrir_sessao_para_destravar(self, texto: str, timeout: float) -> str | None:
        """Sessão travada por um prompt que não settleia: fecha, abre OUTRA e manda
        o prompt de novo.

        Devolve None se conseguiu (o turno segue na sessão nova) ou a mensagem de
        erro. O prompt preso morre junto com a sessão antiga, então isto resolve
        até o caso "prewarm que NUNCA responde" (#172)."""
        self._log("[dsh] prompt recusado com o slot preso — reabrindo sessão")
        try:
            self.fechar_sessao(timeout=5.0)
        except Exception as exc:  # noqa: BLE001 — close é best-effort
            self._log(f"[dsh] close da sessão presa falhou ({exc}) — seguindo")
        with self._voo_lock:            # o prompt preso foi embora com a sessão
            self._prompts_em_voo.clear()
            self._prewarm_rid = None
        self._prompt_livre.set()
        self._reiniciar_turno()         # buffer do turno começa limpo na sessão nova
        try:
            self._abrir_sessao()
            self._request("session/prompt",
                          {"sessionId": self._session_id,
                           "prompt": [{"type": "text", "text": texto}]},
                          timeout=timeout, prompt=True)
        except DshError as exc:
            return str(exc)
        return None

    def _cancelar_prewarm(self) -> None:
        """Turno REAL chegou com o prewarm em voo: cancela o prewarm e espera o
        settle dele por um tempo CURTO (#172).

        Cancelar é barato (o harness settleia o turno abortado em ms) e o prewarm
        já entregou o que importa (processo, sessão, rota). Esperar um prompt
        preso por 60 s seria deixar o "aquecimento" ADIAR o 1º turno."""
        if not self._prewarm_em_voo():
            return
        self._log("[dsh] turno chegou com pre-warm em voo — cancelando o pre-warm")
        try:
            self._notify("session/cancel", {"sessionId": self._session_id})
        except DshError as exc:
            self._log(f"[dsh] cancel do pre-warm falhou ({exc})")
        if self._prompt_livre.wait(self._prewarm_cancel_espera):
            return
        self._forcar_slot_livre(
            f"pre-warm não settleou em {self._prewarm_cancel_espera:.0f}s após o "
            "cancel — liberando o slot à força (o retry cobre um -32602)")

    def prewarm(self) -> None:
        self._garantir()

    def fechar_sessao(self, timeout: float = _CONTROL_TIMEOUT_S) -> None:
        """`session/close` (a próxima chamada abre sessão nova).

        `timeout` curto no caminho de cancel órfão: com um prompt não-settleado o
        dsh pode não responder o close e 60 s travariam o próximo turno."""
        sid, self._session_id = self._session_id, None
        self._config_options = []
        if not sid or not self.alive:
            return
        try:
            self._request("session/close", {"sessionId": sid}, timeout=timeout)
        except Exception:  # noqa: BLE001 — close é best-effort
            pass

    def close(self) -> None:
        self._stopping = True
        try:
            self.fechar_sessao()
        finally:
            self._matar()
            with self._cond:
                self._fim_turno = self._turno_atual
                self._cond.notify_all()
            if self._cwd_tmp is not None:
                shutil.rmtree(self._cwd_tmp, ignore_errors=True)
                self._cwd_tmp = None

    def _matar(self) -> None:
        proc, self._proc = self._proc, None
        self._session_id = None
        self._config_options = []
        with self._voo_lock:                     # sem processo não há prompt em voo
            self._prompts_em_voo.clear()
            self._prewarm_rid = None
        self._prompt_livre.set()
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=_TERMINATE_GRACE_S)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=_KILL_GRACE_S)
            except OSError:
                pass
        self._ultimo_rc = proc.poll()      # mensagem de boot diz "existe e saiu rc=N"
        self._falhar_pendentes(DshError("processo dsh encerrado"))

    def _falhar_pendentes(self, exc: Exception) -> None:
        with self._pend_lock:
            pend, self._pend = self._pend, {}
        for ev, box in pend.values():
            box["error"] = str(exc)
            ev.set()

    # --------------------------------------------------------------- transporte
    def _escrever(self, obj: dict) -> None:
        proc = self._proc
        if not proc or proc.stdin is None or proc.poll() is not None:
            raise DshError("processo dsh não está vivo")
        linha = (json.dumps(obj) + "\n").encode()
        with self._write_lock:
            try:
                proc.stdin.write(linha)
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise DshError(f"escrita no dsh falhou: {exc}") from exc

    def _request(self, method: str, params, timeout: float, *, prompt: bool = False):
        if not self.alive:
            raise DshError(f"{method}: processo dsh não está vivo")
        with self._pend_lock:
            rid = self._next_id
            self._next_id += 1
            ev = threading.Event()
            box: dict = {}
            self._pend[rid] = (ev, box)
        if prompt:
            # registrado ANTES da escrita: o leitor só pode ver a resposta
            # depois dela, então nunca existe janela sem registro
            with self._voo_lock:
                self._prompts_em_voo[rid] = None
            self._ultimo_rid_prompt = rid
        try:
            self._escrever({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        except DshError:
            with self._pend_lock:
                self._pend.pop(rid, None)
            if prompt:
                self._liberar_prompt(rid)
            raise
        if not ev.wait(timeout):
            # o pendente sai, mas o prompt pode seguir EM VOO no harness: o slot
            # só libera quando a resposta chegar (é o bug do #170)
            with self._pend_lock:
                self._pend.pop(rid, None)
            extra = ""
            if prompt and self._prompt_em_voo_rid(rid):
                extra = " (o prompt segue em voo: o slot libera no settle)"
            raise DshError(f"{method} timeout após {round(timeout)}s{extra}")
        with self._pend_lock:
            self._pend.pop(rid, None)
        if box.get("error"):
            raise DshError(f"{method}: {box['error']}")
        return box.get("result")

    # ------------------------------------------------------------------- slot
    def _prompt_em_voo_rid(self, rid: int) -> bool:
        with self._voo_lock:
            return rid in self._prompts_em_voo

    def _liberar_prompt(self, rid: int) -> None:
        """A resposta do prompt X chegou: o slot libera SÓ quando não sobra nenhum."""
        with self._voo_lock:
            # só o prompt MAIS ANTIGO libera de fato: um prewarm cancelado e o
            # turno que tomou o lugar não podem se atropelar na contabilidade
            if self._prompts_em_voo:
                self._prompts_em_voo.pop(rid, None)
            if rid == self._prewarm_rid or (self._prewarm_rid == -1
                                            and not self._prompts_em_voo):
                self._prewarm_rid = None
            livre = not self._prompts_em_voo
        if livre:
            self._prompt_livre.set()

    def _tomar_slot(self, timeout: float) -> bool:
        """wait+clear ATÔMICOS: sem isso prewarm e turno podem se achar livres juntos."""
        with self._toma_slot_lock:
            if not self._prompt_livre.wait(timeout):
                return False
            self._prompt_livre.clear()
            return True

    def _forcar_slot_livre(self, motivo: str) -> None:
        with self._voo_lock:
            self._prompts_em_voo.clear()
            self._prewarm_rid = None
        self._prompt_livre.set()
        self._log(f"[dsh] {motivo}")

    def _esperar_slot_prompt(self) -> None:
        """Espera o prompt em voo (prewarm ou turno de outra thread) liberar."""
        if self._tomar_slot(_PROMPT_LIVRE_ESPERA_S):
            return
        # ninguém mais pode ajudar: solta à força para o turno ao menos tentar
        # (o retry do transitório é o cinto se o harness ainda estiver ocupado)
        self._forcar_slot_livre(
            f"slot de prompt preso há {round(_PROMPT_LIVRE_ESPERA_S)}s — liberando à força")
        self._tomar_slot(1.0)

    def _notify(self, method: str, params) -> None:
        self._escrever({"jsonrpc": "2.0", "method": method, "params": params})

    def _ler_stderr(self, proc: subprocess.Popen) -> None:
        if proc.stderr is None:
            return
        for raw in proc.stderr:
            linha = raw.decode("utf-8", "replace").rstrip()
            if linha:
                self._stderr_ultimas.append(linha)
                del self._stderr_ultimas[:-10]
                self._log(f"[dsh stderr] {linha}")

    def _ler_stdout(self, proc: subprocess.Popen) -> None:
        if proc.stdout is None:
            return
        buf = b""
        while True:
            bloco = proc.stdout.read1(65536)
            if not bloco:
                break
            buf += bloco
            while b"\n" in buf:
                linha, buf = buf.split(b"\n", 1)
                self._rota_linha(linha.strip())
        if proc is self._proc and not self._stopping:
            motivo = DshError(f"processo dsh encerrou sozinho (rc={proc.poll()})")
            self._falhar_pendentes(motivo)
            with self._cond:            # destrava um stream pendurado
                self._fim_turno = self._turno_atual
                self._cond.notify_all()

    def _rota_linha(self, linha: bytes) -> None:
        if not linha:
            return
        try:
            msg = json.loads(linha.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._log(f"[dsh] frame inválido: {linha[:200]!r}")
            return
        if not isinstance(msg, dict):
            return
        if isinstance(msg.get("id"), int) and ("result" in msg or "error" in msg):
            with self._pend_lock:
                p = self._pend.pop(msg["id"], None)
            if p:
                ev, box = p
                if msg.get("error"):
                    box["error"] = _erro_acp(msg["error"])
                else:
                    box["result"] = msg.get("result")
                ev.set()
            # o prompt SETTLEOU de fato: é AQUI (e só aqui) que o slot libera —
            # um timeout do cliente não significa sessão liberada (#170)
            self._liberar_prompt(msg["id"])
            return
        method = msg.get("method") or ""
        if method == "session/update":
            params = msg.get("params") or {}
            self._rota_update(params.get("update") or {}, str(params.get("sessionId") or ""))
            return
        if isinstance(msg.get("id"), int) and method:
            if method == "session/request_permission":
                self._responder_permissao(msg)
            else:
                self._responder(msg["id"], {})

    def _rota_update(self, update: dict, sessao: str = "") -> None:
        # Guarda de identidade no nível da SESSÃO (espelho do `self.dsh !== client`):
        # chunk de uma sessão abandonada no meio de um cancel órfão não contamina
        # o turno novo.
        if sessao and self._session_id and sessao != self._session_id:
            return
        tipo = str(update.get("sessionUpdate") or "")
        if tipo == "agent_message_chunk":
            texto = (update.get("content") or {}).get("text")
            if texto:
                # `partial` é a marca OPCIONAL do patch do bridge (DSH-4a): True =
                # delta de stream, False = mensagem comitada. Ausente = o harness
                # de hoje, que manda só o comitado — quem separa é a heurística.
                parcial = update.get("partial")
                if parcial is None and isinstance(update.get("_meta"), dict):
                    parcial = update["_meta"].get("partial")
                with self._cond:
                    self._buffer.append((str(texto), parcial))
                    self._cond.notify_all()
        elif tipo == "agent_thought_chunk":
            self.ultimas_thought_chars += len((update.get("content") or {}).get("text") or "")

    def _responder(self, rid: int, result) -> None:
        self._escrever({"jsonrpc": "2.0", "id": rid, "result": result})

    def _responder_permissao(self, msg: dict) -> None:
        """Sempre NEGA (reject_once): aqui o dsh não executa tool nenhuma."""
        opcoes = (msg.get("params") or {}).get("options") or []
        rejeitar = next((o for o in opcoes
                         if o.get("kind") == "reject_once"
                         or "reject" in str(o.get("optionId") or "").lower()
                         or "deny" in str(o.get("optionId") or "").lower()), None)
        if rejeitar and rejeitar.get("optionId"):
            self._responder(msg["id"], {"outcome": {"outcome": "selected",
                                                    "optionId": rejeitar["optionId"]}})
        else:
            self._escrever({"jsonrpc": "2.0", "id": msg["id"],
                            "error": {"code": -32000, "message": "permission denied"}})

    # ------------------------------------------------------------------ turnos
    def _reiniciar_turno(self) -> None:
        with self._cond:
            self._buffer.clear()
            self._fim_turno = None

    def cancel(self, turno_id: str) -> None:
        """Cancela o turno: `session/cancel` + marca p/ o iterador DESCARTAR o chunk
        tardio. Não bloqueia; idempotente; casa por identidade de turno."""
        if not turno_id:
            return
        if turno_id not in self._cancelados:
            self._cancelados.append(turno_id)
            del self._cancelados[:-16]
        if self._turno_atual == turno_id and self._session_id and self.alive:
            try:
                self._notify("session/cancel", {"sessionId": self._session_id})
            except DshError as exc:
                self._log(f"[dsh] cancel falhou ({exc})")

    def cancelado(self, turno_id: str) -> bool:
        return turno_id in self._cancelados

    def _novo_turno(self) -> str:
        self._contador_turno += 1
        return f"t{self._contador_turno}"

    def _render(self, msgs) -> tuple[str, bool]:
        """(prompt, efêmero). str → só o texto (sessão persistente);
        list → histórico renderizado (sessão efêmera, para não duplicar contexto)."""
        if isinstance(msgs, str):
            return msgs.strip(), False
        partes = []
        for m in msgs or []:
            if not isinstance(m, dict):
                continue
            conteudo = str(m.get("content") or "").strip()
            if not conteudo:
                continue
            papel = str(m.get("role") or "user")
            if papel == "system":
                partes.append(conteudo)
            elif papel == "assistant":
                partes.append(f"Assistente: {conteudo}")
            else:
                partes.append(f"Usuário: {conteudo}")
        return "\n\n".join(partes), True

    def stream(self, msgs, turno_id: str = "") -> Iterator[str]:
        """Iterator de deltas de texto do turno. Bloqueia entre chunks.

        Se `turno_id` foi cancelado, o iterador encerra e NÃO deixa passar o chunk
        tardio daquele turno. Erro do dsh vira DshError."""
        turno_id = turno_id or self._novo_turno()
        self._garantir()
        texto, efemero = self._render(msgs)
        if not texto:
            return
        self._turno_lock.acquire()
        try:
            yield from self._stream_com_lock(texto, turno_id, efemero)
        finally:
            self._turno_lock.release()

    def _stream_com_lock(self, texto: str, turno_id: str, efemero: bool) -> Iterator[str]:
        # Um prompt por sessão no harness. Se quem está no slot é o PREWARM, um
        # turno REAL não espera por ele: cancela e espera só o settle (#172).
        self._cancelar_prewarm()
        # espera (e TOMA, atomicamente) o slot antes de qualquer troca de sessão
        self._esperar_slot_prompt()
        try:
            if efemero and self._session_id:
                self.fechar_sessao()
                self._abrir_sessao()
            self._turno_atual = turno_id
            self._reiniciar_turno()
            resultado: dict = {}

            def _prompt() -> None:
                try:
                    for tentativa in range(_TRANSIENTE_TENTATIVAS):
                        try:
                            resultado["res"] = self._request(
                                "session/prompt",
                                {"sessionId": self._session_id,
                                 "prompt": [{"type": "text", "text": texto}]},
                                timeout=self._prompt_timeout, prompt=True)
                            break
                        except DshError as exc:
                            if tentativa + 1 < _TRANSIENTE_TENTATIVAS \
                                    and _prompt_em_voo(exc) \
                                    and not self.cancelado(turno_id):
                                # cinto: a ESPERA do slot é o mecanismo principal
                                self._log(f"[dsh] prompt recusado em voo, repetindo: {exc}")
                                time.sleep(_TRANSIENTE_ESPERAS[tentativa])
                                continue
                            raise
                except Exception as exc:  # noqa: BLE001 — vira DshError no iterador
                    resultado["err"] = str(exc)
                    # prompt não aceito: o slot volta a ficar livre
                    self._liberar_prompt(self._ultimo_rid_prompt or -1)
                    if _prompt_em_voo(exc) and not self.cancelado(turno_id):
                        # ÚLTIMO CINTO (#172): o prewarm não settleou nem depois do
                        # cancel e das esperas — sessão nova resolve por construção
                        novo = self._reabrir_sessao_para_destravar(
                            texto, self._prompt_timeout)
                        resultado["err"] = "" if novo is None else novo
                finally:
                    with self._cond:
                        self._fim_turno = turno_id
                        self._cond.notify_all()

            threading.Thread(target=_prompt, daemon=True).start()

            t0 = time.perf_counter()
            drenando_cancel = False
            avisou = False
            prazo_cancel = 0.0
            emitido = ""
            ultimo_bruto = ""
            while True:
                with self._cond:
                    # sempre espera: sem spin quando o turno foi cancelado
                    if not self._buffer and self._fim_turno != turno_id:
                        self._cond.wait(0.15)
                    item = self._buffer.pop(0) if self._buffer else None
                    fim = self._fim_turno == turno_id
                # guarda de identidade REAVALIADA A CADA DELTA (não só no fim):
                # turno trocou (cancel + fala nova) → encerra aqui mesmo.
                if self._turno_atual != turno_id:
                    break
                if item is not None:
                    # nome próprio: `texto` é o prompt lido pela thread `_prompt`
                    pedaco, parcial = item
                    if not self.cancelado(turno_id):   # descarta o chunk tardio
                        novo = _nao_duplicar(pedaco, parcial, emitido, ultimo_bruto)
                        ultimo_bruto = pedaco
                        if novo != pedaco:
                            # chunk reconhecido como já emitido (total ou em parte)
                            self.chunks_duplicados += 1
                        if novo:
                            emitido += novo
                            self.chunks_emitidos += 1
                            if not self.ultimo_ttft_ms:
                                self.ultimo_ttft_ms = int((time.perf_counter() - t0) * 1000)
                            yield novo
                    continue
                if fim:
                    break
                if self.cancelado(turno_id):
                    if not drenando_cancel:
                        drenando_cancel = True
                        prazo_cancel = time.perf_counter() + self._cancel_espera
                    if not avisou:
                        avisou = True
                        self.cancel(turno_id)          # idempotente
                    if time.perf_counter() > prazo_cancel:
                        # prompt não settleou: a sessão pode estar suja → reabre
                        self._log("[dsh] cancel sem settle — sessão será reaberta")
                        self.fechar_sessao(timeout=5.0)
                        break
        finally:
            # O slot NÃO é liberado aqui: quem libera é o LEITOR, no settle da
            # resposta (#170/#172) — inclusive se este gerador for abandonado
            # antes. Prompt que nem chegou a ser aceito já libera no `_prompt`.
            pass
        if resultado.get("err"):
            raise DshError(str(resultado["err"]))
        # NOTA: o slot NÃO é liberado aqui. Quem libera é o leitor, no settle da
        # resposta (#170/#172) — inclusive se este gerador for abandonado antes.

    def collect(self, msgs, turno_id: str = "") -> str:
        """Atalho não-stream (''.join do stream) — usado pelo `_chat_llm`."""
        return "".join(self.stream(msgs, turno_id))


# ---------------------------------------------------------------------------
# Descoberta de modelos (endpoint GET /api/chat/dsh/models)
# ---------------------------------------------------------------------------
def parse_config_options(config_options: list) -> list[dict]:
    """Catálogo a partir das configOptions do `session/new`.

    O option `model` traz grupos (provider) com options aninhadas
    {value, name, description}; `value` é o par opaco que volta em
    `chat_dsh_model`. `currentValue` marca o default."""
    modelo_opt = next((o for o in config_options or []
                       if isinstance(o, dict) and str(o.get("id") or "") == "model"), None)
    if not modelo_opt or not isinstance(modelo_opt.get("options"), list):
        return []
    effort_opt = next((o for o in config_options or []
                       if isinstance(o, dict)
                       and str(o.get("id") or "") == "reasoning_effort"), None)
    efforts = [str(o.get("value") or "") for o in (effort_opt or {}).get("options") or []
               if isinstance(o, dict)]
    efforts = [e for e in efforts if e] or list(DSH_EFFORTS)
    saida, vistos = [], set()
    for entrada in modelo_opt["options"]:
        if not isinstance(entrada, dict):
            continue
        sub = entrada.get("options") if isinstance(entrada.get("options"), list) else None
        for item in (sub if sub is not None else [entrada]):
            if not isinstance(item, dict):
                continue
            valor = str(item.get("value") or "")
            if not valor or valor in vistos:
                continue
            vistos.add(valor)
            modelo = None
            if dsh_model_valido(valor):
                modelo = str(json.loads(valor)[1])
            saida.append({"id": valor,
                          "label": str(item.get("name") or "") or valor,
                          "provider": dsh_model_route(valor),
                          "modelo": modelo,
                          "efforts": efforts,
                          "isDefault": bool(modelo_opt.get("currentValue") == valor)})
    return saida


def descobrir_modelos(*, bin=None, profile=DSH_DEFAULT_PROFILE) -> dict:
    """Sobe um dsh, lê as configOptions e devolve {models, node, bin, profile}."""
    exe = resolver_bin(bin)
    cliente = DshClient(bin=exe, profile=profile, prewarm=False)
    try:
        cliente.prewarm()
        return {"bin": exe,
                "profile": profile,
                "node": ".".join(str(x) for x in node_versao()),
                "models": parse_config_options(cliente.config_options)}
    finally:
        cliente.close()