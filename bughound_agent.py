import ast #rec by claude
import json
import os
import re
import secrets
from typing import Any, Dict, List, Optional, Tuple

from reliability.risk_assessor import assess_risk

# Directory where prompt templates live, relative to this file
PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")


def _load_prompt(filename: str) -> str:
    """Load a prompt template from the prompts/ folder."""
    path = os.path.join(PROMPTS_DIR, filename)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class BugHoundAgent:
    """
    BugHound runs a small agentic workflow:

    1) PLAN: decide what to look for
    2) ANALYZE: detect issues (heuristics or LLM)
    3) ACT: propose a fix (heuristics or LLM)
    4) TEST: run simple reliability checks
    5) REFLECT: decide whether to apply the fix automatically
    """

    MAX_FIX_RETRIES = 2
    MAX_CODE_CHARS = 20_000
    MAX_ISSUES = 20

    # Log-only: instruction-like text in the input. Code comments legitimately contain imperatives,
    # so this is a signal in the trace, never a reason to block.
    _SUSPICIOUS_INPUT = re.compile(
        r"(ignore|disregard|forget)\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier)\s+(instructions|rules|prompts?)"
        r"|return\s+(an\s+)?empty\s+(json\s+)?array"
        r"|system\s+prompt",
        re.IGNORECASE,
    )

    def __init__(self, client: Optional[Any] = None):
        # client should implement: complete(system_prompt: str, user_prompt: str) -> str
        self.client = client
        self.logs: List[Dict[str, str]] = []

    # ----------------------------
    # Public API
    # ----------------------------
    def run(self, code_snippet: str) -> Dict[str, Any]:
        self.logs = []
        self._log("PLAN", "Planning a quick scan + fix proposal workflow.")

        code_snippet = code_snippet.replace("\x00", "")
        if len(code_snippet) > self.MAX_CODE_CHARS:
            self._log("INPUT", f"Input is {len(code_snippet)} chars (limit {self.MAX_CODE_CHARS}). Skipping analysis and fix.")
            return {
                "issues": [],
                "fixed_code": "",
                "risk": assess_risk(original_code=code_snippet, fixed_code="", issues=[]),
                "logs": self.logs,
            }
        if self._SUSPICIOUS_INPUT.search(code_snippet):
            self._log("INPUT", "Input contains instruction-like text. It is passed to the model as data only; review results carefully.")

        issues = self.analyze(code_snippet)
        self._log("ANALYZE", f"Found {len(issues)} issue(s).")

        fixed_code = self.propose_fix(code_snippet, issues)
        if fixed_code.strip() == "":
            self._log("ACT", "No fix produced (refused, error, or empty output).")

        risk = assess_risk(original_code=code_snippet, fixed_code=fixed_code, issues=issues)
        self._log("TEST", f"Risk assessed as {risk.get('level', 'unknown')} (score={risk.get('score', '-')}).")

        if risk.get("should_autofix"):
            self._log("REFLECT", "Fix appears safe enough to auto-apply under current policy.")
        else:
            self._log("REFLECT", "Fix is not safe enough to auto-apply. Human review recommended.")

        return {
            "issues": issues,
            "fixed_code": fixed_code,
            "risk": risk,
            "logs": self.logs,
        }

    # ----------------------------
    # Workflow steps
    # ----------------------------
    def analyze(self, code_snippet: str) -> List[Dict[str, str]]:
        if not self._can_call_llm():
            self._log("ANALYZE", "Using heuristic analyzer (offline mode).")
            return self._heuristic_analyze(code_snippet)

        self._log("ANALYZE", "Using LLM analyzer.")

        # Load prompt templates from prompts/ folder
        system_prompt = _load_prompt("analyzer_system.txt")
        user_template = _load_prompt("analyzer_user.txt")
        user_prompt = self._render(user_template, CODE=self._wrap_code(code_snippet))

        # UPDATED: Added exception handling for API errors/rate limits
        try:
            raw = self.client.complete(system_prompt=system_prompt, user_prompt=user_prompt)
        except Exception as e:
            self._log("ANALYZE", f"API Error: {str(e)}. Falling back to heuristics.")
            return self._heuristic_analyze(code_snippet)

        issues = self._parse_json_array_of_issues(raw)

        if issues is None:
            self._log("ANALYZE", "LLM output was not parseable JSON. Falling back to heuristics.")
            return self._heuristic_analyze(code_snippet)

        if not issues:
            # An empty array is the easiest result to get wrong (or to induce via injected text in the code),
            # so cross-check it against the deterministic rules before trusting it.
            cross_check = self._heuristic_analyze(code_snippet)
            if cross_check:
                self._log("ANALYZE", f"LLM reported no issues but the heuristic cross-check found {len(cross_check)}. Using those.")
                return cross_check

        return issues

    def propose_fix(self, code_snippet: str, issues: List[Dict[str, str]]) -> str:
        if not issues:
            self._log("ACT", "No issues, returning original code unchanged.")
            return code_snippet

        if not self._can_call_llm():
            self._log("ACT", "Using heuristic fixer (offline mode).")
            return self._heuristic_fix(code_snippet, issues)

        self._log("ACT", "Using LLM fixer.")

        # Load prompt templates from prompts/ folder
        system_prompt = _load_prompt("fixer_system.txt")
        user_template = _load_prompt("fixer_user.txt")
        base_prompt = self._render(user_template, ISSUES=json.dumps(issues), CODE=self._wrap_code(code_snippet))
        user_prompt = base_prompt

        # 1 initial attempt + up to MAX_FIX_RETRIES corrective retries
        for attempt in range(self.MAX_FIX_RETRIES + 1):
            # UPDATED: Added exception handling for API errors/rate limits
            try:
                raw = self.client.complete(system_prompt=system_prompt, user_prompt=user_prompt)
            except Exception as e:
                self._log("ACT", f"API Error: {str(e)}. Falling back to heuristic fixer.")
                return self._heuristic_fix(code_snippet, issues)

            cleaned = self._strip_code_fences(raw).strip()

            if not cleaned:
                self._log("ACT", "LLM returned empty output. Falling back to heuristic fixer.")
                return self._heuristic_fix(code_snippet, issues)

            try:
                ast.parse(cleaned)  # Raises SyntaxError for invalid Python, prose, or truncation
                return cleaned
            except SyntaxError as e:
                if attempt < self.MAX_FIX_RETRIES:
                    self._log("ACT", f"LLM returned invalid Python ({e}). Retrying with correction (retry {attempt + 1}/{self.MAX_FIX_RETRIES}).")
                    # Show the model its own bad output and the exact error so it can correct it
                    user_prompt = (
                        user_template.replace("{{ISSUES}}", json.dumps(issues)).replace("{{CODE}}", code_snippet)
                        + "\n\nYour previous response was NOT valid Python and failed to parse with this error:\n"
                        + f"{e}\n\nPrevious response:\n{cleaned}\n\n"
                        + "Return ONLY the complete, valid Python source. No prose, no markdown, no backticks."
                    )
                else:
                    self._log("ACT", f"LLM returned invalid Python ({e}) after {self.MAX_FIX_RETRIES} retries. Falling back to heuristic fixer.")

        return self._heuristic_fix(code_snippet, issues)

    # ----------------------------
    # Heuristic analyzer/fixer
    # ----------------------------
    def _heuristic_analyze(self, code: str) -> List[Dict[str, str]]:
        issues: List[Dict[str, str]] = []

        if "print(" in code:
            issues.append(
                {
                    "type": "Code Quality",
                    "severity": "Low",
                    "msg": "Found print statements. Consider using logging for non-toy code.",
                }
            )

        if re.search(r"\bexcept\s*:\s*(\n|#|$)", code):
            issues.append(
                {
                    "type": "Reliability",
                    "severity": "High",
                    "msg": "Found a bare `except:`. Catch a specific exception or use `except Exception as e:`.",
                }
            )

        if "TODO" in code:
            issues.append(
                {
                    "type": "Maintainability",
                    "severity": "Medium",
                    "msg": "Found TODO comments. Unfinished logic can hide bugs or missing cases.",
                }
            )

        return issues

    def _heuristic_fix(self, code: str, issues: List[Dict[str, str]]) -> str:
        fixed = code

        if any(i.get("type") == "Reliability" for i in issues):
            fixed = re.sub(r"\bexcept\s*:\s*", "except Exception as e:\n        # [BugHound] log or handle the error\n        ", fixed)

        if any(i.get("type") == "Code Quality" for i in issues):
            if "import logging" not in fixed:
                fixed = "import logging\n\n" + fixed
            fixed = fixed.replace("print(", "logging.info(")

        return fixed

    # ----------------------------
    # Parsing + utilities
    # ----------------------------
    def _parse_json_array_of_issues(self, text: str) -> Optional[List[Dict[str, str]]]:
        text = text.strip()
        parsed = self._try_json_loads(text)
        if not isinstance(parsed, list):
            array_str = self._extract_first_json_array(text)
            parsed = self._try_json_loads(array_str) if array_str else None
        if not isinstance(parsed, list):
            return None

        issues = self._normalize_issues(parsed)
        if parsed and not issues:
            # Non-empty array where every item was malformed: unparseable, not "no issues".
            return None
        return issues

    def _normalize_issues(self, arr: List[Any]) -> List[Dict[str, str]]:
        issues: List[Dict[str, str]] = []
        for item in arr:
            if not isinstance(item, dict):
                continue
            msg = str(item.get("msg", "")).strip()
            if not msg:
                continue
            issues.append(
                {
                    "type": str(item.get("type", "Issue")),
                    "severity": str(item.get("severity", "Unknown")),
                    "msg": msg,
                }
            )
        return issues[: self.MAX_ISSUES]

    def _wrap_code(self, code: str) -> str:
        """Fence untrusted code in a tag with a random suffix so it can't contain its own closing tag."""
        while True:
            tag = f"code_{secrets.token_hex(4)}"
            if tag not in code:
                return f"<{tag}>\n{code}\n</{tag}>"

    @staticmethod
    def _render(template: str, **values: str) -> str:
        """Single-pass {{KEY}} substitution, so substituted text is never re-scanned for placeholders."""
        return re.sub(r"\{\{(\w+)\}\}", lambda m: values.get(m.group(1), m.group(0)), template)

    def _try_json_loads(self, s: str) -> Any:
        try:
            return json.loads(s)
        except Exception:
            return None

    def _extract_first_json_array(self, s: str) -> Optional[str]:
        start = s.find("[")
        if start == -1:
            return None
        depth = 0
        for i in range(start, len(s)):
            if s[i] == "[":
                depth += 1
            elif s[i] == "]":
                depth -= 1
                if depth == 0:
                    return s[start : i + 1]
        return None

    def _strip_code_fences(self, text: str) -> str:
        text = text.strip()
        match = re.search(r"```(?:python)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1)
        return text

    def _can_call_llm(self) -> bool:
        return self.client is not None and hasattr(self.client, "complete")

    def _log(self, step: str, message: str) -> None:
        self.logs.append({"step": step, "message": message})
