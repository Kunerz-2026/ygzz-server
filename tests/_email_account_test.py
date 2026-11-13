# -*- coding: utf-8 -*-
r"""_email_account_test.py —— 邮箱作为账号的注册/登录口径（红先行）。

背景：用户 2026-10-27 决定测试服用**邮箱注册**。现有 `ygzz_auth._ACCOUNT_RE`
不允许 `@`，注册接口会在 `check_account_name` 就拒掉邮箱；本套件先钉住：
  E1 邮箱能过账号校验（改前应红）
  E2 邮箱长度上限（254，改前 32）
  E3 `:` 仍必须被拒（accounts 索引键防注入）
  E4 Registry 真注册 + 真登录（PBKDF2 路径）
  E5 普通用户名 `test1` 仍可用（不破坏存量）

⚠ 本套件只覆盖服务端；客户端注册框 `LoginHomeSdk.lua` 仍是 32 字符上限，
   手机包重打时要同步抬到 254，否则“服务端收、客户端存不下”。
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ygzz_auth as A  # noqa: E402

OK = []
BAD = []


def ck(name, cond, detail=''):
    (OK if cond else BAD).append(name)
    print('  %s %s%s' % ('[ok]' if cond else '[FAIL]', name,
                         ('  -- ' + str(detail)) if detail else ''))


def main():
    email = 'player@example.com'
    ok, why = A.check_account_name(email)
    ck('E1 邮箱能过账号校验', ok, why)

    long_email = ('a' * 240) + '@example.com'  # 252
    ok, why = A.check_account_name(long_email)
    ck('E2a 长邮箱(252)可过', ok, why)
    too_long = ('a' * 250) + '@example.com'  # 262 > 254
    ok, _why = A.check_account_name(too_long)
    ck('E2b 超长邮箱(262)被拒', not ok)

    ok, _why = A.check_account_name('user:pass@example.com')
    ck('E3 含冒号仍被拒', not ok)

    ok, why = A.check_account_name('test1')
    ck('E5 普通用户名仍可过', ok, why)

    root = tempfile.mkdtemp(prefix='ygzz_email_')
    try:
        store = A.AuthStore(root)
        rec = store.register(email, 'secret1', 'u0001')
        ck('E4a 邮箱注册成功', rec.get('aid') == 'u0001')
        got = store.login(email, 'secret1')
        ck('E4b 邮箱登录成功', got.get('aid') == 'u0001')
        try:
            store.register(email, 'secret2', 'u0002')
            ck('E4c 重复注册被拒', False)
        except A.AuthError:
            ck('E4c 重复注册被拒', True)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print('=== EMAIL ACCOUNT %s（%d 项失败）===' %
          ('ALL PASS' if not BAD else 'FAIL', len(BAD)))
    return 0 if not BAD else 1


if __name__ == '__main__':
    sys.exit(main())
