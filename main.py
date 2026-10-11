#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Quantitative Risk & Valuation Engine (Execution-Ready Edition)
- 決策矩陣化：趨勢優先於價值，消除訊號衝突
- 具體份額執行指引：提供明確階梯倉位比例與建議價位
- 系統靜默失敗主動報警 (Silent Failure Watchdog)
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
# market: "US" 或 "TW" (配合排程分流)
# fair_value: 基準合理價
# valuation_date: 估值設定日期 (超過 90 天未更新會提示估值過期)
WATCHLIST = {
    "NVDA": {
        "name": "NVIDIA",
        "market": "US",
        "fair_value": 130.0,
        "valuation_date": "2026-08-15",
        "cost": 115.0,
        "stop_loss_pct": -10.0,
        "trailing_stop_pct": -15.0,
    },
    "AMAT": {
        "name": "Applied Materials",
        "market": "US",
        "fair_value": 220.0,
        "valuation_date": "2026-08-15",
        "cost": 190.0,
        "stop_loss_pct": -10.0,
        "trailing_stop_pct": -15.0,
    },
    "0050.TW": {
        "name": "元大台灣50",
        "market": "TW",
        "fair_value": 185.0,
        "valuation_date": "2026-07-01",
        "cost": 165.0,
        "stop_loss_pct": -8.0,
        "trailing_stop_pct": -12.0,
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
# 3. 穩定行情報價模組 (含跳空偵測與退避重試)
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
# 4. 決策矩陣量化核心 (消除衝突與具體行動)
# ==========================================
def evaluate_quant_matrix(symbol: str, meta: dict, market_data: dict, prev_stock_state: dict) -> dict:
    price = market_data["current_price"]
    ma200 = market_data["ma200"]
    ma20 = market_data["ma20"]
    gap_pct = market_data["gap_pct"]

    fair = meta["fair_value"]
    cost = meta["cost"]
    stop_loss = meta.get("stop_loss_pct", -10.0)
    trailing_stop = meta.get("trailing_stop_pct", -15.0)

    # 1. 計算回撤與損益
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

    # 3. 決策矩陣 (Rule Priority: 風控 > 移動停利 > 昂貴減碼 > 買進濾網)
    gap_warning = f" (注意: 今日跳空 {gap_pct}%)" if abs(gap_pct) >= 3.0 else ""

    if pnl_rate <= stop_loss:
        risk_level = "停損警戒"
        color = "#991B1B"
        action = "市價全數平倉 / 嚴格停損"
        desc = f"成本損益達 {pnl_rate:.1f}%，跌破防線。不問理由執行紀律停損。{gap_warning}"
        should_alert = True

    elif drawdown_from_peak <= trailing_stop and pnl_rate > 8.0:
        risk_level = "移動停利"
        color = "#B91C1C"
        action = "出清剩餘部位 1/2 / 鎖住獲利"
        desc = f"自高點 ${peak_price:.2f} 回撤達 {drawdown_from_peak:.1f}%，啟動移動停利保全利潤。"
        should_alert = True

    elif premium_rate > 25.0:
        risk_level = "高風險 (嚴重昂貴)"
        color = "#DC2626"
        action = "執行第 2 階減碼：賣出持倉 1/3"
        desc = f"溢價率達 {premium_rate:.1f}%，估值嚴重透支，收回本金並提高現金水位。"
        should_alert = True

    elif 12.0 <= premium_rate <= 25.0:
        risk_level = "中風險 (估值警戒)"
        color = "#F59E0B"
        action = "執行第 1 階減碼：賣出持倉 1/4"
        desc = f"溢價率 {premium_rate:.1f}%，進入超買警戒區，禁止追價，維持部分調節。"
        should_alert = True

    elif safety_margin >= 15.0:
        # 價值 + 趨勢 矩陣判定
        if price < ma200:
            risk_level = "偏空觀察 (左側禁買)"
            color = "#D97706"
            action = "禁止接刀，等待站上 20MA 拐點"
            desc = f"安全邊際充足 ({safety_margin:.1f}%)，但受制於 200MA 空頭走勢。嚴防基本面惡化陷阱。"
            should_alert = False  # 觀察中不騷擾推播
        else:
            risk_level = "低風險 (買進區)"
            color = "#10B981"
            action = "啟動第 1 批建倉：買進部位 1/3"
            desc = f"安全邊際達 {safety_margin:.1f}% 且位於 200MA 上方，符合量化進場防禦門檻。"
            should_alert = True
    else:
        risk_level = "合理估值區"
        color = "#3B82F6"
        action = "定時定額 / 續抱不動"
        desc = "估值處於合理防禦帶內，波動屬於市場噪音，維持原配置步調。"
        should_alert = False

    # 4. 去重與狀態轉移比對
    last_risk = prev_stock_state.get("risk_level", "")
    is_state_changed = (last_risk != risk_level)
    must_push = should_alert and (is_state_changed or "停損" in risk_level)

    return {
        "symbol": symbol,
        "name": meta.get("name", symbol),
        "market": meta.get("market", "US"),
        "current_price": round(price, 2),
        "gap_pct": gap_pct,
        "ma200": round(ma200, 2),
        "ma20": round(ma20, 2),
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
# 5. LINE Flex 卡片與系統異常報警
# ==========================================
def build_stock_bubble(item: dict) -> dict:
    pnl_sign = "+" if item["pnl_rate"] > 0 else ""
    pnl_color = "#10B981" if item["pnl_rate"] >= 0 else "#EF4444"
    stale_text = " ⚠️估值超時" if item["is_valuation_stale"] else ""

    # 外部行情快速跳轉連結 (縮短執行路徑)
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
    """兜底警報：系統崩潰或網路阻擋時主動發出通知，拒絕靜默失效"""
    user_id = os.environ.get("LINE_USER_ID")
    if not user_id:
        return
    payload = {
        "to": user_id,
        "messages": [
            {
                "type": "text",
                "text": f"🚨【股票監控系統異常警報】\n執行失敗，排程已被阻擋或發生錯誤:\n{error_msg[:300]}",
            }
        ],
    }
    send_line_message(payload)


# ==========================================
# 6. HTML 儀表板
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
            raise RuntimeError("所有標的皆無法自 Yahoo Finance 獲取報價，可能遭到風控 IP 封鎖。")

        # 執行推播
        if items_to_push and user_id:
            bubbles = [build_stock_bubble(x) for x in items_to_push[:10]]
            payload = {
                "to": user_id,
                "messages": [
                    {
                        "type": "flex",
                        "altText": "【資產風控指令】具體操作建議與警戒通知",
                        "contents": {"type": "carousel", "contents": bubbles},
                    }
                ],
            }
            send_line_message(payload)

        # 儲存持久化資料與儀表板
        save_state(new_stocks_dict)
        generate_dashboard(eval_results)
        print("[完成] 量化監控與狀態保存完畢。")

    except Exception as e:
        err_stack = traceback.format_exc()
        print(f"[致命錯誤] {err_stack}")
        push_system_watchdog_error(str(e))
        raise e


if __name__ == "__main__":
    main()
