from dataclasses import replace
from typing import Optional, Tuple

from std2.types import never

from ..snippets.parse import requires_snip
from .types import (
    UTF8,
    UTF16,
    UTF32,
    BaseRangeEdit,
    Cursors,
    Edit,
    RangeEdit,
    SnippetEdit,
    SnippetRangeEdit,
    WTF8Pos,
)


def _shift(cursor: Cursors, edit: BaseRangeEdit) -> Tuple[WTF8Pos, WTF8Pos]:
    row, u8, u16, u32 = cursor
    if edit.encoding == UTF16:
        col = u16
    elif edit.encoding == UTF8:
        col = u8
    elif edit.encoding == UTF32:
        col = u32
    else:
        never(edit.encoding)

    (b_row, b_col), (e_row, e_col) = edit.begin, edit.end
    edit_col = edit.cursor_pos
    diff = col - edit_col

    if b_row == row:
        if b_col > edit_col and diff < 0:
            new_b_col = b_col + diff
        else:
            new_b_col = b_col
    else:
        new_b_col = b_col

    if e_row == row:
        if diff > 0 and e_col >= edit_col:
            new_e_col = e_col + diff
        else:
            new_e_col = e_col
    else:
        new_e_col = e_col

    new_begin, new_end = (b_row, max(0, new_b_col)), (e_row, max(0, new_e_col))
    return new_begin, new_end


def sanitize(cursor: Cursors, edit: Edit) -> Optional[Edit]:
    row, *_ = cursor
    if isinstance(edit, SnippetRangeEdit):
        if row == -1:
            if edit.fallback == edit.new_text:
                return SnippetEdit(grammar=edit.grammar, new_text=edit.new_text)
            elif not requires_snip(edit.new_text):
                return Edit(new_text=edit.new_text)
            else:
                return None
        elif fallback := edit.fallback:
            return SnippetEdit(grammar=edit.grammar, new_text=fallback)
        elif not requires_snip(edit.new_text):
            return Edit(new_text=edit.new_text)
        else:
            begin, end = _shift(cursor, edit=edit)
            return replace(edit, begin=begin, end=end)
    elif isinstance(edit, RangeEdit):
        if fallback := edit.fallback:
            return Edit(new_text=fallback)
        elif not requires_snip(edit.new_text):
            return Edit(new_text=edit.new_text)
        else:
            return None
    elif isinstance(edit, SnippetEdit):
        return edit
    else:
        return Edit(new_text=edit.new_text)
