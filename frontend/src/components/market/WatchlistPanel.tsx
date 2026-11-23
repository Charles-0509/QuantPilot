import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ArrowDown, ArrowUp, Check, ChevronDown, ChevronRight, FolderPlus, MoreHorizontal, Pencil, Plus, Trash2, X } from 'lucide-react'
import { api } from '../../api'
import { readJson, writeJson } from '../../storage'
import type { WatchlistGroup, WatchQuote } from '../../types'

const COLLAPSED_KEY = 'qp-watchlist-collapsed'

function signed(value: number | null, digits = 2, suffix = '') {
  if (value == null) return '—'
  return `${value > 0 ? '+' : ''}${value.toFixed(digits)}${suffix}`
}

function tone(value: number | null) {
  return value == null || value === 0 ? '' : value > 0 ? 'positive' : 'negative'
}

export default function WatchlistPanel({ groups, quotes, selected, onSelect, error }: {
  groups: WatchlistGroup[]
  quotes: Record<string, WatchQuote>
  selected: string
  onSelect: (symbol: string) => void
  error?: string
}) {
  const client = useQueryClient()
  const [collapsed, setCollapsed] = useState<number[]>(() => readJson<number[]>(COLLAPSED_KEY, []))
  const [adding, setAdding] = useState(false)
  const [newSymbol, setNewSymbol] = useState('')
  const [addGroup, setAddGroup] = useState<number | null>(null)
  const [creating, setCreating] = useState(false)
  const [groupName, setGroupName] = useState('')
  const [renaming, setRenaming] = useState<number | null>(null)
  const [renameValue, setRenameValue] = useState('')
  const [menu, setMenu] = useState<string | null>(null)
  const [dragging, setDragging] = useState<string | null>(null)
  const [dropTarget, setDropTarget] = useState<string | null>(null)
  const menuRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => { writeJson(COLLAPSED_KEY, collapsed) }, [collapsed])
  useEffect(() => {
    if (!menu) return
    const close = (event: MouseEvent) => { if (!menuRef.current?.contains(event.target as Node)) setMenu(null) }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [menu])

  const mutate = useMutation({
    mutationFn: ({ path, method, body }: { path: string; method: string; body?: unknown }) =>
      api<WatchlistGroup[]>(path, { method, body: body === undefined ? undefined : JSON.stringify(body) }),
    onSuccess: (next) => {
      client.setQueryData(['watchlist-groups'], next)
      client.invalidateQueries({ queryKey: ['watchlist'] })
    },
  })
  const run = (path: string, method: string, body?: unknown) => mutate.mutate({ path, method, body })

  const submitSymbol = (event: FormEvent) => {
    event.preventDefault()
    const symbol = newSymbol.trim().toUpperCase()
    const group = addGroup ?? groups[0]?.id
    if (!symbol || group == null) return
    mutate.mutate({ path: '/api/watchlist/items', method: 'POST', body: { symbol, group_id: group } }, {
      onSuccess: () => { setNewSymbol(''); setAdding(false); onSelect(symbol) },
    })
  }
  const submitGroup = (event: FormEvent) => {
    event.preventDefault()
    if (!groupName.trim()) return
    mutate.mutate({ path: '/api/watchlist/groups', method: 'POST', body: { name: groupName.trim() } }, {
      onSuccess: () => { setGroupName(''); setCreating(false) },
    })
  }
  const submitRename = (event: FormEvent, id: number) => {
    event.preventDefault()
    if (!renameValue.trim()) return
    mutate.mutate({ path: `/api/watchlist/groups/${id}`, method: 'PATCH', body: { name: renameValue.trim() } }, {
      onSuccess: () => setRenaming(null),
    })
  }
  const removeGroup = (group: WatchlistGroup) => {
    const target = groups.find((item) => item.id !== group.id)
    if (!target) return
    const detail = group.symbols.length ? `，其中 ${group.symbols.length} 只股票会移到“${target.name}”` : ''
    if (window.confirm(`删除分组“${group.name}”${detail}？`)) run(`/api/watchlist/groups/${group.id}`, 'DELETE')
  }
  const drop = (groupId: number, position?: number) => {
    if (dragging) run(`/api/watchlist/items/${dragging}`, 'PATCH', { group_id: groupId, position })
    setDragging(null)
    setDropTarget(null)
  }

  return <section className="watchlist-panel" aria-label="自选列表">
    <header className="watchlist-header">
      <strong>自选表</strong>
      <div className="watchlist-tools">
        <button className="icon-button" title="添加股票" aria-label="添加股票" onClick={() => { setAdding((value) => !value); setCreating(false) }}><Plus size={16} /></button>
        <button className="icon-button" title="新建分组" aria-label="新建分组" onClick={() => { setCreating((value) => !value); setAdding(false) }}><FolderPlus size={16} /></button>
      </div>
    </header>
    {adding && <form className="watchlist-inline-form" onSubmit={submitSymbol}>
      <input autoFocus aria-label="股票代码" placeholder="代码，如 AAPL" value={newSymbol} onChange={(event) => setNewSymbol(event.target.value.toUpperCase())} />
      <select aria-label="加入分组" value={addGroup ?? groups[0]?.id ?? ''} onChange={(event) => setAddGroup(Number(event.target.value))}>
        {groups.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}
      </select>
      <button className="icon-button" type="submit" aria-label="确认添加" disabled={mutate.isPending}><Check size={15} /></button>
    </form>}
    {creating && <form className="watchlist-inline-form" onSubmit={submitGroup}>
      <input autoFocus aria-label="分组名称" placeholder="分组名称，如 ETF" maxLength={40} value={groupName} onChange={(event) => setGroupName(event.target.value)} />
      <button className="icon-button" type="submit" aria-label="创建分组" disabled={mutate.isPending}><Check size={15} /></button>
    </form>}
    {(mutate.error || error) && <div className="watchlist-error" role="alert">{mutate.error ? (mutate.error as Error).message : error}
      {mutate.error && <button className="icon-button" aria-label="关闭提示" onClick={() => mutate.reset()}><X size={13} /></button>}</div>}
    <div className="watchlist-columns"><span>代码</span><span>最新价</span><span>涨跌</span><span>涨跌%</span></div>
    <div className="watchlist-scroll">
      {groups.map((group, groupIndex) => {
        const isCollapsed = collapsed.includes(group.id)
        return <div key={group.id} className="watchlist-group">
          <div className={`watchlist-group-header${dropTarget === `group-${group.id}` ? ' drop-target' : ''}`}
            onDragOver={(event) => { if (dragging) { event.preventDefault(); setDropTarget(`group-${group.id}`) } }}
            onDragLeave={() => setDropTarget(null)} onDrop={() => drop(group.id)}>
            <button className="watchlist-collapse" aria-label={isCollapsed ? `展开${group.name}` : `收起${group.name}`}
              onClick={() => setCollapsed((items) => isCollapsed ? items.filter((id) => id !== group.id) : [...items, group.id])}>
              {isCollapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
            </button>
            {renaming === group.id ? <form className="watchlist-rename" onSubmit={(event) => submitRename(event, group.id)}>
              <input autoFocus aria-label="新分组名称" maxLength={40} value={renameValue} onChange={(event) => setRenameValue(event.target.value)}
                onKeyDown={(event) => { if (event.key === 'Escape') setRenaming(null) }} />
              <button className="icon-button" type="submit" aria-label="保存分组名称"><Check size={13} /></button>
            </form> : <span className="watchlist-group-name">{group.name}<small>{group.symbols.length}</small></span>}
            {renaming !== group.id && <span className="watchlist-group-actions">
              <button className="icon-button" title="重命名" aria-label={`重命名${group.name}`} onClick={() => { setRenaming(group.id); setRenameValue(group.name) }}><Pencil size={13} /></button>
              <button className="icon-button" title="上移" aria-label={`上移${group.name}`} disabled={groupIndex === 0} onClick={() => run(`/api/watchlist/groups/${group.id}`, 'PATCH', { position: groupIndex - 1 })}><ArrowUp size={13} /></button>
              <button className="icon-button" title="下移" aria-label={`下移${group.name}`} disabled={groupIndex === groups.length - 1} onClick={() => run(`/api/watchlist/groups/${group.id}`, 'PATCH', { position: groupIndex + 1 })}><ArrowDown size={13} /></button>
              {groups.length > 1 && <button className="icon-button" title="删除分组" aria-label={`删除${group.name}`} onClick={() => removeGroup(group)}><Trash2 size={13} /></button>}
            </span>}
          </div>
          {!isCollapsed && group.symbols.map((symbol, index) => {
            const quote = quotes[symbol]
            return <div key={symbol} role="button" tabIndex={0} draggable
              className={`watchlist-row${symbol === selected ? ' selected' : ''}${dropTarget === symbol ? ' drop-target' : ''}${dragging === symbol ? ' dragging' : ''}`}
              onClick={() => onSelect(symbol)} onKeyDown={(event) => { if (event.key === 'Enter') onSelect(symbol) }}
              onDragStart={(event) => { setDragging(symbol); event.dataTransfer.effectAllowed = 'move' }}
              onDragEnd={() => { setDragging(null); setDropTarget(null) }}
              onDragOver={(event) => { if (dragging && dragging !== symbol) { event.preventDefault(); setDropTarget(symbol) } }}
              onDrop={(event) => { event.stopPropagation(); drop(group.id, index) }}>
              <span className="watchlist-symbol">{symbol}{quote?.session && quote.session !== 'regular' && <i title={quote.session === 'pre' ? '盘前价' : quote.session === 'post' ? '盘后价' : '已收盘'}>{quote.session === 'pre' ? '前' : quote.session === 'post' ? '后' : ''}</i>}</span>
              <span>{quote?.price != null ? quote.price.toFixed(2) : '—'}</span>
              <span className={tone(quote?.change ?? null)}>{signed(quote?.change ?? null)}</span>
              <span className={tone(quote?.change_pct ?? null)}>{signed(quote?.change_pct ?? null, 2, '%')}</span>
              <span className="watchlist-row-menu" ref={menu === symbol ? menuRef : undefined} onClick={(event) => event.stopPropagation()}>
                <button className="icon-button" aria-label={`${symbol}操作`} onClick={() => setMenu(menu === symbol ? null : symbol)}><MoreHorizontal size={14} /></button>
                {menu === symbol && <div className="watchlist-menu" role="menu">
                  {groups.filter((item) => item.id !== group.id).map((item) => <button key={item.id} role="menuitem" onClick={() => { run(`/api/watchlist/items/${symbol}`, 'PATCH', { group_id: item.id }); setMenu(null) }}>移到“{item.name}”</button>)}
                  <button role="menuitem" className="negative" onClick={() => { run(`/api/watchlist/items/${symbol}`, 'DELETE'); setMenu(null) }}>从自选删除</button>
                </div>}
              </span>
            </div>
          })}
          {!isCollapsed && !group.symbols.length && <div className="watchlist-empty">拖动股票到这里，或点击上方 + 添加</div>}
        </div>
      })}
    </div>
  </section>
}
