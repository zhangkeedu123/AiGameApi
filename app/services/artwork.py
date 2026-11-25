import io
import json

from PIL import Image, ImageOps, UnidentifiedImageError

from app.services.levels import PNG_LIMIT, invalid, validate_upload


def generate(raw, name, pieces, rotate):
    if len(raw) > PNG_LIMIT:
        invalid("image", "图片最大 12 MiB")
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
    xs, ys = (400-image.width)//2, (450-image.height)//2
    bounds = (xs, ys, xs+image.width, ys+image.height)
    if min(image.size) < 8:
        invalid("image", "图片可见区域过窄")
    atlas = Image.new("RGBA", (400,450))
    atlas.paste(image, (xs,ys))
    mask = atlas.getchannel("A")
    rectangles = [bounds]
    while len(rectangles) < pieces:
        choices = sorted(rectangles, key=lambda r: (r[2]-r[0])*(r[3]-r[1]), reverse=True)
        split = None
        for rect in choices:
            x,y,r,b = rect
            vertical = [((x,y,r,cut),(x,cut,r,b)) for cut in range((y+b)//2-2, (y+b)//2+3)]
            horizontal = [((x,y,cut,b),(cut,y,r,b)) for cut in range((x+r)//2-2, (x+r)//2+3)]
            for pair in (horizontal + vertical if r-x >= b-y else vertical + horizontal):
                if all(min(q[2]-q[0],q[3]-q[1]) >= 3 and mask.crop(q).getbbox() for q in pair):
                    split = rect, pair
                    break
            if split:
                break
        if not split:
            invalid("pieces", "图片可见区域不足以生成这些碎片")
        rect, pair = split
        rectangles.remove(rect)
        rectangles.extend(pair)
    rectangles.sort(key=lambda rect: (rect[1],rect[0]))
    def poly(rect):
        x,y,r,b = rect
        return [x,y,r,y,r,b,x,b]
    shards = []
    for x,y,r,b in rectangles:
        cx,cy = (x+r)/2,(y+b)/2
        ix,iy = min(((u,v) for u in range(x,r) for v in range(y,b) if mask.getpixel((u,v))),
                    key=lambda q:(q[0]-cx)**2+(q[1]-cy)**2)
        shards.append({"cx":ix+0.5,"cy":iy+0.5,"box":[x,y,r,b],"poly":poly((x,y,r,b)),
                       "sprite":[x,y,r-x,b-y]})
    seams = []
    for i,(x,y,r,b) in enumerate(rectangles):
        for j,(u,v,w,z) in enumerate(rectangles[i+1:], i+1):
            if (r==u or w==x) and max(y,v)<min(b,z):
                k = r if r==u else x
                seams.append({"ids":[i,j],"poly":[k,max(y,v),k,min(b,z)]})
            if (b==v or z==y) and max(x,u)<min(r,w):
                k = b if b==v else y
                seams.append({"ids":[i,j],"poly":[max(x,u),k,min(r,w),k]})
    stream = io.BytesIO()
    atlas.save(stream, format="PNG", optimize=True)
    if stream.tell() > 196608:
        stream = io.BytesIO()
        atlas.quantize(colors=256, method=Image.Quantize.FASTOCTREE).save(stream, format="PNG", optimize=True)
    png = stream.getvalue()
    config = {"name":name,"difficulty":"线上关卡","story":"","rotations":3 if rotate else 0,
              "snapDistance":14,"poster":[0,0,400,450],"outline":poly(bounds),
              "shards":shards,"seams":seams,"points":[]}
    return png, validate_upload(png, json.dumps(config))
