#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小红书 自动派发器（白名单 auto 回复 · 能力可正常使用的执行端）
================================================================
用途: 找到 route=auto(白名单) 的客户, 机器在 app 里真实发出预设回复,
      并把"已自动回"记回流, 让「自动分流→自动派发→回流记录」闭环跑通。

红线(业务):
  * 只对 route=auto 且 auto_safe=True 的白名单客户自动发(资料/干货类, 无价无承诺无硬引流)。
  * 高意向/涉价/涉加微(route=manual) 一律跳过, 绝不自动发。
  * 走「存草稿/发送」分离思路保守一点: 默认 dry-run 只停在输入框不点发送,
    加 --send 才真点发送(宇豪拍板后再放)。

用法(手机已连):
  python auto_reply.py                  # dry-run: 找出白名单并定位输入框, 不发送
  python auto_reply.py --send           # 真发送(谨慎, 需--confirm)
  python auto_reply.py --confirm --send # 确认真发

设备: 读 XHS_DEVICE(同采集器), 缺省连默认设备。
"""
from __future__ import annotations
import argparse, csv, os, sys, time, json, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                       # interceptor/
OUT = os.path.join(ROOT, "out")
CLS = os.path.join(OUT, "leads_classified.csv")    # 分级+拟稿(只读)
FOL = os.path.join(OUT, "leads_followup.csv")      # 回流表(唯一可写)
STRATEGY_FP = os.path.join(ROOT, "console", "strategy.yaml")
SEND_LOG = os.path.join(OUT, "auto_send_log.json") # 自动派发频控日志(本机持久化)

PKG = "com.xingin.xhs"

# 会话输入框中心(1080x2400 分辨率, 真机实测) → 由 dump 动态定位, 此处仅兜底
FALLBACK_INPUT = (487, 2273)


# ---------------------------------------------------------------- u2
def conn():
    import uiautomator2 as u2
    serial = os.environ.get("XHS_DEVICE", "")
    return u2.connect(serial) if serial else u2.connect()


def _dump(d):
    import xml.etree.ElementTree as ET
    try:
        return ET.fromstring(d.dump_hierarchy())
    except Exception:
        return None


def wait_app(d):
    """前台化小红书并等首屏。"""
    d.app_start(PKG, use_monkey=True, stop=False)
    time.sleep(2.5)


def go_msg_list(d):
    """确保处于私信会话列表(w/ 底部导航『消息』)。幂等: 处理漂移。"""
    w, h = d.window_size()
    # 若当前是会话正文(有 EditText 输入框) → back 退回
    if d(className="android.widget.EditText").count > 0:
        d.press("back"); time.sleep(1)
    # 点底部『消息』Tab
    d.click(int(w * 0.75), int(h * 0.97)); time.sleep(1.2)


def find_conv_rows(d):
    """解析消息列表私信会话行, 返回 [(nick, center_y)]。排除系统通知块。"""
    import re
    root = _dump(d)
    if root is None:
        return []
    w, h = d.window_size()
    y_top = h * 0.10
    nav_y = h * 0.88
    bad_prefix = ("赞和收藏", "新增关注", "评论和@", "活动消息", "系统消息",
                  "关注", "粉丝", "消息通知", "私信")
    cands = []
    for n in root.iter("node"):
        a = n.attrib
        desc = (a.get("content-desc", "") or "").strip()
        if not desc:
            continue
        if any(desc.startswith(p) for p in bad_prefix):
            continue
        m = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", a.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = (int(v) for v in m.groups())
        cy = (y1 + y2) // 2
        if cy < y_top or cy >= nav_y:
            continue
        nick = desc.split("，，")[0] or desc.split("，")[0]
        cands.append((nick, (x1 + x2) // 2, cy, a.get("bounds", "")))
    # 去重(保留首次)
    seen, rows = set(), []
    for nick, cx, cy, bnd in cands:
        if nick in seen or len(nick) < 1:
            continue
        seen.add(nick); rows.append((nick, cx, cy))
    return rows


def open_conv(d, cx, cy):
    d.click(cx, cy); time.sleep(1.5)
    # 确认进入会话(有输入框)
    return d(className="android.widget.EditText").count > 0


def input_and_send(d, text, send=False):
    """定位输入框→set_text→(可发)点发送。返回 True=输入成功。"""
    et = d(className="android.widget.EditText")
    if et.count < 1:
        print("  [FAIL] 找不到输入框"); return False
    box = et[0]
    box.click(); time.sleep(0.6)
    box.set_text(text); time.sleep(1.0)
    # 校验输入成功
    cur = box.info.get("text", "").strip()
    if text not in cur:
        print(f"  [WARN] 输入未生效: got={cur!r}"); return False
    print(f"  [OK] 已输入: {text[:40]}...")
    if not send:
        print("  [SKIP] dry-run: 未点发送"); return True
    # 找『发送』按钮
    import re
    root = _dump(d)
    if root is None:
        print("  [FAIL] 发送按钮定位失败"); return False
    # 发送按钮是 TextView text=发送 (键盘弹出后 y~1325)
    for n in root.iter("node"):
        a = n.attrib
        if (a.get("text", "") or "").strip() == "发送":
            m = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", a.get("bounds", ""))
            if m:
                x1, y1, x2, y2 = (int(v) for v in m.groups())
                d.click((x1 + x2) // 2, (y1 + y2) // 2)
                print("  [SEND] 已点发送"); time.sleep(1.2)
                return True
    print("  [FAIL] 未找到『发送』按钮(可能输入未触发发送态)")
    return False


def clean_input(d):
    """清空输入框, 不发送。"""
    et = d(className="android.widget.EditText")
    for e in et:
        if (e.info.get("text", "") or "").strip():
            e.set_text(""); time.sleep(0.5)


def leave_conv(d):
    d.press("back"); time.sleep(1)


# ---------------------------------------------------------------- 数据
def _load_strategy():
    try:
        import yaml
        with open(STRATEGY_FP, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {"rules": [], "global": {"auto_enabled": False}}


def route_for(intent, tier, action, strategy):
    """取可自动发的客户。
    auto_mode 三态:
      semi(默认): 仅 route=auto 且 auto_safe=True 可自动发
      full       : 除 ignore 外全可自动发(auto_safe 放宽)
      manual     : 一律 None(不可自动发)
    返回命中的 rule 或 None。"""
    rules = (strategy or {}).get("rules", []) or []
    g = (strategy or {}).get("global", {}) or {}
    auto_on = bool(g.get("auto_enabled", True))
    mode = (g.get("auto_mode") or "semi").lower()
    if mode == "manual" or not auto_on:
        return None
    it = intent or ""
    for r in rules:
        needle = r.get("intent_contains", "")
        if not needle:
            continue
        if any(seg and seg in it for seg in needle.split("|")):
            route = r.get("route", "manual")
            safe = bool(r.get("auto_safe", False))
            if mode == "full":
                if route == "ignore":
                    return None
                return r
            if route == "auto" and safe:
                return r
            return None   # 命中规则但非安全 auto → 不自动发
    return None


# ---------------------------------------------------------------- 频控(限流真执行)
# 从 strategy.yaml 的 global 读 auto_daily_cap(单日上限) / auto_interval_min(两条间隔),
# 用 out/auto_send_log.json 持久化每次真发时间戳, 逐条发送前校验, 超限则跳过。
# —— 落地"仅展示不强制"→ 真正在发送执行层限流 ——
def _read_log():
    """读发送日志, 返回 [{ts, user}...]。文件损坏则置空。"""
    if not os.path.exists(SEND_LOG):
        return []
    try:
        with open(SEND_LOG, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _write_log(items):
    os.makedirs(OUT, exist_ok=True)
    with open(SEND_LOG, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)


def _sent_today(log):
    """今天已真发的条数。"""
    today = datetime.date.today().isoformat()
    return sum(1 for x in log if (x.get("ts") or "").startswith(today))


def _last_ts(log):
    """最近一次真发时间(epoch秒), 无则 None。"""
    ts = [x.get("epoch") or 0 for x in log]
    return max(ts) if ts else None


def check_rate_limit(strategy):
    """频控校验。返回 (ok, msg)。
    ok=True 可继续发; ok=False 说明超限/间隔不足, msg 为原因。"""
    g = (strategy or {}).get("global", {}) or {}
    cap = int(g.get("auto_daily_cap", 5) or 0)
    interval = int(g.get("auto_interval_min", 120) or 0)
    log = _read_log()
    # 单日上限
    if cap > 0:
        today_cnt = _sent_today(log)
        if today_cnt >= cap:
            return (False, f"已达单日上限 {cap} 条(今日已发 {today_cnt}), 明日再发")
    # 两条间隔(单位分钟)
    if interval > 0:
        last = _last_ts(log)
        if last is not None:
            wait_s = interval * 60
            elapsed = time.time() - last
            if elapsed < wait_s:
                remain = int((wait_s - elapsed) / 60) + 1
                return (False, f"距上一条不足 {interval} 分钟, 还需等约 {remain} 分钟")
    return (True, "ok")


def log_sent(user):
    """真发成功后记录一条(含用户+时间戳)。"""
    log = _read_log()
    log.append({"user": user, "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "epoch": int(time.time())})
    _write_log(log)


def load_pending_auto():
    """从 classified 找 route=auto 且 auto_safe 的白名单待回客户。
    ★ 2026-09-06 修复: 读取回流表, 排除已"已回/已自动回"的用户 → 防重复派发。"""
    if not os.path.exists(CLS):
        return []
    with open(CLS, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    # 回流表: 已回 或 已自动回 的用户, 不再进待发(避免重复发送)
    replied = set()
    if os.path.exists(FOL):
        try:
            with open(FOL, encoding="utf-8-sig", newline="") as f2:
                for fr in csv.DictReader(f2):
                    if (fr.get("已回?") or "") in ("已回", "已自动回"):
                        replied.add((fr.get("user") or "").strip())
        except Exception:
            replied = set()
    strategy = _load_strategy()
    out = []
    seen = set()
    for r in rows:
        user = (r.get("user") or "").strip()
        if not user or user in seen:
            continue
        if user in replied:
            continue
        seen.add(user)
        intent = r.get("意图", "")
        tier = (r.get("tier") or "").upper()
        action = r.get("action", "human")
        rule = route_for(intent, tier, action, strategy)
        if rule is None:
            continue
        out.append({"user": user, "intent": intent, "tier": tier,
                    "draft": r.get("拟稿", "") or rule.get("note", ""),
                    "note": rule.get("note", "")})
    return out


def mark_followup(user, status):
    """把已自动回记一笔到回流表(同名 upsert)。带 device(多设备不串)。"""
    import datetime
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    dev = os.environ.get("XHS_DEVICE", "").strip() or "默认设备"
    fol = []
    if os.path.exists(FOL):
        with open(FOL, encoding="utf-8-sig", newline="") as f:
            fol = [x for x in csv.DictReader(f) if x]
    m = { (r.get("user") or "").strip(): r for r in fol }
    old = m.get(user, {})
    row = dict(old)
    row["hs"] = now
    row["user"] = user
    row["已回?"] = "已回"
    row["对方反应"] = status
    row["真商机?"] = row.get("真商机?", "") or "—待定"
    row["device"] = dev
    m[user] = row
    cols = ["hs","user","来源笔记","来源类型","tier","意图","action",
            "拟稿(自动生成)","已回?","对方反应","真商机?","备注","device"]
    allcols = cols + [c for c in next(iter(m.values()), {}).keys() if c not in cols]
    with open(FOL, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=allcols)
        w.writeheader()
        w.writerows(m.values())


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="真点发送(dry-run 默认不发送)")
    ap.add_argument("--confirm", action="store_true", help="确认真发")
    a = ap.parse_args()

    if a.send and not a.confirm:
        print("⚠️  --send 需配 --confirm 才真发。当前转 dry-run。")
        a.send = False

    d = conn()
    wait_app(d)
    go_msg_list(d)

    strategy = _load_strategy()

    # 频控总校验: 单日上限/间隔, 超限则直接不派发
    if a.send:
        ok, msg = check_rate_limit(strategy)
        if not ok:
            print(f"\n[频控] 停止派发: {msg}")
            return
        print(f"\n[频控] 通过: 今日可发(上限 {strategy.get('global',{}).get('auto_daily_cap',5)}/天)")

    targets = load_pending_auto()
    print(f"\n=== 白名单 auto 待回: {len(targets)} 条 ===")
    for t in targets:
        print(f"  @{t['user']} [{t['tier']}] {t['intent']}")

    if not targets:
        print("无白名单客户可自动发。")
        return

    rows = find_conv_rows(d)
    row_by_nick = {nick: (cx, cy) for nick, cx, cy in rows}
    print(f"\n消息列表会话: {len(rows)} 行")

    for t in targets:
        user = t["user"]
        # 每条真发前再校验一次频控(防止批量超限)
        if a.send:
            ok2, msg2 = check_rate_limit(strategy)
            if not ok2:
                print(f"\n[频控] 已在 {msg2} 处停止, 剩余客户跳过")
                break
        print(f"\n>> 处理 @{user} ({t['intent']})")
        if user not in row_by_nick:
            print("  [SKIP] 消息列表未见此用户(可能不在列表/未采到)"); continue
        cx, cy = row_by_nick[user]
        if not open_conv(d, cx, cy):
            print("  [SKIP] 进入会话失败"); continue
        draft = t["draft"] or t["note"]
        if not draft:
            print("  [SKIP] 无预设拟稿"); leave_conv(d); continue
        ok_send = input_and_send(d, draft, send=a.send)
        if ok_send and a.send:
            mark_followup(user, "已自动回")
            log_sent(user)   # 频控日志: 记一次真发
            print(f"  [记录] 已写回流 @{user}")
        elif ok_send:
            mark_followup(user, "已拟稿(未发)")
            print(f"  [记录] 已写回流 @{user}(dry-run)")
        clean_input(d)
        leave_conv(d)


if __name__ == "__main__":
    main()
