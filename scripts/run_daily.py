#!/usr/bin/env python3
"""每日编排入口（单仓双推 + 通知 + 状态持久化）。

按顺序执行：采集 → AI生成 → 发WP → 发公众号 → 通知。
特点：
  - AI 只跑一次（generate_report 内部增量，仅新信号调 AI）；
  - 任意步骤失败都不阻断后续无关步骤（WP 失败不影响公众号，反之亦然）；
  - 通知在 finally 中**必定执行**（成功/失败都报）；
  - 当日 state（增量累积、已推送标记、运行状态）提交回仓库，供下次增量与跨触发去重。

通知策略（避免刷屏）：
  - 整体失败 → 立即告警；
  - 当日末次触发且成功 → 汇总一条日报摘要；
  - 晨间成功 → 不发。

退出码：关键步骤（generate/wp/wechat）任一失败 → 非 0（CNB 构建标红）；通知已在 finally 中发出。
"""
import os
import sys
import json
import subprocess
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
import notify
import source_manager as SM

HERE = os.path.dirname(os.path.abspath(__file__))
STEPS = [
    ("fetch", "fetch_signals.py"),
    ("generate", "generate_report.py"),
    ("wp", "publish_wp.py"),
    ("wechat", "publish_wechat.py"),
]

BJ = datetime.timezone(datetime.timedelta(hours=8))


def _run(script):
    # 用 -u 关闭子进程缓冲，并把子进程 stdout/stderr 实时回显到主进程 stdout，
    # 使 CNB 日志能完整捕获各步骤真实输出（之前 capture_output 把输出吞掉，导致"假成功"）。
    p = subprocess.run([sys.executable, "-u", os.path.join(HERE, script)],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out = (p.stdout or "").strip()
    print("\n===== %s (rc=%d) =====" % (script, p.returncode), flush=True)
    print(out, flush=True)
    print("===== /%s =====" % script, flush=True)
    return p.returncode == 0, out


def _heartbeat_push():
    """把当日 state/ 提交回仓库（增量累积 / 已推送标记持久化）。失败不影响主流程。"""
    try:
        repo = os.environ.get("CNB_REPO", "jinbufenzi/fuyrribao")
        token = os.environ.get("CNB_TOKEN", "")
        url = "https://cnb:%s@cnb.cool/%s.git" % (token, repo) if token else ""
        os.system('git config user.email "bot@cnb.cool"')
        os.system('git config user.name "cnb-bot"')
        os.system("git add -A state/ 2>/dev/null")
        os.system('git diff --cached --quiet || git commit -m "daily: %s [heartbeat]"' % C.date_str())
        if url:
            os.system('git push "%s" HEAD:main 2>&1 | tail -3' % url)
    except Exception as e:
        print("HEARTBEAT: 状态回写失败（不影响主流程）: %s" % e)


def main():
    C.ensure_dirs()
    now = datetime.datetime.now(BJ)
    today = now.strftime("%Y-%m-%d")
    final_hour = int((os.environ.get("WX_FINAL_HOUR", "19") or "19").strip() or "19")
    final_run = now.hour >= final_hour

    status = {
        "date": today,
        "final_run": final_run,
        "ok": True,
        "steps": {},
    }

    try:
        # 公众号推送已移交 WordPress 插件（fuyr-wechat-pusher）在源站处理，
        # 设置 WX_PUSH_VIA_WP=1 时跳过 CI 侧 wechat 步骤，避免双重推送。
        push_via_wp = os.environ.get("WX_PUSH_VIA_WP", "").strip().lower() in ("1", "true", "yes")
        for key, script in STEPS:
            if key == "wechat" and push_via_wp:
                status["steps"][key] = {"ok": True,
                                        "tail": "skipped（公众号推送已交由 WordPress 插件 fuyr-wechat-pusher 在源站处理）"}
                continue
            ok, out = _run(script)
            status["steps"][key] = {"ok": ok, "tail": out[-800:]}
            if not ok:
                status["ok"] = False
                # 采集/生成是后续步骤的前置；失败则后续无意义，标记跳过
                if key in ("fetch", "generate"):
                    for k2, _ in STEPS:
                        if k2 not in status["steps"]:
                            status["steps"][k2] = {"ok": None, "tail": "skipped（前置步骤失败）"}
                    break
        # 单条二次发布（默认关闭，FUYR_REPUBLISH=1 才启用；非阻塞，失败不影响主流程）
        try:
            if os.environ.get("FUYR_REPUBLISH", "").strip() in ("1", "true", "True", "yes"):
                _run("republish_items.py")
        except Exception as e:
            print("REPUBLISH: 单条发布失败（不影响主流程）: %s" % e)
    except Exception as e:
        status["ok"] = False
        status["error"] = str(e)
    finally:
        try:
            C.save_json(os.path.join(C.STATE_DIR, "run_status.json"), status)
        except Exception as e:
            print("STATUS: 写 run_status.json 失败: %s" % e)
        # 通知：失败立即报；末次成功汇总；其余不发
        try:
            if (status.get("ok") is False) or status.get("final_run"):
                notify.send(status)
        except Exception as e:
            print("NOTIFY: 调用失败（不影响主流程）: %s" % e)
        # 状态回写（增量累积）
        try:
            _heartbeat_push()
        except Exception:
            pass
        # 源管理系统：每轮扫描死源/观测期毕业，写建议（FUYR_SOURCE_AUTOMATION=1 才落地）
        try:
            _srcs = C.load_json(C.SOURCES_FILE, [])
            SM.maybe_automate(_srcs)
        except Exception as e:
            print("SOURCE_MGR: 自动化调用失败（不影响主流程）: %s" % e)

    # 关键步骤（generate/wp/wechat）任一失败 → 返回非 0，让 CNB 构建标红，
    # 避免"假成功"。通知已在 finally 中发出，红/绿由构建状态与机器人共同体现。
    sys.exit(0 if status.get("ok", True) else 1)


if __name__ == "__main__":
    main()
