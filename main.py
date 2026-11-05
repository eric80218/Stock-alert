import os, sys, json, time, requests, io, datetime
import pandas as pd
import yfinance as yf
from typing import Dict, List, Optional, Tuple

# 1. 系統設定與環境變數
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_USER_ID = os.getenv("LINE_USER_ID", "")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")
PORTFOLIO_SHEET_URL = os.getenv("PORTFOLIO_SHEET_URL", "")
RUN_MODE = os.getenv("RUN_MODE", "ALL").upper()
FORCE_NOTIFY = os.getenv("FORCE_NOTIFY", "false").lower() == "true"

DASHBOARD_URL = "https://eric80218.github.io/Stock-alert/"
CACHE_FILE = "state_cache.json"
DATA_EXPORT_FILE = "data.json"

# 2. 台美雙市場完整持股清單與核心底倉保護名單 (已將 AMZN 加入長線底倉)
EXCLUDED_TICKERS = {"1904.TW", "2616.TW", "2337.TW", "6182.TW", "6182.TWO", "VRT"}
CORE_HOLD_TICKERS = {"0050.TW", "009816.TW", "BRK-B", "COST", "2330.TW", "1232.TW", "AMZN"}

PORTFOLIO_OVERRIDES = {
    # 🇹🇼 台股部位 (國泰證券)
    "0050.TW": {"ticker": "0050.TW", "name": "元大台灣50", "currency": "TWD", "broker": "國泰證券", "shares": 1386.0, "cost_price": 84.96, "val_model": "ETF_TREND", "mos_buy": 0.08, "expensive_sell": 0.35},
    "009816.TW": {"ticker": "009816.TW", "name": "凱基台灣TOP50", "currency": "TWD", "broker": "國泰證券", "shares": 1569.0, "cost_price": 15.81, "val_model": "ETF_TREND", "mos_buy": 0.08, "expensive_sell": 0.35},
    "2317.TW": {"ticker": "2317.TW", "name": "鴻海", "currency": "TWD", "broker": "國泰證券", "shares": 60.0, "cost_price": 245.5, "val_model": "PE_EPS", "mos_buy": 0.15, "expensive_sell": 0.25},
    "2330.TW": {"ticker": "2330.TW", "name": "台積電", "currency": "TWD", "broker": "國泰證券", "shares": 2.0, "cost_price": 2405.0, "val_model": "PE_EPS", "mos_buy": 0.15, "expensive_sell": 0.35},
    "2002.TW": {"ticker": "2002.TW", "name": "中鋼", "currency": "TWD", "broker": "國泰證券", "shares": 1000.0, "cost_price": 21.05, "val_model": "DIVIDEND", "mos_buy": 0.12, "expensive_sell": 0.25},
    "1232.TW": {"ticker": "1232.TW", "name": "大統益", "currency": "TWD", "broker": "國泰證券", "shares": 50.0, "cost_price": 144.0, "val_model": "DIVIDEND", "mos_buy": 0.12, "expensive_sell": 0.35},
    # 🇺🇸 美股部位 (Firstrade / UBS)
    "AMZN": {"ticker": "AMZN", "name": "Amazon 亞馬遜", "currency": "USD", "broker": "Firstrade", "shares": 3.0, "cost_price": 254.55, "val_model": "ANALYST", "mos_buy": 0.15, "expensive_sell": 0.35},
    "NVDA": {"ticker": "NVDA", "name": "NVIDIA 輝達", "currency": "USD", "broker": "Firstrade", "shares": 22.01, "cost_price": 219.80, "val_model": "ANALYST", "mos_buy": 0.18, "expensive_sell": 0.30},
    "COST": {"ticker": "COST", "name": "Costco 好市多", "currency": "USD", "broker": "Firstrade", "shares": 8.01, "cost_price": 1044.55, "val_model": "ANALYST", "mos_buy": 0.12, "expensive_sell": 0.35},
    "BRK-B": {"ticker": "BRK-B", "name": "Berkshire 波克夏B", "currency": "USD", "broker": "Firstrade", "shares": 5.0, "cost_price": 492.03, "val_model": "ANALYST", "mos_buy": 0.10, "expensive_sell": 0.35},
    "LLY": {"ticker": "LLY", "name": "Eli Lilly 禮來", "currency": "USD", "broker": "Firstrade", "shares": 2.0, "cost_price": 1159.82, "val_model": "ANALYST", "mos_buy": 0.15, "expensive_sell": 0.25},
    "NVO": {"ticker": "NVO", "name": "Novo Nordisk 諾和諾德", "currency": "USD", "broker": "Firstrade", "shares": 2.0, "cost_price": 39.10, "val_model": "ANALYST", "mos_buy": 0.15, "expensive_sell": 0.25},
    "AMAT": {"ticker": "AMAT", "name": "Applied Materials 應材", "currency": "USD", "broker": "UBS ESPP", "shares": 0.0, "cost_price": 0.0, "val_model": "ANALYST", "mos_buy": 0.15, "expensive_sell": 0.25}
}

def load_portfolio_watchlist() -> List[dict]:
    portfolio_map = {tk: dict(data) for tk, data in PORTFOLIO_OVERRIDES.items()}
    if PORTFOLIO_SHEET_URL:
        try:
            res = requests.get(PORTFOLIO_SHEET_URL, timeout=10)
            res.encoding = 'utf-8'
            if res.status_code == 200:
                df = pd.read_csv(io.StringIO(res.text))
                for _, r in df.iterrows():
                    tk = str(r.get("ticker", "")).strip().upper()
                    if not tk or tk == "NAN" or tk in EXCLUDED_TICKERS: continue
                    sh = pd.to_numeric(r.get("shares"), errors='coerce')
                    cp = pd.to_numeric(r.get("cost_price"), errors='coerce')
                    if tk == "AMAT" and portfolio_map["AMAT"]["shares"] == 0.0:
                        if pd.notna(sh) and sh > 0: portfolio_map["AMAT"]["shares"] = float(sh)
                        if pd.notna(cp) and cp > 0: portfolio_map["AMAT"]["cost_price"] = float(cp)
                        continue
                    if tk in portfolio_map: continue
                    mb = pd.to_numeric(r.get("mos_buy"), errors='coerce')
                    es = pd.to_numeric(r.get("expensive_sell"), errors='coerce')
                    portfolio_map[tk] = {
                        "ticker": tk, "name": str(r.get("name", tk)).strip(),
                        "currency": str(r.get("currency", "USD")).strip().upper(),
                        "broker": str(r.get("broker", "一般")).strip(),
                        "shares": float(sh) if pd.notna(sh) else 0.0,
                        "cost_price": float(cp) if pd.notna(cp) else 0.0,
                        "val_model": str(r.get("val_model", "ANALYST")).strip().upper(),
                        "mos_buy": float(mb) if pd.notna(mb) else 0.15,
                        "expensive_sell": float(es) if pd.notna(es) else 0.25
                    }
        except Exception as e:
            print(f"⚠️ 試算表讀取略過，使用內建主清單: {e}")
    return list(portfolio_map.values())

# 3. 台灣證交所 (TWSE) 官方指標與 100% 市場浮動估值引擎
def fetch_twse_official_metrics() -> dict:
    twse_dict = {}
    try:
        res = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL", timeout=8)
        if res.status_code == 200:
            for row in res.json():
                c = row.get("Code")
                if c:
                    pe = float(row["PEratio"]) if row.get("PEratio") and row["PEratio"] != "-" else None
                    dy = float(row["DividendYield"]) if row.get("DividendYield") and row["DividendYield"] != "-" else None
                    twse_dict[c] = {"pe": pe, "yield": dy}
    except Exception:
        pass
    return twse_dict

def calculate_dynamic_fair_value(item: dict, yf_info: dict, twse_data: dict, price: float, extra: dict) -> Tuple[float, str]:
    tk, model = item["ticker"], item["val_model"]
    ma120 = extra.get("ma120") or price
    if model == "ANALYST":
        yf_t = yf_info.get("targetMeanPrice") or yf_info.get("targetMedianPrice")
        if yf_t and float(yf_t) > 0: return round(float(yf_t), 2), "Yahoo法人共識"
        if FINNHUB_API_KEY:
            try:
                r = requests.get(f"https://finnhub.io/api/v1/stock/price-target?symbol={tk}&token={FINNHUB_API_KEY}", timeout=5).json()
                fh_t = r.get("targetMean") or r.get("targetMedian")
                if fh_t and float(fh_t) > 0: return round(float(fh_t), 2), "Finnhub法人目標"
            except Exception: pass
        return round(float(ma120) * 1.08, 2), "120MA長線成長錨點"
    elif model == "PE_EPS":
        yf_t = yf_info.get("targetMeanPrice") or yf_info.get("targetMedianPrice")
        if yf_t and float(yf_t) > 0: return round(float(yf_t), 2), "法人目標共識"
        tw_m = twse_data.get(tk.replace(".TW", "").replace(".TWO", ""))
        if tw_m and tw_m.get("pe") and tw_m["pe"] > 0:
            fair_pe_p = (price / tw_m["pe"]) * 20.0
            return round((fair_pe_p * 0.5) + (ma120 * 0.5), 2), "證交所EPS×均值浮動"
        eps = yf_info.get("forwardEps") or yf_info.get("trailingEps")
        if eps and float(eps) > 0: return round((float(eps) * 20.0 + ma120) / 2.0, 2), "動態EPS均值"
        return round(float(ma120) * 1.05, 2), "120MA市場均值"
    elif model == "DIVIDEND":
        tw_m = twse_data.get(tk.replace(".TW", "").replace(".TWO", ""))
        if tw_m and tw_m.get("yield") and tw_m["yield"] >= 1.5:
            y_fair = (price * (tw_m["yield"] / 100.0)) / 0.045
            return round((y_fair * 0.5) + (ma120 * 0.5), 2), "殖利率×半年線浮動"
        div_r = yf_info.get("dividendRate") or 0.0
        if div_r > 0: return round(((float(div_r) / 0.045) * 0.5) + (ma120 * 0.5), 2), "配息×半年線浮動"
        return round(float(ma120) * 1.05, 2), "120MA半年通道"
    return round(float(ma120) * 1.08, 2), "120MA長線成長錨點"

# 4. 快取、總經雷達與技術分析
def load_state_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f: return json.load(f)
        except Exception: pass
    return {}

def save_state_cache(cache: dict):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f: json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception: pass

def fetch_global_macro_snapshot() -> dict:
    ind = {"TSM": "TSM", "NQ_F": "NQ=F", "ES_F": "ES=F", "GOLD": "GC=F", "VIX": "^VIX", "TNX": "^TNX", "DXY": "DX-Y.NYB"}
    data = {}
    for k, sym in ind.items():
        data[k] = {"price": 0.0, "chg_pct": 0.0}
        for attempt in range(2):
            try:
                close = yf.Ticker(sym).history(period="5d")['Close'].dropna()
                if len(close) >= 2:
                    c, p = float(close.iloc[-1]), float(close.iloc[-2])
                    data[k] = {"price": round(c, 2), "chg_pct": round(((c - p) / p) * 100, 2)}
                    break
            except Exception:
                if attempt == 0: time.sleep(1)
    return data

def analyze_stock(ticker: str) -> Optional[dict]:
    for attempt in range(2):
        try:
            st = yf.Ticker(ticker)
            df = st.history(period="1y")
            if df.empty or len(df) < 25:
                if attempt == 0: time.sleep(1); continue
                return None
            close, high, low = df['Close'].dropna(), df['High'].dropna(), df['Low'].dropna()
            curr_p, prev_p = round(float(close.iloc[-1]), 2), round(float(close.iloc[-2]), 2)
            low_10d = round(float(low.tail(10).min()), 2)
            info = {}
            try:
                info = st.info or {}
                if not ticker.endswith((".TW", ".TWO")) and info.get("preMarketPrice") and info["preMarketPrice"] > 0:
                    curr_p = round(float(info["preMarketPrice"]), 2)
            except Exception: pass
            ma20_s = close.rolling(20).mean()
            c_ma20, p_ma20 = round(float(ma20_s.iloc[-1]), 2), round(float(ma20_s.iloc[-2]), 2)
            ma120_s = close.rolling(120).mean()
            c_ma120 = round(float(ma120_s.iloc[-1]), 2) if len(close) >= 120 and pd.notna(ma120_s.iloc[-1]) else c_ma20
            delta = close.diff()
            rs = delta.clip(lower=0).rolling(14).mean() / (-delta.clip(upper=0)).rolling(14).mean().replace(0, 0.0001)
            rsi = round(float((100 - (100 / (1 + rs))).iloc[-1]), 1)
            pc = close.shift(1)
            tr = pd.concat([high - low, (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
            atr_s = tr.rolling(14).mean()
            c_atr = round(float(atr_s.iloc[-1]), 2) if pd.notna(atr_s.iloc[-1]) else round(curr_p * 0.025, 2)
            return {
                "price": curr_p, "prev_price": prev_p, "ma20": c_ma20, "prev_ma20": p_ma20,
                "ma120": c_ma120, "rsi": rsi, "low_10d": low_10d, "atr": c_atr,
                "atr_pct": round((c_atr / curr_p) * 100, 1) if curr_p > 0 else 2.5, "info": info
            }
        except Exception:
            if attempt == 0: time.sleep(1)
    return None

def evaluate_decision(item: dict, data: dict, regime: dict, macro: dict, fair_val: float, val_src: str, gold_panic: bool) -> dict:
    p, sh, tk = data["price"], item["shares"], item["ticker"]
    is_core = tk in CORE_HOLD_TICKERS
    fv = fair_val if fair_val > 0 else p
    diff_pct = ((p - fv) / fv) * 100
    score = 0.0

    if diff_pct <= -(item["mos_buy"] * 100): score += 2.0
    elif diff_pct < 0: score += 0.5
    elif diff_pct >= (item["expensive_sell"] * 100): score -= 2.0
    elif diff_pct > 15 and not is_core: score -= 0.5

    if data["prev_price"] <= data["prev_ma20"] and p > data["ma20"] * 1.005:
        score += 1.5
    elif p > data["ma20"]:
        score += 0.5
    elif not is_core:
        if data["prev_price"] >= data["prev_ma20"] and p < data["ma20"] * 0.985 and data["ma20"] < data["prev_ma20"]:
            score -= 1.2
        elif p < data["ma20"] * 0.98:
            score -= 0.5

    if data["rsi"] <= 32: score += 1.0
    elif data["rsi"] >= 78 and not is_core: score -= 1.0

    warns = []
    if gold_panic:
        if score > 0: score = -0.5
        warns.append("🚨 [黃金瘋搶避險] 資金撤出股市湧向黃金，暫停追價！")
    if macro.get("VIX", {}).get("price", 15.0) >= 24.0 or not regime["is_bull"]:
        if score >= 2.0: score = 1.2; warns.append("⚠️ [大盤偏空] 買進降級為少量試單")

    knife = (p < data["ma20"] * 0.985) and (data["ma20"] < data["prev_ma20"])
    crash = (data["prev_price"] - p) >= (2.5 * data["atr"])
    if (knife or crash) and score >= 1.0 and not is_core:
        score = 0.0
        warns.append("🚨 [黑天鵝異動] 重挫逾2.5×ATR，嚴禁接刀！" if crash else "⚠️ [防接刀保護] 破線下彎，等待站回！")

    atr_mult = 3.5 if is_core else 2.5
    vol_stop = p - (atr_mult * data["atr"])
    stop_loss = round(vol_stop if is_core else min(data["low_10d"], vol_stop), 2)
    if stop_loss >= p or stop_loss <= (p * 0.75): stop_loss = round(vol_stop, 2)
    if stop_loss >= p: stop_loss = round(p * 0.94, 2)
    rr = round((fv - p) / (p - stop_loss), 1) if (p - stop_loss) > 0 and (fv - p) > 0 else None

    sym = "$" if item["currency"] == "USD" else "NT$"
    breached = (sh > 0) and (not is_core) and (p <= stop_loss * 0.99)
    b_note = f" (建議於 {item['broker']} 掛設 GTC 停損單)" if item["currency"] == "USD" and sh > 0 and not is_core else ""

    if breached:
        badge, b_col, h_col = "🚨 跌破停損", "#DC2626", "#7F1D1D"
        adv = f"【停損破線】市價實質跌破 2.5×ATR 防守價 {sym}{stop_loss}！請執行紀律減碼或停損！{b_note}"
    elif score >= 2.5:
        badge, b_col, h_col = "🔥 立即買進", "#10B981", "#064E3B"
        adv = f"【右側買點】落入安全邊際且翻多。防守支撐 {sym}{stop_loss}，風報比 1:{rr or '優'}。{b_note}"
    elif score >= 1.0:
        badge, b_col, h_col = "🟢 逢低加碼", "#34D399", "#065F46"
        adv = f"【性價比佳】回測支撐有守可分批承接。防守支撐 {sym}{stop_loss}。{b_note}"
    elif score <= -2.5:
        badge, b_col, h_col = "🔴 立即賣出", "#EF4444", "#7F1D1D"
        adv = "【估值透支】價格大幅超越市場共識目標，建議分批停利鎖定獲利。"
    elif score <= -1.2 and not is_core:
        badge, b_col, h_col = "🟠 建議減碼", "#F97316", "#7C2D12"
        adv = "【轉弱防守】實質跌破月線防線或受壓抑，建議多單部分減碼。"
    else:
        badge, b_col, h_col = "⚪ 觀望續抱", "#94A3B8", "#1E293B"
        hold_tag = "【核心底倉長抱】豁免短線洗盤，長線續抱等待低接買點。" if is_core else f"【常態區間】持股續抱，忽略微幅震盪。防守支撐 {sym}{stop_loss}。{b_note}"
        adv = hold_tag

    return {
        "score": round(score, 1), "diff_pct": diff_pct, "signal_badge": badge,
        "badge_color": b_col, "header_color": h_col,
        "action_advice": (" | ".join(warns) + "\n" if warns else "") + adv,
        "stop_loss": stop_loss, "rr_ratio": rr, "dynamic_fair": fv,
        "val_source": val_src, "is_stop_loss_breached": breached, "is_extreme_crash": crash
    }

# 5. LINE Flex 卡片建構與推播模組
def flex_row(l_txt: str, r_txt: str, r_col: str = "#CBD5E1", bold: bool = False) -> dict:
    return {"type": "box", "layout": "horizontal", "contents": [
        {"type": "text", "text": l_txt, "color": "#94A3B8", "size": "xs"},
        {"type": "text", "text": r_txt, "color": r_col, "weight": "bold" if bold else "regular", "size": "xs", "align": "end"}
    ]}

def build_morning_brief_bubble(macro: dict, gold_panic: bool) -> dict:
    tsm, nq, gold, vix, tnx = macro.get("TSM",{}), macro.get("NQ_F",{}), macro.get("GOLD",{}), macro.get("VIX",{}), macro.get("TNX",{})
    v_val = vix.get("price", 15)
    if gold_panic: mood, m_col, h_bg, adv = "🚨 避險警報", "#EF4444", "#7F1D1D", "黃金期貨暴漲且期指走跌，資金撤退，嚴禁抄底！"
    elif tsm.get("chg_pct",0) > 1.0 and nq.get("chg_pct",0) > 0.5: mood, m_col, h_bg, adv = "🟢 偏多開出", "#10B981", "#064E3B", "ADR與期指偏多，持股續抱。"
    elif v_val >= 25 or nq.get("chg_pct",0) < -1.0: mood, m_col, h_bg, adv = "🔴 避險防禦", "#EF4444", "#7F1D1D", f"VIX達{v_val}，維持紀律觀望。"
    else: mood, m_col, h_bg, adv = "🟡 常態整理", "#F59E0B", "#78350F", "市場多空平衡，照既定計畫持股。"
    return {
        "type": "bubble", "size": "kilo",
        "header": {"type": "box", "layout": "vertical", "backgroundColor": h_bg, "paddingAll": "14px", "contents": [
            {"type": "box", "layout": "horizontal", "contents": [
                {"type": "text", "text": "☀️ 戰略與黃金雷達", "weight": "bold", "color": "#FFFFFF", "size": "sm", "flex": 3},
                {"type": "text", "text": mood, "weight": "bold", "color": m_col, "size": "xs", "align": "end", "flex": 2}
            ]}
        ]},
        "body": {"type": "box", "layout": "vertical", "backgroundColor": "#0F172A", "paddingAll": "14px", "spacing": "sm", "contents": [
            flex_row("紐約黃金期 (GC=F)", f"${gold.get('price')} ({gold.get('chg_pct',0):+.2f}%)", "#FDE68A", True),
            flex_row("台積電 ADR (TSM)", f"${tsm.get('price')} ({tsm.get('chg_pct',0):+.2f}%)", "#34D399" if tsm.get('chg_pct',0)>=0 else "#F87171", True),
            flex_row("那斯達克期 (NQ)", f"{nq.get('chg_pct',0):+.2f}%", "#34D399" if nq.get('chg_pct',0)>=0 else "#F87171", True),
            flex_row("VIX 恐慌 / 美債", f"VIX: {v_val} | 10Y: {tnx.get('price')}%"),
            {"type": "text", "text": f"💡 {adv}", "color": "#F8FAFC", "size": "xxs", "wrap": True, "margin": "md"}
        ]},
        "footer": {"type": "box", "layout": "horizontal", "backgroundColor": "#1E293B", "paddingAll": "10px", "contents": [
            {"type": "button", "style": "primary", "height": "sm", "color": "#0284C7", "action": {"type": "uri", "label": "開啟雲端儀表板", "uri": DASHBOARD_URL}}
        ]}
    }

def build_stock_bubble(d: dict, regime: dict) -> dict:
    diff_t = f"折價 {abs(d['diff_pct']):.1f}%" if d['diff_pct'] < 0 else f"溢價 {d['diff_pct']:.1f}%"
    rows = [
        flex_row("當前市價", f"{d['curr_symbol']}{d['price']}", "#FFFFFF", True),
        flex_row(f"浮動合理價 ({d['val_source']})", f"{d['curr_symbol']}{d['dynamic_fair']}", "#38BDF8", True),
        flex_row("20MA / RSI", f"{d['ma20']} | RSI: {d['rsi']}"),
        flex_row("動態防守支撐", f"{d['curr_symbol']}{d['stop_loss']}", "#FCA5A5", True)
    ]
    if d["shares"] > 0 and d["cost_price"] > 0:
        pnl = (d["price"] - d["cost_price"]) * d["shares"]
        pct = ((d["price"] - d["cost_price"]) / d["cost_price"]) * 100
        p_str = f"{pnl:+,.1f}" if d['currency'] == "USD" else f"{pnl:+,.0f}"
        rows.append(flex_row(f"持倉損益 ({d['broker']})", f"{d['curr_symbol']}{p_str} ({pct:+.1f}%)", "#34D399" if pnl>=0 else "#F87171", True))
    rows.append({"type": "text", "text": f"🎯 {d['action_advice']}", "color": "#F8FAFC", "size": "xxs", "wrap": True, "margin": "md"})
    return {
        "type": "bubble", "size": "kilo",
        "header": {"type": "box", "layout": "vertical", "backgroundColor": d["header_color"], "paddingAll": "14px", "contents": [
            {"type": "box", "layout": "horizontal", "contents": [
                {"type": "text", "text": d["name"], "weight": "bold", "color": "#FFFFFF", "size": "sm", "flex": 3},
                {"type": "text", "text": d["signal_badge"], "weight": "bold", "color": d["badge_color"], "size": "xs", "align": "end", "flex": 2}
            ]},
            {"type": "text", "text": f"{d['ticker']} · {diff_t} · {regime['label']}", "color": "#CBD5E1", "size": "xxs", "margin": "xs"}
        ]},
        "body": {"type": "box", "layout": "vertical", "backgroundColor": "#0F172A", "paddingAll": "14px", "spacing": "xs", "contents": rows}
    }

def push_line_flex(token: str, user_id: str, bubbles: List[dict], alt_text: str):
    if not token or not user_id or not bubbles: return
    msgs = [{"type": "flex", "altText": alt_text, "contents": {"type": "carousel", "contents": bubbles[i:i+10]}} for i in range(0, len(bubbles), 10)][:5]
    try:
        requests.post("https://api.line.me/v2/bot/message/push", headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"}, json={"to": user_id, "messages": msgs}, timeout=15)
    except Exception as e:
        print(f"LINE 推播異常: {e}")

# 6. 主排程執行入口
def main():
    watchlist = load_portfolio_watchlist()
    if not watchlist: return
    macro = fetch_global_macro_snapshot()
    vix = macro.get("VIX", {}).get("price", 15.0)
    gold_panic = (macro.get("GOLD", {}).get("chg_pct", 0.0) >= 1.2) and ((macro.get("NQ_F", {}).get("chg_pct", 0.0) <= -0.8) or (macro.get("ES_F", {}).get("chg_pct", 0.0) <= -0.8)) and (vix >= 22.0)
    twse_data = fetch_twse_official_metrics()

    def get_regime(curr: str) -> dict:
        bm = "SPY" if curr == "USD" else "^TWII"
        try:
            c = yf.Ticker(bm).history(period="1y")['Close'].dropna()
            bull = float(c.iloc[-1]) >= float(c.rolling(120).mean().iloc[-1]) and (vix < 25)
            return {"is_bull": bull, "label": "🟢 大盤多頭" if bull else "⚠️ 大盤偏空", "benchmark": bm}
        except Exception:
            return {"is_bull": True, "label": "大盤正常", "benchmark": bm}

    regimes = {"USD": get_regime("USD"), "TWD": get_regime("TWD")}
    state_cache = load_state_cache()
    new_cache = dict(state_cache)
    cards, pool, emerg_cnt = [], [], 0
    stats = {"usd_val": 0.0, "usd_cost": 0.0, "usd_pnl": 0.0, "usd_pnl_pct": 0.0, "twd_val": 0.0, "twd_cost": 0.0, "twd_pnl": 0.0, "twd_pnl_pct": 0.0}

    for item in watchlist:
        curr, tk = item["currency"], item["ticker"]
        data = analyze_stock(tk)
        if not data: continue
        p, sh, cp = data["price"], item["shares"], item["cost_price"]
        if p > 0 and sh > 0 and cp > 0:
            if curr == "USD": stats["usd_val"] += sh * p; stats["usd_cost"] += sh * cp
            else: stats["twd_val"] += sh * p; stats["twd_cost"] += sh * cp
        if RUN_MODE in ["TWD", "USD"] and curr != RUN_MODE: continue

        fv, v_src = calculate_dynamic_fair_value(item, data.get("info", {}), twse_data, p, data)
        dec = evaluate_decision(item, data, regimes[curr], macro, fv, v_src, gold_panic)
        sig = dec["signal_badge"]
        new_cache[tk] = sig
        info_obj = {
            "name": item["name"], "ticker": tk, "currency": curr, "broker": item["broker"],
            "shares": sh, "cost_price": cp, "curr_symbol": "$" if curr == "USD" else "NT$",
            "price": p, "ma20": data["ma20"], "rsi": data["rsi"], "atr": data["atr"], "atr_pct": data["atr_pct"], **dec
        }
        pool.append(info_obj)
        is_emerg = dec["is_stop_loss_breached"] or dec["is_extreme_crash"]
        if is_emerg: emerg_cnt += 1
        if is_emerg or any(k in sig for k in ["買進", "賣出", "減碼"]) or FORCE_NOTIFY:
            cards.append(build_stock_bubble(info_obj, regimes[curr]))

    if stats["usd_cost"] > 0:
        stats["usd_pnl"] = stats["usd_val"] - stats["usd_cost"]
        stats["usd_pnl_pct"] = (stats["usd_pnl"] / stats["usd_cost"]) * 100
    if stats["twd_cost"] > 0:
        stats["twd_pnl"] = stats["twd_val"] - stats["twd_cost"]
        stats["twd_pnl_pct"] = (stats["twd_pnl"] / stats["twd_cost"]) * 100

    now_str = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    with open(DATA_EXPORT_FILE, "w", encoding="utf-8") as f:
        json.dump({"updated_at": now_str, "macro": macro, "summary": stats, "is_gold_panic": gold_panic, "stocks": pool}, f, ensure_ascii=False, indent=2)

    if gold_panic or cards or FORCE_NOTIFY:
        prefix = "🚨【盤中突發異常快訊】" if emerg_cnt > 0 or gold_panic else "🔔 投資決策提醒："
        push_line_flex(LINE_CHANNEL_ACCESS_TOKEN, LINE_USER_ID, [build_morning_brief_bubble(macro, gold_panic)] + cards, f"{prefix}共 {len(cards)} 檔觸發警戒！")

    save_state_cache(new_cache)
    print("✅ 巡航與 data.json 輸出順利完成！")

if __name__ == "__main__":
    main()
