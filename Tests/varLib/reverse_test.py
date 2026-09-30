import io
from copy import deepcopy

import pytest

from fontTools.pens.recordingPen import RecordingPen
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables import otTables
from fontTools.ttLib.tables.otTables import VVAR
from fontTools.ttLib.tables.TupleVariation import TupleVariation
from fontTools.varLib import build
from fontTools.varLib.builder import (
    buildVarData,
    buildVarRegionList,
    buildVarStore,
)
from fontTools.varLib.reverse import reverseVariableFontAxis
from fontTools.varLib.varStore import NO_VARIATION_INDEX, VarStoreInstancer


DATA_DIR = "Tests/varLib/data"


def mutator_font():
    font = TTFont()
    font.importXML(f"{DATA_DIR}/MutatorSans_All_Variable.ttx")
    font["GDEF"].table.VarStore = None
    for tag in ("GSUB", "GPOS"):
        if tag in font:
            table = font[tag].table
            if getattr(table, "FeatureVariations", None) is not None:
                del table.FeatureVariations
    return font


def save_and_reload(font):
    data = io.BytesIO()
    font.save(data)
    data.seek(0)
    return TTFont(data)


def outline(font, glyph_name, location):
    pen = RecordingPen()
    font.getGlyphSet(location=location)[glyph_name].draw(pen)
    return pen.value


def axis_value(format, axis_index, value_name_id=0):
    result = otTables.AxisValue()
    result.Format = format
    result.Flags = 0
    result.ValueNameID = value_name_id
    if format == 1:
        result.AxisIndex = axis_index
        result.Value = 100
    elif format == 2:
        result.AxisIndex = axis_index
        result.NominalValue = 200
        result.RangeMinValue = 100
        result.RangeMaxValue = 300
    elif format == 3:
        result.AxisIndex = axis_index
        result.Value = 400
        result.LinkedValue = 600
    elif format == 4:
        result.AxisValueRecord = []
        for index, value in ((axis_index, 100), (1 - axis_index, 400)):
            record = otTables.AxisValueRecord()
            record.AxisIndex = index
            record.Value = value
            result.AxisValueRecord.append(record)
    return result


def test_reverse_axis_mirrors_stat_values():
    font = mutator_font()
    stat = font["STAT"].table
    axis_index = next(
        index
        for index, axis in enumerate(stat.DesignAxisRecord.Axis)
        if axis.AxisTag == "wght"
    )
    stat.AxisValueArray = otTables.AxisValueArray()
    stat.AxisValueArray.AxisValue = [
        axis_value(format, axis_index) for format in (1, 2, 3, 4)
    ]

    reversed_font = reverseVariableFontAxis(font, "wght")
    values = reversed_font["STAT"].table.AxisValueArray.AxisValue

    assert values[0].Value == 900
    assert (
        values[1].RangeMinValue,
        values[1].NominalValue,
        values[1].RangeMaxValue,
    ) == (700, 800, 900)
    assert (values[2].Value, values[2].LinkedValue) == (600, 400)
    assert [record.Value for record in values[3].AxisValueRecord] == [900, 400]


def test_reverse_axis_inplace_keeps_other_axes():
    font = mutator_font()
    result = reverseVariableFontAxis(font, "wght", inplace=True)

    assert result is font
    assert font["fvar"].axes[0].defaultValue == 0
    assert font["fvar"].axes[1].defaultValue == 1000


def test_reverse_axis_mirrors_metadata_avar_gvar_and_metrics():
    original = mutator_font()
    axis = next(axis for axis in original["fvar"].axes if axis.axisTag == "wght")
    axis.minValue = 0
    axis.defaultValue = 250
    axis.maxValue = 1000
    original["avar"] = newTable("avar")
    original["avar"].segments = {
        "wdth": {-1: -1, 0: 0, 1: 1},
        "wght": {-1: -1, -0.5: -0.75, 0: 0, 0.5: 0.25, 1: 1},
    }

    reversed_font = reverseVariableFontAxis(original, "wght")

    reversed_axis = next(
        axis for axis in reversed_font["fvar"].axes if axis.axisTag == "wght"
    )
    assert reversed_axis.minValue == 0
    assert reversed_axis.defaultValue == 750
    assert reversed_axis.maxValue == 1000
    assert reversed_font["fvar"].instances[1].coordinates["wght"] == 0
    assert reversed_font["avar"].segments["wght"] == {
        -1: -1,
        -0.5: -0.25,
        0: 0,
        0.5: 0.75,
        1: 1,
    }
    assert original["fvar"].axes[1].defaultValue == 250

    first_variation = next(iter(reversed_font["gvar"].variations.values()))
    wght_variation = next(
        variation for variation in first_variation if "wght" in variation.axes
    )
    assert wght_variation.axes["wght"] == (-1.0, -1.0, 0.0)

    original = save_and_reload(original)
    reversed_font = save_and_reload(reversed_font)
    for value in (100, 450, 750, 900):
        location = {"wdth": 400, "wght": value}
        mirrored = {"wdth": 400, "wght": 1000 - value}
        assert outline(reversed_font, "A", location) == outline(
            original, "A", mirrored
        )
        assert (
            reversed_font.getGlyphSet(location=location)["A"].width
            == original.getGlyphSet(location=mirrored)["A"].width
        )


def test_reverse_axis_transforms_shared_variation_store_once_and_leaves_other_axis():
    font = mutator_font()
    hvar = font["HVAR"].table
    shared_store = deepcopy(hvar.VarStore)
    hvar.VarStore = shared_store
    font["MVAR"].table.VarStore = shared_store
    vvar = newTable("VVAR")
    vvar.table = VVAR()
    vvar.table.Version = 0x00010000
    vvar.table.VarStore = shared_store
    font["VVAR"] = vvar
    original_regions = deepcopy(shared_store.VarRegionList.Region)

    reversed_font = reverseVariableFontAxis(font, "wght")

    assert reversed_font["HVAR"].table.VarStore is reversed_font["MVAR"].table.VarStore
    axis_index = next(
        index
        for index, axis in enumerate(reversed_font["fvar"].axes)
        if axis.axisTag == "wght"
    )
    other_index = 1 - axis_index
    for old_region, new_region in zip(
        original_regions,
        reversed_font["HVAR"].table.VarStore.VarRegionList.Region,
    ):
        old_axis = old_region.VarRegionAxis[axis_index]
        new_axis = new_region.VarRegionAxis[axis_index]
        assert (new_axis.StartCoord, new_axis.PeakCoord, new_axis.EndCoord) == (
            -old_axis.EndCoord,
            -old_axis.PeakCoord,
            -old_axis.StartCoord,
        )
        assert (
            new_region.VarRegionAxis[other_index].PeakCoord
            == old_region.VarRegionAxis[other_index].PeakCoord
        )

    assert reversed_font["VVAR"].table.VarStore is reversed_font["HVAR"].table.VarStore
    assert reversed_font["VVAR"].table.VarStore is not shared_store

    original = save_and_reload(font)
    reversed_font = save_and_reload(reversed_font)
    mvar_indexes = [record.VarIdx for record in original["MVAR"].table.ValueRecord]
    for value in (200, 500, 800):
        location = {"wdth": 0, "wght": value}
        mirrored = {"wdth": 0, "wght": 1000 - value}
        original_values = VarStoreInstancer(
            original["MVAR"].table.VarStore,
            original["fvar"].axes,
            original.normalizeLocation(mirrored),
        )
        reversed_values = VarStoreInstancer(
            reversed_font["MVAR"].table.VarStore,
            reversed_font["fvar"].axes,
            reversed_font.normalizeLocation(location),
        )
        assert [reversed_values[index] for index in mvar_indexes] == [
            original_values[index] for index in mvar_indexes
        ]


def test_reverse_axis_updates_colrv1_store():
    original, _, _ = build(f"{DATA_DIR}/TestVariableCOLR.designspace")
    reversed_font = reverseVariableFontAxis(original, "wght")

    region = reversed_font["COLR"].table.VarStore.VarRegionList.Region[0]
    assert region.VarRegionAxis[0].PeakCoord == -1

    var_indexes = [
        index
        for index in reversed_font["COLR"].table.VarIndexMap.mapping
        if index != NO_VARIATION_INDEX
    ]

    original = save_and_reload(original)
    reversed_font = save_and_reload(reversed_font)
    for value in (400, 475, 550, 700):
        location = {"wght": value}
        mirrored = {"wght": 1100 - value}
        original_store = VarStoreInstancer(
            original["COLR"].table.VarStore,
            original["fvar"].axes,
            original.normalizeLocation(mirrored),
        )
        reversed_store = VarStoreInstancer(
            reversed_font["COLR"].table.VarStore,
            reversed_font["fvar"].axes,
            reversed_font.normalizeLocation(location),
        )
        assert [reversed_store[index] for index in var_indexes] == [
            original_store[index] for index in var_indexes
        ]
        assert outline(reversed_font, "A.0", location) == outline(
            original, "A.0", mirrored
        )
        assert (
            reversed_font.getGlyphSet(location=location)["A.0"].width
            == original.getGlyphSet(location=mirrored)["A.0"].width
        )


def test_reverse_axis_rejects_unsupported_variation_store():
    font = mutator_font()
    region_list = buildVarRegionList(
        [{"wght": (0, 1, 1)}],
        ["wdth", "wght"],
    )
    font["GDEF"].table.Version = 0x00010003
    var_data = buildVarData([0], [[10]])
    font["GDEF"].table.VarStore = buildVarStore(region_list, [var_data])

    with pytest.raises(NotImplementedError, match="GDEF VarStore"):
        reverseVariableFontAxis(font, "wght")

    assert font["fvar"].axes[1].defaultValue == 0


def test_reverse_axis_rejects_cff2():
    original, _, _ = build(f"{DATA_DIR}/TestVariableCOLR.designspace")
    original["CFF2"] = newTable("CFF2")

    with pytest.raises(NotImplementedError, match="CFF2"):
        reverseVariableFontAxis(original, "wght")


def test_reverse_axis_rejects_avar2():
    font = mutator_font()
    font["avar"] = newTable("avar")
    font["avar"].majorVersion = 2
    font["avar"].minorVersion = 0
    font["avar"].segments = {
        "wdth": {-1: -1, 0: 0, 1: 1},
        "wght": {-1: -1, 0: 0, 1: 1},
    }

    with pytest.raises(NotImplementedError, match="avar version 2"):
        reverseVariableFontAxis(font, "wght")


def test_reverse_axis_rejects_cvar():
    font = mutator_font()
    cvar = font["cvar"] = newTable("cvar")
    cvar.version = 1
    cvar.variations = []

    assert reverseVariableFontAxis(font, "wght") is not font

    cvar.variations = [
        TupleVariation({"wght": (0, 1, 1)}, [0])
    ]
    with pytest.raises(NotImplementedError, match="cvar"):
        reverseVariableFontAxis(font, "wght")
