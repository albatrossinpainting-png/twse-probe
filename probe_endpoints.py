"""探測「沒有歷史」的五個來源端點，從「這台機器的 IP」打得通嗎。

## 這支程式要回答的問題

停券／除權息預告（`update_forecast.py`）和集保股權分散
（`update_shareholding.py`）必須天天跑，但家用電腦不會天天開機。
候選方案是 GitHub Actions（免費、有排程），但**證交所常擋資料中心 IP**，
而 GitHub Actions 走的是 Azure 的 IP 段。

在蓋任何東西之前先確認這件事。做法是**同一支程式跑兩次**：

    本機（家用 IP）  → 基準線，已知可通
    GitHub Actions（Azure IP）→ 要驗證的

兩邊結果一比就知道是不是被擋。**這是受控比較，不是猜測。**

## 刻意的設計

* **不依賴這個專案的任何東西**，只用 `requests`。可以整支複製到別的 repo。
* **每個端點打兩次**：一次帶瀏覽器 User-Agent、一次不帶。
  被擋的原因是 IP 還是 UA，這樣才分得出來——只打一次的話會混在一起。
* **不只看狀態碼。** 交易所被擋時常常還是回 200，裡面是一頁 HTML 錯誤頁。
  所以要驗內容的形狀，並把開頭幾個字印出來。

## 用法

    python probe_endpoints.py       # 只要有 requests 就能跑，不需要別的東西

任何一個端點失敗就回傳非零離開碼，GitHub Actions 上會直接顯示紅燈。

⚠️ 每次請求之間固定間隔 5 秒（`SLEEP`），跟 `crawler.DEFAULT_SLEEP` 一致。
全程約 50 秒。**不要調低**——把家裡的 IP 弄進證交所黑名單會連正常爬蟲一起斷。
"""

import json
import sys
import time

import requests
import urllib3

urllib3.disable_warnings()
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 證交所是「每個 IP 幾秒一次」，連打會被列進黑名單。這支程式總共只打 10 次，
# 但寧可慢也不要把**家裡的 IP** 弄進黑名單——那會連正常爬蟲一起斷掉。
SLEEP = 5

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# (代號, 網址, 期望的內容形狀, 要不要驗 TLS)
ENDPOINTS = [
    ("TWSE 停資停券預告",
     "https://www.twse.com.tw/rwd/zh/marginTrading/BFI84U?response=json", "twse", True),
    ("TWSE 除權息預告",
     "https://www.twse.com.tw/rwd/zh/exRight/TWT48U?response=json", "twse", True),
    ("TPEX 暫停融券賣出",
     "https://www.tpex.org.tw/openapi/v1/tpex_margin_trading_term", "list", True),
    ("TPEX 除權息預告",
     "https://www.tpex.org.tw/openapi/v1/tpex_exright_prepost", "list", True),
    # 集保不帶 User-Agent 會被丟進轉址迴圈，而且憑證鏈有問題，沿用正式程式的 verify=False
    ("TDCC 集保股權分散",
     "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5", "csv", False),
]


def outbound_ip():
    """印出這台機器對外的 IP。兩邊比對時這是最重要的一行。"""
    for url in ("https://api.ipify.org", "https://checkip.amazonaws.com"):
        try:
            return requests.get(url, timeout=15).text.strip()
        except Exception:
            continue
    return "（查不到）"


def looks_like_data(shape, body_bytes):
    """驗內容形狀。被擋時常常是 200 + 一頁 HTML，光看狀態碼會誤判。"""
    text = body_bytes.decode("utf-8-sig", errors="replace")
    head = text.lstrip()[:200].replace("\n", " ").replace("\r", "")
    if head[:1] == "<":
        return False, "回的是 HTML，多半是錯誤頁或 WAF", head
    try:
        if shape == "twse":
            j = json.loads(text)
            ok = isinstance(j, dict) and "data" in j
            n = len(j.get("data") or []) if ok else 0
            return ok, ("data 有 {} 列".format(n) if ok else "JSON 裡沒有 data 欄"), head
        if shape == "list":
            j = json.loads(text)
            ok = isinstance(j, list)
            return ok, ("陣列 {} 筆".format(len(j)) if ok else "不是 JSON 陣列"), head
        if shape == "csv":
            ok = "證券代號" in text[:500] or "資料日期" in text[:500]
            return ok, ("CSV 表頭正確" if ok else "CSV 表頭對不上"), head
    except json.JSONDecodeError as e:
        return False, "JSON 解析失敗：{}".format(e), head
    return False, "未知形狀", head


def probe(name, url, shape, verify, ua):
    headers = {"Accept": "*/*", "Connection": "keep-alive"}
    if ua:
        headers["User-Agent"] = BROWSER_UA
    label = "帶 UA " if ua else "無 UA "
    t0 = time.time()
    try:
        r = requests.get(url, headers=headers, timeout=45, verify=verify)
    except Exception as e:
        print("  {} ✗ 連線失敗 {}：{}".format(label, type(e).__name__, str(e)[:120]))
        return False
    ms = (time.time() - t0) * 1000
    ok, why, head = looks_like_data(shape, r.content)
    mark = "✓" if (r.status_code == 200 and ok) else "✗"
    print("  {} {} HTTP {}  {:>6.0f}ms  {:>8,} bytes  {}".format(
        label, mark, r.status_code, ms, len(r.content), why))
    if mark == "✗":
        print("      開頭：{}".format(head[:150]))
    return r.status_code == 200 and ok


def main():
    print("=" * 78)
    print("對外 IP：{}".format(outbound_ip()))
    print("=" * 78)

    results = {}
    for name, url, shape, verify in ENDPOINTS:
        print("\n{}".format(name))
        print("  {}".format(url))
        with_ua = probe(name, url, shape, verify, ua=True)
        time.sleep(SLEEP)
        no_ua = probe(name, url, shape, verify, ua=False)
        time.sleep(SLEEP)
        results[name] = (with_ua, no_ua)

    print("\n" + "=" * 78)
    print("結論")
    print("=" * 78)
    bad = []
    for name, (with_ua, no_ua) in results.items():
        if with_ua:
            note = "可用" if no_ua else "可用（但一定要帶 User-Agent）"
        else:
            note = "**打不通**"
            bad.append(name)
        print("  {:<22} {}".format(name, note))

    if bad:
        print("\n打不通的：{}".format("、".join(bad)))
        print("如果本機跑得通、這裡跑不通，就是 IP 被擋——這個方案不能用。")
        return 1
    print("\n五個端點全通。從這台機器的 IP 跑每日更新沒問題。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
