"""Strandline's situation-report copilot.

    from copilot import answer
    answer("runs/my_run", "how much area is flooded?", "en")

Everything it can say comes from one run folder's own output files. See
`copilot/evidence.py` for why that is the only source, and `copilot/verifier.py`
for what happens to a number that is not in it.
"""

from copilot.executor import answer

__all__ = ["answer"]
