"""
Public surface of the worker's post record store.

Mirrors the write half of `apps/api/src/posts/index.ts`: the worker finishes post
records, it never creates or lists them.
"""

from .post_store import (
    DEFAULT_POST_STORE_KEYS,
    POST_STATUSES,
    TERMINAL_POST_STATUSES,
    PlatformResult,
    PlatformStatus,
    PostStatus,
    PostStore,
    PostStoreCommands,
    PostStoreKeys,
    RedisPostStore,
    encode_platform_results,
)

__all__ = [
    "DEFAULT_POST_STORE_KEYS",
    "POST_STATUSES",
    "TERMINAL_POST_STATUSES",
    "PlatformResult",
    "PlatformStatus",
    "PostStatus",
    "PostStore",
    "PostStoreCommands",
    "PostStoreKeys",
    "RedisPostStore",
    "encode_platform_results",
]
