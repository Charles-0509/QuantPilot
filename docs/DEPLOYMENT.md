# QuantPilot 外部部署

## 监听与安全

QuantPilot 默认监听 `0.0.0.0:10000`。首次启动可直接访问初始化页面创建唯一管理员。生产环境建议只允许 FRP 或可信内网访问目标服务器的10000端口，公网流量由阿里云 Nginx 终止 HTTPS。

生产服务器 `.env`：

```env
QUANTPILOT_HOST=0.0.0.0
QUANTPILOT_PORT=10000
QUANTPILOT_COOKIE_SECURE=true
QUANTPILOT_SESSION_HOURS=12
```

公开镜像支持 AMD64 与 ARM64：

```bash
docker pull ghcr.io/charles-0509/quantpilot:1.1.0
```

使用仓库中的 `docker-compose.yml` 时，执行 `docker compose pull && docker compose up -d` 即可拉取并运行 `latest`；需要从源码重新构建时仍可使用 `docker compose up --build`。

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

不要直接在公网使用 HTTP 登录；否则用户名、密码和会话 Cookie 可能被窃听。
