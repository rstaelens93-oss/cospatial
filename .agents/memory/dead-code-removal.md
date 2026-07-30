---
name: Dead code removal pattern
description: How to surgically excise large multi-function blocks from Python files without breaking things.
---

**Problem:** Removing 400-600 lines spanning multiple functions using chained Edit calls is error-prone — partial edits leave the file in an unrunnable state, and long `old_string` values are fragile.

**Pattern that works:** Use a ShellExec Python splice script:

```python
with open("backend/main.py", "r") as f:
    content = f.read()

START = "def _first_dead_function"       # unique string at block start
END   = "unique text just after block end"  # unique anchor AFTER the block

si = content.find(START)
ei = content.find(END)
# ei_end = ei + len(END) to keep the anchor, or just ei to drop it

content = content[:si] + REPLACEMENT + content[ei_end:]

with open("backend/main.py", "w") as f:
    f.write(content)
```

**Why:** Single atomic write; no risk of partial state; anchors are easy to verify before writing; the replacement text can be any length without Edit tool size limits.

**How to apply:** Use for any excision > ~5 functions or > ~100 lines. Always print a sanity check (grep for dead symbol names) after the write.
