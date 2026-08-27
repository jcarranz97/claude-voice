"""A local voice, an ear and a HUD for a coding agent in a terminal.

The modules here are also hook entry points, run directly as scripts by the
host agent. Importing this package must therefore stay cheap and side-effect
free -- a hook that pays for a model import before deciding it has nothing to
say is a hook that slows down every turn.
"""

__version__ = "0.1.0"
