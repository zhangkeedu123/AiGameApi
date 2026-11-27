import io
import json
import math
from collections import defaultdict
from random import Random, SystemRandom

from PIL import Image, ImageOps, UnidentifiedImageError

from app.services.levels import PNG_LIMIT, invalid, validate_upload
from app.services.silhouette import piece_alpha, pixel_contours


def clip(cell, a, b, offset):
    result = []
    for start, end in zip(cell[-1:] + cell[:-1], cell):
        u = a * start[0] + b * start[1] - offset
        v = a * end[0] + b * end[1] - offset
        if (u > 0) != (v > 0):
            t = u / (u - v)
            result.append((start[0] + t * (end[0] - start[0]), start[1] + t * (end[1] - start[1])))
        if v <= 0:
            result.append(end)
    return result


def cells_for(sites, boundary):
    cells = []
    for x, y in sites:
        cell = boundary
        for u, v in sites:
            if (u, v) != (x, y):
                cell = clip(cell, u - x, v - y, (u * u + v * v - x * x - y * y) / 2)
        points = [(round(a, 3), round(b, 3)) for a, b in cell]
        cells.append([point for i, point in enumerate(points) if point != points[i - 1]])
    return cells


def jagged_edges(cells, sites, random):
    edges = defaultdict(list)
    for i, cell in enumerate(cells):
        for start, end in zip(cell, cell[1:] + cell[:1]):
            edges[tuple(sorted((start, end)))].append(i)
    bends, seams = {}, []
    for (start, end), owners in edges.items():
        if len(owners) != 2:
            continue
        dx, dy = end[0] - start[0], end[1] - start[1]
        length = math.hypot(dx, dy)
        path = [start, end]
        if length >= 12:
            i, j = owners
            side = random.choice((i, j))
            # 在相邻凸多边形内部落折点，两块碎片共享完全相反的裂边路径。
            fraction = random.uniform(0.34, 0.66)
            mx, my = start[0] + dx * fraction, start[1] + dy * fraction
            sx, sy = sites[side]
            distance = math.hypot(sx - mx, sy - my)
            weight = min(0.25, length * random.uniform(0.10, 0.18) / max(distance, 1))
            point = (round(mx + (sx - mx) * weight, 3), round(my + (sy - my) * weight, 3))
            bends[(start, end)] = bends[(end, start)] = point
            path.insert(1, point)
        seams.append({"ids": owners, "poly": [coordinate for point in path for coordinate in point]})
    shapes = []
    for cell in cells:
        shape = []
        for start, end in zip(cell, cell[1:] + cell[:1]):
            shape.append(start)
            if (start, end) in bends:
                shape.append(bends[(start, end)])
        shapes.append(shape)
    return shapes, seams


def select_sites(mask, count, random):
    reduced = mask.point(lambda opacity: 255 if opacity >= 60 else 0)
    left, top, right, bottom = mask.getbbox()
    candidates = [(x, y) for y in range(top, bottom, 3) for x in range(left, right, 3) if reduced.getpixel((x, y))]
    random.shuffle(candidates)
    candidates = candidates[:3600]
    if len(candidates) < count * 5:
        invalid("pieces", "图片可见区域不足，请减少碎片数或更换图片")
    scores = [float("inf")] * len(candidates)
    chosen = random.randrange(len(candidates))
    sites = []
    for _ in range(count):
        x, y = candidates[chosen]
        sites.append((x, y))
        for i, (u, v) in enumerate(candidates):
            scores[i] = min(scores[i], (x - u) ** 2 + (y - v) ** 2)
        chosen = max(range(len(scores)), key=lambda i: scores[i] * random.uniform(0.72, 1.28))
    return sites


def describe_piece(mask, shape, center):
    poly = [coordinate for point in shape for coordinate in point]
    visible = piece_alpha(mask, poly)
    bounds = visible.getbbox()
    if bounds is None:
        invalid("pieces", "部分碎片没有可见像素，请减少碎片数")
    left, top, right, bottom = bounds
    cx, cy = center
    return {"cx": cx, "cy": cy, "box": bounds, "poly": poly,
            "contours": pixel_contours(visible.crop(bounds), left, top),
            "sprite": [left, top, right - left, bottom - top]}


def generate(raw, name, pieces, rotate_count, decoy_count=0, story="", seed=None):
    if len(raw) > PNG_LIMIT:
        invalid("image", "图片最大 12 MiB")
    if not 0 <= rotate_count <= pieces or not 0 <= decoy_count <= 16:
        invalid("pieces", "旋转片数不能超过拼合片数，干扰片最多 16 片")
    try:
        with Image.open(io.BytesIO(raw), formats=["PNG", "JPEG", "WEBP"]) as src:
            if max(src.size) > 4096 or getattr(src, "n_frames", 1) != 1:
                invalid("image", "需要边长不超过 4096 的单帧图片")
            image = ImageOps.contain(ImageOps.exif_transpose(src).convert("RGBA"), (384, 434))
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        invalid("image", "请选择可解码的 PNG、JPEG 或 WebP")
    alpha = image.getchannel("A").getbbox()
    if not alpha:
        invalid("image", "图片完全透明")
    image = image.crop(alpha)
    if min(image.size) < 8:
        invalid("image", "图片可见区域过窄")
    left, top = (400 - image.width) // 2, (450 - image.height) // 2
    right, bottom = left + image.width, top + image.height
    atlas = Image.new("RGBA", (400, 450))
    atlas.paste(image, (left, top))
    mask = atlas.getchannel("A")
    boundary = [(left, top), (right, top), (right, bottom), (left, bottom)]
    random = Random(seed if seed is not None else SystemRandom().getrandbits(64))
    sites = select_sites(mask, pieces, random)
    shapes, seams = jagged_edges(cells_for(sites, boundary), sites, random)
    shards = [describe_piece(mask, shape, center) for shape, center in zip(shapes, sites)]
    order = random.sample(range(pieces), pieces)
    for i in range(decoy_count):
        index = order[i % pieces]
        cx, cy = sites[index]
        scale = random.uniform(0.47, 0.71)
        shape = [(round(cx + (x - cx) * scale, 3), round(cy + (y - cy) * scale, 3)) for x, y in shapes[index]]
        shards.append(describe_piece(mask, shape, (cx, cy)))
    stream = io.BytesIO()
    atlas.save(stream, format="PNG", optimize=True)
    if stream.tell() > 196608:
        stream = io.BytesIO()
        atlas.quantize(colors=256, method=Image.Quantize.FASTOCTREE).save(stream, format="PNG", optimize=True)
    png = stream.getvalue()
    config = {"name": name, "difficulty": "瓷器拼合", "story": story, "rotations": 3 if rotate_count else 0,
              "rotate_count": rotate_count, "decoy_count": decoy_count, "cutout": True,
              "snapDistance": 14, "poster": [0, 0, 400, 450],
              "outline": [coordinate for point in boundary for coordinate in point], "contours": pixel_contours(mask),
              "shards": shards, "seams": seams, "points": []}
    return png, validate_upload(png, json.dumps(config))
