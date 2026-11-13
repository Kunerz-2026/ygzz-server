# -*- coding: utf-8 -*-
r"""_acct_backup_test.py —— 多人单服存档备份/恢复的**隔离**验收套件（T1–T7）。

覆盖（每条都能红）：
  T1  --data-root 生效；默认仍是 work\accounts（只读常量，不碰真数据）
  T2  snapshot 的 MANIFEST 对**快照副本**算 sha256；players/consistent/data_root/auth_enabled
  T3  篡改快照 -> verify 非 0 且指名文件；restore 拒绝且数据根逐字节不变
  T4  正常 restore 往返；pre-restore 有 MANIFEST 且 verify 通过；用它能再次 restore
  T5  在 restore 第 1 个 replace 后注入失败 -> 非 0、自动回滚、journal status=failed
  T5b 手工把 journal 改成 started（模拟硬崩）-> restore 拒绝、doctor 报警、--auto-rollback 可用
  T6  服务端在跑时 snapshot 拒绝（同级 server.pid / --assume-running）；--live 强制；
      restore 默认拒绝、--force 放行并打印危险提示
  T7  prune 不删 *-pre-restore；普通快照受 KEEP=20 限制；显式 prune 至少留 3 份 pre-restore

隔离：所有数据根都在系统 TEMP 下的本次专用目录里，**绝不**碰真实 `work\accounts`，
也**不起服务端、不占 18080/27001/27002**（测试环境设 `YGZZ_BACKUP_NO_PORTSCAN=1`）。

测试钩子（工具文档里也写明只用于测试）：`YGZZ_BACKUP_FAIL_AFTER=<n>`、`--assume-running`。

用法: python -X utf8 -B _acct_backup_test.py
通过时打印 `结果: ALL PASS` 并以 0 退出；失败打印 `FAIL n` 并以 1 退出。
**不**用 try/finally 把异常吞成 exit 0：未捕获异常自然冒泡成非 0。
"""
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

WORK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'work')
SCRIPT = os.path.join(WORK, '_acct_backup.py')
PY = sys.executable
BASE = os.path.join(tempfile.gettempdir(),
                    'dsh-acct-backup-test-%d-%d' % (os.getpid(), int(time.time())))
fails = []


def ck(name, cond, detail=''):
    if not isinstance(detail, str):
        detail = repr(detail)
    print('  %s %s%s' % ('[ok]' if cond else '[FAIL]', name,
                        ('  -- ' + detail) if detail else ''))
    if not cond:
        fails.append('%s %s' % (name, detail))


def norm(p):
    return os.path.normcase(os.path.abspath(p or ''))


def sha(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for blk in iter(lambda: f.read(1 << 20), b''):
            h.update(blk)
    return h.hexdigest()


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write('\n')


def read_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def _env(extra=None):
    env = os.environ.copy()
    for k in ('YGZZ_DATA_ROOT', 'YGZZ_BACKUP_FAIL_AFTER', 'YGZZ_BACKUP_ASSUME_RUNNING'):
        env.pop(k, None)
    env.update({
        'YGZZ_BACKUP_NO_PORTSCAN': '1',
        'YGZZ_AUTH': '1',
        'PYTHONIOENCODING': 'utf-8',
        'PYTHONDONTWRITEBYTECODE': '1',
    })
    if extra:
        env.update(extra)
    return env


def run_cli(args, extra_env=None):
    return subprocess.run([PY, '-X', 'utf8', '-B', SCRIPT] + list(args),
                          cwd=WORK, env=_env(extra_env), capture_output=True,
                          text=True, encoding='utf-8', errors='replace', timeout=120)


def run_code(code, *args):
    return subprocess.run([PY, '-X', 'utf8', '-B', '-c', code] + list(args),
                          cwd=WORK, env=_env(), capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=60)


def out(r):
    return (r.stdout or '') + (r.stderr or '')


def tail(r, n=800):
    return out(r).strip()[-n:]


def parse_banner(r):
    m = re.search(r'^数据根: (.*?)  \(文件 \d+ 个, 玩家档 \d+ 份\)$', out(r), re.M)
    return m.group(1).strip() if m else None


def parse_snap(r):
    m = re.search(r'^快照完成: (.+)$', out(r), re.M)
    return m.group(1).strip() if m else None


def make_fixture(base):
    root = os.path.join(base, 'accounts')
    os.makedirs(root, exist_ok=True)
    write_json(os.path.join(root, '_accounts.json'), {
        'peer:devA': 'u0001',
        'peer:devB': 'u0002',
        'user:alice': 'u0001',
    })
    write_json(os.path.join(root, 'u0001.json'),
               {'role_name': '甲', 'gold': 12345, 'items': {'1001': 1}})
    write_json(os.path.join(root, 'u0002.json'),
               {'role_name': '乙', 'gold': 6789, 'items': {}})
    write_json(os.path.join(root, '_world.json'),
               {'guilds': {}, 'chat': []})
    write_json(os.path.join(root, '_auth.json'),
               {'version': 1, 'users': {'alice': {'aid': 'u0001', 'salt': 's', 'hash': 'h'}}})
    write_json(os.path.join(root, '_guild_txn.json'), {})
    return root


def hash_map(root):
    """数据文件逐文件哈希（与工具 collect() 同口径，排除 snapshot/journal/staging/pid）。"""
    result = {}
    if not os.path.isdir(root):
        return result
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs
                   if d != '_backups' and not d.startswith('.restore-staging-')]
        for fn in files:
            if (fn == '.restore-journal.json' or fn.startswith('.restore-journal.json.')
                    or fn == 'server.pid' or fn.endswith(('.tmp', '.corrupt'))
                    or '.corrupt-' in fn):
                continue
            fp = os.path.join(dirpath, fn)
            rel = os.path.relpath(fp, root).replace('\\', '/')
            result[rel] = sha(fp)
    return result


def snapdir(root):
    return os.path.join(root, '_backups')


def all_snap_dirs(root):
    d = snapdir(root)
    if not os.path.isdir(d):
        return []
    return sorted(n for n in os.listdir(d)
                  if not n.startswith('.') and os.path.isdir(os.path.join(d, n)))


def pre_dirs(root):
    d = snapdir(root)
    if not os.path.isdir(d):
        return []
    return sorted(n for n in os.listdir(d)
                  if re.search(r'-pre-restore(?:-\d+)?$', n)
                  and os.path.isdir(os.path.join(d, n)))


def normal_dirs(root):
    d = snapdir(root)
    if not os.path.isdir(d):
        return []
    return sorted(n for n in os.listdir(d)
                  if not n.startswith('.') and not re.search(r'-pre-restore(?:-\d+)?$', n)
                  and os.path.isdir(os.path.join(d, n))
                  and os.path.isfile(os.path.join(d, n, 'MANIFEST.json')))


def newest_pre(root):
    names = pre_dirs(root)
    if not names:
        return None
    names.sort(key=lambda n: (os.path.getmtime(os.path.join(snapdir(root), n)), n))
    return os.path.join(snapdir(root), names[-1])


def read_manifest(snap_path):
    return read_json(os.path.join(snap_path, 'MANIFEST.json'))


def snapshot_hashes(snap_path, man):
    """直接对**快照目录里的副本**算哈希（不信任 MANIFEST），与恢复后的数据根比较。"""
    hashes = {}
    for rel in (man.get('files') or {}):
        fp = os.path.join(snap_path, rel.replace('/', os.sep))
        if os.path.isfile(fp):
            hashes[rel] = sha(fp)
    return hashes


def sname(path):
    """snapshot 输出是绝对路径；CLI 恢复/校验统一传目录名（工具也支持绝对路径，双保险）。"""
    return os.path.basename(os.path.normpath(path))


def mk_snap(root, note):
    r = run_cli(['snapshot', '--data-root', root, '--note', note])
    p = parse_snap(r)
    ck('快照 %r 成功' % note, r.returncode == 0 and bool(p), tail(r))
    return p


def fail_detail(before, after, limit=6):
    keys = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    return '不一致: %s' % keys[:limit]


# ---------------------------------------------------------------- T1
def t1():
    print('\n== T1. --data-root 生效；默认仍是 work\\accounts ==')
    root = make_fixture(os.path.join(BASE, 't1'))
    r = run_cli(['doctor', '--data-root', root])
    ck('T1a 合格数据根 doctor 返回 0', r.returncode == 0, tail(r))
    ck('T1b doctor 打印解析后的绝对路径 == 传入路径',
       norm(parse_banner(r)) == norm(root), (parse_banner(r), root))
    r = run_cli(['--data-root', root, 'doctor'])
    ck('T1c 全局位置的 --data-root 同样生效',
       r.returncode == 0 and norm(parse_banner(r)) == norm(root), tail(r))
    r = run_cli(['doctor'], extra_env={'YGZZ_DATA_ROOT': root})
    ck('T1d 环境变量 YGZZ_DATA_ROOT 生效',
       r.returncode == 0 and norm(parse_banner(r)) == norm(root), tail(r))
    # 默认值只读模块常量，不读真实 work\accounts
    code = ('import sys;sys.path.insert(0,sys.argv[1]);import _acct_backup as B;'
            'print(B.default_data_root());print(B.resolve_data_root(None))')
    r = run_code(code, WORK)
    lines = [x.strip() for x in out(r).splitlines() if x.strip()]
    expect = os.path.join(WORK, 'accounts')
    ck('T1e 默认数据根仍是 work\\accounts',
       len(lines) >= 1 and norm(lines[0]) == norm(expect), lines[:2])
    ck('T1f resolve_data_root(None) 与默认一致',
       len(lines) >= 2 and norm(lines[1]) == norm(expect), lines[:2])


# ---------------------------------------------------------------- T2
def t2():
    print('\n== T2. snapshot MANIFEST 对快照副本算 sha256 ==')
    root = make_fixture(os.path.join(BASE, 't2'))
    before = hash_map(root)
    snap = mk_snap(root, 't2')
    if not snap:
        return
    m = read_manifest(snap)
    files = m.get('files') or {}
    ck('T2a version == 2', m.get('version') == 2, m.get('version'))
    ck('T2b players 计数 == 2', m.get('players') == 2, m.get('players'))
    ck('T2c 非 --live 时 consistent is True', m.get('consistent') is True,
       m.get('consistent'))
    ck('T2d data_root 是解析后的绝对路径', norm(m.get('data_root')) == norm(root),
       m.get('data_root'))
    ck('T2e 口令库在，auth_enabled is True', m.get('auth_enabled') is True,
       m.get('auth_enabled'))
    ck('T2f 文件数 6（含 _guild_txn.json）',
       len(files) == 6 and '_guild_txn.json' in files, sorted(files))
    bad = []
    for rel, info in files.items():
        fp = os.path.join(snap, rel.replace('/', os.sep))
        if (not os.path.isfile(fp) or sha(fp) != info.get('sha256')
                or os.path.getsize(fp) != info.get('size')):
            bad.append(rel)
    ck('T2g MANIFEST 的 sha256/size 与快照副本一致', not bad, bad)
    ck('T2h 快照未改动源数据根', hash_map(root) == before,
       fail_detail(before, hash_map(root)))
    ck('T2i 非 --live 快照不含 --live 警示',
       not any('--live' in w for w in (m.get('warnings') or [])),
       m.get('warnings'))


# ---------------------------------------------------------------- T3
def t3():
    print('\n== T3. 篡改快照 -> verify/restore 必须拒绝且不动现场 ==')
    root = make_fixture(os.path.join(BASE, 't3'))
    snap = mk_snap(root, 't3')
    if not snap:
        return
    r = run_cli(['verify', '--data-root', root, os.path.basename(snap)])
    ck('T3a 完好快照 verify 返回 0', r.returncode == 0, tail(r))
    tamper_name = os.path.basename(snap) + '-tampered'
    tamper = os.path.join(snapdir(root), tamper_name)
    shutil.copytree(snap, tamper)
    with open(os.path.join(tamper, 'u0001.json'), 'ab') as f:
        f.write(b'\nTAMPER\n')
    before = hash_map(root)
    r = run_cli(['verify', '--data-root', root, tamper_name])
    ck('T3b 篡改后 verify 返回非 0', r.returncode != 0, tail(r))
    ck('T3c verify 指名 u0001.json', 'u0001.json' in out(r), tail(r))
    r = run_cli(['restore', '--data-root', root, tamper_name])
    ck('T3d 篡改快照 restore 拒绝（非 0）', r.returncode != 0, tail(r))
    ck('T3e 拒绝后数据根逐文件哈希不变', hash_map(root) == before,
       fail_detail(before, hash_map(root)))
    ck('T3f 拒绝时不产生 pre-restore', pre_dirs(root) == [], pre_dirs(root))
    ck('T3g 拒绝时不写恢复日志',
       not os.path.exists(os.path.join(root, '.restore-journal.json')))


# ---------------------------------------------------------------- T4
def t4():
    print('\n== T4. 正常 restore 往返 + pre-restore 可再次 restore ==')
    root = make_fixture(os.path.join(BASE, 't4'))
    snap = mk_snap(root, 't4-a')
    if not snap:
        return
    m = read_manifest(snap)
    snap_hashes = snapshot_hashes(snap, m)
    # 改成另一份内容：改档、加档、删口令库、改世界/索引
    write_json(os.path.join(root, 'u0001.json'),
               {'role_name': '甲-改', 'gold': 99999, 'items': {'2002': 3}})
    write_json(os.path.join(root, 'u0003.json'), {'role_name': '丙', 'gold': 1})
    os.remove(os.path.join(root, '_auth.json'))
    write_json(os.path.join(root, '_world.json'),
               {'guilds': {'g1': {'name': '新会'}}, 'chat': ['x']})
    write_json(os.path.join(root, '_accounts.json'),
               {'peer:devA': 'u0001', 'peer:devB': 'u0002', 'peer:devC': 'u0003'})
    mutated = hash_map(root)
    r = run_cli(['restore', '--data-root', root, sname(snap)])
    ck('T4a restore 成功', r.returncode == 0, tail(r))
    cur = hash_map(root)
    ck('T4b 恢复后与快照逐字节一致', cur == snap_hashes, fail_detail(snap_hashes, cur))
    ck('T4c 快照外的 u0003.json 被删除', 'u0003.json' not in cur, sorted(cur))
    ck('T4d 输出含恢复前后 sha256 对照',
       '恢复前后' in out(r) and bool(re.search(r'[0-9a-f]{64}', out(r))), tail(r))
    ck('T4e 输出汇总含索引/口令库哈希',
       '_accounts.json' in out(r) and '_auth.json' in out(r), tail(r))
    names = pre_dirs(root)
    ck('T4f 产生 1 份 pre-restore', len(names) == 1, names)
    pre = newest_pre(root)
    if not pre:
        ck('T4g pre-restore 目录存在', False, '')
        return
    pre_manifest_path = os.path.join(pre, 'MANIFEST.json')
    ck('T4g pre-restore 有 MANIFEST', os.path.isfile(pre_manifest_path))
    if not os.path.isfile(pre_manifest_path):
        return
    pm = read_manifest(pre)
    pre_hashes = snapshot_hashes(pre, pm)
    ck('T4h pre-restore MANIFEST version 2', pm.get('version') == 2, pm.get('version'))
    ck('T4i pre-restore 内容 == 恢复前现场', pre_hashes == mutated,
       fail_detail(mutated, pre_hashes))
    rv = run_cli(['verify', '--data-root', root, os.path.basename(pre)])
    ck('T4j pre-restore verify 通过', rv.returncode == 0, tail(rv))
    r2 = run_cli(['restore', '--data-root', root, os.path.basename(pre)])
    ck('T4k 用 pre-restore 再 restore 成功', r2.returncode == 0, tail(r2))
    ck('T4l 回到恢复前内容（逐文件哈希一致）', hash_map(root) == mutated,
       fail_detail(mutated, hash_map(root)))


# ---------------------------------------------------------------- T5 / T5b
def t5():
    print('\n== T5. restore 中途失败 -> 自动回滚 + journal failed ==')
    root = make_fixture(os.path.join(BASE, 't5'))
    snap = mk_snap(root, 't5-base')
    if not snap:
        return
    # 改成另一份现场（作为“恢复前”基准）
    write_json(os.path.join(root, 'u0001.json'), {'role_name': 'C甲', 'gold': 1})
    write_json(os.path.join(root, 'u0002.json'), {'role_name': 'C乙', 'gold': 2})
    write_json(os.path.join(root, 'u0004.json'), {'role_name': 'C丁', 'gold': 4})
    os.remove(os.path.join(root, '_world.json'))
    write_json(os.path.join(root, '_accounts.json'),
               {'peer:devA': 'u0001', 'peer:devB': 'u0002', 'peer:devD': 'u0004'})
    c_hashes = hash_map(root)
    r = run_cli(['restore', '--data-root', root, sname(snap)],
                extra_env={'YGZZ_BACKUP_FAIL_AFTER': '1'})
    ck('T5a 注入失败 -> 返回非 0', r.returncode != 0, tail(r))
    ck('T5b 输出说明已自动回滚', '回滚' in out(r), tail(r))
    ck('T5c 数据根回到恢复前（逐文件哈希一致）', hash_map(root) == c_hashes,
       fail_detail(c_hashes, hash_map(root)))
    rv = run_cli(['verify', '--data-root', root, os.path.basename(snap)])
    ck('T5d 被恢复的快照本身未被破坏', rv.returncode == 0, tail(rv))
    jp = os.path.join(root, '.restore-journal.json')
    ck('T5e 写入了恢复日志', os.path.isfile(jp), jp)
    if not os.path.isfile(jp):
        return
    j = read_json(jp)
    ck('T5f journal status=failed', j.get('status') == 'failed', j.get('status'))
    ck('T5g journal rolled_back is True', j.get('rolled_back') is True,
       j.get('rolled_back'))
    ck('T5h journal 记录快照与 pre-restore',
       j.get('snapshot') == os.path.basename(snap)
       and j.get('pre_restore') in pre_dirs(root),
       (j.get('snapshot'), j.get('pre_restore')))
    names = pre_dirs(root)
    ck('T5i 保留了 pre-restore 目录', bool(names), names)
    if names:
        pre = newest_pre(root)
        pm = read_manifest(pre)
        pre_hashes = snapshot_hashes(pre, pm)
        ck('T5j pre-restore 内容 == 恢复前现场', pre_hashes == c_hashes,
           fail_detail(c_hashes, pre_hashes))
    leftover = sorted(n for n in os.listdir(root) if n.startswith('.restore-staging-'))
    ck('T5k 暂存目录已清理', leftover == [], leftover)

    # T5b：模拟硬崩（journal 停在 started）
    print('\n== T5b. 上次恢复未完成（journal started）必须提示并可回滚 ==')
    pre_name = j.get('pre_restore')
    j2 = dict(j)
    j2.update({'status': 'started', 'ended': None, 'error': None,
               'rolled_back': None, 'rollback_error': None})
    write_json(jp, j2)
    before = hash_map(root)
    r = run_cli(['restore', '--data-root', root, sname(snap)])
    ck('T5b1 journal started 时 restore 拒绝', r.returncode != 0, tail(r))
    ck('T5b2 提示 --rollback 与 pre-restore 名',
       '--rollback' in out(r) and (pre_name or '') in out(r), tail(r))
    ck('T5b3 拒绝后数据根不变', hash_map(root) == before,
       fail_detail(before, hash_map(root)))
    r = run_cli(['doctor', '--data-root', root])
    ck('T5b4 doctor 报未完成恢复 -> 非 0', r.returncode != 0, tail(r))
    ck('T5b5 doctor 指名 pre-restore', (pre_name or '') in out(r), tail(r))
    r = run_cli(['restore', '--data-root', root, '--auto-rollback'])
    ck('T5b6 --auto-rollback 成功', r.returncode == 0, tail(r))
    ck('T5b7 自动回滚后内容 == pre-restore（恢复前现场）', hash_map(root) == c_hashes,
       fail_detail(c_hashes, hash_map(root)))
    j3 = read_json(jp)
    ck('T5b8 journal status=finished', j3.get('status') == 'finished',
       j3.get('status'))


# ---------------------------------------------------------------- T6
def t6():
    print('\n== T6. 服务端在跑时 snapshot/restore 的拒绝与 --live/--force ==')
    base = os.path.join(BASE, 't6')
    root = make_fixture(base)
    snap = mk_snap(root, 't6')
    if not snap:
        return
    snap_hashes = snapshot_hashes(snap, read_manifest(snap))
    # 真实 server.pid 判据（不占端口）
    pidfile = os.path.join(base, 'server.pid')
    with open(pidfile, 'w', encoding='ascii') as f:
        f.write('999999')
    count = len(all_snap_dirs(root))
    r = run_cli(['snapshot', '--data-root', root, '--note', 'should-refuse'])
    ck('T6a 同级 server.pid -> snapshot 拒绝', r.returncode != 0, tail(r))
    ck('T6b 拒绝时不产出快照', len(all_snap_dirs(root)) == count,
       all_snap_dirs(root))
    os.remove(pidfile)
    # --assume-running 测试钩子（不真占 18080/27001/27002）
    r = run_cli(['snapshot', '--data-root', root, '--assume-running'])
    ck('T6c --assume-running -> snapshot 拒绝', r.returncode != 0, tail(r))
    ck('T6d 提示可用 --live 强制', '--live' in out(r), tail(r))
    r = run_cli(['snapshot', '--data-root', root, '--assume-running', '--live',
                 '--note', 'live'])
    live = parse_snap(r)
    ck('T6e --live 可强制在线快照', r.returncode == 0 and bool(live), tail(r))
    if live:
        lm = read_manifest(live)
        ck('T6f live MANIFEST consistent is False', lm.get('consistent') is False,
           lm.get('consistent'))
        ck('T6g live MANIFEST 有 warning',
           bool(lm.get('warnings')) and any('--live' in w for w in lm['warnings']),
           lm.get('warnings'))
        rv = run_cli(['verify', '--data-root', root, os.path.basename(live)])
        ck('T6h live 快照 verify 仍通过（可用但要警示）', rv.returncode == 0, tail(rv))
    before = hash_map(root)
    r = run_cli(['restore', '--data-root', root, sname(snap), '--assume-running'])
    ck('T6i 运行中 restore 默认拒绝', r.returncode != 0, tail(r))
    ck('T6j 拒绝后数据根不变', hash_map(root) == before,
       fail_detail(before, hash_map(root)))
    r = run_cli(['restore', '--data-root', root, sname(snap), '--assume-running', '--force'])
    ck('T6k --force 允许恢复并成功', r.returncode == 0, tail(r))
    ck('T6l --force 打印危险提示', '危险' in out(r), tail(r))
    ck('T6m --force 后与快照逐字节一致', hash_map(root) == snap_hashes,
       fail_detail(snap_hashes, hash_map(root)))


# ---------------------------------------------------------------- T7
def t7():
    print('\n== T7. prune 不删 *-pre-restore ==')
    root = make_fixture(os.path.join(BASE, 't7'))
    base = mk_snap(root, 't7-base')
    if not base:
        return
    ok = True
    detail = ''
    for _i in range(4):
        r = run_cli(['restore', '--data-root', root, sname(base)])
        if r.returncode != 0:
            ok = False
            detail = tail(r)
    ck('T7a 4 次 restore 都成功', ok, detail)
    before = pre_dirs(root)
    ck('T7b 产生 4 份 pre-restore', len(before) == 4, before)
    made = 0
    for i in range(22):
        r = run_cli(['snapshot', '--data-root', root, '--note', 'bulk-%02d' % i])
        made += 1 if r.returncode == 0 else 0
    ck('T7c 22 份普通快照成功', made == 22, made)
    ck('T7d 普通快照受 KEEP=20 限制', len(normal_dirs(root)) <= 20,
       len(normal_dirs(root)))
    ck('T7e 自动 prune 未删任何 pre-restore', set(before) <= set(pre_dirs(root)),
       (before, pre_dirs(root)))
    ordered = sorted(before,
                     key=lambda n: (os.path.getmtime(os.path.join(snapdir(root), n)), n))
    newest3 = set(ordered[-3:]) if len(ordered) >= 3 else set(ordered)
    r = run_cli(['prune', '--data-root', root])
    ck('T7f 显式 prune 成功', r.returncode == 0, tail(r))
    after = pre_dirs(root)
    ck('T7g prune 后 pre-restore 至少保留 3 份', len(after) >= 3, after)
    ck('T7h 最近 3 份 pre-restore 全在', len(newest3) == 3 and newest3 <= set(after),
       (sorted(newest3), after))
    ck('T7i 普通快照仍受 KEEP=20 限制', len(normal_dirs(root)) <= 20,
       len(normal_dirs(root)))


def main():
    print('== _acct_backup_test（隔离临时目录: %s）==' % BASE)
    os.makedirs(BASE, exist_ok=True)
    t1()
    t2()
    t3()
    t4()
    t5()
    t6()
    t7()
    print('\n结果: %s' % ('ALL PASS' if not fails else 'FAIL %d' % len(fails)))
    for f in fails:
        print('  FAIL: %s' % f)
    if fails:
        print('临时目录保留供排查: %s' % BASE)
        sys.exit(1)
    shutil.rmtree(BASE, ignore_errors=True)
    sys.exit(0)


if __name__ == '__main__':
    main()
