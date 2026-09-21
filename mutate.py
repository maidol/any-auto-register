#!/usr/bin/env python3
"""反向对照：把修复逐处改错，看哪几条守卫会红。

一条变异必须做到两件事，缺一条这份报告就是假的：
  1. 替换**确实发生**（原文匹配到，且只匹配到预期的处数）——否则是「没跑」不是「没红」；
  2. pytest **确实执行**（返回码 0/1 之外一律报 BROKEN）。
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent
PY = ROOT / ".venv311" / "bin" / "python"
TESTS = ["tests/test_cycle_identity_and_cleanup.py", "tests/test_pinned_mailbox.py"]

TASKS = ROOT / "application" / "tasks.py"
MAILBOX = ROOT / "core" / "base_mailbox.py"

# (名字, 文件, 原文, 替换成, 预期匹配处数, 这处修错在现实里长什么样)
MUTATIONS = [
    (
        "M1 地板跟着缓存",
        MAILBOX,
        """    def get_current_ids(self, account: MailboxAccount) -> set:
        return self.wrapped.get_current_ids(account)

    def wait_for_code(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None,
                      code_pattern: str = None) -> str:
        return self.wrapped.wait_for_code(""",
        """    def get_current_ids(self, account: MailboxAccount) -> set:
        if not hasattr(self, "_ids_cache"):
            self._ids_cache = self.wrapped.get_current_ids(account)
        return self._ids_cache

    def wait_for_code(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None,
                      code_pattern: str = None) -> str:
        return self.wrapped.wait_for_code(""",
        1,
        "「两个都是按邮箱来的，一起缓存更一致」—— 过期验证码被读走",
    ),
    (
        "M2 每次尝试都开新周期",
        TASKS,
        "        if attempt == 0:\n            _open_cycle()\n",
        "        _open_cycle()\n",
        1,
        "忘了周期边界，退回「每次尝试一个新邮箱」",
    ),
    (
        "M3 最后一轮不清理",
        TASKS,
        """    finally:
        # 最后一轮也要清。「下一轮开始前再清」会把它漏掉，而那正是任务
        # 结束后仍然留着一个浏览器进程的那一份。
        _close_cycle()""",
        "    finally:\n        pass",
        1,
        "只在下一轮开始前清 —— 任务结束后留着一个浏览器进程",
    ),
    (
        "M4 忽略 clean_browser_context",
        TASKS,
        "        if shared_mailbox is None or not strategy.clean_browser_context:",
        "        if shared_mailbox is None:",
        1,
        "把逃生开关写死成 True",
    ),
    (
        "M5 fallback 关一半",
        MAILBOX,
        """        for _key, mailbox in self.providers:
            try:
                mailbox.close()
            except Exception as exc:
                logger.warning("邮箱 provider %s 关闭失败: %s", _key, exc)""",
        """        for _key, mailbox in self.providers:
            mailbox.close()""",
        1,
        "第一个 provider 抛异常，后面几个静默留着",
    ),
    (
        "M6 清理异常冒到任务上",
        TASKS,
        """        try:
            close()
        except Exception as exc:
            logger.log(f"邮箱清理失败（不影响本轮结果）: {exc}", level="warning")""",
        "        close()",
        1,
        "裸调 close()，清理失败把整个任务弄成 failed",
    ),
    (
        "M7 close 不清句柄",
        MAILBOX,
        "        self._executor = None\n        self._browser = None\n        self._page = None\n\n    def __del__(self):",
        "\n    def __del__(self):",
        1,
        "关了但句柄还在 —— 下一轮 _ensure_browser() 直接复用上一轮那个页面",
    ),
    (
        # 注意：只加 `if cycle["mailbox"] is None` 是**无效变异** ——
        # _close_cycle() 已经把它置空了，那个条件恒真，行为零变化。
        # 真要做出「钉到任务级」，两处必须一起改，所以这条带两个编辑。
        "M8 钉到任务级",
        TASKS,
        [
            (
                '        cycle["open"] = False\n        cycle["mailbox"] = None\n',
                '        cycle["open"] = False\n',
            ),
            (
                '        cycle["mailbox"] = PinnedMailbox(shared_mailbox) if shared_mailbox is not None else None\n        cycle["open"] = True',
                '        if cycle["mailbox"] is None:\n            cycle["mailbox"] = PinnedMailbox(shared_mailbox) if shared_mailbox is not None else None\n        cycle["open"] = True',
            ),
        ],
        None,
        None,
        "钉过头：整个任务共用一个邮箱",
    ),
]


def run_tests() -> tuple[int, list[str]]:
    proc = subprocess.run(
        [str(PY), "-m", "pytest", *TESTS, "-q", "-p", "no:cacheprovider", "--no-header"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"pytest 没有正常结束: rc={proc.returncode}\n{proc.stdout[-2000:]}")
    failed = [
        line.split("::", 1)[1].split()[0]
        for line in proc.stdout.splitlines()
        if line.startswith("FAILED ") and "::" in line
    ]
    errors = [line for line in proc.stdout.splitlines() if line.startswith("ERROR ")]
    return len(failed) + len(errors), failed + errors


def main() -> int:
    if not PY.exists():
        print(f"BROKEN: 解释器不存在 {PY}")
        return 2

    print("=== 基线（全部修复就位）===")
    n, names = run_tests()
    if n != 0:
        print(f"BROKEN: 基线就不是全绿，{n} 条红: {names}")
        return 2
    print("基线 0 红 ✓\n")

    broken = 0
    for name, path, old, new, expected_hits, why in MUTATIONS:
        edits = old if isinstance(old, list) else [(old, new)]
        original = path.read_text(encoding="utf-8")
        bad = [(o, original.count(o)) for o, _ in edits if original.count(o) != 1]
        if bad:
            print(f"BROKEN {name}: {len(bad)} 处原文没有恰好匹配一次 —— 这条没跑")
            for snippet, hits in bad:
                print(f"    匹配到 {hits} 次: {snippet.strip()[:70]!r}")
            broken += 1
            continue
        backup = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, backup)
        try:
            mutated = original
            for o, n in edits:
                mutated = mutated.replace(o, n, 1)
            if mutated == original:
                raise RuntimeError(f"{name}: 替换后文件没有变化 —— 无效变异")
            path.write_text(mutated, encoding="utf-8")
            count, names = run_tests()
            mark = "红 ✓" if count else "**没红** ✗"
            if not count:
                broken += 1
            print(f"{name}: {count} 条{mark}  （{why}）")
            for n2 in names:
                print(f"    - {n2}")
        finally:
            shutil.move(str(backup), str(path))
        print()

    print(f"=== {len(MUTATIONS)} 处变异，{len(MUTATIONS) - broken} 处被捕获，{broken} 处漏网/没跑 ===")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
