## ADDED Requirements

### Requirement: Generate answers for the eval questions
The evaluation script SHALL have a mode that, for each question in the user's eval file, produces an answer the way `ask-coach` answers it: the `ask` prompt's instructions, searches through the same search service with the same settings, and a Gemini model writing the answer. The answer SHALL be saved with the searches it ran and the passages it was given, so its grades can be checked against what it saw. The model, its settings and a version number for the answering instructions SHALL be recorded with each answer.

#### Scenario: Answer one question
- **WHEN** the user runs the answer evaluation on a file with 36 questions
- **THEN** each question gets one answer, saved with its searches and the passages they returned

#### Scenario: Searches use the live settings
- **WHEN** the answer evaluation runs
- **THEN** its searches go through the same search service, settings and index version that the MCP server's `search` tool uses

### Requirement: Grade each answer
An LLM judge SHALL grade each answer against the passages it was given and the question's relevant posts (its expected posts plus posts the retrieval judge graded 2 or 3). For each answer it SHALL report:
- **faithfulness:** the share of the answer's claims that the passage cited for them supports;
- **citation precision:** the share of citations that point at a post the answer's searches returned;
- **post coverage:** the share of the question's relevant posts that the answer draws on;
- **overall grade:** 0 to 3 for how well the answer serves the question.
The report SHALL give the mean of each, overall and per question type.

#### Scenario: Unsupported claim
- **WHEN** an answer states a claim and cites a post whose passages do not say it
- **THEN** that claim counts as unsupported in the answer's faithfulness

#### Scenario: Citation to a post never retrieved
- **WHEN** an answer cites a post that none of its searches returned
- **THEN** that citation counts against citation precision

#### Scenario: Narrow answer
- **WHEN** a question has 8 relevant posts and the answer draws on 3 of them
- **THEN** its post coverage is 3/8

### Requirement: Saved answers and grades
Answers and grades SHALL be saved in the user's data folder, never in the repository, and reused: an answer SHALL be generated again only when its question, the answering instructions' version, the model, or the search results it would receive have changed, and a grade SHALL be computed again only when its answer or the judge's version or model has changed. Each saved run SHALL be labelled, so two runs (for example before and after a search change) can be compared side by side.

#### Scenario: Re-run without changes
- **WHEN** the user runs the answer evaluation twice with nothing changed
- **THEN** the second run makes no Gemini calls and prints the same scores

#### Scenario: Compare two runs
- **WHEN** the user has a run labelled `baseline` and a run labelled `post-selection`
- **THEN** the report shows each score for both runs and the difference

### Requirement: Cost shown before spending
Before any Gemini call, the answer evaluation SHALL estimate the number of answers and grades to make and their token cost, and print it. It SHALL stop without calling Gemini when the estimate is over `search.answers.max_tokens_per_run`, unless the user confirms with `--yes`. A `--dry-run` SHALL print the estimate and change nothing.

#### Scenario: Dry run
- **WHEN** the user runs the answer evaluation with `--dry-run`
- **THEN** it prints how many answers and grades it would make and the estimated cost, and calls no model

#### Scenario: Over the ceiling
- **WHEN** the estimate is over `search.answers.max_tokens_per_run` and `--yes` is not given
- **THEN** it stops before any Gemini call and says to re-run with `--yes`

### Requirement: Answer evaluation errors
A Gemini failure for one question SHALL be recorded for that question and the run SHALL continue with the others; the report SHALL list the questions that have no answer or no grade. A missing `GEMINI_API_KEY` SHALL stop the run before any search, with a message saying to set it in `.env`. No answer text, passage text or key SHALL be written to the log.

#### Scenario: One answer fails
- **WHEN** Gemini returns an error for one of 36 questions
- **THEN** the other 35 are answered and graded, and the report lists the failed question

#### Scenario: No key
- **WHEN** `GEMINI_API_KEY` is not set
- **THEN** the run stops at once and says to set `GEMINI_API_KEY` in `.env`
