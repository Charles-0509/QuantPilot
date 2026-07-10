import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { ConditionGroup } from '../types'
import RuleBuilder from './RuleBuilder'

describe('RuleBuilder', () => {
  it('adds a condition card through the visual editor', () => {
    const group: ConditionGroup = { type: 'group', op: 'AND', negate: false, children: [] }
    const onChange = vi.fn()
    render(<RuleBuilder title="入场条件" value={group} onChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: /条件/ }))
    expect(onChange).toHaveBeenCalled()
    expect(onChange.mock.calls[0][0].children).toHaveLength(1)
  })
})
