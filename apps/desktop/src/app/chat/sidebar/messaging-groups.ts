import { normalizeSessionSource, sessionSourceLabel } from '@/lib/session-source'
import type { ConversationBindingInfo, ProjectInfo, SessionInfo } from '@/types/hermes'

export interface SessionConversationIdentity {
  profile: string
  platform: string
  targetRef: string
  conversationRef: string
  conversationLabel: string
  topicLabel: null | string
}

export interface MessagingTopicGroup {
  id: string
  identity: SessionConversationIdentity
  label: string
  binding: ConversationBindingInfo | null
  canManageBinding: boolean
  sessions: SessionInfo[]
}

export interface MessagingConversationGroup {
  id: string
  label: string
  topics: MessagingTopicGroup[]
}

export interface MessagingPlatformGroup {
  sourceId: string
  label: string
  conversations: MessagingConversationGroup[]
  flatSessions: SessionInfo[]
  sessions: SessionInfo[]
  total: number
  hasMore: boolean
}

const text = (value: null | string | undefined): string => (value ?? '').trim()
const profileKey = (session: SessionInfo): string => text(session.profile) || 'default'

export function sessionConversationIdentity(session: SessionInfo): null | SessionConversationIdentity {
  const origin = session.origin
  const platform = normalizeSessionSource(text(origin?.platform))
  const targetRef = text(origin?.target_ref)
  const conversationRef = text(origin?.conversation_ref) || targetRef

  if (!platform || !targetRef || !conversationRef) {return null}

  return {
    profile: profileKey(session),
    platform,
    targetRef,
    conversationRef,
    conversationLabel: text(origin?.display_label) || 'Conversation',
    topicLabel: text(origin?.topic_label) || null
  }
}

function bindingMap(projects: readonly ProjectInfo[]): Map<string, ConversationBindingInfo> {
  const bindings = new Map<string, ConversationBindingInfo>()

  for (const project of projects) {
    for (const binding of project.conversation_bindings ?? []) {bindings.set(binding.target_ref, binding)}
  }

  return bindings
}

const sessionTime = (session: SessionInfo): number => session.last_active || session.started_at || 0

const topicTime = (topic: MessagingTopicGroup | undefined): number =>
  topic?.sessions[0] ? sessionTime(topic.sessions[0]) : 0

export function buildMessagingGroups(params: {
  sessions: readonly SessionInfo[]
  projects: readonly ProjectInfo[]
  projectProfile: null | string
  platformTotals: Record<string, number>
  truncated: boolean
}): MessagingPlatformGroup[] {
  const bindings = bindingMap(params.projects)
  const byPlatform = new Map<string, { identified: SessionInfo[]; flat: SessionInfo[] }>()

  for (const session of params.sessions) {
    const identity = sessionConversationIdentity(session)
    const sourceId = identity?.platform ?? normalizeSessionSource(session.source)

    if (!sourceId) {continue}
    const bucket = byPlatform.get(sourceId) ?? { identified: [], flat: [] }
    bucket[identity ? 'identified' : 'flat'].push(session)
    byPlatform.set(sourceId, bucket)
  }

  return [...byPlatform.entries()]
    .map(([sourceId, bucket]) => {
      const conversations = new Map<string, MessagingConversationGroup>()

      for (const session of bucket.identified) {
        const identity = sessionConversationIdentity(session)

        if (!identity) {continue}
        const conversationId = `${identity.profile}:${identity.conversationRef}`
        const topicId = `${identity.profile}:${identity.targetRef}`

        const conversation = conversations.get(conversationId) ?? {
          id: conversationId,
          label: identity.conversationLabel,
          topics: []
        }

        let topic = conversation.topics.find(item => item.id === topicId)

        if (!topic) {
          const canManageBinding = params.projectProfile !== null && identity.profile === params.projectProfile
          const binding = canManageBinding ? (bindings.get(identity.targetRef) ?? null) : null
          topic = {
            id: topicId,
            identity,
            binding,
            canManageBinding,
            label: binding?.alias || identity.topicLabel || identity.conversationLabel,
            sessions: []
          }
          conversation.topics.push(topic)
        }

        topic.sessions.push(session)
        conversations.set(conversationId, conversation)
      }

      for (const conversation of conversations.values()) {
        for (const topic of conversation.topics) {
          topic.sessions.sort((a, b) => sessionTime(b) - sessionTime(a) || a.id.localeCompare(b.id))
        }

        conversation.topics.sort((a, b) => topicTime(b) - topicTime(a) || a.label.localeCompare(b.label))
      }

      const nested = [...conversations.values()].sort(
        (a, b) => topicTime(b.topics[0]) - topicTime(a.topics[0]) || a.label.localeCompare(b.label)
      )

      const sessions = [...bucket.identified, ...bucket.flat].sort((a, b) => sessionTime(b) - sessionTime(a))
      const known = params.platformTotals[sourceId]

      return {
        sourceId,
        label: sessionSourceLabel(sourceId) ?? sourceId,
        conversations: nested,
        flatSessions: bucket.flat.sort((a, b) => sessionTime(b) - sessionTime(a)),
        sessions,
        total: Math.max(sessions.length, known ?? 0),
        hasMore: known != null ? known > sessions.length : params.truncated
      }
    })
    .sort((a, b) => sessionTime(b.sessions[0]) - sessionTime(a.sessions[0]))
}
