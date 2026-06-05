"""
app_6.py — NL2SQL 智能体（全新版 - 优化迭代纠错+多表查询）
框架: Retrieve(多粒度RAG) → Plan → Generate → Guard → Repair
编码表替换: 主表字段 CODE → 自动 JOIN 编码表取 NAME（来自 app_3 核心逻辑）
"""

# ============================================================
# 依赖导入（与你原环境完全一致，一个都不少）
# ============================================================
from flask import Flask, render_template, request, jsonify, session
import oracledb
import json
from pathlib import Path
from decimal import Decimal
import requests
import re
import hashlib
import faiss
import numpy as np
from sentence_transformers import SentenceTransformer
from datetime import datetime, date
import traceback
import os
import sys
import time
from typing import Dict, List, Tuple, Optional, Any, Set
from collections import defaultdict
from raganything_schema_retriever import RAGAnythingSchemaRetriever


def _configure_console_encoding() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_configure_console_encoding()

# ============================================================
# ① 基本配置（与你原来的基础环境配置完全一致，必须保留）
# ============================================================
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only-change-me")

# Ollama
OLLAMA_BASE_URL        = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_EMBEDDING_MODEL = os.environ.get("OLLAMA_EMBEDDING_MODEL", "qwen3-vl:8b")
OLLAMA_SQL_MODEL       = os.environ.get("OLLAMA_SQL_MODEL", "qwen3-vl:8b")
STRICT_QWEN3_VL_ONLY   = os.environ.get("STRICT_QWEN3_VL_ONLY", "1").lower() not in {"0", "false", "no"}
DETERMINISTIC_EMBEDDING_DIM = int(os.environ.get("DETERMINISTIC_EMBEDDING_DIM", "1024"))

# Oracle client
ORACLE_CLIENT_DIR = os.environ.get("ORACLE_CLIENT_DIR", r"F:\oracle\instantclient_11_2")
DEFAULT_DB_CONFIG = {
    "DB_USER":         os.environ.get("DB_USER",         "bjzx"),
    "DB_PASSWORD":     os.environ.get("DB_PASSWORD",     ""),
    "DB_HOST":         os.environ.get("DB_HOST",         "127.0.0.1"),
    "DB_PORT":         int(os.environ.get("DB_PORT",     "1521")),
    "DB_SERVICE_NAME": os.environ.get("DB_SERVICE_NAME", "oral"),
}
oracledb.init_oracle_client(lib_dir=ORACLE_CLIENT_DIR)

# 系统参数
_DEFAULT_XLSX_DICT_PATH = Path("数据库字典结构.xlsx")
DATA_DICT_PATH = Path(os.environ.get(
    "DATA_DICT_PATH",
    str(_DEFAULT_XLSX_DICT_PATH if _DEFAULT_XLSX_DICT_PATH.exists() else "data_dictionary.json"),
))
MAX_ROWS       = int(os.environ.get("MAX_ROWS", "500"))
RAG_CACHE_DIR  = Path(os.environ.get("RAG_CACHE_DIR", "./rag_cache"))
RAG_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# SentenceTransformer 降级备用
LOCAL_EMBEDDING_MODEL_NAME = os.environ.get("LOCAL_EMBEDDING_MODEL_NAME", "all-MiniLM-L6-v2")
_local_st_model: Optional[SentenceTransformer] = None


# ============================================================
# ② JSON 序列化辅助
# ============================================================
def _json_default(obj):
    if isinstance(obj, Decimal):      return float(obj)
    if isinstance(obj, (datetime, date)): return obj.isoformat()
    return str(obj)


# ============================================================
# ③ Ollama 调用（embedding + chat）- GPU加速优化版
# ============================================================
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

# ★ 全局信号量：限制同时只有1个 LLM（SQL生成）请求，防止 Ollama 过载超时
_llm_semaphore = threading.Semaphore(1)

# 全局embedding缓存（减少重复计算）
_embedding_cache: Dict[str, np.ndarray] = {}
_cache_lock = threading.Lock()


def deterministic_schema_embed(texts: List[str], dim: int = DETERMINISTIC_EMBEDDING_DIM) -> np.ndarray:
    """Non-model lexical hashing embedding used when qwen3-vl-only mode is required."""
    rows = []
    token_re = re.compile(r"[A-Za-z0-9_#$]+|[\u4e00-\u9fff]+")
    for text in texts:
        vec = np.zeros(dim, dtype=np.float32)
        tokens = token_re.findall((text or "").upper())
        if not tokens:
            rows.append(vec)
            continue
        for token in tokens:
            grams = [token]
            if re.fullmatch(r"[\u4e00-\u9fff]+", token):
                max_n = min(6, len(token))
                for n in range(2, max_n + 1):
                    grams.extend(token[i:i + n] for i in range(len(token) - n + 1))
            elif len(token) > 3:
                grams.extend(token[i:i + 3] for i in range(len(token) - 2))
            for gram in grams:
                digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
                bucket = int.from_bytes(digest[:4], "little") % dim
                sign = 1.0 if (digest[4] & 1) == 0 else -1.0
                vec[bucket] += sign
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec /= norm
        rows.append(vec)
    return np.vstack(rows).astype(np.float32) if rows else np.zeros((0, dim), dtype=np.float32)


def ollama_embed(texts: List[str], batch_size: int = 100, show_progress: bool = False, use_cache: bool = True) -> np.ndarray:
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)

    if STRICT_QWEN3_VL_ONLY and OLLAMA_EMBEDDING_MODEL == "qwen3-vl:8b":
        return deterministic_schema_embed(texts)

    vecs = []
    use_fallback = False
    texts_to_process = []

    for i, t in enumerate(texts):
        t = (t or "").strip()
        if not t:
            vecs.append((i, None))
            continue
        if use_cache:
            with _cache_lock:
                if t in _embedding_cache:
                    vecs.append((i, _embedding_cache[t]))
                    continue
        texts_to_process.append((i, t))

    if not texts_to_process:
        vecs.sort(key=lambda x: x[0])
        valid = [v for _, v in vecs if v is not None]
        if not valid:
            return np.zeros((0, 0), dtype=np.float32)
        return np.vstack(valid).astype(np.float32)

    def embed_single(idx_text):
        idx, text = idx_text
        try:
            resp = requests.post(
                f"{OLLAMA_BASE_URL}/api/embeddings",
                json={"model": OLLAMA_EMBEDDING_MODEL, "prompt": text},
                timeout=30
            )
            if resp.status_code != 200:
                raise RuntimeError(f"http {resp.status_code}")
            emb = resp.json().get("embedding")
            if not emb:
                raise RuntimeError("empty embedding")
            vec = np.array(emb, dtype=np.float32)
            if use_cache:
                with _cache_lock:
                    _embedding_cache[text] = vec
            return (idx, vec)
        except Exception as e:
            if STRICT_QWEN3_VL_ONLY:
                return (idx, deterministic_schema_embed([text])[0])
            return (idx, None, str(e))

    max_workers = min(32, len(texts_to_process))

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(embed_single, item): item for item in texts_to_process}

        for i, future in enumerate(as_completed(futures)):
            result = future.result()

            if len(result) == 3:
                idx, _, error = result
                if STRICT_QWEN3_VL_ONLY:
                    _, text = futures[future]
                    vecs.append((idx, deterministic_schema_embed([text])[0]))
                    continue
                if not use_fallback:
                    print(f"  [Embed warn] Ollama 失败: {error}，切换到 SentenceTransformer")
                    use_fallback = True
                    global _local_st_model
                    if _local_st_model is None:
                        print(f"  正在加载本地模型 {LOCAL_EMBEDDING_MODEL_NAME}...")
                        os.environ["TRANSFORMERS_OFFLINE"] = "1"
                        os.environ["HF_HUB_OFFLINE"] = "1"
                        _local_st_model = SentenceTransformer(LOCAL_EMBEDDING_MODEL_NAME)

                _, text = futures[future]
                e_vec = _local_st_model.encode([text], normalize_embeddings=False)[0]
                vec = np.array(e_vec, dtype=np.float32)

                if use_cache:
                    with _cache_lock:
                        _embedding_cache[text] = vec

                vecs.append((idx, vec))
            else:
                idx, vec = result
                vecs.append((idx, vec))

            if show_progress and (i + 1) % 100 == 0:
                print(f"      进度: {i + 1}/{len(texts_to_process)}")

    if use_fallback and _local_st_model is not None:
        remaining = [(idx, text) for idx, text in texts_to_process
                     if not any(idx == v[0] for v in vecs)]
        if remaining:
            texts_batch = [text for _, text in remaining]
            embeddings = _local_st_model.encode(texts_batch, normalize_embeddings=False, show_progress_bar=False)
            for (idx, text), emb in zip(remaining, embeddings):
                vec = np.array(emb, dtype=np.float32)
                if use_cache:
                    with _cache_lock:
                        _embedding_cache[text] = vec
                vecs.append((idx, vec))

    vecs.sort(key=lambda x: x[0])
    valid = [v for _, v in vecs if v is not None]

    if not valid:
        return np.zeros((0, 0), dtype=np.float32)

    mat = np.vstack(valid).astype(np.float32)
    return mat


def ollama_chat(prompt: str,
                model: str = None,
                temperature: float = 0.0,
                max_tokens: int = 2048,
                timeout: int = 120,
                num_ctx: int = 8192) -> str:          # ★ 新增 num_ctx 参数
    model = model or OLLAMA_SQL_MODEL
    if model == "qwen3-vl:8b":
        if "/no_think" not in prompt[:200]:
            prompt = (
                "/no_think\n"
                "Do not output <think> or hidden reasoning. Output only the final answer requested.\n"
                + prompt
            )
        max_tokens = max(
            int(max_tokens),
            int(os.environ.get("QWEN3_VL_MIN_NUM_PREDICT", "4096")),
        )
    url   = f"{OLLAMA_BASE_URL}/api/generate"
    payload = {
        "model":  model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
            "num_ctx":     num_ctx,
        }
    }
    if model == "qwen3-vl:8b":
        payload["think"] = False
    try:
        with _llm_semaphore:  # ★ 串行化：同时只允许1个 LLM 请求，防止 Ollama 过载
            resp = requests.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        text = data.get("response", "").strip()
        if not text and model == "qwen3-vl:8b":
            text = data.get("thinking", "").strip()
        return text
    except requests.exceptions.ReadTimeout:
        print(f"  ⚠️ LLM 调用超时（>{timeout}s），返回空字符串")
        return ""
    except Exception as e:
        print(f"  ❌ LLM 调用失败: {e}")
        return ""


def _quote_chinese_aliases(sql: str) -> str:
    """
    Oracle 不允许裸中文列别名，例如 AS 中国籍受检总人数 会报 ORA-00923。
    将所有未加引号的非 ASCII 别名用双引号包裹：
      AS 中文名  →  AS "中文名"
      AS 中文名,  →  AS "中文名",
    同时修复列别名中含 / 的情况（如 中国籍异常/阳性人数）。
    """
    # 匹配 AS 后面跟着非 ASCII 字符组成的别名（未被引号包裹）
    # 别名可能以逗号、空格、FROM、WHERE、GROUP 等终止
    def replacer(m):
        alias = m.group(1).strip()
        return f'AS "{alias}"'
    sql = re.sub(
        r'\bAS\s+([^\x00-\x7F][^,\s)]*)',
        replacer,
        sql,
        flags=re.IGNORECASE
    )
    return sql


def _strip_sql_tail(sql: str) -> str:
    tail_markers = [
        r"\s+在表结构中[:：]?",
        r"\s+表结构中[:：]?",
        r"\s+(?:说明|注意|解释|思路|步骤|但是|不过|此外|然而|因此|所以)[:：]?",
        r"\s+(?:However|Note|But|Another|Explanation|Steps?|The\s+only|This\s+query)\b",
    ]
    for pattern in tail_markers:
        sql = re.split(pattern, sql, maxsplit=1, flags=re.I | re.S)[0]
    return sql.strip()


def extract_sql(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.I | re.S).strip()
    fenced = re.findall(r"```(?:sql|SQL)?\s*(.*?)```", text, flags=re.I | re.S)
    candidates = fenced if fenced else [text]

    sql = ""
    for candidate in candidates:
        candidate = candidate.strip()
        starts = [m.start() for m in re.finditer(r"\b(?:SELECT\s+|WITH\b)", candidate, flags=re.I)]
        for start in reversed(starts):
            item = candidate[start:].strip()
            if re.match(r"SELECT\s+[\u4e00-\u9fff]", item, flags=re.I):
                continue
            if ";" in item:
                item = item.split(";", 1)[0]
            item = _strip_sql_tail(item)
            item = re.split(
                r"\s+(?:但|不过|此外|现在|如果|因为|所以|需要|用户|表结构|知识图|之前|例如|这里|注意)",
                item,
                maxsplit=1,
            )[0].strip()
            if re.match(r"^(SELECT\s+|WITH\b)", item, flags=re.I) and re.search(r"\bFROM\b", item, flags=re.I):
                sql = item
                break
        if sql:
            break
    sql = sql.strip().rstrip(";")  
    sql = re.sub(r"--.*?$", "", sql, flags=re.M)  
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.S)  
    sql = re.sub(
        r"\s+-\s+(?=(FROM|JOIN|WHERE|GROUP\s+BY|HAVING|ORDER\s+BY)\b)",
        " ",
        sql,
        flags=re.I,
    )
    sql = re.sub(r"\s+", " ", sql).strip()  
    sql = _quote_chinese_aliases(sql)  # ★ 修复：Oracle 中文别名必须加双引号
    return sql  


def validate_sql_against_dictionary(sql: str, allowed_tables: Optional[List[str]] = None) -> Tuple[bool, str]:
    if not sql:
        return False, "SQL is empty"

    allowed = {t.upper() for t in (allowed_tables or []) if t}
    alias_to_table: Dict[str, str] = {}
    used_tables: Set[str] = set()

    from_match = re.search(
        r"\bFROM\b\s+(.+?)(?=\bWHERE\b|\bGROUP\s+BY\b|\bORDER\s+BY\b|\bHAVING\b|\bUNION\b|\Z)",
        sql,
        flags=re.I | re.S,
    )
    if from_match:
        from_part = re.sub(r"\b(LEFT|RIGHT|FULL|INNER|OUTER|CROSS)\s+JOIN\b", " JOIN ", from_match.group(1), flags=re.I)
        pieces = re.split(r"\bJOIN\b|,", from_part, flags=re.I)
        for piece in pieces:
            piece = re.split(r"\bON\b", piece, flags=re.I)[0].strip()
            if not piece or piece.startswith("("):
                continue
            match = re.match(
                r"([A-Z0-9_#$]+)(?:\s+(?:AS\s+)?([A-Z][A-Z0-9_#$]*))?",
                piece,
                flags=re.I,
            )
            if not match:
                continue
            table = match.group(1).upper()
            alias = (match.group(2) or table).upper()
            if table in {"SELECT", "WHERE", "GROUP", "ORDER", "ON"}:
                continue
            used_tables.add(table)
            alias_to_table[alias] = table
            alias_to_table[table] = table

    for table in used_tables:
        if get_table_info(table) is None:
            return False, f"table {table} is not in the database dictionary"
        if allowed and table not in allowed:
            return False, f"table {table} is not in retrieved allowed tables"

    for alias, column in re.findall(r"\b([A-Z][A-Z0-9_#$]*)\.([A-Z][A-Z0-9_#$]*)\b", sql, flags=re.I):
        alias_u = alias.upper()
        column_u = column.upper()
        table = alias_to_table.get(alias_u)
        if not table:
            continue
        live_columns = get_live_table_column_names(table)
        if live_columns == set():
            return False, f"table {table} does not exist in live database"
        table_columns = live_columns if live_columns is not None else set(get_table_columns(table).keys())
        if column_u not in table_columns:
            return False, f"column {alias_u}.{column_u} is not in table {table}"

    return True, ""


# ============================================================  
# ④ Oracle 连接  
# ============================================================  
def make_dsn(cfg: Dict[str, Any]) -> str:  
    host = cfg["DB_HOST"]  
    port = cfg["DB_PORT"]  
    service_name = cfg["DB_SERVICE_NAME"]  
    dsn = (  
        f"(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST={host})(PORT={port}))"  
        f"(CONNECT_DATA=(SERVICE_NAME={service_name})))"  
    )  
    return dsn  


def get_conn_from_session() -> oracledb.Connection:  
    cfg = session.get("db_config") or DEFAULT_DB_CONFIG  
    return oracledb.connect(  
        user=cfg["DB_USER"], password=cfg["DB_PASSWORD"], dsn=make_dsn(cfg)  
    )  


def run_sql(sql: str, max_rows: int = MAX_ROWS, timeout: int = 60) -> Tuple[List[str], List[List[Any]]]:  
    conn = get_conn_from_session()  
    try:  
        cur = conn.cursor()  
        try:  
            cur.execute(f"ALTER SESSION SET MAX_DUMP_FILE_SIZE = UNLIMITED")  
        except:  
            pass  
        start_time = time.time()  
        cur.execute(sql)  
        cols = [d[0] for d in cur.description] if cur.description else []  
        rows = []  
        batch_size = 1000  
        fetched = 0  
        while fetched < max_rows:  
            elapsed = time.time() - start_time  
            if elapsed > timeout:  
                print(f"  ⚠️ SQL执行超时（{timeout}秒），已获取 {fetched} 行")  
                break  
            batch = cur.fetchmany(min(batch_size, max_rows - fetched))  
            if not batch:  
                break  
            for r in batch:  
                row = []  
                for v in r:  
                    if isinstance(v, (datetime, date)):  
                        row.append(v.isoformat(sep=" "))  
                    elif isinstance(v, Decimal):  
                        row.append(float(v))  
                    elif v is None:  
                        row.append("")  
                    else:  
                        row.append(v)  
                rows.append(row)  
            fetched += len(batch)  
        return cols, rows  
    finally:  
        conn.close()  


# ============================================================  
# ⑤ 数据字典加载  
# ============================================================  
def load_data_dictionary() -> Dict[str, Any]:  
    print(f"📖 正在加载数据字典：{DATA_DICT_PATH}")  
    if DATA_DICT_PATH.suffix.lower() in {".xlsx", ".xlsm"}:
        try:
            import importlib.util

            adapter_path = (
                Path(__file__).resolve().parent
                / "third_party"
                / "raganything-1.3.1"
                / "raganything"
                / "sql_dictionary.py"
            )
            spec = importlib.util.spec_from_file_location(
                "coordsql_raganything_sql_dictionary", adapter_path
            )
            if spec is None or spec.loader is None:
                raise RuntimeError(f"Cannot load dictionary adapter from {adapter_path}")
            adapter = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(adapter)
            dd = adapter.load_database_dictionary_excel(DATA_DICT_PATH)
        except Exception as exc:
            json_fallback = Path("data_dictionary.json")
            if "DATA_DICT_PATH" not in os.environ and json_fallback.exists():
                print(
                    f"⚠️ Excel 数据字典加载失败，临时回退到 {json_fallback}: "
                    f"{type(exc).__name__}: {exc}"
                )
                with open(json_fallback, "r", encoding="utf-8") as f:
                    dd = json.load(f)
            else:
                raise
    else:
        with open(DATA_DICT_PATH, "r", encoding="utf-8") as f:  
            dd = json.load(f)  
    print("✅ 数据字典加载成功")  
    print(f"  - 主表数量：{len(dd.get('main_tables', {}))}")  
    print(f"  - 编码表数量：{len(dd.get('code_tables', {}))}")  
    return dd  


DATA_DICT: Dict[str, Any] = load_data_dictionary()  


# ============================================================  
# ⑥ Schema 工具函数  
# ============================================================  
def get_table_info(table: str) -> Optional[Dict[str, Any]]:  
    t = table.upper()  
    for src in [DATA_DICT.get("main_tables", {}), DATA_DICT.get("code_tables", {})]:  
        if t in src:  
            return src[t]  
        for k, v in src.items():  
            if k.upper() == t:  
                return v  
    return None  


def get_table_columns(table: str) -> Dict[str, Dict[str, Any]]:  
    info = get_table_info(table)  
    if not info:  
        return {}  
    cols = info.get("columns", {})  
    result = {}  
    if isinstance(cols, list):  
        for c in cols:  
            if isinstance(c, dict):  
                n = (c.get("name") or "").upper()  
                if n:  
                    result[n] = c  
    elif isinstance(cols, dict):  
        for _, c in cols.items():  
            if isinstance(c, dict):  
                n = (c.get("name") or "").upper()  
                if n:  
                    result[n] = c  
    return result  


_live_columns_cache: Dict[str, Optional[Set[str]]] = {}


def get_live_table_column_names(table: str) -> Optional[Set[str]]:
    table_u = (table or "").upper()
    if not table_u:
        return None
    if table_u in _live_columns_cache:
        return _live_columns_cache[table_u]
    try:
        conn = get_conn_from_session()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT COLUMN_NAME FROM USER_TAB_COLUMNS WHERE TABLE_NAME = :table_name",
                {"table_name": table_u},
            )
            cols = {str(row[0]).upper() for row in cur.fetchall() if row and row[0]}
            _live_columns_cache[table_u] = cols
            return _live_columns_cache[table_u]
        finally:
            conn.close()
    except Exception:
        _live_columns_cache[table_u] = None
        return None


def is_code_table(table: str) -> bool:  
    t = table.upper()  
    for src_key in ["main_tables", "code_tables"]:  
        src = DATA_DICT.get(src_key, {})  
        for k, v in src.items():  
            if k.upper() == t:  
                return bool(v.get("is_code_table", src_key == "code_tables"))  
    return False  


# ============================================================  
# ⑦ 编码表映射（核心：来自 app_3 的"CODE→NAME 替换"逻辑）  
# ============================================================  
def _build_code_map() -> Dict[str, List[Dict[str, str]]]:  
    code_tables_info = DATA_DICT.get("code_tables", {})  
    main_tables_info = DATA_DICT.get("main_tables", {})  

    def _find_name_col(col_dict: Dict[str, Any], code_col: str) -> Optional[str]:  
        base = re.sub(r'(_CODE|_BM|_ID|_NO)$', '', code_col, flags=re.I)  
        candidates = [  
            f"{base}_NAME", f"{base}_DESC", f"{base}NAME",  
            f"{base}_CN", f"{base}MC", "NAME", "DESC",  
            "DESCRIPTION", "ITEM_NAME", "DEPT_NAME",  
        ]  
        col_upper = {k.upper(): k for k in col_dict.keys()}  
        for cand in candidates:  
            if cand.upper() in col_upper:  
                return cand.upper()  
        for cn in col_dict.keys():  
            cu = cn.upper()  
            if any(x in cu for x in ["NAME", "DESC", "MC", "_CN"]) and cu != code_col.upper():  
                return cu  
        return None  

    mapping: Dict[str, List[Dict[str, str]]] = defaultdict(list)  

    for ct_name, ct_info in code_tables_info.items():  
        ct_cols = ct_info.get("columns", {})  
        ct_col_names = {}  
        if isinstance(ct_cols, list):  
            for c in ct_cols:  
                if isinstance(c, dict):  
                    n = (c.get("name") or "").upper()  
                    if n:  
                        ct_col_names[n] = c  
        elif isinstance(ct_cols, dict):  
            for _, c in ct_cols.items():  
                if isinstance(c, dict):  
                    n = (c.get("name") or "").upper()  
                    if n:  
                        ct_col_names[n] = c  

        if not ct_col_names:  
            continue  

        code_col_candidates = []  
        for cn in ct_col_names.keys():  
            if (cn.endswith("_CODE") or cn.endswith("_BM") or  
                cn.endswith("_ID") or cn.endswith("_NO") or  
                cn in ("CODE", "BM", "ID")):  
                code_col_candidates.append(cn)  

        for code_col in code_col_candidates:  
            name_col = _find_name_col(ct_col_names, code_col)  
            if not name_col:  
                continue  
            for mt_name, mt_info in main_tables_info.items():  
                mt_cols = mt_info.get("columns", {})  
                mt_col_names = set()  
                if isinstance(mt_cols, list):  
                    mt_col_names = {(c.get("name") or "").upper()  
                                   for c in mt_cols if isinstance(c, dict) and c.get("name")}  
                elif isinstance(mt_cols, dict):  
                    mt_col_names = {(c.get("name") or "").upper()  
                                   for _, c in mt_cols.items() if isinstance(c, dict) and c.get("name")}  
                if code_col in mt_col_names:  
                    entry = {  
                        "code_table": ct_name.upper(),  
                        "code_col":   code_col,  
                        "name_col":   name_col,  
                        "main_table": mt_name.upper(),  
                    }  
                    existing = mapping[code_col]  
                    dup = any(  
                        e["code_table"] == entry["code_table"] and  
                        e["code_col"] == entry["code_col"]  
                        for e in existing  
                    )  
                    if not dup:  
                        mapping[code_col].append(entry)  

    return dict(mapping)  


CODE_MAP: Dict[str, List[Dict[str, str]]] = _build_code_map()  
print(f"  ✅ 编码表映射构建完成，共 {len(CODE_MAP)} 个字段可替换")  


def get_code_table_hint(main_tables: List[str]) -> str:  
    hints = []  
    seen_ct = set()  
    for mt in main_tables:  
        cols = get_table_columns(mt)  
        for col_name in cols.keys():  
            if col_name not in CODE_MAP:  
                continue  
            for mapping in CODE_MAP[col_name][:2]:  
                ct = mapping["code_table"]  
                cc = mapping["code_col"]  
                nc = mapping["name_col"]  
                key = f"{mt}.{col_name}->{ct}"  
                if key in seen_ct:  
                    continue  
                seen_ct.add(key)  
                hints.append(  
                    f"  {mt}.{col_name} 可关联编码表 {ct}：\n"  
                    f"    JOIN {ct} ON {mt}.{col_name} = {ct}.{cc}\n"  
                    f"    → 用 {ct}.{nc} 显示中文名称"  
                )  
    if not hints:  
        return ""  
    return "【编码表替换建议（如需显示中文名称，请按以下方式 JOIN）】\n" + "\n".join(hints[:12])  


def expand_with_code_tables(main_tables: List[str]) -> List[str]:  
    extra = []  
    seen = set(t.upper() for t in main_tables)  
    for mt in main_tables:  
        cols = get_table_columns(mt)  
        for col_name in cols.keys():  
            if col_name not in CODE_MAP:  
                continue  
            for mapping in CODE_MAP[col_name][:2]:  
                ct = mapping["code_table"]  
                if ct not in seen:  
                    extra.append(ct)  
                    seen.add(ct)  
    return extra[:8]  


# ============================================================  
# ⑧ 多粒度 Schema RAG 索引（表/列/关系三层）  
# ============================================================  
class MultiGranularitySchemaIndex:  
    def __init__(self, dd: Dict[str, Any]):  
        self.dd = dd  
        self.table_entries: List[Dict[str, Any]] = []  
        self.col_entries:   List[Dict[str, Any]] = []  
        self.rel_entries:   List[Dict[str, Any]] = []  
        self.table_index = None  
        self.col_index   = None  
        self.rel_index   = None  
        self.dim_table = 0  
        self.dim_col   = 0  
        self.dim_rel   = 0  

    def _all_tables(self) -> Dict[str, Any]:  
        all_t = {}  
        all_t.update(self.dd.get("main_tables", {}))  
        all_t.update(self.dd.get("code_tables", {}))  
        return all_t  

    def build_entries(self):  
        all_tables = self._all_tables()  

        self.table_entries = []  
        for tname, info in all_tables.items():  
            sd = info.get("short_description", "")  
            cn = info.get("table_cn", "")  
            text = (sd or f"{tname} {cn}").strip()  
            if text:  
                self.table_entries.append({  
                    "type": "table", "table": tname.upper(), "text": text  
                })  

        self.col_entries = []  
        for tname, info in all_tables.items():  
            cols = info.get("columns", {})  
            col_list = []  
            if isinstance(cols, list):  
                col_list = [c for c in cols if isinstance(c, dict)]  
            elif isinstance(cols, dict):  
                col_list = [c for _, c in cols.items() if isinstance(c, dict)]  
            for c in col_list:  
                col = (c.get("name") or "").upper()  
                cn  = c.get("cn", "") or ""  
                fd  = c.get("full_description", "") or ""  
                if not col:  
                    continue  
                text = f"{tname.upper()}.{col} {cn} {fd}".strip()  
                self.col_entries.append({  
                    "type": "column", "table": tname.upper(),  
                    "column": col, "text": text  
                })  

        tcols: Dict[str, Set[str]] = {}  
        for tname, info in all_tables.items():  
            cols = info.get("columns", {})  
            col_names = set()  
            if isinstance(cols, list):  
                col_names = {(c.get("name") or "").upper()  
                            for c in cols if isinstance(c, dict) and c.get("name")}  
            elif isinstance(cols, dict):  
                col_names = {(c.get("name") or "").upper()  
                            for _, c in cols.items() if isinstance(c, dict) and c.get("name")}  
            if col_names:  
                tcols[tname.upper()] = col_names  

        def is_join_key(c: str) -> bool:  
            u = c.upper()  
            if "DATE" in u:  
                return False  
            return (u == "EXAM_NO" or u.endswith("_NO") or  
                    u.endswith("_ID") or u.endswith("_CODE"))  

        self.rel_entries = []  
        tables = list(tcols.keys())  
        for i in range(len(tables)):  
            for j in range(i + 1, len(tables)):  
                t1, t2 = tables[i], tables[j]  
                common = tcols[t1] & tcols[t2]  
                join_cols = sorted([c for c in common if is_join_key(c)])[:3]  
                for c in join_cols:  
                    self.rel_entries.append({  
                        "type": "relation", "left": t1, "right": t2,  
                        "column": c,  
                        "text": f"JOIN {t1}.{c} = {t2}.{c}"  
                    })  

    def _build_faiss(self, entries: List[Dict[str, Any]]) -> Tuple[Any, int]:  
        if not entries:  
            return None, 0  
        texts = [(e.get("text") or "").strip() for e in entries]  
        texts = [t for t in texts if t]  
        if not texts:  
            return None, 0  
        print(f"    正在生成 {len(texts)} 条 embedding（GPU加速+并行）...")  
        mat = ollama_embed(texts, show_progress=True, use_cache=True)  
        if mat.size == 0:  
            return None, 0  
        faiss.normalize_L2(mat)  
        dim = mat.shape[1]  
        try:  
            if hasattr(faiss, 'StandardGpuResources'):  
                print(f"    使用 GPU 构建 FAISS 索引 (dim={dim})...")  
                res = faiss.StandardGpuResources()  
                res.setTempMemory(256 * 1024 * 1024)  
                config = faiss.GpuIndexFlatConfig()  
                config.device = 0  
                gpu_index = faiss.GpuIndexFlatIP(res, dim, config)  
                gpu_index.add(mat)  
                index = faiss.index_gpu_to_cpu(gpu_index)  
                print(f"    ✅ GPU 索引构建完成")  
            else:  
                print(f"    使用 CPU 构建 FAISS 索引 (dim={dim})...")  
                index = faiss.IndexFlatIP(dim)  
                index.add(mat)  
        except Exception as e:  
            print(f"    ⚠️ GPU 构建失败，使用 CPU: {e}")  
            index = faiss.IndexFlatIP(dim)  
            index.add(mat)  
        return index, dim  

    def build_or_load(self):  
        try:  
            stat = DATA_DICT_PATH.stat()  
            cache_key = f"schema_idx_{stat.st_size}_{int(stat.st_mtime)}"  
        except Exception:  
            cache_key = "schema_idx_default"  

        meta_file        = RAG_CACHE_DIR / f"{cache_key}.meta.json"  
        table_index_file = RAG_CACHE_DIR / f"{cache_key}.table.faiss"  
        col_index_file   = RAG_CACHE_DIR / f"{cache_key}.col.faiss"  
        rel_index_file   = RAG_CACHE_DIR / f"{cache_key}.rel.faiss"  

        os.environ["TRANSFORMERS_OFFLINE"] = "1"  
        os.environ["HF_HUB_OFFLINE"] = "1"  
        _probe = ollama_embed(["test"], use_cache=False)  
        current_dim = int(_probe.shape[1]) if _probe.size > 0 else 0  
        print(f"  当前 embedding 维度: {current_dim}")  

        if (meta_file.exists() and table_index_file.exists() and  
                col_index_file.exists()):  
            try:  
                with open(meta_file, "r", encoding="utf-8") as f:  
                    meta = json.load(f)  
                cached_dim = meta.get("dim_table", 0)  
                if current_dim > 0 and cached_dim != current_dim:  
                    print(f"⚠️ 缓存维度 {cached_dim} 与当前模型维度 {current_dim} 不一致，重新构建索引")
                    raise ValueError("dim_mismatch")
                self.table_entries = meta["table_entries"]
                self.col_entries   = meta["col_entries"]
                self.rel_entries   = meta.get("rel_entries", [])
                self.dim_table     = meta.get("dim_table", 0)
                self.dim_col       = meta.get("dim_col", 0)
                self.dim_rel       = meta.get("dim_rel", 0)
                self.table_index = faiss.read_index(str(table_index_file))
                self.col_index   = faiss.read_index(str(col_index_file))
                if rel_index_file.exists() and self.rel_entries:
                    self.rel_index = faiss.read_index(str(rel_index_file))
                print("✅ RAG 索引加载完成（使用缓存）")
                return
            except Exception as e:
                print(f"⚠️ 缓存加载失败，重新构建: {e}")

        print("【首次运行】正在构建多粒度 Schema 索引...")
        self.build_entries()
        print(f"  表级 entries: {len(self.table_entries)}")
        print(f"  列级 entries: {len(self.col_entries)}")
        print(f"  关系 entries: {len(self.rel_entries)}")

        self.table_index, self.dim_table = self._build_faiss(self.table_entries)
        self.col_index,   self.dim_col   = self._build_faiss(self.col_entries)
        if self.rel_entries:
            self.rel_index, self.dim_rel = self._build_faiss(self.rel_entries)
        else:
            self.rel_index, self.dim_rel = None, 0

        meta = {
            "table_entries": self.table_entries,
            "col_entries":   self.col_entries,
            "rel_entries":   self.rel_entries,
            "dim_table":     self.dim_table,
            "dim_col":       self.dim_col,
            "dim_rel":       self.dim_rel,
        }
        try:
            with open(meta_file, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False)
            if self.table_index is not None:
                faiss.write_index(self.table_index, str(table_index_file))
            if self.col_index is not None:
                faiss.write_index(self.col_index, str(col_index_file))
            if self.rel_index is not None:
                faiss.write_index(self.rel_index, str(rel_index_file))
        except Exception as e:
            print(f"⚠️ 缓存写入失败: {e}")

        print("✅ 多粒度 Schema 索引构建完成")

    def search(self, query: str,
               topk_table: int = 6, topk_col: int = 12, topk_rel: int = 10):
        qv = ollama_embed([query], use_cache=True)
        if qv.size == 0:
            return [], [], []
        qv = qv.astype(np.float32)
        faiss.normalize_L2(qv)

        def _search(index, entries, k):
            if index is None or not entries:
                return []
            if qv.shape[1] != index.d:
                print(f"  ⚠️ 查询向量维度 {qv.shape[1]} 与索引维度 {index.d} 不匹配，跳过检索")
                return []
            try:
                scores, idxs = index.search(qv, min(k, len(entries)))
            except Exception as e:
                print(f"  ⚠️ FAISS 检索失败: {e}")
                return []
            return [
                (float(s), entries[int(i)])
                for s, i in zip(scores[0], idxs[0])
                if i >= 0
            ]

        tables = _search(self.table_index, self.table_entries, topk_table)
        cols   = _search(self.col_index,   self.col_entries,   topk_col)
        rels   = _search(self.rel_index,   self.rel_entries,   topk_rel) if self.rel_index else []
        return tables, cols, rels


_FAISS_SCHEMA_INDEX = MultiGranularitySchemaIndex(DATA_DICT)
SCHEMA_INDEX = RAGAnythingSchemaRetriever(
    data_dict=DATA_DICT,
    embed_func=ollama_embed,
    chat_func=ollama_chat,
    fallback_index=_FAISS_SCHEMA_INDEX,
    dictionary_path=DATA_DICT_PATH,
    working_dir=Path(os.environ.get("RAGANYTHING_WORKING_DIR", "./raganything_storage")),
    enabled=os.environ.get("USE_RAGANYTHING", "1").lower() not in {"0", "false", "no"},
)
SCHEMA_INDEX.build_or_load()


# ============================================================
# ⑨ Schema 文本生成（给 LLM 的 prompt 上下文）
# ============================================================
def schema_text_for_tables(tables: List[str], max_cols_each: int = 35) -> str:
    lines = ["=" * 60,
             "可用表结构（Oracle 11g），只能使用以下表和字段：",
             "=" * 60]
    for t in tables:
        info = get_table_info(t)
        if not info:
            continue
        cols = info.get("columns", {})
        tag = "[编码表]" if info.get("is_code_table") or is_code_table(t) else "[业务表]"
        lines.append(f"\nTABLE {t.upper()} {tag} ({info.get('table_cn', '')})")

        col_list = []
        if isinstance(cols, list):
            col_list = [c for c in cols if isinstance(c, dict)]
        elif isinstance(cols, dict):
            col_list = [c for _, c in cols.items() if isinstance(c, dict)]

        live_cols = get_live_table_column_names(t)
        if live_cols == set():
            continue
        if live_cols is not None:
            col_list = [
                c for c in col_list
                if (c.get("name") or "").upper() in live_cols
            ]

        shown = 0
        for c in col_list:
            if shown >= max_cols_each:
                lines.append(f"  ... 共 {len(col_list)} 列，省略其余")
                break
            name = c.get("name", "")
            cn   = c.get("cn", "")
            ts   = c.get("type_str", c.get("data_type", ""))
            lines.append(f"  - {name} ({cn}) [{ts}]")
            shown += 1
    return "\n".join(lines)


def schema_kg_context_for_prompt(retrieve: Dict[str, Any], max_chars: int = 3000) -> str:
    context = (retrieve.get("rag_context") or "").strip()
    if not context:
        return ""
    context = context[:max_chars]
    return (
        "\n\n[RAGAnything LightRAG knowledge-graph context]\n"
        "Use these retrieved schema markers as evidence. Keep exact table/column names.\n"
        f"{context}\n"
    )


# ★ 新增：生成 JOIN 关系提示文本（把 RAG 检索到的关系传给 LLM）
def join_hints_text(rels_hits: List[Tuple[float, Dict[str, Any]]], candidate_tables: List[str]) -> str:
    """
    将 RAG 检索到的关系信息格式化为 LLM 可理解的 JOIN 提示。
    只保留候选表之间的关系。
    """
    if not rels_hits:
        return ""
    cand_set = set(t.upper() for t in candidate_tables)
    hints = []
    seen = set()
    for score, ent in rels_hits:
        left  = ent.get("left", "").upper()
        right = ent.get("right", "").upper()
        col   = ent.get("column", "")
        if left not in cand_set or right not in cand_set:
            continue
        key = f"{left}.{col}={right}.{col}"
        if key in seen:
            continue
        seen.add(key)
        hints.append(f"  {left}.{col} = {right}.{col}")
    if not hints:
        return ""
    return "【表间可用 JOIN 关系（基于同名关联键自动推断）】\n" + "\n".join(hints[:15])


# ============================================================
# ⑩ Retrieve（含编码表自动扩展）
# ============================================================
def retrieve_schema(question: str,
                    user_selected_tables: Optional[List[str]] = None) -> Dict[str, Any]:
    tables_hits, cols_hits, rels_hits = SCHEMA_INDEX.search(question)

    table_scores: Dict[str, float] = defaultdict(float)
    evidence_order: List[str] = []

    def add_table_score(table: str, score: float) -> None:
        table = (table or "").upper()
        if not table or get_table_info(table) is None:
            return
        if get_live_table_column_names(table) == set():
            return
        if table not in table_scores:
            evidence_order.append(table)
        table_scores[table] += float(score)

    if user_selected_tables:
        for t in user_selected_tables:
            add_table_score(t, 100.0)

    for score, ent in tables_hits:
        add_table_score(ent.get("table"), max(float(score), 0.0))

    for score, ent in cols_hits:
        add_table_score(ent.get("table"), max(float(score), 0.0) * 1.2)

    for score, ent in rels_hits:
        add_table_score(ent.get("left"), max(float(score), 0.0) * 0.6)
        add_table_score(ent.get("right"), max(float(score), 0.0) * 0.6)

    order_index = {table: idx for idx, table in enumerate(evidence_order)}
    ranked_tables = sorted(
        table_scores,
        key=lambda table: (
            table_scores[table],
            0 if not is_code_table(table) else -1,
            -order_index.get(table, 0),
        ),
        reverse=True,
    )
    base_limit = int(os.environ.get("SCHEMA_CANDIDATE_TABLE_LIMIT", "14"))
    final_limit = int(os.environ.get("SCHEMA_CANDIDATE_TABLE_FINAL_LIMIT", "16"))
    relation_expand_limit = int(os.environ.get("SCHEMA_RELATION_EXPAND_LIMIT", "2"))

    uniq = ranked_tables[:base_limit]
    seen = set(uniq)

    expanded_by_relation = 0
    for _score, rel in rels_hits:
        left = (rel.get("left") or "").upper()
        right = (rel.get("right") or "").upper()
        if not left or not right:
            continue
        if is_code_table(left) == is_code_table(right):
            continue
        if left in seen and right not in seen and get_table_info(right) is not None and get_live_table_column_names(right) != set():
            uniq.append(right)
            seen.add(right)
        elif right in seen and left not in seen and get_table_info(left) is not None and get_live_table_column_names(left) != set():
            uniq.append(left)
            seen.add(left)
        else:
            continue
        expanded_by_relation += 1
        if expanded_by_relation >= relation_expand_limit or len(uniq) >= final_limit:
            break

    need_name = any(k in question for k in [
        "名称", "名字", "中文", "含义", "描述", "字典", "对应", "显示",
        "叫什么", "是什么", "部门名", "科室名", "类型名"
    ])
    if need_name:
        extra = expand_with_code_tables(uniq)
        for t in extra:
            if t not in seen and get_live_table_column_names(t) != set():
                uniq.append(t)
                seen.add(t)
        uniq = uniq[:final_limit]

    return {
        "tables_hits":      tables_hits,
        "cols_hits":        cols_hits,
        "rels_hits":        rels_hits,       # ★ 新增：把关系检索结果也传出去
        "candidate_tables": uniq,
        "need_name":        need_name,
        "rag_backend":      getattr(SCHEMA_INDEX, "backend_name", "unknown"),
        "rag_context":      getattr(SCHEMA_INDEX, "last_context", ""),
        "rag_error":        getattr(SCHEMA_INDEX, "last_error", ""),
    }


# ============================================================
# ⑪ 多表查询：Plan → Generate → Execute → LLM Self-Repair
# ============================================================

def make_plan(question: str, start_date: str, end_date: str,
              schema_ctx: str, code_hint: str = "") -> Dict[str, Any]:
    """
    ★ 修改：极简 Plan Prompt，只传表名列表，不传列信息。
    目标：让 LLM 在 10~20s 内返回 needed_tables，其余字段留空。
    """
    # 只提取表名，不传任何列信息
    table_names = re.findall(r'TABLE\s+(\w+)', schema_ctx)
    table_list  = "、".join(table_names) if table_names else "（见Schema）"

    if OLLAMA_SQL_MODEL == "qwen3-vl:8b" and os.environ.get("QWEN3_VL_SKIP_LLM_PLAN", "1").lower() not in {"0", "false", "no"}:
        return _empty_plan("qwen3_vl_deterministic_empty_plan")

    prompt = (
        f"从以下表中选出回答用户问题最需要的1~4张表，只输出JSON。\n"
        f"问题：{question[:200]}\n"
        f"可用表：{table_list}\n"
        f'只输出：{{"needed_tables":["表名"],"need_code_table":true/false}}'
    )

    try:
        txt = ollama_chat(prompt, temperature=0.0, max_tokens=200, timeout=30, num_ctx=2048)
    except Exception as e:
        print(f"  ⚠️ make_plan 异常: {e}，使用空计划")
        return _empty_plan("plan_exception")

    if not txt:
        print(f"  ⚠️ make_plan 超时或返回空，使用空计划（让 generate 自行决策）")
        return _empty_plan("plan_timeout")

    txt = re.sub(r"```(?:json)?", "", txt, flags=re.I).replace("```", "").strip()
    m = re.search(r"\{.*\}", txt, flags=re.S)
    if not m:
        return _empty_plan("plan_parse_failed")
    try:
        return json.loads(m.group())
    except Exception:
        return _empty_plan("plan_json_error")


def _empty_plan(reason: str = "") -> Dict[str, Any]:
    """返回空 Plan，让 generate 完全依赖 RAG 候选表自主生成"""
    return {
        "needed_tables":  [],
        "group_by":       [],
        "metrics":        [],
        "filters":        [],
        "join_hints":     [],
        "need_code_table": False,
        "notes":          reason
    }


def _slim_schema_for_plan(schema_ctx: str, max_cols_per_table: int = 5) -> str:
    """
    ★ 新增：为 make_plan 精简 Schema，每张表只保留前N列。
    避免把完整 8000+ 字符 Schema 塞进 Plan Prompt。
    """
    lines  = schema_ctx.splitlines()
    result = []
    col_count = 0

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("TABLE ") or stripped.startswith("="):
            result.append(line)
            col_count = 0           # 每张新表重置计数
        elif stripped.startswith("- "):
            if col_count < max_cols_per_table:
                result.append(line)
                col_count += 1
            elif col_count == max_cols_per_table:
                result.append("  ... (更多列已省略)")
                col_count += 1      # 只加一次省略提示
        else:
            result.append(line)

    return "\n".join(result)


def generate_multi_sql(question: str, start_date: str, end_date: str,
                       plan: Dict[str, Any], schema_ctx: str,
                       join_hint: str = "",    # ★ 新增：JOIN关系提示
                       code_hint: str = "",
                       history: str = "",      # ★ 修改：改为全量历史（原来叫feedback）
                       temperature: float = 0.0) -> str:
    """
    多表 SQL 生成器。
    ★ 修改要点：
      1. join_hint 参数：把 RAG 检索到的已知JOIN关系传给 LLM
      2. history 参数：传入所有历史失败记录（不只是上一轮）
      3. few-shot 示例：帮助 LLM 理解多表 JOIN 写法
    """
    plan_txt  = json.dumps(plan, ensure_ascii=False, indent=2)
    code_sec  = f"\n{code_hint}\n" if code_hint else ""
    join_sec  = f"\n{join_hint}\n" if join_hint else ""
    hist_sec  = (
        f"\n【历史失败记录（必须逐一分析并避免重复同样错误）】\n{history}\n"
        if history else ""
    )

    # ★ 新增：精简 schema_ctx，只保留 plan 指定的表 + 编码表，最多8张表
    # 避免把16张表全部塞进 prompt 导致超过 num_ctx 截断或超时
    plan_tables = [t.upper() for t in plan.get("needed_tables", []) if isinstance(t, str)]
    all_schema_tables = re.findall(r'TABLE\s+(\w+)', schema_ctx)
    if plan_tables:
        # 优先 plan 选定的表，再补充编码表（is_code_table），总数不超过8
        priority = [t for t in all_schema_tables if t.upper() in {p.upper() for p in plan_tables}]
        code_tbls = [t for t in all_schema_tables if is_code_table(t) and t not in priority]
        trimmed_tables = (priority + code_tbls)[:8]
        if trimmed_tables and len(trimmed_tables) < len(all_schema_tables):
            schema_ctx = schema_text_for_tables(trimmed_tables)

    # 动态生成业务口径提示
    all_cols: Set[str] = set()
    for t in plan.get("needed_tables", []):
        if isinstance(t, str):
            all_cols.update(get_table_columns(t).keys())
    for t_name in re.findall(r'TABLE\s+(\w+)', schema_ctx):
        all_cols.update(get_table_columns(t_name).keys())

    business_hint = ""
    if "ISCHINNESE" in all_cols:
        business_hint += "- ISCHINNESE = '1' 表示中国人（中宾），'0' 表示外国人（外宾）\n"
    if "ISNORMAL" in all_cols:
        business_hint += "- ISNORMAL = '1' 表示结果正常，'0' 表示结果异常\n"

    business_sec = (
        f"\n业务字段说明（字段存在时才使用）：\n{business_hint}"
        if business_hint else ""
    )

    # Fixed SQL examples can be copied by qwen3-vl and hurt generalization.
    few_shot = ""

    prompt = f"""你是 Oracle 11g SQL 专家（只写 SELECT 语句）。
根据用户问题、查询计划和表结构，生成正确的多表关联查询 SQL。
{hist_sec}
用户问题：{question}
日期范围：{start_date} 到 {end_date}

查询计划：
{plan_txt}
{business_sec}

表结构（只能使用以下表和字段）：
{schema_ctx}
{join_sec}
{code_sec}
{few_shot}
SQL 规范：
1. 只输出一条 SELECT 语句，不加任何解释
2. Oracle 11g：不用 LIMIT/FETCH FIRST，分页用 ROWNUM
3. 条件计数：SUM(CASE WHEN ... THEN 1 ELSE 0 END)
4. 日期过滤：字段 >= TO_DATE('{start_date}','YYYY-MM-DD') AND 字段 <= TO_DATE('{end_date}','YYYY-MM-DD')
5. 分组字段必须同时出现在 SELECT 和 GROUP BY
6. 字符串值用单引号，表别名用 a/b/c/d
7. SQL 以 SELECT 开头，不加分号
8. 优先使用【已知JOIN关系】中的条件连表
9. 如果有历史失败记录，必须针对性修复，不能重复同样的写法
10. **严禁使用表结构中未列出的字段**
11. 列别名如果含中文或特殊字符（如 /），必须用双引号包裹，例如：AS "中国籍受检总人数"、AS "中国籍异常/阳性人数"

直接输出SQL："""

    try:
        txt = ollama_chat(prompt, temperature=temperature, max_tokens=2048)
        return extract_sql(txt)
    except Exception as e:
        print(f"  ❌ LLM 调用失败: {e}")
        return ""


def _fallback_query_terms(text: str) -> Set[str]:
    terms = {m.group(0).lower() for m in re.finditer(r"[A-Za-z_][A-Za-z0-9_]*", text or "")}
    for segment in re.findall(r"[\u4e00-\u9fff]+", text or ""):
        terms.add(segment)
        for n in range(2, min(8, len(segment)) + 1):
            for i in range(0, len(segment) - n + 1):
                terms.add(segment[i : i + n])
    return terms


def _fallback_requested_labels(question: str) -> List[str]:
    q = question or ""
    if "包括" in q:
        target = q.split("包括", 1)[1]
    elif "包含" in q:
        target = q.split("包含", 1)[1]
    elif "需要" in q:
        target = q.split("需要", 1)[1]
    else:
        target = q
    labels: List[str] = []
    for part in re.split(r"[、,，；;。\s]+|以及|和|及", target):
        label = part.strip(" ：:()（）")
        if 2 <= len(label) <= 12 and re.search(r"[\u4e00-\u9fff]", label):
            if not any(x in label for x in ["关联", "按照", "期间", "每个"]):
                labels.append(label)
    out: List[str] = []
    seen: Set[str] = set()
    for label in labels:
        if label not in seen:
            out.append(label)
            seen.add(label)
    return out[:12]


def _table_has_column(table: str, column: str) -> bool:
    table_u = (table or "").upper()
    column_u = (column or "").upper()
    live = get_live_table_column_names(table_u)
    if live is not None:
        return column_u in live
    return column_u in get_table_columns(table_u)


def _explicit_query_limit(question: str, default: int) -> int:
    m = re.search(r"前\s*(\d+)\s*条", question or "")
    if not m:
        return default
    try:
        return max(1, min(int(m.group(1)), 5000))
    except Exception:
        return default


def _explicit_tables_in_question(question: str, allowed_tables: List[str]) -> List[str]:
    q_upper = (question or "").upper()
    pool = {str(t).upper() for t in allowed_tables or [] if get_table_info(str(t))}
    pool.update(str(t).upper() for t in DATA_DICT.keys())
    hits: List[Tuple[int, int, str]] = []
    for table in pool:
        if not table:
            continue
        paren = re.search(rf"[（(]\s*{re.escape(table)}\s*[）)]", q_upper)
        m = paren or re.search(rf"(?<![A-Z0-9_#$]){re.escape(table)}(?![A-Z0-9_#$])", q_upper)
        if m and get_live_table_column_names(table) != set():
            priority = 0 if paren else 1
            hits.append((priority, m.start(), table))
    hits.sort(key=lambda item: (item[0], item[1], -len(item[2])))
    out: List[str] = []
    seen: Set[str] = set()
    for _priority, _pos, table in hits:
        if table not in seen:
            out.append(table)
            seen.add(table)
    return out


def _explicit_parenthesized_columns(question: str, tables: List[str]) -> List[str]:
    ids = re.findall(r"\(([A-Z][A-Z0-9_#$]{1,})\)", (question or "").upper())
    out: List[str] = []
    seen: Set[str] = set()
    for ident in ids:
        if ident in seen:
            continue
        if any(_table_has_column(table, ident) for table in tables):
            out.append(ident)
            seen.add(ident)
    return out


def _explicit_table_column_pairs(question: str, tables: List[str]) -> List[Tuple[str, str]]:
    table_set = {t.upper() for t in tables}
    pairs: List[Tuple[str, str]] = []
    seen: Set[Tuple[str, str]] = set()
    for table, column in re.findall(r"([A-Z][A-Z0-9_#$]+)\.([A-Z][A-Z0-9_#$]+)", (question or "").upper()):
        pair = (table.upper(), column.upper())
        if pair in seen or pair[0] not in table_set:
            continue
        if _table_has_column(pair[0], pair[1]):
            pairs.append(pair)
            seen.add(pair)
    return pairs


def _explicit_default_columns(table: str, limit: int = 4) -> List[str]:
    live = get_live_table_column_names(table)
    cols: List[str] = []
    for column in get_table_columns(table).keys():
        col_u = column.upper()
        if live is not None and col_u not in live:
            continue
        cols.append(col_u)
        if len(cols) >= limit:
            break
    return cols


def explicit_structured_sql(
    question: str,
    start_date: str,
    end_date: str,
    allowed_tables: List[str],
) -> str:
    """
    Deterministic SQL path for explicit enterprise-report questions.
    It only fires when the user states concrete table/column identifiers
    and a simple list/group/aggregate/join intent; otherwise it returns
    an empty string and the normal RAG + LLM path handles the request.
    """
    q = question or ""
    tables = _explicit_tables_in_question(q, allowed_tables)
    if not tables:
        return ""

    relation = re.search(
        r"([A-Z][A-Z0-9_#$]+)\.([A-Z][A-Z0-9_#$]+)\s*=\s*([A-Z][A-Z0-9_#$]+)\.([A-Z][A-Z0-9_#$]+)",
        q.upper(),
    )

    if relation:
        left, left_col, right, right_col = [x.upper() for x in relation.groups()]
        if left not in tables:
            tables.insert(0, left)
        if right not in tables:
            tables.append(right)
        if not (
            get_table_info(left)
            and get_table_info(right)
            and _table_has_column(left, left_col)
            and _table_has_column(right, right_col)
        ):
            return ""
        limit = _explicit_query_limit(q, 5000)
        join_sql = f"{left} a JOIN {right} b ON a.{left_col} = b.{right_col}"

        if "记录总数" in q or ("COUNT" in q.upper() and "GROUP" not in q.upper()):
            return f"SELECT COUNT(*) AS CNT FROM (SELECT 1 FROM {join_sql} WHERE ROWNUM <= {limit})"

        if "合计" in q:
            m = re.search(
                r"按\s*([A-Z][A-Z0-9_#$]+)\.([A-Z][A-Z0-9_#$]+)\s*分组.*?([A-Z][A-Z0-9_#$]+)\.([A-Z][A-Z0-9_#$]+).*?合计",
                q.upper(),
            )
            if m:
                g_table, g_col, n_table, n_col = [x.upper() for x in m.groups()]
                if {g_table, n_table}.issubset({left, right}) and _table_has_column(g_table, g_col) and _table_has_column(n_table, n_col):
                    g_alias = "a" if g_table == left else "b"
                    n_alias = "a" if n_table == left else "b"
                    return (
                        f"SELECT GROUP_VALUE, SUM(NUM_VALUE) AS SUM_VALUE FROM ("
                        f"SELECT {g_alias}.{g_col} AS GROUP_VALUE, {n_alias}.{n_col} AS NUM_VALUE "
                        f"FROM {join_sql} WHERE {g_alias}.{g_col} IS NOT NULL "
                        f"AND {n_alias}.{n_col} IS NOT NULL AND ROWNUM <= {limit}) "
                        f"GROUP BY GROUP_VALUE ORDER BY SUM_VALUE DESC, GROUP_VALUE"
                    )

        if "分组" in q and ("记录数" in q or "匹配记录数" in q):
            group_col = ""
            m = re.search(r"再按\s*([A-Z][A-Z0-9_#$]+)\s*的.*?\(([A-Z][A-Z0-9_#$]+)\)\s*分组", q.upper())
            if m and m.group(1).upper() in {left, right}:
                candidate_table, candidate_col = m.group(1).upper(), m.group(2).upper()
                if _table_has_column(candidate_table, candidate_col):
                    group_col = candidate_col
                    group_alias = "a" if candidate_table == left else "b"
                else:
                    group_alias = "a"
            else:
                paren_cols = [c for c in _explicit_parenthesized_columns(q, [left]) if c != left_col]
                group_col = paren_cols[0] if paren_cols else ""
                group_alias = "a"
            if group_col and _table_has_column(left if group_alias == "a" else right, group_col):
                return (
                    f"SELECT GROUP_VALUE, COUNT(*) AS CNT FROM ("
                    f"SELECT {group_alias}.{group_col} AS GROUP_VALUE FROM {join_sql} "
                    f"WHERE {group_alias}.{group_col} IS NOT NULL AND ROWNUM <= {limit}) "
                    f"GROUP BY GROUP_VALUE ORDER BY CNT DESC, GROUP_VALUE"
                )

        if "返回" in q:
            ret_seg = q.upper().split("返回", 1)[-1]
            pairs = [
                pair for pair in _explicit_table_column_pairs(ret_seg, [left, right])
                if pair not in {(left, left_col), (right, right_col)}
            ]
            if len(pairs) >= 2:
                selects = []
                for idx, (table, column) in enumerate(pairs[:6], start=1):
                    alias = "a" if table == left else "b"
                    label = "LEFT_VALUE" if idx == 1 else "RIGHT_VALUE" if idx == 2 else f"VALUE_{idx}"
                    selects.append(f"{alias}.{column} AS {label}")
                row_limit = _explicit_query_limit(q, 10)
                return f"SELECT {', '.join(selects)} FROM {join_sql} WHERE ROWNUM <= {row_limit}"

        return ""

    table = tables[0]
    cols = _explicit_parenthesized_columns(q, [table])
    limit = _explicit_query_limit(q, 10)

    if "分组" in q and "记录数" in q and cols:
        col = cols[0]
        return (
            f"SELECT {col} AS GROUP_VALUE, COUNT(*) AS CNT "
            f"FROM (SELECT {col} FROM {table} WHERE {col} IS NOT NULL AND ROWNUM <= 5000) "
            f"GROUP BY {col} ORDER BY CNT DESC, GROUP_VALUE"
        )

    if all(word in q for word in ["合计", "平均"]) and cols:
        col = cols[0]
        return (
            f"SELECT SUM({col}) AS SUM_VALUE, AVG({col}) AS AVG_VALUE, "
            f"MIN({col}) AS MIN_VALUE, MAX({col}) AS MAX_VALUE "
            f"FROM (SELECT {col} FROM {table} WHERE {col} IS NOT NULL AND ROWNUM <= 5000)"
        )

    if ("最早日期" in q or "最晚日期" in q) and cols:
        col = cols[0]
        return (
            f"SELECT MIN({col}) AS MIN_DATE, MAX({col}) AS MAX_DATE, COUNT(*) AS CNT "
            f"FROM (SELECT {col} FROM {table} WHERE {col} IS NOT NULL AND ROWNUM <= 5000)"
        )

    if "返回" in q or "查询" in q:
        select_cols = cols or _explicit_default_columns(table, 4)
        if not select_cols:
            return ""
        return f"SELECT {', '.join(select_cols[:8])} FROM {table} WHERE ROWNUM <= {limit}"

    return ""


def _schema_only_fallback_sql(
    question: str,
    start_date: str,
    end_date: str,
    allowed_tables: List[str],
    join_hint: str,
    max_select_cols: int = 10,
) -> str:
    tables = [
        t.upper()
        for t in allowed_tables
        if get_table_info(t) and get_live_table_column_names(t) != set()
    ]
    if not tables:
        return ""

    base = next((t for t in tables if not is_code_table(t)), tables[0])
    terms = _fallback_query_terms(question)
    scored_cols: List[Tuple[float, str, str, str]] = []

    for table in tables:
        live = get_live_table_column_names(table)
        for column, meta in get_table_columns(table).items():
            col_u = column.upper()
            if live is not None and col_u not in live:
                continue
            cn = str(meta.get("cn") or "")
            usage = str(meta.get("usage") or meta.get("full_description") or "")
            haystack = f"{table} {col_u} {cn} {usage}".lower()
            score = 0.0
            if col_u.lower() in (question or "").lower():
                score += 8.0
            if cn and cn in (question or ""):
                score += 10.0
            score += sum(0.35 for term in terms if len(term) >= 2 and term in haystack)
            if score > 0:
                scored_cols.append((score, table, col_u, cn or col_u))

    scored_cols.sort(key=lambda item: item[0], reverse=True)
    selected_cols = scored_cols[:max_select_cols]
    needed_tables = {base}
    needed_tables.update(table for _score, table, _column, _label in selected_cols)

    relations: List[Tuple[str, str, str, str]] = []
    for match in re.finditer(
        r"([A-Z0-9_#$]+)\.([A-Z0-9_#$]+)\s*=\s*([A-Z0-9_#$]+)\.([A-Z0-9_#$]+)",
        join_hint or "",
        flags=re.I,
    ):
        relations.append(tuple(x.upper() for x in match.groups()))
    for rel in getattr(SCHEMA_INDEX, "rel_entries", []):
        left = str(rel.get("left") or "").upper()
        right = str(rel.get("right") or "").upper()
        left_col = str(rel.get("column") or "").upper()
        right_col = str(rel.get("right_column") or rel.get("column") or "").upper()
        if left and right and left_col and right_col:
            relations.append((left, left_col, right, right_col))

    aliases = {base: "a"}
    joined = {base}
    from_sql = f"{base} a"
    alias_names = list("bcdefghijklmnopqrstuvwxyz")
    changed = True
    while changed:
        changed = False
        for left, left_col, right, right_col in relations:
            if left not in tables or right not in tables:
                continue
            if not _table_has_column(left, left_col) or not _table_has_column(right, right_col):
                continue
            if left in joined and right in needed_tables and right not in joined:
                alias = alias_names[len(aliases) - 1]
                aliases[right] = alias
                from_sql += f" LEFT JOIN {right} {alias} ON {aliases[left]}.{left_col} = {alias}.{right_col}"
                joined.add(right)
                changed = True
            elif right in joined and left in needed_tables and left not in joined:
                alias = alias_names[len(aliases) - 1]
                aliases[left] = alias
                from_sql += f" LEFT JOIN {left} {alias} ON {aliases[right]}.{right_col} = {alias}.{left_col}"
                joined.add(left)
                changed = True

    select_parts: List[str] = []
    covered_labels: Set[str] = set()

    alias_counts: Dict[str, int] = defaultdict(int)

    def unique_label(label: str) -> str:
        label = (label or "COL").strip() or "COL"
        alias_counts[label] += 1
        if alias_counts[label] == 1:
            return label
        return f"{label}_{alias_counts[label]}"

    for _score, table, column, label in selected_cols:
        if table not in joined:
            continue
        label = unique_label(label)
        select_parts.append(f'{aliases[table]}.{column} AS "{label}"')
        covered_labels.add(label)
    if not select_parts:
        live = get_live_table_column_names(base)
        for column, meta in list(get_table_columns(base).items())[:max_select_cols]:
            col_u = column.upper()
            if live is not None and col_u not in live:
                continue
            label = str(meta.get("cn") or col_u)
            label = unique_label(label)
            select_parts.append(f'{aliases[base]}.{col_u} AS "{label}"')
            covered_labels.add(label)
            if len(select_parts) >= max_select_cols:
                break

    for label in _fallback_requested_labels(question):
        if label not in covered_labels and len(select_parts) < max_select_cols + 4:
            label = unique_label(label)
            select_parts.append(f'NULL AS "{label}"')
            covered_labels.add(label)

    date_col = ""
    for preferred in ["REG_DATE", "FEE_DATE", "LAB_DATE", "NOTE_DATE", "CREATE_DATE"]:
        for table in joined:
            if _table_has_column(table, preferred):
                date_col = f"{aliases[table]}.{preferred}"
                break
        if date_col:
            break

    where_sql = ""
    if date_col and re.search(r"\d{4}-\d{2}-\d{2}|日期|期间|至", question or ""):
        where_sql = (
            f" WHERE {date_col} >= TO_DATE('{start_date}','YYYY-MM-DD') "
            f"AND {date_col} <= TO_DATE('{end_date}','YYYY-MM-DD')"
        )

    if not select_parts:
        return ""
    return "SELECT " + ", ".join(select_parts) + " FROM " + from_sql + where_sql


def _final_repair(
    question: str,
    start_date: str,
    end_date: str,
    last_sql: str,
    history: List[Dict[str, str]],
    schema_ctx: str,
    join_hint: str,
    code_hint: str,
    allowed_tables: List[str],
    banned_tables: Set[str],
) -> str:
    """
    迭代纠错全部失败后的最终兜底修复。
    将所有失败历史、完整 Schema、JOIN 关系、业务提示、banned 表
    一次性喂给 LLM，做最后一次深度修复尝试。
    """
    print(f"\n🔧 【最终兜底修复】启动...")

    # 整理所有失败历史
    history_parts = []
    for h in history:
        history_parts.append(f"第{h['round']}轮 SQL:\n{h['sql']}\n错误：{h['error']}")
    history_text = "\n\n".join(history_parts)

    banned_hint = (
        f"\n【严禁使用的表/视图（数据库中不存在）】：{', '.join(banned_tables)}"
        if banned_tables else ""
    )

    join_sec  = f"\n{join_hint}\n" if join_hint else ""
    code_sec  = f"\n{code_hint}\n" if code_hint else ""
    allowed_hint = f"\n【可用表清单】：{', '.join(allowed_tables[:20])}" if allowed_tables else ""

    prompt = f"""你是 Oracle 11g SQL 专家，擅长深度错误分析和 SQL 修复。

【任务】：之前的自动生成已经经过 {len(history)} 轮尝试，全部失败。请你综合所有信息，生成一条能正确执行的 SQL。

【用户问题】：{question}
【日期范围】：{start_date} 到 {end_date}

【所有失败记录】：
{history_text}
{banned_hint}
{allowed_hint}

【完整表结构】：
{schema_ctx}
{join_sec}
{code_sec}
【修复要求】：
1. 逐一分析每轮失败原因，找出共同根本原因
2. 只使用【可用表清单】和【完整表结构】中明确存在的表
3. 绝对不使用【严禁使用的表】
4. 列别名含中文或特殊字符必须用双引号，例如 AS "中国籍受检总人数"
5. 日期过滤用 TO_DATE 函数
6. GROUP BY 包含所有非聚合字段
7. 只输出一条完整 SELECT SQL，不加任何解释

直接输出修复后的SQL："""

    def schema_fallback(reason: str) -> str:
        explicit_sql = explicit_structured_sql(
            question=question,
            start_date=start_date,
            end_date=end_date,
            allowed_tables=allowed_tables,
        )
        if explicit_sql:
            print(f"  [fallback] {reason}: using explicit structured SQL: {explicit_sql[:120]}...")
            return explicit_sql
        fallback_sql = _schema_only_fallback_sql(
            question=question,
            start_date=start_date,
            end_date=end_date,
            allowed_tables=allowed_tables,
            join_hint=join_hint,
        )
        if fallback_sql:
            print(f"  [fallback] {reason}: {fallback_sql[:120]}...")
        return fallback_sql

    try:
        txt = ollama_chat(prompt, temperature=0.3, max_tokens=2048, timeout=180)
        fixed_sql = extract_sql(txt)
        if not fixed_sql:
            print(f"  ⚠️ 最终修复 LLM 未返回 SQL")
            fixed_sql = schema_fallback("LLM no SQL, using Schema-only SQL")
            if not fixed_sql:
                return last_sql

        print(f"  📄 最终修复 SQL: {fixed_sql[:120]}...")

        is_valid_sql, validation_error = validate_sql_against_dictionary(fixed_sql, allowed_tables)
        if not is_valid_sql:
            print(f"  ❌ 最终修复 Schema 校验失败: {validation_error}")
            fixed_sql = schema_fallback("schema validation failed, using Schema-only SQL")
            if not fixed_sql:
                return last_sql
            is_valid_sql, validation_error = validate_sql_against_dictionary(fixed_sql, allowed_tables)
            if not is_valid_sql:
                print(f"  ❌ Schema-only SQL 校验失败: {validation_error}")
                return last_sql

        # 验证修复后的 SQL
        try:
            conn = get_conn_from_session()
            cur = conn.cursor()
            cur.execute(f"SELECT * FROM ({fixed_sql}) WHERE ROWNUM <= 1")
            cur.fetchall()
            conn.close()
            print(f"  ✅ 最终修复 SQL 执行成功")
            return fixed_sql
        except Exception as e:
            conn.close() if 'conn' in dir() else None
            print(f"  ❌ 最终修复 SQL 仍有错误: {str(e)[:200]}")
            fallback_sql = schema_fallback("execution failed, using Schema-only SQL")
            if fallback_sql and fallback_sql != fixed_sql:
                return fallback_sql
            print(f"  ↩️ 返回最终修复后的 SQL（供用户参考）")
            return fixed_sql  # 即使执行失败，也返回修复后的版本（比原始更好）
    except Exception as e:
        print(f"  ❌ 最终修复调用失败: {e}")
        return last_sql


def generate_with_repair(
    question: str,
    start_date: str,
    end_date: str,
    plan: Dict[str, Any],
    schema_ctx: str,
    allowed_tables: List[str],
    join_hint: str = "",   # ★ 新增：JOIN关系提示
    code_hint: str = "",
    rounds: int = 5
) -> str:
    """
    多表查询核心循环：Generate → Execute → LLM Self-Repair。
    ★ 修改要点：
      1. history[] 累积所有历史失败记录，每轮全量传给 LLM
      2. 重复SQL检测，连续重复时大幅提高 temperature
      3. temperature 策略更激进：0.0→0.5→0.7→0.85→0.95
      4. join_hint 每轮都传入 generate_multi_sql
      5. 验证改用 ROWNUM <= 1
    """
    last_sql = ""
    # ★ 新增：历史记录列表 + 重复检测
    history: List[Dict[str, str]] = []  # [{"round":1,"sql":"...","error":"..."}]
    seen_sqls: Set[str] = set()
    consecutive_duplicates = 0
    banned_tables: Set[str] = set()  # ★ 新增：记录确认不存在的表

    explicit_sql = explicit_structured_sql(
        question=question,
        start_date=start_date,
        end_date=end_date,
        allowed_tables=allowed_tables,
    )
    if explicit_sql:
        try:
            conn = get_conn_from_session()
            cur = conn.cursor()
            cur.execute(f"SELECT * FROM ({explicit_sql}) WHERE ROWNUM <= 1")
            cur.fetchall()
            conn.close()
            print(f"  [explicit-template] SQL execution probe succeeded: {explicit_sql[:120]}...")
            return explicit_sql
        except Exception as e:
            conn.close() if 'conn' in dir() else None
            print(f"  [explicit-template] skipped after probe failure: {str(e)[:160]}")

    for i in range(rounds):
        print(f"\n🔄 第 {i+1}/{rounds} 轮生成")

        # ★ 修改：temperature 更激进的递增策略
        temp_map = {0: 0.0, 1: 0.5, 2: 0.7, 3: 0.85}
        temp = temp_map.get(i, min(0.9 + (i - 4) * 0.05, 1.0))
        temp = min(temp + 0.15 * consecutive_duplicates, 1.0)

        # ★ 修改：把所有历史失败拼成完整 history 字符串传给 LLM
        history_text = ""
        if history:
            parts = []
            parts.append("=" * 50)
            parts.append(f"⚠️ 前 {len(history)} 轮均失败，请仔细分析所有错误后生成全新SQL")
            parts.append("=" * 50)
            for h in history:
                parts.append(f"\n--- 第 {h['round']} 轮 ---")
                parts.append(f"失败SQL:\n{h['sql']}")
                parts.append(f"错误原因: {h['error']}")
            parts.append("\n" + "=" * 50)
            parts.append("修复要求：")
            parts.append("1. 逐一分析上面每轮的错误原因")
            parts.append("2. 绝对不能重复出现过的SQL写法")
            parts.append("3. 只使用表结构中明确存在的字段")
            parts.append("4. 生成一条语法完全正确的新SQL")
            if banned_tables:
                parts.append(f"5. 【严禁】使用以下表（数据库中不存在）：{', '.join(banned_tables)}")
            parts.append("=" * 50)
            history_text = "\n".join(parts)

        sql = generate_multi_sql(
            question, start_date, end_date, plan, schema_ctx,
            join_hint=join_hint,
            code_hint=code_hint,
            history=history_text,
            temperature=temp
        )

        if not sql:
            print(f"  ❌ SQL 为空")
            history.append({
                "round": i + 1,
                "sql":   "(空)",
                "error": "LLM 未生成任何SQL"
            })
            consecutive_duplicates = 0
            continue

        # ★ 修改：重复 SQL 检测 — 不再 continue，而是强制换思路重试
        if sql in seen_sqls:
            consecutive_duplicates += 1
            print(f"  ⚠️ 生成了重复SQL（连续第 {consecutive_duplicates} 次），强制换思路重试")
            # 注入强制换思路指令，立刻以高温重新生成
            force_history = history_text + (
                f"\n\n🚨 紧急：刚才再次生成了完全相同的SQL！"
                f"必须换一种完全不同的写法，例如："
                f"换用其他表、改变JOIN方式、或用子查询代替JOIN。"
                f"当前SQL已失败：{sql[:200]}"
            )
            new_sql = generate_multi_sql(
                question, start_date, end_date, plan, schema_ctx,
                join_hint=join_hint,
                code_hint=code_hint,
                history=force_history,
                temperature=min(0.7 + 0.1 * consecutive_duplicates, 1.0)
            )
            if new_sql and new_sql not in seen_sqls:
                sql = new_sql
                consecutive_duplicates = 0
                print(f"  🔁 强制重试后得到新SQL: {sql[:80]}...")
            else:
                history.append({
                    "round": i + 1,
                    "sql":   sql,
                    "error": "与之前某轮SQL完全相同，必须换一种完全不同的写法"
                })
                continue
        else:
            consecutive_duplicates = 0

        seen_sqls.add(sql)
        last_sql = sql
        print(f"  📄 SQL: {sql[:120]}...")

        is_valid_sql, validation_error = validate_sql_against_dictionary(sql, allowed_tables)
        if not is_valid_sql:
            print(f"  ❌ Schema 校验失败: {validation_error}")
            history.append({
                "round": i + 1,
                "sql":   sql,
                "error": f"Schema validation failed before execution: {validation_error}"
            })
            continue

        try:
            conn = get_conn_from_session()
            try:
                cur = conn.cursor()
                # ★ 修改：改用 ROWNUM <= 1
                cur.execute(f"SELECT * FROM ({sql}) WHERE ROWNUM <= 1")
                cur.fetchall()
                conn.close()
                print(f"  ✅ 第 {i+1} 轮 SQL 执行成功")
                return sql
            except Exception as e:
                conn.close()
                oracle_error = str(e)
                print(f"  ❌ Oracle 错误: {oracle_error[:200]}")
                # ★ 新增：ORA-00942 时提取实际不存在的表名，加入禁用列表
                if "ORA-00942" in oracle_error:
                    used_tables = re.findall(
                        r'\bFROM\s+(\w+)|\bJOIN\s+(\w+)', sql, re.IGNORECASE
                    )
                    used_flat = [t for pair in used_tables for t in pair if t]
                    # 把 SQL 中用到的、不在 allowed_tables 中的表标记为 banned
                    allowed_upper = {t.upper() for t in allowed_tables}
                    for t in used_flat:
                        if t.upper() not in allowed_upper:
                            banned_tables.add(t.upper())
                            print(f"  🚫 标记不存在的表: {t}")
                    # 如果全部在 allowed 里，说明 allowed_tables 本身有问题，也记录
                    if not banned_tables:
                        for t in used_flat:
                            banned_tables.add(t.upper())
                        print(f"  🚫 标记可疑表（ORA-00942）: {banned_tables}")
                error_analysis = _analyze_oracle_error(
                    oracle_error, ", ".join(allowed_tables[:10]), set(), sql
                )
                history.append({
                    "round": i + 1,
                    "sql":   sql,
                    "error": f"{oracle_error} → 分析：{error_analysis}"
                })
        except Exception as e:
            print(f"  ⚠️ 连接异常: {str(e)}")
            history.append({
                "round": i + 1,
                "sql":   sql,
                "error": f"连接异常: {str(e)}"
            })

    print(f"  ⚠️ {rounds} 轮后仍未成功，进入最终兜底修复...")
    last_sql = _final_repair(
        question=question,
        start_date=start_date,
        end_date=end_date,
        last_sql=last_sql,
        history=history,
        schema_ctx=schema_ctx,
        join_hint=join_hint,
        code_hint=code_hint,
        allowed_tables=allowed_tables,
        banned_tables=banned_tables,
    )
    return last_sql

# ============================================================  
# ⑫ 单表查询 SQL 生成（简化版，不需要 Plan）+ 迭代纠错  
# ============================================================  
def generate_single_table_sql(
    question: str,
    table_name: str,
    start_date: str,
    end_date: str,
    schema_ctx: str,
    code_hint: str = "",
    feedback: str = "",
    temperature: float = 0.0   # ★ 新增：支持纠错轮次调温
) -> str:
    code_section    = f"\n{code_hint}\n" if code_hint else ""
    feedback_section = f"\n{feedback}\n" if feedback else ""

    table_cols     = get_table_columns(table_name)
    has_isnormal   = "ISNORMAL"   in table_cols
    has_ischinnese = "ISCHINNESE" in table_cols

    business_rules = ""
    if has_isnormal or has_ischinnese:
        business_rules = "业务口径（如果使用以下字段，必须严格遵守）：\n"
        if has_isnormal:
            business_rules += "- ISNORMAL字段：'1'=正常，'0'=异常\n"
        if has_ischinnese:
            business_rules += "- ISCHINNESE字段：'1'=中国人(中宾)，'0'=外国人(外宾)\n"

    is_simple_query = any(keyword in question for keyword in [
        "检索", "查询", "查看", "显示", "列出", "所有", "全部"
    ]) and not any(keyword in question for keyword in [
        "统计", "计数", "求和", "平均", "最大", "最小", "分组", "汇总", "多少"
    ])

    if is_simple_query:
        select_instruction = """
5. **重要**：如果用户只是简单检索/查看表数据（没有要求统计、分组等），请：
   - 使用 SELECT * 返回所有列
   - 不要给列添加中文别名（不要用 AS "中文名"）
   - 保持列名为原始英文字段名
   - 只在 WHERE 子句中添加必要的过滤条件"""
    else:
        select_instruction = """
5. 如果用户要求统计、分组、聚合等复杂查询，请：
   - 明确列出需要的字段
   - 可以适当添加中文别名便于理解
   - 使用聚合函数（COUNT、SUM、AVG等）"""

    prompt = f"""你是 Oracle 11g SQL 专家。根据用户问题生成**单表查询** SQL。

用户问题：{question}
目标表：{table_name}
日期范围：{start_date} 到 {end_date}

{business_rules}
可用表结构：
{schema_ctx}
{code_section}

SQL生成规则（必须遵守）：
1. 这是**单表查询**，主表必须是 {table_name}
2. 只能 JOIN 编码表来获取中文名称（如果需要），不能 JOIN 其他业务表
3. 只输出一条 SELECT SQL，不要任何解释文字
4. **严格限制**：只能使用表结构中明确列出的字段，禁止使用任何未在表结构中出现的字段
{select_instruction}
6. Oracle 11g 不支持 LIMIT/FETCH FIRST，分页请用 ROWNUM
7. 条件计数用 SUM(CASE WHEN ... THEN 1 ELSE 0 END)
8. 日期过滤：字段 BETWEEN TO_DATE('{start_date}','YYYY-MM-DD') AND TO_DATE('{end_date}','YYYY-MM-DD')
9. 如果问题要求分组统计，必须在 SELECT 和 GROUP BY 中包含分组字段
10. 字符串值用单引号
11. 表别名用 a,b,c 依次命名
12. SQL 必须以 SELECT 开头，不能有分号结尾
13. 确保 SQL 语法完全正确，符合 Oracle 11g 规范
14. 列别名如果含中文或特殊字符（如 /），必须用双引号包裹，例如：AS "中国籍受检总人数"、AS "异常/阳性人数"
{feedback_section}
直接输出SQL："""

    print(f"  📤 发送 Prompt 到 LLM (长度: {len(prompt)} 字符, temperature={temperature})")
    try:
        # ★ 修改：使用传入的 temperature 参数
        txt = ollama_chat(prompt, model=OLLAMA_SQL_MODEL,
                          temperature=temperature, max_tokens=1500)
        print(f"  📥 收到 LLM 响应 (长度: {len(txt)} 字符)")
        print(f"  原始响应: {txt[:200]}...")
        sql = extract_sql(txt)
        print(f"  🔧 提取后的 SQL: {sql}")
        if sql and table_name.upper() not in sql.upper():
            print(f"  ⚠️ 警告：生成的 SQL 未包含主表 {table_name}")
        return sql
    except Exception as e:
        print(f"  ❌ LLM 调用失败: {str(e)}")
        traceback.print_exc()
        return ""


def generate_single_table_with_repair(
    question: str,
    table_name: str,
    start_date: str,
    end_date: str,
    schema_ctx: str,
    code_hint: str = "",
    rounds: int = 5
) -> str:
    """
    单表查询 SQL 生成 + 迭代纠错
    ★ 修改要点：
      1. history[] 累积所有历史失败记录，每轮 feedback 包含全部历史
      2. 重复 SQL 检测 + 连续重复时加大 temperature
      3. temperature 更激进：0.0→0.4→0.7→0.85→0.95
      4. 验证改用 ROWNUM <= 1（兼容性更好）
      5. 集中使用 _analyze_oracle_error 分析错误
    """
    last_sql = ""
    table_cols    = get_table_columns(table_name)
    valid_columns: Set[str] = set(table_cols.keys())
    if code_hint:
        for ct in expand_with_code_tables([table_name]):
            valid_columns.update(get_table_columns(ct).keys())

    # ★ 新增：历史记录列表 + 重复SQL检测
    history: List[Dict[str, str]] = []  # [{"round":1,"sql":"...","error":"..."}]
    seen_sqls: Set[str] = set()
    consecutive_duplicates = 0

    for i in range(rounds):
        print(f"\n🔄 第 {i+1}/{rounds} 轮生成")

        # ★ 修改：temperature 激进递增策略
        temp_map = {0: 0.0, 1: 0.4, 2: 0.7, 3: 0.85}
        temp = temp_map.get(i, min(0.9 + (i - 4) * 0.05, 1.0))
        # 连续重复时额外提高 temperature
        temp = min(temp + 0.1 * consecutive_duplicates, 1.0)

        # ★ 修改：把所有历史失败拼成完整 feedback 传给 LLM
        feedback = ""
        if history:
            parts = []
            parts.append("=" * 50)
            parts.append(f"⚠️ 前 {len(history)} 轮均失败，请仔细分析所有错误后生成全新的SQL")
            parts.append("=" * 50)
            for h in history:
                parts.append(f"\n--- 第 {h['round']} 轮 ---")
                parts.append(f"失败SQL:\n{h['sql']}")
                parts.append(f"错误原因: {h['error']}")
            parts.append("\n" + "=" * 50)
            parts.append("修复要求：")
            parts.append("1. 逐一分析上面每轮的错误原因")
            parts.append("2. 绝对不能重复出现过的SQL写法")
            parts.append("3. 只使用表结构中明确存在的字段")
            parts.append("4. 生成一条语法完全正确的新SQL")
            parts.append("=" * 50)
            feedback = "\n".join(parts)

        sql = generate_single_table_sql(
            question=question,
            table_name=table_name,
            start_date=start_date,
            end_date=end_date,
            schema_ctx=schema_ctx,
            code_hint=code_hint,
            feedback=feedback,
            temperature=temp   # ★ 修改：传入动态 temperature
        )

        last_sql = sql

        if not sql:
            print(f"  ❌ SQL 为空，准备重试")
            history.append({
                "round": i + 1,
                "sql":   "(空)",
                "error": "LLM 未生成任何SQL，请严格输出一条以SELECT开头的SQL语句"
            })
            consecutive_duplicates = 0
            continue

        # ★ 修改：重复 SQL 检测
        if sql in seen_sqls:
            consecutive_duplicates += 1
            print(f"  ⚠️ 生成了重复SQL（连续第 {consecutive_duplicates} 次），强制加大多样性")
            history.append({
                "round": i + 1,
                "sql":   sql,
                "error": "与之前某轮SQL完全相同，必须改变查询结构、换用不同的写法"
            })
            continue
        else:
            consecutive_duplicates = 0

        seen_sqls.add(sql)

        # ★ 修改：验证 SQL 用 ROWNUM <= 1（比 ROWNUM <= 0 兼容性更好）
        print(f"  🔍 验证 SQL（ROWNUM <= 1）...")
        try:
            conn = get_conn_from_session()
            try:
                cur = conn.cursor()
                cur.execute(f"SELECT * FROM ({sql}) WHERE ROWNUM <= 1")
                cur.fetchall()
                conn.close()
                print(f"  ✅ 第 {i+1} 轮 SQL 验证通过")
                return sql
            except Exception as e:
                conn.close()
                oracle_error = str(e)
                print(f"  ❌ 第 {i+1} 轮 Oracle 错误: {oracle_error[:200]}")
                # ★ 修改：用统一错误分析函数，记录到历史
                error_analysis = _analyze_oracle_error(
                    oracle_error, table_name, valid_columns, sql
                )
                history.append({
                    "round": i + 1,
                    "sql":   sql,
                    "error": f"{oracle_error} → 分析：{error_analysis}"
                })
        except Exception as e:
            print(f"  ⚠️ 连接异常: {str(e)}")
            history.append({
                "round": i + 1,
                "sql":   sql,
                "error": f"数据库连接异常: {str(e)}"
            })

    print(f"  ⚠️ {rounds} 轮后仍有问题，返回最后生成的 SQL")
    return last_sql

def join_hints_text_for_tables(tables: List[str]) -> str:
    """
    ★ 新增：从 SCHEMA_INDEX.rel_entries 中提取候选表之间的 JOIN 关系，
    生成明确的 JOIN 提示文本给 LLM，减少 LLM 猜测 JOIN 条件。
    """
    table_set = set(t.upper() for t in tables)
    hints = []
    seen  = set()
    for entry in SCHEMA_INDEX.rel_entries:
        left  = entry.get("left",   "").upper()
        right = entry.get("right",  "").upper()
        col   = entry.get("column", "")
        if left in table_set and right in table_set:
            key = f"{left}-{right}-{col}"
            if key not in seen:
                seen.add(key)
                hints.append(f"  {left}.{col} = {right}.{col}")
    if not hints:
        return ""
    return "【已知 JOIN 关系（优先使用以下连表条件）】\n" + "\n".join(hints[:15])

def _analyze_oracle_error(
    error_msg: str,
    table_name: str,
    valid_columns: Set[str],
    sql: str
) -> str:
    """
    ★ 新增：集中处理 Oracle 错误分析，供单表/多表纠错共用。
    返回人类可读的错误原因和修复建议。
    """
    # ORA-00904: 标识符无效（字段不存在）
    if "ORA-00904" in error_msg:
        field_match = re.search(r'"([^"]+)"', error_msg)
        if field_match:
            missing_field = field_match.group(1)
            sample = sorted(list(valid_columns))[:20]
            return (
                f"字段 [{missing_field}] 不存在于表 {table_name}。"
                f"请从以下字段中选择：{', '.join(sample)}"
            )
        return f"存在不合法的标识符（字段名或表名错误）"

    # ORA-00933: SQL命令未正确结束
    elif "ORA-00933" in error_msg:
        return (
            "SQL语法错误（ORA-00933）：可能原因：多余逗号、括号不匹配、"
            "SELECT末尾多余的FROM、或WHERE子句语法有误"
        )

    # ORA-00979: 不是GROUP BY表达式
    elif "ORA-00979" in error_msg:
        return (
            "GROUP BY错误（ORA-00979）：SELECT中的非聚合字段必须全部出现在GROUP BY子句中。"
            "请检查SELECT列表，把所有非COUNT/SUM/AVG/MAX/MIN的字段加入GROUP BY"
        )

    # ORA-00942: 表或视图不存在
    elif "ORA-00942" in error_msg:
        # 尝试从 SQL 中提取实际使用的表名，帮助 LLM 定位问题
        sql_tables = re.findall(r'\bFROM\s+(\w+)|\bJOIN\s+(\w+)', sql, re.IGNORECASE) if sql else []
        used_tables = [t for pair in sql_tables for t in pair if t]
        bad_tables = [t for t in used_tables if t.upper() not in (table_name or "").upper()]
        hint = f"（SQL中使用了这些表: {', '.join(used_tables)}）" if used_tables else ""
        return (
            f"表或视图不存在（ORA-00942）：SQL引用了数据库中不存在的表。{hint} "
            f"请仔细核对表结构，只使用 Schema 中明确列出的表名，不要猜测或缩写表名。"
            f"例如不要用 T_L_OPERATION，正确写法是 TL_OPERATION。"
        )

    # ORA-01722: 无效数字
    elif "ORA-01722" in error_msg:
        return (
            "数据类型错误（ORA-01722）：对字符串类型字段做了数值运算，"
            "或WHERE条件中数字字段与字符串比较时缺少TO_NUMBER转换"
        )

    # ORA-01843: 月份无效
    elif "ORA-01843" in error_msg:
        return (
            "日期格式错误（ORA-01843）：请使用 TO_DATE('日期','YYYY-MM-DD') 格式，"
            "不要直接写日期字符串"
        )

    # ORA-00907: 缺少右括号
    elif "ORA-00907" in error_msg:
        return "括号不匹配（ORA-00907）：请检查所有括号是否成对，子查询括号是否正确闭合"

    # ORA-01747: user.table.column 说明无效
    elif "ORA-01747" in error_msg:
        return "列引用格式错误（ORA-01747）：请检查 a.column_name 的写法，不要出现三层引用"

    # 其他
    else:
        return (
            "请仔细检查：①字段名是否存在于表结构；②GROUP BY是否包含所有非聚合列；"
            "③日期函数是否用TO_DATE；④括号是否匹配；⑤别名引用是否正确"
        )


# ============================================================
# ⑮ Flask API 路由
# ============================================================

# ============================================================
# 表名智能匹配（精确匹配 + 相似度匹配）
# ============================================================
def find_best_matching_table(user_input: str) -> Optional[Tuple[str, float, str]]:
    """
    智能表名匹配：精确匹配优先，然后相似度匹配。
    返回: (表名, 相似度分数, 匹配类型) 或 None
    """
    if not user_input:
        return None

    user_input_upper = user_input.upper().strip()

    all_tables = {}
    all_tables.update(DATA_DICT.get("main_tables", {}))
    all_tables.update(DATA_DICT.get("code_tables", {}))

    if not all_tables:
        return None

    # Step 1: 精确匹配（英文表名）
    for tname in all_tables.keys():
        if tname.upper() == user_input_upper:
            print(f"  ✅ 精确匹配（英文）: {tname.upper()}")
            return (tname.upper(), 1.0, "exact_en")

    # Step 2: 精确匹配（中文表名）
    for tname, tinfo in all_tables.items():
        table_cn = (tinfo.get("table_cn") or "").strip()
        if table_cn and table_cn == user_input:
            print(f"  ✅ 精确匹配（中文）: {tname.upper()}")
            return (tname.upper(), 1.0, "exact_cn")

    # Step 3: 相似度匹配
    print(f"  ⚙️ 未找到精确匹配，开始相似度计算...")

    candidates = []
    for tname, tinfo in all_tables.items():
        table_cn   = tinfo.get("table_cn", "")
        short_desc = tinfo.get("short_description", "")
        text = f"{tname} {table_cn} {short_desc}".strip()
        candidates.append({
            "table_name": tname.upper(),
            "text": text
        })

    if not candidates:
        return None

    try:
        texts = [user_input] + [c["text"] for c in candidates]
        embeddings = ollama_embed(texts, show_progress=False, use_cache=True)

        if embeddings.size == 0 or len(embeddings) < len(texts):
            print(f"  ⚠️ Embedding 生成失败")
            return None

        faiss.normalize_L2(embeddings)
        query_vec     = embeddings[0:1]
        candidate_vecs = embeddings[1:]

        # ★ 修改：统一用 CPU 内积，去掉不稳定的 GPU 分支（GPU加速已在索引构建时用）
        similarities = np.dot(candidate_vecs, query_vec.T).flatten()

        best_idx   = int(np.argmax(similarities))
        best_score = float(similarities[best_idx])
        best_table = candidates[best_idx]["table_name"]

        print(f"  🎯 最佳匹配: {best_table} (分数: {best_score:.4f})")

        if best_score < 0.3:
            print(f"  ⚠️ 相似度过低（{best_score:.4f} < 0.3），拒绝匹配")
            return None

        return (best_table, best_score, "similar")

    except Exception as e:
        print(f"  ⚠️ 相似度计算失败: {e}")
        traceback.print_exc()
        return None


# ---------- 单表查询 ----------
@app.route("/api/ask", methods=["POST"])
def api_ask():
    data       = request.json or {}
    question   = (data.get("question") or data.get("query") or "").strip()
    table_name = (data.get("table_name") or data.get("selected_table") or "").strip()
    start_date = (data.get("start_date") or data.get("startDate") or "2021-01-01").strip()
    end_date   = (data.get("end_date")   or data.get("endDate")   or
                  datetime.now().strftime("%Y-%m-%d")).strip()
    execute    = bool(data.get("execute", True))

    if not question:
        return jsonify({"ok": False, "success": False, "error": "question 不能为空"}), 200

    # 如果没有指定 table_name，从问题中自动识别
    if not table_name:
        print(f"📋 未指定表名，从问题中自动识别")
        match_result = find_best_matching_table(question)
        if match_result is None:
            return jsonify({
                "ok": False, "success": False,
                "error": "无法从问题中识别表名，请明确指定 table_name"
            }), 200
        table_name = match_result[0]
        print(f"  ✅ 自动识别到表: {table_name}")

    try:
        t0 = time.time()
        print("\n" + "="*60)
        print("🔍 【单表查询】开始处理")
        print("="*60)
        print(f"📝 用户问题: {question}")
        print(f"📅 日期范围: {start_date} ~ {end_date}")
        print(f"📋 输入表名: {table_name}")

        # Step 1: 智能表名匹配
        print(f"\n🔎 Step 1: 智能表名匹配")
        match_result = find_best_matching_table(table_name)

        if match_result is None:
            return jsonify({
                "ok": False, "success": False,
                "error": f"未找到与 [{table_name}] 匹配的表，请检查表名是否正确"
            }), 200

        target_table, match_score, match_type = match_result
        print(f"✅ 匹配成功: {target_table} (分数: {match_score:.4f}, 类型: {match_type})")

        if get_table_info(target_table) is None:
            return jsonify({
                "ok": False, "success": False,
                "error": f"表 {target_table} 不存在于数据字典中"
            }), 200

        # Step 2: 检查是否需要编码表替换
        print(f"\n🔎 Step 2: 检查是否需要编码表替换")
        need_name = any(k in question for k in [
            "名称","名字","中文","含义","描述","字典","对应","显示",
            "叫什么","是什么","部门名","科室名","类型名"
        ])
        print(f"  需要中文名称: {need_name}")

        code_hint       = ""
        extra_code_tables = []
        if need_name:
            code_hint = get_code_table_hint([target_table])
            if code_hint:
                extra_code_tables = expand_with_code_tables([target_table])
                print(f"  ✅ 找到 {len(extra_code_tables)} 个相关编码表: {extra_code_tables}")
            else:
                print(f"  ⚠️ 未找到相关编码表")

        # Step 3: 构建 Schema 上下文
        all_tables = [target_table] + extra_code_tables
        print(f"\n🔎 Step 3: 构建 Schema 上下文，涉及表: {all_tables}")
        schema_ctx = schema_text_for_tables(all_tables)
        print(f"  Schema 长度: {len(schema_ctx)} 字符")

        # Step 4: 生成 SQL（迭代纠错）
        print(f"\n🔎 Step 4: 生成 SQL（迭代纠错，最多5轮）")
        sql = generate_single_table_with_repair(
            question=question,
            table_name=target_table,
            start_date=start_date,
            end_date=end_date,
            schema_ctx=schema_ctx,
            code_hint=code_hint,
            rounds=5
        )
        print(f"\n📄 生成的 SQL: {sql}")

        if not sql:
            return jsonify({
                "ok": False, "success": False,
                "error": "未能生成有效的 SQL"
            }), 200

        # Step 5: 构建响应
        resp: Dict[str, Any] = {
            "ok": True,
            "success": True,
            "time_cost": round(time.time() - t0, 2),
            "main_table": {
                "name":    target_table,
                "name_cn": get_table_info(target_table).get("table_cn", "")
                           if get_table_info(target_table) else "",
                "sql":     sql,
                "columns": [],
                "rows":    []
            },
            "code_tables": {},
            "debug": {
                "matched_table": target_table,
                "match_score":   round(match_score, 4),
                "match_type":    match_type,
            }
        }

        if execute:
            try:
                print(f"  正在执行 SQL...")
                cols, rows = run_sql(sql)
                print(f"  ✅ 执行成功: {len(rows)} 行, {len(cols)} 列")
                resp["main_table"]["columns"] = cols
                resp["main_table"]["rows"]    = rows
            except Exception as e:
                print(f"  ❌ SQL 执行失败: {str(e)}")
                resp["ok"]      = False
                resp["success"] = False
                resp["error"]   = str(e)

        print("="*60)
        print("✅ 【单表查询】处理完成")
        print("="*60 + "\n")
        return jsonify(resp), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "success": False, "error": str(e)}), 200


# ---------- 多表查询 ----------
@app.route("/api/ask_multi", methods=["POST"])
def api_ask_multi():
    data            = request.json or {}
    question        = (data.get("question") or data.get("query") or "").strip()
    selected_tables = data.get("selected_tables") or data.get("tables") or []
    start_date      = (data.get("start_date") or data.get("startDate") or "2021-01-01").strip()
    end_date        = (data.get("end_date")   or data.get("endDate")   or
                       datetime.now().strftime("%Y-%m-%d")).strip()
    execute         = bool(data.get("execute", True))

    if not question:
        return jsonify({"ok": False, "success": False, "error": "question 不能为空"}), 200

    try:
        t0 = time.time()
        print("\n" + "="*60)
        print("🔍 【多表查询】开始处理")
        print("="*60)
        print(f"📝 用户问题: {question}")
        print(f"📅 日期范围: {start_date} ~ {end_date}")
        print(f"📋 选择的表: {selected_tables if selected_tables else '(自动检索)'}")

        # Step 1: Retrieve
        print(f"\n🔎 Step 1: Retrieve（检索相关表）")
        retrieve   = retrieve_schema(
            question,
            user_selected_tables=selected_tables if selected_tables else None
        )
        candidates = retrieve["candidate_tables"]
        print(f"  候选表: {candidates}")

        if not candidates:
            return jsonify({
                "ok": False, "success": False,
                "error": "未检索到相关表，请检查问题描述或手动选择表",
                "debug": _debug_retrieve(retrieve)
            }), 200

        main_tables = [t for t in candidates if not is_code_table(t)]
        print(f"  业务表: {main_tables}")

        if not main_tables:
            return jsonify({
                "ok": False, "success": False,
                "error": "未检索到业务表，多表查询至少需要一个业务表",
                "debug": _debug_retrieve(retrieve)
            }), 200

        # Step 2: 编码表扩展
        print(f"\n🔎 Step 2: 编码表扩展")
        need_name = retrieve.get("need_name", False)
        print(f"  需要中文名称: {need_name}")
        code_hint = ""

        if need_name:
            code_hint = get_code_table_hint(main_tables)
            if code_hint:
                extra_code = expand_with_code_tables(main_tables)
                print(f"  ✅ 找到 {len(extra_code)} 个相关编码表: {extra_code}")
                for t in extra_code:
                    if t not in candidates:
                        candidates.append(t)
                candidates = candidates[:16]

        # Step 3: 构建 Schema 上下文
        print(f"\n🔎 Step 3: 构建 Schema 上下文，涉及表: {candidates}")
        schema_ctx = schema_text_for_tables(candidates)
        schema_ctx += schema_kg_context_for_prompt(retrieve)
        print(f"  Schema 长度: {len(schema_ctx)} 字符")

        # ★ 修改：用 join_hints_text_for_tables（只传 candidates，函数内部查 SCHEMA_INDEX）
        join_hint = join_hints_text_for_tables(candidates)
        if join_hint:
            print(f"  ✅ JOIN 关系提示: {len(join_hint)} 字符")
        else:
            print(f"  ℹ️ 未找到已知 JOIN 关系")

        # Step 4: Plan
        print(f"\n🔎 Step 4: Plan（生成查询计划）")
        plan = make_plan(question, start_date, end_date, schema_ctx, code_hint)
        print(f"  计划: {json.dumps(plan, ensure_ascii=False)}")

        # ★ 修改：Plan 指定的表与 RAG 候选表合并校验，防止 plan 选错表
        plan_tables_raw = [
            t.upper() for t in plan.get("needed_tables", [])
            if isinstance(t, str) and get_table_info(t)
        ]
        if plan_tables_raw:
            # 合并：plan表优先 + RAG候选表（保序去重，保证不丢表）
            merged = list(dict.fromkeys(plan_tables_raw + candidates))
            # 编码表单独保留（防止被截断）
            for t in candidates:
                if is_code_table(t) and t not in merged:
                    merged.append(t)
            merged     = merged[:16]
            candidates = merged
            schema_ctx = schema_text_for_tables(candidates)
            schema_ctx += schema_kg_context_for_prompt(retrieve)
            # ★ 合并后重新生成 JOIN 提示（候选表变了）
            join_hint  = join_hints_text_for_tables(candidates)
            print(f"  合并后最终候选表: {candidates}")

        # Step 5: Generate + Repair
        print(f"\n🔎 Step 5: Generate + Repair（迭代纠错，最多5轮）")
        sql = generate_with_repair(
            question, start_date, end_date, plan, schema_ctx,
            allowed_tables=candidates,
            join_hint=join_hint,    # ★ 新增：传入 JOIN 提示
            code_hint=code_hint,
            rounds=5
        )
        print(f"\n📄 生成的 SQL: {sql}")

        if not sql:
            return jsonify({
                "ok": False, "success": False,
                "error": "未能生成有效的 SQL",
                "debug": {
                    "candidates": candidates,
                    "plan":       plan,
                    "retrieve":   _debug_retrieve(retrieve)
                }
            }), 200

        # Step 6: 构建响应
        main_table_name = main_tables[0] if main_tables else candidates[0]
        main_table_info = get_table_info(main_table_name)

        resp: Dict[str, Any] = {
            "ok":      True,
            "success": True,
            "time_cost": round(time.time() - t0, 2),
            "main_table": {
                "name":    main_table_name,
                "name_cn": main_table_info.get("table_cn", "") if main_table_info else "",
                "sql":     sql,
                "columns": [],
                "rows":    []
            },
            "code_tables": {},
            "debug": {
                "candidate_tables": candidates,
                "main_tables":      main_tables,
                "plan":             plan,
                "join_hint_lines":  len(join_hint.splitlines()) if join_hint else 0,
                "code_hint_lines":  len(code_hint.splitlines()) if code_hint else 0,
                "retrieve":         _debug_retrieve(retrieve)
            }
        }

        if execute:
            try:
                print(f"  正在执行 SQL...")
                cols, rows = run_sql(sql)
                print(f"  ✅ 执行成功: {len(rows)} 行, {len(cols)} 列")
                resp["main_table"]["columns"] = cols
                resp["main_table"]["rows"]    = rows
            except Exception as e:
                print(f"  ❌ SQL 执行失败: {str(e)}")
                resp["ok"]      = False
                resp["success"] = False
                resp["error"]   = str(e)

        print("="*60)
        print("✅ 【多表查询】处理完成")
        print("="*60 + "\n")
        return jsonify(resp), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "success": False, "error": str(e)}), 200


# ---------- Debug 辅助 ----------
def _debug_retrieve(retrieve: Dict[str, Any]) -> Dict[str, Any]:
    def top(entries, n=8):
        out = []
        for score, ent in entries[:n]:
            item = dict(ent)
            item["score"] = round(score, 4)
            out.append(item)
        return out
    return {
        "tables":    top(retrieve.get("tables_hits", []),  6),
        "columns":   top(retrieve.get("cols_hits",   []), 10),
        "relations": top(retrieve.get("rels_hits",   []), 10),
        "rag_backend": retrieve.get("rag_backend", "unknown"),
        "rag_context_chars": len(retrieve.get("rag_context", "") or ""),
        "rag_error": retrieve.get("rag_error", ""),
    }


# ---------- 数据库连接/断开 ----------
@app.route("/api/connect", methods=["POST"])
def api_connect():
    data = request.json or {}
    print("=" * 60)
    print("📥 收到连接请求")

    db_user    = (data.get("DB_USER")    or data.get("username")  or
                  data.get("user")       or data.get("dbUser")    or "").strip()
    db_password= (data.get("DB_PASSWORD")or data.get("password")  or
                  data.get("pwd")        or data.get("dbPassword")or "").strip()
    db_host    = (data.get("DB_HOST")    or data.get("host")       or
                  data.get("dbHost")     or DEFAULT_DB_CONFIG["DB_HOST"]).strip()
    db_port    = (data.get("DB_PORT")    or data.get("port")       or
                  data.get("dbPort")     or DEFAULT_DB_CONFIG["DB_PORT"])
    db_service = (data.get("DB_SERVICE_NAME") or data.get("service_name") or
                  data.get("serviceName")     or data.get("dbService")    or
                  DEFAULT_DB_CONFIG["DB_SERVICE_NAME"]).strip()

    cfg = {
        "DB_USER":         db_user,
        "DB_PASSWORD":     db_password,
        "DB_HOST":         db_host,
        "DB_PORT":         int(db_port),
        "DB_SERVICE_NAME": db_service,
    }

    print(f"  用户名: {cfg['DB_USER']}")
    print(f"  主机:   {cfg['DB_HOST']}:{cfg['DB_PORT']}/{cfg['DB_SERVICE_NAME']}")
    print("=" * 60)

    if not cfg["DB_USER"]:
        return jsonify({"ok": False, "success": False,
                        "message": "用户名不能为空", "error": "DB_USER is required"}), 400
    if not cfg["DB_PASSWORD"]:
        return jsonify({"ok": False, "success": False,
                        "message": "密码不能为空", "error": "DB_PASSWORD is required"}), 400

    try:
        conn = oracledb.connect(
            user=cfg["DB_USER"], password=cfg["DB_PASSWORD"], dsn=make_dsn(cfg)
        )
        conn.close()
        session["db_config"] = cfg
        print("✅ 数据库连接成功")
        return jsonify({
            "ok": True, "success": True,
            "message": "连接成功",
            "db_config": {k: v if k != "DB_PASSWORD" else "***" for k, v in cfg.items()}
        }), 200
    except Exception as e:
        print(f"❌ 连接失败: {str(e)}")
        traceback.print_exc()
        return jsonify({
            "ok": False, "success": False,
            "message": "连接失败", "error": str(e),
            "db_config": {k: v if k != "DB_PASSWORD" else "***" for k, v in cfg.items()}
        }), 400


@app.route("/api/disconnect", methods=["POST"])
def api_disconnect():
    session.pop("db_config", None)
    return jsonify({"success": True, "message": "已清除连接信息"}), 200


# ---------- 获取所有表列表 ----------
@app.route("/api/tables", methods=["GET"])
def api_tables():
    result = []
    for src_key in ["main_tables", "code_tables"]:
        for tname, tinfo in DATA_DICT.get(src_key, {}).items():
            result.append({
                "table_name":        tname,
                "table_cn":          tinfo.get("table_cn", ""),
                "is_code_table":     tinfo.get("is_code_table", src_key == "code_tables"),
                "short_description": tinfo.get("short_description", "")
            })
    result.sort(key=lambda x: x["table_name"])
    return jsonify({"tables": result, "total": len(result)}), 200


# ---------- 获取单张表的列信息 ----------
@app.route("/api/table_columns", methods=["POST"])
def api_table_columns():
    data       = request.json or {}
    table_name = (data.get("table_name") or "").strip().upper()
    if not table_name:
        return jsonify({"error": "table_name 不能为空"}), 400
    info = get_table_info(table_name)
    if not info:
        return jsonify({"error": f"表 {table_name} 不在数据字典中"}), 404

    cols_raw = info.get("columns", {})
    columns  = []

    if isinstance(cols_raw, list):
        for c in cols_raw:
            if isinstance(c, dict):
                columns.append({
                    "name":      c.get("name", ""),
                    "cn":        c.get("cn", ""),
                    "data_type": c.get("data_type", ""),
                    "type_str":  c.get("type_str", ""),
                    "desc":      c.get("full_description", "")
                })
    elif isinstance(cols_raw, dict):
        for _, c in cols_raw.items():
            if isinstance(c, dict):
                columns.append({
                    "name":      c.get("name", ""),
                    "cn":        c.get("cn", ""),
                    "data_type": c.get("data_type", ""),
                    "type_str":  c.get("type_str", ""),
                    "desc":      c.get("full_description", "")
                })

    code_mappings = []
    for col in columns:
        cn = col["name"].upper()
        for m in CODE_MAP.get(cn, [])[:3]:
            code_mappings.append({
                "main_col":   cn,
                "code_table": m["code_table"],
                "code_col":   m["code_col"],
                "name_col":   m["name_col"],
            })

    return jsonify({
        "table_name":    table_name,
        "table_cn":      info.get("table_cn", ""),
        "is_code_table": info.get("is_code_table", False),
        "columns":       columns,
        "code_mappings": code_mappings
    }), 200


# ---------- 直接执行 SQL ----------
@app.route("/api/execute_sql", methods=["POST"])
def api_execute_sql():
    data = request.json or {}
    sql  = (data.get("sql") or "").strip()
    if not sql:
        return jsonify({"success": False, "error": "sql 不能为空"}), 400
    su = sql.upper().strip()
    if not su.startswith("SELECT"):
        return jsonify({"success": False, "error": "只允许执行 SELECT 语句"}), 400
    for kw in ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "CREATE"]:
        if re.search(rf"\b{kw}\b", su):
            return jsonify({"success": False, "error": f"SQL 包含危险关键字: {kw}"}), 400
    try:
        cols, rows = run_sql(sql)
        return jsonify({
            "success":   True,
            "columns":   cols,
            "rows":      rows,
            "row_count": len(rows)
        }), 200
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 200


# ---------- 推荐相关表 ----------
@app.route("/api/recommend_tables", methods=["POST"])
def api_recommend_tables():
    data     = request.json or {}
    question = (data.get("question") or "").strip()
    top_k    = int(data.get("top_k", 10))
    if not question:
        return jsonify({"error": "question 不能为空"}), 400
    retrieve = retrieve_schema(question)
    result   = []
    for tname in retrieve["candidate_tables"][:top_k]:
        info = get_table_info(tname)
        result.append({
            "table_name":        tname,
            "table_cn":          info.get("table_cn", "") if info else "",
            "is_code_table":     is_code_table(tname),
            "short_description": info.get("short_description", "") if info else ""
        })
    return jsonify({"recommended_tables": result}), 200


# ---------- 获取可用模型列表 ----------
@app.route("/api/models", methods=["GET"])
def api_models():
    try:
        resp = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=8)
        if resp.status_code == 200:
            models = [m["name"] for m in resp.json().get("models", [])]
            return jsonify({"models": models}), 200
    except Exception:
        pass
    return jsonify({"models": [OLLAMA_SQL_MODEL]}), 200


# ---------- 健康检查 ----------
@app.route("/api/health", methods=["GET"])
def api_health():
    dd = DATA_DICT
    gpu_available = False
    try:
        if hasattr(faiss, 'StandardGpuResources'):
            gpu_available = True
    except Exception:
        pass

    fallback_index = getattr(SCHEMA_INDEX, "fallback_index", SCHEMA_INDEX)
    table_entries = getattr(fallback_index, "table_entries", [])
    col_entries = getattr(fallback_index, "col_entries", [])
    rel_entries = getattr(SCHEMA_INDEX, "rel_entries", getattr(fallback_index, "rel_entries", []))
    faiss_index = getattr(fallback_index, "table_index", None)

    return jsonify({
        "status":        "ok",
        "main_tables":   len(dd.get("main_tables", {})),
        "code_tables":   len(dd.get("code_tables", {})),
        "code_map_keys": len(CODE_MAP),
        "rag_backend":   getattr(SCHEMA_INDEX, "backend_name", "unknown"),
        "rag_error":     getattr(SCHEMA_INDEX, "last_error", ""),
        "faiss_built":   faiss_index is not None,
        "faiss_tables":  len(table_entries),
        "faiss_cols":    len(col_entries),
        "faiss_rels":    len(rel_entries),
        "ollama_url":    OLLAMA_BASE_URL,
        "default_model": OLLAMA_SQL_MODEL,
        "embedding_model": OLLAMA_EMBEDDING_MODEL,
        "strict_qwen3_vl_only": STRICT_QWEN3_VL_ONLY,
        "gpu_available": gpu_available,
        "cache_size":    len(_embedding_cache),
        "performance": {
            "parallel_embedding": True,
            "gpu_accelerated":    gpu_available,
            "cache_enabled":      True,
        }
    }), 200


# ---------- 清理缓存 ----------
@app.route("/api/clear_cache", methods=["POST"])
def api_clear_cache():
    global _embedding_cache
    cache_size = len(_embedding_cache)
    with _cache_lock:
        _embedding_cache.clear()
    return jsonify({
        "success": True,
        "message": f"已清理 {cache_size} 条缓存"
    }), 200


# ---------- 页面（保留兼容）----------
@app.route("/")
def index():
    return render_template("index.html") \
        if Path("templates/index.html").exists() else "OK"


@app.route("/qa")
def qa():
    return render_template("qa.html") \
        if Path("templates/qa.html").exists() else "QA OK"


# ============================================================
# ⑯ 启动
# ============================================================
if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("🚀 NL2SQL 智能体启动中（优化迭代纠错版）...")
    print("=" * 60)
    print(f"  Ollama URL:      {OLLAMA_BASE_URL}")
    print(f"  SQL 模型:         {OLLAMA_SQL_MODEL}")
    print(f"  Embedding 模型:  {OLLAMA_EMBEDDING_MODEL}")
    print(f"  数据字典:         {DATA_DICT_PATH}")
    print(f"  编码表映射字段数:  {len(CODE_MAP)}")
    print(f"  RAG 缓存目录:    {RAG_CACHE_DIR}")

    gpu_status = "❌ 不可用（使用CPU）"
    try:
        if hasattr(faiss, 'StandardGpuResources'):
            gpu_status = "✅ 可用（已启用GPU加速）"
    except Exception:
        pass
    print(f"  GPU 加速:        {gpu_status}")

    print("\n优化项：")
    print("  ✅ 迭代纠错：全量历史传递（所有失败轮次的SQL+错误）")
    print("  ✅ 重复SQL检测：连续重复时激进加大 temperature")
    print("  ✅ 单表纠错：同样使用历史累积机制")
    print("  ✅ 多表JOIN：RAG关系信息传入生成 Prompt")
    print("  ✅ Plan+RAG合并校验：防止 plan 选错表")
    print("  ✅ num_ctx 8192：避免长 prompt 被截断")
    print("  ✅ Oracle错误分析：_analyze_oracle_error 统一处理")

    print("\n接口列表:")
    print("  POST /api/connect          — 数据库连接")
    print("  POST /api/disconnect       — 断开连接")
    print("  POST /api/ask              — 单表查询")
    print("  POST /api/ask_multi        — 多表查询")
    print("  POST /api/execute_sql      — 直接执行 SQL")
    print("  GET  /api/tables           — 获取所有表列表")
    print("  POST /api/table_columns    — 获取表列信息+编码表映射")
    print("  POST /api/recommend_tables — 推荐相关表")
    print("  GET  /api/models           — 可用模型列表")
    print("  GET  /api/health           — 健康检查")
    print("  POST /api/clear_cache      — 清理缓存")
    print("=" * 60)

    app.run(host="0.0.0.0", port=5000, debug=False)
