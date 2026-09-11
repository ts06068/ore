"""Authenticated conversation endpoints; SSE replays only persisted public events."""
from __future__ import annotations

import json

from fastapi import Request
from fastapi.responses import StreamingResponse

from .conversation import ConversationInput, ConversationManager, MessageInput
from .conversation_library import FolderInput, ConversationUpdate, BranchInput


def attach_conversation_routes(app, engine):
    manager = getattr(engine, "conversations", None)
    if manager is None:
        manager = engine.conversations = ConversationManager(engine)

    @app.get("/v1/conversation-folders")
    async def folders():
        return manager.folders()

    @app.post("/v1/conversation-folders", status_code=201)
    async def create_folder(values: FolderInput):
        return manager.create_folder(values.model_dump())

    @app.get("/v1/conversation-folders/{ident}")
    async def folder(ident: str):
        return manager.folder(ident)

    @app.patch("/v1/conversation-folders/{ident}")
    async def rename_folder(ident: str, values: FolderInput):
        return manager.rename_folder(ident, values.model_dump())

    @app.delete("/v1/conversation-folders/{ident}")
    async def delete_folder(ident: str):
        return manager.delete_folder(ident)

    @app.get("/v1/conversations")
    async def conversations():
        return manager.list()

    @app.post("/v1/conversations", status_code=201)
    async def create(values: ConversationInput):
        return manager.create(values.model_dump(exclude_none=True))

    @app.get("/v1/conversations/{ident}")
    async def detail(ident: str):
        return manager.get(ident)

    @app.patch("/v1/conversations/{ident}")
    async def update(ident: str, values: ConversationUpdate):
        return manager.update_conversation(ident, values.model_dump(exclude_unset=True))

    @app.post("/v1/conversations/{ident}/branch", status_code=201)
    async def branch(ident: str, values: BranchInput):
        return manager.branch(ident, values.model_dump(exclude_unset=True))

    @app.post("/v1/conversations/{ident}/messages", status_code=202)
    async def message(ident: str, values: MessageInput):
        return await manager.message(ident, values.model_dump(exclude_none=True))

    @app.post("/v1/conversations/{ident}/plans/{plan_id}/approve")
    async def approve(ident: str, plan_id: str):
        return await manager.approve(ident, plan_id)

    @app.post("/v1/conversations/{ident}/interrupt")
    async def interrupt(ident: str):
        return await manager.interrupt(ident)

    @app.post("/v1/conversations/{ident}/resume")
    async def resume(ident: str):
        return await manager.resume(ident)

    @app.get("/v1/conversations/{ident}/event-history")
    async def event_history(ident: str, before: int | None = None, limit: int = 100):
        return manager.event_history(ident, before=before, limit=limit)

    @app.get("/v1/conversations/{ident}/events")
    async def events(ident: str, request: Request, after: int = 0):
        manager.get(ident)
        try:
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise ValueError("Event cursor must be an integer") from None
        async def stream():
            nonlocal cursor
            while not await request.is_disconnected():
                rows = manager.events(ident, cursor)
                for event in rows:
                    cursor = event["id"]
                    yield f"id: {cursor}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if not rows:
                    yield ": keepalive\n\n"
                    await manager.wait_events(ident, timeout=15)
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    return manager
