from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


PROJECT_ROOT = Path(__file__).resolve().parent
LOCAL_RAGANYTHING = PROJECT_ROOT / "third_party" / "raganything-1.3.1"
if LOCAL_RAGANYTHING.exists() and str(LOCAL_RAGANYTHING) not in sys.path:
    sys.path.insert(0, str(LOCAL_RAGANYTHING))
LOCAL_LIGHTRAG = PROJECT_ROOT / "third_party" / "lightrag_hku-1.5.0"
if LOCAL_LIGHTRAG.exists() and str(LOCAL_LIGHTRAG) not in sys.path:
    sys.path.insert(0, str(LOCAL_LIGHTRAG))


_ACTIVE_EMBED_FUNC: Optional[Callable[[List[str]], Any]] = None
_ACTIVE_CHAT_FUNC: Optional[Callable[..., str]] = None


async def _schema_embedding_async(texts: List[str]) -> Any:
    if _ACTIVE_EMBED_FUNC is None:
        raise RuntimeError("RAGAnything schema embedding function is not configured")
    matrix = _ACTIVE_EMBED_FUNC(list(texts))
    return matrix


async def _schema_llm_async(
    prompt: str,
    system_prompt: Optional[str] = None,
    history_messages: Optional[List[Dict[str, str]]] = None,
    **kwargs: Any,
) -> str:
    if _ACTIVE_CHAT_FUNC is None:
        raise RuntimeError("RAGAnything schema LLM function is not configured")

    parts = []
    if system_prompt:
        parts.append(system_prompt)
    if history_messages:
        for message in history_messages:
            role = message.get("role", "user")
            content = message.get("content", "")
            parts.append(f"{role}: {content}")
    parts.append(prompt)
    max_tokens = int(
        kwargs.get("max_tokens")
        or kwargs.get("num_predict")
        or os.environ.get("RAGANYTHING_LLM_MAX_TOKENS", "1024")
    )
    timeout = int(os.environ.get("RAGANYTHING_LLM_TIMEOUT", "120"))
    return _ACTIVE_CHAT_FUNC(
        "\n\n".join(parts),
        temperature=float(kwargs.get("temperature", 0.0)),
        max_tokens=max_tokens,
        timeout=timeout,
        num_ctx=int(os.environ.get("RAGANYTHING_LLM_NUM_CTX", "8192")),
    )


class RAGAnythingSchemaRetriever:
    """RAGAnything/LightRAG-backed schema retriever with deterministic fallback."""

    def __init__(
        self,
        data_dict: Dict[str, Any],
        embed_func: Callable[[List[str]], Any],
        chat_func: Callable[..., str],
        fallback_index: Any,
        dictionary_path: Path,
        working_dir: Path,
        enabled: bool = True,
    ) -> None:
        self.data_dict = data_dict
        self.embed_func = embed_func
        self.chat_func = chat_func
        self.fallback_index = fallback_index
        self.dictionary_path = Path(dictionary_path)
        self.working_dir = Path(working_dir)
        self.enabled = enabled
        self.available = False
        self._rag = None
        self._lightrag = None
        self._adapter = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._last_context = ""
        self._last_error = ""
        self.rel_entries = getattr(fallback_index, "rel_entries", [])

        if self.enabled:
            self._load_dependencies()

    @property
    def last_context(self) -> str:
        return self._last_context

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def backend_name(self) -> str:
        return "raganything_lightrag_kg" if self.available else "fallback_faiss"

    def _load_dependencies(self) -> None:
        try:
            import raganything.sql_dictionary as adapter
            from lightrag import LightRAG
            from lightrag import QueryParam
            from lightrag.kg.shared_storage import initialize_pipeline_status
            from lightrag.utils import EmbeddingFunc

            self._adapter = adapter
            self._LightRAG = LightRAG
            self._QueryParam = QueryParam
            self._initialize_pipeline_status = initialize_pipeline_status
            self._EmbeddingFunc = EmbeddingFunc
            self.available = True
        except Exception as exc:
            self.available = False
            self._last_error = f"{type(exc).__name__}: {exc}"

    def build_or_load(self) -> None:
        if not self.available:
            self.fallback_index.build_or_load()
            return
        try:
            self._run_async(self._abuild_or_load())
        except Exception as exc:
            self.available = False
            self._last_error = f"{type(exc).__name__}: {exc}"
            print(f"[RAGAnything] KG init failed, falling back to FAISS: {self._last_error}")
            traceback.print_exc(limit=2)
            self.fallback_index.build_or_load()

    async def _abuild_or_load(self) -> None:
        signature = self._adapter.dictionary_signature(self.data_dict)
        schema_dir = self.working_dir / f"schema_{signature}"
        schema_dir.mkdir(parents=True, exist_ok=True)
        marker_path = schema_dir / "coordsql_schema_kg.marker.json"

        embedding_dim = int(os.environ.get("RAGANYTHING_EMBEDDING_DIM", "0"))
        if embedding_dim <= 0:
            probe = self.embed_func(["dimension probe"])
            embedding_dim = int(getattr(probe, "shape", [0, 0])[1])
        if embedding_dim <= 0:
            raise RuntimeError("Unable to determine embedding dimension for RAGAnything")

        global _ACTIVE_EMBED_FUNC, _ACTIVE_CHAT_FUNC
        _ACTIVE_EMBED_FUNC = self.embed_func
        _ACTIVE_CHAT_FUNC = self.chat_func

        embedding_func = self._EmbeddingFunc(
            embedding_dim=embedding_dim,
            max_token_size=int(os.environ.get("RAGANYTHING_EMBEDDING_MAX_TOKENS", "8192")),
            func=_schema_embedding_async,
        )
        self._lightrag = self._LightRAG(
            working_dir=str(schema_dir),
            llm_model_func=_schema_llm_async,
            embedding_func=embedding_func,
            chunk_token_size=int(os.environ.get("RAGANYTHING_CHUNK_TOKENS", "900")),
            chunk_overlap_token_size=int(
                os.environ.get("RAGANYTHING_CHUNK_OVERLAP", "120")
            ),
            top_k=int(os.environ.get("RAGANYTHING_TOP_K", "12")),
            chunk_top_k=int(os.environ.get("RAGANYTHING_CHUNK_TOP_K", "12")),
        )

        await self._lightrag.initialize_storages()
        await self._initialize_pipeline_status()
        if not marker_path.exists():
            custom_kg = self._adapter.build_custom_kg(
                self.data_dict, file_path=str(self.dictionary_path)
            )
            await self._lightrag.ainsert_custom_kg(
                custom_kg, full_doc_id=f"coordsql-schema-{signature}"
            )
            marker_path.write_text(
                json.dumps(
                    {
                        "signature": signature,
                        "tables": len(custom_kg.get("entities", [])),
                        "relationships": len(custom_kg.get("relationships", [])),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

        entries = self._adapter.build_schema_entries(self.data_dict)
        self.rel_entries = entries.get("relationships", [])
        print(f"[RAGAnything] schema KG ready: {schema_dir}")

    def search(
        self,
        query: str,
        topk_table: int = 6,
        topk_col: int = 12,
        topk_rel: int = 10,
    ):
        if not self.available or self._lightrag is None:
            return self.fallback_index.search(query, topk_table, topk_col, topk_rel)

        try:
            keywords = self._adapter.query_keywords(self.data_dict, query)
            param = self._QueryParam(
                mode=os.environ.get("RAGANYTHING_QUERY_MODE", "mix"),
                only_need_context=True,
                top_k=max(topk_table, topk_rel, 8),
                chunk_top_k=max(topk_col, 8),
                hl_keywords=keywords.get("high_level", []),
                ll_keywords=keywords.get("low_level", []),
                max_entity_tokens=int(
                    os.environ.get("RAGANYTHING_MAX_ENTITY_TOKENS", "4200")
                ),
                max_relation_tokens=int(
                    os.environ.get("RAGANYTHING_MAX_RELATION_TOKENS", "2400")
                ),
                max_total_tokens=int(
                    os.environ.get("RAGANYTHING_MAX_TOTAL_TOKENS", "9000")
                ),
            )
            context = self._run_async(
                self._lightrag.aquery(
                    query,
                    param=param,
                )
            )
            self._last_context = context or ""
            hits = self._adapter.hits_from_context(
                self._last_context,
                self.data_dict,
                question=query,
                topk_table=topk_table,
                topk_col=topk_col,
                topk_rel=topk_rel,
            )
            if not any(hits):
                hits = self._adapter.hits_from_context(
                    "",
                    self.data_dict,
                    question=query,
                    topk_table=topk_table,
                    topk_col=topk_col,
                    topk_rel=topk_rel,
                )
            if not any(hits):
                raise RuntimeError("RAGAnything returned empty schema hits")
            return hits
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            print(f"[RAGAnything] KG query failed, falling back to FAISS: {self._last_error}")
            try:
                return self._adapter.hits_from_context(
                    "",
                    self.data_dict,
                    question=query,
                    topk_table=topk_table,
                    topk_col=topk_col,
                    topk_rel=topk_rel,
                )
            except Exception:
                return self.fallback_index.search(query, topk_table, topk_col, topk_rel)

    def _run_async(self, coro):
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
        return self._loop.run_until_complete(coro)
