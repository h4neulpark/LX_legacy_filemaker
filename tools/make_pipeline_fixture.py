"""Create tiny synthetic native/scanned PDFs for frozen-worker smoke checks."""
from pathlib import Path
import pymupdf


def main():
    output = Path('build')
    output.mkdir(exist_ok=True)
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        page.insert_text((60, 100), 'Seoul 123-4', fontsize=24)
        doc.save(output / 'native.pdf')
        pix = page.get_pixmap(dpi=150)
        with pymupdf.open() as scanned:
            page = scanned.new_page(width=600, height=800)
            page.insert_image(page.rect, pixmap=pix)
            scanned.save(output / 'scanned.pdf')


if __name__ == '__main__':
    main()
