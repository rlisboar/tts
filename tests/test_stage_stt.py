"""#972: o fechamento do estágio stt para no próximo stt da sessão.

Dois `stage_fim stage=stt` na mesma sessão e só o segundo chega em
`stage_inicio stage=tts`. O primeiro para nesse stt e não herda o tts.
"""

import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SCRIPT = BASE / "tools" / "stage_stt.py"

LOG = """\
16:36:11 [live] stage_fim sess=aaa stage=stt ms=115
16:36:12 [live] stage_inicio sess=aaa stage=stt
16:36:13 [live] stage_fim sess=aaa stage=stt ms=1024
16:36:13 [live] stage_inicio sess=aaa stage=llm
16:36:15 [live] stage_fim sess=aaa stage=llm ms=2000
16:36:15 [live] stage_inicio sess=aaa stage=tts
"""


def test_dois_stt_o_primeiro_nao_herda_o_tts(tmp_path):
    log = tmp_path / "app.log"
    log.write_text(LOG)
    r = subprocess.run(
        [sys.executable, str(SCRIPT), str(log), "--minimo-ms", "0"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert r.returncode == 0, r.stderr
    linhas = [ln.strip() for ln in r.stdout.splitlines() if "fecha=" in ln]
    assert any(ln.startswith("115 ms") and "fecha=stt" in ln for ln in linhas)
    assert any(ln.startswith("1024 ms") and "fecha=tts" in ln for ln in linhas)
    assert not any("115 ms" in ln and "fecha=tts" in ln for ln in linhas)
