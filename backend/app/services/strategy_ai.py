"""Natural-language strategy drafting through any OpenAI-compatible chat API.

The model only proposes a RuleDefinition; the server validates it and returns
an unsaved draft. Nothing here saves, enables or trades.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
from pydantic import ValidationError

from app.schemas import RuleDefinition
from app.templates import TEMPLATES

MAX_REPAIRS = 2
INDICATORS = {
    "SMA": "简单均线 params.period；field=value",
    "EMA": "指数均线 params.period；field=value",
    "RSI": "相对强弱 params.period（0–100）",
    "MACD": "params.fast/slow/signal；field=macd|signal|histogram",
    "BOLLINGER": "布林带 params.period/std；field=upper|middle|lower",
    "ATR": "平均真实波幅 params.period（价格单位）",
    "ROC": "变化率 params.period（百分比）",
    "HIGHEST": "过去N根最高收盘 params.period, exclude_current=true",
    "LOWEST": "过去N根最低收盘 params.period, exclude_current=true",
    "VOLUME_SMA": "成交量均线×倍数 params.period/multiplier，与 price.volume 比较",
    "DEVIATION": "收盘相对SMA偏离百分比 params.period",
}


class StrategyAIError(RuntimeError):
    pass


def system_prompt() -> str:
    example = {key: TEMPLATES[key] for key in ("rsi_reversion", "donchian_breakout")}
    return (
        "你把用户的中文交易想法转换成 QuantPilot 的 RuleDefinition JSON（美股/ETF，"
        "在已完成K线上计算，信号在下一可交易时点执行）。\n"
        "只输出一个 JSON 对象：{\"definition\": RuleDefinition, \"explanation\": 中文逐条解释规则, "
        "\"assumptions\": [用户没说清而你替他做的假设]}。\n"
        "规则：\n"
        "- version 固定 1；timeframe 只能是 5Min/15Min/30Min/1Hour/1Day；weekdays 0–4；不要输出 paper_validation。\n"
        "- 条件树 entry/exit 为 {type:'group', op:'AND'|'OR', negate, children:[...]}；条件为 "
        "{type:'condition', left, operator, right, label}，operator 只能是 > >= < <= == crosses_above crosses_below。\n"
        "- 操作数：{kind:'price', field:open|high|low|close|volume, offset}、{kind:'number', value}、"
        "{kind:'indicator', indicator, params, field, offset}。可用指标：" + json.dumps(INDICATORS, ensure_ascii=False) + "\n"
        "- position.mode：percent_equity|fixed_notional|fixed_qty|risk_based；用户未指定时用 percent_equity 5。\n"
        "- order：止损和止盈要么同时设置（bracket，mode percent|atr），要么都为 null；移动止损 trailing_stop 不能与 bracket 同时使用。"
        "用户未提风控时设置 2% 止损、4% 止盈，并写进 assumptions。\n"
        "- warmup_bars ≥ 最长指标周期的 3 倍且 ≥30；label 用简短中文。\n"
        "- 方向与品种：默认 direction='long'、instrument='stock'、option=null。用户明确要做空股票时 direction='short'"
        "（entry 为开空条件、exit 为平空条件；止损在上方、止盈在下方；不能用移动止损；只做易借券股票、整股）。"
        "用户要交易期权时 instrument='option'、direction='long'，option={right:'call'|'put', min_dte, max_dte, "
        "strike_mode:'atm'|'otm_pct'|'itm_pct', strike_offset_pct, min_open_interest, max_spread_pct, stop_loss_pct, "
        "take_profit_pct, close_dte}；只买入开仓（看涨用 call，看跌用 put，不卖出开仓）；条件在标的K线上计算；"
        "order.stop_loss/take_profit/trailing_stop 必须为 null，order.time_in_force='day'，position.mode 不能是 risk_based；"
        "close_dte 必须小于 min_dte；用户未说明时用 min_dte 14、max_dte 45、atm、stop_loss_pct 50、take_profit_pct 100、close_dte 1。\n"
        "- 无法用现有指标表达的部分，用最接近的近似并在 assumptions 中说明，不要编造字段。\n"
        "示例定义：" + json.dumps(example, ensure_ascii=False)
    )


def _extract_json(text: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("模型没有返回 JSON")
    return json.loads(match.group(0))


def review_warnings(definition: RuleDefinition) -> list[str]:
    warnings = []
    if definition.is_option:
        if not definition.option.stop_loss_pct:
            warnings.append("期权没有设置权利金止损，最多可能亏掉全部权利金。")
        warnings.append("期权回测使用免费 indicative 数据（非交易所实际报价），只能从 2024 年 2 月开始。")
    elif not (definition.order.stop_loss or definition.order.trailing_stop):
        warnings.append("没有设置止损，单笔亏损没有上限。")
    if definition.is_short:
        warnings.append("做空理论上亏损没有上限，股价跳空上涨时止损可能以更差价格成交。")
    if definition.timeframe == "5Min":
        warnings.append("5分钟线交易频繁，免费 IEX 行情成交量偏少，信号可能失真。")
    if definition.position.mode == "percent_equity" and definition.position.value > 20:
        warnings.append(f"单笔仓位 {definition.position.value}% 偏高。")
    warnings.append("这是草稿：请先回测，再保存并决定是否启用。")
    return warnings


PROVIDERS = {"openai", "openai_responses", "anthropic"}
SAMPLING_KEYS = {"temperature", "max_tokens", "max_output_tokens"}


def _endpoint(provider: str, base_url: str) -> str:
    base = base_url.rstrip("/")
    if provider == "anthropic":
        return base + ("/messages" if base.endswith("/v1") else "/v1/messages")
    if provider == "openai_responses":
        return base + "/responses"
    return base + "/chat/completions"


async def complete(client: httpx.AsyncClient, *, provider: str, base_url: str, model: str, api_key: str,
                   messages: list[dict[str, str]], max_tokens: int = 4096) -> str:
    """One chat turn against an OpenAI-compatible or Anthropic Messages endpoint."""
    if provider not in PROVIDERS:
        raise StrategyAIError(f"不支持的接口类型：{provider}")
    system = "\n".join(m["content"] for m in messages if m["role"] == "system")
    turns = [m for m in messages if m["role"] != "system"]
    if provider == "anthropic":
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        body: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "temperature": 0.2,
                                "system": system, "messages": turns}
    elif provider == "openai_responses":
        headers = {"Authorization": f"Bearer {api_key}"}
        body = {"model": model, "temperature": 0.2, "max_output_tokens": max_tokens, "store": False,
                "input": [{"role": m["role"], "content": m["content"]} for m in turns]}
        if system:
            body["instructions"] = system
    else:
        headers = {"Authorization": f"Bearer {api_key}"}
        body = {"model": model, "temperature": 0.2, "max_tokens": max_tokens,
                "messages": ([{"role": "system", "content": system}] if system else []) + turns}
    try:
        response = await client.post(_endpoint(provider, base_url), headers=headers, json=body)
        if (provider != "anthropic" and response.status_code == 400
                and any(word in response.text for word in SAMPLING_KEYS)):
            # Reasoning models reject sampling/length parameters; retry with defaults.
            body = {key: value for key, value in body.items() if key not in SAMPLING_KEYS}
            response = await client.post(_endpoint(provider, base_url), headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise StrategyAIError(f"无法连接大模型服务：{type(exc).__name__}") from exc
    if response.status_code >= 400:
        detail = ""
        try:
            error = response.json().get("error")
            detail = (error.get("message") if isinstance(error, dict) else str(error or ""))[:200]
        except ValueError:
            pass
        raise StrategyAIError(f"大模型服务返回 HTTP {response.status_code}{'：' + detail if detail else ''}")
    data = response.json()
    if provider == "anthropic":
        return "".join(block.get("text", "") for block in data.get("content") or [] if block.get("type") == "text")
    if provider == "openai_responses":
        if isinstance(data.get("output_text"), str):
            return data["output_text"]
        return "".join(part.get("text", "") for item in data.get("output") or [] if item.get("type") == "message"
                       for part in item.get("content") or [] if part.get("type") == "output_text")
    return (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""


async def test_connection(*, provider: str, base_url: str, model: str, api_key: str,
                          client: httpx.AsyncClient | None = None) -> str:
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10))
    try:
        reply = await complete(client, provider=provider, base_url=base_url, model=model, api_key=api_key,
                               messages=[{"role": "user", "content": "只回复：连接成功"}], max_tokens=20)
    finally:
        if owns_client:
            await client.aclose()
    return reply.strip()[:80] or "（模型返回空内容）"


async def draft_strategy(text: str, *, base_url: str, model: str, api_key: str, provider: str = "openai",
                         current: dict[str, Any] | None = None,
                         client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    request = f"用户需求：{text.strip()}"
    if current:
        request += "\n请在下列现有策略基础上修改，未提到的部分保持不变：" + json.dumps(current, ensure_ascii=False)
    messages = [{"role": "system", "content": system_prompt()}, {"role": "user", "content": request}]
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(90, connect=10))
    last_error = ""
    try:
        for _attempt in range(MAX_REPAIRS + 1):
            content = await complete(client, provider=provider, base_url=base_url, model=model,
                                     api_key=api_key, messages=messages)
            try:
                payload = _extract_json(content)
                definition = RuleDefinition.model_validate(payload.get("definition") or payload)
                if definition.paper_validation is not None:
                    raise ValueError("不允许生成 paper_validation")
            except (ValueError, ValidationError) as exc:
                last_error = str(exc)[:1500]
                messages += [{"role": "assistant", "content": content},
                             {"role": "user", "content": f"这个 JSON 校验失败，请修正后重新只输出 JSON：{last_error}"}]
                continue
            explanation = payload.get("explanation") or ""
            if isinstance(explanation, list):
                explanation = "\n".join(str(item) for item in explanation)
            return {"definition": definition.model_dump(mode="json"),
                    "explanation": str(explanation),
                    "assumptions": [str(item) for item in payload.get("assumptions") or []],
                    "warnings": review_warnings(definition)}
    finally:
        if owns_client:
            await client.aclose()
    raise StrategyAIError(f"模型多次生成的策略未通过校验：{last_error}")
