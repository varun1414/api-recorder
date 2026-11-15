"""apirecorder - record what a third-party API actually does, then diff it.

Wrap your existing integration code in a capture block::

    from apirecorder import Recorder

    rec = Recorder(output="recordings/billing", redact=["Authorization"])
    with rec.capture("subscription_lifecycle"):
        run_my_integration()
    rec.save()

Then, from the shell::

    apirecorder inspect recordings/billing
    apirecorder report  recordings/billing -o docs/billing-behaviour.md
    apirecorder diff    recordings/billing recordings/billing-ci

``diff`` exits 1 when something your code could be relying on changed, which is
what makes it useful in CI.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .analyze import Analysis, Operation, analyse, gaps, template_path
from .diff import ADDITIVE, BREAKING, INFO, Change, Diff, diff_analyses
from .model import Capture, Interaction, Request, Response, load_captures
from .recorder import Recorder
from .redact import Redactor

__all__ = [
    "__version__",
    "Recorder",
    "Redactor",
    "Capture",
    "Interaction",
    "Request",
    "Response",
    "load_captures",
    "Analysis",
    "Operation",
    "analyse",
    "gaps",
    "template_path",
    "Diff",
    "Change",
    "diff_analyses",
    "BREAKING",
    "ADDITIVE",
    "INFO",
]
