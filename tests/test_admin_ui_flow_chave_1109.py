"""#1109 — `tests/admin_ui_flow.sh`: contrato da chave dos `curl`.

Antes: `CHAVE_TESTE="${TESTE_API_KEY:-${TTS_ROD_ADMIN_KEY:-}}"` e o header ia no
argv (`-H "X-API-Key: $CHAVE_TESTE"`) de 5 `curl` — inclusive no modo instância
própria, onde o loopback dispensa chave. Num shell com `TTS_ROD_ADMIN_KEY`
exportada (a credencial de OPERADOR do dono, a que o `run.sh` usa) a suíte a
herdava e a deixava legível em `ps`.

Contrato (decisão do PM, 2026-11-10):
1. instância própria (padrão): nenhum header de chave;
2. BASE externo fora do loopback: só `TESTE_API_KEY`; sem ela, erro e saída ≠ 0;
   o fallback para `TTS_ROD_ADMIN_KEY` saiu;
3. com chave: `-H @arquivo` 0600, apagado no trap.

A suíte real precisa de navegador e modelo; aqui ela roda numa CÓPIA (raiz sem
`.venv-mlx`, então o `$PY` dela é o `python3` do PATH) com shims de `curl`,
`python3` e `ipconfig`. O shim de python faz de conta o servidor e as etapas de
UI (anotando o ambiente) e repassa o resto (json, porta livre) ao python real.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

AQUI = Path(__file__).resolve().parent
OPERADOR = "chave-de-OPERADOR-do-dono-0123"
TESTE = "chave-de-TESTE-explicita-4567"


def _shim(path: Path, corpo: str) -> None:
    path.write_text(corpo)
    path.chmod(0o755)


@pytest.fixture
def caixa(tmp_path: Path) -> dict:
    raiz = tmp_path / "repo"
    (raiz / "tests" / "fixtures").mkdir(parents=True)
    for nome in ("admin_ui_flow.sh", "serial.sh"):
        shutil.copy2(AQUI / nome, raiz / "tests" / nome)
    shutil.copy2(AQUI / "fixtures" / "voz_fala_5s_16k.wav",
                 raiz / "tests" / "fixtures" / "voz_fala_5s_16k.wav")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    _shim(bin_ / "ipconfig", "#!/bin/sh\necho 192.0.2.10\n")
    _shim(bin_ / "curl", r'''#!/bin/bash
# curl falso: anota o argv (o que `ps` veria) e o modo de todo `-H @arquivo`
printf '%s\n' "$*" >> "$T_CURL_ARGV"
metodo=GET; url=""; prox=""
for a in "$@"; do
  case "$prox" in
    X) metodo="$a"; prox=""; continue ;;
    H) if [ "${a#@}" != "$a" ]; then
         f="${a#@}"; printf '%s %s %s\n' "$f" "$(stat -f %Lp "$f" 2>/dev/null)" \
           "$(grep -c '^X-API-Key: ' "$f" 2>/dev/null)" >> "$T_CURL_HDR"
       fi; prox=""; continue ;;
  esac
  case "$a" in
    -X) prox=X ;;
    -H) prox=H ;;
    http*) url="$a" ;;
  esac
done
case "$metodo $url" in
  "POST "*/api/apikeys) n=$(( $(wc -l < "$T_CURL_ARGV") ))
                        printf '{"key":{"id":"k%s","secret":"segredo-sintetico-%s"}}' "$n" "$n" ;;
  "GET "*/api/apikeys)  printf '{"keys":[]}' ;;
esac
exit 0
''')
    _shim(bin_ / "python3", f'''#!/bin/bash
# python falso só para o servidor e as etapas de navegador; o resto é o real
printf '%s\\n' "$*" >> "$T_PY_ARGV"
if [ "${{1:-}}" = "-" ]; then
  cat > /dev/null
  printf 'admin=%s\\n' "${{TTS_ROD_ADMIN_KEY:-}}" >> "$T_PY_ENV"
  case "${{2:-}}" in
    [0-9]*) exec sleep 60 ;;          # "servidor": o encerra mata pelo pid
    *) exit 0 ;;                      # etapas de UI/contrato: passam
  esac
fi
exec "{sys.executable}" "$@"
''')
    env = {k: v for k, v in os.environ.items()
           if k not in ("BASE", "TESTE_API_KEY", "TTS_ROD_ADMIN_KEY", "ADMIN_UI_NO_DONO")}
    env.update(
        PATH=f"{bin_}:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
        TTS_SERIAL="0",
        TMPDIR=str(tmp_path / "tmpd"),
        T_CURL_ARGV=str(tmp_path / "curl.argv"),
        T_CURL_HDR=str(tmp_path / "curl.hdr"),
        T_PY_ARGV=str(tmp_path / "py.argv"),
        T_PY_ENV=str(tmp_path / "py.env"),
    )
    (tmp_path / "tmpd").mkdir()
    return {"env": env, "script": raiz / "tests" / "admin_ui_flow.sh", "tmp": tmp_path}


def _roda(caixa: dict, **extra: str) -> subprocess.CompletedProcess:
    env = dict(caixa["env"], **extra)
    return subprocess.run(["bash", str(caixa["script"])], env=env, capture_output=True,
                          text=True, timeout=120, cwd=str(caixa["tmp"]))


def _linhas(caixa: dict, nome: str) -> list[str]:
    p = caixa["tmp"] / nome
    return p.read_text().splitlines() if p.exists() else []


def test_instancia_propria_nao_usa_chave_mesmo_com_a_do_operador_exportada(caixa):
    """Modo padrão com `TTS_ROD_ADMIN_KEY` (e até `TESTE_API_KEY`) exportadas:
    nenhum `curl` com header de chave, nenhum argv com qualquer das duas, e a
    instância própria e as etapas de navegador sobem sem a credencial do dono."""
    r = _roda(caixa, TTS_ROD_ADMIN_KEY=OPERADOR, TESTE_API_KEY=TESTE)
    assert r.returncode == 0, r.stdout + r.stderr
    curl = _linhas(caixa, "curl.argv")
    assert [l for l in curl if "/api/apikeys" in l], curl      # a suíte rodou mesmo
    assert not [l for l in curl if "-H @" in l or "X-API-Key" in l], curl
    tudo = curl + _linhas(caixa, "py.argv")
    assert not [l for l in tudo if OPERADOR in l or TESTE in l], tudo
    assert not _linhas(caixa, "curl.hdr")
    env = _linhas(caixa, "py.env")
    assert env and all(l == "admin=" for l in env), env        # servidor + UI sem a env
    assert OPERADOR not in r.stdout + r.stderr


def test_base_externo_sem_chave_de_teste_para_e_nao_usa_a_do_operador(caixa):
    """BASE fora do loopback sem `TESTE_API_KEY`: erro claro e saída ≠ 0, mesmo
    com `TTS_ROD_ADMIN_KEY` exportada — reintroduzir o fallback derruba esta."""
    r = _roda(caixa, BASE="http://192.0.2.10:53203", TTS_ROD_ADMIN_KEY=OPERADOR)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "TESTE_API_KEY" in r.stderr, r.stderr
    curl = _linhas(caixa, "curl.argv")
    assert not [l for l in curl if "/api/apikeys" in l], curl
    assert not [l for l in curl if OPERADOR in l], curl
    assert not _linhas(caixa, "curl.hdr")
    assert not list((caixa["tmp"] / "tmpd").glob("adminui-hdr.*"))


def test_base_externo_com_chave_de_teste_vai_por_arquivo_0600_e_some(caixa):
    """Com `TESTE_API_KEY`: todo `curl` da API de chaves lê o header de arquivo
    0600, a chave não aparece em argv nenhum e o arquivo sai no trap."""
    r = _roda(caixa, BASE="http://192.0.2.10:53203", TESTE_API_KEY=TESTE,
              TTS_ROD_ADMIN_KEY=OPERADOR)
    assert r.returncode == 0, r.stdout + r.stderr
    curl = _linhas(caixa, "curl.argv")
    api = [l for l in curl if "/api/apikeys" in l]
    assert len(api) >= 3, curl                                  # lista + 2 criações (+ DELETEs)
    assert all("-H @" in l for l in api), api
    tudo = curl + _linhas(caixa, "py.argv")
    assert not [l for l in tudo if TESTE in l or OPERADOR in l], tudo
    hdr = _linhas(caixa, "curl.hdr")
    assert len(hdr) == len(api), (hdr, api)
    assert all(l.split()[1:] == ["600", "1"] for l in hdr), hdr
    assert all(not Path(l.split()[0]).exists() for l in hdr), hdr
    assert not list((caixa["tmp"] / "tmpd").glob("adminui-hdr.*"))
    assert TESTE not in r.stdout + r.stderr


def test_header_sai_tambem_quando_a_suite_falha_antes_do_trap_principal(caixa):
    """Falha ANTES do `trap encerra` (app sem resposta): o header já existe e tem
    de sair igual — os traps do serial.sh foram repostos com a remoção junto."""
    # curl que falha em tudo: o "app não respondeu" sai logo depois do header
    _shim(Path(caixa["env"]["PATH"].split(":")[0]) / "curl",
          '#!/bin/bash\nprintf \'%s\\n\' "$*" >> "$T_CURL_ARGV"\nexit 7\n')
    r = _roda(caixa, BASE="http://192.0.2.10:53203", TESTE_API_KEY=TESTE)
    assert r.returncode != 0
    assert "app não respondeu" in r.stderr, r.stderr
    assert not list((caixa["tmp"] / "tmpd").glob("adminui-hdr.*"))


def test_script_nao_le_a_credencial_do_operador():
    """Guarda estática: fora do `unset` do topo, nenhuma linha de código do
    script lê `TTS_ROD_ADMIN_KEY`. O `unset` sozinho já anula um fallback
    reintroduzido (por isso a célula de comportamento não o vê); esta pega o
    fallback mesmo com o `unset` no lugar."""
    codigo = [l for l in (AQUI / "admin_ui_flow.sh").read_text().splitlines()
              if not l.lstrip().startswith("#")]
    leituras = [l for l in codigo
                if "TTS_ROD_ADMIN_KEY" in l and l.strip() != "unset TTS_ROD_ADMIN_KEY"
                and not l.lstrip().startswith("echo ")]
    assert not leituras, leituras
    assert "unset TTS_ROD_ADMIN_KEY" in [l.strip() for l in codigo]
