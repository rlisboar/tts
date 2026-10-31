"""Suíte de UI não pode LER o estado do dono da raiz (regra do #720/P1 #643).

O sintoma que trouxe isto: o `live_ui` lia `RAIZ/settings.json` (com a chave
secreta) e `voices/*.wav` (vozes clonadas, insubstituíveis) para dentro da pasta da
rodada — e o hábito de "copiar do dono para validar" nasceu daí. Conserto: sempre
SINTÉTICO (settings `{}` em tmp, fala da fixture versionada).

Duas células, de propósito:

* **canário**: monta uma raiz FALSA com `settings.json`/`.apikeys.json` contendo
  uma isca, roda uma suíte de verdade lá dentro e cobra que a isca NÃO apareça na
  pasta da rodada. Prova o efeito, não a intenção de quem escreveu.
* **estática**: nenhuma suíte pode voltar a apontar para o estado da raiz. Há uma
  lista de exceções com o porquê (leitura só de COMPARAÇÃO, nunca fonte de dados).
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
AQUI = Path(__file__).resolve().parent
PY = RAIZ / ".venv-mlx" / "bin" / "python"
ISCA = "ISCA-CANARIO-NAO-CHEGUE-NA-RODADA"

#: suítes que PODEM tocar o estado da raiz, com o motivo. Todas são leitura de
#: COMPARAÇÃO (régua de "o dono não foi tocado"), nunca fonte do que a rodada usa.
EXCECOES = {
    # guarda anti-vazamento: lê o `.apikeys.json` do dono para provar que a rodada
    # não escreveu nele (#463) — se ler, compara; não copia para a pasta
    "live_ws.sh": (r'\.apikeys\.json"', r'\.apikey"', r'RAIZ / "settings\.json"'),
}

#: caminhos do estado do dono que a suíte não pode ler como FONTE
PADROES = (
    r'RAIZ / "settings\.json"',
    r'RAIZ/"settings\.json"',
    r'RAIZ / "voices"',
    r'RAIZ/"voices"',
    r'RAIZ / "\.speaker-profiles\.json"',
    r'RAIZ/"\.speaker-profiles\.json"',
)


def _suites() -> list[Path]:
    return sorted(p for p in AQUI.glob("*.sh") if p.name != "serial.sh")


def test_nenhuma_suite_usa_o_estado_da_raiz_como_fonte():
    problemas = []
    for arq in _suites():
        texto = arq.read_text(errors="replace")
        for padrao in PADROES:
            for m in re.finditer(padrao, texto):
                trecho = texto[max(0, m.start() - 40):m.end() + 40].splitlines()
                if arq.name in EXCECOES and any(
                        re.search(permitido, m.group(0)) for permitido in EXCECOES[arq.name]):
                    continue
                linha = texto[:m.start()].count("\n") + 1
                problemas.append(f"{arq.name}:{linha} `{m.group(0)}` "
                                 f"({trecho[len(trecho) // 2].strip()[:60]})")
    assert not problemas, (
        "suíte lendo estado do dono da raiz — use `{}` em tmp e a fixture "
        "`tests/fixtures/voz_fala_5s_16k.wav`:\n  " + "\n  ".join(problemas))


def _isca_wav(raiz: Path) -> None:
    """`voices/<isca>.wav` com 0,9 s — ABAIXO do filtro de 1 s das suítes.

    Assim, quem voltar a usar glob em `voices/` pega a isca e morre no filtro (não
    cai na fixture de novo): o efeito é uma falha clara, não um falso verde.
    """
    import soundfile as sf
    import numpy as np

    (raiz / "voices").mkdir(parents=True, exist_ok=True)
    (raiz / "voices" / "isca.wav").write_bytes(b"RIFF")
    sf.write(str(raiz / "voices" / "isca.wav"), np.zeros(int(0.9 * 16000), dtype="<f4"),
             16000, subtype="PCM_16")


def _raiz_falsa(tmp_path: Path) -> Path:
    """Raiz de mentira com o estado do dono envenenado pela isca.

    Só o que a suíte precisa de verdade (o resto continua no repo, por symlink) e
    um `settings.json`/`.apikeys.json` que DENUNCIAM leitura.
    """
    raiz = tmp_path / "raiz"
    (raiz / "tests").mkdir(parents=True)
    for nome in ("app.py", "common.py", "backends.py", "tts_worker.py", "tts_cache.py",
                 "metrics_hist.py", "live_turns.py", "live_pipeline.py", "live_rnnoise.py",
                 "live_intencao.py", "intencao_worker.py", "dsh_client.py"):
        alvo = RAIZ / nome
        if alvo.exists():
            (raiz / nome).symlink_to(alvo)
    for nome in ("static", ".venv-mlx"):
        alvo = RAIZ / nome
        if alvo.exists():
            (raiz / nome).symlink_to(alvo)
    for nome in ("serial.sh",):
        (raiz / "tests" / nome).symlink_to(AQUI / nome)
    if (AQUI / "fixtures").exists():
        (raiz / "tests" / "fixtures").symlink_to(AQUI / "fixtures")
    # as suítes vão COPIADAS (o `RAIZ` delas vem do próprio caminho do script)
    for nome in ("hash_deep_link.sh", "obs_ui.sh"):
        shutil.copy(AQUI / nome, raiz / "tests" / nome)
        os.chmod(raiz / "tests" / nome, 0o755)
    (raiz / "settings.json").write_text('{"chat_model": "%s"}\n' % ISCA)
    (raiz / ".apikeys.json").write_text('{"keys": [{"name": "%s", "secret": "%s"}]}\n'
                                        % (ISCA, ISCA))
    (raiz / ".apikey").write_text(ISCA + "\n")
    _isca_wav(raiz)
    return raiz


def test_suite_roda_sem_ler_o_estado_da_raiz(tmp_path):
    """Com a raiz envenenada, a pasta da rodada tem de sair limpa da isca."""
    if not PY.exists():
        pytest.skip("sem venv para subir o app")
    raiz = _raiz_falsa(tmp_path)
    antes = set(glob.glob("/tmp/hash_deep_*"))
    r = subprocess.run([str(raiz / "tests" / "hash_deep_link.sh")], cwd=str(raiz),
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, (r.stdout + r.stderr)[-2000:]
    novos = set(glob.glob("/tmp/hash_deep_*")) - antes
    assert novos, "a rodada não deixou o arquivo sintético (caminho mudou?)"
    for nome in novos:
        p = Path(nome)
        if p.is_file():
            assert ISCA not in p.read_text(errors="replace"), (
                f"a rodada leu o estado da raiz: {p} tem a isca")
    for nome in novos:                      # não deixa lixo do teste
        try:
            Path(nome).unlink()
        except OSError:
            pass


def test_suite_usa_a_fixture_mesmo_com_voz_na_raiz(tmp_path):
    """`voices/` da raiz com uma isca curta: a suíte tem de seguir na fixture.

    A isca tem 0,9 s e as suítes descartam áudio com menos de 1 s — se voltarem a
    fazer glob em `voices/`, pegam a isca e morrem no filtro.
    """
    if not PY.exists():
        pytest.skip("sem venv para subir o app")
    raiz = _raiz_falsa(tmp_path)
    antes = set(glob.glob("/tmp/obs_ui_estado_*"))
    r = subprocess.run([str(raiz / "tests" / "obs_ui.sh")], cwd=str(raiz),
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, (r.stdout + r.stderr)[-2000:]
    novos = set(glob.glob("/tmp/obs_ui_estado_*")) - antes
    assert novos, "a rodada não deixou a pasta de estado (caminho mudou?)"
    for d in novos:
        cfg = Path(d) / "settings.json"
        if cfg.exists():
            assert ISCA not in cfg.read_text(errors="replace"), (
                f"a rodada copiou o settings da raiz para {cfg}")
    for d in novos:
        shutil.rmtree(d, ignore_errors=True)


def test_raiz_falsa_envenenada_de_fato(tmp_path):
    """Controle: se a suíte voltar a ler a raiz, a isca aparece (a célula morde)."""
    raiz = _raiz_falsa(tmp_path)
    assert ISCA in (raiz / "settings.json").read_text()
    escrito = tmp_path / "espelho.json"
    escrito.write_text((raiz / "settings.json").read_text())
    assert ISCA in escrito.read_text()
