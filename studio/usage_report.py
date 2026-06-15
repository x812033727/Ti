"""跨 session 的 token 用量彙總（唯讀）。

讀 history/ 下所有 meta.json 的 `token_usage` 區塊（finish_session 聚合而成）；
缺漏或中斷未收尾的場次則回讀對應 jsonl、以 history._derive_token_usage 即時重算，
確保涵蓋舊場與崩潰場。輸出總量與 by-provider／model／role 分組，並估算 USD：
  - Claude：直接採 meta 內 SDK 提供的 cost_usd（估算，訂閱實扣以官方為準）。
  - MiniMax／OpenAI：依下方價目表（model → USD/Mtok）估算；未配置單價者只報 token。

用法：
    python -m studio.usage_report [--since YYYY-MM-DD] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from . import config, history

# 模型單價（USD / 每百萬 token），(input, output)。
# 來源：platform.minimax.io 公告價（2026-06 取得，異動請就地更新）。
# 未列入的模型不估 USD、只報 token 量。
PRICES: dict[str, tuple[float, float]] = {
    # MiniMax（如與官方公告不符，請更新此處）
    "MiniMax-M3": (0.30, 1.20),
    "MiniMax-M2.7": (0.30, 1.20),
    "MiniMax-M2.5": (0.30, 1.20),
    "MiniMax-M2.1": (0.30, 1.20),
    "MiniMax-M2": (0.30, 1.20),
}


def _blank() -> dict:
    return {"prompt": 0, "completion": 0, "total": 0, "cost_usd": 0.0, "calls": 0}


def _add(dst: dict, src: dict) -> None:
    for k in ("prompt", "completion", "total", "calls"):
        dst[k] += src.get(k, 0) or 0
    dst["cost_usd"] += src.get("cost_usd", 0.0) or 0.0


def _usage_for(meta: dict) -> dict | None:
    """取一場的 token_usage；meta 缺漏時回讀 events 即時重算。"""
    tu = meta.get("token_usage")
    if tu and tu.get("total", {}).get("calls"):
        return tu
    sid = meta.get("session_id")
    if not sid:
        return None
    events = history.load_events(sid)
    derived = history._derive_token_usage(events)
    return derived if derived["total"]["calls"] else None


def _estimate_minimax_usd(by_model: dict) -> float:
    """對有列價的模型估算 USD（Claude 已含 cost_usd，這裡只補沒成本的 model）。"""
    usd = 0.0
    for model, b in by_model.items():
        if b.get("cost_usd"):  # 已有實際/SDK 成本，不重複估
            continue
        price = PRICES.get(model)
        if not price:
            continue
        usd += b["prompt"] / 1_000_000 * price[0]
        usd += b["completion"] / 1_000_000 * price[1]
    return usd


def aggregate(since: float | None = None) -> dict:
    total = _blank()
    by_provider: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    by_role: dict[str, dict] = {}
    n_sessions = 0
    for meta in history.list_sessions():
        if since is not None and (meta.get("started_at") or 0) < since:
            continue
        tu = _usage_for(meta)
        if tu is None:
            continue
        n_sessions += 1
        _add(total, tu["total"])
        for grp, dst in (
            ("by_provider", by_provider),
            ("by_model", by_model),
            ("by_role", by_role),
        ):
            for key, b in (tu.get(grp) or {}).items():
                _add(dst.setdefault(key, _blank()), b)
    return {
        "sessions": n_sessions,
        "total": total,
        "by_provider": by_provider,
        "by_model": by_model,
        "by_role": by_role,
        "est_extra_usd": _estimate_minimax_usd(by_model),
    }


def _fmt_row(name: str, b: dict) -> str:
    cost = b.get("cost_usd") or 0.0
    cost_s = f"${cost:,.4f}" if cost else "—"
    return (
        f"  {name:<22} calls={b['calls']:<5} "
        f"in={b['prompt']:>10,} out={b['completion']:>10,} "
        f"total={b['total']:>11,} cost={cost_s}"
    )


def render(agg: dict) -> str:
    out: list[str] = []
    t = agg["total"]
    out.append("=== Ti Token 用量彙總 ===")
    out.append(f"涵蓋 session 數：{agg['sessions']}")
    out.append(
        f"總計：calls={t['calls']} in={t['prompt']:,} out={t['completion']:,} "
        f"total={t['total']:,}"
    )
    claude_cost = t.get("cost_usd") or 0.0
    out.append(
        f"成本估算：Claude(SDK) ${claude_cost:,.4f} + "
        f"MiniMax/OpenAI(價目表) ${agg['est_extra_usd']:,.4f} "
        f"≈ ${claude_cost + agg['est_extra_usd']:,.4f}　（皆為估算，實扣以各家後台為準）"
    )
    for title, key in (
        ("依 Provider", "by_provider"),
        ("依 Model", "by_model"),
        ("依角色", "by_role"),
    ):
        out.append(f"\n[{title}]")
        rows = sorted(agg[key].items(), key=lambda kv: kv[1]["total"], reverse=True)
        for name, b in rows:
            out.append(_fmt_row(name, b))
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Ti 跨 session token 用量統計")
    ap.add_argument("--since", help="只計此日期(YYYY-MM-DD)起的 session", default=None)
    ap.add_argument("--json", action="store_true", help="輸出原始 JSON")
    args = ap.parse_args(argv)

    since = None
    if args.since:
        since = time.mktime(time.strptime(args.since, "%Y-%m-%d"))

    if not config.HISTORY_ROOT.exists():
        print(f"找不到 history 目錄：{config.HISTORY_ROOT}", file=sys.stderr)
        return 1
    agg = aggregate(since)
    print(json.dumps(agg, ensure_ascii=False, indent=2) if args.json else render(agg))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
