"""#728 (P1): suíte de integração e ferramenta não leem nem gravam o `voices/` da raiz.

Origem: `live_ws.sh`, `live_ui.sh`, `obs_ui.sh`, `live_barge_rep.sh` e
`live_dsh_fallback.sh` usavam `voices/*.wav` da árvore como fala do mic falso, e a
instância que subiam ainda apontava o `VOICES_DIR` do app para a árvore. Num worktree
o `voices/` nasce vazio e ninguém notava; rodada no checkout principal pegava as
gravações do dono (clonadas, insubstituíveis). A fala agora é a fixture versionada
(`tests/fixtures/voz_fala_5s_16k.wav`) e a instância sobe com `TTS_VOICES_DIR` /
`TTS_OUTPUTS_DIR` / `TTS_SPEAKER_PATH` em tmp.

Três coisas guardadas, de propósito estáticas (uma suíte de integração de verdade
custaria Chromium e minutos, e o que regride é uma linha):

* nenhuma linha de código (comentário não conta) de `tests/*.sh` ou `tools/*` referencia
  o `voices/` da árvore como fonte ou destino, fora da lista de exceções justificadas;
* toda suíte que SOBE o app (`uvicorn.run`/`import app`) passa `TTS_VOICES_DIR` e
  `TTS_OUTPUTS_DIR`: sem isso o app resolve o `voices/` da própria árvore;
* o detector tem mordida própria: as células de `_achados` provam que ele pega a receita
  velha e deixa passar o que é legítimo (voices em tmp, comentário, prosa).
"""
from __future__ import annotations

import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent

#: formas de tocar o `voices/` DA ÁRVORE. `voices/` colado a crase (prosa) e `$DIR/voices`
#: (pasta de tmp) não contam: o lookbehind exige que o token COMECE em `voices/`.
_PADROES = (
    r"(?<![\w/`.$}\-])voices/",                 # `cp voices/*.wav`, "voices/x.wav", |voices/*|
    r"(?<![\w/`.$}\-])\./voices\b",
    r"\bRAIZ\s*/\s*[\"']voices[\"']",           # RAIZ / "voices"
    r"\bBASE\s*/\s*[\"']voices[\"']",
    r"\$\{?RAIZ\}?/voices\b",                   # "$RAIZ/voices"
    r"Path\(\s*[\"']voices\b",                  # Path("voices"), pathlib.Path("voices/…")
    r"glob\([^)\n]*\bvoices\b",                 # glob("voices/*.wav")
)
_RE = re.compile("|".join(f"(?:{p})" for p in _PADROES))

#: arquivo -> por que pode citar `voices/` no CÓDIGO. Nada aqui lê ou escreve o `voices/`
#: do dono; se aparecer um motivo novo, ele entra com o porquê escrito.
EXCECOES = {
    "tools/integra.sh": "lista de exclusão do que NÃO pode ir para a main (impede a cópia)",
    "tools/worktrees.sh": "documenta e VERIFICA que o worktree nasce sem `voices/` (só lê sha)",
    "tests/live_lock_freeze.sh": "copia a FIXTURE para o `voices/` da árvore TEMPORÁRIA da rodada",
    "tools/live_bench.py": "receita velha `cp voices/*` — sai no #1125 (speech-pipeline, "
                           "wt/speech-pipeline-728); sem esta linha o #728 esperaria por ele",
}


def _codigo(texto: str) -> list[tuple[int, str]]:
    """Linhas de código: comentário inteiro (`# …`) fica de fora."""
    return [(i, l) for i, l in enumerate(texto.splitlines(), 1) if not l.lstrip().startswith("#")]


def _achados(texto: str) -> list[tuple[int, str]]:
    return [(i, l.strip()[:90]) for i, l in _codigo(texto) if _RE.search(l)]


def _arquivos() -> list[Path]:
    achados = sorted((RAIZ / "tests").glob("*.sh"))
    for ext in ("sh", "py"):
        achados += sorted((RAIZ / "tools").glob(f"*.{ext}"))
    return achados


def test_nenhum_script_toca_o_voices_da_arvore():
    problemas = []
    for arq in _arquivos():
        rel = arq.relative_to(RAIZ).as_posix()
        if rel in EXCECOES:
            continue
        for i, linha in _achados(arq.read_text(errors="replace")):
            problemas.append(f"{rel}:{i}  {linha}")
    assert not problemas, (
        "script lê/grava o `voices/` da árvore (voz do DONO no checkout principal, #728). "
        "Use `tests/fixtures/voz_fala_5s_16k.wav`/`voz_sintetica/` ou o corpus FLEURS, e "
        "`TTS_VOICES_DIR` em tmp:\n  " + "\n  ".join(problemas))


def test_excecoes_existem_e_tem_motivo():
    for rel, motivo in EXCECOES.items():
        assert (RAIZ / rel).is_file(), f"exceção de arquivo que sumiu: {rel}"
        assert len(motivo) > 20, f"exceção sem motivo de verdade: {rel}"


def test_toda_suite_que_sobe_o_app_isola_voz_e_saidas():
    sobe = re.compile(r"uvicorn\.run\(|^\s*import (?:\w+, )*app\b", re.M)
    sem = []
    for arq in sorted((RAIZ / "tests").glob("*.sh")):
        texto = arq.read_text(errors="replace")
        if not sobe.search(texto):
            continue
        faltam = [v for v in ("TTS_VOICES_DIR", "TTS_OUTPUTS_DIR") if v not in texto]
        if faltam:
            sem.append(f"{arq.name}: sobe o app sem {', '.join(faltam)}")
    assert not sem, ("a instância de teste resolveria `BASE / \"voices\"` (a do dono no "
                     "checkout principal):\n  " + "\n  ".join(sem))


def test_as_cinco_suites_usam_so_a_fixture_como_fala():
    """A ÚNICA fonte de fala é a fixture: a lista do `for` é literal e de um elemento."""
    fonte = {
        "live_ws": 'wavs = [RAIZ / "tests" / "fixtures" / "voz_fala_5s_16k.wav"]',
        "live_ui": 'for w in [pathlib.Path("tests/fixtures/voz_fala_5s_16k.wav")]:',
        "obs_ui": 'for w in [pathlib.Path("tests/fixtures/voz_fala_5s_16k.wav")]:',
        "live_barge_rep": 'for w in [pathlib.Path("tests/fixtures/voz_fala_5s_16k.wav")]:',
        "live_dsh_fallback": 'for w in [pathlib.Path("tests/fixtures/voz_fala_5s_16k.wav")]:',
    }
    assert (RAIZ / "tests/fixtures/voz_fala_5s_16k.wav").is_file()
    for nome, linha in fonte.items():
        texto = (RAIZ / "tests" / f"{nome}.sh").read_text()
        assert texto.count(linha) == 1, f"{nome}.sh: a fala não sai SÓ da fixture ({linha!r})"
        assert "TTS_VOICES_DIR" in texto, f"{nome}.sh sobe o app sem isolar o voices/"


# ── o detector tem dente ────────────────────────────────────────────────────────
def test_detector_pega_a_receita_velha_e_as_variantes():
    ruins = [
        "cp voices/*.wav /tmp/live-bench-audio/",
        'wavs = sorted(glob.glob("voices/*.wav"))',
        'wavs = sorted((RAIZ / "voices").glob("*.wav"))',
        'for w in pathlib.Path("voices").glob("*.wav"):',
        'cp "$RAIZ/voices/a.wav" /tmp/x',
        'args=(--audio ./voices)',
        'x = BASE / ' + '"voices"',   # montado: a varredura do #1018 não casa com esta amostra
        'raise SystemExit("sem voices/*.wav")',
    ]
    for linha in ruins:
        assert _achados(linha), f"o detector deixou passar: {linha!r}"


def test_detector_deixa_passar_o_que_e_legitimo():
    bons = [
        '# cp voices/*.wav /tmp   (comentário)',
        '    # a fala do mic falso é SINTÉTICA: nada é lido do `voices/` do dono',
        'Num worktree não há `voices/` e sem voz registrada o turno não gera áudio.',
        'for _s in ("voices", "outputs"): (_VZ / _s).mkdir(parents=True, exist_ok=True)',
        '"TTS_VOICES_DIR": str(DIR_TMP / "voices"),',
        'mkdir -p "$DIR/voices" "$DIR/outputs"',
        'req = urllib.request.Request(BASE + "/api/voices", data=corpo, method="POST")',
        'cp -n "$RAIZ/tests/fixtures/voz_sintetica/voz.wav" "$ARVORE/voices/"',   # destino tmp (exceção própria)
    ]
    # o último é o `cp` do live_lock_freeze: o detector o pega pelo `$ARVORE/voices/`? não —
    # o lookbehind exclui `/`, e é por isso que o arquivo NÃO precisa da exceção por esta linha
    for linha in bons:
        assert not _achados(linha), f"falso positivo: {linha!r}"
