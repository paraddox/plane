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


@pytest.fixture
def runtime_api_case(runtime_case, execution):
    from asgiref.sync import sync_to_async
    from rest_framework.test import APIClient

    runtime, worker, chat, issue = runtime_case
    prefix = execution[-1].removesuffix("coordination/")
    dispatcher_key = execution[0]._credentials["HTTP_X_API_KEY"]

    async def api_call(project, method, path, data=None, token=None):
        def request():
            client = APIClient()
            client.credentials(HTTP_X_API_KEY=token or dispatcher_key)
            response = client.generic(method, prefix + path, json.dumps(data or {}), content_type="application/json")
            if response.status_code >= 400:
                raise ValueError(f"Plane API {response.status_code}: {response.data}")
            return response.data

        return await sync_to_async(request)()

    runtime.api_call = api_call
    asyncio.run(runtime.task_token(worker))
    human = execution[1]
    response = human.patch(
        f"/api/workspaces/{issue.project.workspace.slug}/projects/{issue.project_id}/issues/{issue.pk}/",
        {
            "description_html": "<p>New human description</p>",
            "requirements": "Revised requirement",
            "expected_revision": 1,
        },
        format="json",
    )
    assert response.status_code == 200, response.data
    return runtime, worker, chat, issue


@pytest.fixture
def runtime_startup_case(runtime_api_case, execution, monkeypatch, tmp_path):
    runtime, worker, _, issue = runtime_api_case
    config = runtime.project(worker)
    config.data.update(
        runtime="local",
        worker_id=str(execution[7].pk),
        dispatcher_key_env="PLANE_TEST_DISPATCHER",
        placement={role: {"cwd": str(tmp_path)} for role in ("overseer", "implementation", "chat")},
    )
    monkeypatch.setenv("PLANE_TEST_DISPATCHER", execution[0]._credentials["HTTP_X_API_KEY"])
    run = worker.run
    run.worker_id = f"codex:{issue.pk}"
    run.save(update_fields=["worker_id"])
    CodexThread.objects.filter(run=run).delete()
    runtime.clients["local"].socket = None
    return runtime, config, run, issue


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("saved_roles", [(), ("implementation",), ("implementation", "chat")])
def test_startup_finishes_interrupted_handoff_without_restarting_saved_sessions(runtime_startup_case, saved_roles):
    from plane.db.models.codex import AgentConversation

    runtime, config, run, issue = runtime_startup_case
    addresses = {}
    for role in saved_roles:
        thread = asyncio.run(runtime.binding(config, issue.project, role, run))
        thread.thread_id, thread.state = f"saved-{role}", "idle"
        thread.save(update_fields=["thread_id", "state"])
        addresses[role] = thread.thread_id
        if role == "implementation":
            CodexCommand.objects.create(
                thread=thread,
                event=CoordinationEvent.objects.filter(issue=issue).first(),
                text="Feedback queued during the unfinished handoff",
            )
    asyncio.run(runtime.setup())
    worker = CodexThread.objects.get(run=run, role="implementation")
    chat = CodexThread.objects.get(run=run, role="chat")
    assert AgentConversation.objects.get(issue=issue).chat_id == chat.pk
    assert worker.commands.filter(event__isnull=True, sender__isnull=True, message__isnull=True).count() == 1
    assert chat.commands.count() == 1
    asyncio.run(runtime.setup())
    assert CodexThread.objects.filter(run=run).count() == 2
    assert worker.commands.filter(event__isnull=True, sender__isnull=True, message__isnull=True).count() == 1
    for thread in (worker, chat):
        if thread.role in addresses:
            thread.refresh_from_db()
            assert thread.thread_id == addresses[thread.role]
    runtime.clients["local"].request.assert_not_awaited()


@pytest.mark.django_db(transaction=True)
def test_startup_completes_a_claim_stopped_before_handoff(runtime_startup_case, execution):
    from plane.db.models import AgentRun, State
    from plane.db.models.codex import AgentConversation

    runtime, config, previous, issue = runtime_startup_case
    dispatcher, human, project, _, _, _, _, worker, _, prefix = execution
    session = prefix.replace("/api/v1/", "/api/", 1)
    assert human.post(session + f"runs/{previous.pk}/cancel/", {}, format="json").status_code == 200
    state = State.objects.get(project=project, name="Todo")
    assert (
        human.patch(
            f"/api/workspaces/{project.workspace.slug}/projects/{project.pk}/issues/{issue.pk}/",
            {"state_id": str(state.pk)},
            format="json",
        ).status_code
        == 200
    )
    response = dispatcher.post(
        prefix + f"work-items/{issue.pk}/claim/",
        {
            "agent": str(worker.pk),
            "worker_id": f"codex:{issue.pk}",
            "repository": "example/product",
            "base_branch": "main",
        },
        format="json",
    )
    assert response.status_code == 201, response.data
    run = AgentRun.objects.get(pk=response.data["id"])
    assert run.handed_off_at is None
    asyncio.run(runtime.setup())
    run.refresh_from_db()
    assert run.handed_off_at is not None
    assert CodexThread.objects.filter(run=run).count() == 2
    assert AgentConversation.objects.get(issue=issue).chat.run_id == run.pk
    assert run.credentials.filter(purpose="assignment", is_active=True).count() == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("reason", ["other_runtime", "other_dispatcher", "revoked_dispatcher"])
def test_startup_does_not_adopt_unowned_or_unauthorized_runs(runtime_startup_case, reason, monkeypatch):
    from plane.db.models import APIToken

    runtime, _, run, _ = runtime_startup_case
    if reason == "other_runtime":
        run.worker_id = "external-worker"
        run.save(update_fields=["worker_id"])
    elif reason == "other_dispatcher":
        APIToken.objects.create(
            user=run.dispatcher.user,
            project=run.project,
            workspace=run.project.workspace,
            purpose="dispatcher",
            token="other-configured-dispatcher-key",
        )
        monkeypatch.setenv("PLANE_TEST_DISPATCHER", "other-configured-dispatcher-key")
    else:
        run.dispatcher.is_active = False
        run.dispatcher.save(update_fields=["is_active"])
    asyncio.run(runtime.setup())
    assert not CodexThread.objects.filter(run=run).exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("role", ["implementation", "chat"])
def test_revised_task_context_can_be_read_before_explicit_execution_acknowledgement(runtime_api_case, role):
    from asgiref.sync import sync_to_async
    from plane.db.models import APIToken

    runtime, worker, chat, issue = runtime_api_case
    reader = worker if role == "implementation" else chat
    path = f"work-items/{issue.pk}/"

    async def exercise():
        task = await runtime.tool(reader, {"action": "task_api", "args": {"path": path}})
        assert task["description_html"] == "<p>New human description</p>"
        specification = await runtime.tool(reader, {"action": "read_spec", "args": {}})
        assert specification["spec_revision"] == 2
        assert specification["requirements"] == "Revised requirement"
        assert specification["task"]["description_html"] == task["description_html"]
        assignment = await sync_to_async(
            lambda: APIToken.objects.get(agent_run_id=worker.run_id, purpose="assignment", is_active=True).pk
        )()
        with pytest.raises(ValueError, match="Specification changed"):
            await runtime.tool(
                worker, {"action": "task_api", "args": {"method": "PATCH", "path": path, "data": {"name": "Too soon"}}}
            )
        if role == "chat":
            with pytest.raises(ValueError, match="Only the implementation"):
                await runtime.tool(chat, {"action": "acknowledge_spec", "args": {"expected_revision": 2}})
        assert not await sync_to_async(
            APIToken.objects.filter(agent_run_id=worker.run_id, purpose="execution", is_active=True).exists
        )()
        acknowledged = await runtime.tool(worker, {"action": "acknowledge_spec", "args": {"expected_revision": 2}})
        assert acknowledged == {"acknowledged_revision": 2}
        await runtime.tool(chat, {"action": "task_api", "args": {"path": path}})
        changed = await runtime.tool(
            worker,
            {"action": "task_api", "args": {"method": "PATCH", "path": path, "data": {"name": "Acknowledged work"}}},
        )
        assert changed["name"] == "Acknowledged work"
        assert (
            await sync_to_async(
                lambda: APIToken.objects.get(agent_run_id=worker.run_id, purpose="assignment", is_active=True).pk
            )()
            == assignment
        )

    asyncio.run(exercise())
    worker.run.refresh_from_db()
    assert worker.run.spec_revision == 2
    assert worker.run.credentials.filter(purpose="assignment", is_active=True).count() == 1
    assert worker.run.credentials.filter(purpose="execution", is_active=True).count() == 1


@pytest.mark.django_db(transaction=True)
def test_revised_task_attachment_download_stays_scoped_without_acknowledging_execution(runtime_api_case, monkeypatch):
    import httpx
    from plane.coordination import notification_views
    from plane.db.models import FileAsset, Issue

    runtime, worker, _, issue = runtime_api_case
    asset = FileAsset.objects.create(
        project=issue.project,
        workspace=issue.project.workspace,
        issue=issue,
        is_uploaded=True,
        asset="task/revised-requirements.txt",
        attributes={"name": "requirements.txt"},
    )
    foreign = Issue(project=issue.project, name="Not assigned", created_by=issue.created_by)
    foreign.save(disable_auto_set_user=True)
    foreign_asset = FileAsset.objects.create(
        project=issue.project, workspace=issue.project.workspace, issue=foreign, is_uploaded=True
    )
    monkeypatch.setattr(
        notification_views,
        "S3Storage",
        Mock(return_value=Mock(generate_presigned_url=Mock(return_value="http://storage/requirements.txt"))),
    )
    runtime.api.get = AsyncMock(
        return_value=httpx.Response(
            200, content=b"New requirements", request=httpx.Request("GET", "http://storage/requirements.txt")
        )
    )

    async def exercise():
        result = await runtime.tool(
            worker, {"action": "download_asset", "args": {"asset_id": str(asset.pk), "path": "/tmp/requirements.txt"}}
        )
        assert result["asset_id"] == str(asset.pk)
        with pytest.raises(ValueError, match="Plane API 404"):
            await runtime.tool(
                worker,
                {"action": "download_asset", "args": {"asset_id": str(foreign_asset.pk), "path": "/tmp/foreign.txt"}},
            )

    asyncio.run(exercise())
    worker.run.refresh_from_db()
    assert worker.run.spec_revision == 1
    runtime.clients["local"].request.assert_awaited_once_with(
        "fs/writeFile", {"path": "/tmp/requirements.txt", "dataBase64": "TmV3IHJlcXVpcmVtZW50cw=="}
    )


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
@pytest.mark.parametrize("delivery", ["live", "history"])
@pytest.mark.parametrize("partial", [False, True])
def test_reopened_task_retains_old_chat_output_without_rebinding_new_chat(runtime_case, execution, delivery, partial):
    from plane.coordination.codex.chat import assistant_message, ensure_conversation
    from plane.db.models import State
    from plane.db.models.codex import AgentChatMessage

    runtime, worker, old_chat, issue = runtime_case
    _, human, project, _, _, _, _, _, _, prefix = execution
    conversation = ensure_conversation(issue)
    conversation.chat = old_chat
    conversation.save()
    if partial:
        assistant_message(old_chat, "previous-turn", "previous-reply", "Partial reply", final=False)
    session = prefix.replace("/api/v1/", "/api/", 1)
    assert human.post(session + f"runs/{worker.run_id}/cancel/", {}, format="json").status_code == 200
    state = State.objects.get(project=project, name="Todo")
    assert (
        human.patch(
            f"/api/workspaces/{project.workspace.slug}/projects/{project.pk}/issues/{issue.pk}/",
            {"state_id": str(state.pk)},
            format="json",
        ).status_code
        == 200
    )
    new_run = claim(execution)
    new_chat = CodexThread.objects.create(
        project=project,
        run_id=new_run,
        role="chat",
        host_id="mac",
        runtime_id="local",
        thread_id="new-chat",
        state="idle",
    )
    conversation.chat = new_chat
    conversation.save(update_fields=["chat"])
    item = {"type": "agentMessage", "id": "previous-reply", "text": "Completed reply from the previous session"}

    async def deliver():
        if delivery == "live":
            await runtime.notification(
                "local",
                {
                    "method": "item/completed",
                    "params": {"threadId": old_chat.thread_id, "turnId": "previous-turn", "item": item},
                },
            )
        else:
            await runtime.reconcile_turn(old_chat, {"id": "previous-turn", "status": "completed", "items": [item]})

    asyncio.run(deliver())
    asyncio.run(deliver())
    conversation.refresh_from_db()
    assert conversation.chat_id == new_chat.pk
    message = AgentChatMessage.objects.get(conversation=conversation)
    assert message.thread_id == old_chat.pk and message.body == item["text"] and message.state == "completed"
    saved = human.get(session + f"work-items/{issue.pk}/conversation/")
    assert saved.status_code == 200 and saved.data["messages"][0]["body"] == item["text"]
    identifier = str(uuid4())
    assert (
        human.post(
            session + f"work-items/{issue.pk}/conversation/",
            {"id": identifier, "body": "Continue the new attempt", "assets": []},
            format="json",
        ).status_code
        == 201
    )
    assert CodexCommand.objects.get(pk=identifier).thread_id == new_chat.pk


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
    runtime.assignments[str(worker.run_id)] = "private-assignment-key"
    asyncio.run(runtime.pump())
    message.refresh_from_db()
    assert message.state == "cancelled"
    assert chat.commands.get(pk=message.pk).state == "cancelled"
    assert str(worker.run_id) not in runtime.credentials
    assert str(worker.run_id) not in runtime.assignments


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


@pytest.mark.django_db(transaction=True)
def test_reconnect_restores_final_reply_when_completion_arrived_without_final_item(runtime_case):
    from plane.coordination.codex.chat import assistant_message, ensure_conversation
    from plane.db.models.codex import AgentChatMessage

    runtime, _, chat, issue = runtime_case
    conversation = ensure_conversation(issue)
    conversation.chat = chat
    conversation.save()
    assistant_message(chat, "finishing", "reply", "Partial", final=False)
    final_turn = {
        "id": "finishing",
        "status": "completed",
        "items": [{"type": "agentMessage", "id": "reply", "text": "The complete reply recovered from Codex"}],
    }

    async def history(thread_id):
        if thread_id == chat.thread_id:
            await runtime.notification(
                "local",
                {
                    "method": "turn/completed",
                    "params": {"threadId": chat.thread_id, "turn": {"id": "finishing", "status": "completed"}},
                },
            )
            yield final_turn

    runtime.clients["local"].history = history
    runtime.clients["local"].request.return_value = {"thread": {"status": {"type": "idle"}}}
    asyncio.run(runtime.reconnect("local"))
    message = AgentChatMessage.objects.get(conversation=conversation)
    assert message.state == "completed" and message.body == final_turn["items"][0]["text"]
    conversation.refresh_from_db()
    revision = conversation.revision
    asyncio.run(runtime.reconcile_turn(chat, final_turn))
    asyncio.run(
        runtime.reconcile_turn(
            chat,
            {
                "id": "finishing",
                "status": "inProgress",
                "items": [{"type": "agentMessage", "id": "reply", "text": "Partial"}],
            },
        )
    )
    conversation.refresh_from_db()
    assert conversation.revision == revision
    assert AgentChatMessage.objects.get(conversation=conversation).body == message.body
    assert all(call.args[0] != "turn/start" for call in runtime.clients["local"].request.await_args_list)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("final_status", ["completed", "failed"])
def test_stale_reconnect_history_does_not_regress_finished_chat_delivery(runtime_case, final_status):
    from plane.coordination.codex.chat import ensure_conversation, post_message

    runtime, _, chat, issue = runtime_case
    conversation = ensure_conversation(issue)
    conversation.chat = chat
    conversation.save()
    message = post_message(issue, issue.created_by, uuid4(), "Check the implementation", [])
    command = chat.commands.get(pk=message.pk)
    command.state, command.turn_id = "sent", "finishing"
    command.save(update_fields=["state", "turn_id"])
    message.state, message.turn_id = "sent", "finishing"
    message.save(update_fields=["state", "turn_id"])

    async def history(thread_id):
        if thread_id == "chat":
            # The history response was captured before this live completion arrived.
            await runtime.notification(
                "local",
                {
                    "method": "turn/completed",
                    "params": {"threadId": "chat", "turn": {"id": "finishing", "status": final_status}},
                },
            )
            yield {
                "id": "finishing",
                "status": "inProgress",
                "items": [{"type": "userMessage", "clientId": str(message.pk)}],
            }

    runtime.clients["local"].history = history
    runtime.clients["local"].request.return_value = {"thread": {"status": {"type": "idle"}}}
    asyncio.run(runtime.reconnect("local"))
    command.refresh_from_db()
    message.refresh_from_db()
    chat.refresh_from_db()
    assert command.state == final_status
    assert message.state == final_status
    assert chat.state == "idle" and chat.active_turn == ""
    assert all(call.args[0] != "turn/start" for call in runtime.clients["local"].request.await_args_list)


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
@pytest.mark.parametrize("failure_stage", ["reservation", "storage", "confirmation"])
def test_file_sharing_resumes_partial_upload_without_duplicate_assets(runtime_api_case, monkeypatch, failure_stage):
    import httpx
    from asgiref.sync import sync_to_async
    from plane.coordination.codex.chat import ensure_conversation
    from plane.db.models import FileAsset
    from plane.db.models.codex import AgentChatMessage

    runtime, worker, chat, issue = runtime_api_case
    conversation = ensure_conversation(issue)
    conversation.chat = chat
    conversation.save()
    identifier = str(uuid4())
    values = {"message_id": identifier, "path": "/tmp/verification.txt", "text": "Verification notes"}
    runtime.clients["local"].request.return_value = {"dataBase64": "cHJldmlldw=="}
    storage = Mock()
    storage.generate_presigned_post.return_value = {"url": "http://storage.test/upload", "fields": {}}
    monkeypatch.setattr("plane.api.views.issue.S3Storage", Mock(return_value=storage))
    api_call = runtime.api_call
    failed = False

    async def interrupted_call(project, method, path, data=None, token=None):
        nonlocal failed
        result = await api_call(project, method, path, data, token)
        if not failed and (
            (failure_stage == "reservation" and method == "POST" and path.endswith("/attachments/"))
            or (failure_stage == "confirmation" and method == "PATCH")
        ):
            failed = True
            raise ConnectionError("Upload response lost after the API committed")
        return result

    async def upload(*args, **kwargs):
        nonlocal failed
        status = 204
        if failure_stage == "storage" and not failed:
            failed, status = True, 503
        return httpx.Response(status, request=httpx.Request("POST", args[0]))

    runtime.api_call = interrupted_call
    runtime.api.post = AsyncMock(side_effect=upload)

    async def exercise():
        await runtime.tool(worker, {"action": "acknowledge_spec", "args": {"expected_revision": 2}})
        with pytest.raises((ConnectionError, httpx.HTTPStatusError)):
            await runtime.tool(worker, {"action": "share_file", "args": values})
        assert await sync_to_async(FileAsset.objects.filter(external_id=identifier).count)() == 1
        assert not await sync_to_async(AgentChatMessage.objects.filter(pk=identifier).exists)()
        runtime.credentials.clear()
        result = await runtime.tool(worker, {"action": "share_file", "args": values})
        assert result["shared"] and result["message_id"] == identifier
        assert (await runtime.tool(worker, {"action": "share_file", "args": values}))["shared"]

    asyncio.run(exercise())
    asset = FileAsset.objects.get(external_id=identifier)
    assert asset.is_uploaded and str(asset.agent_run_id) == str(worker.run_id)
    message = AgentChatMessage.objects.get(pk=identifier)
    assert message.body == values["text"] and list(message.assets.all()) == [asset]
    assert runtime.api.post.await_count == (2 if failure_stage == "storage" else 1)
    assert runtime.clients["local"].request.await_count == (1 if failure_stage == "confirmation" else 2)
    assert {call.kwargs["object_name"] for call in storage.generate_presigned_post.call_args_list} == {asset.asset.name}


@pytest.mark.django_db
@pytest.mark.parametrize("scope", ["other_issue", "human_upload", "other_run", "deleted", "revoked_key"])
def test_upload_resume_preserves_execution_ownership_checks(execution, monkeypatch, scope):
    from plane.db.models import APIToken, FileAsset, Issue
    from .test_coordination_execution import worker_client

    _, _, project, issue, _, _, _, worker, _, _ = execution
    run_id = claim(execution)
    client, _ = worker_client(execution, run_id)
    asset = FileAsset(
        project=project,
        workspace=project.workspace,
        issue=issue,
        agent_run_id=run_id,
        created_by=worker,
        entity_type=FileAsset.EntityTypeContext.ISSUE_ATTACHMENT,
        asset="synthetic/verification.txt",
        size=7,
        attributes={"name": "verification.txt", "type": "text/plain", "size": 7},
    )
    if scope == "other_issue":
        asset.issue = Issue.objects.create(project=project, name="Not assigned")
    elif scope == "human_upload":
        asset.created_by = issue.created_by
    elif scope == "other_run":
        asset.agent_run_id = None
    elif scope == "deleted":
        asset.is_deleted = True
    else:
        APIToken.objects.filter(agent_run_id=run_id, purpose="execution").update(is_active=False)
    asset.save(disable_auto_set_user=True)
    storage = Mock()
    monkeypatch.setattr("plane.api.views.issue.S3Storage", storage)
    path = (
        f"/api/v1/workspaces/{project.workspace.slug}/projects/{project.pk}/"
        f"work-items/{issue.pk}/attachments/{asset.pk}/"
    )
    response = client.post(path, {}, format="json")
    assert response.status_code == (404 if scope == "deleted" else 403), response.data
    storage.assert_not_called()
    asset.refresh_from_db()
    assert not asset.is_uploaded


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
@pytest.mark.parametrize("final_status", ["failed", "interrupted"])
def test_stale_history_does_not_reopen_interrupted_assistant_output(runtime_case, final_status):
    from plane.coordination.codex.chat import assistant_message, ensure_conversation
    from plane.db.models.codex import AgentChatMessage

    runtime, _, chat, issue = runtime_case
    conversation = ensure_conversation(issue)
    conversation.chat = chat
    conversation.save()
    assistant_message(chat, "turn", "partial", "Latest partial reply", final=False)
    stale = {
        "id": "turn",
        "status": "inProgress",
        "items": [{"type": "agentMessage", "id": "partial", "text": "Older partial"}],
    }
    asyncio.run(
        runtime.notification(
            "local",
            {
                "method": "turn/completed",
                "params": {"threadId": chat.thread_id, "turn": {"id": "turn", "status": final_status}},
            },
        )
    )
    asyncio.run(runtime.reconcile_turn(chat, stale))
    message = AgentChatMessage.objects.get(conversation=conversation)
    assert message.state == "failed" and message.body == "Latest partial reply"
    terminal = {
        "id": "turn",
        "status": final_status,
        "items": [{"type": "agentMessage", "id": "partial", "text": "Recovered incomplete reply"}],
    }
    asyncio.run(runtime.reconcile_turn(chat, terminal))
    message.refresh_from_db()
    assert message.state == final_status and message.body == "Recovered incomplete reply"


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
