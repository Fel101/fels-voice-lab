"""Regenerate the original geometric app icon; needs Pillow only at development time."""
from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
scale = 4
image = Image.new("RGBA", (256 * scale, 256 * scale))
draw = ImageDraw.Draw(image)
def points(values):
    return [(x * scale, y * scale) for x, y in values]
draw.rounded_rectangle((32, 32, 992, 992), radius=224, fill="#121b2b")
draw.line(points([(100, 46), (156, 46)]), fill="#67e3cf", width=15 * scale)
draw.line(points([(110, 46), (110, 111), (64, 187), (61, 199), (68, 211),
                  (79, 214), (177, 214), (188, 211), (195, 199), (192, 187),
                  (146, 111), (146, 46)]), fill="#67e3cf", width=15 * scale, joint="curve")
draw.line(points([(82, 167), (103, 167), (113, 145), (128, 190), (143, 145),
                  (153, 167), (174, 167)]), fill="#f6c66c", width=10 * scale, joint="curve")
image = image.resize((256, 256), Image.Resampling.LANCZOS)
image.save(ROOT / "assets/voice-lab.ico", sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])
