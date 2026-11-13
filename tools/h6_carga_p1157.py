# Carga /api/tts da medida P1–P5 (#1157, régua 720b6d7).
# Igual à H6 (1 job por vez, texto fixo ~200 ch, voz flvoz, ≥ 3,5 s entre POSTs)
# e, além disso, não abandona o job quando o Live acaba:
#   H6_LIVE_FIM  — ao aparecer, para de postar e drena o job em curso.
#                  Marca o instante em que `pausado_pelo_live` solta (P4:
#                  o trecho pendente começa) e espera o `done` (P3).
#   H6_PARA      — aborta. Sem LIVE_FIM, também encerra a janela (P5) e drena.
#   H6_JANELA_S  — teto de postagem (s) a partir do 1º POST. 0 = sem teto.
import json, os, sys, time, urllib.request, urllib.error

porta, log = sys.argv[1], sys.argv[2]
B = f"http://127.0.0.1:{porta}"
T = ("Este é um texto de carga sintética para medir a disputa entre o Live e os jobs de "
     "síntese no mesmo processo. Ele tem tamanho fixo, voz fixa e ritmo controlado, "
     "para que a carga não dependa de ninguém narrando.")
PARA = os.environ.get("H6_PARA", "/tmp/h6.para")
LIVE = os.environ.get("H6_LIVE_FIM", "")
JANELA = float(os.environ.get("H6_JANELA_S", "0") or 0)
cont = {"posts": 0, "ok": 0, "s429": 0, "outros": 0, "jobs_fim": 0, "jobs_erro": 0,
        "timeouts": 0, "jobs": []}

def grava():
    tmp = log + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cont, f)
    os.replace(tmp, log)

def req(m, p, corpo=None, timeout=30):
    r = urllib.request.Request(
        B + p, method=m,
        data=None if corpo is None else json.dumps(corpo).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout) as f:
        return f.status, json.loads(f.read() or b"{}")

def vivo():
    try:
        return req("GET", "/health", timeout=5)[0] == 200
    except Exception:
        return False

def acabou_postar():
    if os.path.exists(PARA):
        return True
    if LIVE and os.path.exists(LIVE):
        return True
    if JANELA and cont.get("t_primeiro") and time.time() - cont["t_primeiro"] >= JANELA:
        return True
    return False

def drena(jid, item):
    """Espera o terminal. Com LIVE_FIM, o P4 é o instante em que a pausa solta."""
    viu_pausa = False
    while jid and not os.path.exists(PARA):
        if LIVE and os.path.exists(LIVE) and "t_live_fim" not in cont:
            cont["t_live_fim"] = time.time()
        try:
            _st, j = req("GET", f"/api/tts/jobs/{jid}", timeout=10)
        except Exception:
            cont["timeouts"] += 1
            break
        pausado = bool(j.get("pausado_pelo_live"))
        if pausado:
            viu_pausa = True
            item["viu_pausa"] = True
        if viu_pausa and not pausado and "t_soltou" not in item:
            item["t_soltou"] = time.time()
            saida = j.get("pausa_lote_saida")
            if saida:
                item["pausa_lote_saida"] = saida
            # P4 = t_soltou - t_ocioso. t_live_fim é o fim do processo da
            # bancada e não entra nessa conta.
            if j.get("t_ocioso"):
                item["t_ocioso"] = j["t_ocioso"]
                item["p4_s"] = round(item["t_soltou"] - float(j["t_ocioso"]), 3)
        s = j.get("status")
        if s in ("done", "error", "cancelled", "canceled", "failed"):
            item["t_fim"] = time.time()
            item["status"] = s
            cont["jobs_fim" if s == "done" else "jobs_erro"] += 1
            break
        time.sleep(0.25)
    grava()

for _ in range(600):
    if vivo() or os.path.exists(PARA):
        break
    time.sleep(0.5)
time.sleep(20)
falhas = 0
ult = 0.0
while not acabou_postar():
    if not vivo():
        falhas += 1
        if falhas >= 3:
            break
        time.sleep(1)
        continue
    falhas = 0
    espera = 3.5 - (time.monotonic() - ult)
    if espera > 0:
        time.sleep(espera)
    if acabou_postar():
        break
    ult = time.monotonic()
    cont["posts"] += 1
    if "t_primeiro" not in cont:
        cont["t_primeiro"] = time.time()
    item = {"t_post": time.time()}
    try:
        _st, j = req("POST", "/api/tts", {"text": T, "voice_id": "flvoz"})
        cont["ok"] += 1
        jid = j.get("job_id")
        item["id"] = jid
    except urllib.error.HTTPError as e:
        cont["s429" if e.code == 429 else "outros"] += 1
        item["http"] = e.code
        cont["jobs"].append(item)
        grava()
        time.sleep(2)
        continue
    except Exception:
        cont["outros"] += 1
        cont["timeouts"] += 1
        cont["jobs"].append(item)
        grava()
        time.sleep(2)
        continue
    cont["jobs"].append(item)
    drena(jid, item)
# O Live fechou ou a janela acabou: o job em curso ainda tem de chegar a done (P3).
if cont["jobs"] and "status" not in cont["jobs"][-1] and cont["jobs"][-1].get("id"):
    drena(cont["jobs"][-1]["id"], cont["jobs"][-1])
cont["t_fim"] = time.time()
if cont.get("t_primeiro"):
    dt = cont["t_fim"] - cont["t_primeiro"]
    cont["janela_s"] = round(dt, 1)
    cont["jobs_por_min"] = round(cont["jobs_fim"] / dt * 60.0, 2) if dt > 0 else None
grava()
