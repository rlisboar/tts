"""#872: a Conversa FALA a versão limpa e MOSTRA a crua.

O servidor (#871) passou a devolver `reply_fala`/`text_fala` ao lado de
`reply`/`text`: o mesmo texto sem markdown, emoji, URL e código. O TTS tem de
receber o `*_fala`; a tela continua com o cru. O `??` cobre servidor antigo.

Célula estática de propósito: exercitar a Conversa de ponta a ponta custaria
modelo e alguns minutos por rodada, e o que se cobra aqui é a FIAÇÃO das duas
pontas — o conteúdo do saneamento é do `test_texto_para_fala.py`.
"""
from pathlib import Path

INDEX = Path(__file__).resolve().parent.parent / "static" / "index.html"


def test_cvspeak_usa_a_versao_de_fala():
    fonte = INDEX.read_text()
    assert "cvSpeak(d.text_fala ?? d.text" in fonte, \
        "o confirmado da Conversa voltou a mandar o texto cru ao TTS"
    assert "cvSpeak(d.reply_fala ?? d.reply" in fonte, \
        "a resposta da Conversa voltou a mandar o texto cru ao TTS"


def test_tela_continua_mostrando_o_texto_cru():
    fonte = INDEX.read_text()
    assert 'cvAdd("IA", "✅ " + d.text)' in fonte, "o balão do confirmado perdeu o texto cru"
    assert "cvAddIA(d.reply)" in fonte, "o balão da IA perdeu o texto cru"
