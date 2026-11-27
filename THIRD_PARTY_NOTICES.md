# Third-party components

The release zip bundles the following components. Each remains under its own license.

| Component | License | Source |
|---|---|---|
| Python 3.10 runtime | PSF License | https://www.python.org/ |
| Qt for Python (PySide6 Essentials, Shiboken6) and Qt 6 | LGPL-3.0 | https://www.qt.io/qt-for-python |
| OpenCV (opencv-python-headless) | Apache-2.0 | https://github.com/opencv/opencv-python |
| NumPy | BSD-3-Clause | https://numpy.org/ |
| ONNX Runtime | MIT | https://github.com/microsoft/onnxruntime |
| RapidOCR (rapidocr_onnxruntime) and its bundled PaddleOCR models | Apache-2.0 | https://github.com/RapidAI/RapidOCR |
| PP-LCNet_x1_0_doc_ori model (`models/doc_ori.onnx`, from PaddleOCR / PaddleX) | Apache-2.0 | https://github.com/PaddlePaddle/PaddleOCR |
| img2pdf | LGPL-3.0 | https://gitlab.mister-muffin.de/josch/img2pdf |
| pikepdf (and QPDF) | MPL-2.0 (QPDF: Apache-2.0) | https://github.com/pikepdf/pikepdf |
| pypdf | BSD-3-Clause | https://github.com/py-pdf/pypdf |
| Pillow | MIT-CMU | https://python-pillow.org/ |
| Shapely (and GEOS) | BSD-3-Clause (GEOS: LGPL-2.1) | https://github.com/shapely/shapely |
| pyclipper | MIT | https://github.com/fonttools/pyclipper |
| PyYAML | MIT | https://github.com/yaml/pyyaml |
| pywin32 | PSF-2.0 | https://github.com/mhammond/pywin32 |
| lxml | BSD-3-Clause | https://lxml.de/ |

The LGPL libraries (Qt, img2pdf, GEOS) are shipped as separate, unmodified files in the `_internal` folder and can be replaced with other compatible builds.
