from __future__ import annotations

import os
from pathlib import Path

# Platforms like Railway mount a persistent volume at a path given via this
# env var when one's attached to the service, so data written under it
# survives redeploys instead of living on the container's ephemeral
# filesystem. Local dev has no such volume, so DATA_DIR is just "." and
# everything lives relative to the working directory as before.
DATA_DIR = Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "."))
