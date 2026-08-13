import type { useSensors } from '@dnd-kit/core'
import { useStore } from '@nanostores/react'
import type * as React from 'react'
import { useCallback, useId, useMemo, useState } from 'react'

import { SidebarPanelLabel } from '@/app/shell/sidebar-label'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog'
import { DisclosureCaret } from '@/components/ui/disclosure-caret'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { SidebarGroup, SidebarGroupContent } from '@/components/ui/sidebar'
import { Tip } from '@/components/ui/tooltip'
import type { HermesGitWorktree } from '@/global'
import type { ProjectInfo, SessionInfo } from '@/hermes'
import { useI18n } from '@/i18n'
import { flattenSessionsWithBranches } from '@/lib/session-branch-tree'
import {
  groupEntriesByRecency,
  groupEntriesByStatus,
  type SidebarListRow,
  toSessionRows
} from '@/lib/session-date-groups'
import { sessionBucketLabel } from '@/lib/time'
import { cn } from '@/lib/utils'
import { bindConversationToProject, unbindConversationFromProject } from '@/store/projects'
import { sessionPinId } from '@/store/session'
import { $sessionDotStateById, hasLiveTurn } from '@/store/session-dot-state'

import { SidebarDateDivider, SidebarSectionMeta } from './chrome'
import type { MessagingConversationGroup, MessagingTopicGroup } from './messaging-groups'
import { orderRowsWithinGroups, reorderableRowIds } from './order'
import {
  EnteredProjectContent,
  ProjectOverviewRow,
  type SidebarProjectTree,
  type SidebarSessionGroup,
  SidebarWorkspaceGroup,
  type SidebarWorkspaceTree
} from './projects'
import { WorkspaceAddButton } from './projects/workspace-header'
import { ReorderableList, useSortableBindings } from './reorderable-list'
import { SidebarSessionSkeletons } from './section-states'
import { SidebarSessionRow } from './session-row'
import { VirtualSessionList } from './virtual-session-list'

export const VIRTUALIZE_THRESHOLD = 25

interface SidebarSectionHeaderProps {
  label: string
  open: boolean
  onToggle: () => void
  action?: React.ReactNode
  meta?: React.ReactNode
  icon?: React.ReactNode
  // When false the section can't be collapsed: the label renders static (no
  // toggle, no caret) and the section is always open. Used for the single-
  // project view, where collapsing one project makes no sense.
  collapsible?: boolean
}

function SidebarSectionHeader({
  label,
  open,
  onToggle,
  action,
  meta,
  icon,
  collapsible = true
}: SidebarSectionHeaderProps) {
  const labelBody = (
    <>
      {icon}
      <SidebarPanelLabel>{label}</SidebarPanelLabel>
      {meta && <SidebarSectionMeta>{meta}</SidebarSectionMeta>}
    </>
  )

  return (
    <div className="group/section flex shrink-0 items-center justify-between gap-1 pb-1 pt-1.5">
      {collapsible ? (
        <button
          // min-w-0 lets the label truncate at narrow sidebar widths instead of
          // pushing the header's trailing action icons out of view.
          className="group/section-label flex w-fit min-w-0 items-center gap-1 bg-transparent text-left leading-none"
          onClick={onToggle}
          type="button"
        >
          {labelBody}
          <DisclosureCaret
            className="text-(--ui-text-tertiary) opacity-0 transition group-hover/section-label:opacity-100"
            open={open}
          />
        </button>
      ) : (
        <div className="flex w-fit min-w-0 items-center gap-1 leading-none">{labelBody}</div>
      )}
      {action}
    </div>
  )
}

interface SidebarSessionsSectionProps {
  label: string
  open: boolean
  onToggle: () => void
  sessions: SessionInfo[]
  activeSessionId: null | string
  onResumeSession: (sessionId: string) => void
  onDeleteSession: (sessionId: string) => void
  onArchiveSession: (sessionId: string) => void
  onBranchSession?: (sessionId: string, profile?: string) => void
  onTogglePin: (sessionId: string) => void
  onNewSessionInWorkspace?: (path: null | string) => void
  pinned: boolean
  rootClassName?: string
  contentClassName?: string
  emptyState: React.ReactNode
  forceEmptyState?: boolean
  headerAction?: React.ReactNode
  footer?: React.ReactNode
  groups?: SidebarSessionGroup[]
  messagingConversations?: MessagingConversationGroup[]
  projects?: ProjectInfo[]
  tree?: SidebarWorkspaceTree[]
  // Project overview: when present, render a drill-in list of project rows
  // instead of sessions. Clicking a row enters that project (onEnterProject),
  // which then passes `projectContent` on the next render. Takes precedence
  // over `tree` / `groups`.
  projectOverview?: SidebarProjectTree[]
  // Per-project preview rows (from the backend tree), keyed by project id.
  projectOverviewPreviews?: Record<string, SessionInfo[]>
  // True while the backend project tree is loading (overview skeleton).
  projectsLoading?: boolean
  onEnterProject?: (id: string) => void
  // The entered project's flattened content: main-checkout sessions render
  // directly (no redundant repo/branch header); only linked worktrees nest.
  projectContent?: SidebarProjectTree
  // Live git lanes (`git worktree list`) for repos in the entered project —
  // a VISUAL enhancer only (empty lanes), never session membership.
  projectRepoWorktrees?: Record<string, HermesGitWorktree[]>
  // Live session cache used for optimistic placement inside entered-project lanes.
  liveSessions?: SessionInfo[]
  // Client-side optimistic eviction layer (deleted/archived ids).
  removedSessionIds?: ReadonlySet<string>
  activeProjectId?: null | string
  labelMeta?: React.ReactNode
  labelIcon?: React.ReactNode
  // When false the section header is static (no caret/toggle) and always open.
  collapsible?: boolean
  sortable?: boolean
  // The persisted drag order, applied WITHIN each date group (see
  // orderRowsWithinGroups). Chronology decides the groups; this decides the
  // sequence inside one, so a reorder no longer costs the whole list its
  // dividers. Pinned passes nothing — its rows arrive in pin order already.
  manualOrderIds?: string[]
  // The flat session list is the only hand-reorderable surface (grouped/project
  // views sort deterministically), so it owns the one ReorderableList.
  onReorderSessions?: (ids: string[]) => void
  // Drag-to-reorder for the project overview list (top-level projects).
  onReorderProjects?: (ids: string[]) => void
  // Rendered atop the entered-project body (a "back to overview" row).
  projectBackRow?: React.ReactNode
  dndSensors?: ReturnType<typeof useSensors>
  // Tag every row with its owning profile. Set on the flat cross-profile
  // lists (Pinned / search results) in the All-profiles view, where no group
  // header communicates ownership (#66003).
  showProfileTags?: boolean
  // Which dividers to fold into the flat list: `date` gives the chronological
  // "Yesterday" / "Last week" separators (flat recents + entered-project lanes),
  // `status` splits into WORKING / DONE under the same separators. `none` for
  // pinned, messaging groups, and the project overview, where the order isn't
  // strictly by recency so a bucket would be misleading.
  grouping?: 'date' | 'none' | 'status'
  // Inbox style: render every flat session row as a three-line card (project ·
  // age / title / model · size). A render variant that composes with whichever
  // grouping is active — the flat recents list opts in; dense tree surfaces
  // (pinned, projects, messaging) keep the one-line row.
  card?: boolean
}

export function SidebarSessionsSection({
  label,
  open,
  onToggle,
  sessions,
  activeSessionId,
  onResumeSession,
  onDeleteSession,
  onArchiveSession,
  onBranchSession,
  onTogglePin,
  onNewSessionInWorkspace,
  pinned,
  rootClassName,
  contentClassName,
  emptyState,
  forceEmptyState = false,
  headerAction,
  footer,
  groups,
  messagingConversations,
  projects = [],
  projectOverview,
  projectOverviewPreviews,
  projectsLoading = false,
  onEnterProject,
  projectContent,
  projectRepoWorktrees,
  liveSessions,
  removedSessionIds,
  activeProjectId,
  labelMeta,
  labelIcon,
  collapsible = true,
  sortable = false,
  manualOrderIds,
  onReorderSessions,
  onReorderProjects,
  projectBackRow,
  dndSensors,
  showProfileTags = false,
  grouping = 'none',
  card = false
}: SidebarSessionsSectionProps) {
  const { t } = useI18n()
  const dividerLabels = t.sidebar.dateDivider
  const statusDividerLabels = t.sidebar.statusDivider
  const dotStates = useStore($sessionDotStateById)
  const sectionOpen = collapsible ? open : true
  const hasGroupedSessions = Boolean(groups?.some(group => group.sessions.length > 0))

  const hasMessagingConversations = Boolean(
    messagingConversations?.some(conversation => conversation.topics.some(topic => topic.sessions.length > 0))
  )

  // A defined project list is itself content (even an empty project should
  // render as a drill-in row so the user can see it exists).
  const hasProjectOverview = Boolean(projectOverview?.length)

  // Lanes count as content even with no rows left in them: the backend only
  // emits a lane that has sessions, so a lane surviving with zero rows means
  // they were filtered out (pinned) — the branch is real and must still render.
  // A genuinely empty project has no lanes at all and keeps its empty state.
  const hasProjectContent = Boolean(
    projectContent && (projectContent.sessionCount > 0 || projectContent.repos.some(repo => repo.groups.length > 0))
  )

  const showEmptyState =
    forceEmptyState ||
    (!hasGroupedSessions &&
      !hasMessagingConversations &&
      !hasProjectOverview &&
      !hasProjectContent &&
      sessions.length === 0)

  // The flat recents/pinned list is the only place sessions reorder by hand;
  // grouped/tree views always sort by creation date and never drag.
  const sessionsDraggable = sortable && !!onReorderSessions

  // Only Pinned arrives pre-ordered as a flat sequence. Recents keeps its
  // recency sort — the drag order is layered on per date group below, so the
  // buckets stay truthful and a reorder never costs the list its dividers.
  const displayEntries = useMemo(
    () => flattenSessionsWithBranches(sessions, { preserveOrder: pinned }),
    [sessions, pinned]
  )

  const renderRow = useCallback(
    (session: SessionInfo, draggable: boolean, branchStem?: string) => {
      const rowProps = {
        branchStem,
        card,
        isPinned: pinned,
        isSelected: session.id === activeSessionId,
        onArchive: () => onArchiveSession(session.id),
        onBranch: onBranchSession ? () => onBranchSession(session.id, session.profile) : undefined,
        onDelete: () => onDeleteSession(session.id),
        onPin: () => onTogglePin(sessionPinId(session)),
        onResume: () => onResumeSession(session.id),
        reorderable: draggable && !branchStem,
        session,
        showProfile: showProfileTags
      }

      return draggable && !branchStem ? (
        <SortableSidebarSessionRow key={session.id} {...rowProps} />
      ) : (
        <SidebarSessionRow key={session.id} {...rowProps} />
      )
    },
    [
      activeSessionId,
      card,
      onArchiveSession,
      onBranchSession,
      onDeleteSession,
      onResumeSession,
      onTogglePin,
      pinned,
      showProfileTags
    ]
  )

  // Date dividers head a group the same way a repo header does, so they carry
  // the same hover-revealed "+". Only for dates: "new session in WORKING" is
  // not a thing.
  const dividerAction =
    grouping === 'date' && onNewSessionInWorkspace ? (
      <WorkspaceAddButton label={t.sidebar.nav['new-session']} onClick={() => onNewSessionInWorkspace(null)} />
    ) : null

  // A single flat/virtual/lane list row — either a divider or a session.
  const renderListRow = useCallback(
    (row: SidebarListRow, draggable: boolean, action?: React.ReactNode) => {
      if (row.kind === 'session') {
        return renderRow(row.entry.session, draggable, row.entry.branchStem)
      }

      return (
        <SidebarDateDivider
          action={action}
          key={row.key}
          label={'label' in row ? row.label : sessionBucketLabel(row.bucket, dividerLabels)}
        />
      )
    },
    [dividerLabels, renderRow]
  )

  // Sessions inside repos/worktrees are date-ordered and static.
  const renderRows = useCallback(
    (items: SessionInfo[]) =>
      flattenSessionsWithBranches(items).map(({ branchStem, session }) => renderRow(session, false, branchStem)),
    [renderRow]
  )

  // Same as `renderRows`, but with date dividers folded in — used for
  // entered-project lanes so a lane spanning multiple days reads
  // chronologically, matching the flat recents list.
  const renderRowsDated = useCallback(
    (items: SessionInfo[]) => {
      const entries = flattenSessionsWithBranches(items)

      return (grouping === 'date' ? groupEntriesByRecency(entries) : toSessionRows(entries)).map(row =>
        renderListRow(row, false)
      )
    },
    [grouping, renderListRow]
  )

  // Flat recents as list rows: grouped by recency when enabled, plain otherwise.
  // The hand-picked order is then applied INSIDE each date group, so dragging a
  // row ranks it among its own day's chats instead of freezing the whole list
  // into an undated manual mode.
  const flatRows: SidebarListRow[] = useMemo(() => {
    const rows =
      grouping === 'date'
        ? groupEntriesByRecency(displayEntries)
        : grouping === 'status'
          ? groupEntriesByStatus(
              displayEntries,
              entry => hasLiveTurn(dotStates[entry.session.id] ?? 'idle'),
              statusDividerLabels
            )
          : toSessionRows(displayEntries)

    return manualOrderIds?.length ? orderRowsWithinGroups(rows, manualOrderIds) : rows
  }, [grouping, displayEntries, dotStates, manualOrderIds, statusDividerLabels])

  // dnd-kit must see exactly the ids it renders, in render order: the sortable
  // set is derived from the rows, not from `sessions`. Feeding it the unrendered
  // session order made a drop compute its target index against a list the user
  // wasn't looking at — the drag that landed a row in the wrong slot.
  const sortableRowIds = useMemo(() => reorderableRowIds(flatRows), [flatRows])

  // Pinned never virtualizes. Virtualization needs a bounded viewport to
  // measure against, and Pinned deliberately has none — however many chats you
  // pin, all of them render and the sidebar's own scroll carries the length.
  const flatVirtualized =
    !pinned &&
    !showEmptyState &&
    !groups?.length &&
    !messagingConversations?.length &&
    !projectOverview?.length &&
    !projectContent &&
    sessions.length >= VIRTUALIZE_THRESHOLD

  // First paint into the grouped view (e.g. the app restoring the Projects tab)
  // has flat recents in `sessions` but no tree yet. Show skeletons rather than
  // flashing the flat session list until the overview/content/groups resolve. A
  // background refresh keeps the prior tree, so this only fires when empty.
  const showProjectsSkeleton =
    projectsLoading &&
    !hasProjectOverview &&
    !hasProjectContent &&
    !projectContent &&
    !groups?.length &&
    !messagingConversations?.length

  let inner: React.ReactNode

  if (showProjectsSkeleton) {
    inner = <SidebarSessionSkeletons />
  } else if (projectContent) {
    // Entered a project: the back row is always present, then either the
    // (overlay-aware) content or a clean empty state — never a bare spinner or a
    // blank pane while lanes hydrate.
    inner = (
      <>
        {projectBackRow}
        {hasProjectContent ? (
          <EnteredProjectContent
            liveSessions={liveSessions}
            onNewSession={onNewSessionInWorkspace}
            project={projectContent}
            removedSessionIds={removedSessionIds}
            renderRows={renderRowsDated}
            repoWorktrees={projectRepoWorktrees}
          />
        ) : (
          emptyState
        )}
      </>
    )
  } else if (showEmptyState) {
    inner = emptyState
  } else if (projectOverview?.length) {
    // The model is already ordered (Home leads; then the default sort groups
    // explicit-before-auto, with a manual drag-order winning when present).
    // Render in that order and make rows drag-to-reorder when a handler is
    // wired — Home stays outside the sortable list, it's a fixture.
    const home = projectOverview[0]?.isNoProject ? projectOverview[0] : undefined
    const sortableProjects = home ? projectOverview.slice(1) : projectOverview
    const projectsDraggable = sortableProjects.length > 1 && !!onReorderProjects
    const Row = projectsDraggable ? SortableProjectOverviewRow : ProjectOverviewRow

    const projectRow = (project: SidebarProjectTree, Component: typeof ProjectOverviewRow) => (
      <Component
        activeProjectId={activeProjectId}
        key={project.id}
        onEnter={onEnterProject}
        onNewSession={onNewSessionInWorkspace}
        previewSessions={projectOverviewPreviews?.[project.id]}
        project={project}
        renderRows={renderRows}
      />
    )

    const rows = sortableProjects.map(project => projectRow(project, Row))

    inner = (
      <>
        {home && projectRow(home, ProjectOverviewRow)}
        {projectsDraggable && onReorderProjects ? (
          <ReorderableList
            ids={sortableProjects.map(project => project.id)}
            onReorder={onReorderProjects}
            sensors={dndSensors}
          >
            {rows}
          </ReorderableList>
        ) : (
          rows
        )}
      </>
    )
  } else if (messagingConversations?.length) {
    inner = (
      <>
        {messagingConversations.map(conversation => (
          <MessagingConversationTree
            conversation={conversation}
            key={conversation.id}
            projects={projects}
            renderRows={renderRows}
          />
        ))}
        {renderRows(sessions)}
      </>
    )
  } else if (groups?.length) {
    // Profile/source groups never reorder; render them flat with static rows.
    inner = groups.map(group => (
      <SidebarWorkspaceGroup
        group={group}
        key={group.id}
        onNewSession={onNewSessionInWorkspace}
        renderRows={renderRows}
      />
    ))
  } else if (flatVirtualized) {
    const virtual = (
      <VirtualSessionList
        activeSessionId={activeSessionId}
        card={card}
        className={contentClassName}
        dividerAction={dividerAction}
        onArchiveSession={onArchiveSession}
        onBranchSession={onBranchSession}
        onDeleteSession={onDeleteSession}
        onResumeSession={onResumeSession}
        onTogglePin={onTogglePin}
        pinned={pinned}
        rows={flatRows}
        showProfileTags={showProfileTags}
        sortable={sessionsDraggable}
      />
    )

    inner =
      sessionsDraggable && onReorderSessions ? (
        <ReorderableList ids={sortableRowIds} onReorder={onReorderSessions} sensors={dndSensors}>
          {virtual}
        </ReorderableList>
      ) : (
        virtual
      )
  } else if (sessionsDraggable && onReorderSessions) {
    inner = (
      <ReorderableList ids={sortableRowIds} onReorder={onReorderSessions} sensors={dndSensors}>
        {flatRows.map(row => renderListRow(row, true, dividerAction))}
      </ReorderableList>
    )
  } else {
    inner = flatRows.map(row => renderListRow(row, false, dividerAction))
  }

  // The virtualizer owns its own scroller, so suppress the wrapper's overflow
  // to avoid a double scroll container. Both axes: `overflow-y-visible` next
  // to the inherited `overflow-x-hidden` computes to `auto` (CSS spec), which
  // kept a phantom 4px scrollbar gutter and cut every row short on the right.
  const resolvedContentClassName = cn(contentClassName, flatVirtualized && 'overflow-visible')

  return (
    <SidebarGroup className={rootClassName}>
      <SidebarSectionHeader
        action={headerAction}
        collapsible={collapsible}
        icon={labelIcon}
        label={label}
        meta={labelMeta}
        onToggle={onToggle}
        open={sectionOpen}
      />
      {sectionOpen && (
        <SidebarGroupContent className={resolvedContentClassName}>
          {inner}
          {footer}
        </SidebarGroupContent>
      )}
    </SidebarGroup>
  )
}

interface MessagingConversationTreeProps {
  conversation: MessagingConversationGroup
  projects: ProjectInfo[]
  renderRows: (sessions: SessionInfo[]) => React.ReactNode
}

function MessagingConversationTree({ conversation, projects, renderRows }: MessagingConversationTreeProps) {
  const [open, setOpen] = useState(true)
  const contentId = useId()

  return (
    <div className="grid gap-px">
      <button
        aria-controls={contentId}
        aria-expanded={open}
        className="group/conversation flex min-h-[1.625rem] min-w-0 items-center gap-1.5 rounded-md bg-transparent pl-2 pr-1 text-left"
        onClick={() => setOpen(value => !value)}
        type="button"
      >
        <Codicon className="shrink-0 text-(--ui-text-tertiary)" name="comment-discussion" size="0.75rem" />
        <span className="min-w-0 flex-1 truncate text-[0.8125rem] leading-none text-(--ui-text-secondary)">
          {conversation.label}
        </span>
        <DisclosureCaret
          className="text-(--ui-text-tertiary) opacity-0 transition group-hover/conversation:opacity-100"
          open={open}
        />
      </button>
      {open ? (
        <div className="grid gap-px pl-3" id={contentId}>
          {conversation.topics.map(topic => (
            <MessagingTopicTree key={topic.id} projects={projects} renderRows={renderRows} topic={topic} />
          ))}
        </div>
      ) : null}
    </div>
  )
}

interface MessagingTopicTreeProps {
  projects: ProjectInfo[]
  renderRows: (sessions: SessionInfo[]) => React.ReactNode
  topic: MessagingTopicGroup
}

function MessagingTopicTree({ projects, renderRows, topic }: MessagingTopicTreeProps) {
  const { t } = useI18n()
  const strings = t.sidebar.projects
  const [open, setOpen] = useState(true)
  const contentId = useId()
  const [dialogOpen, setDialogOpen] = useState(false)
  const firstActiveProject = projects.find(project => !project.archived)?.id ?? ''
  const [projectId, setProjectId] = useState(topic.binding?.project_id ?? firstActiveProject)
  const [alias, setAlias] = useState(topic.binding?.alias ?? topic.label)
  const [saving, setSaving] = useState(false)
  const projectName = projects.find(project => project.id === topic.binding?.project_id)?.name
  const actionLabel = topic.binding ? strings.topicManageAction : strings.topicBindAction

  const openDialog = () => {
    setProjectId(topic.binding?.project_id ?? firstActiveProject)
    setAlias(topic.binding?.alias ?? topic.label)
    setDialogOpen(true)
  }

  const save = async () => {
    if (!projectId) {
      return
    }

    setSaving(true)

    try {
      await bindConversationToProject({
        alias,
        projectId,
        targetRef: topic.identity.targetRef
      })
      setDialogOpen(false)
    } finally {
      setSaving(false)
    }
  }

  const unlink = async () => {
    setSaving(true)

    try {
      await unbindConversationFromProject({
        projectId: topic.binding?.project_id,
        targetRef: topic.identity.targetRef
      })
      setDialogOpen(false)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="grid gap-px">
      <div className="group/topic grid min-h-[1.625rem] grid-cols-[minmax(0,1fr)_auto] items-stretch rounded-md">
        <button
          aria-controls={contentId}
          aria-expanded={open}
          className="flex h-full min-w-0 items-center gap-1.5 bg-transparent pl-2 pr-1 text-left"
          onClick={() => setOpen(value => !value)}
          type="button"
        >
          <Codicon className="shrink-0 text-(--ui-text-tertiary)" name="comment" size="0.75rem" />
          <span className="min-w-0 flex-1 truncate text-[0.8125rem] leading-none text-(--ui-text-secondary)">
            {topic.label}
          </span>
          {projectName ? (
            <span className="max-w-20 truncate text-[0.6875rem] font-medium text-(--ui-text-quaternary)">
              {projectName}
            </span>
          ) : null}
          <DisclosureCaret
            className="text-(--ui-text-tertiary) opacity-0 transition group-hover/topic:opacity-100"
            open={open}
          />
        </button>
        {topic.canManageBinding ? (
          <Tip label={actionLabel}>
            <Button
              aria-label={actionLabel}
              className="self-center"
              onClick={openDialog}
              size="icon-xs"
              variant="ghost"
            >
              <Codicon name={topic.binding ? 'link' : 'link-external'} />
            </Button>
          </Tip>
        ) : null}
      </div>
      {open ? (
        <div className="grid gap-px pl-3" id={contentId}>
          {renderRows(topic.sessions)}
        </div>
      ) : null}
      <Dialog onOpenChange={setDialogOpen} open={topic.canManageBinding && dialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{actionLabel}</DialogTitle>
            <DialogDescription>{strings.topicProjectDescription}</DialogDescription>
          </DialogHeader>
          <div className="grid gap-3">
            <Select onValueChange={setProjectId} value={projectId}>
              <SelectTrigger aria-label={strings.topicProjectPlaceholder}>
                <SelectValue placeholder={strings.topicProjectPlaceholder} />
              </SelectTrigger>
              <SelectContent>
                {projects
                  .filter(project => !project.archived)
                  .map(project => (
                    <SelectItem key={project.id} value={project.id}>
                      {project.name}
                    </SelectItem>
                  ))}
              </SelectContent>
            </Select>
            <Input
              aria-label={strings.topicAliasPlaceholder}
              onChange={event => setAlias(event.target.value)}
              placeholder={strings.topicAliasPlaceholder}
              value={alias}
            />
          </div>
          <DialogFooter>
            {topic.binding ? (
              <Button disabled={saving} onClick={() => void unlink()} type="button" variant="text">
                {strings.topicUnbind}
              </Button>
            ) : null}
            <Button disabled={!projectId || saving} onClick={() => void save()} type="button">
              {topic.binding ? strings.topicSave : strings.topicBind}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

interface SortableSessionRowProps {
  session: SessionInfo
  isPinned: boolean
  isSelected: boolean
  onArchive: () => void
  onDelete: () => void
  onPin: () => void
  onResume: () => void
}

function SortableSidebarSessionRow(props: SortableSessionRowProps) {
  return <SidebarSessionRow {...props} {...useSortableBindings(props.session.id)} />
}

function SortableProjectOverviewRow(props: React.ComponentProps<typeof ProjectOverviewRow>) {
  return <ProjectOverviewRow {...props} {...useSortableBindings(props.project.id)} />
}
