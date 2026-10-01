import os
from io import BytesIO

import pytest

from fontTools.cffLib import CFFFontSet, SubrsIndex
from fontTools.cffLib.transforms import desubroutinize, subroutinize
from fontTools.fontBuilder import FontBuilder
from fontTools.misc.psCharStrings import T2CharString
from fontTools.pens.recordingPen import RecordingPen
from fontTools.ttLib import TTFont

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

# A box-drawing sequence shared by several glyphs.
BOX = [
    100,
    100,
    "rmoveto",
    300,
    0,
    "rlineto",
    0,
    300,
    "rlineto",
    -300,
    0,
    "rlineto",
    0,
    -300,
    "rlineto",
]
TAIL = [50, 400, "rmoveto", 200, 0, "rlineto", 0, 200, "rlineto"]
HINTS = [50, 100, "hstem", 60, 100, "vstem", "hintmask", b"\xf0"]


def make_font(programs, private_dict=None):
    fb = FontBuilder(1000, isTTF=False)
    fb.setupGlyphOrder(list(programs))
    fb.setupCharacterMap({65 + i: n for i, n in enumerate(programs) if n != ".notdef"})
    fb.setupNameTable({"familyName": "Test", "styleName": "Regular"})
    fb.setupPost()
    fb.setupHorizontalMetrics({n: (500, 0) for n in programs})
    fb.setupOS2()
    fb.setupHorizontalHeader()
    charstrings = {n: T2CharString(program=p) for n, p in programs.items()}
    fb.setupCFF("Test", {}, charstrings, private_dict or {})
    return fb.font


def draw_all(font):
    glyph_set = font.getGlyphSet()
    result = {}
    for name in font.getGlyphOrder():
        pen = RecordingPen()
        glyph_set[name].draw(pen)
        result[name] = (glyph_set[name].width, tuple(pen.value))
    return result


def get_programs(cff):
    top = cff[0]
    programs = {}
    for name in top.CharStrings.keys():
        cs = top.CharStrings[name]
        cs.decompile()
        programs[name] = list(cs.program)
    return programs


def save_and_reload(font):
    buf = BytesIO()
    font.save(buf)
    buf.seek(0)
    return TTFont(buf)


def all_subrs(cff):
    result = []
    for font_name in cff.fontNames:
        font = cff[font_name]
        privates = (
            [fd.Private for fd in font.FDArray]
            if hasattr(font, "FDArray")
            else [font.Private]
        )
        for private in privates:
            subrs = getattr(private, "Subrs", None)
            if subrs is not None:
                result.extend(subrs[i] for i in range(len(subrs)))
    result.extend(cff.GlobalSubrs[i] for i in range(len(cff.GlobalSubrs)))
    return result


class SubroutinizeTest:
    pass


def test_subroutinize_hints_and_outlines_preserved():
    programs = {
        ".notdef": ["endchar"],
        "A": [300] + HINTS + BOX + TAIL + ["endchar"],
        "B": [300] + HINTS + BOX + TAIL + ["endchar"],
        "C": [400] + BOX + ["endchar"],
    }
    font = make_font(programs, {"BlueValues": [-12, 0, 700, 712]})
    cff = font["CFF "].cff

    before = draw_all(font)
    original = get_programs(cff)
    buf = BytesIO()
    font.save(buf)
    size_before = len(buf.getvalue())

    subroutinize(cff)

    # Outlines, widths and hints are preserved.
    assert draw_all(font) == before
    # hintmask instructions stay in the charstrings (never extracted).
    assert "hintmask" in cff[0].CharStrings["A"].program

    # New subroutines are flat (no calls, so no recursion and no added
    # call depth) and properly terminated.
    subrs = all_subrs(cff)
    assert subrs
    for subr in subrs:
        subr.decompile()
        assert subr.program[-1] == "return"
        assert "callsubr" not in subr.program
        assert "callgsubr" not in subr.program

    # Desubroutinizing restores the exact original programs.
    desubroutinize(cff)
    assert get_programs(cff) == original

    # Subroutinizing again gives a smaller font that survives a
    # save/reload round-trip.
    subroutinize(cff)
    buf = BytesIO()
    font.save(buf)
    assert len(buf.getvalue()) < size_before
    buf.seek(0)
    font2 = TTFont(buf)
    assert draw_all(font2) == before
    private = font2["CFF "].cff[0].Private
    assert len(getattr(private, "Subrs", [])) > 0


def test_subroutinize_cid_multiple_fdarrays():
    ttx_path = os.path.join(DATA_DIR, "TestSubroutinizeCID.ttx")
    font = TTFont()
    font.importXML(ttx_path)
    cff = font["CFF "].cff

    before = draw_all(font)
    original = get_programs(cff)

    subroutinize(cff)

    assert draw_all(font) == before

    # Desubroutinizing restores the exact original programs.
    desubroutinize(cff)
    assert get_programs(cff) == original

    subroutinize(cff)
    top = cff[0]
    fd0_subrs = getattr(top.FDArray[0].Private, "Subrs", None)
    fd1_subrs = getattr(top.FDArray[1].Private, "Subrs", None)
    # The sequence shared within FD0 / FD1 only became a local subroutine,
    # the one shared across FDs became a global subroutine.
    assert fd0_subrs is not None and len(fd0_subrs) > 0
    assert fd1_subrs is not None and len(fd1_subrs) > 0
    assert len(cff.GlobalSubrs) > 0

    font2 = save_and_reload(font)
    assert draw_all(font2) == before
    top2 = font2["CFF "].cff[0]
    assert len(top2.FDArray[0].Private.Subrs) > 0
    assert len(top2.FDArray[1].Private.Subrs) > 0
    assert len(font2["CFF "].cff.GlobalSubrs) > 0


def test_subroutinize_no_gain_keeps_original():
    # Nothing repeats: the font must be left untouched.
    programs = {
        ".notdef": ["endchar"],
        "A": [300, 100, 100, "rmoveto", 300, 0, "rlineto", "endchar"],
        "B": [400, 200, 200, "rmoveto", 0, 300, "rlineto", "endchar"],
    }
    font = make_font(programs)
    cff = font["CFF "].cff
    original = get_programs(cff)

    subroutinize(cff)

    assert get_programs(cff) == original
    assert getattr(cff[0].Private, "Subrs", None) is None
    assert len(cff.GlobalSubrs) == 0


def test_subroutinize_short_repeat_not_extracted():
    # A 4-byte sequence repeated twice: the two calls plus the stored
    # subroutine cost more than the bytes saved, so it must not be
    # extracted.
    programs = {
        ".notdef": ["endchar"],
        "A": [100, 100, "rmoveto", 300, 0, "rlineto", 0, 300, "rlineto", "endchar"],
        "B": [200, 200, "rmoveto", 300, 0, "rlineto", 0, -300, "rlineto", "endchar"],
    }
    font = make_font(programs)
    cff = font["CFF "].cff
    original = get_programs(cff)

    subroutinize(cff)

    assert get_programs(cff) == original
    assert getattr(cff[0].Private, "Subrs", None) is None
    assert len(cff.GlobalSubrs) == 0


def test_subroutinize_bias_threshold_renumbers_calls():
    # 1239 existing local subroutines; extracting one more crosses the
    # 1240 threshold, switching the subr bias from 107 to 1131.  All
    # existing call operands must be re-encoded.
    programs = {
        ".notdef": ["endchar"],
        "A": BOX + ["endchar"],
        "B": BOX + ["endchar"],
        # Calls subr 0 with the old bias (0 - 107 = -107).
        "D": [100, 100, "rmoveto", -107, "callsubr", "endchar"],
    }
    font = make_font(programs)
    cff = font["CFF "].cff
    top = cff[0]
    private = top.Private
    subrs = SubrsIndex(globalSubrs=cff.GlobalSubrs, private=private)
    subrs.append(
        T2CharString(
            program=[10, 0, "rlineto", "return"],
            private=private,
            globalSubrs=cff.GlobalSubrs,
        )
    )
    for _ in range(1238):
        subrs.append(
            T2CharString(
                program=[5, 0, "rlineto", "return"],
                private=private,
                globalSubrs=cff.GlobalSubrs,
            )
        )
    private.Subrs = subrs
    assert len(subrs) == 1239

    before = draw_all(font)
    subroutinize(cff)

    assert len(private.Subrs) == 1240
    # The pre-existing call now uses the new bias (0 - 1131 = -1131) and
    # still draws the same outline.
    assert top.CharStrings["D"].program == [
        100,
        100,
        "rmoveto",
        -1131,
        "callsubr",
        "endchar",
    ]
    assert draw_all(font) == before

    font2 = save_and_reload(font)
    assert draw_all(font2) == before


def test_subroutinize_cff2_rejected():
    cff = CFFFontSet()
    cff.major = 2
    cff.fontNames = []
    with pytest.raises(ValueError):
        subroutinize(cff)


def test_subroutinize_existing_subrs_real_font():
    # A real font that already has subroutines: outlines must be
    # preserved and the result must survive a save/reload round-trip.
    font = TTFont(os.path.join(DATA_DIR, "LinLibertine_RBI.otf"))
    cff = font["CFF "].cff
    before = draw_all(font)

    subroutinize(cff)

    assert draw_all(font) == before
    font2 = save_and_reload(font)
    assert draw_all(font2) == before
