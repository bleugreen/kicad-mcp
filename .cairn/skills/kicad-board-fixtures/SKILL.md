---
name: kicad-board-fixtures
description: Use when adding, editing, or committing a KiCad board fixture (.kicad_pcb, .kicad_sch, or .kicad_pro) under tests/fixtures in kicad-mcp. Covers the .gitignore negation required so the file is actually tracked (an ignored path commits as a silent empty commit), the synthetic fixture's one-tab indentation for text patches, and validating that KiCad can still load an edited board.
---

# Committing and editing KiCad board fixtures

The PCB-model tests depend on small synthetic board fixtures committed under
`tests/fixtures/`. These are the rules for adding or editing one without silently
losing it.

## The .gitignore trap

The repo `.gitignore` ignores `*.kicad_pcb`, `*.kicad_sch`, `*.kicad_pro`, and
`.kicad_mcp.yaml`. A write to an ignored path **commits silently as an empty commit**:
the file exists on disk (so pytest passes locally) but is never tracked, so it is absent
from the branch tip and vanishes on worktree cleanup.

- `tests/fixtures/*.kicad_pcb` is already re-included by a trailing
  `!tests/fixtures/*.kicad_pcb` negation. To commit a `.kicad_sch` or `.kicad_pro`
  fixture, first add the matching negation (for example `!tests/fixtures/*.kicad_sch`)
  **after** the ignore lines.
- After committing, confirm the file is actually tracked with
  `jj file list -r <branch> tests/fixtures` (or `git ls-files`). `No matching entries`
  means it hit the trap — do not trust a passing pytest run alone.

## Editing the synthetic .kicad_pcb by text patch

`tests/fixtures/synthetic.kicad_pcb` uses **one-tab** indentation for top-level KiCad
S-expressions, not two spaces. Anchor patches on one-tab forms such as `\t(net ...)`,
`\t(segment ...)`, and `\t(via ...)`.

## Validating that KiCad can still load a fixture

KiCad 9's `kicad-cli` has no `pcb check`; use `pcb drc` as the load and syntax check:

    /Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli pcb drc \
      tests/fixtures/synthetic.kicad_pcb --output "$TMPDIR/drc.json" --format json

DRC *violations* are acceptable for a synthetic fixture — producing the report at all
confirms KiCad parsed the board. Omit `--exit-code-violations` when the fixture
intentionally carries violations; without that flag `pcb drc` exits 0 even with
violations, so a nonzero exit genuinely means the run failed rather than that the board
has problems.
