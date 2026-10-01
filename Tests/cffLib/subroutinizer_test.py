import os
from io import BytesIO

from fontTools.cffLib.subroutinizer import Command, _apply_local
from fontTools.fontBuilder import FontBuilder
from fontTools.misc.psCharStrings import T2CharString
from fontTools.pens.recordingPen import RecordingPen
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.ttLib import TTFont

DATA = os.path.join(os.path.dirname(__file__), "..", "subset", "data")
CID_FONT = os.path.join(DATA, "TestCID-Regular.ttx")
CFF_DATA = os.path.join(os.path.dirname(__file__), "data")


def draw(font):
    result = {}
    for name in font["CFF "].cff[0].CharStrings.keys():
        charstring = font["CFF "].cff[0].CharStrings[name]
        pen = RecordingPen()
        charstring.decompile()
        charstring.draw(pen)
        result[name] = (pen.value, charstring.width)
    return result


def test_subroutinize_multi_fd_hinted_cid_font():
    font = TTFont()
    font.importXML(CID_FONT)
    original = draw(font)

    cff = font["CFF "].cff
    cff.subroutinize()
    top = cff[0]

    assert top.GlobalSubrs
    assert any(
        "callgsubr" in charstring.program or "callsubr" in charstring.program
        for charstring in top.CharStrings.values()
    )

    blob = BytesIO()
    font.save(blob)
    blob.seek(0)
    reopened = TTFont(blob)
    rebuilt = draw(reopened)
    assert rebuilt == original


def test_no_benefit_leaves_charstrings_unchanged():
    def glyph(width, x):
        pen = T2CharStringPen(width, None)
        pen.moveTo((x, 0))
        pen.lineTo((x + 10, 0))
        pen.lineTo((x + 10, 10))
        pen.lineTo((x, 10))
        pen.closePath()
        return pen.getCharString()

    builder = FontBuilder(1000, isTTF=False)
    builder.setupGlyphOrder([".notdef", "a", "b"])
    a = glyph(500, 0)
    b = glyph(700, 25)
    builder.setupCFF(
        "Test",
        {"FullName": "Test"},
        {".notdef": glyph(500, 40), "a": a, "b": b},
        {},
    )
    cff = builder.font["CFF "].cff
    before_a = list(cff[0].CharStrings["a"].program)
    before_b = list(cff[0].CharStrings["b"].program)

    cff.subroutinize()

    assert len(cff[0].GlobalSubrs) == 0
    assert cff[0].CharStrings["a"].program == before_a
    assert cff[0].CharStrings["b"].program == before_b


def test_local_subroutines_are_scoped_to_one_fd():
    class Private:
        nominalWidthX = 0
        defaultWidthX = 0
        Subrs = []

    private = Private()
    global_subrs = []
    body = [Command("rlineto", (1, 2)), Command("hlineto", (3,))]
    records = []
    for fd_index in (0, 1):
        for _ in range(3):
            charstring = T2CharString(
                program=[], private=private, globalSubrs=global_subrs
            )
            commands = body if fd_index == 1 else list(reversed(body))
            records.append([charstring, list(commands), None, fd_index])

    result = _apply_local(1, records, private, global_subrs)
    assert result is not None
    subrs, _call_count = result
    assert len(subrs) == 1
    assert subrs[0].program == [1, 2, "rlineto", 3, "hlineto", "return"]
    for charstring, _, _, fd_index in records:
        if fd_index == 0:
            assert "callsubr" not in charstring.program


def test_cff2_is_not_subroutinized():
    font = TTFont()
    font.importXML(os.path.join(CFF_DATA, "TestFDSelect4.ttx"))
    cff = font["CFF2"].cff
    before = list(cff[0].CharStrings["A"].program)

    cff.subroutinize()

    assert list(cff[0].CharStrings["A"].program) == before
