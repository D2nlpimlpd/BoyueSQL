# 50-Question Semantic Text-to-SQL Evaluation

- Time: 2026-06-05 03:04:16
- Benchmark: `E:\text2sql\semantic_50_benchmark.json`
- Cases: 50 (single=25, multi=25)
- Gold-table hints: False

## Summary

| System | Model | Cases | Exec | Result exact | Semantic pass | Avg score | Avg time |
|---|---|---:|---:|---:|---:|---:|---:|
| direct_prompt | sqlcoder:7b | 50 | 0.00% | 0.00% | 0.00% | 0.002 | 3.30s |
| self_debug_style | sqlcoder:7b | 50 | 0.00% | 0.00% | 0.00% | 0.011 | 8.36s |

## Single vs Multi

| System | Model | Type | Exec | Result exact | Semantic pass | Avg score |
|---|---|---|---:|---:|---:|---:|
| direct_prompt | sqlcoder:7b | multi | 0.00% | 0.00% | 0.00% | 0.000 |
| direct_prompt | sqlcoder:7b | single | 0.00% | 0.00% | 0.00% | 0.004 |
| self_debug_style | sqlcoder:7b | multi | 0.00% | 0.00% | 0.00% | 0.011 |
| self_debug_style | sqlcoder:7b | single | 0.00% | 0.00% | 0.00% | 0.010 |

## Failure Types

### direct_prompt / sqlcoder:7b
- RuntimeError: 48
- DatabaseError: 2

### self_debug_style / sqlcoder:7b
- RuntimeError: 48
- DatabaseError: 2
