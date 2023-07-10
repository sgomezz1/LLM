"""
This type stub file was generated by pyright.
"""

import ast
from typing import Callable
from typing import Dict
from typing import Union

from IPython.utils.decorators import undoc
from prompt_toolkit.filters import Condition
from prompt_toolkit.filters import Filter
from prompt_toolkit.key_binding import KeyPressEvent
from prompt_toolkit.layout.layout import FocusableElement

"""
Filters restricting scope of IPython Terminal shortcuts.
"""

@undoc
@Condition
def cursor_in_leading_ws(): ...
def has_focus(value: FocusableElement):  # -> Condition:
    """Wrapper around has_focus adding a nice `__name__` to tester function"""
    ...

@undoc
@Condition
def has_line_below() -> bool: ...
@undoc
@Condition
def is_cursor_at_the_end_of_line() -> bool: ...
@undoc
@Condition
def has_line_above() -> bool: ...
@Condition
def ebivim(): ...
@Condition
def supports_suspend(): ...
@Condition
def auto_match(): ...
def all_quotes_paired(quote, buf): ...

_preceding_text_cache: Dict[Union[str, Callable], Condition] = ...
_following_text_cache: Dict[Union[str, Callable], Condition] = ...

def preceding_text(pattern: Union[str, Callable]): ...
def following_text(pattern): ...
@Condition
def not_inside_unclosed_string(): ...
@Condition
def navigable_suggestions(): ...
@Condition
def readline_like_completions(): ...
@Condition
def is_windows_os(): ...

class PassThrough(Filter):
    """A filter allowing to implement pass-through behaviour of keybindings.

    Prompt toolkit key processor dispatches only one event per binding match,
    which means that adding a new shortcut will suppress the old shortcut
    if the keybindings are the same (unless one is filtered out).

    To stop a shortcut binding from suppressing other shortcuts:
    - add the `pass_through` filter to list of filter, and
    - call `pass_through.reply(event)` in the shortcut handler.
    """

    def __init__(self) -> None: ...
    def reply(self, event: KeyPressEvent): ...
    def __call__(self): ...

pass_through = ...
default_buffer_focused = ...
KEYBINDING_FILTERS = ...

def eval_node(node: Union[ast.AST, None]): ...
def filter_from_string(code: str): ...

__all__ = ["KEYBINDING_FILTERS", "filter_from_string"]
