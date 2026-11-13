# -*- coding: utf-8 -*-
# 妖怪正传 私服 —— 静态响应数据常量 (config-driven response map)
# 本模块不 import 任何其它业务模块, 只存放消息响应字节串与 _mon 构造器。
#
# ★2026-10-01 (B 任务): 下发地址改为**可配置**(环境变量), 默认值与历史行为逐字节一致。
#   为什么改: 旧实现把 1005 的下发地址字面量写死成 127.0.0.1(DECISIONS 2026-09-30 记为
#   "改动 0 行"), 这在"同机自闭环"下正确, 但换端口它就撒谎、也挡死了局域网/公网多人。
#   为什么放本模块: 本模块**零 import**(见上), 常驻 import 闭包底层, 在这里读 env 不会有
#   循环依赖; game_server 侧的同名常量必须与这里取同一个 env(见 game_server.py 的 ADV_*)。
#
#   ⚠ 端口必须按 YGZZ_ADVERTISE_SOCK_PORT(下发) -> YGZZ_SOCK_PORT(监听) 的**同一优先级链**解析。
#     这里写错过一次(只读了 YGZZ_SOCK_PORT), 后果就是 NAT/端口映射场景下 1005 下发内部端口 ——
#     本模块的 `_b3_address_config_test.py` B4 用例正是为此而写, 它当场抓到并 FAIL。
import os

ADV_HOST = os.environ.get('YGZZ_ADVERTISE_HOST') or '127.0.0.1'
ADV_SOCK_PORT = int(os.environ.get('YGZZ_ADVERTISE_SOCK_PORT')
                    or os.environ.get('YGZZ_SOCK_PORT') or 27001)

# --- 30字段 怪物 item 构造器: [ucode,id,level,exp,islock,grow,uequiphp,uequipatk,uequipdef,addhp..addcri,
#   skill1-6,feature1-6,featureexp,featurelev,skin,isformation,monsternum]
#   (字段1 ucode 字符串; 字段7-9 uequip=装备背包ucode(已装备=字符串"1", 空槽=数字0); 字段14-19 skill=技能背包ucode(字符串))
def _mon(ucode, mid=100031, num=100, skin=11301, skills=None, uequip=(0,0,0), lv=1, exp=0,
         grow=0, addhp=0, addatk=0, adddef=0, addcri=0, feat=None, featexp=0, featlev=1, islock=None):
    if skills is None: skills=[0]*6
    if feat is None: feat=[0]*6
    if islock is None:
        # ★2026-09-12 (v26): 字段5 islock 由 MONSTER_LOCK(6001) 落库值驱动。
        #   旧实现恒写 '0' -> 客户端 SQMonster:getLock 永远读到 0 -> "锁定妖怪"点了不生效/图标不亮。
        try:
            from state import EQ_STATE as _ES
            islock = 1 if int((_ES.get('monster_lock') or {}).get(str(ucode), 0) or 0) else 0
        except Exception:
            islock = 0
    def ueq(v):
        if v == 0: return '"0"'          # 空槽 -> 字符串"0"(=UCODE_ZERO, 客户端判为无装备)
        return '"%s"' % v              # 已装备 -> 字符串(装备背包ucode)
    p=['"%d"'%ucode, str(mid), str(lv)]  # 1-3 ucode,id,level
    p+=['%d'%exp, str(int(islock)), str(grow)]   # 4-6 exp,islock,growvalue(成长值)
    p+=[ueq(uequip[0]),ueq(uequip[1]),ueq(uequip[2])]  # 7-9 uequiphp/atk/def
    p+=[str(addhp),str(addatk),str(adddef),str(addcri)]  # 10-13 命/攻/防/暴 加成
    # 技能字段(14-19) 真实技能输出字符串"11001"(=技能背包ucode), 空槽输出字符串"0"(=UCODE_ZERO, 客户端判为空技能槽)
    p+=[('"%s"' % s) if s != 0 else '"0"' for s in skills]
    # 20-25 feature1-6: 客户端 SQMonster:insertItem _info[20]=="" and 0 or _info[20]
    #   -> 空槽必须是【数字0】(不是字符串"0", 否则 `feature ~= 0` 恒真走 analysisSpeciality 解析失败路径)
    #   非空槽 = 字符串 "id#lv#isUse" (analysisSpeciality: tosplit(_feature,"&") -> tosplit(...,"#") [id,lv,isUse])
    p+=[('"%s"' % f) if isinstance(f, str) and f else '0' for f in feat]
    p+=[str(featexp), str(featlev)]      # 26-27 featureexp(特性总经验), featurelev(特性总等级0-6)
    p+=[str(skin),'0',str(num)]          # 28-30 skin,isformation,monsternum
    return '['+','.join(p)+']'

SDK_LOGIN_RESP_JSON = b'[1,["1","1","1","1",1]]'   # statecode=1, hasrole=1 -> 直接进游戏
SESSION_RESP_JSON   = b'[1,{}]'                      # SESSION(1008)/HEARTBEAT(1000) 响应: 只回调,内容随意
# GAME_SERVER(1005): msg[1]=[ip,port] -> Client_LoginToGame
#   ★B 任务: 由字面量 '127.0.0.1',27001 改为跟随 ADV_HOST/ADV_SOCK_PORT。
#   旧写法的问题是"端口撒谎": 改 SOCK_PORT 起服时, 这里仍下发 27001 -> 客户端连到无人监听的端口。
GAME_SERVER_RESP_JSON = ('[1,[["%s",%d]]]' % (ADV_HOST, ADV_SOCK_PORT)).encode('utf-8')
# ROLE_INFO(1004): roleInfo = msg[1] = [account,rolename,goldcoin,soulcoin,vitality,checkpoint,newtowernum,contacts,
#   level,exp,eggcoin,guide,timerid,integral,dangrading,state,physicallimit,expbuff,coinbuff,invasionstate,
#   legendsstate,achievepoint,roleId,img,imgframe,WeekLastTime,MonthLastTime,rankIntegral,exploreRoleID,hasGuideTask]
ROLE_INFO_RESP_JSON = ('[1,[["1","测试",100000,0,20,1,1,400,1,0,0,"1",0,0,0,0,20,'
                       '"0","0","0","0",0,1,0,0,0,0,0,1010,false]]]').encode('utf-8')
# --- belfast-style config-driven response map: msgid -> response json payload `[1,...]` ---
# Fill each message's data per the Lua handler schema (lua_dump\*.lua NetXxx:recvMessage).
# monster item (SQMonster:insertItem, 30 fields): [ucode,id,level,exp,islock,growvalue,uequiphp,uequipatk,uequipdef,
#   addhp,addatk,adddef,addcri, skill1..6, feature1..6, featureexp,featurelev,skin,isformation,monsternum]
# 怪物背包: 3只真实收集怪物; 技能字段=技能背包ucode(字符串); 三怪各配 HP/ATK/DEF 三槽装备
#   装备ucode: 哪吒HP="4"/ATK="1"(桃木剑)/DEF="5"; 雷震子HP="6"/ATK="7"/DEF="8"; 黄九郎HP="9"/ATK="10"/DEF="11"
MONSTER_BAG_JSON = (b'[1,[['
    + _mon(1,100031,skin=11301,skills=[11001,11006,11011,0,0,0],uequip=("4","1","5")).encode()
    + b',' + _mon(2,200101,skin=20011,skills=[10236,10226,10061,0,0,0],uequip=("6","7","8")).encode()
    + b',' + _mon(3,201001,skin=20101,skills=[10356,10361,10366,0,0,0],uequip=("9","10","11")).encode()
    + b'],"1/1"]]')
# FORMATION_LIST(3200): msg=[formation,loadFormation]; formation=[[ucode,posx,posy,skin]],loadFormation=怪物item数组
FORMATION_LIST_JSON = (b'[1,[['
    b'["1",1,1,0],["2",1,2,0],["3",1,3,0]],'
    b'[' + _mon(1,100031,skin=11301,skills=[11001,11006,11011,0,0,0],uequip=("4","1","5")).encode()
    + b',' + _mon(2,200101,skin=20011,skills=[10236,10226,10061,0,0,0],uequip=("6","7","8")).encode()
    + b',' + _mon(3,201001,skin=20101,skills=[10356,10361,10366,0,0,0],uequip=("9","10","11")).encode() + b']]]')
# DRAW_CARD(8002): msg=[cardtype,totalticket,goblineggs,totaldemonsoul,luckvalue,remaintime,remaindaycount,
#   cardlist(怪物item数组),talentList,count(>0背包满),bNextPage]  (lua_dump\34.lua)
DRAW_CARD_JSON = b'[1,[1,0,0,100000,0,0,0,[' + _mon(1,100031,skin=11301,skills=[11001,11006,11011,0,0,0]).encode() + b'],[],0,false]]'
# CARD_TIME(8001): msg=[costdemonsoul,costticket,remaintime,totalticket,goblineggs,totaldemonsoul,
#   luckvalue,remaindaycount,cardpool,summonFiveEnsure]  (NetCardTime:recvMessage, lua_dump\35.lua)
CARD_TIME_JSON = b'[1,[100,1,-1,0,0,100000,0,0,[],0]]'
# --- 邮件推送(1059 MAIL_TOTAL): msg=[ [id,title,time,emailtype,state],... ]  (lua_dump\128.lua) ---
#   emailtype: 0通知 1奖励 ; state: 0未读 1已读
MAIL_TOTAL_JSON = b'[1,[[1,"\xe6\xac\xa2\xe8\xbf\x8e\xe6\x9d\xa5\xe5\x88\xb0\xe5\xa6\x96\xe6\x80\xaa\xe4\xb8\x96\xe7\x95\x8c","20260824000000",1,0]]]'
# 新手手札(103 PUSH_LETTER 请求 -> 服务器【主动推】1046 LETTER_REV): msg[1] = [id,state] 对列表
#   源码核实: 客户端 NetLetterRev:sendMessage 无任何调用方, 列表只能由服务器推。
#   NetLetterRev:recvMessage 源码 (client Lua\app\network\guide\NetLetterRev.lua):
#     local list = msg[1]; for i,v in ipairs(list) do data.id=tonumber(v[1]); data.state=tonumber(v[2]) end
#   -> msg 必须是 [ [ [id,state],... ] ] (即 msg[1] = 对列表) — 不是"CJson2Table 解包", 是协议本身多一层。
#   id 来自 UG_03_UGP(新手手札静态表): 101=队伍系统 UnlockLv=1 -> 玩家1级时 getLetterState()==1 -> 弹新手手札,
#   且 next(getLetterList()) 非空 -> TipsManager:checkLetter 不再发 103 -> 止住死循环
#   state: 0=未读 1=已读
#   真机实证 (2026-09-06): [1,[[101,0]]] 失败 (客户端对 101 取 v[1]=nil 报错, 列表恒空, 103 风暴不止);
#      [1,[[[101,0]]]] 实测成功 n=1; 多对 [1,[[[101,0],[102,1]]]] = n=2 ✓
LETTER_REV_JSON = b'[1,[[[101,0]]]]'
# 游戏主界面客户端登录/加载批量请求的消息（从 game_server.log 观测到的 msgid）
# 1184 SWITCH_ROLE_INFO(探索形象): msg=[ [id,state,time],... ] (lua_dump\306.lua)
# 3002 GET_VITALITY: msg[1]=roleInfo(30字段,同 ROLE_INFO) -> gamer_setRoleInfo (lua_dump\212.lua)
# 7013 ACTIVITY_LIST: msg=[ [ID,begin,over,countdown,finish,[[cid,group,valve,state]]],... ] (lua_dump\118.lua)
RESPONSES = {
    1007: SDK_LOGIN_RESP_JSON,                # SDK_LOGIN
    1005: GAME_SERVER_RESP_JSON,              # GAME_SERVER -> [1,[[ip,port]]]
    1004: ROLE_INFO_RESP_JSON,                # ROLE_INFO
    1008: SESSION_RESP_JSON,                  # SESSION
    1002: b'[1,["\xe6\xb5\x8b\xe8\xaf\x95"]]',   # ROLE_CREATE -> [1,[rolename]] (NetRoleCreate: msg[1]=rolename)
    1003: SDK_LOGIN_RESP_JSON,                # LOGIN -> [1,[seqid,account,userpwd,macid,hasrole]] (NetLogin msg[1..5])
    1009: b'[1,["\xe6\xb5\x8b\xe8\xaf\x95\xe8\xa7\x92\xe8\x89\xb2"]]',  # ROLE_NAME -> [1,[rolename]] (NetRoleName)
    103: b'[1,{}]',                            # PUSH_LETTER 确认 (NetPushLetter:recvMessage 只打日志; 列表由服务器推 1046)
    1046: LETTER_REV_JSON,                     # LETTER_REV 手札列表(服务器主动推)
    1047: b'[1,{}]',                            # LETTER_UPDATE 手札更新确认
    # page-based bags: [1, [data, "cur/total"]]
    # item field schemas (clean_lua2\sqdata_SQ*.lua):
    #   EQUIP item=[ucode,id,level,exp,a1,a2,islock,isload,0,0,0,0,a3,a4] (id->L01_Equipments id=1001)
    #   PROP  item=[ucode,id,count] (id->L02_Loots id=1001)
    #   SKILL item=[ucode,id,level,isload,isopen,ischange] (id->L93_Skills id=10001)
    1030: (b'[1,[[["1",1001,1,0,0,0,0,1,0,0,0,0,0,0],["2",1002,1,0,0,0,0,0,0,0,0,0,0,0],'
           b'["3",1003,1,0,0,0,0,0,0,0,0,0,0,0],["4",1006,1,0,0,0,0,1,0,0,0,0,0,0],'
           b'["5",1004,1,0,0,0,0,1,0,0,0,0,0,0],["6",1006,1,0,0,0,0,1,0,0,0,0,0,0],'
           b'["7",1002,1,0,0,0,0,1,0,0,0,0,0,0],["8",1004,1,0,0,0,0,1,0,0,0,0,0,0],'
           b'["9",1006,1,0,0,0,0,1,0,0,0,0,0,0],["10",1003,1,0,0,0,0,1,0,0,0,0,0,0],'
           b'["11",1004,1,0,0,0,0,1,0,0,0,0,0,0]],"1/1"]]'),   # EQUIP_BAG: 三怪各HP/ATK/DEF + 2备用ATK
    1031: b'[1,[[],"1/1"]]',                  # GHOST_BAG
    1032: MONSTER_BAG_JSON,                   # MONSTER_BAG (3只真实怪物)
    1033: b'[1,[[[1,1001,5]],"1/1"]]',         # PROP_BAG (1个道具 x5)
    1506: (b'[1,[[["11001",11001,1,0,1,0],["11006",11006,1,0,1,0],["11011",11011,1,0,1,0],'
           b'["10236",10236,1,0,1,0],["10226",10226,1,0,1,0],["10061",10061,1,0,1,0],'
           b'["10356",10356,1,0,1,0],["10361",10361,1,0,1,0],["10366",10366,1,0,1,0]],"1/1"]]'),  # SKILL_BAG: 3怪技能
    1507: b'[1,[[],"1/1"]]',                  # SKILL_RECV
    # SHOP_TOTAL(5001): msg=[page,time,goodsInfo]; 商品项=[id,page,goodstype,[[type,id,count]],goodscount,
    #   limittype,limitcoun,pricetype,price,originalprice,image,tittle,desc,limittime,isbuy,isdouble]
    #   真实page=8限定商品(每日惊喜礼包 id=800) 见 G02_Shop.json
    5001: b'[1,[8,0,[[800,8,10,[[10,8000,1]],1,4,1,2,15,15,"Store_FreePack",'
          b'"\xe6\xaf\x8f\xe6\x97\xa5\xe6\x83\x8a\xe5\x96\x9c\xe7\xa4\xbc\xe5\x8c\x85",'
          b'"\xe5\xb1\xb1\xe5\xa6\x96\xe6\x83\x8a\xe5\x96\x9c\xe5\xae\x9d\xe7\xae\xb1x1\xe3\x80\x81\xe4\xbd\x93\xe5\x8a\x9bx5",'
          b'0,0,0]]]]',
    # --- 队伍/召唤(p10 优先) ---
    3200: FORMATION_LIST_JSON,              # FORMATION_LIST (队伍: 3只怪物上阵)
    8002: DRAW_CARD_JSON,                   # DRAW_CARD (召唤结果: 抽到1只鬼魂)
    # --- 进游戏后客户端批量请求的消息(从 game_server.log 观测) ---
    1184: b'[1,[]]',                          # SWITCH_ROLE_INFO (探索形象列表)
    3002: ROLE_INFO_RESP_JSON,                # GET_VITALITY -> roleInfo(30字段)
    # ACTIVITY_LIST(7013): msg=[ [ID,begin,over,countdown,finish,[[cid,group,finishvalve,state]]],... ]
    #   (lua_dump\118.lua) 真实活动ID/title 见 A01_Activity.json; i=1 为7日累计登陆
    7013: b'[1,[[101,"20180810000001","20291231235959",0,1,[]],'
          b'[103,"20180810000001","20291231235959",0,1,[]],'
          b'[104,"20180810000001","20291231235959",0,1,[]]]]',  # 3个真实活动
    8001: CARD_TIME_JSON,                     # CARD_TIME (进入抽卡界面)
    # --- 全新登录后客户端请求的消息(p3 观测) ---
    # ACTIVITY_ANNOUNCE(1099): msg=[point,dangrading,ysranking,monsterranking,defenseranking,
    #   mapimg,detailtype(1跳id/2网址/3不配置),detail]; 排名项=[rank,img,frame,rolename,point/staticid/score]
    #   (lua_dump\221.lua) 真实活动ID见 A01_Activity.json
    1099: b'[1,[0,1001,[[1,0,0,"\xe7\x81\xb0\xe8\xa1\x8c\xe5\xb0\x8f\xe9\x81\x93",1000],'
          b'[2,0,0,"\xe6\xb5\x8b\xe8\xaf\x95",800]],'
          b'[[1,0,0,"\xe9\xac\xbc\xe9\xad\x82",1000001]],'
          b'[[1,0,0,"\xe6\xa1\x83\xe6\x9c\xa8\xe5\x89\x91",999]],"",3,""]]',
    #   ★2026-10-02 (审计 §12.1 逐字段复核, 本轮唯一改这条的地方): 第 6 个字段 `mapimg` 原为 **0**,
    #     而客户端 `NetAnnounce.lua:74-76` 只挡 nil 与 `""` —— **0 在 Lua 里为真** ⇒ 会拿 0 去
    #     `setTexture(0)`(不存在的贴图 id)。改成 `""` 走"没有图"的安全分支。
    #     **这是本条里唯一实测成立的缺陷**; 排名数组形状已与客户端逐下标对齐
    #     (`game_server.log:209` 的帧长 168 与 123B 应答吻合)。
    #   ⚠ 排名内容(灰行小道/测试/鬼髅/桃木剑)是**私服编的假数据**, 违反"没有真实数据源必须返回 0"。
    #     1099 的排名项是"活动公告榜", 目前**没有真实数据源可接**, 且整段清空会让公告位变空白。
    #     本轮只修 mapimg; 假榜单作为**已登记欠债**写进 HANDOFF「未完成」, 不在这里偷偷抹掉。
    # OPERATION_ACTIVITY_LIST(1108): msg=[ [ID,Activityype,Title,Starttime,Enttime,Priority] ]
    #   (lua_dump\223.lua) 真实活动见 A01_Activity.json
    1108: b'[1,[[101,0,"\xe7\xb4\xaf\xe8\xae\xa1\xe7\x99\xbb\xe9\x99\x86\xe5\xa5\x96\xe5\x8a\xb1",'
          b'"20180810000001","20291231235959",1],'
          b'[103,0,"\xe5\x8f\xac\xe5\x94\xa4\xe5\xbe\x97\xe5\xa5\x96\xe5\x8a\xb1",'
          b'"20180810000001","20291231235959",2]]]',  # 2个运营活动(累计登陆/召唤得奖)
}

def resp_for(msgid):
    # 未注册 msgid 的兜底响应。
    #   ★2026-09-12 修复 (用户反馈 §1 "界面卡死"): 旧兜底 b'[1,{}]' 会让客户端
    #   `msg = recvMsg[2] = {}` -> `msg[1] = nil`; 而所有 NetXxx:recvMessage 都按
    #   `pairs(msg[1])` / `msg[1].field` 取值 -> 直接抛 Lua 错误 ->
    #   SocketManager.lua:497 `m_messageMap[id]:recvMessage()` 中断整条 socket tick ->
    #   界面卡住不动 (实测 NetGetLimitedTimeTask/NetInvadeRewards/NetInvade 三处)。
    #   改成 b'[1,[[]]]' 后 msg[1] 恒为一个 table, `pairs/`#`/字段索引都不会炸。
    return RESPONSES.get(msgid, b'[1,[[]]]')
