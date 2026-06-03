import { describe, expect, it } from 'vitest'

import type { DashboardAgent, DashboardConversation, DashboardProject, SessionInfo } from '@/hermes'

import { sessionGroupingLabel, sessionGroupsFor, sessionsForScope, sidebarControlSurfaceFor } from './session-groups'

const baseSession = (overrides: Partial<SessionInfo>): SessionInfo => ({
  archived: false,
  cwd: null,
  ended_at: null,
  id: 'session',
  input_tokens: 0,
  is_active: false,
  last_active: 100,
  message_count: 1,
  model: null,
  output_tokens: 0,
  preview: '',
  source: 'telegram',
  started_at: 100,
  title: null,
  tool_call_count: 0,
  ...overrides
})

const agent = (overrides: Partial<DashboardAgent>): DashboardAgent => ({
  gateway: { running: true, state: 'running' },
  id: 'profile:default',
  kind: 'hermes-profile',
  name: 'default',
  profile: 'default',
  ...overrides
})

const conversation = (overrides: Partial<DashboardConversation>): DashboardConversation => ({
  chat_id: '-1001',
  display_label: 'Dolly Main / Vw AI render',
  id: 'telegram:-1001:11648',
  name: 'Vw AI render',
  platform: 'telegram',
  session_ids: ['topic-session'],
  source: ['config', 'sessions'],
  thread_id: '11648',
  type: 'topic',
  ...overrides
})

const project = (overrides: Partial<DashboardProject>): DashboardProject => ({
  chat_id: '-1001',
  conversation_id: 'telegram:-1001:11648',
  display_label: 'Dolly Main / Vw AI render',
  id: 'project:telegram:-1001:11648',
  name: 'Vw AI render',
  platform: 'telegram',
  source: 'telegram_topic',
  topic_id: '11648',
  ...overrides
})

describe('sessionGroupingLabel', () => {
  it('prefers Telegram topic/group labels over filesystem workspace labels', () => {
    const session = baseSession({
      cwd: '/home/openclaw/.hermes/hermes-agent',
      origin: {
        chat_name: 'Dolly Main Projects',
        chat_topic: '▸ HWNextApp – /finance',
        chat_type: 'group',
        display_label: 'Dolly Main Projects / ▸ HWNextApp – /finance',
        group_key: 'telegram:topic:finance',
        platform: 'telegram'
      }
    })

    expect(sessionGroupingLabel(session)).toEqual({
      id: 'telegram:topic:finance',
      label: 'Dolly Main Projects / ▸ HWNextApp – /finance',
      path: null
    })
  })

  it('falls back to workspace labels when origin metadata is unavailable', () => {
    const session = baseSession({ cwd: '/home/openclaw/.hermes/hermes-agent' })

    expect(sessionGroupingLabel(session)).toEqual({
      id: '/home/openclaw/.hermes/hermes-agent',
      label: 'hermes-agent',
      path: '/home/openclaw/.hermes/hermes-agent'
    })
  })
})

describe('sessionGroupsFor', () => {
  it('keeps Telegram topic groups above workspace groups even when workspace sessions are newer', () => {
    const sessions = [
      baseSession({ id: 'workspace', cwd: '/tmp/hermes-agent', started_at: 300 }),
      baseSession({
        id: 'topic',
        origin: {
          display_label: 'Dolly Main Projects / ▸ HW-external tilbud',
          group_key: 'telegram:topic:external',
          platform: 'telegram'
        },
        started_at: 100
      })
    ]

    const groups = sessionGroupsFor(sessions)

    expect(groups.map(group => group.label)).toEqual(['Dolly Main Projects / ▸ HW-external tilbud', 'hermes-agent'])
  })
})

describe('sidebarControlSurfaceFor', () => {
  it('builds one-gateway agents projects chats and workspaces without duplicating project conversations as chats', () => {
    const sessions = [
      baseSession({ id: 'topic-session', cwd: '/work/hermes-agent', started_at: 300 }),
      baseSession({ id: 'dm-session', cwd: '/work/other', started_at: 200 })
    ]

    const sections = sidebarControlSurfaceFor({
      agents: [agent({})],
      conversations: [
        conversation({}),
        conversation({
          display_label: 'Stig DM',
          id: 'telegram:42',
          name: 'Stig DM',
          session_ids: ['dm-session'],
          thread_id: undefined,
          type: 'dm'
        })
      ],
      projects: [project({})],
      sessions
    })

    expect(sections.find(section => section.id === 'agents')?.items.map(item => item.label)).toEqual(['default'])
    expect(sections.find(section => section.id === 'projects')?.items.map(item => item.label)).toEqual(['Vw AI render'])
    expect(sections.find(section => section.id === 'chats')?.items.map(item => item.label)).toEqual(['Stig DM'])
    expect(sections.find(section => section.id === 'workspaces')?.items.map(item => item.label)).toEqual([
      'hermes-agent',
      'other'
    ])
  })

  it('marks agents as display-only so they do not select an empty Recent Sessions scope', () => {
    const sessions = [baseSession({ id: 'topic-session' }), baseSession({ id: 'other-session' })]

    const agentItem = sidebarControlSurfaceFor({
      agents: [agent({})],
      conversations: [],
      projects: [],
      sessions
    }).find(section => section.id === 'agents')?.items[0]

    expect(agentItem?.selectable).toBe(false)
    expect(agentItem?.scope.kind).toBe('agent')
  })

  it('scopes sessions from a project topic click when matching sessions are loaded', () => {
    const sessions = [baseSession({ id: 'topic-session' }), baseSession({ id: 'other-session' })]

    const projectScope = sidebarControlSurfaceFor({
      agents: [],
      conversations: [conversation({ session_ids: ['topic-session', 'not-loaded'] })],
      projects: [project({})],
      sessions
    }).find(section => section.id === 'projects')?.items[0]?.scope

    expect(projectScope).toBeDefined()
    expect(sessionsForScope(sessions, projectScope ?? null).map(session => session.id)).toEqual(['topic-session'])
  })
})
