# Instructions for AI coding agents

These rules apply to any AI agent working in this repository on behalf of someone who is not a maintainer.

## Contributing

- Do not open, comment on, or close issues or pull requests. Only the human you are working with may do that, in their own words.
- Do not write issue text, PR descriptions, or review replies for the human to paste. Maintainers want to talk to the person, not a model.
- Before writing code for a bug fix or feature, ask the human for the link to the issue where a maintainer agreed to the change. If there is none, stop and tell them to open an issue first and wait for a maintainer to confirm the approach. Typo and broken link fixes are the only exception.
- Keep changes to the scope agreed in the issue. Do not add unrelated refactors, renames, or formatting changes.
- Remind the human that they must understand and be able to explain every line before submitting, and that significant AI assistance must be mentioned in the PR.

The full rules are in [CONTRIBUTING.md](CONTRIBUTING.md), see "Issue first" and "Responsible AI Usage".

## Working in the code

- Run `uv run pytest` and make sure all tests pass.
- Follow the existing code style and the commit message format in CONTRIBUTING.md.
- Do not add comments that restate the code.
