# xjtu-notices

**「等通知大学」自救工具** —— 一个 [Hermes Agent](https://github.com/NousResearch/hermes-agent) skill，把西安交通大学散落在各处的通知（官网 OA / 教务处 / 学院网站 / 微信公众号围墙）聚合起来，每天早晚定时整理成一份简报推送到你的聊天软件。

> 西安交大被戏称为"等通知大学"：重要信息分散在 OA、教务处官网、学院网站和微信公众号里，公众号内容还藏在搜索引擎不可见的微信围墙内。没关注那个号、没刷到那条推送，信息就等于不存在。这个 skill 就是来解决它的。

## 它做什么

每天 7:00 / 22:00（可配置），Hermes Agent 自动：

1. **抓取四个渠道**的新通知（Python 脚本，纯标准库）
2. **URL 去重**——每条通知只推一次，漏跑一轮也不丢
3. 按你的**身份画像**筛选优先级（选课/考试/选拔 > 后勤 > 教师向通知）
4. 对含截止日期的高优条目**抓正文摘要**
5. 推送到 QQ / Telegram / Discord 等 Hermes 支持的平台

## 覆盖的信息源

| 源 | 地址 | 说明 |
|---|---|---|
| OA 全校通知 | `oa.xjtu.edu.cn/zxgg_index.jsp` | 全校所有部门的行政通知，抓前 2 页防高峰挤掉 |
| 教务处（=jwc） | `dean.xjtu.edu.cn/jxxx/jxtz2.htm` + 学生事务 4 子栏目 | 选课/考试/学籍/交流，带分类标签 |
| 钱学森学院 | `bjb.xjtu.edu.cn/xydt/tzgg.htm` | 少年班/试验班选拔；**其他学院用户换成自己学院的栏目即可** |
| 微信公众号 | 本机 [wewe-rss](https://github.com/cooderl/wewe-rss) | 突破微信围墙（robots.txt `Disallow: /`，搜索引擎全瞎） |

教务处和钱院网站有一个 JS 算术挑战反爬门（`POST /dynamic_challenge`），脚本内置了 bypass：读页面里的算式 → 算结果 → 按它的 JS 哈希函数签名 → 拿 `client_id` cookie。

## 安装

前置：已安装 [Hermes Agent](https://hermes-agent.nousresearch.com/)（gateway 在跑）。

```bash
# 1. clone 即安装（仓库根本身就是 skill 目录）
git clone https://github.com/fsadj/xjtu-notices ~/.hermes/skills/xjtu-notices

# 2. 编辑 SKILL.md 开头的身份画像（影响筛选优先级）

# 3. 手动跑一次验证 + 初始化去重 state
python3 ~/.hermes/skills/xjtu-notices/scripts/fetch_notices.py

# 4. 建定时任务（每天 7:00/22:00，推送到 qqbot；换成你的平台）
hermes cron create "0 7,22 * * *" \
  --name "交大通知早晚报" \
  --deliver qqbot \
  --skill xjtu-notices \
  "为用户整理西安交大新通知，按 skill 流程生成详尽简报。"

# 5. 测试一轮
hermes cron list   # 拿到 job_id
hermes cron run <job_id>
```

## 公众号订阅（可选，但推荐）

公众号内容搜索引擎不可见（微信 robots.txt 全站 `Disallow: /`），需要 [wewe-rss](https://github.com/cooderl/wewe-rss) 做墙内代理——基于微信读书账号读取，扫码登录。

```bash
# 部署（两个坑都替你踩过了：必须钉死公共 DNS + IPv4 优先，否则间歇 500）
mkdir -p ~/wewe-rss-data
docker run -d --name wewe-rss -p 4000:4000 \
  -e DATABASE_TYPE=sqlite \
  -e AUTH_CODE=$(openssl rand -hex 8) \
  -e TZ=Asia/Shanghai \
  -e "NODE_OPTIONS=--dns-result-order=ipv4first" \
  --dns 223.5.5.5 --dns 119.29.29.29 \
  -v ~/wewe-rss-data:/app/data \
  --restart unless-stopped \
  cooderl/wewe-rss-sqlite:latest
```

然后 `localhost:4000/dash/accounts` 扫码登录微信读书（**不要**勾选"24小时后自动退出"），再往 `.env` 加一行：

```
XJTU_WEWE_RSS_FEEDS=http://localhost:4000/feeds/all.json
```

### 订阅队列（防封控）

wewe-rss 添加公众号走微信读书接口，**添加频率过高会被封控 24h**。`scripts/add_wewe_feeds.py` 提供限速队列：每天自动加 2 个、间隔 75 秒、失败自动重试、状态持久化。

```bash
python3 scripts/add_wewe_feeds.py init                 # 写入交大常用公众号清单
# 从微信里复制每个公众号任意一篇文章的链接（唯一的墙外人工步骤）：
python3 scripts/add_wewe_feeds.py enqueue "西安交通大学教务处" "https://mp.weixin.qq.com/s/xxxx"
python3 scripts/add_wewe_feeds.py run                  # 立即消化 2 个
python3 scripts/add_wewe_feeds.py status

# 配 cron 每天自动消化（脚本无任务时零输出 = 静默）
cp scripts/add_wewe_feeds.py ~/.hermes/scripts/
hermes cron create "7 10 * * *" --name "公众号订阅队列" \
  --script add_wewe_feeds.py --no-agent --deliver qqbot
```

为什么需要文章链接：wewe-rss 的接口只认 `https://mp.weixin.qq.com/s/` 开头的文章 URL 来反查公众号 ID，而搜狗微信（唯一能搜公众号文章的地方）反爬严格，自动发现不可靠。每个号只需一篇种子文章，之后自动追踪。

## 已知盲区（诚实声明）

- 公众号只覆盖**已订阅**的账号；学校公众号几百个，按需往队列里加
- 辅导员 QQ 群 / 班级群 / 学生邮件里的通知不在覆盖内（邮件可后续用 IMAP 接入）
- OA 单日全校通知超过 50 条才会漏（抓 2 页容量，开学季峰值约 10-20 条/天）

## 文件

```
├── SKILL.md                    # Hermes skill 定义（安装后先改身份画像）
└── scripts/
    ├── fetch_notices.py        # 四源抓取 + 去重 + 正文提取（纯标准库）
    └── add_wewe_feeds.py       # wewe-rss 公众号订阅限速队列
```

## License

MIT
