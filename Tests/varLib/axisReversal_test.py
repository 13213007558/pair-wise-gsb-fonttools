import os
from io import StringIO

from fontTools import ttLib
from fontTools.ttLib import newTable
from fontTools.ttLib.tables import otTables
from fontTools.varLib import instancer
from fontTools.varLib.axisReversal import reverseAxisDirection
from fontTools.varLib.varStore import VarStoreInstancer

import pytest


TESTDATA = os.path.join(os.path.dirname(__file__), "data")
INSTANCER_TESTDATA = os.path.join(os.path.dirname(__file__), "instancer", "data")
SUBSET_TESTDATA = os.path.join(os.path.dirname(__file__), "..", "subset", "data")
TTLIB_TABLES_TESTDATA = os.path.join(
    os.path.dirname(__file__), "..", "ttLib", "tables", "data"
)

# wght 100..900, default 400 (not the midpoint); wdth 70..100, default 100.
# Non-linear avar on wght; gvar with intermediate and multi-axis tuples;
# HVAR and MVAR variation stores with regions spanning both axes; STAT
# AxisValues in formats 1, 2, 3 and 4; plus a cvar table.
PARTIAL_INSTANCER_VF = os.path.join(INSTANCER_TESTDATA, "PartialInstancerTest-VF.ttx")
# wght 100..900, default 400; non-linear avar; HVAR and VVAR tables.
HVVAR_VF = os.path.join(SUBSET_TESTDATA, "TestHVVAR.ttx")
# COLRv1 with a variation store; wght 400..700, default 400.
COLR_VF = os.path.join(TESTDATA, "test_results", "TestVariableCOLR-VF.ttx")
CFF2_VF = os.path.join(INSTANCER_TESTDATA, "CFF2Instancer-VF-1.ttx")
AVAR2_VF = os.path.join(TTLIB_TABLES_TESTDATA, "Amstelvar-avar2.subset.ttf")


def importFont(path):
    font = ttLib.TTFont()
    font.importXML(path)
    return font


@pytest.fixture
def varfont():
    font = importFont(PARTIAL_INSTANCER_VF)
    # cvar reversal is not supported (tested separately below)
    del font["cvar"]
    return font


def dumpXML(font, tables=None):
    s = StringIO()
    font.saveXML(s, tables=tables)
    return s.getvalue()


def varStoreDeltas(varStore, fvarAxes, location):
    inst = VarStoreInstancer(varStore, fvarAxes, location)
    return [
        inst[(major << 16) + minor]
        for major, varData in enumerate(varStore.VarData)
        for minor in range(len(varData.Item))
    ]


# The instancer sets OS/2.usWeightClass/usWidthClass from the user-space
# coordinates, which are mirrored by design; ignore those two fields.
STATIC_COMPARE_TABLES = ["glyf", "loca", "hmtx", "hhea", "OS/2", "post", "maxp", "name"]


def assertSameInstance(font1, font2):
    font1["OS/2"].usWeightClass = font2["OS/2"].usWeightClass = 0
    font1["OS/2"].usWidthClass = font2["OS/2"].usWidthClass = 0
    assert dumpXML(font1, STATIC_COMPARE_TABLES) == dumpXML(
        font2, STATIC_COMPARE_TABLES
    )


class FvarTest:
    def test_axis_default_mirrored_range_kept(self, varfont):
        reversed_font = reverseAxisDirection(varfont, "wght")

        wght, wdth = reversed_font["fvar"].axes
        # default 400 -> 100 + 900 - 400 = 600; min/max are NOT swapped
        assert (wght.minValue, wght.defaultValue, wght.maxValue) == (
            100.0,
            600.0,
            900.0,
        )
        # the other axis is untouched
        assert (wdth.minValue, wdth.defaultValue, wdth.maxValue) == (
            70.0,
            100.0,
            100.0,
        )

    def test_named_instances_mirrored(self, varfont):
        original = [dict(i.coordinates) for i in varfont["fvar"].instances]

        reversed_font = reverseAxisDirection(varfont, "wght")

        instances = reversed_font["fvar"].instances
        assert len(instances) == len(original)
        for originalCoords, instance in zip(original, instances):
            assert instance.coordinates["wght"] == 1000 - originalCoords["wght"]
            assert instance.coordinates["wdth"] == originalCoords["wdth"]


class AvarTest:
    def test_nonlinear_segment_mirrored(self, varfont):
        original = dict(varfont["avar"].segments["wght"])
        # sanity check: the fixture really exercises a non-linear mapping
        assert any(fro != to for fro, to in original.items())

        reversed_font = reverseAxisDirection(varfont, "wght")

        expected = {-fro: -to for fro, to in original.items()}
        assert reversed_font["avar"].segments["wght"] == expected
        # the resulting map is still monotonically increasing
        segment = reversed_font["avar"].segments["wght"]
        keys = sorted(segment)
        assert [segment[k] for k in keys] == sorted(segment.values())
        assert segment[-1.0] == -1.0 and segment[0.0] == 0.0 and segment[1.0] == 1.0

    def test_other_axis_segment_unchanged(self, varfont):
        original = dict(varfont["avar"].segments["wdth"])

        reversed_font = reverseAxisDirection(varfont, "wght")

        assert reversed_font["avar"].segments["wdth"] == original


class STATTest:
    def test_axis_values_mirrored(self, varfont):
        reversed_font = reverseAxisDirection(varfont, "wght")
        axisValues = reversed_font["STAT"].table.AxisValueArray.AxisValue

        # Format 1 (Thin): 100 -> 900
        assert axisValues[0].Format == 1
        assert axisValues[0].Value == 900.0
        # Format 3 (Regular): 400 -> 600, LinkedValue 700 -> 300
        assert axisValues[1].Format == 3
        assert axisValues[1].Value == 600.0
        assert axisValues[1].LinkedValue == 300.0
        # Format 1 (Medium): 500 -> 500
        assert axisValues[2].Format == 1
        assert axisValues[2].Value == 500.0
        # Format 2 (Black): nominal 900 -> 100, range [801, 900] -> [100, 199]
        assert axisValues[3].Format == 2
        assert axisValues[3].NominalValue == 100.0
        assert axisValues[3].RangeMinValue == 100.0
        assert axisValues[3].RangeMaxValue == 199.0

    def test_other_axes_values_unchanged(self, varfont):
        reversed_font = reverseAxisDirection(varfont, "wght")
        axisValues = reversed_font["STAT"].table.AxisValueArray.AxisValue

        # Format 4 (Condensed) references the wdth axis
        assert axisValues[4].Format == 4
        assert axisValues[4].AxisValueRecord[0].Value == 79.0
        # Format 3 (Upright) references the ital axis
        assert axisValues[5].Value == 0.0
        assert axisValues[5].LinkedValue == 1.0
        # Format 4 (Normal) references the wdth axis
        assert axisValues[6].AxisValueRecord[0].Value == 100.0


class GvarTest:
    def test_tuple_regions_mirrored(self, varfont):
        original = [
            dict(tv.axes) for tv in varfont["gvar"].variations["hyphen"]
        ]

        reversed_font = reverseAxisDirection(varfont, "wght")

        variations = reversed_font["gvar"].variations["hyphen"]
        assert len(variations) == len(original)
        for originalAxes, variation in zip(original, variations):
            for axisTag, triple in variation.axes.items():
                if axisTag == "wght":
                    start, peak, end = originalAxes[axisTag]
                    assert triple == (-end, -peak, -start)
                else:
                    # other axes keep their regions
                    assert triple == originalAxes[axisTag]

    @pytest.mark.parametrize("wght", [100, 250, 400, 550, 600, 725, 900])
    @pytest.mark.parametrize("wdth", [75, 100])
    def test_instances_match_mirrored_original(self, varfont, wght, wdth):
        reversed_font = reverseAxisDirection(varfont, "wght")

        expected = instancer.instantiateVariableFont(
            varfont, {"wght": 1000 - wght, "wdth": wdth}
        )
        actual = instancer.instantiateVariableFont(
            reversed_font, {"wght": wght, "wdth": wdth}
        )

        # compare outlines (gvar) and metrics (HVAR, MVAR)
        assertSameInstance(actual, expected)


class ReverseSecondAxisTest:
    def test_fvar_and_stat_format4_mirrored(self, varfont):
        reversed_font = reverseAxisDirection(varfont, "wdth")

        wght, wdth = reversed_font["fvar"].axes
        # wdth default 100 -> 70 + 100 - 100 = 70; wght untouched
        assert (wdth.minValue, wdth.defaultValue, wdth.maxValue) == (
            70.0,
            70.0,
            100.0,
        )
        assert (wght.minValue, wght.defaultValue, wght.maxValue) == (
            100.0,
            400.0,
            900.0,
        )

        axisValues = reversed_font["STAT"].table.AxisValueArray.AxisValue
        # Format 4 records reference the wdth axis: 79 -> 91, 100 -> 70
        assert axisValues[4].AxisValueRecord[0].Value == 91.0
        assert axisValues[6].AxisValueRecord[0].Value == 70.0
        # Format 1/2/3 records reference the wght axis: unchanged
        assert axisValues[0].Value == 100.0
        assert axisValues[1].Value == 400.0
        assert axisValues[3].NominalValue == 900.0

    @pytest.mark.parametrize("wdth", [70, 80, 90, 100])
    def test_instances_match_mirrored_original(self, varfont, wdth):
        reversed_font = reverseAxisDirection(varfont, "wdth")

        expected = instancer.instantiateVariableFont(
            varfont, {"wght": 250, "wdth": 170 - wdth}
        )
        actual = instancer.instantiateVariableFont(
            reversed_font, {"wght": 250, "wdth": wdth}
        )

        assertSameInstance(actual, expected)


class VHvarTest:
    @pytest.mark.parametrize("tableTag", ["HVAR", "VVAR"])
    def test_varstore_regions_mirrored(self, tableTag):
        font = importFont(HVVAR_VF)
        reversed_font = reverseAxisDirection(font, "wght")

        varStore = font[tableTag].table.VarStore
        reversedVarStore = reversed_font[tableTag].table.VarStore

        for location in [-1.0, -0.75, -0.3, 0.0, 0.2, 0.5, 1.0]:
            expected = varStoreDeltas(varStore, font["fvar"].axes, {"wght": location})
            actual = varStoreDeltas(
                reversedVarStore,
                reversed_font["fvar"].axes,
                {"wght": -location},
            )
            assert actual == pytest.approx(expected)


class MvarTest:
    def test_varstore_regions_mirrored(self, varfont):
        reversed_font = reverseAxisDirection(varfont, "wght")

        varStore = varfont["MVAR"].table.VarStore
        reversedVarStore = reversed_font["MVAR"].table.VarStore

        # the MVAR store has regions spanning both the wght and wdth axes
        for wght in [-1.0, -0.5, 0.0, 0.3, 1.0]:
            for wdth in [-1.0, 0.0]:
                location = {"wght": wght, "wdth": wdth}
                expected = varStoreDeltas(varStore, varfont["fvar"].axes, location)
                actual = varStoreDeltas(
                    reversedVarStore,
                    reversed_font["fvar"].axes,
                    {"wght": -wght, "wdth": wdth},
                )
                assert actual == pytest.approx(expected)


class ColrTest:
    def test_colrv1_varstore_mirrored(self):
        font = importFont(COLR_VF)
        reversed_font = reverseAxisDirection(font, "wght")

        # fvar default moved to the mirrored position
        assert reversed_font["fvar"].axes[0].defaultValue == 700.0

        # the single region (0, 1, 1) becomes (-1, -1, 0)
        region = reversed_font["COLR"].table.VarStore.VarRegionList.Region[0]
        regionAxis = region.VarRegionAxis[0]
        assert (regionAxis.StartCoord, regionAxis.PeakCoord, regionAxis.EndCoord) == (
            -1.0,
            -1.0,
            0.0,
        )

        varStore = font["COLR"].table.VarStore
        reversedVarStore = reversed_font["COLR"].table.VarStore
        for location in [-1.0, -0.4, 0.0, 0.25, 1.0]:
            expected = varStoreDeltas(varStore, font["fvar"].axes, {"wght": location})
            actual = varStoreDeltas(
                reversedVarStore,
                reversed_font["fvar"].axes,
                {"wght": -location},
            )
            assert actual == pytest.approx(expected)


class UnsupportedVariationTablesTest:
    def test_cvar_rejected(self):
        font = importFont(PARTIAL_INSTANCER_VF)
        assert "cvar" in font
        with pytest.raises(NotImplementedError, match="cvar"):
            reverseAxisDirection(font, "wght")

    def test_cff2_rejected(self):
        font = importFont(CFF2_VF)
        with pytest.raises(NotImplementedError, match="CFF2"):
            reverseAxisDirection(font, "wght")

    def test_avar2_rejected(self):
        font = ttLib.TTFont(AVAR2_VF)
        with pytest.raises(NotImplementedError, match="avar"):
            reverseAxisDirection(font, "wght")

    def test_gdef_varstore_rejected(self, varfont):
        gdef = newTable("GDEF")
        gdef.table = otTables.GDEF()
        gdef.table.VarStore = otTables.VarStore()
        varfont["GDEF"] = gdef
        with pytest.raises(NotImplementedError, match="GDEF"):
            reverseAxisDirection(varfont, "wght")

    def test_feature_variations_rejected(self, varfont):
        gsub = newTable("GSUB")
        gsub.table = otTables.GSUB()
        gsub.table.FeatureVariations = otTables.FeatureVariations()
        varfont["GSUB"] = gsub
        with pytest.raises(NotImplementedError, match="GSUB"):
            reverseAxisDirection(varfont, "wght")


class ReverseAxisDirectionTest:
    def test_missing_fvar(self):
        with pytest.raises(ValueError, match="fvar"):
            reverseAxisDirection(ttLib.TTFont(), "wght")

    def test_missing_axis(self, varfont):
        with pytest.raises(ValueError, match="XXXX"):
            reverseAxisDirection(varfont, "XXXX")

    def test_not_inplace_by_default(self, varfont):
        reversed_font = reverseAxisDirection(varfont, "wght")
        assert reversed_font is not varfont
        assert varfont["fvar"].axes[0].defaultValue == 400.0
        assert reversed_font["fvar"].axes[0].defaultValue == 600.0

    def test_inplace(self, varfont):
        result = reverseAxisDirection(varfont, "wght", inplace=True)
        assert result is varfont
        assert varfont["fvar"].axes[0].defaultValue == 600.0

    def test_roundtrip_restores_original(self, varfont):
        once = reverseAxisDirection(varfont, "wght")
        twice = reverseAxisDirection(once, "wght")
        assert dumpXML(twice) == dumpXML(varfont)
