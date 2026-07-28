#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
feishu_sync.py — 工作台任务 -> 飞书日历 同步脚本
=====================================================
读取工作台多设备同步数据(JSONBlob)，把"带截止时间且未完成"的任务
写入专属飞书日历「工作台任务」，并在截止时刻弹提醒；任务完成或删除
时自动从日历移除对应事件。用 id 映射避免重复创建。

前置条件：
  - 已用 `lark-cli auth login --domain calendar,task` 完成用户授权(本机)
  - 已安装 lark-cli 且在 PATH 中

用法：
  python feishu_sync.py --sync-code <你的同步码> [--dry-run]
  python feishu_sync.py --sync-code <码> --calendar-id <已有日历ID>   # 指定日历
  python feishu_sync.py --sync-code <码> --list-calendars            # 仅列出日历
  python feishu_sync.py --json-file <工作台导出的备份.json>          # 直接读本地备份(绕过 file:// 同步失败)

环境变量(可选)：
  SYNC_CODE            同步码(JSONBlob ID)
  FEISHU_CALENDAR_NAME 日历名(默认「工作台任务」)
  FEISHU_LEAD_MINUTES   提前提醒分钟数(默认 0 = 截止时刻提醒)
  FEISHU_MAP_FILE       id映射本地文件(默认 ./feishu_sync_map.json)
  LARK_CLI              lark-cli 路径(默认 lark-cli)
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

SYNC_API = "https://jsonblob.com/api/jsonBlob"
CALENDAR_NAME = os.environ.get("FEISHU_CALENDAR_NAME", "工作台任务")
LEAD_MINUTES = int(os.environ.get("FEISHU_LEAD_MINUTES", "0"))
MAP_FILE = os.environ.get("FEISHU_MAP_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "feishu_sync_map.json"))
LARK = os.environ.get("LARK_CLI", "lark-cli")
TZ = "Asia/Shanghai"


def resolve_lark():
    """在 Windows 上 lark-cli 实为 lark-cli.cmd，subprocess 需完整路径。"""
    global LARK
    if os.name == "nt":
        # Windows 优先找 .cmd/.exe/.ps1，避免选中无后缀的 sh 脚本
        for cand in [LARK + ".cmd", LARK + ".exe", LARK + ".ps1", LARK]:
            found = shutil.which(cand)
            if found:
                LARK = found
                return
        for base in [os.path.expandvars(r"%APPDATA%\npm"),
                     r"C:\Users\Lenovo\.workbuddy\binaries\node\cli-connector-packages"]:
            for ext in (".cmd", ".exe", ".ps1", ""):
                p = os.path.join(base, "lark-cli" + ext)
                if os.path.exists(p) and not os.path.isdir(p):
                    LARK = p
                    return
    else:
        found = shutil.which(LARK)
        if found:
            LARK = found


def lark(args, dry_run=False):
    """调用 lark-cli，返回解析后的 JSON(dict)。"""
    resolve_lark()
    cmd = [LARK] + args
    if dry_run:
        cmd.append("--dry-run")
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"lark-cli 超时: {' '.join(cmd)}")
    if out.returncode != 0 and not dry_run:
        # 某些成功命令也会返回非0(如 --dry-run 打印 request)，以 stderr/stdout 判断
        raise RuntimeError(f"lark-cli 失败({out.returncode}): {out.stderr.strip() or out.stdout.strip()}")
    text = out.stdout.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text}


def fetch_db(sync_code):
    url = f"{SYNC_API}/{sync_code}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def put_db(sync_code, db):
    url = f"{SYNC_API}/{sync_code}"
    data = json.dumps(db).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="PUT",
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status


def load_map():
    if os.path.exists(MAP_FILE):
        try:
            with open(MAP_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"events": {}, "calendar_id": None}


def save_map(m):
    with open(MAP_FILE, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)


def ensure_calendar(calendar_id, dry_run):
    """返回要使用的日历ID：优先用传入的，否则查找/创建「工作台任务」。"""
    if calendar_id:
        return calendar_id
    m = load_map()
    if m.get("calendar_id"):
        return m["calendar_id"]
    # 查找已有同名日历
    resp = lark(["calendar", "calendars", "list", "--as", "user"])
    for c in resp.get("data", {}).get("calendar_list", []):
        if c.get("summary") == CALENDAR_NAME:
            cid = c["calendar_id"]
            m["calendar_id"] = cid
            save_map(m)
            return cid
    # 创建
    resp = lark(["calendar", "calendars", "create", "--as", "user",
                 "--data", json.dumps({"summary": CALENDAR_NAME,
                                       "description": "曾生工作台·任务截止提醒同步日历",
                                       "permissions": "public"})])
    cid = resp.get("data", {}).get("calendar", {}).get("calendar_id")
    if not cid:
        raise RuntimeError(f"创建日历失败: {resp}")
    m["calendar_id"] = cid
    save_map(m)
    return cid


def parse_due(due):
    """解析 datetime-local 'YYYY-MM-DDTHH:mm' -> 上海时区 datetime。"""
    if not due:
        return None
    try:
        dt = datetime.strptime(due, "%Y-%m-%dT%H:%M")
        return dt.replace(tzinfo=timezone(timedelta(hours=8)))
    except Exception:
        return None


def to_ts(dt):
    return int(dt.timestamp())


def create_event(calendar_id, task, dry_run):
    dt = parse_due(task.get("due"))
    if not dt:
        return None
    start_ts = to_ts(dt)
    end_ts = start_ts + 30 * 60  # 30分钟时长
    summary = task.get("title") or task.get("name") or "未命名任务"
    assignee = task.get("assignee") or ""
    desc = f"负责人: {assignee}\n来自曾生工作台·截止时刻自动提醒" if assignee else "来自曾生工作台·截止时刻自动提醒"
    body = {
        "summary": summary,
        "description": desc,
        "start_time": {"timestamp": str(start_ts), "timezone": TZ},
        "end_time": {"timestamp": str(end_ts), "timezone": TZ},
        "reminders": [{"minutes": LEAD_MINUTES}],
    }
    resp = lark(["calendar", "events", "create", "--as", "user",
                 "--calendar-id", calendar_id, "--data", json.dumps(body)], dry_run=dry_run)
    if dry_run:
        return {"_dry": True}
    ev = resp.get("data", {}).get("event", {})
    return ev.get("event_id")


def update_event(calendar_id, event_id, task, dry_run):
    dt = parse_due(task.get("due"))
    if not dt:
        return
    start_ts = to_ts(dt)
    end_ts = start_ts + 30 * 60
    summary = task.get("title") or task.get("name") or "未命名任务"
    assignee = task.get("assignee") or ""
    desc = f"负责人: {assignee}\n来自曾生工作台·截止时刻自动提醒" if assignee else "来自曾生工作台·截止时刻自动提醒"
    body = {
        "summary": summary,
        "description": desc,
        "start_time": {"timestamp": str(start_ts), "timezone": TZ},
        "end_time": {"timestamp": str(end_ts), "timezone": TZ},
        "reminders": [{"minutes": LEAD_MINUTES}],
    }
    lark(["calendar", "events", "patch", "--as", "user",
          "--calendar-id", calendar_id, "--event-id", event_id,
          "--data", json.dumps(body)], dry_run=dry_run)


def delete_event(calendar_id, event_id, dry_run):
    if not event_id:
        return
    lark(["calendar", "events", "delete", "--as", "user",
          "--calendar-id", calendar_id, "--event-id", event_id], dry_run=dry_run)


def is_done(task):
    st = str(task.get("status", "")).lower()
    return st in ("done", "completed", "finished", "1", "true") or task.get("done") is True


def load_db_from_file(path):
    """读取工作台「导出整库备份」生成的 JSON 文件，兼容两种格式：
    - {__wb_backup__:true, version:1, db:{...}}  (导出整库备份)
    - {tasks:[...], ...}                          (裸 DB)
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and isinstance(data.get("db"), dict):
        return data["db"]
    if isinstance(data, dict) and "tasks" in data:
        return data
    raise ValueError("无法识别的备份文件格式（缺少 tasks 字段）")


def sync_db(db, calendar_id, dry_run, write_back=True, sync_code=None):
    cal_id = ensure_calendar(calendar_id, dry_run)
    m = load_map()
    events = m.get("events", {})

    tasks = db.get("tasks", []) or []
    # 期望存在的任务：有 due 且未完成
    desired = {}
    for t in tasks:
        tid = t.get("id")
        if not tid:
            continue
        if is_done(t):
            continue
        if not parse_due(t.get("due")):
            continue
        desired[tid] = t

    created = updated = deleted = 0
    # 1) 创建/更新期望任务
    for tid, t in desired.items():
        if tid in events:
            update_event(cal_id, events[tid], t, dry_run)
            updated += 1
        else:
            eid = create_event(cal_id, t, dry_run)
            if eid and "_dry" not in eid:
                events[tid] = eid
            created += 1
    # 2) 删除不再需要(完成/无截止/已删)的事件
    for tid, eid in list(events.items()):
        if tid not in desired:
            delete_event(cal_id, eid, dry_run)
            del events[tid]
            deleted += 1

    m["events"] = events
    if not dry_run:
        save_map(m)
        if write_back:
            db.setdefault("meta", {})
            db["meta"]["feishuStatus"] = {
                "enabled": True,
                "lastSync": int(datetime.now().timestamp() * 1000),
                "eventCount": len(events),
                "message": f"已同步 {len(events)} 个任务到飞书日历",
            }
            try:
                put_db(sync_code, db)
            except Exception as e:
                print(f"[warn] 写回 feishuStatus 失败(不影响飞书): {e}")

    return {"calendar_id": cal_id, "created": created, "updated": updated,
            "deleted": deleted, "total": len(events), "dry_run": dry_run}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sync-code", default=os.environ.get("SYNC_CODE"))
    ap.add_argument("--json-file", default=None,
                   help="直接读取工作台导出的整库备份 JSON 文件(绕过 file:// 同步限制)")
    ap.add_argument("--calendar-id", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list-calendars", action="store_true")
    ap.add_argument("--no-write-back", action="store_true")
    args = ap.parse_args()

    if args.list_calendars:
        resp = lark(["calendar", "calendars", "list", "--as", "user"])
        for c in resp.get("data", {}).get("calendar_list", []):
            print(f"{c.get('calendar_id')}  |  {c.get('summary')}  ({c.get('type')})")
        return

    # 确定数据来源：本地备份文件 优先，否则云端同步码
    if args.json_file:
        db = load_db_from_file(args.json_file)
        sync_code = args.sync_code
        write_back = bool(sync_code) and not args.no_write_back
    else:
        if not args.sync_code:
            print("错误：请提供 --sync-code 或 --json-file")
            sys.exit(1)
        db = fetch_db(args.sync_code)
        sync_code = args.sync_code
        write_back = not args.no_write_back

    result = sync_db(db, args.calendar_id, args.dry_run,
                     write_back=write_back, sync_code=sync_code)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
