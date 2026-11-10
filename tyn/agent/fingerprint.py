"""A short, stable hash of everything that shapes the model's instructions.

Checkpoints record it so ``--resume`` and ``--only`` never combine results
produced under different system prompts or tool schemas.
"""

import hashlib
import json
from typing import Final

from tyn.agent.prompts import (
    DISCOVERY_SUBMIT,
    DISCOVERY_SYSTEM,
    INVESTIGATION_SYSTEM,
    RECORD_FINDING,
    VERDICT_SUBMIT,
)
from tyn.agent.tools import SCHEMAS

FINGERPRINT_CHARS: Final = 12


def prompt_fingerprint() -> str:
    """Return a 12-character hash of the system prompts and tool schemas."""
    material = {
        "discovery_system": DISCOVERY_SYSTEM,
        "investigation_system": INVESTIGATION_SYSTEM,
        "tools": SCHEMAS,
        "submit": [DISCOVERY_SUBMIT, VERDICT_SUBMIT, RECORD_FINDING],
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:FINGERPRINT_CHARS]
