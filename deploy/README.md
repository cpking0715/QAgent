# QAgent 内网部署

LLM Key 只放环境变量或挂载的 `qagent.local.yaml`，不要写进镜像。

## Docker

```bash
cd deploy
export OPENAI_API_KEY=sk-...
export QAGENT_TOKEN=请换成内网口令
docker compose up -d --build
```

浏览器打开 `http://服务器:8765/`。接口会校验 `Authorization: Bearer <QAGENT_TOKEN>`；页面遇到未授权时会提示输入口令。

飞书事件订阅地址：`https://你的域名/api/feishu/event`  
需配置 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_VERIFICATION_TOKEN`。

## Nginx

将 [`nginx.conf`](nginx.conf) 放到站点配置，反代到本机 `8765`。

## 环境变量

| 变量 | 作用 |
|------|------|
| `QAGENT_JOBS_DIR` | 任务目录，默认 `/data/qagent/jobs` |
| `QAGENT_TOKEN` | 共享访问口令；为空则不校验（仅开发） |
| `OPENAI_API_KEY` / `qagent.local.yaml` | LLM |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 飞书发消息、下文件 |
| `FEISHU_VERIFICATION_TOKEN` | 事件订阅校验 |
