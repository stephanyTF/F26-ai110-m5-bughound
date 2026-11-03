# BugHound Mini Model Card (Reflection)

Fill this out after you run BugHound in **both** modes (Heuristic and Gemini).

---

## 1) What is this system?

**Name:** BugHound  
**Purpose:** Analyze a Python snippet, propose a fix, and run reliability checks before suggesting whether the fix should be auto-applied.

**Intended users:** Students learning agentic workflows and AI reliability concepts.

---

## 2) How does it work?

- **PLAN** → run() where self.log is updated on each of the workflow steps of the Bug Hound agent as it processed the entered code.

- **ANALYZE** → analyze(): the user’s function / code is passed to the LLM along with the system prompt and user template
    - If there is no client, an API error occurs, or the output isn't parseable JSON, it falls back to _heuristic_analyze.
    - The heuristics check only for print(, a bare except:, and TODO
    - Heuristics = runs w/o api

- **ACT** → propose_fix() if there were issues found, BugHound  calls the LLM with the issues JSON and the code and retrieve the LLM’s revised code 
    - Fallbacks go to _heuristic_fix on an API error or an empty reply. Without a client it uses _heuristic_fix directly.

- **TEST** → assess_risk(),
    - Takes in original_code (str), fixed_code (str) and issues (a list of dicts
    - Outputs a list of dictionaries consisting of:
        - score: int from 0 to 100
        - Severity level: "low" | "medium" | "high"
            - Severity is based on the original submitted code reasons: list of strings explaining deductions
        - should_autofix: bool

- **REFLECT:** 
    - Considers if the revised code is safe enough to apply, based on the boolean results “should_autofix var” from assess_risk()


**Heuristics vs. Gemini:**
- Heuristics runs if there’s no api key or LLM available
- Falls back from Gemini to Heuristics if the LLM returns an empty output


---

## 3) Inputs and outputs

**Inputs:**

    print_spam.py (simple function w/ repetitive print statements
    ```
        def greet(name):
            print("Hello", name)
            print("Welcome!")
            return True
    ```

**Outputs:**

- What types of issues were detected?
    - 1. Maintainability | Low (Use of print statements in non-demo code.)
- What kinds of fixes were proposed?
    - 
        ``` 
            def greet(name):
               return True

        ```
- What did the risk report show?
    - Risk Level: Low
    - Score 95
    - Auto-fix: YES
    - Reasons: Low severity issue detected.

---

## 4) Reliability and safety rules

List at least **two** reliability rules currently used in `assess_risk`. For each:

- What does the rule check?
- Why might that check matter for safety or correctness?
- What is a false positive this rule could cause?
- What is a false negative this rule could miss?

- What does the rule check?
- Why might that check matter for safety or correctness?
- What is a false positive this rule could cause?
- What is a false negative this rule could miss?

---

## 5) Observed failure modes

Provide at least **two** examples:

1. A time BugHound missed an issue it should have caught  
2. A time BugHound suggested a fix that felt risky, wrong, or unnecessary  

For each, include the snippet (or describe it) and what went wrong.

---

## 6) Heuristic vs Gemini comparison

Compare behavior across the two modes:

- What did Gemini detect that heuristics did not?
- What did heuristics catch consistently?
- How did the proposed fixes differ?
- Did the risk scorer agree with your intuition?

---

## 7) Human-in-the-loop decision

Describe one scenario where BugHound should **refuse** to auto-fix and require human review.

- What trigger would you add?
- Where would you implement it (risk_assessor vs agent workflow vs UI)?
- What message should the tool show the user?

---

## 8) Improvement idea

Propose one improvement that would make BugHound more reliable *without* making it dramatically more complex.

Examples:

- A better output format and parsing strategy
- A new guardrail rule + test
- A more careful “minimal diff” policy
- Better detection of changes that alter behavior

Write your idea clearly and briefly.
