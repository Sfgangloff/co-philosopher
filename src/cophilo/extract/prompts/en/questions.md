# Question extraction — English

You will receive a philosophical document split into numbered passages. Identify the **open questions** the author is grappling with — questions raised, reformulated, attempted, or answered.

## Rules

1. **`role`:**
   - `raise` — the passage poses the question (explicitly or implicitly)
   - `reformulate` — the passage restates an earlier question more precisely
   - `attempt` — the passage works toward an answer without settling it
   - `answer` — the passage commits to an answer
2. **`label`** is short (under 12 words) and headline-like — useful to identify the question across passages.
3. **`description`** is one sentence stating the question.
4. **`explicit`**: true if the passage uses interrogative phrasing or explicitly says "the question of X"; false if the question is only implied.
5. **`span_quote`**: a verbatim substring (≤ 200 chars) anchoring the question.
6. Skip rhetorical questions whose answer is obviously presupposed in the same passage.
7. Confidence ≥ 0.4.

## Document

Title: {title}
Language: {language}

{passages}

Return a JSON object matching the `QuestionPassResponse` schema.
