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

已有部署升级到最新稳定版：

```bash
quan update
quan upgrade
quan status
```

`quan upgrade` 会保留 `.env`、`data/`、管理员、用户凭据、策略和交易记录，拉取新镜像并在健康检查成功后更新 `/usr/local/bin/quan`。升级完成后会自动清理旧的 QuantPilot 镜像，不影响其他项目的 Docker 镜像。

生产服务器 `.env`：

```env
QUANTPILOT_HOST=0.0.0.0
QUANTPILOT_PORT=10000
QUANTPILOT_COOKIE_SECURE=true
QUANTPILOT_SESSION_HOURS=12
```

## Alpaca 连接可靠性参数

1.4.0 起，Alpaca 交易接口与行情接口使用独立的锁、超时和断路器。可安全重试的读取请求在 TLS 断流、连接超时、HTTP 429 或 5xx 时进行最多3次指数退避重试；普通4xx不会重试。连续失败达到阈值后只暂停故障通道，冷却期结束后自动进行半开探测，成功即恢复。订单提交不会直接重复 POST，而是通过确定性的 `client_order_id` 查询并恢复可能已经被 Alpaca 接受的订单。

以下变量均为可选项，示例值也是程序默认值：

```env
# TCP/TLS 建连超时；交易读取使用较短超时，历史行情允许更长响应时间。
ALPACA_CONNECT_TIMEOUT_SECONDS=5
ALPACA_TRADING_READ_TIMEOUT_SECONDS=6
ALPACA_DATA_READ_TIMEOUT_SECONDS=45

# 单次可安全读取的总尝试次数和指数退避范围。
ALPACA_RETRY_ATTEMPTS=3
ALPACA_RETRY_BASE_SECONDS=0.5
ALPACA_RETRY_MAX_SECONDS=4

# 连续失败达到3次后断路，默认30秒后允许一次恢复探测。
ALPACA_CIRCUIT_FAILURE_THRESHOLD=3
ALPACA_CIRCUIT_RECOVERY_SECONDS=30

# 降低账户、资产和近期K线的重复请求；设为0可关闭对应短缓存。
ALPACA_READ_CACHE_SECONDS=5
ALPACA_ASSET_CACHE_SECONDS=300
ALPACA_RECENT_BARS_CACHE_SECONDS=10
ALPACA_DAILY_BARS_CACHE_SECONDS=900

# WebSocket 数据流持续断线时的指数重连下限和上限。
ALPACA_STREAM_RETRY_BASE_SECONDS=5
ALPACA_STREAM_RETRY_MAX_SECONDS=300
```

日线会由30分钟常规时段K线聚合，以排除盘前盘后成交量；1小时和日线都读取 Alpaca 官方交易日历，并按每个交易日的实际收市时间聚合，包括美东时间13:00提前收市。日线行情独立缓存并增量刷新。除非正在诊断特定网络问题，建议保留默认值。特别是不要为了掩盖链路不稳定而设置无限超时或过大的重试次数，否则会延迟策略轮次和故障反馈。

Web UI 中的连接状态含义：

- `unconfigured`：该用户尚未配置 Alpaca Paper 凭据。
- `unknown`：已配置但进程尚未获得一次探测结果。
- `connected`：最近访问成功，可接受新订单。
- `degraded`：链路异常但尚未或无需进入断路保护，系统正在自动重试。
- `circuit_open`：连续失败后进入保护期，等待自动半开探测。

引擎的用户意图和运行能力分开显示：`status=running` 只表示用户开启了引擎；`operational_status=active` 且 `accepting_new_orders=true` 才表示当前可以创建订单。故障期间策略会安全跳过，不会把不完整行情标记为已评估；连接恢复后会自动继续。Dashboard 单项读取失败时显示“未知/暂不可用”，不会把故障误报为零余额、没有持仓、没有订单或美股休市。

`/api/health` 只表示 QuantPilot 应用和容器本身正常，不把 Alpaca 上游状态纳入 Docker 健康检查。请通过 Web UI 的连接状态、`/api/connection` 或 `quan logs` 判断上游是否降级，避免因为 Alpaca 网络波动触发容器重启循环。

## 执行安全隔离与升级约束

1.4.0 的数据库迁移头为 `0006_execution_incidents`。容器启动时会自动执行 Alembic 升级并新增持久化执行事件表，原有管理员、用户、凭据、策略、订单和回测记录不会被删除。

QuantPilot 会持续核对 Alpaca 实际持仓、开放卖单与本地策略持仓归属。出现下列任一情况时，系统会暂停该用户的引擎、阻止新订单、取消 QuantPilot 自有相关订单并进入执行安全隔离：

- Alpaca 端发生无法归属到 QuantPilot 订单的手工卖出。
- 策略归属数量大于 Alpaca 实际持仓。
- 开放卖单总量可能超过实际多头持仓。
- Alpaca 账户出现空头仓位。

隔离期间不要反复点击恢复。系统会用新的 Alpaca REST 快照确认没有开放卖单且不存在空头后，将事件标记为已控制；用户仍应先核对 Alpaca Paper 持仓和订单，再手工恢复引擎。系统不会为修复不一致而自动追买或猜测应归属哪个策略。

为了避免把旧账户的订单和持仓误归到新账户，只要存在策略归属持仓、未结 QuantPilot 订单、待对账订单意图或活动隔离，设置页就会拒绝更换 Alpaca API Key/Secret 并返回409。应先在原 Paper 账户完成订单和持仓处置，再切换凭据；当前版本不支持带仓更换同一 Paper 账户的新 Key。

公开镜像支持 AMD64 与 ARM64：

```bash
docker pull ghcr.io/charles-0509/quantpilot:1.4.0
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
