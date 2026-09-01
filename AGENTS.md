# RSS 推流项目上下文

## 当前架构

- 全部源跑在 GitHub Actions：`.github/workflows/reddit.yml` 每小时执行（RSS_SOURCES=reddit,hn,rss），去重状态写回 `state-reddit.json`；拉取/推送失败有 `if: failure()` 飞书告警步骤。
- Reddit 正文取自 RSS `<content>`（OAuth 备用通道读 selftext），匿名 `.json` 详情接口已废弃（2026-05 起全面 403）；HN 外链帖附 Algolia 热评。
- 通用 RSS 源：`config.yaml` 的 `generic_feeds`（name+url），由 `sources/generic_rss.py` 拉取，积压只记录已展示条目、后续小时排空。
- 飞书卡片支持 AI 中文翻译和批量总结，未配置或调用失败时降级为原文；`feishu.send` 发送前 `fit_card` 自动压缩超限卡片（30KB 上限）。
- `main.py --dry-run` 不写去重状态；范围内全部源拉取失败时以非零码退出触发告警。
- X (Twitter) 订阅已于 2026-08 停用并删除相关代码（twitterapi.io 按查询计费，服务器容器/镜像已清理）；如需恢复参考 git 历史。

## 约束

- 本项目不部署服务器、不构建镜像，GitHub Actions 是唯一运行时；不要重新引入常驻容器类方案。
- 凭据由当前仓库的 vars/secrets 提供，不写入仓库或项目记忆。

## 验证

```powershell
python main.py --dry-run
```
