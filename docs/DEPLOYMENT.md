# QuantPilot 外部部署

## 监听与安全

QuantPilot 默认监听 `0.0.0.0:10000`。首次启动可直接访问初始化页面创建初始管理员，之后由管理员在“用户管理”创建账户。可信内网可直接使用 `HTTP + IP`；公网部署强烈建议由 Nginx/Caddy 终止 HTTPS，但程序不会强制 HTTPS。

## Debian/Ubuntu 一键安装

```bash
curl -fsSL https://raw.githubusercontent.com/Charles-0509/QuantPilot/main/scripts/install.sh | sudo bash
```

安装器支持自定义目录与端口、修复现有安装，以及选择保留或删除数据的卸载流程。管理配置保存在 `/etc/quantpilot/quan.conf`，命令安装到 `/usr/local/bin/quan`。

常用命令：

```bash
quan update       # 只检查版本
quan upgrade      # 在线升级镜像和 quan 命令
quan status
quan logs
quan restart
quan start
quan stop
```

生产服务器 `.env`：

```env
QUANTPILOT_HOST=0.0.0.0
QUANTPILOT_PORT=10000
QUANTPILOT_COOKIE_SECURE=true
QUANTPILOT_SESSION_HOURS=12
```

公开镜像支持 AMD64 与 ARM64：

```bash
docker pull ghcr.io/charles-0509/quantpilot:1.3.4
```

使用仓库中的 `docker-compose.yml` 时，执行 `docker compose pull && docker compose up -d` 即可拉取并运行 `latest`。需要从源码构建时执行 `docker build -t quantpilot:local .`。

## FRP 示例

目标服务器 `frpc.toml`：

```toml
serverAddr = "你的阿里云公网地址"
serverPort = 7000

[[proxies]]
name = "quantpilot"
type = "tcp"
localIP = "127.0.0.1"
localPort = 10000
remotePort = 11000
```

## Nginx 示例

阿里云 Nginx 将域名代理到 FRP 暴露的本机端口：

```nginx
server {
    listen 443 ssl http2;
    server_name quant.example.com;

    ssl_certificate /etc/letsencrypt/live/quant.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/quant.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:11000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600;
    }
}
```

HTTP 与 HTTPS 均可工作。不要在公网或不可信网络使用 HTTP 登录，否则用户名、密码和会话 Cookie 可能被窃听；HTTPS 部署建议设置 `QUANTPILOT_COOKIE_SECURE=true`。
