"""Deps for the run child in tests (WORKER_CHILD_DEPS=tests.child_deps:deps): fake LLM and fake web."""
from tests import fakes


def deps():
    return fakes.make_deps(fakes.world_llm())
