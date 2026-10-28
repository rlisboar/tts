#!/bin/zsh
# Baixa/verifica os modelos que o app usa (#526) — NUNCA roda no boot.
#
# O app sobe sem modelo e cai no fallback atual com aviso (mesmo padrão do
# RNNoise); quem materializa os pesos é este script, e ele é idempotente:
#
#   ./tools/baixa_modelos.sh            # baixa só o que falta e confere o resto
#   ./tools/baixa_modelos.sh --check    # não baixa nada; só verifica sha256
#   ./tools/baixa_modelos.sh --forca    # rebaixa o que estiver divergente
#
# Regras:
#   • todo arquivo tem sha256 em `models/sha256sums.txt`; divergência é ERRO (não
#     sobrescreve em silêncio — peso trocado muda o veredito do bake-off);
#   • o que é gerado localmente (coorte do AS-norm) NÃO vem da internet: o script
#     diz o comando e falha se o artefato faltar;
#   • o que é DERIVADO de outro arquivo versionado (`label_encoder.ckpt`, cópia do
#     `label_encoder.txt`) o próprio script materializa e confere o sha256 — sem
#     ele o load offline morre no `Pretrainer` (#598);
#   • `--check` REPROVA quando falta arquivo (antes saía 0 com tudo faltando);
#   • a coorte é específica do modelo — trocar o extrator exige refazê-la.
set -u
RAIZ="${0:A:h:h}"
cd "$RAIZ" || exit 1
SUMS="models/sha256sums.txt"
# Base das URLs: `TTS_MODELOS_URL` permite servir um espelho (é o que o teste faz
# com um http.server local — nada de rede no CI).
BASE_URL="${TTS_MODELOS_URL:-https://huggingface.co}"
FORCA=0
CHECK=0
while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK=1; shift ;;
    --forca) FORCA=1; shift ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) print -r -- "opção desconhecida: $1" >&2; exit 2 ;;
  esac
done

# arquivo → origem (repo HF + revisão). Gerado localmente fica com "-".
# Formato: caminho|repo|revisao
FONTES=(
  "models/ecapa-voxceleb/hyperparams.yaml|speechbrain/spkrec-ecapa-voxceleb|0f99f2d0ebe89ac095bcc5903c4dd8f72b367286"
  "models/ecapa-voxceleb/embedding_model.ckpt|speechbrain/spkrec-ecapa-voxceleb|0f99f2d0ebe89ac095bcc5903c4dd8f72b367286"
  "models/ecapa-voxceleb/mean_var_norm_emb.ckpt|speechbrain/spkrec-ecapa-voxceleb|0f99f2d0ebe89ac095bcc5903c4dd8f72b367286"
  "models/ecapa-voxceleb/classifier.ckpt|speechbrain/spkrec-ecapa-voxceleb|0f99f2d0ebe89ac095bcc5903c4dd8f72b367286"
  "models/ecapa-voxceleb/label_encoder.txt|speechbrain/spkrec-ecapa-voxceleb|0f99f2d0ebe89ac095bcc5903c4dd8f72b367286"
  "models/mossformer2-ss-16k/last_best_checkpoint.pt|alibabasglab/MossFormer2_SS_16K|407cb030cd66340918ebb6c8cc63b18f8592cdbe"
  "models/dfn3-onnx/deepfilter.onnx|soniqo/DeepFilterNet3-ONNX|63d8ba442ba900143c468b798e94a04009b2f0c9"
  "models/dfn3-onnx/config.json|soniqo/DeepFilterNet3-ONNX|63d8ba442ba900143c468b798e94a04009b2f0c9"
  "models/asnorm-coorte-ecapa.npy|-|-"
)
# Arquivos DERIVADOS de outro arquivo versionado (não vêm da internet): o script
# copia e confere o sha256. `label_encoder.ckpt` é um deles porque o `Pretrainer`
# do speechbrain busca `<savedir>/label_encoder.ckpt` no load e, ausente, cai no
# Hub — mesmo com `savedir` (medido: `LocalEntryNotFoundError` offline, #598).
# Formato: caminho|origem local
DERIVADOS=(
  "models/ecapa-voxceleb/label_encoder.ckpt|models/ecapa-voxceleb/label_encoder.txt"
)

# portátil: o job do CI é Linux (`sha256sum`), a máquina de trabalho é macOS
# (`shasum`). Antes o caminho cravado em /usr/bin quebrava no CI (#598).
if command -v sha256sum >/dev/null 2>&1; then
  sha_de() { sha256sum "$1" 2>/dev/null | awk '{print $1}'; }
else
  sha_de() { shasum -a 256 "$1" 2>/dev/null | awk '{print $1}'; }
fi
# `.tmp` POR EXECUÇÃO: nome fixo fazia duas rodadas simultâneas atropelarem o
# arquivo uma da outra (#598).
TMP_SUF="$$.$RANDOM"
esperado() { awk -v alvo="$1" '$2 == alvo {print $1}' "$SUMS"; }

falhas=0
faltando=()
for linha in "${FONTES[@]}"; do
  arq="${linha%%|*}"; resto="${linha#*|}"; repo="${resto%%|*}"; rev="${resto#*|}"
  alvo="$(esperado "$arq")"
  if [ -z "$alvo" ]; then
    print -r -- "  [FALHA] $arq sem sha256 em $SUMS"; falhas=1; continue
  fi
  if [ -f "$arq" ]; then
    atual="$(sha_de "$arq")"
    if [ "$atual" = "$alvo" ]; then
      print -r -- "  [ok] $arq (sha256 confere)"
      continue
    fi
    if [ "$FORCA" = 0 ] || [ "$repo" = "-" ]; then
      print -r -- "  [FALHA] $arq divergente: ${atual[1,12]}… ≠ ${alvo[1,12]}…"
      [ "$repo" = "-" ] && print -r -- "          (gerado localmente: rode tools/coorte_asnorm.py)"
      falhas=1; continue
    fi
    print -r -- "  [força] $arq divergente — rebaixando"
  fi
  if [ "$CHECK" = 1 ]; then
    faltando+=("$arq"); continue
  fi
  if [ "$repo" = "-" ]; then
    print -r -- "  [FALHA] $arq falta e não é baixável — regenere com tools/coorte_asnorm.py"
    falhas=1; continue
  fi
  mkdir -p "${arq:h}"
  url="$BASE_URL/$repo/resolve/$rev/${arq:t}"
  print -r -- "  baixando ${arq:t} de $repo@${rev[1,8]}…"
  if ! curl -fsSL --retry 2 -o "$arq.$TMP_SUF" "$url"; then
    print -r -- "  [FALHA] download de $arq falhou"; rm -f "$arq.$TMP_SUF"; falhas=1; continue
  fi
  atual="$(sha_de "$arq.$TMP_SUF")"
  if [ "$atual" != "$alvo" ]; then
    print -r -- "  [FALHA] $arq baixado com sha256 ${atual[1,12]}… ≠ esperado ${alvo[1,12]}…"
    rm -f "$arq.$TMP_SUF"; falhas=1; continue
  fi
  mv "$arq.$TMP_SUF" "$arq"
  chmod 444 "$arq" 2>/dev/null   # peso é imutável na árvore, como os já materializados
  print -r -- "  [ok] $arq baixado e conferido"
done

for linha in "${DERIVADOS[@]}"; do
  arq="${linha%%|*}"; origem="${linha#*|}"
  alvo="$(esperado "$arq")"
  if [ -z "$alvo" ]; then
    print -r -- "  [FALHA] $arq sem sha256 em $SUMS"; falhas=1; continue
  fi
  if [ -f "$arq" ] && [ "$(sha_de "$arq")" = "$alvo" ]; then
    print -r -- "  [ok] $arq (derivado, sha256 confere)"; continue
  fi
  if [ -f "$arq" ] && [ "$FORCA" = 0 ]; then
    atual="$(sha_de "$arq")"
    print -r -- "  [FALHA] $arq divergente da origem: ${atual[1,12]}… ≠ ${alvo[1,12]}…"
    print -r -- "          (derivado: use --forca para refazer de $origem)"
    falhas=1; continue
  fi
  if [ ! -f "$origem" ]; then
    print -r -- "  [FALHA] $arq falta e a origem ($origem) também"; falhas=1; continue
  fi
  if [ "$CHECK" = 1 ]; then faltando+=("$arq"); continue; fi
  print -r -- "  derivando ${arq:t} de ${origem:t}"
  if ! cp "$origem" "$arq.$TMP_SUF" || ! chmod 444 "$arq.$TMP_SUF"; then
    print -r -- "  [FALHA] não consegui derivar $arq"; rm -f "$arq.$TMP_SUF"; falhas=1; continue
  fi
  atual="$(sha_de "$arq.$TMP_SUF")"
  if [ "$atual" != "$alvo" ]; then
    print -r -- "  [FALHA] $arq derivado com sha256 ${atual[1,12]}… ≠ esperado ${alvo[1,12]}…"
    rm -f "$arq.$TMP_SUF"; falhas=1; continue
  fi
  mv "$arq.$TMP_SUF" "$arq"
  print -r -- "  [ok] $arq derivado e conferido"
done

if [ "${#faltando[@]}" -gt 0 ]; then
  print -r -- ""
  print -r -- "faltando (modo --check não baixa):"
  print -r -- "  ${faltando[*]}"
  falhas=1     # `--check` com arquivo faltando é REPROVAÇÃO (#598)
fi
[ "$falhas" = 0 ] || exit 1
print -r -- ""
print -r -- "modelos prontos. O app NÃO baixa sozinho: aponte os caminhos por ambiente"
print -r -- "(TTS_SPEAKER_MODELO_DIR / TTS_ASNORM_COORTE) — ver models/README.md."
