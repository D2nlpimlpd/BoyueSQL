#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Run only model-level SOTA baselines without re-running CoordSQL ablations."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Dict, List

import xiaorong as x


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--baseline-models", nargs="+", required=True)
    p.add_argument(
        "--baseline-systems",
        nargs="+",
        default=["direct_prompt", "self_debug_style"],
        choices=x.RUNNABLE_BASELINES,
    )
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--start-date", default="2021-01-01")
    p.add_argument("--end-date", default="2026-03-17")
    p.add_argument("--sql-timeout", type=int, default=45)
    p.add_argument("--use-gold-tables", action="store_true")
    p.add_argument("--baselines-dir", default="baselines")
    p.add_argument("--output", default="xiaorong_results_sota_models_only.json")
    p.add_argument("--markdown-output", default="xiaorong_results_sota_models_only.md")
    p.add_argument("--db-user", default=x.m.DEFAULT_DB_CONFIG.get("DB_USER", ""))
    p.add_argument("--db-password", default=x.m.DEFAULT_DB_CONFIG.get("DB_PASSWORD", ""))
    p.add_argument("--db-host", default=x.m.DEFAULT_DB_CONFIG.get("DB_HOST", "127.0.0.1"))
    p.add_argument("--db-port", type=int, default=x.m.DEFAULT_DB_CONFIG.get("DB_PORT", 1521))
    p.add_argument("--db-service-name", default=x.m.DEFAULT_DB_CONFIG.get("DB_SERVICE_NAME", "oral"))
    args = p.parse_args()

    db_cfg = {
        "DB_USER": args.db_user,
        "DB_PASSWORD": args.db_password,
        "DB_HOST": args.db_host,
        "DB_PORT": args.db_port,
        "DB_SERVICE_NAME": args.db_service_name,
    }

    sota_summary: Dict[str, Dict[str, Any]] = {}
    sota_details: List[Dict[str, Any]] = []

    print("=" * 70)
    print("SOTA model-only text-to-SQL baseline comparison")
    print("=" * 70)

    for model in args.baseline_models:
        sota_summary[model] = {}
        for system in args.baseline_systems:
            print(f"\n🧪 baseline: {system} / model: {model}")
            runs: List[Dict[str, Any]] = []
            for case in x.BENCHMARK_CASES:
                print(f"     · {case['id']} ...", end=" ", flush=True)
                result = x.run_sota_baseline_case(
                    model=model,
                    system=system,
                    case=case,
                    db_cfg=db_cfg,
                    rounds=args.rounds,
                    start_date=args.start_date,
                    end_date=args.end_date,
                    sql_timeout=args.sql_timeout,
                    use_gold_tables=args.use_gold_tables,
                )
                runs.append(result)
                print("✅" if result.get("ok") else "❌", flush=True)
            summary = x.summarize(runs)
            sota_summary[model][system] = summary
            sota_details.append({
                "model": model,
                "system": system,
                "summary": summary,
                "cases": runs,
            })

    out = {
        "meta": {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "start_date": args.start_date,
            "end_date": args.end_date,
            "rounds": args.rounds,
            "cases": [case["id"] for case in x.BENCHMARK_CASES],
            "use_gold_tables": args.use_gold_tables,
            "baseline_systems": args.baseline_systems,
            "baseline_models": args.baseline_models,
            "mode": "sota_models_only",
        },
        "summary": {},
        "sota_baseline_summary": sota_summary,
        "sota_system_sources": x._baseline_inventory(Path(args.baselines_dir)),
        "sota_details": sota_details,
    }

    output_path = Path(args.output).resolve()
    output_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path = Path(args.markdown_output).resolve()
    x.write_markdown_report(out, markdown_path)

    print("\n" + "=" * 70)
    print("实验完成")
    print(f"结果文件: {output_path}")
    print(f"Markdown汇总: {markdown_path}")
    print("=" * 70)


if __name__ == "__main__":
    main()
