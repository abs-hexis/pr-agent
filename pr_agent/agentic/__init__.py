"""HEXIS Agentic PR Reviewer.

A tool-using LLM loop that reviews a PR with read access to the full repo.
Local-first design — outputs `review.md` to disk so we can iterate on prompts
fast without a docker rebuild or webhook round-trip.
"""

__version__ = "0.1.0"
