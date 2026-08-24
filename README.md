# RSS 信息推流到飞书

定时拉取指定 X (Twitter) 账号的推文和 Reddit 版块的新帖，去重后以卡片消息推送到飞书群。

## 架构

```
git push master
   │
   ▼
GitHub Actions（deploy.yml）
   │  SSH 密钥登录服务器
   ▼
rsync 同步代码到 ~/deployments/rss
   │  venv 装依赖 + 更新 systemd 单元
   ▼
rss-push.timer 每小时触发 rss-push.service
   │
   ├── sources/twitter.py   twitterapi.io 拉用户时间线
   ├── sources/reddit.py    Reddit RSS 拉新帖，403/429 自动回退 OAuth
   └── feishu.py            组装卡片推送到飞书机器人

去重状态：state.json 存在服务器上（每个源保留最近 500 条 ID）
首次运行某源只记录基线不推送，避免刷屏。
```

部署方式沿用 `G:\project\template` 的思想并按本项目体量做了适配：
**Actions 决定发布哪一版 → 确定性同步到目标机器 → systemd 负责让这一版按时运行。**
周期脚本不涉及常驻服务和数据库，因此不引入 Docker/MySQL/Redis/镜像仓库。

## 配置

- 订阅列表、每卡片条数上限等：`config.yaml`
- 密钥全部放 GitHub Secrets，部署时自动生成服务器上的 `.env`：

| Secret | 必填 | 说明 |
|---|---|---|
| `DEPLOY_SSH_KEY` | 是 | 部署专用 SSH 私钥 |
| `SERVER_HOST` | 是 | 服务器地址 |
| `TWITTER_API_KEY` | 是 | twitterapi.io 的 Key |
| `FEISHU_WEBHOOK` | 是 | 飞书自定义机器人 webhook |
| `FEISHU_SECRET` | 否 | 飞书签名校验密钥 |

本地运行则复制 `.env.example` 为 `.env` 填入真实值。

## 本地测试

```bash
pip install -r requirements.txt
python main.py --dry-run   # 只打印将推送的卡片内容，不发送
python main.py             # 正式推送
```

想看卡片效果：删掉 `state.json` 里某个源的几条 ID 再跑 dry-run。

## 部署与运维

日常发布：改完代码 push 到 master 即可，Actions 会自动同步并保持定时器运行。

手动触发部署：GitHub 仓库 Actions 页面 -> Deploy -> Run workflow。

服务器上的常用操作（SSH 登录后）：

```bash
systemctl list-timers rss-push.timer     # 看下次执行时间
systemctl start rss-push.service         # 立即执行一次
journalctl -u rss-push.service -n 50     # 最近一次运行日志
tail -5 ~/deployments/rss/state.json     # 查看去重状态
```

修改订阅后不需要登服务器，改 `config.yaml` 推送即可。

## 飞书机器人配置步骤

1. 打开目标飞书群 -> 设置 -> 群机器人 -> 添加机器人 -> 自定义机器人。
2. 复制 **Webhook 地址** 填入 Secret `FEISHU_WEBHOOK`。
3. 安全设置建议选 **签名校验**，密钥填入 Secret `FEISHU_SECRET`；
   （关键词校验需保证卡片含固定词；IP 白名单不适用）。

## Reddit OAuth 凭据（建议配置）

GitHub Actions 与服务器的机房 IP 都可能被 Reddit 匿名接口拦截（403/429），
代码检测到后会自动切换 OAuth 拉取，需要一对免费凭据：

1. 登录 Reddit 后打开 <https://www.reddit.com/prefs/apps> -> create another app...
2. 类型选 **script**，redirect uri 填 `http://localhost:8080`。
3. 应用名下方的字符串是 client id，另一串是 secret。
4. 在 `.env` 或 GitHub Secrets 里加 `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET`
   （若走 Secrets，还需在 deploy.yml 的"生成 .env"步骤里补两行 echo）。
