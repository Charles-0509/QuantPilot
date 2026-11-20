/// <reference types="vite/client" />
import { render } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import css from '../styles.css?raw'
import type { ConditionGroup } from '../types'
import RuleBuilder from './RuleBuilder'

const rule = (selector: string, source = css) => {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return source.match(new RegExp(`(?:^|[\\s}])${escaped}\\s*\\{([^}]*)\\}`))?.[1] ?? ''
}

const containerBlock = () => css.match(/@container condition-group \(max-width: (\d+)px\) \{([\s\S]*?)\n\}/)

describe('RuleBuilder layout', () => {
  it('makes each condition group a size container so nested rows adapt to their own width', () => {
    expect(rule('.condition-group')).toMatch(/container:\s*condition-group\s*\/\s*inline-size/)
    expect(rule('.condition-group')).toMatch(/min-width:\s*0/)
  })

  it('wraps group header actions instead of pushing them past the card edge', () => {
    expect(rule('.condition-group-header')).toMatch(/flex-wrap:\s*wrap/)
    expect(rule('.condition-group-actions')).toMatch(/flex-wrap:\s*wrap/)
  })

  it('stacks condition rows in narrow groups while keeping the delete button visible', () => {
    const block = containerBlock()
    expect(block).not.toBeNull()
    const [, width, body] = block!
    expect(Number(width)).toBeGreaterThanOrEqual(600)
    expect(rule('.condition-row', body)).toMatch(/grid-template-columns:\s*minmax\(0,\s*1fr\)\s+36px/)
    expect(rule('.condition-row > .icon-button', body)).toMatch(/grid-column:\s*2/)
  })

  it('renders delete buttons as direct children of condition rows so the narrow layout applies', () => {
    const group: ConditionGroup = {
      type: 'group', op: 'AND', negate: false,
      children: [
        { type: 'condition', left: { kind: 'price', field: 'close', offset: 0 }, operator: '>', right: { kind: 'number', value: 1, offset: 0 } },
        { type: 'group', op: 'OR', negate: true, children: [{ type: 'condition', left: { kind: 'price', field: 'close', offset: 0 }, operator: '<', right: { kind: 'number', value: 2, offset: 0 } }] },
      ],
    }
    const { container } = render(<RuleBuilder title="入场条件" value={group} onChange={vi.fn()} />)
    const rows = container.querySelectorAll('.condition-group .condition-row')
    expect(rows).toHaveLength(2)
    rows.forEach((row) => expect(row.querySelector(':scope > .icon-button')).not.toBeNull())
    expect(container.querySelectorAll('.condition-group.nested')).toHaveLength(1)
  })
})
