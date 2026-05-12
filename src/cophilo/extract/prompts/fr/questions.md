# Extraction de questions — français

Vous recevez un document philosophique découpé en passages numérotés. Identifiez les **questions ouvertes** auxquelles l'auteur se confronte — soulevées, reformulées, abordées ou résolues.

## Règles

1. **`role` :**
   - `raise` — le passage pose la question (explicitement ou implicitement)
   - `reformulate` — le passage reformule plus précisément une question antérieure
   - `attempt` — le passage tente une réponse sans la trancher
   - `answer` — le passage adopte une réponse
2. **`label`** est court (moins de 12 mots) et descriptif — utile pour identifier la question à travers les passages.
3. **`description`** est une phrase énonçant la question.
4. **`explicit`** : vrai si le passage utilise une formulation interrogative ou dit explicitement « la question de X » ; faux si la question n'est que sous-entendue.
5. **`span_quote`** : une sous-chaîne exacte (≤ 200 caractères) ancrant la question.
6. Ignorez les questions rhétoriques dont la réponse est manifestement présupposée dans le même passage.
7. Confiance ≥ 0.4.

## Document

Titre : {title}
Langue : {language}

{passages}

Renvoyez un objet JSON conforme au schéma `QuestionPassResponse`.
