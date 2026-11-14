import json

import psycopg
from asgiref.sync import sync_to_async
from django.http import StreamingHttpResponse
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.renderers import BaseRenderer

from plane.coordination.contracts import require_human
from plane.coordination.views import CoordinationEndpoint
from plane.db.models import Issue
from .chat import ensure_conversation, post_message, snapshot
from .signals import CHANNEL, database_params


class MessageInput(serializers.Serializer):
    id = serializers.UUIDField()
    body = serializers.CharField(allow_blank=True, trim_whitespace=True)
    assets = serializers.ListField(child=serializers.UUIDField(), default=list)

    def validate(self, data):
        if not data["body"] and not data["assets"]:
            raise serializers.ValidationError("Write a message or attach a file.")
        return data


class ConversationEndpoint(CoordinationEndpoint):
    def conversation(self, request):
        project = self.project(request)
        issue = get_object_or_404(Issue, project=project, pk=self.kwargs["issue_id"])
        return ensure_conversation(issue)

    def get(self, request, **kwargs):
        return Response(snapshot(self.conversation(request)))

    def post(self, request, **kwargs):
        self.project(request, write=True)
        require_human(request)
        conversation = self.conversation(request)
        serializer = MessageInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        message = post_message(conversation.issue, request.user, values["id"], values["body"], values["assets"])
        return Response({"id": str(message.pk)}, status=201)


class EventStreamRenderer(BaseRenderer):
    media_type = "text/event-stream"
    format = "event-stream"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        return json.dumps(data).encode()


class ConversationStreamEndpoint(ConversationEndpoint):
    renderer_classes = [EventStreamRenderer]
    http_method_names = ["get", "head", "options"]

    def get(self, request, **kwargs):
        self.conversation(request)
        params = database_params()

        async def stream():
            # Subscribe before reading: a commit between snapshot and LISTEN must not be lost.
            async with await psycopg.AsyncConnection.connect(**params, autocommit=True) as conn:
                await conn.execute(f"LISTEN {CHANNEL}")

                async def event():
                    data = await sync_to_async(lambda: snapshot(self.conversation(request)))()
                    return f"data: {json.dumps(data)}\n\n"

                yield await event()
                async for _ in conn.notifies():
                    yield await event()

        response = StreamingHttpResponse(stream(), content_type="text/event-stream")
        # Streaming gzip members can wait for the next frame before a proxy flushes.
        response["Content-Encoding"] = "identity"
        response["Cache-Control"] = "no-cache, no-transform"
        response["X-Accel-Buffering"] = "no"
        return response
