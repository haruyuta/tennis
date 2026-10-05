#!/usr/bin/env python3
"""chotto-ii.com の掲示板からイベント一覧を取得し events.json を更新する。 (v2)

GitHub Actions から定期実行される想定。手動実行も可:
    python scripts/update_events.py              # 通常更新
    python scripts/update_events.py --debug      # 検出リンク一覧を表示し、取得HTMLを debug/ に保存
    python scripts/update_events.py --dry-run    # 書き込まずに差分表示のみ
    python scripts/update_events.py --html F     # ローカルHTMLファイルを解析(テスト用・通信しない)

v2 変更点:
  - 掲示板の日付が一新(全件入れ替え・HTML構造変化)されても追従できるよう解析を強化
    * href のクォート形式( " / ' / なし)と &amp; に対応
    * 日付がリンク文字列の外(同じ行の前後)にある場合も拾う
    * <option value="...eid=..."> 形式にも対応
    * トップがイベント詳細ページ等に変わった場合、「一覧」リンクを辿って取得
      (現状トップ ?gid=... は ?eid=343500&gid=... の空ページへリダイレクトされ、
       旧版はそこで0件→exit 0 で黙って終了していた)
    * meta refresh / JavaScript の location リダイレクトと Cookie にも追従
    * 日付が一覧から取れない新規イベントは詳細ページを取得して日付を推定
  - 文字コードを HTTPヘッダ / XML宣言 / meta から判定(Shift_JIS 対応)
  - 1件も検出できない場合は exit 1 で Actions を失敗させ、debug/ にHTMLを保存
    (従来は exit 0 で黙って成功扱いになり、更新停止に気付けなかった)
"""
import datetime
import html as htmllib
import http.cookiejar
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = "2.0"

GID = "14un6e"
BOARD_URL = f"http://chotto-ii.com/apps/mobile/?gid={GID}"
ROOT = Path(__file__).resolve().parent.parent
JSON_PATH = ROOT / "events.json"
DEBUG_DIR = ROOT / "debug"
JST = datetime.timezone(datetime.timedelta(hours=9))
UA = "Mozilla/5.0 (nighter-launcher-updater/" + VERSION + ")"

MAX_LIST_PAGES = 6      # 一覧ページを辿る最大数
MAX_DETAIL_FETCH = 40   # 詳細ページを取得する最大数

A_RE = re.compile(r"<a\b([^>]*)>(.*?)</a\s*>", re.I | re.S)
HREF_RE = re.compile(r"""href\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
OPTION_RE = re.compile(
    r"""<option\b([^>]*)>(.*?)(?=</option|<option\b|</select)""", re.I | re.S)
VALUE_RE = re.compile(r"""value\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
EID_RE = re.compile(r"[?&;]eid=(\d+)")
TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1\s*>", re.I | re.S)
BLOCK_RE = re.compile(r"<(?:br|/?li|/?tr|/?td|/?p|/?div|/?dt|/?dd|hr)\b[^>]*>", re.I)
HEAD_RE = re.compile(r"<(title|h1|h2|h3)\b[^>]*>(.*?)</\1\s*>", re.I | re.S)
CHARSET_RE = re.compile(rb"""(?:encoding|charset)\s*=\s*["']?([A-Za-z0-9_\-]+)""", re.I)

# 日付パターン(年あり / 年なし)
DATE_FULL_RE = re.compile(r"(\d{4})\s*[/\-.年]\s*(\d{1,2})\s*[/\-.月]\s*(\d{1,2})")
DATE_MD_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[/月]\s*(\d{1,2})(?!\d)")


# ---------------- 取得 ----------------
def decode(raw: bytes, header_charset) -> str:
    cands = []
    if header_charset:
        cands.append(header_charset)
    m = CHARSET_RE.search(raw[:4096])
    if m:
        cands.append(m.group(1).decode("ascii", "ignore"))
    cands += ["utf-8", "cp932", "euc_jp"]
    for enc in cands:
        enc = enc.lower()
        if enc in ("shift_jis", "shift-jis", "sjis", "x-sjis", "windows-31j"):
            enc = "cp932"
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
META_REFRESH_RE = re.compile(
    r"""<meta\b[^>]*http-equiv\s*=\s*["']?refresh["']?[^>]*content\s*=\s*["']?\s*\d*\s*;?\s*url\s*=\s*([^"'>\s]+)""",
    re.I)
JS_LOC_RE = re.compile(
    r"""location(?:\.href)?\s*(?:=|\.replace\(|\.assign\()\s*["']([^"']+)["']""", re.I)


def fetch_html(url: str, _depth: int = 0):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with OPENER.open(req, timeout=30) as resp:
        raw = resp.read()
        final = resp.geturl()
        cs = resp.headers.get_content_charset()
    html = decode(raw, cs)
    # HTTP以外のリダイレクト(meta refresh / JS)に追従。本文がほぼ空の場合のみ
    if _depth < 3 and len(TAG_RE.sub("", html).strip()) < 300:
        m = META_REFRESH_RE.search(html) or JS_LOC_RE.search(html)
        if m:
            nxt = urllib.parse.urljoin(final, htmllib.unescape(m.group(1)))
            if nxt != final:
                return fetch_html(nxt, _depth + 1)
    return html, final


# ---------------- 解析 ----------------
def text_of(fragment: str) -> str:
    t = TAG_RE.sub(" ", fragment)
    t = htmllib.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


def infer_date(text: str, today: datetime.date):
    """文字列から日付を推定。年が無い場合は今日の前後で妥当な年を選ぶ。"""
    if not text:
        return None
    m = DATE_FULL_RE.search(text)
    if m:
        y, mo, d = map(int, m.groups())
        try:
            return datetime.date(y, mo, d)
        except ValueError:
            pass
    for m in DATE_MD_RE.finditer(text):
        mo, d = map(int, m.groups())
        best = None
        for y in (today.year - 1, today.year, today.year + 1):
            try:
                cand = datetime.date(y, mo, d)
            except ValueError:
                continue
            # 過去120日〜未来330日の範囲に収まる年を採用
            if -120 <= (cand - today).days <= 330:
                best = cand
                break
        if best:
            return best
    return None


def get_attr(attrs: str, rx):
    m = rx.search(attrs)
    if not m:
        return None
    return htmllib.unescape(next(g for g in m.groups() if g is not None))


def same_line(fragment: str, last: bool) -> str:
    """アンカー前後のHTML断片から、同じ行(ブロック境界の内側)の部分だけ取り出す。"""
    parts = BLOCK_RE.split(fragment)
    return text_of(parts[-1] if last else parts[0])


def parse_page(html: str, page_url: str, today: datetime.date, debug=False):
    """戻り値: (found: {eid: {"date": date|None, "title": str}}, follow_urls: [url])"""
    html = SCRIPT_RE.sub(" ", html)
    found, follow = {}, []
    anchors = list(A_RE.finditer(html))
    m_self = EID_RE.search(page_url)
    self_eid = m_self.group(1) if m_self else None

    def put(eid, d, title):
        cur = found.get(eid)
        if cur is None or (cur["date"] is None and d is not None):
            found[eid] = {"date": d, "title": title}

    for i, a in enumerate(anchors):
        href = get_attr(a.group(1), HREF_RE)
        if not href:
            continue
        url = urllib.parse.urljoin(page_url, href)
        title = text_of(a.group(2))
        host = urllib.parse.urlparse(url).netloc
        # 「イベント一覧」等のナビリンクは eid を含んでいても一覧ページとして辿る
        if "一覧" in title:
            if host.endswith("chotto-ii.com") and f"gid={GID}" in url:
                follow.append(url)
            continue
        m = EID_RE.search(url)
        if m:
            eid = m.group(1)
            d = infer_date(title, today)
            if d is None:  # 日付がリンク外(同じ行の前/後)にある場合
                prev_end = anchors[i - 1].end() if i > 0 else max(0, a.start() - 400)
                next_start = anchors[i + 1].start() if i + 1 < len(anchors) else a.end() + 400
                d = infer_date(same_line(html[prev_end:a.start()], last=True), today) \
                    or infer_date(same_line(html[a.end():next_start], last=False), today)
            if eid == self_eid and d is None:
                continue  # 詳細ページ内の日付なし自己リンク(出席状況など)は除外
            put(eid, d, title)
            if debug:
                print(f"  link: eid={eid} title={title!r} -> date={d}")
        elif debug and host.endswith("chotto-ii.com"):
            print(f"  other link: {url} {title!r}")

    for o in OPTION_RE.finditer(html):
        val = get_attr(o.group(1), VALUE_RE) or ""
        m = EID_RE.search(val) or (re.fullmatch(r"\d{5,}", val) and re.match(r"(\d+)", val))
        if not m:
            continue
        title = text_of(o.group(2))
        d = infer_date(title, today)
        put(m.group(1), d, title)
        if debug:
            print(f"  option: eid={m.group(1)} title={title!r} -> date={d}")
    return found, follow


def date_from_detail(html: str, today: datetime.date):
    html = SCRIPT_RE.sub(" ", html)
    for m in HEAD_RE.finditer(html):
        d = infer_date(text_of(m.group(2)), today)
        if d:
            return d
    # 見出しに無ければ本文先頭のみ(コメント欄の日付を誤検出しないため)
    return infer_date(text_of(html)[:300], today)


# ---------------- 入出力 ----------------
def load_json():
    if JSON_PATH.exists():
        with open(JSON_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"updated": "", "gid": GID, "events": []}


def save_debug(name: str, html: str):
    DEBUG_DIR.mkdir(exist_ok=True)
    (DEBUG_DIR / name).write_text(html, encoding="utf-8")


def scrape(today, debug, local_html=None):
    """掲示板を巡回して {eid: {"date","title"}} を返す。"""
    found = {}
    if local_html is not None:
        f, _ = parse_page(local_html, BOARD_URL, today, debug)
        return f, [local_html]

    queue, seen, pages = [BOARD_URL], set(), []
    while queue and len(seen) < MAX_LIST_PAGES:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        print(f"fetch: {url}")
        html, final = fetch_html(url)
        seen.add(final)
        pages.append(html)
        if debug:
            save_debug(f"page{len(pages)}.html", html)
            if final != url:
                print(f"  -> redirected: {final}")
        f, follow = parse_page(html, final, today, debug)
        for eid, v in f.items():
            if eid not in found or (found[eid]["date"] is None and v["date"]):
                found[eid] = v
        queue += [u for u in follow if u not in seen]
    return found, pages


def main():
    args = sys.argv[1:]
    debug = "--debug" in args
    dry_run = "--dry-run" in args
    local_html = None
    if "--html" in args:
        p = Path(args[args.index("--html") + 1])
        local_html = decode(p.read_bytes(), None)

    now = datetime.datetime.now(JST)
    today = now.date()
    print(f"update_events v{VERSION}")

    data = load_json()
    known = {e["eid"]: e["date"] for e in data["events"]}

    found, pages = scrape(today, debug, local_html)

    if not found:
        for i, h in enumerate(pages, 1):
            save_debug(f"failed_page{i}.html", h)
        print("エラー: イベントリンク(eid=...)を1件も検出できませんでした。"
              "掲示板のHTML構造が変わった可能性があります。debug/ に取得HTMLを保存しました。")
        sys.exit(1)

    # 一覧で日付が取れなかった新規イベントは詳細ページから日付を推定
    undated = [eid for eid, v in found.items() if v["date"] is None and eid not in known]
    if local_html is None:
        for eid in undated[:MAX_DETAIL_FETCH]:
            url = f"{BOARD_URL}&eid={eid}"
            try:
                html, _ = fetch_html(url)
            except Exception as ex:  # 個別の取得失敗は続行
                print(f"  詳細取得失敗 eid={eid}: {ex}")
                continue
            d = date_from_detail(html, today)
            found[eid]["date"] = d
            print(f"  詳細ページ eid={eid} -> date={d}")

    scraped = {eid: v["date"].isoformat() for eid, v in found.items() if v["date"]}
    still = [eid for eid, v in found.items() if v["date"] is None and eid not in known]
    print(f"掲示板から {len(found)} 件のリンクを検出(日付確定 {len(scraped)} 件)")
    if still:
        print(f"警告: 日付を特定できない新規イベント: {', '.join(still)}")

    if not scraped and not any(eid in known for eid in found):
        for i, h in enumerate(pages, 1):
            save_debug(f"failed_page{i}.html", h)
        print("エラー: 日付を特定できたイベントがありません。debug/ を確認してください。")
        sys.exit(1)

    merged = dict(known)
    added, changed = [], []
    for eid, d in scraped.items():
        old = merged.get(eid)
        if old is None:
            added.append((eid, d))
        elif old != d:
            changed.append((eid, old, d))
        merged[eid] = d   # 掲示板側を正とする(掲示板から消えた過去分は保持)

    gone = [eid for eid in known if eid not in found]
    if gone and debug:
        print(f"  (掲示板から消えたイベント {len(gone)} 件は保持)")

    for eid, d in added:
        print(f"  追加: {d} eid={eid}")
    for eid, old, d in changed:
        print(f"  変更: eid={eid} {old} -> {d}")
    if not added and not changed:
        print("変更なし。events.json は更新しません。")
        return

    events = sorted(({"date": d, "eid": e} for e, d in merged.items()),
                    key=lambda x: (x["date"], x["eid"]))
    new_data = {"updated": now.isoformat(timespec="seconds"), "gid": GID, "events": events}
    if dry_run:
        print("--dry-run のため書き込みません。")
        return
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(new_data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"events.json を更新しました({len(events)} 件)")


if __name__ == "__main__":
    main()
