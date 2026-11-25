import io
import re
import csv
import html
import json
import time
import calendar
import datetime
from pathlib import Path
import requests
import yfinance as yf
import feedparser
from google import genai
from google.genai import types
from playwright.sync_api import sync_playwright

# 台灣時間 (UTC+8)
tz_tw = datetime.timezone(datetime.timedelta(hours=8))
now_tw = datetime.datetime.now(tz_tw)
now_utc = datetime.datetime.now(datetime.timezone.utc)
today_str = now_tw.strftime("%Y/%m/%d")
weekday_map = ["一", "二", "三", "四", "五", "六", "日"]
weekday_str = weekday_map[now_tw.weekday()]

MODELS = ["gemini-4-hydrogen", "gemini-3.8-flash"]
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
}

RSS_FEEDS = [
    ("Google財經", "https://news.google.com/rss/search?q=%E5%8F%B0%E8%82%A1+OR+%E7%BE%8E%E8%82%A1+OR+%E8%B2%BB%E5%8D%8A+OR+%E8%81%AF%E6%BA%96%E6%9C%83&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"),
    ("Google科技", "https://news.google.com/rss/search?q=%E5%8F%B0%E7%A9%8D%E9%9B%BB+OR+%E8%BC%9D%E9%81%94+OR+AI%E4%BC%BA%E6%9C%8D%E5%99%A8+OR+%E9%97%9C%E7%A8%85+OR+%E5%A4%96%E8%B3%87&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"),
    ("Yahoo台股", "https://tw.stock.yahoo.com/rss?category=tw-market"),
    ("Yahoo國際", "https://tw.stock.yahoo.com/rss?category=intl-markets"),
    ("CNBC", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664"),
]

# 專抓台灣政經大事、選舉、法說會、央行會議等「本週／下週預告」
TW_EVENT_PREVIEW_RSS = (
    "https://news.google.com/rss/search?"
    "q=(%E5%8F%B0%E8%82%A1+OR+%E5%8F%B0%E7%81%A3)+AND+(%E6%9C%AC%E9%80%B1%E5%A4%A7%E4%BA%8B+OR+%E4%B8%8B%E9%80%B1%E7%84%A6%E9%BB%9E+OR+%E6%B3%95%E8%AA%AA%E6%9C%83+OR+%E9%81%B8%E8%88%89+OR+%E5%A4%AE%E8%A1%8C+OR+%E8%B2%A1%E7%B6%93%E8%A1%8C%E4%BA%8B%E6%9B%86)"
    "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
)


def fetch_quote(symbol, name, is_us=True, is_yield=False):
    expected_us_date = (now_tw - datetime.timedelta(days=1)).date()
    try:
        hist = yf.Ticker(symbol).history(period="7d")
        if hist is None or hist.empty:
            raise ValueError("Empty history")
        hist = hist.dropna(subset=["Close"])
        if hist.empty:
            raise ValueError("No valid close prices")

        last_val = float(hist["Close"].iloc[-1])
        last_date = hist.index[-1].date()

        if is_us and (now_tw.weekday() in (0, 6) or last_date < expected_us_date):
            return {
                "name": name,
                "value": "休市",
                "change": f"前收 {last_date.strftime('%m/%d')}",
                "trend": "flat",
                "pct_num": 0.0,
                "date": last_date.strftime("%m/%d"),
            }

        prev_val = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else last_val
        pct = ((last_val - prev_val) / prev_val * 100.0) if prev_val else 0.0

        if is_yield:
            val_str = f"{last_val:.2f}%"
            diff_bp = (last_val - prev_val) * 100.0
            chg_str = f"{diff_bp:+.1f} bps"
        elif last_val >= 1000:
            val_str = f"{last_val:,.0f}"
            chg_str = f"{pct:+.2f}%"
        else:
            val_str = f"{last_val:,.2f}"
            chg_str = f"{pct:+.2f}%"

        trend = "up" if pct > 0.01 else ("down" if pct < -0.01 else "flat")
        return {
            "name": name,
            "value": val_str,
            "change": chg_str,
            "trend": trend,
            "pct_num": abs(pct),
            "date": last_date.strftime("%m/%d"),
        }
    except Exception as e:
        print(f"[WARN] Quote failed for {symbol} ({name}): {e}")
        return {
            "name": name,
            "value": "暫無報價",
            "change": "--",
            "trend": "flat",
            "pct_num": 0.0,
            "date": "--",
        }


def fetch_twse_spot():
    """證交所三大法人現貨買賣超（單位：億元）"""
    try:
        r = requests.get(
            "https://www.twse.com.tw/rwd/zh/fund/BFI82U?response=json",
            headers=HTTP_HEADERS,
            timeout=15,
        )
        r.raise_for_status()
        j = r.json()
        if j.get("stat") != "OK" or not j.get("data"):
            raise ValueError(f"TWSE stat={j.get('stat')}")

        d_raw = str(j.get("date", ""))
        d_fmt = f"{d_raw[4:6]}/{d_raw[6:8]}" if len(d_raw) == 8 else ""
        foreign_net = None
        total_net = None

        for row in j["data"]:
            name = str(row[0])
            diff = float(str(row[3]).replace(",", "")) / 1e8
            if "外資及陸資" in name and "不含" in name:
                foreign_net = diff
            elif "合計" in name:
                total_net = diff

        if foreign_net is None:
            raise ValueError("Foreign spot row not found")

        trend = "up" if foreign_net > 0 else ("down" if foreign_net < 0 else "flat")
        sub = f"三大法人 {total_net:+.1f} 億 ({d_fmt})" if total_net is not None else f"資料日 {d_fmt}"
        return {
            "label": "外資現貨買賣超",
            "value": f"{foreign_net:+.1f} 億",
            "sub": sub,
            "trend": trend,
        }
    except Exception as e:
        print(f"[WARN] TWSE spot failed: {e}")
        return {"label": "外資現貨買賣超", "value": "暫無資料", "sub": "證交所連線逾時", "trend": "flat"}


def fetch_taifex_foreign_oi():
    """期交所外資台指期未平倉淨口數"""
    try:
        end_d = now_tw.date()
        start_d = end_d - datetime.timedelta(days=7)
        payload = {
            "queryStartDate": start_d.strftime("%Y/%m/%d"),
            "queryEndDate": end_d.strftime("%Y/%m/%d"),
            "commodityId": "TXF",
        }
        r = requests.post(
            "https://www.taifex.com.tw/cht/3/futContractsDateDown",
            data=payload,
            headers=HTTP_HEADERS,
            timeout=15,
        )
        r.raise_for_status()
        text = r.content.decode("cp950", errors="ignore")
        rows = list(csv.reader(io.StringIO(text)))
        if len(rows) < 2:
            raise ValueError("Empty TAIFEX OI CSV")

        header = rows[0]
        net_idx = next((i for i, h in enumerate(header) if "多空未平倉口數淨額" in h), 13)
        latest_row = None
        for row in rows[1:]:
            if len(row) > net_idx and "臺股期貨" in row[1] and "外資" in row[2]:
                latest_row = row

        if not latest_row:
            raise ValueError("Foreign TXF OI row not found")

        net_oi = int(float(latest_row[net_idx].replace(",", "").strip()))
        date_parts = latest_row[0].split("/")
        d_fmt = f"{int(date_parts[1]):02d}/{int(date_parts[2]):02d}" if len(date_parts) == 3 else latest_row[0]
        trend = "up" if net_oi > 0 else ("down" if net_oi < 0 else "flat")
        status = "淨多單" if net_oi > 0 else ("淨空單" if net_oi < 0 else "中性")
        return {
            "label": "外資台指期未平倉",
            "value": f"{net_oi:+,} 口",
            "sub": f"{status} ({d_fmt})",
            "trend": trend,
        }
    except Exception as e:
        print(f"[WARN] TAIFEX OI failed: {e}")
        return {"label": "外資台指期未平倉", "value": "暫無資料", "sub": "期交所連線逾時", "trend": "flat"}


def fetch_taifex_night():
    """期交所台指期盤後（夜盤）最新報價"""
    try:
        payload = {
            "MarketType": "1",
            "SymbolType": "F",
            "KindID": "1",
            "CID": "TXF",
            "ExpireMonth": "",
            "RowSize": "全部",
            "PageNo": "",
            "SortColumn": "",
            "AscDesc": "A",
        }
        r = requests.post(
            "https://mis.taifex.com.tw/futures/api/getQuoteList",
            json=payload,
            headers={**HTTP_HEADERS, "Content-Type": "application/json"},
            timeout=15,
        )
        r.raise_for_status()
        qlist = r.json().get("RtData", {}).get("QuoteList", [])
        target = None
        for q in qlist:
            cname = str(q.get("DispCName", ""))
            last_p = str(q.get("CLastPrice", "")).strip()
            if "臺股期貨" in cname and "/" not in cname and "現貨" not in cname and last_p not in ("", "-"):
                target = q
                break

        if not target:
            raise ValueError("No active night TXF quote")

        last_val = float(target["CLastPrice"])
        diff_val = float(target["CDiff"])
        pct_val = float(target["CDiffRate"])
        trend = "up" if diff_val > 0 else ("down" if diff_val < 0 else "flat")
        return {
            "label": "台指期夜盤",
            "value": f"{diff_val:+,.0f} 點",
            "sub": f"{last_val:,.0f} ({pct_val:+.2f}%)",
            "trend": trend,
        }
    except Exception as e:
        print(f"[WARN] TAIFEX night failed: {e}")
        return {"label": "台指期夜盤", "value": "休市／暫無", "sub": "非盤後或連線逾時", "trend": "flat"}


def fetch_all_market_data():
    return {
        "us_indices": [
            fetch_quote("^DJI", "道瓊工業", is_us=True),
            fetch_quote("^GSPC", "S&P 500", is_us=True),
            fetch_quote("^IXIC", "那斯達克", is_us=True),
            fetch_quote("^SOX", "費城半導體", is_us=True),
        ],
        "barometer": {
            "tsm": fetch_quote("TSM", "台積電 ADR", is_us=True),
            "nvda": fetch_quote("NVDA", "輝達 NVDA", is_us=True),
        },
        "asia": [
            fetch_quote("^TWII", "台灣加權", is_us=False),
            fetch_quote("^N225", "日經 225", is_us=False),
        ],
        "macro": [
            fetch_quote("^TNX", "10Y 美債殖利率", is_us=False, is_yield=True),
            fetch_quote("DX-Y.NYB", "美元指數 DXY", is_us=False),
            fetch_quote("^VIX", "VIX 恐慌指數", is_us=False),
        ],
        "tw_chips": [
            fetch_taifex_night(),
            fetch_twse_spot(),
            fetch_taifex_foreign_oi(),
        ],
    }


def fetch_rss_news(max_items=28):
    cutoff = now_utc - datetime.timedelta(hours=24)
    items = []
    seen_titles = set()

    for default_source, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:20]:
                raw_title = html.unescape(entry.get("title", "")).strip()
                if not raw_title or raw_title in seen_titles:
                    continue

                pub_struct = entry.get("published_parsed") or entry.get("updated_parsed")
                if pub_struct:
                    pub_dt = datetime.datetime.fromtimestamp(calendar.timegm(pub_struct), tz=datetime.timezone.utc)
                    if pub_dt < cutoff:
                        continue

                source = default_source
                title = raw_title
                if hasattr(entry, "source") and getattr(entry.source, "title", None):
                    source = entry.source.title
                if " - " in raw_title and default_source.startswith("Google"):
                    parts = raw_title.rsplit(" - ", 1)
                    title, source = parts[0].strip(), parts[1].strip()

                raw_summary = html.unescape(entry.get("summary", ""))
                clean_summary = re.sub(r"<[^>]+>", " ", raw_summary)
                clean_summary = re.sub(r"\s+", " ", clean_summary).strip()[:400]

                seen_titles.add(raw_title)
                items.append({
                    "title": title,
                    "source": source,
                    "summary": clean_summary,
                })
        except Exception as e:
            print(f"[WARN] RSS failed ({default_source}): {e}")

    return items[:max_items]


def fetch_auto_calendar():
    """三層全自動行事曆：ForexFactory(美國總經) + 台灣國定假日API + calendar.json備援"""
    today_d = now_tw.date()
    end_d = today_d + datetime.timedelta(days=7)
    events = []
    seen = set()

    # 1. ForexFactory 美國高影響力事件 & 休市
    for url in [
        "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
        "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
    ]:
        try:
            r = requests.get(url, headers=HTTP_HEADERS, timeout=12)
            r.raise_for_status()
            for ev in r.json():
                if ev.get("country") != "USD":
                    continue
                if ev.get("impact") not in ("High", "Holiday"):
                    continue
                d_raw = ev.get("date", "")
                if not d_raw:
                    continue
                dt_tw = datetime.datetime.fromisoformat(d_raw).astimezone(tz_tw)
                if today_d <= dt_tw.date() <= end_d:
                    t_label = "全日" if ev.get("impact") == "Holiday" else dt_tw.strftime("%H:%M")
                    entry_str = f"{dt_tw.strftime('%m/%d')} ({t_label}) [US] {ev.get('title', '').strip()}"
                    if entry_str not in seen:
                        seen.add(entry_str)
                        events.append(entry_str)
        except Exception as e:
            print(f"[WARN] ForexFactory calendar failed ({url}): {e}")

    # 2. 台灣政府開放資料國定假日 API
    years = {today_d.year, end_d.year}
    for y in years:
        try:
            r = requests.get(f"https://api.pin-yi.me/taiwan-calendar/{y}?isHoliday=true", headers=HTTP_HEADERS, timeout=10)
            r.raise_for_status()
            for item in r.json():
                desc = str(item.get("description") or "").strip()
                if not desc:
                    continue  # 略過一般週六日
                d_str = str(item.get("date", ""))
                if len(d_str) == 8:
                    ev_d = datetime.date(int(d_str[:4]), int(d_str[4:6]), int(d_str[6:8]))
                    if today_d <= ev_d <= end_d:
                        entry_str = f"{ev_d.strftime('%m/%d')} (全日) [TW] {desc}"
                        if entry_str not in seen:
                            seen.add(entry_str)
                            events.append(entry_str)
        except Exception as e:
            print(f"[WARN] Taiwan holiday API failed ({y}): {e}")

    # 3. 本地 calendar.json 選配備援
    cal_path = Path("calendar.json")
    if cal_path.exists():
        try:
            for ev in json.loads(cal_path.read_text(encoding="utf-8")):
                if "範例" in ev.get("event", ""):
                    continue
                ev_d = datetime.date.fromisoformat(ev.get("date", ""))
                if today_d <= ev_d <= end_d:
                    t_str = f" ({ev.get('time_tw')})" if ev.get("time_tw") else ""
                    entry_str = f"{ev_d.strftime('%m/%d')}{t_str} {ev.get('event', '')}".strip()
                    if entry_str not in seen:
                        seen.add(entry_str)
                        events.append(entry_str)
        except Exception as e:
            print(f"[WARN] calendar.json load failed: {e}")

    return sorted(events)


def fetch_tw_event_previews(max_items=12):
    """抓過去 5 天台灣本週大事／選舉／法說會／央行預告新聞，供 AI 擷取明確日期事件"""
    cutoff = now_utc - datetime.timedelta(days=5)
    previews = []
    seen = set()
    try:
        feed = feedparser.parse(TW_EVENT_PREVIEW_RSS)
        for entry in feed.entries[:20]:
            raw_title = html.unescape(entry.get("title", "")).strip()
            if not raw_title or raw_title in seen:
                continue
            pub_struct = entry.get("published_parsed") or entry.get("updated_parsed")
            if pub_struct:
                pub_dt = datetime.datetime.fromtimestamp(calendar.timegm(pub_struct), tz=datetime.timezone.utc)
                if pub_dt < cutoff:
                    continue
            raw_summary = html.unescape(entry.get("summary", ""))
            clean_summary = re.sub(r"<[^>]+>", " ", raw_summary)
            clean_summary = re.sub(r"\s+", " ", clean_summary).strip()[:240]
            seen.add(raw_title)
            previews.append(f"{raw_title}｜{clean_summary}")
    except Exception as e:
        print(f"[WARN] TW preview RSS failed: {e}")
    return previews[:max_items]


def build_prompt(market, news, structured_cal, tw_previews):
    news_lines = "\n".join(
        f"{idx+1}. [{n['source']}] {n['title']}｜摘要：{n['summary']}"
        for idx, n in enumerate(news)
    ) or "今日暫無抓到 24 小時內 RSS 新聞。"

    preview_lines = "\n".join(f"- {p}" for p in tw_previews) or "無"

    return f"""
你是一位客觀中立的資深財經主編。今天是台灣時間 {today_str}（週{weekday_str}）。
以下所有行情數字、台股籌碼、行事曆與新聞清單均由程式即時抓取，請「嚴格只依據提供資料」產出深度濃縮晨報：

【核心規範】
1. 嚴禁自行編造或補充任何未出現在資料中的數字、點位、稅率、日期或事件。
2. 語氣純陳述事實，不做買賣建議、不做多空預測，禁用「史詩級」「崩盤」「暴漲暴跌」等煽動字眼。
3. 請將相關的多則新聞「整合成最多 3 個完整主題」，清楚交代「具體發生什麼事（事實）」與「對台美股供應鏈或板塊的傳導路徑（影響）」。
4. 未來 7 天行事曆（calendar_events）：請整合下方【自動抓取總經與休市行事曆】，並從【台灣焦點預告新聞】中擷取「未來 7 天內且有明確日期」的台灣重大事件（例如選舉、央行會議、重量級法說會、除息等），統一翻成精簡繁體中文，按日期排序，最多列 4 項（格式：`MM/DD (時間或全日) 事件簡稱`）；若新聞未寫明確日期切勿猜測。
5. 盲點觀察（blindspot）請結合「美股／殖利率／VIX／台股外資籌碼」之間的背離或結構性風險撰寫。

【行情與籌碼數據】
{json.dumps(market, ensure_ascii=False)}

【過去 24 小時財經新聞（多源彙整）】
{news_lines}

【自動抓取總經與休市行事曆（未來 7 天）】
{json.dumps(structured_cal, ensure_ascii=False)}

【台灣焦點預告新聞（供擷取未來 7 天明確日期事件）】
{preview_lines}

請只輸出一段合法 JSON（用 ```json 與 ``` 包覆）：
{{
  "regime_tag": "4-6字客觀現象標籤",
  "one_line_summary": "一句話客觀總結昨夜市場主軸與籌碼氛圍（40字內）",
  "top_news": [
    {{
      "source": "主要媒體名稱（如 經濟日報 / CNBC）",
      "title": "整合後精簡主題標題（20字內）",
      "fact": "整合多篇報導的具體客觀事實（65字內）",
      "impact": "對台美股板塊與供應鏈的傳導邏輯（55字內）"
    }}
  ],
  "calendar_events": [
    "MM/DD (時間或全日) 繁中精簡事件"
  ],
  "blindspot": "1句結合行情與外資籌碼的結構性風險／容易忽略的訊號（65字內）"
}}
"""


def generate_ai_summary(market, news, structured_cal, tw_previews):
    client = genai.Client()
    prompt = build_prompt(market, news, structured_cal, tw_previews)

    for model_name in MODELS:
        for attempt in range(2):
            try:
                print(f"Calling {model_name} (attempt {attempt + 1})...")
                resp = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(temperature=0.2),
                )
                text = (resp.text or "").strip()
                if not text:
                    raise ValueError("Empty AI response")
                match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
                json_str = match.group(1) if match else text[text.find("{"):text.rfind("}") + 1]
                parsed = json.loads(json_str)
                print(f"AI summary generated by {model_name}")
                return parsed, False
            except Exception as e:
                print(f"[WARN] {model_name} attempt {attempt + 1} failed: {e}")
                time.sleep(5 * (attempt + 1))

    print("[FALLBACK] AI unavailable, generating pure-data card.")
    fallback_news = [
        {
            "source": n["source"],
            "title": n["title"][:24],
            "fact": n["summary"][:60] or "請參閱各媒體原始報導。",
            "impact": "（AI 摘要暫時忙碌，此為原始快訊直出）",
        }
        for n in news[:3]
    ]
    return {
        "regime_tag": "純數據速報",
        "one_line_summary": "AI 摘要暫時離線，已自動切換為即時數據與新聞標題直出模式。",
        "top_news": fallback_news,
        "calendar_events": structured_cal[:4],
        "blindspot": "今日為系統自動容錯模式，請直接參考上方最新收盤數據、外資籌碼與快訊。",
    }, True


def render_and_screenshot(payload):
    docs_dir = Path("docs")
    docs_dir.mkdir(exist_ok=True)

    template = Path("template.html").read_text(encoding="utf-8")
    html_content = template.replace("__BRIEF_DATA__", json.dumps(payload, ensure_ascii=False))
    rendered_html = docs_dir / "index.html"
    rendered_html.write_text(html_content, encoding="utf-8")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 680, "height": 100}, device_scale_factor=2)
        page.goto(f"file://{rendered_html.resolve()}")
        page.evaluate("document.fonts.ready")
        page.wait_for_timeout(500)
        card = page.locator("#brief-card")
        card.screenshot(path=str(docs_dir / "latest.png"))
        browser.close()

    meta = {
        "date": now_tw.strftime("%Y-%m-%d"),
        "timestamp": int(now_tw.timestamp()),
        "mode": payload.get("mode", "精簡版"),
        "ai_fallback": payload.get("ai_fallback", False),
    }
    (docs_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    market_data = fetch_all_market_data()
    rss_news = fetch_rss_news()
    structured_cal = fetch_auto_calendar()
    tw_previews = fetch_tw_event_previews()
    ai_data, is_fallback = generate_ai_summary(market_data, rss_news, structured_cal, tw_previews)

    big_move = any(i.get("pct_num", 0) >= 2.0 for i in market_data["us_indices"])
    mode = "深度版" if (now_tw.weekday() == 5 or big_move) else "精簡版"

    us_dates = sorted({i["date"] for i in market_data["us_indices"] if i["date"] != "--"})
    tw_date = market_data["asia"][0]["date"]
    data_basis = f"美股 {us_dates[-1] if us_dates else '--'} ｜ 台股 {tw_date}"

    cal_list = ai_data.get("calendar_events") or structured_cal[:4]

    payload = {
        "date_label": f"{today_str}（週{weekday_str}）",
        "mode": mode,
        "data_basis": data_basis,
        "regime_tag": ai_data.get("regime_tag", "盤前總覽"),
        "one_line_summary": ai_data.get("one_line_summary", ""),
        "us_indices": market_data["us_indices"],
        "barometer": market_data["barometer"],
        "asia": market_data["asia"],
        "macro": market_data["macro"],
        "tw_chips": market_data["tw_chips"],
        "top_news": ai_data.get("top_news", [])[:3],
        "calendar_list": cal_list[:4],
        "blindspot": ai_data.get("blindspot", ""),
        "ai_fallback": is_fallback,
    }

    render_and_screenshot(payload)
    print("Successfully generated docs/latest.png")
