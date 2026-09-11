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

## ArgyllCMS

Colorimeter support runs ArgyllCMS's `spotread` as a separate program. No ArgyllCMS
source or binary is copied into, linked against, or redistributed with this project;
the user installs it themselves and points this app at it.

Project: https://www.argyllcms.com/ -- Copyright Graeme W. Gill

ArgyllCMS as a whole is licensed under the Affero GPL version 3, while its instrument
driver sources carry GPL version 2 or later. Neither reaches this project, because
invoking a program is not the same as incorporating it. Anyone wishing to *port* Argyll
code rather than run it should read those licences directly.
