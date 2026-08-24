# RSS 信息推流到飞书

定时拉取指定 X (Twitter) 账号的推文和 Reddit 版块的新帖，去重后以卡片消息推送到飞书群。

## 架构

```
git push master
   │
   ▼
GitHub Actions: build-and-push（ubuntu-latest）
   │  构建唯一 SHA 镜像，双推
   ├── ghcr.io/<user>/rss:<sha>
   └── 阿里云 ACR wangshun_build/rss:<sha>
   │
   ▼
服务器 Self-hosted Runner（label: aliyun）
   │  优先拉 GHCR，失败回退 ACR（同一 SHA）
   ▼
docker compose 启动 rss-push 常驻容器
   │  容器内调度器：启动即跑一轮，之后每个整点 :17 执行
   ▼
twitterapi.io 拉 X 时间线 + Reddit RSS 拉新帖 -> 飞书卡片推送
```

部署链路沿用 `G:\project\template`：**Actions 决定发布哪一版（SHA 镜像），
镜像仓库保存这一版（GHCR + ACR 双源），Docker Compose 让这一版运行。**

- 去重状态 `state.json` 挂载在宿主机 `~/deployments/rss/data/`，容器重建不丢
- 首次运行某源只记录基线不推送，避免刷屏
- 旧版本镜像在每次部署后自动清理，只保留当前运行版本

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

## Reddit 说明（重要）

Reddit 在中国大陆被整站阻断，阿里云服务器直连不可达，**配 OAuth 凭据也无法绕过网络阻断**。
当前可选方案：

1. **走服务器本地代理**：确保代理进程可用后，在仓库 Secrets 加
   `REDDIT_PROXY=http://host.docker.internal:7890`（端口按实际改），重新部署即可。
   代码已内置支持，未设置时自动直连。
2. 服务器上已有 sing-box（7890 端口），若节点修复则方案 1 即刻生效。

另外，若 Reddit 返回 403/429（常见于数据中心 IP 的匿名访问），代码会尝试回退到
OAuth API 拉取：在 <https://www.reddit.com/prefs/apps> 创建 script 应用，
把 `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` 加进 Secrets 并在 deploy.yml 的
deploy env 与 compose.yaml environment 里补上两个变量。
