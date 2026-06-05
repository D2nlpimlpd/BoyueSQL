"""
Database-dictionary adapter for enterprise Text-to-SQL schema retrieval.

This module extends RAG-Anything's direct content-list workflow for database
catalogs whose source fields follow the workbook used in this project:

    表名称, 表类型, 表中文名, 列名称, 列中文名, 使用说明, 字段类型, 长度, 精度

The adapter intentionally emits plain text blocks with stable schema markers.
LightRAG can build a knowledge graph from those blocks, while downstream
Text-to-SQL code can parse the retrieved context back into tables, columns, and
join relationships without relying on fragile natural-language summaries.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


TABLE_MARKER_RE = re.compile(r"\[\[TABLE:([A-Z0-9_#$]+)\]\]")
COLUMN_MARKER_RE = re.compile(r"\[\[COLUMN:([A-Z0-9_#$]+)\.([A-Z0-9_#$]+)\]\]")
REL_MARKER_RE = re.compile(
    r"\[\[REL:([A-Z0-9_#$]+)\.([A-Z0-9_#$]+)=([A-Z0-9_#$]+)\.([A-Z0-9_#$]+)\]\]"
)


EXCEL_COLUMN_MAP = {
    "table_name": "表名称",
    "table_type": "表类型",
    "table_cn": "表中文名",
    "column_name": "列名称",
    "column_cn": "列中文名",
    "usage": "使用说明",
    "data_type": "字段类型",
    "length": "长度",
    "precision": "精度",
}


def _clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value).strip()
    if text.lower() == "nan":
        return ""
    return text


def _upper(value: Any) -> str:
    return _clean(value).upper()


def _is_code_table(table_name: str, table_cn: str = "") -> bool:
    name = table_name.upper()
    cn = table_cn or ""
    return (
        name.startswith("BM_")
        or "_BM_" in name
        or name.startswith("AA_BM_")
        or name.startswith("ZD_")
        or "CODE" in name
        or "字典" in cn
        or "编码" in cn
    )


def _column_iter(table_info: Dict[str, Any]) -> List[Dict[str, Any]]:
    columns = table_info.get("columns", {})
    if isinstance(columns, dict):
        return [c for c in columns.values() if isinstance(c, dict)]
    if isinstance(columns, list):
        return [c for c in columns if isinstance(c, dict)]
    return []


def load_database_dictionary_json(path: str | Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return normalize_database_dictionary(data)


def load_database_dictionary_excel(path: str | Path) -> Dict[str, Any]:
    """Load the project workbook schema into the normalized JSON structure."""

    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise RuntimeError(
            "openpyxl is required to read 数据库字典结构.xlsx. "
            "Install it in the text2sql environment or use data_dictionary.json."
        ) from exc

    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]

    header = [_clean(cell.value) for cell in next(ws.iter_rows(min_row=1, max_row=1))]
    index = {name: i for i, name in enumerate(header)}
    required = list(EXCEL_COLUMN_MAP.values())
    missing = [col for col in required[:6] if col not in index]
    if missing:
        raise ValueError(f"Workbook is missing required columns: {missing}")

    def row_value(row_values: Sequence[Any], header_name: str) -> Any:
        pos = index.get(header_name)
        if pos is None or pos >= len(row_values):
            return ""
        return row_values[pos]

    tables: Dict[str, Dict[str, Any]] = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        table_name = _upper(row[index[EXCEL_COLUMN_MAP["table_name"]]])
        column_name = _upper(row[index[EXCEL_COLUMN_MAP["column_name"]]])
        if not table_name or not column_name:
            continue

        table_cn = _clean(row[index[EXCEL_COLUMN_MAP["table_cn"]]])
        table_type = _clean(row[index[EXCEL_COLUMN_MAP["table_type"]]])
        table = tables.setdefault(
            table_name,
            {
                "table_name": table_name,
                "table_cn": table_cn,
                "table_type": table_type,
                "is_code_table": _is_code_table(table_name, table_cn),
                "columns": [],
                "short_description": f"{table_name} {table_cn}".strip(),
                "detail_description": "",
            },
        )
        if table_cn and not table.get("table_cn"):
            table["table_cn"] = table_cn
        if table_type and not table.get("table_type"):
            table["table_type"] = table_type

        data_type = _clean(row_value(row, EXCEL_COLUMN_MAP["data_type"]))
        length = _clean(row_value(row, EXCEL_COLUMN_MAP["length"]))
        precision = _clean(row_value(row, EXCEL_COLUMN_MAP["precision"]))
        type_parts = [data_type]
        if length:
            type_parts.append(f"len={length}")
        if precision:
            type_parts.append(f"precision={precision}")

        table["columns"].append(
            {
                "name": column_name,
                "cn": _clean(row[index[EXCEL_COLUMN_MAP["column_cn"]]]),
                "usage": _clean(row[index[EXCEL_COLUMN_MAP["usage"]]]),
                "data_type": data_type,
                "type_str": " ".join(part for part in type_parts if part),
                "length": length,
                "precision": precision,
                "full_description": _clean(row[index[EXCEL_COLUMN_MAP["usage"]]]),
            }
        )

    main_tables = {}
    code_tables = {}
    for table_name, table in tables.items():
        target = code_tables if table.get("is_code_table") else main_tables
        target[table_name] = table

    return {
        "main_tables": main_tables,
        "code_tables": code_tables,
        "metadata": {
            "main_table_count": len(main_tables),
            "code_table_count": len(code_tables),
            "total_count": len(main_tables) + len(code_tables),
            "source": str(path),
        },
    }


def normalize_database_dictionary(data: Dict[str, Any]) -> Dict[str, Any]:
    main_tables: Dict[str, Any] = {}
    code_tables: Dict[str, Any] = {}

    for source_key, target in (("main_tables", main_tables), ("code_tables", code_tables)):
        for table_name, table_info in (data.get(source_key, {}) or {}).items():
            if not isinstance(table_info, dict):
                continue
            normalized_name = _upper(table_info.get("table_name") or table_name)
            if not normalized_name:
                continue
            table_cn = _clean(table_info.get("table_cn"))
            is_code = bool(table_info.get("is_code_table", source_key == "code_tables"))
            columns = []
            for col in _column_iter(table_info):
                col_name = _upper(col.get("name") or col.get("column_name"))
                if not col_name:
                    continue
                columns.append(
                    {
                        **col,
                        "name": col_name,
                        "cn": _clean(col.get("cn") or col.get("column_cn")),
                        "usage": _clean(col.get("usage") or col.get("full_description")),
                        "data_type": _clean(col.get("data_type") or col.get("type")),
                        "type_str": _clean(col.get("type_str") or col.get("data_type")),
                        "full_description": _clean(
                            col.get("full_description") or col.get("usage")
                        ),
                    }
                )
            table = {
                **table_info,
                "table_name": normalized_name,
                "table_cn": table_cn,
                "is_code_table": is_code,
                "columns": columns,
                "short_description": _clean(table_info.get("short_description"))
                or f"{normalized_name} {table_cn}".strip(),
            }
            (code_tables if is_code else main_tables)[normalized_name] = table

    return {
        "main_tables": main_tables,
        "code_tables": code_tables,
        "metadata": {
            **(data.get("metadata") or {}),
            "main_table_count": len(main_tables),
            "code_table_count": len(code_tables),
            "total_count": len(main_tables) + len(code_tables),
        },
    }


def all_tables(data: Dict[str, Any]) -> Dict[str, Any]:
    normalized = normalize_database_dictionary(data)
    merged = {}
    merged.update(normalized.get("main_tables", {}))
    merged.update(normalized.get("code_tables", {}))
    return merged


def dictionary_signature(data: Dict[str, Any]) -> str:
    normalized = normalize_database_dictionary(data)
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def is_join_key(column: str) -> bool:
    col = column.upper()
    if any(token in col for token in ("DATE", "TIME", "STATUS_NAME", "DESC")):
        return False
    return (
        col in {"ID", "CODE", "BM", "EXAM_NO", "PERSON_ID", "PATIENT_ID"}
        or col.endswith("_ID")
        or col.endswith("_NO")
        or col.endswith("_CODE")
        or col.endswith("_BM")
    )


def build_code_mappings(data: Dict[str, Any]) -> List[Dict[str, str]]:
    normalized = normalize_database_dictionary(data)
    main_tables = normalized.get("main_tables", {})
    code_tables = normalized.get("code_tables", {})
    mappings: List[Dict[str, str]] = []

    def name_col(cols: Sequence[Dict[str, Any]], code_col: str) -> Optional[str]:
        names = {c["name"].upper(): c["name"].upper() for c in cols if c.get("name")}
        base = re.sub(r"(_CODE|_BM|_ID|_NO)$", "", code_col, flags=re.I)
        candidates = [
            f"{base}_NAME",
            f"{base}_DESC",
            f"{base}NAME",
            f"{base}_CN",
            f"{base}MC",
            "NAME",
            "DESC",
            "DESCRIPTION",
            "ITEM_NAME",
            "DEPT_NAME",
        ]
        for cand in candidates:
            if cand.upper() in names:
                return cand.upper()
        for col in names:
            if col != code_col and any(x in col for x in ("NAME", "DESC", "MC", "_CN")):
                return col
        return None

    main_col_index: Dict[str, Set[str]] = {}
    for table_name, table in main_tables.items():
        main_col_index[table_name] = {c["name"].upper() for c in _column_iter(table)}

    seen: Set[Tuple[str, str, str, str]] = set()
    for code_table, table in code_tables.items():
        cols = _column_iter(table)
        code_cols = [c["name"].upper() for c in cols if c.get("name") and is_join_key(c["name"])]
        for code_col in code_cols:
            label_col = name_col(cols, code_col)
            if not label_col:
                continue
            for main_table, main_cols in main_col_index.items():
                if code_col not in main_cols:
                    continue
                key = (main_table, code_col, code_table, label_col)
                if key in seen:
                    continue
                seen.add(key)
                mappings.append(
                    {
                        "main_table": main_table,
                        "main_column": code_col,
                        "code_table": code_table,
                        "code_column": code_col,
                        "name_column": label_col,
                    }
                )
    return mappings


def build_relationships(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    tables = all_tables(data)
    table_cols: Dict[str, Set[str]] = {}
    for table_name, table in tables.items():
        cols = {c["name"].upper() for c in _column_iter(table) if c.get("name")}
        if cols:
            table_cols[table_name] = cols

    relationships: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, str, str]] = set()
    names = sorted(table_cols)
    for i, left in enumerate(names):
        for right in names[i + 1 :]:
            common = sorted(c for c in table_cols[left] & table_cols[right] if is_join_key(c))
            for col in common[:4]:
                key = (left, right, col)
                if key in seen:
                    continue
                seen.add(key)
                relationships.append(
                    {
                        "type": "relation",
                        "left": left,
                        "right": right,
                        "column": col,
                        "text": f"JOIN {left}.{col} = {right}.{col}",
                    }
                )

    for mapping in build_code_mappings(data):
        left = mapping["main_table"]
        right = mapping["code_table"]
        col = mapping["main_column"]
        key = tuple(sorted([left, right]) + [col])
        if key in seen:
            continue
        seen.add(key)
        relationships.append(
            {
                "type": "relation",
                "left": left,
                "right": right,
                "column": col,
                "right_column": mapping["code_column"],
                "name_column": mapping["name_column"],
                "text": (
                    f"JOIN {left}.{col} = {right}.{mapping['code_column']} "
                    f"for Chinese label {right}.{mapping['name_column']}"
                ),
            }
        )
    return relationships


def build_schema_entries(data: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    normalized = normalize_database_dictionary(data)
    tables = all_tables(normalized)

    table_entries: List[Dict[str, Any]] = []
    column_entries: List[Dict[str, Any]] = []
    for table_name, table in tables.items():
        table_cn = _clean(table.get("table_cn"))
        description = _clean(table.get("short_description"))
        table_type = "code_table" if table.get("is_code_table") else "business_table"
        col_names = [c["name"] for c in _column_iter(table) if c.get("name")]
        table_entries.append(
            {
                "type": "table",
                "table": table_name,
                "table_name": table_name,
                "table_cn": table_cn,
                "is_code_table": bool(table.get("is_code_table")),
                "text": (
                    f"{table_name} {table_cn} {description} "
                    f"{table_type} columns: {', '.join(col_names[:25])}"
                ).strip(),
            }
        )
        for col in _column_iter(table):
            col_name = col.get("name", "").upper()
            if not col_name:
                continue
            column_entries.append(
                {
                    "type": "column",
                    "table": table_name,
                    "column": col_name,
                    "column_cn": _clean(col.get("cn")),
                    "data_type": _clean(col.get("type_str") or col.get("data_type")),
                    "text": (
                        f"{table_name}.{col_name} {col.get('cn', '')} "
                        f"{col.get('usage', '')} {col.get('full_description', '')} "
                        f"{col.get('type_str', '') or col.get('data_type', '')}"
                    ).strip(),
                }
            )

    return {
        "tables": table_entries,
        "columns": column_entries,
        "relationships": build_relationships(normalized),
    }


def build_content_list(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Create RAG-Anything direct insertion content blocks for the schema KG."""

    normalized = normalize_database_dictionary(data)
    entries = build_schema_entries(normalized)
    meta = normalized.get("metadata", {})
    content: List[Dict[str, Any]] = [
        {
            "type": "text",
            "page_idx": 0,
            "text": (
                "[[DATABASE_DICTIONARY]]\n"
                f"Enterprise Oracle schema dictionary. "
                f"Tables={meta.get('total_count', len(entries['tables']))}, "
                f"business_tables={meta.get('main_table_count', 0)}, "
                f"code_tables={meta.get('code_table_count', 0)}. "
                "Use exact table and column names. Prefer explicit join edges. "
                "Code tables map coded fields to Chinese display labels."
            ),
        }
    ]

    page_idx = 1
    for table in entries["tables"]:
        kind = "CODE" if table.get("is_code_table") else "BUSINESS"
        text = (
            f"[[TABLE:{table['table']}]]\n"
            f"Table: {table['table']}\n"
            f"Chinese name: {table.get('table_cn', '')}\n"
            f"Kind: {kind}\n"
            f"Description: {table.get('text', '')}\n"
            "Text-to-SQL rule: this table can be used only with listed columns."
        )
        content.append({"type": "text", "text": text, "page_idx": page_idx})
        page_idx += 1

    by_table: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for col in entries["columns"]:
        by_table[col["table"]].append(col)

    for table_name, cols in by_table.items():
        lines = [f"[[TABLE:{table_name}]] column catalog"]
        for col in cols:
            lines.append(
                f"[[COLUMN:{table_name}.{col['column']}]] "
                f"{table_name}.{col['column']} | cn={col.get('column_cn', '')} | "
                f"type={col.get('data_type', '')} | usage={col.get('text', '')}"
            )
        content.append({"type": "text", "text": "\n".join(lines), "page_idx": page_idx})
        page_idx += 1

    rel_lines = ["[[RELATION_CATALOG]] Known schema join edges."]
    for rel in entries["relationships"]:
        right_col = rel.get("right_column") or rel["column"]
        rel_lines.append(
            f"[[REL:{rel['left']}.{rel['column']}={rel['right']}.{right_col}]] "
            f"{rel.get('text', '')}"
        )
    content.append({"type": "text", "text": "\n".join(rel_lines), "page_idx": page_idx})
    return content


def build_custom_kg(data: Dict[str, Any], file_path: str = "database_dictionary") -> Dict[str, Any]:
    """Build a deterministic LightRAG custom KG for schema retrieval.

    Entities use stable names that are easy to recover from retrieved context:
    - TABLE::<table>
    - COLUMN::<table>.<column>

    Relationships explicitly encode containment, joinability, and code-table
    label mappings. This removes the uncertainty of LLM-based entity extraction
    for database catalogs.
    """

    normalized = normalize_database_dictionary(data)
    entries = build_schema_entries(normalized)
    max_join_edges = int(os.environ.get("RAGANYTHING_MAX_JOIN_EDGES", "800"))
    include_column_entities = os.environ.get(
        "RAGANYTHING_INCLUDE_COLUMN_ENTITIES", "0"
    ).lower() in {"1", "true", "yes"}
    chunks: List[Dict[str, Any]] = []
    entities: List[Dict[str, Any]] = []
    relationships: List[Dict[str, Any]] = []

    by_table: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for col in entries["columns"]:
        by_table[col["table"]].append(col)

    for table in entries["tables"]:
        table_name = table["table"]
        source_id = f"table:{table_name}"
        col_lines = []
        for col in by_table.get(table_name, []):
            col_lines.append(
                f"[[COLUMN:{table_name}.{col['column']}]] {col['column']} "
                f"{col.get('column_cn', '')} {col.get('data_type', '')} {col.get('text', '')}"
            )
        chunk_content = (
            f"[[TABLE:{table_name}]]\n"
            f"Table {table_name} Chinese name {table.get('table_cn', '')}. "
            f"Kind {'code table' if table.get('is_code_table') else 'business table'}.\n"
            + "\n".join(col_lines)
        )
        chunks.append(
            {
                "content": chunk_content,
                "source_id": source_id,
                "file_path": file_path,
                "chunk_order_index": len(chunks),
            }
        )
        entities.append(
            {
                "entity_name": f"TABLE::{table_name}",
                "entity_type": "CODE_TABLE" if table.get("is_code_table") else "BUSINESS_TABLE",
                "description": table.get("text", ""),
                "source_id": source_id,
                "file_path": file_path,
            }
        )
        if include_column_entities:
            for col in by_table.get(table_name, []):
                column_entity = f"COLUMN::{table_name}.{col['column']}"
                entities.append(
                    {
                        "entity_name": column_entity,
                        "entity_type": "COLUMN",
                        "description": (
                            f"[[COLUMN:{table_name}.{col['column']}]] "
                            f"{table_name}.{col['column']} {col.get('column_cn', '')} "
                            f"{col.get('data_type', '')} {col.get('text', '')}"
                        ),
                        "source_id": source_id,
                        "file_path": file_path,
                    }
                )
                relationships.append(
                    {
                        "src_id": f"TABLE::{table_name}",
                        "tgt_id": column_entity,
                        "description": f"TABLE {table_name} has column {col['column']}",
                        "keywords": "has_column schema column catalog",
                        "weight": 1.0,
                        "source_id": source_id,
                        "file_path": file_path,
                    }
                )

    rel_source_id = "relations:join_edges"
    rel_lines = ["[[RELATION_CATALOG]]"]
    join_entries = sorted(
        entries["relationships"],
        key=lambda rel: (
            0 if rel.get("name_column") else 1,
            rel.get("left", ""),
            rel.get("right", ""),
            rel.get("column", ""),
        ),
    )
    if max_join_edges > 0:
        join_entries = join_entries[:max_join_edges]
    for rel in join_entries:
        left_col = rel["column"]
        right_col = rel.get("right_column") or left_col
        marker = f"[[REL:{rel['left']}.{left_col}={rel['right']}.{right_col}]]"
        rel_lines.append(f"{marker} {rel.get('text', '')}")
        relationships.append(
            {
                "src_id": f"TABLE::{rel['left']}",
                "tgt_id": f"TABLE::{rel['right']}",
                "description": f"{marker} {rel.get('text', '')}",
                "keywords": f"join relation foreign key {left_col} {right_col}",
                "weight": 2.0,
                "source_id": rel_source_id,
                "file_path": file_path,
            }
        )
        if include_column_entities:
            relationships.append(
                {
                    "src_id": f"COLUMN::{rel['left']}.{left_col}",
                    "tgt_id": f"COLUMN::{rel['right']}.{right_col}",
                    "description": f"{marker} equivalent join columns",
                    "keywords": "join columns equality",
                    "weight": 2.0,
                    "source_id": rel_source_id,
                    "file_path": file_path,
                }
            )

    chunks.append(
        {
            "content": "\n".join(rel_lines),
            "source_id": rel_source_id,
            "file_path": file_path,
            "chunk_order_index": len(chunks),
        }
    )

    return {"chunks": chunks, "entities": entities, "relationships": relationships}


def parse_schema_markers(context: str) -> Dict[str, Set[Any]]:
    tables = set(TABLE_MARKER_RE.findall(context or ""))
    columns = {(m.group(1), m.group(2)) for m in COLUMN_MARKER_RE.finditer(context or "")}
    relations = {
        (m.group(1), m.group(2), m.group(3), m.group(4))
        for m in REL_MARKER_RE.finditer(context or "")
    }
    for table, _column in columns:
        tables.add(table)
    for left, _left_col, right, _right_col in relations:
        tables.add(left)
        tables.add(right)
    return {"tables": tables, "columns": columns, "relationships": relations}


def _query_terms(question: str) -> Set[str]:
    query = (question or "").lower()
    terms: Set[str] = set()
    for token in re.findall(r"[0-9a-zA-Z_#$]+", query):
        if token:
            terms.add(token)

    for segment in re.findall(r"[\u4e00-\u9fff]+", query):
        if not segment:
            continue
        terms.add(segment)
        max_n = min(8, len(segment))
        for n in range(2, max_n + 1):
            for i in range(0, len(segment) - n + 1):
                terms.add(segment[i : i + n])
    return terms


def _entry_match_score(question: str, entry: Dict[str, Any], key_fields: Sequence[str]) -> float:
    query = (question or "").lower()
    terms = _query_terms(question)
    haystack_parts = [_clean(entry.get(field)) for field in key_fields]
    haystack = " ".join(haystack_parts).lower()
    if not haystack:
        return 0.0

    score = 0.0
    table_name = _clean(entry.get("table") or entry.get("table_name")).lower()
    column_name = _clean(entry.get("column")).lower()
    if table_name and table_name in query:
        score += 8.0
    if column_name and column_name in query:
        score += 6.0

    for field in key_fields:
        value = _clean(entry.get(field)).lower()
        if len(value) >= 2 and value in query:
            score += min(8.0, 1.0 + len(value) / 2.0)

    for term in terms:
        if len(term) < 2:
            continue
        if term in haystack:
            score += min(2.0, 0.2 + len(term) / 8.0)
    return score


def keyword_rank_entries(
    question: str,
    entries: Sequence[Dict[str, Any]],
    key_fields: Sequence[str],
    limit: int,
) -> List[Tuple[float, Dict[str, Any]]]:
    query = (question or "").lower()
    value_freq: Dict[Tuple[str, str], int] = defaultdict(int)
    for entry in entries:
        for field in ("column_cn", "column"):
            value = _clean(entry.get(field)).lower()
            if len(value) >= 2:
                value_freq[(field, value)] += 1

    ranked: List[Tuple[float, Dict[str, Any]]] = []
    for entry in entries:
        score = _entry_match_score(question, entry, key_fields)
        for field in ("column_cn", "column"):
            value = _clean(entry.get(field)).lower()
            freq = value_freq.get((field, value), 0)
            if freq > 1 and value and value in query:
                score /= math.sqrt(freq)
        if score > 0:
            ranked.append((score, dict(entry)))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[:limit]


def query_keywords(data: Dict[str, Any], question: str, limit: int = 12) -> Dict[str, List[str]]:
    entries = build_schema_entries(data)
    hl: List[str] = []
    ll: List[str] = []

    for score, entry in keyword_rank_entries(
        question, entries["tables"], ["table", "table_cn", "text"], limit
    ):
        table = entry.get("table")
        if table:
            hl.append(str(table))
        table_cn = _clean(entry.get("table_cn"))
        if table_cn:
            hl.append(table_cn)

    for score, entry in keyword_rank_entries(
        question,
        entries["columns"],
        ["table", "column", "column_cn", "data_type", "text"],
        limit,
    ):
        if entry.get("table") and entry.get("column"):
            ll.append(f"{entry['table']}.{entry['column']}")
        column_cn = _clean(entry.get("column_cn"))
        if column_cn:
            ll.append(column_cn)

    def dedupe(values: List[str]) -> List[str]:
        out: List[str] = []
        seen: Set[str] = set()
        for value in values:
            cleaned = _clean(value)
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            out.append(cleaned)
        return out[:limit]

    return {"high_level": dedupe(hl), "low_level": dedupe([question] + ll)}


def hits_from_context(
    context: str,
    data: Dict[str, Any],
    question: str = "",
    topk_table: int = 6,
    topk_col: int = 12,
    topk_rel: int = 10,
) -> Tuple[List[Tuple[float, Dict[str, Any]]], List[Tuple[float, Dict[str, Any]]], List[Tuple[float, Dict[str, Any]]]]:
    entries = build_schema_entries(data)
    markers = parse_schema_markers(context or "")

    table_scores: Dict[str, Tuple[float, Dict[str, Any]]] = {}
    table_by_name = {entry["table"]: entry for entry in entries["tables"]}
    for table in sorted(markers["tables"]):
        if table in table_by_name:
            table_scores[table] = (1.0, dict(table_by_name[table]))

    for score, entry in keyword_rank_entries(
        question, entries["tables"], ["table", "table_cn", "text"], topk_table * 4
    ):
        table = entry["table"]
        prev_score, _prev_entry = table_scores.get(table, (0.0, entry))
        table_scores[table] = (prev_score + score, entry)

    table_hits: List[Tuple[float, Dict[str, Any]]] = sorted(
        table_scores.values(), key=lambda item: item[0], reverse=True
    )

    column_scores: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}
    col_by_key = {
        (entry["table"], entry["column"]): entry for entry in entries["columns"]
    }
    for key in sorted(markers["columns"]):
        if key in col_by_key:
            column_scores[key] = (1.0, dict(col_by_key[key]))

    for score, entry in keyword_rank_entries(
        question,
        entries["columns"],
        ["table", "column", "column_cn", "data_type", "text"],
        topk_col * 4,
    ):
        key = (entry["table"], entry["column"])
        prev_score, _prev_entry = column_scores.get(key, (0.0, entry))
        column_scores[key] = (prev_score + score, entry)

    column_hits: List[Tuple[float, Dict[str, Any]]] = sorted(
        column_scores.values(), key=lambda item: item[0], reverse=True
    )

    candidate_tables = {hit[1]["table"] for hit in table_hits[:topk_table]}
    candidate_tables.update(hit[1]["table"] for hit in column_hits[:topk_col])

    rel_scores: Dict[Tuple[str, str, str, str], Tuple[float, Dict[str, Any]]] = {}

    def add_relation(score: float, rel: Dict[str, Any]) -> None:
        left = _clean(rel.get("left")).upper()
        right = _clean(rel.get("right")).upper()
        left_col = _clean(rel.get("column")).upper()
        right_col = _clean(rel.get("right_column") or rel.get("column")).upper()
        if not left or not right or not left_col or not right_col:
            return
        if candidate_tables and left not in candidate_tables and right not in candidate_tables:
            return
        rel_entry = dict(rel)
        rel_entry.update(
            {
                "left": left,
                "right": right,
                "column": left_col,
                "right_column": right_col,
                "text": rel.get("text") or f"JOIN {left}.{left_col} = {right}.{right_col}",
            }
        )
        lexical = _entry_match_score(
            question,
            rel_entry,
            ["left", "right", "column", "right_column", "text"],
        )
        if left in candidate_tables:
            lexical += 0.35
        if right in candidate_tables:
            lexical += 0.35
        key = (left, left_col, right, right_col)
        total = float(score) + lexical
        prev = rel_scores.get(key)
        if prev is None or total > prev[0]:
            rel_scores[key] = (total, rel_entry)

    for left, left_col, right, right_col in sorted(markers["relationships"]):
        add_relation(
            1.0,
            {
                "type": "relation",
                "left": left,
                "right": right,
                "column": left_col,
                "right_column": right_col,
                "text": f"JOIN {left}.{left_col} = {right}.{right_col}",
            },
        )

    for rel in entries["relationships"]:
        add_relation(0.25, dict(rel))

    rel_hits = sorted(rel_scores.values(), key=lambda item: item[0], reverse=True)
    return table_hits[:topk_table], column_hits[:topk_col], rel_hits[:topk_rel]
