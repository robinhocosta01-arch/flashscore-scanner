import logging
import time
import csv
import os
import re
import requests
from datetime import datetime

# ============================================================
# CONFIGURAÇÃO
# ============================================================
TOKEN    = "8651051857:AAEjY3MKNuoFiRFu4YG2zUGf2tKCgMuWZo8"
CHAT_ID  = "803725273"

INTERVALO_SCAN = 30    # segundos entre varreduras
SCORE_MINIMO   = 40    # score de pressão mínimo para alertar

CHUTES_GOL_PESO    = 3
ATAQUES_PERIG_PESO = 1

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

# ============================================================
# LOG CSV
# ============================================================
LOG_FILE = "jogos_log.csv"
if not os.path.exists(LOG_FILE):
    with open(LOG_FILE, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([
            "data_hora", "home", "away", "placar",
            "minuto", "alvo", "chutes_gol",
            "ataques_perigosos", "posse_home", "score_pressao"
        ])

def logar_alerta(home, away, sh, sa, minuto, alvo, chutes_gol, ataques, posse, score):
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([
            datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
            home, away, f"{sh}x{sa}",
            minuto, alvo, chutes_gol, ataques, posse, score
        ])

# ============================================================
# TELEGRAM
# ============================================================
def enviar_telegram(mensagem):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        resp = requests.get(url, params={
            "chat_id": CHAT_ID,
            "text": mensagem,
            "parse_mode": "HTML"
        }, timeout=10)
        if not resp.ok:
            logging.error(f"Telegram erro: {resp.status_code} - {resp.text}")
        else:
            logging.info("✅ Telegram enviado")
    except Exception as e:
        logging.error(f"Telegram exceção: {e}")

# ============================================================
# FLASHSCORE API INTERNA
# Headers necessários para a API interna do Flashscore
# ============================================================
FS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.flashscore.com/",
    "x-fsign": "SW9D1eZo",  # token fixo da API interna
}

def buscar_jogos_ao_vivo():
    """
    Busca todos os jogos ao vivo do Flashscore via API interna.
    Retorna lista de dicts com dados dos jogos.
    """
    url = "https://d.flashscore.com/x/feed/f_1_0_3_en_1"
    try:
        resp = requests.get(url, headers=FS_HEADERS, timeout=15)
        resp.raise_for_status()
        return parsear_feed(resp.text)
    except Exception as e:
        logging.error(f"❌ Erro ao buscar jogos: {e}")
        return []

def parsear_feed(texto):
    """
    O feed do Flashscore é um formato proprietário separado por '¬'.
    Extrai os dados de cada jogo ao vivo.
    """
    jogos = []
    # Cada jogo começa com AA÷ e termina com ~
    blocos = texto.split("~")
    for bloco in blocos:
        try:
            jogo = {}
            partes = bloco.split("¬")
            for parte in partes:
                if "÷" in parte:
                    chave, valor = parte.split("÷", 1)
                    jogo[chave] = valor

            # Só processa futebol ao vivo (status 6 = ao vivo)
            if jogo.get("AE") != "6":
                continue

            # Extrai campos necessários
            home    = jogo.get("CX", "")  # time da casa
            away    = jogo.get("AF", "")  # time visitante
            sh_raw  = jogo.get("AG", "")  # placar casa
            sa_raw  = jogo.get("AH", "")  # placar visitante
            min_raw = jogo.get("AN", "")  # minuto
            jogo_id = jogo.get("AA", "")  # id do jogo

            if not all([home, away, sh_raw, sa_raw, min_raw]):
                continue

            sh = int(sh_raw)
            sa = int(sa_raw)

            # Parse do minuto
            min_raw = min_raw.replace("'", "").strip()
            if "+" in min_raw:
                minuto = int(min_raw.split("+")[0])
            else:
                minuto = int(min_raw)

            jogos.append({
                "id": jogo_id,
                "home": home,
                "away": away,
                "sh": sh,
                "sa": sa,
                "minuto": minuto
            })
        except:
            continue

    return jogos

def buscar_stats(jogo_id):
    """
    Busca estatísticas do jogo via API interna.
    Retorna dict com chutes no gol, ataques perigosos e posse.
    """
    stats = {"chutes_gol_h": 0, "chutes_gol_a": 0,
             "ataques_h": 0,    "ataques_a": 0,
             "posse_h": 0}
    try:
        url = f"https://d.flashscore.com/x/feed/d_st_{jogo_id}_en_1"
        resp = requests.get(url, headers=FS_HEADERS, timeout=10)
        if not resp.ok:
            return stats

        texto = resp.text
        blocos = texto.split("~")

        for bloco in blocos:
            partes = bloco.split("¬")
            dados = {}
            for parte in partes:
                if "÷" in parte:
                    k, v = parte.split("÷", 1)
                    dados[k] = v

            nome = dados.get("WI", "").lower()
            val_h = dados.get("WJ", "0")
            val_a = dados.get("WK", "0")

            try:
                if "shots on target" in nome or "shots on goal" in nome:
                    stats["chutes_gol_h"] = int(val_h)
                    stats["chutes_gol_a"] = int(val_a)
                elif "dangerous attacks" in nome:
                    stats["ataques_h"] = int(val_h)
                    stats["ataques_a"] = int(val_a)
                elif "ball possession" in nome:
                    stats["posse_h"] = int(val_h.replace("%", ""))
            except:
                continue

    except Exception as e:
        logging.warning(f"⚠️ Erro stats {jogo_id}: {e}")

    return stats

# ============================================================
# SCANNER
# ============================================================
class FlashscoreScanner:
    def __init__(self):
        self.alertas_enviados = {}
        self.total_alertas    = 0
        self.inicio           = datetime.now()

    def _uptime(self):
        delta = datetime.now() - self.inicio
        h, rem = divmod(int(delta.total_seconds()), 3600)
        m, s   = divmod(rem, 60)
        return f"{h}h {m}m {s}s"

    def processar(self, jogo):
        home   = jogo["home"]
        away   = jogo["away"]
        sh     = jogo["sh"]
        sa     = jogo["sa"]
        minuto = jogo["minuto"]
        jid    = jogo["id"]
        total  = sh + sa

        # Define alvo
        alvo = None
        if   total == 0 and 25 <= minuto <= 45: alvo = "OVER 0.5 HT 🕐"
        elif total == 0 and minuto >= 70:        alvo = "OVER 0.5"
        elif total == 1 and minuto <= 60:        alvo = "OVER 1.5"
        elif total == 2 and minuto <= 75:        alvo = "OVER 2.5"

        if not alvo:
            return

        chave = f"{home}_{away}_{alvo}"
        if chave in self.alertas_enviados:
            return

        # Busca estatísticas via API interna (leve, sem browser)
        stats         = buscar_stats(jid)
        chutes_gol    = stats["chutes_gol_h"] + stats["chutes_gol_a"]
        ataques_perig = stats["ataques_h"]    + stats["ataques_a"]
        posse_home    = stats["posse_h"]
        score         = (chutes_gol * CHUTES_GOL_PESO) + (ataques_perig * ATAQUES_PERIG_PESO)

        # Over HT sempre envia — é urgente
        if alvo != "OVER 0.5 HT 🕐" and score < SCORE_MINIMO:
            logging.info(f"⏭ Ignorado (pressão {score} < {SCORE_MINIMO}): {home} x {away} | {minuto}'")
            return

        barra = "🟩" * min(int(score / 10), 10)
        msg = (
            f"🚨 <b>{alvo}</b>\n"
            f"⚽ {home} {sh} x {sa} {away}\n"
            f"⏱ {minuto}' | {datetime.now().strftime('%H:%M:%S')}\n"
            f"\n📊 <b>Estatísticas</b>\n"
            f"🎯 Chutes no gol: {chutes_gol}\n"
            f"⚡ Ataques perigosos: {ataques_perig}\n"
            f"🔵 Posse (casa): {posse_home}%\n"
            f"🔥 Pressão: {score} {barra}"
        )

        enviar_telegram(msg)
        logar_alerta(home, away, sh, sa, minuto, alvo, chutes_gol, ataques_perig, posse_home, score)
        self.alertas_enviados[chave] = True
        self.total_alertas += 1
        logging.info(f"🚨 {alvo} | {home} x {away} | {minuto}' | pressão: {score}")

    def scan(self):
        logging.info("🔍 Scanner iniciado (modo leve — sem Chrome)")

        while True:
            try:
                jogos = buscar_jogos_ao_vivo()

                if not jogos:
                    logging.warning("⚠️ Nenhum jogo ao vivo encontrado.")
                else:
                    logging.info(f"✅ {len(jogos)} jogos ao vivo | Uptime: {self._uptime()} | Alertas: {self.total_alertas}")
                    for jogo in jogos:
                        try:
                            self.processar(jogo)
                        except:
                            continue

            except Exception as e:
                logging.error(f"❌ Erro geral: {e}")

            time.sleep(INTERVALO_SCAN)

# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    enviar_telegram(
        f"🚀 <b>Scanner Iniciado! (modo leve)</b>\n"
        f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n"
        f"⚙️ Sem Chrome | Scan: {INTERVALO_SCAN}s | Score mínimo: {SCORE_MINIMO}"
    )
    scanner = FlashscoreScanner()
    scanner.scan()
