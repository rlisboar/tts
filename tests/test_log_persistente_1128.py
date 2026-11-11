"""#1128 — o log da 7860 sai de `/tmp` (varrido às 00:00 e zerado no reboot) e ganha rotação.

Estático: o plist de produção, o `deploy_mini.sh`, o probe e o newsyslog apontam para
`~/Library/Logs/tts-studio/`, nenhum cita mais `/tmp/tts-studio.log` como DESTINO do
mini. Dinâmico: o `remote/log_rotate.sh` rotaciona por tamanho TRUNCANDO NO LUGAR (o
launchd abre em append; o inode e as linhas novas sobrevivem), comprime, respeita o
teto de gerações e deixa tudo 0600.
"""
from __future__ import annotations

import gzip
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ROT = REPO / "remote" / "log_rotate.sh"
ZSH = shutil.which("zsh")
NOVO = "Library/Logs/tts-studio/tts-studio.log"


def _sem_comentario(p: Path) -> list[str]:
    return [l for l in p.read_text().splitlines() if not l.lstrip().startswith(("#", "<!--"))]


def test_plist_de_producao_escreve_fora_de_tmp():
    linhas = _sem_comentario(REPO / "remote" / "launchd" / "studio.tts.server.plist")
    saidas = [l for l in linhas if "/tmp/" in l]
    assert not saidas, saidas
    assert sum(NOVO in l for l in linhas) == 2          # StandardOutPath + StandardErrorPath


@pytest.mark.parametrize("arq", ["remote/deploy_mini.sh", "remote/health_probe.sh"])
def test_scripts_do_mini_nao_apontam_mais_para_o_log_em_tmp(arq):
    achados = [l for l in _sem_comentario(REPO / arq) if "/tmp/tts-studio.log" in l]
    assert not achados, achados


def test_deploy_mini_le_o_log_do_caminho_novo_e_o_home_e_o_do_mini():
    src = (REPO / "remote" / "deploy_mini.sh").read_text()
    m = re.search(r'^LOG_MINI="\$\{TTS_MINI_LOG:-(.*)\}"', src, re.M)
    assert m, "LOG_MINI sem default"
    # `$HOME` ESCAPADO: quem expande é o shell do mini, não o Mac que roda o deploy
    assert m.group(1) == r"\$HOME/" + NOVO, m.group(1)


def test_newsyslog_aponta_para_o_caminho_novo():
    linhas = _sem_comentario(REPO / "remote" / "newsyslog.d" / "tts-studio.conf")
    assert any("Library/Logs/tts-studio/tts-studio.log" in l for l in linhas), linhas
    assert not [l for l in linhas if l.startswith("/tmp/")], linhas


@pytest.mark.skipif(ZSH is None, reason="precisa de zsh")
class TestRotacao:
    def _rodar(self, tmp_path, tam_mb_log, **env):
        d = tmp_path / "logs"
        d.mkdir(mode=0o755)
        log = d / "tts-studio.log"
        log.write_bytes(b"linha de diagnostico\n" * (tam_mb_log * 1024 * 1024 // 21))
        log.chmod(0o644)
        e = dict(os.environ, TTS_LOG_DIR=str(d), HOME=str(tmp_path), **env)
        r = subprocess.run([ZSH, "-f", str(ROT), "rotaciona"], env=e, capture_output=True,
                           text=True, stdin=subprocess.DEVNULL)
        return d, log, r

    def test_abaixo_do_teto_nao_mexe_mas_aperta_o_modo(self, tmp_path):
        d, log, r = self._rodar(tmp_path, 1, TTS_LOG_MAX_MB="5")
        assert r.returncode == 0, r.stderr
        assert "sem rotação" in r.stdout
        assert not list(d.glob("*.gz"))
        assert oct(log.stat().st_mode & 0o777) == "0o600"
        assert oct(d.stat().st_mode & 0o777) == "0o700"

    def test_acima_do_teto_trunca_no_lugar_e_comprime(self, tmp_path):
        d, log, r = self._rodar(tmp_path, 2, TTS_LOG_MAX_MB="1")
        assert r.returncode == 0, r.stderr
        assert log.stat().st_size == 0
        gz = d / "tts-studio.log.1.gz"
        assert gz.exists() and oct(gz.stat().st_mode & 0o777) == "0o600"
        assert gzip.decompress(gz.read_bytes()).startswith(b"linha de diagnostico\n")
        assert not (d / "tts-studio.log.1").exists()

    def test_o_inode_sobrevive_e_o_append_do_launchd_continua(self, tmp_path):
        d = tmp_path / "logs"
        d.mkdir()
        log = d / "tts-studio.log"
        log.write_bytes(b"x" * (2 * 1024 * 1024))
        aberto = open(log, "ab")                      # o "launchd": descritor em append
        ino = os.fstat(aberto.fileno()).st_ino
        e = dict(os.environ, TTS_LOG_DIR=str(d), HOME=str(tmp_path), TTS_LOG_MAX_MB="1")
        subprocess.run([ZSH, "-f", str(ROT), "rotaciona"], env=e, check=True,
                       capture_output=True, stdin=subprocess.DEVNULL)
        aberto.write(b"linha nova depois da rotacao\n"); aberto.flush(); aberto.close()
        assert log.stat().st_ino == ino                # mesmo arquivo: nada de rename
        assert log.read_bytes() == b"linha nova depois da rotacao\n"

    def test_respeita_o_teto_de_geracoes(self, tmp_path):
        d, log, _ = self._rodar(tmp_path, 2, TTS_LOG_MAX_MB="1", TTS_LOG_GERACOES="2")
        for i in range(3):
            log.write_bytes(b"y" * (2 * 1024 * 1024))
            e = dict(os.environ, TTS_LOG_DIR=str(d), HOME=str(tmp_path),
                     TTS_LOG_MAX_MB="1", TTS_LOG_GERACOES="2")
            subprocess.run([ZSH, "-f", str(ROT), "rotaciona"], env=e, check=True,
                           capture_output=True, stdin=subprocess.DEVNULL)
        assert sorted(p.name for p in d.glob("*.gz")) == [
            "tts-studio.log.1.gz", "tts-studio.log.2.gz"]

    def test_geracao_velha_demais_sai_por_idade(self, tmp_path):
        d, log, _ = self._rodar(tmp_path, 1, TTS_LOG_MAX_MB="5", TTS_LOG_DIAS="2")
        velha = d / "tts-studio.log.2.gz"
        velha.write_bytes(b"gz"); velha.chmod(0o600)
        os.utime(velha, (1, 1))
        e = dict(os.environ, TTS_LOG_DIR=str(d), HOME=str(tmp_path),
                 TTS_LOG_MAX_MB="5", TTS_LOG_DIAS="2")
        subprocess.run([ZSH, "-f", str(ROT), "rotaciona"], env=e, check=True,
                       capture_output=True, stdin=subprocess.DEVNULL)
        assert not velha.exists()


@pytest.mark.skipif(ZSH is None, reason="precisa de zsh")
def test_install_copia_o_script_para_tts_studio_e_o_plist_aponta_para_a_copia(tmp_path):
    """Achado do deploy do #1064: o agente do launchd NÃO lê `~/Documents` (TCC) — a 1ª passada
    deu "can't open input file". O `install` se copia para `~/.tts-studio` e o plist aponta
    para a cópia, nunca para o repo."""
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "launchctl").write_text("#!/bin/sh\nexit 0\n")
    (bin_ / "launchctl").chmod(0o755)
    e = dict(os.environ, HOME=str(tmp_path), PATH=f"{bin_}:{os.environ['PATH']}")
    e.pop("TTS_LOG_DIR", None)
    r = subprocess.run([ZSH, "-f", str(ROT), "install"], env=e, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stdout + r.stderr
    copia = tmp_path / ".tts-studio" / "log_rotate.sh"
    assert copia.exists() and os.access(copia, os.X_OK)
    assert copia.read_text() == ROT.read_text()
    plist = (tmp_path / "Library" / "LaunchAgents" / "com.local.tts-log-rotate.plist").read_text()
    assert f"<string>{copia}</string>" in plist, plist
    assert str(ROT) not in plist, "o plist aponta para o repo (o launchd não lê ~/Documents)"
    # reinstalar a partir da própria cópia não quebra (cp de arquivo sobre ele mesmo)
    r2 = subprocess.run([ZSH, "-f", str(copia), "install"], env=e, capture_output=True, text=True,
                        stdin=subprocess.DEVNULL)
    assert r2.returncode == 0, r2.stdout + r2.stderr
