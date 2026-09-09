#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小红书「人工值守」Console · 本地服务
--------------------------------------------------
把散在 out/ 下的三张 csv(分级/回流/评论)合并成一个「访客看板」,
浏览器打开 dashboard.html, 真人一眼看全: 谁来了 / 怎么回 / 回了没。

 - 只读分类表/评论表, 只写「回流表」(leads_followup.csv)。
 - 自动拟稿复用在 classify_draft.py 的规则 + intent_map.yaml, 并做"对话阶段感知"修正。
 - 半自动值守预留: action=auto 的低风险模板条目标记「可auto-代发」, 高商机 P0/P1 标「须真人」。

依赖: 仅 Python 标准库 + PyYAML(用于加载 intent_map)。
启动:  python server.py   →  http://127.0.0.1:8090
"""
from __future__ import annotations

import csv, io, json, os, re, sys, threading, webbrowser, argparse, subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

HERE   = os.path.join(os.path.dirname(os.path.abspath(__file__)))
ROOT   = os.path.dirname(HERE)                      # interceptor/
OUT    = os.path.join(ROOT, "out")
CLS    = os.path.join(OUT, "leads_classified.csv")   # 分级+拟稿(只读引用,不写)
FOL    = os.path.join(OUT, "leads_followup.csv")     # 回流表(唯一可写)
CMT    = os.path.join(OUT, "leads_comments.csv")     # 评论区(只读引用)
YAML_FP = os.path.join(ROOT, "classify", "intent_map.yaml")
STRATEGY_FP = os.path.join(HERE, "strategy.yaml")   # 承接策略引擎(自动/人工分流规则)
LANES_FP = os.path.join(HERE, "lanes.yaml")          # 赛道调度 + 设备账号映射(中央配置)
PROACTIVE_LOG = os.path.join(OUT, "proactive_send_log.json")   # 主动获客日志流水
PROACTIVE_YAML = os.path.join(ROOT, "classify", "proactive_targets.yaml")  # 主动获客策略
DASH   = os.path.join(HERE, "dashboard.html")

_FOL_COLS = ["hs", "user", "来源笔记", "来源类型", "tier", "意图", "action",
             "拟稿(自动生成)", "已回?", "对方反应", "真商机?", "备注", "device"]
_WRITE_LOCK = threading.Lock()

# ---------------------------------------------------------------- 主动获客运行状态
#   _proactive_run 用 Popen 后台拉起 proactive_engage.py, 把其 stdout 追加到
#   PROACTIVE_RUN_LOG, 并维护 _RUN_STATE 供 /api/proactive/status 轮询。
#   这样"点执行一轮"后, 前端能看到实时阶段进度(不再"点了没反应")。
PROACTIVE_RUN_LOG = os.path.join(OUT, "proactive_run.log")          # 本轮实时进度(追加写)
PROACTIVE_RUN_STATE = os.path.join(OUT, "proactive_run_state.json")  # 运行状态快照(供前端)
_RUN_LOCK = threading.Lock()
# 内存镜像: {pid, started, phase, progress, lines, done, ok, note, error, mode, kw, maxn, send}
_RUN_STATE = {}


# ---------------------------------------------------------------- 运行器 python
#   主动获客 / 自动派发都依赖 uiautomator2, 而它只在 venv 里装了。
#   server.py 自身(仅标准库+PyYAML)用任何 python 都能起, 但若用系统 python 拉起
#   子进程 → 子进程报 ModuleNotFoundError: uiautomator2。
#   这里固定探测"带 uiautomator2 的 python", 找不到才回退 sys.executable。
def _runner_python():
    # 1) 环境变量显式指定(最高优先)
    env_py = os.environ.get("XHS_RUNNER_PY", "")
    if env_py and os.path.exists(env_py):
        return env_py
    # 2) 本项目已知 venv(已装 uiautomator2 + yaml)
    candidates = [
        # Windows venv
        r"C:\Users\39705\.workbuddy\binaries\python\envs\default\Scripts\python.exe",
        # 项目内 venv
        os.path.join(ROOT, ".venv", "Scripts", "python.exe"),
        os.path.join(HERE, ".venv", "Scripts", "python.exe"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    # 3) 找不到 → 回退当前 server python(可能没有 uiautomator2, 但至少不让 Popen 崩溃)
    return sys.executable or "python"


# ---------------------------------------------------------------- 读 csv
def _read(fp):
    if not os.path.exists(fp):
        return []
    try:
        with open(fp, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        return [r for r in rows if r]
    except Exception as e:
        return []


def _load_intents():
    try:
        import yaml
        d = _track_yaml("intent_map.yaml")
        return (d or {}).get("intent", [])
    except Exception as e:
        return []


def _track_yaml(basename, default_dir=None):
    """按当前赛道读取配置(默认赛道行为不变; 仅赛道有差异文件时叠加)。"""
    try:
        from config_track import load_yaml_for_track
        return load_yaml_for_track(basename, default_dir)
    except Exception:
        # 兜底: 直接读默认目录
        import yaml
        ddir = default_dir or os.path.dirname(YAML_FP)
        fp = os.path.join(ddir, basename)
        if os.path.exists(fp):
            with open(fp, encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        return {}


def _load_strategy():
    """读取承接策略引擎规则(自动/人工分流)。文件缺失/异常 → 返回安全默认(全人工)。
    注意: 承接分流(自动/人工/忽略)是【全局统一】, 不随赛道变(内容价值决定是否回)。"""
    try:
        import yaml
        with open(STRATEGY_FP, encoding="utf-8") as f:
            d = yaml.safe_load(f) or {}
        return d
    except Exception as e:
        return {"rules": [], "global": {"auto_enabled": False}}


def _route_for(intent, tier, action, strategy):
    """按策略规则给一条客户算 route(auto/manual/ignore)。
    支持值班模式 auto_mode:
      semi  = 半自动, follow rules, auto_safe 才自动; manual/ignore 维持(默认)
      full  = 全自动, 除 ignore 外全部自动(auto_safe 放宽, 涉价/做研学也自动回)
      manual= 全人工, 一切转人工, 任何自动都停
    规则按优先级扫描 intent_contains(tier/action 匹配)。无命中 → 按 classify 的 action 兜底。"""
    rules = (strategy or {}).get("rules", []) or []
    g = (strategy or {}).get("global", {}) or {}
    auto_on = bool(g.get("auto_enabled", True))
    mode = (g.get("auto_mode") or "semi").lower()
    it = (intent or "")
    # 规则命中(意图子串匹配, 支持 | 表示或)
    for r in rules:
        needle = r.get("intent_contains", "")
        if not needle:
            continue
        hit = False
        for seg in needle.split("|"):
            if seg and seg in it:
                hit = True
                break
        if not hit:
            continue
        # tier/action 条件
        rt = r.get("tier")
        ra = r.get("action")
        if rt and rt != tier:
            continue
        if ra and ra != action:
            continue
        route = r.get("route", "manual")
        safe = bool(r.get("auto_safe", False))
        # ---- 值班模式改写 ----
        if mode == "full":
            # 全自动: 除 ignore 外全置 auto
            if route != "ignore":
                route, safe = "auto", True
        elif mode == "manual":
            # 全人工: 一切转 manual, auto 停
            route = "manual"
            safe = False
        else:
            # semi(默认): 总开关关则 auto 强制人工
            if not auto_on and route == "auto":
                route, safe = "manual", False
        return {
            "route": route,
            "auto_safe": safe,
            "route_note": r.get("note", ""),
        }
    # 兜底: 按 classify action
    fb = {
        "auto":  "auto", "human": "manual", "ignore": "ignore",
    }.get(action, "manual")
    if mode == "full":
        if fb != "ignore":
            fb = "auto"
    elif mode == "manual":
        fb = "manual"
    else:
        if not auto_on and fb == "auto":
            fb = "manual"
    return {"route": fb, "auto_safe": (fb == "auto" and mode != "manual"),
            "route_note": {"auto": "按默认白名单自动", "manual": "按默认人工", "ignore": "忽略"}.get(fb, "")}


def _norm(s):
    return (s or "").strip()


def _latest_dm_rows(cls_rows):
    """同一 user 只取最新一条(按 ts 排序取最后), 供看板展示"当下最需回应"。"""
    by = {}
    for r in cls_rows:
        u = _norm(r.get("user"))
        if not u:
            continue
        by.setdefault(u, []).append(r)
    out = []
    for u, rs in by.items():
        rs.sort(key=lambda x: x.get("ts") or "")
        latest = rs[-1].copy()
        latest["_msg_count"] = len(rs)
        latest["_msgs"] = rs
        out.append(latest)
    return out


def _followup_map(fol_rows):
    """回流表: 一 user 一条最新结论。"""
    m = {}
    for r in fol_rows:
        u = _norm(r.get("user"))
        if not u:
            continue
        m[u] = r
    return m


# ---------------------------------------------------------------- 对话阶段感知拟稿修正
_STAGE_CUE = {   # 命中即视为已进入「承接/价格」阶段, 不再问背景
    "直接接单":   "承接",  "能直接接单": "承接", "直接接单吗": "承接",
    "多少钱":     "价格",  "多少钱一单": "价格", "价格": "价格",
    "怎么收费":   "价格", "一单多少钱": "价格",
    "报名":       "承接", "怎么报名": "承接", "想报名": "承接",
}


def _detect_stage(text):
    if not text:
        return None
    t = _norm(text)
    pool = t  # 直接用最后一次留言判断
    for cue, st in _STAGE_CUE.items():
        if cue in pool:
            return st
    # 兜底: 含价格/接单/报名意向词也视为承接
    if re.search(r"(接单|线下|约一下|怎么联系|加你|我发你)", pool):
        return "承接"
    return None


def _stage_draft(it_name, stage, user, recent):
    """按对话阶段返回更贴的承接话术(不再重复试探背景)。"""
    if stage == "价格":
        return "咱们初阶班是 1499 起(早鸟有优惠),面向研学导游/地接的 AI 落地实操,不堆理论、一步一步带着做。" \
               "您方便的话我同学长跟您约个时间,给您看下具体任务怎么接、价格怎么算——比在这儿打字快得多。"
    if stage == "承接":
        return "太好了,那咱们直接往落地上谈:您手头有没有已经在跑的项目/团队?我同学长跟您约个时间," \
               "把怎么接单、怎么派活、结算怎么走一次跟您讲清楚——线下聊不绕弯。"
    # 没识别到明确阶段 → 沿用规则稿(模板首句), 不硬造
    return None


def _auto_draft_intents(user, recent, it=None):
    """返回 (意图对象, 阶段化修正后拟稿)。复用 intent_map 规则。"""
    intents = _load_intents()
    if not intents:
        return None, ""
    judge = f"{_norm(recent)} "
    # 复用 classify 命中逻辑
    target = None
    for it in intents:
        for kw in it.get("keywords", []):
            if kw and kw in judge:
                target = it
                break
        if target:
            break
    if not target:
        target = intents[-1]
    it_name = target.get("name", "")
    # 阶段感知: 若命中承接/价格 cue, 且当前意图落在"泛咨询/问背景"族, 则给承接话术
    stage = _detect_stage(recent)
    if stage and it_name in ("问怎么做/还招吗/这是干嘛", "泛研学人群|感兴趣/想了解", "其他/待分类"):
        dk = _stage_draft(it_name, stage, user, recent)
        if dk:
            return target, dk
    # 规则稿
    dk = target.get("draft", "")
    if user and dk and "〔昵称〕" in dk:
        dk = dk.replace("〔昵称〕", user)
    return target, dk


# ---------------------------------------------------------------- 看板数据组装
def _load_proactive_log():
    """读主动获客日志流水(proactive_send_log.json)。文件缺失/异常 → []。"""
    if not os.path.exists(PROACTIVE_LOG):
        return []
    try:
        with open(PROACTIVE_LOG, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception:
        return []


# ------------------------------------------------------------------ #
# 评论区建议回复生成(公开区合规): 用 reply_diagnose+reply_strategy+_pass_red(public)
# 给每条评论生成一条"建议你回什么"。只走规则兜底(秒级, 不依赖LLM网络),
# 且必须过 _PUBLIC_RED(公开区最严: 不报价/不导流/不卖课/不绝对化)。
# → 让评论区 Tab 不再是"只有原始评论", 而是"我能看懂对方说啥+该回啥"。
_CMT_REPLY_CACHE = {}   # body -> reply(进程内缓存, 避免重复计算)
def _comment_reply(body, src_type=""):
    try:
        import sys
        _cp = os.path.join(ROOT, "classify")
        for _m in ("reply_diagnose", "reply_strategy", "reply_compose"):
            if _cp not in sys.path:
                sys.path.insert(0, _cp)
        from reply_diagnose import diagnose
        from reply_strategy import match_strategy
        from reply_compose import _pass_red
        key = (body, src_type)
        if key in _CMT_REPLY_CACHE:
            return _CMT_REPLY_CACHE[key]
        # —— 噪声评论: 不值得给一条"自我介绍式"建议, 免打脸（应直接忽略/轻互动）——
        b = (body or "").strip()
        # 纯数字/一串111/哈哈/表情/纯点赞收藏等无信息量 → 不给推销式建议
        import re as _re
        if (not b) or _re.fullmatch(r'[\d\s]+', b) or len(b) <= 1 \
           or _re.fullmatch(r'[😀-🙏\u4e00-\u9fa5]*(哈哈哈|哈哈|666|111|000|沙发|打卡|路过|支持|顶)[^.。!？?\n]{0,4}', b) \
           or ('点赞' in b and '收藏' in b and '回来' in b):
            _CMT_REPLY_CACHE[key] = ""
            return ""
        # —— 无关人群/咖啡探店类(误采到别的笔记下的路人): 明确标"不用回", 别再套研学话术 ——
        #   征象: 评论里只有吃喝/探店/品牌/地点/等生活向词, 无任何 AI/研学/接单/工具意向
        _IRR_WORDS = ("咖啡", "探店", "味道", "好喝", "甜", "性价比", "玫瑰", "色色",
                      "店面", "店名", "好甜", "喝过", "奶茶", "甜品", "装修", "氛围",
                      "三圣乡", "万象城", "太古里", "网红店", "值得去", "打卡过", "入口", "点单")
        _BIZ_WORDS = ("AI", "研学", "导游", "带团", "地接", "接单", "变现", "提效", "技能",
                      "工具", "课程", "认证", "培训", "ai", "aigc", "AIGC", "学习", "副业")
        if _re.search(r'|'.join(map(_re.escape, _IRR_WORDS)), b) \
           and not _re.search(r'|'.join(map(_re.escape, _BIZ_WORDS)), b):
            _CMT_REPLY_CACHE[key] = ""
            return ""
        # 来源提示: 按赛道给话术视角(研学/OPC/AI培训)
        hint = "xueyan_ai"
        if "OPC" in (src_type or "") or "接单" in (src_type or ""):
            hint = "opc_recruit"
        elif "培训" in (src_type or "") or "认证" in (src_type or ""):
            hint = "ai_training"
        diag = diagnose(body, "", src_type, source_hint=hint)
        strat = match_strategy(diag)
        lines = [(l, _pass_red(l, channel="public")) for l in (strat.get("lines") or [])]
        lines = [l for l, ok in lines if ok]
        reply = ""
        if lines:
            # 打分挑选"更贴对方原话"的那条, 而非一律取第一条通用介绍:
            #   +3 句式里含对方情绪/场景词(焦虑/累/失眠/经验/带团/接单/0基础/六年等)
            #   +2 反过来引用对方(您/你 + 目前/已经/是这样) → 更像"接住他说话"
            ec = _re.search(r'焦虑|累|失眠|赔|担心|怕|零基础|0基础|经验|六年|八年|带团|地接|接单|学过|小白|想学|怎么做|咋做|具体|多少|报名', body or '', _re.I)
            def _score(l):
                s = 0
                if ec:
                    # 该候选是否包含跟对方同主题的回应词
                    if _re.search(ec.group(0), l, _re.I):
                        s += 3
                if _re.search(r'您|你', l):
                    s += 1
                if _re.search(r'目前|已经|是这样|您说|您这|您做|您手头|是不是', l):
                    s += 1
                # 太长(>70)扣分, 评论区短句更友好
                if len(l) > 70:
                    s -= 1
                return s
            scored = sorted(lines, key=_score, reverse=True)
            reply = scored[0]
        # 无合规候选 → 给一条"公开区安全提问"兜底(只种草/提问, 不导流不卖课)
        if not reply:
            reply = "你这个想法挺实在的——你目前是已经在带团/做研学, 还是刚想入门? 可以多聊聊~"
        _CMT_REPLY_CACHE[key] = reply
        return reply
    except Exception:
        return ""


def _read_proactive_yaml():
    """读 proactive_targets.yaml(主动获客策略)。缺失/异常 → 空结构。
    ★ 正交拆分: 主动获客 = 【全局稳定模块】, 不再按赛道叠加。
    targets 已全量并入一份全局文件(每个 target 带 track 标签),
    赛道切换只在前端过滤展示当前赛道人群, 底层数据不变。
    读/写/执行三处统一读本全局文件(proactive_plan.load_targets 同源),
    消除原来「读按赛道、写/跑用全局」的漂移。"""
    try:
        import yaml
        with open(PROACTIVE_YAML, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {"targets": {}, "global": {}}


def _proactive_stats(log):
    """从获客流水算每日看板统计(按天聚合)。已知类型字典驱动, 未知类型(如 manual_check/skip)也计入 total。"""
    import datetime
    by_day = {}
    today = datetime.date.today().isoformat()
    for x in log:
        day = (x.get("ts") or "")[:10] or today
        s = by_day.setdefault(day, {"day": day, "send": 0, "hit": 0, "fail": 0,
                                    "rate_block": 0, "no_target": 0, "skip": 0,
                                    "manual_check": 0, "total": 0})
        t = x.get("type", "")
        s["total"] += 1
        if t in s:
            s[t] += 1
    # 按天倒序
    days = sorted(by_day.values(), key=lambda d: d["day"], reverse=True)
    return {"days": days, "today": by_day.get(today, {"day": today, "send": 0, "hit": 0,
                                                      "fail": 0, "rate_block": 0, "no_target": 0,
                                                      "skip": 0, "manual_check": 0, "total": 0})}


# ---------------------------------------------------------------- 主动获客运行状态追踪
#   让"点执行一轮"后前端能看到实时进度(不再只回 one-shot started)。
#   _proactive_run 启动 Popen → 把 stdout 追加 PROACTIVE_RUN_LOG;
#   派生线程读该文件尾部, 解析出"阶段"(搜索/进笔记/生成/发送/完成)写回 _RUN_STATE。
#   /api/proactive/status 直接读 _RUN_STATE + PROACTIVE_RUN_LOG 尾部返回。

import socket as _socket  # noqa: E402  (局部用, 避免顶部污染)

_PROACTIVE_STAGES = [
    ("搜索页", ("未能进入搜索页", "进入搜索页", "搜索", "输入搜索", "搜索输入", "搜索框")),
    ("搜关键词", ("关键词=", "搜索结果命中", "搜索输入失败", "去搜索", "输入")),
    ("命中人群", ("命中目标", "命中 ", "目标=", "自动匹配", "入池", "匹配")),
    ("进笔记/留言", ("进入「", "进入笔记", "评论区", "留了", "已留言", "已生成", "发送", "输入框", "说点什么")),
    ("结束", ("本轮共处理", "停止", "完成", "频控] 停止", "频控] 通过", "未能进入")),
]


def _reset_run_state():
    global _RUN_STATE
    with _RUN_LOCK:
        _RUN_STATE = {}


def _proactive_status():
    """返回当前主动获客运行状态 + 实时进度日志(最近 N 行)。"""
    with _RUN_LOCK:
        st = dict(_RUN_STATE)
    # 读进度日志尾部(最近 40 行), 叠加到状态供前端展示
    lines = []
    try:
        if os.path.exists(PROACTIVE_RUN_LOG):
            with open(PROACTIVE_RUN_LOG, encoding="utf-8", errors="replace") as f:
                all_lines = [ln.rstrip("\n") for ln in f if ln.strip()]
            lines = all_lines[-40:]
    except Exception:
        lines = []
    st["run_log"] = lines
    if not st:
        st["running"] = False
    return st


def _set_run_phase(lines):
    """从最新几行日志推断阶段(关键词匹配), 更新 _RUN_STATE['phase']/['progress']。"""
    if not lines:
        return
    joined = "\n".join(lines[-6:])
    with _RUN_LOCK:
        for name, kws in _PROACTIVE_STAGES:
            if any(k in joined for k in kws):
                if _RUN_STATE.get("phase") != name:
                    _RUN_STATE["phase"] = name
                    _RUN_STATE["progress"] = f"{_RUN_STATE.get('progress','')}\n→ {name}"
                break
        # 判定结束/失败
        if any(k in joined for k in ("本轮共处理", "未能进入", "停止", "搜索输入失败", "Traceback", "Error")):
            _RUN_STATE["done"] = True
            _RUN_STATE["ok"] = not any(k in joined for k in ("未能进入", "搜索输入失败", "Traceback", "Error"))
            _RUN_STATE["error"] = ("执行异常" if any(k in joined for k in ("Traceback", "Error")) else
                                   ("进入搜索页失败/搜索输入失败" if any(k in joined for k in ("未能进入", "搜索输入失败")) else ""))


def _watch_run(proc, log_fp):
    """后台线程: 等子进程结束, 期间滚动更新 _RUN_STATE, 最后落盘状态文件。"""
    import datetime as _dt
    try:
        proc.wait(timeout=3600)  # 兜底: 最多 1 小时
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    # 结束后再读一遍日志尾部
    lines = []
    try:
        with open(log_fp, encoding="utf-8", errors="replace") as f:
            lines = [ln.rstrip("\n") for ln in f if ln.strip()]
    except Exception:
        pass
    tail = "\n".join(lines[-40:])
    with _RUN_LOCK:
        _RUN_STATE["done"] = True
        _RUN_STATE["ok"] = (proc.returncode == 0) and not any(k in tail for k in ("Traceback", "Error"))
        _RUN_STATE["phase"] = "完成" if _RUN_STATE.get("ok") else "结束(异常)"
        _RUN_STATE["progress"] = (_RUN_STATE.get("progress", "") + "\n→ " + _RUN_STATE["phase"]).strip()
        if not _RUN_STATE.get("ok"):
            _RUN_STATE.setdefault("error", "执行异常, 见 run_log")
        _RUN_STATE["finished_ts"] = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        snap = dict(_RUN_STATE)
    try:
        with open(PROACTIVE_RUN_STATE, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return snap


def _track_state(device=None):
    """账号×赛道信息。
    ★ 账号驱动: 赛道 = 当前账号的 lane(不再有全局 active)。
    device 传入 → 按该账号推赛道; 不传 → 回退第一个账号/全局兜底。
    返回: {active, tracks:[{id,name,desc}], account:{id,nick,lane,desc}}。"""
    try:
        from config_track import get_active_track, list_tracks, account_meta, list_accounts
        active = get_active_track(device)
        tr = list_tracks()
        acc = account_meta(device)
        acc_extra = list_accounts()
        # 把账号对象里除配置字段外的都带上
        acc_full = acc_extra.get(acc.get("id"), {})
        return {
            "active": active,                     # 当前账号推导出的赛道
            "tracks": [{"id": k, "name": v.get("name", k), "desc": v.get("desc", "")}
                       for k, v in tr.items()],
            "account": {**acc, **{k: v for k, v in acc_full.items() if k not in ("nick", "lane", "desc")}},
        }
    except Exception:
        return {"active": "yanxue", "tracks": [], "account": {"id": "默认设备", "nick": "默认账号", "lane": "yanxue", "desc": ""}}


def _accounts_state():
    """账号清单(含赛道): [{id,nick,lane,lane_name,desc,serial}...]。
    供前端「看哪台手机」下拉 + 账号定位显示。"""
    try:
        from config_track import list_accounts, list_tracks
        accs = list_accounts()
        tm = list_tracks()
        out = []
        for did, meta in accs.items():
            if isinstance(meta, str):
                meta = {"nick": meta}
            lane = meta.get("lane") or "yanxue"
            out.append({
                "id": did, "serial": did,
                "nick": meta.get("nick", did),
                "lane": lane, "lane_name": tm.get(lane, {}).get("name", lane),
                "desc": meta.get("desc", ""),
            })
        return out
    except Exception:
        return []


def build_state():
    cls  = _read(CLS)
    fol  = _read(FOL)
    cmt  = _read(CMT)
    fmap = _followup_map(fol)
    strategy = _load_strategy()
    g = (strategy or {}).get("global", {}) or {}
    auto_on = bool(g.get("auto_enabled", True))

    dm_rows = []
    for r in _latest_dm_rows(cls):
        u = _norm(r.get("user"))
        f = fmap.get(u, {})
        tier = (r.get("tier") or "").upper().replace("P", "P") or "P3"
        intent = r.get("意图", "")
        action = r.get("action", "human")
        rt = _route_for(intent, tier, action, strategy)
        dm_rows.append({
            "ts": r.get("ts", ""),
            "user": u,
            "src": r.get("src", ""),
            "留言": r.get("留言", ""),
            "来源笔记": r.get("来源笔记", ""),
            "来源类型": r.get("来源类型", ""),
            "tier": tier,
            "意图": intent,
            "action": action,
            "状态": r.get("状态", "待真人"),
            "拟稿": r.get("拟稿", ""),
            "msg_count": r.get("_msg_count", 1),
            # 完整对话历史(同用户多条, 供前端展开看上下文)
            "_msgs": [{"ts": (m.get("ts") or ""), "留言": (m.get("留言") or "")}
                      for m in (r.get("_msgs") or [])],
            # 多设备标签: 透传采集/分级层的 device; 无 → 默认设备
            "device": r.get("device") or "默认设备",
            # 策略引擎分流(自动/人工/忽略)
            "route": rt["route"],
            "auto_safe": rt["auto_safe"],
            "route_note": rt["route_note"],
            # 回流侧(真人已填的结论, 优先展示)
            "已回": f.get("已回?", "") or "",
            "对方反应": f.get("对方反应", "") or "",
            "真商机": f.get("真商机?", "") or "",
            "备注": f.get("备注", "") or "",
            "followup_ts": f.get("hs", "") or "",
        })

    # 评论区: 逐条展开(含已回复的)
    # 说明: 评论聚合里 action 区分 评论了你的笔记 / 回复了你的评论
    cmt_rows = []
    for r in cmt:
        u = _norm(r.get("user"))
        if not u:
            continue
        body = _norm(r.get("body"))
        if not body:
            body = "(纯图/空评论)"
        cmt_rows.append({
            "ts": r.get("ts", ""), "user": u, "action": r.get("action", ""),
            "body": body, "note": r.get("note", "") or "", "src_type": r.get("src_type", "") or "",
            "stat": r.get("stat", "") or "",
            # 建议回复(公开区合规, 规则兜底): 让评论区 Tab 能直接看到"该回啥"
            "suggest": _comment_reply(body, r.get("src_type", "") or ""),
        })

    # ---- 汇总
    #   已回 判定: 回流表"已回?" 字段为 "已回" 或 "已自动回"(系统自动发过也算已回)
    #   → 避免「自动分流→已自动发」的线索还挂待办, 也防重复派发
    _REPLIED = ("已回", "已自动回")
    dm_pending = [r for r in dm_rows if r["已回"] not in _REPLIED]
    cnt = {"P0": 0, "P1": 0, "P2": 0, "P3": 0}
    for r in dm_pending:
        t = (r["tier"] or "P3").upper()
        cnt[t] = cnt.get(t, 0) + 1
    rcount = {"auto": 0, "manual": 0, "ignore": 0}
    for r in dm_pending:
        rcount[r.get("route", "manual")] = rcount.get(r.get("route", "manual"), 0) + 1

    # ---- 多设备: 按 device 分组(仅统计待跟进; 已回流的不占待办)
    #   device 显示为小红书账号昵称(通过 lanes.yaml accounts 映射), 避免只看 adb 序列号认错设备
    try:
        from config_track import device_name
        _dn = device_name
    except Exception:
        _dn = lambda x: x
    dev_map = {}   # device -> {pending, route_auto, route_manual, device}
    for r in dm_pending:
        dv = r.get("device") or "默认设备"
        d = dev_map.setdefault(dv, {"device": dv, "pending": 0,
                                    "route_auto": 0, "route_manual": 0})
        d["pending"] += 1
        if r.get("route") == "auto":
            d["route_auto"] += 1
        else:
            d["route_manual"] += 1
    # 已回流(记过一笔)的也挂到对应 device, 便于看该设备全貌
    for r in dm_rows:
        if r["已回"] in _REPLIED:
            dv = r.get("device") or "默认设备"
            d = dev_map.setdefault(dv, {"device": dv, "pending": 0,
                                        "route_auto": 0, "route_manual": 0})
    devices = [dev_map[k] for k in sorted(dev_map.keys())]
    # 补账号昵称字段(dashboard 显示账号名, 序列号作副标题/兜底)
    for d in devices:
        raw = d.get("device") or "默认设备"
        d["nick"] = _dn(raw)
        d["serial"] = raw

    # ---- 合规提示(平台 2026-07-17 治理公告口径: AI辅助✅ / AI全权托管❌) ----
    #   只提示, 不阻断(宇豪保留全自动风险自担; 此处让真人明确知道当前自动回范围与风险)
    mode_now = (g.get("auto_mode") or "semi").lower()
    compliance = {
        "mode": mode_now,
        "human_in_loop": True,                                   # 架构上关键回复须真人发
        "auto_reply_count": rcount["auto"],                      # 当前会被系统自动回的数量
        "auto_risk": "high" if mode_now == "full" else ("menu" if mode_now == "semi" else "none"),
        # 平台判定"AI托管号"的警示: 仅全自动(脚本替代真人自动发私信)命中该画像
        "warn": ("当前为【全自动】: 系统会脚本式自动回私信。按平台2026-07-17治理公告, "
                 "『AI/脚本批量自动发私信』属违规画像(AI托管号)——你已选择保留, 风险自担。"
                 "建议日常切【半自动】: 只自动回资料/干货, 关键回复(涉价/求对接/加微)真人发。"
                 if mode_now == "full" else
                 ("当前为【半自动】: 只自动回资料/干货等安全消息, 涉价/求对接/加微留真人——符合平台"
                  "『AI辅助』口径, 是推荐状态。" if mode_now == "semi" else
                  "当前为【全人工】: 所有私信都真人回, 系统不自动发——最安全, 适合起号阶段把控节奏。")),
    }

    return {
        "summary": {
            "pending_total": len(dm_pending), **cnt,
            "replied_total": sum(1 for r in dm_rows if r["已回"] in _REPLIED),
            "auto_ok": sum(1 for r in dm_pending if r["action"] == "auto"),
            "route_auto": rcount["auto"],
            "route_manual": rcount["manual"],
            "route_ignore": rcount["ignore"],
            "auto_enabled": auto_on,
            "auto_mode": g.get("auto_mode") or "semi",
        },
        "compliance": compliance,    # 合规提示: 当前模式 + 自动回范围 + 平台画像风险
        "track": _track_state(),     # 账号×赛道: 当前账号 + 赛道清单(账号驱动)
        "accounts": _accounts_state(),  # 账号清单: [{id,nick,lane,lane_name,desc,pending}...] 供账号下拉
        "devices": devices,          # 多设备: [{device,pending,route_auto,route_manual,nick,serial}...]
        "dm": dm_rows,
        "comments": cmt_rows,
        "strategy_rules": (strategy or {}).get("rules", []),
        "strategy_global": g,
        "intent_names": [it.get("name", "") for it in _load_intents()],
    }


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    # 统一在响应头禁缓存: 浏览器常吃旧 dashboard.html / 旧 /api/state,
    # 旧版字段与当前数据源不一致 → 表现为「tab有数字但主体空白」。
    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _html(self, path):
        try:
            with open(path, "rb") as f:
                b = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        except Exception:
            self._json(404, {"error": f"dashboard.html 缺失: {path}"})

    def _serve_file(self, path):
        """提供本地文件(留痕截图/图片)。按扩展名给 MIME, 浏览器直接内联展示。"""
        import mimetypes
        try:
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            with open(path, "rb") as f:
                b = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        except Exception:
            self._json(404, {"error": f"file not found: {path}"})

    def log_message(self, fmt, *a):
        pass                                   # 静默, 避免刷屏

    def do_GET(self):
        p = urlparse(self.path)
        if p.path == "/" or p.path == "/index.html":
            self._html(DASH)
        elif p.path == "/content_workbench.html":
            self._html(os.path.join(HERE, "content_workbench.html"))
        elif p.path == "/api/state":
            self._json(200, build_state())
        elif p.path == "/api/strategy":
            self._json(200, _load_strategy())
        elif p.path == "/api/proactive":
            log = _load_proactive_log()
            # ★ 词库跟赛道联动: 从 query 读当前账号 device → 返回该赛道默认搜索词
            qs = parse_qs(p.query)
            dev = (qs.get("device") or [""])[0]
            try:
                from config_track import get_default_kws
                dkws = get_default_kws(device=dev)
            except Exception:
                dkws = []
            self._json(200, {
                "log": log[-80:],            # 最近80条流水, 供前端看板
                "stats": _proactive_stats(log),
                "strategy": _read_proactive_yaml(),
                "run": _proactive_status(),  # 当前执行一轮的实时状态(进度)
                "default_kws": dkws,         # 账号赛道的默认搜索词(词跟赛道走)
            })
        elif p.path == "/api/proactive/status":
            self._json(200, _proactive_status())
        elif p.path == "/api/tracks":
            self._json(200, _track_state())
        elif p.path == "/api/autoreply/preview":
            self._json(200, self._autoreply_preview())
        elif p.path.startswith("/files/"):
            # ★ 留痕截图/附件服务: /files/out/proactive_shots/xxx.png
            #   路径相对 interceptor/ 根(ROOT), 安全校验防目录穿越
            #   unquote 解码 %E4%B8%AD 等中文文件名, 否则 urlparse().path 保留编码态
            rel = unquote(p.path[len("/files/"):]).lstrip("/")
            import posixpath
            safe = posixpath.normpath(rel)
            if safe.startswith("..") or not safe:
                self._json(404, {"error": "invalid path"})
                return
            target = os.path.join(ROOT, safe)
            if os.path.isfile(target):
                self._serve_file(target)
            else:
                self._json(404, {"error": f"not found: {safe}"})
        else:
            self._html(DASH)                   # 其他当静态首页兜底

    def do_POST(self):
        p = urlparse(self.path)
        try:
            ln = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(ln) if ln else b"{}"
            data = json.loads(raw.decode("utf-8") or "{}") if raw else {}
        except Exception:
            data = {}

        if p.path == "/api/followup":
            res = self._save_followup(data)
            self._json(200, res)
        elif p.path == "/api/account":
            self._json(200, self._select_account(data))
        elif p.path == "/api/strategy":
            res = self._save_strategy(data)
            self._json(200, res)
        elif p.path == "/api/proactive/strategy":
            res = self._save_proactive_strategy(data)
            self._json(200, res)
        elif p.path == "/api/proactive/run":
            self._json(200, self._proactive_run(data))
        elif p.path == "/api/proactive/stop":
            self._json(200, self._proactive_stop())
        elif p.path == "/api/draft":
            user = data.get("user", "")
            recent = data.get("留言", "")
            note = data.get("来源笔记", "")
            src_type = data.get("来源类型", "")
            # 新引擎(诊断器→策略命中器→组合器): 给"为什么这样回"的诊断 + 更贴合回复
            try:
                sys.path.insert(0, os.path.join(ROOT, "classify"))
                from reply_compose import compose_reply
                from config_track import get_track_source
                # ★ 账号驱动: 话术视角 = 当前账号的赛道, 而非全局先选
                #   (前端会传 device; 不传则回退账号推导/兜底)
                sh = get_track_source(device=(data.get("device") or ""))
                out = compose_reply(recent, note, src_type, source_hint=sh)
                d = out.get("diag", {})
                self._json(200, {
                    "user": user, "recent": recent,
                    "intent": d.get("intent", ""),
                    "tier": d.get("tier", ""),
                    "action": ("auto" if d.get("tier") in ("P2",) else "human"),
                    "draft": out.get("reply", ""),
                    "engine": out.get("engine", "rule"),
                    "strategy_key": out.get("key", ""),
                    "diag": d,   # 5信号: 阶段/问价类型/来源/人群/情绪, 前端可展示"为何这样回"
                })
            except Exception as e:
                # 降级: 旧规则稿, 保证不空
                it, dk = _auto_draft_intents(user, recent)
                self._json(200, {
                    "user": user, "recent": recent,
                    "intent": it.get("name", "") if it else "",
                    "tier": it.get("tier", "") if it else "",
                    "action": it.get("action", "") if it else "",
                    "draft": dk, "engine": "legacy", "diag": {},
                })
        elif p.path == "/api/autoreply/run":
            self._json(200, self._autoreply_run(data))
        else:
            self._json(404, {"error": "unknown api " + p.path})

    # -------- 一键自动派发: 预览当前白名单待发清单 --------
    def _autoreply_preview(self):
        """复用 auto_reply.load_pending_auto() 读 classified 找 route=auto 且 auto_safe 的白名单。
        只读, 不发送。返回 {ok, targets, auto_enabled, note}。"""
        try:
            sys.path.insert(0, os.path.join(ROOT, "collector"))
            import auto_reply as ar
            strategy = ar._load_strategy()
            g = (strategy or {}).get("global", {}) or {}
            auto_on = bool(g.get("auto_enabled", True))
            mode = (g.get("auto_mode") or "semi").lower()
            targets = ar.load_pending_auto()
            return {"ok": True, "auto_enabled": auto_on, "auto_mode": mode,
                    "targets": targets,
                    "count": len(targets),
                    "note": ("当前为全自动/半自动, 可一键真发" if mode != "manual" and auto_on
                             else ("当前全人工, 需真人回" if mode == "manual" else "总开关关闭, 不会自动发")) if auto_on else "总开关关闭, 不会自动发"}
        except Exception as e:
            return {"ok": False, "error": str(e), "targets": [], "count": 0}

    # -------- 一键自动派发: 拉起 auto_reply.py 真发(独立 u2 长会话) --------
    def _autoreply_run(self, payload):
        """subprocess 拉起 collector/auto_reply.py --send --confirm 真发白名单。
        由于 uiautomator2 需独立长会话, 用子进程跑; 简短等待后返回已启动。
        payload: {device} 可选设备号(否则用 XHS_DEVICE/默认)。"""
        py = _runner_python()
        ar_script = os.path.join(ROOT, "collector", "auto_reply.py")
        if not os.path.exists(ar_script):
            return {"ok": False, "error": f"auto_reply.py 缺失: {ar_script}"}
        device = (payload or {}).get("device") or os.environ.get("XHS_DEVICE", "")
        import subprocess as sp
        env = dict(os.environ)
        if device:
            env["XHS_DEVICE"] = device
        cmd = [py, ar_script, "--send", "--confirm"]
        # 后台跑, 不阻塞 HTTP 响应(发送可能要几十秒)
        try:
            sp.Popen(cmd, env=env,
                     stdout=sp.DEVNULL, stderr=sp.DEVNULL,
                     creationflags=getattr(sp, "CREATE_NO_WINDOW", 0))
            return {"ok": True, "started": True, "device": device or "默认",
                    "note": "已后台启动自动派发(真发白名单); 稍后刷新回流表看结果"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # -------- 控制器: 保存策略(开关/规则) 到 strategy.yaml --------
    def _save_strategy(self, payload):
        """payload: {global:{auto_enabled,auto_daily_cap,auto_interval_min}, rules:[...]}
        合并写回 strategy.yaml, 供控制器热更新。"""
        import yaml
        with _WRITE_LOCK:
            try:
                with open(STRATEGY_FP, encoding="utf-8") as f:
                    cur = yaml.safe_load(f) or {}
            except Exception:
                cur = {}
            # 合并 global 开关
            g = dict(cur.get("global", {}) or {})
            newg = payload.get("global") or {}
            for k in ("auto_mode", "auto_enabled", "auto_daily_cap", "auto_interval_min"):
                if k in newg:
                    g[k] = newg[k]
            cur["global"] = g
            # 合并 rules(若推送了完整 rules 则替换)
            if payload.get("rules"):
                cur["rules"] = payload["rules"]
            with open(STRATEGY_FP, "w", encoding="utf-8") as f:
                yaml.safe_dump(cur, f, allow_unicode=True, sort_keys=False)
        return {"ok": True, "global": g, "rules": cur.get("rules", [])}

    # -------- 写主动获客策略(proactive_targets.yaml) --------
    def _save_proactive_strategy(self, payload):
        """payload: {targets:{key:{enabled,kw,note_match,comment_style,hook,...}}, global:{...}}
        合并写回 proactive_targets.yaml(策略热更新, 不改代码)。
        只合并推送的字段, 保留未推送的 target 与 global 项。"""
        import yaml
        with _WRITE_LOCK:
            try:
                with open(PROACTIVE_YAML, encoding="utf-8") as f:
                    cur = yaml.safe_load(f) or {}
            except Exception:
                cur = {}
            cur.setdefault("targets", {})
            cur.setdefault("global", {})
            # 合并 targets: 推送的 key 逐字段合并(保留旧值)
            newtg = payload.get("targets") or {}
            for key, t in newtg.items():
                cur["targets"][key] = {**cur["targets"].get(key, {}), **(t or {})}
            # 合并 global 频控参数 + 主动获客总开关(正交: 独立于赛道)
            newg = payload.get("global") or {}
            for k in ("daily_comment_cap", "comment_interval_min", "per_note_max_comment",
                      "open_notes_cap", "target_diversity", "dry_run_default",
                      "proactive_enabled", "lanes_multi_select"):
                if k in newg:
                    cur["global"][k] = newg[k]
            # 若推送了 enabled=false 且无其他字段, 保留该 target 但关闭
            with open(PROACTIVE_YAML, "w", encoding="utf-8") as f:
                yaml.safe_dump(cur, f, allow_unicode=True, sort_keys=False)
        return {"ok": True, "targets": cur.get("targets", {}),
                "global": cur.get("global", {})}

    # -------- 一键跑主动获客(拉起 proactive_engage.py) --------
    def _proactive_run(self, payload):
        """subprocess 拉起 collector/proactive_engage.py 跑一轮主动获客。
        默认 dry-run; 真发需 payload.send + confirm。
        ★ 进度: 把子进程 stdout 追加写 PROACTIVE_RUN_LOG(而非 DEVNULL),
          并起 _watch_run 线程滚动更新 _RUN_STATE → 前端 /api/proactive/status 轮询可见实时阶段。
        返回 {ok, started, kw, send, note}。"""
        py = _runner_python()
        script = os.path.join(ROOT, "collector", "proactive_engage.py")
        if not os.path.exists(script):
            return {"ok": False, "error": f"proactive_engage.py 缺失: {script}"}
        kw = (payload or {}).get("kw") or "研学"
        target = (payload or {}).get("target") or ""
        maxn = int((payload or {}).get("max") or 3)
        send = bool((payload or {}).get("send"))
        # ★ 真发 hook 修复: 前端勾了「真发」即视同确认(不再要求额外 confirm 字段)
        #   原来前端只传 send 没传 confirm, 后端 confirm 恒 False → 永远 dry-run
        confirm = bool((payload or {}).get("confirm", send))
        import subprocess as sp
        import datetime as _dt
        env = dict(os.environ)
        # ★ 关键: 强制子进程无缓冲输出(Python 非TTY下默认全缓冲, print 不实时落日志→前端看不到进度)
        env["PYTHONUNBUFFERED"] = "1"
        device = (payload or {}).get("device") or os.environ.get("XHS_DEVICE", "")
        if device:
            env["XHS_DEVICE"] = device
        cmd = [py, script, "--kw", kw, "--max", str(maxn)]
        if target:
            cmd += ["--target", target]
        if send and confirm:
            cmd += ["--send", "--confirm"]

        # 清空上次进度日志, 写入本次运行头
        try:
            if os.path.exists(PROACTIVE_RUN_LOG):
                os.remove(PROACTIVE_RUN_LOG)
            with open(PROACTIVE_RUN_LOG, "w", encoding="utf-8") as f:
                f.write(f"=== 本轮主动获客已启动 @ {_dt.datetime.now().strftime('%H:%M:%S')} "
                        f"| 词={kw} | 目标={target or '自动'} | 上限={maxn} | "
                        f"{'真发+确认' if (send and confirm) else 'dry-run 只生成'} ===\n")
        except Exception:
            pass

        # 以写 stdout 方式拉起(父进程不随会话清理; 子进程 CWD 用 console/, 脚本用绝对路径)
        try:
            import io
            log_fh = open(PROACTIVE_RUN_LOG, "a", encoding="utf-8", buffering=1)
        except Exception:
            log_fh = None
        try:
            proc = sp.Popen(cmd, env=env, stdout=log_fh or sp.DEVNULL, stderr=sp.STDOUT,
                            creationflags=sp.CREATE_NO_WINDOW, close_fds=True,
                            cwd=os.path.join(ROOT, "collector"))
        except Exception as e:
            if log_fh:
                log_fh.close()
            return {"ok": False, "error": f"启动执行引擎失败: {e}"}

        with _RUN_LOCK:
            _RUN_STATE.update({
                "running": True, "done": False, "ok": None,
                "pid": proc.pid,
                "started": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "phase": "启动中", "progress": "已启动执行引擎",
                "mode": "真发+确认" if (send and confirm) else "dry-run 只生成",
                "kw": kw, "target": target or "自动", "maxn": maxn, "send": bool(send and confirm),
                "note": "", "error": "",
            })
        # 子进程结束后的收尾线程(set phase/done + 落盘)
        threading.Thread(target=_watch_run, args=(proc, PROACTIVE_RUN_LOG), daemon=True).start()
        # 读日志尾部做一次初始阶段推断
        _set_run_phase([kw])

        mode = "真发" if (send and confirm) else "干跑(默认)"
        return {"ok": True, "started": True, "kw": kw, "target": target,
                "max": maxn, "send": bool(send and confirm),
                "pid": proc.pid,
                "note": f"已启动一轮'{kw}'主动获客 · {mode} · 可在下方查看实时进度"}

    def _proactive_stop(self):
        """终止当前正在跑的主动获客一轮。找到活跃子进程(按 _RUN_STATE.pid)并 kill。"""
        with _RUN_LOCK:
            pid = _RUN_STATE.get("pid")
            running = bool(_RUN_STATE.get("running")) and not _RUN_STATE.get("done")
        if not pid or not running:
            return {"ok": False, "error": "当前没有正在运行的主动获客一轮"}
        import subprocess as sp
        try:
            # 用 taskkill 杀整个进程树(proactive_engage 可能派生子进程/守护)
            r = sp.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, text=True)
            if r.returncode == 0:
                with _RUN_LOCK:
                    _RUN_STATE["done"] = True
                    _RUN_STATE["ok"] = False
                    _RUN_STATE["phase"] = "已手动停止"
                    _RUN_STATE["note"] = "主动获客一轮已被手动终止"
                return {"ok": True, "note": f"已终止主动获客一轮 (PID {pid})"}
            else:
                return {"ok": False, "error": f"终止失败: {r.stdout.strip() or r.stderr.strip()}"}
        except Exception as e:
            return {"ok": False, "error": f"终止异常: {e}"}

    # -------- 选择账号(账号驱动赛道) --------
    def _select_account(self, payload):
        """payload: {device:"YOUR_DEVICE_SERIAL"} → 选中某账号; 返回该账号的赛道状态。
        ★ 账号驱动: 赛道 = 账号定位(lane), 不再有"先切全局赛道"。
        切换账号 = 切换赛道策略(主动获客人群 + 承接话术视角都随账号走)。
        若账号带显式 lane, 保持; 也可 payload.lane 覆盖该账号的 lane(改定位)。"""
        import yaml
        device = (payload or {}).get("device") or ""
        with _WRITE_LOCK:
            try:
                with open(LANES_FP, encoding="utf-8") as f:
                    cur = yaml.safe_load(f) or {}
            except Exception:
                cur = {}
            # 可选: 调整该账号的定位(赛道)
            new_lane = (payload or {}).get("lane")
            acc = cur.setdefault("accounts", {})
            if device in acc and new_lane:
                if isinstance(acc[device], dict):
                    acc[device]["lane"] = new_lane
                # 兼容旧格式(值是纯字符串昵称) → 转成对象
            # 读账号当前 lane
            try:
                from config_track import account_meta, lane_for_device
                lane = lane_for_device(device)
            except Exception:
                lane = "yanxue"
            if device not in acc:
                # 未知账号则新增(补齐对象结构)
                acc[device] = {"nick": device, "lane": lane, "desc": ""}
            with open(LANES_FP, "w", encoding="utf-8") as f:
                yaml.safe_dump(cur, f, allow_unicode=True, sort_keys=False)
        try:
            from config_track import list_tracks
            am = account_meta(device)
            nm = list_tracks().get(am["lane"], {}).get("name", am["lane"])
            ts = _track_state(device)   # {active, tracks, account}
            return {"ok": True,
                    # 兼容两套前端读取: 嵌套 track(新) + 顶层展开(旧) 都给出
                    **ts, "track": ts,
                    "accounts": _accounts_state(),
                    "note": f"已选账号【{am['nick']}】· {nm}（定位决定赛道，随账号切换）"}
        except Exception:
            return {"ok": False, "error": "账号选择失败"}


    # -------- 写回流表(唯一可写) --------
    def _save_followup(self, payload):
        """payload: {rows:[{user,已回,对方反应,真商机,备注}]} → upsert 到 leads_followup.csv"""
        rows = payload.get("rows") or []
        if not rows:
            return {"ok": False, "error": "no rows"}
        global FOL
        with _WRITE_LOCK:
            fol = _read(FOL)
            folmap = {_norm(r.get("user")): r for r in fol}
            import datetime
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            changed = []
            for it in rows:
                u = _norm(it.get("user"))
                if not u:
                    continue
                # 兼容两种 key 写法: 前端发「已回/对方反应/真商机」; CSV 列名带「?」
                combos = {
                    "已回?":   it.get("已回")  or it.get("已回?"),
                    "对方反应": it.get("对方反应"),
                    "真商机?": it.get("真商机") or it.get("真商机?"),
                    "备注":   it.get("备注"),
                }
                # 合并回流表同 user 旧行携带的来源/分级(若 controller 没推来)
                old = folmap.get(u, {})
                merged = dict(old)
                merged["hs"] = it.get("hs") or old.get("hs") or now
                merged["user"] = u
                for src_k, fol_k in (("来源笔记","来源笔记"),("来源类型","来源类型"),
                                     ("tier","tier"),("意图","意图"),("action","action"),
                                     ("拟稿","拟稿(自动生成)"),("device","device")):
                    if it.get(src_k):
                        merged[fol_k] = it[src_k]
                # device: 控制器推来的优先, 其次沿用旧行, 兜底默认设备
                if not merged.get("device"):
                    merged["device"] = it.get("device") or old.get("device") or "默认设备"
                for fol_k, v in combos.items():
                    if v and _norm(v):
                        merged[fol_k] = _norm(v)
                folmap[u] = merged
                changed.append(u)
            sorted_fol = list(folmap.values())
            # 写回
            cols = _FOL_COLS + [c for c in sorted_fol[0].keys() if c not in _FOL_COLS] if sorted_fol else _FOL_COLS
            with open(FOL, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=cols)
                w.writeheader()
                w.writerows(sorted_fol)
        return {"ok": True, "changed": changed}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--nobrowser", action="store_true")
    a = ap.parse_args()

    # 首启时若回流表不存在, 建表头(并确保 out/ 目录存在)
    if not os.path.exists(FOL):
        os.makedirs(OUT, exist_ok=True)
        with open(FOL, "w", encoding="utf-8-sig", newline="") as f:
            csv.writer(f).writerow(_FOL_COLS)

    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://127.0.0.1:{a.port}"
    print("=" * 54)
    print("  小红书人工值守 · Console")
    print(f"  打开: {url}")
    print("  Ctrl+C 退出")
    print("=" * 54)
    if not a.nobrowser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
