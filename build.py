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


# 日期欄位的名稱。Notion 裡目前叫「活動日程」，之前叫過「時程」。
# 都找不到的話會自動抓第一個 date 型別的欄位，避免改名就整個壞掉。
DATE_CANDIDATES = ("活動日程", "時程")
_date_key = None


def find_date_key(props):
    global _date_key
    if _date_key and _date_key in props:
        return _date_key
    for n in DATE_CANDIDATES:
        if isinstance(props.get(n), dict) and props[n].get("type") == "date":
            _date_key = n
            return n
    for k, v in props.items():
        if isinstance(v, dict) and v.get("type") == "date":
            _date_key = k
            print(f"⚠️ 找不到預期的日期欄位，改用「{k}」")
            return k
    return None


def p_date(props):
    key = find_date_key(props)
    if not key:
        return None, None
    d = (props.get(key) or {}).get("date") or {}
    return d.get("start"), d.get("end")


def to_row(page):
    """轉成看板用的 12 欄格式。"""
    pr = page.get("properties", {})
    start, end = p_date(pr)
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


# 不對外公開的狀態。洽談中／口袋名單會洩漏正在接觸與想談的點位，
# 對競爭對手和百貨窗口都是有價值的情報，所以不放上公開網頁。
# 空白狀態代表案子還沒確認，同樣先不公開（Sarah 在 Notion 填好狀態就會出現）。
PRIVATE_STATUS = {"洽談中", "口袋名單", ""}


def drop_private(rows):
    keep = [r for r in rows if (r[2] or "") not in PRIVATE_STATUS]
    dropped = len(rows) - len(keep)
    return keep, dropped


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

    print(f"日期欄位：「{find_date_key(pages[0].get('properties', {}))}」")

    rows = [to_row(p) for p in pages]

    rows, dropped = drop_private(rows)
    print(f"已排除不公開的狀態（洽談中／口袋名單／未填狀態）：{dropped} 筆 → 剩 {len(rows)} 筆")

    removed = strip_goal(rows)
    print(f"已清除 {removed} 筆營業目標")

    # 健康檢查：日期抓不到就中止，不要默默產出一份沒有甘特圖的網頁
    dated = sum(1 for r in rows if r[6])
    print(f"有營業日的：{dated} / {len(rows)} 筆")
    if dated == 0:
        sys.exit("❌ 一筆日期都沒抓到，中止發布。可能是 Notion 的日期欄位改名了。")

    # 保險：確認真的沒有金額殘留
    leaked = [r[1] for r in rows if r[8] is not None]
    if leaked:
        sys.exit(f"❌ 營業目標沒有清乾淨，中止發布：{leaked[:5]}")

    # 保險：確認沒有不該公開的狀態混進來
    bad = [r[2] for r in rows if (r[2] or "") in PRIVATE_STATUS]
    if bad:
        sys.exit(f"❌ 有不該公開的狀態沒濾掉，中止發布：{set(bad)}")

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
