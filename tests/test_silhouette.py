import io
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image, ImageDraw

from app.services.artwork import generate
from app.services.silhouette import piece_alpha, pixel_contours, with_silhouettes


def contains(contours, x, y):
    inside = False
    for contour in contours:
        points = list(zip(contour[::2], contour[1::2]))
        for (ax, ay), (bx, by) in zip(points, points[1:] + points[:1]):
            if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
                inside = not inside
    return inside


class SilhouetteTest(unittest.TestCase):
    def test_holes_and_diagonally_touching_pixels(self):
        mask = Image.new("L", (8, 8))
        draw = ImageDraw.Draw(mask)
        draw.rectangle((1, 1, 5, 5), fill=255)
        draw.rectangle((2, 2, 4, 4), fill=0)
        mask.putpixel((6, 6), 255)
        contours = pixel_contours(mask)
        self.assertEqual(len(contours), 3)
        for y in range(8):
            for x in range(8):
                self.assertEqual(contains(contours, x + 0.5, y + 0.5), bool(mask.getpixel((x, y))))

    def test_new_and_saved_pieces_exclude_transparent_pixels(self):
        image = Image.new("RGBA", (140, 170))
        draw = ImageDraw.Draw(image)
        draw.polygon([(58, 30), (80, 30), (84, 54), (119, 92), (106, 133), (77, 152),
                      (39, 147), (23, 113), (34, 78), (56, 53)], fill="#3264aa")
        draw.ellipse((50, 90, 75, 115), fill=(0, 0, 0, 0))
        source = io.BytesIO()
        image.save(source, format="PNG")
        png, config = generate(source.getvalue(), "空心瓷器", 12, 3, 2, seed=31)
        with Image.open(io.BytesIO(png)) as atlas:
            alpha = atlas.convert("RGBA").getchannel("A")
        self.assertEqual(len(config["shards"]), 14)
        self.assertGreaterEqual(len(config["contours"]), 2)
        for shard in config["shards"]:
            left, top, right, bottom = map(int, shard["box"])
            visible = piece_alpha(alpha, shard["poly"], (left, top, right, bottom))
            self.assertTrue(shard["contours"])
            for y in range(0, visible.height, 7):
                for x in range(0, visible.width, 7):
                    self.assertEqual(
                        contains(shard["contours"], left + x + 0.5, top + y + 0.5),
                        visible.getpixel((x, y)) >= 32,
                    )
        older = deepcopy(config)
        del older["contours"]
        for shard in older["shards"]:
            del shard["contours"]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "atlas.png"
            path.write_bytes(png)
            self.assertEqual(with_silhouettes(older, path), config)
            self.assertNotIn("contours", older)

