# -*- coding: utf-8 -*-
# 妖怪正传 私服 —— 剧情 STORY_ENTER(4443) / STORY_DROP(4444) / STORY_FINISH(4445)
#                        + 妖怪对战防守族 PVP_MAPLIST(1036) / PVP_GETDEFENSEINFO(7004)
#                          / PVP_DELETEEFENSEINFO(7005) / PVP_CHANGENAME(7015)
#   ★2026-09-12 (v26 goal-round-4) 新增实装。都是 `_audit_netmap.py` 修正口径后的"可达未覆盖"。
#
#   客户端契约 (逐行核对):
#     4443 NetStoryEnter  req{storyid}; recvMessage **只回调** (不读 msg[N]) -> [1,[]] 即可
#          · 入口: app/story/StoryHome.lua:357 `enterStory` -> 回调里才进 StoryScene
#          · 错误码 5025 = "等级不足"
#     4444 NetStoryDrop   req{storyid, storygroupid}; recvMessage:
#          msg[1] = roleInfo(30字段) -> gamer_setRoleInfo ; msg[2] = dropInfo -> refreshDropInfo
#          · 入口: app/story/StoryPlot.lua:237 —— 剧情里"打怪掉落"，且客户端自己记 isdrop 防重复
#     4445 NetStoryFinish req{storyid, review(0首次/1回顾)}; recvMessage **只回调**
#          · 入口: StoryPlot.lua:202/208 + GuideStory.lua:84/212 + WarGuideManager.lua:183
#     1036 NetPvpMapList  req{}; recvMessage: msg[1] = [id,...] -> `unlocklist[id]=id`
#          · ★NetPvpMapList:sendMessage 里是 `if true then return end` (**所有版本都不发**)
#            -> 只能服务端**主动推**; 推进是"已解锁地图 id 列表"
#     7004 NetPvpGetDefenseInfo req{}; recvMessage:
#          msg[1] = [[attacker, atkImg, atkLevel, atkTime, atkResult, reward]] (pairs, 无守卫)
#     7005 NetPvpDeleteDefenseInfo req{...}; recvMessage **只回调**
#     7015 NetPvpChangeName req{teamNum, newName}; recvMessage **只回调**
#          · 入口: app/pvp/PVPDefenseHome.lua:441
#
#   ★ 剧情族的服务端语义: 客户端靠本地 SDStory 表推进剧情, 服务端只做三件事:
#       ① 放行/拦截 (等级) ② 掉落发放 ③ 记录"已看过"用于图鉴解锁口径。
#     私服对"等级不足"要真判, 否则用户会在低等级进到高等级剧情里卡住。
import json, time
from state import EQ_STATE, save_state, _DIR

from state import table_dir as _table_dir   # ★2026-10-02 (审计 §1.6): 表根不再写死盘符
_DATA = _table_dir()


def _load_json(name):
    # ★v60 闭环 v58 §10 缺口 #3: 改走 state.load_table —— 按「尾字段 u32 长度」精确剥离,
    #   不再用 c[:c.rfind(']')+1]（长度字段含 0x5D 时会切错 -> 调用方 except -> 静默空表,
    #   实测 L98_AIPriority / T32_Achievement）。护栏: work/_v60_legacy_tail_guard_test.py
    #   ⚠ 必须**逐行复制**: load_table 的结果是**带缓存的同一个对象**, 而旧实现每次返回全新对象。
    #     调用方若原地改行(排序/填字段)会污染全局缓存 -> 这里保持"每次都是新对象"的原语义。
    from state import load_table as _lt
    return [list(_r) for _r in _lt(name, _DATA)]


def _out(*body):
    return ('[1,' + json.dumps(list(body), separators=(',', ':')) + ']').encode('utf-8')


def _i(v, d=0):
    try:
        return int(v)
    except Exception:
        return d


def _req(body_json):
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    return req if isinstance(req, dict) else {}


# ---------- 剧情静态表: 客户端用哪张, 服务端就用哪张做等级校验 ----------
#   SDStory(剧情) / S01_Story... 1.31 里剧情表带 Level 门槛; 找不到就退化为"不拦"
_STORY = {}      # storyid -> {'next': int, 'level': int, 'group': int}
try:
    import os as _os
    _cand = [f for f in _os.listdir(_DATA) if 'Story' in f or 'story' in f]
    for _f in _cand:
        try:
            _t = _load_json(_f)
            _h = {c: k for k, c in enumerate(_t[0])}
            if 'ID' not in _h:
                continue
            for _r in _t[1:]:
                try:
                    _sid = _i(_r[_h['ID']])
                    _lv = _i(_r[_h['Level']]) if 'Level' in _h else 0
                    _nx = _i(_r[_h['NextID']]) if 'NextID' in _h else 0
                    if _sid:
                        _STORY.setdefault(_sid, {'next': _nx, 'level': _lv, 'group': 0})
                except Exception:
                    continue
        except Exception:
            continue
except Exception:
    pass


def _story_seen():
    v = EQ_STATE.setdefault('story_seen', {})
    return v if isinstance(v, dict) else {}


def _story_dropped():
    v = EQ_STATE.setdefault('story_dropped', {})
    return v if isinstance(v, dict) else {}


# ---------- 1037 STORY_UNLOCKLIST (已解锁剧情) ----------
#   ★2026-09-12 (v30) 新增实装。原为 `_audit_netmap.py` B 类"可达未覆盖"(静默失效)。
#   _audit_netmap 曾把它标为"本轮实发"(1037 由塔入口链发出)；实际上塔链发的是
#   STORY_UNLOCKSCENE=**1038**(已实装)，1037 的真入口在剧情界面:
#     · app/story/StoryHome.lua:71  sendMessage(MSG_ID.STORY_UNLOCKLIST, cb)  -> 章节列表
#     · app/story/StoryPlot.lua:512 getMsg(...):getUnlockList()               -> 回顾用
#   ⚠ 包裹层 (已按 NetBase:parseMessage 实测推导, 勿凭直觉改):
#     parseMessage: msg = _recvMsg[#_recvMsg]  (取最后一个元素), statecode = _recvMsg[1]
#     NetStoryUnlockList:recvMessage: unlockInfo = msg[1] -> 期望 = rows (三重数组列表)
#     => 正确响应 = [1,[rows]]  (仅一层 [ ] 包 rows)
#     ❌ [1,[[rows]]] 会让 msg[1] = [rows] (把 rows 整体当一行) -> info[1] = 第一行(表) -> tonumber 得 nil
#   ⚠ 与 1038 STORY_UNLOCKSCENE 的包裹层**不同**: 1038 客户端 `tonumber(info)` 直接吃 id,
#     故 1038 需要 [1,[[ids]]] (多包一层)。**两者不可互抄**(v25 §10.1 图鉴 1072 少包一层是同一个坑)。
#   语义 (客户端 StoryHome:parseData callfunc):
#     · lastInfo = _unlocklist[#_unlocklist]  → 取列表**最后一项**做进度锚点
#     · ID < lastInfo.id 的章节一律 unlock=1 (顺序解锁)
#     · info.state==1 → isReview=true / unlock=1 / state=1 (已读可回顾)
#   私服口径: 按玩家**等级**返回 (与 T20_StoryGroup.UnlockLv 对齐),
#     state 取服务端真实"已看过"记录 (_story_seen) —— 不伪造已读, 让玩家能正常首通。
_STORY_CHAPTERS = []                    # (id, unlocklv) 仅 Type==1
_CHAPTER_INFO = {}                      # id -> {'unlocklv','lockitem','locktask','type','islocked'}
try:
    _t20 = _load_json('T20_StoryGroup.json')
    _h20 = {c: k for k, c in enumerate(_t20[0])}

    def _c20(r, col):
        return r[_h20[col]] if col in _h20 and _h20[col] < len(r) else '0'

    for _r in _t20[1:]:
        try:
            if str(_c20(_r, 'Type')) != '1':
                continue
            _cid = _i(_c20(_r, 'ID'))
            if _cid:
                _STORY_CHAPTERS.append((_cid, _i(_c20(_r, 'UnlockLv'))))
        except Exception:
            continue
    _STORY_CHAPTERS.sort()
    # ★2026-10-11（3502~3505 批）：`LockedItem` / `LockedTask` 是目标客户端独有的两列，
    #   本轮用 `work\_table_migrate.py cols` 补进表根（既有 15 列逐元素未动）。
    for _r in _t20[1:]:
        try:
            _cid = _i(_c20(_r, 'ID'))
            if not _cid:
                continue
            _CHAPTER_INFO[_cid] = {
                'unlocklv': _i(_c20(_r, 'UnlockLv')),
                'lockitem': str(_c20(_r, 'LockedItem') or '0'),
                'locktask': _i(_c20(_r, 'LockedTask')),
                'type': _i(_c20(_r, 'Type')),
                'islocked': _i(_c20(_r, 'IsLocked')),
            }
        except Exception:
            continue
except Exception:
    _STORY_CHAPTERS = []
    _CHAPTER_INFO = {}


# ---- 剧情任务 T20_2_StoryTaskList（**目标客户端独有**，本轮由 _table_migrate.py add 迁入） ----
#   7 行，全部 `LockedTask=116` —— 也就是整个剧情任务系统只服务**第十六章**（`T20_StoryGroup.ID=116`，
#   该章的 `LockedItem='1103|5'`＝信物"芝罘秘令"×5、`LockedTask=116`）。
#   列：ID / LockedTask / Tittle* / JumpTag / **ConditionType** / Value / **Quantity** / RewardID / Sort
_STORY_TASKS = []
_STORY_TASK_BY_ID = {}
try:
    _t22 = _load_json('T20_2_StoryTaskList.json')
    _h22 = {c: k for k, c in enumerate(_t22[0])}
    for _r in _t22[1:]:
        try:
            _t = {'id': _i(_r[_h22['ID']]), 'locktask': _i(_r[_h22['LockedTask']]),
                  'ct': _i(_r[_h22['ConditionType']]), 'qty': _i(_r[_h22['Quantity']]),
                  'reward': _i(_r[_h22['RewardID']]), 'sort': _i(_r[_h22['Sort']])}
            if _t['id']:
                _STORY_TASKS.append(_t)
        except Exception:
            continue
    _STORY_TASKS.sort(key=lambda x: (x['sort'], x['id']))
    _STORY_TASK_BY_ID = {t['id']: t for t in _STORY_TASKS}
except Exception:
    _STORY_TASKS = []
    _STORY_TASK_BY_ID = {}


def _story_task_claimed():
    v = EQ_STATE.setdefault('story_task_claimed', {})
    if not isinstance(v, dict):
        v = EQ_STATE['story_task_claimed'] = {}
    return v


def _story_unlocked():
    v = EQ_STATE.setdefault('story_unlocked', {})
    if not isinstance(v, dict):
        v = EQ_STATE['story_unlocked'] = {}
    return v


def _story_task_num(ct, qty):
    """任务当前进度 —— **与 UN15 周任务/3068 限时任务同一本账**（`a07_ct`，事件驱动）。

    ⚠ 本工程只有一部分 ConditionType 有驱动点（见 `handlers_task.bump_task` 的调用点：
      1/2/3/9/10/14/15/18/19）。剧情这 7 条用的是 25/80/49/75/37/2/52 ⇒ 目前**只有 2（召唤）
      会真的涨**，其余恒 0。这是"诚实显示真值"，不是漏做：**不许**为了好看编一个进度。
      要让它们动，正确做法是给 `bump_task` 加驱动点（那是另一件事，已记进 AUDIT-backlog）。
    """
    st = EQ_STATE.get('a07_ct')
    n = _i(st.get(str(_i(ct))), 0) if isinstance(st, dict) else 0
    return min(n, _i(qty)) if qty else n


def _story_lockstate(cid):
    """3505 的 `lockstate`：**1=该章已可进入，0=还被挡着**（客户端只在 `==0` 时显示解锁蒙板）。

    判据两条**任一成立即可**（对应客户端的两条路）：
      ① 该章已解锁（`story_unlocked`，3504 真的解过）；
      ② 它的剧情任务**全部**达成（`num >= Quantity` 或已领奖）。
    ⚠ 客户端源码注释写的是"是否完成"，而 `StoryHome.lua:81` 的用法是
      `if lockstate == 0 then taskState = 0`（0 才置"未完成"）⇒ **0=未完成**（注释写反了）。
    """
    info = _CHAPTER_INFO.get(cid) or {}
    if str(cid) in _story_unlocked():
        return 1
    lt = _i(info.get('locktask'))
    if not lt:
        return 1                       # 这章没有解锁任务 -> 不挡
    tasks = [t for t in _STORY_TASKS if t['locktask'] == lt]
    if not tasks:
        return 1
    claimed = _story_task_claimed()
    for t in tasks:
        if str(t['id']) in claimed:
            continue
        if _story_task_num(t['ct'], t['qty']) < t['qty']:
            return 0
    return 1


def _story_unlock_item(info):
    """`LockedItem` -> `(itemid, count)`；`'0'`/空/格式不对 -> `(0, 0)`。

    ⚠ 格式是 `itemid|count`（`StoryHome.lua:61` 用 `tosplit(...,"|")` 且**只认长度恰好 2**）。
    """
    s = str((info or {}).get('lockitem') or '0')
    parts = s.split('|')
    if len(parts) != 2:
        return 0, 0
    return _i(parts[0]), _i(parts[1])


def handle_story_task_info(body_json=None):
    """3502 STORY_TASK_INFO req{storyid} -> [1,[[[taskid,tasknum,taskstate],...]]]。

    客户端 `NetGetStoryTask:recvMessage`：`msg[1]` 用 `pairs` 遍历（**无 or{} 守卫**，
    给 nil 直接抛错 ⇒ 必须给表）；每行 3 槽，`taskstate` 0 未完成 / 1 未领取 / 2 已领取。
    `MStoryHome:refreshData` 还会拿 `taskid` 回查 `T20_2_StoryTaskList` —— **发一条表里没有的
    taskid 会让它 `dataObject[1]` 索引 nil 崩**，所以行只能来自那张表。
    """
    req = _req(body_json)
    sid = _i(req.get('storyid'))
    claimed = _story_task_claimed()
    rows = []
    for t in _STORY_TASKS:
        if sid and t['locktask'] and t['locktask'] != sid:
            continue
        num = _story_task_num(t['ct'], t['qty'])
        if str(t['id']) in claimed:
            state = 2
        else:
            state = 1 if (t['qty'] and num >= t['qty']) else 0
        rows.append([t['id'], num, state])
    return _out(rows)


def handle_story_task_reward(body_json=None):
    """3503 STORY_TASKREWARD req{taskid} -> `[roleInfo(30字段数组), dropInfo]`。

    码（`NetStoryTaskReward.m_errorCode` 逐字）：`537` 任务ID错误 / `538` 该任务未完成 /
    `539` 该任务奖励已经领取。
    ⚠ `roleInfo` 必须是**数组**（客户端 `gamer_setRoleInfo(roleInfo)` 逐字段读）——
      见 `handlers_game._role_info` 的说明，那个字符串/数组两种形态的坑本项目踩过。
    """
    from handlers_game import _role_info as _gi, _drop as _gd
    from handlers_task import _grant_group
    req = _req(body_json)
    tid = _i(req.get('taskid'))
    t = _STORY_TASK_BY_ID.get(tid)
    if t is None:
        return b'[537]'
    if _story_task_num(t['ct'], t['qty']) < t['qty']:
        return b'[538]'
    claimed = _story_task_claimed()
    if str(tid) in claimed:
        return b'[539]'
    groups, ok = _grant_group(t['reward'])
    if not ok:
        return b'[0]'                     # 奖励组查不到 -> 不记"已领"（下次还能再来）
    claimed[str(tid)] = 1
    save_state()
    return _out(_gi(), _gd(groups))


def handle_story_unlock(body_json=None):
    """3504 STORY_UNLOCK req{storyid} -> `[1,[[ucode, itemid, 剩余数量]]]`。

    码（`NetStoryUnLock.m_errorCode` 逐字）：`5023` 剧情id错误 / `5024` 剧情id已经解锁 /
    `5025` 等级不足；另 `30032`＝`T41_CodeKey` 的"道具数量不足"（这张表里没有该码，
    但客户端 `showTips(nil, 30032)` 会按 codeValue 查到文案，比回 `[0]` "消息体为空"准确）。

    ★★ `msg[1]` 是**扁平三元组** `[ucode, itemid, 剩余count]` ——**不是**道具列表：
      客户端 `local propList = msg[1]; prop_getMe():updateItem(propList[1], propList)`，
      而 `SQProp:updateItem` 读的是 `_info[3]`。多发一层（`[[uc,id,n]]`）会让 `_info[3]` = nil
      ⇒ `cclog("updateItem _count = nil")` 后**直接 return**，信物数量在界面上不减。
    """
    req = _req(body_json)
    sid = _i(req.get('storyid'))
    info = _CHAPTER_INFO.get(sid)
    if not info or info.get('type') != 1:
        return b'[5023]'
    if str(sid) in _story_unlocked():
        return b'[5024]'
    if _i(EQ_STATE.get('player_lv', 1)) < _i(info.get('unlocklv')):
        return b'[5025]'
    item, need = _story_unlock_item(info)
    ucode = str(item)
    left = 0
    if item > 0 and need > 0:
        # ⚠ 道具袋的 ucode **就是静态 id 的字符串**（`handlers_task.grant_one` 的 ty==2 分支
        #   `uc = str(obj)`）⇒ 同一静态 id 只有一个 ucode，与客户端 `prop_getCount(id)`
        #   取"第一个同 id 的 ucode 的数量"完全一致（它不求和，所以服务端也不能跨 ucode 凑）。
        have = _i((EQ_STATE.get('prop') or {}).get(ucode), 0)
        if have < need:
            return b'[30032]'                     # 道具数量不足
        left = have - need
        EQ_STATE['prop'][ucode] = left
    _story_unlocked()[str(sid)] = int(time.time())
    save_state()
    return _out([ucode, item, left])


def handle_story_task_unlock_list(body_json=None):
    """3505 STORY_TASK_UNLOCKLIST req{} -> `[1,[[[id, lockstate],...]]]`。

    只有 `getSysOpenV(11026) == 1` 时客户端才发它（`StoryHome.lua:23`），发了就**先等它回来**
    再 `parseData()` ⇒ 不实装的话剧情界面在开关打开后直接不刷新（列表空白）。
    `id` 必须是 `T20_StoryGroup` 的 ID（客户端拿它跟章节 ID 比），`lockstate` 0=未完成/1=可进入。
    """
    rows = []
    for cid in sorted(_CHAPTER_INFO.keys()):
        if _i((_CHAPTER_INFO.get(cid) or {}).get('type')) != 1:
            continue
        rows.append([cid, _story_lockstate(cid)])
    return _out(rows)


def handle_story_unlock_list(body_json=None):
    lv = _i(EQ_STATE.get('player_lv', 1))
    seen = _story_seen()
    dropped = _story_dropped()
    rows = []
    for cid, need in _STORY_CHAPTERS:
        if need and lv < need:
            continue                    # 未达等级门槛 -> 不下发 (客户端按 ID 顺序自行置 unlock)
        rows.append([cid, 1 if str(cid) in dropped else 0, 1 if str(cid) in seen else 0])
    # _out(*body) 已产出 [1,[body...]] -> _out(rows) = [1,[rows]] = 正确形状
    return _out(rows)


def handle_story_enter(body_json=None):
    # 4443: recvMessage 只回调 -> [1,[]]; 但"等级不足"要真判 (码 5025)
    req = _req(body_json)
    sid = _i(req.get('storyid'))
    if not sid:
        # ★2026-11-06 真机：客户端在“章节解锁/无 id”路径会发空 body 的 4443，
        #   原来回 [0,{}] -> 客户端弹「错误，消息体为空」。4443 只是“记录看过”的 ack，
        #   没有 id 时**直接 ack**（不改任何状态），不拿错误弹窗打扰玩家。
        return b'[1,[]]'
    need = _i((_STORY.get(sid) or {}).get('level'))
    if need and _i(EQ_STATE.get('player_lv', 1)) < need:
        return b'[5025]'                              # 等级不足
    _story_seen()[str(sid)] = int(time.time())
    save_state()
    return b'[1,[]]'


# ★★2026-10-20（真机 41003「花末·厨师的基础1」卡死取证）：T21_StorysVer2 的
#   `EventType==1` 是"剧情节点**直接发怪**"：
#     · 10421「迷之狐耳少女下」-> 201301 花末
#     · 109141「兄弟的童年密咒」-> 201401
#   修前 4445 只记 `story_seen`、**不发怪** ⇒ 主线 41003 要求"花末升到 10 级"时玩家没有这只怪，
#   而 `T02_Reward` 里 201301 是 0 命中（魂只有抽卡转盘出）⇒ 只能靠剧情发。
_STORY_MON_EVENTS = None


def _story_mon_events():
    """[(storyRowId, monsterMid)]：T21 里 EventType==1 的节点。懒加载 + 缓存。

    ★★2026-10-22（审查中 M1）：**读表失败时不再缓存空表**。修前异常分支也会把
      `_STORY_MON_EVENTS = []` 写死，于是后面每一次 4445 都"查无发怪节点"、
      而且永久不重试 —— 一次坏读就让玩家再也拿不到剧情怪。现在失败返回空列表但**不缓存**，
      下一次调用会重新读表。
    """
    global _STORY_MON_EVENTS
    if _STORY_MON_EVENTS is not None:
        return _STORY_MON_EVENTS
    out = []
    try:
        t = _load_json('T21_StorysVer2.json')
        h = {c: k for k, c in enumerate(t[0])}
        for r in t[1:]:
            try:
                if _i(r[h['EventType']]) == 1:
                    out.append((str(r[h['ID']]), _i(r[h['EventID']])))
            except Exception:
                continue
    except Exception as _e:
        print('[STORY] !! 读 T21 失败(本次剧情发怪不可用，不缓存): %r' % (_e,), flush=True)
        return out
    _STORY_MON_EVENTS = out
    return out


_T20_LV = None


def _t20_lv():
    """章节号（T20_StoryGroup.ID）-> `UnlockLv`；读表失败 -> 空表（= 不拦）。"""
    global _T20_LV
    if _T20_LV is not None:
        return _T20_LV
    d = {}
    try:
        t = _load_json('T20_StoryGroup.json')
        h = {c: k for k, c in enumerate(t[0])}
        for r in t[1:]:
            try:
                d[int(r[h['ID']])] = int(r[h['UnlockLv']] or 0)
            except Exception:
                continue
    except Exception as _e:
        print('[STORY] !! 读 T20 失败(章节等级不拦): %r' % (_e,), flush=True)
        return d
    _T20_LV = d
    return d


def _story_nodes(sid):
    """`storyid` -> 该次 4445 应当发怪的 T21 节点列表。

    ★★2026-10-22（审查判红 B3）：修前是裸 `rid.startswith(storyid)`，只挡空串。
      传 `1` 或 `10` 会一次匹配到 10421/109141 这些**别的章节**的节点（`'10'` 命中 109141），
      等于凭空发怪。现在两条硬规则：
        · **精确命中**（rid == sid，客户端直接传节点 id）→ 只发这一个节点；
        · **章节前缀**（客户端传 T20 章节号，实测 `StoryPlot.lua` 的 `m_taskID` 是 3 位章节号
          `102`、`WarGuideManager` 直接写 `100`）→ 必须 `len(sid) == 3` 且 rid 以其开头。
      ⇒ `'1'`/`'10'` 一律匹配不到任何节点。
    """
    sid = str(sid or '').strip()
    if not sid.isdigit() or len(sid) < 3:
        return []
    out = []
    for rid, mid in _story_mon_events():
        if mid <= 0:
            continue
        if rid == sid or (len(sid) == 3 and rid.startswith(sid)):
            out.append((rid, mid))
    return out


def _mon_granted():
    """已发放流水：`{'<T21行id>:<mid>': ts}`（永久、跨天不清）。

    ★★2026-10-22（审查判红 B3）：这是**唯一的防重复发怪判据**。修前只靠"当前持有相同 mid"
      ⇒ 花末进化成 201302 之后重放 4445 会再发一只 201301；被当材料吞噬后同理。
      流水按"哪个剧情节点发过哪只怪"记，与怪现在的形态/在不在包里无关。
    """
    v = EQ_STATE.setdefault('story_mon_granted', {})
    if not isinstance(v, dict):
        v = EQ_STATE['story_mon_granted'] = {}
    return v


def grant_story_monsters(storyid):
    """把该章剧情里 `EventType==1` 的怪补发给玩家（幂等：按**发放流水**判重）。

    返回本次新发的只数。⚠ 调用方负责 `save_state()`。
    · 判重：`story_mon_granted['<行id>:<mid>']` 已存在 -> 跳过（与怪的当前形态无关）；
      另外"背包里已经有同一 mid"也算已有（避免玩家已经从抽卡拿到还再发一只），并补记流水。
    · 满包：走 `handlers_bag.grant_monsters`（21 只上限 + 溢出转邮件），
      不再直接 `_add_monster` 塞进存档（审查高危：满包时主线怪进档但显示不出来）。
    """
    from state import all_monster_ucodes, get_monster_base
    sid = str(storyid or '').strip()
    nodes = _story_nodes(sid)
    if not nodes:
        return 0
    ledger = _mon_granted()
    have = set()
    for uc in all_monster_ucodes():
        b = get_monster_base(uc)
        if b:
            have.add(_i(b[0]))
    n = 0
    for rid, mid in nodes:
        key = '%s:%s' % (rid, mid)
        if key in ledger:
            continue
        if mid in have:
            ledger[key] = int(time.time())      # 已经有过 -> 只补流水，不再发
            continue
        try:
            from handlers_bag import grant_monsters as _gm
            rows, mailed = _gm(mid, 1)
        except Exception as _e:
            # ★ 不写流水：这次没发成功，下一次（重放/回顾）还要能补上。
            print('[STORY] !! 剧情怪发放异常 story=%s mid=%s: %r' % (sid, mid, _e), flush=True)
            continue
        if not rows and not mailed:
            print('[STORY] !! 剧情怪没有落地 story=%s mid=%s -> 不记流水，待重试'
                  % (sid, mid), flush=True)
            continue
        ledger[key] = int(time.time())
        have.add(mid)
        n += 1
        if mailed:
            print('[STORY] 剧情怪 %s 因背包已满转邮件（mid=%s）' % (mid, mailed), flush=True)
    return n


def handle_story_finish(body_json=None):
    # 4445: recvMessage 只回调; review=1 表示回顾 (不重复记"首次")
    req = _req(body_json)
    sid = _i(req.get('storyid'))
    review = _i(req.get('review'))
    if not sid:
        # ★2026-11-06 真机：同 4443 —— 无 id 时 ack（不记录、不发怪），不弹「消息体为空」
        return b'[1,[]]'
    # ★★2026-10-22（审查判红 B3）：4445 原来**一个校验都没有**（只挡空 id），
    #   谁都能对任意 id 调"剧情结束"、顺带触发发怪。现在三道闸：
    #     ① 表里对不上 -> 5023「剧情id错误」（`NetStoryFinish.lua:8`，客户端有这条码）；
    #     ② 章节 `UnlockLv` 不够 -> 5025「等级不足」（同 4443 的口径，T20 是那张带等级的章节表）；
    #     ③ 发怪按**发放流水**幂等（见 `grant_story_monsters`），重放/回顾都不会再发。
    #   ⚠ `_STORY` 为空（表读取整体失败）时**不拦** —— 不能因为一次坏读把所有剧情锁死。
    # ★★2026-10-24（独立审查 高 H1）：修前白名单是 `_STORY` —— 它是"目录名含 Story 的**所有**表"
    #   合并出来的集合（约 1282 个 id，含 T21 剧情节点）。于是低等级账号送一个 T21 节点 id
    #   （如 `10421`）既在白名单内、又不在 T20 等级表内 ⇒ `need=0` 直接跳过等级闸，
    #   再经 `_story_nodes` 的**精确命中**发怪，绕开章节 104 的 7 级门槛。
    #   ⇒ 现在判据换成"**必须能映射回 T20 章节**"：
    #     · 本身是 T20 章节 id -> 用它做等级校验；
    #     · 是 T21 节点 id（如 10421/109141）-> 取**前三位**回落到章节 104/109 再做等级校验
    #       （客户端确有按节点发 4445 的路径，`_story_nodes` 的精确命中就是为它留的）；
    #     · 回落不到 T20 的 id -> 5023，**不再**因为"在 `_STORY` 里"就放行。
    #   ⚠ 与修前一致：T20 读表整体失败（`_t20_lv()` 为空）时仍退回旧 `_STORY` 判据，不能因为
    #     一次坏读把所有剧情锁死；两张表都空 -> 不拦。
    _lv20 = _t20_lv()
    if _lv20:
        _chap = sid if sid in _lv20 else None
        if _chap is None:
            _s = str(sid)
            if len(_s) > 3 and _s[:3].isdigit() and int(_s[:3]) in _lv20:
                _chap = int(_s[:3])
        if _chap is None:
            print('[STORY] 4445 id=%s 映射不到 T20 章节 -> 5023' % (sid,), flush=True)
            return b'[5023]'
        need = _lv20.get(_chap, 0)
        if need and _i(EQ_STATE.get('player_lv', 1)) < need:
            print('[STORY] 4445 章节 %s 需 %s 级(当前 %s) -> 5025'
                  % (_chap, need, EQ_STATE.get('player_lv')), flush=True)
            return b'[5025]'
    elif _STORY and sid not in _STORY:
        print('[STORY] 4445 未知剧情 id=%s -> 5023' % (sid,), flush=True)
        return b'[5023]'
    seen = _story_seen()
    if review:
        seen.setdefault(str(sid), int(time.time()))
    else:
        seen[str(sid)] = int(time.time())
    try:
        _n = grant_story_monsters(sid)
        if _n:
            print('[STORY] 剧情发怪 story=%s +%d' % (sid, _n), flush=True)
    except Exception as _e:
        # ★ 发怪失败**不**影响 4445 的 ack（客户端已经播完剧情，回错误只会让它卡在剧情里）；
        #   但也不会记流水 ⇒ 下一次回顾还能补发（见 grant_story_monsters）。
        print('[STORY] !! 剧情发怪失败(不记流水，可重放补发): %r' % (_e,), flush=True)
    save_state()
    return b'[1,[]]'


def handle_story_drop(body_json=None):
    # 4444: msg[1]=roleInfo(30字段) msg[2]=dropInfo
    #   客户端自己用 taskInfo.isdrop 去重, 服务端也记一份 (防重放)
    from handlers_game import _role_info as _gi, _drop as _gd
    from handlers_task import _grant_group
    req = _req(body_json)
    sid = _i(req.get('storyid'))
    gid = _i(req.get('storygroupid'))
    key = '%s_%s' % (sid, gid)
    dropped = _story_dropped()
    if key in dropped:
        return _out(_gi(), _gd([]))                   # 已领过 -> 空 dropInfo (不报错, 客户端只做展示)
    groups = []
    if gid:
        try:
            from handlers_bag import _apply_gift_rewards
            import json as _j
            di = _apply_gift_rewards(str(gid))
            if di and di != '[]':
                groups = _j.loads(di)[1]
        except Exception:
            groups = []
    if not groups:
        # 兜底: 给一点铜钱/妖魂 (不编造道具)
        groups = [[3, [[200]]], [7, [[10]]]]
        EQ_STATE['gold'] = _i(EQ_STATE.get('gold', 0)) + 200
        EQ_STATE['soulcoin'] = _i(EQ_STATE.get('soulcoin', 0)) + 10
    dropped[key] = 1
    save_state()
    return _out(_gi(), _gd(groups))


# ============================================================================
# 妖怪对战防守族 1036 / 7004 / 7005 / 7015
# ============================================================================
_PVP_MAPS = None


def _pvp_maps():
    """PVP 地图静态表 (S/D 系列里带 mapid 的表) -> 已解锁列表用真实 id"""
    global _PVP_MAPS
    if _PVP_MAPS is not None:
        return _PVP_MAPS
    ids = []
    try:
        import os as _os
        for _f in _os.listdir(_DATA):
            if not _f.startswith(('S0', 'S1', 'S2', 'S3', 'S4', 'S5')):
                continue
            if 'Map' not in _f and 'map' not in _f:
                continue
            try:
                _t = _load_json(_f)
                _h = {c: k for k, c in enumerate(_t[0])}
                if 'ID' not in _h:
                    continue
                for _r in _t[1:20]:
                    try:
                        ids.append(_i(_r[_h['ID']]))
                    except Exception:
                        continue
                if ids:
                    break
            except Exception:
                continue
    except Exception:
        ids = []
    _PVP_MAPS = sorted(set(x for x in ids if x))
    return _PVP_MAPS


def _pvp_def():
    v = EQ_STATE.setdefault('pvp_defense', {'records': [], 'teamname': {}, 'maps': [], 'cleared': 0})
    if not isinstance(v.get('records'), list):
        v['records'] = []
    if not isinstance(v.get('teamname'), dict):
        v['teamname'] = {}
    if not isinstance(v.get('maps'), list):
        v['maps'] = []
    return v


def handle_pvp_map_list(body_json=None):
    # 1036: msg[1] = [id, id, ...] -> 客户端 `unlocklist[id]=id`
    #   ★ 客户端 sendMessage 是 `if true then return end`, 只能服务端主动推;
    #     而且**全客户端没有任何回调消费者** (grep 过), 所以内容取"世界地图已解锁节点"
    #     与 1038 STORY_UNLOCKSCENE 保持同源同形 (同一套 map id), 免得两处口径打架。
    #   ⚠ 包裹层: 客户端 `local unlockInfo = msg[1]` -> 必须是 [1,[ids]] (与 1038 一致)
    try:
        from handlers_game import handle_story_unlock_scene
        return handle_story_unlock_scene()
    except Exception:
        ids = _pvp_maps() or [4001]
        return ('[1,[' + json.dumps(sorted(ids), separators=(',', ':')) + ']]').encode('utf-8')


def handle_pvp_get_defense_info(body_json=None):
    # 7004: msg[1] = [[attacker, atkImg, atkLevel, atkTime, atkResult, reward]] (pairs 无守卫)
    #   私服无其他玩家 -> 造 3 条"被挑战"记录 (全是真实结构, 客户端只展示)
    from state import _default_state  # noqa: F401  (保持 import 语义清晰)
    head = EQ_STATE.get('head') or {}
    img = _i(head.get('eq', 1001), 1001)
    d = _pvp_def()
    now = int(time.time())
    # 只有"从没造过 且 用户没主动清空过"才播种 —— 否则 7005 清空后又会立刻冒出来
    if not d.get('cleared') and not d.get('records'):
        d['records'] = [
            ['bot_pvp_1', img, 20, now - 3600, 1, 0],
            ['bot_pvp_2', img, 22, now - 7200, 0, 0],
            ['bot_pvp_3', img, 18, now - 86400, 1, 0],
        ]
        save_state()
    return _out([list(r) for r in (d.get('records') or [])])


def handle_pvp_delete_defense_info(body_json=None):
    # 7005: recvMessage 只回调 -> [1,[]]; 顺手清空记录并**记住"用户清过了"**
    d = _pvp_def()
    d['records'] = []
    d['cleared'] = 1
    save_state()
    return b'[1,[]]'


def handle_pvp_change_name(body_json=None):
    # 7015: recvMessage 只回调（不读 msg）-> 回 [1,[]] 即可
    req = _req(body_json)
    # ★★2026-10-16（请求字段对账抓到的真缺陷）：`NetPvpChangeName.lua` 发的是
    #   **{teamnum, teamname}**（全小写）。修前这里读 `teamNum` / `newName` / `name`
    #   —— 三个键客户端一个都不发 ⇒ `nm` 恒空 ⇒ **改队名永远回 `[0,{}]`（静默失败）**，
    #   玩家点"确认"没反应。与 2026-10-08 的 `1153 damage` 同族：代码写了、字段永远拿不到。
    tn = str(req.get('teamnum') or req.get('teamNum') or '1')
    nm = str(req.get('teamname') or req.get('newName') or req.get('name') or '').strip()
    if not nm:
        return b'[0,{}]'
    if len(nm) > 12:
        return b'[2]'                                 # 名字过长 (与改名同族码)
    _pvp_def()['teamname'][tn] = nm
    save_state()
    return b'[1,[]]'
