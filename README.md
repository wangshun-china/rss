# RSS 信息推流到飞书

定时拉取 Reddit 版块、Hacker News 和自定义 RSS/Atom 源，去重后以卡片消息推送到飞书群，非中文内容自动附 AI 中文翻译与整批总结。

> 历史说明：本项目最初还订阅 X (Twitter)，因 twitterapi.io 按查询计费已于 2026-08 停用并删除相关代码。如需恢复，可参考 git 历史（`poller.py` / `deploy.yml`）。

## 架构

全部跑在 GitHub Actions，无服务器、无容器、零成本（public 仓库分钟数免费）：

```
GitHub Actions（ubuntu 虚拟机，跑完即销毁）
   └> reddit.yml 每小时 :47：python main.py（RSS_SOURCES=reddit,hn,rss）
       ├─ Reddit   hot 热门榜，匿名直连 RSS（Azure 出口 IP 不被拦截）
       ├─ HN       Algolia API，低于 hn_min_points 的帖子跳过
       ├─ 通用 RSS config.yaml 的 generic_feeds（arXiv 带 AI 相关性过滤）
       ├─ 🧧 羊毛雷达 deals.py：OpenRouter 免费模型 / Reddit 关键词 /
       │    GitHub 羊毛清单 / HN 关键词，AI 判定"真能领"才推送
       └> 去重状态 state-reddit.json 提交回仓库
   └> trend.yml 每天上午 9 点（北京时间）：RSS_SOURCES=trending,radar,papers
       ├─ 🔥 GitHub Trending 榜（不去重，连续上榜如实推送）
       ├─ 🤗 Hugging Face 模型雷达（趋势榜 API）
       └─ 📄 HF 论文日报（Daily Papers API，按赞数取前 10）
```

- Reddit 正文直接来自 RSS `<content>`（OAuth 备用通道读 selftext），
  不逐帖请求匿名 .json 详情（该接口 2026-05 起对数据中心 IP 全面 403）。
- 飞书卡片发送前自动做整卡大小预算（`fit_card`），满载内容超限时逐块压缩，
  避免超限被拒后形成"失败->重试更大卡片"的死循环。
- 拉取/推送失败时 `if: failure()` 步骤发飞书告警；范围内全部源失败时
  main.py 以非零码退出触发该告警。
- `main.py --dry-run` 只打印卡片，不写去重状态，可反复试跑。
- 首次运行某源只记录基线不推送，避免刷屏。

## 通用 RSS 订阅

`config.yaml` 的 `generic_feeds` 可接入任何标准 RSS/Atom（无凭据）：
AI 官方博客、GitHub Releases（`github.com/{owner}/{repo}/releases.atom`）、
arXiv（`export.arxiv.org/rss/cs.CL`）、V2EX、YouTube 频道、
Google News 关键词等。卡片每源最多 `max_items_per_card` 条，突发大量更新时
积压会在后续小时逐步排空。取消注释并在 `RSS_SOURCES` 中包含 `rss` 即生效
（reddit.yml 已包含）。

## GitHub Trending / 模型雷达

`config.yaml` 的 `github_trending`（`since`: daily/weekly/monthly，`languages`
留空只订总榜）与 `hf_radar`（HF 趋势榜模型）由 `trend.yml` **每天上午 9 点**
推送。两个榜单**不去重**：同一仓库/模型连续上榜会如实重复推送。

## 卡片长度说明

飞书单卡硬上限 30KB，无法突破。策略是"按条数均摊预算"：一次新帖越少，
单帖可展示的正文和译文越长（仅 1 帖时约 7300 字正文 + 全量对应译文）；
AI 翻译输入与展示截断对齐，保证译文覆盖卡片上显示的全部内容。
`fit_card` 作为最后防线兜底整卡大小。

## 配置

订阅列表、单卡条数上限等在 `config.yaml`；密钥走本仓库 GitHub Secrets：

| 名称 | 必填 | 说明 |
|---|---|---|
| `FEISHU_WEBHOOK` | 是 | 飞书群机器人 webhook |
| `FEISHU_SECRET` | 否 | 飞书签名校验密钥 |
| `AI_API_BASE` / `AI_API_KEY` | 否 | OpenAI 兼容接口，启用中文翻译与 AI 总结 |
| `AI_MODEL` | 否 | 模型名，默认 `deepseek-v4-flash-0731` |
| `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` | 否 | Reddit OAuth 兜底凭据，一般不配 |

未配置 AI 或调用失败时自动降级为只推原文。本地开发复制 `.env.example`
为 `.env` 填入真实值（本地直连 Reddit 困难时可配 `REDDIT_PROXY`）。

## 本地测试

```bash
pip install -r requirements.txt
python main.py --dry-run   # 只打印将推送的卡片内容，不发送、不写状态
python main.py             # 正式推送
```

想看卡片效果：删掉 `state-reddit.json` 里某个源的几条 ID 再跑 dry-run
（dry-run 不落盘，可以反复试跑，不会吃掉待推内容）。

## 日常发布

改完代码 push 到 master 即可；也可在 Actions 页面手动 Run workflow。
修改订阅（Reddit 版块 / RSS 源）同样只改 `config.yaml` 推送。

## 飞书机器人配置步骤

1. 打开目标飞书群 -> 设置 -> 群机器人 -> 添加机器人 -> 自定义机器人。
2. 复制 **Webhook 地址** 填入 Secret `FEISHU_WEBHOOK`。
3. 安全设置建议选 **签名校验**，密钥填入 Secret `FEISHU_SECRET`
   （关键词校验需保证卡片含固定词；IP 白名单不适用云端出口）。

## Reddit 说明

Reddit 自 2025-11 起**关闭自服务 API 申请**（Responsible Builder Policy），个人无法再
创建 OAuth 应用；2026-05 起匿名 `.json` 接口也全面 403。因此本项目的 Reddit 订阅改为
**GitHub Actions 直连匿名 RSS**（Azure 出口 IP 实测可用），不需要任何 Reddit 凭据。
`REDDIT_PROXY` / OAuth 相关配置仅作为本地环境或备用路径保留在代码里。
