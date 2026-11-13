# -*- coding: utf-8 -*-
r"""_auth_admin.py —— 账号生命周期运维工具（认领放行 / 重置口令 / 解锁 / 查看）。

## 为什么需要它（2026-10-25 独立审查 M3 + H2）
审查确认：账号只有"注册、登录、首次认领"，**没有改密、没有找回、没有运维介入手段**。
两个后果：
  · 玩家忘了口令 = 永久进不去（只能手工改 `_auth.json`，那没有任何校验与留痕）；
  · H2 的"先到先得认领"被修掉后（存量档默认 `claimable: False`），**必须有**一个
    运维放行入口，否则那三个老档的玩家真的进不来。

## 为什么是本地 CLI 而不是 HTTP 接口
服务端进程把口令库读进内存（`AuthStore._data`）并会整体写回 —— 在服务端运行时改文件，
下一次 `_save()` 就把改动覆盖掉。所以本工具**默认拒绝在服务端运行时执行**（同
`_acct_backup.py` 的口径），要强来必须 `--force` 并自己承担"被内存覆盖"的风险。
也正因为它不是网络接口，**没有**"谁能调它"的鉴权问题：能跑它的人本来就能读整个目录。

## 用法
```
python -X utf8 _auth_admin.py list
python -X utf8 _auth_admin.py claim   <账号>      # 放行一次自助认领（存量档）
python -X utf8 _auth_admin.py unclaim <账号>
python -X utf8 _auth_admin.py reset   <账号> --password <新口令>
python -X utf8 _auth_admin.py unlock  <账号>
```
★ 数据根默认 `work\accounts`（与备份工具同一个默认值）；隔离部署用
  `--data-root %TEMP%\dsh-ygzz-deploy\work\accounts`。
★ 重置口令后必须**重启服务端**：会话表在内存里，重启即让全部旧票据失效
  （这正是"泄漏后止损"要的效果）。
"""
import argparse
import io
import os
import shutil
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ROOT = os.path.join(HERE, 'accounts')


def _root(args):
    return os.path.abspath(args.data_root or os.environ.get('YGZZ_DATA_ROOT') or DEFAULT_ROOT)


def _playing(root):
    """服务端是否在跑：优先看**数据根上级**的 server.pid 对应的端口监听。

    与 `_acct_backup._playing` 同一套保守判据（pid 文件 + 端口 + LISTENING 三合一），
    不靠"进程名里有 python"这种会误伤的判据。
    """
    if os.name != 'nt':
        return None
    pidf = os.path.join(os.path.dirname(os.path.normpath(root)), 'server.pid')
    try:
        pid = int(open(pidf, encoding='ascii').read().strip())
    except Exception:
        return None
    try:
        import subprocess
        r = subprocess.run(['netstat', '-ano', '-p', 'tcp'], capture_output=True,
                           text=True, errors='replace', timeout=8)
        for line in r.stdout.splitlines():
            cols = line.split()
            if (len(cols) >= 5 and cols[3].upper() == 'LISTENING' and cols[4] == str(pid)
                    and cols[1].rsplit(':', 1)[-1] in ('18080', '27001', '27002')):
                return pid
    except Exception:
        pass
    return None


def _store(root):
    sys.path.insert(0, HERE)
    import ygzz_auth as YA
    return YA, YA.AuthStore(root)


def main():
    ap = argparse.ArgumentParser(description='账号生命周期运维（认领放行/重置口令/解锁）')
    ap.add_argument('cmd', choices=('list', 'claim', 'unclaim', 'reset', 'unlock', 'reset-data'))
    ap.add_argument('account', nargs='?', default='')
    ap.add_argument('--password', default='')
    ap.add_argument('--data-root', default='')
    ap.add_argument('--yes', action='store_true',
                    help='reset-data 必须显式确认（会清空该账号的角色/存档）')
    ap.add_argument('--force', action='store_true',
                    help='服务端在跑时也执行（会被内存里的旧库覆盖，慎用）')
    a = ap.parse_args()
    root = _root(a)
    YA, store = _store(root)
    print('数据根: %s' % root)
    if not os.path.isdir(root):
        print('★ 目录不存在 —— 是不是还没启用多账号？用 --data-root 指到真实数据根。')
        return 2
    pid = _playing(root)
    if pid and not a.force:
        print('★ 服务端 PID %d 正在监听 —— 拒绝在运行中改口令库（内存里的库会覆盖回去）。' % pid)
        print('  正解：先停服（需人工确认），再执行本命令；或 --force 自担风险。')
        return 2
    if pid:
        print('⚠ --force：服务端在跑，改动可能被内存覆盖；执行完请立刻重启服务端。')

    if a.cmd == 'list':
        recs = store.accounts()
        print('共 %d 个账号（* = 已有口令；! = 可自助认领；无标记 = 存量未放行）'
              % len(recs))
        for name in sorted(recs):
            r = recs[name]
            mark = '*' if r.get('hash') else ('!' if r.get('claimable') else ' ')
            locked = '  LOCKED' if float(r.get('locked_until') or 0) > YA.now() else ''
            print('  %s %-24s aid=%-6s legacy=%s%s'
                  % (mark, name, r.get('aid'), bool(r.get('legacy')), locked))
        return 0

    account = (a.account or '').strip()
    if not account:
        print('★ 需要账号名')
        return 2
    try:
        if a.cmd == 'claim':
            rec = store.set_claimable(account, True)
            print('已放行自助认领: %s（玩家下次用该账号名+新口令登录即完成认领）'
                  % account)
        elif a.cmd == 'unclaim':
            rec = store.set_claimable(account, False)
            print('已收回自助认领: %s' % account)
        elif a.cmd == 'reset':
            if len(a.password) < YA.PASSWORD_MIN:
                print('★ 新口令至少 %d 个字符' % YA.PASSWORD_MIN)
                return 2
            rec = store.reset_password(account, a.password)
            print('已重置口令: %s（*重启服务端*后旧票据全部失效）' % account)
        elif a.cmd == 'reset-data':
            if not a.yes:
                print('★ reset-data 会清空该账号的角色/存档；确认请加 --yes')
                return 2
            rec = store.get(account)
            if not rec:
                print('★ 未找到账号: %s' % account)
                return 2
            aid = str(rec.get('aid') or '')
            if not aid:
                print('★ 该账号没有 aid，无法定位存档')
                return 2
            fp = os.path.join(root, aid + '.json')
            if not os.path.isfile(fp) and aid.isdigit():
                fp = os.path.join(root, ('u%04d' % int(aid)) + '.json')
            if not os.path.isfile(fp):
                print('★ 没找到存档文件（可能还没首次进游戏）: %s' % fp)
                return 2
            bak = os.path.join(root, '_backups',
                               'resetdata-%s' % time.strftime('%Y%m%d-%H%M%S'))
            os.makedirs(bak, exist_ok=True)
            shutil.copy2(fp, os.path.join(bak, os.path.basename(fp)))
            os.remove(fp)
            print('已清空角色存档: %s（备份在 %s）' % (fp, bak))
            print('  账号/口令保留；下次登录会按模板重新建角色。')
            return 0
        else:
            rec = store.unlock(account)
            print('已解锁: %s' % account)
        print('  记录: aid=%s legacy=%s claimable=%s has_hash=%s'
              % (rec.get('aid'), bool(rec.get('legacy')), bool(rec.get('claimable')),
                 bool(rec.get('hash'))))
        return 0
    except YA.AuthError as e:
        print('★ %s（码 %s）' % (e.msg, e.http_code))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
