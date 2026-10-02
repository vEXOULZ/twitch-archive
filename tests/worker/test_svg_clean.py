"""Cleaning an uploaded site tag shape (svg_clean.py): what refuses the file, what is dropped, what stays."""

import re
from xml.etree import ElementTree as ET

import pytest

from archive_worker.svg_clean import SVG_NS, UnsafeSvg, clean, digest

OPEN = '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 24 24">'


def svg(inner: str) -> bytes:
    return f"{OPEN}{inner}</svg>".encode()


@pytest.mark.parametrize("raw, msg", [
    (svg("<script>alert(1)</script>"), "<script>"),
    (svg('<script xmlns="http://example.com/x">alert(1)</script>'), "<script>"),  # any namespace
    (OPEN.replace("<svg ", '<svg onload="alert(1)" ').encode() + b"</svg>", "event handlers"),
    (svg('<path d="M0 0" onclick="alert(1)"/>'), "event handlers"),
    (svg('<g><path d="M0 0" ONMOUSEOVER="x()"/></g>'), "event handlers"),
    (svg('<foreignObject><div xmlns="http://www.w3.org/1999/xhtml">hi</div></foreignObject>'), "<foreignObject>"),
    (svg('<image href="data:image/png;base64,iVBORw0KGgo="/>'), "<image>"),
    (svg('<image href="http://example.com/a.png"/>'), "<image>"),
    (svg('<iframe src="http://example.com"/>'), "<iframe>"),
    (svg('<a href="#x"><path d="M0 0"/></a>'), "<a>"),
    (svg('<animate attributeName="href" to="javascript:alert(1)"/>'), "<animate>"),
    (svg('<set attributeName="fill" to="red"/>'), "<set>"),
    (svg('<use href="http://example.com/s.svg#a"/>'), "links may only point inside"),
    (svg('<use xlink:href="javascript:alert(1)"/>'), "links may only point inside"),
    (svg('<use href="//example.com/s.svg#a"/>'), "links may only point inside"),
    (svg('<path d="M0 0" fill="url(http://example.com/x.svg#g)"/>'), "url()"),
    (svg('<path d="M0 0" fill="url( \'https://example.com/g\' )"/>'), "url()"),
    (svg('<path d="M0 0" style="fill: url(data:image/svg+xml;base64,AAAA)"/>'), "url()"),
    (svg("<style>@import 'https://example.com/a.css';</style>"), "@import"),
    (svg("<style>path { fill: url(https://example.com/x) }</style>"), "url()"),
    (svg("<style>@\\69mport 'x';</style>"), "escapes"),
    (svg('<path d="M0 0" style="background: javascript:alert(1)"/>'), "javascript:"),
    # Kept or dropped, an unsafe element refuses the file: here inside metadata that would be dropped.
    (svg('<metadata><script>alert(1)</script></metadata>'), "<script>"),
])
def test_refused(raw, msg):
    with pytest.raises(UnsafeSvg, match=re.escape(msg)):
        clean(raw)


@pytest.mark.parametrize("raw", [
    # Billion laughs.
    b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">]>'
    + svg("<title>&lol2;</title>"),
    # External entity.
    b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>' + svg("<title>&xxe;</title>"),
    # A DOCTYPE alone.
    b'<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">' + svg(""),
])
def test_doctype_and_entities_refused(raw):
    with pytest.raises(UnsafeSvg, match="DOCTYPE"):
        clean(raw)


@pytest.mark.parametrize("raw, msg", [
    (b"<svg", "not well-formed"),
    (b"", "not well-formed"),
    (b'<html xmlns="http://www.w3.org/1999/xhtml"/>', "root element"),
    (b"<svg/>", "root element"),  # no namespace: not an SVG to a browser either
])
def test_not_an_svg(raw, msg):
    with pytest.raises(UnsafeSvg, match=msg):
        clean(raw)


def test_inside_references_pass():
    out = clean(svg(
        '<defs><linearGradient id="g"><stop offset="0" stop-color="#fff"/></linearGradient>'
        '<path id="a" d="M0 0L10 10"/></defs>'
        '<use href="#a"/><use xlink:href="#a"/><rect width="4" height="4" fill="url(#g)" style="fill: url( #g )"/>'
        "<style>.x { fill: url(#g) }</style>"
    ))
    root = ET.fromstring(out)
    uses = root.findall(f"{{{SVG_NS}}}use")
    assert [u.get("href") for u in uses] == ["#a", "#a"]  # xlink:href is now SVG 2's href
    assert root.find(f"{{{SVG_NS}}}rect").get("fill") == "url(#g)"
    assert root.find(f"{{{SVG_NS}}}style").text == ".x { fill: url(#g) }"


def test_editor_metadata_dropped():
    raw = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<!-- made in Inkscape -->\n'
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
        'xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd" '
        'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" width="24" height="24" viewBox="0 0 24 24" '
        'inkscape:version="1.3" sodipodi:docname="star.svg" data-x="1">'
        '<metadata><rdf:RDF><rdf:Description/></rdf:RDF></metadata>'
        '<sodipodi:namedview pagecolor="#fff"/>'
        '<?inkscape something?>'
        '<g inkscape:label="Layer 1" inkscape:groupmode="layer"><path d="M12 2L15 9H22L16 14L18 21L12 17L6 21L8 14L2 9H9Z" '
        'fill="#000" sodipodi:nodetypes="ccccc"/></g>'
        '<text x="0" y="10">dropped</text>'
        "</svg>"
    ).encode()
    out = clean(raw)
    for gone in ("inkscape", "sodipodi", "rdf", "metadata", "data-x", "<!--", "<?", "text", "dropped"):
        assert gone not in out
    root = ET.fromstring(out)  # still well-formed, an SVG, the outline kept
    assert root.tag == f"{{{SVG_NS}}}svg" and root.get("viewBox") == "0 0 24 24" and root.get("width") == "24"
    path = root.find(f"{{{SVG_NS}}}g/{{{SVG_NS}}}path")
    assert path is not None and path.get("d").startswith("M12 2") and path.get("fill") == "#000"
    assert out.startswith('<svg xmlns="http://www.w3.org/2000/svg"')
    assert clean(out.encode()) == out  # cleaning again changes nothing


def test_digest_follows_the_content():
    a, b = clean(svg('<path d="M0 0"/>')), clean(svg('<path d="M1 1"/>'))
    assert digest(a) == digest(a) and digest(a) != digest(b) and len(digest(a)) == 16


def test_colors_kept():
    """The site draws the file in its own colors, recoloring only currentColor (or black) parts."""
    raw = svg(
        '<defs><linearGradient id="g"><stop offset="0" stop-color="#ff0"/><stop offset="1" style="stop-color: red"/>'
        '</linearGradient></defs>'
        '<path d="M0 0" fill="currentColor" stroke="#12345678"/><path d="M1 1" style="fill: currentColor; stroke: '
        'rgb(0, 0, 0)"/><g color="blue"><rect width="1" height="1" fill="url(#g)"/></g>'
        "<style>.a { fill: currentColor } .b { stroke: hsl(10 50% 50%) }</style>"
    )
    out = clean(raw)
    for kept in ('fill="currentColor"', 'stroke="#12345678"', 'stop-color="#ff0"', 'style="stop-color: red"',
                 'style="fill: currentColor; stroke: rgb(0, 0, 0)"', 'color="blue"', 'fill="url(#g)"',
                 ".a { fill: currentColor } .b { stroke: hsl(10 50% 50%) }"):
        assert kept in out, kept
