"""#1158 — fala curta demais para a biometria não é barrada na TRANSCRIÇÃO.

Relato do dono: 1 s de ditado com `speaker_gate=enforce` voltou "rejeitado pelo
anti-ruído: áudio curto p/ identificar a voz". A biometria não pode barrar o que
não consegue avaliar. Nas rotas que só transcrevem, o curto passa com o aviso
"voz não verificada". Continua barrado:
- voz de outra pessoa com amostra suficiente;
- extrator que falhou com áudio de tamanho bom (falha fechado);
- clone marcado;
- fala→voz (`/api/modify-speech`, `/api/translate-speech`).

O Live (`_voz_do_dono`) não passa pelo gate de arquivo.

Os wavs são reais (recortes de `fixtures/voz_fala_5s_16k.wav`), porque o "curto"
é decidido pela duração decodificada. Só o extrator é dublê: o ECAPA real não
carrega na suíte.
"""
import inspect
import json
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest
import soundfile as sf

from fastapi.testclient import TestClient

import app

FIXTURE = Path(__file__).parent / "fixtures" / "voz_fala_5s_16k.wav"
PERFIS = {"dono": {"vecs": [[1.0, 0.0]], "modelo": "ecapa:v1"}}


def _recorte(tmp_path: Path, seg: float) -> Path:
    a, sr = sf.read(str(FIXTURE), dtype="float32")
    assert sr == 16000
    p = tmp_path / f"fala-{seg}s.wav"
    sf.write(str(p), a[: int(sr * seg)], sr)
    return p


@pytest.fixture()
def client():
    return TestClient(app.app, raise_server_exceptions=False)


@pytest.fixture()
def auth():
    key = app._primary_api_key()
    return {"X-API-Key": key} if key else {}


@pytest.fixture()
def gate(monkeypatch, tmp_path):
    """Gate em `enforce` com um perfil ECAPA e upload que grava um wav real.

    O extrator dublê imita o piso real: sem embedding abaixo de `_SPEAKER_PISO_S`.
    Com embedding, devolve a voz do dono (`voz["sim"]`) ou outra voz."""
    monkeypatch.setitem(app._settings, "speaker_gate", "enforce")
    monkeypatch.setitem(app._settings, "speaker_anticlone", False)
    monkeypatch.setattr(app, "_speaker_load_estado", lambda: (PERFIS, ""))
    monkeypatch.setattr(app, "_speaker_modelo_efetivo", lambda: "ecapa:v1")
    monkeypatch.setattr(app, "_speaker_limiar", lambda m: (0.44, "cru"))
    voz = {"seg": 1.0, "sim": 0.9, "falha": False}

    def _pontua(wav, perfis):
        if voz["falha"] or app._wav_to_mono16k(wav).size < 16000 * app._SPEAKER_PISO_S:
            return None, None, False, "cru"
        return "dono", voz["sim"], True, "cru"

    monkeypatch.setattr(app, "_speaker_pontua", _pontua)
    criados = []

    def _grava(upload, prefix=".stt"):
        p = _recorte(tmp_path, voz["seg"])
        destino = tmp_path / f"up-{len(criados)}.wav"
        p.rename(destino)
        criados.append(destino)
        return destino

    monkeypatch.setattr(app, "_save_audio_upload", _grava)
    monkeypatch.setattr(app, "_transcribe", lambda p, language=None: {
        "text": "pode mandar", "language": "pt",
        "segments": [{"start": 0.0, "end": 0.5, "text": "pode mandar"}],
        "no_speech_prob": 0.0, "avg_logprob": -0.1, "compression_ratio": 1.0})
    monkeypatch.setattr(app, "_stt_ok", lambda r, t: (True, ""))
    voz["criados"] = criados
    return voz


def _post(client, auth, rota, campo="audio", dados=None):
    return client.post(rota, headers=auth, data=dados or {},
                       files={campo: ("a.wav", b"x", "audio/wav")})


# --- célula do card: curto passa com aviso -----------------------------------

def test_transcribe_curto_passa_com_aviso(client, auth, gate):
    gate["seg"] = 0.5
    r = _post(client, auth, "/api/transcribe")
    corpo = r.json()
    assert r.status_code == 200, r.text
    assert corpo.get("rejected") is not True, corpo
    assert corpo["text"] == "pode mandar"
    assert corpo["speaker_verificado"] is False
    assert corpo["speaker_aviso"] == app._SPEAKER_AVISO_NAO_VERIFICADA
    assert "não verificada" in corpo["speaker_aviso"]
    assert gate["criados"] and not gate["criados"][-1].exists()


# --- célula do card: outra voz com amostra suficiente segue barrada -------------

def test_transcribe_outra_voz_4s_rejeitada_como_hoje(client, auth, gate):
    gate["seg"], gate["sim"] = 4.0, 0.10
    r = _post(client, auth, "/api/transcribe")
    corpo = r.json()
    assert corpo["rejected"] is True and corpo["text"] == ""
    assert corpo["reason"].startswith("voz não autorizada"), corpo


def test_transcribe_dono_4s_passa_sem_aviso(client, auth, gate):
    gate["seg"] = 4.0
    corpo = _post(client, auth, "/api/transcribe").json()
    assert corpo["text"] == "pode mandar"
    assert "speaker_aviso" not in corpo and "speaker_verificado" not in corpo


def test_extrator_falhou_com_audio_bom_falha_fechado(client, auth, gate):
    """Acima do piso o áudio é bom: sem embedding aí é FALHA, não é curto —
    segue barrado, com o motivo real em vez de "áudio curto"."""
    gate["seg"], gate["falha"] = 1.5, True
    corpo = _post(client, auth, "/api/transcribe").json()
    assert corpo["rejected"] is True, corpo
    assert "falhou" in corpo["reason"] and "curto" not in corpo["reason"]


def test_audio_ilegivel_falha_fechado(gate, tmp_path, monkeypatch):
    monkeypatch.setattr(app, "_speaker_pontua",
                        lambda wav, perfis: (None, None, False, "cru"))
    lixo = tmp_path / "lixo.wav"
    lixo.write_bytes(b"RIFF")
    out = app._speaker_gate_stt(lixo)
    assert out["rejected"] is True and "falhou" in out["reason"]


def test_curto_com_clone_marcado_segue_barrado(client, auth, gate, monkeypatch):
    gate["seg"] = 0.5
    monkeypatch.setitem(app._settings, "speaker_anticlone", True)
    marca = types.ModuleType("marca_dagua")
    marca.prewarm = lambda: None
    monkeypatch.setitem(sys.modules, "marca_dagua", marca)
    monkeypatch.setattr(app, "_speaker_anticlone",
                        lambda _w: {"anticlone": "marcado", "anticlone_prob": 0.93})
    corpo = _post(client, auth, "/api/transcribe").json()
    assert corpo["rejected"] is True and "clone" in corpo["reason"], corpo


def test_curto_com_anticlone_cru_passa(client, auth, gate, monkeypatch):
    gate["seg"] = 0.5
    monkeypatch.setitem(app._settings, "speaker_anticlone", True)
    marca = types.ModuleType("marca_dagua")
    marca.prewarm = lambda: None
    monkeypatch.setitem(sys.modules, "marca_dagua", marca)
    monkeypatch.setattr(app, "_speaker_anticlone",
                        lambda _w: {"anticlone": "cru", "anticlone_prob": 0.02})
    corpo = _post(client, auth, "/api/transcribe").json()
    assert corpo["text"] == "pode mandar" and corpo["speaker_verificado"] is False


# --- as outras rotas que só transcrevem ---------------------------------------

def test_stt_partial_curto_passa_com_aviso(client, auth, gate):
    gate["seg"] = 0.5
    corpo = _post(client, auth, "/api/stt-partial").json()
    assert corpo["text"] == "pode mandar" and "rejected" not in corpo
    assert corpo["speaker_aviso"] == app._SPEAKER_AVISO_NAO_VERIFICADA


@pytest.mark.parametrize("fmt", ["json", "verbose_json", "text", "srt", "vtt"])
def test_v1_curto_transcreve_e_marca_nao_verificado(client, auth, gate, fmt):
    gate["seg"] = 0.5
    r = _post(client, auth, "/v1/audio/transcriptions", "file",
              {"response_format": fmt})
    assert r.status_code == 200, r.text
    assert "x-tts-speaker-rejected" not in r.headers
    assert r.headers.get("x-tts-speaker-verificado") == "0"
    assert "pode mandar" in r.text
    if fmt == "verbose_json":
        corpo = r.json()
        assert corpo["speaker_verificado"] is False
        assert corpo["speaker_aviso"] == app._SPEAKER_AVISO_NAO_VERIFICADA


def test_v1_outra_voz_segue_barrada(client, auth, gate):
    gate["seg"], gate["sim"] = 4.0, 0.10
    r = _post(client, auth, "/v1/audio/transcriptions", "file")
    assert r.headers.get("x-tts-speaker-rejected") == "1"
    assert r.json() == {"text": ""}


# --- fala→voz continua barrando o curto ----------------------------------------

@pytest.mark.parametrize("rota", ["/api/modify-speech", "/api/translate-speech"])
def test_fala_para_voz_curto_segue_barrado(client, auth, gate, monkeypatch, rota):
    """Fala→voz sai na voz clonada: aí o curto continua barrado."""
    gate["seg"] = 0.5
    monkeypatch.setattr(app, "_run_tts_job", lambda *a, **k: None)
    r = _post(client, auth, rota, dados={"voice_id": "vd-narrador",
                                         "target_lang": "en"})
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert corpo["rejected"] is True and "curto" in corpo["reason"], corpo
    assert "curto" not in corpo                    # o marcador interno não vaza


# --- gate direto: os modos e o legado ------------------------------------------

def test_label_curto_mantem_o_aviso_de_antes(gate, tmp_path, monkeypatch):
    monkeypatch.setitem(app._settings, "speaker_gate", "label")
    out = app._speaker_gate_stt(_recorte(tmp_path, 0.5))
    assert out == {"speaker_aviso": "áudio curto p/ identificar a voz — segue sem "
                                    "etiqueta de locutor"}


def test_gate_estrito_segue_rejeitando_o_curto(gate, tmp_path):
    out = app._speaker_gate_ok(_recorte(tmp_path, 0.5))
    assert out["rejected"] is True and out["curto"] is True
    assert out["reason"] == "áudio curto p/ identificar a voz"


def test_legado_mede_depois_do_corte_de_silencio(gate, tmp_path, monkeypatch):
    """O Resemblyzer corta o silêncio antes do piso: 1 s gravado com 0,4 s de fala
    é curto para ele, não é falha."""
    monkeypatch.setattr(app, "_speaker_modelo_efetivo", lambda: app.SPEAKER_MODELO_LEGADO)
    resem = types.ModuleType("resemblyzer")
    resem.preprocess_wav = lambda p: [0.0] * int(16000 * 0.4)
    monkeypatch.setitem(sys.modules, "resemblyzer", resem)
    monkeypatch.setattr(app, "_speaker_pontua",
                        lambda wav, perfis: (None, None, False, "cru"))
    out = app._speaker_gate_stt(_recorte(tmp_path, 1.0))
    assert out.get("speaker_verificado") is False, out


# --- Live / barge: não muda ---------------------------------------------------

def test_live_nao_passa_pelo_gate_de_arquivo():
    """O "só a minha voz" do Live tem o caminho dele (`_voz_do_dono`, janelas de
    1 s e piso próprio). Nem ele nem o pipeline chamam o gate de arquivo."""
    import live_pipeline

    fonte_live = inspect.getsource(app._voz_do_dono) + inspect.getsource(live_pipeline)
    assert "_speaker_gate" not in fonte_live
    # #1167: o piso do Live é o MESMO `_SPEAKER_PISO_S`, não um 0,6 próprio
    assert "_SPEAKER_PISO_S" in inspect.getsource(app._voz_do_dono)


# --- setting `speaker_curto_aceita` (decisão do PM) -----------------------------

def test_setting_desligado_volta_a_rejeitar_o_curto(client, auth, gate, monkeypatch):
    gate["seg"] = 0.5
    monkeypatch.setitem(app._settings, "speaker_curto_aceita", False)
    for rota in ("/api/transcribe", "/api/stt-partial"):
        corpo = _post(client, auth, rota).json()
        assert corpo["rejected"] is True and corpo["text"] == "", (rota, corpo)
        assert corpo["reason"] == "áudio curto p/ identificar a voz"
    r = _post(client, auth, "/v1/audio/transcriptions", "file")
    assert r.headers.get("x-tts-speaker-rejected") == "1" and r.json() == {"text": ""}


def test_setting_ausente_vale_o_default_ligado(gate, tmp_path, monkeypatch):
    assert app._SETTINGS_DEFAULTS["speaker_curto_aceita"] is True
    monkeypatch.delitem(app._settings, "speaker_curto_aceita", raising=False)
    out = app._speaker_gate_stt(_recorte(tmp_path, 0.5))
    assert out.get("speaker_verificado") is False, out


@pytest.mark.parametrize("v,esperado", [(False, False), ("false", False), ("0", False),
                                        ("", False), (True, True), ("on", True)])
def test_setting_post_booleano_explicito(client, auth, monkeypatch, v, esperado):
    monkeypatch.setitem(app._settings, "speaker_curto_aceita", not esperado)
    r = client.post("/api/settings", headers=auth, json={"speaker_curto_aceita": v})
    assert r.status_code == 200, r.text
    assert app._settings["speaker_curto_aceita"] is esperado


def test_setting_e_administrativo():
    """Afrouxa o `enforce`: chave de uso não mexe (mesmo grupo do `speaker_gate`)."""
    assert "speaker_curto_aceita" in app._SETTINGS_ADMIN


# --- UI: a dica certa para quem barrou ------------------------------------------

HTML = (Path(__file__).parent.parent / "static" / "index.html").read_text()


def _js_rejeicao() -> str:
    ini = HTML.index("const MOTIVOS_BIOMETRIA")
    fim = HTML.index("async function transcreveMonitor")
    return HTML[ini:fim]


@pytest.mark.skipif(not shutil.which("node"), reason="sem node")
def test_ui_texto_da_rejeicao_por_origem():
    casos = {
        "áudio curto p/ identificar a voz": "fala curta demais para conferir a voz",
        "voz não autorizada (melhor: ana 0.20 < 0.44)": "rejeitado pela biometria: voz não autorizada",
        "biometria falhou ao extrair a voz (veja o log)": "rejeitado pela biometria",
        "áudio com marca d'água de TTS (clone) — detecção 0.9": "rejeitado pela biometria",
        "sem fala (no_speech 0.92)": "rejeitado pelo anti-ruído",
    }
    js = _js_rejeicao() + "\nconst casos = " + json.dumps(list(casos)) + ";\n" \
        "console.log(JSON.stringify(casos.map(textoRejeicaoStt)));"
    p = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    saidas = json.loads(p.stdout)
    for (motivo, esperado), saida in zip(casos.items(), saidas):
        assert esperado in saida, (motivo, saida)
        biometria = "anti-ruído" not in esperado
        assert ("Mín. confiança" in saida) is not biometria, (motivo, saida)
        assert ("Biometria de voz" in saida) is biometria, (motivo, saida)


def test_ui_monitor_usa_a_dica_e_mostra_o_aviso():
    ini = HTML.index("async function transcreveMonitor")
    corpo = HTML[ini:HTML.index("\n}\n", ini)]
    assert "textoRejeicaoStt(r.reason)" in corpo
    assert "r.speaker_aviso" in corpo
    assert "rejeitado pelo anti-ruído" not in corpo     # a dica fixa saiu daqui


def test_ui_checkbox_do_setting_carrega_e_salva():
    assert 'id="spCurtoAceita" data-admin-setting="speaker_curto_aceita"' in HTML
    assert '$("spCurtoAceita").checked = s.speaker_curto_aceita !== false' in HTML
    assert 'speaker_curto_aceita: $("spCurtoAceita")?.checked !== false' in HTML


# --- #1159: o piso agora é 1,0 s (medido) -------------------------------------

def test_piso_e_um_segundo_e_os_tres_sitios_concordam():
    """O piso é UM número só (#1159/#1167). Se alguém cravar 0,6 de novo num
    sítio, o gate de arquivo e o do Live divergem e o curto passa por um deles."""
    assert app._SPEAKER_PISO_S == 1.0
    fonte = inspect.getsource(app)
    assert "16000 * 0.6" not in fonte, "sobrou piso cravado em app.py"
    for fn in (app._speaker_embed, app._speaker_embed_ecapa_x, app._voz_do_dono):
        assert "_SPEAKER_PISO_S" in inspect.getsource(fn), fn.__name__


def test_um_segundo_verifica_e_meio_segundo_e_curto(gate, tmp_path):
    """Fronteira do piso: 1,0 s verifica (dá embedding), 0,5 s é curto."""
    curto = app._speaker_gate_stt(_recorte(tmp_path, 0.5))
    assert curto.get("speaker_verificado") is False, curto
    assert "curto" in curto["speaker_aviso"]
    ok = app._speaker_gate_stt(_recorte(tmp_path, 1.0))
    assert not ok.get("curto") and not ok.get("rejected"), ok
