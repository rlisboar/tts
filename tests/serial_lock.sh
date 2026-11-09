#!/usr/bin/env bash
# Teste da trava de modelo (tests/serial.sh) — task #134.
#
# A trava é a defesa contra o falso vermelho de latência do live_ws.sh quando a
# bateria roda em paralelo (medido: 1792 ms sob contenção x 884 ms sozinho). Se
# ela quebrar, o falso vermelho volta — e um falso verde é pior: uma suíte que
# "entra" na trava alheia mede contaminada e ninguém percebe.
#
# Roda SEM servidor e SEM modelo (segundos), com um lock PRÓPRIO para não
# atropelar baterias de verdade em andamento.
set -uo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SUF="$$"
export TTS_SERIAL_LOCK="/tmp/tts-rod-serial-teste-$SUF.lock"
TMPD="$(mktemp -d "/tmp/tts-rod-serial-$SUF.XXXXXX")"
falhas=0
cobrar() { [ "$1" = "1" ] && echo "  ✔ $2" || { echo "  ✘ $2"; falhas=$((falhas+1)); }; }

limpa() { /bin/rm -rf "$TMPD" "$TTS_SERIAL_LOCK"; }
trap limpa EXIT

# worker: entra na trava, avisa, dorme o que pedirem e sai
cat > "$TMPD/worker.sh" <<EOF
#!/usr/bin/env bash
source "$RAIZ/tests/serial.sh"; serial_pega "\$@" || exit 1
echo "DENTRO \$(date +%s%N)"
sleep "\${DORMIR:-1}"
EOF
chmod +x "$TMPD/worker.sh"

# ─── 1) dois workers NÃO entram juntos; o segundo espera o primeiro ─────────
# ORDEM FIXADA (#141): sem esperar a dona registrar o pid, o 2º (DORMIR=0) podia
# ganhar o mkdir e quem esperava era o 1º — o aviso saía no OUTRO log e a
# asserção virava corrida (2/3 rodadas vermelhas com a trava funcionando).
/bin/rm -rf "$TTS_SERIAL_LOCK"
t0=$(date +%s%N)
( DORMIR=2 bash "$TMPD/worker.sh" primeiro.sh > "$TMPD/a.log" 2>&1 ) &
for _ in $(seq 250); do [ -s "$TTS_SERIAL_LOCK/pid" ] && break; sleep 0.02; done
cobrar "$([ -s "$TTS_SERIAL_LOCK/pid" ] && echo 1 || echo 0)" "a 1ª dona registrou o pid antes de o 2º entrar"
cobrar "$([ "$(cat "$TTS_SERIAL_LOCK/qual" 2>/dev/null)" = "primeiro.sh" ] && echo 1 || echo 0)" \
       "a trava registra QUEM é a dona (dona: $(cat "$TTS_SERIAL_LOCK/qual" 2>/dev/null))"
( DORMIR=0 bash "$TMPD/worker.sh" segundo.sh  > "$TMPD/b.log" 2>&1 ) &
wait
t1=$(date +%s%N)
dur=$(( (t1 - t0) / 1000000 ))
cobrar "$(grep -c 'segundo.sh esperando a trava' "$TMPD/b.log" || true)" \
       "o 2º worker esperou e o aviso NOMEIA quem era a dona"
cobrar "$([ "$dur" -ge 1800 ] && echo 1 || echo 0)" "serializou de fato (levou ${dur} ms; sem trava seria ~<1 s)"
cobrar "$([ ! -d "$TTS_SERIAL_LOCK" ] && echo 1 || echo 0)" "a trava foi liberada no exit"
cobrar "$(grep -c 'DENTRO' "$TMPD/a.log")" "o 1º worker entrou"
cobrar "$([ "$(grep -c 'DENTRO' "$TMPD/b.log")" = "1" ] && echo 1 || echo 0)" "o 2º entrou (depois)"

# ─── 1b) controle NEGATIVO: sem a trava, o 2º NÃO espera ───────────────────
# Sem isto, "esperou" poderia passar por acidente (ex.: asserção lendo o log
# errado) — aqui o mesmo par roda com TTS_SERIAL=0 e ninguém pode esperar.
/bin/rm -rf "$TTS_SERIAL_LOCK"
( DORMIR=2 TTS_SERIAL=0 bash "$TMPD/worker.sh" n1.sh > "$TMPD/f1.log" 2>&1 ) &
( DORMIR=0 TTS_SERIAL=0 bash "$TMPD/worker.sh" n2.sh > "$TMPD/f2.log" 2>&1 ) &
wait
cobrar "$([ "$(grep -c 'esperando a trava' "$TMPD/f2.log")" = "0" ] && echo 1 || echo 0)" \
       "controle negativo: com TTS_SERIAL=0 o 2º não espera"
cobrar "$([ "$(grep -c 'DENTRO' "$TMPD/f2.log")" = "1" ] && echo 1 || echo 0)" \
       "controle negativo: com TTS_SERIAL=0 o 2º entra na hora"
/bin/rm -rf "$TTS_SERIAL_LOCK"

# ─── 2) lock ÓRFÃO (dona morta) é roubado na hora, sem esperar o TTL ────────
mkdir -p "$TTS_SERIAL_LOCK"; echo 999999 > "$TTS_SERIAL_LOCK/pid"; echo "morta.sh" > "$TTS_SERIAL_LOCK/qual"
TTS_SERIAL_TTL=99999 bash "$TMPD/worker.sh" nova.sh > "$TMPD/c.log" 2>&1
cobrar "$(grep -c 'órfã' "$TMPD/c.log")" "lock órfão (pid morto) foi roubado e avisado"
cobrar "$(grep -c 'DENTRO' "$TMPD/c.log")" "o worker entrou depois de roubar o órfão"

# ─── 2b) lock SEM pid e antigo (dona morta entre o mkdir e o echo) ──────────
# Sem este ramo a trava sem pid só cairia no TTL de 40 min: a suíte pareceria
# pendurada e o "falso vermelho" viraria "falso travado".
mkdir -p "$TTS_SERIAL_LOCK"; touch -t 202001010000 "$TTS_SERIAL_LOCK"
TTS_SERIAL_TTL=99999 bash "$TMPD/worker.sh" sempid.sh > "$TMPD/f.log" 2>&1
cobrar "$(grep -c 'sem pid' "$TMPD/f.log")" "lock sem pid e antigo foi assumido (não esperou o TTL)"
cobrar "$(grep -c 'DENTRO' "$TMPD/f.log")" "o worker entrou depois de assumir o lock sem pid"
# e a janela de ms do pid NÃO é roubada indevidamente: um lock recém-criado
# (dona viva, pid ainda não escrito) é respeitado — com espera de 1 s ele vence
# o tempo e sai != 0, em vez de atropelar quem acabou de entrar.
mkdir -p "$TTS_SERIAL_LOCK"
TTS_SERIAL_ESPERA=1 bash "$TMPD/worker.sh" janela.sh > "$TMPD/g.log" 2>&1
rc_jan=$?
cobrar "$([ "$rc_jan" != "0" ] && echo 1 || echo 0)" "lock recém-criado não é roubado na janela do pid (rc=$rc_jan)"
cobrar "$(grep -c 'não liberou' "$TMPD/g.log")" "e a espera terminou com motivo, não em silêncio"
/bin/rm -rf "$TTS_SERIAL_LOCK"

# ─── 2c) `rm` de PATH quebrado (#996) não pode prender o laço ────────────────
# O guard do harness morre no parse de `-rf` e sai 0 SEM apagar. Com `rm` de PATH,
# a trava órfã era "assumida" a cada 0,5 s para sempre (1,5 MB de log por rodada).
# O shim abaixo reproduz o guard. O fix usa `/bin/rm` e tem teto de tentativas: sem
# qualquer dos dois, a célula cai (rc != 0 ou aviso repetido).
mkdir -p "$TMPD/bin-rm-quebrado"
cat > "$TMPD/bin-rm-quebrado/rm" <<'SH'
#!/usr/bin/env bash
exit 0          # sai 0 sem apagar nada (guard do harness, #996)
SH
chmod +x "$TMPD/bin-rm-quebrado/rm"
mkdir -p "$TTS_SERIAL_LOCK"; echo 999999 > "$TTS_SERIAL_LOCK/pid"; echo "morta.sh" > "$TTS_SERIAL_LOCK/qual"
t_quebrado=$(date +%s)
PATH="$TMPD/bin-rm-quebrado:$PATH" TTS_SERIAL_TTL=99999 TTS_SERIAL_ESPERA=5 \
    bash "$TMPD/worker.sh" quebrado.sh > "$TMPD/h.log" 2>&1
rc_quebrado=$?
dur_quebrado=$(( $(date +%s) - t_quebrado ))
cobrar "$([ "$rc_quebrado" = "0" ] && echo 1 || echo 0)" \
       "rm de PATH quebrado: a trava órfã ainda é assumida (rc=$rc_quebrado)"
cobrar "$([ "$dur_quebrado" -lt 3 ] && echo 1 || echo 0)" \
       "e sem girar o laço (${dur_quebrado}s; sem fix ele só sai no TTS_SERIAL_ESPERA)"
cobrar "$([ "$(grep -c 'assumindo' "$TMPD/h.log")" = "1" ] && echo 1 || echo 0)" \
       "o aviso de assumir sai UMA vez, não a cada 0,5 s"
cobrar "$(grep -c 'DENTRO' "$TMPD/h.log")" "o worker entrou mesmo com o rm quebrado"
/bin/rm -rf "$TTS_SERIAL_LOCK"

# ─── 2d) teto inválido não pode desligar o teto (#1013) ─────────────────────
# Trava que nem o `/bin/rm` tira (pai sem escrita). Com `TTS_SERIAL_REMOCAO_TETO=abc`
# o `[ n -gt abc ]` errava, o teto ficava inerte e o laço girava para sempre. Sem o
# fix a célula estoura o prazo de 5 s (o processo é morto) e cai.
mkdir -p "$TMPD/pai-ro/trava"; chmod 555 "$TMPD/pai-ro"
for teto in abc "" 0 -3; do
  TTS_SERIAL_LOCK="$TMPD/pai-ro/trava" TTS_SERIAL_REMOCAO_TETO="$teto" \
      bash -c 'source "$1/tests/serial.sh"; _serial_remove_trava' _ "$RAIZ" \
      > "$TMPD/teto.log" 2>&1 &
  pid_teto=$!
  for _ in $(seq 50); do kill -0 "$pid_teto" 2>/dev/null || break; sleep 0.1; done
  if kill -0 "$pid_teto" 2>/dev/null; then
    kill "$pid_teto" 2>/dev/null; wait "$pid_teto" 2>/dev/null
    cobrar 0 "teto '$teto': laço girou >5 s (teto inerte)"
  else
    wait "$pid_teto"; rc_teto=$?
    cobrar "$([ "$rc_teto" != "0" ] && grep -q 'desisto' "$TMPD/teto.log" && echo 1 || echo 0)" \
           "teto '$teto' cai no padrão 5 e desiste com erro claro (rc=$rc_teto)"
  fi
done
chmod 755 "$TMPD/pai-ro"

# ─── 3) timeout curto devolve erro em vez de pendurar a bateria ─────────────
mkdir -p "$TTS_SERIAL_LOCK"; echo $$ > "$TTS_SERIAL_LOCK/pid"; echo "eu.sh" > "$TTS_SERIAL_LOCK/qual"
TTS_SERIAL_ESPERA=1 bash "$TMPD/worker.sh" apressada.sh > "$TMPD/d.log" 2>&1
rc=$?
cobrar "$([ "$rc" != "0" ] && echo 1 || echo 0)" "espera estourou e retornou != 0 (rc=$rc)"
cobrar "$(grep -c 'não liberou' "$TMPD/d.log")" "e disse o motivo (não falhou mudo)"
/bin/rm -rf "$TTS_SERIAL_LOCK"

# ─── 4) escape TTS_SERIAL=0 entra mesmo com a trava presa ──────────────────
mkdir -p "$TTS_SERIAL_LOCK"; echo $$ > "$TTS_SERIAL_LOCK/pid"; echo "presa.sh" > "$TTS_SERIAL_LOCK/qual"
TTS_SERIAL=0 bash "$TMPD/worker.sh" solta.sh > "$TMPD/e.log" 2>&1
cobrar "$(grep -c 'DENTRO' "$TMPD/e.log")" "TTS_SERIAL=0 ignora a trava (escape documentado)"
# Não basta "entrou": também NÃO pode soltar a trava alheia ao sair — senão o
# escape viraria ladrão silencioso para o próximo da fila.
cobrar "$([ -d "$TTS_SERIAL_LOCK" ] && echo 1 || echo 0)" "e preserva a trava da dona (não a apaga ao sair)"

# ─── 5) as suítes que carregam modelo estão de fato plugadas ────────────────
for s in live_ws.sh live_ui.sh obs_ui.sh; do
  cobrar "$(grep -c 'serial.sh' "$RAIZ/tests/$s")" "$s chama a trava (sem isto não há serialização)"
done

echo
if [ "$falhas" != "0" ]; then echo "✖ $falhas falha(s)"; exit 1; fi
echo "✔ OK — trava de modelo serializa, rouba órfão, expira e tem escape"