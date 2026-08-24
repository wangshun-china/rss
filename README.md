# RSS 信息推流到飞书

定时拉取指定 X (Twitter) 账号的推文和 Reddit 版块的新帖，去重后以卡片消息推送到飞书群。

## 架构

Reddit 在中国大陆被整站阻断、且 Reddit 已关闭自服务 API 申请（Responsible Builder Policy），
而 GitHub Actions 的出口 IP 可正常匿名访问 Reddit RSS。因此采用双通道分工：

```
X 订阅（服务器容器）
git push master
   └> Actions 构建 SHA 镜像双推 GHCR+ACR
       └> aliyun Runner compose 启动 rss-push 容器
           └> 每整点 :17 拉 twitterapi.io -> AI 翻译总结 -> 飞书卡片
           去重状态：宿主机 ~/deployments/rss/data/state.json（挂载卷）

Reddit 订阅（GitHub Actions）
   └> reddit.yml 每小时 :47 匿名直连 RSS
       └> 飞书卡片推送，state-reddit.json 提交回仓库持久化
```

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
| 本仓库 secret | `TWITTER_API_KEY` | 是 | twitterapi.io 的 Key |
| 本仓库 secret | `FEISHU_WEBHOOK` | 是 | 飞书群机器人 webhook |
| 本仓库 secret | `FEISHU_SECRET` | 否 | 飞书签名校验密钥 |
| 本仓库 secret | `AI_API_BASE` / `AI_API_KEY` | 否 | OpenAI 兼容接口，启用推文中文翻译与 AI 总结 |
| 本仓库 secret | `AI_MODEL` | 否 | 模型名，默认 `deepseek-v4-flash-0731` |
| 本仓库 secret | `REDDIT_PROXY` | 否 | Reddit 出站代理，如 `http://host.docker.internal:7890` |

X 订阅主通道是 **syndication 嵌入接口**（官方给网页组件用的公开端点，无需任何认证），
twitterapi.io 仅作为备用（配置了 Key 且 syndication 失败时才走）。
国内服务器访问该接口需经代理：容器默认 `TWITTER_PROXY=http://host.docker.internal:7890`
（即宿主机 sing-box mixed 端口），可在 Secrets 覆盖。

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
