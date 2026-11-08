"""LLM falso OpenAI-compat (SSE) com ritmo FIXO — régua do `pos_llm` (#1022).

O `pos_llm` do Live (1º token → 1º áudio) mistura o LLM ainda escrevendo a 1ª
frase com TTS, marca e fila. Com o provedor real o ritmo dos tokens varia de
turno a turno e esconde o que é nosso. Este servidor devolve SEMPRE a mesma
resposta, palavra a palavra, com TTFT e tokens/s declarados: o que muda entre
duas rodadas passa a ser só o lado do app.

Sem estado do dono e sem rede: escuta em 127.0.0.1.

Uso:
    ./.venv-mlx/bin/python tools/llm_fake_sse.py --porta 7899 --ttft-ms 300 --tps 40
    TTS_CHAT_BASE_URL=http://127.0.0.1:7899/v1 TTS_CHAT_MODEL=fake ...
"""
from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RESPOSTA = ("Claro, posso te ajudar com isso agora mesmo. O mais simples é começar "
            "pelo que você já tem em casa e ver o que falta. Depois a gente ajusta "
            "os detalhes com calma, sem pressa nenhuma.")


def tokens(texto: str) -> list[str]:
    """Palavra + espaço: perto do que um provedor real manda por delta."""
    partes = texto.split(" ")
    return [p + (" " if i < len(partes) - 1 else "") for i, p in enumerate(partes)]


def chunk(conteudo: str | None, fim: bool = False) -> bytes:
    escolha = {"index": 0, "delta": {} if conteudo is None else {"content": conteudo},
               "finish_reason": "stop" if fim else None}
    corpo = {"id": "fake", "object": "chat.completion.chunk", "model": "fake",
             "choices": [escolha]}
    return b"data: " + json.dumps(corpo, ensure_ascii=False).encode() + b"\n\n"


def faz_handler(ttft_s: float, intervalo_s: float, resposta: str):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):          # silencioso: a régua não polui o log
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            pedido = json.loads(self.rfile.read(n) or b"{}")
            if not pedido.get("stream"):
                corpo = json.dumps({"choices": [{"index": 0, "finish_reason": "stop",
                                                 "message": {"role": "assistant",
                                                             "content": resposta}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(corpo)))
                self.end_headers()
                self.wfile.write(corpo)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                time.sleep(ttft_s)
                for tok in tokens(resposta):
                    self.wfile.write(chunk(tok))
                    self.wfile.flush()
                    time.sleep(intervalo_s)
                self.wfile.write(chunk(None, fim=True))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass                         # barge-in derrubou o turno: normal
            self.close_connection = True

    return H


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--porta", type=int, default=7899)
    ap.add_argument("--ttft-ms", type=float, default=300.0)
    ap.add_argument("--tps", type=float, default=40.0, help="tokens (palavras) por segundo")
    ap.add_argument("--resposta", default=RESPOSTA)
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.porta),
                              faz_handler(a.ttft_ms / 1000, 1 / max(a.tps, 0.1), a.resposta))
    print(f"[llm-fake] 127.0.0.1:{a.porta} ttft={a.ttft_ms:.0f}ms tps={a.tps}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
