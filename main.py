import os
import requests
import pandas as pd
import yfinance as yf
from typing import Dict, List, Optional, Tuple

# ==========================================
# 1. 系統設定與憑證
# ==========================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_USER_ID = os.getenv("LINE_USER_ID", "")

# 專屬雲端儀表板網址 (GitHub Pages)
DASHBOARD_URL = "https://eric80218.github.io/Stock-alert/"

# ==========================================
# 2. 持股監控清單 (台股與美股)
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
# 3. AI 技術線型與趨勢操作判讀核心
# ==========================================
def analyze_market_data(ticker: str) -> Optional[dict]:
    try:
        stock = yf.Ticker(ticker)
        df = stock.history(period="1y")
        if df.empty or len(df) < 25:
            return None
        
        close = df['Close']
        curr_price = round(float(close.iloc[-1]), 2)
        prev_price = round(float(close.iloc[-2]), 2)
        
        # 20MA (月線)、60MA (季線)
        ma20_series = close.rolling(window=20).mean()
        curr_ma20 = round(float(ma20_series.iloc[-1]), 2)
        prev_ma20 = round(float(ma20_series.iloc[-2]), 2)
        curr_ma60 = round(float(close.rolling(window=60).mean().iloc[-1]), 2) if len(close) >= 60 else None

        # RSI (14)
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, 0.0001)
        rsi_val = (100 - (100 / (1 + rs))).iloc[-1]
        curr_rsi = round(float(rsi_val), 1)

        # 線型策略自動判讀
        action_type = "HOLD"
        action_title = "區間整理"
        action_color = "#38BDF8"
        strategy_reason = "股價處於均線常態波動區間，無重大突破或跌破。"

        if prev_price <= prev_ma20 and curr_price > curr_ma20:
            action_type = "BUY_SIGNAL"
            action_title = "🟢 右側翻多：強勢站上月線"
            action_color = "#10B981"
            strategy_reason = f"今日股價由下往上突破 20MA (${curr_ma20})，短線動能翻多，屬絕佳右側建倉買點。"
        elif curr_price > curr_ma20 and (curr_ma60 and curr_ma20 > curr_ma60) and ((curr_price - curr_ma20) / curr_ma20 <= 0.025):
            action_type = "BUY_SIGNAL"
            action_title = "🟢 多頭有守：回測月線支撐"
            action_color = "#10B981"
            strategy_reason = f"均線維持多頭排列，回測 20MA (${curr_ma20}) 未破，具備高盈虧比加碼優勢。"
        elif prev_price >= prev_ma20 and curr_price < curr_ma20:
            action_type = "WEAK_SIGNAL"
            action_title = "🔴 短多轉弱：摜破月線防守"
            action_color = "#EF4444"
            strategy_reason = f"收盤失守重要防守均線 20MA (${curr_ma20})，短線進入修正期，建議多單暫停加碼或部分調節。"
        elif curr_rsi <= 28:
            action_type = "OVERSOLD"
            action_title = "🟡 嚴重超賣：恐慌打底區"
            action_color = "#F59E0B"
            strategy_reason = f"RSI 僅 {curr_rsi} 進入極度超賣區，空方力竭隨時出現技術反彈，切忌恐慌殺低。"
        elif curr_rsi >= 75:
            action_type = "OVERBOUGHT"
            action_title = "⚠️ 短線過熱：動能極端超買"
            action_color = "#EC4899"
            strategy_reason = f"RSI 高達 {curr_rsi}，短線指標嚴重過熱且遠離均線，慎防獲利回吐賣壓。"

        return {
            "current_price": curr_price,
            "ma20": curr_ma20,
            "ma60": curr_ma60,
            "rsi": curr_rsi,
            "action_type": action_type,
            "action_title": action_title,
            "action_color": action_color,
            "strategy_reason": strategy_reason
        }
    except Exception as e:
        print(f"[{ticker}] 分析出錯: {e}")
        return None

# ==========================================
# 4. LINE Flex Message 卡片生成
# ==========================================
def build_line_bubble(card_info: dict) -> dict:
    """單檔標的警報卡片"""
    is_buy = card_info["status_category"] == "BUY"
    status_label = "🟢 進入安全邊際" if is_buy else ("🔴 估值過熱警戒" if card_info["status_category"] == "SELL" else "⚡ 線型關鍵轉折")
    status_badge_color = "#10B981" if is_buy else ("#EF4444" if card_info["status_category"] == "SELL" else "#38BDF8")
    
    diff_text = f"折價 {abs(card_info['diff_pct']):.1f}%" if card_info['diff_pct'] < 0 else f"溢價 {card_info['diff_pct']:.1f}%"
    yahoo_chart_url = f"https://finance.yahoo.com/quote/{card_info['ticker']}"

    return {
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
                        {"type": "text", "text": card_info["name"], "weight": "bold", "color": "#FFFFFF", "size": "md", "flex": 3},
                        {"type": "text", "text": status_label, "weight": "bold", "color": status_badge_color, "size": "xs", "align": "end", "flex": 2}
                    ]
                },
                {"type": "text", "text": f"{card_info['ticker']} · {diff_text}", "color": "#94A3B8", "size": "xs", "margin": "xs"}
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
                        {"type": "text", "text": "當前股價", "color": "#94A3B8", "size": "xs"},
                        {"type": "text", "text": f"{card_info['curr_symbol']}{card_info['price']}", "color": "#FFFFFF", "weight": "bold", "size": "sm", "align": "end"}
                    ]
                },
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "合理價值", "color": "#94A3B8", "size": "xs"},
                        {"type": "text", "text": f"{card_info['curr_symbol']}{card_info['fair_val']}", "color": "#CBD5E1", "size": "xs", "align": "end"}
                    ]
                },
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "均線與指標", "color": "#94A3B8", "size": "xs"},
                        {"type": "text", "text": f"20MA: {card_info['ma20']} | RSI: {card_info['rsi']}", "color": "#CBD5E1", "size": "xs", "align": "end"}
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
                        {"type": "text", "text": card_info["action_title"], "color": card_info["action_color"], "weight": "bold", "size": "xs"},
                        {"type": "text", "text": card_info["strategy_reason"], "color": "#E2E8F0", "size": "xxs", "wrap": True, "margin": "xs"}
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

def build_rotation_bubble(from_item: dict, to_item: dict, spread: float) -> dict:
    """專屬【持股轉換 / 資金輪動建議】卡片"""
    return {
        "type": "bubble",
        "size": "kilo",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#312E81",  # 沉穩靛藍色
            "paddingAll": "16px",
            "contents": [
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "🔄 資金輪動再平衡", "weight": "bold", "color": "#FFFFFF", "size": "md", "flex": 3},
                        {"type": "text", "text": f"{from_item['currency']} 部位", "color": "#A5B4FC", "size": "xs", "align": "end", "flex": 2}
                    ]
                },
                {"type": "text", "text": f"估值性價比差距達 {spread:.1f}%", "color": "#C7D2FE", "size": "xs", "margin": "xs"}
            ]
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#0F172A",
            "paddingAll": "16px",
            "spacing": "md",
            "contents": [
                # 轉出標的
                {
                    "type": "box",
                    "layout": "vertical",
                    "backgroundColor": "#1E293B",
                    "paddingAll": "10px",
                    "cornerRadius": "8px",
                    "contents": [
                        {
                            "type": "box",
                            "layout": "horizontal",
                            "contents": [
                                {"type": "text", "text": f"📤 調節轉出：{from_item['name']}", "weight": "bold", "color": "#F87171", "size": "xs"},
                                {"type": "text", "text": f"溢價 {from_item['diff_pct']:+.1f}%", "color": "#FCA5A5", "size": "xs", "align": "end"}
                            ]
                        },
                        {"type": "text", "text": f"市價 {from_item['curr_symbol']}{from_item['price']} (估值透支/高檔轉弱，鎖定獲利)", "color": "#94A3B8", "size": "xxs", "margin": "xs"}
                    ]
                },
                # 箭頭分隔
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "⬇️ 資金換手轉進", "color": "#38BDF8", "size": "xs", "align": "center", "weight": "bold"}
                    ]
                },
                # 轉進標的
                {
                    "type": "box",
                    "layout": "vertical",
                    "backgroundColor": "#1E293B",
                    "paddingAll": "10px",
                    "cornerRadius": "8px",
                    "contents": [
                        {
                            "type": "box",
                            "layout": "horizontal",
                            "contents": [
                                {"type": "text", "text": f"📥 潛力轉進：{to_item['name']}", "weight": "bold", "color": "#34D399", "size": "xs"},
                                {"type": "text", "text": f"折價 {abs(to_item['diff_pct']):.1f}%", "color": "#6EE7B7", "size": "xs", "align": "end"}
                            ]
                        },
                        {"type": "text", "text": f"市價 {to_item['curr_symbol']}{to_item['price']} (落入安全邊際，具高勝率保護)", "color": "#94A3B8", "size": "xxs", "margin": "xs"}
                    ]
                },
                {"type": "separator", "color": "#334155"},
                {"type": "text", "text": "💡 策略效益：此操作能有效落袋高位浮盈，將籌碼轉移至高安全邊際標的，提升總投組抗震力。", "color": "#E2E8F0", "size": "xxs", "wrap": True}
            ]
        },
        "footer": {
            "type": "box",
            "layout": "horizontal",
            "backgroundColor": "#1E293B",
            "paddingAll": "10px",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "height": "sm",
                    "color": "#4F46E5",
                    "action": {"type": "uri", "label": "開啟估值儀表板試算", "uri": DASHBOARD_URL}
                }
            ]
        }
    }

# ==========================================
# 5. 推播發送模組
# ==========================================
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
                "altText": f"📊 投資決策報告：{len(bubbles)} 張重要分析卡片已生成！",
                "contents": {
                    "type": "carousel",
                    "contents": bubbles[:12]
                }
            }
        ]
    }
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        if res.status_code == 200:
            print(f"✅ LINE Flex 輪動決策推播成功（共 {len(bubbles)} 張卡片，僅計 1 則額度）！")
        else:
            print(f"❌ LINE 推播失敗: {res.status_code} - {res.text}")
    except Exception as e:
        print(f"LINE 請求異常: {e}")

def push_telegram_digest(token: str, chat_id: str, alerts: List[dict], rotations: List[dict]):
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    lines = [f"📊 *【台美股線型、估值與換股決策日報】*", "---------------------------"]
    for a in alerts:
        lines.append(f"*{a['name']} ({a['ticker']})* · `{a['curr_symbol']}{a['price']}`\n• {a['action_title']}\n• 指標：`20MA: {a['ma20']} | RSI: {a['rsi']}`\n")
    if rotations:
        lines.append("🔄 *【換股輪動建議】*")
        for r in rotations:
            lines.append(f"• 調節 `{r['from']['name']}` (溢價 {r['from']['diff_pct']:+.1f}%)\n  ➡️ 轉進 `{r['to']['name']}` (折價 {abs(r['to']['diff_pct']):.1f}%) | 差距: `{r['spread']:.1f}%`\n")
    
    payload = {"chat_id": chat_id, "text": "\n".join(lines), "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
        print("✓ Telegram 摘要推播完成。")
    except Exception as e:
        print(f"Telegram 發送異常: {e}")

# ==========================================
# 6. 主執行流程
# ==========================================
def main():
    print("===== 開始持股【估值 + 線型 + 換股再平衡】智能掃描 =====")
    all_evaluated = []
    triggered_cards = []
    telegram_items = []

    # 1. 遍歷並評估每檔股票
    for item in PORTFOLIO_WATCHLIST:
        ticker = item["ticker"]
        name = item["name"]
        fair_val = item["fair_value"]
        currency = item["currency"]
        curr_symbol = "$" if currency == "USD" else "NT$"
        
        buy_target = round(fair_val * (1 - item["mos_buy"]), 2)
        sell_target = round(fair_val * (1 + item["expensive_sell"]), 2)

        tech = analyze_market_data(ticker)
        if not tech:
            continue

        price = tech["current_price"]
        diff_pct = ((price - fair_val) / fair_val) * 100
        print(f"[{name}] 市價: {curr_symbol}{price} | 偏離: {diff_pct:+.1f}% | 行動: {tech['action_title']}")

        status_category = "NORMAL"
        if price <= buy_target:
            status_category = "BUY"
        elif price >= sell_target:
            status_category = "SELL"
        elif tech["action_type"] in ["BUY_SIGNAL", "WEAK_SIGNAL"]:
            status_category = "TECH"

        data = {
            "name": name,
            "ticker": ticker,
            "currency": currency,
            "curr_symbol": curr_symbol,
            "price": price,
            "fair_val": fair_val,
            "diff_pct": diff_pct,
            "ma20": tech["ma20"],
            "rsi": tech["rsi"],
            "action_type": tech["action_type"],
            "action_title": tech["action_title"],
            "action_color": tech["action_color"],
            "strategy_reason": tech["strategy_reason"],
            "status_category": status_category
        }
        all_evaluated.append(data)

        # 若符合警報條件，加入單檔卡片
        if status_category != "NORMAL":
            triggered_cards.append(build_line_bubble(data))
            telegram_items.append(data)

    # 2. 智慧換股配對引擎 (分台股與美股獨立計算)
    rotation_records = []
    for cur in ["USD", "TWD"]:
        cur_stocks = [s for s in all_evaluated if s["currency"] == cur]
        # 尋找高估/轉弱的賣出候選 (溢價最高者)
        sell_candidates = sorted([s for s in cur_stocks if s["diff_pct"] >= 20.0 or s["action_type"] == "WEAK_SIGNAL"], key=lambda x: x["diff_pct"], reverse=True)
        # 尋找具備深厚安全邊際的買進候選 (折價最多者)
        buy_candidates = sorted([s for s in cur_stocks if s["diff_pct"] <= -8.0], key=lambda x: x["diff_pct"])

        if sell_candidates and buy_candidates:
            top_sell = sell_candidates[0]
            top_buy = buy_candidates[0]
            spread = top_sell["diff_pct"] - top_buy["diff_pct"]
            
            # 若性價比差距達標 (>= 28%)，產生換股建議卡片
            if spread >= 28.0:
                print(f"💡 發現【{cur}】高性價比換股機會：{top_sell['name']} ➡️ {top_buy['name']} (差距 {spread:.1f}%)")
                triggered_cards.append(build_rotation_bubble(top_sell, top_buy, spread))
                rotation_records.append({"from": top_sell, "to": top_buy, "spread": spread})

    # 3. 執行推播
    if triggered_cards:
        push_line_flex(LINE_CHANNEL_ACCESS_TOKEN, LINE_USER_ID, triggered_cards)
        push_telegram_digest(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, telegram_items, rotation_records)
    else:
        print("💡 目前所有標的處於合理區間，且無顯著性價比換股機會。")

if __name__ == "__main__":
    main()
