"""``hermes project`` CLI — manage first-class, multi-folder Projects."""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import sys
from pathlib import Path

from hermes_cli import projects_db as pdb


def build_parser(parent_subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
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
    p_create.add_argument("folders", nargs="*", help="Folder paths to include (first = primary)")
    p_create.add_argument("--slug", default=None, help="Explicit slug override")
    p_create.add_argument("--primary", default=None, metavar="PATH", help="Primary repo path")
    for opt in ("--description", "--icon", "--color"):
        p_create.add_argument(opt, default=None)
    p_create.add_argument("--board", default=None, metavar="SLUG", help="Bind a kanban board")
    p_create.add_argument("--use", action="store_true", help="Set as the active project")
    p_list = sub.add_parser("list", aliases=["ls"], help="List projects")
    p_list.add_argument("--all", action="store_true", dest="include_archived", help="Include archived projects")

    def project_sub(name: str, help: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help)
        sp.add_argument("project", help="Project id or slug")
        return sp

    project_sub("show", "Show a project's details")
    p_add = project_sub("add-folder", "Add a folder to a project")
    p_add.add_argument("path", help="Folder path")
    p_add.add_argument("--label", default=None)
    p_add.add_argument("--primary", action="store_true", help="Mark as primary repo")
    project_sub("remove-folder", "Remove a folder from a project").add_argument("path", help="Folder path")
    project_sub("rename", "Rename a project").add_argument("name", help="New name")
    project_sub("set-primary", "Set the primary folder").add_argument("path", help="Folder path (must already be in project)")
    p_use = sub.add_parser("use", help="Set the active project")
    p_use.add_argument("project", nargs="?", default=None, help="Project id or slug (omit to clear)")
    project_sub("archive", "Archive a project")
    project_sub("restore", "Restore an archived project")
    project_sub("bind-board", "Bind a kanban board to a project").add_argument(
        "board", nargs="?", default="", help="Board slug (omit to unbind)"
    )
    p_outcomes = project_sub("outcomes", "List current Outcomes for a project")
    p_outcomes.add_argument("--all", action="store_true", dest="include_archived")
    p_outcome_create = project_sub("outcome-create", "Create a material Outcome")
    p_outcome_create.add_argument(
        "outcome_key", help="Stable key, e.g. STAFFING-TEST-ENABLER-R1")
    p_outcome_create.add_argument("--name", default=None)
    p_outcome_create.add_argument("--state", default="planning")
    p_outcome_create.add_argument("--owner", default=None)
    p_outcome_create.add_argument("--base", default=None, dest="current_base_ref")
    p_outcome_create.add_argument(
        "--acceptance", action="append", default=None, dest="frozen_acceptance",
        help="Frozen acceptance criterion (repeatable). Stored on the Outcome, not a task body.",
    )
    p_outcome_create.add_argument("--next", default=None, dest="next_action")
    p_outcome_update = project_sub(
        "outcome-update", "Update current Outcome projection")
    p_outcome_update.add_argument("outcome", help="Outcome id or key")
    p_outcome_update.add_argument("--name", default=None)
    p_outcome_update.add_argument("--state", default=None)
    p_outcome_update.add_argument("--owner", default=None)
    p_outcome_update.add_argument("--base", default=None, dest="current_base_ref")
    p_outcome_update.add_argument(
        "--candidate", default=None, dest="current_candidate_ref")
    p_outcome_update.add_argument("--live", default=None, dest="current_live_ref")
    p_outcome_update.add_argument(
        "--acceptance", action="append", default=None, dest="frozen_acceptance",
        help="Replace frozen acceptance with the supplied criterion/criteria (repeatable).",
    )
    p_outcome_update.add_argument("--next", default=None, dest="next_action")
    p_outcome_update.add_argument("--archive", action="store_true")
    p_depend = project_sub(
        "outcome-depend", "Bind one Outcome dependency")
    p_depend.add_argument("outcome", help="Owning Outcome id or key")
    p_depend.add_argument(
        "depends_on_project", help="Required Project id or slug")
    p_depend.add_argument(
        "depends_on_outcome", help="Required Outcome id or key")
    p_depend.add_argument(
        "--kind", default="requires", dest="dependency_kind")
    p_lane = project_sub(
        "bind-lane", "Bind a conversation lane to Project/Outcome context")
    p_lane.add_argument("--platform", required=True)
    p_lane.add_argument("--chat-id", required=True)
    p_lane.add_argument("--thread-id", default=None)
    p_lane.add_argument("--outcome", default=None)
    p_lane.add_argument("--label", default=None)
    p_lane.add_argument("--kind", default="workstream", dest="lane_kind")
    p_snapshot = project_sub(
        "snapshot", "Show one-screen Project/Outcome coordination state")
    p_snapshot.add_argument("--json", action="store_true", dest="as_json")
    p_materialize = project_sub(
        "materialize-status",
        "Write docs/outcomes/<OUTCOME>/00-status.md from current Project/Outcome/Git state",
    )
    p_materialize.add_argument("outcome", help="Outcome id or key")
    p_materialize.add_argument(
        "--repo", default=None,
        help="Repository root (defaults to Project primary path)")
    p_tg = project_sub(
        "telegram-provision",
        "Create/bind topics in an existing Telegram forum supergroup",
    )
    p_tg.add_argument("chat_id", help="Existing Telegram forum supergroup id")
    p_tg.add_argument(
        "--control", default=None, metavar="NAME",
        help="Create/reuse a project control topic with no Outcome binding")
    p_tg.add_argument(
        "--topic", action="append", default=[], metavar="NAME[=OUTCOME]",
        help="Workstream topic; repeat for multiple topics")
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
            print("usage: hermes project <action> [options]\nRun 'hermes project --help' for the full list.", file=sys.stderr)
        return 0
    handler = _HANDLERS.get(action)
    if handler is None:
        print(f"Unknown project action: {action}", file=sys.stderr)
        return 1
    return handler(args)


def _err(message: str) -> int:
    print(f"project: {message}", file=sys.stderr)
    return 1


def _resolve(conn, ident: str):
    proj = pdb.get_project(conn, ident)
    if proj is None:
        _err(f"no such project: {ident}")
    return proj


def _db_command(fn):
    """Open the DB and run ``fn(args, conn)``; a ``str`` result is printed (rc 0), an ``int`` is the rc;
    a ``ValueError`` prints ``project: …`` and exits 2."""

    @functools.wraps(fn)
    def wrapper(args: argparse.Namespace) -> int:
        try:
            with pdb.connect_closing() as conn:
                out = fn(args, conn)
        except ValueError as exc:
            print(f"project: {exc}", file=sys.stderr)
            return 2
        if isinstance(out, str):
            print(out)
            return 0
        return out

    return wrapper


def _with_project(fn):
    """Like ``_db_command`` but also resolves ``args.project`` into ``fn(args, conn, proj)``."""

    @functools.wraps(fn)
    def wrapper(args: argparse.Namespace, conn):
        proj = _resolve(conn, args.project)
        return 1 if proj is None else fn(args, conn, proj)

    return _db_command(wrapper)


def _print_project(proj) -> None:
    print(f"{proj.slug}  [{proj.id}]{' (archived)' if proj.archived else ''}")
    print(f"  name:    {proj.name}")
    for label, value in (("about", proj.description), ("board", proj.board_slug), ("primary", proj.primary_path)):
        if value:
            print(f"  {label}:{' ' * (8 - len(label))}{value}")
    if proj.folders:
        print("  folders:")
        for f in proj.folders:
            print(f"   {' *' if f.is_primary else '  '} {f.path}{f' ({f.label})' if f.label else ''}")


@_db_command
def _cmd_create(args, conn) -> int:
    pid = pdb.create_project(
        conn, name=args.name, slug=args.slug, folders=args.folders, primary_path=args.primary,
        description=args.description, icon=args.icon, color=args.color, board_slug=args.board,
    )
    if args.use:
        pdb.set_active(conn, pid)
    proj = pdb.get_project(conn, pid)
    if proj is None:
        print("project: vanished after create", file=sys.stderr)
        return 2
    print(f"Created project {proj.slug} ({pid})")
    _print_project(proj)
    return 0


@_db_command
def _cmd_list(args, conn):
    active = pdb.get_active_id(conn)
    projs = pdb.list_projects(conn, include_archived=getattr(args, "include_archived", False))
    if not projs:
        return "No projects yet. Create one with `hermes project create <name>`."
    for p in projs:
        flags = " (archived)" if p.archived else ""
        print(f"{'*' if p.id == active else ' '} {p.slug:<24} {p.name}{flags}  [{len(p.folders)} folder(s)]")
    return 0


@_with_project
def _cmd_show(args, conn, proj) -> int:
    _print_project(proj)
    return 0


@_with_project
def _cmd_add_folder(args, conn, proj) -> str:
    path = pdb.add_folder(conn, proj.id, args.path, label=args.label, is_primary=args.primary)
    return f"Added {path} to {proj.slug}"


@_with_project
def _cmd_remove_folder(args, conn, proj):
    if not pdb.remove_folder(conn, proj.id, args.path):
        return _err(f"folder not in project: {args.path}")
    return f"Removed {args.path} from {proj.slug}"


@_with_project
def _cmd_rename(args, conn, proj) -> str:
    pdb.update_project(conn, proj.id, name=args.name)
    return f"Renamed {proj.slug} -> {args.name}"


@_with_project
def _cmd_set_primary(args, conn, proj):
    if not pdb.set_primary(conn, proj.id, args.path):
        return _err(f"'{args.path}' is not a folder of {proj.slug}; add it first with `hermes project add-folder`.")
    return f"Set primary of {proj.slug} -> {args.path}"


@_db_command
def _cmd_use(args, conn):
    if not args.project:
        pdb.set_active(conn, None)
        return "Cleared active project"
    proj = _resolve(conn, args.project)
    if proj is None:
        return 1
    pdb.set_active(conn, proj.id)
    return f"Active project: {proj.slug}"


def _flag_command(op: str, verb: str):
    """Handler for ``pdb.<op>(conn, proj.id)`` followed by ``"<verb> <slug>"``."""
    return _with_project(lambda args, conn, proj: (getattr(pdb, op)(conn, proj.id), f"{verb} {proj.slug}")[1])


@_with_project
def _cmd_bind_board(args, conn, proj) -> str:
    pdb.update_project(conn, proj.id, board_slug=args.board)
    if not args.board.strip():
        return f"Unbound board from {proj.slug}"
    if proj.primary_path:  # best-effort: point the bound board's default_workdir at the primary repo
        try:
            from hermes_cli import kanban_db as kb

            slug = kb._normalize_board_slug(args.board)
            if slug and (slug == kb.DEFAULT_BOARD or kb.board_exists(slug)):
                kb.write_board_metadata(slug, default_workdir=proj.primary_path)
        except Exception:
            pass
    return f"Bound {proj.slug} -> board {args.board}"


@_with_project
def _cmd_outcomes(args, _conn, proj) -> int:
    from hermes_cli import outcomes_db as odb

    with odb.connect_closing() as outcomes_conn:
        outcomes = odb.list_outcomes(
            outcomes_conn, proj.id, include_archived=bool(args.include_archived))
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
def _cmd_outcome_create(args, _conn, proj) -> str:
    from hermes_cli import outcomes_db as odb

    with odb.connect_closing() as outcomes_conn:
        outcome_id = odb.create_outcome(
            outcomes_conn,
            project_id=proj.id,
            outcome_key=args.outcome_key,
            name=args.name,
            state=args.state,
            visible_owner=args.owner,
            current_base_ref=args.current_base_ref,
            frozen_acceptance=args.frozen_acceptance,
            next_action=args.next_action,
        )
        outcome = odb.get_outcome(outcomes_conn, outcome_id)
    return f"Outcome {outcome.outcome_key} [{outcome.id}] state={outcome.state}"


@_with_project
def _cmd_outcome_update(args, _conn, proj):
    from hermes_cli import outcomes_db as odb

    with odb.connect_closing() as outcomes_conn:
        outcome = odb.get_outcome(outcomes_conn, args.outcome, project_id=proj.id)
        if outcome is None:
            return _err(f"no such Outcome in {proj.slug}: {args.outcome}")
        fields = {
            attr: value
            for attr in (
                "name", "state", "current_base_ref", "current_candidate_ref",
                "current_live_ref", "next_action")
            if (value := getattr(args, attr, None)) is not None
        }
        if args.owner is not None:
            fields["visible_owner"] = args.owner
        if args.frozen_acceptance is not None:
            fields["frozen_acceptance"] = args.frozen_acceptance
        if args.archive:
            fields["archived"] = True
        odb.update_outcome(outcomes_conn, outcome.id, **fields)
        updated = odb.get_outcome(outcomes_conn, outcome.id)
    return f"Outcome {updated.outcome_key} [{updated.id}] state={updated.state}"


@_with_project
def _cmd_outcome_depend(args, conn, proj) -> str | int:
    from hermes_cli import outcomes_db as odb

    required_project = pdb.get_project(conn, args.depends_on_project)
    if required_project is None:
        return _err(f"no such required project: {args.depends_on_project}")
    with odb.connect_closing() as outcomes_conn:
        outcome = odb.get_outcome(
            outcomes_conn, args.outcome, project_id=proj.id)
        required = odb.get_outcome(
            outcomes_conn, args.depends_on_outcome,
            project_id=required_project.id)
        if outcome is None:
            return _err(f"no such Outcome in {proj.slug}: {args.outcome}")
        if required is None:
            return _err(
                f"no such required Outcome in {required_project.slug}: "
                f"{args.depends_on_outcome}")
        dependency_id = odb.add_outcome_dependency(
            outcomes_conn,
            outcome_id=outcome.id,
            depends_on_outcome_id=required.id,
            dependency_kind=args.dependency_kind,
        )
    return (
        f"Dependency {dependency_id}: {proj.slug}/{outcome.outcome_key} -> "
        f"{required_project.slug}/{required.outcome_key}")


@_with_project
def _cmd_bind_lane(args, _conn, proj):
    from hermes_cli import outcomes_db as odb

    with odb.connect_closing() as outcomes_conn:
        outcome_id = None
        if args.outcome:
            outcome = odb.get_outcome(
                outcomes_conn, args.outcome, project_id=proj.id)
            if outcome is None:
                return _err(f"no such Outcome in {proj.slug}: {args.outcome}")
            outcome_id = outcome.id
        lane_id = odb.bind_conversation_lane(
            outcomes_conn,
            project_id=proj.id,
            outcome_id=outcome_id,
            platform=args.platform,
            chat_id=args.chat_id,
            thread_id=args.thread_id,
            label=args.label,
            lane_kind=args.lane_kind,
        )
        lane = next(
            lane
            for lane in odb.list_conversation_lanes(outcomes_conn, proj.id)
            if lane.id == lane_id)
    target = f"{lane.platform}:{lane.chat_id}"
    if lane.thread_id:
        target += f":{lane.thread_id}"
    suffix = f" outcome={lane.outcome_id}" if lane.outcome_id else ""
    return f"Bound {target} -> project={proj.id}{suffix}"


@_with_project
def _cmd_snapshot(args, _conn, proj) -> int:
    from hermes_cli import outcomes_db as odb

    with odb.connect_closing() as outcomes_conn:
        snapshot = odb.project_snapshot(outcomes_conn, proj.id)
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
                if lease.get("outcome_id") == outcome["id"]]
            mutator = active[0]["owner_execution_id"] if active else "-"
            print(
                f"    {outcome['outcome_key']:<34} "
                f"{outcome['state']:<14} mutator={mutator}")
            if outcome.get("next_action"):
                print(f"      next: {outcome['next_action']}")
    print(f"  Conversation lanes: {len(snapshot['conversation_lanes'])}")
    print(f"  Active mutators: {len(snapshot['active_mutation_leases'])}")
    return 0


@_with_project
def _cmd_materialize_status(args, _conn, proj) -> int:
    from hermes_cli import outcomes_db as odb
    from hermes_cli.outcome_packet import materialize_status

    with odb.connect_closing() as outcomes_conn:
        outcome = odb.get_outcome(
            outcomes_conn, args.outcome, project_id=proj.id)
        if outcome is None:
            return _err(f"no such Outcome in {proj.slug}: {args.outcome}")
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
        name, outcome = (part.strip() for part in text.split("=", 1))
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
        results = asyncio.run(provision_telegram_topics_with_configured_bot(
            project_id=proj.id, chat_id=args.chat_id, topics=specs))
    except (ValueError, RuntimeError) as exc:
        print(f"project: {exc}", file=sys.stderr)
        return 2
    for result in results:
        lane = result["lane"]
        verb = "Created" if result["created"] else "Reused"
        outcome = f" outcome={lane['outcome_id']}" if lane.get("outcome_id") else ""
        print(
            f"{verb} Telegram topic {lane.get('label')!r} "
            f"thread={lane.get('thread_id')} -> project={proj.id}{outcome}")
    return 0


_HANDLERS = {
    "create": _cmd_create,
    "list": _cmd_list,
    "ls": _cmd_list,
    "show": _cmd_show,
    "add-folder": _cmd_add_folder,
    "remove-folder": _cmd_remove_folder,
    "rename": _cmd_rename,
    "set-primary": _cmd_set_primary,
    "use": _cmd_use,
    "archive": _flag_command("archive_project", "Archived"),
    "restore": _flag_command("restore_project", "Restored"),
    "bind-board": _cmd_bind_board,
    "outcomes": _cmd_outcomes,
    "outcome-create": _cmd_outcome_create,
    "outcome-update": _cmd_outcome_update,
    "outcome-depend": _cmd_outcome_depend,
    "bind-lane": _cmd_bind_lane,
    "snapshot": _cmd_snapshot,
    "materialize-status": _cmd_materialize_status,
    "telegram-provision": _cmd_telegram_provision,
}
