from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from typing import Optional


def openvpn_proxy_identity(name: str, server: str, port: int) -> str:
    """Return a stable key for normalized name/server/integer-port identity.

    Names and servers are stripped and case-folded; the compact JSON array
    keeps the tuple unambiguous while remaining deterministic across runtimes.
    """
    return json.dumps(
        [str(name).strip().casefold(), str(server).strip().casefold(), int(port)],
        ensure_ascii=False,
        separators=(",", ":"),
    )


@dataclass(slots=True)
class OpenVPNProxyRecord:
    id: int
    name: str
    server: str
    port: int
    proto: str
    region: str = ""
    success_count: int = 0
    fail_count: int = 0
    is_active: bool = True
    last_checked: Optional[datetime] = None


@dataclass(slots=True)
class OpenVPNProxyRuntimeRecord(OpenVPNProxyRecord):
    config: dict[str, object] | None = None


@dataclass(slots=True)
class OpenVPNImportSummary:
    total: int = 0
    added: int = 0
    updated: int = 0
    existing: int = 0
    rejected: int = 0
    failures: list[dict[str, str]] | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "added": self.added,
            "updated": self.updated,
            "existing": self.existing,
            "rejected": self.rejected,
            "failures": list(self.failures or []),
        }
