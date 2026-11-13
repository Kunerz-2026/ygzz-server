# -*- coding: utf-8 -*-
r"""_fix_stage6_monsters.py —— 把存档里“客户端不支持”的 6 阶妖怪降回同链 5 阶。

背景（2026-11-07）：
  · 客户端进化界面以 `L91_Status.AdvanceID` 为门控；5 阶行的 AdvanceID 在
    1.28/1.28g/1.29v/1.30/1.31 五套表里全是 0 ⇒ 正常玩法最高 5 阶。
  · 6 阶只能从后台/邮件直接发进来；直发会出现属性空白/队伍错位（v35 用户实测），
    而且修前只能靠后台删档。
  · 本轮起 `validate_mail_item` 默认拒绝 Star>=6；本脚本清理**已存在**的 6 阶。

行为：
  · 默认 dry-run，只打印要改什么，不落盘；
  · `--apply` 才写；写前把原文件复制到 `<work>\_backups\stage6-<时间戳>\`；
  · 降阶：同 `SourceID` 链上找 Star=5 的行（没有就找链首），保留 ucode/等级/技能；
    `monster_lv` 超过 L104_RevLv[5].MstLvLimit 时 clamp 到上限，`monster_exp` 清零；
    `evolve.max_stage` / `ug.mon_star` 里的 6 封顶到 5；
  · 幂等：再跑一次不会再有 Star>=6 的怪。
  · 检测到 `server.pid` 对应进程还活着时拒绝执行（除非 `--force`）。

用法（`--data-root` 指服务端的 `work` 目录；也兼容直接指 `.../work/accounts`）：
    python -X utf8 -B _fix_stage6_monsters.py --data-root "<server>\work"
    python -X utf8 -B _fix_stage6_monsters.py --data-root "<server>\work" --apply
"""
import argparse
import io
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TABLE_DIR = os.environ.get('YGZZ_TABLE_DIR') or os.path.normpath(os.path.join(HERE, '..', 'data', 'tables'))


def _load_json(name):
    p = os.path.join(TABLE_DIR, name)
    raw = open(p, 'rb').read()
    i = raw.rfind(b']')
    return json.loads(raw[:i + 1].decode('utf-8'))


def _h(rows):
    return {c: k for k, c in enumerate(rows[0])}


def load_tables():
    rows = _load_json('L91_Status.json')
    h = _h(rows)
    by_id = {}
    for r in rows[1:]:
        try:
            sid = str(r[h['ID']])
            by_id[sid] = {
                'id': sid,
                'star': int(r[h['Star']] or 1),
                'src': str(r[h['SourceID']] or sid),
                'adv': str(r[h['AdvanceID']] or '0'),
            }
        except Exception:
            continue
    chains = {}
    for sid, d in by_id.items():
        chains.setdefault(d['src'], []).append(sid)
    for k in chains:
        chains[k].sort()
    cap = {}
    try:
        t = _load_json('L104_RevLv.json')
        hh = _h(t)
        for r in t[1:]:
            try:
                cap[int(r[hh['RevLv']])] = int(r[hh['MstLvLimit']] or 30)
            except Exception:
                continue
    except Exception:
        pass
    return by_id, chains, cap


def prev_ok(by_id, chains, sid):
    """同链上一个可用阶段（优先 Star=5，否则链首）；找不到返回 None。"""
    d = by_id.get(str(sid))
    if not d:
        return None
    chain = chains.get(d['src']) or []
    target_star = max(1, d['star'] - 1)
    for x in chain:
        if by_id[x]['star'] == target_star:
            return x
    if target_star != 5:
        for x in chain:
            if by_id[x]['star'] == 5:
                return x
    return chain[0] if chain else None


def scan_file(path, by_id, chains, cap):
    """返回 (data, hits)；hits = [(ucode, old_sid, new_sid, old_lv, new_lv)]"""
    try:
        data = json.load(io.open(path, encoding='utf-8-sig'))
    except Exception as e:
        print('  [WARN] %s 解析失败: %r' % (os.path.basename(path), e))
        return None, []
    mons = data.get('monsters') or {}
    ov = data.get('monster_base_override') or {}
    lv = data.get('monster_lv') or {}
    hits = []
    for uc in set(list(mons.keys()) + list(ov.keys())):
        old = None
        if uc in ov and isinstance(ov[uc], list) and ov[uc]:
            old = str(ov[uc][0])
        elif uc in mons and isinstance(mons[uc], list) and mons[uc]:
            old = str(mons[uc][0])
        if not old:
            continue
        d = by_id.get(old)
        if not d or d['star'] < 6:
            continue
        new = prev_ok(by_id, chains, old)
        if not new:
            continue
        old_lv = int(lv.get(uc) or 0)
        lim = int(cap.get(by_id[new]['star'], cap.get(5, 30)))
        new_lv = min(old_lv, lim) if old_lv else old_lv
        hits.append((str(uc), old, new, old_lv, new_lv))
    return data, hits


def apply_file(path, data, hits, by_id, cap):
    mons = data.get('monsters') or {}
    ov = data.get('monster_base_override') or {}
    lv = data.get('monster_lv') or {}
    exp = data.get('monster_exp') or {}
    for uc, old, new, old_lv, new_lv in hits:
        if uc in mons and isinstance(mons[uc], list) and mons[uc]:
            mons[uc][0] = int(new)
        if uc in ov and isinstance(ov[uc], list) and ov[uc]:
            ov[uc][0] = int(new)
        if uc in lv:
            lv[uc] = int(new_lv)
            lim = int(cap.get(by_id[new]['star'], cap.get(5, 30)))
            if int(new_lv) >= lim:
                exp[uc] = 0
    ev = data.get('evolve') or {}
    if isinstance(ev, dict) and int(ev.get('max_stage') or 0) > 5:
        ev['max_stage'] = 5
    ug = data.get('ug') or {}
    if isinstance(ug, dict):
        ms = ug.get('mon_star') or {}
        if isinstance(ms, dict):
            for k, v in list(ms.items()):
                try:
                    if int(v) > 5:
                        ms[k] = 5
                except Exception:
                    continue
    tmp = path + '.tmp'
    with io.open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(data, f, ensure_ascii=False, separators=(',', ':'))
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--data-root', required=True)
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--force', action='store_true')
    a = ap.parse_args(argv)
    root = os.path.abspath(a.data_root)
    if not os.path.isdir(root):
        print('★ 数据根不存在: %s' % root)
        return 2
    # 兼容 --data-root 指 work 或 work/accounts
    if os.path.isdir(os.path.join(root, 'accounts')):
        work, acct = root, os.path.join(root, 'accounts')
    else:
        work, acct = os.path.dirname(root), root
    pidf = os.path.join(work, 'server.pid')
    if os.path.isfile(pidf) and not a.force:
        try:
            pid = int(io.open(pidf, encoding='utf-8').read().strip())
            os.kill(pid, 0)
            print('★ 服务端还在运行（server.pid=%s）；先停服，或确认后用 --force。' % pid)
            return 2
        except Exception:
            pass
    by_id, chains, cap = load_tables()
    print('表: L91=%d 行 / 链=%d 条 / L104=%s' %
          (len(by_id), len(chains), sorted(cap.items())[:6]))
    targets = []
    st = os.path.join(work, 'state.json')
    if os.path.isfile(st):
        targets.append((st, 'state'))
    if os.path.isdir(acct):
        for n in sorted(os.listdir(acct)):
            if n.startswith('u') and n.endswith('.json'):
                targets.append((os.path.join(acct, n), 'account'))
    if not targets:
        print('★ 没找到 state.json / accounts/u*.json；--data-root 指到服务端 work 目录。')
        return 2
    plans = []
    total = 0
    for path, kind in targets:
        data, hits = scan_file(path, by_id, chains, cap)
        if data is None or not hits:
            continue
        plans.append((path, data, hits))
        total += len(hits)
        print('\n%s (%s): %d 只 6 阶' % (os.path.basename(path), kind, len(hits)))
        for uc, old, new, olv, nlv in hits:
            print('  ucode=%-6s %s(Star=%s) -> %s(Star=%s)  lv %s -> %s'
                  % (uc, old, by_id[old]['star'], new, by_id[new]['star'], olv, nlv))
    if not total:
        print('\n没有 Star>=6 的妖怪；不用改。')
        return 0
    print('\n合计 %d 只。' % total)
    if not a.apply:
        print('dry-run：未写文件。加 --apply 才落盘。')
        return 0
    bak = os.path.join(work, '_backups', 'stage6-%s' % time.strftime('%Y%m%d-%H%M%S'))
    os.makedirs(bak, exist_ok=True)
    for path, data, hits in plans:
        base = os.path.basename(path)
        shutil.copy2(path, os.path.join(bak, base))
        apply_file(path, data, hits, by_id, cap)
        print('  已改 %s' % base)
    print('\n完成；原文件备份在 %s' % bak)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
