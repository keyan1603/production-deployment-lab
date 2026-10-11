"""Copy a clean snapshot of the AI System Design lab into vendor/system for container builds.

The images install that lab as a package. Until it is installed from its GitHub URL, the build uses the
local checkout: `git archive` exports only tracked files, so no virtual environment, `.env` or run output
can end up in an image.
"""

import io
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT.parent / "ai-system-design-lab"
DEST = ROOT / "vendor" / "system"

if DEST.exists():
    shutil.rmtree(DEST)
DEST.mkdir(parents=True)
data = subprocess.run(["git", "-C", str(SOURCE), "archive", "HEAD"], check=True, capture_output=True).stdout
tarfile.open(fileobj=io.BytesIO(data)).extractall(DEST)
print(f"vendored {sum(1 for _ in DEST.rglob('*') if _.is_file())} files from {SOURCE} into {DEST}")
