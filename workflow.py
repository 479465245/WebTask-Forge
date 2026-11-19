from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from browser_tools import BrowserTools
from db import NodeExecution, Run, Session, Task, utcnow
from nodes import NodeContext, automation_processor_manager

ACTIVE = ("running", "waiting_input", "waiting_operation")
tasks: dict[int, asyncio.Task] = {}
pages: dict[int, Any] = {}
human_waiters: dict[int, asyncio.Future] = {}
cancel_reasons: dict[int, str] = {}
runtime = None


def configure(browser_runtime) -> None:
    global runtime
    runtime = browser_runtime


def path_get(value: Any, path: str) -> Any:
    if not path:
        return value
    for key in path.replace("[", ".").replace("]", "").split("."):
        if key == "":
            continue
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def path_set(value: dict, path: str, item: Any) -> None:
    parts = [p for p in path.replace("[", ".").replace("]", "").split(".") if p]
    if not parts:
        raise ValueError("映射目标字段不能为空")
    cursor: Any = value
    for index, part in enumerate(parts):
        is_last = index == len(parts) - 1
        if isinstance(cursor, list):
            if not part.isdigit():
                raise ValueError(f"数组路径段必须是索引：{part}")
            offset = int(part)
            while len(cursor) <= offset:
                cursor.append(None)
            if is_last:
                cursor[offset] = item
            else:
                next_is_index = parts[index + 1].isdigit()
                if not isinstance(cursor[offset], (dict, list)):
                    cursor[offset] = [] if next_is_index else {}
                cursor = cursor[offset]
        elif isinstance(cursor, dict):
            if is_last:
                cursor[part] = item
            else:
                next_is_index = parts[index + 1].isdigit()
                if not isinstance(cursor.get(part), (dict, list)):
                    cursor[part] = [] if next_is_index else {}
                cursor = cursor[part]
        else:
            raise ValueError("映射路径与已存在数据类型冲突")


def mapped_inputs(node: dict, global_data: dict, previous: Any, start_data: dict | None = None) -> dict:
    result: dict[str, Any] = {}
    for mapping in node.get("input_mappings", []):
        target = mapping.get("target", "")
        source = mapping.get("source", {})
        kind = source.get("kind", "fixed")
        if kind == "fixed":
            item = source.get("value")
        elif kind == "global":
            try:
                item = path_get(global_data, source.get("path", ""))
            except (KeyError, IndexError, TypeError, ValueError):
                item = None
        elif kind == "previous":
            try:
                item = path_get(previous, source.get("path", ""))
            except (KeyError, IndexError, TypeError, ValueError):
                item = None
        elif kind == "start":
            try:
                item = path_get(start_data or {}, source.get("path", ""))
            except (KeyError, IndexError, TypeError, ValueError):
                item = None
        else:
            raise ValueError(f"不支持的数据来源：{kind}")
        path_set(result, target, item)
    return result


def apply_output_mappings(global_data: dict, output: Any, mappings: list, previous: Any = None) -> dict:
    updated = json.loads(json.dumps(global_data))
    for mapping in mappings:
        source = mapping.get("source", {})
        # Older task definitions stored output paths as a string.
        if isinstance(source, str):
            source = {"kind": "output", "path": source}
        kind = source.get("kind", "output")
        if kind == "fixed":
            item = source.get("value")
        else:
            source_data = {"output": output, "global": global_data, "previous": previous}.get(kind)
            if kind not in ("output", "global", "previous"):
                raise ValueError(f"不支持的输出映射来源：{kind}")
            try:
                item = path_get(source_data, source.get("path", ""))
            except (KeyError, IndexError, TypeError, ValueError):
                item = None
        path_set(updated, mapping["target"], item)
    return updated


def _condition_child(script: str, global_data: dict, previous: Any, timeout: int) -> bool:
    payload = json.dumps({"global_data": global_data, "previous": previous}, ensure_ascii=False)
    child_code = r'''
import json, sys, ast
if sys.platform != "win32":
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (max(2, int(sys.argv[2]) + 1), max(2, int(sys.argv[2]) + 1)))
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
p=json.loads(sys.stdin.read())
safe={"len":len,"str":str,"int":int,"float":float,"bool":bool,"min":min,"max":max,"sum":sum,"all":all,"any":any,"abs":abs,"round":round,"sorted":sorted,"enumerate":enumerate,"range":range,"True":True,"False":False,"None":None}
scope={"global_data":p["global_data"],"previous":p["previous"],"result":None}
tree=ast.parse(sys.argv[1],"<condition>","exec")
for node in ast.walk(tree):
    if isinstance(node,(ast.Import,ast.ImportFrom)) or (isinstance(node,ast.Attribute) and node.attr.startswith("__")) or (isinstance(node,ast.Name) and node.id.startswith("__")):
        raise ValueError("条件脚本包含不允许的访问")
exec(compile(tree,"<condition>","exec"),{"__builtins__":safe},scope)
if type(scope.get("result")) is not bool: raise TypeError("条件脚本必须将布尔值赋给 result")
print(json.dumps(scope["result"]))
'''
    try:
        result = subprocess.run([sys.executable, "-I", "-c", child_code, script, str(timeout)], input=payload,
                                capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise TimeoutError("条件脚本执行超时") from error
    if result.returncode:
        raise ValueError(result.stderr.strip()[-1000:] or "条件脚本执行失败")
    return json.loads(result.stdout)


async def _condition(script: str, global_data: dict, previous: Any, timeout: int) -> bool:
    try:
        result = await asyncio.wait_for(asyncio.to_thread(_condition_child, script, global_data, previous, timeout), timeout=timeout + 1)
    except asyncio.TimeoutError as error:
        raise TimeoutError("条件脚本执行超时") from error
    if type(result) is not bool:
        raise ValueError("条件脚本必须返回布尔值")
    return result


def _set_run(run_id: int, **values) -> None:
    with Session() as session:
        run = session.get(Run, run_id)
        if run:
            for key, value in values.items():
                setattr(run, key, value)
            session.commit()


async def _capture_node(run_id: int, execution_id: int) -> str | None:
    page = pages.get(run_id)
    if not page or page.is_closed():
        return None
    try:
        async with runtime.locks[run_id]:
            image = await page.screenshot(type="jpeg", quality=72, animations="disabled")
        profile_dir = Path(os.getenv("BROWSER_DATA_DIR", str(Path(__file__).parent / ".browser-data")))
        artifact_dir = Path(os.getenv("ARTIFACTS_DIR", str(profile_dir.parent / "artifacts")))
        artifact_dir.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(artifact_dir, 0o700)
        path = artifact_dir / f"run-{run_id}-node-{execution_id}.jpg"
        await asyncio.to_thread(path.write_bytes, image)
        if os.name == "posix":
            os.chmod(path, 0o600)
        return str(path)
    except Exception:
        return None


async def start_run(task_id: int, start_data: dict | None = None) -> int | None:
    if runtime and getattr(runtime, "_clearing_storage", False):
        raise ValueError("浏览器存储正在清理，请稍后重试。")
    with Session() as session:
        task = session.get(Task, task_id)
        if not task:
            raise LookupError("任务不存在")
        active = session.scalar(select(Run.id).where(Run.task_id == task_id, Run.status.in_(ACTIVE)).limit(1))
        if active:
            return None
        snapshot = json.loads(json.dumps(task.definition))
        if not snapshot.get("nodes"):
            raise ValueError("任务至少需要配置一个流程节点")
        snapshot["max_node_executions"] = task.max_node_executions
        snapshot["device_mode"] = task.device_mode
        snapshot["viewport_width"] = task.viewport_width
        snapshot["viewport_height"] = task.viewport_height
        initial_global_data = task.initial_global_data or {}
        run_global_data = initial_global_data if start_data is None else start_data
        start_data = start_data or {}
        snapshot["initial_global_data"] = json.loads(json.dumps(initial_global_data))
        run = Run(task_id=task_id, task_name=task.name, status="running", start_data=start_data,
                  global_data=json.loads(json.dumps(run_global_data)), definition_snapshot=snapshot)
        session.add(run)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return None
        run_id = run.id
        width, height, device_mode = task.viewport_width, task.viewport_height, task.device_mode
    try:
        page = await runtime.new_page(run_id, width, height, device_mode)
    except Exception as error:
        _set_run(run_id, status="error", error=f"浏览器异常：{error}", finished_at=utcnow())
        raise
    pages[run_id] = page
    tasks[run_id] = asyncio.create_task(execute_run(run_id))
    return run_id


async def _finish(run_id: int, status: str, error: str | None = None) -> None:
    with Session() as session:
        run = session.get(Run, run_id)
        if run:
            if run.status == "error" and run.error:
                status, error = run.status, run.error
            elif error and run.current_node_id:
                record = session.scalar(select(NodeExecution).where(NodeExecution.run_id == run_id,
                    NodeExecution.node_id == run.current_node_id, NodeExecution.status == "running")
                    .order_by(NodeExecution.id.desc()).limit(1))
                if record:
                    record.status, record.error, record.finished_at = "error", error, utcnow()
                    record.duration_ms = int((record.finished_at - record.started_at).total_seconds() * 1000)
            run.status, run.error, run.finished_at = status, error, utcnow()
            run.current_node_id = run.current_node_name = run.current_node_started_at = None
            session.commit()
    pages.pop(run_id, None)
    if runtime:
        await runtime.close_page(run_id)
    current_task = asyncio.current_task()
    if tasks.get(run_id) is current_task:
        tasks.pop(run_id, None)
    cancel_reasons.pop(run_id, None)


async def _wait_human(run_id: int, status: str) -> dict:
    future = asyncio.get_running_loop().create_future()
    human_waiters[run_id] = future
    _set_run(run_id, status=status)
    try:
        return await future
    finally:
        human_waiters.pop(run_id, None)


def submit_human(run_id: int, value: dict | None = None) -> bool:
    future = human_waiters.get(run_id)
    if not future or future.done():
        return False
    future.set_result(value or {})
    return True


async def execute_run(run_id: int) -> None:
    try:
        with Session() as session:
            run = session.get(Run, run_id)
            definition = json.loads(json.dumps(run.definition_snapshot))
            task = session.get(Task, run.task_id)
            max_exec = task.max_node_executions
            start_data = json.loads(json.dumps(run.start_data))
        nodes = {node["id"]: node for node in definition.get("nodes", [])}
        current = definition.get("start_node") or (definition.get("nodes", [{}])[0].get("id") if nodes else None)
        previous: Any = None
        while current:
            with Session() as session:
                run = session.get(Run, run_id)
                if not run or run.status == "error":
                    return
                count = run.node_count
            if count >= max_exec:
                raise RuntimeError(f"达到最大节点执行次数上限（{max_exec}）")
            node = nodes.get(current)
            if node is None:
                raise ValueError(f"目标节点不存在：{current}")
            kind = node.get("type", "")
            name = node.get("name") or node.get("id")
            started = utcnow()
            with Session() as session:
                run = session.get(Run, run_id)
                run.node_count += 1
                run.current_node_id, run.current_node_name, run.current_node_started_at = current, name, started
                inputs = mapped_inputs(node, json.loads(json.dumps(run.global_data)), previous, start_data)
                execution = NodeExecution(run_id=run_id, node_id=current, node_name=name, node_type=kind,
                                          status="running", input_data=inputs, logs=[])
                session.add(execution)
                session.commit()
                execution_id = execution.id
                global_data = json.loads(json.dumps(run.global_data))
            log_lines: list[str] = []
            def log(message: str) -> None:
                entry = str(message)[:4000]
                log_lines.append(entry)
                with Session() as session:
                    record = session.get(NodeExecution, execution_id)
                    if record:
                        record.logs = list(record.logs or []) + [entry]
                        session.commit()
            config = node.get("config", {})
            inputs = mapped_inputs(node, global_data, previous, start_data)
            next_id = node.get("next")
            output: Any = {}
            try:
                startup_delay = node.get("startup_delay_seconds", 0)
                if isinstance(startup_delay, bool) or not isinstance(startup_delay, int) or startup_delay < 0:
                    raise ValueError("节点启动延时必须是非负整数秒")
                if startup_delay:
                    log(f"启动延时 {startup_delay} 秒，等待后开始执行节点")
                    await asyncio.sleep(startup_delay)
                if kind == "human_operation":
                    output = await _wait_human(run_id, "waiting_operation")
                elif kind == "human_input":
                    output = await _wait_human(run_id, "waiting_input")
                elif kind == "delay":
                    seconds = config.get("seconds", inputs.get("seconds", 0))
                    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 0:
                        raise ValueError("延时时间必须是非负整数秒")
                    await asyncio.sleep(seconds)
                    output = {"seconds": seconds}
                elif kind == "url_jump":
                    url = inputs.get("url", config.get("url", ""))
                    await runtime.goto(run_id, url)
                elif kind == "condition":
                    matched = False
                    for rule in sorted(config.get("rules", []), key=lambda item: item.get("order", 0)):
                        condition_timeout = int(rule.get("timeout", 5))
                        if not 1 <= condition_timeout <= 300:
                            raise ValueError("条件脚本超时须为 1 到 300 秒")
                        hit = await _condition(rule.get("script", ""), global_data, previous, condition_timeout)
                        if hit:
                            next_id, matched = rule.get("target"), True
                            break
                    if not matched:
                        next_id = config.get("default_target")
                    if not next_id:
                        raise ValueError("条件判断未配置有效跳转目标")
                    output = {"target": next_id}
                elif kind == "end":
                    output = config.get("output", {})
                else:
                    processor_type = node.get("processor_type") if kind == "automation" else kind
                    definition = automation_processor_manager.definitions().get(processor_type)
                    if not definition:
                        raise ValueError(f"未注册的自动化处理器：{processor_type}")
                    inputs = mapped_inputs(node, global_data, previous, start_data)
                    context = NodeContext(run_id, current, global_data, pages.get(run_id), log,
                                          BrowserTools(pages.get(run_id), runtime.locks[run_id]),
                                          runtime.network_captures.get(run_id))
                    try:
                        output = await asyncio.wait_for(definition.handler(context, inputs), timeout=int(node.get("timeout", definition.timeout)))
                    except asyncio.TimeoutError as error:
                        raise TimeoutError("自动化处理节点执行超时") from error
                    if output is None:
                        output = {}
                    if not isinstance(output, dict):
                        raise TypeError("自动化节点必须返回对象")
                # The code-stage state is committed before configured output mappings.
                if kind not in ("condition", "human_input", "human_operation"):
                    _set_run(run_id, global_data=global_data)
                if node.get("output_mappings"):
                    mapped_global = apply_output_mappings(global_data, output, node["output_mappings"], previous)
                    _set_run(run_id, global_data=mapped_global)
                    global_data = mapped_global
                screenshot_path = None
                if config.get("capture_screenshot", True):
                    screenshot_path = await _capture_node(run_id, execution_id)
                    if pages.get(run_id) and screenshot_path is None:
                        log("节点截图未能保存")
                with Session() as session:
                    record = session.get(NodeExecution, execution_id)
                    record.status, record.output_data, record.logs = "completed", output, log_lines
                    record.screenshot_path = screenshot_path
                    record.finished_at = utcnow()
                    record.duration_ms = int((record.finished_at - started).total_seconds() * 1000)
                    session.commit()
                previous = output
            except asyncio.CancelledError:
                raise
            except Exception as error:
                screenshot_path = None
                if config.get("capture_screenshot", True):
                    screenshot_path = await _capture_node(run_id, execution_id)
                with Session() as session:
                    record = session.get(NodeExecution, execution_id)
                    record.status, record.error, record.logs = "error", str(error), log_lines
                    record.screenshot_path = screenshot_path
                    record.finished_at = utcnow()
                    record.duration_ms = int((record.finished_at - started).total_seconds() * 1000)
                    session.commit()
                raise RuntimeError(f"节点 {name} 执行失败：{error}") from error
            if kind == "end" or not next_id:
                break
            current = next_id
        await _finish(run_id, "completed")
    except asyncio.CancelledError:
        await _finish(run_id, "error", cancel_reasons.pop(run_id, "人工取消"))
    except Exception as error:
        await _finish(run_id, "error", str(error))


async def cancel_run(run_id: int, reason: str = "人工取消") -> bool:
    task = tasks.get(run_id)
    if not task or task.done():
        return False
    cancel_reasons[run_id] = reason
    task.cancel()
    return True


async def stop_run_for_deletion(run_id: int) -> None:
    """Stop a run completely before its persistent records are removed."""
    task = tasks.get(run_id)
    if task and not task.done():
        cancel_reasons[run_id] = "运行记录已删除"
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    if runtime:
        await runtime.close_page(run_id)
    pages.pop(run_id, None)
    human_waiters.pop(run_id, None)
    cancel_reasons.pop(run_id, None)
    tasks.pop(run_id, None)


async def browser_crashed() -> None:
    with Session() as session:
        runs = session.scalars(select(Run).where(Run.status.in_(ACTIVE))).all()
        ids = []
        for run in runs:
            ids.append(run.id)
            record = session.scalar(select(NodeExecution).where(NodeExecution.run_id == run.id,
                NodeExecution.status == "running").order_by(NodeExecution.id.desc()).limit(1))
            if record:
                record.status, record.error, record.finished_at = "error", "浏览器异常退出", utcnow()
                record.duration_ms = int((record.finished_at - record.started_at).total_seconds() * 1000)
            run.status, run.error, run.finished_at = "error", "浏览器异常退出", utcnow()
            run.current_node_id = run.current_node_name = run.current_node_started_at = None
        session.commit()
    await asyncio.gather(*(cancel_run(run_id, "浏览器异常退出") for run_id in ids), return_exceptions=True)


async def service_stopping() -> None:
    with Session() as session:
        ids = list(session.scalars(select(Run.id).where(Run.status.in_(ACTIVE))).all())
    running = [tasks[run_id] for run_id in ids if run_id in tasks and not tasks[run_id].done()]
    for run_id in ids:
        if run_id in tasks and not tasks[run_id].done():
            cancel_reasons[run_id] = "服务重启"
            tasks[run_id].cancel()
    await asyncio.gather(*running, return_exceptions=True)


async def mark_interrupted_runs() -> None:
    with Session() as session:
        runs = session.scalars(select(Run).where(Run.status.in_(ACTIVE))).all()
        for run in runs:
            record = session.scalar(select(NodeExecution).where(NodeExecution.run_id == run.id,
                NodeExecution.status == "running").order_by(NodeExecution.id.desc()).limit(1))
            if record:
                record.status, record.error, record.finished_at = "error", "服务重启", utcnow()
                record.duration_ms = int((record.finished_at - record.started_at).total_seconds() * 1000)
            run.status, run.error, run.finished_at = "error", "服务重启", utcnow()
            run.current_node_id = run.current_node_name = run.current_node_started_at = None
        session.commit()


async def scheduler_loop() -> None:
    from croniter import croniter
    last_minute = datetime.now().astimezone().replace(second=0, microsecond=0).isoformat()
    while True:
        now = datetime.now().astimezone().replace(second=0, microsecond=0)
        marker = now.isoformat()
        if marker != last_minute:
            last_minute = marker
            with Session() as session:
                scheduled = session.scalars(select(Task).where(Task.enabled.is_(True), Task.cron.is_not(None))).all()
                due_ids = []
                for task in scheduled:
                    try:
                        if croniter.match(task.cron, now):
                            due_ids.append(task.id)
                    except (ValueError, KeyError):
                        continue
            for task_id in due_ids:
                await start_run(task_id)
        await asyncio.sleep(5)
