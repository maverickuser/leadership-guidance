## MODIFIED Requirements

### Requirement: Strip newsletter boilerplate
The system SHALL remove the standard newsletter intro paragraphs from the top of the article body: leading paragraphs whose text starts with "Welcome to my newsletter!" or "Free members can read", or with any prefix listed in the optional `boilerplate_prefixes` setting of the local `config.yaml` (whitespace-normalised). Site-specific prefixes SHALL live only in the local config, never in the repository. It MUST stop at the first paragraph that does not match, and MUST NOT remove matching text elsewhere in the body; repeated paragraphs elsewhere are removed at index time by the search boilerplate filter.

#### Scenario: Standard intro
- **WHEN** a post body opens with the "Welcome to my newsletter!" and "Free members can read" paragraphs
- **THEN** neither paragraph, nor any link inside them, appears in `text`, `sections` or `links`

#### Scenario: Configured intro
- **WHEN** `boilerplate_prefixes` lists "Hello from the example letter" and a post opens with a paragraph starting with those words, followed by "Free members can read…"
- **THEN** both paragraphs are removed

#### Scenario: Post-specific italic opener
- **WHEN** a post opens with its own italic note such as "I'm writing about something different today!"
- **THEN** that note is kept
