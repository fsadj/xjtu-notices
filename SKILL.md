---
name: xjtu-notices
description: 整理西安交大通知 (XJTU notices digest: OA/教务处/钱院/公众号)
version: 0.1.0
author: brazion
license: MIT
platforms: [macos, linux]
metadata:
  hermes:
    tags: [xjtu, notices, university, digest, cron]
---

# XJTU Notices (西安交大通知整理)

搜集西安交通大学的新通知并整理成简报。数据由 `scripts/fetch_notices.py` 抓取，你只负责
**筛选、摘要、排版**——不要自己去找通知源，不要编造脚本输出里没有的条目。

> **安装后先改这一段**：把下面的身份画像换成使用者自己的情况，直接影响筛选优先级。
> 示例（替换我）：用户是**钱学森书院少年班 2026 级（数学）本科生**，对选课/考试/少年班
> 选拔/讲座类通知最敏感。如果是研究生，把高优先级改成：导师/课题组、奖学金、答辩、学术报告。

## When to Use

- 每日 7:00 / 22:00 的定时早晚报（cron 调用）
- 用户问"今天有什么新通知 / 有没有关于 X 的通知"（用 `--all` 或 `--lookback` 调参）

## Prerequisites

- Python 3（标准库即可，无第三方依赖）
- 微信公众号订阅（可选）：本机 wewe-rss（`http://localhost:4000`）+ `.env` 里的
  `XJTU_WEWE_RSS_FEEDS`。未配置时脚本自动跳过该源，不是错误。

## How to Run

```bash
# 常规（cron 用这个）：输出上次运行以来的新通知，并记录去重 state
python3 ~/.hermes/skills/xjtu-notices/scripts/fetch_notices.py

# 按需查询：最近 72 小时全部条目（忽略去重，不写 state）
python3 ~/.hermes/skills/xjtu-notices/scripts/fetch_notices.py --all --lookback 72 --no-update
```

## Quick Reference

- 输出按源分组：`OA 全校通知` / `教务处` / `钱学森学院` / `公众号 (wewe-rss)`
- 条目格式：`[日期][分类] 标题 — URL`；教务处带 `[课程安排]` `[考试安排]` 等分类标签
- state 在 `~/.hermes/xjtu-notices/state.json`；`--no-update` 预览不写
- 末尾 `⚠️ 来源异常` 表示某个源抓取失败——如实转告用户，不要假装它没有通知

## Procedure

1. 用 `terminal` 跑脚本（命令见 How to Run），拿到 Markdown 清单
2. **筛选**：与用户相关的排前面——
   - 高：选课/考试/成绩/少年班与试验班选拔/推免/奖学金/缴费截止/讲座报告/电脑网络
   - 中：后勤停水停电施工、场馆开放、社团活动
   - 低（一句话带过或省略）：教师向通知（招聘/工会/人事）、与本科生无关的公示
3. **摘要**：标题信息量足够时不必打开详情；对**含截止日期/时间地点**的高优先级条目，
   用脚本的 `--detail` 模式抓正文（教务处/钱院有反爬，`web_extract` 会失败）：
   ```bash
   python3 ~/.hermes/skills/xjtu-notices/scripts/fetch_notices.py --detail "<URL>"
   ```
   （最多抓 5 条，控制耗时）
4. **排版**输出（QQ 消息，**详尽模式**——用户明确表示"不嫌啰嗦，尽量翔实"）：
   - 开头总结（"今天 N 条新通知，其中 M 条需要你 action"）
   - 高优先级：逐条展开——标题、发布单位、截止/时间地点（**加粗**）、内容摘要 1-3 句、链接
   - 中低优先级**不省略**：逐条一行列出（标题 + 链接），保证不丢信息
   - 总长尽量控制在 4000 字内（QQ 平台截断上限）；超了优先砍低优先级的摘要展开，不砍条目数
   - 没有新通知时**不要用 [SILENT]**——用户希望收到确认。回复一句简短的
     `📭 过去X小时没有新通知（OA/教务处/钱院抓取均正常）`
5. 若脚本失败（退出码非 0）：把 stderr 概要告诉用户，不要静默

## Pitfalls

- 列表有**置顶**（日期乱序），不要按出现顺序当时间序；一律以条目自带日期为准
- 首次运行会把当前列表页全部当作"新增"（state 为空）——这是预期，正常输出即可
- OA 链接里的 `processInsId` 含 `[` 等字符，抄 URL 时必须完整原样保留
- 教务处子栏目里 2013-2023 年的"XX流程"是常驻文档，脚本已按日期过滤；若用 `--all` 看到
  它们，别当新闻报

## Verification

- 手动跑一次脚本确认退出码 0、各源有输出
- 检查 `~/.hermes/xjtu-notices/state.json` 存在且 `seen` 非空
- 同一条通知跑两次只出现一次（URL 去重生效）
