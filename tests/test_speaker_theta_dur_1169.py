"""#1169 — o limiar do ECAPA varia com a DURAÇÃO, com margem.

Um θ só para a curva inteira dava 1,55 % de FAR aos 3 s e 0,02 % aos 0,3 s: alto
demais para o longo, baixo demais para o curto. O θ agora sai de uma tabela por
duração, derivada no quantil de 0,9 % do corpus mais duro (o ponto fica em 0,9 % e
o IC95 superior em ~1 %, que é a letra do pré-registro).

Estas células existem para a interpolação não virar decoração: sem elas, trocar o
`return por_dur` por `return None` deixa tudo verde.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import soundfile as sf

import app

FIXTURE = Path(__file__).parent / "fixtures" / "voz_fala_5s_16k.wav"


def test_interpola_entre_dois_pontos():
    """2,5 s cai entre 0,394 e 0,417 — e no meio deles, não em nenhuma ponta."""
    v = app._speaker_limiar_por_dur(2.5)
    assert v is not None
    assert 0.394 < v < 0.417, v
    # linear de verdade: 2,5 é o meio de 2,0–3,0
    assert v == pytest.approx((0.394 + 0.417) / 2)


def test_clampa_acima_do_ultimo_ponto():
    assert app._speaker_limiar_por_dur(9.0) == 0.417
    assert app._speaker_limiar_por_dur(3.0) == 0.417


def test_abaixo_do_piso_nao_se_aplica():
    """Abaixo de 1,0 s quem manda é o piso de verificação (`indeterminado`)."""
    assert app._speaker_limiar_por_dur(0.8) is None
    assert app._speaker_limiar_por_dur(None) is None


def test_alvo_0_1_continua_na_tabela_plana(monkeypatch):
    """O eixo do ALVO não pode regredir: 0,1 % segue no 0,48 plano."""
    monkeypatch.setitem(app._settings, "speaker_far_alvo", "0.1")
    assert app._speaker_limiar("ecapa:v1", 2.5)[0] == 0.48
    monkeypatch.setitem(app._settings, "speaker_far_alvo", "1")
    assert app._speaker_limiar("ecapa:v1", 2.5)[0] == pytest.approx(app._speaker_limiar_por_dur(2.5))


def test_sem_alvo_usa_o_calibrado_de_identidade(monkeypatch):
    monkeypatch.setitem(app._settings, "speaker_far_alvo", "")
    assert app._speaker_limiar("ecapa:v1", 2.5)[0] == app._ECAPA_LIMIAR_IDENTIDADE


def _wav(tmp_path: Path, seg: float) -> Path:
    a, sr = sf.read(str(FIXTURE), dtype="float32")
    p = tmp_path / f"fala-{seg}s.wav"
    sf.write(str(p), a[: int(sr * seg)], sr)
    return p


def test_gate_usa_o_theta_da_duracao(tmp_path, monkeypatch):
    """Integração: 2,5 s de fala com escore 0,40 é RECUSADO.

    O escore fica acima do θ plano de identidade (0,44? não: 0,40 < 0,44) e abaixo
    do θ da duração (~0,4055) — quem decide é a duração. Com o θ fixo de 0,39
    passaria, então a célula morde a fiação inteira.
    """
    monkeypatch.setitem(app._settings, "speaker_gate", "enforce")
    monkeypatch.setitem(app._settings, "speaker_far_alvo", "1")
    monkeypatch.setitem(app._settings, "speaker_anticlone", False)
    monkeypatch.setattr(app, "_speaker_load_estado",
                        lambda: ({"dono": {"vecs": [[1.0, 0.0]], "modelo": "ecapa:v1"}}, ""))
    monkeypatch.setattr(app, "_speaker_modelo_efetivo", lambda: "ecapa:v1")
    monkeypatch.setattr(app, "_speaker_pontua",
                        lambda wav, perfis: ("dono", 0.40, True, "cru"))
    out = app._speaker_gate_ok(_wav(tmp_path, 2.5))
    assert out.get("rejected") is True, out
    # o limiar APLICADO é o da duração, não o plano de 0,39
    assert out["speaker_limiar"] == pytest.approx(app._speaker_limiar_por_dur(2.5), abs=1e-3), out
    assert out["speaker_limiar"] > 0.39


def test_gate_com_escore_acima_passa(tmp_path, monkeypatch):
    monkeypatch.setitem(app._settings, "speaker_gate", "enforce")
    monkeypatch.setitem(app._settings, "speaker_far_alvo", "1")
    monkeypatch.setattr(app, "_speaker_load_estado",
                        lambda: ({"dono": {"vecs": [[1.0, 0.0]], "modelo": "ecapa:v1"}}, ""))
    monkeypatch.setattr(app, "_speaker_modelo_efetivo", lambda: "ecapa:v1")
    monkeypatch.setattr(app, "_speaker_pontua",
                        lambda wav, perfis: ("dono", 0.55, True, "cru"))
    assert not app._speaker_gate_ok(_wav(tmp_path, 2.5)).get("rejected")


def test_tabela_e_monotonica_e_dentro_da_faixa():
    """θ sobe com a duração e nenhum ponto sai da faixa plausível do ECAPA cru."""
    ds = [d for d, _ in app._SPEAKER_LIMIAR_DUR]
    vs = [v for _, v in app._SPEAKER_LIMIAR_DUR]
    assert ds == sorted(ds) and vs == sorted(vs)
    assert all(0.3 < v < 0.5 for v in vs), vs
    assert json.dumps(app._SPEAKER_LIMIAR_DUR)  # serializável (vai para o meta)


# --- as quatro do gate, com a tabela de 0,9 % já no lugar ---------------------

def test_limiar_por_dur_interpola_entre_os_pontos():
    v = app._speaker_limiar_por_dur(2.5)
    assert 0.394 < v < 0.417


def test_limiar_por_dur_clampa_e_devolve_none_fora_da_faixa():
    assert app._speaker_limiar_por_dur(9.0) == 0.417
    assert app._speaker_limiar_por_dur(0.8) is None


def test_limiar_por_dur_so_vale_no_alvo_de_um_por_cento(monkeypatch):
    # o nome é o do extrator (`ecapa:v1`); `"ecapa"` sozinho cai no ramo legado
    monkeypatch.setitem(app._settings, "speaker_far_alvo", "0.1")
    assert app._speaker_limiar("ecapa:v1", 2.5)[0] == 0.48
    monkeypatch.setitem(app._settings, "speaker_far_alvo", "1")
    assert 0.394 < app._speaker_limiar("ecapa:v1", 2.5)[0] < 0.417


def test_gate_numa_fala_de_dois_e_meio_segundos_usa_o_theta_da_duracao(tmp_path, monkeypatch):
    """Integração: o θ aplicado é o da duração, e ele aparece na resposta."""
    monkeypatch.setitem(app._settings, "speaker_gate", "enforce")
    monkeypatch.setitem(app._settings, "speaker_far_alvo", "1")
    monkeypatch.setattr(app, "_speaker_load_estado",
                        lambda: ({"dono": {"vecs": [[1.0, 0.0]], "modelo": "ecapa:v1"}}, ""))
    monkeypatch.setattr(app, "_speaker_modelo_efetivo", lambda: "ecapa:v1")
    monkeypatch.setattr(app, "_speaker_pontua",
                        lambda wav, perfis: ("dono", 0.40, True, "cru"))
    out = app._speaker_gate_ok(_wav(tmp_path, 2.5))
    assert out["speaker_limiar"] == pytest.approx(app._speaker_limiar_por_dur(2.5), abs=1e-3)
    assert out["speaker_limiar"] > 0.39, "θ plano vazando: a duração não foi usada"


@pytest.mark.parametrize("bruto,canonico", [
    ("1", "1"), ("1.0", "1"), (" 1 ", "1"), ("1,0", "1"),
    ("0.1", "0.1"), ("0.10", "0.1"), ("0,1", "0.1"),
    ("", ""), (None, ""), ("2", "2"),
])
def test_alvo_e_normalizado_antes_de_comparar(bruto, canonico):
    """`settings.json` escrito à mão traz "1.0"/"1,0": sem normalizar, o alvo caía
    calado no limiar de identidade e o dono perdia a curva por duração."""
    assert app._normaliza_alvo_far(bruto) == canonico


def test_alvo_1_virgula_usa_a_curva(monkeypatch):
    """A consequência do normalizador, no caminho de verdade."""
    monkeypatch.setitem(app._settings, "speaker_far_alvo", "1,0")
    assert app._speaker_limiar("ecapa:v1", 3.0)[0] == pytest.approx(0.417, abs=1e-3)


def test_alvo_0_1_virgula_usa_a_tabela_plana(monkeypatch):
    monkeypatch.setitem(app._settings, "speaker_far_alvo", "0,1")
    assert app._speaker_limiar("ecapa:v1", 3.0)[0] == 0.48
