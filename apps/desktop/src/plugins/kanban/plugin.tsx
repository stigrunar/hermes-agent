import {
  Badge,
  Button,
  cn,
  ErrorState,
  host,
  Loader,
  useValue
} from '@hermes/plugin-sdk'
import type { HermesPlugin, PluginContext } from '@hermes/plugin-sdk'
import * as React from 'react'

interface KanbanTask {
  assignee?: string | null
  id: string
  title?: string | null
}

interface KanbanColumn {
  name: string
  tasks: KanbanTask[]
}

interface KanbanBoard {
  columns: KanbanColumn[]
  latest_event_id?: number
}

const AUTO_REFRESH_DELAY_MS = 30_000
const boardRequests = new WeakMap<PluginContext, Map<string, Promise<KanbanBoard>>>()

function boardRequestFor(context: PluginContext, profile: string): Promise<KanbanBoard> | null {
  return boardRequests.get(context)?.get(profile) ?? null
}

function requestBoard(context: PluginContext, profile: string): Promise<KanbanBoard> {
  const existing = boardRequestFor(context, profile)

  if (existing) {
    return existing
  }

  const requestsForContext = boardRequests.get(context) ?? new Map<string, Promise<KanbanBoard>>()

  const request = context.rest<KanbanBoard>('/board', { timeoutMs: 60_000 }).finally(() => {
    if (requestsForContext.get(profile) === request) {
      requestsForContext.delete(profile)
    }
  })

  requestsForContext.set(profile, request)
  boardRequests.set(context, requestsForContext)

  return request
}

function KanbanPage({ context }: { context: PluginContext }) {
  const profile = useValue(host.state.profile).trim() || 'default'
  const [board, setBoard] = React.useState<KanbanBoard | null>(null)
  const [error, setError] = React.useState<string | null>(null)
  const [loading, setLoading] = React.useState(true)
  const autoRefreshTimer = React.useRef<ReturnType<typeof setTimeout> | null>(null)
  const requestGeneration = React.useRef(0)

  const load = React.useCallback(async (showLoading = true) => {
    const generation = ++requestGeneration.current

    if (showLoading) {
      setLoading(true)
    }

    setError(null)

    try {
      // Shared boards can exceed the desktop bridge's generic 15 s timeout.
      const value = await requestBoard(context, profile)

      if (requestGeneration.current === generation) {
        setBoard(value)
      }
    } catch (reason) {
      if (requestGeneration.current === generation) {
        setError(reason instanceof Error ? reason.message : String(reason))
      }
    } finally {
      if (showLoading && requestGeneration.current === generation) {
        setLoading(false)
      }
    }
  }, [context, profile])

  React.useEffect(() => {
    void load()

    const disposeSocket = context.socket('/events', () => {
      if (boardRequestFor(context, profile) || autoRefreshTimer.current) {
        return
      }

      autoRefreshTimer.current = setTimeout(() => {
        autoRefreshTimer.current = null
        void load(false)
      }, AUTO_REFRESH_DELAY_MS)
    })

    return () => {
      requestGeneration.current += 1
      disposeSocket()

      if (autoRefreshTimer.current) {
        clearTimeout(autoRefreshTimer.current)
        autoRefreshTimer.current = null
      }
    }
  }, [context, load, profile])

  if (loading && !board) {
    return (
      <div className="grid h-full place-items-center p-8">
        <Loader aria-label="Loading Kanban" className="size-6 text-(--ui-text-secondary)" />
      </div>
    )
  }

  if (error && !board) {
    return (
      <div className="grid h-full place-items-center p-8">
        <ErrorState description={error} title="Kanban could not be loaded">
          <Button onClick={() => void load()} size="sm" type="button">
            Retry
          </Button>
        </ErrorState>
      </div>
    )
  }

  return (
    <div className="flex h-full min-h-0 flex-col gap-4 overflow-auto p-4">
      <div className="flex items-center gap-3">
        <div className="min-w-0">
          <h1 className="text-lg font-semibold">Kanban</h1>
          <p className="text-xs text-(--ui-text-secondary)">Tasks from the active Hermes profile</p>
        </div>
        <Button
          className="ml-auto"
          disabled={loading}
          onClick={() => {
            if (autoRefreshTimer.current) {
              clearTimeout(autoRefreshTimer.current)
              autoRefreshTimer.current = null
            }

            void load()
          }}
          size="sm"
          type="button"
          variant="outline"
        >
          {loading ? 'Refreshing…' : 'Refresh'}
        </Button>
      </div>

      {error && <div className="text-xs text-destructive">Refresh failed: {error}</div>}

      <div className="grid min-h-0 auto-cols-[minmax(14rem,1fr)] grid-flow-col gap-3 overflow-x-auto pb-2">
        {(board?.columns ?? []).map(column => (
          <section className="min-h-40 rounded-md border border-(--ui-stroke-tertiary) bg-(--ui-bg-secondary) p-3" key={column.name}>
            <div className="mb-3 flex items-center justify-between gap-2">
              <h2 className="text-sm font-medium capitalize">{column.name}</h2>
              <Badge size="xs" variant="muted">
                {column.tasks.length}
              </Badge>
            </div>
            <div className="grid gap-2">
              {column.tasks.map(task => (
                <article className={cn('rounded border border-(--ui-stroke-tertiary) bg-(--ui-bg-primary) p-2')} key={task.id}>
                  <div className="text-xs font-medium">{task.title || task.id}</div>
                  {task.assignee && <div className="mt-1 text-[0.68rem] text-(--ui-text-secondary)">{task.assignee}</div>}
                </article>
              ))}
              {column.tasks.length === 0 && <div className="text-xs text-(--ui-text-secondary)">No tasks</div>}
            </div>
          </section>
        ))}
      </div>
    </div>
  )
}

const kanbanPlugin: HermesPlugin = {
  id: 'kanban',
  name: 'Kanban',
  register(context) {
    context.register({
      area: 'routes',
      data: { path: '/kanban' },
      id: 'route',
      render: () => <KanbanPage context={context} />,
      title: 'Kanban'
    })
    context.register({
      area: 'sidebar.nav',
      data: { codicon: 'package', label: 'Kanban', path: '/kanban', position: 'after:skills' },
      id: 'nav',
      order: 500
    })
  }
}

export default kanbanPlugin
