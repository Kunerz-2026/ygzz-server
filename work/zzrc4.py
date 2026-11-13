# -*- coding: utf-8 -*-
"""zzrc4.py —— 客户端 `app/zzrc/ZZRc4.lua` 的 Python 实现（服务端用来读加密字段）。

【为什么需要它】
  多份结算/奖励协议把关键参数加密后才发上来，例：
    app/network/war/NetWarResult.lua:41   _dropID = ZZRc4.encodeEncrypt(tostring(_dropID), timeStamp)
                                          msg["value"] = _dropID     -- 官方掉落组 id
                                          msg["time"]  = timeStamp
    app/network/lengend/NetLengendReward.lua:54  sign = ZZRc4.encodeEncrypt(CTable2Json(sign), msg["time"])
  服务端此前**没有这个实现**，只能"宽松解析"（handlers_activity 里那句
  "加密串按 0 计" 就是它），于是这些字段里装的东西（官方掉落组 id！）全被丢弃。

【算法（逐行取自 1.31 装机明文，可复核）】
  app/zzrc/ZZRc4.lua:1     const_key = "eed9d3744e144361"
  app/zzrc/ZZRc4.lua:115   function ZZRc4.encodeEncrypt(text,timeStamp)
  app/zzrc/ZZRc4.lua:117-118   key = const_key .. tostring(timeStamp)
  app/zzrc/ZZRc4.lua:8-58      ZZRc4.encrypt = 教科书 RC4 (KSA / PRGA / 逐字节 XOR)
  app/zzrc/ZZRc4.lua:121       encodeBase64(密文)
  app/zzrc/ZZRc4.lua:127-135   decodeDecrypt = base64 解码 + 同一个 encrypt（RC4 是对合运算）
  app/tools/Utilities.lua:1455   b64chars = 'A-Za-z0-9+/'（标准字母表，'=' 填充）
  app/zzrc/ZZMathBit.lua:45-47   xorOp = 逐位异或（手写实现，非负数语义等价于 ^）

  ⇒ 对称：encrypt == decrypt。KSA/PRGA 与标准 RC4 逐行一致，所以可拿公开测试向量验证。

【用法】
  from zzrc4 import zz_decrypt, zz_decrypt_int
  gid = zz_decrypt_int(req['value'], req['time'])      # -> 官方掉落组 id 或 None
"""

import base64

# app/zzrc/ZZRc4.lua:1
CONST_KEY = 'eed9d3744e144361'


def rc4(data, key):
    """标准 RC4（KSA + PRGA + XOR）。data/key 都是 bytes。"""
    if isinstance(data, str):
        data = data.encode('utf-8')
    if isinstance(key, str):
        key = key.encode('utf-8')
    S = list(range(256))
    kl = len(key)
    j = 0
    for i in range(256):
        j = (j + S[i] + key[i % kl]) & 0xFF
        S[i], S[j] = S[j], S[i]
    out = bytearray(len(data))
    i = j = 0
    for n in range(len(data)):
        i = (i + 1) & 0xFF
        j = (j + S[i]) & 0xFF
        S[i], S[j] = S[j], S[i]
        out[n] = data[n] ^ S[(S[i] + S[j]) & 0xFF]
    return bytes(out)


def zz_key(time_stamp):
    """key = const_key .. tostring(timeStamp)  (ZZRc4.lua:117-118)"""
    return (CONST_KEY + str(time_stamp)).encode('ascii')


def zz_encrypt(plain, time_stamp):
    """明文 -> base64(RC4(明文))；与客户端 encodeEncrypt 等价。"""
    if not isinstance(plain, (bytes, bytearray)):
        plain = str(plain).encode('utf-8')
    return base64.b64encode(rc4(bytes(plain), zz_key(time_stamp))).decode('ascii')


def zz_decrypt(b64_text, time_stamp):
    """base64(RC4(x)) -> 原文。解不出来返回 None（对齐 Lua decodeBase64 的 `return` 即 nil）。

    ⚠ 客户端 decodeBase64 遇到非法字符会 `return`（nil），所以这里也**不抛异常**，
      由调用方决定"解不出来就当没有奖励"，绝不让一个坏字段把整条结算打崩。
    """
    if b64_text is None:
        return None
    if isinstance(b64_text, (bytes, bytearray)):
        try:
            b64_text = bytes(b64_text).decode('ascii')
        except Exception:
            return None
    s = str(b64_text).strip()
    if not s:
        return None
    try:
        raw = base64.b64decode(s, validate=False)
    except Exception:
        return None
    if not raw:
        return None
    try:
        return rc4(raw, zz_key(time_stamp)).decode('utf-8')
    except Exception:
        return rc4(raw, zz_key(time_stamp)).decode('utf-8', 'replace')


def zz_decrypt_int(b64_text, time_stamp, lo=0, hi=10 ** 12):
    """解出一个整数 id；解不出 / 越界 -> None（调用方据此走"不发奖"分支）。"""
    txt = zz_decrypt(b64_text, time_stamp)
    if txt is None:
        return None
    txt = txt.strip().strip('"').strip("'")
    if not txt.isdigit():
        return None
    v = int(txt)
    if v < lo or v > hi:
        return None
    return v


if __name__ == '__main__':
    import sys
    # 用法: python zzrc4.py <base64> <time>   -> 打印原文
    if len(sys.argv) >= 3:
        print(zz_decrypt(sys.argv[1], sys.argv[2]))
    else:
        print('rc4 self-check:',
              rc4(b'Plaintext', b'Key').hex().upper())   # 期望 BBF316E8D940AF0AD3
        print('zz round-trip :', zz_decrypt(zz_encrypt('500141', 1757868000), 1757868000))
