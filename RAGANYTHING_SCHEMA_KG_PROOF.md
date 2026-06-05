# RAGAnything Schema KG: Formal Guarantee for This Database Dictionary

## Setting

Let the database dictionary workbook be a finite set of rows

```text
D = {(t, t_cn, c, c_cn, u, tau, l, p)}
```

where `t` is a table name, `c` is a column name, `t_cn` and `c_cn` are Chinese labels, `u` is the usage description, and `tau/l/p` are type, length, and precision.

The adapter first applies a normalization map

```text
N: D -> (T, {C_t}_{t in T})
```

where `T` is the set of upper-cased table identifiers and `C_t` is the finite set of upper-cased columns belonging to table `t`.

For the current workbook, the normalized dictionary contains:

```text
|T| = 292
sum_t |C_t| = 2964
```

## KG Construction

The constructed schema graph is

```text
G = (V, E, M)
```

where:

```text
V = { TABLE::t | t in T }
```

and `M` is the set of schema chunks. For every table `t`, one chunk is emitted:

```text
m_t = [[TABLE:t]] + concat_{c in C_t} [[COLUMN:t.c]]
```

Therefore every column remains explicitly recoverable even when column-level entities are disabled for tractable LightRAG construction.

The relationship set is a bounded subset of valid dictionary-derived join candidates:

```text
E subseteq { (TABLE::t_i, TABLE::t_j, c) |
             c in C_{t_i} cap C_{t_j}, is_join_key(c) }
          union code-table mapping edges
```

The implementation uses `RAGANYTHING_MAX_JOIN_EDGES=300` for the current run. After
canonical de-duplication of repeated join candidates, the persisted graph contains:

```text
|V| = 292
|E| = 287
|M| = 293
```

The extra chunk is the relation catalog. The marker file records `relationships=300`
as the construction cap/candidate budget, while the GraphML/vector stores contain the
287 unique persisted relationship edges.

## Theorem 1: Table Coverage

For every table `t in T`, the KG contains exactly one table entity `TABLE::t` and at least one chunk marker `[[TABLE:t]]`.

Proof: `build_custom_kg` iterates over every table entry produced by `build_schema_entries(N(D))`. For each table, it appends one entity with name `TABLE::<table_name>` and one chunk whose first line is `[[TABLE:<table_name>]]`. Since `build_schema_entries` enumerates all normalized tables, every table in `T` is covered. Because table names are dictionary keys after normalization, no table outside `T` is introduced. QED.

## Theorem 2: Column Coverage

For every dictionary column `c in C_t`, the table chunk `m_t` contains the marker `[[COLUMN:t.c]]`.

Proof: for each table `t`, the adapter groups all normalized column entries by table. The chunk content for `t` is formed by iterating over every column in that group and appending `[[COLUMN:t.c]]` with its label, type, and usage text. Thus every column in the dictionary is represented in the retrievable text. Since the loop source is exactly the normalized column set, no non-dictionary column marker is created. QED.

## Theorem 3: Identifier Soundness

Every table or column identifier exposed to the SQL generator through KG markers is present in the database dictionary.

Proof: table markers are emitted only from `T`; column markers are emitted only from pairs `(t, c)` where `c in C_t`. Both `T` and `C_t` are produced only from workbook rows after normalization. Therefore all emitted identifiers originate from the database dictionary. QED.

## Theorem 4: Join Referential Validity

Every generated join edge references tables and columns that exist in the dictionary.

Proof: a join edge is added only if either:

1. Two tables share a column `c` and `is_join_key(c)` is true; then `c in C_left` and `c in C_right`.
2. A code-table mapping is detected; then the mapping is constructed from a main-table column and a code-table column found in their respective normalized column sets.

In both cases, the edge endpoints and join columns are dictionary-derived. This proves referential validity. Semantic optimality of inferred joins is an empirical ranking problem, not a purely mathematical property; the proof guarantees the generated edge cannot reference a nonexistent table or column. QED.

## Consequence for Text-to-SQL

Because all retrieved markers are dictionary-derived and exactly formatted, the SQL prompt receives a bounded, parseable evidence set:

```text
[[TABLE:t]]
[[COLUMN:t.c]]
[[REL:t1.c=t2.c]]
```

Thus the generator can be constrained to valid table and column names from the database dictionary, reducing schema hallucination relative to free-form RAG text.
