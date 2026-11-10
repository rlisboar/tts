"""#1107: a UI nunca põe a chave de API em URL — o WS do Live entra por ticket.

Origem: no gate do #1104 o qa-review contou 80 linhas com `?key=` no log do mini
(`/tmp/tts-studio.log`): 44 handshakes `/api/live/ws?key=` e 36 avisos de fallback
do próprio servidor. Medido em 2026-11-10, nenhuma veio da UI. As 44 seguem o
padrão `/api/voices` → `/api/build` → WS da sonda de `remote/deploy_mini.sh`: 22
vindas da LAN e 22 do túnel. Esta guarda impede que a UI vire a próxima fonte.

Chave em query string vaza no access log do uvicorn, no log do proxy ou do túnel e
no histórico do navegador. O caminho do navegador é o ticket de um uso (#91/#98): `POST
/api/live/ticket` com a chave no HEADER e depois `?ticket=<t>` no WS.

Estas células são estáticas de propósito, porque cobram a fiação. Um Live de
ponta a ponta precisaria de modelo e minutos, e o que pode regredir é uma linha:
alguém "consertando" o ticket que falhou com `?key=` no lugar.

O caminho `?key=` do SERVIDOR continua para clientes externos (CLI, scripts); a
célula da compatibilidade fica em `test_api.py`. Aqui só se cobra o cliente da UI.
"""
import re
from pathlib import Path

INDEX = Path(__file__).resolve().parent.parent / "static" / "index.html"


def _codigo() -> str:
    """O index sem comentários: os comentários EXPLICAM o `?key=` proibido.

    `//` só conta como comentário no começo da linha ou depois de espaço, porque
    `"wss://"` é string, não comentário. Comentário de bloco e de HTML também saem.
    """
    fonte = INDEX.read_text()
    fonte = re.sub(r"<!--.*?-->", "", fonte, flags=re.S)
    fonte = re.sub(r"/\*.*?\*/", "", fonte, flags=re.S)
    return "\n".join(re.sub(r"(^|\s)//.*$", r"\1", linha) for linha in fonte.splitlines())


# Nomes de credencial em query: o que o servidor aceita (`key` no WS, `ticket`) e
# o que o mascaramento de log trata como segredo (`api_key`, `apikey`, `token`,
# `secret`, …). O `ticket=` tem UM lugar legítimo, o fluxo do ticket em
# `lxwsConectar`, e é contado à parte.
_NOMES = r"(?:key|api[_-]?key|x-api-key|token|access_token|secret|ticket)"
_TICKET_DO_FLUXO = '"?ticket=" + encodeURIComponent(t.ticket)'

# De onde a chave sai no navegador: `KEY`, o `keyRead()` que a carrega e o storage
# por trás dele. Cada um tem dono fixo: `keyRead()` só carrega `KEY` e o storage
# da chave só é lido dentro do `keyRead`. Outra leitura contorna a guarda.
_FONTE = r"(?:\bKEY\b|\bkeyRead\(\s*\))"


def test_nenhuma_string_da_ui_monta_credencial_em_query():
    codigo = _codigo()
    achados = re.findall(r"""["'`][^"'`\n]*[?&]""" + _NOMES + r"""=[^"'`\n]*["'`]""",
                         codigo, flags=re.I)
    assert achados == ['"?ticket="'], f"a UI monta credencial em query string: {achados}"
    assert codigo.count(_TICKET_DO_FLUXO) == 1, "o `?ticket=` saiu do fluxo do ticket"
    # as outras formas de montar a mesma URL
    hit = re.search(r"""\.(?:set|append)\(\s*["'`]""" + _NOMES + r"""["'`]""", codigo, flags=re.I)
    assert not hit, f"a UI põe credencial em URLSearchParams: {hit.group(0)!r}"
    hit = re.search(r"URLSearchParams\(\s*\{[^}]*\b" + _NOMES + r"\b", codigo, flags=re.I)
    assert not hit, f"a UI põe credencial em URLSearchParams: {hit.group(0)!r}"


def test_a_chave_so_sai_do_navegador_no_header():
    """`KEY`/`keyRead()` não entram em concatenação, template nem URLSearchParams."""
    codigo = _codigo()
    proibidos = [
        r"encodeURIComponent\(\s*" + _FONTE,
        r"\+\s*" + _FONTE + r"(?!\s*\.)",   # `"…" + KEY` (o `KEY.slice` da máscara é leitura)
        _FONTE + r"\s*\+",
        r"\$\{\s*" + _FONTE,
        r"\.(?:set|append)\([^,)]*,\s*" + _FONTE,
    ]
    for padrao in proibidos:
        hit = re.search(padrao, codigo)
        assert not hit, f"a chave vira texto de URL/concatenação: {hit.group(0)!r}"
    assert '"X-API-Key": KEY' in codigo, "a chave sumiu do header do `api()`"


def test_a_chave_so_e_lida_por_um_caminho():
    """`keyRead()` só carrega `KEY`; o storage da chave só é lido dentro do `keyRead`.

    Sem isto, `searchParams.set("x", keyRead())` ou um `localStorage.getItem` da
    chave levariam o segredo para a URL por fora de `KEY`.
    """
    codigo = _codigo()
    chamadas = re.findall(r"[^\n]*\bkeyRead\(\s*\)[^\n]*", codigo)
    assert [c.strip() for c in chamadas] == ["let KEY = keyRead();"], \
        f"`keyRead()` usado fora da carga de `KEY`: {chamadas}"
    leitor = codigo.split("const keyRead = () => {", 1)[1].split("};", 1)[0]
    fora = codigo.replace(leitor, "")
    hit = re.search(r"""\.getItem\(\s*["'`](?:ttsStudioKey|ttsRodKey)["'`]""", fora)
    assert not hit, f"a chave é lida do storage fora do `keyRead`: {hit.group(0)!r}"


def test_ws_do_live_abre_por_ticket_e_unico():
    codigo = _codigo()
    aberturas = re.findall(r"new WebSocket\(([^;]*)\)", codigo)
    assert aberturas == ['lxWsUrl() + q, "tts-live.v1"'], \
        f"abertura de WS fora do contrato do ticket: {aberturas}"
    corpo_url = codigo.split("function lxWsUrl()", 1)[1].split("}", 1)[0]
    assert "key" not in corpo_url.lower(), f"lxWsUrl carrega credencial: {corpo_url!r}"
    conectar = codigo.split("async function lxwsConectar(", 1)[1].split("new WebSocket(", 1)[0]
    assert 'api("/api/live/ticket", { method: "POST" })' in conectar, \
        "o ticket não é pedido pelo `api()` (header) antes do WS"
    atribuicoes = re.findall(r"\bq\s*=\s*([^;]+);", conectar)
    assert atribuicoes == ['""', '"?ticket=" + encodeURIComponent(t.ticket)'], \
        f"`q` ganhou outro valor além do ticket: {atribuicoes}"


def test_falha_do_ticket_vira_erro_sem_degradar_para_a_chave():
    """Sem ticket a UI mostra erro e não abre o WS. Não há segunda via."""
    conectar = _codigo().split("async function lxwsConectar(", 1)[1].split("new WebSocket(", 1)[0]
    pega = conectar.split("} catch (e) {", 1)[1].split("}", 1)[0]
    assert 'lxEstado("erro"' in pega and "return;" in pega, \
        f"a falha do ticket não termina em erro visível: {pega!r}"
    assert "q =" not in pega and "q=" not in pega, \
        f"o catch do ticket monta outra URL: {pega!r}"
