"""Reverse user-facing variation axes while preserving interpolation."""

from __future__ import annotations

from copy import deepcopy
from typing import Iterable

from fontTools.misc.fixedTools import floatToFixed as fl2fi
from fontTools.ttLib.tables import otTables as ot


__all__ = ["reverseVariableFontAxis", "reverseVariableFontAxes"]


_SUPPORTED_VAR_STORE_TABLES = {"COLR", "HVAR", "VVAR", "MVAR"}


def _quantize(value, precisionBits):
    from fontTools.misc.fixedTools import fixedToFloat as fi2fl

    return fi2fl(fl2fi(value, precisionBits), precisionBits)


def _findAxis(varfont, axisTag):
    for axis in varfont["fvar"].axes:
        if axis.axisTag == axisTag:
            return axis
    raise ValueError(f"Unknown variation axis: {axisTag!r}")


def _mirrorUserValue(axis, value):
    return axis.minValue + axis.maxValue - value


def _mirrorFvarAndSTAT(varfont, axisTag):
    axis = _findAxis(varfont, axisTag)
    oldMinimum = axis.minValue
    oldDefault = axis.defaultValue
    oldMaximum = axis.maxValue

    axis.defaultValue = _quantize(_mirrorUserValue(axis, oldDefault), 16)

    for instance in varfont["fvar"].instances:
        if axisTag in instance.coordinates:
            value = instance.coordinates[axisTag]
            instance.coordinates[axisTag] = _quantize(
                oldMinimum + oldMaximum - value, 16
            )

    if "STAT" not in varfont:
        return

    stat = varfont["STAT"].table
    designAxisRecord = getattr(stat, "DesignAxisRecord", None)
    designAxes = getattr(designAxisRecord, "Axis", None) or []
    axisIndex = next(
        (
            index
            for index, record in enumerate(designAxes)
            if record.AxisTag == axisTag
        ),
        None,
    )
    axisValueArray = getattr(stat, "AxisValueArray", None)
    if axisIndex is None or axisValueArray is None:
        return

    for axisValue in axisValueArray.AxisValue:
        format = axisValue.Format
        if format in (1, 3) and axisValue.AxisIndex == axisIndex:
            axisValue.Value = _quantize(oldMinimum + oldMaximum - axisValue.Value, 16)
            if format == 3:
                axisValue.LinkedValue = _quantize(
                    oldMinimum + oldMaximum - axisValue.LinkedValue, 16
                )
        elif format == 2 and axisValue.AxisIndex == axisIndex:
            nominal = oldMinimum + oldMaximum - axisValue.NominalValue
            minimum = oldMinimum + oldMaximum - axisValue.RangeMaxValue
            maximum = oldMinimum + oldMaximum - axisValue.RangeMinValue
            axisValue.NominalValue = _quantize(nominal, 16)
            axisValue.RangeMinValue = _quantize(minimum, 16)
            axisValue.RangeMaxValue = _quantize(maximum, 16)
        elif format == 4:
            for record in axisValue.AxisValueRecord:
                if record.AxisIndex == axisIndex:
                    record.Value = _quantize(
                        oldMinimum + oldMaximum - record.Value, 16
                    )


def _reverseAvar(varfont, axisTag):
    if "avar" not in varfont:
        return

    avar = varfont["avar"]
    if getattr(avar, "majorVersion", 1) >= 2:
        raise NotImplementedError(
            "Reversing an axis in an avar version 2 font is not supported"
        )

    segments = avar.segments.get(axisTag)
    if not segments:
        return

    avar.segments[axisTag] = {
        _quantize(-fromValue, 14): _quantize(-toValue, 14)
        for fromValue, toValue in segments.items()
    }


def _reverseTupleVariationAxes(variations, axisTag):
    for variation in variations:
        region = variation.axes.get(axisTag)
        if region is None:
            continue
        start, peak, end = region
        variation.axes[axisTag] = (
            _quantize(-end, 14),
            _quantize(-peak, 14),
            _quantize(-start, 14),
        )


def _reverseGvar(varfont, axisTag):
    if "gvar" not in varfont:
        return
    for glyphVariations in varfont["gvar"].variations.values():
        _reverseTupleVariationAxes(glyphVariations, axisTag)


def _varStoreTouchesAxis(varStore, axisIndex):
    regionList = getattr(varStore, "VarRegionList", None)
    if regionList is None:
        return False
    regions = getattr(regionList, "Region", None) or []
    for region in regions:
        regionAxis = region.VarRegionAxis[axisIndex]
        if regionAxis.PeakCoord != 0:
            return True
    return False


def _reverseVarStore(varStore, axisIndex):
    for region in varStore.VarRegionList.Region:
        regionAxis = region.VarRegionAxis[axisIndex]
        start = regionAxis.StartCoord
        peak = regionAxis.PeakCoord
        end = regionAxis.EndCoord
        regionAxis.StartCoord = _quantize(-end, 14)
        regionAxis.PeakCoord = _quantize(-peak, 14)
        regionAxis.EndCoord = _quantize(-start, 14)


def _iterOTSubTables(value):
    if isinstance(value, ot.BaseTable):
        yield value
        for subtable in value.iterSubTables():
            yield from _iterOTSubTables(subtable.value)
    elif isinstance(value, list):
        for item in value:
            yield from _iterOTSubTables(item)


def _tableOTRoot(varfont, tag):
    table = varfont[tag]
    return getattr(table, "table", table)


def _varStoreRoots(varfont):
    roots = {}
    for tag in ("COLR", "HVAR", "VVAR", "MVAR", "GDEF", "BASE"):
        if tag not in varfont:
            continue
        root = _tableOTRoot(varfont, tag)
        for subtable in _iterOTSubTables(root):
            varStore = getattr(subtable, "VarStore", None)
            if varStore is not None:
                roots[id(varStore)] = (tag, varStore)
    return roots


def _conditionUsesAxis(condition, axisIndices):
    if condition.Format == 1:
        return condition.AxisIndex in axisIndices
    if condition.Format == 2:
        return True
    if condition.Format in (3, 4):
        return any(
            _conditionUsesAxis(child, axisIndices)
            for child in condition.ConditionTable
        )
    if condition.Format == 5:
        return _conditionUsesAxis(condition.ConditionTable, axisIndices)
    return False


def _hasFeatureVariations(varfont, axisIndices):
    for tag in ("GSUB", "GPOS"):
        if tag not in varfont:
            continue
        table = varfont[tag].table
        featureVariations = getattr(table, "FeatureVariations", None)
        if featureVariations is None:
            continue
        records = getattr(featureVariations, "FeatureVariationRecord", None) or []
        for record in records:
            conditionSet = record.ConditionSet
            if conditionSet is None:
                continue
            if any(
                _conditionUsesAxis(condition, axisIndices)
                for condition in conditionSet.ConditionTable
            ):
                return True
    return False


def _unsupportedReasons(varfont, axisTags):
    axisIndexSet = {
        axis.axisTag: index for index, axis in enumerate(varfont["fvar"].axes)
    }
    axisIndices = set(axisIndexSet[axisTag] for axisTag in axisTags)
    reasons = []

    if "cvar" in varfont and varfont["cvar"].variations:
        reasons.append("cvar")
    if "CFF2" in varfont:
        reasons.append("CFF2")
    if "avar" in varfont and getattr(varfont["avar"], "majorVersion", 1) >= 2:
        reasons.append("avar version 2")
    if "VARC" in varfont:
        reasons.append("VARC")
    if _hasFeatureVariations(varfont, axisIndices):
        reasons.append("GSUB/GPOS FeatureVariations")

    for tag, varStore in _varStoreRoots(varfont).values():
        if tag in _SUPPORTED_VAR_STORE_TABLES:
            continue
        if any(
            _varStoreTouchesAxis(varStore, axisIndexSet[axisTag])
            for axisTag in axisTags
        ):
            reasons.append(f"{tag} VarStore")

    return sorted(set(reasons))


def _reverseSupportedVarStores(varfont, axisTag):
    axisIndex = next(
        index
        for index, axis in enumerate(varfont["fvar"].axes)
        if axis.axisTag == axisTag
    )
    seen = set()
    for tag, varStore in _varStoreRoots(varfont).values():
        if tag not in _SUPPORTED_VAR_STORE_TABLES or id(varStore) in seen:
            continue
        seen.add(id(varStore))
        _reverseVarStore(varStore, axisIndex)


def reverseVariableFontAxes(varfont, axisTags: Iterable[str], *, inplace=False):
    """Reverse one or more variation axes in a variable font.

    A coordinate x on each selected output axis represents the appearance that
    was previously located at minimum + maximum - x.

    The currently supported variation-carrying tables are gvar, HVAR, VVAR,
    MVAR and COLR version 1. Other variable tables cause NotImplementedError
    before the font is modified.
    """
    axisTags = list(axisTags)
    if not axisTags:
        return varfont
    if "fvar" not in varfont:
        raise ValueError("Missing required table fvar")
    if "gvar" in varfont and "glyf" not in varfont:
        raise ValueError("Can't have gvar without glyf")

    selectedAxes = {axis.axisTag for axis in varfont["fvar"].axes}
    unknownAxes = sorted(set(axisTags) - selectedAxes)
    if unknownAxes:
        raise ValueError(f"Unknown variation axis: {unknownAxes[0]!r}")
    if len(axisTags) != len(set(axisTags)):
        raise ValueError("Cannot reverse the same axis more than once")

    unsupported = _unsupportedReasons(varfont, axisTags)
    if unsupported:
        raise NotImplementedError(
            "Reversing an axis is not supported for variation table(s): "
            + ", ".join(unsupported)
        )

    if not inplace:
        varfont = deepcopy(varfont)

    for axisTag in axisTags:
        _reverseAvar(varfont, axisTag)
        _reverseGvar(varfont, axisTag)
        _reverseSupportedVarStores(varfont, axisTag)
        _mirrorFvarAndSTAT(varfont, axisTag)

    return varfont


def reverseVariableFontAxis(varfont, axisTag, *, inplace=False):
    """Reverse a single variation axis; see reverseVariableFontAxes."""
    return reverseVariableFontAxes(varfont, [axisTag], inplace=inplace)
