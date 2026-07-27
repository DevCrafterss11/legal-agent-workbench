"""Connector interfaces for enterprise legal systems."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ConnectorTool:
    server: str
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConnectorResource:
    server: str
    name: str
    uri: str
    description: str = ""
