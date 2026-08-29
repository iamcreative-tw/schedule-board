#!/usr/bin/env python3
"""
從 Notion「檔期活動看板」資料庫抓資料，產生公開版 index.html。

重要：這支程式會強制清空「營業目標」，公開網頁絕對不會出現金額。
      這是寫死的，不是設定選項——不要改掉 strip_goal()。
"""

import io
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

DATABASE_ID = "28e3e58d-95ec-818c-8bf4-c0724fd31e1e"
NOTION_VERSION = "2022-06-28"
TEMPLATE = "template.html"
OUTPUT = "index.html"

def get_token():
    t = os.environ.get("NOTION_TOKEN", "").strip()
    if not t:
        sys.exit("❌ 找不到 NOTION_TOKEN。請確認 GitHub Secrets 裡有設定這個名稱。")
    return t


def notion_post(path, payload):
    req = urllib.request.Request(
        "https://api.notion.com/v1/" + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + get_token(),
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        # 只印錯誤內容，絕不印出 token
        sys.exit(f"❌ Notion API 回應 {e.code}\n{body}")


def fetch_rows():
    """抓完整資料庫，自動處理分頁。"""
    rows, cursor = [], None
    while True:
        payload = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        data = notion_post(f"databases/{DATABASE_ID}/query", payload)
        rows.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return rows


# ── 取值小工具：Notion 每種欄位型別的結構都不一樣 ──
def p_text(props, name):
    p = props.get(name) or {}
    parts = p.get("title") or p.get("rich_text") or []
    return "".join(x.get("plain_text", "") for x in parts).strip()


def p_select(props, name):
    p = props.get(name) or {}
    sel = p.get("select")
    return sel.get("name") if sel else ""


def p_multi(props, name):
    p = props.get(name) or {}
    return [x.get("name", "") for x in (p.get("multi_select") or [])]


def p_number(props, name):
    p = props.get(name) or {}
    return p.get("number")


def p_date(props, name):
    p = props.get(name) or {}
    d = p.get("date") or {}
    return d.get("start"), d.get("end")


def to_row(page):
    """轉成看板用的 12 欄格式。"""
    pr = page.get("properties", {})
    start, end = p_date(pr, "時程")
    # 日期可能帶時間，只留日期部分
    start = start[:10] if start else None
    end = end[:10] if end else None
    return [
        p_text(pr, "檔次編號"),
        p_text(pr, "名稱"),
        p_select(pr, "狀態"),
        p_select(pr, "活動類型"),
        ",".join(p_multi(pr, "主題")),
        p_select(pr, "進場負責窗口"),
        start,
        end,
        p_number(pr, "營業目標(萬)"),   # ← 會在下一步被清掉
        p_number(pr, "坪數"),
        p_text(pr, "場地大小"),
        p_text(pr, "備註"),
    ]


def strip_goal(rows):
    """把營業目標整個清空。公開版絕對不能有金額——不要移除這個函式。"""
    removed = 0
    for r in rows:
        if r[8] is not None:
            removed += 1
        r[8] = None
    return removed


def main():
    pages = fetch_rows()
    print(f"從 Notion 讀到 {len(pages)} 筆")
    if not pages:
        sys.exit("❌ 一筆都沒抓到。通常是資料庫還沒連結到「檔期看板同步」這個整合。")

    # ── 診斷：印出欄位名稱與型別，方便對照 ──
    # 注意：這是公開 repo，執行紀錄任何人都看得到，所以只印欄位「結構」不印金額。
    pr0 = pages[0].get("properties", {})
    print("\n--- 欄位清單（名稱 → 型別）---")
    for k, v in sorted(pr0.items()):
        print(f"  {k!r} → {v.get('type')}")
    print("\n--- 前 3 筆的「時程」原始結構 ---")
    for pg in pages[:3]:
        p = pg.get("properties", {}).get("時程")
        print("  ", json.dumps(p, ensure_ascii=False))
    print("---\n")

    rows = [to_row(p) for p in pages]

    removed = strip_goal(rows)
    print(f"已清除 {removed} 筆營業目標")

    # 保險：確認真的沒有金額殘留
    leaked = [r[1] for r in rows if r[8] is not None]
    if leaked:
        sys.exit(f"❌ 營業目標沒有清乾淨，中止發布：{leaked[:5]}")

    # 依營業起始日排序，沒日期的排最後（跟網頁的排序一致）
    rows.sort(key=lambda r: (r[6] is None, r[6] or ""))

    tpl = io.open(TEMPLATE, encoding="utf-8").read()
    raw_js = "[\n" + ",\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n]"
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")

    html = tpl.replace("__RAW__", raw_js).replace("__SNAPSHOT__", today)
    if "__RAW__" in html or "__SNAPSHOT__" in html:
        sys.exit("❌ 版型檔的佔位符沒有被正確取代")

    io.open(OUTPUT, "w", encoding="utf-8").write(html)
    print(f"✅ 已產生 {OUTPUT}（{len(html)} bytes，資料日期 {today}）")


if __name__ == "__main__":
    main()
