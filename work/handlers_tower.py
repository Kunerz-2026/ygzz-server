# -*- coding: utf-8 -*-
# 妖怪正传 私服 —— 妖塔 (2026-09-06 按 NetTower/NetTowerResult 契约)
#   1034 TOWER_DATA -> [1,[[plies, starlevel, c1, c2, c3],...]] (S51_Tower 99层, 星级/条件持久化)
#   9004 TOWER_RESULT -> 战斗结算 (handlers_game.handle_war_result) + 胜则进一层 (plies+1, 该层满星)
import json, time
from state import EQ_STATE, save_state
from handlers_game import handle_war_result

MAX_FLOOR = 99

def _tw():
    return EQ_STATE.setdefault('tower', {'plies': 0, 'stars': {}})

def handle_tower_data(body_json=None):
    # 1034: 每层 [plies, star, c1, c2, c3]; 已通关层 3星满条件; 当前层 0
    #   ⚠ 包裹层: NetTower:recvMessage for i,data in pairs(msg[1]) -> 响应必须 [1,[rows]]
    tw = _tw()
    plies = min(int(tw.get('plies', 0)), MAX_FLOOR)
    rows = []
    for f in range(1, MAX_FLOOR + 1):
        if f <= plies:
            rows.append([f, 3, 1, 1, 1])
        else:
            rows.append([f, 0, 0, 0, 0])
    return ('[1,[' + json.dumps(rows, separators=(',', ':')) + ']]').encode('utf-8')

def handle_tower_war_result(body_json):
    # 9004: 胜 -> 楼层+1 (登顶=99封顶); 复用战斗结算逻辑
    url_body = body_json if isinstance(body_json, bytes) else (body_json or '').encode('utf-8')
    try:
        req = json.loads(url_body.decode('utf-8'))
        if not isinstance(req, dict):   # ★2026-10-11: `(req or {})` 挡不住 list/str（见 _body.py 注释）
            req = {}
        iswin = int(req.get('result') or 0) == 0
    except Exception:
        iswin = False
    # ★★2026-10-16（任务书 D）：`_meta` 是**结算状态机的出口**。
    #   修前"楼层 +1"只信请求体里的 `result` —— 同一条 9004 连发两次就爬两层。
    #   现在只有当 `handle_war_result` 里那次 `battle_session.settle()` 真的成功
    #   （= 本场第一次结算）才进层；重放时 `settled=False`，楼层与任务进度都不动。
    meta = {}
    resp = handle_war_result(body_json, 9004, meta)
    if iswin and meta.get('settled'):
        tw = _tw()
        if tw.get('plies', 0) < MAX_FLOOR:
            tw['plies'] = tw.get('plies', 0) + 1
            save_state()
            # ★2026-10-12: T37 每日任务 CT=4「通关任意 3 层妖塔」。
            #   记在**楼层真的 +1 之后**，而不是 `iswin` 上 —— 登顶(99 层)之后再赢不该再涨
            #   （那时 `plies` 不动，任务进度也不该动）。懒 import + 吞异常。
            try:
                from handlers_task import bump_task, CT_TOWER
                bump_task(CT_TOWER, 1)
            except Exception:
                pass
            # ★★2026-10-18（真机：打完第 8 层仍提示"还没有解锁塔！"）：本场应答里的
            #   roleInfo 是 `handle_war_result` 在**楼层 +1 之前**生成的（本函数才加层），
            #   客户端拿到的 `gamer_getTowerPlies()` 仍是旧值 ⇒ 下一层被判未解锁。
            #   这里把应答的第 0 槽换成本次 +1 之后的 roleInfo 再下发。
            try:
                import json as _json
                from handlers_game import _role_info as _ri
                _j = _json.loads(resp.decode('utf-8'))
                if (isinstance(_j, list) and len(_j) > 1 and isinstance(_j[1], list)
                        and _j[1]):
                    _j[1][0] = _ri()
                    resp = _json.dumps(_j, separators=(',', ':')).encode('utf-8')
            except Exception as _e:
                print('[TOWER] !! 回包 roleInfo 刷新失败(不影响结算): %r' % (_e,), flush=True)
    return resp
