# Deploy remoto (servidores RTX) — inventário

> **Status: PARCIAL.** Os caminhos, portas e variáveis vêm dos arquivos versionados
> (fato). O **host/IP e o usuário do servidor RTX ainda NÃO estão confirmados** pelo
> dono — em 2026-09-24 uma varredura inteira não achou a máquina (seção 2). Tudo que
> depende de confirmação está marcado com `❓`.
>
> Última atualização: 2026-09-24 (infra-remote, tasks #16 e #27). Como atualizar: assim
> que o dono confirmar host/serviço, rode `remote/deploy.sh recon` (§4) e troque os `❓`
> por valores reais; mantenha a data no topo.

## 1. Inventário

### Confirmado pelo código versionado
| peça | valor | fonte |
|---|---|---|
| Serviço do OmniVoice | `omnivoice-tts.service` (systemd), drop-in define `CUDA_VISIBLE_DEVICES=1,0` | `remote/omni_server.py` (docstring) |
| Como o OmniVoice sobe | `uvicorn server:app --host 0.0.0.0 --port 8800` | idem |
| Porta do OmniVoice | **8800** (exposta pelo drop-in) | idem |
| Arquivo no servidor (OmniVoice) | `/root/omnivoice/server.py` | `VOICES_DIR`/`model_local` absolutos no código |
| Vozes clonadas | `/root/omnivoice/voices` (`.wav` + `.txt` de `ref_text`) | `remote/omni_server.py:41` |
| Modelo do OmniVoice | `/root/omnivoice/model_local` (bf16) | `remote/omni_server.py:62` |
| ffmpeg do servidor | `/usr/local/bin/ffmpeg8` | `remote/omni_server.py:39` |
| GPUs esperadas | 2 NVIDIA: `cuda:0` = RTX 4090 (OmniVoice+Whisper), `cuda:1` = RTX 4070 (tradutor 14B, thread isolada) | `remote/omni_server.py` (docstring) |
| Arquivo no servidor (Voxtral) | `/root/voxtral/server.py` | `remote/voxtral_server.py` (docstring) |
| Deploy (os dois servidores) | `remote/deploy.sh recon|compare|deploy --apply|rollback --apply|smoke` (§4) | `remote/deploy.sh` |
| Modelo do Voxtral | HF `mistralai/Voxtral-Small-24B-2507` (bnb nf4, `VOXTRAL_REPO` troca) | `remote/voxtral_server.py:35` |
| VAD (anti-alucinação) | `_load_vad()` pede **ONNX explícito** e cai no jit do torch com aviso no log se `onnxruntime` faltar | `remote/voxtral_server.py` |
| Como conferir qual VAD subiu | `GET /health` devolve `"vad": "onnx"` \| `"torch-jit"` | idem |
| Autenticação | `OMNI_API_KEY` e `VOXTRAL_API_KEY` **obrigatórias** (fail closed: sem elas o processo não sobe; escape hatch `<PREFIXO>_ALLOW_NO_AUTH=1`) | `remote/auth_policy.py`, README §"servidores `remote/`" |
| Arquivo de política no servidor | `auth_policy.py` ao lado do `server.py` (`/root/omnivoice/`, `/root/voxtral/`) — **copiar junto no deploy** | `remote/auth_policy.py` |

### Não confirmado (❓)
| peça | o que falta |
|---|---|
| ❓ host/IP do servidor RTX | **não está em lugar nenhum do repo** (o commit `a14c596` anonimizou os exemplos: `rtx-host`, `seu-dominio`). Pedir ao dono. |
| ❓ usuário de SSH | os deploys citam caminhos `/root/...`, então provavelmente `root` — confirmar |
| ❓ serviço/porta do Voxtral | o código não declara porta (`uvicorn` fica no unit do lado do servidor); o app aponta via `remote_stt_base_url` em `settings.json`. Confirmar nome do unit e porta |
| ❓ `omnivoice-tts.service` na máquina | o nome vem da docstring e casa com a ficha do time, mas nunca foi lido na máquina |
| ❓ RAM/VRAM e pressão de memória | nunca medida; a máquina estava inalcançável |

### Variáveis de ambiente esperadas nos servidores
`OMNI_API_KEY`, `OMNI_MAX_UPLOAD_MB`, `OMNI_TTS_WORKERS`, `OMNI_ASR_WORKERS`,
`OMNI_IO_WORKERS`, `OMNI_LOAD_WHISPER`, `OMNI_LOAD_TURBO`, `OMNI_LOAD_MT_FAST`,
`OMNI_MT_REPO`, `OMNI_MT_DEV`, `OMNI_MT_FAST_REPO`, `OMNI_MT_FAST_DEV` (omni) ·
`VOXTRAL_API_KEY`, `VOXTRAL_REPO`, `VOXTRAL_MAX_NEW`, `VOXTRAL_MIN_SPEECH`,
`VOXTRAL_MAX_UPLOAD_MB` (voxtral). Escapes (opcionais, uma por servidor):
`OMNI_ALLOW_NO_AUTH`, `VOXTRAL_ALLOW_NO_AUTH`.

### Autenticação: política e migração (task #26)
O servidor sobe em `0.0.0.0` (LAN inteira): sem chave, o middleware liberava
**todos** os endpoints e nada avisava. Agora a política é explícita e fail
closed, em `remote/auth_policy.py`:

| ambiente | efeito |
|---|---|
| `<PREFIXO>_API_KEY` preenchida | exige `Authorization: Bearer <chave>` ou `X-API-Key` em tudo, menos `/health` e `OPTIONS` |
| chave ausente, sem escape | o processo **não sobe** (mensagem no journal, antes de carregar os modelos na VRAM) |
| chave ausente + `<PREFIXO>_ALLOW_NO_AUTH=1` | sobe **aberto** (modo legado), com aviso no log |

`GET /health` fica fora da chave e devolve `"auth": "required" | "open"` — é como
se confere a política do servidor no ar sem ssh. Comportamento antigo: qualquer
valor em `_ALLOW_NO_AUTH` que não seja exatamente `1` conta como desligado.

Migração (num host onde o unit **já** tem a chave, nada muda; onde não tem, usar
a escape hatch ou definir a chave antes do restart): o app manda a chave por
`remote_api_key` / `remote_stt_key` (Configurações → Modelos remotos) e é
`Bearer` — não precisa mexer no app se a chave for a mesma dos dois lados.

Ao subir código novo, copie **os dois** arquivos (`server.py` e `auth_policy.py`);
faltando o `auth_policy.py` o import falha e o serviço não sobe (barulhento, não
silencioso).

### Endpoints úteis para smoke test
- OmniVoice: `GET /health`, `GET /voices`, `POST /tts`, `POST /v1/audio/speech`,
  `POST /v1/audio/transcriptions`, `POST /v1/audio/translations`, `POST /v1/chat/completions`
- Voxtral: `GET /health`, `POST /v1/audio/transcriptions`, `POST /v1/audio/translations`
  (`/health` fica fora do middleware de chave; o resto exige `Authorization: Bearer <KEY>`)

## 2. O que já foi descartado (varredura de 2026-09-24)

Feita a partir do Mac (`Rodrigos-MacBook-Pro`, 192.168.15.48). Tudo read-only.
**Resumo: nenhuma máquina alcançável tem GPU ou os arquivos do deploy.** Nenhum host
respondeu na 8800 (OmniVoice) nem na 8000.

| alvo | resultado |
|---|---|
| LAN `192.168.15.1-254`, portas 22/7860/8800/8000 | só respondem SSH: `.1` (roteador, dropbear), `.31` (Mac `lisboa--mac.local`), `.34` (Mac mini — roda o TTS-STUDIO na 7860 e o conector Cloudflare), `.59` (ubuntu-vm de CI, 3 runners do GitHub). **Nenhum com `nvidia-smi`.** |
| `192.168.15.49` (aparece no history do dono) | **offline** (ARP `incomplete`, ssh timeout) |
| Frota `213.155.16.5-43` (varridos `.5-.10` e `.34-.43`) | dumbledore, flamel, fawkes, buckbeak, hedwig, dobby, kreacher, winky — SSH ok com a chave do Mac; sem `/dev/nvidia*`, sem `/root/voxtral\|omnivoice`. RAM: dumbledore 63 GB, fawkes 1,5 TB (os mesmos hostnames aparecem em mais de um IP — parece VIP/round-robin) |
| Frota `217.179.88.2-44` (varridos `.5-.10` e `.20-.43`) | voldemort, bellatrix, barty, aragog, norbert, fluffy, nagini, scabbers, trevor — mesma coisa: sem GPU, sem os arquivos do deploy |
| Jump hosts `64.34.89.186` / `64.34.88.234` (de `~/.ssh/config.audit`) | timeout a partir do Mac em 2026-09-24 |
| `buckbeak.gryffindor.eonf.ltd` | resolve para `213.155.16.6`, responde ssh, **sem GPU** |
| `104.131.165.53`, `146.190.162.37`, `167.99.122.185`, `138.197.73.46`, `136.248.119.231`, `104.131.29.0` (hostname `gr-bet4all-online`) e `134.209.41.55` (hostname `wp-bg-bet4all-online`) | respondem ssh, **sem GPU** (`136.248.119.231` exige usuário `ubuntu`) |
| `192.168.200.32` | inalcançável daqui |

Leitura provável: a máquina CUDA estava **desligada** (ou fora do alcance desta rede) em
2026-09-24. Antes de repetir a varredura, pergunte o host ao dono.

## 3. Recon read-only (copiar e colar)

```sh
# armadilha: o ~/.ssh/config usa ControlMaster com socket em ~/.ssh/cm — se o HOME
# não for gravável (sandbox do agente/CI) o ssh morre com "cannot bind to path".
# Por isso: -o ControlMaster=no -o ControlPath=none
ssh -o BatchMode=yes -o ControlMaster=no -o ControlPath=none root@HOST '
  hostname; uname -srm; free -m | head -2
  nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv
  ls -d /root/omnivoice /root/voxtral
  systemctl status "omnivoice*" "voxtral*" --no-pager | head -30
  ss -ltnp | grep -E ":8800|:8[0-9]{3}"'
```

Dicas que economizam tempo:
- `nvidia-smi` ausente **não** é conclusivo sozinho: confira `/dev/nvidia*` e, se o
  `lspci` existir na imagem (em VM mínima ele pode faltar), `lspci -n | grep -i 10de`
  (10de = vendor NVIDIA). Foi assim que as máquinas da frota foram descartadas.
- `ru_maxrss` do `resource.getrusage` é **bytes no macOS e KB no Linux** (divisor
  diferente ao medir RSS em cada máquina).
- Varrer a /24 inteira por TCP (portas 22, 7860, 8800, 8000) com ~64 threads leva
  segundos e responde "existe máquina aqui?" melhor que ping (que pode estar bloqueado).

## 4. Deploy reproduzível (`remote/deploy.sh`)

O script faz o ciclo inteiro offline-quando-possível e só muda o servidor com
`--apply`. Ele **não** adivinha host: passe `host` ou exporte `TTS_REMOTE_HOST`
(e, se unit/porta/pastas forem diferentes do ❓ abaixo, `OMNI_REMOTE_*` /
`VOXTRAL_REMOTE_*`).

```sh
./remote/deploy.sh recon    [host]              # read-only: units, portas, venv, sha256, gpu
./remote/deploy.sh compare  [host]              # sha256 do repo × o que está no ar (rc=1 se difere)
./remote/deploy.sh deploy   [host] --apply      # backup → scp → ast.parse → restart → smoke
./remote/deploy.sh rollback [host] --apply      # volta o último server.py.bak-* e reinicia
./remote/deploy.sh smoke    [host]              # /health 200 com "auth" + 401 sem chave
```

`--service voxtral|omni|all` limita o alvo (padrão `all`). O que o script garante:

- **sem diff, sem restart**: igual ao repo → não reinicia (restart recarrega os
  modelos na VRAM, não é de graça);
- **backup antes de subir** (`server.py.bak-<data>`) e `ast.parse` no servidor
  antes do restart;
- **nada de segredo na saída**: `Environment=` sai com o valor redigido
  (`OMNI_API_KEY=<len 32>`) — presença dá para conferir, chave não vaza para o chat;
- `deploy` copia **dois** arquivos (`server.py` + `auth_policy.py`, importado por
  ele) e o `recon` registra o `pip freeze`/versão do python para o item de ambiente.

Antes do primeiro deploy, confirme a chave (senão o serviço não sobe — §1, política
de auth): `recon` mostra o `Environment` do unit; sem `*_API_KEY` ou defina a chave
ou acrescente `Environment=<PREFIXO>_ALLOW_NO_AUTH=1` (modo legado, aberto).

### Passo manual equivalente (se preferir na mão)

```sh
ssh -o BatchMode=yes -o ControlMaster=no -o ControlPath=none root@HOST \
  'cd /root/voxtral && cp -a server.py server.py.bak-$(date +%F)'
scp remote/auth_policy.py remote/voxtral_server.py root@HOST:/root/voxtral/
scp remote/auth_policy.py root@HOST:/root/omnivoice/
ssh root@HOST 'cd /root/voxtral && python3 -c "import ast,pathlib;ast.parse(pathlib.Path(\"server.py\").read_text())" \
  && systemctl restart ❓voxtral*.service && systemctl is-active ❓voxtral*.service'
curl -s http://HOST:❓PORTA/health      # 200 + "auth"; sem chave num endpoint → 401
```

Dependência nova (ex.: `onnxruntime` do VAD): `ssh root@HOST 'python3 -m pip install
--dry-run onnxruntime'` e depois sem `--dry-run` — antes do restart.

> **Pendência conhecida (2026-09-24):** o arquivo versionado já pede o VAD em ONNX, mas
> o servidor no ar ainda roda o jit do torch — o deploy não aconteceu porque a máquina
> nunca foi localizada (seção 2). Enquanto isso, `remote/voxtral_server.py` **não bate
> 100% com o que está no ar**: subir sem instalar `onnxruntime` não quebra nada (cai no
> mesmo jit, agora com aviso no log); para migrar de verdade, instale a dependência
> antes. Confira pelo `GET /health` → campo `vad`.

### Unit real e venv remoto (❓ até o dono confirmar)

Pendente de confirmação (item 1). Quando o dono responder, `recon` preenche:

| peça | valor |
|---|---|
| ❓ unit do Voxtral / porta | `VOXTRAL_REMOTE_UNIT` / `VOXTRAL_REMOTE_PORT` (hoje chutes) |
| ❓ `WorkingDirectory` e `ExecStart` do unit | saída do `recon` (`FragmentPath`, `ExecStart`) |
| ❓ python/venv que serve os dois | saída do `recon` (`python:`, `pacotes no venv:`); é o mesmo que o `pip install` deve atingir |
| ❓ hash do que está no ar | saída do `compare` (com o `repo` no mesmo commit, `compare` tem de dizer `[IGUAL]`) |

## 5. Segurança (leia antes de rodar ssh no escuro)

- **A chave `~/.ssh/id_ed25519` do Mac abre dezenas de máquinas públicas da frota.** Um
  `ssh` no host errado funciona e parece certo — confirme com `hostname`/`ls -d /root/...`
  antes de concluir qualquer coisa. Recon é read-only; `pip install` e `systemctl restart`
  já são intervenção (pedir OK).
- Nunca imprimir token/chave: o token do tunnel mora no plist, e as chaves de API
  (`OMNI_API_KEY`, `VOXTRAL_API_KEY`) em `Environment=` do unit. `deploy.sh recon` redige
  o valor (`OMNI_API_KEY=<len 32>`) — presença dá para conferir sem vazar para o chat;
  `systemctl cat` cru mostra.
- Autenticação é **fail closed** (política em `remote/auth_policy.py`): sem
  `OMNI_API_KEY`/`VOXTRAL_API_KEY` o processo **não sobe**; com a escape hatch
  `<PREFIXO>_ALLOW_NO_AUTH=1` ele sobe aberto — aí sim, só atrás de firewall/VPN.
  `/health` fica fora da chave e publica o modo em `auth`.
- Túnel do app (não confundir): o destino é **sempre o IP de LAN da máquina do TTS**,
  nunca `127.0.0.1` (loopback na VPS dispensa chave).

## 6. Arquivos relacionados

`remote/deploy.sh` · `remote/auth_policy.py` · `remote/omni_server.py` · `remote/voxtral_server.py` · `README.md` §"servidores `remote/`"
· `tunnel.sh` (Mac↔VPS) · `cloudflare.sh` (conector no Mac mini).