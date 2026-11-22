"""
台股每日資金面日報
------------------
每天收盤後自動整理：
  1. 三大法人買賣超金額（證交所 BFI82U）
  2. 外資 / 投信 個股買賣超前 10 名（證交所 T86）
  3. 台指期 三大法人未平倉（期交所）
  4. 匯率、加權指數、美股（Yahoo Finance）

輸出：
  reports/YYYY-MM-DD.md   當日日報
  history.csv             每日數據累積（可以拿來畫圖）

用法：
  py -3.12 tw_market_daily.py                 # 抓最近一個交易日
  py -3.12 tw_market_daily.py --date 20261120 # 抓指定日期
  py -3.12 tw_market_daily.py --open          # 抓完用瀏覽器打開報告
  py -3.12 tw_market_daily.py --make-shortcut # 在桌面建立「台股日報.bat」
  python tw_market_daily.py --today-only      # 雲端排程用：今天休市就安靜結束

環境變數（雲端用，可不設）：
  REPORT_DIR          報告資料夾，預設 reports
  LINE_CHANNEL_ACCESS_TOKEN  LINE 官方帳號的 Channel access token
  LINE_USER_ID        你自己的 LINE user ID（U 開頭）
  PAGES_URL           GitHub Pages 網址，推播時附上完整報告連結

安裝：
  pip install requests pandas yfinance
"""

import argparse
import csv
import io
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

TW_TZ = timezone(timedelta(hours=8))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(BASE_DIR, os.environ.get("REPORT_DIR", "reports"))
HISTORY_CSV = os.path.join(BASE_DIR, "history.csv")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120 Safari/537.36"
}

# 要追蹤的國際行情（Yahoo Finance 代號: 顯示名稱）
TICKERS = {
    "TWD=X": "美元/台幣",
    "^TWII": "台股加權指數",
    "2330.TW": "台積電",
    "DX-Y.NYB": "美元指數",
    "^SOX": "費城半導體",
    "^IXIC": "那斯達克",
    "NVDA": "輝達",
    "TSM": "台積電ADR",
}


# ---------------------------------------------------------------- 工具函式
def to_num(s):
    """'1,234,567' -> 1234567.0；無法轉換回傳 None"""
    try:
        return float(str(s).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def get_json(url, params):
    r = requests.get(url, params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def fmt_yi(x):
    """元 -> 億元，帶正負號"""
    if x is None:
        return "N/A"
    return f"{x / 1e8:+,.2f} 億"


# ---------------------------------------------------------------- 1. 三大法人買賣金額
def fetch_institutional_amount(date_str):
    """
    證交所「三大法人買賣金額統計表」
    回傳 dict: {'外資': 淨額(元), '投信': ..., '自營商': ..., '合計': ...}
    """
    data = get_json(
        "https://www.twse.com.tw/rwd/zh/fund/BFI82U",
        {"type": "day", "dayDate": date_str, "response": "json"},
    )
    if data.get("stat") != "OK":
        return None

    result = {"外資": 0.0, "投信": 0.0, "自營商": 0.0, "合計": None}
    for row in data["data"]:
        name, net = row[0], to_num(row[3])
        if net is None:
            continue
        if "外資" in name:          # 外資及陸資 + 外資自營商
            result["外資"] += net
        elif "投信" in name:
            result["投信"] += net
        elif "自營商" in name:      # 自行買賣 + 避險
            result["自營商"] += net
        elif "合計" in name:
            result["合計"] = net
    return result


# ---------------------------------------------------------------- 2. 個股買賣超
def fetch_stock_ranking(date_str, top_n=10):
    """
    證交所「三大法人買賣超日報」(T86)，排除權證
    回傳 {'外資買超': [...], '外資賣超': [...], '投信買超': [...], '投信賣超': [...]}
    每筆為 (代號, 名稱, 買賣超張數)
    """
    data = get_json(
        "https://www.twse.com.tw/rwd/zh/fund/T86",
        {"date": date_str, "selectType": "ALLBUT0999", "response": "json"},
    )
    if data.get("stat") != "OK":
        return None

    fields = data["fields"]

    def col(keyword, exclude=None):
        for i, f in enumerate(fields):
            if keyword in f and (exclude is None or exclude not in f):
                return i
        raise KeyError(keyword)

    i_code = col("證券代號")
    i_name = col("證券名稱")
    i_foreign = col("外陸資買賣超股數")       # 不含外資自營商
    i_trust = col("投信買賣超股數")

    rows = []
    for r in data["data"]:
        rows.append((
            r[i_code].strip(),
            r[i_name].strip(),
            (to_num(r[i_foreign]) or 0) / 1000,   # 股 -> 張
            (to_num(r[i_trust]) or 0) / 1000,
        ))

    return {
        "外資買超": [(x[0], x[1], x[2]) for x in sorted(rows, key=lambda x: x[2], reverse=True)[:top_n]],
        "外資賣超": [(x[0], x[1], x[2]) for x in sorted(rows, key=lambda x: x[2])[:top_n]],
        "投信買超": [(x[0], x[1], x[3]) for x in sorted(rows, key=lambda x: x[3], reverse=True)[:top_n]],
        "投信賣超": [(x[0], x[1], x[3]) for x in sorted(rows, key=lambda x: x[3])[:top_n]],
    }


# ---------------------------------------------------------------- 3. 台指期未平倉
def fetch_futures_oi(date_str):
    """
    期交所「三大法人 - 區分各期貨契約」台指期 (TXF)
    回傳 {'外資': {'交易淨口數':..., '未平倉淨口數':...}, '投信': ..., '自營商': ...}
    """
    d = f"{date_str[:4]}/{date_str[4:6]}/{date_str[6:]}"
    r = requests.post(
        "https://www.taifex.com.tw/cht/3/futContractsDateDown",
        data={"queryStartDate": d, "queryEndDate": d, "commodityId": "TXF"},
        headers=HEADERS, timeout=30,
    )
    r.raise_for_status()
    text = r.content.decode("cp950", errors="ignore")
    reader = list(csv.reader(io.StringIO(text)))
    if len(reader) < 2:
        return None

    header = [h.strip() for h in reader[0]]

    def col(keyword):
        for i, h in enumerate(header):
            if keyword in h and "金額" not in h:
                return i
        raise KeyError(keyword)

    i_who = col("身份別")
    i_trade = col("多空交易口數淨額")
    i_oi = col("多空未平倉口數淨額")

    result = {}
    for row in reader[1:]:
        if len(row) <= i_oi:
            continue
        who = row[i_who].strip()
        key = "外資" if "外資" in who else who
        result[key] = {
            "交易淨口數": to_num(row[i_trade]),
            "未平倉淨口數": to_num(row[i_oi]),
        }
    return result or None


# ---------------------------------------------------------------- 4. 國際行情
def fetch_quotes(date_str):
    """
    用 yfinance 抓指定日期（含）之前最後兩個交易日的收盤，算漲跌
    回傳 {代號: (收盤, 漲跌%, 資料日期)}
    """
    import yfinance as yf

    end = datetime.strptime(date_str, "%Y%m%d") + timedelta(days=1)
    start = end - timedelta(days=15)
    out = {}
    for t in TICKERS:
        try:
            h = yf.Ticker(t).history(start=start.strftime("%Y-%m-%d"),
                                     end=end.strftime("%Y-%m-%d"))
            closes = h["Close"].dropna()
            if len(closes) >= 2:
                last, prev = closes.iloc[-1], closes.iloc[-2]
                out[t] = (last, (last / prev - 1) * 100,
                          closes.index[-1].strftime("%m/%d"))
            elif len(closes) == 1:
                out[t] = (closes.iloc[-1], None, closes.index[-1].strftime("%m/%d"))
        except Exception as e:
            print(f"  [警告] {t} 抓取失敗：{e}", file=sys.stderr)
    return out


# ---------------------------------------------------------------- 歷史紀錄
HISTORY_FIELDS = ["日期", "外資(億)", "投信(億)", "自營商(億)", "三大法人合計(億)",
                  "外資台指期未平倉淨口數", "美元台幣", "加權指數"]


def save_history(row):
    rows = []
    if os.path.exists(HISTORY_CSV):
        with open(HISTORY_CSV, encoding="utf-8-sig") as f:
            rows = [r for r in csv.DictReader(f) if r["日期"] != row["日期"]]
    rows.append(row)
    rows.sort(key=lambda r: r["日期"])
    with open(HISTORY_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        w.writeheader()
        w.writerows(rows)
    return rows


# ---------------------------------------------------------------- 產生報告
def build_report(date_str, inst, ranking, fut, quotes, history):
    d = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    L = [f"# 台股資金面日報 {d}", ""]

    # 三大法人
    L += ["## 三大法人買賣超（上市）", ""]
    if inst:
        L += ["| 法人 | 買賣超 |", "|---|---:|"]
        for k in ["外資", "投信", "自營商", "合計"]:
            L.append(f"| {k} | {fmt_yi(inst.get(k))} |")
    else:
        L.append("無資料（休市或尚未公布）")
    L.append("")

    # 近 5 日外資
    if history and len(history) > 1:
        L += ["### 近 5 日外資買賣超", "", "| 日期 | 外資(億) | 美元/台幣 |", "|---|---:|---:|"]
        for r in history[-5:]:
            L.append(f"| {r['日期']} | {r['外資(億)']} | {r['美元台幣']} |")
        L.append("")

    # 台指期
    L += ["## 台指期 三大法人", ""]
    if fut:
        L += ["| 法人 | 當日交易淨口數 | 未平倉淨口數 |", "|---|---:|---:|"]
        for k, v in fut.items():
            t = v["交易淨口數"]; o = v["未平倉淨口數"]
            L.append(f"| {k} | {t:+,.0f} | {o:+,.0f} |" if t is not None and o is not None
                     else f"| {k} | N/A | N/A |")
        L += ["", "> 未平倉淨口數為正 = 偏多，為負 = 偏空"]
    else:
        L.append("無資料（休市或尚未公布）")
    L.append("")

    # 行情
    L += ["## 匯率與行情", "", "| 項目 | 收盤 | 漲跌 | 資料日 |", "|---|---:|---:|---|"]
    for t, name in TICKERS.items():
        if t in quotes:
            px, chg, day = quotes[t]
            chg_s = f"{chg:+.2f}%" if chg is not None else "-"
            L.append(f"| {name} | {px:,.3f} | {chg_s} | {day} |")
        else:
            L.append(f"| {name} | N/A | - | - |")
    L += ["", "> 美股資料日為美國時間，通常是台灣日期的前一天", ""]

    # 個股排行
    if ranking:
        for title in ["外資買超", "外資賣超", "投信買超", "投信賣超"]:
            L += [f"## {title}前 10 名（張）", "", "| 代號 | 名稱 | 張數 |", "|---|---|---:|"]
            for code, name, v in ranking[title]:
                L.append(f"| {code} | {name} | {v:+,.0f} |")
            L.append("")

    # 簡單判讀
    L += ["## 簡單判讀", ""]
    signals = []
    if inst:
        signals.append("外資**買超**" if inst["外資"] > 0 else "外資**賣超**")
    if "TWD=X" in quotes and quotes["TWD=X"][1] is not None:
        signals.append("台幣**貶值**" if quotes["TWD=X"][1] > 0 else "台幣**升值**")
    if fut and "外資" in fut and fut["外資"]["交易淨口數"] is not None:
        oi = fut["外資"]["未平倉淨口數"]
        stance = "整體**偏多**" if oi and oi > 0 else "整體**偏空**"
        move = "當日加碼多單/減空單" if fut["外資"]["交易淨口數"] > 0 else "當日加碼空單/減多單"
        signals.append(f"外資期貨{stance}（{move}）")
    L.append("、".join(signals) if signals else "資料不足")
    L += ["", "*僅為數據整理，不構成投資建議。*"]
    return "\n".join(L)


# ---------------------------------------------------------------- HTML 輸出
HTML_STYLE = """
body{font-family:"Microsoft JhengHei","PingFang TC",sans-serif;max-width:860px;
margin:24px auto;padding:0 16px;background:#111;color:#e6e6e6;line-height:1.6}
h1{border-bottom:2px solid #444;padding-bottom:8px}
h2{margin-top:32px;color:#9cc3ff}
table{border-collapse:collapse;width:100%;margin:8px 0}
th,td{border:1px solid #333;padding:6px 10px}
th{background:#1d1d1d}
td.r,th.r{text-align:right;font-variant-numeric:tabular-nums}
.pos{color:#ff5b5b}.neg{color:#2ecc71}
blockquote{color:#999;margin:4px 0;font-size:14px}
a{color:#9cc3ff}
#history{display:flex;flex-wrap:wrap;gap:8px}
#history a{padding:6px 12px;border:1px solid #333;border-radius:8px;text-decoration:none}
#history a.on{background:#9cc3ff;color:#111}
@media(max-width:600px){body{margin:12px auto;font-size:15px}th,td{padding:4px 6px}}
"""


def md_to_html(md, title, extra_html=""):
    """把本程式產生的簡單 Markdown 轉成 HTML（台股慣例：漲紅跌綠）"""
    import html as h
    import re

    def inline(s):
        s = h.escape(s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"\*(.+?)\*", r"<i>\1</i>", s)
        s = re.sub(r"\[(.+?)\]\((.+?)\)", r'<a href="\2">\1</a>', s)
        return s

    def cell(s, right):
        s = s.strip()
        cls = []
        if right:
            cls.append("r")
            if re.match(r"^\+\d", s):
                cls.append("pos")
            elif re.match(r"^-\d", s):
                cls.append("neg")
        c = f' class="{" ".join(cls)}"' if cls else ""
        return c, inline(s)

    out, lines, i = [], md.split("\n"), 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("|"):
            tbl = []
            while i < len(lines) and lines[i].startswith("|"):
                tbl.append([c for c in lines[i].strip().strip("|").split("|")])
                i += 1
            align = [a.strip().endswith(":") for a in tbl[1]] if len(tbl) > 1 else []
            out.append("<table>")
            for k, row in enumerate(tbl):
                if k == 1:
                    continue
                tag = "th" if k == 0 else "td"
                tds = []
                for j, c in enumerate(row):
                    cls, txt = cell(c, j < len(align) and align[j])
                    tds.append(f"<{tag}{cls}>{txt}</{tag}>")
                out.append("<tr>" + "".join(tds) + "</tr>")
            out.append("</table>")
            continue
        if ln.startswith("### "):
            out.append(f"<h3>{inline(ln[4:])}</h3>")
        elif ln.startswith("## "):
            out.append(f"<h2>{inline(ln[3:])}</h2>")
        elif ln.startswith("# "):
            out.append(f"<h1>{inline(ln[2:])}</h1>")
        elif ln.startswith("> "):
            out.append(f"<blockquote>{inline(ln[2:])}</blockquote>")
        elif ln.strip():
            out.append(f"<p>{inline(ln)}</p>")
        i += 1

    return (f'<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="apple-mobile-web-app-capable" content="yes">'
            '<meta name="mobile-web-app-capable" content="yes">'
            '<meta name="apple-mobile-web-app-title" content="台股日報">'
            '<meta name="theme-color" content="#111111">'
            '<meta http-equiv="Cache-Control" content="no-cache">'
            f"<title>{h.escape(title)}</title><style>{HTML_STYLE}</style></head>"
            f'<body>{REFRESH_BAR}<div id="report">{"".join(out)}</div>'
            f"{extra_html}{REFRESH_JS}</body></html>")


HISTORY_JS = """<script>
async function loadReport(d){
  const box = document.getElementById('report');
  box.style.opacity = 0.4;
  try {
    const r = await fetch(d + '.html?t=' + Date.now());
    const doc = new DOMParser().parseFromString(await r.text(), 'text/html');
    const src = doc.getElementById('report') || doc.body;
    // 舊版報告沒有 #report，把重新整理按鈕和 script 拿掉
    src.querySelectorAll('script').forEach(e => e.remove());
    src.querySelectorAll('button').forEach(e => (e.closest('div') || e).remove());
    box.innerHTML = src.innerHTML;
    document.querySelectorAll('#history a').forEach(a =>
      a.classList.toggle('on', a.textContent === d));
    history.replaceState(null, '', '#' + d);
    window.scrollTo(0, 0);
  } catch (e) { alert('載入失敗：' + e); }
  box.style.opacity = 1;
}
// 標示目前顯示的日期；網址帶 #日期 就直接載入那天
(function(){
  const want = location.hash.slice(1);
  const first = document.querySelector('#history a');
  if (want && /^\\d{4}-\\d{2}-\\d{2}$/.test(want)) loadReport(want);
  else if (first) first.classList.add('on');
})();
</script>"""

# 主畫面 App 模式沒有重新整理按鈕：加一顆按鈕，並在切回 App 時自動抓最新版
REFRESH_BAR = ('<div style="text-align:right;margin-bottom:-8px">'
               '<button onclick="refresh()" style="background:#222;color:#9cc3ff;'
               'border:1px solid #444;border-radius:8px;padding:6px 14px;font-size:15px">'
               '🔄 重新整理</button></div>')
REFRESH_JS = """<script>
const loadedAt = Date.now();
function refresh(){ location.replace(location.pathname + '?t=' + Date.now()); }
document.addEventListener('visibilitychange', function(){
  if (document.visibilityState === 'visible' && Date.now() - loadedAt > 5*60*1000) refresh();
});
</script>"""


def write_index(latest_md, latest_date):
    """index.html = 最新一天的報告 + 歷史報告連結（給 GitHub Pages / 手機主畫面用）"""
    files = sorted((f for f in os.listdir(REPORT_DIR)
                    if f.endswith(".html") and f != "index.html"), reverse=True)
    # 首頁顯示「日期最新」的報告，而不是「最後一次執行」的
    newest_md = os.path.join(REPORT_DIR, files[0][:-5] + ".md") if files else None
    if newest_md and os.path.exists(newest_md):
        with open(newest_md, encoding="utf-8") as f:
            latest_md = f.read()
    # 歷史清單：點了在同一頁載入（不換頁，iPhone 主畫面 App 才不會跳出另一個瀏覽器）
    items = "".join(
        f'<a href="#{f[:-5]}" onclick="loadReport(\'{f[:-5]}\');return false;">{f[:-5]}</a>'
        for f in files[:60])
    history_html = f'<h2>歷史報告</h2><div id="history">{items}</div>' + HISTORY_JS
    with open(os.path.join(REPORT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(md_to_html(latest_md, "台股日報", history_html))
    # 告訴 GitHub Pages 不要用 Jekyll 處理（否則 .md 會被轉成 .html 跟我們的檔案衝突）
    open(os.path.join(REPORT_DIR, ".nojekyll"), "w").close()


# ---------------------------------------------------------------- LINE 推播
def line_send(text):
    """用 LINE Messaging API 推播給自己（需要 LINE_CHANNEL_ACCESS_TOKEN、LINE_USER_ID）"""
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    user = os.environ.get("LINE_USER_ID")
    if not token or not user:
        return
    try:
        r = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": f"Bearer {token}"},
            json={"to": user, "messages": [{"type": "text", "text": text[:4900]}]},
            timeout=30,
        )
        print("  ✓ LINE 已推播" if r.ok else f"  ✗ LINE 推播失敗：{r.status_code} {r.text}")
    except Exception as e:
        print(f"  ✗ LINE 推播失敗：{e}", file=sys.stderr)


def build_summary(d, inst, ranking, fut, quotes):
    """給手機推播的精簡版（純文字）"""
    L = [f"📊 台股資金面日報 {d}", ""]
    if inst:
        L.append("【三大法人】")
        for k in ["外資", "投信", "自營商"]:
            L.append(f"  {k} {fmt_yi(inst[k])}")
    if fut and "外資" in fut and fut["外資"]["未平倉淨口數"] is not None:
        f = fut["外資"]
        L.append(f"【外資台指期】未平倉 {f['未平倉淨口數']:+,.0f} 口"
                 f"（當日 {f['交易淨口數']:+,.0f}）")
    L.append("")
    for t in ["TWD=X", "^TWII", "2330.TW", "^SOX", "NVDA"]:
        if t in quotes:
            px, chg, _ = quotes[t]
            c = f"（{chg:+.2f}%）" if chg is not None else ""
            L.append(f"{TICKERS[t]} {px:,.2f}{c}")
    if ranking:
        L.append("")
        L.append("【外資買超】" + "、".join(n for _, n, _ in ranking["外資買超"][:5]))
        L.append("【外資賣超】" + "、".join(n for _, n, _ in ranking["外資賣超"][:5]))
    url = os.environ.get("PAGES_URL")
    if url:
        L += ["", f"👉 完整報告：{url}"]
    return "\n".join(L)


# ---------------------------------------------------------------- 桌面捷徑
def make_shortcut():
    if os.name != "nt":
        print("--make-shortcut 只支援 Windows")
        return
    import ctypes
    buf = ctypes.create_unicode_buffer(260)
    ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf)  # 桌面（含 OneDrive 桌面）
    desktop = buf.value or os.path.join(os.environ["USERPROFILE"], "Desktop")

    bat = os.path.join(desktop, "台股日報.bat")
    content = (
        "@echo off\r\n"
        "title 台股資金面日報\r\n"
        f'"{sys.executable}" "{os.path.abspath(__file__)}" --open\r\n'
        "echo.\r\n"
        "pause\r\n"
    )
    # 用系統 ANSI 編碼（繁中 Windows 為 cp950）存檔，中文路徑才不會變亂碼
    with open(bat, "w", encoding="mbcs") as f:
        f.write(content)
    print(f"已建立：{bat}")


# ---------------------------------------------------------------- main
def find_latest_trading_day(max_back=10):
    """從今天往前找，第一個證交所有三大法人資料的日期"""
    today = datetime.now(TW_TZ)
    for k in range(max_back):
        d = (today - timedelta(days=k))
        if d.weekday() >= 5:      # 週末直接跳過，不浪費請求
            continue
        ds = d.strftime("%Y%m%d")
        try:
            if fetch_institutional_amount(ds):
                return ds
        except Exception:
            pass
        time.sleep(3)
    return None


def main():
    ap = argparse.ArgumentParser(description="台股資金面日報")
    ap.add_argument("--date", help="日期 YYYYMMDD，預設為最近一個交易日")
    ap.add_argument("--open", action="store_true", help="完成後用瀏覽器打開報告")
    ap.add_argument("--make-shortcut", action="store_true", help="在桌面建立 .bat")
    ap.add_argument("--today-only", action="store_true",
                    help="只抓今天；休市就安靜結束（雲端排程用）")
    args = ap.parse_args()

    if args.make_shortcut:
        make_shortcut()
        return

    date_str = args.date
    if args.today_only:
        date_str = datetime.now(TW_TZ).strftime("%Y%m%d")
        try:
            if not fetch_institutional_amount(date_str):
                print(f"{date_str} 沒有資料（休市或尚未公布），結束。")
                return
        except Exception as e:
            print(f"連不到證交所：{e}", file=sys.stderr)
            line_send(f"⚠️ 台股日報：連不到證交所\n{str(e)[:300]}")
            sys.exit(1)
        time.sleep(3)
    elif not date_str:
        print("尋找最近一個交易日...")
        date_str = find_latest_trading_day()
        if not date_str:
            print("找不到最近 10 天的資料，可能是網路問題。")
            return

    print(f"抓取 {date_str} 資料...")
    steps = [
        ("三大法人金額", lambda: fetch_institutional_amount(date_str)),
        ("個股買賣超", lambda: fetch_stock_ranking(date_str)),
        ("台指期未平倉", lambda: fetch_futures_oi(date_str)),
        ("國際行情", lambda: fetch_quotes(date_str)),
    ]
    results = {}
    for name, fn in steps:
        try:
            results[name] = fn()
            print(f"  ✓ {name}" if results[name] else f"  - {name}：無資料")
        except Exception as e:
            results[name] = None
            print(f"  ✗ {name} 失敗：{e}", file=sys.stderr)
        time.sleep(3)   # 證交所有頻率限制，別抓太快

    inst = results["三大法人金額"]
    ranking = results["個股買賣超"]
    fut = results["台指期未平倉"]
    quotes = results["國際行情"] or {}

    if not inst and not fut:
        print("今天沒有台股資料（休市或尚未公布），不產生報告。")
        return

    def q(t):
        return f"{quotes[t][0]:.3f}" if t in quotes else ""

    d = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    history = save_history({
        "日期": d,
        "外資(億)": f"{inst['外資'] / 1e8:.2f}" if inst else "",
        "投信(億)": f"{inst['投信'] / 1e8:.2f}" if inst else "",
        "自營商(億)": f"{inst['自營商'] / 1e8:.2f}" if inst else "",
        "三大法人合計(億)": f"{inst['合計'] / 1e8:.2f}" if inst and inst["合計"] is not None else "",
        "外資台指期未平倉淨口數": f"{fut['外資']['未平倉淨口數']:.0f}"
            if fut and "外資" in fut and fut["外資"]["未平倉淨口數"] is not None else "",
        "美元台幣": q("TWD=X"),
        "加權指數": q("^TWII"),
    })

    report = build_report(date_str, inst, ranking, fut, quotes, history)
    os.makedirs(REPORT_DIR, exist_ok=True)
    path = os.path.join(REPORT_DIR, f"{d}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(report)
    html_path = os.path.join(REPORT_DIR, f"{d}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(md_to_html(report, f"台股資金面日報 {d}"))
    write_index(report, d)

    print(f"\n報告已存到 {path}")
    line_send(build_summary(d, inst, ranking, fut, quotes))
    if args.open:
        import webbrowser
        webbrowser.open("file:///" + html_path.replace("\\", "/"))
        print("已在瀏覽器打開報告")
    else:
        print()
        print(report)


if __name__ == "__main__":
    main()
