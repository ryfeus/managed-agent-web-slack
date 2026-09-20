from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class SessionSummary(BaseModel):
    """Provider-neutral session metadata exposed through the agent port."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    title: str | None
    status: str
    createdAt: str
    archivedAt: str | None
    environmentId: str
    metadata: dict[str, str]
