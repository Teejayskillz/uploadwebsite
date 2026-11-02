"""
Test-only compatibility shim.

Python 3.14 broke ``copy(super())`` in ``BaseContext.__copy__``, which Django's
test client relies on when it stores rendered template contexts. Rebuild the
actual subclass so ``RenderContext`` keeps its ``push_state`` method.

Production runs Python 3.11, where this block does nothing.
"""

import sys

if sys.version_info >= (3, 14):  # pragma: no cover - version specific
    from django.template.context import BaseContext, Context

    def _patched_context_copy(self):
        duplicate = self.__class__.__new__(self.__class__)
        duplicate.__dict__.update(self.__dict__)
        duplicate.dicts = [mapping.copy() for mapping in self.dicts]
        return duplicate

    BaseContext.__copy__ = _patched_context_copy