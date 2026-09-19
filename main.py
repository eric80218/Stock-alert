import os
import sys
import json
import requests
import io
import pandas as pd
import yfinance as yf
from typing import Dict, List, Optional, Tuple

# ==========================================
# 1. 系統設定與環境變數
# ==========================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_USER_ID = os.getenv("LINE_USER_ID", "")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")
PORTFOLIO_SHEET_URL = os.getenv("PORTFOLIO_SHEET_URL", "")

RUN_MODE = os.getenv("RUN_MODE", "ALL").upper()
FORCE_NOTIFY = os.getenv("FORCE_NOTIFY", "false").lower() == "true"

DASHBOARD_URL = "https://eric80218.github.io/Stock-alert/"
CACHE_FILE = "state_cache.json"

# ==========================================
# 2. Google 試算表動態同步模組 (UTF-8 編碼)
# ==========================================
def load_portfolio_watchlist() -> List[dict]:
    if not PORTFOLIO_SHEET_URL:
        print("⚠️ 未設定 PORTFOLIO_SHEET_URL")
        return []
    
    try:
        res = requests.get(PORTFOLIO_SHEET_URL, timeout=10)
        res.encoding = 'utf-8'
        
        if res.status_code == 200:
            df = pd.read_csv(io.StringIO(res.text))
            portfolio = []
            for _, r in df.iterrows():
                ticker = str(r.get("ticker", "")).strip()
                if not ticker or ticker == "nan":
                    continue
                
                shares = pd.to_numeric(r.get("shares"), errors='coerce')
                cost_price = pd.to_numeric(r.get("cost_price"), errors='coerce')
                base_fair = pd.to_numeric(r.get("base_fair"), errors='coerce')
                mos_buy = pd.to_numeric(r.get("mos_buy"), errors='coerce')
                expensive_sell = pd.to_numeric(r.get("expensive_sell"), errors='coerce')

                portfolio.append({
                    "ticker": ticker,
                    "name": str(r.get("name", ticker)).strip(),
                    "currency": str(r.get("currency", "USD")).strip().upper(),
                    "broker": str(r.get("broker", "一般")).strip(),
                    "shares": float(shares) if pd.notna(shares) else 0.0,
                    "cost_price": float(cost_price) if pd.notna(cost_price) else 0.0,
                    "base_fair": float(base_fair) if pd.notna(base_fair) else 100.0,
                    "val_model": str(r.get("val_model", "BASE")).strip().upper(),
                    "mos_buy": float(mos_buy) if pd.notna(mos_buy) else 0.15,
                    "expensive_sell": float(expensive_sell) if pd.notna(expensive_sell) else 0.25
                })
            print(f"✅ 成功從 Google 試算表載入 {len(portfolio)} 檔持股資料！")
            return portfolio
    except Exception as e:
        print(f"❌ 讀取 Google 試算表異常: {e}")
    
    return []

# ==========================================
# 3. 台灣證交所 (TWSE) 官方權威指標
# ==========================================
def fetch_twse_official_metrics() -> dict:
    url = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"
    twse_dict = {}
    try:
        res = requests.get(url, timeout=8)
        if res.status_code == 200:
            for row in res.json():
                code = row.get("Code")
                if code:
                    pe = float(row.get("PEratio", 0)) if row.get("PEratio") and row.get("PEratio") != "-" else None
                    div_yield = float(row.get("DividendYield", 0)) if row.get("DividendYield") and row.get("DividendYield") != "-" else None
                    twse_dict[code] = {"pe": pe, "yield": div_yield}
    except Exception:
        pass
    return twse_dict

# ==========================================
# 4. 全自動動態估值引擎
# ==========================================
def calculate_dynamic_fair_value(item: dict, yf_info: dict, twse_data: dict, current_price: float) -> Tuple[float, str]:
    ticker = item["ticker"]
    base_fair = item["base_fair"]
    model = item["val_model"]

    if model == "ANALYST":
        if FINNHUB_API_KEY:
            try:
                fh_url = f"https://finnhub.io/api/v1/stock/price-target?symbol={ticker}&token={FINNHUB_API_KEY}"
                res = requests.get(fh_url, timeout=5).json()
                fh_target = res.get("targetMean") or res.get("targetMedian")
                if fh_target and 0.5 * base_fair <= fh_target <= 2.0 * base_fair:
                    return round(float(fh_target), 2), "Finnhub法人目標"
            except Exception:
                pass
        try:
            yf_target = yf_info.get("targetMeanPrice") or yf_info.get("targetMedianPrice")
            if yf_target and 0.5 * base_fair <= yf_target <= 2.0 * base_fair:
                return round(float(yf_target), 2), "Yahoo法人目標"
        except Exception:
            pass

    elif model == "DIVIDEND":
        clean_code = ticker.replace(".TW", "").replace(".TWO", "")
        tw_metric = twse_data.get(clean_code)
        if tw_metric and tw_metric.get("yield"):
            curr_yield = tw_metric["yield"]
            if curr_yield > 0.5 and current_price > 0:
                calc_val = (current_price * (curr_yield / 100.0)) / 0.05
                if 0.5 * base_fair <= calc_val <= 1.8 * base_fair:
                    return round(calc_val, 2), "證交所殖利率折算"
        
        div_rate = yf_info.get("dividendRate") or 0.0
        if div_rate > 0:
            calc_val = div_rate / 0.05
            if 0.5 * base_fair <= calc_val <= 1.8 * base_fair:
                return round(calc_val, 2), "股息5%折算法"

    elif model == "PE_EPS":
        eps = yf_info.get("trailingEps") or yf_info.get("forwardEps")
        pe = yf_info.get("trailingPE")
        if eps and eps > 0:
            applied_pe = min(28.0, max(12.0, pe)) if pe else (base_fair / eps)
            calc_val = eps * applied_pe
            if 0.5 * base_fair <= calc_val <= 1.8 * base_fair:
                return round(calc_val, 2), "最新EPS本益比"

    return base_fair, "基準錨定"

# ==========================================
# 5. 狀態記憶模組
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
# 6. 夜盤與總經數據
# ==========================================
def fetch_global_macro_snapshot() -> dict:
    indicators = {"TSM": "TSM", "NQ_F": "NQ=F", "ES_F": "ES=F", "VIX": "^VIX", "TNX": "^TNX", "DXY": "DX-Y.NYB"}
    data = {}
    for key, symbol in indicators.items():
        try:
            t = yf.Ticker(symbol)
            hist = t.history(period="5d")
            close = hist['Close'].dropna()
            if len(close) >= 2:
                curr = float(close.iloc[-1])
                prev = float(close.iloc[-2])
                chg_pct = ((curr - prev) / prev) * 100
                data[key] = {"price": round(curr, 2), "chg_pct": round(chg_pct, 2)}
            else:
                data[key] = {"price": 0.0, "chg_pct": 0.0}
        except Exception:
            data[key] = {"price": 0.0, "chg_pct": 0.0}
    return data

def build_morning_brief_bubble(macro: dict) -> dict:
    tsm = macro.get("TSM", {})
    nq = macro.get("NQ_F", {})
    es = macro.get("ES_F", {})
    vix = macro.get("VIX", {})
    tnx = macro.get("TNX", {})
    dxy = macro.get("DXY", {})

    score = 0
    if tsm.get("chg_pct", 0) > 1.0: score += 1
    elif tsm.get("chg_pct", 0) < -1.0: score -= 1

    if nq.get("chg_pct", 0) > 0.5: score += 1
    elif nq.get("chg_pct", 0) < -0.5: score -= 1

    vix_val = vix.get("price", 15)
    if vix_val >= 25: score -= 2
    elif vix_val <= 16: score += 1

    if score >= 2:
        market_mood, mood_color, header_bg = "🟢 多方強勢開出", "#10B981", "#064E3B"
        advice = "台積電 ADR 與美指期表現亮眼，台股早盤高機率開高。持股續抱，嚴防急拉追高，逢回調支撐可分批布局。"
    elif score <= -2:
        market_mood, mood_color, header_bg = "🔴 恐慌偏空震盪", "#EF4444", "#7F1D1D"
        advice = f"VIX 恐慌指數攀升至 {vix_val}，避險情緒高漲，開盤恐面臨回吐賣壓。建議多看少做，保留充足現金。"
    else:
        market_mood, mood_color, header_bg = "🟡 平衡震盪整理", "#F59E0B", "#78350F"
        advice = "夜盤與總經指標多空拉鋸，預估開盤平開震盪。按既定合理價與分批紀律操作即可，無須過度反應。"

    def fmt_chg(val): return f"{val:+.2f}%" if val else "0.00%"

    return {
        "type": "bubble", "size": "kilo",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": header_bg, "paddingAll": "16px",
            "contents": [
                {
                    "type": "box", "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "☀️ 全球晨間前瞻", "weight": "bold", "color": "#FFFFFF", "size": "md", "flex": 3},
                        {"type": "text", "text": market_mood, "weight": "bold", "color": mood_color, "size": "sm", "align": "end", "flex": 3}
                    ]
                },
                {"type": "text", "text": "台美夜盤期指 · VIX 恐慌 · 總經避險總覽", "color": "#CBD5E1", "size": "xs", "margin": "xs"}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "backgroundColor": "#0F172A", "paddingAll": "16px", "spacing": "sm",
            "contents": [
                {"type": "text", "text": "⚡ 夜盤期指動向", "color": "#38BDF8", "weight": "bold", "size": "xs"},
                {
                    "type": "box", "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "台積電 ADR (TSM)", "color": "#94A3B8", "size": "xs"},
                        {"type": "text", "text": f"${tsm.get('price')} ({fmt_chg(tsm.get('chg_pct'))})", "color": tsm.get('chg_pct',0)>=0 and "#34D399" or "#F87171", "weight": "bold", "size": "xs", "align": "end"}
                    ]
                },
                {
                    "type": "box", "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "那斯達克期 (NQ)", "color": "#94A3B8", "size": "xs"},
                        {"type": "text", "text": f"{fmt_chg(nq.get('chg_pct'))}", "color": nq.get('chg_pct',0)>=0 and "#34D399" or "#F87171", "weight": "bold", "size": "xs", "align": "end"}
                    ]
                },
                {
                    "type": "box", "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "標普 500 期 (ES)", "color": "#94A3B8", "size": "xs"},
                        {"type": "text", "text": f"{fmt_chg(es.get('chg_pct'))}", "color": es.get('chg_pct',0)>=0 and "#34D399" or "#F87171", "weight": "bold", "size": "xs", "align": "end"}
                    ]
                },
                {"type": "separator", "color": "#334155", "margin": "sm"},
                {"type": "text", "text": "🌐 國際情勢與總經風險", "color": "#38BDF8", "weight": "bold", "size": "xs", "margin": "sm"},
                {
                    "type": "box", "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "VIX 恐慌指數", "color": "#94A3B8", "size": "xs"},
                        {"type": "text", "text": f"{vix.get('price')} ({vix_val>=25 and '恐慌警戒' or '常態平穩'})", "color": vix_val>=25 and "#EF4444" or "#CBD5E1", "weight": "bold", "size": "xs", "align": "end"}
                    ]
                },
                {
                    "type": "box", "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "10年美債殖利率", "color": "#94A3B8", "size": "xs"},
                        {"type": "text", "text": f"{tnx.get('price')}%", "color": "#CBD5E1", "size": "xs", "align": "end"}
                    ]
                },
                {"type": "separator", "color": "#334155", "margin": "md"},
                {
                    "type": "box", "layout": "vertical", "backgroundColor": "#1E293B", "paddingAll": "12px", "cornerRadius": "8px", "margin": "md",
                    "contents": [
                        {"type": "text", "text": "💡 今日盤前作戰方針：", "color": "#38BDF8", "weight": "bold", "size": "xs"},
                        {"type": "text", "text": advice, "color": "#F8FAFC", "size": "xxs", "wrap": True, "margin": "xs"}
                    ]
                }
            ]
        },
        "footer": {
            "type": "box", "layout": "horizontal", "backgroundColor": "#1E293B", "paddingAll": "10px",
            "contents": [
                {"type": "button", "style": "primary", "height": "sm", "color": "#0284C7", "action": {"type": "uri", "label": "開啟估值儀表板", "uri": DASHBOARD_URL}}
            ]
        }
    }

# ==========================================
# 7. 資產總覽卡片
# ==========================================
def build_portfolio_summary_bubble(summary: dict) -> dict:
    usd_val, usd_pnl, usd_pct = summary["usd_val"], summary["usd_pnl"], summary["usd_pnl_pct"]
    twd_val, twd_pnl, twd_pct = summary["twd_val"], summary["twd_pnl"], summary["twd_pnl_pct"]

    usd_pnl_color = "#34D399" if usd_pnl >= 0 else "#F87171"
    twd_pnl_color = "#34D399" if twd_pnl >= 0 else "#F87171"

    usd_val_str = f"${usd_val:,.2f}"
    usd_pnl_str = f"{usd_pnl:+,.2f} ({usd_pct:+.2f}%)" if summary["usd_cost"] > 0 else "$0.00 (0.00%)"

    twd_val_str = f"NT${twd_val:,.0f}"
    twd_pnl_str = f"{twd_pnl:+,.0f} ({twd_pct:+.2f}%)" if summary["twd_cost"] > 0 else "NT$0 (0.00%)"

    return {
        "type": "bubble", "size": "kilo",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#1E1B4B", "paddingAll": "16px",
            "contents": [
                {
                    "type": "box", "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "💼 資產部位與損益總覽", "weight": "bold", "color": "#FFFFFF", "size": "md", "flex": 4},
                        {"type": "text", "text": "即時同步", "color": "#C7D2FE", "size": "xs", "align": "end", "flex": 2}
                    ]
                },
                {"type": "text", "text": "Firstrade · UBS ESPP · 國泰證券", "color": "#A5B4FC", "size": "xs", "margin": "xs"}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "backgroundColor": "#0F172A", "paddingAll": "16px", "spacing": "md",
            "contents": [
                {
                    "type": "box", "layout": "vertical", "backgroundColor": "#1E293B", "paddingAll": "12px", "cornerRadius": "8px",
                    "contents": [
                        {
                            "type": "box", "layout": "horizontal",
                            "contents": [
                                {"type": "text", "text": "🇺🇸 美股總市值 (USD)", "color": "#94A3B8", "size": "xs"},
                                {"type": "text", "text": usd_val_str, "color": "#FFFFFF", "weight": "bold", "size": "sm", "align": "end"}
                            ]
                        },
                        {
                            "type": "box", "layout": "horizontal", "margin": "xs",
                            "contents": [
                                {"type": "text", "text": "未實現損益", "color": "#64748B", "size": "xxs"},
                                {"type": "text", "text": usd_pnl_str, "color": usd_pnl_color, "weight": "bold", "size": "xs", "align": "end"}
                            ]
                        }
                    ]
                },
                {
                    "type": "box", "layout": "vertical", "backgroundColor": "#1E293B", "paddingAll": "12px", "cornerRadius": "8px",
                    "contents": [
                        {
                            "type": "box", "layout": "horizontal",
                            "contents": [
                                {"type": "text", "text": "🇹🇼 台股總市值 (TWD)", "color": "#94A3B8", "size": "xs"},
                                {"type": "text", "text": twd_val_str, "color": "#FFFFFF", "weight": "bold", "size": "sm", "align": "end"}
                            ]
                        },
                        {
                            "type": "box", "layout": "horizontal", "margin": "xs",
                            "contents": [
                                {"type": "text", "text": "未實現損益", "color": "#64748B", "size": "xxs"},
                                {"type": "text", "text": twd_pnl_str, "color": twd_pnl_color, "weight": "bold", "size": "xs", "align": "end"}
                            ]
                        }
                    ]
                },
                {"type": "text", "text": "💡 數據源自 Google 試算表，手機修改股數即時更新連動。", "color": "#94A3B8", "size": "xxs", "wrap": True}
            ]
        },
        "footer": {
            "type": "box", "layout": "horizontal", "backgroundColor": "#1E293B", "paddingAll": "10px",
            "contents": [
                {"type": "button", "style": "primary", "height": "sm", "color": "#4338CA", "action": {"type": "uri", "label": "開啟雲端儀表板", "uri": DASHBOARD_URL}}
            ]
        }
    }

# ==========================================
# 8. 智能持股輪動卡片
# ==========================================
def build_rotation_bubble(pair: dict) -> dict:
    from_s = pair["from_stock"]
    to_s = pair["to_stock"]
    curr_sym = "$" if pair["currency"] == "USD" else "NT$"
    freed_str = f"{pair['freed_capital']:,.1f}" if pair["currency"] == "USD" else f"{pair['freed_capital']:,.0f}"

    return {
        "type": "bubble", "size": "kilo",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#4C1D95", "paddingAll": "16px",
            "contents": [
                {
                    "type": "box", "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "🔄 智能資金輪動建議", "weight": "bold", "color": "#FFFFFF", "size": "md", "flex": 4},
                        {"type": "text", "text": f"{pair['currency']}部位", "color": "#DDD6FE", "size": "xs", "align": "end", "flex": 2}
                    ]
                },
                {"type": "text", "text": "汰弱留強 · 鎖定利潤換軌高性價比標的", "color": "#C4B5FD", "size": "xs", "margin": "xs"}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "backgroundColor": "#0F172A", "paddingAll": "16px", "spacing": "sm",
            "contents": [
                {"type": "text", "text": "🔴 建議調節轉出：", "color": "#F87171", "weight": "bold", "size": "xs"},
                {
                    "type": "box", "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": f"{from_s['name']} ({from_s['ticker']})", "color": "#FFFFFF", "weight": "bold", "size": "sm"},
                        {"type": "text", "text": from_s["signal_badge"], "color": from_s["badge_color"], "size": "xs", "align": "end"}
                    ]
                },
                {
                    "type": "box", "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": f"持倉: {from_s['shares']} 股 ({from_s['broker']})", "color": "#94A3B8", "size": "xxs"},
                        {"type": "text", "text": f"預估釋出: {curr_sym}{freed_str}", "color": "#FCA5A5", "size": "xxs", "align": "end"}
                    ]
                },
                {"type": "separator", "color": "#334155", "margin": "md"},
                {"type": "text", "text": "🟢 最佳換軌轉進首選：", "color": "#34D399", "weight": "bold", "size": "xs", "margin": "sm"},
                {
                    "type": "box", "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": f"{to_s['name']} ({to_s['ticker']})", "color": "#FFFFFF", "weight": "bold", "size": "sm"},
                        {"type": "text", "text": to_s["signal_badge"], "color": to_s["badge_color"], "size": "xs", "align": "end"}
                    ]
                },
                {
                    "type": "box", "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": f"現價: {curr_sym}{to_s['price']} | 估值: {curr_sym}{to_s['dynamic_fair']}", "color": "#94A3B8", "size": "xxs"},
                        {"type": "text", "text": f"折價 {abs(to_s['diff_pct']):.1f}%", "color": "#6EE7B7", "weight": "bold", "size": "xxs", "align": "end"}
                    ]
                },
                {
                    "type": "box", "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": f"動態停損: {curr_sym}{to_s['stop_loss']} (2.0×ATR)", "color": "#94A3B8", "size": "xxs"},
                        {"type": "text", "text": f"風報比 1:{to_s['rr_ratio'] or '佳'}", "color": "#6EE7B7", "size": "xxs", "align": "end"}
                    ]
                },
                {"type": "separator", "color": "#334155", "margin": "md"},
                {
                    "type": "box", "layout": "vertical", "backgroundColor": "#1E293B", "paddingAll": "12px", "cornerRadius": "8px", "margin": "md",
                    "contents": [
                        {"type": "text", "text": "🎯 戰略換軌方針：", "color": "#A78BFA", "weight": "bold", "size": "xs"},
                        {"type": "text", "text": f"{from_s['name']} 估值偏高或跌破短期均線進入防禦期；建議將部位資金轉進折價幅度達 {abs(to_s['diff_pct']):.1f}%、風報比絕佳的 {to_s['name']}，實現鎖利並放大期望值！", "color": "#F8FAFC", "size": "xxs", "wrap": True, "margin": "xs"}
                    ]
                }
            ]
        },
        "footer": {
            "type": "box", "layout": "horizontal", "backgroundColor": "#1E293B", "paddingAll": "10px",
            "contents": [
                {"type": "button", "style": "primary", "height": "sm", "color": "#6D28D9", "action": {"type": "uri", "label": "開啟估值儀表板", "uri": DASHBOARD_URL}}
            ]
        }
    }

# ==========================================
# 9. 技術分析與【ATR 動態風控停損】核心
# ==========================================
def calculate_risk_reward(price: float, fair_val: float, ma20: float, low_10d: float, atr: float) -> Tuple[float, Optional[float]]:
    """
    專業動態波動停損 (Chandelier / ATR Volatility Stop):
    - 依據個股 14 日真實波幅 (ATR)，動態給予 2.0 倍 ATR 的容忍緩衝區
    - 結合近期 10 日結構低點，有效防範主力洗盤假跌破與插針
    """
    # 2.0 倍 ATR 基準保護位
    volatility_stop = price - (2.0 * atr)
    
    # 結合 10 日低點結構防守 (取兩者中更穩健的支撐位，並避免過度貼近或過度遠離)
    stop_loss = round(min(low_10d, volatility_stop), 2)
    if stop_loss >= price or stop_loss <= (price * 0.75):
        stop_loss = round(price - (2.0 * atr), 2)
    if stop_loss >= price:
        stop_loss = round(price * 0.95, 2)

    risk = price - stop_loss
    reward = fair_val - price
    rr_ratio = round(reward / risk, 1) if risk > 0 and reward > 0 else None
    return stop_loss, rr_ratio

def analyze_stock(ticker: str) -> Optional[dict]:
    try:
        stock = yf.Ticker(ticker)
        df = stock.history(period="1y")
        if df.empty or len(df) < 25: return None
        
        close = df['Close'].dropna()
        high = df['High'].dropna()
        low = df['Low'].dropna()
        if close.empty or len(close) < 25: return None

        curr_price = round(float(close.iloc[-1]), 2)
        prev_price = round(float(close.iloc[-2]), 2)
        low_10d = round(float(low.tail(10).min()), 2)

        # 20MA
        ma20_s = close.rolling(20).mean()
        curr_ma20 = round(float(ma20_s.iloc[-1]), 2)
        prev_ma20 = round(float(ma20_s.iloc[-2]), 2)

        # RSI(14)
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, 0.0001)
        rsi = round(float((100 - (100 / (1 + rs))).iloc[-1]), 1)

        # ATR(14) 真實波動區間計算
        prev_close = close.shift(1)
        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr_series = tr.rolling(14).mean()
        curr_atr = round(float(atr_series.iloc[-1]), 2) if pd.notna(atr_series.iloc[-1]) else round(curr_price * 0.025, 2)
        atr_pct = round((curr_atr / curr_price) * 100, 1) if curr_price > 0 else 2.5

        try: info = stock.info
        except Exception: info = {}

        return {
            "price": curr_price, "prev_price": prev_price,
            "ma20": curr_ma20, "prev_ma20": prev_ma20,
            "rsi": rsi, "low_10d": low_10d,
            "atr": curr_atr, "atr_pct": atr_pct,
            "info": info
        }
    except Exception:
        return None

def evaluate_decision(item: dict, data: dict, market_regime: dict, macro_data: dict, dynamic_fair: float, val_source: str) -> dict:
    price = data["price"]
    fair_val = dynamic_fair
    diff_pct = ((price - fair_val) / fair_val) * 100
    buy_threshold_pct = - (item["mos_buy"] * 100)
    sell_threshold_pct = item["expensive_sell"] * 100

    vix_val = macro_data.get("VIX", {}).get("price", 15.0)
    nq_chg = macro_data.get("NQ_F", {}).get("chg_pct", 0.0)
    tsm_chg = macro_data.get("TSM", {}).get("chg_pct", 0.0)

    score = 0.0
    if diff_pct <= buy_threshold_pct: score += 2.0
    elif diff_pct < 0: score += 0.5
    elif diff_pct >= sell_threshold_pct: score -= 2.0
    elif diff_pct > 10: score -= 0.5

    if data["prev_price"] <= data["prev_ma20"] and price > data["ma20"]: score += 1.5
    elif price > data["ma20"]: score += 0.5
    if data["prev_price"] >= data["prev_ma20"] and price < data["ma20"]: score -= 1.5
    elif price < data["ma20"]: score -= 0.5

    if data["rsi"] <= 30: score += 1.0
    elif data["rsi"] >= 75: score -= 1.0

    macro_warnings = []
    if vix_val >= 24.0 or not market_regime["is_bull"]:
        if score >= 2.0:
            score = 1.2
            macro_warnings.append("⚠️ [大盤偏空] 買進降級為少量試單")

    is_night_crash = (nq_chg <= -1.5) or (item["currency"] == "TWD" and tsm_chg <= -2.0)
    is_near_ma20 = (price - data["ma20"]) / data["ma20"] <= 0.015
    if is_night_crash:
        if diff_pct > 10.0 or is_near_ma20:
            score -= 1.5
            macro_warnings.append(f"⚡ [夜盤急跌避險] 期指/ADR重挫({tsm_chg:+.1f}%)，觸發提前減碼！")

    if vix_val >= 28.0:
        if diff_pct >= 15.0:
            score -= 1.8
            macro_warnings.append(f"🚨 [恐慌警戒] VIX達{vix_val}，高估值部位強制分批停利！")
        elif diff_pct >= 0:
            score -= 1.0

    # 引入 ATR 計算自適應動態停損
    stop_loss, rr_ratio = calculate_risk_reward(price, fair_val, data["ma20"], data["low_10d"], data["atr"])

    curr_sym = "$" if item["currency"] == "USD" else "NT$"
    if score >= 2.5:
        signal_badge, badge_color, header_color = "🔥 立即買進", "#10B981", "#064E3B"
        base_advice = f"【右側建倉買點】落入安全邊際且翻多。防守停損 {curr_sym}{stop_loss} (2.0×ATR動態防守)，風報比 1:{rr_ratio or '優'}。"
    elif score >= 1.0:
        signal_badge, badge_color, header_color = "🟢 逢低加碼", "#34D399", "#065F46"
        base_advice = f"【性價比充足】回測支撐有守，可分批承接。防守停損 {curr_sym}{stop_loss} (2.0×ATR動態防守)。"
    elif score <= -2.5:
        signal_badge, badge_color, header_color = "🔴 立即賣出", "#EF4444", "#7F1D1D"
        base_advice = "【估值嚴重透支】價格大幅高估，強烈建議分批停利或掛設移動停利單以鎖定獲利。"
    elif score <= -1.0:
        signal_badge, badge_color, header_color = "🟠 建議減碼", "#F97316", "#7C2D12"
        base_advice = "【轉弱避險防守】摜破防線或受夜盤/總經利空壓抑，建議多單部分減碼或暫停加碼。"
    else:
        signal_badge, badge_color, header_color = "⚪ 觀望續抱", "#94A3B8", "#1E293B"
        base_advice = "【常態區間】未達顯著買賣標準，持股續抱，耐心等待趨勢明朗。"

    prefix = " | ".join(macro_warnings) + "\n" if macro_warnings else ""
    final_advice = prefix + base_advice

    return {
        "score": round(score, 1), "diff_pct": diff_pct,
        "signal_badge": signal_badge, "badge_color": badge_color,
        "header_color": header_color, "action_advice": final_advice,
        "stop_loss": stop_loss, "rr_ratio": rr_ratio,
        "dynamic_fair": dynamic_fair, "val_source": val_source
    }

def build_stock_bubble(data: dict, market_regime: dict) -> dict:
    diff_text = f"折價 {abs(data['diff_pct']):.1f}%" if data['diff_pct'] < 0 else f"溢價 {data['diff_pct']:.1f}%"
    yahoo_chart_url = f"https://finance.yahoo.com/quote/{data['ticker']}"
    rr_text = f"1 : {data['rr_ratio']}" if data['rr_ratio'] else "N/A"

    holdings_section = []
    if data["shares"] > 0 and data["cost_price"] > 0:
        curr_val = data["shares"] * data["price"]
        cost_val = data["shares"] * data["cost_price"]
        pnl = curr_val - cost_val
        pnl_pct = (pnl / cost_val) * 100 if cost_val > 0 else 0.0
        pnl_color = "#34D399" if pnl >= 0 else "#F87171"
        pnl_str = f"{pnl:+,.1f}" if data['currency'] == "USD" else f"{pnl:+,.0f}"

        holdings_section = [
            {"type": "separator", "color": "#334155", "margin": "sm"},
            {
                "type": "box", "layout": "horizontal", "margin": "sm",
                "contents": [
                    {"type": "text", "text": f"持倉 ({data['broker']})", "color": "#94A3B8", "size": "xs"},
                    {"type": "text", "text": f"{data['shares']} 股 @ {data['curr_symbol']}{data['cost_price']}", "color": "#CBD5E1", "size": "xs", "align": "end"}
                ]
            },
            {
                "type": "box", "layout": "horizontal",
                "contents": [
                    {"type": "text", "text": "未實現損益", "color": "#94A3B8", "size": "xs"},
                    {"type": "text", "text": f"{data['curr_symbol']}{pnl_str} ({pnl_pct:+.1f}%)", "color": pnl_color, "weight": "bold", "size": "xs", "align": "end"}
                ]
            }
        ]

    return {
        "type": "bubble", "size": "kilo",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": data["header_color"], "paddingAll": "16px",
            "contents": [
                {
                    "type": "box", "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": data["name"], "weight": "bold", "color": "#FFFFFF", "size": "md", "flex": 3},
                        {"type": "text", "text": data["signal_badge"], "weight": "bold", "color": data["badge_color"], "size": "sm", "align": "end", "flex": 3}
                    ]
                },
                {
                    "type": "box", "layout": "horizontal", "margin": "xs",
                    "contents": [
                        {"type": "text", "text": f"{data['ticker']} · {diff_text}", "color": "#CBD5E1", "size": "xs", "flex": 3},
                        {"type": "text", "text": market_regime["label"], "color": market_regime["is_bull"] and "#A7F3D0" or "#FCA5A5", "size": "xxs", "align": "end", "flex": 2}
                    ]
                }
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "backgroundColor": "#0F172A", "paddingAll": "16px", "spacing": "sm",
            "contents": [
                {
                    "type": "box", "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "當前市價", "color": "#94A3B8", "size": "xs"},
                        {"type": "text", "text": f"{data['curr_symbol']}{data['price']}", "color": "#FFFFFF", "weight": "bold", "size": "sm", "align": "end"}
                    ]
                },
                {
                    "type": "box", "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": f"合理價 ({data['val_source']})", "color": "#38BDF8", "size": "xs"},
                        {"type": "text", "text": f"{data['curr_symbol']}{data['dynamic_fair']}", "color": "#38BDF8", "weight": "bold", "size": "xs", "align": "end"}
                    ]
                },
                {
                    "type": "box", "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "均線與指標", "color": "#94A3B8", "size": "xs"},
                        {"type": "text", "text": f"20MA: {data['ma20']} | RSI: {data['rsi']}", "color": "#94A3B8", "size": "xs", "align": "end"}
                    ]
                },
                # 新增 ATR 波動度與自適應動態停損
                {
                    "type": "box", "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "14日波幅 (ATR)", "color": "#A78BFA", "size": "xs"},
                        {"type": "text", "text": f"{data['curr_symbol']}{data['atr']} (日震幅 {data['atr_pct']}%)", "color": "#C4B5FD", "size": "xs", "align": "end"}
                    ]
                },
                {
                    "type": "box", "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "動態停損 (2.0×ATR)", "color": "#F87171", "size": "xs"},
                        {"type": "text", "text": f"{data['curr_symbol']}{data['stop_loss']} (風報比 {rr_text})", "color": "#FCA5A5", "weight": "bold", "size": "xs", "align": "end"}
                    ]
                },
                *holdings_section,
                {"type": "separator", "color": "#334155", "margin": "md"},
                {
                    "type": "box", "layout": "vertical", "backgroundColor": "#1E293B", "paddingAll": "12px", "cornerRadius": "8px", "margin": "md",
                    "contents": [
                        {"type": "text", "text": "🎯 立即操作方針：", "color": "#38BDF8", "weight": "bold", "size": "xs"},
                        {"type": "text", "text": data["action_advice"], "color": "#F8FAFC", "size": "xxs", "wrap": True, "margin": "xs"}
                    ]
                }
            ]
        },
        "footer": {
            "type": "box", "layout": "horizontal", "backgroundColor": "#1E293B", "spacing": "sm", "paddingAll": "10px",
            "contents": [
                {"type": "button", "style": "secondary", "height": "sm", "color": "#334155", "action": {"type": "uri", "label": "即時線圖", "uri": yahoo_chart_url}},
                {"type": "button", "style": "primary", "height": "sm", "color": "#0284C7", "action": {"type": "uri", "label": "估值儀表板", "uri": DASHBOARD_URL}}
            ]
        }
    }

# ==========================================
# 10. 推播發送模組 (自動分頁：支援超過 10 張卡片)
# ==========================================
def push_line_flex(token: str, user_id: str, bubbles: List[dict], alt_text: str):
    if not token or not user_id or not bubbles: return
    url = "https://api.line.me/v2/bot/message/push"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    
    messages = []
    chunk_size = 10
    for i in range(0, len(bubbles), chunk_size):
        chunk = bubbles[i:i + chunk_size]
        messages.append({
            "type": "flex",
            "altText": alt_text,
            "contents": {"type": "carousel", "contents": chunk}
        })
        if len(messages) >= 5:
            break

    payload = {
        "to": user_id,
        "messages": messages
    }
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=15)
        if res.status_code == 200:
            print(f"✅ LINE Flex 推播成功（共 {len(bubbles)} 檔卡片，拆分為 {len(messages)} 則訊息送達）！")
        else:
            print(f"❌ LINE 推播失敗: {res.status_code} - {res.text}")
    except Exception as e:
        print(f"LINE 請求異常: {e}")

# ==========================================
# 11. 主排程流程
# ==========================================
def main():
    print(f"===== 啟動多源智能投研系統 (模式: {RUN_MODE} | 強制推播: {FORCE_NOTIFY}) =====")

    watchlist = load_portfolio_watchlist()
    if not watchlist:
        print("❌ 無持股監控標的，結束執行。")
        return

    macro_data = fetch_global_macro_snapshot()
    vix_val = macro_data.get("VIX", {}).get("price", 15.0)

    if RUN_MODE == "MORNING":
        print("☀️ 正在建構【晨間全球前瞻與夜盤快報】...")
        morning_bubble = build_morning_brief_bubble(macro_data)
        push_line_flex(LINE_CHANNEL_ACCESS_TOKEN, LINE_USER_ID, [morning_bubble], "☀️ 晨間全球前瞻與夜盤快報已送達！")
        return

    twse_data = fetch_twse_official_metrics()
    
    def check_market_regime(currency: str, vix: float) -> dict:
        benchmark = "SPY" if currency == "USD" else "^TWII"
        try:
            df = yf.Ticker(benchmark).history(period="1y")
            close = df['Close'].dropna()
            curr = float(close.iloc[-1])
            ma120 = float(close.rolling(120).mean().iloc[-1])
            is_bull = curr >= ma120 and (vix < 25)
            label = "🟢 大盤多頭" if is_bull else ("⚠️ 恐慌高壓" if vix >= 25 else "⚠️ 大盤偏空")
            return {"is_bull": is_bull, "label": label, "benchmark": benchmark, "price": round(curr, 2)}
        except Exception:
            return {"is_bull": True, "label": "大盤數據正常", "benchmark": benchmark}

    regimes = {
        "USD": check_market_regime("USD", vix_val),
        "TWD": check_market_regime("TWD", vix_val)
    }

    state_cache = load_state_cache()
    new_state_cache = dict(state_cache)
    
    actionable_cards = []
    evaluated_pool = []
    
    summary_stats = {
        "usd_val": 0.0, "usd_cost": 0.0, "usd_pnl": 0.0, "usd_pnl_pct": 0.0,
        "twd_val": 0.0, "twd_cost": 0.0, "twd_pnl": 0.0, "twd_pnl_pct": 0.0
    }

    for item in watchlist:
        currency = item["currency"]
        ticker = item["ticker"]
        name = item["name"]
        curr_symbol = "$" if currency == "USD" else "NT$"

        data = analyze_stock(ticker)
        if not data:
            print(f"⚠️ [{name} ({ticker})] 無法取得歷史行情數據，略過。")
            continue

        price = data["price"]
        shares = item["shares"]
        cost_price = item["cost_price"]

        if pd.notna(price) and price > 0 and pd.notna(shares) and shares > 0 and pd.notna(cost_price) and cost_price > 0:
            pos_val = shares * price
            pos_cost = shares * cost_price
            if currency == "USD":
                summary_stats["usd_val"] += pos_val
                summary_stats["usd_cost"] += pos_cost
            else:
                summary_stats["twd_val"] += pos_val
                summary_stats["twd_cost"] += pos_cost

        if RUN_MODE in ["TWD", "USD"] and currency != RUN_MODE:
            continue

        dynamic_fair, val_source = calculate_dynamic_fair_value(item, data.get("info", {}), twse_data, price)
        market_regime = regimes[currency]
        decision = evaluate_decision(item, data, market_regime, macro_data, dynamic_fair, val_source)

        current_signal = decision["signal_badge"]
        last_signal = state_cache.get(ticker)
        new_state_cache[ticker] = current_signal

        print(f"[{name}] 市價: {curr_symbol}{price} | ATR: {data['atr']} ({data['atr_pct']}%) | 合理價: {curr_symbol}{dynamic_fair} | 訊號: {current_signal}")

        card_info = {
            "name": name, "ticker": ticker, "currency": currency, "broker": item["broker"],
            "shares": shares, "cost_price": cost_price, "curr_symbol": curr_symbol,
            "price": price, "ma20": data["ma20"], "rsi": data["rsi"],
            "atr": data["atr"], "atr_pct": data["atr_pct"],
            **decision
        }
        evaluated_pool.append(card_info)

        is_state_changed = (current_signal != last_signal)
        should_alert = is_state_changed or FORCE_NOTIFY

        if should_alert:
            actionable_cards.append(build_stock_bubble(card_info, market_regime))

    if summary_stats["usd_cost"] > 0:
        summary_stats["usd_pnl"] = summary_stats["usd_val"] - summary_stats["usd_cost"]
        summary_stats["usd_pnl_pct"] = (summary_stats["usd_pnl"] / summary_stats["usd_cost"]) * 100
    if summary_stats["twd_cost"] > 0:
        summary_stats["twd_pnl"] = summary_stats["twd_val"] - summary_stats["twd_cost"]
        summary_stats["twd_pnl_pct"] = (summary_stats["twd_pnl"] / summary_stats["twd_cost"]) * 100

    # 資金輪動計算
    rotation_bubbles = []
    sell_candidates = [s for s in evaluated_pool if s["shares"] > 0 and s["score"] <= -1.0]
    buy_candidates = [b for b in evaluated_pool if b["score"] >= 1.5]

    for sell_item in sell_candidates:
        currency = sell_item["currency"]
        valid_targets = [b for b in buy_candidates if b["currency"] == currency and b["ticker"] != sell_item["ticker"]]
        if valid_targets:
            best_target = sorted(valid_targets, key=lambda x: (x["score"], -x["diff_pct"]), reverse=True)[0]
            pair_data = {
                "from_stock": sell_item,
                "to_stock": best_target,
                "freed_capital": sell_item["shares"] * sell_item["price"],
                "currency": currency
            }
            rotation_bubbles.append(build_rotation_bubble(pair_data))

    # 組裝推播
    if actionable_cards or rotation_bubbles:
        summary_bubble = build_portfolio_summary_bubble(summary_stats)
        final_bubbles = [summary_bubble] + rotation_bubbles + actionable_cards
        push_line_flex(LINE_CHANNEL_ACCESS_TOKEN, LINE_USER_ID, final_bubbles, f"🚨 投資資產與持股分析報告：共 {len(actionable_cards)} 檔標的最新資訊！")
    else:
        print("💡 所有標的狀態未變動，無須打擾。")

    save_state_cache(new_state_cache)
    print("===== 掃描流程完畢 =====")

if __name__ == "__main__":
    main()
