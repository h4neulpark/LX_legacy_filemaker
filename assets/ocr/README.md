# Offline OCR models

The app uses the Tesseract OCR engine included in PyMuPDF and bundles Korean and
English LSTM models. No external Tesseract executable or network connection is
needed at runtime. PDF contents stay on the computer.

Source: https://github.com/tesseract-ocr/tessdata_best

Pinned revision: `e12c65a915945e4c28e237a9b52bc4a8f39a0cec`

Model files are distributed under the Apache License 2.0; see `tessdata/LICENSE`.

| File | SHA-256 |
| --- | --- |
| kor.traineddata | f888d4038348a0c3d25151e7f452bda0d74ca275b18cab146798bcbb94084fff |
| eng.traineddata | 8280aed0782fe27257a68ea10fe7ef324ca0f8d85bd2fd145d1c2b560bcb66ba |

Models can be restored from:
`https://raw.githubusercontent.com/tesseract-ocr/tessdata_best/<revision>/<file>`.
