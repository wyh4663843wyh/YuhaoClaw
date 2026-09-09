#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小红书 自动回复系统 · P1 采集器（只读 · 不发送）

职责:按「消息·未读角标」触发,进入小红书消息区,逐会话读取收到的私信留言,
      并将命中意图库的留言文本/用户/来源结构化落屏,交给 classify 分级拟稿。

红线:本文件只做"读",绝不执行任何回复/发送 action。退出条件见下。
GELab 方法论:坐标优先 → 文本/desc 锚点兜底 → dump 探针；单步超时护栏;卡住即中止。

用法(设备已连,如红米K40):
  python collect_inbox.py --once          # 只跑一个会话读取轮回,退出(默认)
  python collect_inbox.py --watch N       # 连续 N min :每轮触发检查 + 新留言读取

环境:需要已 pip 安装 uiautomator2 (隔离python env),手机开 USB 调试。 
"""
from __future__ import annotations

import argparse, csv, json, os, sys, tempfile, time, datetime, xml.etree.ElementTree as ET

# --------------------------------------------------------------------------- #
# 0. 常量 / 路径
# --------------------------------------------------------------------------- #
HERE        = os.path.dirname(os.path.abspath(__file__))
ROOT        = os.path.dirname(HERE)                       # interceptor/
OUT_DIR     = os.path.join(ROOT, "out")
OSCREEN_DIR = os.path.join(OUT_DIR, "screens")            # 屏记录截图
XML_LOCAL   = os.path.join(OUT_DIR, ".last_dump.xml")     # dump 后拷贝到本机
CSV_OUT     = os.path.join(OUT_DIR, "leads_raw.csv")      # 结构化落屏
JSON_LOG    = os.path.join(OUT_DIR, "session_last.json")  # 本次采集摘要
ANCHORS_FP  = os.path.join(HERE, "anchors.json")

PKG = "com.xingin.xhs"                                    # 小红书包名

# 我方已发布笔记标题(片段)常量表 —— 来源归因的关键匹配库。会话顶部的平台来源卡
# (『来自笔记』+标题) 或对端把笔记卡当首条消息发时,标题文本在其中,靠这表反查。
# 结构:笔记标题关键词片段 -> (规范笔记名, 策略桶 src_type)
#   src_type 用于分级后按『来源→策略』自动选型(研学AI落地 / 接单入池 / 工具路人)。
# ⚠️ 账号主体名(你在小红书上的昵称, 见 lanes.yaml accounts[i].nick)不是笔记标题,勿混入;
#    新增笔记后必须在此登记, 否则来源归因漏配(README §9 落地项)。
KNOWN_NOTE_TITLES = {
    # ⬇︎ 示例：把你已发布的笔记标题片段(规范名, 策略桶)替换成你自己的。
    "你的笔记A":    ("你的笔记A", "opc_recruit"),
    "你的笔记B":    ("你的笔记B", "xueyan_ai"),
    "你的笔记C":    ("你的笔记C", "tool_public"),
}
# 长关键词优先命中(防『研学』吞『研学AI』),排序预生成
_KNOWN_MATCH_ORDER = sorted(KNOWN_NOTE_TITLES, key=len, reverse=True)

# 平台来源卡标签文本(在会话顶部出现,其后紧跟笔记标题)
_SRC_LABEL = "来自笔记"

# 单步超时护栏(GELab 内核对 7 分钟;这里采集步更短更稳)
STEP_TIMEOUT = 120     # 每个"找到锚点"等待秒数
SCROLL_PAUSE = 1.2     # 两次滑动间隔
POLL_STEP    = 0.6     # 无新留言判断用;连续同一个首条会话文本即视为收敛

os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(OSCREEN_DIR, exist_ok=True)


# --------------------------------------------------------------------------- #
# 1. 锚点读取
# --------------------------------------------------------------------------- #
_D = None            # uiautomator2 device 句柄(手动 session 内持有)


def anchor(kind: str, key: str):
    """
    anchors.json 形如:
    {"msg_tab": {"desc":"消息"}, "conv_title":{"class_end":"TextView"}}
    返回 dict;找不到返回空 dict(调用方自行降级)。
    """
    try:
        with open(ANCHORS_FP, encoding="utf-8") as f:
            return json.load(f).get(kind, {}).get(key, {})
    except Exception:
        return {}


def d():
    global _D
    if _D is None:
        import uiautomator2 as u2
        serial = os.environ.get("XHS_DEVICE", "")
        _D = u2.connect(serial) if serial else u2.connect()
    return _D


# --------------------------------------------------------------------------- #
# 2. 屏 dump + 解析(文本/desc 优先,坐标兜底)
# --------------------------------------------------------------------------- #
def dump_current():
    """取当前屏 XML ElementTree root。红米MIUI适配(2026-09-04 真机)。

    踩坑结论:
      * d.dump_hierarchy('/sdcard/x.xml') 带路径 → uia2/jsonrpc,MIUI 抛
        RPCUnknownError(InvalidFormatException)→ 禁用。
      * adb 原生 uiautomator dump 在小米上常 rc=137(SIGKILL,界面非 idle)不稳。
      * d.dump_hierarchy() 无参字符串形式实测最稳(u2 内部走 shell+自带重试)。
    故主路径 = u2 无参 dump→ET.fromstring;失败再 adb+等idle 重试兜底。
    """
    xml_str = _dump_str()
    if xml_str:
        try:
            return ET.fromstring(xml_str)
        except ET.ParseError:
            pass
    # 兜底:adb 原生,失败则等界面 idle 后多试几次
    return dump_via_adb_retry()


def _strip_xml_decl(s):
    # u2 偶尔返回带解码头的字符串,去第一行 <?xml ...?>
    idx = s.find("?>")
    return s[s.find("<", idx + 1):] if "<?xml" in s[:60] else s


def _dump_str():
    """u2 无参 dump 返回 str;不稳时重试若干次。"""
    import time as _t
    for _ in range(3):
        try:
            raw = d().dump_hierarchy()      # 无路径→shell 通道,实测最稳
            if raw and "<node" in raw:
                return _strip_xml_decl(raw)
        except Exception as e:
            print(f"[dump] u2 dump 失败({e});retry")
        _t.sleep(1.2)
    return None


def dump_via_adb_retry():
    """纯 adb uiautomator dump+pull,含等 idle 重试(rc137 时 2 次兜底)。"""
    import subprocess, time as _t
    serial = _serial()
    dev_opt = ["-s", serial] if serial else []
    onphone = "/sdcard/wb_ui_dump.xml"
    ok = False
    for _ in range(3):
        r = subprocess.run(["adb"] + dev_opt + ["shell",
                            "uiautomator", "dump", onphone],
                           capture_output=True, timeout=45)
        if r.returncode == 0:
            ok = True
            break
        _t.sleep(2.0)          # 等界面回 idle 再试(MIUI rc137 缓解)
    if not ok:
        raise RuntimeError("adb uiautomator dump 连续失败(界面未空闲?)")
    subprocess.run(["adb"] + dev_opt + ["pull", onphone, XML_LOCAL],
                   capture_output=True, timeout=60)
    return ET.parse(XML_LOCAL)


def _serial():
    return os.environ.get("XHS_DEVICE", "")


# ---- 多设备标签: 设备号解析(2026-09-06 改造, 防止线索错落"默认设备") ----
# 优先级: 命令行 --device > 环境变量 XHS_DEVICE > lanes.yaml 里唯一的真实序列号 > "默认设备"
# 背景: 之前只读环境变量, 忘记设 XHS_DEVICE 就会全部落"默认设备", 导致设备卡认不到真实账号。
# 现在即使忘设, 只要 lanes.yaml 定义了唯一账号序列号(YOUR_DEVICE_SERIAL), 也能自动带上。
_CLI_DEVICE = ""   # 命令行 --device 传入; 通过 set_cli_device() 设置


def set_cli_device(dev):
    global _CLI_DEVICE
    _CLI_DEVICE = (dev or "").strip()


def _lane_single_serial():
    """从 lanes.yaml accounts 里取『唯一真实序列号』(排除 默认设备 别名)。
    若明确定义了且只有一个真实账号 → 返回该序列号; 否则返回 ''(无法自动推断)。"""
    try:
        fp = os.path.join(ROOT, "console", "lanes.yaml")
        if not os.path.exists(fp):
            return ""
        import yaml
        with open(fp, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        acc = cfg.get("accounts") or {}
        # 排除"默认设备"这类别名, 只留真实序列号
        reals = [k for k in acc.keys() if k and k not in ("默认设备", "默认账号")]
        return reals[0] if len(reals) == 1 else ""
    except Exception:
        return ""


def _resolve_device():
    """统一的设备号解析。优先级: CLI > env > lanes唯一序列号 > 默认设备。"""
    if _CLI_DEVICE:
        return _CLI_DEVICE
    env = os.environ.get("XHS_DEVICE", "").strip()
    if env:
        return env
    lane = _lane_single_serial()
    if lane:
        return lane
    return "默认设备"


def _device():
    d = _resolve_device()
    return d if d else "默认设备"


def nodes_of(tree, text_kw=None, desc_kw=None, class_end=None, sel=None):
    """
    通用匹配:遍历 node。
      text_kw / desc_kw : 需内嵌的子串(支持多种写法加分号分隔)
      class_end         : class 末段,如 "TextView" / "LinearLayout"
      sel               : 若传 True,只返回该 node文本的 selected==true(用于判断Tab态)
    返回 [{text,desc,class,bounds,sel}]。
    """
    out = []
    tk = text_kw.split("|") if text_kw else []
    dk = desc_kw.split("|") if desc_kw else []
    for n in tree.iter("node"):
        a = n.attrib
        t, c, cls = a.get("text", ""), a.get("content-desc", ""), a.get("class", "")
        blobby = t + c
        if tk and not any(k in blobby for k in tk):
            continue
        if dk and not any(k in blobby for k in dk):
            continue
        if class_end and not cls.endswith(class_end):
            continue
        if sel is not None and (a.get("selected", "") != "true") != (not sel):
            continue
        out.append({
            "text": t, "desc": c, "class": cls,
            "bounds": a.get("bounds", ""), "sel": a.get("selected", "false"),
        })
    return out


def cxmt(node):
    """把边界串 "[x1,y1][x2,y2]" 转中心 (x,y)。失败返回 None"""
    import re
    m = re.findall(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
    if not m:
        return None
    x1, y1, x2, y2 = (int(v) for v in m[0])
    return (x1 + x2) // 2, (y1 + y2) // 2


# --------------------------------------------------------------------------- #
# 3. 原语动作
# --------------------------------------------------------------------------- #
def tap(x, y):
    d().click(x, y)
    time.sleep(1.0)


def swipe_up(steps=6):
    w, h = d().window_size()
    d().swipe(w // 2, int(h * 0.78), w // 2, int(h * 0.25),
              duration=0.2 / steps if steps else 0.2)
    time.sleep(SCROLL_PAUSE)


def ensure_app_foreground():
    """启动/前台化小红书;若已在前台则不动。启动后等首屏。"""
    dd = d()
    dd.app_start(PKG, use_monkey=True, stop=False)
    time.sleep(2.5)
    return dd


_FOCUS_RE = None
def xhs_on_top():
    """
    2026-09-04 真机加护栏:运行中若来真实来电/系统弹窗抢占前台,
    继续点会话会点到拨号/桌面里,采出 短信/微信/电话 等垃圾文本。
    每次采集前先确认前台进程是小红书;漂移则回拉。
    返回 bool。
    """
    global _FOCUS_RE
    if _FOCUS_RE is None:
        import re
        _FOCUS_RE = re.compile(r"com\.xingin\.xhs")
    try:
        fg = d().app_current()        # {'package': ..., 'activity': ...}
        pkg = fg.get("package", "")
        return bool(pkg) and _FOCUS_RE.search(pkg) is not None
    except Exception:
        # 取前台失败不算"在XHS";保守返回 False 让上层去回拉
        return False


def guard(cond):
    """护栏:反复轮询直到 cond() 为真或 STEP_TIMEOUT 超时中止。"""
    deadline = time.time() + STEP_TIMEOUT
    while time.time() < deadline:
        tree = dump_current()
        if cond(tree):
            return tree
        time.sleep(POLL_STEP)
    print("[guard] 超时未满足,中止本轮,走重启清理")
    raise TimeoutError("guard timeout")


# --------------------------------------------------------------------------- #
# 4. 消息区触发与读取
# --------------------------------------------------------------------------- #
def find_msg_tab(tree):
    """在底部导航找『消息』Tab(desc 常为 '消息,N条未读' 或 '消息')。

    2026-09-04 真机踩坑:消息总览页里『系统消息』互动/系统块 desc 含 '消息'
    且 y=1918 > 0.72*h(1728),会先于底部导航『消息』Tab(y≈2270)命中。
    修正:
      * 优先要求 desc == '消息' 或 '消息,N条未读'(精确锚定底部 Tab)
      * 排除含 '_NON_CONV_PREFIX' 的块(系统消息 等)
      * y 阈值收到 0.90*h(底部导航安全区)
    """
    wh = d().window_size()
    h = wh[1]
    for n in nodes_of(tree, desc_kw="消息"):
        dsc = (n.get("desc") or "")
        c = cxmt(n)
        if not c:
            continue
        _, y = c
        if y <= h * 0.90:                 # 必须落在底部导航安全区
            continue
        # 精确锚定:'消息' 或 '消息,N条未读';或至少不以系统/互动块前缀开头
        if dsc.startswith("消息"):
            return n
        is_notif_block = any(dsc.startswith(p) for p in
                             ("系统消息", "赞和收藏", "新增关注", "评论和@",
                              "活动消息", "消息通知"))
        if not is_notif_block:
            return n
    return None


# --------------------------------------------------------------------------- #
# 4.1 消息视图识别(兼容「私信会话列表」与「陌生人来信页」)
# --------------------------------------------------------------------------- #
# 2026-09-05 踩坑沉淀(宇豪真机观察):
#   * 『陌生人消息』是**独立来信页**, 不是「私信会话列表」。
#   * 旧 collect_a_round 入口死等 底部『消息』Tab(find_msg_tab), 页面停在陌生
#     来信页/非标准列表态 → guard 120s 超时 → raise TimeoutError → 重开App清屏,
#     这类来信永远进不了采集管道。
#   * 修复思路 = **不死等**: 识别"当前是否已是消息视图", 是则直接用, 不是才点
#     底部『消息』Tab; 点了仍未达 → 降级返回 None(交上层跳过), 绝不 raise。
# 视图枚举:
#   dm_list  : 私信会话列表(行 desc 形如 `昵称，正文，日期` / `昵称，，正文`)
#   stranger : 陌生人来信页(屏上出现『陌生人』/『来信』提示, 或行用 ，， 双逗号)
#   unknown  : 无法识别(在发现页/会话正文/系统界面等)
_STRANGER_HINT = ("陌生人", "来信", "陌生人消息")

# 行 desc 可能的分隔形式(陌生来信行常见 `，，` 双逗号; 私信列表 `，` 单逗号)
_DESC_SEPS = ("，，", "，")


def _detect_msg_view(tree):
    """识别当前屏属于哪种消息视图。返回 'dm_list' / 'stranger' / 'unknown'。
    判据(文本/desc 优先, 不依赖坐标):
      * 屏上已有会话行(_extract_convs 能解析出候选) → dm_list
      * 否则屏上出现 陌生来信 提示词标题 → stranger
      * 否则 unknown
    注意: 陌生来信行若能被 _extract_convs 解析, 也先归 dm_list(可采集为主)。
    这里把『陌生来信』作为独立态, 一是日志可观测, 二为后续可能的专用采集路线留钩子。
    """
    if tree is None:
        return "unknown"
    try:
        if _extract_convs(tree):
            return "dm_list"
    except Exception:
        pass                               # 取窗口尺寸失败不碍事, 走提示词判据
    dts = _dump_texts(tree)
    if any(h in dts for h in _STRANGER_HINT):
        return "stranger"
    return "unknown"


def _ensure_msg_view():
    """确保当前处于可采集的消息视图(私信列表 / 陌生来信页), 幂等, **不死等**。
    返回该屏 tree(已可采集); 若彻底无法进入消息视图 → None(上层降级跳过)。
    步骤:
      1) 当前已是消息视图 → 直接返回;
      2) 找底部『消息』Tab 并点它 → 再判;
      3) 有界短等几次(非全时长) → 仍未知则返回 None。
    """
    tree = dump_current()
    if _detect_msg_view(tree) != "unknown":
        return tree
    # 找底部『消息』Tab 点它(若当前在发现页/其它 Tab)
    n = find_msg_tab(tree)
    if n and cxmt(n):
        print(f"[step] 点底部『消息』Tab @ {cxmt(n)},desc='{n['desc']}'")
        tap(*cxmt(n))
        tree = dump_current()
        if _detect_msg_view(tree) != "unknown":
            return tree
    # 有界短等(每 0.6s 判一次, 最多 ~2.4s; 不再全时长死等)
    for _ in range(4):
        time.sleep(POLL_STEP)
        tree = dump_current()
        if _detect_msg_view(tree) != "unknown":
            return tree
    print("[step] 未进入任何消息视图(可能不在消息区);本轮跳过")
    return None


def count_unread(tree):
    """从消息 Tab 的 desc '消息,N条未读' 里抓 N;解析不到返回 None(视为未知非零)。"""
    for n in nodes_of(tree, desc_kw="消息"):
        import re
        m = re.search(r"(\d+)", n["desc"])
        if m:
            return int(m.group(1))
    return None


def collect_a_round(max_conv=20):
    """
    单轮回:触发检查 → 进消息视图(私信列表/陌生来信页) → 逐会话采集首条对方文本。
    返回消息列表 [{user, recent, ts}...]。只读。

    2026-09-05 重构:入口从『死等底部消息Tab』改为『_ensure_msg_view 不死等』,
    兼容陌生来信页;进入何种消息视图由 _detect_msg_view 识别,日志可观测。
    """
    lst = _ensure_msg_view()
    if lst is None:
        print("[step] 未能进入消息视图,本轮无采集")
        return []
    _view = _detect_msg_view(lst)
    print(f"[step] 消息视图 = {_view}")
    convs = _extract_convs(lst)

    # 先在列表里固定本轮要读的目标清单(昵称),再去逐个进会话读取。
    # 2026-09-04 真机坑:运行中若来真实来电/系统弹窗抢占前台(曾把全局内容
    # 采成 短信/微信/电话/营业厅),后续 tap 点到拨号器里。→ 每个会话读取前
    # 先 _at_dm_list() 确认在消息列表、且前台确为小红书;漂移则从列表重锚。
    targets = []
    for cv in convs[:max_conv]:
        full = (cv.get("desc") or cv["text"] or "")
        nm, rest = _split_desc(full)         # 兼容 `，，`/`，` 分隔(陌生来信/私信列表)
        # 列表 desc 里昵称后的正文预览最后一个非空 field,即该会话最后可见消息
        preview = ""
        for s in reversed(rest):
            if s and s not in ("N条未读",) and not s.endswith("条未读"):
                preview = s
                break
        cc = cxmt(cv)
        if nm and cc:
            targets.append({"nick": nm, "cx": cc, "preview": preview})
    if not targets:
        print("[warn] 未解析到任一私信会话行")
        return []

    rows = []
    for t in targets:
        nm = t["nick"]
        # 会话级进入前,确保回到(或本就处于)DM 会话列表视图
        tree = _dm_list_tree()
        if tree is None:
            print(f"[skip] 未能锚定消息列表;跳过会话 {nm}")
            continue
        # 重查目标行(每次现查,避免列表滚动/漂移后旧坐标失准)
        row = _find_row_in(tree, nm)
        if not row:
            # 兜底:即使用列表预览也能出一条可分级记录(无来源归因)
            rows.append({"user": nm, "recent": t.get("preview", ""),
                         "context": t.get("preview", ""),
                         "src": "私信", "ts": now(), "note": "", "src_type": "",
                         "device": _device()})
            continue
        cc = cxmt(row)
        summary = _peek_conv_tail(cc, nm)
        rows.append({"user": nm,
                     "recent": summary.get("recent") or t.get("preview", ""),
                     "context": summary.get("context") or t.get("preview", ""),
                     "src": "私信", "ts": now(),
                     # 来源归因(进会话滚顶抓到的笔记标题 + 策略桶)
                     "note": summary.get("note") or "",
                     "src_type": summary.get("src_type") or "",
                     "device": _device()})
    return rows


def _has_conv_or_empty(tree):
    # 会话列表/来信页存在即可(即使空也算收敛,避免空跑恶意超时)
    dts = _dump_texts(tree)
    return (len(_extract_convs(tree)) > 0) or ("暂无会话" in dts) or \
           ("暂无" in dts) or ("陌生人" in dts) or ("来信" in dts)


def _dm_list_tree():
    """
    确保当前处于小红书『消息·私信会话列表』,返回该屏 tree;否则 None。
    处理漂移:
      * XHS 不在前台      → ensure_app_foreground() + 点底导航『消息』
      * 在某个会话正文里    → press back 退回列表
      * 在消息总览/列表    → 直接可用
      * 不在”消息“Tab     → 点底导航『消息』
    幂等,每会话进入前调用一次,把页面状态拉平为『消息列表』。
    """
    # 1) XHS 前台
    if not xhs_on_top():
        try:
            ensure_app_foreground()
        except Exception as e:
            print(f"[anchor] 前台化XHS失败:{e}")
            return None
        time.sleep(1.0)

    # 2) 若在会话正文(带输入『发消息…』) → back 退回列表
    for _ in range(3):
        tree = dump_current()
        dts = _dump_texts(tree)
        if _extract_convs(tree):            # 已是列表
            return tree
        if "发消息" in dts or "发消息…" in dts:   # 在会话正文 → 退
            d().press("back")
            time.sleep(1.0)
            continue
        # 3) 不确定状态:点底导航『消息』回列表底
        n = find_msg_tab(tree)
        if n and cxmt(n):
            d().click(*cxmt(n))
            time.sleep(1.2)
            continue
        break                               # 无法判断,跳出,返回 None 由上层跳过
    # 最后一查
    tree = dump_current()
    return tree if _extract_convs(tree) else None


def _go_back_to_list():
    """从会话正文退回到消息列表(与 _dm_list_tree 幂等拉齐状态)。"""
    d().press("back")
    time.sleep(1.0)
    _dm_list_tree()


def _find_row_in(tree, nick):
    """从 DM 列表 tree 里按首段昵称找会话行容器 bounds。兼容双逗号分隔。"""
    for cv in _extract_convs(tree):
        full = (cv.get("desc") or cv["text"] or "")
        nm, _ = _split_desc(full)
        if nm == nick:
            return cv
    return None


# 消息页里非"私信会话"的系统/互动容器前缀(点进去的不是留言,是通知/评论聚合页)
_NON_CONV_PREFIX = ("赞和收藏", "新增关注", "评论和@", "活动消息", "系统消息",
                    "关注", "粉丝", "消息通知", "私信")


def _split_desc(full):
    """把会话行 desc 按 昵称与后续 切开。兼容陌生来信/私信列表两种行格式。
    真实行形态(真机观察):
      私信列表 : `昵称，正文预览，日期`                (单逗号)
      陌生来信 : `昵称，，N条未读，正文预览，日期`      (先双逗号, 残段再单逗号)
    算法:先按 `，，` 切 → 昵称 + 残段;残段再按 `，` 切成字段列表。
    无 `，，` 时退化为整串按 `，` 切。
    返回 (昵称, 其余字段列表)。
    """
    full = full or ""
    if "，，" in full:
        first, rest = full.split("，，", 1)
        fields = [s for s in rest.split("，") if s != ""] if rest else []
        return first, fields
    if "，" in full:
        seg = full.split("，")
        return seg[0], [s for s in seg[1:] if s != ""]
    return full, []


def _is_conv_like(n):
    """判定一个 TextView/候选是否像『私信会话』。排除互动/系统容器块。"""
    blob = ((n.get("text") or "") + (n.get("desc") or ""))
    if not blob.strip():
        return False
    low = blob
    for p in _NON_CONV_PREFIX:
        if low.startswith(p):
            return False
    return True


def _extract_convs(tree):
    """
    从消息页 dump 里筛『私信会话』候选行容器。
    真实结构(2026-09-04 实测):消息总览页 = 『互动/系统容器块 + 私信会话容器行』
    合一的滚动列表。『会话行容器』特征:
      * 含整行 bounds 的可点容器,info 集中在 content-desc(非 TextView 叶子)
      * desc 形如 `昵称，，N条未读，正文预览，日期` / `昵称，，正文，日期`
      * 不含互动/系统块前缀
    返回这些容器节点(后续逐个 _peek_conv_tail 读取内部正文)。
    """
    h = d().window_size()
    y_top = h[1] * 0.10
    nav_y = h[1] * 0.88          # 底部导航(首页/市集/消息/我)以下略
    cands = []
    for n in tree.iter("node"):
        a = n.attrib
        dsc = a.get("content-desc", "").strip()
        if not dsc:
            continue
        if any(dsc.startswith(p) for p in _NON_CONV_PREFIX):
            continue
        c = cxmt({"bounds": a.get("bounds", "")})
        if not c:
            continue
        _, y = c
        if y < y_top or y >= nav_y:
            continue
        cands.append({"text": a.get("text", ""), "desc": dsc,
                      "class": a.get("class", ""),
                      "bounds": a.get("bounds", ""), "_y": y})
    # 同一会话可能父子容器同 desc → 保留最小 bounds(最顶层完整容器)去重
    cands.sort(key=lambda x: (x["_y"], len(x["bounds"])))
    seen, uniq = set(), []
    for n in cands:
        nm, _ = _split_desc(n["desc"])   # 以 desc 首段(昵称)去重,兼容双逗号
        if nm in seen or len(nm) < 1:
            continue
        seen.add(nm)
        uniq.append(n)
    return uniq


def _dump_texts(tree):
    out = []
    for n in tree.iter("node"):
        a = n.attrib
        if a.get("text") or a.get("content-desc"):
            out.append(a.get("text", "") + " " + a.get("content-desc", ""))
    return " | ".join(out)[:2000]


def _peek_conv_tail(cy, name):
    """
    点进会话,抓回:①来源笔记(note_thread 归因) ②最近可读正文(context)。
    2026-09-04 真机坑:抢前台来电/系统弹窗会让 _has_bubble 误判(拨号器也有
    文本 1/2/3/联系人/短信)。→ 采集帧必须同时满足:①XHS 前台 ②当前屏是会话
    (存在输入框区 input 锚点 或 出现返回+对端昵称顶栏),才可信。
    来源归因(README §9):来源卡在会话**顶部**(最新消息在底部,需上滑才能见),
    进会话先读尾(近消息),再**有界上滑**到顶/收敛来抓『来自笔记+标题』或
    首条笔记气泡标题;读完即回 DM 列表(下轮重进,不要求恢复底部位)。
    """
    # 点进会话前确认还在 XHS;不在则上层已 _at_dm_list 重锚,这里再兜底
    tap(*cy)
    try:
        deadline = time.time() + STEP_TIMEOUT
        tree = None
        while time.time() < deadline:
            if not xhs_on_top():
                break                       # 前台丢了,立刻中止,别采系统垃圾
            tree = dump_current()
            if _is_conv_open(tree, name):
                break
            time.sleep(POLL_STEP)
    finally:
        pass
    if tree is None or not xhs_on_top() or not _is_conv_open(tree, name):
        # 未能进入有效会话视图 → 回退到 DM 列表(由上层下一轮 _at_dm_list 重锚)
        _go_back_to_list()
        return {"recent": "", "context": "", "err": "not_in_conv",
                "note": "", "src_type": ""}

    # Phase 1 — 近消息正文(当前=底部最新屏)
    kept = _tail_txts(tree)

    # Phase 2 — 有界上滑找来源笔记卡(不回底部亦可,读完全部回列表)
    note, src_type, note_tree = _capture_src_note()

    _go_back_to_list()
    ctx = " / ".join(kept[-5:]) if kept else ""
    return {"recent": kept[-1] if kept else "",
            "context": ctx,
            # 若上滑过程扫到 note 标题也顺带并入 context 尾部作归因证据(去重)
            "note": note, "src_type": src_type}


# 上滑次数上限 / 单次滑动像素(来源卡若在超长历史顶部,控住别无限滚)
_SRC_SCROLL_MAX   = 6
_SRC_SCROLL_CHUNK = 900


def _capture_src_note():
    """
    当前已身处会话屏。有界上滑(最多 _SRC_SCROLL_MAX 次)扫描,从会话顶部区
    提取来源笔记标题 note + 策略桶 src_type。返回 (note, src_type, last_tree)。
    匹配优先级:
      1) 屏上出现平台标签『来自笔记』→ 取紧随其后的疑似标题文本(优先对端气泡名再扩);
         再用 KNOWN_NOTE_TITLES 在标签右侧文本/标题上命中。
      2) 直接在全屏可见文本里跑 KNOWN_NOTE_TITLES 关键词命中(覆盖『对端首条笔记卡
         标题』形态,霉霉/苹果)。
      3) 都无 → ('', '', 当前屏)。上滑收敛(两次内容相同)即提前停。
    """
    h = d().window_size()[1]
    y_top, y_bot = int(h * 0.28), int(h * 0.80)
    seen = set()
    for _ in range(_SRC_SCROLL_MAX):
        if not xhs_on_top():
            return ("", "", None)
        tree = dump_current()
        if tree is None:
            return ("", "", None)
        found = _src_note_from_tree(tree)
        if found:
            return (found[0], found[1], tree)
        sig = _screen_digest(tree)
        if sig in seen:                     # 已收敛(滚到顶/无新内容)
            return ("", "", tree)
        seen.add(sig)
        # 上滑 = 手指自下往上,让内容向下移露出更旧消息
        d().swipe(int(h * 0.5), y_bot, int(h * 0.5), y_top,
                  duration=0.25)
        time.sleep(0.6)
    return ("", "", None)


def _screen_digest(tree):
    """当前屏可见文本粗签,用于判断上滑是否收敛(内容是否已不动)。"""
    import re
    ts = [n for n in nodes_of(tree) if (n.get("text") or "").strip()]
    ts.sort(key=lambda n: _y(n))
    return "|".join((n.get("text") or "")[:14] for n in ts[-25:])


def _src_note_from_tree(tree):
    """
    在一帧屏树里找来源笔记。返回 (note, src_type) 或 None(未命中)。
    双路校验:
      * 直看全部文本:任一文本命中 KNOWN_NOTE_TITLES 关键词且非『账号名』噪声区
        (账号名=你的小红书昵称不在关键词列表内,本就无害) → 返回。
      * 平台卡形态『来自笔记』:找到该标签,再取其后物理下方(y 更大)最近的
        标题文本,用它命中表;标签本身旁若已含全文标题则直接命中。
    """
    texts = _ordered_text_nodes(tree)
    h = d().window_size()[1]
    # 来源卡只会出现在会话的**上部**(顶栏下方、首屏气泡区)。整个屏扫会误命中
    # 底部某个很长的正文里恰好含关键词(尤其己方笔记被转发复述)。故限定相对上限区:
    #   取全屏文本节点的中位 y 以下一律不算 → 只在屏上半区(上半屏内容)里找。
    upper_limit = h * 0.60
    texts_up = [nd for nd in texts if _y(nd) < upper_limit
                and (nd.get("text") or "").strip()]

    # 路1:屏上半区文本关键词命中(覆盖『对端/平台把来源笔记标题以首条卡片放上来』,
    #     言玉/霉霉/苹果形态;下方长正文复述不误吞)
    for kw in _KNOWN_MATCH_ORDER:
        for nd in texts_up:
            t = (nd.get("text") or "").strip()
            if kw in t and len(t) <= 60:     # 标题短;长正文如被分割仍能被容器文本命中
                canonical, st = KNOWN_NOTE_TITLES[kw]
                return (canonical, st)

    # 路2:『来自笔记』平台标签形态 → 命中其下(或同屏参照)具体标题。全屏找标签,
    #     取其下方最近的标题文本;标题多半在上半区(标签本身 y 必在上)。
    label_y = None
    label_x = None
    for nd in texts_up:
        if (nd.get("text") or "").strip() == _SRC_LABEL:
            cy_ = _y(nd)
            if label_y is None or cy_ < label_y:
                label_y, label_x = cy_, _x(nd)
    if label_y is not None:
        # 标签一般位于首帧上部;只在此标签下、且与其相邻(同屏首屏)的候选标题里找
        cands = [nd for nd in texts
                 if _y(nd) > label_y and (_y(nd) - label_y) < 140
                 and (nd.get("text") or "").strip()]
        cands.sort(key=_y)
        for nd in cands:
            t = (nd.get("text") or "").strip()
            for kw in _KNOWN_MATCH_ORDER:
                if kw in t and len(t) <= 60:
                    canonical, st = KNOWN_NOTE_TITLES[kw]
                    return (canonical, st)
        # 标签命中但没人认得该标题(新发的未登记笔记)→ 仍给『帖子入口』这个弱来源
        return (None, "")                    # 调用方会当 note=None 处理
    return None



# 会话屏里"非气泡正文"的 UI 残留(顶栏关注/橱窗、回复建议、输入区、系统时间分隔)
# 分两类:纯噪声(整串命中即整串扔掉) 与 reaction 快捷文案。用判别式过滤。
_TXT_NOISE = ("系统工具", "微信", "电话", "短信", "联系人", "营业厅",
              "通话", "呼入", "首页", "市集", "消息", "我", "发现",
              "搜索", "发送", "输入", "1", "2", "3", "4", "5", "6",
              "7", "8", "9", "0", "*", "#", "发消息", "发消息…",
              "加个关注，方便以后常聊", "关注", "逛橱窗", "逛逛橱窗",
              "已阅", "码住", "好家伙", "hello", "在干嘛", "喜欢", "呃",
              "谢谢宝", "谢谢", "好的", "在", "表情", "图片", "长按可发送",
              # 会话太长按动作单(空会话点开会弹出来)泄漏
              "删除对话", "拉黑", "举报", "置顶", "不感兴趣",
              "带我去", "想吃", "想买", "想去", "收藏", "笔记", "橱窗")
_TXT_NOISE_SET = set(_TXT_NOISE)

import re as _re
_TS_PAT = _re.compile(
    r"^\d{1,2}-\d{1,2}\s*\d{1,2}:\d{2}$"     # 08-13 01:01
    r"|^\d{1,2}:\d{2}(:\d{2})?$"              # 12:06 / 12:06:33
    r"|^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}$"       # 2025-12-24
    r"|^\d{1,2}月\d{1,2}号?$"                 # 08月16号
)
_MT_PAT  = _re.compile(r"^\d{1,3}(\.\d+)?\s*[m\"']?$")   # '100' / '32"' 贴纸残码


def _clean_txt(t):
    # 去 LRM/RLM 等隐形方向符 & 首尾空白
    t = (t or "").replace("\u200e", "").replace("\u200f", "").strip()
    # 空 / 超长(多为 app 整页复制) 直接丢弃
    if not t or len(t) > 160:
        return None
    # 纯时间戳 / 日期 / 贴纸残留码(32"、100) 直接丢弃
    if _TS_PAT.match(t) or _MT_PAT.match(t):
        return None
    # emoji/图片占位(如 [表情] [图片] [笔记] xx) 若整体极短且带方括号 → drop
    st = t.strip()
    if st.startswith("[") and st.endswith("]") and len(st) <= 25:
        return None
    # 整串拆分后各 token 都在噪声且极短 → 纯 chrome,整条 Drop
    toks = [s for s in t.replace("/", " ").split() if s]
    if toks and all(s in _TXT_NOISE_SET and len(s) <= 16 for s in toks):
        return None
    return t


def _tail_txts(tree):
    nodes = [n for n in nodes_of(tree)
             if _clean_txt(n["text"]) is not None]
    nodes.sort(key=lambda n: _y(n))
    vals = []
    for n in nodes[-18:]:                 # 尾部最近 18 条可见文本
        v = _clean_txt(n["text"])
        if v:
            vals.append(v)
    return vals


def _is_conv_open(tree, name):
    """
    判定当前屏是『私信会话正文』而非 DM 列表/总览页。
    会话正文区内一定有『输入框位』且多为竖排两列气泡;DM 列表则含会话行 row
    (desc 首段==某昵称 …，长度>1 )。判据(discriminant,不强求100%):
      * 必须 XHS 前台;
      * 屏上出现会话输入锚点(『发消息…』placeholder) ⇒ 已在会话;
      * 否则若屏上存在 >=1 条纯文本(非噪声)→ 保守当作会话(由上层滤波)。
    DM 列表的特征『无输入框且全是短行容器』由 _at_dm_list 区分。
    """
    if not xhs_on_top():
        return False
    txts = _dump_texts(tree)
    if "发消息" in txts or "发消息…" in txts:
        return True                          # 输入框就位 ⇒ 会话正文
    texts = [_clean_txt(n["text"]) for n in nodes_of(tree)]
    texts = [t for t in texts if t]
    return len(texts) >= 1


def _has_bubble(tree):
    # 会话对话里至少有文本 message(txt 非空非导航按钮)
    return any(n["text"] for n in nodes_of(tree) if len(n["text"]) > 1)


def _y(node):
    import re
    m = re.search(r"\[(\d+),(\d+)\]", node.get("bounds", ""))
    return int(m.group(2)) if m else 0


def _x(node):
    """bounds 左上 x(用于『来自笔记』标题在右的近似取同行判断)。"""
    import re
    m = re.search(r"\[(\d+),(\d+)\]", node.get("bounds", ""))
    return int(m.group(1)) if m else 0


def _ordered_text_nodes(tree):
    """屏上所有带文本 node,按 上→下 排好。只留含非空文本的纯 UI 文本节点。"""
    ns = [n for n in nodes_of(tree)
          if (n.get("text") or "").strip() and
          not (n.get("class") or "").endswith("Button")]  # 文本节点(去按钮文案混入)
    ns.sort(key=lambda n: (_y(n), _x(n)))
    return ns


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- #
# 5. 调用入口
# --------------------------------------------------------------------------- #
def _auto_learn():
    """采集完成后自动触达话术库自学习闭环(零人工)。

    采集器只管"抓真实私信"落 CSV; 这里在采集成功后, 立即调 collect_learn_samples
    抽取可学样本(导流技巧 lead_angle 优先 + 常规咨询) → reply_self_learn 三路择优
    → 星级 → 回灌叠加层。让话术库每采一轮就自动长一次肉。

    ★ 隔离: 开子进程跑 (非 import), 因为 reply_self_learn 会改 LLM 全局开关/写叠加层 YAML,
      且本进程持有 uiautomator2 连接; 子进程避免状态冲突, 失败静默(不阻断采集主流程)。
    """
    learn_py = os.path.join(ROOT, "classify", "collect_learn_samples.py")
    if not os.path.exists(learn_py):
        print("[learn] 跳过(collect_learn_samples.py 不存在)")
        return
    # 用与采集器相同的 python 解释器; 默认 --no-llm 走 stdlib+scene 兜底(秒到)+回灌叠加层,
    # 时效性优先(采集完立即反哺, 不被 LLM 链拖慢/挂住)。LLM 精修可另手动跑 collect_learn_samples.py。
    # 子进程隔离: reply_self_learn 会改 LLM 全局态/写叠加层 YAML, 且本进程持有 uiautomator2 连接。
    # 失败静默降级(不阻断采集主流程)。
    import subprocess, sys as _sys
    try:
        r = subprocess.run([_sys.executable, learn_py, "--no-llm"],
                           capture_output=True, text=True, timeout=120,
                           encoding="utf-8", errors="replace")
        out = (r.stdout or "").strip()[-3000:]
        print(f"[learn] 自动话术库闭环输出:\n{out}")
        if r.returncode != 0 and (r.stderr or "").strip():
            print(f"[learn] 闭环 stderr: {r.stderr.strip()[-800:]}")
    except subprocess.TimeoutExpired:
        print("[learn] 话术库闭环超时(>120s), 已放弃, 不影响采集结果")
    except Exception as e:
        print(f"[learn] 自动闭环异常: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", default=True,
                    help="只跑一轮消息轮回")
    ap.add_argument("--watch", type=int, default=0,
                    help="若>0,连续 watch N 分钟(0=单轮)")
    ap.add_argument("--device", default="",
                    help="设备序列号/adb序号(如 YOUR_DEVICE_SERIAL)。优先级最高; 缺省读环境变量 XHS_DEVICE, "
                         "再缺省读 lanes.yaml 唯一序列号, 最后落'默认设备'")
    a = ap.parse_args()
    set_cli_device(a.device)

    def round_once():
        try:
            ensure_app_foreground()
            rows = collect_a_round(max_conv=25)
            snap = {"ts": now(), "rows": rows}
            with open(JSON_LOG, "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False, indent=2)
            _append_csv(rows)
            print(f"[done] 本轮采到 {len(rows)} 条会话 → {CSV_OUT}")
            # ⭐ 自动触发 话术库自学习闭环(2026-09-06 宇豪拍板"自动触发")
            # 采集到的真实私信 → classify/collect_learn_samples.py 抽导流/咨询样本 → reply_self_learn
            # 用子进程隔离(模块 import 会动 reply_self_learn 的 LLM 全局态, 且写叠加层, 不污染本采集进程)
            _auto_learn()
            return True
        except TimeoutError:
            print("[error] 采集超时护栏触发;按 GELab 重开 App 清屏再试")
            try:
                d().app_stop(PKG)
            except Exception:
                pass
            return False

    if a.watch > 0:
        stop = time.time() + a.watch * 60
        ok = True
        while ok and time.time() < stop:
            ok = round_once()
            time.sleep(5)
    else:
        round_once()


def _append_csv(rows):
    new_ = not os.path.exists(CSV_OUT)
    # 兼容旧数据: 若已存在旧表头(无 device 列) → 追加 device 列
    cols = ["ts", "source", "user", "recent_text", "context",
            "note_thread", "src_type", "device"]
    with open(CSV_OUT, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if new_ or not _csv_has_col(CSV_OUT, "device"):
            w.writeheader()
        for r in rows:
            w.writerow({"ts": r["ts"], "source": r["src"], "user": r["user"],
                        "recent_text": r["recent"], "context": r["context"],
                        "note_thread": (r.get("note") or ""),
                        "src_type": (r.get("src_type") or ""),
                        "device": (r.get("device") or "默认设备")})


def _csv_has_col(fp, col):
    """判断 CSV 是否已含某列(靠表头)。读不到/表头缺失 → False(让上层重写表头)。"""
    try:
        with open(fp, encoding="utf-8-sig", newline="") as f:
            head = f.readline().strip()
        return col in [c.strip() for c in head.split(",")]
    except Exception:
        return False


if __name__ == "__main__":
    main()
