"""Parse Fannie Mae ILLD files into the common parquet schema.

Usage (from repo root):
    uv run python -m src.parse_fannie [--file PATH] [--force]

All format specifics (delimiter, field positions, score-model codes) come
from sources.fannie.layout in config/settings.yaml. The parser refuses to
run until that layout is filled in from the confirmed Fannie file spec —
it never guesses field positions. Fannie-specific quirks that can't be
expressed in the layout config belong here.
"""

from src.parse_base import run

if __name__ == "__main__":
    run("fannie")
