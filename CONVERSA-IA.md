# Conversa — Manual de integração (IA do The Dudes)

Serviço de conversa por voz/texto do TTS-STUDIO para **decidir, junto com o
humano, o texto que um agente de voz vai falar**. A IA conduz: pergunta,
propõe rascunhos, incorpora ajustes — e devolve um **texto final aprovado**
quando o humano confirma. O Dudes envia esse texto ao agente (síntese na voz
dele).

- **Provedor do LLM**: roda no TTS-STUDIO (OpenAI-compat, configurável lá).
  O Dudes só fala com o TTS-STUDIO — nunca diretamente com o provedor.
- **Privacidade**: o histórico da conversa vai para o provedor do LLM.
- **A conversa não para** após o texto aprovado: ela continua até o `DELETE`
  ou expiração (1h sem uso). Cada novo objetivo = nova sessão.

## Base URL e autenticação

```
LAN:     http://192.168.1.50:7860
Internet: https://SEU-DOMINIO.com/ttsproxy
```

Toda requisição a `/api/*` exige a chave em **um** destes:

```
X-API-Key: <chave>
Authorization: Bearer <chave>
?api_key=<chave>          (query string, p/ players de áudio)
```

## Máquina de estados da sessão

```
POST /start → thinking → (worker chama o LLM em background)
            → chatting  → IA respondeu, aguardando o humano
            → confirmed → texto aprovado em "text"   ← use este
            → chatting  → (nova fala reabre a rodada; last_text persiste)
```

| Status | Significado | Ação |
|---|---|---|
| `thinking` | LLM processando em background | aguarde, faça polling |
| `chatting` | IA respondeu em `reply`; aguardando o humano | mostre `reply`, colete a próxima fala |
| `confirmed` | Humano aprovou; texto final em `text` | **envie `text` ao agente** |

- `last_text`: último texto aprovado — persiste mesmo se a conversa seguir.
- A conversa **não para** no `confirmed`: o humano pode pedir ajustes ("muda
  o tom") e um novo `confirmed` atualiza `text`.

## Endpoints

### POST `/api/chat/start` — abre a sessão

```json
{ "objective": "fala de abertura do episódio 5", "context": "podcast de tecnologia, tom informal" }
```
→ `200`
```json
{ "session_id": "abc123def456", "status": "thinking" }
```
- `objective` (obrigatório): o que a fala precisa comunicar
- `context` (opcional): qualquer informação útil — nomes, datas, público, tom

### POST `/api/chat/{session_id}` — envia a fala do humano

```json
{ "message": "o nome da professora é Letícia" }
```
→ `200 { "ok": true, "status": "thinking" }`

Erros: `404` sessão expirou/inexistente · `409` IA ainda processando a fala
anterior (aguarde ~2s e reenvie) · `400` message vazio

### GET `/api/chat/{session_id}` — estado da sessão

→ `200`
```json
{
  "session_id": "abc123",
  "status": "chatting",
  "reply": "Ok, atualizei com Letícia. Fica assim?",
  "text": "",
  "last_text": "Comunicamos que no dia 31...",
  "error": "",
  "messages": [ { "role": "user" | "assistant", "content": "..." }, ... ]
}
```

### DELETE `/api/chat/{session_id}` — encerra

→ `200 { "ok": true }`

## Entrada por voz (STT)

O humano fala; o Dudes grava e envia o áudio **antes** de conversar:

```sh
curl -X POST $BASE/api/transcribe -H "X-API-Key: $KEY" \
  -F "audio=@fala.webm" -F "source_lang=pt"
# → {"text": "o nome da professora é Letícia", "language": "pt", ...}
```

Formatos aceitos: webm, mp4/m4a, mp3, ogg, flac, wav. O servidor decodifica,
denoiseia (ffmpeg afftdn) e valida com Silero VAD antes do Whisper — ruído
puro devolve texto vazio.

## Síntese com a voz do agente

```sh
curl -X POST $BASE/v1/audio/speech -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "tts-1", "input": "TEXTO_APROVADO", "voice": "ID_DA_VOZ", "response_format": "wav"}'
```

## Preprompt (instruções da IA)

```sh
# ler o atual e o padrão
curl $BASE/api/settings -H "X-API-Key: $KEY"
# → { ..., "chat_system": "<efetivo>", "chat_system_default": "<built-in>" }

# trocar (ex.: dar contexto dos agentes ao LLM)
curl -X POST $BASE/api/settings -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"chat_system": "Você é o assistente do agente X. Ajude a decidir a fala. ..."}'
```

- Vazio = usa o prompt built-in (decidir texto, perguntar, confirmar)
- O preprompt é lido a cada turno — mudanças valem na hora, sem reinício

## Tratamento de erros

| HTTP | Causa | Ação |
|---|---|---|
| 400 | campo faltando / áudio vazio | corrija a requisição |
| 401 | chave ausente ou inválida | confira a X-API-Key |
| 404 | sessão expirou (TTL 1h) | abra outra com `start` |
| 409 | IA ainda processando a fala anterior | aguarde ~2s e reenvie |
| 413 | áudio > limite | grave um trecho menor |
| 502 | provedor do LLM fora do ar | aguarde e tente de novo |

## Exemplo completo (Node.js / fetch)

```js
const BASE = "https://SEU-DOMINIO.com/ttsproxy";
const KEY = process.env.TTS_KEY;

const api = async (path, opts = {}) => {
  const r = await fetch(BASE + path, {
    ...opts,
    headers: { "X-API-Key": KEY, "Content-Type": "application/json", ...opts.headers },
  });
  if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
  return r.json();
};

async function conversar(objective, falasDoHumano) {
  const { session_id: sid } = await api("/api/chat/start", {
    method: "POST", body: JSON.stringify({ objective }),
  });

  for (const fala of falasDoHumano) {
    await api(`/api/chat/${sid}`, {
      method: "POST", body: JSON.stringify({ message: fala }),
    });
    for (;;) {
      await new Promise(r => setTimeout(r, 1500));
      const d = await api(`/api/chat/${sid}`);
      if (d.status === "thinking") continue;
      if (d.error) throw new Error(d.error);
      if (d.status === "confirmed") return d.text;   // ✅ texto final
      console.log("IA:", d.reply);                    // mostre ao humano
      break;
    }
  }
}

// uso:
// const texto = await conversar("fala de abertura do episódio 5",
//   ["o nome da professora é Letícia", "pode mandar"]);
```

## Acesso pela internet

```
BASE = https://SEU-DOMINIO.com/ttsproxy
```
Mesma chave, mesmos endpoints — Cloudflare + túnel SSH já configurados.
O áudio sintetizado (`/v1/audio/speech`) também está disponível pelo proxy.
