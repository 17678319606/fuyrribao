#!/usr/bin/env python3
"""步骤5（通知）：把每日流水线结果汇总推送到企业微信机器人（Webhook）。

触发策略（在 run_daily.py 中决定）：
  - 任意步骤「失败」→ 立即告警（成功/失败提醒）；
  - 当日「末次触发」且整体成功 → 汇总一条日报摘要；
  - 其余情况（如晨间 6:00 成功）→ 不发，避免刷屏。

依赖环境变量：
  - WXWORK_WEBHOOK：企业微信机器人 Webhook 地址（仓库环境变量 / Secrets，绝不硬编码）。未配置则跳过。

消息格式：企业微信 markdown（支持 # / **粗体** / > 引用 / 行内 code）。
安全：Webhook Key 仅来自环境变量。
"""
import os
import sys
import json
import datetime
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

WEBHOOK = (os.environ.get("WXWORK_WEBHOOK") or "").strip()


def _post_markdown(content):
    if not WEBHOOK:
        print("NOTIFY: 未配置 WXWORK_WEBHOOK，跳过企业微信通知。")
        return False
    body = json.dumps({"msgtype": "markdown", "markdown": {"content": content}},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(WEBHOOK, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=15)
        d = json.loads(r.read().decode("utf-8", "ignore"))
        if d.get("errcode", 0) != 0:
            print("NOTIFY: 企业微信返回错误 %s" % d)
            return False
        print("NOTIFY: 企业微信通知已发送")
        return True
    except Exception as e:
        print("NOTIFY: 发送失败 %s" % e)
        return False


def _step_line(name, st):
    if st is None:
        return "- %s：⏭ 跳过（前置失败）" % name
    ok = st.get("ok")
    if ok is True:
        return "- %s：✅ 成功" % name
    if ok is False:
        return "- %s：❌ 失败" % name
    return "- %s：ℹ️ %s" % (name, (st.get("tail", "") or "")[:60])


def send(status):
    """根据 run_daily.py 写入的 status 字典，构建并发送一条 markdown 通知。"""
    date = status.get("date") or C.date_str()
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%H:%M")
    overall = "✅ 成功" if status.get("ok") else "❌ 失败"

    lines = [
        "## 副业日报 · 流水线通知",
        "> 日期：**%s**  %s  ｜ 总体：**%s**" % (date, now, overall),
        "",
    ]

    steps = status.get("steps", {})
    for k in ("fetch", "generate", "wp", "wechat"):
        if k in steps:
            lines.append(_step_line(k, steps[k]))

    w = steps.get("wechat")
    if w and w.get("ok") is True:
        tail = w.get("tail", "")
        if "跳过" in tail:
            lines.append("> 公众号未配置，本次仅发布 WordPress。")
        elif "freepublish" in tail or "draft" in tail:
            lines.append("> ⚠️ 公众号已发布/存草稿，**请到公众号后台手动点「群发」**完成当日推送。")
        else:
            lines.append("> 公众号已自动群发 / 发布。")
    elif w and w.get("ok") is False:
        lines.append("> 公众号推送失败，请检查 WX_APPID / WX_APPSECRET / 模式权限。")
    elif status.get("final_run") is False:
        lines.append("> 公众号将在当日末次触发（19:00）推送。")

    # 末次汇总：附日报摘要
    if status.get("final_run") and status.get("ok"):
        try:
            rep = C.load_json(os.path.join(C.DATA_DIR, "report-%s.json" % date), {})
            mods = rep.get("modules", {})
            total = sum(len(mods.get(k, [])) for k in
                        ("project_opportunities", "growth_operations", "views_insights"))
            lines.append("")
            lines.append("**今日日报共 %d 条**（项目机会 / 增长运营 / 观点心法）。" % total)
            meth = rep.get("daily_summary", {}).get("methodology", "")
            if meth:
                lines.append("> 方法论：%s" % meth[:120])
        except Exception:
            pass

    content = "\n".join(lines)
    # 企业微信 markdown 上限 4096 字节，超长截断
    if len(content.encode("utf-8")) > 4000:
        content = content[:1800] + "\n> …（内容过长已截断）"
    return _post_markdown(content)


if __name__ == "__main__":
    # 独立运行时：读 state/run_status.json 发送（便于手动补发通知）
    p = os.path.join(C.STATE_DIR, "run_status.json")
    if os.path.exists(p):
        send(C.load_json(p, {}))
    else:
        print("NOTIFY: 未找到 run_status.json，无内容可通知。")
