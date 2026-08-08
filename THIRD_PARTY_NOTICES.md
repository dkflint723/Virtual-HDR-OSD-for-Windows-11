# Third-Party Notices

## PySide6 / Qt for Python

Virtual HDR OSD for Windows uses PySide6 / Qt for Python. Redistribution must comply with the applicable Qt and PySide6 licensing terms.

## PySide6-Fluent-Widgets

The graphical interface uses the PySide6 build of PyQt-Fluent-Widgets / PySide6-Fluent-Widgets by zhiyiYo. Review the upstream project license before redistribution, especially for commercial use.

Project: https://github.com/zhiyiYo/PyQt-Fluent-Widgets

## SDR-in-HDR gamma-correction reference

The optional Windows piecewise-sRGB to pure-gamma-2.2 correction is implemented from the publicly documented transfer-function method and current LUT-generator formulas in dylanraga's reference project:

https://github.com/dylanraga/win11hdr-srgb-to-gamma2.2-icm

Virtual HDR OSD does not bundle that project's ICC/ICM profile binaries, ArgyllCMS, or the community AutoHotkey script.
