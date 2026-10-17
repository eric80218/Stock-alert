#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Quantitative Risk & Valuation Engine (Pro Dashboard Rebuilt Edition)
- 清洗 yfinance NaN 空值，徹底修復 0050.TW 顯示 $nan 問題
- 整合 200MA、20MA、RSI(14)、波段高點回撤 (Trailing Stop) 與安全邊際階梯
- 個股與大盤 ETF 差異化風控 (ETF 自動豁免硬停損)
- 自動生成深色互動式量化儀表板 (內建 Chart.js 歷史走勢圖、安全邊際量尺、市場過濾器)
- LINE Flex Message 推播與異常看門狗通知
"""

import os
import json
import math
import time
import datetime
import traceback
import requests
import pandas as pd
import yfinance as yf

# ==========================================
# 1. 監控標的與量化策略參數
# ==========================================
WATCHLIST = {
    "NVDA": {
        "name": "NVIDIA",
        "market": "US",
        "currency": "$",
        "is_etf": False,
        "fair_value": 220.0,
        "valuation_date": "2026-10-18",
        "cost": 190.0,
        "stop_loss_pct": -10.0,
        "trailing_stop_pct": -15.0,
    },
    "AMAT": {
        "name": "Applied Materials",
        "market": "US",
        "currency": "$",
        "is_etf": False,
        "fair_value": 480.0,
        "valuation_date": "2026-10-18",
        "cost": 420.0,
        "stop_loss_pct": -10.0,
        "trailing_stop_pct": -15.0,
    },
    "0050.TW": {
        "name": "元大台灣50",
        "market": "TW",
        "currency": "NT$",
        "is_etf": True,
        "fair_value": 115.0,
        "valuation_date": "2026-10-18",
        "cost": 105.0,
        "stop_loss_pct": None,
        "trailing_stop_pct": -20.0,
    },
}

STATE_FILE = "state.json"
DASHBOARD_FILE = "index.html"
TW_TZ = datetime.timezone(datetime.timedelta(hours=8))


def now_tw_str() -> str:
    return datetime.datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S")


# ==========================================
# 2. 狀態快取讀寫模組
# ==========================================
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[警告] 讀取狀態快取失敗: {e}")
    return {"updated_at": "", "stocks": {}, "results": []}


def save_state(stocks_dict: dict, eval_results: list):
    payload = {
        "updated_at": now_tw_str(),
        "stocks": stocks_dict,
        "results": eval_results,
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"[完成] 狀態數據已寫入 {STATE_FILE}")


# ==========================================
# 3. 穩定報價與技術指標計算 (修復 NaN 問題)
# ==========================================
def calculate_rsi(series: pd.Series, period: int = 14) -> float:
    delta = series.diff()
    gain = (delta.where(delta > 0, 0.0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    val = float(rsi.iloc[-1])
    return round(val, 1) if not math.isnan(val) else 50.0


def fetch_stock_data(symbol: str, retries: int = 3, delay: int = 3):
    for attempt in range(1, retries + 1):
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="1y")
            if hist is None or hist.empty:
                raise ValueError("回傳歷史資料為空")

            # 關鍵修復：清除含有 NaN 的未結算列，防止 0050.TW 出現 $nan
            hist = hist.dropna(subset=["Close", "Open"])
            if len(hist) < 5:
                raise ValueError("有效交易日數不足 5 天")

            close_s = hist["Close"]
            current_price = float(close_s.iloc[-1])
            prev_close = float(close_s.iloc[-2])
            open_price = float(hist["Open"].iloc[-1])

            gap_pct = ((open_price - prev_close) / prev_close) * 100
            day_change_pct = ((current_price - prev_close) / prev_close) * 100

            ma200_val = close_s.rolling(window=200).mean().iloc[-1] if len(close_s) >= 200 else close_s.mean()
            ma20_val = close_s.rolling(window=20).mean().iloc[-1] if len(close_s) >= 20 else close_s.mean()
            rsi14 = calculate_rsi(close_s, 14)

            # 擷取近 30 日收盤價與日期供前端繪製互動走勢圖
            recent_30 = hist.tail(30)
            history_dates = [d.strftime("%m/%d") for d in recent_30.index]
            history_prices = [round(float(p), 2) for p in recent_30["Close"].tolist()]

            return {
                "current_price": round(current_price, 2),
                "prev_close": round(prev_close, 2),
                "open_price": round(open_price, 2),
                "gap_pct": round(gap_pct, 2),
                "day_change_pct": round(day_change_pct, 2),
                "ma200": round(float(ma200_val), 2),
                "ma20": round(float(ma20_val), 2),
                "rsi14": rsi14,
                "history_dates": history_dates,
                "history_prices": history_prices,
            }
        except Exception as e:
            print(f"[重試 {attempt}/{retries}] {symbol} 抓取異常: {e}")
            time.sleep(delay * attempt)
    return None


# ==========================================
# 4. 決策矩陣與量化風險評估引擎
# ==========================================
def evaluate_quant_matrix(
    symbol: str, meta: dict, market_data: dict, prev_stock_state: dict
) -> dict:
    price = market_data["current_price"]
    ma200 = market_data["ma200"]
    ma20 = market_data["ma20"]
    rsi14 = market_data["rsi14"]
    gap_pct = market_data["gap_pct"]
    day_change_pct = market_data["day_change_pct"]

    fair = float(meta["fair_value"])
    cost = float(meta["cost"])
    is_etf = meta.get("is_etf", False)
    stop_loss = meta.get("stop_loss_pct", None)
    trailing_stop = meta.get("trailing_stop_pct", -15.0)

    # 歷史波段最高價追蹤 (排除過去誤存的 NaN)
    raw_peak = prev_stock_state.get("peak_price", price)
    cached_peak = price if (raw_peak is None or math.isnan(float(raw_peak))) else float(raw_peak)
    peak_price = max(cached_peak, price)
    drawdown_from_peak = ((price - peak_price) / peak_price) * 100

    safety_margin = ((fair - price) / fair) * 100
    premium_rate = ((price - fair) / fair) * 100
    pnl_rate = ((price - cost) / cost) * 100

    val_date_str = meta.get("valuation_date", "")
    is_valuation_stale = False
    if val_date_str:
        val_date = datetime.datetime.strptime(val_date_str, "%Y-%m-%d").date()
        if (datetime.date.today() - val_date).days > 90:
            is_valuation_stale = True

    gap_warning = f" (注意: 今日跳空 {gap_pct}%)" if abs(gap_pct) >= 3.0 else ""

    # 決策優先級：停損 > 移動停利 > 高風險減碼 > 中風險調節 > 安全邊際買進 > 合理續抱
    if not is_etf and stop_loss is not None and pnl_rate <= stop_loss:
        risk_level = "停損警戒"
        risk_tag = "HIGH"
        color = "#EF4444"
        action = "市價全數平倉 / 嚴格停損"
        desc = f"成本損益達 {pnl_rate:.1f}%，跌破硬停損防線。嚴防基本面惡化，紀律執行平倉。{gap_warning}"
        should_alert = True

    elif drawdown_from_peak <= trailing_stop and pnl_rate > 8.0:
        risk_level = "移動停利"
        risk_tag = "HIGH"
        color = "#DC2626"
        action = "出清持股 1/2 / 鎖定波段獲利"
        desc = f"自波段高點 {meta['currency']}{peak_price:.2f} 回撤達 {drawdown_from_peak:.1f}%，啟動移動停利保護。"
        should_alert = True

    elif premium_rate > 25.0:
        risk_level = "高風險 (嚴重昂貴)"
        risk_tag = "HIGH"
        color = "#EF4444"
        action = "執行第 2 階減碼：賣出持倉 1/3"
        desc = f"溢價率達 {premium_rate:.1f}% (RSI {rsi14})，估值顯著透支未來預期，分批收回現金。"
        should_alert = True

    elif 12.0 <= premium_rate <= 25.0:
        risk_level = "中風險 (估值警戒)"
        risk_tag = "MEDIUM"
        color = "#F59E0B"
        action = "執行第 1 階減碼：賣出持倉 1/4"
        desc = f"溢價率 {premium_rate:.1f}%，安全邊際轉負，進入超買警戒區，禁止追高並適度調節。"
        should_alert = True

    elif safety_margin >= 10.0:
        if is_etf:
            risk_level = "低風險 (ETF加碼區)"
            risk_tag = "LOW"
            color = "#10B981"
            action = "大盤折價，執行定期定額或逢低加碼"
            desc = f"安全邊際達 {safety_margin:.1f}%，指數估值具吸引力，適合長期累積部位。"
            should_alert = True
        else:
            if price < ma200:
                risk_level = "偏空觀察 (左側禁買)"
                risk_tag = "MEDIUM"
                color = "#F97316"
                action = "禁止接刀，等待站上 20MA 右側訊號"
                desc = f"安全邊際充足 ({safety_margin:.1f}%)，但股價位於 200MA ({meta['currency']}{ma200}) 之下，嚴防價值陷阱。"
                should_alert = False
            else:
                risk_level = "低風險 (買進區)"
                risk_tag = "LOW"
                color = "#10B981"
                action = "啟動第 1 批建倉：買進目標部位 1/3"
                desc = f"安全邊際達 {safety_margin:.1f}% 且守穩 200MA 多頭趨勢之上，符合雙重防禦進場條件。"
                should_alert = True

    else:
        risk_level = "合理估值區"
        risk_tag = "NORMAL"
        color = "#3B82F6"
        action = "定期定額 / 維持續抱"
        desc = "股價處於合理估值防禦帶內，短期波動屬正常市場雜訊，維持既定配置。"
        should_alert = False

    last_risk = prev_stock_state.get("risk_level", "")
    is_state_changed = last_risk != risk_level
    must_push = should_alert and (is_state_changed or "停損" in risk_level)

    return {
        "symbol": symbol,
        "name": meta.get("name", symbol),
        "market": meta.get("market", "US"),
        "currency": meta.get("currency", "$"),
        "is_etf": is_etf,
        "current_price": price,
        "day_change_pct": day_change_pct,
        "gap_pct": gap_pct,
        "ma200": ma200,
        "ma20": ma20,
        "rsi14": rsi14,
        "peak_price": round(peak_price, 2),
        "drawdown_from_peak": round(drawdown_from_peak, 2),
        "fair_value": round(fair, 2),
        "cost": round(cost, 2),
        "safety_margin": round(safety_margin, 2),
        "premium_rate": round(premium_rate, 2),
        "pnl_rate": round(pnl_rate, 2),
        "risk_level": risk_level,
        "risk_tag": risk_tag,
        "action": action,
        "color": color,
        "desc": desc,
        "must_push": must_push,
        "is_valuation_stale": is_valuation_stale,
        "history_dates": market_data["history_dates"],
        "history_prices": market_data["history_prices"],
    }


# ==========================================
# 5. LINE Flex Message 推播模組
# ==========================================
def build_stock_bubble(item: dict) -> dict:
    pnl_sign = "+" if item["pnl_rate"] > 0 else ""
    pnl_color = "#10B981" if item["pnl_rate"] >= 0 else "#EF4444"
    cur = item["currency"]
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
                            "text": item["risk_level"].split()[0],
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
                        {
                            "type": "text",
                            "text": f"{cur}{item['current_price']}",
                            "weight": "bold",
                            "size": "xl",
                            "align": "end",
                        },
                    ],
                },
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "持股損益 / 回撤", "color": "#6B7280", "size": "sm"},
                        {
                            "type": "text",
                            "text": f"{pnl_sign}{item['pnl_rate']}% / {item['drawdown_from_peak']}%",
                            "weight": "bold",
                            "size": "sm",
                            "align": "end",
                            "color": pnl_color,
                        },
                    ],
                },
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "安全邊際 / 200MA", "color": "#6B7280", "size": "sm"},
                        {
                            "type": "text",
                            "text": f"{item['safety_margin']}% / {cur}{item['ma200']}",
                            "size": "sm",
                            "align": "end",
                            "color": "#374151",
                        },
                    ],
                },
                {"type": "separator", "margin": "sm"},
                {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {
                            "type": "text",
                            "text": f"【指令】{item['action']}",
                            "weight": "bold",
                            "size": "sm",
                            "color": item["color"],
                        },
                        {
                            "type": "text",
                            "text": item["desc"],
                            "size": "xs",
                            "color": "#4B5563",
                            "wrap": True,
                            "margin": "xs",
                        },
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
                    "action": {
                        "type": "uri",
                        "label": "開啟 Yahoo Finance 線圖",
                        "uri": external_url,
                    },
                }
            ],
        },
    }


def send_line_message(payload: dict):
    channel_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    if not channel_token:
        print("[資訊] 未設定 LINE_CHANNEL_ACCESS_TOKEN，略過推播。")
        return

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {channel_token}",
    }
    try:
        res = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers=headers,
            json=payload,
            timeout=15,
        )
        if res.status_code == 200:
            print("[成功] LINE 訊息已推送。")
        else:
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
# 6. 旗艦版深色互動式量化儀表板產生器 (index.html)
# ==========================================
def generate_pro_dashboard(eval_results: list):
    updated_time = now_tw_str()
    total_count = len(eval_results)
    high_risk_count = sum(1 for r in eval_results if r["risk_tag"] in ["HIGH", "MEDIUM"])
    buy_zone_count = sum(1 for r in eval_results if r["risk_tag"] == "LOW")
    avg_pnl = round(sum(r["pnl_rate"] for r in eval_results) / total_count, 2) if total_count else 0.0
    avg_pnl_sign = "+" if avg_pnl > 0 else ""
    avg_pnl_color = "text-emerald-400" if avg_pnl >= 0 else "text-rose-400"

    cards_html = ""
    for idx, r in enumerate(eval_results):
        cur = r["currency"]
        pnl_sign = "+" if r["pnl_rate"] > 0 else ""
        day_sign = "+" if r["day_change_pct"] > 0 else ""
        pnl_cls = "text-emerald-400" if r["pnl_rate"] >= 0 else "text-rose-400"
        day_cls = "text-emerald-400" if r["day_change_pct"] >= 0 else "text-rose-400"

        # 計算安全邊際視覺化進度條位置 (將 -40% ~ +40% 映射到 5% ~ 95%)
        clamped_margin = max(-40.0, min(40.0, r["safety_margin"]))
        gauge_pct = round(50.0 + (clamped_margin / 40.0) * 45.0, 1)

        cards_html += f"""
        <div class="stock-card bg-slate-900/90 border border-slate-800 rounded-2xl shadow-xl overflow-hidden flex flex-col justify-between transition-all duration-200 hover:border-slate-700"
             data-market="{r['market']}" data-risk="{r['risk_tag']}">
            <div>
                <!-- 頂部狀態橫幅 -->
                <div class="px-5 py-3.5 flex justify-between items-center border-b border-slate-800/80" style="background: linear-gradient(90deg, {r['color']}22 0%, transparent 100%); border-left: 4px solid {r['color']};">
                    <div>
                        <div class="flex items-center gap-2">
                            <span class="text-lg font-black tracking-wide text-white">{r['symbol']}</span>
                            <span class="text-xs px-2 py-0.5 rounded bg-slate-800 text-slate-300 font-medium">{r['market']}</span>
                        </div>
                        <p class="text-xs text-slate-400 mt-0.5">{r['name']}</p>
                    </div>
                    <span class="px-3 py-1 rounded-full text-xs font-bold text-white shadow-sm" style="background-color: {r['color']};">
                        {r['risk_level']}
                    </span>
                </div>

                <!-- 核心報價與損益 -->
                <div class="p-5">
                    <div class="flex justify-between items-end mb-4">
                        <div>
                            <span class="text-xs text-slate-400 block mb-1">當前市價 (日漲跌)</span>
                            <div class="flex items-baseline gap-2">
                                <span class="text-3xl font-black text-white">{cur}{r['current_price']}</span>
                                <span class="text-sm font-bold {day_cls}">{day_sign}{r['day_change_pct']}%</span>
                            </div>
                        </div>
                        <div class="text-right">
                            <span class="text-xs text-slate-400 block mb-1">持倉損益率</span>
                            <span class="text-2xl font-black {pnl_cls}">{pnl_sign}{r['pnl_rate']}%</span>
                        </div>
                    </div>

                    <!-- 安全邊際視覺化量尺 -->
                    <div class="mb-4 bg-slate-950/60 p-3 rounded-xl border border-slate-800/60">
                        <div class="flex justify-between text-xs mb-1.5">
                            <span class="text-slate-400">安全邊際量尺 (合理價 <strong class="text-slate-200">{cur}{r['fair_value']}</strong>)</span>
                            <span class="font-bold" style="color: {r['color']}">邊際 {r['safety_margin']}% / 溢價 {r['premium_rate']}%</span>
                        </div>
                        <div class="w-full h-2 bg-slate-800 rounded-full relative overflow-hidden">
                            <div class="absolute inset-y-0 left-0 bg-gradient-to-r from-rose-500 via-blue-500 to-emerald-500 w-full opacity-30"></div>
                            <div class="h-full rounded-full transition-all duration-500" style="width: {gauge_pct}%; background-color: {r['color']};"></div>
                        </div>
                        <div class="flex justify-between text-[10px] text-slate-500 mt-1">
                            <span>昂貴溢價區 (-40%)</span>
                            <span>合理價 (0%)</span>
                            <span>安全買進區 (+40%)</span>
                        </div>
                    </div>

                    <!-- 近 30 日互動走勢圖 -->
                    <div class="h-28 w-full mb-4 bg-slate-950/40 rounded-xl p-2 border border-slate-800/40">
                        <canvas id="chart-{idx}"></canvas>
                    </div>

                    <!-- 六宮格量化技術指標 -->
                    <div class="grid grid-cols-3 gap-2 text-xs bg-slate-950/50 p-3 rounded-xl border border-slate-800/50 mb-4">
                        <div>
                            <span class="text-slate-500 block">持有成本</span>
                            <span class="font-bold text-slate-200">{cur}{r['cost']}</span>
                        </div>
                        <div>
                            <span class="text-slate-500 block">200MA 均線</span>
                            <span class="font-bold text-slate-200">{cur}{r['ma200']}</span>
                        </div>
                        <div>
                            <span class="text-slate-500 block">20MA 均線</span>
                            <span class="font-bold text-slate-200">{cur}{r['ma20']}</span>
                        </div>
                        <div>
                            <span class="text-slate-500 block">波段最高價</span>
                            <span class="font-bold text-slate-200">{cur}{r['peak_price']}</span>
                        </div>
                        <div>
                            <span class="text-slate-500 block">高點回撤</span>
                            <span class="font-bold text-rose-400">{r['drawdown_from_peak']}%</span>
                        </div>
                        <div>
                            <span class="text-slate-500 block">RSI (14) 強弱</span>
                            <span class="font-bold text-amber-400">{r['rsi14']}</span>
                        </div>
                    </div>

                    <!-- 決策行動指令盒 -->
                    <div class="p-3.5 rounded-xl border" style="background-color: {r['color']}15; border-color: {r['color']}40;">
                        <div class="text-xs font-extrabold flex items-center gap-1.5" style="color: {r['color']};">
                            <span>⚡ 量化指令：{r['action']}</span>
                        </div>
                        <p class="text-xs text-slate-300 mt-1 leading-relaxed">{r['desc']}</p>
                    </div>
                </div>
            </div>

            <!-- 底部快捷操作列 -->
            <div class="px-5 py-3 bg-slate-950/80 border-t border-slate-800/80 flex justify-between items-center text-xs">
                <a href="https://finance.yahoo.com/quote/{r['symbol']}" target="_blank" class="text-blue-400 hover:text-blue-300 font-semibold">Yahoo 報價 ↗</a>
                <a href="https://www.tradingview.com/symbols/{r['symbol'].replace('.TW', '')}/" target="_blank" class="text-slate-400 hover:text-slate-200">TradingView 線圖 ↗</a>
            </div>
        </div>
        """

    # 準備給前端 Chart.js 的 JSON 陣列
    charts_json = json.dumps(
        [
            {
                "id": f"chart-{idx}",
                "symbol": r["symbol"],
                "dates": r["history_dates"],
                "prices": r["history_prices"],
                "fair": r["fair_value"],
                "color": r["color"],
            }
            for idx, r in enumerate(eval_results)
        ],
        ensure_ascii=False,
    )

    html = f"""<!DOCTYPE html>
<html lang="zh-TW" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Quantitative Portfolio & Risk Engine</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen font-sans selection:bg-blue-500 selection:text-white">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 py-8">
        <!-- 頂部標題與系統狀態 -->
        <header class="flex flex-col md:flex-row md:items-center md:justify-between gap-4 pb-6 border-b border-slate-800">
            <div>
                <div class="flex items-center gap-2.5">
                    <span class="h-3 w-3 rounded-full bg-emerald-500 animate-pulse"></span>
                    <h1 class="text-2xl sm:text-3xl font-black tracking-tight text-white">量化估值與動態風控決策系統</h1>
                </div>
                <p class="text-xs sm:text-sm text-slate-400 mt-1">
                    整合安全邊際階梯、200MA 趨勢濾網與高點移動停利機制 ｜ 最後更新 (台灣時間): <span class="text-slate-200 font-mono">{updated_time}</span>
                </p>
            </div>
            <!-- 市場與風險篩選按鈕 -->
            <div class="flex flex-wrap gap-2">
                <button onclick="filterCards('ALL')" class="filter-btn active px-3.5 py-1.5 rounded-lg text-xs font-bold bg-blue-600 text-white transition">全部標的 ({total_count})</button>
                <button onclick="filterCards('US')" class="filter-btn px-3.5 py-1.5 rounded-lg text-xs font-bold bg-slate-800 text-slate-300 hover:bg-slate-700 transition">美股 US</button>
                <button onclick="filterCards('TW')" class="filter-btn px-3.5 py-1.5 rounded-lg text-xs font-bold bg-slate-800 text-slate-300 hover:bg-slate-700 transition">台股 TW</button>
                <button onclick="filterCards('ALERT')" class="filter-btn px-3.5 py-1.5 rounded-lg text-xs font-bold bg-rose-950 text-rose-300 border border-rose-800/60 hover:bg-rose-900 transition">中高風險警戒 ({high_risk_count})</button>
            </div>
        </header>

        <!-- 投資組合總覽 KPI 列 -->
        <section class="grid grid-cols-2 md:grid-cols-4 gap-4 my-6">
            <div class="bg-slate-900/80 border border-slate-800 p-4 rounded-2xl">
                <span class="text-xs text-slate-400">監控核心標的</span>
                <div class="text-2xl font-black text-white mt-1">{total_count} <span class="text-xs font-normal text-slate-400">檔</span></div>
            </div>
            <div class="bg-slate-900/80 border border-slate-800 p-4 rounded-2xl">
                <span class="text-xs text-slate-400">組合平均損益率</span>
                <div class="text-2xl font-black {avg_pnl_color} mt-1">{avg_pnl_sign}{avg_pnl}%</div>
            </div>
            <div class="bg-slate-900/80 border border-slate-800 p-4 rounded-2xl">
                <span class="text-xs text-slate-400">中高風險 / 減碼警戒</span>
                <div class="text-2xl font-black text-amber-400 mt-1">{high_risk_count} <span class="text-xs font-normal text-slate-400">檔</span></div>
            </div>
            <div class="bg-slate-900/80 border border-slate-800 p-4 rounded-2xl">
                <span class="text-xs text-slate-400">低風險 / 安全買進區</span>
                <div class="text-2xl font-black text-emerald-400 mt-1">{buy_zone_count} <span class="text-xs font-normal text-slate-400">檔</span></div>
            </div>
        </section>

        <!-- 個股量化卡片網格 -->
        <main class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
            {cards_html}
        </main>
    </div>

    <script>
        const chartConfigs = {charts_json};

        window.addEventListener('DOMContentLoaded', () => {{
            chartConfigs.forEach(cfg => {{
                const ctx = document.getElementById(cfg.id);
                if (!ctx) return;
                new Chart(ctx, {{
                    type: 'line',
                    data: {{
                        labels: cfg.dates,
                        datasets: [
                            {{
                                label: '收盤價',
                                data: cfg.prices,
                                borderColor: cfg.color,
                                backgroundColor: cfg.color + '20',
                                borderWidth: 2,
                                pointRadius: 0,
                                fill: true,
                                tension: 0.3
                            }},
                            {{
                                label: '合理基準價',
                                data: Array(cfg.dates.length).fill(cfg.fair),
                                borderColor: '#94A3B8',
                                borderDash: [4, 4],
                                borderWidth: 1,
                                pointRadius: 0,
                                fill: false
                            }}
                        ]
                    }},
                    options: {{
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: {{ legend: {{ display: false }} }},
                        scales: {{
                            x: {{ display: false }},
                            y: {{
                                ticks: {{ color: '#64748B', font: {{ size: 10 }} }},
                                grid: {{ color: '#1E293B' }}
                            }}
                        }}
                    }}
                }});
            }});
        }});

        function filterCards(mode) {{
            const cards = document.querySelectorAll('.stock-card');
            cards.forEach(card => {{
                const market = card.getAttribute('data-market');
                const risk = card.getAttribute('data-risk');
                if (mode === 'ALL') {{
                    card.style.display = 'flex';
                }} else if (mode === 'US' || mode === 'TW') {{
                    card.style.display = (market === mode) ? 'flex' : 'none';
                }} else if (mode === 'ALERT') {{
                    card.style.display = (risk === 'HIGH' || risk === 'MEDIUM') ? 'flex' : 'none';
                }}
            }});
        }}
    </script>
</body>
</html>"""

    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[完成] 旗艦版互動儀表板已輸出至 {DASHBOARD_FILE}")


# ==========================================
# 7. 主執行入口
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
                print(f"[警告] 標的 {symbol} 抓取失敗，略過。")
                continue

            stock_prev = prev_stocks.get(symbol, {})
            eval_data = evaluate_quant_matrix(symbol, meta, market_data, stock_prev)

            eval_results.append(eval_data)
            new_stocks_dict[symbol] = {
                "risk_level": eval_data["risk_level"],
                "current_price": eval_data["current_price"],
                "peak_price": eval_data["peak_price"],
                "updated_at": now_tw_str(),
            }

            if eval_data["must_push"]:
                items_to_push.append(eval_data)

        if not eval_results:
            raise RuntimeError("無法自 Yahoo Finance 獲取任何報價。")

        # 1. 發送 LINE 推播
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

        # 2. 更新 state.json 與生成全新深色互動儀表板 index.html
        save_state(new_stocks_dict, eval_results)
        generate_pro_dashboard(eval_results)
        print("[完成] 量化監控與儀表板重建完畢。")

    except Exception as e:
        err_stack = traceback.format_exc()
        print(f"[致命錯誤] {err_stack}")
        push_system_watchdog_error(str(e))
        raise e


if __name__ == "__main__":
    main()
