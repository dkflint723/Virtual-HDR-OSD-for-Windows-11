"""The colour core may not reach into Windows or Qt.

docs/adr/0001-split.md moves eight modules into a package of their own, vhdr-color,
which the desktop app, a game LUT tool and an exporter all build on. That only works if
the core imports nothing but the standard library and itself. So this reads every import
statement in each core module, at any depth, from source -- a module never has to be
importable to be checked -- and refuses anything else.

Two imports still cross, and each is marked as an expected failure. When one moves, its
test passes, unittest reports an unexpected success, and the run fails until the marker
is deleted. test_nothing_else_crosses pins each crossing exactly, because an expected
failure passes however many imports cross and would otherwise hide a second one.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

PACKAGE_NAME = "sdr_hdr_profile_creator"
PACKAGE = Path(__file__).parents[1] / "src" / PACKAGE_NAME

CORE = (
    "curves",
    "delta_itp",
    "gamma_correction",
    "greyscale",
    "icc",
    "measure",
    "model",
    "patterns",
)

# In the standard library, and the platform in all but name.
PLATFORM_STDLIB = frozenset({"ctypes", "winreg", "msvcrt", "_winapi", "winsound", "subprocess"})

# Cut in Phase 2 of the ADR. Written as crossings() reports them.
KNOWN_CROSSINGS = {
    "measure": [".meter"],
    "patterns": [".hdr_display"],
}


def _own(name: str) -> str:
    """This package's modules as ".name", however they were imported."""
    if name == PACKAGE_NAME:
        return ".__init__"
    if name.startswith(PACKAGE_NAME + "."):
        return "." + name[len(PACKAGE_NAME) + 1:].split(".")[0]
    return name


def imports_in(source: str) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(_own(alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module == PACKAGE_NAME:
                found.update("." + alias.name for alias in node.names)
            elif node.level == 0:
                found.add(_own(node.module or ""))
            elif node.level > 1:
                # Above the package, which nothing in the core has any business doing.
                found.add("." * node.level + (node.module or ""))
            elif node.module:
                found.add("." + node.module.split(".")[0])
            else:
                found.update("." + alias.name for alias in node.names)
    return found


def crossings_in(source: str) -> list[str]:
    """Every import that leaves the core: another module of this package, anything
    outside the standard library, or the standard library's platform half."""
    crossing = []
    for name in imports_in(source):
        if name.startswith("."):
            if name[1:] not in CORE:
                crossing.append(name)
        elif name.split(".")[0] not in sys.stdlib_module_names or name.split(".")[0] in PLATFORM_STDLIB:
            crossing.append(name)
    return sorted(crossing)


def crossings(module: str) -> list[str]:
    return crossings_in((PACKAGE / f"{module}.py").read_text(encoding="utf-8"))


class ColourCoreBoundaryTests(unittest.TestCase):
    def assertStaysInTheCore(self, module: str) -> None:
        self.assertEqual([], crossings(module), f"{module}.py imports from outside the colour core")

    def test_curves(self):
        self.assertStaysInTheCore("curves")

    def test_delta_itp(self):
        self.assertStaysInTheCore("delta_itp")

    def test_gamma_correction(self):
        self.assertStaysInTheCore("gamma_correction")

    def test_greyscale(self):
        self.assertStaysInTheCore("greyscale")

    def test_icc(self):
        self.assertStaysInTheCore("icc")

    def test_model(self):
        self.assertStaysInTheCore("model")

    @unittest.expectedFailure
    def test_measure(self):
        """Imports MeterError and Reading from meter, which runs Argyll's spotread as a
        subprocess. Phase 2 moves both types into the core."""
        self.assertStaysInTheCore("measure")

    @unittest.expectedFailure
    def test_patterns(self):
        """Imports SCRGB_WHITE_NITS from hdr_display, the Direct3D swapchain. Phase 2
        moves the constant into the core."""
        self.assertStaysInTheCore("patterns")

    def test_nothing_else_crosses(self):
        for module in CORE:
            with self.subTest(module=module):
                self.assertEqual(KNOWN_CROSSINGS.get(module, []), crossings(module))

    def test_every_core_module_has_its_own_test(self):
        """A module added to CORE without one is checked only by the pin above, which a
        known crossing written for it would quietly satisfy."""
        for module in CORE:
            with self.subTest(module=module):
                self.assertTrue(callable(getattr(self, f"test_{module}", None)))


class CheckerTests(unittest.TestCase):
    """The checker itself. A boundary test whose parser missed an import would pass
    every module, and look exactly like a clean core."""

    def test_it_finds_every_way_out(self):
        source = (
            "import ctypes\n"
            "from PySide6.QtGui import QImage\n"
            "from .windows_api import enumerate_displays\n"
            "from sdr_hdr_profile_creator import app\n"
            "import sdr_hdr_profile_creator.hotkeys\n"
            "from .. import elsewhere\n"
            "def late():\n"
            "    import winreg\n"
            "try:\n"
            "    import numpy\n"
            "except ImportError:\n"
            "    pass\n"
        )
        self.assertEqual(
            ["..", ".app", ".hotkeys", ".windows_api", "PySide6.QtGui", "ctypes", "numpy", "winreg"],
            crossings_in(source),
        )

    def test_it_allows_the_standard_library_and_the_core(self):
        source = (
            "from __future__ import annotations\n"
            "import struct\n"
            "from dataclasses import dataclass\n"
            "from .gamma_correction import pq_eotf\n"
            "from . import greyscale\n"
            "from sdr_hdr_profile_creator.model import ModeState\n"
        )
        self.assertEqual([], crossings_in(source))


if __name__ == "__main__":
    unittest.main()
