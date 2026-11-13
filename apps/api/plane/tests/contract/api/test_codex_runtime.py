import asyncio
import json
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from plane.coordination.codex.config import RuntimeConfig, ProjectConfig
from plane.coordination.codex.runtime import Runtime
from plane.coordination.codex.client import CodexClient
from plane.db.models import CoordinationEvent
from plane.db.models.codex import CodexThread, CodexCommand, CodexProject
from .test_coordination_execution import execution as execution, claim
from .test_coordination_contracts import coordination as coordination


@pytest.fixture
def runtime_case(execution, tmp_path):
    _, _, project, issue, _, _, _, _, _, _ = execution
    run = claim(execution)
    response = execution[0].post(execution[-1] + f"runs/{run}/handoff/", {}, format="json")
    assert response.status_code == 201
    config = ProjectConfig(
        {"project_id": str(project.pk), "workspace": project.workspace.slug},
        {"local": {"host": "mac", "url": "ws://127.0.0.1:1"}},
        tmp_path,
    )
    runtime = Runtime(RuntimeConfig("http://localhost", config.runtimes, [config]))
    worker = CodexThread.objects.create(
        project=project,
        run_id=run,
        role="implementation",
        host_id="mac",
        runtime_id="local",
        thread_id="worker",
        state="busy",
        active_turn="working",
    )
    chat = CodexThread.objects.create(
        project=project, run_id=run, role="chat", host_id="mac", runtime_id="local", thread_id="chat", state="idle"
    )
    cursor = CoordinationEvent.objects.filter(project=project).order_by("-sequence").first().sequence
    CodexProject.objects.create(project=project, event_cursor=cursor)
    runtime.clients["local"] = AsyncMock(socket=object(), ready=True)
    yield runtime, worker, chat, issue
    asyncio.run(runtime.api.aclose())


@pytest.mark.django_db(transaction=True)
def test_busy_worker_keeps_working_while_chat_observes_and_queues(runtime_case):
    runtime, worker, chat, _ = runtime_case

    async def exercise():
        runtime.clients["local"].request.return_value = {"thread": {"status": {"type": "active"}}}
        await runtime.tool(chat, {"action": "worker_status", "args": {}})
        await runtime.tool(
            chat, {"action": "send_to_worker", "args": {"message_id": str(uuid4()), "text": "Use the uploaded logo"}}
        )
        await runtime.pump()

    asyncio.run(exercise())
    worker.refresh_from_db()
    assert worker.state == "busy" and worker.active_turn == "working"
    assert worker.commands.get().state == "queued"
    assert [call.args[0] for call in runtime.clients["local"].request.await_args_list] == ["thread/read"]


@pytest.mark.django_db(transaction=True)
def test_feedback_routes_directly_to_both_task_threads_not_overseer(runtime_case):
    runtime, worker, chat, issue = runtime_case
    overseer = CodexThread.objects.create(project=issue.project, role="overseer", host_id="mac", runtime_id="local")
    event = CoordinationEvent.objects.create(
        project=issue.project,
        sequence=1000,
        issue=issue,
        kind="attachment.uploaded",
        payload={"asset_id": str(uuid4())},
    )
    asyncio.run(runtime.collect_events())
    asyncio.run(runtime.collect_events())
    assert CodexCommand.objects.filter(event=event).count() == 2
    assert set(CodexCommand.objects.filter(event=event).values_list("thread_id", flat=True)) == {worker.pk, chat.pk}
    assert not overseer.commands.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("archived", [False, True])
@pytest.mark.parametrize("old_dispatch_pending", [False, True])
def test_reopened_task_is_dispatched_again_instead_of_routed_to_old_threads(
    runtime_case, execution, archived, old_dispatch_pending
):
    from plane.db.models import State

    runtime, worker, chat, issue = runtime_case
    _, human, project, _, _, _, _, _, _, prefix = execution
    overseer = CodexThread.objects.create(project=project, role="overseer", host_id="mac", runtime_id="local")
    previous_ready = CoordinationEvent.objects.filter(issue=issue, kind="task.ready").last()
    if old_dispatch_pending:
        CodexCommand.objects.create(thread=overseer, event=previous_ready, text="Previous dispatch", state="sent")
    session = prefix.replace("/api/v1/", "/api/", 1)
    assert human.post(session + f"runs/{worker.run_id}/cancel/", {}, format="json").status_code == 200
    if archived:
        CodexThread.objects.filter(run_id=worker.run_id).update(state="archived")
    state = State.objects.get(project=project, name="Todo")
    response = human.patch(
        f"/api/workspaces/{project.workspace.slug}/projects/{project.pk}/issues/{issue.pk}/",
        {"state_id": str(state.pk)},
        format="json",
    )
    assert response.status_code == 200, response.data
    ready = CoordinationEvent.objects.filter(issue=issue, kind="task.ready").last()
    assert ready.sequence > previous_ready.sequence
    assert ready.spec_revision == previous_ready.spec_revision
    asyncio.run(runtime.collect_events())
    assert CodexCommand.objects.filter(thread=overseer, event=ready).exists()
    assert not CodexCommand.objects.filter(thread__in=[worker, chat], event=ready).exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("method", ["turn/started", "item/completed"])
def test_chat_progress_notifications_publish_delivery_state(runtime_case, monkeypatch, method):
    from plane.coordination.codex import runtime as module
    from plane.coordination.codex.chat import ensure_conversation, post_message

    runtime, _, chat, issue = runtime_case
    conversation = ensure_conversation(issue)
    conversation.chat = chat
    conversation.save()
    message = post_message(issue, issue.created_by, uuid4(), "Please check progress", [])
    params = {"threadId": "chat", "turnId": "chat-turn"}
    if method == "turn/started":
        params["turn"] = {"id": "chat-turn"}
    else:
        params["item"] = {"type": "userMessage", "clientId": str(message.pk)}
    publish = Mock()
    monkeypatch.setattr(module, "wake", publish)
    asyncio.run(runtime.notification("local", {"method": method, "params": params}))
    publish.assert_called_once_with()


@pytest.mark.django_db(transaction=True)
def test_turn_acceptance_publishes_sent_chat_message_before_any_reply(runtime_case, monkeypatch):
    from plane.coordination.codex import runtime as module
    from plane.coordination.codex.chat import ensure_conversation, post_message

    runtime, _, chat, issue = runtime_case
    conversation = ensure_conversation(issue)
    conversation.chat = chat
    conversation.save()
    message = post_message(issue, issue.created_by, uuid4(), "A long-running status request", [])
    runtime.clients["local"].request.return_value = {"turn": {"id": "chat-turn"}}
    publish = Mock()
    monkeypatch.setattr(module, "wake", publish)
    asyncio.run(runtime.pump())
    message.refresh_from_db()
    assert message.state == "sent"
    publish.assert_called_once_with()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("connected", [False, True])
def test_closure_cancels_unsent_chat_messages_even_when_host_is_offline(runtime_case, connected):
    from plane.coordination.codex.chat import ensure_conversation, post_message

    runtime, worker, chat, issue = runtime_case
    conversation = ensure_conversation(issue)
    conversation.chat = chat
    conversation.save()
    message = post_message(issue, issue.created_by, uuid4(), "Never sent to Codex", [])
    worker.run.status = "cancelled"
    worker.run.save(update_fields=["status"])
    runtime.clients["local"].ready = connected
    runtime.credentials[str(worker.run_id)] = (1, "private-cached-key")
    asyncio.run(runtime.pump())
    message.refresh_from_db()
    assert message.state == "cancelled"
    assert chat.commands.get(pk=message.pk).state == "cancelled"
    assert str(worker.run_id) not in runtime.credentials


@pytest.mark.django_db(transaction=True)
def test_terminal_task_archives_task_threads_not_project(runtime_case):
    runtime, worker, chat, issue = runtime_case
    worker.run.status = "cancelled"
    worker.run.save(update_fields=["status"])
    asyncio.run(runtime.pump())
    assert set(CodexThread.objects.filter(run=worker.run).values_list("state", flat=True)) == {"archived"}
    methods = [call.args[0] for call in runtime.clients["local"].request.await_args_list]
    assert methods.count("thread/archive") == 2
    assert methods.count("turn/interrupt") == 1


def test_rpc_disconnect_releases_pending_requests():
    class Socket:
        async def send(self, data):
            self.sent = json.loads(data)

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    async def exercise():
        client = CodexClient({}, AsyncMock(), AsyncMock(), AsyncMock())
        client.socket = Socket()
        pending = asyncio.create_task(client.request("turn/start", {}))
        await asyncio.sleep(0)  # yield to the pending request, not a retry/polling timer
        await client.read()
        with pytest.raises(ConnectionError):
            await pending
        assert not client.pending

    asyncio.run(exercise())


def test_rpc_send_disconnect_is_a_recoverable_connection_error():
    from websockets.exceptions import ConnectionClosed

    async def exercise():
        client = CodexClient({}, AsyncMock(), AsyncMock(), AsyncMock())
        client.socket = AsyncMock()
        client.socket.send.side_effect = ConnectionClosed(None, None)
        with pytest.raises(ConnectionError):
            await client.request("turn/start", {})
        assert not client.pending

    asyncio.run(exercise())


def test_native_client_is_ready_only_after_session_reconciliation(monkeypatch):
    from plane.coordination.codex import client as module

    async def exercise():
        entered, release, restored, disconnected = (asyncio.Event() for _ in range(4))
        socket = AsyncMock()

        async def connections(*args, **kwargs):
            yield socket

        async def reconnect():
            entered.set()
            await release.wait()
            restored.set()

        monkeypatch.setattr(module, "connect", connections)
        client = CodexClient({"url": "ws://host"}, AsyncMock(), reconnect, AsyncMock())
        client.request = AsyncMock(return_value={})
        client.read = AsyncMock(side_effect=disconnected.wait)
        task = asyncio.create_task(client.run())
        try:
            await entered.wait()
            assert client.socket is socket
            assert not client.ready
            release.set()
            await restored.wait()
            assert client.ready
        finally:
            release.set()
            disconnected.set()
            await task
        assert not client.ready

    asyncio.run(exercise())


@pytest.mark.django_db(transaction=True)
def test_queued_work_waits_until_host_reconciliation_finishes(runtime_case):
    runtime, worker, _, _ = runtime_case
    worker.state = "idle"
    worker.save(update_fields=["state"])
    command = CodexCommand.objects.create(thread=worker, text="Wait for restored sessions")
    runtime.clients["local"].ready = False
    runtime.clients["local"].request.return_value = {"turn": {"id": "unexpected"}}
    asyncio.run(runtime.pump())
    command.refresh_from_db()
    assert command.state == "queued"
    runtime.clients["local"].request.assert_not_awaited()


@pytest.mark.django_db(transaction=True)
def test_reconnect_restores_active_turn_before_cancelling_work(runtime_case):
    runtime, worker, _, _ = runtime_case
    worker.state, worker.active_turn = "offline", ""
    worker.save(update_fields=["state", "active_turn"])
    command = CodexCommand.objects.create(thread=worker, text="Accepted before disconnect", state="submitting")

    async def history(thread_id):
        if thread_id == "worker":
            yield {
                "id": "accepted",
                "status": "inProgress",
                "items": [{"type": "userMessage", "clientId": str(command.pk)}],
            }

    async def request(method, params):
        if method == "thread/read":
            return {"thread": {"status": {"type": "active" if params["threadId"] == "worker" else "idle"}}}
        return {}

    runtime.clients["local"].history = history
    runtime.clients["local"].request.side_effect = request
    asyncio.run(runtime.reconnect("local"))
    worker.refresh_from_db()
    command.refresh_from_db()
    assert command.state == "sent"
    assert worker.state == "busy" and worker.active_turn == "accepted"
    worker.run.status = "cancelled"
    worker.run.save(update_fields=["status"])
    asyncio.run(runtime.pump())
    calls = runtime.clients["local"].request.await_args_list
    assert any(call.args == ("turn/interrupt", {"threadId": "worker", "turnId": "accepted"}) for call in calls)


@pytest.mark.django_db(transaction=True)
def test_reconnect_publishes_online_state_to_open_chats(runtime_case, monkeypatch):
    from plane.coordination.codex import runtime as module

    runtime, worker, chat, _ = runtime_case
    CodexThread.objects.filter(pk__in=[worker.pk, chat.pk]).update(state="offline")

    async def history(thread_id):
        if False:
            yield

    runtime.clients["local"].history = history
    runtime.clients["local"].request.return_value = {"thread": {"status": {"type": "idle"}}}
    publish = Mock()
    monkeypatch.setattr(module, "wake", publish)
    asyncio.run(runtime.reconnect("local"))
    publish.assert_called_once_with()


@pytest.mark.django_db(transaction=True)
def test_reconnect_does_not_overwrite_a_concurrent_turn_completion(runtime_case):
    runtime, worker, _, _ = runtime_case
    command = CodexCommand.objects.create(thread=worker, text="Already running", state="sent", turn_id="working")

    async def history(thread_id):
        if thread_id == "worker":
            yield {
                "id": "working",
                "status": "inProgress",
                "items": [{"type": "userMessage", "clientId": str(command.pk)}],
            }

    async def request(method, params):
        if method == "thread/read" and params["threadId"] == "worker":
            await runtime.notification(
                "local",
                {
                    "method": "turn/completed",
                    "params": {"threadId": "worker", "turn": {"id": "working", "status": "completed"}},
                },
            )
            return {"thread": {"status": {"type": "active"}}}
        return {"thread": {"status": {"type": "idle"}}}

    runtime.clients["local"].history = history
    runtime.clients["local"].request.side_effect = request
    asyncio.run(runtime.reconnect("local"))
    worker.refresh_from_db()
    command.refresh_from_db()
    assert command.state == "completed"
    assert worker.state == "idle" and worker.active_turn == ""


def test_history_reads_all_pages_without_replaying_messages():
    async def exercise():
        client = CodexClient({}, AsyncMock(), AsyncMock(), AsyncMock())
        client.request = AsyncMock(
            side_effect=[
                {"data": [{"id": "first", "status": "completed"}], "nextCursor": "next"},
                {"data": [{"turnId": "first", "item": {"type": "agentMessage", "text": "One"}}], "nextCursor": "items"},
                {"data": [{"turnId": "first", "item": {"type": "agentMessage", "text": "Two"}}], "nextCursor": None},
                {"data": [{"id": "last", "status": "completed"}], "nextCursor": None},
                {"data": [], "nextCursor": None},
            ]
        )
        history = [turn async for turn in client.history("chat")]
        assert len(history) == 2 and len(history[0]["items"]) == 2
        assert all(call.args[0] != "turn/start" for call in client.request.await_args_list)

    asyncio.run(exercise())


@pytest.mark.django_db(transaction=True)
def test_reconnect_does_not_erase_worker_acknowledgement(runtime_case):
    runtime, worker, chat, _ = runtime_case
    command = CodexCommand.objects.create(thread=worker, sender=chat, text="Use the feedback", state="acknowledged")
    asyncio.run(
        runtime.reconcile_turn(
            worker,
            {
                "id": "old-turn",
                "status": "completed",
                "items": [
                    {"type": "userMessage", "clientId": str(command.pk)},
                ],
            },
        )
    )
    command.refresh_from_db()
    assert command.state == "acknowledged"


@pytest.mark.django_db(transaction=True)
def test_role_binding_cannot_move_an_existing_project_to_another_runtime(runtime_case):
    runtime, worker, _, issue = runtime_case
    config = runtime.projects[str(issue.project_id)]
    config.runtimes["other"] = {"host": "soso", "url": "ws://127.0.0.1:2"}
    config.data.update(
        runtime="other", placement={role: {"cwd": "/tmp/test"} for role in ("overseer", "implementation", "chat")}
    )
    with pytest.raises(ValueError, match="project cannot move hosts"):
        asyncio.run(runtime.binding(config, issue.project, "implementation", worker.run))
    worker.refresh_from_db()
    assert worker.host_id == "mac" and worker.runtime_id == "local"


@pytest.mark.django_db(transaction=True)
def test_attachment_download_uses_scoped_api_and_native_host_filesystem(runtime_case):
    import httpx

    runtime, worker, _, _ = runtime_case
    runtime.task_token = AsyncMock(return_value="private-task-key")
    runtime.api_call = AsyncMock(return_value={"download_url": "http://storage/attachment"})
    runtime.api.get = AsyncMock(
        return_value=httpx.Response(200, content=b"feedback", request=httpx.Request("GET", "http://storage/attachment"))
    )
    result = asyncio.run(runtime.download_file(worker, {"asset_id": "asset", "path": "/tmp/feedback.txt"}))
    assert result == {"asset_id": "asset", "path": "/tmp/feedback.txt"}
    assert runtime.api_call.await_args.kwargs["token"] == "private-task-key"
    runtime.clients["local"].request.assert_awaited_once_with(
        "fs/writeFile", {"path": "/tmp/feedback.txt", "dataBase64": "ZmVlZGJhY2s="}
    )


@pytest.mark.django_db(transaction=True)
def test_turn_start_error_reconciles_an_accepted_turn_without_repeating_it(runtime_case):
    from plane.coordination.codex.client import RPCError

    runtime, worker, _, _ = runtime_case
    worker.state = "idle"
    worker.save(update_fields=["state"])
    command = CodexCommand.objects.create(thread=worker, text="Follow-up")
    runtime.clients["local"].request.side_effect = RPCError("Turn start returned an ambiguous error")

    async def history(thread_id):
        yield {"id": "accepted", "status": "completed", "items": [{"type": "userMessage", "clientId": str(command.pk)}]}

    runtime.clients["local"].history = history
    asyncio.run(runtime.pump())
    command.refresh_from_db()
    worker.refresh_from_db()
    assert command.state == "completed" and command.turn_id == "accepted"
    assert worker.state == "idle"
    assert runtime.clients["local"].request.await_count == 1


@pytest.mark.django_db(transaction=True)
def test_reconciliation_keeps_interrupted_output_incomplete(runtime_case):
    from plane.coordination.codex.chat import ensure_conversation, assistant_message, snapshot

    runtime, _, chat, issue = runtime_case
    conversation = ensure_conversation(issue)
    conversation.chat = chat
    conversation.save()
    assistant_message(chat, "turn", "partial", "Partial reply", final=False)
    asyncio.run(
        runtime.reconcile_turn(
            chat,
            {
                "id": "turn",
                "status": "interrupted",
                "items": [
                    {"type": "agentMessage", "id": "partial", "text": "Partial reply"},
                ],
            },
        )
    )
    message = snapshot(conversation)["messages"][0]
    assert message["body"] == "Partial reply" and message["state"] == "interrupted"


@pytest.mark.django_db(transaction=True)
def test_early_completion_is_not_overwritten_by_late_turn_start_response(runtime_case):
    runtime, worker, _, _ = runtime_case
    worker.state = "idle"
    worker.save(update_fields=["state"])
    command = CodexCommand.objects.create(thread=worker, text="A fast follow-up")

    async def request(method, params):
        await runtime.notification(
            "local",
            {
                "method": "item/completed",
                "params": {
                    "threadId": "worker",
                    "turnId": "fast",
                    "item": {"type": "userMessage", "clientId": str(command.pk)},
                },
            },
        )
        await runtime.notification(
            "local",
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "worker",
                    "turn": {"id": "fast", "status": "completed"},
                },
            },
        )
        return {"turn": {"id": "fast"}}

    runtime.clients["local"].request.side_effect = request
    asyncio.run(runtime.pump())
    command.refresh_from_db()
    worker.refresh_from_db()
    assert command.state == "completed"
    assert worker.state == "idle" and worker.active_turn == ""


@pytest.mark.django_db(transaction=True)
def test_duplicate_ready_events_queue_only_one_overseer_handoff(runtime_case):
    from django.db import transaction
    from plane.coordination.contracts import emit_event
    from plane.db.models import Issue

    runtime, _, _, issue = runtime_case
    overseer = CodexThread.objects.create(project=issue.project, role="overseer", host_id="mac", runtime_id="local")
    other = Issue(project=issue.project, name="A new task", created_by=issue.created_by)
    other.save(disable_auto_set_user=True)
    with transaction.atomic():
        emit_event(issue.project, "task.ready", actor=issue.created_by, issue=other)
    asyncio.run(runtime.collect_events())
    assert overseer.commands.filter(event__issue=other, event__kind="task.ready").count() == 1
