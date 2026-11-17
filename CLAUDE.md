# Agent Guidelines for Python Code Quality

**Every lead session ends by updating `docs/defence-brief.md`; subagents never touch it.** Update sections 1-2 when the design changed, plus a new dated evidence entry in section 3 with numbers from the ledger, the shift logs and Metaculus. Public repo: no forecasts, rationales or claims of open questions.

This document provides guidelines for maintaining high-quality Python code. These rules must be followed by all AI coding agents and contributors.

## Your Core Principles

All code you write must be fully optimized.

"Fully optimized" includes:

- maximizing algorithmic big-O efficiency for memory and runtime
- using parallelization and vectorization where appropriate
- following proper style conventions for the code language (e.g. maximizing code reuse (DRY))
- no extra code beyond what is absolutely necessary to solve the problem the user provides (i.e. no technical debt)
  - If a Python library can be imported to significantly reduce the amount of new code required to implement a function at optimal performance, and the library itself is small and does not have much overhead, always use the library instead.

Before handing code to the user, check it against this definition and do another pass if it falls short.

## Preferred Tools

- Use `uv` for Python package management and to create a `.venv` if it is not present.
- Ensure `ipykernel` and `ipywidgets` is installed in `.venv` for Jupyter Notebook compatability. This should not be in package requirements.
- Use `tqdm` to track long-running loops within Jupyter Notebooks. The `description` of the progress bar should be contextually sensitive.
- Use `orjson` for JSON loading/dumping.
- When reporting error to the console, use `logger.error` instead of `print`.
- If the project involves the creation of images (e.g. PNG/WEBP), you have permission to use the Read tool to verify the rendered images fit the user and application requirements.
- For data science:
  - Always use `polars` instead of `pandas` for data frame manipulation.
  - If a `polars` dataframe will be printed, never simultaneously print the number of entries in the dataframe nor the schema as it is redundant.
  - Never ingest more than 10 rows of a data frame at a time. Only analyze subsets of code to avoid overloading your memory context.
- For creating databases:
  - Do not denormalized unless explicitly prompted to do so.
  - Always use the most appropriate datatype, such as `DATETIME/TIMESTAMP` for datetime-related fields.
  - Use `ARRAY` datatypes for nested fields. Never save as `TEXT/STRING`.
- In Jupyter Notebooks, DataFrame objects within conditional blocks should be explicitly `print()` as they will not be printed automatically.

## Code Style and Formatting

- Use meaningful, descriptive variable and function names
- Follow PEP 8 style guidelines
- Use 4 spaces for indentation (never tabs)
- Never use emoji, or unicode that emulates emoji (e.g. ✓, ✗). The only exception is when writing tests and testing the impact of multibyte characters.
- Use snake_case for functions/variables, PascalCase for classes, UPPER_CASE for constants
- Limit line length to 88 characters (ruff formatter standard)
- Avoid including redundant comments which are tautological or self-demonstating (e.g. cases where it is easily parsable what the code does at a glance so the comment does)
- Avoid including comments which leak what this file contains, or leak the original user prompt, especially if it's irrelevant to the output code.

## Documentation

- Include docstrings for all public functions, classes, and methods
- Document function parameters, return values, and exceptions raised
- Keep comments up-to-date with code changes
- Include examples in docstrings for complex functions

Example docstring:

```python
def calculate_total(items: list[dict], tax_rate: float = 0.0) -> float:
    """Calculate the total cost of items including tax.

    Args:
        items: List of item dictionaries with 'price' keys
        tax_rate: Tax rate as decimal (e.g., 0.08 for 8%)

    Returns:
        Total cost including tax

    Raises:
        ValueError: If items is empty or tax_rate is negative
    """
```

## Type Hints

- Use type hints for all function signatures (parameters and return values)
- Never use `Any` type unless absolutely necessary
- Run mypy and resolve all type errors
- Use `Optional[T]` or `T | None` for nullable types

## Error Handling

- Never silently swallow exceptions without logging
- Never use bare `except:` clauses
- Catch specific exceptions rather than broad exception types
- Use context managers (`with` statements) for resource cleanup
- Provide meaningful error messages

## Function Design

- Keep functions focused on a single responsibility
- Never use mutable objects (lists, dicts) as default argument values
- Limit function parameters to 5 or fewer
- Return early to reduce nesting

## Class Design

- Keep classes focused on a single responsibility
- Keep `__init__` simple; avoid complex logic
- Use dataclasses for simple data containers
- Prefer composition over inheritance
- Avoid creating additional class functions if they are not necessary
- Use `@property` for computed attributes

## Testing

- Write unit tests for all new functions and classes
- Mock external dependencies (APIs, databases, file systems)
- Use pytest as the testing framework
- Never run tests you generate without first saving them as their own discrete file
- Never delete files created as a part of testing.
- Ensure the folder used for test outputs is present in `.gitignore`
- Follow the Arrange-Act-Assert pattern
- Do not commit commented-out tests

## Imports and Dependencies

- Avoid wildcard imports (`from module import *`)
- Document dependencies in `pyproject.toml`
- Use `uv` for fast package management and dependency resolution
- Organize imports: standard library, third-party, local imports
- Use `isort` to automate import formatting

## Python Best Practices

- Never use mutable default arguments
- Use context managers (`with` statement) for file/resource management
- Use `is` for comparing with `None`, `True`, `False`
- Use f-strings for string formatting
- Use list comprehensions and generator expressions
- Use `enumerate()` instead of manual counter variables

## Benchmarking and Optimization

- Never run benchmarks in parallel, as the benchmarks will compete for resources and the results will be invalid
- Never game the benchmarks. Do not manipulate the benchmarks themselves to satisfy any required performance constraints
- If benchmarking against another crate or library, ensure the benchmarks are apples-to-apples comparisons
- Ensure benchmark tests are independent. If the tests are dependent due to a feature (e.g. caching), ensure the feature is disabled

## Security

- Never store secrets, API keys, or passwords in code. Only store them in `.env`
  - Ensure `.env` is declared in `.gitignore`.
  - Never print or log URLs to console if they contain an API key
- Use environment variables for sensitive configuration
- Never log sensitive information (passwords, tokens, PII)

## Version Control

- Write clear, descriptive commit messages
- Never commit commented-out code; delete it
- Never commit debug print statements or breakpoints
- Never commit credentials or sensitive data

## Tools

- Use Ruff for code formatting and linting (replaces Black, isort, flake8)
- Use mypy for static type checking
- Use `uv` for package management (faster alternative to pip)
- Use pytest for testing

## Before Committing

- [ ] All tests pass
- [ ] Type checking passes (mypy)
- [ ] Code formatter and linter pass (Ruff)
- [ ] All functions have docstrings and type hints
- [ ] No commented-out code or debug statements
- [ ] No hardcoded credentials

---

Where full optimization and clarity pull apart, choose the clearer code unless the task needs the speed.