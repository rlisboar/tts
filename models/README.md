# `models/` — pesos que o app usa, versionados por sha256

Aqui ficam os modelos que o app **não** baixa em tempo de execução. O boot sobe sem
eles e cai no fallback atual **com aviso** (mesmo padrão do RNNoise): modelo
ausente nunca derruba o servidor, mas também nunca é buscado sozinho.

| Diretório | Papel | Licença |
|---|---|---|
| `ecapa-voxceleb/` | extrator de locutor (identidade de voz, #521/#525) | Apache-2.0 (código) / CC BY 4.0 (pesos) — ver `NOTICE` |
| `mossformer2-ss-16k/` | separação de dois locutores (alvo × concorrente, #532/#526-bis) | Apache-2.0 (código vendorizado em `tools/separa_vend/` e pesos) — ver `NOTICE` |
| `dfn3-onnx/` | redução de ruído da limpeza de amostra (#523, grafo ONNX — sem `df`/torch) | MIT — ver `NOTICE` |

Artefatos ao lado:
- `asnorm-coorte-ecapa.npy` / `.json` — coorte de impostores do AS-norm do ECAPA,
  80 vetores de 40 locutores do corpus público (LibriSpeech dev-clean, CC BY 4.0).
  **É específica do modelo**: trocar o extrator exige refazer a coorte, senão o
  AS-norm passa a normalizar pelo lugar errado (no Resemblyzer ele piora: 1,9 % →
  5,0 % de EER em 1 s).
- `sha256sums.txt` — sha256 de cada arquivo de peso/artefato. Divergência é ERRO.
  O que o script materializa sai em `444` (peso é imutável na árvore): para
  inspecionar/substituir à mão, `chmod 644` antes — o teste que injeta divergência
  faz isso e a falta do `chmod` foi um vermelho real do hook (#630).
  Os `.ckpt` NÃO estão no git (83 MB de um só): quem os materializa é o script,
  conferindo o sha256. `label_encoder.ckpt` é DERIVADO do `label_encoder.txt`
  (cópia byte a byte, mesmo sha256) e TAMBÉM está no manifesto: o `Pretrainer` do
  speechbrain busca `<savedir>/label_encoder.ckpt` no load e, sem ele, cai no Hub
  — mesmo com `savedir` e `source` locais (offline quebra, #598).

## Como materializar

```sh
./tools/baixa_modelos.sh          # baixa só o que falta (e deriva o label_encoder)
./tools/baixa_modelos.sh --check  # não baixa: só verifica — FALHA se faltar algo
./tools/baixa_modelos.sh --forca  # rebaixa/refaz o que estiver divergente
```

A coorte não é baixável (é gerada): se o `.npy` faltar, o script diz o comando —
`tools/coorte_asnorm.py`, que precisa do venv da bancada (`speechbrain`). O
`label_encoder.ckpt`, por outro lado, o próprio script gera: é `cp` do
`label_encoder.txt` versionado (sem rede), com o sha256 conferido depois.

Quem chama o extrator (app e `tools/bakeoff/`) passa `source` **e** `savedir` no
diretório acima: é o par que mantém o load fora do cache do HuggingFace. A célula
`tests/test_ecapa_offline.py` roda o load com `HF_HOME` vazio e
`HF_HUB_OFFLINE=1` de propósito.

## Contrato de ambiente (quem lê é o app)

| Variável | Aponta para |
|---|---|
| `TTS_SPEAKER_MODELO_DIR` | `models/ecapa-voxceleb` |
| `TTS_ASNORM_COORTE` | `models/asnorm-coorte-ecapa.npy` |
| `TTS_SEPARA_MODELO_DIR` | `models/mossformer2-ss-16k` |
| `TTS_DFN3_DIR` | `models/dfn3-onnx` |

O separador (`last_best_checkpoint.pt`, 670 MB) segue o mesmo contrato do ECAPA:
sha256 no manifesto, `.pt` fora do git, e quem materializa é o script. O código da
rede é **vendorizado** em `tools/separa_vend/` (revisão fixada, NOTICE e sha256 por
arquivo no `FONTE.txt` próprio) — as únicas dependências são `torch` (do lock) e
`einops`/`rotary_embedding_torch`. O load é local: com `HF_HOME` vazio e
`HF_HUB_OFFLINE=1` funciona, e sem os pesos o app/CLI cai na seleção por janela.

Sem `TTS_SPEAKER_MODELO_DIR` (ou com o diretório incompleto) o app usa o extrator
anterior; sem `TTS_ASNORM_COORTE` usa o escore cru. Os dois casos são AVISO no log,
nunca exceção.

## O que NÃO é baixado aqui

- corpus do bake-off (`tools/bakeoff/corpus.py`, só para reproduzir medição);
- qualquer coisa em tempo de boot: `run.sh`/`app.py` não chamam este diretório
  para baixar.

## Custo medido (M3, CPU, `HF_HUB_OFFLINE=1`, 3 rodadas, mediana)

| Cenário | RSS | Tempo |
|---|---|---|
| `import app` sem extrator | 60 MB | 0,4 s |
| extrator carregado + 1 embedding | 517 MB | 0,88 s de carga · 0,03 s por embedding |

Ou seja: o ECAPA custa ~**460 MB de RSS** e ~0,5 s de carga acima do boot atual —
caro para o Live, aceitável para o cadastro/verificação. Roda em CPU: não disputa
o Metal do TTS. Quem ligar o extrator deve carregá-lo sob demanda (primeiro uso),
não no boot do servidor.
