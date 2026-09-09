#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
赛道调度层 · 中央配置解析（账号驱动赛道 / 2026-09-06 重构）
================================================================
★ 核心逻辑（按宇豪定性修正）：
  实体 = 【账号（手机）】，每个账号自带定位 → 定位决定赛道。
  赛道【不是】用户先选的全局开关，而是【跟着账号走】的属性。
  你切「看哪台手机」，就自动加载这台账号的赛道策略
  （主动获客人群 + 承接话术视角 都随账号切换）。

  因此：
  - "当前赛道" = 当前所选账号的 lane（不再有全局 active）。
  - 若前端没传账号，才用全局 active 兜底（兼容旧调用）。

★ 设计原则(最小侵入 + 无回归):
  - 现有 classify/*.yaml 仍是「默认赛道」的完整配置，不动。
  - 赛道差异化通过「叠加」实现: 每个赛道在 console/tracks/<id>/ 下可放
    「差异化配置文件」(如 tracks/opc/intent_map.yaml)，缺省字段继承默认。
  - 主动获客 targets = 全局稳定模块(已全量并入 proactive_targets.yaml, 每 target 带 track 标签)。

用法(纯函数):
  from config_track import (
      lane_for_device,       # 某账号(设备)的赛道 id
      get_active_track,      # 当前赛道(有 device/当前账号 则按其, 否则全局兜底)
      get_track_source,      # 当前赛道对应的来源视角(source)
      load_yaml_for_track,   # 按当前赛道叠加差异读取配置
      device_name, device_map,
  )
"""
from __future__ import annotations

import os

_CONSOLE = os.path.dirname(os.path.abspath(__file__))          # console/
_ROOT = os.path.dirname(_CONSOLE)                              # interceptor/
_CLASSIFY = os.path.join(_ROOT, "classify")                    # 默认配置目录
_TRACKS_DIR = os.path.join(_CONSOLE, "tracks")                 # 赛道差异化目录
_LANES_FP = os.path.join(_CONSOLE, "lanes.yaml")               # 中央调度配置


def _load_yaml(fp):
    if not fp or not os.path.exists(fp):
        return None
    try:
        import yaml
        with open(fp, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return None


def _lanes():
    return _load_yaml(_LANES_FP) or {"tracks": {}, "accounts": {}}


# ---------------------------------------------------------------- 赛道查询
def list_tracks():
    """赛道字典: {id: {name, desc, source}}。"""
    return (_lanes().get("tracks") or {})


def list_accounts():
    """账号字典: {设备号/别名: {nick, lane, desc}}。"""
    return (_lanes().get("accounts") or {})


def account_meta(device=None):
    """某账号元信息 {id, nick, lane, desc}; 缺省回退第一个账号或空结构。"""
    acc = list_accounts()
    did = device or (list(acc.keys())[0] if acc else None)
    meta = acc.get(did) or {}
    return {"id": did or "默认设备", "nick": meta.get("nick", did or "默认设备"),
            "lane": meta.get("lane", "yanxue"), "desc": meta.get("desc", "")}


def lane_for_device(device=None):
    """某账号(设备)的赛道 id。查不到 → yanxue(默认)。"""
    return account_meta(device)["lane"]


def get_active_track(device=None):
    """当前赛道 id。
    ★ 账号驱动: 有 device/当前账号 → 按账号 lane; 无 → 全局兜底(兼容旧调用)。
    说明: lanes.yaml 不再维护全局 active, 但仍兼容读旧的 active 字段作兜底。"""
    if device:
        return lane_for_device(device)
    # 默认取第一个账号的 lane(代表"你正在看的那个账号")
    acc = list_accounts()
    if acc:
        first = list(acc.keys())[0]
        return lane_for_device(first)
    return (_lanes().get("active") or "yanxue")


def get_track_meta(track=None, device=None):
    """某赛道元信息 {id,name,desc,source}。track 缺省用当前(账号推导)。"""
    t = track or get_active_track(device)
    meta = (list_tracks().get(t) or {})
    default_source = "dm_unknown"
    src = meta.get("source") or default_source
    return {"id": t, "name": meta.get("name", t), "desc": meta.get("desc", ""),
            "source": src}


def get_track_source(track=None, device=None):
    """当前赛道对应的『来源视角』(用于诊断器 source, 影响话术切入角度)。
    yanxue→xueyan_ai ; opc→opc_recruit ; ai_training→ai_training ; 未定义→dm_unknown。"""
    return get_track_meta(track, device)["source"]


def get_default_kws(track=None, device=None):
    """当前赛道(账号推导)的『默认搜索词』列表——主动获客搜索词跟赛道走。
    ★ 词库联动: 账号=OPC→ AI副业/接单词; 账号=研学→研学/带团词;
      账号=ai_training→学AI/提效词。前端切账号自动带出, 可手动覆盖。
    若赛道未配 default_kws → 回退该赛道的人群关键词(从 proactive_targets 抽 or 常见词)。
    无赛道信息 → 返回 []。"""
    t = track or get_active_track(device)
    meta = list_tracks().get(t) or {}
    kws = meta.get("default_kws") or []
    if kws:
        return kws
    # 兜底: 按赛道给一组稳健的常见词(示例, 请按你实际业务改写)
    _fallback = {
        "xueyan": ["研学", "带团", "讲解词"],
        "opc": ["AI副业", "AI接单"],
        "ai_training": ["学AI", "AI技能"],
    }
    return _fallback.get(t, [])


# ---------------------------------------------------------------- 赛道差异叠加
def _diff_path(basename, device=None):
    """当前赛道(账号推导)差异文件路径 tracks/<id>/<basename>; 不存在返回 None。"""
    t = get_active_track(device)
    fp = os.path.join(_TRACKS_DIR, t, basename)
    return fp if os.path.exists(fp) else None


def load_yaml_for_track(basename, default_dir=None, device=None):
    """读某个配置, 按当前赛道(账号推导)【叠加差异】:
      - 默认文件(默认目录) 完整读入;
      - 若赛道差异文件存在, 深合并覆盖(赛道优先);
      - 返回合并后 dict(缺省时返回默认文件的 dict)。
    default_dir 缺省 = classify/。
    """
    ddir = default_dir or _CLASSIFY
    base = _load_yaml(os.path.join(ddir, basename)) or {}
    diff = _load_yaml(_diff_path(basename, device)) or {}
    if not diff:
        return base
    replace_top = diff.pop("_replace_top", None) or []
    if replace_top:
        for k in replace_top:
            if k in diff:
                base[k] = diff.pop(k)
    return _deep_merge(base, diff)


def _deep_merge(a, b):
    """b 覆盖 a(递归 dict)。list 整体替换(b 的为准)。"""
    if not isinstance(a, dict) or not isinstance(b, dict):
        return b
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# ---------------- 设备→账号昵称 映射 ----------------
def device_name(device_id):
    """把采集/回复时写的 device 值翻译成小红书账号昵称(供 Console 设备卡显示)。
       device_id 可能是 adb 序列号(YOUR_DEVICE_SERIAL) 或 别名(默认设备)。
       查不到映射 → 原样返回(前端可回退显示原值)。"""
    if not device_id:
        return device_id or "默认设备"
    meta = account_meta(device_id)
    # account_meta 对未知 device 会回退第一个账号 → 需精确判断
    acc = list_accounts()
    if device_id in acc:
        return acc[device_id].get("nick", device_id)
    return device_id


def device_map():
    """返回完整设备→账号对象映射(供前端一次性渲染多设备卡)。"""
    return (list_accounts())


if __name__ == "__main__":
    print("accounts      =", device_map())
    print("device_name(YOUR_DEVICE_SERIAL_1) =", device_name("YOUR_DEVICE_SERIAL_1"))
    print("lane(YOUR_DEVICE_SERIAL_1) =", lane_for_device("YOUR_DEVICE_SERIAL_1"))
    print("current track =", get_active_track())
    print("source        =", get_track_source())
