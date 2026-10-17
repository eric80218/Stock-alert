#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Quantitative Risk & Valuation Engine (Dynamic Forward EPS Valuation Edition)
- 動態估值核心：自動抓取 Yahoo Finance Forward/TTM EPS 乘以設定之合理本益比計算動態合理價
- 頂層總經雷達：大戶避險 ETF (SPXU) 爆量、費半 (^SOX)、黃金攻擊量比與美元指數
- 智慧混合防呆：若 API 無法取得即時 EPS，自動切換至手動預備基準價 (Fallback)
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
# 1. 監控標的與動態估值參數 (PE Ratio & Fallback)
# ==========================================
WATCHLIST = {
    "NVDA": {
        "name": "NVIDIA",
        "market": "US",
        "currency": "$",
        "is_etf": False,
        "target_pe": 35.0,              # 合理預估本益比
        "fallback_fair_value": 220.0,   # EPS 抓取失敗時的備援合理價
        "valuation_date": "2026-10-18",
        "cost": 190.0,
        "stop_loss_pct": -10.0,
        "trailing_stop_pct": -15.0,
        "flash_pct": 4.0,
    },
    "AMAT": {
        "name": "Applied Materials",
        "market": "US",
        "currency": "$",
        "is_etf": False,
        "target_pe": 22.0,              # 合理預估本益比
        "fallback_fair_value": 480.0,   # 備援合理價
        "valuation_date": "2026-10-18",
        "cost": 420.0,
        "stop_loss_pct": -10.0,
        "trailing_stop_pct": -15.0,
        "flash_pct": 4.0,
    },
    "2330.TW": {
        "name": "台積電",
        "market": "TW",
        "currency": "NT$",
        "is_etf": False,
        "target_pe": 20.0,              # 台積電長期合理本益比
        "fallback_fair_value": 1350.0,  # 備援合理價 (已對齊當前券商預期)
        "valuation_date": "2026-10-18",
        "cost": 920.0,
        "stop_loss_pct": -10.0,
        "trailing_stop_pct": -15.0,
        "flash_pct": 3.0,
    },
    "0050.TW": {
        "name": "元大台灣50",
        "market": "TW",
        "currency": "NT$",
        "is_etf": True,
        "target_pe": 20.0,
        "fallback_fair_value": 115.0,
        "valuation_date": "2026-10-18",
        "cost": 105.0,
        "stop_loss_pct": None,
        "trailing_stop_pct": -20.0,
        "flash_pct": 2.5,
    },
}

STATE_FILE = "state.json"
DASHBOARD_FILE = "index.html"
TW_TZ = datetime.timezone(datetime.timedelta(hours=8))


def now_tw_str() -> str:
    return datetime.datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S")


def today_tw_str() -> str:
    return datetime.datetime.now(TW_TZ).strftime("%Y-%m-%d")


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[警告] 讀取狀態快取失敗: {e}")
    return {"updated_at": "", "macro_gold": {}, "stocks": {}, "results": []}


def save_state(macro_gold: dict, stocks_dict: dict, eval_results: list):
    payload = {
        "updated_at": now_tw_str(),
        "macro_gold": macro_gold,
        "stocks": stocks_dict,
        "results": eval_results,
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"[完成] 狀態數據已寫入 {STATE_FILE}")


# ==========================================
# 2. 自動取得動態 EPS 與計算合理價
# ==========================================
def get_dynamic_fair_value(symbol: str, meta: dict) -> float:
    """
    自動透過 yfinance 抓取機構預估 EPS (Forward EPS) 或 TTM EPS，
    計算出當下的動態合理價 (Fair Value = EPS * Target PE)。
    若抓取失敗則自動切換為 fallback_fair_value。
    """
    target_pe = meta.get("target_pe", 20.0)
    fallback = meta.get("fallback_fair_value", 100.0)
    is_etf = meta.get("is_etf", False)

    # ETF 不適用 EPS 估值法，直接回傳備援基準價
    if is_etf:
        return fallback

    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info
        # 優先嘗試取得 Forward EPS (預估遠期每股盈餘)，次選 trailingEps
        eps = info.get("forwardEps") or info.get("trailingEps") or info.get("epsTrailingTwelveMonths")
        
        if eps and eps > 0:
            dynamic_fair = round(eps * target_pe, 2)
            print(f"[動態估值] {symbol}: 取得 EPS = {eps}, 本益比 = {target_pe} => 動態合理價 = ${dynamic_fair}")
            return dynamic_fair
    except Exception as e:
        print(f"[警告] {symbol} 自動抓取 EPS 失敗 ({e})，採用備援合理價。")

    return fallback


# ==========================================
# 3. 頂層總經雷達
# ==========================================
def fetch_macro_gold_radar(prev_macro_state: dict) -> dict:
    print("[總經雷達] 正在掃描總經與大戶避險指標...")
    today_str = today_tw_str()
    try:
        spxu_hist = yf.Ticker("SPXU").history(period="3mo").dropna(subset=["Close", "Volume"])
        spxu_hist = spxu_hist[spxu_hist["Volume"] > 0]
        gld_hist = yf.Ticker("GLD").history(period="6mo").dropna(subset=["Close", "Volume"])
        gld_hist = gld_hist[gld_hist["Volume"] > 0]
        spy_hist = yf.Ticker("SPY").history(period="6mo").dropna(subset=["Close"])
        sox_hist = yf.Ticker("^SOX").history(period="6mo").dropna(subset=["Close"])
        dxy_hist = yf.Ticker("DX-Y.NYB").history(period="3mo").dropna(subset=["Close"])
        gcf_hist = yf.Ticker("GC=F").history(period="3mo").dropna(subset=["Close"])
        vix_hist = yf.Ticker("^VIX").history(period="1mo").dropna(subset=["Close"])
        tnx_hist = yf.Ticker("^TNX").history(period="1mo").dropna(subset=["Close"])

        spxu_vol_ratio = 1.0
        if not spxu_hist.empty and len(spxu_hist) >= 20:
            spxu_vol = spxu_hist["Volume"]
            spxu_vol_20avg = float(spxu_vol.tail(20).mean())
            spxu_vol_ratio = round(float(spxu_vol.iloc[-1]) / spxu_vol_20avg, 2) if spxu_vol_20avg > 0 else 1.0

        is_smart_money_hedging = spxu_vol_ratio >= 1.50

        gld_close = gld_hist["Close"]
        gld_vol = gld_hist["Volume"]
        gld_price = float(gld_close.iloc[-1])
        gld_ma20 = float(gld_close.rolling(20).mean().iloc[-1])
        gld_ret_1d = ((gld_price - float(gld_close.iloc[-2])) / float(gld_close.iloc[-2])) * 100
        gld_ret_20d = ((gld_price - float(gld_close.iloc[-21])) / float(gld_close.iloc[-21])) * 100

        recent_10 = gld_hist.tail(10).copy()
        recent_10["ret"] = recent_10["Close"].pct_change()
        up_days_vol = recent_10[recent_10["ret"] > 0]["Volume"]
        up_vol_avg = float(up_days_vol.mean()) if not up_days_vol.empty else float(gld_vol.tail(5).mean())
        vol_60d_avg = float(gld_vol.tail(60).mean()) if len(gld_vol) >= 60 else float(gld_vol.mean())
        effective_vol_ratio = max(round(up_vol_avg / vol_60d_avg, 2) if vol_60d_avg > 0 else 1.0, 1.0)

        sox_price = float(sox_hist["Close"].iloc[-1]) if not sox_hist.empty else 0.0
        sox_ma200 = float(sox_hist["Close"].rolling(200).mean().iloc[-1]) if len(sox_hist) >= 200 else (sox_price if sox_price > 0 else 1.0)
        is_sox_bear = sox_price < sox_ma200 if sox_price > 0 else False

        dxy_price = float(dxy_hist["Close"].iloc[-1]) if not dxy_hist.empty else 100.0
        spy_close = spy_hist["Close"]
        spy_price = float(spy_close.iloc[-1])
        spy_ret_20d = ((spy_price - float(spy_close.iloc[-21])) / float(spy_close.iloc[-21])) * 100
        gold_vs_spy_spread = round(gld_ret_20d - spy_ret_20d, 2)

        gold_spot = round(float(gcf_hist["Close"].iloc[-1]), 1) if not gcf_hist.empty else round(gld_price * 10.8, 1)
        vix_val = round(float(vix_hist["Close"].iloc[-1]), 2) if not vix_hist.empty else 18.0
        us10y_val = round(float(tnx_hist["Close"].iloc[-1]), 2) if not tnx_hist.empty else 4.2

        gld_30 = gld_close.tail(30)
        spy_30 = spy_close.reindex(gld_30.index).ffill().bfill()
        dates_30 = [d.strftime("%m/%d") for d in gld_30.index]
        gld_base = float(gld_30.iloc[0])
        spy_base = float(spy_30.iloc[0])
        gld_norm_series = [round(((float(p) / gld_base) - 1.0) * 100, 2) for p in gld_30]
        spy_norm_series = [round(((float(p) / spy_base) - 1.0) * 100, 2) for p in spy_30]

        is_gold_uptrend = gld_price >= gld_ma20 and gld_ret_20d > 2.5
        is_equity_weak = (vix_val >= 20.0) or is_sox_bear or is_smart_money_hedging

        if is_gold_uptrend and ((effective_vol_ratio >= 1.40 and gold_vs_spy_spread >= 5.0 and is_equity_weak) or is_smart_money_hedging):
            bubble_level = "高度警報：重量級大舉避險與買金"
            bubble_tag = "HIGH"
            color = "#EF4444"
            premium_shift = -10.0
            action = "🚨 聰明錢重押空頭，個股中高風險門檻已下調 10%！"
            desc = f"SPXU 量比 {spxu_vol_ratio}x，GLD 量比 {effective_vol_ratio}x，費半{'空頭' if is_sox_bear else '多頭'}。"
            should_alert = True
        elif is_gold_uptrend and (effective_vol_ratio >= 1.20 or gold_vs_spy_spread >= 4.0):
            bubble_level = "中度警戒：機構避險買盤升溫"
            bubble_tag = "MEDIUM"
            color = "#F59E0B"
            premium_shift = -5.0
            action = "⚠️ 聰明錢轉進黃金避險，個股減碼門檻收緊 5%。"
            desc = f"黃金多頭且量能放大 ({effective_vol_ratio}x)，領先美股 {gold_vs_spy_spread:+.1f}%。"
            should_alert = True
        else:
            bubble_level = "資金安穩：未見異常避險潮"
            bubble_tag = "LOW"
            color = "#10B981"
            premium_shift = 0.0
            action = "✅ 華爾街資金留駐股市，維持標準估值門檻運作。"
            desc = f"SPXU 量比 {spxu_vol_ratio}x，VIX {vix_val}，費半{'多頭' if not is_sox_bear else '跌破200MA'}。"
            should_alert = False

        prev_flash_date = prev_macro_state.get("flash_alert_date", "")
        is_macro_flash = (gld_ret_1d >= 1.8 or vix_val >= 23.0 or is_smart_money_hedging) and (prev_flash_date != today_str)

        return {
            "bubble_level": bubble_level, "bubble_tag": bubble_tag, "color": color,
            "action": action, "desc": desc, "gold_spot": gold_spot, "gld_price": round(gld_price, 2),
            "gld_ret_1d": gld_ret_1d, "gold_vs_spy_spread": gold_vs_spy_spread, "vol_ratio": effective_vol_ratio,
            "spxu_vol_ratio": spxu_vol_ratio, "vix": vix_val, "us10y": us10y_val,
            "sox_price": round(sox_price, 1), "is_sox_bear": is_sox_bear, "dxy_price": round(dxy_price, 2),
            "premium_shift": premium_shift, "must_push": should_alert or is_macro_flash,
            "flash_alert_date": today_str if is_macro_flash else prev_flash_date,
            "dates_30": dates_30, "gld_norm_series": gld_norm_series, "spy_norm_series": spy_norm_series,
        }
    except Exception as e:
        print(f"[警告] 總經雷達抓取異常: {e}")
        return {
            "bubble_level": "資料連線中", "bubble_tag": "LOW", "color": "#64748B",
            "action": "暫以標準門檻運行", "desc": str(e), "gold_spot": 0.0, "gld_price": 0.0,
            "gld_ret_1d": 0.0, "gold_vs_spy_spread": 0.0, "vol_ratio": 1.0, "spxu_vol_ratio": 1.0,
            "vix": 0.0, "us10y": 0.0, "sox_price": 0.0, "is_sox_bear": False, "dxy_price": 0.0,
            "premium_shift": 0.0, "must_push": False,
            "flash_alert_date": prev_macro_state.get("flash_alert_date", ""),
            "dates_30": [], "gld_norm_series": [], "spy_norm_series": [],
        }


def calculate_wilder_rsi(series: pd.Series, period: int = 14) -> float:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    last_gain = float(avg_gain.iloc[-1])
    last_loss = float(avg_loss.iloc[-1])
    if last_loss == 0:
        return 100.0 if last_gain > 0 else 50.0
    rs = last_gain / last_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return round(rsi, 1) if not math.isnan(rsi) else 50.0


def fetch_stock_data(symbol: str, retries: int = 3, delay: int = 3):
    for attempt in range(1, retries + 1):
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="1y")
            if hist is None or hist.empty:
                raise ValueError("回傳歷史資料為空")
            hist = hist.dropna(subset=["Close", "Open", "High"])
            if "Volume" in hist.columns:
                hist = hist[hist["Volume"] > 0]
            if len(hist) < 5:
                raise ValueError("有效交易日數不足 5 天")

            close_s = hist["Close"]
            current_price = float(close_s.iloc[-1])
            prev_close = float(close_s.iloc[-2])
            open_price = float(hist["Open"].iloc[-1])
            high_60d = float(hist["High"].tail(60).max())

            gap_pct = ((open_price - prev_close) / prev_close) * 100
            day_change_pct = ((current_price - prev_close) / prev_close) * 100

            ma200_val = close_s.rolling(window=200).mean().iloc[-1] if len(close_s) >= 200 else close_s.mean()
            ma20_val = close_s.rolling(window=20).mean().iloc[-1] if len(close_s) >= 20 else close_s.mean()
            rsi14 = calculate_wilder_rsi(close_s, 14)

            recent_30 = hist.tail(30)
            history_dates = [d.strftime("%m/%d") for d in recent_30.index]
            history_prices = [round(float(p), 2) for p in recent_30["Close"].tolist()]

            return {
                "current_price": round(current_price, 2), "prev_close": round(prev_close, 2),
                "open_price": round(open_price, 2), "high_60d": round(high_60d, 2),
                "gap_pct": round(gap_pct, 2), "day_change_pct": round(day_change_pct, 2),
                "ma200": round(float(ma200_val), 2), "ma20": round(float(ma20_val), 2),
                "rsi14": rsi14, "history_dates": history_dates, "history_prices": history_prices,
            }
        except Exception as e:
            print(f"[重試 {attempt}/{retries}] {symbol} 抓取異常: {e}")
            time.sleep(delay * attempt)
    return None


def evaluate_quant_matrix(
    symbol: str, meta: dict, market_data: dict, prev_stock_state: dict, macro_gold: dict, dynamic_fair: float
) -> dict:
    price = market_data["current_price"]
    high_60d = market_data["high_60d"]
    ma200 = market_data["ma200"]
    ma20 = market_data["ma20"]
    rsi14 = market_data["rsi14"]
    gap_pct = market_data["gap_pct"]
    day_change_pct = market_data["day_change_pct"]

    fair = dynamic_fair
    cost = float(meta["cost"])
    is_etf = meta.get("is_etf", False)
    stop_loss = meta.get("stop_loss_pct", None)
    trailing_stop = meta.get("trailing_stop_pct", -15.0)
    flash_threshold = float(meta.get("flash_pct", 4.0))

    raw_peak = prev_stock_state.get("peak_price", price)
    cached_peak = price if (raw_peak is None or math.isnan(float(raw_peak))) else float(raw_peak)
    peak_price = max(cached_peak, high_60d, price)
    drawdown_from_peak = ((price - peak_price) / peak_price) * 100

    safety_margin = ((fair - price) / fair) * 100
    premium_rate = ((price - fair) / fair) * 100
    pnl_rate = ((price - cost) / cost) * 100

    shift = macro_gold.get("premium_shift", 0.0)
    is_sox_bear = macro_gold.get("is_sox_bear", False)
    sector_penalty = -3.0 if (not is_etf and is_sox_bear) else 0.0
    total_shift = shift + sector_penalty

    high_risk_threshold = 25.0 + total_shift
    med_risk_threshold = 12.0 + total_shift
    macro_note = f" (⚠️受總經連動下調 {abs(total_shift):.0f}%)" if total_shift < 0 else ""

    if not is_etf and stop_loss is not None and pnl_rate <= stop_loss:
        risk_level, risk_tag, color = "停損警戒", "HIGH", "#EF4444"
        action, desc, should_alert = "市價全數平倉 / 嚴格停損", f"成本損益達 {pnl_rate:.1f}%，跌破防線。", True
    elif drawdown_from_peak <= trailing_stop and pnl_rate > 8.0:
        risk_level, risk_tag, color = "移動停利", "HIGH", "#DC2626"
        action, desc, should_alert = "出清持股 1/2 / 鎖定波段獲利", f"自波段高點回撤 {drawdown_from_peak:.1f}%。", True
    elif premium_rate > high_risk_threshold:
        risk_level, risk_tag, color = "高風險 (嚴重昂貴)", "HIGH", "#EF4444"
        action, desc, should_alert = "執行第 2 階減碼：賣出持倉 1/3", f"溢價率達 {premium_rate:.1f}% (門檻 >{high_risk_threshold:.0f}%{macro_note})。", True
    elif med_risk_threshold <= premium_rate <= high_risk_threshold:
        risk_level, risk_tag, color = "中風險 (估值警戒)", "MEDIUM", "#F59E0B"
        action, desc, should_alert = "執行第 1 階減碼：賣出持倉 1/4", f"溢價率 {premium_rate:.1f}% (門檻 >{med_risk_threshold:.0f}%{macro_note})。", True
    elif safety_margin >= 10.0:
        if is_etf:
            risk_level, risk_tag, color = "低風險 (ETF加碼區)", "LOW", "#10B981"
            action, desc, should_alert = "大盤折價，執行定期定額或加碼", f"安全邊際達 {safety_margin:.1f}%。", True
        else:
            if price < ma200 or is_sox_bear:
                risk_level, risk_tag, color = "偏空觀察 (左側禁買)", "MEDIUM", "#F97316"
                action, desc, should_alert = "禁止接刀，等待站回均線", f"安全邊際充足 ({safety_margin:.1f}%) 但受制於趨勢空頭。", False
            else:
                risk_level, risk_tag, color = "低風險 (買進區)", "LOW", "#10B981"
                action, desc, should_alert = "啟動第 1 批建倉：買進 1/3", f"安全邊際達 {safety_margin:.1f}% 且多頭排列。", True
    else:
        risk_level, risk_tag, color = "合理估值區", "NORMAL", "#3B82F6"
        action, desc, should_alert = "定期定額 / 維持續抱", f"處於合理估值防禦帶內 (警戒線 >{med_risk_threshold:.0f}%{macro_note})。", False

    today_str = today_tw_str()
    last_risk = prev_stock_state.get("risk_level", "")
    is_state_changed = last_risk != risk_level
    prev_flash_date = prev_stock_state.get("flash_alert_date", "")
    is_intraday_flash = (abs(day_change_pct) >= flash_threshold) and (prev_flash_date != today_str)

    if is_intraday_flash:
        action = f"⚡【{'暴漲急拉' if day_change_pct > 0 else '盤中急殺'} {day_change_pct:+.1f}%】" + action

    must_push = (should_alert and (is_state_changed or "停損" in risk_level)) or is_intraday_flash

    return {
        "symbol": symbol, "name": meta.get("name", symbol), "market": meta.get("market", "US"),
        "currency": meta.get("currency", "$"), "is_etf": is_etf, "current_price": price,
        "day_change_pct": day_change_pct, "gap_pct": gap_pct, "ma200": ma200, "ma20": ma20,
        "rsi14": rsi14, "peak_price": round(peak_price, 2), "drawdown_from_peak": round(drawdown_from_peak, 2),
        "fair_value": fair, "cost": float(meta["cost"]), "safety_margin": round(safety_margin, 2),
        "premium_rate": round(premium_rate, 2), "pnl_rate": round(pnl_rate, 2),
        "high_risk_threshold": high_risk_threshold, "med_risk_threshold": med_risk_threshold,
        "risk_level": risk_level, "risk_tag": risk_tag, "action": action, "color": color, "desc": desc,
        "must_push": must_push, "flash_alert_date": today_str if is_intraday_flash else prev_flash_date,
        "history_dates": market_data["history_dates"], "history_prices": market_data["history_prices"],
    }


def build_macro_gold_bubble(macro: dict) -> dict:
    return {
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": macro["color"], "paddingAll": "15px",
            "contents": [
                {"type": "text", "text": "🛡️ 華爾街大戶避險與動態估值雷達", "weight": "bold", "color": "#FFFFFF", "size": "sm"},
                {"type": "text", "text": macro["bubble_level"], "weight": "bold", "color": "#FFFFFF", "size": "sm", "margin": "xs"}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "大戶避險(SPXU) / 費半", "color": "#6B7280", "size": "sm"},
                    {"type": "text", "text": f"{macro['spxu_vol_ratio']}x | ${macro['sox_price']}", "weight": "bold", "size": "xs", "align": "end"}
                ]},
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "黃金攻擊量比 / 金股差", "color": "#6B7280", "size": "sm"},
                    {"type": "text", "text": f"{macro['vol_ratio']}x | {macro['gold_vs_spy_spread']:+.1f}%", "weight": "bold", "size": "sm", "align": "end", "color": macro["color"]}
                ]},
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": macro["action"], "weight": "bold", "size": "xs", "color": macro["color"], "wrap": True, "margin": "md"},
                {"type": "text", "text": macro["desc"], "size": "xs", "color": "#4B5563", "wrap": True, "margin": "xs"}
            ]
        }
    }


def build_stock_bubble(item: dict) -> dict:
    pnl_sign, day_sign = ("+" if item["pnl_rate"] > 0 else ""), ("+" if item["day_change_pct"] > 0 else "")
    pnl_color = "#10B981" if item["pnl_rate"] >= 0 else "#EF4444"
    cur, external_url = item["currency"], f"https://finance.yahoo.com/quote/{item['symbol']}"
    return {
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": item["color"], "paddingAll": "15px",
            "contents": [{
                "type": "box", "layout": "horizontal",
                "contents": [
                    {"type": "text", "text": f"{item['symbol']} {item['name']}", "weight": "bold", "color": "#FFFFFF", "size": "lg", "flex": 4},
                    {"type": "text", "text": item["risk_level"].split()[0], "weight": "bold", "color": "#FFFFFF", "size": "xs", "align": "end", "flex": 3}
                ]
            }]
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "contents": [
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "現價 (動態合理價)", "color": "#6B7280", "size": "sm"},
                    {"type": "text", "text": f"{cur}{item['current_price']} (折{cur}{item['fair_value']})", "weight": "bold", "size": "xs", "align": "end"}
                ]},
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "持股損益 / 60D回撤", "color": "#6B7280", "size": "sm"},
                    {"type": "text", "text": f"{pnl_sign}{item['pnl_rate']}% / {item['drawdown_from_peak']}%", "weight": "bold", "size": "sm", "align": "end", "color": pnl_color}
                ]},
                {"type": "separator", "margin": "sm"},
                {"type": "box", "layout": "vertical", "contents": [
                    {"type": "text", "text": f"【指令】{item['action']}", "weight": "bold", "size": "sm", "color": item["color"]},
                    {"type": "text", "text": item["desc"], "size": "xs", "color": "#4B5563", "wrap": True, "margin": "xs"}
                ]}
            ]
        },
        "footer": {
            "type": "box", "layout": "vertical",
            "contents": [{"type": "button", "style": "link", "height": "sm", "action": {"type": "uri", "label": "開啟 Yahoo Finance 即時線圖", "uri": external_url}}]
        }
    }


def send_line_message(payload: dict):
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    if not token: return
    try:
        requests.post("https://api.line.me/v2/bot/message/push", headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"}, json=payload, timeout=15)
    except Exception as e:
        print(f"[異常] LINE 推播失敗: {e}")


def push_system_watchdog_error(error_msg: str):
    user_id = os.environ.get("LINE_USER_ID")
    if not user_id: return
    send_line_message({"to": user_id, "messages": [{"type": "text", "text": f"🚨【系統異常警報】\n{error_msg[:300]}"}]})


def generate_pro_dashboard(macro_gold: dict, eval_results: list):
    updated_time, total_count = now_tw_str(), len(eval_results)
    high_risk_count = sum(1 for r in eval_results if r["risk_tag"] in ["HIGH", "MEDIUM"])
    buy_zone_count = sum(1 for r in eval_results if r["risk_tag"] == "LOW")
    avg_pnl = round(sum(r["pnl_rate"] for r in eval_results) / total_count, 2) if total_count else 0.0
    avg_pnl_color = "text-emerald-400" if avg_pnl >= 0 else "text-rose-400"
    m_color, sox_cls = macro_gold["color"], ("text-rose-400" if macro_gold["is_sox_bear"] else "text-emerald-400")

    cards_html = ""
    for idx, r in enumerate(eval_results):
        cur, pnl_sign, day_sign = r["currency"], ("+" if r["pnl_rate"] > 0 else ""), ("+" if r["day_change_pct"] > 0 else "")
        pnl_cls, day_cls = ("text-emerald-400" if r["pnl_rate"] >= 0 else "text-rose-400"), ("text-emerald-400" if r["day_change_pct"] >= 0 else "text-rose-400")
        gauge_pct = round(50.0 + (max(-40.0, min(40.0, r["safety_margin"])) / 40.0) * 45.0, 1)

        cards_html += f"""
        <div class="stock-card bg-slate-900/90 border border-slate-800 rounded-2xl shadow-xl overflow-hidden flex flex-col justify-between" data-market="{r['market']}" data-risk="{r['risk_tag']}">
            <div>
                <div class="px-5 py-3.5 flex justify-between items-center border-b border-slate-800/80" style="background: linear-gradient(90deg, {r['color']}22 0%, transparent 100%); border-left: 4px solid {r['color']};">
                    <div>
                        <div class="flex items-center gap-2">
                            <span class="text-lg font-black tracking-wide text-white">{r['symbol']}</span>
                            <span class="text-xs px-2 py-0.5 rounded bg-slate-800 text-slate-300 font-medium">{r['market']}</span>
                        </div>
                        <p class="text-xs text-slate-400 mt-0.5">{r['name']}</p>
                    </div>
                    <span class="px-3 py-1 rounded-full text-xs font-bold text-white shadow-sm" style="background-color: {r['color']};">{r['risk_level']}</span>
                </div>
                <div class="p-5">
                    <div class="flex justify-between items-end mb-4">
                        <div>
                            <span class="text-xs text-slate-400 block mb-1">市價 (動態合理價: {cur}{r['fair_value']})</span>
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
                    <div class="mb-4 bg-slate-950/60 p-3 rounded-xl border border-slate-800/60">
                        <div class="flex justify-between text-xs mb-1.5">
                            <span class="text-slate-400">安全邊際量尺</span>
                            <span class="font-bold" style="color: {r['color']}">邊際 {r['safety_margin']}% / 溢價 {r['premium_rate']}%</span>
                        </div>
                        <div class="w-full h-2 bg-slate-800 rounded-full relative overflow-hidden">
                            <div class="h-full rounded-full transition-all duration-500" style="width: {gauge_pct}%; background-color: {r['color']};"></div>
                        </div>
                    </div>
                    <div class="h-28 w-full mb-4 bg-slate-950/40 rounded-xl p-2 border border-slate-800/40"><canvas id="chart-{idx}"></canvas></div>
                    <div class="grid grid-cols-3 gap-2 text-xs bg-slate-950/50 p-3 rounded-xl border border-slate-800/50 mb-4">
                        <div><span class="text-slate-500 block">持有成本</span><span class="font-bold text-slate-200">{cur}{r['cost']}</span></div>
                        <div><span class="text-slate-500 block">200MA</span><span class="font-bold text-slate-200">{cur}{r['ma200']}</span></div>
                        <div><span class="text-slate-500 block">RSI(14)</span><span class="font-bold text-amber-400">{r['rsi14']}</span></div>
                    </div>
                    <div class="p-3.5 rounded-xl border" style="background-color: {r['color']}15; border-color: {r['color']}40;">
                        <div class="text-xs font-extrabold" style="color: {r['color']};">⚡ 指令：{r['action']}</div>
                        <p class="text-xs text-slate-300 mt-1">{r['desc']}</p>
                    </div>
                </div>
            </div>
            <div class="px-5 py-3 bg-slate-950/80 border-t border-slate-800/80 flex justify-between items-center text-xs">
                <a href="https://finance.yahoo.com/quote/{r['symbol']}" target="_blank" class="text-blue-400 hover:text-blue-300">Yahoo ↗</a>
                <a href="https://www.tradingview.com/symbols/{r['symbol'].replace('.TW', '')}/" target="_blank" class="text-slate-400">TradingView ↗</a>
            </div>
        </div>
        """

    charts_json = json.dumps([{"id": f"chart-{idx}", "dates": r["history_dates"], "prices": r["history_prices"], "fair": r["fair_value"], "color": r["color"]} for idx, r in enumerate(eval_results)], ensure_ascii=False)
    macro_chart_json = json.dumps({"dates": macro_gold["dates_30"], "gld": macro_gold["gld_norm_series"], "spy": macro_gold["spy_norm_series"]}, ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html lang="zh-TW" class="dark">
<head><meta charset="UTF-8"><title>Quant Portfolio</title><script src="https://cdn.tailwindcss.com"></script><script src="https://cdn.jsdelivr.net/npm/chart.js"></script></head>
<body class="bg-slate-950 text-slate-100 min-h-screen font-sans p-6">
    <div class="max-w-7xl mx-auto">
        <header class="flex flex-col md:flex-row justify-between items-center pb-6 border-b border-slate-800 gap-4">
            <div>
                <h1 class="text-2xl font-black text-white">動態 EPS 估值與華爾街大戶避險系統</h1>
                <p class="text-xs text-slate-400 mt-1">每小時自動同步 Forward EPS 與總經數據 ｜ 更新: {updated_time}</p>
            </div>
            <div class="flex gap-2">
                <button onclick="filterCards('ALL')" class="px-3 py-1 rounded bg-blue-600 text-xs font-bold">全部</button>
                <button onclick="filterCards('ALERT')" class="px-3 py-1 rounded bg-rose-950 text-rose-300 text-xs font-bold">警戒 ({high_risk_count})</button>
            </div>
        </header>
        <section class="my-6 rounded-2xl border p-5" style="background: linear-gradient(135deg, {m_color}18 0%, #0F172A 65%); border-color: {m_color}60;">
            <div class="flex justify-between items-center"><h2 class="text-lg font-black text-white">🛡️ 總經避險雷達</h2><span class="px-3 py-1 rounded-full text-xs font-bold text-white" style="background-color: {m_color};">{macro_gold['bubble_level']}</span></div>
            <p class="text-xs text-slate-300 mt-2">{macro_gold['action']} - {macro_gold['desc']}</p>
        </section>
        <main class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">{cards_html}</main>
    </div>
    <script>
        const chartConfigs = {charts_json};
        window.addEventListener('DOMContentLoaded', () => {{
            chartConfigs.forEach(cfg => {{
                const ctx = document.getElementById(cfg.id);
                if (!ctx) return;
                new Chart(ctx, {{
                    type: 'line',
                    data: {{ labels: cfg.dates, datasets: [{{ data: cfg.prices, borderColor: cfg.color, borderWidth: 2, pointRadius: 0, fill: true, backgroundColor: cfg.color+'20' }}, {{ data: Array(cfg.dates.length).fill(cfg.fair), borderColor: '#94A3B8', borderDash: [4,4], pointRadius: 0 }}] }},
                    options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ display: false }} }}, scales: {{ x: {{ display: false }}, y: {{ ticks: {{ color: '#64748B', font: {{ size: 9 }} }}, grid: {{ color: '#1E293B' }} }} }}
                }});
            }});
        }});
        function filterCards(mode) {{
            document.querySelectorAll('.stock-card').forEach(card => {{
                card.style.display = (mode === 'ALL' || card.getAttribute('data-risk') === 'HIGH' || card.getAttribute('data-risk') === 'MEDIUM') ? 'flex' : 'none';
            }});
        }}
    </script>
</body>
</html>"""
    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f: f.write(html)


def main():
    user_id = os.environ.get("LINE_USER_ID")
    is_manual_run = os.environ.get("GITHUB_EVENT_NAME", "") == "workflow_dispatch"
    prev_state, prev_stocks, prev_macro = load_state(), load_state().get("stocks", {}), load_state().get("macro_gold", {})

    eval_results, new_stocks_dict, bubbles_to_push = [], {}, []
    try:
        macro_gold = fetch_macro_gold_radar(prev_macro)
        if macro_gold.get("must_push") or is_manual_run:
            bubbles_to_push.append(build_macro_gold_bubble(macro_gold))

        for symbol, meta in WATCHLIST.items():
            market_data = fetch_stock_data(symbol)
            if not market_data: continue

            # 動態計算合理價 (Forward EPS * PE)
            dynamic_fair = get_dynamic_fair_value(symbol, meta)

            eval_data = evaluate_quant_matrix(symbol, meta, market_data, prev_stocks.get(symbol, {}), macro_gold, dynamic_fair)
            eval_results.append(eval_data)
            new_stocks_dict[symbol] = {"risk_level": eval_data["risk_level"], "current_price": eval_data["current_price"], "peak_price": eval_data["peak_price"], "flash_alert_date": eval_data["flash_alert_date"], "updated_at": now_tw_str()}

            if eval_data["must_push"] or is_manual_run:
                bubbles_to_push.append(build_stock_bubble(eval_data))

        if not eval_results: raise RuntimeError("無法獲取任何個股報價。")

        if bubbles_to_push and user_id:
            send_line_message({"to": user_id, "messages": [{"type": "flex", "altText": "🛡️【動態估值與風控通知】", "contents": {"type": "carousel", "contents": bubbles_to_push[:10]}}]})

        macro_summary = {"bubble_level": macro_gold["bubble_level"], "bubble_tag": macro_gold["bubble_tag"], "gold_spot": macro_gold["gold_spot"], "gld_price": macro_gold["gld_price"], "vol_ratio": macro_gold["vol_ratio"], "spxu_vol_ratio": macro_gold["spxu_vol_ratio"], "gold_vs_spy_spread": macro_gold["gold_vs_spy_spread"], "vix": macro_gold["vix"], "us10y": macro_gold["us10y"], "sox_price": macro_gold["sox_price"], "is_sox_bear": macro_gold["is_sox_bear"], "dxy_price": macro_gold["dxy_price"], "flash_alert_date": macro_gold["flash_alert_date"], "updated_at": now_tw_str()}
        save_state(macro_summary, new_stocks_dict, eval_results)
        generate_pro_dashboard(macro_gold, eval_results)
        print("[完成] 動態 EPS 估值與量化巡檢執行完畢。")
    except Exception as e:
        push_system_watchdog_error(str(e))
        raise e

if __name__ == "__main__":
    main()
