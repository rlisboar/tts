"""Testes do catálogo/adapter (backends.py) — funções puras."""

import backends


def test_resolve_backend_atalhos_e_aliases():
    be = backends.resolve_backend("qwen3-0.6b")
    assert be["family"] == "qwen3_tts" and be["is_shortcut"]
    assert be["path"] == "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-bf16"
    assert backends.resolve_backend("qwen3")["id"] == "qwen3-0.6b"      # alias
    assert backends.resolve_backend("fish")["id"] == "fish-s2"
    assert backends.resolve_backend("voxtral")["id"] == "voxtral-tts"
    assert backends.resolve_backend("")["family"] == "omnivoice"        # vazio = omni
    assert backends.resolve_backend("omnivoice")["family"] == "omnivoice"


def test_resolve_backend_path_livre_deduz_familia():
    be = backends.resolve_backend("mlx-community/Algum-Modelo-VoiceDesign-bf16")
    assert be["is_shortcut"] is False
    assert be["family"] == "qwen3_design"
    assert be["path"] == "mlx-community/Algum-Modelo-VoiceDesign-bf16"


def test_guess_family():
    assert backends._guess_family("mlx-community/Kokoro-82M-bf16") == "kokoro"
    assert backends._guess_family("chatterbox-turbo-fp16") == "chatterbox"
    assert backends._guess_family("VoxCPM2-8bit") == "voxcpm2"
    assert backends._guess_family("modelo-totalmente-desconhecido") == "generic"


def test_temp_prioridade_e_legado():
    assert backends._temp({"temperature": 0.3}) == 0.3
    # legado Omni: class_temperature 0 = greedy -> em AR cai no default
    assert backends._temp({"class_temperature": 0.0}, default=0.8) == 0.8
    assert backends._temp({"class_temperature": 1.2}) == 1.2
    assert backends._temp({}) == 0.8


def test_lang_name():
    assert backends._lang_name("pt") == "Portuguese"
    assert backends._lang_name("auto") == "Auto"
    assert backends._lang_name("none") == "Auto"
    assert backends._lang_name(None) == "Auto"
    assert backends._lang_name("xx") == "xx"    # desconhecido passa cru


def test_filter_kwargs():
    def fn(a, b=1):
        pass
    assert backends._filter_kwargs(fn, {"a": 1, "c": 2}) == {"a": 1}

    def fn_kwargs(**kw):
        pass
    assert backends._filter_kwargs(fn_kwargs, {"a": 1}) == {"a": 1}  # **kwargs aceita tudo


def test_catalogo_consistente():
    for bid, meta in backends.BACKENDS.items():
        assert meta["family"] in backends.FAMILY_CONTROLS, f"{bid}: sem controls"
        assert meta["family"] in backends.FAMILY_DESIGN_MODE, f"{bid}: sem design_mode"
        assert meta.get("label") and meta.get("repo"), bid
    for alias, alvo in backends.ALIASES.items():
        assert alvo in backends.BACKENDS, f"alias {alias} → {alvo} inexistente"


def test_controls_for_family_fallback_generico():
    assert (backends.controls_for_family("familia-inexistente")
            == backends.controls_for_family("generic"))


def test_design_mode_por_familia():
    assert backends.design_mode_for_family("omnivoice") == "omni_tags"
    assert backends.design_mode_for_family("qwen3_design") == "free_text_required"
    assert backends.design_mode_for_family("inexistente") == "free_text"
