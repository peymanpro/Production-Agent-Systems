# Production Agent Systems

Production-oriented engineering of an LLM-driven agent runtime with explicit state, tools, policy, reliability, security, recovery, observability, and evaluation boundaries.

> Status: Repository foundation initialized. Agent behavior is intentionally not implemented yet.

## Purpose

This project is a capstone for engineering AI agents as operational systems rather than prompt-and-tool demos. The implementation will keep model decisions separate from runtime authority and will make important execution behavior testable and observable.

## Current Foundation

The repository establishes a clean Python package layout with src/, tests/, docs/, examples/, and config/ boundaries. The first implementation unit does not add a planner, model provider, tool execution engine, or production API.

## Development

    python -m venv .venv
    python -m pip install -e .
    python -m unittest discover -s tests -v

Pytest, Ruff, and mypy are introduced in the next foundation unit.
