# Runbook — produção "viva no loopback, morta pela LAN" (firewall do macOS no mini)

Incidente de 2026-11-11 19:49 → 2026-11-12 15:26 (#1152): o firewall de aplicativo do macOS (ALF)
passou a segurar as conexões de entrada do `Python` no Mac mini. O app respondia 200 em
`127.0.0.1:7860`, mas pelo IP de LAN o TCP conectava e a resposta nunca vinha; o cloudflared (que
aponta para o IP de LAN, de propósito) devolvia 1033/timeout. ~20 h sem ninguém ver.

## Como reconhecer

- `./remote/deploy_mini.sh smoke` vermelho **e** o deploy imprime "FALHA DE AMBIENTE, NÃO DO REV"
  (loopback do mini 200, LAN morta). Nesse caso o deploy **não reverte nem reinicia**.
- No mini: `/usr/bin/log show --last 10m --info --predicate 'process == "socketfilterfw"' | grep Enqueuing`
  mostra `Enqueuing new inbound flow for Python (org.python.python)` repetido.
- `lsof -nP -p <pid do uvicorn> -a -iTCP` sem nenhuma conexão ESTABLISHED durante um `curl` pela LAN.
- A sonda `tools/probe_externo.sh` (LAN + público) acusa `ALARME` depois de 2 falhas seguidas.

## Passo do DONO (precisa de GUI/sudo; o agente não tem — `sudo` por ssh pede senha)

1. Na **tela do mini**, clicar **Permitir** no diálogo "Deseja que o aplicativo "Python" aceite
   conexões de entrada?" — ou, no terminal do mini:

       sudo /usr/libexec/ApplicationFirewall/socketfilterfw --unblockapp \
         /Library/Frameworks/Python.framework/Versions/3.12/Resources/Python.app

2. Conferir: `curl -s -m 8 -o /dev/null -w '%{http_code}\n' http://192.168.15.34:7860/health` → 200
   e `https://tts.the-dudes.com/health` → 200.
3. Se o diálogo voltar a cada restart, considerar o firewall de aplicativo desligado nesse mini
   (rede local confiável; a chave de API continua exigida fora do loopback) — decisão do dono.

## Depois

`./remote/deploy_mini.sh smoke` (read-only) e, se era um deploy interrompido, repetir o `deploy --apply`.

## Sonda externa (detecção)

    tools/probe_externo.sh instala     # LaunchAgent, a cada 5 min (Mac de dev E mini)
    tools/probe_externo.sh status
    TTS_PROBE_ALARME_CMD='…'           # gancho do alarme (recebe PROBE_ALVO/PROBE_FALHAS/PROBE_MSG)

Só `GET /health`; nenhuma chave é lida, enviada ou gravada. Log: `~/Library/Logs/tts-studio/probe-externo.log`.
