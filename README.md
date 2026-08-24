# RSS 信息推流到飞书

定时拉取指定 X (Twitter) 账号的推文和 Reddit 版块的新帖，去重后以卡片消息推送到飞书群。

## 架构

X 官方免费通道（syndication）实测返回的是排名筛选后的精选视图而非完整时间线，
会漏推文；Reddit 则关闭了自助 API 申请。因此采用三通道分工，各用各的长处：

```
X 订阅（服务器容器，实时流式）
git push master
   └> Actions 构建 SHA 镜像双推 GHCR+ACR
       └> aliyun Runner compose 启动 rss-push 常驻容器
           └> WebSocket 连接 twitterapi.io 流式接口
               新推文秒级到达 -> 缓冲聚合(2分钟/8条) -> AI 翻译总结 -> 飞书卡片
           去重状态：宿主机 ~/deployments/rss/data/state.json（挂载卷）
           启动时 REST 回补一次停机缺口；断线自动重连

Reddit 订阅（GitHub Actions）
   └> reddit.yml 每小时 :47 匿名直连 RSS
       └> 飞书卡片推送，state-reddit.json 提交回仓库持久化
```

- X 流式计费按实际收到的新推文（15 credits/条），五账号日更百条级 ≈ $0.02/天，
  远低于轮询式；twitterapi.io 的 syndication 备用拉取代码保留在 main.py 本地可用。
- 服务器容器设 `RSS_SOURCES=twitter`，Actions 设 `RSS_SOURCES=reddit`，
  同一份代码按环境变量各跑各的源，互不干扰。
- 首次运行某源只记录基线不推送，避免刷屏；旧版本镜像每次部署后自动清理。

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

X 订阅主通道是 **twitterapi.io WebSocket 流式接口**：常驻连接实时接收五个账号的新推文，
按实际收到条数计费（15 credits/条 ≈ $0.15/千条）。服务端过滤规则由容器启动时自动
创建/更新/激活（tag: rss-push-x），断线自动重连，启动时 REST 回补一次停机缺口。
`main.py` 里保留的 syndication 免费拉取代码可作为本地无 Key 环境的备用手段
（注意其返回的是精选视图而非完整时间线，仅适合临时用途）。

X 推文卡片默认带 **AI 总结**（整批内容概括），非中文推文自动附中文译文（保留原文）。
未配置 AI 或调用失败时自动降级为只推原文。

本地开发则复制 `.env.example` 为 `.env` 填入真实值。

## 本地测试

```bash
pip install -r requirements.txt
python main.py --dry-run   # 只打印将推送的卡片内容，不发送
python main.py             # 正式推送
```

想看卡片效果：删掉 `state.json` 里某个源的几条 ID 再跑 dry-run。

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
