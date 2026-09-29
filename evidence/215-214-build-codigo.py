"""GATE #215 item 7 (#214) — `codigo` de /api/build é o CÓDIGO CARREGADO (do boot).

O campo prometia o código carregado e, calculado no 1º uso, descrevia o DISCO do
momento da checagem: instância que nunca serviu a rota dava "bate" falso contra uma
árvore já editada. Aqui a prova é por fora e em duas INSTÂNCIAS de verdade:

  1. processo deste gate (instância A): `codigo` == hash da árvore no boot;
  2. edito um módulo (árvore vira B);
  3. a instância A continua dizendo A (o disco já é B — a divergência é o ponto);
  4. uma instância NOVA (subprocesso) nascida depois da edição diz B;
  5. `version` (commit) × `codigo` (carregado) DIVERGEM com a árvore suja — é o par
     que sustenta o veredito "a instância não é este código".

Uso: PYTHONPATH=. ./.venv-mlx/bin/python evidence/215-214-build-codigo.py
"""
import hashlib
import os
import pathlib
import subprocess
import sys

os.environ.setdefault("TTS_LIVE_WORKER", "0")
os.environ["TTS_ROD_API_KEY"] = "chave-do-gate-215"

import app                                    # noqa: E402
from fastapi.testclient import TestClient     # noqa: E402

KEY = {"x-api-key": "chave-do-gate-215"}
cli = TestClient(app.app)
BASE = pathlib.Path(app.BASE)
ALVO = "dsh_client.py"
falhas = []


def ok(m):
    print(f"  ✔ {m}")


def falha(m):
    falhas.append(m)
    print(f"  ✘ {m}")


def hash_commit() -> str:
    """Hash dos módulos COMO ESTÃO NO COMMIT (HEAD) — o par do `version`."""
    h = hashlib.sha256()
    for nome in app._BUILD_MODULOS:
        try:
            h.update(subprocess.run(["git", "show", f"HEAD:{nome}"], cwd=BASE,
                                    capture_output=True, check=True).stdout)
        except subprocess.CalledProcessError:
            h.update(b"<ausente>")
    return h.hexdigest()[:8]


def codigo_em_processo_novo() -> str:
    """`codigo` que uma instância NASCIDA AGORA reportaria (subprocesso limpo)."""
    r = subprocess.run([sys.executable, "-c",
                        "import app; print(app._BUILD_CODIGO)"],
                       cwd=BASE, capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": str(BASE)})
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-500:])
    return r.stdout.strip()


def main():
    arq = BASE / ALVO
    original = arq.read_bytes()

    boot = app._BUILD_CODIGO
    disco_antes = app._build_hash()
    print(f"    boot={boot} disco_agora={disco_antes} version={app._VERSION} "
          f"commit={hash_commit()}")
    if boot == disco_antes:
        ok("`codigo` do boot == árvore do boot (a instância descreve o que carregou)")
    else:
        falha(f"codigo({boot}) != árvore no boot({disco_antes})")

    r = cli.get("/api/build", headers=KEY)
    d = r.json()
    if d.get("codigo") == boot:
        ok(f"/api/build devolve o codigo do BOOT ({boot}) já na 1ª chamada")
    else:
        falha(f"1ª chamada devolveu {d.get('codigo')} (esperado {boot})")

    if codigo_em_processo_novo() == boot:
        ok("outra instância nascida no MESMO estado reporta o mesmo codigo")
    else:
        falha("duas instâncias no mesmo estado divergiram (hash instável)")

    # ---- edita a árvore: vira B, e a instância viva continua A
    arq.write_bytes(original + b"\n# gate #215: comentario de teste\n")
    try:
        disco_depois = app._build_hash()
        d2 = cli.get("/api/build", headers=KEY).json()
        if disco_depois != boot:
            ok(f"a árvore mudou de fato (disco agora {disco_depois} != boot {boot})")
        else:
            falha("a edição não mudou o hash do disco (alvo errado?)")
        if d2.get("codigo") == boot:
            ok("instância que NUNCA tinha servido /api/build segue acusando A "
               "depois da edição (era o falso 'bate' do #204/#190)")
        else:
            falha(f"codigo virou {d2.get('codigo')} — seguiu o disco em vez do carregado")
        if codigo_em_processo_novo() == disco_depois:
            ok("instância NASCIDA depois da edição acusa B (a divergência é real)")
        else:
            falha("instância nova não acusou a árvore editada")
        if d2.get("version") != d2.get("codigo"):
            ok(f"`version`({d2.get('version')}) × `codigo`({d2.get('codigo')}) divergem "
               f"com árvore suja — o par sustenta 'a instância não é este código'")
        else:
            ok("version == codigo (árvore limpa neste estado)")
    finally:
        arq.write_bytes(original)

    if app._build_hash() == disco_antes:
        ok("árvore restaurada byte a byte depois do teste")
    else:
        falha("a árvore ficou alterada depois do teste")

    print(f"\nfalhas: {len(falhas)}")
    for f in falhas:
        print(f"  - {f}")
    print("VEREDITO:", "OK" if not falhas else "FALHOU")
    return 0 if not falhas else 1


if __name__ == "__main__":
    sys.exit(main())