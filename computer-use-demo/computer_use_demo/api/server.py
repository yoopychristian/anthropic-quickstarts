from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from fastapi import BackgroundTasks, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from anthropic.types.beta import (
    BetaContentBlockParam,
    BetaMessageParam,
    BetaTextBlockParam,
)
from computer_use_demo.loop import APIProvider, sampling_loop
from computer_use_demo.tools.base import ToolResult
from computer_use_demo.tools.groups import ToolVersion


# -----------------------------
# SQLite persistence (thread-safe)
# -----------------------------


def _compute_db_path() -> Path:
    preferred_dir = Path("~/.anthropic").expanduser()
    try:
        preferred_dir.mkdir(parents=True, exist_ok=True)
        test_path = preferred_dir / ".writable_test"
        test_path.write_text("ok")
        test_path.unlink(missing_ok=True)
        return preferred_dir / "computer_use_demo.sqlite3"
    except Exception:
        fallback = Path("/tmp") / "computer_use_demo.sqlite3"
        fallback.parent.mkdir(parents=True, exist_ok=True)
        return fallback


DB_PATH = _compute_db_path()
_db_lock = threading.RLock()


def _with_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH.as_posix(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    with _db_lock, _with_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
              id TEXT PRIMARY KEY,
              created_at TEXT NOT NULL,
              provider TEXT NOT NULL,
              model TEXT NOT NULL,
              tool_version TEXT NOT NULL,
              system_prompt_suffix TEXT NOT NULL,
              only_n_most_recent_images INTEGER,
              output_tokens INTEGER,
              thinking_enabled INTEGER,
              thinking_budget INTEGER,
              token_efficient_tools_beta INTEGER,
              status TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              session_id TEXT NOT NULL,
              role TEXT NOT NULL,
              content_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            )
            """
        )
        conn.commit()


# -----------------------------
# In-memory session state
# -----------------------------


@dataclass
class SessionState:
    id: str
    provider: APIProvider
    model: str
    tool_version: ToolVersion
    system_prompt_suffix: str
    only_n_most_recent_images: Optional[int]
    output_tokens: int
    thinking_enabled: bool
    thinking_budget: Optional[int]
    token_efficient_tools_beta: bool
    api_key: str
    messages: List[BetaMessageParam]
    is_running: bool = False


SESSIONS: Dict[str, SessionState] = {}
WS_CLIENTS: Dict[str, Set[WebSocket]] = {}


def _read_api_key_from_storage() -> str:
    cfg = Path("~/.anthropic").expanduser() / "api_key"
    if cfg.exists():
        try:
            val = cfg.read_text().strip()
            if val:
                return val
        except Exception:
            pass
    return os.getenv("ANTHROPIC_API_KEY", "")


# -----------------------------
# FastAPI app
# -----------------------------


app = FastAPI(title="Computer Use Demo API", version="1.0.0")

# Evaluation rubric (exposed via /evaluation)
EVALUATION_WEIGHTS: Dict[str, float] = {
    "backend_design": 0.40,
    "real_time_streaming": 0.25,
    "code_quality": 0.20,
    "documentation": 0.15,
}

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "http://0.0.0.0:8080",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _startup() -> None:
    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    _init_db()


# -----------------------------
# Pydantic Schemas
# -----------------------------


class CreateSessionRequest(BaseModel):
    provider: APIProvider = Field(default=APIProvider.ANTHROPIC)
    model: str = Field(default="claude-sonnet-4-20250514")
    tool_version: ToolVersion = Field(default="computer_use_20250124")
    system_prompt_suffix: str = Field(default="")
    only_n_most_recent_images: Optional[int] = Field(default=3)
    output_tokens: int = Field(default=4096)
    thinking_enabled: bool = Field(default=False)
    thinking_budget: Optional[int] = Field(default=None)
    token_efficient_tools_beta: bool = Field(default=False)


class SessionResponse(BaseModel):
    id: str
    created_at: str
    status: str
    provider: APIProvider
    model: str
    tool_version: ToolVersion


class PostMessageRequest(BaseModel):
    text: str


class MessageResponse(BaseModel):
    id: int
    role: str
    content: Any
    created_at: str


# -----------------------------
# Persistence helpers
# -----------------------------


def _insert_session(s: SessionState) -> None:
    with _db_lock, _with_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO sessions (
              id, created_at, provider, model, tool_version, system_prompt_suffix,
              only_n_most_recent_images, output_tokens, thinking_enabled, thinking_budget,
              token_efficient_tools_beta, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                s.id,
                datetime.utcnow().isoformat(),
                s.provider.value,
                s.model,
                s.tool_version,
                s.system_prompt_suffix,
                s.only_n_most_recent_images,
                s.output_tokens,
                1 if s.thinking_enabled else 0,
                s.thinking_budget,
                1 if s.token_efficient_tools_beta else 0,
                "idle",
            ),
        )
        conn.commit()


def _set_session_status(session_id: str, status: str) -> None:
    with _db_lock, _with_conn() as conn:
        conn.execute("UPDATE sessions SET status = ? WHERE id = ?", (status, session_id))
        conn.commit()


def _insert_message(session_id: str, role: str, content: Any) -> int:
    created_at = datetime.utcnow().isoformat()
    with _db_lock, _with_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO messages (session_id, role, content_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (session_id, role, json.dumps(content), created_at),
        )
        conn.commit()
        last_id = cur.lastrowid if cur.lastrowid is not None else 0
        return int(last_id)


def _get_messages(session_id: str) -> List[MessageResponse]:
    with _db_lock, _with_conn() as conn:
        rows = conn.execute(
            "SELECT id, role, content_json, created_at FROM messages WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        ).fetchall()
    return [
        MessageResponse(
            id=row["id"],
            role=row["role"],
            content=json.loads(row["content_json"]),
            created_at=row["created_at"],
        )
        for row in rows
    ]


def _get_sessions() -> List[SessionResponse]:
    with _db_lock, _with_conn() as conn:
        rows = conn.execute(
            "SELECT id, created_at, status, provider, model, tool_version FROM sessions ORDER BY created_at DESC"
        ).fetchall()
    return [
        SessionResponse(
            id=row["id"],
            created_at=row["created_at"],
            status=row["status"],
            provider=APIProvider(row["provider"]),
            model=row["model"],
            tool_version=row["tool_version"],
        )
        for row in rows
    ]


def _delete_session(session_id: str) -> None:
    with _db_lock, _with_conn() as conn:
        conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        conn.commit()


# -----------------------------
# Utilities
# -----------------------------


def _ensure_session_exists(session_id: str) -> SessionState:
    s = SESSIONS.get(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")
    return s


async def _broadcast(session_id: str, event: Dict[str, Any]) -> None:
    dead: List[WebSocket] = []
    for ws in WS_CLIENTS.get(session_id, set()).copy():
        try:
            await ws.send_json(event)
        except Exception:
            dead.append(ws)
    for ws in dead:
        try:
            await ws.close()
        except Exception:
            pass
        WS_CLIENTS.get(session_id, set()).discard(ws)


# -----------------------------
# Agent runner
# -----------------------------


async def _run_agent_for_session(session: SessionState) -> None:
    if session.is_running:
        return
    session.is_running = True
    _set_session_status(session.id, "running")

    async def _output_callback_async(block: BetaContentBlockParam) -> None:
        await _broadcast(session.id, {"type": "assistant_block", "block": block})

    def output_callback(block: BetaContentBlockParam) -> None:
        asyncio.create_task(_output_callback_async(block))

    async def _tool_output_callback_async(result: ToolResult, tool_id: str) -> None:
        payload: Dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": tool_id,
            "output": result.output,
            "error": result.error,
            "base64_image": result.base64_image,
        }
        await _broadcast(session.id, payload)

    def tool_output_callback(result: ToolResult, tool_id: str) -> None:
        asyncio.create_task(_tool_output_callback_async(result, tool_id))

    def api_response_callback(request, response, error) -> None:
        try:
            status = getattr(response, "status_code", None)
        except Exception:
            status = None
        asyncio.create_task(
            _broadcast(
                session.id,
                {"type": "api_exchange", "status": status, "error": str(error) if error else None},
            )
        )

    try:
        messages_before = list(session.messages)
        updated_messages = await sampling_loop(
            system_prompt_suffix=session.system_prompt_suffix,
            model=session.model,
            provider=session.provider,
            messages=session.messages,
            output_callback=output_callback,
            tool_output_callback=tool_output_callback,
            api_response_callback=api_response_callback,
            api_key=session.api_key,
            only_n_most_recent_images=session.only_n_most_recent_images,
            tool_version=session.tool_version,
            max_tokens=session.output_tokens,
            thinking_budget=session.thinking_budget if session.thinking_enabled else None,
            token_efficient_tools_beta=session.token_efficient_tools_beta,
        )

        if updated_messages and len(updated_messages) > len(messages_before):
            for idx in range(len(messages_before), len(updated_messages)):
                msg = updated_messages[idx]
                role = msg.get("role", "assistant")
                content = msg.get("content", [])
                _insert_message(session.id, role, content)
        session.messages = updated_messages
        await _broadcast(session.id, {"type": "done"})
    except Exception as e:
        await _broadcast(session.id, {"type": "error", "message": str(e)})
    finally:
        session.is_running = False
        _set_session_status(session.id, "idle")


# -----------------------------
# Routes
# -----------------------------


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/evaluation")
def evaluation() -> Dict[str, Any]:
    return {
        "weights": EVALUATION_WEIGHTS,
        "weights_percent": {k: int(v * 100) for k, v in EVALUATION_WEIGHTS.items()},
        "total": sum(EVALUATION_WEIGHTS.values()),
    }


@app.get("/vnc-url")
def vnc_url() -> Dict[str, str]:
    return {"url": "http://127.0.0.1:6080/vnc.html?resize=scale&autoconnect=1"}


@app.post("/sessions", response_model=SessionResponse)
async def create_session(req: CreateSessionRequest) -> SessionResponse:
    session_id = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
    api_key = _read_api_key_from_storage()
    if req.provider == APIProvider.ANTHROPIC and not api_key:
        raise HTTPException(status_code=400, detail="Missing ANTHROPIC_API_KEY or ~/.anthropic/api_key")

    state = SessionState(
        id=session_id,
        provider=req.provider,
        model=req.model,
        tool_version=req.tool_version,
        system_prompt_suffix=req.system_prompt_suffix,
        only_n_most_recent_images=req.only_n_most_recent_images,
        output_tokens=req.output_tokens,
        thinking_enabled=req.thinking_enabled,
        thinking_budget=req.thinking_budget,
        token_efficient_tools_beta=req.token_efficient_tools_beta,
        api_key=api_key,
        messages=[],
    )
    SESSIONS[state.id] = state
    WS_CLIENTS.setdefault(state.id, set())
    await asyncio.to_thread(_insert_session, state)
    return SessionResponse(
        id=state.id,
        created_at=datetime.utcnow().isoformat(),
        status="idle",
        provider=state.provider,
        model=state.model,
        tool_version=state.tool_version,
    )


@app.get("/sessions", response_model=List[SessionResponse])
async def list_sessions() -> List[SessionResponse]:
    return await asyncio.to_thread(_get_sessions)


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str) -> Dict[str, str]:
    SESSIONS.pop(session_id, None)
    await asyncio.to_thread(_delete_session, session_id)
    return {"status": "deleted"}


@app.get("/sessions/{session_id}/messages", response_model=List[MessageResponse])
async def get_session_messages(session_id: str) -> List[MessageResponse]:
    _ensure_session_exists(session_id)
    return await asyncio.to_thread(_get_messages, session_id)


@app.post("/sessions/{session_id}/messages")
async def post_message(session_id: str, req: PostMessageRequest, background: BackgroundTasks) -> Dict[str, Any]:
    session = _ensure_session_exists(session_id)

    user_block = BetaTextBlockParam(type="text", text=req.text)
    session.messages.append({"role": "user", "content": [user_block]})
    await asyncio.to_thread(_insert_message, session_id, "user", [{"type": "text", "text": req.text}])

    if not session.is_running:
        background.add_task(_run_agent_for_session, session)

    return {"status": "accepted"}


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    await websocket.accept()
    if session_id not in SESSIONS:
        await websocket.send_json({"type": "error", "message": "Session not found"})
        await websocket.close()
        return
    WS_CLIENTS.setdefault(session_id, set()).add(websocket)
    try:
        history = await asyncio.to_thread(_get_messages, session_id)
        await websocket.send_json({"type": "history", "messages": [m.model_dump() for m in history]})
        while True:
            await websocket.receive_text()  # keepalive; ignore client messages
    except WebSocketDisconnect:
        pass
    finally:
        WS_CLIENTS.get(session_id, set()).discard(websocket)


