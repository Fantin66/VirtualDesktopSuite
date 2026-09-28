# Third-Party Notices

This project bundles the following third-party software. Their license texts are
reproduced in full below, as required by their respective licenses.

---

## 1. pyvda

- **Version:** 0.6.0
- **Homepage:** https://github.com/mrob95/py-VirtualDesktopAccessor
- **Author:** Mike Roberts
- **License:** MIT
- **How it is used:** Vendored source in `libs/pyvda/`, and bundled into the
  released `.exe`. Provides the virtual desktop API surface
  (`VirtualDesktop.create()` / `.go()` / `.remove()`,
  `AppView(hwnd).move()` / `.switch_to()`).

```
Copyright (c) 2015, 2016 Jari Pennanen, 2020 Mike Roberts

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.  IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```

---

## 2. Pillow (PIL fork)

- **Version:** 12.3.0
- **Homepage:** https://python-pillow.org
- **License:** MIT-CMU
- **How it is used:** Vendored source in `libs/PIL/`, and bundled into the
  released `.exe`. Used to rasterize the desktop pills, the menu, and the
  preview images.

```
The Python Imaging Library (PIL) is

    Copyright © 1997-2011 by Secret Labs AB
    Copyright © 1995-2011 by Fredrik Lundh and contributors

Pillow is the friendly PIL fork. It is

    Copyright © 2010 by Jeffrey 'Alex' Clark and contributors

Like PIL, Pillow is licensed under the open source MIT-CMU License:

By obtaining, using, and/or copying this software and/or its associated
documentation, you agree that you have read, understood, and will comply
with the following terms and conditions:

Permission to use, copy, modify and distribute this software and its
documentation for any purpose and without fee is hereby granted,
provided that the above copyright notice appears in all copies, and that
both that copyright notice and this permission notice appear in supporting
documentation, and that the name of Secret Labs AB or the author not be
used in advertising or publicity pertaining to distribution of the software
without specific, written prior permission.

SECRET LABS AB AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH REGARD TO THIS
SOFTWARE, INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS.
IN NO EVENT SHALL SECRET LABS AB OR THE AUTHOR BE LIABLE FOR ANY SPECIAL,
INDIRECT OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM
LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE
OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR
PERFORMANCE OF THIS SOFTWARE.
```

> The text above is the canonical short form. The **full verbatim** license
> file shipped with the package is reproduced at
> [`licenses/pillow-LICENSE.txt`](licenses/pillow-LICENSE.txt) — copy that file
> into any binary release to be strictly compliant.

---

## 3. comtypes

- **Version:** 1.4.16
- **Homepage:** https://github.com/enthought/comtypes
- **License:** MIT
- **How it is used:** Vendored source in `libs/comtypes/`, and bundled into
  the released `.exe`. Transitive dependency of `pyvda`; provides the COM
  plumbing used to talk to the virtual desktop interfaces.

```
This software is OSI Certified Open Source Software.
OSI Certified is a certification mark of the Open Source Initiative.

Copyright (c) 2006-2013, Thomas Heller.
Copyright (c) 2014, Comtypes Developers.
All rights reserved.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```

---

## Not bundled, but credited

- **MaximizeToVirtualDesktop** by Scott Hanselman
  (https://github.com/shanselman/MaximizeToVirtualDesktop, MIT) — the
  "maximize to an exclusive full-screen desktop" feature, the
  `Ctrl+Alt+Shift+X` hotkey, and the `[MVD] <ProcessName>` desktop naming
  convention are borrowed from this project. Its C# code is not used; the
  implementation here is written from scratch in Python on top of `pyvda`.
  See the "Credits & Prior Art" section of `README.md`.
