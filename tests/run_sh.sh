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
#   TTS_EXIGE_DONO_INTOCADO=1 ./tests/run_sh.sh navegador   # escrita no estado real
#                                              # do dono vira FALHA (padrão: aviso)
#
# Estado real do dono (`settings.json`, `.apikeys.json`, `.apikey`): o runner tira
# uma impressão GLOBAL antes e depois da rodada — arquivo que mudou é AVISO, ou
# FALHA sob o knob acima. Quem escreveu vem como dica amostrada (sob `-j N` pode
# haver co-roda) e, sem ninguém da rodada rodando no instante, a dica diz "escritor
# de fora" (um pytest de outro agente). `TTS_DONO_DIR` aponta o alvo para uma CÓPIA.
#
# Paralelo-seguro por construção: cada suíte escreve o log num diretório próprio
# (`mktemp -d`), nunca em nome fixo de /tmp — duas rodadas simultâneas não se
# truncam. Cada suíte tem TETO próprio: suíte pendurada falha, não prende o gate.
set -uo pipefail

AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RAIZ="$(dirname "$AQUI")"
# 90 s: o hook HERDA este teto, e 240 s por suíte pendurada prendiam o commit por
# minutos. Suíte que passa de 90 s de verdade usa `TETO_S=…` para si; pendurada
# agora falha rápido em vez de segurar o gate.
# TETO PRÓPRIO das pesadas (#592): 90 s é o default do HOOK, não serve para suíte
# de UI/Live — com a máquina carregada elas passam disso trabalhando (medido 93 s
# e 423 s) e morriam de teto no meio do trabalho. A tabela é o ÚNICO lugar que
# decide; `TETO_S=…` do operador continua mandando em tudo.
_teto_da_suite() {
    case "$(basename "$1")" in
        admin_ui_flow.sh|live_barge_rep.sh) echo 600 ;;
        live_ui.sh|live_ws.sh|live_interrupcao_ui.sh|obs_ui.sh) echo 300 ;;
        voz_cadastro_ui.sh|voz_desfazer_ui.sh|voz_check_ui.sh) echo 300 ;;
        live_dsh_fallback.sh|conversa_429.sh) echo 240 ;;
        *) echo 90 ;;
    esac
}
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
            voz_desfazer_ui voz_cadastro_ui voz_check_ui)
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

# ───────────────────── estado REAL do dono: impressão e veredito ─────────────
# Arquivos que as suítes já vazaram. O VEREDITO do estágio é o diff de uma
# impressão GLOBAL da rodada: com `-j 3` a janela de uma suíte longa engole a
# escrita de outra e a acusação por janela trocava de culpado a cada rodada (4
# conjuntos diferentes para a mesma causa — #591); escritor de fora (um pytest de
# outro agente) também não aparecia. A janela por suíte CONTINUA, como DICA de
# autoria — e para ela o estado é amostrado enquanto há suíte rodando.
# `TTS_DONO_DIR` aponta o "dono" para uma CÓPIA: é assim que o teste roda sem
# encostar no `settings.json` real da máquina de trabalho.
_ARQ_DONO=(settings.json .apikeys.json .apikey)
DONO_DIR="${TTS_DONO_DIR:-$RAIZ}"
# portátil: o job `suites-sh` do CI é Linux (`sha256sum`/`stat -c`), a máquina de
# trabalho é macOS (`shasum`/`stat -f`). Sem o ramo, a impressão degrada a ruído.
if command -v sha256sum >/dev/null 2>&1; then _hash() { sha256sum "$1"; }
else _hash() { shasum -a 256 "$1"; }; fi
if stat --version >/dev/null 2>&1; then _mtime() { stat -c %Y "$1"; }
else _mtime() { stat -f %m "$1"; }; fi
_impressao_dono() {
    local a
    for a in "${_ARQ_DONO[@]}"; do
        if [ -f "$DONO_DIR/$a" ]; then
            printf '%s %s %s\n' "$a" "$(_hash "$DONO_DIR/$a" | cut -c1-12)" "$(_mtime "$DONO_DIR/$a")"
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
DONO_ANTES="$(_impressao_dono)"
mkdir -p "$DIR/rodando" "$DIR/inicio" "$DIR/nome"

# Amostrador do estado do dono a cada 0,2 s (#591). Registra, a cada mudança, o
# arquivo tocado e QUEM estava rodando com a idade de cada um: a escrita vazada
# acontece ~1 s depois da largada (é o `_save_settings()` do import), então o
# candidato mais NOVO é o provável e os demais entram como "co-rodavam". Para
# quando o `$DIR/fim` aparece (o `wait` só volta depois disso).
_amostra() {
    local antes="$DONO_ANTES" agora ts a n t lista
    while [ ! -e "$DIR/fim" ]; do
        agora="$(_impressao_dono)"
        if [ "$agora" != "$antes" ]; then
            ts="$(_agora)"
            lista=""
            for n in $(ls "$DIR/rodando" 2>/dev/null); do
                t="$(cat "$DIR/inicio/$n" 2>/dev/null || echo "$ts")"
                lista="$lista $(cat "$DIR/nome/$n" 2>/dev/null || echo "$n"):$(awk -v a="$ts" -v b="$t" 'BEGIN{printf "%.3f", a - b}')"
            done
            for a in $(_arquivos_tocados "$antes" "$agora"); do
                printf '%s|%s\n' "$a" "${lista# }" >> "$DIR/eventos"
            done
            antes="$agora"
        fi
        sleep 0.2
    done
}
# `EPOCHREALTIME` só existe no bash 5 (o macOS traz 3.2): sub-segundo pelo perl,
# que o runner já usa para o alarme. Idade em SEGUNDOS FRACIONÁRIOS é o que
# desempata quem largou por último (o escritor é o mais novo, não o do nome).
_agora() { perl -MTime::HiRes=time -e 'printf "%.3f", time'; }
_amostra &
AMOSTRADOR=$!

# `-1`: o amostrador também é job — ele não ocupa vaga de suíte.
declare -a PIDS=()
N=0
for nome in "${ALVOS[@]}"; do
    while [ "$(( $(jobs -rp | wc -l | tr -d ' ') - 1 ))" -ge "$J" ]; do
        wait -n 2>/dev/null || break
    done
    N=$((N + 1))          # no PAI: o contador dentro do subshell não voltaria
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
        # resultado (o relatório lê de lá, sem reconstruir). A CHAVE achatada é
        # também o marcador de "esta suíte estava rodando" lido pelo amostrador, e
        # o `N` a torna única quando o mesmo nome roda duas vezes.
        CHAVE="$N-$(printf '%s' "$nome" | tr '/' '_')"
        LOG="$DIR/$CHAVE.log"
        echo "$(_agora)" > "$DIR/inicio/$CHAVE"
        echo "$nome" > "$DIR/nome/$CHAVE"
        : > "$DIR/rodando/$CHAVE"
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
        # teto da suíte: o do operador (`TETO_S`) manda; senão, o default dela
        # (90 s do hook ou o próprio, tabela `_teto_da_suite`).
        teto="${TETO_S:-$(_teto_da_suite "$nome")}"
        if _toma_a_trava "$nome"; then
            # teto largo na fila; a suíte troca pelo teto real quando pega a trava
            export TTS_TETO_POS_TRAVA="$teto"
            _com_teto "$TETO_FILA" bash "$(_arquivo_da_suite "$nome")" >"$LOG" 2>&1
        else
            _com_teto "$teto" bash "$(_arquivo_da_suite "$nome")" >"$LOG" 2>&1
        fi
        rc=$?
        # janela DESTA suíte: só dica de autoria, nunca veredito (com `-j 3` a
        # janela de uma suíte longa contém a escrita de outra) — a fonte precisa é
        # o amostrador; esta linha é o que sobra se ele morrer.
        DONO_DEPOIS_S="$(_impressao_dono)"
        for a in $(_arquivos_tocados "$DONO_ANTES" "$DONO_DEPOIS_S"); do
            printf '%s|%s\n' "$a" "$nome" >> "$DIR/janelas"
        done
        rm -f "$DIR/rodando/$CHAVE"
        echo "$rc $nome $(( $(date +%s) - inicio )) $LOG"
    ) >>"$DIR/resultado" &
    PIDS+=($!)
done
# `wait` nas suítes (e não em tudo: o amostrador só sai no `$DIR/fim`). O `fim` é
# criado com as suítes JÁ terminadas para a amostra não perder o fim de uma suíte
# curta; o `wait` final é só para o amostrador sair e o último estado ficar lido.
[ "${#PIDS[@]}" -gt 0 ] && wait "${PIDS[@]}"
: > "$DIR/fim"
wait "$AMOSTRADOR" 2>/dev/null || true

while read -r rc nome dur log; do
    if [ "$rc" -eq 0 ]; then
        printf '  ok    %-24s %ss\n' "$nome" "$dur"
    else
        printf '  FALHA %-24s %ss (rc=%s)\n' "$nome" "$dur" "$rc"
        FALHAS+=("$nome")
    fi
done < <(sort -k2 "$DIR/resultado")

# Dica de autoria de UM arquivo: o evento com MENOS candidatos e, ali, o candidato
# mais NOVO (a escrita do import sai ~1 s depois da largada); os outros ficam como
# "co-rodavam". Sem candidato em nenhum evento → escritor de fora.
_dica() {
    awk -F'|' -v alvo="$1" '
        $1 != alvo { next }
        { n = split($2, c, " "); if (n == 0) { sem = 1; next }
          if (melhor == "" || n < n_melhor) { melhor = $2; n_melhor = n } }
        END {
            if (melhor == "") { if (sem) print "-"; exit }
            split(melhor, c, " "); esc = ""; mi = -1
            for (i = 1; i <= n_melhor; i++) {
                split(c[i], p, ":")
                if (mi < 0 || p[2] + 0 < mi) { mi = p[2] + 0; esc = p[1] }
            }
            printf "%s %.1f", esc, mi
            for (i = 1; i <= n_melhor; i++) {
                split(c[i], p, ":")
                if (p[1] != esc) printf " %s", p[1]
            }
        }' "$DIR/eventos" 2>/dev/null
}

# ── veredito: a impressão GLOBAL da rodada mudou? ──────────────────────────
DONO_AGORA="$(_impressao_dono)"
MUDOU="$(_arquivos_tocados "$DONO_ANTES" "$DONO_AGORA")"
if [ -n "$MUDOU" ]; then
    echo
    echo "══ o estado REAL do dono MUDOU nesta rodada (veredito: impressão global):"
    printf '%s\n' "$MUDOU" | sed 's/^/  /'
    echo "  dica de autoria (amostrador; sob -j N pode haver co-roda):"
    for a in $MUDOU; do
        read -r quem idade outros <<< "$(_dica "$a")"
        # suíte curta (0 s) escreve e sai ANTES da amostra: sem candidato ou sem
        # evento, a janela é o que sobra — e, sem janela também, não é da rodada.
        if [ -z "$quem" ] || [ "$quem" = "-" ]; then
            por_janela="$(awk -F'|' -v x="$a" '$1 == x {printf " %s", $2}' "$DIR/janelas")"
            if [ -n "$por_janela" ]; then
                for q in $por_janela; do echo "    $q escreveu $a (janela da suíte)"; done
            elif [ "$quem" = "-" ]; then
                echo "    $a: ninguém desta rodada rodava no instante — escritor de fora (pytest/outro agente)"
            else
                echo "    $a: sem dica (amostragem e janela vazias)"
            fi
        else
            echo "    $quem escreveu $a (mudou ${idade}s após começar)"
            [ -n "${outros:-}" ] && echo "      co-rodavam no instante: $outros"
        fi
    done
    if [ "${TTS_EXIGE_DONO_INTOCADO:-0}" = 1 ]; then
        FALHAS+=("dono-sujos")
        echo "  ✖ TTS_EXIGE_DONO_INTOCADO=1: suíte do estágio não pode encostar no estado do dono"
    fi
fi

echo "--- $estagio: ${#FALHAS[@]} falha(s) de ${#RODADAS[@]} suíte(s) em $(( $(date +%s) - comeca ))s"

if [ "${#FALHAS[@]}" -gt 0 ]; then
    for nome in "${FALHAS[@]}"; do
        log="$(awk -v n="$nome" '$2 == n {print $4; exit}' "$DIR/resultado")"
        # falha que não é suíte (ex.: escrita no estado do dono) não tem log: sem
        # isto o `awk` não acha linha e o relatório imprime um bloco vazio.
        [ -n "$log" ] || continue
        echo
        echo "===== $nome (últimas 30 linhas; log completo em $log) ====="
        [ -f "$log" ] && tail -30 "$log"
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
