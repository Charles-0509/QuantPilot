# QuantPilot — Alpaca 模拟盘量化交易平台

QuantPilot 是一个完全本地运行的美股/ETF量化交易程序。它提供中文 Web UI、条件卡片规则编辑器、历史回测、全局风险控制和 Alpaca Paper Trading 自动执行。

> 安全边界：程序中的 `TradingClient` 和 `TradingStream` 永久使用 `paper=True`，前端、API 和配置均不存在实盘切换入口。

## 功能

- 深色未来科技风格中文控制台。
- 5分钟、15分钟、30分钟、1小时和日线策略。
- 递归 AND/OR/NOT 条件树与 JSON 版本管理。
- SMA、EMA、RSI、MACD、布林带、ATR、ROC、最高/最低价、成交量均线和偏离率。
- SMA趋势、RSI均值回归、布林带、MACD、Donchian、量价突破和定投模板。
- GOOGL 专用研究模板：日线趋势突破、15分钟趋势回调、15分钟量价突破与日线布林带回归；默认禁用，需复制、回测后再启用。
- 下一根K线成交的事件式回测，支持滑点、限价、止损止盈和保守的同K线成交顺序。
- Alpaca IEX 行情、Paper 账户、持仓、订单和成交状态同步。
- 单股票仓位、总暴露、最大持仓数、单日亏损和日内回撤熔断。
- SQLite 持久化、Alembic 基线迁移、信号幂等和订单对账。

## 启动

1. 首次使用时复制环境文件（不需要在其中填写密钥）：

```bash
cp .env.example .env
```

2. 启动：

```bash
docker compose up --build
```

3. 打开 [http://localhost:10000](http://localhost:10000)。首次启动先创建唯一管理员；登录后，在 Alpaca Dashboard 切换到 **Paper Account**，进入“设置”填写并验证 API Key 与 Secret。网页配置会以密文保存到本机 SQLite 数据库；解密密钥仅保存在同一台机器的 `data/.credentials.key`，请保护 Docker 的 `data/` 目录。

4. `.env` 仍可作为无界面部署的后备配置：

```env
APCA_API_KEY_ID=你的模拟盘Key
APCA_API_SECRET_KEY=你的模拟盘Secret
ALPACA_DATA_FEED=iex
```

Docker 默认将 `10000` 端口发布到 `0.0.0.0`，可供局域网、FRP 或反向代理访问。生产环境必须使用 HTTPS，并在 `.env` 中设置 `QUANTPILOT_COOKIE_SECURE=true`。网页中更新 Alpaca 配置后，交易引擎会进入安全暂停状态，需在“自动交易”页面确认后重新启动。网页配置优先于 `.env`；移除网页配置后会自动回退到 `.env`。

也可以直接拉取 AMD64/ARM64 公共镜像：`ghcr.io/charles-0509/quantpilot:1.1.0` 或 `ghcr.io/charles-0509/quantpilot:latest`。

## 推荐使用顺序

1. 从策略库复制模板。
2. 在规则编辑器中修改股票池、条件、仓位与风控。
3. 在回测实验室使用至少一个完整市场阶段验证。
4. 在策略库启用策略。
5. 在自动交易页启动引擎。
6. 观察一段时间模拟盘行为，再决定是否继续改进规则。

## 开发与测试

前端：

```bash
cd frontend
npm install
npm run build
npm test
```

后端测试建议在 Python 3.12 容器中执行：

```bash
docker compose run --rm quantpilot pytest -q
```

登录后的 API 文档位于 [http://localhost:10000/docs](http://localhost:10000/docs)。FRP、Nginx 与 HTTPS 配置参见 [部署说明](docs/DEPLOYMENT.md)。

## 数据与模拟限制

- 免费 IEX 并不代表全美交易所 SIP 全市场成交量。
- 免费实时订阅最多使用30个股票代码；应用在启用策略时会检查。
- Alpaca Paper 不完整模拟市场冲击、订单排队、真实延迟滑点、费用和分红。
- 本程序不是投资建议，历史回测和模拟盘收益不代表未来表现。

详细资料参见 [架构说明](docs/ARCHITECTURE.md) 与 [规则格式](docs/RULES.md)。
