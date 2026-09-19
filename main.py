import os
import requests
import yfinance as yf
from typing import Dict, List, Optional

# ==========================================
# 1. 通訊軟體 Token 設定
# ==========================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_TELEGRAM_CHAT_ID")

# 若使用 LINE Messaging API (非必填)
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_USER_ID = os.getenv("LINE_USER_ID", "")

# ==========================================
# 2. 專屬持股監控清單 (含台股與美股)
# ==========================================
# ticker: Yahoo Finance 支援代碼
# currency: 幣別 (USD / TWD)
# fair_value: 內在合理價值 (可依個人估值定期更新)
# mos_buy: 安全邊際買進折價 (0.15 = 折價 15% 買進)
# expensive_sell: 高估警戒溢價 (0.25 = 溢價 25% 考慮調節)
PORTFOLIO_WATCHLIST = [
    # --- 美股部位 ---
    {
        "ticker": "AMAT",
        "name": "應用材料 (AMAT)",
        "currency": "USD",
        "fair_value": 210.0,
        "mos_buy": 0.15,
        "expensive_sell": 0.25,
    },
    {
        "ticker": "NVDA",
        "name": "輝達 (NVDA)",
        "currency": "USD",
        "fair_value": 135.0,
        "mos_buy": 0.15,
        "expensive_sell": 0.30,
    },
    {
        "ticker": "COST",
        "name": "好市多 (COST)",
        "currency": "USD",
        "fair_value": 850.0,
        "mos_buy": 0.10,
        "expensive_sell": 0.20,
    },
    {
        "ticker": "BRK-B",
        "name": "波克夏 B 股 (BRK.B)",
        "currency": "USD",
        "fair_value": 450.0,
        "mos_buy": 0.10,
        "expensive_sell": 0.20,
    },
    {
        "ticker": "LLY",
        "name": "禮來製藥 (LLY)",
        "currency": "USD",
        "fair_value": 880.0,
        "mos_buy": 0.15,
        "expensive_sell": 0.25,
    },
    {
        "ticker": "VRT",
        "name": "維諦技術 (VRT)",
        "currency": "USD",
        "fair_value": 110.0,
        "mos_buy": 0.20,
        "expensive_sell": 0.35,
    },
    {
        "ticker": "INTC",
        "name": "英特爾 (INTC)",
        "currency": "USD",
        "fair_value": 24.0,
        "mos_buy": 0.15,
        "expensive_sell": 0.25,
    },

    # --- 台股部位 (上市股票代號後綴 .TW) ---
    {
        "ticker": "0050.TW",
        "name": "元大台灣50 (0050)",
        "currency": "TWD",
        "fair_value": 185.0,
        "mos_buy": 0.10,
        "expensive_sell": 0.20,
    },
    {
        "ticker": "2337.TW",
        "name": "旺宏 (2337)",
        "currency": "TWD",
        "fair_value": 28.0,
        "mos_buy": 0.15,
        "expensive_sell": 0.25,
    },
    {
        "ticker": "2002.TW",
        "name": "中鋼 (2002)",
        "currency": "TWD",
        "fair_value": 23.5,
        "mos_buy": 0.10,
        "expensive_sell": 0.20,
    },
    {
        "ticker": "1232.TW",
        "name": "大統益 (1232)",
        "currency": "TWD",
        "fair_value": 155.0,
        "mos_buy": 0.10,
        "expensive_sell": 0.20,
    },
    {
        "ticker": "1904.TW",
        "name": "正隆 (1904)",
        "currency": "TWD",
        "fair_value": 29.0,
        "mos_buy": 0.12,
        "expensive_sell": 0.25,
    },
    {
        "ticker": "2616.TW",
        "name": "山隆 (2616)",
        "currency": "TWD",
        "fair_value": 31.0,
        "mos_buy": 0.10,
        "expensive_sell": 0.20,
    }
]

# ==========================================
# 3. 訊息推播模組
# ==========================================
def send_telegram(token: str, chat_id: str, text: str) -> bool:
    if not token or token == "YOUR_TELEGRAM_BOT_TOKEN":
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try:
        res = requests.post(url, json=payload, timeout=10)
        return res.status_code == 200
    except Exception as e:
        print(f"[Telegram 失敗]: {e}")
        return False

def send_line(token: str, user_id: str, text: str) -> bool:
    if not token or not user_id:
        return False
    url = "https://api.line.me/v2/bot/message/push"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    payload = {"to": user_id, "messages": [{"type": "text", "text": text}]}
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        return res.status_code == 200
    except Exception as e:
        print(f"[LINE 失敗]: {e}")
        return False

def broadcast_alert(message: str):
    send_telegram(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, message)
    send_line(LINE_CHANNEL_ACCESS_TOKEN, LINE_USER_ID, message)

# ==========================================
# 4. 價格擷取與邏輯判斷
# ==========================================
def fetch_price(ticker: str) -> Optional[float]:
    try:
        stock = yf.Ticker(ticker)
        fast_info = getattr(stock, "fast_info", None)
        if fast_info and hasattr(fast_info, "last_price") and fast_info.last_price:
            return round(float(fast_info.last_price), 2)
        hist = stock.history(period="1d")
        if not hist.empty:
            return round(float(hist["Close"].iloc[-1]), 2)
    except Exception as e:
        print(f"[{ticker}] 抓取失敗: {e}")
    return None

def evaluate_portfolio():
    print("===== 開始持股健康檢核與安全邊際監控 =====")
    alerts = []

    for item in PORTFOLIO_WATCHLIST:
        ticker = item["ticker"]
        name = item["name"]
        curr_symbol = "$" if item["currency"] == "USD" else "NT$"
        fair_val = item["fair_value"]
        
        buy_threshold = round(fair_val * (1 - item["mos_buy"]), 2)
        sell_threshold = round(fair_val * (1 + item["expensive_sell"]), 2)

        current_price = fetch_price(ticker)
        if current_price is None:
            print(f"⚠️ [{name}] 無法取得即時報價，略過。")
            continue

        # 計算溢折價比率
        diff_pct = ((current_price - fair_val) / fair_val) * 100
        print(f"[{name}] 市價: {curr_symbol}{current_price} | 合理價: {curr_symbol}{fair_val} (偏離: {diff_pct:+.1f}%)")

        # 1. 觸發安全邊際買進訊號
        if current_price <= buy_threshold:
            discount = abs(diff_pct)
            msg = (
                f"🟢 *【買進訊號：進入安全邊際】*\n\n"
                f"標的：*{name}*\n"
                f"當前價格：*{curr_symbol}{current_price}*\n"
                f"合理價值：{curr_symbol}{fair_val}\n"
                f"買進防守線：{curr_symbol}{buy_threshold} (折價 {item['mos_buy']*100:.0f}%)\n"
                f"目前折價：*{discount:.1f}%*\n\n"
                f"📌 *操作建議*：已落入低估區間，可執行分批建倉或啟動 Trailing Stop Buy。"
            )
            alerts.append(msg)

        # 2. 觸發高估值透支警報 (再平衡 / 停利)
        elif current_price >= sell_threshold:
            premium = diff_pct
            msg = (
                f"🔴 *【高估警戒：透支未來預期】*\n\n"
                f"標的：*{name}*\n"
                f"當前價格：*{curr_symbol}{current_price}*\n"
                f"合理價值：{curr_symbol}{fair_val}\n"
                f"溢價警戒線：{curr_symbol}{sell_threshold} (溢價 {item['expensive_sell']*100:.0f}%)\n"
                f"目前溢價：*{premium:.1f}%*\n\n"
                f"📌 *操作建議*：估值透支風險增加，可掛設 Trailing Stop 保護利潤或進行再平衡。"
            )
            alerts.append(msg)

    # 發送所有符合條件的警報
    if alerts:
        for alert in alerts:
            broadcast_alert(alert)
        print(f"✅ 已成功推播 {len(alerts)} 則到價提醒。")
    else:
        print("💡 所有標的目前皆在合理震盪區間內，無須動作。")

if __name__ == "__main__":
    evaluate_portfolio()
