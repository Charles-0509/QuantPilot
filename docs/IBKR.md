# IBKR 接入与部署

本源码版本 1.6.0 增加 IBKR Client Portal Gateway REST 接入。现有 Alpaca Paper 配置保留，每位用户选择一个当前券商。当前开发验收采用模拟接口；2026-10-23 已在服务器部署 Gateway，并通过实际后端核验Paper账户、USD资金字段、持仓/订单、八标的合约及AAPL历史K线。当前AAPL实时快照为DPB延迟行情，尚待确认实时行情权限；**没有提交IBKR订单或验证Paper成交**。当前入口和运维记录见 [服务器部署记录](research/ibkr-gateway-deployment-2026-10-23.md)。

## 支持范围

| 能力 | Alpaca | IBKR 第一版 |
| --- | --- | --- |
| 环境 | 仅 Paper | 默认 Paper，真实账户须单次风险确认 |
| 账户 | 用户独立密钥 | 用户独立网关与账户；禁止跨用户共用当前网关或账户 |
| 产品 | 美股/ETF | USD 基础币种账户的美元股票/ETF，多头 |
| 订单 | 市价、限价、Bracket、移动止损 | 市价、限价、Bracket；移动止损策略禁止启用 |
| 更新 | WebSocket 和 REST | REST 订单轮询约5秒，K线按引擎轮次获取 |
| 现金归集 | 按现有设置 | 关闭且禁止启用 |
| 断线恢复 | 客户端订单号对账 | cOID 对账；已尝试提交但查不到原单时保持待对账，不重发 |

IBKR 自动订单要求实时买卖报价。延迟、冻结或缺失行情均拒绝自动下单。IBKR 历史数据复权与成交量口径不能假定与 Alpaca IEX 一致，回测导出记录券商和数据口径。IBKR 结果不明的订单不会因为近期订单列表为空而自动重发。额外券商订单警告默认拒绝，未自动接受未知风险提示。

## 1. 安装并登录 Gateway

按 [IBKR 官方安装说明](https://www.interactivebrokers.com/docs/web-api/authentication/cpgw/installation-authentication) 检查账户/API资格、安装 Java（先执行 `java -version`），下载并解压官方 Client Portal Gateway。Linux/macOS 在解压目录执行：

```bash
bin/run.sh root/conf.yaml
```

Windows 使用官方说明的 `bin\run.bat root\conf.yaml`。在运行 Gateway 的机器上打开 `https://localhost:5000` 并完成登录和双重认证。默认测试请使用独立的 **Paper 用户名**，不是将真实账户凭据当成模拟凭据；见 [Paper 登录说明](https://www.interactivebrokers.com/docs/web-api/authentication/paper)。不要向 QuantPilot 提交 IBKR 密码、验证码或恢复代码。

官方要求浏览器登录及 API 请求与 Gateway 在同一机器，见 [Gateway 限制](https://www.interactivebrokers.com/docs/web-api/authentication/cpgw/limitations-of-the-client-portal-gateway)。服务器部署应将 Gateway 与 QuantPilot 后端放在同一主机，并在该主机浏览器完成认证（远程桌面亦在服务器运行浏览器）。不将 Gateway 直接暴露到公网。远程 Gateway 地址虽然可以配置，是否符合账户认证和网络要求仍需现场验证。

Gateway 会话需要定期人工重新登录。程序每分钟保活不能替代登录或绕过双重认证；不要据此期待永久无人值守。

## 2. Docker 访问本机 Gateway

普通桥接容器内的 `127.0.0.1` 指向容器本身。Linux 上可在原部署文件基础上将 QuantPilot 服务设置为 `network_mode: host`，同时移除该服务的 `ports`，保持原镜像、`.env`、数据卷和重启策略。例如：

```yaml
services:
  quantpilot:
    image: quantpilot:你的已验证版本
    container_name: quantpilot
    network_mode: host
    env_file:
      - .env
    volumes:
      - ./data:/app/data
    restart: unless-stopped
```

服务依然使用 `QUANTPILOT_PORT`（默认10000）。先检查端口是否占用，再执行 `docker compose config` 校验。2026-10-23当前服务器已应用主机网络，后端Gateway地址为`https://localhost:5050/v1/api`，当前券商仍为Alpaca Paper。Gateway保持独立网络并仅发布127.0.0.1:5050。Docker Desktop 的主机网络行为不同，建议先在 Gateway 同机原生运行后端进行接入验证。

当前服务器将镜像原certifi公共根证书与Gateway的唯一localhost证书合并为`/opt/quantpilot/config/ibkr-trust-bundle.pem`，只读挂载到`/usr/local/lib/python3.12/site-packages/certifi/cacert.pem`，已在实际后端验证TLS开启时可连接。更换基础镜像后需按新镜像的原CA集合重新生成组合文件。

生产推荐使用可信网关证书。页面的“验证网关 TLS 证书”默认开启；如果使用官方本机自签名证书且尚未配置信任，可由用户对可信本机连接显式关闭。切勿把关闭验证当成公开远程网关的部署方案。

新连接的预填地址由服务器环境变量`IBKR_GATEWAY_URL`提供；当前部署设置为`https://localhost:5050/v1/api`。修改环境变量后需重启后端。此默认值不覆盖用户已保存的网关地址，也不会改变当前券商、账户或实盘授权。

## 3. 在设置页连接 Paper

1. 完成现有策略/订单的对账并暂停引擎。未结策略持仓、订单、待对账意图或活动隔离会阻止更换连接。
2. 选择IBKR，使用页面预填的服务器网关地址；本次服务器为`https://localhost:5050/v1/api`，保留真实账户开关关闭。官方未修改端口的Gateway才使用5000。
3. 点击“读取网关账户”，选择 Paper 账户，保存。环境由 IBKR 的 `isPaper` 明确核验，不依赖 DU 前缀、端口或用户手工标签。真实登录不能冒充 Paper。
4. 检查账户币种、资金、持仓、实时行情和订单列表；网关当前选中账户须与配置一致。
5. 保存连接后引擎保持暂停、现金归集关闭。检查该券商支持的策略与风险设置，再由用户启动。

可先保存尚无账户ID的 IBKR Paper 连接，用于准备设置；此状态不可下单。切换券商不会删除原 Alpaca 密钥。切回 Alpaca 仍固定使用 Paper。

## 4. 真实账户确认机制

勾选真实账户开关时，开关保持关闭，先弹出目标账户、风险说明和免责声明。用户必须准确输入 **我同意并确认**，服务端验证成功后才开启。

后端采用5分钟一次性挑战，绑定用户、券商、账户ID、网关地址、TLS设置、配置修订号与免责声明版本。直接调用保存API、空输入、近似短语、过期/已用挑战、其他用户或其他账户的挑战均不能启用真实账户。切回任何模拟环境或其他券商会撤销旧授权；返回原真实账户也必须再次弹窗并重新输入。任何配置修改也需重新确认。保存后引擎继续暂停。

本任务实现这一功能，不授权助手打开真实交易或提交真实订单。当前部署和测试均保持 Alpaca Paper；没有进行真实账户启用。

## 5. 升级与回退

正式公开版本发布前应使用本源码构建或已验证的本地镜像，旧公共 `latest` 不保证包含 IBKR 功能。升级前停止自动执行、对账并备份完整数据目录（包括 SQLite/WAL、`.credentials.key`）、`.env` 和 Compose 文件；使用 SQLite online backup 或停服一致性备份，勿单独复制活动中的 DB 主文件。记录原镜像ID，先在隔离数据库执行迁移、Python3.12测试和前端构建，再替换服务。

迁移 `0009_broker_settings` 添加用户券商配置及一次性风险确认表。原用户没有券商记录时继续使用原 Alpaca Paper。回退时保持引擎暂停，恢复同一份一致性数据库与配置备份、使用已记录旧镜像；先确认没有未结新订单，避免回退到旧账本后重复提交。

## 6. 实际接入验收与剩余限制

本次只读验收已通过Paper标识、USD资金字段、持仓/订单、八标的合约唯一性和AAPL历史K线。当前AAPL快照6509为DPB（延迟）且无买卖价，平台继续拒绝自动订单。请在官网User menu → Settings → Account Configuration → Paper Trading Account确认实时行情共享/权限，见[官方指南](https://www.ibkrguides.com/clientportal/papertradingaccount.htm)。不会自动购买订阅或切换当前Alpaca券商。

2026-10-23 15:12复查时，用户已自行选择IBKR Paper并运行引擎，该状态在默认地址修复部署中保留。网关仍需按提示在服务器浏览器人工重新登录。正式启用前仍须确认实时行情、节假日/提前收市及服务器时间。只有另行确认具体限额和截止时间后，才进行小仓位 Paper 买卖、Bracket撤单、部分成交、会话失效与超时恢复验证。本轮八标的授权针对 Alpaca Paper，不自动扩展到 IBKR 交易。

接口依据与官方字段说明见 [研究记录](research/ibkr-api-research-2026-10-22.md)。


## 7. 已开启共享但API仍无报价

2026-10-23用户确认共享开关已打开后，15:16复查仍缺少行情标记和买卖报价，账户认证正常。请核对共享来源用户名的现有美股Level1/API订阅，以及设置→市场数据订阅中的Market Data API Acknowledgement是否为Enabled。共享现有订阅与取得API行情授权是不同验收条件，不能仅凭开关已打开判定完成。

[API确认协议官方说明](https://www.interactivebrokers.com/docs/general/market-data-subscriptions/compliance-requirements-for-api-market-data/market-data-api-acknowledgement)列出启用、本人签署以及退出后重新登录以刷新权限的步骤；如已启用则无需重复签署。平台内免费可见的报价不一定覆盖API，见[官方授权区别](https://www.interactivebrokers.com/docs/general/market-data-subscriptions/tws-data-vs-api-data)。本程序不会替用户购买行情订阅或签署账户协议。


2026-10-23 15:30更新：共享、API协议签署和重新登录均已完成，八标的API开始返回最新价，但6509均为DPB，买卖报价为空，仍属于延迟行情。下一步仅核对共享来源用户名的现有美股行情订阅及API授权范围，无需重复签署或登录。后端证据见[重新登录后复核](research/ibkr-api-ack-relogin-verified-2026-10-23.json)。


当前订阅清单及单交易所探测已完成，REST SMART为DPB，AAPL@IEX/BATS的WebSocket也为DB，均是延迟数据。相关NASDAQ Network C与ARCA Network B覆盖映射、账户侧限制和可验证结论见[行情授权报告](research/ibkr-market-data-entitlement-2026-10-23.md)。
