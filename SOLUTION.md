# PS-1: Find the Promos

## Problem choice and scope

Promo selection is a repeated editorial task: an editor watches short episodes and
finds moments that can hook a new viewer. This prototype is a rerunnable first-pass
assistant, not an automatic publishing system. It proposes and ranks one promo for
each of the 50 supplied episodes, then provides a separate related cross-episode
montage. It selects moments and timestamps only; it does not edit video, add music,
or create overlays.

## Assumptions

- Each episode promo uses 2 to 4 distinct moments from that episode; combined
  duration is 20 to 60 seconds.
- A cross-episode montage follows a connected dramatic arc. Episode loglines and
  continuity notes guide selection of 2 to 4 episodes, followed by 2 to 4 moments.
- Segment boundaries must be timestamps written in the screenplay. Valid
  timestamps do not prove that the rendered footage or transition is effective.
- Model scores are editorial ranking signals, not measured ad performance.

## Approach, scale, and ranking

The batch extractor reads screenplay text from a folder or ZIP. The Streamlit app
also accepts a local folder/ZIP path or uploaded ZIP/TXT files. A provider adapter
connects the extraction flow to Gemini (`gemini-3.5-flash-lite`) or an
OpenAI-compatible JSON chat API. Gemini proposes moments, a hook for the first
three seconds, an unresolved cliffhanger, a standalone rationale, and four scores
from 1 to 5. Python validates the response shape, source timecodes, order, overlap,
segment count, and combined duration. A second model pass selects a related
episode arc and proposes a timestamp-validated montage. Invalid responses are
retried with corrective feedback; exhausted failures are recorded. The Streamlit
prototype shows episode promos and the cross-episode montage in separate tabs.

The weighted score is 20 times (hook x 0.30 + cliffhanger x 0.30 + standalone
x 0.30 + timing x 0.10). Top episode candidates from this run:

| Rank | Episode | Score | Duration | Editorial reason to review |
|---|---|---:|---:|---|
| 1 | Episode 15 | 100/100 | 44.50 sec | Threatening intruders and a defender's arrival create immediate stakes. |
| 2 | Episode 1 | 98/100 | 28.20 sec | A wedding-day betrayal gives new viewers a clear emotional arc. |
| 3 | Episode 3 | 94/100 | 21.35 sec | A rescue leads to a dramatic proposal and an open question. |
| 4 | Episode 12 | 94/100 | 30.31 sec | A false arrest escalates into a high-stakes sacrifice. |
| 5 | Episode 14 | 94/100 | 22.83 sec | A revealed signet ring leads into a vow of revenge. |

These are candidates to review first based on the rubric, not a prediction of
clicks. The cross-episode montage scored 94/100 and uses three moments from
Episodes 41, 42, and 44; its total duration is 20.5 seconds.

For a larger slate, process each show's episodes as resumable jobs, throttle
requests to provider quotas, and aggregate rankings per show. The CLI supports
resume; queueing or controlled parallelism can be added if sequential processing
does not meet production throughput. Raw model responses are kept beside
validated outputs, and an editor remains the approval gate. The selected model
API receives screenplay text, so processing is subject to content-sharing terms.
This run used Gemini with a 15-second request interval. Each run needs at least
one generation call per episode plus two calls for cross-episode selection and
montage generation; retries add calls. Estimate cost from the resulting input and
output tokens and the selected model's current rates; costs were not measured for
this run.

## Quality check and limitations

The full input pack produced 50 of 50 episode promos, one cross-episode montage,
and zero final validation failures. All episode promos contain 2 to 4 moments,
use source-listed timestamps, and meet the 20 to 60 second duration requirement.
The montage has three source-valid moments across three episodes and totals 20.5
seconds. Sixteen offline tests cover folder, ZIP and uploaded input loading,
provider adapters, timestamp parsing, segment validation, ranking, retries and
resume, and cross-episode selection. Streamlit smoke tests confirm the saved
ranked results and controls for new screenplay inputs and provider configuration.

Observed model failures included proposing clips shorter than 20 seconds and an
edit boundary absent from a screenplay. Validation rejected these; seven episodes
needed corrective retries before passing. Scores are optimistic and bunched:
standalone clarity scored 4/5 for 48 of 50 episode promos. The screenplay pack
does not include rendered video, so visual continuity, emotional impact, and the
actual cut still require human review. A next evaluation would compare blind
editor ratings for hook, clarity, edit coherence, and cliffhanger against model
rankings.

## AI tools and deliverables

Gemini generated promo candidates. Copilot in VS Code assisted with
implementation, testing, and this write-up. Model outputs were checked with
deterministic validators and offline tests. A concrete model error was proposing
clips below the required duration (and an unavailable source timestamp); the
validator rejected those responses and required corrected output.

The runnable prototype is `app.py`, with local run steps in `README.md`. The
Streamlit interface presents ranked promos and can run new local or uploaded
screenplays. Gemini and OpenAI-compatible APIs are supported; a different API
protocol needs an adapter implementing the shared JSON generation interface.
The `raw_outputs/` folder and `raw_outputs.zip` contain
unedited model responses, validated episode promos, the selected episode arc,
the montage, and the ranked manifest. The solution document is within the
assignment's two-page limit.
