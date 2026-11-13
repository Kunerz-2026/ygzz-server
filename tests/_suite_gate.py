# -*- coding: utf-8 -*-
r"""_suite_gate.py —— 回归套件"假绿"判据（★2026-10-26 审查 B2 的修法）。

## 为什么需要它
`the regression runner` 与 `_isolated_regression.py` 过去只看**子进程退出码**。
而 Python 里 `try: ... finally: sys.exit(0)` 会把**任意异常**吞成成功，连
traceback 都不会打印 —— `_auth_login_test.py` 的 `rA2` 未赋值就是这样把
A5c–A11 整段吞掉的（2026-10-25 独立审查 B2）。

## 两层判据
1. **运行器层（主判据）**：`_suite_runner.py` 在进程内守 `sys.exit`，
   发现"退出码 0 + 正在异常清理"就改以 97 退出。这能抓住**不打印 traceback**
   的吞异常形态（正是 B2 那种）。
2. **日志层（副判据）**：即便套件自己捕获了异常、退出码 0，只要日志里出现
   **未捕获的 Python traceback**，仍判红。个别套件故意演示异常时，在
   traceback 前后 4 行内写 `EXPECT-TRACEBACK` 豁免。

**不要**把"日志里有 FAIL 字样"当判据：大量负例用例会打印预期失败文本，
那会把闸口变成狼来了。

## 接口
`classify(text, code)`、`verify_runner()`（用真的会吞异常的子进程做负例控制）、
`runner_command(suite_path)`。`the regression runner` 全部走这里。
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile

GATE_VERSION = 2
TRACEBACK = 'Traceback (most recent call last)'
EXEMPT = 'EXPECT-TRACEBACK'
FAKE_GREEN = 97
HERE = os.path.dirname(os.path.abspath(__file__))
RUNNER = os.path.join(HERE, '_suite_runner.py')


def runner_command(suite_path):
    """给 `the regression runner` 用的命令行：与直接执行套件等价，但过了守卫。"""
    return [sys.executable, '-X', 'utf8', RUNNER, suite_path]


def classify(text, code):
    """把"退出码 + 完整日志"折成红/绿。纯函数，便于自检。"""
    try:
        code_n = int(code)
    except (TypeError, ValueError):
        return {'red': True, 'fake_green': False, 'reason': '退出码不是整数: %r' % (code,)}
    if code_n == FAKE_GREEN:
        return {'red': True, 'fake_green': True,
                'reason': '★假绿：退出码 0 时正处于异常清理路径（原始异常被 finally 吞掉）'}
    if code_n != 0:
        return {'red': True, 'fake_green': False, 'reason': '退出码 %d' % code_n}
    lines = (text or '').splitlines()
    for i, ln in enumerate(lines):
        if TRACEBACK not in ln:
            continue
        window = '\n'.join(lines[max(0, i - 4):i + 5])
        if EXEMPT in window:
            continue
        nxt = ''
        for j in range(i + 1, min(len(lines), i + 6)):
            if lines[j].strip():
                nxt = lines[j].strip()
                break
        return {'red': True, 'fake_green': False,
                'reason': '退出码 0 但出现未捕获 traceback: %s' % nxt[:120]}
    return {'red': False, 'fake_green': False, 'reason': ''}


def verify_runner():
    """负例控制：**真的跑**一个"异常被 finally 吞掉、退出码 0"的套件。

    返回 `(ok, details)`。证明的不是字符串匹配，而是"闸口在退出码为 0 时判红"。
    """
    swallow = (
        'import sys\n'
        'print("A5b 完成")\n'
        'try:\n'
        '    raise NameError("rA2 在赋值前被使用")\n'
        'finally:\n'
        '    print("结果: ALL PASS")\n'
        '    sys.exit(0)\n'
    )
    clean = 'import sys\nprint("结果: ALL PASS")\nsys.exit(0)\n'
    d = tempfile.mkdtemp(prefix='dsh-gate-selftest-')
    try:
        outs = {}
        for name, src in (('swallow', swallow), ('clean', clean)):
            p = os.path.join(d, '_%s_suite.py' % name)
            with io.open(p, 'w', encoding='utf-8') as f:
                f.write(src)
            r = subprocess.run(runner_command(p), cwd=d, capture_output=True,
                               text=True, errors='replace', timeout=60)
            outs[name] = ((r.stdout or '') + (r.stderr or ''), r.returncode)
        _st, _sc = outs['swallow']
        _ct, _cc = outs['clean']
        v_swallow = classify(_st, _sc)
        v_clean = classify(_ct, _cc)
        direct = subprocess.run([sys.executable, '-X', 'utf8',
                                 os.path.join(d, '_swallow_suite.py')],
                                cwd=d, capture_output=True, text=True,
                                errors='replace', timeout=60)
        return (v_swallow['fake_green'] and v_swallow['red']
                and not v_clean['red']
                and direct.returncode == 0), {
            'guard_exit': outs['swallow'][1], 'guard': v_swallow,
            'clean': v_clean, 'direct_exit': direct.returncode,
            'direct_stdout_tail': (direct.stdout or '').strip().splitlines()[-1:]}
    finally:
        shutil.rmtree(d, ignore_errors=True)
