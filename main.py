#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Quantitative Risk & Valuation Engine (Execution-Ready Edition)
- 支援個股與大盤 ETF 差異化風控 (ETF 自動豁免硬停損，改採定期定額/逢低加碼)
- 具體倉位執行指引與決策矩陣 (趨勢優先於價值)
- 系統異常主動報警 (Silent Failure Watchdog)
- 狀態去重 (Deduplication) 與回撤保護 (Trailing Stop)
"""

import os
import json
import time
import datetime
import traceback
import requests
import yfinance as yf

# ==========================================
# 1. 監控標的與策略設定
# ==========================================
# is_etf: 若為 True，代表大盤市值型 ETF，不執行恐慌停損
# valuation_date: 估值更新日期 (若超過 90 天未更新會標註超時)
WATCHLIST = {
    "NVDA": {
        "name": "NVIDIA",
        "market": "US",
        "is_etf": False,
        "fair_value": 220.0,
        "valuation_date": "2026-10-12",
        "cost": 190.0,
        "stop_loss_pct": -10.0,       # 個股嚴格停損
        "trailing_stop_pct": -15.0,
    },
    "AMAT": {
        "name": "Applied Materials",
        "market": "US",
        "is_etf": False,
        "fair_value": 480.0,
        "valuation_date": "2026-10-12",
        "cost": 420.0,
        "stop_loss_pct": -10.0,       # 個股嚴格停損
        "trailing_stop_pct": -15.0,
    },
    "0050.TW": {
        "name": "元大台灣50",
        "market": "TW",
        "is_etf": True,               # 指數型 ETF：豁免硬停損
        "fair_value": 115.0,          # 對齊除息/分割後市價水準
        "valuation_date": "2026-10-12",
        "cost": 105.0,                # 定期定額持有均價
        "stop_loss_pct": None,        # 大盤 ETF 不設停損，嚴防恐慌賣出
        "trailing_stop_pct": -20.0,   # 移動停利門檻 (大盤放寬)
    },
}

STATE_FILE = "state.json"
DASHBOARD_FILE = "index.html"


# ==========================================
# 2. 狀態快取模組
# ==========================================
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[警告] 讀取狀態快取失敗: {e}")
    return {"updated_at": "", "stocks": {}}


def save_state(stocks_dict: dict):
    payload = {
        "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "stocks": stocks_dict,
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


# ==========================================
# 3. 行情報價擷取模組 (含指數退避重試)
# ==========================================
def fetch_stock_data(symbol: str, retries: int = 3, delay: int = 3):
    for attempt in range(1, retries + 1):
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="1y")
            if not hist.empty and len(hist) >= 5:
                current_price = float(hist["Close"].iloc[-1])
                prev_close = float(hist["Close"].iloc[-2])
                open_price = float(hist["Open"].iloc[-1])
                gap_pct = ((open_price - prev_close) / prev_close) * 100

                ma200 = float(hist["Close"].rolling(window=200).mean().iloc[-1]) if len(hist) >= 200 else float(hist["Close"].mean())
                ma20 = float(hist["Close"].rolling(window=20).mean().iloc[-1]) if len(hist) >= 20 else float(hist["Close"].mean())

                return {
                    "current_price": current_price,
                    "prev_close": prev_close,
                    "open_price": open_price,
                    "gap_pct": round(gap_pct, 2),
                    "ma200": round(ma200, 2),
                    "ma20": round(ma20, 2),
                }
        except Exception as e:
            print(f"[重試 {attempt}/{retries}] {symbol} 抓取失敗: {e}")
            time.sleep(delay * attempt)
    return None


# ==========================================
# 4. 決策矩陣與風控核心
# ==========================================
def evaluate_quant_matrix(symbol: str, meta: dict, market_data: dict, prev_stock_state: dict) -> dict:
    price = market_data["current_price"]
    ma200 = market_data["ma200"]
    gap_pct = market_data["gap_pct"]

    fair = meta["fair_value"]
    cost = meta["cost"]
    is_etf = meta.get("is_etf", False)
    stop_loss = meta.get("stop_loss_pct", None)
    trailing_stop = meta.get("trailing_stop_pct", -15.0)

    # 1. 回撤與損益計算
    cached_peak = prev_stock_state.get("peak_price", price)
    peak_price = max(cached_peak, price)
    drawdown_from_peak = ((price - peak_price) / peak_price) * 100

    safety_margin = ((fair - price) / fair) * 100
    premium_rate = ((price - fair) / fair) * 100
    pnl_rate = ((price - cost) / cost) * 100

    # 2. 檢測估值是否過期 (> 90 天)
    val_date_str = meta.get("valuation_date", "")
    is_valuation_stale = False
    if val_date_str:
        val_date = datetime.datetime.strptime(val_date_str, "%Y-%m-%d").date()
        if (datetime.date.today() - val_date).days > 90:
            is_valuation_stale = True

    gap_warning = f" (注意: 今日跳空 {gap_pct}%)" if abs(gap_pct) >= 3.0 else ""

    # 3. 決策矩陣判斷
    # 規則 A: 個股硬停損 (ETF 豁免)
    if not is_etf and stop_loss is not None and pnl_rate <= stop_loss:
        risk_level = "停損警戒"
        color = "#991B1B"
        action = "市價全數平倉 / 嚴格停損"
        desc = f"成本損益達 {pnl_rate:.1f}%，跌破防線。個股基本面疑慮，不問理由執行紀律停損。{gap_warning}"
        should_alert = True

    # 規則 B: 移動停利 (獲利回吐防禦)
    elif drawdown_from_peak <= trailing_stop and pnl_rate > 8.0:
        risk_level = "移動停利"
        color = "#B91C1C"
        action = "出清持股 1/2 / 鎖定波段獲利"
        desc = f"自高點 ${peak_price:.2f} 回撤達 {drawdown_from_peak:.1f}%，啟動移動停利保全利潤。"
        should_alert = True

    # 規則 C: 嚴重溢價 (高風險昂貴區)
    elif premium_rate > 25.0:
        risk_level = "高風險 (嚴重昂貴)"
        color = "#DC2626"
        action = "執行第 2 階減碼：賣出持倉 1/3"
        desc = f"溢價率達 {premium_rate:.1f}%，估值嚴重透支，收回本金並提高現金水位。"
        should_alert = True

    # 規則 D: 估值警戒 (中風險)
    elif 12.0 <= premium_rate <= 25.0:
        risk_level = "中風險 (估值警戒)"
        color = "#F59E0B"
        action = "執行第 1 階減碼：賣出持倉 1/4"
        desc = f"溢價率 {premium_rate:.1f}%，進入超買警戒區，禁止追價，維持部分調節。"
        should_alert = True

    # 規則 E: 具安全邊際 (買進 / 加碼區)
    elif safety_margin >= 10.0:
        if is_etf:
            risk_level = "低風險 (ETF加碼區)"
            color = "#10B981"
            action = "大盤折價，執行定期定額或加碼"
            desc = f"安全邊際達 {safety_margin:.1f}%，大盤估值合理偏低，適合長期累積單位數。"
            should_alert = True
        else:
            if price < ma200:
                risk_level = "偏空觀察 (左側禁買)"
                color = "#D97706"
                action = "禁止接刀，等待站上均線右側訊號"
                desc = f"安全邊際充足 ({safety_margin:.1f}%)，但受制於 200MA 空頭走勢，嚴防價值陷阱。"
                should_alert = False
            else:
                risk_level = "低風險 (買進區)"
                color = "#10B981"
                action = "啟動第 1 批建倉：買進部位 1/3"
                desc = f"安全邊際達 {safety_margin:.1f}% 且位於 200MA 上方，符合量化進場防禦門檻。"
                should_alert = True

    # 規則 F: 合理估值區
    else:
        risk_level = "合理估值區"
        color = "#3B82F6"
        action = "定期定額 / 續抱不動"
        desc = "估值處於合理防禦帶內，波動屬於市場正常雜訊，維持既定步調。"
        should_alert = False

    # 4. 去重比對
    last_risk = prev_stock_state.get("risk_level", "")
    is_state_changed = (last_risk != risk_level)
    must_push = should_alert and (is_state_changed or "停損" in risk_level)

    return {
        "symbol": symbol,
        "name": meta.get("name", symbol),
        "market": meta.get("market", "US"),
        "is_etf": is_etf,
        "current_price": round(price, 2),
        "gap_pct": gap_pct,
        "ma200": round(ma200, 2),
        "peak_price": round(peak_price, 2),
        "drawdown_from_peak": round(drawdown_from_peak, 2),
        "fair_value": round(fair, 2),
        "cost": round(cost, 2),
        "safety_margin": round(safety_margin, 2),
        "premium_rate": round(premium_rate, 2),
        "pnl_rate": round(pnl_rate, 2),
        "risk_level": risk_level,
        "action": action,
        "color": color,
        "desc": desc,
        "must_push": must_push,
        "is_valuation_stale": is_valuation_stale,
    }


# ==========================================
# 5. LINE 推播與異常告警模組
# ==========================================
def build_stock_bubble(item: dict) -> dict:
    pnl_sign = "+" if item["pnl_rate"] > 0 else ""
    pnl_color = "#10B981" if item["pnl_rate"] >= 0 else "#EF4444"
    stale_text = " ⚠️估值超時" if item["is_valuation_stale"] else ""
    external_url = f"https://finance.yahoo.com/quote/{item['symbol']}"

    return {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": item["color"],
            "paddingAll": "15px",
            "contents": [
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [
                        {
                            "type": "text",
                            "text": f"{item['symbol']} {item['name']}",
                            "weight": "bold",
                            "color": "#FFFFFF",
                            "size": "lg",
                            "flex": 4,
                        },
                        {
                            "type": "text",
                            "text": f"{item['risk_level'].split()[0]}{stale_text}",
                            "weight": "bold",
                            "color": "#FFFFFF",
                            "size": "xs",
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
                        {"type": "text", "text": f"${item['current_price']}", "weight": "bold", "size": "xl", "align": "end"},
                    ],
                },
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "持股損益 / 回撤", "color": "#6B7280", "size": "sm"},
                        {"type": "text", "text": f"{pnl_sign}{item['pnl_rate']}% / {item['drawdown_from_peak']}%", "weight": "bold", "size": "sm", "align": "end", "color": pnl_color},
                    ],
                },
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "邊際 / 200MA", "color": "#6B7280", "size": "sm"},
                        {"type": "text", "text": f"{item['safety_margin']}% / ${item['ma200']}", "size": "sm", "align": "end", "color": "#374151"},
                    ],
                },
                {"type": "separator", "margin": "sm"},
                {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {"type": "text", "text": f"【指令】{item['action']}", "weight": "bold", "size": "sm", "color": item["color"]},
                        {"type": "text", "text": item["desc"], "size": "xs", "color": "#4B5563", "wrap": True, "margin": "xs"},
                    ],
                },
            ],
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "button",
                    "style": "link",
                    "height": "sm",
                    "action": {"type": "uri", "label": "開啟完整即時線圖", "uri": external_url},
                }
            ],
        },
    }


def send_line_message(payload: dict):
    channel_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    if not channel_token:
        print("[資訊] 未配置 LINE Token，略過推播。")
        return

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {channel_token}",
    }
    try:
        res = requests.post("https://api.line.me/v2/bot/message/push", headers=headers, json=payload, timeout=15)
        if res.status_code != 200:
            print(f"[失敗] LINE 推播錯誤 ({res.status_code}): {res.text}")
    except Exception as e:
        print(f"[異常] LINE 推播失敗: {e}")


def push_system_watchdog_error(error_msg: str):
    user_id = os.environ.get("LINE_USER_ID")
    if not user_id:
        return
    payload = {
        "to": user_id,
        "messages": [
            {
                "type": "text",
                "text": f"🚨【股票監控系統異常警報】\n排程執行發生錯誤:\n{error_msg[:300]}",
            }
        ],
    }
    send_line_message(payload)


# ==========================================
# 6. HTML 儀表板生成器
# ==========================================
def generate_dashboard(eval_results: list):
    cards_html = ""
    for r in eval_results:
        pnl_class = "text-emerald-600" if r["pnl_rate"] >= 0 else "text-rose-600"
        pnl_sign = "+" if r["pnl_rate"] > 0 else ""
        stale_badge = '<span class="bg-red-500 text-white text-[10px] px-1.5 py-0.5 rounded ml-1">估值過期</span>' if r["is_valuation_stale"] else ''

        cards_html += f"""
        <div class="bg-white rounded-xl shadow-sm border border-slate-200 overflow-hidden">
            <div class="p-3 text-white flex justify-between items-center" style="background-color: {r['color']};">
                <span class="font-bold">{r['symbol']} {r['name']} {stale_badge}</span>
                <span class="text-xs bg-black/20 px-2 py-0.5 rounded font-mono">{r['risk_level']}</span>
            </div>
            <div class="p-4 space-y-2 text-sm">
                <div class="flex justify-between">
                    <span class="text-slate-500">現價 / 跳空</span>
                    <span class="font-bold">${r['current_price']} <span class="text-xs text-slate-400">({r['gap_pct']}%)</span></span>
                </div>
                <div class="flex justify-between">
                    <span class="text-slate-500">損益 / 高點回撤</span>
                    <span class="font-semibold {pnl_class}">{pnl_sign}{r['pnl_rate']}% / {r['drawdown_from_peak']}%</span>
                </div>
                <div class="flex justify-between">
                    <span class="text-slate-500">安全邊際 / 200MA</span>
                    <span class="font-semibold">${r['safety_margin']}% / ${r['ma200']}</span>
                </div>
                <div class="pt-2 border-t border-slate-100">
                    <div class="font-bold text-xs" style="color: {r['color']}">{r['action']}</div>
                    <div class="text-xs text-slate-500 mt-0.5">{r['desc']}</div>
                </div>
            </div>
        </div>
        """

    html = f"""<!DOCTYPE html>
<html lang="zh-TW"><head><meta charset="UTF-8"><title>Quant Portfolio</title>
<script src="https://cdn.tailwindcss.com"></script></head>
<body class="bg-slate-50 p-6 max-w-5xl mx-auto font-sans">
    <div class="flex justify-between items-center mb-6">
        <h1 class="text-xl font-bold">資產決策與風控儀表板</h1>
        <span class="text-xs text-slate-400">更新: {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</span>
    </div>
    <div class="grid grid-cols-1 md:grid-cols-3 gap-4">{cards_html}</div>
</body></html>"""

    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(html)


# ==========================================
# 7. 主程式入口
# ==========================================
def main():
    user_id = os.environ.get("LINE_USER_ID")
    prev_state = load_state()
    prev_stocks = prev_state.get("stocks", {})

    eval_results = []
    new_stocks_dict = {}
    items_to_push = []

    try:
        for symbol, meta in WATCHLIST.items():
            market_data = fetch_stock_data(symbol)
            if not market_data:
                print(f"[警告] 標的 {symbol} 抓取超時或失敗")
                continue

            stock_prev = prev_stocks.get(symbol, {})
            eval_data = evaluate_quant_matrix(symbol, meta, market_data, stock_prev)

            eval_results.append(eval_data)
            new_stocks_dict[symbol] = {
                "risk_level": eval_data["risk_level"],
                "current_price": eval_data["current_price"],
                "peak_price": eval_data["peak_price"],
                "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

            if eval_data["must_push"]:
                items_to_push.append(eval_data)

        if not eval_results:
            raise RuntimeError("無法自 Yahoo Finance 獲取任何報價，可能遭到 IP 風控。")

     # 1. 發送 LINE 卡片
        if items_to_push and user_id:
            send_line_message(...)

        # 2. 寫入狀態快取 (供前端儀表板讀取)
        save_state(new_stocks_dict)

        # 3. 移除或註解掉這行，保護你的前端 index.html 不再被覆蓋：
        # generate_dashboard(eval_results)
