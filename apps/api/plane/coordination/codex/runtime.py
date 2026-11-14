import asyncio
import json
import logging
import base64
import mimetypes
from pathlib import PurePosixPath

import httpx
import psycopg
from asgiref.sync import sync_to_async
from django.db import transaction
from django.db.models import Q
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import SAFE_METHODS

from plane.db.models import AgentRun, CoordinationEvent, Project, FileAsset
from plane.db.models.api import hash_api_token
from plane.db.models.codex import AgentConversation, AgentChatMessage, CodexCommand, CodexProject, CodexThread
from plane.coordination.notifications import WORKER_EVENT_TYPES
from .chat import assistant_message, ensure_conversation, is_closed, share_file
from .client import CodexClient, RPCError
from .signals import CHANNEL, database_params, wake


logger = logging.getLogger(__name__)
db = sync_to_async
TOOLS = [
    {
        "type": "function",
        "name": "plane",
        "description": (
            "Plane coordination. Actions: dispatch (overseer, args.issue_id); task_api (args.method, path, data; "
            "path relative to project's /api/v1/workspaces/slug/projects/id/); worker_status (observe only); "
            "send_to_worker / send_to_chat (args.text, args.message_id UUID for deduplication); "
            "inbox (read queued/delivered coordination messages); acknowledge (args.message_id). "
            "read_spec (current specification plus task details); "
            "acknowledge_spec (implementation only, args.expected_revision). "
            "share_file (args.path absolute on your host, args.message_id UUID, args.text caption): "
            "upload a screenshot/document from your host as a work-item attachment and display it in chat. "
            "download_asset (args.asset_id, args.path absolute on your host): retrieve a task attachment "
            "to your local filesystem without putting binary content or storage credentials in model context. "
            "Sending to a busy worker queues a later turn, never interrupts it. "
            "Use task_api to read requirements, attachments, report progress, ask formal questions and use merge APIs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"action": {"type": "string"}, "args": {"type": "object"}},
            "required": ["action", "args"],
        },
    }
]
ROLE_RULES = {
    "overseer": "You oversee this Plane project. On task.ready, call plane dispatch with issue_id. "
    "Dispatch once, then return. Do not implement or monitor handed-off tasks; workers receive feedback directly.",
    "implementation": "Implement this work item end to end. Read requirements, definition of done and attachments "
    "using plane task_api. Ask humans when ambiguous. At checkpoints read plane inbox and acknowledge "
    "messages you handle. Report meaningful progress to the chat thread with send_to_chat. "
    "Keep casual chat out of issue comments. Use existing review/merge APIs and never infer approval from chat.",
    "chat": "You converse with humans about this work item. You are not the implementation worker. "
    "Read worker_status and task_api to answer accurately. Never change source, deploy or merge. "
    "Forward actionable human feedback via send_to_worker with a stable message_id. This queues work without "
    "interrupting a busy worker. Clearly distinguish queued feedback from acknowledged changes. "
    "Internal coordination messages are not human messages; do not automatically forward them back. "
    "Share artifacts as work-item attachments, not host-local filesystem download links. "
    "Do not expose private tool output, credentials or reasoning in public replies.",
}


class Runtime:
    def __init__(self, config):
        self.config = config
        self.projects = {item.data["project_id"]: item for item in config.projects}
        self.woken = asyncio.Event()
        self.events = asyncio.Queue()
        self.credentials = {}
        self.assignments = {}
        self.credential_locks = {}
        self.api = httpx.AsyncClient(base_url=config.api_url, timeout=None, follow_redirects=False)
        self.clients = {
            name: CodexClient(
                value,
                lambda message, connection, name=name: self.events.put((name, message, connection)),
                lambda name=name: self.reconnect(name),
                lambda name=name: self.offline(name),
            )
            for name, value in config.runtimes.items()
        }

    def project(self, thread):
        return self.projects[str(thread.project_id)]

    async def api_call(self, project, method, path, data=None, token=None):
        if path.startswith("/") or ".." in path or ":" in path:
            raise ValueError("Use a relative project API path")
        prefix = f"/api/v1/workspaces/{project.data['workspace']}/projects/{project.data['project_id']}/"
        response = await self.api.request(method, prefix + path, json=data, headers={"X-API-Key": token or project.key})
        if response.is_error:
            # Preserve API validation details, never request headers or tokens.
            raise ValueError(f"Plane API {response.status_code}: {response.text}")
        return response.json() if response.content else None

    async def setup(self):
        for config in self.config.projects:
            project = await db(Project.objects.get)(
                pk=config.data["project_id"], workspace__slug=config.data["workspace"]
            )
            await db(CodexProject.objects.get_or_create)(project=project)
            overseer = await self.binding(config, project, "overseer")
            if await db(
                CodexThread.objects.filter(project=project)
                .exclude(state="archived")
                .exclude(host_id=overseer.host_id, runtime_id=overseer.runtime_id)
                .exists
            )():
                raise ValueError("Project threads span runtimes. Explicitly migrate or archive them before starting.")
            for issue_id in await self.pending_handoffs(config, project):
                await self.dispatch(overseer, issue_id)

    @db
    def pending_handoffs(self, config, project):
        from plane.coordination.execution import validate_live_run

        runs = AgentRun.objects.filter(
            project=project,
            agent_id=config.data["worker_id"],
            dispatcher__token=hash_api_token(config.key),
            status__in=["active", "merging"],
        ).select_related("contract__issue__state", "project__workspace", "agent", "dispatcher__user")
        pending = []
        for run in runs:
            if run.worker_id != f"codex:{run.contract.issue_id}":
                continue
            threads = {thread.role: thread for thread in CodexThread.objects.filter(run=run)}
            worker, chat = threads.get("implementation"), threads.get("chat")
            if (
                run.handed_off_at
                and worker
                and chat
                and worker.commands.filter(event__isnull=True, sender__isnull=True, message__isnull=True).exists()
                and AgentConversation.objects.filter(issue_id=run.contract.issue_id, chat=chat).exists()
            ):
                continue
            try:
                validate_live_run(run, assignment=bool(run.handed_off_at))
            except PermissionDenied:
                continue
            pending.append(run.contract.issue_id)
        return pending

    async def binding(self, config, project, role, run=None):
        placement = config.placement(role)
        host_id, runtime_id = placement.pop("host_id"), placement.pop("runtime_id")
        thread, _ = await db(CodexThread.objects.get_or_create)(
            project=project,
            role=role,
            run=run,
            defaults={
                "host_id": host_id,
                "runtime_id": runtime_id,
                "settings": placement,
            },
        )
        runtime = self.config.runtimes.get(thread.runtime_id)
        if not runtime or runtime["host"] != thread.host_id:
            raise ValueError("Existing session runtime/host is unavailable; explicit migration is required")
        if thread.host_id != host_id or thread.runtime_id != runtime_id:
            raise ValueError("A project cannot move hosts through configuration edits; migrate its sessions explicitly")
        if role in ("overseer", "chat") and not await db(thread.commands.exists)():
            await db(CodexCommand.objects.create)(
                thread=thread,
                text=(
                    "Initialize this persistent session. Acknowledge your role briefly, then await future messages. "
                    "Do not dispatch or change files until a work-item event or human message arrives."
                ),
            )
        if not thread.thread_id and self.clients[thread.runtime_id].socket:
            await self.start(thread)
        return thread

    async def start(self, thread):
        settings = thread.settings
        context = f"\nPlane project: {thread.project_id}."
        if thread.run_id:
            issue_id = await db(lambda: AgentRun.objects.get(pk=thread.run_id).contract.issue_id)()
            context += f" Run: {thread.run_id}. Work item: {issue_id}. "
            context += f"Task context API: coordination/work-items/{issue_id}/overview/."
        params = {
            "cwd": settings["cwd"],
            "developerInstructions": settings["instructions"] + "\n\n" + ROLE_RULES[thread.role] + context,
            "config": settings["config"],
            "dynamicTools": TOOLS,
        }
        if settings.get("model"):
            params["model"] = settings["model"]
        result = await self.clients[thread.runtime_id].request("thread/start", params)
        thread.thread_id, thread.state = result["thread"]["id"], "idle"
        await db(thread.save)()

    async def reconnect(self, runtime_id):
        threads = await db(list)(
            CodexThread.objects.filter(runtime_id=runtime_id, project_id__in=self.projects).exclude(state="archived")
        )
        for thread in threads:
            await db(CodexThread.objects.filter(pk=thread.pk).update)(state="offline")
            try:
                if not thread.thread_id:
                    await self.start(thread)
                    continue
                client = self.clients[runtime_id]
                resume = {"threadId": thread.thread_id, "config": thread.settings.get("config", {})}
                if thread.settings.get("model"):
                    resume["model"] = thread.settings["model"]
                await client.request("thread/resume", resume)
                async for turn in client.history(thread.thread_id):
                    await self.reconcile_turn(thread, turn)
                    if turn["status"] == "inProgress":
                        thread.active_turn = turn["id"]
                result = await client.request("thread/read", {"threadId": thread.thread_id})
                state = result["thread"]["status"]["type"]
                thread.state = "busy" if state == "active" else "idle"
                if thread.state == "idle":
                    thread.active_turn = ""
                thread.error = ""
                unresolved = thread.commands.filter(state__in=["submitting", "uncertain"])
                if await db(unresolved.exists)():
                    await db(unresolved.update)(state="uncertain")
                    thread.state = "needs_attention"
                    thread.error = (
                        "A turn submission could not be reconciled. Operator attention is required before retrying."
                    )
                # Live turn notifications take precedence over an older history/status response.
                await db(CodexThread.objects.filter(pk=thread.pk, state="offline").update)(
                    state=thread.state, active_turn=thread.active_turn, error=thread.error
                )
            except (RPCError, ValueError) as error:
                thread.state, thread.error = "needs_attention", str(error)
                await db(thread.save)(update_fields=["state", "error"])
        await db(wake)()
        self.woken.set()

    async def offline(self, runtime_id):
        await db(CodexThread.objects.filter(runtime_id=runtime_id).exclude(state="archived").update)(state="offline")
        await db(wake)()

    async def reconcile_turn(self, thread, turn):
        for item in turn.get("items", []):
            if item["type"] == "userMessage" and item.get("clientId"):
                state = (
                    "completed"
                    if turn["status"] == "completed"
                    else ("sent" if turn["status"] == "inProgress" else "failed")
                )
                # History captured before a live completion must not reopen a settled delivery.
                pending_states = ["queued", "submitting", "sent", "uncertain"]
                await db(thread.commands.filter(pk=item["clientId"], state__in=pending_states).update)(
                    turn_id=turn["id"], state=state
                )
                await db(AgentChatMessage.objects.filter(pk=item["clientId"], state__in=pending_states).update)(
                    turn_id=turn["id"], state=state
                )
            if thread.role == "chat" and item["type"] == "agentMessage":
                state = "streaming" if turn["status"] == "inProgress" else turn["status"]
                await db(assistant_message)(
                    thread, turn["id"], item["id"], item["text"], final=state == "completed", replace=True, state=state
                )

    async def dispatch(self, thread, issue_id):
        if thread.role != "overseer":
            raise ValueError("Only the project overseer dispatches new work")
        config = self.project(thread)
        prefix = "coordination/"
        run = await db(
            lambda: AgentRun.objects.filter(
                project_id=thread.project_id, contract__issue_id=issue_id, status__in=["active", "merging"]
            ).first()
        )()
        if not run:
            response = await self.api_call(
                config,
                "POST",
                prefix + f"work-items/{issue_id}/claim/",
                {
                    "agent": config.data["worker_id"],
                    "worker_id": f"codex:{issue_id}",
                    "repository": config.data["repository"],
                    "base_branch": config.data["base_branch"],
                },
            )
            run = await db(AgentRun.objects.get)(pk=response["id"])
        # GET verifies the dispatcher still owns this run even after a partial dispatch.
        await self.api_call(config, "GET", prefix + f"runs/{run.pk}/")
        if not run.handed_off_at:
            await self.assignment_token(config, run.pk)
        project = await db(Project.objects.get)(pk=thread.project_id)
        worker = await self.binding(config, project, "implementation", run)
        chat = await self.binding(config, project, "chat", run)

        @db
        @transaction.atomic
        def save():
            conversation = ensure_conversation(run.contract.issue)
            conversation.chat = chat
            conversation.save(update_fields=["chat"])
            if not worker.commands.filter(event__isnull=True, sender__isnull=True, message__isnull=True).exists():
                CodexCommand.objects.create(
                    thread=worker,
                    text=f"Implement Plane work item {issue_id}. Run {run.pk}. "
                    "Start by reading the task with plane task_api. "
                    f"API path: coordination/work-items/{issue_id}/overview/",
                )
            wake()

        await save()
        self.woken.set()
        return {"run": str(run.pk), "implementation": self.address(worker), "chat": self.address(chat)}

    @staticmethod
    def address(thread):
        return {"host_id": thread.host_id, "runtime_id": thread.runtime_id, "thread_id": thread.thread_id}

    def credential_lock(self, run_id):
        return self.credential_locks.setdefault(str(run_id), asyncio.Lock())

    async def assignment_token(self, project, run_id):
        identifier = str(run_id)
        async with self.credential_lock(identifier):
            if identifier not in self.assignments:
                response = await self.api_call(project, "POST", f"coordination/runs/{run_id}/handoff/", {})
                self.assignments[identifier] = response["token"]
                self.credentials.pop(identifier, None)
            return self.assignments[identifier]

    async def task_token(self, thread, *, read_only=False):
        run = await db(AgentRun.objects.select_related("contract").get)(pk=thread.run_id)
        if run.spec_revision != run.contract.spec_revision:
            if read_only:
                return await self.assignment_token(self.project(thread), run.pk)
            raise ValueError(
                "Specification changed. Read the current specification with read_spec, then explicitly use "
                "acknowledge_spec with args.expected_revision before continuing implementation."
            )
        async with self.credential_lock(run.pk):
            cached = self.credentials.get(str(run.pk))
            # A waiting lookup may predate another thread's completed acknowledgement.
            if cached and cached[0] >= run.spec_revision:
                return cached[1]
            response = await self.api_call(self.project(thread), "POST", f"coordination/runs/{run.pk}/credentials/", {})
            self.credentials[str(run.pk)] = (run.spec_revision, response["token"])
            return response["token"]

    async def tool(self, thread, args):
        action, values = args["action"], args.get("args", {})
        if thread.run_id:
            from plane.coordination.execution import validate_live_run

            @db
            def validate():
                run = AgentRun.objects.get(pk=thread.run_id)
                validate_live_run(run, assignment=True)

            await validate()
        if action == "dispatch":
            return await self.dispatch(thread, values["issue_id"])
        if action == "task_api":
            method = values.get("method", "GET").upper()
            token = await self.task_token(thread, read_only=method in SAFE_METHODS) if thread.run_id else None
            return await self.api_call(
                self.project(thread),
                method,
                values["path"],
                values.get("data"),
                token=token,
            )
        if action in ("read_spec", "acknowledge_spec") and thread.run_id:
            run = await db(AgentRun.objects.select_related("contract").get)(pk=thread.run_id)
            if action == "read_spec":
                token = await self.task_token(thread, read_only=True)
                specification = await self.api_call(
                    self.project(thread),
                    "GET",
                    f"coordination/work-items/{run.contract.issue_id}/contract/",
                    token=token,
                )
                task = await self.api_call(
                    self.project(thread), "GET", f"work-items/{run.contract.issue_id}/", token=token
                )
                return {**specification, "task": task}
            if thread.role != "implementation":
                raise ValueError("Only the implementation thread acknowledges specification changes")
            assignment = await self.assignment_token(self.project(thread), run.pk)
            async with self.credential_lock(run.pk):
                response = await self.api_call(
                    self.project(thread),
                    "POST",
                    f"coordination/runs/{run.pk}/refresh/",
                    {"expected_revision": values["expected_revision"]},
                    token=assignment,
                )
                self.credentials[str(run.pk)] = (values["expected_revision"], response["token"])
            return {"acknowledged_revision": values["expected_revision"]}
        if not thread.run_id:
            raise ValueError("This tool needs an assigned work item")
        if action == "share_file":
            return await self.upload_file(thread, values)
        if action == "download_asset":
            return await self.download_file(thread, values)
        if action == "worker_status":
            worker = await db(CodexThread.objects.get)(run_id=thread.run_id, role="implementation")
            result = {**self.address(worker), "state": worker.state, "error": worker.error}
            result["feedback"] = await db(list)(worker.commands.filter(sender=thread).values("id", "text", "state"))
            if worker.thread_id and self.clients[worker.runtime_id].socket:
                result["thread"] = (
                    await self.clients[worker.runtime_id].request("thread/read", {"threadId": worker.thread_id})
                )["thread"]
            return result
        if action in ("send_to_worker", "send_to_chat"):
            from uuid import UUID

            identifier = UUID(values["message_id"])
            role = "implementation" if action == "send_to_worker" else "chat"
            target = await db(CodexThread.objects.get)(run_id=thread.run_id, role=role)
            command, created = await db(CodexCommand.objects.get_or_create)(
                id=identifier, defaults={"thread": target, "sender": thread, "text": values["text"]}
            )
            if command.thread_id != target.pk or command.sender_id != thread.pk or command.text != values["text"]:
                raise ValueError("Message identifier conflicts with an existing message")
            if created:
                self.woken.set()
            return {"message_id": str(command.pk), "state": command.state}
        if action == "inbox":
            return await db(list)(
                thread.commands.filter(Q(sender__isnull=False) | Q(event__isnull=False))
                .values("id", "text", "state")
                .exclude(state__in=["acknowledged", "cancelled"])
            )
        if action == "acknowledge":
            updated = await db(
                thread.commands.filter(pk=values["message_id"], state__in=["queued", "sent", "completed"]).update
            )(state="acknowledged")
            return {"acknowledged": bool(updated)}
        raise ValueError("Unknown Plane tool action")

    async def download_file(self, thread, values):
        path = PurePosixPath(values["path"])
        if not path.is_absolute():
            raise ValueError("Use an absolute destination path on the project's owning host")
        token = await self.task_token(thread, read_only=True)
        asset = await self.api_call(
            self.project(thread), "GET", f"coordination/runs/{thread.run_id}/assets/{values['asset_id']}/", token=token
        )
        try:
            response = await self.api.get(asset["download_url"], follow_redirects=True)
            response.raise_for_status()
        except httpx.HTTPError:
            raise ValueError("The work-item attachment could not be downloaded from storage") from None
        await self.clients[thread.runtime_id].request(
            "fs/writeFile",
            {
                "path": str(path),
                "dataBase64": base64.b64encode(response.content).decode(),
            },
        )
        return {"asset_id": values["asset_id"], "path": str(path)}

    async def upload_file(self, thread, values):
        from uuid import UUID

        identifier = UUID(values["message_id"])
        existing = await db(lambda: AgentChatMessage.objects.filter(pk=identifier, thread=thread).first())()
        if existing:
            return {"message_id": str(existing.pk), "shared": True}
        path = PurePosixPath(values["path"])
        if not path.is_absolute():
            raise ValueError("Use an absolute path on the owning Codex host")
        token = await self.task_token(thread)
        config = self.project(thread)
        issue_id = await db(lambda: AgentRun.objects.get(pk=thread.run_id).contract.issue_id)()
        prefix = f"work-items/{issue_id}/attachments/"
        asset_id = await db(
            lambda: (
                FileAsset.objects.filter(
                    agent_run_id=thread.run_id,
                    issue_id=issue_id,
                    external_id=str(identifier),
                    external_source="codex-conversation",
                    is_deleted=False,
                )
                .values_list("pk", flat=True)
                .first()
            )
        )()
        upload = await self.api_call(config, "POST", prefix + f"{asset_id}/", {}, token=token) if asset_id else None
        if upload is None or not upload["is_uploaded"]:
            result = await self.clients[thread.runtime_id].request("fs/readFile", {"path": str(path)})
            content = base64.b64decode(result["dataBase64"], validate=True)
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if upload is None:
                upload = await self.api_call(
                    config,
                    "POST",
                    prefix,
                    {
                        "name": path.name,
                        "type": mime,
                        "size": len(content),
                        "external_id": str(identifier),
                        "external_source": "codex-conversation",
                    },
                    token=token,
                )
            # Storage presigned fields, not the Plane credential, authorize this separate request.
            response = await self.api.post(
                upload["upload_data"]["url"],
                data=upload["upload_data"]["fields"],
                files={"file": (path.name, content, mime)},
            )
            response.raise_for_status()
            await self.api_call(config, "PATCH", prefix + f"{upload['asset_id']}/", {}, token=token)
        asset = await db(FileAsset.objects.get)(pk=upload["asset_id"], issue_id=issue_id, is_uploaded=True)
        message = await db(share_file)(thread, identifier, asset, values.get("text", ""))
        return {"message_id": str(message.pk), "asset_id": str(asset.pk), "shared": True}

    async def notifications(self):
        queues, workers = {}, set()

        async def consume(key, queue):
            try:
                while not queue.empty():
                    runtime_id, message, connection = queue.get_nowait()
                    try:
                        await self.notification(runtime_id, message, connection)
                    except Exception:
                        logger.exception("Codex event could not be processed")
                    finally:
                        self.events.task_done()
            finally:
                queues.pop(key, None)

        try:
            while True:
                runtime_id, message, connection = await self.events.get()
                key = (runtime_id, message.get("params", {}).get("threadId"))
                queue = queues.get(key)
                if queue is None:
                    queue = asyncio.Queue()
                    queues[key] = queue
                    worker = asyncio.create_task(consume(key, queue))
                    workers.add(worker)
                    worker.add_done_callback(workers.discard)
                queue.put_nowait((runtime_id, message, connection))
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    async def notification(self, runtime_id, message, connection=None):
        params = message.get("params", {})
        thread = await db(
            lambda: CodexThread.objects.filter(runtime_id=runtime_id, thread_id=params.get("threadId")).first()
        )()
        if not thread:
            return
        method = message.get("method")
        if "id" in message:
            client = self.clients[runtime_id]
            # Requests are connection-scoped; already received transcript notifications remain useful.
            if connection is None or connection is not client.socket:
                return
            if method != "item/tool/call" or params.get("tool") != "plane":
                error = "Codex requires operator input. Configure the host's approved unattended policy."
                await db(CodexThread.objects.filter(pk=thread.pk).update)(state="needs_attention", error=error)
                await client.reject(message["id"], error, connection=connection)
                await db(wake)()
                return
            result, success = None, True
            try:
                result = await self.tool(thread, params["arguments"])
            except Exception as error:
                result, success = str(error), False
            await client.reply(
                message["id"],
                {"contentItems": [{"type": "inputText", "text": json.dumps(result, default=str)}], "success": success},
                connection=connection,
            )
        elif method == "item/agentMessage/delta" and thread.role == "chat":
            await db(assistant_message)(thread, params["turnId"], params["itemId"], params["delta"], final=False)
        elif method == "item/completed" and params["item"]["type"] == "agentMessage" and thread.role == "chat":
            item = params["item"]
            await db(assistant_message)(thread, params["turnId"], item["id"], item["text"], final=True)
        elif method == "item/completed" and params["item"]["type"] == "userMessage":
            identifier = params["item"].get("clientId")
            if identifier:
                pending_states = ["queued", "submitting", "uncertain"]
                await db(thread.commands.filter(pk=identifier, state__in=pending_states).update)(
                    state="sent", turn_id=params["turnId"]
                )
                await db(AgentChatMessage.objects.filter(pk=identifier, state__in=pending_states).update)(
                    state="sent", turn_id=params["turnId"]
                )
                await db(wake)()
        elif method == "turn/started":
            if connection is not None and connection is not self.clients[runtime_id].socket:
                return
            await db(CodexThread.objects.filter(pk=thread.pk).exclude(state="archived").update)(
                state="busy", active_turn=params["turn"]["id"]
            )
            await db(wake)()
        elif method == "turn/completed":
            turn = params["turn"]
            state = "completed" if turn["status"] == "completed" else "failed"
            await db(thread.commands.filter(turn_id=turn["id"], state="sent").update)(state=state)
            await db(
                AgentChatMessage.objects.filter(
                    thread=thread,
                    turn_id=turn["id"],
                    state__in=["queued", "submitting", "sent", "uncertain", "streaming"],
                ).update
            )(state=state)
            if connection is None or connection is self.clients[runtime_id].socket:
                await db(CodexThread.objects.filter(pk=thread.pk).exclude(state="archived").update)(
                    state="idle",
                    active_turn="",
                    error=turn.get("error", {}).get("message", "") if turn.get("error") else "",
                )
                self.woken.set()
            await db(wake)()

    @db
    @transaction.atomic
    def collect_events(self):
        for config in self.config.projects:
            state = CodexProject.objects.select_for_update().get(project_id=config.data["project_id"])
            for event in CoordinationEvent.objects.filter(
                project=state.project, sequence__gt=state.event_cursor
            ).select_related("actor"):
                bindings = (
                    CodexThread.objects.filter(
                        run__targets__issue_id=event.issue_id,
                        run__status__in=["active", "merging"],
                        project=state.project,
                    )
                    .exclude(state="archived")
                    .distinct()
                )
                if event.kind == "task.ready" and not bindings.exists():
                    bindings = CodexThread.objects.filter(project=state.project, role="overseer")
                    pending = CodexCommand.objects.filter(
                        thread__in=bindings,
                        event__kind="task.ready",
                        event__issue_id=event.issue_id,
                        state__in=["queued", "submitting", "sent", "uncertain"],
                    )
                    latest_claim = (
                        CoordinationEvent.objects.filter(
                            project=state.project, issue_id=event.issue_id, kind="run.claimed"
                        )
                        .values_list("sequence", flat=True)
                        .last()
                    )
                    if latest_claim is not None:
                        # A fulfilled dispatch must not suppress a later reopening of the same specification.
                        pending = pending.filter(event__sequence__gt=latest_claim)
                    if pending.exists():
                        state.event_cursor = event.sequence
                        continue
                elif event.kind not in WORKER_EVENT_TYPES or (event.actor and event.actor.is_bot):
                    bindings = bindings.none()
                for thread in bindings:
                    CodexCommand.objects.get_or_create(
                        thread=thread,
                        event=event,
                        defaults={
                            "text": f"Plane event {event.kind}, issue {event.issue_id}, "
                            f"revision {event.spec_revision}. "
                            f"Payload: {json.dumps(event.payload)}. Read current work-item context before acting.",
                        },
                    )
                state.event_cursor = event.sequence
            state.save(update_fields=["event_cursor"])

    async def pump(self):
        await self.collect_events()
        threads = await db(list)(
            CodexThread.objects.exclude(state="archived").select_related("run__contract__issue__state")
        )
        for thread in threads:
            if str(thread.project_id) not in self.projects:
                continue
            client = self.clients.get(thread.runtime_id)
            if thread.run_id and (
                thread.run.status not in ("active", "merging") or is_closed(thread.run.contract.issue)
            ):
                cancelled = await db(thread.commands.filter(state="queued").update)(state="cancelled")
                messages = await db(AgentChatMessage.objects.filter(thread=thread, state="queued").update)(
                    state="cancelled"
                )
                self.credentials.pop(str(thread.run_id), None)
                self.assignments.pop(str(thread.run_id), None)
                self.credential_locks.pop(str(thread.run_id), None)
                if cancelled or messages:
                    await db(wake)()
                if not client or not client.ready:
                    continue
                interrupt_error = None
                if thread.active_turn:
                    try:
                        await client.request(
                            "turn/interrupt", {"threadId": thread.thread_id, "turnId": thread.active_turn}
                        )
                    except RPCError as error:
                        interrupt_error = error
                # Interrupt acceptance does not prove that the final transcript has been persisted.
                unfinished = False
                async for turn in client.history(thread.thread_id):
                    await self.reconcile_turn(thread, turn)
                    unfinished |= turn["status"] == "inProgress"
                if unfinished:
                    if interrupt_error:
                        raise interrupt_error
                    continue
                await client.request("thread/archive", {"threadId": thread.thread_id})
                await db(CodexThread.objects.filter(pk=thread.pk).update)(state="archived", active_turn="")
                await db(wake)()
                continue
            if not client or not client.ready:
                continue
            if thread.state != "idle":
                continue
            command = await db(lambda: thread.commands.filter(state="queued").first())()
            if not command:
                continue
            await db(CodexThread.objects.filter(pk=thread.pk).update)(state="busy")
            await db(CodexCommand.objects.filter(pk=command.pk).update)(state="submitting")
            try:
                if not thread.thread_id:
                    await self.start(thread)
                result = await client.request(
                    "turn/start",
                    {
                        "threadId": thread.thread_id,
                        "input": [{"type": "text", "text": command.text}],
                        "clientUserMessageId": str(command.pk),
                    },
                )
                turn_id = result["turn"]["id"]
                changed = await db(CodexCommand.objects.filter(pk=command.pk, state="submitting").update)(
                    state="sent", turn_id=turn_id
                )
                if changed and command.message_id:
                    await db(
                        AgentChatMessage.objects.filter(pk=command.message_id, state__in=["queued", "uncertain"]).update
                    )(state="sent", turn_id=turn_id)
                if changed:
                    await db(CodexThread.objects.filter(pk=thread.pk).update)(state="busy", active_turn=turn_id)
                    await db(wake)()
            except (ConnectionError, RPCError):
                # Even an RPC error can follow an accepted turn. Reconcile; never auto-repeat it.
                try:
                    async for turn in client.history(thread.thread_id):
                        await self.reconcile_turn(thread, turn)
                except (ConnectionError, RPCError):
                    pass
                await db(command.refresh_from_db)()
                if command.turn_id:
                    await db(CodexThread.objects.filter(pk=thread.pk).update)(
                        state="busy" if command.state == "sent" else "idle",
                        active_turn=command.turn_id if command.state == "sent" else "",
                        error="",
                    )
                    self.woken.set()
                    continue
                await db(CodexCommand.objects.filter(pk=command.pk).update)(state="uncertain")
                if command.message_id:
                    await db(AgentChatMessage.objects.filter(pk=command.message_id).update)(state="uncertain")
                await db(CodexThread.objects.filter(pk=thread.pk).update)(
                    state="needs_attention",
                    error="Turn delivery uncertain. Reconnect to reconcile before sending again.",
                )
                await db(wake)()

    async def scheduler(self):
        while True:
            await self.woken.wait()
            self.woken.clear()
            try:
                await self.pump()
            except (RPCError, ConnectionError):
                logger.warning("Codex host unavailable; queued work is retained")

    async def run(self):
        params = await db(database_params)()
        async with await psycopg.AsyncConnection.connect(**params, autocommit=True) as conn:
            # One owner of the durable queue, including after process restarts.
            cursor = await conn.execute("SELECT pg_try_advisory_lock(hashtext('plane_codex_runtime'))")
            if not (await cursor.fetchone())[0]:
                raise ValueError("A Plane Codex connector is already running")
            await conn.execute(f"LISTEN {CHANNEL}")
            await self.setup()
            tasks = [asyncio.create_task(client.run()) for client in self.clients.values()]
            tasks += [asyncio.create_task(self.notifications()), asyncio.create_task(self.scheduler())]
            self.woken.set()

            async def listen():
                async for _ in conn.notifies():
                    self.woken.set()

            tasks.append(asyncio.create_task(listen()))
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await self.api.aclose()
