"""Subroutinize CFF1 Type 2 charstrings."""

from dataclasses import dataclass

from fontTools.cffLib import SubrsIndex
from fontTools.cffLib.transforms import desubroutinizeCharString
from fontTools.misc.psCharStrings import T2CharString

WIDTH_OPS = frozenset(
    [
        "hstem",
        "hstem3",
        "hstemhm",
        "vstem",
        "vstem3",
        "vstemhm",
        "cntrmask",
        "hintmask",
        "rmoveto",
        "hmoveto",
        "vmoveto",
        "endchar",
    ]
)
ODD_WIDTH_OPS = frozenset(["hmoveto", "vmoveto"])
MASK_OPS = frozenset(["hintmask", "cntrmask"])
PATH_OPS = frozenset(
    [
        "rlineto",
        "hlineto",
        "vlineto",
        "rrcurveto",
        "rcurveline",
        "rlinecurve",
        "vvcurveto",
        "hhcurveto",
        "vhcurveto",
        "hvcurveto",
        "hflex",
        "flex",
        "hflex1",
        "flex1",
        "rmoveto",
        "hmoveto",
        "vmoveto",
    ]
)
GLOBAL_EXCLUDED_OPS = frozenset(
    [
        "endchar",
        "return",
        "callsubr",
        "callgsubr",
        "hintmask",
        "cntrmask",
        "hstem",
        "vstem",
        "hstemhm",
        "vstemhm",
        "hstem3",
        "vstem3",
        "blend",
        "vsindex",
    ]
)
LOCAL_EXCLUDED_OPS = frozenset(
    [
        "endchar",
        "return",
        "callsubr",
        "hintmask",
        "cntrmask",
        "hstem",
        "vstem",
        "hstemhm",
        "vstemhm",
        "hstem3",
        "vstem3",
        "blend",
        "vsindex",
    ]
)


@dataclass(frozen=True)
class Command:
    op: str
    args: tuple


class Mask(tuple):
    pass


@dataclass
class Segment:
    commands: list
    start: int
    end: int


@dataclass
class Pattern:
    body: tuple
    estimated_count: int
    body_cost: int


def _program_byte_length(program, private=None):
    charstring = T2CharString(program=list(program), private=private)
    charstring.compile()
    return len(charstring.bytecode)


def _cloned_charstring(charstring):
    clone = T2CharString(
        bytecode=charstring.bytecode,
        program=list(charstring.program) if charstring.program is not None else None,
        private=charstring.private,
        globalSubrs=charstring.globalSubrs,
    )
    return clone


def _bytes_token(token):
    if isinstance(token, Mask):
        return token
    return Mask((token,))


def _is_mask_token(token):
    return isinstance(token, bytes) or (isinstance(token, Mask) and bool(token))


def _call_cost(ordered_index):
    if ordered_index < 215:
        return 2
    if ordered_index < 1240:
        return 3
    return 5


def _command_program(command):
    args = []
    for arg in command.args:
        if _is_mask_token(arg):
            if isinstance(arg, Mask):
                value = b"".join(arg)
            else:
                value = arg
            args.extend([command.op, value])
        else:
            args.append(arg)
    if any(_is_mask_token(arg) for arg in command.args):
        return args
    return args + ([command.op] if command.op else [])


def _parse_commands(program):
    commands = []
    stack = []
    first = True
    i = 0
    while i < len(program):
        token = program[i]
        i += 1
        if not isinstance(token, str):
            stack.append(token)
            continue

        mask = b""
        if token in MASK_OPS:
            mask = program[i]
            i += 1

        if first and token in WIDTH_OPS:
            numeric_args = len(stack)
            odd = token in ODD_WIDTH_OPS
            has_width = numeric_args % 2 == (0 if odd else 1)
            if has_width:
                commands.append(Command("", (stack[0],)))
                stack = stack[1:]
            first = False

        args = tuple(stack) + (_bytes_token(mask) if token in MASK_OPS else ())
        commands.append(Command(token, args))
        stack = []
        if token == "endchar":
            first = False
    return commands


def _is_eligible(command, excluded_ops):
    return command.op not in excluded_ops and (
        command.op in PATH_OPS or command.op == "callgsubr"
    )


class _SuffixAutomaton:
    def __init__(self):
        self.length = [0]
        self.link = [-1]
        self.next = [{}]
        self.first_end = [-1]
        self.count = [0]
        self.last = 0

    def extend(self, token, end):
        cur = len(self.length)
        self.length.append(self.length[self.last] + 1)
        self.link.append(0)
        self.next.append({})
        self.first_end.append(end)
        self.count.append(1)
        p = self.last
        while p != -1 and token not in self.next[p]:
            self.next[p][token] = cur
            p = self.link[p]
        if p == -1:
            self.link[cur] = 0
        else:
            q = self.next[p][token]
            if self.length[p] + 1 == self.length[q]:
                self.link[cur] = q
            else:
                clone = len(self.length)
                self.length.append(self.length[p] + 1)
                self.next.append(self.next[q].copy())
                self.link.append(self.link[q])
                self.first_end.append(self.first_end[q])
                self.count.append(0)
                while p != -1 and self.next[p].get(token) == q:
                    self.next[p][token] = clone
                    p = self.link[p]
                self.link[q] = self.link[cur] = clone
        self.last = cur

    def reset(self):
        self.last = 0

    def finish(self):
        states = sorted(
            range(1, len(self.length)), key=self.length.__getitem__, reverse=True
        )
        for state in states:
            self.count[self.link[state]] += self.count[state]


def _segments(commands, excluded_ops):
    result = []
    start = None
    for index, command in enumerate(commands):
        if _is_eligible(command, excluded_ops):
            if start is None:
                start = index
        elif start is not None:
            if index > start:
                result.append(Segment(commands, start, index))
            start = None
    if start is not None and len(commands) > start:
        result.append(Segment(commands, start, len(commands)))
    return result


def _candidate_patterns(segments, overhead, excluded_ops, private=None):
    sam = _SuffixAutomaton()
    for segment in segments:
        for end in range(segment.start, segment.end):
            command = segment.commands[end]
            if _is_eligible(command, excluded_ops):
                sam.extend(command, (segment, end))
        sam.extend(object(), None)
        sam.reset()
    sam.finish()

    patterns = {}
    for state in range(1, len(sam.length)):
        count = sam.count[state]
        if count < 2:
            continue
        segment, end = sam.first_end[state]
        length = sam.length[state]
        start = end - length + 1
        if start < segment.start:
            continue
        body = tuple(segment.commands[start : end + 1])
        if not body or any(not _is_eligible(command, excluded_ops) for command in body):
            continue
        body_cost = (
            sum(
                _program_byte_length(_command_program(command), private)
                for command in body
            )
            + 1
        )
        call_cost = 2 if count * 2 <= 215 else 3
        net = count * (body_cost - 1 - call_cost) - overhead
        if net <= 0:
            continue
        previous = patterns.get(body)
        if previous is None or count > previous.estimated_count:
            patterns[body] = Pattern(body, count, body_cost)

    selected = list(patterns.values())
    frequency_order = sorted(
        selected, key=lambda item: item.estimated_count, reverse=True
    )
    call_costs = {}
    for index, pattern in enumerate(frequency_order):
        call_costs[id(pattern.body)] = _call_cost(index)
    return frequency_order, call_costs


class _PatternMatcher:
    def __init__(self, patterns):
        self.goto = [{}]
        self.failure = [0]
        self.outputs = [[]]
        for pattern in patterns:
            node = 0
            for command in pattern.body:
                nxt = self.goto[node].get(command)
                if nxt is None:
                    nxt = len(self.goto)
                    self.goto[node][command] = nxt
                    self.goto.append({})
                    self.failure.append(0)
                    self.outputs.append([])
                node = nxt
            self.outputs[node].append(pattern)

        queue = list(self.goto[0].values())
        head = 0
        while head < len(queue):
            node = queue[head]
            head += 1
            for command, child in self.goto[node].items():
                queue.append(child)
                fallback = self.failure[node]
                while fallback and command not in self.goto[fallback]:
                    fallback = self.failure[fallback]
                self.failure[child] = self.goto[fallback].get(command, 0)
        for node in range(1, len(self.goto)):
            self.outputs[node].extend(self.outputs[self.failure[node]])

    def matches(self, segment):
        node = 0
        result = []
        for position in range(segment.start, segment.end):
            command = segment.commands[position]
            while node and command not in self.goto[node]:
                node = self.failure[node]
            node = self.goto[node].get(command, 0)
            end = position + 1
            for pattern in self.outputs[node]:
                start = end - len(pattern.body)
                if start >= segment.start:
                    result.append((start, end, pattern))
        return result


def _choose_matches(segments, patterns, call_costs):
    if not patterns:
        return {}
    matcher = _PatternMatcher(patterns)
    choices = {}
    for segment in segments:
        matches = sorted(matcher.matches(segment), key=lambda item: item[1])
        size = segment.end - segment.start
        best = [0] * (size + 1)
        choice = [None] * (size + 1)
        cursor = 0
        for absolute_end in range(segment.start, segment.end + 1):
            index = absolute_end - segment.start
            best[index] = best[index - 1] if index else 0
            while cursor < len(matches) and matches[cursor][1] < absolute_end:
                cursor += 1
            while cursor < len(matches) and matches[cursor][1] == absolute_end:
                start, _, pattern = matches[cursor]
                local_start = start - segment.start
                saving = pattern.body_cost - 1 - call_costs[id(pattern.body)]
                candidate = best[local_start] + saving
                if candidate > best[index]:
                    best[index] = candidate
                    choice[index] = (local_start, index, pattern)
                cursor += 1

        position = size
        selected = []
        while position > 0:
            item = choice[position]
            if item is None:
                position -= 1
            else:
                selected.append(item)
                position = item[0]
        if selected:
            choices[id(segment)] = list(reversed(selected))
    return choices


def _set_program(charstring, commands):
    program = []
    for command in commands:
        program.extend(_command_program(command))
    charstring.setProgram(program)


def _make_subr_index(patterns, private, global_subrs):
    subrs = SubrsIndex()
    for pattern in patterns:
        program = []
        for command in pattern.body:
            program.extend(_command_program(command))
        program.append("return")
        subrs.append(
            T2CharString(program=program, private=private, globalSubrs=global_subrs)
        )
    return subrs


def _selected_patterns(segments, choices, patterns):
    counts = {}
    for segment in segments:
        for _, _, pattern in choices.get(id(segment), []):
            counts[id(pattern.body)] = counts.get(id(pattern.body), 0) + 1
    selected = [pattern for pattern in patterns if id(pattern.body) in counts]
    selected.sort(key=lambda item: counts[id(item.body)], reverse=True)
    return selected, counts


def _apply_replacements(segments, choices, call_op, index_by_body):
    by_commands = {}
    for segment in segments:
        for local_start, local_end, pattern in choices.get(id(segment), []):
            by_commands.setdefault(id(segment.commands), []).append(
                (
                    segment.start + local_start,
                    segment.start + local_end,
                    index_by_body[pattern.body] - 107,
                )
            )
    touched = {id(segment.commands): segment.commands for segment in segments}
    for commands in touched.values():
        intervals = by_commands.get(id(commands), [])
        for start, end, subr_index in sorted(intervals, reverse=True):
            commands[start:end] = [Command(call_op, (subr_index,))]


def _apply_global(records):
    segments = [
        segment for _, _, old_segments, _ in records for segment in old_segments
    ]
    patterns, call_costs = _candidate_patterns(segments, 3, GLOBAL_EXCLUDED_OPS)
    choices = _choose_matches(segments, patterns, call_costs)
    if not choices:
        return None
    selected, counts = _selected_patterns(segments, choices, patterns)
    index_by_body = {pattern.body: index for index, pattern in enumerate(selected)}
    _apply_replacements(segments, choices, "callgsubr", index_by_body)
    for charstring, commands, _, _ in records:
        _set_program(charstring, commands)
    total_calls = sum(counts[id(pattern.body)] for pattern in selected)
    return _make_subr_index(selected, None, None), total_calls


def _apply_local(fd_index, records, private, global_subrs):
    fd_commands = [
        (charstring, commands, segments)
        for charstring, commands, segments, glyph_fd in records
        if glyph_fd == fd_index
    ]
    all_segments = [
        segment
        for _, commands, _ in fd_commands
        for segment in _segments(commands, LOCAL_EXCLUDED_OPS)
    ]
    patterns, call_costs = _candidate_patterns(
        all_segments, 3, LOCAL_EXCLUDED_OPS, private
    )
    choices = _choose_matches(all_segments, patterns, call_costs)
    if not choices:
        return None
    selected, counts = _selected_patterns(all_segments, choices, patterns)
    index_by_body = {pattern.body: index for index, pattern in enumerate(selected)}
    _apply_replacements(all_segments, choices, "callsubr", index_by_body)
    for charstring, commands, _ in fd_commands:
        _set_program(charstring, commands)
    total_calls = sum(counts[id(pattern.body)] for pattern in selected)
    return _make_subr_index(selected, private, global_subrs), total_calls


def _build_record(charstring, fd_index):
    commands = _parse_commands(charstring.program)
    segments = _segments(commands, GLOBAL_EXCLUDED_OPS)
    return [charstring, commands, segments, fd_index]


def _estimated_index_length(items):
    if not items:
        return 2
    total = 0
    for item in items:
        clone = _cloned_charstring(item)
        if clone.bytecode is None:
            clone.compile()
        total += len(clone.bytecode)
    last_offset = total + len(items)
    off_size = 1 if last_offset < 256 else 2 if last_offset < 65536 else 3
    return 2 + 1 + (len(items) + 1) * off_size + total


def _privates(font):
    if hasattr(font, "FDArray"):
        return [fd.Private for fd in font.FDArray]
    return [font.Private]


def _original_size(font, privates):
    size = 0
    for charstring in font.CharStrings.values():
        clone = _cloned_charstring(charstring)
        if clone.bytecode is None:
            clone.compile()
        size += len(clone.bytecode)
    size += _estimated_index_length(font.GlobalSubrs)
    for private in privates:
        if hasattr(private, "Subrs") and private.Subrs:
            size += _estimated_index_length(private.Subrs) + 3
    return size


def _subroutine_index_size(items):
    return _estimated_index_length(
        [
            T2CharString(
                bytecode=item.bytecode,
                program=list(item.program) if item.program is not None else None,
                private=item.private,
                globalSubrs=item.globalSubrs,
            )
            for item in items
        ]
    )


def _new_size(font, privates, global_subrs, local_subrs):
    size = 0
    for charstring in font.CharStrings.values():
        size += _program_byte_length(charstring.program, charstring.private)
    size += _subroutine_index_size(list(global_subrs))
    for private in privates:
        replacement = local_subrs.get(id(private))
        if replacement is not None:
            size += _subroutine_index_size(list(replacement)) + 3
        elif hasattr(private, "Subrs") and private.Subrs:
            size += 2
    return size


def _snapshot(font, privates):
    charstrings = []
    for charstring in font.CharStrings.values():
        if charstring.bytecode is None:
            clone = T2CharString(
                program=list(charstring.program),
                private=charstring.private,
                globalSubrs=charstring.globalSubrs,
            )
            charstrings.append((charstring, None, clone))
        else:
            clone = T2CharString(
                bytecode=charstring.bytecode,
                private=charstring.private,
                globalSubrs=charstring.globalSubrs,
            )
            charstrings.append((charstring, charstring.bytecode, clone))
    locals_ = []
    for private in privates:
        had_subrs = hasattr(private, "Subrs")
        locals_.append(
            (
                private,
                had_subrs,
                list(private.Subrs.items) if had_subrs else None,
                "Subrs" in private.rawDict,
                private.rawDict.get("Subrs"),
            )
        )
    return (
        font,
        charstrings,
        locals_,
        [_cloned_charstring(item) for item in font.GlobalSubrs],
    )


def _restore(snapshot):
    font, charstrings, locals_, global_items = snapshot
    for charstring, bytecode, clone in charstrings:
        if bytecode is not None:
            charstring.setBytecode(bytecode)
        else:
            charstring.setProgram(list(clone.program))
    for private, had_subrs, subrs, had_raw, raw_value in locals_:
        if had_subrs:
            index = SubrsIndex()
            index.items = list(subrs)
            private.Subrs = index
        elif hasattr(private, "Subrs"):
            del private.Subrs
        if had_raw:
            private.rawDict["Subrs"] = raw_value
        else:
            private.rawDict.pop("Subrs", None)
    font.GlobalSubrs.items = list(global_items)
    for attribute in ("file", "offsets"):
        if hasattr(font.GlobalSubrs, attribute):
            delattr(font.GlobalSubrs, attribute)


def subroutinize(cff):
    for font_name in cff.fontNames:
        font = cff[font_name]
        if getattr(cff, "major", 1) != 1:
            continue

        privates = _privates(font)
        snapshot = _snapshot(font, privates)
        before = _original_size(font, privates)
        for charstring in font.CharStrings.values():
            desubroutinizeCharString(charstring)
        cff.GlobalSubrs.items = []
        for private in privates:
            if hasattr(private, "Subrs"):
                del private.Subrs
            private.rawDict.pop("Subrs", None)

        records = []
        if hasattr(font, "FDArray"):
            glyph_names = font.charset or font.CharStrings.keys()
            for glyph_id, glyph_name in enumerate(glyph_names):
                charstring = font.CharStrings[glyph_name]
                if hasattr(charstring, "fdSelectIndex"):
                    fd_index = charstring.fdSelectIndex
                else:
                    fd_index = font.FDSelect[glyph_id]
                records.append(_build_record(charstring, fd_index))
        else:
            for charstring in font.CharStrings.values():
                records.append(_build_record(charstring, 0))

        global_result = _apply_global(records)
        if global_result is None:
            _restore(snapshot)
            continue
        global_subrs, _global_call_count = global_result

        local_subrs = {}
        fd_count = len(font.FDArray) if hasattr(font, "FDArray") else 1
        for fd_index in range(fd_count):
            result = _apply_local(
                fd_index, records, privates[fd_index], font.GlobalSubrs
            )
            if result is not None:
                local_subrs[id(privates[fd_index])] = result[0]

        after = _new_size(font, privates, global_subrs, local_subrs)
        if after >= before:
            _restore(snapshot)
            continue

        font.GlobalSubrs.items = list(global_subrs.items)
        for index in ("file", "offsets"):
            if hasattr(font.GlobalSubrs, index):
                delattr(font.GlobalSubrs, index)

        for private in privates:
            replacement = local_subrs.get(id(private))
            if replacement is not None:
                for subr in replacement:
                    subr.globalSubrs = font.GlobalSubrs
                private.Subrs = replacement
                private.rawDict["Subrs"] = 0
                for index_name in ("file", "offsets"):
                    if hasattr(replacement, index_name):
                        delattr(replacement, index_name)
            else:
                if hasattr(private, "Subrs"):
                    del private.Subrs
                private.rawDict.pop("Subrs", None)
