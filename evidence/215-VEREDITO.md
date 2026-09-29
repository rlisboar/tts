# GATE QA #215 — 6 achados da auditoria do backend (#208–#213) + item 7 (#214)

**VEREDITO: APROVADO** — os 6 achados estão corrigidos e a correção MORDE em todos
os 6 (revertendo o mecanismo, a re-derivação cai). Regressão verde. **1 achado novo
P3** (residual do #208, não bloqueia): `task_26f85430`.

Data: 2026-09-29 ~20:45–20:50Z · commit auditado `4729512` (a árvore já tem
edição de terceiro por cima).

## PIN medido (sha256-8 do conteúdo)

| arquivo | commit `4729512` | árvore no gate |
|---|---|---|
| `app.py` | `8ad4fd2e` | `8db6f8e0` |
| `tests/test_api.py` | `2785fa02` | `b48f5e67` |
| `tests/test_live_turno_pendente.py` | `ceb14b11` | `ceb14b11` |

O PIN do enunciado (`c9d0b53d` · `18ea0b04` · `e3f2b0d0`) **não** reproduz por
sha256-8 do arquivo (nem por sha1/md5/sha512/blake2, com ou sem nome, no commit ou
na árvore; varridos também os 200 commits anteriores). O valor acima é o medido.

## Como cada item foi re-derivado (nada do teste do autor)

| # | achado | re-derivação por fora | mordida |
|---|---|---|---|
| 1 | #208 corrida do `turno_pendente` | `evidence/215-208-corrida.py` — observador REAL em thread + pipeline que abre/fecha; 2 escritores, janela alargada | pré-fix verbatim (`descarrega_prefix`): 2203 B perdidos na janela e 10 quedas de contador; fix: 0 B / 0 quedas |
| 2 | #209 `await` sob `_live_lock` | `evidence/215-209-invariante.py` (espião no instante do send) **e** `evidence/215-209-efeito.py` (uvicorn próprio, peer que não lê, `/health` cronometrado) | invariante: mutação vê `True`; efeito: cena A 113 ms vs cena B (lock preso) 2001 ms = timeout |
| 3 | #210 `session_id` do cliente | `evidence/215-ws.py --somente=item3` + `evidence/215-210-mutacao.sh` (patch textual no `app.py`) | pré-fix devolve o uuid do servidor (`zzzzzzzzzz`) e nunca retoma: 5 falhas |
| 4 | #211 stub sem pipeline | `evidence/215-ws.py --somente=item4` — prova pelo ESTADO no fim do turno | pré-fix: `st_stage` preso em `stt`, buffer com 1000 B, `truncado` não consumido, `hist_pos_turno` 0x |
| 5 | #212 ticket inválido | `evidence/215-ws.py --somente=item5` — host não-loopback (`testclient` não é isento por `_is_local`), logo é o cenário LAN/túnel | pré-fix: ticket usado/inventado com chave ou header válidos → 4401 |
| 6 | #213 descoberta dsh | `evidence/215-213-dsh-models.py` — 3 GETs simultâneos com cache frio e descoberta presa em Event | pré-fix (nullcontext): 3 descobertas em vez de 1 |
| 7 | #214 `codigo` do boot | `evidence/215-214-build-codigo.py` — 2 instâncias reais, árvore editada no meio | instância viva segue acusando A, instância nova acusa B, `version`×`codigo` divergem |

Saída crua de tudo: `evidence/215-raw.txt`.

## Regressão

* `pyflakes app.py common.py tts_worker.py backends.py tests/*.py client/mic_router.py` → **0**
* `pytest tests/ -q` → **618 passed, 3 skipped** (`evidence/215-pytest.txt`)
* `./evidence/215-210-mutacao.sh` devolve o `app.py` byte a byte (md5 conferido)

## Achado novo

**P3 `task_26f85430`** — `app.py:6490-6492`: o observador PERDEDOR do turno pendente
zera `pendentes_trechos`/`pendentes_descartados_ms` enquanto o vencedor re-arma o
MESMO pendente → o evento `turno_pendente` subconta. Repro determinístico em
`evidence/215-208-residual.py` (com perdedor: `trechos=1` com 2 trechos; controle:
`trechos=2`). Cosmético, não perde áudio.

Observação menor (mesma área, não virou task): `_live_pend_rearma` (`app.py:6450`)
concatena `pendente + novo` sem reaplicar o teto — o pendente pode chegar a 2×
`_LIVE_PENDENTE_MAX_BYTES` (≈1,92 MB) até a próxima guarda truncar. Limitado e
transitório.

## Notas de ambiente (a nomear, não a atribuir)

* A árvore mudou durante o gate: `live_turns.py` `fbe7a039` → `0a35d2c8` (terceiro).
* O harness de efeito do autor (`tests/live_lock_freeze.sh`) ficou **preso na trava
  de modelo** (~6 min) porque outra suíte (`live_barge_rep.sh`, #216) segurava
  `/tmp/tts-rod-modelo.lock`. Por isso o efeito do #209 foi medido também por um
  harness PRÓPRIO, sem trava de modelo.
---

## RE-CHECK (segunda passada, mesma sessão de gate) — 2026-09-29T20:48–20:52Z

Árvore INALTERADA desde o veredito (mesmos hashes: `app.py` `8db6f8e0`,
`tests/test_api.py` `b48f5e67`, `tests/test_live_turno_pendente.py` `ceb14b11`),
e os 7 itens foram RE-RODADOS por fora para confirmar que o veredito não dependia
de estado intermediário. Saída crua: `evidence/215-raw2.txt`.

| item | re-execução |
|---|---|
| #208 fix | `--janela-ms 3`: 0 B perdidos, 0 quedas, 7 re-armes (5 janelas com bytes) — OK |
| #208 mutação | mesma janela: **864 B perdidos + 5 quedas** — morde |
| #209 invariante | fix: `locked()=False` no instante do send; mutação: `True` — morde |
| #209 EFEITO | cena A **62 ms** (livre) × cena B **2003 ms** = timeout — a medida separa os mundos |
| #210 + #211 + #212 | `215-ws.py`: 0 falhas nos três (id adotado/recusado, estado do stub limpo, fallback de ticket) |
| #213 | 3 GETs frios → **1** descoberta, 3× 200; mutação (nullcontext) → 3 |
| #214 | instância antiga segue acusando A, nova acusa B, `version`×`codigo` divergem |
| regressão | `pyflakes` **0** · `pytest tests/ -q` = **618 passed, 3 skipped** (rodada 20:48–20:51Z, árvore estável) |

### Observação nova (mesma família do item 3, P3) — `evidence/215-210-teto.py`

A adoção do `session_id` do cliente (#210) torna o registro chaveado pelo id do
cliente, e `_live_sessions[sid] = sess` SOBRESCREVE a entrada quando o id repete:
com `TTS_LIVE_MAX_SESSIONS=1`, três sockets com o MESMO id foram **3 ready** com
`len(_live_sessions)==1` (e um id diferente levou `busy`). Ou seja, o teto de
sessões simultâneas é contornável reusando um id, e a sessão sobrescrita fica
viva FORA do registro (`_live_sweep` itera o registro → não é marcada `vencida`
nem fechada pelo servidor). O `pop` do socket zumbi já é protegido por
identidade (`if _live_sessions.get(sid) is sess`), então o registro não é
corrompido — é contagem e varredura. Não bloqueia o gate; task nova para o dono.
