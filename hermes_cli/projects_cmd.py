"""``hermes project`` CLI — manage first-class, multi-folder Projects.

A Project is a human-named workspace spanning one or more folders, with one
designated primary repo. Projects anchor desktop session grouping and (when
bound to a kanban board) give kanban tasks a deterministic worktree + branch
convention. State lives in the per-profile ``$HERMES_HOME/projects.db`` store
(see :mod:`hermes_cli.projects_db`).

This is a footprint-ladder rung-2 capability: a CLI command + gateway RPC,
with zero model-tool schema cost.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import sys
from pathlib import Path

from hermes_cli import projects_db as pdb


def build_parser(
    parent_subparsers: argparse._SubParsersAction,
) -> argparse.ArgumentParser:
    """Attach the ``project`` subcommand tree. Returns the top parser."""
    parser = parent_subparsers.add_parser(
        "project",
        help="Manage projects (named, multi-folder workspaces)",
        description=(
            "Projects are human-named workspaces that can span multiple "
            "folders / repos. They anchor desktop session grouping and, when "
            "bound to a kanban board, give tasks a deterministic worktree + "
            "branch convention. State is per-profile."
        ),
    )
    sub = parser.add_subparsers(dest="project_action")

    p_create = sub.add_parser("create", help="Create a new project")
    p_create.add_argument("name", help="Human name, e.g. 'Hermes Agent'")
    p_create.add_argument(
        "folders", nargs="*", help="Folder paths to include (first = primary)"
    )
    p_create.add_argument("--slug", default=None, help="Explicit slug override")
    p_create.add_argument(
        "--primary", default=None, metavar="PATH", help="Primary repo path"
    )
    p_create.add_argument("--description", default=None)
    p_create.add_argument("--icon", default=None)
    p_create.add_argument("--color", default=None)
    p_create.add_argument(
        "--board", default=None, metavar="SLUG", help="Bind a kanban board"
    )
    p_create.add_argument(
        "--use", action="store_true", help="Set as the active project"
    )

    p_list = sub.add_parser("list", aliases=["ls"], help="List projects")
    p_list.add_argument(
        "--all", action="store_true", dest="include_archived",
        help="Include archived projects",
    )

    p_show = sub.add_parser("show", help="Show a project's details")
    p_show.add_argument("project", help="Project id or slug")

    p_add = sub.add_parser("add-folder", help="Add a folder to a project")
    p_add.add_argument("project", help="Project id or slug")
    p_add.add_argument("path", help="Folder path")
    p_add.add_argument("--label", default=None)
    p_add.add_argument(
        "--primary", action="store_true", help="Mark as primary repo"
    )

    p_rm = sub.add_parser("remove-folder", help="Remove a folder from a project")
    p_rm.add_argument("project", help="Project id or slug")
    p_rm.add_argument("path", help="Folder path")

    p_rename = sub.add_parser("rename", help="Rename a project")
    p_rename.add_argument("project", help="Project id or slug")
    p_rename.add_argument("name", help="New name")

    p_primary = sub.add_parser("set-primary", help="Set the primary folder")
    p_primary.add_argument("project", help="Project id or slug")
    p_primary.add_argument("path", help="Folder path (must already be in project)")

    p_use = sub.add_parser("use", help="Set the active project")
    p_use.add_argument(
        "project", nargs="?", default=None,
        help="Project id or slug (omit to clear)",
    )

    p_archive = sub.add_parser("archive", help="Archive a project")
    p_archive.add_argument("project", help="Project id or slug")

    p_restore = sub.add_parser("restore", help="Restore an archived project")
    p_restore.add_argument("project", help="Project id or slug")

    p_bind = sub.add_parser("bind-board", help="Bind a kanban board to a project")
    p_bind.add_argument("project", help="Project id or slug")
    p_bind.add_argument(
        "board", nargs="?", default="", help="Board slug (omit to unbind)"
    )

    p_outcomes = sub.add_parser("outcomes", help="List current Outcomes for a project")
    p_outcomes.add_argument("project", help="Project id or slug")
    p_outcomes.add_argument("--all", action="store_true", dest="include_archived")

    p_outcome_create = sub.add_parser("outcome-create", help="Create a material Outcome")
    p_outcome_create.add_argument("project", help="Project id or slug")
    p_outcome_create.add_argument("outcome_key", help="Stable key, e.g. STAFFING-TEST-ENABLER-R1")
    p_outcome_create.add_argument("--name", default=None)
    p_outcome_create.add_argument("--state", default="planning")
    p_outcome_create.add_argument("--owner", default=None)
    p_outcome_create.add_argument("--base", default=None, dest="current_base_ref")
    p_outcome_create.add_argument("--next", default=None, dest="next_action")

    p_outcome_update = sub.add_parser("outcome-update", help="Update current Outcome projection")
    p_outcome_update.add_argument("project", help="Project id or slug")
    p_outcome_update.add_argument("outcome", help="Outcome id or key")
    p_outcome_update.add_argument("--name", default=None)
    p_outcome_update.add_argument("--state", default=None)
    p_outcome_update.add_argument("--owner", default=None)
    p_outcome_update.add_argument("--base", default=None, dest="current_base_ref")
    p_outcome_update.add_argument("--candidate", default=None, dest="current_candidate_ref")
    p_outcome_update.add_argument("--live", default=None, dest="current_live_ref")
    p_outcome_update.add_argument("--next", default=None, dest="next_action")
    p_outcome_update.add_argument("--archive", action="store_true")

    p_lane = sub.add_parser("bind-lane", help="Bind a conversation lane to Project/Outcome context")
    p_lane.add_argument("project", help="Project id or slug")
    p_lane.add_argument("--platform", required=True)
    p_lane.add_argument("--chat-id", required=True)
    p_lane.add_argument("--thread-id", default=None)
    p_lane.add_argument("--outcome", default=None)
    p_lane.add_argument("--label", default=None)
    p_lane.add_argument("--kind", default="workstream", dest="lane_kind")

    p_snapshot = sub.add_parser("snapshot", help="Show one-screen Project/Outcome coordination state")
    p_snapshot.add_argument("project", help="Project id or slug")
    p_snapshot.add_argument("--json", action="store_true", dest="as_json")

    p_materialize = sub.add_parser(
        "materialize-status",
        help="Write docs/outcomes/<OUTCOME>/00-status.md from current Project/Outcome/Git state",
    )
    p_materialize.add_argument("project", help="Project id or slug")
    p_materialize.add_argument("outcome", help="Outcome id or key")
    p_materialize.add_argument(
        "--repo", default=None, help="Repository root (defaults to Project primary path)"
    )

    p_tg = sub.add_parser(
        "telegram-provision",
        help="Create/bind topics in an existing Telegram forum supergroup",
    )
    p_tg.add_argument("project", help="Project id or slug")
    p_tg.add_argument("chat_id", help="Existing Telegram forum supergroup id")
    p_tg.add_argument(
        "--control",
        default=None,
        metavar="NAME",
        help="Create/reuse a project control topic with no Outcome binding",
    )
    p_tg.add_argument(
        "--topic",
        action="append",
        default=[],
        metavar="NAME[=OUTCOME]",
        help="Workstream topic; repeat for multiple topics",
    )

    parser.set_defaults(_project_parser=parser)
    return parser


def projects_command(args: argparse.Namespace) -> int:
    """Entry point from ``hermes project …`` argparse dispatch."""
    action = getattr(args, "project_action", None)
    if not action:
        parser = getattr(args, "_project_parser", None)
        if parser is not None:
            parser.print_help()
        else:
            print(
                "usage: hermes project <action> [options]\n"
                "Run 'hermes project --help' for the full list.",
                file=sys.stderr,
            )
        return 0

    handlers = {
        "create": _cmd_create,
        "list": _cmd_list,
        "ls": _cmd_list,
        "show": _cmd_show,
        "add-folder": _cmd_add_folder,
        "remove-folder": _cmd_remove_folder,
        "rename": _cmd_rename,
        "set-primary": _cmd_set_primary,
        "use": _cmd_use,
        "archive": _cmd_archive,
        "restore": _cmd_restore,
        "bind-board": _cmd_bind_board,
        "outcomes": _cmd_outcomes,
        "outcome-create": _cmd_outcome_create,
        "outcome-update": _cmd_outcome_update,
        "bind-lane": _cmd_bind_lane,
        "snapshot": _cmd_snapshot,
        "materialize-status": _cmd_materialize_status,
        "telegram-provision": _cmd_telegram_provision,
    }
    handler = handlers.get(action)
    if handler is None:
        print(f"Unknown project action: {action}", file=sys.stderr)
        return 1
    return handler(args)


def _resolve(conn, ident: str):
    proj = pdb.get_project(conn, ident)
    if proj is None:
        print(f"project: no such project: {ident}", file=sys.stderr)
    return proj


def _with_project(fn):
    """Open the DB, resolve ``args.project``, and run ``fn(args, conn, proj)``.

    Collapses the connect / resolve / not-found(1) / bad-arg(2) boilerplate every
    project-scoped subcommand repeated.
    """

    @functools.wraps(fn)
    def wrapper(args: argparse.Namespace) -> int:
        with pdb.connect_closing() as conn:
            proj = _resolve(conn, args.project)
            if proj is None:
                return 1
            try:
                return fn(args, conn, proj)
            except ValueError as exc:
                print(f"project: {exc}", file=sys.stderr)
                return 2

    return wrapper


def _print_project(proj) -> None:
    flags = " (archived)" if proj.archived else ""
    print(f"{proj.slug}  [{proj.id}]{flags}")
    print(f"  name:    {proj.name}")
    if proj.description:
        print(f"  about:   {proj.description}")
    if proj.board_slug:
        print(f"  board:   {proj.board_slug}")
    if proj.primary_path:
        print(f"  primary: {proj.primary_path}")
    if proj.folders:
        print("  folders:")
        for f in proj.folders:
            mark = " *" if f.is_primary else "  "
            label = f" ({f.label})" if f.label else ""
            print(f"   {mark} {f.path}{label}")


def _cmd_create(args: argparse.Namespace) -> int:
    try:
        with pdb.connect_closing() as conn:
            pid = pdb.create_project(
                conn,
                name=args.name,
                slug=args.slug,
                folders=args.folders,
                primary_path=args.primary,
                description=args.description,
                icon=args.icon,
                color=args.color,
                board_slug=args.board,
            )
            if args.use:
                pdb.set_active(conn, pid)
            proj = pdb.get_project(conn, pid)
    except ValueError as exc:
        print(f"project: {exc}", file=sys.stderr)
        return 2
    if proj is None:
        print("project: vanished after create", file=sys.stderr)
        return 2
    print(f"Created project {proj.slug} ({pid})")
    _print_project(proj)
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    with pdb.connect_closing() as conn:
        active = pdb.get_active_id(conn)
        projs = pdb.list_projects(
            conn, include_archived=getattr(args, "include_archived", False)
        )
    if not projs:
        print("No projects yet. Create one with `hermes project create <name>`.")
        return 0
    for p in projs:
        marker = "*" if p.id == active else " "
        flags = " (archived)" if p.archived else ""
        nfolders = len(p.folders)
        print(f"{marker} {p.slug:<24} {p.name}{flags}  [{nfolders} folder(s)]")
    return 0


@_with_project
def _cmd_show(args, conn, proj) -> int:
    _print_project(proj)
    return 0


@_with_project
def _cmd_add_folder(args, conn, proj) -> int:
    path = pdb.add_folder(conn, proj.id, args.path, label=args.label, is_primary=args.primary)
    print(f"Added {path} to {proj.slug}")
    return 0


@_with_project
def _cmd_remove_folder(args, conn, proj) -> int:
    if not pdb.remove_folder(conn, proj.id, args.path):
        print(f"project: folder not in project: {args.path}", file=sys.stderr)
        return 1
    print(f"Removed {args.path} from {proj.slug}")
    return 0


@_with_project
def _cmd_rename(args, conn, proj) -> int:
    pdb.update_project(conn, proj.id, name=args.name)
    print(f"Renamed {proj.slug} -> {args.name}")
    return 0


@_with_project
def _cmd_set_primary(args, conn, proj) -> int:
    if not pdb.set_primary(conn, proj.id, args.path):
        print(
            f"project: '{args.path}' is not a folder of {proj.slug}; "
            f"add it first with `hermes project add-folder`.",
            file=sys.stderr,
        )
        return 1
    print(f"Set primary of {proj.slug} -> {args.path}")
    return 0


def _cmd_use(args: argparse.Namespace) -> int:
    with pdb.connect_closing() as conn:
        if not args.project:
            pdb.set_active(conn, None)
            print("Cleared active project")
            return 0
        proj = _resolve(conn, args.project)
        if proj is None:
            return 1
        pdb.set_active(conn, proj.id)
    print(f"Active project: {proj.slug}")
    return 0


@_with_project
def _cmd_archive(args, conn, proj) -> int:
    pdb.archive_project(conn, proj.id)
    print(f"Archived {proj.slug}")
    return 0


@_with_project
def _cmd_restore(args, conn, proj) -> int:
    pdb.restore_project(conn, proj.id)
    print(f"Restored {proj.slug}")
    return 0


@_with_project
def _cmd_bind_board(args, conn, proj) -> int:
    pdb.update_project(conn, proj.id, board_slug=args.board)
    if args.board.strip():
        print(f"Bound {proj.slug} -> board {args.board}")
        _sync_board_default_workdir(proj, args.board)
    else:
        print(f"Unbound board from {proj.slug}")
    return 0


@_with_project
def _cmd_outcomes(args, _conn, proj) -> int:
    from hermes_cli import outcomes_db as odb

    with odb.connect_closing() as oc:
        outcomes = odb.list_outcomes(
            oc, proj.id, include_archived=bool(args.include_archived)
        )
    if not outcomes:
        print(f"No Outcomes for {proj.slug}")
        return 0
    for outcome in outcomes:
        marker = " (archived)" if outcome.archived else ""
        print(f"{outcome.outcome_key:<36} {outcome.state:<14} [{outcome.id}]{marker}")
        if outcome.next_action:
            print(f"  next: {outcome.next_action}")
    return 0


@_with_project
def _cmd_outcome_create(args, _conn, proj) -> int:
    from hermes_cli import outcomes_db as odb

    with odb.connect_closing() as oc:
        oid = odb.create_outcome(
            oc,
            project_id=proj.id,
            outcome_key=args.outcome_key,
            name=args.name,
            state=args.state,
            visible_owner=args.owner,
            current_base_ref=args.current_base_ref,
            next_action=args.next_action,
        )
        outcome = odb.get_outcome(oc, oid)
    print(f"Outcome {outcome.outcome_key} [{outcome.id}] state={outcome.state}")
    return 0


@_with_project
def _cmd_outcome_update(args, _conn, proj) -> int:
    from hermes_cli import outcomes_db as odb

    with odb.connect_closing() as oc:
        outcome = odb.get_outcome(oc, args.outcome, project_id=proj.id)
        if outcome is None:
            print(f"project: no such Outcome in {proj.slug}: {args.outcome}", file=sys.stderr)
            return 1
        fields = {}
        for attr in (
            "name",
            "state",
            "current_base_ref",
            "current_candidate_ref",
            "current_live_ref",
            "next_action",
        ):
            value = getattr(args, attr, None)
            if value is not None:
                fields[attr] = value
        if args.owner is not None:
            fields["visible_owner"] = args.owner
        if args.archive:
            fields["archived"] = True
        odb.update_outcome(oc, outcome.id, **fields)
        updated = odb.get_outcome(oc, outcome.id)
    print(f"Outcome {updated.outcome_key} [{updated.id}] state={updated.state}")
    return 0


@_with_project
def _cmd_bind_lane(args, _conn, proj) -> int:
    from hermes_cli import outcomes_db as odb

    with odb.connect_closing() as oc:
        outcome_id = None
        if args.outcome:
            outcome = odb.get_outcome(oc, args.outcome, project_id=proj.id)
            if outcome is None:
                print(f"project: no such Outcome in {proj.slug}: {args.outcome}", file=sys.stderr)
                return 1
            outcome_id = outcome.id
        lane_id = odb.bind_conversation_lane(
            oc,
            project_id=proj.id,
            outcome_id=outcome_id,
            platform=args.platform,
            chat_id=args.chat_id,
            thread_id=args.thread_id,
            label=args.label,
            lane_kind=args.lane_kind,
        )
        lane = next(
            lane for lane in odb.list_conversation_lanes(oc, proj.id) if lane.id == lane_id
        )
    target = f"{lane.platform}:{lane.chat_id}"
    if lane.thread_id:
        target += f":{lane.thread_id}"
    suffix = f" outcome={lane.outcome_id}" if lane.outcome_id else ""
    print(f"Bound {target} -> project={proj.id}{suffix}")
    return 0


@_with_project
def _cmd_snapshot(args, _conn, proj) -> int:
    from hermes_cli import outcomes_db as odb

    with odb.connect_closing() as oc:
        snapshot = odb.project_snapshot(oc, proj.id)
    if args.as_json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print(f"{proj.name} [{proj.id}]")
    outcomes = snapshot["outcomes"]
    if not outcomes:
        print("  Outcomes: none")
    else:
        print("  Outcomes:")
        for outcome in outcomes:
            active = [
                lease
                for lease in snapshot["active_mutation_leases"]
                if lease.get("outcome_id") == outcome["id"]
            ]
            mutator = active[0]["owner_execution_id"] if active else "-"
            print(
                f"    {outcome['outcome_key']:<34} {outcome['state']:<14} mutator={mutator}"
            )
            if outcome.get("next_action"):
                print(f"      next: {outcome['next_action']}")
    print(f"  Conversation lanes: {len(snapshot['conversation_lanes'])}")
    print(f"  Active mutators: {len(snapshot['active_mutation_leases'])}")
    return 0


@_with_project
def _cmd_materialize_status(args, _conn, proj) -> int:
    from hermes_cli import outcomes_db as odb
    from hermes_cli.outcome_packet import materialize_status

    with odb.connect_closing() as oc:
        outcome = odb.get_outcome(oc, args.outcome, project_id=proj.id)
        if outcome is None:
            print(f"project: no such Outcome in {proj.slug}: {args.outcome}", file=sys.stderr)
            return 1
    repo = args.repo or proj.primary_path
    if not repo:
        print("project: Project has no primary repo; pass --repo", file=sys.stderr)
        return 2
    target = materialize_status(
        project_id=proj.id,
        project_name=proj.name,
        outcome_id=outcome.id,
        repo=Path(repo),
    )
    print(target)
    return 0


def _parse_topic_arg(raw: str):
    from hermes_cli.project_forum import TopicSpec

    text = str(raw or "").strip()
    if not text:
        raise ValueError("--topic must be NAME or NAME=OUTCOME")
    if "=" in text:
        name, outcome = text.split("=", 1)
        name = name.strip()
        outcome = outcome.strip()
        if not name or not outcome:
            raise ValueError("--topic must be NAME or NAME=OUTCOME")
        return TopicSpec(name=name, outcome_id=outcome, lane_kind="workstream")
    return TopicSpec(name=text, lane_kind="workstream")


@_with_project
def _cmd_telegram_provision(args, _conn, proj) -> int:
    from hermes_cli.project_forum import (
        TopicSpec,
        provision_telegram_topics_with_configured_bot,
    )

    specs = []
    if args.control:
        specs.append(TopicSpec(name=args.control, lane_kind="control"))
    specs.extend(_parse_topic_arg(raw) for raw in args.topic)
    if not specs:
        print("project: provide --control and/or at least one --topic", file=sys.stderr)
        return 2
    try:
        results = asyncio.run(
            provision_telegram_topics_with_configured_bot(
                project_id=proj.id,
                chat_id=args.chat_id,
                topics=specs,
            )
        )
    except (ValueError, RuntimeError) as exc:
        print(f"project: {exc}", file=sys.stderr)
        return 2
    for result in results:
        lane = result["lane"]
        verb = "Created" if result["created"] else "Reused"
        outcome = f" outcome={lane['outcome_id']}" if lane.get("outcome_id") else ""
        print(
            f"{verb} Telegram topic {lane.get('label')!r} "
            f"thread={lane.get('thread_id')} -> project={proj.id}{outcome}"
        )
    return 0


def _sync_board_default_workdir(proj, board_slug: str) -> None:
    """Best-effort: point the bound board's default_workdir at the primary repo.

    Keeps kanban task worktrees anchored to the project's repo. Failures here
    are non-fatal — the binding itself already succeeded.
    """
    if not proj.primary_path:
        return
    try:
        from hermes_cli import kanban_db as kb

        slug = kb._normalize_board_slug(board_slug)
        if not slug:
            return
        if slug != kb.DEFAULT_BOARD and not kb.board_exists(slug):
            return
        kb.write_board_metadata(slug, default_workdir=proj.primary_path)
    except Exception:
        pass
