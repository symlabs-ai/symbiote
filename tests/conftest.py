"""Global test configuration."""

import os

# Enable dev mode for API auth middleware during tests
os.environ.setdefault("SYMBIOTE_DEV_MODE", "1")
