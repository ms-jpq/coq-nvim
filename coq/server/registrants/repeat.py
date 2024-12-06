from dataclasses import replace
from typing import Optional

from ...registry import rpc
from ...shared.repeat import sanitize
from ...shared.types import ContextualEdit, Edit
from ..context import context
from ..edit import edit
from ..rt_types import Stack
from ..state import state


def _edit(prev: Edit) -> Optional[Edit]:
    sanitized = sanitize((-1, -1, -1, -1), edit=prev)
    new_edit = (
        ContextualEdit(
            new_text=sanitized.new_text, old_prefix="", new_prefix=sanitized.new_text
        )
        if type(sanitized) is Edit
        else sanitized
    )
    return new_edit


@rpc()
async def repeat(stack: Stack) -> None:
    ctx = await context(
        options=stack.settings.match, state=state(), change=None, manual=True
    )
    s = state(context=ctx)
    metric = s.last_edit
    if sanitized := _edit(metric.comp.primary_edit):
        new_metric = replace(
            metric,
            comp=replace(metric.comp, primary_edit=sanitized, secondary_edits=()),
        )
        await edit(stack=stack, state=s, metric=new_metric, synthetic=True)
