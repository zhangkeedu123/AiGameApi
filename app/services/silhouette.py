from functools import lru_cache

from PIL import Image, ImageChops, ImageDraw

# Pixellation is limited to the 400 × 450 board. Shared crack geometry stays vector-based.
VISIBLE = [0] * 32 + [255] * 224
DIRECTIONS = {(1, 0): 0, (0, 1): 1, (-1, 0): 2, (0, -1): 3}
TURN_PRIORITY = {1: 0, 0: 1, 3: 2, 2: 3}


def piece_alpha(alpha, poly, box=None):
    bounds = box or (0, 0, alpha.width, alpha.height)
    left, top, _, _ = bounds
    pixels = alpha.crop(bounds)
    region = Image.new("L", pixels.size)
    points = [(int(x) - left, int(y) - top) for x, y in zip(poly[::2], poly[1::2])]
    ImageDraw.Draw(region).polygon(points, fill=255)
    return ImageChops.multiply(pixels, region)


def direction(start, end):
    return DIRECTIONS[(end[0] - start[0], end[1] - start[1])]


def pixel_contours(alpha, x_offset=0, y_offset=0):
    width, height = alpha.size
    pixels = alpha.point(VISIBLE).tobytes()
    edges = {}

    def add_edge(start, end):
        edges.setdefault(start, []).append(end)

    for i, visible in enumerate(pixels):
        if not visible:
            continue
        y, x = divmod(i, width)
        if y == 0 or not pixels[i - width]:
            add_edge((x, y), (x + 1, y))
        if x == width - 1 or not pixels[i + 1]:
            add_edge((x + 1, y), (x + 1, y + 1))
        if y == height - 1 or not pixels[i + width]:
            add_edge((x + 1, y + 1), (x, y + 1))
        if x == 0 or not pixels[i - 1]:
            add_edge((x, y + 1), (x, y))

    contours = []
    while edges:
        start = current = next(iter(edges))
        incoming = None
        points = []
        while True:
            options = edges[current]
            if len(options) == 1 or incoming is None:
                next_point = options.pop()
            else:
                next_point = min(options, key=lambda point: TURN_PRIORITY[(direction(current, point) - incoming) % 4])
                options.remove(next_point)
            if not options:
                del edges[current]
            outgoing = direction(current, next_point)
            if incoming != outgoing:
                points.append(current)
            current, incoming = next_point, outgoing
            if current == start:
                break
        if len(points) > 3:
            a, b, c = points[-1], points[0], points[1]
            if (a[0] == b[0] == c[0]) or (a[1] == b[1] == c[1]):
                points.pop(0)
        contours.append([value for x, y in points for value in (x + x_offset, y + y_offset)])
    return contours


@lru_cache(maxsize=32)
def legacy_contours(path, modified_ns, poster, shards):
    with Image.open(path) as image:
        x, y, w, h = poster
        alpha = image.crop((x, y, x + w, y + h)).convert("RGBA").getchannel("A")
    outlines = []
    for poly, box in shards:
        left, top, _, _ = box
        outlines.append(pixel_contours(piece_alpha(alpha, poly, box), left, top))
    return pixel_contours(alpha), outlines


def with_silhouettes(config, path):
    if not config.get("cutout") or config.get("contours"):
        return config
    try:
        modified = path.stat().st_mtime_ns
        shards = tuple((tuple(shard["poly"]), tuple(map(int, shard["box"]))) for shard in config["shards"])
        outline, pieces = legacy_contours(path, modified, tuple(config["poster"]), shards)
    except (OSError, ValueError):
        return config
    return {**config, "contours": outline, "shards": [
        {**shard, "contours": contours} for shard, contours in zip(config["shards"], pieces)
    ]}

