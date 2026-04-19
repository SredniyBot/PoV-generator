from __future__ import annotations

from pathlib import Path
import sys


# Support direct execution of this file from IDEs that run it as a script
# instead of as a package module (`python -m pov_generator`).
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from pov_generator.interfaces.cli import main
else:
    from .interfaces.cli import main


if __name__ == "__main__":
    main()
