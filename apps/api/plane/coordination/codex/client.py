"""Codex JSON-RPC over a native app-server WebSocket (experimental transport)."""

import asyncio
import json
import os
from uuid import uuid4

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed


class RPCError(Exception):
    pass


class CodexClient:
    def __init__(self, config, handler, connected, disconnected):
        self.config = config
        self.handler = handler
        self.connected = connected
        self.disconnected = disconnected
        self.socket = None
        self.ready = False
        self.pending = {}
        self.tasks = set()

    async def request(self, method, params):
        if self.socket is None:
            raise ConnectionError("Codex host is offline")
        identifier = str(uuid4())
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        try:
            await self.socket.send(json.dumps({"id": identifier, "method": method, "params": params}))
            return await future
        except ConnectionClosed as error:
            self.ready = False
            raise ConnectionError("Codex connection lost; reconcile delivery") from error
        finally:
            self.pending.pop(identifier, None)

    async def reply(self, identifier, result):
        await self.socket.send(json.dumps({"id": identifier, "result": result}))

    async def reject(self, identifier, message):
        await self.socket.send(json.dumps({"id": identifier, "error": {"code": -32601, "message": message}}))

    async def read(self):
        try:
            await self.receive()
        finally:
            self.ready = False
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("Codex connection lost; reconcile delivery"))

    async def receive(self):
        async for raw in self.socket:
            message = json.loads(raw)
            if "method" not in message and message.get("id") in self.pending:
                future = self.pending[message["id"]]
                if not future.done():
                    if "error" in message:
                        future.set_exception(RPCError(message["error"].get("message", "Codex request failed")))
                    else:
                        future.set_result(message.get("result"))
            else:
                await self.handler(message)

    async def restore(self):
        await self.connected()
        self.ready = True

    async def run(self):
        token = os.environ[self.config["token_env"]] if self.config.get("token_env") else None
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        # Library defaults supply keepalive/backoff. History may exceed a single frame's default size.
        async for socket in connect(self.config["url"], additional_headers=headers, proxy=None, max_size=None):
            self.socket = socket
            reader = asyncio.create_task(self.read())
            startup = None
            try:
                await self.request(
                    "initialize",
                    {"clientInfo": {"name": "plane", "version": "1"}, "capabilities": {"experimentalApi": True}},
                )
                await socket.send(json.dumps({"method": "initialized"}))
                startup = asyncio.create_task(self.restore())
                done, _ = await asyncio.wait([startup, reader], return_when=asyncio.FIRST_EXCEPTION)
                for task in done:
                    task.result()
            except (ConnectionClosed, ConnectionError):
                pass
            finally:
                self.ready = False
                self.socket = None
                for future in self.pending.values():
                    if not future.done():
                        future.set_exception(ConnectionError("Codex connection lost; delivery must be reconciled"))
                if startup:
                    startup.cancel()
                    await asyncio.gather(startup, return_exceptions=True)
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
                await self.disconnected()

    async def history(self, thread_id):
        cursor = None
        while True:
            page = await self.request(
                "thread/turns/list", {"threadId": thread_id, "cursor": cursor, "sortDirection": "asc"}
            )
            for turn in page["data"]:
                items = []
                item_cursor = None
                while True:
                    result = await self.request(
                        "thread/items/list",
                        {"threadId": thread_id, "turnId": turn["id"], "cursor": item_cursor, "sortDirection": "asc"},
                    )
                    items.extend(entry["item"] for entry in result["data"])
                    item_cursor = result.get("nextCursor")
                    if not item_cursor:
                        break
                yield {**turn, "items": items}
            cursor = page.get("nextCursor")
            if not cursor:
                break
