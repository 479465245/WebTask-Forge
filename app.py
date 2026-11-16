from __future__ import annotations

import asyncio
import base64
import hmac
import ipaddress
import json
import os
from contextlib import asynccontextmanager
from datetime import timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright
from sqlalchemy import func, select

from db import (NetworkEvent, NodeExecution, Recording, RecordingAction, Run, Session, Task,
                initialize_database, utcnow)
from nodes import automation_processor_manager, catalog, load_plugins
from network_capture import NetworkCapture, _safe_url
import workflow

ROOT = Path(__file__).parent
HOST = os.environ.get("BROWSER_DEMO_HOST", "127.0.0.1")
PORT = int(os.environ.get("BROWSER_DEMO_PORT", "8765"))
USERNAME = os.environ.get("BROWSER_DEMO_USERNAME", "")
PASSWORD = os.environ.get("BROWSER_DEMO_PASSWORD", "")


def loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def authorized(header: str) -> bool:
    if loopback(HOST):
        return True
    try:
        scheme, encoded = header.split(" ", 1)
        supplied = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    return scheme.lower() == "basic" and hmac.compare_digest(supplied, f"{USERNAME}:{PASSWORD}")


def valid_url(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        return None
    value = value.strip()
    if not value.lower().startswith(("http://", "https://")):
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    return value


if not loopback(HOST) and (not USERNAME or not PASSWORD):
    raise RuntimeError("Set BROWSER_DEMO_USERNAME and BROWSER_DEMO_PASSWORD before binding beyond localhost.")


class BrowserRuntime:
    def __init__(self):
        self.playwright = None
        self.context = None
        self.pages: dict[int, object] = {}
        self.mobile_sessions: dict[int, object] = {}
        self.network_captures: dict[int, NetworkCapture] = {}
        self.recordings: dict[int, dict] = {}
        self.locks: dict[int, asyncio.Lock] = {}
        self.controllers: dict[int, WebSocket] = {}
        self.connections: dict[int, set[WebSocket]] = {}
        self._closing = False
        self._recovering = False

    async def start(self):
        self.playwright = await async_playwright().start()
        profile = os.environ.get("BROWSER_DATA_DIR", str(ROOT / ".browser-data"))
        Path(profile).mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(profile, 0o700)
        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=profile, headless=os.getenv("BROWSER_HEADLESS", "true").lower() != "false",
            viewport={"width": 1440, "height": 960}, device_scale_factor=1)
        self.context.on("close", self._context_closed)

    def _context_closed(self):
        if not self._closing and not self._recovering:
            asyncio.create_task(self.recover())

    async def recover(self):
        self._recovering = True
        try:
            await workflow.browser_crashed()
            self.pages.clear()
            self.mobile_sessions.clear()
            for capture in list(self.network_captures.values()):
                await capture.close()
            self.network_captures.clear()
            for run_id in list(self.recordings):
                await stop_recording(run_id)
            self.locks.clear()
            self.controllers.clear()
            self.connections.clear()
            await self.start()
        finally:
            self._recovering = False

    async def stop(self):
        self._closing = True
        if self.context:
            await self.context.close()
        if self.playwright:
            await self.playwright.stop()

    async def new_page(self, run_id: int, width: int, height: int, device_mode: str = "desktop"):
        page = await self.context.new_page()
        capture = NetworkCapture(run_id)
        capture.attach(page)
        self.network_captures[run_id] = capture
        await page.set_viewport_size({"width": width, "height": height})
        if device_mode == "mobile":
            session = await self.context.new_cdp_session(page)
            try:
                await session.send("Emulation.setDeviceMetricsOverride", {
                    "width": width, "height": height, "deviceScaleFactor": 1, "mobile": True,
                    "screenOrientation": {"type": "portraitPrimary", "angle": 0},
                })
                await session.send("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
                user_agent = await page.evaluate("navigator.userAgent")
                for desktop_platform in ("Windows NT 10.0; Win64; x64", "Macintosh; Intel Mac OS X 10_15_7", "X11; Linux x86_64"):
                    user_agent = user_agent.replace(desktop_platform, "Linux; Android 14; Pixel 8")
                if " Mobile Safari/" not in user_agent:
                    user_agent = user_agent.replace(" Safari/", " Mobile Safari/")
                user_agent_data = await page.evaluate("navigator.userAgentData ? navigator.userAgentData.toJSON() : null")
                override = {"userAgent": user_agent}
                if user_agent_data:
                    override["userAgentMetadata"] = {
                        "brands": user_agent_data.get("brands", []),
                        "fullVersionList": user_agent_data.get("fullVersionList", []),
                        "platform": "Android", "platformVersion": "14.0.0",
                        "architecture": "arm", "model": "Pixel 8", "mobile": True,
                        "bitness": "64", "wow64": False,
                    }
                await session.send("Emulation.setUserAgentOverride", override)
            except Exception:
                await session.detach()
                await page.close()
                await capture.close()
                self.network_captures.pop(run_id, None)
                raise
            self.mobile_sessions[run_id] = session
        self.pages[run_id] = page
        self.locks[run_id] = asyncio.Lock()
        self.connections[run_id] = set()
        return page

    async def close_page(self, run_id: int):
        await stop_recording(run_id)
        page = self.pages.pop(run_id, None)
        session = self.mobile_sessions.pop(run_id, None)
        capture = self.network_captures.pop(run_id, None)
        self.locks.pop(run_id, None)
        self.controllers.pop(run_id, None)
        connections = self.connections.pop(run_id, set())
        for connection in tuple(connections):
            try:
                await connection.close(code=1001, reason="运行实例已关闭")
            except Exception:
                pass
        if session:
            await session.detach()
        if page and not page.is_closed():
            await page.close()
        if capture:
            await capture.close()

    async def goto(self, run_id: int, address: object):
        url = valid_url(address)
        if not url:
            raise ValueError("网址为空或格式无效，仅支持 HTTP/HTTPS")
        page = self.pages.get(run_id)
        if not page or page.is_closed():
            raise RuntimeError("任务浏览器标签页不可用")
        async with self.locks[run_id]:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)


browser = BrowserRuntime()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    initialize_database()
    with Session() as session:
        interrupted_recordings = session.scalars(select(Recording).where(
            Recording.status == "recording")).all()
        for recording in interrupted_recordings:
            recording.status = "draft"
            recording.finished_at = utcnow()
        if interrupted_recordings:
            session.commit()
    load_plugins()
    await workflow.mark_interrupted_runs()
    await browser.start()
    workflow.configure(browser)
    scheduler = asyncio.create_task(workflow.scheduler_loop())
    try:
        yield
    finally:
        scheduler.cancel()
        await asyncio.gather(scheduler, return_exceptions=True)
        await workflow.service_stopping()
        for run_id in list(browser.pages):
            await browser.close_page(run_id)
        await browser.stop()


app = FastAPI(title="WebTask Forge | 网页自动化工坊", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if not authorized(request.headers.get("authorization", "")):
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Automation Platform", charset="UTF-8"'})
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return Response(status_code=403, content="Cross-origin request rejected")
    return await call_next(request)


class TaskPayload(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    enabled: bool = False
    cron: str | None = None
    viewport_width: int = Field(default=1440, ge=320, le=3840)
    viewport_height: int = Field(default=960, ge=240, le=3840)
    device_mode: Literal["desktop", "mobile"] = "desktop"
    max_node_executions: int = Field(default=100, ge=1, le=10000)
    initial_global_data: dict = Field(default_factory=dict)
    definition: dict = Field(default_factory=lambda: {"nodes": [], "start_node": None})

    @field_validator("cron")
    @classmethod
    def validate_cron(cls, value, info):
        if info.data.get("enabled") and not value:
            raise ValueError("启用定时时必须填写 CRON 表达式")
        if value:
            from croniter import croniter
            if not croniter.is_valid(value):
                raise ValueError("CRON 表达式无效")
        return value

    @field_validator("definition")
    @classmethod
    def validate_definition(cls, value):
        nodes = value.get("nodes", [])
        if not isinstance(nodes, list):
            raise ValueError("流程 nodes 必须是数组")
        ids = [node.get("id") for node in nodes if isinstance(node, dict)]
        if len(ids) != len(nodes) or any(not isinstance(node_id, str) or not node_id for node_id in ids):
            raise ValueError("每个流程节点都必须有非空字符串 ID")
        if len(set(ids)) != len(ids):
            raise ValueError("流程节点 ID 必须唯一")
        allowed = {"automation", "human_operation", "human_input", "condition", "delay", "url_jump", "end"} | set(automation_processor_manager.definitions())
        node_ids = set(ids)
        start = value.get("start_node")
        if start is not None and start not in node_ids:
            raise ValueError("流程起始节点不存在")
        for node in nodes:
            if node.get("type") not in allowed:
                raise ValueError(f"未注册的节点类型：{node.get('type')}")
            if node.get("type") == "automation" and node.get("processor_type") not in automation_processor_manager.definitions():
                raise ValueError(f"未注册的自动化处理器：{node.get('processor_type')}")
            targets = [node.get("next")]
            if node.get("type") == "condition":
                config = node.get("config", {})
                targets.append(config.get("default_target"))
                targets.extend(rule.get("target") for rule in config.get("rules", []))
            if any(target is not None and target not in node_ids for target in targets):
                raise ValueError(f"节点 {node.get('name') or node.get('id')} 的跳转目标不存在")
        return value


class StartPayload(BaseModel):
    data: dict = Field(default_factory=dict)


class RecordingNamePayload(BaseModel):
    name: str = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("请输入录制名称")
        return value


def artifact_directory() -> Path:
    profile_dir = Path(os.getenv("BROWSER_DATA_DIR", str(ROOT / ".browser-data")))
    directory = Path(os.getenv("ARTIFACTS_DIR", str(profile_dir.parent / "artifacts")))
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(directory, 0o700)
    return directory


def begin_recording(run_id: int) -> int:
    with Session() as session:
        run = session.get(Run, run_id)
        if not run:
            raise LookupError("运行实例不存在")
        recording = Recording(run_id=run_id, status="recording")
        session.add(recording)
        session.commit()
        session.refresh(recording)
        recording_id = recording.id
    browser.recordings[run_id] = {"id": recording_id, "sequence": 0}
    return recording_id


async def stop_recording(run_id: int) -> int | None:
    current = browser.recordings.pop(run_id, None)
    if not current:
        return None
    with Session() as session:
        recording = session.get(Recording, current["id"])
        if recording and recording.status == "recording":
            recording.finished_at = utcnow()
            recording.status = "draft"
            session.commit()
    return current["id"]


def save_recording_action(run_id: int, action: str, details: dict, screenshot: bytes) -> int | None:
    current = browser.recordings.get(run_id)
    if not current:
        return None
    sequence = current["sequence"] + 1
    path = artifact_directory() / f"recording-{current['id']}-action-{sequence}.jpg"
    path.write_bytes(screenshot)
    try:
        with Session() as session:
            row = RecordingAction(recording_id=current["id"], sequence=sequence, action=action,
                                  details=details, occurred_at=utcnow(), screenshot_path=str(path))
            session.add(row)
            session.commit()
    except Exception:
        path.unlink(missing_ok=True)
        raise
    current["sequence"] = sequence
    return sequence


def network_event_json(item: NetworkEvent) -> dict:
    return {"id": item.id, "capture_index": item.capture_index, "url": item.url,
            "method": item.method, "resource_type": item.resource_type,
            "started_at": item.started_at.isoformat(),
            "request_headers": item.request_headers, "request_body": item.request_body,
            "request_body_truncated": item.request_body_truncated,
            "request_body_skipped": item.request_body_skipped, "status": item.status,
            "response_status": item.response_status, "response_status_text": item.response_status_text,
            "response_headers": item.response_headers, "response_body": item.response_body,
            "response_body_truncated": item.response_body_truncated,
            "response_body_skipped": item.response_body_skipped,
            "response_body_bytes": item.response_body_bytes, "failure": item.failure,
            "finished_at": item.finished_at.isoformat() if item.finished_at else None}


def recording_json(recording: Recording, *, include_details: bool = False) -> dict:
    result = {"id": recording.id, "run_id": recording.run_id, "name": recording.name,
              "status": recording.status, "started_at": recording.started_at,
              "finished_at": recording.finished_at,
              "action_count": len(recording.actions),
              "network_event_count": len(recording.network_events or [])}
    if include_details:
        result["actions"] = [{"id": action.id, "sequence": action.sequence, "action": action.action,
                               "details": action.details, "occurred_at": action.occurred_at,
                               "screenshot_url": f"/api/recordings/{recording.id}/actions/{action.id}/screenshot"}
                              for action in recording.actions]
        result["network_events"] = recording.network_events or []
    return result


def task_json(task: Task) -> dict:
    return {"id": task.id, "name": task.name, "enabled": task.enabled, "cron": task.cron,
            "viewport_width": task.viewport_width, "viewport_height": task.viewport_height,
            "device_mode": task.device_mode,
            "max_node_executions": task.max_node_executions,
            "initial_global_data": task.initial_global_data, "definition": task.definition,
            "created_at": task.created_at, "updated_at": task.updated_at}


def run_json(run: Run, include_nodes=True) -> dict:
    current_started = run.current_node_started_at
    if current_started and current_started.tzinfo is None:
        current_started = current_started.replace(tzinfo=timezone.utc)
    current_elapsed = int((utcnow() - current_started).total_seconds()) if current_started else None
    payload = {"id": run.id, "task_id": run.task_id, "task_name": run.task_name, "status": run.status,
               "started_at": run.started_at, "finished_at": run.finished_at,
               "current_node_id": run.current_node_id, "current_node_name": run.current_node_name,
               "current_node_started_at": current_started, "current_node_elapsed_seconds": current_elapsed,
               "start_data": run.start_data, "global_data": run.global_data, "error": run.error,
               "node_count": run.node_count, "max_node_executions": run.definition_snapshot.get("max_node_executions"),
               "definition_snapshot": run.definition_snapshot, "browser_available": run.id in browser.pages}
    if include_nodes:
        payload["nodes"] = [{"id": item.id, "node_id": item.node_id, "name": item.node_name,
                             "type": item.node_type, "status": item.status, "started_at": item.started_at,
                             "finished_at": item.finished_at, "duration_ms": item.duration_ms,
                             "input": item.input_data, "output": item.output_data, "logs": item.logs,
                             "error": item.error,
                             "screenshot_url": f"/api/runs/{run.id}/nodes/{item.id}/screenshot" if item.screenshot_path else None}
                            for item in run.executions]
    return payload


@app.get("/")
async def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/nodes")
async def get_nodes():
    return {"builtins": [
        {"type": "human_operation", "title": "人工操作", "input_schema": {}, "output_schema": {"type": "object"}},
        {"type": "human_input", "title": "人工数据输入", "input_schema": {}, "output_schema": {"type": "object"}},
        {"type": "condition", "title": "条件判断", "input_schema": {}, "output_schema": {}},
        {"type": "delay", "title": "延时等待", "input_schema": {"seconds": {"type": "integer"}}, "output_schema": {"seconds": {"type": "integer"}}},
        {"type": "url_jump", "title": "URL 跳转", "input_schema": {"url": {"type": "string"}}, "output_schema": {}},
        {"type": "end", "title": "结束", "input_schema": {}, "output_schema": {}},
    ], "automation": catalog()}


@app.get("/api/settings")
async def get_settings():
    return {"timezone": os.getenv("TZ", "server local"), "max_parallel_tasks": "unlimited"}


@app.get("/api/tasks")
async def list_tasks():
    with Session() as session:
        tasks = session.scalars(select(Task).order_by(Task.id.desc())).all()
        return [task_json(task) for task in tasks]


@app.post("/api/tasks")
async def create_task(payload: TaskPayload):
    with Session() as session:
        task = Task(**payload.model_dump())
        session.add(task)
        session.commit()
        session.refresh(task)
        return task_json(task)


@app.put("/api/tasks/{task_id}")
async def update_task(task_id: int, payload: TaskPayload):
    with Session() as session:
        task = session.get(Task, task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        if session.scalar(select(Run.id).where(Run.task_id == task_id, Run.status.in_(workflow.ACTIVE)).limit(1)):
            raise HTTPException(409, "任务有实例正在运行或等待人工，暂不能修改编排")
        for key, value in payload.model_dump().items():
            setattr(task, key, value)
        task.updated_at = utcnow()
        session.commit()
        return task_json(task)


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: int):
    with Session() as session:
        task = session.get(Task, task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        if session.scalar(select(Run.id).where(Run.task_id == task_id, Run.status.in_(workflow.ACTIVE)).limit(1)):
            raise HTTPException(409, "任务仍有运行实例")
        session.delete(task)
        session.commit()
        return Response(status_code=204)


@app.post("/api/tasks/{task_id}/runs")
async def start_task(task_id: int, payload: StartPayload):
    try:
        run_id = await workflow.start_run(task_id, payload.data)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    if run_id is None:
        raise HTTPException(409, "该任务已有运行中或等待人工的实例，本次触发已跳过")
    return {"id": run_id}


@app.get("/api/runs")
async def list_runs():
    with Session() as session:
        runs = session.scalars(select(Run).order_by(Run.id.desc()).limit(300)).all()
        return [run_json(run, include_nodes=False) for run in runs]


@app.get("/api/runs/{run_id}")
async def get_run(run_id: int, include_nodes: bool = True):
    with Session() as session:
        run = session.get(Run, run_id)
        if not run:
            raise HTTPException(404, "运行实例不存在")
        return run_json(run, include_nodes=include_nodes)


@app.get("/api/recordings")
async def list_recordings():
    with Session() as session:
        records = session.scalars(select(Recording).order_by(Recording.id.desc()).limit(500)).all()
        result = []
        for recording in records:
            item = recording_json(recording)
            run = session.get(Run, recording.run_id)
            item["task_name"] = run.task_name if run else "已删除的任务"
            result.append(item)
        return result


@app.get("/api/recordings/{recording_id}")
async def get_recording(recording_id: int):
    with Session() as session:
        recording = session.get(Recording, recording_id)
        if not recording:
            raise HTTPException(404, "录制内容不存在")
        if recording.status != "saved":
            raise HTTPException(409, "录制内容尚未保存")
        item = recording_json(recording, include_details=True)
        run = session.get(Run, recording.run_id)
        item["task_name"] = run.task_name if run else "已删除的任务"
        return item


@app.post("/api/recordings/{recording_id}/save")
async def save_recording(recording_id: int, payload: RecordingNamePayload):
    with Session() as session:
        recording = session.get(Recording, recording_id)
        if not recording:
            raise HTTPException(404, "录制内容不存在")
        if recording.status == "recording" or recording.finished_at is None:
            raise HTTPException(409, "请先锁定操作，再保存录制")
        if recording.status != "draft":
            raise HTTPException(409, "该录制内容已处理")
        events = session.scalars(select(NetworkEvent).where(
            NetworkEvent.run_id == recording.run_id,
            NetworkEvent.started_at >= recording.started_at,
            NetworkEvent.started_at <= recording.finished_at
        ).order_by(NetworkEvent.started_at, NetworkEvent.id)).all()
        recording.name = payload.name
        recording.network_events = [network_event_json(event) for event in events]
        recording.status = "saved"
        session.commit()
        session.refresh(recording)
        return recording_json(recording)


@app.delete("/api/recordings/{recording_id}")
async def delete_recording(recording_id: int):
    with Session() as session:
        recording = session.get(Recording, recording_id)
        if not recording:
            raise HTTPException(404, "录制内容不存在")
        if recording.status == "recording":
            raise HTTPException(409, "正在录制，锁定操作后再删除")
        paths = [Path(action.screenshot_path) for action in recording.actions]
        for path in paths:
            if path.is_file():
                path.unlink()
        session.delete(recording)
        session.commit()
    return Response(status_code=204)


@app.get("/api/recordings/{recording_id}/actions/{action_id}/screenshot")
async def recording_screenshot(recording_id: int, action_id: int):
    with Session() as session:
        action = session.scalar(select(RecordingAction).join(Recording).where(
            RecordingAction.id == action_id, RecordingAction.recording_id == recording_id,
            Recording.status == "saved"))
        if not action:
            raise HTTPException(404, "录制截图不存在")
        path = Path(action.screenshot_path)
    if not path.is_file():
        raise HTTPException(404, "录制截图文件不存在")
    return FileResponse(path, media_type="image/jpeg", filename=path.name)


@app.delete("/api/runs/{run_id}")
async def delete_run(run_id: int):
    with Session() as session:
        if not session.get(Run, run_id):
            raise HTTPException(404, "运行实例不存在")

    # Stop execution and drain network capture writes before deleting dependent rows.
    await workflow.stop_run_for_deletion(run_id)

    with Session() as session:
        run = session.get(Run, run_id)
        if not run:
            raise HTTPException(404, "运行实例不存在")
        screenshot_paths = session.scalars(select(NodeExecution.screenshot_path).where(
            NodeExecution.run_id == run_id, NodeExecution.screenshot_path.is_not(None))).all()
        recording_paths = session.scalars(select(RecordingAction.screenshot_path).join(
            Recording, RecordingAction.recording_id == Recording.id).where(
                Recording.run_id == run_id)).all()
        for path in {Path(path) for path in recording_paths if path}:
            if path.is_file():
                try:
                    path.unlink()
                except OSError as error:
                    raise HTTPException(500, f"无法删除录制截图文件：{path.name}") from error
        # Filenames are generated with the run ID. This also removes artifacts
        # left behind when their DB path did not get committed.
        artifact_dirs = {Path(path).parent for path in screenshot_paths if path}
        profile_dir = Path(os.getenv("BROWSER_DATA_DIR", str(ROOT / ".browser-data")))
        artifact_dirs.add(Path(os.getenv("ARTIFACTS_DIR", str(profile_dir.parent / "artifacts"))))
        for directory in artifact_dirs:
            if not directory.is_dir():
                continue
            for path in directory.glob(f"run-{run_id}-node-*.jpg"):
                if path.is_file():
                    try:
                        path.unlink()
                    except OSError as error:
                        raise HTTPException(500, f"无法删除截图文件：{path.name}") from error
        session.delete(run)
        session.commit()
    return Response(status_code=204)


@app.get("/api/runs/{run_id}/network")
async def get_run_network(run_id: int, page: int = Query(default=1, ge=1),
                          page_size: int = Query(default=10, ge=1, le=100),
                          after_id: int | None = Query(default=None, ge=0),
                          limit: int = Query(default=100, ge=1, le=500)):
    with Session() as session:
        if not session.get(Run, run_id):
            raise HTTPException(404, "运行实例不存在")
        filters = (NetworkEvent.run_id == run_id,)
        total = session.scalar(select(func.count(NetworkEvent.id)).where(*filters)) or 0
        if after_id is not None:
            records = session.scalars(select(NetworkEvent).where(
                *filters, NetworkEvent.id > after_id
            ).order_by(NetworkEvent.id).limit(limit + 1)).all()
            has_more = len(records) > limit
            records = records[:limit]
            next_after_id = records[-1].id if records else after_id
            page_info = {"next_after_id": next_after_id, "has_more": has_more}
        else:
            records = session.scalars(select(NetworkEvent).where(*filters).order_by(
                NetworkEvent.started_at.desc(), NetworkEvent.id.desc()
            ).offset((page - 1) * page_size).limit(page_size)).all()
            page_info = {"page": page, "page_size": page_size,
                         "total_pages": max(1, (total + page_size - 1) // page_size)}
        return {"items": [{"id": item.id, "capture_index": item.capture_index, "run_id": item.run_id,
            "url": item.url, "method": item.method, "resource_type": item.resource_type,
            "started_at": item.started_at, "request_headers": item.request_headers,
            "request_body": item.request_body, "request_body_truncated": item.request_body_truncated,
            "request_body_skipped": item.request_body_skipped, "status": item.status,
            "response_status": item.response_status, "response_status_text": item.response_status_text,
            "response_headers": item.response_headers, "response_body": item.response_body,
            "response_body_truncated": item.response_body_truncated,
            "response_body_skipped": item.response_body_skipped, "response_body_bytes": item.response_body_bytes,
            "failure": item.failure, "finished_at": item.finished_at} for item in records],
            "total": total, **page_info}


@app.get("/api/runs/{run_id}/nodes")
async def get_run_nodes(run_id: int, page: int = Query(default=1, ge=1),
                        page_size: int = Query(default=10, ge=1, le=100)):
    with Session() as session:
        if not session.get(Run, run_id):
            raise HTTPException(404, "运行实例不存在")
        filters = (NodeExecution.run_id == run_id,)
        total = session.scalar(select(func.count(NodeExecution.id)).where(*filters)) or 0
        records = session.scalars(select(NodeExecution).where(*filters).order_by(
            NodeExecution.started_at.desc(), NodeExecution.id.desc()
        ).offset((page - 1) * page_size).limit(page_size)).all()
        total_pages = max(1, (total + page_size - 1) // page_size)
        return {"items": [{"id": item.id, "node_id": item.node_id, "name": item.node_name,
            "type": item.node_type, "status": item.status, "started_at": item.started_at,
            "finished_at": item.finished_at, "duration_ms": item.duration_ms,
            "input": item.input_data, "output": item.output_data, "logs": item.logs,
            "error": item.error,
            "screenshot_url": f"/api/runs/{run_id}/nodes/{item.id}/screenshot" if item.screenshot_path else None}
            for item in records], "page": page, "page_size": page_size,
            "total": total, "total_pages": total_pages}


@app.post("/api/runs/{run_id}/human-operation")
async def complete_human_operation(run_id: int):
    with Session() as session:
        run = session.get(Run, run_id)
        if not run or run.status != "waiting_operation":
            raise HTTPException(409, "当前实例不在等待人工操作")
    if not workflow.submit_human(run_id):
        raise HTTPException(409, "人工操作节点已被处理")
    _update_node_output(run_id, {})
    return {"ok": True}


def _update_node_output(run_id: int, data: dict):
    from db import NodeExecution
    with Session() as session:
        item = session.scalar(select(NodeExecution).where(NodeExecution.run_id == run_id,
                                       NodeExecution.status == "running").order_by(NodeExecution.id.desc()).limit(1))
        if item:
            item.status, item.output_data, item.finished_at = "completed", data, utcnow()
            item.duration_ms = int((item.finished_at - item.started_at).total_seconds() * 1000)
            session.commit()


@app.post("/api/runs/{run_id}/human-input")
async def submit_human_input(run_id: int, request: Request):
    try:
        data = await request.json()
    except ValueError as error:
        raise HTTPException(400, "输入内容无法识别") from error
    if not isinstance(data, dict):
        raise HTTPException(400, "输入内容必须是对象")
    with Session() as session:
        run = session.get(Run, run_id)
        if not run or run.status != "waiting_input":
            raise HTTPException(409, "当前实例不在等待人工输入")
        node = next((n for n in run.definition_snapshot.get("nodes", []) if n.get("id") == run.current_node_id), {})
        errors = validate_human_form(node.get("config", {}).get("fields", []), data)
        if errors:
            raise HTTPException(422, detail={"message": "请检查输入字段", "fields": errors})
    if not workflow.submit_human(run_id, data):
        raise HTTPException(409, "人工输入节点已被处理")
    _update_node_output(run_id, data)
    return {"ok": True}


def validate_human_form(fields: list, data: dict) -> dict:
    errors = {}
    for field in fields:
        name, kind = field.get("name"), field.get("type", "text")
        try:
            value = workflow.path_get(data, name)
        except (KeyError, IndexError, TypeError, ValueError):
            value = None
        label = field.get("label", name)
        if field.get("required") and (value is None or value == ""):
            errors[name] = f"{label}为必填项"
        elif value is not None and value != "":
            try:
                if kind == "number":
                    number = float(value)
                    if field.get("min") is not None and number < field["min"]: raise ValueError
                    if field.get("max") is not None and number > field["max"]: raise ValueError
                elif kind == "boolean" and not isinstance(value, bool):
                    raise ValueError
                elif kind == "date":
                    from datetime import date
                    date.fromisoformat(str(value))
                elif kind in ("text", "select"):
                    if not isinstance(value, str): raise ValueError
                    if field.get("min_length") is not None and len(value) < field["min_length"]: raise ValueError
                    if field.get("max_length") is not None and len(value) > field["max_length"]: raise ValueError
                    if kind == "select" and value not in field.get("options", []): raise ValueError
            except (ValueError, TypeError):
                errors[name] = f"{label}格式或范围不符合要求"
    return errors


@app.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: int):
    if not await workflow.cancel_run(run_id):
        raise HTTPException(409, "运行实例已结束")
    return {"ok": True}


@app.post("/api/runs/{run_id}/rerun")
async def rerun(run_id: int, payload: StartPayload | None = None):
    with Session() as session:
        run = session.get(Run, run_id)
        if not run or run.status != "error":
            raise HTTPException(409, "仅出错实例可以重新发起")
        task_id, saved_data = run.task_id, run.start_data
    result = await workflow.start_run(task_id, payload.data if payload else saved_data, source_run_id=run_id)
    if result is None:
        raise HTTPException(409, "该任务已有运行实例，本次触发已跳过")
    return {"id": result}


@app.get("/api/tasks/{task_id}/export")
async def export_task(task_id: int):
    with Session() as session:
        task = session.get(Task, task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        return {"format": "automation-task-v1", "task": task_json(task)}


@app.post("/api/tasks/import")
async def import_task(request: Request):
    try:
        data = await request.json()
        source = data["task"]
        payload = TaskPayload.model_validate(source)
    except (ValueError, KeyError, TypeError) as error:
        raise HTTPException(400, "任务配置文件格式无效") from error
    with Session() as session:
        existing = session.scalar(select(Task.id).where(Task.name == payload.name).limit(1))
        if existing:
            raise HTTPException(409, "存在同名任务，请先修改导入文件中的任务名称")
        values = payload.model_dump()
        values["enabled"] = False
        task = Task(**values)
        session.add(task)
        session.commit()
        session.refresh(task)
        return task_json(task)


@app.get("/api/runs/{run_id}/screenshot")
async def screenshot(run_id: int):
    page = browser.pages.get(run_id)
    if not page or page.is_closed():
        raise HTTPException(409, "该实例没有可用的浏览器标签页")
    try:
        async with browser.locks[run_id]:
            image = await page.screenshot(full_page=True, type="png", animations="disabled")
    except PlaywrightError as error:
        raise HTTPException(502, f"截图失败：{error}") from error
    return Response(image, media_type="image/png", headers={"Content-Disposition": f'attachment; filename="run-{run_id}.png"'})


@app.get("/api/runs/{run_id}/nodes/{execution_id}/screenshot")
async def node_screenshot(run_id: int, execution_id: int):
    from db import NodeExecution
    with Session() as session:
        record = session.scalar(select(NodeExecution).where(NodeExecution.id == execution_id,
            NodeExecution.run_id == run_id))
        if not record or not record.screenshot_path:
            raise HTTPException(404, "此节点没有保存截图")
        path = Path(record.screenshot_path)
    if not path.is_file():
        raise HTTPException(404, "节点截图文件不存在")
    return FileResponse(path, media_type="image/jpeg", filename=path.name)


async def capture_recorded_action(run_id: int, action: str, details: dict) -> int | None:
    if run_id not in browser.recordings:
        return None
    page = browser.pages.get(run_id)
    if not page or page.is_closed():
        return None
    async with browser.locks[run_id]:
        screenshot = await page.screenshot(type="jpeg", quality=82, animations="disabled")
    return save_recording_action(run_id, action, details, screenshot)


async def handle_browser_command(websocket: WebSocket, run_id: int, message: dict):
    page = browser.pages.get(run_id)
    if not page or page.is_closed():
        await websocket.send_json({"type": "error", "message": "任务浏览器标签页不可用"})
        return
    action = message.get("action")
    try:
        if action == "unlock":
            if run_id not in browser.controllers or browser.controllers[run_id] is websocket:
                browser.controllers[run_id] = websocket
                if message.get("record") and run_id not in browser.recordings:
                    recording_id = begin_recording(run_id)
                    await websocket.send_json({"type": "recording_started", "id": recording_id})
            await broadcast_control(run_id)
        elif action == "lock":
            if browser.controllers.get(run_id) is websocket:
                browser.controllers.pop(run_id, None)
                recording_id = await stop_recording(run_id)
                await broadcast_control(run_id, stopped_recording_id=recording_id)
                if recording_id:
                    await websocket.send_json({"type": "recording_stopped", "id": recording_id})
        elif action == "navigate" and browser.controllers.get(run_id) is websocket:
            url = valid_url(message.get("url"))
            if not url:
                raise ValueError("网址为空或格式无效，仅支持 HTTP/HTTPS")
            await browser.goto(run_id, url)
            sequence = await capture_recorded_action(run_id, "navigate", {"url": _safe_url(url)})
            if sequence:
                await websocket.send_json({"type": "recording_action", "sequence": sequence})
        elif browser.controllers.get(run_id) is websocket:
            details = {}
            async with browser.locks[run_id]:
                if action in ("mouse_down", "mouse_up"):
                    x, y = float(message["x"]), float(message["y"])
                    button = message.get("button", "left")
                    if button not in ("left", "middle", "right"):
                        raise ValueError("鼠标按键无效")
                    await page.mouse.move(x, y)
                    await getattr(page.mouse, "down" if action == "mouse_down" else "up")(button=button)
                    details = {"x": x, "y": y, "button": button}
                elif action == "move":
                    await page.mouse.move(float(message["x"]), float(message["y"]))
                elif action == "wheel":
                    dx, dy = float(message.get("deltaX", 0)), float(message.get("deltaY", 0))
                    await page.mouse.wheel(dx, dy)
                    details = {"delta_x": dx, "delta_y": dy}
                elif action == "key":
                    key = str(message.get("key", ""))[:200]
                    if len(key) == 1:
                        await page.keyboard.insert_text(key)
                    elif key:
                        await page.keyboard.press(key)
                    details = {"key": key}
                else:
                    return
            if action in ("mouse_down", "mouse_up", "wheel", "key"):
                sequence = await capture_recorded_action(run_id, action, details)
                if sequence:
                    await websocket.send_json({"type": "recording_action", "sequence": sequence})
        else:
            await websocket.send_json({"type": "error", "message": "请先解锁操作"})
    except Exception as error:
        await websocket.send_json({"type": "error", "message": f"操作失败：{error}"})


async def broadcast_control(run_id: int, stopped_recording_id: int | None = None):
    controller = browser.controllers.get(run_id)
    recording = browser.recordings.get(run_id)
    for connection in tuple(browser.connections.get(run_id, set())):
        try:
            await connection.send_json({"type": "control", "locked": controller is not None,
                                        "owned": controller is connection,
                                        "recording": recording is not None,
                                        "recording_count": recording["sequence"] if recording else 0,
                                        "recording_id": recording["id"] if recording else None,
                                        "stopped_recording_id": stopped_recording_id})
        except Exception:
            browser.connections.get(run_id, set()).discard(connection)


@app.websocket("/ws/runs/{run_id}")
async def run_browser_stream(websocket: WebSocket, run_id: int):
    if not authorized(websocket.headers.get("authorization", "")):
        await websocket.close(code=4401, reason="Authentication required")
        return
    if run_id not in browser.pages:
        await websocket.close(code=4404, reason="Browser page unavailable")
        return
    origin = websocket.headers.get("origin")
    if origin and urlparse(origin).netloc != websocket.headers.get("host"):
        await websocket.close(code=4403, reason="Cross-origin request rejected")
        return
    await websocket.accept()
    browser.connections[run_id].add(websocket)
    page = browser.pages[run_id]
    try:
        await websocket.send_json({"type": "control", "locked": run_id in browser.controllers,
                                   "owned": browser.controllers.get(run_id) is websocket,
                                   "recording": run_id in browser.recordings,
                                   "recording_count": browser.recordings.get(run_id, {}).get("sequence", 0),
                                   "recording_id": browser.recordings.get(run_id, {}).get("id")})
        await websocket.send_json({"type": "status", "url": page.url})
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive_json(), timeout=0.16)
                if isinstance(message, dict):
                    await handle_browser_command(websocket, run_id, message)
            except asyncio.TimeoutError:
                pass
            page = browser.pages.get(run_id)
            if not page or page.is_closed():
                await websocket.send_json({"type": "error", "message": "任务浏览器标签页已关闭"})
                break
            async with browser.locks[run_id]:
                frame = await page.screenshot(type="jpeg", quality=68, animations="disabled")
                current_url = page.url
            await websocket.send_json({"type": "status", "url": current_url})
            await websocket.send_bytes(frame)
    except WebSocketDisconnect:
        pass
    except PlaywrightError:
        await websocket.send_json({"type": "error", "message": "连接已断开"})
    finally:
        browser.connections.get(run_id, set()).discard(websocket)
        if browser.controllers.get(run_id) is websocket:
            browser.controllers.pop(run_id, None)
            await stop_recording(run_id)
            await broadcast_control(run_id)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, proxy_headers=True)
