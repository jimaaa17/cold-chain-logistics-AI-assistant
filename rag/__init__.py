"""Cold-Chain SOP RAG platform.

The package is split along the read/write boundary so each side can be deployed,
scaled and secured independently:

* ``rag.core``       - shared building blocks (embeddings, Pinecone gateway, retries, models).
* ``rag.ingestion``  - offline write path: parse -> chunk -> embed -> upsert (batch job / CLI).
* ``rag.retrieval``  - online read path: stateless HTTP retrieval service + agent client/tool.
"""

