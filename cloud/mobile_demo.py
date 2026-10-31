"""Community Cloud entrypoint: session-isolated paper trading only."""

from pathlib import Path
import sys


# Community Cloud starts from the repository root; add it explicitly so the
# wrapper also works when launched by tools from a different working directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main  # noqa: E402


if __name__ == "__main__":
    main(hosted_demo=True)
