import json
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import OrderRecord, Signal, Strategy, StrategyPosition
from app.services.performance import strategy_performance
from app.services.strategy_ai import StrategyAIError, draft_strategy
from app.templates import TEMPLATES


def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'perf.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def order(oid, side, qty, price, minute, order_type='market', signal_id=None, strategy='s', symbol='SPY'):
    stamp = datetime(2026, 11, 3, 15, minute, tzinfo=timezone.utc)
    return OrderRecord(id=oid, user_id=1, strategy_id=strategy, signal_id=signal_id, client_order_id=f'qp-{oid}',
                       symbol=symbol, side=side, order_type=order_type, qty=qty, filled_qty=qty, filled_avg_price=price,
                       status='filled', submitted_at=stamp, raw={'filled_at': stamp.isoformat()})


def test_fifo_attribution_separates_exit_reasons_and_cleared_ownership(tmp_path):
    db = session(tmp_path)
    when = datetime(2026, 11, 3, tzinfo=timezone.utc)
    db.add_all([
        Strategy(id='s', owner_user_id=1, name='布林带', enabled=True, definition={'symbols': ['SPY']}),
        Strategy(id='other', owner_user_id=1, name='其他', enabled=True, definition={'symbols': ['SPY']}),
        Signal(id='b1', user_id=1, strategy_id='s', symbol='SPY', unique_key='b1', bar_timestamp=when, action='buy', price=100, reason='跌破下轨'),
        Signal(id='x1', user_id=1, strategy_id='s', symbol='SPY', unique_key='x1', bar_timestamp=when, action='sell', price=104, reason='回到中轨'),
        order('buy1', 'buy', 10, 100, 0, signal_id='b1'),
        order('stop1', 'sell', 4, 98, 10, order_type='stop', signal_id='b1'),
        order('exit1', 'sell', 6, 104, 20, signal_id='x1'),
        order('buy2', 'buy', 5, 110, 30, signal_id='b1'),
        order('buy3', 'buy', 3, 120, 40, signal_id='b1'),
        order('obuy', 'buy', 2, 50, 5, strategy='other'),
        StrategyPosition(user_id=1, strategy_id='s', symbol='SPY', qty=5),
    ])
    db.commit()
    result = {row['strategy_id']: row for row in strategy_performance(db, 1, {'SPY': 112})}
    s = result['s']
    assert s['realized_pnl'] == pytest.approx(-8 + 24)
    assert s['exit_breakdown']['止损'] == {'count': 1, 'pnl': pytest.approx(-8)}
    assert s['exit_breakdown']['策略离场信号']['count'] == 1
    assert s['trade_list'][0]['exit_reason'] == '回到中轨' and s['trade_list'][0]['entry_reason'] == '跌破下轨'
    assert len(s['open_lots']) == 1 and s['open_lots'][0]['qty'] == 5 and s['open_lots'][0]['entry_price'] == 110
    assert s['unrealized_pnl'] == pytest.approx((112 - 110) * 5)
    assert s['ownership_cleared_qty'] == 3
    assert result['other']['realized_pnl'] == 0 and result['other']['trades'] == 0
    assert any('止损' in note for note in s['diagnosis'])


def model_reply(content):
    return httpx.Response(200, json={'choices': [{'message': {'content': content}}]})


@pytest.mark.asyncio
async def test_strategy_draft_repairs_invalid_model_output_once():
    calls = []
    valid = dict(TEMPLATES['rsi_reversion'], name='RSI 超卖反弹', symbols=['QQQ'])

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert request.headers['authorization'] == 'Bearer secret'
        if len(calls) == 1:
            return model_reply('好的：{"definition": {"name": "坏的"}}')
        assert '校验失败' in body['messages'][-1]['content']
        return model_reply(json.dumps({'definition': valid, 'explanation': ['RSI 低于 30 买入'], 'assumptions': ['默认 15 分钟']}, ensure_ascii=False))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        draft = await draft_strategy('QQQ 超卖时买入', base_url='http://llm/v1', model='m', api_key='secret', client=client)
    assert len(calls) == 2 and draft['definition']['symbols'] == ['QQQ']
    assert draft['explanation'] == 'RSI 低于 30 买入' and draft['assumptions'] == ['默认 15 分钟']
    assert any('回测' in item for item in draft['warnings'])


@pytest.mark.asyncio
async def test_strategy_draft_rejects_paper_validation_and_reports_failure():
    bad = dict(TEMPLATES['rsi_reversion'], paper_validation={'expires_at': '2030-01-01T00:00:00Z'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: model_reply(json.dumps({'definition': bad})))) as client:
        with pytest.raises(StrategyAIError):
            await draft_strategy('任意', base_url='http://llm/v1', model='m', api_key='k', client=client)


@pytest.mark.asyncio
async def test_anthropic_messages_format_and_repair_turns():
    from app.services.strategy_ai import draft_strategy as draft
    valid = dict(TEMPLATES['rsi_reversion'], name='RSI 反弹')
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append((str(request.url), request.headers.get('x-api-key'), request.headers.get('anthropic-version'), body))
        assert all(m['role'] in {'user', 'assistant'} for m in body['messages']) and body['system']
        text = '不是 JSON' if len(seen) == 1 else json.dumps({'definition': valid})
        return httpx.Response(200, json={'content': [{'type': 'text', 'text': text}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await draft('RSI 反弹', provider='anthropic', base_url='https://api.anthropic.com', model='claude', api_key='k', client=client)
    assert seen[0][:3] == ('https://api.anthropic.com/v1/messages', 'k', '2023-06-01')
    assert [m['role'] for m in seen[1][3]['messages']] == ['user', 'assistant', 'user']
    assert result['definition']['name'] == 'RSI 反弹'


@pytest.mark.asyncio
async def test_openai_reasoning_model_retries_without_sampling_parameters():
    from app.services.strategy_ai import test_connection
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if 'temperature' in body:
            return httpx.Response(400, json={'error': {'message': "Unsupported parameter: 'temperature'"}})
        return model_reply('连接成功')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reply = await test_connection(provider='openai', base_url='https://api.openai.com/v1', model='o3', api_key='k', client=client)
    assert reply == '连接成功' and 'max_tokens' not in bodies[-1] and len(bodies) == 2


@pytest.mark.asyncio
async def test_http_errors_are_reported_without_secrets():
    from app.services.strategy_ai import test_connection
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401, json={'error': {'message': 'invalid x-api-key'}}))) as client:
        with pytest.raises(StrategyAIError, match='HTTP 401'):
            await test_connection(provider='anthropic', base_url='https://api.anthropic.com/v1', model='m', api_key='secret-k', client=client)


@pytest.mark.asyncio
async def test_openai_responses_api_format_and_output_parsing():
    from app.services.strategy_ai import draft_strategy as draft
    valid = dict(TEMPLATES['rsi_reversion'], name='Responses 草稿')
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append((str(request.url), request.headers['authorization'], body))
        text = '格式不对' if len(seen) == 1 else json.dumps({'definition': valid})
        return httpx.Response(200, json={'output': [
            {'type': 'reasoning', 'summary': []},
            {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': text}]}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await draft('RSI', provider='openai_responses', base_url='https://api.openai.com/v1', model='gpt', api_key='k', client=client)
    url, auth, body = seen[1]
    assert url == 'https://api.openai.com/v1/responses' and auth == 'Bearer k'
    assert body['instructions'] and body['store'] is False and 'messages' not in body
    assert [m['role'] for m in body['input']] == ['user', 'assistant', 'user']
    assert result['definition']['name'] == 'Responses 草稿'


@pytest.mark.asyncio
async def test_responses_api_reasoning_model_drops_unsupported_parameters():
    from app.services.strategy_ai import test_connection
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if 'temperature' in body:
            return httpx.Response(400, json={'error': {'message': "Unsupported parameter: 'temperature'"}})
        return httpx.Response(200, json={'output_text': '连接成功'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await test_connection(provider='openai_responses', base_url='https://api.openai.com/v1', model='o3', api_key='k', client=client) == '连接成功'
    assert 'max_output_tokens' not in bodies[-1] and len(bodies) == 2
