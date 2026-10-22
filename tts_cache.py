"""Cache de áudio gerado, por (texto, voz, parâmetros) — #325.

Repetir a mesma frase é comum (Conversa, A/B de parâmetro) e pagava geração
inteira de novo, com Metal ocupado e RAM gasta. Aqui o PCM já PÓS-PROCESSADO de
cada trecho é guardado em disco e devolvido no lugar do `generate`.

Regras que importam:

- desligado por padrão (`TTS_TTS_CACHE=1` ou `tts_cache: true`); TTL em segundos
  (`tts_cache_ttl_s`, padrão 1 h) e teto de entradas (`tts_cache_max`);
- só acerta com a chave INTEIRA igual: texto normalizado, voz + mtime, família,
  modelo, precisão, idioma, motor (local/remoto) e todos os parâmetros que mudam
  o áudio — inclusive EQ/ganho, que entram depois da geração;
- nada do TEXTO vai para o disco, só o hash: o cache é local e não expõe o que
  foi falado;
- escrita atômica (tmp único + `os.replace`), como o resto da casa.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

# parâmetros que mudam o áudio do trecho (os do OmniVoice e os dos backends AR)
_CHAVES_OMNI = (
    "num_steps", "guidance_scale", "class_temperature", "position_temperature",
    "layer_penalty_factor", "t_shift", "instruct", "seed", "speed", "duration_s",
    "denoise", "preprocess_prompt", "postprocess_output", "audio_chunk_duration",
    "audio_chunk_threshold", "temperature", "top_p", "top_k", "repetition_penalty",
    "max_tokens", "exaggeration", "cfg_weight", "min_p", "chunk_length",
    "speaker", "kokoro_voice", "pocket_voice", "voxtral_voice",
)


def ligado(settings: dict | None = None) -> bool:
    env = (os.environ.get("TTS_TTS_CACHE") or "").strip()
    if env:
        return env != "0"
    return bool((settings or {}).get("tts_cache", False))


def _ttl_s(settings: dict) -> float:
    try:
        return max(0.0, float(settings.get("tts_cache_ttl_s", 3600)))
    except (TypeError, ValueError):
        return 3600.0


def _teto(settings: dict) -> int:
    try:
        return max(1, int(settings.get("tts_cache_max", 500)))
    except (TypeError, ValueError):
        return 500


def chave(*, texto: str, voice_id: str, voice_mtime: int, family: str, model: str,
          precision: str, language: str, motor: str, omni: dict) -> str:
    """Hash estável de tudo que muda o áudio do trecho."""
    h = hashlib.sha256()
    partes = {
        "texto": texto, "voz": voice_id, "mtime": int(voice_mtime),
        "familia": family, "modelo": model, "precisao": precision,
        "idioma": language, "motor": motor,
        "params": {k: omni.get(k) for k in _CHAVES_OMNI},
    }
    h.update(json.dumps(partes, sort_keys=True, default=str).encode("utf-8"))
    return h.hexdigest()[:32]


class Cache:
    """Diretório do cache. `pega`/`guarda` com PCM16 e poda por TTL/teto."""

    def __init__(self, base: Path):
        self.base = Path(base)

    def _caminho(self, chave: str) -> Path:
        return self.base / f"{chave}.wav"

    def pega(self, chave: str, ttl_s: float):
        """Áudio do PCM16 guardado, ou None. Expirado/ausente = None."""
        caminho = self._caminho(chave)
        try:
            if ttl_s and (time.time() - caminho.stat().st_mtime) > ttl_s:
                caminho.unlink(missing_ok=True)
                return None
            import soundfile as sf

            dados, _ = sf.read(str(caminho), dtype="float32")
            return dados
        except Exception:            # noqa: BLE001 — cache é best-effort
            return None

    def guarda(self, chave: str, audio, sr: int, *, ttl_s: float, teto: int) -> bool:
        import numpy as np
        import soundfile as sf

        self.base.mkdir(parents=True, exist_ok=True)
        fd, tmp_nome = tempfile.mkstemp(dir=str(self.base), prefix=chave + ".", suffix=".tmp")
        os.close(fd)
        tmp = Path(tmp_nome)
        try:
            # o tmp não tem extensão de áudio: o formato tem de ser explícito
            sf.write(str(tmp), np.asarray(audio, dtype=np.float32), sr,
                     subtype="PCM_16", format="WAV")
            os.replace(tmp, self._caminho(chave))
        except Exception:            # noqa: BLE001
            return False
        finally:
            tmp.unlink(missing_ok=True)
        self.poda(ttl_s=ttl_s, teto=teto)
        return True

    def poda(self, *, ttl_s: float, teto: int) -> int:
        """Remove expirados e, se passar do teto, os mais antigos."""
        try:
            itens = sorted(self.base.glob("*.wav"), key=lambda p: p.stat().st_mtime)
        except OSError:
            return 0
        agora = time.time()
        removidos = 0
        for p in itens:
            try:
                expirado = ttl_s and (agora - p.stat().st_mtime) > ttl_s
                if expirado or len(itens) - removidos > teto:
                    p.unlink(missing_ok=True)
                    removidos += 1
            except OSError:
                continue
        return removidos

    def limpa(self) -> int:
        n = 0
        try:
            for p in self.base.glob("*.wav"):
                p.unlink(missing_ok=True)
                n += 1
        except OSError:
            pass
        return n
