"""Bake-offs de `tools/` não leem o estado do dono ao importar o app (#1127).

Cinco scripts de `tools/bakeoff/` faziam `import app` sem `TTS_STATE_DIR`. O
app resolve os caminhos de estado no IMPORT, então eles liam o `settings.json`,
as chaves e os perfis do dono, e o `voices/`/`outputs/` da árvore. Agora
`comum.isola_estado()` aponta tudo para um tmp antes do import.

1. Guarda estática: todo `import app` em `tools/**/*.py` precisa da linha de
   código imediatamente anterior chamando `isola_estado(`. Script novo que
   esquecer cai aqui.
2. Comportamento: com caminhos "do dono" exportados no shell, o app importado
   depois de `isola_estado()` resolve TODOS os caminhos de estado dentro do tmp,
   e o tmp some na saída.
3. Ordem: `isola_estado()` depois do `import app` levanta, em vez de fingir que
   isolou.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
BAKEOFF = RAIZ / "tools" / "bakeoff"

_IMPORTA_APP = re.compile(r"^\s*(import app\b|from app import\b)")


def _linha_de_codigo_anterior(linhas: list[str], i: int) -> str:
    for j in range(i - 1, -1, -1):
        s = linhas[j].strip()
        if s and not s.startswith("#"):
            return s
    return ""


def test_todo_import_app_em_tools_vem_depois_de_isolar_o_estado():
    achados, faltam = [], []
    for p in sorted((RAIZ / "tools").rglob("*.py")):
        linhas = p.read_text(encoding="utf-8", errors="replace").splitlines()
        for i, linha in enumerate(linhas):
            if not _IMPORTA_APP.match(linha):
                continue
            rel = f"{p.relative_to(RAIZ)}:{i + 1}"
            achados.append(rel)
            if "isola_estado(" not in _linha_de_codigo_anterior(linhas, i):
                faltam.append(rel)
    # a guarda não pode passar por não achar nada (regex quebrada, pasta movida)
    assert len(achados) >= 6, achados
    assert not faltam, (
        "`import app` em tools/ sem `comum.isola_estado()` na linha de código "
        f"anterior — o app leria o estado do dono: {faltam}")


_SONDA = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[2])
import comum
raiz = comum.isola_estado()
assert comum.isola_estado() == raiz          # idempotente
import app, metrics_hist
print(json.dumps({"raiz": str(raiz), "caminhos": {
    "STATE_DIR": str(app.STATE_DIR), "SETTINGS_PATH": str(app.SETTINGS_PATH),
    "APIKEYS_PATH": str(app.APIKEYS_PATH), "LEGACY_APIKEY_PATH": str(app.LEGACY_APIKEY_PATH),
    "SPEAKER_PATH": str(app.SPEAKER_PATH), "SPEAKER_AUDIO_DIR": str(app.SPEAKER_AUDIO_DIR),
    "VOICES_DIR": str(app.VOICES_DIR), "OUTPUTS_DIR": str(app.OUTPUTS_DIR),
    "JOBS_HIST_PATH": str(app._JOBS_HIST_PATH), "METRICS_DIR": str(metrics_hist.DIR)}}))
"""


def test_app_importado_depois_de_isolar_nao_ve_nenhum_caminho_do_dono(tmp_path):
    dono = tmp_path / "dono"
    env = dict(os.environ)
    # o pior caso: o shell do dono exporta os caminhos dele (#461: venceriam o
    # TTS_STATE_DIR se o isolamento só fixasse o diretório)
    env["TTS_STATE_DIR"] = str(dono)
    for nome in ("TTS_SETTINGS_PATH", "TTS_APIKEYS_PATH", "TTS_SPEAKER_PATH",
                 "TTS_VOICES_DIR", "TTS_OUTPUTS_DIR", "TTS_METRICS_DIR"):
        env[nome] = str(dono / nome.lower())
    env.setdefault("TTS_LIVE_WORKER", "0")
    saida = subprocess.run([sys.executable, "-c", _SONDA, str(BAKEOFF), str(RAIZ)],
                           env=env, capture_output=True, text=True, timeout=300,
                           cwd=str(tmp_path))
    assert saida.returncode == 0, saida.stderr[-1500:]
    dados = json.loads(saida.stdout.strip().splitlines()[-1])
    raiz = Path(dados["raiz"])
    for nome, caminho in dados["caminhos"].items():
        c = Path(caminho)
        assert c == raiz or raiz in c.parents, f"{nome} fora do tmp isolado: {c}"
        assert dono not in c.parents and RAIZ not in c.parents, f"{nome}: {c}"
    assert not raiz.exists(), "o tmp do estado ficou para trás"


def test_isolar_depois_do_import_app_levanta(tmp_path):
    sonda = ("import sys, types; sys.path.insert(0, sys.argv[1]); "
             "sys.modules['app'] = types.ModuleType('app'); import comum\n"
             "try:\n    comum.isola_estado()\nexcept RuntimeError:\n    print('levantou')\n")
    saida = subprocess.run([sys.executable, "-c", sonda, str(BAKEOFF)],
                           capture_output=True, text=True, timeout=60, cwd=str(tmp_path))
    assert saida.stdout.strip() == "levantou", saida.stdout + saida.stderr[-800:]
