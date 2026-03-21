# main.py - NL2SQL 智能体系统

一个基于多智能体协调框架的企业级自然语言转SQL系统，专为Oracle 11g数据库优化。

## 📋 系统概述

### 核心架构

```
用户问题
    ↓
[Retrieve] → 多粒度RAG检索（表/列/关系三层）
    ↓
[Plan] → 查询计划生成（可选）
    ↓
[Generate] → SQL生成（LLM）
    ↓
[Guard] → 静态验证
    ↓
[Execute] → Oracle执行
    ↓
[Repair] → 迭代纠错（失败时循环）
    ↓
最终SQL结果
```

### 关键特性

- **多粒度RAG检索**：表级、列级、关系级三层FAISS索引
- **迭代纠错机制**：全量历史传递，智能温度调度
- **编码表自动映射**：CODE字段自动关联编码表获取中文名称
- **多表JOIN智能化**：自动推断表间关系，生成JOIN提示
- **GPU加速**：FAISS索引构建和向量搜索支持GPU
- **并行Embedding**：32线程并行计算，支持缓存
- **Oracle方言适配**：TO_DATE、ROWNUM、中文别名双引号等

---

## 🚀 快速开始

### 环境要求

```bash
Python 3.8+
Oracle Instant Client 11.2+
Ollama（本地LLM服务）
CUDA 11.8+（可选，GPU加速）
```

### 安装依赖

```bash
pip install -r requirements.txt
```

### 配置环境变量

创建 `.env` 文件或设置系统环境变量：

```bash
# Ollama 配置
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_SQL_MODEL=qwen2.5-coder:7b
OLLAMA_EMBEDDING_MODEL=nomic-embed-text:latest

# Oracle 配置
ORACLE_CLIENT_DIR=/path/to/instantclient_11_2
DB_USER=bjzx
DB_PASSWORD=your_password
DB_HOST=127.0.0.1
DB_PORT=1521
DB_SERVICE_NAME=oral

# 数据字典路径
DATA_DICT_PATH=data_dictionary.json
RAG_CACHE_DIR=./rag_cache
MAX_ROWS=500
```

### 启动服务

```bash
python main.py
```

服务将在 `http://0.0.0.0:5000` 启动

---

## 📚 核心模块说明

### ① 基本配置（第1-50行）

- Flask应用初始化
- Ollama API配置
- Oracle客户端初始化
- 系统参数设置

### ② JSON序列化（第51-60行）

处理Decimal、datetime等特殊类型的JSON序列化

### ③ Ollama调用（第61-200行）

**关键函数：**

- `ollama_embed(texts, batch_size=100, use_cache=True)` - 文本向量化
  - 支持并行处理（32线程）
  - 自动缓存机制
  - Ollama失败时自动降级到SentenceTransformer

- `ollama_chat(prompt, model, temperature, max_tokens, timeout, num_ctx)` - LLM调用
  - 全局信号量限制并发（防止Ollama过载）
  - 支持自定义num_ctx（默认8192）
  - 超时自动返回空字符串

- `extract_sql(text)` - SQL提取
  - 移除Markdown代码块
  - 清理注释
  - 修复中文别名（加双引号）

### ④ Oracle连接（第201-250行）

- `make_dsn(cfg)` - 构建Oracle连接字符串
- `get_conn_from_session()` - 从Session获取连接
- `run_sql(sql, max_rows, timeout)` - 执行SQL并返回结果

### ⑤ 数据字典加载（第251-280行）

- `load_data_dictionary()` - 从JSON加载表结构
- 支持主表和编码表分类
- 自动缓存到内存

### ⑥ Schema工具函数（第281-350行）

- `get_table_info(table)` - 获取表信息
- `get_table_columns(table)` - 获取表列信息
- `is_code_table(table)` - 判断是否编码表

### ⑦ 编码表映射（第351-450行）

**核心逻辑：**

```
CODE字段 → 编码表 → NAME字段 → 中文名称
```

- `_build_code_map()` - 构建CODE→编码表映射
- `get_code_table_hint(main_tables)` - 生成编码表JOIN提示
- `expand_with_code_tables(main_tables)` - 自动扩展编码表

### ⑧ 多粒度Schema RAG索引（第451-700行）

**类：`MultiGranularitySchemaIndex`**

三层索引：
1. **表级索引** - 表名+中文名+描述
2. **列级索引** - 表.列+中文名+描述
3. **关系索引** - JOIN关系（同名关键字段）

**关键方法：**

- `build_entries()` - 构建索引条目
- `_build_faiss()` - 构建FAISS索引（支持GPU）
- `build_or_load()` - 构建或加载缓存
- `search(query, topk_table, topk_col, topk_rel)` - 向量搜索

**缓存机制：**
- 基于data_dictionary.json的文件大小和修改时间
- 缓存路径：`./rag_cache/schema_idx_*.faiss`

### ⑨ Schema文本生成（第701-750行）

- `schema_text_for_tables(tables, max_cols_each)` - 生成表结构文本
- `join_hints_text_for_tables(tables)` - 生成JOIN关系提示

### ⑩ Retrieve（第751-800行）

**函数：`retrieve_schema(question, user_selected_tables)`**

流程：
1. 多粒度RAG搜索（表/列/关系）
2. 候选表去重和排序
3. 自动检测是否需要编码表
4. 编码表自动扩展

返回：
```python
{
    "tables_hits": [(score, entry), ...],
    "cols_hits": [(score, entry), ...],
    "rels_hits": [(score, entry), ...],
    "candidate_tables": ["TABLE1", "TABLE2", ...],
    "need_name": bool
}
```

### ⑪ 多表查询核心循环（第801-1000行）

**函数：`generate_with_repair()`**

迭代纠错流程（最多5轮）：

```
第1轮：temperature=0.0（贪心）
第2轮：temperature=0.5（低温）
第3轮：temperature=0.7（中温）
第4轮：temperature=0.85（高温）
第5轮：temperature=0.95（极高温）
```

**重复SQL检测：**
- 连续重复时强制换思路
- 注入"change approach"指令
- 激进提高temperature

**历史累积机制：**
- 每轮失败记录：`{"round": i, "sql": "...", "error": "..."}`
- 全量历史传给LLM
- LLM逐一分析每轮错误

**最终兜底修复：**
- 5轮全部失败后调用`_final_repair()`
- 传入完整Schema、所有历史、禁用表列表
- temperature=0.3进行深度修复

### ⑫ 单表查询（第1001-1100行）

**函数：`generate_single_table_with_repair()`**

类似多表流程，但：
- 只操作单个主表
- 可选JOIN编码表
- 支持简单查询（SELECT *）和复杂查询（分组统计）

### ⑬ Oracle错误分析（第1101-1200行）

**函数：`_analyze_oracle_error(error_msg, table_name, valid_columns, sql)`**

支持的错误类型：
- ORA-00904：字段不存在
- ORA-00933：SQL命令未正确结束
- ORA-00979：不是GROUP BY表达式
- ORA-00942：表或视图不存在
- ORA-01722：无效数字
- ORA-01843：月份无效
- ORA-00907：缺少右括号
- ORA-01747：列引用格式错误

### ⑭ 表名智能匹配（第1201-1300行）

**函数：`find_best_matching_table(user_input)`**

匹配策略：
1. 精确匹配（英文表名）
2. 精确匹配（中文表名）
3. 相似度匹配（向量搜索）

返回：`(表名, 相似度分数, 匹配类型)`

### ⑮ Flask API路由（第1301-1600行）

#### 单表查询
```
POST /api/ask
{
    "question": "查询体检记录",
    "table_name": "EXAM_RECORD",
    "start_date": "2021-01-01",
    "end_date": "2021-12-31",
    "execute": true
}
```

#### 多表查询
```
POST /api/ask_multi
{
    "question": "中外人员检验异常统计",
    "selected_tables": ["EXAM_RECORD", "LAB_RESULT"],
    "start_date": "2021-01-01",
    "end_date": "2021-12-31",
    "execute": true
}
```

#### 其他接口
- `GET /api/tables` - 获取所有表列表
- `POST /api/table_columns` - 获取表列信息
- `POST /api/execute_sql` - 直接执行SQL
- `POST /api/recommend_tables` - 推荐相关表
- `GET /api/models` - 可用模型列表
- `GET /api/health` - 健康检查
- `POST /api/clear_cache` - 清理缓存

---

## 🔧 配置调优

### 性能优化

#### 1. 并行Embedding
```python
# 默认32线程，可调整
max_workers = min(32, len(texts_to_process))
```

#### 2. 缓存策略
```python
# 启用缓存（默认True）
ollama_embed(texts, use_cache=True)

# 清理缓存
POST /api/clear_cache
```

#### 3. GPU加速
```python
# 自动检测GPU
if hasattr(faiss, 'StandardGpuResources'):
    # 使用GPU构建索引
```

#### 4. 上下文窗口
```python
# 调整num_ctx（默认8192）
ollama_chat(prompt, num_ctx=16384)
```

### 纠错参数调整

```python
# 修改最大纠错轮数
sql = generate_with_repair(..., rounds=7)  # 默认5

# 修改温度调度
temp_map = {0: 0.0, 1: 0.5, 2: 0.7, 3: 0.85}
```

### 检索参数调整

```python
# 修改RAG检索数量
tables, cols, rels = SCHEMA_INDEX.search(
    query,
    topk_table=6,   # 默认6
    topk_col=12,    # 默认12
    topk_rel=10     # 默认10
)
```

---

## 📊 数据字典格式

```json
{
  "main_tables": {
    "EXAM_RECORD": {
      "table_cn": "体检记录",
      "is_code_table": false,
      "short_description": "...",
      "detail_description": "...",
      "columns": [
        {
          "name": "EXAM_NO",
          "cn": "体检编号",
          "data_type": "VARCHAR2",
          "type_str": "VARCHAR2(50)",
          "full_description": "..."
        }
      ]
    }
  },
  "code_tables": {
    "AA_BM_MEDICAL_DEPT": {
      "table_cn": "科室编码表",
      "is_code_table": true,
      "columns": [
        {
          "name": "DEPT_CODE",
          "cn": "科室代码"
        },
        {
          "name": "DEPT_NAME",
          "cn": "科室名称"
        }
      ]
    }
  }
}
```

---

## 🐛 常见问题

### Q1: Ollama连接失败
```
❌ LLM 调用失败: Connection refused
```
**解决：** 确保Ollama服务运行
```bash
ollama serve
```

### Q2: Oracle连接失败
```
❌ ORA-12514: TNS:listener does not currently know of service
```
**解决：** 检查SERVICE_NAME和监听器配置

### Q3: FAISS索引构建失败
```
⚠️ GPU 构建失败，使用 CPU
```
**解决：** 自动降级到CPU，或检查CUDA版本

### Q4: 生成的SQL仍有错误
```
⚠️ 5轮后仍未成功，进入最终兜底修复
```
**解决：** 
- 检查数据字典是否完整
- 增加纠错轮数
- 手动调整温度参数

### Q5: 中文别名报ORA-00923
```
❌ ORA-00923: FROM keyword not found where expected
```
**解决：** 自动修复已启用，检查是否有特殊字符

---

## 📈 性能指标

### 典型响应时间

| 操作 | 耗时 |
|------|------|
| Embedding（100条） | 2-5s |
| FAISS搜索 | 50-100ms |
| Plan生成 | 5-10s |
| SQL生成（第1轮） | 7-15s |
| SQL执行 | 1-5s |
| **总耗时（单表）** | **15-30s** |
| **总耗时（多表）** | **30-60s** |

### 资源占用

| 资源 | 占用 |
|------|------|
| 内存（启动） | 2-3GB |
| 内存（运行） | 4-6GB |
| GPU显存（可选） | 2-4GB |
| 缓存大小 | 100-500MB |

---

## 🔐 安全性

### SQL注入防护

- 只允许SELECT语句
- 禁止DML/DDL关键字
- 参数化查询（Oracle绑定变量）

### 权限管理

- 数据库用户权限最小化
- 只授予SELECT权限
- 不允许修改表结构

### 日志记录

所有SQL执行都记录到标准输出，便于审计

---

## 📝 开发指南

### 添加新的错误类型

编辑 `_analyze_oracle_error()` 函数：

```python
elif "ORA-XXXXX" in error_msg:
    return "错误描述和修复建议"
```

### 自定义温度调度

编辑 `generate_with_repair()` 中的 `temp_map`：

```python
temp_map = {0: 0.0, 1: 0.3, 2: 0.6, 3: 0.9}
```

### 扩展编码表映射

编辑 `_build_code_map()` 中的匹配规则：

```python
code_col_candidates = []
for cn in ct_col_names.keys():
    if cn.endswith("_CODE") or ...:  # 添加新规则
        code_col_candidates.append(cn)
```

---

## 📞 支持

- 问题报告：提交Issue
- 功能建议：提交PR
- 文档反馈：编辑README

---

## 📄 许可证

MIT License

---

## 🙏 致谢

- Ollama - 本地LLM服务
- FAISS - 向量搜索库
- SentenceTransformers - 文本嵌入模型
- Oracle - 企业数据库

---

**最后更新：2026年3月**

