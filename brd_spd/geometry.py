"""IPC primitives. All input geometry is in mm; output follows CadHeader units."""
import math
from lxml import etree as E

NS = "http://webstds.ipc.org/2581"


def el(tag, **attrs):
    return E.Element(tag, {k: str(v) for k, v in attrs.items() if v is not None})


def sub(parent, tag, **attrs):
    node = el(tag, **attrs)
    parent.append(node)
    return node


def num(value):
    if not math.isfinite(value):
        raise ValueError("Non-finite geometry coordinate")
    return format(value, ".12g")


def xy(parent, tag, x, y, scale=1, **attrs):
    return sub(parent, tag, x=num(x * scale), y=num(y * scale), **attrs)


def ring(data, scale=1, tag="Polygon"):
    if len(data) < 6 or len(data) % 2:
        raise ValueError("A polygon requires at least three coordinate pairs")
    node = el(tag)
    xy(node, "PolyBegin", data[0], data[1], scale)
    for i in range(2, len(data), 2):
        xy(node, "PolyStepSegment", data[i], data[i + 1], scale)
    if data[-2:] != data[:2]:
        xy(node, "PolyStepSegment", data[0], data[1], scale)
    return node


def circle_ring(data, scale=1, tag="Cutout"):
    x, y, radius = data
    if radius <= 0:
        raise ValueError("Circle radius must be positive")
    node = el(tag)
    xy(node, "PolyBegin", x + radius, y, scale)
    for endx, endy in [(x, y + radius), (x - radius, y), (x, y - radius), (x + radius, y)]:
        sub(node, "PolyStepCurve", x=num(endx * scale), y=num(endy * scale),
            centerX=num(x * scale), centerY=num(y * scale), clockwise="false")
    return node


def primitive(kind, data, scale=1):
    if kind == "Circle":
        if data[0] <= 0:
            raise ValueError("Pad radius must be positive")
        return el("Circle", diameter=num(2 * data[0] * scale))
    if kind in ("Square", "Rectangle"):
        width, height = (data[0], data[0]) if kind == "Square" else data[:2]
        if width <= 0 or height <= 0:
            raise ValueError("Pad dimensions must be positive")
        return el("RectCenter", width=num(width * scale), height=num(height * scale))
    if kind == "Polygon":
        contour = el("Contour")
        contour.append(ring(data, scale))
        return contour
    raise ValueError(f"Unsupported pad primitive: {kind}")


def feature_set(net, feature, polarity="POSITIVE", x=0, y=0, scale=1):
    result = el("Set", net=net or None, polarity=polarity)
    fs = sub(result, "Features")
    xy(fs, "Location", x, y, scale)
    fs.append(feature)
    return result
