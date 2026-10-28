"""Culprit: find the commit that broke your code.

Culprit writes a test that reproduces a bug, then searches your git history in
parallel Nebius Sandboxes to find the exact commit that introduced it. NVIDIA
Nemotron models on Nebius Token Factory write the test, explain the root cause
and propose a fix, and every claim is checked by running code.
"""

__version__ = "0.1.0"
