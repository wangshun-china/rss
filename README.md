# RSS 信息推流到飞书

定时拉取指定 X (Twitter) 账号的推文和 Reddit 版块的新帖，去重后以卡片消息推送到飞书群。

## 架构

三通道分工，各用各的长处：

```
X 订阅（服务器容器，常驻轮询）
git push master
   └> Actions 构建 SHA 镜像双推 GHCR+ACR
       └> aliyun Runner compose 启动 rss-push 常驻容器
           └> poller.py 每轮 advanced_search + since_id 增量拉取
               按作者分组 -> AI 翻译总结 -> 飞书卡片
           去重状态：宿主机 ~/deployments/rss/data/state.json（挂载卷）

Reddit / HN / 通用 RSS（GitHub Actions）
   └> reddit.yml 每小时 :47 直连拉取（Reddit 匿名 RSS / HN Algolia / generic_feeds）
       └> 飞书卡片推送，state-reddit.json 提交回仓库持久化
```

- X 主通道是 **twitterapi.io advanced_search**：5 个账号 OR 成一条查询，
  配合 `since_id` 只取上次之后的新推文。游标存 state.json，不做启动回补
  （重启窗口期的漏推按需求接受）；推文正文不截断。
- Reddit 正文直接来自 RSS `<content>`（OAuth 备用通道读 selftext），
  不再逐帖请求匿名 .json 详情（该接口 2026-05 起对数据中心 IP 全面 403）。
- HN 外链帖附 Algolia 热评，自述帖（Ask HN 等）带 story_text。
- 飞书卡片发送前自动做整卡大小预算（`fit_card`），满载内容超限时逐块压缩，
  避免超限被拒后形成"失败->重试更大卡片"的死循环。
- `main.py --dry-run` 只打印卡片，不写去重状态，可反复试跑。
- 服务器容器设 `RSS_SOURCES=twitter`，Actions 设 `RSS_SOURCES=reddit,hn,rss`，
  同一份代码按环境变量各跑各的源，互不干扰。
- 首次运行某源只记录基线不推送，避免刷屏；旧版本镜像每次部署后自动清理。

## 通用 RSS 订阅

`config.yaml` 的 `generic_feeds` 可接入任何标准 RSS/Atom（无凭据）：
AI 官方博客、GitHub Releases（`github.com/{owner}/{repo}/releases.atom`）、
arXiv（`export.arxiv.org/rss/cs.CL`）、V2EX、YouTube 频道、
Google News 关键词等。卡片每源最多 `max_items_per_card` 条，突发大量更新时
积压会在后续小时逐步排空。取消注释并在 `RSS_SOURCES` 中包含 `rss` 即生效
（reddit.yml 已包含）。

## 配置

订阅列表、单卡条数上限等在 `config.yaml`；密钥全部走 GitHub Secrets，
由 compose 注入容器环境变量：

| 来源 | 名称 | 必填 | 说明 |
|---|---|---|---|
| 组织 vars | `ALIYUN_ACR_REGISTRY` / `ALIYUN_ACR_USERNAME` | 是 | ACR 地址与用户名 |
| 组织 secret | `ALIYUN_ACR_PASSWORD` | 是 | ACR 登录密码 |
| 本仓库 secret | `FEISHU_WEBHOOK` | 是 | 飞书群机器人 webhook |
| 本仓库 secret | `FEISHU_SECRET` | 否 | 飞书签名校验密钥 |
| 本仓库 secret | `AI_API_BASE` / `AI_API_KEY` | 否 | OpenAI 兼容接口，启用推文中文翻译与 AI 总结 |
| 本仓库 secret | `AI_MODEL` | 否 | 模型名，默认 `deepseek-v4-flash-0731` |
| 本仓库 secret | `REDDIT_PROXY` | 否 | Reddit 出站代理，如 `http://host.docker.internal:7890` |

X 订阅**当前已停用**（twitterapi.io 按查询计费）。恢复方式：把仓库变量
`ENABLE_X_PUSH` 设为 `true` 后手动 Run 一次 deploy workflow 即可——容器改为
空转模式零 API 调用，`since_id` 游标保留在挂载卷，恢复后自动补拉停机窗口。
恢复后的行为：5 个账号 OR 成一条查询，`since_id` 只取上次之后的新推文，
连续整轮失败自动发飞书告警。`main.py` 里保留的 syndication 免费拉取代码可
作为本地无 Key 环境的备用手段（注意其返回的是精选视图而非完整时间线，
仅适合临时用途）。

Reddit / HN / RSS 订阅跑在 GitHub Actions（`reddit.yml`），拉取或推送失败时
通过 `if: failure()` 步骤发飞书告警；`REDDIT_CLIENT_ID/SECRET` 为可选兜底凭据。

X 推文卡片默认带 **AI 总结**（整批内容概括），非中文推文自动附中文译文（保留原文）。
未配置 AI 或调用失败时自动降级为只推原文。

本地开发则复制 `.env.example` 为 `.env` 填入真实值。

## 本地测试

```bash
pip install -r requirements.txt
python main.py --dry-run   # 只打印将推送的卡片内容，不发送、不写状态
python main.py             # 正式推送
```

想看卡片效果：删掉 `state.json` 里某个源的几条 ID 再跑 dry-run
（dry-run 不落盘，可以反复试跑，不会吃掉待推内容）。

本地起完整容器：

```bash
docker build -t rss-push .
mkdir -p data
docker run -d --name rss-push --env-file .env -e TZ=Asia/Shanghai \
  -e RSS_DATA_DIR=/data -v "$(pwd)/data:/data" rss-push
docker logs -f rss-push
```

## 部署与运维

日常发布：改完代码 push 到 master 即可；也可在 Actions 页面手动 Run workflow。

服务器上运维（SSH 登录后）：

```bash
cd ~/deployments/rss
docker compose ps                  # 容器状态
docker logs -f rss-push            # 调度与每轮运行日志
tail -5 data/state.json            # 去重状态
docker compose pull && docker compose up -d   # 手动更新到 latest
```

修改订阅后不需要登服务器，改 `config.yaml` 推送即可。

## 飞书机器人配置步骤

1. 打开目标飞书群 -> 设置 -> 群机器人 -> 添加机器人 -> 自定义机器人。
2. 复制 **Webhook 地址** 填入 Secret `FEISHU_WEBHOOK`。
3. 安全设置建议选 **签名校验**，密钥填入 Secret `FEISHU_SECRET`
   （关键词校验需保证卡片含固定词；IP 白名单不适用云端出口）。

## Reddit 说明

Reddit 自 2025-11 起**关闭自服务 API 申请**（Responsible Builder Policy），个人无法再
创建 OAuth 应用；2026-05 起匿名 `.json` 接口也全面 403。因此本项目的 Reddit 订阅改为
**GitHub Actions 直连匿名 RSS**（Azure 出口 IP 实测可用），不再需要任何 Reddit 凭据，
也不依赖服务器代理。`REDDIT_PROXY` / OAuth 相关配置仅作为备用路径保留在代码里。

服务器运维（X 部分，SSH 登录后）：
