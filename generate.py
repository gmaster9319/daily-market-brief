import time
from pathlib import Path
from google import genai

client = genai.Client()
MODELS = ["gemini-4-hydrogen", "gemini-3.8-flash"]
results = []

for i, model_name in enumerate(MODELS):
    if i > 0:
        time.sleep(2)
    try:
        resp = client.models.generate_content(
            model=model_name,
            contents="請只回覆 TEST_OK",
        )
        msg = f"[PASS] {model_name} -> {(resp.text or '').strip()}"
        print(msg)
        results.append(msg)
    except Exception as e:
        print(f"[FAIL] {model_name} -> {e}")

if not results:
    raise RuntimeError("所有模型純文字測試皆失敗，請截圖上方 [FAIL] 訊息。")

docs_dir = Path("docs")
docs_dir.mkdir(exist_ok=True)
(docs_dir / "test_result.txt").write_text("\n".join(results), encoding="utf-8")
print("TEST_OK 驗證通過！")
