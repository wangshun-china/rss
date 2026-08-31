# RSS 推流项目上下文

## 当前架构

- X：已停用（twitterapi.io 按查询计费）。仓库变量 `ENABLE_X_PUSH` 控制：`true` 时容器每 2 小时 `advanced_search + since_id` 增量轮询，否则容器空转零调用；`since_id` 游标保留在挂载卷，恢复后自动补拉。
- Reddit/Hacker News/通用 RSS：`.github/workflows/reddit.yml` 定时执行（RSS_SOURCES=reddit,hn,rss），状态写回 `state-reddit.json`；拉取/推送失败有 `if: failure()` 飞书告警步骤。
- Reddit 正文取自 RSS `<content>`（OAuth 备用通道读 selftext），匿名 `.json` 详情接口已废弃（2026-05 起全面 403）；HN 外链帖附 Algolia 热评。
- 通用 RSS 源：`config.yaml` 的 `generic_feeds`（name+url），由 `sources/generic_rss.py` 拉取，积压只记录已展示条目、后续小时排空。
- 飞书卡片支持 AI 中文翻译和批量总结，未配置或调用失败时降级为原文；`feishu.send` 发送前 `fit_card` 自动压缩超限卡片（30KB 上限）。
- `main.py --dry-run` 不写去重状态；范围内全部源拉取失败时以非零码退出触发告警。

## 部署约束

- Actions 构建同一 SHA 镜像并推送 GHCR 与阿里云 ACR；ACR 个人版要求 buildx `provenance: false`。
- 国内服务器拉 GHCR 有卡死风险，部署脚本限时后回退 ACR。
- self-hosted Runner 的非交互环境可能没有 `HOME`，从 passwd 解析用户目录。
- 容器只运行 X；Reddit/HN/通用 RSS 留在 GitHub Actions，避免两边重复推送。
- 凭据由当前仓库的 vars/secrets 提供，不写入仓库或项目记忆。

## 验证

```powershell
python main.py --dry-run
```

涉及部署时同时检查 `.github/workflows/deploy.yml`、`entrypoint.sh` 和实际入口，README 或旧文件名不能替代运行态证据。
