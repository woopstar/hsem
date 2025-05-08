# Custom GitHub Copilot Instructions

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
