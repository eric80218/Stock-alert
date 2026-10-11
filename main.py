import os
import sys
import json
import requests
import io
import datetime
import pandas as pd
import yfinance as yf
from typing import Dict, List, Optional, Tuple

# ==========================================
# 1. 系統設定與環境變數
# ==========================================
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_USER_ID = os.getenv("LINE_USER_ID", "")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")
PORTFOLIO_SHEET_URL = os.getenv("PORTFOLIO_SHEET_URL", "")

RUN_MODE = os.getenv("RUN_MODE", "ALL").upper()
FORCE_NOTIFY = os.getenv("FORCE_NOTIFY", "false").lower() == "true"

DASHBOARD_URL = "https://eric80218.github.io/Stock-alert/"
CACHE_FILE = "state_cache.json"
DATA_EXPORT_FILE = "data.json"

# ==========================================
# 2. Google 試算表動態同步模組
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
# 3. 台灣證交所 (TWSE) 官方指標
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
def calculate_dynamic_fair_value(item: dict, yf_info: dict, twse_data: dict, current_price: float, extra_data: dict) -> Tuple[float, str]:
    ticker = item["ticker"]
    base_fair = item["base_fair"]
    model = item["val_model"]

    if model == "ETF_TREND":
        ma120 = extra_data.get("ma120")
        if ma120 and ma120 > 0:
            return round(float(ma120), 2), "120MA半年通道"
        return base_fair, "基準固定"

    elif model == "ANALYST":
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
# 5. 狀態快取記憶
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
# 6. 夜盤、總經與黃金避險雷達
# ==========================================
def fetch_global_macro_snapshot() -> dict:
    indicators = {
        "TSM": "TSM", "NQ_F": "NQ=F", "ES_F": "ES=F",
        "GOLD": "GC=F", "VIX": "^VIX", "TNX": "^TNX", "DXY": "DX-Y.NYB"
    }
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

def build_morning_brief_bubble(macro: dict, is_gold_panic: bool) -> dict:
    tsm = macro.get("TSM", {})
    nq = macro.get("NQ_F", {})
    gold = macro.get("GOLD", {})
    vix = macro.get("VIX", {})
    tnx = macro.get("TNX", {})
    vix_val = vix.get("price", 15)

    def fmt_chg(val): return f"{val:+.2f}%" if val else "0.00%"

    if is_gold_panic:
        market_mood, mood_color, header_bg = "🚨 避險警報(搶購黃金)", "#EF4444", "#7F1D1D"
        advice = f"【華爾街避險暴衝】黃金期貨暴漲 {fmt_chg(gold.get('chg_pct'))} 且期指走跌、VIX達{vix_val}！機構資金正自風險資產撤退，嚴禁抄底追高！"
    elif tsm.get("chg_pct", 0) > 1.0 and nq.get("chg_pct", 0) > 0.5:
        market_mood, mood_color, header_bg = "🟢 多方偏多開出", "#10B981", "#064E3B"
        advice = "台積電 ADR 與美指期走勢平穩偏多。持股續抱，無須受盤中微幅震盪影響情緒。"
    elif vix_val >= 25 or nq.get("chg_pct", 0) < -1.0:
        market_mood, mood_color, header_bg = "🔴 震盪避險防禦", "#EF4444", "#7F1D1D"
        advice = f"期指受壓且恐慌指數達 {vix_val}，盤中預料震盪。維持紀律觀望，不隨意加碼攤平。"
    else:
        market_mood, mood_color, header_bg = "🟡 常態區間整理", "#F59E0B", "#78350F"
        advice = "各類資產與黃金處於常態整理，市場多空平衡。照既定計畫持股，今日無須手動操盤。"

    return {
        "type": "bubble", "size": "kilo",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": header_bg, "paddingAll": "16px",
            "contents": [
                {
                    "type": "box", "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "☀️ 市場定錨與避險雷達", "weight": "bold", "color": "#FFFFFF", "size": "md", "flex": 3},
                        {"type": "text", "text": market_mood, "weight": "bold", "color": mood_color, "size": "sm", "align": "end", "flex": 3}
                    ]
                },
                {"type": "text", "text": "夜盤期指 · 黃金避險 · 總經定錨總覽", "color": "#CBD5E1", "size": "xs", "margin": "xs"}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "backgroundColor": "#0F172A", "paddingAll": "16px", "spacing": "sm",
            "contents": [
                {"type": "text", "text": "⚡ 國際期指與避險核心", "color": "#38BDF8", "weight": "bold", "size": "xs"},
                {
                    "type": "box", "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "紐約黃金期 (GC=F)", "color": "#FCD34D", "size": "xs", "weight": "bold"},
                        {"type": "text", "text": f"${gold.get('price')} ({fmt_chg(gold.get('chg_pct'))})", "color": is_gold_panic and "#F87171" or "#FDE68A", "weight": "bold", "size": "xs", "align": "end"}
                    ]
                },
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
                {"type": "separator", "color": "#334155", "margin": "sm"},
                {"type": "text", "text": "🌐 總經風險指標", "color": "#38BDF8", "weight": "bold", "size": "xs", "margin": "sm"},
                {
                    "type": "box", "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "VIX 恐慌指數", "color": "#94A3B8", "size": "xs"},
                        {"type": "text", "text": f"{vix.get('price')} ({vix_val>=25 and '警戒' or '平穩'})", "color": vix_val>=25 and "#EF4444" or "#CBD5E1", "weight": "bold", "size": "xs", "align": "end"}
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
                        {"type": "text", "text": "💡 戰略作戰方針：", "color": "#38BDF8", "weight": "bold", "size": "xs"},
                        {"type": "text", "text": advice, "color": "#F8FAFC", "size": "xxs", "wrap": True, "margin": "xs"}
                    ]
                }
            ]
        },
        "footer": {
            "type": "box", "layout": "horizontal", "backgroundColor": "#1E293B", "paddingAll": "10px",
            "contents": [
                {"type": "button", "style": "primary", "height": "sm", "color": "#0284C7", "action": {"type": "uri", "label": "開啟雲端儀表板", "uri": DASHBOARD_URL}}
            ]
        }
    }

# ==========================================
# 7. 技術分析與【盤中異常過濾 + ATR 風控】核心
# ==========================================
def calculate_risk_reward(price: float, fair_val: float, ma20: float, low_10d: float, atr: float) -> Tuple[float, Optional[float]]:
    volatility_stop = price - (2.0 * atr)
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

        ma20_s = close.rolling(20).mean()
        curr_ma20 = round(float(ma20_s.iloc[-1]), 2)
        prev_ma20 = round(float(ma20_s.iloc[-2]), 2)

        ma120_s = close.rolling(120).mean()
        curr_ma120 = round(float(ma120_s.iloc[-1]), 2) if len(close) >= 120 and pd.notna(ma120_s.iloc[-1]) else curr_ma20

        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, 0.0001)
        rsi = round(float((100 - (100 / (1 + rs))).iloc[-1]), 1)

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
            "ma120": curr_ma120, "rsi": rsi, "low_10d": low_10d,
            "atr": curr_atr, "atr_pct": atr_pct, "info": info
        }
    except Exception:
        return None

def evaluate_decision(item: dict, data: dict, market_regime: dict, macro_data: dict, dynamic_fair: float, val_source: str, is_gold_panic: bool) -> dict:
    price = data["price"]
    shares = item["shares"]
    fair_val = dynamic_fair
    diff_pct = ((price - fair_val) / fair_val) * 100
    buy_threshold_pct = - (item["mos_buy"] * 100)
    sell_threshold_pct = item["expensive_sell"] * 100

    vix_val = macro_data.get("VIX", {}).get("price", 15.0)
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
    
    if is_gold_panic:
        if score > 0: score = -0.5
        macro_warnings.append("🚨 [黃金瘋搶避險] 資金全面撤出股市湧向黃金，暫停追價！")

    if vix_val >= 24.0 or not market_regime["is_bull"]:
        if score >= 2.0:
            score = 1.2
            macro_warnings.append("⚠️ [大盤偏空] 買進降級為少量試單")

    is_falling_knife = (price < data["ma20"]) and (data["ma20"] < data["prev_ma20"])
    is_extreme_crash = (data["prev_price"] - price) >= (2.5 * data["atr"])

    if is_falling_knife or is_extreme_crash:
        if score >= 1.0:
            score = 0.0
            if is_extreme_crash:
                macro_warnings.append(f"🚨 [黑天鵝異動] 單日重挫逾 2.5×ATR ({data['atr']})，嚴禁徒手接刀！")
            else:
                macro_warnings.append("⚠️ [防接刀保護] 股價破線且20MA下彎，強制等待右側站回！")

    stop_loss, rr_ratio = calculate_risk_reward(price, fair_val, data["ma20"], data["low_10d"], data["atr"])
    curr_sym = "$" if item["currency"] == "USD" else "NT$"

    # 核心：盤中摜穿停損判定 (手中持股 且 市價穿透停損)
    is_stop_loss_breached = (shares > 0) and (price <= stop_loss)

    if is_stop_loss_breached:
        signal_badge, badge_color, header_color = "🚨 跌破停損", "#DC2626", "#7F1D1D"
        base_advice = f"【盤中停損破線】市價已跌破 2.0×ATR 防守停損價 {curr_sym}{stop_loss}！請嚴格執行紀律防守，考慮減碼或停損出場！"
    elif score >= 2.5:
        signal_badge, badge_color, header_color = "🔥 立即買進", "#10B981", "#064E3B"
        base_advice = f"【右側建倉買點】落入安全邊際且翻多。防守停損 {curr_sym}{stop_loss} (2.0×ATR動態防守)，風報比 1:{rr_ratio or '優'}。"
    elif score >= 1.0:
        signal_badge, badge_color, header_color = "🟢 逢低加碼", "#34D399", "#065F46"
        base_advice = f"【性價比充足】回測支撐有守，可分批承接。防守停損 {curr_sym}{stop_loss} (2.0×ATR動態防守)。"
    elif score <= -2.5:
        signal_badge, badge_color, header_color = "🔴 立即賣出", "#EF4444", "#7F1D1D"
        base_advice = "【估值嚴重透支】價格大幅高估，強烈建議分批停利以鎖定獲利。"
    elif score <= -1.0:
        signal_badge, badge_color, header_color = "🟠 建議減碼", "#F97316", "#7C2D12"
        base_advice = "【轉弱避險防守】摜破防線或受利空壓抑，建議多單部分減碼。"
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
        "dynamic_fair": dynamic_fair, "val_source": val_source,
        "is_stop_loss_breached": is_stop_loss_breached,
        "is_extreme_crash": is_extreme_crash
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
                        {"type
