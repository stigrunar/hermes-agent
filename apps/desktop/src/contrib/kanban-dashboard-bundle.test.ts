import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

const here = dirname(fileURLToPath(import.meta.url))
const repoRoot = resolve(here, '../../../..')
const bundlePath = resolve(repoRoot, 'plugins/kanban/dashboard/dist/index.js')
const stylePath = resolve(repoRoot, 'plugins/kanban/dashboard/dist/style.css')

describe('Kanban dashboard desktop bundle contract', () => {
  it('reads desktop board selection with legacy default migration and per-profile fallback semantics', () => {
    const bundle = readFileSync(bundlePath, 'utf8')

    expect(bundle).toContain('hermes.kanban.selectedBoard.profile.')
    expect(bundle).toContain('isLegacyDefaultProfileContext')
    expect(bundle).toContain('window.localStorage.getItem(profileStorageKey(ctx))')
    expect(bundle).toContain('!ctx || isLegacyDefaultProfileContext(ctx)')
    expect(bundle).toContain('window.localStorage.getItem(LS_BOARD_KEY)')
    expect(bundle).toContain('preferredBoardForProfile(desktopProfile, boards, data.current)')
  })

  it('preserves the browser dashboard global board key when no Desktop profile SDK exists', () => {
    const bundle = readFileSync(bundlePath, 'utf8')

    expect(bundle).toContain('const desktopProfile = SDK.useDesktopProfile ? SDK.useDesktopProfile() : null')
    expect(bundle).toContain('const key = ctx ? profileStorageKey(ctx) : LS_BOARD_KEY')
    expect(bundle).toContain('? "__web__"')
  })

  it('uses SearchField for board search with Input fallback when host SDK omits SearchField', () => {
    const bundle = readFileSync(bundlePath, 'utf8')

    expect(bundle).toContain('SDK.components.SearchField || function')
    expect(bundle).toContain('h(SearchField, {')
    expect(bundle).toContain('"aria-label": searchLabel')
    expect(bundle).toContain('onChange: props.setSearch')
  })

  it('defaults the assignee filter from a representable Desktop profile and preserves explicit all-profile clearing', () => {
    const bundle = readFileSync(bundlePath, 'utf8')

    expect(bundle).toContain('SDK.useDesktopProfile')
    expect(bundle).toContain('function profileAssignee(ctx, assignees)')
    expect(bundle).toContain('if (!ctx || ctx.allProfiles) return ""')
    expect(bundle).toContain('assigneeExplicitRef.current = true')
  })

  it('persists Kanban view filters per Desktop profile without using the backend current-board state', () => {
    const bundle = readFileSync(bundlePath, 'utf8')

    expect(bundle).toContain('const LS_UI_PROFILE_PREFIX = "hermes.kanban.uiState.profile."')
    expect(bundle).toContain('function profileUiStorageKey(ctx)')
    expect(bundle).toContain('normalizeProfileKey(ctx.profileScope || ctx.activeProfile)')
    expect(bundle).toContain('normalizeProfileKey(desktopProfile.profileScope || desktopProfile.activeProfile)')
    expect(bundle).not.toContain('normalizeProfileKey(ctx.activeProfile || ctx.profileScope)')
    expect(bundle).toContain('if (ctx.allProfiles) return LS_UI_PROFILE_PREFIX + "__all__"')
    expect(bundle).toContain('function readProfileUiState(ctx)')
    expect(bundle).toContain('function writeProfileUiState(ctx, state)')
    expect(bundle).toContain('const persistedUiProfileKeyRef = useRef(profileKey)')
    expect(bundle).toContain('if (persistedUiProfileKeyRef.current !== profileKey)')
    expect(bundle).toContain('tenantFilter: state.tenantFilter || ""')
    expect(bundle).toContain('includeArchived: state.includeArchived === true')
    expect(bundle).toContain('laneByProfile: state.laneByProfile !== false')
    expect(bundle).not.toContain('current-board')
  })

  it('uses canonical page gutters with compact route-pane insets', () => {
    const style = readFileSync(stylePath, 'utf8')

    expect(style).toContain('padding: 1rem clamp(1.25rem, 4vw, 4rem) 1.25rem')
    expect(style).toContain('@container (max-width: 40rem)')
    expect(style).toContain('padding-inline: 0.75rem')
  })

  it('uses an opaque Desktop semantic surface and restrained Sheet-style shade for the task drawer', () => {
    const style = readFileSync(stylePath, 'utf8')
    const shadeStart = style.indexOf('.hermes-kanban-drawer-shade')
    const drawerStart = style.indexOf('.hermes-kanban-drawer', shadeStart + 1)
    const animationStart = style.indexOf('@keyframes hermes-kanban-drawer-in', drawerStart)
    const shade = style.slice(shadeStart, drawerStart)
    const drawer = style.slice(drawerStart, animationStart)

    expect(shadeStart).toBeGreaterThan(-1)
    expect(drawerStart).toBeGreaterThan(shadeStart)
    expect(animationStart).toBeGreaterThan(drawerStart)
    expect(shade).toContain('background: color-mix(in srgb, var(--ui-bg-editor, #000) 55%, transparent)')
    expect(shade).toContain('backdrop-filter: blur(0.125rem)')
    expect(shade).toContain('-webkit-backdrop-filter: blur(0.125rem)')
    expect(drawer).toContain('background: var(--ui-sidebar-surface-background, var(--color-card, Canvas))')
    expect(drawer).toContain('color: var(--ui-text-primary, var(--color-foreground, CanvasText))')
    expect(drawer).toContain('var(--stroke-nous, var(--ui-stroke-secondary')
    expect(drawer).toContain('box-shadow: var(--shadow-nous,')
    expect(drawer).not.toContain('background: var(--color-card);')
  })

  it('reserves semantic Desktop titlebar clearance while preserving Web and mobile fallbacks', () => {
    const style = readFileSync(stylePath, 'utf8')
    const headStart = style.indexOf('.hermes-kanban-drawer-head')
    const closeStart = style.indexOf('.hermes-kanban-drawer-close', headStart)
    const drawerHead = style.slice(headStart, closeStart)

    expect(headStart).toBeGreaterThan(-1)
    expect(closeStart).toBeGreaterThan(headStart)
    expect(drawerHead).toContain('var(--dashboard-plugin-titlebar-height, 0px)')
    expect(drawerHead).toContain('env(safe-area-inset-top)')
    expect(drawerHead).toContain('@media (max-width: 1023px)')
    expect(drawerHead).toContain('padding-top: calc(3.5rem + env(safe-area-inset-top))')
    const desktopShade = style.slice(style.indexOf('.dashboard-plugin-page .hermes-kanban-drawer-shade'))
    expect(desktopShade).toContain('position: fixed')
    expect(style).toContain('.dashboard-plugin-page .hermes-kanban-drawer-head')
    expect(drawerHead).not.toContain('34px')
  })

  it('exposes the task drawer as a named modal dialog with focus and close affordances', () => {
    const bundle = readFileSync(bundlePath, 'utf8')
    const style = readFileSync(stylePath, 'utf8')
    const start = bundle.indexOf('function TaskDrawer(props)')
    const end = bundle.indexOf('function _fmtBytes', start)
    const drawer = bundle.slice(start, end)

    expect(start).toBeGreaterThan(-1)
    expect(end).toBeGreaterThan(start)
    expect(drawer).toContain('role: "dialog"')
    expect(drawer).toContain('"aria-modal": "true"')
    expect(drawer).toContain('"aria-labelledby": titleId')
    expect(drawer).toContain('"aria-describedby": descriptionId')
    expect(drawer).toContain('"aria-label": tx(t, "closeTaskDrawer", "Close task drawer")')
    expect(drawer).toContain('if (e.key === "Escape" && !editing) props.onClose()')
    expect(drawer).toContain('previousFocusRef.current = document.activeElement')
    expect(drawer).toContain('drawerRef.current.focus')
    expect(drawer).toContain('previous.focus()')
    expect(style).toContain('.hermes-kanban-sr-only')
  })

  it('routes new Kanban helper copy through tx fallbacks instead of hardcoded English titles', () => {
    const bundle = readFileSync(bundlePath, 'utf8')

    expect(bundle).toContain('tx(t, "searchHelp", "Fuzzy-match tasks by id, title, or description. Matches across all columns.")')
    expect(bundle).toContain('tx(t, "specifierHelp", "Hermes profile that will spec this task')
    expect(bundle).toContain('tx(t, "assigneeCreateHelp", "Hermes profile to assign.')
    expect(bundle).toContain('tx(t, "priorityHelp", "Priority. Higher-priority tasks are claimed first')
    expect(bundle).toContain('tx(t, "skillsHelp", "Force-load these skills into the worker')
    expect(bundle).toContain('tx(t, "workspaceKindHelp", "Choose whether task files are temporary')
    expect(bundle).toContain('tx(t, "parentTaskHelp", "Optional parent task.')
    expect(bundle).toContain('tx(t, "goalModeHelp", "Goal mode:')
    expect(bundle).toContain('tx(t, "goalMaxTurnsHelp", "Turn budget for the goal loop.')
  })

  it('keeps task metadata flat instead of nesting a card inside the drawer', () => {
    const style = readFileSync(stylePath, 'utf8')
    const metaStart = style.indexOf('.hermes-kanban-drawer-meta')
    const rowStart = style.indexOf('.hermes-kanban-meta-row', metaStart)
    const metadata = style.slice(metaStart, rowStart)

    expect(metaStart).toBeGreaterThan(-1)
    expect(rowStart).toBeGreaterThan(metaStart)
    expect(metadata).toContain('gap: 0.25rem')
    expect(metadata).not.toContain('background:')
    expect(metadata).not.toContain('border:')
    expect(metadata).not.toContain('border-radius:')
  })

  it('uses host button variants for status intent and home subscription state', () => {
    const bundle = readFileSync(bundlePath, 'utf8')
    const style = readFileSync(stylePath, 'utf8')
    const actionsStart = bundle.indexOf('function StatusActions(props)')
    const homeStart = bundle.indexOf('function HomeSubsSection(props)', actionsStart)
    const registerStart = bundle.indexOf('// Register', homeStart)
    const actions = bundle.slice(actionsStart, homeStart)
    const homeSubscriptions = bundle.slice(homeStart, registerStart)

    expect(actionsStart).toBeGreaterThan(-1)
    expect(homeStart).toBeGreaterThan(actionsStart)
    expect(registerStart).toBeGreaterThan(homeStart)
    expect(actions).toContain('const b = function (label, patch, enabled, variant, confirmMsg)')
    expect(actions).toContain('variant: variant')
    expect(actions).toContain('variant: "default"')
    expect(actions).toContain('variant: "secondary"')
    expect(actions).toMatch(/b\("→ triage",[\s\S]*?"secondary"\)/)
    expect(actions).toMatch(/b\("→ ready",[\s\S]*?"secondary"\)/)
    expect(actions).toMatch(/tx\(t, "block", "Block"\)[\s\S]*?"destructive"/)
    expect(actions).toMatch(/tx\(t, "unblock", "Unblock"\)[\s\S]*?"secondary"/)
    expect(actions).toMatch(/tx\(t, "complete", "Complete"\)[\s\S]*?"default"/)
    expect(actions).toMatch(/tx\(t, "archive", "Archive"\)[\s\S]*?"outline"/)
    expect(homeSubscriptions).toContain('variant: hc.subscribed ? "secondary" : "outline"')
    expect(homeSubscriptions).toContain('className: "hermes-kanban-home-sub"')
    expect(homeSubscriptions).not.toContain('hermes-kanban-home-sub--on')
    expect(style).not.toContain('.hermes-kanban-home-sub--on')
  })

  it('uses host dialog and form primitives for new tasks without custom modal chrome', () => {
    const bundle = readFileSync(bundlePath, 'utf8')
    const start = bundle.indexOf('function InlineCreate(props)')
    const end = bundle.indexOf('// Task drawer', start)
    const inlineCreate = bundle.slice(start, end)

    expect(start).toBeGreaterThan(-1)
    expect(end).toBeGreaterThan(start)
    expect(inlineCreate).toContain('h(Dialog, {')
    expect(inlineCreate).toContain('open: true')
    expect(inlineCreate).toContain('if (!open) props.onCancel()')
    expect(inlineCreate).toContain('h(DialogContent, { className: "hermes-kanban-create-dialog" }')
    expect(inlineCreate).toContain('h(DialogHeader, null')
    expect(inlineCreate).toContain('h(DialogTitle, null')
    expect(inlineCreate).toContain('h(DialogFooter, null')
    expect(inlineCreate).toContain('h(Textarea, {')
    expect(inlineCreate).toContain('h(Checkbox, {')
    expect(inlineCreate).toContain('onCheckedChange: function (checked)')
    expect(inlineCreate).toContain('variant: "ghost"')
    expect(inlineCreate).toContain('disabled: !title.trim()')
    expect(inlineCreate).not.toContain('hermes-kanban-dialog-backdrop')
    expect(inlineCreate).not.toContain('h("textarea"')
    expect(inlineCreate).not.toContain('h("input"')
  })

  it('styles only new-task form layout with semantic responsive classes', () => {
    const style = readFileSync(stylePath, 'utf8')

    expect(style).toContain('.hermes-kanban-create-dialog')
    expect(style).toContain('.hermes-kanban-create-fields')
    expect(style).toContain('.hermes-kanban-create-two-column')
    expect(style).toContain('.hermes-kanban-create-grow')
    expect(style).toContain('.hermes-kanban-create-priority')
    expect(style).toContain('.hermes-kanban-create-workspace-row')
    expect(style).toContain('.hermes-kanban-create-goal-row')
    expect(style).toContain('.hermes-kanban-create-label')
    expect(style).toContain('.hermes-kanban-create-hint')
    expect(style).toContain('.hermes-kanban-create-advisory')
    expect(style).toContain('@media (max-width: 42rem)')
    expect(style).toContain('color: var(--ui-text-primary, var(--color-foreground))')
    expect(style).not.toContain('.hermes-kanban-create-dialog-backdrop')
  })
})
