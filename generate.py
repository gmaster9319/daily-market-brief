import os
import re
import json
import datetime
from pathlib import Path
from google import genai
from google.genai import types
from playwright.sync_api import sync_playwright

# 取得台灣時間 (UTC+8)
tz_tw = datetime.timezone(datetime.timedelta(hours=8))
now_tw = datetime.datetime.now(tz_tw)
today_str = now_tw.strftime("%Y/%m/%d")
weekday_map = ["一", "二", "三", "四", "五", "六", "日"]
weekday_str = weekday_map[now_tw.weekday()]

PROMPT = f"""
你是一位客觀中立的財經主編。今天是台灣時間 {today_str}（週{weekday_str}）盤前。
請務必使用 Google 搜尋查證「最新一個交易日」的美股、台股與全球總經數據，並嚴格遵守以下零幻覺鐵律：
1. 所有點位、漲跌幅、殖利率與新聞必須標註所屬日期，嚴禁憑記憶編造。
2. 若為週一或美股休市日，美股指數欄位請直接填「休市」，絕不拿前一日舊數字混充。
3. 若查無交叉驗證的確切數字，請填「查無最新報價」；若無重大公告，請填「今日無重大公告」。
4. 週六或遇重大事件（FOMC、CPI、非農、重大關稅或主要指數單日漲跌逾 2%），mode 標註為「深度版」，其餘平日為「精簡版」。

請只輸出一段合法的 JSON（用 ```json 與 ``` 包覆），格式如下：
{{
  "date_label": "{today_str}（週{weekday_str}）",
  "mode": "精簡版 或 深度版",
  "data_basis": "例如：11/24 美股收盤 / 台股盤後",
  "us_indices": [
    {{"name": "道瓊工業", "value": "點位或休市", "change": "+0.00%", "trend": "up/down/flat"}},
    {{"name": "S&P 500", "value": "點位或休市", "change": "+0.00%", "trend": "up/down/flat"}},
    {{"name": "那斯達克", "value": "點位或休市", "change": "+0.00%", "trend": "up/down/flat"}},
    {{"name": "費城半導體", "value": "點位或休市", "change": "+0.00%", "trend": "up/down/flat"}}
  ],
  "barometer": {{
    "tsm": {{"change": "+0.00% 或 休市", "trend": "up/down/flat"}},
    "nvda": {{"change": "+0.00% 或 休市", "trend": "up/down/flat"}}
  }},
  "macro": [
    {{"name": "10Y 美債殖利率", "value": "例如 4.50%"}},
    {{"name": "美元指數 DXY", "value": "例如 104.2"}},
    {{"name": "VIX 恐慌指數", "value": "例如 18.5"}}
  ],
  "top_news": [
    {{
      "title": "精簡標題（18字內）",
      "fact": "客觀事實（45字內）",
      "impact": "傳導邏輯與受影響板塊（45字內）"
    }}
  ],
  "tw_premarket": {{
    "night_futures": "例如：台指期夜盤 -150 點（或休市）",
    "foreign_flow": "例如：外資現貨賣超 120 億｜期貨淨空單 3.5 萬口",
    "focus": "今日影響大盤權值之公告或重點（35字內）"
  }},
  "calendar": "今晚或本週最重要數據與台灣時間（40字內）",
  "blindspot": "1 句市場主流敘事可能忽略的結構性風險或籌碼異常（55字內）"
}}
注意：top_news 最多 3 則，所有文字保持精煉好讀，適合在手機圖卡上閱讀。
"""

def pick_available_model(client):
    candidates = []
    for m in client.models.list():
        name = getattr(m, "name", "")
        actions = getattr(m, "supported_actions", None) or getattr(m, "supported_generation_methods", None) or []
        if "generateContent" in actions and "gemini" in name.lower():
            short_name = name.replace("models/", "")
            if not any(bad in short_name.lower() for bad in ["tts", "image", "audio", "embedding", "vision", "veo", "robotics", "computer-use"]):
                candidates.append(short_name)
    print("Available Gemini models:", candidates)
    flash_models = sorted([c for c in candidates if "flash" in c.lower() and "lite" not in c.lower()], reverse=True)
    if flash_models:
        return flash_models[0]
    if candidates:
        return sorted(candidates, reverse=True)[0]
    raise RuntimeError("No available Gemini models found for this API key.")

def fetch_brief_data():
    client = genai.Client()
    model_name = pick_available_model(client)
    print("Selected model:", model_name)
    response = client.models.generate_content(
        model=model_name,
        contents=PROMPT,
        config=types.GenerateContentConfig(
            tools=[{"google_search": {}}],
            temperature=0.2,
        ),
    )
    text = response.text
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    json_str = match.group(1) if match else text[text.find("{"):text.rfind("}") + 1]
    return json.loads(json_str)

def render_and_screenshot(data):
    docs_dir = Path("docs")
    docs_dir.mkdir(exist_ok=True)

    template = Path("template.html").read_text(encoding="utf-8")
    html_content = template.replace("__BRIEF_DATA__", json.dumps(data, ensure_ascii=False))
    rendered_html = docs_dir / "index.html"
    rendered_html.write_text(html_content, encoding="utf-8")

    # 透過 Playwright 渲染並輸出 2x 高清 PNG
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 680, "height": 100}, device_scale_factor=2)
        page.goto(f"file://{rendered_html.resolve()}")
        page.evaluate("document.fonts.ready")
        page.wait_for_timeout(500)
        card = page.locator("#brief-card")
        card.screenshot(path=str(docs_dir / "latest.png"))
        browser.close()

    # 寫入 meta.json 供 LINE 機器人避開圖片快取
    meta = {
        "date": now_tw.strftime("%Y-%m-%d"),
        "timestamp": int(now_tw.timestamp()),
        "mode": data.get("mode", "精簡版"),
    }
    (docs_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

if __name__ == "__main__":
    brief_data = fetch_brief_data()
    render_and_screenshot(brief_data)
    print("Successfully generated docs/latest.png")
