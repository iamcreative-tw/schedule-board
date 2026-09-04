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
    """轉成看板用的欄位陣列：
    [檔次, 櫃點, 狀態, 類型, 主題, 窗口, 起, 迄, 目標(萬), 坪數, 場地大小, 備註]

    「類型」現在是 6 個細項（快閃店／寄售／賣斷／7-11 快閃購／7-11門市預購／7-11門市上架），
    網頁再把它們併成 3 個頁籤，分組規則寫在 template.html 的 TYPE_GROUP。
    """
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


SNAPSHOT = "snapshot.json"      # 上次的資料，只用來比對差異，不會發布出去
CHANGELOG = "最近更新.md"        # 這次改了什麼，給 Sarah 轉寄給同仁

# 要比對的欄位（索引 → 給人看的名稱）。
# 跳過 0（檔次編號，已停用）和 8（營業目標，公開版本來就沒有）。
FIELDS = {1:"活動名稱", 2:"狀態", 3:"類型", 4:"主題", 5:"進場窗口",
          6:"開始日", 7:"結束日", 9:"坪數", 10:"場地", 11:"備註"}


def show(v):
    return "（空白）" if v in (None, "") else str(v)


def period(r):
    if not r[6]:
        return "日期未定"
    return f"{r[6]} ~ {r[7]}" if r[7] else f"{r[6]} 起"


def diff(old, new):
    """old / new 都是 {Notion頁面id: 欄位陣列}。用頁面 id 當識別，改名也認得出是同一筆。"""
    added   = [new[k] for k in new if k not in old]
    gone    = [old[k] for k in old if k not in new]
    edited  = []
    for k, n in new.items():
        o = old.get(k)
        if not o:
            continue
        diffs = [(lab, o[i] if i < len(o) else None, n[i])
                 for i, lab in FIELDS.items()
                 if (o[i] if i < len(o) else None) != n[i]
                 and not ((o[i] if i < len(o) else None) in (None, "") and n[i] in (None, ""))]
        if diffs:
            edited.append((n, diffs))
    return added, gone, edited


def changelog(added, gone, edited, today, first_run):
    L = [f"# 檔期活動看板　更新通知　{today}", ""]
    if first_run:
        L += ["這是第一次建立比對基準，所以沒有變更紀錄。",
              "下次更新開始，這裡就會列出異動內容。", ""]
    elif not (added or gone or edited):
        L += ["**這次沒有任何異動**，看板內容跟上次相同。", ""]
    else:
        if added:
            L += [f"## 新增檔期（{len(added)} 檔）", ""]
            for r in added:
                L.append(f"- **{r[1]}**")
                L.append(f"  {period(r)}｜{show(r[3])}｜{show(r[2])}"
                         + (f"｜主題 {r[4]}" if r[4] else ""))
            L.append("")
        if edited:
            L += [f"## 內容變更（{len(edited)} 檔）", ""]
            for r, ds in edited:
                L.append(f"- **{r[1]}**")
                for lab, o, n in ds:
                    L.append(f"  - {lab}：{show(o)} → **{show(n)}**")
            L.append("")
        if gone:
            L += [f"## 不再顯示於看板（{len(gone)} 檔）", ""]
            for r in gone:
                L.append(f"- {r[1]}")
            L += ["", "> 可能是已從 Notion 刪除，或狀態改成洽談中／口袋名單。", ""]
    L += ["---", "", "看板網址　https://iamcreative-tw.github.io/schedule-board/"]
    return "\n".join(L)


def main():
    pages = fetch_rows()
    print(f"從 Notion 讀到 {len(pages)} 筆")
    if not pages:
        sys.exit("❌ 一筆都沒抓到。通常是資料庫還沒連結到「檔期看板同步」這個整合。")

    print(f"日期欄位：「{find_date_key(pages[0].get('properties', {}))}」")

    # 保留 Notion 頁面 id，比對差異時當識別用（改名也認得出是同一筆）
    items = [(p.get("id"), to_row(p)) for p in pages]
    items = [(i, r) for i, r in items if (r[2] or "") not in PRIVATE_STATUS]
    print(f"已排除不公開的狀態（洽談中／口袋名單／未填狀態）：{len(pages)-len(items)} 筆 → 剩 {len(items)} 筆")

    rows = [r for _, r in items]
    removed = strip_goal(rows)          # 先清金額，快照才不會存到不該存的東西
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

    # ── 跟上次比對，產生更新通知 ──
    new_snap = {i: r for i, r in items}
    try:
        old_snap = json.load(io.open(SNAPSHOT, encoding="utf-8"))
        first_run = False
    except (FileNotFoundError, ValueError):
        old_snap, first_run = {}, True

    added, gone, edited = diff(old_snap, new_snap)
    io.open(CHANGELOG, "w", encoding="utf-8").write(
        changelog(added, gone, edited, today, first_run) + "\n")
    json.dump(new_snap, io.open(SNAPSHOT, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    # 給工作流程判斷要不要寄信用：沒異動就不寄，免得信箱被無意義的通知洗版
    has_change = bool(added or gone or edited) and not first_run
    io.open("changed.flag", "w").write("yes" if has_change else "no")

    if first_run:
        print("📋 第一次執行，已建立比對基準（下次更新才會有變更紀錄）")
    else:
        print(f"📋 這次異動：新增 {len(added)}、變更 {len(edited)}、不再顯示 {len(gone)}")
        for r in added:  print(f"    ＋ {r[1]}")
        for r, ds in edited:
            print(f"    ✎ {r[1]}：" + "、".join(f"{l} {show(o)}→{show(n)}" for l, o, n in ds))
        for r in gone:   print(f"    － {r[1]}")


if __name__ == "__main__":
    main()
