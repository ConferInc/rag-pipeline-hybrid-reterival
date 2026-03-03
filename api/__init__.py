"""
API package — FastAPI HTTP layer for B2C frontend integration.

This package wraps the core RAG pipeline (rag_pipeline/) as a REST API.
Dependency direction: api → rag_pipeline (one-way). The core pipeline
is never modified for API concerns.
"""
