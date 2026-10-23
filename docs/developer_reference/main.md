# Architecture

SGLang-Omni is the multi-stage runtime for omni models: models that accept
mixed text, image, audio, and video inputs and may emit text, audio, or other
modalities.

## System Overview

```text
HTTP API -> Client -> Coordinator -> Stage -> Scheduler -> ModelRunner -> model forward
```


| Layer                             | Duty                                                                                   |
| ----------------------------------- | ---------------------------------------------------------------------------------------- |
| [HTTP API](./apiserver_design.md) | OpenAI-compatible request and response schemas, SSE framing, HTTP errors               |
| [Client](./apiserver_design.md)   | `GenerateRequest` to `OmniRequest`, result aggregation, audio encoding                 |
| [Coordinator](./pipeline.md)      | Request lifecycle, entry-stage submission, terminal result collection, abort broadcast |
| [Stage](./pipeline.md)            | Control-plane IO, relay IO, fan-in, stream routing, scheduler inbox/outbox bridging    |
| [Scheduler](./pipeline.md)        | Per-stage execution loop and failure propagation to stage outbox                       |
| [ModelRunner](./pipeline.md)      | AR forward preparation, model forward dispatch, output extraction                      |
| [Communication](./communication.md) | Control-plane messages and relay data transfer between stages                         |
| [TTS Integration](./tts_model_integration.md) | Checklist and lifecycle rules for adding TTS model families                         |

Refer to the layer-specific document for specific design details.

## Directory Layout

```text
sglang_omni/
|-- pipeline/       # Inter-stage orchestration, stages, coordinator, processes
|-- scheduling/     # Scheduler loops and inbox/outbox message types
|-- model_runner/   # Shared model runner abstractions for AR stages
|-- models/         # Model-specific configs, stages, request builders, modules
|-- config/         # PipelineConfig, StageConfig, config manager, topology
|-- relay/          # Data transfer backends
|-- serve/          # HTTP server and OpenAI-compatible API adapter
|-- client/         # Internal client used by API adapters
`-- proto/          # Request, payload, stage, and control-plane message types
```

## Model Directory Convention

Model-specific code should stay under `sglang_omni/models/<model>/`.

Recommended layout:

```text
models/<model>/
|-- config.py             # PipelineConfig subclass and StageConfig list
|-- stages.py             # stage factories
|-- routing.py            # optional data-driven routing helpers
|-- request_builders.py   # inter-stage payload transforms
|-- payload_types.py      # typed model-specific payload state
|-- callbacks.py          # feedback callbacks or strategy, when needed
`-- components/           # model modules, processors, vocoders, adapters
```

Only model-local behavior belongs here. The framework-owned layers are still
`Stage`, `Coordinator`, schedulers, model-runner bases, relay, runtime prep, and
runners.

## Naming and lint checks

Use public names for classes, functions, methods, and attributes defined in
`sglang_omni/` and `tests/`. The leading-underscore lint hook checks every Python
file under those directories, including new model packages and test suites. It
flags class and function
definitions, every attribute read or write, and string names passed to
`getattr`. It ignores vendor copies, dunder names such as `__init__`, a lone
`_`, and definitions inside functions.

A name inherited from an upstream object still fails the check. Keep that
spelling, and mark the line:

```python
session_request._omni_prompt_cache_key = adapter_request._omni_prompt_cache_key  # noqa: leading-underscore
```

When a local definition must keep a name because an external interface
requires it, put `# noqa: leading-underscore` on that line and explain why:

```python
class ExternalAdapter(ExternalBase):
    # ExternalBase calls this hook by its exact name.
    def _required_external_hook(self):  # noqa: leading-underscore
        return self.state
```

The exemption keeps the definition out of lint violations and automatic renames.
Its references retain the same name. A comment such as
`# skip leading-underscore class and function names` is not an exemption.

The pre-commit hook runs `scripts/check_leading_underscore.py` and only reports
violations. It does not rewrite files. To rename violating class and function
definitions and their references within the same file, run the fixer and review
the diff:

```bash
python scripts/check_leading_underscore.py --fix
```

The fixer never renames attributes, and it does not update callers in other
files, pytest fixture parameters, or string references such as
`monkeypatch.setattr(module, "_name", ...)`. Rename those by hand, preserve
third-party API names, and update those references explicitly.

Every `if` under `sglang_omni/` must have an `else`, or belong to an `if`/`elif`
chain that ends in `else`. Returning, raising, or a one-line body does not
exempt it. There is no `# noqa` exemption. Prefer an `else` branch that does
real work. At least write `else: pass`.

The pre-commit hook runs `scripts/check_if_else.py` and only reports
violations. It does not rewrite files. When the other branch truly does
nothing, insert `else: pass` yourself, or run the fixer:

```bash
python scripts/check_if_else.py --fix
```

Run `pre-commit run --all-files` before submitting a change.

## Documentation checks

The `doc-style` pre-commit hook checks every Markdown file under `docs/` for
emoji. It uses an isolated Python environment with pinned lint dependencies;
the inference package does not need these dependencies.

To run the same check and tests manually from the repository root with Python
3.11 or 3.12:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r scripts/requirements-doc-lint.txt pytest
python scripts/check_doc_style.py
python -m pytest tests/unit_test/scripts/test_check_doc_style.py -q
pre-commit run doc-style --all-files
```

Install pre-commit separately with `python -m pip install pre-commit` if needed.
First installation downloads packages; subsequent checker and test runs work
offline. The checker reports `path:line: no-emoji: detail` on standard error
and exits with 1 for violations or 0 when clean. It never rewrites documents.

The checker uses `emoji==2.15.0` (BSD 3-Clause, Python 3.8 or later), whose bundled
data covers Unicode Emoji 17.0. Two narrowly defined standard-library patterns
cover forms the library omits: Regional Indicator code points (U+1F1E6–U+1F1FF), and
ASCII digits, `#`, or `*` immediately followed by VS16 (U+FE0F). Already matched
flags and keycaps are excluded from the supplement to avoid duplicate reports.
The boundaries follow the Unicode 17.0 [character properties](https://www.unicode.org/Public/17.0.0/ucd/emoji/emoji-data.txt)
and [variation sequences](https://www.unicode.org/Public/17.0.0/ucd/emoji/emoji-variation-sequences.txt).

This is library-based matching plus those two supplements, rather than a full
Unicode emoji grammar parser. Revalidate whether the supplements are still
necessary when upgrading the library. There are no custom Unicode tables,
generators, update scripts, or network requests during checking.

The checker rejects default emoji characters and legal
emoji combinations, including flags, keycaps, skin tones, and ZWJ sequences.
Plain text symbols and valid VS15 text presentation are allowed; emoji
presentation using VS16 is rejected. Each complete combination is reported
once. Fenced code is exempt; inline code and URLs are checked.
