from uuid import uuid4

import pytest

from plane.db.models import FileAsset, IssueActivity, IssueComment, State
from .test_coordination_contracts import coordination as coordination


@pytest.mark.django_db
def test_chat_is_private_to_project_and_not_an_activity_comment(coordination):
    client, project, issue, prefix = coordination
    url = prefix + f"work-items/{issue.pk}/conversation/"
    before = (IssueComment.objects.count(), IssueActivity.objects.count())
    message_id = str(uuid4())
    payload = {"id": message_id, "body": "Please explain your approach", "assets": []}
    first = client.post(url, payload, format="json")
    assert first.status_code == 409  # no configured Codex chat yet
    from plane.coordination.codex.chat import ensure_conversation, post_message
    from plane.db.models.codex import CodexThread

    thread = CodexThread.objects.create(project=project, role="chat", host_id="mac", runtime_id="local")
    conversation = ensure_conversation(issue)
    conversation.chat = thread
    conversation.save()
    first = client.post(url, payload, format="json")
    assert first.status_code == 201, first.data
    assert client.post(url, payload, format="json").data["id"] == message_id
    assert client.post(url, {**payload, "body": "Different"}, format="json").status_code == 409
    assert len(client.get(url).data["messages"]) == 1
    assert before == (IssueComment.objects.count(), IssueActivity.objects.count())
    assert post_message  # public helper used by runtime and HTTP endpoint
    client.logout()
    assert client.get(url).status_code in (401, 403)


@pytest.mark.django_db
def test_chat_rejects_foreign_assets_and_closed_task(coordination):
    client, project, issue, prefix = coordination
    from plane.coordination.codex.chat import ensure_conversation
    from plane.db.models.codex import CodexThread

    conversation = ensure_conversation(issue)
    conversation.chat = CodexThread.objects.create(project=project, role="chat", host_id="mac", runtime_id="local")
    conversation.save()
    url = prefix + f"work-items/{issue.pk}/conversation/"
    asset = FileAsset.objects.create(project=project, workspace=project.workspace, is_uploaded=True)
    payload = {"id": str(uuid4()), "body": "See attachment", "assets": [str(asset.pk)]}
    assert client.post(url, payload, format="json").status_code == 400
    done = State.objects.create(project=project, name="Cancelled", group="cancelled", color="#777777")
    issue.state = done
    issue.save(disable_auto_set_user=True)
    assert client.post(url, {**payload, "assets": []}, format="json").status_code == 409
    assert client.get(url).data["closed"] is True


@pytest.mark.django_db
def test_assistant_final_replaces_stream_and_deduplicates(coordination):
    from plane.coordination.codex.chat import assistant_message, ensure_conversation, snapshot
    from plane.db.models.codex import CodexThread

    _, project, issue, _ = coordination
    conversation = ensure_conversation(issue)
    thread = CodexThread.objects.create(project=project, role="chat", host_id="mac", runtime_id="local")
    conversation.chat = thread
    conversation.save()
    assistant_message(thread, "turn", "item", "Hel", final=False)
    assistant_message(thread, "turn", "item", "Hello", final=True)
    assistant_message(thread, "turn", "item", "Hello", final=True)
    messages = snapshot(conversation)["messages"]
    assert len(messages) == 1
    assert messages[0]["body"] == "Hello"
    assert messages[0]["state"] == "completed"


def test_manifest_requires_location_and_loads_instruction_layers(tmp_path):
    from plane.coordination.codex.config import load_config
    import json

    (tmp_path / "AGENTS.md").write_text("Common rules")
    (tmp_path / "chat.md").write_text("Chat rules")
    manifest = {
        "runtimes": {"local": {"host": "mac", "url": "ws://127.0.0.1:18431"}},
        "projects": [
            {
                "project_id": str(uuid4()),
                "runtime": "local",
                "workspace": "test",
                "dispatcher_key_env": "PLANE_KEY",
                "worker_id": str(uuid4()),
                "repository": "example/product",
                "base_branch": "main",
                "instructions": ["AGENTS.md"],
                "placement": {
                    role: {"cwd": "/tmp/test", "instructions": ["chat.md"]}
                    for role in ("overseer", "implementation", "chat")
                },
            }
        ],
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(manifest))
    config = load_config(path)
    assert config.projects[0].placement("chat")["instructions"] == "Common rules\n\nChat rules"
    assert config.projects[0].placement("chat")["model"] == "gpt-6.1-sol"
    assert config.projects[0].placement("chat")["config"]["model_reasoning_effort"] == "max"
    assert {config.projects[0].placement(role)["host_id"] for role in ("overseer", "chat", "implementation")} == {"mac"}
    manifest["projects"][0]["placement"]["chat"]["runtime"] = "elsewhere"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="one runtime for the project"):
        load_config(path)
    del manifest["projects"][0]["placement"]["chat"]["runtime"]
    del manifest["runtimes"]["local"]["host"]
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        load_config(path)


@pytest.mark.django_db
def test_browser_eventsource_accept_header_is_supported(coordination):
    client, _, issue, prefix = coordination
    response = client.get(prefix + f"work-items/{issue.pk}/conversation/stream/", HTTP_ACCEPT="text/event-stream")
    assert response.status_code == 200
    assert response.streaming
    assert response["Content-Type"].startswith("text/event-stream")
    assert response["Content-Encoding"] == "identity"
    assert "no-transform" in response["Cache-Control"]


@pytest.mark.django_db
def test_shared_agent_file_is_a_chat_card_not_a_comment(coordination):
    from plane.coordination.codex.chat import ensure_conversation, share_file, snapshot
    from plane.db.models.codex import CodexThread

    _, project, issue, _ = coordination
    conversation = ensure_conversation(issue)
    worker = CodexThread.objects.create(project=project, role="implementation", host_id="soso", runtime_id="remote")
    asset = FileAsset.objects.create(
        project=project,
        workspace=project.workspace,
        issue=issue,
        is_uploaded=True,
        entity_type=FileAsset.EntityTypeContext.ISSUE_ATTACHMENT,
        attributes={"name": "screenshot.png"},
    )
    identifier = uuid4()
    before = IssueComment.objects.count()
    share_file(worker, identifier, asset, "Implementation screenshot")
    share_file(worker, identifier, asset, "Implementation screenshot")
    messages = snapshot(conversation)["messages"]
    assert len(messages) == 1 and messages[0]["assets"][0]["id"] == str(asset.pk)
    assert IssueComment.objects.count() == before


@pytest.mark.django_db
def test_stream_reuses_one_notification_iterator_for_successive_updates(coordination, monkeypatch):
    import asyncio
    from plane.coordination.codex import views

    class Listener:
        calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, query):
            assert query.startswith("LISTEN")

        async def notifies(self):
            self.calls += 1
            assert self.calls == 1
            yield object()
            yield object()

    listener = Listener()

    async def connect(**kwargs):
        return listener

    monkeypatch.setattr(views.psycopg.AsyncConnection, "connect", connect)
    monkeypatch.setattr(views.ConversationStreamEndpoint, "conversation", lambda *args: object())
    monkeypatch.setattr(views, "snapshot", lambda conversation: {"received": True})
    client, _, issue, prefix = coordination
    response = client.get(prefix + f"work-items/{issue.pk}/conversation/stream/", HTTP_ACCEPT="text/event-stream")

    async def consume():
        return [chunk async for chunk in response.streaming_content]

    assert len(asyncio.run(consume())) == 3
    assert listener.calls == 1
