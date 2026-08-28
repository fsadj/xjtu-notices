#!/usr/bin/env python3
"""xjtu-notices fetcher — 抓取西安交通大学各通知渠道的新通知，输出去重后的 Markdown 清单。

渠道:
  1. OA 全校通知公告   http://oa.xjtu.edu.cn/zxgg_index.jsp        (直连)
  2. 教务处·教学通知    http://dean.xjtu.edu.cn/jxxx/jxtz2.htm      (JS 挑战反爬, 自动过)
  3. 钱学森学院通知公告 http://bjb.xjtu.edu.cn/xydt/tzgg.htm        (同上)
  4. 微信公众号 (可选)   wewe-rss 订阅源, 由 XJTU_WEWE_RSS_FEEDS 环境变量配置
                        (逗号分隔的 .atom/.rss/.json feed URL, 未配置则跳过)

用法:
  python3 fetch_notices.py                 # 输出上次运行以来的新通知, 并记录 state
  python3 fetch_notices.py --lookback 48   # 只看 48 小时内发布的通知
  python3 fetch_notices.py --all           # 忽略去重, 输出当前列表 (不写 state)
  python3 fetch_notices.py --no-update     # 预览模式, 不写 state
  python3 fetch_notices.py --json          # JSON 输出 (供程序消费)

state 文件: ~/.hermes/xjtu-notices/state.json  (seen URL -> 元数据, 90 天自动清理)
配置来源: ${HERMES_HOME:-~/.hermes}/.env 里的 XJTU_WEWE_RSS_FEEDS

只用标准库; 每个 source 独立容错, 单个挂了不影响其他, 失败信息附在输出末尾。
"""

import argparse
import html as html_mod
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Safari/537.36"
TZ = timezone(timedelta(hours=8))  # 北京时间, 与站点日期一致

OA_PAGES = 2  # OA 列表每页 25 条, 抓 2 页防高峰挤掉
DEAN_SUBCOLUMNS = [  # 教务处学生事务子栏目 (汇总页不完整覆盖)
    ("kcks.htm", "课程考试"),
    ("xjgl.htm", "学籍管理"),
    ("zhyw.htm", "综合业务"),
    ("jljh.htm", "交流交换"),
]

SOURCES = [
    {"key": "oa", "name": "OA 全校通知"},
    {"key": "dean", "name": "教务处"},
    {"key": "qxs", "name": "钱学森学院"},
    {"key": "wechat", "name": "公众号 (wewe-rss)"},
]


# ---------------------------------------------------------------- http 层

class Fetcher:
    """带 cookie jar 的极简 HTTP 客户端; dean/bjb 的 JS 挑战门在这里统一过。"""

    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.opener.addheaders = [("User-Agent", UA)]

    def get(self, url, timeout=20, retries=1):
        last = None
        for attempt in range(retries + 1):
            try:
                resp = self.opener.open(url, timeout=timeout)
                raw = resp.read()
                ctype = resp.headers.get("Content-Type", "")
                m = re.search(r"charset=([\w-]+)", ctype)
                enc = m.group(1) if m else None
                if not enc:
                    head = raw[:2048].decode("ascii", "ignore")
                    m = re.search(r'charset=["\']?([\w-]+)', head, re.I)
                    enc = m.group(1) if m else "utf-8"
                try:
                    return raw.decode(enc, "replace")
                except LookupError:
                    return raw.decode("utf-8", "replace")
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = e
                if attempt < retries:
                    time.sleep(2)
        raise RuntimeError(f"GET {url} 失败: {last}")

    def post_json(self, url, payload, timeout=15):
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        return self.opener.open(req, timeout=timeout).read().decode("utf-8", "replace")

    # --- dean / bjb 的 dynamic_challenge 反爬门 -------------------------
    # 页面内嵌 (challengeId, a, b, operator), 算出结果后 POST, 拿 client_id cookie。
    @staticmethod
    def _js_hash(s):
        h = 0
        for ch in s:
            h = ((h << 5) - h) + ord(ch)
            h &= 0xFFFFFFFF
            if h >= 0x80000000:
                h -= 0x100000000  # JS 32 位溢出行为
        return abs(h)

    def pass_challenge(self, base):
        home = self.get(base + "/")
        if "challengeId" not in home:
            return home  # 没有门 (cookie 仍有效等)
        cid = re.search(r"challengeId = '([^']+)'", home).group(1)
        a = int(re.search(r"var a = (\d+)", home).group(1))
        b = int(re.search(r"var b = (\d+)", home).group(1))
        op = re.search(r"var operator = '([^']+)'", home).group(1)
        ans = a + b if op == "+" else (a - b if op == "-" else a * b)
        payload = {
            "challenge_id": cid,
            "answer": ans,
            "browser_info": {"webdriver": False, "language": "zh-CN", "platform": "MacIntel"},
            "hash": self._js_hash(cid + str(ans) + UA[:10]),
        }
        resp = self.post_json(base + "/dynamic_challenge", payload)
        if '"success":true' not in resp.replace(" ", ""):
            raise RuntimeError(f"{base} 挑战应答异常: {resp[:120]}")
        return self.get(base + "/")


# ---------------------------------------------------------------- 各源解析

def _clean(s):
    return re.sub(r"\s+", " ", html_mod.unescape(re.sub(r"<[^>]+>", "", s or ""))).strip()


def parse_oa(f):
    """OA 列表行: <a onclick="gotodetail('ID')" title="标题"> + <td class="timedate1">部门（日期）</td>
    抓前 OA_PAGES 页 (每页 25 条) — 早晚两次运行间隔最长 15h, 开学季高峰单日可达 30+ 条, 2 页 = 50 条容量。"""
    items = []
    row_re = re.compile(
        r"onclick=\"gotodetail\('([^']+)'\)\"[^>]*title=\"([^\"]*)\".*?class=\"timedate1\"[^>]*>([^<]*)（(\d{4}-\d{2}-\d{2})）",
        re.S,
    )
    for page_no in range(1, OA_PAGES + 1):
        url = "http://oa.xjtu.edu.cn/zxgg_index.jsp" + (f"?strPageNo={page_no}" if page_no > 1 else "")
        page = f.get(url)
        rows = row_re.findall(page)
        if not rows:
            break
        for pid, _title, dept, date in rows:
            items.append({
                "title": _clean(_title),
                "dept": _clean(dept.replace("&nbsp;", " ")),
                "date": date,
                "url": f"http://oa.xjtu.edu.cn/zxgg_infonew.jsp?processInsId={pid}",
            })
    return items


def parse_dean(f):
    """教务处: 汇总页 jxtz2.htm + 学生事务子栏目 (子栏目混有常驻流程文档, 靠 state 去重自然沉淀)。"""
    f.pass_challenge("http://dean.xjtu.edu.cn")
    row_re = re.compile(
        r'<a[^>]+href="([^"]+)"[^>]*>(?:<i>\[([^]]*)\]</i>)?([^<]+)</a>(?:[\s\S]{0,200}?)((?:19|20)\d\d-\d\d-\d\d)'
    )
    items = []

    def grab(list_url, default_cat=""):
        page = f.get(list_url)
        for href, cat, title, date in row_re.findall(page):
            items.append({
                "title": _clean(title),
                "category": _clean(cat) or default_cat,
                "date": date,
                "url": urllib.parse.urljoin(list_url, href),
            })

    grab("http://dean.xjtu.edu.cn/jxxx/jxtz2.htm")
    for slug, cat in DEAN_SUBCOLUMNS:
        grab(f"http://dean.xjtu.edu.cn/xssw/{slug}", cat)
    return items


def parse_qxs(f):
    """钱学森学院通知: <span class="date-list">…日期…</span> … <a href="../info/x/y.htm">标题</a>"""
    f.pass_challenge("http://bjb.xjtu.edu.cn")
    page = f.get("http://bjb.xjtu.edu.cn/xydt/tzgg.htm")
    items = []
    row_re = re.compile(
        r'<span class="date-list">([^<]*)</span>[\s\S]{0,150}?<a[^>]+href="([^"]+)"[^>]*>([^<]+)</a>'
    )
    for date_txt, href, title in row_re.findall(page):
        # date-list 里可能是 "2026-07-26" 或带年被拆开的各种形态, 统一抽数字
        digits = re.findall(r"\d+", date_txt)
        date = "-".join(digits[:3]) if len(digits) >= 3 else ""
        if len(digits) == 2:  # 只有 月-日, 补当前年份
            date = f"{datetime.now(TZ).year}-{int(digits[0]):02d}-{int(digits[1]):02d}"
        items.append({
            "title": _clean(title),
            "date": date,
            "url": urllib.parse.urljoin("http://bjb.xjtu.edu.cn/xydt/tzgg.htm", href),
        })
    return items


def parse_wechat(feeds):
    """wewe-rss 订阅源: /feeds/all.json 或单源 .json — JSON Feed 1.1 格式
    ({items: [{title, url, date_published, author}]}), 兼容列表形态。"""
    items = []
    for feed_url in feeds:
        try:
            json_url = re.sub(r"\.(atom|rss)$", ".json", feed_url.strip())
            f = Fetcher()
            data = json.loads(f.get(json_url))
            feed_title = _clean(data.get("title", "")) if isinstance(data, dict) else ""
            entries = data.get("items", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            for e in entries:
                ts = e.get("date_modified") or e.get("date_published") or e.get("created_at") or ""
                date = ""
                dm = re.search(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d)", str(ts))
                if dm:  # ISO UTC → 北京时间 (+8), 防凌晨文章日期差一天
                    dt = datetime(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)),
                                  int(dm.group(4)), int(dm.group(5)), tzinfo=timezone.utc)
                    date = dt.astimezone(TZ).strftime("%Y-%m-%d")
                else:
                    dm2 = re.search(r"(\d{4}-\d{2}-\d{2})", str(ts))
                    date = dm2.group(1) if dm2 else ""
                author = e.get("author") or ""
                if isinstance(author, dict):
                    author = author.get("name", "")
                m2 = re.search(r"'name'\s*:\s*'([^']+)'", str(author))  # author 是 "{'name': 'X'}" 字符串形态
                if m2:
                    author = m2.group(1)
                items.append({
                    "title": _clean(e.get("title", "")),
                    "account": _clean(str(author)) or feed_title,
                    "date": date,
                    "url": e.get("url") or e.get("link") or e.get("external_url") or feed_url,
                })
        except Exception as e:  # 单个 feed 挂了不影响其他
            items.append({"title": f"⚠ 订阅源读取失败: {feed_url}", "account": "", "date": "", "url": feed_url, "error": str(e)})
    return items


def load_feeds_from_env():
    """从 ${HERMES_HOME:-~/.hermes}/.env 读 XJTU_WEWE_RSS_FEEDS (逗号分隔)。"""
    home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    env_path = os.path.join(home, ".env")
    try:
        with open(env_path, encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r"\s*(?:export\s+)?XJTU_WEWE_RSS_FEEDS\s*=\s*['\"]?([^'\"\n#]+)", line)
                if m:
                    return [u for u in m.group(1).strip().split(",") if u.strip()]
    except OSError:
        pass
    raw = os.environ.get("XJTU_WEWE_RSS_FEEDS", "")
    return [u for u in raw.split(",") if u.strip()]


# ---------------------------------------------------------------- state

STATE_KEYS_MAX = 5000
PRUNE_DAYS = 90


def state_path():
    home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    d = os.path.join(home, "xjtu-notices")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "state.json")


def load_state():
    try:
        with open(state_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {"seen": {}, "last_run": None}


def save_state(state, now_iso):
    state["last_run"] = now_iso
    seen = state["seen"]
    if len(seen) > STATE_KEYS_MAX:  # 按首次见到时间裁剪
        keep = sorted(seen.items(), key=lambda kv: kv[1].get("first_seen", ""))[-STATE_KEYS_MAX:]
        state["seen"] = dict(keep)
    tmp = state_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False)
    os.replace(tmp, state_path())


def prune_old(state, now):
    cutoff = (now - timedelta(days=PRUNE_DAYS)).strftime("%Y-%m-%d")
    kept = {k: v for k, v in state["seen"].items() if v.get("date", "9999") >= cutoff}
    state["seen"].clear()  # 原地更新, 保持 main() 里 seen 别名有效
    state["seen"].update(kept)


# ---------------------------------------------------------------- main

def fetch_detail(url):
    """抓单条通知正文 (带 dean/bjb 反爬 bypass), 供 --detail 模式。
    返回纯文本正文; OA 详情页无需 bypass。"""
    f = Fetcher()
    for base in ("http://dean.xjtu.edu.cn", "http://bjb.xjtu.edu.cn"):
        if base in url:
            f.pass_challenge(base)
            break
    page = f.get(url)
    if "zxgg_infonew" in url:
        # OA 详情: 正文在 "发布于…" 时间戳之后
        i = page.find("发布于")
        body = page[i:] if i > 0 else page
        body = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", "", body, flags=re.I)
        return _clean(body)[:4000]
    # vsb CMS 正文容器; OA 详情页无此结构, 回退到全文
    m = re.search(r'class="v_news_content"[^>]*>([\s\S]*?)(?:</div>|<div class="footer)', page)
    body = m.group(1) if m else page
    body = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", "", body, flags=re.I)
    title_m = re.search(r"<title>([^<]*)</title>", page)
    text = (title_m.group(1).strip() + "\n\n" if title_m else "") + _clean(body)
    return text[:4000]


def main():
    ap = argparse.ArgumentParser(description="抓取西安交大新通知")
    ap.add_argument("--detail", metavar="URL", help="抓单条通知正文 (过反爬), 而不是跑列表")
    ap.add_argument("--lookback", type=int, default=36, help="只报告最近 N 小时内 (按通知日期) 的条目")
    ap.add_argument("--max-per-source", type=int, default=20)
    ap.add_argument("--all", action="store_true", help="忽略去重 state, 输出列表页全部条目")
    ap.add_argument("--no-update", action="store_true", help="不写回 state (预览)")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args()

    if args.detail:
        print(fetch_detail(args.detail))
        return

    now = datetime.now(TZ)
    state = load_state()
    seen = state["seen"]
    cutoff_date = (now - timedelta(hours=args.lookback)).strftime("%Y-%m-%d")

    fetcher = Fetcher()
    parsers = {
        "oa": lambda: parse_oa(fetcher),
        "dean": lambda: parse_dean(fetcher),
        "qxs": lambda: parse_qxs(fetcher),
    }
    feeds = load_feeds_from_env()
    if feeds:
        parsers["wechat"] = lambda: parse_wechat(feeds)

    results, errors = {}, {}
    for src in SOURCES:
        fn = parsers.get(src["key"])
        if not fn:
            continue
        try:
            results[src["key"]] = fn()
        except Exception as e:
            errors[src["key"]] = f"{type(e).__name__}: {e}"

    report = {}
    for key, items in results.items():
        # 过滤: 日期在窗口内 (无日期的保留, 常见于公众号) 且没见过
        fresh = []
        for it in items:
            if it.get("error"):
                errors.setdefault("wechat_detail", []).append(it["title"])
                continue
            if not args.all:
                if it["url"] in seen:
                    continue
                if it.get("date") and it["date"] < cutoff_date:
                    continue
            fresh.append(it)
        # 同一源内按标题去重 (列表偶尔重复挂载)
        deduped, seen_t = [], set()
        for it in fresh:
            if it["title"] not in seen_t:
                seen_t.add(it["title"])
                deduped.append(it)
        report[key] = deduped[: args.max_per_source]

    # 写 state
    if not args.no_update:
        prune_old(state, now)
        for key, items in results.items():
            for it in items:
                if it.get("error"):
                    continue
                if it["url"] not in seen:
                    seen[it["url"]] = {
                        "first_seen": now.strftime("%Y-%m-%d %H:%M"),
                        "date": it.get("date", ""),
                        "title": it.get("title", "")[:80],
                    }
        save_state(state, now.strftime("%Y-%m-%d %H:%M"))

    if args.json:
        print(json.dumps({
            "generated_at": now.strftime("%Y-%m-%d %H:%M"),
            "window": f"since {cutoff_date}",
            "sources": {k: report.get(k, []) for k in parsers},
            "errors": errors,
        }, ensure_ascii=False, indent=1))
        return

    # ---- Markdown 输出 ----
    names = {s["key"]: s["name"] for s in SOURCES}
    lines = [f"# 西安交大新通知 ({now.strftime('%m-%d %H:%M')})", ""]
    total = sum(len(v) for v in report.values())
    if total == 0 and not errors:
        lines.append("(自上次运行以来没有新通知)")
    for src in SOURCES:
        items = report.get(src["key"])
        if items is None:
            continue
        if not items:
            lines += [f"## {names[src['key']]}", "(无新增)", ""]
            continue
        lines.append(f"## {names[src['key']]} ({len(items)})")
        for it in items:
            bits = [f"[{it.get('date', '??-??')[5:]}]"] if it.get("date") else []
            if it.get("category"):
                bits.append(f"[{it['category']}]")
            if it.get("dept"):
                bits.append(f"{it['dept']} |")
            elif it.get("account"):
                bits.append(f"{it['account']} |")
            lines.append(f"- {' '.join(bits)} {it['title']} — {it['url']}")
        lines.append("")
    if errors:
        lines.append("## ⚠️ 来源异常")
        for k, v in errors.items():
            lines.append(f"- {names.get(k, k)}: {v}")
        lines.append("")
    lines.append(f"统计: {total} 条新增 | 窗口: {cutoff_date} 起 | state: {'已更新' if not args.no_update else '未更新'}")
    print("\n".join(lines))


if __name__ == "__main__":
    sys.exit(main())
