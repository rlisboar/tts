#!/usr/bin/env python3
"""E2E do gate de biometria (#980) — instância própria, estado em /tmp.

Fase 1: extrator LEGADO (dir de modelos vazio) → cadastra perfil sem `modelo`.
Fase 2: ECAPA (dir real) → perfil legado segue autenticando, `precisa_recadastrar`
        true, enforce NÃO tranca o dono e o terceiro é recusado; o recadastro
        migra o perfil para o extrator novo e ele segue autenticando.
"""
import json, os, pathlib, shutil, signal, socket, subprocess, sys, time
import urllib.request, urllib.error

RAIZ = pathlib.Path("/Users/lisboa/Documents/tts-rod")
PY = RAIZ / ".venv-mlx/bin/python"
TMP = pathlib.Path("/tmp/e2e980")
falhas = []


def cobrar(cond, msg):
    print(("  ✔ " if cond else "  ✘ ") + msg)
    if not cond:
        falhas.append(msg)


def porta_livre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]


def sobe(modelo_dir):
    porta = porta_livre()
    env = {**os.environ,
           "TTS_SETTINGS_PATH": str(TMP / "settings.json"),
           "TTS_APIKEYS_PATH": str(TMP / "apikeys.json"),
           "TTS_LEGACY_APIKEY_PATH": str(TMP / "apikey"),
           "TTS_OUTPUTS_DIR": str(TMP / "outputs"),
           "TTS_SPEAKER_PATH": str(TMP / "perfis.json"),
           "TTS_SPEAKER_MODELO_DIR": str(modelo_dir)}
    proc = subprocess.Popen([str(PY), "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
                             "--port", str(porta), "--log-level", "warning"],
                            cwd=str(RAIZ), env=env, stdout=open(TMP / f"log-{porta}.txt", "w"),
                            stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(300):
        if proc.poll() is not None:
            raise SystemExit(f"servidor morreu — log {TMP}/log-{porta}.txt")
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{porta}/health", timeout=2).read()
            return proc, f"http://127.0.0.1:{porta}"
        except Exception:
            time.sleep(0.2)
    raise SystemExit("servidor não subiu")


def mata(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=20)
    except Exception:
        try: os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception: pass


def multipart(campo, nome, dados: bytes, extra=None):
    b = "----e2e980"
    partes = []
    for k, v in (extra or {}).items():
        partes.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    partes.append(f'--{b}\r\nContent-Disposition: form-data; name="{campo}"; filename="{nome}"\r\n'
                  f'Content-Type: audio/wav\r\n\r\n'.encode() + dados + b"\r\n")
    partes.append(f"--{b}--\r\n".encode())
    return b"".join(partes), f"multipart/form-data; boundary={b}"


def post(url, campo=None, nome=None, dados=None, extra=None, json_body=None):
    if json_body is not None:
        req = urllib.request.Request(url, data=json.dumps(json_body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    else:
        corpo, ct = multipart(campo, nome, dados, extra)
        req = urllib.request.Request(url, data=corpo, headers={"Content-Type": ct}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read() or b"{}")
        except Exception: return e.code, {}


def get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


def wav(caminho, ini_s, dur_s):
    import numpy as np, soundfile as sf
    a, sr = sf.read(caminho, dtype="float32")
    if a.ndim > 1: a = a.mean(axis=1)
    pedaco = a[int(ini_s * sr):int((ini_s + dur_s) * sr)]
    import io
    buf = io.BytesIO(); sf.write(buf, pedaco, sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def main():
    shutil.rmtree(TMP, ignore_errors=True)
    (TMP / "vazio").mkdir(parents=True)
    (TMP / "outputs").mkdir()
    (TMP / "settings.json").write_text("{}\n")

    vozes = sorted(p for p in (RAIZ / "voices").glob("*.wav")
                   if p.with_suffix(".json").exists())
    dono, terceiro = vozes[0], vozes[1]
    print(f"vozes: dono={dono.name} terceiro={terceiro.name}")

    # ── fase 1: perfil LEGADO (extrator Resemblyzer) ───────────────────────
    p1, u1 = sobe(TMP / "vazio")
    try:
        for i in range(3):
            st, r = post(f"{u1}/api/speaker/enroll", "audio", f"a{i}.wav",
                         wav(dono, 0.3 + i * 2.0, 4.0), {"name": "Dono E2E"})
        cobrar(st == 200, f"enroll legado responde 200 (veio {st})")
        perfis = get(f"{u1}/api/speaker/profiles")
        p = (perfis if isinstance(perfis, list) else perfis.get("perfis") or [{}])[0]
        cobrar(p.get("modelo") == "resemblyzer:v1", f"enroll novo grava o extrator (veio {p.get('modelo')!r})")
        # perfil LEGADO de verdade: o campo `modelo` não existia antes do #525
        dados = json.loads((TMP / "perfis.json").read_text())
        _ = dados
        for v in dados.values():
            v.pop("modelo", None)
        (TMP / "perfis.json").write_text(json.dumps(dados))
        st, r = post(f"{u1}/api/speaker/check", "audio", "x.wav", wav(dono, 8.0, 4.0))
        cobrar(r.get("passaria") is True, f"check do dono no legado passa ({r.get('passaria')})")
        # avalia: amostra CURTA (1 s) é recusada com motivo, e `forcar` aceita
        st, r = post(f"{u1}/api/speaker/avalia", "audio", "curta.wav", wav(dono, 0.5, 2.5))
        cobrar(st == 200 and r.get("problemas"), f"avalia recusa amostra curta com motivo ({r.get('problemas')})")
        st, r = post(f"{u1}/api/speaker/enroll?forcar=true", "audio", "forcada.wav",
                     wav(dono, 3.0, 2.5), {"name": "Dono E2E"})
        cobrar(st == 200, f"enroll com forcar aceita amostra curta ({st})")
    finally:
        mata(p1)

    # ── fase 2: ECAPA ligado, mesmo arquivo de perfis ─────────────────────
    p2, u2 = sobe(RAIZ / "models" / "ecapa-voxceleb")
    try:
        st = get(f"{u2}/api/settings")
        cobrar(st.get("extrator_efetivo") == "ecapa:v1", f"settings.extrator_efetivo={st.get('extrator_efetivo')!r}")
        stt = get(f"{u2}/api/status")
        cobrar(stt.get("extrator_locutor") == "ecapa:v1", f"status.extrator_locutor={stt.get('extrator_locutor')!r}")
        perfis = get(f"{u2}/api/speaker/profiles")
        p = (perfis if isinstance(perfis, list) else perfis.get("perfis") or [{}])[0]
        cobrar(p.get("precisa_recadastrar") is True, f"perfil legado marcado precisa_recadastrar ({p.get('precisa_recadastrar')})")

        st, r = post(f"{u2}/api/speaker/check", "audio", "x.wav", wav(dono, 12.0, 4.0))
        cobrar(r.get("passaria") is True, f"dono SEGUE autenticando no legado com ECAPA ligado ({r.get('passaria')}, modo={r.get('modo')})")
        st, r = post(f"{u2}/api/speaker/check", "audio", "y.wav", wav(terceiro, 0.5, 4.0))
        cobrar(r.get("passaria") is False, f"terceiro recusado no check ({r.get('passaria')})")

        st, _ = post(f"{u2}/api/settings", json_body={"speaker_gate": "enforce"})
        cobrar(st == 200, f"gate enforce aplicado ({st})")

        st, r = post(f"{u2}/api/transcribe", "audio", "d.wav", wav(dono, 16.0, 4.0))
        cobrar(st == 200 and not r.get("rejected"), f"enforce NÃO tranca o dono (st={st}, rejected={r.get('rejected')})")
        st, r = post(f"{u2}/api/transcribe", "audio", "t.wav", wav(terceiro, 6.0, 4.0))
        cobrar(bool(r.get("rejected")), f"enforce recusa o terceiro (rejected={r.get('rejected')}, motivo={str(r.get('reason'))[:60]})")

        # recadastro: 3 amostras novas no extrator atual migram o perfil
        for i in range(3):
            st, r = post(f"{u2}/api/speaker/enroll", "audio", f"n{i}.wav",
                         wav(dono, 1.0 + i * 2.5, 4.0), {"name": "Dono E2E"})
        perfis = get(f"{u2}/api/speaker/profiles")
        p = (perfis if isinstance(perfis, list) else perfis.get("perfis") or [{}])[0]
        cobrar(p.get("modelo") == "ecapa:v1", f"recadastro migra o perfil para ecapa (modelo={p.get('modelo')!r})")
        cobrar(not p.get("precisa_recadastrar"), "perfil migrado não pede mais recadastro")
        st, r = post(f"{u2}/api/speaker/check", "audio", "z.wav", wav(dono, 20.0, 4.0))
        cobrar(r.get("passaria") is True, f"dono segue autenticando no extrator novo ({r.get('passaria')})")
    finally:
        mata(p2)

    print()
    if falhas:
        print(f"✖ FALHOU ({len(falhas)})"); sys.exit(1)
    print("✔ OK — legado autentica, enforce não tranca o dono, terceiro recusado, recadastro migra")


if __name__ == "__main__":
    main()
