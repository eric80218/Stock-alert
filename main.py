import os
import sys
import json
import requests
import pandas as pd
import yfinance as yf
from typing import Dict, List, Optional, Tuple

# ==========================================
# 1. 系統設定、憑證與環境變數
# ==========================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_USER_ID = os.getenv("LINE_USER_ID", "")

# 執行模式 (ALL: 全掃, TWD: 僅台股, USD: 僅美股)
TARGET_MARKET = os.getenv("TARGET_MARKET", "ALL").upper()
# 手動觸發時強制推播 (忽略快取)
FORCE_NOTIFY = os.getenv("FORCE_NOTIFY", "false").lower() == "true"

DASHBOARD_URL = "https://eric80218.github.io/Stock-alert/"
CACHE_FILE = "state_cache.json"

# ==========================================
# 2. 專屬持股監控清單
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
# 3. 狀態記憶模組 (防止重複警報洗版)
# ==========================================
def load_state_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_state_cache(cache: dict):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 快取儲存異常: {e}")

# ==========================================
# 4. 大盤環境濾網 (Market Regime Filter)
# ==========================================
def check_market_regime(currency: str) -> dict:
    """美股看 SPY，台股看 ^TWII；判斷指數是否處於 120MA (半年線) 之上"""
    benchmark_ticker = "SPY" if currency == "USD" else "^TWII"
    try:
        df = yf.Ticker(benchmark_ticker).history(period="1y")
        if len(df) < 120:
            return {"is_bull": True, "label": "常態多頭", "benchmark": benchmark_ticker}
        close = df['Close']
        curr = float(close.iloc[-1])
        ma120 = float(close.rolling(120).mean().iloc[-1])
        is_bull = curr >= ma120
        return {
            "is_bull": is_bull,
            "label": "🟢 大盤多頭" if is_bull else "⚠️ 大盤偏空逆風",
            "benchmark": benchmark_ticker,
            "ma120": round(ma120, 2),
            "price": round(curr, 2)
        }
    except Exception:
        return {"is_bull": True, "label": "大盤數據正常", "benchmark": benchmark_ticker}

# ==========================================
# 5. 量化風控計算 (停損點與風報比 R/R)
# ==========================================
def calculate_risk_reward(price: float, fair_val: float, ma20: float, low_10d: float) -> Tuple[float, Optional[float]]:
    """
    計算建議停損點：取 近10日低點 與 (20MA * 0.97) 之較小防守位
    計算風報比 (Reward / Risk)
    """
    stop_loss = round(min(low_10d, ma20 * 0.97), 2)
    if stop_loss >= price:
        stop_loss = round(price * 0.95, 2)  # 防呆：預設 5% 防守位

    risk = price - stop_loss
    reward = fair_val - price

    rr_ratio = None
    if risk > 0 and reward > 0:
        rr_ratio = round(reward / risk, 1)

    return stop_loss, rr_ratio

# ==========================================
# 6. 技術分析與立即決策演算法
# ==========================================
def analyze_stock(ticker: str) -> Optional[dict]:
    try:
        stock = yf.Ticker(ticker)
        df = stock.history(period="1y")
        if df.empty or len(df) < 25:
            return None
        
        close = df['Close']
        low = df['Low']
        curr_price = round(float(close.iloc[-1]), 2)
        prev_price = round(float(close.iloc[-2]), 2)
        low_10d = round(float(low.tail(10).min()), 2)

        ma20_s = close.rolling(20).mean()
        curr_ma20 = round(float(ma20_s.iloc[-1]), 2)
        prev_ma20 = round(float(ma20_s.iloc[-2]), 2)
        curr_ma60 = round(float(close.rolling(60).mean().iloc[-1]), 2) if len(close) >= 60 else None

        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, 0.0001)
        rsi = round(float((100 - (100 / (1 + rs))).iloc[-1]), 1)

        return {
            "price": curr_price,
            "prev_price": prev_price,
            "ma20": curr_ma20,
            "prev_ma20": prev_ma20,
            "ma60": curr_ma60,
            "rsi": rsi,
            "low_10d": low_10d
        }
    except Exception as e:
        print(f"[{ticker}] 數據異常: {e}")
        return None

def evaluate_decision(item: dict, data: dict, market_regime: dict) -> dict:
    price = data["price"]
    fair_val = item["fair_value"]
    diff_pct = ((price - fair_val) / fair_val) * 100
    buy_threshold_pct = - (item["mos_buy"] * 100)
    sell_threshold_pct = item["expensive_sell"] * 100

    score = 0.0

    # 1. 基本面安全邊際得分
    if diff_pct <= buy_threshold_pct:
        score += 2.0
    elif diff_pct < 0:
        score += 0.5
    elif diff_pct >= sell_threshold_pct:
        score -= 2.0
    elif diff_pct > 10:
        score -= 0.5

    # 2. 均線位階得分
    if data["prev_price"] <= data["prev_ma20"] and price > data["ma20"]:
        score += 1.5  # 突破月線翻多
    elif price > data["ma20"]:
        score += 0.5
    
    if data["prev_price"] >= data["prev_ma20"] and price < data["ma20"]:
        score -= 1.5  # 摜破月線
    elif price < data["ma20"]:
        score -= 0.5

    # 3. RSI 動能
    if data["rsi"] <= 30:
        score += 1.0
    elif data["rsi"] >= 75:
        score -= 1.0

    # 4. 大盤環境濾網修正 (若大盤走空，下修強烈買進訊號)
    if not market_regime["is_bull"] and score >= 2.0:
        score = 1.2  # 降級為逢低微量加碼，避免在系統性暴跌中接重倉

    # 計算風報比與防守停損點
    stop_loss, rr_ratio = calculate_risk_reward(price, fair_val, data["ma20"], data["low_10d"])

    # 決定立即指標
    if score >= 2.5:
        signal_badge = "🔥 立即買進"
        badge_color = "#10B981"
        header_color = "#064E3B"
        action_advice = f"【右側建倉買點】估值落入安全邊際，且技術面翻多。建議停損設於 {item['currency']=='USD' and '$' or 'NT$'}{stop_loss}，潛在風報比 1:{rr_ratio or '佳'}。"
    elif score >= 1.0:
        signal_badge = "🟢 逢低加碼"
        badge_color = "#34D399"
        header_color = "#065F46"
        action_advice = f"【具性價比優勢】回測支撐有守，可採定期定額或掛單分批承接。防守停損線 {item['currency']=='USD' and '$' or 'NT$'}{stop_loss}。"
    elif score <= -2.5:
        signal_badge = "🔴 立即賣出"
        badge_color = "#EF4444"
        header_color = "#7F1D1D"
        action_advice = "【估值嚴重透支】價格大幅偏離內在價值，強烈建議獲利了結或啟動移動停利（Trailing Stop）。"
    elif score <= -1.0:
        signal_badge = "🟠 建議減碼"
        badge_color = "#F97316"
        header_color = "#7C2D12"
        action_advice = "【跌破短線支撐】失守 20MA 防守均線，短線進入修正期，建議多單部分減碼或暫停買進。"
    else:
        signal_badge = "⚪ 觀望續抱"
        badge_color = "#94A3B8"
        header_color = "#1E293B"
        action_advice = "【常態區間】未達顯著買賣標準，持股續抱，耐心等待趨勢明朗。"

    return {
        "score": round(score, 1),
        "diff_pct": diff_pct,
        "signal_badge": signal_badge,
        "badge_color": badge_color,
        "header_color": header_color,
        "action_advice": action_advice,
        "stop_loss": stop_loss,
        "rr_ratio": rr_ratio,
        "is_active_signal": (score >= 1.0 or score <= -1.0)
    }

# ==========================================
# 7. LINE Flex Message 卡片建構
# ==========================================
def build_line_bubble(data: dict, market_regime: dict) -> dict:
    diff_text = f"折價 {abs(data['diff_pct']):.1f}%" if data['diff_pct'] < 0 else f"溢價 {data['diff_pct']:.1f}%"
    yahoo_chart_url = f"https://finance.yahoo.com/quote/{data['ticker']}"
    rr_text = f"1 : {data['rr_ratio']}" if data['rr_ratio'] else "N/A"

    return {
        "type": "bubble",
        "size": "kilo",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": data["header_color"],
            "paddingAll": "16px",
            "contents": [
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": data["name"], "weight": "bold", "color": "#FFFFFF", "size": "md", "flex": 3},
                        {"type": "text", "text": data["signal_badge"], "weight": "bold", "color": data["badge_color"], "size": "sm", "align": "end", "flex": 3}
                    ]
                },
                {
                    "type": "box",
                    "layout": "horizontal",
                    "margin": "xs",
                    "contents": [
                        {"type": "text", "text": f"{data['ticker']} · {diff_text}", "color": "#CBD5E1", "size": "xs", "flex": 3},
                        {"type": "text", "text": market_regime["label"], "color": market_regime["is_bull"] and "#A7F3D0" or "#FCA5A5", "size": "xxs", "align": "end", "flex": 2}
                    ]
                }
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
                        {"type": "text", "text": f"{data['curr_symbol']}{data['price']}", "color": "#FFFFFF", "weight": "bold", "size": "sm", "align": "end"}
                    ]
                },
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "內在合理價", "color": "#94A3B8", "size": "xs"},
                        {"type": "text", "text": f"{data['curr_symbol']}{data['fair_val']}", "color": "#94A3B8", "size": "xs", "align": "end"}
                    ]
                },
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "均線與指標", "color": "#94A3B8", "size": "xs"},
                        {"type": "text", "text": f"20MA: {data['ma20']} | RSI: {data['rsi']}", "color": "#94A3B8", "size": "xs", "align": "end"}
                    ]
                },
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "建議防守停損", "color": "#F87171", "size": "xs"},
                        {"type": "text", "text": f"{data['curr_symbol']}{data['stop_loss']} (風報比 {rr_text})", "color": "#FCA5A5", "size": "xs", "align": "end"}
                    ]
                },
                {"type": "separator", "color": "#334155", "margin": "md"},
                {
                    "type": "box",
                    "layout": "vertical",
                    "backgroundColor": "#1E293B",
                    "paddingAll": "12px",
                    "cornerRadius": "8px",
                    "margin": "md",
                    "contents": [
                        {"type": "text", "text": "🎯 立即行動方針：", "color": "#38BDF8", "weight": "bold", "size": "xs"},
                        {"type": "text", "text": data["action_advice"], "color": "#F8FAFC", "size": "xxs", "wrap": True, "margin": "xs"}
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

def push_line_flex(token: str, user_id: str, bubbles: List[dict]):
    if not token or not user_id or not bubbles:
        return
    url = "https://api.line.me/v2/bot/message/push"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    payload = {
        "to": user_id,
        "messages": [
            {
                "type": "flex",
                "altText": f"🚨 持股決策轉折通知：{len(bubbles)} 檔標的出現最新訊號！",
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
            print(f"✅ LINE Flex 推播成功（共 {len(bubbles)} 檔卡片）！")
        else:
            print(f"❌ LINE 推播失敗: {res.status_code} - {res.text}")
    except Exception as e:
        print(f"LINE 請求異常: {e}")

# ==========================================
# 8. 主流程
# ==========================================
def main():
    print(f"===== 啟動持股監控系統 (目標市場: {TARGET_MARKET} | 強制推播: {FORCE_NOTIFY}) =====")
    
    # 預載大盤環境
    regimes = {
        "USD": check_market_regime("USD"),
        "TWD": check_market_regime("TWD")
    }
    print(f"🏛️ 美股大盤狀態: {regimes['USD']['label']} (SPY: {regimes['USD'].get('price')})")
    print(f"🏛️ 台股大盤狀態: {regimes['TWD']['label']} (^TWII: {regimes['TWD'].get('price')})")

    # 讀取狀態快取
    state_cache = load_state_cache()
    new_state_cache = dict(state_cache)

    actionable_cards = []

    for item in PORTFOLIO_WATCHLIST:
        currency = item["currency"]
        # 時區市場過濾
        if TARGET_MARKET != "ALL" and currency != TARGET_MARKET:
            continue

        ticker = item["ticker"]
        name = item["name"]
        curr_symbol = "$" if currency == "USD" else "NT$"

        data = analyze_stock(ticker)
        if not data:
            continue

        market_regime = regimes[currency]
        decision = evaluate_decision(item, data, market_regime)

        current_signal = decision["signal_badge"]
        last_signal = state_cache.get(ticker)
        new_state_cache[ticker] = current_signal

        print(f"[{name}] 當前: {current_signal} | 上次: {last_signal or '無紀錄'} | 市價: {curr_symbol}{data['price']}")

        # 判定是否推播：
        # 條件 1：處於重要訊號狀態 (非觀望)
        # 條件 2：訊號「發生狀態實質改變」OR「使用者手動執行 (FORCE_NOTIFY)」
        is_state_changed = (current_signal != last_signal)
        should_alert = decision["is_active_signal"] and (is_state_changed or FORCE_NOTIFY)

        if should_alert:
            card_info = {
                "name": name,
                "ticker": ticker,
                "curr_symbol": curr_symbol,
                "price": data["price"],
                "fair_val": item["fair_value"],
                "ma20": data["ma20"],
                "rsi": data["rsi"],
                **decision
            }
            actionable_cards.append(build_line_bubble(card_info, market_regime))

    # 執行推播並更新快取檔
    if actionable_cards:
        push_line_flex(LINE_CHANNEL_ACCESS_TOKEN, LINE_USER_ID, actionable_cards)
    else:
        print("💡 所有標的狀態未變動或處於觀望狀態，無須打擾。")

    save_state_cache(new_state_cache)
    print("===== 掃描流程完畢 =====")

if __name__ == "__main__":
    main()
