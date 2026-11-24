"""Render the suite SVG into the Windows icon. Requires PySide6-Essentials only when run."""

import struct
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

ASSETS = Path(__file__).resolve().parents[1] / "assets"
SIZES = (16, 24, 32, 48, 64, 128, 256)
ICO_HEADER = struct.Struct("<HHH")
ICO_ENTRY = struct.Struct("<BBBBHHII")


def render_frame(renderer, size):
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter)
    painter.end()

    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    # WinForms needs bitmap frames for the small window icons. Use PNG for the largest frame.
    output_format = "PNG" if size == 256 else "ICO"
    if not image.save(buffer, output_format):
        raise RuntimeError(f"Could not render the {size}px icon")
    data = bytes(buffer.data())
    if output_format == "ICO":
        # Qt writes a single-image ICO. Keep its bitmap and put it in the shared directory below.
        *_, length, offset = ICO_ENTRY.unpack_from(data, ICO_HEADER.size)
        bitmap = bytearray(data[offset : offset + length])
        # Qt leaves the transparency mask's row padding uninitialized.
        mask_bytes = (size + 7) // 8
        row_bytes = (mask_bytes + 3) // 4 * 4
        padding = bytes(row_bytes - mask_bytes)
        for row in range(len(bitmap) - size * row_bytes, len(bitmap), row_bytes):
            bitmap[row + mask_bytes : row + row_bytes] = padding
        return bitmap
    return data


def main():
    source = ASSETS / "voice-lab.svg"
    renderer = QSvgRenderer(str(source))
    if not renderer.isValid():
        raise RuntimeError(f"Could not read {source}")

    header = ICO_HEADER.pack(0, 1, len(SIZES))
    offset = len(header) + ICO_ENTRY.size * len(SIZES)
    directory = bytearray()
    images = bytearray()
    for size in SIZES:
        frame = render_frame(renderer, size)
        side = size % 256  # ICO stores 256px as 0.
        directory.extend(ICO_ENTRY.pack(side, side, 0, 0, 1, 32, len(frame), offset))
        images.extend(frame)
        offset += len(frame)

    (ASSETS / "voice-lab.ico").write_bytes(header + directory + images)


if __name__ == "__main__":
    main()
