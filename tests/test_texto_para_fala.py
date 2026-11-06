"""#871 — texto do LLM limpo ANTES do TTS (Live em streaming e Conversa).

Tabela entrada → saída da `texto_para_fala`, o limpador de streaming com chunk
partido (inclusive `**` e `[link](url)` cortados no meio), a fiação no
`LivePipeline` (TTS recebe limpo, UI recebe o markdown) e o contrato
`reply_fala`/`text_fala` da Conversa, com a instrução do dono preservada (#799).
"""
from __future__ import annotations

import random
import pytest

from common import REGRA_FALA, LimpadorFala, texto_para_fala

TABELA = [
    # ênfase
    ("**Olá!** Tudo bem?", "Olá! Tudo bem?"),
    ("Isso é *muito* __bom__ e _raro_.", "Isso é muito bom e raro."),
    ("~~riscado~~ ficou", "riscado ficou."),
    # cabeçalho e citação
    ("# Título\nTexto normal.", "Título. Texto normal."),
    ("### Passo dois: abrir", "Passo dois: abrir."),
    ("> citação aqui", "citação aqui."),
    # listas viram frases
    ("Opções:\n- maçã\n- banana\n1. primeiro item\n2) segundo!",
     "Opções: maçã. banana. primeiro item. segundo!"),
    ("* um\n+ dois\n• três", "um. dois. três."),
    # links e URLs
    ("Veja [a doc](https://x.com/a_b) e https://exemplo.com/p?q=1.",
     "Veja a doc e link."),
    ("![logo](img.png) acima", "logo acima."),
    ("site www.exemplo.com.br, ok", "site link, ok."),
    # URL com parênteses e colchete que não é ](url) (P3 do gate)
    ("Veja [a doc](https://en.wikipedia.org/wiki/Sao_Paulo_(city)) agora.",
     "Veja a doc agora."),
    ('Veja [wiki](https://ex.com/Foo_(bar) "t") hoje.', "Veja wiki hoje."),
    ("![logo](https://ex.com/a_(b).png) acima", "logo acima."),
    ("Link nu https://en.wikipedia.org/wiki/Sao_Paulo_(city) fim.",
     "Link nu link fim."),
    ("Veja [nota] no fim.", "Veja nota no fim."),
    ("Use [este texto] sem url.", "Use este texto sem url."),
    ("array[i] vale 3.", "array i vale 3."),
    ("[só colchete]", "só colchete."),
    # código
    ("Código:\n```python\nprint('oi')\n```\nFim.", "Código: Código omitido. Fim."),
    ("use `npm install` agora", "use npm install agora."),
    # tabela e régua
    ("| Nome | Idade |\n|---|---|\n| Ana | 30 |", "Nome, Idade. Ana, 30."),
    ("---\nParágrafo", "Parágrafo."),
    # emoji
    ("Legal 😀👍🏽 demais ✅", "Legal demais."),
    # preserva números, pontuação, acentos, snake_case
    ("Custa R$ 1.234,56, ou 15% — às 14h30.", "Custa R$ 1.234,56, ou 15% — às 14h30."),
    ("2026 foi bom. 1.5 milhão", "2026 foi bom. 1.5 milhão."),
    ("snake_case continua", "snake_case continua."),
    ("Ação, coração e pão?", "Ação, coração e pão?"),
    ("", ""),
]


@pytest.mark.parametrize("entrada,saida", TABELA)
def test_tabela_entrada_saida(entrada, saida):
    assert texto_para_fala(entrada) == saida


def _stream(texto: str, cortes: list[int]) -> str:
    lim, saida, ini = LimpadorFala(), [], 0
    for c in cortes + [len(texto)]:
        saida.append(lim.push(texto[ini:c]))
        ini = c
    saida.append(lim.flush())
    return "".join(saida)


def _norm(s: str) -> str:
    # o streaming mantém as quebras de linha (o chunker do Live corta nelas);
    # a versão inteira junta as linhas em frases — compara só as palavras
    return " ".join(s.replace(".", " ").split())


def test_negrito_partido_entre_chunks_nao_vaza():
    # `**` dividido em `*` + `*` e o fechamento em outro delta
    saida = _stream("Ele é **muito importante** mesmo.", [7, 8, 20, 25, 26])
    assert "*" not in saida
    assert _norm(saida) == _norm("Ele é muito importante mesmo.")


def test_link_partido_entre_chunks_nao_vaza_url():
    texto = "Leia [o guia](https://ex.com/guia_1) hoje."
    saida = _stream(texto, [6, 10, 14, 20, 35])
    assert "http" not in saida and "[" not in saida and "](" not in saida
    assert _norm(saida) == _norm("Leia o guia hoje.")


def test_marcador_de_lista_partido_no_inicio_da_linha():
    saida = _stream("Itens:\n1", [])
    assert _norm(saida) == "Itens: 1"           # "1" sozinho não é marcador
    saida = _stream("Itens:\n1. arroz\n2. feijão", [8, 9, 10, 17, 18])
    assert _norm(saida) == "Itens: arroz feijão"


def test_cerca_de_codigo_partida():
    saida = _stream("Veja:\n```js\nx = 1\n```\nPronto.", [7, 8, 14, 17, 20])
    assert "x = 1" not in saida and "`" not in saida
    assert "Código omitido" in saida and "Pronto" in saida


def test_codigo_sem_fechar_nao_vaza_no_flush():
    saida = _stream("A seguir:\n```\nrm -rf /tmp/x", [12])
    assert "rm" not in saida


@pytest.mark.parametrize("entrada,_", TABELA)
def test_streaming_qualquer_corte_da_o_mesmo_texto(entrada, _):
    """Propriedade: com cortes aleatórios (inclusive caractere a caractere) o
    streaming fala as MESMAS palavras que a limpeza do texto inteiro."""
    esperado = _norm(texto_para_fala(entrada))
    assert _norm(_stream(entrada, list(range(1, len(entrada))))) == esperado
    rnd = random.Random(871)
    for _i in range(20):
        cortes = sorted(rnd.sample(range(1, max(2, len(entrada))),
                                   k=min(len(entrada) - 1, rnd.randint(0, 6)))) \
            if len(entrada) > 1 else []
        assert _norm(_stream(entrada, cortes)) == esperado, cortes


def test_palavra_gigante_sem_espaco_nao_trava():
    lim = LimpadorFala()
    assert lim.push("a" * 500) != ""            # passou do teto: sai


def test_colchete_sem_url_nao_e_falado():
    """`[` sem `](url)` não trava a linha nem sai no flush."""
    texto = "Veja [nota] no fim da frase e array[i] vale 3."
    assert texto_para_fala(texto) == "Veja nota no fim da frase e array i vale 3."
    saida = _stream(texto, list(range(1, len(texto))))
    assert "[" not in saida and "]" not in saida
    assert _norm(saida) == _norm("Veja nota no fim da frase e array i vale 3.")
    lim = LimpadorFala()
    antes = lim.push("Frase antes ")
    meio = lim.push("[abre e continua muito texto sem fechar o link de jeito nenhum ")
    fim = lim.flush()
    junto = antes + meio + fim
    assert "[" not in junto and "]" not in junto
    assert "abre e continua" in junto


def test_lista_longa_nao_deixa_espaco_antes_do_ponto():
    """Item longo emitido antes do \\n: o ponto cola na palavra, sem `fim .`."""
    for item in (
        "- " + ("palavra " * 40) + "fim \n",
        "- " + ("palavra " * 40) + "fim  \n",
        "- " + ("palavra " * 40) + "fim\n",
        "# " + ("palavra " * 20) + "fim \n",
    ):
        cheio = texto_para_fala(item)
        assert cheio.endswith("fim.")
        assert " ." not in cheio
        for passo in (1, 3, 5):
            saida = _stream(item, list(range(passo, len(item), passo)))
            assert " ." not in saida, (passo, saida)
            assert saida.rstrip().endswith("fim.")
            assert _norm(saida) == _norm(cheio)


def test_link_com_parenteses_nao_vaza():
    texto = "Veja [a doc](https://en.wikipedia.org/wiki/Sao_Paulo_(city)) agora."
    assert texto_para_fala(texto) == "Veja a doc agora."
    saida = _stream(texto, list(range(1, len(texto))))
    assert "http" not in saida and "[" not in saida and "(city)" not in saida
    assert _norm(saida) == "Veja a doc agora"
    meio = _stream(texto, [10, 20, 40, 55])
    assert "http" not in meio and "[" not in meio and "(city)" not in meio
    assert _norm(meio) == "Veja a doc agora"
    nua = "Link nu https://en.wikipedia.org/wiki/Sao_Paulo_(city). Fim"
    assert texto_para_fala(nua) == "Link nu link. Fim."
    assert "(city)" not in _stream(nua, list(range(1, len(nua))))


# ── fiação no Live ─────────────────────────────────────────────────────────
def test_live_tts_recebe_limpo_e_ui_recebe_markdown():
    import live_pipeline as lp

    eventos, falado, msgs_llm = [], [], []

    def llm(msgs):
        msgs_llm.append(msgs)
        for d in ["**Cla", "ro!** Veja ", "[o site](https://a.b/c) ", "😀 agora.\n- um\n- dois"]:
            yield d

    def tts(texto, omni):
        falado.append(texto)
        import numpy as np
        return np.zeros(240, dtype=np.float32)

    p = lp.LivePipeline(eventos.append, lambda b: None,
                        stt=lambda pcm, lang: "oi", llm=llm, tts=tts,
                        prewarm=lambda **_: None,
                        system="Instrução do dono: fale como pirata.")
    p.push_pcm(b"\x00\x00" * 1600)
    p.end_of_speech(inicia_thread=False)
    assert not [e for e in eventos if e.get("type") == "error"], eventos
    junto = " ".join(falado)
    assert "*" not in junto and "http" not in junto and "😀" not in junto and "-" not in junto
    assert _norm(junto) == _norm("Claro! Veja o site agora. um. dois.")
    ui = "".join(e["delta"] for e in eventos if e.get("type") == "assistant_text")
    assert "**Claro!**" in ui and "https://a.b/c" in ui   # UI mantém o markdown
    sistema = msgs_llm[0][0]
    assert sistema["role"] == "system" and REGRA_FALA in sistema["content"]
    # #799: a instrução do dono segue inteira e DEPOIS da regra
    assert sistema["content"].endswith("Instrução do dono: fale como pirata.")
    assert sum(1 for m in msgs_llm[0] if m["role"] == "system") == 1


def test_live_persona_dsh_carrega_regra_de_fala():
    import live_pipeline as lp
    assert REGRA_FALA in lp._DSH_PERSONA


# Conversa (`reply_fala`/`text_fala`, #799 preservado): tests/test_api.py
