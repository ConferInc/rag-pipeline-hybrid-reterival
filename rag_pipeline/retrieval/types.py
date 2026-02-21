from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal


SourceType = Literal["semantic", "structural", "hybrid"]


@dataclass(frozen=True)
class RetrievalResult:
    node_id: str
    label: str
    score_raw: float
    source: SourceType
    index_name: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

