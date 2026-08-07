"""CLI entry points — thin wrappers around core logic.

Each module provides a main() function that:
1. Parses command-line arguments
2. Calls into evaluation/ or data/ layers for computation
3. Formats and prints output

No business logic lives here — it's all in evaluation/ and data/.

Entry points are configured in pyproject.toml under [project.scripts].
They can be invoked as:
    spp-draft pick 6
    spp-targets --bucket SP
    python3 -m statsplusplus.cli.draft_board pick 6
"""
