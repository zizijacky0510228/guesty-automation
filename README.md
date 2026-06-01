# Guesty Guest Messaging Automation

这个项目是 Guesty 客人消息长期自动化的安全起点。它默认只读和生成报告，不会自动发送消息。

## 为什么不用登录密码

Guesty 当前 Open API 使用 OAuth2：通过 `Client ID` 和 `Client Secret` 获取 24 小时左右有效的 Bearer token。长期自动化应该使用 API 凭证，而不是网页登录名和密码。

官方文档：

- [Guesty Authentication](https://open-api-docs.guesty.com/docs/authentication)
- [Get conversations](https://open-api-docs.guesty.com/reference/get_communication-conversations)
- [Get conversation posts](https://open-api-docs.guesty.com/reference/get_communication-conversations-conversationid-posts)
- [Send message](https://open-api-docs.guesty.com/reference/post_communication-conversations-conversationid-send-message)
- [Message webhooks](https://open-api-docs.guesty.com/docs/webhooks-messages)

## 本地配置

1. 在 Guesty 里用受限子账号创建 Open API 凭证。
2. 在本目录复制配置文件：

```bash
cp .env.example .env
```

3. 把 `GUESTY_CLIENT_ID` 和 `GUESTY_CLIENT_SECRET` 填进 `.env`。

`.env` 已经被 `.gitignore` 忽略，不会提交到 git。

## 使用

验证凭证：

```bash
python3 guesty_automation.py test-auth
```

生成未读/open 客人消息报告：

```bash
python3 guesty_automation.py inbox-report --out data/inbox_report.md
```

自动回复范围只处理 property nickname 包含以下 token 的物业：

```text
3505, 383, 2171, 6550, 2030, 5553
```

学习历史 host 回复，生成本地风格文件：

```bash
python3 guesty_automation.py learn-style --limit 100
```

检查最新客人消息，分成“可草拟”和“必须问业主”：

```bash
python3 guesty_automation.py review-new --out data/pending_review.md
```

发送消息默认关闭。发送命令会先尝试从同一个 conversation 的历史 posts 推断 Guesty message module；没有 `GUESTY_SEND_ENABLED=true` 和 `--confirm-send` 时只会 dry run。

## 推荐的自动化阶段

1. 只读报告：拉取未读消息，按紧急程度排序。
2. 草稿模式：根据房源规则和历史消息生成建议回复，等待你确认。
3. 半自动发送：普通消息直接回复，限制条件消息提醒业主确认。
4. Webhook 模式：Guesty 有新消息时触发，不靠轮询。

限制条件包括改期/延住/缩短住宿、退款/赔偿/付款纠纷、提前退房、缺失的门禁/房门密码、平台外预订、安全事故、以及历史规则里找不到答案的问题。

## 清洁任务云端自动化

清洁报告脚本在 `guesty_cleaning_report.py`。Render cron 只在 Vancouver 时间 20:00 附近唤醒脚本：

- 20:00：发送明天清洁任务，并保存基准快照。
- 10:30 对比任务暂时关闭，优先减少 Guesty API 调用并保证晚 8 点清洁报告稳定发送。

配置细节见 `CLEANING_SETUP.md`。
