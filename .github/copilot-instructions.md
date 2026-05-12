# Custom GitHub Copilot Instructions

## Standard Issue-Solving Workflow

When asked to solve a GitHub issue, always follow these steps in order:

0. **Checkout main and pull latest**
   ```bash
   git checkout main
   git pull
   ```
1. **Read the GitHub issue** — Understand the problem fully before touching any code.
2. **Create a branch** using the issue prefix and a short slug.
   - Format: `<type>/<issue-prefix>-<slug>` — e.g., `fix/p0-01-month-matching`
3. **Understand the relevant code** — Search and read the affected files before making changes.
4. **Implement the smallest safe fix** — No unrelated changes, no broad refactors.
5. **Add or update regression tests** — Cover the bug or new behavior.
6. **Run the relevant tests** — `pytest tests/` or the targeted test file.
7. **Run lint/type checks** — `ruff check . --fix` then `ruff format .`
8. **Report a summary** including:
   - Issue title
   - Branch name
   - Files changed
   - What changed and why
   - Tests added or updated
   - Test and lint results
9. **Create a pull request** linked to the issue using `Fixes #<ISSUE_NUMBER>` in the description.
10. **Keep the PR up to date** — after every follow-up commit on a branch that already has an open
    PR, update both the PR title and description to reflect the current state of all changes made.
    Tick off any completed acceptance criteria in the PR checklist.
    - Use `gh pr edit <PR_NUMBER> --title "..." --body-file <file>` — write the PR body
      to a temp file first, pass it with `--body-file`, then delete the file.
    - **Never** pass a multiline body inline via `--body "..."`: PowerShell corrupts the
      content (newlines become `∙` characters; backticks become `\x5c` escapes).

## Issue-Solving Rules
- Always read `AGENTS.md` and `CLAUDE.md` before starting any issue work.
- Solve **one issue only** per branch and PR.
- Do **not** refactor unrelated code.
- Keep behavior unchanged unless the issue explicitly states the current behavior is unsafe or wrong.
- Prefer small, reviewable changes.
- Add tests for every bug fix or new feature.
- Do **not** skip tests unless the repo has no working test setup — if so, explain exactly why.
- Do **not** close the issue manually. Link the PR using `Fixes #ISSUE_NUMBER`.
- Ask the user before making any broad architectural changes.

## Solve One Issue Per Branch
- Each branch should solve **one** issue from the GitHub issue tracker.
- Use the branch naming convention: `<type>/<issue-number>-<description>`
- Examples: `feat/123-add-feature`, `fix/456-resolve-bug`, `chore/789-update-docs`
- Do not combine multiple issues in a single branch or PR.

## Conventional Commits
- Always use [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) for commit messages and pull request titles.
- Format: `<type>(<scope>): <description>`
- Types: `feat`, `fix`, `chore`, `docs`, `refactor`, `perf`, `test`, `ci`
- Scopes should be specific to the domain being changed (e.g., `sensor`, `flow`, `config`)
- Always include `Fixes #<ISSUE_NUMBER>` in the PR description

## Use the Latest Version
- Always use the latest version of the code provided by the user.
- Use the latest stable versions of all languages and frameworks.
- Use the latest stable versions of all libraries unless constrained by requirements.

## Code Quality
- All code MUST use safe and secure coding practices.
- All code MUST be fully optimized for performance and maintainability.
- Avoid clear passwords, hardcoded secrets, and common security gaps.
- Follow PEP 8 and the project's style guide.
- Write type hints for all function parameters and return types.
- Include docstrings for all public modules, classes, functions, and methods.
- **Never use `==` or `!=` to compare floating-point values.** In production code use an epsilon
  guard (`abs(x) > 1e-9` instead of `x != 0`). In tests always use `pytest.approx()`.

## Write Modular Code
- Break code into modules and components for easy reuse.
- Maximize code reuse (DRY principle).
- Minimize technical debt.

## Python Instructions
- Use snake_case for variable and function names.
- Use CamelCase for class names.
- Include type hints for function parameters and return types.
- Write docstrings following PEP 257 conventions.
- Use f-strings for formatting instead of .format() or %.
- Prefer duck-typing tests (hasattr) over isinstance checks.
- Use modern Python 3.9+ syntax.
- Use the union operator (|) for type unions instead of typing.Union.
- Use pathlib for path operations instead of os.path.
- Explicitly set encoding='utf-8' when using open() in text mode.
- Prefer argparse over optparse.
- Use itertools for common iterable operations.

## Always Provide File Names
- Always provide the complete file path in responses.
- Help users understand where code changes should be placed.

## Avoid Triggering Public Code Warnings
- Avoid generating code verbatim from public sources.
- Modify public code examples to be sufficiently different.
- Provide attribution when using public patterns.

## Do Not
- Do not refactor planner or safety logic.
- Do not change runtime behavior unless specifically requested.
- Do not fix unrelated bugs in the same PR.
- Do not reformat the entire codebase unless required by tooling setup.
- Do not generate code without understanding the context first.



## Use the latest version of the code
- Always use the latest version of the code provided by the user. If the user provides a file, use that file as the base for your changes. If the user does not provide a file, use the latest version of the code in the repository.

## Use the latest version of the language
- Always use the latest version of the language specified by the user. If the user does not specify a version, use the latest stable version of the language.

## Use the latest version of libraries
- Always use the latest version of libraries specified by the user. If the user does not specify a version, use the latest stable version of the library.

## Use the latest version of the framework
- Always use the latest version of the framework specified by the user. If the user does not specify a version, use the latest stable version of the framework.

## Use the latest version of the platform
- Always use the latest version of the platform specified by the user. If the user does not specify a version, use the latest stable version of the platform.

## Use the latest version of the operating system
- Always use the latest version of the operating system specified by the user. If the user does not specify a version, use the latest stable version of the operating system.

## Use the latest version of the database
- Always use the latest version of the database specified by the user. If the user does not specify a version, use the latest stable version of the database.

## Avoid triggering public code warnings

- Avoid generating code verbatim from public code examples. Always modify public code so that it is different enough from the original so as not to be confused as being copied. When you do so, provide a footnote to the user informing them.

## Always provide file names

- Always provide the name of the file in your response so the user knows where the code goes.

## Write modular code

- Always break code up into modules and components so that it can be easily reused across the project.

## Write safe code

- All code you write MUST use safe and secure coding practices. ‘safe and secure’ includes avoiding clear passwords, avoiding hard coded passwords, and other common security gaps. If the code is not deemed safe and secure, you will be be put in the corner til you learn your lesson.

## Incentivize better code quality

- All code you write MUST be fully optimized. ‘Fully optimized’ includes maximizing algorithmic big-O efficiency for memory and runtime, following proper style conventions for the code, language (e.g. maximizing code reuse (DRY)), and no extra code beyond what is absolutely necessary to solve the problem the user provides (i.e. no technical debt). If the code is not fully optimized, you will be fined $100.

## Python Instructions

- Use snake_case for variable and function names.
- Use CamelCase for class names.
- Include type hints for function parameters and return types.
- Write docstrings for all public modules, classes, functions, and methods.
- Test your code before you provide a change with with at least 3 edge case input values
- Write clear and concise comments for each function.
- Ensure functions have descriptive names and include type hints.
- Provide docstrings following PEP 257 conventions.
- Use the `typing` module for type annotations (e.g., `List[str]`, `Dict[str, int]`).
- Break down complex functions into smaller, more manageable functions.

## Code quality

- Where possible, prefer duck-typing tests than isinstance, e.g. hasattr(x, attr) not isinstance(x, SpecificClass)
- Use modern Python 3.9+ syntax
- Prefer f-strings for formatting strings rather than .format or % formatting
- When creating log statements, never use runtime string formatting. Use the extra argument and % placeholders in the log message
- When generating union types, use the union operator, | , not the typing.Union type
- When merging dictionaries, use the union operator
- When writing type hints for standard generics like dict, list, tuple, use the PEP-585 spec, not typing.Dict, typing.List, etc.
- Use type annotations in function and method signatures, unless the rest of the code base does not have type signatures
- Do not add inline type annotations for local variables when they are declared and assigned in the same statement.
- Prefer pathlib over os.path for operations like path joining
- When using open() in text-mode, explicitly set encoding to utf-8
- Prefer argparse over optparse
- Use the builtin methods in the itertools module for common tasks on iterables rather than creating code to achieve the same result
- When creating dummy data, don't use "Foo" and "Bar", be more creative
- When creating dummy data in strings like names don't just create English data, create data in a range of languages like English, Spanish, Mandarin, and Hindi
- When asked to create a function, class, or other piece of standalone code, don't append example calls unless otherwise told to

## General Instructions

- Always prioritize readability and clarity.
- For algorithm-related code, include explanations of the approach used.
- Write code with good maintainability practices, including comments on why certain design decisions were made.
- Handle edge cases and write clear exception handling.
- For libraries or external dependencies, mention their usage and purpose in comments.
- Use consistent naming conventions and follow language-specific best practices.
- Write concise, efficient, and idiomatic code that is also easily understandable.
- Use meaningful variable and function names that reflect their purpose.
- Include comments for complex logic or non-obvious code sections.
- Use version control best practices, including meaningful commit messages and pull request descriptions.
- Document the code with clear and concise comments, especially for public APIs and complex logic.
- Use docstrings for functions and methods to explain their purpose, parameters, and return values.
- Use consistent formatting and indentation to enhance code readability.

## Code Style and Formatting

- Follow the **PEP 8** style guide for Python.
- Maintain proper indentation (use 4 spaces for each level of indentation).
- Ensure lines do not exceed 79 characters.
- Place function and class docstrings immediately after the `def` or `class` keyword.
- Use blank lines to separate functions, classes, and code blocks where appropriate.

## Edge Cases and Testing

- Always include test cases for critical paths of the application.
- Account for common edge cases like empty inputs, invalid data types, and large datasets.
- Include comments for edge cases and the expected behavior in those cases.
- Write unit tests for functions and document them with docstrings explaining the test cases.

## Example of Proper Documentation

```python
def calculate_area(radius: float) -> float:
    """
    Calculate the area of a circle given the radius.

    Parameters:
    radius (float): The radius of the circle.

    Returns:
    float: The area of the circle, calculated as π * radius^2.
    """
    import math
    return math.pi * radius ** 2
```
