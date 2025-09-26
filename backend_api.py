from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import AsyncExitStack
from datetime import datetime, timedelta
from typing import Any, AsyncGenerator, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from mcp_client import MultiMCPClient, load_servers_from_yaml
from chat_cli import (
    mcp_tools_to_vertex_functions,
    extract_function_calls,
    build_function_response_part,
    init_vertex,
)
from vertexai.generative_models import GenerativeModel, Tool
from dotenv import load_dotenv


app = FastAPI(title="Chatbot API")

load_dotenv()

ALLOWED_ORIGINS = [
    "http://localhost:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class Session:
    def __init__(self, session_id: str, model: GenerativeModel, tools: List[Tool]):
        self.id = session_id
        self.model = model
        self.tools = tools
        self.chat = model.start_chat()
        self.queue: "asyncio.Queue[dict]" = asyncio.Queue()
        self.last_active = datetime.utcnow()


SESSIONS: Dict[str, Session] = {}
SESSION_TTL = timedelta(minutes=30)


async def _session_gc_loop():
    while True:
        await asyncio.sleep(60)
        now = datetime.utcnow()
        expired = [sid for sid, s in SESSIONS.items() if now - s.last_active > SESSION_TTL]
        for sid in expired:
            SESSIONS.pop(sid, None)


@app.on_event("startup")
async def _startup():
    asyncio.create_task(_session_gc_loop())


def _event(type_: str, payload: Any) -> dict:
    return {"type": type_, "payload": payload, "ts": datetime.utcnow().isoformat()}


@app.post("/api/session")
async def create_session():
    system_md_path = os.getenv("system_prompt", "system.md")
    servers_yaml = os.getenv("servers", "servers.yaml")

    # Initialize Vertex model with system text
    with open(system_md_path, "r", encoding="utf-8") as f:
        system_text = f.read()
    model, _project, _location = init_vertex(system_text)

    # Discover MCP tools
    servers = load_servers_from_yaml(servers_yaml)
    async with MultiMCPClient(servers, timeout=45.0) as multi:
        catalog = await multi.list_tools()
    tools = [Tool(function_declarations=mcp_tools_to_vertex_functions(catalog))]

    sid = uuid.uuid4().hex
    SESSIONS[sid] = Session(sid, model, tools)
    return {"sessionId": sid, "createdAt": datetime.utcnow().isoformat()}


@app.post("/api/session/{session_id}/reset")
async def reset_session(session_id: str):
    s = SESSIONS.get(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")
    s.chat = s.model.start_chat()
    s.last_active = datetime.utcnow()
    # Emit a status event so frontend can clear UI if needed
    await s.queue.put(_event("status", {"level": "info", "message": "Session reset"}))
    return {"ok": True}


@app.get("/api/session/{session_id}/events")
async def stream_events(session_id: str):
    s = SESSIONS.get(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")

    async def _generator() -> AsyncGenerator[str, None]:
        # send hello event
        yield f"data: {json.dumps(_event('status', {'level': 'info', 'message': 'connected'}))}\n\n"
        while True:
            item = await s.queue.get()
            yield f"data: {json.dumps(item)}\n\n"

    headers = {"Cache-Control": "no-cache", "Content-Type": "text/event-stream"}
    return StreamingResponse(_generator(), headers=headers)


@app.post("/api/session/{session_id}/message")
async def post_message(session_id: str, body: dict):
    s = SESSIONS.get(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")
    text = body.get("text")
    if not text:
        raise HTTPException(status_code=400, detail="Missing 'text'")

    s.last_active = datetime.utcnow()
    await s.queue.put(_event("message.user", {"text": text}))

    async def _worker():
        try:
            resp = s.chat.send_message(text, tools=s.tools)
            proposed = extract_function_calls(resp)
            unique: list[ProposedCall] = []
            seen = set()
            for call in proposed:
                key = (call.name, json.dumps(call.args, sort_keys=True))
                if key not in seen:
                    seen.add(key)
                    unique.append(call)
            proposed = unique
            if not proposed:
                await s.queue.put(_event("message.model.final", {"text": resp.text or ""}))
                return

            # Execute tools in parallel and emit events
            from mcp_client import MultiMCPClient  # local import to reuse config
            servers_yaml = os.getenv("servers", "servers.yaml")
            servers = load_servers_from_yaml(servers_yaml)
            async with MultiMCPClient(servers, timeout=45.0) as multi:
                tasks = [
                    multi.call_tool(call.name, call.args, timeout=45.0, raise_on_error=False)
                    for call in proposed
                ]
                results = await asyncio.gather(*tasks)

            for call, result in zip(proposed, results):
                await s.queue.put(_event("tool_call.started", {"toolFqn": call.name, "args": call.args}))
                if result.get("is_error"):
                    await s.queue.put(_event("tool_call.error", {"toolFqn": call.name, "message": result.get("content_text"), "structured_content": result.get("structured_content")}))
                else:
                    await s.queue.put(_event("tool_call.result", {"toolFqn": call.name, "data": result.get("data")}))

            # Provide function responses back to model and stream final message
            from vertexai.generative_models import Part
            parts = []
            for call, result in zip(proposed, results):
                payload = result.get("data")
                if result.get("is_error") or payload is None:
                    payload = {
                        "error": True,
                        "message": result.get("content_text") or "Tool error",
                        "structured_content": result.get("structured_content"),
                    }
                parts.append(Part.from_function_response(name=call.name, response={"result": payload}))

            final = s.chat.send_message(parts, tools=s.tools)
            await s.queue.put(_event("message.model.final", {"text": getattr(final, "text", "") or ""}))
        except Exception as e:
            await s.queue.put(_event("status", {"level": "error", "message": str(e)}))

    asyncio.create_task(_worker())
    return Response(status_code=202)


def run():
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=9000, log_level="warning")


if __name__ == "__main__":
    run()


