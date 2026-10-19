Run every day at **11:00 America/Los_Angeles**.

Process feedback the user left after reading Daily Briefs and perform any requested follow-up research under Section 12 of the corresponding Daily.

Do not rewrite, regenerate, or re-finalize the Daily Brief itself.

# 0. Scope

- Do not modify the **02:30 collection/selection pipeline**.
- `06.01. My Interest` is not consumed by the 02:30 job yet.
- Checkbox → Learned Interests collection is **not performed by this prompt**.
- `scripts/interest_collect.py` has already run immediately before this job. Its output is attached below as `script output`.
- If the script output contains `FAILED`, report the failure briefly and continue processing feedback. Do not block the feedback workflow.
- Do not modify:
  - Dashboard
  - 03–05
  - databases or templates under 02
  - existing Daily Sections 01–11
- Do not create Section 13.
- Section 12 is the only destination for follow-up research produced by this job.

# 1. Input

Configuration:

`config/notion_ids.json`

Relevant fields:

- `daily_data_source_id`
- `daily_sections_data_source_id`
- `feedback_data_source_id`
- `deep_dive_queue_data_source_id`
- `story_data_source_id`
- `watchlist_data_source_id`

Use the profile `.env` `NOTION_API_KEY` with the Notion REST API and:

`Notion-Version: 2026-03-11`

Do not use MCP/OAuth for this workflow.

Read-only API calls may be made directly.

**All writes must use the designated scripts described below.**

Search Daily Index pages for newly added or modified feedback under:

- `## My Feedback / Follow-up`
- the legacy feedback heading if an older Daily uses it

Do not apply a fixed day-based hard cutoff.

Use deduplication and existing processing records to determine whether feedback is new instead of assuming that older feedback should be ignored.

Ignore:

- HTML comments
- blank lines
- placeholder instructions such as `Leave feedback here.`
- text that was not written by the user

The Index page `Date` determines which Daily the feedback belongs to.

The result must always be associated with that Daily, even when the request was added later.

Resolve each feedback sentence to the corresponding article using the same day's Section pages and available context:

- `Parent Brief ID`
- issue ID such as `03-108`
- article title
- article content
- surrounding Daily context

Resolve natural references such as:

- `that DeepSeek article`
- `the second security item`
- `the one about the new inference system`

using context.

If the user explicitly provides an issue ID, that issue ID takes priority.

# 2. Deduplication

For every feedback sentence or bullet, generate:

`sha256(source_daily_page_id + "\n" + normalized_whitespace(sentence))`

Use this as the feedback key.

Before processing a request, check Feedback Inbox for an existing record with the same key.

If the same key already exists and has already been processed, do not process it again.

If the user edits the sentence, the modified text receives a new key and is treated as a new request.

Repeated executions must never cause the same unchanged request to be researched twice.

Do not rely on date windows for deduplication.

# 3. Classification

There are no required commands or syntax.

Interpret the user's natural language based on meaning.

One request may belong to more than one classification.

| Classification | Meaning | Action |
|---|---|---|
| `Deep Dive` | The user wants substantially more information or research | Perform additional research and create a report under Section 12. Do not directly affect Interest score. |
| `Follow-up Question` | The user asks a specific question related to an article | If the answer is already sufficiently supported by existing material, answer it directly under Section 12. If additional research is required, handle it as a Deep Dive. |
| `More Like This` | The user wants more content of this type | Record editorial feedback and add appropriate Learned Interest candidates. |
| `Less Like This` | The user wants less content of this type | Record the preference in editorial feedback. |
| `Report Style Preference` | The user requests changes to report structure, writing, detail, format, or presentation | Record the preference in editorial feedback. |
| `Source Preference` | The user expresses a preference for or against certain sources | Record the preference in editorial feedback. |
| `Keep Watching` | The user explicitly requests continued monitoring | Add or restore the relevant Watchlist item only when explicitly requested. |
| `PoC / Technical Validation Request` | The user asks for reproduction, experimentation, or validation | **Use Daybreak Blue for the PoC/attack-analysis section** (§4-A below). Perform actual validation when feasible and appropriate. Never claim execution if it was not actually performed. |

Editorial feedback should include:

- source Daily date
- source Daily URL
- original user feedback
- interpreted preference

Avoid duplicate editorial feedback entries.

Editorial feedback is **not connected to the 02:30 pipeline yet**.

For `Keep Watching`:

- only act when the user explicitly requests continued monitoring
- do not automatically restore entries lacking valid provenance
- do not automatically restore entries intentionally paused by the user
- never invent Watchlist entries

If a request cannot be interpreted with sufficient confidence, do not discard it.

Create an Inbox record containing:

- original request
- possible interpretations
- reason it could not be safely resolved

and leave it in the appropriate blocked/unresolved state from the live schema.

# 4. Follow-up Research

Process each request independently.

**A failure in one request must not prevent other requests from being processed.**

Create or update the initial Feedback Inbox record before beginning potentially long research.

## 4.1 Determine the Actual Research Question

**The user's request is the primary research target.**

The linked Daily article provides context and a starting point, but it does not automatically define the scope of the follow-up report.

First determine exactly what the user wants to know.

Convert the request into a clear research question before researching.

Examples:

### Specific technology, product, project, framework, protocol, company, or tool

If the user asks for more detail about the subject itself:

→ investigate the subject itself in depth.

Do not turn the report into an extended summary of the original news event merely because that is where the subject first appeared.

### Incident or vulnerability

If the user asks how something happened:

→ focus on sequence, cause, evidence, attack path, affected boundaries, and consequences.

### Model performance

If the user asks whether a model is good, fast, efficient, or better than another model:

→ investigate architecture, evaluation methodology, benchmarks, baselines, hardware conditions, limitations, and meaningful comparisons.

### Research paper

If the user wants to understand a paper:

→ investigate the problem, proposed method, assumptions, experiments, results, limitations, and significance.

### Implementation

If the user asks how something works internally:

→ inspect implementation details such as repository structure, modules, dependencies, code paths, execution flow, state, configuration, and integration points.

### Comparison

If the user asks to compare two or more things:

→ establish common comparison criteria and evaluate them directly against those criteria.

### PoC or validation

If the user asks whether something actually works:

→ prioritize actual validation or reproducibility where feasible.

If execution is not performed, clearly separate the proposed procedure from verified execution.

**A longer rewrite of the original Daily article is not a Deep Dive.**

The final report must directly answer the research question inferred from the user's request.

# 4.2 Research Method

Read, in this order:

1. the user's exact feedback
2. the linked Daily article
3. the sources already associated with that Daily article

Then perform additional research as required.

Do not restrict research to the sources already used by the Daily.

Depending on the subject, investigate appropriate primary and secondary material such as:

- official announcements
- official documentation
- specifications
- security advisories
- research papers
- model cards
- technical reports
- original repositories
- releases
- pinned commits
- implementation code
- issues
- pull requests
- changelogs
- public benchmarks
- benchmark methodology
- baseline systems
- deployment documentation
- credible independent technical analysis
- prior related Daily entries
- prior Story timelines

Prefer primary sources whenever they can answer the question directly.

Secondary sources may be used to:

- provide missing context
- independently corroborate claims
- reconstruct unavailable material
- explain operational details not documented by the original publisher

## 4.3 Missing or Removed Sources

If the original source is deleted, private, moved, or otherwise unavailable, do not automatically stop.

Search for relevant:

- archived copies
- preserved releases
- mirrors
- forks
- source snapshots
- derivative projects
- package distributions
- cached documentation
- credible third-party analysis

Use these materials carefully.

Clearly distinguish between:

- original material
- archived original material
- mirror or copy
- fork
- derivative project
- third-party description
- analyst inference

Do not treat a fork or derivative as proof of original behavior unless provenance and relevant implementation history support that conclusion.

# 4.4 Evidence Discipline

Do not treat search-result snippets as source material.

Whenever material claims differ in confidence or provenance, distinguish between:

- directly verified fact
- statement made by a source
- evidence-supported inference
- plausible hypothesis
- unresolved question

Do not invent:

- URLs
- publication dates
- release dates
- versions
- commits
- benchmarks
- benchmark scores
- features
- architectural components
- code behavior
- vulnerabilities
- execution results
- attack steps
- attribution

When available evidence does not answer something, say that it remains unknown.

Lack of public evidence is not evidence that something did not happen.

# 4.5 Topic-specific Research Depth

Use the research structure appropriate to the actual question.

Do not force all requests into the same framework.

## Technology / Product / Project / Tool

Investigate where relevant:

- what it is
- origin and development history
- purpose
- architecture
- major components
- execution lifecycle
- data flow
- control flow
- model/backend integration
- tools
- permissions
- state management
- memory
- scheduling
- dependencies
- configuration
- deployment
- APIs
- user interaction
- human-in-the-loop controls
- differences from similar systems
- limitations
- security properties
- real-world usage

## Security Incident / Vulnerability

Investigate where relevant:

- timeline
- affected systems
- threat model
- initial access
- initial privilege
- authentication weaknesses
- exploitation
- privilege escalation
- persistence
- lateral movement
- boundary crossing
- command execution
- data access
- exfiltration
- impact
- detection
- containment
- remediation
- residual risk
- attribution confidence

Separate demonstrated attack steps from hypothetical or technically possible ones.

## Paper / Model

Investigate where relevant:

- problem definition
- motivation
- core idea
- architecture
- method
- training procedure
- dataset
- evaluation setup
- benchmark
- baseline
- hardware
- metrics
- reported results
- comparison validity
- ablation results
- limitations
- practical implications

Do not repeat benchmark numbers without understanding what was actually measured.

## Implementation / Code

Investigate where relevant:

- repository layout
- major modules
- important classes or functions
- execution entry points
- initialization
- data flow
- control flow
- lifecycle
- dependencies
- configuration
- persistence
- APIs
- tool interfaces
- permission checks
- error handling
- tests
- known issues
- important commits
- relevant changes across versions

When code is publicly available, inspect actual implementation instead of relying only on README descriptions when implementation details matter.

## Comparison

Define common criteria before comparing.

Potential dimensions include:

- architecture
- capabilities
- implementation maturity
- performance
- cost
- resource requirements
- operational complexity
- reliability
- extensibility
- security
- maintainability
- deployment model
- ecosystem
- limitations
- best-fit scenarios

Avoid declaring a universal winner when the answer depends on workload or context.

# 4.6 Reproduction and Validation

## 4-A. Daybreak Blue for PoC / Attack Analysis

When the request involves offensive security, vulnerability reproduction, exploit analysis, or attack-path modeling, delegate the PoC/attack-analysis section to Daybreak Blue:

1. Write the context (article text, source URLs, user instructions) to `/tmp/poc-context-<key>.md`.
2. Run:
   ```
   python scripts/poc_writer.py \
     --topic "<short topic>" \
     --context-file /tmp/poc-context-<key>.md \
     --out /tmp/poc-output-<key>.md
   ```
3. Read the output and incorporate it into the Deep Dive report as a section (e.g. `## 공격 경로 분석` or `## 재현 절차 (PoC)`).
4. The rest of the report (핵심 결론, 무엇을 조사했나, 출처 등) is still written by this agent (gpt-6-luna).
5. If `poc_writer.py` fails (quota, timeout, error), write the PoC section yourself and note that Daybreak was unavailable.

Do NOT use Daybreak for non-security requests (model analysis, tool comparison, paper review, etc.).

## 4-A end

If actual execution is requested, determine whether meaningful validation can be performed safely and locally.

When execution occurs, record:

- environment
- relevant versions
- command or procedure
- inputs
- expected result
- observed result
- discrepancies
- limitations

Only claim that something was executed or validated when it actually was.

If no execution occurred, describe the work accurately as:

- source-based analysis
- literature-based analysis
- proposed reproduction procedure
- proposed PoC
- unexecuted validation plan

Do not fabricate successful reproduction.

# 4.7 Report Construction

Write the report to:

`/tmp/followup-<first 8 chars of feedback key>.md`

The structure must follow the user's actual research question.

Do not force every report into a fixed checklist.

Use this general pattern:

```text id="ik6pqm"
# Deep Dive — <Topic>
> Request source: YYYY-MM-DD Daily · <issue ID> <article title>

## Key Conclusion

## <Sections required to answer the actual research question>

## Confirmed vs Unconfirmed

## Limitations

## Practical Significance

## Open Questions

## Glossary

## Sources
```

Add, remove, rename, or reorder sections when doing so produces a clearer answer.

For example, a technical project may use:

```text id="6khkj1"
## What It Is
## Origin and Development
## Architecture
## Major Components
## Execution Flow
## Models and Tools
## State and Memory
## Permissions and Controls
## Deployment
## How It Differs From Existing Approaches
## Real-world Usage
## Limitations
```

A security incident may instead use:

```text id="c22j99"
## Timeline
## Initial Access
## Attack Path
## Boundary Crossings
## Impact
## Detection and Response
## Attribution
## Remaining Unknowns
```

A research paper may instead