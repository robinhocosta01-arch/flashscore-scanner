import logging
import time
import csv
import os
import requests
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ============================================================
# CONFIGURAÇÃO
# ============================================================
TOKEN    = "8651051857:AAEjY3MKNuoFiRFu4YG2zUGf2tKCgMuWZo8"
CHAT_ID  = "803725273"
URL_LIVE = "https://www.flashscore.com"

INTERVALO_SCAN   = 30   # segundos entre varreduras
INTERVALO_RELOAD = 300  # recarrega página a cada 5 min

# Score mínimo para alertas (exceto OVER HT que é sempre enviado)
SCORE_MINIMO = 40

CHUTES_GOL_PESO    = 3
ATAQUES_PERIG_PESO = 1

# ============================================================
# LOG
# ============================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

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
# HELPERS
# ============================================================
def parse_minuto(tempo_raw):
    tempo = tempo_raw.strip().replace("'", "")
    if tempo in ("HT", "FT", ""):
        return None
    if "+" in tempo:
        return int(tempo.split("+")[0])
    try:
        return int(tempo)
    except:
        return None

def extrair_stat(driver, label):
    try:
        rows = driver.find_elements(By.CLASS_NAME, "stat__row")
        for row in rows:
            try:
                nome = row.find_element(By.CLASS_NAME, "stat__categoryName").text.strip()
                if label.lower() in nome.lower():
                    h = row.find_element(By.CLASS_NAME, "stat__homeValue").text.strip()
                    a = row.find_element(By.CLASS_NAME, "stat__awayValue").text.strip()
                    return int(h), int(a)
            except:
                continue
    except:
        pass
    return None, None

# ============================================================
# SCANNER — UM SÓ DRIVER
# ============================================================
class FlashscoreScanner:
    def __init__(self):
        self.alertas_enviados = {}
        self.ultimo_reload    = 0
        self.total_alertas    = 0
        self.inicio           = datetime.now()

        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-extensions")
        options.add_argument("--blink-settings=imagesEnabled=false")

        # ✅ Aponta para o Chrome instalado pelo Dockerfile no Linux
        options.binary_location = "/usr/bin/chromium"

        from selenium.webdriver.chrome.service import Service
        service = Service("/usr/bin/chromedriver")

        # ✅ Um único driver para tudo
        self.driver = webdriver.Chrome(service=service, options=options)
        self.wait   = WebDriverWait(self.driver, 25)
        logging.info("🌐 Driver Chrome iniciado (modo leve)")

    def _carregar_lista(self):
        """Carrega a página principal com a lista de jogos."""
        self.driver.get(URL_LIVE)
        time.sleep(5)
        self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)
        self.ultimo_reload = time.time()
        logging.info("🔄 Lista de jogos recarregada.")

    def _buscar_stats(self, jogo_id):
        """
        Abre a página de stats do jogo, extrai dados e volta para a lista.
        Só é chamado quando um jogo candidato é encontrado.
        """
        stats = {"chutes_gol_h": 0, "chutes_gol_a": 0,
                 "ataques_h": 0,    "ataques_a": 0,
                 "posse_h": 0}
        try:
            url_stats = f"https://www.flashscore.com/match/{jogo_id}/#/match-summary/match-statistics/0"
            self.driver.get(url_stats)
            time.sleep(3)

            ch, ca = extrair_stat(self.driver, "Shots on Goal")
            if ch is not None:
                stats["chutes_gol_h"] = ch
                stats["chutes_gol_a"] = ca

            ah, aa = extrair_stat(self.driver, "Dangerous Attacks")
            if ah is not None:
                stats["ataques_h"] = ah
                stats["ataques_a"] = aa

            ph, _ = extrair_stat(self.driver, "Ball Possession")
            if ph is not None:
                stats["posse_h"] = ph

        except Exception as e:
            logging.warning(f"⚠️ Erro stats {jogo_id}: {e}")
        finally:
            # ✅ Sempre volta para a lista após buscar stats
            self.driver.get(URL_LIVE)
            time.sleep(3)
            self.ultimo_reload = time.time()

        return stats

    def _processar_candidatos(self, candidatos):
        """
        Busca estatísticas apenas dos jogos candidatos
        e envia alertas se passarem no score de pressão.
        """
        for item in candidatos:
            home, away, sh, sa, minuto, alvo, jogo_id = item
            chave = f"{home}_{away}_{alvo}"

            if chave in self.alertas_enviados:
                continue

            stats = self._buscar_stats(jogo_id) if jogo_id else {}

            chutes_gol    = stats.get("chutes_gol_h", 0) + stats.get("chutes_gol_a", 0)
            ataques_perig = stats.get("ataques_h", 0)    + stats.get("ataques_a", 0)
            posse_home    = stats.get("posse_h", 0)
            score         = (chutes_gol * CHUTES_GOL_PESO) + (ataques_perig * ATAQUES_PERIG_PESO)

            # Over HT é urgente — sempre envia
            if alvo != "OVER 0.5 HT 🕐" and score < SCORE_MINIMO:
                logging.info(f"⏭ Ignorado (pressão {score} < {SCORE_MINIMO}): {home} x {away} | {minuto}'")
                continue

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

    def _varrer_jogos(self):
        """
        Varre todos os jogos da lista e retorna apenas os candidatos.
        Não abre nenhuma página de detalhe — só lê o DOM da lista.
        """
        candidatos = []
        try:
            self.wait.until(EC.presence_of_element_located((By.CLASS_NAME, "event__match")))
            jogos = self.driver.find_elements(By.CLASS_NAME, "event__match")

            if len(jogos) == 0:
                logging.warning("⚠️ Nenhum jogo encontrado.")
                self.driver.save_screenshot("debug.png")
                return candidatos

            logging.info(f"✅ {len(jogos)} jogos analisados.")

            for jogo in jogos:
                try:
                    sh = int(jogo.find_element(By.CLASS_NAME, "event__score--home").text)
                    sa = int(jogo.find_element(By.CLASS_NAME, "event__score--away").text)
                except:
                    continue

                try:
                    home      = jogo.find_element(By.CLASS_NAME, "event__participant--home").text
                    away      = jogo.find_element(By.CLASS_NAME, "event__participant--away").text
                    tempo_raw = jogo.find_element(By.CLASS_NAME, "event__stage--actual").text
                    jogo_id   = jogo.get_attribute("id").replace("g_1_", "")
                except:
                    continue

                minuto = parse_minuto(tempo_raw)
                if minuto is None:
                    continue

                total = sh + sa
                alvo  = None
                if   total == 0 and 25 <= minuto <= 45: alvo = "OVER 0.5 HT 🕐"
                elif total == 0 and minuto >= 70:        alvo = "OVER 0.5"
                elif total == 1 and minuto <= 60:        alvo = "OVER 1.5"
                elif total == 2 and minuto <= 75:        alvo = "OVER 2.5"

                if alvo:
                    chave = f"{home}_{away}_{alvo}"
                    if chave not in self.alertas_enviados:
                        candidatos.append((home, away, sh, sa, minuto, alvo, jogo_id))

        except Exception as e:
            logging.error(f"❌ Erro na varredura: {e}")

        return candidatos

    def scan(self):
        logging.info("🔍 Iniciando scanner...")
        self._carregar_lista()

        while True:
            try:
                # Recarrega lista a cada 5 min
                if time.time() - self.ultimo_reload >= INTERVALO_RELOAD:
                    self._carregar_lista()

                # 1. Varre todos os jogos rapidamente (só lê o DOM)
                candidatos = self._varrer_jogos()

                # 2. Busca stats só dos candidatos (poucos jogos)
                if candidatos:
                    logging.info(f"🔎 {len(candidatos)} candidatos para análise...")
                    self._processar_candidatos(candidatos)
                    self._carregar_lista()

                time.sleep(INTERVALO_SCAN)

            except Exception as e:
                logging.error(f"❌ Erro geral: {e}. Aguardando 30s...")
                time.sleep(30)
                self._carregar_lista()

# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    enviar_telegram(
        f"🚀 <b>Scanner Iniciado!</b>\n"
        f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n"
        f"⚙️ Scan: {INTERVALO_SCAN}s | Score mínimo: {SCORE_MINIMO}"
    )
    scanner = FlashscoreScanner()
    scanner.scan()
