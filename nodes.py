from __future__ import annotations

import importlib
import os
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from browser_tools import BrowserTools


@dataclass
class NodeContext:
    run_id: int
    node_id: str
    global_data: dict[str, Any]
    page: Any
    log: Callable[[str], None]
    tools: BrowserTools
    network_capture: Any = None

    def get_network_events(self, after_index: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        """Return sanitized HTTP events captured for this run, in browser arrival order."""
        if self.network_capture is None:
            return []
        return self.network_capture.get_events(after_index=after_index, limit=limit)

    async def wait_for_network_events(self, after_revision: int = 0, timeout: float = 10) -> int:
        """Wait for a captured request/response update; returns the current revision."""
        if self.network_capture is None:
            return after_revision
        return await self.network_capture.wait_for_events(after_revision=after_revision, timeout=timeout)


class AutomationProcessor(ABC):
    """Contract implemented by trusted, pre-written automation processors."""

    @property
    @abstractmethod
    def type(self) -> str:
        """Stable identifier used by workflow definitions."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable processor name shown in the workflow editor."""

    @property
    @abstractmethod
    def input_description(self) -> dict[str, Any]:
        """Describe the object passed to process()."""

    @property
    @abstractmethod
    def output_description(self) -> dict[str, Any]:
        """Describe the object returned by process()."""

    @property
    def timeout(self) -> int:
        return 300

    @abstractmethod
    async def process(self, context: NodeContext, inputs: dict[str, Any]) -> dict[str, Any]:
        """Process mapped input and return an object for downstream nodes."""


@dataclass(frozen=True)
class NodeDefinition:
    type: str
    title: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    handler: Callable[[NodeContext, dict[str, Any]], Awaitable[dict[str, Any]] | dict[str, Any]]
    timeout: int = 300
    input_description: dict[str, Any] | None = None
    output_description: dict[str, Any] | None = None


class AutomationProcessorManager:
    """Stores and exposes the processors available to workflow definitions."""

    def __init__(self) -> None:
        self._processors: dict[str, AutomationProcessor] = {}
        self._legacy_aliases: dict[str, AutomationProcessor] = {}

    def register(self, processor: AutomationProcessor) -> None:
        if not isinstance(processor, AutomationProcessor):
            raise TypeError("自动化处理器必须实现 AutomationProcessor 接口")
        processor_type = processor.type
        if not isinstance(processor_type, str) or not processor_type.strip():
            raise ValueError("自动化处理器 type 必须是非空字符串")
        if processor_type in self._processors:
            raise ValueError(f"Duplicate automation processor type: {processor_type}")
        if not isinstance(processor.name, str) or not processor.name.strip():
            raise ValueError("自动化处理器 name 必须是非空字符串")
        if not isinstance(processor.input_description, dict) or not isinstance(processor.output_description, dict):
            raise TypeError("自动化处理器入参说明和返回值说明必须是对象")
        self._processors[processor_type] = processor

    def get(self, processor_type: str) -> AutomationProcessor | None:
        return self._processors.get(processor_type) or self._legacy_aliases.get(processor_type)

    def register_legacy_alias(self, alias: str, processor: AutomationProcessor) -> None:
        """Keep saved workflows runnable without exposing retired nodes in the catalog."""
        if alias in self._processors or alias in self._legacy_aliases:
            raise ValueError(f"Duplicate automation processor type: {alias}")
        self._legacy_aliases[alias] = processor

    def definitions(self) -> dict[str, NodeDefinition]:
        definitions = {
            processor.type: NodeDefinition(
                type=processor.type,
                title=processor.name,
                input_schema=processor.input_description,
                output_schema=processor.output_description,
                handler=processor.process,
                timeout=processor.timeout,
                input_description=processor.input_description,
                output_description=processor.output_description,
            )
            for processor in self._processors.values()
        }
        for alias, processor in self._legacy_aliases.items():
            definitions[alias] = NodeDefinition(
                type=alias,
                title=processor.name,
                input_schema=processor.input_description,
                output_schema=processor.output_description,
                handler=processor.process,
                timeout=processor.timeout,
                input_description=processor.input_description,
                output_description=processor.output_description,
            )
        return definitions

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "type": processor.type,
                "name": processor.name,
                "title": processor.name,
                "input_description": processor.input_description,
                "output_description": processor.output_description,
                # Keep these aliases for existing catalog consumers.
                "input_schema": processor.input_description,
                "output_schema": processor.output_description,
                "timeout": processor.timeout,
            }
            for processor in self._processors.values()
        ]


automation_processor_manager = AutomationProcessorManager()


def load_plugins() -> None:
    private_processor_dir = Path(__file__).resolve().parent / "automation_processors"
    if private_processor_dir.is_dir():
        processor_path = str(private_processor_dir)
        if processor_path not in sys.path:
            sys.path.insert(0, processor_path)

    modules = [
        "login_recognition",
        "cashout_listing",
        "cancel_listing",
        "close_popup",
    ]
    private_modules = [
        module
        for module in modules
        if (private_processor_dir / f"{module}.py").is_file()
    ]
    external_modules = filter(None, (name.strip() for name in os.getenv("AUTOMATION_NODE_MODULES", "").split(",")))
    for module in dict.fromkeys([*private_modules, *external_modules]):
        importlib.import_module(module)


def catalog() -> list[dict[str, Any]]:
    return automation_processor_manager.catalog()
