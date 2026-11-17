"""Offline PP-OCRv5 Korean line recognition. No model downloads at runtime.

Input normalization and CTC decoding follow PaddleOCR / RapidOCR (Apache-2.0).
Only the selected survey header uses this recognizer; lot OCR remains Tesseract.
"""
from functools import lru_cache
import hashlib
import math
import os
from pathlib import Path
import sys
import unicodedata

MODEL_NAME = 'korean_PP-OCRv5_rec_mobile.onnx'
MODEL_SHA256 = 'cd6e2ea50f6943ca7271eb8c56a877a5a90720b7047fe9c41a2e541a25773c9b'
HEADER_ENGINE = 'ppocrv5-korean-' + MODEL_SHA256[:12] + '-line-v1'
MODEL_PATH = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)) / 'assets/ocr/ppocrv5' / MODEL_NAME


@lru_cache(maxsize=1)
def load_recognizer():
    import onnxruntime as ort
    if not MODEL_PATH.is_file():
        raise FileNotFoundError('토지소재 PP-OCRv5 모델이 없습니다. 모델이 포함된 배포 파일을 사용하세요.')
    if hashlib.sha256(MODEL_PATH.read_bytes()).hexdigest() != MODEL_SHA256:
        raise ValueError('토지소재 PP-OCRv5 모델이 손상됐습니다. 배포 파일을 다시 받으세요.')
    options = ort.SessionOptions()
    options.intra_op_num_threads = min(4, os.cpu_count() or 1)
    options.inter_op_num_threads = 1
    options.log_severity_level = 3
    session = ort.InferenceSession(str(MODEL_PATH), sess_options=options,
                                  providers=['CPUExecutionProvider'])
    characters = [''] + session.get_modelmeta().custom_metadata_map['character'].splitlines() + [' ']
    if session.get_outputs()[0].shape[-1] != len(characters):
        raise ValueError('PP-OCRv5 모델과 문자 사전이 일치하지 않습니다.')
    return session, characters


def decode_ctc(probabilities, characters):
    """Collapse adjacent CTC tokens; preserve repeated characters separated by blank."""
    indices = probabilities.argmax(axis=1)
    previous = -1
    text = []
    for index in indices:
        if index and index != previous:
            text.append(characters[int(index)])
        previous = index
    return unicodedata.normalize('NFC', ''.join(text)).strip()


def recognize_header(pix):
    """Read a horizontal address line, including any '번지 외 N필' suffix."""
    import numpy as np
    from PIL import Image
    if pix.width < 8 or pix.height < 8:
        return ''
    image = Image.frombytes('RGB', (pix.width, pix.height), pix.samples)
    # A line recognizer needs the text at useful height, not a tall mostly-white cell.
    ink = image.convert('L').point(lambda value: 255 if value < 220 else 0)
    bounds = ink.getbbox()
    if bounds is None:
        return ''
    image = image.crop(bounds)
    margin = max(2, round(image.height * .08))
    padded = Image.new('RGB', (image.width+2*margin, image.height+2*margin), 'white')
    padded.paste(image, (margin, margin))
    image = padded
    session, characters = load_recognizer()
    height = 48
    resized_width = math.ceil(height * image.width / image.height)
    # Bound the tensor instead of silently squeezing an excessively broad crop.
    if resized_width > 4096:
        raise ValueError('토지소재 영역이 너무 가늘고 깁니다. 주소 한 줄 전체를 여유 있게 지정하세요.')
    width = max(320, resized_width)
    image = image.resize((resized_width, height), Image.Resampling.BILINEAR)
    # PaddleOCR models use BGR, CHW, [-1,1], with zero padding on the right.
    values = np.asarray(image, dtype=np.float32)[:, :, ::-1].transpose(2, 0, 1) / 127.5 - 1
    tensor = np.zeros((1, 3, height, width), dtype=np.float32)
    tensor[0, :, :, :resized_width] = values
    probabilities = session.run(None, {session.get_inputs()[0].name: tensor})[0][0]
    return decode_ctc(probabilities, characters)
