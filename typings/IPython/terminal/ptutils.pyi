"""
This type stub file was generated by pyright.
"""

from prompt_toolkit.completion import Completer
from prompt_toolkit.lexers import Lexer

"""prompt-toolkit utilities

Everything in this module is a private API,
not to be used outside IPython.
"""
_completion_sentinel = ...

class IPythonPTCompleter(Completer):
    """Adaptor to provide IPython completions to prompt_toolkit"""

    def __init__(self, ipy_completer=..., shell=...) -> None: ...
    @property
    def ipy_completer(self): ...
    def get_completions(self, document, complete_event): ...

class IPythonPTLexer(Lexer):
    """
    Wrapper around PythonLexer and BashLexer.
    """

    def __init__(self) -> None: ...
    def lex_document(self, document): ...
