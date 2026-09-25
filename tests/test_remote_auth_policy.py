"""Auth dos servidores remotos (RTX): chave presente/ausente e adoção da política.

Não importa torch/transformers/omnivoice (o `server.py` carrega modelo no import):
a política mora em `remote/auth_policy.py`, que é stdlib. O que é dos servidores
(chamar a política no boot, antes de subir modelo; 401 no middleware; modo no
`/health`) é conferido lendo o AST dos dois arquivos.
"""
from __future__ import annotations

import ast
import importlib
import importlib.util
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REMOTE = Path(__file__).resolve().parent.parent / "remote"

# (arquivo, variável da chave, escape hatch documentada)
SERVIDORES = [
    ("omni_server.py", "OMNI_API_KEY", "OMNI_ALLOW_NO_AUTH"),
    ("voxtral_server.py", "VOXTRAL_API_KEY", "VOXTRAL_ALLOW_NO_AUTH"),
]


class _Req:
    """Só o que o middleware usa: um mapping de headers."""

    def __init__(self, **headers):
        self.headers = {k.replace("_", "-").lower(): v for k, v in headers.items()}


def _auth():
    return importlib.import_module("remote.auth_policy")


def _arvore(arquivo: str) -> ast.Module:
    return ast.parse((REMOTE / arquivo).read_text())


def _chamada(arvore: ast.Module, nome: str) -> list[ast.Call]:
    return [n for n in ast.walk(arvore)
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == nome]


def _kwargs(chamada: ast.Call) -> dict:
    return {k.arg: getattr(k.value, "value", None) for k in chamada.keywords}


# ------------------------------------------------------------ política: chave

def test_chave_presente_exige_e_apara_espaco(monkeypatch):
    monkeypatch.setenv("TTS_TEST_KEY", "  s3cr3t com espaço  ")
    monkeypatch.delenv("TTS_TEST_HATCH", raising=False)
    assert _auth().load_key("TTS_TEST_KEY", allow_no_auth_var="TTS_TEST_HATCH") == \
        ("s3cr3t com espaço", "required")


def test_sem_chave_e_sem_hatch_nao_sobe(monkeypatch, capsys):
    monkeypatch.delenv("TTS_TEST_KEY", raising=False)
    monkeypatch.delenv("TTS_TEST_HATCH", raising=False)
    with pytest.raises(SystemExit) as e:
        _auth().load_key("TTS_TEST_KEY", allow_no_auth_var="TTS_TEST_HATCH")
    assert e.value.code == 1
    msg = capsys.readouterr().out
    assert "TTS_TEST_KEY" in msg and "TTS_TEST_HATCH" in msg and "0.0.0.0" in msg


@pytest.mark.parametrize("hatch,esperado", [("1", "open")])
def test_hatch_explicita_deixa_aberto_com_aviso(monkeypatch, capsys, hatch, esperado):
    monkeypatch.delenv("TTS_TEST_KEY", raising=False)
    monkeypatch.setenv("TTS_TEST_HATCH", hatch)
    assert _auth().load_key("TTS_TEST_KEY", allow_no_auth_var="TTS_TEST_HATCH") == ("", esperado)
    assert "ABERTO" in capsys.readouterr().out


@pytest.mark.parametrize("valor", ["0", "", "true", "yes", "2"])
def test_hatch_so_vale_com_1(monkeypatch, valor):
    """`OMNI_ALLOW_NO_AUTH=true` não conta — o contrato é "1" e isso é documentado."""
    monkeypatch.delenv("TTS_TEST_KEY", raising=False)
    monkeypatch.setenv("TTS_TEST_HATCH", valor)
    with pytest.raises(SystemExit):
        _auth().load_key("TTS_TEST_KEY", allow_no_auth_var="TTS_TEST_HATCH")


# ------------------------------------------------------- política: request

def test_health_e_options_ficam_fora_da_chave():
    p = _auth().precisa_chave
    assert p("GET", "/health") is False
    assert p("OPTIONS", "/v1/audio/speech") is False
    assert p("post", "/v1/audio/speech") is True
    assert p("GET", "/voices") is True
    # o caminho tem de bater exato: /health/ (barra no fim) exige chave
    assert p("GET", "/health/") is True


def test_autorizado_aceita_bearer_e_x_api_key():
    auth = _auth()
    key = "chave-com-ç-e-espaço"
    assert auth.autorizado(_Req(authorization=f"Bearer {key}"), key)
    assert auth.autorizado(_Req(authorization=f"bearer {key}"), key)
    assert auth.autorizado(_Req(authorization=f"  Bearer   {key}  "), key)
    assert auth.autorizado(_Req(x_api_key=key), key)


def test_autorizado_recusa_errada_e_ausente():
    auth = _auth()
    key = "s3cr3t"
    assert auth.autorizado(_Req(authorization="Bearer errada"), key) is False
    assert auth.autorizado(_Req(authorization="Bearer "), key) is False
    assert auth.autorizado(_Req(), key) is False
    assert auth.autorizado(_Req(x_api_key="  "), key) is False
    assert auth.autorizado(_Req(authorization="Basic abc"), key) is False


def test_chave_nao_ascii_nao_explode():
    """`compare_digest` de str só aceita ASCII; em bytes qualquer chave serve."""
    auth = _auth()
    key = "chave-ç-中文"
    assert auth.autorizado(_Req(x_api_key=key), key) is True
    assert auth.autorizado(_Req(x_api_key="chave-ç-中"), key) is False


# ---------------------------------------------------- adoção pelos servidores

@pytest.mark.parametrize("arquivo,var,hatch", SERVIDORES)
def test_boot_chama_a_politica_antes_de_carregar_modelo(arquivo, var, hatch):
    arvore = _arvore(arquivo)
    chamadas = _chamada(arvore, "load_key")
    assert chamadas, f"{arquivo} não chama load_key()"
    c = chamadas[0]
    assert getattr(c.args[0], "value", None) == var
    assert _kwargs(c) == {"allow_no_auth_var": hatch}

    linha_politica = c.lineno
    cargas = [n.lineno for n in ast.walk(arvore)
              if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "from_pretrained"]
    assert cargas, f"{arquivo}: esperava um from_pretrained para comparar a ordem"
    assert linha_politica < min(cargas), "a política tem de rodar antes de subir o modelo"


@pytest.mark.parametrize("arquivo,var,hatch", SERVIDORES)
def test_middleware_exige_chave_e_devolve_401(arquivo, var, hatch):
    arvore = _arvore(arquivo)
    fn = next(n for n in ast.walk(arvore)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "require_api_key")
    chamados = {n.func.id for n in ast.walk(fn)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert {"precisa_chave", "autorizado"} <= chamados

    # precisa_chave(request.method, request.url.path) — e não caminho fixo
    pc = next(n for n in ast.walk(fn)
              if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "precisa_chave")
    assert [getattr(a, "attr", None) for a in pc.args] == ["method", "path"]

    respostas_401 = [n for n in ast.walk(fn)
                     if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "JSONResponse"
                     and any(k.arg == "status_code" and getattr(k.value, "value", None) == 401
                             for k in n.keywords)]
    assert respostas_401, "faltou o 401"


@pytest.mark.parametrize("arquivo,var,hatch", SERVIDORES)
def test_health_publica_o_modo_sem_chave_no_corpo(arquivo, var, hatch):
    fn = next(n for n in ast.walk(_arvore(arquivo))
              if isinstance(n, ast.FunctionDef) and n.name == "health")
    dicts = [n for n in ast.walk(fn) if isinstance(n, ast.Dict)]
    pares = [(k, v) for d in dicts for k, v in zip(d.keys, d.values)]
    assert any(getattr(k, "value", None) == "auth" and getattr(v, "id", None) == "AUTH_MODE"
               for k, v in pares), "o /health não publica o modo do boot"


def test_arquivo_copiavel_sobe_sozinho_ao_lado_do_server(tmp_path):
    """O deploy copia `auth_policy.py` ao lado do `server.py`: ele tem de rodar
    sozinho (stdlib), fora do repo e sem os pacotes dos modelos."""
    shutil.copy(REMOTE / "auth_policy.py", tmp_path / "auth_policy.py")
    programa = ("import auth_policy as a;"
                "print(a.load_key('OMNI_API_KEY', allow_no_auth_var='OMNI_ALLOW_NO_AUTH'));"
                "print(a.autorizado(__import__('types').SimpleNamespace("
                "headers={'x-api-key': 'k'}), 'k'))")

    env = {k: v for k, v in os.environ.items() if not k.startswith("OMNI_")}
    com_chave = subprocess.run([sys.executable, "-c", programa], cwd=tmp_path,
                               env={**env, "OMNI_API_KEY": "k"}, capture_output=True, text=True)
    assert com_chave.returncode == 0, com_chave.stderr
    assert "('k', 'required')" in com_chave.stdout and "True" in com_chave.stdout

    sem_chave = subprocess.run([sys.executable, "-c", programa], cwd=tmp_path,
                               env=env, capture_output=True, text=True)
    assert sem_chave.returncode == 1
    assert "OMNI_ALLOW_NO_AUTH" in sem_chave.stdout and "ABERTO" in sem_chave.stdout

    aberto = subprocess.run([sys.executable, "-c", programa], cwd=tmp_path,
                            env={**env, "OMNI_ALLOW_NO_AUTH": "1"}, capture_output=True, text=True)
    assert aberto.returncode == 0 and "('', 'open')" in aberto.stdout

# ------------------------------------- import de verdade (com pesados falsos)

# liga/desliga o "onnxruntime existe" do stub de silero_vad (aceite da #14: sem o
# pacote o server não pode morrer, tem de logar e cair no jit do torch)
SEM_ONNXRUNTIME = {"ok": True}


def _load_silero_vad(onnx=False, *a, **k):
    if onnx and not SEM_ONNXRUNTIME["ok"]:
        raise RuntimeError("no module named onnxruntime")
    return types.SimpleNamespace()


def _instala_stubs(monkeypatch, carregados: list) -> None:
    """Falsifica torch/transformers/omnivoice/... — o suficiente para o import
    do server.py rodar sem CUDA, sem HuggingFace e sem baixar peso."""
    def fake(nome: str, **attrs):
        m = types.ModuleType(nome)
        for k, v in attrs.items():
            setattr(m, k, v)
        monkeypatch.setitem(sys.modules, nome, m)
        return m

    class Pesos:
        def __init__(self):
            self.config = types.SimpleNamespace(sample_rate=24000)

        def eval(self):
            return self

        @classmethod
        def from_pretrained(cls, *a, **k):
            carregados.append(str(a[0]))
            return cls()

    cuda = types.SimpleNamespace(
        is_available=lambda: True, get_device_name=lambda i: "RTX-fake", device_count=lambda: 2,
        mem_get_info=lambda: (1e9, 2e9), memory_allocated=lambda: 0)
    torch = fake("torch", bfloat16="bf16", set_float32_matmul_precision=lambda *_: None,
                 compile=lambda m=None, **k: m, no_grad=lambda: None, cuda=cuda,
                 from_numpy=lambda a: a,
                 backends=types.SimpleNamespace(
                     cuda=types.SimpleNamespace(matmul=types.SimpleNamespace()),
                     cudnn=types.SimpleNamespace()))
    fake("torch.nn", functional=fake("torch.nn.functional", softmax=lambda x, **k: x))
    torch.nn = sys.modules["torch.nn"]
    fake("soundfile", read=lambda *a, **k: (None, None), write=lambda *a, **k: None)
    fake("faster_whisper", WhisperModel=lambda *a, **k: types.SimpleNamespace())
    fake("librosa", load=lambda *a, **k: (None, 16000))
    fake("silero_vad", load_silero_vad=_load_silero_vad,
         get_speech_timestamps=lambda *a, **k: [])
    fake("transformers", BitsAndBytesConfig=lambda **k: types.SimpleNamespace(),
         VoxtralProcessor=Pesos, VoxtralForConditionalGeneration=Pesos,
         AutoTokenizer=Pesos, AutoModelForCausalLM=Pesos)
    fake("omnivoice", OmniVoice=Pesos, OmniVoiceGenerationConfig=types.SimpleNamespace)


@pytest.fixture
def carrega_servidor(monkeypatch):
    """Importa `remote/<arquivo>` de verdade e devolve o módulo."""
    carregados: list[str] = []
    _instala_stubs(monkeypatch, carregados)
    # o módulo cria /root/omnivoice/voices e lista as vozes na /health: no Mac
    # (sandbox) /root não é gravável
    monkeypatch.setattr(os, "makedirs", lambda *a, **k: None)
    _listdir = os.listdir
    monkeypatch.setattr(os, "listdir", lambda p=".": _listdir(p) if os.path.isdir(p) else [])
    monkeypatch.syspath_prepend(str(REMOTE))

    def _carrega(arquivo: str, *, chave: str | None, hatch: str | None = None):
        var = dict((a, v) for a, v, _ in SERVIDORES)[arquivo]
        hatch_var = dict((a, h) for a, _, h in SERVIDORES)[arquivo]
        for _, v, h in SERVIDORES:
            monkeypatch.delenv(v, raising=False)
            monkeypatch.delenv(h, raising=False)
        if chave:
            monkeypatch.setenv(var, chave)
        if hatch:
            monkeypatch.setenv(hatch_var, hatch)

        monkeypatch.delitem(sys.modules, "auth_policy", raising=False)
        nome = f"servidor_{arquivo[:-3]}"
        spec = importlib.util.spec_from_file_location(nome, REMOTE / arquivo)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[nome] = mod
        spec.loader.exec_module(mod)
        return mod

    _carrega.modelos_carregados = carregados
    return _carrega


@pytest.mark.parametrize("arquivo,var,hatch", SERVIDORES)
def test_sem_chave_o_servidor_nem_importa_e_nao_carrega_modelo(arquivo, var, hatch, carrega_servidor):
    with pytest.raises(SystemExit) as e:
        carrega_servidor(arquivo, chave=None)
    assert e.value.code == 1
    assert carrega_servidor.modelos_carregados == [], "morreu DEPOIS de subir o modelo"


@pytest.mark.parametrize("arquivo,var,hatch", SERVIDORES)
def test_com_chave_401_sem_header_e_passa_com_bearer_ou_x_api_key(arquivo, var, hatch, carrega_servidor):
    mod = carrega_servidor(arquivo, chave="k1")
    cliente = TestClient(mod.app)

    assert cliente.get("/health").status_code == 200
    assert cliente.get("/health").json()["auth"] == "required"
    # POST em rota inexistente basta: o middleware roda antes do 404
    assert cliente.post("/rota-inexistente").status_code == 401
    assert cliente.post("/rota-inexistente", headers={"Authorization": "Bearer errada"}).status_code == 401
    assert cliente.post("/rota-inexistente", headers={"Authorization": "Bearer k1"}).status_code == 404
    assert cliente.post("/rota-inexistente", headers={"X-API-Key": "k1"}).status_code == 404
    assert cliente.options("/rota-inexistente").status_code == 404   # OPTIONS fica fora


@pytest.mark.parametrize("arquivo,var,hatch", SERVIDORES)
def test_hatch_sobe_aberto_e_avisa(arquivo, var, hatch, carrega_servidor, capsys):
    mod = carrega_servidor(arquivo, chave=None, hatch="1")
    cliente = TestClient(mod.app)
    assert cliente.get("/health").json()["auth"] == "open"
    assert cliente.post("/rota-inexistente").status_code == 404     # sem 401
    assert "ABERTO" in capsys.readouterr().out


# ------------------------------------------------- VAD do voxtral (task #14)

def test_vad_usa_onnx_quando_disponivel(carrega_servidor):
    mod = carrega_servidor("voxtral_server.py", chave="k1")
    assert mod.VAD_BACKEND == "onnx"


def test_vad_cai_no_torch_jit_sem_onnxruntime_sem_derrubar_o_servidor(
        carrega_servidor, monkeypatch, capsys):
    """Aceite 1 da #14: sem `onnxruntime` o import não morre — avisa no log e
    sobe no jit do torch; `/health` publica qual caminho subiu."""
    monkeypatch.setitem(SEM_ONNXRUNTIME, "ok", False)

    mod = carrega_servidor("voxtral_server.py", chave="k1")   # não levanta
    assert mod.VAD_BACKEND == "torch-jit"
    assert "[vad]" in capsys.readouterr().out
    assert TestClient(mod.app).get("/health").json()["vad"] == "torch-jit"


def test_vad_onnx_nao_vaza_no_health_como_torch(carrega_servidor):
    """Sanidade: `vad` do /health é o caminho REAL que subiu, não um rótulo fixo."""
    assert carrega_servidor("voxtral_server.py", chave="k1").VAD_BACKEND == "onnx"
