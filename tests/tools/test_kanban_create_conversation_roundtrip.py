"""Exercise create schema -> handler -> real temporary stores -> notification target.

No worker, network delivery, model or production database is used. Keep routing
validation in the existing database boundary rather than duplicating it here.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def context(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(home / 'kanban.db'))
    monkeypatch.setenv('HERMES_PROFILE', 'default')
    for key in ('HERMES_KANBAN_TASK', 'HERMES_SESSION_ID', 'HERMES_KANBAN_BOARD', 'HERMES_KANBAN_HOME'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    from hermes_cli import outcomes_db as odb, projects_db as pdb
    from tools import kanban_tools as kt
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    workspace = tmp_path / 'repo'
    workspace.mkdir()
    with pdb.connect_closing() as conn:
        project = pdb.create_project(conn, name='Test project', slug='test-project', primary_path=str(workspace))
        other_dir = tmp_path / 'other-repo'
        other_dir.mkdir()
        other = pdb.create_project(conn, name='Other project', slug='other-project', primary_path=str(other_dir))
    with odb.connect_closing() as conn:
        outcome = odb.create_outcome(conn, project_id=project, outcome_key='TEST', name='Test', visible_owner='default')
        lane = odb.bind_conversation_lane(conn, project_id=project, outcome_id=outcome, platform='telegram', chat_id='-1000000000001', thread_id='3', label='project', lane_kind='control')
    monkeypatch.setattr(kt, 'load_config', lambda: {'kanban': {'auto_subscribe_on_create': True}})
    return {'kb': kb, 'kbc': kbc, 'kt': kt, 'project': project, 'other': other, 'outcome': outcome,
            'lane': lane, 'target': 'telegram:-1000000000001:3', 'workspace': str(workspace)}


def payload(ctx, **overrides):
    result = {'title': 'read-only routing check', 'assignee': 'test-reviewer',
              'triage': True, 'workspace_kind': 'dir', 'workspace_path': ctx['workspace'],
              'project_id': ctx['project'], 'outcome_id': ctx['outcome'],
              'conversation_lane_id': ctx['lane'], 'topic_target': ctx['target']}
    result.update(overrides)
    return result


def test_schema_exposes_structured_conversation_fields(context):
    properties = context['kt'].KANBAN_CREATE_SCHEMA['parameters']['properties']
    for key in ('conversation_lane_id', 'topic_target'):
        assert key in properties, f'{key} missing from advertised tool schema'
        assert properties[key]['type'] == 'string'


@pytest.mark.parametrize('use_aliases', [False, True])
def test_handler_persists_and_returns_exact_identity(context, use_aliases):
    args = payload(context)
    if use_aliases:
        args['project'] = args.pop('project_id')
        args['outcome'] = args.pop('outcome_id')
    result = json.loads(context['kt']._handle_create(args))
    assert result.get('ok') is True, result
    assert result.get('conversation_lane_id') == context['lane']
    assert result.get('topic_target') == context['target']
    with context['kbc'].connect() as conn:
        task = context['kb'].get_task(conn, result['task_id'])
    assert (task.project_id, task.outcome_id, task.conversation_lane_id, task.topic_target) == (
        context['project'], context['outcome'], context['lane'], context['target'])
    assert task.status == 'triage'


def test_registry_handler_roundtrips_structured_identity(context):
    from tools.registry import registry

    result = json.loads(registry.dispatch("kanban_create", payload(context)))
    assert result.get("ok") is True, result
    assert result["project_id"] == context["project"]
    assert result["outcome_id"] == context["outcome"]
    assert result["conversation_lane_id"] == context["lane"]
    assert result["topic_target"] == context["target"]


@pytest.mark.parametrize('origin_chat,origin_thread', [('555000', ''), ('-1000000000001', '3')])
def test_dm_or_project_origin_subscribes_only_to_exact_project(context, monkeypatch, origin_chat, origin_thread):
    from gateway import session_context
    env = {'HERMES_SESSION_PLATFORM': 'telegram', 'HERMES_SESSION_CHAT_ID': origin_chat,
           'HERMES_SESSION_THREAD_ID': origin_thread, 'HERMES_SESSION_CHAT_TYPE': 'group' if origin_thread else 'dm',
           'HERMES_SESSION_USER_ID': '555000'}
    monkeypatch.setattr(session_context, 'get_session_env', lambda key, default='': env.get(key, default))
    result = json.loads(context['kt']._handle_create(payload(context)))
    assert result.get('ok') is True, result
    with context['kbc'].connect() as conn:
        rows = conn.execute('SELECT chat_id, thread_id FROM kanban_notify_subs WHERE task_id=?', (result['task_id'],)).fetchall()
    assert {(str(row['chat_id']), str(row['thread_id'])) for row in rows} == {('-1000000000001', '3')}


def test_wrong_project_lane_is_rejected_without_persisting(context):
    with context['kbc'].connect() as conn:
        before = conn.execute('SELECT count(*) FROM tasks').fetchone()[0]
    result = json.loads(context['kt']._handle_create(payload(context, project_id=context['other'], outcome_id=None)))
    assert result.get('error'), result
    with context['kbc'].connect() as conn:
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == before


def test_target_mismatch_is_rejected_without_persisting(context):
    with context['kbc'].connect() as conn:
        before = conn.execute('SELECT count(*) FROM tasks').fetchone()[0]
    result = json.loads(context['kt']._handle_create(payload(context, topic_target='telegram:-1000000000002:4')))
    assert result.get('error'), result
    with context['kbc'].connect() as conn:
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == before


def test_replayed_creation_returns_same_bound_task(context):
    args = payload(context, idempotency_key='same-routing-request')
    one = json.loads(context['kt']._handle_create(args))
    two = json.loads(context['kt']._handle_create(args))
    assert one.get('ok') and two.get('ok'), (one, two)
    assert one['task_id'] == two['task_id']
    assert two.get('conversation_lane_id') == context['lane']
    assert two.get('topic_target') == context['target']


def test_legacy_unbound_create_remains_valid(context):
    args = payload(context)
    args.pop('conversation_lane_id')
    args.pop('topic_target')
    result = json.loads(context['kt']._handle_create(args))
    assert result.get('ok') is True, result
    assert result.get('conversation_lane_id') is None
    assert result.get('topic_target') is None


def test_child_inherits_exact_parent_route(context):
    parent = json.loads(context['kt']._handle_create(payload(context)))
    assert parent.get('ok'), parent
    child_args = payload(context, parents=[parent['task_id']], title='child routing check')
    child_args.pop('conversation_lane_id')
    child_args.pop('topic_target')
    child = json.loads(context['kt']._handle_create(child_args))
    assert child.get('ok'), child
    assert child.get('conversation_lane_id') == context['lane']
    assert child.get('topic_target') == context['target']


def test_native_terminal_readback_retains_structured_target(context):
    result = json.loads(context['kt']._handle_create(payload(context, triage=False)))
    assert result.get('ok'), result
    with context['kbc'].connect() as conn:
        assert context['kb'].complete_task(conn, result['task_id'], summary='read-only synthetic complete', fire_lifecycle_hook=False)
        task = context['kb'].get_task(conn, result['task_id'])
    assert task.status == 'done'
    assert task.conversation_lane_id == context['lane']
    assert task.topic_target == context['target']
