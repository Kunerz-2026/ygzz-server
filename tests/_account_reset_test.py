# -*- coding: utf-8 -*-
"""_account_reset_test.py —— 账号/密码注册 + 自助重置码 + 旧票吊销（单元层）

钉死 2026-11-07 新增契约：
  1. `register(..., reset_code=...)` 只存哈希，不存明文；
  2. `reset_with_code(account, code, new_password)` 成功后旧密码失效、新密码可登录；
  3. 重置码**轮换**（旧码再用失败），失败 5 次锁 10 分钟；
  4. 没有重置码的账号（老游客号）走运维 CLI，不在本套件里自动放行。
"""
import io
import os
import shutil
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ygzz_auth as YA  # noqa: E402

FAIL = []


def ck(tag, cond, extra=''):
    print('  %-4s %-64s %s' % ('OK' if cond else 'FAIL', tag, extra))
    if not cond:
        FAIL.append(tag)


def main():
    root = tempfile.mkdtemp(prefix='dsh-acct-reset-')
    try:
        store = YA.AuthStore(root)
        rec = store.register('alice', 'alice-pass-1', 'u0001', reset_code='123456')
        ck('注册成功', rec.get('aid') == 'u0001', rec)
        ck('只存重置码哈希，不存明文', 'reset_hash' in rec and rec.get('reset_hash') != '123456'
           and '123456' not in str(rec), '')
        ck('错误码 -> 拒绝并计数',
           _expect_fail(lambda: store.reset_with_code('alice', '000000', 'new-pass-1'), YA.HTTP_BAD_PASSWORD),
           '')
        rec2, new_code = store.reset_with_code('alice', '123456', 'new-pass-2')
        ck('正确码 -> 重置成功且返回新码', bool(new_code) and new_code != '123456', new_code)
        ck('旧密码失效', _expect_fail(lambda: store.login('alice', 'alice-pass-1'), YA.HTTP_BAD_PASSWORD), '')
        ck('新密码可登录', store.login('alice', 'new-pass-2').get('account', 'alice') == 'alice', '')
        ck('旧重置码已失效（轮换）',
           _expect_fail(lambda: store.reset_with_code('alice', '123456', 'x-pass-3'), YA.HTTP_BAD_PASSWORD), '')
        # 锁定：连续 5 次错误码
        for _ in range(YA.RESET_LOCK_AFTER):
            try:
                store.reset_with_code('alice', '999999', 'x-pass-4')
            except YA.AuthError:
                pass
        ck('连续错误 5 次 -> 锁定（HTTP_LOCKED）',
           _expect_fail(lambda: store.reset_with_code('alice', new_code, 'x-pass-5'), YA.HTTP_LOCKED), '')
        print('\n%s' % ('ACCOUNT RESET ALL PASS' if not FAIL else 'ACCOUNT RESET FAILED: %s' % FAIL))
        return 0 if not FAIL else 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _expect_fail(fn, code):
    try:
        fn()
        return False
    except YA.AuthError as e:
        return e.http_code == code


if __name__ == '__main__':
    raise SystemExit(main())
