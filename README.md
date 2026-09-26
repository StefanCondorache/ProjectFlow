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
  with `python -m`, and `if __name__ == "__main__"` scripts, grouped with the
  deployed ones first. The same search box finds any function by name.
- **Flow**: START on the left, END on the right (⇄ turns it top-down), the steps
  in between in the order they run. A step is a call into one of the project's own functions; `+` opens
  it in place, inside a frame named after its function and file. Diamonds are
  decisions (calls that only run sometimes, as in `a() if c else b()` or
  `a() or b()`, get theirs too), dashed frames are loops, red paths are errors,
  blue parallelograms are data entering or leaving the program (files,
  databases, network, env vars). Edge labels are the data each step hands on.
- **Following data**: click any data label (or a data chip in the details panel)
  to follow that value through the code.
  - *Journey*: every function it passes through, grouped by file. ↩ marks a
    function that hands it back to its caller.
  - *Steps*: the flow sliced to the data: only the steps that touch it, with every
    call it enters opened.
- **Simulation**: ▶ Simulate sends data through the diagram. First it shows
  what the run can be given: the variables where it starts (with their types,
  what the code already knows of them, and an *example* of the JSON each one
  takes, built from the annotations, so an `Order` parameter gets
  `{"name": "", "qty": 0, …}`), the command line (parsed by the program's own
  argparse set-up) and the environment variables the code reads. Then an arrow
  carries the data along the flow while the side panel shows the variables of
  the function it is in, marked *new* or *changed* as the code adds keys,
  fields and items.
  - Nothing of the project is run, and nothing is written. What can be
    computed from the code and the data given is computed, as Python would
    (arithmetic, builtins, pure library functions such as `json.loads` or
    `math`, what goes into dicts, lists and objects), and files inside the
    project are really read (`open`, `json.load`, YAML, TOML, CSV,
    `Path.read_text`; hidden files such as `.env` never). Writes, prints and
    log lines are only recorded, and listed as the run's outputs. What only a
    real run would know is shown as where it comes from, e.g.
    `‹json.load(fh)›`; any step's result can be given a value by hand.
  - Loops run every round (the first 25 are shown); `break`, `continue` and
    loop `else` work. Exceptions travel like in Python, through `try` /
    `except` / `finally`, matched by class (the project's own included), and
    real errors on real values are raised where they happen, in red, with the
    file and line.
  - When a decision depends on data nobody gave, the run stops and asks which
    way to go (a `while` loop asks before each round).
  - Closed steps still run, silently, so their results and what they do to
    the objects they get are not lost. ⤵ *Go inside* opens the step the data
    is at and sends it through; *Go inside every step* does that everywhere.
    Opening or closing a box during a run walks it again.
  - ▶ *Simulate from here* (in any box's details) starts the run at that
    point: everything before it runs unseen, taking the branches that lead
    there, and the form shows the data that exists at that point.
  - Space pauses, ← → step through it.
- **Details**: click any box for its docstring, where it is defined and called,
  the data in and out, how the call was resolved, and the source.

Both side panels can be dragged wider or narrower (double-click the edge to reset).

Other outputs:

```
flowmap <folder> --list                          # entry points, ranked
flowmap <folder> --mermaid [ENTRY] [--depth N]   # Mermaid text for an entry (--vertical: top-down)
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
out rather than invented. Values are the simulation's part (above); tracing a
real run would be a later, optional layer.

## Limitations

**Diagrams**

- Python only, so far.
- Calls are resolved without running anything: calls through variables,
  callbacks, `getattr`, registries or dependency injection cannot be followed,
  and are left out rather than guessed.
- Entry points come from Dockerfiles, compose files, Procfiles, shell scripts,
  `pyproject` scripts, `__main__` packages and main guards. Web routes, task
  queues, cron jobs, Makefiles and CI files are not read; their functions can
  still be opened from the search box.
- Only what explains the flow is drawn, so plain computations are hidden, and so
  are `break` and `continue`: a loop that can stop early looks like it runs to
  the end (the simulation does follow them).
- A function already open higher up is not opened again inside itself
  (recursion is marked ↻).
- Following data is a *may flow* analysis: a trail can include functions the
  value only might reach, and very long trails are cut short (12 calls deep,
  300 call sites).

**Simulation**

- The code is interpreted, never run, so only part of Python is modelled:
  - objects from libraries (pandas frames, numpy arrays, connections,
    responses) stay unknown; only pure standard-library functions are computed
    (`json`, `math`, `statistics`, `os.path`, `datetime`, `decimal`,
    `fractions`, `collections`, `copy`, `textwrap`, `base64`, `re`, `pathlib`
    paths);
  - on project classes, properties, class attributes read through an object,
    operators (`__add__`, `__eq__`, `__getitem__`…), `__post_init__` and
    `__enter__`/`__exit__` are not run;
  - nested functions do not see their enclosing function's variables, and
    functions passed around as values (callbacks, lambdas) are not called;
  - module globals come from the module's top-level code, once: a `global`
    assignment in a function does not reach other functions; `del` is ignored;
  - a comprehension that calls functions gives an unknown value (the calls
    themselves still run);
  - generators run to the end at once and hand on the list of what they yield;
  - `async` functions run like plain ones (`await` is a call); event loops
    (`asyncio.run`), threads and processes are not followed;
  - `match` decides literal patterns and `_` only;
  - argparse is the only command-line parser run for real, without
    subcommands, custom `type=` functions or custom actions (those values stay
    strings); click, typer and the like stay unknown.
- The outside world never fails: library calls give unknown values, never
  network or disk errors. Errors come from the code's own `raise`, from real
  values (a missing key, a division by zero), from a malformed project file,
  from argparse and from `sys.exit`.
- Files are read only inside the project folder, up to 2 MB, never hidden ones,
  and only as text (JSON, YAML, TOML, CSV, plain text); pickle, parquet, numpy
  and Excel files and databases are not read. A relative path is looked up from
  the project root and from the module's folder: the real working directory is
  not known.
- Time, randomness, network, databases and subprocesses are always unknown;
  environment variables are only what the run is given.
- Data nobody gave is approximated:
  - a decision it cannot settle asks, and the answer holds for every round of
    a `for` loop (a `while` loop, and what is inside it, asks every round);
  - steps that run silently do not ask: they follow both branches and keep
    what the branches agree on, and a `return` or `raise` in just one of them
    does not end the step for certain;
  - a loop over a collection nobody gave runs once, for "an item of ‹…›", and
    what it changed is unknown afterwards;
  - a change at an unknown key or index (`out[kind].append(x)`) makes the
    whole container unknown.
- Starting from a box forces the way there: the branches that lead to it are
  taken whatever the data says, and starting in an `except` block pretends its
  error happened.
- Limits per run: 4,000 frames and 20 seconds; 200,000 rounds per loop; silent
  steps 12 calls deep and 50,000 statements each (past that, their result is
  unknown); *Go inside every step* opens at most 60 steps, 6 levels deep;
  containers are shown up to 50 items.

## Development

```
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest                 # analyzer tests

cd viewer && npm install
npm test                                   # viewer unit tests
npm run build                              # builds into src/flowmap/web/
```
