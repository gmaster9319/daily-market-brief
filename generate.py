import re
import html
import json
import time
import calendar
import datetime
from pathlib import Path
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

RSS_FEEDS = [
    ("Google財經", "https://news.google.com/rss/search?q=%E5%8F%B0%E8%82%A1+OR+%E7%BE%8E%E8%82%A1+OR+%E8%B2%BB%E5%8D%8A+OR+%E8%81%AF%E6%BA%96%E6%9C%83&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"),
    ("CNBC", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664"),
]


def fetch_quote(symbol, name, is_us=True, is_yield=False):
    """獨立抓取單一標的，內建休市日期比對與防呆"""
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

        # 美股休市判斷：台灣週日/週一早上，或最新資料日期早於昨日
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


def fetch_all_market_data():
    us_indices = [
        fetch_quote("^DJI", "道瓊工業", is_us=True),
        fetch_quote("^GSPC", "S&P 500", is_us=True),
        fetch_quote("^IXIC", "那斯達克", is_us=True),
        fetch_quote("^SOX", "費城半導體", is_us=True),
    ]
    barometer = {
        "tsm": fetch_quote("TSM", "台積電 ADR", is_us=True),
        "nvda": fetch_quote("NVDA", "輝達 NVDA", is_us=True),
    }
    asia = [
        fetch_quote("^TWII", "台灣加權", is_us=False),
        fetch_quote("^N225", "日經 225", is_us=False),
    ]
    macro = [
        fetch_quote("^TNX", "10Y 美債殖利率", is_us=False, is_yield=True),
        fetch_quote("DX-Y.NYB", "美元指數 DXY", is_us=False),
        fetch_quote("^VIX", "VIX 恐慌指數", is_us=False),
    ]
    return {
        "us_indices": us_indices,
        "barometer": barometer,
        "asia": asia,
        "macro": macro,
    }


def fetch_rss_news(max_items=18):
    cutoff = now_utc - datetime.timedelta(hours=24)
    items = []
    seen_titles = set()

    for default_source, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:25]:
                raw_title = html.unescape(entry.get("title", "")).strip()
                if not raw_title or raw_title in seen_titles:
                    continue

                # 過濾超過 24 小時的新聞
                pub_struct = entry.get("published_parsed") or entry.get("updated_parsed")
                if pub_struct:
                    pub_dt = datetime.datetime.fromtimestamp(calendar.timegm(pub_struct), tz=datetime.timezone.utc)
                    if pub_dt < cutoff:
                        continue

                # 拆出媒體名稱
                source = default_source
                title = raw_title
                if hasattr(entry, "source") and getattr(entry.source, "title", None):
                    source = entry.source.title
                if " - " in raw_title and default_source == "Google財經":
                    parts = raw_title.rsplit(" - ", 1)
                    title, source = parts[0].strip(), parts[1].strip()

                raw_summary = html.unescape(entry.get("summary", ""))
                clean_summary = re.sub(r"<[^>]+>", " ", raw_summary)
                clean_summary = re.sub(r"\s+", " ", clean_summary).strip()[:160]

                seen_titles.add(raw_title)
                items.append({
                    "title": title,
                    "source": source,
                    "summary": clean_summary,
                    "link": entry.get("link", ""),
                })
        except Exception as e:
            print(f"[WARN] RSS failed ({default_source}): {e}")

    return items[:max_items]


def load_upcoming_calendar():
    cal_path = Path("calendar.json")
    if not cal_path.exists():
        return []
    try:
        events = json.loads(cal_path.read_text(encoding="utf-8"))
        today_d = now_tw.date()
        end_d = today_d + datetime.timedelta(days=7)
        upcoming = []
        for ev in events:
            d_str = ev.get("date", "")
            if "範例" in ev.get("event", ""):
                continue
            try:
                ev_d = datetime.date.fromisoformat(d_str)
                if today_d <= ev_d <= end_d:
                    upcoming.append(f"{ev_d.strftime('%m/%d')} ({ev.get('time_tw', '')}) {ev.get('event', '')}".strip())
            except ValueError:
                continue
        return upcoming
    except Exception as e:
        print(f"[WARN] calendar.json load failed: {e}")
        return []


def build_prompt(market, news, upcoming_events):
    news_lines = "\n".join(
        f"{idx+1}. [{n['source']}] {n['title']}｜摘要：{n['summary']}"
        for idx, n in enumerate(news)
    ) or "今日暫無抓到 24 小時內 RSS 新聞。"

    return f"""
你是一位客觀中立的財經編輯。今天是台灣時間 {today_str}（週{weekday_str}）。
以下所有行情數字與新聞清單均由程式抓取，請「嚴格只依據以下提供資料」產出晨報摘要：

【核心規範】
1. 嚴禁自行編造或補充任何未出現在資料中的數字、點位或事件。
2. 語氣純陳述事實，不做買賣建議、不做多空預測，禁用「史詩級」「崩盤」「暴漲暴跌」等煽動字眼。
3. 從下方新聞中挑出對台美股影響最直接的「最多 3 則」，每則附上媒體名稱，事實與影響各 1 句、精煉好讀。
4. 盤勢定調（regime_tag）請用客觀現象詞（例如：科技領漲、避險升溫、震盪整理、美股休市），不要寫買進或賣出。

【行情數據】
{json.dumps(market, ensure_ascii=False)}

【過去 24 小時財經新聞】
{news_lines}

【未來 7 天已登錄行事曆】
{json.dumps(upcoming_events, ensure_ascii=False)}

請只輸出一段合法 JSON（用 ```json 與 ``` 包覆）：
{{
  "regime_tag": "4-6字客觀現象標籤",
  "one_line_summary": "一句話客觀總結昨夜市場主軸（35字內）",
  "top_news": [
    {{
      "source": "媒體名稱",
      "title": "精簡標題（18字內）",
      "fact": "客觀事實（45字內）",
      "impact": "傳導與關注板塊（40字內）"
    }}
  ],
  "blindspot": "1句基於上述數據或新聞的結構性風險／容易忽略的訊號（55字內）"
}}
"""


def generate_ai_summary(market, news, upcoming_events):
    client = genai.Client()
    prompt = build_prompt(market, news, upcoming_events)

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

    # 全數失敗時啟用純數字＋新聞標題容錯版
    print("[FALLBACK] AI unavailable, generating pure-data card.")
    fallback_news = [
        {
            "source": n["source"],
            "title": n["title"][:24],
            "fact": n["summary"][:48] or "點擊網頁版查看原新聞內容。",
            "impact": "（AI 摘要服務暫時忙碌，此為原始快訊直出）",
        }
        for n in news[:3]
    ]
    return {
        "regime_tag": "純數據速報",
        "one_line_summary": "AI 摘要暫時離線，已自動切換為即時數據與新聞標題直出模式。",
        "top_news": fallback_news,
        "blindspot": "今日為系統自動容錯模式，請直接參考上方最新收盤數據與各媒體快訊。",
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
    upcoming_cal = load_upcoming_calendar()
    ai_data, is_fallback = generate_ai_summary(market_data, rss_news, upcoming_cal)

    # 週六或美股主要指數單日波動 >= 2% 自動標記為深度版
    big_move = any(i.get("pct_num", 0) >= 2.0 for i in market_data["us_indices"])
    mode = "深度版" if (now_tw.weekday() == 5 or big_move) else "精簡版"

    us_dates = sorted({i["date"] for i in market_data["us_indices"] if i["date"] != "--"})
    tw_date = market_data["asia"][0]["date"]
    data_basis = f"美股 {us_dates[-1] if us_dates else '--'} ｜ 台股 {tw_date}"

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
        "top_news": ai_data.get("top_news", [])[:3],
        "calendar_list": upcoming_cal,
        "blindspot": ai_data.get("blindspot", ""),
        "ai_fallback": is_fallback,
    }

    render_and_screenshot(payload)
    print("Successfully generated docs/latest.png")
