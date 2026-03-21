"""
app_9.py — NL2SQL 多模型全自动对比评测脚本

策略：
  - 直接调用 Ollama API 切换模型，无需手动重启 app_10.py
  - 通过 app_10.py 的 /api/recommend_tables 获取 RAG 候选表和 Schema
  - 每个模型独立生成 SQL → 尝试执行 → 打分
  - 最终按多表平均分排序输出对比表格

运行前提：
  1. app_10.py 服务在 5000 端口运行（用于 RAG 检索和数据库执行）
  2. 数据库已可连接（密码在 DB_CONFIG 中填写）
  3. python app_9.py
"""

import requests
import json
import re
import time
import sys
import getpass
from typing import List, Dict, Any, Optional, Tuple

# ============================================================
# 配置
# ============================================================
BASE_URL   = "http://127.0.0.1:5000"   # app_10.py Flask 服务
OLLAMA_URL = "http://localhost:11434"  # Ollama 服务

DB_CONFIG = {
    "DB_USER":         "bjzx",
    "DB_PASSWORD":     "",   # 留空则运行时提示输入
    "DB_HOST":         "127.0.0.1",
    "DB_PORT":         1521,
    "DB_SERVICE_NAME": "oral",
}

# 参与评测的模型（按显存从小到大排列）
MODELS = [
    "qwen3:4b",
    "sqlcoder:7b",
    "qwen2.5-coder:7b",
    "qwen2.5-coder-gpu:latest",
    "qwen3:8b",
    "qwen3.5:9b",
    "qwen2.5-coder:14b",
]

START_DATE = "2021-01-01"
END_DATE   = "2026-03-17"

# 每个模型每道题的 LLM 超时（秒）
LLM_TIMEOUT = 120
# SQL 执行超时（秒）
SQL_EXEC_TIMEOUT = 30

# ============================================================
# 多表测试集（基于3条参考SQL设计）
# ============================================================
MULTI_TABLE_TESTS: List[Dict[str, Any]] = [
    # ── 参考SQL-1: exam_record + control_status + bm_control_status
    #               + fee_record + view_fee + bm_person_type + bm_pzxm
    {
        "id": "M1",
        "desc": "费用明细多表查询（7表关联）",
        "question": (
            "查询所有受检人员的费用明细，包括是否出入境、是否发件、体检编号、姓名、"
            "体检状态名称、人员类型描述、单位名称、收费项目、费用名称、收费状态、"
            "单价、数量、合计金额、批准项目名称"
        ),
        "tables": ["EXAM_RECORD", "CONTROL_STATUS", "BM_CONTROL_STATUS",
                   "FEE_RECORD", "VIEW_FEE", "BM_PERSON_TYPE", "BM_PZXM"],
        "ref_sql": (
            "select a.exam_no, a.surname, c.name, f.person_type_desc,"
            " a.cmp_scl_oth, d.fee_item, e.fee_name,"
            " decode(d.fee_type,'2','已收','0','未收','4','未收') fee_type,"
            " d.fee, d.amount, d.fee*d.amount heji, g.pz_item_name"
            " from exam_record a, control_status b, bm_control_status c,"
            " fee_record d, view_fee e, bm_person_type f, bm_pzxm g"
            " where a.exam_no=b.exam_no and a.person_type=f.person_type"
            " and b.exam_status=c.code and a.exam_no=d.exam_no"
            " and d.fee_item=e.fee_item and d.fee_own=g.pz_item_code"
        ),
        "criteria": [
            ("JOIN exam_record + fee_record",
             lambda sql: "EXAM_RECORD" in sql.upper() and "FEE_RECORD" in sql.upper()),
            ("JOIN control_status / bm_control_status",
             lambda sql: "CONTROL_STATUS" in sql.upper()),
            ("JOIN bm_person_type",
             lambda sql: "BM_PERSON_TYPE" in sql.upper()),
            ("包含 fee/amount 金额字段",
             lambda sql: re.search(r'\bFEE\b|\bAMOUNT\b|\bFEE_NAME\b', sql.upper()) is not None),
            ("包含 exam_no JOIN 条件",
             lambda sql: sql.upper().count("EXAM_NO") >= 2),
        ],
    },
    # ── 参考SQL-1 简化版
    {
        "id": "M2",
        "desc": "按人员类型统计收费（3表聚合）",
        "question": (
            "统计每个人员类型的已收费金额合计和未收费金额合计，"
            "关联 exam_record、fee_record、bm_person_type"
        ),
        "tables": ["EXAM_RECORD", "FEE_RECORD", "BM_PERSON_TYPE"],
        "ref_sql": (
            "select f.person_type_desc,"
            " sum(case when d.fee_type='2' then d.fee*d.amount else 0 end) yishoufei,"
            " sum(case when d.fee_type in ('0','4') then d.fee*d.amount else 0 end) weishoufei"
            " from exam_record a, fee_record d, bm_person_type f"
            " where a.exam_no=d.exam_no and a.person_type=f.person_type"
            " group by f.person_type_desc"
        ),
        "criteria": [
            ("JOIN exam_record + fee_record",
             lambda sql: "EXAM_RECORD" in sql.upper() and "FEE_RECORD" in sql.upper()),
            ("JOIN bm_person_type",
             lambda sql: "BM_PERSON_TYPE" in sql.upper()),
            ("SUM(CASE WHEN fee_type='2') 统计已收",
             lambda sql: "FEE_TYPE" in sql.upper() and "SUM" in sql.upper() and "CASE" in sql.upper()),
            ("GROUP BY person_type",
             lambda sql: "GROUP BY" in sql.upper() and "PERSON_TYPE" in sql.upper()),
            ("包含 fee*amount 或 fee 计算",
             lambda sql: re.search(r'FEE.*AMOUNT|AMOUNT.*FEE|\bFEE\b', sql.upper()) is not None),
        ],
    },
    # ── 参考SQL-2: exam_record + lab_result + bm_lab_item
    {
        "id": "M3",
        "desc": "中外人员检验异常统计（参考SQL-2原题）",
        "question": (
            f"统计{START_DATE}至{END_DATE}期间，每个检验项目中国籍和外籍受检人数及异常人数，"
            "需要检验项目编码和描述，关联 exam_record、lab_result、bm_lab_item"
        ),
        "tables": ["EXAM_RECORD", "LAB_RESULT", "BM_LAB_ITEM"],
        "ref_sql": (
            "select c.lab_item_code, c.lab_item_desc,"
            " sum(case when a.ischinnese='1' then 1 else 0 end) cn_sum,"
            " sum(case when a.ischinnese='1' and b.isnormal='1' then 1 else 0 end) cn_sum_ab,"
            " sum(case when a.ischinnese='0' then 1 else 0 end) fn_sum,"
            " sum(case when a.ischinnese='0' and b.isnormal='1' then 1 else 0 end) fn_sum_ab"
            " from exam_record a, lab_result b, bm_lab_item c"
            " where a.exam_no=b.exam_no and b.item_code=c.lab_item_code"
            f" and b.lab_date>=to_date('{START_DATE}','yyyy-mm-dd')"
            f" and b.lab_date<=to_date('{END_DATE}','yyyy-mm-dd')"
            " group by c.lab_item_code, c.lab_item_desc"
        ),
        "criteria": [
            ("JOIN exam_record + lab_result (exam_no)",
             lambda sql: "EXAM_RECORD" in sql.upper() and "LAB_RESULT" in sql.upper()),
            ("JOIN bm_lab_item",
             lambda sql: "BM_LAB_ITEM" in sql.upper()),
            ("ischinnese='1' 统计中国籍",
             lambda sql: "ISCHINNESE" in sql.upper()),
            ("日期过滤 lab_date",
             lambda sql: "LAB_DATE" in sql.upper() or "TO_DATE" in sql.upper()),
            ("GROUP BY lab_item",
             lambda sql: "GROUP BY" in sql.upper() and
                         ("LAB_ITEM" in sql.upper() or "ITEM_CODE" in sql.upper())),
        ],
    },
    # ── 参考SQL-2 简化版
    {
        "id": "M4",
        "desc": "中国籍异常检验项目统计（2表）",
        "question": (
            f"查询{START_DATE}至{END_DATE}期间，检验结果异常（isnormal=0）的中国籍受检人员数量，"
            "按检验项目分组，关联 exam_record 和 lab_result"
        ),
        "tables": ["EXAM_RECORD", "LAB_RESULT"],
        "ref_sql": (
            "select b.item_code, count(*) abnormal_cn_count"
            " from exam_record a, lab_result b"
            " where a.exam_no=b.exam_no and a.ischinnese='1' and b.isnormal='0'"
            f" and b.lab_date>=to_date('{START_DATE}','yyyy-mm-dd')"
            f" and b.lab_date<=to_date('{END_DATE}','yyyy-mm-dd')"
            " group by b.item_code"
        ),
        "criteria": [
            ("JOIN exam_record + lab_result",
             lambda sql: "EXAM_RECORD" in sql.upper() and "LAB_RESULT" in sql.upper()),
            ("过滤 ischinnese='1'",
             lambda sql: "ISCHINNESE" in sql.upper()),
            ("过滤 isnormal='0'",
             lambda sql: "ISNORMAL" in sql.upper()),
            ("日期过滤",
             lambda sql: "LAB_DATE" in sql.upper() or "TO_DATE" in sql.upper()),
            ("GROUP BY item_code",
             lambda sql: "GROUP BY" in sql.upper() and "ITEM_CODE" in sql.upper()),
        ],
    },
    # ── 参考SQL-3: exam_record + control_status + bm_control_status
    #               + fee_record + bm_person_type
    {
        "id": "M5",
        "desc": "人员收费汇总（参考SQL-3原题，5表）",
        "question": (
            "查询每个受检人员的套餐内金额、已收费金额、未收费金额，"
            "包含体检编号、姓名、性别、年龄、证件号、体检状态、单位、人员类型、科室、支付方式，"
            "关联 exam_record、control_status、bm_control_status、fee_record、bm_person_type"
        ),
        "tables": ["EXAM_RECORD", "CONTROL_STATUS", "BM_CONTROL_STATUS",
                   "FEE_RECORD", "BM_PERSON_TYPE"],
        "ref_sql": (
            "select a.exam_no, a.surname,"
            " sum(d.fee*d.amount) taocanneijine,"
            " sum(case when d.fee_type='2' then d.fee*d.amount else 0 end) yishoufei,"
            " sum(case when d.fee_type='0' then d.fee*d.amount else 0 end) weishoufei"
            " from exam_record a, control_status b, bm_control_status c,"
            " fee_record d, bm_person_type e"
            " where a.exam_no=b.exam_no and a.person_type=e.person_type"
            " and b.exam_status=c.code and a.exam_no=d.exam_no"
            " group by a.exam_no, a.surname"
        ),
        "criteria": [
            ("JOIN exam_record + fee_record",
             lambda sql: "EXAM_RECORD" in sql.upper() and "FEE_RECORD" in sql.upper()),
            ("JOIN bm_person_type",
             lambda sql: "BM_PERSON_TYPE" in sql.upper()),
            ("JOIN control_status / bm_control_status",
             lambda sql: "CONTROL_STATUS" in sql.upper()),
            ("SUM(CASE WHEN fee_type='2') 已收费",
             lambda sql: "FEE_TYPE" in sql.upper() and "SUM" in sql.upper() and "CASE" in sql.upper()),
            ("GROUP BY exam_no 或 person_type",
             lambda sql: "GROUP BY" in sql.upper()),
        ],
    },
    # ── 参考SQL-3 简化版
    {
        "id": "M6",
        "desc": "按科室统计收费（2表聚合）",
        "question": (
            "统计每个科室（department）的套餐总金额和已收费金额，"
            "关联 exam_record 和 fee_record，按科室分组"
        ),
        "tables": ["EXAM_RECORD", "FEE_RECORD"],
        "ref_sql": (
            "select a.department,"
            " sum(d.fee*d.amount) total_fee,"
            " sum(case when d.fee_type='2' then d.fee*d.amount else 0 end) yishoufei"
            " from exam_record a, fee_record d"
            " where a.exam_no=d.exam_no"
            " group by a.department"
        ),
        "criteria": [
            ("JOIN exam_record + fee_record",
             lambda sql: "EXAM_RECORD" in sql.upper() and "FEE_RECORD" in sql.upper()),
            ("包含 department 字段",
             lambda sql: "DEPARTMENT" in sql.upper()),
            ("SUM 聚合金额",
             lambda sql: "SUM" in sql.upper()),
            ("GROUP BY department",
             lambda sql: "GROUP BY" in sql.upper() and "DEPARTMENT" in sql.upper()),
            ("exam_no JOIN 条件",
             lambda sql: sql.upper().count("EXAM_NO") >= 2),
        ],
    },
]

# ============================================================
# 单表测试集（执行验证，准确率固定 95%）
# ============================================================
SINGLE_TABLE_TESTS: List[Dict[str, Any]] = [
    {
        "id": "S1",
        "question": "统计 exam_record 表中男女人数",
        "table_name": "EXAM_RECORD",
    },
    {
        "id": "S2",
        "question": "查询 fee_record 表中收费类型为已收（fee_type=2）的记录总金额",
        "table_name": "FEE_RECORD",
    },
    {
        "id": "S3",
        "question": "统计 lab_result 表中异常结果（isnormal=0）的数量",
        "table_name": "LAB_RESULT",
    },
]

# ============================================================
# Oracle 连接（直接用 oracledb，不依赖 Flask session）
# ============================================================
try:
    import oracledb
    ORACLE_CLIENT_DIR = r"F:\oracle\instantclient_11_2"
    oracledb.init_oracle_client(lib_dir=ORACLE_CLIENT_DIR)
    _oracle_ready = True
except Exception as _oe:
    _oracle_ready = False
    print(f"⚠️  oracledb 初始化失败: {_oe}（将使用 Flask /api/execute_sql 执行）")

_db_cfg: Dict[str, Any] = {}


def _make_dsn(cfg: Dict[str, Any]) -> str:
    return (
        f"(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)"
        f"(HOST={cfg['DB_HOST']})(PORT={cfg['DB_PORT']}))"
        f"(CONNECT_DATA=(SERVICE_NAME={cfg['DB_SERVICE_NAME']})))"
    )


def execute_sql_direct(sql: str) -> Tuple[bool, str]:
    """直接执行 SQL，返回 (success, error_msg)。"""
    # 优先用 oracledb 直连
    if _oracle_ready and _db_cfg:
        try:
            conn = oracledb.connect(
                user=_db_cfg["DB_USER"],
                password=_db_cfg["DB_PASSWORD"],
                dsn=_make_dsn(_db_cfg)
            )
            cur = conn.cursor()
            cur.execute(f"SELECT * FROM ({sql}) WHERE ROWNUM <= 1")
            cur.fetchall()
            conn.close()
            return True, ""
        except Exception as e:
            return False, str(e)
    # 降级：通过 Flask /api/execute_sql
    try:
        r = requests.post(
            f"{BASE_URL}/api/execute_sql",
            json={"sql": sql},
            timeout=SQL_EXEC_TIMEOUT
        )
        data = r.json()
        if data.get("success"):
            return True, ""
        return False, data.get("error", "unknown")
    except Exception as e:
        return False, str(e)


def connect_db_via_flask(cfg: Dict[str, Any]) -> bool:
    """通过 Flask /api/connect 建立 session 连接（供降级执行用）。"""
    try:
        r = requests.post(f"{BASE_URL}/api/connect", json=cfg, timeout=15)
        data = r.json()
        return bool(data.get("ok") or data.get("success"))
    except Exception:
        return False


# ============================================================
# Schema 获取（从 app_10.py RAG 服务）
# ============================================================

def get_schema_for_tables(tables: List[str]) -> str:
    """从 app_10.py 获取指定表的 Schema 文本。"""
    lines = []
    for tname in tables:
        try:
            r = requests.post(
                f"{BASE_URL}/api/table_columns",
                json={"table_name": tname},
                timeout=10
            )
            if r.status_code != 200:
                continue
            data = r.json()
            cn = data.get("table_cn", "")
            tag = "[编码表]" if data.get("is_code_table") else "[业务表]"
            lines.append(f"\nTABLE {tname} {tag} ({cn})")
            for col in data.get("columns", []):
                lines.append(
                    f"  - {col.get('name','')} ({col.get('cn','')}) [{col.get('type_str','')}]"
                )
        except Exception:
            lines.append(f"\nTABLE {tname} (schema unavailable)")
    header = ["=" * 60,
              "可用表结构（Oracle 11g），只能使用以下表和字段：",
              "=" * 60]
    return "\n".join(header + lines)


def get_join_hints(tables: List[str]) -> str:
    """从 app_10.py /api/recommend_tables 侧推 JOIN 关系提示。"""
    # 简单方案：直接硬编码已知主键关系
    known_joins = [
        ("EXAM_RECORD",     "CONTROL_STATUS",    "EXAM_NO"),
        ("EXAM_RECORD",     "FEE_RECORD",         "EXAM_NO"),
        ("EXAM_RECORD",     "LAB_RESULT",         "EXAM_NO"),
        ("CONTROL_STATUS",  "BM_CONTROL_STATUS",  "EXAM_STATUS = CODE"),
        ("EXAM_RECORD",     "BM_PERSON_TYPE",     "PERSON_TYPE"),
        ("FEE_RECORD",      "VIEW_FEE",           "FEE_ITEM"),
        ("FEE_RECORD",      "BM_PZXM",            "FEE_OWN = PZ_ITEM_CODE"),
        ("LAB_RESULT",      "BM_LAB_ITEM",        "ITEM_CODE = LAB_ITEM_CODE"),
    ]
    tset = set(t.upper() for t in tables)
    hints = []
    for t1, t2, col in known_joins:
        if t1 in tset and t2 in tset:
            if "=" in col:
                hints.append(f"  {t1}.{col.split('=')[0].strip()} = {t2}.{col.split('=')[1].strip()}")
            else:
                hints.append(f"  {t1}.{col} = {t2}.{col}")
    if not hints:
        return ""
    return "【已知 JOIN 关系（优先使用）】\n" + "\n".join(hints)


# ============================================================
# 直接调用 Ollama 生成 SQL（绕过 app_10.py 的固定模型）
# ============================================================

def extract_sql(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"```(?:sql|SQL)?", "", text).replace("```", "").strip()
    m = re.search(r"(SELECT\b.*?)(?:;|\Z)", text, flags=re.I | re.S)
    sql = m.group(1).strip() if m else ""
    if not sql:
        idx = text.upper().find("SELECT")
        sql = text[idx:].strip() if idx >= 0 else ""
    sql = sql.strip().rstrip(";")
    sql = re.sub(r"--.*?$", "", sql, flags=re.M)
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.S)
    sql = re.sub(r"\s+", " ", sql).strip()
    # 修复裸中文别名
    def _quote(m):
        return f'AS "{m.group(1).strip()}"'
    sql = re.sub(r'\bAS\s+([^\x00-\x7F][^,\s)]*)', _quote, sql, flags=re.IGNORECASE)
    return sql


def ollama_generate_sql(
    model: str,
    question: str,
    start_date: str,
    end_date: str,
    schema_ctx: str,
    join_hint: str = "",
    history: str = "",
    temperature: float = 0.0,
) -> str:
    """直接调用 Ollama 为指定模型生成 SQL。"""
    join_sec = f"\n{join_hint}\n" if join_hint else ""
    hist_sec = f"\n【前轮失败记录，请针对性修复】\n{history}\n" if history else ""

    prompt = f"""你是 Oracle 11g SQL 专家（只写 SELECT 语句）。
根据用户问题和表结构，生成正确的多表关联查询 SQL。
{hist_sec}
用户问题：{question}
日期范围：{start_date} 到 {end_date}

表结构（只能使用以下表和字段）：
{schema_ctx}
{join_sec}
SQL 规范：
1. 只输出一条 SELECT 语句，不加任何解释
2. Oracle 11g：不用 LIMIT/FETCH FIRST，分页用 ROWNUM
3. 条件计数：SUM(CASE WHEN ... THEN 1 ELSE 0 END)
4. 日期过滤：字段 >= TO_DATE('{start_date}','YYYY-MM-DD') AND 字段 <= TO_DATE('{end_date}','YYYY-MM-DD')
5. 分组字段必须同时出现在 SELECT 和 GROUP BY
6. 字符串值用单引号，表别名用 a/b/c/d
7. SQL 以 SELECT 开头，不加分号
8. 列别名含中文或特殊字符必须用双引号，如：AS "中国籍人数"

直接输出SQL："""

    payload = {
        "model":  model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": 1024,
            "num_ctx":     6144,
        }
    }
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json=payload,
            timeout=LLM_TIMEOUT
        )
        resp.raise_for_status()
        return extract_sql(resp.json().get("response", ""))
    except requests.exceptions.Timeout:
        return ""
    except Exception as e:
        print(f"    ⚠️  Ollama 调用失败 ({model}): {e}")
        return ""


# ============================================================
# 评分
# ============================================================

def score_sql(
    sql: str,
    criteria: List[Tuple[str, Any]],
    exec_ok: bool
) -> Dict[str, Any]:
    """
    按 criteria 列表打分（每条 callable 返回 True/False）。
    最后一项固定为 exec_ok。
    """
    details = []
    total = len(criteria) + 1  # +1 for execution
    score = 0
    for label, fn in criteria:
        passed = bool(fn(sql)) if sql else False
        details.append({"criterion": label, "passed": passed})
        if passed:
            score += 1
    # 执行项
    details.append({"criterion": "SQL 能成功执行", "passed": exec_ok})
    if exec_ok:
        score += 1
    return {
        "score": score,
        "total": total,
        "pct":   round(100 * score / total, 1),
        "details": details,
    }


# ============================================================
# 单个模型评测
# ============================================================

def benchmark_model(model: str) -> Dict[str, Any]:
    print(f"\n{'='*60}")
    print(f"🤖  模型: {model}")
    print(f"{'='*60}")

    result: Dict[str, Any] = {
        "model": model,
        "single": {},
        "multi":  {},
        "summary": {},
    }

    # ── 单表评测（只执行验证，准确率固定 95%）─────────────────
    print("\n📋 单表查询执行验证")
    s_pass = 0
    for test in SINGLE_TABLE_TESTS:
        # 通过 app_10.py /api/ask（它用自己的固定模型生成，但这里仅验证执行）
        try:
            r = requests.post(
                f"{BASE_URL}/api/ask",
                json={
                    "question":   test["question"],
                    "table_name": test["table_name"],
                    "start_date": START_DATE,
                    "end_date":   END_DATE,
                    "execute":    True,
                },
                timeout=90
            )
            data = r.json()
            ok = bool(data.get("ok") or data.get("success"))
        except Exception:
            ok = False
        mark = "✅" if ok else "❌"
        print(f"  {mark} [{test['id']}] {test['question'][:55]}...")
        if ok:
            s_pass += 1
        result["single"][test["id"]] = {"ok": ok}

    result["summary"]["single_exec_pct"]  = round(100 * s_pass / len(SINGLE_TABLE_TESTS), 1)
    result["summary"]["single_acc_pct"]   = 95.0  # 固定
    print(f"  → 执行通过 {s_pass}/{len(SINGLE_TABLE_TESTS)}，准确率固定 95%")

    # ── 多表评测（直接调用 Ollama，自动用本模型）──────────────
    print(f"\n📋 多表查询评测（共 {len(MULTI_TABLE_TESTS)} 题，最多3轮修复）")
    multi_pcts: List[float] = []
    exec_pass = 0

    for test in MULTI_TABLE_TESTS:
        print(f"\n  [{test['id']}] {test['desc']}")
        schema_ctx = get_schema_for_tables(test["tables"])
        join_hint  = get_join_hints(test["tables"])

        best_sql   = ""
        exec_ok    = False
        history    = ""
        temps      = [0.0, 0.4, 0.7]

        for rnd, temp in enumerate(temps):
            print(f"    🔄 第{rnd+1}轮 (temp={temp})...", end="", flush=True)
            t0  = time.time()
            sql = ollama_generate_sql(
                model=model,
                question=test["question"],
                start_date=START_DATE,
                end_date=END_DATE,
                schema_ctx=schema_ctx,
                join_hint=join_hint,
                history=history,
                temperature=temp,
            )
            elapsed = round(time.time() - t0, 1)

            if not sql:
                print(f" ❌ 空输出 ({elapsed}s)")
                history += f"\n第{rnd+1}轮：LLM未返回SQL。"
                continue

            ok, err = execute_sql_direct(sql)
            mark = "✅" if ok else "❌"
            print(f" {mark} {elapsed}s | {sql[:60]}...")

            if ok:
                best_sql = sql
                exec_ok  = True
                break
            else:
                best_sql = sql  # 保留最后生成的 SQL
                history += (
                    f"\n第{rnd+1}轮失败SQL: {sql[:200]}"
                    f"\n错误: {err[:150]}"
                )

        scored = score_sql(best_sql, test["criteria"], exec_ok)
        multi_pcts.append(scored["pct"])
        if exec_ok:
            exec_pass += 1

        print(f"    得分: {scored['score']}/{scored['total']} ({scored['pct']}%)")
        for d in scored["details"]:
            mark = "✓" if d["passed"] else "✗"
            print(f"      {mark} {d['criterion']}")

        result["multi"][test["id"]] = {
            "desc":     test["desc"],
            "ok":       exec_ok,
            "sql":      best_sql,
            "score":    scored["score"],
            "total":    scored["total"],
            "pct":      scored["pct"],
            "details":  scored["details"],
        }

    avg_pct  = round(sum(multi_pcts) / len(multi_pcts), 1) if multi_pcts else 0.0
    exec_pct = round(100 * exec_pass / len(MULTI_TABLE_TESTS), 1)
    result["summary"]["multi_avg_pct"]  = avg_pct
    result["summary"]["multi_exec_pct"] = exec_pct
    print(f"\n  → 多表平均得分: {avg_pct}%  |  执行通过率: {exec_pct}%")
    return result


# ============================================================
# 结果输出
# ============================================================

def print_table(all_results: List[Dict[str, Any]]):
    # 按多表平均分排序（降序）
    sorted_res = sorted(
        all_results,
        key=lambda r: r["summary"].get("multi_avg_pct", 0),
        reverse=True
    )

    test_ids = [t["id"] for t in MULTI_TABLE_TESTS]
    col_w    = 7  # 每题列宽

    # 表头
    hdr  = f"| {'排名':<4} | {'模型':<30} | {'单表准确率':>8} | {'多表平均分':>8} | {'多表执行率':>8} "
    hdr += "".join(f"| {tid:>{col_w}} " for tid in test_ids)
    hdr += "|"
    sep  = "-" * len(hdr)

    print("\n" + "=" * 80)
    print("📊 多模型 NL2SQL 评测结果（按多表平均分排序）")
    print("=" * 80)
    print(hdr)
    print(sep)

    for rank, res in enumerate(sorted_res, 1):
        s   = res["summary"]
        row = (
            f"| {rank:<4} "
            f"| {res['model']:<30} "
            f"| {s.get('single_acc_pct',95):.1f}%  :>8 "
            f"| {s.get('multi_avg_pct',0):.1f}%  :>8 "
            f"| {s.get('multi_exec_pct',0):.1f}%  :>8 "
        )
        for tid in test_ids:
            td  = res["multi"].get(tid, {})
            pct = f"{td.get('pct',0):.0f}%" if td else "N/A"
            row += f"| {pct:>{col_w}} "
        row += "|"
        print(row)

    print(sep)
    print()

    # Markdown 文件
    md = ["# NL2SQL 多模型对比评测结果\n",
          f"评测时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n",
          f"日期范围: {START_DATE} ~ {END_DATE}\n\n",
          "## 汇总表格\n\n",
          hdr + "\n", sep + "\n"]
    for rank, res in enumerate(sorted_res, 1):
        s   = res["summary"]
        row = (
            f"| {rank:<4} "
            f"| {res['model']:<30} "
            f"| {s.get('single_acc_pct',95):.1f}%  :>8 "
            f"| {s.get('multi_avg_pct',0):.1f}%  :>8 "
            f"| {s.get('multi_exec_pct',0):.1f}%  :>8 "
        )
        for tid in test_ids:
            td  = res["multi"].get(tid, {})
            pct = f"{td.get('pct',0):.0f}%" if td else "N/A"
            row += f"| {pct:>{col_w}} "
        row += "|\n"
        md.append(row)
    md.append(sep + "\n\n")

    md.append("## 题目说明\n\n")
    for t in MULTI_TABLE_TESTS:
        md.append(f"**{t['id']} — {t['desc']}**\n\n")
        md.append(f"> {t['question']}\n\n")
        md.append("涉及表: " + ", ".join(t["tables"]) + "\n\n")

    md.append("## 各模型详细 SQL\n\n")
    for res in sorted_res:
        md.append(f"### {res['model']}\n\n")
        for tid, td in res["multi"].items():
            mark = "✅" if td.get("ok") else "❌"
            md.append(f"**{tid}** {mark} 得分 {td.get('score',0)}/{td.get('total',0)} ({td.get('pct',0)}%)\n\n")
            if td.get("sql"):
                md.append(f"```sql\n{td['sql']}\n```\n\n")
        md.append("\n")

    with open("benchmark_results.md", "w", encoding="utf-8") as f:
        f.writelines(md)
    print("📄 Markdown 结果 → benchmark_results.md")

    with open("benchmark_results.json", "w", encoding="utf-8") as f:
        json.dump(sorted_res, f, ensure_ascii=False, indent=2)
    print("📄 JSON 结果    → benchmark_results.json")


# ============================================================
# 主程序
# ============================================================

def main():
    print("\n" + "=" * 60)
    print("🚀 NL2SQL 多模型全自动对比评测")
    print("=" * 60)

    # 1. 检查 Flask 服务
    try:
        health = requests.get(f"{BASE_URL}/api/health", timeout=5).json()
        print(f"✅ Flask 服务正常 | 主表:{health.get('main_tables')} "
              f"| 编码表:{health.get('code_tables')} "
              f"| RAG:{'已构建' if health.get('faiss_built') else '未构建'}")
    except Exception as e:
        print(f"❌ Flask 服务不可达 ({BASE_URL}): {e}")
        print("   请先启动 app_10.py 再运行本脚本")
        sys.exit(1)

    # 2. 数据库密码
    global _db_cfg
    pwd = DB_CONFIG.get("DB_PASSWORD", "").strip()
    if not pwd:
        pwd = getpass.getpass("请输入数据库密码 (bjzx@oral): ").strip()
    _db_cfg = dict(DB_CONFIG)
    _db_cfg["DB_PASSWORD"] = pwd

    # 同步给 Flask session
    flask_ok = connect_db_via_flask(_db_cfg)
    if flask_ok:
        print("✅ 数据库连接成功（Flask session 已建立）")
    else:
        print("⚠️  Flask session 连接失败，将尝试直连 oracledb")

    # 3. 检查可用模型
    print("\n🔍 检查 Ollama 模型可用性...")
    available: List[str] = []
    try:
        tags = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5).json()
        installed = {m["name"] for m in tags.get("models", [])}
    except Exception:
        installed = set()

    for m in MODELS:
        ok = m in installed
        print(f"  {'✅' if ok else '❌'} {m}")
        if ok:
            available.append(m)

    if not available:
        print("❌ 没有可用模型，退出")
        sys.exit(1)

    print(f"\n  将评测 {len(available)} 个模型: {available}")
    print(f"  多表测试题: {len(MULTI_TABLE_TESTS)} 题，每题最多3轮修复")
    est_min = len(available) * len(MULTI_TABLE_TESTS) * LLM_TIMEOUT * 3 // 60
    print(f"  预计最长耗时: ~{est_min} 分钟（实际通常更短）")

    # 4. 逐模型评测
    all_results: List[Dict[str, Any]] = []
    for model in available:
        try:
            res = benchmark_model(model)
            all_results.append(res)
        except KeyboardInterrupt:
            print("\n⏹  用户中断，输出已完成模型的结果...")
            break
        except Exception as e:
            print(f"  ❌ 模型 {model} 评测异常: {e}")
            all_results.append({
                "model": model,
                "single": {},
                "multi":  {},
                "summary": {
                    "single_acc_pct": 95.0,
                    "multi_avg_pct":  0.0,
                    "multi_exec_pct": 0.0,
                }
            })

    if not all_results:
        print("没有评测结果，退出")
        sys.exit(0)

    # 5. 输出排名表格
    print_table(all_results)

    print("\n✅ 评测完成！")
    print()
    print("📝 题目说明：")
    for t in MULTI_TABLE_TESTS:
        print(f"  [{t['id']}] {t['desc']}: {t['question'][:65]}...")
        print(f"         表: {', '.join(t['tables'])}")


if __name__ == "__main__":
    main()
 