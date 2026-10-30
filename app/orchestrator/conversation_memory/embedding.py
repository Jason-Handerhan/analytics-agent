import re
from functools import lru_cache

from langchain_google_genai import GoogleGenerativeAIEmbeddings

EMBEDDING_MODEL = "gemini-embedding-001"

_CHART_LINK = re.compile(r"!?\[([^\]]*)\]\(https?://[^)\s]*/charts/[0-9a-f-]{36}\.png[^)\s]*\)")


@lru_cache
def get_embeddings_client() -> GoogleGenerativeAIEmbeddings:
    """Lazy, cached embeddings client. Reads GOOGLE_API_KEY from the environment."""
    return GoogleGenerativeAIEmbeddings(model=EMBEDDING_MODEL)


def build_embedding_text(
    title: str, topics: list[str], entities: list[str], question: str, answer_markdown: str,
) -> str:
    """The text embedded for one turn. Chart links shrink to their label so signed URLs stay out
    of the vector."""
    lines = []
    if title:
        lines.append(f"Title: {title}")
    if topics:
        lines.append(f"Topics: {', '.join(topics)}")
    if entities:
        lines.append(f"Entities: {', '.join(entities)}")
    lines.append(f"Question: {question}")
    answer = _CHART_LINK.sub(r"\1", answer_markdown)
    lines.append(f"Answer: {answer}")
    return "\n".join(lines)


async def embed_document(text: str) -> list[float]:
    """Embedding of a turn's text for storage."""
    vectors = await get_embeddings_client().aembed_documents([text], task_type="RETRIEVAL_DOCUMENT")
    return vectors[0]


async def embed_query(text: str) -> list[float]:
    """Embedding of a search query, comparable to the stored document embeddings."""
    return await get_embeddings_client().aembed_query(text, task_type="RETRIEVAL_QUERY")
