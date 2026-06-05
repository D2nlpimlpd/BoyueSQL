#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
xiaorong.py

消融实验：比较 NL2SQL 流程在“有 final repair”与“无 final repair”两种设置下的效果。
默认评测 main.py 的多表查询链路（retrieve -> plan -> generate_with_repair -> execute）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _configure_console_encoding() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_configure_console_encoding()

import main as m
from flask import session


DEFAULT_MODELS = [
    "qwen3-vl:8b",
]


SOTA_SYSTEMS = [
    {
        "id": "dail_sql",
        "name": "DAIL-SQL",
        "repo_dir": "DAIL-SQL",
        "url": "https://github.com/BeachWang/DAIL-SQL",
        "role": "prompting and demonstration retrieval baseline",
    },
    {
        "id": "din_sql",
        "name": "DIN-SQL",
        "repo_dir": "DIN-SQL",
        "url": "https://github.com/madhup-google/DIN-SQL",
        "role": "decomposed prompting and self-correction baseline",
    },
    {
        "id": "mac_sql",
        "name": "MAC-SQL",
        "repo_dir": "MAC-SQL",
        "url": "https://github.com/wbbeyourself/MAC-SQL",
        "role": "multi-agent collaborative text-to-SQL baseline",
    },
    {
        "id": "chess",
        "name": "CHESS",
        "repo_dir": "CHESS",
        "url": "https://github.com/ShayanTalaei/CHESS",
        "role": "schema-aware LLM text-to-SQL baseline",
    },
    {
        "id": "db_gpt_hub",
        "name": "DB-GPT-Hub",
        "repo_dir": "DB-GPT-Hub",
        "url": "https://github.com/eosphoros-ai/DB-GPT-Hub",
        "role": "open-source text-to-SQL toolkit baseline",
    },
    {
        "id": "sqlcoder",
        "name": "SQLCoder",
        "repo_dir": "SQLCoder",
        "url": "https://github.com/defog-ai/sqlcoder",
        "role": "specialized SQL generation model baseline",
    },
]


RUNNABLE_BASELINES = [
    "direct_prompt",
    "dail_sql_style",
    "din_sql_style",
    "self_debug_style",
]


BENCHMARK_CASES: List[Dict[str, Any]] = [
    {
        "id": "M1",
        "question": "查询所有受检人员的费用明细，包括是否出入境、是否发件、体检编号、姓名、体检状态名称、人员类型描述、单位名称、收费项目、费用名称、收费状态、单价、数量、合计金额、批准项目名称",
        "selected_tables": ["EXAM_RECORD", "CONTROL_STATUS", "BM_CONTROL_STATUS", "FEE_RECORD", "VIEW_FEE", "BM_PERSON_TYPE", "BM_PZXM"],
    },
    {
        "id": "M2",
        "question": "统计每个人员类型的已收费金额合计和未收费金额合计，关联 exam_record、fee_record、bm_person_type",
        "selected_tables": ["EXAM_RECORD", "FEE_RECORD", "BM_PERSON_TYPE"],
    },
    {
        "id": "M3",
        "question": "统计2021-01-01至2026-03-17期间，每个检验项目中国籍和外籍受检人数及异常人数，需要检验项目编码和描述，关联 exam_record、lab_result、bm_lab_item",
        "selected_tables": ["EXAM_RECORD", "LAB_RESULT", "BM_LAB_ITEM"],
    },
    {
        "id": "M4",
        "question": "查询2021-01-01至2026-03-17期间，检验结果异常（isnormal=0）的中国籍受检人员数量，按检验项目分组，关联 exam_record 和 lab_result",
        "selected_tables": ["EXAM_RECORD", "LAB_RESULT"],
    },
    {
        "id": "M5",
        "question": "查询每个受检人员的套餐内金额、已收费金额、未收费金额，包含体检编号、姓名、性别、年龄、证件号、体检状态、单位、人员类型、科室、支付方式，关联 exam_record、control_status、bm_control_status、fee_record、bm_person_type",
        "selected_tables": ["EXAM_RECORD", "CONTROL_STATUS", "BM_CONTROL_STATUS", "FEE_RECORD", "BM_PERSON_TYPE"],
    },
    {
        "id": "M6",
        "question": "统计每个科室（department）的套餐总金额和已收费金额，关联 exam_record 和 fee_record，按科室分组",
        "selected_tables": ["EXAM_RECORD", "FEE_RECORD"],
    },
]


def _gold_tables(case: Dict[str, Any]) -> List[str]:
    return [str(t).upper() for t in case.get("selected_tables", []) if t]


def _table_metrics(candidates: List[str], gold: List[str]) -> Dict[str, Any]:
    pred = {str(t).upper() for t in candidates if t}
    expected = {str(t).upper() for t in gold if t}
    hit = pred & expected
    return {
        "expected_tables": sorted(expected),
        "candidate_tables": list(candidates),
        "table_hit_count": len(hit),
        "table_precision": round(len(hit) / max(len(pred), 1), 4),
        "table_recall": round(len(hit) / max(len(expected), 1), 4),
    }


def _baseline_inventory(baselines_dir: Path) -> List[Dict[str, Any]]:
    inventory = []
    for item in SOTA_SYSTEMS:
        path = baselines_dir / item["repo_dir"]
        inventory.append({
            **item,
            "local_path": str(path.resolve()),
            "downloaded": path.exists(),
        })
    return inventory


def _prepare_case_inputs(
    case: Dict[str, Any],
    start_date: str,
    end_date: str,
    use_gold_tables: bool,
    with_plan: bool,
) -> Dict[str, Any]:
    retrieve = m.retrieve_schema(
        case["question"],
        user_selected_tables=_gold_tables(case) if use_gold_tables else None,
    )
    candidates = retrieve.get("candidate_tables", [])
    if not candidates:
        raise RuntimeError("retrieve returned empty candidate_tables")

    main_tables = [t for t in candidates if not m.is_code_table(t)]
    if not main_tables:
        raise RuntimeError("no business table was retrieved")

    need_name = retrieve.get("need_name", False)
    code_hint = ""
    if need_name:
        code_hint = m.get_code_table_hint(main_tables)
        if code_hint:
            for t in m.expand_with_code_tables(main_tables):
                if t not in candidates:
                    candidates.append(t)
            candidates = candidates[:16]

    schema_ctx = m.schema_text_for_tables(candidates)
    schema_ctx += m.schema_kg_context_for_prompt(retrieve)
    join_hint = m.join_hints_text_for_tables(candidates)
    plan: Dict[str, Any] = {}

    if with_plan:
        plan = m.make_plan(case["question"], start_date, end_date, schema_ctx, code_hint)
        plan_tables_raw = [
            t.upper()
            for t in plan.get("needed_tables", [])
            if isinstance(t, str) and m.get_table_info(t)
        ]
        if plan_tables_raw:
            merged = list(dict.fromkeys(plan_tables_raw + candidates))
            for t in candidates:
                if m.is_code_table(t) and t not in merged:
                    merged.append(t)
            candidates = merged[:16]
            schema_ctx = m.schema_text_for_tables(candidates)
            schema_ctx += m.schema_kg_context_for_prompt(retrieve)
            join_hint = m.join_hints_text_for_tables(candidates)

    return {
        "retrieve": retrieve,
        "candidates": candidates,
        "main_tables": main_tables,
        "schema_ctx": schema_ctx,
        "join_hint": join_hint,
        "code_hint": code_hint,
        "plan": plan,
    }


def _oracle_validate(sql: str, timeout: int) -> Tuple[bool, str]:
    try:
        m.run_sql(f"SELECT * FROM ({sql}) WHERE ROWNUM <= 1", max_rows=1, timeout=timeout)
        return True, ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _direct_prompt_sql(
    question: str,
    start_date: str,
    end_date: str,
    schema_ctx: str,
    join_hint: str,
    code_hint: str,
    style: str,
) -> str:
    style_hint = {
        "direct_prompt": "Use a concise zero-shot prompt and generate one executable SQL.",
        "dail_sql_style": (
            "First do schema linking mentally: identify evidence tables, columns, and join keys. "
            "Then generate one SQL. Prefer the retrieved evidence over memorized names."
        ),
    }.get(style, "Generate one executable SQL.")
    prompt = f"""You are an Oracle 11g text-to-SQL system.
Task style: {style_hint}

Question:
{question}

Date range:
{start_date} to {end_date}

Schema evidence:
{schema_ctx}

Join hints:
{join_hint}

Code-table hints:
{code_hint}

Rules:
1. Return only one SELECT statement.
2. Use Oracle 11g syntax; do not use LIMIT or FETCH FIRST.
3. Use TO_DATE('{start_date}','YYYY-MM-DD') and TO_DATE('{end_date}','YYYY-MM-DD') for date filters when needed.
4. Use only tables and columns shown in the schema evidence.
5. Quote non-ASCII aliases with double quotes.
"""
    return m.extract_sql(m.ollama_chat(prompt, temperature=0.0, max_tokens=2048, timeout=180))


def _self_debug_sql(
    question: str,
    start_date: str,
    end_date: str,
    schema_ctx: str,
    join_hint: str,
    code_hint: str,
    rounds: int,
    sql_timeout: int,
) -> str:
    sql = _direct_prompt_sql(
        question, start_date, end_date, schema_ctx, join_hint, code_hint, "direct_prompt"
    )
    feedback = ""
    for _ in range(max(rounds, 1)):
        if not sql:
            feedback = "The previous generation returned empty SQL."
        else:
            ok, feedback = _oracle_validate(sql, sql_timeout)
            if ok:
                return sql
        prompt = f"""You are repairing Oracle 11g SQL.

Question:
{question}

Schema evidence:
{schema_ctx}

Join hints:
{join_hint}

Code-table hints:
{code_hint}

Previous SQL:
{sql}

Execution error:
{feedback}

Return only the corrected SELECT SQL."""
        sql = m.extract_sql(m.ollama_chat(prompt, temperature=0.2, max_tokens=2048, timeout=180))
    return sql


def run_one_case(
    model: str,
    case: Dict[str, Any],
    db_cfg: Dict[str, Any],
    use_final_repair: bool,
    rounds: int,
    start_date: str,
    end_date: str,
    sql_timeout: int,
    use_gold_tables: bool,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "case_id": case["id"],
        "model": model,
        "system": "coordsql",
        "use_final_repair": use_final_repair,
        "ok": False,
        "sql": "",
        "rows": 0,
        "error": "",
        "time_cost": 0.0,
        **_table_metrics([], _gold_tables(case)),
    }

    t0 = time.time()
    original_final_repair = m._final_repair

    try:
        m.OLLAMA_SQL_MODEL = model

        with m.app.test_request_context("/"):
            session["db_config"] = db_cfg

            if not use_final_repair:
                def _no_final_repair(**kwargs):
                    return kwargs.get("last_sql", "")
                m._final_repair = _no_final_repair

            prepared = _prepare_case_inputs(
                case=case,
                start_date=start_date,
                end_date=end_date,
                use_gold_tables=use_gold_tables,
                with_plan=True,
            )
            retrieve = prepared["retrieve"]
            candidates = prepared["candidates"]
            schema_ctx = prepared["schema_ctx"]
            join_hint = prepared["join_hint"]
            code_hint = prepared["code_hint"]
            plan = prepared["plan"]
            result.update(_table_metrics(candidates, _gold_tables(case)))
            result["rag_backend"] = retrieve.get("rag_backend", "unknown")

            sql = m.generate_with_repair(
                question=case["question"],
                start_date=start_date,
                end_date=end_date,
                plan=plan,
                schema_ctx=schema_ctx,
                allowed_tables=candidates,
                join_hint=join_hint,
                code_hint=code_hint,
                rounds=rounds,
            )

            result["sql"] = sql
            if not sql:
                raise RuntimeError("未生成 SQL")

            cols, rows = m.run_sql(sql, max_rows=5, timeout=sql_timeout)
            result["rows"] = len(rows)
            result["ok"] = True
            result["columns"] = cols

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        result["traceback"] = traceback.format_exc(limit=2)
    finally:
        m._final_repair = original_final_repair
        result["time_cost"] = round(time.time() - t0, 2)

    return result


def run_sota_baseline_case(
    model: str,
    system: str,
    case: Dict[str, Any],
    db_cfg: Dict[str, Any],
    rounds: int,
    start_date: str,
    end_date: str,
    sql_timeout: int,
    use_gold_tables: bool,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "case_id": case["id"],
        "model": model,
        "system": system,
        "ok": False,
        "sql": "",
        "rows": 0,
        "error": "",
        "time_cost": 0.0,
        **_table_metrics([], _gold_tables(case)),
    }

    t0 = time.time()
    try:
        m.OLLAMA_SQL_MODEL = model
        with m.app.test_request_context("/"):
            session["db_config"] = db_cfg
            prepared = _prepare_case_inputs(
                case=case,
                start_date=start_date,
                end_date=end_date,
                use_gold_tables=use_gold_tables,
                with_plan=(system == "din_sql_style"),
            )
            retrieve = prepared["retrieve"]
            candidates = prepared["candidates"]
            schema_ctx = prepared["schema_ctx"]
            join_hint = prepared["join_hint"]
            code_hint = prepared["code_hint"]
            plan = prepared["plan"]
            result.update(_table_metrics(candidates, _gold_tables(case)))
            result["rag_backend"] = retrieve.get("rag_backend", "unknown")

            if system == "din_sql_style":
                sql = m.generate_multi_sql(
                    question=case["question"],
                    start_date=start_date,
                    end_date=end_date,
                    plan=plan or {"needed_tables": candidates[:4]},
                    schema_ctx=schema_ctx,
                    join_hint=join_hint,
                    code_hint=code_hint,
                    history="",
                    temperature=0.0,
                )
            elif system == "self_debug_style":
                sql = _self_debug_sql(
                    question=case["question"],
                    start_date=start_date,
                    end_date=end_date,
                    schema_ctx=schema_ctx,
                    join_hint=join_hint,
                    code_hint=code_hint,
                    rounds=min(rounds, 3),
                    sql_timeout=sql_timeout,
                )
            elif system in {"direct_prompt", "dail_sql_style"}:
                sql = _direct_prompt_sql(
                    question=case["question"],
                    start_date=start_date,
                    end_date=end_date,
                    schema_ctx=schema_ctx,
                    join_hint=join_hint,
                    code_hint=code_hint,
                    style=system,
                )
            else:
                raise ValueError(f"Unknown baseline system: {system}")

            result["sql"] = sql
            if not sql:
                raise RuntimeError("baseline did not generate SQL")

            cols, rows = m.run_sql(sql, max_rows=5, timeout=sql_timeout)
            result["rows"] = len(rows)
            result["ok"] = True
            result["columns"] = cols

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        result["traceback"] = traceback.format_exc(limit=2)
    finally:
        result["time_cost"] = round(time.time() - t0, 2)

    return result


def summarize(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(results)
    ok_cnt = sum(1 for x in results if x.get("ok"))
    avg_time = round(sum(float(x.get("time_cost", 0)) for x in results) / max(total, 1), 2)
    recall_values = [float(x.get("table_recall", 0.0)) for x in results]
    precision_values = [float(x.get("table_precision", 0.0)) for x in results]
    return {
        "total": total,
        "ok": ok_cnt,
        "exec_success_rate": round(ok_cnt / max(total, 1) * 100, 2),
        "avg_time_sec": avg_time,
        "avg_table_recall": round(sum(recall_values) / max(len(recall_values), 1) * 100, 2),
        "avg_table_precision": round(sum(precision_values) / max(len(precision_values), 1) * 100, 2),
    }


def write_markdown_report(out: Dict[str, Any], path: Path) -> None:
    lines = [
        "# Text-to-SQL Experiment Summary",
        "",
        f"- Time: {out['meta']['time']}",
        f"- Cases: {', '.join(out['meta']['cases'])}",
        f"- Repair rounds: {out['meta']['rounds']}",
        f"- Gold-table hints: {out['meta']['use_gold_tables']}",
        "",
        "## CoordSQL Ablation",
        "",
        "| Model | Without final repair | With final repair | Delta |",
        "|---|---:|---:|---:|",
    ]
    for model, item in out.get("summary", {}).items():
        lines.append(
            f"| {model} | {item['without_final_repair']:.2f}% | "
            f"{item['with_final_repair']:.2f}% | {item['delta']:.2f}% |"
        )

    sota_summary = out.get("sota_baseline_summary", {})
    if sota_summary:
        lines.extend([
            "",
            "## SOTA-Style Baselines",
            "",
            "| Model | System | Exec success | Avg time (s) | Table recall | Table precision |",
            "|---|---|---:|---:|---:|---:|",
        ])
        for model, systems in sota_summary.items():
            for system, item in systems.items():
                lines.append(
                    f"| {model} | {system} | {item['exec_success_rate']:.2f}% | "
                    f"{item['avg_time_sec']:.2f} | {item['avg_table_recall']:.2f}% | "
                    f"{item['avg_table_precision']:.2f}% |"
                )

    inventory = out.get("sota_system_sources", [])
    if inventory:
        lines.extend([
            "",
            "## Downloaded External Systems",
            "",
            "| System | Downloaded | Local path | Source |",
            "|---|---|---|---|",
        ])
        for item in inventory:
            downloaded = "yes" if item.get("downloaded") else "no"
            lines.append(
                f"| {item['name']} | {downloaded} | `{item['local_path']}` | {item['url']} |"
            )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="final repair 消融实验")
    p.add_argument("--models", nargs="+", default=DEFAULT_MODELS, help="待评测 Ollama 模型列表")
    p.add_argument("--baseline-models", nargs="+", default=None, help="models used for SOTA-style baselines; default: first --models item")
    p.add_argument("--baseline-systems", nargs="+", default=RUNNABLE_BASELINES, choices=RUNNABLE_BASELINES)
    p.add_argument("--include-sota", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--use-gold-tables", action="store_true", help="feed selected_tables as oracle schema hints; off by default for fair retrieval")
    p.add_argument("--baselines-dir", default="baselines")
    p.add_argument("--db-user", default=m.DEFAULT_DB_CONFIG.get("DB_USER", ""))
    p.add_argument("--db-password", default=m.DEFAULT_DB_CONFIG.get("DB_PASSWORD", ""))
    p.add_argument("--db-host", default=m.DEFAULT_DB_CONFIG.get("DB_HOST", "127.0.0.1"))
    p.add_argument("--db-port", type=int, default=int(m.DEFAULT_DB_CONFIG.get("DB_PORT", 1521)))
    p.add_argument("--db-service", default=m.DEFAULT_DB_CONFIG.get("DB_SERVICE_NAME", "oral"))
    p.add_argument("--start-date", default="2021-01-01")
    p.add_argument("--end-date", default="2026-03-17")
    p.add_argument("--rounds", type=int, default=5)
    p.add_argument("--sql-timeout", type=int, default=60)
    p.add_argument("--output", default="xiaorong_results.json")
    p.add_argument("--markdown-output", default="xiaorong_results.md")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    if not args.db_user or not args.db_password:
        raise ValueError("请提供 --db-user 和 --db-password")

    db_cfg = {
        "DB_USER": args.db_user,
        "DB_PASSWORD": args.db_password,
        "DB_HOST": args.db_host,
        "DB_PORT": args.db_port,
        "DB_SERVICE_NAME": args.db_service,
    }

    all_results: List[Dict[str, Any]] = []
    summary: Dict[str, Any] = {}
    sota_results: List[Dict[str, Any]] = []
    sota_summary: Dict[str, Any] = {}
    baseline_inventory = _baseline_inventory(Path(args.baselines_dir))

    print("=" * 70)
    print("消融实验：有/无 final repair")
    print("=" * 70)

    for model in args.models:
        print(f"\n🧪 模型: {model}")
        model_results: Dict[str, Any] = {}

        for use_final in [False, True]:
            tag = "with_final_repair" if use_final else "without_final_repair"
            print(f"  -> 设置: {tag}")
            runs: List[Dict[str, Any]] = []

            for case in BENCHMARK_CASES:
                print(f"     · {case['id']} ...", end=" ")
                r = run_one_case(
                    model=model,
                    case=case,
                    db_cfg=db_cfg,
                    use_final_repair=use_final,
                    rounds=args.rounds,
                    start_date=args.start_date,
                    end_date=args.end_date,
                    sql_timeout=args.sql_timeout,
                    use_gold_tables=args.use_gold_tables,
                )
                runs.append(r)
                print("✅" if r.get("ok") else "❌")

            model_results[tag] = {
                "summary": summarize(runs),
                "cases": runs,
            }

        w = model_results["with_final_repair"]["summary"]["exec_success_rate"]
        wo = model_results["without_final_repair"]["summary"]["exec_success_rate"]
        model_results["delta_exec_success_rate"] = round(w - wo, 2)
        summary[model] = {
            "with_final_repair": w,
            "without_final_repair": wo,
            "delta": model_results["delta_exec_success_rate"],
        }

        all_results.append({
            "model": model,
            "result": model_results,
        })

    if args.include_sota:
        baseline_models = args.baseline_models or args.models[:1]
        print("\n" + "=" * 70)
        print("SOTA-style text-to-SQL baseline comparison")
        print("=" * 70)
        for model in baseline_models:
            sota_summary[model] = {}
            for system in args.baseline_systems:
                print(f"\n🧪 baseline: {system} / model: {model}")
                runs: List[Dict[str, Any]] = []
                for case in BENCHMARK_CASES:
                    print(f"     · {case['id']} ...", end=" ")
                    r = run_sota_baseline_case(
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
                    runs.append(r)
                    print("✅" if r.get("ok") else "❌")
                system_summary = summarize(runs)
                sota_summary[model][system] = system_summary
                sota_results.append({
                    "model": model,
                    "system": system,
                    "summary": system_summary,
                    "cases": runs,
                })

    out = {
        "meta": {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "start_date": args.start_date,
            "end_date": args.end_date,
            "rounds": args.rounds,
            "cases": [c["id"] for c in BENCHMARK_CASES],
            "use_gold_tables": args.use_gold_tables,
            "baseline_systems": args.baseline_systems,
        },
        "summary": summary,
        "sota_baseline_summary": sota_summary,
        "sota_system_sources": baseline_inventory,
        "details": all_results,
        "sota_details": sota_results,
    }

    out_path = Path(args.output)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = Path(args.markdown_output)
    write_markdown_report(out, md_path)

    print("\n" + "=" * 70)
    print("实验完成")
    print(f"结果文件: {out_path.resolve()}")
    print(f"Markdown汇总: {md_path.resolve()}")
    print("=" * 70)


if __name__ == "__main__":
    main()


