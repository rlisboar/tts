#!/usr/bin/env python3
"""TTS-Rod — Roteador de microfone (janela + botão).

Alimenta um MICROFONE VIRTUAL (BlackHole) que os apps de chamada (Zoom/Meet/
Discord) selecionam como entrada. O botão alterna a FONTE desse mic virtual:

  • DESLIGADO (padrão) -> microfone REAL passa direto pro BlackHole (sua voz).
  • LIGADO            -> o cliente fica em silêncio; quem alimenta o BlackHole é
                         o navegador do TTS-Rod (voz traduzida), com a saída de
                         áudio dele apontada pro "BlackHole 2ch".

Assim você deixa "BlackHole 2ch" fixo como microfone na chamada e troca entre
sua voz e a voz do app só clicando no botão — sem mexer nas configs da chamada.

Pré-requisitos:
  • BlackHole 2ch  ->  brew install blackhole-2ch   (reinicie a sessão de áudio)
  • Deps Python    ->  sounddevice, numpy            (o run.command instala)

Uso: python3 mic_router.py   (ou clique em run.command)
"""
import json
import os
import threading
import tkinter as tk
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tkinter import ttk

import numpy as np
import sounddevice as sd

SR = 48000          # BlackHole e a maioria dos mics suportam 48 kHz
BLOCK = 256         # ~5 ms de latência por bloco
VIRTUAL_HINTS = ("blackhole", "loopback", "vb-cable", "vb-audio")
CTRL_PORTS = (7861, 7862, 7863, 7864, 7865)   # portas de controle local (loopback)


def start_control_server(get_mode):
    """Servidor HTTP em 127.0.0.1 que o NAVEGADOR (mesma máquina) consulta pra
    saber o modo. É o canal principal do gate: loopback sempre funciona, sem
    depender de o cliente alcançar o servidor remoto (proxy/VPN/subnet de B).
    Devolve a porta usada (ou None se todas ocupadas)."""
    class H(BaseHTTPRequestHandler):
        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Access-Control-Max-Age", "600")

        def do_OPTIONS(self):
            self.send_response(204); self._cors(); self.end_headers()

        def do_GET(self):
            body = json.dumps({"mode": get_mode()}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self._cors(); self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):   # silencioso
            pass

    for port in CTRL_PORTS:
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), H)
        except OSError:
            continue
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return port
    return None


CFG_PATH = Path(__file__).resolve().parent / "config.json"


def _load_config():
    """Endereço + chave do server TTS-Rod. Prioridade: env > config.json > default.
    O config.json vem do download (com o IP do server + a chave) e é editável."""
    url = os.environ.get("TTS_ROD_URL", "")
    key = os.environ.get("TTS_ROD_API_KEY", "")
    if CFG_PATH.is_file():
        try:
            c = json.loads(CFG_PATH.read_text())
            url = url or c.get("server_url", "")
            key = key or c.get("api_key", "")
        except Exception:
            pass
    return {"url": (url or "http://127.0.0.1:7860").rstrip("/"), "key": key}


CFG = _load_config()


def save_config():
    try:
        CFG_PATH.write_text(json.dumps(
            {"server_url": CFG["url"], "api_key": CFG["key"]}, indent=2))
    except Exception:
        pass


def publish_mode(passthrough, cb=None):
    """Avisa o server o modo: passthrough=True -> 'real' (silencia o TTS no
    navegador); False -> 'app'. cb(ok, msg) é chamado ao fim, de outra thread."""
    mode = "real" if passthrough else "app"
    url, key = CFG["url"], CFG["key"]

    def _send():
        try:
            data = json.dumps({"mode": mode}).encode()
            headers = {"Content-Type": "application/json"}
            if key:
                headers["X-API-Key"] = key
            req = urllib.request.Request(
                f"{url}/api/mic-route", data=data, method="POST", headers=headers)
            with urllib.request.urlopen(req, timeout=3) as r:
                r.read()
            if cb:
                cb(True, mode)
        except Exception as e:  # noqa: BLE001
            if cb:
                cb(False, str(e)[:60])

    threading.Thread(target=_send, daemon=True).start()


def list_io():
    """Devolve (inputs, outputs) como listas de (idx, nome)."""
    ins, outs = [], []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            ins.append((i, d["name"]))
        if d["max_output_channels"] > 0:
            outs.append((i, d["name"]))
    return ins, outs


def find_virtual(outs):
    for idx, name in outs:
        if any(h in name.lower() for h in VIRTUAL_HINTS):
            return idx
    return outs[0][0] if outs else None


def default_input(ins):
    try:
        di = sd.default.device[0]
        if any(i == di for i, _ in ins):
            return di
    except Exception:
        pass
    # evita escolher o próprio dispositivo virtual como "mic real"
    for idx, name in ins:
        if not any(h in name.lower() for h in VIRTUAL_HINTS):
            return idx
    return ins[0][0] if ins else None


class Router:
    """Stream duplex mic_real -> mic_virtual, com gate e medidor de nível."""

    def __init__(self):
        self.stream = None
        self.passthrough = True     # True = voz real; False = silêncio (app fala)
        self.gain = 1.0
        self.level = 0.0            # RMS do último bloco (0..1), lido pela GUI
        self._err = None

    def _callback(self, indata, outdata, frames, time, status):  # noqa: ARG002
        # nível de entrada (sempre, mesmo mutado, pra dar feedback visual)
        self.level = float(np.sqrt(np.mean(indata[:, 0] ** 2)) + 1e-9)
        if self.passthrough:
            mono = indata[:, 0] * self.gain
            np.clip(mono, -1.0, 1.0, out=mono)
            outdata[:, 0] = mono
            if outdata.shape[1] > 1:
                outdata[:, 1] = mono
        else:
            outdata.fill(0.0)       # app (navegador) alimenta o BlackHole

    def start(self, in_idx, out_idx):
        self.stop()
        self._err = None
        out_ch = min(2, sd.query_devices(out_idx)["max_output_channels"]) or 1
        try:
            self.stream = sd.Stream(
                samplerate=SR, blocksize=BLOCK, dtype="float32",
                device=(in_idx, out_idx), channels=(1, out_ch),
                callback=self._callback, latency="low",
            )
            self.stream.start()
        except Exception as e:  # noqa: BLE001
            self._err = str(e)
            self.stream = None

    def stop(self):
        if self.stream is not None:
            try:
                self.stream.stop(); self.stream.close()
            except Exception:
                pass
            self.stream = None


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("TTS-Rod · Roteador de microfone")
        self.configure(bg="#0f1115")
        self.resizable(False, False)
        self.router = Router()

        self.ins, self.outs = list_io()
        self.in_var = tk.StringVar()
        self.out_var = tk.StringVar()

        # canal principal: o navegador (mesma máquina) consulta o modo aqui
        self.ctrl_port = start_control_server(
            lambda: "real" if self.router.passthrough else "app")

        self._build()
        self._restart()
        self._tick()
        self._publish()                          # fallback remoto (best-effort, silencioso)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- UI ----------
    def _build(self):
        pad = dict(padx=16, pady=6)
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TLabel", background="#0f1115", foreground="#cdd3df")
        style.configure("TCombobox", fieldbackground="#1b1f2a", background="#1b1f2a")

        ttk.Label(self, text="Microfone real (fonte)").grid(row=0, column=0, sticky="w", **pad)
        self.cb_in = ttk.Combobox(self, textvariable=self.in_var, width=34, state="readonly",
                                  values=[n for _, n in self.ins])
        self.cb_in.grid(row=1, column=0, sticky="we", **pad)
        self.cb_in.bind("<<ComboboxSelected>>", lambda e: self._restart())

        ttk.Label(self, text="Microfone virtual (destino · BlackHole)").grid(row=2, column=0, sticky="w", **pad)
        self.cb_out = ttk.Combobox(self, textvariable=self.out_var, width=34, state="readonly",
                                   values=[n for _, n in self.outs])
        self.cb_out.grid(row=3, column=0, sticky="we", **pad)
        self.cb_out.bind("<<ComboboxSelected>>", lambda e: self._restart())

        # pré-seleção
        di = default_input(self.ins)
        do = find_virtual(self.outs)
        if di is not None:
            self.in_var.set(dict(self.ins)[di])
        if do is not None:
            self.out_var.set(dict(self.outs)[do])

        # botão grande
        self.btn = tk.Button(self, text="", font=("SF Pro Display", 16, "bold"),
                             width=26, height=2, bd=0, relief="flat",
                             activeforeground="#fff", command=self._toggle)
        self.btn.grid(row=4, column=0, padx=16, pady=(14, 6))

        # medidor de nível
        self.meter = tk.Canvas(self, width=300, height=10, bg="#1b1f2a", highlightthickness=0)
        self.meter.grid(row=5, column=0, padx=16, pady=(0, 4))
        self._meter_rect = self.meter.create_rectangle(0, 0, 0, 10, fill="#39d98a", width=0)

        self.status = ttk.Label(self, text="", font=("SF Pro Text", 10))
        self.status.grid(row=6, column=0, sticky="w", padx=16, pady=(2, 2))

        if self.ctrl_port:
            txt = f"Controle local ativo (127.0.0.1:{self.ctrl_port}) · navegador no MESMO PC"
            col = "#7c8596"
        else:
            txt = "✗ Porta de controle ocupada — feche outra instância"
            col = "#e0707a"
        self.srv = ttk.Label(self, text=txt, font=("SF Pro Text", 9), foreground=col)
        self.srv.grid(row=7, column=0, sticky="w", padx=16, pady=(0, 14))

        self._paint_button()

    def _idx(self, var, items):
        name = var.get()
        for idx, n in items:
            if n == name:
                return idx
        return None

    def _restart(self):
        in_idx = self._idx(self.in_var, self.ins)
        out_idx = self._idx(self.out_var, self.outs)
        if in_idx is None or out_idx is None:
            self.status.config(text="Selecione mic real e mic virtual.")
            return
        self.router.start(in_idx, out_idx)
        if self.router._err:
            self.status.config(text=f"Erro ao abrir áudio: {self.router._err[:60]}")
        else:
            self._paint_button()

    def _toggle(self):
        self.router.passthrough = not self.router.passthrough
        self._paint_button()
        self._publish()                          # avisa o navegador (silencia/libera TTS)

    def _publish(self):
        # O gate funciona pelo servidor de controle local (o navegador faz polling
        # nele). Isto aqui é só um aviso best-effort ao server remoto, silencioso —
        # se B não alcança o Mac (proxy/VPN), falha sem efeito e tudo bem.
        publish_mode(self.router.passthrough)

    def _paint_button(self):
        if self.router.passthrough:
            self.btn.config(text="🎙  Voz real  (clique p/ voz do app)",
                            bg="#2d7d46", activebackground="#246b3b", fg="#fff")
            self.status.config(text="Chamada ouve seu microfone real.")
        else:
            self.btn.config(text="🗣  Voz do app  (clique p/ voz real)",
                            bg="#3358cc", activebackground="#2b49a8", fg="#fff")
            self.status.config(text="Chamada ouve o TTS-Rod (saída do navegador = BlackHole).")

    def _tick(self):
        # medidor: barra proporcional ao nível, só quando passando a voz real
        lvl = self.router.level if self.router.passthrough else 0.0
        w = int(min(1.0, lvl * 4.0) * 300)
        col = "#39d98a" if lvl < 0.5 else "#e8c33d"
        self.meter.coords(self._meter_rect, 0, 0, w, 10)
        self.meter.itemconfig(self._meter_rect, fill=col)
        self.after(60, self._tick)

    def _on_close(self):
        # libera o TTS no navegador ao sair (modo "app") — síncrono p/ garantir
        # que chega antes do processo morrer
        try:
            data = json.dumps({"mode": "app"}).encode()
            headers = {"Content-Type": "application/json"}
            if CFG["key"]:
                headers["X-API-Key"] = CFG["key"]
            req = urllib.request.Request(
                f"{CFG['url']}/api/mic-route", data=data, method="POST", headers=headers)
            urllib.request.urlopen(req, timeout=1).read()
        except Exception:
            pass
        self.router.stop()
        self.destroy()


def main():
    if not any("blackhole" in n.lower() for _, n in list_io()[1]):
        print("AVISO: 'BlackHole' não encontrado nos dispositivos de saída.\n"
              "Instale com:  brew install blackhole-2ch  (e reinicie o app de áudio).")
    App().mainloop()


if __name__ == "__main__":
    main()
