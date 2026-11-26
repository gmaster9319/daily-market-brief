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
today_md = now_tw.strftime("%m/%d")
weekday_map = ["一", "二", "三", "四", "五", "六", "日"]
weekday_str = weekday_map[now_tw.weekday()]

MODELS = ["gemini-4-hydrogen", "gemini-3.8-flash", "gemini-3.5-flash-lite"]
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
}

RSS_FEEDS = [
    ("Google財經", "https://news.google.com/rss/search?q=%E5%8F%B0%E8%82%A1+OR+%E7%BE%8E%E8%82%A1+OR+%E8%B2%BB%E5%8D%8A+OR+%E8%81%AF%E6%BA%96%E6%9C%83+OR+PCE+OR+CPI&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"),
    ("Google科技", "https://news.google.com/rss/search?q=%E5%8F%B0%E7%A9%8D%E9%9B%BB+OR+%E8%BC%9D%E9%81%94+OR+AI%E4%BC%BA%E6%9C%8D%E5%99%A8+OR+%E9%97%9C%E7%A8%85+OR+%E5%A4%96%E8%B3%87&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"),
    ("Yahoo台股", "https://tw.stock.yahoo.com/rss?category=tw-market"),
    ("Yahoo國際", "https://tw.stock.yahoo.com/rss?category=intl-markets"),
    ("CNBC", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664"),
]

TW_EVENT_PREVIEW_RSS = (
    "https://news.google.com/rss/search?"
    "q=(%E5%8F%B0%E8%82%A1+OR+%E5%8F%B0%E7%81%A3)+AND+(%E6%9C%AC%E9%80%B1%E5%A4%A7%E4%BA%8B+OR+%E4%B8%8B%E9%80%B1%E7%84%A6%E9%BB%9E+OR+%E6%B3%95%E8%AA%AA%E6%9C%83+OR+%E9%81%B8%E8%88%89+OR+%E5%A4%AE%E8%A1%8C+OR+%E8%B2%A1%E7%B6%93%E8%A1%8C%E4%BA%8B%E6%9B%86)"
    "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
)


def classify_move(pct):
    if pct >= 2.0:
        return "大漲"
    if pct >= 0.5:
        return "收漲"
    if pct > 0.05:
        return "小漲"
    if pct <= -2.5:
        return "重挫"
    if pct <= -1.0:
        return "走弱"
    if pct < -0.05:
        return "小跌"
    return "平盤"


def fetch_quote(symbol, name, is_us=True, is_yield=False, show_pts=False, lock_prev_close_intraday=False):
    expected_us_date = (now_tw - datetime.timedelta(days=1)).date()
    try:
        hist = yf.Ticker(symbol).history(period="7d")
        if hist is None or hist.empty:
            raise ValueError("Empty history")
        hist = hist.dropna(subset=["Close"])
        if hist.empty:
            raise ValueError("No valid close prices")

        # 若在台股盤中（09:00-13:35）手動執行，略過尚未收盤的當日 K 棒，避免與昨日籌碼時間錯位
        if lock_prev_close_intraday and len(hist) >= 2:
            if hist.index[-1].date() == now_tw.date() and (now_tw.hour < 13 or (now_tw.hour == 13 and now_tw.minute < 35)):
                hist = hist.iloc[:-1]

        last_val = float(hist["Close"].iloc[-1])
        last_date = hist.index[-1].date()

        if is_us and (now_tw.weekday() in (0, 6) or last_date < expected_us_date):
            return {
                "name": name,
                "value": "休市",
                "change": f"前收 {last_date.strftime('%m/%d')}",
                "pts": "",
                "status": "休市",
                "trend": "flat",
                "is_extreme": False,
                "pct_num": 0.0,
                "date": last_date.strftime("%m/%d"),
            }

        prev_val = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else last_val
        diff = last_val - prev_val
        pct = (diff / prev_val * 100.0) if prev_val else 0.0

        if is_yield:
            val_str = f"{last_val:.2f}%"
            chg_str = f"{diff * 100.0:+.1f} bps"
            pts_str = ""
        else:
            val_str = f"{last_val:,.2f}"
            chg_str = f"{pct:+.2f}%"
            pts_str = f"({diff:+,.0f}點)" if (show_pts and abs(diff) >= 1) else ""

        trend = "up" if pct > 0.01 else ("down" if pct < -0.01 else "flat")
        return {
            "name": name,
            "value": val_str,
            "change": chg_str,
            "pts": pts_str,
            "status": classify_move(pct),
            "trend": trend,
            "is_extreme": abs(pct) >= 2.5,
            "pct_num": abs(pct),
            "date": last_date.strftime("%m/%d"),
        }
    except Exception as e:
        print(f"[WARN] Quote failed for {symbol} ({name}): {e}")
        return {
            "name": name,
            "value": "暫無報價",
            "change": "--",
            "pts": "",
            "status": "待更新",
            "trend": "flat",
            "is_extreme": False,
            "pct_num": 0.0,
            "date": "--",
        }


def fetch_twse_spot():
    """證交所三大法人現貨買賣超，拆出外資、投信、自營商（單位：億元）"""
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

        foreign_net = 0.0
        trust_net = 0.0
        dealer_net = 0.0
        total_net = None

        for row in j["data"]:
            name = str(row[0])
            diff = float(str(row[3]).replace(",", "")) / 1e8
            if "外資及陸資" in name and "不含" in name:
                foreign_net = diff
            elif "投信" in name:
                trust_net = diff
            elif "自營商" in name and "外資" not in name:
                dealer_net += diff
            elif "合計" in name:
                total_net = diff

        if total_net is None:
            total_net = foreign_net + trust_net + dealer_net

        def make_pill(label, val):
            t = "up" if val > 0.01 else ("down" if val < -0.01 else "flat")
            return {"label": label, "value": f"{val:+.1f}億", "trend": t}

        total_trend = "up" if total_net > 0 else ("down" if total_net < 0 else "flat")
        action = "買超" if total_net >= 0 else "賣超"
        return {
            "headline": f"合計{action} {abs(total_net):,.1f} 億",
            "trend": total_trend,
            "date": d_fmt,
            "breakdown": [
                make_pill("外資", foreign_net),
                make_pill("投信", trust_net),
                make_pill("自營商", dealer_net),
            ],
        }
    except Exception as e:
        print(f"[WARN] TWSE spot failed: {e}")
        return {
            "headline": "暫無資料",
            "trend": "flat",
            "date": "--",
            "breakdown": [],
        }


def fetch_taifex_foreign_oi():
    """期交所外資台指期未平倉淨口數（含自動重試與 20 秒逾時）"""
    end_d = now_tw.date()
    start_d = end_d - datetime.timedelta(days=7)
    payload = {
        "queryStartDate": start_d.strftime("%Y/%m/%d"),
        "queryEndDate": end_d.strftime("%Y/%m/%d"),
        "commodityId": "TXF",
    }
    for attempt in range(2):
        try:
            r = requests.post(
                "https://www.taifex.com.tw/cht/3/futContractsDateDown",
                data=payload,
                headers=HTTP_HEADERS,
                timeout=20,
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
            status = "淨多單" if net_oi > 0 else ("淨空單" if net_oi < 0 else "中性")
            is_alert = net_oi <= -40000

            if net_oi <= -100000:
                note = f"資料日 {d_fmt} ・ 突破十萬口警戒"
            elif net_oi <= -40000:
                note = f"資料日 {d_fmt} ・ 逾四萬口高空單警戒"
            else:
                note = f"資料日 {d_fmt} ・ 外資期貨{status}"

            return {
                "title": f"外資台指期{status}",
                "value": f"{abs(net_oi):,}",
                "unit": "口",
                "badge": "警戒" if is_alert else status,
                "is_alert": is_alert,
                "note": note,
                "raw_net": net_oi,
            }
        except Exception as e:
            print(f"[WARN] TAIFEX OI attempt {attempt + 1} failed: {e}")
            if attempt == 0:
                time.sleep(3)

    return {
        "title": "外資台指期未平倉",
        "value": "暫無",
        "unit": "",
        "badge": "待更新",
        "is_alert": False,
        "note": "期交所連線逾時",
        "raw_net": 0,
    }


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
            "value": f"{last_val:,.0f}",
            "unit": "點",
            "badge": f"{diff_val:+,.0f}點 ({pct_val:+.2f}%)",
            "trend": trend,
            "is_extreme": abs(pct_val) >= 2.0,
        }
    except Exception as e:
        print(f"[WARN] TAIFEX night failed: {e}")
        return {
            "value": "休市／待開盤",
            "unit": "",
            "badge": "暫無夜盤報價",
            "trend": "flat",
            "is_extreme": False,
        }


def fetch_all_market_data():
    return {
        "us_indices": [
            fetch_quote("^DJI", "道瓊工業", is_us=True, show_pts=True),
            fetch_quote("^GSPC", "S&P 500", is_us=True, show_pts=False),
            fetch_quote("^IXIC", "那斯達克", is_us=True, show_pts=False),
            fetch_quote("^SOX", "費城半導體", is_us=True, show_pts=True),
        ],
        "barometer": {
            "tsm": fetch_quote("TSM", "台積電 ADR (TSM)", is_us=True),
            "nvda": fetch_quote("NVDA", "輝達 (NVDA)", is_us=True),
        },
        "macro": [
            fetch_quote("^TNX", "10Y 美債殖利率", is_us=False, is_yield=True),
            fetch_quote("DX-Y.NYB", "美元指數 (DXY)", is_us=False),
            fetch_quote("^VIX", "VIX 恐慌指數", is_us=False),
        ],
        "tw_night": fetch_taifex_night(),
        "tw_index": fetch_quote("^TWII", "台灣加權指數", is_us=False, show_pts=True, lock_prev_close_intraday=True),
        "nikkei": fetch_quote("^N225", "日經 225", is_us=False, show_pts=False),
        "tw_spot": fetch_twse_spot(),
        "tw_oi": fetch_taifex_foreign_oi(),
    }


def fetch_rss_news(max_items=24):
    cutoff = now_utc - datetime.timedelta(hours=24)
    items = []
    seen_titles = set()

    for default_source, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:18]:
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
                clean_summary = re.sub(r"\s+", " ", clean_summary).strip()[:360]

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
    today_d = now_tw.date()
    end_d = today_d + datetime.timedelta(days=7)
    events = []
    seen = set()

    for url in [
        "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
        "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
    ]:
        try:
            r = requests.get(url, headers=HTTP_HEADERS, timeout=12)
            if r.status_code == 404:
                continue
            r.raise_for_status()
            for ev in r.json():
                if ev.get("country") != "USD" or ev.get("impact") not in ("High", "Holiday"):
                    continue
                d_raw = ev.get("date", "")
                if not d_raw:
                    continue
                dt_tw = datetime.datetime.fromisoformat(d_raw).astimezone(tz_tw)
                if today_d <= dt_tw.date() <= end_d:
                    is_today = (dt_tw.date() == today_d)
                    t_label = "全日" if ev.get("impact") == "Holiday" else dt_tw.strftime("%H:%M")
                    d_label = f"今日 {t_label}" if is_today else f"{dt_tw.strftime('%m/%d')} ({t_label})"
                    key = f"{dt_tw.strftime('%Y-%m-%d')}-{ev.get('title', '').strip()}"
                    if key not in seen:
                        seen.add(key)
                        events.append({
                            "sort_key": dt_tw.strftime("%Y-%m-%d %H:%M"),
                            "time_label": d_label,
                            "title": f"[US] {ev.get('title', '').strip()}",
                            "is_today": is_today,
                        })
        except Exception as e:
            print(f"[WARN] ForexFactory calendar failed ({url}): {e}")

    for y in {today_d.year, end_d.year}:
        try:
            r = requests.get(f"https://api.pin-yi.me/taiwan-calendar/{y}?isHoliday=true", headers=HTTP_HEADERS, timeout=10)
            r.raise_for_status()
            for item in r.json():
                desc = str(item.get("description") or "").strip()
                if not desc:
                    continue
                d_str = str(item.get("date", ""))
                if len(d_str) == 8:
                    ev_d = datetime.date(int(d_str[:4]), int(d_str[4:6]), int(d_str[6:8]))
                    if today_d <= ev_d <= end_d:
                        is_today = (ev_d == today_d)
                        key = f"{ev_d.isoformat()}-{desc}"
                        if key not in seen:
                            seen.add(key)
                            events.append({
                                "sort_key": f"{ev_d.isoformat()} 00:00",
                                "time_label": "今日 (全日)" if is_today else f"{ev_d.strftime('%m/%d')} (全日)",
                                "title": f"[TW] {desc}",
                                "is_today": is_today,
                            })
        except Exception as e:
            print(f"[WARN] Taiwan holiday API failed ({y}): {e}")

    cal_path = Path("calendar.json")
    if cal_path.exists():
        try:
            for ev in json.loads(cal_path.read_text(encoding="utf-8")):
                if "範例" in ev.get("event", ""):
                    continue
                ev_d = datetime.date.fromisoformat(ev.get("date", ""))
                if today_d <= ev_d <= end_d:
                    is_today = (ev_d == today_d)
                    t_str = ev.get("time_tw") or "全日"
                    key = f"{ev_d.isoformat()}-{ev.get('event', '')}"
                    if key not in seen:
                        seen.add(key)
                        events.append({
                            "sort_key": f"{ev_d.isoformat()} 00:00",
                            "time_label": f"今日 ({t_str})" if is_today else f"{ev_d.strftime('%m/%d')} ({t_str})",
                            "title": ev.get("event", "").strip(),
                            "is_today": is_today,
                        })
        except Exception as e:
            print(f"[WARN] calendar.json load failed: {e}")

    events.sort(key=lambda x: x["sort_key"])
    return events


def fetch_tw_event_previews(max_items=10):
    cutoff = now_utc - datetime.timedelta(days=5)
    previews = []
    seen = set()
    try:
        feed = feedparser.parse(TW_EVENT_PREVIEW_RSS)
        for entry in feed.entries[:18]:
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
            clean_summary = re.sub(r"\s+", " ", clean_summary).strip()[:220]
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
2. 嚴禁使用任何 Emoji 或表情符號。
3. 語氣純陳述事實，不做買賣建議、不做多空預測，禁用「史詩級」「崩盤」「暴漲暴跌」等煽動字眼。
4. 全球關鍵 3 大事（top_news）：請將相關多則新聞「整合成 3 個完整主題」，每則配一個 2-5 字英文大寫分類碼（如 US / TW / AI / SEMI / MACRO）。**嚴禁將「美股四大指數漲跌收盤總結」當成一則新聞**（指數已在上方呈現），3 則事件必須聚焦背後的實質驅動原因（如通膨 PCE/CPI、GDP、關稅政策、央行利率、科技巨頭或產業鏈動態）。
5. 未來 7 天行事曆（calendar_events）：整合下方自動抓取的總經行事曆，並從【台灣焦點預告新聞】中擷取「未來 7 天內且有明確日期」的台灣重大事件（選舉、央行、重要法說會等），統一翻成繁體中文，按時間順序最多列 4 項。**務必去重：若同一天出現意義重疊的休市或假期（例如「美國感恩節」與「美國銀行假期」），請合併為一筆，勿重複列出**。若事件在今天（{today_md}），is_today 設為 true。
6. 盲點與反面視角（blindspots）：固定產出「2 則」結構化深度觀察（每則含短標題與 1-2 句說明），結合美股分化、殖利率、VIX 與台股法人／期貨籌碼的背離訊號，並嚴格核對各數據的資料日期，勿混淆不同交易日。

【行情與籌碼數據】
{json.dumps(market, ensure_ascii=False)}

【過去 24 小時財經新聞】
{news_lines}

【自動抓取總經與休市行事曆（未來 7 天）】
{json.dumps(structured_cal, ensure_ascii=False)}

【台灣焦點預告新聞】
{preview_lines}

請只輸出一段合法 JSON（不要加 markdown 標記以外的多餘說明）：
{{
  "regime_tag": "4-6字客觀現象標籤",
  "premarket_focus": "盤前焦點一句話總結昨夜盤勢主軸與台股觀察重點（50字內）",
  "top_news": [
    {{
      "tag": "US 或 TW 或 AI 或 SEMI 或 MACRO",
      "source": "媒體名稱",
      "title": "整合後精簡主題標題（22字內）",
      "fact": "整合多篇報導的具體客觀事實（60字內）",
      "impact": "受影響板塊與供應鏈傳導邏輯（55字內）"
    }}
  ],
  "calendar_events": [
    {{
      "time_label": "今日 21:30 或 11/27 (五) 等簡潔時間標註",
      "title": "繁體中文事件名稱（勿加 [US]/[TW] 前綴）",
      "is_today": false
    }}
  ],
  "blindspots": [
    {{
      "title": "盲點短標題（14字內）",
      "detail": "具體數據佐證與反面結構視角（65字內）"
    }}
  ]
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
                    config=types.GenerateContentConfig(
                        temperature=0.2,
                        response_mime_type="application/json",
                    ),
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
                time.sleep(4 * (attempt + 1))

    print("[FALLBACK] AI unavailable, generating pure-data card.")
    fallback_news = [
        {
            "tag": f"0{idx+1}",
            "source": n["source"],
            "title": n["title"][:26],
            "fact": n["summary"][:65] or "請參閱各媒體原始報導。",
            "impact": "AI 摘要服務暫時忙碌，目前以原始財經快訊直出。",
        }
        for idx, n in enumerate(news[:3])
    ]
    fallback_cal = [
        {
            "time_label": c["time_label"],
            "title": c["title"],
            "is_today": c["is_today"],
        }
        for c in structured_cal[:4]
    ]
    return {
        "regime_tag": "純數據速報",
        "premarket_focus": "AI 摘要服務暫時忙碌，已自動切換為即時市場數據、法人籌碼與原始快訊直出模式。",
        "top_news": fallback_news,
        "calendar_events": fallback_cal,
        "blindspots": [
            {
                "title": "系統容錯模式啟動",
                "detail": "因模型端高流量暫時無法回應，今日由程式直接輸出最新行情、三大法人與期貨未平倉數據。",
            }
        ],
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
        page = browser.new_page(viewport={"width": 760, "height": 100}, device_scale_factor=2)
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
    mode_label = "重大事件日・深度版" if (now_tw.weekday() == 5 or big_move) else "常態盤前・精簡版"
    is_deep = (now_tw.weekday() == 5 or big_move)

    us_dates = sorted({i["date"] for i in market_data["us_indices"] if i["date"] != "--"})
    tw_date = market_data["tw_index"]["date"]
    data_basis = f"美股 {us_dates[-1] if us_dates else '--'} 收盤 ＆ 台股 {tw_date} 盤後／夜盤"

    cal_events = ai_data.get("calendar_events") or [
        {"time_label": c["time_label"], "title": c["title"], "is_today": c["is_today"]}
        for c in structured_cal[:4]
    ]

    payload = {
        "title_date": f"{today_str} ({weekday_str})",
        "mode_label": mode_label,
        "is_deep": is_deep,
        "data_basis": data_basis,
        "tw_date": tw_date,
        "regime_tag": ai_data.get("regime_tag", "盤前總覽"),
        "premarket_focus": ai_data.get("premarket_focus", ""),
        "us_indices": market_data["us_indices"],
        "barometer": market_data["barometer"],
        "macro": market_data["macro"],
        "tw_night": market_data["tw_night"],
        "tw_index": market_data["tw_index"],
        "nikkei": market_data["nikkei"],
        "tw_spot": market_data["tw_spot"],
        "tw_oi": market_data["tw_oi"],
        "top_news": ai_data.get("top_news", [])[:3],
        "calendar_events": cal_events[:4],
        "blindspots": ai_data.get("blindspots", [])[:2],
        "ai_fallback": is_fallback,
    }

    render_and_screenshot(payload)
    print("Successfully generated docs/latest.png")
