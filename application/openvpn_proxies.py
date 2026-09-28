from __future__ import annotations

from core.openvpn_config import parse_mihomo_openvpn_yaml
from domain.openvpn_proxies import OpenVPNProxyRecord
from infrastructure.openvpn_proxies_repository import OpenVPNProxiesRepository


class OpenVPNProxiesService:
    def __init__(self, repository: OpenVPNProxiesRepository | None = None):
        self.repository = repository or OpenVPNProxiesRepository()

    def import_mihomo(self, content: str, region: str = "") -> dict[str, object]:
        parsed = parse_mihomo_openvpn_yaml(content)
        summary = self.repository.import_entries(parsed.entries, region=region)
        summary.rejected = len(parsed.failures)
        summary.failures = list(parsed.failures)
        return summary.as_dict()

    def list_metadata(self, region: str = "") -> list[dict[str, object]]:
        return [self._serialize(record) for record in self.repository.list_metadata(region)]

    def delete(self, proxy_id: int) -> bool:
        return self.repository.delete(proxy_id)

    def toggle(self, proxy_id: int) -> bool | None:
        return self.repository.toggle(proxy_id)

    @staticmethod
    def _serialize(record: OpenVPNProxyRecord) -> dict[str, object]:
        return {
            "id": record.id,
            "name": record.name,
            "server": record.server,
            "port": record.port,
            "proto": record.proto,
            "region": record.region,
            "success_count": record.success_count,
            "fail_count": record.fail_count,
            "is_active": record.is_active,
            "last_checked": record.last_checked,
            "source": record.source,
            "refresh_healthy": record.refresh_healthy,
            "last_seen_at": record.last_seen_at,
            "last_refresh_checked_at": record.last_refresh_checked_at,
        }
