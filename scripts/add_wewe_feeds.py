#!/usr/bin/env python3
"""wewe-rss 公众号订阅队列管理 — 把公众号逐个、限速地加进 wewe-rss。

为什么要队列: wewe-rss 添加公众号走微信读书接口, 添加频率过高会触发微信封控
(账号进"小黑屋"24h)。本脚本每次最多加 MAX_PER_RUN 个, 间隔 GAP 秒, 状态持久化,
配 cron 每天跑一次自动消化队列。

API 链路 (均需 authorization: <AUTH_CODE> 头):
  POST /trpc/platform.getMpInfo  {"wxsLink": "https://mp.weixin.qq.com/s/xxx"}
      → 返回 {id, mpName, mpCover, mpIntro, updateTime, ...}
  POST /trpc/feed.add            {id, mpName, mpCover, mpIntro, updateTime}
  POST /trpc/feed.refreshArticles {...}   (尽力触发同步, 失败不影响)

用法:
  python3 add_wewe_feeds.py status                 # 看队列
  python3 add_wewe_feeds.py enqueue "公众号名" "https://mp.weixin.qq.com/s/xxx"
  python3 add_wewe_feeds.py run                    # 消化队列 (默认每轮 2 个)
  python3 add_wewe_feeds.py run --max 3 --gap 90   # 每轮 3 个, 间隔 90s
  python3 add_wewe_feeds.py init                   # 写入推荐公众号的初始队列 (待补链接)

队列文件: ~/.hermes/xjtu-notices/feeds_queue.json
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = "http://localhost:4000"
QUEUE_PATH = os.path.expanduser("~/.hermes/xjtu-notices/feeds_queue.json")
MAX_PER_RUN = 2   # 保守值: 每轮最多添加数 (防微信读书封控)
GAP_SECONDS = 75  # 两次添加之间的间隔

# 推荐订阅清单 (link 空 = 需要人工从微信复制一篇文章链接)
RECOMMENDED = [
    {"name": "西安交通大学", "note": "校方官方, 重大通知首发"},
    {"name": "西安交通大学教务处", "note": "选课/考试/学籍"},
    {"name": "西安交大钱学森学院", "note": "少年班/试验班选拔, 最关键"},
    {"name": "西安交通大学财务处", "note": "学杂费缴费"},
    {"name": "西安交通大学网络信息中心", "note": "校园网/系统维护/正版软件"},
    {"name": "西安交通大学图书馆", "note": "讲座/数据库/开放时间"},
    {"name": "西安交大就业创业", "note": "实习/校招"},
]


def auth_code():
    if os.environ.get("WEWE_AUTH_CODE"):
        return os.environ["WEWE_AUTH_CODE"]
    try:  # 从容器环境拿 (容器重建也一致)
        out = subprocess.run(["docker", "exec", "wewe-rss", "printenv", "AUTH_CODE"],
                             capture_output=True, text=True, timeout=15)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    sys.exit("拿不到 AUTH_CODE: 请 export WEWE_AUTH_CODE=... 或保证 docker 容器 wewe-rss 在跑")


def trpc(path, payload):
    """POST 一个 tRPC mutation, 返回 (ok, data_or_error)。"""
    req = urllib.request.Request(
        f"{BASE}/trpc/{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "authorization": auth_code()},
    )
    try:
        body = urllib.request.urlopen(req, timeout=40).read().decode()
        data = json.loads(body)
        if isinstance(data, list):
            data = data[0]
        if "error" in data:
            return False, data["error"].get("message") or str(data["error"])
        return True, data.get("result", {}).get("data", data.get("result"))
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode())
        except Exception:
            detail = e.reason
        return False, detail
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def load_queue():
    if os.path.exists(QUEUE_PATH):
        with open(QUEUE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    return []


def save_queue(q):
    os.makedirs(os.path.dirname(QUEUE_PATH), exist_ok=True)
    tmp = QUEUE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(q, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, QUEUE_PATH)


def add_one(link):
    """完整添加流程: 解析 → 入库 → 触发同步。返回 (ok, msg)。"""
    ok, info = trpc("platform.getMpInfo", {"wxsLink": link})
    if isinstance(info, list):  # 中转返回的是单元素数组
        info = info[0] if info else None
    if not ok or not isinstance(info, dict):
        return False, f"getMpInfo 失败: {info}"
    feed = {
        "id": info.get("id") or info.get("mpId"),
        "mpName": info.get("mpName") or info.get("name") or "",
        "mpCover": info.get("mpCover") or info.get("cover") or "",
        "mpIntro": info.get("mpIntro") or info.get("intro") or "",
        "updateTime": info.get("updateTime") or int(time.time()),
    }
    if not feed["id"]:
        return False, f"getMpInfo 返回缺 id: {json.dumps(info, ensure_ascii=False)[:200]}"
    ok, res = trpc("feed.add", feed)
    if not ok:
        return False, f"feed.add 失败: {res}"
    trpc("feed.refreshArticles", {"id": feed["id"]})  # 尽力触发, 失败无所谓
    return True, feed["mpName"] or feed["id"]


def cmd_init(args):
    q = load_queue()
    have = {e["name"] for e in q}
    for r in RECOMMENDED:
        if r["name"] not in have:
            q.append({"name": r["name"], "note": r["note"], "link": "", "status": "pending"})
    save_queue(q)
    print(f"队列已初始化, 共 {len(q)} 个 (待补文章链接)")


def cmd_enqueue(args):
    if not args.link.startswith("https://mp.weixin.qq.com/s/"):
        sys.exit("链接必须是 https://mp.weixin.qq.com/s/ 开头的公众号文章地址")
    q = load_queue()
    for e in q:
        if e["name"] == args.name:
            e.update({"link": args.link, "status": "pending"})
            save_queue(q)
            print(f"已更新: {args.name}")
            return
    q.append({"name": args.name, "note": "", "link": args.link, "status": "pending"})
    save_queue(q)
    print(f"已入队: {args.name}")


def cmd_run(args):
    q = load_queue()
    todo = [e for e in q if e["status"] == "pending" and e.get("link")][: args.max]
    if not todo:
        # cron 静默模式: 无事可做时零输出 (no-agent cron 空输出 = 不投递)
        return
    done, failed = [], []
    for i, entry in enumerate(todo):
        ok, msg = add_one(entry["link"])
        if not ok:  # 中转连接偶发 500/AggregateError, 等 25s 重试一次
            time.sleep(25)
            ok, msg = add_one(entry["link"])
        if ok:
            entry["status"], entry["error"] = "added", ""
            entry["added_at"] = time.strftime("%Y-%m-%d %H:%M")
            done.append(f"{entry['name']} → {msg}")
        else:
            entry["status"], entry["error"] = "failed", str(msg)
            failed.append(f"{entry['name']}: {msg}")
        save_queue(q)
        if i < len(todo) - 1:
            time.sleep(args.gap)
    if done:
        print("✅ 已添加:\n" + "\n".join("- " + d for d in done))
    if failed:
        print("❌ 失败:\n" + "\n".join("- " + f for f in failed))
    remain = [e for e in q if e["status"] == "pending" and e.get("link")]
    if remain:
        print(f"⏳ 队列还剩 {len(remain)} 个待添加, 下轮继续")


def cmd_status(args):
    q = load_queue()
    if not q:
        print("队列为空, 先跑 init")
        return
    icon = {"added": "✅", "pending": "⏳", "failed": "❌"}
    for e in q:
        line = f"{icon.get(e['status'], '?')} {e['name']} [{e['status']}]"
        if e.get("note"):
            line += f" — {e['note']}"
        if not e.get("link") and e["status"] == "pending":
            line += " — 需补文章链接"
        if e.get("error"):
            line += f" ({e['error'][:60]})"
        print(line)


def main():
    if len(sys.argv) == 1:
        sys.argv = [sys.argv[0], "run"]  # cron 裸调用 = 消化队列
    ap = argparse.ArgumentParser(description="wewe-rss 公众号订阅队列")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(func=cmd_init)
    sub.add_parser("status").set_defaults(func=cmd_status)
    run_p = sub.add_parser("run")
    run_p.add_argument("--max", type=int, default=MAX_PER_RUN)
    run_p.add_argument("--gap", type=int, default=GAP_SECONDS)
    run_p.set_defaults(func=cmd_run)
    enq = sub.add_parser("enqueue")
    enq.add_argument("name")
    enq.add_argument("link")
    enq.set_defaults(func=cmd_enqueue)
    args = ap.parse_args()
    if args.cmd in ("run",):
        auth_code()  # 提前校验
    args.func(args)


if __name__ == "__main__":
    main()
