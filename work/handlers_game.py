# -*- coding: utf-8 -*-
# 妖怪正传 私服 —— 玩法系统处理器 (按 服务器接口字段字典.md 字段照抄)
#   商店 5001/5002/5003 | 战斗结算 9001-9007 | 任务/成就 1029/1041-1048 | 探索 1701-1706
#   头像 1092-1096 | 宝箱 1060-1062 | 技能 1502-1505 | 进化 6003 | PVP 7001-7012 | 活动 1100-1116 | 公会 1121-1139
import json, os, random, time
from state import EQ_STATE, save_state, get_monster_base, get_monster_lv, get_monster_exp, all_monster_ucodes, player_add_exp
from state import alloc_monster_ucode   # ★2026-10-02 (审计 §9 #3): 原子取号
from state import get_monster_advance, get_monster_source, get_monster_star, vitality_timerid
from state import explore_role_id as _explore_role_id
from config_data import _mon
from handlers_equip import _mon_with_state, _equip_item, mon_info_slot
from handlers_draw import _MON_STATIC
from handlers_task import bump_task, CT_MONTHCARD, CT_WEEKCARD, _grant_group, _T02
# ★2026-10-16（任务书 E）：战斗类每日任务的 ConditionType（详见 handlers_task 里的
#   "7 个 GAP 逐条接上"那段注释：每个都写了表文案 + 客户端 msgid 出处）。
from handlers_task import (CT_LEGEND, CT_YMYW, CT_AGGRESS, CT_EGG, CT_POT)
from handlers_task import CT_AGGRESS_CLEAR
from handlers_cult import _remove_monster
import state as _state_mod

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

def _role_info():
    # ★★2026-10-02 (审计 §8 / §13 排名 4, P0): 本函数原是 roleInfo 的第 N 份**独立副本**。
    #   副本的字段[11] 停在 v58 已修掉的坏值 `'1'` —— 客户端把该字段按 `id&状态` 拆
    #   (SQGamer.lua:200 guide = tostring(roleInfo[12]) -> :224-232 按 ':' 和 '&' 拆),
    #   发 '1' 得到 guideArray = {{ "1" }} -> info[2] 为 nil ->
    #   NetUpdateInfo:sendMessage 第 24 行 `info[1].."&"..info[2]` 抛错 ->
    #   抽卡回调中断 -> 用户现象「符咒一抽」(cardtype=3) 抽完不出动画、不弹结果。
    #   除 [11] 外, 各副本还与权威实现在 [4] 体力 / [6] 妖塔层数 / [13] 鬼道积分 /
    #   [17][18] 周月卡 buff / [25][26] 卡到期时间戳 等处**各自漂移**(详见 git 历史),
    #   每漂移一处就是一类"用某个功能后 roleInfo 被刷坏"的事故。
    #   ⇒ 统一走**唯一权威实现** state.role_info_array() (它的 docstring 记着 v32/v58 两次事故)。
    from state import role_info_array
    return json.loads(role_info_array())

def _drop(groups):
    # dropInfo 契约 (客户端 NetDropRewardData:refreshDropInfo 源码):
    #   dropInfo = [[], groups]; groups = [ [style, goodslist], ... ]
    #   客户端 ipairs(groups) -> rewardInfo[1]=style, rewardInfo[2]=goodslist(pairs 遍历, 单元素天然无碍)
    #   --- 注意: 客户端 cjson 解析无"单元素数组解包", 直接原样返回; 任何多包一层都会让
    #       goodsInfo 变成 [ [x] ] 导致 tonumber(表) => nil, 客户端掉落解析崩
    return [[], list(groups or [])]

# ============ 妖神传说 1014 (LENGEND_GET) + 妖怪入侵 1063 (AGGRESS_TODAY) —— 时间轮换系统 ============
#   客户端事实: NetLengendGet:recvMessage msg=[monsterid, skillstate, freshtime, id] (callFunc(monsterid,id,skillstate,freshtime))
#   T25_YSCSSX: 妖神轮换表 (ID/Time/MonsterId/MonsterGroup/Difficulty*RewardID/UnlockLV1-5) 共 21 行;
#   按天轮换 row = T25[(day_of_year-1) % 21]; freshtime(ms) = 距下个本地午夜 (客户端"距下次妖神刷新"倒计时).
#   2026-09-08 实装 (用户反馈: 妖神传说怪物应根据时间出现, 恒 00:00:00 无刷新的假象)
_T25_ROWS = []
_T25_FULL = {}        # ★v53: T25 行 id -> 整行 dict (MonsterGroup..5 / UnlockLV1..5 / Difficulty*RewardID)
_T25_GROUP_COLS = ['MonsterGroup', 'MonsterGroup2', 'MonsterGroup3', 'MonsterGroup4', 'MonsterGroup5']
_T25_UNLOCK_COLS = ['UnlockLV1', 'UnlockLV2', 'UnlockLV3', 'UnlockLV4', 'UnlockLV5']
# ★2026-09-15 (v54): 难度 -> 官方奖励组。列名逐字取自 T25 表头 (Difficulty1RewardID..5)。
#   值域实测: 21 行 x 5 档 = 105 个值, 去重 59 个, **59/59 全部命中 T02_Reward.GroupID**
#   (0 命中 T02_Reward.ID) —— 即"奖励组"这一个 id 空间, 取数必须走 GroupID (铁律 #164/#166)。
_T25_REWARD_COLS = ['Difficulty1RewardID', 'Difficulty2RewardID', 'Difficulty3RewardID',
                    'Difficulty4RewardID', 'Difficulty5RewardID']
try:
    _t = _load_json('T25_YSCSSX.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _rid = int(_r[_h['ID']])
            _T25_ROWS.append((_rid, int(_r[_h['MonsterId']] or 0)))
            _T25_FULL[_rid] = {c: _r[_h[c]] for c in _h}     # ★v53: 整行留底(1017 要按难度取 MonsterGroup)
        except Exception:
            continue
except Exception:
    pass

# ============================================================================
# ★v53 (2026-09-13) T01_MstrGroup 战斗组 id 闸门 —— 治"战斗组数据为空 id is: N"卡死
# ----------------------------------------------------------------------------
# 事故: 挑战→妖神传说→点挑战, 客户端弹"战斗组数据为空 id is: 1006"并**永久卡在 0%**。
#   链条(逐行读 1.31 装机源码确认):
#     客户端 legend/VLegendSelect.lua:158  1017 回包 msg[4] -> 回调 _battleID
#                        :166             initScene(PVELEGEND, {...}, _battleID)
#     war/WarData.lua:104  usGetTableData(tT01_MstrGroup.tName, tonumber(_battleID))
#                        :106-111        data==nil -> 弹 LOCAL_TIPS_FIGHT_GROUP_IS_NULL + **return**
#                                        ★这次 return **没有** 置 m_isFinishData=true
#     scenes/StageScene.lua:61-67          永远等不到 getIsFinishData() -> 加载进度停在 0%
#   而服务端 1017 当时回的是 **T25 行 id**(1006=当天行), 客户端却拿它查 T01_MstrGroup
#   (1006 不在表里 -> nil). 两个 id 空间被混用了。
#   ★更阴的一点: T25 行 id 里有 1001/1002/1003/1011/1012 恰好**也在** T01_MstrGroup 里
#   (第一章关卡), 那些天上不会报错, 而是**静默加载错的战斗**(第一章小怪当妖神打)。
# ----------------------------------------------------------------------------
# 所以: 服务端**任何**要下发给客户端当 _battleID 用的值, 必须先过这道闸门。
#   表读不到时返回 True (保守: 不因表缺失阻断玩法, 与 v49 部位校验同口径)。
_T01_GROUP_IDS = set()
try:
    _t = _load_json('T01_MstrGroup.json')
    for _r in _t[1:]:
        try:
            _T01_GROUP_IDS.add(int(_r[0]))
        except Exception:
            continue
except Exception:
    pass

def is_valid_war_group(_gid):
    # 该 id 是不是 T01_MstrGroup 的真实行 (客户端 WarData 就查这张表)
    if not _T01_GROUP_IDS:
        return True
    try:
        return int(_gid) in _T01_GROUP_IDS
    except Exception:
        return False

def war_group_guard(_gid, _where):
    # 出闸门 + 留痕: 非法就大声打日志(客户端那边是"卡死", 服务端必须留证据)
    if is_valid_war_group(_gid):
        return True
    print('[WAR_GROUP] !! 非法战斗组 id=%s 来源=%s (不在 T01_MstrGroup 里; 客户端会弹"战斗组数据为空"并卡死)'
          % (_gid, _where))
    return False

def _t25_today_idx():
    # 当天应取的行下标 (day_of_year 轮换); 表空返回 None
    if not _T25_ROWS:
        return None
    try:
        import datetime as _dt
        _doy = _dt.datetime.now().timetuple().tm_yday
    except Exception:
        _doy = 1
    return (_doy - 1) % len(_T25_ROWS)

def _t25_today():
    # ★2026-09-12 (v26): 抽出 1014 的"当天 T25 行"计算, 供 1012/1017 复用
    #   返回 (row_id, monster_id); 表为空时给安全兜底 (1001, 100031)
    #   ⚠ 1014 用 row_id 是**对的** (客户端拿它查 SDYSCSSX/T25 刷新表);
    #     1017 不能直接用它当 battleID —— 见 _t25_today_row / MonsterGroup{d} (v53)
    _i = _t25_today_idx()
    if _i is None:
        return (1001, 100031)
    _rid, _mid = _T25_ROWS[_i]
    return (int(_rid), int(_mid))

def _t25_today_row():
    # ★v53: 当天 T25 行的**整行** (1017 要按难度取 MonsterGroup{d} / UnlockLV{d})
    _i = _t25_today_idx()
    if _i is None:
        return _T25_FULL.get(1001)
    return _T25_FULL.get(_T25_ROWS[_i][0])

def t25_war_group(row, difficulty):
    # T25 行 + 难度(1..5) -> 该难度真正的战斗组 id (T01_MstrGroup 行)
    #   列映射: 1->MonsterGroup 2->MonsterGroup2 ... 5->MonsterGroup5 (SDYSCSSX.lua:36-54 逐字段确认)
    if not row or not isinstance(difficulty, int) or difficulty < 1 or difficulty > len(_T25_GROUP_COLS):
        return None
    try:
        v = row.get(_T25_GROUP_COLS[difficulty - 1])
        return int(v) if v not in (None, '', '0') else None
    except Exception:
        return None

def t25_unlock_lv(row, difficulty):
    # T25 行 + 难度 -> 该难度解锁等级 (CLegendHome.lua:68-94 客户端门禁用的同一列)
    if not row or not isinstance(difficulty, int) or difficulty < 1 or difficulty > len(_T25_UNLOCK_COLS):
        return None
    try:
        v = row.get(_T25_UNLOCK_COLS[difficulty - 1])
        return int(v) if v not in (None, '') else None
    except Exception:
        return None

def t25_reward_group(row, difficulty):
    # ★2026-09-15 (v54): T25 行 + 难度(1..5) -> 官方奖励组 id (= T02_Reward.GroupID)
    #   证据链:
    #     客户端面板 legend/VLegendSelect.lua:64-84 拿 rewardData.Rate*100 显示 "5%",
    #       而 rewardData 来自 IconData:parseGroupRewards(_id) -> :156
    #       `GetTableSelectData(tT02_Reward.tName, tT02_Reward.GroupID, tostring(_id))`
    #       ⇒ 面板传进去的那个 _id 就是 **GroupID**。
    #     面板那个 _id 从哪来: legend/NetLengendGet (1014) 回包里的 rewardID -> 同表 Difficulty{d}RewardID。
    #   所以"预览"和"实发"必须用同一个值, 否则玩家看到 5% 稀有、实际必掉或干脆没有。
    if not row or not isinstance(difficulty, int) or difficulty < 1 or difficulty > len(_T25_REWARD_COLS):
        return None
    try:
        v = row.get(_T25_REWARD_COLS[difficulty - 1])
        return int(v) if v not in (None, '', '0') else None
    except Exception:
        return None

def handle_lengend_get(body_json=None):
    # 1014 -> [1,[monsterid, skillstate, freshtime, id]]
    try:
        import datetime as _dt
        _now = _dt.datetime.now()
        _doy = _now.timetuple().tm_yday
        _mid = (_now + _dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        _freshtime = int((_mid - _now).total_seconds() * 1000)
    except Exception:
        _doy, _freshtime = 1, 86400000
    if not _T25_ROWS:
        return b'[1,[1,0,86400000,1001]]'
    _rid, _mid2 = _T25_ROWS[(_doy - 1) % len(_T25_ROWS)]
    return ('[1,[%d,0,%d,%d]]' % (_mid2, _freshtime, _rid)).encode('utf-8')

# 入侵: T26_YGRQ 行 GroupID -> [(ID, Difficulty)]; 周一~周六日组映射 (客户端 CAggressHome.weeks 常量)
_AGGRESS_GROUPS = {1: ['910101'], 2: ['920101'], 3: ['930101'], 4: ['940101'], 5: ['950101'],
                   6: ['960101', '970101', '980101'], 7: ['960101', '970101', '980101']}
_T26_BY_GROUP = {}
_T26_ID_GROUP = {}
try:
    _t = _load_json('T26_YGRQ.json'); _h = {c: k for k, c in enumerate(_t[0])}
    _dfk = 'Difficulty ' if 'Difficulty ' in _h else 'Difficulty'
    for _r in _t[1:]:
        try:
            _g = str(_r[_h['GroupID']] or '')
            _sid = int(_r[_h['ID']] or 0)
            _T26_BY_GROUP.setdefault(_g, []).append((_sid, int(_r[_h[_dfk]] or 0)))
            _T26_ID_GROUP[str(_sid)] = _g
        except Exception:
            continue
    for _k in _T26_BY_GROUP:
        _T26_BY_GROUP[_k].sort()
except Exception:
    pass

# ★v53: 妖怪入侵"波次组表" T26_YGRQZD 的真实 StageID 集
#   客户端 pve/PVEAggressData.lua:125 `GetTableSelectData(tT26_YGRQZD, tT26_YGRQZD.StageID, tostring(id))`
#   -> data[1]["ID"] -> SDAggressFight(ID) -> m_Group[波数] = 每波的战斗组
#   ⇒ 断线重连(1012)下发的 levelId 必须是**这里面的 StageID**, 不是 T25 行 id!
_T26ZD_STAGE_IDS = set()
_T26ZD_FIRST_STAGE = {}     # groupid(910101) -> 该组最小的 StageID(10011)  ← 断线重连保守重开点
try:
    _t = _load_json('T26_YGRQZD.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _T26ZD_STAGE_IDS.add(int(_r[_h['StageID']]))
        except Exception:
            continue
    for _g, _rows in _T26_BY_GROUP.items():
        # _T26_BY_GROUP 的 key 是 T26_YGRQ.ID, 而它 == T26_YGRQZD.StageID (48 行 1:1, 见 _legend_group_selftest)
        _ok = sorted(_sid for _sid, _df in _rows if _sid in _T26ZD_STAGE_IDS)
        if _ok:
            _T26ZD_FIRST_STAGE[_g] = _ok[0]
except Exception:
    pass

def _aggress_today_groupid(_wd=None):
    # 今天开放的入侵组 (周一~周六日组映射, 与 1063 同源)
    try:
        if _wd is None:
            _wd = int(time.strftime('%w'))
            if _wd == 0:
                _wd = 7
    except Exception:
        _wd = 1
    _gs = _AGGRESS_GROUPS.get(_wd) or []
    return str(_gs[0]) if _gs else None

def aggress_today_stage_id():
    # ★v53 1012 断线重连用: 今天开放组的**最小 StageID** (私服无波次进度存储 -> 保守重开第 1 关)
    _g = _aggress_today_groupid()
    if _g and _T26ZD_FIRST_STAGE.get(_g):
        return int(_T26ZD_FIRST_STAGE[_g])
    # 兜底: 任何一张合法的入侵 StageID; 再不行才给 10011
    _all = sorted(_T26ZD_STAGE_IDS)
    return int(_all[0]) if _all else 10011

def _aggr():
    # 入侵每日免费挑战扣减持久化 (2026-09-08; groupid -> 今日已用次数)
    return EQ_STATE.setdefault('aggress', {'date': '', 'used': {}})

def _aggr_reset():
    a = _aggr()
    today = time.strftime('%Y-%m-%d')
    if a.get('date') != today:
        a['date'] = today
        a['used'] = {}
    return a

def handle_aggress_today(body_json=None):
    # 1063 -> [1,[[groupid, count, [[id, state, flag]..]], ..]] (仅今日开放组; NetAggressToday 官方样例注释)
    #   count = 剩余免费挑战次数 (每日 1 次/组, 1702 扣减持久化; 用完后 count=0, 客户端 setWeekOpen 门控关闭)
    try:
        _wd = int(time.strftime('%w'))          # 0=周日
    except Exception:
        _wd = 1
    if _wd == 0:
        _wd = 7
    _aggr_reset()
    rows = []
    for _gid in _AGGRESS_GROUPS.get(_wd, []):
        _stages = _T26_BY_GROUP.get(_gid, [])
        _used = int((_aggr()['used'] or {}).get(str(_gid), 0))
        _free = max(0, 1 - _used)
        _inner = [[_sid, 1, 1 if _i == 0 else 0] for _i, (_sid, _df) in enumerate(_stages)]
        rows.append([int(_gid), _free, _inner])
    # ★★2026-10-18（真机 crash：`NetAggressToday.lua:40 attempt to index local 'data'
    #   (a number value)` ⇒ 妖怪入侵图标点不开、客户端每秒重发 1063）：
    #   契约是 `[1, [rows]]`。修前回 `[1, rows]` ⇒ msg=rows ⇒ 客户端 `msg[1]` 拿到
    #   **第一行**，`for k,data in pairs(那一行)` 遍历到数字 ⇒ 崩。与 3500 同型的
    #   "少包一层"。客户端样例注释（NetAggressToday.lua 末尾）也是三层：msg[1] 才是行表。
    return ('[1,' + json.dumps([rows], separators=(',', ':')) + ']').encode('utf-8')

def _rd_resp(dropgroups=None):
    # 注意: parseMessage 取响应【最后一个元素】为 msg -> 正确格式 = [1, [body...]] 嵌套式
    return json.dumps([1, [_role_info(), _drop(dropgroups or [[3, [[0]]]])]], separators=(',', ':'))

def _simple_resp(*body):
    # 嵌套式响应 [1, [body...]] (客户端 parseMessage: msg=响应数组最后一个元素)
    return json.dumps([1, list(body)], separators=(',', ':'))

# ============ 商店 SHOP_TOTAL(5001) / SHOP_BUY(5002) / SHOP_UPDATE(5003) ============
# 2026-09-06 按客户端契约重写 (NetShopTotal/NetShopBuy/NetShopUpdate recvMessage 逐字段对齐):
#   商品项16字段 = [id,page,goodstype,[[type,id,count]],goodscount,limittype,limitcoun,
#                  pricetype,price,originalprice,image,tittle,desc,limittime,isbuy,isdouble]
#   (goodstype/type 均为 REWARD_NEWTYPE: 1装备 2道具 3铜钱 4妖怪 5元灵 7妖魂 8唤妖符 9体力 10周月卡 98礼包;
#    客户端 image 直接拼 .png 加载; tittle/desc 优先本地 SDShop 静态(同 id))
#   终身限购(LimitType=1)的剩余次数/isbuy 按 EQ_STATE['shop_buy'] 持久化计算
_SHOP_ROWS = {}      # id -> 原始行 dict (含 _image/_tittle/_desc; 购买时取 GoodsType/GoodsId/GoodsCount)
try:
    _gs = _load_json('G02_Shop.json'); _gh = {h: k for k, h in enumerate(_gs[0])}
    for _r in _gs[1:]:
        try:
            _x = {
                'ID': int(_r[_gh['ID']]), 'Page': int(_r[_gh['Page']] or 0),
                'GoodsType': int(_r[_gh['GoodsType']] or 0), 'GoodsId': int(_r[_gh['GoodsId']] or 0),
                'GoodsCount': int(_r[_gh['GoodsCount']] or 1), 'LimitType': int(_r[_gh['LimitType']] or 0),
                'LimitTime': int(_r[_gh['LimitTime']] or 0), 'LimitCoun': int(_r[_gh['LimitCoun']] or 1),
                'PriceType': int(_r[_gh['PriceType']] or 1), 'Price': int(_r[_gh['Price']] or 0),
                'OriginalPrice': int(_r[_gh['OriginalPrice']] or 0),
            }
            _im = str(_r[_gh['Image']] or '0')
            _tt = str(_r[_gh['Tittle']] or '')
            _ds = str(_r[_gh['Desc']] or '')
            _x['_image'], _x['_tittle'], _x['_desc'] = _im, _tt, _ds
            # ★2026-10-16（任务书 A2）: `StepPrice` = 阶梯价（`SDShop.lua:49 阶梯价格`）。
            #   表里是 `60|120|300|600` 这种串；单值行（`60`）也照单收，非阶梯商品不会读它。
            #   ⚠ 这里不能用文件后面那个 `_i()`（本段在它定义之前执行）—— 裸 try/except。
            _steps = []
            for _sx in str(_r[_gh.get('StepPrice', _gh['Price'])] or '').split('|'):
                try:
                    _steps.append(int(_sx))
                except (TypeError, ValueError):
                    continue
            _x['_steps'] = _steps
            _SHOP_ROWS[_x['ID']] = _x
        except Exception:
            continue
except Exception:
    pass

# ---- 2026-11-07 全付费回滚: 恢复 G02_Shop 原价（用户裁定：回滚到初始、但全部不可购买） ----
#   删掉 2026-09-07 的两段"RMB -> 妖魂"改价（601/602 周月卡、603-608 妖魂礼包、101 首充、
#   650-668 礼包、20201-20206 等 34 行）。现在这些行保持表里的 PriceType=1（人民币）与原价；
#   购买入口由 `handle_shop_buy` 在限购校验之前统一回 `[77]`「暂未开放」，5006 也回 `[77]`。
#   妖魂(2)/金币(3)/妖豆(4)/鬼道积分(5) 的商品不受影响。

# ============================================================================
# ★2026-10-16（任务书 A2）：限购周期键 / 阶梯价 / 体力上限
# ----------------------------------------------------------------------------
# 修前：`5002` 只实现 `LimitType==1`（终身），其余（日/周/月/定时/日递增）**一律不追踪**
#   ⇒ 每日限购 1 次的礼包可以无限买。而限购枚举与刷新点客户端写得清清楚楚：
#     · the client shop config
#         0 No / 1 Life / 2 Mon(每月) / 3 Schedule(定时) / 4 Day / 5 Week
#         / 6 DaySeq(周续) / 7 WeekSeq(月续) / 8 DayInc(日递增)
#     · `ShopConfig.lua:182` "每周限购1次，**每周一06:00**刷新购买限制"
#     · `ShopConfig.lua:186` 体力购买规则：本日第 1/2/3/4 次 = 12/30/60/120 妖魂，第 4 次后封顶
#       （本服 `G02_Shop.StepPrice` 那一行是 60/120/300/600，**以表为准**）
# 存储：周期计数进**新键** `EQ_STATE['shop_period'][<sid>][<周期键>]`，`shop_buy[<sid>]` 继续当
#   "总购买次数"照旧累加 —— 这样老存档的终身限购（`shop_buy`）与老断言都不用迁移。
# ⚠ `LimitType=9`：表里有 5 行（page2 零食），**客户端枚举只到 8**，属无源。处置 = 与 8 同键
#   （日递增阶梯价 + `LimitCoun` 封顶），并在 `_SHOP_LIMIT_UNSOURCED` 里登记。
# ★2026-10-16（复核）：`LimitType` 在本服表里的实际分布是
#   0:40 / 1:22 / 2:15 / 4:83 / 5:12 / 6:1 / 7:1 / 8:1 / **9:5**，**没有 3（定时）**；
#   且 `G02_Shop.LimitTime` **每一行都是 0** ⇒ 下面 `lt == 3` 那个 'S' 分支在当前数据下
#   **不可达**，`LimitTime` 窗口也不需要判。等哪天真出现 `LimitType=3` 的行，先补窗口判据
#   再放开（`LimitTime` 的语义尚未取证），别直接让它按"终身一次"卖。
# ============================================================================
VITALITY_MAX = 20          # ⚠ 与 state.VITALITY_MAX 必须同值（那里是唯一出处；这里保留别名避免
                           #    本文件既有 20+ 引用点全改）。新增代码请直接 import state.VITALITY_MAX。
_SHOP_LIMIT_UNSOURCED = (9,)   # 无源待拍板：枚举只到 8（见上）


def _shop_now(now=None):
    try:
        return float(now) if now is not None else time.time()
    except (TypeError, ValueError):
        return time.time()


def _shop_period_key(lt, now=None):
    """`LimitType` -> 周期键；`None` = 不限购。

    周期键的形状写进存档，所以**不能随手改**（改了等于所有人重置限购）：
      `L`（终身）/ `D:YYYY-MM-DD`（日）/ `W:YYYY-WW`（ISO 周，周一起算）
      / `M:YYYY-MM`（月）/ `S`（定时，靠 `LimitTime` 窗口另判）
    """
    try:
        lt = int(lt)
    except (TypeError, ValueError):
        return None
    if lt == 0:
        return None
    if lt == 1:
        return 'L'
    t = time.localtime(_shop_now(now))
    if lt in (2, 7):
        return 'M:%04d-%02d' % (t.tm_year, t.tm_mon)
    if lt in (5, 6):
        # 周刷新点 = **周一 06:00**（`ShopConfig.lua:182`）：把时刻回退 6 小时再取 ISO 周，
        # 于是"周一 05:59"仍属上一周、"周一 06:00"才进新周。
        import datetime as _dt
        d = _dt.date(*time.localtime(_shop_now(now) - 6 * 3600)[:3])
        y, w, _ = d.isocalendar()
        return 'W:%04d-%02d' % (y, w)
    if lt == 3:
        return 'S'
    if lt in (4, 8) or lt in _SHOP_LIMIT_UNSOURCED:
        return 'D:%04d-%02d-%02d' % (t.tm_year, t.tm_mon, t.tm_mday)
    return None


def _shop_bought(sid, now=None):
    """该商品在**当前周期**已买次数（终身读老键 `shop_buy`，其余读 `shop_period`）。"""
    x = _SHOP_ROWS.get(int(sid)) if str(sid).lstrip('-').isdigit() else None
    if not x:
        return 0
    lt = x['LimitType']
    if lt == 0:
        return 0
    if lt == 1:
        try:
            return int((EQ_STATE.get('shop_buy') or {}).get(str(sid), 0) or 0)
        except (TypeError, ValueError):
            return 0
    key = _shop_period_key(lt, now)
    if key is None:
        return 0
    book = (EQ_STATE.get('shop_period') or {}).get(str(sid)) or {}
    if not isinstance(book, dict):
        return 0
    try:
        return int(book.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _shop_bump(sid, now=None):
    """记一次购买：总次数（`shop_buy`，老口径）+ 周期计数（`shop_period`）。"""
    sid_s = str(sid)
    EQ_STATE.setdefault('shop_buy', {})
    try:
        EQ_STATE['shop_buy'][sid_s] = int(EQ_STATE['shop_buy'].get(sid_s, 0) or 0) + 1
    except (TypeError, ValueError):
        EQ_STATE['shop_buy'][sid_s] = 1
    x = _SHOP_ROWS.get(int(sid)) if str(sid).lstrip('-').isdigit() else None
    key = _shop_period_key(x['LimitType'], now) if x else None
    if key is None:
        return
    per = EQ_STATE.setdefault('shop_period', {})
    if not isinstance(per, dict):
        per = EQ_STATE['shop_period'] = {}
    book = per.get(sid_s)
    if not isinstance(book, dict):
        book = per[sid_s] = {}
    try:
        book[key] = int(book.get(key, 0) or 0) + 1
    except (TypeError, ValueError):
        book[key] = 1


def _shop_price(x, now=None):
    """该商品**此刻**的应付价（货币由 `PriceType` 决定）。

    `LimitType=8/9`（日递增）按 `G02_Shop.StepPrice` 取第 n 档（n = 本周期已买次数），
    超出阶梯长度就封顶在最后一档 —— 与 `ShopConfig.lua:186` 的"第 4 次后每次都是 120"同口径。
    """
    try:
        lt = int(x['LimitType'])
    except (TypeError, ValueError):
        lt = 0
    if lt in (8, 9) or lt in _SHOP_LIMIT_UNSOURCED:
        steps = x.get('_steps') or []
        if steps:
            n = _shop_bought(x['ID'], now)
            return int(steps[min(max(0, n), len(steps) - 1)])
    return int(x['Price'])


def _shop_limit_error(x, now=None):
    """限购没通过就返回**应答字节**（`[码]`），通过返回 `None`。

    错误码逐个取自客户端码表 `app/network/shop/NetShopBuy.lua:6-28`：
      52 已达到购买次数 / 53 包周礼包还未过期 / 56 包月礼包还未过期
      57 体力购买今日已达上限 / 59 每周只能买一次 / 68 已经达到每月限购次数
      69 每天只能买一次 / 77 暂未开放
    """
    try:
        lt = int(x['LimitType'])
        lc = int(x['LimitCoun'])
    except (TypeError, ValueError):
        return None
    if lt == 0:
        return None
    bought = _shop_bought(x['ID'], now)
    if lt == 3:
        # 定时限购：`LimitTime` 是窗口终点（>0 才判）。表内 0 行，此分支只为完整性。
        if int(x['LimitTime'] or 0) > 0 and _shop_now(now) > int(x['LimitTime']):
            return b'[77]'
    if bought < lc:
        return None
    if lt in (8,) or lt in _SHOP_LIMIT_UNSOURCED:
        return b'[57]' if int(x['GoodsType']) == 9 else b'[52]'
    return {1: b'[52]', 2: b'[68]',
            4: (b'[69]' if lc == 1 else b'[52]'),
            5: (b'[59]' if lc == 1 else b'[52]'),
            6: b'[53]', 7: b'[56]'}.get(lt, b'[52]')


def _shop_item(_x):
    # 构造16字段商品项; 周期限购商品的剩余/已购标记与价格都按**当前周期**算
    #   ★2026-10-16: 修前只有 `LimitType==1`（终身）算剩余，其余恒发 `LimitCoun`
    #     ⇒ 每日/每周/每月限购在界面上永远显示"还能买 N 次"（服务端却该拒）。
    _id, _pg, _gt, _gid, _gc = _x['ID'], _x['Page'], _x['GoodsType'], _x['GoodsId'], _x['GoodsCount']
    _lt, _lc = _x['LimitType'], _x['LimitCoun']
    if _lt == 0:
        _rest, _isbuy = _lc, 0
    else:
        _b = _shop_bought(_id)
        _rest = max(0, _lc - _b)
        _isbuy = 1 if _b >= _lc else 0
    _price = _shop_price(_x)
    # ---- 2026-09-07(第二轮) 礼包类商品 goodslist = 真实内容物品 (客户端购买确认框渲染: IconData 无 type10 分支,
    #     发 [10,gid,1] 会空白; 改为 G03/L10 内容物品列表, 每个物品按类型走对应静态表图标) ----
    _gl = [[_gt, _gid, _gc]]
    if _gt in (10, 98):
        from handlers_bag import _GIFTS_FIXED, _GIFTS_ROLL, _GIFT_CONTENTS, _GIFT_EXTRA
        _items = []
        for c in _GIFTS_FIXED.get(str(_gid), []):
            _items.append([c[0], int(c[1]) if str(c[1]).isdigit() else 0, c[2]])
        for c in _GIFTS_ROLL.get(str(_gid), []):
            _items.append([c[0], int(c[1]) if str(c[1]).isdigit() else 0, c[2]])
        for c in _GIFT_EXTRA.get(str(_gid), []):
            _items.append([c[0], int(c[1]) if str(c[1]).isdigit() else 0, c[2]])
        if not _items:
            for c in _GIFT_CONTENTS.get(str(_gid), []):
                _items.append([c[0], int(c[1]) if str(c[1]).isdigit() else 0, c[2]])
        if _items:
            _gl = _items
    return [_id, _pg, _gt, _gl, _gc, _lt, _rest, _x['PriceType'],
            _price, _x['OriginalPrice'], str(_x['_image']), _x['_tittle'], _x['_desc'],
            _x['LimitTime'], _isbuy, 0]

def _grant_reward(x):
    # 按 REWARD_NEWTYPE 发放奖励 (x=G02_Shop 原始行 dict) -> (dropgroups, True) 或 (None, False 未支持)
    gt, gid, gc = x['GoodsType'], x['GoodsId'], x['GoodsCount']
    if gt == 2:                                     # 道具
        uc = str(gid)
        EQ_STATE['prop'][uc] = EQ_STATE['prop'].get(uc, 0) + gc
        EQ_STATE['prop_id'][uc] = gid
        return [[2, [[uc, gid, gc]]]], True
    if gt == 3:                                     # 铜钱
        EQ_STATE['gold'] += gc
        return [[3, [[gc]]]], True
    if gt == 7:                                     # 妖魂
        EQ_STATE['soulcoin'] += gc
        return [[7, [[gc]]]], True
    if gt == 8:                                     # 唤妖符
        EQ_STATE.setdefault('draw', {})['ticket'] = EQ_STATE['draw'].get('ticket', 0) + gc
        return [[8, [[gc]]]], True
    if gt == 9:                                     # 体力
        # ★2026-10-16（任务书 A2）: 修前无条件 `min(20, cur + gc)` 但**回包恒报 gc**
        #   ⇒ 体力 18 时买 20 点，界面飘 +20 而服务端只加了 2（"飘字与入账不一致"）。
        #   现在只报**真实入账量**；一点都进不去就不算发放成功（调用方按失败处理，不扣钱）。
        cur = int(EQ_STATE.setdefault('prop', {}).get('1006', 0) or 0)
        got = max(0, min(VITALITY_MAX, cur + gc) - cur)
        if got <= 0:
            return None, False
        EQ_STATE['prop']['1006'] = cur + got
        return [[9, [[got]]]], True
    if gt in (10, 98):                              # 周/月卡(10, 背包type=97) 与 礼包(98)
        if gid in (6011, 6021):                     # 周卡/月卡 -> 背包卡(97)
            # ★v39 修"同一物品不堆叠": 旧实现自己发号再 append, 绕过 state.add_giftbag
            #   的合并 -> 重复买同一张卡会在背包里开出第二格。统一走入包原语。
            from state import add_giftbag as _addgb
            uc, _c = _addgb(97, gid, gc)
            return [[98, [[uc, gid, _c]]]], True
        if x.get('ID') in (210, 315, 411):          # 传说强化魂石 (L02 2005; G02#210/315/411)
            #   ★ 2026-10-11 补 210：**这是 `_giftfix_test` 一直红着的真因**（它从未进网, 没人发现）。
            #     原写法把商店 id 写死成 `(315, 411)`，而 **shop 210「进化道具-仙灵」的
            #     `GoodsId` 也是 2005** ⇒ 它掉进了下面的"按礼包发"分支，
            #     `_GIFTS_FIXED['2005']` 是一组**别的**道具（3013/3015/3014/3016 各十几片）
            #     —— 玩家花 1200 妖魂买"传说强化魂石"，拿到的是 4 样不相干的碎片。
            #     口径依据：`GoodsId` 在 **G03 礼包组命名空间**与 **L02 道具命名空间**里会撞号，
            #     而 L02[2005] 的 `SceneName` 就是「传说强化魂石」（`Type=44 / ConsumablesValue=2`），
            #     与商品文案「进化道具-仙灵」同指一类强化魂石 ⇒ 按 L02 直发道具。
            #   ⚠ 判据是"实现里原本的意图 + L02 真表"，不是"我看着哪个顺眼"。
            #     同表里还有 208/209/601/602 四条同样撞号，**本轮不动它们**
            #     （601/602 上面已按周/月卡处理；208/209 的商品名与 L02 名对不上，
            #      要改得先拿到"客户端把这四行渲染成什么"的证据，见 AUDIT-backlog）。
            uc = '2005'
            EQ_STATE['prop'][uc] = EQ_STATE['prop'].get(uc, 0) + gc
            EQ_STATE['prop_id'][uc] = 2005
            return [[2, [[uc, 2005, gc]]]], True
        # 其他礼包/卡包 (2002/2004/2044/2049/2050/2051/8000-8028...): 直接按 L10 内容发奖
        #   (2026-09-07: 原实现只发"背包卡"g 条目 -> 客户端显示"空礼包"; G02 文案即内容)
        from handlers_bag import _apply_gift_rewards
        _di = _apply_gift_rewards(str(gid))
        if _di != '[]':
            return list(json.loads(_di)[1]), True
        return None, False
    if gt == 5:                                     # 元灵 (★v51: 完整元灵一格一个)
        from state import add_ghost_rows as _addgh
        return [[5, _addgh(gid, gc, 0)]], True
    if gt == 1:                                     # 装备 (加入装备背包)
        ucnt = max([int(u) for u in EQ_STATE['equip_id']] + [0]) + 1
        uc = str(ucnt)
        EQ_STATE['equip_id'][uc] = gid; EQ_STATE['lv'][uc] = 1
        try:
            from state import note_manual_unlock as _nmu
            _nmu(gid)                       # 装备图鉴: 拾取即解锁 (1072/1073)
        except Exception:
            pass
        # ★2026-10-16（E / CT=20）：极品装备（Class>=4 = 紫/红）计数 —— 唯一实现见
        #   `handlers_bag.note_equip_gained`（三处造装备的地方都调它）。
        try:
            from handlers_bag import note_equip_gained as _neg
            _neg(gid)
        except Exception:
            pass
        EQ_STATE['exp'][uc] = 0; EQ_STATE['equip_isload'][uc] = 0
        return [[1, [_equip_item(uc)]]], True
    if gt == 4:                                     # 妖怪 (获得怪物本体)
        # ★★2026-10-16（B 复核 2）：造怪收口到 `handlers_bag.grant_monsters`
        #   —— 它负责**背包上限 21** 与"放不下的转邮件"。修前这里既不看上限、
        #   也**忽略 `GoodsCount`**（一次只发 1 只）。现在按 `gc` 逐只发。
        from handlers_bag import grant_monsters as _gm
        rows, mailed = _gm(gid, gc)
        if not rows and not mailed:
            return None, False                      # 一只都没出去 -> 让调用方退款
        if not rows:
            return [], True                         # 全部转邮件：回空掉落组（信封里有）
        return [[4, rows]], True
    return None, False

def handle_shop_total(body_json=None):
    # req {page, extraInfo}; resp [1]=page(echo), [2]=time, [3]=goodsInfo(该页全部商品)
    req = {}
    if body_json:
        try:
            req = json.loads(body_json)
        except Exception:
            req = {}
    if not isinstance(req, dict):     # ★2026-10-11: `(req or {})` 挡不住 list/str（见 _body.py 注释）
        req = {}
    try:
        page = int(req.get('page') or 0)
    except Exception:
        page = 0
    goods = [_shop_item(x) for x in _SHOP_ROWS.values() if x['Page'] == page]
    return _simple_resp(page, 0, goods).encode('utf-8')

def handle_shop_buy(body_json):
    # req {shopid}; resp [1]=roleInfo, [2]=dropInfo, [3]=num(>0=背包满)
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    sid = int(req.get('shopid') or 0)
    g = _SHOP_ROWS.get(sid)
    if g is None:
        # ★2026-09-12: 客户端 NetShopBuy.m_errorCode[34] = "物品不存在"。
        #   旧实现给 [0,{}] -> 弹"获取网络信息失败"(误导)。
        return b'[34]'
    # ★2026-11-07（全付费回滚）：RMB(PriceType=1) 商品一律**在限购校验之前**回「暂未开放」。
    #   修前限购先判，已买过的 RMB 行会先回 [52]/[69] 之类，玩家看到的不是"这货买不了"。
    #   现在与 5006 同口径：[77] = NetShopBuy.m_errorCode 的「暂未开放」。
    if g['PriceType'] == 1:
        return b'[77]'
    # ★2026-10-16（任务书 A2）①：限购按 `LimitType` 落**周期键**（终身/日/周/月/定时/日递增）。
    #   修前只判终身（`LimitType==1`），每日限购的礼包可以无限买。
    _lim = _shop_limit_error(g)
    if _lim is not None:
        return _lim
    # ★2026-10-16（任务书 A2）②：体力已满 -> 退款前就拒（客户端码表 64 = "体力值达到上限,不能购买体力"）。
    #   修前走到 `_grant_reward` 里 `min(20, …)` 后**照样扣钱**、回包还报满额。
    if g['GoodsType'] == 9 and _vitality_count() >= VITALITY_MAX:
        return b'[64]'
    # 扣费 (按 PriceType) —— ★v43 补 5=鬼道积分(积分商城 Page 7)。
    #   权威口径 = 客户端 `app/shop/ShopConfig.lua:56 sh.MoneyType`:
    #     1=RMBCoin(人民币) 2=Token(妖魂) 3=GoldCoin(金币) 4=EggCoin(妖豆)
    #     **5=PointCoin(鬼道积分)** 6=个人妖盟财富 7=夜市信物 8=天罡八卦 9=地煞八卦
    #   用户报的 bug: 商城「积分」页(Page=7, 8 件商品 PriceType=5, 价 11000/12000) 点购买
    #   弹「获取数据失败」= NetShopBuy.m_errorCode[0]。根因就是这里 `else: return [0,{}]`
    #   —— 5 落进了"未实现"分支。
    #   5 的货币是 `EQ_STATE['kidou']`: 客户端 `isMoneyEnough(PointCoin)` 走
    #   `gamer_getIntegral()` -> `SQGamer.lua:202 roleInfo[14]` -> 服务端
    #   `state.role_info_array()[13] = kidou` (1-based 14)。三条链已对齐。
    #   不足时的错误码: NetShopBuy 的 m_errorCode 里**没有**"积分不足"这一条
    #   (60/61/62 分别是妖魂/铜钱/妖精蛋)，所以沿用本工程对同一货币的既有约定
    #   (`handlers_lottery.py:286` 鬼道积分不足 -> `[0,{}]`)；客户端本身会先
    #   用 `sh.popNoEnoughMoneyTip` 拦住，这条只是服务端兜底。
    pt = g['PriceType']
    # ★2026-10-16（任务书 A2）③：未实现的货币在**扣费之前**拒绝（77 = "暂未开放"）。
    #   修前落 `[0,{}]`（"获取数据失败"），玩家看不出是"这货不支持"还是"服务器坏了"。
    if pt not in (2, 3, 4, 5):
        return b'[77]'                        # RMB(1)/妖盟(6)/夜市(7)/八卦(8,9)/皮肤商城(10)
    # ★日递增（LimitType=8/9）的价格是**逐次变化**的，所以价必须在扣费这一刻现算。
    price = _shop_price(g)
    if pt == 2:
        if EQ_STATE['soulcoin'] < price: return b'[60]'
        EQ_STATE['soulcoin'] -= price
    elif pt == 3:
        if EQ_STATE['gold'] < price: return b'[61]'
        EQ_STATE['gold'] -= price
    elif pt == 4:
        if EQ_STATE['eggcoin'] < price: return b'[62]'
        EQ_STATE['eggcoin'] -= price
    elif pt == 5:                             # ★v43 鬼道积分 (积分商城)
        if int(EQ_STATE.get('kidou', 0)) < price: return b'[0,{}]'
        EQ_STATE['kidou'] = int(EQ_STATE.get('kidou', 0)) - price
    else:
        # ★2026-10-16: 上面已按白名单拒绝过（`[77]`），这条只是纵深防御。
        return b'[77]'
    # 发放奖励
    dropgroups, ok = _grant_reward(g)
    if not ok:
        # ★★v53 (2026-09-13): 发放失败必须**退还**已扣的货币。
        #   旧实现直接 `return [0,{}]` —— 钱已经扣了、货一件没给, 玩家看到的是"获取数据失败"。
        #   (触发场景: 礼包抽到的内容本次全部无法入账 / 商品类型服务端没接。)
        #   结构上兜住: 任何"发放失败"的路径都不再能吃玩家的钱。
        try:
            if pt == 2:
                EQ_STATE['soulcoin'] += price
            elif pt == 3:
                EQ_STATE['gold'] += price
            elif pt == 4:
                EQ_STATE['eggcoin'] += price
            elif pt == 5:
                EQ_STATE['kidou'] = int(EQ_STATE.get('kidou', 0)) + price
            save_state()
        except Exception as _e:
            print('[SHOP] !! 退款失败 shopid=%s pt=%s price=%s err=%r' % (sid, pt, price, _e), flush=True)
        print('[SHOP] !! 发放失败已退还 shopid=%s pt=%s price=%s (客户端会弹"获取数据失败")'
              % (sid, pt, price), flush=True)
        return b'[0,{}]'
    # 记录购买次数：总次数（老口径 `shop_buy`）+ 周期计数（`shop_period`，限购的真值）
    _shop_bump(sid)
    if sid == 601:
        bump_task(CT_WEEKCARD, 1)      # 每日任务: 周卡
    elif sid == 602:
        bump_task(CT_MONTHCARD, 1)     # 每日任务: 月卡
    save_state()
    return (_simple_resp(_role_info(), _drop(dropgroups), 0)).encode('utf-8')

def handle_shop_update(body_json=None):
    # resp [1]=soulcoin, [2]=time, [3]=goodList (全量; 客户端 NetShopUpdate 不做页过滤)
    goods = [_shop_item(x) for x in _SHOP_ROWS.values()]
    return _simple_resp(EQ_STATE['soulcoin'], 0, goods).encode('utf-8')

# ============ 战斗结算 9001/9003-9007 (2026-09-06 按 Net*Result 统一契约重写) ============
# 客户端源码核实: 胜 result==0 (warFinishSuccessResultRequest), 败==1; ourside=[{ucode,exp}]
#   resp [1,[roleInfo, ourFormation(30字段更新后怪物items), dropInfo]] (9004 塔追加 msg[4]=talentId 可省)
from handlers_cult import _MONSTER_EXP as _WAR_EXP

# ★2026-10-16（任务书 D⑤）：单场战斗给单只妖怪的经验上限 = 1 级升到满级的总经验
#   （`L100_MonsterExp` 各级阈值之和）。超过它就不是"打了一场"能解释的数量。
try:
    _MON_EXP_CAP = int(sum(int(v) for v in (_WAR_EXP or {}).values())) or 10 ** 7
except Exception:
    _MON_EXP_CAP = 10 ** 7

# ---- 技能学习-合成链 残章掉落池 (L05_ScrowPiece 全部材料, 官方残章= L02 Type33 300002-300013) ----
_SCROW_MATS = []
try:
    _t5 = _load_json('L05_ScrowPiece.json')
    _h5 = {c: k for k, c in enumerate(_t5[0])}
    _mats = set()
    for _r in _t5[1:]:
        for _i in (1, 2, 3):
            try:
                _mats.add(str(_r[_h5['MatrielID%d' % _i]]))
            except Exception:
                continue
    _SCROW_MATS = sorted(_mats)
except Exception:
    pass

def _grant_scrow(n=1):
    # 发放 n 个随机残章 (L02 Type33; 道具背包 ucode=静态id) -> drop 片段
    if not _SCROW_MATS:
        return None
    sid = random.choice(_SCROW_MATS)
    uc = str(sid)
    EQ_STATE['prop'][uc] = EQ_STATE['prop'].get(uc, 0) + n
    EQ_STATE['prop_id'][uc] = int(sid)
    return [2, [[uc, int(sid), n]]]

def _gain_monster_exp(uc, addexp):
    # 怪物经验结算 (用 L100_MonsterExp 升级, 对标吞噬)
    #   ★2026-10-16（任务书 D⑤）：负值/超大值一律**拒绝**（不是夹到边界，而是不给）。
    #     上限口径有表源：从 1 级升到满级所需经验总和 = `sum(L100_MonsterExp 的各级阈值)`
    #     —— 一场战斗的 exp 超过这个数在官方数据里不可能出现（那已经不是"打了一场"）。
    if not addexp or addexp <= 0:
        return
    if addexp > _MON_EXP_CAP:
        print('[WAR_EXP] !! 拒绝超大 addexp=%r (上限 %d = 1→满级总经验) ucode=%s'
              % (addexp, _MON_EXP_CAP, uc), flush=True)
        return
    base = get_monster_base(uc)
    if base is None:
        return
    lv = get_monster_lv(uc)
    exp = get_monster_exp(uc) + int(addexp)
    max_lv = max(_WAR_EXP.keys()) if _WAR_EXP else 200
    while lv < max_lv and exp >= _WAR_EXP.get(lv, 99999999):
        exp -= _WAR_EXP[lv]
        lv += 1
    EQ_STATE['monster_lv'][uc] = lv
    EQ_STATE['monster_exp'][uc] = exp

# ============ ★v54c (2026-09-15) 官方掉落组解析 (T01_MstrGroup.BattleReward) ============
#   缺口: handle_war_result (9001/9003/9005/9006/9007 共用) 对**所有**战斗一律发死值 500金/50魂,
#     而"这场仗掉什么"一直就写在 T01_MstrGroup.BattleReward 列里 (729 个非空值 / 241 个是合法 T02 组)。
#
#   真机实证 (work/_v54b_zzrc4_verify.py; 2026-09-15 00:42~00:44 用户实打探索+妖塔):
#     9003 探索 info=1011 <怒目鬼> -> BattleReward=1106 ; 客户端加密字段 value 解出 **1106** ← 两条独立来源互证
#     9003 探索 info=1012 <林王仙> -> BattleReward=1107 ; value 解出 **1107**
#     9004 妖塔 value 解出 1001 (S51 ID) -> MonsterGroupID 20001 -> BattleReward 20001
#     同一批样本的 sign 解出合法 JSON 且各字段与同包明文逐项一致 (RC4 实现正确性铁证)
#
#   ⚠ **必须按"来源"取数, 不能靠 membership 猜**: 20001 同时在 T01_MstrGroup 与 T02_Reward 里,
#      单表 membership 过关在这类值上给不出任何信息 (v54b 铁律 #175)。
_T01_FULL = {}
try:
    _t = _load_json('T01_MstrGroup.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _T01_FULL[int(_r[0])] = {c: _r[_h[c]] for c in _h}
        except Exception:
            continue
except Exception:
    pass

def t01_battle_reward(_gid):
    # T01_MstrGroup 行 -> 该战斗组的官方奖励组 (T02_Reward.GroupID); 空/0/取不到 -> None
    try:
        v = (_T01_FULL.get(int(_gid)) or {}).get('BattleReward')
        return int(v) if v not in (None, '', '0') else None
    except Exception:
        return None

# ★★2026-10-18（全项目同类扫描）：`ObjectPosition -> T01 行` 反查索引。
#   9007（剧情结算）客户端送的是 `m_saveObjectPosition`（`WarStageUnits.lua:841`），
#   而它**没有 info** —— 只能靠"ObjectPosition 反查唯一 T01 行"还原战斗组。
#   表内 768 个 ObjectPosition 值里 762 个唯一；剩下 6 个重复的返回 None（宁可不发也不猜）。
_T01_BY_OP = {}
try:
    for _gid, _row in _T01_FULL.items():
        try:
            _op = int(_row.get('ObjectPosition') or 0)
        except (TypeError, ValueError):
            continue
        if _op > 0:
            _T01_BY_OP.setdefault(_op, []).append(_gid)
except Exception:
    pass


def _t01_by_object_position(op):
    """ObjectPosition -> 唯一 T01 行 id；0/表外/重复 -> None（不猜）。"""
    try:
        rows = _T01_BY_OP.get(int(op)) or []
    except (TypeError, ValueError):
        return None
    return rows[0] if len(rows) == 1 else None

_S51_TOWER_MG = {}
try:
    _t = _load_json('S51_Tower.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _S51_TOWER_MG[int(_r[0])] = int(_r[_h['MonsterGroupID']])
        except Exception:
            continue
except Exception:
    pass

# ★v54e (2026-09-15): 私服倍率。**默认 1 = 完全按官方原值, 不放大** (用户拍板"直接完全还原官方")。
#   机制保留只为将来想调时不必改代码: set YGZZ_REWARD_MULT=5 (改了要重启服务端)。
#   只作用于**可堆叠资源**(见 handlers_task._MULT_TYPES), 装备/妖怪/皮肤等"件"永远按官方 1 份。
try:
    REWARD_MULT = max(1, int(os.environ.get('YGZZ_REWARD_MULT') or 1))
except Exception:
    REWARD_MULT = 1

# S03_Spawn (探索罐子/宝箱): 行 id -> 官方奖励组 (T02_Reward.GroupID)
#   客户端 ModelBox.lua:255  sendMessage(MSG_ID.EXPLORE_DROP, exploreFirstId, staticId, unlockList, dropFunc)
#     -> NetExploreDrop.lua:24/32  sendMessage(_gateid,_sceneid,..) ; msg["sceneid"] = _sceneid = staticId
#     -> S03_Spawn.lua:26  usGetTableData(tS03_Spawn.tName, _staticID)   (按行 id 查)
_S03_SPAWN_REWARD = {}
_S03_SPAWN_TYPE = {}      # 行 id -> S03_Spawn.Type (1=罐子/莲蓬, 2/3/4=宝箱 普通/封印/神秘)
_S03_SPAWN_SCENE = {}     # 行 id -> S03_Spawn.SceneID (**S02_Scene.ID**，不是大关卡 id)
_S03_SPAWN_SAVETYPE = {}  # 行 id -> S03_Spawn.SaveType (1=每局重置, 2=跨局保留)
try:
    _t = _load_json('S03_Spawn.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _v = int(_r[_h['RewardID']] or 0)
            if _v:
                _S03_SPAWN_REWARD[int(_r[0])] = _v
            _S03_SPAWN_TYPE[int(_r[0])] = int(_r[_h['Type']] or 0)
            _S03_SPAWN_SCENE[int(_r[0])] = int(_r[_h['SceneID']] or 0)
            _S03_SPAWN_SAVETYPE[int(_r[0])] = int(_r[_h['SaveType']] or 0)
        except Exception:
            continue
except Exception:
    pass

# ============================================================================
# ★2026-10-16（任务书 A1）：`S03_Spawn.SceneID` 是 **S02_Scene.ID（子场景）**，不是大关卡 id
# ----------------------------------------------------------------------------
# 修前 1704 把 `S03_Spawn.SceneID` 直接当"大关卡"用（`_S03_SPAWN_SCENE`），
# 更早还直接把请求里的 `exploreid`（= 解锁度 id 串 `"101&102"`，`NetExploreDrop.lua:43`）
# 当关卡。两个错都只在"子场景 id 恰好等于大关卡 id"的少数关卡上碰巧对（40001 就是）。
# 真链是：`S01_Level.ID` ←(S02_Scene.SceneID)— `S02_Scene.ID` ←(S03_Spawn.SceneID)— `S03_Spawn`。
# 证据：S02_Scene[50004]=(ID=50004, SceneID=50001)，而封印宝箱 8055 的 SceneID=50004。
# ============================================================================
_S02_SUB2GATE = {}        # S02_Scene.ID(子场景) -> S02_Scene.SceneID(大关卡)
try:
    _t2s = _load_json('S02_Scene.json'); _h2s = {c: k for k, c in enumerate(_t2s[0])}
    for _r in _t2s[1:]:
        try:
            _S02_SUB2GATE[int(_r[_h2s['ID']])] = int(_r[_h2s['SceneID']] or 0)
        except Exception:
            continue
except Exception:
    pass


def _spawn_gate(static_id):
    """`S03_Spawn` 行 -> 它所属的**大关卡 id**（S01_Level.ID）；解析不出来 -> 0。"""
    return _S02_SUB2GATE.get(_S03_SPAWN_SCENE.get(_i(static_id, 0), 0), 0)

# ============================================================================
# 探索解锁度（1705）的静态条件表 —— `S12_SceneExplore`
# ----------------------------------------------------------------------------
# 客户端 `exploreUnlockTool` 的判据链（逐行取自 1.31 明文）：
#   · 每行 = 一条"解锁度条件"：`{ID, SceneID(大关卡), Type, Value1, Value2, Explore}`
#   · 探索度百分比 = **满足条目的 `Explore` 之和**（`calcuExploreDegree()`:208-225）——
#     客户端只把 `Count >= Value2` 的行累加 `Explore`，所以服务端算的 `degree` 与它同源。
#   · `Count` 由客户端本地自增（开罐/开箱，`openPotOnce/openPotTotal/...`:356-545），
#     但**服务端每次 1705 都会整体覆盖**（`NetExploreDegree.lua:66-68`）
#     ⇒ 服务端不给的条目在客户端会被当成"没进度"。
#   · 一次性事件（怪物/宝箱是否已清）走 `scenedata` -> `SQExplore.OnlyEvent`
#     （`ExploreConfig.isHasOnlyEvent():308` 用它把对象标成 isDeath/isOpen）。
#
# ⚠ Type 语义（`exploreUnlockTool` 头部注释 + 各 openXxx 实现）：
#   1 首杀BOSS  2 N回合内杀BOSS  3 不遇怪杀BOSS  4 本次探索击杀小妖  5 累计击杀小妖
#   6 本次探索发现罐子  7 累计发现罐子  8 本次开启大宝箱  9 累计开启大宝箱
#   10 本次开启上锁宝箱 11 累计开启上锁宝箱
#   ⇒ 服务端能**真实**统计的是 4~11（都来自 1704 的开箱事件）与 2/3 的一部分；
#     1/2/3 需要"战斗结算带大关卡 id"，而探索战斗的 9003 请求体里**没有**大关卡 id
#     （只有加密的掉落组 id）⇒ 本版对 1/2/3 **给 0 并留债**，不编造（见 docs/HANDOFF）。
_XP_S12_BY_GATE = {}      # 大关卡 id -> [行 dict, ...]（按 ID 升序）
_XP_GATES = set()
try:
    _t = _load_json('S12_SceneExplore.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _row = {'id': int(_r[_h['ID']]), 'gate': int(_r[_h['SceneID']]),
                    'detail': str(_r[_h.get('Detail', _h['ID'])] or ''),
                    'type': int(_r[_h['Type']] or 0),
                    'value1': int(_r[_h['Value1']] or 0),
                    'value2': max(1, int(_r[_h['Value2']] or 1)),
                    'explore': int(_r[_h['Explore']] or 0)}
        except Exception:
            continue
        _XP_S12_BY_GATE.setdefault(_row['gate'], []).append(_row)
        _XP_GATES.add(_row['gate'])
    for _g in _XP_S12_BY_GATE:
        _XP_S12_BY_GATE[_g].sort(key=lambda r: r['id'])
except Exception:
    pass

# ★★2026-10-18（CT=3 口径 A，用户拍板）：每个大关卡的 **BOSS 战斗组集合**。
#   = `S12_SceneExplore` 里 `Type==1`（"首杀BOSS-…"）那些行的 `Value1`（T01 战斗组 id）。
#   证据：40001 的 Type=1 行是 1011/1012；客户端 `exploreUnlockTool:killMonsterVictory():728`
#   用 `fightId == Value1 && T01.Type == 2 && isVictory` 来推进"首杀BOSS"条件。
#   ⚠ 不能用"解锁度 100%"替代：打过一次之后，每次 9003（哪怕中途杀小怪）都报 100%
#   （真机 deploy.log 23:42:30 小怪 info=1002 仍报 100%），会把每次战斗都算通关。
_XP_BOSS_GROUPS = {}      # gate -> {T01 组 id}
try:
    for _g, _rows in _XP_S12_BY_GATE.items():
        _bs = set(int(r['value1']) for r in _rows
                  if int(r.get('type', 0) or 0) == 1 and int(r.get('value1', 0) or 0) > 0)
        if _bs:
            _XP_BOSS_GROUPS[int(_g)] = _bs
except Exception:
    pass

# ★★2026-10-18（CT=29，真机取证）：困难难度的**大关卡 id = `S00_WorldMap.EliteID`**。
#   真机实测（14:17）：进 墨色荷塘「困难」→ 1701 `id`=**160001**（= `S00[1006].EliteID`）；
#   同一局 9003 请求 `{"info":13001,"gateid":160001,...}` ⇒ 9003 的 `gateid` 就是 EliteID。
#   而 T37 1020~1026 七行的 `Value` 正是 140001/150001/…/2000001 这七个 EliteID
#   ⇒ 「9003 胜 + info ∈ 该关 S12 Type==1 BOSS 组 + gateid ∈ ELITE_GATES」= 困难难度通关。
#   （同一关的简单难度 gate 是 DefaultID，不在本集合里，不会误计。）
_ELITE_GATES = set()
try:
    _t = _load_json('S00_WorldMap.json')
    _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _e = int(_r[_h['EliteID']] or 0)      # ⚠ 此处 `_i()` 还没定义（本块在模块加载序更前）
            if _e > 0:
                _ELITE_GATES.add(_e)
        except Exception:
            continue
except Exception:
    pass

# 条件 Type -> 计数器键。**同一事件按"本次/累计"两套计数**（客户端也是两套）。
_XP_TYPE_KEY = {4: 'k4', 5: 'k5', 6: 'k6', 7: 'k7', 8: 'k8', 9: 'k9', 10: 'k10', 11: 'k11'}
# 进入关卡（1701）时归零的"本次探索"计数（客户端 `resertExploreDegree():134-164` 同款清单）
_XP_CUR_KEYS = ('k4', 'k6', 'k8', 'k10')
# 跨天归零的"当日累计"计数（k4/k6/k8/k10 是"本次"，见 `_XP_CUR_KEYS`）
_XP_DAY_KEYS = ('k5', 'k7', 'k9', 'k11')


def _i(v, d=0):
    """宽松转 int（本文件其余地方一律用裸 int()，探索这块的入参全是客户端 JSON）。"""
    try:
        return int(v)
    except Exception:
        return d


def _xp_today():
    return time.strftime('%Y-%m-%d')


def _xp_state():
    """当前账号的探索进度（**per-account**：解锁度是"我一个人探到哪了"）。

    结构：`{'date': 当日, 'run': 本次探索序号, 'gates': {<gate>: {'c': {计数}, 'open': {静态id: 1}}}}`
    · `c` 里的 `k4/k6/k8/k10` 是"本次探索"计数（进关卡归零），`k5/k7/k9/k11` 是当日累计；
    · `open` 记录**已清掉的一次性对象**（1705 的 `scenedata` 就由它生成）；
    · 跨天把 `k5/k7/k9/k11` 与 `open` 清掉（"累计"是每日重来的口径，与客户端一致）。
    """
    st = EQ_STATE.setdefault('explore_prog', {})
    if not isinstance(st, dict):
        st = EQ_STATE['explore_prog'] = {}
    today = _xp_today()
    if st.get('date') != today:
        # ★★2026-10-18（真机 bug）：跨天**只清"按日重来"的两样** —— 当日累计计数
        #   k5/k7/k9/k11 与一次性对象 `open`。修前这里是 `st.clear()`，把 `gates` 整棵
        #   连 `cond`（已完成条件）/`best`（历史峰值）/`box`（已领解锁度宝箱）一起抹掉
        #   ⇒ 真机现象：前一天已 100% 的关卡，零点后统计图变回 0%，三个门控宝箱又全部
        #   变成"可领/可预览"。与本函数 docstring 写的口径（只清 k5/k7/k9/k11 与 open）
        #   自相矛盾 —— 实现错了，不是设计。
        _gs = st.get('gates')
        if isinstance(_gs, dict):
            for _g in _gs.values():
                if not isinstance(_g, dict):
                    continue
                _c = _g.get('c')
                if isinstance(_c, dict):
                    for _k in _XP_DAY_KEYS:
                        _c.pop(_k, None)
                _g['open'] = {}
        st['date'] = today
    st.setdefault('run', 0)
    if not isinstance(st.get('gates'), dict):
        st['gates'] = {}
    return st


def _xp_gate(gate):
    st = _xp_state()
    g = st['gates'].get(str(int(gate)))
    if not isinstance(g, dict):
        g = st['gates'][str(int(gate))] = {}
    for k, v in (('c', {}), ('open', {}), ('cond', {}), ('box', {})):
        if not isinstance(g.get(k), dict):
            g[k] = v
    if 'battle_fought' not in g:
        g['battle_fought'] = 0
    return g


def _xp_count(gate, row):
    """这条条件当前的 Count（服务端按 S12 规则真值给；算不出来的给 0）。"""
    rid = _i(row.get('id'), 0)
    g = _xp_gate(gate)
    cond = g.get('cond') if isinstance(g.get('cond'), dict) else {}
    if str(rid) in cond:
        # 客户端在**战斗结算包里直接回了**这条条件已被满足（`msg["exploreid"]`），
        # 以它为准（比服务端自己推更权威：`killMonsterVictory()` 的判据包含
        # "T01.Type==2 BOSS"、"无小怪"、"回合数≤N" 这些服务端拿不到的量）。
        return max(1, _i(row.get('value2'), 1))
    key = _XP_TYPE_KEY.get(_i(row.get('type'), 0))
    if not key:
        # Type 1/2/3：判据在 T01.Type / 回合数 / 无小怪 —— 本版靠 9003 的 exploreid 覆盖，
        # 覆盖不到的（老包/其它入口）**给 0 并留债**，不编造。
        return 0
    v = _i(g['c'].get(key), 0)
    return max(0, min(v, _i(row.get('value2'), 1)))


def _xp_mark_cond(gate, rid):
    g = _xp_gate(gate)
    if not isinstance(g.get('cond'), dict):
        g['cond'] = {}
    g['cond'][str(_i(rid, 0))] = 1


def _xp_enter(gate):
    """1701 进入关卡：本次探索计数归零、run +1（客户端 `resertExploreDegree` 同款）。

    ⚠ **不清 `cond`**：Type 1（首杀 BOSS）与 Type 5/7/9/11（累计）都是跨次的，
      清了会让"打过一次的首杀 BOSS"回来。清理只发生在跨天（`_xp_state()`）。
    """
    st = _xp_state()
    st['run'] = _i(st.get('run'), 0) + 1
    g = _xp_gate(gate)
    for k in _XP_CUR_KEYS:
        g['c'][k] = 0
    g['battle_fought'] = 0
    # ★2026-10-11（A0 第 1 批）：复活次数也是"本次探索"的量，必须与 k4/k6/k8/k10 同批归零 ——
    #   漏了它 = 上一局把 3 次复活用光后，下一局 1708 直接回 times=0，**复活按钮再也不出现**。
    #   这条不是我推的：护栏 V9b（"再进原关卡 -> times 回到上限"）当场抓出来的。
    g['revive'] = 0
    # ★★2026-10-16（任务书 A1 ③）：`SaveType==1` 的一次性对象**按局重置**，`==2` 跨局保留。
    #   证据 = 客户端自己那一行（`app/explore/objects/ModelBox.lua:202`）：
    #       `if self.m_staticData:getStatic_SaveType() == 2 then
    #            EDataManager:…:explore_updateEventByUcode(…)   -- 只对 SaveType==2 记"已清"`
    #   ⇒ 客户端**只**把 SaveType==2 的对象记进 `SQExplore.OnlyEvent`，其余每局都当新的。
    #   服务端若把 `==1` 也永久保留，就会出现"客户端能点、服务端不发奖"的静默失配。
    #   表内分布（601 行）：Type=1 罐子 461 / Type=2 宝箱 95 / Type=4 神秘宝箱 33 都是 SaveType=1；
    #   只有 Type=3 封印宝箱 12 行是 SaveType=2。
    g['open'] = {k: v for k, v in (g.get('open') or {}).items()
                 if _S03_SPAWN_SAVETYPE.get(_i(k, 0), 0) == 2}
    return st['run']


def _xp_is_open(gate, static_id):
    """这个一次性对象在本局里是否**已经开过**（1704 的重放判据；比 `_xp_open` 只读）。"""
    return bool((_xp_gate(gate).get('open') or {}).get(str(_i(static_id, 0))))


def _xp_open(gate, static_id):
    """1704 开罐/开箱：累计对应"本次/累计"两套计数，并记下这个一次性对象已清。

    返回 `True` 表示**这次是新开的**（重复上报同一 staticid 不再计数 —— 否则客户端
    重发一次 1704 就能把"累计开罐 15 个"刷满）。
    """
    g = _xp_gate(gate)
    sid = str(_i(static_id, 0))
    if g['open'].get(sid):
        return False
    stype = _S03_SPAWN_TYPE.get(_i(static_id, 0), 0)
    # S03_Spawn.Type: 1 罐子/莲蓬 -> 条件 6/7；2 宝箱 -> 8/9；3 封印宝箱/4 神秘宝箱 -> 10/11
    pair = {1: ('k6', 'k7'), 2: ('k8', 'k9'), 3: ('k10', 'k11'), 4: ('k10', 'k11')}.get(stype)
    if pair:
        for k in pair:
            g['c'][k] = _i(g['c'].get(k), 0) + 1
    g['open'][sid] = 1
    return True


# ============================================================================
# ★2026-10-11（3500 NET_SHOW_REWARD）：本次探索"累计获得"清单
# ----------------------------------------------------------------------------
# 事故（真机 E1 验收发现，一直挂着未定因）：
#   探索界面退出/失败时客户端弹「本次探索累计获得」结算面板，而同一次里明明开了箱、
#   金币 +20，面板却写「很遗憾，本次探索没有获得任何物品」——UI 与落盘不一致。
# 根因（**不是**奖励没发）：客户端那行字是 `UIShowReward:showRewardList()` 在
#   `table.nums(_msgData) < 1` 时的兜底文案，而 `_msgData` **只来自 3500 的应答**：
#     app/public/UIHelper.lua:79-87  expAddRewardView() -> showRewardSendMessage()
#     app/explore/exploreUi/UIShowReward.lua:253-256  sendMessage(MSG_ID.NET_SHOW_REWARD, 大关卡id, cb)
#     app/network/ConfigNet.lua:436                    NET_SHOW_REWARD = 3500
#     app/network/explore/NetShowReward.lua:29-40      msg[1] = { {itemId,itemNum,itemType}, ... }
#   而服务端 `GAME_HANDLERS` **从来没有 3500** ⇒ `resp_for(3500)` 落到兜底 `b'[1,[[]]]'`
#   （`config_data.py:176-184`）⇒ `pairs(msg[1])` 零次 ⇒ 面板恒为空 ⇒ 恒打那行字。
# 口径（只做能证明的事）：
#   · 统计范围 = **本次探索 run**（1701 开始，1706 结束或 6 小时无活动自然过期）。
#     含 ①1704 开罐/开箱的真实掉落 ②本次 run 的 9003 战斗结算掉落（客户端文案是
#     "本次探索累计获得"，两者都算"这次跑图拿到的"）。
#   · **只用真实发放的 `dropgroups`**（`_drop` 的 `[style, goodslist]`）—— 不重新抽一次
#     T02 组、不推算、不编造（重抽会得到与玩家实际入账不同的清单）。
#   · `itemId/itemNum/itemType` 直接取服务端的 `style` 与物品段，与 `IconData.lua:19-49`
#     的 `REWARD_NEWTYPE` 同一套号（2 道具 / 3 铜钱 / 7 妖魂 / 8 唤妖符 / 17 经验 …）。
#     只登记这份表里能渲染的 kind；未知 kind **跳过并留痕**，绝不发一个客户端渲染不了的 id。
_XP_LEDGER_KINDS = (1, 2, 3, 4, 5, 6, 7, 8, 9, 14, 17, 18, 21, 22, 23, 98)
_XP_LEDGER_MAX = 200          # 单次探索的清单上限（防病态累积把应答撑大）
_XP_LEDGER_TTL = 6 * 3600     # 无 1701/1706 收尾时的兜底过期（秒）


def _xp_run_state():
    """本次探索 run 的账本：`handlers_game.py:1059-1068` 的 9003 路径也用它。"""
    st = _xp_state()
    r = st.get('run_ledger')
    if not isinstance(r, dict) or r.get('date') != st.get('date'):
        r = st['run_ledger'] = {'date': st.get('date'), 'gate': 0, 't': 0, 'items': []}
    if not isinstance(r.get('items'), list):
        r['items'] = []
    return r


def _xp_begin_run(gate):
    """1701 进关卡 = 新的一次探索：账本清空、记住本次的大关卡。"""
    r = _xp_run_state()
    r['gate'] = _i(gate, 0)
    r['t'] = int(time.time())
    r['items'] = []
    return r


def _xp_end_run():
    """1706 退出大关卡 = 本次探索结束（清单仍保留，供紧随其后的 3500 读）。"""
    r = _xp_run_state()
    r['t'] = int(time.time())
    return r


def _as_int(v, d=0):
    """宽松转 int：字符串数字也收（ucode 那类不能静默变 0）。转不动返回 d。"""
    try:
        if isinstance(v, bool) or v is None:
            return d
        return int(v)
    except (TypeError, ValueError):
        return d


def _unwrap_num(v, d=0):
    """从"被多包了一层"的值里取数量。

    ★实测形状（`handlers_task.py:269/298/325` 与 `handlers_sweep.py:199` 都是这么写的）：
      铜钱/妖魂/唤妖符/体力/活跃度这类 kind 的 goods 是 **`[[数量]]`** —— 元素是
      **单元素表**，不是裸数字。第一版按裸数字取（`_i(g)`）⇒ `int([15])` 抛错被吞 ⇒
      **静默丢条目**（面板少东西，且不报错）。所以这里剥壳：单元素表逐层剥到标量为止。
    """
    for _ in range(4):
        if isinstance(v, list):
            if not v:
                return d
            v = v[0]
            continue
        break
    return _as_int(v, d)


def _item_triples(groups):
    """`_drop` 的 `[style, goodslist]` -> `[(静态id, 数量, kind)]`。形状逐个对照实现：

    | kind | goods 元素形状 | 出处 |
    |---|---|---|
    | 2 / 98 | `[ucode, 静态id, 数量]` | `handlers_task.py:280` / `:288` |
    | 3 / 7 / 8 / 9 / 17 / 21 | `[[数量]]`（元素是单元素表） | `handlers_task.py:269/274/292/297` |
    | 1 / 4 | 装备/妖怪的 14 字段行（`grant_entries` 产出） | `handlers_task.py:260` |
    | 5 | `[[行id, 静态id, …], …]`（`add_ghost_rows`）；兜底才是 `[[数量]]` | `handlers_task.py:321/323` |
    | 18 / 22 | `[[静态id, 数量]]` / `[[静态id]]` | `handlers_task.py:309/313` |
    | 14 | `_grant_skin_with_dup` 的 fragments（形状随实现） | `handlers_task.py:302` |

    约定：`kind=3` 回 `itemId=0`（客户端铜钱图标不看 id）；取不到静态 id 的 kind **跳过**，
    免得给客户端一个渲染不出来的 id。
    """
    out = []
    for grp in (groups or []):
        try:
            kind = int(grp[0])
            goods = grp[1] or []
        except Exception:
            continue
        if kind not in _XP_LEDGER_KINDS:
            print('[XP3500] 跳过无法渲染的掉落 kind=%s' % (grp[0] if grp else '?'), flush=True)
            continue
        for g in goods:
            iid, num = 0, 0
            try:
                if kind in (2, 98):                    # [ucode, 静态id, 数量]
                    iid, num = _unwrap_num(g[1], 0), _unwrap_num(g[2], 1)
                elif kind in (1, 4):
                    # ★★2026-10-17（真机复核，3500 非空面板不弹的根因）：
                    #   14 字段**装备**行与 30 字段**妖怪**行的第 0 槽是 **ucode 实例号**，
                    #   静态 id 在第 1 槽（`handlers_equip._equip_item` 的
                    #   `[ucode, id, level, exp, ...]`；`handlers_bag.grant_monsters` 的 30 字段行同形，
                    #   客户端同样是 `insertItem(item[1], item)`）。
                    #   修前取 g[0] ⇒ 3500 清单里发的是实例号 ⇒ 客户端 `IconData:create(ucode, 1)`
                    #   查不到 L01 ⇒ `SDEquip.lua:117 m_SDEquipConfig=nil` 抛错 ⇒
                    #   `UIShowReward` 构造中断（真机 2026-10-17 23:30 的 bug/*.json 里有整条栈）
                    #   ⇒ 面板不弹、玩家卡在关卡里"退出关卡没反应"。
                    iid, num = (_unwrap_num(g[1], 0)
                                if isinstance(g, list) and len(g) >= 2 else 0), 1
                elif kind == 5:                        # 元灵: 完整行 -> 有静态 id；兜底 -> 纯数量
                    if isinstance(g, list) and len(g) >= 2:
                        iid, num = _unwrap_num(g[1], 0), 1
                    else:
                        iid, num = 0, _unwrap_num(g, 0)
                elif kind in (18, 22):                 # 宝箱 [静态id, 数量] / 头像框 [静态id]
                    iid = _unwrap_num(g[0], 0) if isinstance(g, list) and g else 0
                    num = _unwrap_num(g[1], 1) if isinstance(g, list) and len(g) >= 2 else 1
                elif kind == 14:                       # 皮肤：碎片行可能带 id，取不到就只记数量
                    if isinstance(g, list) and len(g) >= 2:
                        iid, num = _unwrap_num(g[0], 0), _unwrap_num(g[1], 1)
                    else:
                        iid, num = 0, _unwrap_num(g, 0)
                else:                                  # 3/6/7/8/9/17/21: [[数量]]
                    iid, num = 0, _unwrap_num(g, 0)
            except Exception:
                continue
            if num <= 0:
                continue
            if kind not in (3, 6, 7, 8, 9, 14, 17, 21, 22) and iid <= 0:
                continue                               # 有静态 id 的 kind 必须取到 id
            out.append((iid, num, kind))
    return out


def _xp_ledger_add(groups):
    """把**真实发放的**掉落并入本次探索账本（同 (id,kind) 累加）。"""
    items = _item_triples(groups)
    if not items:
        return []
    r = _xp_run_state()
    merged = {}
    for (iid, num, kind) in r['items'] + items:
        key = (int(iid), int(kind))
        merged[key] = merged.get(key, 0) + int(num)
    r['items'] = [[k[0], v, k[1]] for k, v in sorted(merged.items())][:_XP_LEDGER_MAX]
    r['t'] = int(time.time())
    return items


def _xp_ledger_for(gate):
    """3500 的应答体：`[[itemId, itemNum, itemType], ...]`（客户端逐元素取 [1]/[2]/[3]）。

    只认**同一个大关卡**且没过期的那份账本 —— 换关卡或超时一律当"这次没有记录"（回空表），
    不回上一局的旧清单（那会让玩家看到"没打过的奖励"）。
    """
    st = _xp_state()
    r = st.get('run_ledger')
    if not isinstance(r, dict) or r.get('date') != st.get('date'):
        return []
    if _i(r.get('gate'), 0) != _i(gate, 0):
        return []
    if int(time.time()) - _i(r.get('t'), 0) > _XP_LEDGER_TTL:
        return []
    out = []
    for it in (r.get('items') or []):
        try:
            if len(it) >= 3 and _i(it[1], 0) > 0:
                out.append([_i(it[0], 0), _i(it[1], 0), _i(it[2], 0)])
        except Exception:
            continue
    return out


def handle_explore_show_reward(body_json):
    """3500 NET_SHOW_REWARD：`[1, [ [itemId,itemNum,itemType], ... ]]`。

    ⚠ 契约来自装机客户端（见本节头部注释的四个出处）。客户端 `NetShowReward:recvMessage`
      取 `msg[1]` 后逐元素 `tonumber(v[1]/v[2]/v[3])`；`UIShowReward:showRewardList` 对
      空表打"本次探索没有获得任何物品"。所以**空表是合法且必要的**（真的什么都没拿），
      但"明明拿了却回空表"就是本条要修的缺陷。
    """
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    if not isinstance(req, dict):
        req = {}
    gate = _i(req.get('gateid'), 0) or _i(req.get('id'), 0)
    rows = _xp_ledger_for(gate) if gate else []
    if not rows:
        print('[XP3500] gate=%s 本次探索无记录 -> 回空表(客户端打"没有获得任何物品")' % gate, flush=True)
    else:
        print('[XP3500] gate=%s 本次探索累计 %d 类' % (gate, len(rows)), flush=True)
    # ★★2026-10-17（真机复核）：客户端 `parseMessage` 把 `_recvMsg[2]` 当 `msg`；
    #   `NetShowReward` 取 `msg[1]` 再 `pairs` 逐行读 `v[1]/[2]/[3]`。
    #   旧实现回 `[1, rows]` ⇒ 客户端拿到 `msg = rows`、`msg[1]` 只是**第一行**
    #   ⇒ 每条 v[1] 都是 nil ⇒ UIShowReward 创建失败（"设置→退出关卡"看起来没反应）。
    #   正确形状必须多包一层：`[1, [rows]]`（与 1037/3505 的 `[1,[rows]]` 同形）。
    return json.dumps([1, [rows]], separators=(',', ':')).encode('utf-8')


def _xp_war_victory(gate, fight_gid, iswin=True, cond_ids=None):
    """9003（探索战斗结算）胜利：按 `S12_SceneExplore` 的规则推进解锁度。

    判据逐行取自客户端 `exploreUnlockTool:killMonsterVictory():658-690`：
      · `fightId`（= 请求里的 `info`）**必须等于** `S12.Value1`；
      · Type 1/2/3 还要求 `T01_MstrGroup.Type == 2`（BOSS 组，`:728`）；
      · Type 4/5 是小妖（`T01.Type in (1,6)`，`:678-680`）；
      · `msg["exploreid"]` 是客户端算好的**已满足条件 id 串**（`&` 分隔）—— 直接采信。
    返回 `(satisfied_ids, degree)`。
    """
    g = _xp_gate(gate)
    gid = _i(fight_gid, 0)
    t01 = _T01_FULL.get(gid) or {}
    t_type = _i(t01.get('Type'), 0)
    if iswin:
        # 记下"这一局打过架"：Type 3「不遇怪击杀BOSS」的判据是它**没被置过**
        g['battle_fought'] = 1
        if t_type in (1, 6):
            g['c']['k4'] = _i(g['c'].get('k4'), 0) + 1
            g['c']['k5'] = _i(g['c'].get('k5'), 0) + 1
        for rid in (cond_ids or []):
            _xp_mark_cond(gate, rid)
    return _xp_note_degree(gate)


def _ct23_bump(gs, gate, boss, run):
    """UG CT=23「在《X》副本中击败某 BOSS」按 **BOSS 组**记账（同一 run 同一组最多 1 次）。

    ★★2026-10-22（独立审查判红 B2）：修前的驱动是
        `if mid in (9003, 9004) and info in _EGG_GROUPS: bump_task(23, 1)`
    其中 `_EGG_GROUPS` 取的是 **T37 每日任务** CT=23 的 `Value`
    （`2013|3013|4013|…` = **异界妖卵**组），而 `UG_07_Task` 里 19 条 CT=23 主线
    要求的全是 **BOSS** 组（2012 黄九郎 / 3012 河伯 / 4012 姬姬 / 11012 林王仙…）。
    两者在 `T01_MstrGroup` 里是**不同的行** ⇒ 修前是"打妖卵点亮击杀 BOSS"，
    而真的打死 BOSS 反而不计数（真机账号档 `ug_ct['23']=3` 就是这么来的）。
    ⇒ 现在只认 9003/9004 胜利时 `info` 命中的那个组 id，按它自己的格子累加。

    ★ 去重粒度是 `(run, boss)` 而不是 `(run)`：gate 50001/60001/70001 的 S12 里各有
      两行 `Type==1`（2011/2012…），同一局连杀两只不同 BOSS 时两只都要算
      （CT=3 仍然是每 run 只记 1 次，那是另一本账）。
    """
    try:
        c23 = gs.get('ct23')
        if not isinstance(c23, dict):
            c23 = gs['ct23'] = {}
        if c23.get(str(boss)) == run:
            return False
        from handlers_ugtask import bump_ug_ctv
        if not bump_ug_ctv(23, boss, 1):
            return False
        c23[str(boss)] = run
        print('[UG23] BOSS击杀 gate=%s boss=%s -> CT=23 目标 +1' % (gate, boss), flush=True)
        return True
    except Exception as _e:
        print('[UG23] !! 计数失败(不影响结算): %r' % (_e,), flush=True)
        return False


def _xp_ct3_bump(gate, fight_gid):
    """CT=3「通关任意关卡」（2026-10-18 用户拍板，口径 A）。

    判据 = **9003 胜利** 且 `info`（本场 T01 战斗组）命中该关 `S12.SceneExplore.Type==1`
    的 `Value1`（= "首杀BOSS-…" 指的 BOSS 组）；**每个 explore run 最多记 1 次**。
    返回 True = 本次真的 bump 了（供测试断言/日志）。

    ⚠ 为什么不是别的信号（真机 deploy.log 已钉）：
      · 9003 胜利本身不够：中途杀小怪 info=1001/1002/1003 也是 9003 胜利；
      · 解锁度 100% 不够：关卡打通过一次后，cond 跨天保留，之后每次 9003 都返回 100%；
      · 1706 不够：手动离开/重开/失败都会发 1706。
    """
    try:
        g = _i(gate, 0)
        gid = _i(fight_gid, 0)
        if not gid or g not in _XP_BOSS_GROUPS or gid not in _XP_BOSS_GROUPS[g]:
            return False
        st = _xp_state()
        run = _i(st.get('run'), 0)
        if run <= 0:
            # 没进过 1701（`run` 恒 0）就不是"一次探索" —— 此时杀 BOSS 不能算通关。
            print('[CT3] 无进行中的探索 run（未进 1701）-> 不计', flush=True)
            return False
        gs = _xp_gate(g)
        # ★★2026-10-22（审查判红 B2）：CT=23 按 BOSS 组记账，必须放在 CT=3 的
        #   per-run 去重**之前** —— 同一局可以杀两只不同 BOSS（50001/60001/70001 都是双 BOSS 关），
        #   CT=3 只算一次，但两只各自的 CT=23 任务都要算。
        _ct23_bump(gs, g, gid, run)
        if gs.get('ct3_run') == run:
            return False                  # 同一局杀第二个 BOSS 不重复计
        from handlers_task import bump_task, CT_CLEAR
        # ★★2026-10-18（审计 F2）：**先计数、后落去重标记**。修前先写 `ct3_run` 再 bump，
        #   bump 抛异常（落盘失败等）时标记已经写下 ⇒ 这一局永远补不回来。
        #   现在计数失败 -> 标记不动 -> 同 run 的下一次 BOSS 击杀仍会补记
        #   （每 run 至多 1 次；已计数的那次不会被重复计）。
        bump_task(CT_CLEAR, 1)
        # ★★2026-10-18（UG 11005）：**同一判据**再喂 UG 的 CT=29 按-gate 账
        #   `ug['gate_clear']`（不写共享 `ug_ct`/`a07_ct`，见 handlers_ugtask 模块头）。
        #   判据与 CT=3 完全一致：9003 胜利 + info 命中该关 S12 Type==1 BOSS 组 +
        #   每个 explore run 最多 1 次 ⇒ "进过一次"（1701）/"退出"（1706）/"小怪"都不算。
        #   真机取证（2026-10-18）：40001 的 9003 小怪请求 info=1001/exploreid=110；
        #   1706 请求体只有 `{"gateid":40001}`，没有通关标志；40001 可重复进（重新挑战=1701）。
        #   放在 `ct3_run` 之前、单独 try：UG 账失败不能把已成立的 CT=3 计数回滚。
        try:
            from handlers_ugtask import bump_ug_gate_clear
            if bump_ug_gate_clear(g, 1):
                print('[UG29] 副本探索 gate=%s -> UG 按 gate 进度 +1' % g, flush=True)
        except Exception as _eu:
            print('[UG29] !! 计数失败(不影响 CT3): %r' % (_eu,), flush=True)
        gs['ct3_run'] = run
        print('[CT3] 通关 gate=%s BOSS组=%s run=%s -> +1' % (g, gid, run), flush=True)
        # ★★2026-10-18（CT=29，真机取证）：同一判据 + `gate ∈ S00.EliteID` = 「困难难度通关」。
        #   T37 1020~1026 七行的 Value 就是七个 EliteID，所以必须用**带 Value 匹配**的接口
        #   （裸 `bump_task(29)` 会一次点亮全部七行）。
        #   放在 `ct3_run` 之后、单独的 try 里：CT=29 失败不影响 CT=3，也不会把标记回滚
        #   （下一次进这关还会再打一次 BOSS，任务本身 Quantity=1，最多晚一轮补上）。
        if g in _ELITE_GATES:
            try:
                from handlers_task import bump_task_value
                bump_task_value(29, g, 1)
                print('[CT29] 困难难度通关 gate=%s -> task(value=%s)' % (g, g), flush=True)
            except Exception as _e29:
                print('[CT29] !! 计数失败(不影响 CT3): %r' % (_e29,), flush=True)
        return True
    except Exception as _e:
        print('[CT3] !! 计数失败(不影响结算): %r' % (_e,), flush=True)
        return False


def _xp_satisfied(gate):
    """按当前计数算出**已满足**的条件 id，并返回 `(ids, degree)`。"""
    ids, total = [], 0
    for row in _XP_S12_BY_GATE.get(_i(gate), []):
        if _xp_count(gate, row) >= _i(row.get('value2'), 1):
            ids.append(_i(row['id']))
            total += _i(row.get('explore'), 0)
    return ids, max(0, min(100, total))


def _xp_note_degree(gate):
    """把"该关卡历史最高解锁度"记进 `explore_prog[gates][g].best`（**只升不降**）。

    这是成就 **Type 105（关卡完成度）** 的唯一数据源（`handlers_achievement._derived_value`
    读它）。为什么必须单独记峰值、而不是让成就去调 `_xp_degree()`：
      · 解锁度**没有清零路径**（日重置只清 k5/k7/k9/k11 与 open，不碰已完成的 cond），
        所以它会单调上升 —— 但"单调"是**当前实现的性质**，不是契约；
      · 成就达成判定是读取时现算的（`ach_done = 计数 >= Value2`），一旦哪天有人给解锁度
        加了清零/重置，"已经亮过的整级奖励"会跟着抖回去（见 `set_ach` 的注释）。
      记峰值把这条耦合切断，成本只有一个 int/关卡。
    """
    ids, deg = _xp_satisfied(gate)
    try:
        g = _xp_gate(gate)
        if deg > _i(g.get('best'), 0):
            g['best'] = deg
    except Exception:
        pass
    return ids, deg


def _xp_degree(gate):
    """`sum(满足条目的 Explore)` —— 与客户端 `calcuExploreDegree()` 同一算法。"""
    return _xp_note_degree(gate)[1]


def _xp_exploredata(gate):
    """1705 的 `msg[3]`：**该大关卡的全部条件行** `[[unlockId, count], ...]`。

    ⚠ 发全量而不是只发"已满足"的：`exploreUnlockTool:isRemoteExploreExit()` 用它判断
      "这条条件的进度已知多少"，缺行的表现是 UI 统计图上那一条**永远显示 0/N**
      （余额不足/超发都不会报错，只是骗人）。行数很少（40001 只有 6 行）。
    """
    return [[_i(r['id']), _xp_count(gate, r)] for r in _XP_S12_BY_GATE.get(_i(gate), [])]


def _xp_scenedata(gate):
    """1705 的 `msg[4]`：一次性对象已清列表 —— **只发 `SaveType==2`** 的对象。

    ★★2026-10-16（任务书 A1 ③④）：修前有两个错，第二个是线上形状问题：
      ① 把 `Type in (2,3,4)`（普通/封印/神秘宝箱）全发下去，而客户端**只**会为自己
         `SaveType==2` 的那些调 `explore_updateEventByUcode`（`ModelBox.lua:202`）
         ⇒ 服务端"记住"了客户端不记的东西，两边对不上。
      ② 元素是 **dict** `{'type':'c','1':<sid>}`，而客户端 `NetExploreDegree.lua:54-65` 是
         `ipairs(scenedata)` + `removeOnlyData[1]` / `[2]`：
            `ipairs` 只走整数下标；`removeOnlyData[1]` 在 JSON 对象里是**字符串键**，取不到；
            更致命的是 `SQExplore:updateEventByUcode`（`SQExplore.lua:177`）**三个参数任一
            nil 就提前 return** —— 而 `[2]` 从来没被填过 ⇒ 这一个箱子永远不算"已清"。
      ⇒ 现在发 **2 元数组** `[静态id, 'c']`：`[1]`/`[2]` 都取得到，`tonumber(removeOnlyData.type)`
        是 nil（不触发 1/2 -> 'a'/'c' 的转换），`tostring(item.Type) == 'c'` 与
        `ExploreObjType.BoxType='c'`（`EDataManager.lua:28`）对得上。
    """
    out = []
    for sid in sorted(_xp_gate(gate)['open'].keys(), key=lambda x: _i(x, 0)):
        st = _i(sid, 0)
        if _S03_SPAWN_SAVETYPE.get(st, 0) != 2:
            continue                  # SaveType=1：客户端每局都会重新渲染，不下发
        stype = _S03_SPAWN_TYPE.get(st, 0)
        out.append([st, 'c' if stype in (2, 3, 4) else 'a'])
    return out


# ============================================================================
# ★2026-10-11（新包 A0 分诊第 1 批）：解锁度门控宝箱 —— 1705 的 `msg[5]` + 3501 领奖
# ----------------------------------------------------------------------------
# 修前事实（逐行核过，判据取自**真机跑的那个消费者**）：
#   · `S01_Level.SearchReward` = 该大关卡三个门控宝箱的 **T02_Reward.ID**（`|` 分隔，
#     `-1` = 没有宝箱，见 40004/99999/100006/30001/20001/21001）。
#   · 客户端 `explore/ExpBoxData.lua:38-45` 用 SearchReward 切出 boxId；
#     `network/explore/NetExploreDegree.lua:72-91` 在 **`msg[5]` 为空**时用它兜底成
#     「三个箱子 status=0」，非空时逐元素读 `v[1]=boxId / v[2]=status`。
#   · 门槛来自 `T23_Function[281].Value = 30|70|100`（客户端 `ExpBoxData:getExpPa`）。
#   · 服务端 `S01_Level` 原先**没有 SearchReward/ReviveNum 两列**（20 行与 1.30 包一一对应，
#     纯列投影已补，见 `docs\AUDIT-msgid-triage-2026-10-11.md`）。
# 状态语义（`ExpBoxData.EXP_BOX_STATUS`）：0 未完 / 1 未领（可点）/ 2 已领。
# ⚠ 与既有 `open` **刻意分开**：`open` 记的是地图上"被清掉的一次性对象"（1704，静态 id，
#   归 1705 的 `scenedata`）；宝箱是**解锁度门槛制**、按大关卡记，两者 id 空间不同
#   （实测 5201~5206 同时是 `T02_Reward.ID` 与 `P01_1_MGIndex.ID`，**绝不能混用**）。
# 发全量三箱（含 status=0）：客户端 UI 只渲染 status∈{1,2} 的箱子，恒发三箱与客户端
#   自己的兜底同形，少发反而会让 `getShowBox()` 拿不到该有的项。
_XP_BOX_THRESHOLDS = []
_XP_S01_ROW = {}
_XP_BOX_GROUP = {}          # T02_Reward.ID -> GroupID（门控宝箱 id 是 **ID**，发放要按 GroupID）
try:
    _s1r = _load_json('S01_Level.json')
    _s1h = {c: k for k, c in enumerate(_s1r[0])}
    if 'SearchReward' in _s1h:
        for _r in _s1r[1:]:
            try:
                _XP_S01_ROW[int(_r[_s1h['ID']])] = _r
            except Exception:
                continue
    _t23 = _load_json('T23_Function.json')
    for _r in _t23[1:]:
        if str(_r[0]) == '281':
            _XP_BOX_THRESHOLDS = [_i(x, 0) for x in str(_r[2]).split('|') if str(x).strip() != '']
            break
    _t02 = _load_json('T02_Reward.json')
    _t02h = {c: k for k, c in enumerate(_t02[0])}
    for _r in _t02[1:]:
        try:
            _XP_BOX_GROUP[int(_r[_t02h['ID']])] = int(_r[_t02h['GroupID']])
        except Exception:
            continue
except Exception:
    pass


def _box_reward_group(boxid):
    """门控宝箱 id（= `T02_Reward.ID`）-> 奖励组（`GroupID`）；表外/取不到 -> None。

    ⚠ 取数必须按来源：`S01_Level.SearchReward` 里放的是 **ID**，而 `_grant_group`
      吃的是 **GroupID** —— 直接拿 ID 当组用会静默发错东西（本项目已吃过两次同款亏）。
    """
    try:
        return _XP_BOX_GROUP.get(int(boxid))
    except Exception:
        return None


# ============================================================================
# ★2026-10-11（A0 第 1 批）：探索内复活 —— 1708 EXPLORE_REVICEINFO + 1707 EXPLORE_REVICE
# ----------------------------------------------------------------------------
# 客户端（1.30 装机，逐个出处的判据）：
#   `war/ui/VWarFailure.lua:13-28` 战斗失败时先看 **106 PUSH_SWITCH** 里 EXPLORE_REVICE 开关
#     （开关关着或不是探索战斗 -> 直接渲染，连请求都不发）；开着就发 **1708** `{gateid=大关卡id}`
#   `network/war/NetExploreReviceInfo.lua:41-43` 读 `msg[1]=timesState / msg[2]=times / msg[3]=cost`
#   `war/ui/VWarFailure.lua:117-124` **`timesState ~= 1` 就把复活按钮整块藏起来**（文案都不出）
#   `:131-135` `times == 0` -> 同样藏按钮（"本次探索还可复活 N 次"）
#   `:140-149` 消耗图标与"当前拥有"数量取 **`T23_Function[267].Value` = 1014 回魂丹**；
#             倒计时取 `T23_Function[269].Value` = 30 秒
#   `:183-190` 点确定发 **1707** `{gateid}`；回调里 `SQBIData:doOutflow(...)` 只是 **BI 打点**
#             （`sqdata/SQBIData.lua:303` 起，纯上报、不发协议、不改本地数量）
#             ⇒ **回魂丹的真值是服务端**，必须由这里扣。
# 修前：1707/1708 都不在 `GAME_HANDLERS` ⇒ 落兜底 ⇒ `timesState` 恒 nil ⇒ **按钮永远不出现**，
#   玩家在探索里失败后只能退出重来（"复活"这个功能对整个 1.30 包不存在）。
# 口径：
#   · `times`（本次还能复活几次）= `S01_Level.ReviveNum`（新包该列：普通 3 / 40004 教学 -1 无限）
#     **减去本次探索 run 已经用掉的次数**；次数按"本次 run"记（与 1701 开 / 1706 收同一口径）。
#   · `cost` = 1 个回魂丹（`T23[267]` 是物品 id 而非数量；客户端只拿它取图标与"当前拥有"，
#     数量文案里没有占位符 ⇒ 服务端按 1 个扣，并在没有时拒发）。
#   · 失败一律回 `[0,{}]`：客户端 1708 的码表**只有 `[0]`**，别的码它 `getLocal` 到 nil
#     ⇒ 静默不弹（不崩），所以不编造一个它不认识的错误码。
_XP_REVIVE_ITEM = 1014          # T23_Function[267].Value = 1014 回魂丹（L02[1014].SceneName 可核）


def _xp_revive_num(gate):
    """该大关卡的复活次数上限；`-1` = 不限（40004 新手教学）。取不到 -> 0（不给假能力）。"""
    r = _XP_S01_ROW.get(_i(gate, 0))
    if not r or 'ReviveNum' not in _s1h:
        return 0
    return _i(r[_s1h['ReviveNum']], 0)


def _xp_run_revives():
    """本次探索 run 已用掉的复活次数（per-account；1701 进关卡时归零）。"""
    return _i(_xp_gate(_xp_run_state().get('gate') or 0).get('revive'), 0)


def _spend_prop_by_static(pid, n=1):
    """按**静态 id** 扣 n 个道具（袋里是 `ucode -> 数量`，静态 id 在 `prop_id`）。

    与 `handlers_specialty.py:418-425` / `handlers_cult.py:387-391` 同款写法：
    扣到 0 就**连键一起清**（留一个 0 值会在 1033 背包里显示成"0 个道具"）。
    返回实际扣掉的个数（不够就一个都不扣，避免扣一半）。
    """
    want = _i(pid, 0)
    if want <= 0 or _i(n, 0) <= 0:
        return 0
    bag = EQ_STATE.get('prop') or {}
    ids = EQ_STATE.get('prop_id') or {}
    for uc in list(bag.keys()):
        if _i(ids.get(uc), 0) == want and _i(bag.get(uc), 0) >= _i(n, 0):
            bag[uc] = _i(bag.get(uc), 0) - _i(n, 0)
            if bag[uc] <= 0:
                bag.pop(uc, None)
                ids.pop(uc, None)
            return _i(n, 0)
    return 0


def handle_explore_revive_info(body_json):
    """1708 EXPLORE_REVICEINFO：`[timesState, times, cost]` —— 探索内复活面板的三格数据。

    `timesState`：1 = 该关卡可复活（客户端只有看到 1 才显示复活按钮）。
      判据取两处：① 该大关卡在 `_XP_GATES` 内（有探索条件=真的可探索）；
      ② `S01_Level.ReviveNum != 0`（表里 40004/99999 这类是 -1/3，0 才会不给）。
    """
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    gateid = _i(req.get('gateid'), 0)
    if gateid <= 0 or gateid not in _XP_GATES:
        return b'[0,{}]'                      # 码表只有 [0]，表外一律走它（客户端静默不弹）
    cap = _xp_revive_num(gateid)
    run_id = _i(_xp_run_state().get('gate'), 0)
    if cap < 0:
        times = 999                           # 不限（教学关）：发一个大数，客户端只做 >0 判断与文案
    else:
        times = max(0, cap - (_xp_run_revives() if run_id == gateid else 0))
    state = 1 if (cap < 0 or times > 0) else 0
    return _simple_resp(state, times, 1).encode('utf-8')


def handle_explore_revive(body_json):
    """1707 EXPLORE_REVICE：`[prop, roleInfo]` —— 复活成功，并**在服务端扣掉回魂丹**。

    ⚠ 客户端拿 `msg[1]` 调 `SQManager:prop_getMe():updateItem(prop[1], prop)`（单条道具行）、
      `msg[2]` 调 `gamer_setRoleInfo` ⇒ 两格都不能省：省了 roleInfo，复活后金币/体力显示会是旧值。
    ⚠ 只支持**物品消耗**这一种复活（与客户端 UI 一致：它显示的就是 `T23[267]` 那个道具）。
      没有该道具 -> `[0,{}]`（码表只有 0），**不发成功** —— 否则玩家能白嫖复活。
    """
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    gateid = _i(req.get('gateid'), 0)
    if gateid <= 0 or gateid not in _XP_GATES:
        return b'[0,{}]'
    cap = _xp_revive_num(gateid)
    run_id = _i(_xp_run_state().get('gate'), 0)
    g = _xp_gate(gateid)
    used = _i(g.get('revive'), 0) if run_id == gateid else 0
    if cap >= 0 and used >= cap:
        return b'[0,{}]'                      # 次数用完（客户端本来就不该发，防伪造）
    spent = _spend_prop_by_static(_XP_REVIVE_ITEM, 1)
    if spent <= 0:
        return b'[0,{}]'                      # 没有回魂丹
    g['revive'] = used + 1
    save_state()
    _bag = EQ_STATE.get('prop') or {}
    _ids = EQ_STATE.get('prop_id') or {}
    _uc = next((u for u in _bag if _i(_ids.get(u), 0) == _XP_REVIVE_ITEM), None)
    prop = [_uc or '', _XP_REVIVE_ITEM, _i(_bag.get(_uc), 0) if _uc else 0]
    return json.dumps([1, [prop, _role_info()]], separators=(',', ':')).encode('utf-8')


def _xp_box_ids(gate):
    """该大关卡的门控宝箱 id 列表（`S01_Level.SearchReward`，`-1`/缺列 -> 空表）。"""
    r = _XP_S01_ROW.get(_i(gate, 0))
    if not r:
        return []
    h = {c: k for k, c in enumerate(_s1h)}
    if 'SearchReward' not in h:
        return []
    raw = str(r[h['SearchReward']] or '').strip()
    if raw in ('', '-1'):
        return []
    return [_i(x, 0) for x in raw.split('|') if str(x).strip() != '']


def _xp_box_status(gate, boxid, degree):
    """`msg[5]` 里单个宝箱的 status（0 未完 / 1 未领 / 2 已领）。"""
    g = _xp_gate(gate)
    if str(_i(boxid, 0)) in (g.get('box') or {}):
        return 2
    ids = _xp_box_ids(gate)
    try:
        idx = ids.index(_i(boxid, 0))
    except ValueError:
        return 0
    th = _XP_BOX_THRESHOLDS[idx] if idx < len(_XP_BOX_THRESHOLDS) else 0
    return 1 if _i(degree, 0) >= th else 0


def _xp_boxdata(gate):
    """1705 的 `msg[5]`：`[[boxId, status], ...]`（**全量下发**，与客户端兜底同形）。"""
    _xp_gate(gate)
    deg = _xp_degree(gate)
    return [[bid, _xp_box_status(gate, bid, deg)] for bid in _xp_box_ids(gate)]


def _zzrc4_int(_b64, _ts):
    # 客户端 ZZRc4.encodeEncrypt 的逆运算 (实现见 work/zzrc4.py, 已对真机样本验证)
    try:
        from zzrc4 import zz_decrypt_int
        return zz_decrypt_int(_b64, _ts)
    except Exception:
        return None

def _t02_is_group(gid):
    """这个 id 是不是一个**真实存在**的 T02 奖励组（`GroupID`，不是 `ID`）。"""
    try:
        return bool(_T02.get(int(gid)))
    except Exception:
        return False


def official_drop_group(mid, req):
    """按协议号 + 请求体解析本场的官方奖励组 id; 取不到 -> None (调用方走兜底, 绝不下发错东西)。

    字段来源 (逐行取自 1.31 装机明文, 见 work/_v54b_battlereward_audit.py):
      9001 妖怪入侵 : NetAggressResult.lua:36 `msg["evalue"] = error_verify(tostring(_evalue))` —— **明文**
                      ★2026-10-18：它是**评价奖励组 id**（`StageDelegateAggress.lua:208`），
                      只认**真实 T02 组**（0/表外/乱码 -> None，调用方走"空掉落不兜底"）
      9003 探索/秘境/世界图 : NetWarResult.lua:41 `_dropID = ZZRc4.encodeEncrypt(tostring(_dropID),timeStamp)` -> msg["value"]
      9005 BOSS结算 : NetBossResult.lua:28 同款加密, 但 `_dropID` 传的是 **`m_saveObjectPosition`**
                      (`WarStageUnits.lua:752`) —— 名字骗人, **值就是奖励组 id**（见下）
      9006 PVE结算  : NetPVEResult.lua:28 同款加密, 传的也是 `m_saveObjectPosition` (`:757`)
      9007 剧情结算 : NetStoryResult.lua:28 同款加密, 传的还是 `m_saveObjectPosition`
      8006 卡池Boss : NetCardBossResult.lua 同款加密 -> msg["value"]
      9004 妖塔     : NetTowerResult.lua 传的是 **TowerID(S51 ID)**, 要跳一跳:
                      value -> S51_Tower.ID -> MonsterGroupID -> T01_MstrGroup.BattleReward

    ★★2026-10-08（战斗奖励轮）：**9005/9006/9007 从此接上官方掉落**。
      修前它们一律走"取不到官方组"的兜底（死值 500 金 / 50 魂），而这三个模式的客户端
      **都**发了加密的掉落组 id（与 9003 同一种加密、同一个 `time` 键）。
      ★2026-10-18（真机 9006）：`info` 对 9005 是 T01 行、对 9006 却是 **T24_YMYW.IDGroup**
      —— 两者不是同一套号，不能共用一条判据。
      · 9005：`value` 解出的组必须 == `T01_MstrGroup[info].BattleReward` **或** 该行
        `ObjectPosition`（客户端 9005 传的是 `m_saveObjectPosition`，见 `WarStageUnits.lua:821`）；
      · 9006：`id`(T24.ID) -> `T24_YMYW.BattleId` -> T01 行；`value` 必须等于该行的
        `BattleReward` **或** `ObjectPosition`（客户端传的是 `m_saveObjectPosition`）；
      · 9007：没有 `info`（`NetStoryResult` 不送），`value` 只能是某个唯一 T01 行的
        `ObjectPosition`（`WarStageUnits.lua:841`，**仅胜利分支**；失败分支送
        `m_saveRewardID`=BattleReward，而本函数只在 `iswin` 路径调用），反查不到 -> None。
      不一致一律 `None`（走兜底并**在日志里报**），绝不按猜测下发。
    """
    if not isinstance(req, dict):
        return None
    if mid == 9001:
        # ★★2026-10-18（用户拍板：关掉 9001 的兜底印钞机）：`evalue` 是**评价奖励组 id**
        #   （`StageDelegateAggress.lua:208`，明文）。客户端真值恒为 `rewardList[score]`，
        #   score = `calcBattleScore()` = `max(5-discontent,1)` ∈ 1..5，而 rewardList[1..5]
        #   = T26_YGRQ 该行五列 —— **240/240 全部命中 T02_Reward.GroupID**（本轮实测）。
        #   ⇒ 只有真实 T02 组才返回；0/表外/乱码一律 None（不是"没解析出来"，而是"不可能是真包"）。
        try:
            v = int(req.get('evalue'))
        except (TypeError, ValueError):
            return None
        return v if _t02_is_group(v) else None
    if mid in (9003, 8006):
        return _zzrc4_int(req.get('value'), req.get('time'))
    if mid == 9004:
        tid = _zzrc4_int(req.get('value'), req.get('time'))
        if tid is None:
            return None
        mgid = _S51_TOWER_MG.get(tid)
        return t01_battle_reward(mgid) if mgid else None
    if mid in (9005, 9006, 9007):
        v = _zzrc4_int(req.get('value'), req.get('time'))
        if v is None:
            return None
        if mid == 9007:
            # ★★2026-10-18（同类扫描）：客户端 9007 送的是 `m_saveObjectPosition`
            #   （`WarStageUnits.lua:841` 的注释明写"把 m_saveRewardID 改为
            #   m_saveObjectPosition"），**不是** T02 组 ⇒ 只按 ObjectPosition 反查唯一
            #   T01 行，再取该行 BattleReward。修前要求 `value` 是 T02 组，而真实值是
            #   890002 这类 T01 行 id ⇒ 每场剧情战斗都被判"不是 T02 组"落兜底 500/50。
            #   ⚠ 仅**胜利**分支如此（`WarStageUnits.lua:843`）；**失败**分支（`:935`）
            #     送的是 `m_saveRewardID`（= `T01.BattleReward`，真机 890001 失败包
            #     解出 90001）。本闸只在 `iswin` 路径调用，所以失败包不会进来；
            #     若将来把闸前移到 result 判断之前，必须同时接受 `BattleReward`。
            _gid7 = _t01_by_object_position(v)
            return t01_battle_reward(_gid7) if _gid7 is not None else None
        if mid == 9006:
            # ★★2026-10-18（真机 1702/9006 取证）：9006 的 `info` 是 **T24_YMYW.IDGroup**
            #   （101..107），**不是** T01 战斗组；`id` 才是 T24_YMYW.ID（1011..1072）。
            #   真机样本：`{"info":"107","id":1072,"value":<ZZRc4>}`，value 解出 **30107**
            #   = `T01_MstrGroup[30113].ObjectPosition` = 同一行的 `BattleReward`。
            #   修前拿 `info=107` 去查 T01 -> 查不到 -> 走兜底 500 金/50 魂
            #   （= 用户看到的"只给 50 妖魂 + 500 铜钱"）。
            #   判据：T24[id].BattleId -> T01 行；解出的 `value` 必须等于该行的
            #   `BattleReward` **或** `ObjectPosition`（客户端 9006 传的是
            #   `m_saveObjectPosition`，见 `WarStagePVE.lua:80` + `WarStageUnits.lua:827`）。
            bid = _ymyw_battle_id(req)
            if not bid:
                return None
            b = t01_battle_reward(bid)
            if b is None:
                print('[WAR_DROP] !! 9006 T24[id=%s] -> BattleId=%s 无 BattleReward -> 兜底'
                      % (req.get('id'), bid), flush=True)
                return None
            op = _i((_T01_FULL.get(bid) or {}).get('ObjectPosition'), 0)
            if v not in (b, op):
                print('[WAR_DROP] !! 9006 value解出=%s 与 T01[%s].BattleReward=%s/'
                      'ObjectPosition=%s 不一致 -> 走兜底' % (v, bid, b, op), flush=True)
                return None
            return b
        # 9005：以 `info`（= T01 战斗组）的 BattleReward 为准；`value` 是**互证**
        #   ★★2026-10-18（全项目同类扫描）：客户端 `WarStageUnits.lua:821` 对 9005 传的是
        #   `m_saveObjectPosition`（`WarData:getObjectPosition()` = `T01[info].ObjectPosition`），
        #   **不是** BattleReward。表内 Type==2（场景Boss/大蛇）60 行里 **46 行**
        #   `ObjectPosition != BattleReward`（如 1011：BattleReward=1106 / ObjectPosition=1011）
        #   ⇒ 修前这些 BOSS 的合法结算会被判"不一致"落兜底 500/50。
        #   现判据：`v` 必须等于 `BattleReward` **或** `ObjectPosition`（与 9006 同款），
        #   发出去的仍是 BattleReward 组。
        b = t01_battle_reward(req.get('info'))
        if b is None:
            return None
        op = _i((_T01_FULL.get(_i(req.get('info'), 0)) or {}).get('ObjectPosition'), 0)
        if v not in (b, op):
            print('[WAR_DROP] !! %s value解出=%s 与 T01[info=%s].BattleReward=%s/'
                  'ObjectPosition=%s 不一致 -> 走兜底'
                  % (mid, v, req.get('info'), b, op))
            return None
        return b
    return None

def _war_drop_group_checked(mid, req):
    """9001/9003/9004/9005/9006/9007 胜利结算的**来源校验** -> `(错误应答字节 or None, 官方奖励组 id or None)`。

    ★★2026-10-16（任务书 D②）：修前这两个号对 `value`/`info` 只做"能解出个 id 就算数"：
      · `9003` 只把 `info` 当"值"用，**从不校验解密出的 `value` 是否等于
        `T01_MstrGroup[info].BattleReward`**（那条检查只写在 9005/9006 上）；
      · `9004` 只要求解出的 TowerID 在 S51 里，也不比 BattleReward。
      ⇒ 客户端/伪造包可以拿任意一个合法 T02 组当掉落来源。
    现在的判据（全部有表源）：
      · `time` 缺失 -> `580 时间错误`
      · `info`（9003）/ `value`（9004）解析不出 -> `503 掉落组ID错误` / `581 密文错误`
      · 该战斗组**没有** BattleReward（官方本来就没配）-> `503`（没有可发的组，不编）
      · 解出的 id ≠ `T01[info].BattleReward` -> **`504 战斗类型与掉落组ID不符`**（任务书点名的码）
      · 9006：`id` 必须命中 `T24_YMYW` 且 `info` 是同一 `IDGroup`（`_ymyw_battle_id`），
        `value` 必须等于 `T01[T24.BattleId].BattleReward`（或该行 `ObjectPosition`）；
        表外 id/无组 -> 503、密文解不出 -> 581、值不符 -> 504。**修前 9006 不在本函数里**，
        坏包会一路走到兜底 500 金/50 魂（2026-10-18 审计 F1）。
      · 9005：`info` 必须是 T01 行且有 `BattleReward`；`value` 必须等于该行的
        `BattleReward` **或** `ObjectPosition`（客户端 `WarStageUnits.lua:821` 传的是后者，
        表内 46/60 个 Type==2 行两者不相等）。修前既会把合法包判成不一致，坏包又直接落兜底。
      · `9001`：`evalue`（评价奖励组，明文）必须是**真实 T02 组**；0/表外/乱码 ->
        `(None, 0)` 哨兵（成功 + 空掉落，**不兜底也不报错**，2026-10-18 用户拍板）。
    """
    t = req.get('time')
    if t in (None, ''):
        return b'[580]', None
    if mid == 9001:
        # ★★2026-10-18（用户拍板）：9001 的 `evalue` 是**评价奖励组 id**（明文）。
        #   客户端真值恒为 T26_YGRQ 该行五列之一（48 行 × 5 列 = 240 个值**全部**
        #   ∈ T02_Reward.GroupID）；0/非数字/表外 id **不可能是真客户端发的**。
        #   口径：不报错、结算照常成功，但**空掉落**（返回 `0` 哨兵 -> 调用方 `_no_reward`，
        #   不兜底）。为什么不用 503/504：9001 没有密文/第二字段可互证，且用户明确
        #   要求"0/表外/解析不出 -> 成功 + 空掉落"；伪造包最坏也只是拿到它自己写明的那个
        #   真实 T02 组的官方内容（`time` 仍是客户端字段，架构级防重放不在本轮）。
        try:
            _ev1 = int(req.get('evalue'))
        except (TypeError, ValueError):
            _ev1 = None
        if _ev1 is None or not _t02_is_group(_ev1):
            print('[WAR_DROP] !! 9001 evalue=%r 不是真实 T02 组 -> 成功+空掉落(不兜底)'
                  % (req.get('evalue'),), flush=True)
            return None, 0
        return None, _ev1
    if mid == 9003:
        if not is_valid_war_group(req.get('info')):
            return b'[503]', None
        want = t01_battle_reward(req.get('info'))
        if not want:
            return b'[503]', None
        got = _zzrc4_int(req.get('value'), t)
        if got is None:
            return b'[581]', None
        if got != want:
            print('[WAR_DROP] !! 9003 value解出=%s ≠ T01[info=%s].BattleReward=%s -> 504'
                  % (got, req.get('info'), want), flush=True)
            return b'[504]', None
        return None, want
    if mid == 9007:
        # 9007 没有 info：`value` **只能**是"某个唯一 T01 行的 ObjectPosition"（客户端行为）。
        #   · 能反查到行 -> 用该行 BattleReward；该行**没配奖励** -> 返回 `0` 哨兵
        #     （调用方据此跳过兜底，但不报错 —— "合法来源 + 官方没配" 不等于坏包）；
        #   · 反查不到 -> 504。**不接受"直接是 T02 组"**：那是修前的猜测，客户端从不发。
        got = _zzrc4_int(req.get('value'), t)
        if got is None:
            return b'[581]', None
        _gid7 = _t01_by_object_position(got)
        if _gid7 is not None:
            _want7 = t01_battle_reward(_gid7)
            return None, (_want7 if _want7 else 0)
        print('[WAR_DROP] !! 9007 value解出=%s 不是任何 T01 行的 ObjectPosition -> 504'
              % (got,), flush=True)
        return b'[504]', None
    if mid == 9005:
        # ★★2026-10-18（全项目同类扫描）：9005 与大蛇 BOSS 同源可自证 ——
        #   `info` 必须是 T01 行且有 BattleReward，`value` 必须等于该行的
        #   `BattleReward` 或 `ObjectPosition`（客户端传的是后者）。坏包在**任何状态变更之前**
        #   拒绝，修前它会落兜底 +500 金/+50 魂并记 `battle_cleared`/CT=5。
        if not is_valid_war_group(req.get('info')):
            return b'[503]', None
        want = t01_battle_reward(req.get('info'))
        if not want:
            print('[WAR_DROP] !! 9005 info=%r 无 BattleReward -> 503' % (req.get('info'),),
                  flush=True)
            return b'[503]', None
        got = _zzrc4_int(req.get('value'), t)
        if got is None:
            return b'[581]', None
        op = _i((_T01_FULL.get(_i(req.get('info'), 0)) or {}).get('ObjectPosition'), 0)
        if got not in (want, op):
            print('[WAR_DROP] !! 9005 value解出=%s 与 T01[%s].BattleReward=%s/'
                  'ObjectPosition=%s 不一致 -> 504'
                  % (got, req.get('info'), want, op), flush=True)
            return b'[504]', None
        return None, want
    if mid == 9006:
        # ★★2026-10-18（审计 F1：无效 9006 仍能落到兜底 +500 金/+50 魂）：
        #   9006 与 9003 一样**来源可自证**（`id` -> T24_YMYW.BattleId -> T01.BattleReward），
        #   所以坏包必须在**任何状态变更之前**拒绝。修前它只过"一次性"闸，
        #   `official_drop_group()` 回 None 后落进 `if not _gid` 的兜底分支 ⇒
        #   伪造/乱码包照样白拿 500 金 50 魂，还会记 `battle_cleared` 与 CT=6。
        #   错误码与 9003 同一套码表：580 时间 / 503 组 / 581 密文 / 504 不符。
        bid = _ymyw_battle_id(req)
        if not bid or not is_valid_war_group(bid):
            print('[WAR_DROP] !! 9006 id=%r/info=%r -> BattleId=%s 非法 -> 503'
                  % (req.get('id'), req.get('info'), bid), flush=True)
            return b'[503]', None
        want = t01_battle_reward(bid)
        if not want:
            print('[WAR_DROP] !! 9006 T24 -> BattleId=%s 无 BattleReward -> 503'
                  % (bid,), flush=True)
            return b'[503]', None
        got = _zzrc4_int(req.get('value'), t)
        if got is None:
            return b'[581]', None
        op = _i((_T01_FULL.get(bid) or {}).get('ObjectPosition'), 0)
        if got not in (want, op):
            print('[WAR_DROP] !! 9006 value解出=%s 与 T01[%s].BattleReward=%s/'
                  'ObjectPosition=%s 不一致 -> 504' % (got, bid, want, op), flush=True)
            return b'[504]', None
        return None, want
    # 9004：`value` 解出的是 **S51_Tower.ID**（不是奖励组），要跳一跳：
    #   TowerID -> MonsterGroupID -> T01.BattleReward。所以这里**不能**拿解出的值直接
    #   与 BattleReward 比（那是两个 id 空间）；等价判据 = "这条 TowerID 能解析到一个
    #   真实存在的官方奖励组"（表外/无 BattleReward -> 503，不编）。
    tid = _zzrc4_int(req.get('value'), t)
    if tid is None:
        return b'[581]', None
    mgid = _S51_TOWER_MG.get(tid)
    want = t01_battle_reward(mgid) if mgid else None
    if not want:
        print('[WAR_DROP] !! 9004 TowerID=%s -> MonsterGroup=%s -> 无官方奖励组 -> 503'
              % (tid, mgid), flush=True)
        return b'[503]', None
    return None, want


def _war_settle_once(mid, req):
    """9003/9004 的**一次性结算**（任务书 D①）。返回 `(ok, reason, target)`。

    · 9003：target = 请求的 `gateid`（探索大关卡）。会话由 1701 开、1706 关；
      `require_open=False`：这两个号的"开战"没有独立协议号，硬加前置会把正常游玩挡掉，
      但只要会话**开着且属于另一个大关卡**就会 `wrong_target`（跨关卡结算被拒）。
    · 9004：target = 密文里解出的 S51 TowerID。
    · 两者都用请求体指纹当**票号**：同一场战斗重发同一条包 -> `replay`，不再发奖。
    ⚠ 9003 的 `gateid` 在部分入口（世界图/秘境）可能为空：那时用 `info`（战斗组）当 target，
      只要它是个稳定正整数，一次性判据照样成立（target 只用来隔离"同一模式的不同局"）。
    ★★2026-10-16（补 D 的同类缺口）：`9001/9005/9006/9007` 也是"只信请求体"就会重复发奖，
      它们同样没有开战消息，所以走 `MODE_WAR`（target = 协议号），票号 = 请求体指纹。
    ⚠⚠ **这一层的边界必须写清**（见 DECISIONS 2026-10-16 同名条）：票号里含 `time`，
      而 `time` 是**客户端填的**。所以本函数挡的是"**同一条包**重发"，
      挡不住"同一场战斗改一位 `time` 再发"—— 后者需要服务端能自证"这局是它开的"，
      属架构级改动，本轮不做。**不要在任何文档里把这里写成"已防重放"。**
      下一轮真机验证要点：确认客户端 `time` 是"战斗开始时刻"还是"发包时刻"——
      若是前者，它本身就是一场战斗的稳定身份，票号不改就已经是"每场一次"。
    """
    if mid == 9003:
        tgt = _i(req.get('gateid'), 0) or _i(req.get('info'), 0)
        mode = 'explore'
    elif mid == 9004:
        tgt = _zzrc4_int(req.get('value'), req.get('time')) or 0
        mode = 'tower'
    else:                                  # 9001/9005/9006/9007：没有大关卡/塔层上下文
        tgt = _i(req.get('info'), 0) or _i(req.get('evalue'), 0) or mid
        mode = 'war'
    tok = '%s|%s|%s|%s|%s' % (mid, req.get('value'), req.get('time'),
                              req.get('info'), req.get('gateid'))
    try:
        from battle_session import settle as _settle
        ok, reason = _settle(mode, tgt, tok, require_open=False, consume=False)
    except Exception as e:                       # 状态机自己不会抛；这里是纵深防御
        print('[BS] !! 结算状态机异常(按未结算处理): %r' % (e,), flush=True)
        ok, reason = False, 'error'
    if not ok:
        print('[BS] %s 结算被拒: reason=%s target=%s（重放/跨关卡 -> 零奖励零状态变化）'
              % (mid, reason, tgt), flush=True)
    return ok, reason, tgt


def _egg_groups():
    """CT=23「妖魔肃清：在任意关卡中累计击败 3 只妖卵」的目标组。

    ★2026-10-16（任务书 E）：判据直接取 `T37_DailyTasks` 里 CT=23 那行的 `Value` 列
      （`2013|3013|4013|5014|6013|7014|12013|13013|14013|15014|16013|17014`）——
      这 13 个 id 与 `T01_MstrGroup` 的**行 id** 同一套空间（9003/9004 的 `info` 就是它）。
      表自己写明了"打谁算妖卵"，所以不编、也不用别名。
    """
    out = set()
    try:
        rows = _load_json('T37_DailyTasks.json')
        head = {c: k for k, c in enumerate(rows[0])}
        for r in rows[1:]:
            if _i(r[head['ConditionType']], 0) != 23:
                continue
            for part in str(r[head['Value']] or '').replace(',', '|').split('|'):
                part = part.strip()
                if part.isdigit():
                    out.add(int(part))
    except Exception:
        return set()
    return out


_EGG_GROUPS = _egg_groups()


# ★2026-10-16（E / CT=21）：`T26_YGRQ` 的关卡 -> 总波数（`TotalRound`，48 行全是 7）。
#   这张表就是客户端 `StageDelegateAggress.lua:56-59` 用来画"第几波/共几波"的那张。
_T26_ROUNDS = {}
try:
    _t = _load_json('T26_YGRQ.json')
    _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _T26_ROUNDS[_i(_r[_h['ID']])] = _i(_r[_h['TotalRound']], 0)
        except Exception:
            continue
except Exception:
    pass


def _legend_groups_today():
    """「今日妖神」的战斗组集合（CT=5 的判据）。

    ★2026-10-16（任务书 E）：9005（BOSS 结算）**不专属于妖神** —— 客户端
      `WarStageUnits.lua` 的 BOSS 分支也可能承载别的 BOSS。而 `T25_YSCSSX` 按天轮换，
      每天那一行的 `MonsterGroup`~`MonsterGroup5`（5 档难度）就是"今天的妖神打哪几个组"。
      ⇒ 只有 `info` 命中**今日这一行的 5 个组**才计 CT=5（宁可不涨，也不把别的 BOSS 算成妖神）。
    """
    try:
        row = _t25_today_row()
        if not row:
            return set()
        return {_i(row.get(c), 0) for c in _T25_GROUP_COLS if _i(row.get(c), 0)}
    except Exception:
        return set()


def _war_ct_bump(mid, req, iswin):
    """9001/9003/9004/9005/9006 胜利结算 -> 对应的每日任务 ConditionType。

    只在**真正结算成功**之后调用（重放/伪造包在 `_war_settle_once` 就被挡掉了，
    所以这里不会重复计数）；每次 `n=1`（T37 的 Quantity 是"次数"，不是"战斗数"）。
    """
    if not iswin:
        return
    try:
        _info = _i(req.get('info'), 0)
        if mid == 9001:                       # 妖怪入侵战斗结算（AGGRESS_RESULT）
            bump_task(CT_AGGRESS, 1)
            # ★2026-10-16（E / CT=21）：「通关妖怪入侵关卡(7波)1次」。
            #   判据：本局**最后一次上报的波数** ≥ 该关卡的 `T26_YGRQ.TotalRound`（48 行都是 7），
            #   关卡 id 来自 1010 的 `pointid`（客户端 `NetAggressWave.lua:52` 明确写"关卡id"）。
            _ag = EQ_STATE.get('aggress_wave') or {}
            _lv = _i(_ag.get('pointid'), 0)
            _wv = _i(_ag.get('last_wave'), 0)
            _tot = _T26_ROUNDS.get(_lv, 0)
            if _tot and _wv >= _tot:
                bump_task(CT_AGGRESS_CLEAR, 1)
                print('[CT21] 妖怪入侵 7 波通关 level=%s wave=%s/%s' % (_lv, _wv, _tot),
                      flush=True)
            else:
                print('[CT21] 未到最后一波 level=%s wave=%s total=%s -> 不计'
                      % (_lv, _wv, _tot), flush=True)
        elif mid == 9006:                      # 妖魔异闻（PVE 结算）
            bump_task(CT_YMYW, 1)
            # ★★2026-10-23：UG 的 CT=72 **不是**"打了几次妖魔异闻"，而是"击败了哪个 BOSS"——
            #   `UG_07_Task` 里 CT=72 共 14 行（主线 51015 + 支线 13 行），`Value` 就是
            #   `T24_YMYW.BattleId`（实测 14 行 / 14 个 Value **一一对应、无重无缺**；
            #   1011 怒目鬼→30001，与 51015「[简单]-怒目鬼」的文案对得上）。
            #   修前 14 行共用 `ug_ct['72']` ⇒ 打一只 BOSS 一次点亮 14 条任务；
            #   本轮改成按 Value 记账后若不补驱动，这 13 条支线会全部停住（= 回归）。
            #   ⇒ 在**同一处**按 id→BattleId 精确记：`_ymyw_battle_id` 已校验 id 与 info 同组。
            try:
                _bid72 = _ymyw_battle_id(req)
                if _bid72:
                    from handlers_ugtask import bump_ug_ctv as _b72
                    if _b72(72, _bid72, 1):
                        print('[UG72] 妖魔异闻 id=%s -> BattleId=%s -> CT=72 目标 +1'
                              % (req.get('id'), _bid72), flush=True)
            except Exception as _e72:
                print('[UG72] !! 计数失败(不影响结算): %r' % (_e72,), flush=True)
        elif mid == 9005:                      # BOSS 结算：只认"今日妖神"的那几个组
            if _info and _info in _legend_groups_today():
                bump_task(CT_LEGEND, 1)
            else:
                print('[CT5] 9005 info=%s 不在今日妖神组 -> 不计妖神传说' % _info, flush=True)
        if mid in (9003, 9004) and _info:
            if _info in _EGG_GROUPS:
                bump_task(CT_EGG, 1)           # T37 每日「妖魔肃清：击败妖卵」
            # ★★2026-10-22（审查判红 B2）：UG 的 CT=23 里**不是 BOSS** 的那些行
            #   （支线 22011「在《太白道观》击败异界妖卵」Value=2013、
            #    `1001|1002|1003|1004` 这类小怪组）在这里按 `info` 记自己的格子。
            #   **BOSS 组不在这里记** —— 那由 `_xp_ct3_bump` 负责（那边有 gate 归属校验
            #   和"每 run 一次"的去重），两处都记会翻倍。
            _g23 = _i(req.get('gateid'), 0)
            if _info not in _XP_BOSS_GROUPS.get(_g23, ()):
                try:
                    from handlers_ugtask import bump_ug_ctv as _b23
                    if _b23(23, _info, 1):
                        print('[UG23] 非 BOSS 目标 info=%s -> CT=23 目标 +1' % _info,
                              flush=True)
                except Exception as _e23:
                    print('[UG23] !! 非 BOSS 目标计数失败: %r' % (_e23,), flush=True)
    except Exception as _e:
        print('[CT] !! 战斗任务计数失败(不影响结算): %r' % (_e,), flush=True)


def handle_war_result(body_json, mid=None, _meta=None):
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    try:
        iswin = int(req.get('result') or 0) == 0
    except Exception:
        iswin = False
    if _meta is not None:
        _meta['settled'] = False
    # ★★2026-10-16（任务书 D②③ + 本轮补的同类缺口）：胜利结算必须过"一次性"这道闸。
    #   顺序很重要：校验/去重都在**任何状态修改之前**，所以重放与伪造包在这里就返回，
    #   `battle_cleared`（A01 活动）、探索解锁度、怪物经验、金币、每日任务 CT 全都不会被二次写入。
    #   · 9003/9004/9005/9006：**来源校验**（9003/9005/9006 的 value 必须等于
    #     T01[info].BattleReward 或 ObjectPosition；9006 的 `id` 先经 T24_YMYW 换算成 T01 行）+ 一次性；
    #   · 9001：`evalue` 是明文**评价奖励组**；来源闸 = 必须是真实 T02 组
    #     （0/表外/乱码 -> 成功 + 空掉落，**不再落 500/50 兜底**）。
    #   · 9007：`value` 必须是某个唯一 T01 行的 ObjectPosition；合法但该行没配奖励
    #     -> 哨兵 0（空掉落、不兜底）；反查不到 -> 504。
    #   两者都仍只保证"**同一条包**不重复发"（票号含客户端 `time`），不是架构级防重放。
    _gid_checked = None
    _no_reward = False
    if mid in (9001, 9003, 9004, 9005, 9006, 9007) and iswin:
        if mid in (9001, 9003, 9004, 9005, 9006, 9007):
            _err, _gid_checked = _war_drop_group_checked(mid, req)
            if _err is not None:
                return _err
            # `0` 是 **9001/9007** 的哨兵：9001 = evalue 不是真实 T02 组（0/表外/乱码）；
            #   9007 = 来源合法但该 T01 行官方没配 BattleReward。
            #   ⇒ 结算照常成功、掉落为空，**不走兜底**（兜底只留给"压根无官方组可依"）。
            _no_reward = (_gid_checked == 0)
        _ok, _reason, _tgt = _war_settle_once(mid, req)
        if not _ok:
            # 重复/跨关卡：给**成功应答 + 空掉落**（客户端不该弹错，但一件东西都不发）
            # ★2026-10-16（逐槽对客户端）：战场结算是**五槽**
            #   （`NetWarResult/NetBossResult/NetPVEResult/NetAggressResult` 都读
            #     `msg[1]=roleInfo / 2=ourFormation / 3=dropInfo / 4=talentId / 5=exDropItems`），
            #   被拒路径也保持 5 槽（同一协议任何情况同槽数，本仓 7009 那条已经踩过一次）。
            return json.dumps([1, [_role_info(), [], _drop([]), 0, _drop([])]],
                              separators=(',', ':')).encode('utf-8')
        if _meta is not None:
            _meta['settled'] = True
    # ★v60: 记录"已通关的关卡BOSS" —— A01 活动组 **108 / 109 / 110** 的**同一个**计数源。
    #   口径(全实测):
    #     · 108 探索寻BOSS: Target **就是 T01_MstrGroup 行 id**(1011 <怒目鬼> / 2012 …),
    #       Desc "普通-<怪名>" -> "把这一关的 BOSS 打掉"。
    #     · 109 妖神封印  : Target **也是 T01_MstrGroup 行 id**(500131 "<毕方>难度1" / 500241 <涂山九尾>…),
    #       Desc "击杀毕方/刑天/腾蛇/帝江…"(500131~500241 段 = 妖神难度组, 已逐行核对)。
    #     · 110 妖魔异闻  : Target 是 T24_YMYW.IDGroup(101..107); 该组的 BattleId **14/14 都是**
    #       T01_MstrGroup 真实行(30001/30101/…) -> 打赢那个战斗组就算这个 IDGroup 通关。
    #   ⇒ 三个活动都只需要"这场仗打的哪个 T01 战斗组"这一件事, 所以只记一个集合就够了。
    #   钩子: 本函数(handle_war_result)只服务 **9001/9003/9005/9006/9007** 五个战场
    #     (9004 妖塔走 handlers_tower.handle_tower_war_result, 不在这里) ——
    #     ⚠ 9003/9005/9006/9007 的客户端都送 `msg["info"]=_warInfo`（
    #       NetWarResult/NetBossResult/NetPVEResult 逐个核对过）；**9001 不送**
    #       （`NetAggressResult.lua:28-54` 只有 `pointid`）⇒ 9001 不会写 `battle_cleared`。
    #   只在**胜利**且 info 是 T01 真实行时记(invalid 不记, 免污染; 幂等)。
    if iswin and mid is not None:
        try:
            # ★2026-10-18：9006 的 `info` 是 T24_YMYW.IDGroup(101..107)，不是 T01 行
            #   ⇒ 直接记会把 "107" 写进 battle_cleared（A01 组 108/109/110 永远不亮）。
            #   这里先换算成 T24[id].BattleId（30113 这类真实 T01 行）再记。
            if mid == 9006:
                _cg = str(_ymyw_battle_id(req) or '')
            else:
                _cg = str(req.get('info') or '')
            if _cg and is_valid_war_group(_cg):
                if _cg not in EQ_STATE.setdefault('battle_cleared', {}):
                    EQ_STATE['battle_cleared'][_cg] = 1
                    print('[ACT108] 已通关战斗组: %s (mid=%s)' % (_cg, mid), flush=True)
        except Exception as _e:
            print('[ACT108] !! 记录通关战斗组失败(不影响结算): %r' % (_e,), flush=True)
    # ★★2026-10-08（探索链轮）：把这场胜利推进到**探索解锁度**。
    #   只有 9003 带 `gateid`（= 大关卡）与 `exploreid`（= 客户端算好的"已满足条件 id 串"），
    #   两者逐行取自 `NetWarResult.lua:70/76-85`；其余战斗（9005/9006/9007）没有大关卡上下文，
    #   不做探索进度（它们的 `value` 是 ObjectPosition，与解锁度无关）。
    if mid == 9003 and iswin:
        try:
            _bg = _i(req.get('gateid'), 0)
            _cids = [_i(x) for x in str(req.get('exploreid') or '').split('&') if _i(x)]
            if _bg in _XP_GATES:
                _ids, _deg = _xp_war_victory(_bg, _i(req.get('info'), 0), True, _cids)
                print('[XP1705] 9003 gate=%s info=%s 已满足=%s 解锁度=%d%%'
                      % (_bg, req.get('info'), _ids, _deg), flush=True)
                # ★2026-10-18（CT=3 口径 A）：本场若是该关 S12 Type==1 的 BOSS 组，
                #   记一次"通关"（每个 explore run 最多 1 次）。放在 `_xp_war_victory`
                #   之后 = 解锁度/条件先落账，任务计数再读同一份状态。
                _xp_ct3_bump(_bg, _i(req.get('info'), 0))
        except Exception as _e:
            print('[XP1705] !! 记录探索解锁度失败(不影响结算): %r' % (_e,), flush=True)
    # 1) 我方怪物经验结算 + 更新后阵容
    formation = []
    for m in (req.get('ourside') or []):
        try:
            uc = str(m.get('ucode') or '')
            addexp = int(m.get('exp') or 0)
        except Exception:
            continue
        _gain_monster_exp(uc, addexp if iswin else 0)
        mi = _mon_with_state(uc) if uc in EQ_STATE['monster_ueq'] or get_monster_base(uc) else None
        if mi:
            formation.append(json.loads(mi))
    # 2) 奖励: ★v54c 优先走**官方掉落表** (T01_MstrGroup.BattleReward -> T02 组, 按 Rate 判定)。
    #    ★★2026-10-16（任务书 D②）：**官方组抽空保持空** —— 兜底只留给"压根取不到官方组"的模式。
    #      修前判据是 `if not dropgroups:`，于是"官方组这次一条都没抽中"（T02 的
    #      MaximumNumber+Rate 语义下完全合法）也会被补上死值 500 金 / 50 魂。
    dropgroups = []
    _gid = None
    if iswin and mid is not None:
        _gid = _gid_checked or official_drop_group(mid, req)
        if _gid:
            _g, _ok = _grant_group(_gid, roll=True, mult=REWARD_MULT)
            if _ok:
                dropgroups = _g or []
                print('[WAR_DROP] %s 官方掉落组=%s -> %d 条 (mult=%d)'
                      % (mid, _gid, len(_g or []), REWARD_MULT))
            else:
                print('[WAR_DROP] !! %s 官方掉落组=%s 发放失败(_ok=False) -> 空掉落(不兜底)'
                      % (mid, _gid), flush=True)
    if not _gid and not _no_reward:
        # 私服兜底：2026-10-18 起**只剩两条路** —— ①战败的 +20/+5；②`mid=None` 的旧调用路径。
        #   胜利且带协议号的六个战场已全部"来源可证"：9001 的 evalue 必须是真实 T02 组、
        #   9003/9004/9005/9006 的坏包在 `_war_drop_group_checked` 里先拒、9007 合法但无组
        #   走 `_no_reward` ⇒ **伪造的胜利包不可能再触发 +500/+50**。
        gold = 500 if iswin else 20
        soul = 50 if iswin else 5
        EQ_STATE['gold'] += gold
        EQ_STATE['soulcoin'] += soul
        dropgroups = [[3, [[gold]]], [7, [[soul]]]]
        # ★v54e: 私服老掉落(战斗胜利 50% 残章) **只在兜底路径**保留。
        #   官方表命中时不再叠加 —— 用户 2026-09-15 拍板"完全还原官方";
        #   而官方 T02 里本来就有 93 个组含残章(其中 78 个被 BattleReward 引用, 掉率 0.03~0.13),
        #   所以去掉这个私服拐杖不会让技能合成断粮, 只是残章回归"稀有"。
        if iswin and random.random() < 0.5:
            g = _grant_scrow(1)
            if g:
                dropgroups.append(g)
    # ★2026-10-16（任务书 E）：结算成功之后才计每日任务里的"战斗类" ConditionType。
    #   位置在 `_grant_group` 之后、`save_state` 之前 —— 发放失败也不会漏计（战斗确实打完了），
    #   而重放/伪造包在这之前就 return 了，结构上不可能重复计数。
    _war_ct_bump(mid, req, iswin)
    # ★2026-10-11 (3500): 本次探索 run 里的 9003 战斗结算也计入"本次探索累计获得"。
    #   只认 9003（唯一带大关卡上下文的战斗号，见上 `_xp_war_victory` 同源注释），
    #   且必须是当前账本那个大关卡；其余战斗（9005/9006/9007/9001/妖塔）不碰账本。
    if mid == 9003 and dropgroups:
        try:
            if _i(req.get('gateid'), 0) == _i(_xp_run_state().get('gate'), 0) and _i(req.get('gateid'), 0):
                _xp_ledger_add(dropgroups)
        except Exception as _e:
            print('[XP3500] !! 战斗掉落并入本次探索账本失败(不影响结算): %r' % (_e,), flush=True)
    player_add_exp(300 if iswin else 0)      # 玩家经验 (L101 表 Lv1->2 仅需10, 打几场即可达10级测PVP/进化)
    save_state()
    # ★2026-10-16（逐槽对客户端）：**五槽**。四个客户端类读的是同一套
    #   （`NetWarResult.lua` / `NetBossResult.lua` / `NetPVEResult.lua` / `NetAggressResult.lua`）：
    #     msg[1]=roleInfo / msg[2]=ourFormation / msg[3]=dropInfo / msg[4]=talentId / msg[5]=exDropItems
    #   修前只回 3 槽 ⇒ msg[4]/msg[5] 恒 nil。本轮核过：本装机的 `talentId` 读完**没有任何使用点**、
    #   `exDropItems` 有 `next()` 守卫，所以**当前不会崩也不会丢奖励**（奖励在 msg[3]）。
    #   但同一"少槽"形态刚刚在 7009 上让"赢来的钱从不显示"，所以这里统一补成 5 槽：
    #   服务端没有天赋掉落（0）与额外掉落（空 dropInfo），槽位固定后客户端升级也不会掉进另一分支。
    return json.dumps([1, [_role_info(), formation, _drop(dropgroups), 0, _drop([])]],
                      separators=(',', ':')).encode('utf-8')

def war_result_handler(mid):
    # 把协议号绑进共用处理器: 9001/9003/9005/9006/9007 的 `value` 字段**语义不同**
    #   (9003/8006 是奖励组 id, 9004 是 S51 TowerID, 9005+ 是 ObjectPosition), 共用函数必须知道自己是哪条。
    def _h(body_json):
        return handle_war_result(body_json, mid)
    _h.__name__ = 'handle_war_result_%d' % mid
    return _h

# ============ 任务/成就 1029 / 1041-1048 ============
_TASK = [[1, '主线·初入妖界', 1], [2, '收集妖怪', 2]]
def handle_task_info(body_json=None):
    # resp: unlockInfo/list
    return _simple_resp(_TASK).encode('utf-8')

def handle_task_get(body_json):
    # req {taskid}; resp: 无逐字段 -> 直接透传 [1, ...]
    return _rd_resp([[3, [[100]]]]).encode('utf-8')

# ============ ★v57: 1029 语义分流（皮肤列表，兼容新客户端） ============
# 背景（v57 §12.5 / 铁律 #188「同名换义最阴」）：
#   other version : 1029 = TASK_INFO（任务列表）  —— 但客户端 NetTaskInfo:sendMessage 里有 `return`，
#               **该分支永不发送**（服务端注释"1029 在客户端已被禁用"因此成立）。
#   新包1.30  : 1029 = GETSKIN_LIST（皮肤列表）—— ★SuccessEnterGame.lua:45 **每次进游戏无条件发**。
# 分派判据：
#   两条请求的 body 都是空表 `{}`，**无法靠 body 区分**；但发送时机完全不同 ——
#   老客户端只有"打开任务列表"才发（而那条路径被 return 掉了 ⇒ 实际不发），
#   新客户端进游戏必发。⇒ 直接把 1029 解释为 GETSKIN_LIST 对**两个客户端都安全**（老客户端不发它）。
# 响应契约（NetSkinGetList:recvMessage 逐行）：
#   [1, [ [ucode, 皮肤静态id], ... ]]
#   · 客户端遍历 msg[1]，用 data[i][2] 与 L91_Status.Skin 比对
#   · **只把"非源皮肤"(Skin 列 != 该值) 的条目** insertItem 进 SQManager:skin_getMe()
#   ⇒ 形状不对会让 data[i][2] 索引非法 -> Lua 抛错 -> Socket tick 中断 -> 进游戏卡死（铁律 #165）
_L91_SKIN_CACHE = None

def _l91_skin_map():
    """{静态妖怪id(str): L91_Status.Skin} —— 客户端用它判断"是不是源皮肤" """
    global _L91_SKIN_CACHE
    if _L91_SKIN_CACHE is None:
        m = {}
        try:
            t = _state_mod.load_table('L91_Status.json')
            h = {c: i for i, c in enumerate(t[0])}
            ci, cs = h['ID'], h['Skin']
            for r in t[1:]:
                if len(r) > max(ci, cs):
                    m[str(r[ci])] = str(r[cs])
        except Exception as e:
            print('[SKIN1029] 读 L91_Status 失败:', e)
        _L91_SKIN_CACHE = m
    return _L91_SKIN_CACHE

# ★皮肤归属表：L98_Monsterskin 的 MonsterID 列是逗号分隔的"可穿该皮肤的静态妖怪id"列表
#   （与 handlers_sys 的 _L98_FAM 同源）。1029 必须用它做**归属校验**，
#   否则 skin_owned 里的无关皮肤会被挂到别的妖怪身上（单测已抓到 30063 串台）。
_L98_FAM_CACHE = None

def _l98_fam():
    """{皮肤id(str): set(静态妖怪id str)}"""
    global _L98_FAM_CACHE
    if _L98_FAM_CACHE is None:
        fam = {}
        try:
            t = _state_mod.load_table('L98_Monsterskin.json')
            h = {c: i for i, c in enumerate(t[0])}
            ci = h['ID']
            cm = h['MonsterID'] if 'MonsterID' in h else None
            for r in t[1:]:
                if len(r) <= ci:
                    continue
                sid = str(r[ci])
                mids = set()
                if cm is not None and len(r) > cm:
                    mids = {x.strip() for x in str(r[cm]).split(',') if x.strip()}
                fam[sid] = mids
        except Exception as e:
            print('[SKIN1029] 读 L98_Monsterskin 失败:', e)
        _L98_FAM_CACHE = fam
    return _L98_FAM_CACHE

def _owned_skin_pairs():
    """已解锁皮肤表 -> [[ucode, 皮肤静态id], ...]（同 ucode 去重，按 ucode 排序）

    选取规则（与客户端 1500 的皮肤列表口径一致）：
      · get_monster_base(ucode) -> (静态id, 基础皮肤id, ...)   ← base[0]=静态id, base[1]=基皮
      · 基础皮肤永远可用
      · 已解锁皮肤(monster_skin_open) + 已拥有皮肤(skin_owned)
        必须经 L98_Monsterskin.MonsterID 校验**归属该妖怪**才计入
    取值：优先"当前穿戴"，否则取归属集合里最大的皮肤 id
    """
    pairs = []
    fam = _l98_fam()
    try:
        for ucode in all_monster_ucodes():
            us = str(ucode)
            b = get_monster_base(ucode)
            if not b:
                continue
            mid = str(b[0])                       # ★base[0] = 静态妖怪 id（base[1] 是皮肤 id）
            bk = int(b[1]) if len(b) > 1 and str(b[1]).strip() else 0
            cand = set()
            if bk:
                cand.add(bk)
            for s in EQ_STATE.get('monster_skin_open', {}).get(us, []):
                try:
                    cand.add(int(s))
                except Exception:
                    pass
            for s in (EQ_STATE.get('skin_owned') or {}):
                s = str(s)
                # ★归属校验：只有 MonsterID 含本怪静态 id 的皮肤才允许挂上来
                if mid in (fam.get(s) or set()):
                    try:
                        cand.add(int(s))
                    except Exception:
                        pass
            eq = int((EQ_STATE.get('monster_skin_eq') or {}).get(us) or 0)
            if eq and eq in cand:
                pairs.append([us, eq])
            elif cand:
                pairs.append([us, max(cand)])
    except Exception as e:
        print('[SKIN1029] 组装皮肤表失败:', e)
    return sorted(pairs, key=lambda x: int(x[0]) if str(x[0]).isdigit() else 0)

def handle_skin_get_list(body_json=None):
    """1029 GETSKIN_LIST（新客户端进游戏无条件调用）"""
    try:
        pairs = _owned_skin_pairs()
        # ★★2026-10-18（真机 crash：`NetSkinGetList.lua:36 attempt to get length of
        #   local 'data' (a number value)`）：1029 的契约是 `[1, [rows]]` ——
        #   客户端 `data = msg[1]` 后立刻 `#data`，msg 是响应的**第二槽**。
        #   修前回 `[1,[1,rows]]` ⇒ msg=[1,rows] ⇒ msg[1]=1（数字）⇒ 登录后第一次
        #   打开队伍/皮肤相关界面就抛错。旧实现还多留了一层 `data=[1,pairs]`。
        body = json.dumps([pairs], separators=(',', ':'))
        print('[SKIN1029] 1029 -> 皮肤列表 %d 条 (前 5: %s)' % (len(pairs), pairs[:5]))
        return ('[1,' + body + ']').encode('utf-8')
    except Exception as e:
        # 兜底也必须**形状合法**：msg[1] 必须是数组（#165）
        print('[SKIN1029] 异常, 回合法空形状:', e)
        return b'[1,[[]]]'

# ============ 探索 1701-1706 (2026-09-06 按 NetExplore* 契约; 体力 = 道具1006) ============
def _vitality_count():
    return int(EQ_STATE['prop'].get('1006', 0))

def _spend_vitality(n=1):
    have = _vitality_count()
    if have < n:
        return False
    EQ_STATE['prop']['1006'] = have - n
    # ★2026-10-12: T37 每日任务 CT=25「总共消耗 60 点体力」的唯一累计点。
    #   放在**扣减出口**而不是各个调用点：本函数是体力扣减的唯一实现（pvp 那条别的路径
    #   走 `handlers_pvp` 的同名出口），在这里记一次 = 结构上不可能漏记/重复记。
    #   懒 import + 吞异常：任务进度再重要也不能把"进关扣体力"这条主链搞崩。
    try:
        from handlers_task import bump_task, CT_VITALITY
        bump_task(CT_VITALITY, n)
    except Exception:
        pass
    return True

def _xp_energy_cost(gate):
    """进入该大关卡要扣几点体力 = `S01_Level.EnergyCost`（表内有这一列，20 行齐）。

    ★2026-10-16（任务书 A1 ①）：修前 1701 恒扣 1 —— 而表里 40001/140001 等都是 **5**，
    只有新手关 40004 是 1。取不到该行/该列时回 1（= 改造前的行为），**不编数**。
    """
    r = _XP_S01_ROW.get(_i(gate, 0))
    if not r or 'EnergyCost' not in _s1h:
        return 1
    v = _i(r[_s1h['EnergyCost']], 0)
    return v if v > 0 else 1


def handle_explore_enter(body_json):
    # req {type, id, tab}; resp [1,[roleInfo]]; tab=Yes/1 进关消耗体力 (516=体力不够, 507=id错)
    #   ★2026-10-08（探索链轮）：`type=1`(NWAR_TYPE.EXPLORE) 时按 id（= 大关卡 id）**开始一次探索**
    #     —— 本次探索计数（S12 的 Type 4/6/8/10）归零，与客户端 `resertExploreDegree()` 同款。
    #     妖塔(3)/其它 type 不动探索进度（它们的 id 是 S51/S58 的号，不是大关卡）。
    #   ★★2026-10-16（任务书 A1 ①）：**先扣费、扣费成功才开局**。修前顺序是
    #     `_xp_enter(gate)` -> `_xp_begin_run(gate)` -> 才 `_spend_vitality(1)`：
    #     体力不够时玩家拿到 516，但**本次探索已经被重置**（上局的罐子/宝箱记录、run 号、
    #     账本全清）—— 也就是"没钱也能把进度洗一次"。顺序反了才与客户端语义一致。
    #   扣费点数按 `S01_Level.EnergyCost`（见 `_xp_energy_cost`），不再是恒定 1。
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[507]'
    if req.get('id') is None:
        return b'[507]'
    _type = _i(req.get('type'), 1)
    _gate = _i(req.get('id'), 0)
    _tab = str(req.get('tab') or '') in ('1', 'true', 'Yes', 'yes')
    if _type == 1:
        # ★2026-11-06（真机）：夜市 `1164` 成功后客户端用 `1701 {type:1, id:30001}` 进夜市地图
        #   （`VNMarketHome:enterExploreScene` 固定 mapSecondID=30001，S03_Spawn 的场景号，
        #   不在 S12_SceneExplore 的大关卡表里）。修前这里直接回 508「进入关卡不存在」，
        #   客户端弹「进入关卡不存在」并把玩家卡在夜市首页。
        #   夜市地图不消耗体力、不重置大关卡探索计数，只开一个 `explore` 会话（给 1704/9003 用）。
        if _gate == 30001:
            try:
                from battle_session import open_session as _bs_open_nm
                _bs_open_nm('nightmarket', _gate)
            except Exception as _e:
                print('[BS] !! 开夜市会话失败(不影响进图): %r' % (_e,), flush=True)
            return json.dumps([1, [_role_info()]], separators=(',', ':')).encode('utf-8')
        if _gate not in _XP_GATES:
            return b'[508]'                   # 进入关卡不存在
        _cost = _xp_energy_cost(_gate)
        if _tab and not _spend_vitality(_cost):
            return b'[516]'                   # ★扣费失败 -> 不动任何探索状态
        _xp_enter(_gate)
        _xp_begin_run(_gate)                  # ★2026-10-11: 本次探索的"累计获得"账本归零
        # ★2026-10-16（A 交付的状态机接口）：进关 = 开一局 explore 会话，1706 退出时关掉。
        #   9003 的"一次性结算"（D 包）以它为前提；失败不影响主链（状态机自己吞异常）。
        try:
            from battle_session import open_session as _bs_open
            _bs_open('explore', _gate)
        except Exception as _e:
            print('[BS] !! 开探索会话失败(不影响进关): %r' % (_e,), flush=True)
        save_state()
        return json.dumps([1, [_role_info()]], separators=(',', ':')).encode('utf-8')
    if _tab:
        # 非探索类（妖塔 3 / 妖魔异闻 5 / 妖怪入侵 6…）：表里没有它们的 EnergyCost，仍按 1 点。
        if not _spend_vitality(1):
            return b'[516]'
        save_state()
    return json.dumps([1, [_role_info()]], separators=(',', ':')).encode('utf-8')

# 妖魔异闻（T24_YMYW）—— 1702 `type=5` 的 id 与 9006 的 `id/info` 都在这张表的号段里。
# ---------------------------------------------------------------------------
# ★★2026-10-18（真机 1702 取证）：修前这里按 `S04_SenenMonster.EnergyCost` 查 **7 位怪物 id**
#   （400101…），而真机 1702 送的是 **4 位 `T24_YMYW.ID`（`{"type":5,"id":"1072"}`）**
#   ⇒ 永远查不到、退回 1（用户看到的"妖魔异闻只扣 1 点体力"）。
#   客户端进关按钮显示的两列就是这张表的两列（`app/pve/PVEView.lua`）：
#     · 体力消耗 `LsetString(Text_CostVitality, monster_config.StrengthValue)`（:355）
#     · 门票消耗 `LsetString(Text_CostRune, monster_config.TicketValue)`（:358）
#     · 门票图标/拥有数 `SQManager:prop_getCount(1017)`（:77）
#   ⇒ 体力 = `StrengthValue`，门票 = `TicketID/TicketValue`（14 行全是 1017×1）。
#   `T24_BY_ID` / `T24_BY_GROUP` 在下方 `_PVE_GROUPS` 那段一起加载（表只读一次）。
def _pve_energy_cost(sid):
    """妖魔异闻 1702(type=5) 的体力消耗 = `T24_YMYW.StrengthValue`；表外 -> 1（不编）。"""
    try:
        r = _T24_BY_ID.get(str(sid))
        if r:
            return max(1, _i(r[_h24['StrengthValue']], 1))
    except Exception:
        pass
    return 1


def _prop_ucode(pid):
    """道具静态 id -> 背包 ucode（`EQ_STATE['prop']` 的键）。

    通常 ucode == 静态 id（`grant_one` ty==2 就这么写），但 `prop_id` 才是权威映射；
    先认映射、再退回"键就是静态 id"（老档/测试档常见）。
    """
    key = str(_i(pid, 0))
    props = EQ_STATE.get('prop') or {}
    pids = EQ_STATE.get('prop_id') or {}
    if key in props and str(pids.get(key, key)) == key:
        return key
    for uc, sid in pids.items():
        if str(sid) == key:
            return str(uc)
    if key in props:
        return key
    return None


def _prop_take(pid, n):
    """扣道具 n 个；够 -> `[ucode, 静态id, 剩余]`，不够/没有 -> None（一分不动）。"""
    n = _i(n, 0)
    if n <= 0:
        return None
    uc = _prop_ucode(pid)
    if uc is None:
        return None
    props = EQ_STATE.setdefault('prop', {})
    have = _i(props.get(uc), 0)
    if have < n:
        return None
    left = have - n
    if left > 0:
        props[uc] = left
    else:
        props.pop(uc, None)
        (EQ_STATE.get('prop_id') or {}).pop(uc, None)
    return [uc, _i(pid, 0), left]


def _prop_refund(uc, pid, n):
    """把刚扣掉的 n 个道具原路退回（`_prop_take` 的逆操作；只在扣费第二步失败时用）。"""
    props = EQ_STATE.setdefault('prop', {})
    uc = str(uc)
    props[uc] = _i(props.get(uc), 0) + _i(n, 0)
    EQ_STATE.setdefault('prop_id', {}).setdefault(uc, _i(pid, 0))
    return props[uc]


# 妖主大入侵门票：道具 1025「妖主封印符」，单价 = `T23_Function[ID=186].Value`（=1）。
#   客户端 `InvadeHome:updateUI:235-238` 读的就是 T23 ID=186 这一行做"持有/需要"显示，
#   服务端必须用同一行，否则会出现"客户端显示够、服务端说不够"。
_A05_TICKET_COST = None


def _a05_ticket_cost():
    global _A05_TICKET_COST
    if _A05_TICKET_COST is None:
        _A05_TICKET_COST = 1
        try:
            _t = _load_json('T23_Function.json')
            _h = {c: i for i, c in enumerate(_t[0])}
            for _r in _t[1:]:
                if str(_r[_h['ID']]) == '186':
                    _v = _i(_r[_h['Value']], 1)
                    if _v > 0:
                        _A05_TICKET_COST = _v
                    break
        except Exception:
            pass
    return _A05_TICKET_COST


def handle_explore_vitality(body_json):
    # req {type, id}; resp [1,[roleInfo, removeProp]]; 扣体力 (513=体力不够)
    #   type: 1探索 3妖塔 5妖魔异闻 6妖怪入侵(每日免费1次/组, 次数扣减持久化; 556=已无次数)
    # ★★2026-10-18（真机："妖魔异闻没有实装正确的体力消耗"）：修前 type=5 一视同仁扣
    #   **1 点**；现在按 T24_YMYW.StrengthValue 扣（客户端 PVEView.lua:355 显示的就是这一列），
    #   并按 TicketID/TicketValue 扣门票；表外 id -> 512（不编）。
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict) or req.get('id') is None:
        return b'[511]'
    if str(req.get('type')) == '5':
        # ★2026-10-18：真机 1702 送 `{"type":5,"id":"1072"}` = T24_YMYW.ID
        #   （不是 S04 的 7 位怪物 id）。体力列是 `StrengthValue`，门票列是
        #   `TicketID/TicketValue`（PVEView.lua:355/358/77）。
        _row = _t24_row(req.get('id'))
        if _row is None:
            return b'[512]'                   # 关卡 id 不存在
        _need_lv = _i(_row.get('Level'), 0)
        if _need_lv > 0 and _i(EQ_STATE.get('player_lv', 1), 1) < _need_lv:
            return b'[554]'                   # 等级不足
        _cost = _pve_energy_cost(str(req.get('id')))
        _tid = _i(_row.get('TicketID'), 0)
        _tnum = _i(_row.get('TicketValue'), 0)
        # ★★2026-10-18（审计 F4）：顺序必须是"**体力先判、门票先扣**"。
        #   修前是 `_spend_vitality` 之后才 `_prop_take`，且 take 返回 None 时**不中止**
        #   （落到 `_ticket_item or ['1006', ...]` 照样回成功）—— 预检与真扣之间状态一变，
        #   就是"体力扣了、门票没扣、玩家看到成功"。现在：
        #     ① 体力不足 -> 513（门票一分不动）；
        #     ② 取票失败 -> 518（体力一分不动）；
        #     ③ 取票成功但扣体力失败（理论上不可达）-> 先退票再回 513。
        if _vitality_count() < _cost:
            return b'[513]'                   # 体力不足
        _ticket_item = None
        if _tid > 0 and _tnum > 0:
            _ticket_item = _prop_take(_tid, _tnum)
            if _ticket_item is None:
                return b'[518]'               # 门票不足 -> 绝不允许只扣体力
        if not _spend_vitality(_cost):
            if _ticket_item is not None:
                _prop_refund(_ticket_item[0], _ticket_item[1], _tnum)
            return b'[513]'
        save_state()
        rem = _vitality_count()
        # 门票有扣 -> removeProp 发门票（客户端 `updateItem(removeProp[1], removeProp)`）；
        # 没门票的关卡才回体力道具 1006（与 1701/妖塔同形）。
        return json.dumps([1, [_role_info(), _ticket_item or ['1006', 1006, rem]]],
                          separators=(',', ':')).encode('utf-8')
    if str(req.get('type')) == '6':
        _sid = str(req.get('id'))
        _gid = _T26_ID_GROUP.get(_sid)
        if _gid is None:
            return b'[512]'                   # 关卡 id 不存在
        _a = _aggr_reset()
        _used = int((_a['used'] or {}).get(_gid, 0))
        if _used >= 1:
            return b'[556]'                   # 已经没有挑战次数
        if not _spend_vitality(1):
            return b'[513]'
        _a['used'][_gid] = _used + 1
        save_state()
        rem = _vitality_count()
        return json.dumps([1, [_role_info(), ['1006', 1006, rem]]], separators=(',', ':')).encode('utf-8')
    if str(req.get('type')) == '7':
        # ★★2026-11-07（内容收口）妖主大入侵开战：客户端 `InvadeHome.startBattle`
        #   发 1702 `{type: NVITALITY_TYPE.MONSTERINVADE = 7, id: A05 行 id(1..22)}`
        #   （`NetExploreVitality.lua:12` / `InvadeHome.lua:463`）。
        #   修前**没有这一支** -> 落到下面的通用分支：只扣 1 点体力、不校验 id、不扣门票、
        #   也不开战局会话 ⇒ 门票（道具 1025，商店 60 妖魂）毫无意义、9008 可无前置重放。
        #   门票单价取 `T23_Function[ID=186].Value`（=1，客户端 `InvadeHome:updateUI:235-238`
        #   读的就是这一行）；id 必须是 A05 真实行；扣票失败回 518（客户端码表）。
        _sid = _i(req.get('id'), 0)
        _ok_row = False
        try:
            from handlers_activity import _A05 as _ha05
            _ok_row = any(int(_r[0]) == _sid for _r in _ha05)
        except Exception as _e:
            print('[INVADE] !! 读 A05 失败: %r' % (_e,), flush=True)
        if not _ok_row:
            return b'[512]'
        _cost = _a05_ticket_cost()
        _item = _prop_take(1025, _cost)
        if _item is None:
            return b'[518]'                   # 门票不足（一分体力都不扣）
        try:
            from battle_session import open_session as _bs_open
            _bs_open('invade', _sid)
        except Exception as _e:
            print('[BS] !! 开妖主会话失败(不影响开战): %r' % (_e,), flush=True)
        save_state()
        print('[INVADE] 1702 type=7 开战 stage=%s 扣门票1025 x%d -> 剩 %s'
              % (_sid, _cost, _item[2]), flush=True)
        return json.dumps([1, [_role_info(), _item]], separators=(',', ':')).encode('utf-8')
    if not _spend_vitality(1):
        return b'[513]'
    save_state()
    rem = _vitality_count()
    return json.dumps([1, [_role_info(), ['1006', 1006, rem]]], separators=(',', ':')).encode('utf-8')

def handle_explore_drop(body_json):
    # 1704 EXPLORE_DROP —— 开罐子 / 宝箱。
    # ★v54e (2026-09-15) 接**官方掉落表**。旧实现写死 `gold=200` + 固定 2 残章, 与客户端能看到的掉落无关:
    #   真机实测 (2026-09-15 00:54~00:56): 一次探索开 22 个罐子 = **+4400 金 +44 残章**,
    #   而同期 6 场战斗的官方掉落合计只有 80 金 ⇒ 罐子才是金币主来源, 且完全是私服编的。
    #   字段链 (逐行取自 1.31 明文):
    #     app/explore/objects/ModelBox.lua:255  sendMessage(MSG_ID.EXPLORE_DROP, exploreFirstId, staticId, unlockList, dropFunc)
    #     app/network/explore/NetExploreDrop.lua:24/32  sendMessage(_gateid,_sceneid,..) ; msg["sceneid"] = _sceneid (= staticId)
    #     app/explore/staticdata/S03_Spawn.lua:26  usGetTableData(tS03_Spawn.tName, _staticID)   <- 按行 id 查
    #   ⇒ req["sceneid"] 就是 S03_Spawn 的行 id; 该表 601 行, RewardID = T02_Reward.GroupID。
    #   resp: [1,[roleInfo, dropInfo]]  (NetExploreDrop:recvMessage msg[1]/msg[2])
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    if not isinstance(req, dict):
        req = {}
    #   ⚠★2026-09-15 (v54e-2) 真机修正: **sceneid 是 ZZRc4 加密的, 不是明文数字!**
    #     真机样本 (01:08:33~01:09:25, work/server_debug.log):
    #       1704 req={"exploreid":"","time":"1789405721","gateid":40001,"sceneid":"p/ufTQ=="}
    #     用 zzrc4 解 `p/ufTQ==`(time=1789405721) -> 1007 -> S03_Spawn[1007].RewardID=1101
    #     8/8 个真样本全部解出合法 S03_Spawn 行且 RewardID 都是合法 T02 组。
    #     (第一版按明文 int() 解析 -> 全落兜底 -> 玩家体感"奖励没变"; 兜底探针把这个问题报出来了)
    #
    # ★★2026-10-16（任务书 A1 ②）：**门禁与归属**。修前三处错：
    #   ① 大关卡取自 `req["exploreid"]` —— 而它是**解锁度 id 串**（`NetExploreDrop.lua:36-43`：
    #      `msg["exploreid"] = "101&102"`），真的大关卡在 `msg["gateid"]`（`:31`）。
    #   ② 不校验 `sceneid` 是否属于该大关卡 ⇒ 可以拿 A 关的箱子 id 去 B 关开（跨关刷）。
    #   ③ 表外 `sceneid` 走固定 **200 金 + 2 残章**兜底 ⇒ 每个未知 id 都是一台印钞机。
    _gate = _i(req.get('gateid'), 0)
    if _gate <= 0 or _gate not in _XP_GATES:
        return b'[521]'                       # 大关卡 id 错误（客户端码表有 521）
    try:
        _sid = int(req.get('sceneid') or 0)
    except Exception:
        _sid = 0
    if not _sid:
        _sid = _zzrc4_int(req.get('sceneid'), req.get('time')) or 0
    if _sid <= 0 or _sid not in _S03_SPAWN_TYPE:
        return b'[519]'                       # 场景ID错误（客户端码表有 519）
    _owner = _spawn_gate(_sid)
    if _owner != _gate:
        print('[SPAWN_DROP] !! 1704 跨关卡: gateid=%s sceneid=%s 属 %s -> 519'
              % (_gate, _sid, _owner), flush=True)
        return b'[519]'
    # ★2026-10-16（任务书 A1 ②）：**发奖前判本次是否已发**。修前重复请求仍先发奖
    #   （`_xp_open` 的去重只挡住了计数，奖励照发），于是重发一次 1704 就多拿一份。
    if _xp_is_open(_gate, _sid):
        print('[SPAWN_DROP] 1704 重放 gateid=%s sceneid=%s -> 不再发奖' % (_gate, _sid), flush=True)
        return json.dumps([1, [_role_info(), _drop([])]], separators=(',', ':')).encode('utf-8')
    dropgroups = []
    _gid = _S03_SPAWN_REWARD.get(_sid)
    if _gid:
        _g, _ok = _grant_group(_gid, roll=True, mult=REWARD_MULT)
        # ★v60 修复（按官方对齐 T02 之后暴露出来的真 bug）:
        #   官方 T02 的组是"MaximumNumber 条 + Rate 当权重"，**抽到 0 条是完全合法的结果**
        #   （1101 的 Rate 只有 0.25/0.4）。原实现把"抽到 0 条"也当成"没命中官方组",
        #   于是**回退成私服兜底(200金/2残章)** —— 玩家开箱会随机多拿 200 金 + 2 残章,
        #   而且日志还打"未命中官方组"这句**误导性**的话（映射其实是好的）。
        #   ⇒ 这里把"映射没命中"和"官方组抽空了"分开: 后者是合法结果, 不发也不兜底。
        if _ok:
            dropgroups = _g or []          # 空 = 官方组这次什么都没抽到 -> 正常, 不下发
            print('[SPAWN_DROP] 1704 sceneid=%s 官方组=%s -> %d 条 (mult=%d)'
                  % (_sid, _gid, len(_g or []), REWARD_MULT))
        else:
            # ★2026-10-16：兜底已删。发放失败**就是**"这次没东西"，宁可空手也不编一份
            #   （旧兜底会凭空发 200 金 + 2 残章）。同时照样把对象记成"已开过" ——
            #   否则玩家重试就能反复掷骰，比空手更糟。
            print('[SPAWN_DROP] !! 1704 sceneid=%s 官方组=%s 发放失败(_ok=False) -> 空掉落(不兜底)'
                  % (_sid, _gid), flush=True)
    else:
        # 表内 601 行的 RewardID 全部是合法 T02 组；走到这里说明表坏了/字段猜错，留证但不发。
        print('[SPAWN_DROP] !! 1704 sceneid=%s 查不到 RewardID（表异常）-> 空掉落' % _sid, flush=True)
    # ★2026-10-11 (3500): 把**这次真实发出去的**掉落并入本次探索账本 —— 客户端结算面板
    #   "本次探索累计获得"只认 3500 的应答，不记这一笔就等于面板上凭空少了东西。
    _xp_ledger_add(dropgroups)
    # 1704 只是开罐/宝箱；T37 CT=3 是“通关任意关卡”，此处不能记进度。
    # ★★2026-10-08（探索链轮）：把"这个罐子/宝箱被清掉了"写进探索进度 ——
    #   ① 累加 S12 的"本次/累计发现"计数（Type 6~11），② 记进一次性对象表，
    #   供 1705 的 `scenedata`/`exploredata` 回给客户端（否则重进地图箱子又回来了）。
    # ★2026-10-16：大关卡就是**请求的 `gateid`**（已过归属校验），不再从 exploreid 猜。
    _xp_open(_gate, _sid)
    # ★2026-10-02 (成就进度写入方): 探索成就组 1001 的两格
    #   T32 Type=104「开启罐子N」 -> 组 1001/级别1 (踢罐子！)
    #   T32 Type=103「开启宝箱N」 -> 组 1001/级别2 (寻宝藏！)
    #   口径取自 S03_Spawn.Type 列(实测: 1=罐子431+莲蓬30, 2=宝箱95, 3=封印宝箱12, 4=神秘宝箱33)。
    #   sceneid 表外 -> Type=0 -> **不计数**(不编造)。
    _stype = _S03_SPAWN_TYPE.get(_sid, 0)
    if _stype == 1:
        from handlers_achievement import note_ach
        note_ach(104, 0, 1)
        # ★2026-10-16（任务书 E）：T37 CT=24「调皮蛋：在任意关卡中累计打碎 50 个罐子」。
        #   判据 = `S03_Spawn.Type==1`（罐子 431 + 莲蓬 30，都是可打碎的"罐子类"对象）；
        #   计数点放在**本局真的开成功**之后（重放在前面就 return 了），所以不会刷。
        try:
            from handlers_task import bump_task as _bt, CT_POT as _CT_POT
            _bt(_CT_POT, 1)
        except Exception as _e:
            print('[CT24] !! 打碎罐子计数失败(不影响开箱): %r' % (_e,), flush=True)
        # ★★2026-10-22（审查高 H1）：UG 71003 是「**在《河洛古墓》(简单)** 累计打碎 10 个罐子」
        #   （Value=1000001），而修前 UG 的 CT=24 读的是共享计数 ⇒ 任意关卡砸罐子都能点亮它
        #   （真机账号档 `ug_ct['24']=65` 就是这么来的）。这里按**本局大关卡**另记一格；
        #   `_gate` 已在上面过 519 归属校验（`_spawn_gate(sceneid) == gateid`），可以直接用。
        try:
            from handlers_ugtask import bump_ug_ctv as _btv
            if _btv(24, _gate, 1):
                print('[UG24] 罐子 gate=%s -> CT=24 目标 +1' % (_gate,), flush=True)
        except Exception as _e24:
            print('[UG24] !! 计数失败(不影响开箱): %r' % (_e24,), flush=True)
    elif _stype in (2, 3, 4):
        from handlers_achievement import note_ach
        note_ach(103, 0, 1)
        # ★2026-10-16（任务书 E）：T37 CT=22「寻宝达人：在任意关卡中开启 8 个隐藏宝箱」。
        #   "隐藏宝箱" 的判据**客户端自己写着**：`app/explore/staticdata/S03_Spawn.lua:149-156`
        #     `function S03_Spawn:getStatic_IsHidden()  local isHide = false
        #        if self.m_Type == 4 then isHide = true end  return isHide`
        #   ⇒ 只有 `Type==4`（神秘宝箱，33 行）算隐藏宝箱；`IsHiddenSweep` 是"扫荡时隐藏"，
        #     与这个成就无关（568 行都是 1，显然不是它）。
        if _stype == 4:
            try:
                from handlers_task import bump_task as _bt, CT_HIDDEN_BOX as _CT_HB
                _bt(_CT_HB, 1)
            except Exception as _e:
                print('[CT22] !! 隐藏宝箱计数失败(不影响开箱): %r' % (_e,), flush=True)
            # ★★2026-10-24（独立审查 高 H2）：UG_07_Task 的 CT=22 有 14 行、`Value` 各指
            #   **不同大关卡**（12010=40001 两箱 / 22009=50001 两箱 / 32013=60001 四箱 …），
            #   而 `bump_task(22)` 只喂共享 `ug_ct['22']` ⇒ 在 40001 开两箱就能点亮要求
            #   50001 的 22009（审查反例）。这里按**本局大关卡**另记一格，与 CT=24 罐子同一做法；
            #   `_gate` 已过 519 归属校验（`_spawn_gate(sceneid) == gateid`）。
            try:
                from handlers_ugtask import bump_ug_ctv as _btv22
                if _btv22(22, _gate, 1):
                    print('[UG22] 隐藏宝箱 gate=%s -> CT=22 目标 +1' % (_gate,), flush=True)
            except Exception as _e22:
                print('[UG22] !! 计数失败(不影响开箱): %r' % (_e22,), flush=True)
    save_state()
    return json.dumps([1, [_role_info(), _drop(dropgroups)]], separators=(',', ':')).encode('utf-8')

def handle_explore_degree(body_json):
    """1705 EXPLORE_DEGREE：`[gateid, degree(0-100), exploredata, scenedata]`。

    ★★2026-10-08（探索链轮）：修前恒 `(gateid, 0, [], [])` —— 即
      **"解锁度永远 0%、地图上的一次性怪物/宝箱永远重新出现"**。
      客户端 `NetExploreDegree.lua:53-68` 把 `scenedata` 灌进 `SQExplore.OnlyEvent`
      （`ExploreConfig.isHasOnlyEvent():308` 用它把对象标成 isDeath/isOpen），
      把 `exploredata` 灌进 `CompleteDegree`（`exploreUnlockTool` 用它算解锁度）。
      两样都空 ⇒ 玩家打完的怪、开过的箱子**每次重进都回来**，解锁度统计图永远 0/N。
      现在按 `S12_SceneExplore`（条件）与 1704 的开箱记录（一次性对象）给真值。
    ⚠ `gateid` 必须是 S01/S12/S03 的那一套"大关卡 id"（40001 简单 / 140001 困难），
      不是 S00_WorldMap 的节点 id。表外的 id -> `521 大关卡id错误`（客户端有这张码表）。
    """
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    if not isinstance(req, dict):
        req = {}
    gateid = _i(req.get('gateid'), 0)
    if gateid <= 0:
        return b'[521]'
    if gateid not in _XP_GATES:
        return b'[521]'                       # 大关卡 id 错误（该表没有这个大关卡）
    save_state()
    return _simple_resp(gateid, _xp_degree(gateid), _xp_exploredata(gateid),
                        _xp_scenedata(gateid), _xp_boxdata(gateid)).encode('utf-8')

def handle_explore_box_reward(body_json):
    """3501 NET_GET_BOX_REWARD：`[roleInfo, dropInfo]` —— 领解锁度门控宝箱。

    ★2026-10-11（A0 第 1 批）：修前服务端**没有 3501** ⇒ 落 `resp_for` 兜底，
      客户端 `NetGetBoxReward:recvMessage` 拿到的是兜底体：`msg[1]` 当 roleInfo 灌进
      `gamer_setRoleInfo`、`msg[2]` 当 dropInfo 灌进 `refreshDropInfo` ⇒
      **箱子点了没反应（或刷出垃圾奖励行）**，而解锁度统计图上那三个箱子永远点不动。

    契约（1.30 装机 `network/explore/NetGetBoxReward.lua:24-45`）：
      请求 `{gateid, boxid}`；应答 `msg[1]=roleInfo`（`gamer_setRoleInfo`）、
      `msg[2]=dropInfo`（`refreshDropInfo` + `multiRewardPandect` 弹奖励）。
      ⚠ 它**不回读解锁度**：客户端领完自己把本地 status 改成 2，并调
      `notifyUpdateDegree()` 再发一次 1705 ⇒ 服务端**必须**真的把领取记下来，
      否则重进统计图箱子又变回"可领"（这正是 1705 `msg[5]` 存在的意义）。

    奖励来源：`boxid` 是 `T02_Reward.ID`（`SearchReward` 里的那个），
      发放要按 **`GroupID`** 取组（铁律：官方 id 空间必须按来源取数）。
      表外 id / 不在本大关卡的门控列表里 -> `537`（客户端有这张码表，且此码族与
      每日任务/成就发的奖同一族：537 组错 / 538 条件不足 / 539 已领）。
    """
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    if not isinstance(req, dict):
        req = {}
    gateid = _i(req.get('gateid'), 0)
    boxid = _i(req.get('boxid'), 0)
    if gateid <= 0 or gateid not in _XP_GATES or boxid <= 0:
        return b'[537]'                       # 大关卡/宝箱 id 错误（客户端有这张码表）
    if boxid not in _xp_box_ids(gateid):
        return b'[537]'
    g = _xp_gate(gateid)
    if str(boxid) in (g.get('box') or {}):
        return b'[539]'                       # 已领取
    if _xp_box_status(gateid, boxid, _xp_degree(gateid)) != 1:
        return b'[538]'                       # 条件不足（解锁度没到该箱门槛）
    grp = _box_reward_group(boxid)
    if grp is None:
        return b'[537]'                       # 表外/没有 GroupID -> 不猜、不发
    _g, _ok = _grant_group(grp, roll=True, mult=REWARD_MULT)
    if not _ok:
        return b'[537]'
    dropgroups = _g or []
    g['box'][str(boxid)] = 1                  # 记下已领（1705 的 msg[5] 就靠它）
    _xp_ledger_add(dropgroups)                # 并入"本次探索累计获得"（3500 面板）
    save_state()
    return json.dumps([1, [_role_info(), _drop(dropgroups)]],
                      separators=(',', ':')).encode('utf-8')

def handle_explore_exit(body_json):
    """1706 EXPLORE_EXIT：客户端 `recvMessage` 只回调（不读 msg[N]）。

    ⚠ 它**不是空桩**：这里的"只回 `[1,[]]`"与 `handle_sociaty_simple` 那种
      "什么都不做"不同 —— 探索进度的收尾在 1705/1704 已经落盘，本函数没有别的状态要改。
      保留显式实现是为了让 `_audit` 的"空桩"清单能把它与真正的空桩区分开
      （清单只看函数体，看不出语义；注释在这里就是判据）。
    """
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    if isinstance(req, dict) and _i(req.get('gateid'), 0) > 0:
        _xp_end_run()                         # ★2026-10-11: 本次探索结束（清单一并留给紧随的 3500）
        # ★2026-10-16（A 的状态机接口）：退出 = 关掉这一局 explore 会话（之后 9003 不许再结算）。
        try:
            from battle_session import close_session as _bs_close
            _bs_close('explore', _i(req.get('gateid'), 0))
        except Exception as _e:
            print('[BS] !! 关探索会话失败(不影响退出): %r' % (_e,), flush=True)
        save_state()                          # 落一次盘：把本次探索的计数固化
    return b'[1,[]]'

# ============ 剧情解锁关卡 1038 STORY_UNLOCKSCENE ============
# 客户端 MapView:addTouchNode -> sendMessage(1038, callfunc); 响应 msg[1] = 已解锁节点id列表
#   (NetStoryUnlockScene:recvMessage 逐元素 unlocklist[id]=id -> 大地图节点点亮/可点击)
#   解锁规则 (S00_WorldMap.UnlockHint 文案 "LV.x时..."): 玩家等级 >= 对应 S01_Level(DefaultID).SceneLv
#   (剧情完成度私服不追踪; 妖神传说 4001 客户端另有等级内判, 服务器下发一致即可)
_WM_ROWS = []      # (nodeid, 普通关卡SceneLv, 精英关卡SceneLv)
try:
    _s0 = _load_json('S00_WorldMap.json')
    _h0 = {c: k for k, c in enumerate(_s0[0])}
    _s1 = _load_json('S01_Level.json')
    _h1 = {c: k for k, c in enumerate(_s1[0])}
    _LV1 = {}
    for _r in _s1[1:]:
        try:
            _LV1[int(_r[_h1['ID']])] = int(_r[_h1['SceneLv']] or 0)
        except Exception:
            continue
    for _r in _s0[1:]:
        try:
            _wid = int(_r[_h0['ID']])
            _def = int(_r[_h0['DefaultID']] or 0)
            _el = int(_r[_h0['EliteID']] or 0)
            _WM_ROWS.append((_wid, _LV1.get(_def, 0), _LV1.get(_el, 0)))
        except Exception:
            continue
except Exception:
    _WM_ROWS = []

# T24_YMYW (妖魔异闻) —— ★★2026-09-15 (v54e-3) P0 修复: 1035 下发的是 **IDGroup, 不是 ID**
# ---------------------------------------------------------------------------
#   用户报"挑战 → 妖魔异闻 **无法进入**"（点不进任何一页）。真机取证: 该时段服务端**从未收到 1702**
#   （挑战按钮的请求），只有 1035 在刷 ⇒ 是客户端本地把入口禁用了。
#   逐行读 1.31 明文 (app/pve/PVEView.lua:212-225):
#       for _, monster_id in pairs(_unlocklist) do
#           monConfig = DataTable:GetTableSelectData(tT24_YMYW.tName, tT24_YMYW.IDGroup, tostring(monster_id))
#           local id = tonumber(string.sub(tostring(monster_id), -1))   -- 取最后一位当页签号 1..7
#           self.unlocked_monster_config[id] = monConfig
#       end
#   ⇒ 客户端是拿**回包里的值去查 IDGroup**、并取**末位字符**当页签号。
#   旧实现发的是 T24_YMYW.ID (1011..1072) -> 按 IDGroup 查 1011 查不到 -> monConfig=nil
#     -> unlocked_monster_config[1..7] 全是 nil -> 7 个页签 `isUnLocked=false` -> 全部禁用 -> 点不进去。
#   ⚠ 这与 v53 修掉的 1017/1012 是**同一类** id 空间错配 (铁律 #164/#166); v53 的入口审计只查了
#     `T24_YMYW.BattleId`(战斗组) 是否合法, 没查 1035 这个"列表 id"该用哪一列。
#   正确值 = **IDGroup** (101..107, 每组 2 行 = 普通/困难两档, 与客户端 mSelectData[1]/[2] 对应);
#     按玩家等级过滤 (取组内最小 Level 作为该页签门槛, 与客户端 Lock_Normal/Lock_Difficulty 同列)。
# ---------------------------------------------------------------------------
_PVE_STAGE_IDS = []        # 兼容保留(旧名): 现在装的是 **IDGroup**
_PVE_GROUPS = {}           # IDGroup -> [行, ...]
_T24_BY_ID = {}            # T24_YMYW.ID (str, 1011..1072) -> 原始行
_T24_BY_GROUP = {}         # T24_YMYW.IDGroup (str, 101..107) -> [原始行, ...]
_h24 = {}
try:
    _t24 = _load_json('T24_YMYW.json')
    if type(_t24) is list and len(_t24) > 1:
        _h24 = {c: k for k, c in enumerate(_t24[0])}
        for _r in _t24[1:]:
            try:
                _g24 = int(_r[_h24['IDGroup']])
                _PVE_GROUPS.setdefault(_g24, []).append(_r)
                _T24_BY_ID[str(_r[_h24['ID']])] = _r
                _T24_BY_GROUP.setdefault(str(_g24), []).append(_r)
            except Exception:
                continue
except Exception:
    pass


def _t24_row(sid):
    """T24_YMYW.ID -> 列名->值 的 dict；表外/加载失败 -> None。"""
    try:
        r = _T24_BY_ID.get(str(sid))
        if r is not None:
            return {c: r[_h24[c]] for c in _h24}
    except Exception:
        pass
    return None


def _ymyw_battle_id(req):
    """9006 的 `(id, info)` -> `T24_YMYW.BattleId`（= T01_MstrGroup 行 id）。

    ★2026-10-18（真机样本 `{"info":"107","id":1072}`）：`id` 是 T24 的 **ID**，
      `info` 是 **IDGroup**；两者必须指向同一行。缺 id / 表外 / id 与 info 不同组
      -> `None`（调用方走兜底，**绝不猜**普通还是困难）。
    """
    if not isinstance(req, dict):
        return None
    sid = str(req.get('id') if req.get('id') is not None else '')
    grp = str(req.get('info') if req.get('info') is not None else '')
    r = _T24_BY_ID.get(sid) if sid else None
    if r is not None:
        try:
            if grp and str(r[_h24['IDGroup']]) != grp:
                print('[YMYW] !! 9006 id=%s 属于 IDGroup=%s 但 info=%s 不一致 -> 拒绝'
                      % (sid, r[_h24['IDGroup']], grp), flush=True)
                return None
            return _i(r[_h24['BattleId']], 0) or None
        except Exception:
            return None
    if sid:
        print('[YMYW] !! 9006 id=%r 不在 T24_YMYW -> 拒绝' % (sid,), flush=True)
    return None


def pve_unlocked_groups():
    """玩家当前已解锁的妖魔异闻页签 (= T24_YMYW.IDGroup 列表, 客户端 PVEView.lua:217 按它查表)。"""
    if not _PVE_GROUPS:
        return []
    try:
        _lv = int(EQ_STATE.get('player_lv', 1))
    except Exception:
        _lv = 1
    out = []
    for _g in sorted(_PVE_GROUPS):
        try:
            _minlv = min(int(x[_h24['Level']] or 0) for x in _PVE_GROUPS[_g])
        except Exception:
            _minlv = 0
        if _lv >= _minlv:
            out.append(_g)
    return out or sorted(_PVE_GROUPS)

def handle_story_unlock_scene(body_json=None):
    # 1038: 返回已解锁节点 id 列表 (按玩家等级; 玩家 lv8 -> 1001/1002/1006/1007/3001塔/5001夜市 点亮)
    #   ⚠ 包裹层: NetStoryUnlockScene:recvMessage unlockInfo=msg[1] 且 msg=响应最后一个元素
    #   -> 响应必须是 [1,[[ids...]]] (msg=[ [ids] ] -> msg[1]=ids); [1,[ids]] 会让 msg[1]=第一个id (空列表!).
    lv = int(EQ_STATE.get('player_lv', 1))
    out = []
    for wid, slv, elv in _WM_ROWS:
        if lv >= slv:
            out.append(wid)
    return ('[1,[' + json.dumps(sorted(out), separators=(',', ':')) + ']]').encode('utf-8')

def handle_pve_list(body_json=None):
    # 1035 PVE_LIST (妖魔异闻): NetPveList:recvMessage unlockInfo=msg[1] -> 客户端逐个 tonumber()
    #   ★★v54e-3 P0: 这里必须发 **T24_YMYW.IDGroup** (101..107), 不是 ID (1011..1072)!
    #     客户端 PVEView.lua:218 拿它去查 `tT24_YMYW.IDGroup`, 并用**末位字符**当页签号 1..7
    #     (见文件上方 _PVE_GROUPS 的取证注释)。发 ID 会让 7 个页签全部查不到 -> 整模式进不去。
    #   Text_Completed = #unlocklist, Text_Total = #all/2 -> 两边都按"页签数"算, 所以发 7 个才对得上。
    ids = pve_unlocked_groups()
    return ('[1,[' + json.dumps(ids, separators=(',', ':')) + ']]').encode('utf-8')

# ============ 头像 1092-1096 ============
def handle_head_info(body_json=None):
    # resp [1]=_listHeadInfo
    return _simple_resp([]).encode('utf-8')

def handle_head_buy(body_json):
    return _rd_resp().encode('utf-8')

def handle_head_frame_info(body_json=None):
    return _simple_resp([]).encode('utf-8')

def handle_head_save(body_json):
    return _rd_resp().encode('utf-8')

def handle_head_frame_save(body_json):
    return _rd_resp().encode('utf-8')

# ============ 宝箱 1060/1061/1062 ============
# 说明: 宝箱系统已在 handlers_profile.py 完整实现 (game_server 注册 1060/1062 -> handlers_profile;
#   L103_TreasureBox + L103_TreasureBoxAward 权重开箱; 状态容器 EQ_STATE['treasure_bag']).
#   此处仅保留礼包/宝箱通用的"单行奖励发放"辅助 _grant_content_row (被 profile 开箱复用).
def _grant_content_row(gt, oid, num):
    # 单行奖励发放 (宝箱抽奖/通用): 返回 style groups 片段
    gt, num = int(gt), int(num or 1)
    oid = str(oid)
    groups = []
    if gt == 1:                        # 装备 (L01 全表; 校验合法 id 避免客户端 Dummy)
        try:
            eid = int(oid)
        except Exception:
            eid = 0
        from handlers_bag import _EQUIP_IDS
        if eid <= 0 or str(eid) not in _EQUIP_IDS:
            return []
        ucnt = max([int(u) for u in EQ_STATE['equip_id'] if str(u).isdigit()] or [0]) + 1
        uc = str(ucnt)
        EQ_STATE['equip_id'][uc] = eid
        EQ_STATE['lv'][uc] = 1
        EQ_STATE['exp'][uc] = 0
        EQ_STATE['equip_isload'][uc] = 0
        try:
            from state import note_manual_unlock as _nmu
            _nmu(eid)                   # 装备图鉴: 拾取即解锁
        except Exception:
            pass
        try:
            from handlers_bag import note_equip_gained as _neg
            _neg(eid)                   # ★CT=20 极品装备（Class>=4）
        except Exception:
            pass
        groups.append([1, [_equip_item(uc)]])
    elif gt == 2:                      # 道具
        pid = int(oid) if str(oid).isdigit() else 0
        if pid > 0:
            uc = str(pid)
            EQ_STATE['prop'][uc] = EQ_STATE['prop'].get(uc, 0) + num
            EQ_STATE['prop_id'][uc] = pid
            groups.append([2, [[uc, pid, num]]])
    elif gt == 3:
        EQ_STATE['gold'] += num
        groups.append([3, [[num]]])
    elif gt == 7:
        EQ_STATE['soulcoin'] += num
        groups.append([7, [[num]]])
    elif gt == 8:
        EQ_STATE.setdefault('draw', {})['ticket'] = EQ_STATE['draw'].get('ticket', 0) + num
        groups.append([8, [[num]]])
    elif gt == 4:
        try:
            # ★2026-10-16（B 复核 2）：造怪收口到 `handlers_bag.grant_monsters`
            #   （背包上限 21 + 放不下的转邮件）；按 `num` 逐只发（修前恒发 1 只）。
            from handlers_bag import grant_monsters as _gm
            _rows, _mailed = _gm(int(oid), num)
            if _rows:
                groups.append([4, _rows])
            if _mailed:
                print('[CONTENT] 宝箱/掉落里的 %d 只妖怪已转邮件' % _mailed, flush=True)
        except Exception as _e:
            print('[CONTENT] !! 妖怪发放异常 type=4 id=%s num=%s: %r' % (oid, num, _e),
                  flush=True)
    elif gt == 5:
        from state import add_ghost_rows as _addgh
        groups.append([5, _addgh(int(oid), num, 0)])
    elif gt == 6:
        EQ_STATE['kidou'] = int(EQ_STATE.get('kidou', 0)) + num
        groups.append([6, [[num]]])
    elif gt == 9:
        EQ_STATE.setdefault('prop', {})['1006'] = min(20, int(EQ_STATE['prop'].get('1006', 0)) + num)
        groups.append([9, [[num]]])
    elif gt == 14:
        # ★2026-09-11 (v21 §3.3): 旧实现直接 set skin_owned -> 重复获得皮肤"没有反应"(不入皮肤碎片)。
        #   统一走 handlers_bag._grant_skin_with_dup: 已拥有 -> SKIN_DUP_FRAGMENT(10) 片皮肤碎片(L02 5000101);
        #   未拥有 -> 记 skin_owned + 按 L98 怪阶段族解锁给该族妖怪(1500/1501 才可用)。
        from handlers_bag import _grant_skin_with_dup
        _dup, _g = _grant_skin_with_dup(int(oid))
        groups.extend(_g)
    elif gt == 22:
        EQ_STATE.setdefault('head', {}).setdefault('frame_own', {})[int(oid)] = 1
        groups.append([22, [[int(oid)]]])
    return groups

def _add_monster_here(mid):
    # 宝箱抽怪: 与 handlers_draw._grant_monster 同款 (L91 static)
    # ★2026-10-02 (审计 §9 高severity #3): 原子取号
    uc = alloc_monster_ucode()
    from handlers_draw import _MON_STATIC
    st = _MON_STATIC.get(mid)
    if st:
        _q, skin, skills, _t = st
    else:
        skin, skills = 0, [0] * 6
    EQ_STATE.setdefault('monsters', {})[uc] = [mid, skin, 1, list(skills)]
    # ★v34 (bug#1): 同上 —— 宝箱抽怪也必须登记 monster_ueq (见 handlers_game gt==4 处的长注释)
    from state import ensure_monster_ueq as _ensure_ueq
    _ensure_ueq(uc)
    return uc

def handle_treasure_get(body_json=None):
    # 已废弃 (注册版=handlers_profile.handle_treasure_get); 此占位仅防外部误 import
    from handlers_profile import handle_treasure_get as _h
    return _h(body_json)

def handle_treasure_buy(body_json):
    from handlers_profile import handle_treasure_buy as _h
    return _h(body_json)

def handle_treasure_achive(body_json):
    from handlers_profile import handle_treasure_achive as _h
    return _h(body_json)

# ============ 技能 1502-1505: 已移至 handlers_skill.py (2026-09-06) ============

# ============ 进化 6003 ============
# 客户端契约 (NetMonsterEvolve.lua + CultivateEvolution.lua + SDEvolution.lua + L04_Evolution):
#   req {monsterucode, matterlist:[{type:3,ucode,num}道具.., {type:1,ucode}怪物..]}
#   resp [1,[eggcoin,soulcoin,goldcoin,monsterInfo(30),matterlist,skillInfo,equipdata]]
#     skillInfo/equipdata: 客户端 recvMessage 由 `if X then` 守卫 -> null 即"无更新" (占位语义正确, 勿改空数组)
#   规则: L91.AdvanceID=下一阶段static id; L04 行按【当前 CardId】取素材:
#     ItemId1-4 x Quantity1-4 (道具), MonsterID("|"分隔列表) x MonsterQuantity (同卡/万能卡), gold (铜钱)
#   错误码: 70 非法 / 71 素材空 / 74 不能进化 / 75 素材有误 / 76 数量错误 / 77 金币不足 / 78 已最高 / 562 玩家等级不足
_EVOL_ROWS = {}      # cardId -> {'items':[(itemid,qty)..], 'monster_ids':[ids], 'monster_qty':N, 'gold':G}
_EVOL_L104 = {}      # star -> (RoleLv, MstLvLimit)

# ============================================================================
# ★v41 进化"怪物素材"槽 —— 复刻客户端 SDEvolution 的加号槽算法
# ----------------------------------------------------------------------------
# 用户报的现象: 大天师(500001) 1阶->2阶 点确定 -> "进化的素材数量错误" == [76]。
#
# 根因 (2026-09-13 实测, 全表扫描 545 行):
#   客户端的怪物素材有**两套语义**, 而服务端只实现了一套:
#     ① 指定怪: `MonsterID` + `MonsterQuantity` (=| 分隔的静态 id 列表)
#        -> 服务端已实现 (_EVOL_ROWS['monster_ids'] / 'monster_qty')
#     ② **非指定怪**: `Monster1` / `Monster2`, 格式 "品质|种族|品阶" (种族 -1 = 通配)
#        -> 服务端**完全没解析这两列**
#   客户端 app/staticdata/SDEvolution.lua:82-136 用 arrFlag1/2/3 决定界面显示几个"+"槽:
#     arrFlag1 = (MonsterID ~= "0") ; arrFlag2 = (#Monster1 >= 1) ; arrFlag3 = (#Monster2 >= 1)
#     槽1 = 正序第一个可用判据 ; 槽2 = 倒序第一个可用判据 ; 两者相同则只显示 1 个
#   过滤条件见 app/team/CultivateEvolution.lua:889-915:
#     品质 == e[1]  且  (e[2] == "-1" 或 种族 == e[2])  且  品阶 == e[3]
#
#   大天师全链 5 行**只有 Monster1/Monster2、没有指定怪**(MonsterID=0/MonsterQuantity=0),
#   于是: 客户端显示 2 个怪物槽要玩家填 -> 服务端认为"这只怪不需要怪物素材" ->
#   `elif used_mons: return b'[76]'` -> 玩家无论选什么怪都被拒。
#   全表命中: 545 行里 **5 行**(500001~500005, 即整条大天师链), 其余 540 行不受影响。
#
# ★ 为什么 EVOLVE_REQUIRE_ALL_MONSTER_SLOTS 默认 False (不强制填满):
#   客户端本该在槽没填满时拦住 (CultivateEvolution.lua:1150/1158), 但它的"未选择"哨兵值
#   初始化成 **0** (第 50/51/71/72/190/191 行) 而守卫只判 `== 1` -> 守卫根本不生效,
#   只填 1 个槽(甚至 0 个)也会照发。实测用户就是这样发出 1 只怪的。
#   服务端若硬要求"必须 2 只", 玩家会永远卡住(而且他包里确实凑不出第 2 只, 见 v41 报告)。
#   所以默认: **校验给出来的每一只都合法**(不能拿不相干的怪当祭品), 但不强求填满。
#   要把设计强度恢复成"必须填满", 把下面这个常量改成 True 即可 (一行)。
EVOLVE_REQUIRE_ALL_MONSTER_SLOTS = False


def _evol_fuzzy(spec):
    """'4|5|2' -> ('4','5','2') ; '0'/空/列数不足 -> None"""
    s = str(spec if spec is not None else '').strip()
    if not s or s == '0':
        return None
    parts = s.split('|')
    if len(parts) < 3:
        return None
    return (parts[0], parts[1], parts[2])


def _evol_slots(r, h):
    """复刻客户端 SDEvolution 的怪物素材槽 -> [(kind, data), ...]

    kind='id'    -> data = 指定怪静态 id 列表 (排除 0)
    kind='fuzzy' -> data = (品质, 种族, 品阶); 种族 '-1' = 全种族
    """
    mid = [int(x) for x in str(r[h['MonsterID']] or '0').split('|')
           if x.strip().lstrip('-').isdigit() and int(x) != 0]
    m1 = _evol_fuzzy(r[h['Monster1']])
    m2 = _evol_fuzzy(r[h['Monster2']])
    f1, f2, f3 = bool(mid), m1 is not None, m2 is not None
    if not (f1 or f2 or f3):
        return []
    i1 = 1 if f1 else (2 if f2 else 3)
    t1 = 'id' if i1 == 1 else 'fuzzy'
    i2 = 3 if f3 else (2 if f2 else 1)
    t2 = 'id' if i2 == 1 else 'fuzzy'
    d1 = mid if i1 == 1 else (m1 if i1 == 2 else m2)
    out = [(t1, d1)]
    if not (i1 == i2 and t1 == t2):
        d2 = mid if i2 == 1 else (m2 if i2 == 3 else m1)
        out.append((t2, d2))
    return out


def _evol_monster_ok(mu, kind, data, self_ucode):
    """素材怪 mu 是否满足某个槽的判据"""
    if str(mu) == str(self_ucode):
        return False                       # 不能把自己当祭品 (客户端也排除 m_roleId)
    base = get_monster_base(mu)
    if base is None:
        return False
    try:
        mmid = int(base[0])
    except Exception:
        return False
    if kind == 'id':
        # 指定怪: 静态 id 命中, 或"同卡/万能卡"(SourceID 相同)
        return mmid in data or get_monster_source(mu) == get_monster_source(self_ucode)
    # 非指定怪: 品质|种族|品阶  (种族 '-1' = 通配)
    q, ty, st = _state_mod.monster_static_attrs(mmid)
    return (str(data[0]) == q) and (str(data[1]) == '-1' or str(data[1]) == ty) and (str(data[2]) == st)


def _evol_monsters_match(mons, slots, self_ucode):
    """把给出的素材怪**一一对应**到各槽 (每只怪只能占一个槽)。槽数 <=2, 直接穷举。"""
    if not mons:
        return True
    if len(mons) > len(slots):
        return False
    import itertools
    for perm in itertools.permutations(range(len(slots)), len(mons)):
        if all(_evol_monster_ok(mons[i], slots[perm[i]][0], slots[perm[i]][1], self_ucode)
               for i in range(len(mons))):
            return True
    return False


def _init_evolve_tables():
    global _EVOL_ROWS, _EVOL_L104
    if _EVOL_ROWS:
        return
    t = _load_json('L04_Evolution.json')
    h = {c: k for k, c in enumerate(t[0])}
    for r in t[1:]:
        try:
            card = int(r[h['CardId']])
            items = []
            for k in range(1, 5):
                ii = int(r[h['ItemId%d' % k]] or 0)
                qq = int(r[h['Quantity%d' % k]] or 0)
                if ii and qq > 0:
                    items.append((ii, qq))
            mids = [int(x) for x in str(r[h['MonsterID']] or '0').split('|') if x.strip().isdigit()]
            _EVOL_ROWS[card] = {'items': items, 'monster_ids': mids,
                                'monster_qty': int(r[h['MonsterQuantity']] or 0),
                                'gold': int(r[h['gold']] or 0),
                                # ★v41: 客户端界面的怪物素材槽 (含"非指定怪" Monster1/Monster2)
                                'slots': _evol_slots(r, h)}
        except Exception:
            continue
    t = _load_json('L104_RevLv.json')
    h = {c: k for k, c in enumerate(t[0])}
    for r in t[1:]:
        try:
            _EVOL_L104[int(r[h['RevLv']])] = (int(r[h['RoleLv']] or 0), int(r[h['MstLvLimit']] or 0))
        except Exception:
            continue

def handle_monster_evolve(body_json):
    _init_evolve_tables()
    try:
        req = json.loads(body_json)
    except Exception:
        return b'[70]'
    if not isinstance(req, dict):
        return b'[70]'
    ucode = str(req.get('monsterucode') or '')
    matter = req.get('matterlist') or []
    # ★v58 诊断留痕（只写日志，零逻辑改动）——为"进化连点/不刷新"取证。
    #   判据：客户端每次请求带的素材种类数 是否 == 服务端当前 mid 的配方种类数。
    #     · 相等 -> 客户端看到的阶段 = 服务端阶段（界面已同步）
    #     · 少一种 -> 客户端stage落后（界面没刷新/连点竞态）
    #   用 try 包住，绝不能因留痕影响业务。
    try:
        _mid_now = get_monster_base(ucode)
        _mid_now = int(_mid_now[0]) if _mid_now else -1
        _row_now = _EVOL_ROWS.get(_mid_now)
        _req_kinds = sorted({str(m.get('ucode')) for m in matter
                             if isinstance(m, dict) and str(m.get('type')) == '3'})
        _need_kinds = sorted({str(it) for it, _q in (_row_now or {}).get('items', [])})
        _nxt_now = get_monster_advance(ucode)
        # ★ 只打 ASCII —— 控制台是 GBK, 打中文会变乱码, 反而看不清
        print('[EVOLVE-DIAG] ucode=%s mid=%s next=%s cli_mats=%s srv_recipe=%s %s'
              % (ucode, _mid_now, _nxt_now, _req_kinds, _need_kinds,
                 'OK-SYNC' if _req_kinds == _need_kinds else
                 ('LAG(missing=%s)' % (sorted(set(_need_kinds) - set(_req_kinds)))
                  if set(_req_kinds) < set(_need_kinds) else
                  ('EXTRA(extra=%s)' % (sorted(set(_req_kinds) - set(_need_kinds)))
                   if set(_need_kinds) < set(_req_kinds) else 'DIFF'))))
    except Exception:
        pass
    if get_monster_base(ucode) is None:
        return b'[70]'
    if not isinstance(matter, list) or not matter:
        return b'[71]'
    nxt = get_monster_advance(ucode)          # 下一阶段 static id (L91.AdvanceID)
    if not nxt or nxt == 0:
        return b'[78]'                        # 已是最进化
    mid = int(get_monster_base(ucode)[0])
    row = _EVOL_ROWS.get(mid)
    if row is None:
        return b'[74]'                        # 无进化配置
    # 分解客户端素材
    used_props = []      # (ucode, num)
    used_mons = []       # ucode
    for m in matter:
        if not isinstance(m, dict):
            continue
        if str(m.get('type')) == '3':
            mu = str(m.get('ucode') or '')
            try:
                nn = int(m.get('num') or 0)
            except Exception:
                nn = 0
            if mu:
                used_props.append((mu, nn))
        elif str(m.get('type')) == '1':
            mu = str(m.get('ucode') or '')
            if mu:
                used_mons.append(mu)
    req_items = {it: q for it, q in row['items']}
    if not req_items and not row['monster_qty']:
        return b'[74]'                        # 该阶段无需素材(不应出现) -> 按不可进化处理
    # 校验道具: id 在所需集合 + 数量精确
    got = {}
    for mu, nn in used_props:
        pid = EQ_STATE.get('prop_id', {}).get(mu)
        if pid is None or int(pid) not in req_items:
            return b'[75]'                    # 素材有误
        if nn <= 0 or int(EQ_STATE['prop'].get(mu, 0)) < nn:
            return b'[71]'                    # 数量不足
        got[int(pid)] = got.get(int(pid), 0) + nn
    for it, q in req_items.items():
        if got.get(it, 0) != q:
            return b'[76]'                    # 数量错误
    # 校验怪物素材 (★v41 修: 原来只认 MonsterID/MonsterQuantity, 完全没解析
    #   Monster1/Monster2 "非指定怪" -> 大天师全链 5 行的任何选择都被 [76] 拒掉)
    slots = row.get('slots') or []
    if used_mons:
        if not slots:
            return b'[76]'                    # 该行压根不要怪物素材, 却给了 -> 数量错误
        if len(used_mons) > len(slots):
            return b'[76]'                    # 给多了
        if EVOLVE_REQUIRE_ALL_MONSTER_SLOTS and len(used_mons) != len(slots):
            return b'[76]'                    # (可选) 强制填满每个槽
        if len(set(str(x) for x in used_mons)) != len(used_mons):
            return b'[75]'                    # 同一只怪不能重复占两个槽
        if not _evol_monsters_match(used_mons, slots, ucode):
            return b'[75]'                    # 素材有误 (不满足 品质|种族|品阶 或 指定怪)
    elif slots and EVOLVE_REQUIRE_ALL_MONSTER_SLOTS:
        return b'[71]'                        # (可选) 一个都没给
    # 金币 + 玩家等级 (客户端 tm.getPlayerLvLimit(star) = L104.RoleLv)
    star = get_monster_star(ucode)
    need_lv = _EVOL_L104.get(star, (30, 30))[0]
    if int(EQ_STATE.get('player_lv', 1)) < need_lv:
        return b'[562]'
    if int(EQ_STATE['gold']) < row['gold']:
        return b'[77]'
    # ----- 生效 -----
    EQ_STATE['gold'] = int(EQ_STATE['gold']) - row['gold']
    for mu, nn in used_props:
        EQ_STATE['prop'][mu] = int(EQ_STATE['prop'].get(mu, 0)) - nn
        if EQ_STATE['prop'][mu] <= 0:
            EQ_STATE['prop'].pop(mu, None)
            EQ_STATE.get('prop_id', {}).pop(mu, None)
    for mu in used_mons:
        _remove_monster(mu)
    # 变身: static id -> AdvanceID (皮肤/技能保留; 新阶段属性自动由客户端 SDMonster(mid) 读取)
    st = _MON_STATIC.get(nxt)
    new_skin = st[1] if st else 0
    if ucode in EQ_STATE['monsters']:
        m = EQ_STATE['monsters'][ucode]
        EQ_STATE['monsters'][ucode] = [nxt, new_skin or m[1], m[2], list(m[3])]
    else:
        # 初始三只: 更新内存 base + ★持久化覆盖 (2026-09-07: 旧版只改 _MONSTER_BASE 内存 -> 重启回退)
        base = _state_mod._MONSTER_BASE.get(ucode)
        if base:
            _state_mod._MONSTER_BASE[ucode] = (nxt, new_skin or base[1], base[2], list(base[3]))
            EQ_STATE.setdefault('monster_base_override', {})[str(ucode)] = \
                [nxt, new_skin or base[1], base[2], list(base[3])]
    # ★v60: 记录"进化到的最高阶" —— A01 活动组 105「妖怪进化奖励」的**唯一计数源**。
    #   口径来自 A01_1 g=105 的 Desc 实测: "**首次进化到2/3/4/5阶**"(Target 2/3/4/5),
    #   即"有没有把某只怪进化到第 N 阶" —— **不是"累计进化次数"**。
    #   所以只在这里(真的发生进化时)记; **不**从背包现存怪的最高星级反推 ——
    #   反推会把"召唤到手就是高星"的怪也算成"进化过", 那是假的可领取状态(§9.3 铁律 8)。
    #   ⚠ 诚实说明: 老档里"已经进化过的"不会被补记 -> 面板暂显示未完成, 再进化一次就对上。
    try:
        _stage = int(get_monster_star(ucode) or 0)      # 进化后该怪的当前星级(=阶)
        _ev = EQ_STATE.setdefault('evolve', {})
        if _stage > int(_ev.get('max_stage', 0)):
            _ev['max_stage'] = _stage
        # ★★2026-10-22（审查高 H2）：UG CT=45「N 个妖怪进化到 M 阶」的真值是
        #   "**哪几只不同的怪**达到了几阶"，不是"进化了几次"。
        #   `ug.mon_star[ucode]` = 该 ucode 达到过的最高阶，只在**真的进化成功**时写
        #   ⇒ `_sched` 按"达到 >=M 阶的不同 ucode 个数"计数：同一只怪 2→3 不会再冒充一次
        #     "到 2 阶"，两只不同怪各升到 2 阶才是 2。
        #   ⚠ 不从背包现存怪的星级反推（那会把"召唤到手就是高星"也算成进化过，与 A01 同一口径）。
        _ugd = EQ_STATE.setdefault('ug', {})
        if not isinstance(_ugd, dict):
            _ugd = EQ_STATE['ug'] = {}
        _ms = _ugd.get('mon_star')
        if not isinstance(_ms, dict):
            _ms = _ugd['mon_star'] = {}
        if _stage > int(_ms.get(str(ucode), 0) or 0):
            _ms[str(ucode)] = _stage
        # ★★2026-10-25（用户裁定）：A07 的 CT=45「将 N 个妖怪进化到 M 阶」改成**去重账本** ——
        #   与上面 `ug.mon_star` 同一笔事实（哪只怪到过几阶），但账本独立：
        #   `a07_ct` 是"进化了几次"且**按天清零**（`_reset_if_new_day`），同一只怪升两阶会冒充两只。
        #   这里按 ucode 记最高阶，3068 读取时数"达到 >=M 阶的不同 ucode 个数"。
        _a7s = EQ_STATE.setdefault('a07_mon_star', {})
        if not isinstance(_a7s, dict):
            _a7s = EQ_STATE['a07_mon_star'] = {}
        if _stage > int(_a7s.get(str(ucode), 0) or 0):
            _a7s[str(ucode)] = _stage
    except Exception as _e:
        print('[EVOLVE] !! 记录最高进化阶失败(不影响进化本身): %r' % (_e,), flush=True)
    # ★★2026-10-17（任务链收口）：CT=45「进化妖怪」的事件源 = 本函数成功路径。
    #   A07 活动 1 的 10104/10313（"将进化 N 个妖怪到 2/3 阶"）此前**永远 0 进度**。
    #   口径：一次成功进化记 1（表里两行 qty 都是 3，与"到几阶"无关 —— 表没有 Value 区分）。
    try:
        from handlers_task import bump_task as _bt, CT_EVOLVE as _CT_EVOLVE
        _bt(_CT_EVOLVE, 1)
    except Exception as _e:
        print('[EVOLVE] !! bump CT=45 失败(不影响进化本身): %r' % (_e,), flush=True)
    save_state()
    mi = _mon_with_state(ucode)
    matter_out = [[1, mu, 1] for mu in used_mons] + [[3, mu, nn] for mu, nn in used_props]
    # ★★ v57 (2026-09-15) P0: 下面这行原来两个 None 都进成功响应。
    #   客户端 NetMonsterEvolve.lua 的消费方式逐一核对：
    #     msg[5] matterlist : `for i=1,#matterlist`        -> matter_out 有内容, 安全
    #     msg[6] skillInfo  : `if skillInfo then if tonumber(skillInfo[2]) ~= 0`
    #     msg[7] equipdata  : `for _, equipinfo in pairs(equipdata)` -> ★pairs(null) 抛错!
    #   ⇒ 用户实测"进化后界面不刷新"就是这个 msg[7]=null 造成的(与 6002 同型)。
    #   详见 work\交接文档_v57_吞噬不刷新修复.md
    #
    # ★★★ v58 (2026-09-16) 修正上面 msg[6] 那条结论 —— 它是【错的】:
    #   原文写 "tonumber(null) 返回 nil, 类型安全"。这句话对 null 勉强成立,
    #   但 v57 把 null 换成了 `[]`, 而【空表】会让客户端走进分支并崩溃:
    #       if skillInfo then                       -- [] 是 table, 为真
    #         if tonumber(skillInfo[2]) ~= 0 then   -- tonumber(nil)=nil, 且 **nil ~= 0 为 true**
    #           updateItem(skillInfo[1], skillInfo) -- skillInfo[1]=nil
    #             -> SQSkill:insertItem(nil,..) -> self.mBagList[nil]={..}
    #             -> SQSkill.lua:35 "table index is nil" 抛错
    #             -> recvMessage 中断 -> m_callFunc 永不执行 -> 界面完全不刷新
    #   ⇒ 全修级 bug(每一次进化都中), 已在客户端 NetMonsterEvolve.lua 加 _si_ok 守卫修复。
    #   详见 work\交接文档_v58_换表修复尝试与回滚_20260916.md §10
    #   ⚠ 本处 `[]` 占位【保留】(客户端已加守卫, 且改形状要同步核对老包), 但别再叫它"类型安全"。
    data = [EQ_STATE['eggcoin'], EQ_STATE['soulcoin'], EQ_STATE['gold'],
            mon_info_slot(mi, ucode, '6003'), matter_out, [], []]
    assert None not in data, 'LAW-A: 成功响应不得含 null (v57 进化刷新修复)'
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')

# ============ PVP 7001-7012: 已移至 handlers_pvp.py (2026-09-06) ============

# ============ 活动 1100-1116 (基础) ============
def handle_activity_list(body_json=None):
    # resp [1]=activityindexlist
    return _simple_resp([]).encode('utf-8')
def handle_activity_cards(body_json=None):
    # 1101 卡池活动列表（`NetActivityCards:recvMessage` 读 `msg[1]` 每行的 id/endtime/img）。
    #
    # ★★2026-11-07（活动卡池轮换）：改由 `cardpool_rotation` 按周给出**一期**活动池：
    #   `[[合成池id, 本期结束epoch秒, 底图文件名]]`。底图是 target client内置的
    #   `res/03_ClientImage/Raffle/*_1.png`（`ResConfig.lua:233` 已加进搜索路径），
    #   所以发裸文件名即可，客户端 `imageSpriteChange` 直接 setTexture。
    #   关闭轮换 / 读不到时回 `[1,[]]`：`NetActivityCards` 用 `msg[1] or {}` 兜住，
    #   页签因此不出现（1108 也不会加 type=6 那行）。
    #   形状口径：成功必须是 `[1,[[行..]]]`；旧实现发过 `[1,[[]]]`（多一层空表），别再混。
    try:
        from cardpool_rotation import activity_rows
        rows = activity_rows()
    except Exception as e:
        print('[CARD_POOL] !! 1101 活动池轮换不可用: %r' % (e,))
        rows = []
    if not rows:
        return b'[1,[]]'
    return _simple_resp(rows).encode('utf-8')
def handle_activity_receive(body_json):
    return _rd_resp().encode('utf-8')
# ⚠ 1108 `handle_operation_activity_list` 的实现**只允许有一份**, 在 `handlers_activity.py`
#   (4 条真活动行, 见那里的注释)。这里原来还有一份 `return b'[1,[]]'` 的空桩 ——
#   game_server 第 169 行的显式 import 正好把它覆盖掉, 所以当时跑的是对的那份;
#   但只要有人调整 import 顺序, 主界面 4 个活动按钮就会**静默全灭**
#   (空列表 ⇒ MMainButton `activityPosData[i]` 全 nil ⇒ 按钮 setVisible 不到)。
#   已删除。护栏 `work/_coverage_gap_test.py` 的 G2 会当场抓住"空桩遮蔽真实装"这类回归。

# ============ 公会 1121-1139: 已移至 handlers_sociaty.py (2026-09-06) ============
