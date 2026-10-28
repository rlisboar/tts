# Bake-offs de áudio (identidade, ruído, anti-clone)

Scripts de bancada dos cards #520/#521/#522/#523. **Nada aqui entra no boot do
app** e nenhum deles toca `voices/`, `outputs/` ou `.speaker-profiles.json`: tudo
mora debaixo de `BAKEOFF_DIR` (padrão `/tmp/tts-bakeoff`).

## Corpus compartilhado (#520 e #521)

```bash
./.venv-mlx/bin/python tools/bakeoff/corpus.py publico     # LibriSpeech dev-clean (CC BY 4.0)
./.venv-mlx/bin/python tools/bakeoff/corpus.py sintetico   # TTS do próprio app (pt)
./.venv-mlx/bin/python tools/bakeoff/corpus.py monta       # misturas em SNR 0/5/10/20
```

`resultados/corpus.json` é o manifesto com sha256 de cada arquivo — é o que torna
a evidência auditável. A semente é fixa (`20261028`): mesma máquina e mesma
semente, mesmos arquivos.

**Ruído é sintético de propósito** (`tools/bakeoff/ruido.py`): sem download, e
cada família tem a assinatura espectral do problema real (ventoinha = passa-baixa
+ tom de rotação; teclado = transientes; TV = babble). A ressalva de que ruído
sintético não é a ventoinha do dono vai em toda evidência.

## Licenças

Ver `LICENCAS.md`. O corpus público é CC BY 4.0 (LibriSpeech); os pesos de cada
candidato têm licença própria e estão listados lá — conferir antes de promover
qualquer um a dependência do app.

## Ambiente de bancada

`venv` separado (`/tmp/tts-bakeoff/venv`), **sem pino novo** no `requirements.txt`
nesta fase.
