"""Zero-admin AI meeting recorder.

Phase 1 of the engineering specification: a resilient, ordinary-user audio
acquisition layer that follows the user's audio environment instead of
depending on the meeting application.

The package never requests elevation and never bypasses an operating system
privacy control.  See ``meetingcap.permissions``.
"""

__version__ = "0.1.0"

SYSTEM = "SYSTEM"
MIC = "MIC"

__all__ = ["SYSTEM", "MIC", "__version__"]
