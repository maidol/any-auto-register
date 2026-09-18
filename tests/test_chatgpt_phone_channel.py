"""add-phone 页面的验证渠道（Text / WhatsApp）选择。

背景：OpenAI 的 add-phone 页在 `multi_channel_allowed` 为真时会同时给出
WhatsApp 和 Text 两个渠道，**默认选中 WhatsApp**。租来的是纯 SMS 号码，
走 WhatsApp 收不到码，现象是空等 180 秒并连烧 3 个号。

这些测试全部用假 page，不需要浏览器。
"""
import re

import pytest

from platforms.chatgpt import browser_register as br


# --------------------------------------------------------------------------
# 假 page：只实现被测代码用到的那几个方法
# --------------------------------------------------------------------------

_HAS_TEXT_RE = re.compile(r'^button:has-text\("(.+)"\)$')


class FakeNode:
    """一个可点的 DOM 节点。`attrs` 里放它的全部属性（含选中态）。"""

    def __init__(self, text, attrs=None, tag="BUTTON"):
        self.text = text
        self.attrs = dict(attrs or {})
        self.tag = tag


class FakePage:
    """按 Playwright 的语义模拟 query_selector / click / evaluate。

    - `button:has-text("X")` 是**大小写不敏感的子串**匹配（和 Playwright 一致）；
    - `button[type="submit"]` 匹配任意节点（模拟"页面上总有个提交按钮"）；
    - `evaluate` 按 JS 里的标记注释分派，不依赖调用顺序。
    """

    def __init__(self, nodes, url="https://auth.openai.com/add-phone", cookies=None):
        self.nodes = list(nodes)
        self.url = url
        self.clicked = []          # 被点中的 FakeNode，按顺序
        self.clicked_selectors = []  # 被点中时用的选择器字符串
        self._cookies = list(cookies or [])
        self.context = _FakeContext(self._cookies)

    # -- Playwright 选择器 --------------------------------------------------

    def _match(self, selector):
        m = _HAS_TEXT_RE.match(selector)
        if m:
            needle = m.group(1).lower()
            for node in self.nodes:
                if needle in node.text.lower():
                    return node
            return None
        if selector == 'button[type="submit"]':
            return self.nodes[0] if self.nodes else None
        return None

    def query_selector(self, selector):
        return self._match(selector)

    def query_selector_all(self, selector):
        return [n for n in self.nodes if self._match(selector) is not None]

    def click(self, selector):
        node = self._match(selector)
        if node is None:
            raise RuntimeError(f"no element for {selector}")
        self.clicked.append(node)
        self.clicked_selectors.append(selector)

    def wait_for_timeout(self, ms):
        return None

    # -- page.evaluate ------------------------------------------------------

    def evaluate(self, script, arg=None):
        if "/*CHANNEL_SCAN*/" in script:
            return [
                {
                    "idx": i,
                    "text": node.text,
                    "tag": node.tag,
                    "attrs": dict(node.attrs),
                }
                for i, node in enumerate(self.nodes)
                if _looks_like_channel(node.text)
            ]
        if "/*CHANNEL_CLICK*/" in script:
            idx = arg if isinstance(arg, int) else (arg or {}).get("idx")
            if idx is None or idx >= len(self.nodes):
                return False
            node = self.nodes[idx]
            self.clicked.append(node)
            self._apply_radio_click(node)
            return True
        raise AssertionError(f"FakePage 收到未知脚本: {script[:60]}")

    def _apply_radio_click(self, node):
        """模拟单选组：点中的那个变选中，同组其它的变未选中。"""
        for key in ("aria-checked", "aria-selected", "data-state", ".checked"):
            if key not in node.attrs:
                continue
            on, off = _on_off_values(key)
            for other in self.nodes:
                if key in other.attrs and _looks_like_channel(other.text):
                    other.attrs[key] = on if other is node else off
            return


class _FakeContext:
    def __init__(self, cookies):
        self._cookies = cookies

    def cookies(self):
        return list(self._cookies)


def _on_off_values(key):
    if key == "data-state":
        return "checked", "unchecked"
    return "true", "false"


def _looks_like_channel(text):
    return bool(re.search(r"whatsapp|text message|短信|sms", text or "", re.I))


def _log(_msg):
    return None


# --------------------------------------------------------------------------
# 页面素材
# --------------------------------------------------------------------------

def _page_with_both_radio():
    """用户实测的形状：两个渠道都在，WhatsApp 默认选中（radix 风格 data-state）。"""
    return FakePage([
        FakeNode("WhatsApp", {"role": "radio", "data-state": "checked"}),
        FakeNode("Text message", {"role": "radio", "data-state": "unchecked"}),
        FakeNode("Continue", {"type": "submit"}),
    ])


def _page_with_both_aria():
    """同一件事，选中态写在 aria-checked 上。"""
    return FakePage([
        FakeNode("Send code via WhatsApp", {"aria-checked": "true"}),
        FakeNode("Send code via SMS", {"aria-checked": "false"}),
        FakeNode("Continue", {"type": "submit"}),
    ])


def _page_whatsapp_only():
    return FakePage([
        FakeNode("Send code via WhatsApp", {"aria-checked": "true"}),
        FakeNode("Continue", {"type": "submit"}),
    ])


def _page_text_only():
    """只有短信渠道时仍可继续，不应被误判为渠道缺失。"""
    return FakePage([
        FakeNode("Text message", {"aria-checked": "true"}),
        FakeNode("Continue", {"type": "submit"}),
    ])


def _page_no_channel_choice():
    """multi_channel_allowed 为假时的形状：没有渠道选项。"""
    return FakePage([
        FakeNode("Send code", {"type": "submit"}),
    ])


def _session_cookie(payload):
    import base64
    import json

    raw = base64.urlsafe_b64encode(
        json.dumps(payload).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return [{"name": "oai-client-auth-session", "value": f"{raw}.sig.mac"}]


# ==========================================================================
# 第 1 步：选择器顺序——泛化兜底必须排在所有精确文案之后
# ==========================================================================

GENERIC_SEND_SELECTORS = {
    'button:has-text("Send")',
    'button[type="submit"]',
    'button:has-text("Continue")',
    'button:has-text("continue")',
    'button:has-text("Send code")',
}


def test_phone_send_selectors_put_generic_fallbacks_last():
    """泛化兜底不能排在精确文案前面，否则它会抢先命中 WhatsApp 那个按钮。"""
    selectors = list(br.PHONE_SEND_SELECTORS)
    first_generic = next(
        (i for i, s in enumerate(selectors) if s in GENERIC_SEND_SELECTORS), None
    )
    assert first_generic is not None, "选择器列表里应当仍然保留泛化兜底"
    later_specific = [
        s for s in selectors[first_generic + 1:] if s not in GENERIC_SEND_SELECTORS
    ]
    assert later_specific == [], (
        f"这些精确文案排在了泛化兜底 {selectors[first_generic]!r} 后面，"
        f"永远轮不到它们: {later_specific}"
    )


# ==========================================================================
# 第 2 步：渠道选择函数
# ==========================================================================

def test_selects_text_when_both_channels_present_data_state():
    page = _page_with_both_radio()
    assert br._select_sms_channel_ui(page, _log) == "text"
    text_node = page.nodes[1]
    wa_node = page.nodes[0]
    assert text_node.attrs["data-state"] == "checked"
    assert wa_node.attrs["data-state"] == "unchecked"


def test_selects_text_when_both_channels_present_aria_checked():
    page = _page_with_both_aria()
    assert br._select_sms_channel_ui(page, _log) == "text"
    assert page.nodes[1].attrs["aria-checked"] == "true"
    assert page.nodes[0].attrs["aria-checked"] == "false"


def test_does_not_click_when_text_already_selected():
    page = _page_with_both_radio()
    page.nodes[0].attrs["data-state"] = "unchecked"
    page.nodes[1].attrs["data-state"] = "checked"
    assert br._select_sms_channel_ui(page, _log) == "text"
    assert page.clicked == [], "Text 已经选中时不该再点一次"


def test_raises_when_only_whatsapp_is_offered():
    """只有 WhatsApp 时必须抛，不能假装已选 SMS 继续走。"""
    page = _page_whatsapp_only()
    with pytest.raises(RuntimeError) as exc:
        br._select_sms_channel_ui(page, _log)
    assert "whatsapp" in str(exc.value).lower()


def test_text_only_channel_continues():
    """只有 Text/SMS 时可继续，不把安全的单渠道页面当成异常。"""
    page = _page_text_only()
    assert br._select_sms_channel_ui(page, _log) == "text"
    assert page.clicked == []


def test_returns_none_when_page_has_no_channel_choice():
    """没有渠道选项时保持原流程，既不点也不抛。"""
    page = _page_no_channel_choice()
    assert br._select_sms_channel_ui(page, _log) == "none"
    assert page.clicked == []


def test_raises_when_click_changes_nothing():
    """点了但选中态一点没变——说明点空了，必须报错而不是静默通过。"""
    page = FakePage([
        FakeNode("WhatsApp", {"data-frozen": "yes"}),
        FakeNode("Text message", {"data-frozen": "yes"}),
    ])
    with pytest.raises(RuntimeError) as exc:
        br._select_sms_channel_ui(page, _log)
    assert "未生效" in str(exc.value) or "没有变化" in str(exc.value)


# ==========================================================================
# 第 3 步：服务端复核
# ==========================================================================

def test_session_channel_sms_passes():
    page = FakePage([], cookies=_session_cookie(
        {"phone_verification_channel": "sms", "multi_channel_allowed": False}
    ))
    assert br._verify_sms_channel_from_session(page, _log) == "sms"


def test_session_channel_whatsapp_raises():
    page = FakePage([], cookies=_session_cookie(
        {"phone_verification_channel": "whatsapp"}
    ))
    with pytest.raises(RuntimeError) as exc:
        br._verify_sms_channel_from_session(page, _log)
    assert "whatsapp" in str(exc.value).lower()


def test_session_channel_missing_does_not_block():
    """字段缺失≠渠道不对。OpenAI 改了 cookie 结构时不能把一切都拦下来。"""
    page = FakePage([], cookies=_session_cookie({"email": "a@b.c"}))
    assert br._verify_sms_channel_from_session(page, _log) == ""


def test_session_cookie_absent_does_not_block():
    page = FakePage([], cookies=[])
    assert br._verify_sms_channel_from_session(page, _log) == ""


# ==========================================================================
# 第 4 步：国家选完之后的重算，要等稳定再读
# ==========================================================================

class _LatePage(FakePage):
    """前两次扫描还没渲染出渠道选项，第三次才出现。"""

    def __init__(self):
        super().__init__([
            FakeNode("WhatsApp", {"aria-checked": "true"}),
            FakeNode("Text message", {"aria-checked": "false"}),
        ])
        self.scans = 0

    def evaluate(self, script, arg=None):
        if "/*CHANNEL_SCAN*/" in script:
            self.scans += 1
            if self.scans <= 2:
                return []
        return super().evaluate(script, arg)


def test_waits_for_channel_options_to_settle():
    """重算期间读到的空结果不能被当成"这一页没有渠道选项"。"""
    page = _LatePage()
    assert br._select_sms_channel_ui(page, _log) == "text"
    assert page.nodes[1].attrs["aria-checked"] == "true"


def test_settle_wait_gives_up_and_returns_last_scan():
    """一直不稳定时不能挂死，按最后一次结果继续。"""
    page = _page_with_both_radio()
    rows = br._wait_for_channel_options(page, _log, timeout=0.1)
    assert [r["text"] for r in rows] == ["WhatsApp", "Text message"]


# ==========================================================================
# 第 5 步：接线——函数写好了但没被调用，等于没写
# ==========================================================================

def _source_of(fn):
    import inspect

    return inspect.getsource(fn)


def test_add_phone_selects_channel_before_clicking_send():
    src = _source_of(br._do_add_phone_attempt)
    assert "_select_sms_channel_ui(" in src, "第 4 步之前必须先定渠道"
    assert src.index("_select_sms_channel_ui(") < src.index(
        "_click_first(page, PHONE_SEND_SELECTORS"
    ), "渠道必须在点发送之前定下来"


def test_add_phone_verifies_channel_before_marking_sent():
    src = _source_of(br._do_add_phone_attempt)
    assert "_verify_sms_channel_from_session(" in src
    assert src.index("_verify_sms_channel_from_session(") < src.index(
        "mark_send_succeeded()"
    ), "复核必须在 mark_send_succeeded 之前，否则已经替服务端认领了『已发出』"


def test_dead_selector_string_guard_is_gone():
    """旧守卫检查的是选择器字符串，取值域上恒假；留着它只会让人以为有防线。"""
    src = _source_of(br._do_add_phone_attempt)
    assert 'in str(send_sel).lower()' not in src


def test_whatsapp_only_error_triggers_number_rotation(monkeypatch):
    """只有 WhatsApp 时，真实 UI 错误必须让 add-phone 换号重试。"""
    error_page = _page_whatsapp_only()
    with pytest.raises(RuntimeError) as exc:
        br._select_sms_channel_ui(error_page, _log)
    message = str(exc.value)

    attempts = []

    def _fake_attempt(_page, _phone_callback, **_kwargs):
        attempts.append(1)
        raise RuntimeError(message)

    monkeypatch.setattr(br, "_do_add_phone_attempt", _fake_attempt)
    monkeypatch.setattr(br.time, "sleep", lambda *_a, **_k: None)

    callback = _StubPhoneCallback()
    with pytest.raises(RuntimeError):
        br._handle_add_phone_challenge(
            _NavPage([]), callback,
            device_id="d", user_agent="ua", log=_log, max_phone_attempts=3,
        )

    assert len(attempts) == 3
    assert callback.cleanups == 3 and callback.rearms == 3


# ==========================================================================
# 第 6 步（F1）：扫描器必须分得清「渠道选项」和「文案里带 SMS 的提交按钮」
#
# `Send code via SMS` 不是渠道选项的文案，它是 PHONE_SEND_SELECTORS 的第 1 条，
# 即某一版页面的**提交按钮**。把它当渠道选项点下去＝提前提交，号真的发出去了。
# ==========================================================================

def _page_sms_submit_button_only():
    """没有渠道单选组，提交按钮的文案恰好带 SMS（multi_channel_allowed 为假那一侧）。"""
    return FakePage([
        FakeNode("Send code via SMS", {"type": "submit"}),
    ])


def _page_input_sms_submit_button_only():
    """input 提交控件会被扫描器标记为 checked=false，也不能成为渠道候选。"""
    return FakePage([
        FakeNode("Send code via SMS", {"type": "submit", ".checked": "false"}, tag="INPUT"),
    ])


def test_input_submit_control_is_not_a_channel_option():
    page = _page_input_sms_submit_button_only()
    assert br._select_sms_channel_ui(page, _log) == "none"
    assert page.clicked == []


def _page_submit_button_before_radio_group():
    """有单选组，但带 SMS 文案的提交按钮在 DOM 里排在它前面。"""
    return FakePage([
        FakeNode("Send code via SMS", {"type": "submit"}),
        FakeNode("WhatsApp", {"role": "radio", "data-state": "checked"}),
        FakeNode("Text message", {"role": "radio", "data-state": "unchecked"}),
    ])


class _StatelessTogglePage(FakePage):
    """页面不用任何标准选中态属性，点击后只有 class 变——兜底路径就是为它准备的。"""

    def _apply_radio_click(self, node):
        for other in self.nodes:
            other.attrs["class"] = "opt selected" if other is node else "opt"


def _page_stateless_two_options():
    return _StatelessTogglePage([
        FakeNode("WhatsApp", {"class": "opt selected"}),
        FakeNode("Text message", {"class": "opt"}),
    ])


def test_sms_labelled_submit_button_is_not_a_channel_option():
    """只有一个候选时那不是「选择」，必须落回 none 走原流程，一次都不能点。"""
    page = _page_sms_submit_button_only()
    assert br._select_sms_channel_ui(page, _log) == "none"
    assert page.clicked == [], "提交按钮不是渠道选项，点它等于提前把号提交出去"


def test_picks_the_radio_not_the_sms_labelled_submit_button():
    """有带选中态的行时，只在这些行里挑——提交按钮没有选中态。"""
    page = _page_submit_button_before_radio_group()
    assert br._select_sms_channel_ui(page, _log) == "text"
    assert [n.text for n in page.clicked] == ["Text message"], (
        f"点错了节点: {[n.text for n in page.clicked]}"
    )
    assert page.nodes[2].attrs["data-state"] == "checked"
    assert page.nodes[1].attrs["data-state"] == "unchecked"


def test_stateless_fallback_still_selects_with_two_candidates():
    """**改前改后都必须绿**：新判据不许把「没有标准选中态」的兜底路径关掉。"""
    page = _page_stateless_two_options()
    assert br._select_sms_channel_ui(page, _log) == "text"
    assert [n.text for n in page.clicked] == ["Text message"]


# ==========================================================================
# 第 7 步（F2）：服务端认定 whatsapp 时，要换号重试，不是整轮结束
#
# 这条测试**不复制错误文案**：先让真的 _verify_sms_channel_from_session 抛，
# 拿它的原句去喂 _handle_add_phone_challenge。文案改了测试跟着走。
# ==========================================================================

class _NavPage(FakePage):
    """换号重试会 page.goto 回 add-phone。"""

    def goto(self, *args, **kwargs):
        return None


class _StubPhoneCallback:
    def __init__(self):
        self.cleanups = 0
        self.rearms = 0

    def __call__(self):
        return "+8613800000000"

    def cleanup(self):
        self.cleanups += 1

    def rearm(self):
        self.rearms += 1


def test_server_side_whatsapp_verdict_triggers_number_rotation(monkeypatch):
    page = FakePage([], cookies=_session_cookie(
        {"phone_verification_channel": "whatsapp"}
    ))
    with pytest.raises(RuntimeError) as exc:
        br._verify_sms_channel_from_session(page, _log)
    message = str(exc.value)

    attempts = []

    def _fake_attempt(_page, _phone_callback, **_kwargs):
        attempts.append(1)
        raise RuntimeError(message)

    monkeypatch.setattr(br, "_do_add_phone_attempt", _fake_attempt)
    monkeypatch.setattr(br.time, "sleep", lambda *_a, **_k: None)

    callback = _StubPhoneCallback()
    with pytest.raises(RuntimeError):
        br._handle_add_phone_challenge(
            _NavPage([]), callback,
            device_id="d", user_agent="ua", log=_log, max_phone_attempts=3,
        )

    assert len(attempts) == 3, (
        f"服务端认定 whatsapp 必须换号重试，实际只尝试了 {len(attempts)} 次就整轮结束"
    )
    assert callback.cleanups == 3 and callback.rearms == 3
