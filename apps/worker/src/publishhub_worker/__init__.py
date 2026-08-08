"""
PublishHub background worker.

Currently contains the queue abstraction (spec task 2.3); configuration loading,
the job processing loop, retry and dead-lettering, and graceful shutdown arrive
with spec task 4.
"""

__all__ = ["queue"]
