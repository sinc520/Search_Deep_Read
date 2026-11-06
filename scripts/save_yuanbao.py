#!/usr/bin/env python3
"""把内置 WebSearch（腾讯元宝）的结果存成与 search.py 同构的存档。

## 为什么需要
内置 WebSearch 的结果只进上下文，不落盘：长会话里工具结果会被 microcompact 清掉，
跨会话更是看不到。于是「上周那个查询元宝给了哪几条」只能靠翻会话记录考古，
也无法与 DeepSeek 的结果放在一起比对。

## 数据从哪来
WorkBuddy 的会话记录 `~/.workbuddy/projects/<project>/<session>.jsonl` 里，
`type=function_call_result` 且 `name=WebSearch` 的记录，`output.text` 就是完整结果
（`# Search Results for "..."` 开头）。
**脚本读的是磁盘上已有的记录，不需要重新输出一遍文本，所以存档的上下文成本为零。**

## 已知风险
这条链路依赖会话记录的内部格式（`function_call_result` / `output.text`）。
客户端升级后若失效，脚本会明确提示「会话格式可能已变」，并可用 `--from-file` 手工兜底
（把结果文本存成文件再喂给它）。

## 用法
  python save_yuanbao.py                # 存最近一次 WebSearch
  python save_yuanbao.py --list         # 列出最近 10 次，带编号与「已存」标记
  python save_yuanbao.py --pick 2       # 存倒数第 2 次
  python save_yuanbao.py --print        # 只解析、打印摘要，不写盘
  python save_yuanbao.py --from-file r.txt
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

# 与 search.py 共用同一套路径解析与文件名规则，避免「两处各写一份」而漂移
sys.path.insert(0, str(Path(__file__).resolve().parent))
from search import _slug, resolve_archive_root  # noqa: E402

PROJECTS_DIR = Path.home() / ".workbuddy" / "projects"
RESULT_MARK = "# Search Results for"
INDEX_NAME = "index.jsonl"

# 结果条目的标题行：`## 1. [标题](链接)`
HEAD_RE = re.compile(r"^## (\d+)\. \[(.*?)\]\((.*?)\)\s*$", re.M)


# --------------------------------------------------------------------------- #
# 1. 从会话记录里找 WebSearch 结果
# --------------------------------------------------------------------------- #
def discover_sessions(all_projects: bool, limit: int) -> list[Path]:
    """按修改时间倒序列出会话记录。默认只看最近若干个文件，避免全盘扫描。"""
    if not PROJECTS_DIR.is_dir():
        return []
    files = sorted(PROJECTS_DIR.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files if all_projects else files[:limit]


def iter_results(files: list[Path]) -> Iterator[dict[str, Any]]:
    """从 jsonl 里抽出 WebSearch 的结果记录。

    逐行流式读取：会话文件可达 10 MB 以上，整文件 json.loads 没有必要。
    """
    for path in files:
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    # 先做两次廉价子串判断，命中才付出解析成本
                    if '"WebSearch"' not in line or RESULT_MARK not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    out = obj.get("output")
                    text = out.get("text") if isinstance(out, dict) else None
                    if not isinstance(text, str) or not text.startswith(RESULT_MARK):
                        continue
                    yield {
                        "session": path,
                        "call_id": str(obj.get("callId") or ""),
                        "ts": obj.get("timestamp") or 0,
                        "cwd": str(obj.get("cwd") or ""),
                        "text": text,
                    }
        except OSError:
            continue


def _norm_dir(value: str) -> str:
    return value.replace("\\", "/").rstrip("/").lower()


def pick_records(all_projects: bool, need: int) -> list[dict[str, Any]]:
    """取最近的若干条 WebSearch 结果，默认只保留当前项目的。"""
    records = list(iter_results(discover_sessions(all_projects, limit=20)))
    records.sort(key=lambda r: r["ts"], reverse=True)

    if not all_projects:
        project = os.environ.get("CODEBUDDY_PROJECT_DIR") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        target = _norm_dir(project)
        scoped = [r for r in records if _norm_dir(r["cwd"]) == target]
        # 老记录可能没有 cwd 字段：一个都没匹配上时不筛，交给 --list 人工确认
        if scoped:
            records = scoped
    return records[:need]


# --------------------------------------------------------------------------- #
# 2. 解析结果文本
# --------------------------------------------------------------------------- #
def parse_text(text: str) -> tuple[str, str, int, list[dict[str, Any]]]:
    """返回 (query, 检索日期, provider, 结果列表)。

    元宝只给一档文本（snippet 量级），没有 summary/content 分层，
    也没有 authority / rerank_score —— 这些字段留空，不编造。
    """
    query = ""
    head = re.match(r'# Search Results for "(.*?)"', text)
    if head:
        query = head.group(1)

    search_date = ""
    date_m = re.search(r"Search performed on (\d{4}-\d{2}-\d{2})", text)
    if date_m:
        search_date = date_m.group(1)

    provider = 0
    prov_m = re.search(r"Provider:\s*(\d+)", text)
    if prov_m:
        provider = int(prov_m.group(1))

    marks = list(HEAD_RE.finditer(text))
    rows: list[dict[str, Any]] = []
    for i, mark in enumerate(marks):
        start = mark.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        block = text[start:end]

        url_m = re.search(r"\*\*URL:\*\*\s*(\S+)", block)
        url = (url_m.group(1) if url_m else mark.group(3)).strip()
        snippet = block.split("**URL:**")[0]
        snippet = re.sub(r"\s+", " ", snippet).strip(" -\n\t")

        host = urlsplit(url).netloc or url
        rows.append({
            "title": mark.group(2).strip() or host,
            "url": url,
            "site_name": host,
            "date": "",              # 元宝不给条目日期；检索日期记在存档顶层 search_date
            "authority": None,       # 该接口没有权威度分级
            "rerank_score": None,    # 也没有相关度打分
            "rank": int(mark.group(1)),  # 引擎返回顺序即排名
            "snippet": snippet,
            "summary": "",           # 没有更高一档的文本
        })
    return query, search_date, provider, rows


# --------------------------------------------------------------------------- #
# 3. 落盘
# --------------------------------------------------------------------------- #
def archived_path(root: Path, call_id: str) -> Path | None:
    """按 call_id 判断是否已存过，返回已有文件路径。"""
    index = root / INDEX_NAME
    if not index.exists():
        return None
    for line in index.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if entry.get("engine") == "yuanbao" and entry.get("call_id") == call_id:
            return Path(entry.get("file") or "")
    return None


def write_archive(
    root: Path, record: dict[str, Any], query: str, rows: list[dict[str, Any]],
    provider: int, search_date: str,
) -> Path:
    ts = record.get("ts") or 0
    moment = datetime.fromtimestamp(ts / 1000) if ts else datetime.now()
    digest = hashlib.sha1(f"{query}|{record.get('call_id')}".encode("utf-8")).hexdigest()[:4]
    path = root / moment.strftime("%Y-%m-%d") / f"yuanbao-{_slug(query)}-{moment.strftime('%H%M%S')}-{digest}.json"

    payload = {
        "schema": "search-archive/1",
        "engine": "yuanbao",
        "created_at": moment.astimezone().isoformat(timespec="seconds"),
        "query_raw": query,
        "query_final": query,
        "endpoint": "builtin WebSearch (Tencent Yuanbao)",
        "request": {"query": query},
        "elapsed_ms": None,
        "search_date": search_date,
        "provider_id": provider,
        "api_returned": len(rows),
        "kept_after_local_filter": len(rows),
        "filters": {"min_authority": 0},
        "results": rows,
        "provenance": {
            "session_file": str(record.get("session") or ""),
            "call_id": record.get("call_id") or "",
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    index_entry = {
        "at": payload["created_at"],
        "engine": "yuanbao",
        "query": query,
        "count_requested": None,
        "api_returned": len(rows),
        "kept": len(rows),
        "min_authority": 0,
        "call_id": record.get("call_id") or "",
        "file": str(path),
    }
    with (root / INDEX_NAME).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(index_entry, ensure_ascii=False) + "\n")
    return path


# --------------------------------------------------------------------------- #
# 4. CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="把内置 WebSearch（元宝）的结果存成与 search.py 同构的存档",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python save_yuanbao.py            # 存最近一次\n"
               "  python save_yuanbao.py --list     # 先看有哪些，再 --pick\n",
    )
    p.add_argument("--list", nargs="?", const=10, type=int, metavar="N",
                   help="列出最近 N 次 WebSearch（默认 10），不写盘")
    p.add_argument("--pick", type=int, default=1, metavar="N",
                   help="选倒数第 N 次（默认 1 = 最近一次）")
    p.add_argument("--from-file", dest="from_file", metavar="PATH",
                   help="不读会话记录，直接解析给定文本文件（手工兜底）")
    p.add_argument("--print", dest="dry_run", action="store_true",
                   help="只解析并打印摘要，不写盘")
    p.add_argument("--force", action="store_true",
                   help="同一 call_id 已存过时仍然重写")
    p.add_argument("--all", dest="all_projects", action="store_true",
                   help="跨项目扫描（默认只看当前项目的会话）")
    p.add_argument("--out", metavar="PATH", help="指定存档文件路径")
    g = p.add_argument_group("存档位置")
    g.add_argument("--archive-dir", dest="archive_dir", metavar="PATH",
                   help="存档根目录，默认 <项目>/.workbuddy/search-results")
    g.add_argument("--archive-scope", dest="archive_scope", choices=["project", "global"],
                   default="project", help="project（默认）或 global")
    return p


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)

    root = resolve_archive_root(args.archive_dir, args.archive_scope)

    # 手工兜底：直接读文本文件
    if args.from_file:
        path = Path(args.from_file).expanduser()
        if not path.is_file():
            raise SystemExit(f"文本文件不存在: {path}")
        text = path.read_text(encoding="utf-8", errors="replace")
        record: dict[str, Any] = {"session": path, "call_id": "", "ts": 0, "cwd": ""}
        chosen = [record]
        records = chosen
        text_of = lambda r: text  # noqa: E731
    else:
        records = pick_records(args.all_projects, need=max(args.list or 0, args.pick, 1))
        if not records:
            raise SystemExit(
                "没有在会话记录里找到 WebSearch 结果。\n"
                "可能原因：① 本会话还没调用过内置 WebSearch；② 客户端升级后会话格式变了。\n"
                "兜底办法：把结果文本存成文件，用 --from-file 解析。"
            )
        text_of = lambda r: r["text"]  # noqa: E731

    # --list：只列不写
    if args.list is not None:
        n = max(1, args.list)
        print(f"最近 {len(records[:n])} 次 WebSearch（用 --pick N 选择，N 从 1 = 最近 开始）：")
        for i, r in enumerate(records[:n], 1):
            q, _, _, rows = parse_text(r["text"])
            ts = r.get("ts") or 0
            when = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M") if ts else "(时间未知)"
            mark = "已存" if r.get("call_id") and archived_path(root, r["call_id"]) else "未存"
            print(f"  {i}. {when}  {len(rows):>2} 条  {len(r['text']):>6} 字符  [{mark}]  {q[:60]}")
        return 0

    record = records[args.pick - 1]
    query, search_date, provider, rows = parse_text(text_of(record))

    if not rows:
        raise SystemExit(
            "解析出 0 条结果。会话格式可能已变，或这次 WebSearch 返回的是空结果。\n"
            "请把原文存成文件用 --from-file 复核，不要按 0 条存档。"
        )

    print(f"query: {query or '(未解析到)'}")
    print(f"结果: {len(rows)} 条 | 原始文本 {len(text_of(record))} 字符 | 检索日期 {search_date or '(未标注)'}")
    print("snippet 长度: " + ", ".join(str(len(r["snippet"])) for r in rows[:12])
          + (" …" if len(rows) > 12 else ""))

    if args.dry_run:
        print("\n--print：未写盘。前 3 条：")
        for r in rows[:3]:
            print(f"  #{r['rank']} {r['title'][:50]} | {r['site_name']} | {len(r['snippet'])} 字符")
        return 0

    # 已存过就跳过，避免同一批结果被反复落盘
    if not args.force and record.get("call_id"):
        existing = archived_path(root, record["call_id"])
        if existing:
            print(f"\n已存过，跳过: {existing}")
            print("（要覆盖请加 --force）")
            return 0

    if args.out:
        target = Path(args.out).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        saved = write_archive(root, record, query, rows, provider, search_date)
        if saved != target:
            target.write_text(saved.read_text(encoding="utf-8"), encoding="utf-8")
            saved.unlink()
            saved = target
    else:
        saved = write_archive(root, record, query, rows, provider, search_date)

    print(f"saved: {saved}")
    print("（engine=yuanbao；无 authority/rerank_score，dump.py 会显示为 —）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
