# -*- coding: utf-8 -*-
"""handlers_specialatt.py —— 特攻规则面板 12000（新包 A0 第 12 批）。

**"特攻"是什么**：它是一个**跨活动共用**的"规则提示"面板（客户端 `VSpecialAttInfo` 直接
`createNode("YaoZhu_TeGong_RuleTips.csb")` —— 「妖主·特攻·规则提示」）。每个活动各有一套：
哪些妖怪算"特攻"（`attacktype`/`attackid`）、同阵营伤害加成（`damage`）、属性加成、
效果描述、额外掉落（`additionReward*`）、额外信物兑换加成（`additionMarketToken`）。
客户端按**活动类型**来问（`C_SPECIAL_ENTER_TYPE` 列了 16 种：2 妖主大入侵 / 3 限时充值 /
4 限时登录 / 5 累天充值 / 6 卡池 / 7 引导活动 / 8 单笔充值 / 9 夜市 / 10 妖魂转盘 /
11 消耗妖魂 / 12 召唤抽卡 / 13 鬼道积分转盘 / 14 消耗金币 / 15 消耗体力 / 16 限时任务 /
**18 额外掉落**）。

**⚠ 这些值是「运营/GM 配置」而不是客户端表**：`NetSpecialAtt.lua:33` 对 `msg[1]` 的注释就写着
「活动类型 **gm所需字段**」，两个客户端里也**没有任何** SpecialAtt/特攻 表（逐表核过：表根里
A0x 家族 11 张 + 目标客户端独有 5 张里都没有）。所以**本服没有可发的特攻规则** —— 硬凑一组伤害
百分比就是往玩家眼前灌假数值。

**⇒ 正确做法：回一个结构完整、`switchIs = 0`（特攻未开启）的 14 槽体。** 这不是"留空应付"，
而是**客户端自己写好的那条"未开启"分支**（`CSpecialAttInfoData:screenMonster` 开头
`if _mAttackData.switchIs == 0 then return end`：不筛名单、不发事件 ⇒ 面板不出现）。

**修前的兜底为什么更糟**（`[1,[[]]]` ⇒ `msg[1..14]` 全 nil ⇒ `switchIs = nil`）：
`nil == 0` 是**假** ⇒ 客户端**走进筛选分支**，而 `attacktype` 也是 nil ⇒ `sortStaticId/sortType/sortJob`
**一个都不执行** ⇒ `self.monsterIdData[_type]` 落成**全部妖怪**，再 `emit(EVENT.SPECIAL_ATTINFO, ...)`
把面板弹出来 —— 玩家看到的是「**所有妖怪都是特攻、加成文案全空**」。⇒ 兜底不是"没功能"，是**假规则**。

**将来要真的开启**：需要按活动类型给出那 9 个 GM 字段（`attacktype/attackid/damage/
attackadditiontype/attackadditionvalue/attackresult/monsterdescribe/interfaceid/additionReward*`），
把它们做成一张配置表或后台开关即可 —— 本模块只用改 `_ACTIVE_RULES` 一处（见下）。
"""
import json

from state import table_dir as _table_dir

_DATA = _table_dir()

# ★自定值（**没有表源，故为空**）：将来运营给出某个活动类型的特攻规则时，在这里登记
#   `activityType -> dict(...)`，`handle_specialatt` 会自动把 `switchIs` 置 1 并把 14 槽填满。
#   ⚠ 只登记**有明确来源**的值；不要为了"看起来像开了"而填默认的数字。
_ACTIVE_RULES = {}


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


def _out(*body):
    return ('[1,' + json.dumps(list(body), separators=(',', ':'), ensure_ascii=False)
            + ']').encode('utf-8')


def handle_specialatt(body_json=None):
    """12000 req `{activityType}` -> `[1,[type, switchIs, attacktype, attackid, damage,
    attackadditiontype, attackadditionvalue, attackresult, monsterdescribe, interfaceid,
    additionRewardTypes, additionRewardIds, additionRewardRates, additionMarketToken]]`。

    ★**14 槽一个都不能少**（见模块头：少一个就会让 `switchIs` 变 nil 并走进筛选分支 ⇒ 假规则面板）。
    ★`msg[1]` 把请求里的 `activityType` **原样回显**（客户端 `specialData.type`，注释写明是
      "gm所需字段" ⇒ 它自己不用，是要我们能对上号）。
    ★错误码只有 `[0]` 与 `[589] 活动时间结束`；本实现**任何输入都走成功路径**（开关关是合法状态，
      不是错误），所以不返回错误码 —— 回错误码会让 `NetSpecialAtt:recvMessage` 弹提示并**根本不发事件**，
      面板反而卡在没有数据的分支上。
    """
    req = _req(body_json)
    atype = _i(req.get('activityType'), 0)
    rule = _ACTIVE_RULES.get(atype)
    if not rule:
        # 未配置：14 槽结构完整 + 开关关（客户端自己那条"未开启"分支）。
        return _out(atype, 0, 0, '', '', 0, '', '', '', 0, '', '', '', '')
    # 已配置（自定值登记后才会走到这里）：逐槽按客户端注释的类型发。
    return _out(
        atype,
        1,
        _i(rule.get('attacktype'), 0),
        str(rule.get('attackid') or ''),
        str(rule.get('damage') or ''),
        _i(rule.get('attackadditiontype'), 0),
        str(rule.get('attackadditionvalue') or ''),
        str(rule.get('attackresult') or ''),
        str(rule.get('monsterdescribe') or ''),
        _i(rule.get('interfaceid'), 0),
        str(rule.get('additionRewardTypes') or ''),
        str(rule.get('additionRewardIds') or ''),
        str(rule.get('additionRewardRates') or ''),
        str(rule.get('additionMarketToken') or ''),
    )
