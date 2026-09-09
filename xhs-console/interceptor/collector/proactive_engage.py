#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小红书 主动留言+截流(全自动 · 可配置目标 · 风控内建)        2026-09-05
================================================================
用途: 去【别人的笔记】下主动留言, 把目标人群截流进私信。
      与 auto_reply.py(被动回私信) 互补: 这是主动出击获取新线索。

链路(真机 alioth 1080×2400 实测锚点):
  亮屏解锁 → 启动小红书(首页 IndexActivityV2)
  → 点顶部『搜索』(Button desc=搜索 [956,91][1055,190])
  → GlobalSearchActivity: 点输入框(EditText '搜索,' [209,96][811,184])
      输入关键词 → 点『搜索』按钮(Button text=搜索 [954,80][1036,201])
  → 搜索结果卡片: 正文 y~1177, 作者昵称 y~1302 click=true
  → 点笔记卡片进详情(NoteDetailActivity) → 点『说点什么...』评论区
  → 输入公开留言 → 点『发送』

红线(触雷即停, 绝不越界):
  * 评论区公开留言=只种草/提问/给价值, 绝不导流/卖课/留联系方式/报价。
  * 引流动作(加微/导流/谈课程)一律在【私信/对方回应之后】, 不在公开区。
  * 默认 dry-run: 只生成并停在输入框, 不点发送; --send --confirm 才真发。
  * 频控: 单日上限/间隔/每篇1条/目标穿插(读 proactive_targets.yaml global)。

设备: 读 XHS_DEVICE(同采集器)。
用法:
  python proactive_engage.py                      # 干跑: 搜→选→生成→进输入框(不发送)
  python proactive_engage.py --send               # 真发(需 --confirm)
  python proactive_engage.py --send --confirm     # 确认真发
  python proactive_engage.py --kw "研学带团" --target yanxue_tonghang
"""
from __future__ import annotations

import argparse, os, re, sys, time, json, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CLS = os.path.join(ROOT, "classify")
OUT = os.path.join(ROOT, "out")
PROACTIVE_LOG = os.path.join(OUT, "proactive_send_log.json")   # 主动留言频控日志
SHOT_DIR = os.path.join(OUT, "proactive_shots")                # 真发留痕截图目录
PKG = "com.xingin.xhs"

# 真机锚点(alioth 1080×2400) —— 主要用文本/desc 动态定位, 这里是兜底坐标
SEARCH_BTN = (1005, 140)        # 首页顶部右侧放大镜
SEARCH_INPUT = (500, 140)       # 搜索页输入框
SEARCH_GO = (995, 140)          # 搜索页『搜索』按钮

# 评论区入口兜底坐标(点『说点什么...』)
COMMENT_BOX_Y = 2250


# ---------------------------------------------------------------- 基础(复用 auto_reply)
def conn():
    import uiautomator2 as u2
    serial = os.environ.get("XHS_DEVICE", "")
    return u2.connect(serial) if serial else u2.connect()


def _own_nick():
    """当前操作设备(手机)的自家账号昵称。用于过滤「不要评论自己的笔记」。
    读 console/lanes.yaml accounts 按 XHS_DEVICE 映射; 默认设备→默认账号。
    ★ 关键(2026-09-07): 实测 YOUR_DEVICE_SERIAL 昵称=「你的账号A」, 搜索结果全是它发的研学笔记,
      导致脚本一直试图给自己留言。必须过滤自家账号, 否则永远撞同一作者、也违背"去别人笔记下留言"。"""
    try:
        import yaml as _y
        dev = os.environ.get("XHS_DEVICE", "") or "默认设备"
        lanes = _y.safe_load(open(os.path.join(ROOT, "console", "lanes.yaml"), encoding="utf-8")) or {}
        acc = (lanes.get("accounts") or {}).get(dev) or {}
        return (acc.get("nick") or "").strip()
    except Exception:
        return ""


def _is_own(author):
    """作者昵称是否=自家账号昵称(真人的昵称/账号名)。是→返回 True, 需过滤。"""
    if not author:
        return False
    nick = _own_nick()
    if not nick:
        return False
    return author.strip() == nick.strip()


def _dump(d):
    import xml.etree.ElementTree as ET
    try:
        return ET.fromstring(d.dump_hierarchy())
    except Exception:
        return None


def _nodes(d):
    """把当前 dump 转成 dict 列表(带 text/desc/bounds/clickable/class)。"""
    root = _dump(d)
    if root is None:
        return []
    import re
    out = []
    for n in root.iter("node"):
        a = n.attrib
        m = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", a.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = (int(v) for v in m.groups())
        out.append({"text": (a.get("text") or "").strip(),
                    "desc": (a.get("content-desc") or "").strip(),
                    "b": (x1, y1, x2, y2),
                    "click": a.get("clickable") == "true",
                    "cls": a.get("class", "").split(".")[-1]})
    return out


def _f(n): return (n["text"] or n["desc"]).strip()


def _center(n):
    x1, y1, x2, y2 = n["b"]
    return ((x1 + x2) // 2, (y1 + y2) // 2)


def _find(nodes, pred):
    for n in nodes:
        if pred(n):
            return n
    return None


def _find_all(nodes, pred):
    return [n for n in nodes if pred(n)]


def _click_center(d, n):
    cx, cy = _center(n)
    d.click(cx, cy)


def wake_and_open(d, timeout=16):
    """亮屏+解锁→【强制冷启动小红书】→等首页稳定。真正幂等。
    关键升级: 每次运行都 app_stop + app_start 清场。否则上次停在 NoteDetail/搜索页,
    go_search 会误判已在搜索页, collect_notes 读到脏数据 → 空结果。
    流程: screen_on → 解锁(锁屏上滑) → app_stop → app_start → 轮询至首页稳定。"""
    import time
    try:
        d.screen_on()
    except Exception:
        pass
    time.sleep(0.6)
    # 解锁: 只识别锁屏特征(节点稀少/锁屏文案), 不在已解锁状态多滑
    def _locked():
        n2 = _nodes(d)
        if len(n2) < 30:
            return True
        return any(("锁屏" in _f(n) or "已充满电" in _f(n)
                    or n["cls"] in ("BatteryService", "LockScreenView"))
                   for n in n2)
    if _locked():
        try:
            d.swipe(540, 1800, 540, 620, duration=0.4)
        except TypeError:
            # 兼容不同 uiautomator2 版本(某些用 direction, 某些数值间距)
            d.swipe(540, 1800, 540, 620)
        time.sleep(1.0)
    # 冷启动清场: 无论如何都杀掉再拉起, 保证从首页开始
    try:
        d.app_stop(PKG)
    except Exception:
        pass
    time.sleep(0.8)
    d.app_start(PKG, use_monkey=True)
    # 轮询直到出现首页特征(节点够多 / activity 是 Index)
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(1.2)
        act = d.app_current().get("activity") or ""
        if "Index" in act and len(_nodes(d)) >= 60:
            return d
        if len(_nodes(d)) >= 90:
            return d
    return d


def go_search(d):
    """首页→点顶部『搜索』→ 进入搜索页(GlobalSearchActivity)。
    用 activity 名判断是否已在搜索页(搜索结果页可能无 EditText, 不能只靠它)。"""
    for _ in range(5):
        act = d.app_current().get("activity") or ""
        nodes = _nodes(d)
        # 已在搜索页(activity 含 Search/GlobalSearch)
        if "GlobalSearch" in act or "Search" in act:
            # 若没有输入框但已有搜索结果(节点多), 也算已在搜索页
            if _find(nodes, lambda n: n["cls"] == "EditText") or len(nodes) >= 100:
                return True
        # 首页: 点顶部右侧放大镜搜索按钮(desc=搜索, y<300)
        sb = _find(nodes, lambda n: (n["desc"] == "搜索" or n["desc"] == "搜索按钮"
                                     or "search" in n["desc"].lower()) and n["b"][1] < 300)
        if sb is None:
            sb = _find(nodes, lambda n: n["click"] and n["b"][1] < 220 and n["b"][0] > 900)
        if sb:
            _click_center(d, sb)
            time.sleep(2.0)
            continue
        if "IndexActivityV2" in act:
            d.click(*SEARCH_BTN)
            time.sleep(2.0)
    act = d.app_current().get("activity") or ""
    return ("GlobalSearch" in act or "Search" in act)


def input_search(d, kw):
    """在搜索页输入关键词并点搜索。返回 True=已发起搜索。
    兼容结果页/输入态: 先点输入框(EditText 或顶部 y<200 的文本区), 清空后输入。"""
    import time
    # 1) 找输入框(EditText 或 y<200 的输入区)
    nodes = _nodes(d)
    et = _find(nodes, lambda n: n["cls"].endswith("EditText"))
    if et is None:
        # 找顶部输入区: y<200 且可点(放大镜旁)
        et = _find(nodes, lambda n: n["click"] and n["b"][1] < 220 and n["b"][0] > 150)
    if et is None:
        print("  [warn] 搜索页未找到输入框")
        return False
    _click_center(d, et)
    time.sleep(0.8)
    # 2) 清空已有输入
    try:
        nodes2 = _nodes(d)
        et2 = _find(nodes2, lambda n: n["cls"].endswith("EditText"))
        et_idx = [i for i, n in enumerate(nodes2) if n["cls"].endswith("EditText")]
        if et_idx:
            el = d(className="android.widget.EditText")[0]
            cur = el.info.get("text", "")
            if cur and cur != kw:
                el.set_text(""); time.sleep(0.5)
    except Exception:
        pass
    # 3) 输入关键词
    d.send_keys(kw)
    time.sleep(1.0)
    # 4) 点『搜索』按钮(desc=搜索 / text=搜索, y<300)
    go = _find(_nodes(d), lambda n: (n["desc"] == "搜索" or n["text"] == "搜索")
               and n["b"][1] < 300)
    if go:
        _click_center(d, go)
    else:
        d.click(*SEARCH_GO)
    time.sleep(2.5)
    return True


def collect_notes(d):
    """从搜索结果页收集笔记卡片: [(title, author_nick, author_y)]。
    结果结构: 标题正文 TextView(y≈565) + 作者昵称 click=true(y≈690, 作者在标题下方~125px)。"""
    import re
    nodes = _nodes(d)
    # 所有作者昵称: click=true 且含中文 且长度适中(昵称) 且 y 在内容区
    # ★ 放宽(2026-09-07): y<2300 误杀底部真实卡片(汉堡小生菜 Y=2314), 改 <2360。
    authors = [n for n in nodes if n["click"] and re.search(r'[\u4e00-\u9fa5]', _f(n))
               and 1 <= len(_f(n)) <= 12 and n["b"][1] > 300 and n["b"][1] < 2360
               and n["cls"] == "TextView"]
    notes = []
    for a in authors:
        ay = a["b"][1]
        title = ""
        best_d = 10**9
        # 向上找最近的正文标题(TextView, 中文, 长度>6, 在作者上方 60~160px)
        for n in nodes:
            if n["cls"] != "TextView":
                continue
            f = _f(n)
            if len(f) >= 6 and re.search(r'[\u4e00-\u9fa5]', f):
                d = ay - n["b"][1]
                if 60 <= d <= 170 and d < best_d:
                    # 标题通常在一行: 取该 y 附近最长中文节点
                    best_d = d
                    title = f
        notes.append({"title": title or "", "author": _f(a), "y": ay})
    # 去重(作者+标题)
    seen, uniq = set(), []
    for nt in notes:
        k = (nt["author"], nt["title"][:18])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(nt)
    # 过滤广告(y>2360 的不收, 已有 '广告' 标记的跳过)
    # ★ 放宽(2026-09-07): 原 <2100 误杀底部真实卡片(实测 汉堡小生菜 Y=2314 被拦),
    #   导致命中 0 条。放宽到 <2360(覆盖第二张卡), 广告多出现在 y>2360。
    uniq = [nt for nt in uniq if nt["y"] < 2360]
    return uniq


def _current_note_author(d):
    """读当前笔记详情的作者昵称(顶部作者名, 通常是正文区域上方的 click=true 昵称)。
    用于校验 open_note 打开的是不是目标作者, 防运行时串页(误入自家/其他笔记)。"""
    nodes = _nodes(d)
    # 作者昵称: TextView, 含中文, 长度1-14, 在 y<1000 的正文头部区
    for n in nodes:
        if n["cls"] == "TextView" and _f(n) and re.search(r'[\u4e00-\u9fa5]', _f(n)):
            y1 = n["b"][1]
            if 100 < y1 < 1000 and len(_f(n)) <= 14:
                f = _f(n)
                # 排除标题/正文长句(作者名通常较短)
                if len(f) <= 12 and not any(k in f for k in ("研学", "导游", "攻略", "教程", "快来", "关注", "赞", "收藏", "说点什么")):
                    return f
    return ""


def open_note(d, note):
    """点某条笔记的正文标题区进入详情。返回 True=已进入目标笔记详情页。
    ★ 加固(2026-09-07): ①判定收紧为 activity 含 'NoteDetail'; ②进入后校验当前笔记作者==目标作者,
      若串页到其他/自家笔记则返回 False, 绝不误发。③优先按标题文本点, 兜底才用坐标(坐标易串页)。"""
    # 标题正文区在上方(~40-150px 处), 点正文中心
    ay = note["y"]
    # ① 优先按标题文本匹配点正文(比裸坐标稳, 防搜索结果刷新后坐标错位)
    title = note.get("title", "")
    clicked = False
    if title:
        tn = _find(_nodes(d), lambda n: n["cls"] == "TextView" and _f(n) and title[:12] in _f(n))
        if tn is None:
            # 放宽: 标题含日期/长度时, 用作者名上方最近的正文
            tn = _find(_nodes(d), lambda n: n["cls"] == "TextView"
                       and _f(n) and note.get("author") and abs(n["b"][1] - ay) < 200)
        if tn:
            _click_center(d, tn)
            time.sleep(2.2)
            clicked = True
    # ② 若标题没点中, 兜底点作者名上方(坐标)
    if not clicked:
        d.click(540, note["y"] - 120)
        time.sleep(2.2)
    # ③ 校验: 进入详情且作者==目标作者(防串页)
    act = d.app_current().get("activity") or ""
    if "NoteDetail" not in act:
        return False
    cur_auth = _current_note_author(d)
    want = note.get("author", "")
    # 若读到的作者非空且与目标明显不同(非包含关系), 视为串页
    if cur_auth and want and cur_auth not in want and want not in cur_auth:
        print(f"  [串页拦截] 打开的是『{cur_auth}』而非『{want}』, 判失败")
        return False
    return True


def tap_comment_box(d):
    """进入笔记详情后, 点『说点什么...』评论区入口, 弹出评论输入层。返回 True=已点开。
    ★ 加固(2026-09-07): 兜底点击前必须确认当前确实是笔记详情页(activity 含 Detail)。
      此前兜底 d.click(320,2280) 会在误入【企业号会话页】时盲点企业号输入框,
      导致 input_and_send 往企业会话里发消息(雷区)。现在: 非笔记详情页一律返回 False, 不盲点。"""
    import time
    # ① 硬校验: 当前必须是笔记详情页, 否则直接判失败(不盲点)
    act = (d.app_current().get("activity") or "")
    if not ("NoteDetail" in act or "Detail" in act):
        return False
    for _ in range(3):
        nodes = _nodes(d)
        # 优先点『说点什么...』入口
        c = _find(nodes, lambda n: ("说点什么" in _f(n) or "评论" in _f(n))
                  and n["click"] and n["b"][1] > 1800)
        if c:
            _click_center(d, c)
            time.sleep(1.2)
            # 弹出评论层后应出现 EditText 输入框
            if _find(_nodes(d), lambda n: n["cls"].endswith("EditText")):
                return True
            continue
        # 兜底: 只在仍然是笔记详情页时, 点底部评论区输入区(避开企业号输入框)
        if "NoteDetail" in (d.app_current().get("activity") or "") \
           or "Detail" in (d.app_current().get("activity") or ""):
            d.click(320, 2280)
            time.sleep(1.2)
            if _find(_nodes(d), lambda n: n["cls"].endswith("EditText")):
                return True
        else:
            return False  # 页面已不再是笔记详情(跳到企业会话页等), 立即停, 不盲点
    return _find(_nodes(d), lambda n: n["cls"].endswith("EditText")) is not None


def input_and_send(d, text, send=False, author=""):
    """在评论区输入框输入留言, 可点发送。返回三态字符串:
      'sent'  = 确实发出去了(检测到发布成功标志)        → 记 send(已真发)
      'unsure'= 点了发送但无法确认真实发出(企业页/无标志) → 记 manual_check(需核对)
      'fail'  = 压根没到发送这步(找不到输入框/发送按钮)  → 记 fail(明确没发)
    ★ 原则: 宁可疑而不决。只有检测到真实发布标志才判 'sent', 其余宁可 'unsure' 或 'fail',
      绝不在不确定时谎报"已真发"。
    ★ 防串页(2026-09-07): 传入目标作者 author, 确认阶段校验当前笔记作者==目标作者,
      防止发送/确认时页面串到自家或别的笔记(误在自家评论区刷留言 = 雷区)。"""
    g_nodes = _nodes(d)
    et = _find(g_nodes, lambda n: n["cls"] == "EditText")
    # 若多个 EditText(评论编辑弹层), 取最下方/焦点那个
    if et:
        # ★ 关键修复(2026-09-07): 不用 d.send_keys() 注入文本。
        #   实测(近我者甜, NoteCommentActivity): d.send_keys 输入后评论编辑层【整个消失】
        #   (发送按钮+输入框同时不见) → 后续永远找不到『发送』按钮, 真发必 fail。
        #   改用 EditText.set_text 注入文本: 编辑层稳定留存(发送按钮=1 EditText=1), 可正常点发送。
        try:
            # 先点一下输入框聚焦(部分输入场景需焦点才接受文本)
            _click_center(d, et)
            time.sleep(0.5)
            el = d(className="android.widget.EditText")[0]
            el.set_text(text)
            time.sleep(0.9)
        except Exception as e:
            print(f"  [warn] set_text 失败({e}), 退回 send_keys")
            d.send_keys(text)
            time.sleep(1.0)
        # 双保险: 确认编辑层还在(发送按钮可定位)。若仍消失, 直接判 fail, 不硬点
        if not (send and _find(_nodes(d), lambda x: ("发送" in _f(x) or "发表" in _f(x) or "发布" in _f(x)))):
            if send:
                print("  [warn] 输入后发送按钮仍未出现, 编辑层可能已崩")
    else:
        print("  [FAIL] 找不到评论区输入框")
        return "fail"
    if not send:
        print("  [SKIP] dry-run: 已输入未发送")
        return "dryrun"
    # ★ 关键修复(2026-09-07 · 真发成功根因): 千万不要在输入后调 d.hide_keyboard() 再找『发送』按钮。
    #   实测(近我者甜, NoteCommentActivity): set_text 后编辑层稳定留存(发送按钮=1 EditText=1)，
    #   但一旦调 hide_keyboard()【会把整个评论编辑层收走】→ 发送按钮从 UI 树消失 → 报"未找到发送按钮"。
    #   之前『鱼块』多次 fail 的真凶就是 hide_keyboard。正确做法: set_text 后【不收起键盘】直接点发送。
    #   已验证: set_text → 直接点发送 → 会检测到「评论已发布」标志, 真发成功。
    # ★ 发送前双保险: 若此时已跳成企业号/会话页, 立即停, 不把消息发进企业会话。
    #    (open_note->tap_comment_box 可能已通过, 但发送瞬间页面被跳成会话页)
    biz, bm = _is_biz_page(d)
    if biz:
        print(f"  [SKIP] 发送前检测到企业号/会话页[{','.join(bm[:3])}], 停止不发送")
        return "fail"
    # 找『发送』按钮(包含匹配, 兼容"发送/发表/发布")
    snd = _find(_nodes(d), lambda n: ("发送" in _f(n) or "发表" in _f(n) or "发布" in _f(n)))
    if snd:
        _click_center(d, snd)
        print("  [SEND] 已点发送")
        # ★ 确认(2026-09-07 · 可靠判据双保险):
        #   1) 浮条(短命 toast): 点发送后立即截+测, 命中即真发。
        #   2) 评论列表找到我的文字: 进评论区滚动, 读到我的留言 = 铁证(最可靠, 浮条易错过)。
        #   两者任一出即判 sent; 都不过 → unsure(宁可疑而不决, 绝不谎报)。
        time.sleep(0.35)
        shot_sent = _save_shot_now(d, f"{text[:8]}")          # 即时抓浮条
        confirmed, why = _confirm_sent(d, text)                # 浮条检测
        if confirmed:
            print("  [确认] 已确认发出(浮条): " + why)
            print(f"  [截图] 发送铁证已存: {shot_sent}")
            _settle_comment_view(d, text)
            return "sent"
        # 浮条未命中(可能错过 toast): 进评论区列表找我的文字(最可靠铁证)
        confirmed2, why2 = _confirm_comment_found(d, text, author=author)
        if confirmed2:
            print("  [确认] 已确认发出(评论区找到我的留言): " + why2)
            return "sent"
        print("  [未确认] " + why2)
        return "unsure"
    print("  [FAIL] 未找到『发送』按钮")
    return "fail"


def _is_biz_page(d):
    """识别是否进入了【企业号/会话/聊天页】而非普通笔记评论区。
    特征: 顶部有企业认证标识 / 提示「进入企业会话」 / 出现『联系企微』『企业』等按钮。
    这类页面点『发送』是发企业会话消息, 不是发公开评论, 属雷区, 不应记为真发评论。
    ★ 加固(2026-09-07): 实测「你的对标企业号」会话页漏判, 补覆盖 企业号会话/私信卡/留资/报价/认证
      等特征词。小红书企业号/专业号客服会话页非常典型: 顶部蓝V认证 + 联系微信/电话按钮 + 获取行程报价卡片。"""
    import time
    try:
        txt = _dump_text(d)
    except Exception:
        return False
    marks = ["企业会话", "进入企业", "联系企微", "联系商家", "企业微信",
             "企业认证", "官方认证", "蓝V", "已认证", "商家",
             # 企业号客服会话页 / 私信卡 特征(2026-09-07 补)
             "获取行程", "获取报价", "行程和报价", "留资", "留资料", "咨询顾问",
             "客服老师", "顾问", "在线咨询", "获取联系方式", "企业号",
             "专业号", "认证主体", "营业执照", "私信卡", "广告", "推广"]
    hit = [m for m in marks if m in txt]
    if hit:
        return True, hit
    return False, []


def _dump_text(d):
    """把当前界面所有可见文本拼接(用于发布状态/页面类型检测)。"""
    try:
        return " ".join(_f(n) for n in _nodes(d) if _f(n))
    except Exception:
        return ""


def _has_sent_mark(d, text):
    """检测「发布成功」类标志: 黑底浮条『评论已发布』/『发布成功』/『已发送』等。
    返回 (是否命中, 命中的标志文本)。"""
    try:
        txt = _dump_text(d)
    except Exception:
        return False, ""
    marks = ["评论已发布", "发布成功", "已发送", "发送成功", "评论成功", "评论已发送"]
    hit = [m for m in marks if m in txt]
    return (len(hit) > 0), (",".join(hit) if hit else "")


def _confirm_sent(d, text):
    """发送后核验『是否真的发出去了』。返回 (confirmed:bool, reason:str)。
    ★ 判定标准(2026-09-07 重大收紧): 【只有检测到发布成功标志(评论已发布/发布成功…)才判已真发】。
      "发送按钮消失"作为判据【已废弃】——实测发现页面跳转/串页/键盘收起也会让发送按钮从界面消失,
      用它会【误报 sent】(把"页面跳走了"当成"评论发出去了")。宁可 miss, 不可谎报。
      因此: 无浮条一律判未确认(unsure), 由人工核截图。宁可疑而不决。"""
    import time
    # 轮询浮条(短命 toast, 短暂窗口内多次检测), 抓到即真发
    for _ in range(6):
        ok, mark = _has_sent_mark(d, text)
        if ok:
            return True, f"检测到发布标志[{mark}]"
        time.sleep(0.2)
    # 再测一次(浮条可能刚好在上轮之后才上屏)
    ok, mark = _has_sent_mark(d, text)
    if ok:
        return True, f"检测到发布标志[{mark}]"
    # 误入企业会话/聊天页 → 明确判未确认(不是真发评论)
    biz, bm = _is_biz_page(d)
    if biz:
        return False, f"疑似误入企业会话页[{','.join(bm[:3])}], 非公开评论, 需核对"
    # 无发布标志 → 无法确认(页面可能已串页/按钮消失都是干扰项)
    return False, "未检测到『评论已发布』标志, 无法确认真发, 需人工核截图"


def _save_shot_now(d, tag=""):
    """立即截图(无需 settle), 用于抓「发送后即时状态」的铁证(浮条)。
    返回相对 out/ 的路径 或 ""。"""
    import datetime as _dt, re as _re
    try:
        os.makedirs(SHOT_DIR, exist_ok=True)
        ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = _re.sub(r'[\\/:*?"<>|]', "_", tag)[:16] or "sent"
        fp = os.path.join(SHOT_DIR, f"{ts}_snap_{safe}.png")
        d.screenshot(fp)
        if os.path.exists(fp):
            return os.path.relpath(fp, ROOT).replace("\\", "/")
        return ""
    except Exception as e:
        print(f"  [warn] 即时截图失败: {e}")
        return ""


def _settle_comment_view(d, sent_text=""):
    """发送成功后, 把页面调到「能看到刚发布的留言」的状态: 收键盘(用 hide_keyboard, 不误退页) + 滚到评论区底部。
    目的: 让 _save_shot 拍到的不是空输入框/顶部提示条, 而是"笔记内容 + 我发的那条留言"同框。
    ★ 关键: 不用 press('back') 收键盘——若键盘已自动收起, back 会退出笔记详情页, 导致截图拍不到评论区。"""
    import time
    # 1) 收起键盘(内建方法, 不会退页)
    try:
        d.hide_keyboard()
    except Exception:
        pass
    time.sleep(0.6)
    # 2) 确认仍在笔记详情页(NoteDetail); 若被误退, 尝试点评论入口回到评论区
    act = d.app_current().get("activity") or ""
    if "NoteDetail" in act or "Detail" in act:
        # 向下滚评论区, 让最新留言(刚发的)进入视野。先小幅滚、再滚一次到底部
        try:
            d.swipe(540, 1500, 540, 750, duration=0.5)
            time.sleep(0.7)
        except Exception:
            pass
        try:
            d.swipe(540, 1450, 540, 700, duration=0.6)
            time.sleep(0.7)
        except Exception:
            pass
    else:
        print("  [warn] 发送后页面不在笔记详情(可能被误退), 尝试回评论区")
        # 尝试点『说点什么...』再度进入评论区(若还在笔记页只是评论层收起)
        try:
            for _ in range(2):
                nodes = _nodes(d)
                c = _find(nodes, lambda n: ("说点什么" in _f(n) or "评论" in _f(n))
                          and n["click"] and n["b"][1] > 1700)
                if c:
                    _click_center(d, c)
                    time.sleep(1.0)
                    break
        except Exception:
            pass


def back_home(d):
    """退出笔记/搜索, 回到可再次搜索的白地。"""
    for _ in range(3):
        act = d.app_current().get("activity") or ""
        if "IndexActivityV2" in act:
            return True
        d.press("back")
        time.sleep(0.8)
    return True


def _confirm_comment_found(d, text, author=""):
    """最可靠的发送确认(2026-09-07): 进评论区列表滚动, 找到我发的那条留言文字。
    比「评论已发布」浮条更可靠(浮条是短命 toast 易错过; 评论在列表里能读出来 = 铁证已发出)。
    返回 (found:bool, reason:str)。
    ★ 原理: 点发送后页面往往回到笔记详情顶部。这里是 点评论入口 → 进评论区列表 → 向下滚动,
      逐屏读文本找 text 的关键词。找到 = 评论真实存在于该笔记评论区 = 100% 已发出。
    ★ 防串页(2026-09-07): author 为目标作者。确认前校验当前笔记作者==目标作者,
      若已串到自家/其他笔记(读到自家评论误判), 立即判失败。宁可疑而不决。"""
    import time
    def _kw(t):
        # 取我的留言里最独特的 2 个短语作为匹配锚点(避开短句误匹配)
        segs = [s.strip() for s in re.split(r'[，。？！、;：\n]', t or "") if len(s.strip()) >= 6]
        return segs[:2]
    anchor = _kw(text)
    if not anchor:
        anchor = [(text or "")[:8]]
    # 防串页: 当前笔记作者若与目标不符, 立即判失败(绝不确认自家/错误笔记)
    if author:
        cur = _current_note_author(d)
        if cur and cur not in author and author not in cur:
            print(f"  [串页拦截] 确认时页面在『{cur}』而非目标『{author}』, 判未确认")
            return False, f"确认时页面串到『{cur}』, 非目标『{author}』, 不确认真发"
    # 确保在笔记详情页; 若发送后被打回首页, 尝试重进评论区
    act = d.app_current().get("activity") or ""
    in_detail = ("NoteDetail" in act or "Detail" in act)
    in_comment = "Comment" in act or "comment" in act
    # 若已停在评论区列表(Comment Activity), 直接滚; 否则点评论入口进入
    if in_comment:
        pass
    elif in_detail:
        ok = tap_comment_box(d)
        if not ok:
            return False, "无法打开评论区列表核验"
    else:
        # 不在笔记详情也不在评论区(可能被打回搜索/首页): 尝试重进评论区失败则放弃
        return False, "发送后页面不在笔记详情/评论区, 无法核验"
    # 先判断当前屏是否已是评论列表(避免误把笔记正文当评论找)
    # 向下滚动, 逐屏找我的留言(最多 12 屏)
    for i in range(12):
        txt = _dump_text(d)
        if any(a in txt for a in anchor):
            return True, f"评论列表读到我的留言(第{i}屏)"
        try:
            d.swipe(540, 1500, 540, 800, duration=0.4)
            time.sleep(0.8)
        except Exception:
            break
    return False, "评论列表未找到我的留言(可能未发出或留言被拦)"


# ---- 读笔记详情正文/话题标签(增强命中判定) ----
def _extract_tags(texts):
    """从详情页文本里抽话题标签: 小红书话题多为 '#话题' / '#[话题]' 或带 # 前缀。"""
    tags = []
    for t in texts:
        t = t.strip()
        if not t:
            continue
        # 形如 #[研学] / #研学 / #带团日记
        for m in re.findall(r'#\s*(\[?[\u4e00-\u9fa5A-Za-z0-9_]{1,20}\]?)', t):
            if m and m not in tags:
                tags.append(m.lstrip('[').rstrip(']'))
    return " ".join(tags)


def read_note_body(d, max_len=600):
    """进入笔记详情后, 读取笔记正文(长文本)+话题标签, 用于更准的人群匹配。
    ★ 判定增强: 只靠标题太薄, 读正文能命中 '成都在哪研学' 这类标题不含人群词的笔记。
    返回 (body, tags)。复用 _nodes 提取, 防弹窗由 open_note 的 caller 负责。"""
    try:
        nodes = _nodes(d)
        texts = [n["text"] for n in nodes if n["text"]]
    except Exception:
        return "", ""
    # 正文 = 详情页里最长的几个中文文本(排除控件/作者名/评论)
    body_parts = []
    seen = set()
    for n in nodes:
        t = (n["text"] or "").strip()
        if len(t) < 8:
            continue
        if not re.search(r'[\u4e00-\u9fa5]', t):
            continue
        if any(x in t for x in ("说点什么", "评论", "关注", "收藏", "分享",
                               "作者", "@", "小时前", "天前", "分钟前", "刚刚", "展开")):
            continue
        if t in seen:
            continue
        seen.add(t)
        body_parts.append(t)
    # 排序: 长的优先(正文往往比评论长), 拼接
    body_parts.sort(key=len, reverse=True)
    body = " ".join(body_parts[:6])[:max_len]
    tags = _extract_tags(texts)
    # 若正文太短(可能没加载出来), 退回空
    if len(body) < 10:
        body = ""
    return body, tags


def ensure_search_reset(d):
    """确认回到搜索结果页(可再次 open_note)。若退回的是首页/详情页, 重新走搜索。
    返回 True=已在可点笔记的结果列表页。每次 open_note 前调用, 防止跨篇脏状态。"""
    act = d.app_current().get("activity") or ""
    nodes = _nodes(d)
    # 已在结果页: 节点够多(有卡片)
    if len(nodes) >= 100 and not ("NoteDetail" in act or "Detail" in act):
        return True
    # 退到首页或详情页: 重新搜索关键词
    if not go_search(d):
        return False
    return True


# ---------------------------------------------------------------- 内容
def _load_targets():
    sys.path.insert(0, CLS)
    from proactive_plan import load_targets, enabled_targets, match_target, plan_comment
    return load_targets(), match_target, plan_comment


def _load_chart(cfg):
    """读频控参数(proactive_targets.yaml global)。"""
    g = (cfg or {}).get("global", {}) or {}
    return {
        "cap": int(g.get("daily_comment_cap", 8) or 0),
        "iv": int(g.get("comment_interval_min", 15) or 0),
        "per_note": int(g.get("per_note_max_comment", 1) or 1),
        "open_cap": int(g.get("open_notes_cap", 20) or 0),
    }


def _read_log():
    if not os.path.exists(PROACTIVE_LOG):
        return []
    try:
        with open(PROACTIVE_LOG, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _write_log(items):
    os.makedirs(OUT, exist_ok=True)
    with open(PROACTIVE_LOG, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)


def _save_shot(d, author="", kw="", title=""):
    """真发成功后截屏留下留痕图(已经是发出去后的状态, 含刚发的留言上下文)。
    存 out/proactive_shots/。返回 相对路径(相对 interceptor/) 或 ""。
    ★ 意图: 用户点明细要看"我到底发给谁、发了什么"。所以截图要尽量拍到
      「笔记内容/标题 + 评论区里我刚发的那条」上下文, 而不是空输入框或顶部提示条。"""
    import datetime as _dt
    try:
        os.makedirs(SHOT_DIR, exist_ok=True)
        ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = re.sub(r'[\\/:*?"<>|]', "_", (author or "未知")[:20])
        fp = os.path.join(SHOT_DIR, f"{ts}_{safe}.png")
        d.screenshot(fp)
        if os.path.exists(fp):
            rel = os.path.relpath(fp, ROOT).replace("\\", "/")
            return rel
        return ""
    except Exception as e:
        print(f"  [warn] 截图失败: {e}")
        return ""


def _log_action(action, author="", title="", target="", engine="", comment="", kw="", note="", shot=""):
    """记录一次主动获客动作(命中/发送/跳过/失败/频控/待核)——结构化流水。
    action ∈ {hit, send, skip, fail, rate_block, no_target, search_fail, manual_check}。
    ★ manual_check = 已点发送但无法确认真实发出(疑似企业会话页/无发布标志), 需人工核截图。
    shot = 真发后截图相对路径(留痕)。"""
    log = _read_log()
    log.append({
        "type": action, "author": author, "title": title, "target": target,
        "engine": engine, "comment": comment, "kw": kw, "note": note,
        "shot": shot,
        "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "epoch": int(time.time()),
    })
    _write_log(log)


def _sent_today(log):
    today = datetime.date.today().isoformat()
    return sum(1 for x in log if x.get("type") in ("send",) and (x.get("ts") or "").startswith(today))


def _recent_author(log, author, window_h=24):
    """同作者去重: 该作者在 window_h 小时内是否已「真正发出去」过。返回 True=近期已真发, 别重复打扰。
    只有 send(真发)/manual_check(已点发送未确认, 需防重复) 才算「已接触」。
    hit(dry-run 生成未发)/fail(未发出)/skip(跳过)/no_target 都说明没真发出去, 不该挡住后续尝试。
    ★ 关键修复(2026-09-07): 之前把 hit 也当已接触, 导致 dry-run 生成未发的作者被误拦, 本轮 0 条。
    兼容旧日志(无 type 字段的都当 send 处理, 因旧格式只记真发)。"""
    if not author:
        return False
    cutoff = time.time() - window_h * 3600
    for x in log:
        typ = x.get("type") or "send"      # 旧记录无 type → 视作已真发
        if typ in ("send", "manual_check") and x.get("author") == author \
           and (x.get("epoch") or 0) > cutoff:
            return True
    return False


def _last_ts(log):
    # send/hit/manual_check 都算「最近一次主动动作」: hit(dry-run 生成了)/manual_check(点了发送待核)
    # 用于频控计时, 避免快速连发触发平台风控。
    ts = [x.get("epoch") or 0 for x in log if x.get("type") in ("send", "hit", "manual_check")]
    return max(ts) if ts else None


def _check_rate(chart):
    """频控校验。返回 (ok, msg)。"""
    log = _read_log()
    if chart["cap"] > 0:
        n = _sent_today(log)
        if n >= chart["cap"]:
            return (False, f"已达单日上限 {chart['cap']} 条(今日已留 {n})")
    if chart["iv"] > 0:
        last = _last_ts(log)
        if last is not None:
            wait_s = chart["iv"] * 60
            if time.time() - last < wait_s:
                remain = int((wait_s - (time.time() - last)) / 60) + 1
                return (False, f"距上一条不足 {chart['iv']} 分钟, 还需等约 {remain} 分钟")
    return (True, "ok")


def _log_sent(author, title):
    log = _read_log()
    log.append({"author": author, "title": title,
                "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "epoch": int(time.time())})
    _write_log(log)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kw", default="研学", help="搜索关键词(默认研学)")
    ap.add_argument("--target", default="", help="指定目标类型 key(默认自动匹配)")
    ap.add_argument("--max", type=int, default=3, help="本轮最多留言条数(默认3)")
    ap.add_argument("--send", action="store_true", help="真发(dry-run 默认不发送)")
    ap.add_argument("--confirm", action="store_true", help="确认真发")
    a = ap.parse_args()

    if a.send and not a.confirm:
        print("⚠️  --send 需配 --confirm 才真发, 当前转 dry-run")
        a.send = False

    cfg, match_target, plan_comment = _load_targets()
    chart = _load_chart(cfg)
    print(f"关键词={a.kw!r} 目标={a.target or '自动'} 真发={a.send} 单轮上限={a.max}")
    print(f"频控: 单日={chart['cap']} 间隔={chart['iv']}min 每篇={chart['per_note']}")

    if a.send:
        ok, msg = _check_rate(chart)
        if not ok:
            print(f"[频控] 停止: {msg}")
            return
        print("[频控] 通过")

    d = conn()
    wake_and_open(d)
    if not go_search(d):
        print("[warn] 未能进入搜索页")
        _log_action("search_fail", kw=a.kw, note="未能进入搜索页")
        return

    # 搜索关键词
    if not input_search(d, a.kw):
        print("[warn] 搜索输入失败")
        _log_action("search_fail", kw=a.kw, note="搜索输入失败")
        return

    notes = collect_notes(d)
    # ★ 修复(2026-09-07): 搜索后若只命中自家账号(或结果太少), 滚动加载更多, 避免直接 no_target。
    #   实测「研?#」首次搜索常只返回自家号(你的账号A), 过滤后 0 条 → 误报 no_target。
    #   向下滚动几轮, 让非自家作者的笔记进入视野。
    _own = _own_nick()
    if _own:
        for _ in range(5):
            if any(nt.get("author", "").strip() != _own for nt in notes):
                break
            try:
                d.swipe(540, 1500, 540, 700, duration=0.5)
                time.sleep(1.2)
                notes = collect_notes(d)
            except Exception:
                break
    # ★ 过滤自家账号的笔记: 不能去评论自己发的笔记(YOUR_DEVICE_SERIAL=你的账号A)
    if _own:
        _before = len(notes)
        notes = [nt for nt in notes if nt.get("author", "").strip() != _own]
        if len(notes) < _before:
            print(f"  [过滤] 剔除自家账号『{_own}』的 {_before - len(notes)} 条笔记")
    print(f"\n=== 搜索结果命中 {len(notes)} 条笔记 ===")
    if not notes:
        _log_action("no_target", kw=a.kw, note="搜索无结果或无可命中笔记(含剔除自家账号)")

    def _resume_search():
        """回到「可点笔记」的结果列表页。已搜索过能复用则直接回, 否则重搜。"""
        act = d.app_current().get("activity") or ""
        nodes = _nodes(d)
        if len(nodes) >= 100 and not ("NoteDetail" in act or "Detail" in act):
            return True
        if not go_search(d):
            return False
        if not input_search(d, a.kw):
            return False
        return True

    candidates = []

    def _cur_track():
        """推导当前账号赛道(从 lanes.yaml accounts 按 XHS_DEVICE 映射)。用于 match_target 的 prefer_track。"""
        try:
            import sys as _s
            _s.path.insert(0, CLS)
            dev = os.environ.get("XHS_DEVICE", "") or "默认设备"
            import yaml as _y
            lanes = _y.safe_load(open(os.path.join(ROOT, "console", "lanes.yaml"), encoding="utf-8")) or {}
            acc = (lanes.get("accounts") or {}).get(dev) or {}
            return acc.get("lane", "")
        except Exception:
            return ""

    _PREFER_TRACK = _cur_track()

    def _note_match_once(nt, body="", tags=""):
        """判定一篇笔记命中哪个 target + 生成留言。返回候选 dict 或 None。
        ★ 两层判定: 先用 title 粗筛, 不进详情; 若调用方要正文增强, 传 body/tags 再判一次。
        ★ prefer_track: 当前账号赛道优先(避免宽泛 target 抢走细分人群)。"""
        key, tgt = match_target(cfg, title=nt["title"], body=body or nt["title"],
                                tags=tags, key=a.target or "", prefer_track=_PREFER_TRACK)
        if tgt is None:
            return None
        pl = plan_comment(cfg, tgt, nt["title"], body, prefer="llm")
        return {"author": nt["author"], "title": nt["title"],
                "y": nt["y"], "target": pl["target"],
                "comment": pl["comment"], "engine": pl["engine"],
                "dm_direction": pl["dm_direction"],
                "pass_red": pl["engine"] != "none",
                "from": "body" if (body or tags) else "title"}

    # ① 标题粗筛(不点进详情, 快)
    for nt in notes:
        c = _note_match_once(nt)
        if c:
            candidates.append(c)
            print(f"  [{c['engine']:>4}·标题] {c['author']}「{c['title'][:24]}」→ {c['target']}")
            print(f"       留言: {c['comment'][:50]}...")

    # ② 正文/标签增强: 对"标题没命中"的笔记, 进详情读正文/标签后再判一次
    #   ★ 修复逻辑缺口: 之前被 `a.target==""` 卡死(指定目标后永不增强) + 命中>0 也放弃。
    #     现在无论是否指定 target, 只要标题命中不足, 就补一轮正文增强, 让"关键词不在标题、
    #     但正文/标签含人群信号"的笔记也能被接住。
    #   ★ 只对标题命不中的笔记补(已命中的不重复进详情, 省时)。
    already = set((c["author"], c["title"][:18]) for c in candidates)
    need_enh = [nt for nt in notes if (nt["author"], nt["title"][:18]) not in already]
    if need_enh:
        print(f"\n=== 标题命中 {len(candidates)} 条, 对 {len(need_enh)} 条未命中笔记补正文/标签增强 ===")
        tried = 0
        for nt in need_enh[:8]:                       # 最多试前 8 条, 避免耗时
            if tried >= 4:
                break
            if not _resume_search():
                print("  [warn] 未能复位到搜索结果页, 停止正文增强")
                break
            notes2 = collect_notes(d)
            # ★ 修复隐藏 bug(2026-09-07): 正文增强路径也必须过滤自家账号, 否则会进自己发的笔记点发送,
            #   产生对自家号(你的账号A)的 manual_check(日志 18:41 即是)。
            if _own:
                notes2 = [nt2 for nt2 in notes2 if nt2.get("author", "").strip() != _own]
            c2 = None
            for nt2 in notes2:
                if nt2["author"] == nt["author"] and nt2["title"][:18] == nt["title"][:18]:
                    c2 = nt2
                    break
            if c2 is None:
                c2 = nt
            if not open_note(d, c2):
                print(f"  [skip] 进「{nt['author']}」详情失败")
                back_home(d)
                continue
            body, tags = read_note_body(d)
            if body or tags:
                c = _note_match_once(nt, body=body, tags=tags)
                if c:
                    candidates.append(c)
                    print(f"  [正文{c['engine']:>4}] {c['author']}「{c['title'][:24]}」→ {c['target']}")
                    print(f"       正文: {body[:60]}...")
                    print(f"       留言: {c['comment'][:50]}...")
            back_home(d)
            tried += 1

    matched = [c for c in candidates if c["pass_red"] and c["comment"]]
    # 同作者去重(24h 内不重复主动留言同一作者)
    matched = [c for c in matched
               if not _recent_author(_read_log(), c["author"], window_h=24)]
    print(f"\n=== 命中目标且过红线: {len(matched)} 条(已去重; 本轮到 {min(a.max,len(matched))} 条) ===")

    did = 0
    for c in matched:
        if a.send:
            ok2, msg2 = _check_rate(chart)
            if not ok2:
                print(f"[频控] {msg2}, 停止")
                _log_action("rate_block", note=msg2, kw=a.kw)
                break
        if did >= a.max:
            break
        # 每篇开始前确保在结果列表页(跨篇复位)
        if not _resume_search():
            print("  [warn] 未能复位到搜索结果页, 停止本轮")
            break
        # 重新收集(复位后坐标可能刷新), 找与 c 匹配的那条
        notes = collect_notes(d)
        if _own:
            notes = [nt for nt in notes if nt.get("author", "").strip() != _own]  # 也剔除自家账号
        c2 = None
        for nt in notes:
            if nt["author"] == c["author"] and nt["title"][:18] == c["title"][:18]:
                c2 = {"title": nt["title"], "y": nt["y"], "author": nt["author"]}
                break
        if c2 is None:
            # 没找到精确匹配, 就直接用记录的标题+兜底坐标, 继续尝试
            c2 = {"title": c["title"], "y": c["y"], "author": c["author"]}
        # 进这篇笔记
        print(f"\n>> 进入「{c['author']}」{c['title'][:24]}")
        if not open_note(d, c2):
            print("  [SKIP] 进入笔记详情失败")
            _log_action("fail", author=c["author"], title=c["title"],
                        target=c["target"], engine=c["engine"],
                        comment=c["comment"], kw=a.kw, note="进入详情失败")
            back_home(d)
            continue
        # ★ 进入后先判页面类型: 若误入企业号/会话/聊天页, 直接跳过不发送(雷区)
        if a.send:
            biz, bm = _is_biz_page(d)
            if biz:
                print(f"  [SKIP] 该笔记是企业号/会话页[{','.join(bm[:3])}], 跳过不发送, 避免误发企业会话")
                _log_action("skip", author=c["author"], title=c["title"],
                            target=c["target"], engine=c["engine"],
                            comment=c["comment"], kw=a.kw, note="企业号/会话页, 跳过发送")
                back_home(d)
                continue
        if not tap_comment_box(d):
            print("  [SKIP] 评论区输入框未找到")
            _log_action("fail", author=c["author"], title=c["title"],
                        target=c["target"], engine=c["engine"],
                        comment=c["comment"], kw=a.kw, note="评论区输入框未找到")
            back_home(d)
            continue
        st = input_and_send(d, c["comment"], send=a.send, author=c["author"])
        if st == "sent":
            print(f"  [记录] 已留言 @{c['author']}")
            # ★ 真发留痕: 截图保存, 供查阅明细时对"发给谁哪篇笔记"
            shot = _save_shot(d, author=c["author"], kw=a.kw, title=c["title"])
            print(f"  [截图] 留痕已存: {shot or '(失败)'}")
            _log_action("send", author=c["author"], title=c["title"],
                        target=c["target"], engine=c["engine"],
                        comment=c["comment"], kw=a.kw, note="已真发", shot=shot)
        elif st == "unsure":
            # ★ 点了发送但无法确认发出(如误入企业会话页/无发布标志) → 记【需核对】, 不报"已真发"
            print(f"  [记录] 已点发送但未确认发出 @{c['author']} —— 不记为已真发, 需人工核截图")
            shot = _save_shot(d, author=c["author"], kw=a.kw, title=c["title"])
            print(f"  [截图] 待核留痕已存: {shot or '(失败)'}")
            _log_action("manual_check", author=c["author"], title=c["title"],
                        target=c["target"], engine=c["engine"],
                        comment=c["comment"], kw=a.kw,
                        note="已点发送但未确认发出, 需人工核验(疑似企业会话页)", shot=shot)
        elif st == "fail":
            # ★ 压根没发出去(找不到输入框/发送按钮) → 明确记 fail, 不是"需核对"
            print(f"  [记录] 发送失败(未发出) @{c['author']}")
            _log_action("fail", author=c["author"], title=c["title"],
                        target=c["target"], engine=c["engine"],
                        comment=c["comment"], kw=a.kw, note="发送失败(未发出)")
        else:
            print(f"  [记录] 已生成(未发) @{c['author']}")
            _log_action("hit", author=c["author"], title=c["title"],
                        target=c["target"], engine=c["engine"],
                        comment=c["comment"], kw=a.kw, note="dry-run:已生成未发")
        # 清空输入 + 返回
        try:
            etidx = [i for i, n in enumerate(_nodes(d)) if n["cls"] == "EditText"]
            if etidx:
                el = d(className="android.widget.EditText")[0]
                cur = el.info.get("text", "")
                if cur:
                    el.set_text("")
                    time.sleep(0.5)
        except Exception:
            pass
        back_home(d)
        did += 1

    print(f"\n=== 本轮共处理 {did} 条(真发={a.send}) ===")


if __name__ == "__main__":
    main()
