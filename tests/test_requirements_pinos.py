"""`requirements.txt`: a política de pinos é COBRADA, não só comentada (#392).

POR QUE ESTE ARQUIVO EXISTE: a regra do repo ("nada de runtime sem pino; mlx-* e
transformers só sobem com síntese real") morava só no comentário do próprio
`requirements.txt` — e bump silencioso de runtime já custou os incidentes #7/#29.
Um comentário não quebra build; um teste quebra.

A REGRA, e por que ela não é "todo mundo com `==`":
  • uma dependência SEM `==` só passa se **estiver pinada em `requirements.lock`**
    — o lock é o teto versionado, e é ele que o `-c requirements.lock` do deploy
    (#343) e o CI usam. Dep nova sem pino: rode `pip freeze` no venv verificado e
    atualize o lock — o teste cobra as duas coisas juntas;
  • ALLOWLIST para os casos em que o pino exato é pior que o teto do lock (cada
    entrada traz o motivo, e o teste falha se uma entrada virar órfã).
"""

import re
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
REQ = BASE / "requirements.txt"
LOCK = BASE / "requirements.lock"

# nome -> motivo. Entrada aqui dispensa o `==`, NUNCA o teto: quem casa com o lock
# continua sendo tetado por ele.
ALLOWLIST = {
    "yt-dlp": "tem piso `>=` de propósito (YouTube 403 em versão antiga): `==` exigiria bump semanal; o teto vem do lock",
    "torch": "no CI vem do índice CPU (`--index-url .../whl/cpu`); pino exato colidiria com o wheel do índice — o teto vem do lock",
    "pytest": "ferramenta de teste, não runtime: acompanhar correção vale mais que congelar",
    "pytest-timeout": "idem (ferramenta do gate de CI)",
    "pytest-rerunfailures": "idem (ferramenta do gate de CI)",
    "pytest-cov": "idem (cobertura, só diagnóstico)",
    "ruff": "linter: queremos as regras novas; o teto vem do lock",
}

LINHA_DEP = re.compile(r"^(?P<nome>[A-Za-z0-9][A-Za-z0-9._-]*)(?P<extra>\[[^\]]*\])?(?P<resto>.*)$")
PINADA_NO_LOCK = re.compile(r"^(?P<nome>[A-Za-z0-9][A-Za-z0-9._-]*)==[^\s;]+$")


def _deps() -> list[tuple[int, str, str]]:
    """(linha, nome, linha_inteira) das dependências — comentários e vazios fora."""
    fora = []
    for n, bruta in enumerate(REQ.read_text().splitlines(), 1):
        linha = bruta.strip()
        if not linha or linha.startswith("#"):
            continue
        m = LINHA_DEP.match(linha)
        if m:
            fora.append((n, m.group("nome"), linha))
    return fora


def _pinadas_no_lock() -> set[str]:
    nomes = set()
    for bruta in LOCK.read_text().splitlines():
        m = PINADA_NO_LOCK.match(bruta.strip())
        if m:
            nomes.add(m.group("nome").lower())
    return nomes


def test_toda_dependencia_tem_teto_ou_esta_na_allowlist():
    lock = _pinadas_no_lock()
    problemas = []
    for linha, nome, bruta in _deps():
        if "==" in bruta:
            continue
        if nome in ALLOWLIST:
            continue
        if nome.lower() in lock:
            continue
        problemas.append(
            f"requirements.txt:{linha}  `{nome}` sem `==` e fora do requirements.lock "
            f"(pine, ou atualize o lock com `pip freeze` no venv verificado, ou justifique na ALLOWLIST)")
    assert not problemas, "dependência sem teto:\n  " + "\n  ".join(problemas)


def test_allowlist_nunca_fica_orfa():
    """Entrada que sobrou vira mentira: o motivo diz de algo que não existe mais."""
    nomes = {nome for _, nome, _ in _deps()}
    orfas = sorted(set(ALLOWLIST) - nomes)
    assert not orfas, f"ALLOWLIST com entrada que não está no requirements.txt: {orfas}"


def test_allowlist_entra_no_lock_ou_e_ferramenta():
    """Toda entrada da allowlist precisa de motivo E de estar no lock (a menos que
    seja ferramenta) — senão o teto do lock não a cobre e o `==` volta a fazer falta."""
    lock = _pinadas_no_lock()
    ferramentas = {"pytest", "pytest-timeout", "pytest-rerunfailures", "pytest-cov", "ruff"}
    sem_teto = [n for n in ALLOWLIST if n not in ferramentas and n.lower() not in lock]
    assert not sem_teto, f"allowlist sem teto no lock e sem ser ferramenta: {sem_teto}"
    assert all(m.strip() for m in ALLOWLIST.values()), "entrada de allowlist sem motivo escrito"


def test_lock_tem_as_dependencias_de_runtime():
    """O lock é o teto: se ele parar de cobrir o runtime, o `-c` do deploy emudece."""
    lock = _pinadas_no_lock()
    faltando = [n for _, n, _ in _deps()
                if n not in ALLOWLIST and n.lower() not in lock]
    assert not faltando, f"fora do lock: {faltando}"


def test_o_teste_se_morde(monkeypatch, tmp_path):
    """Contraprova: uma dependência nova sem pino tem de derrubar o teste."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("pinos", BASE / "tests" / "test_requirements_pinos.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    req = tmp_path / "requirements.txt"
    req.write_text("fastapi\npacote-novo-sem-pino\n")
    monkeypatch.setattr(mod, "REQ", req)
    with __import__("pytest").raises(AssertionError) as e:
        mod.test_toda_dependencia_tem_teto_ou_esta_na_allowlist()
    assert "pacote-novo-sem-pino" in str(e.value)
