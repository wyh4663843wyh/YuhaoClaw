#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P1 分级拟稿:把 collect_inbox.py 采到的 leads_raw.csv 逐条扫一遍,
  按 intent_map.yaml 意图库分级(P0-P3)+ action(分配发送通道)+ 产出首拟稿。
输出: out/leads_classified.csv(人工跟进队列),并在终端打摘要。

可插桩说明:
  - 这是一台"规则先行"机器:先用关键词意图分类做 P0-P3 + 拟稿(模板+意图命中)。
  - 预留 LLM 增强桩:若接了主文档定义的拟稿 LLM 通道,把 _gen_draft() 底部的
    _llm(...) 换空即天然降级到规则稿。当前 P1 不做强依赖,规则稿够用。

用法:
  python classify_draft.py [--in ../out/leads_raw.csv] [--out ../out/leads_classified.csv]
"""
from __future__ import annotations

import argparse, csv, os, re, sys, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
YAML_FP = os.path.join(HERE, "intent_map.yaml")
DFLT_IN = os.path.join(HERE, "..", "out", "leads_raw.csv")
DFLT_OT = os.path.join(HERE, "..", "out", "leads_classified.csv")


def load_intents():
    import yaml
    with open(YAML_FP, encoding="utf-8") as f:
        return yaml.safe_load(f)["intent"]


def classify(text, intents):
    """命中优先级:文本里含某意图关键词即返回该意图;都不中→'其他/待分类'"""
    if not text:
        return intents[-1]
    for it in intents:
        for kw in it.get("keywords", []):
            if kw and kw in text:
                return it
    return intents[-1]     # 兜底"其他/待分类"


def _draft(it, user, recent):
    """先用规则稿;若预留 LLM 通道则走 _llm()。
    @user 昵称,@recent 原留言——拟稿里可带对方称呼让机器可读一点。"""
    dkp = it.get("draft", "")
    if not dkp:
        return (it["name"], "")     # 待人工
    # 简单埋对方称呼:话术里放〔昵称〕则替换(可选,昵称常空白就不硬嵌)
    if user and "〔昵称〕" in dkp:
        dkp = dkp.replace("〔昵称〕", user)
    return (it["name"], dkp)


def _tier_low(tier):
    return {"P0": 0, "P1": 1, "P2": 2, "P3": 3}.get(tier, 3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default=DFLT_IN)
    ap.add_argument("--out", dest="otp", default=DFLT_OT)
    a = ap.parse_args()

    if not os.path.exists(a.inp):
        print("no raw csv yet:", a.inp); return
    intents = load_intents()

    with open(a.inp, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print("raw csv empty. 先跑 collect_inbox.py 采到留言再分类。"); return

    added = False
    out_rows = []
    for r in rows:
        src, user, ts = (r.get("source", ""), r.get("user", ""), r.get("ts", ""))
        recent = (r.get("recent_text") or r.get("recent") or r.get("留言") or "")
        ctx    = (r.get("context") or "")
        # 判意图时合并 recent+context(最大化商机召回);显示文本仅落 recent 尾部
        judge = f"{recent} {ctx}".strip()
        display = (recent or judge)[:90]
        it = classify(judge, intents)
        it_name, dk = _draft(it, user, display)
        out_rows.append({
            "ts": ts, "src": src, "user": user, "留言": display,
            "来源笔记": (r.get("note_thread") or ""),
            "来源类型": (r.get("src_type") or ""),
            "tier": it["tier"], "意图": it_name, "action": it["action"],
            "状态": "待真人" if it["action"] != "auto" else "可auto",
            "拟稿": dk,
            # 多设备标签: 透传采集层的 device; 旧数据无该列 → 默认设备
            "device": (r.get("device") or "默认设备"),
        })

    # 按 tier 排序
    out_rows.sort(key=lambda x: _tier_low(x["tier"]))
    cols = ["ts", "src", "user", "留言", "来源笔记", "来源类型",
            "tier", "意图", "action", "状态", "拟稿", "device"]
    with open(a.otp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(out_rows)

    from collections import Counter
    cnt = Counter(r["tier"] for r in out_rows)
    auto_n = [r for r in out_rows if r["action"] == "auto"]
    print(f"[classify] 共 {len(out_rows)} 条 → {a.otp}")
    print("  分层:", dict(cnt), "| auto白名单可发:", len(auto_n))
    print("  前3高优先(建议优先真人跟进):")
    for r in out_rows[:3]:
        print(f"    P{r.get('tier','')} {r.get('src','')} @{r.get('user','')}: {r.get('拟稿','')[:60]}")


if __name__ == "__main__":
    main()
