"""Histórico de operação: uma linha JSONL por requisição, agregado por janela.

POR QUE EXISTE (#376): o `/api/status` mostra o AGORA. Pergunta do tipo "o Live
ficou lento ontem" ou "quantos 429 de admissão" não tinha onde ser respondida —
nada de latência, erro ou TTFT em série temporal. Aqui cada requisição de API vira
uma linha append-only, e o `/api/metrics` (admin) agrega por rota e janela.

O QUE NUNCA ENTRA: texto, áudio, chave. Só metadados — `ts`, rota NORMALIZADA
(`/api/tts/jobs/*/pieces/*`), `ms`, código HTTP, estágio e id do backend de IA.

Formato no disco (`state/metrics/AAAA-MM-DD.jsonl`, um arquivo por DIA, com sufixo
`-N` quando o dia passa do teto de tamanho):

    {"ts": 1792627533, "rota": "/api/tts", "ms": 41.7, "cod": 200, "est": "tts", "bk": "mlx"}

Retenção: `metrics_ttl_dias` (setting) poda por idade; `metrics_teto_mb` é o teto
por arquivo. A poda roda junto com cada gravação, sem timer próprio — o custo é um
`stat` de diretório a cada N gravações.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
DIR = BASE / "state" / "metrics"

TTL_PADRAO_DIAS = 14
TETO_PADRAO_MB = 8

_lock = threading.Lock()
_contador = 0            # gravações desde a última poda
PODA_A_CADA = 200        # poda por contagem: sem timer, sem thread


def _dir() -> Path:
    DIR.mkdir(parents=True, exist_ok=True)
    return DIR


def _arquivo_do_dia(dia: str, teto_bytes: int) -> Path:
    """Arquivo do dia, abrindo sufixo `-N` quando o anterior estourou o teto."""
    base = _dir() / f"{dia}.jsonl"
    if not base.exists() or base.stat().st_size < teto_bytes:
        return base
    n = 2
    while True:
        alvo = _dir() / f"{dia}-{n}.jsonl"
        if not alvo.exists() or alvo.stat().st_size < teto_bytes:
            return alvo
        n += 1


def registrar(rota: str, ms: float, cod: int, est: str = "", bk: str = "",
              *, ttl_dias: int = TTL_PADRAO_DIAS, teto_mb: int = TETO_PADRAO_MB) -> None:
    """Grava uma linha. Nunca levanta: histórico não pode derrubar request."""
    global _contador
    try:
        dia = time.strftime("%Y-%m-%d")
        linha = json.dumps({"ts": int(time.time()), "rota": rota[:120],
                            "ms": round(float(ms), 1), "cod": int(cod),
                            "est": est[:12], "bk": bk[:24]}, separators=(",", ":"))
        with _lock:
            alvo = _arquivo_do_dia(dia, max(1, teto_mb) * 1024 * 1024)
            with open(alvo, "a", encoding="utf-8") as f:
                f.write(linha + "\n")
            _contador += 1
            if _contador >= PODA_A_CADA:
                _contador = 0
                podar(ttl_dias)
    except Exception:  # noqa: BLE001 — métrica é acessório, nunca fatal
        pass


def podar(ttl_dias: int = TTL_PADRAO_DIAS) -> int:
    """Apaga arquivos mais velhos que o TTL. Devolve quantos saíram."""
    limite = time.time() - max(0, int(ttl_dias)) * 86400
    saiu = 0
    try:
        for p in DIR.glob("*.jsonl"):
            try:
                if p.stat().st_mtime < limite:
                    p.unlink()
                    saiu += 1
            except OSError:
                continue
    except OSError:
        pass
    return saiu


def _percentil(valores: list[float], q: float) -> float:
    if not valores:
        return 0.0
    # posto INFERIOR (nearest-rank, sem interpolação): determinístico entre
    # plataformas e conservador — com [10,20,30,40] o p50 é 20, não 30.
    ordenado = sorted(valores)
    pos = min(len(ordenado) - 1, max(0, int(q * (len(ordenado) - 1))))
    return round(ordenado[pos], 1)


def ler(janela_s: int, rota: str | None = None) -> list[dict]:
    """Linhas dentro da janela (segundos para trás), opcionalmente de uma rota."""
    desde = time.time() - max(1, int(janela_s))
    fora: list[dict] = []
    for p in sorted(DIR.glob("*.jsonl")):
        try:
            if p.stat().st_mtime < desde - 86400:   # dia inteiro antes da janela
                continue
            with open(p, encoding="utf-8") as f:
                for bruta in f:
                    try:
                        d = json.loads(bruta)
                    except ValueError:
                        continue
                    if d.get("ts", 0) < desde:
                        continue
                    if rota and d.get("rota") != rota:
                        continue
                    fora.append(d)
        except OSError:
            continue
    return fora


def agregar(janela_s: int, rota: str | None = None) -> dict:
    """p50/p95 e taxa de erro por rota na janela. Sem rota = todas."""
    linhas = ler(janela_s, rota)
    por_rota: dict[str, list[dict]] = {}
    for d in linhas:
        por_rota.setdefault(str(d.get("rota") or "?"), []).append(d)

    rotas = []
    for nome, itens in sorted(por_rota.items(), key=lambda kv: -len(kv[1])):
        ms = [float(i.get("ms") or 0.0) for i in itens]
        erros = sum(1 for i in itens if int(i.get("cod") or 0) >= 400)
        estagios = sorted({i.get("est") for i in itens if i.get("est")})
        backends = sorted({i.get("bk") for i in itens if i.get("bk")})
        rotas.append({
            "rota": nome, "n": len(itens),
            "p50": _percentil(ms, 0.50), "p95": _percentil(ms, 0.95),
            "ms_max": round(max(ms), 1) if ms else 0.0,
            "erros": erros, "erro_taxa": round(erros / len(itens), 3),
            "estagios": estagios, "backends": backends,
        })
    total = len(linhas)
    return {"janela_s": int(janela_s), "total": total, "rotas": rotas}


def tamanho_por_dia() -> list[dict]:
    """Tamanho e linhas por arquivo — a medição que o aceite pede."""
    fora = []
    for p in sorted(DIR.glob("*.jsonl")):
        try:
            linhas = sum(1 for _ in open(p, encoding="utf-8", errors="replace"))
            fora.append({"arquivo": p.name, "bytes": p.stat().st_size, "linhas": linhas})
        except OSError:
            continue
    return fora


def media_por_linha(amostra: int = 200) -> float:
    """Bytes médios por linha (para dimensionar teto/TTL). 0 se não há amostra."""
    for p in sorted(DIR.glob("*.jsonl"), reverse=True):
        try:
            with open(p, "rb") as f:
                f.seek(max(0, p.stat().st_size - 20000))
                dados = f.read().splitlines()[-amostra:]
        except OSError:
            continue
        if dados:
            return round(sum(len(d) + 1 for d in dados) / len(dados), 1)
    return 0.0


def estatisticas() -> dict:
    return {"dir": str(DIR), "ttl_dias": TTL_PADRAO_DIAS,
            "arquivos": len(list(DIR.glob("*.jsonl"))) if DIR.exists() else 0,
            "bytes": sum(p.stat().st_size for p in DIR.glob("*.jsonl")) if DIR.exists() else 0}


def apagar_tudo() -> int:
    saiu = 0
    for p in DIR.glob("*.jsonl"):
        try:
            p.unlink()
            saiu += 1
        except OSError:
            continue
    return saiu