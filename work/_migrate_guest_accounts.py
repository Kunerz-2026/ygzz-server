# -*- coding: utf-8 -*-
r"""_migrate_guest_accounts.py —— 把“游客自动建号”的旧账号改成“可认领”。

背景（2026-11-07）：
  · 旧发布版用 `YGZZ_GUEST_LOGIN=1`：客户端只带账号名，服务端用
    `guest-<crc32(name)%1e8>` 当口令自动建号。玩家**没有自己设过密码**。
  · 本轮起发布默认 `guest_login=0`，客户端改为账号+密码注册/登录。
  · 旧游客号如果直接改成“必须有密码”，原主人会进不去；本脚本把**确认是游客号**的
    记录标成 `legacy + claimable`：第一次用“账号+任意新密码”登录时由 `claim()` 认领，
    原 aid/角色/存档不变。

安全前提（脚本会逐条核对，不是猜）：
  · 只处理 `hash` 确实等于 `guest-<crc32(账号名)>` 的记录；
  · 不碰已有真口令的账号、不碰 `claimable` 已经为 True 的记录；
  · `--apply` 前备份 `_auth.json`；检测到服务端在跑则拒绝（除非 `--force`）。

用法：
    python -X utf8 -B _migrate_guest_accounts.py --data-root "...\server\work\accounts"
    python -X utf8 -B _migrate_guest_accounts.py --data-root "..." --apply
"""
import argparse
import io
import json
import os
import shutil
import sys
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ygzz_auth as YA  # noqa: E402


def _guest_pwd(name):
    return 'guest-%08d' % (zlib.crc32(name.encode('utf-8')) % 100000000)


def _server_running(acct_root):
    pidf = os.path.join(os.path.dirname(os.path.normpath(acct_root)), 'server.pid')
    try:
        pid = int(io.open(pidf, encoding='ascii').read().strip())
        os.kill(pid, 0)
        return pid
    except Exception:
        return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--data-root', default=os.path.join(HERE, 'accounts'))
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--force', action='store_true')
    a = ap.parse_args(argv)
    root = os.path.abspath(a.data_root)
    if not os.path.isdir(root):
        print('★ 数据根不存在: %s' % root)
        return 2
    pid = _server_running(root)
    if pid and not a.force:
        print('★ 服务端还在运行（PID %s）；先停服，或确认后用 --force。' % pid)
        return 2
    store = YA.AuthStore(root)
    recs = store.accounts()
    targets = []
    for name in sorted(recs):
        rec = recs[name]
        if rec.get('legacy') and not rec.get('hash'):
            continue                          # 已经是待认领，跳过
        if rec.get('claimable'):
            continue
        pwd = _guest_pwd(name)
        if YA.verify_password(pwd, rec.get('salt'), rec.get('hash'),
                              rec.get('iters') or YA._PBKDF2_ITERS):
            targets.append(name)
    print('数据根: %s' % root)
    print('账号总数: %d；确认是游客号: %d' % (len(recs), len(targets)))
    for name in targets:
        print('  - %s (aid=%s)' % (name, (recs[name] or {}).get('aid')))
    if not targets:
        print('没有需要迁移的游客号。')
        return 0
    if not a.apply:
        print('\ndry-run：未写文件。加 --apply 才落盘。')
        return 0
    bak = os.path.join(root, '_backups', 'guest-migrate-%s' % time.strftime('%Y%m%d-%H%M%S'))
    os.makedirs(bak, exist_ok=True)
    shutil.copy2(store._path, os.path.join(bak, '_auth.json'))
    for name in targets:
        try:
            store.mark_claimable_guest(name, why='guest-migrate')
        except Exception as e:
            print('  !! %s 迁移失败: %r' % (name, e))
    print('\n完成；`_auth.json` 备份在 %s' % bak)
    print('玩家下次用“账号 + 新密码”登录即完成认领（原角色/存档不变）。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
