# #982 — escala crua do ECAPA implementada: smoke #533 OK (0 turno falso), barge do terceiro 0/1530

Data: 2026-11-08. Branch `wt/audio-982-cru`, sobre o #984 (2937b15). Decisão do PM:
opção A (janela de 1 s em 0,35 cru) e piso do `misto` recalibrado para o cru.
Calibração: `evidence/982-calibra-mls.md`.

## Régua

| uso | ECAPA (cru) | Resemblyzer (legado, intocado) |
|---|---|---|
| identidade ≥3 s | 0,44 (alvo "1"/"0.1": 0,39/0,48) | `speaker_threshold` (0,6); com alvo, AS-norm 2,906/3,173 |
| janela de 1 s do Live | 0,35 (rigor "0.1": 0,39) | 0,44 cru |

## Smoke #533 (`MINUTOS=10 smoke_so_minha_voz.py`, corpus LibriSpeech do R4)

| limiar da janela | dono (cobertura) | terceiro (turnos falsos/10 min) | misto (cobertura) | veredito |
|---|---|---|---|---|
| AS-norm 1,8 (antes) | 90,0 % | 0 | 40 % | OK, mas a coorte contém os locutores do corpus |
| cru 0,31 | 90,0 % | **48** | 20 % | REPROVADO |
| cru 0,32–0,44 (3 min) | 89,6 % | 0 | 9,7 % | — |
| **cru 0,35 (10 min)** | **90,0 %** | **0** | **10,0 %** (piso novo: 10 %) | **OK** |

A latência do portão ficou em p50 27–33 ms e p95 29–36 ms.

## Bancada do #984 (dono E2E sobre o eco, 17 terceiros PT; `evidence/982-barge-escala-crua.py`)

| escala | dono aceito | 0,4 s | 0,6 s | 1,4 s | terceiro aceito |
|---|---|---|---|---|---|
| antes do #984 (AS-norm, trecho inteiro) | 145/240 | 3/48 | 11/48 | 48/48 | 64/1530 (4,2 %) |
| #984 (AS-norm 1,8, fala recortada) | 204/240 | 29/48 | 34/48 | 48/48 | 28/1530 (1,8 %) |
| cru 0,31 | 169/240 | 14/48 | 26/48 | 47/48 | 0/1530 |
| **cru 0,35 (final)** | **156/240** | **10/48** | **25/48** | **43/48** | **0/1530** |

O preço do 0,35 é cobertura no barge muito curto (0,4 s). Em troca, nenhum terceiro
passa e o smoke não abre turno falso.

## O "eco sozinho aceito"

4/40 trechos de eco puro saem `ok` nas duas escalas. Todos são da mesma voz de
resposta (`1e8e1479ae`), cujo cosseno com o perfil "Dono E2E" é **0,596**: no estado do
E2E, essa voz do TTS é um clone do dono (`evidence/982-eco-clone-do-dono.py`). A
identidade não separa o dono do próprio clone, por desenho. Quem trata isso é o filtro
`eco` da cadeia (antes da `identidade`) e o anti-clone, não o limiar.

## Limitação conhecida (registrada no README e no MORDIDAS)

Com o dono e um terceiro falando ao mesmo tempo, sem separação, o portão aceita ~10 %
das janelas do dono. O piso antigo de 35 % veio do AS-norm com coorte vazada. Proteger
contra fala sobreposta depende da separação do turno (#533 item 2), hoje desligada. O
portão do Live segue **desligado** no mini, e a tela não tem controle dele (#528), então
nenhuma tela promete essa proteção.
