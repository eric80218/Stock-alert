import os
import requests
import yfinance as yf

# ==========================================
# 1. 股票清單與設定 (範例標的)
# ==========================================
WATCHLIST = {
    "NVDA": {"fair_price": 130.0, "cost": 115.0},
    "AMAT": {"fair_price": 220.0, "cost": 190.0},
    "0050.TW": {"fair_price": 185.0, "cost": 165.0},
}


# ==========================================
# 2. 量化與安全邊際 / 風險評估引擎
# ==========================================
def evaluate_stock_risk(symbol: str, current_price: float, fair_price: float, cost: float) -> dict:
    """
    計算安全邊際與風險等級:
    - 安全邊際 (Margin of Safety) = (合理價 - 現價) / 合理價
    - 獲利率/回撤率 = (現價 - 成本價) / 成本價
    - 高風險: 現價大幅溢價 (安全邊際 < 0%) 或跌破停損
    - 中風險: 安全邊際偏低 (0% <= 安全邊際 < 15%)，處於警戒/觀望區
    - 低風險: 安全邊際充裕 (>= 15%)，落在價值投資安全建倉區
    """
    safety_margin = ((fair_price - current_price) / fair_price) * 100
    pnl_rate = ((current_price - cost) / cost) * 100

    if safety_margin < 0:
        risk_level = "高風險"
        risk_color = "#DC2626"  # 鮮紅 (超買/溢價警戒)
        risk_desc = "已高於合理價，溢價風險高，建議暫緩建倉或分批調節。"
    elif 0 <= safety_margin < 15:
        risk_level = "中風險"
        risk_color = "#F59E0B"  # 琥珀橘 (觀望警戒)
        risk_desc = "安全邊際有限，處於中度波動區，維持觀望或小幅調節。"
    else:
        risk_level = "低風險"
        risk_color = "#10B981"  # 翡翠綠 (安全邊際充足)
        risk_desc = "安全邊際充足，估值具備防禦性，符合進場標準。"

    return {
        "symbol": symbol,
        "current_price": round(current_price, 2),
        "fair_price": round(fair_price, 2),
        "cost": round(cost, 2),
        "safety_margin": round(safety_margin, 2),
        "pnl_rate": round(pnl_rate, 2),
        "risk_level": risk_level,
        "risk_color": risk_color,
        "risk_desc": risk_desc,
    }


# ==========================================
# 3. LINE Flex Message 產生器 (修復原 479 行語法中斷)
# ==========================================
def build_stock_flex_bubble(data: dict) -> dict:
    """建構單檔標的的 LINE Flex Bubble 字典結構"""
    return {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": data["risk_color"],
            "paddingAll": "15px",
            "contents": [
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [
                        {
                            "type": "text",
                            "text": data["symbol"],
                            "weight": "bold",
                            "color": "#FFFFFF",
                            "size": "xl",
                            "flex": 4,
                        },
                        {
                            "type": "text",
                            "text": f"[{data['risk_level']}]",
                            "weight": "bold",
                            "color": "#FFFFFF",
                            "size": "md",
                            "align": "end",
                            "flex": 3,
                        },
                    ],
                }
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "contents": [
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "當前現價", "color": "#6B7280", "size": "sm"},
                        {
                            "type": "text",
                            "text": f"${data['current_price']}",
                            "weight": "bold",
                            "size": "lg",
                            "align": "end",
                        },
                    ],
                },
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "安全邊際", "color": "#6B7280", "size": "sm"},
                        {
                            "type": "text",
                            "text": f"{data['safety_margin']}%",
                            "weight": "bold",
                            "color": data["risk_color"],
                            "size": "md",
                            "align": "end",
                        },
                    ],
                },
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "合理基準價", "color": "#6B7280", "size": "sm"},
                        {
                            "type": "text",
                            "text": f"${data['fair_price']}",
                            "size": "sm",
                            "color": "#374151",
                            "align": "end",
                        },
                    ],
                },
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "持股報酬率", "color": "#6B7280", "size": "sm"},
                        {
                            "type": "text",
                            "text": f"{'+' if data['pnl_rate'] > 0 else ''}{data['pnl_rate']}%",
                            "size": "sm",
                            "color": "#10B981" if data["pnl_rate"] >= 0 else "#EF4444",
                            "align": "end",
                        },
                    ],
                },
                {"type": "separator", "margin": "md"},
                {
                    "type": "text",
                    "text": data["risk_desc"],
                    "size": "xs",
                    "color": "#4B5563",
                    "wrap": True,
                    "margin": "md",
                },
            ],
        },
    }


# ==========================================
# 4. LINE 推播發送
# ==========================================
def send_line_alert(evaluated_list: list):
    """
    發送中高風險標的推播 (以 Carousel 輪播卡片呈現)
    """
    channel_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    user_id = os.environ.get("LINE_USER_ID")

    if not channel_token or not user_id:
        print("[警告] 未偵測到 LINE 環境變數，跳過推播發送。")
        return

    # 僅針對中、高風險標的進行推播警戒 (若全標的都要推可拿掉條件)
    alert_targets = [item for item in evaluated_list if item["risk_level"] in ["中風險", "高風險"]]

    if not alert_targets:
        print("[資訊] 所有標的皆在安全邊際內，無需觸發中高風險警戒。")
        return

    # LINE Carousel 上限為 12 張卡片
    bubbles = [build_stock_flex_bubble(item) for item in alert_targets[:10]]

    payload = {
        "to": user_id,
        "messages": [
            {
                "type": "flex",
                "altText": "【量化警示】持股安全邊際與風險警戒通知",
                "contents": {
                    "type": "carousel",
                    "contents": bubbles,
                },
            }
        ],
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {channel_token}",
    }

    res = requests.post("https://api.line.me/v2/bot/message/push", headers=headers, json=payload, timeout=15)

    if res.status_code == 200:
        print(f"[成功] LINE 警報已發送，共推播 {len(bubbles)} 檔中高風險標的。")
    else:
        print(f"[失敗] LINE 推播錯誤: {res.status_code} - {res.text}")


# ==========================================
# 5. 主執行入口
# ==========================================
def main():
    results = []

    for symbol, target in WATCHLIST.items():
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="5d")
            if hist.empty:
                print(f"[跳過] 無法取得 {symbol} 報價")
                continue

            current_price = hist["Close"].iloc[-1]
            eval_data = evaluate_stock_risk(
                symbol=symbol,
                current_price=current_price,
                fair_price=target["fair_price"],
                cost=target["cost"],
            )
            results.append(eval_data)
            print(f"[{eval_data['risk_level']}] {symbol}: 現價 {eval_data['current_price']}, 安全邊際 {eval_data['safety_margin']}%")

        except Exception as e:
            print(f"[錯誤] 處理 {symbol} 時發生異常: {e}")

    if results:
        send_line_alert(results)


if __name__ == "__main__":
    main()
