"""Persistent chat organization; branches copy public messages, never execution state."""
from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .public_stream import public_text
from .store import DocumentConflict


class FolderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=160)

    @field_validator("title")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Title must not be blank")
        return value.strip()


class ConversationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=160)
    folder_id: str | None = None

    @field_validator("title")
    @classmethod
    def nonblank(cls, value):
        if value is None or not value.strip():
            raise ValueError("Title must not be blank")
        return value.strip()


class BranchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: str = Field(min_length=1)
    title: str | None = Field(default=None, min_length=1, max_length=160)
    folder_id: str | None = None

    @field_validator("title")
    @classmethod
    def nonblank(cls, value):
        if value is not None and not value.strip():
            raise ValueError("Title must not be blank")
        return value.strip() if value else value


def _now():
    return datetime.now(timezone.utc).isoformat()


class ConversationLibrary:
    """Mixin using the manager's optimistic document writes and public journal."""

    def folder(self, ident):
        doc = self.store.get_document("conversation.folder", ident)
        if doc is None or doc.get("deleted_at"):
            raise KeyError(ident)
        return {key: doc[key] for key in ("id", "title", "created_at", "updated_at")}

    def folders(self):
        rows = [self.folder(doc["id"]) for doc in self.store.list_documents("conversation.folder")
                if not doc.get("deleted_at")]
        return sorted(rows, key=lambda row: (row["title"].casefold(), row["id"]))

    def create_folder(self, values):
        data = FolderInput.model_validate(values).model_dump()
        ident, at = str(uuid.uuid4()), _now()
        self.store.put_document("conversation.folder", ident,
                                {"id": ident, **data, "created_at": at, "updated_at": at}, expected_version=0)
        return self.folder(ident)

    def rename_folder(self, ident, values):
        data = FolderInput.model_validate(values).model_dump()
        for _ in range(5):
            self.folder(ident)
            doc = self.store.get_document("conversation.folder", ident)
            if doc.get("deleted_at"):
                raise KeyError(ident)
            try:
                self.store.put_document("conversation.folder", ident,
                                        {**doc, **data, "updated_at": _now()}, expected_version=doc["state_version"])
                return self.folder(ident)
            except DocumentConflict:
                continue
        raise DocumentConflict("Folder changed; retry this operation")

    def delete_folder(self, ident):
        # Tombstones preserve history and prevent stale clients from reusing a deleted ID.
        for _ in range(5):
            self.folder(ident)
            doc = self.store.get_document("conversation.folder", ident)
            try:
                self.store.put_document("conversation.folder", ident,
                                        {**doc, "deleted_at": _now(), "updated_at": _now()},
                                        expected_version=doc["state_version"])
                break
            except DocumentConflict:
                continue
        else:
            raise DocumentConflict("Folder changed; retry this operation")
        for doc in self.store.list_documents("conversation"):
            if doc["record"].get("folder_id") == ident:
                def unfile(record):
                    if record.get("folder_id") == ident:
                        record["folder_id"] = None
                        self._append_event(record, "conversation.updated", {"folder_id": None})
                self._mutate(doc["record"]["id"], unfile)
        return {"id": ident, "deleted": True}

    def _visible_folder(self, ident):
        if ident:
            try:
                self.folder(ident)
                return ident
            except KeyError:
                pass
        return None

    def update_conversation(self, ident, values):
        data = ConversationUpdate.model_validate(values).model_dump(exclude_unset=True)
        if data.get("folder_id"):
            self.folder(data["folder_id"])
        def update(record):
            record.update(data)
            self._append_event(record, "conversation.updated", data)
        self._mutate(ident, update)
        return self.get(ident)

    def branch(self, ident, values):
        data = BranchInput.model_validate(values).model_dump(exclude_unset=True)
        source, _ = self._read(ident)
        index = next((i for i, row in enumerate(source["messages"]) if row["id"] == data["message_id"]), None)
        if index is None:
            raise KeyError(data["message_id"])
        folder_id = data.get("folder_id", self._visible_folder(source.get("folder_id")))
        if folder_id:
            self.folder(folder_id)
        settings = copy.deepcopy(source["settings"])
        settings.update(title=data.get("title") or (source["title"] + " (branch)")[:160], folder_id=folder_id)
        # Persisted legacy chats keep their explicit gate even when branched.
        settings.setdefault("execution_policy", "explicit")
        created = self.create(settings)
        branch_id = created["id"]
        messages, ids = [], {}
        for old in source["messages"][:index + 1]:
            if old.get("role") not in ("user", "assistant"):
                continue
            message_id = ids[old["id"]] = str(uuid.uuid4())
            status = old.get("status", "complete")
            row = {"id": message_id, "role": old["role"], "content": public_text(old.get("content", "")),
                   "status": "interrupted" if status == "streaming" else status,
                   "sequence": 0, "created_at": old.get("created_at", _now()),
                   "source_message_id": old["id"], "operator_message_id": ids.get(old.get("operator_message_id"))}
            if old.get("kind") in ("plan_summary", "answer"):
                row["kind"] = old["kind"]
            messages.append(row)
        def seed(record):
            record["messages"] = messages
            record["branched_from"] = {"conversation_id": ident, "message_id": data["message_id"]}
            self._append_event(record, "conversation.branched", record["branched_from"])
        self._mutate(branch_id, seed)
        return self.get(branch_id)
