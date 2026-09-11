"""Synchronous durable storage for SQLite and PostgreSQL.

Call synchronous methods in a thread from async applications. Mutating transactions
are short and serialized across processes (SQLite IMMEDIATE / PostgreSQL advisory
lock); network and model operations never hold a database transaction open.
"""
from __future__ import annotations

import json
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import (JSON, Column, DateTime, ForeignKey, Integer, MetaData,
                        String, Table, Text, UniqueConstraint, and_, create_engine,
                        event, func, insert, or_, select, text, update)
from sqlalchemy.pool import StaticPool

from .models import canonical_digest


class StoreError(RuntimeError):
    pass


class LeaseLost(StoreError):
    pass


class ControlConflict(StoreError):
    pass


class DocumentConflict(StoreError):
    """A durable record changed since the caller observed it."""

    pass


def utcnow():
    return datetime.now(timezone.utc)


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def _json(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return json.loads(json.dumps(value, default=lambda x: x.isoformat() if isinstance(x, datetime) else str(x)))


def _out(row):
    if row is None:
        return None
    result = dict(row._mapping if hasattr(row, "_mapping") else row)
    return {k: _utc(v).isoformat() if isinstance(v, datetime) else v for k, v in result.items()}


metadata = MetaData()
jobs = Table("ore_jobs", metadata,
    Column("id", String(36), primary_key=True), Column("mission", JSON, nullable=False),
    Column("rune", JSON), Column("revision", Integer, nullable=False),
    Column("generation", Integer, nullable=False), Column("state", String(40), nullable=False),
    Column("protocol_digest", String(64), nullable=False), Column("input_hash", String(64), nullable=False),
    Column("details", JSON, nullable=False), Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False))
tasks = Table("ore_tasks", metadata,
    Column("id", String(36), primary_key=True), Column("job_id", String(36), ForeignKey("ore_jobs.id"), index=True),
    Column("kind", String(100), nullable=False), Column("input", JSON, nullable=False),
    Column("task_key", String(64), nullable=False), Column("revision", Integer, nullable=False),
    Column("generation", Integer, nullable=False), Column("state", String(40), nullable=False, index=True),
    Column("worker_id", String(200)), Column("fence", Integer, nullable=False),
    Column("lease_expires_at", DateTime(timezone=True)), Column("retry_at", DateTime(timezone=True)),
    Column("attempts", Integer, nullable=False), Column("result", JSON), Column("error", JSON),
    Column("created_at", DateTime(timezone=True), nullable=False), Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("job_id", "revision", "generation", "kind", "task_key"))
attempts_table = Table("ore_attempts", metadata,
    Column("id", String(36), primary_key=True), Column("task_id", String(36), ForeignKey("ore_tasks.id"), index=True),
    Column("worker_id", String(200), nullable=False), Column("fence", Integer, nullable=False),
    Column("revision", Integer, nullable=False), Column("state", String(40), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False), Column("finished_at", DateTime(timezone=True)),
    Column("result", JSON), Column("error", JSON), UniqueConstraint("task_id", "fence"))
events_table = Table("ore_events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("job_id", String(36), ForeignKey("ore_jobs.id"), index=True),
    Column("type", String(100), nullable=False), Column("payload", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False))
# Durable domain records share a storage envelope, not an identity or payload schema.
documents = Table("ore_documents", metadata,
    Column("row_id", String(36), primary_key=True), Column("collection", String(40), nullable=False, index=True),
    Column("document_key", String(64), nullable=False), Column("job_id", String(36), ForeignKey("ore_jobs.id"), index=True),
    Column("generation", Integer, nullable=False), Column("revision", Integer, nullable=False),
    Column("data", JSON, nullable=False), Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False), UniqueConstraint("collection", "document_key"))


class Store:
    def __init__(self, database_url: str = "sqlite:///ore.db"):
        for old, new in (("postgresql+asyncpg:", "postgresql+psycopg:"),
                         ("postgresql:", "postgresql+psycopg:"), ("sqlite+aiosqlite:", "sqlite:")):
            if database_url.startswith(old):
                database_url = database_url.replace(old, new, 1)
                break
        self.database_url = database_url
        self._lock = threading.RLock()
        options: dict[str, Any] = {"pool_pre_ping": True}
        if database_url.startswith("sqlite"):
            options["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if database_url.endswith(":memory:") or database_url == "sqlite://":
                options["poolclass"] = StaticPool
            elif database_url.startswith("sqlite:///"):
                Path(database_url.removeprefix("sqlite:///")).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(database_url, **options)
        if self.engine.dialect.name == "sqlite":
            @event.listens_for(self.engine, "connect")
            def _setup(dbapi, _):
                cursor = dbapi.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.close()

    def initialize(self):
        metadata.create_all(self.engine)

    def close(self):
        self.engine.dispose()

    @contextmanager
    def _tx(self):
        with self._lock, self.engine.begin() as conn:
            if self.engine.dialect.name == "sqlite":
                conn.exec_driver_sql("BEGIN IMMEDIATE")
            elif self.engine.dialect.name == "postgresql":
                conn.execute(text("SELECT pg_advisory_xact_lock(5198405)"))
            yield conn

    def _job(self, conn, job_id):
        row = conn.execute(select(jobs).where(jobs.c.id == job_id)).mappings().first()
        if row is None:
            raise KeyError(f"unknown job: {job_id}")
        return dict(row)

    @staticmethod
    def _job_out(row):
        value = _out(row)
        if value is not None:
            extra = value.pop("details", {}) or {}
            value = {**extra, **value}
            value["status"] = value["state"]
        return value

    def _event(self, conn, job_id, event_type, payload):
        value = dict(job_id=job_id, type=event_type, payload=_json(payload), created_at=utcnow())
        result = conn.execute(insert(events_table).values(**value))
        return _out({"id": result.inserted_primary_key[0], **value})

    def _doc(self, conn, collection, key):
        row = conn.execute(select(documents).where(documents.c.collection == collection,
                           documents.c.document_key == canonical_digest(key))).mappings().first()
        return dict(row) if row else None

    def _put(self, conn, collection, key, data, job=None):
        now, old = utcnow(), self._doc(conn, collection, key)
        payload = _json(data)
        if old:
            changes = {"data": payload, "updated_at": now}
            if job:
                changes.update(job_id=job["id"], generation=job["generation"], revision=job["revision"])
            conn.execute(update(documents).where(documents.c.row_id == old["row_id"]).values(**changes))
            return {**old, **changes}
        value = dict(row_id=str(uuid.uuid4()), collection=collection, document_key=canonical_digest(key),
                     job_id=job["id"] if job else None, generation=job["generation"] if job else 0,
                     revision=job["revision"] if job else 0, data=payload, created_at=now, updated_at=now)
        conn.execute(insert(documents).values(**value))
        return value

    @staticmethod
    def _doc_out(row):
        value = _out(row)
        data = value.pop("data")
        return {**data, "id": data.get("id", value["row_id"]), "job_id": value["job_id"],
                "generation": value["generation"], "revision": value["revision"],
                "created_at": value["created_at"], "updated_at": value["updated_at"]}

    def _list(self, job_id, collection, all_generations=False):
        with self._lock, self.engine.connect() as conn:
            job = self._job(conn, job_id)
            query = select(documents).where(documents.c.job_id == job_id, documents.c.collection == collection)
            if not all_generations:
                query = query.where(documents.c.generation == job["generation"])
            return [self._doc_out(r) for r in conn.execute(query.order_by(documents.c.created_at))]

    def get_document(self, collection, key):
        with self._lock, self.engine.connect() as conn:
            row = self._doc(conn, collection, key)
            return self._doc_out(row) if row else None

    def put_document(self, collection, key, data, job_id=None, lease=None, expected_version=None):
        """Atomically replace a document; expected_version=0 means create only."""
        with self._tx() as conn:
            job = self._job(conn, job_id) if job_id else None
            if lease:
                if not job_id:
                    raise ValueError('A task lease requires a job-bound document')
                self._verify_lease(conn, job_id, lease)
            old = self._doc(conn, collection, key)
            version = int(old['data'].get('state_version', 0)) if old else 0
            if expected_version is not None and version != expected_version:
                raise DocumentConflict('Document changed; reload before retrying')
            if old and old['job_id'] != job_id:
                raise DocumentConflict('Document belongs to another job')
            value = {**data, 'state_version': version + 1}
            return self._doc_out(self._put(conn, collection, key, value, job))

    def list_documents(self, collection, job_id=None, all_generations=False):
        if job_id:
            return self._list(job_id, collection, all_generations=all_generations)
        with self._lock, self.engine.connect() as conn:
            query = select(documents).where(documents.c.collection == collection)
            return [self._doc_out(row) for row in conn.execute(query.order_by(documents.c.created_at))]

    def create_job(self, mission: dict, rune: dict | None = None):
        mission, rune = _json(mission), _json(rune)
        now = utcnow()
        value = dict(id=str(uuid.uuid4()), mission=mission, rune=rune, revision=1, generation=1,
                     protocol_digest=canonical_digest(rune), input_hash=canonical_digest(mission),
                     state="queued", details={}, created_at=now, updated_at=now)
        with self._tx() as conn:
            conn.execute(insert(jobs).values(**value))
            self._put(conn, "revision", [value["id"], 1], {"mission": mission, "rune": rune}, value)
            self._event(conn, value["id"], "job.created", {"revision": 1})
        return self._job_out(value)

    def get_job(self, job_id):
        with self._lock, self.engine.connect() as conn:
            return self._job_out(conn.execute(select(jobs).where(jobs.c.id == job_id)).first())

    def list_jobs(self):
        with self._lock, self.engine.connect() as conn:
            return [self._job_out(r) for r in conn.execute(select(jobs).order_by(jobs.c.created_at.desc()))]

    def update_job(self, job_id, *, lease=None, **fields):
        if {"mission", "rune", "revision", "generation", "input_hash", "protocol_digest"} & fields.keys():
            raise ValueError("use revise_job/refresh_job to change execution contracts")
        with self._tx() as conn:
            job = self._job(conn, job_id)
            self._verify_lease(conn, job_id, lease)
            state = fields.pop("state", fields.pop("status", job["state"]))
            details = {**(job["details"] or {}), **_json(fields)}
            conn.execute(update(jobs).where(jobs.c.id == job_id).values(state=state, details=details, updated_at=utcnow()))
        return self.get_job(job_id)

    def revise_job(self, job_id, mission, rune=None):
        with self._tx() as conn:
            old = self._job(conn, job_id)
            value = dict(old, mission=_json(mission), rune=old["rune"] if rune is None else _json(rune),
                         revision=old["revision"] + 1, state="queued", updated_at=utcnow())
            value.update(input_hash=canonical_digest(value["mission"]), protocol_digest=canonical_digest(value["rune"]))
            conn.execute(update(jobs).where(jobs.c.id == job_id).values(**{k: v for k, v in value.items() if k != "id"}))
            conn.execute(update(tasks).where(tasks.c.job_id == job_id, tasks.c.state.in_(["queued", "running", "retry_wait"])).values(state="superseded", updated_at=utcnow()))
            self._put(conn, "revision", [job_id, value["revision"]], {"mission": value["mission"], "rune": value["rune"]}, value)
            self._event(conn, job_id, "job.revised", {"revision": value["revision"]})
        return self.get_job(job_id)

    def refresh_job(self, job_id):
        with self._tx() as conn:
            job = self._job(conn, job_id)
            conn.execute(update(jobs).where(jobs.c.id == job_id).values(generation=job["generation"] + 1, state="queued", updated_at=utcnow()))
            conn.execute(update(tasks).where(tasks.c.job_id == job_id, tasks.c.state.in_(["queued", "running", "retry_wait"])).values(state="superseded", updated_at=utcnow()))
            self._event(conn, job_id, "job.refreshed", {"generation": job["generation"] + 1})
        return self.get_job(job_id)

    def reset_for_resume(self, job_id, blocked_task_ids=None):
        with self._tx() as conn:
            job = self._job(conn, job_id)
            conn.execute(update(tasks).where(tasks.c.job_id == job_id, tasks.c.revision == job["revision"],
                tasks.c.generation == job["generation"], tasks.c.id.not_in(blocked_task_ids or []),
                tasks.c.state.in_(["paused", "paused_budget", "awaiting_auth", "awaiting_user", "awaiting_source", "blocked"]))
                .values(state="queued", updated_at=utcnow()))
            conn.execute(update(jobs).where(jobs.c.id == job_id).values(state="queued", updated_at=utcnow()))
            self._event(conn, job_id, "job.resumed", {"revision": job["revision"]})
        return self.get_job(job_id)

    def append_event(self, job_id, event_type, payload):
        with self._tx() as conn:
            self._job(conn, job_id)
            return self._event(conn, job_id, event_type, payload)

    def events(self, job_id, after=0):
        with self._lock, self.engine.connect() as conn:
            return [_out(r) for r in conn.execute(select(events_table).where(events_table.c.job_id == job_id,
                events_table.c.id > after).order_by(events_table.c.id))]

    def _resource(self, conn, job, data):
        data = _json(data)
        doi = str(data.get("doi") or "").strip().lower().removeprefix("https://doi.org/").removeprefix("http://doi.org/").removeprefix("doi:").strip()
        identity = data.get("canonical_id") or data.get("resource_key") or doi or data.get("id") or data.get("url")
        if not identity:
            raise ValueError("resource requires an ID, DOI, or observed URL; title alone is insufficient")
        key = [job["id"], job["generation"], str(identity)]
        old = self._doc(conn, "resource", key)
        payload = {**(old["data"] if old else {}), **data}
        payload["id"] = old["data"]["id"] if old else str(data.get("id") or uuid.uuid4())
        payload["resource_key"] = str(identity)
        value = self._put(conn, "resource", key, payload, job)
        self._observation(conn, job, {"kind": "resource", "resource_id": payload["id"], "data": data})
        return self._doc_out(value)

    def _verify_lease(self, conn, job_id, lease):
        if lease is None:
            return
        task = self._owned(conn, lease["task_id"], lease["worker_id"], lease["fence"], lease["revision"])
        if task["job_id"] != job_id:
            raise LeaseLost("task lease belongs to another job")

    def upsert_resource(self, job_id, data, *, lease=None):
        with self._tx() as conn:
            self._verify_lease(conn, job_id, lease)
            return self._resource(conn, self._job(conn, job_id), data)

    def resources(self, job_id, *, all_generations=False):
        return self._list(job_id, "resource", all_generations)

    def add_artifact(self, job_id, data, *, lease=None):
        data = _json(data)
        if data.get("status") == "verified" and data.get("integrity") == "invalid":
            raise ValueError("invalid artifact cannot be marked verified")
        with self._tx() as conn:
            job = self._job(conn, job_id)
            self._verify_lease(conn, job_id, lease)
            identity = data.get("artifact_key") or data.get("id") or canonical_digest({k: data.get(k) for k in
                       ("resource_id", "role", "version", "source_url", "sha256", "path")})
            key = [job_id, job["generation"], identity]
            old = self._doc(conn, "artifact", key)
            if old:
                immutable = ("sha256", "resource_id", "role", "version")
                if any(old["data"].get(key) != data.get(key) for key in immutable):
                    raise ValueError("artifact identity reused for different bytes, role, resource, or version")
                value = old
            else:
                data.setdefault("id", str(uuid.uuid4()))
                value = self._put(conn, "artifact", key, data, job)
            self._observation(conn, job, {"kind": "artifact", "artifact_id": value["data"]["id"], "data": data})
            return self._doc_out(value)

    def artifacts(self, job_id, *, all_generations=False):
        return self._list(job_id, "artifact", all_generations)

    def _observation(self, conn, job, data):
        identity = str(uuid.uuid4())
        return self._doc_out(self._put(conn, "observation", identity, {**_json(data), "id": identity}, job))

    def record_observation(self, job_id, data, *, lease=None):
        with self._tx() as conn:
            self._verify_lease(conn, job_id, lease)
            return self._observation(conn, self._job(conn, job_id), data)

    def observations(self, job_id):
        return self._list(job_id, "observation", True)

    def _task(self, conn, job, kind, input_data, key):
        hashed_key = canonical_digest(key)
        where = and_(tasks.c.job_id == job["id"], tasks.c.revision == job["revision"],
                     tasks.c.generation == job["generation"], tasks.c.kind == kind, tasks.c.task_key == hashed_key)
        old = conn.execute(select(tasks).where(where)).first()
        if old:
            if _json(input_data) != old._mapping["input"]:
                raise ValueError("idempotency key reused with different task inputs")
            return _out(old)
        now = utcnow()
        value = dict(id=str(uuid.uuid4()), job_id=job["id"], kind=kind, input=_json(input_data), task_key=hashed_key,
                     revision=job["revision"], generation=job["generation"], state="queued", worker_id=None,
                     fence=0, lease_expires_at=None, retry_at=None, attempts=0, result=None, error=None,
                     created_at=now, updated_at=now)
        conn.execute(insert(tasks).values(**value))
        return _out(value)

    def create_task(self, job_id, kind, input_data, key, *, lease=None):
        with self._tx() as conn:
            self._verify_lease(conn, job_id, lease)
            return self._task(conn, self._job(conn, job_id), kind, input_data, key)

    def commit_discovery(self, job_id, checkpoint, cursor, resources, new_tasks, *, revision=None, lease=None):
        """Atomically save discovered items, fan-out tasks, and the next cursor."""
        with self._tx() as conn:
            job = self._job(conn, job_id)
            self._verify_lease(conn, job_id, lease)
            if revision is not None and revision != job["revision"]:
                raise LeaseLost("obsolete discovery revision")
            records = [self._resource(conn, job, item) for item in resources]
            created = [self._task(conn, job, item["kind"], item.get("input", item.get("input_data", {})), item["key"]) for item in new_tasks]
            self._put(conn, "checkpoint", [job_id, job["generation"], job["revision"], checkpoint], {"cursor": cursor}, job)
            return {"resources": records, "tasks": created, "cursor": cursor}

    def get_checkpoint(self, job_id, name):
        with self._lock, self.engine.connect() as conn:
            job = self._job(conn, job_id)
            row = self._doc(conn, "checkpoint", [job_id, job["generation"], job["revision"], name])
            return row["data"].get("cursor") if row else None

    def get_task(self, task_id):
        with self._lock, self.engine.connect() as conn:
            return _out(conn.execute(select(tasks).where(tasks.c.id == task_id)).first())

    def tasks(self, job_id):
        with self._lock, self.engine.connect() as conn:
            return [_out(r) for r in conn.execute(select(tasks).where(tasks.c.job_id == job_id).order_by(tasks.c.created_at))]

    def attempts(self, task_id):
        with self._lock, self.engine.connect() as conn:
            return [_out(r) for r in conn.execute(select(attempts_table).where(attempts_table.c.task_id == task_id).order_by(attempts_table.c.fence))]

    def claim_task(self, worker_id, lease_seconds=60, job_id=None, kinds=None, backend_kinds=None):
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        with self._tx() as conn:
            now = utcnow()
            eligible = or_(tasks.c.state == "queued", and_(tasks.c.state == "retry_wait",
                or_(tasks.c.retry_at.is_(None), tasks.c.retry_at <= now)),
                and_(tasks.c.state == "running", tasks.c.lease_expires_at <= now))
            query = select(tasks).join(jobs, jobs.c.id == tasks.c.job_id).where(eligible,
                tasks.c.revision == jobs.c.revision, tasks.c.generation == jobs.c.generation,
                jobs.c.state.in_(["queued", "running", "resuming"]))
            if job_id:
                query = query.where(tasks.c.job_id == job_id)
            if kinds:
                query = query.where(tasks.c.kind.in_(kinds))
            blocked_jobs = set()
            blocked_tasks = set()
            while True:
                candidate = query.where(tasks.c.job_id.not_in(blocked_jobs)) if blocked_jobs else query
                if blocked_tasks:
                    candidate = candidate.where(tasks.c.id.not_in(blocked_tasks))
                old = conn.execute(candidate.order_by(tasks.c.created_at).limit(1)).mappings().first()
                if not old:
                    return None
                current_job = self._job(conn, old["job_id"])
                if not self._workflow_control_valid(conn, old, current_job):
                    blocked_tasks.add(old["id"])
                    continue
                mission = current_job["mission"]
                backend = mission.get("backend", "codex")
                backend = backend.get("kind", "codex") if isinstance(backend, dict) else backend
                cap = int(mission.get("budget", {}).get("max_agent_workers", mission.get("limits", {}).get("max_agent_workers", 5)))
                from .scheduler import task_admission
                control = self._doc(conn, 'scheduler.control', 'global')
                soft = self._doc(conn, 'scheduler.job', old['job_id'])
                if soft and soft['data'].get('job_revision') == current_job['revision']:
                    cap = min(cap, max(0, int(soft['data']['target'])))
                if control:
                    limits = control['data']
                    all_running = conn.execute(select(tasks, jobs.c.mission.label('admission_mission')).join(
                        jobs, jobs.c.id == tasks.c.job_id).where(tasks.c.state == 'running',
                        tasks.c.lease_expires_at > now)).mappings().all()
                    if len(all_running) >= int(limits.get('global_limit', 64)):
                        return None
                    hint = task_admission(old, mission)
                    if hint['executor'] and limits.get('executor_limit') is not None:
                        occupied = sum(task_admission(t, t['admission_mission'])['executor'] for t in all_running)
                        if occupied >= int(limits['executor_limit']):
                            blocked_tasks.add(old['id']); continue
                    if hint['browser'] and soft and soft['data'].get('runtime_kind') == 'desktop_chrome' and limits.get('native_desktop_limit') is not None:
                        native_running = 0
                        for active in all_running:
                            active_control = self._doc(conn, 'scheduler.job', active['job_id'])
                            if active_control and active_control['data'].get('runtime_kind') == 'desktop_chrome' and task_admission(active, active['admission_mission'])['browser']:
                                native_running += 1
                        if native_running >= int(limits['native_desktop_limit']):
                            blocked_tasks.add(old['id']); continue
                    if hint['origin']:
                        origin_cap = max(1, int((mission.get('parallelism') or {}).get('per_origin', 2)))
                        same_origin = [t for t in all_running if task_admission(t, t['admission_mission'])['origin'] == hint['origin']]
                        if same_origin:
                            origin_cap = min([origin_cap] + [int((t['admission_mission'].get('parallelism') or {}).get('per_origin', 2)) for t in same_origin])
                        if len(same_origin) >= origin_cap:
                            blocked_tasks.add(old['id']); continue
                running = conn.execute(select(func.count()).select_from(tasks).where(
                    tasks.c.job_id == old["job_id"], tasks.c.revision == current_job["revision"],
                    tasks.c.generation == current_job["generation"], tasks.c.state == "running",
                    tasks.c.lease_expires_at > now)).scalar_one()
                if running >= cap or (backend_kinds is not None and backend not in backend_kinds):
                    blocked_jobs.add(old["job_id"])
                    continue
                break
            fence = old["fence"] + 1
            conn.execute(update(tasks).where(tasks.c.id == old["id"]).values(state="running", worker_id=worker_id,
                fence=fence, attempts=old["attempts"] + 1, lease_expires_at=now + timedelta(seconds=lease_seconds),
                retry_at=None, updated_at=now))
            conn.execute(update(attempts_table).where(attempts_table.c.task_id == old["id"], attempts_table.c.state == "running").values(state="abandoned", finished_at=now))
            conn.execute(insert(attempts_table).values(id=str(uuid.uuid4()), task_id=old["id"], worker_id=worker_id,
                fence=fence, revision=old["revision"], state="running", started_at=now))
            self._event(conn, old["job_id"], "task.claimed", {"task_id": old["id"], "worker_id": worker_id, "fence": fence})
            return _out(conn.execute(select(tasks).where(tasks.c.id == old["id"])).first())

    def _workflow_control_valid(self, conn, task, job):
        """Guard workflow writes using both run and individual node controls."""
        if task['kind'] != 'workflow':
            return True
        binding = task['input']
        run = self._doc(conn, 'workflow.run', binding.get('run_id'))
        node = self._doc(conn, 'workflow.node', [binding.get('run_id'), binding.get('node_id')])
        if not run or not node or run['job_id'] != job['id'] or node['job_id'] != job['id']:
            return False
        run, node = run['data'], node['data']
        return (run.get('status') == 'running'
                and run.get('control_epoch') == binding.get('run_epoch')
                and node.get('control_epoch') == binding.get('node_epoch')
                and node.get('task_id') == task['id']
                and node.get('status') in ('queued', 'running', 'retry_wait'))

    def _owned(self, conn, task_id, worker_id, fence, revision=None):
        task = conn.execute(select(tasks).where(tasks.c.id == task_id)).mappings().first()
        if not task:
            raise LeaseLost("unknown task")
        job = self._job(conn, task["job_id"])
        if (task["state"] != "running" or task["worker_id"] != worker_id or task["fence"] != fence
                or not task["lease_expires_at"] or _utc(task["lease_expires_at"]) <= utcnow()
                or task["revision"] != job["revision"] or task["generation"] != job["generation"]
                or (revision is not None and revision != job["revision"])
                or not self._workflow_control_valid(conn, task, job)):
            raise LeaseLost("expired lease, obsolete revision, or mismatched worker/fence")
        return dict(task)

    def validate_task_lease(self, task_id, worker_id, fence, revision=None):
        """Validate current ownership without extending the lease or changing state."""
        with self._tx() as conn:
            return _out(self._owned(conn, task_id, worker_id, fence, revision))

    def heartbeat(self, task_id, worker_id, fence, lease_seconds=60, allowed_job_states=None):
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        with self._tx() as conn:
            task = self._owned(conn, task_id, worker_id, fence)
            if allowed_job_states is not None and self._job(conn, task["job_id"])["state"] not in allowed_job_states:
                raise LeaseLost("Job is paused; the lease cannot be renewed")
            conn.execute(update(tasks).where(tasks.c.id == task_id).values(lease_expires_at=utcnow() + timedelta(seconds=lease_seconds), updated_at=utcnow()))
        return self.get_task(task_id)

    def finish_task(self, task_id, worker_id, fence, result, revision):
        with self._tx() as conn:
            task = self._owned(conn, task_id, worker_id, fence, revision)
            now = utcnow()
            conn.execute(update(tasks).where(tasks.c.id == task_id).values(state="succeeded", result=_json(result), lease_expires_at=None, updated_at=now))
            conn.execute(update(attempts_table).where(attempts_table.c.task_id == task_id, attempts_table.c.fence == fence).values(state="succeeded", result=_json(result), finished_at=now))
            self._event(conn, task["job_id"], "task.succeeded", {"task_id": task_id, "fence": fence})
        return self.get_task(task_id)

    def fail_task(self, task_id, worker_id, fence, error, revision=None, retry_seconds=None, state=None):
        with self._tx() as conn:
            task = self._owned(conn, task_id, worker_id, fence, revision)
            now = utcnow()
            next_state = state or ("retry_wait" if retry_seconds is not None else "failed")
            conn.execute(update(tasks).where(tasks.c.id == task_id).values(state=next_state, error=_json(error),
                lease_expires_at=None, retry_at=now + timedelta(seconds=max(0, retry_seconds)) if retry_seconds is not None else None, updated_at=now))
            conn.execute(update(attempts_table).where(attempts_table.c.task_id == task_id, attempts_table.c.fence == fence).values(state=next_state, error=_json(error), finished_at=now))
            self._event(conn, task["job_id"], "task.failed", {"task_id": task_id, "state": next_state, "error": _json(error)})
        return self.get_task(task_id)

    def reserve_budget(self, scope, name, amount, limit):
        """Atomically consume an ORE budget; this is not provider quota discovery."""
        if amount < 0 or limit < 0:
            raise ValueError("budget values must be nonnegative")
        with self._tx() as conn:
            key = [scope, name]
            old = self._doc(conn, "budget", key)
            value = dict(old["data"]) if old else {"scope": scope, "name": name, "used": 0.0, "limit": float(limit)}
            value["limit"] = min(value["limit"], float(limit))
            allowed = value["used"] + amount <= value["limit"]
            if allowed:
                value["used"] += amount
            self._put(conn, "budget", key, value)
            return {**value, "allowed": allowed, "remaining": max(0, value["limit"] - value["used"])}

    def get_budget(self, scope, name):
        with self._lock, self.engine.connect() as conn:
            row = self._doc(conn, "budget", [scope, name])
            return row["data"] if row else None

    def observe_challenge(self, job_id, origin, auth_context, observation=None):
        from .challenge_policy import observe
        return observe(self, job_id, origin, auth_context, observation)

    def adapt_challenge(self, challenge_id, evidence=None):
        from .challenge_policy import adapt
        return adapt(self, challenge_id, evidence)

    def reserve_challenge(self, job_id, origin, auth_context, max_attempts=3, max_active_seconds=120, active_seconds=0):
        if max_attempts < 1 or max_active_seconds <= 0 or active_seconds < 0:
            raise ValueError("invalid challenge budget")
        identity = canonical_digest([origin, auth_context])
        existing = self.get_challenge(identity)
        if existing and existing.get('clock') == 'elapsed':
            from .challenge_policy import reserve
            return reserve(self, job_id, identity)
        with self._tx() as conn:
            job, now = self._job(conn, job_id), utcnow()
            old = self._doc(conn, "challenge", identity)
            value = dict(old["data"]) if old else None
            if value is None or value["state"] == "resolved":
                value = dict(id=identity, origin=origin, auth_context=auth_context,
                    episode=(value["episode"] + 1) if value else 1, state="detected", attempts=0,
                    active_seconds=0.0, max_attempts=max_attempts, max_active_seconds=float(max_active_seconds),
                    token=None, reserved_at=None, expires_at=None, budget_epoch=1, evidence=None,
                    episode_owner_job_id=job_id, episode_owner_revision=job["revision"], episode_owner_generation=job["generation"])
            from .challenge_policy import join_policy
            join_policy(self, conn, value, job, now)
            owner_job = self._job(conn, value.get('episode_owner_job_id', job_id))
            if value.get('mode') == 'manual':
                value.update(state='awaiting_user', stop_reason='manual_policy')
                self._put(conn, "challenge", identity, value, owner_job)
                return {**value, "allowed": False, "reason": "manual_policy"}
            if value.get("token"):
                if _utc(value["expires_at"]) > now:
                    self._put(conn, "challenge", identity, value, owner_job)
                    return {**value, "allowed": False, "reason": "attempt_in_flight"}
                # A crashed action consumed its reservation; restart never resets it.
                value["active_seconds"] += max(0, (_utc(value["expires_at"]) - _utc(value["reserved_at"])).total_seconds())
                value.update(token=None, reserved_at=None, expires_at=None)
            value["active_seconds"] += active_seconds
            if value["attempts"] >= value["max_attempts"] or value["active_seconds"] >= value["max_active_seconds"]:
                value["state"] = "awaiting_user"
                self._put(conn, "challenge", identity, value, owner_job)
                return {**value, "allowed": False, "reason": "budget_exhausted"}
            value.update(attempts=value["attempts"] + 1, state="attempting", token=str(uuid.uuid4()),
                         reservation_job_id=job_id, reserved_at=now.isoformat(), expires_at=(now + timedelta(seconds=value["max_active_seconds"] - value["active_seconds"])).isoformat())
            self._put(conn, "challenge", identity, value, owner_job)
            self._event(conn, job_id, "challenge.reserved", {"challenge_id": identity, "episode": value["episode"], "attempt": value["attempts"]})
            return {**value, "allowed": True}

    def finish_challenge(self, challenge_id, token, active_seconds=0, resolved=False, evidence=None):
        existing = self.get_challenge(challenge_id)
        if existing and existing.get('clock') == 'elapsed':
            from .challenge_policy import finish
            return finish(self, challenge_id, token, active_seconds, resolved, evidence)
        if active_seconds < 0:
            raise ValueError("active_seconds must be nonnegative")
        if resolved and not evidence:
            raise ValueError("resolution requires observed evidence")
        with self._tx() as conn:
            old = self._doc(conn, "challenge", challenge_id)
            if not old or not token or old["data"].get("token") != token:
                raise LeaseLost("challenge reservation no longer owned")
            value = dict(old["data"])
            elapsed = max(float(active_seconds), max(0, (utcnow() - _utc(value["reserved_at"])).total_seconds()))
            value["active_seconds"] += elapsed
            value.update(token=None, reserved_at=None, expires_at=None, evidence=_json(evidence))
            value["state"] = "resolved" if resolved else (
                "awaiting_user" if value["attempts"] >= value["max_attempts"] or value["active_seconds"] >= value["max_active_seconds"] else "detected")
            job = self._job(conn, old["job_id"])
            self._put(conn, "challenge", challenge_id, value, job)
            self._event(conn, job["id"], "challenge.finished", {"challenge_id": challenge_id, "state": value["state"]})
            return value

    def resolve_challenge(self, challenge_id, evidence):
        """Trusted runtime call after observing target-page recovery, including human recovery."""
        if not evidence:
            raise ValueError("challenge resolution requires observed evidence")
        with self._tx() as conn:
            old = self._doc(conn, "challenge", challenge_id)
            if old is None:
                raise KeyError(challenge_id)
            value = dict(old["data"])
            if value.get("token"):
                raise ControlConflict("finish the reserved attempt before external resolution")
            value.update(state="resolved", evidence=_json(evidence))
            job = self._job(conn, old["job_id"])
            self._put(conn, "challenge", challenge_id, value, job)
            self._event(conn, job["id"], "challenge.resolved", {"challenge_id": challenge_id, "evidence": _json(evidence)})
            return value

    def get_challenge(self, challenge_id):
        with self._lock, self.engine.connect() as conn:
            row = self._doc(conn, "challenge", challenge_id)
            return row["data"] if row else None

    def extend_challenge_budget(self, challenge_id, extra_attempts, extra_seconds, authorized_by):
        existing = self.get_challenge(challenge_id)
        if existing and existing.get('clock') == 'elapsed':
            from .challenge_policy import extend
            return extend(self, challenge_id, extra_attempts, extra_seconds, authorized_by)
        if not authorized_by or extra_attempts < 0 or extra_seconds < 0:
            raise ValueError("an explicit authorizer and nonnegative extension are required")
        with self._tx() as conn:
            old = self._doc(conn, "challenge", challenge_id)
            if old is None:
                raise KeyError(challenge_id)
            value = dict(old["data"])
            if value.get("token"):
                raise ControlConflict("cannot extend an active attempt")
            value.update(max_attempts=value["max_attempts"] + extra_attempts,
                         max_active_seconds=value["max_active_seconds"] + extra_seconds,
                         budget_epoch=value["budget_epoch"] + 1, state="detected")
            job = self._job(conn, old["job_id"])
            self._put(conn, "challenge", challenge_id, value, job)
            self._event(conn, job["id"], "challenge.budget_extended", {"challenge_id": challenge_id,
                "authorized_by": authorized_by, "extra_attempts": extra_attempts, "extra_seconds": extra_seconds})
            return value

    def get_control(self, job_id, session_id):
        with self._lock, self.engine.connect() as conn:
            row = self._doc(conn, "control", [job_id, session_id])
            if not row:
                return None
            value = dict(row["data"])
            value["expired"] = bool(value.get("expires_at") and _utc(value["expires_at"]) <= utcnow())
            return value

    def request_control(self, job_id, session_id, requested_by):
        with self._tx() as conn:
            job = self._job(conn, job_id)
            key = [job_id, session_id]
            old = self._doc(conn, "control", key)
            value = dict(session_id=session_id, owner=None, owner_kind=None, state="handoff_requested",
                         epoch=(old["data"]["epoch"] + 1) if old else 1,
                         requested_by=requested_by, expires_at=None)
            self._put(conn, "control", key, value, job)
            self._event(conn, job_id, "control.requested", value)
            return value

    def acquire_control(self, job_id, session_id, owner, owner_kind="agent", expected_epoch=None, ttl_seconds=300):
        if owner_kind not in ("agent", "user") or ttl_seconds <= 0 or not owner:
            raise ValueError("invalid control owner or TTL")
        with self._tx() as conn:
            job, key = self._job(conn, job_id), [job_id, session_id]
            old = self._doc(conn, "control", key)
            previous = old["data"] if old else None
            if previous:
                if expected_epoch is not None and expected_epoch != previous["epoch"]:
                    raise ControlConflict("stale control epoch")
                same = previous.get("owner") == owner and previous.get("owner_kind") == owner_kind
                if previous.get("owner") and not same:
                    raise ControlConflict("another actor owns this browser; request and drain handoff first")
                if previous.get("requested_by") and previous["requested_by"] != owner:
                    raise ControlConflict("handoff reserved for another actor")
                if not same and expected_epoch is None:
                    raise ControlConflict("control transition requires the observed epoch")
                epoch = previous["epoch"] if same else previous["epoch"] + 1
            else:
                epoch = 1
            value = dict(session_id=session_id, owner=owner, owner_kind=owner_kind,
                         state=f"{owner_kind}_controlled", epoch=epoch, requested_by=None,
                         expires_at=(utcnow() + timedelta(seconds=ttl_seconds)).isoformat())
            self._put(conn, "control", key, value, job)
            self._event(conn, job_id, "control.acquired", value)
            return value

    def validate_control(self, job_id, session_id, owner, epoch):
        value = self.get_control(job_id, session_id)
        if not value or value["owner"] != owner or value["epoch"] != epoch or value["expired"]:
            raise ControlConflict("browser control is expired or held by another actor")
        return value

    def release_control(self, job_id, session_id, owner, epoch):
        with self._tx() as conn:
            job, key = self._job(conn, job_id), [job_id, session_id]
            old = self._doc(conn, "control", key)
            if not old or old["data"].get("owner") != owner or old["data"]["epoch"] != epoch:
                raise ControlConflict("stale browser control lease")
            value = dict(old["data"], owner=None, owner_kind=None, state="paused",
                         requested_by=None, expires_at=None, epoch=epoch + 1)
            self._put(conn, "control", key, value, job)
            self._event(conn, job_id, "control.released", value)
            return value

    @staticmethod
    def _rate_key(key, lane):
        if lane not in ('main', 'browser_resource'):
            raise ValueError('Unknown request pacing lane')
        return key if lane == 'main' else [key, lane]

    def acquire_rate_slot(self, key, interval=3.0, *, lane='main'):
        """Reserve within one lane without resetting legacy main reservations."""
        import math
        if not math.isfinite(interval) or interval < 0:
            raise ValueError('request interval must be finite and nonnegative')
        identity = self._rate_key(key, lane)
        with self._tx() as conn:
            now = utcnow()
            old = self._doc(conn, 'rate', identity)
            value = dict(old['data']) if old else {'key': key, 'lane': lane, 'count': 0, 'interval': float(interval),
                'next_at': None, 'blocked_until': None, 'last_granted_at': None}
            value['interval'] = max(value['interval'], float(interval))
            shared = self._doc(conn, 'rate', key) if lane != 'main' else old
            blocked = shared['data'].get('blocked_until') if shared else None
            earliest = max([now] + [_utc(point) for point in (value.get('next_at'), blocked) if point])
            value.update(next_at=(earliest + timedelta(seconds=value['interval'])).isoformat(), count=value['count'] + 1)
            self._put(conn, 'rate', identity, value)
            return {'slot_at': earliest.isoformat(), 'delay_seconds': max(0, (earliest - now).total_seconds()),
                    'count': value['count'], 'interval': value['interval'], 'lane': lane}

    def confirm_rate_slot(self, key, slot_at, *, lane='main'):
        """Every actual grant rechecks the shared origin cooldown and lane spacing."""
        identity = self._rate_key(key, lane)
        with self._tx() as conn:
            old = self._doc(conn, 'rate', identity)
            if old is None:
                raise KeyError('rate slot was not reserved')
            value, now = dict(old['data']), utcnow()
            candidates = [_utc(slot_at)]
            shared = self._doc(conn, 'rate', key) if lane != 'main' else old
            if shared and shared['data'].get('blocked_until'):
                candidates.append(_utc(shared['data']['blocked_until']))
            if value.get('last_granted_at'):
                candidates.append(_utc(value['last_granted_at']) + timedelta(seconds=value['interval']))
            due = max(candidates)
            delay = max(0, (due - now).total_seconds())
            if delay == 0:
                value['last_granted_at'] = now.isoformat()
                self._put(conn, 'rate', identity, value)
            return {'allowed': delay == 0, 'delay_seconds': delay, 'slot_at': due.isoformat(), 'lane': lane}

    def penalize_rate(self, key, seconds):
        if seconds < 0:
            raise ValueError("rate penalty must be nonnegative")
        with self._tx() as conn:
            now = utcnow()
            old = self._doc(conn, "rate", key)
            value = dict(old["data"]) if old else {"key": key, "count": 0, "interval": 0.0,
                "next_at": None, "blocked_until": None, "last_granted_at": None}
            blocked = max(now + timedelta(seconds=seconds), _utc(value["blocked_until"]) if value.get("blocked_until") else now)
            value["blocked_until"] = blocked.isoformat()
            value["next_at"] = max(blocked, _utc(value["next_at"]) if value.get("next_at") else now).isoformat()
            self._put(conn, "rate", key, value)
            return {"blocked_until": blocked.isoformat(), "delay_seconds": max(0, (blocked - now).total_seconds())}


    def register_worker(self, worker):
        if not worker.get("id"):
            raise ValueError("worker ID is required")
        with self._tx() as conn:
            old = self._doc(conn, "worker", worker["id"])
            value = {**(old["data"] if old else {}), **_json(worker), "last_seen": utcnow().isoformat()}
            self._put(conn, "worker", worker["id"], value)
            return value

    def get_worker(self, worker_id):
        with self._lock, self.engine.connect() as conn:
            old = self._doc(conn, "worker", worker_id)
            return old["data"] if old else None

    def list_workers(self):
        with self._lock, self.engine.connect() as conn:
            rows = conn.execute(select(documents.c.data).where(documents.c.collection == "worker"))
            return [row[0] for row in rows]
