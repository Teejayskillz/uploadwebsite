"""
Test-only compatibility shim.

Python 3.14 changed default ``__copy__`` behaviour for ``BaseContext``, which
Django's test client relies on when it stores rendered template contexts. The
sibling ``url_shortener`` project carries the same patch. Production runs
Python 3.11, where this block does nothing.
"""

import sys

if sys.version_info >= (3, 14):  # pragma: no cover - version specific
    from django.template.context import BaseContext, Context

    def _patched_context_copy(self):
        duplicate = Context()
        duplicate.dicts = [mapping.copy() for mapping in self.dicts]
        return duplicate

    BaseContext.__copy__ = _patched_context_copy