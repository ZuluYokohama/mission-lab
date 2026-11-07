"""`python -m stonx_chart.feed`: serve the mock chart feed on loopback."""

import sys

from stonx_chart.feed.server import main

if __name__ == "__main__":
    sys.exit(main())
