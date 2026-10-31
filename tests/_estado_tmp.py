"""Estado do app em TMP para scripts que importam `app` direto (#710/#711).

O `conftest` faz isso para o pytest, mas smoke/medição invocadas à mão importam o
`app` SEM o conftest: qualquer `_save_settings()` então grava `settings.json` no
DIRETÓRIO DO WORKTREE, e o worktree passa a ter estado do dono — que nunca devia
estar ali (decisão do dono, `tools/worktrees.sh`).

Use antes de `import app`:

    from _estado_tmp import isola
    isola()
    import app

`TTS_ESTADO_TMP=<dir>` reaproveita um diretório (rodada em série); sem ele o tmp é
novo a cada processo e sai no fim.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

_CHAVES = ("TTS_SETTINGS_PATH", "TTS_APIKEYS_PATH", "TTS_LEGACY_APIKEY_PATH",
           "TTS_METRICS_DIR")


def isola() -> Path:
    """Aponta TODO o estado do app para um tmp e devolve o diretório."""
    fixo = (os.environ.get("TTS_ESTADO_TMP") or "").strip()
    dir_ = Path(fixo) if fixo else Path(tempfile.mkdtemp(prefix="tts-estado-"))
    dir_.mkdir(parents=True, exist_ok=True)
    os.environ["TTS_SETTINGS_PATH"] = str(dir_ / "settings.json")
    os.environ["TTS_APIKEYS_PATH"] = str(dir_ / "apikeys.json")
    os.environ["TTS_LEGACY_APIKEY_PATH"] = str(dir_ / "apikey")
    os.environ["TTS_METRICS_DIR"] = str(dir_ / "metrics")
    if not fixo:
        atexit.register(shutil.rmtree, dir_, True)
    return dir_
