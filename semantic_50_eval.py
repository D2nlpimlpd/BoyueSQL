#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Build and run a 50-question semantic Text-to-SQL benchmark.

The benchmark is generated from the live Oracle catalog and the local data
dictionary: 25 single-table questions and 25 multi-table questions.  Each case
has a gold SQL query, so evaluation can report stronger evidence than simple
execution success.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import time
import traceback
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import main as m
import xiaorong as x
from flask import session


PYTHON_MODEL = "qwen3-vl:8b"
DEFAULT_BENCHMARK = "semantic_50_benchmark.json"
DEFAULT_RESULTS = "semantic_50_results.json"
DEFAULT_REPORT = "semantic_50_results.md"


def _db_cfg(args: argparse.Namespace) -> Dict[str, Any]:
    return {
        "DB_USER": args.db_user,
        "DB_PASSWORD": args.db_password,
        "DB_HOST": args.db_host,
        "DB_PORT": args.db_port,
        "DB_SERVICE_NAME": args.db_service,
    }


def safe_ident(name: str) -> str:
    name = (name or "").upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_#$]*", name):
        raise ValueError(f"unsafe identifier: {name!r}")
    return name


def label_for(table: str, column: Optional[str] = None) -> str:
    info = m.get_table_info(table) or {}
    if column is None:
        return str(info.get("table_cn") or info.get("table_name") or table)
    meta = m.get_table_columns(table).get(column.upper(), {})
    return str(meta.get("cn") or column)


def table_dict_type(table: str) -> str:
    return "code" if m.is_code_table(table) else "main"


def run_sql(sql: str, max_rows: int = 200, timeout: int = 45) -> Tuple[List[str], List[List[Any]]]:
    transient_markers = ("ORA-12516", "ORA-12520", "ORA-12519", "DPY-6005", "DPI-1080")
    last_exc: Optional[Exception] = None
    for attempt in range(3):
        try:
            return m.run_sql(sql, max_rows=max_rows, timeout=timeout)
        except Exception as exc:
            last_exc = exc
            msg = str(exc)
            if attempt < 2 and any(marker in msg for marker in transient_markers):
                time.sleep(1.0 + attempt)
                continue
            raise
    raise last_exc if last_exc else RuntimeError("unknown SQL execution failure")


def sql_ok(sql: str, timeout: int = 20) -> bool:
    try:
        m.run_sql(f"SELECT * FROM ({sql}) WHERE ROWNUM <= 1", max_rows=1, timeout=timeout)
        return True
    except Exception:
        return False


def has_rows(table: str) -> bool:
    try:
        cols, rows = run_sql(f"SELECT 1 AS X FROM {safe_ident(table)} WHERE ROWNUM <= 1", max_rows=1, timeout=15)
        return bool(rows)
    except Exception:
        return False


def live_column_meta(table: str) -> Dict[str, Dict[str, Any]]:
    table = safe_ident(table)
    sql = (
        "SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH, DATA_PRECISION "
        "FROM USER_TAB_COLUMNS WHERE TABLE_NAME = :table_name"
    )
    conn = m.get_conn_from_session()
    try:
        cur = conn.cursor()
        cur.execute(sql, {"table_name": table})
        out: Dict[str, Dict[str, Any]] = {}
        for name, dtype, length, precision in cur.fetchall():
            out[str(name).upper()] = {
                "data_type": str(dtype).upper(),
                "length": int(length or 0),
                "precision": int(precision or 0) if precision is not None else None,
            }
        return out
    finally:
        conn.close()


def is_numeric(dtype: str) -> bool:
    return dtype.upper() in {"NUMBER", "FLOAT", "BINARY_FLOAT", "BINARY_DOUBLE", "INTEGER"}


def is_date(dtype: str) -> bool:
    return "DATE" in dtype.upper() or "TIMESTAMP" in dtype.upper()


def is_text(dtype: str) -> bool:
    dtype = dtype.upper()
    return any(x in dtype for x in ["CHAR", "VARCHAR", "NCHAR", "NVARCHAR"])


def distinct_sample_count(table: str, column: str) -> Optional[int]:
    try:
        sql = (
            f"SELECT COUNT(DISTINCT {safe_ident(column)}) AS C "
            f"FROM (SELECT {safe_ident(column)} FROM {safe_ident(table)} "
            f"WHERE {safe_ident(column)} IS NOT NULL AND ROWNUM <= 500)"
        )
        _cols, rows = run_sql(sql, max_rows=1, timeout=20)
        return int(rows[0][0]) if rows else 0
    except Exception:
        return None


def choose_columns(table: str) -> Dict[str, List[str]]:
    live = live_column_meta(table)
    dict_cols = m.get_table_columns(table)
    columns = [c for c in dict_cols if c in live]
    numeric = [c for c in columns if is_numeric(live[c]["data_type"])]
    dates = [c for c in columns if is_date(live[c]["data_type"])]
    text = [c for c in columns if is_text(live[c]["data_type"])]
    groupable: List[str] = []
    for col in text + [c for c in columns if c.endswith(("_TYPE", "_STATUS", "_CODE", "_FLAG", "_SEX"))]:
        if col in groupable:
            continue
        cnt = distinct_sample_count(table, col)
        if cnt is not None and 2 <= cnt <= 80:
            groupable.append(col)
        if len(groupable) >= 4:
            break
    display = [c for c in columns if c not in numeric][:4] or columns[:4]
    return {
        "all": columns,
        "numeric": numeric,
        "date": dates,
        "text": text,
        "group": groupable,
        "display": display,
    }


def normalize_cell(v: Any) -> str:
    if v is None:
        return "<NULL>"
    if isinstance(v, Decimal):
        return f"{float(v):.8g}"
    if isinstance(v, float):
        return f"{v:.8g}"
    text = str(v).strip()
    try:
        num = float(text)
        return f"{num:.8g}"
    except Exception:
        return text


def result_signature(cols: Sequence[str], rows: Sequence[Sequence[Any]]) -> Dict[str, Any]:
    norm_cols = [re.sub(r"\s+", "_", str(c).strip().upper()) for c in cols]
    norm_rows = [tuple(normalize_cell(v) for v in row) for row in rows]
    return {
        "columns": norm_cols,
        "rows_sorted": sorted(norm_rows),
        "row_count": len(norm_rows),
    }


def sql_tables(sql: str) -> List[str]:
    found = []
    for match in re.finditer(r"\b(?:FROM|JOIN)\s+([A-Z][A-Z0-9_#$]*)", sql or "", flags=re.I):
        found.append(match.group(1).upper())
    return list(dict.fromkeys(found))


def sql_column_coverage(sql: str, columns: Sequence[str]) -> float:
    sql_u = (sql or "").upper()
    required = [c.upper() for c in columns if c]
    if not required:
        return 1.0
    hits = sum(1 for c in required if re.search(rf"\b{re.escape(c)}\b", sql_u))
    return hits / len(required)


def table_coverage(sql: str, tables: Sequence[str]) -> float:
    pred = set(sql_tables(sql))
    gold = {t.upper() for t in tables}
    if not gold:
        return 1.0
    return len(pred & gold) / len(gold)


def compare_results(gold: Dict[str, Any], pred: Dict[str, Any]) -> bool:
    if gold["row_count"] != pred["row_count"]:
        return False
    return gold["rows_sorted"] == pred["rows_sorted"]


def make_case(
    case_id: str,
    query_type: str,
    question: str,
    gold_sql: str,
    tables: Sequence[str],
    columns: Sequence[str],
    op: str,
) -> Dict[str, Any]:
    return {
        "id": case_id,
        "query_type": query_type,
        "question": question,
        "gold_sql": gold_sql,
        "selected_tables": [t.upper() for t in tables],
        "required_columns": [c.upper() for c in columns],
        "operation": op,
    }


def table_candidates() -> List[str]:
    tables = list(m.DATA_DICT.get("main_tables", {}).keys())
    scored: List[Tuple[int, str]] = []
    for table in tables:
        table_u = table.upper()
        try:
            cols = choose_columns(table_u)
            if not cols["all"] or not has_rows(table_u):
                continue
            score = len(cols["group"]) * 3 + len(cols["numeric"]) * 2 + len(cols["date"]) + len(cols["display"])
            scored.append((score, table_u))
        except Exception:
            continue
    scored.sort(reverse=True)
    return [t for _score, t in scored]


def build_single_cases(limit: int = 25) -> List[Dict[str, Any]]:
    cases: List[Dict[str, Any]] = []
    idx = 1
    for table in table_candidates():
        cols = choose_columns(table)
        t_cn = label_for(table)
        if len(cols["display"]) >= 2 and len(cases) < limit:
            display = cols["display"][: min(4, len(cols["display"]))]
            select_expr = ", ".join(display)
            q_cols = "、".join(f"{label_for(table, c)}({c})" for c in display)
            question = f"从 {t_cn}（{table}）表查询前10条记录，返回{q_cols}。"
            sql = f"SELECT {select_expr} FROM {table} WHERE ROWNUM <= 10"
            cases.append(make_case(f"S{idx:02d}", "single", question, sql, [table], display, "list"))
            idx += 1
        if cols["group"] and len(cases) < limit:
            g = cols["group"][0]
            question = f"在 {t_cn}（{table}）表前5000条非空样本内，按{label_for(table, g)}({g})分组统计记录数，按记录数降序返回。"
            sql = (
                f"SELECT {g} AS GROUP_VALUE, COUNT(*) AS CNT "
                f"FROM (SELECT {g} FROM {table} WHERE {g} IS NOT NULL AND ROWNUM <= 5000) "
                f"GROUP BY {g} ORDER BY CNT DESC, GROUP_VALUE"
            )
            cases.append(make_case(f"S{idx:02d}", "single", question, sql, [table], [g], "group_count"))
            idx += 1
        if cols["numeric"] and len(cases) < limit:
            n = cols["numeric"][0]
            question = f"在 {t_cn}（{table}）表前5000条非空样本内，统计{label_for(table, n)}({n})的合计、平均值、最小值和最大值。"
            sql = (
                f"SELECT SUM({n}) AS SUM_VALUE, AVG({n}) AS AVG_VALUE, "
                f"MIN({n}) AS MIN_VALUE, MAX({n}) AS MAX_VALUE "
                f"FROM (SELECT {n} FROM {table} WHERE {n} IS NOT NULL AND ROWNUM <= 5000)"
            )
            cases.append(make_case(f"S{idx:02d}", "single", question, sql, [table], [n], "numeric_summary"))
            idx += 1
        if cols["date"] and len(cases) < limit:
            d = cols["date"][0]
            question = f"在 {t_cn}（{table}）表前5000条非空样本内，统计{label_for(table, d)}({d})的最早日期、最晚日期和非空记录数。"
            sql = (
                f"SELECT MIN({d}) AS MIN_DATE, MAX({d}) AS MAX_DATE, COUNT(*) AS CNT "
                f"FROM (SELECT {d} FROM {table} WHERE {d} IS NOT NULL AND ROWNUM <= 5000)"
            )
            cases.append(make_case(f"S{idx:02d}", "single", question, sql, [table], [d], "date_summary"))
            idx += 1
        if len(cases) >= limit:
            break
    return cases[:limit]


def candidate_join_pairs() -> List[Tuple[str, str, str, str]]:
    tables = table_candidates()[:80]
    table_set = set(tables)
    by_col: Dict[str, List[str]] = {}
    for table in tables:
        for col in choose_columns(table)["all"]:
            if col.endswith(("_NO", "_ID", "_CODE", "NO", "ID")) or col in {"EXAM_NO", "FEE_NO", "CARD_NO"}:
                by_col.setdefault(col, []).append(table)
    pairs: List[Tuple[str, str, str, str]] = []
    for col, ts in by_col.items():
        for i, left in enumerate(ts):
            for right in ts[i + 1 :]:
                if left == right or left not in table_set or right not in table_set:
                    continue
                pairs.append((left, col, right, col))
    # Prefer dictionary relationship entries when available.
    for rel in getattr(m.SCHEMA_INDEX, "rel_entries", []):
        left = str(rel.get("left") or "").upper()
        right = str(rel.get("right") or "").upper()
        col = str(rel.get("column") or "").upper()
        rcol = str(rel.get("right_column") or col).upper()
        if left in table_set and right in table_set and col and rcol:
            pairs.insert(0, (left, col, right, rcol))
    out: List[Tuple[str, str, str, str]] = []
    seen = set()
    for p in pairs:
        key = tuple(p)
        rev = (p[2], p[3], p[0], p[1])
        if key in seen or rev in seen:
            continue
        seen.add(key)
        if sql_ok(f"SELECT 1 FROM {p[0]} a JOIN {p[2]} b ON a.{p[1]} = b.{p[3]} WHERE ROWNUM <= 1", timeout=15):
            out.append(p)
    return out


def build_multi_cases(limit: int = 25) -> List[Dict[str, Any]]:
    cases: List[Dict[str, Any]] = []
    idx = 1
    for left, lcol, right, rcol in candidate_join_pairs():
        lc = choose_columns(left)
        rc = choose_columns(right)
        l_cn = label_for(left)
        r_cn = label_for(right)
        if len(cases) < limit:
            question = (
                f"关联 {l_cn}（{left}）和 {r_cn}（{right}），按 {left}.{lcol} = {right}.{rcol} "
                f"统计前5000条匹配样本的记录总数。"
            )
            sql = (
                f"SELECT COUNT(*) AS CNT FROM (SELECT 1 FROM {left} a "
                f"JOIN {right} b ON a.{lcol} = b.{rcol} WHERE ROWNUM <= 5000)"
            )
            cases.append(make_case(f"M{idx:02d}", "multi", question, sql, [left, right], [lcol, rcol], "join_count"))
            idx += 1
        if lc["display"] and rc["display"] and len(cases) < limit:
            ldisp = next((c for c in lc["display"] if c != lcol), lc["display"][0])
            rdisp = next((c for c in rc["display"] if c != rcol), rc["display"][0])
            question = (
                f"关联 {left} 和 {right}，使用 {left}.{lcol} = {right}.{rcol}，"
                f"返回前10条 {label_for(left, ldisp)}({left}.{ldisp}) 和 "
                f"{label_for(right, rdisp)}({right}.{rdisp})。"
            )
            sql = (
                f"SELECT a.{ldisp} AS LEFT_VALUE, b.{rdisp} AS RIGHT_VALUE "
                f"FROM {left} a JOIN {right} b ON a.{lcol} = b.{rcol} WHERE ROWNUM <= 10"
            )
            cases.append(make_case(f"M{idx:02d}", "multi", question, sql, [left, right], [lcol, rcol, ldisp, rdisp], "join_list"))
            idx += 1
        if lc["group"] and len(cases) < limit:
            g = lc["group"][0]
            question = (
                f"关联 {left} 和 {right}，按 {left}.{lcol} = {right}.{rcol}，"
                f"在前5000条匹配样本内，再按 {left} 的{label_for(left, g)}({g})分组统计匹配记录数。"
            )
            sql = (
                f"SELECT GROUP_VALUE, COUNT(*) AS CNT FROM ("
                f"SELECT a.{g} AS GROUP_VALUE FROM {left} a "
                f"JOIN {right} b ON a.{lcol} = b.{rcol} "
                f"WHERE a.{g} IS NOT NULL AND ROWNUM <= 5000) "
                f"GROUP BY GROUP_VALUE ORDER BY CNT DESC, GROUP_VALUE"
            )
            cases.append(make_case(f"M{idx:02d}", "multi", question, sql, [left, right], [lcol, rcol, g], "join_group_count"))
            idx += 1
        if lc["group"] and rc["numeric"] and len(cases) < limit:
            g = lc["group"][0]
            n = rc["numeric"][0]
            question = (
                f"关联 {left} 和 {right}，按 {left}.{lcol} = {right}.{rcol}，"
                f"在前5000条匹配样本内，按 {left}.{g} 分组统计 {right}.{n} 的合计值。"
            )
            sql = (
                f"SELECT GROUP_VALUE, SUM(NUM_VALUE) AS SUM_VALUE FROM ("
                f"SELECT a.{g} AS GROUP_VALUE, b.{n} AS NUM_VALUE FROM {left} a "
                f"JOIN {right} b ON a.{lcol} = b.{rcol} "
                f"WHERE a.{g} IS NOT NULL AND b.{n} IS NOT NULL AND ROWNUM <= 5000) "
                f"GROUP BY GROUP_VALUE ORDER BY SUM_VALUE DESC, GROUP_VALUE"
            )
            cases.append(make_case(f"M{idx:02d}", "multi", question, sql, [left, right], [lcol, rcol, g, n], "join_group_sum"))
            idx += 1
        if len(cases) >= limit:
            break
    return cases[:limit]


def validate_cases(cases: List[Dict[str, Any]], timeout: int = 45) -> List[Dict[str, Any]]:
    valid: List[Dict[str, Any]] = []
    for case in cases:
        try:
            cols, rows = run_sql(case["gold_sql"], max_rows=100, timeout=timeout)
            case["gold_columns"] = cols
            case["gold_preview_rows"] = rows[:5]
            case["gold_row_count_preview"] = len(rows)
            valid.append(case)
        except Exception as exc:
            print(f"[skip] {case['id']} gold failed: {type(exc).__name__}: {exc}")
    return valid


def build_benchmark(path: Path, single_n: int = 25, multi_n: int = 25, timeout: int = 45) -> Dict[str, Any]:
    single = validate_cases(build_single_cases(single_n * 2), timeout=timeout)[:single_n]
    multi = validate_cases(build_multi_cases(multi_n * 2), timeout=timeout)[:multi_n]
    if len(single) < single_n or len(multi) < multi_n:
        raise RuntimeError(f"not enough valid cases: single={len(single)} multi={len(multi)}")
    out = {
        "meta": {
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": "live Oracle catalog + local database dictionary",
            "single_count": len(single),
            "multi_count": len(multi),
        },
        "cases": single + multi,
    }
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def evaluate_prediction(case: Dict[str, Any], sql: str, timeout: int) -> Dict[str, Any]:
    evidence: Dict[str, Any] = {
        "exec_ok": False,
        "result_exact": False,
        "semantic_pass": False,
        "semantic_score": 0.0,
        "has_null_placeholder": bool(re.search(r"\bNULL\s+AS\b", sql or "", flags=re.I)),
        "table_coverage": table_coverage(sql, case.get("selected_tables", [])),
        "column_coverage": sql_column_coverage(sql, case.get("required_columns", [])),
        "pred_error": "",
    }
    try:
        gold_cols, gold_rows = run_sql(case["gold_sql"], max_rows=200, timeout=timeout)
        pred_cols, pred_rows = run_sql(sql, max_rows=200, timeout=timeout)
        evidence["exec_ok"] = True
        gold_sig = result_signature(gold_cols, gold_rows)
        pred_sig = result_signature(pred_cols, pred_rows)
        evidence["gold_signature"] = {
            "columns": gold_sig["columns"],
            "row_count": gold_sig["row_count"],
        }
        evidence["pred_signature"] = {
            "columns": pred_sig["columns"],
            "row_count": pred_sig["row_count"],
        }
        evidence["result_exact"] = compare_results(gold_sig, pred_sig)
    except Exception as exc:
        evidence["pred_error"] = f"{type(exc).__name__}: {exc}"

    score = 0.0
    score += 0.20 if evidence["exec_ok"] else 0.0
    score += 0.20 * evidence["table_coverage"]
    score += 0.20 * evidence["column_coverage"]
    score += 0.35 if evidence["result_exact"] else 0.0
    score += 0.05 if not evidence["has_null_placeholder"] else 0.0
    evidence["semantic_score"] = round(score, 4)
    evidence["semantic_pass"] = bool(
        evidence["exec_ok"]
        and evidence["result_exact"]
        and evidence["table_coverage"] >= 0.999
        and evidence["column_coverage"] >= 0.999
        and not evidence["has_null_placeholder"]
    )
    return evidence


def run_system(
    system: str,
    model: str,
    case: Dict[str, Any],
    db_cfg: Dict[str, Any],
    rounds: int,
    start_date: str,
    end_date: str,
    timeout: int,
    use_gold_tables: bool,
) -> Dict[str, Any]:
    base = {
        "case_id": case["id"],
        "query_type": case["query_type"],
        "system": system,
        "model": model,
        "question": case["question"],
        "gold_sql": case["gold_sql"],
        "expected_tables": case["selected_tables"],
        "required_columns": case["required_columns"],
        "sql": "",
        "error": "",
        "time_cost": 0.0,
    }
    t0 = time.time()
    try:
        if system == "coordsql_with_final":
            r = x.run_one_case(model, case, db_cfg, True, rounds, start_date, end_date, timeout, use_gold_tables)
        elif system == "coordsql_no_final":
            r = x.run_one_case(model, case, db_cfg, False, rounds, start_date, end_date, timeout, use_gold_tables)
        elif system in {"coordsql_no_template", "coordsql_no_final_no_template"}:
            original_template = getattr(m, "explicit_structured_sql", None)
            try:
                m.explicit_structured_sql = lambda *args, **kwargs: ""
                r = x.run_one_case(
                    model,
                    case,
                    db_cfg,
                    system == "coordsql_no_template",
                    rounds,
                    start_date,
                    end_date,
                    timeout,
                    use_gold_tables,
                )
            finally:
                if original_template is not None:
                    m.explicit_structured_sql = original_template
        elif system == "coordsql_schema_only":
            m.OLLAMA_SQL_MODEL = model
            prepared = x._prepare_case_inputs(
                case=case,
                start_date=start_date,
                end_date=end_date,
                use_gold_tables=use_gold_tables,
                with_plan=False,
            )
            sql = m._schema_only_fallback_sql(
                question=case["question"],
                start_date=start_date,
                end_date=end_date,
                allowed_tables=prepared["candidates"],
                join_hint=prepared["join_hint"],
            )
            r = {
                "case_id": case["id"],
                "system": system,
                "model": model,
                "sql": sql,
                "ok": False,
                "error": "",
                "time_cost": 0.0,
                "candidate_tables": prepared["candidates"],
                "table_recall": None,
                "table_precision": None,
                "rag_backend": prepared["retrieve"].get("rag_backend", "unknown"),
            }
            if sql:
                try:
                    cols, rows = run_sql(sql, max_rows=5, timeout=timeout)
                    r.update({"ok": True, "columns": cols, "rows": len(rows)})
                except Exception as exc:
                    r["error"] = f"{type(exc).__name__}: {exc}"
            else:
                r["error"] = "schema-only fallback did not generate SQL"
        elif system in x.RUNNABLE_BASELINES:
            r = x.run_sota_baseline_case(model, system, case, db_cfg, rounds, start_date, end_date, timeout, use_gold_tables)
        else:
            raise ValueError(f"unknown system: {system}")
        base.update({
            "sql": r.get("sql", ""),
            "error": r.get("error", ""),
            "generation_ok": bool(r.get("ok")),
            "retrieval": {
                "table_recall": r.get("table_recall"),
                "table_precision": r.get("table_precision"),
                "candidate_tables": r.get("candidate_tables"),
            },
        })
        if base["sql"]:
            base["semantic_evidence"] = evaluate_prediction(case, base["sql"], timeout)
        else:
            base["semantic_evidence"] = evaluate_prediction(case, "SELECT 1 FROM DUAL WHERE 1=0", timeout)
            base["semantic_evidence"]["exec_ok"] = False
            base["semantic_evidence"]["result_exact"] = False
            base["semantic_evidence"]["semantic_pass"] = False
            base["semantic_evidence"]["semantic_score"] = 0.0
    except Exception as exc:
        base["error"] = f"{type(exc).__name__}: {exc}"
        base["traceback"] = traceback.format_exc(limit=2)
        base["semantic_evidence"] = {
            "exec_ok": False,
            "result_exact": False,
            "semantic_pass": False,
            "semantic_score": 0.0,
            "pred_error": base["error"],
        }
    finally:
        base["time_cost"] = round(time.time() - t0, 2)
    return base


def summarize_runs(runs: List[Dict[str, Any]], include_by_type: bool = True) -> Dict[str, Any]:
    total = len(runs)
    ev = [r.get("semantic_evidence", {}) for r in runs]
    by_type: Dict[str, Any] = {}
    if include_by_type:
        for qtype in sorted({r["query_type"] for r in runs}):
            subset = [r for r in runs if r["query_type"] == qtype]
            by_type[qtype] = summarize_runs(subset, include_by_type=False)
    scores = [float(e.get("semantic_score", 0.0)) for e in ev]
    return {
        "total": total,
        "exec_ok": sum(1 for e in ev if e.get("exec_ok")),
        "exec_rate": round(sum(1 for e in ev if e.get("exec_ok")) / max(total, 1) * 100, 2),
        "result_exact": sum(1 for e in ev if e.get("result_exact")),
        "result_exact_rate": round(sum(1 for e in ev if e.get("result_exact")) / max(total, 1) * 100, 2),
        "semantic_pass": sum(1 for e in ev if e.get("semantic_pass")),
        "semantic_pass_rate": round(sum(1 for e in ev if e.get("semantic_pass")) / max(total, 1) * 100, 2),
        "avg_semantic_score": round(statistics.mean(scores) if scores else 0.0, 4),
        "avg_time_sec": round(statistics.mean([float(r.get("time_cost", 0.0)) for r in runs]) if runs else 0.0, 2),
        "by_type": by_type,
    }


def write_report(out: Dict[str, Any], path: Path) -> None:
    lines = [
        "# 50-Question Semantic Text-to-SQL Evaluation",
        "",
        f"- Time: {out['meta']['time']}",
        f"- Benchmark: `{out['meta']['benchmark_path']}`",
        f"- Cases: {out['meta']['case_count']} (single={out['meta']['single_count']}, multi={out['meta']['multi_count']})",
        f"- Gold-table hints: {out['meta']['use_gold_tables']}",
        "",
        "## Summary",
        "",
        "| System | Model | Cases | Exec | Result exact | Semantic pass | Avg score | Avg time |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for item in out["summaries"]:
        s = item["summary"]
        lines.append(
            f"| {item['system']} | {item['model']} | {s['total']} | {s['exec_rate']:.2f}% | "
            f"{s['result_exact_rate']:.2f}% | {s['semantic_pass_rate']:.2f}% | "
            f"{s['avg_semantic_score']:.3f} | {s['avg_time_sec']:.2f}s |"
        )
    lines.extend(["", "## Single vs Multi", ""])
    lines.append("| System | Model | Type | Exec | Result exact | Semantic pass | Avg score |")
    lines.append("|---|---|---|---:|---:|---:|---:|")
    for item in out["summaries"]:
        for qtype, s in item["summary"].get("by_type", {}).items():
            lines.append(
                f"| {item['system']} | {item['model']} | {qtype} | {s['exec_rate']:.2f}% | "
                f"{s['result_exact_rate']:.2f}% | {s['semantic_pass_rate']:.2f}% | "
                f"{s['avg_semantic_score']:.3f} |"
            )
    lines.extend(["", "## Failure Types", ""])
    for item in out["summaries"]:
        errors = Counter()
        for r in item["runs"]:
            ev = r.get("semantic_evidence", {})
            if not ev.get("exec_ok"):
                err = ev.get("pred_error") or r.get("error") or "unknown"
                errors[err.split(":", 1)[0]] += 1
            elif not ev.get("result_exact"):
                errors["result_mismatch"] += 1
            elif ev.get("has_null_placeholder"):
                errors["null_placeholder"] += 1
        lines.append(f"### {item['system']} / {item['model']}")
        if errors:
            for k, v in errors.most_common():
                lines.append(f"- {k}: {v}")
        else:
            lines.append("- no failures")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--benchmark", default=DEFAULT_BENCHMARK)
    p.add_argument("--generate-benchmark", action="store_true")
    p.add_argument("--run", action="store_true")
    p.add_argument("--systems", nargs="+", default=["coordsql_with_final", "coordsql_no_final", "direct_prompt", "self_debug_style"])
    p.add_argument("--models", nargs="+", default=[PYTHON_MODEL])
    p.add_argument("--baseline-models", nargs="*", default=["qwen2.5-coder:7b", "sqlcoder:7b"])
    p.add_argument("--start-date", default="2021-01-01")
    p.add_argument("--end-date", default="2026-03-17")
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--sql-timeout", type=int, default=45)
    p.add_argument("--use-gold-tables", action="store_true")
    p.add_argument("--max-cases", type=int, default=0)
    p.add_argument("--output", default=DEFAULT_RESULTS)
    p.add_argument("--markdown-output", default=DEFAULT_REPORT)
    p.add_argument("--db-user", default=m.DEFAULT_DB_CONFIG.get("DB_USER", ""))
    p.add_argument("--db-password", default=m.DEFAULT_DB_CONFIG.get("DB_PASSWORD", ""))
    p.add_argument("--db-host", default=m.DEFAULT_DB_CONFIG.get("DB_HOST", "127.0.0.1"))
    p.add_argument("--db-port", type=int, default=int(m.DEFAULT_DB_CONFIG.get("DB_PORT", 1521)))
    p.add_argument("--db-service", default=m.DEFAULT_DB_CONFIG.get("DB_SERVICE_NAME", "oral"))
    return p.parse_args()


def main_cli() -> None:
    args = parse_args()
    if not args.db_user or not args.db_password:
        raise ValueError("database user/password required")
    db_cfg = _db_cfg(args)
    bench_path = Path(args.benchmark)
    with m.app.test_request_context("/"):
        session["db_config"] = db_cfg
        if args.generate_benchmark or not bench_path.exists():
            print(f"[build] generating benchmark -> {bench_path}")
            build_benchmark(bench_path, timeout=args.sql_timeout)
        benchmark = json.loads(bench_path.read_text(encoding="utf-8"))
        cases = benchmark["cases"]
        if args.max_cases > 0:
            cases = cases[: args.max_cases]
        if not args.run:
            print(f"[ok] benchmark ready: {bench_path} ({len(benchmark['cases'])} cases)")
            return

        run_specs: List[Tuple[str, str]] = []
        for system in args.systems:
            for model in args.models:
                run_specs.append((system, model))
        baseline_models = [
            model for model in args.baseline_models
            if model.lower() not in {"none", "null", "-"}
        ]
        for model in baseline_models:
            for system in ["direct_prompt", "self_debug_style"]:
                if (system, model) not in run_specs:
                    run_specs.append((system, model))

        summaries = []
        out_path = Path(args.output)
        report_path = Path(args.markdown_output)

        def build_output() -> Dict[str, Any]:
            return {
                "meta": {
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "benchmark_path": str(bench_path.resolve()),
                    "case_count": len(cases),
                    "single_count": sum(1 for c in cases if c["query_type"] == "single"),
                    "multi_count": sum(1 for c in cases if c["query_type"] == "multi"),
                    "use_gold_tables": args.use_gold_tables,
                    "systems": args.systems,
                    "models": args.models,
                    "baseline_models": baseline_models,
                    "completed_run_specs": [[s["system"], s["model"]] for s in summaries],
                    "requested_run_specs": [[system, model] for system, model in run_specs],
                },
                "benchmark_meta": benchmark.get("meta", {}),
                "summaries": summaries,
            }

        def save_outputs() -> None:
            out = build_output()
            out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
            write_report(out, report_path)

        for system, model in run_specs:
            print(f"\n[run] {system} / {model}")
            runs = []
            for i, case in enumerate(cases, 1):
                print(f"  {i:02d}/{len(cases)} {case['id']} {case['query_type']} ...", end=" ", flush=True)
                r = run_system(system, model, case, db_cfg, args.rounds, args.start_date, args.end_date, args.sql_timeout, args.use_gold_tables)
                runs.append(r)
                ev = r.get("semantic_evidence", {})
                status = "SEM" if ev.get("semantic_pass") else ("EXACT" if ev.get("result_exact") else ("EXEC" if ev.get("exec_ok") else "FAIL"))
                print(status, flush=True)
            summaries.append({
                "system": system,
                "model": model,
                "summary": summarize_runs(runs),
                "runs": runs,
            })
            save_outputs()

        save_outputs()
        print(f"\n[done] {out_path.resolve()}")
        print(f"[done] {report_path.resolve()}")


if __name__ == "__main__":
    main_cli()
