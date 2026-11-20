## MODIFIED Requirements

### Requirement: Generate answers for the eval questions
The evaluation script SHALL have a mode that, for each question in the user's eval file, produces an answer the way `ask-coach` answers it: answering instructions, searches through the same search service with the same settings, and a Gemini model writing the answer. The answering instructions SHALL be the eval's own fixed copy of version 2 of the `ask` prompt's instructions, which link each post inline where its point is used, so that citation precision can be measured and earlier answers and grades stay comparable; changing the live `ask` prompt SHALL NOT change them. The answer SHALL be saved with the searches it ran and the passages it was given, so its grades can be checked against what it saw. The model, its settings and a version number for the answering instructions SHALL be recorded with each answer.

#### Scenario: Answer one question
- **WHEN** the user runs the answer evaluation on a file with 36 questions
- **THEN** each question gets one answer, saved with its searches and the passages they returned

#### Scenario: Searches use the live settings
- **WHEN** the answer evaluation runs
- **THEN** its searches go through the same search service, settings and index version that the MCP server's `search` tool uses

#### Scenario: The live prompt changes
- **WHEN** the `ask` prompt's instructions change and the answer evaluation runs again with nothing else changed
- **THEN** it uses the same version 2 instructions as before, records prompt version 2, and reuses the saved answers
