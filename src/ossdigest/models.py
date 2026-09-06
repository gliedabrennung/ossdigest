from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, HttpUrl


class Candidate(BaseModel):
    host: Literal["github", "gitlab"]
    full_name: str
    remote_id: str
    node_id: str | None = None
    url: HttpUrl
    description: str | None = None
    homepage: str | None = None
    language: str | None = None
    languages: dict[str, int] | None = None
    license_spdx: str | None = None
    stars: int = 0
    forks: int = 0
    open_issues: int = 0
    topics: list[str] = []
    created_at: datetime
    pushed_at: datetime
    is_archived: bool = False
    is_fork: bool = False
    is_template: bool = False
    parent_stars: int | None = None
    owner_type: Literal["User", "Organization"] = "User"
    source: str
    source_meta: dict[str, Any] = {}
