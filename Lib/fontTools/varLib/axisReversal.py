"""Reverse the direction of a variable font axis.

The module exports a `reverseAxisDirection` function that rewrites a variable
font so that a given axis runs in the opposite direction: a user-space
coordinate ``x`` along the reversed axis is remapped to ``min + max - x``,
where ``min`` and ``max`` are the axis's minimum and maximum values. The new
font at position ``x`` looks exactly like the original font at the mirrored
position ``min + max - x``.

This is useful e.g. when a design system exposes a slider whose direction
needs to be flipped without changing the visual output.

Example:

.. code-block:: pycon

    >>>
    >>> from fontTools import ttLib
    >>> from fontTools.varLib.axisReversal import reverseAxisDirection
    >>> varfont = ttLib.TTFont("path/to/MyVariableFont.ttf")
    >>> reversed_font = reverseAxisDirection(varfont, "wght")
    >>> [a.defaultValue for a in reversed_font["fvar"].axes]  # doctest: +SKIP
    [600.0]

The transformation mirrors, for the selected axis: the ``fvar`` default value
and named instance coordinates, the ``avar`` segment map, the ``STAT`` axis
values, the ``gvar`` tuple variation regions, and the variation regions of
the Item Variation Stores used by ``HVAR``, ``VVAR``, ``MVAR`` and ``COLR``
(version 1) tables. All other axes are left untouched; in particular the
axis's minimum and maximum values are *not* swapped.

Variation data in tables other than the ones listed above (e.g. ``CFF2``,
``cvar``, ``VARC``, ``GDEF``/``GPOS``/``GSUB``/``BASE``/``JSTF`` variation
stores or Feature Variations, and version 2 ``avar``) is not supported yet;
``reverseAxisDirection`` raises ``NotImplementedError`` when it is present,
rather than silently producing a corrupt font.
"""

from copy import deepcopy
import logging

from fontTools.misc.fixedTools import floatToFixedToFloat

log = logging.getLogger(__name__)

__all__ = ["reverseAxisDirection"]


# Tables that carry variation data referencing the fvar axes and that we do
# not (yet) know how to reverse; their presence is an error.
_UNSUPPORTED_TABLES = ("CFF2", "cvar", "VARC")

# OpenType layout tables that may contain an Item Variation Store and/or
# Feature Variations with axis conditions, both of which we cannot reverse.
_OT_LAYOUT_TABLES = ("GDEF", "GPOS", "GSUB", "BASE", "JSTF")


def reverseAxisDirection(varfont, axisTag, inplace=False):
    """Reverse the direction of one axis of a variable font.

    The user-space coordinate ``x`` of the axis identified by ``axisTag`` is
    remapped to ``min + max - x``, so that the returned font at position
    ``x`` renders identically to the input font at the mirrored position
    ``min + max - x``. The axis range (minimum/maximum) is unchanged; the
    default value, named instance coordinates, ``avar`` mapping, ``STAT``
    axis values and all supported variation data are mirrored accordingly.

    Args:
        varfont: a TTFont instance, which must contain an 'fvar' table.
        axisTag: the 4-character tag of the axis to reverse.
        inplace (bool): whether to modify the input TTFont object in-place
            instead of returning a distinct object.

    Returns:
        The modified TTFont instance (the input one if ``inplace`` is True).

    Raises:
        ValueError: if the font has no 'fvar' table or no such axis.
        NotImplementedError: if the font contains variation data in tables
            that are not supported yet (CFF2, cvar, VARC, OpenType layout
            variation stores or Feature Variations, avar version 2).
    """
    if "fvar" not in varfont:
        raise ValueError("Missing required table 'fvar'")
    if "gvar" in varfont and "glyf" not in varfont:
        raise ValueError("Can't have gvar without glyf")

    fvar = varfont["fvar"]
    axisTags = [axis.axisTag for axis in fvar.axes]
    if axisTag not in axisTags:
        raise ValueError(f"Axis {axisTag!r} not present in 'fvar' table")
    axisIndex = axisTags.index(axisTag)

    _checkSupportedVariationTables(varfont)

    if not inplace:
        varfont = deepcopy(varfont)

    fvar = varfont["fvar"]
    axis = fvar.axes[axisIndex]
    mirror = axis.minValue + axis.maxValue

    log.info("Reversing axis %r: mapping x to %s - x", axisTag, mirror)

    def mirrored(value):
        # fvar and STAT user-space values are 16.16 fixed-point numbers
        return floatToFixedToFloat(mirror - value, 16)

    _reverseFvar(fvar, axisTag, mirrored)

    if "STAT" in varfont:
        _reverseSTAT(varfont["STAT"].table, axisIndex, mirrored)

    if "avar" in varfont:
        _reverseAvar(varfont["avar"], axisTag)

    if "gvar" in varfont:
        _reverseGvar(varfont["gvar"], axisTag)

    seen = set()
    for varStore in _iterSupportedVarStores(varfont):
        if id(varStore) in seen:
            continue
        seen.add(id(varStore))
        _reverseVarStore(varStore, axisIndex)

    return varfont


def _checkSupportedVariationTables(varfont):
    for tag in _UNSUPPORTED_TABLES:
        if tag in varfont:
            raise NotImplementedError(
                f"Reversing an axis is not supported for fonts containing "
                f"a {tag!r} table"
            )

    if "avar" in varfont:
        avar = varfont["avar"]
        if getattr(avar, "majorVersion", 1) >= 2:
            raise NotImplementedError(
                "Reversing an axis is not supported for fonts containing "
                "a version 2 'avar' table"
            )

    for tag in _OT_LAYOUT_TABLES:
        if tag not in varfont:
            continue
        table = varfont[tag].table
        if getattr(table, "VarStore", None) is not None:
            raise NotImplementedError(
                f"Reversing an axis is not supported for fonts containing "
                f"a variation store in the {tag!r} table"
            )
        if getattr(table, "FeatureVariations", None) is not None:
            raise NotImplementedError(
                f"Reversing an axis is not supported for fonts containing "
                f"Feature Variations in the {tag!r} table"
            )


def _reverseFvar(fvar, axisTag, mirrored):
    for axis in fvar.axes:
        if axis.axisTag == axisTag:
            axis.defaultValue = mirrored(axis.defaultValue)
    for instance in fvar.instances:
        coordinates = instance.coordinates
        if axisTag in coordinates:
            coordinates[axisTag] = mirrored(coordinates[axisTag])


def _reverseSTAT(stat, axisIndex, mirrored):
    axisValueArray = getattr(stat, "AxisValueArray", None)
    if axisValueArray is None:
        return
    for axisValue in axisValueArray.AxisValue:
        if axisValue.Format == 4:
            for record in axisValue.AxisValueRecord:
                if record.AxisIndex == axisIndex:
                    record.Value = mirrored(record.Value)
        elif axisValue.AxisIndex == axisIndex:
            if axisValue.Format == 1:
                axisValue.Value = mirrored(axisValue.Value)
            elif axisValue.Format == 2:
                axisValue.NominalValue = mirrored(axisValue.NominalValue)
                rangeMin = mirrored(axisValue.RangeMaxValue)
                rangeMax = mirrored(axisValue.RangeMinValue)
                axisValue.RangeMinValue = rangeMin
                axisValue.RangeMaxValue = rangeMax
            elif axisValue.Format == 3:
                axisValue.Value = mirrored(axisValue.Value)
                axisValue.LinkedValue = mirrored(axisValue.LinkedValue)
            else:
                raise NotImplementedError(
                    f"Unsupported STAT AxisValue format: {axisValue.Format}"
                )


def _reverseAvar(avar, axisTag):
    segment = avar.segments.get(axisTag)
    if segment is None:
        return
    # The new segment map must produce the negated final normalized coordinate
    # for the negated pre-avar coordinate: avar'(m) = -avar(-m). Negating and
    # swapping both sides keeps the map monotonically increasing.
    avar.segments[axisTag] = dict(sorted((-fro, -to) for fro, to in segment.items()))


def _reverseTupleVariationAxes(axes, axisTag):
    if axisTag in axes:
        start, peak, end = axes[axisTag]
        axes[axisTag] = (-end, -peak, -start)


def _reverseGvar(gvar, axisTag):
    for variations in gvar.variations.values():
        for variation in variations:
            _reverseTupleVariationAxes(variation.axes, axisTag)


def _iterSupportedVarStores(varfont):
    for tag in ("HVAR", "VVAR", "MVAR"):
        if tag in varfont:
            varStore = getattr(varfont[tag].table, "VarStore", None)
            if varStore is not None:
                yield varStore
    if "COLR" in varfont:
        varStore = getattr(varfont["COLR"].table, "VarStore", None)
        if varStore is not None:
            yield varStore


def _reverseVarStore(varStore, axisIndex):
    for region in varStore.VarRegionList.Region:
        regionAxis = region.VarRegionAxis[axisIndex]
        start, peak, end = (
            regionAxis.StartCoord,
            regionAxis.PeakCoord,
            regionAxis.EndCoord,
        )
        regionAxis.StartCoord = -end
        regionAxis.PeakCoord = -peak
        regionAxis.EndCoord = -start
