import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type * as React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ProjectInfo, SessionInfo } from '@/hermes'

const { bindConversationToProject, unbindConversationFromProject } = vi.hoisted(() => ({
  bindConversationToProject: vi.fn(async () => undefined),
  unbindConversationFromProject: vi.fn(async () => undefined)
}))

vi.mock('@/store/projects', async importOriginal => ({
  ...((await importOriginal()) as object),
  bindConversationToProject,
  unbindConversationFromProject
}))

import { SidebarSessionsSection, VIRTUALIZE_THRESHOLD } from './sessions-section'
import type { VirtualSessionListProps } from './virtual-session-list'

afterEach(cleanup)

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      common: { close: 'Close' },
      sidebar: {
        dateDivider: {
          earlierThisMonth: 'Earlier this month',
          lastMonth: 'Last month',
          lastWeek: 'Last week',
          older: 'Older',
          today: 'Today',
          yesterday: 'Yesterday'
        },
        projects: {
          topicHistory: 'History',
          topicMain: 'Main',
          topicBindAction: 'Bind to Project',
          topicManageAction: 'Manage Project binding',
          topicProjectDescription: 'Keep this topic in one Project.',
          topicProjectPlaceholder: 'Choose a Project',
          topicAliasPlaceholder: 'Optional local alias',
          topicBind: 'Bind to Project',
          topicSave: 'Save binding',
          topicUnbind: 'Unbind'
        }
      }
    }
  })
}))

const mockVirtualListPropsHistory: VirtualSessionListProps[] = []

vi.mock('./virtual-session-list', () => ({
  VirtualSessionList: (props: VirtualSessionListProps) => {
    mockVirtualListPropsHistory.push(props)

    return <div data-testid="virtual-session-list">Virtual List ({props.rows.length} rows)</div>
  }
}))

vi.mock('./session-row', () => ({
  SidebarSessionRow: ({
    displayTitle,
    onResume,
    session
  }: {
    displayTitle?: string
    onResume: () => void
    session: SessionInfo
  }) => (
    <button data-testid={`session-row-${session.id}`} onClick={onResume} type="button">
      {displayTitle ?? session.id}
    </button>
  )
}))

function makeSession(id: string, startedAt = 1000): SessionInfo {
  return {
    handoff_platform: null,
    handoff_state: null,
    id,
    last_active: startedAt,
    profile: 'default',
    started_at: startedAt
  } as unknown as SessionInfo
}

function generateSessions(count: number): SessionInfo[] {
  return Array.from({ length: count }, (_, i) => makeSession(`session-${i + 1}`, 10000 - i * 100))
}

const noop = () => {}

describe('SidebarSessionsSection memoization & virtualizer stability', () => {
  it('memoizes flatRows and passes the exact same rows array reference across parent re-renders', () => {
    mockVirtualListPropsHistory.length = 0

    const sessions = generateSessions(VIRTUALIZE_THRESHOLD + 5)

    const { rerender } = render(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={<div>Empty</div>}
        label="Sessions"
        onArchiveSession={noop}
        onDeleteSession={noop}
        onResumeSession={noop}
        onToggle={noop}
        onTogglePin={noop}
        open={true}
        pinned={false}
        sessions={sessions}
      />
    )

    expect(mockVirtualListPropsHistory.length).toBe(1)
    const initialRowsRef = mockVirtualListPropsHistory[0].rows
    expect(initialRowsRef.length).toBeGreaterThan(VIRTUALIZE_THRESHOLD)

    // Re-render parent with the exact same sessions array and props
    rerender(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={<div>Empty</div>}
        label="Sessions"
        onArchiveSession={noop}
        onDeleteSession={noop}
        onResumeSession={noop}
        onToggle={noop}
        onTogglePin={noop}
        open={true}
        pinned={false}
        sessions={sessions}
      />
    )

    expect(mockVirtualListPropsHistory.length).toBe(2)
    const nextRowsRef = mockVirtualListPropsHistory[1].rows

    // Confirm that the flatRows array reference remains strictly identical across renders (useMemo proof)
    expect(nextRowsRef).toBe(initialRowsRef)
  })

  it('re-computes flatRows reference when grouping or sessions change', () => {
    mockVirtualListPropsHistory.length = 0

    const initialSessions = generateSessions(VIRTUALIZE_THRESHOLD + 2)

    const { rerender } = render(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={<div>Empty</div>}
        grouping="none"
        label="Sessions"
        onArchiveSession={noop}
        onDeleteSession={noop}
        onResumeSession={noop}
        onToggle={noop}
        onTogglePin={noop}
        open={true}
        pinned={false}
        sessions={initialSessions}
      />
    )

    const firstRowsRef = mockVirtualListPropsHistory[0].rows

    // Switch on date dividers
    rerender(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={<div>Empty</div>}
        grouping="date"
        label="Sessions"
        onArchiveSession={noop}
        onDeleteSession={noop}
        onResumeSession={noop}
        onToggle={noop}
        onTogglePin={noop}
        open={true}
        pinned={false}
        sessions={initialSessions}
      />
    )

    const secondRowsRef = mockVirtualListPropsHistory[1].rows
    expect(secondRowsRef).not.toBe(firstRowsRef)

    // Change sessions array identity
    const updatedSessions = generateSessions(VIRTUALIZE_THRESHOLD + 4)
    rerender(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={<div>Empty</div>}
        grouping="date"
        label="Sessions"
        onArchiveSession={noop}
        onDeleteSession={noop}
        onResumeSession={noop}
        onToggle={noop}
        onTogglePin={noop}
        open={true}
        pinned={false}
        sessions={updatedSessions}
      />
    )

    const thirdRowsRef = mockVirtualListPropsHistory[2].rows
    expect(thirdRowsRef).not.toBe(secondRowsRef)
  })

  it('opens the inline topic binding dialog and binds the canonical identity', async () => {
    bindConversationToProject.mockClear()
    const topicSession = makeSession('topic-session')

    const projects = [
      {
        archived: false,
        board_slug: null,
        color: null,
        created_at: 0,
        description: null,
        folders: [],
        icon: null,
        id: 'p_project',
        name: 'Operations',
        primary_path: null,
        slug: 'operations'
      } as ProjectInfo
    ]

    render(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={<div>Empty</div>}
        label="Telegram"
        messagingConversations={[
          {
            id: 'telegram:chat',
            label: 'Engineering',
            profile: 'default',
            topics: [
              {
                binding: null,
                canManageBinding: true,
                id: 'telegram:chat:topic',
                identity: {
                  conversationLabel: 'Engineering',
                  conversationRef: 'opaque-chat',
                  platform: 'telegram',
                  profile: 'default',
                  targetRef: 'opaque-topic',
                  topicLabel: 'Deployments'
                },
                label: 'Deployments',
                mainSession: topicSession,
                historySessions: []
              }
            ]
          }
        ]}
        onArchiveSession={noop}
        onDeleteSession={noop}
        onResumeSession={noop}
        onToggle={noop}
        onTogglePin={noop}
        open={true}
        pinned={false}
        projects={projects}
        sessions={[]}
      />
    )

    fireEvent.click(screen.getByRole('button', { name: 'Bind to Project' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Optional local alias' }), {
      target: { value: 'Deploys' }
    })
    fireEvent.click(screen.getAllByRole('button', { name: 'Bind to Project' }).at(-1)!)

    await waitFor(() =>
      expect(bindConversationToProject).toHaveBeenCalledWith({
        alias: 'Deploys',
        projectId: 'p_project',
        targetRef: 'opaque-topic'
      })
    )
  })

  it('shows only Main by default and reveals older topic history on demand', () => {
    const main = makeSession('main', 3000)
    const older = makeSession('older', 2000)
    const oldest = makeSession('oldest', 1000)

    const onResumeSession = vi.fn()

    render(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={<div>Empty</div>}
        label="Telegram"
        messagingConversations={[
          {
            id: 'default:opaque-chat',
            label: 'Engineering',
            profile: 'default',
            topics: [
              {
                binding: null,
                canManageBinding: false,
                historySessions: [older, oldest],
                id: 'default:opaque-topic',
                identity: {
                  conversationLabel: 'Engineering',
                  conversationRef: 'opaque-chat',
                  platform: 'telegram',
                  profile: 'default',
                  targetRef: 'opaque-topic',
                  topicLabel: 'Deployments'
                },
                label: 'Deployments',
                mainSession: main
              }
            ]
          }
        ]}
        onArchiveSession={noop}
        onDeleteSession={noop}
        onResumeSession={onResumeSession}
        onToggle={noop}
        onTogglePin={noop}
        open={true}
        pinned={false}
        sessions={[]}
      />
    )

    expect(screen.getByTestId('session-row-main').textContent).toBe('Main')
    expect(screen.queryByTestId('session-row-older')).toBeNull()
    expect(screen.queryByTestId('session-row-oldest')).toBeNull()

    fireEvent.click(screen.getByTestId('session-row-main'))
    expect(onResumeSession).toHaveBeenCalledWith('main')

    fireEvent.click(screen.getByRole('button', { name: 'History' }))

    expect(screen.getByTestId('session-row-older')).not.toBeNull()
    expect(screen.getByTestId('session-row-oldest')).not.toBeNull()
  })

  it('collapses a direct conversation without a duplicate topic row', () => {
    const direct = makeSession('direct', 1000)

    render(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={<div>Empty</div>}
        label="Telegram"
        messagingConversations={[
          {
            id: 'default:opaque-dm',
            label: 'Stig',
            profile: 'default',
            topics: [
              {
                binding: null,
                canManageBinding: false,
                historySessions: [],
                id: 'default:opaque-dm',
                identity: {
                  conversationLabel: 'Stig',
                  conversationRef: 'opaque-dm',
                  platform: 'telegram',
                  profile: 'default',
                  targetRef: 'opaque-dm',
                  topicLabel: null
                },
                label: 'Stig',
                mainSession: direct
              }
            ]
          }
        ]}
        onArchiveSession={noop}
        onDeleteSession={noop}
        onResumeSession={noop}
        onToggle={noop}
        onTogglePin={noop}
        open={true}
        pinned={false}
        sessions={[]}
      />
    )

    expect(screen.getAllByText('Stig')).toHaveLength(1)
    expect(screen.getByTestId('session-row-direct').textContent).toBe('Main')
  })
})
