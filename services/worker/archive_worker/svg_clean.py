"""Cleaning an uploaded site tag shape (PUT /admin/site/tags/{tag}/shape) before it is stored and served.

The SVG is parsed (defusedxml: no DOCTYPE, entities or external references), then checked and
rebuilt from an allow-list. Anything that could run, load or link something refuses the whole file:
``<script>``, ``<foreignObject>``, embedded pages and images, animations (they can set attributes),
``on*`` handlers, an ``href`` that isn't ``#fragment``, ``url()`` that isn't ``url(#…)``, ``@import``,
``javascript:``. Everything else not on the allow-list (editor metadata, other namespaces, comments)
is dropped. The site draws the shape as a CSS mask, so only its outline matters.
"""

from __future__ import annotations

import hashlib
import re
from xml.etree import ElementTree as ET

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"

# By local name, in any namespace.
REFUSED_ELEMENTS = {
    "script",
    "foreignObject",
    "iframe",
    "image",
    "feImage",
    "a",
    "animate",
    "animateMotion",
    "animateTransform",
    "animateColor",
    "set",
    "object",
    "embed",
    "audio",
    "video",
    "handler",
    "listener",
}
ELEMENTS = {
    "svg",
    "g",
    "path",
    "rect",
    "circle",
    "ellipse",
    "line",
    "polyline",
    "polygon",
    "defs",
    "clipPath",
    "mask",
    "linearGradient",
    "radialGradient",
    "stop",
    "use",
    "symbol",
    "title",
    "desc",
    "style",
}
TEXT_ELEMENTS = {"style", "title", "desc"}
ATTRIBUTES = {
    "id",
    "class",
    "style",
    "transform",
    "transform-origin",
    "viewBox",
    "preserveAspectRatio",
    "version",
    "width",
    "height",
    "x",
    "y",
    "x1",
    "y1",
    "x2",
    "y2",
    "cx",
    "cy",
    "r",
    "rx",
    "ry",
    "fx",
    "fy",
    "fr",
    "d",
    "points",
    "pathLength",
    "offset",
    "href",
    "fill",
    "fill-opacity",
    "fill-rule",
    "stroke",
    "stroke-width",
    "stroke-opacity",
    "stroke-linecap",
    "stroke-linejoin",
    "stroke-miterlimit",
    "stroke-dasharray",
    "stroke-dashoffset",
    "opacity",
    "color",
    "display",
    "visibility",
    "clip-path",
    "clip-rule",
    "mask",
    "stop-color",
    "stop-opacity",
    "gradientUnits",
    "gradientTransform",
    "spreadMethod",
    "clipPathUnits",
    "maskUnits",
    "maskContentUnits",
    "vector-effect",
    "paint-order",
    "shape-rendering",
}

# The lookahead also refuses a space or quote, so backing off the optional parts can't skip past the #.
_EXTERNAL_URL = re.compile(r"url\s*\(\s*['\"]?\s*(?![\s'\"#])", re.I)
_FORBIDDEN_TEXT = {
    "@import": re.compile(r"@import", re.I),
    "javascript:": re.compile(r"javascript\s*:", re.I),
    "expression()": re.compile(r"expression\s*\(", re.I),
}


class UnsafeSvg(ValueError):
    pass


def _split(name: str) -> tuple[str | None, str]:
    """``{ns}local`` -> (ns, local)."""
    if name.startswith("{"):
        ns, local = name[1:].split("}", 1)
        return ns, local
    return None, name


def _check_value(where: str, value: str) -> None:
    if _EXTERNAL_URL.search(value):
        raise UnsafeSvg(f"{where}: url() may only point inside the file (url(#id))")
    if "\\" in value:
        raise UnsafeSvg(f"{where}: escapes are not allowed")
    for label, pattern in _FORBIDDEN_TEXT.items():
        if pattern.search(value):
            raise UnsafeSvg(f"{where}: {label} is not allowed")


def _check(root: ET.Element) -> None:
    """Refuse the file for anything unsafe anywhere in it, kept or not."""
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        _, local = _split(el.tag)
        if local in REFUSED_ELEMENTS:
            raise UnsafeSvg(f"<{local}> is not allowed")
        for name, value in el.attrib.items():
            _, attr = _split(name)
            where = f"<{local} {attr}>"
            if attr.lower().startswith("on"):
                raise UnsafeSvg(f"{where}: event handlers are not allowed")
            if attr == "href" and not value.strip().startswith("#"):
                raise UnsafeSvg(f"{where}: links may only point inside the file (#id)")
            _check_value(where, value)
        if local == "style" and el.text:
            _check_value("<style>", el.text)


def _copy(el: ET.Element) -> ET.Element | None:
    ns, local = _split(el.tag) if isinstance(el.tag, str) else (None, "")
    if ns != SVG_NS or local not in ELEMENTS:
        return None
    out = ET.Element(local)  # plain names; clean() gives the root the SVG namespace back
    for name, value in el.attrib.items():
        attr_ns, attr = _split(name)
        if attr_ns == XLINK_NS and attr == "href":
            out.set("href", value)  # SVG 2's plain href
        elif attr_ns is None and attr in ATTRIBUTES:
            out.set(attr, value)
    if local in TEXT_ELEMENTS:
        out.text = el.text
    for child in el:
        kept = _copy(child)
        if kept is not None:
            out.append(kept)
    return out


def clean(raw: bytes) -> str:
    """The SVG rebuilt from what is allowed in it; ``UnsafeSvg`` if it isn't one, or isn't safe."""
    try:
        root = fromstring(raw, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except DefusedXmlException:
        raise UnsafeSvg("DOCTYPE, entities and external references are not allowed") from None
    except ET.ParseError as exc:
        raise UnsafeSvg(f"not well-formed XML: {exc}") from None
    if root.tag != f"{{{SVG_NS}}}svg":
        raise UnsafeSvg("the root element must be <svg> in the SVG namespace")
    _check(root)
    cleaned = _copy(root)
    assert cleaned is not None
    cleaned.attrib = {"xmlns": SVG_NS, **cleaned.attrib}
    return ET.tostring(cleaned, encoding="unicode")


def digest(svg: str) -> str:
    """The shape URL's ``?v=``."""
    return hashlib.sha256(svg.encode()).hexdigest()[:16]
