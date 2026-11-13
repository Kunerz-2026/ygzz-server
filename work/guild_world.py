# -*- coding: utf-8 -*-
"""guild_world.py —— 妖盟（公会）的**真·共享实体**：本项目**第一个**真正的世界键。

## 它取代什么
修前"妖盟"是三件互相矛盾的东西拼出来的：
  ① **展示**（1122/1143）按每个账号档的 `sociaty.name` **分组派生** —— 于是"会"没有自己的
     身份：没有 id、没有入会申请、没有成员名册、没有职位，改公告只改自己那一份；
  ② **管理动作**（1124/1125/1126/1127/1130/1131）**全是 no-op**：1125 回空（没有任何存储）、
     1126/1127/1130/1131 只回调 ⇒ 玩家点了"同意入会/踢人/调职位"，服务端什么都没发生；
  ③ `acc.data['sociaty']` 同时被当作"我会的账"（`wealth` 是全会的钱、`notice` 是全会的公告）
     与"我的账"（`todaydonate` 是我捐的）—— 两类字段混在一个 per-account 字典里。

## 现在的真值在哪（**唯一一份**）
```
world['guilds'][gid] = {
    'id','name','icon','level','wealth','notice',
    'autoJoin','autoJoinThreshold','created',
    'members':      { '<aid>': {'position':'L|FL|A|N','join_at':ts,'offer':int} },
    'applications': { '<role_id>': {'aid','name','level','points','img','imgframe','at':ts} },
}
```
**会级字段（name/icon/level/wealth/notice/autoJoin*/members/applications）只有世界键这一份真值。**
账号档里的 `sociaty`（含新增的 `guild_id`）降级为**这个账号侧的缓存/投影**，由
`sync_account()` 回灌 —— 它**不再是**判定"谁在哪个会"的依据。
⇒ 于是"A 建的会 B 立刻看得到"、"盟主改公告全盟都看到"、"入会申请存在会里"这三件事
   天然成立，不需要任何双向同步。

## 为什么不能只把"申请"放进世界键（本轮**否决**的方案）
那个方案看着更省事（不动 `sociaty`），它的失败形态是**两套真值**：
`world['guilds'][g].members` 与"各账号档 `sociaty.name` 的分组"会各说一套 ——
建会、退会、踢人、改会名都只改一边，而**两边都能渲染出界面**（1122 读派生视图、1125 读世界键），
于是"成员列表里有他、申请列表里也有他"这种矛盾**不报错、只显示错**。
本项目已经因为"派生视图 + 第二份真值"付过一次学费（`DECISIONS` 2026-10-02 的评论/排行两条）。

## 单档模式（`YGZZ_MULTI_ACCOUNT` 未开）
**逐字节保持改造前的行为**：`use()` 返回 `None`，本模块的所有函数立刻返回 `None`/`False`/`[]`，
调用方走原路径（1122 回 `[1,[]]`、1143 那 3 个假盟、1125 那 3 个假申请人、1126/1127/1130/1131 只回调）。
⇒ 回归网里**所有**"单档假设"的套件与生产单档部署都不受影响。

## 三条铁律（沿用任务 D 的口径）
1. **gate 在 `ENABLED`**：判据是 `ENABLED and REGISTRY is not None`，**不能**只判"结果是不是空"——
   多账号下"世界上只有一个会/一个申请都没有"是**合法**结果。
2. **只读别人档的标量，绝不 bind 到别人**：`save_state()` 按当前 `contextvars` 上下文路由，
   在别人的上下文里触发落盘就会写**别人的档**。跨账号只读走 `acc.data`（`all_accounts_full()`）。
3. **没有数据源的字段一律 0**：`activeWeek`/`liveness`/`hitRate` 服务端没有这些量，不许编造。

## roleId 约定
多账号下，`role_id_of(aid)` 与 `state.role_info_array()` 的 `fields[22]`（Lua [23]）
都使用真实 `owner_ns(aid)`，以 JSON number 下发；身份不随观察者改变。
`1` 只属于 `u0001`，不能作为“当前玩家”的哨兵；单档兼容路径才保留旧行为。
"""
import random
import time
from functools import wraps

# 职位码：**客户端用字符串**，服务端发的也是字符串 —— 唯一的权威出处是客户端源码
#   `sociaty/VSociatyMember.lua`：
#     :498 `ApplicationAdjustment("L",  _index)`  转让盟主
#     :506 `ApplicationAdjustment("FL", _index)`  副盟主
#     :514 `ApplicationAdjustment("A",  _index)`  管理员
#     :522 `ApplicationAdjustment("N",  _index)`  成员
#   :601 `sendMessage(MSGID, roleId, positioncode)` ⇒ **1130 的请求体里 `positioncode` 是字符串**
#   权限判定（同一文件）:307/:308/:484/:486 只认 `"L"` 与 `"FL"`；
#   :480 `transferBtn` 要求我方 `"L"` 且对方 `"FL"`。
#   ⇒ 盟友职位码不是"L/A/M"而是 **"L"/"FL"/"A"/"N"**（另有 `"E"`，`ControllerMSociatyInfoHome.lua:461`
#     把 `"E"`/`"N"` 当"无职位"处理）。
#   ⚠ 另有一套**数字**码（1 盟主 / 2 副盟主 / 3 管理员 / 4 成员）是另一件事：
#     它既不是客户端发上来的、也不是服务端发下去的（`sociaty_post` 全仓**零写入点**），
#     所以两套之间**没有**必然对应关系 —— 早先按"数字码"实现 1130 是错的（见 `_pos_of_code`）。
POS_LEADER = 'L'
POS_DEPUTY = 'FL'
POS_ADMIN = 'A'
POS_MEMBER = 'N'
POS_NONE = 'E'
# 能审批入会 / 踢人 / 调职位的职位（与客户端 :307/:308/:484/:486 同一判据）
_CAN_MANAGE = (POS_LEADER, POS_DEPUTY)
# 兼容"旧数字码"（早先的实现按它写，护栏也按它写过）：1=L / 2=FL / 3=A / 4=N
_NUM_TO_POS = {1: POS_LEADER, 2: POS_DEPUTY, 3: POS_ADMIN, 4: POS_MEMBER}
_KNOWN_POS = (POS_LEADER, POS_DEPUTY, POS_ADMIN, POS_MEMBER, POS_NONE)

DEFAULT_GUILD = {'id': '', 'name': '', 'icon': 0, 'level': 1, 'wealth': 0, 'notice': '',
                 'autoJoin': 0, 'autoJoinThreshold': 0, 'created': '',
                 'members': {}, 'applications': {}}

# ===========================================================================
# 妖盟弈局（1144/1145/1146/1147/1148/1154/1156/1157/1158/1159）的**共享状态**
# ---------------------------------------------------------------------------
# 修前这 9 个号在服务端是 `handle_sociaty_simple`（`[1,[]]`）：客户端按下
# 「升级弈局 / 购买局数 / 开启弈局 / 讨伐 / 上报血量 / 看伤害排行」**都拿到"成功"回调**，
# 而服务端**一个字节都没记住** —— 弈局等级、开启次数、妖神血量、伤害榜全是 `sociaty_boss`
# 这个**per-account** 字典里的假值（`_sgb()`），于是"我开的弈局队友看不到"、
# "队友打的伤害我看不到"，正是妖盟共享实体要根治的形态。
#
# 现在它与会名/财富同级：**只有世界键这一份真值**。
#   world['guilds'][gid]['chess'] = {
#     'level': 1,                        # 弈局等级（1146 升级；决定妖神解锁）
#     'date': 'YYYY-MM-DD',              # 下面三个日计数器属于哪一天（惰性重置）
#     'num_used': 0,                     # 今日已开启次数
#     'num_bought': 0,                   # 今日已购买次数（上限 _CHESS_NUM_BUY_MAX）
#     'entry_bought': 0,                 # 今日已购买进入次数
#     'active': None | {                 # 当前开启中的弈局（同时最多一个：码 50052/50060）
#         'stage': 1001,                 # UN07.StageID
#         'boss_stage': 6101,            # UN07.BossStageID（战斗里用它反查 bossId）
#         'hp_base': 500000, 'hp': 500000,
#         'deadline': ts, 'started': ts,
#         'killer_rid': 0, 'killer_name': '', 'over': False,
#         'damage': {'u0001': {'dmg': 123, 'at': ts}},   # 每账号累计伤害
#     },
#   }
# ⚠ **妖神血量是真值**（`UN07.HP`），不再编造：1145/1152 都把**剩余血量**发下去，
#   客户端 `VSociatyTranscriptBoss:setBloodPercent()` 用它除 `UN07.HP` 画进度条。
#   （修前 1152 恒发 10000，任何 boss 的进度条都显示 2%。）
_CHESS_NUM_BUY_MAX = 5      # 每日最多购买 5 次开局次数（与 T23:118=3000 财富/次配套）
_CHESS_ENTRY_BUY_MAX = 5    # 每日最多购买 5 次进入次数（T23:119=40 妖魂/次）
_CHESS_DMG_CAP = 2000000000


def _chess_now():
    return int(time.time())


def _chess_today():
    return time.strftime('%Y-%m-%d')


def _chess_blank(num_max=1, entry_max=2):
    return {'level': 1, 'date': _chess_today(), 'num_used': 0, 'num_bought': 0,
            'entry_bought': 0, 'num_max': max(1, _i(num_max, 1)),
            'entry_max': max(1, _i(entry_max, 2)), 'active': None, 'killer': None,
            # ★2026-10-26：1153 领奖账本（**持久化**）。修前只靠 `battle_session`
            #   的进程内票号 ⇒ 重启后同一局可再领一次；且票号按 (stage,killer,at)
            #   在击杀前后会变 ⇒ 同一账号一局能领两次。
            #   形状：{round_key: {aid: {'display': ts, 'rank': ts}}}（两种奖各自一次）
            'claims': {}}


def _display_name_of(acc, aid=None):
    """账号显示名：`role_name` -> `未命名#<aid>`（与 `_own_player_name`/名册同口径）。

    ⚠ 必须这样兜底，不能给空串：1155 的 `msg[1][3]`（最后一击的名字）和 1121 的名册
      都直接显示它，空串会让客户端出现"无名的一行"。本轮实测踩到过一次：
      1123 建会路径设的 `role_name` 与迁移路径（`role_name=None`）不同，
      击杀者名字取成了 `''`。
    """
    aid = aid or getattr(acc, 'id', None) or ''
    nm = ''
    if acc is not None and isinstance(acc.data, dict):
        nm = str(acc.data.get('role_name') or '')
    return nm or ('未命名#' + str(aid))


def chess_record(g):
    """这个会的 `chess` 子状态；缺失/坏形状就地补一份（**返回对象可写**，落盘由调用方负责）。"""
    if not isinstance(g, dict):
        return None
    c = g.get('chess')
    if not isinstance(c, dict):
        c = g['chess'] = _chess_blank()
    for k, v in _chess_blank().items():
        if k not in c:
            c[k] = v
    if not isinstance(c.get('active'), (dict, type(None))):
        c['active'] = None
    if isinstance(c.get('active'), dict) and not isinstance(c['active'].get('damage'), dict):
        c['active']['damage'] = {}
    if not isinstance(c.get('claims'), dict):
        c['claims'] = {}
    return c


def chess_reset_day(g):
    """跨天惰性重置：日计数器归零、**未结束的弈局作废**（上一局的开启次数/伤害不跨天延续）。

    返回 `True` 表示改了状态。**不主动落盘** —— 调用方（读路径 `chess_snapshot` /
    写路径 `_persist`）负责；读路径不落盘是刻意的：跨天重置只影响"今天能不能开"，
    在世界档里晚写一次不会让任何已提交的语义变成假的。

    ★ `killer`（最后一击）**不在重置范围内**：它是"上一局谁打死的"的历史记录，
      1155 的 `msg[1]` 在击杀后仍要给出它（客户端会拿它的 roleId 在榜单里认名次）。
    """
    c = chess_record(g)
    if c is None:
        return False
    if str(c.get('date') or '') == _chess_today():
        return False
    c['date'] = _chess_today()
    c['num_used'] = 0
    c['num_bought'] = 0
    c['entry_bought'] = 0
    if isinstance(c.get('active'), dict):
        c['active'] = None            # 弈局是"当日 19:00-22:00"的活动，不在跨天后延续
    return True


def chess_active(g):
    """当前开启中的弈局；**已过期/已结束的一律返回 `None`**（并把状态清干净）。"""
    c = chess_record(g)
    if c is None:
        return None
    a = c.get('active')
    if not isinstance(a, dict):
        return None
    if a.get('over') or _i(a.get('deadline', 0), 0) <= _chess_now():
        c['active'] = None
        return None
    return a


def chess_snapshot(g, aid=None):
    """给 handler 用的**只读**快照（不含 damage 明细）。未入会/无会 -> `None`。"""
    if not isinstance(g, dict):
        return None
    chess_reset_day(g)
    c = chess_record(g)
    a = chess_active(g)
    num_max = max(1, _i(c.get('num_max', 1), 1))
    num_left = max(0, num_max + _i(c.get('num_bought', 0), 0) - _i(c.get('num_used', 0), 0))
    entry_max = max(1, _i(c.get('entry_max', 2), 2))
    d = (a.get('damage') or {}) if isinstance(a, dict) else {}
    me = (d.get(str(aid)) or {}) if aid else {}
    return {'level': max(1, _i(c.get('level', 1), 1)),
            'num_left': num_left, 'num_used': _i(c.get('num_used', 0), 0),
            'num_bought': _i(c.get('num_bought', 0), 0),
            'entry_max': entry_max,
            'entry_left': max(0, entry_max + _i(c.get('entry_bought', 0), 0)
                              - _i(me.get('entry_used', 0), 0)),
            'entry_used': _i(me.get('entry_used', 0), 0),
            'active': ({'stage': _i(a.get('stage', 0), 0),
                        'boss_stage': _i(a.get('boss_stage', 0), 0),
                        'hp': max(0, _i(a.get('hp', 0), 0)),
                        'hp_base': max(1, _i(a.get('hp_base', 1), 1)),
                        'left': max(0, _i(a.get('deadline', 0), 0) - _chess_now())}
                       if isinstance(a, dict) else None),
            'damage': me.get('dmg', 0) if isinstance(me, dict) else 0}


def chess_damage_rows(g):
    """当前弈局的伤害行（**按伤害降序**）：`[(aid, dmg, at), ...]`。无开启的弈局 -> `[]`。"""
    a = chess_active(g)
    if not isinstance(a, dict):
        return []
    d = a.get('damage') if isinstance(a.get('damage'), dict) else {}
    rows = []
    for aid, rec in d.items():
        if not isinstance(rec, dict):
            continue
        rows.append((str(aid), max(0, _i(rec.get('dmg', 0), 0)), _i(rec.get('at', 0), 0)))
    rows.sort(key=lambda r: (-r[1], r[0]))
    return rows


def chess_killer(g):
    """最后一击：`(roleId, name)`。没有（未击杀）-> `(0, '')`。

    ⚠ 存在 `chess['killer']` 而**不是** `active['killer_rid']`：击杀那一次要把 `active` 清掉
      （否则 1144 会一直显示"开启中"），清掉之后 1155 的 `msg[1]` 还要能给出最后一击。
      第一版把击杀者写在 `active` 里，结果是"击杀者查不到"（本轮实测）。
    """
    c = chess_record(g)
    k = c.get('killer') if isinstance(c, dict) else None
    if not isinstance(k, dict):
        return 0, ''
    return _i(k.get('rid', 0), 0), str(k.get('name') or '')


# ===========================================================================
# 弈局局号 + 领奖账本（1153 的唯一判据）
# ---------------------------------------------------------------------------
# 修前 1153 只靠 `battle_session.settle('chess', ...)` 的**进程内**票号：
#   ① 票号按 `(stage, killer_rid, killer_at)` 生成 ⇒ 击杀那一次票号变了，
#      同一账号在击杀前领过 display、击杀后还能再领一次；
#   ② 票号表在内存里 ⇒ 重启后可再领；
#   ③ 只判"这一局有没有人结算过"，**从不校验领奖者是否参战** ⇒ 任何会内成员
#      在击杀后发 1153 都能拿到 UN09.Rank0（最后一击）奖励组。
# 现在改为世界档里的持久账本：`chess['claims'][round_key][aid][display|rank]`，
#   局号 = `active.started`（击杀时原样抄进 `killer`，所以击杀前后同一个局号）。
# ===========================================================================
_CLAIM_KEEP_ROUNDS = 8


def chess_round(g):
    """当前/最近一局的身份快照；没有局 -> `None`。

    返回 `{'key','stage','boss_stage','rows','killer_rid','over','started'}`：
      · `rows` = `[(aid, dmg, at), ...]`（按伤害降序）—— **参战者名单**也是它；
      · `over=False` 读 `active.damage`；`over=True` 读击杀时拍下的 `killer.rows` 快照
        （`active` 已被清空，不能再从 live 状态读）。
    """
    c = chess_record(g)
    if not isinstance(c, dict):
        return None
    a = chess_active(g)
    if isinstance(a, dict):
        return {'key': str(_i(a.get('started', 0), 0) or _i(a.get('deadline', 0), 0)),
                'stage': _i(a.get('stage', 0), 0),
                'boss_stage': _i(a.get('boss_stage', 0), 0),
                'rows': chess_damage_rows(g), 'killer_rid': 0, 'over': False,
                'started': _i(a.get('started', 0), 0)}
    k = c.get('killer') if isinstance(c.get('killer'), dict) else None
    if not isinstance(k, dict):
        return None
    rows = []
    for r in (k.get('rows') or []):
        if isinstance(r, (list, tuple)) and len(r) >= 3:
            rows.append((str(r[0]), max(0, _i(r[1], 0)), _i(r[2], 0)))
    rows.sort(key=lambda r: (-r[1], r[0]))
    return {'key': str(_i(k.get('started', 0), 0) or _i(k.get('at', 0), 0)),
            'stage': _i(k.get('stage', 0), 0),
            'boss_stage': _i(k.get('boss_stage', 0), 0),
            'rows': rows, 'killer_rid': _i(k.get('rid', 0), 0), 'over': True,
            'started': _i(k.get('started', 0), 0)}


def chess_participated(g, aid, rnd=None):
    """这个账号是否**参加了**该局（进入过弈局并留下伤害记录）。"""
    rnd = rnd if isinstance(rnd, dict) else chess_round(g)
    if not rnd:
        return False
    aid = str(aid or '')
    if not aid:
        return False
    if any(str(r[0]) == aid for r in (rnd.get('rows') or [])):
        return True
    # 升级前击杀的老世界档没有 rows 快照：只认击杀者本人，绝不把上一局判给所有会内成员
    rid = _i(rnd.get('killer_rid'), 0)
    return bool(rnd.get('over')) and rid != 0 and role_id_of(aid) == rid


def chess_rank_of(g, aid, rnd=None):
    """该账号在本局的名次：击杀者 `0`（最后一击）/ 其他参战者 `1..N` / 未参战 `None`。"""
    rnd = rnd if isinstance(rnd, dict) else chess_round(g)
    if not rnd:
        return None
    aid = str(aid or '')
    if rnd.get('over'):
        rid = _i(rnd.get('killer_rid'), 0)
        if rid and role_id_of(aid) == rid:
            return 0
    for i, r in enumerate(rnd.get('rows') or [], 1):
        if str(r[0]) == aid:
            return i
    return None


def chess_claim(g, round_key, aid, kind, now=None):
    """原子占用一次领奖名额：`True`=本次可以发奖，`False`=已经领过/参数非法。

    `kind ∈ {'display','rank'}` —— 参战奖与名次奖**各自**每局每账号一次
    （击杀前的结算只能拿 display；击杀后的结算补 rank，两者不互相吞掉）。
    """
    if kind not in ('display', 'rank'):
        return False
    key = str(round_key or '')
    aid = str(aid or '')
    if not key or not aid:
        return False
    c = chess_record(g)
    if not isinstance(c, dict):
        return False
    claims = c.get('claims')
    if not isinstance(claims, dict):
        claims = c['claims'] = {}
    book = claims.get(key)
    if not isinstance(book, dict):
        book = claims[key] = {}
    me = book.get(aid)
    if not isinstance(me, dict):
        me = book[aid] = {}
    if _i(me.get(kind, 0), 0):
        return False
    me[kind] = int(now or _chess_now()) or 1
    while len(claims) > _CLAIM_KEEP_ROUNDS:
        try:
            claims.pop(next(iter(claims)))
        except Exception:
            break
    return True


def _i(v, d=0):
    try:
        return int(v)
    except Exception:
        return d


def _pos_of_code(code):
    """1130 请求里的 `positioncode` -> 内部职位码。认不出来 -> `None`。

    ★★ **必须同时认两种形态**（本轮实测）：
      · 客户端真发的是**字符串** `"L"/"FL"/"A"/"N"`（`VSociatyMember.lua:498/506/514/522`）；
      · 早先的实现按**数字** `1/2/3/4` 写，护栏也按数字写过。
    第一版只认数字 ⇒ `int("FL")` 抛错被吞掉 -> `_i(code,0)` = 0 -> 一律回
    `50019 非法职位代码`：**客户端点"调整为副盟主"永远失败**，而服务端日志看起来一切正常。
    """
    if isinstance(code, bool):
        return None
    if isinstance(code, str):
        s = code.strip().upper()
        if s in _KNOWN_POS:
            return s
        try:
            code = int(s)
        except Exception:
            return None
    try:
        return _NUM_TO_POS.get(int(code))
    except Exception:
        return None


def active():
    """世界键可用吗。

    ★★ 判据是**两个条件缺一不可**，这是本轮实测踩出来的：
      ① `accounts.ENABLED and REGISTRY is not None`；
      ② **账号代理真的接在 `state.EQ_STATE` 上**（`state.EQ_STATE is accounts.EQ_STATE`）。
    为什么 ② 不能省：`accounts.enable()` 会把 `ENABLED` 置 True 并建出 `REGISTRY`，
      但它**不会**换掉 `state.EQ_STATE` —— 那个替换发生在 `state.py:615-617`，**只在
      `YGZZ_MULTI_ACCOUNT=1` 时于 import 期做一次**。于是"单档进程里有人调过
      `accounts.enable()`"（`_sociaty_test.py` 这类旧套件就是这么用的）会让 ① 成立、② 不成立。
      此时若照 ① 走世界键路径：账号上下文取不到（`current_account()` 返回 None）、
      写进去的全是"世界键里的会"，而 `_sg()` 读的是**普通 dict** ⇒ 建会看起来成功、
      实际什么也没发生（本轮实测：`create ok -1000` 与 `info hasgang` 两条判据当场红）。
      失败形态与"多账号没生效"一模一样，但**原因完全不同**，所以必须在这里挡住。
    """
    try:
        import accounts
        import state as _state
    except Exception:
        return False
    if not (getattr(accounts, 'ENABLED', False) and accounts.REGISTRY is not None):
        return False
    return getattr(_state, 'EQ_STATE', None) is getattr(accounts, 'EQ_STATE', None)


def use():
    """返回 `Registry`（世界键的持有者）；单档/未启用 -> `None`。**所有调用点的唯一入口。**

    本函数不做启动迁移或持久化；`_members_of()` 与榜单读取都会调用它。
    首次导入旧账号、以后按世界名册修账号缓存只在 `world_check()` 执行。
    """
    if not active():
        return None
    return _registry()


def current_account():
    """当前上下文的 `Account`（**单档/未绑定 -> `None`**）。

    ⚠ 只能这样取账号：`state.EQ_STATE` 在单档下是**普通 dict**（`state.py:608`），
      只有 `YGZZ_MULTI_ACCOUNT=1` 时才是账号代理（`:616-617`）⇒ 直接 `EQ_STATE.current()`
      会让单档套件当场 `AttributeError`（2026-10-02 实测踩到）。
    """
    try:
        import accounts
        a = accounts.EQ_STATE
    except Exception:
        return None
    fn = getattr(a, 'current', None)
    if not callable(fn):
        return None
    try:
        return fn()
    except Exception:
        return None


def world_check():
    """启动时对齐妖盟：首次迁移从旧账号导入；此后世界名册单向修复账号缓存。"""
    reg = use()
    if reg is None:
        return False
    if reg.guild_world_initialized:
        return reg.world_sync_accounts()
    with reg.guild_transaction():
        a = bool(reg.world_ensure())
        b = bool(reg.world_rebuild_roster())
        reg.save_world()   # chat-only 旧世界即使没有妖盟，也要持久标记已完成迁移
    return a or b


def atomic(fn):
    """把一次妖盟操作的判定、世界变更和账号变更放入同一可重放事务。"""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        reg = use()
        if reg is None:
            return fn(*args, **kwargs)
        candidates = [v for v in args if hasattr(v, 'id') and hasattr(v, 'data')]
        candidates += [v for v in kwargs.values() if hasattr(v, 'id') and hasattr(v, 'data')]
        with reg.guild_transaction(*candidates):
            return fn(*args, **kwargs)
    return wrapped


def _registry():
    try:
        import accounts
        return accounts.REGISTRY
    except Exception:
        return None


def _guilds(reg):
    g = reg.world.get('guilds')
    if not isinstance(g, dict):
        g = reg.world['guilds'] = {}
    return g


def role_id_of(aid):
    """账号 id -> 该账号的**真实 roleId**（两端算法唯一，`state.role_info_array()` 同口径）。

    ★★2026-10-02（本轮**真值化**，取代早先的"就地解冲突"）：
      早先客户端靠 `roleId == gamer_getLeaderID()` 认"我"，而服务端把 `roleInfo[23]` **恒写 1**
      ⇒ 只能"我方发 1、别人发 `100 + owner_ns`"。那是**权宜之计**，两个副作用：
        ① `100 + ns % 1000` 超过 1000 个账号就撞号（`u0002` 与 `u1002` 都是 102），
           撞号后 `kick`/`set_position` 会作用到**错的人**；
        ② 服务端自己有若干处拿 `role_id == 1` 当"这是我"的哨兵，与"真实 id"两套语义并存。
      现在**两端都发真实 id**：`state.role_info_array()` 的 `fields[22]`（客户端 roleInfo[23]）
      发同一个值 ⇒ 客户端仍然只把真正的自己认成"我"，而**任何人算出来的 id 都一样**。
      · 多账号：`owner_ns(aid)`（`u0002` -> 2，由账号 id 推出、稳定、唯一；`u0001` -> 1）；
      · 单档：恒 **1**（`state.role_info_array()` 在单档下也发 1 —— 两边必须一起看）。
      ⚠ 单档不能改成 `owner_ns`：单档下 `EQ_STATE` 是普通 dict、没有账号上下文，
        `owner_ns` 会得到 0，而 `roleInfo[23]` 发的是 1 ⇒ 客户端认不出自己。
    """
    if not active():
        return 1
    try:
        from accounts import owner_ns
        n = owner_ns(aid)
    except Exception:
        n = 0
    return n or 1


def my_aid():
    """当前上下文账号 id；未绑定 -> `None`。"""
    return current_aid()


def current_aid():
    """当前上下文账号 id；未绑定/单档 -> `None`。**唯一实现**（`my_aid` 是它的别名）。"""
    acc = current_account()
    return getattr(acc, 'id', None)


def _acc_of(reg, aid):
    try:
        if not reg.exists(aid):
            return None
        return reg.get_or_create(aid)
    except Exception:
        return None


def guild_name_of_acc(acc):
    d = acc.data if isinstance(acc.data, dict) else {}
    sg = d.get('sociaty')
    if not isinstance(sg, dict):
        return ''
    return str(sg.get('name') or '').strip()


def guild_of_name(reg, name):
    """按会名找一个会（自举用）。返回 `(gid, guild)` 或 `(None, None)`。"""
    name = str(name or '').strip()
    if not name:
        return None, None
    for gid, g in sorted(_guilds(reg).items()):
        if isinstance(g, dict) and str(g.get('name') or '').strip() == name:
            return str(gid), g
    return None, None


def guild_of(acc, reg=None):
    """账号 -> 它所在的会 `(gid, guild)`；不在会/世界键里没有 -> `(None, None)`。

    `sociaty.guild_id` 只是加速提示，必须经过世界名册验证；失效时按名册反查。
    """
    reg = reg or use()
    if reg is None or acc is None:
        return None, None
    gs = _guilds(reg)
    sg = (acc.data or {}).get('sociaty')
    gid = str(sg.get('guild_id') or '') if isinstance(sg, dict) else ''
    if gid:
        g = gs.get(gid)
        if isinstance(g, dict) and acc.id in (g.get('members') or {}):
            return gid, g
    return _roster_of(reg, acc.id)


def _roster_of(reg, aid):
    """按**名册**反查账号在哪两个会（`guild_id` 缺失/漂移时用）。返回 `(gid, guild)`。"""
    gs = _guilds(reg)
    for gid in sorted(gs.keys()):
        g = gs.get(gid)
        if not isinstance(g, dict):
            continue
        mem = g.get('members')
        if isinstance(mem, dict) and aid in mem:
            return str(gid), g
    return None, None


def _members_of(acc):
    """`(gid, guild, my_row)`；`my_row` 为 `None` 表示"档里说在这个会，但名册里没有"。"""
    gid, g = guild_of(acc)
    if not g:
        return None, None, None
    mem = g.get('members') if isinstance(g.get('members'), dict) else {}
    row = mem.get(acc.id)
    return gid, g, (row if isinstance(row, dict) else None)


def my_guild(acc=None, reg=None):
    """我所在的会 `(gid, guild)`（且必须**名册里有我**）。给 handler 用。"""
    reg = reg or use()
    if reg is None:
        return None, None
    if acc is None:
        aid = my_aid()
        acc = _acc_of(reg, aid) if aid else None
    gid, g, row = _members_of(acc) if acc is not None else (None, None, None)
    if not g or row is None:
        return None, None
    return gid, g


def my_position(acc=None):
    reg = use()
    if reg is None:
        return POS_NONE
    if acc is None:
        aid = my_aid()
        acc = _acc_of(reg, aid) if aid else None
    _gid, _g, row = _members_of(acc) if acc is not None else (None, None, None)
    return str((row or {}).get('position') or POS_NONE)


def can_manage(acc=None):
    return my_position(acc) in _CAN_MANAGE


# ===========================================================================
# 读：给 1122 / 1143 / 1125 用
# ===========================================================================
def sync_account(acc):
    """把"世界里的会"回灌进这个账号档的 `sociaty` 缓存。返回 `True` 表示**改了档**。

    `handlers_sociaty._sg()` 每次取会数据都调它 —— 于是缓存**自己修自己**（漂移看得见），
    而不需要把"每次改会都要记得同步所有成员"写进每一个调用点。
    ★ 只回灌会级字段；`todaydonate`/`created`/`shopLevel`/`donate_date` 是账号自己的，不动。
    迁移完成后世界名册是真值；账号弱引用和会名漂移时一并纠正。
    """
    if acc is None:
        return False
    gid, g, row = _members_of(acc)
    sg = (acc.data or {}).get('sociaty')
    if not isinstance(sg, dict):
        if not g or row is None:
            return False
        sg = acc.data['sociaty'] = {}
    if not g or row is None:
        if sg.get('guild_id') or sg.get('name') or sg.get('id'):
            sg['guild_id'] = ''
            sg['name'] = ''
            sg['id'] = ''
            return True
        return False
    before = (sg.get('name'), sg.get('guild_id'), sg.get('id'), sg.get('icon'), sg.get('level'),
              sg.get('wealth'), sg.get('notice'), sg.get('autoJoin'),
              sg.get('autoJoinThreshold'))
    sg['name'] = str(g.get('name') or '')
    sg['guild_id'] = str(gid)
    sg['id'] = str(gid)     # ★1121 的第 14 槽就是它（客户端当"公会 id"展示/回传）；
                            #   不同步的话会出现"1122 的 gangId=g00001 而 1121 的 sociatyId=''"
    sg['icon'] = _i(g.get('icon', 0), 0)
    sg['level'] = _i(g.get('level', 1), 1)
    sg['wealth'] = _i(g.get('wealth', 0), 0)
    sg['notice'] = str(g.get('notice') or '')
    sg['autoJoin'] = _i(g.get('autoJoin', 0), 0)
    sg['autoJoinThreshold'] = _i(g.get('autoJoinThreshold', 0), 0)
    after = (sg.get('name'), sg.get('guild_id'), sg.get('id'), sg.get('icon'), sg.get('level'),
             sg.get('wealth'), sg.get('notice'), sg.get('autoJoin'),
             sg.get('autoJoinThreshold'))
    return before != after


def member_aids(g):
    mem = g.get('members') if isinstance(g.get('members'), dict) else {}
    return sorted(mem.keys())


def member_count(g):
    return len(member_aids(g))


def leader_aid(g):
    """盟主账号 id：职位 `L` 优先，其次名册里第一个（自举的老会没有 `L`）。"""
    mem = g.get('members') if isinstance(g.get('members'), dict) else {}
    for aid in sorted(mem.keys()):
        if str((mem.get(aid) or {}).get('position') or '') == POS_LEADER:
            return aid
    aids = sorted(mem.keys())
    return aids[0] if aids else None


def leader_name(g, reg=None):
    aid = leader_aid(g)
    if not aid:
        return ''
    reg = reg or use()
    acc = _acc_of(reg, aid) if reg is not None else None
    if acc is None:
        return '未命名#' + aid
    return str((acc.data or {}).get('role_name') or ('未命名#' + aid))


def list_rows():
    """1122/1143 的**榜单行**（`_world_guilds()` 的替代）：按财富降序，`mine` 标出我那个会。

    未启用 -> `None`；启用了但一个会都没有 -> `[]`（**合法**结果）。

    ⚠ **成员数为 0 的会不进榜**：名册空了就说明"一个人都没有"，此时它只是个残留记录。
      不过滤的失败形态是**幽灵妖盟**——`world_rebuild_roster()` 修名册漂移时会把没人认领的
      会留在世界键里（刻意保留：它可能还有申请和公告），于是 1122 会显示一个
      `memberNum=0`、盟主是空字符串的会（本轮实测：`['万妖盟'(2), '天罡北斗'(5), ...(0,0)]`）。
    """
    reg = use()
    if reg is None:
        return None
    me = my_aid()
    rows = []
    for gid, g in sorted(_guilds(reg).items()):
        if not isinstance(g, dict) or not str(g.get('name') or ''):
            continue
        mem = g.get('members') if isinstance(g.get('members'), dict) else {}
        if not mem:
            continue                      # 幽灵会（名册空了）不进榜
        rows.append({'id': str(gid), 'name': str(g.get('name') or ''),
                     'level': _i(g.get('level', 1), 1), 'wealth': _i(g.get('wealth', 0), 0),
                     'icon': _i(g.get('icon', 0), 0), 'notice': str(g.get('notice') or ''),
                     'autoJoin': _i(g.get('autoJoin', 0), 0),
                     'autoJoinThreshold': _i(g.get('autoJoinThreshold', 0), 0),
                     'members': len(mem), 'leader': leader_name(g, reg),
                     'mine': bool(me and me in mem)})
    rows.sort(key=lambda r: (-r['wealth'], -r['members'], r['name']))
    return rows


def my_guild_row():
    """我那个会的榜单行（不在会 -> `None`）。给 1143 的 `msg[2]` 用。"""
    reg = use()
    if reg is None:
        return None
    gid, g = my_guild(reg=reg)
    if not g:
        return None
    return {'id': str(gid), 'name': str(g.get('name') or ''),
            'level': _i(g.get('level', 1), 1), 'wealth': _i(g.get('wealth', 0), 0),
            'icon': _i(g.get('icon', 0), 0), 'members': member_count(g),
            'leader': leader_name(g, reg), 'mine': True}


def rows_for_rank():
    """1143 用的榜单行（含我那个会）。未启用 -> `None`。"""
    return list_rows()


def application_rows(acc=None):
    """1125 的申请行：`[[roleid, rolename, img, imgframe, points, level], ...]`。

    `roleid` 用 `role_id_of(申请人)`（跨账号不变），**回传 1126 时按它反查申请人**。
    未启用 -> `None`；未入会/没有权限 -> `[]`（客户端 `pairs` 遍历空安全）。
    """
    reg = use()
    if reg is None:
        return None
    gid, g = my_guild(acc, reg)
    if not g:
        return []
    apps = g.get('applications') if isinstance(g.get('applications'), dict) else {}
    rows = []
    for rid, a in sorted(apps.items(), key=lambda kv: _i((kv[1] or {}).get('at'), 0)):
        if not isinstance(a, dict):
            continue
        rows.append([_i(rid, 0), str(a.get('name') or ('未命名#' + str(a.get('aid') or ''))),
                     _i(a.get('img', 1001), 1001), _i(a.get('imgframe', 1001), 1001),
                     _i(a.get('points', 0), 0), _i(a.get('level', 1), 1)])
    return rows


def find_applicant(role_id):
    """1126 的 `roleid` -> 申请人账号 id。**只在"我会的申请列表"里找**，找不到 -> `None`。

    即使 roleId 已真值化，也只接受本会已有的申请记录，不能由一个合法账号 id
    推定该账号已经申请本会。
    """
    reg = use()
    if reg is None:
        return None
    gid, g = my_guild(reg=reg)
    if not g:
        return None
    apps = g.get('applications') if isinstance(g.get('applications'), dict) else {}
    a = apps.get(str(role_id))
    if not isinstance(a, dict):
        return None
    return str(a.get('aid') or '') or None


def find_member(role_id):
    """1127/1130 的 `roleid` -> **同会**成员账号 id。找不到 -> `None`。

    ★ 客户端发上来的 `roleid` 现在就是**真实 roleId**（`role_id_of`），所以这里直接比对。
      `role_id == 1` 命中 `u0001`（它的真实 id 就是 1）—— 不再是"硬编码的我自己"。
    """
    reg = use()
    if reg is None:
        return None
    _gid, g = my_guild(reg=reg)
    if not g:
        return None
    rid = _i(role_id, -1)
    mem = g.get('members') if isinstance(g.get('members'), dict) else {}
    for aid in sorted(mem.keys()):
        if role_id_of(aid) == rid:
            return aid
    return None


# ===========================================================================
# 写：给 1123 / 1124 / 1126 / 1127 / 1129 / 1130 / 1131 / 1132 / 1140 用
# ===========================================================================
def _persist(reg, g, *accounts_to_mirror):
    """一次改动的统一收尾：回灌成员缓存 + **落盘世界档** + 落盘被改动的账号档。

    ⚠ `reg.save(acc)` 直接写**指定**账号的文件，不走 `save_state()` —— 后者按当前上下文路由，
      在"改别人档"的路径上会写错人（铁律 2）。
    """
    for acc in accounts_to_mirror:
        if acc is None:
            continue
        reg._mirror_guild_into_account(acc, g)
        reg.save(acc)
    reg.save_world()
    return True


@atomic
def create(name, icon=0, acc=None):
    """建会。返回 `(gid, err_code)`（`err_code` 为 `None` 表示成功）。

    码沿用 1123 的 `m_errorCode`：`30000` 名空 / `5031` 特殊字符 / `50002` 重名（会级，世界键判定）。
    """
    reg = use()
    if reg is None:
        return None, None
    import re
    from accounts import owner_ns as _on
    if acc is None:
        aid = my_aid()
        acc = _acc_of(reg, aid) if aid else None
    if acc is None:
        return None, 50001
    name = str(name or '').strip()
    if not name:
        return None, 30000
    if not re.match(r'^[\w\u4e00-\u9fa5]{1,12}$', name):
        return None, 5031
    # ⚠ 判据顺序：**重名(50002) 先于"已入会"(50003)**。两条同时成立时，"会名已被占用"
    #   信息量更大（"你已经有一个公会了"是玩家自己早就知道的事）。
    if guild_of_name(reg, name)[0]:              # ★会名唯一性现在是**世界级**判定
        return None, 50002
    _gid0, g0, _row0 = _members_of(acc)
    if g0 is not None:
        return None, 50003                       # 已加入公会
    gid = reg._next_guild_id()
    g = dict(DEFAULT_GUILD)
    g['members'] = {}
    g['applications'] = {}
    g['id'] = gid
    g['name'] = name
    g['icon'] = _i(icon, 0)
    g['created'] = time.strftime('%Y-%m-%d %H:%M:%S')
    _guilds(reg)[gid] = g
    reg._guild_member_set(g, acc, POS_LEADER)
    _persist(reg, g, acc)
    return gid, None


@atomic
def apply_join(gid, acc=None):
    """1124 申请入会。返回 `(statecode, joined)`。

    · 目标会开了 `autoJoin` 且我的排位积分 ≥ `autoJoinThreshold` -> **直接加入**（`joined=True`）；
    · 否则写进 `applications` 等审批（`joined=False`）。
    码沿用 `NetApplyJoinSociaty.m_errorCode`：`30001` 会 id 错 / `50007` 已入会 / `50001` 不存在
    / `50011` 重复申请；`1131`（直接加入）额外用 `50021` 已申请过。
    """
    reg = use()
    if reg is None:
        return None, False
    if acc is None:
        aid = my_aid()
        acc = _acc_of(reg, aid) if aid else None
    if acc is None:
        return 50001, False
    g = _guilds(reg).get(str(gid))
    if not isinstance(g, dict) or not str(g.get('name') or ''):
        return 50001, False                      # 不存在的工会
    _gid0, g0, _row0 = _members_of(acc)
    if g0 is not None:
        return 50007, False                      # 你已经加入了一个工会
    # 自动加入：门槛用**我的排位积分**（客户端 `gamer_getRankIntegral()` 同口径）
    pts = _my_points(acc)
    if _i(g.get('autoJoin', 0), 0) == 1 and pts >= _i(g.get('autoJoinThreshold', 0), 0):
        join(g, acc, POS_MEMBER)
        return 1, True
    apps = g.setdefault('applications', {})
    if not isinstance(apps, dict):
        apps = g['applications'] = {}
    rid = role_id_of(acc.id)
    if isinstance(apps.get(str(rid)), dict):
        # 1124=50011「请勿重复提交入会申请」/ 1131=50021「已发送入会申请」——
        # 码由各自的 handler 翻（两处客户端 `m_errorCode` 是**不同**的表）。
        return 50011, False
    apps[str(rid)] = {'aid': acc.id,
                      'name': str((acc.data or {}).get('role_name') or ('未命名#' + acc.id)),
                      'level': _i((acc.data or {}).get('player_lv', 1), 1),
                      'points': pts,
                      'img': _acc_head(acc), 'imgframe': _acc_frame(acc),
                      'at': int(time.time())}
    reg.save(acc)
    reg.save_world()
    return 1, False


def _my_points(acc):
    q = (acc.data or {}).get('qualify')
    if isinstance(q, dict):
        return _i(q.get('points', 0), 0)
    return 0


def _acc_head(acc):
    try:
        v = _i(((acc.data or {}).get('head') or {}).get('eq', 1001), 1001)
        return v if v else 1001
    except Exception:
        return 1001


def _acc_frame(acc):
    try:
        v = _i(((acc.data or {}).get('head') or {}).get('frame_eq', 1001), 1001)
        return v if v else 1001
    except Exception:
        return 1001


@atomic
def join(g, acc, position=POS_MEMBER, reg=None):
    """把 `acc` 写进 `g['members']` 并落盘。返回 `True`。"""
    reg = reg or use()
    if reg is None or not isinstance(g, dict):
        return False
    reg._guild_member_set(g, acc, position)
    apps = g.get('applications')
    if isinstance(apps, dict):
        apps.pop(str(role_id_of(acc.id)), None)
    _persist(reg, g, acc)
    return True


@atomic
def approve(role_id, agree, acc=None):
    """1126 审批入会。返回 `statecode`（`1` 成功）。

    码沿用 `NetApplySociaty.m_errorCode`：`30002` 申请人 id 错 / `50000` 未入会 /
    `50001` 不存在 / `50012` 人数上限；另按铁律给 `50005`（权限不足）——
    客户端 `m_errorCode` 里没有它，`showTips(nil)` 只弹空提示，但**服务端拒绝**比"假装成功"正确。
    """
    reg = use()
    if reg is None:
        return None
    if acc is None:
        aid = my_aid()
        acc = _acc_of(reg, aid) if aid else None
    gid, g, row = _members_of(acc) if acc is not None else (None, None, None)
    if not g or row is None:
        return 50000                              # 请先加入一个工会
    if not can_manage(acc):
        return 50005                              # 公会权限不足
    apps = g.get('applications') if isinstance(g.get('applications'), dict) else {}
    a = apps.get(str(role_id))
    if not isinstance(a, dict):
        return 30002                              # 申请人 id 错误（不在我的申请列表里）
    target = _acc_of(reg, str(a.get('aid') or ''))
    if target is None:
        apps.pop(str(role_id), None)
        reg.save_world()
        return 30002
    if not agree:
        apps.pop(str(role_id), None)
        reg.save_world()
        return 1
    _g2, g2, _r2 = _members_of(target)
    if g2 is not None:
        apps.pop(str(role_id), None)              # 已经被批过了 -> 幂等清掉
        reg.save_world()
        return 1
    if member_count(g) >= 50:                     # 人数上限：服务端没有真实配置 -> 用一个明确值
        return 50012                              # 工会已经到达人数上限
    join(g, target, POS_MEMBER, reg=reg)
    return 1


@atomic
def kick(role_id, acc=None):
    """1127 踢人。返回 `statecode`。码沿用 `NetKickOutSociaty.m_errorCode`。

    ⚠ **判据顺序是有意义的**（本轮护栏实测）：`50005 公会权限不足` 必须**最先**判 ——
      一个没有管理权限的人不该从错误码里读出"名册里有没有这个人"（否则"不存在"与"没权限"
      会互相掩盖）。权限过了之后才轮到 `50014 不可以踢自己`：客户端把"我自己"写作
      `roleid = 1`，而 `find_member` 要靠 `my_aid()` 才能把它翻成真实账号 id
      ⇒ 先找人会让自踢退化成 `30002 申请人id错误`（玩家看到的提示完全不对）。
    """
    reg = use()
    if reg is None:
        return None
    if acc is None:
        acc = current_account()
    gid, g, row = _members_of(acc) if acc is not None else (None, None, None)
    if not g or row is None:
        return 50000                              # 请先加入一个公会
    rid = _i(role_id, -1)
    if not can_manage(acc):
        return 50005                              # 公会权限不足
    if acc is not None and rid == role_id_of(acc.id):
        return 50014                              # 不可以踢自己
    target_aid = find_member(rid)
    if target_aid is None:
        return 30002                              # 申请人 id 错误
    if acc is not None and target_aid == acc.id:
        return 50014                              # 不可以踢自己
    _g2, g2, trow = _members_of(_acc_of(reg, target_aid))
    if g2 is None or trow is None:
        return 50013                              # 目标角色已经不在公会中
    if str(trow.get('position') or '') in (POS_LEADER, POS_DEPUTY):
        # 盟主/副盟主不可被**平级或下级**踢（客户端无此码，语义上必须拒）
        if str(row.get('position') or '') != POS_LEADER:
            return 50005
    remove_member(g, target_aid, reg=reg)
    return 1


@atomic
def remove_member(g, aid, reg=None):
    """把 `aid` 从名册里摘掉（会空则**保留会本身**：它可能还有申请/公告，删了会让 id 复用）。"""
    reg = reg or use()
    mem = g.get('members') if isinstance(g.get('members'), dict) else {}
    mem.pop(str(aid), None)
    g['members'] = mem
    acc = _acc_of(reg, aid) if reg is not None else None
    if acc is not None:
        sg = (acc.data or {}).get('sociaty')
        if isinstance(sg, dict):
            sg['guild_id'] = ''
            sg['name'] = ''          # ★一对弱引用成对清（只清 id 会让 1121 仍显示"我在会里"）
            # ★★ `sociaty['id']` 也必须清：它是**legacy 的"有没有会"标志**，而
            #    `handle_sociaty_apply_join_req` 的守卫读的就是它（`sg.get('id')`）
            #    ⇒ 只清 name/guild_id 的话，被踢的人再申请会拿到 `50007 你已经加入了一个工会`，
            #    而名册里根本没有他（本轮实测踩到）。
            sg['id'] = ''
    _persist(reg, g, acc)
    return True


@atomic
def leave(acc=None, dismiss=False):
    """1129 退会 / 1140 解散。返回 `statecode`。

    · 盟主退会 = **解散**（否则群龙无首，名册里没人能审批/踢人）；
    · 成员退会只摘自己。
    """
    reg = use()
    if reg is None:
        return None
    if acc is None:
        aid = my_aid()
        acc = _acc_of(reg, aid) if aid else None
    gid, g, row = _members_of(acc) if acc is not None else (None, None, None)
    if not g or row is None:
        return 50000
    pos = str(row.get('position') or '')
    if dismiss or pos == POS_LEADER:
        # 解散：把名册上**每个人**的弱引用清掉，而不是只清发起人 —— 否则别人的档还挂着一个
        # 已经不存在的会（`guild_of` 会返回 (None,None)，但档里的会名会让 1121 显示"我还在会里"）。
        for aid in list(member_aids(g)):
            a = _acc_of(reg, aid)
            if a is not None and a.id != (acc.id if acc else None):
                sg = (a.data or {}).get('sociaty')
                if isinstance(sg, dict):
                    sg['guild_id'] = ''
                    sg['name'] = ''          # 成对清（同上）
                    sg['id'] = ''
                reg.save(a)
        g['members'] = {}
        g['applications'] = {}
        _persist(reg, g, acc)
    else:
        remove_member(g, acc.id, reg=reg)
    return 1


@atomic
def set_position(role_id, code, acc=None):
    """1130 职位变更。返回 `statecode`。码沿用 `NetPostChangeSociaty.m_errorCode`。"""
    reg = use()
    if reg is None:
        return None
    if acc is None:
        aid = my_aid()
        acc = _acc_of(reg, aid) if aid else None
    gid, g, row = _members_of(acc) if acc is not None else (None, None, None)
    if not g or row is None:
        return 50001                              # 公会不存在或已解散
    if _pos_of_code(code) is None:
        return 50019                              # 非法职位代码
    new_pos = _pos_of_code(code)
    # ⚠ 判据顺序同 `kick()`：权限要早于"目标存不存在"，否则没权限的人能从错误码里
    #   读出"名册里有没有这个人"（50017 vs 50005）。
    if not can_manage(acc):
        return 50005                              # 公会权限不足
    target_aid = find_member(role_id)
    if target_aid is None:
        return 50017                              # 只可以对同公会玩家操作
    trow = (g.get('members') or {}).get(target_aid) or {}
    if str(trow.get('position') or '') == new_pos:
        return 50020                              # 请勿重复设置职位
    # 只有盟主能任免盟主与副盟主；副盟主只能任免普通成员/管理员
    #   ⚠ **例外：副盟主可以被"提为盟主"** —— 那正是接任/让位那一步：
    #     客户端 `VSociatyMember.lua:480` 的 `transferBtn` 要"我方 L 且对方 FL"，
    #     而服务端这一步得能跑完；第一版把 `new_pos == POS_LEADER` 一律挡住，
    #     结果是**新盟主永远升不上去**（`50005`），而老盟主已经把自己降成 N 了
    #     ⇒ 会里**没有盟主**（本轮实测踩到：`{'u0004': 'N', 'u0005': 'FL'}`）。
    if str(row.get('position') or '') != POS_LEADER:
        _i_am_deputy = str(row.get('position') or '') == POS_DEPUTY
        if new_pos == POS_LEADER:
            if not _i_am_deputy:
                return 50005                      # 公会权限不足
        elif new_pos == POS_DEPUTY or str(trow.get('position') or '') in (
                POS_LEADER, POS_DEPUTY):
            return 50005                          # 公会权限不足
    if new_pos in (POS_LEADER, POS_DEPUTY):
        for aid2 in member_aids(g):
            if aid2 == target_aid:
                continue
            r2 = (g.get('members') or {}).get(aid2) or {}
            if str(r2.get('position') or '') == new_pos:
                return 50018                      # 职位已满（盟主/副盟主各一名）
    if new_pos == POS_LEADER:
        # 罢免原盟主：降为普通成员（客户端 `VSociatyMember.lua:586-595` 也是这么本地处理的）
        for aid2 in member_aids(g):
            r2 = (g.get('members') or {}).get(aid2) or {}
            if aid2 != target_aid and str(r2.get('position') or '') == POS_LEADER:
                r2['position'] = POS_MEMBER
    trow['position'] = new_pos
    _persist(reg, g, acc, _acc_of(reg, target_aid))
    return 1


@atomic
def set_notice(txt, acc=None):
    """1128 设置公告（**会级**：修前只写自己那一份，别人永远看不到）。返回 `statecode`。"""
    reg = use()
    if reg is None:
        return None
    gid, g = my_guild(acc, reg)
    if not g:
        return 50000
    if not can_manage(acc):
        return 50005
    g['notice'] = str(txt or '')[:200]
    # 公告是**会级**字段，回灌到**全体成员**的缓存（否则别人面板里还是旧公告）
    _persist(reg, g, acc)
    for aid in member_aids(g):
        if acc is not None and aid == acc.id:
            continue
        a = _acc_of(reg, aid)
        if a is not None:
            reg._mirror_guild_into_account(a, g)
            reg.save(a)
    return 1


@atomic
def set_join_type(auto, threshold, acc=None):
    """1132 修改入会类型（**会级**）。返回 `statecode`。"""
    reg = use()
    if reg is None:
        return None
    gid, g = my_guild(acc, reg)
    if not g:
        return 50000
    if not can_manage(acc):
        return 50005
    g['autoJoin'] = 1 if _i(auto, 0) == 1 else 0
    g['autoJoinThreshold'] = max(0, _i(threshold, 0))
    _persist(reg, g, acc)
    return 1


@atomic
def add_wealth(delta, acc=None):
    """给（我所在的）会加/减财富。**会级字段** —— 修前写的是自己档里的 `wealth`。返回新值或 `None`。"""
    reg = use()
    if reg is None:
        return None
    gid, g = my_guild(acc, reg)
    if not g:
        return None
    g['wealth'] = max(0, _i(g.get('wealth', 0), 0) + _i(delta, 0))
    _persist(reg, g, acc)
    return g['wealth']


@atomic
def chess_upgrade(acc=None, cost=0, num_max=1):
    """1146 升级妖盟弈局。返回 `(statecode, new_level)`；`statecode == 1` 为成功。

    码（`NetSociatyChessUpgrade.m_errorCode`）：`50000` 未入会 / `50005` 权限不足 /
    `50045` 妖盟等级不足 / `50046` 财富不足 / `50049` 已满级。
    ⚠ `cost` 由**调用方**给（`UN06[当前等级].UpgradeConsume`）—— 与 1139/1142 同一口径：
      价格表在 handler 侧（`handlers_sociaty._UN06/_UN05`），世界层只管"钱够不够、扣多少"。
    """
    reg = use()
    if reg is None:
        return None, None
    gid, g = my_guild(acc, reg)
    if not g:
        return 50000, None
    if not can_manage(acc):
        return 50005, None
    c = chess_record(g)
    chess_reset_day(g)
    lv = max(1, _i(c.get('level', 1), 1))
    if lv >= max(1, _i(num_max, 1)):
        return 50049, lv                   # 已经满级
    cost = max(0, _i(cost, 0))
    if _i(g.get('wealth', 0), 0) < cost:
        return 50046, lv                   # 财富不足
    g['wealth'] = max(0, _i(g.get('wealth', 0), 0) - cost)
    c['level'] = lv + 1
    _persist(reg, g, acc)
    if acc is not None:
        sync_account(acc)                  # `sociaty.level` 是缓存，改了会级就回灌（1121 读它）
        reg.save(acc)
    return 1, c['level']


@atomic
def chess_buy_num(acc=None, cost=0, price_max=0):
    """1147 购买开局次数。返回 `(statecode, num_left)`。

    码（`NetSociatyChessNumBuy.m_errorCode`）：`50000` / `50005` / `50046`。
    ⚠ 购买上限 `_CHESS_NUM_BUY_MAX` 服务端自己定（客户端只发空包、没有上限字段）；
      超限返回 `50046`（"财富不足"是这张表里唯一语义接近的码，**不编新码**）。
    """
    reg = use()
    if reg is None:
        return None, None
    gid, g = my_guild(acc, reg)
    if not g:
        return 50000, None
    if not can_manage(acc):
        return 50005, None
    c = chess_record(g)
    chess_reset_day(g)
    if _i(c.get('num_bought', 0), 0) >= _CHESS_NUM_BUY_MAX:
        return 50046, None
    cost = max(0, _i(cost, 0))
    if _i(g.get('wealth', 0), 0) < cost:
        return 50046, None
    g['wealth'] = max(0, _i(g.get('wealth', 0), 0) - cost)
    c['num_bought'] = _i(c.get('num_bought', 0), 0) + 1
    c['num_max'] = max(1, _i(c.get('num_max', 1), 1)) + 1
    _persist(reg, g, acc)
    return 1, chess_snapshot(g, getattr(acc, 'id', None))['num_left']


@atomic
def chess_buy_entry(acc=None, cost=0):
    """1149 购买进入次数（扣**本账号**妖魂）。返回 `(statecode, entry_left)`。

    码沿用 1149 表：`50000` / `50005`；超上限/不够钱同样回 `50005`（该表最短，
    且它的语义就是"权限不足"= 这次操作不被允许）。
    """
    reg = use()
    if reg is None:
        return None, None
    gid, g = my_guild(acc, reg)
    if not g:
        return 50000, None
    if not can_manage(acc):
        return 50005, None
    c = chess_record(g)
    chess_reset_day(g)
    if _i(c.get('entry_bought', 0), 0) >= _CHESS_ENTRY_BUY_MAX:
        return 50005, None
    cost = max(0, _i(cost, 0))
    from accounts import EQ_STATE as _AS
    # ⚠ 键名是 `soulcoin`（`handlers_activity`/`handlers_draw`/`handlers_bag` 全用这个），
    #   不是 `soul_coin` —— 第一版写错名字，于是"永远余额 0"⇒ 每次购买都回 50005，
    #   而形态看起来完全像"权限不足"（最难查的一类失败：判据写错而不是逻辑没做）。
    if _i(_AS.get('soulcoin', 0), 0) < cost:
        return 50005, None
    _AS['soulcoin'] = max(0, _i(_AS.get('soulcoin', 0), 0) - cost)
    c['entry_bought'] = _i(c.get('entry_bought', 0), 0) + 1
    _persist(reg, g, acc)
    return 1, chess_snapshot(g, getattr(acc, 'id', None))['entry_left']


@atomic
def chess_start(stage, boss_stage, hp, findtime, acc=None):
    """1148 开启弈局。返回 `(statecode, active)`；`statecode == 1` 为成功。

    码（`NetSociatyStartBossWar.m_errorCode`）：`50000` 未入会 / `50005` 非盟主 /
    `50050` 不存在该弈局 / `50045` 妖盟等级不足 / `50051` 开启次数不足 /
    `50052` 存在未结束的弈局 / `50060` 不能重复开启。
    ⚠ `hp` 是 `UN07.HP`（**官方血量**），不是 `sociaty_boss` 里的编造值。
    """
    reg = use()
    if reg is None:
        return None, None
    gid, g = my_guild(acc, reg)
    if not g:
        return 50000, None
    if not can_manage(acc):
        return 50005, None
    c = chess_record(g)
    chess_reset_day(g)
    stage = _i(stage, 0)
    if stage <= 0:
        return 50050, None
    a = chess_active(g)
    if isinstance(a, dict):
        return 50052, None                 # 已有未结束的弈局
    num_max = max(1, _i(c.get('num_max', 1), 1)) + _i(c.get('num_bought', 0), 0)
    if _i(c.get('num_used', 0), 0) >= num_max:
        return 50051, None                 # 开启次数不足
    now = _chess_now()
    c['num_used'] = _i(c.get('num_used', 0), 0) + 1
    aid = getattr(acc, 'id', None) or my_aid()
    c['killer'] = None                     # 新的一局：上一局的"最后一击"作废
    c['active'] = {'stage': stage, 'boss_stage': _i(boss_stage, 0),
                   'hp_base': max(1, _i(hp, 1)), 'hp': max(0, _i(hp, 0)),
                   'deadline': now + max(60, _i(findtime, 1800)), 'started': now,
                   'killer_rid': 0, 'killer_name': '', 'over': False,
                   'damage': {}}
    _persist(reg, g, acc)
    return 1, chess_snapshot(g, aid)['active']


@atomic
def chess_join(stage, damage=0, acc=None):
    """1156 进入弈局。返回 `(statecode, info)`：`info = {'hp','left','entry_left','boss_stage'}`。

    码（`NetJoinSociatyChess.m_errorCode`）：`50000` 未入会 / `50050` 不存在该弈局 /
    `50053` 该弈局已经结束 / `50054` 该弈局未开启 / `50055` 进入次数不足。
    ⚠ **进入次数是 per-account 的**（每人每天 `UN06[等级].FightTimes` 次），记在
      弈局状态的 `damage[aid].entry_used` 里 —— 与"伤害"同一张表，天然共享。
    """
    reg = use()
    if reg is None:
        return None, None
    gid, g = my_guild(acc, reg)
    if not g:
        return 50000, None
    c = chess_record(g)
    chess_reset_day(g)
    stage = _i(stage, 0)
    a = chess_active(g)
    if not isinstance(a, dict):
        return 50054, None                 # 未开启（含已过期/已击杀）
    if stage and stage != _i(a.get('stage', 0), 0):
        return 50050, None
    aid = getattr(acc, 'id', None) or my_aid()
    d = a.setdefault('damage', {})
    rec = d.get(str(aid))
    if not isinstance(rec, dict):
        rec = d[str(aid)] = {'dmg': 0, 'at': 0, 'entry_used': 0}
    for k, v in (('dmg', 0), ('at', 0), ('entry_used', 0)):
        if k not in rec:
            rec[k] = v
    entry_max = max(1, _i(c.get('entry_max', 2), 2)) + _i(c.get('entry_bought', 0), 0)
    if _i(rec.get('entry_used', 0), 0) >= entry_max:
        return 50055, None                 # 进入次数不足
    rec['entry_used'] = _i(rec.get('entry_used', 0), 0) + 1
    dmg = max(0, _i(damage, 0))
    if dmg:
        rec['dmg'] = min(_CHESS_DMG_CAP, _i(rec.get('dmg', 0), 0) + dmg)
        rec['at'] = _chess_now()
    _persist(reg, g, acc)
    return 1, {'hp': max(0, _i(a.get('hp', 0), 0)),
               'left': max(0, _i(a.get('deadline', 0), 0) - _chess_now()),
               'entry_left': max(0, entry_max - _i(rec.get('entry_used', 0), 0)),
               'boss_stage': _i(a.get('boss_stage', 0), 0)}


@atomic
def chess_report_hp(stage, damage, acc=None):
    """1157 上报伤害（客户端发的是**本击伤害**，不是剩余血量）。返回 `(statecode, info)`。

    码：`50000` 未入会 / `50054` 未开启 / `50050` 弈局不匹配 / `582` 非法操作
    （未进入这次弈局却上报伤害 —— 客户端对 582 有专门的提示分支）。

    ⚠ 击杀判定在这里：伤害把 `hp` 打到 ≤0 -> 记最后一击、`over=True`、清空 `active`。
      返回的 `info['killed'] = True` 时 handler 要推 1152（最终血量）+ 1154（弈局结束）。
      ⇒ 伤害行必须在**清空 active 之前**取好，否则击杀那一次推送的排行榜会变成空的。
    """
    reg = use()
    if reg is None:
        return None, None
    gid, g = my_guild(acc, reg)
    if not g:
        return 50000, None
    stage = _i(stage, 0)
    a = chess_active(g)
    if not isinstance(a, dict):
        return 50054, None
    if stage and stage != _i(a.get('stage', 0), 0):
        return 50050, None
    aid = getattr(acc, 'id', None) or my_aid()
    d = a.setdefault('damage', {})
    rec = d.get(str(aid))
    if not isinstance(rec, dict) or _i(rec.get('entry_used', 0), 0) <= 0:
        return 582, None                   # 没进过这次弈局（客户端会提示"非法操作"）
    dmg = max(0, min(_CHESS_DMG_CAP, _i(damage, 0)))
    now = _chess_now()
    rec['dmg'] = min(_CHESS_DMG_CAP, _i(rec.get('dmg', 0), 0) + dmg)
    rec['at'] = now
    hp = max(0, _i(a.get('hp', 0), 0) - dmg)
    a['hp'] = hp
    killed = hp <= 0
    # ★2026-10-26：伤害行必须在 `over=True` / `deadline=now` **之前**取。
    #   修前放在 `if killed:` 之后 ⇒ `chess_active()` 一看 over 就清掉 active ⇒
    #   击杀那一次的 `info['damage_rows']` 恒为空（1152 的击杀推送排行榜为空，
    #   1153 的击杀快照也拿不到参战者）。
    rows_now = chess_damage_rows(g)
    name = _display_name_of(acc, aid)
    if killed:
        a['over'] = True
        a['deadline'] = now
        a['killer_rid'] = role_id_of(aid)
        a['killer_name'] = name
    info = {'hp': hp, 'hp_base': max(1, _i(a.get('hp_base', 1), 1)), 'damage': dmg,
            'total': _i(rec.get('dmg', 0), 0), 'killed': killed,
            'stage': _i(a.get('stage', 0), 0),
            'killer_rid': (_i(a.get('killer_rid', 0), 0) if killed else 0),
            'killer_name': (str(a.get('killer_name') or '') if killed else ''),
            'damage_rows': rows_now}
    _persist(reg, g, acc)
    if killed:
        c = chess_record(g)
        # ★2026-10-26：击杀快照必须自带**参战者 + 最终伤害**。修前只存 rid/name/at，
        #   1153 在 `active` 清空后只能读到空伤害行 ⇒ 所有领奖者都落到 Rank 0（最后一击奖）。
        c['killer'] = {'rid': _i(a.get('killer_rid', 0), 0), 'name': str(a.get('killer_name') or ''),
                       'stage': _i(a.get('stage', 0), 0), 'at': now,
                       'started': _i(a.get('started', 0), 0),
                       'boss_stage': _i(a.get('boss_stage', 0), 0),
                       'rows': [[str(x[0]), max(0, _i(x[1], 0)), _i(x[2], 0)]
                                for x in (rows_now or [])]}
        c['active'] = None                 # 已击杀 -> 弈局结束（伤害明细留在 info 里推出去）
        reg.save_world()
    return 1, info


def recalc_level(g, reg=None):
    """会等级 = **成员最大**（沿用改造前的派生口径，避免改变既有玩家看到的数字）。"""
    reg = reg or use()
    lv = 1
    mem = g.get('members') if isinstance(g.get('members'), dict) else {}
    for aid in mem.keys():
        acc = _acc_of(reg, aid)
        if acc is None:
            continue
        sg = (acc.data or {}).get('sociaty')
        if isinstance(sg, dict):
            lv = max(lv, _i(sg.get('level', 1), 1))
    g['level'] = max(1, lv)
    return g['level']


# ===========================================================================
# 妖盟红包（1187 列表 / 1188 发送 / 1189 领取）的**会级共享状态**
# ---------------------------------------------------------------------------
# 修前这三个号服务端**一个都没有** ⇒ 落 `resp_for` 兜底 `[1,[[]]]`。三个玩家现象
# 全部由**同一个**兜底体解释（不是三件事）：
#   · 1187 列表恒空 ⇒「当前还没有人发红包哦！」
#   · 1188 `NetSociatySendRedBag:recvMessage:43` 拿到 statecode=1 **就本地扣积分**
#          （`gamer_setIntegral(getIntegral() - moneyNum)`）、再照读 `msg[2]`（nil）
#          ⇒「**积分扣了，红包不见了**」
#   · 1189 `msg[1]` 是空表（Lua 里**真值**）、`msg[3]=nil -> tonumber(nil)=0`、
#          `msg[5]=nil or {}` ⇒ 面板照开、显示「**恭喜获得 0**」
# ⇒ 修法只有一个：给它们**真的会级共享数据源**（照 `chess` 的写法）。
#
# ★ 语义与错误码**全部有出处**，不是设计的：
#   · `T41_CodeKey[1219].codeKey = GANG_REDPOCKET_TIPS` 逐字给出规则：
#       【发红包限制】1、当天发红包次数尚未用完  2、妖盟成员不低于5人
#       【领红包限制】1、当天还剩余领红包次数
#       【充值礼包说明】…系统会自动以该玩家的名义给妖盟成员发放一个礼包，不消耗该玩家任何物品
#     ⇒ 发/领各有一条**每日次数**、发还额外要求**会人数 ≥5**；`redType=2`（充值礼包）是
#       系统在成员充值后**代发**的（不扣发包人东西）—— 本服务端没有充值链路，故 2 类只会
#       从运营/GM 侧产生，但领取路径照样实现（形状与 1 类同）。
#   · 线上错误码 = `T41_CodeKey.codeValue`（**不是 ID**，两列差 1000+）：
#       58=鬼道积分不足 · 981=文本不合法 ·
#       30404=妖盟红包超过最大上限或小于最低限制 · 30405=暂无妖盟红包 ·
#       30406=今日普通红包发送次数已达上限 · 30407=今日普通红包领取次数已达上限 ·
#       30408=加入妖盟时间不足 · 30409=妖盟人数不足 · 30410=今日充值礼包领取次数已达上限 ·
#       30411=红包已领完
#     ⇒ 这也解释了客户端 `NetSociatyRedBagList.lua:27` 为什么单独对 **30405 静默 return**：
#       "这个会没有红包"是常态，不该弹提示（真值见 `_REDBAG_CODE`）。
#   · 领取上限 = `T23_Function[ID=247].Value` = **5**（`VSociatyRedBag.lua:131` 用**同一个值**
#     渲染「每天最多可领5个红包哦！」）。⚠ 客户端这里按 **ID** 列查，而服务端其它
#     `_t23_value()` 按 **Type** 列查 —— 两列都要会读（见 `handlers_sociaty._t23_by_id`）。
#
# ⚠ **发送次数上限无表源**：`T23_Function` 199 行逐行核过，客户端也没有读它（T41 只给了
#   文案 30406，没给数字）⇒ 只能是服务端常量 `_REDBAG_SEND_MAX`，改动只此一处。
#   **不要**去某张表里"凑"一个数（那正是本项目反复吃亏的"假装表里有"）。
# ⚠ **30408（入会时长不足）有码无阈值** ⇒ **不启用该门**：宁可不拦，不可误拦
#   （阈值猜错会把正常玩家挡在门外，而这是玩家可见的功能不可用）。
# ⚠ 到期未领完的余额**直接退还发包人**（`moneyType=6` -> `kidou`）。官方设计是发一封
#   `MAIL_REDPACKET_BACK_TITLE/INFO`（T41:1214/1215）通知，但本服务端的邮件是**全局 GM 配置**
#   （`gm_mail.json` 对所有账号可见）⇒ **没有"给某个账号发一封邮件"的通道**，
#   所以退回不附通知。钱一分不少，只是玩家看不到那封信。
#
#   world['guilds'][gid]['redbag'] = {
#     'date': 'YYYY-MM-DD',   # `sent` 属于哪一天（惰性重置，同 chess）
#     'sent': 0,              # 今日已发送次数；1187 的 msg[1] = _REDBAG_SEND_MAX - 它
#     'seq': 0,               # redId 自增（会内唯一、单调递增、从不回收）
#     'list': [ 一条红包, ... ],   # 新发的**追加在末尾**（客户端按 ipairs 顺序铺列）
#   }
#   一条红包 = {
#     'redId': 1, 'redType': 1,            # 1=普通(鬼道积分) 2=充值礼包（= UN18.Type）
#     'moneyType': 6, 'moneyNum': 200,     # 发包人实际付的钱；redType=2 恒 0（系统代发）
#     'redNum': 10,                        # 红包**个数**（不是金额）
#     'content': '恭喜发财',
#     'goodsType': 0, 'goodsId': 0,        # redType=2 用（UN18.Content / ContentNum）
#     'goodsNum': 1,                       # redType=2 每份礼包给几个（UN18.LowerNum）
#     'lowerNum': 1, 'upperNum': 170,      # 单次领取的上下界（UN18.LowerNum / UpperNUm）
#                                          #   —— 发包时**冻结**在红包上，之后不随表变
#     'senderAid': 'u0001', 'senderName': '甲',
#     'senderAvatar': 1001, 'senderAvatarBox': 1001,
#     'created': ts, 'expire': ts + 24*3600,
#     'claims': [{'aid','name','amount','at'}],   # 领取明细（顺序即榜单顺序）
#   }
_REDBAG_SEND_MAX = 5          # 每日发送次数上限（**服务端常量**：T41 只给了文案，没给数）
_REDBAG_MEMBER_MIN = 5        # 发红包的最低会人数（T41_CodeKey[1219] 原文「妖盟成员不低于5人」）
_REDBAG_EXPIRE_HOURS = 24     # 兜底有效期（逐档传 UN18.Time；表里 9 行都是 24）
_REDBAG_CONTENT_MAX = 20      # 内容长度（客户端 `MeEditBox:setMaxLength(20)`，超了回 981）
_REDBAG_MONEY_TYPE = 6        # 客户端硬编码的 moneyType（`VSociatySendRedBag.lua:139` 第一个实参）

_REDBAG_CODE = {
    'no_money': 58,           # LOCAL_NET_INTEGRAL_IS_UNENOUGHT 鬼道积分不足
    'bad_text': 981,          # LOCAL_CHAT_NO_EFFECT 文本不合法
    'num_wrong': 30404,       # GANG_REDPOCKET_NUM_WRONG
    'not_found': 30405,       # GANG_REDPOCKET_NOT 暂无妖盟红包（客户端**静默**分支）
    'send_max': 30406,        # GANG_REDPOCKET_SEND_MAX
    'get_max': 30407,         # GANG_REDPOCKET_GET_MAX
    'join_time': 30408,       # GANG_REDPOCKET_JOIN_TIME_NOT_GET（**不启用**，见上）
    'people_not': 30409,      # GANG_REDPOCKET_PEOPLE_NOT_SEND
    'gift_get_max': 30410,    # GANG_REDPOCKET_REC_GET_MAX
    'finished': 30411,        # GANG_REDPOCKET_GET_FINISH 红包已领完
    'no_guild': 30405,        # 未入会/会 id 对不上 —— 与"暂无妖盟红包"同一个码（语义就是它）
}


def _redbag_now():
    return int(time.time())


def _redbag_today():
    return time.strftime('%Y-%m-%d')


def _redbag_blank():
    return {'date': _redbag_today(), 'sent': 0, 'seq': 0, 'list': []}


def redbag_record(g):
    """这个会的 `redbag` 子状态；缺失/坏形状就地补一份（**返回对象可写**，落盘由调用方负责）。"""
    if not isinstance(g, dict):
        return None
    rb = g.get('redbag')
    if not isinstance(rb, dict):
        rb = g['redbag'] = _redbag_blank()
    for k, v in _redbag_blank().items():
        if k not in rb:
            rb[k] = dict(v) if isinstance(v, dict) else (list(v) if isinstance(v, list) else v)
    if not isinstance(rb.get('list'), list):
        rb['list'] = []
    return rb


def redbag_reset_day(g):
    """跨天惰性重置发送次数。返回 `True` 表示改了状态（**不主动落盘**，同 chess_reset_day）。"""
    rb = redbag_record(g)
    if rb is None:
        return False
    if str(rb.get('date') or '') == _redbag_today():
        return False
    rb['date'] = _redbag_today()
    rb['sent'] = 0
    return True


def redbag_claim_count(acc):
    """本账号**今日**已领的红包数 `(普通, 充值礼包)`。纯读，不建键。

    ⚠ 落点是 **per-account** 而不在会级：这是"每人每天"的限次（T41:1219【领红包限制】），
      与会级池子无关。与会级状态分开存是刻意的 —— 会级那份要能被所有成员看到。
    """
    d = (acc.data or {}).get('sociaty') if acc is not None else None
    rb = d.get('redbag') if isinstance(d, dict) else None
    if not isinstance(rb, dict) or str(rb.get('date') or '') != _redbag_today():
        return 0, 0
    return _i(rb.get('n1', 0), 0), _i(rb.get('n2', 0), 0)


def _redbag_bump_claim(acc, red_type):
    """记一次领取。返回记完之后的 (普通, 充值礼包) 次数。"""
    sg = acc.data.setdefault('sociaty', {})
    if not isinstance(sg, dict):
        sg = acc.data['sociaty'] = {}
    rb = sg.get('redbag')
    if not isinstance(rb, dict) or str(rb.get('date') or '') != _redbag_today():
        rb = sg['redbag'] = {'date': _redbag_today(), 'n1': 0, 'n2': 0}
    key = 'n2' if _i(red_type, 1) == 2 else 'n1'
    rb[key] = _i(rb.get(key, 0), 0) + 1
    return _i(rb.get('n1', 0), 0), _i(rb.get('n2', 0), 0)


def redbag_row_of(rb, aid, now=None):
    """一条红包 -> 1187/1188 的 10 槽行（列语义见 `NetSociatyRedBagList.lua:40-50`）。

    `status` 是**按请求者**算的：1=我已领 / 2=已领完 / 0=未领（客户端凭它决定「领取」按钮）；
    `redRewardTotalNum` 是**总金额**（`moneyNum`），`redTotalNum`/`redRemainNum` 是**个数**。
    """
    claims = [c for c in (rb.get('claims') or []) if isinstance(c, dict)]
    red_num = max(0, _i(rb.get('redNum', 0), 0))
    remain = max(0, red_num - len(claims))
    mine = None
    for c in claims:
        if str(c.get('aid') or '') == str(aid or ''):
            mine = c
            break
    if mine is not None:
        status = 1
    elif remain <= 0:
        status = 2
    else:
        status = 0
    return [_i(rb.get('redId', 0), 0), _i(rb.get('redType', 1), 1),
            red_num, remain, str(rb.get('content') or ''), status,
            str(rb.get('senderName') or ''), _i(rb.get('senderAvatar', 0), 0),
            _i(rb.get('senderAvatarBox', 0), 0), _i(rb.get('moneyNum', 0), 0)]


def redbag_unclaimed(g, aid):
    """这个会里**我还没领且还有剩**的红包数（给 8082 妖盟红包红点用）。"""
    rb = redbag_record(g)
    if rb is None:
        return 0
    now = _redbag_now()
    n = 0
    for it in rb.get('list') or []:
        if not isinstance(it, dict):
            continue
        if _i(it.get('expire', 0), 0) <= now:
            continue
        if redbag_row_of(it, aid, now)[5] == 0:
            n += 1
    return n


def _redbag_refund(reg, rb, now):
    """把**到期**红包的未领完余额退回发包人，并从列表移除。返回 `(退了几条, 需落盘的账号)`。

    `moneyType=6` 之外的单位本服务端没有钱包可退 ⇒ **只记一条 print、不动钱**（不编造）。
    """
    keep, gone, accounts, refunded = [], [], [], 0
    for it in rb.get('list') or []:
        if not isinstance(it, dict):
            continue
        if _i(it.get('expire', 0), 0) > now:
            keep.append(it)
            continue
        gone.append(it)
        mt = _i(it.get('moneyType', 0), 0)
        claims = [c for c in (it.get('claims') or []) if isinstance(c, dict)]
        left = max(0, _i(it.get('moneyNum', 0), 0) - sum(_i(c.get('amount', 0), 0) for c in claims))
        aid = str(it.get('senderAid') or '')
        if left <= 0 or not aid:
            continue
        if mt != _REDBAG_MONEY_TYPE:
            print('[REDBAG] 红包 %s 到期，moneyType=%s 无退款通道，余额 %s 未退'
                  % (it.get('redId'), mt, left), flush=True)
            continue
        acc = _acc_of(reg, aid)
        if acc is None:
            print('[REDBAG] 红包 %s 到期，发包人 %s 账号不存在，余额 %s 未退'
                  % (it.get('redId'), aid, left), flush=True)
            continue
        acc.data['kidou'] = _i(acc.data.get('kidou', 0), 0) + left
        accounts.append(acc)
        refunded += left
    rb['list'] = keep
    return len(gone), accounts, refunded


@atomic
def redbag_list(acc=None):
    """1187 列表。返回 `(statecode, remain_send, rows)`；`statecode == 1` 为成功。

    ⚠ **未入会**回 `30405`（与"暂无妖盟红包"同码）：客户端对它**静默 return**，既不弹提示
      也不清空已有列表 —— 这正是它该有的行为（玩家从妖盟面板进来时本来就在会里，
      这条只兜"会 id 对不上/已退会"的竞态）。
    """
    reg = use()
    if reg is None:
        return None, None, None                 # 单档：调用方回原形状（本文件不含单档语义）
    if acc is None:
        acc = current_account()
    gid, g = my_guild(acc, reg)
    if not g:
        return _REDBAG_CODE['no_guild'], None, None
    rb = redbag_record(g)
    reset = redbag_reset_day(g)
    touched, refunded = [], 0
    if rb['list']:
        # 顺手清理到期红包（惰性 sweep：本服务端没有后台定时器；读一次就是一次清理机会）
        _n, touched, refunded = _redbag_refund(reg, rb, _redbag_now())
        if _n:
            reset = True
    rows = [redbag_row_of(it, getattr(acc, 'id', None)) for it in rb['list']]
    if reset or touched:
        _persist(reg, g, acc, *touched)
    return 1, max(0, _REDBAG_SEND_MAX - _i(rb.get('sent', 0), 0)), rows


@atomic
def redbag_send(money_type, money_num, red_type, red_num, content,
                goods_type=0, goods_id=0, lower_num=1, upper_num=1, goods_num=1,
                expire_hours=None, acc=None):
    """1188 发送。返回 `(statecode, remain_send, rows)`；`statecode == 1` 为成功。

    码：`30409` 会人数不足 / `30404` 金额或个数越界 / `981` 文本不合法 / `58` 积分不足 /
    `30406` 今日发送次数已达上限。
    ⚠ `money_num` 必须是**该档位**的值（`UN18.Rank`，客户端只允许从列表里选），
      `red_num` 必须落在 `[LowerLimit, UpperLimit]` —— 两者都由 handler 从表里查出后传入；
      `lower_num`/`upper_num`（`UN18.LowerNum/UpperNUm`）是**单次领取**的上下界，
      存进红包里给 `_redbag_split` 用（红包一旦发出，档位参数就冻结在红包上）。
    """
    reg = use()
    if reg is None:
        return None, None, None
    if acc is None:
        acc = current_account()
    gid, g = my_guild(acc, reg)
    if not g:
        return _REDBAG_CODE['no_guild'], None, None
    rb = redbag_record(g)
    redbag_reset_day(g)
    # ① 会人数（T41:1219「妖盟成员不低于5人」）
    if member_count(g) < _REDBAG_MEMBER_MIN:
        return _REDBAG_CODE['people_not'], None, None
    # ② 文本（客户端 MeEditBox 限 20，超了不许进档 —— 否则会里所有人都会看到超长文本）
    txt = str(content or '')
    if len(txt) > _REDBAG_CONTENT_MAX:
        return _REDBAG_CODE['bad_text'], None, None
    # ③ 个数与金额
    red_type = _i(red_type, 1)
    red_num = _i(red_num, 0)
    money_num = _i(money_num, 0)
    if red_num <= 0 or money_num < 0:
        return _REDBAG_CODE['num_wrong'], None, None
    if red_type == 2:
        money_type, money_num = 0, 0            # 充值礼包：系统代发，**不消耗发包人任何物品**
    elif _i(money_type, 0) != _REDBAG_MONEY_TYPE:
        return _REDBAG_CODE['num_wrong'], None, None
    # ④ 今日发送次数
    if _i(rb.get('sent', 0), 0) >= _REDBAG_SEND_MAX:
        return _REDBAG_CODE['send_max'], None, None
    # ⑤ 钱（只有 1 类要扣；`kidou` 是鬼道积分的**唯一**落点，见 role_info_array[13]）
    if money_num > 0:
        from accounts import EQ_STATE as _AS
        if _i(_AS.get('kidou', 0), 0) < money_num:
            return _REDBAG_CODE['no_money'], None, None
        _AS['kidou'] = _i(_AS.get('kidou', 0), 0) - money_num
    now = _redbag_now()
    rb['seq'] = _i(rb.get('seq', 0), 0) + 1
    rb['sent'] = _i(rb.get('sent', 0), 0) + 1
    aid = getattr(acc, 'id', None) or my_aid()
    rb['list'].append({
        'redId': rb['seq'], 'redType': red_type,
        'moneyType': _i(money_type, 0), 'moneyNum': money_num,
        'redNum': red_num, 'content': txt,
        'goodsType': _i(goods_type, 0), 'goodsId': _i(goods_id, 0),
        'goodsNum': max(1, _i(goods_num, 1)),
        'lowerNum': max(1, _i(lower_num, 1)), 'upperNum': max(1, _i(upper_num, 1)),
        'senderAid': str(aid or ''), 'senderName': _display_name_of(acc, aid),
        'senderAvatar': _acc_head(acc), 'senderAvatarBox': _acc_frame(acc),
        'created': now,
        'expire': now + 3600 * max(1, _i(expire_hours, _REDBAG_EXPIRE_HOURS)),
        'claims': [],
    })
    rows = [redbag_row_of(it, aid) for it in rb['list']]
    _persist(reg, g, acc)
    return 1, max(0, _REDBAG_SEND_MAX - rb['sent']), rows


@atomic
def redbag_claim(red_id, claim_max=0, acc=None):
    """1189 领取。返回 `(statecode, info)`；`info = {'ucode','goodsType','goodsId','num','rows'}`。

    码：`30405` 红包不存在（含已到期被清掉）/ `30411` 已领完或我已领过 /
    `30407` 今日领取次数已达上限（`claim_max` 由 handler 从 `T23_Function[ID=247]` 取）。

    ⚠ **"我已经领过"回 30411 而不是"再给你看一遍上次的结果"**：客户端本地置灰了已领条目
      （`status==1 -> Button_Unlock:setVisible(false)`），能走到这里的只有重放包；
      重放一次就会再弹一次「恭喜获得」，而**钱不会再给** —— 回错误码比回一个会被误解的
      "成功"更诚实。T41 里"您已领取该礼包"（GANG_REDPOCKET_GET_YET）的 codeValue 是 **-1**，
      说明官方本来就没打算让服务端说这句话（是客户端自己拦的）。
    """
    reg = use()
    if reg is None:
        return None, None
    if acc is None:
        acc = current_account()
    gid, g = my_guild(acc, reg)
    if not g:
        return _REDBAG_CODE['no_guild'], None
    rb = redbag_record(g)
    reset = redbag_reset_day(g)
    now = _redbag_now()
    target = None
    for it in rb['list']:
        if isinstance(it, dict) and _i(it.get('redId', 0), 0) == _i(red_id, 0):
            target = it
            break
    if target is None:
        # 可能只是"到期了还没被 sweep 掉" —— 先把 sweep 做掉，再判一次
        _n, touched, _r = _redbag_refund(reg, rb, now)
        if _n or reset:
            _persist(reg, g, acc, *touched)
        return _REDBAG_CODE['not_found'], None
    aid = str(getattr(acc, 'id', None) or my_aid() or '')
    claims = [c for c in (target.get('claims') or []) if isinstance(c, dict)]
    for c in claims:
        if str(c.get('aid') or '') == aid:
            return _REDBAG_CODE['finished'], None       # 我已经领过（重放包）
    if _i(target.get('expire', 0), 0) <= now:
        return _REDBAG_CODE['not_found'], None
    red_num = max(0, _i(target.get('redNum', 0), 0))
    if len(claims) >= red_num:
        return _REDBAG_CODE['finished'], None
    red_type = _i(target.get('redType', 1), 1)
    n1, n2 = redbag_claim_count(acc)
    if red_type == 1 and claim_max > 0 and n1 >= claim_max:
        return _REDBAG_CODE['get_max'], None
    # ---- 分钱 / 发货 ----
    if red_type == 2:
        # 充值礼包：每个领取人拿到 UN18.LowerNum..UpperNUm 个（表里 9 行都是 1）
        amount = max(1, _i(target.get('goodsNum', 1), 1))
        got, goods_id, goods_type = amount, _i(target.get('goodsId', 0), 0), 98
        ucode = 0
    else:
        # 余额已经不够按该档下界发了 -> 当"已领完"拒掉，而不是发一个 < LowerNum 的数
        #   （客户端 `Text_Name_0_1` 直接显示这个金额，发 0 会让面板出现"恭喜获得 0"）
        _used = sum(_i(c.get('amount', 0), 0) for c in claims)
        _lo = max(1, _i(target.get('lowerNum', 1), 1))
        if max(0, _i(target.get('moneyNum', 0), 0) - _used) < _lo:
            return _REDBAG_CODE['finished'], None
        amount = _redbag_split(target, claims)
        got, goods_id, goods_type = amount, 0, _REDBAG_MONEY_TYPE
        ucode = 0
    # ---- 真入账（**走 handlers_task.grant_one**：全工程唯一一份"按类型发放"）----
    try:
        from handlers_task import grant_one
        groups = grant_one(goods_type, (goods_id if goods_type == 98 else 0), got)
        if goods_type == 98 and groups:
            for _g in groups:
                if _i(_g[0], -1) == 98 and _g[1]:
                    ucode = _i(_g[1][0][0], 0)
                    break
        elif not groups:
            return _REDBAG_CODE['finished'], None       # 发不出去就不记领取（不给"空领取"）
    except Exception as e:
        print('[REDBAG] 发放异常 redId=%s type=%s got=%s: %r'
              % (red_id, goods_type, got, e), flush=True)
        return _REDBAG_CODE['finished'], None
    claims.append({'aid': aid, 'name': _display_name_of(acc, aid),
                   'amount': got, 'at': now})
    target['claims'] = claims
    _redbag_bump_claim(acc, red_type)
    _persist(reg, g, acc)
    rows = [[str(c.get('name') or ''), _i(c.get('amount', 0), 0),
             _redbag_feeling(claims, c)] for c in claims]
    return 1, {'ucode': ucode, 'goodsType': goods_type, 'goodsId': goods_id,
               'num': got, 'rows': rows, 'redId': _i(target.get('redId', 0), 0)}


def _redbag_split(target, claims):
    """手气拆分：保证"每人 ≥1、总数**绝不超发**、最后一个拿走余额"。

    `UN18.LowerNum/UpperNUm`（1/170）是**官方给的单次上下界** ⇒ 可分配空间 =
      min(UpperNUm, 剩余金额 - (剩余人数-1) × LowerNum)，下界 = LowerNum。
    ★ 本函数的**唯一硬不变量是"返回值 ≤ 当前剩余金额"** —— 每一条 return 都过这个筛子。
      早期版本在"余额不够按官方下界发"时返回 `lo`，那会凭空造钱（金额差一类最贵的 bug），
      现在改成 `min(left, lo)`；并且 `redbag_claim` 会在 `left < lo` 时**直接拒绝**
      （回 30411），所以那条分支在真实表数据下不可达 —— 留它只是不让"表被改坏"变成造钱。
    """
    total = max(0, _i(target.get('moneyNum', 0), 0))
    used = sum(_i(c.get('amount', 0), 0) for c in claims)
    left = max(0, total - used)
    slots = max(1, _i(target.get('redNum', 1), 1) - len(claims))
    lo = max(1, _i(target.get('lowerNum', 1), 1))
    hi = max(lo, _i(target.get('upperNum', lo), lo))
    if slots <= 1:
        return left                                  # 最后一个：把余额全给他
    room = left - (slots - 1) * lo
    if room < lo:
        return max(0, min(left, lo))                 # 不够按官方下界发 -> 给到下界但**不超余额**
    return random.randint(lo, max(lo, min(hi, room)))


def _redbag_feeling(claims, me):
    """`feelingLuck`：1=最佳 2=最差 0=平常（客户端 `Gangs_38.csb` 的「ZuiJia/ZuiCha」标记）。

    ⚠ 判据必须在**全部已领明细**上独立算一遍，不能照抄"第几个领的"——
      它渲染的是"手气最好/最差"，与领取顺序无关。
    """
    if len(claims) <= 1:
        return 0
    amt = _i(me.get('amount', 0), 0)
    vals = [_i(c.get('amount', 0), 0) for c in claims]
    if amt > max(vals) or (amt == max(vals) and vals.count(amt) == 1):
        return 1
    if amt < min(vals) or (amt == min(vals) and vals.count(amt) == 1):
        return 2
    return 0
