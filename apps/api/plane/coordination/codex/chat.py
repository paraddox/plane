from django.db import transaction
from django.db.models import Prefetch
from rest_framework.exceptions import ValidationError

from plane.coordination.contracts import CoordinationConflict
from plane.db.models import AgentRun, FileAsset
from plane.db.models.codex import AgentConversation, AgentChatMessage, CodexCommand
from .signals import wake


def is_closed(issue):
    return bool(
        issue.deleted_at or issue.archived_at or (issue.state_id and issue.state.group in ("completed", "cancelled"))
    )


def ensure_conversation(issue):
    return AgentConversation.objects.get_or_create(issue=issue)[0]


def conversation_closed(conversation):
    return is_closed(conversation.issue) or bool(
        conversation.chat_id and conversation.chat.run_id and conversation.chat.run.status not in ("active", "merging")
    )


def snapshot(conversation):
    return {
        "revision": conversation.revision,
        "closed": conversation_closed(conversation),
        "state": conversation.chat.state if conversation.chat_id else "not_assigned",
        "error": conversation.chat.error if conversation.chat_id else "",
        "messages": [
            {
                "id": str(message.pk),
                "role": message.role,
                "body": message.body,
                "state": message.state,
                "author": message.author.display_name
                if message.author_id
                else ("Agent" if message.role == "assistant" else "Former member"),
                "created_at": message.created_at.isoformat(),
                "assets": [
                    {
                        "id": str(asset.pk),
                        "name": asset.attributes.get("name", "Attachment"),
                        "mime_type": asset.attributes.get("type", ""),
                        "url": asset.asset_url if not asset.is_deleted and not asset.deleted_at else None,
                    }
                    for asset in message.assets.all()
                ],
            }
            for message in conversation.messages.select_related("author").prefetch_related(
                Prefetch("assets", queryset=FileAsset.all_objects.select_related("workspace"))
            )
        ],
    }


@transaction.atomic
def post_message(issue, author, identifier, body, assets):
    from plane.db.models import Issue

    issue = Issue.objects.select_for_update().get(pk=issue.pk)
    conversation = AgentConversation.objects.select_for_update().get(issue=issue)
    existing = AgentChatMessage.objects.filter(pk=identifier).first()
    if existing:
        if (
            existing.conversation_id != issue.pk
            or existing.author_id != author.pk
            or existing.body != body
            or set(str(pk) for pk in existing.assets.values_list("pk", flat=True)) != set(map(str, assets))
        ):
            raise CoordinationConflict("This message identifier was already used with different content.")
        return existing
    if is_closed(issue) or conversation_closed(conversation):
        raise CoordinationConflict("The conversation is read-only because the work item is closed.")
    if not conversation.chat_id:
        raise CoordinationConflict("The project overseer has not assigned a chat agent yet.")
    files = FileAsset.objects.filter(
        pk__in=assets, issue=issue, project=issue.project, is_uploaded=True, is_deleted=False
    )
    if files.count() != len(set(assets)):
        raise ValidationError({"assets": "Only uploaded attachments belonging to this work item can be shared."})
    conversation.revision += 1
    conversation.save(update_fields=["revision"])
    message = AgentChatMessage.objects.create(
        id=identifier,
        conversation=conversation,
        author=author,
        role="user",
        ordinal=conversation.revision,
        body=body,
        thread=conversation.chat,
    )
    message.assets.set(files)
    references = [{"id": str(file.pk), "attributes": file.attributes} for file in files]
    CodexCommand.objects.create(
        id=identifier,
        thread=conversation.chat,
        message=message,
        text=f"Human {author.display_name}: {body}\nWork-item attachments: {references}",
    )
    wake()
    return message


@transaction.atomic
def assistant_message(thread, turn_id, item_id, text, *, final, replace=False, state=None):
    conversations = AgentConversation.objects.select_for_update()
    if thread.run_id:
        # A reopened work item may already point at a newer chat while old output is recovered.
        conversations = conversations.filter(
            issue_id__in=AgentRun.objects.filter(pk=thread.run_id).values("contract__issue_id")
        )
    else:
        conversations = conversations.filter(chat=thread)
    conversation = conversations.first()
    if conversation is None:
        return
    message = AgentChatMessage.objects.filter(thread=thread, item_id=item_id).first()
    incoming_state = state or ("completed" if final else "streaming")
    if message and (
        message.state == "completed" or (message.state in ("failed", "interrupted") and incoming_state == "streaming")
    ):
        return
    conversation.revision += 1
    conversation.save(update_fields=["revision"])
    if not message:
        message = AgentChatMessage(
            conversation=conversation,
            ordinal=conversation.revision,
            role="assistant",
            thread=thread,
            turn_id=turn_id,
            item_id=item_id,
        )
    message.body = text if final or replace else message.body + text
    message.state = incoming_state
    message.save()
    wake()


@transaction.atomic
def share_file(thread, identifier, asset, text):
    conversation = AgentConversation.objects.select_for_update().get(issue_id=asset.issue_id)
    if conversation_closed(conversation):
        raise CoordinationConflict("This work item is closed.")
    existing = AgentChatMessage.objects.filter(pk=identifier).first()
    if existing:
        if existing.conversation_id != conversation.pk or existing.thread_id != thread.pk:
            raise CoordinationConflict("This message identifier belongs to another conversation.")
        return existing
    conversation.revision += 1
    conversation.save(update_fields=["revision"])
    message = AgentChatMessage.objects.create(
        id=identifier,
        conversation=conversation,
        ordinal=conversation.revision,
        role="assistant",
        body=text,
        thread=thread,
        state="completed",
    )
    message.assets.add(asset)
    wake()
    return message
