# -*- coding: utf-8 -*-
r"""_suite_gate_test.py —— 回归闸口自身的判红自检（★2026-10-25 审查 B2 的验收件）。

## 它证明什么
1. **负例控制（真的跑子进程）**：一个 `try: raise ... finally: sys.exit(0)` 的套件，
   直接运行退出码是 **0**、日志是 `ALL PASS`；经过 `_suite_runner.py` 后退出码必须是
   **97**，且 `classify()` 判"★假绿"。这一条不成立 = 闸口对 B2 形态无效。
2. 干净套件（退出码 0、无 traceback）不被误判。
3. 退出码非 0、日志里出现未捕获 traceback（但退出码 0）都判红。
4. `EXPECT-TRACEBACK` 豁免生效（给"故意演示异常"的套件留出口，而不是让它们被判红后
   再去关掉闸口）。
5. `runner_command()` 指向的 `_suite_runner.py` 真实存在，且用的是当前解释器。

## 为什么这条套件必须自己也会红
本套件只打印 `[ok]/[FAIL]` 并以 `sys.exit(1 if fails else 0)` 结束；**没有任何
`finally: sys.exit(...)`** —— 它自己就是"套件应该怎么写"的样板（审查 B2 的另一半）。
"""
import os
import sys

import _suite_gate as G

fails = []


def ck(name, cond, detail=''):
    if not isinstance(detail, str):
        detail = repr(detail)
    print('  %s %s%s' % ('[ok]' if cond else '[FAIL]', name, ('  -- ' + detail) if detail else ''))
    if not cond:
        fails.append('%s %s' % (name, detail))


print('== G0. 运行器存在性 ==')
ck('G0a runner_command 指向 _suite_runner.py',
   os.path.isfile(os.path.join(os.path.dirname(os.path.abspath(G.RUNNER)), '_suite_runner.py')),
   G.RUNNER)
ck('G0b 用的是当前解释器（隔离回归的护栏只允许 sys.executable）',
   G.runner_command('x.py')[0] == sys.executable, G.runner_command('x.py')[0])

print('\n== G1. 负例控制：异常被 finally 吞掉必须判红 ==')
ok, det = G.verify_runner()
ck('G1a 直接运行该套件退出码确实是 0（负例成立，不是假想）', det.get('direct_exit') == 0,
   det.get('direct_exit'))
ck('G1b 直接运行的日志确实是 ALL PASS（与 B2 实测形态一致）',
   det.get('direct_stdout_tail') == ['结果: ALL PASS'], det.get('direct_stdout_tail'))
ck('G1c 经过 runner 后退出码 == 97（假绿专用码）', det.get('guard_exit') == G.FAKE_GREEN,
   det.get('guard_exit'))
ck('G1d classify 判 red 且 fake_green', bool((det.get('guard') or {}).get('red'))
   and bool((det.get('guard') or {}).get('fake_green')), det.get('guard'))

print('\n== G2. classify 单元判据 ==')
ck('G2a 退出码 0 + 干净日志 -> 绿', not G.classify('结果: ALL PASS\n', 0)['red'])
ck('G2b 退出码 1 -> 红', G.classify('anything\n', 1)['red'])
ck('G2c 退出码 97 -> 红 + fake_green',
   G.classify('', 97)['red'] and G.classify('', 97)['fake_green'])
_tb = 'ok\n%s\nNameError: boom\nALL PASS\n' % G.TRACEBACK
ck('G2d 退出码 0 但有未捕获 traceback -> 红', G.classify(_tb, 0)['red'])
ck('G2e EXPECT-TRACEBACK 豁免生效',
   not G.classify('%s\n%s\nNameError: expected\n' % (G.EXEMPT, G.TRACEBACK), 0)['red'])
ck('G2f 非整数退出码 -> 红', G.classify('', 'TIMEOUT')['red'])

print('\n结果: %s' % ('ALL PASS' if not fails else 'FAIL %d' % len(fails)))
for f in fails:
    print('  FAIL: %s' % f)
sys.exit(1 if fails else 0)
