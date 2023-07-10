"""
This type stub file was generated by pyright.
"""

from IPython.core.displayhook import DisplayHook

"""Terminal input and output prompts."""

class Prompts:
    def __init__(self, shell) -> None: ...
    def vi_mode(self): ...
    def in_prompt_tokens(self): ...
    def continuation_prompt_tokens(self, width=...): ...
    def rewrite_prompt_tokens(self): ...
    def out_prompt_tokens(self): ...

class ClassicPrompts(Prompts):
    def in_prompt_tokens(self): ...
    def continuation_prompt_tokens(self, width=...): ...
    def rewrite_prompt_tokens(self): ...
    def out_prompt_tokens(self): ...

class RichPromptDisplayHook(DisplayHook):
    """Subclass of base display hook using coloured prompt"""

    def write_output_prompt(self): ...
    def write_format_data(self, format_dict, md_dict=...) -> None: ...
