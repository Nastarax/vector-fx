"""Print the saved release calendar (does not rebuild). Rebuild with
`python -m src.fetchers.release_calendar`."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.fetchers import release_calendar as rc

cal = rc.load_calendar()
if not cal:
    print("No calendar yet. Build it: python -m src.fetchers.release_calendar")
else:
    rc.print_calendar(cal)
