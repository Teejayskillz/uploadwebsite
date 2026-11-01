"""Small test helpers.

``tempfile.TemporaryDirectory`` creates 0700 directories, which the local
sandbox refuses to write into. These helpers create ordinary directories inside
the project instead, so file-system tests behave the same as production code.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from django.conf import settings

SCRATCH_ROOT = Path(settings.BASE_DIR) / "_testtmp"


def make_scratch_dir() -> Path:
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    path = SCRATCH_ROOT / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    return path


def remove_scratch_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
