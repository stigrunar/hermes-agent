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
    expect(groups[0].conversations[0].topics[0].sessions.map(row => row.id)).toEqual(['continuation', 'message'])
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
