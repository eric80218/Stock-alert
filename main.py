import os
import requests
import pandas as pd
import yfinance as yf
from typing import Dict, List, Optional, Tuple

# ==========================================
# 1. 憑證與雲端儀表板設定
# ==========================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_USER_ID = os.getenv("LINE_USER_ID", "")

# 你的 GitHub Pages 專屬網址
DASHBOARD_URL = "https://eric80218.github.io/Stock-alert/"

# ==========================================
# 2. 專屬持股監控清單 (台股與美股)
# ==========================================
PORTFOLIO_WATCHLIST = [
    # --- 美股部位 ---
    {"ticker": "AMAT", "name": "應用材料", "currency": "USD", "fair_value": 210.0, "mos_buy": 0.15, "expensive_sell": 0.25},
    {"ticker": "NVDA", "name": "輝達", "currency": "USD", "fair_value": 135.0, "mos_buy": 0.15, "expensive_sell": 0.30},
    {"ticker": "COST", "name": "好市多", "currency": "USD", "fair_value": 850.0, "mos_buy": 0.10, "expensive_sell": 0.20},
    {"ticker": "BRK-B", "name": "波克夏 B", "currency": "USD", "fair_value": 450.0, "mos_buy": 0.10, "expensive_sell": 0.20},
    {"ticker": "LLY", "name": "禮來製藥", "currency": "USD", "fair_value": 880.0, "mos_buy": 0.15, "expensive_sell": 0.25},
    {"ticker": "VRT", "name": "維諦技術", "currency": "USD", "fair_value": 110.0, "mos_buy": 0.20, "expensive_sell": 0.35},
    {"ticker": "INTC", "name": "英特爾", "currency": "USD", "fair_value": 24.0, "mos_buy": 0.15, "expensive_sell": 0.25},

    # --- 台股部位 ---
    {"ticker": "0050.TW", "name": "元大台灣50", "currency": "TWD", "fair_value": 185.0, "mos_buy": 0.10, "expensive_sell": 0.20},
    {"ticker": "2337.TW", "name": "旺宏", "currency": "TWD", "fair_value": 28.0, "mos_buy": 0.15, "expensive_sell": 0.25},
    {"ticker": "2002.TW", "name": "中鋼", "currency": "TWD", "fair_value": 23.5, "mos_buy": 0.10, "expensive_sell": 0.20},
    {"ticker": "1232.TW", "name": "大統益", "currency": "TWD", "fair_value": 155.0, "mos_buy": 0.10, "expensive_sell": 0.20},
    {"ticker": "1904.TW", "name": "正隆", "currency": "TWD", "fair_value": 29.0, "mos_buy": 0.12, "expensive_sell": 0.25},
    {"ticker": "2616.TW", "name": "山隆", "currency": "TWD", "fair_value": 31.0, "mos_buy": 0.10, "expensive_sell": 0.20}
]

# ==========================================
# 3. 技術面計算模組 (RSI 與 200MA)
# ==========================================
def fetch_stock_data(ticker: str) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """取得最新價、200MA、RSI(14)"""
    try:
        stock = yf.Ticker(ticker)
        df = stock.history(period="1y")
        if df.empty or len(df) < 20:
            return None, None, None
        
        current_price = round(float(df['Close'].iloc[-1]), 2)
        
        # 200 日均線
        ma200 = round(float(df['Close'].rolling(window=200).mean().iloc[-1]), 2) if len(df) >= 150 else None
        
        # RSI 14 計算
        delta = df['Close'].diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.rolling(window=14, min_periods=14).mean()
        avg_loss = loss.rolling(window=14, min_periods=14).mean()
        rs = avg_gain / avg_loss.replace(0, 0.0001)
        rsi = round(float((100 - (100 / (1 + rs))).iloc[-1]), 1)
        
        return current_price, ma200, rsi
    except Exception as e:
        print(f"[{ticker}] 數據獲取異常: {e}")
        return None, None, None

# ==========================================
# 4. LINE Flex Message 卡片建構器
# ==========================================
def build_line_bubble(item_data: dict) -> dict:
    """單一標的之精緻卡片設計"""
    is_buy = item_data["type"] == "BUY"
    status_text = "🟢 進入安全邊際" if is_buy else "🔴 估值過熱警戒"
    status_color = "#10B981" if is_buy else "#EF4444"
    diff_text = f"折價 {abs(item_data['diff_pct']):.1f}%" if is_buy else f"溢價 {item_data['diff_pct']:.1f}%"
    
    # 技術面評估文字
    rsi = item_data["rsi"]
    tech_tip = "中性整理"
    if rsi and rsi <= 30:
        tech_tip = f"🔥 極度超賣 (RSI {rsi})，反彈機率高"
    elif rsi and rsi >= 70:
        tech_tip = f"⚠️ 嚴重超買 (RSI {rsi})，防追高回檔"
    elif rsi:
        tech_tip = f"常規區間 (RSI {rsi})"

    # Yahoo 股價走勢連結
    clean_ticker = item_data['ticker']
    yahoo_chart_url = f"https://finance.yahoo.com/quote/{clean_ticker}"

    bubble = {
        "type": "bubble",
        "size": "kilo",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#1E293B",
            "paddingAll": "16px",
            "contents": [
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": item_data["name"], "weight": "bold", "color": "#FFFFFF", "size": "md", "flex": 3},
                        {"type": "text", "text": status_text, "weight": "bold", "color": status_color, "size": "xs", "align": "end", "flex": 2}
                    ]
                },
                {"type": "text", "text": f"{item_data['ticker']} · {diff_text}", "color": "#94A3B8", "size": "xs", "margin": "xs"}
            ]
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#0F172A",
            "paddingAll": "16px",
            "spacing": "sm",
            "contents": [
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "當前市價", "color": "#94A3B8", "size": "xs"},
                        {"type": "text", "text": f"{item_data['curr_symbol']}{item_data['price']}", "color": "#F8FAFC", "weight": "bold", "size": "sm", "align": "end"}
                    ]
                },
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "內在合理價", "color": "#94A3B8", "size": "xs"},
                        {"type": "text", "text": f"{item_data['curr_symbol']}{item_data['fair_val']}", "color": "#CBD5E1", "size": "xs", "align": "end"}
                    ]
                },
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "關鍵觸發門檻", "color": "#94A3B8", "size": "xs"},
                        {"type": "text", "text": f"{item_data['curr_symbol']}{item_data['target_price']}", "color": status_color, "weight": "bold", "size": "xs", "align": "end"}
                    ]
                },
                {"type": "separator", "color": "#334155", "margin": "md"},
                {
                    "type": "box",
                    "layout": "vertical",
                    "margin": "md",
                    "contents": [
                        {"type": "text", "text": f"📊 技術面：{tech_tip}", "color": "#38BDF8", "size": "xxs"}
                    ]
                }
            ]
        },
        "footer": {
            "type": "box",
            "layout": "horizontal",
            "backgroundColor": "#1E293B",
            "spacing": "sm",
            "paddingAll": "10px",
            "contents": [
                {
                    "type": "button",
                    "style": "secondary",
                    "height": "sm",
                    "color": "#334155",
                    "action": {"type": "uri", "label": "即時線圖", "uri": yahoo_chart_url}
                },
                {
                    "type": "button",
                    "style": "primary",
                    "height": "sm",
                    "color": "#0284C7",
                    "action": {"type": "uri", "label": "估值儀表板", "uri": DASHBOARD_URL}
                }
            ]
        }
    }
    return bubble

# ==========================================
# 5. 推播發送模組 (合併發送，只算 1 則)
# ==========================================
def push_line_flex(token: str, user_id: str, bubbles: List[dict]):
    """使用 LINE Flex Carousel 傳送多張卡片（僅計 1 則訊息額度）"""
    if not token or not user_id or not bubbles:
        return
    url = "https://api.line.me/v2/bot/message/push"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    
    # Carousel 最多支援 12 個 Bubble
    payload = {
        "to": user_id,
        "messages": [
            {
                "type": "flex",
                "altText": f"🔔 股票警報：{len(bubbles)} 檔標的已達關鍵門檻！",
                "contents": {
                    "type": "carousel",
                    "contents": bubbles[:10]
                }
            }
        ]
    }
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        if res.status_code == 200:
            print(f"✅ LINE Flex 卡片推播成功（含 {len(bubbles)} 檔標的，僅計 1 則額度）！")
        else:
            print(f"❌ LINE 推播失敗: {res.status_code} - {res.text}")
    except Exception as e:
        print(f"LINE 請求異常: {e}")

def push_telegram_digest(token: str, chat_id: str, text_alerts: List[str]):
    """Telegram 合併為單一 Markdown 摘要"""
    if not token or not chat_id or not text_alerts:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    full_text = f"📊 *【台美股到價監控日報】*\n共有 {len(text_alerts)} 檔標的達到關鍵門檻：\n" + ("-"*25) + "\n\n" + "\n\n".join(text_alerts)
    payload = {"chat_id": chat_id, "text": full_text, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
        print("✓ Telegram 整合報告發送完成。")
    except Exception as e:
        print(f"Telegram 異常: {e}")

# ==========================================
# 6. 核心排程邏輯
# ==========================================
def main():
    print("===== 開始台美股全方位估值與技術面檢核 =====")
    triggered_cards = []
    telegram_texts = []

    for item in PORTFOLIO_WATCHLIST:
        ticker = item["ticker"]
        name = item["name"]
        fair_val = item["fair_value"]
        curr_symbol = "$" if item["currency"] == "USD" else "NT$"
        
        buy_target = round(fair_val * (1 - item["mos_buy"]), 2)
        sell_target = round(fair_val * (1 + item["expensive_sell"]), 2)

        price, ma200, rsi = fetch_stock_data(ticker)
        if price is None:
            print(f"⚠️ [{name}] 無法取得完整行情，略過。")
            continue

        diff_pct = ((price - fair_val) / fair_val) * 100
        print(f"[{name}] 市價: {curr_symbol}{price} | 偏離: {diff_pct:+.1f}% | RSI: {rsi}")

        # 買進訊號 (跌破安全邊際)
        if price <= buy_target:
            item_data = {
                "name": name, "ticker": ticker, "price": price, "fair_val": fair_val,
                "target_price": buy_target, "diff_pct": diff_pct, "curr_symbol": curr_symbol,
                "rsi": rsi, "type": "BUY"
            }
            triggered_cards.append(build_line_bubble(item_data))
            telegram_texts.append(
                f"🟢 *{name} ({ticker})*\n"
                f"• 市價: `{curr_symbol}{price}` (折價 {abs(diff_pct):.1f}%)\n"
                f"• 安全買點: `{curr_symbol}{buy_target}` | RSI: `{rsi}`"
            )

        # 賣出/過熱警報 (溢價過高)
        elif price >= sell_target:
            item_data = {
                "name": name, "ticker": ticker, "price": price, "fair_val": fair_val,
                "target_price": sell_target, "diff_pct": diff_pct, "curr_symbol": curr_symbol,
                "rsi": rsi, "type": "SELL"
            }
            triggered_cards.append(build_line_bubble(item_data))
            telegram_texts.append(
                f"🔴 *{name} ({ticker}) 估值過熱*\n"
                f"• 市價: `{curr_symbol}{price}` (溢價 {diff_pct:.1f}%)\n"
                f"• 警戒門檻: `{curr_symbol}{sell_target}` | RSI: `{rsi}`"
            )

    # 執行合併推播
    if triggered_cards:
        push_line_flex(LINE_CHANNEL_ACCESS_TOKEN, LINE_USER_ID, triggered_cards)
        push_telegram_digest(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, telegram_texts)
    else:
        print("💡 所有標的目前皆在合理區間，無需發送推播。")

if __name__ == "__main__":
    main()
