#!/usr/bin/env python3
"""把多个搜索存档合并成一个（跨批去重）。

⚠️ 状态：**实验阶段 —— 待测试，尚未并入 search-deep-read 主流程**
（2026-11-04 加入。先落成代码存档，等实测过再决定是否进流程。）

──────────────────────────────────────────────────────────────
【目的】为什么要它
──────────────────────────────────────────────────────────────
并发多个关键词搜索时，各批结果会有交集。合并成一个池子后：
延迟 ≈ 1 份、往返次数 1 次、且所有条目落在**同一个编号视野**里，
"同一件事被不同来源用不同说法描述过几次"才有机会被看出来。

【实测：去重几乎不省 token（2026-11-04）】
用当天三批真实存档测：150 条 → URL 去重后 **148 条，只去掉 2 条**。
原因：三批问的是**不同侧面**，返回的是**不同页面**——Brave / Exa 在三批里都出现，
但每次是不同 URL。重复发生在"主张层面"（同一句话被 8 个站点镜像），不在 URL 层面。

→ **所以别指望靠它省上下文。** 它真正的价值只有两条：
  1. 一次往返代替 N 次（省延迟）
  2. 统一编号视野（能发现跨批的重复与矛盾）

若目标真的是省 token，正确做法是**先想清楚该问几个关键词**，而不是合并后去重。

【适用判据】
  关键词指向**同一主题的不同侧面** → 合并成一个池子，派 1 个子智能体
  关键词指向**互不相关的独立问题** → 仍应 N 个子智能体并发，不要合并
  （合并的前提是"它们本该在同一个视野里被通读"）

──────────────────────────────────────────────────────────────
【用法】
──────────────────────────────────────────────────────────────
  # 只看合并前后条数，不输出内容
  python scripts/merge.py a.json b.json c.json --stats

  # 输出合并后的列表（编号已重排，可直接对照）
  python scripts/merge.py a.json b.json c.json

  # 写成合规存档 —— 之后可直接被 dump.py / search.py --replay 读取
  python scripts/merge.py 2026-11-04/*.json \
      --out .workbuddy/search-results/2026-11-04/merged-xxx.json --stats

  # 每条保留来源（哪一批、原编号），便于回溯
  python scripts/merge.py a.json b.json --keep-source

【设计约束】
- 去重规则**复用 dump.py 的 dedup_url**，不另写一份——两处各写一份必然漂移，
  而编号一旦漂移，"看第 12 条"就指到了别处（这个坑已经踩过两次）。
- 输出文件是**合规存档**（schema 与 search.py 写的一致），所以下游工具零改动。
- 合并后的 `filters.min_authority` 固定为 0：合并的是已落盘的结果，
  各批当时的过滤口径可能不同，统一按"全给"处理；要看原批次的过滤请回到原文件。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from dump import dedup_url, norm_text, site_key  # noqa: E402  同目录复用，避免规则漂移

MERGE_SCHEMA = "deepseek-search-archive/1"


def load_rows(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not path.is_file():
        raise SystemExit(f"存档不存在: {path}")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"读取失败 {path}: {exc}")
    rows = [r for r in (record.get("results") or []) if isinstance(r, dict)]
    return record, rows


def merge(paths: list[Path], keep_source: bool) -> tuple[list[dict[str, Any]], list[str], list[dict[str, int]]]:
    """按 URL 去重合并。返回 (合并后的行, 各文件原始条数, 重复明细)。"""
    seen: dict[str, tuple[int, str]] = {}
    merged: list[dict[str, Any]] = []
    counts: list[dict[str, int]] = []
    dupes: list[str] = []

    for path in paths:
        record, rows = load_rows(path)
        kept = 0
        for i, row in enumerate(rows, 1):
            key = dedup_url(row.get("url")) or str(row.get("title") or "")
            if key and key in seen:
                first_row, first_file = seen[key]
                dupes.append(f"{path.name}#{i} = {first_file}#{first_row}")
                continue
            if key:
                seen[key] = (i, path.name)
            item = dict(row)
            if keep_source:
                item["_source_file"] = path.name
                item["_source_index"] = i
                item["_source_query"] = norm_text(record.get("query_raw"))[:80]
            merged.append(item)
            kept += 1
        counts.append({"file": path.name, "raw": len(rows), "kept": kept})

    for idx, item in enumerate(merged, 1):
        item["index"] = idx
    return merged, dupes, counts


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="合并多个搜索存档并跨批去重（实验功能，未并入主流程）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("archives", nargs="+", help="一个或多个存档 JSON 路径")
    p.add_argument("--out", help="写成合并后的存档 JSON（可被 dump.py / --replay 直接读取）")
    p.add_argument("--stats", action="store_true", help="只输出合并前后统计，不输出条目")
    p.add_argument("--keep-source", dest="keep_source", action="store_true",
                   help="每条保留 _source_file / _source_index / _source_query")
    p.add_argument("--limit", type=int, default=0, help="列表模式下标只取前 N 条，0=全部")
    return p


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)

    paths = [Path(a) for a in args.archives]
    merged, dupes, counts = merge(paths, args.keep_source)
    total_raw = sum(c["raw"] for c in counts)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "schema": MERGE_SCHEMA,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "query_raw": "merged: " + " + ".join(p.name for p in paths),
            "query_final": "",
            "endpoint": "merge.py",
            "request": {"sources": [str(p) for p in paths]},
            "elapsed_ms": 0,
            "api_returned": total_raw,
            "kept_after_local_filter": len(merged),
            "filters": {"min_authority": 0},
            "results": merged,
            "merge_meta": {
                "sources": counts,
                "duplicates_removed": len(dupes),
                "dedup_rule": "dump.dedup_url",
            },
        }
        out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写入 {out_path}", file=sys.stderr)

    if args.stats:
        print(f"来源 {len(paths)} 个存档")
        for c in counts:
            print(f"  {c['file'][:58]:58} {c['raw']:>3} 条 → 保留 {c['kept']:>3}")
        print(f"合计 {total_raw} 条 → 去重后 {len(merged)} 条（去掉 {len(dupes)} 条）")
        if dupes:
            print("重复明细（前 12 组）：")
            for d in dupes[:12]:
                print(f"  {d}")
            if len(dupes) > 12:
                print(f"  …另有 {len(dupes) - 12} 组")
        return 0

    items = merged[: args.limit] if args.limit > 0 else merged
    for item in items:
        head = (
            f"{item['index']:>3}. [{site_key(item)}] "
            f"{norm_text(item.get('date'))[:10]} | {norm_text(item.get('title'))[:70]}"
        )
        print(head)
    print(f"\n（合并 {total_raw} 条 → {len(merged)} 条，去掉 {len(dupes)} 条重复）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
