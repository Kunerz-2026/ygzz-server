# -*- coding: utf-8 -*-
# 妖怪正传 私服 —— 冲虚道场/挑战(TRAIN 1025-1028) (2026-09-06 骨架; 2026-09-07 完整实装)
#   1025 TRAIN_GET     req {}                    -> [1,[[mainId,完成度,[[subId,state],..]],..]]
#        state: 0未完成 1完成未领取 2已领取 (NetTrainState, NetTrain:recvMessage)
#   1026 TRAIN_SELECT  req {id(子关卡)}          -> [1,[]]; [507]关卡id错误 [509]等级不足(B01 LvLimit)
#   1027 TRAIN_RESULT  req {subid, result(0胜1败)} -> [1,[]]; [507]/[514]战斗结果错误; 胜 -> state=1
#   1028 TRAIN_REWARD  req {subid}               -> [1,[roleInfo, dropInfo]]; [507]/[536]未完成/[89]已领取
# 数据: B01_TrainMain(3训练场 SonStageID/LvLimit) + B02_TrainStage(子关卡 UnlockID 链/RewardID=T02组)
import json
from state import EQ_STATE, save_state
from handlers_task import _grant_group
from handlers_game import _role_info, _drop

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

# B01: [(mainId, [subIds..], LvLimit)]; B02: subid -> RewardID(T02组); B01_BY_SUB: subid -> (mainId, LvLimit)
B01 = []
B02_REWARD = {}
B02_IDS = set()
B01_BY_SUB = {}
B01_BY_MAIN = {}          # ★2026-10-20：训练场 mainId -> LvLimit（1026 有时传的是它）
B02_PREV = {}             # ★2026-10-22：子关卡 -> **它的前置子关卡**（B02.UnlockID 反查）
try:
    _t = _load_json('B01_TrainMain.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _mid = int(_r[_h['ID']])
            _sons = [int(x) for x in str(_r[_h['SonStageID']] or '').split('|') if x.strip().isdigit()]
            _lvl = int(_r[_h['LvLimit']] or 0)
            B01.append((_mid, _sons, _lvl))
            B01_BY_MAIN[_mid] = _lvl
            for _s in _sons:
                B01_BY_SUB[_s] = (_mid, _lvl)
        except Exception:
            continue
    _t = _load_json('B02_TrainStage.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _sid = int(_r[_h['ID']])
            B02_IDS.add(_sid)
            B02_REWARD[_sid] = int(_r[_h['RewardID']] or 0)
            # ★★2026-10-22（审查判红 B4）：`UnlockID` 是**本关打通后解锁的下一关**
            #   （实测 1011->1012、1015->1016、1016->0；1021->1022…；1037->1038->0）。
            #   所以反查表要反过来建：`B02_PREV[下一关] = 本关`。
            #   ⚠ 别按"UnlockID 是本关的前置"理解：那样 1011 就要先通 1012，链是反的、必死锁。
            _ulk = int(_r[_h['UnlockID']] or 0)
            if _ulk:
                B02_PREV[_ulk] = _sid
        except Exception:
            continue
except Exception:
    pass


def _train_lv_limit(sid):
    """该子关卡所属训练场的 `LvLimit`（查不到 -> 0 = 不拦）。"""
    m = B01_BY_SUB.get(sid)
    return int(m[1]) if m else 0


def _train_locked(sid):
    """子关卡是否还没解锁：`B02.PREV` 那关必须**已经通关**（state >= 1）。

    ★★2026-10-22（审查判红 B4）：修前 1026/1027 **完全不看** `B02.UnlockID` 链
      ⇒ 低等级账号可以直接报"1016 胜利"，跳过前 5 关拿奖励。
      返回 True = 被前置锁住。
    """
    pred = B02_PREV.get(sid)
    if not pred:
        return False                      # 每个训练场的第一关没有前置
    st = _train_state()
    try:
        return int(st.get(str(pred), 0)) < 1
    except Exception:
        return True

def _train_state():
    st = EQ_STATE.get('train')
    if not isinstance(st, dict):
        st = {}
        EQ_STATE['train'] = st
    return st

def handle_train_get(body_json=None):
    # 1025: 三个训练场, 每场子关卡完成度/领取状态
    #   ⚠ 包裹层: NetTrain:recvMessage dataList=msg[1] -> 响应必须 [1,[rows]] (msg=[rows]->msg[1]=rows)
    st = _train_state()
    rows = []
    for (mid, sons, lvl) in B01:
        subs = []
        for sid in sons:
            subs.append([sid, int(st.get(str(sid), 0))])
        done = sum(1 for s in subs if s[1] >= 1)
        rows.append([mid, done, subs])
    return ('[1,[' + json.dumps(rows, separators=(',', ':')) + ']]').encode('utf-8')

def handle_train_select(body_json):
    # 1026: 进入子关卡 (仅回调; 等级不足 509 / id错误 507)
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    if not isinstance(req, dict):
        return b'[0,{}]'
    try:
        sid = int(req.get('id') or 0)
    except Exception:
        sid = 0
    # ★2026-10-20（真机 21021）：冲虚道场首页三张卡「开始修炼」传的是**训练场 mainId**
    #   （101/102/103），不是子关卡 id；旧实现只认 B02 的子关卡 -> 必回 [507]
    #   「进入关卡时关卡ID错误」，整个冲虚道场进不去、CT=85 永远拿不到。
    #   1026 只是"进入"的 ack（战斗在客户端本地开），所以 mainId 只需过等级门就放行。
    if sid in B01_BY_MAIN:
        _lvl = int(EQ_STATE.get('player_lv', 1))
        if _lvl < int(B01_BY_MAIN[sid]):
            return b'[509]'
        return b'[1,[]]'
    if sid not in B02_IDS:
        return b'[507]'
    main = B01_BY_SUB.get(sid)
    if main:
        _lvl = int(EQ_STATE.get('player_lv', 1))
        if _lvl < int(main[1]):
            return b'[509]'
    # ★★2026-10-22（审查判红 B4）：跳关拦截 —— 前置子关卡没通关就不能进下一关。
    if _train_locked(sid):
        print('[TRAIN] 1026 子关卡 %s 前置未通关 -> 507' % sid, flush=True)
        return b'[507]'
    return b'[1,[]]'

def handle_train_result(body_json):
    # 1027: 战斗结算 (0胜1败, TRAIN_RESULT); 胜 -> 该子关卡 state=1 (完成未领取)
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    if not isinstance(req, dict):
        return b'[0,{}]'
    try:
        sid = int(req.get('subid') or 0)
    except Exception:
        sid = 0
    if sid not in B02_IDS:
        return b'[507]'
    # ★★2026-10-22（审查判红 B4）：**结算处也要重验**（不能只靠 1026 放行）。
    #   修前 1027 只认"合法 subid + result==0" ⇒ 低级账号可以直接报后段子关成功，
    #   随后经 1028 领奖并把 CT=85 记上（审查实测的绕过路径）。
    #   两道闸都读服务端状态，客户端的说法一律不算证据：
    #     ① 训练场等级门（B01.LvLimit）  ② 前置子关卡已通关（B02.UnlockID 链）
    #   ⚠ "战斗真的开过"服务端**不可证**：训练营战斗在客户端本地跑
    #     （`StageDelegateTrain` 本地结算后才发 1027），1026 是"进入训练场列表"的导航 ack、
    #     不是开战票 ⇒ 这里只做能得到证据的那两道，不编一个假的开战会话。
    _need_lv = _train_lv_limit(sid)
    if _need_lv and int(EQ_STATE.get('player_lv', 1)) < _need_lv:
        print('[TRAIN] 1027 subid=%s 等级不足(%s<%s) -> 509'
              % (sid, EQ_STATE.get('player_lv'), _need_lv), flush=True)
        return b'[509]'
    if _train_locked(sid):
        print('[TRAIN] 1027 subid=%s 前置未通关 -> 507' % sid, flush=True)
        return b'[507]'
    try:
        res = int(req.get('result'))
    except Exception:
        return b'[514]'
    if res not in (0, 1):
        return b'[514]'
    if res == 0:
        st = _train_state()
        if int(st.get(str(sid), 0)) == 0:
            st[str(sid)] = 1
            # ★★2026-10-20（主线 21021「冲虚道场-入门法阵通关」CT=85 Value=101）：
            #   UG 的 CT=85 **不再读这本账** —— 2026-10-22 审查（高 H3）后改成
            #   `handlers_ugtask._sched` 直接读 `train[subid]` 并经 B01 映射回 mainId：
            #     · 102/103 下的关卡不会点亮"101 通关"；
            #     · 也没有"账没记上但状态已变"的部分成功裂缝。
            #   这里保留 `bump_task` 只为 T37/A07 那些按 CT 聚合的每日任务；
            #   它失败只影响当日任务，不影响主线。
            try:
                from handlers_task import bump_task as _bt85, CT_TRAIN as _CT85
                _bt85(_CT85, 1)
            except Exception as _e:
                print('[TRAIN] !! CT=85 每日账计数失败(不影响 UG，UG 读 train 状态): %r'
                      % (_e,), flush=True)
            save_state()
    return b'[1,[]]'

def handle_train_reward(body_json):
    # 1028: 领取子关卡奖励 (T02 RewardID 组; 已领 89 / 未完成 536)
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    if not isinstance(req, dict):
        return b'[0,{}]'
    try:
        sid = int(req.get('subid') or 0)
    except Exception:
        sid = 0
    if sid not in B02_IDS:
        return b'[507]'
    st = _train_state()
    cur = int(st.get(str(sid), 0))
    if cur == 0:
        return b'[536]'
    if cur == 2:
        return b'[89]'
    gid = B02_REWARD.get(sid, 0)
    groups, ok = _grant_group(gid)
    if not ok:
        return b'[0,{}]'
    st[str(sid)] = 2
    save_state()
    data = [_role_info(), _drop(groups)]
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')
