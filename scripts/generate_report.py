#!/usr/bin/env python3
"""步骤2：调用 AI（OpenAI 兼容，ai.jinbufenzi.com/v1）把候选信号结构化为日报 JSON。
系统提示直接复用仓库根目录 SKILL.md，保证「AI 照字段填」。
"""
import os
import sys
import json
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

LOG = C.get_logger()


def _extract_json(text):
    """从模型输出中提取合法 JSON（兼容 ```json 围栏）。"""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e != -1:
        text = text[s:e + 1]
    return json.loads(text)


def _validate(report):
    assert isinstance(report, dict), "顶层不是对象"
    mods = report.get("modules", {})
    for key in ("project_opportunities", "growth_operations", "views_insights"):
        assert key in mods, f"缺少模块 {key}"
        assert isinstance(mods[key], list), f"{key} 不是数组"
    assert "daily_summary" in report, "缺少 daily_summary"
    return True


def generate(candidates, skill_text):
    import requests

    base = os.environ.get("AI_BASE_URL", "https://ai.jinbufenzi.com/v1").rstrip("/")
    key = os.environ.get("AI_API_KEY", "")
    model = os.environ.get("AI_MODEL", "auto")

    user_prompt = (
        "今天是 " + C.date_str() + "（北京时间）。以下是已完成去重的当日增量信号（JSON）：\n"
        + json.dumps(candidates, ensure_ascii=False)
        + "\n请按 SKILL 规则，输出 ai-sidehustle-report 日报 JSON。"
    )
    payload = {
        "model": model,
        "temperature": 0.5,
        "max_tokens": 16000,
        "messages": [
            {"role": "system", "content": skill_text},
            {"role": "user", "content": user_prompt},
        ],
    }
    headers = {"Authorization": f"Bearer {key}",
               "Content-Type": "application/json"}
    last_err = None
    for attempt in range(3):
        try:
            r = requests.post(base + "/chat/completions", headers=headers,
                              json=payload, timeout=120)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            report = _extract_json(content)
            _validate(report)
            return report
        except Exception as e:
            last_err = e
            LOG.warning("AI 生成第 %d 次失败: %s", attempt + 1, e)
            time.sleep(5)
    raise RuntimeError(f"AI 生成失败: {last_err}")


def main():
    C.ensure_dirs()
    today = C.date_str()
    cand_path = os.path.join(C.DATA_DIR, f"candidates-{today}.json")
    candidates = C.load_json(cand_path, [])
    if not candidates:
        LOG.info("今日无候选信号，跳过生成与发布。")
        C.save_json(os.path.join(C.DATA_DIR, f"report-{today}.json"),
                    {"date": today, "timezone": "Asia/Shanghai",
                     "modules": {"project_opportunities": [], "growth_operations": [],
                                 "views_insights": []},
                     "daily_summary": {"methodology": "今日无新增信号", "evidence": []}})
        return

    skill_text = ""
    try:
        with open(C.SKILL_FILE, "r", encoding="utf-8") as f:
            skill_text = f.read()
    except Exception:
        skill_text = "你是严谨的副业日报编辑，按 ai-sidehustle-report schema 输出 JSON。"

    report = generate(candidates, skill_text)
    report.setdefault("date", today)
    report.setdefault("timezone", "Asia/Shanghai")
    out = os.path.join(C.DATA_DIR, f"report-{today}.json")
    C.save_json(out, report)
    LOG.info("日报 JSON 已生成: %s（模块计数: 项目%d/增长%d/观点%d）",
             out,
             len(report["modules"]["project_opportunities"]),
             len(report["modules"]["growth_operations"]),
             len(report["modules"]["views_insights"]))


if __name__ == "__main__":
    main()
