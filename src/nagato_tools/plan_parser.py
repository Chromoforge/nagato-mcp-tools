"""
Strict Markdown -> FSM-frame parser for the `## Steps` section of plan files.

This module implements a fail-closed parser that:
- Only parses the `## Steps` section
- Requires exact grammar for Phase headers and Step lines
- Raises PlanParseError with exact line number and text on any mismatch
- Returns structured data for seeding subgoal frames

Grammar:
```
## Steps

### Phase <N> - <title>[ (depends on: <comma-separated phase numbers>)]
<N>. <step summary text> - done_when: <verification criterion text>
   <optional indented continuation lines - NOT parsed as separate steps>
<N+1>. <step summary text> - done_when: <verification criterion text>

### Phase <N+1> - <title>
...
```
"""

import re
from dataclasses import dataclass
from typing import List, Dict, Any, Optional


class PlanParseError(Exception):
    """Raised when the plan markdown fails to parse according to the strict grammar."""
    
    def __init__(self, line_no: int, line_text: str, reason: str):
        self.line_no = line_no
        self.line_text = line_text
        self.reason = reason
        super().__init__(f"Plan parse error at line {line_no}: {reason}\n  Line: {line_text!r}")


@dataclass
class ParsedStep:
    step_num: int
    summary: str
    done_when: str


@dataclass
class ParsedPhase:
    phase_num: int
    title: str
    depends_on: List[int]
    steps: List[ParsedStep]


def parse_plan_steps(markdown_text: str) -> List[ParsedPhase]:
    """
    Parse the `## Steps` section of a plan markdown file.
    
    Args:
        markdown_text: Full content of the plan .md file
        
    Returns:
        List of ParsedPhase objects with nested ParsedStep objects
        
    Raises:
        PlanParseError: If any line in the ## Steps section doesn't match the grammar,
                        or if zero steps are found
    """
    lines = markdown_text.splitlines()
    
    # Find the ## Steps section
    steps_start = -1
    for i, line in enumerate(lines):
        if line.strip() == "## Steps":
            steps_start = i
            break
    
    if steps_start == -1:
        raise PlanParseError(0, "", "No '## Steps' section found in plan file")
    
    # Parse from the line after ## Steps
    phases: List[ParsedPhase] = []
    current_phase: Optional[ParsedPhase] = None
    step_count = 0
    
    # Regex patterns
    phase_pattern = re.compile(r'^### Phase (\d+) - (.+?)(?:\s*\(depends on:\s*([\d,\s]+)\))?\s*$')
    step_pattern = re.compile(r'^(\d+)\.\s+(.+?)\s+-\s+done_when:\s*(.+?)\s*$')
    
    for line_no in range(steps_start + 1, len(lines)):
        line = lines[line_no]
        stripped = line.strip()
        
        # Stop parsing at the next top-level section (## ...)
        if stripped.startswith("## "):
            break
        
        # Skip blank lines
        if not stripped:
            continue
        
        # Check for phase header
        phase_match = phase_pattern.match(stripped)
        if phase_match:
            # Save previous phase if exists
            if current_phase is not None:
                phases.append(current_phase)
            
            phase_num = int(phase_match.group(1))
            title = phase_match.group(2).strip()
            depends_on_str = phase_match.group(3)
            
            depends_on = []
            if depends_on_str:
                depends_on = [int(x.strip()) for x in depends_on_str.split(',') if x.strip()]
            
            current_phase = ParsedPhase(
                phase_num=phase_num,
                title=title,
                depends_on=depends_on,
                steps=[]
            )
            continue
        
        # Check for step line
        step_match = step_pattern.match(stripped)
        if step_match:
            if current_phase is None:
                raise PlanParseError(
                    line_no + 1, line,
                    "Step found before any Phase header - steps must be nested under a Phase"
                )
            
            step_num = int(step_match.group(1))
            summary = step_match.group(2).strip()
            done_when = step_match.group(3).strip()
            
            current_phase.steps.append(ParsedStep(
                step_num=step_num,
                summary=summary,
                done_when=done_when
            ))
            step_count += 1
            continue
        
        # Check if it's an indented continuation line (starts with whitespace)
        if line.startswith(' ') or line.startswith('\t'):
            # Continuation line - ignore (not parsed as separate step)
            continue
        
        # Non-blank, non-indented line that matches neither pattern = ERROR
        raise PlanParseError(
            line_no + 1, line,
            f"Line does not match Phase header or Step grammar. "
            f"Expected '### Phase N - Title' or 'N. Summary - done_when: Criterion'"
        )
    
    # Don't forget the last phase
    if current_phase is not None:
        phases.append(current_phase)
    
    if step_count == 0:
        raise PlanParseError(0, "", "Plan has no parseable steps in ## Steps section")
    
    return phases


def phases_to_dicts(phases: List[ParsedPhase]) -> List[Dict[str, Any]]:
    """Convert ParsedPhase objects to plain dicts for JSON serialization."""
    result = []
    for phase in phases:
        result.append({
            "phase_num": phase.phase_num,
            "phase_title": phase.title,
            "depends_on": phase.depends_on,
            "steps": [
                {
                    "step_num": step.step_num,
                    "summary": step.summary,
                    "done_when": step.done_when
                }
                for step in phase.steps
            ]
        })
    return result