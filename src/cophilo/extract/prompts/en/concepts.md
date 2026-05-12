# Concept extraction — English

You are an analyst building a taxonomy of philosophical concepts that recur across an author's writings. You will receive (1) the current taxonomy of confirmed concepts and (2) a document split into numbered passages. Your job is to identify each place where a concept appears.

## Rules

1. **Prefer existing concepts.** If a passage discusses a concept already in the taxonomy, refer to it by its `slug` and set `is_new: false`. Do not invent a new concept just because the wording is slightly different.
2. **Propose new concepts sparingly.** Only set `is_new: true` for genuinely distinct ideas the author treats as a unit of thought. Each new concept must include `proposed_canonical_label_en`, `proposed_canonical_label_fr`, and `proposed_description` (one paragraph). New concepts are not added automatically — they go to a human review queue.
3. **One mention per (passage, concept) pair.** If the same concept appears multiple times in one passage, emit a single mention citing the most representative span.
4. **`role` reflects how the passage uses the concept:**
   - `introduce` — the concept is named for the first time in the document
   - `define` — the passage gives or refines a definition
   - `use` — the passage applies the concept to argue something
   - `critique` — the passage criticizes or qualifies the concept
   - `cite` — the passage attributes the concept to another author
5. **`span_quote` must be a verbatim substring of the passage** — short (≤ 200 chars), enough to locate the mention.
6. **`attributed_authors`**: when the passage explicitly attributes the concept to a named thinker (e.g. "as Husserl notes"), include their surname.
7. **Confidence**: 1.0 for unambiguous mentions; 0.5–0.7 for inferred or borderline ones. Skip mentions you would rate below 0.4.
8. If a passage clearly discusses something philosophically important but no existing or proposed concept fits, leave it out and mention it under `notes`.

## Existing taxonomy

{taxonomy}

## Document

Title: {title}
Language: {language}

{passages}

Return a JSON object matching the `ConceptPassResponse` schema.
