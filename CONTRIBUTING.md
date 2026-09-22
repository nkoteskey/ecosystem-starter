# Contributing

Thanks for looking at ecosystem-starter.

1. Open an issue first for anything larger than a typo, so the change can
   be discussed before it is written.
2. Every change runs the same checks CI runs; run them locally before
   opening a pull request:

   ```sh
   uvx ruff check scripts && uvx ruff format --check scripts
   shellcheck -x scripts/*.sh scripts/git-hooks/pre-push bootstrap.sh
   uvx pytest -q   # about 90 s
   ```

3. A behavior change comes with a test that pins the new behavior. A bug
   fix comes with the test that would have caught the bug.
4. Keep the README honest: if a change adds or removes a property the
   README claims, change the README in the same pull request.
5. No new dependency without a sentence in the pull request saying why,
   and what its license is.

By contributing you agree that your contribution is licensed under the
repository's license.
