# Suítes `.sh`: qual estágio, e por quê (#387)

Estas suítes provam por EFEITO o que o `pytest` não alcança (navegador, WS, áudio,
trava de modelo). Elas ficaram meses fora do gate: nenhuma rodava sozinha e nenhum
doc fixava o conjunto — uma quebra de XSS ou de `key_storage` entrava com o gate
verde. O runner é `tests/run_sh.sh`.

## Os três estágios existem por DEPENDÊNCIA, não por gosto

| Estágio | Precisa | Roda em | Onde |
|---|---|---|---|
| `rapido` | nada (bash + perl) | **~19 s** com `-j 3` | **no hook**, quando o diff toca a área |
| `navegador` | Chromium/Playwright | ~4 min | no gate que mexe em `static/`, `client/` ou `live_*.py` |
| `instancia` | app de pé (`BASE=`) | depende | à mão, com o `./run.sh` no ar — **não** no hook |

`instancia` fica fora do hook de propósito: exige modelo carregado e ninguém deve
subir app por commit. Quando o estágio é chamado sem app, o runner falha uma vez,
com o motivo, em vez de produzir 11 vermelhos mentirosos.

## Regra de paralelismo e de log

Cada suíte escreve em `$DIR/<nome>.log` (`mktemp -d`), **nunca** em nome fixo de
/tmp: duas rodadas simultâneas não truncam o log uma da outra. Cada suíte tem
`TETO_S` (240 por padrão) e o teto é portátil — o `timeout` do coreutils não existe
no macOS, o runner usa `alarm` do perl. Suíte pendurada falha; não prende o gate.

## Regra de crescimento

- Suíte nova: entra em UM estágio, no menor que a dependência permitir, e no
  `tests/test_run_sh.py` se o estágio for `rapido`/`navegador`.
- Suíte que quebrar por depender de estado do dono (voz, chave, `settings.json`)
  vira `instancia`, não ganha `skip` silencioso.
- `tests/test_run_sh.py` guarda o contrato: todo nome da lista existe, é executável
  e não aparece em dois estágios. Renomear uma suíte e esquecer a lista fica vermelho
  no `pytest`, não em produção.

## O que cada estágio tem hoje

- `rapido`: `limpa_bytecode` (invariante do harness), `serial_lock` (trava de
  modelo), `live_lock_freeze` (efeito do `await` com `_live_lock` presa — #209/#215).
- `navegador`: `dsh_ui`, `hash_deep_link`, `live_dsh_fallback`, `live_barge_rep`,
  `live_ui`, `obs_ui`, `live_ws`.
- `instancia`: `a11y_teclado`, `admin_ui_flow`, `cfg_sujo_e_erros`, `conversa_429`,
  `key_storage_repro`, `live_hold_ui`, `mic_erro_ui`, `mobile_layout`,
  `onboarding_atalhos`, `ui_repouso`, `xss_frontend_repro`.

## Dependência de estado conhecida dentro de `instancia`

`onboarding_atalhos` precisa de uma voz que a suíte de XSS (`xss_frontend_repro.sh`)
cria (`voices/xssvirt-*.wav`) e apaga no `--cleanup`. Se ela rodar sozinha num app
sem voz, falha com "No such file or directory: …/xssvirt-*.wav". Para reproduzir
um vermelho dela, rode `xss_frontend_repro.sh` antes (sem `--cleanup`) ou grave uma
voz. Com `-j` alto as duas disputam o mesmo diretório: para depurar,
`./tests/run_sh.sh instancia -j 1`.
