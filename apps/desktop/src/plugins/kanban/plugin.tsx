import {
  Badge,
  Button,
  cn,
  ErrorState,
  Loader
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

function KanbanPage({ context }: { context: PluginContext }) {
  const [board, setBoard] = React.useState<KanbanBoard | null>(null)
  const [error, setError] = React.useState<string | null>(null)
  const [loading, setLoading] = React.useState(true)

  const load = React.useCallback(() => {
    setLoading(true)

    return context
      .rest<KanbanBoard>('/board')
      .then(value => {
        setBoard(value)
        setError(null)
      })
      .catch(reason => {
        setError(reason instanceof Error ? reason.message : String(reason))
      })
      .finally(() => setLoading(false))
  }, [context])

  React.useEffect(() => {
    void load()

    return context.socket('/events', () => void load())
  }, [context, load])

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
        <Button className="ml-auto" disabled={loading} onClick={() => void load()} size="sm" type="button" variant="outline">
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
