from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from config import DATABASE_URL


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


_engine_args = {"pool_pre_ping": True}
if DATABASE_URL.startswith("sqlite"):
    _engine_args["connect_args"] = {"check_same_thread": False}
engine = create_engine(DATABASE_URL, **_engine_args)
if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_foreign_keys(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

Session = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    enabled: Mapped[bool] = mapped_column(default=False, nullable=False)
    cron: Mapped[str | None] = mapped_column(String(120))
    viewport_width: Mapped[int] = mapped_column(Integer, default=1440, nullable=False)
    viewport_height: Mapped[int] = mapped_column(Integer, default=960, nullable=False)
    device_mode: Mapped[str] = mapped_column(String(16), default="desktop", nullable=False)
    max_node_executions: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    initial_global_data: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    definition: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    task_name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_node_id: Mapped[str | None] = mapped_column(String(120))
    current_node_name: Mapped[str | None] = mapped_column(String(200))
    current_node_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    start_data: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    global_data: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    definition_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    node_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    executions: Mapped[list["NodeExecution"]] = relationship(back_populates="run", cascade="all, delete-orphan", order_by="NodeExecution.id")


Index("uq_one_active_run_per_task", Run.task_id, unique=True,
      sqlite_where=Run.status.in_(("running", "waiting_input", "waiting_operation")),
      postgresql_where=Run.status.in_(("running", "waiting_input", "waiting_operation")))


class NodeExecution(Base):
    __tablename__ = "node_executions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    node_id: Mapped[str] = mapped_column(String(120), nullable=False)
    node_name: Mapped[str] = mapped_column(String(200), nullable=False)
    node_type: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    input_data: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    output_data: Mapped[dict | None] = mapped_column(JSON)
    logs: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    screenshot_path: Mapped[str | None] = mapped_column(Text)
    run: Mapped[Run] = relationship(back_populates="executions")


class NetworkEvent(Base):
    __tablename__ = "network_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    capture_index: Mapped[int] = mapped_column(Integer, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    method: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(80), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    request_headers: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    request_body: Mapped[str | None] = mapped_column(Text)
    request_body_truncated: Mapped[bool] = mapped_column(default=False, nullable=False)
    request_body_skipped: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_status_text: Mapped[str | None] = mapped_column(String(200))
    response_headers: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    response_body: Mapped[str | None] = mapped_column(Text)
    response_body_truncated: Mapped[bool] = mapped_column(default=False, nullable=False)
    response_body_skipped: Mapped[str | None] = mapped_column(String(80))
    response_body_bytes: Mapped[int | None] = mapped_column(Integer)
    failure: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Recording(Base):
    __tablename__ = "recordings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(24), default="draft", nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    network_events: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    actions: Mapped[list["RecordingAction"]] = relationship(
        back_populates="recording", cascade="all, delete-orphan", order_by="RecordingAction.sequence")


class RecordingAction(Base):
    __tablename__ = "recording_actions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recording_id: Mapped[int] = mapped_column(ForeignKey("recordings.id", ondelete="CASCADE"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    details: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    screenshot_path: Mapped[str] = mapped_column(Text, nullable=False)
    recording: Mapped[Recording] = relationship(back_populates="actions")


Index("uq_network_event_capture_index", NetworkEvent.run_id, NetworkEvent.capture_index, unique=True)
Index("ix_network_event_run_id_id", NetworkEvent.run_id, NetworkEvent.id)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, nullable=False)


def initialize_database() -> None:
    Base.metadata.create_all(engine)
    # Schema changes are additive and applied before the app accepts requests.
    with engine.begin() as connection:
        if engine.dialect.name == "sqlite":
            columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(tasks)")}
            if "viewport_width" not in columns:
                connection.exec_driver_sql("ALTER TABLE tasks ADD COLUMN viewport_width INTEGER NOT NULL DEFAULT 1440")
            if "viewport_height" not in columns:
                connection.exec_driver_sql("ALTER TABLE tasks ADD COLUMN viewport_height INTEGER NOT NULL DEFAULT 960")
            if "device_mode" not in columns:
                connection.exec_driver_sql("ALTER TABLE tasks ADD COLUMN device_mode VARCHAR(16) NOT NULL DEFAULT 'desktop'")
            if "max_node_executions" not in columns:
                connection.exec_driver_sql("ALTER TABLE tasks ADD COLUMN max_node_executions INTEGER NOT NULL DEFAULT 100")
            if "initial_global_data" not in columns:
                connection.exec_driver_sql("ALTER TABLE tasks ADD COLUMN initial_global_data JSON NOT NULL DEFAULT '{}' ")
            execution_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(node_executions)")}
            if "screenshot_path" not in execution_columns:
                connection.exec_driver_sql("ALTER TABLE node_executions ADD COLUMN screenshot_path TEXT")
        elif engine.dialect.name == "postgresql":
            connection.exec_driver_sql("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS device_mode VARCHAR(16) NOT NULL DEFAULT 'desktop'")
            connection.exec_driver_sql("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS initial_global_data JSON NOT NULL DEFAULT '{}'::json")
            connection.exec_driver_sql("ALTER TABLE node_executions ADD COLUMN IF NOT EXISTS screenshot_path TEXT")
