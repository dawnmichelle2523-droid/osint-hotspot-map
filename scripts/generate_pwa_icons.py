#!/usr/bin/env python3
"""Write the GeoRiskGlobal home-screen icons from Dawn's globe lockup.

The globe crop, favicon, and header assets are built together so the
installed icon matches the navbar. This entry point runs that builder.
"""

from __future__ import annotations

import runpy
from pathlib import Path


def main() -> None:
    runpy.run_path(str(Path(__file__).resolve().with_name("build_brand_assets.py")), run_name="__main__")


if __name__ == "__main__":
    main()
