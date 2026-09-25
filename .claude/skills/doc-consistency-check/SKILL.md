---
name: doc-consistency-check
description: Verify the project's docs, rules, and build specs still agree with each other and with reality after any edit to CLAUDE.md, .claude/rules/*.md, or docs/*.md. Checks cross-file identifier agreement (tool names, state fields, chart specs, secrets), within-file duplicate-definition drift, dangling references (step numbers, test globs, doc links), and bash/python syntax in code blocks. Use this whenever you finish editing one of these spec files, before considering the edit done — not just when explicitly asked to "check consistency" or "validate docs." A doc-spec project like this one treats an internally-inconsistent spec as a build blocker, the same severity as a syntax error.
---

# Doc consistency check

This project's `.claude/rules/*.md` and `docs/*.md` files ARE the build spec.
An inconsistency between them — two files disagreeing on a field name, a
tool list drifting apart, a step reference pointing at the wrong step — is
not a typo. It's a spec defect that produces a wrong implementation later,
silently, because nothing else catches it.

This skill runs the same validation battery developed and refined across an
extensive doc-authoring session for this project. Run it after any edit to
`CLAUDE.md`, `.claude/rules/*.md`, or `docs/*.md` — not just when asked.

## When something fails

Don't just report it — read both sides, determine which one is correct (or
whether both are stale), and fix the wrong one. Re-run the check after
fixing. A failing check is the START of a fix, not a report to hand back.

## The checks

Run this as one script against the repo root. Adjust file lists if the
project's structure has changed.

```python
import glob, re, os, ast, textwrap, subprocess, fnmatch

files = [p for p in ['CLAUDE.md', 'conversational-analytics-platform-COMPONENT-REFERENCE.md',
                      'local-dev-environment-setup.md']
          + sorted(glob.glob('docs/*.md')) + sorted(glob.glob('.claude/rules/*.md'))
          if os.path.exists(p)]
files = [p.replace(os.sep, '/') for p in files]  # glob() returns native separators on
                                                   # Windows (backslash); every corpus.get()
                                                   # lookup below uses a forward-slash literal,
                                                   # so an unnormalized key silently misses
corpus = {p: open(p, encoding='utf-8').read() for p in files}
allok = True

# --- 1. STRUCTURAL: every file individually well-formed -------------------
comp = corpus.get('conversational-analytics-platform-COMPONENT-REFERENCE.md', '')
valid_sections = set(re.findall(r'^## (\d+[a-z]?)\.', comp, re.M)) | \
                 set(re.findall(r'^### (\d+\.\d+[a-z]?)', comp, re.M))
setup = corpus.get('local-dev-environment-setup.md', '')
setup_steps = {int(m.group(1)) for m in re.finditer(r'^## Step (\d+)', setup, re.M)}

for p, txt in corpus.items():
    lines = txt.split('\n')
    if sum(1 for l in lines if l.strip().startswith('```')) % 2:
        allok = False; print(f"FENCE (unclosed code block): {p}")

    # blank-line runs OUTSIDE code fences (PEP8 double-blanks inside are fine)
    infence, run = False, 0
    for i, l in enumerate(lines, 1):
        if l.strip().startswith('```'):
            infence = not infence; run = 0; continue
        if infence: continue
        run = run + 1 if l.strip() == '' else 0
        if run >= 2:
            allok = False; print(f"BLANKS {p}:{i}")

    if f'`{p}`' in txt:
        allok = False; print(f"SELF-REF: {p} cites its own filename")

    # bold-marker balance (strip frontmatter globs, code, inline code first —
    # all three contain ** or ` sequences that aren't markdown emphasis)
    b = re.sub(r'^---\n.*?\n---\n', '', txt, flags=re.S)
    b = re.sub(r'```.*?```', '', b, flags=re.S)
    b = re.sub(r'`[^`]*`', '', b)
    if b.count('**') % 2:
        allok = False; print(f"UNBALANCED BOLD: {p}")

    for r in set(re.findall(r'§(\d+[a-z]?(?:\.\d+[a-z]?)?)', txt)):
        if r not in valid_sections:
            allok = False; print(f"BAD § REF: {p} -> §{r}")

    for r in set(re.findall(r'`((?:docs/|\.claude/rules/)[\w-]+\.md)`', txt)):
        if not os.path.exists(r):
            allok = False; print(f"MISSING FILE REF: {p} -> {r}")

    for m in re.finditer(r'setup guide[^\n]{0,14}Step (\d+)', txt):
        if int(m.group(1)) not in setup_steps:
            allok = False; print(f"BAD STEP REF: {p} -> Step {m.group(1)}")

    # markdown table column-count consistency
    in_table = False
    for i, l in enumerate(lines, 1):
        if l.strip().startswith('|'):
            if not in_table:
                in_table, base = True, l.count('|')
            elif abs(l.count('|') - base) > 1:
                allok = False; print(f"TABLE COLUMN MISMATCH {p}:{i}")
        else:
            in_table = False

    for block in re.findall(r'```python\n(.*?)```', txt, re.S):
        code = textwrap.dedent(block)
        if '...' in code or code.strip().startswith(('@', '#')):
            continue  # fragment/decorator-only snippet, not meant to parse alone
        try:
            ast.parse(code)
        except SyntaxError as e:
            allok = False; print(f"PYTHON SYNTAX: {p} — {e.msg}")

for b in re.findall(r'```bash\n(.*?)```', setup, re.S):
    # placeholder substitution so real values don't break the syntax check
    probe = re.sub(r'YOUR_[A-Z_]+', 'placeholder', b)
    r = subprocess.run(['bash', '-n'], input=probe, capture_output=True, text=True)
    if r.returncode:
        allok = False; print(f"BASH SYNTAX: {r.stderr.strip()[:100]}")

# --- 2. WITHIN-FILE DUPLICATE-DEFINITION DRIFT -----------------------------
# The bug class this skill exists to catch: the SAME named construct
# (a dict, a class, a join([...]) assembly, a shell loop) defined twice in
# one file, where the two copies have silently diverged. This is how two
# `static_text` assemblies in one rule file ended up listing different
# components — nobody edits "the second one" when fixing "the first one."
DEFS = [
    (r'(\w+)\s*=\s*\{\n((?:\s*"[\w-]+":[^\n]*\n)+)\s*\}', 'dict'),
    (r'(\w+)\s*=\s*\[\n((?:\s*"[\w-]+",?[^\n]*\n)+)\s*\]', 'list'),
    (r'"\\n\\n"\.join\(\[()((?:[^\]]*\n)+?)\s*\]\)', 'join-assembly'),
    (r'class (\w+)\((?:TypedDict|BaseModel)\):\n((?:    .*\n|\n)+)', 'class'),
    (r'for S in ()((?:[a-z][\w-]+\s*\\?\s*)+); do', 'shell-loop'),
]
for p, txt in corpus.items():
    by_name = {}
    for pat, kind in DEFS:
        for m in re.finditer(pat, txt):
            key = (kind, m.group(1) or kind)
            # CONSTANT_NAMES only — not every 4+ char word. A comment on one
            # copy ("# from model_schema.json, at startup") but not the
            # other dilutes the overlap ratio below threshold with the naive
            # extraction, producing a false negative on exactly the bug this
            # check exists to catch. Verified against the real static_text
            # duplication bug before shipping this skill.
            members = frozenset(re.findall(r'\b[A-Z][A-Z0-9_]{3,}\b', m.group(2)))
            by_name.setdefault(key, []).append(members)
    for (kind, name), occurrences in by_name.items():
        if len(occurrences) > 1 and len(set(occurrences)) > 1:
            a, b = occurrences[0], occurrences[1]
            overlap = len(a & b) / max(len(a | b), 1)
            if overlap > 0.3:  # same construct, different membership = drift
                allok = False
                print(f"WITHIN-FILE DRIFT: {p} [{kind} '{name}'] two definitions disagree")
                print(f"   only in one: {sorted(a - b)[:6]}")
                print(f"   only in other: {sorted(b - a)[:6]}")

# --- 3. CROSS-FILE IDENTIFIER AGREEMENT ------------------------------------
# Adjust these extraction patterns to match your project's actual constants.
# The PATTERN below (extract the same logical set from N independent
# locations, assert they're equal) generalizes to any project.
def extract_dict_keys(txt, var_name):
    m = re.search(rf'{var_name} = \{{(.*?)\}}', txt, re.S)
    return set(re.findall(r'"(\w+)":', m.group(1))) if m else None

def extract_class_fields(txt, class_name):
    m = re.search(rf'class {class_name}\((?:TypedDict|BaseModel)\):\n((?:    .*\n|\n)+)', txt)
    return set(re.findall(r'^    (\w+):', m.group(1), re.M)) if m else None

# Example wiring for THIS project — replace with your own tool/state names:
g = corpus.get('.claude/rules/gateway.md', '')
o = corpus.get('.claude/rules/orchestrator.md', '')
sets_to_compare = {
    'tool names (3 sources)': [
        extract_dict_keys(g, 'FRIENDLY_TOOL_NAMES'),
        extract_dict_keys(o, 'SOURCE_LABELS'),
    ],
}
for label, found in sets_to_compare.items():
    found = [f for f in found if f is not None]
    if len(found) > 1 and len(set(map(frozenset, found))) > 1:
        allok = False; print(f"CROSS-FILE MISMATCH: {label}")
        for i, s in enumerate(found): print(f"   source {i}: {sorted(s)}")

# --- 4. TEST-GLOB COVERAGE --------------------------------------------------
tst = corpus.get('docs/testing.md', '')
test_files = {f"tests/{x}" for x in re.findall(r'tests/(test_\w+\.py)', tst)}
patterns = []
for p in sorted(glob.glob('.claude/rules/*.md')):
    fm = re.match(r'^---\n(.*?)\n---\n', open(p, encoding='utf-8').read(), re.S)
    if fm:
        patterns += re.findall(r"- '([^']+)'", fm.group(1))
uncovered = [x for x in test_files if not any(fnmatch.fnmatch(x, pt) for pt in patterns)]
dangling = [pt for pt in patterns if pt.startswith('tests/') and pt not in test_files]
if uncovered: allok = False; print(f"TEST FILE WITH NO RULE GLOB: {uncovered}")
if dangling: allok = False; print(f"RULE GLOB POINTS AT NONEXISTENT TEST FILE: {dangling}")

print()
print("FINAL:", "ALL PASS" if allok else "ISSUES ABOVE — fix, then re-run this script")
```

## What to do with a clean run

Nothing — don't announce success verbosely. If the user asked you to make an
edit and this check passes, that's just confirmation the edit didn't break
anything; fold it into your normal summary of the change, don't narrate the
validation mechanics.

## Extending this for new field families

Section 3 (cross-file identifier agreement) is the part most likely to need
new entries as the project grows — e.g. when a new shared constant appears
in more than one file. Add it to `sets_to_compare` following the existing
pattern: extract the same logical set from each location that defines or
restates it, then compare.

Section 2 (within-file drift) needs no project-specific tuning — it operates
on syntax shapes (dict/list/class/loop), not on this project's specific
names, so it generalizes automatically to new files and new constructs of
the same shapes.
