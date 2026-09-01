# RSS 推流项目上下文

## 当前架构

- 全部源跑在 GitHub Actions：`reddit.yml` 每小时跑 reddit/hn/rss（Reddit 用 hot 热门榜，HN 有 hn_min_points 门槛，arXiv 在 generic_feeds 里配 filter_topic 走 AI 相关性过滤、被剔除条目记入 drops 避免重判）；`trend.yml` 每天北京时间 9 点跑 trending/radar。两个 workflow 共用并发组 rss-state 且回写前 `git pull --rebase`，状态都在 `state-reddit.json`；失败有 `if: failure()` 飞书告警。
- 趋势类源（GitHub Trending / 模型雷达）不去重：id 带日期戳，连续上榜如实重复推送，limit 控制单卡容量一次推完。
- Reddit 正文取自 RSS `<content>`（OAuth 备用通道读 selftext），匿名 `.json` 详情接口已废弃（2026-05 起全面 403）；HN 外链帖附 Algolia 热评。
- 通用 RSS 源：`config.yaml` 的 `generic_feeds`（name+url），由 `sources/generic_rss.py` 拉取，积压只记录已展示条目、后续小时排空；GitHub Trending 同策略（`sources/github_trending.py`，解析页面无官方 API，按仓库 id 去重只推首榜）。
- 飞书卡片支持 AI 中文翻译和批量总结，未配置或调用失败时降级为原文；卡片文本按条数均摊 30KB 预算（`per_item_caps`），`feishu.send` 发送前 `fit_card` 兜底压缩；AI 翻译输入与展示截断对齐。
- `main.py --dry-run` 不写去重状态；范围内全部源拉取失败时以非零码退出触发告警。
- X (Twitter) 订阅已于 2026-08 停用并删除相关代码（twitterapi.io 按查询计费，服务器容器/镜像已清理）；如需恢复参考 git 历史。

## 约束

- 本项目不部署服务器、不构建镜像，GitHub Actions 是唯一运行时；不要重新引入常驻容器类方案。
- 凭据由当前仓库的 vars/secrets 提供，不写入仓库或项目记忆。

## 验证

```powershell
python main.py --dry-run
```
