from fontTools.misc.psCharStrings import (
    SimpleT2Decompiler,
    T2CharString,
    T2WidthExtractor,
    calcSubrBias,
    encodeIntT2,
)


def _uniq_sort(l):
    return sorted(set(l))


class StopHintCountEvent(Exception):
    pass


class _DesubroutinizingT2Decompiler(SimpleT2Decompiler):
    stop_hintcount_ops = (
        "op_hintmask",
        "op_cntrmask",
        "op_rmoveto",
        "op_hmoveto",
        "op_vmoveto",
    )

    def __init__(self, localSubrs, globalSubrs, private=None):
        SimpleT2Decompiler.__init__(self, localSubrs, globalSubrs, private)

    def execute(self, charString):
        self.need_hintcount = True  # until proven otherwise
        for op_name in self.stop_hintcount_ops:
            setattr(self, op_name, self.stop_hint_count)

        if hasattr(charString, "_desubroutinized"):
            # If a charstring has already been desubroutinized, we will still
            # need to execute it if we need to count hints in order to
            # compute the byte length for mask arguments, and haven't finished
            # counting hints pairs.
            if self.need_hintcount and self.callingStack:
                try:
                    SimpleT2Decompiler.execute(self, charString)
                except StopHintCountEvent:
                    del self.callingStack[-1]
            return

        charString._patches = []
        SimpleT2Decompiler.execute(self, charString)
        desubroutinized = charString.program[:]
        for idx, expansion in reversed(charString._patches):
            assert idx >= 2
            assert desubroutinized[idx - 1] in [
                "callsubr",
                "callgsubr",
            ], desubroutinized[idx - 1]
            assert type(desubroutinized[idx - 2]) == int
            if expansion[-1] == "return":
                expansion = expansion[:-1]
            desubroutinized[idx - 2 : idx] = expansion
        if not self.private.in_cff2:
            if "endchar" in desubroutinized:
                # Cut off after first endchar
                desubroutinized = desubroutinized[
                    : desubroutinized.index("endchar") + 1
                ]

        charString._desubroutinized = desubroutinized
        del charString._patches

    def op_callsubr(self, index):
        subr = self.localSubrs[self.operandStack[-1] + self.localBias]
        SimpleT2Decompiler.op_callsubr(self, index)
        self.processSubr(index, subr)

    def op_callgsubr(self, index):
        subr = self.globalSubrs[self.operandStack[-1] + self.globalBias]
        SimpleT2Decompiler.op_callgsubr(self, index)
        self.processSubr(index, subr)

    def stop_hint_count(self, *args):
        self.need_hintcount = False
        for op_name in self.stop_hintcount_ops:
            setattr(self, op_name, None)
        cs = self.callingStack[-1]
        if hasattr(cs, "_desubroutinized"):
            raise StopHintCountEvent()

    def op_hintmask(self, index):
        SimpleT2Decompiler.op_hintmask(self, index)
        if self.need_hintcount:
            self.stop_hint_count()

    def processSubr(self, index, subr):
        cs = self.callingStack[-1]
        if not hasattr(cs, "_desubroutinized"):
            cs._patches.append((index, subr._desubroutinized))


def desubroutinizeCharString(cs):
    """Desubroutinize a charstring in-place."""
    cs.decompile()
    subrs = getattr(cs.private, "Subrs", [])
    decompiler = _DesubroutinizingT2Decompiler(subrs, cs.globalSubrs, cs.private)
    decompiler.execute(cs)
    cs.program = cs._desubroutinized
    del cs._desubroutinized


def desubroutinize(cff):
    for fontName in cff.fontNames:
        font = cff[fontName]
        cs = font.CharStrings
        for c in cs.values():
            desubroutinizeCharString(c)
        # Delete all the local subrs
        if hasattr(font, "FDArray"):
            for fd in font.FDArray:
                pd = fd.Private
                if hasattr(pd, "Subrs"):
                    del pd.Subrs
                if "Subrs" in pd.rawDict:
                    del pd.rawDict["Subrs"]
        else:
            pd = font.Private
            if hasattr(pd, "Subrs"):
                del pd.Subrs
            if "Subrs" in pd.rawDict:
                del pd.rawDict["Subrs"]
    # as well as the global subrs
    cff.GlobalSubrs.clear()


class _MarkingT2Decompiler(SimpleT2Decompiler):
    def __init__(self, localSubrs, globalSubrs, private):
        SimpleT2Decompiler.__init__(self, localSubrs, globalSubrs, private)
        for subrs in [localSubrs, globalSubrs]:
            if subrs and not hasattr(subrs, "_used"):
                subrs._used = set()

    def op_callsubr(self, index):
        self.localSubrs._used.add(self.operandStack[-1] + self.localBias)
        SimpleT2Decompiler.op_callsubr(self, index)

    def op_callgsubr(self, index):
        self.globalSubrs._used.add(self.operandStack[-1] + self.globalBias)
        SimpleT2Decompiler.op_callgsubr(self, index)


class _DehintingT2Decompiler(T2WidthExtractor):
    class Hints(object):
        def __init__(self):
            # Whether calling this charstring produces any hint stems
            # Note that if a charstring starts with hintmask, it will
            # have has_hint set to True, because it *might* produce an
            # implicit vstem if called under certain conditions.
            self.has_hint = False
            # Index to start at to drop all hints
            self.last_hint = 0
            # Index up to which we know more hints are possible.
            # Only relevant if status is 0 or 1.
            self.last_checked = 0
            # The status means:
            # 0: after dropping hints, this charstring is empty
            # 1: after dropping hints, there may be more hints
            # 	continuing after this, or there might be
            # 	other things.  Not clear yet.
            # 2: no more hints possible after this charstring
            self.status = 0
            # Has hintmask instructions; not recursive
            self.has_hintmask = False
            # List of indices of calls to empty subroutines to remove.
            self.deletions = []

        pass

    def __init__(
        self, css, localSubrs, globalSubrs, nominalWidthX, defaultWidthX, private=None
    ):
        self._css = css
        T2WidthExtractor.__init__(
            self, localSubrs, globalSubrs, nominalWidthX, defaultWidthX
        )
        self.private = private

    def execute(self, charString):
        old_hints = charString._hints if hasattr(charString, "_hints") else None
        charString._hints = self.Hints()

        T2WidthExtractor.execute(self, charString)

        hints = charString._hints

        if hints.has_hint or hints.has_hintmask:
            self._css.add(charString)

        if hints.status != 2:
            # Check from last_check, make sure we didn't have any operators.
            for i in range(hints.last_checked, len(charString.program) - 1):
                if isinstance(charString.program[i], str):
                    hints.status = 2
                    break
                else:
                    hints.status = 1  # There's *something* here
            hints.last_checked = len(charString.program)

        if old_hints:
            assert hints.__dict__ == old_hints.__dict__

    def op_callsubr(self, index):
        subr = self.localSubrs[self.operandStack[-1] + self.localBias]
        T2WidthExtractor.op_callsubr(self, index)
        self.processSubr(index, subr)

    def op_callgsubr(self, index):
        subr = self.globalSubrs[self.operandStack[-1] + self.globalBias]
        T2WidthExtractor.op_callgsubr(self, index)
        self.processSubr(index, subr)

    def op_hstem(self, index):
        T2WidthExtractor.op_hstem(self, index)
        self.processHint(index)

    def op_vstem(self, index):
        T2WidthExtractor.op_vstem(self, index)
        self.processHint(index)

    def op_hstemhm(self, index):
        T2WidthExtractor.op_hstemhm(self, index)
        self.processHint(index)

    def op_vstemhm(self, index):
        T2WidthExtractor.op_vstemhm(self, index)
        self.processHint(index)

    def op_hintmask(self, index):
        rv = T2WidthExtractor.op_hintmask(self, index)
        self.processHintmask(index)
        return rv

    def op_cntrmask(self, index):
        rv = T2WidthExtractor.op_cntrmask(self, index)
        self.processHintmask(index)
        return rv

    def processHintmask(self, index):
        cs = self.callingStack[-1]
        hints = cs._hints
        hints.has_hintmask = True
        if hints.status != 2:
            # Check from last_check, see if we may be an implicit vstem
            for i in range(hints.last_checked, index - 1):
                if isinstance(cs.program[i], str):
                    hints.status = 2
                    break
            else:
                # We are an implicit vstem
                hints.has_hint = True
                hints.last_hint = index + 1
                hints.status = 0
        hints.last_checked = index + 1

    def processHint(self, index):
        cs = self.callingStack[-1]
        hints = cs._hints
        hints.has_hint = True
        hints.last_hint = index
        hints.last_checked = index

    def processSubr(self, index, subr):
        cs = self.callingStack[-1]
        hints = cs._hints
        subr_hints = subr._hints

        # Check from last_check, make sure we didn't have
        # any operators.
        if hints.status != 2:
            for i in range(hints.last_checked, index - 1):
                if isinstance(cs.program[i], str):
                    hints.status = 2
                    break
            hints.last_checked = index

        if hints.status != 2:
            if subr_hints.has_hint:
                hints.has_hint = True

                # Decide where to chop off from
                if subr_hints.status == 0:
                    hints.last_hint = index
                else:
                    hints.last_hint = index - 2  # Leave the subr call in

        elif subr_hints.status == 0:
            hints.deletions.append(index)

        hints.status = max(hints.status, subr_hints.status)


def _cs_subset_subroutines(charstring, subrs, gsubrs):
    p = charstring.program
    for i in range(1, len(p)):
        if p[i] == "callsubr":
            assert isinstance(p[i - 1], int)
            p[i - 1] = subrs._used.index(p[i - 1] + subrs._old_bias) - subrs._new_bias
        elif p[i] == "callgsubr":
            assert isinstance(p[i - 1], int)
            p[i - 1] = (
                gsubrs._used.index(p[i - 1] + gsubrs._old_bias) - gsubrs._new_bias
            )


def _cs_drop_hints(charstring):
    hints = charstring._hints

    if hints.deletions:
        p = charstring.program
        for idx in reversed(hints.deletions):
            del p[idx - 2 : idx]

    if hints.has_hint:
        assert not hints.deletions or hints.last_hint <= hints.deletions[0]
        charstring.program = charstring.program[hints.last_hint :]
        if not charstring.program:
            # TODO CFF2 no need for endchar.
            charstring.program.append("endchar")
        if hasattr(charstring, "width"):
            # Insert width back if needed
            if charstring.width != charstring.private.defaultWidthX:
                # For CFF2 charstrings, this should never happen
                assert (
                    charstring.private.defaultWidthX is not None
                ), "CFF2 CharStrings must not have an initial width value"
                charstring.program.insert(
                    0, charstring.width - charstring.private.nominalWidthX
                )

    if hints.has_hintmask:
        i = 0
        p = charstring.program
        while i < len(p):
            if p[i] in ["hintmask", "cntrmask"]:
                assert i + 1 <= len(p)
                del p[i : i + 2]
                continue
            i += 1

    assert len(charstring.program)

    del charstring._hints


def remove_hints(cff, *, removeUnusedSubrs: bool = True):
    for fontname in cff.keys():
        font = cff[fontname]
        cs = font.CharStrings
        # This can be tricky, but doesn't have to. What we do is:
        #
        # - Run all used glyph charstrings and recurse into subroutines,
        # - For each charstring (including subroutines), if it has any
        #   of the hint stem operators, we mark it as such.
        #   Upon returning, for each charstring we note all the
        #   subroutine calls it makes that (recursively) contain a stem,
        # - Dropping hinting then consists of the following two ops:
        #   * Drop the piece of the program in each charstring before the
        #     last call to a stem op or a stem-calling subroutine,
        #   * Drop all hintmask operations.
        # - It's trickier... A hintmask right after hints and a few numbers
        #    will act as an implicit vstemhm. As such, we track whether
        #    we have seen any non-hint operators so far and do the right
        #    thing, recursively... Good luck understanding that :(
        css = set()
        for c in cs.values():
            c.decompile()
            subrs = getattr(c.private, "Subrs", [])
            decompiler = _DehintingT2Decompiler(
                css,
                subrs,
                c.globalSubrs,
                c.private.nominalWidthX,
                c.private.defaultWidthX,
                c.private,
            )
            decompiler.execute(c)
            c.width = decompiler.width
        for charstring in css:
            _cs_drop_hints(charstring)
        del css

        # Drop font-wide hinting values
        all_privs = []
        if hasattr(font, "FDArray"):
            all_privs.extend(fd.Private for fd in font.FDArray)
        else:
            all_privs.append(font.Private)
        for priv in all_privs:
            for k in [
                "BlueValues",
                "OtherBlues",
                "FamilyBlues",
                "FamilyOtherBlues",
                "BlueScale",
                "BlueShift",
                "BlueFuzz",
                "StemSnapH",
                "StemSnapV",
                "StdHW",
                "StdVW",
                "ForceBold",
                "LanguageGroup",
                "ExpansionFactor",
            ]:
                if hasattr(priv, k):
                    setattr(priv, k, None)
    if removeUnusedSubrs:
        remove_unused_subroutines(cff)


def _pd_delete_empty_subrs(private_dict):
    if hasattr(private_dict, "Subrs") and not private_dict.Subrs:
        if "Subrs" in private_dict.rawDict:
            del private_dict.rawDict["Subrs"]
        del private_dict.Subrs


def remove_unused_subroutines(cff):
    for fontname in cff.keys():
        font = cff[fontname]
        cs = font.CharStrings
        # Renumber subroutines to remove unused ones

        # Mark all used subroutines
        for c in cs.values():
            subrs = getattr(c.private, "Subrs", [])
            decompiler = _MarkingT2Decompiler(subrs, c.globalSubrs, c.private)
            decompiler.execute(c)

        all_subrs = [font.GlobalSubrs]
        if hasattr(font, "FDArray"):
            all_subrs.extend(
                fd.Private.Subrs
                for fd in font.FDArray
                if hasattr(fd.Private, "Subrs") and fd.Private.Subrs
            )
        elif hasattr(font.Private, "Subrs") and font.Private.Subrs:
            all_subrs.append(font.Private.Subrs)

        subrs = set(subrs)  # Remove duplicates

        # Prepare
        for subrs in all_subrs:
            if not hasattr(subrs, "_used"):
                subrs._used = set()
            subrs._used = _uniq_sort(subrs._used)
            subrs._old_bias = calcSubrBias(subrs)
            subrs._new_bias = calcSubrBias(subrs._used)

        # Renumber glyph charstrings
        for c in cs.values():
            subrs = getattr(c.private, "Subrs", None)
            _cs_subset_subroutines(c, subrs, font.GlobalSubrs)

        # Renumber subroutines themselves
        for subrs in all_subrs:
            if subrs == font.GlobalSubrs:
                if not hasattr(font, "FDArray") and hasattr(font.Private, "Subrs"):
                    local_subrs = font.Private.Subrs
                elif (
                    hasattr(font, "FDArray")
                    and len(font.FDArray) == 1
                    and hasattr(font.FDArray[0].Private, "Subrs")
                ):
                    # Technically we shouldn't do this. But I've run into fonts that do it.
                    local_subrs = font.FDArray[0].Private.Subrs
                else:
                    local_subrs = None
            else:
                local_subrs = subrs

            subrs.items = [subrs.items[i] for i in subrs._used]
            if hasattr(subrs, "file"):
                del subrs.file
            if hasattr(subrs, "offsets"):
                del subrs.offsets

            for subr in subrs.items:
                _cs_subset_subroutines(subr, local_subrs, font.GlobalSubrs)

        # Delete local SubrsIndex if empty
        if hasattr(font, "FDArray"):
            for fd in font.FDArray:
                _pd_delete_empty_subrs(fd.Private)
        else:
            _pd_delete_empty_subrs(font.Private)

        # Cleanup
        for subrs in all_subrs:
            del subrs._used, subrs._old_bias, subrs._new_bias


# ---------------------------------------------------------------------------
# Subroutinization
# ---------------------------------------------------------------------------

# Operators that must never be moved into a subroutine: calls (so that new
# subroutines stay flat and the Type 2 call depth can never increase), and
# return/endchar (a subroutine is terminated by the subroutinizer itself).
# hintmask/cntrmask are excluded as well: their mask bytes are sized by the
# number of hints declared so far, which may differ between call sites.
_subroutinize_excluded_ops = frozenset(
    ["callsubr", "callgsubr", "return", "endchar", "hintmask", "cntrmask"]
)

_t2_opcodes = T2CharString.opcodes


def _subr_bias(count):
    if count < 1240:
        return 107
    elif count < 33900:
        return 1131
    return 32768


def _cs_instructions(program):
    """Split a decompiled Type 2 program into a list of instructions.

    Each instruction is a tuple of zero or more operands followed by an
    operator.  For hintmask/cntrmask the mask bytes that follow the operator
    are glued to it, so that operand stacks and mask byte boundaries can
    never be split by a candidate sequence.  Since whole instructions are
    extracted, the operand stack behaves identically whether the instruction
    is executed in place or inside a subroutine; in particular the glyph
    width, which is parsed from the operand count of the first stem/moveto
    instruction wherever it executes, is preserved.
    """
    instructions = []
    operands = []
    i = 0
    n = len(program)
    while i < n:
        token = program[i]
        i += 1
        if isinstance(token, str):
            if token in ("hintmask", "cntrmask"):
                instructions.append(tuple(operands) + (token, program[i]))
                i += 1
            else:
                instructions.append(tuple(operands) + (token,))
            operands = []
        else:
            operands.append(token)
    if operands:
        raise ValueError("CFF 1.0 charstring has operands left after last operator")
    return instructions


def _instruction_op(instruction):
    # The operator is the last token, except for hintmask/cntrmask where the
    # mask bytes follow it.
    last = instruction[-1]
    return last if isinstance(last, str) else instruction[-2]


def _instruction_cost(instruction):
    """Encoded size of an instruction in bytes."""
    cost = 0
    for token in instruction:
        if isinstance(token, str):
            cost += len(_t2_opcodes[token])
        elif isinstance(token, int):
            cost += len(encodeIntT2(token))
        elif isinstance(token, float):
            cost += 5  # encodeFixed: 0xFF followed by a 4-byte 16.16 value
        else:
            cost += len(token)  # hintmask/cntrmask mask bytes
    return cost


def _flatten_instructions(instructions):
    program = []
    for instruction in instructions:
        program.extend(instruction)
    return program


class _Separator(object):
    """Unique stream token separating charstrings, so that candidate
    sequences can never span a charstring boundary."""

    __slots__ = ()


class _SuffixAutomaton(object):
    """Suffix automaton over a sequence of hashable tokens; used to find
    repeated instruction sequences together with their occurrence counts."""

    def __init__(self):
        self.transitions = [{}]
        self.link = [-1]
        self.length = [0]
        # End position in the stream of the state's longest string.
        self.first_pos = [-1]
        self.occ = [0]
        self.last = 0

    def extend(self, token, pos):
        transitions = self.transitions
        link = self.link
        length = self.length
        cur = len(length)
        length.append(length[self.last] + 1)
        transitions.append({})
        link.append(0)
        self.first_pos.append(pos)
        self.occ.append(1)
        p = self.last
        while p != -1 and token not in transitions[p]:
            transitions[p][token] = cur
            p = link[p]
        if p == -1:
            link[cur] = 0
        else:
            q = transitions[p][token]
            if length[p] + 1 == length[q]:
                link[cur] = q
            else:
                clone = len(length)
                length.append(length[p] + 1)
                transitions.append(dict(transitions[q]))
                link.append(link[q])
                self.first_pos.append(self.first_pos[q])
                self.occ.append(0)
                while p != -1 and transitions[p].get(token) == q:
                    transitions[p][token] = clone
                    p = link[p]
                link[q] = clone
                link[cur] = clone
        self.last = cur

    def propagate_counts(self):
        # Number of end positions of a state = number of occurrences of the
        # substrings it represents.
        order = sorted(
            range(1, len(self.length)), key=self.length.__getitem__, reverse=True
        )
        link = self.link
        occ = self.occ
        for state in order:
            occ[link[state]] += occ[state]


def _find_subr_candidates(instr_streams, call_cost, blacklist, max_candidates=64):
    """Return up to max_candidates repeated instruction sequences, most
    promising first.  Each candidate is a tuple of instructions occurring at
    least twice whose estimated byte saving is positive."""
    stream = []
    for instructions in instr_streams:
        stream.extend(instructions)
        stream.append(_Separator())
    sam = _SuffixAutomaton()
    for pos, instruction in enumerate(stream):
        sam.extend(instruction, pos)
    sam.propagate_counts()
    # Prefix sums of instruction costs over the stream, for O(1) body costs.
    prefix_cost = [0] * (len(stream) + 1)
    cost_cache = {}
    for pos, instruction in enumerate(stream):
        if isinstance(instruction, tuple):
            cost = cost_cache.get(instruction)
            if cost is None:
                cost = cost_cache[instruction] = _instruction_cost(instruction)
        else:
            cost = 0  # _Separator
        prefix_cost[pos + 1] = prefix_cost[pos] + cost
    states = []
    for state in range(1, len(sam.length)):
        occ = sam.occ[state]
        if occ < 2:
            continue
        end = sam.first_pos[state] + 1
        body_cost = prefix_cost[end] - prefix_cost[end - sam.length[state]]
        # Save the body at every occurrence, pay one call per occurrence and
        # store the subr body (plus its return) once.
        saving = occ * (body_cost - call_cost) - (body_cost + 1)
        if saving > 0:
            states.append((saving, state))
    states.sort(reverse=True)
    candidates = []
    for _saving, state in states:
        end = sam.first_pos[state] + 1
        instructions = tuple(stream[end - sam.length[state] : end])
        # Trim to the longest suffix free of excluded operators.  Shorter
        # suffixes are covered by suffix-link ancestors of this state.
        min_len = sam.length[sam.link[state]] + 1
        cut = 0
        for i, instruction in enumerate(instructions):
            if _instruction_op(instruction) in _subroutinize_excluded_ops:
                cut = i + 1
        instructions = instructions[cut:]
        if len(instructions) < min_len:
            continue
        if instructions in blacklist:
            continue
        candidates.append(instructions)
        if len(candidates) >= max_candidates:
            break
    return candidates


def _replace_instructions(instructions, pattern, call_instruction):
    """Greedily replace non-overlapping occurrences of pattern."""
    n = len(pattern)
    out = []
    count = 0
    i = 0
    end = len(instructions)
    while i <= end - n:
        if instructions[i : i + n] == pattern:
            out.append(call_instruction)
            i += n
            count += 1
        else:
            out.append(instructions[i])
            i += 1
    out.extend(instructions[i:])
    return tuple(out), count


def _adjust_call_operands(program, call_op, delta):
    for i, token in enumerate(program):
        if token == call_op:
            program[i - 1] += delta


def _subroutinize_scope(
    charstrings,
    subrs,
    ensure_subrs,
    call_op,
    private,
    globalSubrs,
    renumber_programs,
    min_gain,
):
    """Extract repeated instruction sequences from charstrings into subrs.

    charstrings
        Decompiled T2CharStrings to compress; modified in place.
    subrs
        The SubrsIndex/GlobalSubrsIndex to append to, or None if the local
        Subrs index does not exist yet.
    ensure_subrs
        Callable creating (and attaching) the Subrs index on demand.
    call_op
        "callsubr" (local scope) or "callgsubr" (global scope).
    private
        The PrivateDict new subroutines belong to (None for global subrs).
    renumber_programs
        Callable returning all programs whose call_op operands must be
        adjusted when the subr count crosses a bias threshold.
    min_gain
        Minimum actual saving in bytes required to extract a subroutine.
    """
    blacklist = set()
    while True:
        instr_streams = [tuple(_cs_instructions(cs.program)) for cs in charstrings]
        n_subrs = len(subrs) if subrs is not None else 0
        call_cost = len(encodeIntT2(n_subrs - _subr_bias(n_subrs))) + 1
        candidates = _find_subr_candidates(instr_streams, call_cost, blacklist)
        if not candidates:
            return
        committed = False
        # Try to extract several candidates per round; each commit updates
        # instr_streams, and every following candidate is re-checked against
        # the updated streams before it is applied.
        for pattern in candidates:
            n_subrs = len(subrs) if subrs is not None else 0
            bias = _subr_bias(n_subrs)
            call_cost = len(encodeIntT2(n_subrs - bias)) + 1
            body_cost = sum(_instruction_cost(instr) for instr in pattern)
            call_instruction = (n_subrs - bias, call_op)
            replaced = []
            total = 0
            for i, instructions in enumerate(instr_streams):
                new_instructions, count = _replace_instructions(
                    instructions, pattern, call_instruction
                )
                if count:
                    replaced.append((i, new_instructions))
                    total += count
            saving = total * (body_cost - call_cost) - (body_cost + 1)
            if saving < min_gain:
                # Estimated saving counted overlapping occurrences; the
                # actual one is not worth it, now or later.
                blacklist.add(pattern)
                continue
            if subrs is None:
                subrs = ensure_subrs()
            for i, new_instructions in replaced:
                instr_streams[i] = new_instructions
                charstrings[i].setProgram(_flatten_instructions(new_instructions))
            subr_program = []
            for instruction in pattern:
                subr_program.extend(instruction)
            subr_program.append("return")
            subrs.append(
                T2CharString(
                    program=subr_program, private=private, globalSubrs=globalSubrs
                )
            )
            new_bias = _subr_bias(n_subrs + 1)
            if new_bias != bias:
                # The bias changed; every call operand (including the ones
                # just written, which still use the old bias) must be
                # re-encoded against the new bias.
                delta = bias - new_bias
                for program in renumber_programs():
                    _adjust_call_operands(program, call_op, delta)
                # The renumbering modified the programs behind our backs;
                # resync the instruction streams.
                instr_streams = [
                    tuple(_cs_instructions(cs.program)) for cs in charstrings
                ]
            committed = True
        if not committed:
            return


def subroutinize(cff, min_gain=1):
    """Subroutinize the CFF 1.0 (Type 2) charstrings of a CFFFontSet.

    Repeated instruction sequences are extracted into subroutines: sequences
    shared by glyphs of one FD (Private dict) become local subroutines of
    that FD, sequences shared across FDs or fonts become global subroutines.
    A sequence is only extracted if doing so actually makes the encoding
    smaller, accounting for the cost of the calls and of storing the
    subroutine; fonts with nothing worth extracting are left untouched.

    Extraction works on whole instructions, so the operand stack, the glyph
    width (parsed from the first stem/moveto instruction) and hintmask/
    cntrmask byte boundaries are all preserved; instructions containing
    hintmask/cntrmask, subroutine calls, return or endchar are never moved
    into a subroutine.  New subroutines contain no calls themselves, so no
    recursion is introduced and the Type 2 call depth cannot increase.

    Only CFF 1.0 is supported; CFF2 fontsets are rejected.
    """
    from fontTools.cffLib import SubrsIndex

    if getattr(cff, "major", 1) != 1:
        raise ValueError("subroutinize() only supports CFF 1.0 charstrings")

    globalSubrs = cff.GlobalSubrs

    # Decompile all charstrings and group them by Private dict (FD).
    all_charstrings = []
    privates = []
    groups = {}
    for fontName in cff.fontNames:
        font = cff[fontName]
        for cs in font.CharStrings.values():
            cs.decompile()
            all_charstrings.append(cs)
            private = cs.private
            if private is None:
                continue
            key = id(private)
            if key not in groups:
                groups[key] = []
                privates.append(private)
            groups[key].append(cs)

    def iter_subrs(private):
        subrs = getattr(private, "Subrs", None)
        if subrs is not None:
            for i in range(len(subrs)):
                yield subrs[i]

    # Decompile existing subroutines, so that their call operands can be
    # re-encoded if a bias threshold is crossed.
    for private in privates:
        for subr in iter_subrs(private):
            subr.decompile()
    for i in range(len(globalSubrs)):
        globalSubrs[i].decompile()

    single_private = len(privates) == 1

    # Local pass: extract sequences repeated within each FD.
    for private in privates:
        charstrings = groups[id(private)]
        subrs = getattr(private, "Subrs", None)

        def ensure_subrs(private=private):
            subrs = getattr(private, "Subrs", None)
            if subrs is None:
                subrs = SubrsIndex(globalSubrs=globalSubrs, private=private)
                private.Subrs = subrs
            return subrs

        def renumber_programs(private=private, charstrings=charstrings):
            programs = [cs.program for cs in charstrings]
            programs.extend(subr.program for subr in iter_subrs(private))
            if single_private:
                # Best effort, mirroring remove_unused_subroutines: global
                # subroutines calling the (only) local Subrs index.
                programs.extend(globalSubrs[i].program for i in range(len(globalSubrs)))
            return programs

        _subroutinize_scope(
            charstrings,
            subrs,
            ensure_subrs,
            "callsubr",
            private,
            globalSubrs,
            renumber_programs,
            min_gain,
        )

    # Global pass: extract sequences repeated across FDs/fonts.
    def renumber_global():
        programs = [cs.program for cs in all_charstrings]
        for private in privates:
            programs.extend(subr.program for subr in iter_subrs(private))
        programs.extend(globalSubrs[i].program for i in range(len(globalSubrs)))
        return programs

    _subroutinize_scope(
        all_charstrings,
        globalSubrs,
        lambda: globalSubrs,
        "callgsubr",
        None,
        globalSubrs,
        renumber_global,
        min_gain,
    )
