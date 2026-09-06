# Pre-Parsing & Input Repair Layer

The Nagato Tools suite provides an automated pre-parsing and argument-repair pipeline (`input_repair.py`) designed to handle common LLM payload anomalies seamlessly before passing arguments to tool execution functions.

---

## 1. What It Can Do

When language models interact with tools via JSON-RPC or standard function-calling protocols, they frequently produce malformed payloads:
- Wrapping JSON payloads in Markdown code fences (` ```json ... ``` `) or raw XML tags (`<tool_call>...</tool_call>`).
- Sending invalid JSON containing trailing commas, single-quoted keys/strings, or unescaped characters.
- Packing all function arguments as a single serialized JSON string under one parameter.
- Passing numbers or booleans as raw strings (e.g., `start_line: "5"`, `is_regexp: "true"`).
- Providing slightly misspelled or imprecise file paths.

The pre-parsing layer intercepts incoming arguments inside `facade._preprocess_arguments()` and repairs them automatically **before** the target tool function receives them.

---

## 2. Architecture & Processing Flow

The repair pipeline executes sequentially before tool execution:

```
LLM Raw Payload
      │
      ▼
┌─────────────────────────────────────────────────────────────┐
│ facade._preprocess_arguments(func, kwargs)                  │
│                                                             │
│ 1. strip_noise_from_payload(str_value)                      │
│    - Strips Markdown fences (```json ... ```)               │
│    - Strips <tool_call>...</tool_call> tags                 │
│                                                             │
│ 2. Unpack Single-Arg JSON (repair_malformed_json)           │
│    - If len(kwargs) == 1 and value is a JSON-like string:   │
│      json_repair -> yaml.safe_load -> ast.literal_eval      │
│      Unpacks dictionary to top-level keyword arguments      │
│                                                             │
│ 3. resolve_file_path(path_str, workspace_root)              │
│    - Fuzzy resolves non-existent paths via Levenshtein      │
│                                                             │
│ 4. coerce_primitives(kwargs, sig)                           │
│    - "5" -> 5 for int parameters                            │
│    - "true"/"yes"/"1" -> True for bool parameters           │
└─────────────────────────────────────────────────────────────┘
      │
      ▼
Target Tool Execution (e.g. nagato_read_file, nagato_edit)
```

---

## 3. Core Repair Functions

### `strip_noise_from_payload(raw: str) -> str`
Removes common wrapping artifacts produced by LLMs:
- Strips surrounding Markdown code fences (` ```json\n...\n``` ` or ` ```...``` `).
- Strips enclosing `<tool_call>...</tool_call>` tags.
- Trims outer whitespace.

### `repair_malformed_json(raw: str) -> dict | None`
Repairs invalid JSON strings into valid Python dictionaries using a resilient fallback chain:
1. `json_repair.loads()`: Fixes missing quotes, trailing commas, single quotes, unescaped newlines.
2. `yaml.safe_load()`: Parses JSON-compatible YAML structures if the standard JSON parser fails.
3. `ast.literal_eval()`: Evaluates Python dictionary literals.
- Returns a `dict` on success, or `None` if parsing fails (fails safely without raising unhandled exceptions).

### `coerce_primitives(kwargs: dict, sig: inspect.Signature) -> dict`
Inspects the target tool function's type annotations and coerces string values into expected primitive types:
- **`int` / `Optional[int]`**: Converts numeric strings (e.g., `"10"`) via `int(v)`.
- **`bool` / `Optional[bool]`**: Maps `"true"`, `"yes"`, `"1"` $\to$ `True`, and `"false"`, `"no"`, `"0"` $\to$ `False`.
- Does not mutate the input dictionary (returns a clean new dictionary).
- If coercion fails, retains the original value safely.

### `resolve_file_path(path_str: str, workspace_root: Path) -> str`
- If the given file path already exists on disk, returns it as-is.
- If it does not exist, computes the Levenshtein distance against known workspace files and fuzzy-corrects minor path typos.

---

## 4. Integration with `facade.py`

In both synchronous (`call()`) and asynchronous (`acall()`) invocations, argument preprocessing is applied right before executing the underlying function:

```python
# Synchronous call site in facade.py
kwargs = self._preprocess_arguments(func, kwargs)
return func(*args, **kwargs)

# Asynchronous call site in facade.py
kwargs = self._preprocess_arguments(func, kwargs)
return await func(*args, **kwargs)
```
