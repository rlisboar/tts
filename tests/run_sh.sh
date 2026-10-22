#!/usr/bin/env bash
# Runner das suítes .sh (#387). Antes dele NENHUMA rodava no gate: as provas de
# XSS, key-storage, barge e WS só existiam se alguém lembrasse, e uma quebra
# entrava com o gate verde.
#
# Três estágios, separados por DEPENDÊNCIA — a separação existe para nenhum deles
# fingir cobertura que não tem:
#
#   rapido     sem navegador, auto-contido. É o que roda no hook (<=2 min): invariante
#              do harness (limpa_bytecode), trava de modelo e a prova de lock do Live.
#   navegador  auto-contido, mas precisa de Chromium/Playwright. Cobre UI de verdade
#              (XSS, key-storage, barge, WS). ~4 min; rode no gate que mexe em tela.
#   instancia  NÃO sobe servidor: precisa de app de pé (BASE=...). Está fora do hook
#              de propósito — exige MLX carregado e ninguém deve subir app por commit.
#
# Uso:
#   ./tests/run_sh.sh rapido               # hook
#   ./tests/run_sh.sh navegador            # gate que mexe em static/ ou client/
#   BASE=http://IP:7860 ./tests/run_sh.sh instancia     # com o app de pé
#   ./tests/run_sh.sh lista                # o que cada estágio tem
#   ./tests/run_sh.sh rapido -j 1          # serial
#
# Paralelo-seguro por construção: cada suíte escreve o log num diretório próprio
# (`mktemp -d`), nunca em nome fixo de /tmp — duas rodadas simultâneas não se
# truncam. Cada suíte tem TETO próprio: suíte pendurada falha, não prende o gate.
set -uo pipefail

AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RAIZ="$(dirname "$AQUI")"
TETO="${TETO_S:-240}"          # segundos por suíte
J=3

RAPIDO=(limpa_bytecode serial_lock live_lock_freeze)
NAVEGADOR=(dsh_ui hash_deep_link live_dsh_fallback live_barge_rep live_ui obs_ui live_ws
            ultima_chave_ui)
INSTANCIA=(a11y_teclado admin_ui_flow cfg_sujo_e_erros conversa_429 key_storage_repro
           live_hold_ui mic_erro_ui mobile_layout onboarding_atalhos ui_repouso
           xss_frontend_repro)

estagio="${1:-rapido}"
shift || true
while [ $# -gt 0 ]; do
    case "$1" in
        -j) J="$2"; shift 2 ;;
        *) echo "arg desconhecido: $1" >&2; exit 2 ;;
    esac
done

case "$estagio" in
    rapido)    ALVOS=("${RAPIDO[@]}") ;;
    navegador) ALVOS=("${NAVEGADOR[@]}") ;;
    instancia) ALVOS=("${INSTANCIA[@]}") ;;
    tudo)      ALVOS=("${RAPIDO[@]}" "${NAVEGADOR[@]}" "${INSTANCIA[@]}") ;;
    lista)
        printf 'rapido:    %s\n' "${RAPIDO[*]}"
        printf 'navegador: %s\n' "${NAVEGADOR[*]}"
        printf 'instancia: %s\n' "${INSTANCIA[*]}"
        exit 0 ;;
    *) echo "estágio inválido: $estagio (rapido|navegador|instancia|tudo|lista)" >&2; exit 2 ;;
esac

# ── instância: falha com o motivo, não com N vermelhos ───────────────────────────
if [ "$estagio" = "instancia" ] || [ "$estagio" = "tudo" ]; then
    BASE="${BASE:-http://127.0.0.1:7860}"
    if ! "$RAIZ/.venv-mlx/bin/python" - "$BASE" <<'PY'
import socket, sys, urllib.parse, urllib.request
url = urllib.parse.urlsplit(sys.argv[1])
try:
    with urllib.request.urlopen(url._replace(path="/health").geturl(), timeout=3) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
PY
    then
        echo "sem app de pé em ${BASE:-http://127.0.0.1:7860} — suba ./run.sh (ou passe BASE=)" >&2
        echo "as suítes 'instancia' não têm como rodar sem ele; 'rapido' e 'navegador' rodam." >&2
        [ "$estagio" = "tudo" ] && ALVOS=("${RAPIDO[@]}" "${NAVEGADOR[@]}")
        [ "$estagio" = "instancia" ] && exit 1
    fi
fi

DIR="$(mktemp -d "${TMPDIR:-/tmp}/suites-sh.XXXXXX")"
trap 'rm -rf "$DIR"' EXIT

# teto portátil: o `timeout` do coreutils NÃO existe no macOS; o alarme do perl sim.
_com_teto() {
    perl -e 'alarm shift; exec @ARGV' "$TETO" "$@"
}

declare -a FALHAS=() RODADAS=()
comeca=$(date +%s)

for nome in "${ALVOS[@]}"; do
    while [ "$(jobs -rp | wc -l | tr -d ' ')" -ge "$J" ]; do wait -n 2>/dev/null || break; done
    RODADAS+=("$nome")
    (
        # CWD na raiz: várias suítes leem `static/index.html` e afins por caminho
        # relativo (o `mic_erro_ui.sh` dava FileNotFoundError chamado de fora da
        # raiz, mesmo tendo `RAIZ` em mãos). O runner não pode depender de onde o
        # chamador estava.
        cd "$RAIZ" || exit 1
        inicio=$(date +%s)
        BASE="${BASE:-http://127.0.0.1:7860}" _com_teto bash "$AQUI/$nome.sh" >"$DIR/$nome.log" 2>&1
        rc=$?
        echo "$rc $nome $(( $(date +%s) - inicio ))"
    ) >>"$DIR/resultado" &
done
wait

while read -r rc nome dur; do
    if [ "$rc" -eq 0 ]; then
        printf '  ok    %-24s %ss\n' "$nome" "$dur"
    else
        printf '  FALHA %-24s %ss (rc=%s)\n' "$nome" "$dur" "$rc"
        FALHAS+=("$nome")
    fi
done < <(sort -k2 "$DIR/resultado")

echo "--- $estagio: ${#FALHAS[@]} falha(s) de ${#RODADAS[@]} suíte(s) em $(( $(date +%s) - comeca ))s"

if [ "${#FALHAS[@]}" -gt 0 ]; then
    for nome in "${FALHAS[@]}"; do
        echo
        echo "===== $nome (últimas 30 linhas; log completo em $DIR/$nome.log) ====="
        tail -30 "$DIR/$nome.log"
    done
    # o diretório sai do trap: copia o que falhou para um lugar previsível
    destino="${TMPDIR:-/tmp}/suites-sh-falhas.$$"
    mkdir -p "$destino"
    for nome in "${FALHAS[@]}"; do cp "$DIR/$nome.log" "$destino/" 2>/dev/null; done
    # Poda: um teste por rodada, e rodada vermelha acumulava um diretório por vez
    # (18 no /tmp quando isso foi medido — #428). Fica o dos últimos 3 vermelhos.
    ls -1dt "${TMPDIR:-/tmp}"/suites-sh-falhas.* 2>/dev/null | tail -n +4 | xargs rm -rf 2>/dev/null || true
    echo
    echo "logs preservados em $destino (mantidos os 3 últimos vermelhos)"
    exit 1
fi
