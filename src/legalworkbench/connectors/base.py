"""Connector interfaces for enterprise legal systems."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


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


class EnterpriseConnector(Protocol):
    name: str

    def tools(self) -> list[ConnectorTool]:
        """Return tools exposed by this connector."""

    def resources(self) -> list[ConnectorResource]:
        """Return resources exposed by this connector."""
