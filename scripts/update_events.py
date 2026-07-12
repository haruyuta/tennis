#!/usr/bin/env python3
"""chotto-ii.com の掲示板からイベント一覧を取得し events.json を更新する。

GitHub Actions から定期実行される想定。手動実行も可:
    python scripts/update_events.py            # 通常更新
    python scripts/update_events.py --debug    # 取得したリンク一覧を表示(解析調整用)
    python scripts/update_events.py --dry-run  # 書き込まずに差分表示のみ
"""
import datetime
import json
import re
import sys
import urllib.request
from pathlib import Path

GID = "14un6e"
BOARD_URL = f"http://chotto-ii.com/apps/mobile/?gid={GID}"
JSON_PATH = Path(__file__).resolve().parent.parent / "events.json"
JST = datetime.timezone(datetime.timedelta(hours=9))

# <a href="...eid=数字...">タイトル</a> を抽出
LINK_RE = re.compile(
    r'<a\b[^>]*href="[^"]*[?&;]eid=(\d+)[^"]*"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
TAG_RE = re.compile(r"<[^>]+>")

# タイトル中の日付パターン(年あり / 年なし)
DATE_FULL_RE = re.compile(r"(\d{4})\s*[/\-年]\s*(\d{1,2})\s*[/\-月]\s*(\d{1,2})")
DATE_MD_RE = re.compile(r"(\d{1,2})\s*[/月]\s*(\d{1,2})")


def fetch_html(url: str) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (nighter-launcher-updater)"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    # 文字コードは不明なため順に試す(Shift_JISの可能性が高い個人系サイト想定)
    for enc in ("utf-8", "cp932", "euc_jp"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def infer_date(text: str, today: datetime.date):
    """タイトル文字列から日付を推定。年が無い場合は今日の前後で妥当な年を選ぶ。"""
    m = DATE_FULL_RE.search(text)
    if m:
        y, mo, d = map(int, m.groups())
        try:
            return datetime.date(y, mo, d)
        except ValueError:
            return None
    m = DATE_MD_RE.search(text)
    if m:
        mo, d = map(int, m.groups())
        for y in (today.year - 1, today.year, today.year + 1):
            try:
                cand = datetime.date(y, mo, d)
            except ValueError:
                continue
            # 過去80日〜未来320日の範囲に収まる年を採用
            if -80 <= (cand - today).days <= 320:
                return cand
    return None


def parse_events(html: str, today: datetime.date, debug: bool = False):
    found = {}
    for eid, inner in LINK_RE.findall(html):
        title = TAG_RE.sub("", inner)
        title = re.sub(r"\s+", " ", title).strip()
        d = infer_date(title, today)
        if debug:
            print(f"  link: eid={eid} title={title!r} -> date={d}")
        if d:
            found[eid] = {"date": d.isoformat(), "eid": eid}
    return list(found.values())


def load_json():
    if JSON_PATH.exists():
        with open(JSON_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"updated": "", "gid": GID, "events": []}


def main():
    debug = "--debug" in sys.argv
    dry_run = "--dry-run" in sys.argv
    now = datetime.datetime.now(JST)
    today = now.date()

    print(f"fetch: {BOARD_URL}")
    html = fetch_html(BOARD_URL)
    scraped = parse_events(html, today, debug=debug)
    print(f"掲示板から {len(scraped)} 件のイベントを検出")

    if not scraped:
        # 解析失敗時は既存データを壊さないよう何もしない
        print("警告: イベントが1件も検出できませんでした。HTML構造が変わった可能性があります。")
        print("      --debug で実行してリンク一覧を確認し、LINK_RE / 日付正規表現を調整してください。")
        sys.exit(0)

    data = load_json()
    merged = {e["eid"]: {"date": e["date"], "eid": e["eid"]} for e in data["events"]}
    added, changed = [], []
    for e in scraped:
        old = merged.get(e["eid"])
        if old is None:
            added.append(e)
        elif old["date"] != e["date"]:
            changed.append((old["date"], e))
        merged[e["eid"]] = e  # 掲示板側を正とする(過去分は掲示板から消えても保持)

    events = sorted(merged.values(), key=lambda x: x["date"])

    for e in added:
        print(f"  追加: {e['date']} eid={e['eid']}")
    for old_date, e in changed:
        print(f"  変更: eid={e['eid']} {old_date} -> {e['date']}")
    if not added and not changed:
        print("変更なし。events.json は更新しません。")
        sys.exit(0)

    new_data = {"updated": now.isoformat(timespec="seconds"), "gid": GID, "events": events}
    if dry_run:
        print("--dry-run のため書き込みません。")
        sys.exit(0)

    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(new_data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"events.json を更新しました({len(events)} 件)")


if __name__ == "__main__":
    main()
