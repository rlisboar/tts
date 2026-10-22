# Índice de `evidence/` (#390)

Gerado a partir dos arquivos versionados: task (prefixo numérico do nome), data do
último commit e a primeira linha do arquivo. É o que torna verificável a frase
"já está provado em evidence" — sem isto, achar a prova exigia abrir arquivo por
arquivo, e 29 deles nem número de task no nome tinham (o histórico fica como está).

Regra para arquivo NOVO (cobrada por `tests/test_evidence_index.py`): prefixo `NNN-`
com o número da task e uma primeira linha de cabeçalho dizendo o que a prova mostra.
Depois de acrescentar, regenere: `./.venv-mlx/bin/python tests/gera_evidence_index.py`.

| arquivo | task | data | o que mostra |
|---|---|---|---|
| `117-live-feedback-erros.png` | #117 | 2026-09-29 | (imagem) |
| `119-live-painel-obs.png` | #119 | 2026-09-29 | (imagem) |
| `120-obs-conversa.png` | #120 | 2026-09-29 | (imagem) |
| `120-obs-gerar.png` | #120 | 2026-09-29 | (imagem) |
| `120-obs-stt.png` | #120 | 2026-09-29 | (imagem) |
| `120-obs-vozes.png` | #120 | 2026-09-29 | (imagem) |
| `131-dsh-ui-endpoint.png` | #131 | 2026-09-29 | (imagem) |
| `131-dsh-ui-harness.png` | #131 | 2026-09-29 | (imagem) |
| `150-live-dsh-fallback.png` | #150 | 2026-09-29 | (imagem) |
| `167-baseline-eco.txt` | #167 | 2026-09-29 | servidor http://127.0.0.1:64160 · stub :64159 · repetições=20 |
| `167-dispara.py` | #167 | 2026-09-29 | """Sobe `evidence/167-fechamento2.sh` DESTACADO da sessão/grupo do shell. |
| `167-duracao-eco.txt` | #167 | 2026-09-29 | servidor http://127.0.0.1:62237 · stub :62236 · repetições=20 |
| `167-duracao-true.txt` | #167 | 2026-09-29 | servidor http://127.0.0.1:60817 · stub :60816 · repetições=20 |
| `167-fechamento-eco.txt` | #167 | 2026-09-29 | [serial] live_barge_rep.sh esperando a trava de modelo (dona: live_lock_freeze.sh)… |
| `167-fechamento-live_ui.txt` | #167 | 2026-09-29 | (node:80006) Warning: The 'NO_COLOR' env is ignored due to the 'FORCE_COLOR' env being set. |
| `167-fechamento-resumo.txt` | #167 | 2026-09-29 | === vao (18:13:14) === |
| `167-fechamento-true.txt` | #167 | 2026-09-29 | (node:72235) Warning: The 'NO_COLOR' env is ignored due to the 'FORCE_COLOR' env being set. |
| `167-fechamento-vao.txt` | #167 | 2026-09-29 | servidor http://127.0.0.1:61633 · stub :61632 · repetições=20 |
| `167-fechamento.sh` | #167 | 2026-09-29 | !/usr/bin/env bash |
| `167-fechamento2.log` | #167 | 2026-09-29 | (sem descrição) |
| `167-fechamento2.sh` | #167 | 2026-09-29 | !/usr/bin/env bash |
| `167-matriz-c.sh` | #167 | 2026-09-29 | !/usr/bin/env bash |
| `167-matriz.sh` | #167 | 2026-09-29 | !/usr/bin/env bash |
| `167-rep20-baseline.txt` | #167 | 2026-09-29 | [serial] live_barge_rep.sh esperando a trava de modelo (dona: live_barge_rep.sh)… |
| `167-turno-eco.txt` | #167 | 2026-09-29 | servidor http://127.0.0.1:63050 · stub :63049 · repetições=20 |
| `167-turno-true.txt` | #167 | 2026-09-29 | servidor http://127.0.0.1:61278 · stub :61277 · repetições=20 |
| `174-latencia-provedor.png` | #174 | 2026-09-29 | (imagem) |
| `178-backend-live-app-real.png` | #178 | 2026-09-29 | (imagem) |
| `178-backend-live-heranca.png` | #178 | 2026-09-29 | (imagem) |
| `179-barge-vao-rep20.txt` | #179 | 2026-09-29 | [serial] live_barge_rep.sh esperando a trava de modelo (dona: live_ui.sh)… |
| `179-controle-resumo.txt` | #179 | 2026-09-29 | normal                       EXIT=0  avisos=0  log=/tmp/179-normal.log |
| `179-controle.sh` | #179 | 2026-10-01 | !/usr/bin/env bash |
| `179-fechamento-live_ui.txt` | #179 | 2026-09-29 | [serial] live_ui.sh esperando a trava de modelo (dona: live_barge_rep.sh)… |
| `179-retomada-normal.txt` | #179 | 2026-09-29 | === #179 retomada: tests/live_ui.sh rodada NORMAL na árvore de 17:43 (default do worktree: JANELA_TURNO=1 PLAY |
| `179-vao-default.txt` | #179 | 2026-09-29 | === #179 MODO=vao REP=20 · flags=default (default do worktree) === |
| `179-vao-flags-off.txt` | #179 | 2026-09-29 | === #179 MODO=vao REP=20 · flags=flags-off (TTS_LIVE_BARGE_JANELA_TURNO=0 TTS_LIVE_PLAYBACK_DURACAO=0 TTS_LIVE |
| `179-vao-flags-on.txt` | #179 | 2026-09-29 | === #179 MODO=vao REP=20 · flags=flags-on (TTS_LIVE_BARGE_JANELA_TURNO=1 TTS_LIVE_PLAYBACK_DURACAO=1 TTS_LIVE_ |
| `179-vao-flags.sh` | #179 | 2026-09-29 | !/usr/bin/env bash |
| `180-230-pytest.txt` | #180 | 2026-10-01 | pytest tests/ -q — 2026-09-30T03:10:48Z · HEAD=c348e2a |
| `180-barge-falso-eco.txt` | #180 | 2026-10-01 | servidor http://127.0.0.1:50085 · stub :50084 · repetições=10 |
| `180-barge-verdadeiro.txt` | #180 | 2026-10-01 | servidor http://127.0.0.1:49563 · stub :49561 · repetições=10 |
| `180-controle-estrito-sem-barge.txt` | #180 | 2026-10-01 | servidor de teste em http://127.0.0.1:64937 (stub :64936) |
| `180-controle-tolerante.txt` | #180 | 2026-10-01 | servidor de teste em http://127.0.0.1:65449 (stub :65448) |
| `180-estrito.txt` | #180 | 2026-10-01 | servidor de teste em http://127.0.0.1:64788 (stub :64787) |
| `180-recheck-controle-estrito.txt` | #180 | 2026-10-01 | servidor de teste em http://127.0.0.1:54701 (stub :54700) |
| `180-recheck-controle-tolerante.txt` | #180 | 2026-10-01 | servidor de teste em http://127.0.0.1:54807 (stub :54806) |
| `180-recheck-eco-c.log` | #180 | 2026-10-01 | (sem descrição) |
| `180-recheck-eco-c.sh` | #180 | 2026-10-01 | !/usr/bin/env bash |
| `180-recheck-eco-c.txt` | #180 | 2026-10-01 | servidor http://127.0.0.1:55362 · stub :55361 · repetições=10 |
| `180-recheck-eco-md5.txt` | #180 | 2026-10-01 | 180 eco — 2026-10-01T04:27:38Z · HEAD=c348e2a |
| `180-recheck-eco.log` | #180 | 2026-10-01 | (sem descrição) |
| `180-recheck-eco.sh` | #180 | 2026-10-01 | !/usr/bin/env bash |
| `180-recheck-eco.txt` | #180 | 2026-10-01 | (node:70949) Warning: The 'NO_COLOR' env is ignored due to the 'FORCE_COLOR' env being set. |
| `180-recheck-eco2-md5.txt` | #180 | 2026-10-01 | 180 eco — 2026-10-01T04:33:15Z · HEAD=c348e2a |
| `180-recheck-eco2.log` | #180 | 2026-10-01 | (sem descrição) |
| `180-recheck-eco2.txt` | #180 | 2026-10-01 | servidor http://127.0.0.1:52967 · stub :52966 · repetições=10 |
| `180-recheck-eco3-md5.txt` | #180 | 2026-10-01 | 180 eco — 2026-10-01T04:36:52Z · HEAD=0587865 |
| `180-recheck-eco3.log` | #180 | 2026-10-01 | (sem descrição) |
| `180-recheck-eco3.sh` | #180 | 2026-10-01 | !/usr/bin/env bash |
| `180-recheck-eco3.txt` | #180 | 2026-10-01 | servidor http://127.0.0.1:53674 · stub :53671 · repetições=20 |
| `180-recheck-estrito.txt` | #180 | 2026-10-01 | servidor de teste em http://127.0.0.1:54464 (stub :54462) |
| `180-recheck-md5.txt` | #180 | 2026-10-01 | 180 recheque — 2026-09-30T03:35:43Z · HEAD=c348e2a |
| `180-recheck-vao.txt` | #180 | 2026-10-01 | servidor http://127.0.0.1:54913 · stub :54912 · repetições=10 |
| `180-recheck.sh` | #180 | 2026-10-01 | !/usr/bin/env bash |
| `181-gate-dsh.txt` | #181 | 2026-09-29 | 181 — gate vermelho/flaky no caminho dsh · evidência (speech-pipeline, 16:35–17:00Z) |
| `184-mistos-gate.png` | #184 | 2026-09-29 | (imagem) |
| `194-fora-187-188.py` | #194 | 2026-10-01 | !/usr/bin/env python3 |
| `194-mordida.py` | #194 | 2026-09-29 | !/usr/bin/env python3 |
| `194-repro-independente.py` | #194 | 2026-10-01 | !/usr/bin/env python3 |
| `202-N-dsh-invalido.txt` | #202 | 2026-10-01 | (node:82427) Warning: The 'NO_COLOR' env is ignored due to the 'FORCE_COLOR' env being set. |
| `202-P-admin_ui_flow.sh.txt` | #202 | 2026-10-01 | chaves de teste: use=29626a44… admin=be45ce2c… |
| `202-P-dsh_ui.sh.txt` | #202 | 2026-10-01 | (node:80337) Warning: The 'NO_COLOR' env is ignored due to the 'FORCE_COLOR' env being set. |
| `202-S-sigint.txt` | #202 | 2026-10-01 | chaves de teste: use=9d034266… admin=a5b64626… |
| `202-antes.txt` | #202 | 2026-10-01 | gate #202 — snapshot do estado do dono — 2026-10-01T05:54:28Z |
| `202-fora-de-escopo-suite.txt` | #202 | 2026-10-01 | chaves de teste: use=461d1194… admin=38018a8a… |
| `202-fora-de-escopo.txt` | #202 | 2026-10-01 | gate #202 — célula 3 (campo FORA do escopo mexido no meio da rodada) — 2026-10-01T05:59:35Z |
| `202-gate.sh` | #202 | 2026-10-01 | !/usr/bin/env bash |
| `202-gate.txt` | #202 | 2026-10-01 | gate #202 — 2026-10-01T05:54:46Z · pins: dsh_ui.sh=bffc59c0ecb6474065549b00ab7669e1 admin_ui_flow.sh=d94c0b2ef |
| `202-gate2.txt` | #202 | 2026-10-01 | gate #202 — 2026-10-01T05:57:17Z · pins: dsh_ui.sh=bffc59c0ecb6474065549b00ab7669e1 admin_ui_flow.sh=d94c0b2ef |
| `205-controle-familia.py` | #205 | 2026-10-01 | !/usr/bin/env python3 |
| `207-residual-janela.py` | #207 | 2026-10-01 | """#207 residual: `close()` caindo DENTRO do `_LiveWorker.start()` deixa filho órfão? |
| `207-residual-janela.txt` | #207 | 2026-09-29 | Review do #207 (task_f147cb8c) — janela residual do worker. |
| `214-218-verificacao-qa.txt` | #214 | 2026-10-01 | 214 e #218 — verificação de gate pelo qa-review (independente do relato do fix) |
| `214-midrun-edit.py` | #214 | 2026-10-02 | """Simula o cenário REAL da árvore compartilhada: um agente edita um módulo do |
| `215-208-corrida.py` | #215 | 2026-09-29 | """GATE #215 item 1 (#208) — re-derivação POR FORA, com corrida REAL de threads. |
| `215-208-residual.py` | #215 | 2026-10-02 | """GATE #215 item 1 (#208) — RESIDUAL: o observador PERDEDOR zera os contadores. |
| `215-209-efeito.py` | #215 | 2026-09-29 | """GATE #215 item 2 (#209) — PROVA DE EFEITO, independente do harness do autor. |
| `215-209-efeito.txt` | #215 | 2026-09-29 | · cena A — `await` com o lock LIVRE (o fix): /health TEM de responder |
| `215-209-invariante.py` | #215 | 2026-09-29 | """GATE #215 item 2 (#209) — INVARIANTE: nenhum `await` com `_live_lock` preso. |
| `215-210-mutacao.sh` | #215 | 2026-09-29 | !/usr/bin/env bash |
| `215-210-teto.py` | #215 | 2026-09-29 | """GATE #215 — observação do item 3 (#210): o id do cliente passa a ser ADOTADO, e |
| `215-213-dsh-models.py` | #215 | 2026-09-29 | """GATE #215 item 6 (#213) — descoberta de modelos com cache FRIO e GETs simultâneos. |
| `215-214-build-codigo.py` | #215 | 2026-09-29 | """GATE #215 item 7 (#214) — `codigo` de /api/build é o CÓDIGO CARREGADO (do boot). |
| `215-VEREDITO.md` | #215 | 2026-09-29 | GATE QA #215 — 6 achados da auditoria do backend (#208–#213) + item 7 (#214) |
| `215-pytest.txt` | #215 | 2026-09-29 | ........................................................................ [ 11%] |
| `215-raw.txt` | #215 | 2026-09-29 | GATE #215 — re-derivação por fora — 2026-09-29T20:45:38Z |
| `215-raw2.txt` | #215 | 2026-09-29 | RE-CHECK 2026-09-29T20:48:36Z |
| `215-ws.py` | #215 | 2026-09-29 | """GATE #215 — re-derivação POR FORA dos itens 3 (#210), 4 (#211) e 5 (#212). |
| `216-DECISAO.md` | #216 | 2026-09-29 | 216 — DECISÃO: as alavancas do #167 nascem LIGADAS (e por quê, com número) |
| `216-analise.py` | #216 | 2026-09-29 | !/usr/bin/env python3 |
| `216-combos.sh` | #216 | 2026-09-29 | !/usr/bin/env bash |
| `216-combos2.sh` | #216 | 2026-09-29 | !/usr/bin/env bash |
| `216-combos3.sh` | #216 | 2026-09-29 | !/usr/bin/env bash |
| `216-combos4.sh` | #216 | 2026-09-29 | !/usr/bin/env bash |
| `216-decisivo.sh` | #216 | 2026-09-29 | !/usr/bin/env bash |
| `216-eco-a.txt` | #216 | 2026-09-29 | [serial] live_barge_rep.sh esperando a trava de modelo (dona: live_lock_freeze.sh)… |
| `216-eco-ab.txt` | #216 | 2026-09-29 | servidor http://127.0.0.1:64665 · stub :64664 · repetições=20 |
| `216-eco-abc.txt` | #216 | 2026-09-29 | (node:8880) Warning: The 'NO_COLOR' env is ignored due to the 'FORCE_COLOR' env being set. |
| `216-eco-abc2.txt` | #216 | 2026-09-29 | [serial] live_barge_rep.sh esperando a trava de modelo (dona: live_barge_rep.sh)… |
| `216-eco-abcA.txt` | #216 | 2026-09-29 | [serial] live_barge_rep.sh esperando a trava de modelo (dona: live_barge_rep.sh)… |
| `216-eco-abcB.txt` | #216 | 2026-09-29 | (node:47416) Warning: The 'NO_COLOR' env is ignored due to the 'FORCE_COLOR' env being set. |
| `216-eco-ac.txt` | #216 | 2026-09-29 | (node:14968) Warning: The 'NO_COLOR' env is ignored due to the 'FORCE_COLOR' env being set. |
| `216-eco-baseline.txt` | #216 | 2026-09-29 | servidor http://127.0.0.1:63835 · stub :63834 · repetições=20 |
| `216-eco-baseline2.txt` | #216 | 2026-09-29 | [serial] live_barge_rep.sh esperando a trava de modelo (dona: live_barge_rep.sh)… |
| `216-eco-baselineB.txt` | #216 | 2026-09-29 | (node:65392) Warning: The 'NO_COLOR' env is ignored due to the 'FORCE_COLOR' env being set. |
| `216-eco-baselineC.txt` | #216 | 2026-09-29 | [serial] live_barge_rep.sh esperando a trava de modelo (dona: live_barge_rep.sh)… |
| `216-eco-bc.txt` | #216 | 2026-09-29 | (node:29186) Warning: The 'NO_COLOR' env is ignored due to the 'FORCE_COLOR' env being set. |
| `216-eco-c.txt` | #216 | 2026-09-29 | servidor http://127.0.0.1:64213 · stub :64212 · repetições=20 |
| `216-limpo.sh` | #216 | 2026-09-29 | !/usr/bin/env bash |
| `216-quieto.sh` | #216 | 2026-09-29 | !/usr/bin/env bash |
| `216-resumo.txt` | #216 | 2026-09-29 | true-baseline  20/20 barge · === MOTOR (cumulativo da rodada): {"barge_ativos": 0, "barge_falsos": 0, "estado" |
| `216-tabela.sh` | #216 | 2026-09-29 | !/usr/bin/env bash |
| `216-true-a.txt` | #216 | 2026-09-29 | [serial] live_barge_rep.sh esperando a trava de modelo (dona: live_ui.sh)… |
| `216-true-ab.txt` | #216 | 2026-09-29 | servidor http://127.0.0.1:62760 · stub :62759 · repetições=20 |
| `216-true-abc.txt` | #216 | 2026-09-29 | servidor http://127.0.0.1:63575 · stub :63574 · repetições=20 |
| `216-true-ac.txt` | #216 | 2026-09-29 | servidor http://127.0.0.1:63007 · stub :63006 · repetições=20 |
| `216-true-b.txt` | #216 | 2026-09-29 | servidor http://127.0.0.1:49755 · stub :49754 · repetições=20 |
| `216-true-baseline.txt` | #216 | 2026-09-29 | [serial] live_barge_rep.sh esperando a trava de modelo (dona: live_ui.sh)… |
| `216-true-baseline3.txt` | #216 | 2026-09-29 | [serial] live_barge_rep.sh esperando a trava de modelo (dona: live_barge_rep.sh)… |
| `216-true-bc.txt` | #216 | 2026-09-29 | servidor http://127.0.0.1:63226 · stub :63225 · repetições=20 |
| `216-true-c.txt` | #216 | 2026-09-29 | servidor http://127.0.0.1:62472 · stub :62471 · repetições=20 |
| `216-vao-ab.txt` | #216 | 2026-09-29 | (node:16488) Warning: The 'NO_COLOR' env is ignored due to the 'FORCE_COLOR' env being set. |
| `216-vao-abB.txt` | #216 | 2026-09-29 | (node:79428) Warning: The 'NO_COLOR' env is ignored due to the 'FORCE_COLOR' env being set. |
| `216-vao-abc.txt` | #216 | 2026-09-29 | (node:46486) Warning: The 'NO_COLOR' env is ignored due to the 'FORCE_COLOR' env being set. |
| `216-vao-abcB.txt` | #216 | 2026-09-29 | (node:52005) Warning: The 'NO_COLOR' env is ignored due to the 'FORCE_COLOR' env being set. |
| `216-vao-baseline.txt` | #216 | 2026-09-29 | [serial] live_barge_rep.sh esperando a trava de modelo (dona: live_barge_rep.sh)… |
| `216-vao-quieto-ab.txt` | #216 | 2026-09-29 | [serial] live_barge_rep.sh esperando a trava de modelo (dona: live_barge_rep.sh)… |
| `216-vao-quieto-abB.txt` | #216 | 2026-09-29 | (node:60154) Warning: The 'NO_COLOR' env is ignored due to the 'FORCE_COLOR' env being set. |
| `216-vao-quieto-abc.txt` | #216 | 2026-09-29 | [serial] live_barge_rep.sh esperando a trava de modelo (dona: live_barge_rep.sh)… |
| `216-vao-quieto-abcB.txt` | #216 | 2026-09-29 | (node:57106) Warning: The 'NO_COLOR' env is ignored due to the 'FORCE_COLOR' env being set. |
| `216-vao.sh` | #216 | 2026-09-29 | !/usr/bin/env bash |
| `217-mordida.txt` | #217 | 2026-09-29 | PROVA DE MORDIDA #217 — tests/live_lock_freeze.sh (par do invariante |
| `217-mordida2.txt` | #217 | 2026-09-29 | Prova de EFEITO do #209 (item 2 do gate #215) — captura crua de |
| `220-escrita-de-terceiro.sh` | #220 | 2026-09-29 | !/usr/bin/env bash |
| `220-escrita-de-terceiro.txt` | #220 | 2026-09-29 | ══ 1/3 — SEM escritor (árvore parada: o par tem de usar a prova EXATA) |
| `221-contadores-perdedor.txt` | #221 | 2026-10-01 | === #221 — ANTES (app.py do HEAD 3490bcc: o perdedor zera os contadores fora do lock) === |
| `222-id-repetido.py` | #222 | 2026-10-01 | """#222 — id do cliente JÁ VIVO: a sessão antiga saía do registro e ficava viva. |
| `222-id-repetido.txt` | #222 | 2026-10-01 | === #222 — ANTES (marcação removida: a antiga fica viva e fora do registro) === |
| `223-mordida-harness.txt` | #223 | 2026-10-01 | ══ MORDIDA 1/2 — app.py COMO ESTÁ (o fix): tem de sair 0 |
| `223-mordida.txt` | #223 | 2026-10-01 | GATE #223 — MORDIDA=1 — 2026-09-29T21:12:35Z |
| `223-revert-hand.py` | #223 | 2026-10-01 | """GATE #223 item 2 — o revert do script é MESMO a forma pré-#209? |
| `223-verifica.sh` | #223 | 2026-10-01 | !/usr/bin/env bash |
| `224-tmpdir-sem-credencial.txt` | #224 | 2026-09-29 | 224 — a suíte não deixa mais cópia do `settings.json` (com a chave) em TMPDIR |
| `225-EVIDENCIA.md` | #225 | 2026-10-01 | 225 — o assistente tem de CALAR quando o usuário fala (corte no onset) |
| `225-antes-eco.txt` | #225 | 2026-10-01 | (node:51832) Warning: The 'NO_COLOR' env is ignored due to the 'FORCE_COLOR' env being set. |
| `225-antes-vao-default.txt` | #225 | 2026-10-01 | [serial] live_barge_rep.sh esperando a trava de modelo (dona: live_barge_rep.sh)… |
| `225-antes-vao-eco-tocando.txt` | #225 | 2026-10-01 | (node:59163) Warning: The 'NO_COLOR' env is ignored due to the 'FORCE_COLOR' env being set. |
| `225-celulas.log` | #225 | 2026-10-01 | (sem descrição) |
| `225-celulas.sh` | #225 | 2026-10-01 | !/usr/bin/env bash |
| `225-depois-eco.txt` | #225 | 2026-10-01 | (node:1711) Warning: The 'NO_COLOR' env is ignored due to the 'FORCE_COLOR' env being set. |
| `225-depois-vao-default.txt` | #225 | 2026-10-01 | (node:70477) Warning: The 'NO_COLOR' env is ignored due to the 'FORCE_COLOR' env being set. |
| `225-depois-vao-eco-tocando.txt` | #225 | 2026-10-01 | [serial] live_barge_rep.sh esperando a trava de modelo (dona: 180-recheck.sh)… |
| `225-extra.sh` | #225 | 2026-10-01 | !/usr/bin/env bash |
| `226-229-pytest-final.txt` | #226 | 2026-10-01 | pytest FINAL (gates #226/#229) — 2026-10-01T05:34:39Z · HEAD=fd5d03d |
| `226-229-pytest-final2.txt` | #226 | 2026-10-01 | pytest FINAL2 (gates #226/#229) — 2026-10-01T05:41:00Z · HEAD=7cab566 |
| `226-229-pytest3.txt` | #226 | 2026-10-01 | pytest (gates #226/#229, 3ª) — 2026-10-01T05:27:34Z · HEAD=fd5d03d |
| `226-escrita-de-terceiro.txt` | #226 | 2026-10-01 | gate #226 — 2026-10-01T05:23:38Z · HEAD=68ec138 |
| `226-sabotagens.sh` | #226 | 2026-10-02 | !/usr/bin/env bash |
| `226-sabotagens.txt` | #226 | 2026-10-01 | gate #226 — sabotagens — 2026-10-01T05:24:04Z |
| `227-mordidas.txt` | #227 | 2026-10-02 | === #227 — MUTAÇÃO 1: tolerância desligada no par do /api/build === |
| `228-janela-mid-start.txt` | #228 | 2026-10-01 | 228 — `close()` DENTRO do `_LiveWorker.start()` deixa filho órfão (residual do #207) |
| `228-probe-com-fix.txt` | #228 | 2026-10-01 | · close ANTES do spawn: start() 130 ms · _saiu=True · pipe._worker=None · filho vivo=False · ativo=False |
| `228-probe-sem-fix.txt` | #228 | 2026-10-01 | · close ANTES do spawn: start() 131 ms · _saiu=True · pipe._worker=None · filho vivo=True · ativo=True |
| `228-teste-com-fix.txt` | #228 | 2026-10-01 | ........................                                                 [100%] |
| `228-teste-sem-fix.txt` | #228 | 2026-10-01 | tests/test_live_dsh.py:519: AssertionError |
| `229-final.txt` | #229 | 2026-10-01 | gate #229 — rodada final — 2026-10-01T05:29:54Z · HEAD=fd5d03d |
| `229-probe-e-mordida.txt` | #229 | 2026-10-01 | gate #229 — probe e mordida — 2026-10-01T05:20:57Z |
| `229-probe-ttslive.py` | #229 | 2026-10-22 | """#229 (família do #207/#228) — REPRO DETERMINÍSTICO da leitura dupla em `_tts_live`. |
| `229-pytest.txt` | #229 | 2026-10-01 | pytest (gate #229) — 2026-10-01T04:48:30Z · HEAD=0587865 |
| `229-pytest2.txt` | #229 | 2026-10-01 | pytest (gate #229, 2ª) — 2026-10-01T05:13:01Z · HEAD=78a9c62 |
| `230-celulas.log` | #230 | 2026-10-01 | (sem descrição) |
| `230-celulas.sh` | #230 | 2026-10-01 | !/usr/bin/env bash |
| `230-default-sem-env.txt` | #230 | 2026-10-01 | Config() default             : True True False |
| `230-eco-baseline.txt` | #230 | 2026-10-01 | servidor http://127.0.0.1:58816 · stub :58815 · repetições=20 |
| `230-eco-default.txt` | #230 | 2026-10-01 | servidor http://127.0.0.1:58196 · stub :58195 · repetições=20 |
| `230-live_ui.txt` | #230 | 2026-10-01 | [serial] live_ui.sh esperando a trava de modelo (dona: live_barge_rep.sh)… |
| `230-vao-default.txt` | #230 | 2026-10-01 | servidor http://127.0.0.1:57268 · stub :57264 · repetições=20 |
| `231-mordida-dos-3-bugs.txt` | #231 | 2026-10-01 | MORDIDA DOS 3 BUGS DE ZSH (task #231, pedido do gate #232 item 6) |
| `231-passo1-2arodada-antes.txt` | #231 | 2026-10-01 | rev: 3e663a6 branch: prod-3e663a60ff3a |
| `231-passo1-2arodada-depois.txt` | #231 | 2026-10-01 | === DEPOIS (2a rodada) 2026-10-01 02:01:36 === |
| `231-passo1-2arodada.txt` | #231 | 2026-10-01 | == deploy (rev alvo 3e663a60ff3a… (3e663a6)) |
| `231-passo1-antes.txt` | #231 | 2026-10-01 | === ANTES do passo 1 (read-only) — 2026-10-01 01:36:55 -03 === |
| `231-passo1-deploy.txt` | #231 | 2026-10-01 | == deploy (rev alvo 3e663a60ff3a… (3e663a6)) |
| `231-passo1-depois.txt` | #231 | 2026-10-01 | === DEPOIS do passo 1 — 2026-10-01 01:43:57 -03 === |
| `231-passo1-estado-final.txt` | #231 | 2026-10-01 | === ESTADO FINAL DA PRODUCAO — passo 1, 2026-10-01 02:02:10 -03 === |
| `231-passo2-antes.txt` | #231 | 2026-10-02 | === PASSO 2 — ANTES (read-only) — 2026-10-01T03:29:10-03:00 === |
| `231-passo2-apply.txt` | #231 | 2026-10-02 | == deploy (rev alvo d25269369284… (d252693)) |
| `231-passo2-deploy.txt` | #231 | 2026-10-02 | === PASSO 2 — DEPLOY (--apply --sem-push) — 2026-10-01T03:29:29-03:00 === |
| `231-passo2-depois.txt` | #231 | 2026-10-02 | === PASSO 2 — DEPOIS (read-only) — 2026-10-01T03:35:32-03:00 === |
| `231-passo2-dryrun.txt` | #231 | 2026-10-02 | == deploy (rev alvo d25269369284… (d252693)) |
| `231-passo2-evidencia.txt` | #231 | 2026-10-02 | == .deploy-mini-estado (rollback armado) |
| `231-passo2-reexecucao.txt` | #231 | 2026-10-02 | == deploy (rev alvo d25269369284… (d252693)) |
| `231-passo2-smoke-depois.txt` | #231 | 2026-10-02 | === PASSO 2 — SMOKE DEPOIS (sonda WS com --http1.1, igual ao navegador) — 2026-10-01T03:35:10-03:00 === |
| `231-passo2-smoke-pos248.txt` | #231 | 2026-10-02 | == smoke (rev alvo 289892c89ec1…) |
| `231-passo2-ws-publico.txt` | #231 | 2026-10-02 | === WS REAL pelo caminho público (h1 upgrade, igual ao navegador) — 2026-10-01T03:31:05-03:00 === |
| `232-contraprova-real.sh` | #232 | 2026-10-01 | !/bin/zsh |
| `232-contraprova-real.txt` | #232 | 2026-10-01 | == contraprova REAL em lisboa@192.168.15.34 — 2026-10-01T05:10:20Z |
| `232-contraprova.sh` | #232 | 2026-10-01 | !/bin/zsh |
| `232-contraprova.txt` | #232 | 2026-10-01 | [ok]   D1 deploy: deploy (sem --apply) não escreveu NADA (árvore, HEAD, origin, serviço) |
| `232-mordidas.sh` | #232 | 2026-10-01 | !/bin/bash |
| `232-mordidas.txt` | #232 | 2026-10-01 | mordidas do gate #232 — 2026-10-01T05:00:42Z |
| `232-pytest-final.txt` | #232 | 2026-10-01 | pytest tests/ -q — 2026-10-01T04:37:07Z · HEAD=0587865 |
| `232-real-R1_recon.txt` | #232 | 2026-10-01 | == host |
| `232-real-R2_compare.txt` | #232 | 2026-10-01 | == rev |
| `232-real-R3_smoke_--sem-tts.txt` | #232 | 2026-10-01 | == smoke (rev alvo d25269369284…) |
| `232-real-R4_deploy_dry-run.txt` | #232 | 2026-10-01 | == deploy (rev alvo d25269369284… (HEAD)) |
| `232-real-R5_smoke_tts.txt` | #232 | 2026-10-01 | == smoke (rev alvo d25269369284…) |
| `232-real-R6_smoke_no_rev_publicado.txt` | #232 | 2026-10-01 | == smoke (rev alvo 3e663a60ff3a…) |
| `232-regate-md5.txt` | #232 | 2026-10-01 | 18b581a4ebbdf120ca8ea22fec979433 |
| `233-234-mordidas.sh` | #233 | 2026-10-01 | !/bin/zsh |
| `233-234-mordidas.txt` | #233 | 2026-10-01 | == F1 — preview de deps na árvore do mini |
| `235-mordidas.sh` | #235 | 2026-10-01 | !/bin/zsh |
| `235-mordidas.txt` | #235 | 2026-10-01 | == 1 — paridade do index SEM normalizar o nonce |
| `235-smoke-read-only.txt` | #235 | 2026-10-01 | === SMOKE READ-ONLY CONTRA O MINI REAL (alvo atual 3e663a6) — 2026-10-01 02:03:14 -03 === |
| `236-gate.sh` | #236 | 2026-10-01 | !/bin/zsh |
| `236-gate.txt` | #236 | 2026-10-01 | [ok]   alvo sem rotas: deploy --apply rc=0 e smoke sem FALHA (esperado-para-o-alvo não derruba) |
| `236-real.txt` | #236 | 2026-10-01 | 236 — rodada real read-only no mini (alvo ATUAL 3e663a6) — 2026-10-01T06:03:32Z |
| `238-falso-passa.txt` | #238 | 2026-10-01 | gate #238 — prova do falso PASSA por bytecode velho — 2026-10-01T05:49:14Z |
| `238-mordidas-com-helper.txt` | #238 | 2026-10-01 | mordidas do gate #232 — 2026-10-01T05:49:37Z |
| `240-mordidas.sh` | #240 | 2026-10-01 | !/bin/zsh |
| `240-mordidas.txt` | #240 | 2026-10-01 | == 1 — default volta a ser caminho FIXO (colide entre execuções) |
| `240-paralelo-repro.txt` | #240 | 2026-10-01 | === REPRO DO RELATO (#240): duas rodadas da suíte do arquivo EM PARALELO — 2026-10-01 02:39:40 -03 === |
| `241-hostil-effort.txt` | #241 | 2026-10-01 | 241 (achado de fora): o arquivo do DONO decide o veredito da suíte pelo campo chat_dsh_effort |
| `241-probe-com-fix.txt` | #241 | 2026-10-01 | ✔ sem furo: a 2ª leitura achou o worker |
| `241-probe-sem-fix.txt` | #241 | 2026-10-01 | ✘ FURO: AttributeError na 2ª leitura de `_worker` ('NoneType' object has no attribute 'ativo') |
| `241-teste-com-fix.txt` | #241 | 2026-10-01 | [live-dsh] [dsh stderr] FAKE_ACP PROMPT "oi" |
| `241-teste-sem-fix.txt` | #241 | 2026-10-01 | if not self._worker_indisponivel and _worker_habilitado(): |
| `241-ttslive-leitura-dupla.txt` | #241 | 2026-10-01 | 241 — `_tts_live` lê `self._worker` DUAS vezes: `close()` no meio estoura AttributeError |
| `241-worker-real.txt` | #241 | 2026-10-01 | ...                                                                      [100%] |
| `242-skew.txt` | #242 | 2026-10-02 | === #242 — SABOTAGEM S1b: `_BUILD_TS_HASH` no futuro desliga a tolerância por mtime === |
| `243-mordidas.sh` | #243 | 2026-10-01 | !/bin/zsh |
| `243-mordidas.txt` | #243 | 2026-10-01 | == 1 — 202-gate.sh perde o RUN (backup de settings volta a ser global) |
| `243-varredura-tmp.txt` | #243 | 2026-10-01 | VARREDURA DA CASA — CAMINHO FIXO EM /tmp (classe #12/#83/#224/#240) — task #243 |
| `244-live-substituida-status.png` | #244 | 2026-10-21 | (imagem) |
| `244-live-substituida.png` | #244 | 2026-10-21 | (imagem) |
| `244-live_ui.txt` | #244 | 2026-10-02 | Exception occurred during processing of request from ('127.0.0.1', 54047) |
| `244-mordida-A.txt` | #244 | 2026-10-02 | servidor de teste em http://127.0.0.1:63347 (stub :63346) |
| `244-mordida-B.txt` | #244 | 2026-10-02 | servidor de teste em http://127.0.0.1:63807 (stub :63804) |
| `244-mordidas.sh` | #244 | 2026-10-02 | !/usr/bin/env bash |
| `244-mordidas.txt` | #244 | 2026-10-02 | md5 original: b7583e6400dac36a19f1f3242db2d22c |
| `245-contraprova-passo2.txt` | #245 | 2026-10-02 | === GATE PÓS-DEPLOY PASSO 2 — CONTRAPROVA POR FORA (read-only, sem apply) — task_b559e5bd #245 === |
| `245-live-turno-prod-resumo.txt` | #245 | 2026-10-02 | === DIGEST do turno REAL no Live da produção (full: 245-live-turno-prod.txt; script: .py) === |
| `245-live-turno-prod.py` | #245 | 2026-10-02 | !/usr/bin/env python3 |
| `245-live-turno-prod.txt` | #245 | 2026-10-02 | turno REAL contra wss://tts.the-dudes.com/api/live/ws · fala de 3.5s de voices/320afe68b8.wav |
| `245-prod-ia-sem-provedor.txt` | #245 | 2026-10-02 | === PÓS-SUBIDA (read-only, contra a produção) — o caminho de IA não tem provedor === |
| `246-antes.txt` | #246 | 2026-10-01 | repro #246 — settings do dono com chat_dsh_effort=low (cópia hostil) |
| `246-depois.txt` | #246 | 2026-10-01 | 1) hostil low (o caso do achado) — esperado: 25 passed |
| `246-verifica.sh` | #246 | 2026-10-01 | !/usr/bin/env bash |
| `247-ws-h2-vs-h1.txt` | #247 | 2026-10-02 | === ACHADO #247: a sonda WS do smoke false-vermelha o caminho público (curl h2 × Cloudflare) — 2026-10-01T03:3 |
| `248-mordida.txt` | #248 | 2026-10-02 | +    where CompletedProcess(args=['/bin/zsh', '-f', '/Users/lisboa/Documents/tts-rod/remote/deploy_mini.sh', ' |
| `248-sonda-h2-h1-no-ar.txt` | #248 | 2026-10-02 | Sonda h2 × h1 do /api/live/ws contra o AR (#248, gate pós-deploy passo 2) |
| `249-live-prod-RESUMO.txt` | #249 | 2026-10-02 | === 249 · RESUMO — caminho de ÁUDIO do Live em PRODUÇÃO (read-only) === |
| `249-live-prod-estado.txt` | #249 | 2026-10-02 | === 249 · ESTADO DA PRODUÇÃO (read-only, 2026-10-02) === |
| `249-live-prod-sessao.py` | #249 | 2026-10-02 | !/usr/bin/env python3 |
| `249-live-prod-turnos.txt` | #249 | 2026-10-02 | === 249 · SESSÃO REAL (modo turnos) — LAN 192.168.15.34:7860, rev d252693 === |
| `249-live-prod-worker-antes.txt` | #249 | 2026-10-02 | === 249 · WORKER/ORPHAN CHECK (read-only) — ANTES da sessão === |
| `249-live-prod-worker-durante.txt` | #249 | 2026-10-02 | === 249 · WORKER/ORPHAN CHECK — DURANTE a sessão WS aberta (2ª tentativa) === |
| `252-aviso-boot.txt` | #252 | 2026-10-02 | 252 — aviso de boot_ms ancorado no marcador do restart (.deploy-mini-boot) |
| `257-repro-index.txt` | #257 | 2026-10-02 | == REPRO ANTES do fix (GIT_INDEX_FILE=/tmp/probe-257 + pytest) |
| `331-mobile-obs-360.png` | #331 | — | (imagem) |
| `331-mobile-obs-390.png` | #331 | — | (imagem) |
| `375-F5-ns-rnnoise.md` | #375 | 2026-10-22 | 375/F5 — NS do navegador + RNNoise: o filtro custa precisão do STT |
| `440-rastro-sinal-mascara.txt` | #440 | — | == ANTES da máscara (vazava) == |
| `442-rnnoise-vad-x-stt.md` | #442 | — | 442 — decisão do default do RNNoise: o filtro serve ao VAD, não ao STT |
| `antes-xss-vozes.png` | — | 2026-09-24 | (imagem) |
| `audit-fe-mobile-gerar-pos-fix.png` | — | 2026-10-21 | (imagem) |
| `audit-fe-mobile-gerar.png` | — | 2026-10-21 | (imagem) |
| `audit-fe-mobile-tradutor-pos-fix.png` | — | 2026-10-21 | (imagem) |
| `chave-admin-campos-livres.png` | — | 2026-09-24 | (imagem) |
| `chave-de-uso-campos-travados.png` | — | 2026-09-24 | (imagem) |
| `depois-chave-sessao.png` | — | 2026-09-24 | (imagem) |
| `depois-xss-vozes.png` | — | 2026-09-24 | (imagem) |
| `dsh-acp-3pontas-persona.txt` | — | 2026-09-29 | == busca de turno LENTO com trace (prompt persona) == |
| `dsh-acp-3pontas.txt` | — | 2026-09-29 | == 6 rodada(s) · prompt curto · trace do bridge ligado == |
| `dsh-acp-DSH-4b-EVIDENCIA.md` | — | 2026-09-29 | DSH-4b — o BLOCO da resposta: onde a resposta é retida |
| `dsh-acp-chunks-after.txt` | — | 2026-09-29 | == A. resposta CURTA == |
| `dsh-acp-chunks-before.txt` | — | 2026-09-29 | == A. resposta CURTA == |
| `dsh-acp-concorrencia-3x.txt` | — | 2026-09-29 | == TCP cru no provedor · curta · 1 rodada(s) · host=api.ds.model.craton.app · effort=none == |
| `dsh-acp-daemon-smoke-after.jsonl` | — | 2026-09-29 | (sem descrição) |
| `dsh-acp-daemon-smoke-before.jsonl` | — | 2026-09-29 | (sem descrição) |
| `dsh-acp-gap-probe.py` | — | 2026-09-29 | """DSH-4b: assinatura de INTERVALOS entre deltas, nas duas pontas. |
| `dsh-acp-partial-spike.py` | — | 2026-09-29 | """DSH-4a: o patch do bridge fez o ACP streamar? Mede a MARCA `partial` e o ganho. |
| `dsh-acp-rota-alternativa.py` | — | 2026-09-29 | """DSH-4b: o giro existe também em OUTRA rota? (contornar de produto) |
| `dsh-acp-stall-por-tamanho.txt` | — | 2026-09-29 | curta ### |
| `dsh-acp-stream-EVIDENCIA.md` | — | 2026-09-29 | DSH-4a — evidência do patch do bridge ACP (streaming incremental) |
| `dsh-acp-tres-pontas.py` | — | 2026-10-22 | """DSH-4b: correlaciona as TRÊS pontas no mesmo turno. |
| `dsh-acp-wire-probe.py` | — | 2026-09-29 | """DSH-4b: o giro de 16–42 s acontece NO FIO ou é buffering de biblioteca? |
| `dsh-chunks-spike.py` | — | 2026-09-29 | """Spike: o ACP entrega chunk incremental? Mede timestamps por chunk, 2 perfis.""" |
| `dsh-live-latency-spike.py` | — | 2026-09-29 | """Mede a latência do caminho dsh no pipeline REAL e CARIMBA a rota (#160/#154). |
| `dsh-provider-sse-spike.py` | — | 2026-09-29 | """Mede TTFT do provedor dsflash DIRETO (SSE), para saber o ganho se o ACP streamasse.""" |
| `live-barge-in-ui.txt` | — | 2026-09-25 | Live (task #115) — E2E do barge-in pela UI, ponta a ponta (tests/live_ui.sh) |
| `live-barge-in.txt` | — | 2026-09-25 | Live (task #92) — medicao de barge-in com eco do TTS |
| `papel-da-chave-ui.png` | — | 2026-09-24 | (imagem) |
| `passo2-prep-dry-run.txt` | — | 2026-10-01 | === PREP PASSO 2 — dry-run read-only com alvo = HEAD (d252693) — 2026-10-01 03:07:26 === |
| `passo2-prep-smoke-head.txt` | — | 2026-10-01 | === PREP DO PASSO 2: smoke read-only com o alvo = HEAD atual (produção ainda no corte) — 2026-10-01 02:56:19 - |
