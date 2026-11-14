# -*- coding: utf-8 -*-
r"""_fix_unknown_monsters.py —— 清理存档里"客户端渲染不了"的妖怪（登录卡 80% 的元凶）。

背景（2026-11-14，真机复现）：
  · L07_Item_Compose 的 `4000501 UR卡妖怪·魂`(100 片) / `4500011 花末天奇·魂`(100 片)
    的 ComposeID 里带 **500011（花末天奇）**；这只在 `L91_Status` 里**没有行**
    （只有 `L91_StatusRender` 的展示行）。
  · 旧实现 `handlers_bag._add_monster()` 查不到静态表时**静默兜底**（skin=0/skills=0）照样建怪。
  · 客户端 `SDFactory:getObject(SDMonster, 500011)` 返回 nil ⇒ 加载 `1032` 时抛错 ⇒
    **登录永远卡在 80%**（5 个背包的第 5 个），只能删档。

行为：
  · 默认 dry-run，只打印；`--apply` 才写；
  · 写前把原文件备份到 `<data-root>\_backups\unknown-mon-<时间戳>\`；
  · 已知映射：`500011 -> 500001`（花末天奇 → 大天师，同属 UR 碎片产出池，保留 ucode/等级/经验）；
    `--map 500011=500001` 可追加/覆盖；
  · 没映射的未知 mid：从 `monsters` 删除，并同步清 `monster_ueq/lv/exp/skills`（不占背包格）；
  · 幂等：再跑一次不会再有未知 mid；
  · 检测到 `server.pid` 对应进程还活着时拒绝执行（除非 `--force`）。

用法（`--data-root` 指 `work`；也兼容直接指 `.../work/accounts`）：
    python -X utf8 -B _fix_unknown_monsters.py --data-root "D:\...\server\work"
    python -X utf8 -B _fix_unknown_monsters.py --data-root "D:\...\server\work" --apply
"""
import argparse
import io
import json
import os
import shutil
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
HERE = os.path.dirname(os.path.abspath(__file__))


def _table_dir():
    """数据表目录：与 `state.table_dir()` 同一约定（`YGZZ_TABLE_DIR` 或 `<repo>/data/tables`）。"""
    env = (os.environ.get('YGZZ_TABLE_DIR') or '').strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.normpath(os.path.join(HERE, '..', 'data', 'tables'))


TABLE_DIR = _table_dir()

# 默认映射：UUID 碎片池里"有展示行、没有属性行"的怪 -> 同池里真实存在的怪
DEFAULT_MAP = {500011: 500001}
SKIP_FILES = ('_accounts.json', '_auth.json', '_world.json')


def load_l91():
    """mid -> (skin, [skill1..6])；只用标准库，不 import state。"""
    import json as _j
    p = os.path.join(TABLE_DIR, 'L91_Status.json')
    raw = open(p, 'rb').read()
    i = raw.rfind(b']')
    rows = _j.loads(raw[:i + 1].decode('utf-8'))
    h = {c: k for k, c in enumerate(rows[0])}
    out = {}
    for r in rows[1:]:
        try:
            mid = int(r[h['ID']])
        except Exception:
            continue
        sk = 0
        try:
            sk = int(r[h['Skin1']] or 0)
        except Exception:
            pass
        skills = []
        for n in range(1, 7):
            try:
                skills.append(int(r[h['Skill%d' % n]] or 0))
            except Exception:
                skills.append(0)
        out[mid] = (sk, skills)
    return out


def fix_one(data, known, fixmap, dry):
    """返回 (changed, notes)。data 是 EQ_STATE 形态的 dict。"""
    mons = data.get('monsters') or {}
    notes = []
    changed = 0
    for uc in list(mons.keys()):
        row = mons.get(uc)
        if not isinstance(row, list) or not row:
            continue
        try:
            mid = int(row[0])
        except Exception:
            mid = None
        if mid in known:
            continue
        dst = fixmap.get(mid)
        if dst is not None and dst in known:
            sk, skills = known[dst]
            new = list(row)
            new[0] = dst
            # 存档里的怪行是 [mid, skin, num, skills]（state.get_monster_base 的返回口径）——
            # 别把 skin 写到下标 2 上，那是 num（数量），会变成"皮肤=50001"的脏行。
            if len(new) > 1:
                new[1] = sk
            if len(new) > 3:
                new[3] = list(skills)
            notes.append('  ucode=%s  mid %s -> %s（同池替换，保留等级/经验）' % (uc, mid, dst))
            if not dry:
                mons[uc] = new
        else:
            notes.append('  ucode=%s  mid %s 无映射 -> 删除' % (uc, mid))
            if not dry:
                for key in ('monster_ueq', 'monster_lv', 'monster_exp', 'monster_skills'):
                    (data.get(key) or {}).pop(uc, None)
                mons.pop(uc, None)
        changed += 1
    if changed and not dry:
        if mons:
            data['monsters'] = mons
        else:
            data.setdefault('monsters', {})
    return changed, notes


def _pid_alive(data_root):
    p = os.path.join(data_root, 'server.pid')
    try:
        pid = int(open(p).read().strip())
    except Exception:
        return None
    try:
        os.kill(pid, 0)
        return pid
    except Exception:
        return None


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument('--data-root', default=HERE)
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--map', action='append', default=[], help='额外映射，如 --map 500011=500001')
    a = ap.parse_args(argv[1:])

    root = os.path.abspath(a.data_root)
    if os.path.basename(root).lower() == 'accounts':
        root = os.path.dirname(root)
    if not os.path.isdir(root):
        print('[FAIL] data-root 不存在: %s' % root)
        return 2
    pid = _pid_alive(root)
    if pid and not a.force:
        print('[FAIL] 服务端还在跑（server.pid=%s）。先停服，或加 --force 强制' % pid)
        return 2

    fixmap = dict(DEFAULT_MAP)
    for item in a.map:
        try:
            k, v = item.split('=')
            fixmap[int(k)] = int(v)
        except Exception:
            print('[WARN] 忽略非法 --map %r' % item)

    known = load_l91()
    print('L91_Status 已知怪 %d 只；映射表 %r' % (len(known), fixmap))

    targets = []
    st = os.path.join(root, 'state.json')
    if os.path.isfile(st):
        targets.append(st)
    acc = os.path.join(root, 'accounts')
    if os.path.isdir(acc):
        for f in sorted(os.listdir(acc)):
            if f.endswith('.json') and f not in SKIP_FILES:
                targets.append(os.path.join(acc, f))
    if not targets:
        print('[FAIL] 没找到存档（%s 下无 state.json / accounts/*.json）' % root)
        return 2

    total = 0
    hit_files = []
    for p in targets:
        try:
            data = json.loads(open(p, 'rb').read().decode('utf-8-sig'))
        except Exception as e:
            print('[WARN] 读不了 %s: %r' % (p, e))
            continue
        if not isinstance(data, dict):
            continue
        n, notes = fix_one(data, known, fixmap, dry=not a.apply)
        if not n:
            continue
        hit_files.append(p)
        total += n
        print('\n%s：%d 只' % (os.path.relpath(p, root), n))
        for ln in notes:
            print(ln)
        if a.apply:
            bak = os.path.join(root, '_backups', 'unknown-mon-%s' % time.strftime('%Y%m%d-%H%M%S'))
            os.makedirs(bak, exist_ok=True)
            shutil.copy2(p, os.path.join(bak, os.path.basename(p)))
            tmp = p + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, p)
            print('  已写回（备份: %s）' % os.path.relpath(bak, root))

    print('\n合计: %d 只 / %d 个文件' % (total, len(hit_files)))
    if not total:
        print('没有需要修的怪（幂等通过）。')
    elif not a.apply:
        print('这是 dry-run。确认无误后加 --apply 真正写盘（会先备份）。')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
