"""Mede a latência do caminho dsh no pipeline REAL e CARIMBA a rota (#160/#154).

Por que existe: no caminho dsh existe fallback para o openai (#146) e a corrida do
`-32602` (#162) já derrubou turno no fallback em ~30% — o openai responde em ~0,87 s
e se disfarça de "dsh rápido". Um número de latência do dsh só vale se disser em que
ESTADO e por qual ROTA foi medido; este script falha alto se o turno não foi do dsh.

O que mede, por turno (não só o 1º): `first_token_ms`, fim-de-fala → 1º áudio e os
INTERVALOS entre deltas (fração ≤1 s e maior gap) — a assinatura que o #160 persegue
é "1 delta adiantado e o resto em bloco", que só aparece olhando os intervalos.

Uso (o `~/.dsh` costuma ficar fora do workspace em shell sandboxed; monte DSH_HOME):

    DSH_HOME=/tmp/dsh-home PYTHONPATH=$PWD \
        ./.venv-mlx/bin/python evidence/dsh-live-latency-spike.py [TURNOS]

Notas: exige o patch do bridge para o cenário incremental (o script carimba o estado
via `dsh_client.estado_bridge()`); roda melhor na trava `tests/serial.sh`, porque as
suítes de áudio disputam Metal. Fala curta com a persona que o Live injeta.
"""

import pathlib
import statistics
import sys
import time

import numpy as np
import soundfile as sf

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import dsh_client  # noqa: E402
import live_pipeline as lp  # noqa: E402

RAIZ = pathlib.Path(__file__).resolve().parent.parent
TURNOS = int(sys.argv[1]) if len(sys.argv) > 1 else 5


def _sha8(arquivo) -> str:
    """sha256-8 do arquivo do bridge — o mesmo carimbo que o `--status` publica."""
    import hashlib
    if not arquivo:
        return "—"
    try:
        return hashlib.sha256(pathlib.Path(str(arquivo)).read_bytes()).hexdigest()[:8]
    except OSError:
        return "—"


def pcm_de_teste() -> bytes:
    """Fala REAL de voices/ (o STT roda de verdade); 16 kHz mono PCM16."""
    wav = sorted((RAIZ / "voices").glob("*.wav"))[0]
    a, sr = sf.read(str(wav), dtype="float32", always_2d=True)
    a = a.mean(axis=1)
    if sr != 16000:
        n = int(len(a) * 16000 / sr)
        a = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a).astype("float32")
    return (np.clip(a, -1, 1) * 32767).astype("<i2").tobytes()


def main() -> int:
    info = dsh_client.estado_bridge() or {}
    estado = info.get("estado", "unknown")
    print(f"bridge: {estado} · sha256-8: {_sha8(info.get('arquivo'))} · "
          f"turnos: {TURNOS} · voz: {sorted((RAIZ / 'voices').glob('*.wav'))[0].name}")
    if estado != "patched":
        print("  ! sem o patch do bridge o cenário é o de CHUNK ÚNICO — diga isso no número")

    cwd = RAIZ / "outputs" / ".dsh-cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    cli = dsh_client.DshClient(bin=None, profile=dsh_client.DSH_DEFAULT_PROFILE,
                               model=dsh_client.DSH_DEFAULT_MODEL, effort="off",
                               cwd=cwd, on_log=lambda m: None)
    ev, aud = [], []
    pipe = lp.LivePipeline(lambda o: ev.append((time.perf_counter(), o)),
                           lambda b: aud.append((time.perf_counter(), b)),
                           voice_id=None, dsh=cli)
    pcm = pcm_de_teste()

    t = time.perf_counter()
    pipe.start()
    print(f"prewarm_ms: {int((time.perf_counter() - t) * 1000)} (fora do turno)")

    amostras, ttfts, fallback = [], [], False
    for i in range(TURNOS):
        ev.clear()
        aud.clear()
        pipe.push_pcm(pcm)
        t = time.perf_counter()
        pipe.end_of_speech(inicia_thread=False)
        marcas = [ts - t for ts, e in ev if e.get("type") == "assistant_text"]
        gaps = [b - a for a, b in zip(marcas, marcas[1:])]
        lat = next((e for _, e in ev if e.get("type") == "latency"), {})
        audio = None if not aud else int((aud[0][0] - t) * 1000)
        if getattr(pipe, "_dsh_indisponivel", False):
            fallback = True
        if audio is not None:
            amostras.append(audio)
        if lat.get("first_token_ms") is not None:
            ttfts.append(lat["first_token_ms"])
        pct = round(100 * sum(1 for m in marcas if m <= 1) / len(marcas)) if marcas else 0
        print(f"turno{i}: 1o_audio={audio}ms first_token={lat.get('first_token_ms')}ms "
              f"n_deltas={len(marcas)} pct_deltas_em_1s={pct}% "
              f"gap_max={int(max(gaps) * 1000) if gaps else 0}ms chars="
              f"{len(''.join(e.get('delta', '') for _, e in ev if e.get('type') == 'assistant_text'))}")

    pipe.close()
    if getattr(pipe, "_dsh_indisponivel", False):
        print(f"  ! ROTA = FALLBACK/openai ({pipe._dsh_motivo}) — estes números NÃO são do dsh")
    if amostras:
        print(f"MEDIANA_1o_audio_ms: {int(statistics.median(amostras))} · amostras {amostras}")
    if ttfts:
        print(f"MEDIANA_first_token_ms: {int(statistics.median(ttfts))}")
    return 1 if fallback else 0


if __name__ == "__main__":
    raise SystemExit(main())