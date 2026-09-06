from __future__ import annotations

from typing import Protocol

from ossdigest.models import Candidate


class Source(Protocol):
    name: str

    async def fetch(self) -> list[Candidate]: ...
