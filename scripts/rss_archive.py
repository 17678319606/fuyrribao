#!/usr/bin/env python3
"""Fetch configured RSS/Atom sources, keep the last 24 hours, and emit a stable JSON feed."""
import json, os, sys, time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import feedparser
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES = os.path.join(ROOT, "scripts", "sources.json")
OUT = os.path.join(ROOT, "data", "rss", "daily.json")
NOW = datetime.now(timezone.utc)
CUTOFF = NOW - timedelta(hours=24)
UA = "fuyrribao-rss-archive/1.0 (+https://github.com/17678319606/fuyrribao)"

def dt(entry):
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            return datetime.fromtimestamp(time.mktime(value), timezone.utc)
    for key in ("published", "updated", "created"):
        value = entry.get(key)
        if value:
            try:
                d = parsedate_to_datetime(value)
                return d.astimezone(timezone.utc) if d.tzinfo else d.replace(tzinfo=timezone.utc)
            except Exception:
                pass
    return None

def fetch(cfg):
    url = cfg.get("url", "")
    if cfg.get("type") != "rss" or not url:
        return [], "not_rss"
    r = requests.get(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"}, timeout=(15, 45))
    r.raise_for_status()
    parsed = feedparser.parse(r.content)
    if getattr(parsed, "bozo", False) and not parsed.entries:
        raise RuntimeError("invalid RSS/Atom: " + str(getattr(parsed, "bozo_exception", "unknown")))
    rows = []
    for e in parsed.entries:
        published = dt(e)
        if published is None or published < CUTOFF or published > NOW + timedelta(hours=1):
            continue
        link = e.get("link") or e.get("id")
        title = (e.get("title") or "").strip()
        if not link or not title:
            continue
        summary = e.get("summary") or ""
        rows.append({"id": link, "source_id": cfg.get("id"), "source_name": cfg.get("name"), "title": title, "url": link, "summary": summary, "published_at": published.isoformat()})
    return rows, "ok"

def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    sources = json.load(open(SOURCES, encoding="utf-8"))
    articles, health = [], {}
    for cfg in sources:
        sid = cfg.get("id", cfg.get("name", "unknown"))
        try:
            rows, reason = fetch(cfg)
            articles.extend(rows)
            health[sid] = {"ok": True, "reason": reason, "count": len(rows), "url": cfg.get("url", "")}
        except Exception as exc:
            health[sid] = {"ok": False, "reason": f"{type(exc).__name__}: {exc}", "count": 0, "url": cfg.get("url", "")}
    unique = {a["id"]: a for a in articles}
    articles = sorted(unique.values(), key=lambda x: x["published_at"], reverse=True)
    payload = {"generated_at": NOW.isoformat(), "window": {"hours": 24, "from": CUTOFF.isoformat(), "to": NOW.isoformat()}, "count": len(articles), "articles": articles, "health": health}
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, OUT)
    failed = sum(1 for v in health.values() if not v["ok"])
    empty = sum(1 for v in health.values() if v["ok"] and v["count"] == 0)
    print(json.dumps({"articles": len(articles), "failed": failed, "empty": empty}, ensure_ascii=False))

if __name__ == "__main__":
    main()
