from pathlib import Path


MODAL_SOURCE = Path(__file__).parents[1].joinpath("frontend/src/pages/Accounts.tsx").read_text(encoding="utf-8")
MODAL_SOURCE = MODAL_SOURCE[
    MODAL_SOURCE.index("function RegisterModal") : MODAL_SOURCE.index("export default function Accounts")
]


def test_register_modal_exposes_openvpn_proxy_mode_and_submits_it():
    assert "const [proxyMode, setProxyMode] = useState('pool')" in MODAL_SOURCE
    assert "代理来源" in MODAL_SOURCE
    assert "VPN Gate OpenVPN" in MODAL_SOURCE
    assert "proxy_mode: proxyMode" in MODAL_SOURCE
