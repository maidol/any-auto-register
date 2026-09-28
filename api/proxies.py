from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from application.openvpn_proxies import OpenVPNProxiesService
from application.openvpn_refresh import RefreshBusyError, openvpn_refresh_service
from application.proxies import ProxiesService
from domain.proxies import ProxyBulkCreateCommand, ProxyCreateCommand

router = APIRouter(prefix="/proxies", tags=["proxies"])
service = ProxiesService()
openvpn_service = OpenVPNProxiesService()


class ProxyCreateRequest(BaseModel):
    url: str
    region: str = ""


class ProxyBulkCreateRequest(BaseModel):
    proxies: list[str]
    region: str = ""


class MihomoImportRequest(BaseModel):
    content: str = Field(min_length=1, max_length=2 * 1024 * 1024)
    region: str = ""


@router.get("")
def list_proxies():
    return service.list_proxies()


@router.post("")
def create_proxy(body: ProxyCreateRequest):
    item = service.create_proxy(ProxyCreateCommand(url=body.url, region=body.region))
    if not item:
        raise HTTPException(400, "代理已存在")
    return item


@router.post("/bulk")
def bulk_create_proxies(body: ProxyBulkCreateRequest):
    return service.bulk_create_proxies(ProxyBulkCreateCommand(proxies=body.proxies, region=body.region))


@router.post("/import/mihomo")
def import_mihomo(body: MihomoImportRequest):
    try:
        return openvpn_service.import_mihomo(body.content, body.region)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/openvpn")
def list_openvpn_proxies(region: str = ""):
    return openvpn_service.list_metadata(region)


@router.delete("/openvpn/{proxy_id}")
def delete_openvpn_proxy(proxy_id: int):
    if not openvpn_service.delete(proxy_id):
        raise HTTPException(404, "OpenVPN 代理不存在")
    return {"ok": True}


@router.patch("/openvpn/{proxy_id}/toggle")
def toggle_openvpn_proxy(proxy_id: int):
    value = openvpn_service.toggle(proxy_id)
    if value is None:
        raise HTTPException(404, "OpenVPN 代理不存在")
    return {"is_active": value}


@router.post("/openvpn/refresh")
def refresh_openvpn_proxies():
    try:
        return openvpn_refresh_service.refresh_once()
    except RefreshBusyError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/openvpn/refresh/runs")
def list_openvpn_refresh_runs(limit: int = 30):
    if not 1 <= limit <= 30:
        raise HTTPException(400, "limit must be between 1 and 30")
    return openvpn_refresh_service.list_runs(limit)


@router.delete("/{proxy_id}")
def delete_proxy(proxy_id: int):
    result = service.delete_proxy(proxy_id)
    if not result["ok"]:
        raise HTTPException(404, "代理不存在")
    return result


@router.patch("/{proxy_id}/toggle")
def toggle_proxy(proxy_id: int):
    result = service.toggle_proxy(proxy_id)
    if not result:
        raise HTTPException(404, "代理不存在")
    return result


@router.post("/check")
def check_proxies():
    return service.trigger_check()
