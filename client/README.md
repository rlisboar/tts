# TTS-Rod · Roteador de microfone

Janela com um botão que alimenta um **microfone virtual** (BlackHole). Os apps de
chamada (Zoom, Meet, Discord, OBS…) selecionam esse mic virtual como entrada e
você troca a fonte só clicando no botão — sem mexer nas configs da chamada:

- **Voz real** (padrão): o microfone real passa direto pro mic virtual.
- **Voz do app**: o cliente cala a boca e quem alimenta o mic virtual é o
  navegador do TTS-Rod (voz traduzida), com a saída de áudio dele apontada pro
  `BlackHole 2ch`.

Ao trocar de modo, o cliente avisa o servidor; o navegador (em polling) **silencia
o TTS** quando você volta pra "Voz real" — assim os dois áudios não se somam.

> **Onde rodar:** o cliente, o BlackHole, a chamada **e o navegador** rodam na
> MESMA máquina (é onde o áudio do TTS toca). O servidor pode estar noutra (ex.:
> o Mac com a GPU). O `config.json` dentro do zip já vem com o endereço e a chave
> do servidor de onde você baixou — nada pra configurar à mão. O rótulo
> "Servidor: …" na janela mostra **✓** quando o aviso de modo chega no servidor.
> **O zip contém sua chave da API — não compartilhe** (baixe só na sua rede).

## 1. Instalar o microfone virtual (uma vez)

```sh
brew install blackhole-2ch
```

Depois reinicie os apps de áudio (ou faça logout/login). Vai aparecer um
dispositivo **"BlackHole 2ch"** como entrada **e** saída.

## 2. Apontar o TTS-Rod pro mic virtual

No navegador do TTS-Rod, em **Configurações → Saída de som**, escolha
**BlackHole 2ch**. A voz traduzida do tradutor/modificador passa a tocar no mic
virtual. **Recarregue a aba** depois de baixar/atualizar (o JS do gate é novo).

> Para você TAMBÉM se ouvir enquanto o app fala, crie um **Dispositivo de Saída
> Múltipla** no *Configuração de Áudio e MIDI* (BlackHole 2ch + seus fones) e
> selecione-o como saída no navegador.

## 3. Abrir o roteador

Dois cliques em **`run.command`** (1ª vez: clique direito → Abrir). Ele cria o
ambiente, instala as dependências e abre a janela.

Na janela:
- **Microfone real**: seu mic físico (fonte quando em "Voz real").
- **Microfone virtual**: `BlackHole 2ch` (destino — já vem pré-selecionado).
- **Botão grande**: alterna Voz real ⇄ Voz do app.

## 4. Na chamada

Selecione **"BlackHole 2ch"** como microfone no Zoom/Meet/Discord. Pronto:
clique no botão pra falar como você mesmo ou como a voz do app.

## Notas

- Em "Voz real" o navegador silencia o TTS automaticamente (latência ~⅓ s do
  polling). Mesmo assim, evite disparar TTS enquanto fala como você mesmo.
- Mic real e BlackHole rodam a 48 kHz num stream duplex (sem drift de clock). Se
  seu mic não suportar 48 kHz, troque a fonte ou ajuste `SR` em `mic_router.py`.
- Servidor errado no rótulo? Sobrescreva com variáveis de ambiente:
  `TTS_ROD_URL=http://IP:7860 TTS_ROD_API_KEY=suachave ./run.command`.
- Requisitos: macOS, Python 3.10+ (tkinter incluso), `sounddevice`, `numpy`.
