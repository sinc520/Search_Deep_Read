#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DeepSeek 搜索 CLI —— 直连 https://api.deepseek.com/v1/tools/search

设计目标：把官方 API 的全部参数暴露出来，同时在本地做排序 / 截断，避免把
50 条全文灌进上下文。默认不按 authority 过滤——引擎返回的顺序本身就是
rerank 降序（相关度+权威+时效的合成分），二次过滤会把排序打乱并漏掉好料。

用法示例：
  python search.py "某研究主题 关键词A 关键词B" --count 20
  python search.py "某研究主题 最新进展" --count 10 --lang zh --format md
  python search.py "RAG top-k" --site arxiv.org --site acm.org --after 2026-01-01
  python search.py "query" --need-content --format json > out.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

API_URL = "https://api.deepseek.com/v1/tools/search"
MODELS_JSON = Path.home() / ".workbuddy" / "models.json"

# 全局兜底目录：跨项目检索或项目目录不可用时使用
GLOBAL_ARCHIVE = Path.home() / ".workbuddy" / "search-results"

# 官方文档给出的 count 范围
COUNT_MIN, COUNT_MAX = 1, 50

# 三档预设：field / 摘要截断字数（0=不截断）/ 展示条数
# 依据：元宝每条实测中位 1043 字，DeepSeek snippet 中位 330 字、summary 中位 2511 字
PRESETS: dict[str, dict[str, Any]] = {
    "brief":   {"field": "snippet", "max_summary": 300,  "show": 10},  # 极简：约 3.3k 字符
    "default": {"field": "summary", "max_summary": 1000, "show": 10},  # 默认：约 10k 字符，对齐元宝体验
    "deep":    {"field": "summary", "max_summary": 0,    "show": 5},   # 深读：完整 summary，前 5 条
}


def resolve_archive_root(explicit: str | None, scope: str) -> Path:
    """决定存档根目录。

    优先级：--archive-dir > 环境变量 DEEPSEEK_SEARCH_ARCHIVE >
    scope=global 用全局目录 > scope=project 用 <项目>/.workbuddy/search-results。
    项目目录取自 CLI 注入的 CODEBUDDY_PROJECT_DIR / CLAUDE_PROJECT_DIR，回落当前目录。
    """
    if explicit:
        return Path(explicit).expanduser()

    env_root = os.environ.get("DEEPSEEK_SEARCH_ARCHIVE", "").strip()
    if env_root:
        return Path(env_root).expanduser()

    if scope == "global":
        return GLOBAL_ARCHIVE

    for var in ("CODEBUDDY_PROJECT_DIR", "CLAUDE_PROJECT_DIR"):
        value = os.environ.get(var, "").strip()
        if value and Path(value).is_dir():
            return Path(value) / ".workbuddy" / "search-results"

    cwd = Path.cwd()
    if cwd.is_dir():
        return cwd / ".workbuddy" / "search-results"
    return GLOBAL_ARCHIVE


# --------------------------------------------------------------------------- #
# 密钥加载：显式参数 > 环境变量 > 现有 models.json（不重复落盘明文）
# --------------------------------------------------------------------------- #
def load_api_key(explicit: str | None = None) -> str:
    if explicit:
        return explicit.strip()

    env_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if env_key:
        return env_key

    if MODELS_JSON.is_file():
        try:
            entries = json.loads(MODELS_JSON.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise SystemExit(f"读取 {MODELS_JSON} 失败: {exc}")
        for entry in entries if isinstance(entries, list) else []:
            url = str(entry.get("url", ""))
            key = str(entry.get("apiKey", "")).strip()
            if "api.deepseek.com" in url and key.startswith("sk-"):
                return key

    raise SystemExit(
        "未找到 DeepSeek API key。请任选其一：\n"
        "  --api-key sk-xxx\n"
        "  export DEEPSEEK_API_KEY=sk-xxx\n"
        f"  在 {MODELS_JSON} 中配置一条 url 含 api.deepseek.com 的模型"
    )


# --------------------------------------------------------------------------- #
# 查询构造：站点/时间/语言过滤走官方支持的搜索算子，而不是臆造字段
# --------------------------------------------------------------------------- #
def build_query(args: argparse.Namespace) -> str:
    parts: list[str] = [args.query.strip()]

    if args.site:
        sites = " OR ".join(f"site:{s}" for s in args.site)
        parts.append(f"({sites})" if len(args.site) > 1 else sites)

    for s in args.exclude_site or []:
        parts.append(f"-site:{s}")

    if args.lang:
        parts.append(f"lang:{args.lang}")
    if args.after:
        parts.append(f"after:{args.after}")
    if args.before:
        parts.append(f"before:{args.before}")

    return " ".join(parts)


# --------------------------------------------------------------------------- #
# 本地存档：原始 JSON 全量落盘 + 全局 index.jsonl
# --------------------------------------------------------------------------- #
def _slug(text: str, limit: int = 40) -> str:
    """生成 ASCII 安全的文件名片段；全中文查询退化为哈希前缀。"""
    ascii_part = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit]
    if len(ascii_part) >= 4:
        return ascii_part
    return "q-" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def archive_path(root: Path, query: str, payload: dict[str, Any]) -> Path:
    now = datetime.now()
    digest = hashlib.sha1(
        f"{query}|{json.dumps(payload, sort_keys=True)}".encode("utf-8")
    ).hexdigest()[:4]
    return root / now.strftime("%Y-%m-%d") / f"{_slug(query)}-{now.strftime('%H%M%S')}-{digest}.json"


def save_archive(
    path: Path,
    query_raw: str,
    query_final: str,
    payload: dict[str, Any],
    response: dict[str, Any],
    rows: list[dict[str, Any]],
    elapsed_ms: int,
    api_total: int,
    min_authority: int = 0,
    kept_count: int = -1,
) -> Path:
    """写入原始响应 + 元数据。summary/snippet 保留完整版，不做截断。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "schema": "deepseek-search-archive/1",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "query_raw": query_raw,
        "query_final": query_final,
        "endpoint": API_URL,
        "request": payload,
        "elapsed_ms": elapsed_ms,
        "api_returned": api_total,
        # 本地过滤后的条数。必须与 search.py 展示时的编号口径一致，
        # 否则 dump.py 数出来的 #12 和主轨看到的 #12 不是同一条。
        "kept_after_local_filter": kept_count if kept_count >= 0 else len(rows),
        # 记录当时用的过滤条件，dump.py 据此复现同一套编号
        "filters": {"min_authority": min_authority},
        "results": rows,
        "response_raw_keys": sorted(response.keys()),
    }
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    index = path.parent.parent / "index.jsonl"
    entry = {
        "at": record["created_at"],
        "query": query_raw,
        "count_requested": payload.get("count"),
        "api_returned": api_total,
        "kept": len(rows),
        "min_authority": min_authority,
        "file": str(path).replace("\\", "/"),
    }
    with index.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return path


def load_archive(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SystemExit(f"存档不存在: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"读取存档失败: {exc}")


def prune_archive(root: Path, days: int) -> tuple[int, int]:
    """删除超过 N 天的日期目录，返回 (删除文件数, 删除目录数)。"""
    if not root.is_dir():
        return 0, 0
    cutoff = datetime.now() - timedelta(days=days)
    files = dirs = 0
    for sub in root.iterdir():
        if not sub.is_dir():
            continue
        try:
            stamp = datetime.strptime(sub.name, "%Y-%m-%d")
        except ValueError:
            continue
        if stamp >= cutoff:
            continue
        for child in sub.iterdir():
            if child.is_file():
                child.unlink()
                files += 1
        sub.rmdir()
        dirs += 1
    return files, dirs


# --------------------------------------------------------------------------- #
# API 调用
# --------------------------------------------------------------------------- #
def call_api(
    payload: dict[str, Any], api_key: str, timeout: int, retries: int = 2
) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    last_err: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(API_URL, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            # 4xx 不重试：401 密钥错、402 余额不足、429 限流
            if 400 <= exc.code < 500:
                raise SystemExit(f"HTTP {exc.code}: {detail}")
            last_err = exc
        except (urllib.error.URLError, TimeoutError) as exc:
            last_err = exc
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))

    raise SystemExit(f"请求失败（已重试 {retries} 次）: {last_err}")


# --------------------------------------------------------------------------- #
# 结果整形
# --------------------------------------------------------------------------- #
def trim(text: str | None, limit: int) -> str:
    if not text:
        return ""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def truncate_marked(text: str | None, limit: int) -> str:
    """截断并显式标注，避免把残缺内容当成全貌。limit<=0 表示不截断。"""
    if not text:
        return ""
    original = " ".join(str(text).split())
    if limit <= 0 or len(original) <= limit:
        return original
    head = original[:limit].rstrip()
    return f"{head} …[截断 {len(original)}→{limit}]"


def short_date(value: Any) -> str:
    """'2026-10-22T08:00:00+08:00' -> '2026-10-22'，无日期则留空。"""
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 and text[4:5] == "-" else ""


def clean_url(value: Any) -> str:
    """去掉锚点、utm_* 跟踪参数；超长查询串整体丢弃（存档仍保留原始 URL）。"""
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_")]
    encoded = urlencode(query)
    if len(encoded) > 120:  # 签名/重定向类超长串，展示价值低于占用成本
        encoded = ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path, encoded, ""))


def display_site(record: dict[str, Any]) -> str:
    """site_name 缺省时退化为域名，避免输出空列。"""
    name = str(record.get("site_name") or "").strip()
    if name:
        return name
    try:
        from urllib.parse import urlsplit

        return urlsplit(clean_url(record.get("url"))).netloc
    except ValueError:
        return ""


def postprocess(
    results: list[dict[str, Any]], args: argparse.Namespace
) -> list[dict[str, Any]]:
    rows = [r for r in results if isinstance(r, dict)]

    threshold = args.min_authority or 0
    if threshold > 0:
        rows = [r for r in rows if _num(r.get("authority")) >= threshold]

    if args.sort == "authority":
        rows.sort(key=lambda r: (_num(r.get("authority")), _num(r.get("rerank_score"))), reverse=True)
    elif args.sort == "rerank":
        rows.sort(key=lambda r: _num(r.get("rerank_score")), reverse=True)

    if args.dedup:
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for r in rows:
            key = clean_url(r.get("url")) or str(r.get("title", ""))
            if key in seen:
                continue
            seen.add(key)
            unique.append(r)
        rows = unique

    return rows


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _score(value: Any, digits: int) -> str:
    """展示用：字段缺失写 —。排序仍走 _num，不受影响。

    元宝的存档没有 authority / rerank_score，显示成 -1/0.00 会被误读为低分。
    """
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def format_compact(rows: list[dict[str, Any]], args: argparse.Namespace) -> str:
    if not rows:
        return "（无结果）"
    out: list[str] = []
    for i, r in enumerate(rows, 1):
        head = (
            f"{i:>2}. [{_score(r.get('authority'), 0)}|{_score(r.get('rerank_score'), 2)}] "
            f"{trim(r.get('title'), 80)} | {trim(display_site(r), 24)} "
            f"| {short_date(r.get('date'))} | {clean_url(r.get('url'))}"
        )
        out.append(head)
        body = r.get("summary") if args.field == "summary" else r.get("snippet")
        summary = truncate_marked(body or r.get("snippet"), args.max_summary)
        if summary:
            out.append(f"    {summary}")
    return "\n".join(out)


def format_expanded(rows: list[dict[str, Any]], indices: list[int], limit: int) -> str:
    """按 1-based 序号输出完整 summary（或 snippet）。limit=0 表示不截断。"""
    out: list[str] = []
    for idx in indices:
        if not 1 <= idx <= len(rows):
            out.append(f"[{idx}] 超出范围（共 {len(rows)} 条）")
            continue
        r = rows[idx - 1]
        body = str(r.get("summary") or r.get("snippet") or "")
        text = truncate_marked(body, limit) if limit > 0 else " ".join(body.split())
        out.append(
            f"\n=== [{idx}] {trim(r.get('title'), 100)}\n"
            f"    {clean_url(r.get('url'))} · authority={_score(r.get('authority'), 0)} "
            f"· 全文 {len(body)} 字符\n{text}"
        )
    return "\n".join(out)


def format_md(rows: list[dict[str, Any]], args: argparse.Namespace) -> str:
    if not rows:
        return "（无结果）"
    out: list[str] = []
    for i, r in enumerate(rows, 1):
        out.append(f"## {i}. [{trim(r.get('title'), 120)}]({clean_url(r.get('url'))})")
        out.append(
            f"authority={_score(r.get('authority'), 0)} · "
            f"rerank={_score(r.get('rerank_score'), 2)} · "
            f"{trim(display_site(r), 32)} · {short_date(r.get('date'))}"
        )
        body = r.get("content") if args.need_content else (r.get("summary") or r.get("snippet"))
        if body:
            out.append(trim(body, args.max_summary if not args.need_content else 1200))
        out.append("")
    return "\n".join(out)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="DeepSeek 搜索（api.deepseek.com/v1/tools/search）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("query", nargs="?", help="检索词，1-128 字符；可直接写 site:/after:/lang: 算子。"
                                             "--replay / --prune 时可省略")
    p.add_argument("--count", type=int, default=50,
                   help=f"向 API 请求的条数 {COUNT_MIN}-{COUNT_MAX}，默认 50（按次计费，多取不额外花钱）")
    p.add_argument("--preset", choices=list(PRESETS), default="default",
                   help="展示档位：brief=极简(snippet/300字/10条) · default=默认(summary/1000字/10条) · deep=深读(完整summary/前5条)")
    p.add_argument("--show", type=int, default=None,
                   help="本地展示条数（覆盖 preset，0=全部）；只影响进上下文的量")
    p.add_argument("--ori-question", dest="ori_question", help="用户原始问题，提升意图理解")
    p.add_argument("--localization", help="地域化标识（IP 或地区码，按官方文档填）")
    p.add_argument("--need-content", action="store_true", help="返回网页全文（很吃上下文，慎用）")
    p.add_argument("--site", action="append", help="只搜指定站点，可重复")
    p.add_argument("--exclude-site", dest="exclude_site", action="append", help="排除指定站点，可重复")
    p.add_argument("--lang", help="语言过滤，如 zh / en")
    p.add_argument("--after", help="起始日期 YYYY-MM-DD")
    p.add_argument("--before", help="截止日期 YYYY-MM-DD")
    p.add_argument("--min-authority", dest="min_authority", type=int, default=None,
                   help="只保留 authority≥N 的结果（0-4）。默认不筛——引擎已按 rerank 降序"
                        "返回，authority 只反映域名眼熟程度，不代表内容质量")
    p.add_argument("--sort", choices=["rerank", "authority", "none"], default="none",
                   help="本地重排：rerank=按相关度，authority=按权威度")
    p.add_argument("--no-dedup", dest="dedup", action="store_false",
                   help="关闭按 URL 去重（默认开启：同一页面常被切成多条返回）")
    p.add_argument("--format", choices=["compact", "md", "json"], default="compact")
    p.add_argument("--field", choices=["summary", "snippet"], default=None,
                   help="列表展示字段（覆盖 preset）：summary 信息多，snippet 短")
    p.add_argument("--max-summary", dest="max_summary", type=int, default=None,
                   help="列表每条截断字数（覆盖 preset），0=不截断；只影响展示，存档永远是全文")
    p.add_argument("--expand", metavar="IDX[,IDX]", action="append",
                   help="输出指定序号的完整 summary，如 2,5。可重复给多次，"
                        "也可一次给逗号分隔（--expand 1 --expand 8 等价于 --expand 1,8）")
    p.add_argument("--expand-only", dest="expand_only", action="store_true",
                   help="只输出 --expand 的内容，不重打列表（省上下文）")
    p.add_argument("--expand-max", dest="expand_max", type=int, default=0,
                   help="expand 输出时的截断字数，0=不限制（默认）")
    p.add_argument("--timeout", type=int, default=40, help="单次请求超时秒数")
    p.add_argument("--api-key", dest="api_key", help="覆盖密钥（默认读环境变量或 models.json）")

    g = p.add_argument_group("存档")
    g.add_argument("--archive-scope", dest="archive_scope", choices=["project", "global"],
                   default="project",
                   help="project=存到 <项目>/.workbuddy/search-results（默认），global=存到 ~/.workbuddy/search-results")
    g.add_argument("--archive-dir", dest="archive_dir",
                   help="直接指定存档根目录（优先级最高）")
    g.add_argument("--no-save", dest="save", action="store_false",
                   help="不落盘（默认每次搜索都会存原始 JSON）")
    g.add_argument("--out", help="指定存档文件路径（隐含 --save）")
    g.add_argument("--replay", metavar="FILE",
                   help="离线重放已有存档：不调用 API，按当前过滤/排序参数重新输出")
    g.add_argument("--prune", type=int, metavar="DAYS",
                   help="删除早于 DAYS 天的存档目录后退出")
    return p


def emit(rows: list[dict[str, Any]], args: argparse.Namespace) -> None:
    if args.format == "json":
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    elif args.format == "md":
        print(format_md(rows, args))
    else:
        print(format_compact(rows, args))


def parse_indices(raw: "str | list[str] | None") -> list[int]:
    if not raw:
        return []
    if isinstance(raw, list):
        # --expand 可重复出现，先拼起来再按同一套规则解析
        raw = ",".join(raw)
    out: list[int] = []
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            if a.isdigit() and b.isdigit():
                out.extend(range(int(a), int(b) + 1))
        elif part.isdigit():
            out.append(int(part))
    return out


def unshown_note(rows: list[dict[str, Any]], shown: list[dict[str, Any]]) -> str:
    """没展示的那部分要明说。

    静默丢弃最危险的地方，是把它当成"这就是全部"——后面的判断全建立在一个残缺样本上。
    """
    hidden = len(rows) - len(shown)
    if hidden <= 0:
        return ""
    return (
        f"⚠ 未展示 #{len(shown) + 1}–#{len(rows)} 共 {hidden} 条"
        f"（需要时用 --expand 定向取，或 dump.py --stats 看分布）"
    )


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    args = build_parser().parse_args(argv)

    # 先套 preset，再用显式参数覆盖
    if not args.query and args.replay is None and args.prune is None:
        raise SystemExit("缺少检索词。若是离线重放请用 --replay <存档>，清理存档请用 --prune N")

    preset = PRESETS[args.preset]
    if args.field is None:
        args.field = preset["field"]
    if args.max_summary is None:
        args.max_summary = preset["max_summary"]
    if args.show is None:
        args.show = preset["show"]

    root = resolve_archive_root(args.archive_dir, args.archive_scope)

    if args.prune is not None:
        files, dirs = prune_archive(root, args.prune)
        print(f"pruned {files} files / {dirs} dirs older than {args.prune}d under {root}")
        return 0

    args.count = max(COUNT_MIN, min(COUNT_MAX, args.count))
    if args.out:
        args.save = True

    # 离线重放：不调用 API，只按当前过滤/排序参数重新输出
    if args.replay:
        record = load_archive(Path(args.replay))
        # 沿用存档写入时的过滤阈值。不沿用会让 replay 多出被过滤掉的条目，
        # 编号与 dump.py / 当初那次搜索全部错位——"看第 12 条"就指到了别处。
        if args.min_authority is None:
            args.min_authority = int((record.get("filters") or {}).get("min_authority") or 0)
        if not args.query:
            args.query = record.get("query_raw") or "replay"
        rows = postprocess(record.get("results") or [], args)
        shown = rows[: args.show] if args.show > 0 else rows
        if not args.expand_only:
            emit(shown, args)
        for idx in parse_indices(args.expand):
            print(format_expanded(rows, [idx], args.expand_max))
        # 页脚必须反映真实输出，别在 --expand-only 时还写"展示 10 条"
        expanded = parse_indices(args.expand)
        parts = [f"存档 {len(rows)} 条"]
        if not args.expand_only:
            parts.append(f"展示 {len(shown)} 条")
        if expanded:
            parts.append(f"展开 {len(expanded)} 条")
        note = f"replay: {args.replay} · " + " · ".join(parts) + " (0 API calls)"
        print(note, file=sys.stderr if args.format == "json" else sys.stdout)
        tail = "" if args.expand_only else unshown_note(rows, shown)
        if tail:
            print(tail, file=sys.stderr if args.format == "json" else sys.stdout)
        return 0 if rows else 2

    payload: dict[str, Any] = {"query": build_query(args), "count": args.count}
    if args.ori_question:
        payload["ori_question"] = args.ori_question
    if args.localization:
        payload["localization"] = args.localization
    if args.need_content:
        payload["need_content"] = True

    started = time.time()
    data = call_api(payload, load_api_key(args.api_key), args.timeout)
    elapsed_ms = int((time.time() - started) * 1000)

    raw_rows = data.get("results") or []
    rows = postprocess(raw_rows, args)
    shown = rows[: args.show] if args.show > 0 else rows
    if not args.expand_only:
        emit(shown, args)
    for idx in parse_indices(args.expand):
        print(format_expanded(rows, [idx], args.expand_max))

    if args.save:
        target = Path(args.out) if args.out else archive_path(root, args.query, payload)
        saved = save_archive(
            target, args.query, payload["query"], payload, data,
            raw_rows, elapsed_ms, len(raw_rows), args.min_authority or 0, len(rows),
        )
        print(
            f"saved: {saved} · 存档 {len(raw_rows)} 条"
            + ("" if args.expand_only else f" · 展示 {len(shown)} 条"),
            file=sys.stderr if args.format == "json" else sys.stdout,
        )

    tail = "" if args.expand_only else unshown_note(rows, shown)
    if tail:
        print(tail, file=sys.stderr if args.format == "json" else sys.stdout)

    return 0 if rows else 2


if __name__ == "__main__":
    raise SystemExit(main())
