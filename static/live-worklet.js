// Worklet de CAPTURA da sessão Live (task #94).
//
// Por que arquivo separado e não embutido no index.html via Blob: a CSP da UI é
// `script-src 'self' 'nonce-…' 'wasm-unsafe-eval' https://cdn.jsdelivr.net`, e
// nem `blob:` nem `data:` estão na lista — `audioWorklet.addModule()` com os dois
// falha com AbortError (medido). Servir deste arquivo cai em `'self'` e carrega
// sem mexer na policy.
//
// Taxa: o AudioContext do Live é criado a 16000 Hz, então cada quantum de 128
// amostras já está na taxa do protocolo — nenhum resample aqui (menos código e
// nenhuma deriva por acumulação). Mono, PCM16.
//
// Entrega em blocos de 100 ms (1600 amostras): o main thread só encaminha para o
// WebSocket, sem montar buffer nem converter nada.

const AMOSTRAS_POR_BLOCO = 1600;        // 100 ms @ 16 kHz

class LiveCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.bloco = new Int16Array(AMOSTRAS_POR_BLOCO);
    this.n = 0;
  }

  process(inputs) {
    const canal = inputs[0] && inputs[0][0];
    if (!canal || canal.length === 0) return true;   // nada conectado ainda

    for (let i = 0; i < canal.length; i++) {
      // float [-1,1] -> PCM16 com clamp: sinal quente do mic estoura, e o wrap
      // do Int16 viraria estalo audível
      let s = canal[i];
      if (s > 1) s = 1; else if (s < -1) s = -1;
      this.bloco[this.n++] = s < 0 ? s * 0x8000 : s * 0x7fff;
      if (this.n === AMOSTRAS_POR_BLOCO) {
        // cópia transferível: `this.bloco` é reusado no próximo ciclo, então
        // mandar o mesmo buffer cru exigiria esperar o main thread
        this.port.postMessage(this.bloco.slice(0), []);
        this.n = 0;
      }
    }
    return true;                        // o nó vive até o main desconectar
  }
}

registerProcessor("live-capture", LiveCapture);