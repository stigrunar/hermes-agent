import { describe, expect, it } from 'vitest'

import type { ConversationBindingInfo, ProjectInfo, SessionInfo } from '@/types/hermes'

import { buildMessagingGroups, sessionConversationIdentity } from './messaging-groups'

const session = (over: Partial<SessionInfo> & Pick<SessionInfo, 'id'>): SessionInfo =>
  ({
    ended_at: null,
    input_tokens: 0,
    is_active: false,
    last_active: 0,
    message_count: 1,
    model: null,
    output_tokens: 0,
    preview: null,
    source: 'telegram',
    started_at: 0,
    title: null,
    tool_call_count: 0,
    ...over
  }) as SessionInfo

const binding: ConversationBindingInfo = {
  alias: 'Bound alias',
  created_at: 0,
  project_id: 'p_1',
  target_ref: 'opaque-topic',
  updated_at: 0
}

const project = (): ProjectInfo =>
  ({
    archived: false,
    board_slug: null,
    color: null,
    conversation_bindings: [binding],
    created_at: 0,
    description: null,
    folders: [],
    icon: null,
    id: 'p_1',
    name: 'Project',
    primary_path: null,
    slug: 'project'
  }) as ProjectInfo

const origin: NonNullable<SessionInfo['origin']> = {
  platform: 'telegram',
  display_label: 'Engineering',
  topic_label: 'Deployments',
  conversation_ref: 'opaque-chat',
  target_ref: 'opaque-topic'
}

describe('opaque messaging grouping', () => {
  it('uses only opaque refs and privacy-safe labels', () => {
    expect(sessionConversationIdentity(session({ id: 's1', source: 'desktop', origin }))).toMatchObject({
      platform: 'telegram',
      conversationLabel: 'Engineering',
      conversationRef: 'opaque-chat',
      targetRef: 'opaque-topic',
      topicLabel: 'Deployments'
    })
  })

  it('groups messaging and Desktop continuations under one bound topic', () => {
    const groups = buildMessagingGroups({
      projects: [project()],
      projectProfile: 'default',
      platformTotals: {},
      truncated: false,
      sessions: [
        session({ id: 'message', last_active: 1, origin }),
        session({ id: 'continuation', last_active: 2, origin, source: 'desktop' })
      ]
    })

    expect(groups[0].conversations[0].topics[0].label).toBe('Bound alias')
    expect(groups[0].conversations[0].topics[0].mainSession.id).toBe('continuation')
    expect(groups[0].conversations[0].topics[0].historySessions.map(row => row.id)).toEqual(['message'])
  })

  it('chooses one deterministic main session and keeps older sessions as history', () => {
    const groups = buildMessagingGroups({
      projects: [],
      projectProfile: 'default',
      platformTotals: {},
      truncated: false,
      sessions: [
        session({ id: 'older', last_active: 10, started_at: 5, origin }),
        session({ id: 'tie-b', last_active: 20, started_at: 10, origin }),
        session({ id: 'tie-a', last_active: 20, started_at: 10, origin })
      ]
    })

    const topic = groups[0].conversations[0].topics[0]

    expect(topic.mainSession.id).toBe('tie-a')
    expect(topic.historySessions.map(row => row.id)).toEqual(['tie-b', 'older'])
  })

  it('keeps identical opaque references isolated by profile ownership', () => {
    const groups = buildMessagingGroups({
      projects: [],
      projectProfile: null,
      platformTotals: {},
      truncated: false,
      sessions: [
        session({ id: 'default-new', last_active: 4, profile: 'default', origin }),
        session({ id: 'default-old', last_active: 2, profile: 'default', origin }),
        session({ id: 'work-new', last_active: 3, profile: 'work', origin }),
        session({ id: 'work-old', last_active: 1, profile: 'work', origin })
      ]
    })

    const topics = groups[0].conversations.flatMap(conversation => conversation.topics)

    expect(topics).toHaveLength(2)
    expect(groups[0].conversations.map(conversation => conversation.profile).sort()).toEqual(['default', 'work'])
    expect(topics.find(topic => topic.identity.profile === 'default')).toMatchObject({
      mainSession: { id: 'default-new' },
      historySessions: [{ id: 'default-old' }]
    })
    expect(topics.find(topic => topic.identity.profile === 'work')).toMatchObject({
      mainSession: { id: 'work-new' },
      historySessions: [{ id: 'work-old' }]
    })
  })

  it('models a direct conversation as one main row without inventing a topic identity', () => {
    const directOrigin = {
      ...origin,
      conversation_ref: 'opaque-dm',
      display_label: 'Stig',
      target_ref: 'opaque-dm',
      topic_label: null
    }

    const groups = buildMessagingGroups({
      projects: [],
      projectProfile: 'default',
      platformTotals: {},
      truncated: false,
      sessions: [session({ id: 'dm', last_active: 1, origin: directOrigin })]
    })

    const conversation = groups[0].conversations[0]

    expect(conversation.label).toBe('Stig')
    expect(conversation.topics).toHaveLength(1)
    expect(conversation.topics[0]).toMatchObject({
      label: 'Stig',
      mainSession: { id: 'dm' },
      historySessions: []
    })
  })

  it('keeps legacy rows flat and refuses peer-profile mutation', () => {
    const groups = buildMessagingGroups({
      projects: [project()],
      projectProfile: 'default',
      platformTotals: {},
      truncated: false,
      sessions: [
        session({ id: 'legacy' }),
        session({ id: 'default', profile: 'default', origin }),
        session({ id: 'work', profile: 'work', origin })
      ]
    })

    const topics = groups[0].conversations.flatMap(conversation => conversation.topics)

    expect(groups[0].flatSessions.map(row => row.id)).toEqual(['legacy'])
    expect(topics.find(topic => topic.identity.profile === 'default')?.binding?.alias).toBe('Bound alias')
    expect(topics.find(topic => topic.identity.profile === 'work')).toMatchObject({
      binding: null,
      canManageBinding: false
    })
  })
})
