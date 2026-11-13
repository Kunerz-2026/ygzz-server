# -*- coding: utf-8 -*-
r"""_suite_runner.py —— 回归套件运行器：**在进程内**跑套件并守住"假绿"。

## 它解决的具体缺陷（2026-10-25 独立审查 B2）
Python 里 `try: ... finally: sys.exit(0)` 会**静默吞掉** try 体里的任意异常 ——
连 traceback 都不会打印。`_auth_login_test.py` 就是这么把 A5c–A11 整段吞掉的：
日志停在 A5b，随后打印 `结果: ALL PASS`，退出码 0 ⇒ 只看退出码的闸口全绿。

## 做法
把套件用 `runpy.run_path(..., run_name='__main__')` 在当前进程里执行，并把
`sys.exit` 换成守卫版：
* **退出码 0 且此刻正在处理异常**（`sys.exc_info()` 非空，即处于 `except`/`finally`
  的异常清理路径）⇒ 判定为"把异常吞成成功"：打印原始 traceback，改以 **97** 退出。
* 其余情况按原退出码退出。
* 套件自然抛出的未捕获异常 ⇒ traceback + 退出码 1（与直接运行一致）。

97 是本闸口专用码（`_suite_gate.FAKE_GREEN`），`the regression runner` 会把它
翻成"★假绿"而不是普通失败。

## 不做的事
不修改套件自身、不吞任何异常、不改变 stdout/stderr（套件看到的是同一个
`__main__`、同一个 cwd、同一个 `sys.argv` 形状）。
"""
import os
import runpy
import sys
import traceback

FAKE_GREEN = 97
_real_exit = sys.exit


def main(argv):
    if len(argv) < 2:
        sys.stderr.write('用法: python _suite_runner.py <套件.py> [套件参数…]\n')
        return 2
    target = argv[1]
    if not os.path.isfile(target):
        sys.stderr.write('套件不存在: %s\n' % target)
        return 2
    # 与直接执行一致：argv[0] 是套件路径，其余是套件自己的参数。
    sys.argv = [target] + list(argv[2:])

    def guarded_exit(code=0):
        code = 0 if code is None else code
        exc = sys.exc_info()[1]
        if code == 0 and exc is not None:
            sys.stderr.write(
                '\n★★FAKE-GREEN: sys.exit(0) 在异常清理路径里执行 —— 原始异常会被吞掉。\n'
                '   这是 2026-10-25 审查 B2 的形态；拒绝把它算作通过。\n')
            traceback.print_exception(*sys.exc_info())
            raise SystemExit(FAKE_GREEN)
        raise SystemExit(code)

    sys.exit = guarded_exit
    try:
        runpy.run_path(target, run_name='__main__')
    except SystemExit:
        raise
    except BaseException:
        traceback.print_exc()
        return 1
    return 0


if __name__ == '__main__':
    _real_exit(main(sys.argv))
