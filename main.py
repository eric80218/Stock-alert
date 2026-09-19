import os
import sys
import json
import requests
import pandas as pd
import yfinance as yf
from typing import Dict, List, Optional, Tuple

# ==========================================
# 1. 系統設定、憑證與資料源 API Key
# ==========================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_USER_ID = os.getenv("LINE_USER_ID", "")

# 外部專業數據源金鑰 (選填，未填會自動降級回退到 Yahoo/TWSE 免費數據)
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")

RUN_MODE = os.getenv("RUN_MODE", "ALL").upper()
FORCE_NOTIFY = os.getenv("FORCE_NOTIFY", "false").lower() == "true"

DASHBOARD_URL = "https://eric80218.github.io/Stock-alert/"
CACHE_FILE = "state_cache.json"

# ==========================================
# 2. 專屬持股監控清單 (配置專屬動態估值模型)
# ==========================================
PORTFOLIO_WATCHLIST = [
    # --- 美股部位 (自動追蹤 Finnhub/Yahoo 華爾街投行共識目標價) ---
    {"ticker": "AMAT", "name": "應用材料", "currency": "USD", "base_fair": 210.0, "val_model": "ANALYST", "mos_buy": 0.15, "expensive_sell": 0.25},
    {"ticker": "NVDA", "name": "輝達", "currency": "USD", "base_fair": 135.0, "val_model": "ANALYST", "mos_buy": 0.15, "expensive_sell": 0.30},
    {"ticker": "COST", "name": "好市多", "currency": "USD", "base_fair": 850.0, "val_model": "ANALYST", "mos_buy": 0.10, "expensive_sell": 0.20},
    {"ticker": "BRK-B", "name": "波克夏 B", "currency": "USD", "base_fair": 450.0, "val_model": "PE_EPS", "mos_buy": 0.10, "expensive_sell": 0.20},
    {"ticker": "LLY", "name": "禮來製藥", "currency": "USD", "base_fair": 880.0, "val_model": "ANALYST", "mos_buy": 0.15, "expensive_sell": 0.25},
    {"ticker": "VRT", "name": "維諦技術", "currency": "USD", "base_fair": 110.0, "val_model": "ANALYST", "mos_buy": 0.20, "expensive_sell": 0.35},
    {"ticker": "INTC", "name": "英特爾", "currency": "USD", "base_fair": 24.0, "val_model": "ANALYST", "mos_buy": 0.15, "expensive_sell": 0.25},

    # --- 台股部位 (TWSE 官方殖利率評價 / 動態本益比模型) ---
    {"ticker": "0050.TW", "name": "元大台灣50", "currency": "TWD", "base_fair": 185.0, "val_model": "BASE", "mos_buy": 0.10, "expensive_sell": 0.20},
    {"ticker": "2337.TW", "name": "旺宏", "currency": "TWD", "base_fair": 28.0, "val_model": "PE_EPS", "mos_buy": 0.15, "expensive_sell": 0.25},
    {"ticker": "2002.TW", "name": "中鋼", "currency": "TWD", "base_fair": 23.5, "val_model": "DIVIDEND", "mos_buy": 0.10, "expensive_sell": 0.20},
    {"ticker": "1232.TW", "name": "大統益", "currency": "TWD", "base_fair": 155.0, "val_model": "DIVIDEND", "mos_buy": 0.10, "expensive_sell": 0.20},
    {"ticker": "1904.TW", "name": "正隆", "currency": "TWD", "base_fair": 29.0, "val_model": "DIVIDEND", "mos_buy": 0.12, "expensive_sell": 0.25},
    {"ticker": "2616.TW", "name": "山隆", "currency": "TWD", "base_fair": 31.0, "val_model": "DIVIDEND", "mos_buy": 0.10, "expensive_sell": 0.20}
]

# ==========================================
# 3. 台灣證交所 (TWSE) 官方即時權威指標快取
# ==========================================
def fetch_twse_official_metrics() -> dict:
    """從台灣證券交易所 OpenAPI 取得全市場最新個股本益比與殖利率 (免Token)"""
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
    except Exception as e:
        print(f"⚠️ TWSE OpenAPI 讀取略過: {e}")
    return twse_dict

# ==========================================
# 4. 全自動動態估值引擎 (Dynamic Valuation Engine)
# ==========================================
def calculate_dynamic_fair_value(item: dict, yf_info: dict, twse_data: dict, current_price: float) -> Tuple[float, str]:
    """
    依據股票屬性自動計算最新合理價：
    1. ANALYST: Finnhub 華爾街共識目標價 -> 備援 Yahoo 目標價
    2. DIVIDEND: TWSE 官方殖利率 (回推 5% 殖利率合理線) -> 備援配息折算
    3. PE_EPS: 最新四季 EPS * 合理本益比乘數
    4. BASE: ETF 或無顯著指標標的採用基準錨定
    """
    ticker = item["ticker"]
    base_fair = item["base_fair"]
    model = item["val_model"]

    # --- 模型 1：華爾街投行共識目標價 (美股最適用) ---
    if model == "ANALYST":
        # A. 優先嘗試 Finnhub API
        if FINNHUB_API_KEY:
            try:
                fh_url = f"https://finnhub.io/api/v1/stock/price-target?symbol={ticker}&token={FINNHUB_API_KEY}"
                res = requests.get(fh_url, timeout=5).json()
                fh_target = res.get("targetMean") or res.get("targetMedian")
                if fh_target and fh_target > 0:
                    # 安全護欄：偏離基準 0.5x ~ 2.0x
                    if 0.5 * base_fair <= fh_target <= 2.0 * base_fair:
                        return round(float(fh_target), 2), "Finnhub法人目標"
            except Exception:
                pass
        
        # B. 備援嘗試 Yahoo Finance 內建的法人目標價
        try:
            yf_target = yf_info.get("targetMeanPrice") or yf_info.get("targetMedianPrice")
            if yf_target and 0.5 * base_fair <= yf_target <= 2.0 * base_fair:
                return round(float(yf_target), 2), "Yahoo法人目標"
        except Exception:
            pass

    # --- 模型 2：台股官方現金殖利率回推法 (5% 合理殖利率線) ---
    elif model == "DIVIDEND":
        clean_code = ticker.replace(".TW", "")
        # A. 優先使用台灣證券交易所官方數據
        tw_metric = twse_data.get(clean_code)
        if tw_metric and tw_metric.get("yield"):
            curr_yield = tw_metric["yield"]  # 例如 4.5 (%)
            if curr_yield > 0.5 and current_price > 0:
                annual_dividend = current_price * (curr_yield / 100.0)
                # 以 5.0% 現金殖利率推算合理價防守線
                calc_val = annual_dividend / 0.05
                if 0.5 * base_fair <= calc_val <= 1.8 * base_fair:
                    return round(calc_val, 2), "證交所殖利率折算"
        
        # B. 備援使用 Yahoo 股息率折算
        div_rate = yf_info.get("dividendRate") or 0.0
        if div_rate > 0:
            calc_val = div_rate / 0.05
            if 0.5 * base_fair <= calc_val <= 1.8 * base_fair:
                return round(calc_val, 2), "股息5%折算法"

    # --- 模型 3：動態本益比與 EPS 模型 ---
    elif model == "PE_EPS":
        eps = yf_info.get("trailingEps") or yf_info.get("forwardEps")
        pe = yf_info.get("trailingPE")
        if eps and eps > 0:
            # 限制合理本益比在 12x ~ 28x 之間
            applied_pe = min(28.0, max(12.0, pe)) if pe else (base_fair / eps)
            calc_val = eps * applied_pe
            if 0.5 * base_fair <= calc_val <= 1.8 * base_fair:
                return round(calc_val, 2), "最新EPS本益比"

    # 安全平滑退回：基準底線保護
    return base_fair, "基準錨定"

# ==========================================
# 5. 狀態記憶模組 (避免重複推播)
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
# 6. 夜盤與全球總經數據獲取
# ==========================================
def fetch_global_macro_snapshot() -> dict:
    indicators = {
        "TSM": "TSM", "NQ_F": "NQ=F", "ES_F": "ES=F",
        "VIX": "^VIX", "TNX": "^TNX", "DXY": "DX-Y.NYB"
    }
    data = {}
    for key, symbol in indicators.items():
        try:
            t = yf.Ticker(symbol)
            hist = t.history(period="5d")
            if len(hist) >= 2:
                curr = float(hist['Close'].iloc[-1])
                prev = float(hist['Close'].iloc[-2])
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
        market_mood = "🟢 多方強勢開出"
        mood_color = "#10B981"
        header_bg = "#064E3B"
        advice = "台積電 ADR 與美指期指表現亮眼，台股早盤高機率開高。持股續抱，嚴防急拉追高，逢回調支撐可分批布局。"
    elif score <= -2:
        market_mood = "🔴 恐慌偏空震盪"
        mood_color = "#EF4444"
        header_bg = "#7F1D1D"
        advice = f"VIX 恐慌指數攀升至 {vix_val}，避險情緒高漲，開盤恐面臨獲利回吐賣壓。建議多看少做，保留充足現金，切忌盲目摸底。"
    else:
        market_mood = "🟡 平衡震盪整理"
        mood_color = "#F59E0B"
        header_bg = "#78350F"
        advice = "夜盤與總經指標處於多空拉鋸，預估開盤平開震盪。按既定合理價與分批紀律操作即可，無須過度反應短線波動。"

    def fmt_chg(val):
        return f"{val:+.2f}%" if val else "0.00%"

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
                {
                    "type": "box", "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "美元指數 (DXY)", "color": "#94A3B8", "size": "xs"},
                        {"type": "text", "text": f"{dxy.get('price')}", "color": "#CBD5E1", "size": "xs", "align": "end"}
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
# 7. 大盤環境與風險濾網
# ==========================================
def check_market_regime(currency: str, vix_val: float) -> dict:
    benchmark_ticker = "SPY" if currency == "USD" else "^TWII"
    try:
        df = yf.Ticker(benchmark_ticker).history(period="1y")
        if len(df) < 120:
            return {"is_bull": True, "label": "常態多頭", "benchmark": benchmark_ticker}
        close = df['Close']
        curr = float(close.iloc[-1])
        ma120 = float(close.rolling(120).mean().iloc[-1])
        is_bull = curr >= ma120 and (vix_val < 25)
        label = "🟢 大盤多頭" if is_bull else ("⚠️ 恐慌高壓" if vix_val >= 25 else "⚠️ 大盤偏空")
        return {
            "is_bull": is_bull, "label": label, "benchmark": benchmark_ticker, "price": round(curr, 2)
        }
    except Exception:
        return {"is_bull": True, "label": "大盤數據正常", "benchmark": benchmark_ticker}

# ==========================================
# 8. 技術分析與風控計算
# ==========================================
def calculate_risk_reward(price: float, fair_val: float, ma20: float, low_10d: float) -> Tuple[float, Optional[float]]:
    stop_loss = round(min(low_10d, ma20 * 0.97), 2)
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
        
        close = df['Close']
        curr_price = round(float(close.iloc[-1]), 2)
        prev_price = round(float(close.iloc[-2]), 2)
        low_10d = round(float(df['Low'].tail(10).min()), 2)

        ma20_s = close.rolling(20).mean()
        curr_ma20 = round(float(ma20_s.iloc[-1]), 2)
        prev_ma20 = round(float(ma20_s.iloc[-2]), 2)

        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, 0.0001)
        rsi = round(float((100 - (100 / (1 + rs))).iloc[-1]), 1)

        # 讀取財務資訊 (用於動態估值)
        try:
            info = stock.info
        except Exception:
            info = {}

        return {
            "price": curr_price, "prev_price": prev_price,
            "ma20": curr_ma20, "prev_ma20": prev_ma20,
            "rsi": rsi, "low_10d": low_10d, "info": info
        }
    except Exception:
        return None

# ==========================================
# 9. 決策評估核心 (結合動態合理價與夜盤總經)
# ==========================================
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

    # 1. 基本面安全邊際
    if diff_pct <= buy_threshold_pct: score += 2.0
    elif diff_pct < 0: score += 0.5
    elif diff_pct >= sell_threshold_pct: score -= 2.0
    elif diff_pct > 10: score -= 0.5

    # 2. 技術面 20MA
    if data["prev_price"] <= data["prev_ma20"] and price > data["ma20"]: score += 1.5
    elif price > data["ma20"]: score += 0.5
    if data["prev_price"] >= data["prev_ma20"] and price < data["ma20"]: score -= 1.5
    elif price < data["ma20"]: score -= 0.5

    # 3. RSI
    if data["rsi"] <= 30: score += 1.0
    elif data["rsi"] >= 75: score -= 1.0

    # 4. 夜盤與總經調節
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
            macro_warnings.append(f"⚡ [夜盤急跌避險] 期指/ADR重挫({tsm_chg:+.1f}%)，位階脆弱觸發提前減碼！")

    if vix_val >= 28.0:
        if diff_pct >= 15.0:
            score -= 1.8
            macro_warnings.append(f"🚨 [恐慌警戒] VIX達{vix_val}，高估值部位強制分批停利！")
        elif diff_pct >= 0:
            score -= 1.0

    stop_loss, rr_ratio = calculate_risk_reward(price, fair_val, data["ma20"], data["low_10d"])

    if score >= 2.5:
        signal_badge = "🔥 立即買進"
        badge_color = "#10B981"
        header_color = "#064E3B"
        base_advice = f"【右側建倉買點】落入安全邊際且翻多。防守停損 {item['currency']=='USD' and '$' or 'NT$'}{stop_loss}，風報比 1:{rr_ratio or '優'}。"
    elif score >= 1.0:
        signal_badge = "🟢 逢低加碼"
        badge_color = "#34D399"
        header_color = "#065F46"
        base_advice = f"【性價比充足】回測支撐有守，可分批承接。防守停損 {item['currency']=='USD' and '$' or 'NT$'}{stop_loss}。"
    elif score <= -2.5:
        signal_badge = "🔴 立即賣出"
        badge_color = "#EF4444"
        header_color = "#7F1D1D"
        base_advice = "【估值嚴重透支】價格大幅高估，強烈建議分批停利或掛設移動停利以守住利潤。"
    elif score <= -1.0:
        signal_badge = "🟠 建議減碼"
        badge_color = "#F97316"
        header_color = "#7C2D12"
        base_advice = "【轉弱避險防守】摜破防線或受夜盤/總經利空壓抑，建議多單部分減碼或暫停加碼。"
    else:
        signal_badge = "⚪ 觀望續抱"
        badge_color = "#94A3B8"
        header_color = "#1E293B"
        base_advice = "【常態區間】未達顯著買賣標準，持股續抱。"

    prefix = " | ".join(macro_warnings) + "\n" if macro_warnings else ""
    final_advice = prefix + base_advice

    return {
        "score": round(score, 1), "diff_pct": diff_pct,
        "signal_badge": signal_badge, "badge_color": badge_color,
        "header_color": header_color, "action_advice": final_advice,
        "stop_loss": stop_loss, "rr_ratio": rr_ratio,
        "dynamic_fair": dynamic_fair, "val_source": val_source,
        "is_active_signal": (score >= 1.0 or score <= -1.0)
    }

def build_stock_bubble(data: dict, market_regime: dict) -> dict:
    diff_text = f"折價 {abs(data['diff_pct']):.1f}%" if data['diff_pct'] < 0 else f"溢價 {data['diff_pct']:.1f}%"
    yahoo_chart_url = f"https://finance.yahoo.com/quote/{data['ticker']}"
    rr_text = f"1 : {data['rr_ratio']}" if data['rr_ratio'] else "N/A"

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
                {
                    "type": "box", "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "防守停損點", "color": "#F87171", "size": "xs"},
                        {"type": "text", "text": f"{data['curr_symbol']}{data['stop_loss']} (風報比 {rr_text})", "color": "#FCA5A5", "size": "xs", "align": "end"}
                    ]
                },
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
# 10. 推播發送模組
# ==========================================
def push_line_flex(token: str, user_id: str, bubbles: List[dict], alt_text: str):
    if not token or not user_id or not bubbles: return
    url = "https://api.line.me/v2/bot/message/push"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    payload = {
        "to": user_id,
        "messages": [
            {
                "type": "flex",
                "altText": alt_text,
                "contents": {"type": "carousel", "contents": bubbles[:10]}
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
# 11. 主排程流程控制
# ==========================================
def main():
    print(f"===== 啟動多源智能投研系統 (模式: {RUN_MODE} | 強制推播: {FORCE_NOTIFY}) =====")

    macro_data = fetch_global_macro_snapshot()
    vix_val = macro_data.get("VIX", {}).get("price", 15.0)

    # 模式一：晨間全球情勢快報
    if RUN_MODE == "MORNING":
        print("☀️ 正在建構【晨間全球前瞻與夜盤快報】...")
        morning_bubble = build_morning_brief_bubble(macro_data)
        push_line_flex(LINE_CHANNEL_ACCESS_TOKEN, LINE_USER_ID, [morning_bubble], "☀️ 晨間全球前瞻與夜盤快報已送達！")
        return

    # 模式二：盤後個股估值與操作掃描
    twse_data = fetch_twse_official_metrics()
    print(f"🏛️ 台灣證交所官方指標已加載 (共 {len(twse_data)} 檔標的)")

    regimes = {
        "USD": check_market_regime("USD", vix_val),
        "TWD": check_market_regime("TWD", vix_val)
    }

    state_cache = load_state_cache()
    new_state_cache = dict(state_cache)
    actionable_cards = []

    for item in PORTFOLIO_WATCHLIST:
        currency = item["currency"]
        if RUN_MODE in ["TWD", "USD"] and currency != RUN_MODE:
            continue

        ticker = item["ticker"]
        name = item["name"]
        curr_symbol = "$" if currency == "USD" else "NT$"

        data = analyze_stock(ticker)
        if not data: continue

        # 計算動態合理價
        dynamic_fair, val_source = calculate_dynamic_fair_value(
            item, data.get("info", {}), twse_data, data["price"]
        )

        market_regime = regimes[currency]
        decision = evaluate_decision(item, data, market_regime, macro_data, dynamic_fair, val_source)

        current_signal = decision["signal_badge"]
        last_signal = state_cache.get(ticker)
        new_state_cache[ticker] = current_signal

        print(f"[{name}] 市價: {curr_symbol}{data['price']} | 動態合理價: {curr_symbol}{dynamic_fair} ({val_source}) | 訊號: {current_signal}")

        is_state_changed = (current_signal != last_signal)
        should_alert = decision["is_active_signal"] and (is_state_changed or FORCE_NOTIFY)

        if should_alert:
            card_info = {
                "name": name, "ticker": ticker, "curr_symbol": curr_symbol,
                "price": data["price"], "ma20": data["ma20"], "rsi": data["rsi"], **decision
            }
            actionable_cards.append(build_stock_bubble(card_info, market_regime))

    if actionable_cards:
        push_line_flex(LINE_CHANNEL_ACCESS_TOKEN, LINE_USER_ID, actionable_cards, f"🚨 持股決策轉折通知：{len(actionable_cards)} 檔標的最新訊號！")
    else:
        print("💡 所有標的狀態未變動或處於觀望狀態，無須打擾。")

    save_state_cache(new_state_cache)
    print("===== 掃描流程完畢 =====")

if __name__ == "__main__":
    main()
