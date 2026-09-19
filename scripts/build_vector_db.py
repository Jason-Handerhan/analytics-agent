"""Chunks context/docs/ into staging.doc_chunks for the 'vector_db' Dataform
tag to embed into vector_db.chunks_docs_embedded (docs/data-pipeline.md Part 3).

Run manually, then execute the 'vector_db' tag in Dataform:
    uv run python scripts/build_vector_db.py
"""
import hashlib
import pathlib

from google.cloud import bigquery
from pydantic import BaseModel
from unstructured.chunking.title import chunk_by_title
from unstructured.partition.html import partition_html
from unstructured.partition.md import partition_md

CONTEXT_ROOT = pathlib.Path("context")

# Which document each known file represents. Explicit rather than inferred
# from the filename -- there's one file today, and a naming-inference
# scheme for files that don't exist yet is exactly the premature
# generality this project avoids. Add a line here when a second doc lands.
DOC_SOURCES = {
    "index.html": "readme",
}

# Excludes the Executive Summary / Project Navigator / System Architecture
# block -- already served statically from context/orientation/, so indexing
# it would waste a retrieval slot. By heading name, not a line range, so it
# survives future edits to the document. Harmless no-op for any file that
# doesn't contain these exact headings.
EXCLUDE_START_TITLE = "Executive Summary"
EXCLUDE_END_TITLE = "Environment Setup & Provisioning"


class Chunk(BaseModel):
    chunk_id: str
    doc_source: str
    file_path: str
    section: str | None
    length: int
    chunk_text: str


def _chunk_id(file_path: str, text: str) -> str:
    """Content-addressed -> identical ids for unchanged content, so reloads
    are idempotent instead of duplicating rows."""
    return hashlib.sha256(f"{file_path}:{text}".encode()).hexdigest()[:16]


def chunk_docs(path: pathlib.Path, doc_source: str) -> list[Chunk]:
    """Structure-aware chunking that respects headings, so a chunk is a
    coherent section. Prepends the heading breadcrumb to chunk_text.
    """
    partition = partition_html if path.suffix == ".html" else partition_md
    elements = partition(filename=str(path))

    filtered = []
    skipping = False
    for el in elements:
        is_top_title = el.category == "Title" and el.metadata.category_depth == 0
        if is_top_title and str(el) == EXCLUDE_START_TITLE:
            skipping = True
        if is_top_title and str(el) == EXCLUDE_END_TITLE:
            skipping = False
        if not skipping:
            filtered.append(el)

    # Build a heading-breadcrumb stack from the pre-chunked elements.
    # chunk_by_title decides WHERE to break; it doesn't hand back a
    # breadcrumb, so the stack has to be tracked here.
    breadcrumbs: dict[str, str] = {}
    stack: list[str] = []
    for el in filtered:
        depth = el.metadata.category_depth or 0
        if el.category == "Title":
            stack[depth:] = [str(el)]
        breadcrumbs[el.id] = " > ".join(stack)

    chunks = []
    for c in chunk_by_title(filtered, max_characters=1500, overlap=150):
        # chunk_by_title's own chunk ids don't match any source element id
        # (confirmed against this library version) -- use the chunk's first
        # ORIGINAL element instead, which does.
        first_orig = c.metadata.orig_elements[0]
        crumb = breadcrumbs.get(first_orig.id, "")
        text = f"[{crumb}]\n{c}" if crumb else str(c)
        chunks.append(Chunk(
            chunk_id=_chunk_id(path.as_posix(), text),
            doc_source=doc_source,
            file_path=path.as_posix(),
            section=crumb or None,
            length=len(text),
            chunk_text=text,
        ))
    return chunks


def load_chunks(chunks: list[Chunk], table: str = "staging.doc_chunks") -> None:
    """Writes every chunk to the ONE staging table Dataform reads.

    Call this once with the full combined list across all files -- calling
    it per-file means each call WRITE_TRUNCATEs over the last, leaving only
    the final file's chunks in staging.
    """
    client = bigquery.Client()
    client.load_table_from_json(
        [c.model_dump() for c in chunks], table,
        job_config=bigquery.LoadJobConfig(
            write_disposition="WRITE_TRUNCATE",
            schema=[
                bigquery.SchemaField("chunk_id", "STRING"),
                bigquery.SchemaField("doc_source", "STRING"),
                bigquery.SchemaField("file_path", "STRING"),
                bigquery.SchemaField("section", "STRING"),
                bigquery.SchemaField("length", "INTEGER"),
                bigquery.SchemaField("chunk_text", "STRING"),
            ])).result()


def main() -> None:
    all_chunks: list[Chunk] = []
    for filename, doc_source in DOC_SOURCES.items():
        path = CONTEXT_ROOT / "docs" / filename
        all_chunks.extend(chunk_docs(path, doc_source=doc_source))

    # Empty means chunking silently produced nothing -- which would build an
    # EMPTY chunks_docs_embedded with no error anywhere, and search_docs tool
    # would silently return nothing on every call.
    if not all_chunks:
        raise SystemExit("Refusing to load: 0 chunks. Check context/docs/.")

    print(f"Chunked {len(all_chunks)} pieces from context/docs/.")
    load_chunks(all_chunks)
    print("Loaded to staging.doc_chunks. Now execute the 'vector_db' tag in Dataform.")


if __name__ == "__main__":
    main()
