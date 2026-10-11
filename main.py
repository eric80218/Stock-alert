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
# 6. 夜盤、總經與【黃金避險雷達】
# ==========================================
def fetch_global_macro_snapshot() -> dict:
    # 納入 GC=F (黃金期貨) 作為終極避險監控
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
    es = macro.get("ES_F", {})
    gold = macro.get("GOLD", {})
    vix = macro.get("VIX", {})
    tnx = macro.get("TNX", {})

    vix_val = vix.get("price", 15)
    def fmt_chg(val): return f"{val:+.2f}%" if val else "0.00%"

    if is_gold_panic:
        market_mood, mood_color, header_bg = "🚨 避險警報(搶購黃金)", "#EF4444", "#7F1D1D"
        advice = f"【華爾街避險暴衝】黃金期貨暴漲 {fmt_chg(gold.get('chg_pct'))} 且期指走跌、VIX達{vix_val}！機構資金正全面自風險資產撤退，嚴禁抄底追高，防範系統性回檔！"
    elif tsm.get("chg_pct", 0) > 1.0 and nq.get("chg_pct", 0) > 0.5:
        market_mood, mood_color, header_bg = "🟢 多方偏多開出", "#10B981", "#064E3B"
        advice = "台積電 ADR 與美指期走勢平穩偏多，預期開盤情緒穩定。持股續抱，無須受盤中微幅震盪影響情緒。"
    elif vix_val >= 25 or nq.get("chg_pct", 0) < -1.0:
        market_mood, mood_color, header_bg = "🔴 震盪避險防禦", "#EF4444", "#7F1D1D"
        advice = f"期指受壓且恐慌指數達 {vix_val}，早盤預料承壓。維持紀律觀望，不隨意加碼攤平。"
    else:
        market_mood, mood_color, header_bg = "🟡 常態區間整理", "#F59E0B", "#78350F"
        advice = "全球資產與黃金處於常態整理，市場多空平衡。照既定計畫安心持股，今日無須手動操盤。"

    return {
        "type": "bubble", "size": "kilo",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": header_bg, "paddingAll": "16px",
            "contents": [
                {
                    "type": "box", "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "☀️ 盤前戰略快報", "weight": "bold", "color": "#FFFFFF", "size": "md", "flex": 3},
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
                {"type": "text", "text": "🌐 總經風險與折現率", "color": "#38BDF8", "weight": "bold", "size": "xs", "margin": "sm"},
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
                        {"type": "text", "text": "💡 盤前定錨：", "color": "#38BDF8", "weight": "bold", "size": "xs"},
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
# 7. 技術分析與【黑天鵝接刀 + ATR 風控】核心
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
    
    # 核心：黃金避險暴衝連動防護
    if is_gold_panic:
        if score > 0: score = -0.5
        macro_warnings.append("🚨 [黃金瘋搶避險] 資金全面撤出股市湧向黃金，全線暫停追價，防守為上！")

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
                macro_warnings.append(f"🚨 [黑天鵝異動] 單日崩跌逾 2.5×ATR ({data['atr']})，嚴禁徒手接刀！")
            else:
                macro_warnings.append("⚠️ [防接刀保護] 股價跌破月線且20MA下彎，強制等待右側站回！")

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
                        {"type": "text", "text": "🎯 盤前決策方針：", "color": "#38BDF8", "weight": "bold", "size": "xs"},
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
# 8. 推播模組
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
        if len(messages) >= 5: break

    payload = {"to": user_id, "messages": messages}
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=15)
        if res.status_code == 200:
            print(f"✅ LINE Flex 推播成功（共 {len(bubbles)} 檔卡片）！")
        else:
            print(f"❌ LINE 推播失敗: {res.status_code} - {res.text}")
    except Exception as e:
        print(f"LINE 請求異常: {e}")

# ==========================================
# 9. 主排程流程 (盤前定錨 + 寧靜投資過濾)
# ==========================================
def main():
    print(f"===== 啟動多源智能投研系統 (模式: {RUN_MODE} | 強制推播: {FORCE_NOTIFY}) =====")

    watchlist = load_portfolio_watchlist()
    if not watchlist:
        print("❌ 無持股監控標的，結束執行。")
        return

    macro_data = fetch_global_macro_snapshot()
    vix_val = macro_data.get("VIX", {}).get("price", 15.0)
    gold_data = macro_data.get("GOLD", {})
    gold_chg = gold_data.get("chg_pct", 0.0)
    nq_chg = macro_data.get("NQ_F", {}).get("chg_pct", 0.0)
    es_chg = macro_data.get("ES_F", {}).get("chg_pct", 0.0)

    # 華爾街崩盤避險前兆判定：
    # 當美指期重挫 (NQ 或 ES <= -0.8%) 且 黃金期貨單日暴衝逾 1.2% 且 VIX >= 22
    is_gold_panic = (gold_chg >= 1.2) and ((nq_chg <= -0.8) or (es_chg <= -0.8)) and (vix_val >= 22.0)
    if is_gold_panic:
        print(f"🚨 警告：觸發【華爾街避險崩盤警訊】！黃金暴漲 {gold_chg}%，期指走弱，避險湧入！")

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
            print(f"⚠️ [{name} ({ticker})] 無法取得行情，略過。")
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

        # 模式過濾 (TWD 或 USD 盤前獨立跑)
        if RUN_MODE in ["TWD", "USD"] and currency != RUN_MODE:
            continue

        dynamic_fair, val_source = calculate_dynamic_fair_value(item, data.get("info", {}), twse_data, price, data)
        market_regime = regimes[currency]
        decision = evaluate_decision(item, data, market_regime, macro_data, dynamic_fair, val_source, is_gold_panic)

        current_signal = decision["signal_badge"]
        last_signal = state_cache.get(ticker)
        new_state_cache[ticker] = current_signal

        card_info = {
            "name": name, "ticker": ticker, "currency": currency, "broker": item["broker"],
            "shares": shares, "cost_price": cost_price, "curr_symbol": curr_symbol,
            "price": price, "ma20": data["ma20"], "rsi": data["rsi"],
            "atr": data["atr"], "atr_pct": data["atr_pct"], **decision
        }
        evaluated_pool.append(card_info)

        # 核心防操盤過濾：
        # 平日只在「出現買進、賣出、減碼」訊號，或手動強制 (FORCE_NOTIFY) 時才納入推播！
        # 徹底過濾「⚪ 觀望續抱」卡片，避免天天洗版造成想盯盤的衝動！
        is_actionable = ("買進" in current_signal) or ("賣出" in current_signal) or ("減碼" in current_signal)
        
        if is_actionable or FORCE_NOTIFY:
            actionable_cards.append(build_stock_bubble(card_info, market_regime))

    if summary_stats["usd_cost"] > 0:
        summary_stats["usd_pnl"] = summary_stats["usd_val"] - summary_stats["usd_cost"]
        summary_stats["usd_pnl_pct"] = (summary_stats["usd_pnl"] / summary_stats["usd_cost"]) * 100
    if summary_stats["twd_cost"] > 0:
        summary_stats["twd_pnl"] = summary_stats["twd_val"] - summary_stats["twd_cost"]
        summary_stats["twd_pnl_pct"] = (summary_stats["twd_pnl"] / summary_stats["twd_cost"]) * 100

    # 匯出資料至網頁儀表板 (無論推播與否，網頁隨時保持 100% 最新)
    try:
        now_str = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        dashboard_payload = {
            "updated_at": now_str,
            "macro": macro_data,
            "summary": summary_stats,
            "is_gold_panic": is_gold_panic,
            "stocks": evaluated_pool
        }
        with open(DATA_EXPORT_FILE, "w", encoding="utf-8") as f:
            json.dump(dashboard_payload, f, ensure_ascii=False, indent=2)
        print(f"✅ 儀表板資料已同步儲存至 {DATA_EXPORT_FILE}！")
    except Exception as e:
        print(f"⚠️ 匯出 dashboard 失敗: {e}")

    # ==========================================
    # 寧靜推播決策
    # ==========================================
    # 只要滿足任一條件才推播：
    # 1. 觸發黃金避險崩盤警訊 (is_gold_panic)
    # 2. 有標的出現買進/加碼/減碼/賣出 (actionable_cards)
    # 3. 手動強制測試 (FORCE_NOTIFY)
    should_push = is_gold_panic or (len(actionable_cards) > 0) or FORCE_NOTIFY

    if should_push:
        morning_brief = build_morning_brief_bubble(macro_data, is_gold_panic)
        final_bubbles = [morning_brief] + actionable_cards
        push_line_flex(
            LINE_CHANNEL_ACCESS_TOKEN, LINE_USER_ID, final_bubbles,
            f"🚨 盤前重點快報：{'黃金避險警報！' if is_gold_panic else ''}共 {len(actionable_cards)} 檔標的出現決策訊號！"
        )
    else:
        print("☕ 今日全體持股處於【⚪ 觀望續抱】且總經無異常，啟動寧靜防打擾模式，不發送 LINE，讓您安心生活！")

    save_state_cache(new_state_cache)
    print("===== 排程掃描結束 =====")

if __name__ == "__main__":
    main()
