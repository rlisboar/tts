# `models/` — pesos que o app usa, versionados por sha256

Aqui ficam os modelos que o app **não** baixa em tempo de execução. O boot sobe sem
eles e cai no fallback atual **com aviso** (mesmo padrão do RNNoise): modelo
ausente nunca derruba o servidor, mas também nunca é buscado sozinho.

| Diretório | Papel | Licença |
|---|---|---|
| `ecapa-voxceleb/` | extrator de locutor (identidade de voz, #521/#525) | Apache-2.0 (código) / CC BY 4.0 (pesos) — ver `NOTICE` |

Artefatos ao lado:
- `asnorm-coorte-ecapa.npy` / `.json` — coorte de impostores do AS-norm do ECAPA,
  80 vetores de 40 locutores do corpus público (LibriSpeech dev-clean, CC BY 4.0).
  **É específica do modelo**: trocar o extrator exige refazer a coorte, senão o
  AS-norm passa a normalizar pelo lugar errado (no Resemblyzer ele piora: 1,9 % →
  5,0 % de EER em 1 s).
- `sha256sums.txt` — sha256 de cada arquivo de peso/artefato. Divergência é ERRO.

## Como materializar

```sh
./tools/baixa_modelos.sh          # baixa só o que falta e confere o resto
./tools/baixa_modelos.sh --check  # não baixa: só verifica (é o que o teste roda)
./tools/baixa_modelos.sh --forca  # rebaixa o que estiver divergente
```

A coorte não é baixável (é gerada): se o `.npy` faltar, o script diz o comando —
`tools/coorte_asnorm.py`, que precisa do venv da bancada (`speechbrain`).

## Contrato de ambiente (quem lê é o app)

| Variável | Aponta para |
|---|---|
| `TTS_SPEAKER_MODELO_DIR` | `models/ecapa-voxceleb` |
| `TTS_ASNORM_COORTE` | `models/asnorm-coorte-ecapa.npy` |

Sem `TTS_SPEAKER_MODELO_DIR` (ou com o diretório incompleto) o app usa o extrator
anterior; sem `TTS_ASNORM_COORTE` usa o escore cru. Os dois casos são AVISO no log,
nunca exceção.

## O que NÃO é baixado aqui

- corpus do bake-off (`tools/bakeoff/corpus.py`, só para reproduzir medição);
- qualquer coisa em tempo de boot: `run.sh`/`app.py` não chamam este diretório
  para baixar.
