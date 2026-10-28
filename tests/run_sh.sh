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
#              NÃO encosta no app do dono: BASE e os escapes (`*_NO_DONO`) são
#              exportados SÓ no `instancia`, então cada suíte aqui sobe o próprio
#              servidor. Antes o `:7860` com escrita liberada vazava para cá e uma
#              rodada reescreveu o `settings.json` do dono (#535).
#   instancia  NÃO sobe servidor: precisa de app de pé (BASE=...). Está fora do hook
#              de propósito — exige MLX carregado e ninguém deve subir app por commit.
#              REGRA do estágio: LEITURA por padrão. Suíte daqui que ESCREVA no app
#              alvo (arquivo, setting, chave, item de histórico) precisa de trap de
#              restauração (`|| true` em cada comando — ver #446), senão suja o
#              estado do dono quando a rodada falha. Hoje escreve: `xss_frontend_repro`
#              (vozes) e `onboarding_atalhos` (output do histórico); as outras oito
#              são somente leitura.
#              Suíte que sobe o PRÓPRIO servidor não fica aqui: vai em `navegador`.
#
# Uso:
#   ./tests/run_sh.sh rapido               # hook
#   ./tests/run_sh.sh navegador            # gate que mexe em static/ ou client/
#   BASE=http://IP:7860 ./tests/run_sh.sh instancia     # com o app de pé
#   ./tests/run_sh.sh lista                # o que cada estágio tem
#   ./tests/run_sh.sh rapido -j 1          # serial
#   SUITES="x y" ./tests/run_sh.sh navegador   # roda só essas (nome do estágio
#                                              # continua valendo p/ a decisão de BASE)
#   TTS_RUN_SH_PULA_PRECHECK=1 BASE=… ./tests/run_sh.sh instancia   # sem o app no ar
#
# Paralelo-seguro por construção: cada suíte escreve o log num diretório próprio
# (`mktemp -d`), nunca em nome fixo de /tmp — duas rodadas simultâneas não se
# truncam. Cada suíte tem TETO próprio: suíte pendurada falha, não prende o gate.
set -uo pipefail

AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RAIZ="$(dirname "$AQUI")"
# 90 s: o hook HERDA este teto, e 240 s por suíte pendurada prendiam o commit por
# minutos. Suíte que passa de 90 s de verdade (as de Live ficam perto de 40 s) usa
# `TETO_S=…` para si; pendurada agora falha rápido em vez de segurar o gate.
TETO="${TETO_S:-90}"           # segundos de TRABALHO por suíte (depois da trava)
# Teto de FILA: o tempo esperando a trava de modelo não pode comer o teto do
# trabalho (com -j 3 e 8 suítes de modelo, as últimas morriam no alarme antes de
# rodar — #535). Quem segura a fila é o `serial_pega` (`TTS_SERIAL_ESPERA`); este
# é só o limite duro para suíte que trava ANTES de pedir a trava.
TETO_FILA="${TETO_FILA_S:-900}"
J=3

RAPIDO=(limpa_bytecode pendurada_probe serial_lock live_lock_freeze)
# Estágio é por DEPENDÊNCIA, não por "tem navegador": quem sobe o PRÓPRIO servidor
# é auto-contido e fica em `navegador` — mandá-lo para `instancia` o tiraria de
# quem não tem app no ar. `admin_ui_flow` e `cfg_sujo_e_erros` passaram a subir o
# próprio servidor (#437/#466) e vieram para cá.
NAVEGADOR=(dsh_ui hash_deep_link live_dsh_fallback live_barge_rep live_ui
            live_interrupcao_ui obs_ui live_ws ultima_chave_ui admin_ui_flow cfg_sujo_e_erros
            voz_desfazer_ui)
INSTANCIA=(a11y_teclado conversa_429 key_storage_repro live_hold_ui mic_erro_ui
           mobile_layout onboarding_atalhos ui_repouso xss_frontend_repro)

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
    if [ "${TTS_RUN_SH_PULA_PRECHECK:-0}" = 1 ]; then
        :
    elif ! "$RAIZ/.venv-mlx/bin/python" - "$BASE" <<'PY'
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

# `SUITES="a b"` troca a lista do estágio (é como o teste roda uma suíte-sonda sem
# tocar na lista real). O estágio passado continua mandando na decisão de BASE.
if [ -n "${SUITES:-}" ]; then read -r -a ALVOS <<< "$SUITES"; fi

# Caminho da suíte: nome puro vive em `tests/`, caminho absoluto é usado como veio.
_arquivo_da_suite() { case "$1" in /*) printf '%s' "$1" ;; *) printf '%s/%s.sh' "$AQUI" "$1" ;; esac; }
# BASE/escapes são do `instancia` (único estágio que pressupõe app de pé). Em
# `tudo`, só as suítes de `instancia` recebem — as de `navegador` seguem limpas.
_e_de_instancia() { case " ${INSTANCIA[*]} " in *" $1 "*) return 0 ;; esac; return 1; }
# Quem pede a trava de modelo re-arma o teto REAL ao obtê-la (serial.sh).
_toma_a_trava() { grep -qs 'serial\.sh' "$(_arquivo_da_suite "$1")"; }

DIR="$(mktemp -d "${TMPDIR:-/tmp}/suites-sh.XXXXXX")"
trap 'rm -rf "$DIR"' EXIT

# teto portátil: o `timeout` do coreutils NÃO existe no macOS; o alarme do perl sim.
# `$1` é o teto (o do trabalho ou o da fila) — o valor não pode ser fixo aqui.
_com_teto() {
    local teto="$1"; shift
    perl -e 'alarm shift; exec @ARGV' "$teto" "$@"
}

# Impressão digital do estado REAL do dono: sha256 + mtime dos arquivos que as
# suítes já vazaram. Serve para NOMEAR quem escreve — sem isso, "o settings mudou"
# é fato sem autor (#535). O app do dono pode gravar ao lado: por padrão é aviso;
# `TTS_EXIGE_DONO_INTOCADO=1` transforma em falha (é o que a prova do card usa).
_ARQ_DONO=(settings.json .apikeys.json .apikey)
_impressao_dono() {
    local a
    for a in "${_ARQ_DONO[@]}"; do
        if [ -f "$a" ]; then
            printf '%s %s %s\n' "$a" "$(/usr/bin/shasum -a 256 "$a" | cut -c1-12)" "$(/usr/bin/stat -f %m "$a")"
        else
            printf '%s - -\n' "$a"
        fi
    done
}
_arquivos_tocados() { # $1 = impressão de antes, $2 = de depois
    diff <(printf '%s\n' "$1") <(printf '%s\n' "$2") 2>/dev/null | sed -n 's/^> \([^ ]*\).*/\1/p'
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
        # `SUITES` aceita caminho absoluto; `/` no nome não pode virar diretório
        # dentro do `$DIR`. O nome do log é achatado e o caminho real vai no
        # resultado (o relatório lê de lá, sem reconstruir).
        LOG="$DIR/$(printf '%s' "$nome" | tr '/' '_').log"
        DONO_ANTES="$(_impressao_dono)"
        # `instancia` pressupõe app DE PÉ, mas duas suítes dele recusam o `:7860`
        # (para não escrever no app/arquivo do dono) e ficariam vermelhas por
        # contrato: quando o alvo É o 7860, o runner liga o escape delas — a
        # escolha é do PM, e só ALI o estágio assume a escrita para medir o app
        # real. `navegador` fica com BASE vazio e sobe o próprio servidor (#535).
        # `VAR=x $lista cmd` NÃO funciona: o shell decide o que é atribuição ANTES
        # de expandir, então `ADMIN_UI_NO_DONO=1` virava nome de comando (rc=127).
        # Exportar dentro do subshell resolve e não vaza para as outras suítes.
        if [ "$estagio" = "instancia" ] || _e_de_instancia "$nome"; then
            export BASE="${BASE:-http://127.0.0.1:7860}"
            case "$BASE" in *:7860*) export ADMIN_UI_NO_DONO=1 NO_DONO=1 ;; esac
        else
            # `navegador` é auto-contido: `BASE` herdado do shell do chamador faria
            # a suíte rodar contra o app do DONO (com escrita liberada) em vez de
            # subir o próprio servidor. Descartar aqui é o que fecha o buraco.
            unset BASE ADMIN_UI_NO_DONO NO_DONO
        fi
        if _toma_a_trava "$nome"; then
            # teto largo na fila; a suíte troca pelo teto real quando pega a trava
            export TTS_TETO_POS_TRAVA="$TETO"
            _com_teto "$TETO_FILA" bash "$(_arquivo_da_suite "$nome")" >"$LOG" 2>&1
        else
            _com_teto "$TETO" bash "$(_arquivo_da_suite "$nome")" >"$LOG" 2>&1
        fi
        rc=$?
        # quem tocou no estado do dono durante ESTA suíte (nomeia o culpado)
        DONO_DEPOIS="$(_impressao_dono)"
        for a in $(_arquivos_tocados "$DONO_ANTES" "$DONO_DEPOIS"); do
            printf '%s escreveu %s\n' "$nome" "$a" >> "$DIR/dono-sujos"
        done
        echo "$rc $nome $(( $(date +%s) - inicio )) $LOG"
    ) >>"$DIR/resultado" &
done
wait

while read -r rc nome dur log; do
    if [ "$rc" -eq 0 ]; then
        printf '  ok    %-24s %ss\n' "$nome" "$dur"
    else
        printf '  FALHA %-24s %ss (rc=%s)\n' "$nome" "$dur" "$rc"
        FALHAS+=("$nome")
    fi
done < <(sort -k2 "$DIR/resultado")

if [ -s "$DIR/dono-sujos" ]; then
    echo
    echo "══ escrita(s) no estado REAL do dono nesta rodada (culpado nomeado):"
    sort -u "$DIR/dono-sujos" | sed 's/^/  /'
    if [ "${TTS_EXIGE_DONO_INTOCADO:-0}" = 1 ]; then
        FALHAS+=("dono-sujos")
        echo "  ✖ TTS_EXIGE_DONO_INTOCADO=1: suíte do estágio não pode encostar no estado do dono"
    fi
fi

echo "--- $estagio: ${#FALHAS[@]} falha(s) de ${#RODADAS[@]} suíte(s) em $(( $(date +%s) - comeca ))s"

if [ "${#FALHAS[@]}" -gt 0 ]; then
    for nome in "${FALHAS[@]}"; do
        log="$(awk -v n="$nome" '$2 == n {print $4; exit}' "$DIR/resultado")"
        echo
        echo "===== $nome (últimas 30 linhas; log completo em ${log:-—}) ====="
        [ -n "$log" ] && [ -f "$log" ] && tail -30 "$log"
    done
    # o diretório sai do trap: copia o que falhou para um lugar previsível
    destino="${TMPDIR:-/tmp}/suites-sh-falhas.$$"
    mkdir -p "$destino"
    while read -r rc nome dur log; do
        case " ${FALHAS[*]} " in *" $nome "*) cp "$log" "$destino/" 2>/dev/null ;; esac
    done < <(sort -k2 "$DIR/resultado")
    # Poda: um teste por rodada, e rodada vermelha acumulava um diretório por vez
    # (18 no /tmp quando isso foi medido — #428). Fica o dos últimos 3 vermelhos.
    ls -1dt "${TMPDIR:-/tmp}"/suites-sh-falhas.* 2>/dev/null | tail -n +4 | xargs rm -rf 2>/dev/null || true
    echo
    echo "logs preservados em $destino (mantidos os 3 últimos vermelhos)"
    exit 1
fi
