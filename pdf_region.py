"""Page-relative selection geometry and text extraction, independent of Tk."""

from dataclasses import dataclass

import pymupdf

Point = tuple[float, float]
Bounds = tuple[float, float, float, float]


@dataclass(frozen=True)
class AddressRegion:
    page_index: int
    bounds: Bounds


def normalized_bounds(start: Point, end: Point) -> Bounds:
    x0, x1 = sorted((max(0.0, min(1.0, start[0])), max(0.0, min(1.0, end[0]))))
    y0, y1 = sorted((max(0.0, min(1.0, start[1])), max(0.0, min(1.0, end[1]))))
    return x0, y0, x1, y1


def canvas_to_relative(x: float, y: float, page_rect: pymupdf.Rect,
                       zoom: float, origin: Point = (10, 10)) -> Point:
    return (
        max(0.0, min(1.0, (x - origin[0]) / (page_rect.width * zoom))),
        max(0.0, min(1.0, (y - origin[1]) / (page_rect.height * zoom))),
    )


def move_bounds(bounds: Bounds, dx: float, dy: float) -> Bounds:
    x0, y0, x1, y1 = bounds
    dx = max(-x0, min(1.0 - x1, dx))
    dy = max(-y0, min(1.0 - y1, dy))
    return x0 + dx, y0 + dy, x1 + dx, y1 + dy


def region_pdf_rect(page: pymupdf.Page, bounds: Bounds) -> pymupdf.Rect:
    """Convert displayed (rotated) relative bounds into extraction coordinates."""
    x0, y0, x1, y1 = bounds
    rect = page.rect
    displayed = pymupdf.Rect(x0 * rect.width, y0 * rect.height,
                             x1 * rect.width, y1 * rect.height)
    return displayed * page.derotation_matrix


def extract_region_text(page: pymupdf.Page, bounds: Bounds) -> str:
    text = page.get_text("text", clip=region_pdf_rect(page, bounds), sort=True)
    return " ".join(text.split())
