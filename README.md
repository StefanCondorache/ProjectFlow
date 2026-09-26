# flowmap

Point it at a project folder; get Mermaid-style flow diagrams of what the code
does, from START to END, through files and functions. Nothing to configure and
no AI: the same code always gives the same diagram.

```
flowmap ~/Projects/some-project
```

opens a local viewer in the browser (it only reads the folder, never writes to it).

## What you see

- **Entry points**: the program's starts, found on their own: Docker/compose
  commands (following shell scripts they run), `pyproject` scripts, packages run
  with `python -m`, and `if __name__ == "__main__"` scripts. Deployed ones come first.
- **Flow**: START at the top, END at the bottom, the steps in between in the order
  they run. A step is a call into one of the project's own functions; `+` opens
  it in place, inside a frame named after its function and file. Diamonds are
  decisions, dashed frames are loops, red paths are errors, blue parallelograms
  are data entering or leaving the program (files, databases, network, env vars).
  Edge labels are the data each step hands on.
- **Following data**: click any data label (or a data chip in the details panel)
  to follow that value through the code.
  - *Journey*: every function it passes through, grouped by file. ↩ marks a
    function that hands it back to its caller.
  - *Steps*: the flow sliced to the data: only the steps that touch it, with every
    call it enters opened.
- **Details**: click any box for its docstring, where it is defined and called,
  the data in and out, how the call was resolved, and the source.

Other outputs:

```
flowmap <folder> --list                          # entry points, ranked
flowmap <folder> --mermaid [ENTRY] [--depth N]   # Mermaid text for an entry
```

`ENTRY` is a number from `--list`, part of an entry's label, or a function id
such as `src/shop/orders.py::OrderService.place`.

## Languages

Python today. Each language is an adapter (`src/flowmap/lang/`) that turns
source into a shared model; entry detection from deployment files, diagrams,
data trails and the viewer are shared.

## How calls are resolved

Statically: names, imports (absolute, relative, re-exports, script folders),
`self`/`super()`, classes, and types from annotations, constructor calls and
return values. Each resolved call is marked *exact*, *inferred* or *guess*
(guesses are drawn with a dashed border); calls that cannot be resolved are left
out rather than invented. Values themselves need the program to run; that is
the planned runtime layer.

## Development

```
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest                 # analyzer tests

cd viewer && npm install
npm test                                   # viewer unit tests
npm run build                              # builds into src/flowmap/web/
```
