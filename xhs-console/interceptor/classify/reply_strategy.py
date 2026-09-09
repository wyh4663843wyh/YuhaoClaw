#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小红书回复 · 策略命中器
================================================================
职责: 输入诊断信号(diag), 从 reply_strategy.yaml 查"该场景该怎么回"。
返回:
  {
    "found"    : bool              是否命中策略(找到策略骨架)
    "key"      : str               命中的策略键(形如 value__opc_recruit__guide)
    "tone"     : str               说话基调(策略骨架)
    "goal"     : str               这一句要达到什么
    "taboos"   : [str,...]         禁区(防翻车)
    "lines"    : [str,...]          候选好句组(≥2, 供组合/LLM选)
    "angle"    : str               来源切入角度(说明)
    "fallback": str                中间降级的说明(便于日志, 非致命)
  }

命中逻辑(逐级降级, 保证永远有东西可回):
  1) 精确: stage__source__persona            (如 quote__opc_recruit__newbie)
  2) 去掉 persona: stage__source__any        (如 quote__opc_recruit__any)
  3) 去掉 source: stage__any__any            (如 quote__any__any)
  4) 兜底: first_ask__any__any               (首问通用)
  找不到 → found=False(上层用规则兜底或标"待人工")

★ quote 阶段特殊: 需按 quote_kind(course_price/task_price) 挑到对应子条目,
   比如问"多少钱一单"绝不能命中"课程价"那条话术。这是本命中器的关键职责之一。
"""
from __future__ import annotations

import os

HERE = os.path.dirname(os.path.abspath(__file__))
SFP = os.path.join(HERE, "reply_strategy.yaml")
OFP = os.path.join(HERE, "reply_self_generated.yaml")  # AI 自学习叠加层


def _load_strategy():
    """读策略库 = 主库(reply_strategy.yaml) + 叠加层(reply_self_generated.yaml)。
    叠加层是 AI 自学习回灌的好句, 按同 key 的 lines 并入主库, 供命中器取候选。
    主库保持手工维护; 叠加层由 reply_self_learn.py 写入。"""
    import yaml
    merged = {}
    for fp in (SFP, OFP):
        try:
            with open(fp, encoding="utf-8") as f:
                d = yaml.safe_load(f) or {}
        except Exception:
            continue
        s = d.get("strategies", {}) or {}
        for key, v in s.items():
            if key not in merged:
                merged[key] = dict(v)
                merged[key]["lines"] = list(v.get("lines", []))
            else:
                # 并入叠加层好句(去重)
                extra = v.get("lines", []) or []
                cur = merged[key].setdefault("lines", [])
                for l in extra:
                    if l not in cur:
                        cur.append(l)
    return {"strategies": merged, "source_angle": _load_source_angle()}


def _load_source_angle():
    """从主库读 source_angle(叠加层通常不带 source_angle)。"""
    import yaml
    try:
        with open(SFP, encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("source_angle", {}) or {}
    except Exception:
        return {}


def _find(strategies, parts):
    """按 键 逐级找, 返回 (完整键, 条目dict) 或 (None, None)。
    parts: 尝试的键前缀列表(从精确到泛)。
    2026-09-05 兼容: 历史 YAML 键名不统一(value_any__any__newbie 前两段用单下划线),
    标准 __ 链查不到时, 尝试把 __ 归并成 _ 再查一次(如 value__any__newbie → value_any__newbie)。
    """
    for key in parts:
        if key in strategies:
            return key, strategies[key]
    # 兼容别名: 把双下划线分隔归一成单下划线(处理 value_any__any__newbie 类历史键)
    for key in parts:
        alias = key.replace("__", "_")
        if alias in strategies:
            return alias, strategies[alias]
    return None, None


def match_strategy(diag):
    """diag: reply_diagnose.diagnose() 的返回。返回策略 dict(见 docstring)。"""
    stra = _load_strategy()
    strategies = stra.get("strategies", {}) or {}
    angle_map = stra.get("source_angle", {}) or {}

    stage = (diag.get("stage") or "first_ask")
    source = (diag.get("source") or "dm_unknown")
    persona = (diag.get("persona") or "unknown")
    quote_kind = (diag.get("quote_kind") or "None")

    # ---- ★ 导流/联系方式优先(2026-09-06): source=lead_routing 是最值钱场景 ----
    # 对方主动求联/含蓄递联系方式 → 走专属导流桶, 不按普通 stage 查(否则会落到首询通用桶)。
    # 键: 先试 lead_routing__<stage>__any(如 first_ask/act 细分), 再 lead_routing__any__any。
    if source == "lead_routing":
        parts = [
            f"lead_routing__{stage}__any",   # 按阶段细分(first_ask/act 等)
            f"lead_routing__any__any",       # 导流通用
        ]
        key, entry = _find(strategies, parts)
        if entry:
            return _pack(key, entry, angle_map, source, found=True)
        # 导流无条目 → 降级首询(防卡死, 但一般不会到这)

    # ---- quote 阶段: 必须按 quote_kind 区分课程价/任务价 ----
    if stage == "quote":
        qk = "course_price" if quote_kind == "course_price" else "task_price"
        parts = [
            f"quote__any__any__{qk}",      # 精确到"问什么价"
            f"quote__any__any",            # 泛报价
        ]
        key, entry = _find(strategies, parts)
        if entry:
            return _pack(key, entry, angle_map, source, found=True)
        # quote 无条目 → 降级到 first_ask 兜底
        key, entry = _find(strategies, ["first_ask__any__any"])
        if entry:
            return _pack(key, entry, angle_map, source, found=False,
                         fallback="quote 无专门条目, 降级首问")
        return _empty(angle_map, source, "quote 无可用策略")

    # ---- 其它阶段: 逐级 stage__source__persona → stage__any__persona → stage__source__any → stage__any__any ----
    # 2026-09-05 修复: 原链缺 stage__any__persona → 会漏匹配 confirm__any__any__guide / hesitant__any__any__zero_base
    parts = [
        f"{stage}__{source}__{persona}",
        f"{stage}__any__{persona}",      # 人群特定(如 guide/zero_base), 不限来源
        f"{stage}__{source}__any",
        f"{stage}__any__any",
    ]
    key, entry = _find(strategies, parts)
    if entry:
        return _pack(key, entry, angle_map, source, found=True)

    # 本阶段无条目 → 降级到 value 通用(有给价值的话术) 或 first_ask 兜底
    for fb in (["value__any__any"], ["first_ask__any__any"]):
        key, entry = _find(strategies, fb)
        if entry:
            return _pack(key, entry, angle_map, source, found=False,
                         fallback=f"{stage} 无专门条目, 降级 {fb[0]}")
    return _empty(angle_map, source, "策略库无任何可用条目")


def _pack(key, entry, angle_map, source, found, fallback=""):
    return {
        "found": found,
        "key": key,
        "tone": entry.get("tone", ""),
        "goal": entry.get("goal", ""),
        "taboos": entry.get("taboos", []) or [],
        "lines": entry.get("lines", []) or [],
        "angle": angle_map.get(source, ""),
        "fallback": fallback,
    }


def _empty(angle_map, source, msg):
    return {
        "found": False, "key": "", "tone": "", "goal": "", "taboos": [],
        "lines": [], "angle": angle_map.get(source, ""), "fallback": msg,
    }


if __name__ == "__main__":
    from reply_diagnose import diagnose
    import json
    tests = [
        # (留言, note, 期望阶段)
        ("有aigc制作经验", "示例笔记", "confirm"),
        ("直接接单", "示例笔记", "act"),
        ("多少钱一单", "", "quote"),
        ("你们初阶班多少钱怎么报名", "", "quote"),
        ("求资料", "", "value"),
        ("是做什么呢", "", "first_ask"),
    ]
    for msg, note, exp in tests:
        diag = diagnose(msg, note)
        s = match_strategy(diag)
        print(f"== {msg!r} → stage={diag['stage']} source={diag['source']} persona={diag['persona']}")
        print(f"   主键: {s['key']} | found={s['found']} | 切入: {s['angle']}")
        print(f"   候选好句 {len(s['lines'])} 条: {s['lines'][0][:50] if s['lines'] else '(空)'}")
