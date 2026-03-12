import logging
import time
import csv
import os
import requests
from datetime import datetime
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ============================================================
# CONFIGURAÇÃO
# ============================================================
TOKEN    = "8651051857:AAEjY3MKNuoFiRFu4YG2zUGf2tKCgMuWZo8"
CHAT_ID  = "803725273"

INTERVALO_SCAN = 30
SCORE_MINIMO   = 40

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
# SESSION COM RETRY E HEADERS DE BROWSER REAL
# ============================================================
def criar_session():
    session = requests.Session()

    # Retry automático em falhas de rede
    retry = Retry(total=3, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)

    # Headers que imitam Chrome real no Windows
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9,pt-BR;q=0.8,pt;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Origin": "https://www.sofascore.com",
        "Referer": "https://www.sofascore.com/",
        "Sec-Ch-Ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Connection": "keep-alive",
    })

    return session

SESSION = criar_session()

# ============================================================
# SOFASCORE API
# ============================================================
def buscar_jogos_ao_vivo():
    url = "https://api.sofascore.com/api/v1/sport/football/events/live"
    try:
        resp = SESSION.get(url, timeout=15)
        resp.raise_for_status()
        data   = resp.json()
        eventos = data.get("events", [])
        jogos  = []

        for ev in eventos:
            try:
                status = ev.get("status", {})
                if status.get("type") != "inprogress":
                    continue

                home    = ev["homeTeam"]["name"]
                away    = ev["awayTeam"]["name"]
                jogo_id = ev["id"]
                sh      = ev.get("homeScore", {}).get("current", 0) or 0
                sa      = ev.get("awayScore", {}).get("current", 0) or 0

                minuto_raw = status.get("description", "0").replace("'", "").strip()
                try:
                    minuto = int(minuto_raw.split("+")[0]) if "+" in minuto_raw else int(minuto_raw)
                except:
                    continue

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

    except Exception as e:
        logging.error(f"❌ Erro ao buscar jogos: {e}")
        return []

def buscar_stats(jogo_id):
    stats = {
        "chutes_gol_h": 0, "chutes_gol_a": 0,
        "ataques_h": 0,    "ataques_a": 0,
        "posse_h": 0
    }
    try:
        url  = f"https://api.sofascore.com/api/v1/event/{jogo_id}/statistics"
        resp = SESSION.get(url, timeout=10)
        if not resp.ok:
            return stats

        data    = resp.json()
        grupos  = data.get("statistics", [])
        periodo = None

        for g in grupos:
            if g.get("period") in ("ALL", "1ST", "2ND"):
                periodo = g
                break

        if not periodo:
            return stats

        for grupo in periodo.get("groups", []):
            for item in grupo.get("statisticsItems", []):
                nome = item.get("name", "").lower()
                try:
                    h = int(str(item.get("home", 0)).replace("%", "") or 0)
                    a = int(str(item.get("away", 0)).replace("%", "") or 0)
                except:
                    continue

                if "shots on target" in nome:
                    stats["chutes_gol_h"] = h
                    stats["chutes_gol_a"] = a
                elif "dangerous attack" in nome:
                    stats["ataques_h"] = h
                    stats["ataques_a"] = a
                elif "possession" in nome:
                    stats["posse_h"] = h

    except Exception as e:
        logging.warning(f"⚠️ Erro stats {jogo_id}: {e}")

    return stats

# ============================================================
# SCANNER
# ============================================================
class Scanner:
    def __init__(self):
        self.alertas_enviados = {}
        self.total_alertas    = 0
        self.inicio           = datetime.now()

    def _uptime(self):
        delta  = datetime.now() - self.inicio
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

        stats         = buscar_stats(jid)
        chutes_gol    = stats["chutes_gol_h"] + stats["chutes_gol_a"]
        ataques_perig = stats["ataques_h"]    + stats["ataques_a"]
        posse_home    = stats["posse_h"]
        score         = (chutes_gol * CHUTES_GOL_PESO) + (ataques_perig * ATAQUES_PERIG_PESO)

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
        logging.info("🔍 Scanner SofaScore iniciado (sem Chrome)")

        while True:
            try:
                jogos = buscar_jogos_ao_vivo()

                if not jogos:
                    logging.warning("⚠️ Nenhum jogo ao vivo encontrado.")
                else:
                    logging.info(f"✅ {len(jogos)} jogos | Uptime: {self._uptime()} | Alertas: {self.total_alertas}")
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
        f"🚀 <b>Scanner Iniciado! (SofaScore)</b>\n"
        f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n"
        f"⚙️ Sem Chrome | Scan: {INTERVALO_SCAN}s | Score mínimo: {SCORE_MINIMO}"
    )
    scanner = Scanner()
    scanner.scan()
