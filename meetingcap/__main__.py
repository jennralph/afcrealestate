"""Allow ``python -m meetingcap`` as an alternative to ``meeting_capture.py``."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
