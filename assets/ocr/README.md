# Offline OCR models

The app uses the Tesseract OCR engine included in PyMuPDF and bundles Korean and
English LSTM models. No external Tesseract executable or network connection is
needed at runtime. PDF contents stay on the computer.

Survey headers use **Korean PP-OCRv5 mobile** instead, via ONNX Runtime's CPU
provider. Existing PDF text is bypassed for these headers. Lot recognition and
the single-address report workflow retain Tesseract. Models ship inside the EXE;
neither recognizer downloads files or uses a cloud API at runtime.

## PP-OCRv5 Korean model

- Upstream: https://github.com/PaddlePaddle/PaddleOCR
- ONNX conversion: https://github.com/RapidAI/RapidOCR
- File: `ppocrv5/korean_PP-OCRv5_rec_mobile.onnx`
- Pinned distribution: `RapidAI/RapidOCR`, ModelScope revision `v3.10.0`
- Download: https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/onnx/PP-OCRv5/rec/korean_PP-OCRv5_rec_mobile.onnx
- SHA-256: `cd6e2ea50f6943ca7271eb8c56a877a5a90720b7047fe9c41a2e541a25773c9b`
- License: Apache-2.0, see `ppocrv5/LICENSE`.
- Preprocessing/CTC reference: RapidOCR's `ch_ppocr_rec/main.py` and `utils.py`.
  The app uses Pillow resizing and its own small CPU-only adapter instead of
  bundling the complete PaddlePaddle/RapidOCR pipeline.

## Tesseract models

Source: https://github.com/tesseract-ocr/tessdata_best

Pinned revision: `e12c65a915945e4c28e237a9b52bc4a8f39a0cec`

Model files are distributed under the Apache License 2.0; see `tessdata/LICENSE`.

| File | SHA-256 |
| --- | --- |
| kor.traineddata | f888d4038348a0c3d25151e7f452bda0d74ca275b18cab146798bcbb94084fff |
| eng.traineddata | 8280aed0782fe27257a68ea10fe7ef324ca0f8d85bd2fd145d1c2b560bcb66ba |

Models can be restored from:
`https://raw.githubusercontent.com/tesseract-ocr/tessdata_best/<revision>/<file>`.
