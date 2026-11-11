"""A bancada do Live não lê voz do dono (#728).

A receita antiga (`d88fa54`, #875) mandava `cp voices/*.wav voices/*.json
/tmp/live-bench-audio/`: a bancada rodava sobre CÓPIA do cadastro de vozes do
dono, e a checagem por caminho (`voices` no path) não via a cópia. Agora o corpus
é público (FLEURS pt_br, `tools/corpus_bancada.py`), fica fora do `/tmp` (o
`tmp_cleaner` do macOS o apagava à meia-noite) e a bancada RECUSA:
  * corpus dentro de `voices/`;
  * JSON no formato do cadastro de voz (`created_at`, `denoised`, …) — a cópia.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

import numpy as np
import pytest
import soundfile as sf

RAIZ = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "tools"))

import corpus_bancada  # noqa: E402
import live_bench as lb  # noqa: E402


def _wav(p: pathlib.Path, f0: float, s: float = 1.5, sr: int = 16000) -> None:
    t = np.arange(int(sr * s)) / sr
    x = 0.3 * np.sin(2 * np.pi * f0 * t) * (0.5 + 0.5 * np.sin(2 * np.pi * 2 * t))
    sf.write(str(p), x.astype(np.float32), sr, subtype="PCM_16")


def _fleurs_falso(raiz: pathlib.Path, n: int = 4) -> pathlib.Path:
    """`fala_pt/` no formato do `wer_pt_real.monta` (meta.json com `refs`)."""
    src = raiz / "fala_pt"
    src.mkdir(parents=True)
    refs = {}
    for i in range(n):
        nome = f"{1000 + i}.wav"
        _wav(src / nome, 160 + 40 * i)
        refs[nome] = f"frase publica numero {i}"
    (src / "meta.json").write_text(json.dumps({"refs": refs}), encoding="utf-8")
    return src


def test_corpus_publico_sai_no_formato_da_bancada_e_e_aceito(tmp_path):
    src = _fleurs_falso(tmp_path)
    voz = corpus_bancada.monta(src, tmp_path / "corpus-live")
    assert voz == "flvoz"
    corpus = tmp_path / "corpus-live" / "corpus"
    jsons = sorted(corpus.glob("*.json"))
    assert [j.stem for j in jsons] == ["fl00", "fl01", "fl02"]      # a última é a voz
    for j in jsons:
        assert set(json.loads(j.read_text())) == {"id", "name", "ref_text"}
    assert (tmp_path / "corpus-live" / "voices" / "flvoz.wav").is_file()
    assert len(lb.carrega_corpus(corpus)) == 3


def test_corpus_publico_e_idempotente(tmp_path):
    src = _fleurs_falso(tmp_path)
    corpus_bancada.monta(src, tmp_path / "d")
    (tmp_path / "d" / "corpus" / "lixo.json").write_text("{}")
    corpus_bancada.monta(src, tmp_path / "d")
    assert not (tmp_path / "d" / "corpus" / "lixo.json").exists()


def test_recusa_corpus_dentro_de_voices(tmp_path):
    pasta = tmp_path / "voices"
    pasta.mkdir()
    with pytest.raises(SystemExit, match="#728"):
        lb.carrega_corpus(pasta)


def test_recusa_copia_do_cadastro_de_voz_fora_de_voices(tmp_path):
    """A cópia em /tmp furava a checagem por caminho: o formato do JSON pega."""
    pasta = tmp_path / "live-bench-audio"
    pasta.mkdir()
    _wav(pasta / "ab12cd.wav", 200)
    (pasta / "ab12cd.json").write_text(json.dumps({
        "id": "ab12cd", "name": "Fulana", "created_at": "2026-08-05 01:10:38",
        "duration": 1.5, "denoised": True, "ref_text": "uma frase"}), encoding="utf-8")
    _wav(pasta / "ef34gh.wav", 260)
    (pasta / "ef34gh.json").write_text(json.dumps(
        {"id": "ef34gh", "name": "ef34gh", "ref_text": "outra"}), encoding="utf-8")
    with pytest.raises(SystemExit, match="cadastro de voz"):
        lb.carrega_corpus(pasta)


def test_corpus_bancada_recusa_fonte_dentro_de_voices(tmp_path):
    with pytest.raises(SystemExit, match="#728"):
        corpus_bancada.monta(tmp_path / "voices" / "fala_pt", tmp_path / "d")


# ── receitas: nenhuma instrução viva de copiar voices/ ─────────────────────────
_RECEITA = re.compile(r"cp\s[^\n`]*\bvoices/")


@pytest.mark.parametrize("arq", sorted(
    [p for p in (RAIZ / "tools").rglob("*") if p.suffix in (".py", ".sh", ".md")],
    key=str), ids=lambda p: str(p.relative_to(RAIZ)))
def test_tools_nao_mandam_copiar_voices(arq):
    texto = arq.read_text(encoding="utf-8", errors="replace")
    achados = [l for l in texto.splitlines()
               if _RECEITA.search(l) and "receita antiga" not in l]
    assert not achados, f"{arq.name} manda copiar voices/: {achados}"


def test_rodada_usa_corpus_publico_fora_do_tmp_e_a_voz_da_bancada():
    s = (RAIZ / "tools" / "rodada_c0c1.sh").read_text(encoding="utf-8")
    assert "/tmp/live-bench-audio" not in s and "/tmp/bench-mini" not in s
    assert 'CORPUS="$BANCADA/corpus-live/corpus"' in s
    # sem isso a 7890 clona a partir do voices/ do repo (o do dono)
    assert 'TTS_VOICES_DIR="$VOZES"' in s
    assert s.count('--voice "$VOZ_SESSAO"') == 2          # live_bench e fase_ruido
    assert "recusa_dado_do_dono" in s


def test_mede_pos_llm_monta_pelo_corpus_bancada_fora_do_tmp():
    s = (RAIZ / "tools" / "mede_pos_llm.sh").read_text(encoding="utf-8")
    assert "/tmp/tts-bakeoff" not in s
    assert 'FLEURS="${FLEURS:-$HOME/tts-bench/fleurs/fala_pt}"' in s
    assert "tools/corpus_bancada.py" in s
